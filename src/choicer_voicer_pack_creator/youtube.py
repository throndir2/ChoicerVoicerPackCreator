from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlsplit

import deno
from yt_dlp import YoutubeDL
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.networking.exceptions import HTTPError, RequestError
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, DownloadError

from choicer_voicer_pack_creator.analysis import CancelCallback, ProgressCallback
from choicer_voicer_pack_creator.captions import parse_json3
from choicer_voicer_pack_creator.diagnostics import (
    DiagnosticProgress,
    diagnostic_event,
    diagnostic_exception,
    diagnostic_operation,
    diagnostic_text,
    forward_diagnostics,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import SourceCaption
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.process_worker import (
    ProcessWorkerCancelled,
    ProcessWorkerError,
    ProcessWorkerTimeout,
    run_process_worker,
)

METADATA_TIMEOUT = 60.0
CAPTION_TIMEOUT = 30.0
TRANSFER_IDLE_TIMEOUT = 120.0
POSTPROCESS_IDLE_TIMEOUT = 600.0
PROBE_TIMEOUT = 60.0


class YouTubeError(ValueError):
    pass


class YouTubeCancelled(DownloadCancelled, OperationCancelled):
    pass


class YouTubeNetworkError(YouTubeError):
    pass


class YouTubeProcessError(RuntimeError):
    pass


class _PublicYoutubeIE(YoutubeIE):
    @staticmethod
    def _is_agegated(player_response: dict[str, Any]) -> bool:
        # The pinned extractor otherwise automatically tries an embedded-player age-gate fallback.
        if YoutubeIE._is_agegated(player_response):
            raise YouTubeError("Age-restricted videos are not supported by this importer.")
        return False


@dataclass(frozen=True, slots=True)
class CaptionTrack:
    language: str
    url: str
    automatic: bool


@dataclass(frozen=True, slots=True)
class YouTubeDownload:
    video_path: Path
    title: str
    duration: float
    url: str
    language: str
    captions: list[SourceCaption]
    warnings: list[str]


@diagnostic_operation("youtube_runtime")
def youtube_runtime_path() -> Path:
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "runtime" / "deno" / "deno.exe"
    else:
        path = Path(deno.find_deno_bin()).resolve()
    available = path.is_file()
    diagnostic_event("youtube_runtime_path", path=path, available=available)
    if not available:
        raise YouTubeError(
            f"The bundled YouTube JavaScript runtime is missing: {path}. "
            "Restore the complete application folder or reinstall the source dependencies."
        )
    return path


@diagnostic_operation("youtube_url_validation")
def normalize_youtube_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.username or parsed.password or parsed.port
    ):
        raise YouTubeError("Enter an http(s) YouTube video URL without credentials or a port.")
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    video_id = ""
    if host in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
        video_id = parts[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts == ["watch"]:
            ids = parse_qs(parsed.query).get("v", [])
            if len(ids) == 1:
                video_id = ids[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise YouTubeError("Enter a single YouTube video URL, not a channel or playlist URL.")
    diagnostic_event("youtube_url_validated", host=host)
    return f"https://www.youtube.com/watch?v={video_id}"


def select_caption_track(info: dict[str, Any], language: str) -> CaptionTrack | None:
    tracks: list[CaptionTrack] = []
    original: str | None = None
    for key, automatic in (("subtitles", False), ("automatic_captions", True)):
        for code, formats in (info.get(key) or {}).items():
            for item in formats:
                url = item.get("url", "")
                parsed = urlsplit(url)
                if (
                    item.get("ext") != "json3"
                    or parsed.scheme != "https"
                    or parsed.hostname not in {"www.youtube.com", "youtube.com"}
                    or "tlang" in parse_qs(parsed.query)
                ):
                    continue
                normalized = code.removesuffix("-orig")
                tracks.append(CaptionTrack(normalized, url, automatic))
                if automatic and code.endswith("-orig"):
                    original = normalized
                break
    requested = language if language != "auto" else str(info.get("language") or original or "")
    if requested:
        matches = [track for track in tracks if track.language.casefold() == requested.casefold()]
        if not matches:
            matches = [
                track for track in tracks
                if track.language.split("-")[0].casefold() == requested.split("-")[0].casefold()
            ]
        if matches:
            return min(matches, key=lambda track: track.automatic)
        if language != "auto":
            return None
    return min(tracks, key=lambda track: track.automatic) if tracks else None


def _check_cancel(cancelled: CancelCallback) -> None:
    if cancelled():
        raise YouTubeCancelled("YouTube download canceled")


class _DownloadLogger:
    def __init__(self, warnings: list[str], cancelled: CancelCallback) -> None:
        self.warnings = warnings
        self.cancelled = cancelled
        self._messages = 0
        self._lock = Lock()
        self._diagnostic_context = copy_context()
        self._progress = {
            level: DiagnosticProgress(f"youtube_downloader_{level}")
            for level in ("debug", "info")
        }

    def _record(self, level: str, message: str) -> None:
        # yt-dlp may include signed URLs or dump server responses. Keep only short
        # technical messages; transfer hooks below provide structured byte counts.
        message = str(message)
        if any(marker in message for marker in ("{", "<html", "<?xml", "<!DOCTYPE")):
            message = "[downloader payload omitted]"
        else:
            message = diagnostic_text(message.splitlines()[0], limit=768) if message else ""
        if level in self._progress and re.search(r"\bfragment\b", message, re.IGNORECASE):
            return
        with self._lock:
            self._messages += 1
            if self._messages > 200:
                if self._messages == 201:
                    self._diagnostic_context.run(
                        diagnostic_event, "youtube_downloader_messages_limited", limit=200,
                    )
                return
            if level in self._progress:
                self._diagnostic_context.run(self._progress[level].report, message, None)
            else:
                self._diagnostic_context.run(
                    diagnostic_event, f"youtube_downloader_{level}", message=message,
                )

    def debug(self, message: str) -> None:
        _check_cancel(self.cancelled)
        self._record("debug", message)

    def info(self, message: str) -> None:
        _check_cancel(self.cancelled)
        self._record("info", message)

    def warning(self, message: str) -> None:
        _check_cancel(self.cancelled)
        if message not in self.warnings:
            self.warnings.append(message)
            self._record("warning", message)

    def error(self, message: str) -> None:
        self._record("error", message)
        self.warning(message)


def _positive_size(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def _diagnostic_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


@dataclass(slots=True)
class _Transfer:
    format_id: str | None
    kind: str
    total: float | None
    estimated: bool
    downloaded: float = 0
    finished: bool = False


class _DownloadProgress:
    def __init__(
        self, progress: ProgressCallback, cancelled: CancelCallback,
        activity: Callable[[], None] = lambda: None,
    ) -> None:
        self.progress = progress
        self.cancelled = cancelled
        self.activity = activity
        self.transfers: list[_Transfer] = []
        self.fraction = 0.0
        self.held_estimate = False
        self._unmatched_downloaded = 0.0
        self._lock = Lock()
        self._diagnostic_last = float("-inf")
        self._diagnostic_context = copy_context()
        self._postprocessing = DiagnosticProgress("youtube_postprocessing")

    def prepare(self, info: dict[str, Any]) -> None:
        _check_cancel(self.cancelled)
        # before_dl receives the actual selection, including a single combined-format fallback.
        for selected in info.get("requested_formats") or [info]:
            size = _positive_size(selected.get("filesize"))
            kind = (
                "audio" if selected.get("vcodec") == "none"
                else "video" if selected.get("acodec") == "none"
                else "video and audio"
            )
            self.transfers.append(_Transfer(
                selected.get("format_id"), kind,
                size or _positive_size(selected.get("filesize_approx")), size is None,
            ))
            diagnostic_event(
                "youtube_format_selected", format_id=selected.get("format_id"),
                kind=kind, extension=selected.get("ext"), protocol=selected.get("protocol"),
                video_codec=selected.get("vcodec"), audio_codec=selected.get("acodec"),
                width=_diagnostic_number(selected.get("width")),
                height=_diagnostic_number(selected.get("height")),
                fps=_diagnostic_number(selected.get("fps")),
                total_bytes=self.transfers[-1].total, estimated=self.transfers[-1].estimated,
            )
        diagnostic_event("youtube_transfers_prepared", transfer_count=len(self.transfers))
        self.progress("Preparing selected YouTube downloads...", None)

    def _report_diagnostics(self, status: dict[str, Any], transfer: _Transfer | None) -> None:
        now = time.monotonic()
        if status["status"] != "finished" and now - self._diagnostic_last < 1:
            return
        self._diagnostic_last = now
        self._diagnostic_context.run(
            diagnostic_event, "youtube_transfer", status=status["status"],
            format_id=transfer.format_id if transfer else None,
            kind=transfer.kind if transfer else "unmatched",
            downloaded_bytes=transfer.downloaded if transfer else (
                _positive_size(status.get("downloaded_bytes")) or 0
            ),
            total_bytes=transfer.total if transfer else _positive_size(status.get("total_bytes")),
            estimated=transfer.estimated if transfer else None,
            speed_bytes_per_second=_diagnostic_number(status.get("speed")),
            eta_seconds=_diagnostic_number(status.get("eta")),
            elapsed_seconds=_diagnostic_number(status.get("elapsed")),
            fragment_index=_diagnostic_number(status.get("fragment_index")),
            fragment_count=_diagnostic_number(status.get("fragment_count")),
            transfer_count=len(self.transfers),
            combined_downloaded_bytes=sum(item.downloaded for item in self.transfers),
        )

    def _report_transfer(self, message: str) -> None:
        if not self.transfers or any(transfer.total is None for transfer in self.transfers):
            self.progress(f"{message} — total size unknown", None)
            return
        total = sum(transfer.total for transfer in self.transfers)
        downloaded = sum(
            transfer.total if transfer.finished else min(transfer.downloaded, transfer.total)
            for transfer in self.transfers
        )
        fraction = downloaded / total
        # Hooks contain cumulative bytes, not deltas. Re-estimates and retries can lower the
        # measured ratio; retain the high-water estimate instead of counting bytes twice.
        self.held_estimate |= fraction < self.fraction
        self.fraction = min(0.999, max(self.fraction, fraction))
        estimated = self.held_estimate or any(transfer.estimated for transfer in self.transfers)
        label = "estimated combined transfer progress" if estimated else "combined transfer progress"
        self.progress(f"{message} — {label}", self.fraction)

    def download_hook(self, status: dict[str, Any]) -> None:
        # Native DASH can report video and audio from different threads.
        with self._lock:
            self._download_hook(status)

    def _download_hook(self, status: dict[str, Any]) -> None:
        _check_cancel(self.cancelled)
        if status.get("status") not in {"downloading", "finished"}:
            return
        info = status.get("info_dict") or {}
        matches = [
            (index, transfer) for index, transfer in enumerate(self.transfers, 1)
            if transfer.format_id == info.get("format_id")
        ]
        if len(matches) != 1:
            # External downloaders may report a whole merged selection rather than a stream.
            # Do not guess how those bytes should be allocated across the selected formats.
            downloaded = _positive_size(status.get("downloaded_bytes")) or 0
            if downloaded > self._unmatched_downloaded:
                self.activity()
            self._unmatched_downloaded = downloaded
            self._report_diagnostics(status, None)
            self.progress("Downloading YouTube video and audio — transfer size unavailable", None)
            return
        index, transfer = matches[0]
        size = _positive_size(status.get("total_bytes"))
        estimate = _positive_size(status.get("total_bytes_estimate"))
        if size is not None:
            transfer.total, transfer.estimated = size, False
        elif estimate is not None and (transfer.estimated or transfer.total is None):
            transfer.total, transfer.estimated = estimate, True
        downloaded = status.get("downloaded_bytes")
        if (
            (_positive_size(downloaded) is not None and downloaded > transfer.downloaded)
            or (status["status"] == "finished" and not transfer.finished)
        ):
            self.activity()
        if downloaded == 0 or _positive_size(downloaded) is not None:
            transfer.downloaded = float(downloaded)
        if status["status"] == "finished":
            transfer.finished = True
            if _positive_size(downloaded) is not None:
                transfer.total, transfer.estimated = transfer.downloaded, False
            self._report_diagnostics(status, transfer)
            if all(item.finished for item in self.transfers):
                self._diagnostic_context.run(
                    diagnostic_event, "youtube_transfers_finished", transfer_count=len(self.transfers),
                    downloaded_bytes=sum(item.downloaded for item in self.transfers),
                )
                self.progress("YouTube transfers finished; preparing downloaded media...", None)
                return
            message = f"YouTube {transfer.kind} transfer finished"
        else:
            transfer.finished = False
            self._report_diagnostics(status, transfer)
            message = f"Downloading YouTube {transfer.kind}"
        message += f" ({index} of {len(self.transfers)} transfers)"
        self._report_transfer(message)

    def postprocessor_hook(self, status: dict[str, Any]) -> None:
        _check_cancel(self.cancelled)
        if (
            status.get("status") not in {"started", "processing", "finished"}
            or status.get("postprocessor") == _DownloadProgressPP.pp_key()
        ):
            return
        if status.get("postprocessor") == "Merger":
            message = (
                "Merge finished; preparing downloaded media..."
                if status["status"] == "finished"
                else "Merging downloaded video and audio..."
            )
        else:
            message = "Preparing downloaded media..."
        with self._lock:
            self._diagnostic_context.run(
                self._postprocessing.report,
                f"{status.get('postprocessor', 'unknown')}: {status['status']}",
                1.0 if status["status"] == "finished" else None,
            )
        self.progress(message, None)


class _DownloadProgressPP(PostProcessor):
    def __init__(self, tracker: _DownloadProgress) -> None:
        super().__init__()
        self.tracker = tracker

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        self.tracker.prepare(info)
        return [], info


@dataclass(slots=True)
class _YouTubeRequest:
    stage: Path
    url: str
    ffmpeg: str
    runtime: Path
    ipv4: bool = False
    cookies: list[Cookie] = field(default_factory=list)


@dataclass(slots=True)
class _YouTubeResponse:
    value: Any
    warnings: list[str]
    cookies: list[Cookie] = field(default_factory=list)


def _is_network_error(error: BaseException) -> bool:
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, HTTPError):
            return False
        if isinstance(error, (RequestError, TimeoutError, ConnectionError)):
            return True
        nested = getattr(error, "cause", None) or error.__cause__ or error.__context__
        if nested is None and isinstance(error, DownloadError) and error.exc_info:
            nested = error.exc_info[1]
        if not isinstance(nested, BaseException):
            return False
        error = nested
    return False


def _validate_youtube_metadata(info: Any) -> None:
    if (
        not isinstance(info, dict) or not info
        or info.get("_type", "video") != "video" or "entries" in info
    ):
        raise YouTubeError("The URL did not resolve to a single video.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise YouTubeError("Live and upcoming streams cannot be imported.")
    if (info.get("age_limit") or 0) >= 18 or info.get("availability") in {
        "private", "premium_only", "subscriber_only", "needs_auth",
    }:
        raise YouTubeError("This video requires access that the importer does not request.")


def _youtube_worker(
    emit: Callable[[str, dict[str, Any]], None],
    request: _YouTubeRequest,
    action: str,
    payload: Any,
) -> _YouTubeResponse:
    with forward_diagnostics(
        lambda event, details: emit("diagnostic", {"event": event, "details": details}),
    ):
        if action == "probe":
            media, video = payload
            return _YouTubeResponse(media.probe(video), [])
        notes: list[str] = []
        tracker = _DownloadProgress(
            lambda message, fraction: emit("progress", {"message": message, "fraction": fraction}),
            lambda: False,
            lambda: emit("activity", {}),
        )

        def postprocess(status: dict[str, Any]) -> None:
            if status.get("postprocessor") != _DownloadProgressPP.pp_key():
                emit("phase", {"postprocessing": True})
            tracker.postprocessor_hook(status)

        options = {
            "noplaylist": True,
            "format": "bv*+ba/b",
            "outtmpl": str(request.stage / "source.%(ext)s"),
            "merge_output_format": "mkv",
            "ffmpeg_location": str(Path(request.ffmpeg).parent),
            "cachedir": False,
            "quiet": True,
            "no_warnings": False,
            "logger": _DownloadLogger(notes, lambda: False),
            "progress_hooks": [tracker.download_hook],
            "postprocessor_hooks": [postprocess],
            "socket_timeout": 15,
            "source_address": "0.0.0.0" if request.ipv4 else None,
            "retries": 2,
            "extractor_retries": 2,
            "fragment_retries": 2,
            "skip_unavailable_fragments": False,
            "remote_components": set(),
            "js_runtimes": {"deno": {"path": str(request.runtime)}},
            "extractor_args": {
                "youtube": {"skip": ["translated_subs"], "fetch_pot": ["never"]},
            },
            "geo_bypass": False,
            "cookiefile": None,
            "cookiesfrombrowser": None,
            "usenetrc": False,
            "writesubtitles": False,
            "writeautomaticsub": False,
        }
        try:
            with YoutubeDL(options, auto_init=False) as downloader:
                for cookie in request.cookies:
                    downloader.cookiejar.set_cookie(cookie)
                downloader.add_info_extractor(_PublicYoutubeIE())
                downloader.add_post_processor(_DownloadProgressPP(tracker), when="before_dl")
                if action == "metadata":
                    value = downloader.extract_info(request.url, download=False, process=False)
                    _validate_youtube_metadata(value)
                    # Ended livestreams can contain generator-backed LazyList fragments.
                    # Materialize them while their extractor and deadline are still alive.
                    value = downloader.sanitize_info(value)
                elif action == "captions":
                    with downloader.urlopen(payload) as response:
                        raw = response.read(16 * 1024**2 + 1)
                    if len(raw) > 16 * 1024**2:
                        raise YouTubeError("Caption data exceeded the 16 MiB limit")
                    value = json.loads(raw)
                    diagnostic_event("youtube_caption_data_received", bytes=len(raw))
                elif action == "media":
                    downloader.process_ie_result(payload, download=True)
                    value = None
                else:
                    raise ValueError(f"Unknown YouTube worker action: {action}")
                cookies = list(downloader.cookiejar)
        except DownloadError as error:
            diagnostic_exception("youtube_extractor_failed", error)
            if _is_network_error(error):
                raise YouTubeNetworkError(str(error)) from error
            raise YouTubeError(
                "YouTube could not provide this video. It may be unavailable, restricted, or "
                "rate-limited. No sign-in or restriction bypass is attempted. "
                "You can also download an authorized copy separately and use New from Video."
                f"\n\n{error}"
            ) from error
        except RequestError as error:
            if _is_network_error(error):
                raise YouTubeNetworkError(str(error)) from error
            raise
        return _YouTubeResponse(value, notes, cookies)


def _run_youtube_stage(
    request: _YouTubeRequest, action: str, payload: Any, notes: list[str],
    progress: ProgressCallback, cancelled: CancelCallback,
) -> Any:
    labels = {
        "metadata": "Fetching YouTube video details...",
        "captions": "Fetching available YouTube captions...",
        "media": "Preparing selected YouTube downloads...",
        "probe": "Checking downloaded video...",
    }
    timeout = {
        "metadata": METADATA_TIMEOUT, "captions": CAPTION_TIMEOUT, "probe": PROBE_TIMEOUT,
    }.get(action)

    def on_event(event: str, details: dict[str, Any]) -> bool:
        nonlocal postprocessing, last_message, last_fraction, last_report
        _check_cancel(cancelled)
        if event == "diagnostic":
            diagnostic_event(details["event"], **details["details"])
        elif event == "progress":
            last_message, last_fraction = details["message"], details["fraction"]
            last_report = time.monotonic()
            progress(last_message, last_fraction)
        elif event == "phase":
            changed = postprocessing != details["postprocessing"]
            postprocessing = details["postprocessing"]
            return changed
        elif event == "activity":
            return True
        else:
            raise YouTubeError(f"Unknown YouTube worker event: {event}")
        return False

    def waiting(elapsed: float) -> None:
        nonlocal last_report
        if time.monotonic() - last_report < 5:
            return
        last_report = time.monotonic()
        diagnostic_event(
            "youtube_waiting", stage=action, ipv4=request.ipv4,
            stage_elapsed_seconds=round(elapsed, 1),
        )
        progress(
            f"{last_message} Waiting ({int(elapsed)}s elapsed); you can cancel.",
            last_fraction,
        )

    def idle_timeout() -> float:
        return POSTPROCESS_IDLE_TIMEOUT if postprocessing else TRANSFER_IDLE_TIMEOUT

    while True:
        postprocessing = False
        last_message, last_fraction = labels[action], None
        last_report = time.monotonic()
        progress(last_message, None)
        diagnostic_event(
            "youtube_stage_attempt", stage=action, ipv4=request.ipv4, timeout_seconds=timeout,
        )
        try:
            result = run_process_worker(
                _youtube_worker, (request, action, payload),
                on_event=on_event, cancelled=cancelled, timeout=timeout,
                idle_timeout=idle_timeout,
                waiting=waiting,
            )
            _check_cancel(cancelled)
            if not isinstance(result, _YouTubeResponse):
                raise YouTubeError("The YouTube worker returned an invalid result.")
            notes.extend(note for note in result.warnings if note not in notes)
            if action != "probe":
                request.cookies = result.cookies
            return result.value
        except ProcessWorkerCancelled as error:
            raise YouTubeCancelled("YouTube download canceled") from error
        except ProcessWorkerError as error:
            if error.error_type != "WorkerCleanupTimeout":
                _check_cancel(cancelled)
            diagnostic_exception(
                "youtube_stage_failed", error, stage=action, ipv4=request.ipv4,
                remote_traceback=error.remote_traceback,
            )
            if error.error_type == "WorkerCleanupTimeout":
                raise YouTubeProcessError(
                    f"Could not finish stopping the YouTube worker processes: {error}"
                ) from error
            network_failure = isinstance(error, ProcessWorkerTimeout) or (
                error.error_type == "YouTubeNetworkError"
            )
            if (
                network_failure and not request.ipv4 and not postprocessing
                and action != "probe"
            ):
                request.ipv4 = True
                note = "The default YouTube connection stalled or failed; retrying using IPv4."
                notes.append(note)
                diagnostic_event("youtube_ipv4_retry", stage=action)
                progress(note, None)
                continue
            if network_failure:
                if postprocessing or action == "probe":
                    raise YouTubeError(
                        "Preparing the downloaded media exceeded its wait limit. "
                        "The media tools have been stopped; existing media is unchanged. "
                        "Retry or use New from Video with an authorized local copy."
                    ) from error
                raise YouTubeError(
                    f"{labels[action].removesuffix('...')} did not finish within its wait limit "
                    "or the connection failed. Check your internet connection, proxy/VPN settings, "
                    "and whether your firewall permits this application. You can retry or open an "
                    "authorized local copy with New from Video."
                ) from error
            raise YouTubeError(str(error)) from error


@diagnostic_operation("youtube_download")
def download_youtube(
    media: MediaTools,
    url: str,
    destination: Path,
    language: str,
    *,
    progress: ProgressCallback,
    cancelled: CancelCallback,
) -> YouTubeDownload:
    diagnostic_event("youtube_download_requested", destination=destination, language=language)
    url = normalize_youtube_url(url)
    if language != "auto" and not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        raise YouTubeError("Caption language must be 'auto' or a language code such as en or pt-BR.")
    destination = destination.resolve()
    if not destination.is_dir():
        raise YouTubeError("Choose an existing destination folder.")
    _check_cancel(cancelled)
    notes: list[str] = []
    logged_progress = DiagnosticProgress("youtube_progress")
    callback = progress

    def progress(message: str, fraction: float | None) -> None:
        logged_progress.report(message, fraction)
        callback(message, fraction)

    packages = {}
    for package in ("yt-dlp", "yt-dlp-ejs", "deno"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = "unavailable"
    diagnostic_event(
        "youtube_environment", packages=packages, ffmpeg=media.ffmpeg,
        extractor=_PublicYoutubeIE.__name__, format_selector="bv*+ba/b",
    )

    with tempfile.TemporaryDirectory(prefix=".cvpc-youtube-", dir=destination) as temporary:
        stage = Path(temporary)
        diagnostic_event("youtube_staging_created", path=stage)
        request = _YouTubeRequest(stage, url, media.ffmpeg, youtube_runtime_path())
        with diagnostic_operation("youtube_metadata"):
            info = _run_youtube_stage(request, "metadata", None, notes, progress, cancelled)
        _check_cancel(cancelled)
        _validate_youtube_metadata(info)
        diagnostic_event(
            "youtube_metadata_received",
            format_count=len(info.get("formats") or []),
            subtitle_language_count=len(info.get("subtitles") or {}),
            automatic_caption_language_count=len(info.get("automatic_captions") or {}),
            duration=_diagnostic_number(info.get("duration")),
            live_status=info.get("live_status"),
        )
        track = select_caption_track(info, language)
        diagnostic_event(
            "youtube_caption_track_selected", available=track is not None,
            language=track.language if track else None,
            automatic=track.automatic if track else None,
        )
        captions_json: Any = None
        if track:
            try:
                captions_json = _run_youtube_stage(
                    request, "captions", track.url, notes, progress, cancelled,
                )
            except YouTubeError as error:
                diagnostic_exception("youtube_caption_download_failed", error)
                diagnostic_event("youtube_caption_fallback", reason="download_failed")
                notes.append(f"Captions could not be downloaded; Whisper can draft them: {error}")
        else:
            diagnostic_event("youtube_caption_fallback", reason="no_usable_track")
            notes.append(
                "No usable creator or automatic captions were available for the chosen "
                "language. Local Whisper will draft captions instead."
            )
        _check_cancel(cancelled)
        with diagnostic_operation("youtube_media_transfer"):
            _run_youtube_stage(request, "media", info, notes, progress, cancelled)
        _check_cancel(cancelled)
        files = [
            path for path in stage.iterdir()
            if path.is_file() and path.stem == "source"
            and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        ]
        diagnostic_event("youtube_download_inventory", video_file_count=len(files))
        if len(files) != 1:
            raise YouTubeError("The download did not produce exactly one complete video file.")
        video = files[0]
        media_info = _run_youtube_stage(
            request, "probe", (media, video), notes, progress, cancelled,
        )
        diagnostic_event(
            "youtube_media_probed", path=video, duration=media_info.duration,
            has_audio=media_info.has_audio,
        )
        if not math.isfinite(media_info.duration) or media_info.duration <= 0 or not media_info.has_audio:
            raise YouTubeError("The downloaded video needs a finite duration and an audio stream.")
        captions: list[SourceCaption] = []
        if captions_json is not None and track:
            try:
                captions = parse_json3(
                    captions_json, media_info.duration,
                    automatic=track.automatic, language=track.language,
                )
                diagnostic_event("youtube_caption_data_parsed", caption_count=len(captions))
                if not captions:
                    diagnostic_event("youtube_caption_fallback", reason="no_usable_timed_text")
                    notes.append("The caption track had no usable timed text; Whisper will draft it.")
            except (KeyError, TypeError, ValueError) as error:
                diagnostic_exception("youtube_caption_parse_failed", error)
                diagnostic_event("youtube_caption_fallback", reason="invalid_caption_data")
                notes.append(f"Caption data was invalid; Whisper will draft captions instead: {error}")
        progress("Publishing downloaded video...", None)
        _check_cancel(cancelled)
        folder = destination / f"YouTube-{parse_qs(urlsplit(url).query)['v'][0]}-{uuid.uuid4().hex[:8]}"
        # Publish only our unique staging directory, never an existing user's media folder.
        with diagnostic_operation("youtube_publish", source=stage, destination=folder):
            stage.rename(folder)
        result = YouTubeDownload(
            folder / video.name, str(info.get("title") or "YouTube video"),
            media_info.duration, url,
            track.language if track else (language if language != "auto" else ""),
            captions, notes,
        )
        diagnostic_event(
            "youtube_download_ready", path=result.video_path, duration=result.duration,
            caption_count=len(captions), warning_count=len(notes),
            needs_transcription=not captions,
        )
        progress("YouTube video ready", 1.0)
        return result
