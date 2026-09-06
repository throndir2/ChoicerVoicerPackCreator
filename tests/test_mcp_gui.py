from __future__ import annotations

import shutil
import threading
from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QThread
from PySide6.QtWidgets import QApplication, QDialog

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
    window.show()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
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
    window._automation_disconnected = True
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=10000)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
    window.deleteLater()
    qtbot.wait(1)


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
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert window.active_editor.recovery_store.load().project.segments[0].caption == "LLM correction"
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
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert window.active_editor.recovery_store.load() is None
    assert ProjectStore.load(window.project_path).title == "Human title"
    assert window._recent_project_paths() == [window.project_path]


def test_live_project_open_updates_recent_projects_but_edits_do_not(qtbot, live_editor, tmp_path):
    window, _bridge, automation = live_editor
    path = tmp_path / "opened.cvpack.json"
    ProjectStore.save(PackProject(title="Opened", authors=["Tester"]), path)
    opened = in_worker(qtbot, lambda: automation.open_project(str(path)))
    assert window.project_path == path
    assert window._recent_project_paths() == [path]
    window.action_clear_recent.trigger()
    in_worker(qtbot, lambda: automation.update_project(
        ProjectPatch(title="Edited"), opened["revision"]
    ))
    assert window.project.title == "Edited"
    assert window._recent_project_paths() == []


def test_live_operation_does_not_globally_disable_editor(qtbot, live_editor):
    window, bridge, _automation = live_editor
    in_worker(qtbot, lambda: bridge.begin("Test operation"))
    assert not window._automation_active
    assert window.action_export.isEnabled()
    assert window.recent_projects_menu.menuAction().isEnabled()
    assert window.editor_splitter.isEnabled()
    in_worker(qtbot, bridge.end)
    assert window.action_export.isEnabled()
    assert window.recent_projects_menu.menuAction().isEnabled()
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    with pytest.raises(ValueError, match="modal dialog"):
        in_worker(qtbot, lambda: bridge.begin("Blocked"))
    dialog.close()
    assert not window._automation_active


def test_nonmodal_backing_workflow_does_not_block_other_projects(qtbot, live_editor):
    window, bridge, _automation = live_editor
    dialog = QDialog(window)
    window._backing_dialog = dialog
    try:
        window.add_project(PackProject(title="Other"), dirty=False)
        in_worker(qtbot, lambda: bridge.begin("Edit other project"))
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
    assert window.active_editor.recovery_store.load() is not None


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


def test_disconnect_preserves_each_documents_recovery(qtbot, live_editor):
    window, bridge, _automation = live_editor
    first = window.active_editor
    first._set_dirty(True)
    second = window.add_project(PackProject(title="Recover second"), dirty=True)
    bridge.disconnected.emit()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert first.recovery_store.load().project.title == "Live"
    assert second.recovery_store.load().project.title == "Recover second"


def test_disconnect_dismisses_nonmodal_close_question_without_losing_draft(qtbot, live_editor):
    window, bridge, _automation = live_editor
    editor = window.active_editor
    editor._set_dirty(True)
    assert not window.close()
    assert window._decisions and window._closing
    bridge.disconnected.emit()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert editor.recovery_store.load().project.title == "Live"


def test_bound_live_edit_targets_original_document_after_tab_switch(qtbot, live_editor):
    window, _bridge, automation = live_editor
    first = in_worker(qtbot, automation.get_project)
    bound = in_worker(qtbot, automation.for_project)
    second = window.add_project(PackProject(title="Second", authors=["Tester"]), dirty=False)
    updated = in_worker(qtbot, lambda: bound.update_project(
        ProjectPatch(title="First edited"), first["revision"]
    ))
    assert updated["project_id"] == first["project_id"]
    assert window.active_editor is second
    assert second.project.title == "Second"
    assert window.editor_for_project(first["project_id"]).title_edit.text() == "First edited"
    with pytest.raises(ValueError, match="Unknown project_id"):
        in_worker(qtbot, lambda: automation.for_project("unknown"))
    with pytest.raises(ValueError, match="Unknown project_id"):
        in_worker(qtbot, lambda: automation.for_project(""))


def test_mcp_save_does_not_clear_newer_edits_or_steal_tab(
    qtbot, live_editor, tmp_path, monkeypatch,
):
    window, _bridge, automation = live_editor
    bound = in_worker(qtbot, automation.for_project)
    before = in_worker(qtbot, bound.get_project)
    started, release = threading.Event(), threading.Event()
    save = ProjectStore.save

    def slow_save(project, path):
        started.set()
        assert release.wait(10)
        save(project, path)

    monkeypatch.setattr(ProjectStore, "save", slow_save)
    result = Future()
    path = tmp_path / "snapshot.cvpack.json"

    def run():
        try:
            result.set_result(bound.save_project(before["revision"], str(path)))
        except Exception as error:
            result.set_exception(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        qtbot.waitUntil(started.is_set)
        window.title_edit.selectAll()
        qtbot.keyClicks(window.title_edit, "Newer draft")
        other = window.add_project(PackProject(title="Other"), dirty=False)
    finally:
        release.set()
    qtbot.waitUntil(result.done)
    thread.join(timeout=2)
    saved = result.result()
    assert saved["dirty"]
    assert saved["project"]["title"] == "Newer draft"
    assert ProjectStore.load(path).title == "Live"
    assert window.active_editor is other


def test_ui_hooks_use_real_fields_tabs_and_window_image(qtbot, live_editor):
    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    if QApplication.platformName() in {"offscreen", "minimal"}:
        pytest.skip("Hit-routed visible UI input requires a real window-system platform.")
    window, bridge, automation = live_editor
    hooks = UIAutomation(bridge)
    first = window.active_editor
    second = window.add_project(PackProject(title="Second"), dirty=False)
    state = in_worker(qtbot, hooks.state)
    assert state["visible"]
    assert state["active_project_id"] == second.session.id
    png = in_worker(qtbot, hooks.screenshot)
    assert png.startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="allowlisted"):
        in_worker(qtbot, lambda: hooks.interact("__dict__", "click"))
    with pytest.raises(ValueError, match="not the visible"):
        in_worker(qtbot, lambda: hooks.interact(
            "projectTitle", "type", first.session.id, "Must not change"
        ))
    first_index = window.tabs.indexOf(first)
    action = in_worker(qtbot, lambda: hooks.interact("projectTabs", "select", index=first_index))
    qtbot.waitUntil(lambda: window.active_editor is first)
    assert action["state"] == "queued"
    in_worker(qtbot, lambda: hooks.interact("projectTitle", "type", first.session.id, "UI title"))
    qtbot.waitUntil(lambda: first.title_edit.text() == "UI title")
    in_worker(qtbot, lambda: hooks.interact(
        "segmentCaption", "type", first.session.id, "UI caption"
    ))
    qtbot.waitUntil(lambda: first.project.segments[0].caption == "UI caption")
    assert second.project.title == "Second"
    assert in_worker(qtbot, automation.get_project)["project"]["title"] == "UI title"
    state = in_worker(qtbot, hooks.state)
    assert all(item["state"] == "completed" for item in state["actions"])


def test_background_job_capture_cancel_and_edits_do_not_cross_projects(qtbot, live_editor):
    from choicer_voicer_pack_creator.mcp_jobs import LiveJobs

    window, bridge, automation = live_editor
    jobs = LiveJobs(bridge)
    first = window.active_editor
    release = threading.Event()

    def work(ctx):
        while not release.wait(0.01):
            ctx.check_cancelled()
        return {"revision": "snapshot"}

    handles = [
        window.job_manager.submit(first.session.id, "analysis", "Held scan", work)
        for _ in range(window.job_manager.limits["cpu"] + 1)
    ]
    try:
        qtbot.waitUntil(lambda: handles[-1].record.state == "waiting")
        waiting = in_worker(qtbot, lambda: jobs.cancel(handles[-1].id))
        assert waiting["state"] == "cancelled"
        running = in_worker(qtbot, lambda: jobs.cancel(handles[0].id))
        assert running["cancel_requested"]
        second = window.add_project(PackProject(title="Second"), dirty=False)
        current = in_worker(qtbot, automation.get_project)
        in_worker(qtbot, lambda: automation.for_project(second.session.id).update_project(
            ProjectPatch(title="Second edited"), current["revision"]
        ))
        qtbot.waitUntil(lambda: handles[0].record.state == "cancelled")
        assert window.active_editor is second
        assert first.project.title == "Live"
        assert second.project.title == "Second edited"
        records = in_worker(qtbot, lambda: jobs.list(first.session.id))["jobs"]
        assert all(record["project_id"] == first.session.id for record in records)
    finally:
        release.set()
    qtbot.waitUntil(lambda: all(not handle.record.active for handle in handles))


def test_analysis_job_keeps_submitted_snapshot_without_applying_stale_result(
    qtbot, live_editor, tmp_path, monkeypatch,
):
    from choicer_voicer_pack_creator.mcp_jobs import LiveJobs

    window, bridge, automation = live_editor
    source = tmp_path / "dummy-source.mp4"
    source.write_bytes(b"Only used by deterministic snapshot test")
    first = window.active_editor
    first.project.video_path = str(source)
    before = in_worker(qtbot, automation.get_project)
    release = threading.Event()

    def analyze(frozen, *args):
        snapshot = frozen.access.snapshot()
        while not release.wait(0.01):
            args[-1]()  # Exercise the scheduler's cancellation callback.
        return {"project_id": snapshot.project_id, "revision": snapshot.revision,
                "title": snapshot.project.title}

    monkeypatch.setattr(PackAutomation, "analyze", analyze)
    jobs = LiveJobs(bridge)
    bound = in_worker(qtbot, automation.for_project)
    record = in_worker(qtbot, lambda: jobs.start(bound, "analysis", before["revision"]))
    try:
        qtbot.waitUntil(lambda: window.job_manager.handle(record["job_id"]).record.state == "running")
        first.title_edit.selectAll()
        qtbot.keyClicks(first.title_edit, "Newer title")
        second = window.add_project(PackProject(title="Second"), dirty=False)
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.job_manager.handle(record["job_id"]).record.active)
    result = in_worker(qtbot, lambda: jobs.get(record["job_id"]))
    assert result["state"] == "succeeded"
    assert result["result"] == {
        "project_id": before["project_id"], "revision": before["revision"], "title": "Live",
    }
    assert first.project.title == "Newer title"
    assert first.dirty
    assert window.active_editor is second
    with pytest.raises(ValueError, match="Project changed"):
        in_worker(qtbot, lambda: jobs.start(bound, "analysis", before["revision"]))


def test_queued_processing_rejects_assets_changed_since_submission(
    qtbot, live_editor, tmp_path, monkeypatch,
):
    from choicer_voicer_pack_creator.mcp_jobs import LiveJobs

    window, bridge, automation = live_editor
    source = tmp_path / "queued-source.mp4"
    source.write_bytes(b"original source")
    window.active_editor.project.video_path = str(source)
    before = in_worker(qtbot, automation.get_project)
    release, processed = threading.Event(), threading.Event()
    holders = [
        window.job_manager.submit(
            None, "fixture", "Hold CPU capacity", lambda _ctx: release.wait(10)
        )
        for _ in range(window.job_manager.limits["cpu"])
    ]
    monkeypatch.setattr(PackAutomation, "analyze", lambda *_args: processed.set())
    jobs = LiveJobs(bridge)
    bound = in_worker(qtbot, automation.for_project)
    try:
        qtbot.waitUntil(lambda: all(item.record.state == "running" for item in holders))
        record = in_worker(qtbot, lambda: jobs.start(bound, "analysis", before["revision"]))
        handle = window.job_manager.handle(record["job_id"])
        qtbot.waitUntil(lambda: handle.record.state == "waiting")
        source.write_bytes(b"replacement while queued")
    finally:
        release.set()
    qtbot.waitUntil(lambda: not handle.record.active)
    assert handle.record.state == "failed"
    assert "Source assets changed" in handle.record.error
    assert not processed.is_set()
    assert len(record["source_snapshot"]["asset_revision"]) == 64


def test_queued_tab_close_keeps_identity_when_tabs_reorder(qtbot, live_editor):
    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    window, bridge, _automation = live_editor
    hooks = UIAutomation(bridge)
    first = window.active_editor
    second = window.add_project(PackProject(title="Second"), dirty=False)
    window.focus_project(first.session.id)
    index = window.tabs.indexOf(first)
    with pytest.raises(ValueError, match="does not match project_id"):
        hooks.interact(
            "projectTabs", "close_tab", first.session.id, index=window.tabs.indexOf(second)
        )
    accepted = hooks.interact("projectTabs", "close_tab", first.session.id, index=index)
    window.tabs.tabBar().moveTab(index, window.tabs.indexOf(second))
    qtbot.waitUntil(lambda: window.tabs.indexOf(first) < 0)
    assert window.tabs.indexOf(second) >= 0
    record = next(item for item in hooks.state()["actions"] if item["action_id"] == accepted["action_id"])
    assert record["state"] == "completed"


def test_global_ui_controls_reject_misleading_document_scope(live_editor):
    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    window, bridge, _automation = live_editor
    hooks = UIAutomation(bridge)
    with pytest.raises(ValueError, match="Omit project_id"):
        hooks.interact("taskCancel", "click", window.active_editor.session.id)


def test_queued_caption_input_rejects_changed_segment_selection(qtbot, live_editor):
    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    window, bridge, _automation = live_editor
    hooks = UIAutomation(bridge)
    editor = window.active_editor
    first = editor.project.segments[0]
    second = Segment(3, 4, "Second caption", ["Tester"])
    editor.project.segments.append(second)
    editor._refresh_table(first.id)
    accepted = hooks.interact("segmentCaption", "type", text="Must not apply")
    editor.select_segment(second.id)
    qtbot.waitUntil(lambda: hooks.state()["actions"][-1]["state"] == "failed")
    record = hooks.state()["actions"][-1]
    assert record["action_id"] == accepted["action_id"]
    assert "selected segment changed" in record["error"]
    assert first.caption == "Original"
    assert second.caption == "Second caption"


def test_inactive_window_caption_input_rejects_selection_during_activation(qtbot, live_editor):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QWidget

    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    if QApplication.platformName() in {"offscreen", "minimal"}:
        pytest.skip("Native activation and queued hit-routed input require a real window system.")
    window, bridge, _automation = live_editor
    hooks = UIAutomation(bridge)
    editor = window.active_editor
    first = editor.project.segments[0]
    second = Segment(3, 4, "Second caption", ["Tester"])
    editor.project.segments.append(second)
    editor._refresh_table(first.id)
    other = QWidget()
    qtbot.addWidget(other)
    other.resize(80, 80)
    other.move(window.mapToGlobal(QPoint(0, 0)))
    other.show()
    other.activateWindow()
    qtbot.waitUntil(lambda: other.isActiveWindow() and not window.isActiveWindow())
    try:
        typing = hooks.interact("segmentCaption", "type", text="Only for the first segment")
        selecting = hooks.interact("segmentsTable", "select", index=1)
        qtbot.waitUntil(lambda: all(
            item["state"] in {"completed", "failed"} for item in hooks.state()["actions"]
        ))
        records = {item["action_id"]: item for item in hooks.state()["actions"]}
        assert records[selecting["action_id"]]["state"] == "completed", records
        assert editor.selected_segment_id == second.id
        assert records[typing["action_id"]]["state"] == "failed"
        assert "selected segment changed" in records[typing["action_id"]]["error"]
        assert first.caption == "Original"
        assert second.caption == "Second caption"
    finally:
        other.close()


def test_ui_selection_refuses_a_clipped_row_instead_of_claiming_success(qtbot, live_editor):
    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    window, bridge, _automation = live_editor
    hooks = UIAutomation(bridge)
    table = window.active_editor.segment_table
    table.setFixedHeight(table.horizontalHeader().height() + 2 * table.frameWidth())
    accepted = hooks.interact("segmentsTable", "select", index=0)
    qtbot.waitUntil(lambda: hooks.state()["actions"][-1]["state"] == "failed")
    record = hooks.state()["actions"][-1]
    assert record["action_id"] == accepted["action_id"]
    assert "clipped" in record["error"]


def test_mcp_save_respects_pending_gui_destination_reservation(
    qtbot, live_editor, tmp_path, monkeypatch,
):
    window, _bridge, automation = live_editor
    first = window.active_editor
    second = window.add_project(PackProject(title="Second"), dirty=False)
    started, release = threading.Event(), threading.Event()
    original = ProjectStore.save

    def held_save(project, destination):
        started.set()
        assert release.wait(10)
        original(project, destination)

    monkeypatch.setattr(ProjectStore, "save", held_save)
    destination = tmp_path / "reserved.cvpack.json"
    assert window.save_editor(first, destination=destination)
    try:
        qtbot.waitUntil(started.is_set)
        current = in_worker(qtbot, automation.get_project)
        with pytest.raises(ValueError, match="another save path"):
            in_worker(qtbot, lambda: automation.save_project(
                current["revision"], str(destination), overwrite=True
            ))
        assert second.project_path is None
    finally:
        release.set()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert ProjectStore.load(destination).title == "Live"
    assert not window._save_tokens


def test_live_loading_document_is_readable_but_rejects_mutations(qtbot, live_editor, tmp_path):
    window, _bridge, automation = live_editor
    editor = window.active_editor
    editor.session.loading = True
    try:
        current = in_worker(qtbot, automation.get_project)
        assert current["loading"]
        with pytest.raises(ValueError, match="still loading"):
            in_worker(qtbot, lambda: automation.update_project(
                ProjectPatch(title="Do not overwrite pending load"), current["revision"]
            ))
        with pytest.raises(ValueError, match="still loading"):
            in_worker(qtbot, lambda: automation.save_project(
                current["revision"], str(tmp_path / "pending.cvpack.json")
            ))
        assert editor.project.title == "Live"
        assert not (tmp_path / "pending.cvpack.json").exists()
    finally:
        editor.session.loading = False


def test_closed_pending_document_is_not_reactivated_by_mcp(qtbot, live_editor, tmp_path):
    from choicer_voicer_pack_creator.ui_automation import UIAutomation

    window, bridge, automation = live_editor
    editor = window.active_editor
    path = tmp_path / "closed.cvpack.json"
    project = PackProject(title="Saved disk", authors=["Tester"])
    ProjectStore.save(project, path)
    editor._set_project(project, path, False)
    editor.title_edit.setText("Discarded draft")
    old_id = editor.session.id
    release = threading.Event()
    job = window.job_manager.submit(
        old_id, "recovery", "Held recovery", lambda _ctx: release.wait(10), resource_class="io",
    )
    hooks = UIAutomation(bridge)
    try:
        qtbot.waitUntil(lambda: job.record.state == "running")
        window.close_project_tab(window.tabs.indexOf(editor))
        hooks.interact("projectCloseDiscard", "click")
        qtbot.waitUntil(lambda: old_id not in {session.id for session in window.project_sessions})
        assert old_id in window.editors  # Retained solely until the pending job finishes.
        with pytest.raises(ValueError, match="Unknown project_id"):
            in_worker(qtbot, lambda: automation.for_project(old_id))
        with pytest.raises(ValueError, match="Unknown project_id"):
            in_worker(qtbot, lambda: automation.access.activate(old_id))
        reopened = in_worker(qtbot, lambda: automation.open_project(str(path)))
        assert reopened["project_id"] != old_id
        assert reopened["project"]["title"] == "Saved disk"
        assert not reopened["dirty"]
    finally:
        release.set()
    qtbot.waitUntil(lambda: not job.record.active)


@pytest.mark.integration
def test_real_live_mcp_export_cancellation_cleans_staging(qtbot, live_editor, tmp_path):
    from choicer_voicer_pack_creator.mcp_jobs import LiveJobs
    from choicer_voicer_pack_creator.media import MediaTools

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe required")
    window, bridge, automation = live_editor
    media = MediaTools()
    source = tmp_path / "cancellation-source.mp4"
    media.run([
        media.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=640x360:rate=24:duration=12", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=12", "-shortest",
        "-c:v", "mpeg4", "-c:a", "aac", str(source),
    ], "Preparing generated cancellation fixture")
    project = PackProject(
        title="Cancel fixture", authors=["Tester"], video_path=str(source), video_duration=12,
        video_height=360, video_fps=24,
        segments=[Segment(index * 0.3, index * 0.3 + 0.2, "Tone", ["Test"]) for index in range(32)],
    )
    path = tmp_path / "cancel.cvpack.json"
    ProjectStore.save(project, path)
    window.media = media
    opened = in_worker(qtbot, lambda: automation.open_project(str(path)))
    bound = in_worker(qtbot, lambda: automation.for_project(opened["project_id"]))
    jobs = LiveJobs(bridge)
    output = tmp_path / "output"
    record = in_worker(qtbot, lambda: jobs.start(
        bound, "export", opened["revision"], output_parent=str(output),
    ))
    handle = window.job_manager.handle(record["job_id"])
    qtbot.waitUntil(
        lambda: handle.record.state == "running" and handle.record.message != "Starting",
        timeout=20000,
    )
    second = window.add_project(PackProject(title="Keep editing"), dirty=False)
    cancelled = in_worker(qtbot, lambda: jobs.cancel(handle.id))
    assert cancelled["cancel_requested"]
    qtbot.waitUntil(lambda: not handle.record.active, timeout=20000)
    assert handle.record.state == "cancelled", handle.record
    assert window.active_editor is second
    assert not (output / "Cancel fixture").exists()
    assert not (output / "Cancel fixture.zip").exists()
    assert not output.exists() or not any(output.iterdir())
    assert in_worker(qtbot, bound.get_project)["revision"] == opened["revision"]
