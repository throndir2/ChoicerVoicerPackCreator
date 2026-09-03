from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from choicer_voicer_pack_creator.analysis import AnalysisSuggestion
from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.ui.analysis_dialog import AnalysisDialog
from choicer_voicer_pack_creator.ui.main_window import MainWindow


class UnusedMedia:
    pass


def test_analysis_dialog_returns_only_checked_edited_suggestions(qtbot, tmp_path: Path) -> None:
    dialog = AnalysisDialog(
        UnusedMedia(),  # type: ignore[arg-type]
        tmp_path / "video.mp4",
        10,
        tmp_path / "analysis",
        0,
    )
    qtbot.addWidget(dialog)
    dialog._populate(
        [
            AnalysisSuggestion(1, 2, "First draft", "Whisper", 0.8),
            AnalysisSuggestion(4, 5, "", "Audio activity"),
        ]
    )
    dialog.table.item(0, 3).setText("Corrected transcript")
    dialog.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    previews: list[tuple[float, float]] = []
    dialog.preview_requested.connect(lambda start, end: previews.append((start, end)))
    dialog.preview_row(0)

    selected = dialog.checked_suggestions()

    assert previews == [(1.0, 2.0)]
    assert selected == [
        AnalysisSuggestion(1, 2, "Corrected transcript", "Whisper", 0.8)
    ]


def test_main_window_adds_suggestions_without_speakers_or_duplicates(
    qtbot, tmp_path: Path
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    window = MainWindow(UnusedMedia(), analysis_data_root=tmp_path / "analysis")  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window._set_project(
        PackProject(
            title="Analysis",
            authors=["Creator"],
            video_path=str(video),
            video_duration=10,
        ),
        None,
        mark_dirty=False,
    )
    suggestion = AnalysisSuggestion(1, 2, "Draft line", "Whisper", 0.8)

    window._add_analysis_suggestions([suggestion, suggestion])

    assert len(window.project.segments) == 1
    segment = window.project.segments[0]
    assert (segment.start, segment.end, segment.caption) == (1, 2, "Draft line")
    assert segment.characters == []
    assert segment.audio_mode == "video"
    assert window.selected_segment_id == segment.id
    assert window.dirty
    window.dirty = False
    window.close()


def test_suggestion_range_dedup_uses_canonical_milliseconds(qtbot, tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    window = MainWindow(UnusedMedia(), analysis_data_root=tmp_path / "analysis")  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window._set_project(
        PackProject(
            title="Dedup",
            authors=["Creator"],
            video_path=str(video),
            video_duration=10,
        ),
        None,
        mark_dirty=False,
    )
    window._add_analysis_suggestions(
        [AnalysisSuggestion(1.0, 2.0, "Original", "Whisper")]
    )

    window._add_analysis_suggestions(
        [
            AnalysisSuggestion(1.05, 2.05, "At 50 ms", "Whisper"),
            AnalysisSuggestion(1.051, 2.051, "At 51 ms", "Whisper"),
        ]
    )

    assert [segment.caption for segment in window.project.segments] == [
        "Original",
        "At 51 ms",
    ]
    window.dirty = False
    window.close()
