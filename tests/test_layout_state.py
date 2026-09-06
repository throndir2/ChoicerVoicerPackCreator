from __future__ import annotations

import subprocess
import sys
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtWidgets import QApplication

from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.ui.layout_state import DEFAULT_WINDOW_SIZE
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


@pytest.fixture
def make_window(qtbot, tmp_path):
    def cleanup(window):
        for box in list(window._decisions):
            box.reject()
        for editor in window.editors.values():
            editor._commit_editors()
            editor.dirty = False
            editor._recovery_timer.stop()

    def create(name="settings.ini", *, show=True):
        window = MainWindow(
            Mock(spec=MediaTools),
            settings=QSettings(str(tmp_path / name), QSettings.Format.IniFormat),
            analysis_data_root=tmp_path / "analysis",
        )
        qtbot.addWidget(window, before_close_func=cleanup)
        window.setStyleSheet(APP_STYLESHEET)
        if show:
            window.show()
            qtbot.waitUntil(lambda: window.active_editor._layout_restored)
        return window

    return create


def assert_sizes(actual, expected):
    assert len(actual) == len(expected)
    assert all(abs(a - b) <= 2 for a, b in zip(actual, expected, strict=True)), (
        actual, expected,
    )


def test_shared_layout_follows_existing_and_new_tabs_before_debounce(make_window, qtbot):
    window = make_window()
    first = window.add_project(PackProject(title="First"), dirty=False)
    qtbot.waitUntil(lambda: first._layout_restored)
    second = window.add_project(PackProject(title="Second"), dirty=False)
    qtbot.waitUntil(lambda: second._layout_restored)
    window.tabs.setCurrentWidget(first)
    qtbot.waitUntil(lambda: first._layout_restored)
    first.editor_splitter.moveSplitter(320, 1)
    first.playback_splitter.moveSplitter(210, 1)
    first.project_section.set_collapsed(True)
    qtbot.waitUntil(lambda: first.inspector_splitter.sizes()[0] < 60)
    expected = first.editor_splitter.sizes()
    expected_playback = first.playback_splitter.sizes()
    window.tabs.setCurrentWidget(second)
    qtbot.waitUntil(lambda: second._layout_restored)
    assert_sizes(second.editor_splitter.sizes(), expected)
    assert_sizes(second.playback_splitter.sizes(), expected_playback)
    assert second.project_section.is_collapsed
    second.editor_splitter.moveSplitter(180, 1)
    expected = second.editor_splitter.sizes()
    third = window.add_project(PackProject(title="Third"), dirty=False)
    qtbot.waitUntil(lambda: third._layout_restored)
    assert_sizes(third.editor_splitter.sizes(), expected)
    assert_sizes(third.playback_splitter.sizes(), expected_playback)
    assert third.project_section.is_collapsed
    window.tabs.setCurrentWidget(first)
    qtbot.waitUntil(lambda: first._layout_restored)
    assert_sizes(first.editor_splitter.sizes(), expected)
    assert not any(editor.dirty for editor in window.editors.values())


@pytest.mark.parametrize("left_width", [260, 0], ids=["resized", "collapsed"])
def test_restart_keeps_active_layout_not_last_created_tab(make_window, qtbot, left_width):
    window = make_window()
    window.resize(window.minimumSize())
    first = window.add_project(PackProject(title="First"), dirty=False)
    qtbot.waitUntil(lambda: first._layout_restored)
    second = window.add_project(PackProject(title="Last created"), dirty=False)
    qtbot.waitUntil(lambda: second._layout_restored)
    second.editor_splitter.moveSplitter(500, 1)
    window.tabs.setCurrentWidget(first)
    qtbot.waitUntil(lambda: first._layout_restored)
    first._layout_save_timer.setInterval(10_000)
    window._window_layout_timer.setInterval(10_000)
    first.editor_splitter.moveSplitter(left_width, 1)
    first.playback_splitter.moveSplitter(150, 1)
    first.selected_section.set_collapsed(True)
    qtbot.waitUntil(lambda: first.inspector_splitter.sizes()[2] < 60)
    expected_heights = first.inspector_splitter.sizes()
    expected_playback = first.playback_splitter.sizes()
    expanded_height = first.selected_section.last_expanded_height
    assert first._layout_save_timer.isActive()
    window.close()

    restored = make_window()
    assert abs(restored.editor_splitter.sizes()[0] - left_width) <= 2
    assert_sizes(restored.inspector_splitter.sizes(), expected_heights)
    assert_sizes(restored.playback_splitter.sizes(), expected_playback)
    assert restored.selected_section.is_collapsed
    assert restored.selected_section.last_expanded_height == expanded_height


@pytest.mark.parametrize("maximized", [False, True])
def test_window_geometry_and_maximized_state_survive_restart(make_window, qtbot, maximized):
    window = make_window()
    window.resize(1050, 700)
    window.move(0, 35)
    qtbot.wait(20)
    normal_size = window.size()
    if maximized:
        window.showMaximized()
        qtbot.waitUntil(window.isMaximized)
    window.close()

    restored = make_window()
    assert restored.isMaximized() == maximized
    assert restored.normalGeometry().size().expandedTo(restored.minimumSize()) == normal_size
    if maximized and QApplication.platformName() != "offscreen":
        restored.showNormal()
        qtbot.waitUntil(lambda: restored.size() == normal_size)
    if not maximized:
        assert abs(restored.y() - 35) <= 4


def test_layout_autosaves_without_closing(make_window, qtbot):
    window = make_window()
    window.editor_splitter.moveSplitter(250, 1)
    window.project_section.set_collapsed(True)
    window.move(0, 40)
    qtbot.waitUntil(lambda: (
        not window.active_editor._layout_save_timer.isActive()
        and not window._window_layout_timer.isActive()
    ))
    saved = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
    assert saved.value("layout/packDetailsCollapsedV1", type=bool)
    assert isinstance(saved.value("layout/editorSplitterV1"), QByteArray)
    assert isinstance(saved.value("layout/windowGeometryV1"), QByteArray)


@pytest.mark.parametrize("close_immediately", [False, True])
def test_reset_is_shared_persistent_and_does_not_change_projects_or_preferences(
    make_window, qtbot, close_immediately,
):
    window = make_window()
    first = window.add_project(PackProject(title="Keep my edits"), dirty=True)
    qtbot.waitUntil(lambda: first._layout_restored)
    second = window.add_project(PackProject(title="Keep saved project"), dirty=False)
    qtbot.waitUntil(lambda: second._layout_restored)
    window.settings.setValue("recentProjects", ["saved.cvpack.json"])
    window.settings.setValue("lastProjectDir", "keep-directory")
    window.settings.setValue("updates/checkOnStartup", False)
    originals = {
        editor.session.id: (editor.project.to_dict(), editor.dirty, editor.session.revision)
        for editor in window.editors.values()
    }
    window.showMaximized()
    second.editor_splitter.moveSplitter(0, 1)
    second.playback_splitter.moveSplitter(96, 1)
    for section in second.inspector_sections:
        section.set_collapsed(True)
    window.action_reset_layout.trigger()
    assert window.action_reset_layout in window.view_menu.actions()
    assert not window.isMaximized()
    assert window.width() <= max(DEFAULT_WINDOW_SIZE[0], window.minimumWidth())
    for editor in window.editors.values():
        assert (editor.project.to_dict(), editor.dirty, editor.session.revision) == (
            originals[editor.session.id]
        )
    if not close_immediately:
        for editor in (first, second):
            window.tabs.setCurrentWidget(editor)
            qtbot.waitUntil(lambda editor=editor: editor._layout_restored)
            assert not any(section.is_collapsed for section in editor.inspector_sections)
            assert editor.editor_splitter.sizes()[0] > 0
    for editor in window.editors.values():
        editor.dirty = False
    window.close()

    restored = make_window()
    reference = make_window("fresh.ini")
    reference.resize(restored.size())
    qtbot.wait(20)
    assert_sizes(restored.editor_splitter.sizes(), reference.editor_splitter.sizes())
    assert_sizes(restored.inspector_splitter.sizes(), reference.inspector_splitter.sizes())
    assert_sizes(restored.playback_splitter.sizes(), reference.playback_splitter.sizes())
    assert not any(section.is_collapsed for section in restored.inspector_sections)
    assert restored.settings.value("recentProjects") == ["saved.cvpack.json"]
    assert restored.settings.value("lastProjectDir") == "keep-directory"
    assert not restored.settings.value("updates/checkOnStartup", type=bool)


def test_workspace_view_cannot_override_shared_panes(make_window, qtbot):
    window = make_window()
    editor = window.active_editor
    editor._set_project(PackProject(video_duration=10), None, mark_dirty=False)
    editor.editor_splitter.moveSplitter(210, 1)
    editor.project_section.set_collapsed(True)
    qtbot.waitUntil(lambda: editor.inspector_splitter.sizes()[0] < 60)
    splitters = (editor.editor_splitter, editor.inspector_splitter, editor.playback_splitter)
    before = [splitter.sizes() for splitter in splitters]
    window._restore_view(editor, {
        "editor_sizes": [900, 300], "inspector_sizes": [100, 100, 700],
        "playback_sizes": [100, 500],
        "zoom": 25, "mark_in": 1, "mark_out": 4,
    })
    assert [splitter.sizes() for splitter in splitters] == before
    assert editor.project_section.is_collapsed
    assert editor.zoom_slider.value() == 25
    view = window._view_state(editor)
    assert "editor_sizes" not in view and "inspector_sizes" not in view
    assert "playback_sizes" not in view
    assert view["mark_in"] == 1 and view["mark_out"] == 4


@pytest.mark.parametrize("invalid", [42, "not a Qt state", QByteArray(b"bad state")])
def test_invalid_saved_layout_uses_usable_defaults(make_window, tmp_path, invalid):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    for key in (
        "editorSplitterV1", "inspectorSplitterV1", "playbackSplitterV1", "windowGeometryV1",
    ):
        settings.setValue(f"layout/{key}", invalid)
    settings.setValue("layout/packDetailsExpandedHeightV1", "not a height")
    settings.sync()
    window = make_window()
    assert window.editor_splitter.sizes()[0] > 0
    assert window.editor_splitter.handleWidth() == 1
    assert not window.inspector_splitter.childrenCollapsible()
    assert not any(section.is_collapsed for section in window.inspector_sections)


def test_layout_write_failure_is_reported_once_and_remains_shared(
    make_window, qtbot, monkeypatch,
):
    window = make_window()
    notices = []
    monkeypatch.setattr(window, "notice", lambda *args: notices.append(args))
    monkeypatch.setattr(window.settings, "status", lambda: QSettings.Status.AccessError)
    window.editor_splitter.moveSplitter(240, 1)
    window.active_editor._save_layout_state()
    other = window.add_project(PackProject(title="New tab"), dirty=False)
    qtbot.waitUntil(lambda: other._layout_restored)
    assert abs(other.editor_splitter.sizes()[0] - 240) <= 2
    other._save_layout_state()
    assert len(notices) == 1
    assert notices[0][0] == "Could not save UI layout"
    assert window.settings.fileName() in notices[0][1]
    monkeypatch.setattr(window.settings, "status", lambda: QSettings.Status.NoError)
    other._save_layout_state()
    assert not window._layout_write_failed


def test_layout_survives_a_real_process_restart(tmp_path):
    code = """
import sys
from pathlib import Path
from unittest.mock import Mock
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.ui.main_window import MainWindow
app = QApplication([])
root = Path(sys.argv[1])
window = MainWindow(
    Mock(spec=MediaTools),
    settings=QSettings(str(root / "process.ini"), QSettings.Format.IniFormat),
    analysis_data_root=root / "analysis",
)
if sys.argv[2] == "save":
    window.resize(1050, 700)
window.show()
for _ in range(5):
    app.processEvents()
if sys.argv[2] == "save":
    window.project_section.set_collapsed(True)
    window.move(0, 35)
    for _ in range(5):
        app.processEvents()
    window.editor_splitter.moveSplitter(240, 1)
else:
    assert abs(window.editor_splitter.sizes()[0] - 240) <= 2
    assert window.project_section.is_collapsed
    assert window.size().width() == 1050 and window.size().height() == 700
window.close()
for _ in range(5):
    app.processEvents()
assert window._close_approved
"""
    for stage in ("save", "restore"):
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), stage],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
