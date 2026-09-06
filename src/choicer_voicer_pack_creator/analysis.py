from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import warnings
import wave
import zipfile
from array import array
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from audioop import rms as _audio_rms
except ImportError:  # Removed from Python 3.13; retain the pure-Python fallback.
    _audio_rms = None

from choicer_voicer_pack_creator.captions import pad_source_ranges, refine_captions
from choicer_voicer_pack_creator.diagnostics import (
    diagnostic_event,
    diagnostic_exception,
    diagnostic_text,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import SourceCaption

ProgressCallback = Callable[[str, float | None], None]
CancelCallback = Callable[[], bool]
BUFFER_SIZE = 1024 * 1024
DIAGNOSTIC_HEARTBEAT_SECONDS = 5.0
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "huggingface.co",
    "cdn-lfs.hf.co",
    "cdn-lfs-us-1.hf.co",
    "cdn-lfs-eu-1.hf.co",
    "cas-bridge.xethub.hf.co",
}
ALLOWED_DOWNLOAD_HOST_SUFFIXES = (".cdn.hf.co", ".xethub.hf.co")


class AnalysisError(RuntimeError):
    pass


class AnalysisCancelled(AnalysisError):
    pass


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    cpu_threads: int
    memory_bytes: int | None
    available_memory_bytes: int | None
    recommended_model: str
    description: str


@dataclass(frozen=True, slots=True)
class ActivityRegion:
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class AnalysisSuggestion:
    start: float
    end: float
    caption: str
    source: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    suggestions: list[AnalysisSuggestion]
    activity_regions: int
    transcript_regions: int
    activity_threshold_db: float | None
    model_name: str | None
    detected_language: str | None
    hardware: HardwareProfile
    refined_captions: list[SourceCaption] | None = None


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "whisper-analysis-windows-x64.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(BUFFER_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified(
    url: str,
    destination: Path,
    expected_hash: str,
    expected_bytes: int,
    label: str,
    progress: ProgressCallback,
    cancelled: CancelCallback,
) -> Path:
    """Download a pinned optional component, retaining the shared verification policy."""
    diagnostic_event(
        "component_requested", component=label, destination=str(destination),
        expected_bytes=expected_bytes, expected_sha256=expected_hash,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        progress(f"Verifying cached {label}...", None)
        if destination.stat().st_size == expected_bytes and sha256(destination) == expected_hash:
            diagnostic_event("component_cache_verified", component=label)
            progress(f"Using verified cached {label}.", 1.0)
            return destination
        diagnostic_event(
            "component_cache_invalid", component=label, bytes=destination.stat().st_size,
        )
        destination.unlink()
    partial = destination.with_name(destination.name + ".partial")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ChoicerVoicerPackCreator-analysis/0.4"},
    )
    diagnostic_event("component_download_started", component=label, url=url)
    try:
        transfer_deadline = time.monotonic() + max(
            60.0, min(3600.0, 60.0 + expected_bytes / (128 * 1024))
        )
        with urllib.request.urlopen(request, timeout=10) as response, partial.open("wb") as output:
            final_url = response.geturl()
            parsed = urllib.parse.urlparse(final_url)
            approved_https_host = bool(
                parsed.hostname
                and (
                    parsed.hostname in ALLOWED_DOWNLOAD_HOSTS
                    or parsed.hostname.endswith(ALLOWED_DOWNLOAD_HOST_SUFFIXES)
                )
            )
            if parsed.scheme not in {"https", "file"} or (
                parsed.scheme == "https" and not approved_https_host
            ):
                raise AnalysisError(f"{label} redirected to an unapproved host: {final_url}")
            content_length = response.headers.get("Content-Length")
            diagnostic_event(
                "component_download_response", component=label,
                host=parsed.hostname, content_length=content_length,
            )
            if content_length is not None and int(content_length) != expected_bytes:
                raise AnalysisError(
                    f"{label} reported {content_length} bytes; expected {expected_bytes}."
                )
            downloaded = 0
            while True:
                _check_cancel(cancelled)
                if time.monotonic() > transfer_deadline:
                    raise AnalysisError(f"{label} download exceeded its time limit")
                try:
                    chunk = response.read(min(BUFFER_SIZE, expected_bytes - downloaded + 1))
                except (TimeoutError, OSError) as error:
                    if cancelled():
                        raise AnalysisCancelled("Video analysis was canceled") from None
                    raise AnalysisError(f"{label} download failed: {error}") from error
                if not chunk:
                    break
                if downloaded + len(chunk) > expected_bytes:
                    raise AnalysisError(
                        f"{label} exceeded its pinned size of {expected_bytes} bytes."
                    )
                output.write(chunk)
                downloaded += len(chunk)
                progress(
                    f"Downloading {label} ({downloaded / 1024**2:.1f} / "
                    f"{expected_bytes / 1024**2:.1f} MiB)…",
                    min(1.0, downloaded / max(1, expected_bytes)),
                )
            output.flush()
            os.fsync(output.fileno())
        progress(f"Verifying downloaded {label}...", None)
        _check_cancel(cancelled)
        actual_hash = sha256(partial)
        if partial.stat().st_size != expected_bytes or actual_hash != expected_hash:
            raise AnalysisError(
                f"{label} verification failed. Expected {expected_bytes} bytes / "
                f"{expected_hash}, received {partial.stat().st_size} bytes / {actual_hash}."
            )
        os.replace(partial, destination)
        diagnostic_event(
            "component_download_verified", component=label, bytes=downloaded, sha256=actual_hash,
        )
        return destination
    finally:
        partial.unlink(missing_ok=True)


def detect_hardware() -> HardwareProfile:
    logical_cpus = max(1, os.cpu_count() or 1)
    threads = max(1, min(12, logical_cpus - 1 if logical_cpus > 2 else logical_cpus))
    memory_bytes: int | None = None
    available_memory_bytes: int | None = None
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory_bytes = int(status.total_physical)
            available_memory_bytes = int(status.available_physical)
    recommended = (
        "base"
        if available_memory_bytes is None or available_memory_bytes >= 2 * 1024**3
        else "tiny"
    )
    memory_text = (
        (
            f"{available_memory_bytes / 1024**3:.1f} GiB available / "
            f"{memory_bytes / 1024**3:.1f} GiB RAM"
        )
        if memory_bytes is not None and available_memory_bytes is not None
        else "RAM unknown"
    )
    return HardwareProfile(
        cpu_threads=threads,
        memory_bytes=memory_bytes,
        available_memory_bytes=available_memory_bytes,
        recommended_model=recommended,
        description=(
            f"Optimized local CPU backend · {threads} threads · {memory_text}. "
            "No CUDA installation is required."
        ),
    )


def _check_cancel(cancelled: CancelCallback) -> None:
    if cancelled():
        diagnostic_event("cancellation_observed")
        raise AnalysisCancelled("Video analysis was canceled")


def _run_cancellable(
    command: list[str],
    description: str,
    cancelled: CancelCallback,
    *,
    cwd: Path | None = None,
    output_line: Callable[[str], None] | None = None,
    tick: Callable[[float], None] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    _check_cancel(cancelled)
    diagnostic_event(
        "process_starting", description=description, command=command,
        cwd=str(cwd) if cwd else None, timeout_seconds=timeout,
    )
    startupinfo = MediaTools._startup_info()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
        )
    except OSError as error:
        diagnostic_exception(
            "process_launch_failed", error, description=description,
            winerror=getattr(error, "winerror", None), errno=error.errno,
        )
        raise
    # Windows pipes need reader threads to report live output without blocking either stream.
    messages: queue.Queue[tuple[str, str | OSError | None]] = queue.Queue()
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def read_stream(name: str, stream: IO[str]) -> None:
        try:
            for line in stream:
                messages.put((name, line))
        except OSError as error:
            messages.put((name, error))
        finally:
            messages.put((name, None))

    readers = [
        threading.Thread(target=read_stream, args=(name, stream))
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        if stream is not None
    ]
    started = time.monotonic()
    last_output = started
    last_heartbeat = started
    for reader in readers:
        reader.start()
    try:
        diagnostic_event("process_started", description=description, pid=process.pid)
        closed = 0
        while closed < len(readers) or process.poll() is None:
            _check_cancel(cancelled)
            elapsed = time.monotonic() - started
            if time.monotonic() - last_heartbeat >= DIAGNOSTIC_HEARTBEAT_SECONDS:
                diagnostic_event(
                    "process_heartbeat", description=description, pid=process.pid,
                    process_elapsed_seconds=round(elapsed, 1),
                    seconds_since_output=round(time.monotonic() - last_output, 1),
                )
                last_heartbeat = time.monotonic()
            if timeout is not None and elapsed >= timeout:
                diagnostic_event("process_timeout", description=description, pid=process.pid)
                raise AnalysisError(
                    f"{description} exceeded its {timeout / 60:.1f}-minute time limit. "
                    "Try the Tiny model or a shorter video; existing drafts are unchanged."
                )
            if tick is not None:
                tick(elapsed)
            try:
                name, message = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if message is None:
                closed += 1
            elif isinstance(message, OSError):
                raise AnalysisError(f"Could not read {description} output: {message}") from message
            else:
                last_output = time.monotonic()
                captured[name].append(message)
                if name == "stderr":
                    diagnostic_event(
                        "process_stderr", description=description, pid=process.pid,
                        line=diagnostic_text(message.rstrip(), limit=2048),
                    )
                if output_line is not None:
                    output_line(message)
        process.wait()
    finally:
        termination = None
        if process.poll() is None:
            termination = "terminate"
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                termination = "kill"
                process.kill()
                process.wait()
        for reader in readers:
            reader.join()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        diagnostic_event(
            "process_exited", description=description, pid=process.pid,
            return_code=process.returncode, termination=termination,
            return_code_hex=(
                f"0x{process.returncode & 0xFFFFFFFF:08X}" if process.returncode is not None else None
            ),
            process_elapsed_seconds=round(time.monotonic() - started, 3),
            stdout_lines=len(captured["stdout"]), stderr_lines=len(captured["stderr"]),
        )
    stdout, stderr = "".join(captured["stdout"]), "".join(captured["stderr"])
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise AnalysisError(f"{description} failed: {detail}")
    return completed


def extract_analysis_audio(
    media: MediaTools,
    video: Path,
    destination: Path,
    progress: ProgressCallback,
    cancelled: CancelCallback,
) -> None:
    progress("Decoding 16 kHz mono analysis audio…", None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        media.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    _run_cancellable(command, "Decoding analysis audio", cancelled)
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise AnalysisError("The source video did not produce usable analysis audio")
    diagnostic_event("analysis_audio_ready", bytes=destination.stat().st_size)


def _percentile_sorted(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return -120.0
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def scan_audio_activity(
    wav_path: Path,
    duration: float,
    sensitivity: str,
    progress: ProgressCallback,
    cancelled: CancelCallback,
    *,
    raw: bool = False,
) -> tuple[list[ActivityRegion], float | None]:
    message = (
        "Measuring low-activity gaps for YouTube refinement…"
        if raw else "Measuring deterministic audio activity…"
    )
    progress(message, 0.0)
    with wave.open(str(wav_path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise AnalysisError("Analysis audio must be 16-bit mono PCM")
        sample_rate = source.getframerate()
        window_frames = max(1, round(sample_rate * 0.02))
        total_windows = max(1, math.ceil(source.getnframes() / window_frames))
        levels: list[float] = []
        for index in range(total_windows):
            _check_cancel(cancelled)
            payload = source.readframes(window_frames)
            if not payload:
                break
            payload = payload[: len(payload) - len(payload) % 2]
            if not payload:
                continue
            if _audio_rms is not None:
                normalized = _audio_rms(payload, 2) / 32768.0
            else:
                samples = array("h")
                samples.frombytes(payload)
                if sys.byteorder != "little":
                    samples.byteswap()
                mean_square = sum(sample * sample for sample in samples) / len(samples)
                normalized = math.sqrt(mean_square) / 32768.0
            levels.append(20.0 * math.log10(max(normalized, 1e-8)))
            if index % 250 == 0:
                progress(message, index / total_windows)
    _check_cancel(cancelled)
    ordered_levels = sorted(levels)
    if not levels or max(levels) < -58.0:
        return [], None

    noise_floor = _percentile_sorted(ordered_levels, 0.20)
    foreground = _percentile_sorted(ordered_levels, 0.92)
    threshold = noise_floor + max(7.0, (foreground - noise_floor) * 0.40)
    threshold += {"sensitive": -4.0, "balanced": 0.0, "conservative": 4.0}.get(
        sensitivity, 0.0
    )
    threshold = max(-58.0, min(-20.0, foreground - 3.0, threshold))
    if raw:
        # Quiet speech must not become a false pause merely because another sound is loud.
        threshold = min(threshold, -45.0)
    active = [level >= threshold for level in levels]

    max_gap = 0 if raw else round(0.28 / 0.02)
    index = 0
    while index < len(active):
        _check_cancel(cancelled)
        if active[index]:
            index += 1
            continue
        gap_start = index
        while index < len(active) and not active[index]:
            if index % 250 == 0:
                _check_cancel(cancelled)
            index += 1
        if gap_start > 0 and index < len(active) and index - gap_start <= max_gap:
            active[gap_start:index] = [True] * (index - gap_start)

    regions: list[ActivityRegion] = []
    index = 0
    while index < len(active):
        _check_cancel(cancelled)
        if not active[index]:
            index += 1
            continue
        start_index = index
        while index < len(active) and active[index]:
            if index % 250 == 0:
                _check_cancel(cancelled)
            index += 1
        end_index = index
        if not raw and (end_index - start_index) * 0.02 < 0.16:
            continue
        start = max(0.0, start_index * 0.02 - (0 if raw else 0.10))
        end = min(duration, end_index * 0.02 + (0 if raw else 0.14))
        if end <= start:
            continue
        if not raw and regions and start - regions[-1].end <= 0.08:
            regions[-1] = ActivityRegion(regions[-1].start, round(end, 3))
        elif raw:
            regions.append(ActivityRegion(start, end))
        else:
            regions.append(ActivityRegion(round(start, 3), round(end, 3)))
    progress(
        f"Found {len(regions)} raw activity region(s) for YouTube refinement."
        if raw else f"Found {len(regions)} deterministic activity region(s).",
        1.0,
    )
    return regions, round(threshold, 2)


class WhisperManager:
    def __init__(self, data_root: Path, manifest_path: Path | None = None) -> None:
        self.data_root = data_root.resolve()
        self.manifest_path = (manifest_path or default_manifest_path()).resolve()
        value: Any = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("runtime"), dict):
            raise AnalysisError("Whisper component manifest is invalid")
        if not isinstance(value.get("models"), dict):
            raise AnalysisError("Whisper model manifest is invalid")
        self.manifest: dict[str, Any] = value

    @property
    def runtime(self) -> dict[str, Any]:
        return self.manifest["runtime"]

    @property
    def models(self) -> dict[str, dict[str, Any]]:
        return self.manifest["models"]

    @property
    def runtime_dir(self) -> Path:
        return self.data_root / f"runtime-{self.runtime['build']}"

    @property
    def cli_path(self) -> Path:
        return self.runtime_dir / "whisper-cli.exe"

    def model_path(self, model_key: str) -> Path:
        try:
            filename = str(self.models[model_key]["filename"])
        except KeyError as error:
            raise AnalysisError(f"Unknown Whisper model: {model_key}") from error
        return self.data_root / "models" / filename

    def model_download_bytes(self, model_key: str) -> int:
        try:
            return int(self.models[model_key]["bytes"])
        except (KeyError, TypeError, ValueError) as error:
            raise AnalysisError(f"Invalid Whisper model metadata: {model_key}") from error

    def _download(
        self,
        url: str,
        destination: Path,
        expected_hash: str,
        expected_bytes: int,
        label: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> Path:
        return download_verified(
            url, destination, expected_hash, expected_bytes, label, progress, cancelled,
        )

    def ensure_runtime(
        self, progress: ProgressCallback, cancelled: CancelCallback
    ) -> Path:
        diagnostic_event(
            "runtime_setup", build=self.runtime["build"], version=self.runtime["version"],
            directory=str(self.runtime_dir),
        )
        if sys.platform != "win32" or not sys.maxsize > 2**32:
            raise AnalysisError("Local Whisper setup currently requires 64-bit Windows")
        runtime_files = self.runtime.get("runtime_files")
        if not isinstance(runtime_files, dict) or not runtime_files:
            raise AnalysisError("Whisper runtime inventory is invalid")
        expected_files = [str(item) for item in runtime_files]
        if any(Path(filename).name != filename for filename in expected_files):
            raise AnalysisError("Whisper runtime filenames must be safe basenames")
        allowed_inventory = {
            *expected_files,
            ".verified.json",
            "WhisperCpp-MIT.txt",
            "OpenAI-Whisper-MIT.txt",
            self.manifest_path.name,
        }
        marker = self.runtime_dir / ".verified.json"
        if marker.is_file():
            try:
                state = json.loads(marker.read_text(encoding="utf-8"))
                if state["archive_sha256"] != self.runtime["archive_sha256"]:
                    raise ValueError("archive changed")
                for filename in expected_files:
                    path = self.runtime_dir / filename
                    metadata = runtime_files[filename]
                    if (
                        not path.is_file()
                        or path.stat().st_size != int(metadata["bytes"])
                        or sha256(path) != str(metadata["sha256"])
                    ):
                        raise ValueError(f"runtime file changed: {filename}")
                actual_inventory = {
                    path.name for path in self.runtime_dir.iterdir() if path.is_file()
                }
                if actual_inventory != allowed_inventory:
                    raise ValueError("runtime directory inventory changed")
                for license_name in ("WhisperCpp-MIT.txt", "OpenAI-Whisper-MIT.txt"):
                    shutil.copy2(
                        self.manifest_path.parent / license_name,
                        self.runtime_dir / license_name,
                    )
                shutil.copy2(self.manifest_path, self.runtime_dir / self.manifest_path.name)
                diagnostic_event("runtime_cache_verified", files=expected_files)
                progress("Using verified local Whisper runtime.", 1.0)
                return self.cli_path
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                diagnostic_exception("runtime_cache_invalid", error)
                shutil.rmtree(self.runtime_dir, ignore_errors=True)

        archive = self._download(
            str(self.runtime["archive_url"]),
            self.data_root / "downloads" / str(self.runtime["archive_name"]),
            str(self.runtime["archive_sha256"]),
            int(self.runtime["archive_bytes"]),
            "Whisper CPU runtime",
            progress,
            cancelled,
        )
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)
        temporary = self.runtime_dir.with_name(self.runtime_dir.name + ".partial")
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            progress("Installing and verifying the Whisper CPU runtime...", None)
            temporary.mkdir(parents=True)
            root = str(self.runtime["archive_root"]).strip("/")
            with zipfile.ZipFile(archive) as package:
                infos = {item.filename: item for item in package.infolist()}
                for filename in expected_files:
                    _check_cancel(cancelled)
                    member = f"{root}/{filename}"
                    if member not in infos:
                        raise AnalysisError(f"Whisper archive is missing {member}")
                    metadata = runtime_files[filename]
                    if infos[member].file_size != int(metadata["bytes"]):
                        raise AnalysisError(f"Whisper archive member has wrong size: {member}")
                    with package.open(member) as source, (
                        temporary / filename
                    ).open("wb") as output:
                        shutil.copyfileobj(source, output, BUFFER_SIZE)
            for license_name in ("WhisperCpp-MIT.txt", "OpenAI-Whisper-MIT.txt"):
                shutil.copy2(self.manifest_path.parent / license_name, temporary / license_name)
            shutil.copy2(self.manifest_path, temporary / self.manifest_path.name)
            for filename in expected_files:
                metadata = runtime_files[filename]
                path = temporary / filename
                if sha256(path) != str(metadata["sha256"]):
                    raise AnalysisError(f"Whisper runtime member failed verification: {filename}")
            (temporary / ".verified.json").write_text(
                json.dumps(
                    {
                        "archive_sha256": self.runtime["archive_sha256"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            result = _run_cancellable(
                [str(temporary / "whisper-cli.exe"), "--version"],
                "Starting the Whisper CPU runtime",
                cancelled,
                timeout=30,
            )
            diagnostic_event(
                "runtime_version_reported", output=diagnostic_text(result.stdout, limit=2048),
            )
            if str(self.runtime["version"]) not in result.stdout + result.stderr:
                raise AnalysisError("The Whisper runtime version does not match its manifest")
            os.replace(temporary, self.runtime_dir)
            diagnostic_event(
                "runtime_installed", files=expected_files, cli=self.cli_path,
                version=self.runtime["version"],
            )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        progress("Whisper CPU runtime setup complete.", 1.0)
        return self.cli_path

    def ensure_model(
        self,
        model_key: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> Path:
        try:
            model = self.models[model_key]
        except KeyError as error:
            raise AnalysisError(f"Unknown Whisper model: {model_key}") from error
        return self._download(
            str(model["url"]),
            self.model_path(model_key),
            str(model["sha256"]),
            int(model["bytes"]),
            str(model["name"]),
            progress,
            cancelled,
        )

    def transcribe(
        self,
        wav_path: Path,
        output_directory: Path,
        model_key: str,
        language: str,
        hardware: HardwareProfile,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> tuple[list[AnalysisSuggestion], str | None]:
        cli = self.ensure_runtime(progress, cancelled)
        model = self.ensure_model(model_key, progress, cancelled)
        diagnostic_event("whisper_components_ready", runtime=str(cli), model=str(model))
        with wave.open(str(wav_path), "rb") as audio_source:
            audio_duration = audio_source.getnframes() / max(1, audio_source.getframerate())
        output_directory.mkdir(parents=True, exist_ok=True)
        output_base = output_directory / "whisper-transcript"
        model_name = self.models[model_key]["name"]
        progress(f"Loading {model_name} on CPU; transcription starts after model loading...", None)
        command = [
            str(cli),
            "--model",
            str(model),
            "--file",
            str(wav_path),
            "--language",
            language,
            "--threads",
            str(hardware.cpu_threads),
            "--output-json-full",
            "--output-file",
            str(output_base),
            "--no-gpu",
            "--print-progress",
            "--split-on-word",
            "--max-len",
            "120",
        ]
        diagnostic_event(
            "whisper_transcription_starting", model=model_key, model_bytes=model.stat().st_size,
            language=language, threads=hardware.cpu_threads, audio_duration_seconds=audio_duration,
        )
        percent: int | None = None
        processing_audio = False
        last_report: tuple[int, int | None, bool] | None = None

        def on_output(line: str) -> None:
            nonlocal percent, processing_audio
            was_processing = processing_audio
            match = re.search(r"whisper_print_progress_callback:\s*progress\s*=\s*(\d+)%", line)
            if match:
                percent = max(percent or 0, min(99, int(match.group(1))))
                processing_audio = True
            elif "main: processing " in line:
                processing_audio = True
            if processing_audio and not was_processing:
                diagnostic_event("whisper_audio_processing_started")

        def report_status(elapsed: float) -> None:
            nonlocal last_report
            state = (int(elapsed), percent, processing_audio)
            if state == last_report:
                return
            last_report = state
            elapsed_text = f"{int(elapsed) // 60}:{int(elapsed) % 60:02d} elapsed"
            if percent is not None:
                message = f"Whisper transcription: {percent}% of audio processed ({elapsed_text})."
            elif processing_audio:
                message = f"Whisper is processing the first audio block ({elapsed_text}); you can cancel."
            else:
                message = f"Loading {model_name} on CPU ({elapsed_text}); you can cancel."
            progress(message, percent / 100 if percent is not None else None)

        _run_cancellable(
            command, "Local Whisper transcription", cancelled,
            output_line=on_output, tick=report_status,
            timeout=max(600, audio_duration * 30),
        )
        progress("Reading Whisper transcript and timestamps...", 0.99)
        output_path = output_base.with_suffix(".json")
        if not output_path.is_file():
            raise AnalysisError("Whisper did not produce its expected JSON transcript")
        diagnostic_event("whisper_output_ready", bytes=output_path.stat().st_size)
        value: Any = json.loads(output_path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict) or not isinstance(value.get("transcription"), list):
            raise AnalysisError("Whisper produced an unsupported JSON transcript")
        result = value.get("result", {})
        detected_language = str(result.get("language", "")).strip() or None
        suggestions: list[AnalysisSuggestion] = []
        for item in value["transcription"]:
            if not isinstance(item, dict):
                continue
            offsets = item.get("offsets", {})
            if not isinstance(offsets, dict):
                continue
            try:
                start = float(offsets["from"]) / 1000.0
                end = float(offsets["to"]) / 1000.0
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(start) or not math.isfinite(end):
                raise AnalysisError("Whisper produced a non-finite transcript timestamp")
            start = max(0.0, min(audio_duration, start))
            end = max(0.0, min(audio_duration, end))
            caption = " ".join(str(item.get("text", "")).split())
            if not caption or end - start < 0.05:
                continue
            # Token offsets are estimates, not forced alignment. Even complete, monotonic
            # lexical times can omit a final word; keep the segment envelope for review.
            probabilities = [
                float(token["p"])
                for token in item.get("tokens", [])
                if isinstance(token, dict)
                and isinstance(token.get("p"), (int, float))
                and not str(token.get("text", "")).startswith("[")
            ]
            confidence = sum(probabilities) / len(probabilities) if probabilities else None
            suggestions.append(
                AnalysisSuggestion(
                    start=start,
                    end=end,
                    caption=caption,
                    source="Whisper",
                    confidence=round(confidence, 3) if confidence is not None else None,
                )
            )
        padded = pad_source_ranges(
            [(item.start, item.end) for item in suggestions], audio_duration,
            check_cancel=lambda: _check_cancel(cancelled),
        )
        suggestions = [
            replace(item, start=round(start, 3), end=min(audio_duration, round(end, 3)))
            for item, (start, end) in zip(suggestions, padded, strict=True)
        ]
        progress(f"Whisper produced {len(suggestions)} transcript region(s).", 1.0)
        diagnostic_event(
            "whisper_transcript_parsed", regions=len(suggestions), detected_language=detected_language,
        )
        return suggestions, detected_language


def combine_suggestions(
    activity: list[ActivityRegion],
    transcripts: list[AnalysisSuggestion],
) -> list[AnalysisSuggestion]:
    if not transcripts:
        return [
            AnalysisSuggestion(region.start, region.end, "", "Audio activity")
            for region in activity
        ]
    suggestions = list(transcripts)
    for region in activity:
        covered_intervals = sorted(
            (
                max(region.start, item.start),
                min(region.end, item.end),
            )
            for item in transcripts
            if min(region.end, item.end) > max(region.start, item.start)
        )
        merged: list[tuple[float, float]] = []
        for start, end in covered_intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        cursor = region.start
        for start, end in [*merged, (region.end, region.end)]:
            if start - cursor >= 0.16:
                suggestions.append(
                    AnalysisSuggestion(
                        round(cursor, 3),
                        round(start, 3),
                        "",
                        "Untranscribed activity",
                    )
                )
            cursor = max(cursor, end)
    return sorted(suggestions, key=lambda item: (item.start, item.end, item.source))


def analyze_video(
    media: MediaTools,
    video: Path,
    duration: float,
    data_root: Path,
    *,
    sensitivity: str,
    use_whisper: bool,
    model_key: str,
    language: str,
    progress: ProgressCallback,
    cancelled: CancelCallback,
    manifest_path: Path | None = None,
    source_captions: list[SourceCaption] | None = None,
    pause_threshold: float = 0.4,
) -> AnalysisResult:
    _check_cancel(cancelled)
    if not math.isfinite(duration) or duration <= 0:
        raise AnalysisError("Video analysis requires a finite, positive duration")
    if source_captions is not None and (
        isinstance(pause_threshold, bool) or not math.isfinite(pause_threshold)
        or not 0.2 <= pause_threshold <= 1.0
    ):
        raise ValueError("Caption pause threshold must be between 0.2 and 1.0 seconds")
    hardware = detect_hardware()
    diagnostic_event(
        "analysis_configuration", source_video=str(video), duration_seconds=duration,
        sensitivity=sensitivity, use_whisper=use_whisper, model=model_key, language=language,
        cpu_threads=hardware.cpu_threads, memory_bytes=hardware.memory_bytes,
        available_memory_bytes=hardware.available_memory_bytes,
        refine_youtube=source_captions is not None, pause_threshold=pause_threshold,
    )
    estimated_audio_bytes = max(1, math.ceil(duration * 16_000 * 2))
    temporary_root = Path(tempfile.gettempdir()).resolve()
    temporary_free = shutil.disk_usage(temporary_root).free
    required_temporary_disk = estimated_audio_bytes * 2 + (64 * 1024**2)
    diagnostic_event(
        "analysis_temporary_disk", directory=temporary_root, available_bytes=temporary_free,
        required_bytes=required_temporary_disk,
    )
    if temporary_free < required_temporary_disk:
        raise AnalysisError(
            f"Video analysis needs approximately {required_temporary_disk / 1024**2:.0f} MiB "
            f"free in {temporary_root}, but only {temporary_free / 1024**2:.0f} MiB is available."
        )
    if use_whisper:
        manager = WhisperManager(data_root, manifest_path)
        persistent_required = 32 * 1024**2
        if not manager.cli_path.is_file():
            persistent_required += int(manager.runtime["archive_bytes"]) + sum(
                int(item["bytes"]) for item in manager.runtime["runtime_files"].values()
            )
        if not manager.model_path(model_key).is_file():
            persistent_required += manager.model_download_bytes(model_key)
        persistent_root = data_root if data_root.exists() else data_root.parent
        while not persistent_root.exists() and persistent_root != persistent_root.parent:
            persistent_root = persistent_root.parent
        persistent_free = shutil.disk_usage(persistent_root).free
        diagnostic_event(
            "analysis_component_disk", directory=persistent_root,
            available_bytes=persistent_free, required_bytes=persistent_required,
        )
        if persistent_free < persistent_required:
            raise AnalysisError(
                f"Local transcription setup needs approximately {persistent_required / 1024**2:.0f} "
                f"MiB free near {data_root}, but only {persistent_free / 1024**2:.0f} MiB is available."
            )
    if use_whisper and hardware.available_memory_bytes is not None:
        model_memory = 1_100 * 1024**2 if model_key == "base" else 600 * 1024**2
        audio_memory = estimated_audio_bytes * 2
        if hardware.available_memory_bytes < model_memory + audio_memory:
            raise AnalysisError(
                "This video/model combination exceeds the conservative local-memory budget. "
                "Choose the Tiny model or analyze a shorter source."
            )
    with tempfile.TemporaryDirectory(
        prefix="cvpc-analysis-", dir=temporary_root
    ) as temporary_text:
        temporary = Path(temporary_text)
        wav_path = temporary / "analysis.wav"
        extract_analysis_audio(media, video, wav_path, progress, cancelled)
        refined: list[SourceCaption] | None = None
        if source_captions is not None:
            activity, threshold = scan_audio_activity(
                wav_path, duration, sensitivity, progress, cancelled, raw=True
            )
            progress("Refining YouTube fragments using measured audio pauses…", None)
            refined = refine_captions(
                source_captions, [(region.start, region.end) for region in activity], duration,
                pause_threshold=pause_threshold, check_cancel=lambda: _check_cancel(cancelled),
            )
            progress(f"Prepared {len(refined)} Refined YouTube caption row(s).", 1.0)
            # If both outputs were requested, retain the ordinary scan for Whisper suggestions.
            if use_whisper:
                activity, threshold = scan_audio_activity(
                    wav_path, duration, sensitivity, progress, cancelled
                )
        else:
            activity, threshold = scan_audio_activity(
                wav_path, duration, sensitivity, progress, cancelled
            )
        transcripts: list[AnalysisSuggestion] = []
        detected_language: str | None = None
        model_name: str | None = None
        if use_whisper:
            manager = WhisperManager(data_root, manifest_path)
            transcripts, detected_language = manager.transcribe(
                wav_path,
                temporary,
                model_key,
                language,
                hardware,
                progress,
                cancelled,
            )
            model_name = str(manager.models[model_key]["name"])
        suggestions = (
            [] if source_captions is not None and not use_whisper
            else combine_suggestions(activity, transcripts)
        )
        diagnostic_event(
            "analysis_results", activity_regions=len(activity), transcript_regions=len(transcripts),
            suggestions=len(suggestions), detected_language=detected_language,
            refined_captions=len(refined) if refined is not None else None,
        )
        return AnalysisResult(
            suggestions=suggestions,
            activity_regions=len(activity),
            transcript_regions=len(transcripts),
            activity_threshold_db=threshold,
            model_name=model_name,
            detected_language=detected_language,
            hardware=hardware,
            refined_captions=refined,
        )
