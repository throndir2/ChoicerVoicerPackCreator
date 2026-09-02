from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, Slot
from PySide6.QtWidgets import QMessageBox

from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui.main_window import (
    ExportWorker,
    MainWindow,
    WaveformWorker,
)
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget


class UnusedMedia:
    def probe_audio_duration(self, _path: Path) -> float:
        return 1.75


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


def test_inspector_sections_resize_collapse_and_restore(qtbot, tmp_path: Path) -> None:
    settings_path = tmp_path / "layout.ini"
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    window = MainWindow(UnusedMedia(), settings=settings)  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window.inspector_splitter.height() > 300)

    window.inspector_splitter.setSizes([180, 420, 170])
    window.project_section.set_collapsed(True)
    qtbot.waitUntil(lambda: window.project_section.is_collapsed)
    saved_expanded_height = window.project_section.last_expanded_height
    assert window.project_section.body.isHidden()
    assert window.inspector_splitter.sizes()[1] > window.inspector_splitter.sizes()[0]
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
    assert not restored.project_section.body.isHidden()
    restored.close()


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
    window.recovery_store = recovery
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
    window.recovery_store = recovery
    window._set_project(project, None, mark_dirty=True)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard),
    )

    assert window._maybe_save()
    assert recovery.path.is_file()
    window._set_project(PackProject(authors=["Creator"]), None, mark_dirty=False)
    assert not recovery.path.exists()
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
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes),
    )
    window = MainWindow(UnusedMedia())  # type: ignore[arg-type]
    qtbot.addWidget(window)
    window.open_path(path)

    assert window.project.title == "First"
    assert window.project_path == path.resolve()
    assert window.dirty
    assert path.read_text(encoding="utf-8") == "corrupt"
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


def test_worker_results_are_delivered_on_gui_thread(qtbot, tmp_path: Path) -> None:
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
            return object()

    class TrackingWindow(MainWindow):
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

    window = TrackingWindow(ImmediateMedia())  # type: ignore[arg-type]
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
