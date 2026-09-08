from __future__ import annotations

import json
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

import scripts.compare_separation as comparison
from scripts.compare_separation import crop_audio, read_audio, separate_chunks, write_results


@pytest.mark.parametrize("frames", [1, 7, 24, 31, 32, 33, 83, 101])
@pytest.mark.parametrize("window_kind,overlap", [("linear", 8), ("hann", 8), ("hann", 28)])
def test_comparison_overlap_preserves_samples_and_boundaries(frames, window_kind, overlap):
    samples = np.linspace(-0.8, 0.8, frames * 2, dtype=np.float32).reshape(-1, 2)

    def predict(block):
        return {"backing": block * 0.75, "removed": block * 0.25}

    result = separate_chunks(samples, predict, 32, overlap, window_kind=window_kind)
    np.testing.assert_allclose(result["backing"], samples * 0.75, atol=1e-7)
    np.testing.assert_allclose(result["removed"], samples * 0.25, atol=1e-7)


@pytest.mark.parametrize("failure", ["shape", "nan", "missing"])
def test_comparison_rejects_invalid_model_outputs(failure):
    def predict(block):
        if failure == "shape":
            return {"backing": block[:-1], "removed": block}
        if failure == "nan":
            return {"backing": block * np.nan, "removed": block}
        return {"backing": block}

    with pytest.raises(ValueError, match="output|stems"):
        separate_chunks(np.ones((83, 2), dtype=np.float32), predict, 32, 8)


def test_comparison_crop_uses_input_relative_timing():
    samples = np.arange(410 * 2, dtype=np.float32).reshape(-1, 2)
    result = crop_audio(samples, 10, 5, 31)
    np.testing.assert_array_equal(result, samples[50:360])


@pytest.mark.parametrize("start,duration", [(-1, 1), (0, 0), (0, float("nan")), (40, 2)])
def test_comparison_rejects_invalid_listening_ranges(start, duration):
    with pytest.raises(ValueError, match="Crop|outside"):
        crop_audio(np.zeros((410, 2)), 10, start, duration)


def test_comparison_reads_stereo_and_explicit_mono(tmp_path):
    source = tmp_path / "source.wav"
    samples = np.tile([0.2, 0.6], (100, 1)).astype(np.float32)
    sf.write(source, samples, 44100, subtype="FLOAT")
    np.testing.assert_array_equal(read_audio(source, 44100, 2), samples)
    np.testing.assert_allclose(read_audio(source, 44100, 1), 0.4, atol=1e-7)


def test_comparison_writes_raw_audio_and_safe_listening_copies(tmp_path):
    output = tmp_path / "result"
    audio = {
        "original": np.ones((83, 2), dtype=np.float32) * 0.8,
        "backing": np.ones((83, 2), dtype=np.float32) * 2,
        "removed": np.ones((83, 2), dtype=np.float32) * -1.2,
    }
    write_results(output, audio, 44100, {"backend": "synthetic"})
    report = json.loads((output / "report.json").read_text())
    assert report["listening_gain"] == 0.49
    assert report["frames"] == 83
    assert report["backend"] == "synthetic"
    for name, expected in audio.items():
        raw, rate = sf.read(output / f"{name}.wav", dtype="float32", always_2d=True)
        listening, _ = sf.read(output / f"{name}-listen.wav", always_2d=True)
        np.testing.assert_array_equal(raw, expected)
        np.testing.assert_allclose(listening, expected * 0.49, atol=1 / 32768)
        assert rate == 44100
    with pytest.raises(FileExistsError, match="refusing"):
        write_results(output, audio, 44100, {})


def test_comparison_invalid_audio_does_not_publish_results(tmp_path):
    output = tmp_path / "result"
    audio = {name: np.zeros((83, 2)) for name in ("original", "backing", "removed")}
    audio["backing"][0, 0] = np.nan
    with pytest.raises(ValueError, match="Invalid backing"):
        write_results(output, audio, 44100, {})
    assert not output.exists()


def test_help_does_not_import_optional_models():
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from scripts.compare_separation import main
try:
    main(["--help"])
except SystemExit as error:
    assert error.code == 0
assert not {"torch", "onnxruntime", "sam_audio"} & sys.modules.keys()
"""],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_sam_overrides_disable_auxiliary_models_without_changing_architecture(tmp_path):
    sam = tmp_path / "sam"
    t5 = tmp_path / "t5"
    sam.mkdir()
    t5.mkdir()
    config = {
        "text_encoder": {"name": "t5-base", "max_length": 128},
        "span_predictor": {"name": "large"},
        "text_ranker": {"name": "large"},
        "visual_ranker": {"name": "large"},
    }
    (sam / "config.json").write_text(json.dumps(config))
    (t5 / "config.json").write_text("{}")
    (t5 / "model.safetensors").write_bytes(b"fixture")
    overrides = comparison.sam_overrides(sam, t5)
    assert overrides["text_encoder"] == {"name": str(t5.resolve()), "max_length": 128}
    assert all(overrides[name] is None for name in ("span_predictor", "text_ranker", "visual_ranker"))
    assert json.loads((sam / "config.json").read_text()) == config


def test_sam_overrides_reject_config_where_upstream_would_ignore_override(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"text_encoder": {}}))
    with pytest.raises(ValueError, match="auxiliary models"):
        comparison.sam_overrides(tmp_path, tmp_path)


def test_cpu_baseline_cli_publishes_exact_crop_without_gpu(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    sf.write(source, np.ones((410, 2)) * 0.2, 10, subtype="FLOAT")
    output = tmp_path / "result"
    monkeypatch.setattr(comparison, "preflight", lambda *_: {"device": "synthetic"})
    monkeypatch.setattr(comparison, "load_htdemucs", lambda _: comparison.Backend(
        10, 2, 32, 8,
        lambda block: {"backing": block * 0.75, "removed": block * 0.25},
        {"backend": "synthetic"},
    ))
    assert comparison.main([
        "--backend", "htdemucs", "--input", str(source), "--model", str(tmp_path / "model"),
        "--output", str(output),
    ]) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["frames"] == 310
    assert report["input_seconds"] == 41
    assert report["crop_start_seconds"] == 5
    assert report["status"] == "completed"


def test_invalid_range_is_rejected_before_preflight(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    sf.write(source, np.zeros((10, 2)), 10)
    monkeypatch.setattr(comparison, "preflight", lambda *_: pytest.fail("Runtime must not load"))
    with pytest.raises(SystemExit):
        comparison.main([
            "--backend", "htdemucs", "--input", str(source), "--model", "unused",
            "--output", str(tmp_path / "output"),
        ])


def test_checkpoint_requires_size_and_hash(tmp_path):
    model = tmp_path / "model"
    model.write_bytes(b"model")
    comparison.require_checkpoint(model, 5, comparison.sha256(model))
    with pytest.raises(ValueError, match="pinned"):
        comparison.require_checkpoint(model, 6, comparison.sha256(model))
    with pytest.raises(ValueError, match="pinned"):
        comparison.require_checkpoint(model, 5, "0" * 64)


def test_sam_adapter_retains_residual_and_trims_codec_padding(tmp_path, monkeypatch):
    sam, t5 = tmp_path / "sam", tmp_path / "t5"
    sam.mkdir()
    t5.mkdir()
    (sam / "config.json").write_text(json.dumps({
        "text_encoder": {"name": "t5-base"},
        "span_predictor": {}, "text_ranker": {}, "visual_ranker": {},
    }))
    (t5 / "config.json").write_text("{}")
    (t5 / "model.safetensors").write_bytes(b"fixture")
    loaded = {}

    class Wave:
        ndim = 1

        def __init__(self, samples):
            self.samples = samples

        def numel(self):
            return len(self.samples)

        def __getitem__(self, key):
            return Wave(self.samples[key])

        def float(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.samples

    class Model:
        @classmethod
        def from_pretrained(cls, path, **overrides):
            loaded.update(overrides)
            return cls()

        def eval(self):
            return self

        def to(self, **kwargs):
            assert kwargs == {"device": "cuda", "dtype": "bf16"}
            return self

        def separate(self, batch, **kwargs):
            assert kwargs == {"predict_spans": False, "reranking_candidates": 1}
            return SimpleNamespace(
                target=[Wave(np.full(37, 0.25, dtype=np.float32))],
                residual=[Wave(np.full(37, 0.75, dtype=np.float32))],
            )

    class Processor:
        audio_sampling_rate = 48000

        @classmethod
        def from_pretrained(cls, path):
            return cls()

        def __call__(self, *, audios, descriptions):
            assert descriptions == ["speech"]
            assert audios[0].shape == (1, 32)
            return SimpleNamespace(to=lambda device: {})

    monkeypatch.setattr(comparison, "require_checkpoint", lambda path, size, digest: None)
    monkeypatch.setitem(sys.modules, "sam_audio", SimpleNamespace(
        SAMAudio=Model, SAMAudioProcessor=Processor,
    ))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        bfloat16="bf16", from_numpy=lambda samples: samples,
        inference_mode=nullcontext, autocast=lambda *args, **kwargs: nullcontext(),
    ))
    backend = comparison.load_sam(sam, t5, "speech", 8)
    result = backend.predict(np.ones((32, 1), dtype=np.float32))
    assert backend.channels == 1
    assert backend.chunk_frames == 384000
    assert result["backing"].shape == result["removed"].shape == (32, 1)
    np.testing.assert_array_equal(result["backing"], 0.75)
    np.testing.assert_array_equal(result["removed"], 0.25)
    assert all(loaded[name] is None for name in ("span_predictor", "text_ranker", "visual_ranker"))
