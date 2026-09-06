from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog, QMessageBox

from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.pack_io import ImportResult
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui.main_window import MainWindow


@pytest.fixture
def make_window(qtbot, tmp_path: Path):
    windows: list[MainWindow] = []

    def discard_edits(window: MainWindow) -> None:
        window._commit_editors()
        window.dirty = False

    def create(*, initial_path: Path | None = None) -> MainWindow:
        settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
        window = MainWindow(
            Mock(spec=MediaTools),
            initial_path=initial_path,
            settings=settings,
            analysis_data_root=tmp_path / "analysis",
        )
        windows.append(window)
        qtbot.addWidget(window, before_close_func=discard_edits)
        return window

    return create


def recent_actions(window: MainWindow):
    return [action for action in window.recent_projects_menu.actions() if action.data()]


def test_recent_menu_starts_empty(make_window) -> None:
    window = make_window()
    file_actions = window.menuBar().actions()[0].menu().actions()
    assert file_actions[file_actions.index(window.action_open) + 1].menu() is window.recent_projects_menu
    assert recent_actions(window) == []
    assert window.recent_projects_menu.actions()[0].text() == "No Recent Projects"
    assert not window.recent_projects_menu.actions()[0].isEnabled()
    assert not window.action_clear_recent.isEnabled()


def test_recent_menu_is_disabled_during_busy_operations(make_window, tmp_path: Path) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(), path)
    window.open_path(path)
    window._set_busy(True, "Working")
    assert not window.action_open.isEnabled()
    assert not window.recent_projects_menu.menuAction().isEnabled()
    window._refresh_recent_projects_menu()
    assert not window.recent_projects_menu.menuAction().isEnabled()
    window._set_busy(False, "Ready")
    assert window.action_open.isEnabled()
    assert window.recent_projects_menu.menuAction().isEnabled()
    assert window._recent_project_paths() == [path]


def test_open_history_keeps_ten_unique_projects_newest_first(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    paths = [tmp_path / f"project-{index}.cvpack.json" for index in range(12)]
    for index, path in enumerate(paths):
        ProjectStore.save(PackProject(title=f"Project {index}"), path)
        window.open_path(path)

    expected = list(reversed(paths[-10:]))
    assert window._recent_project_paths() == expected
    assert [action.data() for action in recent_actions(window)] == [str(path) for path in expected]

    monkeypatch.chdir(tmp_path)
    window.open_path(Path(paths[5].name))
    expected.remove(paths[5])
    expected.insert(0, paths[5])
    assert window._recent_project_paths() == expected
    if os.name == "nt":
        window.open_path(Path(str(paths[5]).upper()))
        assert window._recent_project_paths() == expected
    assert len(recent_actions(window)) == 10


def test_recent_projects_persist_and_menu_opens_selected_path(
    make_window, tmp_path: Path
) -> None:
    first = tmp_path / "first.cvpack.json"
    second = tmp_path / "second.cvpack.json"
    ProjectStore.save(PackProject(title="First"), first)
    ProjectStore.save(PackProject(title="Second"), second)
    window = make_window()
    window.open_path(first)
    window.open_path(second)
    window.close()

    restored = make_window()
    assert restored._recent_project_paths() == [second, first]
    recent_actions(restored)[1].trigger()
    assert restored.project.title == "First"
    assert restored.project_path == first
    assert restored._recent_project_paths() == [first, second]
    assert restored.settings.value("lastProjectDir") == str(tmp_path)


def test_initial_project_and_file_dialog_open_are_remembered(
    qtbot, make_window, tmp_path: Path, monkeypatch
) -> None:
    initial = tmp_path / "startup.cvpack.json"
    chosen = tmp_path / "chosen.cvpack.json"
    ProjectStore.save(PackProject(title="Startup"), initial)
    ProjectStore.save(PackProject(title="Chosen"), chosen)
    window = make_window(initial_path=initial)
    qtbot.waitUntil(lambda: window.project_path == initial)
    assert window._recent_project_paths() == [initial]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(chosen), ""))
    window.action_open.trigger()
    assert window._recent_project_paths() == [chosen, initial]


def test_save_and_save_as_remember_the_final_project_paths(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    first = tmp_path / "first.cvpack.json"
    copy = tmp_path / "copy.cvpack.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(first), ""))
    assert window.save_project()
    assert window._recent_project_paths() == [first]
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "copy"), ""))
    assert window.save_project(save_as=True)
    assert window._recent_project_paths() == [copy, first]
    assert window.project_path == copy
    assert first.is_file() and copy.is_file()
    assert window.save_project()
    assert window._recent_project_paths() == [copy, first]
    window.open_path(first)
    window.action_clear_recent.trigger()
    assert window.save_project()
    assert window._recent_project_paths() == [first]


def test_cancelled_open_and_save_do_not_change_history(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    window.open_path(path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: ("", ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: ("", ""))
    window.open_project()
    assert not window.save_project(save_as=True)
    assert window._recent_project_paths() == [path]
    assert window.project_path == path


def test_failed_save_does_not_add_a_recent_project(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    window.open_path(path)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "failed.cvpack.json"), "")
    )
    monkeypatch.setattr(ProjectStore, "save", Mock(side_effect=OSError("Disk full")))
    errors = Mock()
    monkeypatch.setattr(QMessageBox, "critical", errors)
    assert not window.save_project(save_as=True)
    assert window._recent_project_paths() == [path]
    assert window.project_path == path
    errors.assert_called_once_with(window, "Could not save project", "Disk full")


@pytest.mark.parametrize(
    "answer",
    [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Save],
)
def test_recent_open_respects_unsaved_edits(
    make_window, tmp_path: Path, monkeypatch, answer
) -> None:
    window = make_window()
    first = tmp_path / "first.cvpack.json"
    second = tmp_path / "second.cvpack.json"
    ProjectStore.save(PackProject(title="First"), first)
    ProjectStore.save(PackProject(title="Second"), second)
    window.open_path(first)
    window.open_path(second)
    window.title_edit.setText("Unsaved edits")
    prompt = Mock(return_value=answer)
    monkeypatch.setattr(QMessageBox, "warning", prompt)
    recent_actions(window)[1].trigger()
    prompt.assert_called_once()
    assert prompt.call_args.args[1] == "Unsaved changes"
    if answer == QMessageBox.StandardButton.Cancel:
        assert window.project.title == "Unsaved edits"
        assert window.project_path == second
        assert window.dirty
        assert window._recent_project_paths() == [second, first]
    else:
        assert window.project.title == "First"
        assert window.project_path == first
        assert not window.dirty
        assert window._recent_project_paths() == [first, second]
    expected_title = "Unsaved edits" if answer == QMessageBox.StandardButton.Save else "Second"
    assert ProjectStore.load(second).title == expected_title


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_failed_recent_open_keeps_current_project_and_recovery(
    make_window, tmp_path: Path, monkeypatch, failure: str
) -> None:
    window = make_window()
    path = tmp_path / "recent.cvpack.json"
    ProjectStore.save(PackProject(title="Recent"), path)
    window.open_path(path)
    if failure == "missing":
        path.unlink()
    else:
        path.write_text("Not JSON", encoding="utf-8")
    current = PackProject(title="Unsaved")
    window._set_project(current, None, mark_dirty=True)
    window.recovery_store = RecoveryStore(tmp_path / "recovery.json")
    window._write_recovery_snapshot()
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *_args: QMessageBox.StandardButton.Discard
    )
    errors = Mock()
    monkeypatch.setattr(QMessageBox, "critical", errors)
    recent_actions(window)[0].trigger()
    assert window.project is current
    assert window.dirty
    assert window.recovery_store.path.is_file()
    assert window._recent_project_paths() == [path]
    assert errors.call_args.args[1] == "Could not open project"


def test_recent_open_stops_when_saving_edits_fails(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    first = tmp_path / "first.cvpack.json"
    second = tmp_path / "second.cvpack.json"
    ProjectStore.save(PackProject(title="First"), first)
    ProjectStore.save(PackProject(title="Second"), second)
    window.open_path(first)
    window.open_path(second)
    window.title_edit.setText("Unsaved edits")
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(ProjectStore, "save", Mock(side_effect=OSError("Disk full")))
    monkeypatch.setattr(QMessageBox, "critical", Mock())
    recent_actions(window)[1].trigger()
    assert window.project_path == second
    assert window.project.title == "Unsaved edits"
    assert window.dirty
    assert window._recent_project_paths() == [second, first]


def test_menu_distinguishes_same_filenames_and_escapes_ampersands(
    make_window, tmp_path: Path
) -> None:
    window = make_window()
    first = tmp_path / "A&B" / "same&name.cvpack.json"
    second = tmp_path / "other" / first.name
    for path in (first, second):
        ProjectStore.save(PackProject(), path)
        window.open_path(path)
    actions = recent_actions(window)
    assert actions[0].text() != actions[1].text()
    for action, path in zip(actions, [second, first], strict=True):
        assert path.name.replace("&", "&&") in action.text()
        assert str(path.parent).replace("&", "&&") in action.text()
        assert action.toolTip() == str(path)
        assert action.statusTip() == str(path)


def test_clear_history_persists_without_changing_project_files(
    make_window, tmp_path: Path
) -> None:
    window = make_window()
    path = tmp_path / "saved.cvpack.json"
    ProjectStore.save(PackProject(title="Saved"), path)
    window.open_path(path)
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
def test_imported_packs_are_not_recent_projects(
    make_window, tmp_path: Path, monkeypatch, source_name: str
) -> None:
    window = make_window()
    source = tmp_path / source_name
    if not source.suffix:
        source.mkdir()
    result = ImportResult(project=PackProject(title="Imported"), warnings=[])
    monkeypatch.setattr(window.importer, "import_folder", lambda *_args: result)
    monkeypatch.setattr(window.importer, "import_zip", lambda *_args: result)
    window.open_path(source)
    assert window.project.title == "Imported"
    assert window.dirty
    assert window._recent_project_paths() == []


def test_settings_write_failure_is_reported(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    window = make_window()
    monkeypatch.setattr(window.settings, "status", lambda: QSettings.Status.AccessError)
    warning = Mock()
    monkeypatch.setattr(QMessageBox, "warning", warning)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "saved.cvpack.json"), "")
    )
    assert window.save_project()
    assert window.project_path.is_file()
    assert not window.dirty
    assert warning.call_args.args[1] == "Could not save recent projects"
    assert "Project files are not affected" in warning.call_args.args[2]
