from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QLabel, QMessageBox

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui.main_window import MainWindow


class UnusedMedia:
    pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    editor = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    monkeypatch.setattr(editor, "_maybe_save", lambda: True)
    qtbot.addWidget(editor)
    editor._set_project(
        PackProject(
            video_duration=10,
            segments=[
                Segment(1, 2, "First line.", ["Alice"]),
                Segment(3, 4, "Middle line.", ["Bob"]),
                Segment(5, 6, "Last line.", ["Carol", "Alice"]),
            ],
        ),
        None, mark_dirty=False,
    )
    editor.show()
    qtbot.waitUntil(lambda: editor._layout_restored)
    yield editor
    editor.dirty = False
    editor.close()


def click_row(qtbot, window, row, modifier=Qt.KeyboardModifier.NoModifier) -> None:
    table = window.segment_table
    item = table.item(row, 0)
    table.scrollToItem(item)
    qtbot.mouseClick(
        table.viewport(), Qt.MouseButton.LeftButton, modifier,
        pos=table.visualItemRect(item).center(),
    )


def test_combine_button_joins_selected_rows_and_updates_editor_and_saved_project(
    window, qtbot, tmp_path, monkeypatch
) -> None:
    first, untouched, last = window.project.segments
    click_row(qtbot, window, 2)
    window.caption_edit.setPlainText(" Edited last line. ")
    click_row(qtbot, window, 0, Qt.KeyboardModifier.ControlModifier)
    assert set(window._selected_table_ids()) == {first.id, last.id}
    assert window.selected_segment() is None
    assert not window.caption_edit.isEnabled()
    assert window.combine_button.isEnabled()
    seeks = []
    original_seek = window.seek

    def seek(seconds):
        seeks.append(seconds)
        original_seek(seconds)

    monkeypatch.setattr(window, "seek", seek)
    qtbot.mouseClick(window.combine_button, Qt.MouseButton.LeftButton)

    combined = window.project.segments[0]
    assert window.project.segments == [combined, untouched]
    assert (combined.start, combined.end) == (1, 6)
    assert combined.caption == "First line. Edited last line."
    assert combined.characters == ["Alice", "Carol"]
    assert window.selected_segment() is combined
    assert window._selected_table_ids() == [combined.id]
    assert window.caption_edit.toPlainText() == combined.caption
    assert window.speakers_edit.text() == "Alice, Carol"
    assert window.mark_in_spin.value() == 1
    assert window.mark_out_spin.value() == 6
    assert window.timeline.segments == window.project.segments
    assert window.timeline.selected_id == combined.id
    assert window.timeline.mark_segment_id == combined.id
    assert seeks == [1]
    window.player.positionChanged.emit(1000)
    assert window.timeline.playhead == 1
    assert window.segment_table.rowCount() == 2
    assert window.segment_table.item(0, 4).text() == combined.caption
    assert [
        label.text()
        for label in window.video_widget.subtitle_overlay.findChildren(QLabel, "subtitleCaption")
        if label.isVisible()
    ] == [combined.caption]
    assert window.dirty
    assert not window.action_combine.isEnabled()
    window.project_path = tmp_path / "combined.cvpack.json"
    assert window.save_project()
    assert ProjectStore.load(window.project_path).to_dict() == window.project.to_dict()


def test_shift_selection_and_keyboard_shortcut_combine_a_range(window, qtbot) -> None:
    click_row(qtbot, window, 0)
    click_row(qtbot, window, 2, Qt.KeyboardModifier.ShiftModifier)
    assert len(window._selected_table_ids()) == 3
    qtbot.keyClick(
        window.segment_table, Qt.Key.Key_M,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    assert len(window.project.segments) == 1
    assert window.project.segments[0].caption == "First line. Middle line. Last line."


def test_multiselection_survives_refresh_and_busy_state_without_editing(window, qtbot) -> None:
    first, middle, last = window.project.segments
    before = window.project.to_dict()
    assert not window.action_combine.isEnabled()
    assert not window.combine_button.isEnabled()
    click_row(qtbot, window, 0)
    click_row(qtbot, window, 2, Qt.KeyboardModifier.ControlModifier)
    window._refresh_table()
    window._commit_editors()
    assert set(window._selected_table_ids()) == {first.id, last.id}
    assert window.selected_segment() is None
    assert window.timeline.selected_id == ""
    assert window.timeline.mark_segment_id == ""
    assert not window.speakers_edit.isEnabled()
    assert "2 segments selected" in window.segment_audio_help.text()
    window._set_busy(True, "Exporting")
    assert not window.action_combine.isEnabled()
    assert not window.combine_button.isEnabled()
    window._set_busy(False, "Ready")
    assert window.action_combine.isEnabled()
    assert window.combine_button.isEnabled()

    click_row(qtbot, window, 2, Qt.KeyboardModifier.ControlModifier)
    assert window._selected_table_ids() == [first.id]
    assert window.selected_segment() is first
    assert window.caption_edit.isEnabled()
    assert not window.action_combine.isEnabled()
    click_row(qtbot, window, 2, Qt.KeyboardModifier.ControlModifier)
    window.timeline.segment_selected.emit(middle.id)
    assert window._selected_table_ids() == [middle.id]
    assert window.selected_segment() is middle
    assert not window.combine_button.isEnabled()
    window.segment_table.clearSelection()
    assert window.selected_segment() is None
    assert not window.caption_edit.isEnabled()
    assert window.project.to_dict() == before
    assert not window.dirty


def test_loading_project_clears_multiselection(window, qtbot) -> None:
    click_row(qtbot, window, 0)
    click_row(qtbot, window, 2, Qt.KeyboardModifier.ShiftModifier)
    project = PackProject.from_dict(window.project.to_dict())
    window._set_project(project, None, mark_dirty=False)
    assert window._selected_table_ids() == []
    assert not window.action_combine.isEnabled()
    assert not window.combine_button.isEnabled()
    assert window.selected_segment() is None


@pytest.mark.parametrize("approve", [False, True], ids=["cancel", "keep-first"])
def test_combining_different_stills_requires_confirmation_and_never_deletes_files(
    window, qtbot, tmp_path, monkeypatch, approve
) -> None:
    first_image, last_image = tmp_path / "first.png", tmp_path / "last.png"
    first_image.write_bytes(b"first still")
    last_image.write_bytes(b"last still")
    window.project.segments[0].image_path = str(first_image)
    window.project.segments[2].image_path = str(last_image)
    click_row(qtbot, window, 2)
    click_row(qtbot, window, 0, Qt.KeyboardModifier.ControlModifier)
    before = window.project.to_dict()
    questions = []

    def confirm(*args):
        questions.append(args)
        return QMessageBox.StandardButton.Yes if approve else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", confirm)
    window.action_combine.trigger()
    assert len(questions) == 1
    assert "first.png" in questions[0][2]
    assert questions[0][-1] == QMessageBox.StandardButton.No
    assert first_image.read_bytes() == b"first still"
    assert last_image.read_bytes() == b"last still"
    if approve:
        assert len(window.project.segments) == 2
        assert window.selected_segment().image_path == str(first_image)
        assert window.dirty
    else:
        assert window.project.to_dict() == before
        assert len(window._selected_table_ids()) == 2
        assert not window.dirty


@pytest.mark.parametrize("preserved", [True, False], ids=["file-audio", "unknown-range"])
def test_combining_unsafe_audio_reports_how_to_fix_without_changing_project(
    window, qtbot, monkeypatch, preserved
) -> None:
    segment = window.project.segments[0]
    segment.audio_mode = "file" if preserved else "video"
    segment.audio_path = "preserved.mp3"
    segment.source_range_known = False
    click_row(qtbot, window, 0)
    click_row(qtbot, window, 2, Qt.KeyboardModifier.ShiftModifier)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    before = window.project.to_dict()
    window.action_combine.trigger()
    assert len(messages) == 1
    assert "Apply Range" in messages[0]
    assert window.project.to_dict() == before
    assert not window.dirty


@pytest.mark.parametrize("selected_count", [0, 1])
def test_combine_requires_multiple_rows(window, qtbot, monkeypatch, selected_count) -> None:
    if selected_count:
        click_row(qtbot, window, 0)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    before = window.project.to_dict()
    assert not window.action_combine.isEnabled()
    window.combine_segments()
    assert messages == ["Select at least two segments to combine."]
    assert window.project.to_dict() == before
    assert not window.dirty
