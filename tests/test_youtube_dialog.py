from __future__ import annotations

import time

from PySide6.QtWidgets import QDialog, QMessageBox

from choicer_voicer_pack_creator.models import SourceCaption
from choicer_voicer_pack_creator.ui import main_window, youtube_dialog
from choicer_voicer_pack_creator.youtube import YouTubeCancelled, YouTubeDownload


class UnusedMedia:
    pass


def make_download(tmp_path):
    folder = tmp_path / "YouTube-abcdefghijk-unique"
    folder.mkdir()
    video = folder / "source.mp4"
    video.write_bytes(b"video")
    return YouTubeDownload(
        video, "Example", 10, "https://www.youtube.com/watch?v=abcdefghijk", "en",
        [SourceCaption(1, 2, "Hello", "YouTube creator (en)")], [],
    )


def test_download_dialog_waits_for_worker_before_accepting(qtbot, tmp_path, monkeypatch):
    result = make_download(tmp_path)
    monkeypatch.setattr(youtube_dialog, "download_youtube", lambda *_args, **_kwargs: result)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText(result.url)
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.download_result == result
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_cancel_waits_for_worker_and_does_not_accept_partial_download(
    qtbot, tmp_path, monkeypatch,
):
    def download(*_args, cancelled, **_kwargs):
        while not cancelled():
            time.sleep(0.01)
        raise YouTubeCancelled("Canceled")

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    dialog.start_download()
    dialog.reject()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.download_result is None
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_invalid_url_never_starts_worker(qtbot, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://example.com/video")
    dialog.start_download()
    assert dialog.worker is None
    assert warnings


def test_main_window_loads_download_and_starts_caption_comparison(
    qtbot, tmp_path, monkeypatch,
):
    result = make_download(tmp_path)

    class ImportDialog:
        download_result = result

        def __init__(self, *_args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window, "YouTubeDialog", ImportDialog)
    window = main_window.MainWindow(UnusedMedia(), analysis_data_root=tmp_path / "analysis")
    qtbot.addWidget(window)
    scans = []
    monkeypatch.setattr(window, "open_analysis_dialog", lambda **kwargs: scans.append(kwargs))
    window.new_from_youtube()
    qtbot.waitUntil(lambda: bool(scans))
    assert scans == [{"initial_scan": True, "auto_start": True}]
    assert window.project.video_path == str(result.video_path)
    assert window.project.source_captions == result.captions
    assert window.project.source_url == result.url
    assert window.dirty
    window._set_busy(True, "Exporting")
    assert not window.action_youtube.isEnabled()
    window._set_busy(False, "Ready")
    window.dirty = False
    window.close()
