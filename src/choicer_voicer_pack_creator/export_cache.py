"""Small reuse receipts, never video copies or replacements for export validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.diagnostics import diagnostic_event
from choicer_voicer_pack_creator.operations import canonical_path, path_leases

_SCHEMA_VERSION = 1
# Bump whenever the conversion command changes, including libtheora q7/libvorbis q5.
VIDEO_ENCODING_RECIPE = 1
_MAX_RECEIPT_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECEIPT_NAME = re.compile(r"[0-9a-f]{64}\.json")
_FIELDS = {"schema", "recipe", "target", "source_hash", "height", "fps", "video_hash"}


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_positive_integer(value: Any) -> bool:
    return type(value) is int and value > 0


def _valid_receipt(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() == _FIELDS
        and type(value["schema"]) is int
        and value["schema"] == _SCHEMA_VERSION
        and type(value["recipe"]) is int
        and value["recipe"] == VIDEO_ENCODING_RECIPE
        and isinstance(value["target"], str)
        and bool(value["target"])
        and "\0" not in value["target"]
        and _is_hash(value["source_hash"])
        and _is_hash(value["video_hash"])
        and _is_positive_integer(value["height"])
        and _is_positive_integer(value["fps"])
    )


class ExportVideoCache:
    """Remember fingerprints of videos published to a particular pack folder.

    The caller owns the output lease and must hash that folder's ``dub_video.ogv``
    before reusing it, then fully validate the staged and published export.
    ``root`` belongs in application data, outside source and exported packs.
    """

    def __init__(self, root: Path, *, max_receipts: int = 128) -> None:
        if not _is_positive_integer(max_receipts):
            raise ValueError("max_receipts must be a positive integer")
        self.root = Path(canonical_path(root))
        self.max_receipts = max_receipts
        self._resource_key = f"export-video-cache:{self.root}"

    def _receipt_path(self, target: str) -> Path:
        key = hashlib.sha256(target.encode("utf-8")).hexdigest()
        return self.root / f"{key}.json"

    @staticmethod
    def _check_inputs(source_hash: str, height: int, fps: int) -> None:
        if not _is_hash(source_hash):
            raise ValueError("source_hash must be a lowercase SHA-256 digest")
        if not _is_positive_integer(height) or not _is_positive_integer(fps):
            raise ValueError("height and fps must be positive integers")

    def lookup(self, target: Path, source_hash: str, height: int, fps: int) -> str | None:
        """Return the expected video SHA-256, or miss; never trust a receipt path."""
        self._check_inputs(source_hash, height, fps)
        target_key = canonical_path(target)
        receipt = self._receipt_path(target_key)
        with path_leases(resource_keys=[self._resource_key]):
            try:
                with receipt.open("rb") as stream:
                    payload = stream.read(_MAX_RECEIPT_BYTES + 1)
            except FileNotFoundError:
                return None
        try:
            if len(payload) > _MAX_RECEIPT_BYTES:
                raise ValueError("Receipt exceeds the size limit")
            value = json.loads(payload.decode("utf-8"))
            if not _valid_receipt(value):
                raise ValueError("Invalid receipt fields or unsupported schema/encoding recipe")
        except (ValueError, UnicodeError, RecursionError) as error:
            diagnostic_event("export_video_cache_invalid", receipt=receipt, reason=str(error))
            return None
        if (
            value["target"] != target_key
            or value["source_hash"] != source_hash
            or value["height"] != height
            or value["fps"] != fps
        ):
            return None
        return value["video_hash"]

    def remember(
        self, target: Path, source_hash: str, height: int, fps: int, video_hash: str,
    ) -> None:
        """Atomically save a receipt for a successfully validated publication."""
        self._check_inputs(source_hash, height, fps)
        if not _is_hash(video_hash):
            raise ValueError("video_hash must be a lowercase SHA-256 digest")
        target_key = canonical_path(target)
        receipt = self._receipt_path(target_key)
        value = {
            "schema": _SCHEMA_VERSION,
            "recipe": VIDEO_ENCODING_RECIPE,
            "target": target_key,
            "source_hash": source_hash,
            "height": height,
            "fps": fps,
            "video_hash": video_hash,
        }
        payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8",
        )
        if len(payload) > _MAX_RECEIPT_BYTES:
            raise ValueError("Receipt exceeds the size limit")
        with path_leases(resource_keys=[self._resource_key]):
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.root / f".{receipt.stem}-{uuid.uuid4().hex}.partial"
            owned = False
            try:
                with temporary.open("xb") as stream:
                    owned = True
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, receipt)
            finally:
                if owned:
                    temporary.unlink(missing_ok=True)
            self._prune()

    def _prune(self) -> None:
        receipts = []
        for path in self.root.iterdir():
            if _RECEIPT_NAME.fullmatch(path.name) is None:
                continue
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                receipts.append((metadata.st_mtime_ns, path.name, path))
        for _, _, path in sorted(receipts)[:max(0, len(receipts) - self.max_receipts)]:
            path.unlink(missing_ok=True)
