from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from choicer_voicer_pack_creator import export_cache
from choicer_voicer_pack_creator.export_cache import (
    ExportPromptCache,
    PromptAssetReceipt,
    prompt_audio_key,
    prompt_image_key,
    prompt_recipe,
)
from choicer_voicer_pack_creator.operations import canonical_path

SOURCE = "a" * 64
OUTPUT = "b" * 64


def asset(index=1, kind="audio"):
    return PromptAssetReceipt(
        kind, prompt_recipe(kind), f"{index:064x}",
        f"{index:03d}_Speaker.{'mp3' if kind == 'audio' else 'png'}", OUTPUT,
    )


def receipt(cache, target):
    key = hashlib.sha256(canonical_path(target).encode("utf-8")).hexdigest()
    return cache.root / f"{key}.json"


@pytest.fixture
def cache(tmp_path):
    return ExportPromptCache(tmp_path / "app-data" / "export-cache")


@pytest.fixture
def events(monkeypatch):
    result = []
    monkeypatch.setattr(
        export_cache, "diagnostic_event", lambda event, **details: result.append((event, details)),
    )
    return result


def test_round_trip_is_immutable_bounded_metadata_in_separate_namespace(cache, tmp_path):
    target = tmp_path / "Pack"
    entries = [asset(), asset(kind="image")]
    cache.remember(target, entries)
    data = json.loads(receipt(cache, target).read_bytes())
    assert data == {
        "schema": 1, "target": canonical_path(target), "assets": [asdict(item) for item in entries],
    }
    assert cache.root.name == "prompt-assets"
    assert not target.exists()
    assert receipt(cache, target).stat().st_size < 1024
    reloaded = ExportPromptCache(cache.root.parent)
    found = reloaded.lookup(target / ".." / target.name)
    assert list(found.values()) == entries
    with pytest.raises(TypeError):
        found[("audio", entries[0].key)] = entries[0]


def test_missing_cache_is_normal_read_only_miss(cache, tmp_path, events):
    assert not cache.lookup(tmp_path / "Pack")
    assert not cache.root.exists()
    assert not events


def test_target_binding_and_duplicate_selection(cache, tmp_path):
    target = tmp_path / "Pack"
    cache.remember(target, [asset(), replace(asset(), filename="005_Renamed.mp3")])
    assert list(cache.lookup(target).values()) == [asset()]
    other = tmp_path / "Other"
    receipt(cache, other).write_bytes(receipt(cache, target).read_bytes())
    assert not cache.lookup(other)


@pytest.mark.parametrize("kind", ["audio", "image"])
def test_recipe_changes_and_unknown_recipes_invalidate_only_their_kind(
    cache, tmp_path, monkeypatch, events, kind,
):
    target = tmp_path / "Pack"
    cache.remember(target, [asset(), asset(kind="image")])
    monkeypatch.setattr(export_cache, f"PROMPT_{kind.upper()}_RECIPE", 2)
    found = cache.lookup(target)
    assert [item.kind for item in found.values()] == ["image" if kind == "audio" else "audio"]
    assert events == [("export_prompt_cache_recipe_miss", {"kind": kind})]
    with pytest.raises(ValueError, match="unsupported"):
        cache.remember(target, [asset() if kind == "image" else replace(asset(), recipe=1),
                                replace(asset(kind=kind), recipe=999)])


@pytest.mark.parametrize(
    "payload",
    [b"", b"{", b"[]", b"null", b"\xff", b"[" * 2000,
     b" " * (1024 * 1024 + 1), b'{"schema":1,"schema":1}'],
    ids=["empty", "partial", "array", "null", "non-utf8", "nested", "oversized", "duplicate-field"],
)
def test_bad_payloads_are_diagnosed_misses(cache, tmp_path, events, payload):
    target = tmp_path / "Pack"
    cache.root.mkdir(parents=True)
    receipt(cache, target).write_bytes(payload)
    assert not cache.lookup(target)
    assert events[0][0] == "export_prompt_cache_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema", 999), ("schema", True), ("target", []), ("assets", {}),
     ("extra", 1)],
)
def test_invalid_manifest_fields(cache, tmp_path, events, field, value):
    target = tmp_path / "Pack"
    cache.remember(target, [asset()])
    data = json.loads(receipt(cache, target).read_bytes())
    data[field] = value
    receipt(cache, target).write_text(json.dumps(data), encoding="utf-8")
    assert not cache.lookup(target)
    assert events[0][0] == "export_prompt_cache_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "video"), ("kind", []), ("recipe", True), ("recipe", 0),
     ("key", "g" * 64), ("output_hash", "B" * 64),
     ("filename", "..\\001_Speaker.mp3"), ("filename", "../001_Speaker.mp3"),
     ("filename", "C:\\001_Speaker.mp3"), ("filename", "/001_Speaker.mp3"),
     ("filename", "001_Speaker.mp3:extra"), ("filename", "001_Speaker.png"),
     ("filename", "icon.png"), ("extra", "unexpected")],
)
def test_invalid_asset_fields_never_authorize_media(cache, tmp_path, events, field, value):
    target = tmp_path / "Pack"
    cache.remember(target, [asset()])
    data = json.loads(receipt(cache, target).read_bytes())
    data["assets"][0][field] = value
    receipt(cache, target).write_text(json.dumps(data), encoding="utf-8")
    assert not cache.lookup(target)
    assert events[0][0] == "export_prompt_cache_invalid"


def test_duplicate_asset_keys_in_receipt_are_rejected(cache, tmp_path, events):
    target = tmp_path / "Pack"
    cache.remember(target, [asset()])
    data = json.loads(receipt(cache, target).read_bytes())
    data["assets"] *= 2
    receipt(cache, target).write_text(json.dumps(data), encoding="utf-8")
    assert not cache.lookup(target)
    assert events[0][0] == "export_prompt_cache_invalid"


def test_entry_limit_truncates_without_exhausting_large_input(cache, tmp_path, events):
    target = tmp_path / "Pack"
    consumed = 0

    def entries():
        nonlocal consumed
        for index in range(1, 6000):
            consumed += 1
            yield asset(index)

    cache.remember(target, entries())
    found = cache.lookup(target)
    assert len(found) == 4096
    assert consumed == 4097
    assert receipt(cache, target).stat().st_size <= 1024 * 1024
    assert events[0][0] == "export_prompt_cache_limited"


def test_byte_limit_truncates_without_failing(cache, tmp_path, monkeypatch, events):
    target = tmp_path / "Pack"
    # Generated names of different lengths make the byte limit independent of entry count.
    monkeypatch.setattr(export_cache, "_MAX_PROMPT_RECEIPT_BYTES", 1024)
    cache.remember(target, (asset(index) for index in range(1, 100)))
    assert 0 < len(cache.lookup(target)) < 10
    assert receipt(cache, target).stat().st_size <= 1024
    assert events[0][0] == "export_prompt_cache_limited"


def test_real_one_mib_limit_stops_long_valid_names_before_entry_limit(cache, tmp_path):
    target = tmp_path / "Pack"
    entries = (
        replace(asset(index), filename=f"{index:080d}_{'S' * 32}.mp3")
        for index in range(1, 5000)
    )
    cache.remember(target, entries)
    assert 0 < len(cache.lookup(target)) < 4096
    assert receipt(cache, target).stat().st_size <= 1024 * 1024


def test_unknown_asset_recipe_is_diagnosed_without_losing_other_kind(cache, tmp_path, events):
    target = tmp_path / "Pack"
    cache.remember(target, [asset(), asset(kind="image")])
    data = json.loads(receipt(cache, target).read_bytes())
    data["assets"][0]["recipe"] = 999
    receipt(cache, target).write_text(json.dumps(data), encoding="utf-8")
    assert [item.kind for item in cache.lookup(target).values()] == ["image"]
    assert events == [("export_prompt_cache_recipe_miss", {"kind": "audio"})]


def test_empty_successful_manifest_replaces_previous_generated_entries(cache, tmp_path):
    target = tmp_path / "Pack"
    cache.remember(target, [asset()])
    cache.remember(target, [])
    assert not cache.lookup(target)
    assert json.loads(receipt(cache, target).read_bytes())["assets"] == []


def test_oversized_entry_inventory_is_not_partially_trusted(cache, tmp_path, monkeypatch, events):
    target = tmp_path / "Pack"
    cache.remember(target, [asset(1), asset(2)])
    monkeypatch.setattr(export_cache, "_MAX_PROMPT_ASSETS", 1)
    assert not cache.lookup(target)
    assert events[0][0] == "export_prompt_cache_invalid"


@pytest.mark.parametrize("failure_point", ["replace", "fsync"])
def test_atomic_failure_preserves_receipt_and_other_partials(
    cache, tmp_path, monkeypatch, failure_point,
):
    target = tmp_path / "Pack"
    cache.remember(target, [asset()])
    previous = receipt(cache, target).read_bytes()
    other = cache.root / ".another-writer.partial"
    other.write_bytes(b"not ours")

    def fail(*_args):
        raise PermissionError("receipt unavailable")

    monkeypatch.setattr(export_cache.os, failure_point, fail)
    with pytest.raises(PermissionError, match="unavailable"):
        cache.remember(target, [asset(2)])
    assert receipt(cache, target).read_bytes() == previous
    assert set(cache.root.iterdir()) == {receipt(cache, target), other}


def test_pruning_ignores_other_names_and_keeps_newest(cache, tmp_path):
    cache = ExportPromptCache(cache.root.parent, max_receipts=2)
    cache.root.mkdir(parents=True)
    unrelated = cache.root / "notes.json"
    unrelated.write_bytes(b"untouched")
    directory = cache.root / f"{'f' * 64}.json"
    directory.mkdir()
    for index in range(3):
        target = tmp_path / f"Pack-{index}"
        cache.remember(target, [asset(index + 1)])
        if index < 2:
            os.utime(receipt(cache, target), (index + 1, index + 1))
    assert not cache.lookup(tmp_path / "Pack-0")
    assert cache.lookup(tmp_path / "Pack-1")
    assert cache.lookup(tmp_path / "Pack-2")
    assert unrelated.read_bytes() == b"untouched"
    assert directory.is_dir()


@pytest.mark.parametrize("same_target", [False, True])
def test_concurrent_instances_publish_complete_bounded_manifests(tmp_path, same_target):
    root = tmp_path / "cache"

    def remember(index):
        cache = ExportPromptCache(root, max_receipts=2)
        target = tmp_path / ("Pack" if same_target else f"Pack-{index}")
        cache.remember(target, [asset(index)])

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(remember, range(1, 9)))
    paths = list((root / "prompt-assets").iterdir())
    assert len(paths) == (1 if same_target else 2)
    for path in paths:
        data = json.loads(path.read_bytes())
        assert ExportPromptCache(root).lookup(Path(data["target"]))


def test_keys_are_exact_domain_separated_and_independently_versioned(monkeypatch):
    audio = prompt_audio_key(SOURCE, 1, 2, 0.15, 0.25)
    image = prompt_image_key(SOURCE, 1.5, 640, 360)
    assert audio != image
    assert audio == prompt_audio_key(SOURCE, 1.0, 2.0, 0.15, 0.25)
    assert audio != prompt_audio_key(SOURCE, 1, 2 + 1e-8, 0.15, 0.25)
    assert audio != prompt_audio_key(OUTPUT, 1, 2, 0.15, 0.25)
    assert audio != prompt_audio_key(SOURCE, 1, 2, 0.1, 0.25)
    assert image != prompt_image_key(SOURCE, 1.5, 640, 480)
    monkeypatch.setattr(export_cache, "PROMPT_AUDIO_RECIPE", 2)
    assert audio != prompt_audio_key(SOURCE, 1, 2, 0.15, 0.25)
    assert image == prompt_image_key(SOURCE, 1.5, 640, 360)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "1"])
def test_bad_key_inputs_are_rejected(value):
    with pytest.raises(ValueError):
        prompt_audio_key(SOURCE, value, 2, 0.1, 0.25)
    with pytest.raises(ValueError):
        prompt_image_key(SOURCE, value, 640, 360)
