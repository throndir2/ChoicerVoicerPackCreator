from __future__ import annotations

import time

import pytest
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
    assert dialog.progress_bar.value() == 1000
    assert dialog.progress_bar.format() == "Ready"


def test_cancel_waits_for_worker_and_does_not_accept_partial_download(
    qtbot, tmp_path, monkeypatch,
):
    def download(*_args, cancelled, progress, **_kwargs):
        while not cancelled():
            time.sleep(0.01)
        progress("Merging downloaded media...", None)
        raise YouTubeCancelled("Canceled")

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    dialog._progress("Previous transfer", 900)
    dialog.start_download()
    assert dialog.progress_bar.maximum() == 0
    assert "Fetching" in dialog.progress_label.text()
    dialog.reject()
    dialog._progress("Late progress", 950)
    assert dialog.progress_label.text().startswith("Canceling")
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.download_result is None
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.progress_bar.maximum() == 1000
    assert dialog.progress_bar.value() == 0
    assert dialog.progress_bar.format() == "Canceled"


def test_dialog_labels_indeterminate_stages_and_restores_transfer_percentage(qtbot, tmp_path):
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog._progress("Downloading YouTube video — total size unknown", -1)
    assert dialog.progress_bar.maximum() == 0
    assert "not measurable" in dialog.progress_label.text()
    dialog._progress("Downloading YouTube audio — estimated combined transfer progress", 940)
    assert dialog.progress_bar.maximum() == 1000
    assert dialog.progress_bar.value() == 940
    assert dialog.progress_bar.format() == "Transfers: %p%"
    assert "estimated" in dialog.progress_label.text()
    for stage in ("Merging", "Checking", "Publishing"):
        dialog._progress(stage, -1)
        assert dialog.progress_bar.maximum() == 0
        assert "not measurable" in dialog.progress_label.text()


def test_worker_does_not_round_unfinished_transfers_to_100_percent(qtbot, tmp_path, monkeypatch):
    result = make_download(tmp_path)

    def download(*_args, progress, **_kwargs):
        for value in (None, 0, 0.5, 0.9999, 1):
            progress("Progress", value)
        return result

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    worker = youtube_dialog.YouTubeWorker(UnusedMedia(), result.url, tmp_path, "auto")
    values = []
    worker.progress.connect(lambda _message, value: values.append(value))
    worker.run()
    assert values == [-1, 0, 500, 999, 1000]


def test_download_failure_stops_indeterminate_progress_and_allows_retry(qtbot, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: errors.append(_args[-1]))

    def download(*_args, progress, **_kwargs):
        progress("Merging downloaded media...", None)
        raise OSError("Merge failed")

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert errors == ["Merge failed"]
    assert dialog.download_result is None
    assert dialog.progress_bar.maximum() == 1000
    assert dialog.progress_bar.value() == 0
    assert dialog.progress_bar.format() == "Failed"
    assert dialog.download_button.isEnabled()


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


@pytest.mark.parametrize("clear_field", [False, True])
def test_unspecified_download_folder_defaults_to_windows_downloads(
    qtbot, tmp_path, monkeypatch, clear_field,
):
    downloads = tmp_path / "Downloads"
    monkeypatch.setattr(
        youtube_dialog.QStandardPaths, "writableLocation", lambda _location: str(downloads)
    )
    result = make_download(tmp_path)
    folders = []

    def download(_media, _url, folder, *_args, **_kwargs):
        folders.append(folder)
        return result

    monkeypatch.setattr(youtube_dialog, "download_youtube", download)
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), "")
    qtbot.addWidget(dialog)
    assert dialog.folder_edit.text() == str(downloads)
    assert not downloads.exists()
    if clear_field:
        dialog.folder_edit.setText("   ")
    dialog.url_edit.setText(result.url)
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert downloads.is_dir()
    assert folders == [downloads]
    assert dialog.folder_edit.text() == str(downloads)


def test_download_default_does_not_replace_explicit_or_remembered_folder(
    qtbot, tmp_path, monkeypatch,
):
    downloads = tmp_path / "Downloads"
    custom = tmp_path / "custom"
    custom.mkdir()
    monkeypatch.setattr(
        youtube_dialog.QStandardPaths, "writableLocation", lambda _location: str(downloads)
    )
    result = make_download(tmp_path)
    folders = []
    monkeypatch.setattr(
        youtube_dialog, "download_youtube",
        lambda _media, _url, folder, *_args, **_kwargs: folders.append(folder) or result,
    )
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(custom))
    qtbot.addWidget(dialog)
    assert dialog.folder_edit.text() == str(custom)
    dialog.url_edit.setText(result.url)
    dialog.start_download()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert folders == [custom]
    assert not downloads.exists()


def test_invalid_custom_destination_is_reported_instead_of_silently_using_downloads(
    qtbot, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        youtube_dialog.QStandardPaths, "writableLocation", lambda _location: str(tmp_path)
    )
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: errors.append(_args[-1]))
    dialog = youtube_dialog.YouTubeDialog(UnusedMedia(), str(tmp_path / "missing"))
    qtbot.addWidget(dialog)
    dialog.url_edit.setText("https://youtu.be/abcdefghijk")
    dialog.start_download()
    assert dialog.worker is None
    assert errors == ["The media destination must be an existing folder."]
