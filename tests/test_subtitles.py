from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import QLabel, QMessageBox

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.subtitles import SubtitleVideoWidget
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


class UnusedMedia:
    pass


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    editor = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    monkeypatch.setattr(editor, "_maybe_save", lambda: True)
    def close(current):
        qtbot.waitUntil(lambda: not current.job_manager.active_jobs())
        for decision in tuple(current._decisions):
            decision.reject()
        for document in current.editors.values():
            document._commit_editors()
            document.dirty = False
            document._recovery_timer.stop()
        current.close()
        qtbot.waitUntil(lambda: current._close_approved and not current.isVisible())

    qtbot.addWidget(editor, before_close_func=close)
    editor.show()
    return editor


def subtitle_text(video: SubtitleVideoWidget, name: str = "subtitleCaption") -> list[str]:
    return [
        label.text() for label in video.subtitle_overlay.findChildren(QLabel, name)
        if label.isVisible()
    ]


def test_subtitles_follow_playback_boundaries_gaps_overlaps_and_backward_seeks(window) -> None:
    first = Segment(1, 2, "First line", ["Alice", "Bob"])
    second = Segment(2, 3, "Second line", ["Carol"])
    overlap = Segment(2.5, 4, "Together", ["Dave"])
    blank = Segment(0, 5, " \n ", ["No line"])
    window._set_project(
        PackProject(video_duration=5, segments=[overlap, second, first, blank]),
        None, mark_dirty=False,
    )
    assert not window.selected_segment_id
    before = window.project.to_dict()

    for milliseconds, captions, speakers in (
        (999, [], []),
        (1000, ["First line"], ["Alice, Bob"]),
        (1999, ["First line"], ["Alice, Bob"]),
        (2000, ["Second line"], ["Carol"]),
        (2500, ["Second line", "Together"], ["Carol", "Dave"]),
        (3000, ["Together"], ["Dave"]),
        (4000, [], []),
        (1500, ["First line"], ["Alice, Bob"]),
        (0, [], []),
    ):
        window.player.positionChanged.emit(milliseconds)
        assert subtitle_text(window.video_widget) == captions
        assert subtitle_text(window.video_widget, "subtitleSpeaker") == speakers
        assert window.video_widget.subtitle_overlay.isVisible() == bool(captions)

    assert not window.dirty
    assert window.project.to_dict() == before


def test_new_line_and_speakers_update_without_leaving_the_editor(window, qtbot) -> None:
    window._set_project(
        PackProject(video_path="missing-source.mp4", video_duration=5),
        None, mark_dirty=False,
    )
    window.mark_in_spin.setValue(1)
    window.mark_out_spin.setValue(2)
    window.add_segment()
    assert not window.video_widget.subtitle_overlay.isVisible()

    window.caption_edit.setPlainText("A newly written line.")
    assert subtitle_text(window.video_widget) == ["A newly written line."]
    window.speakers_edit.selectAll()
    qtbot.keyClicks(window.speakers_edit, "Alice, Bob")
    assert subtitle_text(window.video_widget, "subtitleSpeaker") == ["Alice, Bob"]
    window.speakers_edit.selectAll()
    qtbot.keyClick(window.speakers_edit, Qt.Key.Key_Backspace)
    assert subtitle_text(window.video_widget, "subtitleSpeaker") == ["Unassigned"]

    window.caption_edit.setPlainText("Revised line\nwith <b>literal markup</b> & symbols.")
    assert subtitle_text(window.video_widget) == [
        "Revised line\nwith <b>literal markup</b> & symbols."
    ]
    window.caption_edit.clear()
    assert not window.video_widget.subtitle_overlay.isVisible()


@pytest.mark.parametrize("preserved_prompt", [False, True], ids=["source", "imported-prompt"])
def test_opened_project_shows_saved_lines_without_selecting_or_editing(
    window, qtbot, tmp_path: Path, preserved_prompt: bool
) -> None:
    path = tmp_path / "saved.cvpack.json"
    project = PackProject(
        video_duration=5,
        segments=[
            Segment(
                0, 2, "Saved line", ["Saved speaker"],
                audio_mode="file" if preserved_prompt else "video",
                audio_path="preserved.mp3" if preserved_prompt else "",
                source_range_known=not preserved_prompt,
            ),
        ],
    )
    ProjectStore.save(project, path)
    window.open_path(path)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs())
    assert not window.selected_segment_id
    assert subtitle_text(window.video_widget) == ["Saved line"]
    assert subtitle_text(window.video_widget, "subtitleSpeaker") == ["Saved speaker"]
    window.seek(2)
    assert not window.video_widget.subtitle_overlay.isVisible()
    window.seek(1)
    assert subtitle_text(window.video_widget) == ["Saved line"]
    assert not window.dirty

    window._set_project(PackProject(), None, mark_dirty=False)
    assert not window.video_widget.subtitle_overlay.isVisible()
    assert subtitle_text(window.video_widget) == []


def test_live_range_edits_duplicates_and_deletions_refresh_subtitles(window, monkeypatch) -> None:
    segment = Segment(1, 3, "Line", ["Alice"])
    window._set_project(
        PackProject(video_duration=5, segments=[segment]), None, mark_dirty=False
    )
    window.select_segment(segment.id)
    assert subtitle_text(window.video_widget) == ["Line"]
    window._timeline_range_changed(segment.id, 3, 4)
    assert not window.video_widget.subtitle_overlay.isVisible()
    window._timeline_range_changed(segment.id, 1, 3)
    assert subtitle_text(window.video_widget) == ["Line"]

    window.duplicate_segment()
    assert subtitle_text(window.video_widget) == ["Line", "Line"]
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.delete_segment()
    assert subtitle_text(window.video_widget) == ["Line"]
    window.select_segment(segment.id)
    window.delete_segment()
    assert not window.video_widget.subtitle_overlay.isVisible()


def test_split_and_applied_range_use_updated_subtitle_boundaries(window, monkeypatch) -> None:
    segment = Segment(1, 3, "Line", ["Alice"])
    window._set_project(
        PackProject(video_duration=5, segments=[segment]), None, mark_dirty=False
    )
    window.select_segment(segment.id)
    monkeypatch.setattr(window, "current_position", lambda: 2.0)
    window.split_segment()
    assert len(window.project.segments) == 2
    assert subtitle_text(window.video_widget) == ["Line"]
    window.mark_in_spin.setValue(3)
    window.mark_out_spin.setValue(4)
    window.apply_selected_range()
    window.seek(2.5)
    assert not window.video_widget.subtitle_overlay.isVisible()
    window.seek(3)
    assert subtitle_text(window.video_widget) == ["Line"]
    window.stop_playback()
    assert not window.video_widget.subtitle_overlay.isVisible()


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_overlay_wraps_plain_text_with_speakers_above_and_does_not_constrain_video(
    qtbot, stylesheet: str
) -> None:
    video = SubtitleVideoWidget()
    qtbot.addWidget(video)
    video.setStyleSheet(stylesheet)
    video.resize(800, 450)
    video.show()
    caption = "A longer caption that wraps over several lines at a smaller video size. " * 2
    video.set_segments([Segment(0, 3, caption, ["<b>Alice & Bob</b>"])])
    qtbot.waitUntil(lambda: video.subtitle_overlay.isVisible())
    wide_height = video.subtitle_overlay.height()
    video.resize(360, 450)
    qtbot.waitUntil(lambda: video.subtitle_overlay.height() > wide_height)

    speaker = video.subtitle_overlay.findChild(QLabel, "subtitleSpeaker")
    dialogue = video.subtitle_overlay.findChild(QLabel, "subtitleCaption")
    assert speaker.text() == "<b>Alice & Bob</b>"
    assert speaker.textFormat() == dialogue.textFormat() == Qt.TextFormat.PlainText
    assert speaker.geometry().bottom() < dialogue.geometry().top()
    assert video.rect().contains(video.subtitle_overlay.geometry())
    assert video.subtitle_overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert video.childAt(
        video.subtitle_overlay.mapTo(video, QPoint(1, 1))
    ) is not video.subtitle_overlay
    video.set_position(1)
    assert video.subtitle_overlay.findChild(QLabel, "subtitleCaption") is dialogue
    video.resize(1, 450)
    assert video.width() == 1
    assert not video.subtitle_overlay.isVisible()
    video.resize(640, 450)
    assert video.subtitle_overlay.isVisible()


def test_video_frame_and_subtitles_are_composited_together(qtbot) -> None:
    video = SubtitleVideoWidget()
    qtbot.addWidget(video)
    video.resize(640, 360)
    video.show()
    frame_image = QImage(640, 360, QImage.Format.Format_RGB32)
    frame_image.fill(QColor("#32527c"))
    video.videoSink().setVideoFrame(QVideoFrame(frame_image))
    video.set_segments([Segment(0, 3, "Visible dialogue", ["Visible speaker"])])

    def video_is_painted() -> bool:
        image = video.grab().toImage()
        return image.pixelColor(image.width() // 2, image.height() // 2).name() == "#32527c"

    qtbot.waitUntil(video_is_painted)
    image = video.grab().toImage()
    origin = video.subtitle_overlay.mapTo(video, QPoint(0, 0))
    scale = image.devicePixelRatio()
    image = image.copy(
        round(origin.x() * scale), round(origin.y() * scale),
        round(video.subtitle_overlay.width() * scale),
        round(video.subtitle_overlay.height() * scale),
    )
    colors = [
        image.pixelColor(x, y)
        for y in range(image.height())
        for x in range(image.width())
    ]
    assert any(
        color.red() < 180 and color.green() > 200 and color.blue() > 200
        for color in colors
    ), "Speaker text must be painted above the video, not hidden behind a native surface"
    assert any(
        color.red() > 220 and color.green() > 220 and color.blue() > 220
        for color in colors
    ), "Dialogue must be painted above the video"
