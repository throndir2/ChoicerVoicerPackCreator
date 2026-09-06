from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
import wave
import zipfile
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import choicer_voicer_pack_creator.analysis as analysis_module
from choicer_voicer_pack_creator.analysis import (
    ActivityRegion,
    AnalysisCancelled,
    AnalysisError,
    AnalysisSuggestion,
    HardwareProfile,
    WhisperManager,
    analyze_video,
    combine_suggestions,
    default_manifest_path,
    scan_audio_activity,
)
from choicer_voicer_pack_creator.diagnostics import AnalysisDiagnostics, analysis_log_path
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import CaptionFragment, SourceCaption
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    operation_scope,
    path_leases,
)


def _write_test_wav(path: Path) -> None:
    sample_rate = 16_000
    samples = array("h")
    for seconds, amplitude in ((0.5, 0), (0.8, 12_000), (0.5, 0), (0.6, 8_000)):
        count = round(seconds * sample_rate)
        for index in range(count):
            samples.append(amplitude if index % 16 < 8 else -amplitude)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())


def test_activity_scan_finds_deterministic_regions(tmp_path: Path) -> None:
    wav_path = tmp_path / "activity.wav"
    _write_test_wav(wav_path)
    progress: list[tuple[str, float | None]] = []

    regions, threshold = scan_audio_activity(
        wav_path,
        2.4,
        "balanced",
        lambda message, value: progress.append((message, value)),
        lambda: False,
    )

    assert threshold is not None
    assert len(regions) == 2
    assert regions[0].start == pytest.approx(0.4, abs=0.04)
    assert regions[0].end == pytest.approx(1.44, abs=0.04)
    assert regions[1].start == pytest.approx(1.7, abs=0.04)
    assert regions[1].end == pytest.approx(2.4, abs=0.04)
    assert progress[-1][1] == 1.0


def test_raw_activity_retains_real_gap_edges_without_default_padding(tmp_path: Path) -> None:
    wav_path = tmp_path / "activity.wav"
    _write_test_wav(wav_path)
    messages = []
    regions, threshold = scan_audio_activity(
        wav_path, 2.4, "balanced", lambda message, _: messages.append(message),
        lambda: False, raw=True,
    )
    assert regions == [ActivityRegion(0.5, 1.3), ActivityRegion(1.8, 2.4)]
    assert threshold is not None
    assert all("YouTube refinement" in message for message in messages)


def test_raw_activity_retains_short_and_quiet_sounds_instead_of_false_pauses(tmp_path) -> None:
    path = tmp_path / "activity.wav"
    samples = array("h")
    for seconds, amplitude in ((0.5, 12000), (0.4, 300), (0.04, 12000), (0.5, 0)):
        samples.extend(amplitude if index % 16 < 8 else -amplitude
                       for index in range(round(seconds * 16000)))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples.tobytes())
    regions, _ = scan_audio_activity(
        path, 1.44, "balanced", lambda *_: None, lambda: False, raw=True,
    )
    assert len(regions) == 1
    assert (regions[0].start, regions[0].end) == pytest.approx((0, 0.94))


def test_raw_activity_scan_checks_cancellation(tmp_path: Path) -> None:
    wav_path = tmp_path / "activity.wav"
    _write_test_wav(wav_path)
    with pytest.raises(AnalysisCancelled):
        scan_audio_activity(
            wav_path, 2.4, "balanced", lambda *_: None, lambda: True, raw=True,
        )


def test_raw_activity_end_never_rounds_beyond_video_duration(tmp_path: Path) -> None:
    wav_path = tmp_path / "activity.wav"
    _write_test_wav(wav_path)
    regions, _ = scan_audio_activity(
        wav_path, 1.23456, "balanced", lambda *_: None, lambda: False, raw=True,
    )
    assert regions == [ActivityRegion(0.5, 1.23456)]


def test_silent_activity_scan_returns_no_suggestions(tmp_path: Path) -> None:
    wav_path = tmp_path / "silence.wav"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(array("h", [0] * 16_000).tobytes())

    regions, threshold = scan_audio_activity(
        wav_path, 1, "balanced", lambda *_args: None, lambda: False
    )

    assert regions == []
    assert threshold is None


def test_sustained_quiet_activity_is_not_rejected_by_threshold(tmp_path: Path) -> None:
    wav_path = tmp_path / "quiet.wav"
    samples = array("h", [800 if index % 16 < 8 else -800 for index in range(16_000)])
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(samples.tobytes())

    regions, threshold = scan_audio_activity(
        wav_path, 1, "balanced", lambda *_args: None, lambda: False
    )

    assert threshold is not None
    assert regions == [ActivityRegion(0.0, 1)]


def test_sparse_activity_and_all_sensitivities_remain_detectable(tmp_path: Path) -> None:
    wav_path = tmp_path / "sparse.wav"
    samples = array("h", [0] * (16_000 * 20))
    for index in range(16_000 * 9, 16_000 * 9 + 4800):
        samples[index] = 9000 if index % 16 < 8 else -9000
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(samples.tobytes())

    for sensitivity in ("sensitive", "balanced", "conservative"):
        regions, threshold = scan_audio_activity(
            wav_path, 20, sensitivity, lambda *_args: None, lambda: False
        )
        assert threshold is not None
        assert len(regions) == 1
        assert regions[0].start == pytest.approx(8.9, abs=0.08)
        assert regions[0].end == pytest.approx(9.44, abs=0.08)


def test_production_whisper_manifest_is_immutable_and_complete() -> None:
    manifest_path = default_manifest_path()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = manifest["runtime"]

    assert runtime["build"] == "b4938"
    assert runtime["version"] == "1.9.3"
    assert len(runtime["archive_sha256"]) == 64
    assert "releases/download/b4938/" in runtime["archive_url"]
    assert "whisper-cli.exe" in runtime["runtime_files"]
    assert len(runtime["runtime_files"]) == len(set(runtime["runtime_files"]))
    for metadata in runtime["runtime_files"].values():
        assert int(metadata["bytes"]) > 0
        assert len(metadata["sha256"]) == 64
    assert set(manifest["models"]) == {"tiny", "base"}
    for model in manifest["models"].values():
        assert len(model["sha256"]) == 64
        assert manifest["model_source"]["commit"] in model["url"]
        assert int(model["bytes"]) > 70 * 1024**2
    assert (manifest_path.parent / "WhisperCpp-MIT.txt").is_file()
    assert (manifest_path.parent / "OpenAI-Whisper-MIT.txt").is_file()


def test_combining_transcript_keeps_untranscribed_activity() -> None:
    activity = [ActivityRegion(1, 2), ActivityRegion(4, 5)]
    transcript = [AnalysisSuggestion(0.9, 2.1, "Hello", "Whisper", 0.8)]

    combined = combine_suggestions(activity, transcript)

    assert combined == [
        transcript[0],
        AnalysisSuggestion(4, 5, "", "Untranscribed activity"),
    ]


def test_combining_transcripts_subtracts_covered_union_only() -> None:
    activity = [ActivityRegion(0, 5)]
    transcripts = [
        AnalysisSuggestion(1, 3, "One", "Whisper"),
        AnalysisSuggestion(2, 4, "Two", "Whisper"),
    ]

    combined = combine_suggestions(activity, transcripts)

    assert combined == [
        AnalysisSuggestion(0, 1, "", "Untranscribed activity"),
        transcripts[0],
        transcripts[1],
        AnalysisSuggestion(4, 5, "", "Untranscribed activity"),
    ]


@pytest.mark.integration
def test_video_activity_analysis_runs_end_to_end_without_model(tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not available")
    wav_path = tmp_path / "activity.wav"
    _write_test_wav(wav_path)
    video = tmp_path / "activity.mp4"
    media = MediaTools()
    media.run(
        [
            media.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=10:d=2.4",
            "-i",
            str(wav_path),
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ],
        "Creating deterministic analysis video",
    )

    result = analyze_video(
        media,
        video,
        2.4,
        tmp_path / "analysis-data",
        sensitivity="balanced",
        use_whisper=False,
        model_key="tiny",
        language="auto",
        progress=lambda *_args: None,
        cancelled=lambda: False,
    )

    assert result.activity_regions == 2
    assert result.transcript_regions == 0
    assert all(item.source == "Audio activity" for item in result.suggestions)
    assert all(item.caption == "" for item in result.suggestions)
    assert result.refined_captions is None


@pytest.mark.parametrize("empty", [False, True])
def test_refine_only_analysis_uses_audio_without_whisper_and_reports_separate_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty: bool,
) -> None:
    (tmp_path / "video.mp4").write_bytes(b"synthetic video")
    def extract(_media, _video, destination, _progress, _cancelled):
        _write_test_wav(destination)

    def forbidden_whisper(*_args, **_kwargs):
        raise AssertionError("Refinement must not construct a Whisper manager or download models")

    monkeypatch.setattr(analysis_module, "extract_analysis_audio", extract)
    monkeypatch.setattr(analysis_module, "WhisperManager", forbidden_whisper)
    cues = [] if empty else [SourceCaption(0.5, 2.4, "Hello there", "YouTube", (
        CaptionFragment("Hello ", 0.5), CaptionFragment("there", 1.8),
    ))]
    original = [cue.to_dict() for cue in cues]
    messages = []
    with AnalysisDiagnostics(tmp_path / "diagnostics"):
        result = analyze_video(
            object(), tmp_path / "video.mp4", 2.4, tmp_path / "data",
            sensitivity="balanced", use_whisper=False, model_key="tiny", language="auto",
            progress=lambda message, _: messages.append(message), cancelled=lambda: False,
            source_captions=cues, pause_threshold=0.4,
        )
    assert result.suggestions == []
    assert result.refined_captions is not None
    assert [row.text for row in result.refined_captions] == ([] if empty else ["Hello", "there"])
    assert result.activity_regions == 2
    assert result.transcript_regions == 0
    assert result.model_name is None
    assert [cue.to_dict() for cue in cues] == original
    assert any("Refining YouTube" in message for message in messages)
    events = [json.loads(line) for line in analysis_log_path(tmp_path / "diagnostics")
              .read_text(encoding="utf-8").splitlines()]
    configuration = next(event for event in events if event["event"] == "analysis_configuration")
    assert configuration["refine_youtube"] is True
    outcome = next(event for event in events if event["event"] == "analysis_results")
    assert outcome["refined_captions"] == (0 if empty else 2)


def test_normal_analysis_keeps_default_scan_and_whisper_output(tmp_path, monkeypatch) -> None:
    (tmp_path / "video").write_bytes(b"synthetic video")
    calls = []
    transcript = AnalysisSuggestion(0.5, 1, "Original Whisper", "Whisper", 0.8)
    component = tmp_path / "installed-component"
    component.write_bytes(b"test")

    class FakeWhisper:
        cli_path = component
        models = {"tiny": {"name": "Fake tiny"}}

        def __init__(self, *_):
            pass

        def model_path(self, _key):
            return component

        def transcribe(self, *_):
            return [transcript], "en"

    def scan(*_args, **kwargs):
        calls.append(kwargs)
        return [ActivityRegion(0.5, 1)], -30

    monkeypatch.setattr(analysis_module, "WhisperManager", FakeWhisper)
    monkeypatch.setattr(analysis_module, "extract_analysis_audio", lambda *_: None)
    monkeypatch.setattr(analysis_module, "scan_audio_activity", scan)
    monkeypatch.setattr(
        analysis_module, "detect_hardware", lambda: HardwareProfile(4, None, None, "tiny", "test")
    )
    result = analyze_video(
        object(), tmp_path / "video", 2, tmp_path,
        sensitivity="balanced", use_whisper=True, model_key="tiny", language="en",
        progress=lambda *_: None, cancelled=lambda: False,
    )
    assert calls == [{}]
    assert result.suggestions == [transcript]
    assert result.refined_captions is None
    assert result.detected_language == "en"


def test_refinement_shares_disk_checks_and_propagates_cancellation(tmp_path, monkeypatch) -> None:
    (tmp_path / "video").write_bytes(b"synthetic video")
    arguments = dict(
        sensitivity="balanced", use_whisper=False, model_key="tiny", language="auto",
        progress=lambda *_: None, source_captions=[],
    )
    with pytest.raises(AnalysisCancelled):
        analyze_video(object(), tmp_path / "video", 2, tmp_path, **arguments, cancelled=lambda: True)
    monkeypatch.setattr(
        analysis_module.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(1, 1, 0)
    )
    with pytest.raises(AnalysisError, match="free"):
        analyze_video(object(), tmp_path / "video", 2, tmp_path, **arguments, cancelled=lambda: False)


@pytest.mark.parametrize("change", ["modify", "replace", "delete"])
def test_standalone_analysis_rejects_changed_source_before_returning(tmp_path, monkeypatch, change):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic video")

    def extract(_media, _video, destination, _progress, _cancelled):
        _write_test_wav(destination)
        if change == "modify":
            video.write_bytes(b"externally changed video")
        elif change == "replace":
            replacement = video.with_suffix(".replacement")
            replacement.write_bytes(b"synthetic video")
            replacement.replace(video)
        else:
            video.unlink()

    monkeypatch.setattr(analysis_module, "extract_analysis_audio", extract)
    with pytest.raises(SourceChangedError):
        analyze_video(
            object(), video, 2.4, tmp_path / "data",
            sensitivity="balanced", use_whisper=False, model_key="tiny", language="auto",
            progress=lambda *_: None, cancelled=lambda: False,
        )


def test_standalone_analysis_holds_source_read_lease_until_result_verification(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic video")
    analyzing = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    cancel_writer = threading.Event()
    result = object()

    def analyze(*_args, **_kwargs):
        analyzing.set()
        assert release.wait(timeout=10)
        return result

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    def replace_video():
        with operation_scope(cancel_writer.is_set, progress), path_leases(write_paths=(video,)):
            video.write_bytes(b"unexpected replacement")

    monkeypatch.setattr(analysis_module, "_analyze_video", analyze)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(
            analyze_video, object(), video, 2.4, tmp_path / "data",
            sensitivity="balanced", use_whisper=False, model_key="tiny", language="auto",
            progress=lambda *_: None, cancelled=lambda: False,
        )
        try:
            assert analyzing.wait(timeout=5)
            writer = executor.submit(replace_video)
            assert waiting.wait(timeout=5)
            cancel_writer.set()
            with pytest.raises(OperationCancelled):
                writer.result(timeout=3)
            assert video.read_bytes() == b"synthetic video"
        finally:
            cancel_writer.set()
            release.set()
        assert pending.result(timeout=5) is result
    with path_leases(write_paths=(video,)):
        video.write_bytes(b"replacement after analysis")


def test_standalone_analysis_source_lease_wait_is_cancellable(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic video")
    waiting = threading.Event()
    cancelled = threading.Event()

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    def unexpected(*_args, **_kwargs):
        pytest.fail("Analysis must not start before the source lease is acquired")

    monkeypatch.setattr(analysis_module, "_analyze_video", unexpected)
    with path_leases(write_paths=(video,)), ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            analyze_video, object(), video, 2.4, tmp_path / "data",
            sensitivity="balanced", use_whisper=False, model_key="tiny", language="auto",
            progress=progress, cancelled=cancelled.is_set,
        )
        try:
            assert waiting.wait(timeout=5)
        finally:
            cancelled.set()
        with pytest.raises(AnalysisCancelled):
            pending.result(timeout=3)


@pytest.fixture
def whisper_manager(tmp_path, monkeypatch):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    runtime_files = ["whisper-cli.exe", "whisper.dll", "ggml.dll"]
    runtime_payloads = {
        filename: f"payload-{filename}".encode() for filename in runtime_files
    }
    archive = metadata / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for filename, payload in runtime_payloads.items():
            package.writestr(f"Release/{filename}", payload)
        package.writestr("Release/not-allowed.exe", b"must not extract")
    model_source = metadata / "model.bin"
    model_source.write_bytes(b"model data")
    for license_name in ("WhisperCpp-MIT.txt", "OpenAI-Whisper-MIT.txt"):
        (metadata / license_name).write_text("MIT test", encoding="utf-8")
    manifest = metadata / "whisper-analysis-windows-x64.json"
    manifest.write_text(
        json.dumps(
            {
                "runtime": {
                    "version": "1.9.3",
                    "build": "test",
                    "archive_name": "runtime.zip",
                    "archive_url": archive.as_uri(),
                    "archive_bytes": archive.stat().st_size,
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "archive_root": "Release",
                    "runtime_files": {
                        filename: {
                            "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                        for filename, payload in runtime_payloads.items()
                    },
                },
                "models": {
                    "tiny": {
                        "name": "Test model",
                        "filename": "model.bin",
                        "bytes": model_source.stat().st_size,
                        "sha256": hashlib.sha256(model_source.read_bytes()).hexdigest(),
                        "url": model_source.as_uri(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        analysis_module,
        "_run_cancellable",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "1.9.3", ""),
    )
    return WhisperManager(tmp_path / "installed", manifest)


def test_whisper_setup_uses_verified_allowlist_and_cache(tmp_path, whisper_manager) -> None:
    manager = whisper_manager
    progress: list[str] = []
    with AnalysisDiagnostics(tmp_path / "installed"):
        cli = manager.ensure_runtime(
            lambda message, _value: progress.append(message), lambda: False
        )
        model = manager.ensure_model(
            "tiny", lambda message, _value: progress.append(message), lambda: False
        )
    events = [
        json.loads(line) for line in analysis_log_path(tmp_path / "installed")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert [item["component"] for item in events if item["event"] == "component_download_verified"] == [
        "Whisper CPU runtime", "Test model",
    ]
    assert any(item["event"] == "runtime_setup" for item in events)

    assert cli.read_bytes() == b"payload-whisper-cli.exe"
    assert model.read_bytes() == b"model data"
    assert not (manager.runtime_dir / "not-allowed.exe").exists()
    assert (manager.runtime_dir / "WhisperCpp-MIT.txt").is_file()
    assert manager.ensure_runtime(
        lambda message, _value: progress.append(message), lambda: False
    ) == cli
    assert manager.ensure_model(
        "tiny", lambda message, _value: progress.append(message), lambda: False
    ) == model
    assert any("verified" in message.casefold() for message in progress)

    (manager.runtime_dir / "unexpected.exe").write_bytes(b"untrusted")
    assert manager.ensure_runtime(lambda *_args: None, lambda: False) == cli
    assert not (manager.runtime_dir / "unexpected.exe").exists()


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_concurrent_runtime_setup_serializes_partial_and_reports_waiting(
    whisper_manager, monkeypatch, cancel_waiter,
):
    manager = whisper_manager
    partial = manager.runtime_dir.with_name(manager.runtime_dir.name + ".partial")
    installing = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    cancelled = threading.Event()
    launches = []

    def version(*args, **kwargs):
        launches.append(args[0])
        installing.set()
        assert release.wait(timeout=10)
        assert partial.is_dir()
        return subprocess.CompletedProcess([], 0, "1.9.3", "")

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    monkeypatch.setattr(analysis_module, "_run_cancellable", version)
    second = WhisperManager(manager.data_root, manager.manifest_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(manager.ensure_runtime, lambda *_: None, lambda: False)
        try:
            assert installing.wait(timeout=5)
            waiter = executor.submit(second.ensure_runtime, progress, cancelled.is_set)
            assert waiting.wait(timeout=5)
            if cancel_waiter:
                cancelled.set()
                with pytest.raises(AnalysisCancelled):
                    waiter.result(timeout=3)
                assert partial.is_dir()
                assert not first.done()
        finally:
            release.set()
        assert first.result(timeout=5) == manager.cli_path
        if not cancel_waiter:
            assert waiter.result(timeout=5) == manager.cli_path
    assert len(launches) == 1
    assert manager.cli_path.read_bytes() == b"payload-whisper-cli.exe"
    assert not partial.exists()


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_concurrent_downloads_share_transfer_and_preserve_active_partial(
    tmp_path, monkeypatch, cancel_waiter,
):
    source = tmp_path / "source.bin"
    payload = b"synthetic pinned model"
    source.write_bytes(payload)
    destination = tmp_path / "cache" / "model.bin"
    partial = destination.with_name(destination.name + ".partial")
    downloading = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    cancelled = threading.Event()
    transfers = []
    open_url = analysis_module.urllib.request.urlopen

    def urlopen(*args, **kwargs):
        transfers.append(args[0])
        return open_url(*args, **kwargs)

    def first_progress(message, _fraction):
        if message.startswith("Downloading "):
            downloading.set()
            assert release.wait(timeout=10)

    def waiter_progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    def download(progress, cancel):
        return analysis_module.download_verified(
            source.as_uri(), destination, hashlib.sha256(payload).hexdigest(),
            len(payload), "test model", progress, cancel,
        )

    monkeypatch.setattr(analysis_module.urllib.request, "urlopen", urlopen)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(download, first_progress, lambda: False)
        try:
            assert downloading.wait(timeout=5)
            waiter = executor.submit(download, waiter_progress, cancelled.is_set)
            assert waiting.wait(timeout=5)
            if cancel_waiter:
                cancelled.set()
                with pytest.raises(AnalysisCancelled):
                    waiter.result(timeout=3)
                assert partial.is_file()
                assert not first.done()
        finally:
            release.set()
        assert first.result(timeout=5) == destination
        if not cancel_waiter:
            assert waiter.result(timeout=5) == destination
    assert len(transfers) == 1
    assert destination.read_bytes() == payload
    assert not partial.exists()


def test_download_lease_includes_partial_path(tmp_path):
    destination = tmp_path / "model.bin"
    partial = destination.with_name(destination.name + ".partial")
    partial.write_bytes(b"owned by another job")
    cancelled = threading.Event()
    waiting = threading.Event()

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    with path_leases(write_paths=(partial,)), ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            analysis_module.download_verified, "file:///unused", destination, "0" * 64,
            1, "test model", progress, cancelled.is_set,
        )
        try:
            assert waiting.wait(timeout=5)
        finally:
            cancelled.set()
        with pytest.raises(AnalysisCancelled):
            pending.result(timeout=3)
        assert partial.read_bytes() == b"owned by another job"
        assert not destination.exists()


def test_runtime_lease_includes_partial_directory(whisper_manager):
    manager = whisper_manager
    partial = manager.runtime_dir.with_name(manager.runtime_dir.name + ".partial")
    partial.mkdir(parents=True)
    owned_file = partial / "owned.dll"
    owned_file.write_bytes(b"other active installation")
    cancelled = threading.Event()
    waiting = threading.Event()

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    with path_leases(write_paths=(partial,)), ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(manager.ensure_runtime, progress, cancelled.is_set)
        try:
            assert waiting.wait(timeout=5)
        finally:
            cancelled.set()
        with pytest.raises(AnalysisCancelled):
            pending.result(timeout=3)
        assert owned_file.read_bytes() == b"other active installation"
        assert not manager.runtime_dir.exists()


def test_cached_download_hashing_checks_cancellation_per_chunk(tmp_path, monkeypatch):
    destination = tmp_path / "model.bin"
    payload = b"many small chunks"
    destination.write_bytes(payload)
    partial = destination.with_name(destination.name + ".partial")
    partial.write_bytes(b"previous partial")
    monkeypatch.setattr(analysis_module, "BUFFER_SIZE", 2)
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 7

    with pytest.raises(AnalysisCancelled):
        analysis_module.download_verified(
            "file:///unused", destination, hashlib.sha256(payload).hexdigest(),
            len(payload), "test model", lambda *_: None, cancelled,
        )
    assert destination.read_bytes() == payload
    assert partial.read_bytes() == b"previous partial"


def test_runtime_extraction_is_chunk_cancellable(whisper_manager, monkeypatch):
    manager = whisper_manager
    cancelled = threading.Event()
    reads = []
    read = zipfile.ZipExtFile.read

    def read_chunk(stream, size=-1):
        chunk = read(stream, size)
        reads.append(chunk)
        cancelled.set()
        return chunk

    monkeypatch.setattr(analysis_module, "BUFFER_SIZE", 2)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", read_chunk)
    with pytest.raises(AnalysisCancelled):
        manager.ensure_runtime(lambda *_: None, cancelled.is_set)
    assert reads == [b"pa"]
    assert not manager.runtime_dir.exists()
    assert not manager.runtime_dir.with_name(manager.runtime_dir.name + ".partial").exists()


def test_analysis_checks_ambient_cancellation(tmp_path):
    assert issubclass(AnalysisCancelled, OperationCancelled)
    cancelled = threading.Event()
    source = tmp_path / "model.bin"
    source.write_bytes(b"model")
    with operation_scope(cancelled.is_set):
        cancelled.set()
        with pytest.raises(AnalysisCancelled):
            analysis_module._check_cancel(lambda: False)
        with pytest.raises(AnalysisCancelled):
            analysis_module.sha256(source)
        with pytest.raises(AnalysisCancelled):
            analysis_module.download_verified(
                source.as_uri(), tmp_path / "target.bin", "0" * 64, 5,
                "test model", lambda *_: None, lambda: False,
            )
    assert not (tmp_path / "target.bin").exists()


def test_component_setup_does_not_commit_the_enclosing_analysis(whisper_manager):
    committed = []
    with operation_scope(committed=lambda: committed.append(True)):
        whisper_manager.ensure_runtime(lambda *_: None, lambda: False)
        whisper_manager.ensure_model("tiny", lambda *_: None, lambda: False)
    assert committed == []


def test_download_rejects_unapproved_redirect_and_oversized_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response:
        def __init__(self, url: str, payload: bytes, content_length: str | None = None):
            self.url = url
            self.payload = payload
            self.headers = {} if content_length is None else {"Content-Length": content_length}
            self.read_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, _size):
            if self.read_count:
                return b""
            self.read_count += 1
            return self.payload

    manager = WhisperManager(tmp_path, default_manifest_path())
    monkeypatch.setattr(
        analysis_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response("https://evil.example/model.bin", b"1234", "4"),
    )
    with pytest.raises(AnalysisError, match="unapproved host"):
        manager._download(
            "https://huggingface.co/model.bin",
            tmp_path / "redirect.bin",
            hashlib.sha256(b"1234").hexdigest(),
            4,
            "test model",
            lambda *_args: None,
            lambda: False,
        )

    monkeypatch.setattr(
        analysis_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response("https://huggingface.co/model.bin", b"12345"),
    )
    with pytest.raises(AnalysisError, match="exceeded its pinned size"):
        manager._download(
            "https://huggingface.co/model.bin",
            tmp_path / "oversized.bin",
            "0" * 64,
            4,
            "test model",
            lambda *_args: None,
            lambda: False,
        )
    assert not (tmp_path / "redirect.bin.partial").exists()
    assert not (tmp_path / "oversized.bin.partial").exists()

    class OutputTracker:
        payload = b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, value):
            self.payload += value

        def flush(self):
            return None

        def fileno(self):
            return 1

    tracker = OutputTracker()
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: tracker)
    monkeypatch.setattr(analysis_module.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(
        analysis_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response("https://huggingface.co/model.bin", b"12345"),
    )
    with pytest.raises(AnalysisError, match="exceeded its pinned size"):
        manager._download(
            "https://huggingface.co/model.bin",
            tmp_path / "prewrite.bin",
            "0" * 64,
            4,
            "test model",
            lambda *_args: None,
            lambda: False,
        )
    assert tracker.payload == b""


def test_download_total_deadline_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DeadlineResponse:
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://huggingface.co/model.bin"

        def read(self, _size):
            return b"x"

    clock = iter((0.0, 0.0, 61.0))
    monkeypatch.setattr(analysis_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        analysis_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: DeadlineResponse(),
    )
    manager = WhisperManager(tmp_path, default_manifest_path())
    with pytest.raises(AnalysisError, match="exceeded its time limit"):
        manager._download(
            "https://huggingface.co/model.bin",
            tmp_path / "deadline.bin",
            "0" * 64,
            4,
            "test model",
            lambda *_args: None,
            lambda: False,
        )


@pytest.mark.parametrize("invalid_edge", [
    None, "missing-first", "missing-last", "zero-first", "zero-last",
    "nonfinite", "negative", "outside-segment", "backwards",
])
@pytest.mark.parametrize(("segment_bounds", "expected"), [
    ((0, 12240), (0, 12)),
    ((400, 5400), (0.25, 5.65)),
])
def test_whisper_parser_preserves_segment_envelope_instead_of_trusting_token_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_edge, segment_bounds, expected,
) -> None:
    wav_path = tmp_path / "source.wav"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(array("h", [0] * (16_000 * 12)).tobytes())
    manager = WhisperManager(tmp_path / "data", default_manifest_path())
    (tmp_path / "model.bin").write_bytes(b"test model")
    monkeypatch.setattr(manager, "ensure_runtime", lambda *_args: tmp_path / "whisper-cli.exe")
    monkeypatch.setattr(manager, "ensure_model", lambda *_args: tmp_path / "model.bin")

    def write_transcript(command, *_args, **_kwargs):
        assert "--print-progress" in command
        assert "--no-prints" not in command
        assert _kwargs["timeout"] >= 600
        _kwargs["tick"](0)
        _kwargs["output_line"]("main: processing 'source.wav' ...")
        _kwargs["tick"](1)
        _kwargs["output_line"]("whisper_print_progress_callback: progress =  50%")
        _kwargs["tick"](2)
        _kwargs["output_line"]("whisper_print_progress_callback: progress =  40%")
        _kwargs["tick"](3)
        output_base = Path(command[command.index("--output-file") + 1])
        tokens = [
            {"text": " Hello", "offsets": {"from": 500, "to": 1500}, "p": 0.9},
            {"text": " there", "offsets": {"from": 1600, "to": 3000}, "p": 0.8},
            {"text": ".", "offsets": {"from": 3100, "to": 11990}, "p": 0.9},
            {"text": "[_TT_612]", "offsets": {"from": 12240, "to": 12240}, "p": 0.2},
        ]
        if invalid_edge == "missing-first":
            tokens[0].pop("offsets")
        elif invalid_edge == "missing-last":
            tokens[1].pop("offsets")
        elif invalid_edge == "zero-first":
            tokens[0]["offsets"]["to"] = 500
        elif invalid_edge == "zero-last":
            tokens[1]["offsets"]["from"] = 3000
        elif invalid_edge == "nonfinite":
            tokens[0]["offsets"]["from"] = float("nan")
        elif invalid_edge == "negative":
            tokens[0]["offsets"]["from"] = -1
        elif invalid_edge == "outside-segment":
            tokens[1]["offsets"]["to"] = 13000
        elif invalid_edge == "backwards":
            tokens[1]["offsets"] = {"from": 300, "to": 1000}
        output_base.with_suffix(".json").write_text(
            json.dumps(
                {
                    "result": {"language": "en"},
                    "transcription": [
                        {
                            "offsets": {"from": segment_bounds[0], "to": segment_bounds[1]},
                            "text": "Hello there.",
                            "tokens": tokens,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(analysis_module, "_run_cancellable", write_transcript)
    progress = []
    suggestions, language = manager.transcribe(
        wav_path,
        tmp_path / "output",
        "base",
        "auto",
        HardwareProfile(4, 8 * 1024**3, 6 * 1024**3, "base", "test"),
        lambda message, fraction: progress.append((message, fraction)),
        lambda: False,
    )

    assert language == "en"
    assert suggestions == [AnalysisSuggestion(*expected, "Hello there.", "Whisper", 0.867)]
    assert any("Loading" in message and "elapsed" in message for message, _ in progress)
    assert any("first audio block" in message for message, _ in progress)
    measured = [fraction for message, fraction in progress if "% of audio" in message]
    assert measured == [0.5, 0.5]
    assert progress[-1][1] == 1


def test_analysis_subprocess_streams_output_and_cancels_without_hanging(monkeypatch):
    real_popen = subprocess.Popen
    processes = []
    lines = []
    ticks = []

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(analysis_module.subprocess, "Popen", start)
    with pytest.raises(AnalysisCancelled):
        analysis_module._run_cancellable(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            "Test transcription", lambda: bool(lines),
            output_line=lines.append, tick=ticks.append, timeout=5,
        )
    assert lines == ["ready\n"]
    assert ticks
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_analysis_subprocess_timeout_terminates_silent_worker(monkeypatch):
    real_popen = subprocess.Popen
    processes = []

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(analysis_module.subprocess, "Popen", start)
    with pytest.raises(AnalysisError, match="time limit"):
        analysis_module._run_cancellable(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            "Test transcription", lambda: False, timeout=0.3,
        )
    assert processes[0].poll() is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows descendant file-handle ownership")
@pytest.mark.parametrize("mode", ["cancel", "ambient", "timeout", "callback"])
def test_analysis_subprocess_reaps_descendants_and_readers(tmp_path, mode):
    locked = tmp_path / "owned-staging.wav"
    locked.write_bytes(b"must be released before cancellation completes")
    lines = []
    threads = set(threading.enumerate())
    script = """
import subprocess, sys, time
child = subprocess.Popen([
    sys.executable, "-c",
    "import sys,time; locked=open(sys.argv[1], 'rb'); print('ready', flush=True); time.sleep(30)",
    sys.argv[1],
], stdout=subprocess.PIPE, text=True)
assert child.stdout.readline().strip() == "ready"
print("ready", flush=True)
time.sleep(30)
"""

    def output_line(line):
        lines.append(line)
        if mode == "callback":
            raise ValueError("progress callback failed")

    expected = (
        AnalysisCancelled if mode in {"cancel", "ambient"}
        else AnalysisError if mode == "timeout" else ValueError
    )
    started = time.monotonic()
    with operation_scope(lambda: mode == "ambient" and bool(lines)), pytest.raises(expected):
        analysis_module._run_cancellable(
            [sys.executable, "-c", script, str(locked)],
            "Synthetic analysis tree", lambda: mode == "cancel" and bool(lines),
            output_line=output_line, timeout=1 if mode == "timeout" else 5,
        )
    assert lines == ["ready\n"]
    assert time.monotonic() - started < 10
    assert set(threading.enumerate()) == threads
    locked.unlink()


def test_analysis_subprocess_drains_both_pipes_and_preserves_failure_diagnostics():
    with pytest.raises(AnalysisError, match="diagnostic"):
        analysis_module._run_cancellable(
            [
                sys.executable, "-c",
                "import sys; print('x' * 131072); "
                "print('diagnostic', file=sys.stderr); sys.exit(3)",
            ],
            "Test transcription", lambda: False, timeout=5,
        )
