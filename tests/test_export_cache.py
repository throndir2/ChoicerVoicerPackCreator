from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from choicer_voicer_pack_creator import export_cache
from choicer_voicer_pack_creator.export_cache import ExportVideoCache
from choicer_voicer_pack_creator.operations import canonical_path

SOURCE_HASH = "a" * 64
VIDEO_HASH = "b" * 64
OTHER_HASH = "c" * 64


def _receipt(root: Path, target: Path) -> Path:
    digest = hashlib.sha256(canonical_path(target).encode("utf-8")).hexdigest()
    return root / f"{digest}.json"


@pytest.fixture
def cache(tmp_path):
    return ExportVideoCache(tmp_path / "app-data" / "export-cache")


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        export_cache, "diagnostic_event", lambda event, **details: recorded.append((event, details)),
    )
    return recorded


def test_receipt_persists_only_hashes_and_recipe_for_canonical_destination(cache, tmp_path, events):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)

    receipt = _receipt(cache.root, target)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data == {
        "schema": 1,
        "recipe": export_cache.VIDEO_ENCODING_RECIPE,
        "target": canonical_path(target),
        "source_hash": SOURCE_HASH,
        "height": 720,
        "fps": 30,
        "video_hash": VIDEO_HASH,
    }
    assert list(cache.root.iterdir()) == [receipt]
    assert receipt.stat().st_size < 2048
    assert not target.exists()
    reloaded = ExportVideoCache(cache.root)
    alias = target / ".." / target.name
    assert reloaded.lookup(alias, SOURCE_HASH, 720, 30) == VIDEO_HASH
    assert events == []


@pytest.mark.parametrize(
    ("source_hash", "height", "fps", "different_target"),
    [(OTHER_HASH, 720, 30, False), (SOURCE_HASH, 480, 30, False),
     (SOURCE_HASH, 720, 24, False), (SOURCE_HASH, 720, 30, True)],
)
def test_changed_source_profile_or_destination_misses(
    cache, tmp_path, events, source_hash, height, fps, different_target,
):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    requested = tmp_path / "other-pack" if different_target else target
    assert cache.lookup(requested, source_hash, height, fps) is None
    assert events == []


def test_receipt_copied_to_different_destination_does_not_match(cache, tmp_path):
    target = tmp_path / "pack"
    other = tmp_path / "other-pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    _receipt(cache.root, other).write_bytes(_receipt(cache.root, target).read_bytes())
    assert cache.lookup(other, SOURCE_HASH, 720, 30) is None


@pytest.mark.parametrize("field", ["schema", "recipe"])
@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_unknown_or_invalid_versions_are_diagnosed(cache, tmp_path, events, field, version):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data[field] = version
    receipt.write_text(json.dumps(data), encoding="utf-8")
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert [event for event, _ in events] == ["export_video_cache_invalid"]


def test_encoding_recipe_bump_invalidates_previous_receipt(cache, tmp_path, monkeypatch, events):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    monkeypatch.setattr(export_cache, "VIDEO_ENCODING_RECIPE", 2)
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert events[0][0] == "export_video_cache_invalid"


@pytest.mark.parametrize(
    "payload",
    [b"", b"{", b'{"schema":1,', b"[]", b"null", b"42", b'"text"', b"\xff",
     b"{}", b'{"schema":1}', b" " * (64 * 1024 + 1), b"[" * 2000],
    ids=[
        "empty", "opening-brace", "partial-object", "array", "null", "number", "string",
        "non-utf8", "empty-object", "missing-fields", "oversized", "deeply-nested",
    ],
)
def test_malformed_partial_or_oversized_receipts_are_diagnosed(
    cache, tmp_path, events, payload,
):
    target = tmp_path / "pack"
    cache.root.mkdir(parents=True)
    _receipt(cache.root, target).write_bytes(payload)
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert [event for event, _ in events] == ["export_video_cache_invalid"]


@pytest.mark.parametrize("field", ["source_hash", "video_hash"])
@pytest.mark.parametrize("value", [None, 123, True, [], {}, "", "a" * 63, "b" * 65, "g" * 64, "A" * 64])
def test_corrupt_hash_fields_are_diagnosed(cache, tmp_path, events, field, value):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data[field] = value
    receipt.write_text(json.dumps(data), encoding="utf-8")
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert events[0][0] == "export_video_cache_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [("target", None), ("target", []), ("target", ""), ("target", "\0"),
     ("height", True), ("height", "720"), ("height", 0), ("height", 720.0),
     ("fps", False), ("fps", "30"), ("fps", -1), ("fps", 30.0)],
)
def test_invalid_target_or_profile_fields_are_diagnosed(cache, tmp_path, events, field, value):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data[field] = value
    receipt.write_text(json.dumps(data), encoding="utf-8")
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert events[0][0] == "export_video_cache_invalid"


def test_arbitrary_path_from_receipt_is_never_followed(cache, tmp_path, events):
    target = tmp_path / "pack"
    unrelated = tmp_path / "unrelated.ogv"
    unrelated.write_bytes(b"not a cache file")
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["video_path"] = str(unrelated)
    receipt.write_text(json.dumps(data), encoding="utf-8")
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert unrelated.read_bytes() == b"not a cache file"
    assert events[0][0] == "export_video_cache_invalid"


def test_missing_root_or_receipt_is_normal_miss_without_creating_files(cache, tmp_path, events):
    target = tmp_path / "pack"
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert not cache.root.exists()
    cache.root.mkdir(parents=True)
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert list(cache.root.iterdir()) == []
    assert events == []


def test_oldest_receipts_are_evicted_without_touching_unrelated_entries(cache, tmp_path):
    cache = ExportVideoCache(cache.root, max_receipts=2)
    cache.root.mkdir(parents=True)
    unrelated = [
        cache.root / "notes.json", cache.root / "video.ogv",
        cache.root / f"{'a' * 64}.json.backup",
        cache.root / ".other-writer.partial", cache.root / f"{'z' * 64}.json",
    ]
    for path in unrelated:
        path.write_bytes(b"unrelated")
    directory = cache.root / f"{'d' * 64}.json"
    directory.mkdir()
    targets = [tmp_path / f"pack-{index}" for index in range(3)]
    cache.remember(targets[0], SOURCE_HASH, 720, 30, VIDEO_HASH)
    cache.remember(targets[1], SOURCE_HASH, 720, 30, VIDEO_HASH)
    os.utime(_receipt(cache.root, targets[0]), (1, 1))
    os.utime(_receipt(cache.root, targets[1]), (2, 2))
    cache.remember(targets[2], SOURCE_HASH, 720, 30, VIDEO_HASH)
    assert cache.lookup(targets[0], SOURCE_HASH, 720, 30) is None
    assert cache.lookup(targets[1], SOURCE_HASH, 720, 30) == VIDEO_HASH
    assert cache.lookup(targets[2], SOURCE_HASH, 720, 30) == VIDEO_HASH
    assert all(path.read_bytes() == b"unrelated" for path in unrelated)
    assert directory.is_dir()


def test_default_retention_is_bounded_to_128_receipts(cache, tmp_path):
    cache.root.mkdir(parents=True)
    for index in range(128):
        path = cache.root / f"{index:064x}.json"
        path.write_bytes(b"{}")
        os.utime(path, (index + 1, index + 1))
    target = tmp_path / "new-pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    assert len(list(cache.root.iterdir())) == 128
    assert not (cache.root / f"{0:064x}.json").exists()
    assert cache.lookup(target, SOURCE_HASH, 720, 30) == VIDEO_HASH


def test_repeated_remember_replaces_one_receipt(cache, tmp_path):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    cache.remember(target, OTHER_HASH, 480, 24, OTHER_HASH)
    assert list(cache.root.iterdir()) == [_receipt(cache.root, target)]
    assert cache.lookup(target, SOURCE_HASH, 720, 30) is None
    assert cache.lookup(target, OTHER_HASH, 480, 24) == OTHER_HASH


@pytest.mark.parametrize("failure_point", ["replace", "fsync"])
def test_atomic_failure_preserves_old_receipt_and_cleans_owned_partial(
    cache, tmp_path, monkeypatch, failure_point,
):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    previous = receipt.read_bytes()
    other_partial = cache.root / ".another-writer.partial"
    other_partial.write_bytes(b"not ours")

    def fail(*_args):
        raise PermissionError("simulated write failure")

    monkeypatch.setattr(export_cache.os, failure_point, fail)
    with pytest.raises(PermissionError, match="simulated write failure"):
        cache.remember(target, SOURCE_HASH, 720, 30, OTHER_HASH)
    assert receipt.read_bytes() == previous
    assert set(cache.root.iterdir()) == {receipt, other_partial}
    assert other_partial.read_bytes() == b"not ours"
    assert cache.lookup(target, SOURCE_HASH, 720, 30) == VIDEO_HASH


def test_atomic_replace_sees_complete_receipt_while_previous_remains_readable(
    cache, tmp_path, monkeypatch,
):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    previous = receipt.read_bytes()
    replace = os.replace
    partials = []

    def observe(source, destination):
        partials.append(source)
        assert source.parent == cache.root
        assert destination == receipt
        assert receipt.read_bytes() == previous
        assert json.loads(source.read_text(encoding="utf-8"))["video_hash"] == OTHER_HASH
        replace(source, destination)

    monkeypatch.setattr(export_cache.os, "replace", observe)
    cache.remember(target, SOURCE_HASH, 720, 30, OTHER_HASH)
    assert len(partials) == 1
    assert not partials[0].exists()
    assert cache.lookup(target, SOURCE_HASH, 720, 30) == OTHER_HASH


@pytest.mark.parametrize("operation", ["lookup", "remember"])
def test_file_open_errors_propagate(cache, tmp_path, monkeypatch, events, operation):
    target = tmp_path / "pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    original_open = Path.open

    def fail(path, *args, **kwargs):
        if path.parent == cache.root:
            raise PermissionError("cache file is unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(PermissionError, match="cache file is unreadable"):
        if operation == "lookup":
            cache.lookup(target, SOURCE_HASH, 720, 30)
        else:
            cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    assert events == []


def test_root_creation_error_propagates(cache, tmp_path):
    cache.root.parent.mkdir(parents=True)
    cache.root.write_bytes(b"not a directory")
    with pytest.raises(FileExistsError):
        cache.remember(tmp_path / "pack", SOURCE_HASH, 720, 30, VIDEO_HASH)


@pytest.mark.parametrize("failure_point", ["iterdir", "lstat", "unlink"])
def test_pruning_errors_propagate(cache, tmp_path, monkeypatch, failure_point):
    cache = ExportVideoCache(cache.root, max_receipts=1)
    target = tmp_path / "old-pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    os.utime(receipt, ns=(1, 1))
    original = getattr(Path, failure_point)

    def fail(path, *args, **kwargs):
        denied = path == cache.root if failure_point == "iterdir" else path == receipt
        if denied:
            raise PermissionError("pruning denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, failure_point, fail)
    with pytest.raises(PermissionError, match="pruning denied"):
        cache.remember(tmp_path / "new-pack", SOURCE_HASH, 720, 30, VIDEO_HASH)


@pytest.mark.parametrize("failure_point", ["lstat", "unlink"])
def test_concurrent_pruner_removing_receipt_is_harmless(cache, tmp_path, monkeypatch, failure_point):
    cache = ExportVideoCache(cache.root, max_receipts=1)
    target = tmp_path / "old-pack"
    cache.remember(target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    receipt = _receipt(cache.root, target)
    os.utime(receipt, ns=(1, 1))
    original = getattr(Path, failure_point)
    unlink = Path.unlink

    def remove_first(path, *args, **kwargs):
        if path == receipt:
            unlink(path, missing_ok=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, failure_point, remove_first)
    new_target = tmp_path / "new-pack"
    cache.remember(new_target, SOURCE_HASH, 720, 30, VIDEO_HASH)
    assert cache.lookup(new_target, SOURCE_HASH, 720, 30) == VIDEO_HASH
    assert list(cache.root.iterdir()) == [_receipt(cache.root, new_target)]


def test_concurrent_instances_publish_complete_receipts_and_remain_bounded(cache, tmp_path):
    def remember(index):
        instance = ExportVideoCache(cache.root, max_receipts=4)
        instance.remember(tmp_path / f"pack-{index}", SOURCE_HASH, 720, 30, VIDEO_HASH)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(remember, range(20)))
    paths = list(cache.root.iterdir())
    assert len(paths) == 4
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["video_hash"] == VIDEO_HASH
        assert cache.lookup(Path(data["target"]), SOURCE_HASH, 720, 30) == VIDEO_HASH


def test_concurrent_writers_to_same_destination_leave_one_complete_receipt(cache, tmp_path):
    target = tmp_path / "pack"

    def remember(index):
        instance = ExportVideoCache(cache.root)
        instance.remember(target, SOURCE_HASH, 720, 30, f"{index:064x}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(remember, range(20)))
    assert list(cache.root.iterdir()) == [_receipt(cache.root, target)]
    assert cache.lookup(target, SOURCE_HASH, 720, 30) in {f"{index:064x}" for index in range(20)}


@pytest.mark.parametrize("max_receipts", [0, -1, True, 1.5])
def test_invalid_retention_limit_is_rejected(tmp_path, max_receipts):
    with pytest.raises(ValueError, match="max_receipts"):
        ExportVideoCache(tmp_path, max_receipts=max_receipts)


@pytest.mark.parametrize(
    ("source_hash", "height", "fps", "video_hash"),
    [("invalid", 720, 30, VIDEO_HASH), (SOURCE_HASH, False, 30, VIDEO_HASH),
     (SOURCE_HASH, 720, 0, VIDEO_HASH), (SOURCE_HASH, 720, 30, "invalid")],
)
def test_invalid_remember_inputs_do_not_create_cache(cache, tmp_path, source_hash, height, fps, video_hash):
    with pytest.raises(ValueError):
        cache.remember(tmp_path / "pack", source_hash, height, fps, video_hash)
    assert not cache.root.exists()
