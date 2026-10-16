from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import wave
from types import SimpleNamespace

import pytest

from choicer_voicer_pack_creator import parakeet, parakeet_worker
from choicer_voicer_pack_creator.analysis import AnalysisError, detect_hardware
from choicer_voicer_pack_creator.parakeet import ParakeetManager
from choicer_voicer_pack_creator.parakeet_worker import (
    TimedToken,
    TranscriptLine,
    build_lines,
    chunk_bounds,
)


def test_chunks_are_at_most_30_seconds_and_cut_at_quiet_points():
    levels = [1.0] * (70 * 50)
    for second in (20, 45):
        levels[second * 50:second * 50 + 15] = [0.0] * 15
    bounds = chunk_bounds(levels, 70 * 16000)
    assert bounds[0][0] == 0 and bounds[-1][1] == 70 * 16000
    assert all(0 < end - start <= 30 * 16000 for start, end in bounds)
    assert all(bounds[index][1] == bounds[index + 1][0] for index in range(len(bounds) - 1))
    cuts = [start / 16000 for start, _ in bounds[1:]]
    assert len(cuts) == 2
    assert 20 <= cuts[0] <= 20.3 and 45 <= cuts[1] <= 45.3
    assert chunk_bounds([0.5] * 100, 32000) == [(0, 32000)]


def _token(text, start, end, probability=0.9):
    return TimedToken(text, start, end, probability)


def test_lines_split_at_sentences_pauses_and_chunks():
    first = [
        _token(" H", 0.0, 0.1, 1.0), _token("ello", 0.1, 0.3, 0.8), _token(" there", 0.3, 0.6),
        _token(".", 0.6, 0.7), _token(" How", 0.8, 1.0), _token(" are", 1.0, 1.2),
        _token(" you", 1.2, 1.4), _token("?\u201d", 1.4, 1.5), _token(" Fine", 1.6, 1.8),
        _token(" thanks", 1.8, 2.1), _token(" then", 3.0, 3.3, None),
    ]
    second = [_token(" Next", 31.0, 31.4)]
    lines = build_lines([first, second])
    assert [line.text for line in lines] == [
        "Hello there.", "How are you?\u201d", "Fine thanks", "then", "Next",
    ]
    assert (lines[0].start, lines[0].end) == (0.0, 0.7)
    assert lines[0].confidence == round((1.0 + 0.8 + 0.9 + 0.9) / 4, 3)
    assert lines[3].confidence is None
    assert (lines[4].start, lines[4].end) == (31.0, 31.4)


def test_long_lines_wrap_at_120_characters():
    tokens = [_token(" word", index * 0.3, index * 0.3 + 0.2) for index in range(60)]
    lines = build_lines([tokens])
    assert len(lines) > 1
    assert all(len(line.text) <= 120 for line in lines)
    assert " ".join(line.text for line in lines) == " ".join(["word"] * 60)


def _fake_model(monkeypatch, files):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:bz2") as package:
        for name, payload in {**files, "test_wavs/en.wav": b"ignored"}.items():
            info = tarfile.TarInfo(f"{parakeet.ARCHIVE_NAME}/{name}")
            info.size = len(payload)
            package.addfile(info, io.BytesIO(payload))
    pinned = {
        name: (len(payload), hashlib.sha256(payload).hexdigest()) for name, payload in files.items()
    }
    monkeypatch.setattr(parakeet, "MODEL_FILES", pinned)
    monkeypatch.setattr(parakeet, "MODEL_BYTES", sum(size for size, _ in pinned.values()))
    downloads = []

    def download(url, destination, digest, size, label, progress, cancelled):
        downloads.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.getvalue())
        return destination

    monkeypatch.setattr(parakeet, "download_verified", download)
    return downloads


def test_model_installs_verifies_and_repairs(tmp_path, monkeypatch):
    files = {"encoder.int8.onnx": b"encoder", "decoder.int8.onnx": b"decoder",
             "joiner.int8.onnx": b"joiner", "tokens.txt": b"tokens"}
    downloads = _fake_model(monkeypatch, files)
    manager = ParakeetManager(tmp_path)
    assert not manager.installed
    model = manager.ensure_model(lambda *_: None, lambda: False)
    assert manager.installed and model == manager.model_dir
    assert {path.name for path in model.iterdir()} == {*files, "NOTICE.txt"}
    assert "CC BY 4.0" in (model / "NOTICE.txt").read_text(encoding="utf-8")
    assert not list((tmp_path / "downloads").iterdir())
    assert not list(model.parent.glob(".extract-*"))
    manager.ensure_model(lambda *_: None, lambda: False)
    assert len(downloads) == 1
    (model / "encoder.int8.onnx").write_bytes(b"ENCODER")
    manager.ensure_model(lambda *_: None, lambda: False)
    assert len(downloads) == 2
    assert (model / "encoder.int8.onnx").read_bytes() == b"encoder"


def test_model_with_wrong_hash_is_not_installed(tmp_path, monkeypatch):
    _fake_model(monkeypatch, {"tokens.txt": b"tokens"})
    monkeypatch.setitem(parakeet.MODEL_FILES, "tokens.txt", (6, "0" * 64))
    manager = ParakeetManager(tmp_path)
    with pytest.raises(AnalysisError, match="failed verification"):
        manager.ensure_model(lambda *_: None, lambda: False)
    assert not manager.model_dir.exists()


def _wav(path, seconds):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * int(seconds * 16000))


def test_transcribe_returns_padded_parakeet_suggestions(tmp_path, monkeypatch):
    wav = tmp_path / "audio.wav"
    _wav(wav, 5)
    manager = ParakeetManager(tmp_path)
    monkeypatch.setattr(manager, "ensure_model", lambda *_: tmp_path)
    calls = []

    def run(target, args, **kwargs):
        calls.append((target, args))
        kwargs["on_event"]("progress", {"message": "Parakeet transcription: 50%", "fraction": 0.5})
        return [TranscriptLine(1.0, 2.0, "Hello there.", 0.95), TranscriptLine(2.5, 2.52, "", None)]

    monkeypatch.setattr(parakeet, "run_process_worker", run)
    progress = []
    suggestions, language = manager.transcribe(
        wav, "auto", detect_hardware(), lambda message, _fraction: progress.append(message),
        lambda: False,
    )
    assert calls[0][0] is parakeet_worker.transcribe_lines
    assert language is None
    assert "Parakeet transcription: 50%" in progress
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert (suggestion.caption, suggestion.source, suggestion.confidence) == (
        "Hello there.", "Parakeet", 0.95,
    )
    assert suggestion.start < 1.0 and suggestion.end > 2.0
    with pytest.raises(AnalysisError, match="25 European languages"):
        manager.transcribe(wav, "ja", detect_hardware(), lambda *_: None, lambda: False)


def test_worker_decodes_chunks_with_absolute_token_times(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    wav = tmp_path / "audio.wav"
    samples = np.full(40 * 16000, 0.1, dtype=np.float32)
    samples[20 * 16000:int(20.3 * 16000)] = 0
    sf.write(wav, samples, 16000, subtype="PCM_16")
    decoded = []

    class Recognizer:
        def create_stream(self):
            return SimpleNamespace(accept_waveform=lambda rate, data: decoded.append(len(data)))

        def decode_stream(self, stream):
            stream.result = SimpleNamespace(
                tokens=[" Hi", "."], timestamps=[0.5, 0.9], durations=[0.4, 0.1],
                ys_log_probs=[-0.1, -0.2],
            )

    configs = []

    def from_transducer(**kwargs):
        configs.append(kwargs)
        return Recognizer()

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        OfflineRecognizer=SimpleNamespace(from_transducer=from_transducer),
    ))
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    events = []
    lines = parakeet_worker.transcribe_lines(
        lambda event, details: events.append(details), str(wav), str(tmp_path), 2,
    )
    assert configs[0]["model_type"] == "nemo_transducer" and configs[0]["num_threads"] == 2
    assert len(decoded) == 2 and sum(decoded) == 40 * 16000
    assert [line.text for line in lines] == ["Hi.", "Hi."]
    assert lines[0].start == pytest.approx(0.5)
    assert 20.5 <= lines[1].start <= 20.8
    assert events[-1]["fraction"] == 1.0


def test_worker_smoke_loads_sherpa_onnx_in_an_isolated_process(tmp_path):
    import json

    report = tmp_path / "report.json"
    assert parakeet_worker.smoke_main(report) == 0
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "sherpa_onnx": "1.13.8", "onnxruntime_imported": False, "qt_imported": False,
    }
