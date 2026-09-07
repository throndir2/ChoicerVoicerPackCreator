from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.scene_editing import SceneEditMode, plan_scene_edit


class SceneEditDialog(QDialog):
    apply_requested = Signal()
    preview_requested = Signal(float, float)

    def __init__(
        self, project: PackProject, start: float, end: float, mode: SceneEditMode,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sceneEditDialog")
        self.project = project
        self.mode = mode
        operation = "Cut Out Video Range" if mode == "cut" else "New Project from Scene"
        self.setWindowTitle(f"{operation} - {project.title}")
        self.resize(510, 300)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.start_spin = self._time_spin("Scene In", project.video_duration, start)
        self.end_spin = self._time_spin("Scene Out", project.video_duration, end)
        form.addRow("In (seconds)", self.start_spin)
        form.addRow("Out (seconds)", self.end_spin)
        self.title_edit = QLineEdit(f"{project.title} - Scene", self)
        self.title_edit.setAccessibleName("Scene project title")
        if mode == "extract":
            form.addRow("Project title", self.title_edit)
        else:
            self.title_edit.hide()
        layout.addLayout(form)
        self.summary_label = QLabel(self)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.warning_label = QLabel(self)
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #ffbf69;")
        layout.addWidget(self.warning_label)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ffad7a;")
        layout.addWidget(self.error_label)
        self.preview_button = QPushButton("Preview Range", self)
        self.preview_button.clicked.connect(
            lambda: self.preview_requested.emit(self.start_spin.value(), self.end_spin.value())
        )
        self.apply_button = QPushButton(
            "Cut Out Range..." if mode == "cut" else "Create Scene Project...", self
        )
        self.apply_button.setToolTip(
            "Choose where to keep the new media. Original files are never overwritten. "
            "Lossless edited video can require substantial disk space."
        )
        self.apply_button.clicked.connect(self.apply_requested)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.preview_button)
        buttons.addStretch()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        for spin in (self.start_spin, self.end_spin):
            spin.valueChanged.connect(self.refresh_plan)
        self.title_edit.textChanged.connect(self.refresh_plan)
        self.refresh_plan()

    def _time_spin(self, name: str, duration: float, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setAccessibleName(name)
        spin.setDecimals(3)
        spin.setRange(0, max(0, duration))
        spin.setSingleStep(0.1)
        spin.setValue(value)
        return spin

    def refresh_plan(self) -> None:
        self.error_label.clear()
        self.error_label.hide()
        self.warning_label.clear()
        self.warning_label.hide()
        self.apply_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        try:
            plan = plan_scene_edit(
                self.project, self.start_spin.value(), self.end_spin.value(), self.mode,
            )
            if self.mode == "extract" and not self.title_edit.text().strip():
                raise ValueError("Enter a title for the scene project.")
        except ValueError as error:
            self.summary_label.clear()
            self.show_error(str(error))
            return
        if self.mode == "cut":
            summary = (
                f"Remove {plan.end - plan.start:.3f}s; remaining video: {plan.duration:.3f}s.\n"
                "Dialogue inside the cut is removed. Later dialogue and backing audio move "
                "with the video to close the gap. Original media files stay unchanged."
            )
        else:
            summary = (
                f"New scene: {plan.duration:.3f}s, starting at 0.\n"
                "Copy this range and its dialogue into a saved project in a new tab. "
                "The current project stays unchanged."
            )
        self.summary_label.setText(summary)
        self.warning_label.setText("\n".join((
            "Saves new lossless media, which can be substantially larger than the original. "
            "Choose a folder with enough free space.",
            *plan.warnings,
        )))
        self.warning_label.show()
        self.apply_button.setEnabled(True)
        self.preview_button.setEnabled(True)

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()
