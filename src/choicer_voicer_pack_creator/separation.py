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
from contextlib import ExitStack
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
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceSnapshot,
    cancellation_deferred,
    check_cancelled,
    critical_stage,
    operation_scope,
    path_leases,
)
from choicer_voicer_pack_creator.separation_types import (
    KEEP_SINGING,
    REMOVE_ALL_VOCALS,
    BackingMode,
    validate_backing_mode,
)

ProgressCallback = Callable[[str, float | None], None]
CancelCallback = Callable[[], bool]
SAMPLE_RATE = 44100
CHUNK_FRAMES = 343980
OVERLAP_FRAMES = CHUNK_FRAMES // 4
BLOCK_FRAMES = 65536
PEAK_LIMIT = 0.98


class SeparationError(RuntimeError):
    pass


class SeparationCancelled(SeparationError, OperationCancelled):
    pass


class SeparationDownloadRequired(SeparationError):
    pass


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "backing-separation.json"


def check_cancel(cancelled: CancelCallback) -> None:
    try:
        check_cancelled()
    except OperationCancelled as error:
        raise SeparationCancelled("Backing-track generation was canceled") from error
    if not cancellation_deferred() and cancelled():
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
    *, expected_md5: str | None = None,
) -> bool:
    check_cancel(cancelled)
    if not path.is_file() or path.stat().st_size != expected_bytes:
        return False
    digest = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False) if expected_md5 is not None else None
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            check_cancel(cancelled)
            digest.update(block)
            if md5 is not None:
                md5.update(block)
    check_cancel(cancelled)
    return digest.hexdigest() == expected_hash and (
        md5 is None or md5.hexdigest() == expected_md5
    )


def validate_audio(
    path: Path, frames: int, cancelled: CancelCallback, *, sample_rate: int = SAMPLE_RATE,
) -> None:
    import numpy as np
    import soundfile as sf

    check_cancel(cancelled)
    if type(sample_rate) is not int or sample_rate not in (SAMPLE_RATE, 48000):
        raise SeparationError("Unsupported backing-track sample rate")
    try:
        with sf.SoundFile(path) as source:
            if (
                source.frames != frames or frames <= 0 or source.channels != 2
                or source.samplerate != sample_rate or source.format not in {"WAV", "RF64"}
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
    def __init__(self, data_root: Path, *, mode: BackingMode = REMOVE_ALL_VOCALS) -> None:
        self._mode = validate_backing_mode(mode)
        self.data_root = data_root.resolve()
        self.manifest_path = default_manifest_path()
        try:
            if self.mode == KEEP_SINGING:
                from choicer_voicer_pack_creator._bandit import load_manifest, manifest_path

                self.manifest_path = manifest_path()
                self.manifest = load_manifest()
                return
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
    def mode(self) -> BackingMode:
        return self._mode

    @property
    def sample_rate(self) -> int:
        return 48000 if self.mode == KEEP_SINGING else SAMPLE_RATE

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
            **({"expected_md5": self.manifest["model"]["md5"]} if self.mode == KEEP_SINGING else {}),
        )

    def _ensure_model(
        self, job: Path, allow_download: bool, progress: ProgressCallback, cancelled: CancelCallback,
    ) -> Path:
        try:
            with operation_scope(cancelled, progress), path_leases(
                write_paths=(self.model_path.parent,),
            ):
                return self._install_model(job, allow_download, progress, cancelled)
        except OperationCancelled as error:
            raise SeparationCancelled("Backing-track generation was canceled") from error

    def _install_model(
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
            if self.mode == KEEP_SINGING:
                self._check_disk(job, self.model_download_bytes + 64 * 1024**2)
            # Download to this job, not a shared .partial file or an invalid existing cache.
            downloaded = download_verified(
                model["url"], job / model["filename"], model["sha256"], self.model_download_bytes,
                ("BandIt combined (CC BY-NC 4.0; non-commercial)" if self.mode == KEEP_SINGING
                 else "HTDemucs backing-separation model"), progress, cancelled,
            )
            if self.mode == KEEP_SINGING and not verify_model_file(
                downloaded, self.model_download_bytes, model["sha256"], cancelled,
                expected_md5=model["md5"],
            ):
                raise SeparationError("BandIt checkpoint failed its published MD5 / pinned SHA-256")
            check_cancel(cancelled)
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                check_cancel(cancelled)
                os.replace(downloaded, self.model_path)
            except PermissionError:
                # Another application may have published and opened this exact model meanwhile.
                if not self._verified_model(progress, cancelled):
                    raise
                downloaded.unlink(missing_ok=True)
        notices = (
            (*self.manifest["notice_files"], self.manifest_path.name)
            if self.mode == KEEP_SINGING
            else ("Demucs-MIT.txt", "StemSplit-MIT.txt", self.manifest_path.name)
        )
        notice_sources = {
            filename: self.manifest_path.parent / filename for filename in notices
        }
        if self.mode == KEEP_SINGING:
            notice_sources[self.manifest["source_provenance_file"]] = (
                self.manifest_path.parent.parent / "_bandit" / "provenance.json"
            )
        for filename, source in notice_sources.items():
            check_cancel(cancelled)
            payload = source.read_bytes()
            destination = self.model_path.parent / filename
            if destination.is_file() and destination.read_bytes() == payload:
                continue
            staged = job / filename
            with staged.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                check_cancel(cancelled)
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
        if self.mode == KEEP_SINGING:
            return self._decode_bandit(media, video, destination, progress, cancelled)
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

    @staticmethod
    def _check_disk(directory: Path, required: int) -> None:
        available = shutil.disk_usage(directory).free
        if available < required:
            raise SeparationError(
                f"Backing generation needs approximately {required / 1024**3:.2f} GiB of free "
                f"staging space; only {available / 1024**3:.2f} GiB is available. "
                "Free disk space and retry; the source and existing backing are unchanged."
            )

    def _decode_bandit(
        self, media: MediaTools, video: Path, destination: Path,
        progress: ProgressCallback, cancelled: CancelCallback,
    ) -> int:
        from choicer_voicer_pack_creator.export_resources import current_ffmpeg_threads

        progress("Inspecting the source video timeline and native audio rate…", None)
        completed = _run_cancellable(
            [media.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
            "Inspecting backing-track source", cancelled,
        )
        try:
            value = json.loads(completed.stdout)
            video_stream = next(s for s in value["streams"] if s.get("codec_type") == "video")
            audio_stream = next(s for s in value["streams"] if s.get("codec_type") == "audio")
            duration = float(value.get("format", {}).get("duration")
                             or video_stream.get("duration") or 0)
            native_rate = int(audio_stream["sample_rate"])
            if not math.isfinite(duration) or duration <= 0 or not 1 <= native_rate <= 768000:
                raise ValueError("invalid video duration or native audio sample rate")
            frames = round(duration * self.sample_rate)
            native_frames = round(duration * native_rate)
            if min(frames, native_frames) <= 0:
                raise ValueError("the video is shorter than one audio frame")
        except (KeyError, TypeError, ValueError, StopIteration) as error:
            raise SeparationError(f"Could not determine the source video timeline: {error}") from error
        native = (
            destination if native_rate == self.sample_rate
            else destination.with_name("decoded-native.wav")
        )
        self._check_disk(
            destination.parent,
            frames * (8 + 8 + 6) + (native_frames * 8 if native != destination else 0)
            + 64 * 1024**2,
        )
        threads = str(current_ffmpeg_threads() or 1)
        progress("Decoding native-rate stereo audio aligned to the video timeline…", None)
        try:
            _run_cancellable(
                [media.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-threads", threads, "-filter_threads", threads,
                 "-copyts", "-start_at_zero", "-i", str(video), "-map", "0:a:0", "-vn",
                 "-af", f"aresample={native_rate}:async=1:first_pts=0,"
                        f"apad=whole_len={native_frames},atrim=end_sample={native_frames}",
                 "-ar", str(native_rate), "-ac", "2", "-c:a", "pcm_f32le",
                 "-threads", threads, "-rf64", "auto", str(native)],
                "Decoding backing-track source", cancelled,
            )
            import soundfile as sf

            with sf.SoundFile(native) as source:
                if (source.frames, source.samplerate, source.channels) != (native_frames, native_rate, 2):
                    raise SeparationError("Source audio decoding did not preserve the video timeline")
            if native != destination:
                from choicer_voicer_pack_creator.bandit_runtime import resample_stream

                progress("Resampling aligned audio to 48 kHz with bounded polyphase filtering…", None)
                resample_stream(native, destination, frames, cancelled)
        finally:
            if native != destination:
                native.unlink(missing_ok=True)
        return frames

    def generate(
        self, media: MediaTools, video: Path, *, allow_download: bool = False,
        progress: ProgressCallback, cancelled: CancelCallback,
    ) -> Path:
        try:
            video = video.resolve()
            with operation_scope(cancelled, progress), path_leases(read_paths=(video,)):
                source = SourceSnapshot.capture((video,))
                return self._generate(
                    media, video, allow_download=allow_download,
                    progress=progress, cancelled=cancelled, source_snapshot=source,
                )
        except OperationCancelled as error:
            raise SeparationCancelled("Backing-track generation was canceled") from error
        except OSError as error:
            raise SeparationError(f"Backing-track generation failed: {error}") from error

    def _generate(
        self, media: MediaTools, video: Path, *, allow_download: bool,
        progress: ProgressCallback, cancelled: CancelCallback, source_snapshot: SourceSnapshot,
    ) -> Path:
        check_cancel(cancelled)
        job_id = uuid.uuid4().hex
        job = self.data_root / "separation-jobs" / job_id
        runtime = ExitStack()
        try:
            job.mkdir(parents=True)
            model = self._ensure_model(job, allow_download, progress, cancelled)
            threads = None
            if self.mode == KEEP_SINGING:
                from choicer_voicer_pack_creator.bandit_runtime import WORK_ESTIMATE
                from choicer_voicer_pack_creator.export_resources import (
                    ResourceError,
                    export_resources,
                )

                try:
                    admission = runtime.enter_context(export_resources.acquire(
                        WORK_ESTIMATE, work_label="singing-preserving backing",
                    ))
                except ResourceError as error:
                    raise SeparationError(str(error)) from error
                threads = admission.ffmpeg_threads
            decoded = job / "decoded.wav"
            frames = self._decode(media, video.resolve(), decoded, progress, cancelled)
            output = job / "backing.wav"
            status_path = job / "status.json"
            request_path = job / "request.json"
            write_json_atomic(request_path, {
                "version": 1, "job_id": job_id, "model": str(model),
                "frames": frames, "mode": self.mode,
                **({"threads": threads} if threads is not None else {}),
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
                with path_leases(read_paths=(model,)):
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
            validate_audio(output, frames, cancelled, sample_rate=self.sample_rate)
            check_cancel(cancelled)
            destination = self.data_root / "backing-tracks" / f"backing-{job_id}.wav"
            with critical_stage("Publishing the verified backing track..."):
                source_snapshot.verify()
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
            try:
                if job.exists():
                    try:
                        shutil.rmtree(job)
                    except OSError as error:
                        diagnostic_exception("backing_separation_cleanup_failed", error, directory=job)
            finally:
                runtime.close()
