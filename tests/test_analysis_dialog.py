from __future__ import annotations

import json
from pathlib import Path
from threading import Event, current_thread, main_thread
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLineEdit,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTabWidget,
)

from choicer_voicer_pack_creator.analysis import (
    AnalysisCancelled,
    AnalysisError,
    AnalysisResult,
    AnalysisSuggestion,
    detect_hardware,
)
from choicer_voicer_pack_creator.diagnostics import analysis_log_path
from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.models import (
    AnalysisDraftRow,
    AnalysisReview,
    PackProject,
    SourceCaption,
)
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui import analysis_dialog, backing_dialog
from choicer_voicer_pack_creator.ui.analysis_dialog import AnalysisDialog
from choicer_voicer_pack_creator.ui.main_window import MainWindow, ProjectEditor
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


class UnusedMedia:
    pass


@pytest.fixture
def managed_jobs(qtbot):
    manager = JobManager(limits={"cpu": 2, "io": 1, "network": 1})
    yield manager
    manager.shutdown(cancel=True, wait=True)


@pytest.mark.parametrize("manager_location", ["direct", "workspace"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_managed_diagnostic_bundle_uses_background_io_and_nonmodal_notice(
    qtbot, tmp_path, monkeypatch, managed_jobs, manager_location, outcome,
):
    import zipfile

    parent = QDialog()
    qtbot.addWidget(parent)
    if manager_location == "direct":
        parent.job_manager = managed_jobs
        parent.project_id = "project-a"
    else:
        parent.workspace = SimpleNamespace(job_manager=managed_jobs)
        parent.session = SimpleNamespace(id="project-a")
    data_root = tmp_path / "analysis"
    log = analysis_log_path(data_root)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"event": "test_diagnostics"}\n', encoding="utf-8")
    destination = tmp_path / "diagnostics.zip"
    started, release = Event(), Event()
    on_gui_thread = []
    save_bundle = analysis_dialog.save_diagnostic_bundle

    def save(root, output):
        on_gui_thread.append(current_thread() is main_thread())
        started.set()
        assert release.wait(5)
        if outcome == "failure":
            raise OSError("Destination unavailable")
        return save_bundle(root, output)

    monkeypatch.setattr(analysis_dialog, "save_diagnostic_bundle", save)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))
    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *_a: pytest.fail("Modal diagnostic notice"))
    try:
        analysis_dialog.save_diagnostic_logs(parent, data_root)
        qtbot.waitUntil(started.is_set)
        assert not destination.exists()
        assert on_gui_thread == [False]
        record = managed_jobs.active_jobs()[0]
        assert (record.project_id, record.kind, record.resource_class) == (
            "project-a", "diagnostics", "io",
        )
        parent.hide()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not managed_jobs.active_jobs())
    box = next(box for box in parent.findChildren(QMessageBox) if box.isVisible())
    assert not box.isModal()
    assert box.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    if outcome == "success":
        assert managed_jobs.tasks()[0].state == "succeeded"
        assert box.windowTitle() == "Diagnostic bundle saved"
        with zipfile.ZipFile(destination) as archive:
            assert {"analysis.log", "support-info.json"} <= set(archive.namelist())
    else:
        assert managed_jobs.tasks()[0].state == "failed"
        assert "Destination unavailable" in box.text()
        assert not destination.exists()
    box.close()


def test_workspace_whisper_and_refinement_start_independently_and_survive_use(
    qtbot, tmp_path, monkeypatch, installed_whisper, managed_jobs,
):
    started = {False: Event(), True: Event()}
    release = {False: Event(), True: Event()}
    canceled = []

    def analyze(*_args, source_captions, cancelled, **_kwargs):
        refine = source_captions is not None
        started[refine].set()
        assert release[refine].wait(5)
        canceled.append(cancelled())
        return AnalysisResult(
            [AnalysisSuggestion(1, 2, "Whisper result", "Whisper")],
            1, 1, -30, None if refine else "tiny", "en", detect_hardware(),
            refined_captions=[SourceCaption(1, 2, "Refined result", "Refined YouTube")]
            if refine else None,
        )

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: pytest.fail("Modal consent"))
    details = {}
    host = QDialog()
    qtbot.addWidget(host)
    host.workspace = SimpleNamespace(tasks_window=SimpleNamespace(
        register_detail=lambda job_id, widget: details.__setitem__(job_id, widget),
    ))
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0, host,
        source_captions=[SourceCaption(1, 2, "Evidence", "YouTube")], auto_start=True,
        job_manager=managed_jobs, project_id="project-a", source_snapshot={"revision": 7},
    )
    qtbot.addWidget(dialog)
    dialog.show()
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    try:
        qtbot.waitUntil(lambda: all(event.is_set() for event in started.values()))
        assert dialog.worker is not None and dialog.refinement_worker is not None
        assert details == {
            dialog.worker.job_handle.id: dialog,
            dialog.refinement_worker.job_handle.id: dialog,
        }
        assert {job.kind for job in managed_jobs.active_jobs("project-a")} == {
            "analysis", "refinement",
        }
        assert all(job.source_snapshot["revision"] == 7 for job in managed_jobs.tasks())
        assert not dialog.isModal()
        assert dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        dialog.close()
        assert not dialog.isVisible()
        assert not any(job.cancel_requested for job in managed_jobs.active_jobs())
        release[False].set()
        qtbot.waitUntil(lambda: dialog.worker is None)
        dialog.show()
        dialog.local_add_button.click()
        assert accepted[0].caption == "Whisper result"
        assert dialog.refinement_worker is not None
        assert not dialog.refinement_worker.isInterruptionRequested()
        assert not dialog.isVisible()
    finally:
        for event in release.values():
            event.set()
        qtbot.waitUntil(
            lambda: dialog.worker is None and dialog.refinement_worker is None
        )
    assert canceled == [False, False]
    assert dialog.refined_table.item(0, 3).text() == "Refined result"
    assert len(accepted) == 1


def test_workspace_backing_and_whisper_share_memory_lease_but_refinement_can_run(
    qtbot, tmp_path, monkeypatch, installed_whisper, managed_jobs,
):
    backing_started, release_backing, whisper_started = Event(), Event(), Event()
    output = tmp_path / "backing.wav"
    output.write_bytes(b"backing")

    class Separation:
        def __init__(self, _root):
            pass

        def generate(self, *_args, **_kwargs):
            backing_started.set()
            assert release_backing.wait(5)
            return output

    def analyze(*_args, use_whisper, **_kwargs):
        if use_whisper:
            whisper_started.set()
        return AnalysisResult(
            [AnalysisSuggestion(1, 2, "Local result", "Whisper")],
            1, 1, -30, "tiny", "en", detect_hardware(),
            refined_captions=None if use_whisper else [
                SourceCaption(1, 2, "Refined result", "Refined YouTube"),
            ],
        )

    monkeypatch.setattr(backing_dialog, "SeparationManager", Separation)
    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    backing = backing_dialog.BackingDialog(
        UnusedMedia(), tmp_path / "other-video.mp4", tmp_path / "backing-cache",
        job_manager=managed_jobs, project_id="project-b",
    )
    qtbot.addWidget(backing)
    dialog = None
    try:
        qtbot.waitUntil(backing_started.is_set)
        dialog = AnalysisDialog(
            UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
            source_captions=[SourceCaption(1, 2, "Evidence", "YouTube")], auto_start=True,
            job_manager=managed_jobs, project_id="project-a",
        )
        qtbot.addWidget(dialog)
        qtbot.waitUntil(lambda: dialog.refined_table.rowCount() == 1)
        assert backing.worker is not None
        assert dialog.worker is not None
        assert dialog.worker.job_handle.record.state in {"queued", "waiting"}
        assert not whisper_started.is_set()
        assert {job.kind for job in managed_jobs.tasks("project-a")} == {
            "analysis", "refinement",
        }
    finally:
        release_backing.set()
        qtbot.waitUntil(lambda: backing.worker is None)
        if dialog is not None:
            qtbot.waitUntil(
                lambda: dialog.worker is None and dialog.refinement_worker is None
            )
    assert whisper_started.is_set()
    assert {job.state for job in managed_jobs.tasks()} == {"succeeded"}


def test_workspace_autostart_download_consent_is_nonmodal_and_does_not_delay_refinement(
    qtbot, tmp_path, monkeypatch, managed_jobs,
):
    missing = tmp_path / "missing-model"
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(
            cli_path=missing, model_path=lambda _key: missing,
            model_download_bytes=lambda _key: 74 * 1024**2,
        ),
    )
    calls = []
    started = Event()
    release = Event()

    def analyze(*_args, use_whisper, **_kwargs):
        calls.append(use_whisper)
        if not use_whisper:
            started.set()
            assert release.wait(5)
        return AnalysisResult(
            [AnalysisSuggestion(1, 2, "Local result", "Whisper")],
            1, 1, -30, "tiny", "en", detect_hardware(),
            refined_captions=None if use_whisper else [
                SourceCaption(1, 2, "Refined result", "Refined YouTube"),
            ],
        )

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: pytest.fail("Modal question"))
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Evidence", "YouTube")],
        auto_start=True, job_manager=managed_jobs, project_id="project-a",
    )
    qtbot.addWidget(dialog)
    dialog.show()
    try:
        qtbot.waitUntil(started.is_set)
        box = next(box for box in dialog.findChildren(QMessageBox) if box.isVisible())
        assert not box.isModal()
        assert box.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert calls == [False]
        dialog.close()
        box.button(QMessageBox.StandardButton.Yes).click()
        qtbot.waitUntil(lambda: True in calls)
        qtbot.waitUntil(lambda: dialog.worker is None)
        assert dialog.refinement_worker is not None
    finally:
        release.set()
        qtbot.waitUntil(
            lambda: dialog.worker is None and dialog.refinement_worker is None
        )


def test_workspace_explicit_cancel_interrupts_both_jobs_without_losing_drafts(
    qtbot, tmp_path, monkeypatch, installed_whisper, managed_jobs,
):
    started = {False: Event(), True: Event()}

    def analyze(*_args, source_captions, cancelled, **_kwargs):
        started[source_captions is not None].set()
        while not cancelled():
            Event().wait(0.01)
        raise AnalysisCancelled("Canceled")

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    review = AnalysisReview(
        local_rows=[AnalysisDraftRow("1", "2", "Local edit", "Whisper")],
        refined_rows=[AnalysisDraftRow("1", "2", "Refined edit", "Refined YouTube")],
        selected_source="local",
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Evidence", "YouTube")], review=review,
        job_manager=managed_jobs, project_id="project-a",
    )
    qtbot.addWidget(dialog)
    dialog._start_worker(use_whisper=True)
    dialog._start_worker(use_whisper=False, refine=True)
    try:
        qtbot.waitUntil(lambda: all(event.is_set() for event in started.values()))
        dialog.cancel_button.click()
        qtbot.waitUntil(
            lambda: dialog.worker is None and dialog.refinement_worker is None
        )
        assert {record.state for record in managed_jobs.tasks()} == {"cancelled"}
        assert dialog.review_state() == review
        assert dialog.local_add_button.isEnabled()
    finally:
        dialog.cancel_scan()


def test_workspace_analysis_failure_message_is_nonmodal(qtbot, tmp_path, monkeypatch, managed_jobs):
    monkeypatch.setattr(QMessageBox, "critical", lambda *_a: pytest.fail("Modal failure"))
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        job_manager=managed_jobs, project_id="project-a",
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._failed("Failed locally")
    box = next(box for box in dialog.findChildren(QMessageBox) if box.isVisible())
    assert not box.isModal()
    assert box.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert "Failed locally" in box.text()
    assert dialog.scan_button.isEnabled()
    box.close()


@pytest.mark.parametrize("refine", [False, True], ids=["whisper", "refinement"])
def test_workspace_late_result_preserves_inflight_draft_edits_until_explicit_replacement(
    qtbot, tmp_path, monkeypatch, installed_whisper, managed_jobs, refine,
):
    started, release = Event(), Event()

    def analyze(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return AnalysisResult(
            [AnalysisSuggestion(3, 4, "New result", "Whisper")],
            1, 1, -30, "tiny", "en", detect_hardware(),
            refined_captions=[SourceCaption(3, 4, "New result", "Refined YouTube")]
            if refine else None,
        )

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    review = AnalysisReview(
        local_rows=[AnalysisDraftRow("1", "2", "Old local", "Whisper")],
        refined_rows=[AnalysisDraftRow("1", "2", "Old refined", "Refined YouTube")],
        selected_source="refined" if refine else "local",
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Evidence", "YouTube")],
        review=review, job_manager=managed_jobs, project_id="project-a",
    )
    qtbot.addWidget(dialog)
    dialog.show()
    table = dialog.refined_table if refine else dialog.local_table
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    try:
        dialog._start_worker(use_whisper=not refine, refine=refine)
        qtbot.waitUntil(started.is_set)
        assert table.isEnabled()
        table.item(0, 1).setText("1.250")
        table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        table.editItem(table.item(0, 3))
        editor = table.findChild(QLineEdit)
        assert editor is not None
        editor.selectAll()
        qtbot.keyClicks(editor, "Edited while running")
    finally:
        release.set()
        qtbot.waitUntil(
            lambda: dialog.worker is None and dialog.refinement_worker is None
        )
    assert table.item(0, 3).text() == "Edited while running"
    assert table.item(0, 1).text() == "1.250"
    assert table.item(0, 0).checkState() == Qt.CheckState.Unchecked
    button = dialog.apply_refined_result_button if refine else dialog.apply_local_result_button
    assert button.isVisible()
    assert not accepted
    button.click()
    assert table.item(0, 3).text() == "New result"
    assert not button.isVisible()
    assert not accepted
    dialog.add_button.click()
    assert [suggestion.caption for suggestion in accepted] == ["New result"]


def complete_refinement(dialog):
    dialog._completed(AnalysisResult(
        [], 1, 0, -30, None, None, detect_hardware(),
        refined_captions=[
            SourceCaption(cue.start, cue.end, cue.text, "Refined YouTube")
            for cue in dialog.source_captions
        ],
    ))


@pytest.fixture
def installed_whisper(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.touch()
    monkeypatch.setattr(
        analysis_dialog, "WhisperManager",
        lambda _root: SimpleNamespace(
            cli_path=runtime, model_path=lambda _key: runtime,
            model_download_bytes=lambda _key: 141 * 1024**2,
        ),
    )
    return runtime


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_transcript_divider_has_a_thin_gap_and_remains_draggable(
    qtbot, tmp_path: Path, stylesheet: str,
) -> None:
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube creator (en)")],
    )
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(stylesheet)
    dialog.show()
    splitter = dialog.refined_panel.parentWidget()
    assert isinstance(splitter, QSplitter)
    qtbot.waitUntil(lambda: splitter.isVisible())

    for width in (1300, 1600):
        dialog.resize(width, 900)
        left = dialog.refined_panel.geometry()
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
        dialog.refined_panel.x() + dialog.refined_panel.width()
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


def test_unprocessed_captions_are_never_shown_or_selectable(qtbot, tmp_path, monkeypatch) -> None:
    starts = []

    def start(dialog):
        starts.append(dialog.table.rowCount())

    monkeypatch.setattr(AnalysisDialog, "start_refinement", start)
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "YouTube text", "YouTube creator (en)")],
        caption_language="en-US", auto_start=True,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.table.rowCount() == 0
    assert not dialog.add_button.isEnabled()
    assert not dialog.preview_button.isEnabled()
    assert not dialog.refined_radio.isEnabled()
    assert not dialog.findChildren(QTabWidget)
    assert len(dialog.findChildren(QTableWidget)) == 2
    assert dialog.checked_suggestions() == []
    assert dialog.language_combo.currentData() == "en"
    qtbot.waitUntil(lambda: bool(starts))
    assert starts == [0]
    complete_refinement(dialog)
    assert dialog.table.item(0, 3).text() == "YouTube text"
    assert dialog.add_button.isEnabled()


def test_whisper_completion_does_not_replace_edits_or_checks(qtbot, tmp_path) -> None:
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube automatic (en)")],
    )
    qtbot.addWidget(dialog)
    complete_refinement(dialog)
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
    dialog.refined_radio.setChecked(True)
    assert dialog.table.item(0, 3).text() == "My edit"
    assert dialog.checked_suggestions() == []


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
@pytest.mark.parametrize("source", ["refined", "local"])
@pytest.mark.parametrize("action", ["click", "enter"])
def test_each_transcript_has_a_direct_use_button(
    qtbot, tmp_path, stylesheet, source, action,
):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[
            SourceCaption(1, 2, "YouTube line", "YouTube"),
            SourceCaption(3, 4, "Another YouTube line", "YouTube"),
        ],
    )
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(stylesheet)
    dialog.show()
    assert dialog.refined_add_button.isVisible()
    assert dialog.local_add_button.isVisible()
    assert not dialog.refined_add_button.isEnabled()
    assert not dialog.local_add_button.isEnabled()
    complete_refinement(dialog)
    assert dialog.refined_add_button.isEnabled()
    assert not dialog.local_add_button.isEnabled()
    dialog._completed(AnalysisResult(
        [
            AnalysisSuggestion(0.5, 2.5, "Whisper line", "Whisper", 0.8),
            AnalysisSuggestion(3.5, 5, "Another Whisper line", "Whisper", 0.7),
        ],
        2, 2, -30, "tiny", "en", detect_hardware(),
    ))
    assert dialog.selected_source == "refined"
    assert dialog.refined_add_button.text() == "Use Refined YouTube Transcript"
    assert dialog.local_add_button.text() == "Use Whisper Transcript"
    for panel, table, button in (
        (dialog.refined_panel, dialog.refined_table, dialog.refined_add_button),
        (dialog.local_panel, dialog.local_table, dialog.local_add_button),
    ):
        assert button.isVisible()
        assert button.isEnabled()
        assert button.objectName() == "primary"
        assert not button.autoDefault()
        assert panel.contentsRect().contains(button.geometry())
        assert button.y() >= table.y() + table.height()

    radio, table, button, other_radio, other_table, other_button = (
        (
            dialog.refined_radio, dialog.refined_table, dialog.refined_add_button,
            dialog.local_radio, dialog.local_table, dialog.local_add_button,
        ) if source == "refined" else (
            dialog.local_radio, dialog.local_table, dialog.local_add_button,
            dialog.refined_radio, dialog.refined_table, dialog.refined_add_button,
        )
    )
    other_radio.setChecked(True)
    other_rows = dialog._draft_rows(other_table)
    for row in range(table.rowCount()):
        table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not button.isEnabled()
    assert other_button.isEnabled()
    table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert button.isEnabled()
    assert dialog.selected_source != source

    table.editItem(table.item(0, 3))
    editor = table.findChild(QLineEdit)
    assert editor is not None
    editor.selectAll()
    qtbot.keyClicks(editor, "Edited line")
    accepted = []
    saved = []
    dialog.suggestions_accepted.connect(accepted.extend)
    dialog.review_changed.connect(saved.append)
    if action == "click":
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    else:
        radio.click()
        assert button.isDefault()
        assert not other_button.isDefault()
        qtbot.keyClick(dialog, Qt.Key.Key_Return)

    assert accepted == [
        AnalysisSuggestion(1, 2, "Edited line", "Refined YouTube")
        if source == "refined" else
        AnalysisSuggestion(0.5, 2.5, "Edited line", "Whisper", 0.8)
    ]
    assert saved[-1].selected_source == source
    assert dialog._draft_rows(other_table) == other_rows
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.worker is None


@pytest.mark.parametrize("source", ["youtube", "refined"], ids=["legacy-selection", "refined"])
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
    assert dialog.add_button.text() == "Use Refined YouTube Transcript"
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
    assert dialog.selected_source == "refined"
    assert dialog.add_button.text() == "Use Refined YouTube Transcript"
    assert dialog.add_button.isEnabled()
    assert dialog.refine_button.isEnabled()
    assert dialog.source_captions == captions


@pytest.mark.parametrize("outcome", ["fail", "cancel", "close"])
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
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == review
    assert len(calls) == 1
    assert not whisper_starts
    if outcome == "fail":
        assert "failed" in dialog.refined_status.text()
    elif outcome == "cancel":
        assert "canceled" in dialog.refined_status.text()
        assert dialog.refine_button.isEnabled()
    else:
        assert dialog.result() == QDialog.DialogCode.Rejected
    assert not accepted
    assert dialog.refined_table.rowCount() == 0
    assert not dialog.add_button.isEnabled()
    assert dialog.source_captions[0].text == "Original"


def test_closing_before_automatic_refinement_keeps_evidence_but_no_usable_rows(
    qtbot, tmp_path, monkeypatch,
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
    assert not dialog.add_button.isEnabled()
    dialog.reject()
    qtbot.wait(10)
    assert not starts
    assert dialog.checked_suggestions() == []
    assert not accepted
    assert dialog.source_captions[0].text == "Original"


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
def test_restoring_drafts_only_processes_missing_refinement(
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
    assert starts == ([] if refined_rows else ["refined"])
    saved = dialog.review_state()
    assert saved.selected_source == "refined"
    assert saved.youtube_rows == review.youtube_rows
    assert saved.refined_rows == refined_rows
    assert saved.pause_threshold == 0.6
    assert not dialog.findChildren(QTabWidget)


def test_whisper_failure_keeps_edited_caption_rows(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube creator (en)")],
    )
    qtbot.addWidget(dialog)
    complete_refinement(dialog)
    dialog.table.item(0, 3).setText("Edited")
    dialog._failed("Model unavailable")
    assert dialog.checked_suggestions()[0].caption == "Edited"
    assert "failed" in dialog.local_status.text()
    assert dialog.add_button.isEnabled()


@pytest.mark.parametrize("source", ["local", "refined"])
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
    dialog.local_radio.setChecked(source == "refined")
    expected = [
        AnalysisSuggestion(1.1, 2.9, "Refined line", "Refined YouTube")
        if source == "refined" else
        AnalysisSuggestion(0.5, 3.5, "One longer Whisper line", "Whisper", 0.8)
    ]
    assert dialog.selected_source != source
    {
        "refined": dialog.refined_add_button,
        "local": dialog.local_add_button,
    }[source].click()
    assert window.save_project()
    path = window.project_path
    qtbot.waitUntil(lambda: not window.dirty and path.is_file())
    saved = ProjectStore.load(path)
    assert [(s.start, s.end, s.caption) for s in saved.segments] == [
        (s.start, s.end, s.caption) for s in expected
    ]
    assert saved.analysis_review.selected_source == source
    assert saved.analysis_review.youtube_rows == []
    assert saved.source_captions == captions
    assert len(saved.analysis_review.local_rows) == 1
    assert len(saved.analysis_review.refined_rows) == 1
    window.dirty = False
    window.close()


def test_closing_review_preserves_all_edited_drafts_and_checkboxes(
    qtbot, tmp_path,
):
    captions = [SourceCaption(1, 2, "Original", "YouTube creator (en)")]
    legacy_rows = [
        AnalysisDraftRow("unfinished time", "2", "Legacy YouTube edit", "YouTube", checked=False),
    ]
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=captions,
        review=AnalysisReview(
            youtube_rows=legacy_rows,
            refined_rows=[AnalysisDraftRow("1", "2", "Previous draft", "Refined YouTube")],
            selected_source="refined",
        ),
    )
    qtbot.addWidget(dialog)
    dialog._completed(AnalysisResult([
        AnalysisSuggestion(0.5, 3, "Whisper draft", "Whisper", 0.876),
    ], 1, 1, -30, "base", "en", detect_hardware()))
    dialog._completed(AnalysisResult(
        [], 1, 0, -30, None, None, detect_hardware(),
        refined_captions=[SourceCaption(1.1, 1.9, "Refined draft", "Refined YouTube")],
    ))
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
    assert restored.review_state().youtube_rows == legacy_rows
    assert restored.pause_spin.value() == 0.65
    assert restored.checked_suggestions() == []
    restored.local_radio.setChecked(True)
    assert restored.checked_suggestions() == [
        AnalysisSuggestion(0.5, 3, "Edited Whisper", "Whisper", 0.876)
    ]
    assert loaded.segments == []


@pytest.mark.parametrize("finish", ["close", "use"])
@pytest.mark.parametrize("source", ["local", "refined"])
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
    else:
        dialog._populate([AnalysisSuggestion(1, 2, "Whisper", "Whisper")])
        dialog.local_radio.setChecked(True)
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
    rows = changes[-1].local_rows if source == "local" else changes[-1].refined_rows
    assert rows[0].caption == "New dialogue"


def test_no_youtube_captions_still_offers_whisper_source(qtbot, tmp_path):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
    )
    qtbot.addWidget(dialog)
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
        refined_rows=[AnalysisDraftRow("1.1", "1.9", "Refined edit", "Refined YouTube")],
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True, review=review,
    )
    qtbot.addWidget(dialog)
    dialog.start_scan()
    assert dialog.review_state() == review
    assert dialog.local_table.isEnabled()
    assert dialog.add_button.isEnabled()
    assert dialog.local_add_button.isEnabled()
    assert dialog.refined_add_button.isEnabled()
    dialog.cancel_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == review
    assert dialog.local_table.isEnabled()
    assert dialog.add_button.isEnabled()
    assert dialog.local_add_button.isEnabled()
    assert dialog.refined_add_button.isEnabled()


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
        qtbot.waitUntil(lambda: window.project.analysis_review is None)
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
    monkeypatch.setattr(ProjectEditor, "generate_backing_track", lambda _self: False)
    monkeypatch.setattr(
        ProjectEditor, "open_analysis_dialog", lambda _self, **kwargs: scans.append(kwargs),
    )
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
    assert dialog.refined_panel.title() == "Refined YouTube"
    assert dialog.local_panel.title() == "Whisper Transcript"
    assert not dialog.preview_button.isEnabled()
    complete_refinement(dialog)
    dialog.refined_table.selectRow(0)
    assert dialog.preview_button.isEnabled()
    assert dialog.preview_button.text() == "Play Selected Refined YouTube Line"
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
    assert dialog.refined_table.isEnabled()
    assert dialog.local_table.isEnabled()
    assert dialog.refined_add_button.isEnabled()
    assert dialog.local_add_button.isEnabled()
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
    assert dialog.selected_source == "refined"
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
        selected_source="local",
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
        dialog.refined_radio.setChecked(True)
        dialog.local_add_button.click()
        assert not dialog.refined_add_button.isEnabled()
        assert not dialog.local_add_button.isEnabled()
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
        assert [suggestion.caption for suggestion in accepted] == ["Whisper edit"]
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


def test_processing_transcript_cannot_be_imported_or_previewed(qtbot, tmp_path, monkeypatch):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        review=AnalysisReview(
            refined_rows=[AnalysisDraftRow("1", "2", "Saved draft", "Refined YouTube")],
            selected_source="refined",
        ),
    )
    qtbot.addWidget(dialog)
    dialog.refined_table.setEnabled(False)
    messages = []
    previews = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[-1]))
    dialog.preview_requested.connect(lambda *args: previews.append(args))
    with pytest.raises(ValueError, match="finish processing"):
        dialog.checked_suggestions()
    dialog.preview_row(0)
    assert not previews
    assert len(messages) == 1
    assert dialog.review_state().refined_rows[0].caption == "Saved draft"


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
    assert dialog.refined_add_button.isVisible()
    assert dialog.local_add_button.isVisible()
    assert dialog.refined_panel.contentsRect().contains(dialog.refined_add_button.geometry())
    assert dialog.local_panel.contentsRect().contains(dialog.local_add_button.geometry())


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
        local_rows=[AnalysisDraftRow("1.1", "2.9", "Whisper edit", "Whisper")],
        refined_rows=[
            AnalysisDraftRow("1.2", "2.8", "Refined edit", "Refined YouTube"),
        ],
        selected_source="local" if source == "refined" else "refined",
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
        ["Whisper edit" if source == "refined" else "Refined edit"] if finish == "use" else []
    )


@pytest.mark.parametrize("selected_source", ["local", "refined"])
@pytest.mark.parametrize("outcome", [
    "memory", "runtime", "cancel", "empty", "invalid",
    "decline-replacement", "decline-download", "setup",
])
def test_model_switch_recovery_can_use_current_whisper_without_rerunning(
    qtbot, tmp_path, monkeypatch, installed_whisper, selected_source, outcome,
):
    import time

    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
        review=AnalysisReview(
            youtube_rows=[AnalysisDraftRow("1", "3", "YouTube edit", "YouTube")],
            refined_rows=[AnalysisDraftRow("1.1", "2.9", "Refined edit", "Refined YouTube")],
            selected_source="refined",
        ),
    )
    qtbot.addWidget(dialog)
    tiny_result = AnalysisResult([
        AnalysisSuggestion(0.5, 3, "Tiny draft", "Whisper", 0.8),
        AnalysisSuggestion(4, 5, "Excluded line", "Whisper"),
    ], 2, 2, -30, "tiny", "en", detect_hardware())
    dialog._completed(tiny_result)
    dialog.local_table.item(0, 3).setText("My current Whisper edit")
    dialog.local_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    if selected_source == "local":
        dialog.local_radio.setChecked(True)
    before = dialog.review_state()
    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("base"))
    dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("ja"))
    calls = []
    errors = []
    prompts = []

    def analyze(*_args, cancelled, **kwargs):
        calls.append(kwargs)
        if outcome == "cancel":
            while not cancelled():
                time.sleep(0.01)
            raise AnalysisCancelled("Canceled")
        if outcome == "empty":
            return AnalysisResult(
                [AnalysisSuggestion(1, 3, "", "Untranscribed activity")],
                1, 0, -30, "base", "ja", detect_hardware(),
            )
        if outcome == "invalid":
            return object()
        raise AnalysisError(
            "This video/model combination exceeds the conservative local-memory budget."
            if outcome == "memory" else "Runtime failed to load"
        )

    def question(_parent, title, *_args):
        prompts.append(title)
        if (
            outcome == "decline-replacement"
            or outcome == "decline-download" and title.startswith("Download")
        ):
            return QMessageBox.StandardButton.Cancel
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: errors.append(_args[-1]))
    if outcome == "decline-download":
        monkeypatch.setattr(
            analysis_dialog, "WhisperManager",
            lambda _root: SimpleNamespace(
                cli_path=installed_whisper, model_path=lambda _key: tmp_path / "missing-base",
                model_download_bytes=lambda _key: 141 * 1024**2,
            ),
        )
    elif outcome == "setup":
        def unavailable(_root):
            raise AnalysisError("Setup manifest unavailable")

        monkeypatch.setattr(analysis_dialog, "WhisperManager", unavailable)

    dialog.start_scan()
    if outcome == "cancel":
        qtbot.waitUntil(lambda: bool(calls))
        dialog.cancel_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == before
    assert dialog.analysis_result is tiny_result
    assert dialog.local_table.isEnabled()
    assert dialog.scan_button.isEnabled()
    assert dialog.scan_button.text() == "Rerun Whisper..."
    assert dialog.model_combo.currentData() == "base"
    assert dialog.language_combo.currentData() == "ja"
    assert "tiny; detected en" in dialog.local_draft_label.text()
    assert "base" not in dialog.local_draft_label.text()
    assert "Use Whisper Transcript" in dialog.progress_label.text()
    assert bool(errors) == (outcome in {"memory", "runtime", "invalid", "setup"})
    if errors:
        assert "Use Whisper Transcript" in errors[0]
    assert len(calls) == (0 if outcome in {
        "decline-replacement", "decline-download", "setup",
    } else 1)
    if calls:
        assert calls[0]["model_key"] == "base"
        assert calls[0]["language"] == "ja"
        assert calls[0]["use_whisper"]
    assert len(prompts) == (2 if outcome == "decline-download" else 1)

    # Clicking the already-selected row must choose its transcript, not just highlight it.
    dialog.show()
    dialog.local_table.selectRow(0)
    qtbot.mouseClick(
        dialog.local_table.viewport(), Qt.MouseButton.LeftButton,
        pos=dialog.local_table.visualItemRect(dialog.local_table.item(0, 3)).center(),
    )
    assert dialog.selected_source == "local"
    assert dialog.add_button.text() == "Use Whisper Transcript"
    assert dialog.add_button.isEnabled()
    accepted = []
    dialog.suggestions_accepted.connect(accepted.extend)
    dialog.add_button.click()
    assert accepted == [AnalysisSuggestion(0.5, 3, "My current Whisper edit", "Whisper", 0.8)]
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(calls) <= 1


def test_successful_model_switch_updates_only_local_draft_provenance(
    qtbot, tmp_path, monkeypatch, installed_whisper,
):
    review = AnalysisReview(
        youtube_rows=[AnalysisDraftRow("1", "2", "YouTube edit", "YouTube")],
        local_rows=[AnalysisDraftRow("1", "2", "Tiny edit", "Whisper", checked=False)],
        refined_rows=[AnalysisDraftRow("1.1", "1.9", "Refined edit", "Refined YouTube")],
        selected_source="refined", local_model_name="tiny", local_detected_language="en",
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True, review=review,
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        analysis_dialog, "analyze_video",
        lambda *_args, **_kwargs: AnalysisResult(
            [AnalysisSuggestion(3, 5, "New base draft", "Whisper")],
            1, 1, -30, "base", "ja", detect_hardware(),
        ),
    )
    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("base"))
    dialog.start_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    saved = dialog.review_state()
    assert saved.selected_source == review.selected_source
    assert saved.youtube_rows == review.youtube_rows
    assert saved.refined_rows == review.refined_rows
    assert saved.local_rows == [AnalysisDraftRow("3.000", "5.000", "New base draft", "Whisper")]
    assert saved.local_model_name == "base"
    assert saved.local_detected_language == "ja"
    path = tmp_path / "review.cvpack.json"
    ProjectStore.save(PackProject(analysis_review=saved), path)
    restored = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True, review=ProjectStore.load(path).analysis_review,
    )
    qtbot.addWidget(restored)
    assert "base; detected ja" in restored.local_draft_label.text()
    assert restored.review_state() == saved
    restored.local_radio.click()
    assert restored.checked_suggestions() == [AnalysisSuggestion(3, 5, "New base draft", "Whisper")]
    assert restored.add_button.isEnabled()


def test_legacy_whisper_draft_does_not_claim_next_scan_model(qtbot, tmp_path):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
        review=AnalysisReview(local_rows=[AnalysisDraftRow("1", "2", "Saved", "Whisper")]),
    )
    qtbot.addWidget(dialog)
    for model in ("base", "tiny"):
        dialog.model_combo.setCurrentIndex(dialog.model_combo.findData(model))
        assert "model not recorded" in dialog.local_draft_label.text()
        assert model not in dialog.local_draft_label.text()
        assert dialog.add_button.isEnabled()


@pytest.mark.parametrize("outcome", ["fail", "empty"])
@pytest.mark.parametrize("with_captions", [False, True])
def test_first_whisper_attempt_without_draft_can_retry(
    qtbot, tmp_path, monkeypatch, installed_whisper, outcome, with_captions,
):
    calls = []

    def analyze(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            if outcome == "fail":
                raise AnalysisError("Insufficient memory")
            return AnalysisResult([], 0, 0, -30, "base", "en", detect_hardware())
        return AnalysisResult(
            [AnalysisSuggestion(1, 2, "Retry result", "Whisper")],
            1, 1, -30, "tiny", "en", detect_hardware(),
        )

    monkeypatch.setattr(analysis_dialog, "analyze_video", analyze)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube")] if with_captions else [],
    )
    qtbot.addWidget(dialog)
    dialog.start_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert not dialog.local_radio.isEnabled()
    assert not dialog.add_button.isEnabled()
    assert dialog.scan_button.text() == "Run Whisper"
    assert "No Whisper draft is available" in dialog.progress_label.text()
    dialog.model_combo.setCurrentIndex(dialog.model_combo.findData("tiny"))
    dialog.start_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert len(calls) == 2
    assert dialog.local_radio.isEnabled()
    dialog.local_radio.click()
    assert dialog.add_button.isEnabled()
    assert dialog.checked_suggestions() == [AnalysisSuggestion(1, 2, "Retry result", "Whisper")]


@pytest.mark.parametrize("has_previous", [False, True])
def test_empty_refinement_keeps_drafts_and_does_not_chain_whisper(
    qtbot, tmp_path, monkeypatch, has_previous,
):
    review = AnalysisReview(
        youtube_rows=[AnalysisDraftRow("1", "2", "Original", "YouTube")],
        refined_rows=(
            [AnalysisDraftRow("1.1", "1.9", "Refined edit", "Refined YouTube")]
            if has_previous else []
        ),
        selected_source="refined",
    )
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        source_captions=[SourceCaption(1, 2, "Original", "YouTube")], review=review,
    )
    qtbot.addWidget(dialog)
    calls = []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        analysis_dialog, "analyze_video",
        lambda *_args, **_kwargs: calls.append(1) or AnalysisResult(
            [], 0, 0, -30, None, None, detect_hardware(), refined_captions=[],
        ),
    )
    dialog._start_automatic_refinement()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert calls == [1]
    assert dialog.review_state() == review
    assert "No new rows" in dialog.refined_status.text()
    assert dialog.add_button.isEnabled() == has_previous
    assert dialog.refined_radio.isEnabled() == has_previous


@pytest.mark.parametrize("source", ["refined", "local"])
def test_clicking_draft_selects_its_source_and_checked_rows_control_import(qtbot, tmp_path, source):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
        review=AnalysisReview(
            youtube_rows=[AnalysisDraftRow("1", "2", "YouTube", "YouTube")],
            refined_rows=[AnalysisDraftRow("2", "3", "Refined", "Refined YouTube")],
            local_rows=[AnalysisDraftRow("3", "4", "Whisper", "Whisper")],
            selected_source="local" if source != "local" else "refined",
        ),
    )
    qtbot.addWidget(dialog)
    table = {"refined": dialog.refined_table, "local": dialog.local_table}[source]
    dialog.show()
    table.selectRow(0)
    assert dialog.selected_source != source
    qtbot.mouseClick(
        table.viewport(), Qt.MouseButton.LeftButton,
        pos=table.visualItemRect(table.item(0, 3)).center(),
    )
    assert dialog.selected_source == source
    assert dialog.add_button.isEnabled()
    table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not dialog.add_button.isEnabled()
    assert dialog.preview_button.isEnabled()
    table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert dialog.add_button.isEnabled()


@pytest.mark.parametrize("start,end", [("unfinished", "2"), ("nan", "2"), ("1", "inf"), ("2", "1")])
def test_invalid_checked_draft_stays_editable_until_fixed(
    qtbot, tmp_path, monkeypatch, start, end,
):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
        review=AnalysisReview(local_rows=[AnalysisDraftRow(start, end, "Edit", "Whisper")]),
    )
    qtbot.addWidget(dialog)
    warnings = []
    accepted = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))
    dialog.suggestions_accepted.connect(accepted.extend)
    dialog.add_button.click()
    assert warnings
    assert not accepted
    assert not dialog._close_after_cancel
    dialog.local_table.item(0, 1).setText("1")
    dialog.local_table.item(0, 2).setText("2")
    dialog.add_button.click()
    assert accepted == [AnalysisSuggestion(1, 2, "Edit", "Whisper")]


@pytest.mark.parametrize("unavailable", ["youtube", "refined", "local"])
def test_restored_unavailable_source_selects_an_existing_draft(qtbot, tmp_path, unavailable):
    rows = [AnalysisDraftRow("1", "2", "Available", "Whisper")]
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        youtube_import=True,
        review=AnalysisReview(
            youtube_rows=[] if unavailable == "youtube" else rows,
            refined_rows=[] if unavailable == "refined" else rows,
            local_rows=[] if unavailable == "local" else rows,
            selected_source=unavailable,
        ),
    )
    qtbot.addWidget(dialog)
    assert dialog.selected_source != unavailable
    assert dialog.table.rowCount() == 1
    assert dialog.add_button.isEnabled()
    if unavailable == "youtube":
        assert dialog.selected_source == "refined"
        assert not dialog.findChildren(QTabWidget)
    else:
        radio = {"refined": dialog.refined_radio, "local": dialog.local_radio}[unavailable]
        assert not radio.isEnabled()


def test_switching_to_activity_scan_keeps_whisper_draft_until_success(
    qtbot, tmp_path, monkeypatch,
):
    dialog = AnalysisDialog(
        UnusedMedia(), tmp_path / "video.mp4", 10, tmp_path / "analysis", 0,
        review=AnalysisReview(
            local_rows=[AnalysisDraftRow("1", "2", "Whisper edit", "Whisper")],
            local_model_name="tiny", local_detected_language="en",
        ),
    )
    qtbot.addWidget(dialog)
    before = dialog.review_state()
    dialog.whisper_check.setChecked(False)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: None)

    def fail(*_args, **_kwargs):
        raise AnalysisError("Audio unavailable")

    monkeypatch.setattr(analysis_dialog, "analyze_video", fail)
    dialog.start_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.review_state() == before
    assert dialog.add_button.text() == "Use Whisper Transcript"
    assert not dialog.model_combo.isEnabled()
    assert not dialog.language_combo.isEnabled()
    monkeypatch.setattr(
        analysis_dialog, "analyze_video",
        lambda *_args, **_kwargs: AnalysisResult(
            [AnalysisSuggestion(3, 4, "", "Audio activity")],
            1, 0, -30, None, None, detect_hardware(),
        ),
    )
    dialog.start_scan()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.add_button.text() == "Use Detected Ranges"
    assert dialog.review_state().local_model_name == ""
    assert dialog.review_state().local_detected_language == ""
    assert dialog.checked_suggestions() == [AnalysisSuggestion(3, 4, "", "Audio activity")]
