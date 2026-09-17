"""Bounded CPU inference for the unchanged, combined Facing the Music BandIt."""
from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator._bandit import (
    CHUNK_FRAMES,
    COMBINED_CHECKPOINT,
    HOP_FRAMES,
    SAMPLE_RATE,
    load_manifest,
)
from choicer_voicer_pack_creator.export_resources import WorkEstimate
from choicer_voicer_pack_creator.separation import (
    BLOCK_FRAMES,
    PEAK_LIMIT,
    SeparationError,
    check_cancel,
    validate_audio,
)

Predict = Callable[[Any], Mapping[str, Any]]
# CPU 2.8.0, one thread, real 384000-sample mono probe: 2,133,352,448-byte
# peak working set / 2,297,196,544-byte peak commit (37,022,688 parameters).
# Reserve 3 GiB for runtime, load, activations, stereo buffers and margin.
# The eight-second model is never reduced to meet this reservation.
WORK_ESTIMATE = WorkEstimate(memory_bytes=3 * 1024**3, cpu_threads=2)


def configure_cpu(threads: int) -> Any:
    if type(threads) is not int or not 1 <= threads <= 2:
        raise SeparationError("BandIt requires an admitted CPU thread count of one or two")
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS"):
        os.environ[name] = str(threads)
    try:
        import torch
        import torchaudio
    except (ImportError, OSError) as error:
        raise SeparationError(
            "The optional singing-preserving CPU runtime is unavailable. Source users: follow "
            "README's singing setup (pinned CPU wheels, then the singing extra). Portable users: "
            "re-extract the complete application; do not install Python or CUDA."
        ) from error
    if (
        torch.__version__ != "2.8.0+cpu" or torchaudio.__version__ != "2.8.0+cpu"
        or torch.version.cuda is not None
    ):
        raise SeparationError("BandIt requires the pinned torch/torchaudio 2.8.0+cpu runtime, not CUDA")
    torch.set_num_threads(threads)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    return torch


def load_cpu_model(path: Path, threads: int) -> Any:
    from choicer_voicer_pack_creator._bandit import load_model

    load_manifest()
    configure_cpu(threads)
    return load_model(path, "cpu")


def make_predictor(model: Any) -> Predict:
    def predict(block: Any) -> dict[str, Any]:
        import numpy as np
        import torch

        if (
            block.ndim != 2 or block.shape[1] != 2 or block.dtype != np.float32
            or not np.isfinite(block).all()
        ):
            raise SeparationError("BandIt requires finite float32 stereo input")
        stems = COMBINED_CHECKPOINT.stems
        result = {stem: np.empty_like(block) for stem in stems}
        with torch.inference_mode():
            for channel in range(2):
                tensor = torch.from_numpy(block[:, channel].copy())[None, None]
                batch = model({"mixture": {"audio": tensor}})
                estimates = batch.get("estimates") if isinstance(batch, Mapping) else None
                if not isinstance(estimates, Mapping) or set(estimates) != set(stems):
                    raise SeparationError("BandIt returned an unexpected stem layout")
                for stem in stems:
                    entry = estimates[stem]
                    audio = entry.get("audio") if isinstance(entry, Mapping) else None
                    if (
                        not isinstance(audio, torch.Tensor)
                        or tuple(audio.shape) != (1, 1, len(block))
                        or audio.dtype != torch.float32 or audio.device.type != "cpu"
                        or not torch.isfinite(audio).all()
                    ):
                        raise SeparationError(f"BandIt returned invalid or non-finite {stem} audio")
                    result[stem][:, channel] = audio[0, 0].numpy()
                del batch, estimates, audio, tensor
        return result

    return predict


def resample_stream(
    source_path: Path, destination: Path, frames: int, cancelled: Callable[[], bool],
    *, sample_rate: int = SAMPLE_RATE, block_frames: int = BLOCK_FRAMES,
) -> None:
    """Phase-aligned FIR-context blocks equivalent to SciPy's whole-array resample_poly."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    if type(frames) is not int or frames <= 0 or block_frames <= 0 or sample_rate <= 0:
        raise SeparationError("Invalid resampling timeline or block size")
    with sf.SoundFile(source_path) as source, sf.SoundFile(
        destination, "w", samplerate=sample_rate, channels=2, format="RF64", subtype="FLOAT",
    ) as target:
        if source.channels != 2 or source.frames <= 0:
            raise SeparationError("Resampling requires nonempty stereo audio")
        divisor = math.gcd(source.samplerate, sample_rate)
        up, down = sample_rate // divisor, source.samplerate // divisor
        natural_frames = (source.frames * up + down - 1) // down
        # SciPy's default FIR half-length is 10 * max(up, down); include its
        # centering pre-pad, and align every local input origin to the down phase.
        halo = (10 * max(up, down) + down + up - 1) // up + 2
        for start in range(0, frames, block_frames):
            check_cancel(cancelled)
            stop = min(frames, start + block_frames)
            useful_stop = min(stop, natural_frames)
            result = np.zeros((stop - start, 2), dtype=np.float32)
            if useful_stop > start:
                first = max(0, ((start * down // up - halo) // down) * down)
                last = min(source.frames, (useful_stop * down + up - 1) // up + halo)
                source.seek(first)
                block = source.read(last - first, dtype="float32", always_2d=True)
                if len(block) != last - first or not np.isfinite(block).all():
                    raise SeparationError("Decoded source audio is incomplete or non-finite")
                converted = resample_poly(block, up, down, axis=0)
                offset = start - first * up // down
                result[:useful_stop - start] = converted[offset:offset + useful_stop - start]
            if not np.isfinite(result).all():
                raise SeparationError("Resampling produced non-finite audio")
            target.write(result)
        target.flush()


def overlap_add_blocks(
    source: Any, predict: Predict, frames: int, progress: Callable[[str, float | None], None],
    cancelled: Callable[[], bool], *, chunk_frames: int = CHUNK_FRAMES,
    hop_frames: int = HOP_FRAMES,
) -> Iterator[Any]:
    """Yield completed stereo samples, retaining only one window of pending overlap."""
    import numpy as np

    if not 0 < hop_frames < chunk_frames or frames <= 0:
        raise SeparationError("Invalid BandIt chunk or timeline configuration")
    window = np.hanning(chunk_frames + 2)[1:-1].astype(np.float32)
    pending = np.zeros((chunk_frames, 2), dtype=np.float32)
    weights = np.zeros(chunk_frames, dtype=np.float32)
    overlap = chunk_frames - hop_frames
    total = (frames + hop_frames - 1) // hop_frames
    for index, start in enumerate(range(0, frames, hop_frames)):
        check_cancel(cancelled)
        source.seek(start)
        block = source.read(chunk_frames, dtype="float32", always_2d=True)
        length = min(chunk_frames, frames - start)
        if block.shape != (length, 2) or not np.isfinite(block).all():
            raise SeparationError("Decoded source audio is incomplete or non-finite")
        mix = np.zeros((chunk_frames, 2), dtype=np.float32)
        mix[:length] = block
        progress(f"Keeping singing locally: chunk {index + 1} of {total}…", index / total * 0.9)
        predictions = predict(mix)
        check_cancel(cancelled)
        if not isinstance(predictions, Mapping) or set(predictions) != set(COMBINED_CHECKPOINT.stems):
            raise SeparationError("BandIt returned an unexpected stem layout")
        for stem, audio in predictions.items():
            if (
                not isinstance(audio, np.ndarray) or audio.shape != mix.shape
                or audio.dtype != np.float32 or not np.isfinite(audio).all()
            ):
                raise SeparationError(f"BandIt returned invalid or non-finite {stem} audio")
        backing = predictions["music"] + predictions["sfx"]
        pending[:length] += backing[:length] * window[:length, None]
        weights[:length] += window[:length]
        emit = min(hop_frames, frames - start)
        samples = pending[:emit] / weights[:emit, None]
        if not np.isfinite(samples).all():
            raise SeparationError("BandIt overlap-add produced non-finite audio")
        yield samples
        pending[:overlap] = pending[hop_frames:].copy()
        pending[overlap:] = 0
        weights[:overlap] = weights[hop_frames:].copy()
        weights[overlap:] = 0


def separate_stream(
    source_path: Path, output_path: Path, predict: Predict, frames: int,
    progress: Callable[[str, float | None], None], cancelled: Callable[[], bool],
    *, chunk_frames: int = CHUNK_FRAMES, hop_frames: int = HOP_FRAMES,
) -> None:
    import numpy as np
    import soundfile as sf

    raw_path = output_path.with_name("unscaled-bandit.wav")
    peak = 0.0
    try:
        with sf.SoundFile(source_path) as source, sf.SoundFile(
            raw_path, "w", samplerate=SAMPLE_RATE, channels=2, format="RF64", subtype="FLOAT",
        ) as raw:
            if (source.frames, source.samplerate, source.channels) != (frames, SAMPLE_RATE, 2):
                raise SeparationError("Decoded source audio has an incorrect duration or format")
            for samples in overlap_add_blocks(
                source, predict, frames, progress, cancelled,
                chunk_frames=chunk_frames, hop_frames=hop_frames,
            ):
                peak = max(peak, float(np.max(np.abs(samples))))
                raw.write(samples)
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
        validate_audio(output_path, frames, cancelled, sample_rate=SAMPLE_RATE)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        raw_path.unlink(missing_ok=True)


def smoke_test(job: Path) -> dict[str, Any]:
    """Offline native architecture test, not checkpoint quality or production-size evidence."""
    if any(name == "ctranslate2" or name.startswith("ctranslate2.") for name in sys.modules):
        raise SeparationError("The BandIt worker unexpectedly imported CTranslate2")
    torch = configure_cpu(1)

    import numpy as np
    import soundfile as sf
    import torchaudio

    from choicer_voicer_pack_creator._bandit.models.bandit.bandit import Bandit

    manifest = load_manifest()
    torch.manual_seed(0)
    model = Bandit(**manifest["parameters"]).eval().to("cpu")
    predict = make_predictor(model)
    frames = 4097
    samples = np.column_stack((
        np.sin(np.arange(frames, dtype=np.float32) * 0.07) * 0.1,
        np.cos(np.arange(frames, dtype=np.float32) * 0.03) * 0.1,
    )).astype(np.float32)
    source, output = job / "decoded-bandit.wav", job / "backing-bandit.wav"
    sf.write(source, samples, SAMPLE_RATE, subtype="FLOAT")
    separate_stream(source, output, predict, frames, lambda *_: None, lambda: False,
                    chunk_frames=4096, hop_frames=2048)
    actual, rate = sf.read(output, dtype="float32", always_2d=True)
    if (
        rate != SAMPLE_RATE or actual.shape != samples.shape or not np.isfinite(actual).all()
        or not np.any(actual) or np.array_equal(actual[:, 0], actual[:, 1])
    ):
        raise SeparationError("BandIt native CPU stereo smoke verification failed")
    qt_imported = any(name.startswith("PySide6") for name in sys.modules)
    if qt_imported:
        raise SeparationError("The BandIt worker unexpectedly imported Qt")
    ctranslate2_imported = any(
        name == "ctranslate2" or name.startswith("ctranslate2.") for name in sys.modules
    )
    if ctranslate2_imported:
        raise SeparationError("The BandIt worker unexpectedly imported CTranslate2")
    return {
        "frames": frames, "sample_rate": rate, "torch": torch.__version__,
        "channels": 2, "stems": list(COMBINED_CHECKPOINT.stems), "finite": True,
        "torchaudio": torchaudio.__version__, "cuda": torch.version.cuda,
        "threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads(),
        "numpy": np.__version__, "qt_imported": qt_imported,
        "ctranslate2_imported": ctranslate2_imported,
    }
