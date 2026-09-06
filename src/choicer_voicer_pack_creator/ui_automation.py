"""Opt-in, application-local UI observation and real Qt input; no evaluation API."""
from __future__ import annotations

from collections import deque
from typing import Any, Literal, Protocol
from uuid import uuid4

from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QTableWidget,
    QTabWidget,
    QWidget,
)

SELECTORS = frozenset({
    "projectTabs", "projectTitle", "segmentCaption", "segmentsTable",
    "saveProject", "exportProject", "analyzeProject",
    "tasksDock", "taskProjectFilter", "tasksTable", "taskLog",
    "taskShowProject", "taskCancel", "taskRetry", "taskOpenOutput", "taskDetails",
})
KEYS = {
    "Enter": Qt.Key.Key_Return, "Escape": Qt.Key.Key_Escape,
    "Tab": Qt.Key.Key_Tab, "Backspace": Qt.Key.Key_Backspace,
    "Space": Qt.Key.Key_Space, "Delete": Qt.Key.Key_Delete,
}


class Bridge(Protocol):
    window: Any

    def call(self, function): ...


class UIAutomation:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self._actions: deque[dict[str, Any]] = deque(maxlen=40)

    def _target(self, selector: str, project_id: str | None) -> QObject:
        if selector not in SELECTORS:
            raise ValueError(f"Selector is not allowlisted: {selector}")
        window = self.bridge.window
        editor = window.editor_for_project(project_id) if project_id else window.active_editor
        if project_id and editor is not window.active_editor:
            raise ValueError("Project is not the visible tab. Select it through projectTabs first.")
        roots = [editor, window] if editor is not None else [window]
        for root in roots:
            target = root.findChild(QObject, selector)
            if target is not None:
                return target
        raise ValueError(f"Widget is unavailable: {selector}")

    def _belongs(self, widget: QWidget) -> bool:
        owner: QObject | None = widget
        while owner is not None:
            if owner is self.bridge.window:
                return True
            owner = owner.parent()
        return False

    @staticmethod
    def _describe(target: QObject) -> dict[str, Any]:
        result: dict[str, Any] = {"selector": target.objectName()}
        if isinstance(target, QWidget):
            result.update(
                enabled=target.isEnabled(), visible=target.isVisible(), focused=target.hasFocus(),
                accessible_name=target.accessibleName(),
            )
        elif isinstance(target, QAction):
            result.update(enabled=target.isEnabled(), visible=target.isVisible(), text=target.text())
        if isinstance(target, QLineEdit):
            result["text"] = target.text()
        elif isinstance(target, QPlainTextEdit):
            result["text"] = target.toPlainText()[:10000]
        elif isinstance(target, QTableWidget):
            result.update(rows=target.rowCount(), selected_rows=sorted({
                index.row() for index in target.selectedIndexes()
            }))
        elif isinstance(target, QTabWidget):
            result.update(index=target.currentIndex(), tabs=[
                {"index": index, "text": target.tabText(index),
                 "project_id": target.widget(index).session.id}
                for index in range(target.count())
            ])
        elif isinstance(target, QComboBox):
            result.update(index=target.currentIndex(), text=target.currentText())
        return result

    def state(self) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            window = self.bridge.window
            editor = window.active_editor
            widgets = []
            for selector in sorted(SELECTORS):
                roots = [editor, window] if editor is not None else [window]
                target = next(
                    (item for root in roots
                     if (item := root.findChild(QObject, selector)) is not None), None
                )
                if target is not None:
                    widgets.append(self._describe(target))
            focus = QApplication.focusWidget()
            return {
                "platform": QApplication.platformName(),
                "visible": window.isVisible(),
                "active_project_id": editor.session.id if editor else None,
                "focus_selector": focus.objectName() if focus and self._belongs(focus) else None,
                "widgets": widgets,
                "windows": [
                    {"title": item.windowTitle(), "modal": item.isModal(),
                     "visible": item.isVisible(), "object_name": item.objectName()}
                    for item in QApplication.topLevelWidgets() if self._belongs(item)
                ],
                "actions": list(self._actions),
            }
        return self.bridge.call(read)

    def screenshot(self) -> bytes:
        def capture() -> bytes:
            window = self.bridge.window
            if not window.isVisible() or window.isMinimized():
                raise ValueError("The application window must be visible and not minimized.")
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not window.grab().save(buffer, "PNG"):
                raise RuntimeError("Unable to capture the rendered application window.")
            return bytes(buffer.data())
        return self.bridge.call(capture)

    def interact(
        self, selector: str, action: Literal["click", "type", "key", "select"],
        project_id: str | None = None, text: str | None = None,
        index: int | None = None, key: str | None = None,
    ) -> dict[str, str]:
        def enqueue() -> dict[str, str]:
            target = self._target(selector, project_id)
            if not isinstance(target, (QWidget, QAction)) or not target.isEnabled():
                raise ValueError("Target is not enabled for input.")
            if not target.isVisible():
                raise ValueError("Target is not visible.")
            modal = QApplication.activeModalWidget()
            if modal is not None and (
                not isinstance(target, QWidget)
                or (target is not modal and not modal.isAncestorOf(target))
            ):
                raise ValueError("A modal window blocks this target; dismiss it in the editor.")
            if action == "type":
                if not isinstance(target, (QLineEdit, QPlainTextEdit)) or target.isReadOnly():
                    raise ValueError("Typing is allowed only in editable title/caption fields.")
                if text is None or len(text) > 10000:
                    raise ValueError("Provide text of at most 10000 characters.")
            elif action == "key":
                if not isinstance(target, QWidget) or key not in KEYS:
                    raise ValueError(f"Allowed keys: {', '.join(KEYS)}")
            elif action == "select":
                if not isinstance(target, (QTabWidget, QTableWidget, QComboBox)):
                    raise ValueError("Selection requires tabs, a table, or a combobox.")
                count = target.rowCount() if isinstance(target, QTableWidget) else target.count()
                if index is None or not 0 <= index < count:
                    raise ValueError("Selection index is outside the available items.")
            elif action != "click":
                raise ValueError("Unsupported UI action.")
            record = {"action_id": uuid4().hex, "selector": selector, "state": "queued"}
            self._actions.append(record)

            def perform() -> None:
                record["state"] = "running"
                try:
                    # A queued input may enter a nested modal event loop. Never hold an MCP
                    # request waiting for it; subsequent state calls report its real status.
                    if isinstance(target, QAction):
                        menus = [obj for obj in target.associatedObjects() if isinstance(obj, QMenu)]
                        if not menus:
                            raise ValueError("Action has no application menu.")
                        menu = menus[0]
                        menu.popup(self.bridge.window.mapToGlobal(self.bridge.window.rect().center()))
                        QTest.mouseClick(menu, Qt.MouseButton.LeftButton,
                                         pos=menu.actionGeometry(target).center())
                    elif action == "type":
                        target.setFocus()
                        QTest.keyClick(target, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                        # QTest.keyClicks is ASCII-only. Input-method events deliver Unicode
                        # locally, without touching the clipboard or OS-wide input.
                        from PySide6.QtGui import QInputMethodEvent
                        event = QInputMethodEvent()
                        event.setCommitString(text)
                        QApplication.sendEvent(target, event)
                        QTest.keyClick(target, Qt.Key.Key_Tab)
                    elif action == "key":
                        QTest.keyClick(target, KEYS[key])
                    elif action == "select" and isinstance(target, QTabWidget):
                        bar = target.tabBar()
                        QTest.mouseClick(bar, Qt.MouseButton.LeftButton,
                                         pos=bar.tabRect(index).center())
                    elif action == "select" and isinstance(target, QTableWidget):
                        item = target.item(index, 0)
                        if item is None:
                            raise ValueError("Selected row has no visible item.")
                        target.scrollToItem(item)
                        QTest.mouseClick(target.viewport(), Qt.MouseButton.LeftButton,
                                         pos=target.visualItemRect(item).center())
                    elif action == "select" and isinstance(target, QComboBox):
                        target.setFocus()
                        QTest.keyClick(target, Qt.Key.Key_Home)
                        for _ in range(index):
                            QTest.keyClick(target, Qt.Key.Key_Down)
                    else:
                        QTest.mouseClick(target, Qt.MouseButton.LeftButton)
                    record["state"] = "completed"
                except Exception as error:
                    # Input is asynchronous; expose failure through get_ui_state, not stderr-only.
                    record.update(state="failed", error=str(error))

            QTimer.singleShot(0, perform)
            return {"action_id": record["action_id"], "state": "queued"}
        return self.bridge.call(enqueue)
