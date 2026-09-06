"""Per-source processing state, shared by the inline overview and job detail surfaces."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.jobs import JobManager, JobRecord
from choicer_voicer_pack_creator.project_session import ProjectSession

PROCESSING_KINDS = frozenset({"analysis", "refinement", "speaker-preparation", "speakers", "backing"})
GROUPS = {
    "transcript": ("analysis", "refinement"),
    "voices": ("speaker-preparation", "speakers"),
    "backing": ("backing",),
}
ACTIVE_STATES = frozenset({"queued", "waiting", "running", "cancelling", "consent"})
STATE_LABELS = {
    "idle": "Not started", "queued": "Queued", "waiting": "Waiting",
    "running": "Working", "cancelling": "Stopping", "consent": "Needs permission",
    "ready": "Ready", "failed": "Failed", "cancelled": "Paused", "off": "Off",
}


@dataclass(frozen=True)
class ProcessingState:
    state: str = "idle"
    message: str = "Not started."
    fraction: float | None = None


class ProcessingModel(QObject):
    changed = Signal()

    def __init__(self, manager: JobManager, session: ProjectSession, parent: QObject) -> None:
        super().__init__(parent)
        self.session = session
        self._token = session.source_token()
        self._states: dict[str, ProcessingState] = {}
        self._latest: dict[str, str] = {}
        manager.changed.connect(self._job_changed)

    def reset(self) -> None:
        self._token = self.session.source_token()
        self._states.clear()
        self._latest.clear()
        if self.session.project.analysis_review:
            self.set_status("analysis", "ready", "Saved transcript drafts available for review.")
        if self.session.project.backing_track_path:
            self.set_status("backing", "ready", "Using the project's selected backing track.")
        if not self.session.project.auto_speaker_matching:
            self.set_status("speaker-preparation", "off", "Automatic voice matching is off.")
        self.changed.emit()

    def set_status(
        self, kind: str, state: str, message: str, fraction: float | None = None,
    ) -> None:
        if kind not in PROCESSING_KINDS or state not in STATE_LABELS:
            raise ValueError(f"Invalid processing status: {kind}/{state}")
        value = ProcessingState(state, message, fraction)
        if self._states.get(kind) != value:
            self._states[kind] = value
            self.changed.emit()

    def _job_changed(self, record: JobRecord) -> None:
        if (
            record.project_id != self.session.id or record.kind not in PROCESSING_KINDS
            or self.session.source_token() != self._token
            or record.source_snapshot.get("source_revision") != self.session.source_revision
        ):
            return
        if record.state == "queued":
            self._latest[record.kind] = record.id
        if self._latest.get(record.kind) != record.id:
            return
        state = {"succeeded": "ready", "blocked": "failed"}.get(record.state, record.state)
        message = record.error or record.message
        if state == "ready":
            message = {
                "analysis": "Transcript draft ready. Review it before adding segments.",
                "refinement": "YouTube draft ready. Review it before adding segments.",
                "speaker-preparation": "Voice fingerprints ready; name a line to match its voice.",
                "speakers": "Matching complete. Review automatic names.",
                "backing": "Backing generated. Listen before exporting.",
            }[record.kind]
        self.set_status(record.kind, state, message, record.fraction)

    def group_state(self, group: str) -> ProcessingState:
        states = [self._states[kind] for kind in GROUPS[group] if kind in self._states]
        if not states:
            return ProcessingState()
        order = [
            "cancelling", "running", "consent", "queued", "waiting",
            "failed", "cancelled", "ready", "off", "idle",
        ]
        primary = min(states, key=lambda value: order.index(value.state))
        return ProcessingState(
            primary.state, "\n".join(value.message for value in states), primary.fraction,
        )


class _StatusLabel(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self._message = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_message(self, message: str) -> None:
        self._message = message.replace("\n", " | ")
        self.setToolTip(message)
        self._elide()

    def _elide(self) -> None:
        self.setText(self.fontMetrics().elidedText(
            self._message, Qt.TextElideMode.ElideRight, self.contentsRect().width(),
        ))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()


class ProcessingPanel(QGroupBox):
    action_requested = Signal(str, str)

    def __init__(self, model: ProcessingModel, parent: QWidget) -> None:
        super().__init__("Background processing", parent)
        self.setObjectName("videoProcessing")
        self.model = model
        layout = QGridLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setHorizontalSpacing(18)
        self.rows: dict[str, tuple[QLabel, _StatusLabel, QProgressBar, QPushButton]] = {}
        for column, (group, title, action) in enumerate((
            ("transcript", "Transcript", "Review"),
            ("voices", "Voice fingerprints", "Speakers"),
            ("backing", "Backing track", "Details"),
        )):
            card = QWidget(self)
            content = QVBoxLayout(card)
            content.setContentsMargins(0, 0, 0, 0)
            content.setSpacing(3)
            header = QHBoxLayout()
            header.addWidget(QLabel(title))
            header.addStretch()
            state = QLabel()
            state.setObjectName(f"{group}ProcessingState")
            header.addWidget(state)
            open_button = QPushButton(action)
            open_button.setObjectName(f"{group}ProcessingOpen")
            open_button.clicked.connect(
                lambda _checked=False, group=group: self.action_requested.emit(group, "open")
            )
            control = QPushButton("Start")
            control.setObjectName(f"{group}ProcessingControl")
            control.clicked.connect(
                lambda _checked=False, group=group: self.action_requested.emit(
                    group, "cancel" if self.model.group_state(group).state in ACTIVE_STATES else "retry",
                )
            )
            content.addLayout(header)
            message = _StatusLabel()
            message.setObjectName(f"{group}ProcessingMessage")
            details = QHBoxLayout()
            details.addWidget(message, 1)
            details.addWidget(open_button)
            details.addWidget(control)
            content.addLayout(details)
            progress = QProgressBar()
            progress.setTextVisible(False)
            progress.setFixedHeight(4)
            progress.setObjectName(f"{group}ProcessingProgress")
            content.addWidget(progress)
            layout.addWidget(card, 0, column)
            layout.setColumnStretch(column, 1)
            self.rows[group] = state, message, progress, control
        self.setToolTip(
            "Transcript and voice preparation take priority over queued backing generation. "
            "One CPU-heavy task runs at a time; cached voice comparisons can overlap it. "
            "You can keep editing, playing video, and switching tabs."
        )
        model.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        for group, (label, message, progress, control) in self.rows.items():
            value = self.model.group_state(group)
            label.setText(STATE_LABELS[value.state])
            message.set_message(value.message)
            active = value.state in ACTIVE_STATES
            progress.setRange(0, 0 if value.state == "running" and value.fraction is None else 1000)
            progress.setValue(
                round(value.fraction * 1000) if value.fraction is not None
                else 1000 if value.state == "ready" else 0
            )
            control.setText("Cancel" if active else "Start" if value.state in {"idle", "off"} else "Retry")
            control.setEnabled(value.state not in {"ready", "cancelling"})
