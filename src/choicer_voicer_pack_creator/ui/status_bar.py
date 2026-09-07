"""One-line project activity, short-lived notices, and retained issue details."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QProgressBar, QStatusBar, QWidget


class ProjectStatusBar(QStatusBar):
    details_changed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(False)
        self._activities: dict[str, str] = {}
        self._issues: dict[str, tuple[str, str]] = {}
        self._details: dict[str, str] = {}
        self._notice = ""
        self._notice_details = ""
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.clearMessage)
        self._readiness: QWidget | None = None
        self.progress = QProgressBar(self)
        self.progress.setObjectName("statusProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(80)
        self.progress.setFixedHeight(6)
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.addPermanentWidget(self.progress)

    def set_readiness_widget(self, widget: QWidget) -> None:
        self._readiness = widget
        self.addPermanentWidget(widget, 1)
        widget.setMaximumWidth(self.width() // 2)

    def resizeEvent(self, event) -> None:  # noqa: N802
        if self._readiness is not None:
            self._readiness.setMaximumWidth(self.width() // 2)
        super().resizeEvent(event)

    def showMessage(  # noqa: N802
        self, message: str, timeout: int = 5000, *, details: str = "",
    ) -> None:
        self._notice = message
        self._notice_details = details or message
        if message:
            self._details["latest"] = f"Latest notice: {self._notice_details}"
        self._notice_timer.stop()
        if message and timeout > 0:
            self._notice_timer.start(timeout)
        self._refresh()

    def clearMessage(self) -> None:  # noqa: N802
        self._notice_timer.stop()
        self._notice = ""
        self._notice_details = ""
        self._refresh()

    def set_activity(self, key: str, message: str) -> None:
        self._activities[key] = message
        self._refresh()

    def clear_activity(self, key: str) -> None:
        self._activities.pop(key, None)
        self._refresh()

    def set_issue(self, key: str, message: str, *, details: str = "") -> None:
        self._issues[key] = (message, details or message)
        self._refresh()

    def clear_issue(self, key: str) -> None:
        if self._issues.pop(key, None) is not None:
            self._refresh()

    def acknowledge_issues(self) -> None:
        self._issues.clear()
        self._refresh()

    @property
    def issue_count(self) -> int:
        return len(self._issues)

    def set_detail(self, key: str, message: str) -> None:
        self._details[key] = message
        self.details_changed.emit()

    def details_text(self) -> str:
        sections = list(self._details.values())
        if self._activities:
            sections.append("Current activity:\n" + "\n".join(self._activities.values()))
        if self._issues:
            sections.append(
                "Notices needing attention:\n"
                + "\n".join(
                    message if message == details else f"{message}: {details}"
                    for message, details in self._issues.values()
                )
            )
        return "\n\n".join(sections)

    def _refresh(self) -> None:
        # A confirmation must not replace ongoing work or erase an unresolved issue.
        if self._activities:
            message = next(reversed(self._activities.values()))
            details = "\n".join(self._activities.values())
        elif self._issues:
            message, details = next(reversed(self._issues.values()))
        else:
            message, details = self._notice, self._notice_details
        super().showMessage(message)
        self.setToolTip(details)
        self.progress.setVisible(bool(self._activities))
        self.details_changed.emit()
