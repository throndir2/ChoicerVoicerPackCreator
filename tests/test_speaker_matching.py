from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from choicer_voicer_pack_creator import speaker_matching as speaker
from choicer_voicer_pack_creator import speaker_worker as worker
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceSnapshot,
    operation_scope,
)
from choicer_voicer_pack_creator.process_worker import run_process_worker
from choicer_voicer_pack_creator.speaker_matching import (
    SpeakerClip,
    SpeakerDownloadRequired,
    SpeakerMatchingCancelled,
    SpeakerMatchingError,
    SpeakerMatchingManager,
    choose_matches,
)


def vector(*values):
    result = np.zeros(256, dtype=np.float32)
    result[:len(values)] = values
    return result / np.linalg.norm(result)


def clip(identifier, character="", *, path="source.wav", start=0, end=3):
    return SpeakerClip(identifier, str(path), start, end, (character,) if character else ())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Speaker matching tests must not download models or media")

    monkeypatch.setattr(speaker, "download_verified", forbidden)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"source audio fixture")
    manager = SpeakerMatchingManager(tmp_path / "local-data")
    manager.model_path.parent.mkdir(parents=True)
    manager.model_path.write_bytes(b"valid model fixture")
    monkeypatch.setattr(
        speaker, "verify_model",
        lambda path, cancelled: path.is_file() and path.read_bytes() == b"valid model fixture",
    )
    calls = []
    signatures = {"reference": vector(1), "target": vector(1)}

    def infer(target, args, **kwargs):
        assert target is worker.embed_clips
        assert kwargs["idle_timeout"] == 120
        calls.append(args[-1])
        kwargs["on_event"]("progress", {"message": "Test inference", "fraction": 0.5})
        return {
            key: {"embedding": signatures[item.segment_id].tolist(), "reason": ""}
            for key, item in args[-1]
        }

    monkeypatch.setattr(speaker, "run_process_worker", infer)
    clips = (
        clip("reference", "Alice", path=source, start=0, end=3),
        clip("target", path=source, start=4, end=7),
    )
    return SimpleNamespace(
        manager=manager, source=source, clips=clips, calls=calls, signatures=signatures,
        media=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )


def run(engine, **kwargs):
    return engine.manager.match(
        engine.media, kwargs.pop("clips", engine.clips), allow_download=kwargs.pop("allow_download", False),
        progress=kwargs.pop("progress", lambda *_: None),
        cancelled=kwargs.pop("cancelled", lambda: False), **kwargs,
    )


def test_single_reference_requires_strong_absolute_evidence():
    clips = (clip("reference", "Alice"), clip("same"), clip("unknown"), clip("weak"))
    result = choose_matches(clips, {
        "reference": vector(1), "same": vector(0.99, 0.05),
        "unknown": vector(0, 1), "weak": vector(0.75, math.sqrt(1 - 0.75**2)),
    })
    assert [(match.segment_id, match.character) for match in result] == [("same", "Alice")]
    assert 0.99 < result[0].similarity <= 1


def test_competition_margin_ambiguous_unknown_and_multi_character_references():
    clips = (
        clip("alice", "Alice"), clip("bob", "Bob"), clip("same"),
        clip("ambiguous"), clip("unknown"), clip("multi"), clip("close"),
    )
    clips = (*clips[:-2], replace(clips[-2], characters=("Alice", "Bob")), clips[-1])
    result = choose_matches(clips, {
        "alice": vector(1), "bob": vector(0.8, 0.6),
        "same": vector(1), "ambiguous": vector(0.9, 0.3),
        "unknown": vector(0, 0, 1), "multi": vector(0, 0, 1),
        "close": vector(0.98, 0.2),
    })
    assert [(match.segment_id, match.character) for match in result] == [("same", "Alice")]


def test_multiple_human_seeds_aggregate_without_using_targets():
    clips = (clip("a1", "Alice"), clip("a2", "Alice"), clip("target"), clip("unknown"))
    matches = choose_matches(clips, {
        "a1": vector(1, 0.3), "a2": vector(1, -0.3),
        "target": vector(1), "unknown": vector(0, 1),
    })
    assert len(matches) == 1 and matches[0].segment_id == "target"
    assert matches[0].similarity == pytest.approx(1)
    assert choose_matches((clip("target"), clip("other")), {
        "target": vector(1), "other": vector(1),
    }) == ()


def test_inconsistent_seeds_cannot_manufacture_centroid_evidence():
    clips = (clip("a1", "Alice"), clip("a2", "Alice"), clip("target"))
    assert choose_matches(clips, {
        "a1": vector(0.6, 0.8), "a2": vector(0.6, -0.8), "target": vector(1),
    }) == ()


def test_cache_reuses_signatures_but_recomputes_labels(engine):
    first = run(engine)
    assert [(match.segment_id, match.character) for match in first.matches] == [("target", "Alice")]
    assert (first.examined, first.cached, first.skipped) == (1, 0, 0)
    second = run(engine, clips=(replace(engine.clips[0], characters=("Bob",)), engine.clips[1]))
    assert (second.matches[0].character, second.cached, len(engine.calls)) == ("Bob", 2, 1)
    assert second.sources == SourceSnapshot.capture([engine.source])
    assert len(list(engine.manager.cache_directory.glob("*.json"))) == 2
    assert not list((engine.manager.data_root / "speaker-jobs").iterdir())
    assert not list(engine.source.parent.glob("*.json"))


@pytest.mark.parametrize("short_index", [0, 1])
def test_known_short_reference_or_target_needs_no_model_or_inference(engine, short_index):
    engine.manager.model_path.unlink()
    clips = list(engine.clips)
    clips[short_index] = replace(clips[short_index], end=clips[short_index].start + 0.4)
    result = run(engine, clips=tuple(clips))
    assert result.matches == ()
    assert result.skip_reasons == ((clips[short_index].segment_id, "short"),)
    assert engine.calls == []


@pytest.mark.parametrize("change", ["range", "stat", "replacement", "path", "preprocessing", "model"])
def test_cache_identity_invalidates_stale_signatures(engine, monkeypatch, change):
    run(engine)
    clips = engine.clips
    if change == "range":
        clips = (clips[0], replace(clips[1], start=4.5))
    elif change == "stat":
        engine.source.write_bytes(b"new source audio")
    elif change == "replacement":
        previous = engine.source.stat()
        alternate = engine.source.with_name("replacement.wav")
        alternate.write_bytes(engine.source.read_bytes())
        os.utime(alternate, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        os.replace(alternate, engine.source)
    elif change == "path":
        alternate = engine.source.with_name("renamed.wav")
        alternate.write_bytes(engine.source.read_bytes())
        clips = tuple(replace(item, path=str(alternate)) for item in clips)
    elif change == "preprocessing":
        monkeypatch.setattr(speaker, "PREPROCESSING_VERSION", "test-next-version")
    else:
        monkeypatch.setattr(speaker, "MODEL_SHA256", "0" * 64)
        engine.manager.model_path.parent.mkdir(parents=True)
        engine.manager.model_path.write_bytes(b"valid model fixture")
    result = run(engine, clips=clips)
    assert len(engine.calls) == 2
    assert result.cached == (1 if change == "range" else 0)


@pytest.mark.parametrize("damage", ["json", "checksum", "shape", "nonfinite", "oversize", "null"])
def test_corrupt_cache_is_ignored_and_recomputed(engine, damage):
    run(engine)
    cache = next(engine.manager.cache_directory.glob("*.json"))
    value = json.loads(cache.read_text())
    if damage == "json":
        cache.write_text("{not json")
    elif damage == "null":
        cache.write_text("null")
    elif damage == "oversize":
        cache.write_text(" " * 32769)
    else:
        if damage == "checksum":
            value["sha256"] = "0" * 64
        elif damage == "shape":
            value["record"]["embedding"] = [1, 0]
            value["sha256"] = hashlib.sha256(
                json.dumps(value["record"], sort_keys=True).encode(),
            ).hexdigest()
        else:
            value["record"]["embedding"][0] = float("nan")
        cache.write_text(json.dumps(value))
    result = run(engine)
    assert result.cached == 1 and len(engine.calls) == 2
    assert result.matches[0].character == "Alice"


def test_missing_or_corrupt_model_never_downloads_without_permission(engine):
    engine.manager.model_path.unlink()
    with pytest.raises(SpeakerDownloadRequired, match="permission"):
        run(engine)
    engine.manager.model_path.write_bytes(b"damaged")
    with pytest.raises(SpeakerDownloadRequired, match="repair"):
        run(engine)
    assert engine.manager.model_path.read_bytes() == b"damaged"
    assert not engine.calls


def test_model_download_is_verified_staged_and_keeps_attribution(engine, monkeypatch):
    engine.manager.model_path.write_bytes(b"damaged")
    downloads = []

    def download(url, path, digest, size, label, progress, cancelled):
        assert url == engine.manager.manifest["model"]["url"]
        assert digest == speaker.MODEL_SHA256 and size == speaker.MODEL_BYTES
        assert path.parent.parent.name == "speaker-jobs"
        assert path != engine.manager.model_path
        path.write_bytes(b"valid model fixture")
        downloads.append(path)
        return path

    monkeypatch.setattr(speaker, "download_verified", download)
    result = run(engine, allow_download=True)
    assert len(result.matches) == 1 and len(downloads) == 1
    assert not downloads[0].exists()
    for name in speaker.MODEL_NOTICES:
        assert (engine.manager.model_path.parent / name).is_file()
    run(engine, allow_download=True)
    assert len(downloads) == 1


def test_invalid_download_does_not_replace_model(engine, monkeypatch):
    engine.manager.model_path.write_bytes(b"damaged")

    def invalid_download(url, path, *args):
        path.write_bytes(b"wrong model")
        return path

    monkeypatch.setattr(speaker, "download_verified", invalid_download)
    with pytest.raises(SpeakerMatchingError, match="verification"):
        run(engine, allow_download=True)
    assert engine.manager.model_path.read_bytes() == b"damaged"
    assert not list(engine.manager.cache_directory.glob("*.json"))


def test_real_model_verifier_checks_size_hash_and_cancellation(tmp_path, monkeypatch):
    path = tmp_path / "model"
    content = b"verified bytes"
    monkeypatch.setattr(speaker, "MODEL_BYTES", len(content))
    monkeypatch.setattr(speaker, "MODEL_SHA256", hashlib.sha256(content).hexdigest())
    assert not speaker.verify_model(path, lambda: False)
    path.write_bytes(content)
    assert speaker.verify_model(path, lambda: False)
    path.write_bytes(b"changed bytes!")
    assert not speaker.verify_model(path, lambda: False)
    with pytest.raises(SpeakerMatchingCancelled):
        speaker.verify_model(path, lambda: True)


def test_cancelled_operation_and_worker_leave_no_partial_result(engine, monkeypatch):
    with pytest.raises(SpeakerMatchingCancelled):
        run(engine, cancelled=lambda: True)
    assert not engine.calls

    def cancel(*args, **kwargs):
        raise OperationCancelled("user canceled")

    monkeypatch.setattr(speaker, "run_process_worker", cancel)
    with pytest.raises(SpeakerMatchingCancelled):
        run(engine)
    assert not list(engine.manager.cache_directory.glob("*.json"))
    assert not list((engine.manager.data_root / "speaker-jobs").iterdir())


def test_nested_operation_cancellation_is_honored(engine):
    stop = False
    with operation_scope(lambda: stop):
        stop = True
        with pytest.raises(SpeakerMatchingCancelled):
            run(engine)


@pytest.mark.parametrize("when", ["inference", "completion"])
def test_source_changes_discard_results(engine, when):
    def progress(message, fraction):
        if (when == "inference" and message == "Test inference") or (
            when == "completion" and message.startswith("Voice matching finished")
        ):
            engine.source.write_bytes(b"changed outside editor")

    with pytest.raises(SpeakerMatchingError, match="changed"):
        run(engine, progress=progress)
    if when == "inference":
        assert not list(engine.manager.cache_directory.glob("*.json"))


def test_no_targets_or_human_references_does_not_require_a_model(engine):
    engine.manager.model_path.unlink()
    assert run(engine, clips=(engine.clips[0],)).matches == ()
    assert run(engine, clips=(engine.clips[1],)).matches == ()
    multi = replace(engine.clips[0], characters=("Alice", "Bob"))
    assert run(engine, clips=(multi, engine.clips[1])).skip_reasons == (
        ("reference", "multiple-characters"),
    )
    assert not engine.calls


@pytest.mark.parametrize("change", [
    {"start": -1}, {"start": float("nan")}, {"end": float("inf")}, {"end": 0},
])
def test_invalid_clip_ranges_fail_closed(engine, change):
    with pytest.raises(SpeakerMatchingError, match="ranges"):
        run(engine, clips=(engine.clips[0], replace(engine.clips[1], **change)))
    assert not engine.calls


def test_duplicate_ids_fail_closed(engine):
    with pytest.raises(SpeakerMatchingError, match="unique"):
        run(engine, clips=(engine.clips[0], engine.clips[0]))


def test_duplicate_audio_ranges_are_inferred_once(engine):
    duplicate = replace(engine.clips[1], segment_id="duplicate")
    result = run(engine, clips=(*engine.clips, duplicate))
    assert len(result.matches) == 2
    assert len(engine.calls[0]) == 2


def test_skip_reasons_are_reported_and_cached(engine, monkeypatch):
    def infer(target, args, **kwargs):
        return {
            key: {"embedding": None, "reason": "silence" if item.segment_id == "target" else "short"}
            for key, item in args[-1]
        }

    monkeypatch.setattr(speaker, "run_process_worker", infer)
    multi = replace(engine.clips[0], segment_id="multi", characters=("Alice", "Bob"))
    clips = (*engine.clips, multi)
    result = run(engine, clips=clips)
    assert result.matches == () and result.skipped == 3
    assert dict(result.skip_reasons) == {
        "multi": "multiple-characters", "reference": "short", "target": "silence",
    }
    assert run(engine, clips=clips).cached == 2


@pytest.mark.parametrize(("samples", "reason"), [
    (np.zeros(32000, dtype=np.float32), "silence"),
    (np.ones(16000, dtype=np.float32) * 0.1, "short"),
    (np.full(32000, float("nan"), dtype=np.float32), "nonfinite"),
    (np.full(32000, float("inf"), dtype=np.float32), "nonfinite"),
    (np.ones(32000, dtype=np.float32) * 0.001, "silence"),
])
def test_unusable_audio_abstains(samples, reason):
    assert worker.usable_audio(samples) == (None, reason)


def test_usable_audio_measures_active_duration_and_trims_silence():
    insufficient = np.concatenate((np.zeros(16000), np.full(16000, 0.1)))
    assert worker.usable_audio(insufficient) == (None, "short")
    enough = np.concatenate((np.zeros(16000), np.full(32000, 0.1), np.zeros(16000)))
    samples, reason = worker.usable_audio(enough)
    assert not reason and len(samples) == 32640


def test_preprocessing_matches_training_options_and_scaling(monkeypatch):
    frame = SimpleNamespace()
    mel = SimpleNamespace()
    options = SimpleNamespace(frame_opts=frame, mel_opts=mel)
    observed = []

    class Bank:
        num_frames_ready = 2

        def __init__(self, received):
            assert received is options

        def accept_waveform(self, rate, samples):
            observed.append((rate, samples))

        def input_finished(self):
            pass

        def get_frame(self, index):
            return np.full(80, index + 3, dtype=np.float32)

    monkeypatch.setitem(sys.modules, "kaldi_native_fbank", SimpleNamespace(
        FbankOptions=lambda: options, OnlineFbank=Bank,
    ))
    features = worker.fbank_features(np.array([0.5, -0.5], dtype=np.float32))
    assert observed == [(16000, [16384.0, -16384.0])]
    assert vars(frame) == {
        "samp_freq": 16000, "frame_length_ms": 25, "frame_shift_ms": 10,
        "preemph_coeff": 0.97, "dither": 0, "snip_edges": True, "window_type": "hamming",
    }
    assert vars(mel) == {"num_bins": 80, "low_freq": 20, "high_freq": 0}
    assert features.shape == (1, 2, 80) and features.dtype == np.float32
    assert np.array_equal(features.mean(axis=1), np.zeros((1, 80), dtype=np.float32))


def test_cpu_session_is_bounded_and_spinning_disabled(monkeypatch, tmp_path):
    import onnxruntime as ort

    monkeypatch.setattr(worker, "verify_model", lambda *args: True)
    expected = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(type="tensor(float)", shape=[1, "frames", 80])],
        get_outputs=lambda: [SimpleNamespace(type="tensor(float)", shape=[1, 256])],
    )

    def session(path, *, sess_options, providers):
        assert sess_options.intra_op_num_threads == 2 and sess_options.inter_op_num_threads == 1
        assert sess_options.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL
        assert providers == ["CPUExecutionProvider"]
        for kind in ("intra", "inter"):
            assert sess_options.get_session_config_entry(f"session.{kind}_op.allow_spinning") == "0"
        return expected

    monkeypatch.setattr(ort, "InferenceSession", session)
    assert worker.load_session(tmp_path / "model") is expected


def test_decode_is_bounded_and_tied_to_requested_source_range(monkeypatch, tmp_path):
    import soundfile as sf

    destination = tmp_path / "clip.wav"
    commands = []

    def decode(command, description, cancelled, **kwargs):
        commands.append(command)
        sf.write(destination, np.full(16000, 0.1), 16000, subtype="FLOAT")

    monkeypatch.setattr(worker, "_run_cancellable", decode)
    result = worker.decode_clip(
        "ffmpeg", clip("long", start=100, end=200), 3600, destination,
    )
    command = commands[0]
    assert command[command.index("-ss") + 1] == "144.000000000"
    assert command[command.index("-t") + 1] == "12.000000000"
    assert len(result) <= speaker.MAX_CLIP_SECONDS * speaker.SAMPLE_RATE
    assert worker.decode_clip("ffmpeg", clip("short", start=100, end=101), 200, destination) is None
    worker.decode_clip("ffmpeg", clip("prompt", start=0, end=None), 4, destination)
    assert commands[-1][commands[-1].index("-t") + 1] == "4.000000000"


def test_worker_reuses_probe_and_avoids_loading_model_for_silence(monkeypatch, tmp_path):
    probes = []
    monkeypatch.setattr(worker, "source_duration", lambda *args: probes.append(args) or 10)
    monkeypatch.setattr(worker, "decode_clip", lambda *args: np.zeros(32000))
    monkeypatch.setattr(worker, "load_session", lambda *_: pytest.fail("No model needed for silence"))
    result = worker.embed_clips(lambda *_: None, "model", "ffmpeg", "ffprobe", str(tmp_path), (
        ("one", clip("one")), ("two", clip("two", start=4, end=6)),
    ))
    assert len(probes) == 1
    assert result == {
        "one": {"embedding": None, "reason": "silence"},
        "two": {"embedding": None, "reason": "silence"},
    }


def test_manifest_records_actual_preprocessing_and_threshold_policy(tmp_path):
    manifest = SpeakerMatchingManager(tmp_path).manifest
    assert manifest["model"]["bytes"] == speaker.MODEL_BYTES
    assert manifest["model"]["sha256"] == speaker.MODEL_SHA256
    assert manifest["preprocessing"]["version"] == speaker.PREPROCESSING_VERSION
    assert manifest["preprocessing"]["minimum_active_seconds"] == speaker.MIN_ACTIVE_SECONDS
    assert manifest["preprocessing"]["maximum_clip_seconds"] == speaker.MAX_CLIP_SECONDS
    assert manifest["matching"]["minimum_cosine"] == speaker.MIN_COSINE
    assert manifest["matching"]["single_character_minimum_cosine"] == speaker.SINGLE_CHARACTER_MIN_COSINE
    assert manifest["matching"]["minimum_runner_up_margin"] == speaker.MIN_RUNNER_UP_MARGIN
    for name in speaker.MODEL_NOTICES:
        assert (speaker.default_manifest_path().parent / name).is_file()


def test_supervised_native_preprocessing_smoke_without_model_download():
    report = run_process_worker(
        worker.smoke_test, (), on_event=lambda *_: True, cancelled=lambda: False, timeout=45,
    )
    assert report == {
        "features": [1, 198, 80], "kaldi_native_fbank": "1.22.3", "numpy": "2.4.6",
        "onnxruntime": "1.26.0", "soundfile": "0.13.1", "qt_imported": False,
    }
