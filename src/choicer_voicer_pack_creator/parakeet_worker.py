"""NVIDIA Parakeet TDT inference through sherpa-onnx, in an isolated worker without Qt.

sherpa-onnx ships its own ONNX Runtime DLL, so it must never share a process with the
``onnxruntime`` package used by the separation and speaker workers.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320  # 20 ms energy frames choose the chunk cuts.
MAX_CHUNK_SECONDS = 30.0
MIN_CHUNK_SECONDS = 15.0
PAUSE_SECONDS = 0.7
MAX_LINE_CHARACTERS = 120
SENTENCE_END = (".", "?", "!", "\u2026", "\u3002", "\uff1f", "\uff01")
_CLOSING = "\"')]}\u00bb\u201d\u2019"


class ParakeetWorkerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TimedToken:
    text: str
    start: float
    end: float
    probability: float | None


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    start: float
    end: float
    text: str
    confidence: float | None


def chunk_bounds(levels: Sequence[float], total_samples: int) -> list[tuple[int, int]]:
    """Cut audio into chunks of at most 30 s at the quietest 200 ms after 15 s."""
    import numpy as np

    count = len(levels)
    maximum = round(MAX_CHUNK_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
    minimum = round(MIN_CHUNK_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
    values = np.asarray(levels, dtype=np.float64)
    bounds = []
    start = 0
    while count - start > maximum:
        window = values[start + minimum:start + maximum]
        smoothed = np.convolve(window, np.ones(10) / 10, mode="same")
        cut = start + minimum + int(np.argmin(smoothed))
        bounds.append((start, cut))
        start = cut
    bounds.append((start, count))
    return [
        (first * FRAME_SAMPLES, min(total_samples, last * FRAME_SAMPLES))
        for first, last in bounds if first * FRAME_SAMPLES < total_samples
    ]


def _ends_sentence(text: str) -> bool:
    return text.rstrip(_CLOSING).endswith(SENTENCE_END)


def _line(words: Sequence[tuple[str, float, float, list[float]]]) -> TranscriptLine | None:
    text = " ".join(word[0] for word in words if word[0]).strip()
    if not text:
        return None
    probabilities = [value for word in words for value in word[3]]
    return TranscriptLine(
        words[0][1], max(words[0][1], words[-1][2]), text,
        round(sum(probabilities) / len(probabilities), 3) if probabilities else None,
    )


def build_lines(chunks: Sequence[Sequence[TimedToken]]) -> list[TranscriptLine]:
    """Group word-timed tokens into caption lines at sentence ends, pauses and 120 characters."""
    lines: list[TranscriptLine] = []
    for tokens in chunks:
        words: list[tuple[str, float, float, list[float]]] = []
        for token in tokens:
            probabilities = [] if token.probability is None else [token.probability]
            if not words or token.text.startswith(" "):
                words.append((token.text.strip(), token.start, token.end, probabilities))
            else:
                text, start, end, previous = words[-1]
                words[-1] = (text + token.text, start, max(end, token.end), previous + probabilities)
        groups: list[list[tuple[str, float, float, list[float]]]] = [[]]
        for word in words:
            current = groups[-1]
            if current and (
                word[1] - current[-1][2] >= PAUSE_SECONDS
                or _ends_sentence(current[-1][0])
                or sum(len(item[0]) + 1 for item in current) + len(word[0]) > MAX_LINE_CHARACTERS
            ):
                groups.append([])
            groups[-1].append(word)
        lines.extend(line for group in groups if group and (line := _line(group)) is not None)
    return lines


def _tokens(result: Any, offset: float, seconds: float) -> list[TimedToken]:
    texts = list(result.tokens)
    starts = list(result.timestamps)
    durations = list(result.durations)
    log_probabilities = list(result.ys_log_probs)
    tokens = []
    previous = 0.0
    for index, text in enumerate(texts):
        start = float(starts[index]) if index < len(starts) else previous
        duration = float(durations[index]) if index < len(durations) else 0.0
        if not math.isfinite(start) or not math.isfinite(duration):
            raise ParakeetWorkerError("Parakeet returned a non-finite token time")
        start = min(seconds, max(previous, start))
        end = min(seconds, max(start, start + duration))
        probability = None
        if index < len(log_probabilities) and math.isfinite(log_probabilities[index]):
            probability = min(1.0, math.exp(float(log_probabilities[index])))
        tokens.append(TimedToken(str(text), offset + start, offset + end, probability))
        previous = start
    return tokens


def transcribe_lines(
    emit: Callable[[str, dict], None], wav_path: str, model_dir: str, threads: int,
) -> list[TranscriptLine]:
    import os
    import sys
    from pathlib import Path

    import numpy as np
    import soundfile as sf

    if "onnxruntime" in sys.modules:
        raise ParakeetWorkerError("The Parakeet worker must not share a process with ONNX Runtime")
    os.environ["OMP_NUM_THREADS"] = str(max(1, threads))
    import sherpa_onnx

    def progress(message: str, fraction: float | None = None) -> None:
        emit("progress", {"message": message, "fraction": fraction})

    model = Path(model_dir)
    with sf.SoundFile(wav_path) as audio:
        if audio.samplerate != SAMPLE_RATE or audio.channels != 1:
            raise ParakeetWorkerError("Parakeet requires 16 kHz mono analysis audio")
        total = audio.frames
        if total <= 0:
            return []
        progress("Finding quiet points to split the audio for Parakeet…", None)
        levels: list[float] = []
        for block in audio.blocks(
            blocksize=FRAME_SAMPLES * 500, dtype="float32", always_2d=False,
        ):
            whole = len(block) // FRAME_SAMPLES * FRAME_SAMPLES
            if whole:
                frames = block[:whole].reshape(-1, FRAME_SAMPLES)
                levels.extend(np.sqrt(np.mean(frames * frames, axis=1)).tolist())
            if whole < len(block):
                rest = block[whole:]
                levels.append(float(np.sqrt(np.mean(rest * rest))))
        bounds = chunk_bounds(levels, total)
        progress(f"Loading Parakeet TDT 0.6B v3 on CPU ({threads} threads)…", None)
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model / "encoder.int8.onnx"),
            decoder=str(model / "decoder.int8.onnx"),
            joiner=str(model / "joiner.int8.onnx"),
            tokens=str(model / "tokens.txt"),
            num_threads=max(1, threads), sample_rate=SAMPLE_RATE, feature_dim=80,
            decoding_method="greedy_search", model_type="nemo_transducer",
        )
        chunks = []
        for number, (first, last) in enumerate(bounds):
            progress(
                f"Parakeet transcription: {round(100 * first / total)}% of audio processed "
                f"(chunk {number + 1} of {len(bounds)}).",
                first / total,
            )
            audio.seek(first)
            samples = audio.read(last - first, dtype="float32", always_2d=False)
            if len(samples) != last - first or not np.isfinite(samples).all():
                raise ParakeetWorkerError("The analysis audio chunk is incomplete or invalid")
            if not len(samples) or float(np.max(np.abs(samples))) < 1e-4:
                continue
            stream = recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            recognizer.decode_stream(stream)
            chunks.append(_tokens(stream.result, first / SAMPLE_RATE, len(samples) / SAMPLE_RATE))
            del stream, samples
    progress("Parakeet transcription: 100% of audio processed.", 1.0)
    return build_lines(chunks)


def smoke_test(emit: Callable[[str, dict], None]) -> dict[str, Any]:
    import sys

    if "onnxruntime" in sys.modules:
        raise ParakeetWorkerError("The Parakeet worker must not share a process with ONNX Runtime")
    import sherpa_onnx

    sherpa_onnx.OfflineRecognizerConfig()  # Loads the native module and its ONNX Runtime DLL.
    return {
        "sherpa_onnx": sherpa_onnx.__version__,
        "onnxruntime_imported": "onnxruntime" in sys.modules,
        "qt_imported": any(name.startswith("PySide6") for name in sys.modules),
    }


def smoke_main(report_path: Any) -> int:
    import json

    from choicer_voicer_pack_creator.process_worker import run_process_worker

    try:
        result = run_process_worker(
            smoke_test, (), on_event=lambda *_: True, cancelled=lambda: False, timeout=60,
        )
        report_path.write_text(json.dumps(result), encoding="utf-8")
        return 0
    except Exception as error:
        report_path.write_text(json.dumps({"error": str(error)}), encoding="utf-8")
        return 1
