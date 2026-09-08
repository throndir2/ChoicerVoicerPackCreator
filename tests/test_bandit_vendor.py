from __future__ import annotations

import ast
import hashlib
import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import _bandit

ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = ROOT / "scripts" / "_bandit"


def test_import_does_not_require_optional_dependencies():
    subprocess.run(
        [
            sys.executable,
            "-S",
            "-B",
            "-c",
            "import sys; import scripts._bandit; "
            "assert not {'torch', 'torchaudio', 'numpy', 'librosa', "
            "'pytorch_lightning', 'ray', 'astroid'} & sys.modules.keys()",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_vendored_sources_match_pinned_originals_with_only_documented_adaptation():
    provenance = json.loads((VENDOR_ROOT / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["repository"] == _bandit.SOURCE_REPOSITORY
    assert provenance["revision"] == _bandit.SOURCE_REVISION
    assert provenance["source_license"] == "Apache-2.0"
    assert provenance["upstream_notice"] is None
    expected = {
        "src/models/base.py",
        "src/models/bandit/bandit.py",
        "src/models/bandit/bandsplit.py",
        "src/models/bandit/maskestim.py",
        "src/models/bandit/tfmodel.py",
        "src/models/bandit/utils.py",
        "LICENSE",
    }
    assert {entry["upstream_path"] for entry in provenance["files"]} == expected
    for entry in provenance["files"]:
        content = (VENDOR_ROOT / entry["vendored_path"]).read_bytes().replace(b"\r\n", b"\n")
        if entry["upstream_path"] == "src/models/base.py":
            content = content.replace(
                b"# Adapted for inference: replace the empty Lightning base with torch.nn.Module.\n",
                b"",
            ).replace(
                b"from torch import nn", b"import pytorch_lightning as pl"
            ).replace(
                b"class BaseEndToEndModule(nn.Module):",
                b"class BaseEndToEndModule(pl.LightningModule):",
            )
        assert hashlib.sha256(content).hexdigest() == entry["original_sha256"]


def test_vendored_imports_have_no_training_or_global_source_namespace_dependencies():
    forbidden = {"pytorch_lightning", "lightning", "ray", "astroid", "src", "models"}
    for path in (VENDOR_ROOT / "models").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {alias.name.split(".")[0] for alias in node.names} & forbidden
            elif isinstance(node, ast.ImportFrom) and not node.level:
                assert node.module.split(".")[0] not in forbidden


@pytest.mark.parametrize("split", [False, True])
def test_checkpoint_metadata_matches_provenance(split):
    provenance = json.loads((VENDOR_ROOT / "provenance.json").read_text(encoding="utf-8"))
    spec = _bandit.SPLIT_CHECKPOINT if split else _bandit.COMBINED_CHECKPOINT
    recorded = next(
        entry for entry in provenance["weights"]["checkpoints"]
        if entry["filename"] == spec.filename
    )
    assert spec.url == recorded["url"]
    assert spec.url.startswith("https://zenodo.org/records/13327983/files/")
    assert spec.size_bytes == recorded["size_bytes"]
    assert spec.md5 == recorded["md5"]
    assert spec.sha256 == recorded["sha256"]
    assert spec.stems == tuple(recorded["stems"])
    assert provenance["weights"]["license"] == _bandit.WEIGHTS_LICENSE == "CC-BY-NC-4.0"
    assert not provenance["weights"]["distributed_here"]


@pytest.fixture
def checkpoint_file(monkeypatch):
    data = b"small checkpoint stand-in for dependency-free loader tests"
    opens = []

    def open_readonly(mode):
        opens.append(mode)
        assert mode == "rb"
        return io.BytesIO(data)

    path = SimpleNamespace(
        stat=lambda: SimpleNamespace(st_size=len(data)),
        open=open_readonly,
    )
    monkeypatch.setattr(_bandit, "Path", lambda _: path)
    for name in ("COMBINED_CHECKPOINT", "SPLIT_CHECKPOINT"):
        monkeypatch.setattr(
            _bandit,
            name,
            replace(
                getattr(_bandit, name),
                size_bytes=len(data),
                md5=hashlib.md5(data, usedforsecurity=False).hexdigest(),
                sha256=hashlib.sha256(data).hexdigest(),
            ),
        )
    return SimpleNamespace(path=path, data=data, opens=opens)


@pytest.fixture
def fake_dependencies(monkeypatch):
    torch = ModuleType("torch")
    value = object()
    state_dict = {
        "model.mask_estim.music.norm_mlp.0.norm.weight": value,
        "model.mask_estim.music.norm_mlp.0.combined.0.weight": value,
        "loss.weight": object(),
    }
    torch.load = Mock(return_value={
        "state_dict": state_dict,
        "optimizer_states": [object()],
        "epoch": 10,
    })
    model = Mock()
    model.eval.return_value = model
    model.to.return_value = model
    module = ModuleType("scripts._bandit.models.bandit.bandit")
    module.Bandit = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return SimpleNamespace(torch=torch, constructor=module.Bandit, model=model, value=value)


@pytest.mark.parametrize("split", [False, True])
def test_verify_reads_checkpoint_without_writing(checkpoint_file, split):
    spec = _bandit.verify_checkpoint("unused.ckpt", split=split)
    assert spec is (_bandit.SPLIT_CHECKPOINT if split else _bandit.COMBINED_CHECKPOINT)
    assert checkpoint_file.opens == ["rb"]


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"size_bytes": 1}, "size"),
        ({"md5": "0" * 32}, "MD5"),
        ({"sha256": "0" * 64}, "SHA-256"),
    ],
)
def test_invalid_checkpoint_rejected_before_deserialization(
    checkpoint_file, fake_dependencies, monkeypatch, changes, error
):
    monkeypatch.setattr(
        _bandit, "COMBINED_CHECKPOINT", replace(_bandit.COMBINED_CHECKPOINT, **changes)
    )
    with pytest.raises(ValueError, match=error):
        _bandit.load_model("unused.ckpt", "cuda")
    fake_dependencies.torch.load.assert_not_called()
    fake_dependencies.constructor.assert_not_called()
    assert checkpoint_file.opens == ([] if error == "size" else ["rb"])


def test_unpinned_split_checkpoint_is_disabled_before_file_access(fake_dependencies):
    assert _bandit.SPLIT_CHECKPOINT.sha256 is None
    with pytest.raises(ValueError, match="no pinned SHA-256"):
        _bandit.load_model("unused.ckpt", "cuda", split=True)
    fake_dependencies.torch.load.assert_not_called()
    fake_dependencies.constructor.assert_not_called()


@pytest.mark.parametrize("split", [False, True])
def test_loader_uses_strict_weights_only_state_and_preserves_aliases(
    checkpoint_file, fake_dependencies, split
):
    result = _bandit.load_model("unused.ckpt", "cuda:0", split=split)
    assert result is fake_dependencies.model
    fake_dependencies.torch.load.assert_called_once_with(
        checkpoint_file.path, map_location="cpu", weights_only=True, mmap=True
    )
    fake_dependencies.model.load_state_dict.assert_called_once_with(
        {
            "mask_estim.music.norm_mlp.0.norm.weight": fake_dependencies.value,
            "mask_estim.music.norm_mlp.0.combined.0.weight": fake_dependencies.value,
        },
        strict=True,
    )
    spec = _bandit.SPLIT_CHECKPOINT if split else _bandit.COMBINED_CHECKPOINT
    kwargs = fake_dependencies.constructor.call_args.kwargs
    assert kwargs["stems"] == list(spec.stems)
    assert kwargs["in_channels"] == 1
    assert kwargs["fs"] == 48000
    assert kwargs["n_sqm_modules"] == 8
    assert kwargs["rnn_type"] == "GRU"
    assert kwargs["pad_mode"] == "reflect"
    assert kwargs["normalized"] is True
    assert kwargs["power"] is None
    assert kwargs["hop_length"] == 512
    assert [call[0] for call in fake_dependencies.model.method_calls] == [
        "load_state_dict", "eval", "to"
    ]
    fake_dependencies.model.to.assert_called_once_with("cuda:0")


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"state_dict": []}, {"state_dict": {"loss.weight": object()}}],
)
def test_malformed_training_checkpoint_is_rejected(
    checkpoint_file, fake_dependencies, payload
):
    fake_dependencies.torch.load.return_value = payload
    with pytest.raises(ValueError, match="state_dict"):
        _bandit.load_model("unused.ckpt", "cpu")
    fake_dependencies.constructor.assert_not_called()


def test_strict_load_failure_is_not_retried_unsafely(checkpoint_file, fake_dependencies):
    fake_dependencies.model.load_state_dict.side_effect = RuntimeError("Missing key")
    with pytest.raises(RuntimeError, match="Missing key"):
        _bandit.load_model("unused.ckpt", "cuda")
    assert fake_dependencies.torch.load.call_count == 1
    assert fake_dependencies.model.load_state_dict.call_count == 1
    assert fake_dependencies.model.load_state_dict.call_args.kwargs == {"strict": True}
    fake_dependencies.model.eval.assert_not_called()
