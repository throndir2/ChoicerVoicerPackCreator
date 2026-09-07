"""Small reuse receipts, never media copies or replacements for export validation."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from choicer_voicer_pack_creator.diagnostics import diagnostic_event
from choicer_voicer_pack_creator.operations import canonical_path, check_cancelled, path_leases

_SCHEMA_VERSION = 1
# Bump whenever the conversion command changes, including libtheora q7/libvorbis q5.
VIDEO_ENCODING_RECIPE = 1
_MAX_RECEIPT_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECEIPT_NAME = re.compile(r"[0-9a-f]{64}\.json")
_FIELDS = {"schema", "recipe", "target", "source_hash", "height", "fps", "video_hash"}
_PROMPT_SCHEMA_VERSION = 1
# Bump for changes to extract_prompt or its source-activity policy.
PROMPT_AUDIO_RECIPE = 1
# Bump for changes to generated frame seeking, scale/crop, or PNG encoding.
PROMPT_IMAGE_RECIPE = 1
_MAX_PROMPT_RECEIPT_BYTES = 1024 * 1024
_MAX_PROMPT_ASSETS = 4096
_PROMPT_FIELDS = {"schema", "target", "assets"}
_ASSET_FIELDS = {"kind", "recipe", "key", "filename", "output_hash"}
_PROMPT_FILENAME = re.compile(r"[0-9]{3,}_[A-Za-z0-9-]{1,32}\.(mp3|png)")
PromptAssetKind = Literal["audio", "image"]


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_positive_integer(value: Any) -> bool:
    return type(value) is int and value > 0


def _replace_receipt(receipt: Path, payload: bytes | bytearray) -> None:
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = receipt.with_name(f".{receipt.stem}-{uuid.uuid4().hex}.partial")
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
            _replace_receipt(receipt, payload)
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


def prompt_recipe(kind: PromptAssetKind) -> int:
    if kind == "audio":
        return PROMPT_AUDIO_RECIPE
    if kind == "image":
        return PROMPT_IMAGE_RECIPE
    raise ValueError(f"Unknown prompt asset kind: {kind}")


def _time_key(value: float) -> str:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Prompt times must be finite non-negative numbers")
    return float(value or 0.0).hex()


def _prompt_key(kind: PromptAssetKind, source_hash: str, *inputs: str | int) -> str:
    if not _is_hash(source_hash):
        raise ValueError("source_hash must be a lowercase SHA-256 digest")
    payload = json.dumps(
        [kind, prompt_recipe(kind), source_hash, *inputs], separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def prompt_audio_key(
    source_hash: str, start: float, end: float, actual_head: float, actual_tail: float,
) -> str:
    times = tuple(_time_key(value) for value in (start, end, actual_head, actual_tail))
    if end <= start or actual_head > start:
        raise ValueError("Invalid prompt range or effective head padding")
    return _prompt_key("audio", source_hash, *times)


def prompt_image_key(source_hash: str, midpoint: float, width: int, height: int) -> str:
    if not _is_positive_integer(width) or not _is_positive_integer(height):
        raise ValueError("Prompt image dimensions must be positive integers")
    return _prompt_key("image", source_hash, _time_key(midpoint), width, height)


def _valid_prompt_asset(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() == _ASSET_FIELDS
        and isinstance(value["kind"], str)
        and value["kind"] in {"audio", "image"}
        and _is_positive_integer(value["recipe"])
        and _is_hash(value["key"])
        and _is_hash(value["output_hash"])
        and isinstance(value["filename"], str)
        and len(value["filename"]) <= 128
        and _PROMPT_FILENAME.fullmatch(value["filename"]) is not None
        and value["filename"].endswith(".mp3" if value["kind"] == "audio" else ".png")
    )


@dataclass(frozen=True, slots=True)
class PromptAssetReceipt:
    kind: PromptAssetKind
    recipe: int
    key: str
    filename: str
    output_hash: str

    def __post_init__(self) -> None:
        if not _valid_prompt_asset(asdict(self)):
            raise ValueError("Invalid prompt asset receipt")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate receipt field")
        value[key] = item
    return value


class ExportPromptCache:
    """Bounded destination-local provenance for previously published prompt assets.

    The caller owns the destination lease, verifies old and copied media hashes, and
    validates the complete export. Only successfully published assets may be remembered.
    """

    def __init__(self, root: Path, *, max_receipts: int = 128) -> None:
        if not _is_positive_integer(max_receipts):
            raise ValueError("max_receipts must be a positive integer")
        self.root = Path(canonical_path(root)) / "prompt-assets"
        self.max_receipts = max_receipts
        self._resource_key = f"export-prompt-cache:{self.root}"

    def _receipt_path(self, target: str) -> Path:
        key = hashlib.sha256(target.encode("utf-8")).hexdigest()
        return self.root / f"{key}.json"

    def lookup(self, target: Path) -> Mapping[tuple[PromptAssetKind, str], PromptAssetReceipt]:
        target_key = canonical_path(target)
        receipt = self._receipt_path(target_key)
        with path_leases(resource_keys=[self._resource_key]):
            if receipt.is_symlink():
                diagnostic_event("export_prompt_cache_invalid", reason="Symlink receipt")
                return MappingProxyType({})
            try:
                with receipt.open("rb") as stream:
                    payload = stream.read(_MAX_PROMPT_RECEIPT_BYTES + 1)
            except FileNotFoundError:
                return MappingProxyType({})
        try:
            if len(payload) > _MAX_PROMPT_RECEIPT_BYTES:
                raise ValueError("Prompt receipt exceeds the size limit")
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
            if not (
                isinstance(value, dict)
                and value.keys() == _PROMPT_FIELDS
                and type(value["schema"]) is int
                and value["schema"] == _PROMPT_SCHEMA_VERSION
                and isinstance(value["target"], str)
                and value["target"]
                and "\0" not in value["target"]
                and isinstance(value["assets"], list)
                and len(value["assets"]) <= _MAX_PROMPT_ASSETS
            ):
                raise ValueError("Invalid prompt receipt fields or unsupported schema")
            if value["target"] != target_key:
                return MappingProxyType({})
            assets = {}
            seen = set()
            unsupported = set()
            for item in value["assets"]:
                check_cancelled()
                if not _valid_prompt_asset(item):
                    raise ValueError("Invalid prompt asset fields")
                asset = PromptAssetReceipt(**item)
                key = (asset.kind, asset.key)
                if key in seen:
                    raise ValueError("Duplicate prompt asset key")
                seen.add(key)
                if asset.recipe != prompt_recipe(asset.kind):
                    unsupported.add(asset.kind)
                else:
                    assets[key] = asset
        except (ValueError, UnicodeError, RecursionError) as error:
            diagnostic_event("export_prompt_cache_invalid", receipt=receipt, reason=str(error))
            return MappingProxyType({})
        for kind in sorted(unsupported):
            diagnostic_event("export_prompt_cache_recipe_miss", kind=kind)
        return MappingProxyType(assets)

    def remember(self, target: Path, entries: Iterable[PromptAssetReceipt]) -> None:
        target_key = canonical_path(target)
        receipt = self._receipt_path(target_key)
        header = json.dumps(
            {"schema": _PROMPT_SCHEMA_VERSION, "target": target_key},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        payload = bytearray(header[:-1] + b',"assets":[')
        ending = b"]}\n"
        if len(payload) + len(ending) > _MAX_PROMPT_RECEIPT_BYTES:
            diagnostic_event("export_prompt_cache_limited", reason="Destination exceeds receipt limit")
            return
        seen = set()
        for asset in entries:
            check_cancelled()
            if asset.recipe != prompt_recipe(asset.kind):
                raise ValueError("Cannot remember an unsupported prompt recipe")
            key = (asset.kind, asset.key)
            if key in seen:
                continue
            encoded = json.dumps(
                asdict(asset), ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            if (
                len(seen) >= _MAX_PROMPT_ASSETS
                or len(payload) + bool(seen) + len(encoded) + len(ending) > _MAX_PROMPT_RECEIPT_BYTES
            ):
                diagnostic_event("export_prompt_cache_limited", asset_count=len(seen))
                break
            if seen:
                payload.extend(b",")
            payload.extend(encoded)
            seen.add(key)
        payload.extend(ending)
        with path_leases(resource_keys=[self._resource_key]):
            _replace_receipt(receipt, payload)
            self._prune()

    def _prune(self) -> None:
        retained: list[tuple[int, str, Path]] = []
        for path in self.root.iterdir():
            check_cancelled()
            if _RECEIPT_NAME.fullmatch(path.name) is None:
                continue
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            item = (metadata.st_mtime_ns, path.name, path)
            if len(retained) < self.max_receipts:
                heapq.heappush(retained, item)
            else:
                _, _, expired = heapq.heappushpop(retained, item)
                expired.unlink(missing_ok=True)
