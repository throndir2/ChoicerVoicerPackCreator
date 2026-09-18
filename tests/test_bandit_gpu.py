"""Simulated CUDA control-flow coverage; these tests are not GPU hardware evidence."""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from choicer_voicer_pack_creator import bandit_gpu as gpu
from choicer_voicer_pack_creator import bandit_runtime, separation, separation_worker
from choicer_voicer_pack_creator.export_resources import ExportResourceBudget, ResourceSnapshot
from choicer_voicer_pack_creator.separation_types import KEEP_SINGING


@pytest.mark.parametrize("error,expected", [
    (gpu.GPUUnavailable("driver"), True),
    (RuntimeError("CUDA out of memory. Tried to allocate"), True),
    (RuntimeError("CUDA error: no kernel image is available for execution"), True),
    (RuntimeError("CUDA error: invalid device function"), True),
    (RuntimeError("CUDA error: initialization error"), True),
    (RuntimeError("CUDA driver version is insufficient for CUDA runtime version"), True),
    (RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"), True),
    (RuntimeError("CUDNN_STATUS_NOT_SUPPORTED"), True),
    (RuntimeError("CUDA error: device-side assert triggered"), False),
    (RuntimeError("CUDA error: an illegal memory access was encountered"), False),
    (RuntimeError("Error(s) in loading state_dict for Bandit"), False),
    (ValueError("Invalid checkpoint SHA-256"), False),
    (separation.SeparationError("non-finite audio"), False),
    (separation.SeparationCancelled("CUDA out of memory"), False),
    (OSError("disk full"), False),
])
def test_gpu_error_classification_is_narrow(error, expected):
    assert gpu.recoverable_gpu_error(error) is expected


@pytest.mark.parametrize("capability,architectures,expected", [
    ((8, 6), ["sm_75", "sm_80"], True),
    ((7, 0), ["sm_75", "sm_80"], False),
    ((12, 0), ["sm_90", "sm_120"], True),
    ((12, 0), ["sm_90"], False),
    ((12, 0), ["compute_90"], True),
    ((8, 0), ["sm_86"], False),
    ((8, 6), [], False),
])
def test_architecture_compatibility(capability, architectures, expected):
    assert gpu.architecture_supported(capability, architectures) is expected


@pytest.mark.parametrize("version,count,init_error,expected", [
    (12080, 1, 0, True), (13000, 2, 0, True), (12070, 1, 0, False),
    (12080, 0, 0, False), (12080, 1, 100, False),
])
def test_download_probe_checks_driver_api_not_gpu_presence(
    monkeypatch, version, count, init_error, expected,
):
    def fill(pointer, value):
        pointer._obj.value = value
        return 0

    driver = SimpleNamespace(
        cuInit=lambda _: init_error,
        cuDriverGetVersion=lambda ptr: fill(ptr, version),
        cuDeviceGetCount=lambda ptr: fill(ptr, count),
    )
    monkeypatch.setattr(gpu.sys, "platform", "win32")
    monkeypatch.setattr(gpu.ctypes, "WinDLL", lambda name, **kw: driver, raising=False)
    result = gpu.probe_nvidia()
    assert result["candidate"] is expected
    assert bool(result["reason"]) is not expected


def test_missing_nvidia_driver_reports_cpu_without_importing_torch(monkeypatch):
    def missing(*_args, **_kwargs):
        raise OSError("NVIDIA driver missing")

    monkeypatch.setattr(gpu.sys, "platform", "win32")
    monkeypatch.setattr(gpu.ctypes, "WinDLL", missing, raising=False)
    before = set(sys.modules)
    result = gpu.probe_nvidia()
    assert result["candidate"] is False and "driver missing" in result["reason"]
    assert not any(name.startswith(("torch", "PySide6")) for name in set(sys.modules) - before)


@pytest.fixture
def cuda(monkeypatch):
    calls = []
    cuda = SimpleNamespace(
        is_available=lambda: True, device_count=lambda: 1,
        get_device_capability=lambda index: (8, 6), get_arch_list=lambda: ["sm_80"],
        mem_get_info=lambda index: (8 * 1024**3, 12 * 1024**3),
        get_device_name=lambda index: "Synthetic NVIDIA",
        synchronize=lambda index: calls.append(("synchronize", index)),
        max_memory_allocated=lambda index: 2 * 1024**3,
    )
    torch = SimpleNamespace(
        __version__="2.8.0+cu128", version=SimpleNamespace(cuda="12.8"), cuda=cuda,
        set_num_threads=lambda threads: calls.append(("threads", threads)),
        get_num_interop_threads=lambda: 1,
        backends=SimpleNamespace(
            cuda=SimpleNamespace(matmul=SimpleNamespace(allow_tf32=True)),
            cudnn=SimpleNamespace(allow_tf32=True, benchmark=True),
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torchaudio", SimpleNamespace(__version__="2.8.0+cu128"))

    class Model:
        def to(self, device):
            calls.append(("to", device))
            return self

    def load(path, device, *, check_cancelled):
        check_cancelled()
        calls.append(("load", path, device))
        return Model()

    def predictor(model, *, device, cancelled):
        def predict(block):
            cancelled()
            calls.append(("predict", device, block.shape, block.dtype))
        return predict

    monkeypatch.setattr("choicer_voicer_pack_creator._bandit.load_model", load)
    monkeypatch.setattr(bandit_runtime, "make_predictor", predictor)
    return torch, calls


def test_auto_gpu_qualification_executes_full_stereo_window_without_tf32(cuda):
    torch, calls = cuda
    messages = []
    _, device, details = gpu.load_gpu_model(
        Path("verified.ckpt"), 2, lambda *args: messages.append(args), lambda: False,
    )
    assert device == "cuda:0"
    assert ("load", Path("verified.ckpt"), "cpu") in calls
    assert ("predict", "cuda:0", (384000, 2), np.dtype("float32")) in calls
    assert calls[-1] == ("synchronize", 0)
    assert details["device_name"] == "Synthetic NVIDIA"
    assert not torch.backends.cuda.matmul.allow_tf32
    assert not torch.backends.cudnn.allow_tf32
    assert not torch.backends.cudnn.benchmark
    assert "eight-second" in messages[-1][0]


@pytest.mark.parametrize("failure", ["runtime", "unavailable", "architecture", "memory", "headroom"])
def test_gpu_qualification_rejects_unsupported_runtime_architecture_and_memory(cuda, failure):
    torch, calls = cuda
    if failure == "runtime":
        torch.__version__ = "2.8.0+cpu"
    elif failure == "unavailable":
        torch.cuda.is_available = lambda: False
    elif failure == "architecture":
        torch.cuda.get_arch_list = lambda: ["sm_90"]
    elif failure == "memory":
        torch.cuda.mem_get_info = lambda _: (3 * 1024**3, 8 * 1024**3)
    else:
        snapshots = iter([(8 * 1024**3, 12 * 1024**3), (128 * 1024**2, 12 * 1024**3)])
        torch.cuda.mem_get_info = lambda _: next(snapshots)
    with pytest.raises(gpu.GPUUnavailable):
        gpu.load_gpu_model(Path("verified.ckpt"), 1, lambda *_: None, lambda: False)
    if failure != "headroom":
        assert not any(call[0] == "load" for call in calls)


def test_automatic_selection_uses_eligible_device_with_most_free_memory(cuda):
    torch, _ = cuda
    torch.cuda.device_count = lambda: 3
    torch.cuda.get_device_capability = lambda index: (7, 0) if index == 0 else (8, 6)
    torch.cuda.mem_get_info = lambda index: ((4 + index) * 1024**3, 12 * 1024**3)
    _, device, _ = gpu.load_gpu_model(Path("verified.ckpt"), 1, lambda *_: None, lambda: False)
    assert device == "cuda:2"


def test_gpu_qualification_preserves_cancellation_and_checkpoint_errors(cuda, monkeypatch):
    _, calls = cuda
    with pytest.raises(separation.SeparationCancelled):
        gpu.load_gpu_model(Path("verified.ckpt"), 1, lambda *_: None, lambda: True)
    assert not calls

    def corrupt(*_args, **_kwargs):
        raise ValueError("Invalid checkpoint SHA-256")

    monkeypatch.setattr("choicer_voicer_pack_creator._bandit.load_model", corrupt)
    with pytest.raises(ValueError, match="SHA-256"):
        gpu.load_gpu_model(Path("verified.ckpt"), 1, lambda *_: None, lambda: False)


def test_cuda_import_failure_is_recoverable_before_model_loading(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(gpu.GPUUnavailable, match="runtime could not initialize"):
        gpu.load_gpu_model(Path("verified.ckpt"), 1, lambda *_: None, lambda: False)


@pytest.mark.parametrize("cancel_second_channel", [False, True])
def test_predictor_transfers_sequential_stereo_float32_and_cancels_between_channels(
    monkeypatch, cancel_second_channel,
):
    calls = []
    class Tensor(np.ndarray):
        device = SimpleNamespace(type="cpu")

        def __array_finalize__(self, obj):
            self.device = obj.device if isinstance(obj, Tensor) else SimpleNamespace(type="cpu")

        def to(self, device):
            result = self.copy()
            result.device = SimpleNamespace(type=device.split(":")[0])
            calls.append(("to", device))
            return result

        def cpu(self):
            calls.append(("cpu",))
            return self.to("cpu")

        def numpy(self):
            return np.asarray(self)

    @contextmanager
    def inference_mode():
        yield

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        Tensor=Tensor, float32=np.dtype("float32"), inference_mode=inference_mode,
        from_numpy=lambda x: x.view(Tensor), isfinite=np.isfinite,
    ))
    channels = []
    def model(batch):
        audio = batch["mixture"]["audio"]
        assert audio.device.type == "cuda" and audio.dtype == np.float32
        channels.append(np.asarray(audio).copy())
        return {"estimates": {stem: {"audio": audio * gain}
                             for stem, gain in (("music", .4), ("sfx", .2), ("speech", 10))}}

    predictor = bandit_runtime.make_predictor(
        model, device="cuda:1", cancelled=lambda: cancel_second_channel and len(channels) == 1,
    )
    samples = np.column_stack((np.arange(8), -np.arange(8))).astype(np.float32)
    if cancel_second_channel:
        with pytest.raises(separation.SeparationCancelled):
            predictor(samples)
        assert len(channels) == 1
    else:
        result = predictor(samples)
        np.testing.assert_array_equal(result["music"], samples * .4)
        assert len(channels) == 2 and calls.count(("to", "cuda:1")) == 2
        assert calls.count(("cpu",)) == 6


@pytest.mark.parametrize("available,cached,consent,offer", [
    (False, False, False, True), (True, True, False, True),
    (True, False, False, True), (True, False, True, True),
    (True, False, False, False),
])
def test_runtime_selection_requires_distinct_consent_and_reuses_cache(
    tmp_path, monkeypatch, available, cached, consent, offer,
):
    import choicer_voicer_pack_creator.bandit_cuda_runtime as runtime

    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    job = tmp_path / "job"
    job.mkdir()
    root = tmp_path / "cuda"
    calls = []
    monkeypatch.setattr(runtime, "runtime_supported", lambda: True)
    monkeypatch.setattr(runtime, "runtime_download_bytes", lambda: 3 * 1024**3)
    monkeypatch.setattr(runtime, "installed_runtime", lambda *_: root if cached else None)
    monkeypatch.setattr(
        runtime, "install_runtime", lambda *_args, **_kw: calls.append("download") or root,
    )
    def probe(command, *_args):
        request = Path(command[-1])
        value = json.loads(request.read_text())
        assert value["probe_cuda"] is True
        separation.write_json_atomic(request.parent / "probe.json", {
            "candidate": available, "reason": "" if available else "No NVIDIA GPU",
        })
    monkeypatch.setattr(separation, "_run_cancellable", probe)
    if available and not cached and not consent and offer:
        with pytest.raises(separation.SeparationRuntimeDownloadRequired) as caught:
            manager._select_bandit_runtime(job, consent, offer, lambda *_: None, lambda: False)
        assert caught.value.download_bytes == 3 * 1024**3
    else:
        selected, reason = manager._select_bandit_runtime(
            job, consent, offer, lambda *_: None, lambda: False,
        )
        assert (selected == root) is (available and (cached or consent))
        assert bool(reason) is (selected is None)
    assert calls == (["download"] if available and not cached and consent else [])


def test_unsupported_platform_never_probes_or_downloads_runtime(tmp_path, monkeypatch):
    import choicer_voicer_pack_creator.bandit_cuda_runtime as runtime

    monkeypatch.setattr(runtime, "runtime_supported", lambda: False)
    monkeypatch.setattr(separation, "_run_cancellable", lambda *_: pytest.fail("No subprocess"))
    manager = separation.SeparationManager(tmp_path, mode=KEEP_SINGING)
    selected, reason = manager._select_bandit_runtime(
        tmp_path, True, True, lambda *_: None, lambda: False,
    )
    assert selected is None and "Windows x64" in reason


@pytest.mark.parametrize("cancel", [False, True])
def test_optional_runtime_install_failure_uses_cpu_but_cancellation_does_not(
    tmp_path, monkeypatch, cancel,
):
    import choicer_voicer_pack_creator.bandit_cuda_runtime as runtime

    manager = separation.SeparationManager(tmp_path, mode=KEEP_SINGING)
    monkeypatch.setattr(runtime, "runtime_supported", lambda: True)
    monkeypatch.setattr(runtime, "installed_runtime", lambda *_: None)
    def probe(command, *_args):
        path = Path(command[-1])
        separation.write_json_atomic(path.parent / "probe.json", {"candidate": True, "reason": ""})
    monkeypatch.setattr(separation, "_run_cancellable", probe)
    def install(*_args, **kwargs):
        assert kwargs["allow_download"] is True
        if cancel:
            raise separation.SeparationCancelled("Runtime download canceled")
        raise runtime.CUDARuntimeError("Downloaded CUDA wheel hash mismatch")
    monkeypatch.setattr(runtime, "install_runtime", install)
    messages = []
    if cancel:
        with pytest.raises(separation.SeparationCancelled):
            manager._select_bandit_runtime(tmp_path, True, True, lambda *_: None, lambda: False)
    else:
        selected, reason = manager._select_bandit_runtime(
            tmp_path, True, True, lambda *a: messages.append(a), lambda: False,
        )
        assert selected is None and "hash mismatch" in reason
        assert "Continuing on CPU" in messages[-1][0]


@pytest.fixture
def worker_job(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    request = job / "request.json"
    request.write_text(json.dumps({
        "version": 1, "job_id": job.name, "mode": KEEP_SINGING, "frames": 49,
        "model": str(tmp_path / "verified.ckpt"), "threads": 1, "cuda_runtime": "optional-runtime",
    }))
    sf.write(job / "decoded.wav", np.full((49, 2), (0.2, -0.3)), 48000, subtype="FLOAT")
    return request


def stub_worker_gpu(monkeypatch):
    import choicer_voicer_pack_creator.bandit_cuda_runtime as runtime

    monkeypatch.setattr(runtime, "activate_runtime", lambda _: None)
    monkeypatch.setattr(gpu, "load_gpu_model", lambda *_: (
        object(), "cuda:0", {"device": "cuda:0", "device_name": "Synthetic NVIDIA"},
    ))
    monkeypatch.setattr(bandit_runtime, "make_predictor", lambda *_args, **_kw: (
        lambda block: {"music": block * 0.4, "sfx": block * 0.2, "speech": block * 10}
    ))
    stream = bandit_runtime.separate_stream
    monkeypatch.setattr(bandit_runtime, "separate_stream", lambda *args: stream(
        *args, chunk_frames=32, hop_frames=24,
    ))
    return runtime


def test_gpu_worker_reports_actual_device_and_preserves_stereo_output(worker_job, monkeypatch):
    stub_worker_gpu(monkeypatch)
    assert separation_worker.worker_main(worker_job) == 0
    status = json.loads((worker_job.parent / "status.json").read_text())
    assert status["state"] == "succeeded" and status["device"] == "cuda:0"
    assert "Synthetic NVIDIA" in status["message"]
    result, rate = sf.read(worker_job.parent / "backing.wav", always_2d=True)
    assert rate == 48000 and result.shape == (49, 2)
    np.testing.assert_allclose(result, np.tile([0.12, -0.18], (49, 1)), atol=1.3e-7)


def test_worker_native_runtime_smoke_uses_cached_cuda_without_model_or_gpu(
    worker_job, monkeypatch,
):
    import choicer_voicer_pack_creator.bandit_cuda_runtime as runtime

    request = json.loads(worker_job.read_text())
    request.update(smoke_test=True, cuda_runtime_smoke=True)
    worker_job.write_text(json.dumps(request))
    calls = []
    monkeypatch.setattr(separation_worker, "sys", SimpleNamespace(modules={}))
    monkeypatch.setattr(runtime, "native_import_smoke", lambda root: (
        calls.append(root) or {"torch": "2.8.0+cu128", "gpu_available": False, "qt_imported": False}
    ))
    monkeypatch.setattr(bandit_runtime, "load_cpu_model", lambda *_: pytest.fail("No model needed"))
    assert separation_worker.worker_main(worker_job) == 0
    assert calls == [Path("optional-runtime")]
    report = json.loads((worker_job.parent / "smoke.json").read_text())
    assert report["torch"] == "2.8.0+cu128" and report["gpu_available"] is False


@pytest.mark.parametrize("stage", ["activate", "cache", "qualification", "inference"])
def test_gpu_worker_requests_fresh_cpu_retry_and_cleans_output(worker_job, monkeypatch, stage):
    runtime = stub_worker_gpu(monkeypatch)
    def fail(*_args, **_kwargs):
        if stage == "activate":
            raise OSError("CUDA DLL could not initialize")
        if stage == "cache":
            raise runtime.CUDARuntimeError("Optional CUDA cache was corrupted")
        if stage == "inference":
            (worker_job.parent / "backing.wav").write_bytes(b"partial")
        raise RuntimeError("CUDA out of memory")

    if stage in {"activate", "cache"}:
        monkeypatch.setattr(runtime, "activate_runtime", fail)
    elif stage == "qualification":
        monkeypatch.setattr(gpu, "load_gpu_model", fail)
    else:
        monkeypatch.setattr(bandit_runtime, "make_predictor", lambda *_args, **_kw: fail)
    monkeypatch.setattr(bandit_runtime, "load_cpu_model", lambda *_: pytest.fail("Fresh process required"))
    assert separation_worker.worker_main(worker_job) == 2
    status = json.loads((worker_job.parent / "status.json").read_text())
    assert status["state"] == "cpu_retry" and status["attempt"] == 1
    assert status["fallback_reason"]
    assert not (worker_job.parent / "backing.wav").exists()
    assert not (worker_job.parent / "unscaled-bandit.wav").exists()


@pytest.mark.parametrize("error", [
    ValueError("corrupt checkpoint"), RuntimeError("state_dict keys mismatch"),
    separation.SeparationError("invalid audio"),
    separation.SeparationCancelled("Backing generation canceled"),
    RuntimeError("CUDA error: device-side assert triggered"),
])
def test_worker_does_not_hide_invalid_models_audio_cancellation_or_logic_errors(
    worker_job, monkeypatch, error,
):
    stub_worker_gpu(monkeypatch)
    def fail(*_args):
        raise error
    monkeypatch.setattr(gpu, "load_gpu_model", fail)
    assert separation_worker.worker_main(worker_job) == 1
    status = json.loads((worker_job.parent / "status.json").read_text())
    assert status["state"] == "failed" and str(error) in status["message"]


@pytest.fixture
def generation(tmp_path, monkeypatch):
    import choicer_voicer_pack_creator.export_resources as resources

    manager = separation.SeparationManager(tmp_path / "data", mode=KEEP_SINGING)
    video = tmp_path / "source.mp4"
    video.write_bytes(b"immutable source")
    monkeypatch.setattr(manager, "_ensure_model", lambda *_: manager.model_path)
    monkeypatch.setattr(manager, "_decode", lambda *_: 49)
    monkeypatch.setattr(manager, "_select_bandit_runtime", lambda *_: (tmp_path / "cuda", ""))
    budget = ExportResourceBudget(snapshot_provider=lambda: ResourceSnapshot(
        12 * 1024**3, 32 * 1024**3, 24, 20, None,
    ))
    monkeypatch.setattr(resources, "export_resources", budget)
    return manager, video, budget


@pytest.mark.parametrize("cpu_outcome", ["success", "failure", "cancel"])
def test_parent_restarts_once_on_cpu_reaps_then_cleans_before_retry(
    generation, monkeypatch, cpu_outcome,
):
    manager, video, budget = generation
    calls, updates, diagnostics = [], [], []
    monkeypatch.setattr(separation, "diagnostic_event", lambda *a, **kw: diagnostics.append((a, kw)))

    def run(command, _description, _cancelled, *, tick):
        path = Path(command[-1])
        request = json.loads(path.read_text())
        assert budget._held
        calls.append(request)
        job = path.parent
        status = {
            "job_id": job.name, "attempt": request["attempt"], "progress": None,
            "fallback_reason": "CUDA out of memory",
        }
        if len(calls) == 1:
            assert "cuda_runtime" in request
            (job / "unscaled-bandit.wav").write_bytes(b"partial GPU audio")
            (job / "backing.wav").write_bytes(b"partial GPU output")
            status.update(state="cpu_retry", message="releasing GPU")
            separation.write_json_atomic(job / "status.json", status)
            raise separation.AnalysisError("reaped GPU failure")
        assert "cuda_runtime" not in request
        assert request["fallback_reason"] == "CUDA out of memory"
        assert not (job / "unscaled-bandit.wav").exists()
        assert not (job / "backing.wav").exists()
        assert not (job / "status.json").exists()
        if cpu_outcome == "cancel":
            raise separation.AnalysisCancelled("CPU canceled")
        status.update(
            state="succeeded" if cpu_outcome == "success" else "failed", message="CPU result",
            device="cpu", device_name="CPU", progress=1.0,
        )
        separation.write_json_atomic(job / "status.json", status)
        if cpu_outcome == "failure":
            raise separation.AnalysisError("CPU inference failed")
        sf.write(job / "backing.wav", np.full((49, 2), (0.12, -0.18)), 48000, subtype="PCM_24")
        tick(0)

    monkeypatch.setattr(separation, "_run_cancellable", run)
    kwargs = {"progress": lambda *a: updates.append(a), "cancelled": lambda: False}
    if cpu_outcome == "success":
        output = manager.generate(None, video, **kwargs)
        assert sf.info(output).frames == 49
        completed = next(kw for a, kw in diagnostics if a[0] == "backing_separation_completed")
        assert completed["device"] == "cpu" and completed["fallback_reason"] == "CUDA out of memory"
    else:
        with pytest.raises(
            separation.SeparationCancelled if cpu_outcome == "cancel" else separation.SeparationError,
        ):
            manager.generate(None, video, **kwargs)
        assert not (manager.data_root / "backing-tracks").exists()
    assert len(calls) == 2
    assert any("time estimate resets" in message for message, _ in updates)
    assert not budget._held
    assert not list((manager.data_root / "separation-jobs").iterdir())
    assert video.read_bytes() == b"immutable source"


@pytest.mark.parametrize("failure", ["cancelled-process", "cancel-before-retry", "crash", "unrelated"])
def test_parent_never_retries_cancellation_native_crashes_or_unrelated_errors(
    generation, monkeypatch, failure,
):
    manager, video, budget = generation
    canceled, calls = [False], []
    def run(command, *_args, **_kwargs):
        calls.append(command)
        path = Path(command[-1])
        if failure == "cancelled-process":
            raise separation.AnalysisCancelled("canceled")
        if failure != "crash":
            separation.write_json_atomic(path.parent / "status.json", {
                "job_id": path.parent.name, "attempt": 1,
                "state": "cpu_retry" if failure == "cancel-before-retry" else "failed",
                "message": failure, "fallback_reason": "CUDA out of memory",
            })
        canceled[0] = failure == "cancel-before-retry"
        raise separation.AnalysisError(failure)
    monkeypatch.setattr(separation, "_run_cancellable", run)
    with pytest.raises(separation.SeparationError):
        manager.generate(None, video, progress=lambda *_: None, cancelled=lambda: canceled[0])
    assert len(calls) == 1 and not budget._held
    assert not list((manager.data_root / "separation-jobs").iterdir())


def test_fallback_really_reaps_gpu_process_before_new_cpu_process(generation, monkeypatch):
    from choicer_voicer_pack_creator import analysis

    manager, video, budget = generation
    children = []
    original_popen = analysis.subprocess.Popen
    def popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(analysis.subprocess, "Popen", popen)
    code = """
import json, sys
from pathlib import Path
import numpy as np
import soundfile as sf
from choicer_voicer_pack_creator.separation import write_json_atomic
request_path = Path(sys.argv[1])
request = json.loads(request_path.read_text())
job = request_path.parent
status = {'job_id': job.name, 'attempt': request['attempt'], 'progress': None}
if 'cuda_runtime' in request:
    (job / 'unscaled-bandit.wav').write_bytes(b'failed GPU attempt')
    status.update(state='cpu_retry', fallback_reason='CUDA out of memory', message='GPU exit')
    write_json_atomic(job / 'status.json', status)
    sys.exit(2)
assert not (job / 'unscaled-bandit.wav').exists()
assert not (job / 'status.json').exists()
sf.write(job / 'backing.wav', np.zeros((49, 2)), 48000, subtype='PCM_24')
status.update(state='succeeded', device='cpu', device_name='CPU', message='CPU success')
write_json_atomic(job / 'status.json', status)
"""
    def run(command, description, cancelled, *, tick):
        assert all(child.poll() is not None for child in children)
        try:
            return analysis._run_cancellable(
                [sys.executable, "-E", "-s", "-c", code, command[-1]],
                description, cancelled, tick=tick,
            )
        finally:
            assert budget._held
            assert children[-1].poll() is not None
    monkeypatch.setattr(separation, "_run_cancellable", run)
    output = manager.generate(None, video, progress=lambda *_: None, cancelled=lambda: False)
    assert sf.info(output).frames == 49
    assert len(children) == 2 and children[0].pid != children[1].pid
    assert not budget._held


def test_cpu_remains_available_when_only_cuda_host_ram_budget_cannot_fit(generation, monkeypatch):
    import choicer_voicer_pack_creator.export_resources as resources

    manager, video, _ = generation
    clock = [0.0]
    def advance(seconds):
        clock[0] += seconds
    budget = ExportResourceBudget(
        snapshot_provider=lambda: ResourceSnapshot(4 * 1024**3, 8 * 1024**3, 8, 8, None),
        clock=lambda: clock[0], wait=advance,
    )
    monkeypatch.setattr(resources, "export_resources", budget)
    requests = []
    def run(command, *_args, **_kwargs):
        path = Path(command[-1])
        request = json.loads(path.read_text())
        requests.append(request)
        assert "cuda_runtime" not in request
        assert "CUDA host resources unavailable" in request["fallback_reason"]
        sf.write(path.parent / "backing.wav", np.zeros((49, 2)), 48000, subtype="PCM_24")
        separation.write_json_atomic(path.parent / "status.json", {
            "job_id": path.parent.name, "state": "succeeded", "device": "cpu",
            "device_name": "CPU", "fallback_reason": request["fallback_reason"],
        })
    monkeypatch.setattr(separation, "_run_cancellable", run)
    assert manager.generate(None, video, progress=lambda *_: None, cancelled=lambda: False).is_file()
    assert len(requests) == 1 and not budget._held


def test_streaming_cpu_restart_recalibrates_after_discarding_gpu_chunks(tmp_path, monkeypatch):
    from choicer_voicer_pack_creator import separation_progress

    clock, calls, updates = [0.0], [0], []
    monkeypatch.setattr(separation_progress, "monotonic", lambda: clock[0])
    source, output = tmp_path / "decoded.wav", tmp_path / "backing.wav"
    samples = np.full((49, 2), (0.2, -0.3), np.float32)
    sf.write(source, samples, 48000, subtype="FLOAT")
    def predict(block):
        calls[0] += 1
        clock[0] += 1
        if calls[0] == 2:
            raise RuntimeError("CUDA out of memory")
        return {"music": block * 0.4, "sfx": block * 0.2, "speech": block}
    with pytest.raises(RuntimeError, match="CUDA"):
        bandit_runtime.separate_stream(
            source, output, predict, 49, lambda *a: updates.append(a), lambda: False,
            chunk_frames=32, hop_frames=24,
        )
    assert not output.exists() and not (tmp_path / "unscaled-bandit.wav").exists()
    assert "about 2s remaining" in updates[-1][0]
    updates.clear()
    clock[0] += 1000
    def cpu_predict(block):
        clock[0] += 20
        return {"music": block * 0.4, "sfx": block * 0.2, "speech": block}
    bandit_runtime.separate_stream(
        source, output, cpu_predict, 49, lambda *a: updates.append(a), lambda: False,
        chunk_frames=32, hop_frames=24,
    )
    assert "estimating time after the first chunk" in updates[0][0]
    assert "about 40s remaining" in updates[1][0]
    np.testing.assert_allclose(sf.read(output, always_2d=True)[0], samples * 0.6, atol=1.3e-7)
