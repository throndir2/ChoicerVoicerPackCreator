from __future__ import annotations

import threading
from time import perf_counter
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_checks import ProjectChecksResult
from choicer_voicer_pack_creator.ui import project_checks
from choicer_voicer_pack_creator.ui.main_window import MainWindow


@pytest.fixture
def checked_editor(qtbot, tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test media")
    window = MainWindow(
        SimpleNamespace(waveform_peaks=lambda *_args, **_kwargs: []),
        settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    editor = window.active_editor
    editor._set_project(
        PackProject(
            title="Checks", authors=["Author"], video_path=str(video), video_duration=10000,
            auto_speaker_matching=False,
            segments=[Segment(1, 3, "One", ["Alice"]), Segment(2, 4, "Two", ["Bob"])],
        ),
        None, mark_dirty=False,
    )

    def close(_widget):
        for item in window.editors.values():
            item.project_checks.close()
            item.speaker_matching.close_processing()
            item.derived_work.close()
            item.dirty = False
            item._recovery_timer.stop()
        for job in window.job_manager.active_jobs():
            window.job_manager.cancel(job.id)
        qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
        window.close()
        qtbot.waitUntil(lambda: window._close_approved, timeout=10000)

    qtbot.addWidget(window, before_close_func=close)
    window.show()
    qtbot.waitUntil(lambda: not editor.project_checks.pending, timeout=10000)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
    return window, editor


def test_checks_and_overlap_details_never_run_on_gui_thread(
    checked_editor, qtbot, monkeypatch,
):
    _window, editor = checked_editor
    thread = threading.get_ident()
    calls = []
    original = project_checks.check_project

    def check(metadata, segments):
        calls.append(threading.get_ident())
        assert calls[-1] != thread
        return original(metadata, segments)

    monkeypatch.setattr(project_checks, "check_project", check)
    monkeypatch.setattr(
        editor, "_timeline_review_details",
        lambda _warnings: pytest.fail("Overlap details ran on the GUI thread"),
    )
    segment = editor.project.segments[0]
    editor._timeline_range_edit_started(segment.id, 1, 3)
    editor._timeline_range_changed(segment.id, 1.1, 3.1)
    editor._timeline_range_edit_finished(segment.id, 1, 3, 1.1, 3.1)
    assert editor.project_checks.pending
    qtbot.waitUntil(lambda: not editor.project_checks.pending, timeout=10000)
    assert len(calls) == 1
    assert editor.project_checks.result.overlaps[0].seconds == 1.1


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_obsolete_checks_are_ineligible_before_next_submission(
    checked_editor, qtbot, monkeypatch, outcome,
):
    window, editor = checked_editor
    started, release = threading.Event(), threading.Event()
    calls = []
    original = project_checks.check_project

    def check(metadata, segments):
        calls.append(segments)
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
            if outcome == "failure":
                raise ValueError("obsolete validation failure")
            # Deliberately ignore cancellation and return a success-shaped stale result.
            return ProjectChecksResult(("obsolete",), (), (), len(segments), 0)
        return original(metadata, segments)

    monkeypatch.setattr(project_checks, "check_project", check)
    segment = editor.project.segments[0]
    old_result = editor.project_checks.result
    try:
        segment.start = 1.1
        editor._set_dirty(True, segment=segment)
        qtbot.waitUntil(started.is_set, timeout=10000)
        generation = editor.derived_work.generation(project_checks.ProjectChecks.key)
        editor._timeline_range_edit_started(segment.id, 1.1, 3)
        editor._timeline_range_changed(segment.id, 1.2, 3)
        assert editor.derived_work.generation(project_checks.ProjectChecks.key) > generation
        release.set()
        qtbot.waitUntil(
            lambda: not window.job_manager.active_jobs(editor.session.id), timeout=10000,
        )
        assert editor.project_checks.result is old_result
        assert editor.project_checks.pending
        editor._timeline_range_changed(segment.id, 1.1, 3)
        editor._timeline_range_edit_finished(segment.id, 1.1, 3, 1.1, 3)
        qtbot.waitUntil(lambda: not editor.project_checks.pending, timeout=10000)
        assert len(calls) == 2
        assert editor.project_checks.result.errors == ()
        assert editor.project_checks.result.overlaps
    finally:
        release.set()


def test_timing_release_reuses_items_selection_and_scroll(
    checked_editor, qtbot, monkeypatch,
):
    _window, editor = checked_editor
    editor.project.segments = [
        Segment(index * 3, index * 3 + 2, f"Line {index}", ["Alice"]) for index in range(1000)
    ]
    editor._refresh_table()
    qtbot.waitUntil(lambda: not editor.project_checks.pending, timeout=10000)
    segment = editor.project.segments[500]
    editor.select_segment(segment.id)
    editor.timeline.set_zoom(40, segment.start)
    table = editor.segment_table
    table.verticalScrollBar().setValue(450)
    scroll = table.verticalScrollBar().value()
    selected = editor._selected_table_ids()
    items = {
        item.id: tuple(table.item(row, col) for col in range(table.columnCount()))
        for row, item in enumerate(editor.project.segments)
    }
    monkeypatch.setattr(
        editor, "_refresh_table", lambda *_args: pytest.fail("Timing release rebuilt the table"),
    )
    original = segment.start, segment.end
    timeline = editor.timeline
    anchor = timeline._time_to_x(segment.start + 1)
    timeline._prepare_drag("segment-body", segment.id, *original, anchor)
    timeline._activate_drag()
    QApplication.processEvents()
    begin = perf_counter()
    for index in range(20):
        x = timeline._time_to_x(original[0] + 1 + (index + 1) * 0.2)
        timeline._update_drag(x)
        QApplication.processEvents()
    callback_ms = (perf_counter() - begin) * 1000 / 20
    begin = perf_counter()
    qtbot.mouseRelease(
        timeline, Qt.MouseButton.LeftButton,
        pos=QPoint(round(x), round(timeline._segment_rect(segment).center().y())),
    )
    QApplication.processEvents()
    release_ms = (perf_counter() - begin) * 1000
    assert editor.project_checks.pending
    assert editor._selected_table_ids() == selected
    assert table.verticalScrollBar().value() == scroll
    for row, item in enumerate(editor.project.segments):
        assert tuple(table.item(row, col) for col in range(table.columnCount())) == items[item.id]
    qtbot.waitUntil(lambda: not editor.project_checks.pending, timeout=10000)
    print(
        f"\n1000 segments, including Qt layout/paint: "
        f"drag frame {callback_ms:.2f}ms; release frame {release_ms:.2f}ms"
    )


def test_cancelled_checks_stay_pending_until_explicit_retry(
    checked_editor, qtbot, monkeypatch,
):
    window, editor = checked_editor
    started, release = threading.Event(), threading.Event()
    original = project_checks.check_project
    calls = []

    def check(metadata, segments):
        calls.append(segments)
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
            return ProjectChecksResult((), (), (), len(segments), 2)
        return original(metadata, segments)

    monkeypatch.setattr(project_checks, "check_project", check)
    old_result = editor.project_checks.result
    segment = editor.project.segments[0]
    try:
        segment.start = 1.1
        editor._set_dirty(True, segment=segment)
        qtbot.waitUntil(started.is_set, timeout=10000)
        job = next(
            job for job in window.job_manager.active_jobs(editor.session.id)
            if job.kind == "project-checks"
        )
        window.job_manager.cancel(job.id)
        release.set()
        qtbot.waitUntil(
            lambda: not window.job_manager.active_jobs(editor.session.id), timeout=10000,
        )
        assert editor.project_checks.result is old_result
        assert editor.project_checks.pending
        segment.start = 1.2
        editor._set_dirty(True, segment=segment)
        editor.derived_work.wake()
        qtbot.wait(350)
        assert len(calls) == 1
        editor.project_checks.retry()
        qtbot.waitUntil(lambda: not editor.project_checks.pending, timeout=10000)
        assert len(calls) == 2
        assert editor.project_checks.result.overlaps[0].seconds == 1.0
    finally:
        release.set()
