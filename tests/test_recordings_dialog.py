from __future__ import annotations

import shutil
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.operations import SourceChangedError, check_cancelled
from choicer_voicer_pack_creator.ui import recordings_dialog
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.recordings_dialog import GAME_LOCATION_SETTING, RecordingsDialog
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


@pytest.fixture
def workspace(qtbot, tmp_path):
    window = MainWindow(
        object(), settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )

    def close(current):
        for job in current.job_manager.active_jobs():
            current.job_manager.cancel(job.id)
        qtbot.waitUntil(lambda: not current.job_manager.active_jobs(), timeout=10000)
        for box in list(current._decisions):
            box.reject()
        for editor in current.editors.values():
            editor.dirty = False
            editor._recovery_timer.stop()
        current.close()
        qtbot.waitUntil(lambda: current._close_approved and not current.isVisible(), timeout=10000)

    qtbot.addWidget(window, before_close_func=close)
    window.show()
    return window


def wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\x00\x00" * 480)


@pytest.fixture
def game(tmp_path):
    root = tmp_path / "game"
    pack = root / "packs_voice" / "Demon"
    take = root / "recordings" / "dub_recordings" / "Demon" / "2026-08-25 19_55_56"
    pack.mkdir(parents=True)
    take.mkdir(parents=True)
    (pack / "_pack_info.ini").write_text('[data]\ntitle="Demon"\nauthors=["Tester"]\n')
    (pack / "line.ini").write_text(
        '[data]\ncaption="Synthetic line"\ndub_timestamps=[0.25]\n'
        'dub_characters=["Actor"]\nimage="actor.png"\n'
    )
    (pack / "dub_video.ogv").write_bytes(b"metadata-only fixture")
    wav(pack / "line.wav")
    wav(take / "_dubrecord_line.wav")
    return root, pack, take


def wait_idle(qtbot, dialog):
    qtbot.waitUntil(lambda: dialog._job is None, timeout=15000)


def load_game(workspace, qtbot, game):
    dialog = RecordingsDialog(workspace)
    workspace.recordings_dialog = dialog
    dialog.show()
    dialog.load_game_location(str(game[0]))
    wait_idle(qtbot, dialog)
    return dialog


def test_location_setup_is_lazy_global_and_nonmodal(workspace, monkeypatch, tmp_path):
    looked_up = []
    monkeypatch.setattr(
        recordings_dialog, "default_game_location",
        lambda: looked_up.append(True) or tmp_path / "suggested-game",
    )
    assert workspace.recordings_dialog is None
    assert not looked_up
    before = workspace.active_editor.project.to_dict()
    workspace.action_recordings.trigger()
    dialog = workspace.recordings_dialog
    assert dialog is not None and not dialog.isModal()
    assert dialog._location_dialog.isVisible()
    assert dialog._location_dialog.path.text() == str(tmp_path / "suggested-game")
    assert len(looked_up) == 1
    assert not workspace.settings.contains(GAME_LOCATION_SETTING)
    dialog._location_dialog.reject()
    dialog.close()
    workspace.action_recordings.trigger()
    assert workspace.recordings_dialog is dialog
    assert not dialog._location_dialog.isVisible()
    assert workspace.active_editor.project.to_dict() == before
    assert not workspace.active_editor.dirty
    assert workspace.action_recordings not in workspace.active_editor.project_toolbar.actions()


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_library_discovers_takes_without_loading_media(workspace, qtbot, game, stylesheet):
    workspace.setStyleSheet(stylesheet)
    dialog = load_game(workspace, qtbot, game)
    assert dialog.table.rowCount() == 1
    assert dialog._selected_pack().path == game[1]
    assert dialog._take.path == game[2]
    assert workspace.settings.value(GAME_LOCATION_SETTING) == str(game[0])
    assert dialog.play_button.isEnabled()
    assert dialog.export_button.isEnabled()
    assert "Voices only" in dialog.backing_label.text()
    assert not dialog.progress.isVisible()
    assert not dialog.open_output_button.isVisible()
    assert not dialog.play_button.visibleRegion().isEmpty()
    assert not dialog.export_button.visibleRegion().isEmpty()
    assert dialog.player.source().isEmpty()


def test_missing_saved_location_prompts_on_use_not_startup(workspace, qtbot, tmp_path):
    missing = str(tmp_path / "disconnected-drive")
    workspace.settings.setValue(GAME_LOCATION_SETTING, missing)
    assert workspace.recordings_dialog is None
    workspace.action_recordings.trigger()
    dialog = workspace.recordings_dialog
    wait_idle(qtbot, dialog)
    assert dialog._location_dialog.isVisible()
    assert dialog._location_dialog.path.text() == missing
    assert "unavailable" in dialog._location_dialog.error.text().lower()
    assert workspace.settings.value(GAME_LOCATION_SETTING) == missing
    assert not dialog.play_button.isEnabled()


def test_standalone_recordings_do_not_require_game_setup(workspace, qtbot, game):
    dialog = RecordingsDialog(workspace)
    workspace.recordings_dialog = dialog
    dialog.load_recording_folder(game[2].parent)
    wait_idle(qtbot, dialog)
    assert dialog.table.rowCount() == 1
    assert not dialog.play_button.isEnabled()
    dialog.load_pack(game[1])
    wait_idle(qtbot, dialog)
    assert dialog.play_button.isEnabled()
    assert dialog._selected_pack().path == game[1]
    assert not workspace.settings.contains(GAME_LOCATION_SETTING)
    assert dialog._location_dialog is None


def test_preview_and_export_share_plan_and_different_volume_controls(
    workspace, qtbot, game, monkeypatch, tmp_path,
):
    dialog = load_game(workspace, qtbot, game)
    prepared, rendered, played = [], [], []
    sources = []
    monkeypatch.setattr(dialog.player, "setSource", sources.append)
    monkeypatch.setattr(dialog.player, "play", lambda: played.append(True))
    monkeypatch.setattr(dialog.player, "pause", lambda: None)

    def prepare(media, pack, take, **options):
        plan = SimpleNamespace(
            pack=pack, take=take, duration=2.0, warnings=(), clips=(), options=options,
        )
        prepared.append(plan)
        return plan

    def render(media, plan, destination, **options):
        destination.write_bytes(b"synthetic render")
        rendered.append((plan, destination, options))
        return SimpleNamespace(path=destination, warnings=())

    monkeypatch.setattr(recordings_dialog, "prepare_recording", prepare)
    monkeypatch.setattr(recordings_dialog, "render_recording", render)
    dialog.voices_gain.setValue(125)
    dialog.backing_gain.setValue(75)
    dialog.toggle_playback()
    wait_idle(qtbot, dialog)
    assert len(prepared) == len(rendered) == len(played) == 1
    assert prepared[0].options["voices_gain"] == 1.25
    assert prepared[0].options["backing_gain"] == 0.75
    assert rendered[0][2]["format"] == "preview"
    assert Path(sources[-1].toLocalFile()) == rendered[0][1]
    dialog.volume.setValue(23)
    assert dialog._plan is prepared[0]
    target = tmp_path / "recording.mp4"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    dialog.export_video()
    wait_idle(qtbot, dialog)
    assert len(prepared) == 1
    assert rendered[1][0] is rendered[0][0]
    assert rendered[1][2] == {"format": "mp4", "overwrite": False}
    assert dialog.open_output_button.isVisible()
    assert dialog.show_output_button.isVisible()
    assert target.read_bytes() == b"synthetic render"
    dialog.voices_gain.setValue(100)
    assert dialog._plan is None
    assert dialog._preview_path is None
    assert not dialog.open_output_button.isVisible()


def test_warning_confirmation_can_cancel_without_rendering(workspace, qtbot, game, monkeypatch):
    dialog = load_game(workspace, qtbot, game)
    plan = SimpleNamespace(
        pack=dialog._selected_pack(), take=dialog._take, duration=2.0, clips=(),
        warnings=("One recording extends past the video and will be cut off.",),
    )
    monkeypatch.setattr(recordings_dialog, "prepare_recording", lambda *_args, **_kwargs: plan)
    renders = []
    monkeypatch.setattr(
        recordings_dialog, "render_recording",
        lambda *_args, **_kwargs: renders.append(True),
    )
    dialog.toggle_playback()
    wait_idle(qtbot, dialog)
    assert dialog._deciding
    assert not dialog.export_button.isEnabled()
    box = workspace._decisions[-1]
    assert "cut off" in box.text()
    box.done(QMessageBox.StandardButton.Cancel)
    assert not dialog._deciding
    assert not renders
    assert dialog.play_button.isEnabled()


def test_background_tasks_survive_hiding_and_can_be_cancelled(workspace, qtbot, game):
    dialog = load_game(workspace, qtbot, game)
    started, release = threading.Event(), threading.Event()

    def run(_ctx):
        started.set()
        while not release.wait(0.01):
            check_cancelled()
        check_cancelled()

    dialog._submit("Synthetic recording work", run, lambda _result: None)
    qtbot.waitUntil(started.is_set)
    handle = dialog._job
    assert handle.record.project_id is None
    assert handle.id in workspace.tasks_window._details
    dialog.close()
    assert handle.record.active
    handle.cancel()
    release.set()
    wait_idle(qtbot, dialog)
    assert handle.record.state == "cancelled"
    assert "cancelled" in dialog.status_label.text()
    assert not workspace.active_editor.statusBar()._activities


def test_pack_changes_invalidate_preview_but_not_project(workspace, qtbot, game):
    dialog = load_game(workspace, qtbot, game)
    before = workspace.active_editor.project.to_dict()
    dialog._plan = SimpleNamespace()
    dialog._preview_path = Path("previous.mkv")
    dialog._custom_backing = Path("backing.wav")
    dialog.reset_backing()
    assert dialog._plan is None
    assert dialog._preview_path is None
    assert dialog._custom_backing is None
    assert workspace.active_editor.project.to_dict() == before
    assert not workspace.active_editor.dirty


def test_recording_and_editor_playback_are_mutually_exclusive(
    workspace, qtbot, game, monkeypatch,
):
    dialog = load_game(workspace, qtbot, game)
    events = []
    editor = workspace.active_editor
    monkeypatch.setattr(editor.player, "pause", lambda: events.append("editor paused"))
    monkeypatch.setattr(editor.prompt_player, "stop", lambda: events.append("prompt stopped"))
    monkeypatch.setattr(dialog.player, "play", lambda: events.append("recording played"))
    monkeypatch.setattr(dialog.player, "pause", lambda: events.append("recording paused"))
    dialog._play()
    assert events == ["editor paused", "prompt stopped", "recording played"]
    events.clear()
    editor._playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
    assert events == ["recording paused"]
    events.clear()
    editor.prompt_player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)
    assert events == ["recording paused"]


def test_cached_preview_checks_sources_before_playing(workspace, qtbot, game, monkeypatch):
    dialog = load_game(workspace, qtbot, game)
    played = []
    monkeypatch.setattr(dialog.player, "play", lambda: played.append(True))

    def changed():
        raise SourceChangedError("Recording changed. Refresh the recording library.")

    dialog._plan = SimpleNamespace(verify_sources=changed)
    dialog._preview_path = Path("cached.mkv")
    dialog.toggle_playback()
    wait_idle(qtbot, dialog)
    assert not played
    assert "Refresh" in dialog.status_label.text()


@pytest.mark.parametrize("interrupt", ["hide", "editor"])
def test_new_playback_intent_wins_over_background_preview(
    workspace, qtbot, game, monkeypatch, interrupt,
):
    dialog = load_game(workspace, qtbot, game)
    started, release = threading.Event(), threading.Event()
    played = []
    monkeypatch.setattr(dialog.player, "play", lambda: played.append(True))

    def verify():
        started.set()
        while not release.wait(0.01):
            check_cancelled()

    dialog._plan = SimpleNamespace(verify_sources=verify)
    dialog._preview_path = Path("cached.mkv")
    dialog.toggle_playback()
    qtbot.waitUntil(started.is_set)
    if interrupt == "hide":
        dialog.close()
    else:
        workspace.pause_recording_playback()
    release.set()
    wait_idle(qtbot, dialog)
    assert not played


def test_overwrite_needs_permission_and_detects_changes_during_confirmation(
    workspace, qtbot, game, monkeypatch, tmp_path,
):
    dialog = load_game(workspace, qtbot, game)
    plan = SimpleNamespace(take=dialog._take, warnings=(), clips=())
    dialog._plan = plan
    target = tmp_path / "existing.mp4"
    target.write_bytes(b"original export")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    rendered = []
    monkeypatch.setattr(
        recordings_dialog, "render_recording",
        lambda *_args, **_kwargs: rendered.append(True),
    )
    dialog.export_video()
    assert dialog._deciding
    workspace._decisions[-1].done(QMessageBox.StandardButton.Cancel)
    assert not rendered and target.read_bytes() == b"original export"
    dialog.export_video()
    target.write_bytes(b"changed while deciding")
    workspace._decisions[-1].done(QMessageBox.StandardButton.Yes)
    wait_idle(qtbot, dialog)
    assert not rendered
    assert target.read_bytes() == b"changed while deciding"
    assert "changed" in dialog.status_label.text().lower()


@pytest.mark.integration
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Requires local FFmpeg")
def test_real_recording_player_previews_seeks_and_exports(workspace, qtbot, game, monkeypatch, tmp_path):
    media = MediaTools()
    workspace.media = media
    media.run([
        media.ffmpeg, "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=duration=3:size=160x90:rate=12",
        "-f", "lavfi", "-i", "sine=frequency=800:duration=3",
        "-c:v", "libtheora", "-c:a", "libvorbis", str(game[1] / "dub_video.ogv"),
    ], "Creating synthetic recording video")
    dialog = load_game(workspace, qtbot, game)
    errors = []
    dialog.player.errorOccurred.connect(lambda _error, message: errors.append(message))
    dialog.toggle_playback()
    wait_idle(qtbot, dialog)
    if dialog._deciding:
        workspace._decisions[-1].done(QMessageBox.StandardButton.Yes)
    wait_idle(qtbot, dialog)
    assert dialog._preview_path is not None, dialog.status_label.text()
    qtbot.waitUntil(lambda: dialog.player.position() > 0 or bool(errors), timeout=10000)
    assert not errors
    dialog.player.pause()
    dialog.seek.setValue(1500)
    qtbot.waitUntil(lambda: dialog.player.position() >= 1400, timeout=10000)
    target = tmp_path / "real-recording.mp4"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    dialog.export_video()
    if dialog._deciding:
        workspace._decisions[-1].done(QMessageBox.StandardButton.Yes)
    wait_idle(qtbot, dialog)
    assert dialog._export_path == target, dialog.status_label.text()
    output = media.probe(target)
    assert (output.width, output.height, output.fps) == (160, 90, 12)
    assert output.duration == pytest.approx(3, abs=0.1)
    assert output.audio_codec == "aac"
