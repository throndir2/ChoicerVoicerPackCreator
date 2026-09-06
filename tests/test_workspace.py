from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QMimeData, QObject, QPoint, QPointF, QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDragMoveEvent, QDropEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QLineEdit,
    QMenuBar,
    QMessageBox,
    QScrollArea,
    QTabBar,
    QToolButton,
)
from shiboken6 import isValid

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore, WorkspaceStore
from choicer_voicer_pack_creator.project_session import canonical_project_path
from choicer_voicer_pack_creator.ui.job_worker import JobWorker
from choicer_voicer_pack_creator.ui.main_window import MainWindow, ProjectEditor, WaveformWorker
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


class QuietMedia:
    def waveform_peaks(self, _path, _duration, *, cancelled):
        return [] if cancelled() else [0.5]


@pytest.fixture
def workspace(qtbot, tmp_path):
    window = MainWindow(
        QuietMedia(),
        settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    def close(current):
        current.setup_consent.cancel_all()
        for record in current.job_manager.active_jobs():
            current.job_manager.cancel(record.id)
        qtbot.waitUntil(lambda: not current.job_manager.active_jobs(), timeout=10000)
        for box in list(current._decisions):
            box.reject()
        for editor in current.editors.values():
            editor._commit_editors()
            editor.dirty = False
            editor._recovery_timer.stop()
        current.close()
        qtbot.waitUntil(lambda: current._close_approved and not current.isVisible(), timeout=10000)

    qtbot.addWidget(window, before_close_func=close)
    window.show()
    return window


def send_drop(target, mime, actions=Qt.DropAction.CopyAction | Qt.DropAction.MoveAction):
    events = [
        QDragEnterEvent(QPoint(5, 5), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
        QDragMoveEvent(QPoint(5, 5), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
        QDropEvent(QPointF(5, 5), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
    ]
    for event in events:
        QApplication.sendEvent(target, event)
    return events


@pytest.mark.parametrize("width", [1050, 1500])
@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_workspace_menu_precedes_tabs_and_project_toolbar(
    workspace, qtbot, width, stylesheet,
):
    workspace.setStyleSheet(stylesheet)
    workspace.resize(width, 950)
    menu = workspace.menuBar()
    tabs = workspace.tabs.tabBar()
    toolbar = workspace.active_editor.project_toolbar
    qtbot.waitUntil(lambda: (
        workspace.active_editor._layout_restored
        and workspace.tabs.y() >= menu.height()
    ))
    assert workspace.findChildren(QMenuBar) == [menu]
    assert menu.parent() is workspace
    assert menu.y() == 0
    assert menu.mapTo(workspace, QPoint(0, menu.height())).y() <= (
        tabs.mapTo(workspace, QPoint(0, 0)).y()
    )
    assert tabs.mapTo(workspace, QPoint(0, tabs.height())).y() <= (
        toolbar.mapTo(workspace, QPoint(0, 0)).y()
    )
    assert [action.text() for action in menu.actions()] == [
        "&File", "&Project", "&Segments", "&Tools", "&Help",
    ]
    actions = [action for action in toolbar.actions() if not action.isSeparator()]
    assert actions == [
        workspace.action_save, workspace.action_export,
        workspace.action_analyze, workspace.action_backing,
    ]
    assert [toolbar.widgetForAction(action).text() for action in actions] == [
        "Save", "Export", "Analyze", "Backing",
    ]
    assert all(toolbar.widgetForAction(action).isVisible() for action in actions)
    assert toolbar.widgetForAction(actions[-1]).geometry().right() < toolbar.width()
    assert workspace.tools_menu.actions() == [workspace.tasks_window.show_action]


def test_global_commands_survive_project_replacement_and_close(workspace, qtbot):
    menu = workspace.menuBar()
    initial = workspace.active_editor
    new_action = workspace.action_new
    help_actions = workspace.help_menu.actions()
    editor = workspace.add_project(PackProject(title="Replacement"), dirty=False)
    qtbot.waitUntil(lambda: not isValid(initial))
    workspace.action_close_project.trigger()
    qtbot.waitUntil(lambda: not isValid(editor))
    assert workspace.menuBar() is menu
    assert workspace.action_new is new_action
    assert new_action.parent() is workspace
    assert workspace.help_menu.actions() == help_actions
    assert workspace.updater.check_action in help_actions
    assert workspace.action_logs in help_actions
    assert workspace.findChildren(QMenuBar) == [menu]
    assert len([
        action for action in workspace.findChildren(QAction)
        if action.shortcut() == new_action.shortcut()
    ]) == 1


def test_project_menus_follow_active_tab_without_duplicate_actions(workspace, qtbot):
    first = workspace.add_project(PackProject(title="First"), dirty=False)
    second = workspace.add_project(PackProject(title="Second"), dirty=False)
    for current, previous in ((first, second), (second, first), (first, second)):
        workspace.tabs.setCurrentWidget(current)
        for menu, current_actions, previous_actions in (
            (workspace.file_menu, current.file_actions, previous.file_actions),
            (workspace.project_menu, current.project_actions, previous.project_actions),
            (workspace.segments_menu, current.segment_actions, previous.segment_actions),
        ):
            assert all(menu.actions().count(action) == 1 for action in current_actions)
            assert not any(action in menu.actions() for action in previous_actions)
    workspace.tabs.tabBar().moveTab(0, 1)
    assert workspace.active_editor is first
    assert first.action_save in workspace.file_menu.actions()
    workspace.close_project_tab(workspace.tabs.indexOf(second))
    qtbot.waitUntil(lambda: not isValid(second))
    assert first.action_save in workspace.file_menu.actions()


@pytest.mark.parametrize("control", ["menu", "shortcut", "toolbar"])
def test_project_save_commands_only_save_active_tab(
    workspace, qtbot, monkeypatch, control,
):
    first = workspace.add_project(PackProject(title="First"), dirty=False)
    second = workspace.add_project(PackProject(title="Second"), dirty=False)
    saved = []
    monkeypatch.setattr(workspace, "save_editor", lambda editor, **_kwargs: saved.append(editor))
    workspace.activateWindow()
    qtbot.waitUntil(workspace.isActiveWindow)
    for editor in (first, second, first):
        workspace.tabs.setCurrentWidget(editor)
        editor.title_edit.setFocus()
        qtbot.waitUntil(editor.title_edit.hasFocus)
        if control == "menu":
            menu = workspace.file_menu
            menu.popup(workspace.menuBar().mapToGlobal(QPoint(0, workspace.menuBar().height())))
            qtbot.waitUntil(menu.isVisible)
            qtbot.mouseClick(
                menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(editor.action_save).center(),
            )
        elif control == "shortcut":
            qtbot.keyClick(editor.title_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        else:
            qtbot.mouseClick(
                editor.project_toolbar.widgetForAction(editor.action_save),
                Qt.MouseButton.LeftButton,
            )
    assert saved == [first, second, first]


def test_global_open_shortcut_is_not_duplicated_across_tabs(workspace, qtbot, monkeypatch):
    first = workspace.add_project(PackProject(title="First"), dirty=False)
    second = workspace.add_project(PackProject(title="Second"), dirty=False)
    opened = []
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (opened.append(1) or "", ""))
    workspace.activateWindow()
    qtbot.waitUntil(workspace.isActiveWindow)
    for editor in (first, second):
        workspace.tabs.setCurrentWidget(editor)
        editor.title_edit.setFocus()
        qtbot.waitUntil(editor.title_edit.hasFocus)
        qtbot.keyClick(editor.title_edit, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
        assert editor.action_open is workspace.action_open
    assert opened == [1, 1]


def test_project_loading_disables_matching_menu_and_toolbar_actions(workspace):
    ready = workspace.add_project(PackProject(title="Ready"), dirty=False)
    loading = workspace.add_project(PackProject(title="Loading"), dirty=False)
    loading._set_loading(True)
    assert not loading.processing_panel.isEnabled()
    for action in loading.file_actions + loading.project_actions + loading.segment_actions:
        assert not action.isEnabled()
        button = loading.project_toolbar.widgetForAction(action)
        if button is not None:
            assert not button.isEnabled()
    assert workspace.action_open.isEnabled()
    assert workspace.action_new.isEnabled()
    assert workspace.tasks_window.show_action.isEnabled()
    workspace.tabs.setCurrentWidget(ready)
    assert ready.action_save.isEnabled()
    assert ready.action_save in workspace.file_menu.actions()
    assert ready.action_backing in workspace.project_menu.actions()
    loading._set_loading(False)
    assert loading.processing_panel.isEnabled()
    workspace.tabs.setCurrentWidget(loading)
    assert loading.action_save.isEnabled()
    assert loading.action_backing.isEnabled()


@pytest.mark.parametrize("control", ["tab-button", "shortcut"])
def test_close_project_controls_keep_unsaved_confirmation(workspace, qtbot, control):
    editor = workspace.add_project(PackProject(title="Unsaved"), dirty=True)
    workspace.activateWindow()
    qtbot.waitUntil(workspace.isActiveWindow)
    if control == "tab-button":
        button = workspace.tabs.tabBar().tabButton(
            workspace.tabs.indexOf(editor), QTabBar.ButtonPosition.RightSide,
        )
        assert button.accessibleName() == "Close project: Unsaved"
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    else:
        editor.title_edit.setFocus()
        qtbot.keyClick(editor.title_edit, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: bool(workspace._decisions))
    box = workspace._decisions[-1]
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert workspace.active_editor is editor
    assert editor.dirty


def test_command_icons_and_compact_buttons_are_described(workspace):
    editor = workspace.active_editor
    for action in editor.file_actions + editor.project_actions + editor.segment_actions + [
        workspace.action_new, workspace.action_open, workspace.action_import,
        workspace.tasks_window.show_action, workspace.action_logs, workspace.updater.check_action,
    ]:
        assert action.toolTip()
        assert action.statusTip()
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
            image = action.icon().pixmap(32, 32, mode).toImage()
            assert not image.isNull()
            assert any(
                image.pixelColor(x, y).alpha() > 0
                for x in range(image.width()) for y in range(image.height())
            )
    for button in editor.findChildren(QToolButton):
        if button.defaultAction() is not None:
            assert button.toolTip()
            assert button.accessibleName()
    assert "Ctrl+S" in editor.action_save.toolTip()
    assert "Ctrl+Shift+M" in editor.combine_button.toolTip()
    assert editor.combine_button.defaultAction() is editor.action_combine


@pytest.mark.parametrize("target", [
    "window", "tabs", "video_widget", "timeline", "title_edit", "readme_edit",
    "caption_edit", "table",
])
def test_project_drop_opens_tab_without_editing_drop_target(
    workspace, qtbot, tmp_path, target,
):
    original = workspace.active_editor
    original._set_project(PackProject(title="Unsaved work", readme="Keep notes"), None, True)
    before = original.project.to_dict()
    path = tmp_path / "Dropped project.cvpack.json"
    ProjectStore.save(PackProject(title="Dropped project"), path)
    targets = {
        "window": workspace,
        "tabs": workspace.tabs.tabBar(),
        "video_widget": original.video_widget,
        "timeline": original.timeline,
        "title_edit": original.title_edit,
        "readme_edit": original.readme_edit.viewport(),
        "caption_edit": original.caption_edit.viewport(),
        "table": original.segment_table.viewport(),
    }
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    events = send_drop(targets[target], mime)
    assert all(event.isAccepted() for event in events)
    assert all(event.dropAction() == Qt.DropAction.CopyAction for event in events)
    qtbot.waitUntil(lambda: workspace.active_editor.project_path == path)
    assert workspace.tabs.count() == 2
    assert workspace.project.title == "Dropped project"
    assert not workspace.dirty
    assert original.project.to_dict() == before
    assert original.dirty
    assert not workspace._decisions
    assert path.is_file()


@pytest.mark.parametrize("extension", [".mp4", ".MKV", ".mov", ".webm", ".ogv", ".avi"])
def test_video_drop_creates_project_and_keeps_initial_processing(
    workspace, qtbot, tmp_path, monkeypatch, extension,
):
    source = tmp_path / f"Dropped video{extension}"
    source.write_bytes(b"synthetic")
    probed, processed = [], []

    def probe(path):
        probed.append(path)
        return SimpleNamespace(duration=4.0)

    monkeypatch.setattr(workspace.media, "probe", probe, raising=False)
    monkeypatch.setattr(
        ProjectEditor, "_finish_new_import",
        lambda editor, project: processed.append((editor.session.id, project)),
    )
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    assert all(event.isAccepted() for event in send_drop(workspace, mime))
    qtbot.waitUntil(lambda: bool(processed))
    assert workspace.tabs.count() == 1
    assert probed == [source]
    assert processed == [(workspace.active_editor.session.id, workspace.project)]
    assert workspace.project.title == "Dropped video"
    assert workspace.project.video_path == str(source)
    assert workspace.project.video_duration == 4.0
    assert workspace.project_path is None and workspace.dirty
    assert source.read_bytes() == b"synthetic"


def test_multiple_drops_keep_independent_tabs_and_focus_duplicate_projects(
    workspace, qtbot, tmp_path, monkeypatch,
):
    paths = [tmp_path / f"Project {index}.CVPACK.JSON" for index in range(2)]
    for index, path in enumerate(paths):
        ProjectStore.save(PackProject(title=f"Project {index}"), path)
    started, release = threading.Event(), threading.Event()
    load = ProjectStore.load

    def blocked_load(path):
        started.set()
        assert release.wait(5)
        return load(path)

    monkeypatch.setattr(ProjectStore, "load", blocked_load)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    try:
        send_drop(workspace, mime)
        qtbot.waitUntil(started.is_set)
        assert workspace.tabs.count() == 2
        assert all(editor.session.loading for editor in workspace.editors.values())
        mime.setUrls([QUrl.fromLocalFile(str(paths[0]))])
        send_drop(workspace, mime)
        qtbot.waitUntil(lambda: workspace.active_editor is workspace.project_for_path(paths[0]))
        assert workspace.tabs.count() == 2
    finally:
        release.set()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    first = workspace.active_editor
    first.title_edit.setText("Unsaved changes")
    first._commit_editors()
    send_drop(workspace, mime)
    qtbot.wait(1)
    assert workspace.active_editor is first
    assert workspace.tabs.count() == 2
    assert first.project.title == "Unsaved changes" and first.dirty
    assert load(paths[0]).title == "Project 0"


@pytest.mark.parametrize("kind", ["unsupported", "folder", "missing", "remote", "mixed", "move_only"])
def test_unsupported_drop_does_not_open_tabs_or_insert_paths(
    workspace, qtbot, tmp_path, kind,
):
    original = workspace.active_editor
    before = original.title_edit.text()
    path = tmp_path / "item.txt"
    path.write_text("not a project", encoding="utf-8")
    urls = [QUrl.fromLocalFile(str(path))]
    if kind == "folder":
        urls = [QUrl.fromLocalFile(str(tmp_path))]
    elif kind == "missing":
        urls = [QUrl.fromLocalFile(str(tmp_path / "missing.mp4"))]
    elif kind == "remote":
        urls = [QUrl("https://example.com/video.mp4")]
    elif kind in {"mixed", "move_only"}:
        project = tmp_path / "valid.cvpack.json"
        ProjectStore.save(PackProject(title="Valid"), project)
        urls = [QUrl.fromLocalFile(str(project)), *urls] if kind == "mixed" else [
            QUrl.fromLocalFile(str(project))
        ]
    mime = QMimeData()
    mime.setUrls(urls)
    actions = Qt.DropAction.MoveAction if kind == "move_only" else Qt.DropAction.CopyAction
    events = send_drop(original.title_edit, mime, actions)
    assert not any(event.isAccepted() for event in events)
    qtbot.wait(1)
    assert workspace.tabs.count() == 1
    assert original.title_edit.text() == before
    assert not original.dirty
    assert "Drop local video files" in workspace.statusBar().currentMessage()


def test_text_drops_still_edit_text_and_dialog_file_drops_do_not_open_projects(
    workspace, qtbot, tmp_path,
):
    mime = QMimeData()
    mime.setText("Dropped text")
    send_drop(workspace.title_edit, mime)
    assert "Dropped text" in workspace.title_edit.text()
    assert workspace.tabs.count() == 1
    dialog = QDialog(workspace)
    qtbot.addWidget(dialog)
    field = QLineEdit(dialog)
    dialog.show()
    path = tmp_path / "project.cvpack.json"
    ProjectStore.save(PackProject(), path)
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    send_drop(field, mime)
    qtbot.wait(1)
    assert workspace.tabs.count() == 1


@pytest.mark.parametrize("kind", ["video", "project"])
def test_failed_drop_import_reports_error_and_preserves_other_edits(
    workspace, qtbot, tmp_path, monkeypatch, kind,
):
    original = workspace.active_editor
    original._set_project(PackProject(title="Keep me"), None, True)
    path = tmp_path / ("invalid.mp4" if kind == "video" else "invalid.cvpack.json")
    path.write_text("invalid", encoding="utf-8")

    def probe(_path):
        raise ValueError("Invalid video")

    monkeypatch.setattr(workspace.media, "probe", probe, raising=False)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    send_drop(workspace, mime)
    qtbot.waitUntil(lambda: bool(workspace._decisions))
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert workspace._decisions[0].windowTitle() == f"Could not open {kind}"
    assert workspace.active_editor is not original
    assert not workspace.active_editor.session.loading
    assert original.project.title == "Keep me" and original.dirty
    assert path.read_text(encoding="utf-8") == "invalid"


@pytest.mark.parametrize("kind", ["update-check", "export", "analysis"])
@pytest.mark.parametrize("failed", [False, True])
def test_background_jobs_do_not_open_tasks_or_resize_editor(workspace, qtbot, kind, failed):
    editor = workspace.active_editor
    tasks = workspace.tasks_window
    QApplication.processEvents()
    geometry = workspace.tabs.geometry()
    assert not workspace.findChildren(QDockWidget)
    assert not tasks.isVisible()
    assert not tasks._timer.isActive()

    def work(_context):
        if failed:
            raise RuntimeError("Synthetic task failure")

    project_id = None if kind == "update-check" else editor.session.id
    job = workspace.job_manager.submit(project_id, kind, "Background task", work)
    qtbot.waitUntil(lambda: not job.record.active)
    assert job.record.state == ("failed" if failed else "succeeded")
    assert not tasks.isVisible()
    assert not tasks._timer.isActive()
    assert tasks.table.rowCount() == 0
    assert workspace.tabs.geometry() == geometry
    assert workspace.active_editor is editor
    if failed and project_id is not None:
        assert "[!]" in workspace.tabs.tabText(workspace.tabs.indexOf(editor))
    tasks.show_action.trigger()
    assert tasks.isVisible()
    assert tasks.table.rowCount() == 1
    assert tasks.table.item(0, 2).text() == job.record.state.capitalize()
    tasks.table.selectRow(0)
    if failed:
        assert "Synthetic task failure" in tasks.details.toPlainText()
    tasks.close()
    assert not tasks._timer.isActive()


def test_running_project_does_not_block_edit_save_or_open(workspace, qtbot, tmp_path):
    window = workspace
    a = window.active_editor
    a._set_project(PackProject(title="A", authors=["Author"]), None, False)
    release = threading.Event()

    def export(context):
        while not release.wait(0.01):
            context.check_cancelled()
        return tmp_path / "A"

    job = window.job_manager.submit(a.session.id, "export", "Export A", export)
    qtbot.waitUntil(lambda: job.record.state == "running")
    assert "[working]" in window.tabs.tabText(window.tabs.indexOf(a))
    tasks = window.tasks_window
    geometry = window.tabs.geometry()
    tasks.show_action.trigger()
    assert tasks.isVisible() and tasks.isWindow() and not tasks.isModal()
    assert QApplication.activeModalWidget() is None
    assert tasks._timer.isActive()
    assert window.tabs.geometry() == geometry
    tasks.table.selectRow(0)
    qtbot.keyClick(tasks.table, Qt.Key.Key_Return)
    assert not job.record.cancel_requested
    b = window.add_project(PackProject(title="B", authors=["Author"]), dirty=False)
    b.title_edit.selectAll()
    qtbot.keyClicks(b.title_edit, "B edited")
    assert b.dirty and b.project.title == "B edited"
    tasks.close()
    assert not tasks.isVisible()
    assert not tasks._timer.isActive()
    assert job.record.active and not job.record.cancel_requested
    destination = tmp_path / "B.cvpack.json"
    assert window.save_editor(b, destination=destination)
    qtbot.waitUntil(lambda: destination.is_file() and not b.dirty)
    source = tmp_path / "C.cvpack.json"
    ProjectStore.save(PackProject(title="C", authors=["Author"]), source)
    window.open_path(source)
    count = window.tabs.count()
    window.open_path(source)
    assert window.tabs.count() == count
    qtbot.waitUntil(lambda: window.active_editor.project.title == "C")
    assert job.record.active
    window.focus_project(a.session.id)
    window.focus_project(b.session.id)
    assert b.project.title == "B edited"
    assert not job.record.cancel_requested
    release.set()
    qtbot.waitUntil(lambda: not job.record.active)
    assert window.active_editor is b
    assert not tasks.isVisible()
    assert "[working]" not in window.tabs.tabText(window.tabs.indexOf(a))
    tasks.show_action.trigger()
    tasks.show_action.trigger()
    assert window.findChildren(QDialog, "tasksWindow") == [tasks]
    assert tasks.table.rowCount() == len(window.job_manager.tasks())
    assert window.active_editor is b
    tasks.close()


def test_tasks_restore_only_hidden_project_tabs(workspace, qtbot):
    first = workspace.active_editor
    first._set_project(PackProject(title="Origin"), None, False)
    job = workspace.job_manager.submit(first.session.id, "analysis", "Scan", lambda _ctx: None)
    qtbot.waitUntil(lambda: not job.record.active)
    second = workspace.add_project(PackProject(title="Other"), dirty=False)
    tasks = workspace.tasks_window
    tasks.show_action.trigger()
    tasks.table.selectRow(0)
    assert not tasks.restore_button.isEnabled()
    tasks._restore_project()
    assert workspace.active_editor is second
    workspace._hide_editor(first, retain=True)
    tasks.refresh()
    assert tasks.restore_button.isEnabled()
    tasks.restore_button.click()
    assert workspace.active_editor is first
    assert workspace.tabs.indexOf(first) >= 0
    assert not first.session.hidden
    assert not tasks.restore_button.isEnabled()
    tasks.close()


def test_tabs_keep_selection_range_and_zoom(workspace):
    window = workspace
    first = window.active_editor
    segment = Segment(1, 4, "First line", ["A"])
    first._set_project(
        PackProject(title="A", authors=["Author"], video_duration=10, segments=[segment]),
        None, False,
    )
    first.select_segment(segment.id)
    first.mark_in_spin.setValue(2)
    first.mark_out_spin.setValue(5)
    first.zoom_slider.setValue(35)
    second = window.add_project(PackProject(title="B", authors=["Author"]), dirty=False)
    window.focus_project(first.session.id)
    assert first.selected_segment_id == segment.id
    assert first.mark_in_spin.value() == 2
    assert first.mark_out_spin.value() == 5
    assert first.zoom_slider.value() == 35
    assert second.project.segments == []


def test_two_projects_share_exact_whisper_component_consent(workspace, qtbot, tmp_path, monkeypatch):
    from choicer_voicer_pack_creator.ui import analysis_dialog

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Synthetic analysis failure; no model download")

    monkeypatch.setattr(analysis_dialog, "analyze_video", unavailable)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic")
    editors = [
        workspace.add_project(
            PackProject(title=title, video_path=str(source), video_duration=5), dirty=False,
        )
        for title in ("A", "B")
    ]
    for editor in editors:
        editor.open_analysis_dialog()
        editor._analysis_dialog.whisper_check.setChecked(True)
        editor._analysis_dialog.model_combo.setCurrentIndex(
            editor._analysis_dialog.model_combo.findData("tiny")
        )
        editor._analysis_dialog.start_scan()
    box = workspace.setup_consent.box
    assert box is not None and not box.isModal()
    assert len(workspace.findChildren(QMessageBox, "sharedSetupConsent")) == 1
    assert box.text().count("Whisper CPU runtime") == 1
    assert box.text().count("Whisper tiny model") == 1
    assert all(editor._analysis_dialog.worker is None for editor in editors)
    editors[0].session.source_revision += 1
    box.button(QMessageBox.StandardButton.Yes).click()
    qtbot.waitUntil(lambda: editors[1]._analysis_dialog.worker is None)
    assert not any(
        job.kind == "analysis" for job in workspace.job_manager.tasks(editors[0].session.id)
    )
    jobs = [
        job for job in workspace.job_manager.tasks(editors[1].session.id) if job.kind == "analysis"
    ]
    assert len(jobs) == 1 and jobs[0].state == "failed"
    assert all(not editor._analysis_dialog._pending_scan for editor in editors)


@pytest.mark.parametrize("background", [False, True])
def test_backing_consent_keeps_other_project_request_when_one_closes(
    workspace, qtbot, tmp_path, monkeypatch, background,
):
    from choicer_voicer_pack_creator.separation import SeparationDownloadRequired
    from choicer_voicer_pack_creator.ui.backing_dialog import SeparationManager

    output = tmp_path / "backing.wav"
    output.write_bytes(b"synthetic backing")
    calls = []

    def generate(_manager, _media, source, *, allow_download, **_kwargs):
        calls.append((source.name, allow_download))
        if not allow_download:
            raise SeparationDownloadRequired("Model consent required")
        return output

    monkeypatch.setattr(SeparationManager, "generate", generate)
    editors = []
    for name in ("A", "B"):
        source = tmp_path / f"{name}.mp4"
        source.write_bytes(b"synthetic")
        editor = workspace.add_project(
            PackProject(title=name, video_path=str(source), video_duration=5), dirty=False,
        )
        editors.append(editor)
        editor.generate_backing_track(background=background)
    qtbot.waitUntil(lambda: all(editor._backing_dialog._pending_consent for editor in editors))
    assert all(editor._backing_dialog.isVisible() is not background for editor in editors)
    box = workspace.setup_consent.box
    assert box is not None and box.text().count("Music-separation model") == 1
    first_id = editors[0].session.id
    workspace._hide_editor(editors[0], retain=False)
    assert workspace.setup_consent.box is box and box.isVisible()
    box.button(QMessageBox.StandardButton.Yes).click()
    qtbot.waitUntil(lambda: editors[1].project.backing_track_path == str(output))
    assert sorted(calls) == [("A.mp4", False), ("B.mp4", False), ("B.mp4", True)]
    assert all(job.state == "failed" for job in workspace.job_manager.tasks(first_id)
               if job.kind == "backing")


@pytest.mark.parametrize("kind", ["analysis", "refinement", "backing", "youtube", "export"])
def test_tasks_retry_uses_origin_and_rejects_superseded_source(
    workspace, qtbot, tmp_path, monkeypatch, kind,
):
    from choicer_voicer_pack_creator.models import SourceCaption
    from choicer_voicer_pack_creator.ui import analysis_dialog, backing_dialog, youtube_dialog
    from choicer_voicer_pack_creator.ui.export_options_dialog import ExportOptionsDialog

    def failed(*_args, **_kwargs):
        raise RuntimeError("Synthetic retry failure")

    monkeypatch.setattr(analysis_dialog, "analyze_video", failed)
    monkeypatch.setattr(backing_dialog.SeparationManager, "generate", failed)
    monkeypatch.setattr(youtube_dialog, "download_youtube", failed)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path))
    monkeypatch.setattr(ExportOptionsDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic")
    editor = workspace.add_project(PackProject(
        title="Origin", authors=["A"], video_path=str(source), video_duration=5,
        segments=[Segment(1, 2, "Line", ["A"])],
        backing_track_path=str(source) if kind == "export" else "",
        source_captions=[SourceCaption(1, 2, "Caption", "YouTube")] if kind == "refinement" else [],
    ), dirty=False)
    def jobs():
        return [job for job in workspace.job_manager.tasks(editor.session.id) if job.kind == kind]

    if kind in {"analysis", "refinement"}:
        editor.open_analysis_dialog()
        dialog = editor._analysis_dialog
        if kind == "refinement":
            dialog.start_refinement()
        else:
            dialog.whisper_check.setChecked(False)
            dialog.start_scan()
    elif kind == "backing":
        editor.generate_backing_track()
    elif kind == "youtube":
        editor._start_youtube_import()
        editor._youtube_dialog.url_edit.setText("https://www.youtube.com/watch?v=abcdefghijk")
        editor._youtube_dialog.folder_edit.setText(str(tmp_path))
        editor._youtube_dialog.start_download()
    else:
        editor.exporter = SimpleNamespace(export=failed)
        editor.export_pack()
    qtbot.waitUntil(lambda: len(jobs()) == 1)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs(editor.session.id))
    record = jobs()[0]
    assert record.kind == kind and record.state == "failed"
    other = workspace.add_project(PackProject(title="Other"), dirty=False)
    panel = workspace.tasks_window
    panel.show_action.trigger()
    row = next(
        row for row in range(panel.table.rowCount())
        if panel.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == record.id
    )
    panel.table.selectRow(row)
    qtbot.waitUntil(lambda: panel.retry_button.isEnabled())
    panel.retry_button.click()
    qtbot.waitUntil(lambda: len(jobs()) == 2)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs(editor.session.id))
    assert workspace.active_editor is other
    assert not workspace.job_manager.tasks(other.session.id)
    assert all(job.state == "failed" for job in jobs())
    editor.session.source_revision += 1
    panel.refresh()
    assert not panel.retry_button.isEnabled()
    panel._retry_selected()
    assert len(jobs()) == 2


def test_fresh_native_layout_keeps_task_and_segment_rows_clickable(workspace, qtbot, tmp_path):
    if QApplication.platformName() == "offscreen":
        pytest.skip("Requires a native Qt screen; run with QT_QPA_PLATFORM=windows.")
    workspace.setStyleSheet(APP_STYLESHEET)
    first = workspace.active_editor
    first._set_project(PackProject(title="A"), None, True)
    workspace.add_project(PackProject(title="B"), dirty=False)
    editor = workspace.add_project(PackProject(
        title="C", video_duration=10,
        segments=[Segment(index, index + 0.5, f"Line {index}", ["Actor"]) for index in range(4)],
    ), dirty=True)
    jobs = [
        workspace.job_manager.submit(first.session.id, "analysis", f"Task {index}", lambda _ctx: None)
        for index in range(6)
    ]
    detail = QDialog(workspace)
    workspace.tasks_window.register_detail(jobs[-1].id, detail)
    available = workspace.screen().availableGeometry()
    workspace.resize(min(1500, available.width() - 40), min(850, available.height() - 80))
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    QApplication.processEvents()
    assert workspace.height() <= available.height()
    assert not editor.editor_scroll.isAncestorOf(editor.processing_panel)
    assert editor.processing_panel.visibleRegion().boundingRect().height() == editor.processing_panel.height()
    scrollbar = editor.editor_scroll.verticalScrollBar()
    assert scrollbar.objectName() == "projectEditorScrollbar"
    if scrollbar.isVisible():
        qtbot.mouseClick(scrollbar, Qt.MouseButton.LeftButton, pos=scrollbar.rect().center())
        qtbot.keyClick(scrollbar, Qt.Key.Key_Home)
        assert scrollbar.value() == 0
        qtbot.keyClick(scrollbar, Qt.Key.Key_PageDown)
        assert scrollbar.value() > 0
        qtbot.keyClick(scrollbar, Qt.Key.Key_Home)
    for table in (editor.segment_table, workspace.tasks_window.table):
        if table is workspace.tasks_window.table:
            geometry = workspace.tabs.geometry()
            workspace.tasks_window.show_action.trigger()
            QApplication.processEvents()
            assert workspace.tabs.geometry() == geometry
        assert table.viewport().height() >= 2 * table.verticalHeader().defaultSectionSize(), (
            table.objectName(), table.size(), table.viewport().size(), workspace.size(), available
        )
        item = table.item(table.rowCount() - 1, 0)
        table.scrollToItem(item)
        if table is editor.segment_table:
            editor.editor_scroll.ensureWidgetVisible(table)
            QApplication.processEvents()
        rectangle = table.visualItemRect(item)
        assert table.viewport().rect().contains(rectangle.center())
        assert table.viewport().visibleRegion().contains(rectangle.center()), (
            table.objectName(), table.viewport().visibleRegion().boundingRect(), rectangle,
        )
        qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rectangle.center())
        assert table.currentRow() == table.rowCount() - 1
    assert workspace.tasks_window.detail_button.isEnabled()
    assert workspace.tasks_window.details.visibleRegion().boundingRect().height() >= 32, (
        workspace.tasks_window.geometry(), workspace.tasks_window.minimumSizeHint(),
        workspace.tasks_window.table.geometry(), workspace.tasks_window.details.geometry(),
        workspace.tasks_window.details.minimumHeight(),
    )
    qtbot.mouseClick(workspace.tasks_window.detail_button, Qt.MouseButton.LeftButton)
    assert detail.isVisible()
    detail.close()
    workspace.tasks_window.close()
    QApplication.processEvents()
    root = editor.editor_splitter.parentWidget()
    assert root.grab().toImage().pixelColor(2, 2).name() == "#080d14"
    assert workspace.grab().save(str(tmp_path / "native-readable-workspace.png"))
    for field, target, text in (
        (editor.title_edit, editor.title_edit, "Visible title"),
        (editor.caption_edit, editor.caption_edit.viewport(), "Visible caption"),
    ):
        parent = field.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                # Text editors expose their caret to ensureWidgetVisible; reveal the full field
                # through both scroll layers instead, regardless of the workspace header height.
                center = field.mapTo(parent.widget(), field.rect().center())
                parent.ensureVisible(
                    center.x(), center.y(), field.width() // 2 + 10, field.height() // 2 + 10,
                )
            parent = parent.parentWidget()
        QApplication.processEvents()
        point = target.rect().center()
        assert target.visibleRegion().contains(point)
        assert target.visibleRegion().boundingRect().height() >= target.height() - 2, (
            field.objectName(), target.size(), target.visibleRegion().boundingRect(),
        )
        workspace.raise_()
        workspace.activateWindow()
        assert QApplication.widgetAt(target.mapToGlobal(point)) is target
        qtbot.mouseClick(target, Qt.MouseButton.LeftButton, pos=point)
        qtbot.keyClick(target, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        qtbot.keyClicks(target, text)
    assert editor.project.title == "Visible title"
    assert editor.selected_segment().caption == "Visible caption"
    QApplication.processEvents()
    assert workspace.grab().save(str(tmp_path / "native-caption-workspace.png"))


def test_save_snapshot_cannot_clear_newer_edits(workspace, qtbot, tmp_path, monkeypatch):
    window = workspace
    editor = window.active_editor
    editor._set_project(PackProject(title="Revision N", authors=["Author"]), None, True)
    started, release = threading.Event(), threading.Event()
    original = ProjectStore.save

    def save(snapshot, destination):
        started.set()
        assert release.wait(5)
        original(snapshot, destination)

    monkeypatch.setattr(ProjectStore, "save", save)
    path = tmp_path / "project.cvpack.json"
    assert window.save_editor(editor, destination=path)
    qtbot.waitUntil(started.is_set)
    editor.title_edit.selectAll()
    qtbot.keyClicks(editor.title_edit, "Revision N plus one")
    release.set()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert editor.dirty
    assert editor.project.title == "Revision N plus one"
    assert ProjectStore.load(path).title == "Revision N"
    assert editor.session.saved_revision < editor.session.revision


def test_save_path_cannot_be_shared_by_two_documents(workspace, qtbot, tmp_path):
    first = workspace.active_editor
    first._set_project(PackProject(title="A", authors=["Author"]), None, True)
    second = workspace.add_project(PackProject(title="B", authors=["Author"]), dirty=True)
    path = tmp_path / "shared.cvpack.json"
    assert workspace.save_editor(first, destination=path)
    assert not workspace.save_editor(second, destination=path)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert ProjectStore.load(path).title == "A"
    assert second.dirty


def test_open_during_pending_save_as_focuses_owner(workspace, qtbot, tmp_path, monkeypatch):
    first = workspace.active_editor
    first._set_project(PackProject(title="A"), None, True)
    path = tmp_path / "shared.cvpack.json"
    started, release = threading.Event(), threading.Event()
    original = ProjectStore.save

    def save(project, destination):
        started.set()
        assert release.wait(5)
        original(project, destination)

    monkeypatch.setattr(ProjectStore, "save", save)
    try:
        assert workspace.save_editor(first, destination=path)
        qtbot.waitUntil(started.is_set)
        workspace.add_project(PackProject(title="B"), dirty=False)
        count = workspace.tabs.count()
        workspace.open_path(path)
        assert workspace.active_editor is first
        assert workspace.project_for_path(path) is first
        assert workspace.tabs.count() == count
    finally:
        release.set()
        qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert first.project_path == path and not first.dirty


def test_save_reservation_rejects_another_pending_open(workspace, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "shared.cvpack.json"
    ProjectStore.save(PackProject(title="Opening"), path)
    other = workspace.active_editor
    other._set_project(PackProject(title="Other"), None, True)
    started, release = threading.Event(), threading.Event()
    original = ProjectStore.load

    def load(source):
        started.set()
        assert release.wait(5)
        return original(source)

    monkeypatch.setattr(ProjectStore, "load", load)
    try:
        workspace.open_path(path)
        qtbot.waitUntil(started.is_set)
        opening = workspace.active_editor
        assert workspace.project_for_path(path) is opening
        with pytest.raises(ValueError, match="existing project's tab"):
            workspace.reserve_project_save(other.session.id, path)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert opening.project_path == path


@pytest.mark.parametrize("unsaved", [False, True])
def test_restart_reveals_retained_documents_without_restored_task_history(
    workspace, qtbot, tmp_path, unsaved,
):
    identity = "a" * 32
    project = PackProject(title="Retained")
    path = tmp_path / "retained.cvpack.json"
    workspace.recovery_store = RecoveryStore(tmp_path / "recovery-v2.json")
    workspace.workspace_store = WorkspaceStore(tmp_path / "workspace-v1.json")
    if unsaved:
        workspace.recovery_store.for_session(identity).save(project, None)
    else:
        ProjectStore.save(project, path)
    workspace.workspace_store.save([{
        "id": identity, "path": "" if unsaved else str(path), "hidden": True, "view": {},
    }], None)
    workspace.restore_workspace()
    qtbot.waitUntil(lambda: identity in workspace.editors)
    retained = workspace.editor_for_project(identity)
    assert workspace.tabs.count() >= 1
    assert workspace.tabs.indexOf(retained) >= 0
    assert not retained.session.hidden
    assert workspace.active_editor is retained
    assert not workspace.job_manager.tasks(identity)


def test_source_probe_delivered_after_discard_cannot_resurrect_document(
    workspace, qtbot, tmp_path, monkeypatch,
):
    class PendingProbe(QObject):
        completed = Signal(object)
        failed = Signal(str)

    probe = PendingProbe(workspace)
    original = workspace.job_manager.submit

    def submit(project_id, kind, *args, **kwargs):
        return probe if kind == "probe" else original(project_id, kind, *args, **kwargs)

    workspace.recovery_store = RecoveryStore(tmp_path / "recovery-v2.json")
    editor = workspace.add_project(PackProject(title="Discard me"), dirty=True)
    recovery = editor.recovery_store
    editor._write_recovery_snapshot()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    source = tmp_path / "replacement.mp4"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(workspace.job_manager, "submit", submit)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(source), ""))
    editor.choose_source_video()
    revision = editor.session.revision
    identity = editor.session.id
    workspace.close_project_tab(workspace.tabs.indexOf(editor))
    workspace._decisions[-1].button(QMessageBox.StandardButton.Discard).click()
    probe.completed.emit(SimpleNamespace(duration=2))
    assert editor.project.video_path == ""
    assert editor.session.revision == revision
    assert not editor._recovery_timer.isActive()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert recovery.load() is None
    assert not any(job.kind == "waveform" for job in workspace.job_manager.tasks(identity))


def test_export_action_reopens_hidden_finished_details(workspace, tmp_path):
    from choicer_voicer_pack_creator.ui.export_dialog import ExportProgressDialog

    editor = workspace.active_editor
    dialog = ExportProgressDialog(tmp_path, editor, background=True)
    editor._export_dialog = dialog
    dialog.show()
    dialog.close()
    assert not dialog.isVisible()
    dialog.show_error("Synthetic failure")
    dialog.worker_finished()
    editor.action_export.trigger()
    assert dialog.isVisible()
    assert editor._export_dialog is dialog


def test_keep_processing_hides_tab_not_document_or_job(workspace, qtbot):
    editor = workspace.active_editor
    editor._set_project(PackProject(title="Working", authors=["Author"]), None, True)
    release = threading.Event()

    def scan(context):
        while not release.wait(0.01):
            context.check_cancelled()
        return "ready"

    job = workspace.job_manager.submit(editor.session.id, "analysis", "Scanning", scan)
    qtbot.waitUntil(lambda: job.record.state == "running")
    workspace.close_project_tab(workspace.tabs.indexOf(editor))
    box = workspace._decisions[-1]
    assert box.windowModality() == Qt.WindowModality.NonModal
    keep = next(button for button in box.buttons() if button.text() == "Keep processing")
    qtbot.mouseClick(keep, Qt.MouseButton.LeftButton)
    assert editor.session.hidden
    assert workspace.tabs.indexOf(editor) == -1
    assert editor.session.id in workspace.editors
    assert job.record.active and not job.record.cancel_requested
    release.set()
    qtbot.waitUntil(lambda: job.record.state == "succeeded")
    assert workspace.active_editor is not editor
    workspace.focus_project(editor.session.id)
    assert workspace.active_editor is editor
    assert editor.dirty


def test_per_document_recovery_and_workspace_restore(qtbot, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    recovery = RecoveryStore(tmp_path / "recovery-v2.json")
    window = MainWindow(QuietMedia(), settings=settings, recovery_store=recovery)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    a = window.active_editor
    a._set_project(PackProject(title="Recovery A", authors=["A"]), None, True)
    b = window.add_project(PackProject(title="Recovery B", authors=["B"]), dirty=True)
    a._write_recovery_snapshot()
    b._write_recovery_snapshot()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert a.recovery_store.load().project.title == "Recovery A"
    assert b.recovery_store.load().project.title == "Recovery B"
    a._clear_recovery_snapshot()
    a.dirty = False
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert a.recovery_store.load() is None
    assert b.recovery_store.load().project.title == "Recovery B"
    window.save_workspace_state()
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    restored = MainWindow(QuietMedia(), settings=settings, recovery_store=recovery)
    qtbot.addWidget(restored)
    qtbot.waitUntil(lambda: any(
        editor.project.title == "Recovery B" for editor in restored.editors.values()
    ))
    recovered = restored.editor_for_project(b.session.id)
    assert recovered.dirty
    assert recovered.project.title == "Recovery B"
    for current in (window, restored):
        current.recovery_store = None
        current.workspace_store = None
        for editor in current.editors.values():
            editor._recovery_timer.stop()
            editor.dirty = False
        qtbot.waitUntil(lambda current=current: not current.job_manager.active_jobs())
        current.close()


def test_workspace_view_keeps_independent_video_timeline_sizes(workspace, qtbot):
    first = workspace.add_project(PackProject(title="First"), dirty=False)
    qtbot.waitUntil(lambda: first._layout_restored)
    first.playback_splitter.moveSplitter(200, 1)
    first_view = workspace._view_state(first)
    second = workspace.add_project(PackProject(title="Second"), dirty=False)
    qtbot.waitUntil(lambda: second._layout_restored)
    second.playback_splitter.moveSplitter(300, 1)
    second_view = workspace._view_state(second)
    assert first_view["playback_sizes"] != second_view["playback_sizes"]

    for editor, view in ((first, first_view), (second, second_view)):
        workspace.tabs.setCurrentWidget(editor)
        editor.playback_splitter.moveSplitter(96, 1)
        workspace._restore_view(editor, view)
        assert editor.playback_splitter.sizes() == view["playback_sizes"]
        assert not editor.dirty


def test_managed_worker_cancellation_is_terminal_on_gui_thread(workspace, qtbot):
    threads = []

    class Worker(JobWorker):
        failed = Signal(str)
        canceled = Signal()

        def run(self):
            try:
                while not self.isInterruptionRequested():
                    QThread.msleep(5)
                raise OperationCancelled("Stopped")
            except OperationCancelled:
                self.canceled.emit()

    worker = Worker()
    worker.configure_job(workspace.job_manager, workspace.active_editor.session.id, "scan", "Scan")
    worker.finished.connect(lambda: threads.append(QThread.currentThread()))
    worker.start()
    qtbot.waitUntil(lambda: worker.job_handle.record.state == "running")
    worker.requestInterruption()
    qtbot.waitUntil(lambda: bool(threads))
    assert worker.job_handle.record.state == "cancelled"
    assert threads == [workspace.thread()]
    assert not worker.isRunning()
    assert worker.wait(0)
    assert worker._job_exception is None


@pytest.mark.parametrize("unhandled", [False, True])
def test_waveform_task_failure_preserves_request_identity_and_error(
    workspace, qtbot, tmp_path, unhandled,
):
    class FailedMedia:
        def waveform_peaks(self, *_args, **_kwargs):
            raise RuntimeError("Synthetic waveform failure")

    class FailedWorker(WaveformWorker):
        def run(self):
            raise RuntimeError("Synthetic waveform failure")

    worker_type = FailedWorker if unhandled else WaveformWorker
    source = str(tmp_path / "missing.mp4")
    worker = worker_type(FailedMedia(), 7, source, 1)
    worker.setParent(workspace.active_editor)
    worker.configure_job(
        workspace.job_manager, workspace.active_editor.session.id, "waveform", "Waveform",
    )
    failures = []
    worker.failed.connect(lambda *values: failures.append(values))
    worker.start()
    qtbot.waitUntil(lambda: not worker.isRunning())
    assert worker.job_handle.record.state == "failed"
    assert worker.job_handle.record.error.endswith("Synthetic waveform failure")
    expected = (
        "RuntimeError: Synthetic waveform failure" if unhandled else "Synthetic waveform failure"
    )
    assert failures == [(7, source, expected)]
    assert worker._job_exception is None


@pytest.mark.parametrize("close_workspace", [False, True])
def test_close_keeps_qobjects_alive_until_operation_cleanup_finishes(
    workspace, qtbot, close_workspace,
):
    editor = workspace.active_editor
    cleanup_started, cleanup_release = threading.Event(), threading.Event()
    completed_on = []

    class Worker(JobWorker):
        canceled = Signal()

        def run(self):
            while not self.isInterruptionRequested():
                QThread.msleep(5)
            cleanup_started.set()
            assert cleanup_release.wait(5)
            self.canceled.emit()

    worker = Worker()
    worker.setParent(editor)
    worker.configure_job(workspace.job_manager, editor.session.id, "scan", "Held cleanup")
    worker.finished.connect(lambda: completed_on.append(QThread.currentThread()))
    worker.start()
    qtbot.waitUntil(lambda: worker.job_handle.record.state == "running")
    if close_workspace:
        assert not workspace.close()
        box = workspace._decisions[-1]
        button = box.button(QMessageBox.StandardButton.Yes)
    else:
        workspace.close_project_tab(workspace.tabs.indexOf(editor))
        box = workspace._decisions[-1]
        button = next(b for b in box.buttons() if b.objectName() == "projectCloseCancelTasks")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(cleanup_started.is_set)
    assert workspace.isVisible()
    assert isValid(editor) and isValid(worker) and isValid(workspace.job_manager)
    assert worker.job_handle.record.active
    assert not completed_on
    if not close_workspace:
        workspace.tasks_window.show_action.trigger()
        for row in range(workspace.tasks_window.table.rowCount()):
            if workspace.tasks_window.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == worker.job_handle.id:
                workspace.tasks_window.table.selectRow(row)
                break
        assert not workspace.tasks_window.restore_button.isEnabled()
        workspace.tasks_window._restore_project()
        assert workspace.tabs.indexOf(editor) == -1
    cleanup_release.set()
    qtbot.waitUntil(lambda: bool(completed_on))
    assert completed_on == [workspace.thread()]
    if close_workspace:
        qtbot.waitUntil(lambda: not workspace.isVisible())
        assert not workspace.job_manager.active_jobs()
    else:
        qtbot.waitUntil(lambda: not isValid(editor))
        assert editor.session.id not in workspace.editors
        assert not isValid(worker)
        assert workspace.isVisible()


def test_reopening_cancelled_pending_load_keeps_new_request_identity(
    workspace, qtbot, tmp_path, monkeypatch,
):
    path = tmp_path / "pending.cvpack.json"
    ProjectStore.save(PackProject(title="Saved document"), path)
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []
    original = ProjectStore.load

    def load(source):
        index = len(calls)
        calls.append(source)
        started[index].set()
        assert release[index].wait(5)
        return original(source)

    monkeypatch.setattr(ProjectStore, "load", load)
    workspace.open_path(path)
    old = workspace.active_editor
    qtbot.waitUntil(started[0].is_set)
    assert old.session.loading
    assert not old.action_save.isEnabled()
    assert old.action_open.isEnabled()
    assert not workspace.save_editor(old, destination=path)
    assert original(path).title == "Saved document"
    workspace._decisions[-1].accept()
    workspace.close_project_tab(workspace.tabs.indexOf(old))
    box = workspace._decisions[-1]
    button = next(b for b in box.buttons() if b.objectName() == "projectCloseCancelTasks")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    workspace.open_path(path)
    new = workspace.active_editor
    assert new is not old
    qtbot.waitUntil(started[1].is_set)
    release[0].set()
    qtbot.waitUntil(lambda: old.session.id not in workspace.editors)
    assert workspace._opening_paths[canonical_project_path(path)] == new.session.id
    count = workspace.tabs.count()
    workspace.open_path(path)
    assert workspace.active_editor is new
    assert workspace.tabs.count() == count
    release[1].set()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert new.project.title == "Saved document"
    assert not new.dirty
    assert not new.session.loading and new.action_save.isEnabled()


def test_new_edits_in_discarded_document_require_a_fresh_exit_decision(workspace, qtbot):
    first = workspace.active_editor
    first._set_project(PackProject(title="First dirty"), None, True)
    second = workspace.add_project(PackProject(title="Second dirty"), dirty=True)
    assert not workspace.close()
    box = workspace._decisions[-1]
    assert box.property("projectId") == first.session.id
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Discard), Qt.MouseButton.LeftButton)
    box = workspace._decisions[-1]
    assert box.property("projectId") == second.session.id
    first.title_edit.setText("New edits after discard decision")
    first._commit_editors()
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Discard), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: any(
        decision.property("projectId") == first.session.id for decision in workspace._decisions
    ))
    assert workspace.isVisible()
    box = workspace._decisions[-1]
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
    assert first.dirty
    assert first.project.title == "New edits after discard decision"
    assert not workspace._closing


def test_cancelling_queued_close_save_keeps_workspace_open_and_retryable(
    workspace, qtbot, tmp_path, monkeypatch,
):
    editor = workspace.active_editor
    editor._set_project(PackProject(title="Unsaved"), None, True)
    release = threading.Event()
    blocker = workspace.job_manager.submit(
        editor.session.id, "recovery", "Held recovery", lambda _ctx: release.wait(5),
        resource_class="io", resource_keys=(f"document-save:{editor.session.id}",),
    )
    qtbot.waitUntil(lambda: blocker.record.state == "running")
    destination = tmp_path / "saved.cvpack.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))
    workspace.close()
    box = workspace._decisions[-1]
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Save), Qt.MouseButton.LeftButton)
    save = next(record for record in workspace.job_manager.tasks(editor.session.id) if record.kind == "save")
    assert save.active and save.state != "running"
    workspace.tasks_window.show_action.trigger()
    workspace.tasks_window.table.selectRow(0)
    qtbot.waitUntil(workspace.tasks_window.cancel_button.isVisible)
    qtbot.mouseClick(workspace.tasks_window.cancel_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: workspace.job_manager.handle(save.id).record.state == "cancelled")
    qtbot.waitUntil(lambda: not workspace._closing)
    assert editor.dirty and workspace.isVisible()
    assert not destination.exists()
    release.set()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    workspace.close()
    box = workspace._decisions[-1]
    assert box.property("projectId") == editor.session.id
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)


def test_source_probe_applies_latest_request_only_to_its_own_document(
    workspace, qtbot, tmp_path, monkeypatch,
):
    first = workspace.active_editor
    first._set_project(PackProject(title="A"), None, True)
    older, newer = tmp_path / "older.mp4", tmp_path / "newer.mp4"
    old_started, old_release = threading.Event(), threading.Event()

    class ProbeMedia(QuietMedia):
        def probe(self, path):
            if path == older:
                old_started.set()
                assert old_release.wait(5)
            return SimpleNamespace(
                duration=2, video_codec="h264", audio_codec="aac", pixel_format="yuv420p",
                audio_sample_rate=48000, audio_channels=2, fps=24, height=240,
            )

    first.media = ProbeMedia()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(older), ""))
    first.choose_source_video()
    qtbot.waitUntil(old_started.is_set)
    assert first.project.video_path == ""
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(newer), ""))
    first.choose_source_video()
    second = workspace.add_project(PackProject(title="B"), dirty=False)
    qtbot.waitUntil(lambda: first.project.video_path == str(newer))
    old_release.set()
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert first.project.video_path == str(newer)
    assert second.project.video_path == ""
    assert workspace.active_editor is second


def test_retained_review_tracks_segments_added_while_it_is_open(workspace):
    editor = workspace.active_editor
    dialog = QDialog(editor)
    dialog.existing_segments = 0
    editor._analysis_dialog = dialog
    editor.project.add_segment(Segment(1, 2, "Manual line", ["Actor"]))
    editor._refresh_table()
    assert dialog.existing_segments == 1


def test_backing_completion_keeps_newer_choice_and_never_targets_active_tab(
    workspace, qtbot, tmp_path, monkeypatch,
):
    from choicer_voicer_pack_creator.ui import main_window

    class BackingReview(QDialog):
        def __init__(self, _media, _video, _root, parent, **_kwargs):
            super().__init__(parent)
            self.backing_path = tmp_path / "generated.wav"

    monkeypatch.setattr(main_window, "BackingDialog", BackingReview)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic")
    a = workspace.active_editor
    a._set_project(PackProject(
        title="A", video_path=str(source), video_duration=1,
    ), None, False)
    assert a.generate_backing_track()
    first = a._backing_dialog
    b = workspace.add_project(PackProject(title="B"), dirty=False)
    first.accept()
    assert a.project.backing_track_path == str(tmp_path / "generated.wav")
    assert b.project.backing_track_path == ""
    assert workspace.active_editor is b
    a.project.backing_track_path = ""
    assert a.generate_backing_track()
    second = a._backing_dialog
    chosen = tmp_path / "chosen.wav"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(chosen), ""))
    a.choose_backing_track()
    second.accept()
    assert a.project.backing_track_path == str(chosen)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())


def test_discarded_tab_reopens_saved_contents_not_hidden_edits(
    workspace, qtbot, tmp_path,
):
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    workspace.open_path(path)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    editor = workspace.active_editor
    editor.title_edit.setText("Discard this")
    editor._commit_editors()
    old_id = editor.session.id
    workspace.close_project_tab(workspace.tabs.indexOf(editor))
    box = workspace._decisions[-1]
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Discard), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: old_id not in workspace.editors)
    workspace.open_path(path)
    qtbot.waitUntil(lambda: not workspace.job_manager.active_jobs())
    assert workspace.project.title == "Saved"
    assert workspace.active_editor.session.id != old_id


@pytest.mark.parametrize("invalid", [False, True])
def test_workspace_ignores_single_project_recovery_and_restores_current_snapshots(
    qtbot, tmp_path, invalid,
):
    recovery = RecoveryStore(tmp_path / "recovery-v2.json")
    recovery.save(PackProject(title="Obsolete snapshot"), None)
    recovery.save(PackProject(title="Obsolete edits"), None)
    if invalid:
        recovery.path.write_text("not JSON", encoding="utf-8")
    originals = {path: path.read_bytes() for path in (recovery.path, recovery.previous_path)}
    identity = "abcd1234"
    recovery.for_session(identity).save(PackProject(title="Current edits"), None)
    window = MainWindow(
        QuietMedia(),
        settings=QSettings(str(tmp_path / "recovery.ini"), QSettings.Format.IniFormat),
        recovery_store=recovery, analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: identity in window.editors and not window.job_manager.active_jobs())
    assert window.editors[identity].project.title == "Current edits"
    assert not window._decisions
    assert all(editor.project.title not in {"Obsolete snapshot", "Obsolete edits"}
               for editor in window.editors.values())
    assert all(path.read_bytes() == payload for path, payload in originals.items())
    window.workspace_store = None
    for editor in window.editors.values():
        editor._recovery_timer.stop()
        editor.dirty = False
    window.close()


@pytest.mark.integration
def test_visible_tabs_remain_usable_during_real_ffmpeg_export(qtbot, tmp_path, monkeypatch):
    from choicer_voicer_pack_creator.export_progress import ExportProgress
    from choicer_voicer_pack_creator.exporter import PackExporter
    from choicer_voicer_pack_creator.media import MediaTools
    from choicer_voicer_pack_creator.ui.export_options_dialog import ExportOptionsDialog

    media = MediaTools()
    video, backing = tmp_path / "synthetic.mp4", tmp_path / "backing.wav"
    media.run([
        media.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "3", "-c:v", "mpeg4", "-c:a", "aac", str(video),
    ], "Creating synthetic workspace fixture")
    media.run([
        media.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", "3", str(backing),
    ], "Creating synthetic backing fixture")
    window = MainWindow(
        media, settings=QSettings(str(tmp_path / "native.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    window.show()
    a = window.active_editor
    a._set_project(PackProject(
        title="Synthetic A", authors=["Synthetic fixture"],
        video_path=str(video), video_duration=3, backing_track_path=str(backing),
        video_height=240, video_fps=24, segments=[Segment(0.4, 1.3, "Synthetic line", ["Actor"])],
    ), None, True)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
    released = threading.Event()
    exporter = PackExporter(media)

    class HeldExporter:
        def export(self, project, destination, *, create_zip, progress):
            progress(ExportProgress("Ready for concurrent workspace interaction"))
            assert released.wait(10)
            return exporter.export(project, destination, create_zip=create_zip, progress=progress)

    a.exporter = HeldExporter()
    monkeypatch.setattr(ExportOptionsDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path))
    a.action_export.trigger()
    qtbot.waitUntil(lambda: a._export_worker is not None and any(
        record.kind == "export" and record.state == "running"
        for record in window.job_manager.tasks(a.session.id)
    ))
    b = window.add_project(PackProject(title="B", authors=["Synthetic fixture"]), dirty=False)
    b.title_edit.selectAll()
    qtbot.keyClicks(b.title_edit, "Editable while A exports")
    saved = tmp_path / "B.cvpack.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(saved), ""))
    b.action_save.trigger()
    qtbot.waitUntil(lambda: not b.dirty and saved.is_file())
    c = tmp_path / "C.cvpack.json"
    ProjectStore.save(PackProject(title="Opened C", authors=["Synthetic fixture"]), c)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(c), ""))
    b.action_open.trigger()
    qtbot.waitUntil(lambda: window.active_editor.project.title == "Opened C")
    bar = window.tabs.tabBar()
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(window.tabs.indexOf(a)).center())
    assert window.active_editor is a
    a._export_dialog.close()
    assert not a._export_dialog.isVisible()
    assert a._export_worker.isRunning()
    qtbot.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(window.tabs.indexOf(b)).center())
    assert window.active_editor is b
    assert b.title_edit.text() == "Editable while A exports"
    assert window.grab().save(str(tmp_path / "workspace-during-export.png"))
    released.set()
    qtbot.waitUntil(lambda: a._export_worker is None, timeout=60000)
    exported = next(record for record in window.job_manager.tasks(a.session.id) if record.kind == "export")
    assert exported.state == "succeeded", exported.error
    assert exported.result.pack_path.is_dir()
    assert exported.result.zip_path.is_file()
    assert window.active_editor is b
    for editor in window.editors.values():
        editor.dirty = False
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
