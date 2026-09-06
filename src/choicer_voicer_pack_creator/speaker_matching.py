"""Conservative local voice similarity, not diarization or speaker identification.

Callers supply only HUMAN single-character assignments as references, never prior
automatic matches. Results are proposals; callers must recheck live labels/ranges.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.analysis import AnalysisError, download_verified
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    SourceSnapshot,
    canonical_path,
    check_cancelled,
    operation_scope,
    path_leases,
)
from choicer_voicer_pack_creator.process_worker import ProcessWorkerError, run_process_worker

ProgressCallback = Callable[[str, float | None], None]
CancelCallback = Callable[[], bool]
SAMPLE_RATE = 16000
EMBEDDING_DIMENSIONS = 256
PREPROCESSING_VERSION = "wespeaker-knf-1"
MIN_ACTIVE_SECONDS = 1.5
MAX_CLIP_SECONDS = 12.0
ACTIVITY_WINDOW_SECONDS = 0.02
MIN_RMS = 0.005
MIN_COSINE = 0.72
SINGLE_CHARACTER_MIN_COSINE = 0.78
MIN_RUNNER_UP_MARGIN = 0.12
MODEL_SHA256 = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
MODEL_BYTES = 26530550
MODEL_FILENAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"
MODEL_NOTICES = ("WeSpeaker-Attribution.txt", "WeSpeaker-CC-BY-4.0.txt", "speaker-matching.json")


class SpeakerMatchingError(RuntimeError):
    pass


class SpeakerDownloadRequired(SpeakerMatchingError):
    """Missing or damaged optional model; downloading/repair needs explicit consent."""


class SpeakerMatchingCancelled(SpeakerMatchingError, OperationCancelled):
    pass


@dataclass(frozen=True, slots=True)
class SpeakerClip:
    segment_id: str
    path: str
    start: float
    end: float | None
    characters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpeakerMatch:
    segment_id: str
    character: str
    similarity: float


@dataclass(frozen=True, slots=True)
class SpeakerResult:
    matches: tuple[SpeakerMatch, ...]
    sources: SourceSnapshot
    examined: int
    cached: int
    skipped: int
    skip_reasons: tuple[tuple[str, str], ...] = ()


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "speaker-matching.json"


def verify_model(path: Path, cancelled: CancelCallback) -> bool:
    check_cancelled()
    if cancelled():
        raise SpeakerMatchingCancelled("Speaker matching was canceled")
    if not path.is_file() or path.stat().st_size != MODEL_BYTES:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            check_cancelled()
            if cancelled():
                raise SpeakerMatchingCancelled("Speaker matching was canceled")
            digest.update(block)
    check_cancelled()
    return digest.hexdigest() == MODEL_SHA256


def normalized_embedding(value: Any) -> Any:
    import numpy as np

    array = np.asarray(value, dtype=np.float32).reshape(-1)
    if array.shape != (EMBEDDING_DIMENSIONS,) or not np.isfinite(array).all():
        raise SpeakerMatchingError("The speaker model returned an invalid embedding")
    norm = float(np.linalg.norm(array))
    if not math.isfinite(norm) or norm < 1e-8:
        raise SpeakerMatchingError("The speaker model returned an empty embedding")
    return array / norm


def choose_matches(
    clips: tuple[SpeakerClip, ...], signatures: dict[str, Any],
) -> tuple[SpeakerMatch, ...]:
    """Cosine scores are similarity measures, not calibrated probabilities."""
    import numpy as np

    references: dict[str, list[Any]] = {}
    for clip in clips:
        check_cancelled()
        if len(clip.characters) == 1 and clip.segment_id in signatures:
            references.setdefault(clip.characters[0], []).append(
                normalized_embedding(signatures[clip.segment_id]),
            )
    prototypes = {}
    for character, seeds in references.items():
        mean = np.mean(seeds, axis=0)
        if float(np.linalg.norm(mean)) >= 1e-8:
            prototypes[character] = normalized_embedding(mean)
    matches = []
    for clip in clips:
        check_cancelled()
        if clip.characters or clip.segment_id not in signatures or not prototypes:
            continue
        target = normalized_embedding(signatures[clip.segment_id])
        scores = sorted(
            ((float(np.clip(np.dot(target, mean), -1, 1)), character)
             for character, mean in prototypes.items()),
            reverse=True,
        )
        score, character = scores[0]
        threshold = SINGLE_CHARACTER_MIN_COSINE if len(prototypes) == 1 else MIN_COSINE
        # A centroid alone can overstate evidence from mutually inconsistent references.
        best_seed = max(float(np.dot(target, seed)) for seed in references[character])
        if score < threshold or best_seed < threshold:
            continue
        if len(scores) > 1 and score - scores[1][0] < MIN_RUNNER_UP_MARGIN:
            continue
        matches.append(SpeakerMatch(clip.segment_id, character, score))
    return tuple(matches)


class SpeakerMatchingManager:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.manifest_path = default_manifest_path()
        try:
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            model = self.manifest["model"]
            if (
                self.manifest["version"] != 1 or model["filename"] != MODEL_FILENAME
                or model["sha256"] != MODEL_SHA256 or model["bytes"] != MODEL_BYTES
                or not model["url"].startswith("https://")
                or self.manifest["preprocessing"]["version"] != PREPROCESSING_VERSION
                or self.manifest["preprocessing"]["sample_rate"] != SAMPLE_RATE
                or self.manifest["preprocessing"]["embedding_dimensions"] != EMBEDDING_DIMENSIONS
            ):
                raise ValueError("unsupported model configuration")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SpeakerMatchingError(f"Speaker model manifest is invalid: {error}") from error

    @property
    def model_path(self) -> Path:
        return self.data_root / "speaker-models" / MODEL_SHA256 / MODEL_FILENAME

    @property
    def model_download_bytes(self) -> int:
        return MODEL_BYTES

    @property
    def cache_directory(self) -> Path:
        return self.data_root / "speaker-signatures" / PREPROCESSING_VERSION / MODEL_SHA256

    def _ensure_model(
        self, job: Path, allow_download: bool, progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> None:
        with path_leases(write_paths=(self.model_path.parent,)):
            progress("Verifying the local speaker model…", None)
            if not verify_model(self.model_path, cancelled):
                if not allow_download:
                    raise SpeakerDownloadRequired(
                        "The local speaker model is missing or invalid. Downloading or repairing "
                        f"it requires permission ({MODEL_BYTES / 1024**2:.1f} MiB)."
                    )
                model = self.manifest["model"]
                downloaded = download_verified(
                    model["url"], job / MODEL_FILENAME, MODEL_SHA256, MODEL_BYTES,
                    "WeSpeaker speaker-matching model", progress, cancelled,
                )
                check_cancelled()
                # Recheck the staged file even when a custom downloader supplied it.
                if not verify_model(downloaded, cancelled):
                    raise SpeakerMatchingError("Downloaded speaker model failed verification")
                self.model_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(downloaded, self.model_path)
                except PermissionError:
                    if not verify_model(self.model_path, cancelled):
                        raise
            for filename in MODEL_NOTICES:
                check_cancelled()
                payload = (self.manifest_path.parent / filename).read_bytes()
                destination = self.model_path.parent / filename
                if destination.is_file() and destination.read_bytes() == payload:
                    continue
                staged = job / filename
                with staged.open("wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                check_cancelled()
                try:
                    os.replace(staged, destination)
                except PermissionError:
                    if not destination.is_file() or destination.read_bytes() != payload:
                        raise

    def _cache_key(self, clip: SpeakerClip, sources: SourceSnapshot) -> str:
        identity = next(row for row in sources.entries if row[0] == canonical_path(clip.path))
        payload = {
            "version": PREPROCESSING_VERSION, "model": MODEL_SHA256,
            "source": identity, "start": float(clip.start),
            # Some Windows filesystems retain the file index across atomic replacement.
            "ctime_ns": Path(clip.path).stat().st_ctime_ns,
            "end": None if clip.end is None else float(clip.end),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8"),
        ).hexdigest()

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        import numpy as np

        path = self.cache_directory / f"{key}.json"
        try:
            if path.stat().st_size > 32768:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("key") != key:
                return None
            record = value["record"]
            encoded = json.dumps(record, sort_keys=True, allow_nan=False).encode("utf-8")
            if hashlib.sha256(encoded).hexdigest() != value.get("sha256"):
                return None
            if record["reason"] not in {"", "short", "silence", "nonfinite", "no-audio"}:
                return None
            if record["reason"]:
                return record if record["embedding"] is None else None
            vector = np.asarray(record["embedding"], dtype=np.float32)
            if vector.shape != (EMBEDDING_DIMENSIONS,) or not np.isfinite(vector).all():
                return None
            return record if abs(float(np.linalg.norm(vector)) - 1) < 1e-4 else None
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return None

    def _write_cache(self, key: str, record: dict[str, Any], job: Path) -> None:
        encoded = json.dumps(record, sort_keys=True, allow_nan=False).encode("utf-8")
        value = {"key": key, "record": record, "sha256": hashlib.sha256(encoded).hexdigest()}
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        staged = job / f"{key}.json"
        with staged.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        check_cancelled()
        os.replace(staged, self.cache_directory / staged.name)

    def match(
        self, media: MediaTools, clips: tuple[SpeakerClip, ...], *,
        allow_download: bool, progress: ProgressCallback, cancelled: CancelCallback,
    ) -> SpeakerResult:
        job = self.data_root / "speaker-jobs" / uuid.uuid4().hex
        try:
            with operation_scope(cancelled, progress):
                self._validate_clips(clips)
                with path_leases(read_paths=(clip.path for clip in clips)):
                    sources = SourceSnapshot.capture(clip.path for clip in clips)
                    eligible = tuple(
                        clip for clip in clips
                        if clip.end is None or clip.end - clip.start >= MIN_ACTIVE_SECONDS
                    )
                    if not any(len(clip.characters) == 1 for clip in eligible) or not any(
                        not clip.characters for clip in eligible
                    ):
                        reasons = tuple(
                            (
                                clip.segment_id,
                                "multiple-characters" if len(clip.characters) > 1 else "short",
                            )
                            for clip in clips
                            if len(clip.characters) > 1 or (
                                clip.end is not None and clip.end - clip.start < MIN_ACTIVE_SECONDS
                            )
                        )
                        sources.verify()
                        return SpeakerResult((), sources, 0, 0, len(reasons), reasons)
                    job.mkdir(parents=True)
                    self._ensure_model(job, allow_download, progress, cancelled)
                    with path_leases(
                        read_paths=(self.model_path,),
                        write_paths=(self.cache_directory,),
                    ):
                        return self._match(media, clips, sources, job, progress, cancelled)
        except OperationCancelled as error:
            raise SpeakerMatchingCancelled("Speaker matching was canceled") from error
        except SpeakerMatchingError:
            raise
        except (OSError, ValueError, SourceChangedError, ProcessWorkerError, AnalysisError) as error:
            raise SpeakerMatchingError(f"Local speaker matching failed: {error}") from error
        finally:
            if job.exists():
                shutil.rmtree(job)

    @staticmethod
    def _validate_clips(clips: tuple[SpeakerClip, ...]) -> None:
        ids = set()
        for clip in clips:
            check_cancelled()
            if (
                not clip.segment_id or clip.segment_id in ids or not clip.path
                or not math.isfinite(clip.start) or clip.start < 0
                or (clip.end is not None and (
                    not math.isfinite(clip.end) or clip.end <= clip.start
                ))
                or any(not name.strip() for name in clip.characters)
            ):
                raise SpeakerMatchingError("Speaker clips require unique IDs and valid source ranges")
            if not Path(clip.path).is_file():
                raise SpeakerMatchingError("A speaker-matching source file is missing")
            ids.add(clip.segment_id)

    def _match(
        self, media: MediaTools, clips: tuple[SpeakerClip, ...], sources: SourceSnapshot,
        job: Path, progress: ProgressCallback, cancelled: CancelCallback,
    ) -> SpeakerResult:
        from choicer_voicer_pack_creator.speaker_worker import embed_clips

        keys = {}
        records = {}
        pending = {}
        cached = 0
        reasons = []
        for clip in clips:
            check_cancelled()
            if len(clip.characters) > 1:
                reasons.append((clip.segment_id, "multiple-characters"))
                continue
            if clip.end is not None and clip.end - clip.start < MIN_ACTIVE_SECONDS:
                reasons.append((clip.segment_id, "short"))
                continue
            key = keys[clip.segment_id] = self._cache_key(clip, sources)
            record = self._read_cache(key)
            if record is not None:
                records[key] = record
                cached += 1
            else:
                pending.setdefault(key, clip)

        def on_event(event: str, details: dict) -> bool:
            if event != "progress":
                return False
            progress(details["message"], details["fraction"])
            return True

        if pending:
            generated = run_process_worker(
                embed_clips,
                (str(self.model_path), media.ffmpeg, media.ffprobe, str(job), tuple(pending.items())),
                on_event=on_event, cancelled=cancelled,
                timeout=180 + 120 * len(pending), idle_timeout=120,
            )
            check_cancelled()
            sources.verify()
            if set(generated) != set(pending):
                raise SpeakerMatchingError("Speaker worker returned an incomplete signature set")
            for key, record in generated.items():
                check_cancelled()
                self._write_cache(key, record, job)
                records[key] = record

        signatures = {}
        examined = 0
        for clip in clips:
            check_cancelled()
            if clip.segment_id not in keys:
                continue
            record = records[keys[clip.segment_id]]
            if record["reason"]:
                reasons.append((clip.segment_id, record["reason"]))
                progress(f"Skipped voice clip {clip.segment_id}: {record['reason']}.", None)
            else:
                signatures[clip.segment_id] = record["embedding"]
                examined += not clip.characters
        matches = choose_matches(clips, signatures)
        matched = {match.segment_id for match in matches}
        reasons.extend(
            (clip.segment_id, "insufficient-evidence")
            for clip in clips
            if not clip.characters and clip.segment_id in signatures and clip.segment_id not in matched
        )
        check_cancelled()
        sources.verify()
        progress(f"Voice matching finished: {len(matches)} matches; {len(reasons)} skipped.", 1.0)
        check_cancelled()
        sources.verify()
        return SpeakerResult(matches, sources, examined, cached, len(reasons), tuple(reasons))
