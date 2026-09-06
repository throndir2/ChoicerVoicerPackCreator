"""Bounded, Qt-free inference target for the existing supervised process worker."""
from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.analysis import _run_cancellable
from choicer_voicer_pack_creator.speaker_matching import (
    ACTIVITY_WINDOW_SECONDS,
    MAX_CLIP_SECONDS,
    MIN_ACTIVE_SECONDS,
    MIN_RMS,
    SAMPLE_RATE,
    SpeakerClip,
    SpeakerMatchingError,
    normalized_embedding,
    verify_model,
)


def fbank_features(samples: Any) -> Any:
    import kaldi_native_fbank as knf
    import numpy as np

    options = knf.FbankOptions()
    options.frame_opts.samp_freq = SAMPLE_RATE
    options.frame_opts.frame_length_ms = 25
    options.frame_opts.frame_shift_ms = 10
    options.frame_opts.preemph_coeff = 0.97
    options.frame_opts.dither = 0
    options.frame_opts.snip_edges = True
    options.frame_opts.window_type = "hamming"
    options.mel_opts.num_bins = 80
    options.mel_opts.low_freq = 20
    options.mel_opts.high_freq = 0
    bank = knf.OnlineFbank(options)
    bank.accept_waveform(SAMPLE_RATE, (samples * 32768).tolist())
    bank.input_finished()
    features = np.stack([bank.get_frame(index) for index in range(bank.num_frames_ready)])
    features = features.astype(np.float32)
    features -= features.mean(axis=0, keepdims=True)
    if not np.isfinite(features).all():
        raise SpeakerMatchingError("Speaker preprocessing produced non-finite features")
    return features[None, :, :]


def usable_audio(samples: Any) -> tuple[Any | None, str]:
    import numpy as np

    if not np.isfinite(samples).all():
        return None, "nonfinite"
    if len(samples) < round(SAMPLE_RATE * MIN_ACTIVE_SECONDS):
        return None, "short"
    width = round(SAMPLE_RATE * ACTIVITY_WINDOW_SECONDS)
    starts = np.arange(0, len(samples), width)
    lengths = np.minimum(width, len(samples) - starts)
    energy = np.add.reduceat(samples.astype(np.float64) ** 2, starts)
    active = np.flatnonzero(energy >= MIN_RMS**2 * lengths)
    if not len(active):
        return None, "silence"
    # Count the final partial window by its actual length, not a full or missing 20 ms.
    if int(lengths[active].sum()) < round(SAMPLE_RATE * MIN_ACTIVE_SECONDS):
        return None, "short"
    first = max(0, int(active[0]) - 1) * width
    last = min(len(samples), (int(active[-1]) + 2) * width)
    return samples[first:last], ""


def load_session(model: Path) -> Any:
    import onnxruntime as ort

    if not verify_model(model, lambda: False):
        raise SpeakerMatchingError("The speaker model changed or is invalid; verify or repair it.")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = ort.InferenceSession(
        str(model), sess_options=options, providers=["CPUExecutionProvider"],
    )
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if (
        len(inputs) != 1 or inputs[0].type != "tensor(float)"
        or len(inputs[0].shape) != 3 or inputs[0].shape[-1] != 80
        or len(outputs) != 1 or outputs[0].type != "tensor(float)"
        or len(outputs[0].shape) != 2 or outputs[0].shape[-1] != 256
    ):
        raise SpeakerMatchingError("The speaker model has an unsupported input/output signature")
    return session


def source_duration(ffprobe: str, path: str) -> float | None:
    completed = _run_cancellable(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        "Inspecting voice clip source", lambda: False, timeout=60,
    )
    try:
        value = json.loads(completed.stdout)
        streams = value["streams"]
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            return None
        duration = float(value.get("format", {}).get("duration") or next(
            (stream.get("duration") for stream in streams if stream.get("duration")), 0,
        ))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("no finite source duration")
        return duration
    except (KeyError, TypeError, ValueError) as error:
        raise SpeakerMatchingError(f"Could not inspect voice clip duration: {error}") from error


def decode_clip(
    ffmpeg: str, clip: SpeakerClip, duration: float, destination: Path,
) -> Any:
    import soundfile as sf

    end = min(duration, duration if clip.end is None else clip.end)
    length = max(0.0, end - clip.start)
    if length < MIN_ACTIVE_SECONDS:
        return None
    # Center sampling bounds memory/inference even for hours-long source ranges.
    # This is deliberately not a claim that the rest of a clip contains one speaker.
    start = clip.start + max(0.0, (length - MAX_CLIP_SECONDS) / 2)
    length = min(length, MAX_CLIP_SECONDS)
    _run_cancellable(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-ss", f"{start:.9f}", "-i", clip.path, "-t", f"{length:.9f}",
         "-map", "0:a:0", "-vn", "-ar", str(SAMPLE_RATE), "-ac", "1",
         "-c:a", "pcm_f32le", str(destination)],
        "Decoding bounded voice clip", lambda: False, timeout=90,
    )
    with sf.SoundFile(destination) as audio:
        if audio.samplerate != SAMPLE_RATE or audio.channels != 1:
            raise SpeakerMatchingError("Decoded voice clip has an incorrect audio format")
        return audio.read(round(MAX_CLIP_SECONDS * SAMPLE_RATE), dtype="float32")


def embed_clips(
    emit: Callable[[str, dict], None], model: str, ffmpeg: str, ffprobe: str,
    job_path: str, requests: tuple[tuple[str, SpeakerClip], ...],
) -> dict[str, dict[str, Any]]:
    def progress(message: str, fraction: float | None) -> None:
        emit("progress", {"message": message, "fraction": fraction})

    session = None
    durations = {}
    records = {}
    decoded = Path(job_path) / "clip.wav"
    for index, (key, clip) in enumerate(requests):
        progress(f"Comparing voices locally: clip {index + 1} of {len(requests)}…",
                 index / len(requests))
        try:
            if clip.path not in durations:
                durations[clip.path] = source_duration(ffprobe, clip.path)
            duration = durations[clip.path]
            if duration is None:
                samples, reason = None, "no-audio"
            else:
                samples = decode_clip(ffmpeg, clip, duration, decoded)
                samples, reason = (None, "short") if samples is None else usable_audio(samples)
            embedding = None
            if samples is not None:
                if session is None:
                    progress("Loading the verified local speaker model…", None)
                    session = load_session(Path(model))
                features = fbank_features(samples)
                output = session.run(None, {session.get_inputs()[0].name: features})[0]
                embedding = normalized_embedding(output).tolist()
            records[key] = {"embedding": embedding, "reason": reason}
        finally:
            decoded.unlink(missing_ok=True)
    return records


def smoke_test(emit: Callable[[str, dict], None]) -> dict[str, Any]:
    import kaldi_native_fbank as knf
    import numpy as np
    import onnxruntime as ort
    import soundfile as sf

    time = np.arange(SAMPLE_RATE * 2, dtype=np.float32) / SAMPLE_RATE
    samples, reason = usable_audio((0.2 * np.sin(2 * np.pi * 220 * time)).astype(np.float32))
    if reason:
        raise SpeakerMatchingError("Synthetic speaker smoke audio was rejected")
    features = fbank_features(samples)
    if features.shape != (1, 198, 80) or np.max(np.abs(features.mean(axis=1))) > 1e-4:
        raise SpeakerMatchingError("Training-matched speaker filterbank smoke check failed")
    identity = bytes.fromhex(
        "08083a410a100a017812017922084964656e74697479120b6c6f63616c2d736d6f6b65"
        "5a0f0a0178120a0a08080112040a020802620f0a0179120a0a08080112040a0208024202100d"
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    runtime = ort.InferenceSession(
        identity, sess_options=options, providers=["CPUExecutionProvider"],
    )
    probe = np.array([0.25, -0.5], dtype=np.float32)
    if not np.array_equal(runtime.run(["y"], {"x": probe})[0], probe):
        raise SpeakerMatchingError("Speaker runtime smoke inference failed")
    return {
        "features": list(features.shape), "kaldi_native_fbank": knf.__version__,
        "numpy": np.__version__, "onnxruntime": ort.__version__, "soundfile": sf.__version__,
        "qt_imported": any(name.startswith("PySide6") for name in sys.modules),
    }


def smoke_main(report_path: Path) -> int:
    from choicer_voicer_pack_creator.process_worker import run_process_worker

    try:
        result = run_process_worker(
            smoke_test, (), on_event=lambda *_: True, cancelled=lambda: False, timeout=45,
        )
        report_path.write_text(json.dumps(result), encoding="utf-8")
        return 0
    except Exception as error:
        report_path.write_text(json.dumps({"error": str(error)}), encoding="utf-8")
        return 1
