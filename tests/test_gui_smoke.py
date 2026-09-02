from __future__ import annotations

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget


class UnusedMedia:
    pass


def test_main_window_starts_with_empty_editor(qtbot) -> None:
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.show()
    assert window.project.title == "Untitled Dub Pack"
    assert window.segment_table.rowCount() == 0
    assert "Choicer Voicer Pack Creator" in window.windowTitle()
    window.dirty = False
    window.close()


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
