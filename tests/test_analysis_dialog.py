from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QDialog, QFileDialog, QLineEdit, QMessageBox, QSplitter

from choicer_voicer_pack_creator.analysis import (
    AnalysisCancelled,
    AnalysisError,
    AnalysisResult,
    AnalysisSuggestion,
    detect_hardware,
)
from choicer_voicer_pack_creator.diagnostics import analysis_log_path
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
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


class UnusedMedia:
    pass


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
@pytest.mark.parametrize("tab_index", [0, 1], ids=["original", "refined"])
def test_transcript_divider_has_a_thin_gap_and_remains_draggable(
    qtbot, tmp_path: Path, stylesheet: str, tab_index: int,
) -> None:
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube creator (en)")],
    )
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(stylesheet)
    dialog.youtube_tabs.setCurrentIndex(tab_index)
    dialog.show()
    splitter = dialog.youtube_tabs.parentWidget()
    assert isinstance(splitter, QSplitter)
    qtbot.waitUntil(lambda: splitter.isVisible())

    for width in (1300, 1600):
        dialog.resize(width, 900)
        left = dialog.youtube_tabs.geometry()
        right = dialog.local_panel.geometry()
        assert right.x() - (left.x() + left.width()) == 1
        assert splitter.handleWidth() == 1
        assert splitter.handle(1).width() >= 5

    before = splitter.sizes()
    handle = splitter.handle(1)
    grab_point = handle.rect().center()
    target = handle.mapToGlobal(grab_point) + QPoint(80, 0)
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=grab_point)
    qtbot.mouseMove(handle, handle.mapFromGlobal(target))
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(target))
    qtbot.waitUntil(lambda: abs(splitter.sizes()[0] - before[0] - 80) <= 2)
    assert splitter.sizes()[1] < before[1]
    assert not splitter.childrenCollapsible()
    assert dialog.local_panel.x() - (
        dialog.youtube_tabs.x() + dialog.youtube_tabs.width()
    ) == 1


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


def test_caption_rows_are_editable_before_automatic_refinement(qtbot, tmp_path, monkeypatch) -> None:
    starts = []

    def start(dialog):
        starts.append(dialog.table.item(0, 3).text())

    monkeypatch.setattr(AnalysisDialog, "start_refinement", start)
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


@pytest.mark.parametrize("source", ["youtube", "refined"])
def test_adding_captions_during_background_scan_waits_for_cancellation(
    qtbot, tmp_path, monkeypatch, source,
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
        review=AnalysisReview(
            youtube_rows=[AnalysisDraftRow("1", "2", "Original", "YouTube")],
            refined_rows=[AnalysisDraftRow("1.1", "1.9", "Refined", "Refined YouTube")],
            selected_source=source,
        ),
    )
    qtbot.addWidget(dialog)
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    qtbot.waitUntil(lambda: dialog.worker is not None and dialog.worker.isRunning())
    assert dialog.scan_button.text() == "Whisper Running..."
    assert dialog.scan_button.objectName() != "primary"
    assert dialog.add_button.objectName() == "primary"
    label = "YouTube" if source == "youtube" else "Refined YouTube"
    assert dialog.add_button.text() == f"Use {label} Transcript"
    assert dialog.table.isEnabled()
    dialog.table.item(0, 3).setText("Edited while Whisper runs")
    dialog.accept_suggestions()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert accepted[0].caption == "Edited while Whisper runs"
    assert QDialog.result(dialog) == QDialog.DialogCode.Accepted
    log = analysis_log_path(tmp_path / "analysis")
    assert log.is_file()
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["event"] == "analysis_canceled"


@pytest.mark.parametrize("whisper_outcome", ["success", "fail", "decline"])
def test_automatic_refinement_precedes_whisper_and_keeps_its_selected_draft(
    qtbot, tmp_path, monkeypatch, whisper_outcome,
):
    runtime = tmp_path / "runtime"
    if whisper_outcome != "decline":
        runtime.touch()
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(
            cli_path=runtime, model_path=lambda _key: runtime,
            model_download_bytes=lambda _key: 74 * 1024**2,
        ),
    )
    captions = [SourceCaption(1, 3, "Original words", "YouTube creator (en)")]
    refined = [
        SourceCaption(1.1, 1.8, "Original", "Refined YouTube"),
        SourceCaption(2.3, 2.9, "words", "Refined YouTube"),
    ]
    calls = []

    def analyze(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs["source_captions"] is not None:
            return AnalysisResult(
                [], 2, 0, -30, None, None, detect_hardware(), refined_captions=refined,
            )
        if whisper_outcome == "fail":
            raise AnalysisError("Whisper unavailable")
        return AnalysisResult(
            [AnalysisSuggestion(1, 3, "Whisper words", "Whisper")],
            1, 1, -30, "tiny", "en", detect_hardware(),
        )

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    prompts = []

    def decline(_parent, title, *_args):
        prompts.append(title)
        assert dialog.refined_table.rowCount() == 2
        assert dialog.selected_source == "refined"
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", decline)
    errors = []
    monkeypatch.setattr(
        QMessageBox, "critical", lambda *_args: errors.append(_args[-1]),
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=captions, caption_language="en", auto_start=True,
    )
    qtbot.addWidget(dialog)
    saved = []
    dialog.review_changed.connect(saved.append)
    original_rows = dialog.review_state().youtube_rows
    qtbot.waitUntil(lambda: bool(calls) and dialog.worker is None)
    assert len(calls) == (1 if whisper_outcome == "decline" else 2)
    assert calls[0]["source_captions"] == captions
    assert calls[0]["use_whisper"] is False
    assert calls[0]["pause_threshold"] == 0.4
    if whisper_outcome == "decline":
        assert prompts == ["Download local transcription components?"]
        assert "not started" in dialog.progress_label.text()
        assert not runtime.exists()
    else:
        assert calls[1]["source_captions"] is None
        assert calls[1]["use_whisper"] is True
        assert not prompts
    assert bool(errors) == (whisper_outcome == "fail")
    assert dialog.local_table.rowCount() == (1 if whisper_outcome == "success" else 0)
    assert dialog.checked_suggestions() == [
        AnalysisSuggestion(cue.start, cue.end, cue.text, cue.source) for cue in refined
    ]
    assert saved[-1].selected_source == "refined"
    assert saved[-1].youtube_rows == original_rows
    assert len(saved[-1].refined_rows) == 2
    assert dialog.youtube_tabs.currentIndex() == 1
    assert dialog.add_button.text() == "Use Refined YouTube Transcript"
    assert dialog.add_button.isEnabled()
    assert dialog.refine_button.isEnabled()
    assert dialog.source_captions == captions


@pytest.mark.parametrize("outcome", ["fail", "cancel", "close", "use"])
def test_interrupted_automatic_refinement_does_not_start_whisper(
    qtbot, tmp_path, monkeypatch, outcome,
):
    import time

    calls = []

    def analyze(*_args, cancelled, **kwargs):
        calls.append(kwargs)
        if outcome == "fail":
            raise AnalysisError("Audio extraction failed")
        while not cancelled():
            time.sleep(0.01)
        raise AnalysisCancelled("Canceled")

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)
    whisper_starts = []
    monkeypatch.setattr(AnalysisDialog, "start_scan", lambda _self: whisper_starts.append(True))
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 3, "Original", "YouTube")], auto_start=True,
    )
    qtbot.addWidget(dialog)
    review = dialog.review_state()
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    qtbot.waitUntil(lambda: bool(calls))
    if outcome == "cancel":
        dialog.cancel_scan()
    elif outcome == "close":
        dialog.reject()
    elif outcome == "use":
        dialog.accept_suggestions()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == review
    assert len(calls) == 1
    assert not whisper_starts
    if outcome == "fail":
        assert "failed" in dialog.refined_status.text()
    elif outcome == "cancel":
        assert "canceled" in dialog.refined_status.text()
        assert dialog.refine_button.isEnabled()
    elif outcome == "use":
        assert [suggestion.caption for suggestion in accepted] == ["Original"]
        assert dialog.result() == QDialog.DialogCode.Accepted
    else:
        assert dialog.result() == QDialog.DialogCode.Rejected


@pytest.mark.parametrize("finish", ["close", "use"])
def test_finishing_before_automatic_refinement_starts_keeps_original_captions(
    qtbot, tmp_path, monkeypatch, finish,
):
    starts = []
    monkeypatch.setattr(AnalysisDialog, "start_refinement", lambda _self: starts.append("refined"))
    monkeypatch.setattr(AnalysisDialog, "start_scan", lambda _self: starts.append("whisper"))
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 3, "Original", "YouTube")], auto_start=True,
    )
    qtbot.addWidget(dialog)
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    if finish == "close":
        dialog.reject()
    else:
        dialog.accept_suggestions()
    qtbot.wait(10)
    assert not starts
    assert dialog.checked_suggestions()[0].caption == "Original"
    assert len(accepted) == (1 if finish == "use" else 0)


@pytest.mark.parametrize("youtube_import", [False, True])
def test_automatic_analysis_without_captions_starts_whisper_directly(
    qtbot, tmp_path, monkeypatch, youtube_import,
):
    starts = []
    monkeypatch.setattr(AnalysisDialog, "start_scan", lambda _self: starts.append("whisper"))
    monkeypatch.setattr(AnalysisDialog, "start_refinement", lambda _self: starts.append("refined"))
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=youtube_import, auto_start=True,
    )
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: bool(starts))
    assert starts == ["whisper"]


@pytest.mark.parametrize("refined_rows", [[], [
    AnalysisDraftRow("1.1", "2.9", "Refined edit", "Refined YouTube", checked=False),
]])
def test_restoring_drafts_does_not_automatically_refine(
    qtbot, tmp_path, monkeypatch, refined_rows,
):
    starts = []
    monkeypatch.setattr(AnalysisDialog, "start_scan", lambda _self: starts.append("whisper"))
    monkeypatch.setattr(AnalysisDialog, "start_refinement", lambda _self: starts.append("refined"))
    review = AnalysisReview(
        youtube_rows=[AnalysisDraftRow("1", "3", "YouTube edit", "YouTube")],
        refined_rows=refined_rows,
        selected_source="refined" if refined_rows else "youtube", pause_threshold=0.6,
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 3, "Original", "YouTube")], review=review,
    )
    qtbot.addWidget(dialog)
    qtbot.wait(10)
    assert not starts
    assert dialog.review_state() == review


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


@pytest.mark.parametrize("source", ["youtube", "local", "refined"])
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
    dialog._completed(AnalysisResult(
        [], 1, 0, -30, None, None, detect_hardware(),
        refined_captions=[SourceCaption(1.1, 2.9, "Refined line", "Refined YouTube")],
    ))
    {
        "youtube": dialog.youtube_radio,
        "refined": dialog.refined_radio,
        "local": dialog.local_radio,
    }[source].setChecked(True)
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
    assert len(window.project.analysis_review.refined_rows) == 1
    window.dirty = False
    window.close()


def test_closing_review_preserves_all_edited_drafts_and_checkboxes(
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
    dialog._completed(AnalysisResult(
        [], 1, 0, -30, None, None, detect_hardware(),
        refined_captions=[SourceCaption(1.1, 1.9, "Refined draft", "Refined YouTube")],
    ))
    dialog.youtube_table.item(0, 3).setText("Edited YouTube")
    dialog.youtube_table.item(0, 1).setText("unfinished time")
    dialog.youtube_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.local_table.item(0, 3).setText("Edited Whisper")
    dialog.refined_table.item(0, 3).setText("Edited refinement")
    dialog.refined_table.item(0, 2).setText("unfinished out")
    dialog.refined_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.pause_spin.setValue(0.65)
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
    assert restored.refined_radio.isChecked()
    assert restored.youtube_tabs.currentIndex() == 1
    assert restored.pause_spin.value() == 0.65
    assert restored.checked_suggestions() == []
    restored.local_radio.setChecked(True)
    assert restored.checked_suggestions() == [
        AnalysisSuggestion(0.5, 3, "Edited Whisper", "Whisper", 0.876)
    ]
    assert loaded.segments == []


@pytest.mark.parametrize("finish", ["close", "use"])
@pytest.mark.parametrize("source", ["youtube", "refined"])
def test_finishing_commits_active_table_editor_to_draft(qtbot, tmp_path, finish, source):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube creator (en)")],
    )
    qtbot.addWidget(dialog)
    if source == "refined":
        dialog._completed(AnalysisResult(
            [], 1, 0, -30, None, None, detect_hardware(),
            refined_captions=[SourceCaption(1, 2, "Refined", "Refined YouTube")],
        ))
    dialog.show()
    item = dialog.table.item(0, 3)
    dialog.table.editItem(item)
    editor = dialog.table.findChild(QLineEdit)
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
    rows = changes[-1].youtube_rows if source == "youtube" else changes[-1].refined_rows
    assert rows[0].caption == "New dialogue"


def test_no_youtube_captions_still_offers_whisper_source(qtbot, tmp_path):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
    )
    qtbot.addWidget(dialog)
    assert not dialog.youtube_radio.isEnabled()
    assert not dialog.refined_radio.isEnabled()
    assert not dialog.refine_button.isEnabled()
    assert dialog.local_radio.isChecked()
    assert not dialog.add_button.isEnabled()
    dialog._completed(AnalysisResult([
        AnalysisSuggestion(0.5, 3, "Whisper only", "Whisper"),
    ], 1, 1, -30, "base", "en", detect_hardware()))
    assert dialog.add_button.isEnabled()
    assert dialog.checked_suggestions()[0].caption == "Whisper only"
    assert dialog.scan_button.text() == "Rerun Whisper..."
    assert dialog.add_button.text() == "Use Whisper Transcript"


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


def test_new_local_video_starts_whisper_automatically(qtbot, tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    media = UnusedMedia()
    monkeypatch.setattr(media, "probe", lambda _path: SimpleNamespace(duration=10), raising=False)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(video), ""))
    window = MainWindow(media, analysis_data_root=tmp_path / "analysis")
    qtbot.addWidget(window)
    scans = []
    monkeypatch.setattr(window, "open_analysis_dialog", lambda **kwargs: scans.append(kwargs))
    window.new_from_video()
    qtbot.waitUntil(lambda: bool(scans))
    assert scans == [{"initial_scan": True, "auto_start": True}]
    window.dirty = False
    window.close()


def test_source_labels_and_play_action_identify_the_selected_transcript(qtbot, tmp_path):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "YouTube line", "YouTube creator (en)")],
    )
    qtbot.addWidget(dialog)
    assert dialog.youtube_panel.title() == "YouTube Captions"
    assert dialog.local_panel.title() == "Whisper Transcript"
    assert not dialog.preview_button.isEnabled()
    dialog.youtube_table.selectRow(0)
    assert dialog.preview_button.isEnabled()
    assert dialog.preview_button.text() == "Play Selected YouTube Line"
    previews = []
    dialog.preview_requested.connect(lambda start, end: previews.append((start, end)))
    dialog.preview_button.click()
    assert previews == [(1, 2)]
    dialog._completed(AnalysisResult(
        [AnalysisSuggestion(0.5, 3, "Whisper line", "Whisper")],
        1, 1, -30, "base", "en", detect_hardware(),
    ))
    dialog.local_radio.setChecked(True)
    assert dialog.preview_button.text() == "Play Selected Whisper Line"
    assert dialog.add_button.text() == "Use Whisper Transcript"
    assert dialog.add_button.isDefault()
    assert not dialog.scan_button.autoDefault()
    dialog.preview_button.click()
    assert previews[-1] == (0.5, 3)


def test_refinement_runs_without_whisper_and_replaces_only_its_draft(
    qtbot, tmp_path, monkeypatch,
):
    calls = []

    def analyze(*_args, **kwargs):
        calls.append(kwargs)
        return AnalysisResult(
            [], 2, 0, -30, None, None, detect_hardware(),
            refined_captions=[
                SourceCaption(1.1, 1.8, "First", "Refined YouTube"),
                SourceCaption(2.3, 2.9, "Second", "Refined YouTube"),
            ],
        )

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    review = AnalysisReview(
        youtube_rows=[AnalysisDraftRow("1", "3", "Edited original", "YouTube")],
        local_rows=[AnalysisDraftRow("0.5", "3.5", "Edited Whisper", "Whisper")],
        selected_source="local",
        refined_rows=[AnalysisDraftRow("1", "3", "Previous refined edit", "Refined YouTube")],
        pause_threshold=0.55,
    )
    captions = [SourceCaption(1, 3, "Original imported words", "YouTube automatic (en)")]
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=captions, review=review,
    )
    qtbot.addWidget(dialog)

    missing = tmp_path / "missing-whisper"
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(cli_path=missing, model_path=lambda _key: missing),
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel
    )
    dialog.start_refinement()
    assert not calls
    assert dialog.review_state() == review
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes
    )
    dialog.start_refinement()
    assert not dialog.refined_table.isEnabled()
    assert dialog.youtube_table.isEnabled()
    assert dialog.local_table.isEnabled()
    assert not dialog.pause_spin.isEnabled()
    assert not dialog.refine_button.isEnabled()
    assert dialog.review_state() == review
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert len(calls) == 1
    assert calls[0]["use_whisper"] is False
    assert calls[0]["source_captions"] == captions
    assert calls[0]["pause_threshold"] == 0.55
    saved = dialog.review_state()
    assert saved.youtube_rows == review.youtube_rows
    assert saved.local_rows == review.local_rows
    assert dialog.source_captions == captions
    assert [row.caption for row in saved.refined_rows] == ["First", "Second"]
    assert saved.selected_source == "refined"
    assert dialog.youtube_tabs.currentIndex() == 1
    assert dialog.add_button.text() == "Use Refined YouTube Transcript"
    assert dialog.preview_button.text() == "Play Selected Refined YouTube Line"
    previews = []
    dialog.preview_requested.connect(lambda start, end: previews.append((start, end)))
    dialog.preview_button.click()
    assert previews == [(1.1, 1.8)]
    assert dialog.refine_button.isEnabled()
    assert dialog.pause_spin.isEnabled()


@pytest.mark.parametrize("outcome", ["fail", "cancel", "close", "use"])
def test_refinement_interruption_keeps_all_drafts(qtbot, tmp_path, monkeypatch, outcome):
    import time

    from choicer_voicer_pack_creator.analysis import AnalysisError

    def analyze(*_args, cancelled, **_kwargs):
        if outcome == "fail":
            raise AnalysisError("Audio extraction failed")
        while not cancelled():
            time.sleep(0.01)
        raise AnalysisCancelled("Canceled")

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes
    )
    review = AnalysisReview(
        youtube_rows=[AnalysisDraftRow("1", "3", "YouTube edit", "YouTube")],
        local_rows=[AnalysisDraftRow("0.5", "3.5", "Whisper edit", "Whisper")],
        selected_source="youtube",
        refined_rows=[AnalysisDraftRow("1.1", "2.9", "Refined edit", "Refined YouTube")],
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 3, "Original", "YouTube")], review=review,
    )
    qtbot.addWidget(dialog)
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    dialog.start_refinement()
    if outcome == "cancel":
        dialog.cancel_scan()
    elif outcome == "close":
        dialog.reject()
    elif outcome == "use":
        dialog.accept_suggestions()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == review
    assert dialog.source_captions[0].text == "Original"
    if outcome == "fail":
        assert "failed" in dialog.refined_status.text()
        assert "Saved Whisper draft" in dialog.local_status.text()
    elif outcome == "cancel":
        assert "canceled" in dialog.refined_status.text()
        assert dialog.refined_table.isEnabled()
    elif outcome == "use":
        assert [suggestion.caption for suggestion in accepted] == ["YouTube edit"]
        assert dialog.result() == QDialog.DialogCode.Accepted
    else:
        assert dialog.result() == QDialog.DialogCode.Rejected


def test_whisper_rescan_leaves_refined_edits_and_pause_setting_unchanged(qtbot, tmp_path):
    review = AnalysisReview(
        refined_rows=[
            AnalysisDraftRow("1.05", "unfinished", "Edited refinement", "Refined YouTube", checked=False),
        ],
        selected_source="refined",
        pause_threshold=0.7,
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        review=review,
    )
    qtbot.addWidget(dialog)
    assert dialog.source_choice
    dialog._completed(AnalysisResult(
        [AnalysisSuggestion(1, 3, "New Whisper", "Whisper")],
        1, 1, -30, "base", "en", detect_hardware(),
    ))
    assert dialog.review_state().refined_rows == review.refined_rows
    assert dialog.review_state().pause_threshold == 0.7
    assert dialog.selected_source == "refined"
    assert not dialog.refine_button.isEnabled()
    assert dialog.local_table.item(0, 3).text() == "New Whisper"


def test_compact_review_keeps_refined_rows_visible(qtbot, tmp_path):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 3, "Original", "YouTube")],
        review=AnalysisReview(
            refined_rows=[AnalysisDraftRow("1", "3", "Refined", "Refined YouTube")],
            selected_source="refined",
        ),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.resize(760, 520)
    qtbot.waitUntil(lambda: dialog.refined_table.viewport().height() >= 70)
    assert dialog.refined_panel.contentsRect().contains(dialog.refined_table.geometry())
    assert dialog.refine_button.isVisible()
    assert dialog.add_button.isVisible()


@pytest.mark.parametrize("source", ["local", "refined"])
@pytest.mark.parametrize("finish", ["cancel", "close", "use"])
def test_late_cancellation_discards_completion_queued_after_final_worker_check(
    qtbot, tmp_path, monkeypatch, source, finish,
):
    from contextlib import contextmanager
    from threading import Event

    final_check_passed = Event()
    release_worker = Event()

    @contextmanager
    def diagnostics(_root):
        yield SimpleNamespace(progress=lambda *_: None)
        final_check_passed.set()
        assert release_worker.wait(5)

    regenerated = AnalysisResult(
        [AnalysisSuggestion(1, 3, "New Whisper", "Whisper")],
        1, 1, -30, "base", "en", detect_hardware(),
        refined_captions=(
            [SourceCaption(1, 3, "New refinement", "Refined YouTube")]
            if source == "refined" else None
        ),
    )
    monkeypatch.setattr(analysis_dialog, "AnalysisDiagnostics", diagnostics)
    monkeypatch.setattr(analysis_dialog, "analyze_video", lambda *_args, **_kwargs: regenerated)
    runtime = tmp_path / "runtime"
    runtime.touch()
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(cli_path=runtime, model_path=lambda _key: runtime),
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes
    )
    review = AnalysisReview(
        youtube_rows=[AnalysisDraftRow("1", "3", "YouTube edit", "YouTube")],
        local_rows=[AnalysisDraftRow("1.1", "2.9", "Whisper edit", "Whisper", checked=False)],
        refined_rows=[
            AnalysisDraftRow("1.2", "2.8", "Refined edit", "Refined YouTube", checked=False),
        ],
        selected_source="youtube",
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 3, "Original", "YouTube")], review=review,
    )
    qtbot.addWidget(dialog)
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    if source == "refined":
        dialog.start_refinement()
    else:
        dialog.start_scan()
    try:
        qtbot.waitUntil(final_check_passed.is_set)
        if finish == "cancel":
            dialog.cancel_scan()
        elif finish == "close":
            dialog.reject()
        else:
            dialog.accept_suggestions()
        assert dialog.worker.isInterruptionRequested()
    finally:
        release_worker.set()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == review
    assert [suggestion.caption for suggestion in accepted] == (
        ["YouTube edit"] if finish == "use" else []
    )
