from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp.utils import DownloadError

from choicer_voicer_pack_creator import youtube

VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CAPTION_URL = "https://www.youtube.com/api/timedtext?lang=en"


@pytest.mark.parametrize("url", [
    URL, f"https://youtu.be/{VIDEO_ID}?t=42",
    f"https://www.youtube.com/shorts/{VIDEO_ID}",
    f"https://m.youtube.com/watch?v={VIDEO_ID}&list=ignored",
    f"http://youtube.com/embed/{VIDEO_ID}",
])
def test_youtube_urls_normalize_to_one_video(url: str) -> None:
    assert youtube.normalize_youtube_url(url) == URL


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "https://youtube.com.evil.example/watch?v=abcdefghijk",
    "https://example.com/watch?v=abcdefghijk",
    "https://user:password@youtube.com/watch?v=abcdefghijk",
    "https://youtube.com:443/watch?v=abcdefghijk",
    "https://youtube.com/playlist?list=test", "not a URL",
    "https://youtube.com/watch?v=short", "https://youtu.be/abcdefghijk/extra",
    "https://youtube.com/watch?v=abcdefghijk&v=lmnopqrstuv",
])
def test_unsupported_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        youtube.normalize_youtube_url(url)


def track(url: str = CAPTION_URL) -> list[dict[str, str]]:
    return [{"ext": "json3", "url": url}]


def test_caption_selection_prefers_creator_in_original_language() -> None:
    info = {
        "language": "ja",
        "subtitles": {"en": track(), "ja": track(CAPTION_URL + "&manual=1")},
        "automatic_captions": {"ja-orig": track(), "ja": track()},
    }
    selected = youtube.select_caption_track(info, "auto")
    assert selected.language == "ja"
    assert not selected.automatic
    assert youtube.select_caption_track(info, "de") is None


def test_caption_selection_uses_original_auto_instead_of_translation() -> None:
    info = {
        "subtitles": {"en": track()},
        "automatic_captions": {
            "ja-orig": track(CAPTION_URL + "&lang=ja"),
            "en": track(CAPTION_URL + "&tlang=en"),
        },
    }
    selected = youtube.select_caption_track(info, "auto")
    assert selected.language == "ja"
    assert selected.automatic
    assert youtube.select_caption_track(
        {"automatic_captions": {"fr": track(CAPTION_URL + "&tlang=fr")}}, "fr"
    ) is None


class FakeMedia:
    ffmpeg = str(Path("ffmpeg.exe").resolve())

    def probe(self, path: Path):
        assert path.read_bytes() == b"downloaded-video"
        return SimpleNamespace(duration=10, has_audio=True)


@pytest.fixture
def downloader(monkeypatch):
    class FakeDownloader:
        options = None
        fail_video = False
        fail_captions = False
        cancel = False
        live = False
        no_captions = False

        def __init__(self, options, *, auto_init):
            assert not auto_init
            type(self).options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def add_info_extractor(self, extractor):
            assert isinstance(extractor, youtube._PublicYoutubeIE)

        def extract_info(self, url, *, download, process):
            assert url == URL and not download and not process
            return {
                "id": VIDEO_ID, "title": "../Not a filename", "is_live": self.live,
                "subtitles": {} if self.no_captions else {"en": track()},
            }

        def urlopen(self, url):
            assert url == CAPTION_URL
            if self.fail_captions:
                raise OSError("captions rate-limited")
            return io.BytesIO(json.dumps({"events": [{
                "tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": "Hello"}],
            }]}).encode())

        def process_ie_result(self, _info, *, download):
            assert download
            path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            path.write_bytes(b"downloaded-video")
            if self.fail_video:
                raise DownloadError("unavailable")
            self.options["progress_hooks"][0]({"status": "finished"})

    monkeypatch.setattr(youtube, "YoutubeDL", FakeDownloader)
    return FakeDownloader


def run_download(destination: Path, cancelled=lambda: False):
    return youtube.download_youtube(
        FakeMedia(), URL, destination, "auto",
        progress=lambda _message, _fraction: None, cancelled=cancelled,
    )


def test_download_publishes_unique_media_and_preserves_existing_files(
    tmp_path: Path, downloader,
) -> None:
    existing = tmp_path / "source.mp4"
    existing.write_bytes(b"user-video")
    first = run_download(tmp_path)
    second = run_download(tmp_path)
    assert first.video_path != second.video_path
    assert first.video_path.read_bytes() == b"downloaded-video"
    assert first.captions[0].text == "Hello"
    assert first.title == "../Not a filename"
    assert first.url == URL
    assert existing.read_bytes() == b"user-video"
    assert not list(tmp_path.glob(".cvpc-youtube-*"))
    assert downloader.options["noplaylist"]
    assert not downloader.options["remote_components"]
    assert downloader.options["ffmpeg_location"] == str(Path(FakeMedia.ffmpeg).parent)


def test_caption_failure_does_not_discard_downloaded_video(tmp_path: Path, downloader) -> None:
    downloader.fail_captions = True
    result = run_download(tmp_path)
    assert result.video_path.is_file()
    assert result.captions == []
    assert any("captions rate-limited" in warning for warning in result.warnings)


def test_missing_captions_falls_back_explicitly_to_whisper(tmp_path: Path, downloader) -> None:
    downloader.no_captions = True
    result = run_download(tmp_path)
    assert not result.captions
    assert "Local Whisper" in result.warnings[0]


def test_download_failure_cleans_partial_files(tmp_path: Path, downloader) -> None:
    downloader.fail_video = True
    with pytest.raises(youtube.YouTubeError, match="unavailable"):
        run_download(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cancellation_after_download_cleans_partial_files(tmp_path: Path, downloader) -> None:
    def cancelled():
        return bool(list(tmp_path.glob(".cvpc-youtube-*\\source.mp4")))

    with pytest.raises(youtube.YouTubeCancelled):
        run_download(tmp_path, cancelled)
    assert list(tmp_path.iterdir()) == []


def test_live_stream_is_rejected_before_download(tmp_path: Path, downloader) -> None:
    downloader.live = True
    with pytest.raises(youtube.YouTubeError, match="Live"):
        run_download(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_age_gate_is_rejected_before_extractor_fallback() -> None:
    with pytest.raises(youtube.YouTubeError, match="Age-restricted"):
        youtube._PublicYoutubeIE._is_agegated(
            {"playabilityStatus": {"reason": "Please confirm your age"}}
        )
    assert not youtube._PublicYoutubeIE._is_agegated({"playabilityStatus": {"status": "OK"}})


def test_frozen_runtime_must_come_from_application_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(youtube.sys, "frozen", True, raising=False)
    monkeypatch.setattr(youtube.sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(youtube.YouTubeError, match="runtime is missing"):
        youtube.youtube_runtime_path()
    executable = tmp_path / "runtime" / "deno" / "deno.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"runtime")
    assert youtube.youtube_runtime_path() == executable
