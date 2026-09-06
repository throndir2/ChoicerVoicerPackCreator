from __future__ import annotations

from unittest.mock import Mock

import pytest
from PySide6.QtCore import QItemSelectionModel, QPoint, QSettings, Qt, QUrl
from PySide6.QtWidgets import QApplication, QLabel

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


class UnusedMedia:
    pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    editor = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(editor)
    monkeypatch.setattr(editor, "_maybe_save", lambda: True)
    editor._set_project(
        PackProject(
            video_path=str(tmp_path / "source.mp4"),
            video_duration=10,
            segments=[Segment(1, 3, "First", ["Alice"]), Segment(6, 8, "Second", ["Bob"])],
        ),
        None, mark_dirty=False,
    )
    yield editor
    editor.dirty = False
    editor.close()


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_controls_share_one_row_with_help_only_in_tooltips(window, qtbot, stylesheet):
    window.setStyleSheet(stylesheet)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.editor_splitter.setSizes([780, 420])
    qtbot.waitUntil(lambda: window.mark_in_spin.isVisible())

    assert not any(
        "Drag the white playback line" in label.text()
        for label in window.findChildren(QLabel)
    )
    assert "Drag the white playback line" in window.timeline.toolTip()
    assert window.apply_range_button.accessibleName() == "Update Segment Timing"
    assert window.split_button.accessibleName() == "Split at Playhead"
    assert window.preview_segment_button.accessibleName() == "Play Selected Segment"
    for button in (window.apply_range_button, window.split_button, window.preview_segment_button):
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        assert not button.icon().isNull()
    row_y = window.mark_in_spin.geometry().center().y()
    for control in (
        window.mark_out_spin, window.set_in_button, window.set_out_button,
        window.add_segment_button, window.apply_range_button, window.split_button,
        window.preview_segment_button, window.zoom_slider,
    ):
        assert abs(control.geometry().center().y() - row_y) <= 1
    assert window.set_out_button.geometry().right() < window.add_segment_button.x()
    assert window.preview_segment_button.geometry().right() < window.zoom_slider.x()
    buttons = (
        window.add_segment_button, window.apply_range_button,
        window.split_button, window.preview_segment_button,
    )
    for button in buttons:
        assert button.toolTip()
    for control in (
        window.set_in_button, window.set_out_button, window.mark_in_spin, window.mark_out_spin,
        window.zoom_slider, window.seek_slider, window.volume_slider,
    ):
        assert control.toolTip()


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_native_timeline_controls_fit_without_clipping(window, qtbot, stylesheet):
    if QApplication.platformName() == "offscreen":
        pytest.skip("Requires native fonts; run with QT_QPA_PLATFORM=windows.")
    window.setStyleSheet(stylesheet)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.editor_splitter.moveSplitter(780, 1)
    assert window.editor_splitter.sizes()[0] == 780
    for control in (
        window.add_segment_button, window.apply_range_button, window.split_button,
        window.preview_segment_button, window.set_in_button, window.set_out_button,
        window.mark_in_spin, window.mark_out_spin, window.zoom_slider,
    ):
        assert control.width() >= control.sizeHint().width()
        assert control.parentWidget().rect().contains(control.geometry())


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_segment_controls_wrap_only_when_the_video_pane_is_narrow(window, qtbot, stylesheet):
    window.setStyleSheet(stylesheet)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    for width in (960, 620, 960):
        window.editor_splitter.moveSplitter(width, 1)
        if width == 620:
            qtbot.waitUntil(
                lambda: window.add_segment_button.y() > window.mark_in_spin.geometry().bottom()
            )
        else:
            qtbot.waitUntil(lambda: abs(
                window.add_segment_button.geometry().center().y()
                - window.mark_in_spin.geometry().center().y()
            ) <= 1)
        for control in (
            window.mark_in_spin, window.mark_out_spin, window.set_in_button,
            window.set_out_button, window.add_segment_button, window.apply_range_button,
            window.split_button, window.preview_segment_button, window.zoom_slider,
        ):
            assert control.isVisible()
            assert control.parentWidget().rect().contains(control.geometry())


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_video_timeline_divider_resizes_both_panes_without_hiding_controls(
    window, qtbot, stylesheet,
):
    window.setStyleSheet(stylesheet)
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    splitter = window.playback_splitter
    control_bar = window.add_segment_button.parentWidget()
    qtbot.waitUntil(lambda: (
        abs(
            window.add_segment_button.geometry().center().y()
            - window.mark_in_spin.geometry().center().y()
        ) <= 1
        and control_bar.height() == control_bar.minimumSizeHint().height()
    ))
    assert splitter.orientation() == Qt.Orientation.Vertical
    assert not splitter.childrenCollapsible()
    assert splitter.handleWidth() == 1
    handle = splitter.handle(1)
    assert handle.height() >= 5
    assert handle.accessibleName() == "Resize video and timeline"
    assert handle.toolTip()
    assert window.timeline.height() >= window.timeline.minimumHeight()
    initial_size = window.size()
    initial_video, initial_controls = splitter.sizes()
    initial_waveform_height = window.timeline.height()
    assert splitter.widget(1).y() - window.video_widget.height() == 1

    for delta in (-120, 80):
        before = splitter.sizes()
        before_timeline = window.timeline.height()
        grab_point = handle.rect().center()
        target = handle.mapToGlobal(grab_point) + QPoint(0, delta)
        qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=grab_point)
        qtbot.mouseMove(handle, handle.mapFromGlobal(target))
        qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.mapFromGlobal(target))
        qtbot.waitUntil(
            lambda before=before, delta=delta: abs(splitter.sizes()[0] - before[0] - delta) <= 2
        )
        assert abs(splitter.sizes()[1] - before[1] + delta) <= 2
        assert abs(window.timeline.height() - before_timeline + delta) <= 2
        assert window.size() == initial_size
        for control in (window.play_button, window.mark_in_spin, window.add_segment_button):
            assert control.isVisible()
            assert control.parentWidget().rect().contains(control.geometry())

    assert splitter.sizes()[0] < initial_video
    assert splitter.sizes()[1] > initial_controls
    assert window.timeline.height() > initial_waveform_height
    for position in (0, splitter.height()):
        splitter.moveSplitter(position, 1)
        assert window.video_widget.height() >= 96
        assert window.timeline.height() >= window.timeline.minimumHeight()
        assert all(size > 0 for size in splitter.sizes())
        assert window.size() == initial_size
    assert not window.dirty


def test_video_timeline_divider_restores_saved_layout(window, qtbot, tmp_path):
    window.show()
    qtbot.waitUntil(lambda: window._layout_restored)
    window.playback_splitter.moveSplitter(220, 1)
    saved_sizes = window.playback_splitter.sizes()
    qtbot.waitUntil(lambda: window.settings.contains("layout/playbackSplitterV1"))
    window.close()

    restored = MainWindow(
        UnusedMedia(),
        settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
    )  # type: ignore[arg-type]
    qtbot.addWidget(restored)
    restored._set_project(window.project, None, mark_dirty=False)
    restored.show()
    qtbot.waitUntil(lambda: restored._layout_restored)
    assert restored.playback_splitter.sizes() == saved_sizes
    restored.close()


def test_segment_buttons_require_one_selection_and_reset_with_project(window):
    buttons = (window.apply_range_button, window.split_button, window.preview_segment_button)
    actions = (window.action_apply_range, window.action_split, window.action_preview)
    assert all(not button.isEnabled() for button in buttons)
    assert all(not action.isEnabled() for action in actions)
    assert window.add_segment_button.isEnabled()
    first = window.project.segments[0]
    window.select_segment(first.id)
    assert all(button.isEnabled() for button in buttons)
    assert all(action.isEnabled() for action in actions)

    window.segment_table.selectionModel().select(
        window.segment_table.model().index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert window.selected_segment() is None
    assert all(not button.isEnabled() for button in buttons)
    assert all(not action.isEnabled() for action in actions)
    window.select_segment(first.id)
    assert all(button.isEnabled() for button in buttons)
    window.segment_table.clearSelection()
    assert all(not button.isEnabled() for button in buttons)
    window.select_segment(first.id)
    window._set_project(PackProject(), None, mark_dirty=False)
    assert all(not button.isEnabled() for button in buttons)


def test_markers_do_not_edit_until_update_timing_is_clicked(window, qtbot, monkeypatch):
    segment, other = window.project.segments
    window.select_segment(segment.id)
    monkeypatch.setattr(window.player, "position", lambda: 2000)
    qtbot.mouseClick(window.set_in_button, Qt.MouseButton.LeftButton)
    monkeypatch.setattr(window.player, "position", lambda: 4500)
    qtbot.mouseClick(window.set_out_button, Qt.MouseButton.LeftButton)
    assert (window.mark_in_spin.value(), window.mark_out_spin.value()) == (2, 4.5)
    assert (segment.start, segment.end) == (1, 3)
    assert not window.dirty

    qtbot.mouseClick(window.apply_range_button, Qt.MouseButton.LeftButton)
    assert (segment.start, segment.end) == (2, 4.5)
    assert (other.start, other.end) == (6, 8)
    assert len(window.project.segments) == 2
    assert window.dirty


def test_add_segment_uses_marked_range_without_changing_selected_segment(window, qtbot):
    original = window.project.segments[0]
    window.select_segment(original.id)
    window.mark_in_spin.setValue(4)
    window.mark_out_spin.setValue(5)
    qtbot.mouseClick(window.add_segment_button, Qt.MouseButton.LeftButton)

    assert len(window.project.segments) == 3
    assert (original.start, original.end) == (1, 3)
    added = window.selected_segment()
    assert added is not original
    assert (added.start, added.end) == (4, 5)


def test_split_button_uses_playhead_not_marked_range(window, qtbot, monkeypatch):
    first = window.project.segments[0]
    window.select_segment(first.id)
    window.mark_in_spin.setValue(4)
    window.mark_out_spin.setValue(5)
    monkeypatch.setattr(window.player, "position", lambda: 2000)
    qtbot.mouseClick(window.split_button, Qt.MouseButton.LeftButton)

    assert len(window.project.segments) == 3
    assert (first.start, first.end) == (1, 2)
    second = window.selected_segment()
    assert (second.start, second.end) == (2, 3)


@pytest.mark.parametrize("preserved_audio", [False, True], ids=["video", "preserved-prompt"])
def test_play_segment_uses_saved_segment_not_unapplied_marks(
    window, qtbot, monkeypatch, tmp_path, preserved_audio
):
    segment = window.project.segments[0]
    if preserved_audio:
        segment.audio_mode = "file"
        segment.audio_path = str(tmp_path / "prompt.mp3")
    window.select_segment(segment.id)
    window.mark_in_spin.setValue(4)
    window.mark_out_spin.setValue(5)
    seek, play, pause, prompt_source, prompt_play = (Mock() for _ in range(5))
    monkeypatch.setattr(window.player, "setPosition", seek)
    monkeypatch.setattr(window.player, "play", play)
    monkeypatch.setattr(window.player, "pause", pause)
    monkeypatch.setattr(window.prompt_player, "setSource", prompt_source)
    monkeypatch.setattr(window.prompt_player, "play", prompt_play)
    qtbot.mouseClick(window.preview_segment_button, Qt.MouseButton.LeftButton)

    if preserved_audio:
        prompt_source.assert_called_once_with(QUrl.fromLocalFile(segment.audio_path))
        prompt_play.assert_called_once_with()
        pause.assert_called_once_with()
        play.assert_not_called()
        seek.assert_not_called()
        assert window._preview_end is None
    else:
        seek.assert_called_once_with(1000)
        play.assert_called_once_with()
        prompt_play.assert_not_called()
        assert window._preview_end == 3
        window.player.positionChanged.emit(3000)
        pause.assert_called_once_with()
        assert window._preview_end is None
    assert (segment.start, segment.end) == (1, 3)
    assert not window.dirty
