from __future__ import annotations

import threading
from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QThread, QTimer
from PySide6.QtWidgets import QDialog

from choicer_voicer_pack_creator.automation import PackAutomation, ProjectPatch, SegmentPatch
from choicer_voicer_pack_creator.mcp_gui import EditorBridge, EditorProjectAccess
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui.main_window import MainWindow


def in_worker(qtbot, function):
    result = Future()

    def run():
        try:
            result.set_result(function())
        except Exception as error:
            result.set_exception(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    qtbot.waitUntil(result.done, timeout=10000)
    worker.join(timeout=1)
    return result.result()


@pytest.fixture
def live_editor(qtbot, tmp_path):
    window = MainWindow(
        None,
        settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        recovery_store=RecoveryStore(tmp_path / "recovery.json"),
        analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(
        window, before_close_func=lambda widget: setattr(widget, "_automation_disconnected", True)
    )
    window.show()
    segment = Segment(1, 2, "Original", ["Actor"])
    window._set_project(
        PackProject(title="Live", authors=["Tester"], video_duration=10, segments=[segment]),
        None, mark_dirty=False,
    )
    window.select_segment(segment.id)
    bridge = EditorBridge(window)
    automation = PackAutomation(EditorProjectAccess(bridge), tmp_path)
    yield window, bridge, automation
    window._automation_active = False
    window.dirty = False
    window.close()


def test_live_tools_edit_real_widgets_and_preserve_human_edits(qtbot, live_editor, tmp_path):
    window, bridge, automation = live_editor
    gui_thread = in_worker(qtbot, lambda: bridge.call(QThread.currentThread))
    assert gui_thread == window.thread()
    before = in_worker(qtbot, automation.get_project)
    segment_id = before["segments"][0]["id"]
    edited = in_worker(qtbot, lambda: automation.edit_segments([
        SegmentPatch(id=segment_id, caption="LLM correction", start=1.2, end=2.2),
        SegmentPatch(start=3, end=4, caption="New line", characters=["Second actor"]),
    ], [], before["revision"]))
    assert window.segment_table.rowCount() == 2
    assert window.caption_edit.toPlainText() == "LLM correction"
    assert window.dirty
    assert window.recovery_store.load().project.segments[0].caption == "LLM correction"
    window.title_edit.setText("Human title")
    with pytest.raises(ValueError, match="Project changed"):
        in_worker(qtbot, lambda: automation.update_project(
            ProjectPatch(title="Stale LLM title"), edited["revision"]
        ))
    current = in_worker(qtbot, automation.get_project)
    assert current["project"]["title"] == "Human title"
    saved = in_worker(qtbot, lambda: automation.save_project(
        current["revision"], str(tmp_path / "live.cvpack.json")
    ))
    assert not window.dirty
    assert window.project_path == Path(saved["project_path"])
    assert window.recovery_store.load() is None
    assert ProjectStore.load(window.project_path).title == "Human title"


def test_live_operation_busy_state_and_modal_rejection(qtbot, live_editor):
    window, bridge, _automation = live_editor
    in_worker(qtbot, lambda: bridge.begin("Test operation"))
    assert window._automation_active
    assert not window.action_export.isEnabled()
    assert not window.editor_splitter.isEnabled()
    assert not window.close()
    in_worker(qtbot, bridge.end)
    assert window.action_export.isEnabled()
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    with pytest.raises(ValueError, match="modal dialog"):
        in_worker(qtbot, lambda: bridge.begin("Blocked"))
    dialog.close()
    assert not window._automation_active


def test_nonmodal_backing_workflow_blocks_mcp_mutations(qtbot, live_editor):
    window, bridge, _automation = live_editor
    dialog = QDialog(window)
    window._backing_dialog = dialog
    try:
        with pytest.raises(ValueError, match="backing-track"):
            in_worker(qtbot, lambda: bridge.begin("Edit project"))
        assert not window._automation_active
    finally:
        window._backing_dialog = None
        dialog.close()


def test_live_show_and_disconnect_preserve_unsaved_recovery(qtbot, live_editor):
    window, bridge, automation = live_editor
    state = in_worker(qtbot, automation.get_project)
    in_worker(qtbot, lambda: automation.access.show(state["segments"][0]["id"], 1.5))
    assert window.selected_segment_id == state["segments"][0]["id"]
    window._set_dirty(True)
    bridge.disconnected.emit()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert window.recovery_store.load() is not None


def test_live_inspection_preserves_structured_names_and_padding(qtbot, live_editor):
    window, _bridge, automation = live_editor
    before = in_worker(qtbot, automation.get_project)
    updated = in_worker(qtbot, lambda: automation.update_project(
        ProjectPatch(authors=["Doe, Jane"], head_padding=0.1234), before["revision"]
    ))
    edited = in_worker(qtbot, lambda: automation.edit_segments(
        [SegmentPatch(start=3, end=4, caption="Hello", characters=["Doe, John"])],
        [], updated["revision"],
    ))
    in_worker(qtbot, lambda: automation.access.show(edited["changed_ids"][0], None))
    read = in_worker(qtbot, automation.get_project)
    assert read["revision"] == edited["revision"]
    assert read["project"]["authors"] == ["Doe, Jane"]
    assert read["project"]["head_padding"] == 0.1234
    assert read["segments"][-1]["characters"] == ["Doe, John"]
    window._selected_speakers_changed()
    assert in_worker(qtbot, automation.get_project)["revision"] == edited["revision"]


def test_focusing_unchanged_fields_does_not_dirty_project_on_mcp_call(qtbot, live_editor):
    window, bridge, automation = live_editor
    before = in_worker(qtbot, automation.get_project)
    window.title_edit.setFocus()
    qtbot.waitUntil(window.title_edit.hasFocus)
    in_worker(qtbot, lambda: bridge.begin("Update"))
    try:
        assert not window.dirty
        in_worker(qtbot, lambda: automation.update_project(
            ProjectPatch(title="Updated"), before["revision"]
        ))
    finally:
        in_worker(qtbot, bridge.end)
    assert window.project.title == "Updated"


def test_disconnect_during_recovery_question_does_not_discard_record(qtbot, live_editor):
    window, bridge, _automation = live_editor
    qtbot.wait(1)
    window.recovery_store.save(PackProject(title="Recover me"), None)
    QTimer.singleShot(25, bridge.disconnected.emit)
    window._offer_recovery()
    assert not window.isVisible()
    assert window.recovery_store.load().project.title == "Recover me"
