"""Optional, local-only inference for the singing-aware Facing the Music BandIt.

Importing this package does not import its optional ML dependencies. Source is
Apache-2.0; the separately obtained weights are CC-BY-NC-4.0, not the app's license.
See LICENSE and provenance.json in this directory.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import torch

SOURCE_REPOSITORY = "https://github.com/kwatcharasupat/bandit-v2"
SOURCE_REVISION = "d5563d9031e95fdaa3e5a73d5020b9a0df61adb6"
WEIGHTS_RECORD_URL = "https://zenodo.org/records/13327983"
WEIGHTS_LICENSE = "CC-BY-NC-4.0"
WEIGHTS_LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"
SAMPLE_RATE = 48_000


@dataclass(frozen=True)
class CheckpointSpec:
    filename: str
    url: str
    size_bytes: int
    md5: str
    stems: tuple[str, ...]
    sha256: str | None = None


COMBINED_CHECKPOINT = CheckpointSpec(
    filename="bandit-combined.ckpt",
    url=f"{WEIGHTS_RECORD_URL}/files/bandit-combined.ckpt?download=1",
    size_bytes=446_680_129,
    md5="d04760e77bb947668d8f5582d36b45a0",
    stems=("speech", "music", "sfx"),
    sha256="ebcd8a3c8c783aa8f3379c0cab925b76987f4f8959dfb595a84426817e1ffb60",
)
SPLIT_CHECKPOINT = CheckpointSpec(
    filename="bandit-split.ckpt",
    url=f"{WEIGHTS_RECORD_URL}/files/bandit-split.ckpt?download=1",
    size_bytes=551_004_479,
    md5="e9f9054a452cb86b30a6f705266dbd7c",
    stems=("speech", "music_instrumental", "music_vocals", "sfx"),
)


def verify_checkpoint(
    path: str | PathLike[str], *, split: bool = False
) -> CheckpointSpec:
    """Check the length, published MD5 and pinned SHA-256 before loading Torch.

    MD5 is the publisher's file-integrity checksum, not a cryptographic signature.
    SHA-256 is pinned from the independently verified combined checkpoint.
    Obtain weights from the recorded HTTPS URL. Split loading is disabled until
    a verified SHA-256 is also pinned for that checkpoint.
    """
    path = Path(path)
    spec = SPLIT_CHECKPOINT if split else COMBINED_CHECKPOINT
    if spec.sha256 is None:
        raise ValueError(
            f"{spec.filename} has no pinned SHA-256; use the verified combined checkpoint."
        )
    size = path.stat().st_size
    if size != spec.size_bytes:
        raise ValueError(
            f"Invalid {spec.filename} size: expected {spec.size_bytes} bytes, got {size}."
        )
    digest = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as checkpoint_file:
        while chunk := checkpoint_file.read(1024 * 1024):
            digest.update(chunk)
            sha256.update(chunk)
    actual_sha256 = sha256.hexdigest()
    if actual_sha256 != spec.sha256:
        raise ValueError(
            f"Invalid {spec.filename} SHA-256: expected {spec.sha256}, got {actual_sha256}."
        )
    actual_md5 = digest.hexdigest()
    if actual_md5 != spec.md5:
        raise ValueError(
            f"Invalid {spec.filename} MD5: expected {spec.md5}, got {actual_md5}."
        )
    return spec


def load_model(
    path: str | PathLike[str], device: str | torch.device, *, split: bool = False
) -> torch.nn.Module:
    """Load only the verified model state, strictly, with no pickle fallback.

    Use a separate environment with the optional Torch, TorchAudio, NumPy and
    librosa dependencies. This function does not download weights or install
    packages. The model is returned in evaluation mode on ``device``.

    Under ``torch.inference_mode()``, call
    ``model({"mixture": {"audio": mono_audio}})`` with float32 audio of shape
    ``[batch, 1, samples]`` at 48 kHz. Read
    ``result["estimates"][stem]["audio"]``. Combined ``music`` includes singing;
    split loading is disabled pending its own verified SHA-256 pin.
    Resampling, channel handling and chunk overlap are the caller's responsibility.
    """
    path = Path(path)
    spec = verify_checkpoint(path, split=split)

    import torch

    from .models.bandit.bandit import Bandit

    checkpoint = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
    if not isinstance(checkpoint, Mapping) or not isinstance(
        checkpoint.get("state_dict"), Mapping
    ):
        raise ValueError("BandIt checkpoint must contain a state_dict mapping.")
    state_dict = {
        key.removeprefix("model."): value
        for key, value in checkpoint["state_dict"].items()
        if isinstance(key, str) and key.startswith("model.")
    }
    del checkpoint
    if not state_dict:
        raise ValueError("BandIt checkpoint contains no model.* state_dict entries.")

    model = Bandit(
        in_channels=1,
        stems=list(spec.stems),
        fs=SAMPLE_RATE,
        band_type="musical",
        n_bands=64,
        require_no_overlap=False,
        require_no_gap=True,
        normalize_channel_independently=False,
        treat_channel_as_feature=True,
        n_sqm_modules=8,
        emb_dim=128,
        rnn_dim=256,
        bidirectional=True,
        rnn_type="GRU",
        mlp_dim=512,
        hidden_activation="Tanh",
        hidden_activation_kwargs=None,
        complex_mask=True,
        use_freq_weights=True,
        n_fft=2048,
        win_length=2048,
        hop_length=512,
        window_fn="hann_window",
        wkwargs=None,
        power=None,
        center=True,
        normalized=True,
        pad_mode="reflect",
        onesided=True,
    )
    model.load_state_dict(state_dict, strict=True)
    del state_dict
    return model.eval().to(device)
