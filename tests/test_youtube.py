from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from choicer_voicer_pack_creator import youtube

VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CAPTION_URL = "https://www.youtube.com/api/timedtext?lang=en"
VIDEO_FORMAT = {"format_id": "137", "vcodec": "h264", "acodec": "none", "filesize": 900}
AUDIO_FORMAT = {"format_id": "140", "vcodec": "none", "acodec": "aac", "filesize": 100}


def transfer_event(format_id, downloaded, *, status="downloading", **kwargs):
    return {
        "status": status, "info_dict": {"format_id": format_id},
        "downloaded_bytes": downloaded, **kwargs,
    }


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
def downloader(monkeypatch, inline_youtube_worker):
    class FakeDownloader:
        sanitize_info = staticmethod(YoutubeDL.sanitize_info)
        options = None
        fail_video = False
        fail_captions = False
        cancel = False
        live = False
        no_captions = False
        selected_formats = [VIDEO_FORMAT, AUDIO_FORMAT]
        events = [
            transfer_event("137", 450, total_bytes=900),
            transfer_event("137", 900, status="finished", total_bytes=900),
            transfer_event("140", 10, total_bytes=100),
            transfer_event("140", 100, status="finished", total_bytes=100),
        ]
        postprocess_events = [
            {"status": "started", "postprocessor": "Merger"},
            {"status": "finished", "postprocessor": "Merger"},
        ]

        def __init__(self, options, *, auto_init):
            assert not auto_init
            type(self).options = options
            self.cookiejar = CookieJar()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def add_info_extractor(self, extractor):
            assert isinstance(extractor, youtube._PublicYoutubeIE)

        def add_post_processor(self, processor, *, when):
            assert when == "before_dl"
            self.before_dl = processor

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
            info = (
                {**_info, "requested_formats": self.selected_formats}
                if len(self.selected_formats) > 1 else {**_info, **self.selected_formats[0]}
            )
            self.before_dl.run(info)
            path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            path.write_bytes(b"downloaded-video")
            for event in self.events:
                self.options["progress_hooks"][0](event)
            if self.fail_video:
                raise DownloadError("unavailable")
            for event in self.postprocess_events:
                self.options["postprocessor_hooks"][0](event)

    monkeypatch.setattr(youtube, "YoutubeDL", FakeDownloader)
    return FakeDownloader


def run_download(destination: Path, cancelled=lambda: False, progress=lambda *_args: None):
    return youtube.download_youtube(
        FakeMedia(), URL, destination, "auto",
        progress=progress, cancelled=cancelled,
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


def test_progress_combines_selected_streams_and_only_completes_after_publication(
    tmp_path, downloader,
):
    def collect_run(expected_folders):
        events = []

        def report(message, fraction):
            events.append((message, fraction))
            if fraction == 1:
                assert len(list(tmp_path.glob("YouTube-*"))) == expected_folders
                assert not list(tmp_path.glob(".cvpc-youtube-*"))

        run_download(tmp_path, progress=report)
        return events

    events = collect_run(1)
    assert collect_run(2) == events
    fractions = [fraction for _, fraction in events if fraction is not None]
    assert fractions == pytest.approx([0.45, 0.9, 0.91, 1.0])
    messages = [message for message, _ in events]
    assert any("Fetching YouTube video details" in message for message in messages)
    assert any("Fetching available YouTube captions" in message for message in messages)
    assert any("Downloading YouTube video (1 of 2" in message for message in messages)
    assert any("Downloading YouTube audio (2 of 2" in message for message in messages)
    for stage in ("Merging", "Checking", "Publishing"):
        assert any(message.startswith(stage) and value is None for message, value in events)
    assert events[-1] == ("YouTube video ready", 1.0)


def make_progress(formats):
    events = []
    tracker = youtube._DownloadProgress(lambda *event: events.append(event), lambda: False)
    tracker.prepare({"requested_formats": formats})
    return tracker, events


def test_estimates_fragment_updates_and_retries_do_not_reset_or_double_count_progress():
    tracker, events = make_progress([
        {**VIDEO_FORMAT, "filesize": None, "filesize_approx": 900}, AUDIO_FORMAT,
    ])
    updates = [
        transfer_event("137", 450, total_bytes_estimate=900, fragment_index=4, fragment_count=10),
        transfer_event("137", 450, total_bytes_estimate=1800, fragment_index=4, fragment_count=10),
        transfer_event("137", 0, total_bytes_estimate=1800, fragment_index=0, fragment_count=10),
        transfer_event("137", 450, total_bytes_estimate=1800, fragment_index=4, fragment_count=10),
        transfer_event("137", 450, total_bytes_estimate=1800, fragment_index=4, fragment_count=10),
        transfer_event("137", 1710, total_bytes=1800, fragment_index=9, fragment_count=10),
        transfer_event("137", 1800, total_bytes=1800, status="finished"),
        transfer_event("140", 10, total_bytes=100),
        transfer_event("140", 100, total_bytes=100, status="finished"),
    ]
    for update in updates:
        tracker.download_hook(update)
    fractions = [fraction for _, fraction in events if fraction is not None]
    assert fractions == pytest.approx([0.45] * 5 + [0.9, 1800 / 1900, 1810 / 1900])
    assert fractions == sorted(fractions)
    assert all("estimated" in message for message, value in events if value is not None)
    assert events[-1][1] is None


def test_concurrent_dash_stream_hooks_are_serialized():
    events = []

    def report(message, value):
        if value is not None and value < 0.5:
            time.sleep(0.001)
        events.append((message, value))

    tracker = youtube._DownloadProgress(report, lambda: False)
    tracker.prepare({"requested_formats": [VIDEO_FORMAT, AUDIO_FORMAT]})
    updates = [
        transfer_event("137", 9 * index) if index % 2 else transfer_event("140", index)
        for index in range(1, 101)
    ]

    def download_stream(format_id):
        for update in updates:
            if update["info_dict"]["format_id"] == format_id:
                tracker.download_hook(update)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(download_stream, ("137", "140")))
    fractions = [value for _, value in events if value is not None]
    assert fractions == sorted(fractions)
    assert fractions[-1] == pytest.approx(0.991)


@pytest.mark.parametrize("bad_size", [None, 0, -10, float("nan"), float("inf"), "100"])
def test_unknown_selected_transfer_sizes_are_indeterminate_until_measured(bad_size):
    tracker, events = make_progress([VIDEO_FORMAT, {**AUDIO_FORMAT, "filesize": bad_size}])
    tracker.download_hook(transfer_event("137", 450, total_bytes=900))
    tracker.download_hook(transfer_event("137", 900, total_bytes=900, status="finished"))
    tracker.download_hook(transfer_event("140", 10, total_bytes=bad_size, fragment_count=10))
    assert all(value is None for _, value in events)
    assert "total size unknown" in events[-1][0]
    tracker.download_hook(transfer_event("140", 50, total_bytes=100))
    assert events[-1][1] == pytest.approx(0.95)


def test_underestimated_transfer_never_reports_completion_and_exact_size_wins():
    tracker, events = make_progress([
        {**VIDEO_FORMAT, "filesize": None, "filesize_approx": 100},
    ])
    tracker.download_hook(transfer_event("137", 200, total_bytes_estimate=100))
    tracker.download_hook(transfer_event("137", 200, total_bytes=1000))
    tracker.download_hook(transfer_event("137", 300, total_bytes_estimate=10))
    assert tracker.transfers[0].total == 1000
    assert [value for _, value in events if value is not None] == [0.999] * 3
    tracker.download_hook(transfer_event("137", 1000, total_bytes=1000, status="finished"))
    assert events[-1][1] is None


def test_finished_hook_without_byte_count_does_not_shrink_known_transfer():
    tracker, events = make_progress([VIDEO_FORMAT, AUDIO_FORMAT])
    tracker.download_hook(transfer_event("137", 450, total_bytes=900))
    tracker.download_hook({"status": "finished", "info_dict": {"format_id": "137"}})
    assert events[-1][1] == 0.9
    assert tracker.transfers[0].total == 900


def test_repeated_finished_hooks_and_restarted_transfer_do_not_double_count():
    tracker, events = make_progress([VIDEO_FORMAT, AUDIO_FORMAT])
    for _ in range(2):
        tracker.download_hook(transfer_event("137", 900, status="finished"))
    tracker.download_hook(transfer_event("137", 0))
    tracker.download_hook(transfer_event("140", 50))
    assert [value for _, value in events if value is not None] == [0.9] * 4
    tracker.download_hook(transfer_event("137", 900, status="finished"))
    assert events[-1][1] == pytest.approx(0.95)


def test_combined_external_download_is_indeterminate_instead_of_guessing_stream_bytes():
    tracker, events = make_progress([VIDEO_FORMAT, AUDIO_FORMAT])
    tracker.download_hook({
        "status": "downloading", "info_dict": {
            "format_id": "137+140", "requested_formats": [VIDEO_FORMAT, AUDIO_FORMAT],
        }, "downloaded_bytes": 800, "total_bytes": 1000,
    })
    assert events[-1][1] is None
    assert "unavailable" in events[-1][0]


@pytest.mark.parametrize("split", [False, True])
def test_pinned_ytdlp_before_download_sees_actual_selected_formats_offline(tmp_path, split):
    formats = [
        {"format_id": "18", "vcodec": "h264", "acodec": "aac", "filesize": 500, "height": 360},
    ]
    if split:
        formats += [{**VIDEO_FORMAT, "height": 1080}, AUDIO_FORMAT]
    tracker = youtube._DownloadProgress(lambda *_args: None, lambda: False)
    with YoutubeDL({
        "format": "bv*+ba/b", "skip_download": True, "quiet": True,
        "outtmpl": str(tmp_path / "source.%(ext)s"),
        "postprocessor_hooks": [tracker.postprocessor_hook],
    }, auto_init=False) as ydl:
        ydl.add_post_processor(youtube._DownloadProgressPP(tracker), when="before_dl")
        ydl.process_ie_result({
            "id": VIDEO_ID, "title": "Offline selection", "extractor": "youtube",
            "webpage_url": URL,
            "formats": [
                {**item, "url": "https://example.invalid/media", "ext": "mp4"}
                for item in formats
            ],
        }, download=True)
    assert [item.format_id for item in tracker.transfers] == (["137", "140"] if split else ["18"])
    assert [item.kind for item in tracker.transfers] == (
        ["video", "audio"] if split else ["video and audio"]
    )


@pytest.mark.parametrize("stage", ["Merging", "Checking", "Publishing"])
def test_cancellation_in_final_stages_never_reports_ready_or_publishes(
    tmp_path, downloader, stage,
):
    events = []
    canceled = False

    def report(message, value):
        nonlocal canceled
        events.append((message, value))
        canceled |= message.startswith(stage)

    with pytest.raises(youtube.YouTubeCancelled):
        run_download(tmp_path, cancelled=lambda: canceled, progress=report)
    assert not any(value == 1 for _, value in events)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["download", "merge", "probe", "publication"])
def test_failures_never_report_ready_or_leave_partial_media(
    tmp_path, downloader, monkeypatch, failure,
):
    events = []
    if failure == "download":
        downloader.fail_video = True
    elif failure == "probe":
        monkeypatch.setattr(
            FakeMedia, "probe", lambda *_args: SimpleNamespace(duration=0, has_audio=True),
        )
    elif failure == "publication":
        def fail_rename(*_args):
            raise OSError("Cannot publish")

        monkeypatch.setattr(Path, "rename", fail_rename)

    def report(message, value):
        events.append((message, value))
        if failure == "merge" and message.startswith("Merging"):
            raise DownloadError("Merge failed")

    with pytest.raises((youtube.YouTubeError, OSError)):
        run_download(tmp_path, progress=report)
    assert not any(value == 1 for _, value in events)
    assert not list(tmp_path.iterdir())


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


@pytest.mark.parametrize("stage", ["metadata", "captions", "media"])
def test_stalled_connections_retry_once_with_ipv4_and_keep_it_for_later_stages(
    tmp_path, downloader, inline_youtube_worker, monkeypatch, stage,
):
    attempts = []
    events = []

    def run(target, args, **kwargs):
        request, action, _payload = args
        attempts.append((action, request.ipv4, kwargs["timeout"]))
        if action == stage and not request.ipv4:
            raise youtube.ProcessWorkerTimeout("blocked network request")
        return inline_youtube_worker(target, args, **kwargs)

    monkeypatch.setattr(youtube, "run_process_worker", run)
    result = run_download(tmp_path, progress=lambda *event: events.append(event))
    assert result.video_path.is_file()
    assert any("IPv4" in note for note in result.warnings)
    index = next(i for i, attempt in enumerate(attempts) if attempt[0] == stage)
    assert attempts[index][1] is False
    assert attempts[index + 1][0:2] == (stage, True)
    assert all(ipv4 for _, ipv4, _ in attempts[index + 1:])
    assert attempts[0][2] == youtube.METADATA_TIMEOUT
    assert next(timeout for action, _, timeout in attempts if action == "captions") == (
        youtube.CAPTION_TIMEOUT
    )
    assert next(timeout for action, _, timeout in attempts if action == "media") is None
    assert downloader.options["source_address"] == "0.0.0.0"
    assert sum("retrying using IPv4" in message for message, _ in events) == 1
    assert events[-1] == ("YouTube video ready", 1.0)


def test_metadata_stall_on_both_address_families_fails_without_partial_media(
    tmp_path, downloader, monkeypatch,
):
    attempts = []

    def stalled(_target, args, **_kwargs):
        request, action, _payload = args
        attempts.append((action, request.ipv4))
        raise youtube.ProcessWorkerTimeout("blocked DNS")

    monkeypatch.setattr(youtube, "run_process_worker", stalled)
    with pytest.raises(youtube.YouTubeError, match="proxy/VPN"):
        run_download(tmp_path)
    assert attempts == [("metadata", False), ("metadata", True)]
    assert list(tmp_path.iterdir()) == []


def test_caption_stalls_remain_optional_after_bounded_ipv4_retry(
    tmp_path, downloader, inline_youtube_worker, monkeypatch,
):
    def run(target, args, **kwargs):
        if args[1] == "captions":
            raise youtube.ProcessWorkerTimeout("caption request stalled")
        return inline_youtube_worker(target, args, **kwargs)

    monkeypatch.setattr(youtube, "run_process_worker", run)
    result = run_download(tmp_path)
    assert result.video_path.is_file()
    assert not result.captions
    assert any("Whisper can draft" in note for note in result.warnings)


@pytest.mark.parametrize("stage", ["metadata", "captions", "media", "probe"])
def test_worker_cancellation_never_retries_or_publishes(
    tmp_path, downloader, inline_youtube_worker, monkeypatch, stage,
):
    attempts = []

    def run(target, args, **kwargs):
        attempts.append(args[1])
        if args[1] == stage:
            raise youtube.ProcessWorkerCancelled("Canceled")
        return inline_youtube_worker(target, args, **kwargs)

    monkeypatch.setattr(youtube, "run_process_worker", run)
    with pytest.raises(youtube.YouTubeCancelled):
        run_download(tmp_path)
    assert attempts.count(stage) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["YouTubeError", "HTTPError"])
def test_access_errors_do_not_trigger_ipv4_retry(tmp_path, downloader, monkeypatch, failure):
    calls = []

    def run(_target, args, **_kwargs):
        calls.append(args[1])
        raise youtube.ProcessWorkerError("Access denied", error_type=failure)

    monkeypatch.setattr(youtube, "run_process_worker", run)
    with pytest.raises(youtube.YouTubeError, match="Access denied"):
        run_download(tmp_path)
    assert calls == ["metadata"]
    assert list(tmp_path.iterdir()) == []


def test_waiting_status_and_activity_deadlines_do_not_treat_logs_as_progress(
    tmp_path, monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(youtube.time, "monotonic", lambda: clock[0])
    events = []

    def run(_target, _args, *, on_event, idle_timeout, waiting, **_kwargs):
        assert idle_timeout() == youtube.TRANSFER_IDLE_TIMEOUT
        assert not on_event("diagnostic", {"event": "retry", "details": {}})
        assert not on_event("progress", {"message": "Downloading", "fraction": 0.2})
        assert on_event("activity", {})
        clock[0] = 5
        waiting(5)
        assert on_event("phase", {"postprocessing": True})
        assert not on_event("phase", {"postprocessing": True})
        assert idle_timeout() == youtube.POSTPROCESS_IDLE_TIMEOUT
        raise youtube.ProcessWorkerTimeout("merge stalled")

    monkeypatch.setattr(youtube, "run_process_worker", run)
    request = youtube._YouTubeRequest(tmp_path, URL, "", tmp_path)
    with pytest.raises(youtube.YouTubeError, match="media exceeded its wait limit"):
        youtube._run_youtube_stage(
            request, "media", {}, [], lambda *event: events.append(event), lambda: False,
        )
    assert events[-1] == ("Downloading Waiting (5s elapsed); you can cancel.", 0.2)
    assert not request.ipv4


def test_only_advancing_bytes_count_as_transfer_activity():
    updates = []
    tracker = youtube._DownloadProgress(
        lambda *_args: None, lambda: False, lambda: updates.append(True),
    )
    tracker.prepare({"requested_formats": [VIDEO_FORMAT]})
    for size in (0, 0, 10, 10, 0, 5):
        tracker.download_hook(transfer_event("137", size))
    assert len(updates) == 2


def test_download_errors_preserve_underlying_network_classification():
    from yt_dlp.networking.exceptions import TransportError
    from yt_dlp.utils import ExtractorError

    network = ExtractorError("connection failed", cause=TransportError("timed out"))
    wrapped = DownloadError("download failed", exc_info=(type(network), network, None))
    assert youtube._is_network_error(wrapped)
    assert not youtube._is_network_error(DownloadError("Video unavailable"))


@pytest.mark.parametrize("cancel", [False, True])
def test_cleanup_failure_is_not_hidden_by_cancellation_or_optional_caption_fallback(
    tmp_path, downloader, inline_youtube_worker, monkeypatch, cancel,
):
    canceled = False
    attempts = []

    def run(target, args, **kwargs):
        nonlocal canceled
        attempts.append(args[1])
        if args[1] == "captions":
            canceled = cancel
            raise youtube.ProcessWorkerError(
                "Timed out waiting for worker process tree cleanup",
                error_type="WorkerCleanupTimeout",
            )
        return inline_youtube_worker(target, args, **kwargs)

    monkeypatch.setattr(youtube, "run_process_worker", run)
    with pytest.raises(youtube.YouTubeProcessError, match="Could not finish stopping"):
        run_download(tmp_path, cancelled=lambda: canceled)
    assert attempts == ["metadata", "captions"]
    assert list(tmp_path.iterdir()) == []
