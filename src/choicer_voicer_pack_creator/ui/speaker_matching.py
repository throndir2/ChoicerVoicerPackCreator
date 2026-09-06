from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.operations import SourceChangedError
from choicer_voicer_pack_creator.speaker_matching import (
    MIN_ACTIVE_SECONDS,
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
    targets: dict[str, tuple[_SegmentState, int]]
    clips: tuple[SpeakerClip, ...]
    preparing: bool = False


def _audio_range(clip: SpeakerClip) -> tuple[str, float, float | None]:
    return clip.path, clip.start, clip.end


class SpeakerWorker(JobWorker):
    progress = Signal(str, int)
    completed = Signal(object)
    failed = Signal(str)
    download_required = Signal()
    canceled = Signal()
    preparation_required = Signal()

    def __init__(self, manager, media, clips, *, allow_download: bool, preparing: bool) -> None:
        super().__init__()
        self.manager = manager
        self.media = media
        self.clips = clips
        self.allow_download = allow_download
        self.preparing = preparing

    def run(self) -> None:
        def report(message: str, fraction: float | None) -> None:
            self.progress.emit(
                message, -1 if fraction is None else max(0, min(1000, round(fraction * 1000))),
            )

        try:
            if self.preparing:
                result = self.manager.prepare(
                    self.media, self.clips, allow_download=self.allow_download,
                    progress=report, cancelled=self.isInterruptionRequested,
                )
            else:
                result = self.manager.match_cached(
                    self.media, self.clips, progress=report, cancelled=self.isInterruptionRequested,
                )
            self.completed.emit(result)
        except SpeakerPreparationRequired:
            self.preparation_required.emit()
        except SpeakerDownloadRequired:
            self.download_required.emit()
        except SpeakerMatchingCancelled:
            self.canceled.emit()
        except Exception as error:
            diagnostic_exception("speaker_matching_failed", error)
            self.failed.emit(str(error))


class SpeakerMatchingControls(QWidget):
    """Per-document scheduling and optimistic publication; never lock the editor."""

    def __init__(self, editor: ProjectEditor) -> None:
        super().__init__(editor)
        self.editor = editor
        self.worker: SpeakerWorker | None = None
        self._generation = 0
        self._source_token = editor.session.source_token()
        self._observed: dict[str, _SegmentState] = {}
        self._versions: dict[str, int] = {}
        self._request: _Request | None = None
        self._result: SpeakerResult | SpeakerPreparationResult | None = None
        self._outcome = ""
        self._activated = False
        self._preprocess = False
        self._prepared_ranges: set[tuple[str, float, float | None]] = set()
        self._typing = False
        self._paused = False
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
        self._publication_timer = QTimer(self)
        self._publication_timer.setSingleShot(True)
        self._publication_timer.setInterval(100)
        self._publication_timer.timeout.connect(self._publish_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.enabled_check = QCheckBox("Auto-fill matching speakers in the background")
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
        buttons = QHBoxLayout()
        self.match_button = QPushButton("Match now")
        self.match_button.setObjectName("matchSpeakers")
        self.match_button.clicked.connect(self.retry)
        self.undo_button = QPushButton("Undo auto-fill")
        self.undo_button.setObjectName("undoSpeakerMatching")
        self.undo_button.setToolTip(
            "Clear unchanged names from the last automatic batch and keep those segments "
            "unassigned. Your subsequent edits are preserved."
        )
        self.undo_button.clicked.connect(self.undo)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelSpeakerMatching")
        self.cancel_button.clicked.connect(self.cancel)
        for button in (self.match_button, self.undo_button, self.cancel_button):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.status = QLabel("Finish naming a dialogue segment to match its voice.")
        self.status.setObjectName("speakerMatchingStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._update_buttons()

    def _current(self) -> bool:
        workspace = self.editor.workspace
        return (
            self._document_available() and not workspace._closing
            and not self.editor.session.loading
        )

    def _document_available(self) -> bool:
        return (
            not self._closed
            and self.editor.session.id not in self.editor.workspace._closed_ids
        )

    def _observe(self) -> bool:
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
            self._generation += 1
            self._timer.stop()
            self._publication_timer.stop()
            self._publication = None
            if self.worker is not None:
                self.worker.requestInterruption()
            self._activated = False
            self._preprocess = False
            self._prepared_ranges.clear()
            self._typing = False
            self._paused = False
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

    def changed(self) -> None:
        changed = self._observe()
        self._update_buttons()
        if not changed or self._applying:
            return
        if self.worker is not None:
            self._pending = True
        if (self._activated or self._preprocess) and not self._paused:
            self._timer.start(900)

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
        segment.speaker_assignment = (
            "manual" if any(name.strip() for name in segment.characters) else "excluded"
        )
        self._typing = True
        if self._preprocess:
            self._timer.start(900)
        else:
            self._timer.stop()

    def name_committed(self) -> None:
        self._typing = False
        self._activated = True
        self._preprocess = True
        self._observe()
        if not self._paused:
            self._pending = self.worker is not None
            self._timer.start(0)

    @Slot(bool)
    def _enabled_changed(self, enabled: bool) -> None:
        self.editor.project.auto_speaker_matching = enabled
        self.editor._set_dirty(True)
        if enabled:
            self.retry()
        else:
            self.cancel()
            self.status.setText("Automatic speaker matching is off.")
            for kind in ("speaker-preparation", "speakers"):
                self.editor.processing.set_status(kind, "off", "Automatic voice matching is off.")

    @Slot()
    def retry(self) -> None:
        if not self._current():
            return
        if self.worker is not None:
            if self._paused:
                self._resume_requested = True
                self.status.setText("Waiting for cancellation to finish, then restarting matching.")
            return
        if not self.editor.project.auto_speaker_matching:
            self.enabled_check.setChecked(True)
            return
        self._paused = False
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
            self.status.setText(
                f"Name a dialogue segment with at least {MIN_ACTIVE_SECONDS:g} seconds of speech; "
                "automatic names are not used as references."
                if not references else "No eligible unassigned segments to match."
            )
            if self._preprocess:
                self.editor.processing.set_status(
                    "speaker-preparation", "ready" if self._prepared_ranges else "waiting",
                    "Voice fingerprints ready. " + self.status.text()
                    if self._prepared_ranges else self.status.text(),
                )
            return
        if preparing:
            clips = preparation
        self._request = _Request(
            self._generation, self.editor.session.source_token(), references, targets, clips, preparing,
        )
        self._pending = False
        self._result = None
        self._outcome = ""
        self._canceled_run = False
        try:
            manager = SpeakerMatchingManager(self.editor.analysis_data_root)
        except (OSError, RuntimeError, ValueError) as error:
            diagnostic_exception("speaker_matching_setup_failed", error)
            self._failed(f"Speaker matching could not start: {error}")
            return
        worker = SpeakerWorker(
            manager, self.editor.media, clips, allow_download=self._allow_download, preparing=preparing,
        )
        self.worker = worker
        worker.configure_job(
            self.editor.workspace.job_manager, self.editor.session.id,
            "speaker-preparation" if preparing else "speakers",
            "Prepare voice fingerprints" if preparing else "Match cached voices",
            resource_class="cpu" if preparing else "io",
            read_paths=tuple({Path(clip.path) for clip in clips}),
            resource_keys=("speaker-matching-inference",) if preparing else (),
            source_snapshot={"source_revision": self.editor.session.source_revision},
            priority=10 if preparing else 20,
        )
        worker.progress.connect(self._progress)
        worker.completed.connect(self._completed)
        worker.failed.connect(self._failed)
        worker.download_required.connect(lambda: setattr(self, "_outcome", "download"))
        worker.canceled.connect(lambda: setattr(self, "_outcome", "canceled"))
        worker.preparation_required.connect(lambda: setattr(self, "_outcome", "prepare"))
        worker.finished.connect(self._finished)
        worker.finished.connect(worker.deleteLater)
        self.status.setText(
            "Voice preparation queued. Names are not needed; you can keep editing."
            if preparing else "Comparing cached voices. You can keep editing."
        )
        worker.start()
        tasks = self.editor.workspace.tasks_window
        job_id = worker.job_handle.id
        tasks.register_retry(
            job_id, self.retry, available=lambda: self._current() and self.worker is None,
        )
        self.destroyed.connect(lambda: tasks.unregister_retry(job_id))
        self._update_buttons()

    @Slot(str, int)
    def _progress(self, message: str, _value: int) -> None:
        if self._request is not None and self._request.generation == self._generation:
            self.status.setText(message)

    @Slot(object)
    def _completed(self, result: object) -> None:
        if not isinstance(result, (SpeakerResult, SpeakerPreparationResult)):
            self._failed("Speaker matching returned an invalid result.")
            return
        self._result = result
        self._outcome = "completed"

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self._request is not None and self._request.generation != self._generation:
            return
        self._outcome = "failed"
        self._paused = True
        self.status.setText(f"Speaker matching failed: {message}\nUse Match now to retry.")
        self.editor.processing.set_status("speaker-preparation", "failed", message)

    @Slot()
    def _finished(self) -> None:
        worker, self.worker = self.worker, None
        request = self._request
        if (
            request is None or request.generation != self._generation
            or not self._document_available()
        ):
            self._update_buttons()
            if self._document_available() and self._preprocess and not self._paused:
                self._timer.start(0)
            return
        if self._canceled_run:
            resume = self._resume_requested and self.editor.project.auto_speaker_matching
            self._resume_requested = False
            self._paused = not resume
            self._pending = resume
            self.status.setText(
                "Restarting speaker matching with the current edits." if resume else
                "Speaker matching paused. Use Match now to resume."
            )
        elif self._outcome == "download":
            self._request_download(worker.manager)
        elif self._outcome == "prepare":
            self._prepared_ranges.difference_update(_audio_range(clip) for clip in request.clips)
            self._preprocess = True
            self._pending = True
        elif (
            self._outcome == "completed" and self._result is not None
            and worker.job_handle.record.state == "succeeded"
            and self.editor.project.auto_speaker_matching and not self._paused
        ):
            if request.preparing and isinstance(self._result, SpeakerPreparationResult):
                self._prepared_ranges.update(_audio_range(clip) for clip in request.clips)
                self.status.setText("Voice fingerprints ready. Name a dialogue segment to match its voice.")
                self.editor.processing.set_status(
                    "speaker-preparation", "ready", self.status.text(),
                )
                self._pending = True
            elif isinstance(self._result, SpeakerResult):
                self._apply(self._result, request)
        elif self._outcome == "canceled" or worker.job_handle.record.state == "cancelled":
            self._paused = True
            self.status.setText("Speaker matching paused. Use Match now to resume.")
            self.editor.processing.set_status("speaker-preparation", "cancelled", self.status.text())
        elif self._outcome != "failed":
            self._failed("The task stopped without returning a result.")
        self._update_buttons()
        if self._pending and not self._paused:
            self._timer.start(0 if request.preparing else 900)

    def _request_download(self, manager: SpeakerMatchingManager) -> None:
        generation = self._generation
        self._pending_consent = True
        self.status.setText("Waiting for permission to download the local speaker model.")
        self.editor.processing.set_status("speaker-preparation", "consent", self.status.text())

        def current() -> bool:
            return (
                self._current() and self._generation == generation
                and self.editor.project.auto_speaker_matching and not self._paused
            )

        def decided(accepted: bool) -> None:
            self._pending_consent = False
            self._consent_callback = None
            if generation != self._generation or self._closed:
                self._timer.start(900)
                return
            if accepted and current():
                self._allow_download = True
                self._timer.start(0)
            elif not self._paused:
                self._paused = True
                self.status.setText("Speaker model download declined. Use Match now to retry.")
                self.editor.processing.set_status("speaker-preparation", "cancelled", self.status.text())
            self._update_buttons()

        self._consent_callback = decided
        self.editor.workspace.setup_consent.request(
            self.editor.session.id,
            {f"speaker-matching:{manager.manifest['model']['sha256']}": (
                f"Speaker-matching model (~{manager.model_download_bytes / 1024**2:.0f} MiB)"
            )},
            decided, current,
        )

    def _apply(self, result: SpeakerResult, request: _Request) -> None:
        # A modal range/dirty decision may restore an earlier dirty flag when it closes.
        if (
            not self._current() or QApplication.activeModalWidget() is not None
            or self.editor._range_edit_record is not None
        ):
            self._publication = result, request
            self._publication_timer.start()
            self.status.setText("Voice matching is ready; waiting for the current edit to finish.")
            return
        self._observe()
        _, references, _ = self._inputs()
        if self.editor.session.source_token() != request.source_token or references != request.references:
            self.status.setText("Reference speakers changed. Rechecking with your latest names.")
            self._pending = True
            return
        try:
            result.sources.verify()
        except SourceChangedError as error:
            diagnostic_exception("speaker_matching_source_changed", error)
            self._paused = True
            self.status.setText("Source audio changed. Use Match now to analyze the new audio.")
            return
        applied = []
        self._applying = True
        try:
            for match in result.matches:
                segment = self.editor.project.segment_by_id(match.segment_id)
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
                self.editor._set_dirty(True)
                self._refresh_names(applied)
                self._undo = {
                    segment.id: (segment.characters[0], self._versions[segment.id])
                    for segment in applied
                }
        finally:
            self._applying = False
        self.status.setText(
            f"Filled {len(applied)} speaker name(s). Uncertain/short clips stay unassigned. "
            "Automatic names are not used as voice references."
            + (
                " For a stronger reference, name a longer, clean dialogue line "
                "(about 2 seconds or more)."
                if not applied else ""
            )
        )
        diagnostic_event("speaker_matching_applied", count=len(applied), examined=result.examined)

    @Slot()
    def _publish_ready(self) -> None:
        publication, self._publication = self._publication, None
        if publication is None:
            return
        result, request = publication
        if (
            not self._document_available() or self._paused
            or not self.editor.project.auto_speaker_matching
            or request.generation != self._generation
        ):
            self._update_buttons()
            return
        self._apply(result, request)
        self._update_buttons()
        if self._pending and self._publication is None and not self._paused and not self._typing:
            self._timer.start(900)

    def _refresh_names(self, segments: list[Segment]) -> None:
        for segment in segments:
            row = self.editor._row_for_segment(segment.id)
            if row >= 0:
                update_speaker_item(self.editor.segment_table.item(row, 3), segment)
            if segment.id == self.editor.selected_segment_id:
                with QSignalBlocker(self.editor.speakers_edit):
                    self.editor.speakers_edit.setText(", ".join(segment.characters))
                self.editor._sync_speaker_exclusion()
        self.editor.video_widget.set_segments(self.editor.project.segments)
        self.editor.timeline.update()
        self.editor._refresh_validation_label()

    @Slot()
    def undo(self) -> None:
        self._observe()
        restored = []
        self._applying = True
        try:
            for identity, (character, version) in self._undo.items():
                segment = self.editor.project.segment_by_id(identity)
                if (
                    segment is not None and segment.speaker_assignment == "automatic"
                    and segment.characters == [character] and self._versions.get(identity) == version
                ):
                    segment.characters = []
                    segment.speaker_assignment = "excluded"
                    restored.append(segment)
            self._undo.clear()
            if restored:
                self.editor._set_dirty(True)
                self._refresh_names(restored)
        finally:
            self._applying = False
        self.status.setText(
            f"Undid {len(restored)} automatic name(s); later edits were preserved. "
            "Uncheck Keep unassigned on a segment to include it again."
        )
        self._update_buttons()

    @Slot()
    def cancel(self) -> None:
        self._paused = True
        self._resume_requested = False
        self._pending = False
        self._timer.stop()
        self._publication_timer.stop()
        self._publication = None
        if self.worker is not None:
            self._canceled_run = True
            self.worker.requestInterruption()
        if self._consent_callback is not None:
            self.editor.workspace.setup_consent.cancel_request(self._consent_callback)
        self.status.setText("Speaker matching paused. Use Match now to resume.")
        if self._document_available():
            for kind in ("speaker-preparation", "speakers"):
                self.editor.processing.set_status(kind, "cancelled", self.status.text())
        self._update_buttons()

    def close_processing(self) -> None:
        self._closed = True
        self.cancel()

    def _update_buttons(self) -> None:
        running = self.worker is not None or self._publication is not None
        self.match_button.setEnabled(not running and not self._pending_consent)
        self.cancel_button.setEnabled(running or self._pending_consent or self._timer.isActive())
        self.undo_button.setEnabled(bool(self._undo) and not running)
