from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlsplit

import deno
from yt_dlp import YoutubeDL
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, DownloadError

from choicer_voicer_pack_creator.analysis import CancelCallback, ProgressCallback
from choicer_voicer_pack_creator.captions import parse_json3
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import SourceCaption


class YouTubeError(ValueError):
    pass


class YouTubeCancelled(DownloadCancelled):
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


def youtube_runtime_path() -> Path:
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "runtime" / "deno" / "deno.exe"
    else:
        path = Path(deno.find_deno_bin()).resolve()
    if not path.is_file():
        raise YouTubeError(
            f"The bundled YouTube JavaScript runtime is missing: {path}. "
            "Restore the complete application folder or reinstall the source dependencies."
        )
    return path


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

    def debug(self, _message: str) -> None:
        _check_cancel(self.cancelled)

    def info(self, _message: str) -> None:
        _check_cancel(self.cancelled)

    def warning(self, message: str) -> None:
        _check_cancel(self.cancelled)
        if message not in self.warnings:
            self.warnings.append(message)

    def error(self, message: str) -> None:
        self.warning(message)


def _positive_size(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
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
    def __init__(self, progress: ProgressCallback, cancelled: CancelCallback) -> None:
        self.progress = progress
        self.cancelled = cancelled
        self.transfers: list[_Transfer] = []
        self.fraction = 0.0
        self.held_estimate = False
        self._lock = Lock()

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
        self.progress("Preparing selected YouTube downloads...", None)

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
        if downloaded == 0 or _positive_size(downloaded) is not None:
            transfer.downloaded = float(downloaded)
        if status["status"] == "finished":
            transfer.finished = True
            if _positive_size(downloaded) is not None:
                transfer.total, transfer.estimated = transfer.downloaded, False
            if all(item.finished for item in self.transfers):
                self.progress("YouTube transfers finished; preparing downloaded media...", None)
                return
            message = f"YouTube {transfer.kind} transfer finished"
        else:
            transfer.finished = False
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
        self.progress(message, None)


class _DownloadProgressPP(PostProcessor):
    def __init__(self, tracker: _DownloadProgress) -> None:
        super().__init__()
        self.tracker = tracker

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        self.tracker.prepare(info)
        return [], info


def download_youtube(
    media: MediaTools,
    url: str,
    destination: Path,
    language: str,
    *,
    progress: ProgressCallback,
    cancelled: CancelCallback,
) -> YouTubeDownload:
    url = normalize_youtube_url(url)
    if language != "auto" and not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        raise YouTubeError("Caption language must be 'auto' or a language code such as en or pt-BR.")
    destination = destination.resolve()
    if not destination.is_dir():
        raise YouTubeError("Choose an existing destination folder.")
    _check_cancel(cancelled)
    notes: list[str] = []
    tracker = _DownloadProgress(progress, cancelled)

    with tempfile.TemporaryDirectory(prefix=".cvpc-youtube-", dir=destination) as temporary:
        stage = Path(temporary)
        options = {
            "noplaylist": True,
            "format": "bv*+ba/b",
            "outtmpl": str(stage / "source.%(ext)s"),
            "merge_output_format": "mkv",
            "ffmpeg_location": str(Path(media.ffmpeg).parent),
            "cachedir": False,
            "quiet": True,
            "no_warnings": False,
            "logger": _DownloadLogger(notes, cancelled),
            "progress_hooks": [tracker.download_hook],
            "postprocessor_hooks": [tracker.postprocessor_hook],
            "socket_timeout": 15,
            "retries": 2,
            "extractor_retries": 2,
            "fragment_retries": 2,
            "skip_unavailable_fragments": False,
            "remote_components": set(),
            "js_runtimes": {"deno": {"path": str(youtube_runtime_path())}},
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
        progress("Fetching YouTube video details...", None)
        try:
            with YoutubeDL(options, auto_init=False) as downloader:
                downloader.add_info_extractor(_PublicYoutubeIE())
                downloader.add_post_processor(_DownloadProgressPP(tracker), when="before_dl")
                info = downloader.extract_info(url, download=False, process=False)
                _check_cancel(cancelled)
                if not info or info.get("_type", "video") != "video" or "entries" in info:
                    raise YouTubeError("The URL did not resolve to a single video.")
                if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
                    raise YouTubeError("Live and upcoming streams cannot be imported.")
                if (info.get("age_limit") or 0) >= 18 or info.get("availability") in {
                    "private", "premium_only", "subscriber_only", "needs_auth",
                }:
                    raise YouTubeError("This video requires access that the importer does not request.")
                track = select_caption_track(info, language)
                captions_json: Any = None
                if track:
                    progress("Fetching available YouTube captions...", None)
                    try:
                        with downloader.urlopen(track.url) as response:
                            raw = response.read(16 * 1024**2 + 1)
                        if len(raw) > 16 * 1024**2:
                            raise ValueError("Caption data exceeded the 16 MiB limit")
                        captions_json = json.loads(raw)
                    except (RequestError, OSError, ValueError) as error:
                        notes.append(f"Captions could not be downloaded; Whisper can draft them: {error}")
                else:
                    notes.append(
                        "No usable creator or automatic captions were available for the chosen "
                        "language. Local Whisper will draft captions instead."
                    )
                _check_cancel(cancelled)
                downloader.process_ie_result(info, download=True)
        except DownloadError as error:
            _check_cancel(cancelled)
            raise YouTubeError(
                "YouTube could not provide this video. It may be unavailable, restricted, or "
                "rate-limited. No sign-in or restriction bypass is attempted. "
                f"You can also download an authorized copy separately and use New from Video.\n\n{error}"
            ) from error
        _check_cancel(cancelled)
        files = [
            path for path in stage.iterdir()
            if path.is_file() and path.stem == "source"
            and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        ]
        if len(files) != 1:
            raise YouTubeError("The download did not produce exactly one complete video file.")
        video = files[0]
        progress("Checking downloaded video...", None)
        _check_cancel(cancelled)
        media_info = media.probe(video)
        if not math.isfinite(media_info.duration) or media_info.duration <= 0 or not media_info.has_audio:
            raise YouTubeError("The downloaded video needs a finite duration and an audio stream.")
        captions: list[SourceCaption] = []
        if captions_json is not None and track:
            try:
                captions = parse_json3(
                    captions_json, media_info.duration,
                    automatic=track.automatic, language=track.language,
                )
                if not captions:
                    notes.append("The caption track had no usable timed text; Whisper will draft it.")
            except (KeyError, TypeError, ValueError) as error:
                notes.append(f"Caption data was invalid; Whisper will draft captions instead: {error}")
        progress("Publishing downloaded video...", None)
        _check_cancel(cancelled)
        folder = destination / f"YouTube-{parse_qs(urlsplit(url).query)['v'][0]}-{uuid.uuid4().hex[:8]}"
        # Publish only our unique staging directory, never an existing user's media folder.
        stage.rename(folder)
        result = YouTubeDownload(
            folder / video.name, str(info.get("title") or "YouTube video"),
            media_info.duration, url,
            track.language if track else (language if language != "auto" else ""),
            captions, notes,
        )
        progress("YouTube video ready", 1.0)
        return result
