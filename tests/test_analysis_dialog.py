from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFileDialog, QLineEdit, QMessageBox

from choicer_voicer_pack_creator.analysis import (
    AnalysisCancelled,
    AnalysisResult,
    AnalysisSuggestion,
    detect_hardware,
)
from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    PackProject,
    SourceCaption,
)
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
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
        [AnalysisSuggestion(0.5, 3, "Whisper longer draft", "Whisper", 0.8)],
        1, 1, -30, "base", "en", detect_hardware(),
    ))
    assert dialog.table.item(0, 0).checkState() == Qt.CheckState.Unchecked
    assert dialog.table.item(0, 1).text() == "1.100"
    assert dialog.table.item(0, 3).text() == "My edit"
    assert dialog.table.columnCount() == 6
    assert dialog.local_table.item(0, 3).text() == "Whisper longer draft"
    assert dialog.local_table.item(0, 1).text() == "0.500"
    assert dialog.local_table.item(0, 2).text() == "3.000"
    dialog.local_radio.setChecked(True)
    assert dialog.checked_suggestions() == [
        AnalysisSuggestion(0.5, 3, "Whisper longer draft", "Whisper", 0.8)
    ]
    dialog.youtube_radio.setChecked(True)
    assert dialog.table.item(0, 3).text() == "My edit"
    assert dialog.checked_suggestions() == []


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
    assert "failed" in dialog.local_status.text()
    assert dialog.add_button.isEnabled()


@pytest.mark.parametrize("source", ["youtube", "local"])
def test_source_choice_imports_its_own_segmentation_and_saves_it(
    qtbot, tmp_path, source,
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    captions = [
        SourceCaption(1, 2, "First short line", "YouTube creator (en)"),
        SourceCaption(2, 3, "Second short line", "YouTube creator (en)"),
    ]
    window = MainWindow(UnusedMedia(), analysis_data_root=tmp_path / "analysis")
    qtbot.addWidget(window)
    window._set_project(
        PackProject(video_path=str(video), video_duration=10, source_captions=captions),
        tmp_path / "chosen.cvpack.json", mark_dirty=False,
    )
    dialog = AnalysisDialog(
        UnusedMedia(), video, 10, tmp_path / "analysis", 0, source_captions=captions,
    )
    qtbot.addWidget(dialog)
    dialog.suggestions_accepted.connect(window._add_analysis_suggestions)
    dialog.review_changed.connect(window._save_analysis_review)
    dialog._completed(AnalysisResult([
        AnalysisSuggestion(0.5, 3.5, "One longer Whisper line", "Whisper", 0.8),
        AnalysisSuggestion(4, 5, "", "Untranscribed activity"),
    ], 2, 1, -30, "base", "en", detect_hardware()))
    (dialog.youtube_radio if source == "youtube" else dialog.local_radio).setChecked(True)
    expected = dialog.checked_suggestions()
    assert len(expected) == (2 if source == "youtube" else 1)
    dialog.accept_suggestions()
    assert window.save_project()
    window.open_path(window.project_path)
    assert [(s.start, s.end, s.caption) for s in window.project.segments] == [
        (s.start, s.end, s.caption) for s in expected
    ]
    assert window.project.analysis_review.selected_source == source
    assert len(window.project.analysis_review.youtube_rows) == 2
    assert len(window.project.analysis_review.local_rows) == 1
    window.dirty = False
    window.close()


def test_closing_review_preserves_both_edited_drafts_and_checkboxes(
    qtbot, tmp_path,
):
    captions = [SourceCaption(1, 2, "Original", "YouTube creator (en)")]
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=captions,
    )
    qtbot.addWidget(dialog)
    dialog._completed(AnalysisResult([
        AnalysisSuggestion(0.5, 3, "Whisper draft", "Whisper", 0.876),
    ], 1, 1, -30, "base", "en", detect_hardware()))
    dialog.youtube_table.item(0, 3).setText("Edited YouTube")
    dialog.youtube_table.item(0, 1).setText("unfinished time")
    dialog.youtube_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.local_table.item(0, 3).setText("Edited Whisper")
    dialog.local_radio.setChecked(True)
    saved = []
    dialog.review_changed.connect(saved.append)
    dialog.reject()
    project = PackProject(source_captions=captions, analysis_review=saved[-1])
    path = tmp_path / "draft.cvpack.json"
    ProjectStore.save(project, path)
    recovery = RecoveryStore(tmp_path / "recovery.json")
    recovery.save(project, path)
    assert recovery.load().project.analysis_review == project.analysis_review
    loaded = ProjectStore.load(path)
    restored = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=loaded.source_captions, review=loaded.analysis_review,
    )
    qtbot.addWidget(restored)
    assert restored.review_state() == saved[-1]
    assert restored.local_radio.isChecked()
    assert restored.checked_suggestions() == [
        AnalysisSuggestion(0.5, 3, "Edited Whisper", "Whisper", 0.876)
    ]
    assert loaded.segments == []


@pytest.mark.parametrize("finish", ["close", "use"])
def test_finishing_commits_active_table_editor_to_draft(qtbot, tmp_path, finish):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube creator (en)")],
    )
    qtbot.addWidget(dialog)
    dialog.show()
    item = dialog.youtube_table.item(0, 3)
    dialog.youtube_table.editItem(item)
    editor = dialog.youtube_table.findChild(QLineEdit)
    assert editor is not None
    editor.selectAll()
    qtbot.keyClicks(editor, "New dialogue")
    changes = []
    accepted = []
    dialog.review_changed.connect(changes.append)
    dialog.suggestions_accepted.connect(accepted.extend)
    if finish == "close":
        dialog.reject()
    else:
        dialog.accept_suggestions()
        assert accepted[0].caption == "New dialogue"
    assert changes[-1].youtube_rows[0].caption == "New dialogue"


def test_no_youtube_captions_still_offers_whisper_source(qtbot, tmp_path):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
    )
    qtbot.addWidget(dialog)
    assert not dialog.youtube_radio.isEnabled()
    assert dialog.local_radio.isChecked()
    assert not dialog.add_button.isEnabled()
    dialog._completed(AnalysisResult([
        AnalysisSuggestion(0.5, 3, "Whisper only", "Whisper"),
    ], 1, 1, -30, "base", "en", detect_hardware()))
    assert dialog.add_button.isEnabled()
    assert dialog.checked_suggestions()[0].caption == "Whisper only"


def test_failed_rescan_keeps_previously_edited_local_draft(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)
    review = AnalysisReview(
        [AnalysisDraftRow("1", "2", "YouTube edit", "YouTube")],
        [AnalysisDraftRow("0.5", "3", "Whisper edit", "Whisper")],
        "local",
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True, review=review,
    )
    qtbot.addWidget(dialog)
    dialog._failed("Transient error")
    assert dialog.review_state() == review
    dialog._canceled()
    assert dialog.review_state() == review
    assert dialog.add_button.isEnabled()


def test_canceling_rescan_retains_local_and_youtube_drafts(qtbot, tmp_path, monkeypatch):
    import time

    runtime = tmp_path / "runtime"
    runtime.touch()
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(cli_path=runtime, model_path=lambda _key: runtime),
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes
    )

    def analyze(*_args, cancelled, **_kwargs):
        while not cancelled():
            time.sleep(0.01)
        raise AnalysisCancelled("Canceled")

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    review = AnalysisReview(
        [AnalysisDraftRow("1", "2", "YouTube draft", "YouTube")],
        [AnalysisDraftRow("0.5", "3", "Existing Whisper edit", "Whisper")],
        "local",
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True, review=review,
    )
    qtbot.addWidget(dialog)
    dialog.start_scan()
    assert dialog.review_state() == review
    assert not dialog.local_table.isEnabled()
    assert not dialog.add_button.isEnabled()
    dialog.cancel_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == review
    assert dialog.local_table.isEnabled()
    assert dialog.add_button.isEnabled()


@pytest.mark.parametrize("operation", ["replace", "clear"])
def test_source_video_change_clears_stale_drafts(qtbot, tmp_path, monkeypatch, operation):
    video = tmp_path / "video.mp4"
    replacement = tmp_path / "replacement.mp4"
    for path in (video, replacement):
        path.write_bytes(b"video")
    window = MainWindow(UnusedMedia(), analysis_data_root=tmp_path / "analysis")
    qtbot.addWidget(window)
    window._set_project(PackProject(
        video_path=str(video), video_duration=10,
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        source_captions=[SourceCaption(1, 2, "Original", "YouTube")],
        analysis_review=AnalysisReview(local_rows=[
            AnalysisDraftRow("1", "2", "Local draft", "Whisper"),
        ]),
    ), None, mark_dirty=False)
    if operation == "replace":
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(replacement), ""))
        monkeypatch.setattr(
            window.media, "probe", lambda _path: SimpleNamespace(duration=10), raising=False
        )
        window.choose_source_video()
    else:
        window.clear_source_video()
    assert window.project.analysis_review is None
    assert window.project.source_captions == []
    assert window.project.source_url == ""
    window.dirty = False
    window.close()
