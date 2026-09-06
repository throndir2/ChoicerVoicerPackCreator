"""Qt-free local CPU worker. Streaming overlap-add adapted from StemSplit's MIT infer.py.

See resources/backing-separation.json and StemSplit-MIT.txt for immutable provenance.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.separation import (
    BLOCK_FRAMES,
    CHUNK_FRAMES,
    OVERLAP_FRAMES,
    PEAK_LIMIT,
    SAMPLE_RATE,
    SeparationError,
    check_cancel,
    default_manifest_path,
    validate_audio,
    verify_model_file,
    write_json_atomic,
)


def separate_stream(
    source_path: Path, output_path: Path, session: Any, frames: int,
    progress: Callable[[str, float | None], None], cancelled: Callable[[], bool],
    *, chunk_frames: int = CHUNK_FRAMES, overlap_frames: int = OVERLAP_FRAMES,
) -> None:
    import numpy as np
    import soundfile as sf

    if not 0 < overlap_frames < chunk_frames or frames <= 0:
        raise SeparationError("Invalid separation chunk or timeline configuration")
    stride = chunk_frames - overlap_frames
    window = np.ones(chunk_frames, dtype=np.float32)
    # Nonzero endpoints preserve the first/last sample instead of dividing zero by epsilon.
    fade = np.linspace(0, 1, overlap_frames + 2, dtype=np.float32)[1:-1]
    window[:overlap_frames], window[-overlap_frames:] = fade, fade[::-1]
    pending = np.zeros((chunk_frames, 2), dtype=np.float32)
    weights = np.zeros(chunk_frames, dtype=np.float32)
    raw_path = output_path.with_name("unscaled.wav")
    peak = 0.0
    try:
        with sf.SoundFile(source_path) as source, sf.SoundFile(
            raw_path, "w", samplerate=SAMPLE_RATE, channels=2, format="RF64", subtype="FLOAT",
        ) as raw:
            if (source.frames, source.samplerate, source.channels) != (frames, SAMPLE_RATE, 2):
                raise SeparationError("Decoded source audio has an incorrect duration or format")
            total_chunks = (frames + stride - 1) // stride
            for index, start in enumerate(range(0, frames, stride)):
                check_cancel(cancelled)
                source.seek(start)
                block = source.read(chunk_frames, dtype="float32", always_2d=True)
                length = len(block)
                if length != min(chunk_frames, frames - start) or not np.isfinite(block).all():
                    raise SeparationError("Decoded source audio is incomplete or non-finite")
                mix = np.zeros((1, 2, chunk_frames), dtype=np.float32)
                mix[0, :, :length] = block.T
                progress(f"Separating locally: chunk {index + 1} of {total_chunks}…",
                         index / total_chunks * 0.9)
                predictions = session.run(["stems"], {"mix": mix})[0]
                check_cancel(cancelled)
                if predictions.shape != (1, 4, 2, chunk_frames):
                    raise SeparationError("The separation model returned an unexpected stem layout")
                backing = predictions[0, :3].sum(axis=0).T
                if not np.isfinite(backing).all():
                    raise SeparationError("The separation model returned non-finite audio")
                pending[:length] += backing[:length] * window[:length, None]
                weights[:length] += window[:length]
                emit = min(stride, frames - start)
                samples = pending[:emit] / weights[:emit, None]
                if not np.isfinite(samples).all():
                    raise SeparationError("Separation overlap-add produced invalid audio")
                peak = max(peak, float(np.max(np.abs(samples))))
                raw.write(samples)
                pending[:overlap_frames] = pending[stride:].copy()
                pending[overlap_frames:] = 0
                weights[:overlap_frames] = weights[stride:].copy()
                weights[overlap_frames:] = 0
            raw.flush()
        gain = min(1.0, PEAK_LIMIT / peak) if peak else 1.0
        progress(f"Writing full-length backing track (safety gain {gain:.3f})…", 0.9)
        file_format = "RF64" if frames * 6 > 0xFFFFFFFF - 4096 else "WAV"
        with sf.SoundFile(raw_path) as raw, sf.SoundFile(
            output_path, "w", samplerate=SAMPLE_RATE, channels=2,
            format=file_format, subtype="PCM_24",
        ) as output:
            for block in raw.blocks(blocksize=BLOCK_FRAMES, dtype="float32", always_2d=True):
                check_cancel(cancelled)
                output.write(block * gain)
            output.flush()
        with output_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        validate_audio(output_path, frames, cancelled)
    finally:
        raw_path.unlink(missing_ok=True)


def load_session(model: Path) -> Any:
    import onnxruntime as ort

    metadata = json.loads(default_manifest_path().read_text(encoding="utf-8"))["model"]
    if not verify_model_file(model, metadata["bytes"], metadata["sha256"], lambda: False):
        raise SeparationError(
            "The separation model changed or is invalid. Restart generation to verify or repair it."
        )
    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 2) - 1))
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(
        str(model), sess_options=options, providers=["CPUExecutionProvider"],
    )
    if (
        len(session.get_inputs()) != 1
        or session.get_inputs()[0].name != "mix"
        or session.get_inputs()[0].shape != [1, 2, CHUNK_FRAMES]
        or session.get_inputs()[0].type != "tensor(float)"
        or len(session.get_outputs()) != 1
        or session.get_outputs()[0].name != "stems"
        or session.get_outputs()[0].shape != [1, 4, 2, CHUNK_FRAMES]
    ):
        raise SeparationError("The local model has an unsupported input/output signature")
    return session


def worker_main(request_path: Path) -> int:
    job = request_path.resolve().parent
    status_path = job / "status.json"
    output_path = job / "backing.wav"
    job_id = job.name

    def status(message: str, fraction: float | None, state: str = "running") -> None:
        write_json_atomic(status_path, {
            "job_id": job_id, "state": state, "message": message, "progress": fraction,
        })

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if request.get("version") != 1 or request.get("job_id") != job_id:
            raise SeparationError("Invalid separation worker request")
        if request.get("smoke_test") is True:
            report = smoke_test(job)
            if any(name.startswith("PySide6") for name in sys.modules):
                raise SeparationError("The separation worker unexpectedly imported Qt")
            write_json_atomic(job / "smoke.json", report)
        else:
            frames = request.get("frames")
            if type(frames) is not int or frames <= 0:
                raise SeparationError("Invalid separation worker frame count")
            status("Loading the verified local CPU model…", None)
            session = load_session(Path(request["model"]))
            separate_stream(job / "decoded.wav", output_path, session, frames, status, lambda: False)
        status("Full-length backing track generated and verified.", 1.0, "succeeded")
        return 0
    except Exception as error:
        # This process boundary must report native/runtime import errors even without a console.
        output_path.unlink(missing_ok=True)
        status(f"{type(error).__name__}: {error}", None, "failed")
        return 1


def smoke_test(job: Path) -> dict[str, Any]:
    import numpy as np
    import onnxruntime as ort
    import soundfile as sf

    class SyntheticStems:
        def run(self, _outputs: list[str], inputs: dict[str, Any]) -> list[Any]:
            mix = inputs["mix"]
            return [np.stack([mix * 0.1, mix * 0.2, mix * 0.3, mix * 5], axis=1)]

    if "CPUExecutionProvider" not in ort.get_available_providers():
        raise SeparationError("ONNX Runtime CPU execution is unavailable")
    # A tiny application-owned ONNX Identity graph exercises the native CPU runtime offline.
    identity = bytes.fromhex(
        "08083a410a100a017812017922084964656e74697479120b6c6f63616c2d736d6f6b65"
        "5a0f0a0178120a0a08080112040a020802620f0a0179120a0a08080112040a0208024202100d"
    )
    runtime = ort.InferenceSession(identity, providers=["CPUExecutionProvider"])
    probe = np.array([0.25, -0.5], dtype=np.float32)
    if not np.array_equal(runtime.run(["y"], {"x": probe})[0], probe):
        raise SeparationError("ONNX Runtime CPU inference smoke verification failed")
    frames = 83
    source = job / "decoded.wav"
    output = job / "backing.wav"
    samples = np.linspace(-0.5, 0.5, frames * 2, dtype=np.float32).reshape(frames, 2)
    sf.write(source, samples, SAMPLE_RATE, subtype="FLOAT")
    separate_stream(source, output, SyntheticStems(), frames, lambda *_: None, lambda: False,
                    chunk_frames=32, overlap_frames=8)
    actual, rate = sf.read(output, dtype="float32", always_2d=True)
    if rate != SAMPLE_RATE or not np.allclose(actual, samples * 0.6, atol=2e-7):
        raise SeparationError("Packaged streaming separation smoke verification failed")
    return {"frames": frames, "sample_rate": rate, "numpy": np.__version__,
            "onnxruntime": ort.__version__, "soundfile": sf.__version__, "qt_imported": False}
