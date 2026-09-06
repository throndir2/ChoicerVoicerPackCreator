from __future__ import annotations

import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog, QMessageBox

from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.pack_io import ImportResult, PackImporter
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui.main_window import MainWindow


@pytest.fixture
def wait_for_jobs(qtbot):
    def wait(window: MainWindow) -> None:
        # Startup and completion callbacks can schedule another job on the next Qt turn.
        qtbot.wait(1)
        qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)

    return wait


@pytest.fixture
def make_window(qtbot, tmp_path: Path, wait_for_jobs):
    def discard_edits(window: MainWindow) -> None:
        wait_for_jobs(window)
        for editor in window.editors.values():
            editor._commit_editors()
            editor.dirty = False
            editor._recovery_timer.stop()
        wait_for_jobs(window)
        for decision in list(window._decisions):
            decision.reject()

    def create(*, initial_path: Path | None = None) -> MainWindow:
        settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        window = MainWindow(
            Mock(spec=MediaTools),
            initial_path=initial_path,
            settings=settings,
            analysis_data_root=tmp_path / "analysis",
        )
        qtbot.addWidget(window, before_close_func=discard_edits)
        return window

    return create


def recent_actions(window: MainWindow):
    window.recent_projects_menu.aboutToShow.emit()
    return [action for action in window.recent_projects_menu.actions() if action.data()]


def test_recent_menu_starts_empty(make_window) -> None:
    window = make_window()
    file_actions = window.menuBar().actions()[0].menu().actions()
    assert file_actions[file_actions.index(window.action_open) + 1].menu() is window.recent_projects_menu
    assert recent_actions(window) == []
    assert window.recent_projects_menu.actions()[0].text() == "No Recent Projects"
    assert not window.recent_projects_menu.actions()[0].isEnabled()
    assert not window.action_clear_recent.isEnabled()


def test_recent_menu_stays_enabled_during_background_open(
    qtbot, make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    saved = tmp_path / "saved.cvpack.json"
    pending = tmp_path / "pending.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), saved)
    ProjectStore.save(PackProject(title="Pending"), pending)
    window.open_path(saved)
    wait_for_jobs(window)
    original = window.active_editor
    started, release = Event(), Event()
    load = ProjectStore.load

    def blocked_load(path):
        started.set()
        if not release.wait(10):
            raise TimeoutError("Test did not release project loading")
        return load(path)

    monkeypatch.setattr(ProjectStore, "load", blocked_load)
    try:
        window.open_path(pending)
        loading = window.active_editor
        assert loading is not original
        assert loading.session.loading
        assert window.job_manager.active_jobs()
        assert window._recent_project_paths() == [saved]
        qtbot.waitUntil(started.is_set)
        loading._set_busy(True, "Working")
        for editor in (original, loading):
            assert editor.action_new.isEnabled()
            assert editor.action_open.isEnabled()
            assert editor.action_import.isEnabled()
            assert editor.recent_projects_menu.menuAction().isEnabled()
            editor._refresh_recent_projects_menu()
            assert editor.recent_projects_menu.menuAction().isEnabled()
        recent_actions(window)[0].trigger()
        assert window.active_editor is original
        assert window.job_manager.active_jobs()
        original.title_edit.setText("Still editable")
        original._commit_editors()
        assert original.dirty
    finally:
        release.set()
        wait_for_jobs(window)
    assert window.active_editor is original
    assert original.project.title == "Still editable"
    assert loading.project.title == "Pending"
    assert not loading.session.loading
    assert window._recent_project_paths() == [pending, saved]


def test_open_history_keeps_ten_unique_projects_newest_first(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    paths = [tmp_path / f"project-{index}.cvpack.json" for index in range(12)]
    for index, path in enumerate(paths):
        ProjectStore.save(PackProject(title=f"Project {index}"), path)
        window.open_path(path)
        wait_for_jobs(window)

    expected = list(reversed(paths[-10:]))
    assert window._recent_project_paths() == expected
    assert [action.data() for action in recent_actions(window)] == [str(path) for path in expected]

    monkeypatch.chdir(tmp_path)
    window.open_path(Path(paths[5].name))
    wait_for_jobs(window)
    expected.remove(paths[5])
    expected.insert(0, paths[5])
    assert window._recent_project_paths() == expected
    if os.name == "nt":
        window.open_path(Path(str(paths[5]).upper()))
        wait_for_jobs(window)
        assert window._recent_project_paths() == expected
    assert len(recent_actions(window)) == 10
    assert len(window.editors) == len(paths)
    assert make_window()._recent_project_paths() == expected


def test_recent_projects_persist_and_menu_opens_selected_path(
    make_window, wait_for_jobs, tmp_path: Path
) -> None:
    first = tmp_path / "first.cvpack.json"
    second = tmp_path / "second.cvpack.json"
    ProjectStore.save(PackProject(title="First"), first)
    ProjectStore.save(PackProject(title="Second"), second)
    window = make_window()
    window.open_path(first)
    wait_for_jobs(window)
    window.open_path(second)
    wait_for_jobs(window)
    window.close()

    restored = make_window()
    assert restored._recent_project_paths() == [second, first]
    recent_actions(restored)[1].trigger()
    assert restored.job_manager.active_jobs()
    wait_for_jobs(restored)
    assert restored.project.title == "First"
    assert restored.project_path == first
    assert restored._recent_project_paths() == [first, second]
    assert restored.settings.value("lastProjectDir") == str(tmp_path)


def test_initial_project_and_file_dialog_open_are_remembered(
    qtbot, make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    initial = tmp_path / "startup.cvpack.json"
    chosen = tmp_path / "chosen.cvpack.json"
    ProjectStore.save(PackProject(title="Startup"), initial)
    ProjectStore.save(PackProject(title="Chosen"), chosen)
    window = make_window(initial_path=initial)
    qtbot.waitUntil(lambda: window.project_path == initial)
    wait_for_jobs(window)
    startup = window.active_editor
    assert window._recent_project_paths() == [initial]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(chosen), ""))
    window.action_open.trigger()
    assert window.active_editor is not startup
    assert window.active_editor.session.loading
    assert window._recent_project_paths() == [initial]
    wait_for_jobs(window)
    assert window._recent_project_paths() == [chosen, initial]
    assert startup.project.title == "Startup"


def test_save_and_save_as_remember_the_final_project_paths(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    first = tmp_path / "first.cvpack.json"
    copy = tmp_path / "copy.cvpack.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(first), ""))
    assert window.save_project()
    assert window.job_manager.active_jobs()
    assert window._recent_project_paths() == []
    wait_for_jobs(window)
    assert window._recent_project_paths() == [first]
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "copy"), ""))
    assert window.save_project(save_as=True)
    assert window.project_path == first
    assert window._recent_project_paths() == [first]
    wait_for_jobs(window)
    assert window._recent_project_paths() == [copy, first]
    assert window.project_path == copy
    assert first.is_file() and copy.is_file()
    assert window.save_project()
    wait_for_jobs(window)
    assert window._recent_project_paths() == [copy, first]
    window.open_path(first)
    wait_for_jobs(window)
    window.action_clear_recent.trigger()
    assert window.save_project()
    assert window._recent_project_paths() == []
    wait_for_jobs(window)
    assert window._recent_project_paths() == [first]


@pytest.mark.parametrize("save_as", [False, True])
def test_save_suggests_safe_filename_without_renaming_the_project(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch, save_as: bool,
) -> None:
    window = make_window()
    original = tmp_path / "original.cvpack.json"
    if save_as:
        ProjectStore.save(PackProject(title="Original"), original)
        window.open_path(original)
        wait_for_jobs(window)
    window.settings.setValue("lastProjectDir", str(tmp_path))
    title = r"../A: video\clip?"
    window.title_edit.setText(title)
    destination = tmp_path / "..A videoclip.cvpack.json"
    suggestions = []

    def choose_destination(_parent, _caption, suggested, _filter):
        suggestions.append(suggested)
        return suggested, ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_destination)
    assert window.save_project(save_as=save_as)
    wait_for_jobs(window)
    assert suggestions == [str(destination)]
    assert window.project_path == destination
    assert window.project.title == title
    assert window.title_edit.text() == title
    assert ProjectStore.load(destination).title == title
    if save_as:
        assert ProjectStore.load(original).title == "Original"

    window.title_edit.setText("New: title?")
    assert window.save_project()
    wait_for_jobs(window)
    assert suggestions == [str(destination)]
    assert window.project_path == destination
    assert ProjectStore.load(destination).title == "New: title?"


def test_save_remembers_completed_snapshot_without_clearing_newer_edits(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(path), ""))
    window.title_edit.setText("Saved revision")
    assert window.save_project()
    assert window.job_manager.active_jobs()
    window.title_edit.setText("Newer edits")
    window._commit_editors()
    wait_for_jobs(window)
    assert ProjectStore.load(path).title == "Saved revision"
    assert window.project.title == "Newer edits"
    assert window.dirty
    assert window.project_path == path
    assert window._recent_project_paths() == [path]


def test_cancelled_open_and_save_do_not_change_history(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    window.open_path(path)
    wait_for_jobs(window)
    editor = window.active_editor
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: ("", ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: ("", ""))
    window.open_project()
    window.action_new.trigger()
    assert not window.save_project(save_as=True)
    wait_for_jobs(window)
    assert window.active_editor is editor
    assert len(window.editors) == 1
    assert window._recent_project_paths() == [path]
    assert window.project_path == path


def test_failed_save_does_not_add_a_recent_project(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    failed_path = tmp_path / "failed.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    window.open_path(path)
    wait_for_jobs(window)
    window.title_edit.setText("Unsaved edits")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(failed_path), ""))
    monkeypatch.setattr(ProjectStore, "save", Mock(side_effect=OSError("Disk full")))
    assert window.save_project(save_as=True)
    assert window.job_manager.active_jobs()
    wait_for_jobs(window)
    assert window._recent_project_paths() == [path]
    assert window.project_path == path
    assert window.project.title == "Unsaved edits"
    assert window.dirty
    assert not failed_path.exists()
    assert ProjectStore.load(path).title == "Saved"
    assert len(window._decisions) == 1
    assert window._decisions[0].windowTitle() == "Could not save project"
    assert window._decisions[0].text() == "OSError: Disk full"


@pytest.mark.parametrize("action", ["recent", "open", "new"])
def test_open_and_new_preserve_other_dirty_tabs_without_prompting(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch, action: str
) -> None:
    window = make_window()
    first = tmp_path / "first.cvpack.json"
    second = tmp_path / "second.cvpack.json"
    source = tmp_path / "source.mp4"
    ProjectStore.save(PackProject(title="First"), first)
    ProjectStore.save(PackProject(title="Second"), second)
    window.open_path(first)
    wait_for_jobs(window)
    first_editor = window.active_editor
    window.open_path(second)
    wait_for_jobs(window)
    dirty_editor = window.active_editor
    dirty_editor.title_edit.setText("Unsaved edits")
    prompt = Mock()
    monkeypatch.setattr(QMessageBox, "warning", prompt)
    save = Mock(side_effect=OSError("Disk full"))
    monkeypatch.setattr(ProjectStore, "save", save)

    if action == "recent":
        recent_actions(window)[1].trigger()
    elif action == "open":
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(first), ""))
        window.action_open.trigger()
    else:
        window.media.probe.return_value = SimpleNamespace(duration=4.0)
        window.new_from_video(source, auto_process=False)
        assert window.active_editor.session.loading
        assert window.job_manager.active_jobs()
    wait_for_jobs(window)

    prompt.assert_not_called()
    save.assert_not_called()
    assert not window._decisions
    assert window.active_editor is not dirty_editor
    assert dirty_editor.project.title == "Unsaved edits"
    assert dirty_editor.project_path == second
    assert dirty_editor.dirty
    assert ProjectStore.load(second).title == "Second"
    if action == "new":
        assert window.active_editor is not first_editor
        assert len(window.editors) == 3
        assert window.project.video_path == str(source)
        assert window.dirty
        assert window._recent_project_paths() == [second, first]
    else:
        assert window.active_editor is first_editor
        assert len(window.editors) == 2
        assert not window.dirty
        assert window._recent_project_paths() == [first, second]


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
@pytest.mark.parametrize("action", ["recent", "dialog"])
def test_failed_open_retains_other_project_and_recovery(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch, failure: str, action: str
) -> None:
    window = make_window()
    path = tmp_path / "recent.cvpack.json"
    ProjectStore.save(PackProject(title="Recent"), path)
    window.open_path(path)
    wait_for_jobs(window)
    recent_editor = window.active_editor
    current = PackProject(title="Unsaved")
    original = window.add_project(current, dirty=True)
    window.close_project_tab(window.tabs.indexOf(recent_editor))
    wait_for_jobs(window)
    original.recovery_store = RecoveryStore(tmp_path / "recovery.json")
    original._write_recovery_snapshot()
    wait_for_jobs(window)
    recovery = original.recovery_store.path.read_bytes()
    if failure == "missing":
        path.unlink()
    else:
        path.write_text("Not JSON", encoding="utf-8")

    if action == "recent":
        recent_actions(window)[0].trigger()
    else:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(path), ""))
        window.action_open.trigger()
    failed = window.active_editor
    assert failed is not original
    assert failed.session.loading
    assert window.job_manager.active_jobs()
    wait_for_jobs(window)
    assert failed.session.attention
    assert not failed.session.loading
    assert failed.project_path is None
    assert window.editors[original.session.id] is original
    assert original.project is current
    assert original.dirty
    assert original.recovery_store.path.read_bytes() == recovery
    assert window._recent_project_paths() == [path]
    assert window._decisions[0].windowTitle() == "Could not open project"
    window.focus_project(original.session.id)
    assert window.project is current
    assert window.dirty


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_recent_open_focuses_existing_tab_without_rereading(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch, failure: str
) -> None:
    window = make_window()
    path = tmp_path / "recent.cvpack.json"
    ProjectStore.save(PackProject(title="Recent"), path)
    window.open_path(path)
    wait_for_jobs(window)
    existing = window.active_editor
    existing.title_edit.setText("In-memory edits")
    other = window.add_project(PackProject(title="Other"), dirty=True)
    if failure == "missing":
        path.unlink()
    else:
        path.write_text("Not JSON", encoding="utf-8")
    load = Mock(side_effect=AssertionError("An open project must not be reread"))
    monkeypatch.setattr(ProjectStore, "load", load)
    recent_actions(window)[0].trigger()
    wait_for_jobs(window)
    load.assert_not_called()
    assert window.active_editor is existing
    assert window.project.title == "In-memory edits"
    assert window.project_path == path
    assert window.dirty
    assert other.dirty
    assert len(window.editors) == 2
    assert window._recent_project_paths() == [path]
    assert not window._decisions


def test_menu_distinguishes_same_filenames_and_escapes_ampersands(
    make_window, wait_for_jobs, tmp_path: Path
) -> None:
    window = make_window()
    first = tmp_path / "A&B" / "same&name.cvpack.json"
    second = tmp_path / "other" / first.name
    for path in (first, second):
        ProjectStore.save(PackProject(), path)
        window.open_path(path)
        wait_for_jobs(window)
    actions = recent_actions(window)
    assert actions[0].text() != actions[1].text()
    for action, path in zip(actions, [second, first], strict=True):
        assert path.name.replace("&", "&&") in action.text()
        assert str(path.parent).replace("&", "&&") in action.text()
        assert action.toolTip() == str(path)
        assert action.statusTip() == str(path)


def test_clear_history_persists_without_changing_project_files(
    make_window, wait_for_jobs, tmp_path: Path
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    window.open_path(path)
    wait_for_jobs(window)
    original = path.read_bytes()
    window.title_edit.setText("Unsaved edits")
    window._commit_editors()
    window.action_clear_recent.trigger()
    assert window.project_path == path
    assert window.project.title == "Unsaved edits"
    assert window.dirty
    assert path.read_bytes() == original
    assert not recent_actions(window)
    assert not window.action_clear_recent.isEnabled()
    assert not recent_actions(make_window())


@pytest.mark.parametrize("source_name", ["pack", "pack.zip"])
def test_imported_packs_open_new_tabs_without_changing_recent_projects(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch, source_name: str
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    window.open_path(path)
    wait_for_jobs(window)
    original = window.active_editor
    original.title_edit.setText("Unsaved edits")
    source = tmp_path / source_name
    if not source.suffix:
        source.mkdir()
    result = ImportResult(project=PackProject(title="Imported"), warnings=[])
    monkeypatch.setattr(PackImporter, "import_folder", lambda *_args: result)
    monkeypatch.setattr(PackImporter, "import_zip", lambda *_args: result)
    if source.suffix:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(source), ""))
        window.import_pack_zip()
    else:
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(source))
        window.import_pack()
    assert window.active_editor is not original
    assert window.active_editor.session.loading
    wait_for_jobs(window)
    assert window.project.title == "Imported"
    assert window.project_path is None
    assert window.dirty
    assert original.project.title == "Unsaved edits"
    assert original.dirty
    assert len(window.editors) == 2
    assert not window._decisions
    assert window._recent_project_paths() == [path]


def test_settings_write_failure_is_reported(
    make_window, wait_for_jobs, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    monkeypatch.setattr(window.settings, "status", lambda: QSettings.Status.AccessError)
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "saved.cvpack.json"), "")
    )
    assert window.save_project()
    wait_for_jobs(window)
    assert window.project_path.is_file()
    assert not window.dirty
    assert warning.call_args.args[1] == "Could not save recent projects"
    assert "Project files are not affected" in warning.call_args.args[2]
