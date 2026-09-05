from __future__ import annotations

from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtMultimedia import QVideoSink
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QLayout,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.models import Segment


class SubtitleVideoWidget(QGraphicsView):
    """Preview subtitles derived from editable segments, without changing the media."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # QVideoWidget embeds a native window that always covers QWidget overlays.
        scene = QGraphicsScene(self)
        self.setScene(scene)
        self._video_item = QGraphicsVideoItem()
        scene.addItem(self._video_item)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._segments: list[Segment] = []
        self._position = 0.0
        self._subtitles: tuple[tuple[str, str], ...] = ()
        self.subtitle_overlay = QFrame(self.viewport())
        self.subtitle_overlay.setObjectName("subtitleOverlay")
        self.subtitle_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.subtitle_overlay.setStyleSheet("""
            QFrame#subtitleOverlay {
                background: rgba(0, 0, 0, 190);
                border: 0;
                border-radius: 6px;
            }
            QLabel {
                background: transparent;
                border: 0;
                color: white;
                font-size: 14pt;
            }
            QLabel#subtitleSpeaker { color: #8ee8f0; font-size: 11pt; font-weight: 700; }
        """)
        self._subtitle_layout = QVBoxLayout(self.subtitle_overlay)
        self._subtitle_layout.setContentsMargins(12, 8, 12, 8)
        self._subtitle_layout.setSpacing(4)
        # Subtitles must not impose a minimum width on the collapsible video pane.
        self._subtitle_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.subtitle_overlay.hide()

    def videoSink(self) -> QVideoSink:  # noqa: N802
        return self._video_item.videoSink()

    def set_segments(self, segments: list[Segment]) -> None:
        self._segments = segments
        self._refresh_subtitles()

    def set_position(self, seconds: float) -> None:
        self._position = seconds
        self._refresh_subtitles()

    def _refresh_subtitles(self) -> None:
        active = sorted(
            (
                segment for segment in self._segments
                if segment.start <= self._position < segment.end and segment.caption.strip()
            ),
            key=lambda segment: (segment.start, segment.end),
        )
        subtitles = tuple(
            (", ".join(segment.characters) or segment.primary_character, segment.caption)
            for segment in active
        )
        if subtitles == self._subtitles:
            return
        self._subtitles = subtitles
        while self._subtitle_layout.count():
            item = self._subtitle_layout.takeAt(0)
            if widget := item.widget():
                widget.hide()
                widget.deleteLater()
        for index, (speakers, caption) in enumerate(subtitles):
            if index:
                self._subtitle_layout.addSpacing(8)
            for name, text in (("subtitleSpeaker", speakers), ("subtitleCaption", caption)):
                label = QLabel(text, self.subtitle_overlay)
                label.setObjectName(name)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setWordWrap(True)
                self._subtitle_layout.addWidget(label)
                label.show()
        self._position_overlay()

    def _position_overlay(self) -> None:
        if not self._subtitles:
            self.subtitle_overlay.hide()
            return
        viewport = self.viewport()
        margin = min(16, viewport.width() // 8, viewport.height() // 8)
        width = max(0, viewport.width() - 2 * margin)
        height = min(
            max(0, viewport.height() - 2 * margin),
            self._subtitle_layout.totalHeightForWidth(width),
        )
        self.subtitle_overlay.setGeometry(
            margin, viewport.height() - margin - height, width, height
        )
        self.subtitle_overlay.setVisible(bool(self._subtitles) and width > 24 and height > 0)
        self.subtitle_overlay.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.setSceneRect(QRectF(self.viewport().rect()))
        self._video_item.setSize(QSizeF(self.viewport().size()))
        self._position_overlay()
