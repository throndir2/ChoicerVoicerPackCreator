from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from choicer_voicer_pack_creator.automation import (
    HeadlessProjectAccess,
    PackAutomation,
    ProjectPatch,
    ProjectSnapshot,
    SegmentPatch,
)
from choicer_voicer_pack_creator.media import MediaInfo
from choicer_voicer_pack_creator.models import AnalysisReview, PackProject, Segment, SourceCaption
from choicer_voicer_pack_creator.project_io import ProjectStore


class StubMedia:
    def probe(self, _source: Path) -> MediaInfo:
        return MediaInfo(10, 320, 180, 30, True, "mpeg4", "aac", "yuv420p", 48000, 1)


@pytest.fixture
def automation(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source fixture")
    service = PackAutomation(HeadlessProjectAccess(), tmp_path, StubMedia())
    service.new_project(str(source), "Test Pack", ["Tester"])
    return service


def test_batch_edits_are_atomic_and_revisions_reject_stale_writers(automation):
    before = automation.get_project()
    with pytest.raises(ValueError, match="start < end"):
        automation.edit_segments([
            SegmentPatch(start=1, end=2, caption="Valid"),
            SegmentPatch(start=3, end=2),
        ], [], before["revision"])
    assert automation.get_project() == before
    after = automation.edit_segments([
        SegmentPatch(start=1, end=2, caption="First", characters=["Alice"]),
        SegmentPatch(start=1, end=2, caption="Second", characters=["Bob"]),
    ], [], before["revision"])
    assert len(after["changed_ids"]) == 2
    assert automation.validate_project()["overlaps"] == []
    with pytest.raises(ValueError, match="Project changed"):
        automation.update_project(ProjectPatch(title="Stale"), before["revision"])
    with pytest.raises(ValueError, match="Unknown"):
        automation.edit_segments([], ["nonexistent"], after["revision"])
    assert automation.get_project()["revision"] == after["revision"]
    segment_id = after["changed_ids"][0]
    edited = automation.edit_segments(
        [SegmentPatch(id=segment_id, caption="Corrected")], [], after["revision"]
    )
    assert edited["segments"][0]["caption"] == "Corrected"
    deleted = automation.edit_segments([], [segment_id], edited["revision"])
    assert deleted["total_segments"] == 1


@pytest.mark.parametrize("data", [
    {"start": float("nan")}, {"end": float("inf")}, {"start": -1}, {"start": True},
    {"caption": None}, {"characters": "Alice"}, {"audio_mode": "stem"}, {"surprise": 1},
])
def test_segment_schema_rejects_invalid_values(data):
    with pytest.raises(ValidationError):
        SegmentPatch(**data)


@pytest.mark.parametrize("data", [
    {"head_padding": 3}, {"head_padding": float("nan")}, {"video_height": 5},
    {"video_fps": 0}, {"title": None}, {"video_duration": 1000},
])
def test_project_patch_rejects_unsupported_or_invalid_values(data):
    with pytest.raises(ValidationError):
        ProjectPatch(**data)


def test_drafts_pagination_and_save_reopen(automation, tmp_path):
    snapshot = automation.get_project()
    edited = automation.edit_segments([
        SegmentPatch(start=1, end=2),
        SegmentPatch(start=3, end=4),
    ], [], snapshot["revision"])
    assert not automation.validate_project()["valid"]
    first_page = automation.get_project(limit=1)
    assert first_page["next_offset"] == 1
    assert automation.get_project(offset=1, limit=1)["next_offset"] is None
    destination = tmp_path / "Draft.cvpack.json"
    saved = automation.save_project(edited["revision"], str(destination))
    assert not saved["dirty"]
    assert '"video_path": "source.mp4"' in destination.read_text()
    reopened = automation.open_project(str(destination))
    assert reopened == saved
    changed = automation.update_project(ProjectPatch(readme="A note"), saved["revision"])
    automation.save_project(changed["revision"])
    assert ProjectStore.previous_path(destination).is_file()
    assert ProjectStore.load(destination).readme == "A note"


def test_open_existing_document_does_not_reload_an_externally_removed_file(automation, tmp_path):
    before = automation.get_project()
    path = tmp_path / "existing.cvpack.json"
    saved = automation.save_project(before["revision"], str(path))
    edited = automation.update_project(ProjectPatch(title="Unsaved draft"), saved["revision"])
    path.unlink()
    assert automation.open_project(str(path)) == edited
    assert automation.access.list_projects()["active_project_id"] == saved["project_id"]


def test_unsaved_changes_and_external_save_conflicts(automation, tmp_path):
    original = automation.get_project()
    source = automation.get_project()["project"]["video_path"]
    created = automation.new_project(source, "Replacement", ["Tester"])
    assert created["project_id"] != original["project_id"]
    assert automation.for_project(original["project_id"]).get_project() == original
    before = automation.get_project()
    destination = tmp_path / "saved.cvpack.json"
    saved = automation.save_project(before["revision"], str(destination))
    ProjectStore.save(PackProject(title="External edit"), destination)
    with pytest.raises(ValueError, match="changed on disk"):
        automation.save_project(saved["revision"], overwrite=True)
    assert ProjectStore.load(destination).title == "External edit"
    different = tmp_path / "copy.cvpack.json"
    automation.save_project(saved["revision"], str(different))


def test_bound_project_survives_active_switch_and_rejects_unknown_id(automation, tmp_path):
    original = automation.get_project()
    bound = automation.for_project()
    created = automation.new_project(original["project"]["video_path"], "Second", ["Tester"])
    edited = bound.update_project(ProjectPatch(title="First only"), original["revision"])
    assert automation.get_project() == created
    assert edited["project_id"] == original["project_id"]
    assert bound.get_project()["project"]["title"] == "First only"
    with pytest.raises(ValueError, match="Unknown project_id"):
        automation.for_project("missing")
    with pytest.raises(ValueError, match="Unknown project_id"):
        automation.for_project("")
    with pytest.raises(ValueError, match="Project changed"):
        automation.update_project(ProjectPatch(title="Wrong target"), edited["revision"])
    saved = bound.save_project(edited["revision"], str(tmp_path / "first.cvpack.json"))
    with pytest.raises(ValueError, match="another open project"):
        automation.save_project(created["revision"], saved["project_path"], overwrite=True)
    assert automation.open_project(saved["project_path"]) == saved
    assert automation.access.list_projects()["active_project_id"] == original["project_id"]


def test_save_overwrite_and_source_protection(automation, tmp_path):
    before = automation.get_project()
    with pytest.raises(ValueError, match="absolute"):
        automation.save_project(before["revision"], "relative.cvpack.json")
    with pytest.raises(ValueError, match="end in"):
        automation.save_project(before["revision"], str(tmp_path / "source.mp4"))
    destination = tmp_path / "existing.cvpack.json"
    ProjectStore.save(PackProject(title="Existing"), destination)
    with pytest.raises(ValueError, match="exists"):
        automation.save_project(before["revision"], str(destination))
    saved = automation.save_project(before["revision"], str(destination), overwrite=True)
    automation.access.current.project.icon_path = str(destination)
    current = automation.get_project()
    with pytest.raises(ValueError, match="source assets"):
        automation.save_project(current["revision"])
    assert ProjectStore.load(destination).title == saved["project"]["title"]


def test_imported_audio_requires_explicit_source_range_and_keeps_assets(automation, tmp_path):
    audio = tmp_path / "original.mp3"
    audio.write_bytes(b"original")
    segment = Segment(1, 3, "Line", ["Speaker"], "file", str(audio), source_range_known=False)
    automation.access.current.project.segments.append(segment)
    before = automation.get_project()
    edited = automation.edit_segments(
        [SegmentPatch(id=segment.id, start=2, end=4)], [], before["revision"]
    )
    assert edited["segments"][0]["audio_path"] == str(audio)
    with pytest.raises(ValueError, match="explicit start and end"):
        automation.edit_segments(
            [SegmentPatch(id=segment.id, audio_mode="video")], [], edited["revision"]
        )
    regenerated = automation.edit_segments(
        [SegmentPatch(id=segment.id, audio_mode="video", start=2, end=4)],
        [], edited["revision"],
    )
    assert regenerated["segments"][0]["source_range_known"]
    assert not regenerated["segments"][0]["audio_path"]
    assert audio.read_bytes() == b"original"


def test_analysis_requires_download_consent_before_work(automation):
    with pytest.raises(ValueError, match="permission"):
        automation.analyze(True, False, "balanced", "tiny", "auto", lambda *_: None, lambda: False)


def test_source_replacement_is_probed_and_invalid_paths_are_atomic(automation, tmp_path):
    automation.access.current.project.source_url = "https://www.youtube.com/watch?v=test"
    automation.access.current.project.caption_language = "en"
    automation.access.current.project.source_captions = [SourceCaption(1, 2, "Draft", "YouTube")]
    automation.access.current.project.analysis_review = AnalysisReview()
    before = automation.get_project()
    with pytest.raises(ValueError, match="absolute"):
        automation.update_project(ProjectPatch(video_path="relative.mp4"), before["revision"])
    assert automation.get_project() == before
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"replacement fixture")
    updated = automation.update_project(
        ProjectPatch(video_path=str(replacement)), before["revision"]
    )
    assert updated["project"]["video_path"] == str(replacement)
    assert updated["project"]["video_duration"] == 10
    assert not updated["project"]["preserve_source_video"]
    assert not updated["project"]["source_url"]
    assert not updated["project"]["caption_language"]
    assert not updated["project"]["source_captions"]
    assert updated["project"]["analysis_review"] is None


def test_metadata_edits_preserve_imported_caption_evidence(automation, tmp_path):
    automation.access.current.project.source_captions = [SourceCaption(1, 2, "Draft", "YouTube")]
    automation.access.current.project.analysis_review = AnalysisReview()
    before = automation.get_project()
    updated = automation.update_project(ProjectPatch(title="Changed"), before["revision"])
    automation.save_project(updated["revision"], str(tmp_path / "captions.cvpack.json"))
    saved = ProjectStore.load(tmp_path / "captions.cvpack.json")
    assert saved.source_captions[0].text == "Draft"
    assert saved.analysis_review == AnalysisReview()


@pytest.mark.parametrize("invalid", [
    '{"video_duration":1e999}',
    '{"head_padding":NaN}',
    '{"segments":[{"start":0,"end":Infinity}]}',
])
def test_unserializable_projects_never_replace_active_state(automation, tmp_path, invalid):
    before = automation.get_project()
    malformed = tmp_path / "malformed.cvpack.json"
    malformed.write_text(invalid, encoding="utf-8")
    with pytest.raises(ValueError):
        automation.open_project(str(malformed))
    assert automation.get_project() == before
    automation.save_project(before["revision"], str(tmp_path / "still-usable.cvpack.json"))


def test_export_requires_saved_project_and_protects_project_folder(automation, tmp_path):
    before = automation.get_project()
    with pytest.raises(ValueError, match="Save"):
        automation.export_pack(str(tmp_path), before["revision"])
    destination = tmp_path / "Test Pack" / "saved.cvpack.json"
    saved = automation.save_project(before["revision"], str(destination))
    with pytest.raises(ValueError, match="saved project"):
        automation.export_pack(str(tmp_path), saved["revision"], overwrite=True)


def test_preview_bounds_and_headless_show_errors(automation):
    with pytest.raises(ValueError, match="within"):
        automation.get_frame(10)
    with pytest.raises(ValueError, match="within"):
        automation.preview_audio(5, 3)
    with pytest.raises(ValueError, match="headless"):
        automation.access.show(None, None)


def test_loading_snapshot_is_inspectable_but_cannot_be_edited_or_saved(tmp_path):
    access = HeadlessProjectAccess(ProjectSnapshot(PackProject(title="Opening"), loading=True))
    automation = PackAutomation(access, tmp_path)
    state = automation.get_project()
    assert state["loading"]
    with pytest.raises(ValueError, match="still loading"):
        automation.update_project(ProjectPatch(title="Not ready"), state["revision"])
    with pytest.raises(ValueError, match="still loading"):
        automation.save_project(state["revision"], str(tmp_path / "must-not-exist.cvpack.json"))
    assert automation.get_project() == state
    assert not (tmp_path / "must-not-exist.cvpack.json").exists()
