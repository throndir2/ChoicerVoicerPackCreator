"""Per-source processing state, compact status text, and on-demand details."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from html import escape

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
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
        self.publication_guard: Callable[[JobRecord], bool] | None = None
        self._voice_visible = False
        self._voice_timer = QTimer(self)
        self._voice_timer.setSingleShot(True)
        self._voice_timer.setInterval(250)
        self._voice_timer.timeout.connect(self._show_voice_activity)
        manager.changed.connect(self._job_changed)

    def reset(self) -> None:
        self._token = self.session.source_token()
        self._states.clear()
        self._latest.clear()
        self._voice_timer.stop()
        self._voice_visible = False
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
            if kind in GROUPS["voices"]:
                if not self._voice_active():
                    self._voice_timer.stop()
                    self._voice_visible = False
                elif not self._voice_visible and not self._voice_timer.isActive():
                    self._voice_timer.start()
            self.changed.emit()

    def _voice_active(self) -> bool:
        return any(
            self._states.get(kind, ProcessingState()).state in ACTIVE_STATES - {"consent"}
            for kind in GROUPS["voices"]
        )

    def _show_voice_activity(self) -> None:
        self._voice_visible = self._voice_active()
        self.changed.emit()

    def _job_changed(self, record: JobRecord) -> None:
        if (
            record.project_id != self.session.id or record.kind not in PROCESSING_KINDS
            or self.session.source_token() != self._token
            or record.source_snapshot.get("source_revision") != self.session.source_revision
            or self.publication_guard is not None and not self.publication_guard(record)
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

    def status_summary(self) -> str:
        active = attention = 0
        for group, kinds in GROUPS.items():
            states = {self._states[kind].state for kind in kinds if kind in self._states}
            active += bool(states & (ACTIVE_STATES - {"consent"})) and (
                group != "voices" or self._voice_visible
            )
            attention += bool(states & {"consent", "failed"})
        parts = []
        if active:
            parts.append(f"{active} active")
        if attention:
            parts.append(f"{attention} {'needs' if attention == 1 else 'need'} attention")
        return "Background: " + ", ".join(parts) if parts else ""

    def has_active_work(self) -> bool:
        return any(
            value.state in ACTIVE_STATES - {"consent"}
            and (kind not in GROUPS["voices"] or self._voice_visible)
            for kind, value in self._states.items()
        )


class ProcessingStatus(QLabel):
    def __init__(self, model: ProcessingModel, action: QAction, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("processingStatus")
        self.model = model
        self._validation = "No segments"
        self._validation_details = ""
        self._color = "#7f91a8"
        self._issue_count = 0
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setContentsMargins(0, 0, 6, 0)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.linkActivated.connect(lambda _link: action.trigger())
        model.changed.connect(self.refresh)
        self.refresh()

    def set_validation(self, summary: str, details: str, color: str) -> None:
        self._validation, self._validation_details, self._color = summary, details, color
        self.refresh()

    def set_issue_count(self, count: int) -> None:
        self._issue_count = count
        self.refresh()

    def refresh(self) -> None:
        parts = [self._validation]
        background = self.model.status_summary()
        if background:
            parts.append(background)
        if self._issue_count:
            parts.append(f"{self._issue_count} notice(s) need attention")
        summary = " · ".join(parts)
        self.setAccessibleName(summary)
        self.setToolTip(
            summary + "\n\n" + self._validation_details
            + "\n\nOpen project status for readiness, progress, and notice details."
        )
        self._elide()

    def _elide(self) -> None:
        summary = self.accessibleName()
        color = (
            "#ffad7a" if self._issue_count or "attention" in self.model.status_summary()
            else self._color
        )
        text = self.fontMetrics().elidedText(
            summary, Qt.TextElideMode.ElideMiddle, max(0, self.contentsRect().width() - 4),
        )
        self.setText(f'<a href="details" style="color: {color};">{escape(text)}</a>')

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()


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


class ProcessingDialog(QDialog):
    action_requested = Signal(str, str)
    acknowledge_requested = Signal()

    def __init__(self, model: ProcessingModel, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("processingDialog")
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.resize(740, 540)
        self.model = model
        layout = QVBoxLayout(self)
        self.rows: dict[str, tuple[QLabel, _StatusLabel, QProgressBar, QPushButton]] = {}
        for group, title, action in (
            ("transcript", "Transcript", "Review"),
            ("voices", "Speaker matching", "Speakers"),
            ("backing", "Backing track", "Details"),
        ):
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
            open_button.setAutoDefault(False)
            open_button.setObjectName(f"{group}ProcessingOpen")
            open_button.clicked.connect(
                lambda _checked=False, group=group: self.action_requested.emit(group, "open")
            )
            control = QPushButton("Start")
            control.setAutoDefault(False)
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
            layout.addWidget(card)
            self.rows[group] = state, message, progress, control
        self.project_details = QPlainTextEdit(self)
        self.project_details.setReadOnly(True)
        self.project_details.setObjectName("projectStatusDetails")
        self.project_details.setAccessibleName("Project readiness and notice details")
        layout.addWidget(self.project_details, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.acknowledge_button = buttons.addButton(
            "Acknowledge notices", QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.acknowledge_button.setEnabled(False)
        self.acknowledge_button.setToolTip(
            "Dismiss reported notices. Export requirements and background failures remain visible."
        )
        self.acknowledge_button.clicked.connect(self.acknowledge_requested)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setToolTip(
            "Transcript work takes priority over queued backing generation. "
            "Voice preparation and cached matching can run alongside either task "
            "without waiting for CPU capacity. "
            "You can keep editing, playing video, and switching tabs."
        )
        model.changed.connect(self.refresh)
        self.refresh()

    def show_processing(self) -> None:
        self.setWindowTitle(f"Project status - {self.model.session.project.title}")
        self.show()
        self.raise_()
        self.activateWindow()

    def set_project_details(self, text: str, *, has_notices: bool) -> None:
        if self.project_details.toPlainText() != text:
            scrollbar = self.project_details.verticalScrollBar()
            position = scrollbar.value()
            self.project_details.setPlainText(text)
            scrollbar.setValue(position)
        self.acknowledge_button.setEnabled(has_notices)

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
            control.setText(
                "Cancel" if active else
                "Resume" if group == "voices" and value.state == "cancelled" else
                "Start" if value.state in {"idle", "off"} or group == "voices" and value.state == "ready"
                else "Retry"
            )
            control.setEnabled(value.state != "cancelling" and (group == "voices" or value.state != "ready"))
