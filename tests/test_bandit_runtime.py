from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from choicer_voicer_pack_creator import bandit_runtime as bandit
from choicer_voicer_pack_creator import separation
from choicer_voicer_pack_creator._bandit import load_manifest
from choicer_voicer_pack_creator.export_resources import (
    ExportResourceBudget,
    ResourceError,
    ResourceSnapshot,
    WorkEstimate,
)
from choicer_voicer_pack_creator.operations import operation_scope
from choicer_voicer_pack_creator.separation_types import (
    KEEP_SINGING,
    REMOVE_ALL_VOCALS,
    validate_backing_mode,
)
from choicer_voicer_pack_creator.separation_worker import worker_main


def fake_predict(block):
    return {"speech": block * 20, "music": block * 0.4, "sfx": block * 0.2}


@pytest.mark.parametrize("failure", [None, "missing", "shape", "nan", "precision", "device"])
def test_predictor_uses_inference_mode_float32_sequential_channels(monkeypatch, failure):
    state = {"inference": False}

    class Tensor(np.ndarray):
        device = SimpleNamespace(type="cpu")

        def numpy(self):
            return np.asarray(self)

    @contextmanager
    def inference_mode():
        state["inference"] = True
        try:
            yield
        finally:
            state["inference"] = False

    fake_torch = SimpleNamespace(
        Tensor=Tensor, float32=np.dtype("float32"), inference_mode=inference_mode,
        from_numpy=lambda array: array.view(Tensor), isfinite=np.isfinite,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    calls = []
    block = np.column_stack((np.arange(32), -np.arange(32))).astype(np.float32)

    def model(batch):
        assert state["inference"]
        audio = batch["mixture"]["audio"]
        assert audio.shape == (1, 1, 32) and audio.dtype == np.float32
        assert not np.shares_memory(audio, block)
        calls.append(audio.copy())
        estimates = {
            stem: {"audio": audio * gain}
            for stem, gain in (("speech", 10), ("music", 0.4), ("sfx", 0.2))
        }
        if failure == "missing":
            del estimates["speech"]
        elif failure == "shape":
            estimates["speech"]["audio"] = audio[:, :, :-1]
        elif failure == "nan":
            estimates["speech"]["audio"][0, 0, 0] = np.nan
        elif failure == "precision":
            estimates["speech"]["audio"] = audio.astype(np.float64)
        elif failure == "device":
            estimates["speech"]["audio"].device = SimpleNamespace(type="cuda")
        return {"estimates": estimates}

    predictor = bandit.make_predictor(model)
    if failure:
        with pytest.raises(separation.SeparationError, match="stem|invalid|finite"):
            predictor(block)
    else:
        result = predictor(block)
        assert len(calls) == 2
        for channel in range(2):
            np.testing.assert_array_equal(calls[channel][0, 0], block[:, channel])
        np.testing.assert_array_equal(result["music"], block * 0.4)
        np.testing.assert_array_equal(result["sfx"], block * 0.2)
    assert not state["inference"]


@pytest.mark.parametrize("value", [None, "", "automatic", [], {}, True, 0])
def test_invalid_modes_rejected_at_manager_and_validator(tmp_path, value):
    with pytest.raises(ValueError, match="mode"):
        validate_backing_mode(value)
    with pytest.raises(ValueError, match="mode"):
        separation.SeparationManager(tmp_path, mode=value)


def test_mode_is_fixed_and_default_backend_unchanged(tmp_path):
    default = separation.SeparationManager(tmp_path)
    manager = separation.SeparationManager(tmp_path, mode=KEEP_SINGING)
    assert default.mode == REMOVE_ALL_VOCALS
    assert default.sample_rate == 44100
    assert manager.mode == KEEP_SINGING
    assert manager.sample_rate == 48000
    assert manager.model_download_bytes == 446680129
    assert manager.model_path.name == "bandit-combined.ckpt"
    assert manager.manifest["model"]["md5"] == "d04760e77bb947668d8f5582d36b45a0"
    with pytest.raises(AttributeError):
        manager.mode = REMOVE_ALL_VOCALS


@pytest.mark.parametrize("key,value", [
    ("mode", "remove_all_vocals"), ("sample_rate", 44100), ("chunk_frames", 24000),
    ("hop_frames", 96000), ("stems", ["music", "sfx"]), ("parameters", {}),
    ("model", {"filename": "..\\other.ckpt"}), ("version", True),
    ("source_provenance_file", "..\\unrelated.json"),
])
def test_manifest_rejects_alternative_configuration(tmp_path, key, value):
    manifest = load_manifest()
    manifest[key] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="configuration"):
        load_manifest(path)


@pytest.mark.parametrize("frames", [1, 7, 8, 31, 32, 33, 41, 65, 101])
def test_hann_stream_matches_reference_ola_and_preserves_edges(tmp_path, frames):
    from scripts.compare_separation import separate_chunks

    samples = np.linspace(-0.8, 0.8, frames * 2, dtype=np.float32).reshape(-1, 2)
    samples[0] = (0.6, -0.7)
    source, output = tmp_path / "source.wav", tmp_path / "backing.wav"
    sf.write(source, samples, 48000, subtype="FLOAT")
    bandit.separate_stream(source, output, fake_predict, frames, lambda *_: None, lambda: False,
                           chunk_frames=32, hop_frames=8)
    reference = separate_chunks(
        samples, lambda x: {"backing": x * 0.4 + x * 0.2, "removed": x * 20},
        32, 24, window_kind="hann",
    )["backing"]
    actual, rate = sf.read(output, dtype="float32", always_2d=True)
    assert rate == 48000 and actual.shape == (frames, 2)
    np.testing.assert_allclose(actual, reference, atol=1.3e-7, rtol=1e-6)
    assert not (tmp_path / "unscaled-bandit.wav").exists()


@pytest.mark.parametrize("failure", ["missing", "extra", "shape", "nan-speech", "float64"])
def test_all_stems_are_required_finite_float32(tmp_path, failure):
    source, output = tmp_path / "source.wav", tmp_path / "backing.wav"
    sf.write(source, np.zeros((41, 2), np.float32), 48000, subtype="FLOAT")

    def predict(block):
        result = fake_predict(block)
        if failure == "missing":
            del result["speech"]
        elif failure == "extra":
            result["other"] = block
        elif failure == "shape":
            result["sfx"] = block[:-1]
        elif failure == "nan-speech":
            result["speech"][0, 0] = np.nan
        else:
            result["speech"] = result["speech"].astype(np.float64)
        return result

    with pytest.raises(separation.SeparationError, match="stem|invalid|finite"):
        bandit.separate_stream(source, output, predict, 41, lambda *_: None, lambda: False,
                               chunk_frames=32, hop_frames=8)
    assert not output.exists()
    assert not (tmp_path / "unscaled-bandit.wav").exists()


def test_global_gain_not_window_normalization(tmp_path):
    samples = np.full((101, 2), 0.1, np.float32)
    samples[-5:] = (4, -4)
    source, output = tmp_path / "source.wav", tmp_path / "backing.wav"
    sf.write(source, samples, 48000, subtype="FLOAT")
    bandit.separate_stream(source, output, fake_predict, 101, lambda *_: None, lambda: False,
                           chunk_frames=32, hop_frames=8)
    actual, _ = sf.read(output, dtype="float32", always_2d=True)
    np.testing.assert_allclose(actual, samples * (0.98 / 4), atol=2e-7)


def test_cancellation_cleans_job_owned_intermediates(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "backing.wav"
    sf.write(source, np.ones((101, 2), np.float32), 48000, subtype="FLOAT")
    calls = []

    def predict(block):
        calls.append(True)
        return fake_predict(block)

    with pytest.raises(separation.SeparationCancelled):
        bandit.separate_stream(source, output, predict, 101, lambda *_: None,
                               lambda: len(calls) > 1, chunk_frames=32, hop_frames=8)
    assert len(calls) == 2
    assert source.exists()
    assert not output.exists()
    assert not (tmp_path / "unscaled-bandit.wav").exists()


def test_longer_than_120_seconds_has_bounded_reads_and_full_output(tmp_path):
    frames = 121 * 48000 + 11
    source, output = tmp_path / "source.wav", tmp_path / "backing.wav"
    samples = np.tile(np.array([0.2, -0.4], np.float32), (65536, 1))
    with sf.SoundFile(source, "w", samplerate=48000, channels=2, subtype="FLOAT") as stream:
        for start in range(0, frames, len(samples)):
            stream.write(samples[:min(len(samples), frames - start)])
    reads = []
    with sf.SoundFile(source) as stream:
        class BoundedSource:
            def seek(self, start):
                stream.seek(start)

            def read(self, count, **kwargs):
                assert count == 384000
                reads.append(count)
                return stream.read(count, **kwargs)

        count = 0
        for block in bandit.overlap_add_blocks(
            BoundedSource(), fake_predict, frames, lambda *_: None, lambda: False,
        ):
            assert len(block) <= 48000
            np.testing.assert_allclose(block, np.broadcast_to([0.12, -0.24], block.shape), atol=1e-7)
            count += len(block)
    assert count == frames and len(reads) == 122
    assert not output.exists()


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 44100, 47999, 48000, 96000, 192000])
@pytest.mark.parametrize("block_frames", [1, 37, 1000])
def test_bounded_resampler_matches_whole_scipy_exactly(tmp_path, rate, block_frames):
    from scipy.signal import resample_poly

    rng = np.random.default_rng(143)
    samples = rng.uniform(-0.8, 0.8, (203, 2)).astype(np.float32)
    source, output = tmp_path / "source.wav", tmp_path / "resampled.wav"
    sf.write(source, samples, rate, subtype="FLOAT")
    from math import gcd

    divisor = gcd(rate, 48000)
    reference = resample_poly(samples, 48000 // divisor, rate // divisor, axis=0)
    bandit.resample_stream(source, output, len(reference), lambda: False, block_frames=block_frames)
    actual, actual_rate = sf.read(output, dtype="float32", always_2d=True)
    assert actual_rate == 48000
    np.testing.assert_array_equal(actual, reference)


def test_resampler_phase_boundaries_and_explicit_timeline_padding(tmp_path):
    from scipy.signal import resample_poly

    samples = np.zeros((20000, 2), np.float32)
    samples[[0, 146, 147, 148, 3999, 4000, 19999], 0] = 0.7
    samples[:, 1] = np.sin(np.arange(len(samples), dtype=np.float32) * 0.157)
    reference = resample_poly(samples, 160, 147, axis=0)
    source, output = tmp_path / "source.wav", tmp_path / "resampled.wav"
    sf.write(source, samples, 44100, subtype="FLOAT")
    bandit.resample_stream(source, output, len(reference) + 7, lambda: False, block_frames=251)
    actual, _ = sf.read(output, dtype="float32", always_2d=True)
    np.testing.assert_array_equal(actual[:-7], reference)
    assert not np.any(actual[-7:])


def test_bandit_cache_consent_hashes_notices_and_offline_reuse(tmp_path, monkeypatch):
    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    payload = b"pinned combined model stand-in"
    download = tmp_path / "source.ckpt"
    download.write_bytes(payload)
    manager.manifest["model"].update(
        url=download.as_uri(), bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
    )
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(separation.SeparationDownloadRequired):
        manager._ensure_model(job, False, lambda *_: None, lambda: False)
    manager._ensure_model(job, True, lambda *_: None, lambda: False)
    assert manager.model_path.read_bytes() == payload
    for name in (*manager.manifest["notice_files"], manager.manifest_path.name):
        assert (manager.model_path.parent / name).is_file()
    provenance = manager.model_path.parent / "BandIt-provenance.json"
    expected_provenance = (
        manager.manifest_path.parent.parent / "_bandit" / "provenance.json"
    ).read_bytes()
    assert provenance.read_bytes() == expected_provenance
    provenance.write_bytes(b"outdated provenance")
    monkeypatch.setattr(separation, "download_verified", lambda *_: pytest.fail("Must work offline"))
    assert manager._ensure_model(job, False, lambda *_: None, lambda: False) == manager.model_path
    assert provenance.read_bytes() == expected_provenance
    manager.manifest["model"]["md5"] = "0" * 32
    with pytest.raises(separation.SeparationDownloadRequired):
        manager._ensure_model(job, False, lambda *_: None, lambda: False)


def test_failed_md5_repair_preserves_prior_cache_and_assets(tmp_path, monkeypatch):
    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    previous = tmp_path / "previous.wav"
    previous.write_bytes(b"previous backing")
    job = tmp_path / "job"
    job.mkdir()
    payload = b"right SHA but wrong published MD5"
    manager.manifest["model"].update(
        bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(), md5="0" * 32,
    )
    manager.model_path.parent.mkdir(parents=True)
    manager.model_path.write_bytes(b"original invalid cache")

    def download(_url, destination, *_args):
        destination.write_bytes(payload)
        return destination

    monkeypatch.setattr(separation, "download_verified", download)
    with pytest.raises(separation.SeparationError, match="MD5"):
        manager._ensure_model(job, True, lambda *_: None, lambda: False)
    assert manager.model_path.read_bytes() == b"original invalid cache"
    assert previous.read_bytes() == b"previous backing"


def test_low_disk_blocks_before_source_decode(tmp_path, monkeypatch):
    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    monkeypatch.setattr(separation.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(separation.SeparationError, match="staging space"):
        manager._check_disk(tmp_path, 1024**3)


@pytest.mark.parametrize("mode", [None, "unrecognized", {}, False])
def test_worker_rejects_explicit_bad_mode_before_runtime(tmp_path, monkeypatch, mode):
    job = tmp_path / "job"
    job.mkdir()
    request = job / "request.json"
    request.write_text(json.dumps({"version": 1, "job_id": job.name, "mode": mode, "frames": 1}))
    monkeypatch.setattr(bandit, "load_cpu_model", lambda *_: pytest.fail("No model load"))
    assert worker_main(request) == 1
    assert "mode" in json.loads((job / "status.json").read_text())["message"]


def test_bandit_worker_dispatch_has_no_htdemucs_fallback(tmp_path, monkeypatch):
    import choicer_voicer_pack_creator.separation_worker as worker

    job = tmp_path / "job"
    job.mkdir()
    request = job / "request.json"
    request.write_text(json.dumps({
        "version": 1, "job_id": job.name, "mode": KEEP_SINGING,
        "frames": 1, "model": "verified.ckpt", "threads": 1,
    }))
    monkeypatch.setattr(worker, "load_session", lambda *_: pytest.fail("No fallback"))

    def fail(path, threads):
        assert threads == 1 and path == Path("verified.ckpt")
        raise separation.SeparationError("synthetic CPU failure")

    monkeypatch.setattr(bandit, "load_cpu_model", fail)
    assert worker_main(request) == 1
    assert "synthetic CPU failure" in json.loads((job / "status.json").read_text())["message"]


@pytest.mark.parametrize("threads", [None, True, 0, 3, 8])
def test_invalid_thread_budget_rejected_before_torch(threads):
    with pytest.raises(separation.SeparationError, match="thread count"):
        bandit.configure_cpu(threads)


@pytest.mark.parametrize("foreign_import", [False, True])
def test_native_smoke_report_contract_with_fake_inference(tmp_path, monkeypatch, foreign_import):
    from choicer_voicer_pack_creator._bandit import MODEL_PARAMETERS

    class FakeModel:
        def __init__(self, **parameters):
            assert parameters == MODEL_PARAMETERS

        def eval(self):
            return self

        def to(self, device):
            assert device == "cpu"
            return self

    module = ModuleType("choicer_voicer_pack_creator._bandit.models.bandit.bandit")
    module.Bandit = FakeModel
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, "torchaudio", SimpleNamespace(__version__="2.8.0+cpu"))
    fake_torch = SimpleNamespace(
        __version__="2.8.0+cpu", version=SimpleNamespace(cuda=None),
        manual_seed=lambda seed: None, get_num_threads=lambda: 1,
        get_num_interop_threads=lambda: 1,
    )
    monkeypatch.setattr(bandit, "configure_cpu", lambda threads: fake_torch)
    modules = {}

    def predictor(_model):
        if foreign_import:
            modules["ctranslate2"] = object()
        return fake_predict

    monkeypatch.setattr(bandit, "make_predictor", predictor)
    monkeypatch.setattr(bandit, "sys", SimpleNamespace(modules=modules))
    if foreign_import:
        with pytest.raises(separation.SeparationError, match="CTranslate2"):
            bandit.smoke_test(tmp_path)
        return
    report = bandit.smoke_test(tmp_path)
    assert report["channels"] == 2 and report["frames"] == 4097
    assert report["sample_rate"] == 48000
    assert report["stems"] == ["speech", "music", "sfx"]
    assert report["finite"] is True and report["qt_imported"] is False
    assert report["cuda"] is None
    assert report["ctranslate2_imported"] is False
    assert report["threads"] == report["interop_threads"] == 1


@pytest.mark.parametrize("module_name", ["ctranslate2", "ctranslate2._ext"])
def test_bandit_smoke_rejects_foreign_backend_before_native_import(tmp_path, monkeypatch, module_name):
    monkeypatch.setattr(bandit, "sys", SimpleNamespace(modules={module_name: object()}))
    monkeypatch.setattr(bandit, "configure_cpu", lambda *_: pytest.fail("Must not import Torch"))
    with pytest.raises(separation.SeparationError, match="CTranslate2"):
        bandit.smoke_test(tmp_path)


def test_import_runtime_and_modes_does_not_import_torch_or_qt():
    subprocess.run([
        sys.executable, "-c",
        "import sys; from choicer_voicer_pack_creator import bandit_runtime, separation_types; "
        "assert not any(n.startswith(('torch','torchaudio','PySide6','librosa','scipy')) "
        "for n in sys.modules)",
    ], check=True, capture_output=True)


def test_backing_budget_labels_and_known_low_memory():
    gib = 1024**3
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        gib, 32 * gib, 24, 20, None,
    ))
    with pytest.raises(ResourceError, match="singing-preserving backing") as caught:
        budget.acquire(bandit.WORK_ESTIMATE, max_wait_seconds=0,
                       work_label="singing-preserving backing")
    assert "reduce the export" not in str(caught.value)


def test_backing_budget_unknown_telemetry_serializes_with_existing_work():
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        None, None, None, None, "unavailable",
    ))
    with budget.acquire(WorkEstimate(100, 2), work_label="singing-preserving backing") as admission:
        assert admission.ffmpeg_threads == 1
    with budget.acquire(WorkEstimate(100, 2)) as admission:
        assert admission.ffmpeg_threads == 1


def test_backing_budget_known_impossible_work_and_cancellation():
    from choicer_voicer_pack_creator.operations import OperationCancelled

    gib = 1024**3
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        2 * gib, 2 * gib, 24, 20, None,
    ))
    with pytest.raises(ResourceError, match="Cannot admit singing-preserving backing"):
        budget.acquire(bandit.WORK_ESTIMATE, work_label="singing-preserving backing")
    with pytest.raises(OperationCancelled), operation_scope(cancelled=lambda: True):
        budget.acquire(bandit.WORK_ESTIMATE, work_label="singing-preserving backing")
    assert not budget._held


def test_backing_budget_counts_concurrent_export_reservations():
    gib = 1024**3
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        5 * gib, 32 * gib, 24, 20, None,
    ))
    export = budget.acquire(WorkEstimate(2 * gib, 2))
    try:
        with pytest.raises(ResourceError, match="RAM"):
            budget.acquire(bandit.WORK_ESTIMATE, max_wait_seconds=0,
                           work_label="singing-preserving backing")
    finally:
        export.release()
    admission = budget.acquire(bandit.WORK_ESTIMATE, max_wait_seconds=0,
                               work_label="singing-preserving backing")
    admission.release()


def test_backing_generation_admission_precedes_decode_and_survives_child_cleanup(tmp_path, monkeypatch):
    import choicer_voicer_pack_creator.export_resources as resources

    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    video = tmp_path / "source.mp4"
    video.write_bytes(b"immutable source")
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        8 * 1024**3, 32 * 1024**3, 24, 20, None,
    ))
    monkeypatch.setattr(resources, "export_resources", budget)
    monkeypatch.setattr(manager, "_ensure_model", lambda *_: manager.model_path)

    def decode(*_args):
        assert resources.current_ffmpeg_threads() == 2
        return 83

    monkeypatch.setattr(manager, "_decode", decode)

    def run(command, *_args, **kwargs):
        assert resources.current_ffmpeg_threads() == 2
        request = Path(command[-1])
        value = json.loads(request.read_text())
        assert value["mode"] == KEEP_SINGING and value["threads"] == 2
        sf.write(request.parent / "backing.wav", np.zeros((83, 2)), 48000, subtype="PCM_24")
        separation.write_json_atomic(request.parent / "status.json", {
            "job_id": value["job_id"], "state": "succeeded", "progress": 1.0,
        })

    monkeypatch.setattr(separation, "_run_cancellable", run)
    with operation_scope():
        output = manager.generate(None, video, progress=lambda *_: None, cancelled=lambda: False)
    assert sf.info(output).samplerate == 48000
    assert resources.current_ffmpeg_threads() is None
    assert not budget._held
    assert not list((manager.data_root / "separation-jobs").iterdir())


def test_bandit_source_mutation_rejects_successful_result_and_releases_budget(tmp_path, monkeypatch):
    import choicer_voicer_pack_creator.export_resources as resources
    from choicer_voicer_pack_creator.operations import SourceChangedError

    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    video = tmp_path / "source.mp4"
    video.write_bytes(b"source before generation")
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        8 * 1024**3, 32 * 1024**3, 24, 20, None,
    ))
    monkeypatch.setattr(resources, "export_resources", budget)
    monkeypatch.setattr(manager, "_ensure_model", lambda *_: manager.model_path)
    monkeypatch.setattr(manager, "_decode", lambda *_: 83)

    def run(command, *_args, **_kwargs):
        request = Path(command[-1])
        value = json.loads(request.read_text())
        sf.write(request.parent / "backing.wav", np.zeros((83, 2)), 48000, subtype="PCM_24")
        separation.write_json_atomic(request.parent / "status.json", {
            "job_id": value["job_id"], "state": "succeeded", "progress": 1.0,
        })
        video.write_bytes(b"changed source during generation")

    monkeypatch.setattr(separation, "_run_cancellable", run)
    with pytest.raises(SourceChangedError):
        manager.generate(None, video, progress=lambda *_: None, cancelled=lambda: False)
    assert not (manager.data_root / "backing-tracks").exists()
    assert not budget._held
    assert not list((manager.data_root / "separation-jobs").iterdir())


def test_bandit_cancelled_child_is_reaped_before_resource_release(tmp_path, monkeypatch):
    import choicer_voicer_pack_creator.analysis as analysis
    import choicer_voicer_pack_creator.export_resources as resources

    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    video = tmp_path / "source.mp4"
    video.write_bytes(b"immutable source")
    ready = tmp_path / "ready"
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        8 * 1024**3, 32 * 1024**3, 24, 20, None,
    ))
    monkeypatch.setattr(resources, "export_resources", budget)
    monkeypatch.setattr(manager, "_ensure_model", lambda *_: manager.model_path)
    monkeypatch.setattr(manager, "_decode", lambda *_: 83)
    processes = []
    popen = analysis.subprocess.Popen

    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def run(_command, description, cancelled, *, tick):
        try:
            return analysis._run_cancellable([
                sys.executable, "-c",
                "import pathlib,time,sys;pathlib.Path(sys.argv[1]).write_text('ready');time.sleep(30)",
                str(ready),
            ], description, cancelled, tick=tick)
        finally:
            assert resources.current_ffmpeg_threads() == 2
            assert processes[0].poll() is not None

    monkeypatch.setattr(analysis.subprocess, "Popen", capture)
    monkeypatch.setattr(separation, "_run_cancellable", run)
    with pytest.raises(separation.SeparationCancelled):
        manager.generate(None, video, progress=lambda *_: None, cancelled=ready.exists)
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not budget._held
    assert not (manager.data_root / "backing-tracks").exists()
    assert not list((manager.data_root / "separation-jobs").iterdir())


@pytest.mark.integration
@pytest.mark.parametrize("rate", [44100, 48000])
def test_bandit_decode_native_rate_and_delayed_timeline(tmp_path, monkeypatch, rate):
    import shutil

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg is not installed")
    video = tmp_path / "delayed.mkv"
    subprocess.run([
        ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi",
        "-i", "color=c=black:s=32x32:r=25:d=2", "-itsoffset", "0.5",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration=0.5",
        "-map", "0:v", "-map", "1:a", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(video),
    ], check=True, timeout=30)
    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    if rate == 48000:
        monkeypatch.setattr(
            bandit, "resample_stream",
            lambda *_args, **_kwargs: pytest.fail("Native 48 kHz must never be resampled"),
        )
    output = tmp_path / "decoded.wav"
    frames = manager._decode(SimpleNamespace(ffmpeg=ffmpeg, ffprobe=ffprobe), video, output,
                             lambda *_: None, lambda: False)
    actual, actual_rate = sf.read(output, dtype="float32", always_2d=True)
    assert frames == 96000 and actual_rate == 48000 and actual.shape == (96000, 2)
    assert not np.any(actual[:23000])
    assert np.max(np.abs(actual[27000:45000])) > 0.01
    assert not np.any(actual[49000:])
    assert not (tmp_path / "decoded-native.wav").exists()
