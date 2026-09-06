from __future__ import annotations

import io
import json
import subprocess
import sys
from array import array
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from choicer_voicer_pack_creator import media as media_module
from choicer_voicer_pack_creator import project_io, youtube
from choicer_voicer_pack_creator.diagnostics import (
    ApplicationDiagnostics,
    application_log_path,
    diagnostic_operation,
)
from choicer_voicer_pack_creator.exporter import PackExporter, sha256
from choicer_voicer_pack_creator.media import (
    MediaError,
    MediaInfo,
    MediaTools,
    VideoEncodingProgress,
)
from choicer_voicer_pack_creator.models import PackProject, Segment, SourceCaption
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.ui import update_controller
from choicer_voicer_pack_creator.updates import (
    PreparedUpdate,
    Release,
    UpdateCancelled,
    UpdateError,
)

PRIVATE_CAPTION = "private-caption-content"
PRIVATE_AUTHOR = "private-author-content"
PRIVATE_PAYLOAD = "private-output-payload"


def records(root: Path, event: str | None = None) -> list[dict]:
    values = [
        json.loads(line) for line in application_log_path(root).read_text(encoding="utf-8").splitlines()
    ]
    return [value for value in values if event is None or value["event"] == event]


def assert_private_content_absent(root: Path) -> None:
    text = application_log_path(root).read_text(encoding="utf-8")
    for private in (PRIVATE_CAPTION, PRIVATE_AUTHOR, PRIVATE_PAYLOAD):
        assert private not in text


def test_project_and_recovery_log_boundaries_counts_and_fallback(tmp_path: Path) -> None:
    path = tmp_path / "project.json"
    project = PackProject(
        authors=[PRIVATE_AUTHOR], readme=PRIVATE_PAYLOAD,
        segments=[Segment(1, 2, PRIVATE_CAPTION, ["Speaker"])],
        source_captions=[SourceCaption(1, 2, PRIVATE_CAPTION, "YouTube")],
    )
    recovery = RecoveryStore(tmp_path / "recovery.json")
    with ApplicationDiagnostics(tmp_path):
        ProjectStore.save(project, path)
        ProjectStore.save(project, path)
        assert ProjectStore.load(path).segments[0].caption == PRIVATE_CAPTION
        recovery.save(project, path)
        recovery.save(project, path)
        recovery.path.write_text("not JSON", encoding="utf-8")
        assert recovery.load().source_path == recovery.previous_path
        assert not recovery.saved_project_changed(recovery.load())
        recovery.clear()
        assert recovery.load() is None
        with pytest.raises(FileNotFoundError):
            ProjectStore.load(tmp_path / "missing.json")
    assert records(tmp_path, "project_save_completed")
    assert records(tmp_path, "project_load_failed")[0]["error_type"] == "FileNotFoundError"
    assert records(tmp_path, "project_loaded")[0]["source_caption_count"] == 1
    assert records(tmp_path, "recovery_candidate_failed")
    assert records(tmp_path, "recovery_loaded")[0]["previous"]
    assert records(tmp_path, "recovery_clear_completed")
    starts = records(tmp_path, "project_save_started")
    assert len({row["operation"] for row in starts}) == 2
    assert_private_content_absent(tmp_path)


def test_project_save_failure_remains_an_exception_and_is_logged(tmp_path: Path, monkeypatch) -> None:
    def fail_stage(*_args):
        raise OSError("storage not writable")

    monkeypatch.setattr(project_io, "_stage_bytes", fail_stage)
    with ApplicationDiagnostics(tmp_path), pytest.raises(OSError, match="not writable"):
        ProjectStore.save(PackProject(), tmp_path / "project.json")
    failure = records(tmp_path, "project_save_failed")[0]
    assert failure["error_type"] == "OSError"
    assert "fail_stage" in failure["traceback"]
    assert not records(tmp_path, "project_save_completed")


@pytest.mark.parametrize("returncode", [0, -1073741819])
def test_media_commands_record_bounded_stderr_not_stdout(
    tmp_path: Path, monkeypatch, returncode: int,
) -> None:
    stderr = "diagnostic " * 600 + " final error"
    completed = subprocess.CompletedProcess([], returncode, PRIVATE_PAYLOAD, stderr)
    monkeypatch.setattr(MediaTools, "_capture", lambda *_args, **_kwargs: completed)
    media = MediaTools.__new__(MediaTools)
    command = ["ffmpeg.exe", "-i", "https://user:password@example.test/media?signature=secret"]
    with ApplicationDiagnostics(tmp_path):
        if returncode:
            with pytest.raises(MediaError):
                media.run(command, "Converting test media")
        else:
            assert media.run(command, "Converting test media") is completed
    event = "media_command_failed" if returncode else "media_command_completed"
    result = records(tmp_path, event)[0]
    assert result["returncode"] == returncode
    assert result["returncode_hex"] == ("0xC0000005" if returncode else "0x00000000")
    assert len(result["stderr"]) <= 4096
    assert result["stderr"].endswith("final error")
    assert result["stderr_truncated"]
    assert result["duration_seconds"] >= 0
    assert result["command_id"] == records(tmp_path, "media_command_started")[0]["command_id"]
    text = application_log_path(tmp_path).read_text(encoding="utf-8")
    assert "signature" not in text and "user:password" not in text
    assert_private_content_absent(tmp_path)


def test_media_launch_errors_and_probe_metadata_are_logged(tmp_path: Path, monkeypatch) -> None:
    media = MediaTools.__new__(MediaTools)
    media.ffprobe = "ffprobe.exe"

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("executable missing")

    monkeypatch.setattr(media, "_capture", missing)
    with ApplicationDiagnostics(tmp_path):
        with pytest.raises(FileNotFoundError):
            media.probe(tmp_path / "video.mp4")
        stdout = json.dumps({
            "streams": [
                {"codec_type": "video", "width": 1280, "height": 720, "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": 48000, "channels": 2},
            ],
            "format": {"duration": "10", "tags": {"comment": PRIVATE_PAYLOAD}},
        })
        monkeypatch.setattr(
            media, "_capture",
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout, ""),
        )
        assert media.probe(tmp_path / "video.mp4").has_audio
    assert records(tmp_path, "media_command_launch_failed")[0]["error_type"] == "FileNotFoundError"
    assert records(tmp_path, "media_probed")[0]["width"] == 1280
    assert_private_content_absent(tmp_path)


def test_binary_audio_commands_do_not_log_samples(tmp_path: Path, monkeypatch) -> None:
    samples = array("h", [0, 32760, 0]).tobytes()
    monkeypatch.setattr(
        MediaTools, "_capture",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, samples, b""),
    )
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "ffmpeg.exe"
    with ApplicationDiagnostics(tmp_path):
        assert media.decoded_audio_stats(tmp_path / "audio.mp3").has_activity
        assert media.has_audio_activity(tmp_path / "video.mp4", 1, 2)
    assert len(records(tmp_path, "media_command_completed")) == 2
    assert all("stdout" not in row for row in records(tmp_path))
    assert repr(samples) not in application_log_path(tmp_path).read_text(encoding="utf-8")


def test_media_stderr_redacts_long_signed_urls_before_truncation(tmp_path: Path) -> None:
    stderr = "request failed https://example.test/file?signature=" + PRIVATE_PAYLOAD * 1000
    with ApplicationDiagnostics(tmp_path):
        started = MediaTools._command_started(["ffmpeg.exe"], "Test error")
        MediaTools._command_finished(1, stderr.encode(), started)
    assert records(tmp_path, "media_command_failed")[0]["stderr_truncated"]
    assert_private_content_absent(tmp_path)


def test_waveform_cancellation_logs_reaped_process_without_payload(tmp_path: Path, monkeypatch) -> None:
    media = MediaTools.__new__(MediaTools)
    media.ffmpeg = "ffmpeg.exe"
    ready = tmp_path / "ready"
    children = []
    real_popen = subprocess.Popen
    real_capture = media._capture

    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    def capture(_command):
        return real_capture([
            sys.executable, "-u", "-c",
            "import pathlib,sys,time; print(sys.argv[2], flush=True); "
            "pathlib.Path(sys.argv[1]).touch(); time.sleep(30)",
            str(ready), PRIVATE_PAYLOAD,
        ])

    monkeypatch.setattr(media_module.subprocess, "Popen", launch)
    monkeypatch.setattr(media, "_capture", capture)
    with ApplicationDiagnostics(tmp_path):
        assert media.waveform_peaks(tmp_path / "video.mp4", 10, cancelled=ready.exists) == []
    assert ready.exists()
    assert children and children[0].poll() is not None
    assert records(tmp_path, "media_command_canceled")
    assert_private_content_absent(tmp_path)


@pytest.fixture
def fake_youtube(tmp_path: Path, monkeypatch, inline_youtube_worker):
    state = SimpleNamespace(caption_failure=False, download_failure=False)

    class Downloader:
        sanitize_info = staticmethod(YoutubeDL.sanitize_info)

        def __init__(self, options, **_kwargs):
            self.options = options
            self.cookiejar = CookieJar()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def add_info_extractor(self, _extractor):
            pass

        def add_post_processor(self, processor, **_kwargs):
            self.processor = processor

        def extract_info(self, *_args, **_kwargs):
            return {
                "title": PRIVATE_PAYLOAD, "duration": 10,
                "subtitles": {"en": [{
                    "ext": "json3", "url": "https://youtube.com/api/timedtext?secret=signed",
                }]},
            }

        def urlopen(self, _url):
            if state.caption_failure:
                raise OSError("caption service unavailable")
            return io.BytesIO(json.dumps({"events": [{
                "tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": PRIVATE_CAPTION}],
            }]}).encode())

        def process_ie_result(self, info, **_kwargs):
            self.processor.run({
                **info, "format_id": "18", "vcodec": "h264", "acodec": "aac", "filesize": 1000,
            })
            logger = self.options["logger"]
            logger.debug("[debug] Extracting video details")
            logger.info("[info] Downloading one video")
            logger.warning("upstream retry https://youtube.com/api?signature=signed")
            if state.download_failure:
                raise DownloadError("download service unavailable")
            Path(self.options["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")
            self.options["progress_hooks"][0]({
                "status": "finished", "downloaded_bytes": 1000, "info_dict": {"format_id": "18"},
            })
            self.options["postprocessor_hooks"][0]({"status": "finished", "postprocessor": "Merger"})

    monkeypatch.setattr(youtube, "YoutubeDL", Downloader)
    monkeypatch.setattr(youtube, "youtube_runtime_path", lambda: tmp_path / "deno.exe")
    media = SimpleNamespace(
        ffmpeg=str(tmp_path / "ffmpeg.exe"),
        probe=lambda _path: SimpleNamespace(duration=10, has_audio=True),
    )

    def download(*, cancelled=lambda: False):
        return youtube.download_youtube(
            media, "https://youtu.be/abcdefghijk", tmp_path, "auto",
            progress=lambda *_args: None, cancelled=cancelled,
        )

    return state, download


@pytest.mark.parametrize("caption_failure", [False, True])
def test_youtube_success_and_caption_fallback_log_no_captions(
    tmp_path: Path, fake_youtube, caption_failure: bool,
) -> None:
    state, download = fake_youtube
    state.caption_failure = caption_failure
    with ApplicationDiagnostics(tmp_path):
        result = download()
    assert result.video_path.is_file()
    assert bool(result.captions) is not caption_failure
    ready = records(tmp_path, "youtube_download_ready")[0]
    assert ready["needs_transcription"] is caption_failure
    assert ready["caption_count"] == (0 if caption_failure else 1)
    assert records(tmp_path, "youtube_download_completed")
    assert records(tmp_path, "youtube_postprocessing")
    assert records(tmp_path, "youtube_downloader_debug")
    assert records(tmp_path, "youtube_transfer")[0]["downloaded_bytes"] == 1000
    if caption_failure:
        assert records(tmp_path, "youtube_caption_download_failed")
    assert_private_content_absent(tmp_path)
    assert "signature" not in application_log_path(tmp_path).read_text(encoding="utf-8")


def test_youtube_failures_and_cancellation_keep_cleanup_and_log_outcomes(
    tmp_path: Path, fake_youtube,
) -> None:
    state, download = fake_youtube
    state.download_failure = True
    with ApplicationDiagnostics(tmp_path):
        with pytest.raises(youtube.YouTubeError):
            download()
        with pytest.raises(youtube.YouTubeCancelled):
            download(cancelled=lambda: True)
        with pytest.raises(youtube.YouTubeError):
            youtube.normalize_youtube_url("https://example.test/not-youtube")
    assert records(tmp_path, "youtube_download_failed")
    assert records(tmp_path, "youtube_download_canceled")
    assert records(tmp_path, "youtube_url_validation_failed")
    assert not list(tmp_path.glob(".cvpc-youtube-*"))
    assert_private_content_absent(tmp_path)


def test_transfer_byte_diagnostics_are_throttled_across_threads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(youtube, "time", SimpleNamespace(monotonic=lambda: 100.0))
    with ApplicationDiagnostics(tmp_path), diagnostic_operation("youtube_transfer_test"):
        tracker = youtube._DownloadProgress(lambda *_args: None, lambda: False)
        tracker.prepare({"format_id": "18", "filesize": 1000})

        def emit(index: int):
            tracker.download_hook({
                "status": "downloading", "downloaded_bytes": index,
                "fragment_index": index, "fragment_count": 100, "speed": 20,
                "info_dict": {"format_id": "18", "title": PRIVATE_PAYLOAD},
            })

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(emit, range(100)))
        tracker.download_hook({
            "status": "finished", "downloaded_bytes": 1000, "info_dict": {"format_id": "18"},
        })
    transfers = records(tmp_path, "youtube_transfer")
    assert len(transfers) == 2
    assert transfers[0]["speed_bytes_per_second"] == 20
    assert transfers[-1]["downloaded_bytes"] == 1000
    assert all(
        row["operation"] == records(tmp_path, "youtube_transfer_test_started")[0]["operation"]
        for row in transfers
    )
    assert_private_content_absent(tmp_path)


def test_downloader_log_omits_payloads_and_bounds_messages(tmp_path: Path) -> None:
    logger = youtube._DownloadLogger([], lambda: False)
    with ApplicationDiagnostics(tmp_path):
        logger.debug(json.dumps({"title": PRIVATE_PAYLOAD}))
        logger.warning("x" * 10000)
        for index in range(250):
            logger.info(f"[info] step-{index}")
    assert records(tmp_path, "youtube_downloader_debug")[0]["message"] == "[downloader payload omitted]"
    assert len(records(tmp_path, "youtube_downloader_warning")[0]["message"]) <= 768
    assert len(records(tmp_path, "youtube_downloader_messages_limited")) == 1
    assert_private_content_absent(tmp_path)


class ExportMedia:
    def convert_video(self, _source, destination, _height, _fps, *, encoding_progress):
        encoding_progress(VideoEncodingProgress(45, 30, 1))
        destination.write_bytes(b"media")

    def probe(self, _path):
        return MediaInfo(10, 1280, 720, 30, True, "theora", "vorbis", "yuv420p", 48000, 2)

    def probe_audio(self, _path):
        return SimpleNamespace(duration=1, codec="mp3", sample_rate=48000, channels=1)

    def probe_audio_duration(self, _path):
        return 1

    def probe_image_dimensions(self, _path):
        return 1280, 720

    def make_icon(self, _source, destination, **_kwargs):
        destination.write_bytes(b"image")

    def create_silent_backing(self, destination, _duration):
        destination.write_bytes(b"audio")

    def audio_peak_dbfs(self, _path):
        return float("-inf")


@pytest.mark.parametrize("preserve_video", [True, False])
def test_pack_export_and_import_report_counts_not_metadata(
    tmp_path: Path, preserve_video: bool,
) -> None:
    source = tmp_path / "source.ogv"
    audio = tmp_path / "source.mp3"
    image = tmp_path / "source.png"
    for path in (source, audio, image):
        path.write_bytes(b"media")
    project = PackProject(
        title="Pack", authors=[PRIVATE_AUTHOR], video_path=str(source), video_duration=10,
        preserve_source_video=preserve_video, video_height=720, video_fps=30,
        segments=[Segment(
            1, 2, PRIVATE_CAPTION, [PRIVATE_AUTHOR], audio_mode="file",
            audio_path=str(audio), image_path=str(image),
        )],
    )
    exporter = PackExporter(ExportMedia())
    exporter.validator = SimpleNamespace(
        validate_folder=lambda *_args, **_kwargs: {"status": "passed", "payload": PRIVATE_PAYLOAD},
        validate_zip=lambda *_args, **_kwargs: None,
    )
    with ApplicationDiagnostics(tmp_path):
        result = exporter.export(project, tmp_path / "output")
        imported = PackImporter(ExportMedia()).import_folder(result.pack_path)
        assert imported.project.segments[0].caption == PRIVATE_CAPTION
    assert records(tmp_path, "pack_export_ready")[0]["segment_count"] == 1
    assert records(tmp_path, "pack_export_completed")
    assert records(tmp_path, "pack_import_ready")[0]["segment_count"] == 1
    assert records(tmp_path, "pack_import_completed")
    assert_private_content_absent(tmp_path)


def test_pack_failures_and_publish_rollback_are_logged(tmp_path: Path) -> None:
    target = tmp_path / "pack"
    target.mkdir()
    (target / "old.txt").write_text("original", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    new_file = stage / "new.txt"
    new_file.write_text(PRIVATE_PAYLOAD, encoding="utf-8")
    exporter = PackExporter(ExportMedia())

    def fail_validation(*_args, **_kwargs):
        raise ValueError("validation failed")

    exporter.validator = SimpleNamespace(validate_folder=fail_validation)
    with ApplicationDiagnostics(tmp_path):
        with pytest.raises(ValueError):
            PackImporter(ExportMedia()).import_folder(tmp_path / "missing")
        with pytest.raises(ValueError):
            exporter.export(PackProject(), tmp_path / "output")
        with pytest.raises(ValueError):
            exporter._publish_verified(
                stage, target, None, None, "pack", {"new.txt": sha256(new_file)}, 1,
            )
    assert (target / "old.txt").read_text(encoding="utf-8") == "original"
    assert records(tmp_path, "pack_import_failed")
    assert records(tmp_path, "pack_export_failed")
    assert records(tmp_path, "pack_publish_failed")
    assert records(tmp_path, "pack_rollback_completed")
    assert_private_content_absent(tmp_path)


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_update_check_worker_logs_outcome(tmp_path: Path, monkeypatch, outcome: str) -> None:
    def find(**_kwargs):
        if outcome == "failure":
            raise UpdateError("release lookup failed")
        if outcome == "cancel":
            raise UpdateCancelled("update canceled")
        return None

    monkeypatch.setattr(update_controller, "find_release", find)
    worker = update_controller.UpdateWorker(include_prereleases=False)
    with ApplicationDiagnostics(tmp_path):
        worker.run()
    event = {"success": "completed", "failure": "failed", "cancel": "canceled"}[outcome]
    assert records(tmp_path, f"update_check_{event}")
    assert worker.was_cancelled is (outcome == "cancel")
    assert bool(worker.error) is (outcome == "failure")


def test_update_preparation_logs_throttled_progress_and_ready_paths(tmp_path: Path, monkeypatch) -> None:
    release = Release("99.0.0", "v99.0.0", False, "https://example.test/app.zip", 1000, "", None)
    prepared = PreparedUpdate(tmp_path / "stage", tmp_path / "portable", release.version)

    def prepare(_release, _target, progress, _cancelled):
        for _ in range(100):
            progress("Downloading update...", 0.5)
        progress("Verifying and staging update...", 1)
        return prepared

    monkeypatch.setattr(update_controller, "prepare_update", prepare)
    worker = update_controller.UpdateWorker(
        include_prereleases=False, release=release, target=prepared.target,
    )
    with ApplicationDiagnostics(tmp_path):
        worker.run()
    assert worker.result is prepared
    assert records(tmp_path, "update_prepare_completed")
    assert records(tmp_path, "update_prepared")[0]["version"] == "99.0.0"
    assert len(records(tmp_path, "update_progress")) == 2


@pytest.mark.parametrize("canceled", [False, True])
def test_update_prepare_failures_and_cancellation_are_logged(
    tmp_path: Path, monkeypatch, canceled: bool,
) -> None:
    release = Release("99.0.0", "v99.0.0", False, "https://example.test/app.zip", 1000, "", None)

    def prepare(*_args):
        if canceled:
            raise UpdateCancelled("download stopped")
        raise UpdateError("download failed")

    monkeypatch.setattr(update_controller, "prepare_update", prepare)
    worker = update_controller.UpdateWorker(
        include_prereleases=False, release=release, target=tmp_path / "portable",
    )
    with ApplicationDiagnostics(tmp_path):
        worker.run()
    assert records(tmp_path, "update_prepare_canceled" if canceled else "update_prepare_failed")
    assert worker.was_cancelled is canceled
    assert bool(worker.error) is not canceled


@pytest.mark.parametrize("failed", [False, True])
def test_update_handoff_logs_result_and_preserves_failure_behavior(
    tmp_path: Path, monkeypatch, failed: bool,
) -> None:
    prepared = PreparedUpdate(tmp_path / "stage", tmp_path / "portable", "99.0.0")

    def launch(*_args):
        if failed:
            raise UpdateError("helper startup failed")

    monkeypatch.setattr(update_controller, "launch_update", launch)
    timer = SimpleNamespace(stop=lambda: None)
    controller = SimpleNamespace(
        prepared=prepared, downloaded=None, window=SimpleNamespace(project_path=None),
        startup_timer=timer, prompt_timer=timer, _show_error=lambda _message: None,
    )
    with ApplicationDiagnostics(tmp_path):
        assert update_controller.UpdateController.install_on_close(controller) is not failed
    assert controller.prepared is None
    assert records(tmp_path, "update_handoff_failed" if failed else "update_handoff_completed")
