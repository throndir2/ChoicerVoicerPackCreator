"""Offline, bounded Whisper inference. See resources/FasterWhisper-MIT.txt for provenance."""
from __future__ import annotations

import math
import os
import re
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from choicer_voicer_pack_creator.caption_timing_runtime import (
    CONTEXT_MARGIN_SECONDS,
    MAX_CONTENT_SECONDS,
    MAX_MODEL_SECONDS,
    MAX_TEXT_TOKENS,
    SAMPLE_RATE,
    CaptionTimingError,
    CaptionTimingManager,
    runtime_threads,
)
from choicer_voicer_pack_creator.caption_timing_types import (
    CaptionTimingEvidence,
    CaptionTimingResult,
    TimingWord,
)
from choicer_voicer_pack_creator.models import SourceCaption


@dataclass(frozen=True)
class _Cue:
    index: int
    start: float
    end: float
    tokens: tuple[int, ...]


@dataclass(frozen=True)
class _Window:
    start: float
    end: float
    cues: tuple[_Cue, ...]


def mel_filters() -> Any:
    import numpy as np

    mels = np.linspace(0, 45.245640471924965, 130)
    frequencies = (200.0 / 3) * mels
    logarithmic = mels >= 15
    frequencies[logarithmic] = 1000 * np.exp(
        (np.log(6.4) / 27) * (mels[logarithmic] - 15),
    )
    ramps = frequencies[:, None] - np.fft.rfftfreq(400, 1 / SAMPLE_RATE)[None, :]
    differences = np.diff(frequencies)
    filters = np.maximum(
        0, np.minimum(-ramps[:-2] / differences[:-1, None],
                      ramps[2:] / differences[1:, None]),
    )
    filters *= (2 / (frequencies[2:] - frequencies[:-2]))[:, None]
    return filters.astype(np.float32)


def log_mel_features(samples: Any, filters: Any = None) -> Any:
    """Training-matched 128-bin Slaney log-mel, padded to the 30-second encoder."""
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32)
    if (
        samples.ndim != 1 or not 0 < len(samples) <= round(MAX_MODEL_SECONDS * SAMPLE_RATE)
        or not np.isfinite(samples).all()
    ):
        raise CaptionTimingError("Caption timing needs a finite mono window of at most 30 seconds")
    if filters is None:
        filters = mel_filters()
    padded = np.pad(np.pad(samples, (0, 160)), (200, 200), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, 400)[::160]
    window = np.hanning(401)[:-1].astype(np.float32)
    spectrum = np.fft.rfft(frames * window, axis=-1).astype(np.complex64).T
    power = np.abs(spectrum[:, :-1]) ** 2
    features = np.log10(np.maximum(filters @ power, 1e-10))
    features = (np.maximum(features, features.max() - 8) + 4) / 4
    features = features[:, :3000]
    if features.shape[1] < 3000:
        features = np.pad(features, ((0, 0), (0, 3000 - features.shape[1])))
    return np.ascontiguousarray(features[None], dtype=np.float32)


class _Tokenizer:
    def __init__(self, path: Path, language: str) -> None:
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(path))
        self.eot = self._id("<|endoftext|>")
        self.timestamp_begin = self._id("<|0.00|>")
        self.start_sequence = [
            self._id("<|startoftranscript|>"), self._id(f"<|{language}|>"),
            self._id("<|transcribe|>"),
        ]
        self.no_space = language in {"zh", "ja", "th", "lo", "my", "yue"}

    def _id(self, text: str) -> int:
        result = self.tokenizer.token_to_id(text)
        if result is None:
            raise CaptionTimingError(f"Unsupported caption timing language/token: {text}")
        return result

    def encode(self, text: str) -> tuple[int, ...]:
        return tuple(self.tokenizer.encode(" " + text.strip(), add_special_tokens=False).ids)

    def split(self, tokens: Sequence[int]) -> list[tuple[str, int, int]]:
        """Decode complete Unicode points before grouping whitespace-delimited words."""
        decode = self.tokenizer.decode
        full = decode(list(tokens), skip_special_tokens=False)
        pieces = []
        begin = offset = 0
        for end in range(1, len(tokens) + 1):
            text = decode(list(tokens[begin:end]), skip_special_tokens=False)
            position = text.find("\ufffd")
            if position >= 0 and full[offset + position:offset + position + 1] != "\ufffd":
                continue
            pieces.append((text, begin, end))
            offset += len(text)
            begin = end
        if begin != len(tokens):
            raise CaptionTimingError("The model returned an incomplete Unicode word")
        words: list[tuple[str, int, int]] = []
        for text, begin, end in pieces:
            if (
                self.no_space or not words or text.startswith(" ")
                or text.strip() in string.punctuation
            ):
                words.append((text, begin, end))
            else:
                previous, start, _ = words[-1]
                words[-1] = (previous + text, start, end)
        return words


def plan_windows(
    captions: Sequence[SourceCaption], duration: float, tokenizer: _Tokenizer,
) -> tuple[tuple[_Window, ...], dict[int, CaptionTimingEvidence]]:
    from choicer_voicer_pack_creator.caption_timing import alignment_text

    cues = []
    problems = {}
    for index, caption in enumerate(captions):
        problem = ""
        if (
            not math.isfinite(caption.start) or not math.isfinite(caption.end)
            or caption.start < 0 or caption.end <= caption.start
            or caption.end > duration + 0.05
        ):
            problem = "Caption range is outside the decoded audio."
        elif caption.end - caption.start > MAX_CONTENT_SECONDS:
            problem = "Caption exceeds the 24-second complete-context limit."
        elif len(caption.text) > 8192:
            problem = "Caption exceeds the complete-text alignment limit."
        if problem:
            problems[index] = CaptionTimingEvidence(index, problem=problem)
            continue
        text = alignment_text(caption.text)
        tokens = tokenizer.encode(text) if text else ()
        if not tokens:
            problem = "Caption has no alignable spoken words."
        elif len(tokens) > MAX_TEXT_TOKENS:
            problem = "Caption exceeds the complete-text alignment token limit."
        elif any(token >= tokenizer.eot for token in tokens):
            problem = "Caption contains unsupported model control tokens."
        if problem:
            problems[index] = CaptionTimingEvidence(index, problem=problem)
        else:
            cues.append(_Cue(index, caption.start, min(duration, caption.end), tokens))
    cues.sort(key=lambda cue: (cue.start, cue.end, cue.index))
    windows = []
    group: list[_Cue] = []
    token_count = 0

    def finish() -> None:
        if group:
            windows.append(_Window(
                max(0, group[0].start - CONTEXT_MARGIN_SECONDS),
                min(duration, max(cue.end for cue in group) + CONTEXT_MARGIN_SECONDS),
                tuple(group),
            ))

    for cue in cues:
        if group and (
            max(cue.end, max(item.end for item in group)) - group[0].start > MAX_CONTENT_SECONDS
            or token_count + len(cue.tokens) > MAX_TEXT_TOKENS
            or cue.start - group[-1].end > 3
        ):
            finish()
            group = []
            token_count = 0
        group.append(cue)
        token_count += len(cue.tokens)
    finish()
    return tuple(windows), problems


def read_audio_window(audio: Any, start: float, end: float) -> tuple[Any, float]:
    import numpy as np

    first = max(0, math.floor(start * SAMPLE_RATE))
    last = min(len(audio), math.ceil(end * SAMPLE_RATE))
    if not 0 < last - first <= round(MAX_MODEL_SECONDS * SAMPLE_RATE):
        raise CaptionTimingError("Caption audio window is empty or exceeds the model limit")
    audio.seek(first)
    samples = audio.read(last - first, dtype="float32", always_2d=False)
    if len(samples) != last - first or samples.ndim != 1 or not np.isfinite(samples).all():
        raise CaptionTimingError("The caption audio window is incomplete or invalid")
    return samples, first / SAMPLE_RATE


def _token_boundaries(result: Any, count: int, seconds: float) -> tuple[list[float], list[float]]:
    times = []
    previous_text = previous_time = -1
    for text, frame in result.alignments:
        if text < previous_text or frame < previous_time or frame < 0:
            raise CaptionTimingError("The model returned nonmonotonic word alignment")
        if text != previous_text:
            if text != previous_text + 1:
                raise CaptionTimingError("The model omitted caption token boundaries")
            times.append(min(seconds, frame * 0.02))
        previous_text, previous_time = text, frame
    probabilities = list(result.text_token_probs)
    if len(times) != count + 1 or len(probabilities) != count:
        raise CaptionTimingError("The model returned incomplete caption word alignment")
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities):
        raise CaptionTimingError("The model returned invalid caption probabilities")
    return times, probabilities


def _words(
    tokenizer: _Tokenizer, tokens: Sequence[int], times: Sequence[float],
    probabilities: Sequence[float], offset: float, *, token_offset: int = 0,
) -> tuple[TimingWord, ...]:
    words: list[TimingWord] = []
    prefix = ""
    for text, begin, end in tokenizer.split(tokens):
        # Punctuation has no acoustic duration. Never append its DTW silence to a word.
        if not any(char.isalnum() for char in text):
            if words:
                words[-1] = replace(words[-1], text=words[-1].text + text.strip())
            else:
                prefix += text
            continue
        begin += token_offset
        end += token_offset
        start, stop = times[begin], times[end]
        probability = sum(probabilities[begin:end]) / (end - begin)
        if stop <= start:
            probability = min(probability, 0.2)
        words.append(TimingWord((prefix + text).strip(), offset + start, offset + stop, probability))
        prefix = ""
    return tuple(words)


def _timestamp_segments(
    tokens: Sequence[int], tokenizer: _Tokenizer, seconds: float,
) -> tuple[tuple[tuple[int, ...], float, float], ...]:
    start = None
    previous_timestamp = 0.0
    text: list[int] = []
    segments = []
    for token in tokens:
        if token >= tokenizer.timestamp_begin:
            timestamp = (token - tokenizer.timestamp_begin) * 0.02
            if timestamp > MAX_MODEL_SECONDS or timestamp < previous_timestamp:
                raise CaptionTimingError("Independent recognition returned invalid timestamps")
            previous_timestamp = timestamp
            if text:
                if start is None or timestamp <= start:
                    raise CaptionTimingError("Independent recognition has no complete time envelope")
                segments.append((tuple(text), min(start, seconds), min(timestamp, seconds)))
                text = []
            start = timestamp
        elif token < tokenizer.eot:
            text.append(token)
    if text:
        raise CaptionTimingError("Independent recognition ended with an incomplete caption")
    return tuple(segments)


def _recognized_words(
    model: Any, tokenizer: _Tokenizer, encoded: Any, frames: int, seconds: float, offset: float,
) -> tuple[TimingWord, ...]:
    generated = model.generate(
        encoded, [tokenizer.start_sequence], beam_size=5, max_length=448,
        return_scores=True, return_no_speech_prob=True,
        max_initial_timestamp_index=min(1500, max(50, frames // 2)),
        suppress_blank=True, suppress_tokens=[-1],
    )[0]
    if (
        not generated.sequences_ids or not generated.scores
        or not math.isfinite(generated.scores[0])
        or not math.isfinite(generated.no_speech_prob)
        or not 0 <= generated.no_speech_prob <= 1
    ):
        raise CaptionTimingError("Independent recognition returned invalid confidence scores")
    sequence = generated.sequences_ids[0]
    if len(sequence) >= 448 - len(tokenizer.start_sequence):
        raise CaptionTimingError("Independent recognition reached the complete-text token limit")
    segments = _timestamp_segments(sequence, tokenizer, seconds)
    tokens = tuple(token for segment, _, _ in segments for token in segment)
    if not tokens:
        return ()
    if len(tokens) > MAX_TEXT_TOKENS:
        raise CaptionTimingError("Independent recognition exceeds the alignment token limit")
    result = model.align(encoded, tokenizer.start_sequence, [list(tokens)], frames)[0]
    times, probabilities = _token_boundaries(result, len(tokens), seconds)
    uncertain = generated.no_speech_prob > 0.6 or generated.scores[0] < -1.0
    words = []
    token_offset = 0
    previous_end = offset
    for segment, start, end in segments:
        aligned = _words(
            tokenizer, segment, times, probabilities, offset, token_offset=token_offset,
        )
        for word in aligned:
            # Generated envelopes are approximate, not measured lexical boundaries.
            # A long DTW span may include silence, but neither its length nor the
            # envelope proves where speech starts. Flag uncertainty without clipping.
            probability = word.probability
            if (
                uncertain or word.start < previous_end or word.end <= word.start
                or word.end - word.start > 1.4
                or word.end <= offset + start or word.start >= offset + end
            ):
                probability = min(probability, 0.2)
            words.append(replace(word, probability=probability))
            previous_end = word.end
        token_offset += len(segment)
    return tuple(words)


def _detect_language(model: Any, encoded: Any) -> str:
    results = model.detect_language(encoded)
    if not results or len(results) != 1 or not results[0]:
        raise CaptionTimingError("Spoken language could not be detected; choose a language and retry")
    token, probability = results[0][0]
    match = re.fullmatch(r"<\|([a-z]{2,3})\|>", token) if isinstance(token, str) else None
    if (
        match is None or isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not math.isfinite(probability) or not 0.5 <= probability <= 1
    ):
        raise CaptionTimingError("Spoken language is uncertain; choose a language and retry")
    return match[1]


def align_captions(
    emit: Callable[[str, dict], None], wav_path: str, captions: tuple[SourceCaption, ...],
    duration: float, data_root: str, manifest_path: str, language: str, threads: int,
) -> tuple[CaptionTimingEvidence, ...]:
    return cast(tuple[CaptionTimingEvidence, ...], _run_caption_job(
        emit, wav_path, captions, duration, data_root, manifest_path, language, threads, audit=False,
    ))


def improve_captions(
    emit: Callable[[str, dict], None], wav_path: str, captions: tuple[SourceCaption, ...],
    duration: float, data_root: str, manifest_path: str, language: str, threads: int,
) -> CaptionTimingResult:
    return cast(CaptionTimingResult, _run_caption_job(
        emit, wav_path, captions, duration, data_root, manifest_path, language, threads, audit=True,
    ))


def _audit_proposed_cuts(
    progress: Callable[[str, float | None], None], audio: Any,
    originals: tuple[SourceCaption, ...], proposed: CaptionTimingResult,
    model: Any, tokenizer: _Tokenizer, filters: Any,
) -> CaptionTimingResult:
    import ctranslate2
    import numpy as np

    from choicer_voicer_pack_creator.caption_timing import audit_caption_text

    rows = list(proposed.captions)
    confidences = list(proposed.confidences)
    reasons = list(proposed.review_reasons)
    trusted = []

    def flag(index: int, reason: str) -> None:
        reasons[index] = reason
        confidences[index] = None
        rows[index] = replace(
            rows[index], source=f"{originals[index].source} - timing review needed: {reason}",
        )

    for index, confidence in enumerate(confidences):
        if reasons[index]:
            confidences[index] = None
            continue
        if confidence is None or not math.isfinite(confidence) or confidence < 0.65:
            flag(index, "Low-confidence alignment; proposed-cut audit was not run")
        else:
            trusted.append(index)
    for completed, index in enumerate(trusted):
        progress(
            f"Auditing proposed caption cuts: {completed + 1} of {len(trusted)}…",
            0.7 + 0.3 * completed / len(trusted),
        )
        cut = rows[index]
        try:
            if (
                not math.isfinite(cut.start) or not math.isfinite(cut.end)
                or not 0 <= cut.start < cut.end
                or cut.end > audio.frames / SAMPLE_RATE + 1e-7
            ):
                raise CaptionTimingError("Proposed caption cut is outside the decoded PCM audio")
            # These are the proposed cut boundaries, with no surrounding caption context.
            samples, offset = read_audio_window(audio, cut.start, cut.end)
            seconds = len(samples) / SAMPLE_RATE
            if float(np.max(np.abs(samples))) < 1e-5:
                recognized = ()
            else:
                features = log_mel_features(samples, filters)
                encoded = model.encode(ctranslate2.StorageView.from_array(features))
                try:
                    recognized = _recognized_words(
                        model, tokenizer, encoded,
                        min(3000, math.ceil(len(samples) / 160)), seconds, offset,
                    )
                finally:
                    del encoded, features
            del samples
            reason = audit_caption_text(cut.text, recognized)
        except CaptionTimingError as error:
            reason = str(error)
        if reason:
            flag(index, f"Proposed-cut audit: {reason}")
        else:
            confidences[index] = min(
                confidences[index], min(word.probability for word in recognized),
            )
            rows[index] = replace(
                cut, source=f"{cut.source} - proposed-cut wording corroborated locally",
            )
    return CaptionTimingResult(tuple(rows), tuple(confidences), tuple(reasons))


def _run_caption_job(
    emit: Callable[[str, dict], None], wav_path: str, captions: tuple[SourceCaption, ...],
    duration: float, data_root: str, manifest_path: str, language: str, threads: int, *, audit: bool,
) -> tuple[CaptionTimingEvidence, ...] | CaptionTimingResult:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["OMP_NUM_THREADS"] = str(max(1, min(4, threads)))
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    import ctranslate2
    import numpy as np
    import soundfile as sf

    def progress(message: str, fraction: float | None = None) -> None:
        emit("progress", {"message": message, "fraction": fraction})

    manager = CaptionTimingManager(Path(data_root), Path(manifest_path))
    progress("Verifying the local high-accuracy caption model…")
    if not manager.verify(lambda: False):
        raise CaptionTimingError("The caption timing model changed or needs permission to repair")
    detect_language = language == "auto"
    # Text tokenization is language-independent; choose the spoken language from audio
    # before generation/alignment, then reuse it for the job and proposed-cut audits.
    tokenizer = _Tokenizer(manager.model_path / "tokenizer.json", "en" if detect_language else language)
    with sf.SoundFile(wav_path) as audio:
        if (
            audio.samplerate != SAMPLE_RATE or audio.channels != 1
            or audio.format not in {"WAV", "WAVEX", "RF64"}
            or audio.subtype not in {"PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE"}
            or abs(len(audio) / SAMPLE_RATE - duration) > 0.1
        ):
            raise CaptionTimingError("Caption alignment requires complete 16 kHz mono PCM WAV audio")
        windows, evidence = plan_windows(captions, duration, tokenizer)
        if not windows:
            ordered = tuple(evidence[index] for index in range(len(captions)))
            if audit:
                from choicer_voicer_pack_creator.caption_timing import correct_caption_timings

                return correct_caption_timings(captions, ordered, duration)
            return ordered
        threads = min(max(1, threads), runtime_threads(), 4)
        progress(f"Loading {manager.model_name} locally ({threads} CPU threads)…")
        model = ctranslate2.models.Whisper(
            str(manager.model_path), device="cpu", compute_type="int8",
            inter_threads=1, intra_threads=threads,
        )
        filters = mel_filters()
        for number, window in enumerate(windows):
            fraction = (0.7 if audit else 1.0) * number / len(windows)
            progress(
                f"Aligning caption context {number + 1} of {len(windows)}…", fraction,
            )
            samples, offset = read_audio_window(audio, window.start, window.end)
            seconds = len(samples) / SAMPLE_RATE
            if float(np.max(np.abs(samples))) < 1e-5:
                for cue in window.cues:
                    evidence[cue.index] = CaptionTimingEvidence(
                        cue.index, problem="Caption context contains no audible signal.",
                    )
                continue
            features = log_mel_features(samples, filters)
            frames = min(3000, math.ceil(len(samples) / 160))
            encoded = model.encode(ctranslate2.StorageView.from_array(features))
            if detect_language:
                try:
                    language = _detect_language(model, encoded)
                    tokenizer = _Tokenizer(manager.model_path / "tokenizer.json", language)
                    detect_language = False
                    progress(f"Detected spoken language: {language}", fraction)
                except CaptionTimingError as error:
                    for cue in window.cues:
                        evidence[cue.index] = CaptionTimingEvidence(cue.index, problem=str(error))
                    del encoded, features, samples
                    continue
            tokens = tuple(token for cue in window.cues for token in cue.tokens)
            recognized = ()
            recognition_problem = ""
            try:
                progress(f"Checking independent speech in context {number + 1}…",
                         fraction)
                recognized = _recognized_words(model, tokenizer, encoded, frames, seconds, offset)
                if not recognized:
                    recognition_problem = "Independent recognition found no spoken words."
            except CaptionTimingError as error:
                recognition_problem = str(error)
            progress(f"Locating supplied caption words in context {number + 1}…",
                     fraction)
            try:
                result = model.align(encoded, tokenizer.start_sequence, [list(tokens)], frames)[0]
                times, probabilities = _token_boundaries(result, len(tokens), seconds)
                token_offset = 0
                for cue in window.cues:
                    words = _words(
                        tokenizer, cue.tokens, times, probabilities, offset,
                        token_offset=token_offset,
                    )
                    problem = recognition_problem
                    if not words or any(word.end <= word.start for word in words):
                        problem = "The model could not locate every complete caption word."
                    evidence[cue.index] = CaptionTimingEvidence(
                        cue.index, words, recognized, problem,
                    )
                    token_offset += len(cue.tokens)
            except CaptionTimingError as error:
                for cue in window.cues:
                    evidence[cue.index] = CaptionTimingEvidence(
                        cue.index, recognized_words=recognized, problem=str(error),
                    )
            del encoded, features, samples
        if audit:
            from choicer_voicer_pack_creator.caption_timing import correct_caption_timings

            progress("Preparing proposed caption cuts for focused review…", 0.7)
            proposed = correct_caption_timings(
                captions, tuple(evidence[index] for index in range(len(captions))), duration,
            )
            result = _audit_proposed_cuts(
                progress, audio, captions, proposed, model, tokenizer, filters,
            )
            progress("Local caption alignment and proposed-cut audit finished.", 1.0)
            return result
    progress("Local caption word alignment finished.", 1.0)
    return tuple(evidence[index] for index in range(len(captions)))


def smoke_test(emit: Callable[[str, dict], None]) -> dict[str, Any]:
    import sys

    if any(name == "torch" or name.startswith("torch.") for name in sys.modules):
        raise CaptionTimingError("Caption timing worker unexpectedly imported Torch")

    import ctranslate2
    import numpy as np
    import tokenizers

    features = log_mel_features(np.zeros(SAMPLE_RATE, dtype=np.float32))
    storage = ctranslate2.StorageView.from_array(features)
    if tuple(storage.shape) != (1, 128, 3000) or "int8" not in (
        ctranslate2.get_supported_compute_types("cpu")
    ):
        raise CaptionTimingError("The bundled caption timing runtime failed its offline smoke check")
    torch_imported = any(name == "torch" or name.startswith("torch.") for name in sys.modules)
    if torch_imported:
        raise CaptionTimingError("Caption timing worker unexpectedly imported Torch")
    return {
        "ctranslate2": ctranslate2.__version__, "tokenizers": tokenizers.__version__,
        "torch_imported": torch_imported,
        "features": list(storage.shape), "qt_imported": any(
            name.startswith("PySide6") for name in sys.modules
        ),
    }


def smoke_main(report_path: Path) -> int:
    import json

    from choicer_voicer_pack_creator.process_worker import run_process_worker

    try:
        result = run_process_worker(
            smoke_test, (), on_event=lambda *_: True, cancelled=lambda: False, timeout=30,
        )
        if result["qt_imported"]:
            raise CaptionTimingError("Caption timing worker unexpectedly imported Qt")
        if result.get("torch_imported") is not False:
            raise CaptionTimingError("Caption timing worker failed to verify Torch is absent")
        report_path.write_text(json.dumps(result), encoding="utf-8")
        return 0
    except Exception as error:
        report_path.write_text(json.dumps({"error": str(error)}), encoding="utf-8")
        return 1
