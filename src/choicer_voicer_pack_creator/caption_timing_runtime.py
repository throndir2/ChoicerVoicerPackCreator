"""Verified, opt-in local caption alignment; native inference lives in an owned process."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from choicer_voicer_pack_creator.caption_timing_types import (
    CaptionTimingEvidence,
    CaptionTimingResult,
)
from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.models import SourceCaption
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    check_cancelled,
    operation_scope,
    path_leases,
)
from choicer_voicer_pack_creator.process_worker import ProcessWorkerError, run_process_worker

ProgressCallback = Callable[[str, float | None], None]
CancelCallback = Callable[[], bool]
MODEL_FILES = frozenset(("config.json", "model.bin", "tokenizer.json", "vocabulary.json"))
MODEL_REPOSITORY = "dropbox-dash/faster-whisper-large-v3-turbo"
MODEL_NOTICES = ("caption-timing.json", "OpenAI-Whisper-MIT.txt")
SAMPLE_RATE = 16000
MAX_CONTENT_SECONDS = 24.0
CONTEXT_MARGIN_SECONDS = 2.0
MAX_MODEL_SECONDS = 30.0
MAX_TEXT_TOKENS = 384
MODEL_WORKING_BYTES = 3 * 1024**3


class CaptionTimingError(ValueError):
    pass


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "caption-timing.json"


def _check_cancel(cancelled: CancelCallback) -> None:
    check_cancelled()
    if cancelled():
        raise OperationCancelled("Caption timing was canceled")


def _verified_file(path: Path, spec: dict, cancelled: CancelCallback) -> bool:
    _check_cancel(cancelled)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != spec["bytes"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                _check_cancel(cancelled)
                digest.update(block)
        return digest.hexdigest() == spec["sha256"]
    except OSError:
        return False


class CaptionTimingManager:
    def __init__(self, data_root: Path, manifest_path: Path | None = None) -> None:
        self.data_root = Path(data_root).resolve()
        self.manifest_path = Path(manifest_path or default_manifest_path()).resolve()
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            revision = manifest["revision"]
            files = manifest["files"]
            if (
                manifest["version"] != 1
                or manifest["repository"] != MODEL_REPOSITORY
                or not isinstance(revision, str)
                or re.fullmatch(r"[0-9a-f]{40}", revision) is None
                or manifest["model"] != "large-v3-turbo"
                or manifest["preprocessing"] != {
                    "sample_rate": SAMPLE_RATE, "n_fft": 400, "hop_length": 160,
                    "mel_bins": 128, "model_seconds": 30,
                }
                or not isinstance(files, list)
                or len(files) != len(MODEL_FILES)
                or {item["filename"] for item in files} != MODEL_FILES
            ):
                raise ValueError("unsupported model configuration")
            for item in files:
                if (
                    type(item["bytes"]) is not int or not 0 < item["bytes"] < 2 * 1024**3
                    or not isinstance(item["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                    or item["url"] != (
                        f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/"
                        f"{revision}/{item['filename']}"
                    )
                ):
                    raise ValueError("model files must have immutable URLs, sizes and SHA-256")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CaptionTimingError(f"Caption timing model manifest is invalid: {error}") from error
        self.manifest = manifest

    @property
    def model_name(self) -> str:
        return "Whisper large-v3-turbo"

    @property
    def download_bytes(self) -> int:
        return sum(item["bytes"] for item in self.manifest["files"])

    @property
    def component_key(self) -> str:
        return f"caption-timing-large-v3-turbo-{self.manifest['revision']}"

    @property
    def model_path(self) -> Path:
        return self.data_root / "caption-timing-models" / self.manifest["revision"]

    def _safe_directory(self) -> bool:
        return not self.model_path.is_symlink() and not self.model_path.parent.is_symlink()

    @property
    def installed(self) -> bool:
        """Cheap availability hint only; every inference verifies actual file hashes."""
        try:
            return self._safe_directory() and all(
                not (path := self.model_path / item["filename"]).is_symlink()
                and path.is_file() and path.stat().st_size == item["bytes"]
                for item in self.manifest["files"]
            )
        except OSError:
            return False

    def verify(self, cancelled: CancelCallback) -> bool:
        return self._safe_directory() and all(
            _verified_file(self.model_path / item["filename"], item, cancelled)
            for item in self.manifest["files"]
        )

    def ensure_model(
        self, progress: ProgressCallback, cancelled: CancelCallback, *,
        allow_download: bool = False,
    ) -> Path:
        # Lazy import avoids loading the analysis stack just to show download consent.
        from choicer_voicer_pack_creator.analysis import AnalysisError, download_verified

        job = self.data_root / "caption-timing-downloads" / uuid.uuid4().hex
        with operation_scope(cancelled, progress), path_leases(write_paths=(self.model_path,)):
            _check_cancel(cancelled)
            if not self._safe_directory():
                raise CaptionTimingError("The caption timing model directory must not be a link")
            progress("Verifying the local caption timing model…", None)
            invalid = [
                item for item in self.manifest["files"]
                if not _verified_file(self.model_path / item["filename"], item, cancelled)
            ]
            if invalid and allow_download is not True:
                raise CaptionTimingError(
                    "The optional caption timing model is missing or damaged. Downloading or "
                    f"repairing it needs permission ({self.download_bytes / 1024**3:.2f} GiB)."
                )
            try:
                if invalid:
                    job.mkdir(parents=True)
                    for item in invalid:
                        _check_cancel(cancelled)
                        try:
                            downloaded = download_verified(
                                item["url"], job / item["filename"], item["sha256"], item["bytes"],
                                f"{self.model_name} {item['filename']}", progress, cancelled,
                            )
                        except OperationCancelled:
                            raise
                        except (AnalysisError, OSError) as error:
                            raise CaptionTimingError(
                                f"Could not download the optional caption timing model: {error}",
                            ) from error
                        if not _verified_file(downloaded, item, cancelled):
                            raise CaptionTimingError("Downloaded caption timing model is invalid")
                    # Publish only after every replacement passed verification.
                    self.model_path.mkdir(parents=True, exist_ok=True)
                    for item in invalid:
                        _check_cancel(cancelled)
                        os.replace(job / item["filename"], self.model_path / item["filename"])
                if self.model_path.is_dir():
                    for filename in MODEL_NOTICES:
                        source = (
                            self.manifest_path if filename == "caption-timing.json"
                            else default_manifest_path().parent / filename
                        )
                        payload = source.read_bytes()
                        destination = self.model_path / filename
                        if (
                            destination.is_file() and not destination.is_symlink()
                            and destination.read_bytes() == payload
                        ):
                            continue
                        job.mkdir(parents=True, exist_ok=True)
                        staged = job / filename
                        with staged.open("wb") as stream:
                            stream.write(payload)
                            stream.flush()
                            os.fsync(stream.fileno())
                        _check_cancel(cancelled)
                        os.replace(staged, destination)
                diagnostic_event(
                    "caption_timing_model_verified", component=self.component_key,
                    downloaded=bool(invalid),
                )
                return self.model_path
            finally:
                if job.exists():
                    shutil.rmtree(job)


def runtime_threads() -> int:
    from choicer_voicer_pack_creator.analysis import detect_hardware

    hardware = detect_hardware()
    physical = hardware.memory_bytes
    reserve = max(1024**3, min(2 * 1024**3, (physical or 0) // 8))
    available = hardware.available_memory_bytes
    if (
        physical is not None and physical < MODEL_WORKING_BYTES + reserve
        or available is not None and available < MODEL_WORKING_BYTES + reserve
    ):
        raise CaptionTimingError(
            "High-accuracy caption timing needs about 3 GiB working RAM plus "
            f"{reserve / 1024**3:g} GiB reserved for the editor and OS. Close other applications "
            "and retry, or leave caption timing unchanged."
        )
    threads = min(4, max(1, hardware.cpu_threads)) if available is not None else 1
    diagnostic_event(
        "caption_timing_resource_budget", threads=threads, available_bytes=available,
        working_bytes=MODEL_WORKING_BYTES, reserve_bytes=reserve,
    )
    return threads


def align_caption_audio(
    wav_path: Path, captions: Sequence[SourceCaption], duration: float, data_root: Path, *,
    language: str = "en", allow_download: bool = False,
    progress: ProgressCallback = lambda *_: None,
    cancelled: CancelCallback = lambda: False,
    manifest_path: Path | None = None,
) -> tuple[CaptionTimingEvidence, ...]:
    return cast(tuple[CaptionTimingEvidence, ...], _run_caption_audio(
        wav_path, captions, duration, data_root, language=language, allow_download=allow_download,
        progress=progress, cancelled=cancelled, manifest_path=manifest_path, audit=False,
    ))


def improve_caption_audio(
    wav_path: Path, captions: Sequence[SourceCaption], duration: float, data_root: Path, *,
    language: str = "en", allow_download: bool = False,
    progress: ProgressCallback = lambda *_: None,
    cancelled: CancelCallback = lambda: False,
    manifest_path: Path | None = None,
) -> CaptionTimingResult:
    """Align words, propose cuts, then independently audit their exact PCM ranges locally."""
    return cast(CaptionTimingResult, _run_caption_audio(
        wav_path, captions, duration, data_root, language=language, allow_download=allow_download,
        progress=progress, cancelled=cancelled, manifest_path=manifest_path, audit=True,
    ))


def _run_caption_audio(
    wav_path: Path, captions: Sequence[SourceCaption], duration: float, data_root: Path, *,
    language: str, allow_download: bool, progress: ProgressCallback, cancelled: CancelCallback,
    manifest_path: Path | None, audit: bool,
) -> tuple[CaptionTimingEvidence, ...] | CaptionTimingResult:
    from choicer_voicer_pack_creator.caption_timing_worker import align_captions, improve_captions

    if (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
        or not math.isfinite(duration) or duration <= 0
    ):
        raise CaptionTimingError("Caption timing requires a finite positive audio duration")
    if not isinstance(language, str) or re.fullmatch(r"auto|[a-z]{2,3}", language) is None:
        raise CaptionTimingError("Caption timing language must be auto or a lowercase language code")
    _check_cancel(cancelled)
    if not captions:
        return CaptionTimingResult((), (), ()) if audit else ()
    manager = CaptionTimingManager(data_root, manifest_path)
    with operation_scope(cancelled, progress):
        threads = runtime_threads()
        model = manager.ensure_model(progress, cancelled, allow_download=allow_download)

        def on_event(event: str, details: dict) -> bool:
            if event != "progress":
                return False
            progress(details["message"], details.get("fraction"))
            return True

        try:
            with path_leases(read_paths=(model, wav_path)):
                result = run_process_worker(
                    improve_captions if audit else align_captions,
                    (str(wav_path), tuple(captions), duration, str(manager.data_root),
                     str(manager.manifest_path), language, threads),
                    on_event=on_event, cancelled=cancelled, idle_timeout=600,
                    timeout=min(86400, 600 + (900 if audit else 600) * len(captions)),
                )
            _check_cancel(cancelled)
            if audit:
                if (
                    not isinstance(result, CaptionTimingResult)
                    or any(len(items) != len(captions) for items in (
                        result.captions, result.confidences, result.review_reasons,
                    ))
                    or any(
                        not isinstance(cue, SourceCaption)
                        or (cue.text, cue.fragments) != (original.text, original.fragments)
                        for cue, original in zip(result.captions, captions, strict=True)
                    )
                ):
                    raise CaptionTimingError("Caption timing worker returned incomplete cut review")
                diagnostic_event(
                    "caption_timing_cut_review_completed", captions=len(result.captions),
                    problems=sum(bool(reason) for reason in result.review_reasons),
                    model=manager.component_key,
                )
                return result
            if (
                not isinstance(result, tuple) or len(result) != len(captions)
                or any(not isinstance(item, CaptionTimingEvidence) or item.index != index
                       for index, item in enumerate(result))
            ):
                raise CaptionTimingError("Caption timing worker returned incomplete evidence")
            diagnostic_event(
                "caption_timing_alignment_completed", captions=len(result),
                problems=sum(bool(item.problem) for item in result), model=manager.component_key,
            )
            return result
        except OperationCancelled:
            raise
        except ProcessWorkerError as error:
            diagnostic_exception(
                "caption_timing_worker_failed", error,
                remote_traceback=error.remote_traceback, worker_error_type=error.error_type,
            )
            raise CaptionTimingError(f"Local caption timing failed: {error}") from error
