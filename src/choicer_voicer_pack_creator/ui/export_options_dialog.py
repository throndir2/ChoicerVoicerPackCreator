from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.ui.collapsible import CollapsibleSection


@dataclass(frozen=True)
class ExportOptions:
    video_height: int
    video_fps: int
    preserve_source_video: bool
    head_padding: float
    tail_padding: float

    @classmethod
    def from_project(cls, project: PackProject) -> ExportOptions:
        return cls(
            project.video_height, project.video_fps, project.preserve_source_video,
            project.head_padding, project.tail_padding,
        )

    def apply_to(self, project: PackProject) -> None:
        project.video_height = self.video_height
        project.video_fps = self.video_fps
        project.preserve_source_video = self.preserve_source_video
        project.head_padding = self.head_padding
        project.tail_padding = self.tail_padding


class ExportOptionsDialog(QDialog):
    """Edit a detached draft; the caller applies it only after acceptance."""

    def __init__(self, project: PackProject, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initial = ExportOptions.from_project(project)
        self._preserve_preference = project.preserve_source_video
        self.setObjectName("exportOptions")
        self.setWindowTitle("Export options")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        self.current_label = QLabel(
            f"Current project quality: {project.video_height}p at {project.video_fps} FPS"
        )
        layout.addWidget(self.current_label)

        form = QFormLayout()
        self.quality_combo = QComboBox()
        self.quality_combo.setObjectName("exportQualityPreset")
        self.quality_combo.setAccessibleName("Export quality")
        self.quality_combo.addItem("Fast (480p at 30 FPS)", (480, 30))
        self.quality_combo.addItem("Higher quality (720p at 30 FPS)", (720, 30))
        self.quality_combo.addItem("Custom", None)
        profile = (project.video_height, project.video_fps)
        index = next(
            (index for index in range(2) if self.quality_combo.itemData(index) == profile), 2,
        )
        self.quality_combo.setCurrentIndex(index)
        self.quality_combo.setToolTip(
            "Fast reduces encoding work; Higher quality retains more detail. "
            "Custom keeps your own height and frame rate."
        )
        form.addRow("&Quality", self.quality_combo)
        self.height_spin = QSpinBox()
        self.height_spin.setObjectName("exportHeight")
        self.height_spin.setAccessibleName("Export height in pixels")
        self.height_spin.setRange(144, 2160)
        self.height_spin.setSingleStep(72)
        self.height_spin.setValue(project.video_height)
        self.height_spin.setToolTip(
            "144-2160 pixels. Keeps aspect ratio and does not upscale smaller sources."
        )
        form.addRow("&Height (pixels)", self.height_spin)
        self.fps_spin = QSpinBox()
        self.fps_spin.setObjectName("exportFps")
        self.fps_spin.setAccessibleName("Export frames per second")
        self.fps_spin.setRange(1, 120)
        self.fps_spin.setValue(project.video_fps)
        self.fps_spin.setToolTip("1-120 FPS. Higher frame rates take longer to encode.")
        form.addRow("&FPS", self.fps_spin)
        layout.addLayout(form)
        self.quality_note = QLabel()
        self.quality_note.setWordWrap(True)
        layout.addWidget(self.quality_note)

        self.preserve_check = QCheckBox("Copy compatible imported OGV without re-encoding")
        self.preserve_check.setObjectName("exportPreserveVideo")
        self.preserve_check.setChecked(project.preserve_source_video)
        self.preserve_check.setToolTip(
            "Only copies OGV with compatible codecs/audio and matching height/FPS. "
            "Compatibility is checked during export; otherwise the video is converted."
        )
        layout.addWidget(self.preserve_check)
        self.preserve_note = QLabel()
        self.preserve_note.setWordWrap(True)
        layout.addWidget(self.preserve_note)

        self.advanced = CollapsibleSection("Advanced: prompt audio padding", self)
        self.advanced.toggle_button.setObjectName("exportAdvanced")
        self.advanced.toggle_button.setAccessibleName("Advanced prompt audio padding")
        padding = QWidget()
        padding_form = QFormLayout(padding)
        self.head_pad_spin = QDoubleSpinBox()
        self.tail_pad_spin = QDoubleSpinBox()
        for spin, name, label, value in (
            (self.head_pad_spin, "exportHeadPadding", "Head padding", project.head_padding),
            (self.tail_pad_spin, "exportTailPadding", "Tail padding", project.tail_padding),
        ):
            spin.setObjectName(name)
            spin.setAccessibleName(f"{label} in seconds")
            spin.setRange(0, 2)
            spin.setDecimals(3)
            spin.setSingleStep(0.025)
            spin.setSuffix(" s")
            spin.setValue(value)
            spin.setToolTip(
                f"{label} for prompts extracted from video, from 0 to 2 seconds. "
                "Imported prompt recordings are kept unchanged."
            )
            padding_form.addRow(f"{label} (seconds)", spin)
        padding_note = QLabel(
            "Silence before/after newly generated prompts. Export adjusts timestamps to keep "
            "them synchronized. Imported prompt recordings are not repadded."
        )
        padding_note.setWordWrap(True)
        padding_form.addRow(padding_note)
        self.advanced.set_content(padding)
        self.advanced.set_collapsed(True)
        self.advanced.collapsed_changed.connect(lambda _collapsed: self.adjustSize())
        layout.addWidget(self.advanced)

        note = QLabel(
            "Continue applies these settings to the project, then asks for an export location "
            "(and backing music if needed). Save the project to keep changed settings for next "
            "time. Cancel leaves settings unchanged. Source files are never modified."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.continue_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.continue_button.setText("Continue to export location...")
        self.continue_button.setObjectName("exportOptionsContinue")
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("exportOptionsCancel")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.quality_combo.currentIndexChanged.connect(self._quality_changed)
        self.height_spin.valueChanged.connect(self._profile_changed)
        self.fps_spin.valueChanged.connect(self._profile_changed)
        self.preserve_check.toggled.connect(self._preservation_changed)
        self._quality_changed()

    def _quality_changed(self) -> None:
        profile = self.quality_combo.currentData()
        custom = profile is None
        self.height_spin.setEnabled(custom)
        self.fps_spin.setEnabled(custom)
        if profile is not None:
            with QSignalBlocker(self.height_spin), QSignalBlocker(self.fps_spin):
                self.height_spin.setValue(profile[0])
                self.fps_spin.setValue(profile[1])
        self.quality_note.setText(
            "Fast: less encoding work and smaller output; best for quicker exports."
            if self.quality_combo.currentIndex() == 0 else
            "Higher quality: more picture detail, but slower encoding and larger output."
            if self.quality_combo.currentIndex() == 1 else
            "Custom: keep your current profile or choose a height and frame rate. "
            "Larger sizes and higher frame rates take longer to encode."
        )
        self._profile_changed()

    def _profile_changed(self) -> None:
        unchanged = (
            self.height_spin.value() == self._initial.video_height
            and self.fps_spin.value() == self._initial.video_fps
        )
        with QSignalBlocker(self.preserve_check):
            self.preserve_check.setEnabled(unchanged)
            self.preserve_check.setChecked(unchanged and self._preserve_preference)
        self.preserve_note.setText(
            "Copying avoids video conversion only when the imported OGV is compatible and "
            "its height/FPS match. Otherwise export converts using the selected quality."
            if unchanged else
            "Height/FPS changed: imported-video copying is off so the selected profile is used. "
            "Restore the original height/FPS to make copying available again."
        )

    def _preservation_changed(self, checked: bool) -> None:
        self._preserve_preference = checked

    def options(self) -> ExportOptions:
        # Keep stored sub-millisecond precision unless the displayed padding was edited.
        return ExportOptions(
            self.height_spin.value(), self.fps_spin.value(), self.preserve_check.isChecked(),
            self._initial.head_padding
            if self.head_pad_spin.value() == round(self._initial.head_padding, 3)
            else self.head_pad_spin.value(),
            self._initial.tail_padding
            if self.tail_pad_spin.value() == round(self._initial.tail_padding, 3)
            else self.tail_pad_spin.value(),
        )
