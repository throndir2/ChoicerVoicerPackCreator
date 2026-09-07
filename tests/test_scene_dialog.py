from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QFileDialog

from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui import main_window
from choicer_voicer_pack_creator.ui.main_window import MainWindow, ProjectEditor
from choicer_voicer_pack_creator.ui.scene_dialog import SceneEditDialog
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


@pytest.fixture
def source_project(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic GUI fixture")
    return PackProject(
        title="Whole video", authors=["Tester"], video_path=str(source), video_duration=10,
        segments=[Segment(2, 4, "Scene line", ["Actor"])], auto_speaker_matching=False,
    )


@pytest.mark.parametrize("mode", ["cut", "extract"])
@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_range_dialog_previews_without_mutating_project(
    qtbot, source_project, mode, stylesheet,
):
    before = source_project.to_dict()
    dialog = SceneEditDialog(source_project, 2, 4, mode)
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(stylesheet)
    dialog.show()
    assert not dialog.isModal()
    assert (dialog.start_spin.value(), dialog.end_spin.value()) == (2, 4)
    assert dialog.start_spin.decimals() == dialog.end_spin.decimals() == 3
    assert dialog.title_edit.isVisible() is (mode == "extract")
    assert dialog.apply_button.isEnabled()
    assert dialog.summary_label.text()
    for button in (dialog.preview_button, dialog.apply_button, dialog.cancel_button):
        assert not button.visibleRegion().isEmpty()
    with qtbot.waitSignal(dialog.preview_requested) as preview:
        qtbot.mouseClick(dialog.preview_button, Qt.MouseButton.LeftButton)
    assert preview.args == [2.0, 4.0]
    dialog.start_spin.setValue(4)
    assert not dialog.apply_button.isEnabled()
    assert dialog.error_label.isVisible()
    dialog.start_spin.setValue(1)
    assert dialog.apply_button.isEnabled()
    if mode == "extract":
        dialog.title_edit.clear()
        assert not dialog.apply_button.isEnabled()
    dialog.reject()
    assert source_project.to_dict() == before


def test_dialog_blocks_whole_video_deletion_and_partial_preserved_audio(
    qtbot, source_project,
):
    dialog = SceneEditDialog(source_project, 0, 10, "cut")
    qtbot.addWidget(dialog)
    assert not dialog.apply_button.isEnabled()
    source_project.segments[0].audio_mode = "file"
    source_project.segments[0].audio_path = "recording.mp3"
    dialog.start_spin.setValue(3)
    assert not dialog.apply_button.isEnabled()
    assert dialog.error_label.text()
    dialog.start_spin.setValue(4)
    assert dialog.apply_button.isEnabled()


@pytest.fixture
def editor(qtbot, tmp_path, source_project, monkeypatch):
    monkeypatch.setattr(ProjectEditor, "_start_waveform", lambda *_args: None)
    window = MainWindow(
        object(), settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    editor = window.active_editor
    editor._set_project(source_project, tmp_path / "original.cvpack.json", mark_dirty=False)
    window.show()
    yield editor
    for job in window.job_manager.active_jobs():
        window.job_manager.cancel(job.id)
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=10000)
    for box in list(window._decisions):
        box.reject()
    for current in window.editors.values():
        current._commit_editors()
        current.dirty = False
        current._recovery_timer.stop()
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=10000)


def test_scene_actions_use_selection_and_do_not_expand_toolbar(editor, qtbot):
    window = editor.workspace
    for action in (editor.action_cut_video, editor.action_extract_scene):
        assert action in window.project_menu.actions()
        assert action not in editor.project_toolbar.actions()
        assert action.isEnabled()
    editor.select_segment(editor.project.segments[0].id)
    editor.action_extract_scene.trigger()
    dialog = editor._scene_dialog
    assert dialog.mode == "extract"
    assert (dialog.start_spin.value(), dialog.end_spin.value()) == (2, 4)
    editor.action_extract_scene.trigger()
    assert editor._scene_dialog is dialog
    editor.action_cut_video.trigger()
    assert editor._scene_dialog.mode == "cut"
    editor._scene_dialog.reject()
    editor._set_loading(True)
    assert not editor.action_cut_video.isEnabled()
    assert not editor.action_extract_scene.isEnabled()
    editor._set_loading(False)
    assert editor.action_cut_video.isEnabled()
    editor._set_project(PackProject(), None, mark_dirty=False)
    assert not editor.action_extract_scene.isEnabled()


def test_apply_requires_review_after_project_changes(editor, monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(editor, "_start_scene_edit", lambda *args, **kwargs: launched.append(args))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path))
    editor.open_scene_dialog("cut")
    dialog = editor._scene_dialog
    editor.title_edit.setText("Changed title")
    dialog.apply_button.click()
    assert not launched
    assert "project changed" in dialog.error_label.text().lower()
    dialog.apply_button.click()
    assert len(launched) == 1
    assert editor._scene_dialog is None


def test_cancelling_destination_and_replacing_source_do_not_start_edit(editor, monkeypatch):
    launched = []
    monkeypatch.setattr(editor, "_start_scene_edit", lambda *args, **kwargs: launched.append(args))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: "")
    editor.open_scene_dialog("extract")
    editor._scene_dialog.apply_button.click()
    assert not launched
    assert editor._scene_dialog.isVisible()
    editor._set_project(PackProject(), None, mark_dirty=False)
    assert editor._scene_dialog is None


def test_hiding_project_closes_its_pending_scene_dialog(editor):
    editor.open_scene_dialog("extract")
    editor.workspace._hide_editor(editor, retain=True)
    assert editor._scene_dialog is None
    assert editor.session.hidden


def test_player_duration_only_fills_unknown_project_duration(editor):
    editor._player_duration_changed(10067)
    assert editor.project.video_duration == 10
    editor.project.video_duration = 0
    editor._player_duration_changed(4000)
    assert editor.project.video_duration == 4


def fake_result(project, _start, _end, _mode, output, _media, *, title=None):
    output.mkdir(exist_ok=True)
    result = PackProject.from_dict(project.to_dict())
    result.title = title or project.title
    video = output / "edited.mkv"
    video.write_bytes(b"synthetic edited fixture")
    result.video_path = str(video)
    result.video_duration = 2
    result.segments[0].start, result.segments[0].end = 0, 2
    ProjectStore.save(result, output / "project.cvpack.json")
    return result


@pytest.mark.parametrize("mode", ["cut", "extract"])
def test_background_result_updates_only_intended_project(
    editor, qtbot, monkeypatch, tmp_path, mode,
):
    monkeypatch.setattr(main_window, "execute_scene_edit", fake_result)
    window = editor.workspace
    before = editor.project.to_dict()
    original_path = editor.project_path
    editor._start_scene_edit(2, 4, mode, tmp_path / "result", title="New scene" if mode == "extract" else None)
    assert not editor.action_cut_video.isEnabled()
    job = editor._scene_job
    other = window.add_project(PackProject(title="Other tab"), dirty=False)
    qtbot.waitUntil(lambda: editor._scene_job is None)
    assert job.record.state == "succeeded"
    assert other.project.title == "Other tab"
    assert editor.action_cut_video.isEnabled()
    if mode == "cut":
        assert editor.project_path == original_path
        assert editor.project.video_duration == 2
        assert editor.dirty
        assert window.active_editor is other
    else:
        assert editor.project.to_dict() == before
        assert not editor.dirty
        scene = window.active_editor
        assert scene is not editor and scene is not other
        assert scene.project.title == "New scene"
        assert scene.project_path == tmp_path / "result" / "project.cvpack.json"
        assert ProjectStore.load(scene.project_path).to_dict() == scene.project.to_dict()
        assert not scene.dirty
        assert scene.project_path in window._recent_project_paths()


def test_cut_completion_never_overwrites_concurrent_edits(editor, qtbot, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()

    def execute(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release the scene job")
        return fake_result(*args, **kwargs)

    monkeypatch.setattr(main_window, "execute_scene_edit", execute)
    editor._start_scene_edit(2, 4, "cut", tmp_path / "result")
    try:
        qtbot.waitUntil(entered.is_set)
        editor.title_edit.setText("Keep this new title")
        editor._commit_editors()
    finally:
        release.set()
    qtbot.waitUntil(lambda: editor._scene_job is None)
    assert editor.project.title == "Keep this new title"
    assert editor.project.video_duration == 10
    scene = editor.workspace.active_editor
    assert scene is not editor
    assert scene.project.video_duration == 2
    assert scene.project.title == "Whole video"
    assert any("opened separately" in box.windowTitle() for box in editor.workspace._decisions)


def test_cut_completion_keeps_replaced_source_and_creates_saved_copy(
    editor, qtbot, monkeypatch, tmp_path,
):
    entered, release = threading.Event(), threading.Event()

    def execute(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release the scene job")
        return fake_result(*args, **kwargs)

    monkeypatch.setattr(main_window, "execute_scene_edit", execute)
    editor._start_scene_edit(2, 4, "cut", tmp_path / "result")
    try:
        qtbot.waitUntil(entered.is_set)
        editor._set_project(PackProject(title="Replacement"), None, mark_dirty=False)
    finally:
        release.set()
    qtbot.waitUntil(lambda: editor._scene_job is None)
    assert editor.project.title == "Replacement"
    assert editor.project.video_path == ""
    assert editor.workspace.active_editor.project.video_duration == 2
    assert editor.workspace.active_editor.project_path.is_file()


@pytest.mark.parametrize("failure", [ValueError("Cannot process fixture"), OperationCancelled()])
def test_failure_or_cancellation_keeps_original(editor, qtbot, monkeypatch, tmp_path, failure):
    def execute(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(main_window, "execute_scene_edit", execute)
    before = editor.project.to_dict()
    editor._start_scene_edit(2, 4, "cut", tmp_path / "result")
    job = editor._scene_job
    qtbot.waitUntil(lambda: editor._scene_job is None)
    assert job.record.state == ("cancelled" if isinstance(failure, OperationCancelled) else "failed")
    assert editor.project.to_dict() == before
    assert not editor.dirty
    assert editor.action_cut_video.isEnabled()
    assert "original project was not changed" in editor.statusBar().currentMessage()
    assert not (tmp_path / "result").exists()


def test_multiple_scenes_keep_source_available(editor, qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(main_window, "execute_scene_edit", fake_result)
    before = editor.project.to_dict()
    for name in ("First scene", "Second scene"):
        editor._start_scene_edit(2, 4, "extract", tmp_path / name, title=name)
        qtbot.waitUntil(lambda: editor._scene_job is None)
    titles = [item.project.title for item in editor.workspace.editors.values()]
    assert titles == ["Whole video", "First scene", "Second scene"]
    assert editor.project.to_dict() == before
    assert Path(editor.project.video_path).read_bytes() == b"synthetic GUI fixture"


@pytest.mark.integration
@pytest.mark.parametrize("start,end", [(0, 2), (0.413, 1.717)])
def test_cut_media_plays_the_retained_scene_in_editor(
    editor, qtbot, tmp_path, monkeypatch, start, end,
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not available")
    media = MediaTools()
    source = tmp_path / "red-blue.mkv"
    media.run([
        media.ffmpeg, "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=160x120:r=15:d=2",
        "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=15:d=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "ffv1", str(source),
    ], "Creating scene playback fixture")
    editor.media = media
    editor._set_project(PackProject(
        title="Cut playback", authors=["Tester"], video_path=str(source), video_duration=4,
        segments=[Segment(2.25, 3.5, "Blue scene", ["Actor"])], auto_speaker_matching=False,
    ), None, mark_dirty=False)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path))
    editor.mark_in_spin.setValue(start)
    editor.mark_out_spin.setValue(end)
    editor.action_cut_video.trigger()
    editor._scene_dialog.apply_button.click()
    job = editor._scene_job
    qtbot.waitUntil(lambda: editor._scene_job is None, timeout=30000)
    assert job.record.state == "succeeded", job.record.error
    duration = 4 - (end - start)
    assert editor.project.video_duration == pytest.approx(duration)
    assert editor.project.segments[0].start == pytest.approx(2.25 - (end - start))
    assert editor.project.segments[0].end == pytest.approx(3.5 - (end - start))
    qtbot.waitUntil(lambda: editor.player.mediaStatus() in {
        QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
    }, timeout=6000)
    colors = []

    def frame_changed(frame):
        image = frame.toImage()
        if not image.isNull():
            colors.append(image.pixelColor(image.width() // 2, image.height() // 2))

    editor.video_widget.videoSink().videoFrameChanged.connect(frame_changed)
    editor.seek(1)
    qtbot.waitUntil(lambda: any(
        color.blue() > 150 and color.red() < 50 for color in colors
    ), timeout=6000)
    assert editor.project.video_duration == pytest.approx(duration)
    assert editor.dirty
    assert source.is_file()
