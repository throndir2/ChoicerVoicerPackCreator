from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QCheckBox, QTableWidgetItem, QVBoxLayout, QWidget

from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.jobs import JobHandle, JobRecord
from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.operations import SourceChangedError
from choicer_voicer_pack_creator.speaker_matching import (
    MIN_ACTIVE_SECONDS,
    PREPARATION_BATCH_SIZE,
    SpeakerClip,
    SpeakerDownloadRequired,
    SpeakerMatchingCancelled,
    SpeakerMatchingManager,
    SpeakerPreparationRequired,
    SpeakerPreparationResult,
    SpeakerResult,
)
from choicer_voicer_pack_creator.ui.job_worker import JobWorker

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import ProjectEditor


def update_speaker_item(item: QTableWidgetItem, segment: Segment) -> None:
    automatic = segment.speaker_assignment == "automatic"
    item.setText(", ".join(segment.characters))
    font = item.font()
    font.setItalic(automatic)
    item.setFont(font)
    item.setToolTip(
        "Automatically matched voice. Review this name; edit it to make a manual reference."
        if automatic else
        "Kept unassigned. Uncheck Keep unassigned to include this segment in matching."
        if segment.speaker_assignment == "excluded" else ", ".join(segment.characters)
    )


def _nonverbal(caption: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:[\[(](?:grunts?|grunting|groans?|groaning|sighs?|sighing|"
        r"gasps?|gasping|screams?|screaming|laughs?|laughing|laughter|"
        r"music|silence|breathing|crying)[\])]|u+gh|u+h|h+m+|m+m|huh|a+h)"
        r"\s*[.!?\u2026]*\s*",
        caption, re.IGNORECASE,
    ))


@dataclass(frozen=True)
class _SegmentState:
    start: float
    end: float
    audio_mode: str
    audio_path: str
    source_range_known: bool
    characters: tuple[str, ...]
    assignment: str
    nonverbal: bool

    @classmethod
    def capture(cls, segment: Segment) -> _SegmentState:
        return cls(
            segment.start, segment.end, segment.audio_mode, segment.audio_path,
            segment.source_range_known, tuple(segment.characters), segment.speaker_assignment,
            _nonverbal(segment.caption),
        )


@dataclass(frozen=True)
class _Request:
    generation: int
    source_token: tuple[str, int, str]
    references: tuple[tuple[str, _SegmentState, int], ...]
    targets: Mapping[str, tuple[_SegmentState, int]]
    clips: tuple[SpeakerClip, ...]
    preparing: bool = False
    verifying: bool = False

    @property
    def key(self) -> str:
        return "speaker-preparation" if self.preparing else "speakers"


def _audio_range(clip: SpeakerClip) -> tuple[str, float, float | None]:
    return clip.path, clip.start, clip.end


class SpeakerWorker(JobWorker):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)
    download_required = Signal()
    canceled = Signal()
    preparation_required = Signal()

    def __init__(
        self, manager, media, clips, *, allow_download: bool, preparing: bool,
        verification: SpeakerResult | None = None,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.media = media
        self.clips = clips
        self.allow_download = allow_download
        self.preparing = preparing
        self.outcome = ""
        self.verification = verification
        self.missing_ids: tuple[str, ...] = ()

    def run(self) -> None:
        def report(message: str, fraction: float | None) -> None:
            self.progress.emit(
                message, -1 if fraction is None else max(0, min(1000, round(fraction * 1000))),
            )

        try:
            if self.verification is not None:
                result = self.verification
            elif self.preparing:
                result = self.manager.prepare(
                    self.media, self.clips, allow_download=self.allow_download,
                    progress=report, cancelled=self.isInterruptionRequested,
                )
            else:
                result = self.manager.match_cached(
                    self.media, self.clips, progress=report, cancelled=self.isInterruptionRequested,
                )
            result.sources.verify()
            self.outcome = "completed"
            self.completed.emit(result)
        except SpeakerPreparationRequired as error:
            self.outcome = "prepare"
            self.missing_ids = error.segment_ids
            self.preparation_required.emit()
        except SpeakerDownloadRequired:
            self.outcome = "download"
            self.download_required.emit()
        except SpeakerMatchingCancelled:
            self.outcome = "canceled"
            self.canceled.emit()
        except SourceChangedError:
            self.outcome = "failed"
            self.failed.emit("Source audio changed. Retry to analyze the new audio.")
        except Exception as error:
            self.outcome = "failed"
            diagnostic_exception("speaker_matching_failed", error)
            self.failed.emit(str(error))


class SpeakerMatchingControls(QWidget):
    """Per-document scheduling and optimistic publication; never lock the editor."""

    def __init__(self, editor: ProjectEditor) -> None:
        super().__init__(editor)
        self.editor = editor
        self.derived_work = editor.derived_work
        self.worker: SpeakerWorker | None = None
        self._workers: dict[str, SpeakerWorker] = {}
        self._generation = 0
        self._source_token = editor.session.source_token()
        self._observed: dict[str, _SegmentState] = {}
        self._versions: dict[str, int] = {}
        self._request: _Request | None = None
        self._activated = False
        self._preprocess = False
        self._prepared_ranges: set[tuple[str, float, float | None]] = set()
        self._typing = False
        self._paused = False
        self._history_paused = False
        self._resume_requested = False
        self._canceled_run = False
        self._pending_consent = False
        self._consent_callback: Callable[[bool], None] | None = None
        self._allow_download = False
        self._closed = False
        self._applying = False
        self._pending = False
        self._publication: tuple[SpeakerResult, _Request] | None = None
        self._undo: dict[str, tuple[str, int]] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(900)
        self._timer.timeout.connect(self._start)
        self.derived_work.changed.connect(self._update_actions)
        self.derived_work.resumed.connect(self._gesture_finished)
        self.derived_work.cancelled.connect(self._derived_cancelled)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.enabled_check = QCheckBox("Auto-fill speaker names")
        self.enabled_check.setObjectName("autoSpeakerMatching")
        self.enabled_check.setToolTip(
            "Prepare local voice fingerprints in the background as transcript ranges arrive. "
            "After you name a segment, compare cached voices without rerunning the model. "
            "For a stronger reference, name a clear dialogue line of about 2 seconds or more. "
            "Manual names and cleared segments are preserved."
        )
        self.enabled_check.setChecked(editor.project.auto_speaker_matching)
        self.enabled_check.toggled.connect(self._enabled_changed)
        layout.addWidget(self.enabled_check)
        self._update_actions()

    def _current(self) -> bool:
        workspace = self.editor.workspace
        return (
            self._document_available() and not workspace._closing
            and not self.editor.session.loading
            and not getattr(getattr(self.editor, "edit_history", None), "busy", False)
        )

    def _document_available(self) -> bool:
        return (
            not self._closed
            and self.editor.session.id not in self.editor.workspace._closed_ids
        )

    def _observe(self, segment: Segment | None = None) -> bool:
        if segment is not None:
            state = _SegmentState.capture(segment)
            if self._observed.get(segment.id) == state:
                return False
            self._observed[segment.id] = state
            self._versions[segment.id] = self._versions.get(segment.id, 0) + 1
            return True
        current = {
            segment.id: _SegmentState.capture(segment) for segment in self.editor.project.segments
        }
        changed = current != self._observed
        for identity in current.keys() | self._observed.keys():
            if current.get(identity) != self._observed.get(identity):
                self._versions[identity] = self._versions.get(identity, 0) + 1
        self._observed = current
        return changed

    def project_replaced(self, *, preserve_view: bool) -> None:
        token = self.editor.session.source_token()
        if token != self._source_token:
            self._source_token = token
            self._invalidate_comparison()
            self.derived_work.invalidate("speaker-preparation")
            self._timer.stop()
            self._publication = None
            if self.worker is not None:
                self.worker.requestInterruption()
            self._activated = False
            self._preprocess = False
            self._prepared_ranges.clear()
            self._typing = False
            self._paused = False
            self._history_paused = False
            self.derived_work.resume("speakers")
            self.derived_work.resume("speaker-preparation")
            self._resume_requested = False
            self._undo.clear()
            if self._consent_callback is not None:
                self.editor.workspace.setup_consent.cancel_request(self._consent_callback)
        names_changed = preserve_view and any(
            segment.speaker_assignment == "manual"
            and any(name.strip() for name in segment.characters)
            and (
                (state := self._observed.get(segment.id)) is None
                or state.characters != tuple(segment.characters)
            )
            for segment in self.editor.project.segments
        )
        enabled_changed = self.enabled_check.isChecked() != self.editor.project.auto_speaker_matching
        with QSignalBlocker(self.enabled_check):
            self.enabled_check.setChecked(self.editor.project.auto_speaker_matching)
        if enabled_changed:
            if self.editor.project.auto_speaker_matching:
                self._activated = True
                self._paused = False
                self._timer.start(900)
            else:
                self.cancel()
        if names_changed:
            self._activated = True
        self.changed()

    def changed(self, *, segment: Segment | None = None) -> None:
        changed = self._observe(segment)
        self._update_actions()
        if not changed or self._applying:
            return
        self._invalidate_comparison()
        # Preparation labels no live segments: its immutable old ranges remain
        # useful, source-verified cache entries even if a range changes mid-batch.
        if self.worker is not None:
            self._pending = True
        if (self._activated or self._preprocess) and not self._paused:
            self._timer.start(900)

    def _gesture_finished(self) -> None:
        if not self._paused and self._timer.isActive():
            self._timer.start(900)

    def _invalidate_comparison(self) -> None:
        self._generation = self.derived_work.invalidate("speakers")
        self._workers = {
            identity: worker for identity, worker in self._workers.items()
            if worker.isRunning() or worker.preparing
        }
        self._publication = None
        if self._activated and not self._paused and self._document_available():
            self.editor.processing.set_status(
                "speakers", "queued",
                "Reference speakers changed. Rechecking with your latest names and ranges.",
            )

    def history_replayed(self) -> None:
        self._source_token = self.editor.session.source_token()
        self._history_paused = True
        self._paused = True
        self._typing = False
        self._undo.clear()
        self._timer.stop()
        self._pending = False
        self._invalidate_comparison()
        self.derived_work.pause("speakers")
        self.derived_work.pause("speaker-preparation")
        self._generation = self.derived_work.generation("speakers")
        with QSignalBlocker(self.enabled_check):
            self.enabled_check.setChecked(self.editor.project.auto_speaker_matching)
        if self._consent_callback is not None:
            self.editor.workspace.setup_consent.cancel_request(self._consent_callback)
        self._observe()
        for kind in ("speaker-preparation", "speakers"):
            self.editor.processing.set_status(
                kind, "cancelled", "Speaker matching paused after undo or redo.",
            )
        self._update_actions()

    def prepare(self) -> None:
        """Activate name-independent preparation for this imported source."""
        self._preprocess = True
        if not self.editor.project.auto_speaker_matching:
            self.editor.processing.set_status("speaker-preparation", "off", "Automatic voice matching is off.")
            return
        self._start()

    def prepare_if_enabled(self) -> None:
        if self._preprocess:
            self._start()

    def name_typed(self, segment: Segment) -> None:
        if self._history_paused:
            self._history_paused = False
            self._paused = False
            self.derived_work.resume("speakers")
            self.derived_work.resume("speaker-preparation")
        segment.speaker_assignment = (
            "manual" if any(name.strip() for name in segment.characters) else "excluded"
        )
        self._typing = True
        if self._preprocess:
            self._timer.start(900)
        else:
            self._timer.stop()

    def name_committed(self, *, force: bool = False) -> None:
        if not self._typing and not force:
            return
        self._typing = False
        self._activated = True
        self._preprocess = True
        if not self._paused:
            self._pending = self.worker is not None
            self._timer.start(900)

    @Slot(bool)
    def _enabled_changed(self, enabled: bool) -> None:
        self.editor.project.auto_speaker_matching = enabled
        self.editor._set_dirty(
            True, history_label="Change automatic speaker matching", fields_only=True,
        )
        if enabled:
            self.retry()
        else:
            self.cancel()

    @Slot()
    def retry(self) -> None:
        if not self._current():
            return
        if self.worker is not None:
            if self._paused:
                self._resume_requested = True
                self.editor.processing.set_status(
                    "speakers", "cancelling",
                    "Waiting for cancellation to finish, then restarting matching.",
                )
            return
        if not self.editor.project.auto_speaker_matching:
            self.enabled_check.setChecked(True)
            return
        self._paused = False
        self._history_paused = False
        self.derived_work.resume("speakers")
        self.derived_work.resume("speaker-preparation")
        self._activated = True
        self._preprocess = True
        self._typing = False
        self.editor.processing.set_status("speakers", "idle", "Waiting for a named dialogue reference.")
        self.editor.processing.set_status(
            "speaker-preparation", "ready" if self._prepared_ranges else "queued",
            "Voice fingerprints ready." if self._prepared_ranges else "Preparing voice fingerprints.",
        )
        self._observe()
        self._timer.start(0)

    def _inputs(self) -> tuple[
        tuple[SpeakerClip, ...], tuple[tuple[str, _SegmentState, int], ...],
        dict[str, tuple[_SegmentState, int]],
    ]:
        clips = []
        references = []
        targets = {}
        for segment in self.editor.project.segments:
            state = _SegmentState.capture(segment)
            if state.assignment != "manual" or state.nonverbal:
                continue
            names = tuple(name.strip() for name in state.characters if name.strip())
            if len(names) > 1:
                continue
            if state.audio_mode == "video":
                if not self.editor.project.video_path or not state.source_range_known:
                    continue
                if state.end - state.start < MIN_ACTIVE_SECONDS:
                    continue
                path, start, end = self.editor.project.video_path, state.start, state.end
            else:
                if not state.audio_path:
                    continue
                path, start, end = state.audio_path, 0.0, None
            clips.append(SpeakerClip(segment.id, path, start, end, names))
            version = self._versions.get(segment.id, 0)
            if names:
                references.append((segment.id, state, version))
            else:
                targets[segment.id] = state, version
        return tuple(clips), tuple(references), targets

    def _preparation_inputs(self, clips: tuple[SpeakerClip, ...]) -> tuple[SpeakerClip, ...]:
        ranges = {
            _audio_range(clip): SpeakerClip(clip.segment_id, clip.path, clip.start, clip.end)
            for clip in clips
        }
        project = self.editor.project
        if project.video_path and project.analysis_review is not None:
            rows = project.analysis_review.local_rows + project.analysis_review.refined_rows
            for index, row in enumerate(rows):
                if not row.checked or _nonverbal(row.caption):
                    continue
                try:
                    start, end = float(row.start), float(row.end)
                except ValueError:
                    diagnostic_event("voice_draft_range_skipped", row=index, reason="unfinished_time")
                    continue
                if (
                    not math.isfinite(start) or not math.isfinite(end) or start < 0
                    or end > project.video_duration or end - start < MIN_ACTIVE_SECONDS
                ):
                    continue
                clip = SpeakerClip(f"draft-{index}", project.video_path, start, end)
                ranges.setdefault(_audio_range(clip), clip)
        return tuple(
            SpeakerClip(f"prepare-{index}", clip.path, clip.start, clip.end)
            for index, clip in enumerate(ranges.values())
        )

    @Slot()
    def _start(self) -> None:
        if (
            not self._current() or not self.editor.project.auto_speaker_matching
            or self._paused or self._pending_consent
        ):
            return
        if self.editor._range_edit_record is not None:
            # A pause with the mouse held down is not a committed audio range.
            self._timer.start(900)
            return
        if self.worker is not None or self._publication is not None:
            self._pending = True
            return
        self._observe()
        clips, references, targets = self._inputs()
        preparation = tuple(
            clip for clip in self._preparation_inputs(clips)
            if _audio_range(clip) not in self._prepared_ranges
        ) if self._preprocess else ()
        preparing = bool(preparation)
        if not preparing and (self._typing or not self._activated):
            if not self._prepared_ranges and self._preprocess:
                self.editor.processing.set_status(
                    "speaker-preparation", "waiting",
                    "Waiting for dialogue ranges from a transcript or your segments.",
                )
            return
        if not preparing and (not references or not targets):
            message = (
                f"Name a dialogue segment with at least {MIN_ACTIVE_SECONDS:g} seconds of speech."
                if not references else "No eligible unassigned segments to match."
            )
            self.editor.processing.set_status("speakers", "idle" if not references else "ready", message)
            if self._preprocess:
                self.editor.processing.set_status(
                    "speaker-preparation", "ready" if self._prepared_ranges else "waiting",
                    "Voice fingerprints ready." if self._prepared_ranges else message,
                )
            return
        if preparing:
            # Amortize model setup while yielding shared CPU capacity between batches.
            clips = preparation[:PREPARATION_BATCH_SIZE]
        key = "speaker-preparation" if preparing else "speakers"
        request = _Request(
            self.derived_work.generation(key), self.editor.session.source_token(),
            references, MappingProxyType(targets), clips, preparing,
        )
        self._request = request
        self._pending = False
        self._canceled_run = False
        try:
            manager = SpeakerMatchingManager(self.editor.analysis_data_root)
        except (OSError, RuntimeError, ValueError) as error:
            diagnostic_exception("speaker_matching_setup_failed", error)
            self._failed(f"Speaker matching could not start: {error}")
            return
        self._enqueue(manager, request)

    def _enqueue(
        self, manager: SpeakerMatchingManager, request: _Request,
        verification: SpeakerResult | None = None,
    ) -> None:
        def start(generation: int) -> JobHandle:
            nonlocal request
            request = replace(request, generation=generation)
            self._request = request
            if request.key == "speakers":
                self._generation = generation
            return self._launch(manager, request, verification)

        self.derived_work.request(
            request.key, start, lambda record: self._publish(record, request), delay_ms=0,
        )

    def _launch(
        self, manager: SpeakerMatchingManager, request: _Request,
        verification: SpeakerResult | None = None,
    ) -> JobHandle:
        preparing, clips = request.preparing, request.clips
        worker = SpeakerWorker(
            manager, self.editor.media, clips, allow_download=self._allow_download,
            preparing=preparing, verification=verification,
        )
        self.worker = worker
        worker.configure_job(
            self.editor.workspace.job_manager, self.editor.session.id,
            "speaker-preparation" if preparing else "speakers",
            "Prepare voice fingerprints" if preparing else
            "Verify speaker source audio" if request.verifying else "Match cached voices",
            resource_class="cpu" if preparing else "io",
            read_paths=tuple({Path(clip.path) for clip in clips}),
            resource_keys=("speaker-matching-inference",) if preparing else (),
            source_snapshot={
                "source_revision": self.editor.session.source_revision,
                "derived_key": request.key, "derived_generation": request.generation,
            },
            priority=10 if preparing else 20,
        )
        worker.finished.connect(lambda: self._finished(worker, request))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        tasks = self.editor.workspace.tasks_window
        job_id = worker.job_handle.id
        tasks.register_retry(
            job_id, self.retry, available=lambda: self._current() and self.worker is None,
        )
        self.destroyed.connect(lambda: tasks.unregister_retry(job_id))
        self._update_actions()
        # Retain the worker only until this request's terminal publication.
        self._workers[job_id] = worker
        return worker.job_handle

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self._request is not None and not self._request_current(self._request):
            return
        self._paused = True
        self.derived_work.pause("speakers")
        self.derived_work.pause("speaker-preparation")
        self.editor.processing.set_status(
            "speaker-preparation" if self._request is not None and self._request.preparing else "speakers",
            "failed", message,
        )

    def _finished(self, worker: SpeakerWorker, request: _Request) -> None:
        if self.worker is worker:
            self.worker = None
        if not self._request_current(request):
            self._workers.pop(worker.job_handle.id, None)
            if self._canceled_run and self._document_available():
                resume = self._resume_requested and self.editor.project.auto_speaker_matching
                self._resume_requested = False
                self._canceled_run = False
                if resume:
                    self.retry()
                else:
                    self.cancel()
                return
            self._update_actions()
            if (
                self._document_available() and self._preprocess and not self._paused
                and not self._timer.isActive()
            ):
                self._timer.start(900)
            return
        if isinstance(worker.job_handle.record.result, SpeakerResult):
            self._publication = worker.job_handle.record.result, request
            if self.editor._derived_publication_blocked():
                self.editor.processing.set_status(
                    "speakers", "waiting",
                    "Voice matching is ready; waiting for the current edit to finish.",
                )
        self._update_actions()

    def _request_current(self, request: _Request) -> bool:
        return (
            self._document_available() and not self._paused
            and self.editor.session.source_token() == request.source_token
            and self.derived_work.current(request.key, request.generation)
        )

    def _publish(self, record: JobRecord, request: _Request) -> None:
        worker = self._workers.pop(record.id, None)
        if worker is None or not self._request_current(request):
            return
        self._publication = None
        if worker.outcome == "download":
            self._request_download(worker.manager)
        elif worker.outcome == "prepare":
            self._prepared_ranges.difference_update(
                _audio_range(clip) for clip in request.clips if clip.segment_id in worker.missing_ids
            )
            self._preprocess = True
            self._pending = True
        elif (
            worker.outcome == "completed" and record.state == "succeeded"
            and self.editor.project.auto_speaker_matching and not self._paused
        ):
            if request.preparing and isinstance(record.result, SpeakerPreparationResult):
                self._prepared_ranges.update(_audio_range(clip) for clip in request.clips)
                self.editor.processing.set_status(
                    "speaker-preparation", "ready", "Voice fingerprints ready.",
                )
                self._pending = True
            elif isinstance(record.result, SpeakerResult):
                if (
                    request.verifying
                    and not self.derived_work.publication_was_deferred(request.key)
                ):
                    self._apply(record.result, request)
                else:
                    # Publication may have waited behind a long modal/gesture. Check
                    # the original source identity again off-thread, not on the GUI.
                    verification_request = replace(request, verifying=True)
                    self._request = verification_request
                    self._publication = record.result, verification_request
                    self._enqueue(worker.manager, verification_request, record.result)
            else:
                self._failed("Speaker matching returned an invalid result.")
        elif worker.outcome == "canceled" or record.state == "cancelled":
            self.cancel()
        else:
            self._failed(
                worker._job_error or record.error or "The task stopped without returning a result.",
            )
        self._update_actions()
        if self._pending and not self._paused and not self._timer.isActive():
            self._timer.start(0 if request.preparing else 900)

    def _request_download(self, manager: SpeakerMatchingManager) -> None:
        source_token = self.editor.session.source_token()
        self._pending_consent = True
        self.editor.processing.set_status(
            "speaker-preparation", "consent",
            "Waiting for permission to download the local speaker model.",
        )

        def current() -> bool:
            return (
                self._current() and self.editor.session.source_token() == source_token
                and self.editor.project.auto_speaker_matching and not self._paused
            )

        def decided(accepted: bool) -> None:
            self._pending_consent = False
            self._consent_callback = None
            if self.editor.session.source_token() != source_token or self._closed:
                self._timer.start(900)
                return
            if accepted and current():
                self._allow_download = True
                self._timer.start(0)
            elif not self._paused:
                self._paused = True
                self.editor.processing.set_status(
                    "speaker-preparation", "cancelled", "Speaker model download declined.",
                )
            self._update_actions()

        self._consent_callback = decided
        self.editor.workspace.setup_consent.request(
            self.editor.session.id,
            {f"speaker-matching:{manager.manifest['model']['sha256']}": (
                f"Speaker-matching model (~{manager.model_download_bytes / 1024**2:.0f} MiB)"
            )},
            decided, current,
        )

    def _apply(self, result: SpeakerResult, request: _Request) -> None:
        if not self._request_current(request):
            return
        self._observe()
        _, references, _ = self._inputs()
        if self.editor.session.source_token() != request.source_token or references != request.references:
            self.editor.processing.set_status(
                "speakers", "queued", "Reference speakers changed. Rechecking with your latest names.",
            )
            self._pending = True
            return
        applied = []
        segments = {segment.id: segment for segment in self.editor.project.segments}
        self._applying = True
        try:
            for match in result.matches:
                segment = segments.get(match.segment_id)
                expected = request.targets.get(match.segment_id)
                if (
                    segment is None or expected is None
                    or (_SegmentState.capture(segment), self._versions.get(segment.id, 0)) != expected
                    or any(name.strip() for name in segment.characters)
                    or (segment.id == self.editor.selected_segment_id and self.editor.speakers_edit.hasFocus())
                ):
                    continue
                segment.characters = [match.character]
                segment.speaker_assignment = "automatic"
                applied.append(segment)
            if applied:
                self.editor._set_dirty(True, history_label="Auto-fill speakers")
                self._refresh_names(applied)
                self._undo = {
                    segment.id: (segment.characters[0], self._versions[segment.id])
                    for segment in applied
                }
        finally:
            self._applying = False
        message = f"Filled {len(applied)} speaker name(s)."
        self.editor.processing.set_status("speakers", "ready", message)
        self.editor.statusBar().showMessage(message, 5000)
        diagnostic_event("speaker_matching_applied", count=len(applied), examined=result.examined)

    def _refresh_names(self, segments: list[Segment]) -> None:
        by_id = {segment.id: segment for segment in segments}
        table = self.editor.segment_table
        # Walk the table once, rather than searching every row for every match.
        with QSignalBlocker(table):
            for row in range(table.rowCount()):
                identity = table.item(row, 0)
                segment = by_id.get(identity.data(Qt.ItemDataRole.UserRole)) if identity else None
                if segment is not None:
                    update_speaker_item(table.item(row, 3), segment)
        selected = by_id.get(self.editor.selected_segment_id)
        if selected is not None:
            text = ", ".join(selected.characters)
            if self.editor.speakers_edit.text() != text:
                with QSignalBlocker(self.editor.speakers_edit):
                    self.editor.speakers_edit.setText(text)
            self.editor._sync_speaker_exclusion()
        self.editor.video_widget.set_segments(self.editor.project.segments)
        self.editor.timeline.update()
        self.editor._refresh_validation_label(refresh_highlights=True)

    @Slot()
    def undo(self) -> None:
        self._observe()
        restored = []
        segments = {segment.id: segment for segment in self.editor.project.segments}
        self._applying = True
        try:
            for identity, (character, version) in self._undo.items():
                segment = segments.get(identity)
                if (
                    segment is not None and segment.speaker_assignment == "automatic"
                    and segment.characters == [character] and self._versions.get(identity) == version
                ):
                    segment.characters = []
                    segment.speaker_assignment = "excluded"
                    restored.append(segment)
            self._undo.clear()
            if restored:
                self.editor._set_dirty(True, history_label="Clear last speaker auto-fill")
                self._refresh_names(restored)
        finally:
            self._applying = False
        self.editor.statusBar().showMessage(f"Cleared {len(restored)} auto-filled name(s).", 5000)
        self._update_actions()

    @Slot()
    def cancel(self) -> None:
        self._paused = True
        self._history_paused = False
        self._resume_requested = False
        self._pending = False
        self._timer.stop()
        self._publication = None
        self._canceled_run = self.worker is not None
        self.derived_work.pause("speakers")
        self.derived_work.pause("speaker-preparation")
        self._generation = self.derived_work.generation("speakers")
        if self._consent_callback is not None:
            self.editor.workspace.setup_consent.cancel_request(self._consent_callback)
        if self._document_available():
            state, message = (
                ("cancelling", "Stopping speaker matching.") if self.worker is not None else
                ("cancelled", "Speaker matching paused.") if self.editor.project.auto_speaker_matching
                else ("off", "Automatic voice matching is off.")
            )
            for kind in ("speaker-preparation", "speakers"):
                self.editor.processing.set_status(kind, state, message)
        self._update_actions()

    @Slot(str, object)
    def _derived_cancelled(self, key: str, _record: JobRecord) -> None:
        if key in {"speakers", "speaker-preparation"}:
            self.cancel()

    def close_processing(self) -> None:
        self._closed = True
        self.cancel()

    def _update_actions(self) -> None:
        running = self.worker is not None or self._publication is not None
        self.editor.action_clear_speaker_autofill.setEnabled(
            bool(self._undo) and not running and self._current()
        )
