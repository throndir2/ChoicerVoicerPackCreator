from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from choicer_voicer_pack_creator import analysis, caption_timing
from choicer_voicer_pack_creator import caption_timing_runtime as runtime
from choicer_voicer_pack_creator import caption_timing_worker as worker
from choicer_voicer_pack_creator.caption_timing_types import (
    CaptionTimingEvidence,
    CaptionTimingResult,
    TimingWord,
)
from choicer_voicer_pack_creator.models import SourceCaption
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.process_worker import ProcessWorkerError


@pytest.fixture
def local_model(tmp_path):
    manifest = json.loads(runtime.default_manifest_path().read_text())
    payloads = {}
    for item in manifest["files"]:
        payload = f"verified {item['filename']}".encode()
        payloads[item["filename"]] = payload
        item["bytes"] = len(payload)
        item["sha256"] = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "caption-timing.json"
    path.write_text(json.dumps(manifest))
    manager = runtime.CaptionTimingManager(tmp_path / "local-data", path)
    return manager, payloads


def _install(manager, payloads):
    manager.model_path.mkdir(parents=True)
    for name, payload in payloads.items():
        (manager.model_path / name).write_bytes(payload)


def test_manifest_has_immutable_complete_inventory():
    manager = runtime.CaptionTimingManager(Path("unused"))
    assert manager.model_name == "Whisper large-v3-turbo"
    assert manager.download_bytes == 1621665643
    assert manager.component_key.endswith(manager.manifest["revision"])
    assert {item["filename"] for item in manager.manifest["files"]} == runtime.MODEL_FILES
    assert not manager.installed


@pytest.mark.parametrize("damage", ["revision", "url", "hash", "size", "filename", "duplicate", "mel"])
def test_invalid_manifest_fails_closed(tmp_path, damage):
    manifest = json.loads(runtime.default_manifest_path().read_text())
    if damage == "revision":
        manifest["revision"] = "main"
    elif damage == "url":
        manifest["files"][0]["url"] = "https://example.com/config.json"
    elif damage == "hash":
        manifest["files"][0]["sha256"] = "unchecked"
    elif damage == "size":
        manifest["files"][0]["bytes"] = True
    elif damage == "filename":
        manifest["files"][0]["filename"] = "..\\model.bin"
    elif damage == "duplicate":
        manifest["files"][0] = manifest["files"][1]
    else:
        manifest["preprocessing"]["mel_bins"] = 80
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(runtime.CaptionTimingError, match="manifest is invalid"):
        runtime.CaptionTimingManager(tmp_path, path)


def test_missing_or_damaged_model_never_downloads_without_consent(local_model, monkeypatch):
    manager, payloads = local_model
    monkeypatch.setattr(analysis, "download_verified", lambda *_: pytest.fail("network attempted"))
    with pytest.raises(runtime.CaptionTimingError, match="needs permission"):
        manager.ensure_model(lambda *_: None, lambda: False)
    with pytest.raises(runtime.CaptionTimingError, match="needs permission"):
        manager.ensure_model(lambda *_: None, lambda: False, allow_download="yes")
    _install(manager, payloads)
    path = manager.model_path / "model.bin"
    path.write_bytes(b"x" * path.stat().st_size)
    assert manager.installed  # Cheap UI hint is deliberately not an integrity claim.
    with pytest.raises(runtime.CaptionTimingError, match="needs permission"):
        manager.ensure_model(lambda *_: None, lambda: False)
    assert path.read_bytes().startswith(b"x")


def test_model_install_repair_offline_and_notices(local_model, monkeypatch):
    manager, payloads = local_model
    downloads = []

    def download(url, destination, digest, size, _label, _progress, _cancelled):
        data = payloads[destination.name]
        assert size == len(data) and hashlib.sha256(data).hexdigest() == digest
        assert manager.manifest["revision"] in url
        destination.write_bytes(data)
        downloads.append(destination.name)
        return destination

    monkeypatch.setattr(analysis, "download_verified", download)
    assert manager.ensure_model(lambda *_: None, lambda: False, allow_download=True) == (
        manager.model_path
    )
    assert set(downloads) == runtime.MODEL_FILES and manager.installed
    assert manager.verify(lambda: False)
    assert all((manager.model_path / name).is_file() for name in runtime.MODEL_NOTICES)
    assert not list((manager.data_root / "caption-timing-downloads").iterdir())
    downloads.clear()
    manager.ensure_model(lambda *_: None, lambda: False)
    assert not downloads
    (manager.model_path / "tokenizer.json").write_bytes(b"bad")
    manager.ensure_model(lambda *_: None, lambda: False, allow_download=True)
    assert downloads == ["tokenizer.json"]


def test_download_is_reverified_before_publication(local_model, monkeypatch):
    manager, _ = local_model

    def unverified(_url, destination, *_args):
        destination.write_bytes(b"corrupt")
        return destination

    monkeypatch.setattr(analysis, "download_verified", unverified)
    with pytest.raises(runtime.CaptionTimingError, match="Downloaded"):
        manager.ensure_model(lambda *_: None, lambda: False, allow_download=True)
    assert not manager.model_path.exists()
    assert not list((manager.data_root / "caption-timing-downloads").iterdir())


def test_cancel_during_model_verification(local_model):
    manager, payloads = local_model
    _install(manager, payloads)
    with pytest.raises(OperationCancelled):
        manager.ensure_model(lambda *_: None, lambda: True, allow_download=True)
    assert manager.installed


def test_symlink_model_directory_is_not_available(local_model, monkeypatch):
    manager, payloads = local_model
    _install(manager, payloads)
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == manager.model_path or real(path))
    assert not manager.installed
    with pytest.raises(runtime.CaptionTimingError, match="must not be a link"):
        manager.ensure_model(lambda *_: None, lambda: False, allow_download=True)


@pytest.mark.parametrize("available,physical,expected", [
    (2 * 1024**3, 16 * 1024**3, None),
    (4 * 1024**3, 32 * 1024**3, None),
    (8 * 1024**3, 16 * 1024**3, 4),
    (None, None, 1),
])
def test_memory_headroom_and_threads(monkeypatch, available, physical, expected):
    monkeypatch.setattr(analysis, "detect_hardware", lambda: SimpleNamespace(
        available_memory_bytes=available, memory_bytes=physical, cpu_threads=32,
    ))
    if expected is None:
        with pytest.raises(runtime.CaptionTimingError, match="reserved for the editor"):
            runtime.runtime_threads()
    else:
        assert runtime.runtime_threads() == expected


def test_alignment_uses_owned_worker_and_read_only_verified_model(local_model, monkeypatch):
    manager, payloads = local_model
    _install(manager, payloads)
    captions = [SourceCaption(0, 1, "Hello", "test")]
    records = (CaptionTimingEvidence(0, problem="test evidence"),)
    monkeypatch.setattr(runtime, "runtime_threads", lambda: 2)

    def run(target, args, *, on_event, cancelled, timeout, idle_timeout):
        assert target is worker.align_captions
        assert args[1] == tuple(captions) and args[-1] == 2
        assert not cancelled() and timeout > idle_timeout > 0
        assert on_event("progress", {"message": "actual work", "fraction": 0.5})
        assert not on_event("heartbeat", {})
        return records

    monkeypatch.setattr(runtime, "run_process_worker", run)
    assert runtime.align_caption_audio(
        Path("unused.wav"), captions, 1, manager.data_root, manifest_path=manager.manifest_path,
    ) == records


def test_improvement_runs_alignment_and_cut_review_in_one_owned_worker(local_model, monkeypatch):
    manager, payloads = local_model
    _install(manager, payloads)
    captions = (SourceCaption(0, 1, "Hello", "test"),)
    expected = CaptionTimingResult(captions, (None,), ("review",))
    monkeypatch.setattr(runtime, "runtime_threads", lambda: 2)
    workers = []

    def run(target, args, *, on_event, cancelled, timeout, idle_timeout):
        workers.append(target)
        assert args[1] == captions
        assert timeout > idle_timeout and not cancelled()
        assert on_event("progress", {"message": "Auditing proposed cuts", "fraction": 0.8})
        return expected

    monkeypatch.setattr(runtime, "run_process_worker", run)
    result = runtime.improve_caption_audio(
        Path("unused.wav"), captions, 1, manager.data_root, manifest_path=manager.manifest_path,
    )
    assert result == expected and workers == [worker.improve_captions]


def test_empty_improvement_does_not_load_or_download_model(monkeypatch):
    monkeypatch.setattr(runtime, "runtime_threads", lambda: pytest.fail("model admission attempted"))
    assert runtime.improve_caption_audio(Path("unused.wav"), (), 1, Path("unused")) == (
        CaptionTimingResult((), (), ())
    )


def test_improvement_rejects_rewritten_caption_text(local_model, monkeypatch):
    manager, payloads = local_model
    _install(manager, payloads)
    caption = SourceCaption(0, 1, "Visible original", "test")
    monkeypatch.setattr(runtime, "runtime_threads", lambda: 1)
    monkeypatch.setattr(runtime, "run_process_worker", lambda *_args, **_kwargs: CaptionTimingResult(
        (replace(caption, text="unwanted correction"),), (0.9,), ("",),
    ))
    with pytest.raises(runtime.CaptionTimingError, match="incomplete cut review"):
        runtime.improve_caption_audio(
            Path("unused.wav"), (caption,), 1, manager.data_root,
            manifest_path=manager.manifest_path,
        )


@pytest.mark.parametrize("error", [
    OperationCancelled("stop"),
    ProcessWorkerError("native failure", error_type="NativeError", remote_traceback="trace"),
])
def test_worker_failure_and_cancellation_are_preserved(local_model, monkeypatch, error):
    manager, payloads = local_model
    _install(manager, payloads)
    monkeypatch.setattr(runtime, "runtime_threads", lambda: 1)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(runtime, "run_process_worker", fail)
    expected = OperationCancelled if isinstance(error, OperationCancelled) else runtime.CaptionTimingError
    with pytest.raises(expected, match="stop|native failure"):
        runtime.align_caption_audio(
            Path("unused.wav"), [SourceCaption(0, 1, "Hello", "test")], 1,
            manager.data_root, manifest_path=manager.manifest_path,
        )


class _WordsTokenizer:
    eot = 10000
    timestamp_begin = 20000
    start_sequence = [10001, 10002, 10003]

    def __init__(self):
        self.vocabulary = {}

    def encode(self, text):
        result = []
        for word in text.split():
            token = len(self.vocabulary) + 1
            self.vocabulary[token] = word
            result.append(token)
        return tuple(result)

    def split(self, tokens):
        return [(self.vocabulary[token], index, index + 1) for index, token in enumerate(tokens)]


def test_windows_cover_entire_cues_without_next_caption_token_truncation():
    tokenizer = _WordsTokenizer()
    captions = [
        SourceCaption(2, 12, "first full caption", "test"),
        SourceCaption(12, 25, "second full caption", "test"),
        SourceCaption(25, 28, "never partially include this caption", "test"),
        SourceCaption(28, 53, "too long", "test"),
    ]
    windows, problems = worker.plan_windows(captions, 60, tokenizer)
    assert len(windows) == 2
    assert [cue.index for window in windows for cue in window.cues] == [0, 1, 2]
    assert [cue.index for cue in windows[0].cues] == [0, 1]
    assert "24-second" in problems[3].problem
    for window in windows:
        assert window.end - window.start <= 28
        for cue in window.cues:
            assert window.start <= cue.start < cue.end <= window.end
            assert len(cue.tokens) == len(captions[cue.index].text.split())


def test_dense_and_nonfinite_captions_have_explicit_problems():
    captions = [
        SourceCaption(0, 1, "word " * 400, "test"),
        SourceCaption(1, 2, "good text", "test"),
        SourceCaption(float("nan"), 3, "invalid", "test"),
        SourceCaption(20, 21, "outside", "test"),
    ]
    windows, problems = worker.plan_windows(captions, 10, _WordsTokenizer())
    assert [cue.index for window in windows for cue in window.cues] == [1]
    assert set(problems) == {0, 2, 3}
    assert "token limit" in problems[0].problem


def test_mel_features_match_upstream_numerical_reference():
    samples = (0.2 * np.sin(2 * np.pi * 440 * np.arange(19213) / 16000)).astype(np.float32)
    features = worker.log_mel_features(samples)
    # faster-whisper 1.2.1 FeatureExtractor(128), then pad_or_trim(..., 3000).
    reference = [0.7085496187, 0.2301185727, -0.7136158943, -0.7136158943, 0.0580182076]
    assert features.shape == (1, 128, 3000)
    assert features.dtype == np.float32 and features.flags.c_contiguous
    np.testing.assert_allclose(
        features[0, [0, 10, 30, 60, 127], [0, 1, 50, 100, 120]], reference, atol=2e-6,
    )
    np.testing.assert_array_equal(features[:, :, 121:], 0)


@pytest.mark.parametrize("samples", [
    np.zeros(480001, dtype=np.float32), np.zeros((10, 2)), np.array([np.nan]), np.array([]),
])
def test_mel_features_reject_unbounded_or_invalid_audio(samples):
    with pytest.raises(runtime.CaptionTimingError):
        worker.log_mel_features(samples)


def test_pcm_audio_windows_are_disk_bounded(tmp_path):
    path = tmp_path / "audio.wav"
    sf.write(path, np.arange(160000, dtype=np.float32) / 160000, 16000, subtype="FLOAT")
    with sf.SoundFile(path) as audio:
        samples, offset = worker.read_audio_window(audio, 8, 9)
    assert offset == 8 and len(samples) == 16000
    assert samples[0] == pytest.approx(0.8)


def test_dtw_coverage_and_low_confidence_are_retained():
    result = SimpleNamespace(
        alignments=[(0, 0), (0, 100), (1, 101), (2, 120)],
        text_token_probs=[0.99, 0.15],
    )
    times, probabilities = worker._token_boundaries(result, 2, 10)
    tokenizer = _WordsTokenizer()
    tokens = tokenizer.encode("hello there")
    words = worker._words(tokenizer, tokens, times, probabilities, 50)
    assert words[0].start == 50 and words[0].end == 52.02
    assert words[0].probability == 0.99
    assert words[1].probability == 0.15
    result.alignments = [(0, 0), (2, 100)]
    with pytest.raises(runtime.CaptionTimingError, match="omitted"):
        worker._token_boundaries(result, 2, 10)


def test_timestamp_envelopes_reject_unterminated_or_backwards_text():
    tokenizer = _WordsTokenizer()
    tokens = tokenizer.encode("hello there")
    assert worker._timestamp_segments([20100, *tokens, 20150, 10000], tokenizer, 5) == (
        (tokens, 2, 3),
    )
    with pytest.raises(runtime.CaptionTimingError, match="incomplete"):
        worker._timestamp_segments([20000, *tokens], tokenizer, 5)
    with pytest.raises(runtime.CaptionTimingError, match="invalid timestamps"):
        worker._timestamp_segments([20100, *tokens, 20050], tokenizer, 5)


def _recognition_model(tokenizer, segments, boundaries):
    sequence = []
    token_count = 0
    for text, start, end in segments:
        tokens = tokenizer.encode(text)
        token_count += len(tokens)
        sequence.extend([
            tokenizer.timestamp_begin + round(start * 50), *tokens,
            tokenizer.timestamp_begin + round(end * 50),
        ])
    assert len(boundaries) == token_count + 1
    generated = SimpleNamespace(
        sequences_ids=[[*sequence, tokenizer.eot]], no_speech_prob=0.01, scores=[-0.1],
    )
    alignment = SimpleNamespace(
        alignments=[(index, round(time * 50)) for index, time in enumerate(boundaries)],
        text_token_probs=[0.9] * token_count,
    )
    return SimpleNamespace(
        encode=lambda _features: object(),
        generate=lambda *_args, **_kwargs: [generated],
        align=lambda *_args: [alignment],
    )


@pytest.mark.parametrize("envelope", [(1, 2.2), (0.8, 2), (1, 2)])
def test_normal_recognized_words_keep_support_outside_approximate_envelope(envelope):
    tokenizer = _WordsTokenizer()
    model = _recognition_model(tokenizer, [("Hello world", *envelope)], (0.9, 1.1, 2.1))
    words = worker._recognized_words(model, tokenizer, object(), 300, 3, 100)
    assert [(word.start, word.end) for word in words] == [
        pytest.approx((100.9, 101.1)), pytest.approx((101.1, 102.1)),
    ]
    assert [word.probability for word in words] == [0.9, 0.9]


@pytest.mark.parametrize("boundaries,probabilities", [
    ((0.5, 0.9, 1.3), (0.2, 0.9)),
    ((1.7, 2.1, 2.5), (0.9, 0.2)),
    ((0.1, 0.3, 0.5), (0.2, 0.2)),
])
def test_disjoint_envelopes_lower_confidence_without_moving_words(boundaries, probabilities):
    tokenizer = _WordsTokenizer()
    model = _recognition_model(tokenizer, [("Hello world", 1, 2)], boundaries)
    words = worker._recognized_words(model, tokenizer, object(), 300, 3, 0)
    assert [(word.start, word.end) for word in words] == [
        pytest.approx(pair) for pair in pairwise(boundaries)
    ]
    assert tuple(word.probability for word in words) == probabilities
    assert "Low-confidence" in caption_timing.audit_caption_text("Hello world", words)


def test_long_first_word_is_reviewed_without_inventing_onset_or_punctuation_duration():
    tokenizer = _WordsTokenizer()
    model = _recognition_model(
        tokenizer, [("Hello . there", 2, 4)], (0, 2.4, 3.6, 4),
    )
    words = worker._recognized_words(model, tokenizer, object(), 500, 5, 100)
    assert words[0].text == "Hello."
    assert words[0].start == 100 and words[0].end == 102.4
    assert words[0].probability == 0.2
    assert words[1].start == 103.6 and words[1].end == 104
    assert words[1].probability == 0.9
    assert "Low-confidence" in caption_timing.audit_caption_text("Hello there", words)


def test_uncertain_long_onset_keeps_independent_supported_closing_correction():
    tokenizer = _WordsTokenizer()
    model = _recognition_model(
        tokenizer, [("Hello friendly wide world", 2, 3.3)], (0, 2.4, 2.7, 3, 3.3),
    )
    words = worker._recognized_words(model, tokenizer, object(), 500, 5, 0)
    cue = SourceCaption(0, 3.8, "Hello friendly wide world", "test")
    forced = tuple(replace(word, probability=0.9) for word in words)
    proposed = caption_timing.correct_caption_timings(
        (cue,), (CaptionTimingEvidence(0, forced, words),), 5,
    )
    assert proposed.captions[0].start == cue.start
    assert proposed.captions[0].end == pytest.approx(3.55)
    assert "Opening unchanged" in proposed.review_reasons[0]
    assert "Closing unchanged" not in proposed.review_reasons[0]
    reviewed = worker._audit_proposed_cuts(
        lambda *_: None, object(), (cue,), proposed, model, tokenizer, object(),
    )
    assert reviewed.captions == proposed.captions
    assert reviewed.confidences == (None,)
    assert reviewed.review_reasons == proposed.review_reasons


def test_contiguous_caption_edges_preserve_words_without_relying_on_padding():
    tokenizer = _WordsTokenizer()
    model = _recognition_model(
        tokenizer,
        [("Before", 0.4, 0.9), ("Hello world", 1, 2), ("After", 2.1, 2.4)],
        (0.4, 0.9, 1.1, 2.1, 2.4),
    )
    words = worker._recognized_words(model, tokenizer, object(), 300, 3, 0)
    forced = (
        (TimingWord("Before", 0.4, 0.9, 0.9),),
        (TimingWord("Hello", 0.9, 1.1, 0.9), TimingWord("world", 1.1, 2.1, 0.9)),
        (TimingWord("After", 2.1, 2.4, 0.9),),
    )
    cues = (
        SourceCaption(0.3, 1, "Before", "test"),
        SourceCaption(1, 2, "Hello world", "test"),
        SourceCaption(2, 2.5, "After", "test"),
    )
    result = caption_timing.correct_caption_timings(
        cues, tuple(CaptionTimingEvidence(i, span, words) for i, span in enumerate(forced)), 3,
    )
    assert result.review_reasons == ("", "", "")
    assert result.captions[0].end == result.captions[1].start == pytest.approx(0.9)
    assert result.captions[1].end == result.captions[2].start == pytest.approx(2.1)
    assert [cue.text for cue in result.captions] == [cue.text for cue in cues]


@pytest.mark.parametrize("boundaries", [(-0.1, 0.2, 0.8), (0.4, 0.2, 0.8)])
def test_invalid_recognized_alignment_is_rejected_not_clamped(boundaries):
    tokenizer = _WordsTokenizer()
    model = _recognition_model(tokenizer, [("Hello world", 0, 1)], boundaries)
    with pytest.raises(runtime.CaptionTimingError, match="nonmonotonic"):
        worker._recognized_words(model, tokenizer, object(), 100, 1, 0)


def test_zero_length_recognized_word_remains_invalid_not_repaired():
    tokenizer = _WordsTokenizer()
    model = _recognition_model(tokenizer, [("Hello world", 0, 1)], (0.4, 0.4, 0.8))
    words = worker._recognized_words(model, tokenizer, object(), 100, 1, 0)
    assert words[0].start == words[0].end == 0.4
    assert words[0].probability == 0.2
    assert "Invalid word timestamps" in caption_timing.audit_caption_text("Hello world", words)


@pytest.mark.parametrize("envelope,boundaries,review", [
    ((0.2, 1.2), (0.1, 0.3, 1.3), False),
    ((2, 2.8), (0, 2.4, 2.8), True),
    ((1, 2), (0.1, 0.3, 0.5), True),
])
def test_post_cut_audit_uses_unclipped_lexical_support(
    tmp_path, monkeypatch, envelope, boundaries, review,
):
    tokenizer = _WordsTokenizer()
    model = _recognition_model(tokenizer, [("Hello world", *envelope)], boundaries)
    path = tmp_path / "audit.wav"
    sf.write(path, np.full(16000 * 4, 0.1, dtype=np.float32), 16000, subtype="PCM_16")
    cue = SourceCaption(0.5, 3.5, "Hello world", "test")
    proposed = CaptionTimingResult((cue,), (0.9,), ("",))
    audited_words = []
    actual_audit = caption_timing.audit_caption_text

    def audit(text, words):
        audited_words.extend(words)
        return actual_audit(text, words)

    monkeypatch.setattr(caption_timing, "audit_caption_text", audit)
    with sf.SoundFile(path) as audio:
        result = worker._audit_proposed_cuts(
            lambda *_: None, audio, (cue,), proposed, model, tokenizer, worker.mel_filters(),
        )
    assert [(word.start, word.end) for word in audited_words] == [
        pytest.approx((cue.start + start, cue.start + end))
        for start, end in pairwise(boundaries)
    ]
    assert result.confidences == ((None,) if review else (0.9,))
    assert result.review_reasons == (
        ("Proposed-cut audit: Low-confidence opening or closing word in the proposed cut",)
        if review else ("",)
    )
    assert (result.captions[0].start, result.captions[0].end) == (cue.start, cue.end)
    assert result.captions[0].text == cue.text
    assert ("wording corroborated locally" in result.captions[0].source) is not review


@pytest.mark.parametrize("language", ["en", "auto"])
def test_worker_loads_once_and_reuses_encoded_windows(local_model, tmp_path, monkeypatch, language):
    import ctranslate2

    manager, payloads = local_model
    _install(manager, payloads)
    tokenizer = _WordsTokenizer()
    monkeypatch.setattr(worker, "_Tokenizer", lambda *_: tokenizer)
    monkeypatch.setattr(worker, "runtime_threads", lambda: 4)
    path = tmp_path / "audio.wav"
    sf.write(path, np.ones(16000 * 9, dtype=np.float32) * 0.1, 16000, subtype="PCM_16")
    counts = {"loaded": 0, "encoded": 0, "aligned": 0, "generated": 0, "detected": 0}
    encoded_objects = []

    class Model:
        def __init__(self, _path, **kwargs):
            assert kwargs["intra_threads"] == 4 and kwargs["inter_threads"] == 1
            assert kwargs["device"] == "cpu" and kwargs["compute_type"] == "int8"
            counts["loaded"] += 1

        def encode(self, features):
            assert tuple(features.shape) == (1, 128, 3000)
            counts["encoded"] += 1
            encoded = object()
            encoded_objects.append(encoded)
            return encoded

        def generate(self, encoded, *_args, **_kwargs):
            assert encoded is encoded_objects[-1]
            counts["generated"] += 1
            return [SimpleNamespace(
                sequences_ids=[[20000, 1, 2, 20020, 10000]],
                no_speech_prob=0.01, scores=[-0.1],
            )]

        def detect_language(self, encoded):
            assert encoded is encoded_objects[-1]
            counts["detected"] += 1
            return [[("<|en|>", 0.98)]]

        def align(self, encoded, _start, texts, _frames):
            assert encoded is encoded_objects[-1]
            counts["aligned"] += 1
            count = len(texts[0])
            return [SimpleNamespace(
                alignments=[(i, i * 10) for i in range(count + 1)],
                text_token_probs=[0.95] * count,
            )]

    monkeypatch.setattr(ctranslate2.models, "Whisper", Model)
    captions = (
        SourceCaption(1, 2, "hello there", "test"),
        SourceCaption(7, 8, "other words", "test"),
    )
    result = worker.align_captions(
        lambda *_: None, str(path), captions, 9, str(manager.data_root),
        str(manager.manifest_path), language, 12,
    )
    assert counts == {
        "loaded": 1, "encoded": 2, "aligned": 4, "generated": 2,
        "detected": int(language == "auto"),
    }
    assert len(result) == 2 and all(item.words and not item.problem for item in result)
    assert result[1].words[0].start >= 5
    assert captions[0] == replace(captions[0], text="hello there")


@pytest.mark.parametrize("results", [
    [], [[]], [[("en", 0.9)]], [[("<|en|>", 0.4)]], [[("<|en|>", float("nan"))]],
    [[("<|en|>", 1.1)]], [[("<|en|>", True)]],
])
def test_language_detection_requires_reliable_supported_evidence(results):
    model = SimpleNamespace(detect_language=lambda _encoded: results)
    with pytest.raises(runtime.CaptionTimingError, match="language"):
        worker._detect_language(model, object())


@pytest.mark.parametrize("language", ["", "EN", "../en", None, True])
def test_invalid_language_is_rejected_before_model_setup(monkeypatch, language):
    monkeypatch.setattr(runtime, "runtime_threads", lambda: pytest.fail("model admission attempted"))
    with pytest.raises(runtime.CaptionTimingError, match="language"):
        runtime.improve_caption_audio(
            Path("unused.wav"), (SourceCaption(0, 1, "Hello", "test"),), 1, Path("unused"),
            language=language,
        )


def test_post_cut_audit_reuses_model_and_reads_only_trusted_exact_ranges(
    local_model, tmp_path, monkeypatch,
):
    import ctranslate2

    manager, payloads = local_model
    _install(manager, payloads)
    tokenizer = _WordsTokenizer()
    monkeypatch.setattr(worker, "_Tokenizer", lambda *_: tokenizer)
    monkeypatch.setattr(worker, "runtime_threads", lambda: 2)
    path = tmp_path / "audit.wav"
    sf.write(path, np.ones(16000 * 9, dtype=np.float32) * 0.1, 16000, subtype="PCM_16")
    captions = (
        SourceCaption(0.5, 1.5, "Hello there!", "first source"),
        SourceCaption(2.5, 3.5, "Other words.", "second source"),
        SourceCaption(5.5, 6.5, "Uncertain line", "third source"),
    )
    low_confidence = replace(captions[2], source="third source - timing review needed: uncertain")
    proposed = CaptionTimingResult(
        (
            replace(captions[0], start=0.8, end=1.3, source="first source - model/audio aligned"),
            replace(captions[1], start=3.0, end=3.8, source="second source - model/audio aligned"),
            low_confidence,
        ),
        (0.95, 0.95, None), ("", "", "uncertain"),
    )
    counts = {"loaded": 0, "encoded": 0, "forced": 0}
    progress_messages = []
    reads = []
    actual_read = worker.read_audio_window

    def read(audio, start, end):
        reads.append((start, end))
        return actual_read(audio, start, end)

    monkeypatch.setattr(worker, "read_audio_window", read)

    class Model:
        def __init__(self, _path, **kwargs):
            assert kwargs["intra_threads"] == 2
            counts["loaded"] += 1

        def encode(self, features):
            assert tuple(features.shape) == (1, 128, 3000)
            counts["encoded"] += 1
            return object()

        def align(self, _encoded, _start, texts, _frames):
            counts["forced"] += 1
            count = len(texts[0])
            return [SimpleNamespace(
                alignments=[(index, index * 10) for index in range(count + 1)],
                text_token_probs=[0.95] * count,
            )]

    monkeypatch.setattr(ctranslate2.models, "Whisper", Model)
    recognized_calls = []

    def recognize(model, _tokenizer, _encoded, frames, seconds, offset):
        assert isinstance(model, Model)
        recognized_calls.append((frames, seconds, offset))
        words = ("hello", "there") if offset < 2 else ("other", "words", "extra")
        return tuple(
            TimingWord(text, offset + 0.05 + index * 0.1, offset + 0.15 + index * 0.1, 0.9)
            for index, text in enumerate(words)
        )

    monkeypatch.setattr(worker, "_recognized_words", recognize)

    def correct(originals, evidence, duration):
        assert originals == captions and duration == 9
        assert len(evidence) == 3 and all(item.words for item in evidence)
        return proposed

    monkeypatch.setattr(caption_timing, "correct_caption_timings", correct)
    result = worker.improve_captions(
        lambda event, data: progress_messages.append(data["message"]), str(path), captions,
        9, str(manager.data_root), str(manager.manifest_path), "en", 2,
    )
    assert counts == {"loaded": 1, "encoded": 3, "forced": 1}
    assert reads == [(0, 8.5), (0.8, 1.3), (3.0, 3.8)]
    assert recognized_calls == [(850, 8.5, 0), (50, 0.5, 0.8), (80, 0.8, 3.0)]
    assert result.confidences == (0.9, None, None)
    assert result.review_reasons[0] == ""
    assert "Extra words after" in result.review_reasons[1]
    assert result.review_reasons[2] == "uncertain"
    assert result.captions[2] == low_confidence
    assert "wording corroborated locally" in result.captions[0].source
    assert "model/audio aligned" not in result.captions[1].source
    assert "timing review needed" in result.captions[1].source
    assert [(cue.text, cue.fragments) for cue in result.captions] == [
        (cue.text, cue.fragments) for cue in captions
    ]
    assert any(message.startswith("Aligning caption context") for message in progress_messages)
    assert sum(message.startswith("Auditing proposed caption cuts") for message in progress_messages) == 2


@pytest.mark.parametrize("confidence,reason", [(None, ""), (0.4, ""), (None, "existing concern")])
def test_post_cut_audit_does_not_process_low_confidence_rows(monkeypatch, confidence, reason):
    caption = SourceCaption(0, 1, "Keep this exact text", "original")
    proposed = CaptionTimingResult((caption,), (confidence,), (reason,))
    monkeypatch.setattr(worker, "read_audio_window", lambda *_: pytest.fail("untrusted cut read"))
    result = worker._audit_proposed_cuts(
        lambda *_: None, object(), (caption,), proposed, object(), _WordsTokenizer(), object(),
    )
    assert result.confidences == (None,)
    assert result.review_reasons[0]
    assert result.captions[0].text == caption.text
    assert (result.captions[0].start, result.captions[0].end) == (0, 1)


def test_post_cut_audit_failure_flags_the_draft_without_accurate_label(monkeypatch):
    caption = SourceCaption(0, 1, "Unchanged", "original")
    cut = replace(caption, source="original - model/audio aligned")
    proposed = CaptionTimingResult((cut,), (0.9,), ("",))

    def fail(*_args):
        raise runtime.CaptionTimingError("recognition failed")

    monkeypatch.setattr(worker, "read_audio_window", fail)
    result = worker._audit_proposed_cuts(
        lambda *_: None, SimpleNamespace(frames=16000), (caption,), proposed,
        object(), _WordsTokenizer(), object(),
    )
    assert result.confidences == (None,)
    assert "recognition failed" in result.review_reasons[0]
    assert result.captions[0].text == caption.text
    assert "model/audio aligned" not in result.captions[0].source


def test_post_cut_audit_never_silently_truncates_cut_at_pcm_end(monkeypatch):
    caption = SourceCaption(0, 1, "Unchanged", "original")
    proposed = CaptionTimingResult((caption,), (0.9,), ("",))
    monkeypatch.setattr(worker, "read_audio_window", lambda *_: pytest.fail("truncated cut read"))
    result = worker._audit_proposed_cuts(
        lambda *_: None, SimpleNamespace(frames=15000), (caption,), proposed,
        object(), _WordsTokenizer(), object(),
    )
    assert result.confidences == (None,)
    assert "outside the decoded PCM audio" in result.review_reasons[0]


def test_offline_native_runtime_smoke():
    result = worker.smoke_test(lambda *_: None)
    assert result["features"] == [1, 128, 3000]
    assert result["ctranslate2"] and result["tokenizers"]


def _blocking_native_work(emit, *_args):
    emit("progress", {"message": str(os.getpid())})
    time.sleep(60)


@pytest.mark.parametrize("audit", [False, True])
def test_inflight_worker_cancellation_reaps_owned_process(local_model, monkeypatch, audit):
    manager, payloads = local_model
    _install(manager, payloads)
    monkeypatch.setattr(runtime, "runtime_threads", lambda: 1)
    monkeypatch.setattr(
        worker, "improve_captions" if audit else "align_captions", _blocking_native_work,
    )
    child_pids = []

    def progress(message, _fraction):
        if message.isdigit():
            child_pids.append(int(message))

    with pytest.raises(OperationCancelled):
        method = runtime.improve_caption_audio if audit else runtime.align_caption_audio
        method(
            Path("unused.wav"), [SourceCaption(0, 1, "hello", "test")], 1,
            manager.data_root, progress=progress, cancelled=lambda: bool(child_pids),
            manifest_path=manager.manifest_path,
        )
    assert len(child_pids) == 1
    assert child_pids[0] not in {process.pid for process in multiprocessing.active_children()}
