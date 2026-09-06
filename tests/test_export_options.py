from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox
from shiboken6 import isValid

from choicer_voicer_pack_creator.exporter import ExportResult, PackExporter
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui.export_options_dialog import ExportOptions, ExportOptionsDialog
from choicer_voicer_pack_creator.ui.main_window import MainWindow
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


@pytest.mark.parametrize("height,fps,index", [
    (480, 30, 0), (720, 30, 1), (720, 60, 2), (360, 24, 2), (1080, 60, 2),
])
def test_options_open_with_current_profile_without_mutation(qtbot, height, fps, index):
    project = PackProject(
        video_height=height, video_fps=fps, preserve_source_video=True,
        head_padding=0.1234, tail_padding=0.5678,
    )
    before = project.to_dict()
    dialog = ExportOptionsDialog(project)
    qtbot.addWidget(dialog)
    assert dialog.quality_combo.currentIndex() == index
    assert dialog.height_spin.value() == height
    assert dialog.fps_spin.value() == fps
    assert dialog.height_spin.isEnabled() is (index == 2)
    assert f"{height}p at {fps} FPS" in dialog.current_label.text()
    assert dialog.options() == ExportOptions.from_project(project)
    assert project.to_dict() == before
    assert dialog.advanced.is_collapsed


def test_fast_higher_quality_custom_and_padding_controls(qtbot):
    project = PackProject()
    dialog = ExportOptionsDialog(project)
    qtbot.addWidget(dialog)
    assert dialog.quality_combo.currentIndex() == 0
    assert dialog.options() == ExportOptions(480, 30, False, 0.15, 0.25)
    dialog.quality_combo.setCurrentIndex(1)
    assert dialog.options() == ExportOptions(720, 30, False, 0.15, 0.25)
    assert "slower" in dialog.quality_note.text()
    dialog.quality_combo.setCurrentIndex(2)
    assert dialog.height_spin.isEnabled() and dialog.fps_spin.isEnabled()
    assert dialog.height_spin.value() == 720
    dialog.height_spin.setValue(1080)
    dialog.fps_spin.setValue(60)
    dialog.advanced.set_collapsed(False)
    dialog.head_pad_spin.setValue(0.3)
    dialog.tail_pad_spin.setValue(0.4)
    assert dialog.options() == ExportOptions(1080, 60, False, 0.3, 0.4)
    assert (dialog.height_spin.minimum(), dialog.height_spin.maximum()) == (144, 2160)
    assert (dialog.fps_spin.minimum(), dialog.fps_spin.maximum()) == (1, 120)
    for spin in (dialog.head_pad_spin, dialog.tail_pad_spin):
        assert (spin.minimum(), spin.maximum(), spin.decimals(), spin.singleStep()) == (0, 2, 3, 0.025)
        assert spin.accessibleName()
    assert ExportOptions.from_project(project) == ExportOptions(480, 30, False, 0.15, 0.25)


@pytest.mark.parametrize("changed", ["height", "fps", "preset"])
def test_imported_copy_preference_clears_on_profile_change_and_returns_on_restore(qtbot, changed):
    project = PackProject(video_height=720, video_fps=60, preserve_source_video=True)
    dialog = ExportOptionsDialog(project)
    qtbot.addWidget(dialog)
    if changed == "preset":
        dialog.quality_combo.setCurrentIndex(0)
    elif changed == "height":
        dialog.height_spin.setValue(480)
    else:
        dialog.fps_spin.setValue(30)
    assert not dialog.preserve_check.isEnabled()
    assert not dialog.options().preserve_source_video
    assert "copying is off" in dialog.preserve_note.text()
    dialog.quality_combo.setCurrentIndex(2)
    dialog.height_spin.setValue(720)
    dialog.fps_spin.setValue(60)
    assert dialog.preserve_check.isEnabled()
    assert dialog.options().preserve_source_video
    dialog.preserve_check.setChecked(False)
    dialog.fps_spin.setValue(30)
    dialog.fps_spin.setValue(60)
    assert not dialog.options().preserve_source_video
    assert project.preserve_source_video


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
def test_dialog_buttons_and_advanced_controls_are_visible_and_keyboard_accessible(qtbot, stylesheet):
    dialog = ExportOptionsDialog(PackProject())
    qtbot.addWidget(dialog)
    dialog.setStyleSheet(stylesheet)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    assert dialog.isModal()
    assert "location" in dialog.continue_button.text()
    for widget in (dialog.quality_combo, dialog.continue_button, dialog.cancel_button):
        assert widget.isVisible() and not widget.visibleRegion().isEmpty()
    dialog.advanced.toggle_button.setFocus()
    qtbot.keyClick(dialog.advanced.toggle_button, Qt.Key.Key_Space)
    assert not dialog.advanced.is_collapsed
    qtbot.waitUntil(lambda: not dialog.head_pad_spin.visibleRegion().isEmpty())
    assert not dialog.tail_pad_spin.visibleRegion().isEmpty()
    qtbot.keyClick(dialog.cancel_button, Qt.Key.Key_Escape)
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Rejected


@pytest.fixture
def editor(qtbot, tmp_path, monkeypatch):
    window = MainWindow(
        SimpleNamespace(),
        settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        analysis_data_root=tmp_path / "analysis",
    )
    qtbot.addWidget(window)
    editor = window.active_editor
    monkeypatch.setattr(editor, "_start_waveform", lambda *_args: None)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic fixture")
    editor._set_project(PackProject(
        title="Options fixture", authors=["Tester"], video_path=str(source), video_duration=3,
        segments=[Segment(0.5, 1.5, "Synthetic line", ["Actor"])],
    ), None, mark_dirty=False)
    window.show()
    yield editor
    qtbot.waitUntil(lambda: not window.job_manager.active_jobs(), timeout=60000)
    monkeypatch.undo()
    editor._recovery_timer.stop()
    editor.dirty = False
    window.close()
    qtbot.waitUntil(lambda: not isValid(window) or not window.isVisible())


@pytest.mark.parametrize("cancel", ["button", "escape", "close"])
def test_gui_cancel_does_not_mutate_or_start_any_job(editor, qtbot, monkeypatch, cancel):
    before, revision = editor.project.to_dict(), editor.session.revision
    settings = {key: editor.settings.value(key) for key in editor.settings.allKeys()}
    commits = []
    commit = editor._commit_editors
    monkeypatch.setattr(editor, "_commit_editors", lambda: (commits.append(True), commit()))
    monkeypatch.setattr(editor, "_confirm_backing_export", lambda: pytest.fail("Started backing"))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: pytest.fail("Destination"))

    def cancel_options():
        dialog = editor._export_options_dialog
        assert dialog is not None
        dialog.quality_combo.setCurrentIndex(1)
        dialog.head_pad_spin.setValue(0.4)
        if cancel == "button":
            qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
        elif cancel == "escape":
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            dialog.close()

    QTimer.singleShot(0, cancel_options)
    editor.action_export.trigger()
    assert editor._export_options_dialog is None
    assert not commits
    assert editor._export_worker is None
    assert editor.project.to_dict() == before
    assert editor.session.revision == revision
    assert not editor.dirty
    assert not editor.workspace.job_manager.tasks(editor.session.id)
    assert settings == {key: editor.settings.value(key) for key in editor.settings.allKeys()}


@pytest.mark.parametrize("choice", ["unchanged", "higher", "custom"])
def test_accepted_options_reach_export_snapshot_and_explicit_save(
    editor, qtbot, tmp_path, monkeypatch, choice,
):
    calls, order = [], []
    revision = editor.session.revision

    def choose(dialog):
        assert not editor.workspace.job_manager.tasks(editor.session.id)
        order.append("options")
        if choice != "unchanged":
            dialog.quality_combo.setCurrentIndex(1 if choice == "higher" else 2)
            if choice == "custom":
                dialog.height_spin.setValue(1080)
                dialog.fps_spin.setValue(60)
            dialog.head_pad_spin.setValue(0.35)
            dialog.tail_pad_spin.setValue(0.45)
        return QDialog.DialogCode.Accepted

    def export(project, destination, **_kwargs):
        calls.append(project)
        order.append("export")
        return ExportResult(destination / "Pack", destination / "Pack.zip", {
            "clip_count": 1, "file_count": 7,
        }, {}, [])

    def backing():
        order.append("backing")
        return True

    def destination(*_args):
        order.append("destination")
        return str(tmp_path / "output")

    monkeypatch.setattr(ExportOptionsDialog, "exec", choose)
    monkeypatch.setattr(editor, "_confirm_backing_export", backing)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", destination)
    editor.exporter = SimpleNamespace(export=export)
    editor.action_export.trigger()
    qtbot.waitUntil(lambda: editor._export_worker is None)
    assert order == ["options", "backing", "destination", "export"]
    expected = {
        "unchanged": ExportOptions(480, 30, False, 0.15, 0.25),
        "higher": ExportOptions(720, 30, False, 0.35, 0.45),
        "custom": ExportOptions(1080, 60, False, 0.35, 0.45),
    }[choice]
    assert ExportOptions.from_project(calls[0]) == expected
    assert calls[0] is not editor.project
    assert calls[0].to_dict() == editor.project.to_dict()
    assert editor.height_spin.value() == expected.video_height
    assert editor.fps_spin.value() == expected.video_fps
    assert editor.head_pad_spin.value() == expected.head_padding
    assert editor.dirty is (choice != "unchanged")
    assert editor.session.revision == revision + (choice != "unchanged")
    saved = tmp_path / "saved.cvpack.json"
    assert editor.workspace.save_editor(editor, destination=saved)
    qtbot.waitUntil(lambda: not editor.dirty and saved.is_file())
    assert ExportOptions.from_project(ProjectStore.load(saved)) == expected
    editor._export_dialog.close()
    reopened = []

    def reopen(dialog):
        reopened.append(dialog.options())
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ExportOptionsDialog, "exec", reopen)
    editor.export_pack()
    assert reopened == [expected]
    assert len(calls) == 1


@pytest.mark.parametrize("stop", ["validation", "destination", "overwrite"])
def test_acceptance_keeps_dirty_settings_when_later_export_steps_stop(
    editor, tmp_path, monkeypatch, stop,
):
    def choose(dialog):
        dialog.quality_combo.setCurrentIndex(1)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExportOptionsDialog, "exec", choose)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _self, title, *_args: (
        warnings.append(title) or QMessageBox.StandardButton.Cancel
    ))
    if stop == "validation":
        editor.project.segments.clear()
        monkeypatch.setattr(editor, "_confirm_backing_export", lambda: pytest.fail("Backing"))
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: pytest.fail("Destination"))
    else:
        monkeypatch.setattr(editor, "_confirm_backing_export", lambda: True)
        if stop == "overwrite":
            (tmp_path / editor.project.title).mkdir()
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            lambda *_args: "" if stop == "destination" else str(tmp_path),
        )
    editor.export_pack()
    assert editor.project.video_height == editor.height_spin.value() == 720
    assert editor.dirty
    assert not editor.workspace.job_manager.tasks(editor.session.id)
    assert "lastExportDir" not in editor.settings.allKeys()
    if stop == "validation":
        assert warnings == ["Project is not ready"]
    elif stop == "overwrite":
        assert warnings == ["Replace existing export?"]
        assert (tmp_path / editor.project.title).is_dir()


def test_options_do_not_overwrite_a_project_changed_during_dialog(editor, monkeypatch):
    def choose(dialog):
        dialog.quality_combo.setCurrentIndex(1)
        editor.height_spin.setValue(1080)
        return QDialog.DialogCode.Accepted

    notices = []
    monkeypatch.setattr(ExportOptionsDialog, "exec", choose)
    monkeypatch.setattr(editor.workspace, "notice", lambda *args: notices.append(args))
    editor.export_pack()
    assert editor.project.video_height == 1080
    assert len(notices) == 1 and "project changed" in notices[0][1].lower()
    assert not editor.workspace.job_manager.tasks(editor.session.id)


@pytest.mark.integration
@pytest.mark.parametrize("change_profile", [False, True])
def test_imported_ogv_is_only_converted_after_explicit_profile_change(
    editor, qtbot, tmp_path, monkeypatch, change_profile,
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe required")
    media = MediaTools()
    source = tmp_path / "imported.ogv"
    media.run([
        media.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
        "-c:v", "libtheora", "-pix_fmt", "yuv420p", "-c:a", "libvorbis", str(source),
    ], "Creating synthetic imported OGV")
    editor._set_project(PackProject(
        title="Imported fixture", authors=["Tester"], video_path=str(source), video_duration=3,
        video_height=240, video_fps=24, preserve_source_video=True,
        segments=[Segment(0.5, 1.5, "Synthetic line", ["Actor"])],
    ), None, mark_dirty=False)
    before = source.read_bytes()
    conversions = []
    convert = media.convert_video

    def tracked_conversion(*args, **kwargs):
        conversions.append(args)
        return convert(*args, **kwargs)

    def choose(dialog):
        assert dialog.quality_combo.currentText() == "Custom"
        assert dialog.options().preserve_source_video
        if change_profile:
            dialog.quality_combo.setCurrentIndex(1)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(media, "convert_video", tracked_conversion)
    monkeypatch.setattr(ExportOptionsDialog, "exec", choose)
    monkeypatch.setattr(editor, "_confirm_backing_export", lambda: True)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: str(tmp_path / "output"))
    editor.exporter = PackExporter(media)
    editor.export_pack()
    qtbot.waitUntil(lambda: editor._export_worker is None, timeout=60000)
    record = next(job for job in editor.workspace.job_manager.tasks() if job.kind == "export")
    assert record.state == "succeeded", record.error
    assert len(conversions) == int(change_profile)
    assert record.result.validation["status"] == "passed"
    assert source.read_bytes() == before
    video = record.result.pack_path / "dub_video.ogv"
    assert media.probe(video).fps == (30 if change_profile else 24)
    assert (video.read_bytes() == before) is (not change_profile)
    assert editor.project.preserve_source_video is (not change_profile)
