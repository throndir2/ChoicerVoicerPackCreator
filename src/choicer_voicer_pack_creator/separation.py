from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.analysis import (
    AnalysisCancelled,
    AnalysisError,
    _run_cancellable,
    download_verified,
)
from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.media import MediaTools

ProgressCallback = Callable[[str, float | None], None]
CancelCallback = Callable[[], bool]
SAMPLE_RATE = 44100
CHUNK_FRAMES = 343980
OVERLAP_FRAMES = CHUNK_FRAMES // 4
BLOCK_FRAMES = 65536
PEAK_LIMIT = 0.98


class SeparationError(RuntimeError):
    pass


class SeparationCancelled(SeparationError):
    pass


class SeparationDownloadRequired(SeparationError):
    pass


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "backing-separation.json"


def check_cancel(cancelled: CancelCallback) -> None:
    if cancelled():
        raise SeparationCancelled("Backing-track generation was canceled")


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    partial = path.with_suffix(".partial")
    try:
        with partial.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(partial, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                # A Windows reader may briefly hold the previous status without delete sharing.
                time.sleep(0.025)
    finally:
        partial.unlink(missing_ok=True)


def verify_model_file(
    path: Path, expected_bytes: int, expected_hash: str, cancelled: CancelCallback,
) -> bool:
    check_cancel(cancelled)
    if not path.is_file() or path.stat().st_size != expected_bytes:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            check_cancel(cancelled)
            digest.update(block)
    return digest.hexdigest() == expected_hash


def validate_audio(path: Path, frames: int, cancelled: CancelCallback) -> None:
    import numpy as np
    import soundfile as sf

    check_cancel(cancelled)
    try:
        with sf.SoundFile(path) as source:
            if (
                source.frames != frames or frames <= 0 or source.channels != 2
                or source.samplerate != SAMPLE_RATE or source.format not in {"WAV", "RF64"}
            ):
                raise SeparationError("Generated backing track has an incorrect format or duration")
            count = 0
            for block in source.blocks(blocksize=BLOCK_FRAMES, dtype="float32", always_2d=True):
                check_cancel(cancelled)
                if not np.isfinite(block).all() or np.max(np.abs(block)) > PEAK_LIMIT + 1e-6:
                    raise SeparationError("Generated backing track contains invalid or clipped audio")
                count += len(block)
            if count != frames:
                raise SeparationError("Generated backing track is incomplete")
    except (OSError, sf.LibsndfileError) as error:
        raise SeparationError(f"Could not verify the generated backing track: {error}") from error


class SeparationManager:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.manifest_path = default_manifest_path()
        try:
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            model = self.manifest["model"]
            if (
                model["filename"] != "htdemucs.onnx"
                or len(model["sha256"]) != 64
                or int(model["bytes"]) <= 0
                or self.manifest["sample_rate"] != SAMPLE_RATE
                or self.manifest["input_shape"] != [1, 2, CHUNK_FRAMES]
                or self.manifest["stems"] != ["drums", "bass", "other", "vocals"]
            ):
                raise ValueError("unsupported model configuration")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SeparationError(f"Backing-separation model manifest is invalid: {error}") from error

    @property
    def model_download_bytes(self) -> int:
        return int(self.manifest["model"]["bytes"])

    @property
    def model_path(self) -> Path:
        model = self.manifest["model"]
        return self.data_root / "separation-models" / model["sha256"] / model["filename"]

    def _verified_model(self, progress: ProgressCallback, cancelled: CancelCallback) -> bool:
        check_cancel(cancelled)
        if not self.model_path.is_file():
            return False
        progress("Verifying the cached local separation model…", None)
        return verify_model_file(
            self.model_path, self.model_download_bytes, self.manifest["model"]["sha256"], cancelled,
        )

    def _ensure_model(
        self, job: Path, allow_download: bool, progress: ProgressCallback, cancelled: CancelCallback,
    ) -> Path:
        if not self._verified_model(progress, cancelled):
            if not allow_download:
                raise SeparationDownloadRequired(
                    "The local separation model is missing or invalid. Downloading or repairing "
                    f"it requires permission ({self.model_download_bytes / 1024**2:.0f} MiB)."
                )
            check_cancel(cancelled)
            model = self.manifest["model"]
            # Download to this job, not a shared .partial file or an invalid existing cache.
            downloaded = download_verified(
                model["url"], job / "htdemucs.onnx", model["sha256"], self.model_download_bytes,
                "HTDemucs backing-separation model", progress, cancelled,
            )
            check_cancel(cancelled)
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(downloaded, self.model_path)
            except PermissionError:
                # Another application may have published and opened this exact model meanwhile.
                if not self._verified_model(progress, cancelled):
                    raise
                downloaded.unlink(missing_ok=True)
        for filename in ("Demucs-MIT.txt", "StemSplit-MIT.txt", self.manifest_path.name):
            payload = (self.manifest_path.parent / filename).read_bytes()
            destination = self.model_path.parent / filename
            if destination.is_file() and destination.read_bytes() == payload:
                continue
            staged = job / filename
            with staged.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(staged, destination)
            except PermissionError:
                if not destination.is_file() or destination.read_bytes() != payload:
                    raise
                staged.unlink(missing_ok=True)
        return self.model_path

    def _decode(
        self, media: MediaTools, video: Path, destination: Path,
        progress: ProgressCallback, cancelled: CancelCallback,
    ) -> int:
        progress("Inspecting the source video timeline…", None)
        completed = _run_cancellable(
            [media.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json",
             str(video)],
            "Inspecting backing-track source", cancelled,
        )
        try:
            value = json.loads(completed.stdout)
            streams = value["streams"]
            video_stream = next(item for item in streams if item.get("codec_type") == "video")
            if not any(item.get("codec_type") == "audio" for item in streams):
                raise SeparationError("The source video has no audio to separate")
            # Match MediaTools.probe / the editor's source timeline, not the audio's length.
            duration = float(value.get("format", {}).get("duration")
                             or video_stream.get("duration") or 0)
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("the video has no finite positive duration")
            frames = round(duration * SAMPLE_RATE)
            if frames <= 0:
                raise ValueError("the video is shorter than one audio frame")
        except (KeyError, TypeError, ValueError, StopIteration) as error:
            raise SeparationError(f"Could not determine the source video timeline: {error}") from error
        progress("Decoding and aligning stereo audio to the video timeline…", None)
        _run_cancellable(
            [media.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-copyts", "-start_at_zero", "-i", str(video), "-map", "0:a:0", "-vn",
             "-af", f"aresample={SAMPLE_RATE}:async=1:first_pts=0,"
                    f"apad=whole_len={frames},atrim=end_sample={frames}",
             "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_f32le",
             "-rf64", "auto", str(destination)],
            "Decoding backing-track source", cancelled,
        )
        import soundfile as sf

        try:
            with sf.SoundFile(destination) as source:
                if (source.frames, source.samplerate, source.channels) != (frames, SAMPLE_RATE, 2):
                    raise SeparationError("Source audio decoding did not preserve the video timeline")
        except (OSError, sf.LibsndfileError) as error:
            raise SeparationError(f"Could not read decoded source audio: {error}") from error
        return frames

    def generate(
        self, media: MediaTools, video: Path, *, allow_download: bool = False,
        progress: ProgressCallback, cancelled: CancelCallback,
    ) -> Path:
        check_cancel(cancelled)
        job_id = uuid.uuid4().hex
        job = self.data_root / "separation-jobs" / job_id
        try:
            job.mkdir(parents=True)
            model = self._ensure_model(job, allow_download, progress, cancelled)
            decoded = job / "decoded.wav"
            frames = self._decode(media, video.resolve(), decoded, progress, cancelled)
            output = job / "backing.wav"
            status_path = job / "status.json"
            request_path = job / "request.json"
            write_json_atomic(request_path, {
                "version": 1, "job_id": job_id, "model": str(model),
                "frames": frames,
            })
            last_status: dict[str, Any] | None = None

            def poll_status(_elapsed: float) -> None:
                nonlocal last_status
                if not status_path.is_file():
                    return
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if not isinstance(status, dict) or status.get("job_id") != job_id:
                    raise SeparationError("The separation worker returned an unrelated job status")
                if status != last_status:
                    fraction = status.get("progress")
                    if fraction is not None and (
                        not isinstance(fraction, (int, float))
                        or not math.isfinite(fraction) or not 0 <= fraction <= 1
                    ):
                        raise SeparationError("The separation worker reported invalid progress")
                    progress(str(status.get("message", "Separating locally…")), fraction)
                    last_status = status

            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.extend(["-m", "choicer_voicer_pack_creator"])
            command.extend(["--separate-audio", str(request_path)])
            progress("Starting local CPU separation (no audio is uploaded)…", None)
            try:
                _run_cancellable(
                    command, "Local backing-track separation", cancelled, tick=poll_status,
                )
            except AnalysisCancelled:
                raise
            except AnalysisError as error:
                poll_status(0)
                detail = (last_status or {}).get("message", str(error))
                raise SeparationError(f"Local backing-track separation failed: {detail}") from error
            poll_status(0)
            check_cancel(cancelled)
            if not last_status or last_status.get("state") != "succeeded":
                raise SeparationError("The separation worker exited without a successful result")
            progress("Verifying the full-length backing track…", None)
            validate_audio(output, frames, cancelled)
            check_cancel(cancelled)
            destination = self.data_root / "backing-tracks" / f"backing-{job_id}.wav"
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(output, destination)
            diagnostic_event("backing_separation_completed", destination=destination, frames=frames)
            return destination
        except AnalysisCancelled as error:
            raise SeparationCancelled("Backing-track generation was canceled") from error
        except AnalysisError as error:
            raise SeparationError(str(error)) from error
        except (OSError, ValueError) as error:
            raise SeparationError(f"Backing-track generation failed: {error}") from error
        finally:
            if job.exists():
                try:
                    shutil.rmtree(job)
                except OSError as error:
                    diagnostic_exception("backing_separation_cleanup_failed", error, directory=job)
