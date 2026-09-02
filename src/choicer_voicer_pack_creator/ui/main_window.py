from __future__ import annotations

import getpass
from pathlib import Path

from PySide6.QtCore import QSettings, QSignalBlocker, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator import __version__
from choicer_voicer_pack_creator.exporter import (
    ExportResult,
    PackExporter,
    safe_name,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.project_io import ProjectStore
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget


class WaveformWorker(QThread):
    completed = Signal(str, float, list)
    failed = Signal(str, str)

    def __init__(self, media: MediaTools, path: str, duration: float) -> None:
        super().__init__()
        self.media = media
        self.path = path
        self.duration = duration

    def run(self) -> None:
        try:
            info = self.media.probe(Path(self.path))
            peaks = self.media.waveform_peaks(Path(self.path), info.duration)
            self.completed.emit(self.path, info.duration, peaks)
        except Exception as error:
            self.failed.emit(self.path, str(error))


class ExportWorker(QThread):
    progress = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, exporter: PackExporter, project: PackProject, destination: Path) -> None:
        super().__init__()
        self.exporter = exporter
        self.project = PackProject.from_dict(project.to_dict())
        self.destination = destination

    def run(self) -> None:
        try:
            result = self.exporter.export(
                self.project,
                self.destination,
                create_zip=True,
                progress=self.progress.emit,
            )
            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(str(error))


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:06.3f}"


class MainWindow(QMainWindow):
    def __init__(self, media: MediaTools, initial_path: Path | None = None) -> None:
        super().__init__()
        self.media = media
        self.importer = PackImporter(media)
        self.exporter = PackExporter(media)
        self.settings = QSettings("ChoicerVoicerCommunity", "ChoicerVoicerPackCreator")
        self.project = PackProject(authors=[getpass.getuser()])
        self.project_path: Path | None = None
        self.selected_segment_id = ""
        self.dirty = False
        self._syncing = False
        self._slider_dragging = False
        self._preview_end: float | None = None
        self._waveform_workers: list[WaveformWorker] = []
        self._export_worker: ExportWorker | None = None

        self.setWindowTitle("Choicer Voicer Pack Creator")
        self.resize(1500, 900)
        self.setMinimumSize(1050, 680)
        self._build_actions()
        self._build_ui()
        self._connect_player()
        self._set_project(self.project, None, mark_dirty=False)

        if initial_path:
            QTimer.singleShot(0, lambda: self.open_path(initial_path))

    # ---------- UI construction ----------

    def _build_actions(self) -> None:
        self.action_new = QAction("New from Video…", self)
        self.action_new.setShortcut(QKeySequence.StandardKey.New)
        self.action_new.triggered.connect(self.new_from_video)
        self.action_open = QAction("Open Project…", self)
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.triggered.connect(self.open_project)
        self.action_import = QAction("Import Existing Pack…", self)
        self.action_import.setShortcut(QKeySequence("Ctrl+I"))
        self.action_import.triggered.connect(self.import_pack)
        self.action_save = QAction("Save Project", self)
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)
        self.action_save.triggered.connect(self.save_project)
        self.action_save_as = QAction("Save Project As…", self)
        self.action_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.action_save_as.triggered.connect(lambda: self.save_project(save_as=True))
        self.action_export = QAction("Export Pack + ZIP…", self)
        self.action_export.setShortcut(QKeySequence("Ctrl+E"))
        self.action_export.triggered.connect(self.export_pack)
        self.action_exit = QAction("Exit", self)
        self.action_exit.triggered.connect(self.close)

        self.action_add = QAction("Add Segment", self)
        self.action_add.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.action_add.triggered.connect(self.add_segment)
        self.action_split = QAction("Split at Playhead", self)
        self.action_split.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.action_split.triggered.connect(self.split_segment)
        self.action_delete = QAction("Delete Segment", self)
        self.action_delete.setShortcut(QKeySequence("Ctrl+Delete"))
        self.action_delete.triggered.connect(self.delete_segment)
        self.action_duplicate = QAction("Duplicate Segment", self)
        self.action_duplicate.setShortcut(QKeySequence("Ctrl+D"))
        self.action_duplicate.triggered.connect(self.duplicate_segment)

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addActions([self.action_new, self.action_open, self.action_import])
        file_menu.addSeparator()
        file_menu.addActions([self.action_save, self.action_save_as, self.action_export])
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)
        edit_menu = self.menuBar().addMenu("&Segments")
        edit_menu.addActions(
            [self.action_add, self.action_split, self.action_duplicate, self.action_delete]
        )
        help_menu = self.menuBar().addMenu("&Help")
        about = help_menu.addAction("About")
        about.triggered.connect(self.show_about)

    def _build_ui(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        toolbar.addActions([self.action_new, self.action_open, self.action_import])
        toolbar.addSeparator()
        toolbar.addActions([self.action_save, self.action_export])
        self.addToolBar(toolbar)

        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 8)
        root_layout.setSpacing(8)
        self.setCentralWidget(root)

        splitter = QSplitter(Qt.Orientation.Horizontal, root)
        splitter.setChildrenCollapsible(False)
        self.editor_splitter = splitter
        root_layout.addWidget(splitter, 1)

        left = QFrame(splitter)
        left.setFrameShape(QFrame.Shape.StyledPanel)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(7, 7, 7, 7)
        left_layout.setSpacing(7)

        self.video_widget = QVideoWidget(left)
        self.video_widget.setMinimumHeight(320)
        self.video_widget.setStyleSheet("background: #000; border: 1px solid #26384d;")
        left_layout.addWidget(self.video_widget, 1)

        transport = QHBoxLayout()
        self.play_button = QPushButton("▶ Play")
        self.play_button.clicked.connect(self.toggle_playback)
        transport.addWidget(self.play_button)
        self.stop_button = QPushButton("■")
        self.stop_button.setToolTip("Stop and return to the start")
        self.stop_button.clicked.connect(self.stop_playback)
        transport.addWidget(self.stop_button)
        self.position_label = QLabel("00:00.000 / 00:00.000")
        self.position_label.setMinimumWidth(165)
        transport.addWidget(self.position_label)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 100_000)
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_slider_dragging", True))
        self.seek_slider.sliderReleased.connect(self._seek_slider_released)
        self.seek_slider.sliderMoved.connect(self._seek_slider_moved)
        transport.addWidget(self.seek_slider, 1)
        transport.addWidget(QLabel("Volume"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(90)
        transport.addWidget(self.volume_slider)
        left_layout.addLayout(transport)

        self.timeline = TimelineWidget(left)
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.segment_selected.connect(self.select_segment)
        self.timeline.boundary_changed.connect(self._timeline_boundary_changed)
        self.timeline.zoom_changed.connect(self._timeline_zoom_changed)
        left_layout.addWidget(self.timeline)

        cutter = QHBoxLayout()
        self.mark_in_spin = self._time_spin()
        self.mark_out_spin = self._time_spin()
        self.mark_in_spin.valueChanged.connect(self._marks_changed)
        self.mark_out_spin.valueChanged.connect(self._marks_changed)
        cutter.addWidget(QLabel("In"))
        cutter.addWidget(self.mark_in_spin)
        set_in = QPushButton("Set In")
        set_in.clicked.connect(lambda: self.mark_in_spin.setValue(self.current_position()))
        cutter.addWidget(set_in)
        cutter.addWidget(QLabel("Out"))
        cutter.addWidget(self.mark_out_spin)
        set_out = QPushButton("Set Out")
        set_out.clicked.connect(lambda: self.mark_out_spin.setValue(self.current_position()))
        cutter.addWidget(set_out)
        add_button = QPushButton("+ Add Segment")
        add_button.setObjectName("primary")
        add_button.clicked.connect(self.add_segment)
        cutter.addWidget(add_button)
        apply_button = QPushButton("Apply Range")
        apply_button.clicked.connect(self.apply_selected_range)
        cutter.addWidget(apply_button)
        split_button = QPushButton("Split")
        split_button.clicked.connect(self.split_segment)
        cutter.addWidget(split_button)
        preview_button = QPushButton("Preview Segment")
        preview_button.clicked.connect(self.preview_segment)
        cutter.addWidget(preview_button)
        cutter.addStretch()
        cutter.addWidget(QLabel("Zoom"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(10, 800)
        self.zoom_slider.setValue(10)
        self.zoom_slider.setFixedWidth(120)
        self.zoom_slider.valueChanged.connect(lambda value: self.timeline.set_zoom(value / 10.0))
        cutter.addWidget(self.zoom_slider)
        left_layout.addLayout(cutter)

        right = QWidget(splitter)
        right.setMinimumWidth(420)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(8)

        project_group = QGroupBox("PACK DETAILS", right)
        project_form = QFormLayout(project_group)
        self.title_edit = QLineEdit()
        self.title_edit.editingFinished.connect(self._pack_details_changed)
        self.title_edit.textEdited.connect(self._pack_details_changed)
        project_form.addRow("Title", self.title_edit)
        self.authors_edit = QLineEdit()
        self.authors_edit.setPlaceholderText("Author One, Author Two")
        self.authors_edit.editingFinished.connect(self._pack_details_changed)
        self.authors_edit.textEdited.connect(self._pack_details_changed)
        project_form.addRow("Authors", self.authors_edit)
        self.readme_edit = QPlainTextEdit()
        self.readme_edit.setPlaceholderText("Pack notes, source credit, or recording guidance…")
        self.readme_edit.setMaximumHeight(72)
        self.readme_edit.textChanged.connect(self._pack_details_changed)
        project_form.addRow("Notes", self.readme_edit)
        self.video_path_label, video_row = self._path_controls(
            self.choose_source_video, self.clear_source_video
        )
        project_form.addRow("Video", video_row)
        self.backing_path_label, backing_row = self._path_controls(
            self.choose_backing_track, self.clear_backing_track
        )
        project_form.addRow("Backing", backing_row)
        self.icon_path_label, icon_row = self._path_controls(self.choose_icon, self.clear_icon)
        project_form.addRow("Icon", icon_row)

        export_settings = QHBoxLayout()
        self.head_pad_spin = QDoubleSpinBox()
        self.head_pad_spin.setRange(0, 2)
        self.head_pad_spin.setDecimals(3)
        self.head_pad_spin.setSingleStep(0.025)
        self.tail_pad_spin = QDoubleSpinBox()
        self.tail_pad_spin.setRange(0, 2)
        self.tail_pad_spin.setDecimals(3)
        self.tail_pad_spin.setSingleStep(0.025)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(144, 2160)
        self.height_spin.setSingleStep(72)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 120)
        for widget in (self.head_pad_spin, self.tail_pad_spin):
            widget.valueChanged.connect(self._pack_details_changed)
        self.height_spin.valueChanged.connect(self._video_profile_changed)
        self.fps_spin.valueChanged.connect(self._video_profile_changed)
        export_settings.addWidget(QLabel("Head"))
        export_settings.addWidget(self.head_pad_spin)
        export_settings.addWidget(QLabel("Tail"))
        export_settings.addWidget(self.tail_pad_spin)
        export_settings.addWidget(QLabel("Height"))
        export_settings.addWidget(self.height_spin)
        export_settings.addWidget(QLabel("FPS"))
        export_settings.addWidget(self.fps_spin)
        project_form.addRow("Export", export_settings)
        self.preserve_video_check = QCheckBox("Preserve imported compatible OGV without re-encoding")
        self.preserve_video_check.toggled.connect(self._pack_details_changed)
        project_form.addRow("Video mode", self.preserve_video_check)
        right_layout.addWidget(project_group)

        segment_group = QGroupBox("SEGMENTS", right)
        segment_layout = QVBoxLayout(segment_group)
        self.segment_table = QTableWidget(0, 6)
        self.segment_table.setHorizontalHeaderLabels(
            ["#", "In", "Out", "Speaker(s)", "Line", "Audio"]
        )
        self.segment_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.segment_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.segment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.segment_table.setAlternatingRowColors(True)
        self.segment_table.verticalHeader().hide()
        header = self.segment_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.segment_table.itemSelectionChanged.connect(self._table_selection_changed)
        self.segment_table.cellDoubleClicked.connect(lambda _row, _column: self.preview_segment())
        segment_layout.addWidget(self.segment_table, 1)
        row_buttons = QHBoxLayout()
        for label, handler in (
            ("+ Add", self.add_segment),
            ("Duplicate", self.duplicate_segment),
            ("Delete", self.delete_segment),
        ):
            button = QPushButton(label)
            if label == "Delete":
                button.setObjectName("danger")
            button.clicked.connect(handler)
            row_buttons.addWidget(button)
        row_buttons.addStretch()
        segment_layout.addLayout(row_buttons)
        right_layout.addWidget(segment_group, 1)

        editor_group = QGroupBox("SELECTED SEGMENT", right)
        editor_form = QFormLayout(editor_group)
        self.speakers_edit = QLineEdit()
        self.speakers_edit.setPlaceholderText("Speaker, Second Speaker")
        self.speakers_edit.editingFinished.connect(self._selected_speakers_changed)
        self.speakers_edit.textEdited.connect(self._selected_speakers_typed)
        editor_form.addRow("Speaker(s)", self.speakers_edit)
        self.caption_edit = QPlainTextEdit()
        self.caption_edit.setPlaceholderText("The exact line the player should perform…")
        self.caption_edit.setMaximumHeight(78)
        self.caption_edit.textChanged.connect(self._selected_caption_changed)
        editor_form.addRow("Line", self.caption_edit)
        self.audio_mode_combo = QComboBox()
        self.audio_mode_combo.addItem("Extract from source video", "video")
        self.audio_mode_combo.addItem("Preserve / use an audio file", "file")
        self.audio_mode_combo.currentIndexChanged.connect(self._audio_mode_changed)
        editor_form.addRow("Prompt audio", self.audio_mode_combo)
        self.segment_audio_label, audio_row = self._path_controls(
            self.choose_segment_audio, self.use_video_audio, clear_text="Use video"
        )
        editor_form.addRow("Audio file", audio_row)
        self.segment_image_label, image_row = self._path_controls(
            self.choose_segment_image, self.clear_segment_image
        )
        editor_form.addRow("Still image", image_row)
        right_layout.addWidget(editor_group)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        self.validation_label.setObjectName("muted")
        right_layout.addWidget(self.validation_label)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)
        QTimer.singleShot(0, lambda: splitter.setSizes([1030, 470]))

        progress_row = QHBoxLayout()
        self.progress_label = QLabel("Ready")
        self.progress_label.setObjectName("muted")
        progress_row.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.progress_bar.setMaximumWidth(280)
        self.progress_bar.hide()
        progress_row.addWidget(self.progress_bar)
        root_layout.addLayout(progress_row)
        self.statusBar().showMessage("Create a pack from a video or import an existing pack.")

    def _time_spin(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, 24 * 60 * 60)
        spin.setDecimals(3)
        spin.setSingleStep(0.05)
        spin.setSuffix(" s")
        spin.setMinimumWidth(105)
        return spin

    def _path_controls(self, choose, clear, clear_text: str = "Clear") -> tuple[QLabel, QWidget]:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("None")
        label.setObjectName("path")
        label.setWordWrap(False)
        layout.addWidget(label, 1)
        choose_button = QPushButton("Choose…")
        choose_button.clicked.connect(choose)
        layout.addWidget(choose_button)
        clear_button = QPushButton(clear_text)
        clear_button.clicked.connect(clear)
        layout.addWidget(clear_button)
        return label, container

    def _connect_player(self) -> None:
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self._player_position_changed)
        self.player.durationChanged.connect(self._player_duration_changed)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(self._player_error)
        self.prompt_player = QMediaPlayer(self)
        self.prompt_audio_output = QAudioOutput(self)
        self.prompt_audio_output.setVolume(0.8)
        self.prompt_player.setAudioOutput(self.prompt_audio_output)
        self.volume_slider.valueChanged.connect(self._set_volume)

    def _set_volume(self, value: int) -> None:
        volume = value / 100
        self.audio_output.setVolume(volume)
        self.prompt_audio_output.setVolume(volume)

    # ---------- Project lifecycle ----------

    def new_from_video(self) -> None:
        if not self._maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose source video",
            str(self.settings.value("lastVideoDir", "")),
            "Video files (*.mp4 *.mkv *.mov *.webm *.ogv *.avi);;All files (*)",
        )
        if not path:
            return
        source = Path(path).resolve()
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            info = self.media.probe(source)
        except Exception as error:
            QMessageBox.critical(self, "Could not open video", str(error))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.settings.setValue("lastVideoDir", str(source.parent))
        project = PackProject(
            title=source.stem,
            authors=[getpass.getuser()],
            video_path=str(source),
            video_duration=info.duration,
        )
        self._set_project(project, None, mark_dirty=True)
        self.statusBar().showMessage(f"Loaded {source.name}. Mark a range and add the first segment.")

    def open_project(self) -> None:
        if not self._maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Pack Creator project",
            str(self.settings.value("lastProjectDir", "")),
            "Pack Creator projects (*.cvpack.json *.json)",
        )
        if path:
            self.open_path(Path(path))

    def open_path(self, path: Path) -> None:
        try:
            if path.is_dir():
                result = self.importer.import_folder(path)
                self._set_project(result.project, None, mark_dirty=True)
                self._show_import_warnings(result.warnings)
            else:
                project = ProjectStore.load(path)
                self._set_project(project, path.resolve(), mark_dirty=False)
                self.settings.setValue("lastProjectDir", str(path.resolve().parent))
        except Exception as error:
            QMessageBox.critical(self, "Could not open project", str(error))

    def import_pack(self) -> None:
        if not self._maybe_save():
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose an existing Choicer Voicer pack folder",
            str(self.settings.value("lastPackDir", "")),
        )
        if not folder:
            return
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            result = self.importer.import_folder(Path(folder))
        except Exception as error:
            QMessageBox.critical(self, "Could not import pack", str(error))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.settings.setValue("lastPackDir", str(Path(folder).parent))
        self._set_project(result.project, None, mark_dirty=True)
        self._show_import_warnings(result.warnings)
        self.statusBar().showMessage(
            f"Imported {len(result.project.segments)} segments. Existing prompt media will be preserved."
        )

    def save_project(self, save_as: bool = False) -> bool:
        self._commit_editors()
        destination = self.project_path
        if destination is None or save_as:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save Pack Creator project",
                str(
                    Path(self.settings.value("lastProjectDir", str(Path.home())))
                    / f"{self.project.title}.cvpack.json"
                ),
                "Pack Creator projects (*.cvpack.json)",
            )
            if not path:
                return False
            destination = Path(path)
            if not destination.name.casefold().endswith(".cvpack.json"):
                destination = destination.with_name(destination.name + ".cvpack.json")
        try:
            ProjectStore.save(self.project, destination)
        except Exception as error:
            QMessageBox.critical(self, "Could not save project", str(error))
            return False
        self.project_path = destination.resolve()
        self.settings.setValue("lastProjectDir", str(self.project_path.parent))
        self._set_dirty(False)
        self.statusBar().showMessage(f"Saved project to {self.project_path}")
        return True

    def _set_project(
        self,
        project: PackProject,
        project_path: Path | None,
        mark_dirty: bool,
    ) -> None:
        self.player.stop() if hasattr(self, "player") else None
        self.project = project
        self.project.sort_segments()
        self.project_path = project_path
        self.selected_segment_id = ""
        self._syncing = True
        try:
            self.title_edit.setText(project.title)
            self.authors_edit.setText(", ".join(project.authors))
            self.readme_edit.setPlainText(project.readme)
            self.video_path_label.setText(project.video_path or "No video loaded")
            self.video_path_label.setToolTip(project.video_path)
            self.backing_path_label.setText(Path(project.backing_track_path).name if project.backing_track_path else "None")
            self.backing_path_label.setToolTip(project.backing_track_path)
            self.icon_path_label.setText(Path(project.icon_path).name if project.icon_path else "Generated from video")
            self.icon_path_label.setToolTip(project.icon_path)
            self.head_pad_spin.setValue(project.head_padding)
            self.tail_pad_spin.setValue(project.tail_padding)
            self.height_spin.setValue(project.video_height)
            self.fps_spin.setValue(project.video_fps)
            self.preserve_video_check.setChecked(project.preserve_source_video)
            duration = max(0.1, project.video_duration)
            self.mark_in_spin.setMaximum(duration)
            self.mark_out_spin.setMaximum(duration)
            self.mark_in_spin.setValue(0.0)
            self.mark_out_spin.setValue(min(3.0, duration))
            self.timeline.set_duration(duration)
            self.timeline.set_waveform([])
            self.timeline.set_segments(project.segments)
            self.timeline.set_marks(self.mark_in_spin.value(), self.mark_out_spin.value())
            self._refresh_table()
            self._sync_selected_editor()
        finally:
            self._syncing = False
        if project.video_path and Path(project.video_path).is_file():
            self.player.setSource(QUrl.fromLocalFile(project.video_path))
            self._start_waveform(project.video_path, project.video_duration)
        else:
            self.player.setSource(QUrl())
        self._set_dirty(mark_dirty)
        self._refresh_validation_label()

    # ---------- Media and timeline ----------

    def _start_waveform(self, path: str, duration: float) -> None:
        worker = WaveformWorker(self.media, path, duration)
        self._waveform_workers.append(worker)
        worker.completed.connect(self._waveform_ready)
        worker.failed.connect(self._waveform_failed)
        worker.finished.connect(lambda: self._retire_waveform_worker(worker))
        self.progress_label.setText("Reading waveform…")
        worker.start()

    def _retire_waveform_worker(self, worker: WaveformWorker) -> None:
        if worker in self._waveform_workers:
            self._waveform_workers.remove(worker)
        worker.deleteLater()

    def _waveform_ready(self, path: str, duration: float, peaks: list[float]) -> None:
        if path == self.project.video_path:
            self.project.video_duration = duration
            self.timeline.set_duration(duration)
            self.mark_in_spin.setMaximum(duration)
            self.mark_out_spin.setMaximum(duration)
            self.timeline.set_waveform(peaks)
            self.progress_label.setText(f"Waveform ready · {len(peaks):,} peaks")

    def _waveform_failed(self, path: str, message: str) -> None:
        if path == self.project.video_path:
            self.progress_label.setText("Waveform unavailable")
            self.statusBar().showMessage(message)

    def toggle_playback(self) -> None:
        self.prompt_player.stop()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self._preview_end = None
            self.player.play()

    def stop_playback(self) -> None:
        self.prompt_player.stop()
        self.player.stop()
        self.seek(0.0)

    def seek(self, seconds: float) -> None:
        self.player.setPosition(int(max(0.0, seconds) * 1000))

    def current_position(self) -> float:
        return self.player.position() / 1000.0

    def _player_position_changed(self, milliseconds: int) -> None:
        position = milliseconds / 1000.0
        self.timeline.set_playhead(position)
        if not self._slider_dragging and self.project.video_duration > 0:
            self.seek_slider.setValue(int(position / self.project.video_duration * 100_000))
        self.position_label.setText(
            f"{format_time(position)} / {format_time(self.project.video_duration)}"
        )
        if self._preview_end is not None and position >= self._preview_end:
            self.player.pause()
            self._preview_end = None

    def _player_duration_changed(self, milliseconds: int) -> None:
        if milliseconds <= 0:
            return
        decoded_duration = milliseconds / 1000.0
        if abs(decoded_duration - self.project.video_duration) <= 0.001:
            return
        self.project.video_duration = decoded_duration
        self.timeline.set_duration(self.project.video_duration)
        self.mark_in_spin.setMaximum(self.project.video_duration)
        self.mark_out_spin.setMaximum(self.project.video_duration)

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText(
            "❚❚ Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "▶ Play"
        )

    def _player_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if message:
            self.statusBar().showMessage(f"Video preview error: {message}")

    def _seek_slider_moved(self, value: int) -> None:
        if self.project.video_duration > 0:
            self.timeline.set_playhead(value / 100_000 * self.project.video_duration)

    def _seek_slider_released(self) -> None:
        self._slider_dragging = False
        if self.project.video_duration > 0:
            self.seek(self.seek_slider.value() / 100_000 * self.project.video_duration)

    def _timeline_zoom_changed(self, zoom: float) -> None:
        with QSignalBlocker(self.zoom_slider):
            self.zoom_slider.setValue(round(zoom * 10))

    def _marks_changed(self) -> None:
        if self._syncing:
            return
        start = self.mark_in_spin.value()
        end = self.mark_out_spin.value()
        if end < start:
            sender = self.sender()
            if sender is self.mark_in_spin:
                self.mark_out_spin.setValue(start)
            else:
                self.mark_in_spin.setValue(end)
        self.timeline.set_marks(self.mark_in_spin.value(), self.mark_out_spin.value())

    # ---------- Segment editing ----------

    def add_segment(self) -> None:
        if not self.project.video_path:
            QMessageBox.information(self, "No video", "Load a source video before adding segments.")
            return
        start, end = self.mark_in_spin.value(), self.mark_out_spin.value()
        if end - start < 0.05:
            QMessageBox.warning(self, "Invalid range", "A segment must be at least 0.05 seconds long.")
            return
        existing_speaker = self.project.speakers[0] if self.project.speakers else "Speaker"
        segment = Segment(start=start, end=end, characters=[existing_speaker])
        self.project.add_segment(segment)
        self._set_dirty(True)
        self._refresh_table(segment.id)
        self.select_segment(segment.id)
        self.caption_edit.setFocus()
        self.statusBar().showMessage("Segment added. Enter its speaker and exact line.")

    def apply_selected_range(self) -> None:
        segment = self.selected_segment()
        if segment is None:
            QMessageBox.information(self, "No segment", "Select a segment first.")
            return
        start, end = self.mark_in_spin.value(), self.mark_out_spin.value()
        if end - start < 0.05:
            QMessageBox.warning(self, "Invalid range", "A segment must be at least 0.05 seconds long.")
            return
        segment.start, segment.end = start, end
        if segment.audio_mode == "file":
            answer = QMessageBox.question(
                self,
                "Keep imported audio?",
                "This segment currently preserves an audio file. Keep that audio while changing its "
                "timeline position? Choose No to regenerate it from the video.",
            )
            if answer == QMessageBox.StandardButton.No:
                segment.audio_mode = "video"
                segment.audio_path = ""
                segment.source_range_known = True
        else:
            segment.source_range_known = True
        self.project.sort_segments()
        self._set_dirty(True)
        self._refresh_table(segment.id)
        self.timeline.set_segments(self.project.segments)

    def split_segment(self) -> None:
        segment = self.selected_segment()
        if segment is None:
            QMessageBox.information(self, "No segment", "Select the segment to split.")
            return
        if segment.audio_mode == "file":
            QMessageBox.information(
                self,
                "Set source range first",
                "A preserved recording cannot be split safely because existing packs do not store "
                "its original source-video cut. Mark the exact spoken In/Out range, click Apply "
                "Range, and choose No when asked whether to keep imported audio. Then split it.",
            )
            return
        split_at = self.current_position()
        if not segment.start + 0.05 < split_at < segment.end - 0.05:
            QMessageBox.warning(self, "Cannot split", "Move the playhead inside the selected segment.")
            return
        second = segment.clone()
        segment.end = split_at
        second.start = split_at
        second.image_path = ""
        self.project.add_segment(second)
        self._set_dirty(True)
        self._refresh_table(second.id)
        self.select_segment(second.id)
        self.statusBar().showMessage("Segment split. File audio was switched to source-video audio if needed.")

    def duplicate_segment(self) -> None:
        segment = self.selected_segment()
        if segment is None:
            QMessageBox.information(self, "No segment", "Select a segment to duplicate.")
            return
        duplicate = segment.clone()
        self.project.add_segment(duplicate)
        self._set_dirty(True)
        self._refresh_table(duplicate.id)
        self.select_segment(duplicate.id)
        self.speakers_edit.setFocus()
        self.speakers_edit.selectAll()
        self.statusBar().showMessage(
            "Segment duplicated at the same timestamp—useful for simultaneous speakers."
        )

    def delete_segment(self) -> None:
        segment = self.selected_segment()
        if segment is None:
            return
        if (
            QMessageBox.question(
                self,
                "Delete segment",
                f"Delete {segment.primary_character}: “{segment.caption or 'Untitled line'}”?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.project.remove_segment(segment.id)
        self.selected_segment_id = ""
        self._set_dirty(True)
        self._refresh_table()
        self._sync_selected_editor()

    def preview_segment(self) -> None:
        segment = self.selected_segment()
        if segment is None:
            return
        if segment.audio_mode == "file" and segment.audio_path:
            self.player.pause()
            self._preview_end = None
            self.prompt_player.setSource(QUrl.fromLocalFile(segment.audio_path))
            self.prompt_player.play()
            self.statusBar().showMessage(f"Auditioning preserved prompt: {Path(segment.audio_path).name}")
            return
        self.prompt_player.stop()
        self._preview_end = segment.end
        self.seek(segment.start)
        self.player.play()

    def select_segment(self, segment_id: str) -> None:
        if not self.project.segment_by_id(segment_id):
            return
        self.selected_segment_id = segment_id
        self.timeline.set_selected(segment_id)
        self._select_table_row(segment_id)
        self._sync_selected_editor()
        segment = self.selected_segment()
        if segment:
            self._syncing = True
            try:
                self.mark_in_spin.setValue(segment.start)
                self.mark_out_spin.setValue(segment.end)
            finally:
                self._syncing = False
            self.timeline.set_marks(segment.start, segment.end)

    def selected_segment(self) -> Segment | None:
        return self.project.segment_by_id(self.selected_segment_id)

    def _timeline_boundary_changed(self, segment_id: str, start: float, end: float) -> None:
        segment = self.project.segment_by_id(segment_id)
        if not segment:
            return
        segment.start, segment.end = start, end
        self.selected_segment_id = segment_id
        self._syncing = True
        try:
            self.mark_in_spin.setValue(start)
            self.mark_out_spin.setValue(end)
        finally:
            self._syncing = False
        self.timeline.set_marks(start, end)
        self._set_dirty(True)
        self._refresh_table(segment_id)

    def _refresh_table(self, selected_id: str | None = None) -> None:
        selected = selected_id if selected_id is not None else self.selected_segment_id
        self.segment_table.blockSignals(True)
        try:
            self.segment_table.setRowCount(len(self.project.segments))
            for row, segment in enumerate(self.project.segments):
                values = (
                    f"{row + 1:03d}",
                    format_time(segment.start),
                    format_time(segment.end),
                    ", ".join(segment.characters),
                    segment.caption,
                    "Video" if segment.audio_mode == "video" else Path(segment.audio_path).name,
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, segment.id)
                    if column in {4, 5}:
                        item.setToolTip(value)
                    self.segment_table.setItem(row, column, item)
        finally:
            self.segment_table.blockSignals(False)
        self.timeline.set_segments(self.project.segments)
        if selected:
            self._select_table_row(selected)
        self._refresh_validation_label()

    def _select_table_row(self, segment_id: str) -> None:
        for row in range(self.segment_table.rowCount()):
            item = self.segment_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == segment_id:
                with QSignalBlocker(self.segment_table):
                    self.segment_table.selectRow(row)
                return

    def _table_selection_changed(self) -> None:
        if self._syncing:
            return
        selected = self.segment_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        identifier = self.segment_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if identifier and identifier != self.selected_segment_id:
            self.select_segment(str(identifier))

    def _sync_selected_editor(self) -> None:
        segment = self.selected_segment()
        self._syncing = True
        try:
            enabled = segment is not None
            for widget in (
                self.speakers_edit,
                self.caption_edit,
                self.audio_mode_combo,
            ):
                widget.setEnabled(enabled)
            if not segment:
                self.speakers_edit.clear()
                self.caption_edit.clear()
                self.segment_audio_label.setText("None")
                self.segment_image_label.setText("Generated from video")
                return
            self.speakers_edit.setText(", ".join(segment.characters))
            self.caption_edit.setPlainText(segment.caption)
            index = self.audio_mode_combo.findData(segment.audio_mode)
            self.audio_mode_combo.setCurrentIndex(max(0, index))
            self.segment_audio_label.setText(
                Path(segment.audio_path).name if segment.audio_path else "Generated from video"
            )
            self.segment_audio_label.setToolTip(segment.audio_path)
            self.segment_image_label.setText(
                Path(segment.image_path).name if segment.image_path else "Generated from video"
            )
            self.segment_image_label.setToolTip(segment.image_path)
        finally:
            self._syncing = False

    def _selected_speakers_changed(self) -> None:
        if self._syncing:
            return
        segment = self.selected_segment()
        if not segment:
            return
        names = [item.strip() for item in self.speakers_edit.text().split(",") if item.strip()]
        segment.characters = list(dict.fromkeys(names))
        self._set_dirty(True)
        self._refresh_table(segment.id)

    def _selected_speakers_typed(self) -> None:
        if self._syncing:
            return
        segment = self.selected_segment()
        if not segment:
            return
        segment.characters = list(
            dict.fromkeys(
                item.strip() for item in self.speakers_edit.text().split(",") if item.strip()
            )
        )
        self._set_dirty(True)
        self._refresh_validation_label()

    def _selected_caption_changed(self) -> None:
        if self._syncing:
            return
        segment = self.selected_segment()
        if not segment:
            return
        segment.caption = self.caption_edit.toPlainText()
        self._set_dirty(True)
        row = self._row_for_segment(segment.id)
        if row >= 0:
            self.segment_table.item(row, 4).setText(segment.caption)
            self.segment_table.item(row, 4).setToolTip(segment.caption)
        self.timeline.update()
        self._refresh_validation_label()

    def _audio_mode_changed(self) -> None:
        if self._syncing:
            return
        segment = self.selected_segment()
        if not segment:
            return
        mode = self.audio_mode_combo.currentData()
        if mode == "video":
            self.use_video_audio()
            return
        if mode == "file" and not segment.audio_path:
            self.choose_segment_audio()
            return
        segment.audio_mode = mode
        self._set_dirty(True)
        self._refresh_table(segment.id)

    def _row_for_segment(self, segment_id: str) -> int:
        for row in range(self.segment_table.rowCount()):
            if self.segment_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == segment_id:
                return row
        return -1

    # ---------- Paths and pack details ----------

    def _pack_details_changed(self) -> None:
        if self._syncing:
            return
        self.project.title = self.title_edit.text().strip()
        self.project.authors = [
            item.strip() for item in self.authors_edit.text().split(",") if item.strip()
        ]
        self.project.readme = self.readme_edit.toPlainText()
        self.project.head_padding = self.head_pad_spin.value()
        self.project.tail_padding = self.tail_pad_spin.value()
        self.project.video_height = self.height_spin.value()
        self.project.video_fps = self.fps_spin.value()
        self.project.preserve_source_video = self.preserve_video_check.isChecked()
        self._set_dirty(True)
        self._refresh_validation_label()

    def _video_profile_changed(self) -> None:
        if self._syncing:
            return
        if self.preserve_video_check.isChecked():
            with QSignalBlocker(self.preserve_video_check):
                self.preserve_video_check.setChecked(False)
        self._pack_details_changed()

    def _commit_editors(self) -> None:
        if self._syncing:
            return
        before = self.project.to_dict()
        self.project.title = self.title_edit.text().strip()
        self.project.authors = [
            item.strip() for item in self.authors_edit.text().split(",") if item.strip()
        ]
        self.project.readme = self.readme_edit.toPlainText()
        self.project.head_padding = self.head_pad_spin.value()
        self.project.tail_padding = self.tail_pad_spin.value()
        self.project.video_height = self.height_spin.value()
        self.project.video_fps = self.fps_spin.value()
        self.project.preserve_source_video = self.preserve_video_check.isChecked()
        segment = self.selected_segment()
        if segment:
            segment.characters = list(
                dict.fromkeys(
                    item.strip()
                    for item in self.speakers_edit.text().split(",")
                    if item.strip()
                )
            )
            segment.caption = self.caption_edit.toPlainText()
        if self.project.to_dict() != before:
            self._set_dirty(True)

    def choose_source_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose source video",
            str(Path(self.project.video_path).parent if self.project.video_path else ""),
            "Video files (*.mp4 *.mkv *.mov *.webm *.ogv *.avi);;All files (*)",
        )
        if not path:
            return
        source = Path(path).resolve()
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            info = self.media.probe(source)
        except Exception as error:
            QMessageBox.critical(self, "Could not open video", str(error))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.player.stop()
        self.prompt_player.stop()
        self.project.video_path = str(source)
        self.project.video_duration = info.duration
        self.project.preserve_source_video = (
            source.suffix.casefold() == ".ogv"
            and info.video_codec == "theora"
            and info.audio_codec == "vorbis"
            and info.pixel_format == "yuv420p"
            and info.audio_sample_rate in {44100, 48000}
            and info.audio_channels in {1, 2}
            and 1 <= info.fps <= 120
        )
        if self.project.preserve_source_video:
            self.project.video_height = info.height
            self.project.video_fps = round(info.fps)
            with QSignalBlocker(self.height_spin), QSignalBlocker(self.fps_spin):
                self.height_spin.setValue(info.height)
                self.fps_spin.setValue(round(info.fps))
        self.video_path_label.setText(str(source))
        self.video_path_label.setToolTip(str(source))
        with QSignalBlocker(self.preserve_video_check):
            self.preserve_video_check.setChecked(self.project.preserve_source_video)
        self.mark_in_spin.setMaximum(info.duration)
        self.mark_out_spin.setMaximum(info.duration)
        self.timeline.set_duration(info.duration)
        self.timeline.set_waveform([])
        self.player.setSource(QUrl.fromLocalFile(str(source)))
        self._start_waveform(str(source), info.duration)
        self._set_dirty(True)
        self._refresh_validation_label()
        invalid = [item for item in self.project.segments if item.end > info.duration + 0.05]
        if invalid:
            QMessageBox.warning(
                self,
                "Segments exceed replacement video",
                f"{len(invalid)} segment(s) extend past the new video. Retiming is required before export.",
            )

    def clear_source_video(self) -> None:
        if self.project.segments:
            answer = QMessageBox.warning(
                self,
                "Clear source video",
                "Without a source video the project cannot preview or export. Clear it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.player.stop()
        self.player.setSource(QUrl())
        self.project.video_path = ""
        self.project.video_duration = 0.0
        self.project.preserve_source_video = False
        self.video_path_label.setText("No video loaded")
        self.video_path_label.setToolTip("")
        self.timeline.set_waveform([])
        self.timeline.set_duration(0.1)
        with QSignalBlocker(self.preserve_video_check):
            self.preserve_video_check.setChecked(False)
        self._set_dirty(True)
        self._refresh_validation_label()

    def choose_backing_track(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose optional backing track",
            "",
            "Audio files (*.mp3 *.wav *.ogg *.flac *.m4a);;All files (*)",
        )
        if path:
            self.project.backing_track_path = str(Path(path).resolve())
            self.backing_path_label.setText(Path(path).name)
            self.backing_path_label.setToolTip(path)
            self._set_dirty(True)

    def clear_backing_track(self) -> None:
        self.project.backing_track_path = ""
        self.backing_path_label.setText("None")
        self.backing_path_label.setToolTip("")
        self._set_dirty(True)

    def choose_icon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose pack icon", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if path:
            self.project.icon_path = str(Path(path).resolve())
            self.icon_path_label.setText(Path(path).name)
            self.icon_path_label.setToolTip(path)
            self._set_dirty(True)

    def clear_icon(self) -> None:
        self.project.icon_path = ""
        self.icon_path_label.setText("Generated from video")
        self.icon_path_label.setToolTip("")
        self._set_dirty(True)

    def choose_segment_audio(self) -> None:
        segment = self.selected_segment()
        if not segment:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose prompt audio",
            "",
            "Audio files (*.mp3 *.wav *.ogg *.flac *.m4a);;All files (*)",
        )
        if not path:
            self._sync_selected_editor()
            return
        segment.audio_mode = "file"
        segment.audio_path = str(Path(path).resolve())
        self._set_dirty(True)
        self._sync_selected_editor()
        self._refresh_table(segment.id)

    def use_video_audio(self) -> None:
        segment = self.selected_segment()
        if not segment:
            return
        if not segment.source_range_known:
            QMessageBox.information(
                self,
                "Source range is unknown",
                "Existing packs store the playback timestamp and padded recording, not the original "
                "spoken cut. Mark the exact source-video In/Out range, click Apply Range, and "
                "choose No when asked whether to keep imported audio.",
            )
            self._sync_selected_editor()
            return
        segment.audio_mode = "video"
        segment.audio_path = ""
        self._set_dirty(True)
        self._sync_selected_editor()
        self._refresh_table(segment.id)

    def choose_segment_image(self) -> None:
        segment = self.selected_segment()
        if not segment:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose prompt still", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if path:
            segment.image_path = str(Path(path).resolve())
            self._set_dirty(True)
            self._sync_selected_editor()

    def clear_segment_image(self) -> None:
        segment = self.selected_segment()
        if not segment:
            return
        segment.image_path = ""
        self._set_dirty(True)
        self._sync_selected_editor()

    # ---------- Export and status ----------

    def export_pack(self) -> None:
        self._commit_editors()
        if self.project.import_warnings:
            answer = QMessageBox.warning(
                self,
                "Canonical conversion",
                "This imported pack contains metadata or files outside the canonical format. "
                "They will remain safe in the source folder but will not be copied into the "
                "converted export. Continue?\n\n"
                + "\n".join(f"• {item}" for item in self.project.import_warnings),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        errors = self.project.validate()
        if errors:
            QMessageBox.warning(
                self,
                "Project is not ready",
                "Fix these items before exporting:\n\n" + "\n".join(f"• {item}" for item in errors),
            )
            return
        destination = QFileDialog.getExistingDirectory(
            self,
            "Choose export location",
            str(self.settings.value("lastExportDir", str(Path.home() / "Desktop"))),
        )
        if not destination:
            return
        output_parent = Path(destination).resolve()
        output_folder = output_parent / safe_name(self.project.title)
        output_zip = output_parent / f"{safe_name(self.project.title)}.zip"
        if output_folder.exists() or output_zip.exists():
            answer = QMessageBox.warning(
                self,
                "Replace existing export?",
                f"A pack or ZIP already exists at:\n{output_folder}\n\n"
                "The existing export will be retained as a rollback backup until the new pack "
                "passes final validation. Replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.settings.setValue("lastExportDir", destination)
        self._set_busy(True, "Starting validated export…")
        worker = ExportWorker(self.exporter, self.project, output_parent)
        self._export_worker = worker
        worker.progress.connect(self.progress_label.setText)
        worker.completed.connect(self._export_completed)
        worker.failed.connect(self._export_failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _export_completed(self, value: object) -> None:
        self._set_busy(False, "Export complete")
        self._export_worker = None
        result = value
        if not isinstance(result, ExportResult):
            self._export_failed("Exporter returned an unexpected result")
            return
        message = (
            f"Validated pack folder:\n{result.pack_path}\n\n"
            f"Validated ZIP:\n{result.zip_path}\n\n"
            f"{result.validation['clip_count']} prompts · {result.validation['file_count']} files"
        )
        if result.warnings:
            message += "\n\nCleanup notes:\n" + "\n".join(
                f"• {warning}" for warning in result.warnings
            )
        self.statusBar().showMessage(f"Exported {result.pack_path.name}")
        QMessageBox.information(self, "Pack exported", message)

    def _export_failed(self, message: str) -> None:
        self._set_busy(False, "Export failed")
        self._export_worker = None
        QMessageBox.critical(self, "Export failed", message)

    def _set_busy(self, busy: bool, message: str) -> None:
        self.progress_label.setText(message)
        self.progress_bar.setRange(0, 0 if busy else 1)
        self.progress_bar.setValue(0 if busy else 1)
        self.progress_bar.setVisible(busy)
        for action in (
            self.action_new,
            self.action_open,
            self.action_import,
            self.action_save,
            self.action_save_as,
            self.action_export,
            self.action_add,
            self.action_split,
            self.action_delete,
            self.action_duplicate,
        ):
            action.setEnabled(not busy)
        self.editor_splitter.setEnabled(not busy)

    def _refresh_validation_label(self) -> None:
        errors = self.project.validate()
        if not self.project.segments:
            self.validation_label.setText("No segments yet. Set In/Out points, then add a segment.")
            self.validation_label.setStyleSheet("color: #7f91a8;")
        elif errors:
            self.validation_label.setText(
                f"{len(self.project.segments)} segments · {len(errors)} item(s) need attention before export."
            )
            self.validation_label.setStyleSheet("color: #ffad7a;")
        else:
            self.validation_label.setText(
                f"Ready to export · {len(self.project.segments)} segments · "
                f"{len(self.project.speakers)} speakers"
            )
            self.validation_label.setStyleSheet("color: #66ddb0;")

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        name = self.project_path.name if self.project_path else self.project.title
        self.setWindowTitle(
            f"{'*' if dirty else ''}{name} — Choicer Voicer Pack Creator"
        )

    def _maybe_save(self) -> bool:
        self._commit_editors()
        if not self.dirty:
            return True
        answer = QMessageBox.warning(
            self,
            "Unsaved changes",
            "Save the current project before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return True

    def _show_import_warnings(self, warnings: list[str]) -> None:
        if warnings:
            QMessageBox.information(
                self,
                "Pack imported with notes",
                "The pack was imported. Review these details:\n\n"
                + "\n".join(f"• {item}" for item in warnings),
            )

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Choicer Voicer Pack Creator",
            f"<h3>Choicer Voicer Pack Creator {__version__}</h3>"
            "<p>An unofficial community desktop editor for creating, importing, and validating "
            "Choicer Voicer dub packs.</p>"
            "<p>The desktop interface uses PySide6/Qt. Windows bundles include an unmodified "
            "FFmpeg LGPL shared build for media conversion; its license, provenance, and source "
            "links are in <code>THIRD_PARTY_NOTICES.md</code>.</p>"
            "<p>Godot is <b>not</b> the GUI framework or an end-user dependency. Release tests use "
            "Godot's native <code>ConfigFile</code> parser because The Choicer Voicer is a Godot "
            "application and reads pack metadata with that parser.</p>"
            "<p>Project files store paths and edit decisions only. Source media remains yours.</p>",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._export_worker and self._export_worker.isRunning():
            QMessageBox.information(self, "Export running", "Wait for the current export to finish.")
            event.ignore()
            return
        if not self._maybe_save():
            event.ignore()
            return
        self.player.stop()
        for worker in self._waveform_workers:
            worker.requestInterruption()
            worker.wait(2000)
        event.accept()
