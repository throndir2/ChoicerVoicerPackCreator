from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QLabel

from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.ui.main_window import MainWindow


@pytest.mark.integration
def test_stopped_seek_retains_requested_video_frame(qtbot, tmp_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not available")
    media = MediaTools()
    video = tmp_path / "red-then-blue.mp4"
    media.run(
        [
            media.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:r=15:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=15:d=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo:d=4",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-t",
            "4",
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-c:a",
            "aac",
            str(video),
        ],
        "Creating stopped-seek test video",
    )
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(media, settings=settings)
    qtbot.addWidget(window)
    frames: list[tuple[float, int, int, int]] = []

    def collect_frame(frame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        color = image.pixelColor(image.width() // 2, image.height() // 2)
        frames.append((frame.startTime() / 1000, color.red(), color.green(), color.blue()))

    window.video_widget.videoSink().videoFrameChanged.connect(collect_frame)
    first = Segment(0.5, 1.0, "Red segment", ["Red"])
    second = Segment(3.0, 3.5, "Blue segment", ["Blue"])
    window._set_project(
        PackProject(
            title="Seek test",
            authors=["Test"],
            video_path=str(video),
            video_duration=4,
            segments=[first, second],
        ),
        None,
        mark_dirty=False,
    )
    window.show()
    qtbot.waitUntil(
        lambda: window.player.mediaStatus()
        in {
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        },
        timeout=6000,
    )
    window.player.stop()
    window.select_segment(first.id)
    qtbot.waitUntil(
        lambda: not window._stopped_seek_active
        and window.player.playbackState() == QMediaPlayer.PlaybackState.PausedState,
        timeout=6000,
    )
    assert abs(window.player.position() - 500) <= 150
    assert window.video_widget.subtitle_overlay.isVisible()
    assert window.video_widget.subtitle_overlay.findChild(
        QLabel, "subtitleCaption"
    ).text() == "Red segment"
    frames.clear()

    window.segment_table.selectRow(1)
    qtbot.waitUntil(
        lambda: window.selected_segment_id == second.id
        and abs(window.player.position() - 3000) <= 150,
        timeout=6000,
    )
    qtbot.waitUntil(lambda: bool(frames), timeout=2000)
    timestamp, red, green, blue = min(frames, key=lambda item: abs(item[0] - 3000))

    assert abs(timestamp - 3000) <= 100
    assert blue > red * 2 and blue > green * 2
    assert abs(window.player.position() - 3000) <= 150
    assert not window.audio_output.isMuted()
    qtbot.waitUntil(
        lambda: any(
            label.isVisible() and label.text() == "Blue segment"
            for label in window.video_widget.subtitle_overlay.findChildren(QLabel, "subtitleCaption")
        )
    )
    preview = window.video_widget.grab().toImage()
    color = preview.pixelColor(preview.width() // 2, preview.height() // 2)
    assert color.blue() > color.red() * 2 and color.blue() > color.green() * 2
    window.toggle_playback()
    qtbot.waitUntil(
        lambda: window.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
        timeout=2000,
    )
    assert window.player.position() >= 2850
    qtbot.waitUntil(
        lambda: window.player.position() >= 3500
        and not window.video_widget.subtitle_overlay.isVisible(),
        timeout=3000,
    )
    window.dirty = False
    window.close()
