"""Pinned local NVIDIA Parakeet TDT 0.6B v3 transcription, the same model and sherpa-onnx
runtime that AudioTranscriber uses. Inference runs in an owned worker process."""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import uuid
import wave
from dataclasses import replace
from pathlib import Path

from choicer_voicer_pack_creator.analysis import (
    BUFFER_SIZE,
    AnalysisCancelled,
    AnalysisError,
    AnalysisSuggestion,
    CancelCallback,
    HardwareProfile,
    ProgressCallback,
    _check_cancel,
    download_verified,
)
from choicer_voicer_pack_creator.captions import pad_source_ranges
from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    operation_scope,
    path_leases,
)
from choicer_voicer_pack_creator.parakeet_worker import TranscriptLine, transcribe_lines
from choicer_voicer_pack_creator.process_worker import ProcessWorkerError, run_process_worker

MODEL_KEY = "parakeet"
MODEL_NAME = "Parakeet TDT 0.6B v3"
SOURCE = "Parakeet"
ARCHIVE_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{ARCHIVE_NAME}.tar.bz2"
)
ARCHIVE_BYTES = 487_170_055
ARCHIVE_SHA256 = "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf"
MODEL_FILES = {
    "encoder.int8.onnx": (652_184_281, "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247"),
    "decoder.int8.onnx": (11_845_275, "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e"),
    "joiner.int8.onnx": (6_355_277, "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3"),
    "tokens.txt": (93_939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
}
MODEL_BYTES = sum(size for size, _ in MODEL_FILES.values())
# Measured peak in AudioTranscriber is about 0.95 GB; keep a margin.
MODEL_MEMORY_BYTES = 1_200 * 1024**2
# Parakeet TDT 0.6B v3 detects which of these 25 European languages is spoken.
LANGUAGES = frozenset((
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu", "it", "lv", "lt",
    "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk",
))
NOTICE = (
    "NVIDIA Parakeet TDT 0.6B v3, https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3\n"
    "License: Creative Commons Attribution 4.0 International (CC BY 4.0), "
    "https://creativecommons.org/licenses/by/4.0/\n"
    f"Converted to int8 ONNX by the sherpa-onnx project (Apache-2.0): {ARCHIVE_URL}\n"
)


def supports_language(language: str) -> bool:
    return language == "auto" or language.split("-", 1)[0].lower() in LANGUAGES


def _file_hash(path: Path, cancelled: CancelCallback) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(BUFFER_SIZE):
            _check_cancel(cancelled)
            digest.update(block)
    return digest.hexdigest()


class ParakeetManager:
    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root).resolve()

    @property
    def model_dir(self) -> Path:
        return self.data_root / "parakeet" / ARCHIVE_NAME

    @property
    def component_key(self) -> str:
        return f"parakeet-model:{ARCHIVE_SHA256}"

    @property
    def download_bytes(self) -> int:
        return ARCHIVE_BYTES

    @property
    def setup_bytes(self) -> int:
        """Disk needed while the archive and the unpacked files both exist."""
        return ARCHIVE_BYTES + MODEL_BYTES + 64 * 1024**2

    @property
    def installed(self) -> bool:
        """Cheap availability hint; every transcription verifies the file hashes."""
        try:
            return all(
                (path := self.model_dir / name).is_file() and not path.is_symlink()
                and path.stat().st_size == size
                for name, (size, _) in MODEL_FILES.items()
            )
        except OSError:
            return False

    def _verified(self, cancelled: CancelCallback) -> bool:
        return self.installed and all(
            _file_hash(self.model_dir / name, cancelled) == digest
            for name, (_, digest) in MODEL_FILES.items()
        )

    def ensure_model(self, progress: ProgressCallback, cancelled: CancelCallback) -> Path:
        stage = self.model_dir.parent / f".extract-{uuid.uuid4().hex}"
        try:
            with operation_scope(cancelled, progress), path_leases(
                write_paths=(self.model_dir,),
            ):
                return self._ensure_model(stage, progress, cancelled)
        except OperationCancelled as error:
            raise AnalysisCancelled("Video analysis was canceled") from error
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _ensure_model(self, stage: Path, progress: ProgressCallback, cancelled: CancelCallback) -> Path:
        progress(f"Verifying the local {MODEL_NAME} model…", None)
        if self._verified(cancelled):
            diagnostic_event("parakeet_model_verified", directory=str(self.model_dir))
            return self.model_dir
        archive = self.data_root / "downloads" / f"{ARCHIVE_NAME}.tar.bz2"
        try:
            download_verified(
                ARCHIVE_URL, archive, ARCHIVE_SHA256, ARCHIVE_BYTES, f"{MODEL_NAME} model",
                progress, cancelled,
            )
            progress(f"Unpacking the {MODEL_NAME} model (about a minute)…", None)
            stage.mkdir(parents=True)
            found: set[str] = set()
            written = 0
            prefix = ARCHIVE_NAME + "/"
            with tarfile.open(archive, "r|bz2") as package:
                for member in package:
                    _check_cancel(cancelled)
                    name = member.name[len(prefix):] if member.name.startswith(prefix) else ""
                    if name not in MODEL_FILES:
                        continue
                    size, digest = MODEL_FILES[name]
                    if not member.isfile() or member.size != size or name in found:
                        raise AnalysisError(f"The Parakeet archive has an unexpected {name}")
                    source = package.extractfile(member)
                    if source is None:
                        raise AnalysisError(f"The Parakeet archive could not read {name}")
                    hasher = hashlib.sha256()
                    with source, (stage / name).open("wb") as output:
                        while block := source.read(BUFFER_SIZE):
                            _check_cancel(cancelled)
                            hasher.update(block)
                            output.write(block)
                            written += len(block)
                            progress(
                                f"Unpacking the {MODEL_NAME} model "
                                f"({written / 1024**2:.0f} / {MODEL_BYTES / 1024**2:.0f} MiB)…",
                                min(1.0, written / MODEL_BYTES),
                            )
                    if hasher.hexdigest() != digest:
                        raise AnalysisError(f"The Parakeet model file {name} failed verification")
                    found.add(name)
            if found != set(MODEL_FILES):
                raise AnalysisError("The Parakeet archive is missing model files")
            (stage / "NOTICE.txt").write_text(NOTICE, encoding="utf-8")
            _check_cancel(cancelled)
            if self.model_dir.exists():
                shutil.rmtree(self.model_dir)
            os.replace(stage, self.model_dir)
        except (OSError, tarfile.TarError) as error:
            raise AnalysisError(f"Could not install the {MODEL_NAME} model: {error}") from error
        finally:
            archive.unlink(missing_ok=True)
        diagnostic_event("parakeet_model_installed", directory=str(self.model_dir))
        progress(f"{MODEL_NAME} model setup complete.", 1.0)
        return self.model_dir

    def transcribe_lines(
        self,
        wav_path: Path,
        model: Path,
        hardware: HardwareProfile,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> tuple[list[TranscriptLine], float]:
        """Run the worker on verified model files; return unpadded lines and the audio duration."""
        with wave.open(str(wav_path), "rb") as audio:
            duration = audio.getnframes() / max(1, audio.getframerate())
        threads = max(1, min(4, hardware.cpu_threads))
        diagnostic_event(
            "parakeet_transcription_starting", threads=threads, audio_duration_seconds=duration,
        )

        def on_event(event: str, details: dict) -> bool:
            if event != "progress":
                return False
            progress(str(details["message"]), details.get("fraction"))
            return True

        try:
            with operation_scope(cancelled, progress), path_leases(read_paths=(model, wav_path)):
                lines = run_process_worker(
                    transcribe_lines, (str(wav_path), str(model), threads),
                    on_event=on_event, cancelled=cancelled,
                    timeout=max(900.0, duration * 10), idle_timeout=600,
                )
        except OperationCancelled as error:
            raise AnalysisCancelled("Video analysis was canceled") from error
        except ProcessWorkerError as error:
            diagnostic_exception(
                "parakeet_worker_failed", error,
                remote_traceback=error.remote_traceback, worker_error_type=error.error_type,
            )
            raise AnalysisError(f"Local Parakeet transcription failed: {error}") from error
        return lines, duration

    def transcribe(
        self,
        wav_path: Path,
        language: str,
        hardware: HardwareProfile,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> tuple[list[AnalysisSuggestion], str | None]:
        if not supports_language(language):
            raise AnalysisError(
                f"{MODEL_NAME} supports 25 European languages. "
                "Choose a Whisper model for this spoken language."
            )
        model = self.ensure_model(progress, cancelled)
        lines, duration = self.transcribe_lines(wav_path, model, hardware, progress, cancelled)
        suggestions = [
            AnalysisSuggestion(
                max(0.0, min(duration, line.start)), max(0.0, min(duration, line.end)),
                line.text, SOURCE, line.confidence,
            )
            for line in lines
        ]
        suggestions = [item for item in suggestions if item.caption and item.end - item.start >= 0.05]
        padded = pad_source_ranges(
            [(item.start, item.end) for item in suggestions], duration,
            check_cancel=lambda: _check_cancel(cancelled),
        )
        suggestions = [
            replace(item, start=round(start, 3), end=min(duration, round(end, 3)))
            for item, (start, end) in zip(suggestions, padded, strict=True)
        ]
        progress(f"Parakeet produced {len(suggestions)} transcript region(s).", 1.0)
        diagnostic_event("parakeet_transcript_parsed", regions=len(suggestions))
        return suggestions, None
