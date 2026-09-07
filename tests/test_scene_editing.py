from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import wave
from array import array
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.exporter import PackExporter
from choicer_voicer_pack_creator.media import AudioInfo, MediaError, MediaInfo, MediaTools
from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    CaptionFragment,
    PackProject,
    Segment,
    SourceCaption,
)
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    operation_scope,
)
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.scene_editing import execute_scene_edit, plan_scene_edit


@pytest.fixture
def project(tmp_path: Path) -> PackProject:
    source = tmp_path / "original.mkv"
    source.write_bytes(b"immutable source")
    return PackProject(video_path=str(source), video_duration=10.0)


class FakeMedia:
    ffmpeg = "ffmpeg"
    ffprobe = "ffprobe"

    def __init__(self, project: PackProject, duration: float, *, has_audio: bool = True):
        self.project = project
        self.duration = duration
        self.has_audio = has_audio
        self.commands = []
        self.on_decode = lambda: None
        self.on_render = lambda: None
        self.bad_duration = False
        self.bad_audio = False

    def probe(self, path):
        duration = (
            self.project.video_duration if path == Path(self.project.video_path) else self.duration
        )
        return MediaInfo(
            duration, 64, 48, 20.0, self.has_audio, "ffv1",
            "pcm_s16le" if self.has_audio else "", "yuv420p", 48000, 1,
        )

    def probe_audio(self, _path):
        return AudioInfo(self.duration, "pcm_s16le", 48000, 1)

    def run(self, command, description):
        self.commands.append(command)
        stdout = ""
        if command[0] == self.ffprobe:
            duration = (
                self.project.video_duration if command[-1] == self.project.video_path
                else self.duration + (1 if self.bad_duration else 0)
            )
            stdout = json.dumps({
                "streams": [{"start_time": "0"}],
                "packets": [{"pts_time": str(duration - 0.05), "duration_time": "0.05"}],
            })
            if "packet=pts_time" in command:
                position = float(command[command.index("-read_intervals") + 1].split("%")[0])
                stdout = json.dumps({"packets": [{"pts_time": str(position)}]})
        elif "-progress" in command:
            stdout = f"out_time_us={int((self.duration + self.bad_audio) * 1e6)}\nprogress=end"
        else:
            Path(command[-1]).write_bytes(b"complete derived media")
            self.on_render()
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def decode(self, _path):
        self.on_decode()


def execute(project, tmp_path, start, end, mode="cut", **kwargs):
    plan = plan_scene_edit(project, start, end, mode)
    return execute_scene_edit(
        project, start, end, mode, tmp_path,
        FakeMedia(project, plan.duration), **kwargs,
    )


@pytest.mark.parametrize(
    ("start", "end", "mode", "ranges", "expected"),
    [
        (0, 2, "cut", ((2, 10),), [(0, 2), (3, 5), (6, 8)]),
        (8, 10, "cut", ((0, 8),), [(0, 2), (2, 4), (5, 7)]),
        (3, 6, "cut", ((0, 3), (6, 10)), [(0, 2), (2, 3), (3, 4), (5, 7)]),
        (2, 8, "extract", ((2, 8),), [(0, 2), (3, 5)]),
    ],
)
def test_retained_segments_ripple_and_preserve_text(
    project, tmp_path, start, end, mode, ranges, expected,
):
    project.segments = [
        Segment(a, b, f"Full dialogue {a}", ["Speaker"], image_path="")
        for a, b in [(0, 2), (2, 4), (5, 7), (8, 10)]
    ]
    original = copy.deepcopy(project)
    plan = plan_scene_edit(project, start, end, mode)
    assert plan.kept_ranges == ranges
    with pytest.raises(FrozenInstanceError):
        plan.duration = 123
    result = execute(project, tmp_path, start, end, mode)
    assert [(s.start, s.end) for s in result.segments] == expected
    assert project == original
    assert result is not project
    for segment in result.segments:
        source = project.segment_by_id(segment.id)
        assert segment.caption == source.caption
        assert segment.characters == source.characters
        assert segment.characters is not source.characters
    if (start, end) == (3, 6):
        assert any("clipped" in warning and "review" in warning for warning in plan.warnings)
    else:
        assert any("removed" in warning for warning in plan.warnings)


def test_segment_spanning_deleted_interior_becomes_one_contiguous_prompt(project, tmp_path):
    project.segments = [Segment(1, 9, "Keep all these words", ["A", "B"])]
    result = execute(project, tmp_path, 3, 6)
    assert len(result.segments) == 1
    assert (result.segments[0].start, result.segments[0].end) == (1, 6)
    assert result.segments[0].caption == project.segments[0].caption


@pytest.mark.parametrize("mode", ["cut", "extract"])
@pytest.mark.parametrize("known", [False, True])
def test_partially_intersecting_recording_requires_explicit_regeneration(project, mode, known):
    project.segments = [
        Segment(1, 5, "recorded", ["A"], audio_mode="file", source_range_known=known),
    ]
    with pytest.raises(ValueError, match="preserved recording"):
        plan_scene_edit(project, 2, 4, mode)


def test_unknown_video_range_is_not_silently_regenerated(project):
    project.segments = [Segment(1, 5, source_range_known=False)]
    with pytest.raises(ValueError, match="unknown source range"):
        plan_scene_edit(project, 2, 4, "extract")


def test_whole_recordings_and_imported_assets_are_preserved_by_reference(project, tmp_path):
    audio, image, icon = [tmp_path / name for name in ("recording.mp3", "still.png", "icon.png")]
    for asset in (audio, image, icon):
        asset.write_bytes(asset.name.encode())
    project.icon_path = str(icon)
    project.source_pack_path = str(tmp_path / "original-pack")
    project.source_url = "https://example.com/original"
    project.import_warnings = ["Unknown source metadata must be retained"]
    project.caption_language = "en"
    project.authors = ["Creator"]
    project.auto_speaker_matching = False
    project.preserve_source_video = True
    project.segments = [
        Segment(0, 1, audio_mode="file", audio_path=str(audio)),
        Segment(5, 8, "Full preserved recording", ["A"], "file", str(audio), str(image),
                source_range_known=False, speaker_assignment="excluded"),
        Segment(8, 9, audio_path=project.video_path),
        Segment(9, 10, audio_path=str(audio)),
    ]
    before = copy.deepcopy(project)
    result = execute(project, tmp_path, 0, 2)
    assert project == before
    assert result.source_pack_path == project.source_pack_path
    assert result.source_url == project.source_url
    assert result.import_warnings == project.import_warnings
    assert result.import_warnings is not project.import_warnings
    assert result.caption_language == "en"
    assert result.icon_path == str(icon)
    assert result.authors == ["Creator"]
    assert not result.auto_speaker_matching
    assert not result.preserve_source_video
    recording = result.segments[0]
    assert recording == replace(project.segments[1], start=3, end=6)
    assert recording.audio_path == str(audio)
    assert recording.image_path == str(image)
    assert result.segments[1].audio_path == result.video_path
    assert result.segments[2].audio_path == str(audio)
    assert {path.name for path in Path(result.video_path).parent.iterdir()} == {
        "video.mkv", "project.cvpack.json",
    }
    for asset in (audio, image, icon):
        assert asset.read_bytes() == asset.name.encode()


def test_file_mode_recording_pointing_to_source_video_is_not_replaced(project, tmp_path):
    project.segments = [Segment(3, 5, audio_mode="file", audio_path=project.video_path)]
    edited = execute(project, tmp_path, 0, 2)
    assert edited.segments[0].audio_path == project.video_path
    assert edited.segments[0].audio_mode == "file"
    assert PackProject.from_dict(edited.to_dict()) == edited


@pytest.mark.parametrize("mode,start,end", [("cut", 3, 6), ("extract", 2, 8)])
def test_caption_fragments_and_draft_review_remap_without_losing_text(
    project, tmp_path, mode, start, end,
):
    fragments = tuple(
        CaptionFragment(text, time) for text, time in [
            ("before ", 1.0), ("inside ", 4.0), ("after", 8.5), ("!", None),
        ]
    )
    project.source_captions = [
        SourceCaption(1, 9, "before inside after!", "Creator", fragments),
        SourceCaption(3.1, 5.9, "excluded by middle cut", "Creator"),
    ]
    local = AnalysisDraftRow("1", "9", "Unfinished wording", "Whisper", 0.8, False)
    refined = AnalysisDraftRow("2", "8", "Reviewed wording", "Creator", checked=True)
    project.analysis_review = AnalysisReview(
        [local], "refined", "Whisper", [refined], 0.7, "small", "en",
    )
    before = copy.deepcopy(project)
    result = execute(project, tmp_path, start, end, mode)
    assert project == before
    cue = result.source_captions[0]
    assert cue.text == before.source_captions[0].text
    assert [fragment.text for fragment in cue.fragments] == [f.text for f in fragments]
    assert cue.fragments[-1].start is None
    assert all(cue.start <= fragment.start < cue.end for fragment in cue.fragments[:-1])
    review = result.analysis_review
    assert review is not project.analysis_review
    assert (review.selected_source, review.local_source, review.pause_threshold) == (
        "refined", "Whisper", 0.7,
    )
    assert (review.local_model_name, review.local_detected_language) == ("small", "en")
    assert review.local_rows[0].caption == local.caption
    assert review.local_rows[0].confidence == 0.8
    assert not review.local_rows[0].checked
    assert review.refined_rows[0].checked
    assert review.refined_rows[0].caption == refined.caption
    if mode == "cut":
        assert len(result.source_captions) == 1
        assert (cue.start, cue.end) == (1, 6)
        assert [f.start for f in cue.fragments[:2]] == [1, 3]
        assert (float(review.refined_rows[0].start), float(review.refined_rows[0].end)) == (2, 5)
    else:
        assert (cue.start, cue.end) == (0, 6)
        assert [f.start for f in cue.fragments[:2]] == [0, 2]
        assert (float(review.local_rows[0].start), float(review.local_rows[0].end)) == (0, 6)


@pytest.mark.parametrize("row_start,row_end", [
    ("unfinished", "2"), ("nan", "2"), ("0", "inf"), ("-1", "2"), ("5", "4"), ("8", "11"),
])
@pytest.mark.parametrize("rows_name", ["local_rows", "refined_rows"])
def test_malformed_drafts_rejected_even_when_unchecked_or_excluded(
    project, row_start, row_end, rows_name,
):
    project.analysis_review = AnalysisReview(**{
        rows_name: [AnalysisDraftRow(row_start, row_end, "user edits", "Whisper", checked=False)],
    })
    before = copy.deepcopy(project)
    with pytest.raises(ValueError, match="analysis draft row 1"):
        plan_scene_edit(project, 7, 9, "extract")
    assert project == before


def test_excluded_drafts_can_disappear_but_review_options_survive(project, tmp_path):
    project.analysis_review = AnalysisReview(
        [AnalysisDraftRow("0", "1", "not in scene", "Whisper")], "refined", "Whisper",
        [AnalysisDraftRow("8", "9", "kept", "Creator", checked=False)],
    )
    plan = plan_scene_edit(project, 7, 10, "extract")
    assert any("1 analysis draft row(s)" in warning for warning in plan.warnings)
    result = execute(project, tmp_path, 7, 10, "extract")
    assert result.analysis_review.local_rows == []
    assert result.analysis_review.selected_source == "refined"
    assert float(result.analysis_review.refined_rows[0].start) == 1
    assert not result.analysis_review.refined_rows[0].checked


def test_precise_draft_times_are_not_rounded_into_an_invalid_range(project, tmp_path):
    project.analysis_review = AnalysisReview([
        AnalysisDraftRow("1", "1.0000000001", "Keep this unfinished draft", "Whisper"),
    ])
    edited = execute(project, tmp_path, 0.5, 2, "extract")
    row = edited.analysis_review.local_rows[0]
    assert float(row.end) > float(row.start)


def test_unsavably_short_segment_remnants_are_rejected(project):
    project.segments = [Segment(2.9999999, 4, "Boundary dialogue", ["A"])]
    with pytest.raises(ValueError, match="too short to save"):
        plan_scene_edit(project, 3, 6, "cut")


@pytest.mark.parametrize("start,end,mode", [
    (float("nan"), 2, "cut"), (0, float("inf"), "extract"),
    (float("-inf"), 2, "cut"), (True, 2, "cut"), ("1", 2, "cut"),
    (-0.1, 2, "cut"), (2, 2, "extract"), (3, 2, "cut"),
    (0, 10.001, "extract"), (0, 10, "cut"), (0.01, 10, "cut"),
    (0, 0.01, "extract"), (1, 2, "invalid"),
])
def test_invalid_edits_fail_before_creating_output(project, tmp_path, start, end, mode):
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError):
        execute_scene_edit(project, start, end, mode, tmp_path, FakeMedia(project, 1))
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("duration", [9.9995, 9.9996])
@pytest.mark.parametrize("mode", ["cut", "extract"])
def test_millisecond_boundary_rounding_is_clamped_to_measured_duration(
    project, tmp_path, duration, mode,
):
    project.video_duration = duration
    plan = plan_scene_edit(project, 2, 10, mode)
    assert plan.start == 2
    assert plan.end == duration
    assert plan.kept_ranges == (((0, 2),) if mode == "cut" else ((2, duration),))
    result = execute(project, tmp_path, 2, 10, mode)
    assert result.video_duration == pytest.approx(2 if mode == "cut" else duration - 2)


def test_small_negative_start_rounding_is_clamped_to_zero(project):
    plan = plan_scene_edit(project, -0.0005, 2, "extract")
    assert plan.start == 0
    assert plan.kept_ranges == ((0, 2),)
    assert plan.duration == 2


@pytest.mark.parametrize("start,end", [
    (-0.0006, 2), (0, 10.0002), (9.9996, 10), (10, 9.9996),
])
def test_rounding_tolerance_does_not_accept_out_of_range_or_empty_edits(project, start, end):
    project.video_duration = 9.9996
    with pytest.raises(ValueError, match="ordered In/Out"):
        plan_scene_edit(project, start, end, "extract")


@pytest.mark.parametrize("start,end", [
    (float("nan"), 2), (0, float("inf")), (-1, 2), (3, 2), (9, 11),
])
def test_invalid_segment_timestamps_are_not_silently_discarded(project, start, end):
    project.segments = [Segment(start, end)]
    with pytest.raises(ValueError, match="segment 1"):
        plan_scene_edit(project, 3, 4, "extract")


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), 0, -1])
def test_invalid_project_duration(project, duration):
    project.video_duration = duration
    with pytest.raises(ValueError):
        plan_scene_edit(project, 1, 2, "cut")


def test_missing_video_and_bad_output_parent(project, tmp_path):
    with pytest.raises(ValueError, match="existing output"):
        execute_scene_edit(project, 1, 2, "cut", tmp_path / "missing", FakeMedia(project, 9))
    project.source_pack_path = str(tmp_path)
    with pytest.raises(ValueError, match="original source pack"):
        execute_scene_edit(project, 1, 2, "cut", tmp_path, FakeMedia(project, 9))
    project.video_path = ""
    with pytest.raises(ValueError, match="source video"):
        plan_scene_edit(project, 1, 2, "cut")


def test_repeated_scenes_are_unique_and_original_is_unchanged(project, tmp_path):
    project.segments = [Segment(4, 7, "Full text", ["A"])]
    before = copy.deepcopy(project)
    first = execute(project, tmp_path, 2, 9, "extract", title="First scene")
    second = execute(project, tmp_path, 2, 9, "extract", title="Second scene")
    nested = execute(first, tmp_path, 1, 6, "extract", title="Nested scene")
    assert project == before
    assert first.title == "First scene"
    assert first.video_path != second.video_path != nested.video_path
    assert nested.title == "Nested scene"
    assert nested.video_duration == 5
    assert (nested.segments[0].start, nested.segments[0].end) == (1, 4)
    assert Path(project.video_path).read_bytes() == b"immutable source"


@pytest.mark.parametrize("mode", ["cut", "extract"])
def test_published_project_snapshot_round_trips_final_media_and_preserved_assets(
    project, tmp_path, mode, monkeypatch,
):
    backing, audio, image = [tmp_path / name for name in ("backing.wav", "prompt.mp3", "still.png")]
    for asset in (backing, audio, image):
        asset.write_bytes(asset.name.encode())
    project.backing_track_path = str(backing)
    project.segments = [
        Segment(0.5, 1.5, "Before", ["A"], audio_path=project.video_path),
        Segment(3, 4, "Inside", ["B"], "file", str(audio), str(image)),
        Segment(6, 8, "After", ["A"], "file", str(audio), str(image)),
    ]
    project.source_url = "https://example.com/source"
    project.import_warnings = ["Original import warning"]
    project.source_captions = [SourceCaption(1, 8, "Full text", "Creator")]
    project.analysis_review = AnalysisReview([
        AnalysisDraftRow("1", "8", "Review text", "Whisper", checked=False),
    ])
    original = copy.deepcopy(project)
    real_save = ProjectStore.save
    staged_paths = []

    def save_while_staged(result, path):
        assert path.parent.name.endswith(".staging")
        assert Path(result.video_path).is_file()
        assert Path(result.backing_track_path).is_file()
        real_save(result, path)
        staged_paths.append(path)

    monkeypatch.setattr(ProjectStore, "save", save_while_staged)
    result = execute(project, tmp_path, 2, 5, mode, title="Saved scene")
    snapshot = Path(result.video_path).parent / "project.cvpack.json"
    assert len(staged_paths) == 1 and not staged_paths[0].exists()
    assert snapshot.is_file()
    assert ".staging" not in snapshot.read_text(encoding="utf-8")
    saved = ProjectStore.load(snapshot)
    assert saved.to_dict() == result.to_dict()
    assert Path(saved.video_path).is_file()
    assert Path(saved.backing_track_path).is_file()
    for segment in saved.segments:
        if segment.audio_mode == "file":
            assert segment.audio_path == str(audio)
            assert segment.image_path == str(image)
        else:
            assert segment.audio_path == result.video_path
    assert project == original

    project.title = "Concurrent live edits"
    project.segments.clear()
    assert ProjectStore.load(snapshot).to_dict() == result.to_dict()


def test_scene_folder_can_be_relocated_with_its_saved_media(project, tmp_path):
    project.backing_track_path = project.video_path
    project.segments = [Segment(3, 4, "Kept line", ["Actor"], audio_path=project.video_path)]
    result = execute(project, tmp_path, 2, 5, "extract")
    original_folder = Path(result.video_path).parent
    saved = json.loads((original_folder / "project.cvpack.json").read_text(encoding="utf-8"))
    assert saved["video_path"] == "video.mkv"
    assert saved["backing_track_path"] == "backing.wav"
    assert saved["segments"][0]["audio_path"] == "video.mkv"
    moved = tmp_path / "Relocated scene"
    original_folder.rename(moved)
    restored = ProjectStore.load(moved / "project.cvpack.json")
    assert restored.video_path == str(moved / "video.mkv")
    assert restored.backing_track_path == str(moved / "backing.wav")
    assert restored.segments[0].audio_path == restored.video_path
    assert Path(restored.video_path).is_file()
    assert Path(restored.backing_track_path).is_file()


@pytest.mark.parametrize("cancel", [False, True])
def test_project_snapshot_save_failure_rolls_back_media(project, tmp_path, monkeypatch, cancel):
    original_files = set(tmp_path.iterdir())
    original = copy.deepcopy(project)
    real_save = ProjectStore.save
    cancelled = False

    def failed_save(result, path):
        nonlocal cancelled
        real_save(result, path)
        if cancel:
            cancelled = True
            return
        raise OSError("Snapshot save failed")

    monkeypatch.setattr(ProjectStore, "save", failed_save)
    with (
        operation_scope(cancelled=lambda: cancelled),
        pytest.raises(OperationCancelled if cancel else OSError),
    ):
        execute(project, tmp_path, 1, 2)
    assert set(tmp_path.iterdir()) == original_files
    assert project == original


@pytest.mark.parametrize("failure", [
    "render", "decode", "cancel", "source-change", "wrong-video-duration", "wrong-audio-duration",
])
def test_failure_and_cancellation_leave_no_artifacts(project, tmp_path, failure):
    before = copy.deepcopy(project)
    original_files = set(tmp_path.iterdir())
    media = FakeMedia(project, 8)
    cancel = False

    def fail():
        nonlocal cancel
        if failure in {"render", "decode"}:
            raise MediaError("injected failure")
        if failure == "cancel":
            cancel = True
        elif failure == "source-change":
            Path(project.video_path).write_bytes(b"external source replacement")

    if failure == "render":
        media.on_render = fail
    else:
        media.on_decode = fail
    media.bad_duration = failure == "wrong-video-duration"
    media.bad_audio = failure == "wrong-audio-duration"
    expected = (
        OperationCancelled if failure == "cancel" else
        SourceChangedError if failure == "source-change" else MediaError
    )
    with operation_scope(cancelled=lambda: cancel), pytest.raises(expected):
        execute_scene_edit(project, 2, 4, "cut", tmp_path, media)
    assert set(tmp_path.iterdir()) == original_files
    assert project == before
    if failure != "source-change":
        assert Path(project.video_path).read_bytes() == b"immutable source"


def test_late_cancel_is_deferred_after_publication_begins(project, tmp_path):
    cancelled = False
    committed = []

    def progress(message, _fraction):
        nonlocal cancelled
        if message.startswith("Publishing"):
            cancelled = True

    with operation_scope(
        cancelled=lambda: cancelled, progress=progress, committed=lambda: committed.append(True),
    ):
        result = execute(project, tmp_path, 1, 2)
    assert committed == [True]
    assert Path(result.video_path).is_file()


def test_publication_failure_removes_only_its_owned_directory(project, tmp_path, monkeypatch):
    original_rename = Path.rename
    before = set(tmp_path.iterdir())

    def replace_source(stage, target):
        renamed = original_rename(stage, target)
        Path(project.video_path).write_bytes(b"external edit during publish")
        return renamed

    monkeypatch.setattr(Path, "rename", replace_source)
    with pytest.raises(SourceChangedError):
        execute(project, tmp_path, 1, 2)
    assert set(tmp_path.iterdir()) == before


def test_failed_commit_callback_rolls_back_publication(project, tmp_path):
    before = set(tmp_path.iterdir())

    def failed_commit():
        raise RuntimeError("Cannot acknowledge publication")

    with operation_scope(committed=failed_commit), pytest.raises(RuntimeError, match="acknowledge"):
        execute(project, tmp_path, 1, 2)
    assert set(tmp_path.iterdir()) == before


def test_changed_retained_recording_aborts_publication(project, tmp_path):
    recording = tmp_path / "recording.mp3"
    recording.write_bytes(b"preserved recording")
    project.segments = [Segment(5, 7, audio_mode="file", audio_path=str(recording))]
    before = set(tmp_path.iterdir())
    media = FakeMedia(project, 8)
    media.on_decode = lambda: recording.write_bytes(b"external replacement")
    with pytest.raises(SourceChangedError):
        execute_scene_edit(project, 1, 3, "cut", tmp_path, media)
    assert set(tmp_path.iterdir()) == before
    assert Path(project.video_path).read_bytes() == b"immutable source"


@pytest.fixture
def real_media():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not available")
    return MediaTools()


def make_video(media, path, *, audio=True, delayed=False, fps=20, codec="ffv1"):
    filters = (
        f"color=red:s=64x48:r={fps}:d=3,"
        "drawbox=c=lime:t=fill:enable='gte(t,1)*lt(t,2)',"
        "drawbox=c=blue:t=fill:enable='gte(t,2)'"
    )
    command = [
        media.ffmpeg, "-v", "error", "-nostdin", "-n",
        "-threads", "2", "-filter_complex_threads", "2",
        "-f", "lavfi", "-i", filters,
    ]
    if audio:
        expression = (
            "aevalsrc=0.2:s=48000:d=0.6" if delayed else
            r"aevalsrc=if(lt(t\,1)\,0.1\,if(lt(t\,2)\,0.3\,0.5)):s=48000:d=3"
        )
        command += ["-f", "lavfi", "-i", expression]
    if delayed:
        command += [
            "-filter_complex", "[0:v]setpts=PTS+5/TB[v];[1:a]asetpts=PTS+5.4/TB[a]",
            "-map", "[v]", "-map", "[a]", "-copyts",
        ]
    command += ["-c:v", codec, "-threads:v", "2", "-fps_mode", "passthrough"]
    if codec == "mpeg4":
        command += ["-bf", "2", "-g", "40"]
    if audio:
        command += ["-c:a", "pcm_s16le"]
    media.run([*command, str(path)], "Generating original synthetic scene fixture")


def samples(media, source, destination):
    media.run([
        media.ffmpeg, "-v", "error", "-nostdin", "-y", "-threads", "2",
        "-i", str(source), "-map", "0:a:0", "-c:a", "pcm_s16le", str(destination),
    ], "Decoding synthetic scene samples")
    with wave.open(str(destination), "rb") as audio:
        assert audio.getnchannels() == 1
        result = array("h", audio.readframes(audio.getnframes()))
        return result, audio.getframerate()


def level(values, rate, start, end):
    part = values[round(start * rate):round(end * rate)]
    assert part
    return sum(part) / len(part) / 32768


@pytest.mark.integration
@pytest.mark.parametrize("codec", ["ffv1", "mpeg4"])
def test_real_middle_cut_removes_actual_frames_audio_and_backing(real_media, tmp_path, codec):
    media = real_media
    source, backing = tmp_path / "original.mkv", tmp_path / "original.wav"
    make_video(media, source, codec=codec)
    media.run([
        media.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
        r"aevalsrc=if(lt(t\,1)\,0.2\,if(lt(t\,2)\,0.4\,0.6)):s=48000:d=3",
        "-c:a", "pcm_s16le", str(backing),
    ], "Generating synthetic backing")
    original_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, backing)]
    project = PackProject(video_path=str(source), video_duration=3, backing_track_path=str(backing))
    before = copy.deepcopy(project)
    edited = execute_scene_edit(project, 1, 2, "cut", tmp_path, media)
    assert edited.video_duration == 2
    assert media.probe(Path(edited.video_path)).duration == pytest.approx(2, abs=0.051)
    decoded = tmp_path / "frames.rgb"
    media.run([
        media.ffmpeg, "-v", "error", "-threads", "2", "-i", edited.video_path,
        "-an", "-pix_fmt", "rgb24", "-f", "rawvideo", str(decoded),
    ], "Decoding synthetic scene frames")
    frame_bytes = 64 * 48 * 3
    data = decoded.read_bytes()
    assert len(data) // frame_bytes == 40
    colors = [tuple(data[offset:offset + 3]) for offset in range(0, len(data), frame_bytes)]
    assert all(r > 200 and g < 10 and b < 10 for r, g, b in colors[:20])
    assert all(b > 200 and r < 10 and g < 10 for r, g, b in colors[20:])
    for path, levels in ((edited.video_path, (0.1, 0.5)), (edited.backing_track_path, (0.2, 0.6))):
        values, rate = samples(media, path, tmp_path / f"{Path(path).stem}-samples.wav")
        assert len(values) / rate == pytest.approx(2, abs=1 / rate)
        assert level(values, rate, 0.1, 0.9) == pytest.approx(levels[0], abs=0.001)
        assert level(values, rate, 1.1, 1.9) == pytest.approx(levels[1], abs=0.001)
    assert project == before
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, backing)] == original_hashes


@pytest.mark.integration
def test_subframe_boundaries_keep_sample_accurate_sync_and_support_repeated_edits(real_media, tmp_path):
    media = real_media
    source = tmp_path / "original.mkv"
    make_video(media, source)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    project = PackProject(
        video_path=str(source), video_duration=3, backing_track_path=str(source),
        segments=[Segment(2, 2.8, "Later dialogue", ["A"])],
    )
    edited = execute_scene_edit(project, 0.413, 1.717, "cut", tmp_path, media)
    assert edited.video_duration == pytest.approx(1.696)
    assert edited.segments[0].start == pytest.approx(0.696)
    video_audio, rate = samples(media, edited.video_path, tmp_path / "video-audio.wav")
    backing_audio, _ = samples(media, edited.backing_track_path, tmp_path / "backing-audio.wav")
    assert video_audio == backing_audio
    assert len(video_audio) == round(edited.video_duration * rate)
    assert level(video_audio, rate, 0.1, 0.4) == pytest.approx(0.1, abs=0.001)
    assert level(video_audio, rate, 0.42, 0.69) == pytest.approx(0.3, abs=0.001)
    assert level(video_audio, rate, 0.7, 1.6) == pytest.approx(0.5, abs=0.001)
    original_derived = Path(edited.video_path).read_bytes()
    scene = execute_scene_edit(edited, 0.5, 1.6, "extract", tmp_path, media, title="Nested scene")
    assert scene.title == "Nested scene"
    assert scene.video_duration == pytest.approx(1.1)
    assert scene.segments[0].start == pytest.approx(0.196)
    assert Path(edited.video_path).read_bytes() == original_derived
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash


@pytest.mark.integration
@pytest.mark.parametrize("with_backing", [False, True])
def test_silent_video_and_short_backing_are_supported(real_media, tmp_path, with_backing):
    media = real_media
    source = tmp_path / "silent.mkv"
    make_video(media, source, audio=False)
    project = PackProject(video_path=str(source), video_duration=3)
    if with_backing:
        backing = tmp_path / "short.wav"
        media.run([
            media.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
            "aevalsrc=0.2:s=48000:d=0.35", "-c:a", "pcm_s16le", str(backing),
        ], "Generating short synthetic backing")
        project.backing_track_path = str(backing)
    edited = execute_scene_edit(project, 1, 2, "extract", tmp_path, media)
    assert not media.probe(Path(edited.video_path)).has_audio
    if with_backing:
        values, rate = samples(media, edited.backing_track_path, tmp_path / "padded.wav")
        assert len(values) == rate
        assert not any(values)
    else:
        assert not edited.backing_track_path


@pytest.mark.integration
def test_nonzero_video_timestamps_preserve_delayed_short_audio_and_pad_silence(real_media, tmp_path):
    media = real_media
    source = tmp_path / "offset.mkv"
    make_video(media, source, delayed=True)
    original = source.read_bytes()
    project = PackProject(video_path=str(source), video_duration=3, backing_track_path=str(source))
    edited = execute_scene_edit(project, 0.2, 1.8, "extract", tmp_path, media)
    values, rate = samples(media, edited.video_path, tmp_path / "aligned.wav")
    assert len(values) == round(1.6 * rate)
    assert level(values, rate, 0.01, 0.19) == pytest.approx(0, abs=0.001)
    assert level(values, rate, 0.21, 0.79) == pytest.approx(0.2, abs=0.001)
    assert level(values, rate, 0.81, 1.59) == pytest.approx(0, abs=0.001)
    backing, backing_rate = samples(media, edited.backing_track_path, tmp_path / "aligned-backing.wav")
    assert backing_rate == rate
    assert backing == values
    assert source.read_bytes() == original


@pytest.mark.integration
def test_stale_project_duration_is_rejected_before_rendering(real_media, tmp_path):
    source = tmp_path / "original.mkv"
    make_video(real_media, source)
    before = set(tmp_path.iterdir())
    project = PackProject(video_path=str(source), video_duration=4)
    with pytest.raises(ValueError, match="duration differs"):
        execute_scene_edit(project, 1, 2, "cut", tmp_path, real_media)
    assert set(tmp_path.iterdir()) == before


@pytest.mark.integration
def test_subframe_scene_retains_the_frame_already_visible_at_in(real_media, tmp_path):
    source = tmp_path / "slow.mkv"
    make_video(real_media, source, fps=10)
    project = PackProject(video_path=str(source), video_duration=3)
    edited = execute_scene_edit(project, 1.02, 1.07, "extract", tmp_path, real_media)
    assert edited.video_duration == pytest.approx(0.05)
    image = tmp_path / "single-frame.rgb"
    real_media.run([
        real_media.ffmpeg, "-v", "error", "-threads", "2", "-i", edited.video_path,
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", str(image),
    ], "Checking a scene shorter than one source frame")
    red, green, blue = image.read_bytes()[:3]
    assert green > 200 and red < 10 and blue < 10
    audio, rate = samples(real_media, edited.video_path, tmp_path / "short-scene.wav")
    assert len(audio) == round(0.05 * rate)
    assert level(audio, rate, 0, 0.05) == pytest.approx(0.3, abs=0.001)


@pytest.mark.integration
def test_edited_scene_exports_game_video_backing_and_retimed_prompts(real_media, tmp_path):
    media = real_media
    source = tmp_path / "export-source.mkv"
    media.run([
        media.ffmpeg, "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=192x144:r=20:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source),
    ], "Creating scene export fixture")
    project = PackProject(
        title="Scene export", authors=["Tester"], video_path=str(source),
        backing_track_path=str(source), video_duration=3, video_height=144, video_fps=20,
        segments=[
            Segment(0.2, 0.8, "First line", ["Actor"]),
            Segment(2.2, 2.8, "Last line", ["Actor"]),
        ],
    )
    edited = execute_scene_edit(project, 1, 2, "cut", tmp_path, media)
    saved = ProjectStore.load(Path(edited.video_path).parent / "project.cvpack.json")
    exported = PackExporter(media, cache_root=tmp_path / "cache").export(saved, tmp_path / "export")
    assert exported.validation["status"] == "passed"
    assert exported.validation["clip_count"] == 2
    assert exported.zip_path.is_file()
    assert media.probe(exported.pack_path / "dub_video.ogv").duration == pytest.approx(2, abs=0.1)
    assert media.probe_audio_duration(exported.pack_path / "_backing_track.mp3") == pytest.approx(2, abs=0.1)
    for index, timestamp in ((1, 0.05), (2, 1.05)):
        metadata = read_config(exported.pack_path / f"{index:03d}_Actor.txt")["data"]
        assert metadata["dub_timestamps"] == pytest.approx([timestamp])
