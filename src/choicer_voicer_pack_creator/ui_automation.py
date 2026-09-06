"""Opt-in, application-local UI observation and real Qt input; no evaluation API."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar
from uuid import uuid4

from PySide6.QtCore import QBuffer, QIODevice, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
    QScrollBar,
    QTableWidget,
    QTabWidget,
    QWidget,
)

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import MainWindow

T = TypeVar("T")
SELECTORS = frozenset({
    "projectTabs", "projectTitle", "segmentCaption", "segmentsTable",
    "saveProject", "exportProject", "analyzeProject",
    "tasksDock", "taskProjectFilter", "tasksTable", "taskLog",
    "taskShowProject", "taskCancel", "taskRetry", "taskOpenOutput", "taskDetails",
    "exportDetailsClose",
    "projectCloseKeepProcessing", "projectCloseCancelTasks", "projectCloseKeepOpen",
    "projectCloseSave", "projectCloseDiscard", "projectCloseCancel",
    "projectEditorScroll",
    "projectEditorScrollbar", "projectDetailsScrollbar", "selectedSegmentScrollbar",
})
EDITOR_SELECTORS = frozenset({
    "projectTitle", "segmentCaption", "segmentsTable",
    "saveProject", "exportProject", "analyzeProject",
    "projectEditorScroll",
    "projectEditorScrollbar", "projectDetailsScrollbar", "selectedSegmentScrollbar",
})
KEYS = {
    "Enter": Qt.Key.Key_Return, "Escape": Qt.Key.Key_Escape,
    "Tab": Qt.Key.Key_Tab, "Backspace": Qt.Key.Key_Backspace,
    "Space": Qt.Key.Key_Space, "Delete": Qt.Key.Key_Delete,
    "PageUp": Qt.Key.Key_PageUp, "PageDown": Qt.Key.Key_PageDown,
    "Home": Qt.Key.Key_Home, "End": Qt.Key.Key_End,
    "Up": Qt.Key.Key_Up, "Down": Qt.Key.Key_Down,
}
TASK_ACTIONS = frozenset({
    "taskShowProject", "taskCancel", "taskRetry", "taskOpenOutput", "taskDetails",
})


class Bridge(Protocol):
    window: MainWindow

    def call(self, function: Callable[[], T]) -> T: ...


class UIAutomation:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self._actions: deque[dict[str, Any]] = deque(maxlen=40)

    @staticmethod
    def _find(root: QObject, selector: str) -> QObject | None:
        targets = root.findChildren(QObject, selector)
        if not targets:
            return None
        return next(
            (target for target in targets
             if isinstance(target, QWidget) and target.isVisible()), targets[0]
        )

    def _target(self, selector: str, project_id: str | None) -> QObject:
        if selector not in SELECTORS:
            raise ValueError(f"Selector is not allowlisted: {selector}")
        if project_id is not None and selector not in EDITOR_SELECTORS | {"projectTabs"}:
            raise ValueError("Omit project_id for global Tasks and decision controls.")
        window = self.bridge.window
        if project_id is not None and project_id not in {
            session.id for session in window.project_sessions
        }:
            raise ValueError(f"Unknown project_id: {project_id}")
        editor = window.editor_for_project(project_id) if project_id is not None else window.active_editor
        if project_id is not None and editor is not window.active_editor:
            raise ValueError("Project is not the visible tab. Select it through projectTabs first.")
        roots = [editor] if selector in EDITOR_SELECTORS and editor is not None else [window]
        for root in roots:
            visible = [
                item for item in root.findChildren(QObject, selector)
                if isinstance(item, (QWidget, QAction)) and item.isVisible()
            ]
            if len(visible) > 1:
                raise ValueError("Multiple visible widgets match this selector; dismiss other windows.")
            target = self._find(root, selector)
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
    def _hit(widget: QWidget, point: QPoint) -> QWidget:
        hit = QApplication.widgetAt(widget.mapToGlobal(point))
        if hit is None or (hit is not widget and not widget.isAncestorOf(hit)):
            raise ValueError("The target input point is clipped or obscured by another widget/window.")
        local = hit.mapFromGlobal(widget.mapToGlobal(point))
        if not hit.visibleRegion().contains(local):
            raise ValueError("The actual input receiver is clipped. Enlarge or scroll its panel.")
        return hit

    @staticmethod
    def _input_point(widget: QWidget) -> QPoint:
        area = widget.viewport() if isinstance(widget, QAbstractScrollArea) else widget
        visible = area.visibleRegion()
        if visible.isEmpty():
            raise ValueError("The target input area is clipped. Enlarge or scroll its panel.")
        return widget.mapFromGlobal(area.mapToGlobal(visible.boundingRect().center()))

    def _click(
        self, widget: QWidget, verify: Callable[[], None], point: QPoint | None = None,
    ) -> None:
        point = self._input_point(widget) if point is None else point
        hit = self._prepare_input(widget, point, verify)
        position = hit.mapFromGlobal(widget.mapToGlobal(point))
        verify()
        QTest.mouseClick(hit, Qt.MouseButton.LeftButton, pos=position, delay=0)

    def _prepare_input(
        self, widget: QWidget, point: QPoint, verify: Callable[[], None],
    ) -> QWidget:
        verify()
        hit = self._hit(widget, point)
        window = hit.window()
        if not isinstance(window, QMenu) and not window.isActiveWindow():
            window.activateWindow()
            if not QTest.qWaitForWindowActive(window, 1000):
                raise ValueError("The application window could not receive focus. Focus it and retry.")
        # Activation waits dispatch queued actions; the original target may no longer be current.
        verify()
        return self._hit(widget, point)

    @staticmethod
    def _describe(target: QObject) -> dict[str, Any]:
        result: dict[str, Any] = {"selector": target.objectName()}
        if isinstance(target, QWidget):
            visible = target.visibleRegion().boundingRect()
            result.update(
                enabled=target.isEnabled(), visible=target.isVisible(), focused=target.hasFocus(),
                rendered=not target.visibleRegion().isEmpty(),
                accessible_name=target.accessibleName(),
                size=[target.width(), target.height()],
                visible_rect=[visible.x(), visible.y(), visible.width(), visible.height()],
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
            }), viewport_height=target.viewport().height(), viewport_width=target.viewport().width(),
                row_ids=[
                    target.item(row, 0).data(Qt.ItemDataRole.UserRole)
                    if target.item(row, 0) is not None else None
                    for row in range(min(target.rowCount(), 500))
                ])
        elif isinstance(target, QTabWidget):
            result.update(index=target.currentIndex(), tabs=[
                {"index": index, "text": target.tabText(index),
                 "project_id": target.widget(index).session.id}
                for index in range(target.count())
            ])
        elif isinstance(target, QComboBox):
            result.update(index=target.currentIndex(), text=target.currentText())
        elif isinstance(target, QScrollArea):
            result.update(
                vertical_scroll=target.verticalScrollBar().value(),
                vertical_maximum=target.verticalScrollBar().maximum(),
            )
        elif isinstance(target, QScrollBar):
            result.update(value=target.value(), minimum=target.minimum(), maximum=target.maximum())
        return result

    def state(self) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            window = self.bridge.window
            editor = window.active_editor
            widgets = []
            for selector in sorted(SELECTORS):
                roots = [editor] if selector in EDITOR_SELECTORS and editor is not None else [window]
                target = next(
                    (item for root in roots
                     if (item := self._find(root, selector)) is not None), None
                )
                if target is not None:
                    widgets.append(self._describe(target))
            focus = QApplication.focusWidget()
            return {
                "platform": QApplication.platformName(),
                "visible": window.isVisible(),
                "window_title": window.windowTitle(),
                "window_active": window.isActiveWindow(),
                "process_id": QApplication.applicationPid(),
                "data_root": QApplication.instance().property("isolatedDataRoot"),
                "active_project_id": editor.session.id if editor else None,
                "focus_selector": focus.objectName() if focus and self._belongs(focus) else None,
                "widgets": widgets,
                "windows": [
                    {"title": item.windowTitle(), "modal": item.isModal(),
                     "visible": item.isVisible(), "object_name": item.objectName()}
                    for item in QApplication.topLevelWidgets() if self._belongs(item)
                ],
                "actions": [dict(action) for action in self._actions],
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
        self, selector: str, action: Literal["click", "type", "key", "select", "close_tab", "reveal"],
        project_id: str | None = None, text: str | None = None,
        index: int | None = None, key: str | None = None,
    ) -> dict[str, str]:
        def enqueue() -> dict[str, str]:
            target = self._target(selector, project_id)
            if not isinstance(target, (QWidget, QAction)) or not target.isEnabled():
                raise ValueError("Target is not enabled for input.")
            if not target.isVisible():
                raise ValueError("Target is not visible.")
            if action != "reveal" and isinstance(target, QWidget) and target.visibleRegion().isEmpty():
                raise ValueError("Target has no rendered input area.")
            modal = QApplication.activeModalWidget()
            if modal is not None and (
                not isinstance(target, QWidget)
                or (target is not modal and not modal.isAncestorOf(target))
            ):
                raise ValueError("A modal window blocks this target; dismiss it in the editor.")
            if action == "reveal":
                if not isinstance(target, QWidget):
                    raise ValueError("Only widgets can be revealed by scrolling.")
            elif action == "type":
                if not isinstance(target, (QLineEdit, QPlainTextEdit)) or target.isReadOnly():
                    raise ValueError("Typing is allowed only in editable title/caption fields.")
                if text is None or len(text) > 10000:
                    raise ValueError("Provide text of at most 10000 characters.")
            elif action == "key":
                if not isinstance(target, QWidget) or key not in KEYS:
                    raise ValueError(f"Allowed keys: {', '.join(KEYS)}")
            elif action in {"select", "close_tab"}:
                if not isinstance(target, (QTabWidget, QTableWidget, QComboBox)):
                    raise ValueError("Selection requires tabs, a table, or a combobox.")
                if action == "close_tab" and not isinstance(target, QTabWidget):
                    raise ValueError("close_tab requires projectTabs.")
                count = target.rowCount() if isinstance(target, QTableWidget) else target.count()
                if index is None or not 0 <= index < count:
                    raise ValueError("Selection index is outside the available items.")
            elif action != "click":
                raise ValueError("Unsupported UI action.")
            tab = target.widget(index) if isinstance(target, QTabWidget) and index is not None else None
            if tab is not None and project_id is not None and tab.session.id != project_id:
                raise ValueError("The tab index does not match project_id.")
            row_id = None
            if isinstance(target, QTableWidget) and action == "select":
                item = target.item(index, 0)
                if item is None or item.data(Qt.ItemDataRole.UserRole) is None:
                    raise ValueError("The selected row has no stable identity.")
                row_id = item.data(Qt.ItemDataRole.UserRole)
            editor = self.bridge.window.active_editor
            editor_id = editor.session.id if selector in EDITOR_SELECTORS else None
            caption_id = editor.selected_segment_id if selector == "segmentCaption" else None
            task_id = (
                self.bridge.window.tasks_panel._selected_id() if selector in TASK_ACTIONS else None
            )
            record = {"action_id": uuid4().hex, "selector": selector, "state": "queued"}
            self._actions.append(record)

            def verify() -> None:
                if not target.isVisible() or not target.isEnabled():
                    raise ValueError("Target became hidden or disabled before input.")
                if action != "reveal" and isinstance(target, QWidget) and target.visibleRegion().isEmpty():
                    raise ValueError("Target lost its rendered input area before input.")
                if project_id is not None and self.bridge.window.active_editor.session.id != project_id:
                    raise ValueError("The active project changed before input.")
                if editor_id and self.bridge.window.active_editor.session.id != editor_id:
                    raise ValueError("The active project changed before input.")
                if caption_id is not None and editor.selected_segment_id != caption_id:
                    raise ValueError("The selected segment changed before typing.")
                if selector in TASK_ACTIONS and (
                    self.bridge.window.tasks_panel._selected_id() != task_id
                ):
                    raise ValueError("The selected task changed before input.")
                if tab is not None and target.indexOf(tab) < 0:
                    raise ValueError("The target tab closed before input.")
                if self._target(selector, project_id) is not target:
                    raise ValueError("The target widget changed before input.")
                modal = QApplication.activeModalWidget()
                if modal is not None and (
                    not isinstance(target, QWidget)
                    or (target is not modal and not modal.isAncestorOf(target))
                ):
                    raise ValueError("A modal window blocked the target before input.")

            def key_click(value: Qt.Key, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
                verify()
                QTest.keyClick(target, value, modifiers, delay=0)

            def perform() -> None:
                record["state"] = "running"
                try:
                    verify()
                    tab_index = target.indexOf(tab) if tab is not None else None
                    # A queued input may enter a nested modal event loop. Never hold an MCP
                    # request waiting for it; subsequent state calls report its real status.
                    if action == "reveal":
                        ancestor = target.parentWidget()
                        while ancestor is not None:
                            if isinstance(ancestor, QScrollArea):
                                ancestor.ensureWidgetVisible(target, 10, 10)
                            ancestor = ancestor.parentWidget()
                        self._hit(target, self._input_point(target))
                    elif isinstance(target, QAction):
                        menus = [obj for obj in target.associatedObjects() if isinstance(obj, QMenu)]
                        if not menus:
                            raise ValueError("Action has no application menu.")
                        menu = menus[0]
                        menu.popup(self.bridge.window.mapToGlobal(self.bridge.window.rect().center()))
                        if not QTest.qWaitForWindowExposed(menu, 1000):
                            raise ValueError("The action menu did not become visible.")
                        self._click(menu, verify, menu.actionGeometry(target).center())
                    elif action == "type":
                        self._prepare_input(target, self._input_point(target), verify)
                        target.setFocus()
                        key_click(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                        if all(" " <= char <= "~" for char in text):
                            if text:
                                for char in text:
                                    verify()
                                    QTest.keyClicks(target, char, delay=0)
                            else:
                                key_click(Qt.Key.Key_Backspace)
                        else:
                            # Qt's keyClicks is ASCII-only; use normal input-method events
                            # for Unicode/newlines, never the clipboard or OS-wide input.
                            from PySide6.QtGui import QInputMethodEvent
                            event = QInputMethodEvent()
                            event.setCommitString(text)
                            verify()
                            QApplication.sendEvent(target, event)
                        if isinstance(target, QLineEdit):
                            key_click(Qt.Key.Key_Tab)
                    elif action == "key":
                        self._prepare_input(target, self._input_point(target), verify)
                        target.setFocus()
                        key_click(KEYS[key])
                    elif action == "close_tab":
                        from PySide6.QtWidgets import QTabBar
                        bar = target.tabBar()
                        button = bar.tabButton(tab_index, QTabBar.ButtonPosition.RightSide)
                        if button is None:
                            button = bar.tabButton(tab_index, QTabBar.ButtonPosition.LeftSide)
                        if button is None:
                            raise ValueError("The tab has no close button.")
                        self._click(button, verify)
                    elif action == "select" and isinstance(target, QTabWidget):
                        bar = target.tabBar()
                        point = bar.tabRect(tab_index).center()

                        def verify_tab() -> None:
                            verify()
                            if bar.tabAt(point) != target.indexOf(tab):
                                raise ValueError("The target tab moved before input.")

                        self._click(bar, verify_tab, point)
                        if target.currentWidget() is not tab:
                            raise RuntimeError("The tab click did not select the requested project.")
                    elif action == "select" and isinstance(target, QTableWidget):
                        item = next((
                            target.item(row, 0) for row in range(target.rowCount())
                            if target.item(row, 0) is not None
                            and target.item(row, 0).data(Qt.ItemDataRole.UserRole) == row_id
                        ), None)
                        if item is None:
                            raise ValueError("Selected row is no longer available.")
                        target.scrollToItem(item)
                        point = target.visualItemRect(item).center()
                        if not target.viewport().rect().contains(point):
                            raise ValueError(
                                "The target row is clipped. Enlarge its panel before selecting."
                            )
                        def verify_row() -> None:
                            verify()
                            current = target.itemAt(point)
                            if current is None or current.data(Qt.ItemDataRole.UserRole) != row_id:
                                raise ValueError("The target row moved before input.")

                        self._click(target.viewport(), verify_row, point)
                        if not any(
                            selected.data(Qt.ItemDataRole.UserRole) == row_id
                            for selected in target.selectedItems()
                        ):
                            raise RuntimeError("The row click did not select the requested item.")
                    elif action == "select" and isinstance(target, QComboBox):
                        self._prepare_input(target, self._input_point(target), verify)
                        target.setFocus()
                        key_click(Qt.Key.Key_Home)
                        for _ in range(index):
                            key_click(Qt.Key.Key_Down)
                    else:
                        self._click(target, verify)
                    record["state"] = "completed"
                except Exception as error:
                    # Input is asynchronous; expose failure through get_ui_state, not stderr-only.
                    record.update(state="failed", error=str(error))

            QTimer.singleShot(0, perform)
            return {"action_id": record["action_id"], "state": "queued"}
        return self.bridge.call(enqueue)
