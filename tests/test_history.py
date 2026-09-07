from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from random import Random

import pytest

from choicer_voicer_pack_creator import history as history_module
from choicer_voicer_pack_creator.history import ProjectHistory
from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    CaptionFragment,
    PackProject,
    Segment,
    SourceCaption,
)


def rich_project() -> PackProject:
    return PackProject(
        title="Original", authors=["Author", " Second "], readme="Notes\n",
        video_path="missing-video.mp4", video_duration=123.123456789123,
        backing_track_path="preserved-backing.wav", icon_path="icon.png",
        head_padding=0.123456789, tail_padding=0.987654321, video_height=1080, video_fps=59,
        source_pack_path="original-pack", preserve_source_video=True,
        import_warnings=["Keep this warning"], source_url="https://example.invalid/media",
        caption_language="ja", auto_speaker_matching=False,
        source_captions=[
            SourceCaption(
                0.123456789, 2.345678912, "Source text", "YouTube",
                (CaptionFragment("Source ", 0.123456789), CaptionFragment("text")),
            ),
        ],
        analysis_review=AnalysisReview(
            [AnalysisDraftRow("unfinished.", "2.345678912", "Local", "Whisper", 0.123456789)],
            "refined", "Whisper",
            [AnalysisDraftRow("1.234567891", "3.456789123", "Refined", "YouTube", None, False)],
            0.456789123, "small", "ja",
        ),
        segments=[
            Segment(
                4.123456789123, 7.987654321987, "Recording", [" B ", "A"],
                "file", "keep-this-prompt.mp3", "still.png", False, "recording-id", "excluded",
            ),
            Segment(0.123456789123, 2.234567891234, "Earlier", ["C"], id="earlier-id"),
        ],
    )


def jump(history: ProjectHistory, target: int) -> PackProject:
    old_index = history.index
    request = history.prepare(target)
    with ThreadPoolExecutor(max_workers=1) as pool:
        project = pool.submit(request.build_project).result()
    assert history.index == old_index
    assert history.accept(request, project)
    assert history.index == target
    assert history.current_id == request.state_id
    return project


def test_every_model_field_and_exact_values_survive_replay(monkeypatch):
    project = rich_project()
    original = deepcopy(project)
    history = ProjectHistory(project)
    initial_id = history.current_id
    assert history.is_clean
    assert not history.can_undo and not history.can_redo
    assert history.undo_label is None and history.redo_label is None

    changed = PackProject(
        title="Changed", authors=["New"], readme="Other", video_path="new.mp4",
        video_duration=789.987654321, backing_track_path="other.wav", icon_path="other.png",
        segments=[Segment(8.987654321, 9.123456789, "New", ["New"], id="new")],
        head_padding=0.9, tail_padding=0.1, video_height=720, video_fps=24,
        source_pack_path="other", preserve_source_video=False, import_warnings=["Other"],
        source_url="other-url", caption_language="en",
        source_captions=[SourceCaption(1.111111111, 2.222222222, "Other", "Whisper")],
        analysis_review=AnalysisReview(
            [], "local", "Audio activity", [], 0.5, "different-model", "en",
        ),
        auto_speaker_matching=True,
    )
    assert all(
        getattr(original, field.name) != getattr(changed, field.name) for field in fields(PackProject)
    )
    assert history.record(changed, "Replace everything")
    changed_id = history.current_id
    expected = deepcopy(changed)

    def forbidden(*args, **kwargs):
        pytest.fail("History must not serialize, validate, access media, or clone through model APIs")

    for model in (PackProject, Segment):
        monkeypatch.setattr(model, "to_dict", forbidden)
        monkeypatch.setattr(model, "from_dict", forbidden)
    monkeypatch.setattr(PackProject, "validate", forbidden)
    monkeypatch.setattr(Segment, "clone", forbidden)
    restored = jump(history, 0)
    assert restored == original
    assert original == restored
    assert history.current_id == initial_id
    assert history.is_clean
    assert history.redo_label == "Replace everything"
    assert jump(history, 1) == expected
    assert history.current_id == changed_id
    assert history.undo_label == "Replace everything"


def test_added_deleted_moved_and_changed_segments_multiple_jumps():
    project = rich_project()
    history = ProjectHistory(project)
    versions = [deepcopy(project)]
    identifiers = [segment.id for segment in project.segments]
    project.segments.insert(1, Segment(10.123456789, 11.234567891, id="added"))
    history.record(project, "Add")
    versions.append(deepcopy(project))
    del project.segments[0]
    history.record(project, "Delete")
    versions.append(deepcopy(project))
    project.segments.reverse()
    project.segments[0].start = 0.987654321123
    project.segments[0].end = 1.234567891234
    history.record(project, "Move")
    versions.append(deepcopy(project))
    project.segments[0].id = "replacement-id"
    history.record(project, "Replace identity")
    versions.append(deepcopy(project))
    assert history.labels == ("Add", "Delete", "Move", "Replace identity")
    for target in (0, 4, 1, 3, 2, 0, 1, 2, 3, 4):
        assert jump(history, target) == versions[target]
    assert [segment.id for segment in jump(history, 0).segments] == identifiers


def test_noop_preserves_redo_state_ids_and_pending_request():
    project = rich_project()
    history = ProjectHistory(project)
    project.title = "Changed"
    history.record(project, "Title", fields_only=True)
    project = jump(history, 0)
    request = history.prepare(1)
    state = history.current_id
    assert not history.record(project, "Nothing")
    assert not history.record(project, "Nothing", fields_only=True)
    assert not history.record(project, "Nothing", segment=project.segments[0])
    assert history.current_id == state
    assert history.labels == ("Title",) and history.can_redo
    assert history.accept(request, request.build_project())


def test_edit_after_undo_discards_redo_even_with_matching_merge_key(monkeypatch):
    monkeypatch.setattr(history_module, "monotonic", lambda: 1.0)
    project = PackProject()
    history = ProjectHistory(project)
    project.title = "One"
    history.record(project, "One", fields_only=True, merge_key="title")
    history.break_merge()
    project.title = "Two"
    history.record(project, "Two", fields_only=True, merge_key="title")
    project = jump(history, 1)
    project.title = "Branch"
    history.record(project, "Branch", fields_only=True, merge_key="title")
    assert history.labels == ("One", "Branch")
    assert not history.can_redo
    assert jump(history, 0).title == "Untitled Dub Pack"


def test_coalescing_time_keys_and_explicit_boundaries(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(history_module, "monotonic", lambda: now[0])
    project = PackProject(segments=[Segment(0, 1)])
    history = ProjectHistory(project)
    for text in ("H", "He", "Hello"):
        project.segments[0].caption = text
        history.record(project, text, segment=project.segments[0], merge_key="caption")
        now[0] += 0.3
    assert history.labels == ("Hello",)
    project = jump(history, 0)
    assert project.segments[0].caption == ""
    project = jump(history, 1)
    assert project.segments[0].caption == "Hello"
    project.title = "A"
    history.record(project, "A", fields_only=True, merge_key="title")
    now[0] += 0.751
    project.title = "B"
    history.record(project, "B", fields_only=True, merge_key="title")
    history.break_merge()
    project.title = "C"
    history.record(project, "C", fields_only=True, merge_key="title")
    history.mark_saved()
    project.title = "D"
    history.record(project, "D", fields_only=True, merge_key="title")
    project.title = "E"
    history.record(project, "E", fields_only=True, merge_key="another-field")
    project.title = "F"
    history.record(project, "F", fields_only=True)
    project.title = "G"
    history.record(project, "G", fields_only=True)
    assert history.labels == ("Hello", "A", "B", "C", "D", "E", "F", "G")


def test_coalescing_cancellation_and_structural_edits(monkeypatch):
    monkeypatch.setattr(history_module, "monotonic", lambda: 1.0)
    project = rich_project()
    expected = deepcopy(project)
    history = ProjectHistory(project)
    project.segments.append(Segment(20, 21, id="new"))
    assert history.record(project, "Add", merge_key="group")
    project.segments.reverse()
    assert history.record(project, "Reverse", merge_key="group")
    assert history.labels == ("Reverse",)
    final = deepcopy(project)
    assert jump(history, 0) == expected
    project = jump(history, 1)
    assert project == final

    history.reset(project)
    saved = history.current_id
    title = project.title
    project.title = "Typing"
    history.record(project, "Type", fields_only=True, merge_key="type")
    project.title = title
    history.record(project, "Erase", fields_only=True, merge_key="type")
    assert history.labels == () and history.current_id == saved and history.is_clean


def test_latest_one_hundred_entries_and_oldest_retained_state():
    project = PackProject(title="0")
    history = ProjectHistory(project)
    initial = history.current_id
    saved_at_50 = None
    for number in range(1, 152):
        project.title = str(number)
        history.record(project, f"Edit {number}", fields_only=True)
        if number == 50:
            saved_at_50 = history.current_id
            history.mark_saved()
    assert history.index == 100 and len(history.labels) == 100
    assert history.labels[0] == "Edit 52"
    assert history.current_id not in (initial, saved_at_50)
    assert not history.is_clean
    assert jump(history, 0).title == "51"
    assert not history.is_clean and not history.can_undo
    assert jump(history, 100).title == "151"


def test_clean_point_survives_as_oldest_retained_state_then_is_lost():
    project = PackProject(title="0")
    history = ProjectHistory(project, limit=2)
    project.title = "1"
    history.record(project, "1", fields_only=True)
    history.mark_saved()
    for title in ("2", "3"):
        project.title = title
        history.record(project, title, fields_only=True)
    project = jump(history, 0)
    assert project.title == "1" and history.is_clean
    project = jump(history, 2)
    project.title = "4"
    history.record(project, "4", fields_only=True)
    assert jump(history, 0).title == "2"
    assert not history.is_clean


def test_async_save_ids_and_lost_clean_branch():
    project = PackProject(title="0")
    history = ProjectHistory(project)
    project.title = "1"
    history.record(project, "1", fields_only=True)
    saving_id = history.current_id
    history.break_merge()
    project.title = "2"
    history.record(project, "2", fields_only=True)
    history.mark_saved(saving_id)
    assert not history.is_clean
    project = jump(history, 1)
    assert history.is_clean
    project = jump(history, 0)
    project.title = "Branched"
    history.record(project, "Branch", fields_only=True)
    history.mark_saved(saving_id)
    assert not history.is_clean
    assert not history.can_redo
    project = jump(history, 0)
    assert not history.is_clean
    history.mark_saved(-1)
    assert not history.is_clean
    history.mark_saved()
    assert history.is_clean


def test_saved_id_merged_away_cannot_falsely_mark_current_clean(monkeypatch):
    monkeypatch.setattr(history_module, "monotonic", lambda: 1.0)
    project = PackProject(title="0")
    history = ProjectHistory(project)
    project.title = "1"
    history.record(project, "1", fields_only=True, merge_key="title")
    saving_id = history.current_id
    project.title = "2"
    history.record(project, "2", fields_only=True, merge_key="title")
    assert history.current_id != saving_id
    history.mark_saved(saving_id)
    assert not history.is_clean


def test_request_is_immutable_independent_and_worker_does_not_touch_live_model():
    project = rich_project()
    history = ProjectHistory(project)
    project.segments[0].characters.append("Edit")
    history.record(project, "Edit", segment=project.segments[0])
    request = history.prepare(0)
    before = deepcopy(project)
    with pytest.raises(FrozenInstanceError):
        request.target_index = 1
    with pytest.raises(TypeError):
        request._snapshot.segments["other"] = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: request.build_project(), range(2)))
    assert project == before
    assert first == second
    assert first.segments[0] is not second.segments[0]
    first.authors.append("Not shared")
    first.segments[0].characters.append("Not shared")
    first.source_captions.clear()
    first.analysis_review.local_rows.clear()
    assert second == rich_project()
    assert request.build_project() == second
    project.authors.clear()
    project.analysis_review.local_rows.clear()
    project.segments[0].caption = "Unrecorded live mutation"
    assert request.build_project() == second
    assert history.accept(request, second)
    second.analysis_review.refined_rows.clear()
    second.source_captions.clear()
    second.segments[0].characters.clear()
    assert history.prepare(0).build_project() == rich_project()


@pytest.mark.parametrize("invalidate", ["record", "reset", "accept", "other-history"])
def test_stale_requests_are_rejected_without_mutation(invalidate):
    project = PackProject(title="0")
    history = ProjectHistory(project)
    project.title = "1"
    history.record(project, "1", fields_only=True)
    request = history.prepare(0)
    built = request.build_project()
    if invalidate == "record":
        project.title = "2"
        history.record(project, "2", fields_only=True)
    elif invalidate == "reset":
        history.reset(project)
    elif invalidate == "accept":
        jump(history, 0)
    else:
        history = ProjectHistory(project)
    state = (history.index, history.current_id, history.labels, history.is_clean)
    assert not history.accept(request, built)
    assert (history.index, history.current_id, history.labels, history.is_clean) == state
    assert built.title == "0"
    assert request.build_project().title == "0"


def test_accept_requires_result_from_corresponding_request_and_is_constant_time(monkeypatch):
    project = rich_project()
    history = ProjectHistory(project)
    project.title = "Changed"
    history.record(project, "Title", fields_only=True)
    request = history.prepare(0)
    other = history.prepare(0)
    assert not history.accept(request, deepcopy(project))
    assert not history.accept(request, other.build_project())
    built = request.build_project()
    snapshot = built._history_snapshot

    def forbidden(*args, **kwargs):
        pytest.fail("Acceptance must not scan or capture model values")

    monkeypatch.setattr(history_module, "_freeze", forbidden)
    monkeypatch.setattr(history_module, "_segment", forbidden)
    assert history.accept(request, built)
    assert history._segments is snapshot.segments
    assert history._metadata is snapshot.metadata
    assert history._order is snapshot.order
    assert built._history_request is None and built._history_snapshot is None
    assert not history.accept(request, built)


def test_fast_segment_and_fields_only_paths_never_scan_unrelated_segments(monkeypatch):
    project = rich_project()
    history = ProjectHistory(project)
    target = project.segments[0]
    capture = history_module._segment

    class NoIteration(list):
        def __iter__(self):
            pytest.fail("Optimized recording must not iterate project segments")

    def capture_target(segment):
        assert segment is target
        return capture(segment)

    project.segments = NoIteration(project.segments)
    monkeypatch.setattr(history_module, "_segment", capture_target)
    target.caption = "Changed"
    assert history.record(project, "Caption", segment=target)
    project.title = "Changed title"
    assert history.record(project, "Title", fields_only=True)
    assert jump(history, 0) == rich_project()


def test_large_unchanged_metadata_is_shared_and_not_scanned_per_keystroke(monkeypatch):
    project = rich_project()
    project.source_captions *= 2000
    project.analysis_review.local_rows.extend(project.analysis_review.local_rows * 1999)
    project.segments.extend(Segment(index, index + 1) for index in range(1000))
    history = ProjectHistory(project)
    initial_metadata = history._metadata
    initial_segments = history._segments
    target = project.segments[0]
    freeze = history_module._freeze

    def forbid_large_metadata_scan(value):
        assert not isinstance(value, (SourceCaption, CaptionFragment, AnalysisDraftRow))
        return freeze(value)

    monkeypatch.setattr(history_module, "_freeze", forbid_large_metadata_scan)
    for number in range(150):
        target.caption = f"Text {number}"
        assert history.record(project, "Caption", segment=target)
    assert history._metadata is initial_metadata
    assert history._segments is initial_segments
    assert len(history._overrides) == 1
    assert len(history._entries) == 100
    assert all(
        len(entry.delta.segments) == 1 and not entry.delta.metadata and entry.delta.order is None
        for entry in history._entries
    )
    request = history.prepare(0)
    assert request._snapshot.metadata is initial_metadata
    assert request._snapshot.order is history._order
    for identifier, value in initial_segments.items():
        if identifier != target.id:
            assert request._snapshot.segments[identifier] is value
    project = request.build_project()
    assert history.accept(request, project)
    restored_metadata = history._metadata
    project.segments[0].caption = "Editing after replay"
    assert history.record(project, "Caption", segment=project.segments[0])
    assert history._metadata is restored_metadata


def test_mutable_metadata_changes_and_original_inputs_cannot_corrupt_snapshots():
    project = rich_project()
    old_authors = project.authors
    old_rows = project.analysis_review.local_rows
    history = ProjectHistory(project)
    old_authors.clear()
    old_rows.clear()
    assert history.prepare(0).build_project() == rich_project()

    project.source_captions.append(SourceCaption(3, 4, "New", "Whisper"))
    project.analysis_review.local_rows.append(AnalysisDraftRow("3", "4", "New", "Whisper"))
    project.authors[0] = "Changed"
    history.record(project, "Metadata", fields_only=True)
    expected = deepcopy(project)
    project.authors.clear()
    project.source_captions.clear()
    project.analysis_review.local_rows.clear()
    project.analysis_review.refined_rows.clear()
    project.segments[0].characters.clear()
    assert jump(history, 0) == rich_project()
    assert jump(history, 1) == expected


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.append("c"),
        lambda value: value.extend(["c"]),
        lambda value: value.insert(0, "c"),
        lambda value: value.pop(),
        lambda value: value.remove("b"),
        lambda value: value.clear(),
        lambda value: value.reverse(),
        lambda value: value.sort(),
        lambda value: value.__setitem__(0, "c"),
        lambda value: value.__setitem__(slice(None), ["c"]),
        lambda value: value.__delitem__(0),
        lambda value: value.__iadd__(["c"]),
        lambda value: value.__imul__(2),
    ],
)
def test_all_standard_list_mutations_invalidate_metadata_cache(mutate):
    project = PackProject(authors=["b", "a"])
    history = ProjectHistory(project)
    mutate(project.authors)
    expected = list(project.authors)
    assert history.record(project, "Authors", fields_only=True)
    assert jump(history, 0).authors == ["b", "a"]
    assert jump(history, 1).authors == expected


def test_equal_metadata_replacements_are_noops_and_preserve_shared_values():
    project = rich_project()
    history = ProjectHistory(project)
    metadata = history._metadata
    project.authors = list(project.authors)
    project.source_captions = list(project.source_captions)
    project.analysis_review = replace(
        project.analysis_review, local_rows=list(project.analysis_review.local_rows),
    )
    assert not history.record(project, "No change", fields_only=True)
    assert history._metadata is metadata


def test_reset_invalidates_requests_and_starts_independent_clean_history():
    project = rich_project()
    history = ProjectHistory(project)
    project.title = "Changed"
    history.record(project, "Change", fields_only=True)
    old_id = history.current_id
    request = history.prepare(0)
    new_project = PackProject(title="New project")
    history.reset(new_project)
    assert history.current_id != old_id
    assert history.labels == () and history.index == 0 and history.is_clean
    assert history.prepare(0).build_project() == new_project
    assert not history.accept(request, request.build_project())
    independent = ProjectHistory(project)
    assert independent.current_id != history.current_id


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits_are_rejected(limit):
    with pytest.raises(ValueError, match="limit"):
        ProjectHistory(PackProject(), limit=limit)


@pytest.mark.parametrize("target", [-1, 1, True, 0.5])
def test_invalid_targets_are_rejected(target):
    history = ProjectHistory(PackProject())
    with pytest.raises(ValueError, match="index"):
        history.prepare(target)


def test_invalid_fast_path_and_duplicate_identifiers_are_rejected():
    project = rich_project()
    history = ProjectHistory(project)
    with pytest.raises(ValueError, match="either"):
        history.record(project, "Invalid", segment=project.segments[0], fields_only=True)
    with pytest.raises(ValueError, match="full history"):
        history.record(project, "Add", segment=Segment(0, 1))
    project.segments.append(deepcopy(project.segments[0]))
    with pytest.raises(ValueError, match="unique"):
        history.record(project, "Duplicate")
    with pytest.raises(ValueError, match="unique"):
        ProjectHistory(project)
    assert history.labels == ()


def test_random_edits_and_jumps_match_independent_project_snapshots():
    random = Random(72349)
    project = rich_project()
    history = ProjectHistory(project, limit=7)
    expected = [deepcopy(project)]
    for number in range(120):
        if number % 3 == 0:
            target = random.randrange(len(expected))
            project = jump(history, target)
            assert project == expected[target]
        before = deepcopy(project)
        index = history.index
        action = random.randrange(6)
        if action == 0:
            project.title = f"Title {number}"
        elif action == 1:
            project.segments.insert(
                random.randrange(len(project.segments) + 1),
                Segment(number + 0.123456789, number + 1.987654321, id=f"new-{number}"),
            )
        elif action == 2 and project.segments:
            del project.segments[random.randrange(len(project.segments))]
        elif action == 3:
            random.shuffle(project.segments)
        elif action == 4 and project.segments:
            project.segments[random.randrange(len(project.segments))].caption = str(number)
        else:
            project.authors.append(str(number))
        changed = project != before
        assert history.record(project, str(number)) == changed
        if changed:
            expected[index + 1:] = [deepcopy(project)]
            if len(expected) > 8:
                expected.pop(0)
        assert len(history.labels) == len(expected) - 1
        assert history.prepare(history.index).build_project() == project
    for target, snapshot in enumerate(expected):
        assert jump(history, target) == snapshot
