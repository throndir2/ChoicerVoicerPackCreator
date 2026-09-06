from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QSettings

from choicer_voicer_pack_creator.diagnostics import diagnostic_event

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import ProjectEditor

DEFAULT_WINDOW_SIZE = (1500, 950)
DEFAULT_EDITOR_SIZES = (1030, 470)
DEFAULT_INSPECTOR_SIZES = (285, 355, 235)
_SECTION_NAMES = ("packDetails", "segments", "selectedSegment")


def read_layout_bytes(settings: QSettings, key: str) -> QByteArray | None:
    value = settings.value(key)
    if value is None or isinstance(value, QByteArray):
        return value
    diagnostic_event("layout_setting_invalid", key=key)
    return None


@dataclass(frozen=True)
class EditorLayout:
    editor_state: QByteArray | None = None
    inspector_state: QByteArray | None = None
    collapsed: tuple[bool, ...] = (False, False, False)
    expanded_heights: tuple[int, ...] = DEFAULT_INSPECTOR_SIZES

    @classmethod
    def load(cls, settings: QSettings) -> EditorLayout:
        collapsed = []
        heights = []
        for name, default in zip(_SECTION_NAMES, DEFAULT_INSPECTOR_SIZES, strict=True):
            value = settings.value(f"layout/{name}CollapsedV1", False)
            collapsed.append(str(value).strip().casefold() in {"1", "true", "yes", "on"})
            key = f"layout/{name}ExpandedHeightV1"
            try:
                height = int(settings.value(key, default))
            except (TypeError, ValueError, OverflowError):
                diagnostic_event("layout_setting_invalid", key=key)
                height = default
            heights.append(max(120, min(10_000, height)))
        return cls(
            read_layout_bytes(settings, "layout/editorSplitterV1"),
            read_layout_bytes(settings, "layout/inspectorSplitterV1"),
            tuple(collapsed), tuple(heights),
        )

    @classmethod
    def capture(cls, editor: ProjectEditor) -> EditorLayout:
        for section, height in zip(
            editor.inspector_sections, editor.inspector_splitter.sizes(), strict=True,
        ):
            if not section.is_collapsed:
                section.set_last_expanded_height(height)
        return cls(
            editor.editor_splitter.saveState(),
            editor.inspector_splitter.saveState(),
            tuple(section.is_collapsed for section in editor.inspector_sections),
            tuple(section.last_expanded_height for section in editor.inspector_sections),
        )

    def apply(self, editor: ProjectEditor) -> None:
        for section, collapsed, height in zip(
            editor.inspector_sections, self.collapsed, self.expanded_heights, strict=True,
        ):
            section.set_collapsed(collapsed)
            section.set_last_expanded_height(height)
        for splitter, state, defaults, key, handle_width in (
            (
                editor.editor_splitter, self.editor_state, DEFAULT_EDITOR_SIZES,
                "layout/editorSplitterV1", 1,
            ),
            (
                editor.inspector_splitter, self.inspector_state, DEFAULT_INSPECTOR_SIZES,
                "layout/inspectorSplitterV1", 9,
            ),
        ):
            if state is None:
                splitter.setSizes(defaults)
            elif not splitter.restoreState(state):
                diagnostic_event("layout_setting_invalid", key=key)
                splitter.setSizes(defaults)
            # Qt also restores handle widths and collapsibility from saved splitter states.
            splitter.setHandleWidth(handle_width)
            splitter.setChildrenCollapsible(False)
        editor.editor_splitter.setCollapsible(0, True)

    def save(self, settings: QSettings) -> None:
        for key, state in (
            ("layout/editorSplitterV1", self.editor_state),
            ("layout/inspectorSplitterV1", self.inspector_state),
        ):
            if state is None:
                settings.remove(key)
            else:
                settings.setValue(key, state)
        for name, collapsed, height in zip(
            _SECTION_NAMES, self.collapsed, self.expanded_heights, strict=True,
        ):
            settings.setValue(f"layout/{name}CollapsedV1", collapsed)
            settings.setValue(f"layout/{name}ExpandedHeightV1", height)
