from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, QUrl, Slot
from PySide6.QtGui import QColor
from PySide6.QtMultimedia import QMediaPlayer
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
    assert window.editor_splitter.handleWidth() == 9
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
