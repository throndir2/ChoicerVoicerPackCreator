from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QColor, QDesktopServices, QKeyEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QMessageBox, QPushButton
from shiboken6 import isValid

from choicer_voicer_pack_creator import __version__
from choicer_voicer_pack_creator.exporter import ExportResult
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui.about_dialog import AboutDialog
from choicer_voicer_pack_creator.ui.main_window import (
    ExportWorker,
    MainWindow,
    ProjectEditor,
    WaveformWorker,
)
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget


class UnusedMedia:
    def probe_audio_duration(self, _path: Path) -> float:
        return 1.75


@pytest.fixture(autouse=True)
def drain_workspace_tasks(qtbot):
    yield
    windows = [
        widget for widget in QApplication.topLevelWidgets() if isinstance(widget, MainWindow)
    ]
    for window in windows:
        if not isValid(window):
            continue
        for editor in window.editors.values():
            editor._recovery_timer.stop()
        manager = window.job_manager
        for job in manager.active_jobs():
            manager.cancel(job.id)
        qtbot.waitUntil(
            lambda manager=manager: not isValid(manager) or not manager.active_jobs(),
            timeout=10000,
        )
        if isValid(manager):
            manager.shutdown(cancel=False, wait=True)


def test_main_window_starts_with_empty_editor(qtbot) -> None:
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.show()
    assert window.project.title == "Untitled Dub Pack"
    assert window.segment_table.rowCount() == 0
    assert "Choicer Voicer Pack Creator" in window.windowTitle()
    help_actions = window.menuBar().actions()[-1].menu().actions()
    assert window.updater.check_action in help_actions
    assert window.action_logs in help_actions
    window.dirty = False
    window.close()


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_support_button_only_appears_in_help_about(
    qtbot, tmp_path: Path, monkeypatch, stylesheet: str
) -> None:
    urls: list[str] = []

    def open_url(url: QUrl) -> bool:
        urls.append(url.toString())
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(open_url))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.setStyleSheet(stylesheet)
    window.resize(window.minimumSize())
    window.show()
    assert window.tabs.cornerWidget(Qt.Corner.TopRightCorner) is None
    assert window.tabs.tabBar().styleSheet() == ""
    assert not window.findChildren(QPushButton, "coffeeSupport")
    assert window.action_about in window.help_menu.actions()
    assert urls == []
    original = window.active_editor
    original._set_project(PackProject(title="First pack"), None, mark_dirty=False)
    before = original.project.to_dict()
    another = window.add_project(PackProject(title="Another pack"), dirty=False)

    def interact_with_about() -> None:
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, AboutDialog)
            assert dialog.windowTitle() == "About Choicer Voicer Pack Creator"
            text = "".join(label.text() for label in dialog.findChildren(QLabel))
            assert __version__ in text
            assert "THIRD_PARTY_NOTICES.md" in text
            assert "Source media remains yours." in text
            button = dialog.support_button
            assert button.window() is dialog
            assert button.isVisible() and button.isEnabled()
            assert button.accessibleName() == "Buy Me a Coffee"
            assert not button.icon().pixmap(button.iconSize()).isNull()
            assert button.iconSize().height() == 40
            if window.active_editor is original:
                qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
            else:
                button.setFocus()
                qtbot.keyClick(button, Qt.Key.Key_Space)
            assert dialog.isVisible()
            qtbot.keyClick(dialog, Qt.Key.Key_Return)
            assert not dialog.isVisible()
        finally:
            if isinstance(dialog, QDialog):
                dialog.reject()

    for count, editor in enumerate((original, another), start=1):
        window.tabs.setCurrentWidget(editor)
        QTimer.singleShot(0, interact_with_about)
        window.action_about.trigger()
        assert urls == ["https://www.buymeacoffee.com/throndir"] * count
        assert not editor.dirty
        assert not any(
            button.isVisible() for button in window.findChildren(QPushButton, "coffeeSupport")
        )
    assert original.project.to_dict() == before
    assert window.tabs.count() == 2
    window.close()


def test_about_support_button_reports_browser_failure(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda _url: False))
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args: warnings.append(args)
    )
    dialog = AboutDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.mouseClick(dialog.support_button, Qt.MouseButton.LeftButton)
    assert warnings == [(
        dialog, "Could not open browser",
        "Open this URL manually:\nhttps://www.buymeacoffee.com/throndir",
    )]
    assert dialog.isVisible()
    dialog.close()


def test_timeline_separates_simultaneous_segments(qtbot) -> None:
    timeline = TimelineWidget()
    qtbot.addWidget(timeline)
    first = Segment(10, 12, "Together", ["Fischl"])
    second = Segment(10, 12, "Together", ["Diluc"])
    timeline.set_duration(20)
    timeline.set_segments([first, second])
    assert timeline._segment_lanes[first.id] != timeline._segment_lanes[second.id]


def test_focused_text_is_committed_without_focus_change(qtbot) -> None:
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    segment = Segment(1, 2, "Old line", ["Old speaker"])
    window._set_project(
        PackProject(title="Old title", authors=["Old author"], segments=[segment]),
        None,
        mark_dirty=False,
    )
    window.select_segment(segment.id)
    window.title_edit.selectAll()
    qtbot.keyClicks(window.title_edit, "New title")
    window.authors_edit.selectAll()
    qtbot.keyClicks(window.authors_edit, "Alice, Bob")
    window.speakers_edit.selectAll()
    qtbot.keyClicks(window.speakers_edit, "Fischl, Diluc")
    window.caption_edit.setPlainText("Retribution!")
    window._commit_editors()
    assert window.project.title == "New title"
    assert window.project.authors == ["Alice", "Bob"]
    assert segment.characters == ["Fischl", "Diluc"]
    assert segment.caption == "Retribution!"
    assert window.dirty
    window.dirty = False
    window.close()


def test_inspector_sections_resize_collapse_and_restore(qtbot, tmp_path: Path) -> None:
    settings_path = tmp_path / "layout.ini"
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.inspector_splitter.height() > 300)
    window._restore_layout_state()

    window.inspector_splitter.setSizes([180, 420, 170])
    before_collapse = window.inspector_splitter.sizes()
    window.project_section.set_collapsed(True)
    qtbot.waitUntil(lambda: window.project_section.is_collapsed)
    qtbot.wait(50)
    after_collapse = window.inspector_splitter.sizes()
    saved_expanded_height = window.project_section.last_expanded_height
    assert window.project_section.body.isHidden()
    assert after_collapse[0] <= window.project_section.minimumHeight()
    assert after_collapse[1] > before_collapse[1]
    assert after_collapse[1] > after_collapse[0]
    assert window.editor_splitter.handleWidth() == 1
    assert window.inspector_splitter.handleWidth() == 9
    window._save_layout_state()
    window.close()

    restored_settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    restored = MainWindow(UnusedMedia(), settings=restored_settings)  # type: ignore[arg-type]
    qtbot.addWidget(restored)
    restored.show()
    restored._restore_layout_state()
    assert restored.project_section.is_collapsed
    assert restored.project_section.body.isHidden()
    assert restored.project_section.last_expanded_height == saved_expanded_height
    restored.project_section.set_collapsed(False)
    qtbot.waitUntil(lambda: not restored.project_section.is_collapsed)
    qtbot.waitUntil(
        lambda: abs(
            restored.inspector_splitter.sizes()[0]
            - restored.project_section.last_expanded_height
        )
        <= 2
    )
    assert not restored.project_section.body.isHidden()
    restored.close()


def test_editor_divider_has_a_thin_gap_and_wider_grab_area(qtbot, tmp_path: Path) -> None:
    settings = QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.setStyleSheet(APP_STYLESHEET)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)

    splitter = window.editor_splitter
    left = splitter.widget(0)
    inspector_left = window.inspector_splitter.mapTo(splitter, QPoint(0, 0)).x()
    assert inspector_left - (left.x() + left.width()) == 1
    assert splitter.handleWidth() == 1
    assert splitter.handle(1).width() >= 5
    window.close()


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_editor_left_pane_shrinks_collapses_and_reopens(
    qtbot, tmp_path: Path, stylesheet: str
) -> None:
    settings = QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.setStyleSheet(stylesheet)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)

    splitter = window.editor_splitter
    original_window_size = window.size()
    for width in (700, 160, 32, 1, 0):
        splitter.moveSplitter(width, 1)
        assert splitter.sizes()[0] == width
        assert splitter.sizes()[1] >= 420
        assert window.size() == original_window_size

    handle = splitter.handle(1)
    assert handle.isVisible()
    assert handle.isEnabled()
    grab_point = handle.rect().center()
    target = splitter.mapToGlobal(QPoint(320, grab_point.y()))
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=grab_point)
    qtbot.mouseMove(handle, handle.mapFromGlobal(target))
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(target))
    qtbot.waitUntil(lambda: abs(splitter.sizes()[0] - 320) <= 2)
    assert window.video_widget.isVisible()
    assert window.video_widget.width() > 0
    window.close()


@pytest.mark.parametrize("left_width", [80, 0], ids=["narrow", "collapsed"])
def test_editor_restores_pane_size_without_restoring_old_divider_width(
    qtbot, tmp_path: Path, left_width: int
) -> None:
    settings_path = str(tmp_path / "layout.ini")
    settings = QSettings(settings_path, QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.setStyleSheet(APP_STYLESHEET)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.editor_splitter.setHandleWidth(9)
    window.editor_splitter.moveSplitter(left_width, 1)
    window.close()

    restored_settings = QSettings(settings_path, QSettings.Format.IniFormat)
    restored = MainWindow(UnusedMedia(), settings=restored_settings)  # type: ignore[arg-type]
    qtbot.addWidget(restored)
    restored.setStyleSheet(APP_STYLESHEET)
    restored.show()
    qtbot.waitUntil(lambda: restored._layout_restored)
    splitter = restored.editor_splitter
    assert abs(splitter.sizes()[0] - left_width) <= 1
    assert splitter.handleWidth() == 1
    assert splitter.getRange(1)[0] == 0
    assert splitter.getRange(1)[1] <= splitter.width() - 420
    left = splitter.widget(0)
    inspector_left = restored.inspector_splitter.mapTo(splitter, QPoint(0, 0)).x()
    assert inspector_left - (left.x() + left.width()) == 1
    restored.close()


@pytest.fixture
def playback_window(qtbot, tmp_path: Path, monkeypatch):
    settings = QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_maybe_save", lambda: True)
    segment = Segment(1, 2, "Line", ["Speaker"])
    window._set_project(
        PackProject(video_duration=10, segments=[segment]), None, mark_dirty=False
    )
    window.select_segment(segment.id)
    calls: list[str] = []
    monkeypatch.setattr(window.player, "play", lambda: calls.append("play"))
    monkeypatch.setattr(window.player, "pause", lambda: calls.append("pause"))
    window.show()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    qtbot.waitUntil(lambda: window._layout_restored)
    return window, calls


@pytest.mark.parametrize("widget_name", ["timeline", "segment_table", "video_widget"])
@pytest.mark.parametrize(
    ("key", "modifier"),
    [
        (Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier),
        (Qt.Key.Key_Delete, Qt.KeyboardModifier.ControlModifier),
    ],
)
@pytest.mark.parametrize("confirmed", [False, True], ids=["cancel", "confirm"])
def test_delete_shortcuts_use_existing_segment_confirmation(
    qtbot, playback_window, monkeypatch, widget_name: str, key, modifier, confirmed: bool
) -> None:
    window, _calls = playback_window
    selected = window.selected_segment()
    other = Segment(3, 4, "Keep this line", ["Another speaker"])
    window.project.add_segment(other)
    window._refresh_table()
    questions: list[str] = []

    def confirm(_parent, _title, message):
        questions.append(message)
        return QMessageBox.StandardButton.Yes if confirmed else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", confirm)
    widget = getattr(window, widget_name)
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus)

    qtbot.keyClick(widget, key, modifier)

    assert len(questions) == 1
    assert selected.caption in questions[0]
    assert window.project.segments == ([other] if confirmed else [selected, other])
    assert window.segment_table.rowCount() == (1 if confirmed else 2)
    assert window.timeline.segments == window.project.segments
    assert window.selected_segment() == (None if confirmed else selected)
    assert window.dirty == confirmed


@pytest.mark.parametrize(
    "widget_name",
    [
        "title_edit", "authors_edit", "readme_edit", "speakers_edit", "caption_edit",
        "mark_in_spin", "mark_out_spin", "head_pad_spin", "tail_pad_spin",
        "height_spin", "fps_spin",
    ],
)
def test_backspace_remains_available_in_editors(
    qtbot, playback_window, monkeypatch, widget_name: str
) -> None:
    window, _calls = playback_window
    selected = window.selected_segment()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: pytest.fail("Editing must not delete a segment")
    )
    editor = getattr(window, widget_name)
    editor.setFocus()
    qtbot.waitUntil(editor.hasFocus)
    editor.selectAll()

    qtbot.keyClick(editor, Qt.Key.Key_Backspace)
    qtbot.keyClick(editor, Qt.Key.Key_Backspace)

    text = editor.toPlainText() if hasattr(editor, "toPlainText") else editor.text()
    assert text.strip() == ("s" if widget_name in {"mark_in_spin", "mark_out_spin"} else "")
    assert window.project.segments == [selected]


@pytest.mark.parametrize("blocked", ["no-selection", "disabled-action"])
def test_backspace_does_not_delete_without_an_available_action(
    qtbot, playback_window, monkeypatch, blocked: str
) -> None:
    window, _calls = playback_window
    segments = list(window.project.segments)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: pytest.fail("Deletion should be unavailable")
    )
    if blocked == "no-selection":
        window.selected_segment_id = ""
        window._refresh_table()
        window._sync_selected_editor()
    else:
        window.action_delete.setEnabled(False)
    window.setFocus()
    qtbot.waitUntil(window.hasFocus)

    qtbot.keyClick(window, Qt.Key.Key_Backspace)

    assert window.project.segments == segments
    assert not window.dirty


def test_holding_backspace_does_not_repeat_confirmation(
    qtbot, playback_window, monkeypatch
) -> None:
    window, _calls = playback_window
    questions: list[str] = []

    def decline(_parent, _title, message):
        questions.append(message)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    window.timeline.setFocus()
    qtbot.waitUntil(window.timeline.hasFocus)

    qtbot.keyPress(window.timeline, Qt.Key.Key_Backspace)
    for _ in range(3):
        QApplication.sendEvent(
            window.timeline,
            QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier,
                "\b", True,
            ),
        )
    qtbot.keyRelease(window.timeline, Qt.Key.Key_Backspace)

    assert len(questions) == 1
    assert len(window.project.segments) == 1
    assert not window.dirty


@pytest.mark.parametrize("modal", [False, True], ids=["modeless", "modal"])
def test_backspace_in_a_dialog_does_not_delete_a_segment(
    qtbot, playback_window, monkeypatch, modal: bool
) -> None:
    window, _calls = playback_window
    selected = window.selected_segment()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: pytest.fail("Dialogs must not delete a segment")
    )
    dialog = QDialog(window)
    qtbot.addWidget(dialog)
    dialog.setModal(modal)
    button = QPushButton("Dialog action", dialog)
    dialog.show()
    dialog.activateWindow()
    button.setFocus()
    qtbot.waitUntil(dialog.isActiveWindow)
    qtbot.waitUntil(button.hasFocus)

    qtbot.keyClick(button, Qt.Key.Key_Backspace)

    assert window.project.segments == [selected]
    assert not window.dirty
    dialog.close()


@pytest.mark.parametrize(
    "widget_name",
    [
        "video_widget", "timeline", "segment_table", "seek_slider",
        "volume_slider", "play_button", "stop_button",
    ],
)
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (QMediaPlayer.PlaybackState.StoppedState, "play"),
        (QMediaPlayer.PlaybackState.PausedState, "play"),
        (QMediaPlayer.PlaybackState.PlayingState, "pause"),
    ],
)
def test_space_toggles_video_playback(
    qtbot, playback_window, monkeypatch, widget_name: str, state, expected: str
) -> None:
    window, calls = playback_window
    monkeypatch.setattr(window.player, "playbackState", lambda: state)
    widget = getattr(window, widget_name)
    widget.setFocus()
    qtbot.waitUntil(widget.hasFocus)

    qtbot.keyClick(widget, Qt.Key.Key_Space)

    assert calls == [expected]


@pytest.mark.parametrize(
    "widget_name", ["title_edit", "authors_edit", "readme_edit", "speakers_edit", "caption_edit"]
)
def test_space_remains_available_in_text_editors(
    qtbot, playback_window, widget_name: str
) -> None:
    window, calls = playback_window
    editor = getattr(window, widget_name)
    editor.setFocus()
    qtbot.waitUntil(editor.hasFocus)
    editor.clear()

    qtbot.keyClicks(editor, "two words")

    text = editor.text() if isinstance(editor, QLineEdit) else editor.toPlainText()
    assert text == "two words"
    assert calls == []


@pytest.mark.parametrize(
    "widget_name",
    ["mark_in_spin", "mark_out_spin", "head_pad_spin", "tail_pad_spin", "height_spin", "fps_spin"],
)
def test_space_does_not_play_video_while_editing_numbers(
    qtbot, playback_window, widget_name: str
) -> None:
    window, calls = playback_window
    editor = getattr(window, widget_name)
    editor.setFocus()
    qtbot.waitUntil(editor.hasFocus)

    qtbot.keyClick(editor, Qt.Key.Key_Space)

    assert calls == []


def test_holding_space_does_not_repeatedly_toggle_playback(qtbot, playback_window) -> None:
    window, calls = playback_window
    window.timeline.setFocus()
    qtbot.waitUntil(window.timeline.hasFocus)

    qtbot.keyPress(window.timeline, Qt.Key.Key_Space)
    for _ in range(3):
        QApplication.sendEvent(
            window.timeline,
            QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier,
                " ", True,
            ),
        )
    qtbot.keyRelease(window.timeline, Qt.Key.Key_Space)

    assert calls == ["play"]


@pytest.mark.parametrize(
    "modifier",
    [Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.AltModifier,
     Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.MetaModifier],
)
def test_modified_space_does_not_toggle_playback(qtbot, playback_window, modifier) -> None:
    window, calls = playback_window
    window.timeline.setFocus()
    qtbot.waitUntil(window.timeline.hasFocus)

    qtbot.keyClick(window.timeline, Qt.Key.Key_Space, modifier)

    assert calls == []


@pytest.mark.parametrize("modal", [False, True], ids=["modeless", "modal"])
def test_space_in_a_dialog_does_not_toggle_video(qtbot, playback_window, modal: bool) -> None:
    window, calls = playback_window
    dialog = QDialog(window)
    qtbot.addWidget(dialog)
    dialog.setModal(modal)
    button = QPushButton("Dialog action", dialog)
    dialog.show()
    dialog.activateWindow()
    button.setFocus()
    qtbot.waitUntil(dialog.isActiveWindow)
    qtbot.waitUntil(button.hasFocus)

    with qtbot.waitSignal(button.clicked):
        qtbot.keyClick(button, Qt.Key.Key_Space)

    assert calls == []
    dialog.close()


@pytest.mark.parametrize(
    "state",
    [
        QMediaPlayer.PlaybackState.StoppedState,
        QMediaPlayer.PlaybackState.PausedState,
        QMediaPlayer.PlaybackState.PlayingState,
    ],
)
def test_dragging_playhead_seeks_player_without_changing_project(qtbot, state) -> None:
    class SeekPlayer:
        def __init__(self) -> None:
            self.position_ms = 3000

        def playbackState(self):
            return state

        def source(self):
            return QUrl.fromLocalFile("C:/source.mp4")

        def setPosition(self, milliseconds):
            self.position_ms = milliseconds
            window._player_position_changed(milliseconds)

        def stop(self):
            pass

    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    segment = Segment(2, 4, "Line", ["Speaker"])
    window._set_project(
        PackProject(video_duration=10, segments=[segment]), None, mark_dirty=False
    )
    player = SeekPlayer()
    window.player = player  # type: ignore[assignment]
    timeline = window.timeline
    timeline.set_marks(2, 4, segment.id)
    timeline.set_playhead(3)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.editor_splitter.setSizes([700, 250])
    seeks: list[float] = []
    timeline.seek_requested.connect(seeks.append)
    start = QPoint(round(timeline._time_to_x(3)), 65)
    target = QPoint(round(timeline._time_to_x(7.5)), 65)
    expected = int(timeline._x_to_time(target.x()) * 1000)

    qtbot.mousePress(timeline, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(timeline, target)
    assert player.position_ms == expected
    qtbot.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=target)

    assert player.position_ms == expected
    assert seeks[-1] == pytest.approx(expected / 1000, abs=0.001)
    assert timeline.playhead == pytest.approx(expected / 1000, abs=0.001)
    assert window.seek_slider.value() == int(expected / 10_000 * 100_000)
    assert not window.dirty
    assert window._range_edit_record is None
    assert (segment.start, segment.end) == (2, 4)
    assert (timeline.mark_in, timeline.mark_out) == (2, 4)
    assert timeline.mark_segment_id == segment.id
    assert player.playbackState() == state
    if state == QMediaPlayer.PlaybackState.StoppedState:
        assert window._stopped_seek_active
        assert window._stopped_seek_target_ms == expected
    window.close()


def test_seek_from_stopped_state_decodes_then_pauses_on_target(qtbot) -> None:
    class FakeAudioOutput:
        def __init__(self) -> None:
            self.muted = False

        def isMuted(self):
            return self.muted

        def setMuted(self, muted):
            self.muted = muted

    class StoppedPlayer:
        def __init__(self) -> None:
            self.state = QMediaPlayer.PlaybackState.StoppedState
            self.position_ms = 0
            self.calls: list[object] = []

        def playbackState(self):
            return self.state

        def source(self):
            return QUrl.fromLocalFile("C:/source.mp4")

        def play(self):
            self.calls.append("play")
            self.state = QMediaPlayer.PlaybackState.PlayingState

        def pause(self):
            self.calls.append("pause")
            self.state = QMediaPlayer.PlaybackState.PausedState

        def stop(self):
            self.calls.append("stop")
            self.state = QMediaPlayer.PlaybackState.StoppedState

        def setPosition(self, milliseconds):
            self.calls.append(("position", milliseconds))
            self.position_ms = milliseconds

        def position(self):
            return self.position_ms

    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    player = StoppedPlayer()
    window.player = player  # type: ignore[assignment]
    audio_output = FakeAudioOutput()
    window.audio_output = audio_output  # type: ignore[assignment]
    window.timeline.set_duration(10)

    window.seek(4.25)
    assert player.calls == [("position", 4250)]
    assert audio_output.muted
    assert window._stopped_seek_active
    window._start_stopped_seek_decode()
    assert player.calls[-2:] == [("position", 4250), "play"]
    window._finish_stopped_seek()

    assert player.calls[-1] == "pause"
    assert player.state == QMediaPlayer.PlaybackState.PausedState
    assert not audio_output.muted
    assert not window._stopped_seek_active
    assert window.timeline.playhead == 4.25
    window.dirty = False
    window.close()


def test_selecting_table_segment_cues_playback_before_play(qtbot) -> None:
    class PromptPlayer:
        def __init__(self) -> None:
            self.stop_count = 0

        def stop(self):
            self.stop_count += 1

    class PausedPlayer:
        def __init__(self) -> None:
            self.state = QMediaPlayer.PlaybackState.PausedState
            self.position_ms = 250
            self.calls: list[object] = []

        def playbackState(self):
            return self.state

        def setPosition(self, milliseconds):
            self.calls.append(("position", milliseconds))
            self.position_ms = milliseconds

        def position(self):
            return self.position_ms

        def play(self):
            self.calls.append("play")
            self.state = QMediaPlayer.PlaybackState.PlayingState

        def pause(self):
            self.calls.append("pause")
            self.state = QMediaPlayer.PlaybackState.PausedState

        def stop(self):
            self.calls.append("stop")
            self.state = QMediaPlayer.PlaybackState.StoppedState

    first = Segment(1, 2, "First", ["A"])
    second = Segment(6.25, 7.5, "Second", ["B"])
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window._set_project(
        PackProject(
            title="Selection cue",
            authors=["Creator"],
            video_duration=10,
            segments=[first, second],
        ),
        None,
        mark_dirty=False,
    )
    player = PausedPlayer()
    window.player = player  # type: ignore[assignment]
    prompt_player = PromptPlayer()
    window.prompt_player = prompt_player  # type: ignore[assignment]

    window.segment_table.selectRow(1)
    qtbot.waitUntil(lambda: window.selected_segment_id == second.id)

    assert player.position_ms == 6250
    assert player.calls == [("position", 6250)]
    assert prompt_player.stop_count == 1
    assert window.mark_in_spin.value() == second.start
    assert window.mark_out_spin.value() == second.end
    window.toggle_playback()
    assert player.calls[-1] == "play"
    assert player.position_ms == 6250
    window.dirty = False
    window.close()


@pytest.mark.parametrize("surface", ["table", "timeline"])
@pytest.mark.parametrize(
    "state",
    [
        QMediaPlayer.PlaybackState.StoppedState,
        QMediaPlayer.PlaybackState.PausedState,
        QMediaPlayer.PlaybackState.PlayingState,
    ],
)
def test_segment_clicks_cue_start_even_when_already_selected(
    qtbot, tmp_path: Path, surface: str, state
) -> None:
    class SeekPlayer:
        def __init__(self) -> None:
            self.position_ms = 500
            self.state = state

        def playbackState(self):
            return self.state

        def source(self):
            return QUrl.fromLocalFile("C:/source.mp4")

        def setPosition(self, milliseconds):
            self.position_ms = milliseconds
            window._player_position_changed(milliseconds)

        def position(self):
            return self.position_ms

        def play(self):
            self.state = QMediaPlayer.PlaybackState.PlayingState

        def pause(self):
            self.state = QMediaPlayer.PlaybackState.PausedState

        def stop(self):
            self.state = QMediaPlayer.PlaybackState.StoppedState

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    first = Segment(1, 2, "First", ["A"])
    second = Segment(6.25, 7.5, "Second", ["B"])
    window._set_project(
        PackProject(video_duration=10, segments=[first, second]), None, mark_dirty=False
    )
    player = SeekPlayer()
    window.player = player  # type: ignore[assignment]
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.editor_splitter.setSizes([700, 470])
    window.select_segment(first.id)
    if surface == "table":
        QApplication.processEvents()
        window.segment_table.scrollToItem(window.segment_table.item(1, 4))
        target = window.segment_table.viewport()
        point = window.segment_table.visualItemRect(window.segment_table.item(1, 4)).center()
    else:
        target = window.timeline
        point = QPoint(
            round(window.timeline._time_to_x(7)),
            round(window.timeline._segment_rect(second).center().y()),
        )

    for _ in range(2):
        player.setPosition(9000)
        window._preview_end = first.end
        qtbot.mouseClick(target, Qt.MouseButton.LeftButton, pos=point)

        assert window.selected_segment_id == second.id
        assert player.position_ms == 6250
        assert window.timeline.playhead == second.start
        assert window.timeline.mark_segment_id == second.id
        assert window.mark_in_spin.value() == second.start
        assert window.mark_out_spin.value() == second.end
        assert window._preview_end is None
        assert not window.dirty
        assert window._range_edit_record is None
        assert (second.start, second.end) == (6.25, 7.5)
        assert player.playbackState() == state
        if state == QMediaPlayer.PlaybackState.StoppedState:
            assert window._stopped_seek_target_ms == 6250
    if surface == "table":
        qtbot.mouseDClick(target, Qt.MouseButton.LeftButton, pos=point)
        qtbot.mouseRelease(target, Qt.MouseButton.LeftButton, pos=point)
        assert window._preview_end == second.end
        assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
    window.close()


def test_overlap_review_is_visible_but_does_not_block_export_readiness(
    qtbot, tmp_path: Path
) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    first = Segment(1, 3, "First", ["A"])
    second = Segment(2.5, 4, "Second", ["B"])
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window._set_project(
        PackProject(
            title="Overlap review",
            authors=["Creator"],
            video_path=str(video),
            video_duration=5,
            segments=[first, second],
        ),
        None,
        mark_dirty=False,
    )

    assert "Ready to export" in window.validation_label.text()
    assert "1 potential overlap" in window.validation_label.text()
    assert "overlap by 0.500s" in window.validation_label.toolTip()
    assert window.segment_table.item(0, 0).background().color() == QColor("#49351d")

    second.start = 3
    window._refresh_table(second.id)
    assert "potential overlap" not in window.validation_label.text()
    assert window.validation_label.toolTip() == ""
    window.dirty = False
    window.close()


def test_range_edit_can_regenerate_or_undo_preserved_audio(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "source.mp4"
    audio = tmp_path / "prompt.mp3"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    segment = Segment(
        1,
        2,
        "Line",
        ["Hero"],
        audio_mode="file",
        audio_path=str(audio),
        source_range_known=False,
    )
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window._set_project(
        PackProject(
            title="Imported",
            authors=["Creator"],
            video_path=str(video),
            video_duration=10,
            segments=[segment],
        ),
        None,
        mark_dirty=False,
    )
    window.select_segment(segment.id)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel),
    )
    window._timeline_range_edit_started(segment.id, 1, 2)
    window._timeline_range_changed(segment.id, 1.25, 2.5)
    window._timeline_range_edit_finished(segment.id, 1, 2, 1.25, 2.5)
    assert (segment.start, segment.end) == (1, 2)
    assert segment.audio_mode == "file"
    assert not window.dirty

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.No),
    )
    window._timeline_range_edit_started(segment.id, 1, 2)
    window._timeline_range_changed(segment.id, 1.5, 3)
    window._timeline_range_edit_finished(segment.id, 1, 2, 1.5, 3)
    assert (segment.start, segment.end) == (1.5, 3.25)
    assert segment.audio_mode == "file"
    assert segment.audio_path == str(audio)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes),
    )
    window._timeline_range_edit_started(segment.id, 1.5, 3.25)
    window._timeline_range_changed(segment.id, 2, 3.5)
    window._timeline_range_edit_finished(segment.id, 1.5, 3.25, 2, 3.5)
    assert (segment.start, segment.end) == (2, 3.5)
    assert segment.audio_mode == "video"
    assert segment.audio_path == ""
    assert segment.source_range_known
    assert window.dirty
    window.dirty = False
    window.close()


def test_recovery_restores_unsaved_edits_without_overwriting_project(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    project_path = tmp_path / "saved.cvpack.json"
    saved = PackProject(
        title="Saved",
        authors=["Creator"],
        segments=[Segment(1, 2, "Saved line", ["Hero"])],
    )
    ProjectStore.save(saved, project_path)
    recovery = RecoveryStore(tmp_path / "recovery.json")
    recovered = PackProject.from_dict(saved.to_dict())
    recovered.segments[0].caption = "Unsaved recovered line"
    recovery.save(recovered, project_path)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes),
    )
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.active_editor.recovery_store = recovery
    window._offer_recovery()

    assert window.project_path == project_path.resolve()
    assert window.project.segments[0].caption == "Unsaved recovered line"
    assert window.dirty
    assert ProjectStore.load(project_path).segments[0].caption == "Saved line"
    window.dirty = False
    window.close()


def test_discard_only_clears_recovery_after_transition(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    recovery = RecoveryStore(tmp_path / "recovery.json")
    project = PackProject(
        title="Current",
        authors=["Creator"],
        segments=[Segment(1, 2, "Unsaved line", ["Hero"])],
    )
    recovery.save(project, None)
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.active_editor.recovery_store = recovery
    window._set_project(project, None, mark_dirty=True)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard),
    )

    assert window._maybe_save()
    assert recovery.path.is_file()
    window._set_project(PackProject(authors=["Creator"]), None, mark_dirty=False)
    qtbot.waitUntil(lambda: not recovery.path.exists())
    window.close()


def test_open_corrupt_project_offers_previous_save(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "project.cvpack.json"
    project = PackProject(title="First", authors=["Creator"])
    ProjectStore.save(project, path)
    project.title = "Second"
    ProjectStore.save(project, path)
    path.write_text("corrupt", encoding="utf-8")
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.open_path(path)

    qtbot.waitUntil(lambda: any(
        box.windowTitle() == "Open previous save?" for box in window._decisions
    ))
    box = next(box for box in window._decisions if box.windowTitle() == "Open previous save?")
    qtbot.mouseClick(box.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
    assert window.project.title == "First"
    assert window.project_path is None
    assert window.dirty
    assert path.read_text(encoding="utf-8") == "corrupt"
    for box in list(window._decisions):
        box.reject()
    window.dirty = False
    window.close()


def test_waveform_worker_honors_interruption(qtbot, tmp_path: Path) -> None:
    started = threading.Event()

    class CancellableMedia:
        def waveform_peaks(self, _path, _duration, *, cancelled):
            started.set()
            while not cancelled():
                time.sleep(0.005)
            return []

    worker = WaveformWorker(CancellableMedia(), 1, str(tmp_path / "source.mp4"), 10)  # type: ignore[arg-type]
    completed: list[object] = []
    worker.completed.connect(lambda *values: completed.append(values))
    worker.start()
    qtbot.waitUntil(started.is_set)
    worker.requestInterruption()
    assert worker.wait(2000)
    assert completed == []


def test_late_waveform_result_is_ignored(qtbot) -> None:
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.project.video_path = "same-source.mp4"
    window._waveform_request_id = 2

    window._waveform_ready(1, "same-source.mp4", 12, [0.8])
    assert window.timeline.peaks == []
    window._waveform_ready(2, "same-source.mp4", 12, [0.8])
    assert window.timeline.peaks == [0.8]
    window.dirty = False
    window.close()


def test_worker_results_are_delivered_on_gui_thread(qtbot, tmp_path: Path, monkeypatch) -> None:
    waveform_threads: list[QThread] = []
    retirement_threads: list[QThread] = []
    export_threads: list[QThread] = []

    class ImmediateMedia(UnusedMedia):
        def waveform_peaks(self, _path, _duration, *, cancelled):
            return [] if cancelled() else [0.5]

    class ImmediateExporter:
        def export(self, _project, _destination, *, create_zip, progress):
            assert create_zip
            progress("working")
            return ExportResult(tmp_path, None, {}, {}, [])

    class TrackingEditor(ProjectEditor):
        @Slot(int, str, float, list)
        def _waveform_ready(self, request_id, path, duration, peaks):
            waveform_threads.append(QThread.currentThread())
            super()._waveform_ready(request_id, path, duration, peaks)

        @Slot()
        def _retire_waveform_worker(self):
            retirement_threads.append(QThread.currentThread())
            super()._retire_waveform_worker()

        @Slot(object)
        def _export_completed(self, _value):
            export_threads.append(QThread.currentThread())

    monkeypatch.setattr(MainWindow, "editor_type", TrackingEditor)
    window = MainWindow(ImmediateMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    window.project.video_path = str(source)
    window._start_waveform(str(source), 1)
    qtbot.waitUntil(lambda: bool(waveform_threads and retirement_threads))

    export_worker = ExportWorker(ImmediateExporter(), window.project, tmp_path)  # type: ignore[arg-type]
    export_worker.completed.connect(window._export_completed)
    export_worker.start()
    qtbot.waitUntil(lambda: bool(export_threads))
    assert export_worker.wait(2000)

    assert all(thread == window.thread() for thread in waveform_threads)
    assert all(thread == window.thread() for thread in retirement_threads)
    assert all(thread == window.thread() for thread in export_threads)
    window.dirty = False
    window.close()
