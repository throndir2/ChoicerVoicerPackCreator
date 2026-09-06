from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

import choicer_voicer_pack_creator.analysis as analysis
import choicer_voicer_pack_creator.separation as separation
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    operation_scope,
    path_leases,
)
from choicer_voicer_pack_creator.separation import (
    SAMPLE_RATE,
    SeparationCancelled,
    SeparationDownloadRequired,
    SeparationError,
    SeparationManager,
)
from choicer_voicer_pack_creator.separation_worker import load_session, separate_stream, worker_main


class SyntheticStems:
    def __init__(self, multiplier: float = 1) -> None:
        self.multiplier = multiplier
        self.inputs: list[np.ndarray] = []

    def run(self, names, inputs):
        assert names == ["stems"]
        mix = inputs["mix"]
        self.inputs.append(mix.copy())
        return [np.stack(
            [mix * 0.1, mix * 0.2, mix * 0.3, mix * 40], axis=1,
        ) * self.multiplier]


@pytest.mark.parametrize("frames", [1, 7, 23, 24, 25, 31, 32, 33, 47, 48, 49, 83, 101])
def test_streaming_exact_frames_edges_overlap_and_nonvocal_stems(tmp_path, frames):
    source = tmp_path / "source.wav"
    output = tmp_path / "out.wav"
    samples = np.linspace(-0.8, 0.8, frames * 2, dtype=np.float32).reshape(frames, 2)
    samples[0] = (0.25, -0.5)
    samples[-1] = (-0.75, 0.8)
    sf.write(source, samples, SAMPLE_RATE, subtype="FLOAT")
    session = SyntheticStems()
    separate_stream(source, output, session, frames, lambda *_: None, lambda: False,
                    chunk_frames=32, overlap_frames=8)
    actual, rate = sf.read(output, dtype="float32", always_2d=True)
    assert rate == SAMPLE_RATE
    assert actual.shape == (frames, 2)
    np.testing.assert_allclose(actual, samples * 0.6, atol=2e-7)
    assert all(item.shape == (1, 2, 32) and item.dtype == np.float32 for item in session.inputs)
    assert len(session.inputs) == (frames + 23) // 24
    assert not (tmp_path / "unscaled.wav").exists()


def test_streaming_applies_one_global_safety_gain_without_local_pumping(tmp_path):
    samples = np.full((101, 2), 0.1, dtype=np.float32)
    samples[-5:] = (1, -1)
    source, output = tmp_path / "source.wav", tmp_path / "out.wav"
    sf.write(source, samples, SAMPLE_RATE, subtype="FLOAT")
    separate_stream(source, output, SyntheticStems(10), len(samples),
                    lambda *_: None, lambda: False, chunk_frames=32, overlap_frames=8)
    actual, _ = sf.read(output, dtype="float32", always_2d=True)
    np.testing.assert_allclose(actual, samples * 0.98, atol=2e-7)
    assert float(np.max(np.abs(actual))) <= 0.980001


def test_streaming_reads_bounded_blocks_and_cancels(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "out.wav"
    sf.write(source, np.zeros((83, 2)), SAMPLE_RATE, subtype="FLOAT")
    session = SyntheticStems()
    with pytest.raises(SeparationCancelled):
        separate_stream(source, output, session, 83, lambda *_: None,
                        lambda: len(session.inputs) >= 2, chunk_frames=32, overlap_frames=8)
    assert len(session.inputs) == 2
    assert not output.exists()
    assert not (tmp_path / "unscaled.wav").exists()


@pytest.mark.parametrize("failure", ["shape", "nan"])
def test_bad_model_output_is_rejected(tmp_path, failure):
    source, output = tmp_path / "source.wav", tmp_path / "out.wav"
    sf.write(source, np.ones((32, 2)) * 0.2, SAMPLE_RATE, subtype="FLOAT")

    class BadSession:
        def run(self, *_):
            return [np.full((1, 3 if failure == "shape" else 4, 2, 32), np.nan)]

    with pytest.raises(SeparationError, match="unexpected stem layout|non-finite"):
        separate_stream(source, output, BadSession(), 32, lambda *_: None, lambda: False,
                        chunk_frames=32, overlap_frames=8)
    assert not output.exists()


@pytest.fixture
def manager(tmp_path):
    manager = SeparationManager(tmp_path / "data")
    payload = b"small synthetic pinned model"
    source = tmp_path / "model-source.onnx"
    source.write_bytes(payload)
    manager.manifest["model"].update({
        "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        "url": source.as_uri(),
    })
    return manager


@pytest.fixture
def source_video(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic source video")
    return source


@pytest.mark.parametrize("cache", ["missing", "wrong-size", "wrong-hash"])
def test_missing_or_invalid_model_needs_consent_before_network(
    manager, monkeypatch, cache, source_video,
):
    if cache != "missing":
        manager.model_path.parent.mkdir(parents=True)
        manager.model_path.write_bytes(
            b"x" if cache == "wrong-size" else b"x" * manager.model_download_bytes,
        )
    original = manager.model_path.read_bytes() if manager.model_path.exists() else None

    def no_download(*_args, **_kwargs):
        pytest.fail("No download may occur before consent")

    monkeypatch.setattr(separation, "download_verified", no_download)
    with pytest.raises(SeparationDownloadRequired):
        manager.generate(None, source_video, progress=lambda *_: None, cancelled=lambda: False)
    assert (manager.model_path.read_bytes() if manager.model_path.exists() else None) == original
    assert not list((manager.data_root / "separation-jobs").iterdir())


def test_authorized_cache_install_offline_reuse_and_same_size_corruption(manager, tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    manager._ensure_model(job, True, lambda *_: None, lambda: False)
    assert manager.model_path.read_bytes() == b"small synthetic pinned model"
    assert (manager.model_path.parent / "StemSplit-MIT.txt").is_file()
    assert (manager.model_path.parent / "Demucs-MIT.txt").is_file()
    assert (manager.model_path.parent / "backing-separation.json").is_file()

    def offline(*_args, **_kwargs):
        pytest.fail("Valid cached model must work offline")

    monkeypatch.setattr(separation, "download_verified", offline)
    assert manager._ensure_model(job, False, lambda *_: None, lambda: False) == manager.model_path
    manager.model_path.write_bytes(b"z" * manager.model_download_bytes)
    with pytest.raises(SeparationDownloadRequired):
        manager._ensure_model(job, False, lambda *_: None, lambda: False)


def test_failed_authorized_repair_preserves_invalid_cache_and_assets(
    manager, monkeypatch, source_video,
):
    manager.model_path.parent.mkdir(parents=True)
    manager.model_path.write_bytes(b"old-invalid-cache")
    original = manager.data_root / "original.wav"
    original.write_bytes(b"existing asset")

    def failure(*args):
        destination = args[1]
        destination.write_bytes(b"unverified")
        raise analysis.AnalysisError("download hash mismatch")

    monkeypatch.setattr(separation, "download_verified", failure)
    with pytest.raises(SeparationError, match="hash mismatch"):
        manager.generate(None, source_video, allow_download=True,
                         progress=lambda *_: None, cancelled=lambda: False)
    assert original.read_bytes() == b"existing asset"
    assert manager.model_path.read_bytes() == b"old-invalid-cache"
    assert not list((manager.data_root / "separation-jobs").iterdir())


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_concurrent_installs_share_setup_and_keep_job_staging(
    manager, tmp_path, monkeypatch, cancel_waiter,
):
    second = SeparationManager(manager.data_root)
    second.manifest = manager.manifest
    jobs = [tmp_path / "first-job", tmp_path / "second-job"]
    for job in jobs:
        job.mkdir()
    downloading = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    cancelled = threading.Event()
    destinations = []
    download = separation.download_verified
    replace = separation.os.replace

    def synchronized_download(*args):
        destinations.append(args[1])
        downloading.set()
        assert release.wait(timeout=10)
        return download(*args)

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    def check_publication(source, destination):
        if Path(destination) == manager.model_path:
            assert separation.verify_model_file(
                Path(source), manager.model_download_bytes,
                manager.manifest["model"]["sha256"], lambda: False,
            )
        replace(source, destination)

    monkeypatch.setattr(separation, "download_verified", synchronized_download)
    monkeypatch.setattr(separation.os, "replace", check_publication)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(manager._ensure_model, jobs[0], True, lambda *_: None, lambda: False)
        try:
            assert downloading.wait(timeout=5)
            second_result = executor.submit(
                second._ensure_model, jobs[1], False, progress, cancelled.is_set,
            )
            assert waiting.wait(timeout=5)
            if cancel_waiter:
                cancelled.set()
                with pytest.raises(SeparationCancelled):
                    second_result.result(timeout=3)
                assert not first.done()
                assert not list(jobs[1].iterdir())
        finally:
            release.set()
        assert first.result(timeout=5) == manager.model_path
        if not cancel_waiter:
            assert second_result.result(timeout=5) == manager.model_path
    assert destinations == [jobs[0] / "htdemucs.onnx"]
    assert manager._verified_model(lambda *_: None, lambda: False)
    assert not list(manager.model_path.parent.glob("*.partial"))
    for filename in ("Demucs-MIT.txt", "StemSplit-MIT.txt", "backing-separation.json"):
        assert (manager.model_path.parent / filename).read_bytes() == (
            manager.manifest_path.parent / filename
        ).read_bytes()


@pytest.mark.parametrize("other_valid", [True, False])
def test_locked_concurrent_model_is_reused_only_after_verification(
    manager, tmp_path, monkeypatch, other_valid,
):
    job = tmp_path / "job"
    job.mkdir()
    replace = separation.os.replace

    def another_process_won(source, destination):
        if Path(destination) == manager.model_path:
            payload = Path(source).read_bytes()
            manager.model_path.write_bytes(payload if other_valid else b"x" * len(payload))
            raise PermissionError("another inference process opened the published model")
        replace(source, destination)

    monkeypatch.setattr(separation.os, "replace", another_process_won)
    if other_valid:
        assert manager._ensure_model(job, True, lambda *_: None, lambda: False) == manager.model_path
        assert manager._verified_model(lambda *_: None, lambda: False)
        assert not (job / "htdemucs.onnx").exists()
    else:
        with pytest.raises(PermissionError):
            manager._ensure_model(job, True, lambda *_: None, lambda: False)


def test_hash_verification_is_cancellable(manager, tmp_path):
    manager.model_path.parent.mkdir(parents=True)
    manager.model_path.write_bytes(b"small synthetic pinned model")
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(SeparationCancelled):
        manager._verified_model(lambda *_: None, cancelled)


def test_separation_checks_ambient_cancellation(manager, tmp_path):
    assert issubclass(SeparationCancelled, OperationCancelled)
    cancelled = threading.Event()
    with operation_scope(cancelled.is_set):
        cancelled.set()
        with pytest.raises(SeparationCancelled):
            separation.check_cancel(lambda: False)
        with pytest.raises(SeparationCancelled):
            manager._ensure_model(tmp_path, True, lambda *_: None, lambda: False)
        with pytest.raises(SeparationCancelled):
            manager.generate(
                None, Path("unused.mp4"), progress=lambda *_: None, cancelled=lambda: False,
            )
    assert not manager.data_root.exists()


def test_separation_model_setup_does_not_commit_the_enclosing_generation(manager, tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    committed = []
    with operation_scope(committed=lambda: committed.append(True)):
        manager._ensure_model(job, True, lambda *_: None, lambda: False)
    assert committed == []


def _mock_preparation(manager, monkeypatch):
    monkeypatch.setattr(manager, "_ensure_model", lambda *_: manager.model_path)
    monkeypatch.setattr(manager, "_decode", lambda *_: 83)


def _write_successful_result(command):
    request_path = Path(command[-1])
    job = request_path.parent
    request = json.loads(request_path.read_text())
    sf.write(job / "backing.wav", np.ones((83, 2)) * 0.25, SAMPLE_RATE, subtype="PCM_24")
    separation.write_json_atomic(job / "status.json", {
        "job_id": request["job_id"], "state": "succeeded",
        "message": "Done", "progress": 1.0,
    })


def test_generation_publishes_only_verified_unique_durable_assets(manager, monkeypatch, source_video):
    _mock_preparation(manager, monkeypatch)

    def run(command, _description, _cancelled, *, tick):
        _write_successful_result(command)
        tick(0)

    monkeypatch.setattr(separation, "_run_cancellable", run)
    paths = [manager.generate(None, source_video, progress=lambda *_: None,
                              cancelled=lambda: False) for _ in range(2)]
    assert paths[0] != paths[1]
    for path in paths:
        assert path.is_relative_to(manager.data_root / "backing-tracks")
        separation.validate_audio(path, 83, lambda: False)
    assert not list((manager.data_root / "separation-jobs").iterdir())


@pytest.mark.parametrize("change", ["modify", "replace", "delete"])
def test_generation_rejects_changed_source_before_publishing(
    manager, monkeypatch, source_video, change,
):
    _mock_preparation(manager, monkeypatch)

    def run(command, _description, _cancelled, *, tick):
        _write_successful_result(command)

    def progress(message, _fraction):
        if not message.startswith("Publishing the verified backing track"):
            return
        if change == "modify":
            source_video.write_bytes(b"externally changed source video")
        elif change == "replace":
            replacement = source_video.with_suffix(".replacement")
            replacement.write_bytes(source_video.read_bytes())
            replacement.replace(source_video)
        else:
            source_video.unlink()

    monkeypatch.setattr(separation, "_run_cancellable", run)
    with pytest.raises(SourceChangedError):
        manager.generate(None, source_video, progress=progress, cancelled=lambda: False)
    assert not (manager.data_root / "backing-tracks").exists()
    assert not list((manager.data_root / "separation-jobs").iterdir())


def test_standalone_generation_holds_source_read_lease_until_publication(
    manager, monkeypatch, source_video,
):
    _mock_preparation(manager, monkeypatch)
    processing = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    cancel_writer = threading.Event()

    def run(command, _description, _cancelled, *, tick):
        processing.set()
        assert release.wait(timeout=10)
        _write_successful_result(command)

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    def replace_video():
        with operation_scope(cancel_writer.is_set, progress), path_leases(
            write_paths=(source_video,),
        ):
            source_video.write_bytes(b"unexpected replacement")

    monkeypatch.setattr(separation, "_run_cancellable", run)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(
            manager.generate, None, source_video,
            progress=lambda *_: None, cancelled=lambda: False,
        )
        try:
            assert processing.wait(timeout=5)
            writer = executor.submit(replace_video)
            assert waiting.wait(timeout=5)
            cancel_writer.set()
            with pytest.raises(OperationCancelled):
                writer.result(timeout=3)
            assert source_video.read_bytes() == b"synthetic source video"
        finally:
            cancel_writer.set()
            release.set()
        output = pending.result(timeout=5)
    separation.validate_audio(output, 83, lambda: False)
    with path_leases(write_paths=(source_video,)):
        source_video.write_bytes(b"replacement after generation")


def test_standalone_generation_source_lease_wait_is_cancellable(manager, source_video):
    waiting = threading.Event()
    cancelled = threading.Event()

    def progress(message, _fraction):
        if "Waiting" in message:
            waiting.set()

    with path_leases(write_paths=(source_video,)), ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            manager.generate, None, source_video, progress=progress, cancelled=cancelled.is_set,
        )
        try:
            assert waiting.wait(timeout=5)
        finally:
            cancelled.set()
        with pytest.raises(SeparationCancelled):
            pending.result(timeout=3)
    assert not manager.data_root.exists()


def test_missing_generation_source_fails_before_component_setup(manager, tmp_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("A missing source must not start component setup")

    monkeypatch.setattr(manager, "_ensure_model", unexpected)
    with pytest.raises(SeparationError, match="generation failed"):
        manager.generate(
            None, tmp_path / "missing.mp4", allow_download=True,
            progress=lambda *_: None, cancelled=lambda: False,
        )
    assert not manager.data_root.exists()


@pytest.mark.parametrize("failure", [
    "cancel", "process", "missing-status", "failed-status", "wrong-job",
    "missing-output", "short-output", "nan-output", "clipped-output",
])
def test_failure_or_cancel_never_publishes_or_modifies_existing_assets(
    manager, monkeypatch, failure, source_video,
):
    _mock_preparation(manager, monkeypatch)
    manager.data_root.mkdir(parents=True)
    original = manager.data_root / "previous-backing.wav"
    original.write_bytes(b"preserve me")

    def run(command, _description, _cancelled, *, tick):
        job = Path(command[-1]).parent
        if failure == "cancel":
            raise analysis.AnalysisCancelled("cancel")
        if failure == "process":
            raise analysis.AnalysisError("worker launch failed")
        if failure != "missing-output":
            samples = np.ones((82 if failure == "short-output" else 83, 2)) * (
                np.nan if failure == "nan-output" else 2 if failure == "clipped-output" else 0.25
            )
            sf.write(job / "backing.wav", samples, SAMPLE_RATE, subtype="FLOAT")
        if failure != "missing-status":
            separation.write_json_atomic(job / "status.json", {
                "job_id": "wrong" if failure == "wrong-job" else job.name,
                "state": "failed" if failure == "failed-status" else "succeeded",
                "message": "synthetic failure", "progress": 1.0,
            })

    monkeypatch.setattr(separation, "_run_cancellable", run)
    with pytest.raises(SeparationCancelled if failure == "cancel" else SeparationError):
        manager.generate(None, source_video, progress=lambda *_: None, cancelled=lambda: False)
    assert original.read_bytes() == b"preserve me"
    assert not (manager.data_root / "backing-tracks").exists()
    assert not list((manager.data_root / "separation-jobs").iterdir())


@pytest.mark.parametrize("entrypoint", ["module", "frozen-hook"])
def test_worker_dispatch_runs_without_qt_or_console(tmp_path, entrypoint):
    job = tmp_path / "worker-job"
    job.mkdir()
    request = job / "request.json"
    separation.write_json_atomic(request, {"version": 1, "job_id": job.name, "smoke_test": True})
    code = """
import importlib.abc, runpy, sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('PySide6'):
            raise AssertionError('Qt was imported by the worker')
sys.meta_path.insert(0, NoQt())
entrypoint = sys.argv[2]
sys.argv = ['choicer_voicer_pack_creator', '--separate-audio', sys.argv[1]]
sys.stdout = sys.stderr = None
if entrypoint == 'module':
    runpy.run_module('choicer_voicer_pack_creator', run_name='__main__')
else:
    runpy.run_module('scripts.separation_runtime_hook', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(request), entrypoint], timeout=30, check=False,
    )
    assert result.returncode == 0
    status = json.loads((job / "status.json").read_text())
    assert status["state"] == "succeeded"
    report = json.loads((job / "smoke.json").read_text())
    assert report["qt_imported"] is False
    assert report["frames"] == 83
    assert report["onnxruntime"] == "1.26.0"


def test_worker_reports_failure_atomically_and_removes_partial_output(tmp_path):
    job = tmp_path / "bad-worker"
    job.mkdir()
    request = job / "request.json"
    (job / "backing.wav").write_bytes(b"partial")
    separation.write_json_atomic(request, {"version": 1, "job_id": job.name, "frames": -1})
    assert worker_main(request) == 1
    status = json.loads((job / "status.json").read_text())
    assert status["state"] == "failed"
    assert "frame count" in status["message"]
    assert not (job / "backing.wav").exists()
    assert not list(job.glob("*.partial"))


def test_worker_rechecks_pinned_model_before_native_inference(tmp_path, monkeypatch):
    import onnxruntime

    model = tmp_path / "htdemucs.onnx"
    model.write_bytes(b"corrupt")

    def no_load(*_args, **_kwargs):
        pytest.fail("Unverified models must never enter the native inference engine")

    monkeypatch.setattr(onnxruntime, "InferenceSession", no_load)
    with pytest.raises(SeparationError, match="changed or is invalid"):
        load_session(model)


def test_atomic_status_retries_windows_reader_lock(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    status.write_text('{"old": true}')
    replace = separation.os.replace
    attempts = 0

    def locked(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            assert json.loads(status.read_text()) == {"old": True}
            raise PermissionError("reader briefly owns the previous status")
        replace(source, destination)

    monkeypatch.setattr(separation.os, "replace", locked)
    separation.write_json_atomic(status, {"state": "succeeded"})
    assert attempts == 3
    assert json.loads(status.read_text()) == {"state": "succeeded"}
    assert not status.with_suffix(".partial").exists()


def test_cancel_terminates_and_waits_for_only_the_separation_process(
    manager, monkeypatch, tmp_path, source_video,
):
    _mock_preparation(manager, monkeypatch)
    launched = []
    real_popen = analysis.subprocess.Popen
    real_run = analysis._run_cancellable
    ready = tmp_path / "worker-ready"

    def record_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    def run(_command, description, cancelled, *, tick):
        return real_run([
            sys.executable, "-c",
            "import pathlib,time,sys;pathlib.Path(sys.argv[1]).write_text('ready');time.sleep(30)",
            str(ready),
        ], description, cancelled, tick=tick)

    monkeypatch.setattr(analysis.subprocess, "Popen", record_popen)
    monkeypatch.setattr(separation, "_run_cancellable", run)
    with pytest.raises(SeparationCancelled):
        manager.generate(None, source_video, progress=lambda *_: None,
                         cancelled=lambda: ready.exists())
    assert len(launched) == 1
    assert launched[0].poll() is not None
    assert not list((manager.data_root / "separation-jobs").iterdir())


@pytest.mark.integration
@pytest.mark.parametrize("channels", [1, 2, 6])
@pytest.mark.parametrize("source_start", [0, 5])
def test_decode_matches_video_timeline_with_delayed_short_audio(tmp_path, channels, source_start):
    import shutil

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg is not installed")
    video = tmp_path / "delayed.mkv"
    subprocess.run([
        ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi",
        "-i", "color=c=black:s=32x32:r=25:d=2", "-itsoffset", "0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
        "-map", "0:v", "-map", "1:a", "-c:v", "ffv1", "-c:a", "pcm_s16le",
        "-ac", str(channels), "-output_ts_offset", str(source_start), str(video),
    ], check=True, timeout=30)
    probe = json.loads(subprocess.run([
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video),
    ], check=True, capture_output=True, text=True, timeout=30).stdout)
    assert float(probe["format"]["start_time"]) == source_start
    audio = next(item for item in probe["streams"] if item["codec_type"] == "audio")
    assert float(audio["start_time"]) == source_start + 0.5
    manager = SeparationManager(tmp_path / "data")
    decoded = tmp_path / "decoded.wav"
    frames = manager._decode(SimpleNamespace(ffmpeg=ffmpeg, ffprobe=ffprobe), video, decoded,
                             lambda *_: None, lambda: False)
    samples, rate = sf.read(decoded, dtype="float32", always_2d=True)
    assert frames == round(float(probe["format"]["duration"]) * SAMPLE_RATE)
    assert samples.shape == (frames, 2)
    assert rate == SAMPLE_RATE
    assert np.max(np.abs(samples[:round(0.49 * rate)])) < 1e-5
    assert np.max(np.abs(samples[round(0.55 * rate):round(0.95 * rate)])) > 0.01
    assert np.max(np.abs(samples[round(1.02 * rate):])) < 1e-5


def test_manifest_pins_model_and_retains_adaptation_licenses():
    manifest_path = separation.default_manifest_path()
    manifest = json.loads(manifest_path.read_text())
    model = manifest["model"]
    assert model["revision"] == "d54ed9eb60e258ea82131c6ee14578628816456a"
    assert model["revision"] in model["url"]
    assert model["bytes"] == 316446953
    assert model["sha256"] == "68d0bf16428ef66e692cdff8a9ccf28f1ef3f69440d57e58605a4cc55fcc5e74"
    assert manifest["backing_stems"] == ["drums", "bass", "other"]
    assert manifest["overlap"] == 0.25
    assert model["revision"] in manifest["provenance"]["reference_inference"]
    for filename in manifest["provenance"]["licenses"]:
        text = (manifest_path.parent / filename).read_text()
        assert "Permission is hereby granted" in text
        assert "Copyright (c)" in text
