from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget


@dataclass
class _Request:
    project_id: str | None
    components: dict[str, str]
    callback: Callable[[bool], None]
    current: Callable[[], bool]


class SetupConsent:
    """One workspace prompt, with session consent keyed by component checksum/version."""

    def __init__(self, parent: QWidget) -> None:
        self.parent = parent
        self._approved: set[str] = set()
        self._requests: list[_Request] = []
        self.box: QMessageBox | None = None

    def request(
        self, project_id: str | None, components: Mapping[str, str],
        callback: Callable[[bool], None], current: Callable[[], bool],
    ) -> None:
        if not current():
            callback(False)
            return
        missing = {key: label for key, label in components.items() if key not in self._approved}
        if not missing:
            callback(True)
            return
        self._requests.append(_Request(project_id, missing, callback, current))
        if self.box is None:
            box = QMessageBox(
                QMessageBox.Icon.Question, "Download shared local components?", "",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self.parent,
            )
            box.setObjectName("sharedSetupConsent")
            box.setWindowModality(Qt.WindowModality.NonModal)
            box.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            box.finished.connect(self._finished)
            self.box = box
        self._refresh()
        self.box.show()

    def _refresh(self) -> None:
        components = {
            key: label for request in self._requests for key, label in request.components.items()
        }
        self.box.setText(
            "Projects are waiting for these checksum-verified local components:\n\n"
            + "\n".join(f"- {label}" for label in components.values())
            + "\n\nDownload once for all requesting projects? Files stay in this application's "
            "local data and are reused offline. No audio or transcripts are uploaded. "
            "Cancel keeps your projects and existing media unchanged."
        )

    def _finished(self, answer: int) -> None:
        requests, self._requests = self._requests, []
        self.box = None
        accepted = answer == QMessageBox.StandardButton.Yes
        if accepted:
            self._approved.update(key for request in requests for key in request.components)
        for request in requests:
            request.callback(accepted and request.current())

    def cancel_project(self, project_id: str) -> None:
        cancelled = [request for request in self._requests if request.project_id == project_id]
        self._requests = [request for request in self._requests if request.project_id != project_id]
        for request in cancelled:
            request.callback(False)
        if self.box is not None:
            if self._requests:
                self._refresh()
            else:
                self.box.reject()

    def cancel_all(self) -> None:
        if self.box is not None:
            self.box.reject()
