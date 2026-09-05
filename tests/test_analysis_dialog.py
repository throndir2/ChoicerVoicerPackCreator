from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from choicer_voicer_pack_creator.analysis import (
    AnalysisCancelled,
    AnalysisResult,
    AnalysisSuggestion,
    detect_hardware,
)
from choicer_voicer_pack_creator.models import PackProject, SourceCaption
from choicer_voicer_pack_creator.ui import analysis_dialog
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


def test_caption_rows_are_editable_before_automatic_scan(qtbot, tmp_path, monkeypatch) -> None:
    starts = []

    def start(dialog):
        starts.append(dialog.table.item(0, 3).text())

    monkeypatch.setattr(AnalysisDialog, "start_scan", start)
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "YouTube text", "YouTube creator (en)")],
        caption_language="en-US", auto_start=True,
    )
    qtbot.addWidget(dialog)
    assert dialog.table.item(0, 3).text() == "YouTube text"
    assert dialog.add_button.isEnabled()
    assert dialog.language_combo.currentData() == "en"
    qtbot.waitUntil(lambda: bool(starts))
    assert starts == ["YouTube text"]


def test_whisper_completion_does_not_replace_edits_or_checks(qtbot, tmp_path) -> None:
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube automatic (en)")],
    )
    qtbot.addWidget(dialog)
    dialog.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.table.item(0, 1).setText("1.100")
    dialog.table.item(0, 3).setText("My edit")
    dialog._completed(AnalysisResult(
        [AnalysisSuggestion(1, 2, "Whisper draft", "Whisper", 0.8)],
        1, 1, -30, "base", "en", detect_hardware(),
    ))
    assert dialog.table.item(0, 0).checkState() == Qt.CheckState.Unchecked
    assert dialog.table.item(0, 1).text() == "1.100"
    assert dialog.table.item(0, 3).text() == "My edit"
    assert dialog.table.item(0, 6).text() == "Whisper draft"
    assert dialog.table.item(0, 7).text() == "Text differs - review"
    dialog.table.item(0, 3).setText("Whisper draft")
    assert dialog.table.item(0, 7).text() == "Text agrees"


def test_adding_captions_during_background_scan_waits_for_cancellation(
    qtbot, tmp_path, monkeypatch,
) -> None:
    import time

    runtime = tmp_path / "runtime"
    runtime.touch()
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(cli_path=runtime, model_path=lambda _key: runtime),
    )

    def analyze(*_args, cancelled, **_kwargs):
        while not cancelled():
            time.sleep(0.01)
        raise AnalysisCancelled("Canceled")

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube creator (en)")],
        auto_start=True,
    )
    qtbot.addWidget(dialog)
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    qtbot.waitUntil(lambda: dialog.worker is not None and dialog.worker.isRunning())
    assert dialog.table.isEnabled()
    dialog.table.item(0, 3).setText("Edited while Whisper runs")
    dialog.accept_suggestions()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert accepted[0].caption == "Edited while Whisper runs"
    assert QDialog.result(dialog) == QDialog.DialogCode.Accepted


def test_declining_initial_whisper_download_keeps_captions(qtbot, tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(
            cli_path=missing, model_path=lambda _key: missing,
            model_download_bytes=lambda _key: 74 * 1024**2,
        ),
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Available now", "YouTube creator (en)")],
        auto_start=True,
    )
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: "not started" in dialog.progress_label.text())
    assert dialog.worker is None
    assert dialog.checked_suggestions()[0].caption == "Available now"
    assert dialog.add_button.isEnabled()
    assert not missing.exists()


def test_whisper_failure_keeps_edited_caption_rows(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube creator (en)")],
    )
    qtbot.addWidget(dialog)
    dialog.table.item(0, 3).setText("Edited")
    dialog._failed("Model unavailable")
    assert dialog.checked_suggestions()[0].caption == "Edited"
    assert dialog.table.item(0, 7).text() == "Whisper unavailable"
    assert dialog.add_button.isEnabled()
