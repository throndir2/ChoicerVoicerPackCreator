from __future__ import annotations

import getpass
from pathlib import Path

from PySide6.QtCore import (
    QSettings,
    QSignalBlocker,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QBrush, QCloseEvent, QColor, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
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
from choicer_voicer_pack_creator.analysis import AnalysisSuggestion
from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.exporter import (
    ExportResult,
    PackExporter,
    safe_name,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import AnalysisReview, PackProject, Segment
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore
from choicer_voicer_pack_creator.timeline_audit import (
    TimelineOverlap,
    audit_timeline_overlaps,
)
from choicer_voicer_pack_creator.ui.analysis_dialog import (
    AnalysisDialog,
    open_diagnostic_logs,
    save_diagnostic_logs,
)
from choicer_voicer_pack_creator.ui.collapsible import CollapsibleSection
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget
from choicer_voicer_pack_creator.ui.update_controller import UpdateController
from choicer_voicer_pack_creator.ui.youtube_dialog import YouTubeDialog


class WaveformWorker(QThread):
    completed = Signal(int, str, float, list)
    failed = Signal(int, str, str)

    def __init__(self, media: MediaTools, request_id: int, path: str, duration: float) -> None:
        super().__init__()
        self.media = media
        self.request_id = request_id
        self.path = path
        self.duration = duration

    def run(self) -> None:
        diagnostic_event(
            "waveform_worker_started", request_id=self.request_id,
            path=self.path, duration_seconds=self.duration,
        )
        try:
            if self.isInterruptionRequested():
                diagnostic_event("waveform_worker_canceled", request_id=self.request_id)
                return
            peaks = self.media.waveform_peaks(
                Path(self.path), self.duration, cancelled=self.isInterruptionRequested
            )
            if self.isInterruptionRequested():
                diagnostic_event("waveform_worker_canceled", request_id=self.request_id)
                return
            diagnostic_event(
                "waveform_worker_completed", request_id=self.request_id, peaks=len(peaks),
            )
            self.completed.emit(self.request_id, self.path, self.duration, peaks)
        except Exception as error:
            diagnostic_exception("waveform_worker_failed", error, request_id=self.request_id)
            self.failed.emit(self.request_id, self.path, str(error))


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
            diagnostic_exception("export_worker_failed", error)
            self.failed.emit(str(error))


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:06.3f}"


class MainWindow(QMainWindow):
    def __init__(
        self,
        media: MediaTools,
        initial_path: Path | None = None,
        *,
        settings: QSettings | None = None,
        recovery_store: RecoveryStore | None = None,
        analysis_data_root: Path | None = None,
    ) -> None:
        super().__init__()
        self.media = media
        self.importer = PackImporter(media)
        self.exporter = PackExporter(media)
        self.settings = settings or QSettings(
            "ChoicerVoicerCommunity", "ChoicerVoicerPackCreator"
        )
        self.recovery_store = recovery_store
        self.analysis_data_root = (
            analysis_data_root.resolve()
            if analysis_data_root
            else Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppLocalDataLocation
                )
            )
            / "analysis"
        )
        self.project = PackProject(authors=[getpass.getuser()])
        self.project_path: Path | None = None
        self.selected_segment_id = ""
        self.dirty = False
        self._syncing = False
        self._slider_dragging = False
        self._preview_end: float | None = None
        self._stopped_seek_active = False
        self._stopped_seek_target_ms = 0
        self._stopped_seek_audio_was_muted = False
        self._waveform_workers: list[WaveformWorker] = []
        self._waveform_request_id = 0
        self._export_worker: ExportWorker | None = None
        self._restoring_layout = False
        self._layout_restored = False
        self._range_edit_record: tuple[str, float, float, bool] | None = None
        self._discard_recovery_on_transition = False
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.setInterval(750)
        self._recovery_timer.timeout.connect(self._write_recovery_snapshot)
        self._layout_save_timer = QTimer(self)
        self._layout_save_timer.setSingleShot(True)
        self._layout_save_timer.setInterval(250)
        self._layout_save_timer.timeout.connect(self._save_layout_state)
        self._stopped_seek_timer = QTimer(self)
        self._stopped_seek_timer.setSingleShot(True)
        self._stopped_seek_timer.setInterval(1250)
        self._stopped_seek_timer.timeout.connect(self._stopped_seek_timed_out)
        self._stopped_seek_debounce_timer = QTimer(self)
        self._stopped_seek_debounce_timer.setSingleShot(True)
        self._stopped_seek_debounce_timer.setInterval(35)
        self._stopped_seek_debounce_timer.timeout.connect(self._start_stopped_seek_decode)

        self.setWindowTitle("Choicer Voicer Pack Creator")
        self.resize(1500, 900)
        self.setMinimumSize(1050, 680)
        self._build_actions()
        self._build_ui()
        self._connect_player()
        self._set_project(self.project, None, mark_dirty=False)
        QTimer.singleShot(0, self._restore_layout_state)

        if initial_path:
            QTimer.singleShot(0, lambda: self._open_initial_path(initial_path))
        elif self.recovery_store:
            QTimer.singleShot(0, self._offer_recovery)

    # ---------- UI construction ----------

    def _build_actions(self) -> None:
        self.action_new = QAction("New from Video…", self)
        self.action_new.setShortcut(QKeySequence.StandardKey.New)
        self.action_new.triggered.connect(self.new_from_video)
        self.action_youtube = QAction("New from YouTube…", self)
        self.action_youtube.triggered.connect(self.new_from_youtube)
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
        self.action_restore_previous = QAction("Restore Previous Save…", self)
        self.action_restore_previous.triggered.connect(self.restore_previous_save)
        self.action_export = QAction("Export Pack + ZIP…", self)
        self.action_export.setShortcut(QKeySequence("Ctrl+E"))
        self.action_export.triggered.connect(self.export_pack)
        self.action_exit = QAction("Exit", self)
        self.action_exit.triggered.connect(self.close)
        self.action_analyze = QAction("Analyze Video && Suggest Segments…", self)
        self.action_analyze.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.action_analyze.triggered.connect(lambda: self.open_analysis_dialog())

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
        file_menu.addActions(
            [self.action_new, self.action_youtube, self.action_open, self.action_import]
        )
        file_menu.addSeparator()
        file_menu.addActions([self.action_save, self.action_save_as, self.action_restore_previous])
        file_menu.addSeparator()
        file_menu.addAction(self.action_export)
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)
        edit_menu = self.menuBar().addMenu("&Segments")
        edit_menu.addActions(
            [self.action_add, self.action_split, self.action_duplicate, self.action_delete]
        )
        tools_menu = self.menuBar().addMenu("&Tools")
        tools_menu.addAction(self.action_analyze)
        help_menu = self.menuBar().addMenu("&Help")
        self.updater = UpdateController(self, help_menu)
        self.action_logs = help_menu.addAction("Open Diagnostic Logs...")
        self.action_logs.triggered.connect(
            lambda: open_diagnostic_logs(self, self.analysis_data_root)
        )
        self.action_save_logs = help_menu.addAction("Save Diagnostic Bundle...")
        self.action_save_logs.triggered.connect(
            lambda: save_diagnostic_logs(self, self.analysis_data_root)
        )
        about = help_menu.addAction("About")
        about.triggered.connect(self.show_about)

    def _build_ui(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        toolbar.addActions(
            [self.action_new, self.action_youtube, self.action_open, self.action_import]
        )
        toolbar.addSeparator()
        toolbar.addActions([self.action_save, self.action_export])
        self.addToolBar(toolbar)

        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 8)
        root_layout.setSpacing(8)
        self.setCentralWidget(root)

        splitter = QSplitter(Qt.Orientation.Horizontal, root)
        splitter.setObjectName("editorSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        self.editor_splitter = splitter
        root_layout.addWidget(splitter, 1)

        left = QFrame(splitter)
        left.setFrameShape(QFrame.Shape.StyledPanel)
        left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
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
        self.timeline.setToolTip(
            "Drag the white playback line or its top arrow to scrub. "
            "Drag IN/OUT handles to trim, drag inside the highlighted waveform range to move it, "
            "or drag across empty waveform space to define a range. Segment blocks can also be "
            "moved by their center or trimmed by their edges."
        )
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.segment_selected.connect(self.select_segment)
        self.timeline.range_edit_started.connect(self._timeline_range_edit_started)
        self.timeline.range_changed.connect(self._timeline_range_changed)
        self.timeline.range_edit_finished.connect(self._timeline_range_edit_finished)
        self.timeline.zoom_changed.connect(self._timeline_zoom_changed)
        left_layout.addWidget(self.timeline)
        timeline_hint = QLabel(
            "Drag the white playback line or its top arrow to scrub. "
            "Drag the waveform highlight or its IN/OUT handles to edit a range. "
            "Drag a segment block to move it, or drag its edges to trim."
        )
        timeline_hint.setObjectName("muted")
        timeline_hint.setWordWrap(True)
        left_layout.addWidget(timeline_hint)

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
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.inspector_splitter = QSplitter(Qt.Orientation.Vertical, right)
        self.inspector_splitter.setChildrenCollapsible(False)
        self.inspector_splitter.setHandleWidth(9)
        right_layout.addWidget(self.inspector_splitter, 1)

        self.project_section = CollapsibleSection("PACK DETAILS", self.inspector_splitter)
        project_content = QWidget(self.project_section)
        project_form = QFormLayout(project_content)
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
        self.project_section.set_content(project_content, scrollable=True)
        self.inspector_splitter.addWidget(self.project_section)

        self.segments_section = CollapsibleSection("SEGMENTS", self.inspector_splitter)
        segment_content = QWidget(self.segments_section)
        segment_layout = QVBoxLayout(segment_content)
        segment_layout.setContentsMargins(0, 0, 0, 0)
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
        self.segments_section.set_content(segment_content)
        self.inspector_splitter.addWidget(self.segments_section)

        self.selected_section = CollapsibleSection("SELECTED SEGMENT", self.inspector_splitter)
        editor_content = QWidget(self.selected_section)
        editor_form = QFormLayout(editor_content)
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
        self.audio_mode_combo.addItem("Extract from source video (rebuilt on export)", "video")
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
        self.segment_audio_help = QLabel()
        self.segment_audio_help.setObjectName("muted")
        self.segment_audio_help.setWordWrap(True)
        editor_form.addRow("", self.segment_audio_help)
        self.selected_section.set_content(editor_content, scrollable=True)
        self.inspector_splitter.addWidget(self.selected_section)

        self.inspector_sections = (
            self.project_section,
            self.segments_section,
            self.selected_section,
        )
        for index, section in enumerate(self.inspector_sections):
            section.collapsed_changed.connect(
                lambda collapsed, section_index=index: self._section_collapsed_changed(
                    section_index, collapsed
                )
            )
            self.inspector_splitter.setStretchFactor(index, 1 if index == 1 else 0)
        self.inspector_splitter.splitterMoved.connect(self._schedule_layout_save)

        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        self.validation_label.setObjectName("muted")
        right_layout.addWidget(self.validation_label)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setCollapsible(0, True)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)
        splitter.splitterMoved.connect(self._schedule_layout_save)

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

    def _setting_is_true(self, key: str) -> bool:
        value = self.settings.value(key, False)
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in {"1", "true", "yes", "on"}

    def _restore_layout_state(self) -> None:
        if self._layout_restored:
            return
        self._restoring_layout = True
        try:
            collapse_keys = (
                "layout/packDetailsCollapsedV1",
                "layout/segmentsCollapsedV1",
                "layout/selectedSegmentCollapsedV1",
            )
            height_keys = (
                "layout/packDetailsExpandedHeightV1",
                "layout/segmentsExpandedHeightV1",
                "layout/selectedSegmentExpandedHeightV1",
            )
            for section, key, height_key in zip(
                self.inspector_sections, collapse_keys, height_keys, strict=True
            ):
                try:
                    saved_height = int(
                        self.settings.value(height_key, section.last_expanded_height)
                    )
                except (TypeError, ValueError):
                    saved_height = section.last_expanded_height
                section.set_collapsed(self._setting_is_true(key))
                section.set_last_expanded_height(min(10_000, saved_height))

            editor_state = self.settings.value("layout/editorSplitterV1")
            if editor_state is None or not self.editor_splitter.restoreState(editor_state):
                self.editor_splitter.setSizes([1030, 470])
            # Saved splitter states also restore the old handle width.
            self.editor_splitter.setHandleWidth(1)

            inspector_state = self.settings.value("layout/inspectorSplitterV1")
            if inspector_state is None or not self.inspector_splitter.restoreState(
                inspector_state
            ):
                self.inspector_splitter.setSizes([285, 355, 235])
        finally:
            self._restoring_layout = False
            self._layout_restored = True

    def _schedule_layout_save(self, *_args: object) -> None:
        if not self._restoring_layout:
            self._layout_save_timer.start()

    def _save_layout_state(self) -> None:
        if self._restoring_layout:
            return
        self.settings.setValue("layout/editorSplitterV1", self.editor_splitter.saveState())
        self.settings.setValue(
            "layout/inspectorSplitterV1", self.inspector_splitter.saveState()
        )
        collapse_keys = (
            "layout/packDetailsCollapsedV1",
            "layout/segmentsCollapsedV1",
            "layout/selectedSegmentCollapsedV1",
        )
        height_keys = (
            "layout/packDetailsExpandedHeightV1",
            "layout/segmentsExpandedHeightV1",
            "layout/selectedSegmentExpandedHeightV1",
        )
        sizes = self.inspector_splitter.sizes()
        for index, (section, key, height_key) in enumerate(
            zip(self.inspector_sections, collapse_keys, height_keys, strict=True)
        ):
            if not section.is_collapsed and index < len(sizes):
                section.set_last_expanded_height(sizes[index])
            self.settings.setValue(key, section.is_collapsed)
            self.settings.setValue(height_key, section.last_expanded_height)
        self.settings.sync()

    def _section_collapsed_changed(self, index: int, collapsed: bool) -> None:
        QTimer.singleShot(
            0,
            lambda: self._rebalance_inspector_sections(index, collapsed),
        )
        self._schedule_layout_save()

    def _rebalance_inspector_sections(self, index: int, collapsed: bool) -> None:
        if not 0 <= index < len(self.inspector_sections):
            return
        if not collapsed:
            self._restore_section_height(index)
            return

        sizes = self.inspector_splitter.sizes()
        if len(sizes) != len(self.inspector_sections):
            return
        total = sum(sizes)
        target = list(sizes)
        expanded_indexes: list[int] = []
        for section_index, section in enumerate(self.inspector_sections):
            if section.is_collapsed:
                target[section_index] = section.minimumHeight()
            else:
                expanded_indexes.append(section_index)
        if not expanded_indexes:
            self.inspector_splitter.setSizes(target)
            return

        available = max(
            0,
            total
            - sum(target[i] for i in range(len(target)) if i not in expanded_indexes),
        )
        if 1 in expanded_indexes:
            # The segment table benefits most from spare vertical space. Preserve the
            # other open editors and give every released pixel to Segments.
            preserved = sum(target[i] for i in expanded_indexes if i != 1)
            target[1] = max(1, available - preserved)
        else:
            current_total = sum(max(1, target[i]) for i in expanded_indexes)
            remainder = available
            for section_index in expanded_indexes[:-1]:
                share = round(available * max(1, target[section_index]) / current_total)
                target[section_index] = max(1, share)
                remainder -= target[section_index]
            target[expanded_indexes[-1]] = max(1, remainder)
        self.inspector_splitter.setSizes(target)

    def _restore_section_height(self, index: int) -> None:
        if not 0 <= index < len(self.inspector_sections):
            return
        sizes = self.inspector_splitter.sizes()
        if len(sizes) != len(self.inspector_sections):
            return
        section = self.inspector_sections[index]
        desired = min(sum(sizes), max(120, section.last_expanded_height))
        needed = max(0, desired - sizes[index])
        sizes[index] += needed
        donors = [
            donor_index
            for donor_index, donor in enumerate(self.inspector_sections)
            if donor_index != index and not donor.is_collapsed
        ]
        donors.sort(key=lambda donor_index: (donor_index != 1, -sizes[donor_index]))
        for donor_index in donors:
            if needed <= 0:
                break
            available = max(0, sizes[donor_index] - 120)
            donated = min(needed, available)
            sizes[donor_index] -= donated
            needed -= donated
        if needed > 0:
            sizes[index] -= needed
        self.inspector_splitter.setSizes(sizes)

    def _connect_player(self) -> None:
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.8)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.video_widget.videoSink().videoFrameChanged.connect(self._video_frame_changed)
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

    def _write_recovery_snapshot(self) -> None:
        if not self.recovery_store or not self.dirty:
            return
        try:
            self.recovery_store.save(self.project, self.project_path)
        except Exception as error:
            self.statusBar().showMessage(f"Could not update recovery snapshot: {error}")

    def _clear_recovery_snapshot(self) -> None:
        self._recovery_timer.stop()
        self._discard_recovery_on_transition = False
        if not self.recovery_store:
            return
        try:
            self.recovery_store.clear()
        except OSError as error:
            self.statusBar().showMessage(f"Could not remove recovery snapshot: {error}")

    def _open_initial_path(self, initial_path: Path) -> None:
        self.open_path(initial_path)
        self._offer_recovery()

    def _offer_recovery(self) -> None:
        if not self.recovery_store:
            return
        try:
            record = self.recovery_store.load()
        except Exception as error:
            QMessageBox.warning(
                self,
                "Recovery snapshot could not be read",
                f"An automatic recovery file exists but is not readable. It has been left "
                f"untouched for manual inspection.\n\n{error}",
            )
            return
        if record is None:
            return
        if self.recovery_store.is_redundant(record):
            self._clear_recovery_snapshot()
            return
        original = str(record.project_path) if record.project_path else "an unsaved new project"
        saved_project_changed = self.recovery_store.saved_project_changed(record)
        conflict_note = (
            "\n\nThe saved project changed after this snapshot was recorded. No is the safer "
            "choice and keeps the newer saved project; choose Yes only to inspect the snapshot "
            "as unsaved edits."
            if saved_project_changed
            else ""
        )
        answer = QMessageBox.question(
            self,
            "Recover unsaved project changes?",
            f"The editor found automatic recovery data for {original}.\n\n"
            f"Snapshot time: {record.created_at_utc or 'unknown'}\n\n"
            "Choose Yes to recover the unsaved edits. The saved project is not overwritten; "
            "after recovery, use Save to replace it or Save As to preserve both versions. "
            f"Choose No to discard this recovery snapshot.{conflict_note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            (
                QMessageBox.StandardButton.No
                if saved_project_changed
                else QMessageBox.StandardButton.Yes
            ),
        )
        if answer == QMessageBox.StandardButton.No:
            self._clear_recovery_snapshot()
            return
        self._set_project(record.project, record.project_path, mark_dirty=True)
        if self.project.segments:
            self.select_segment(self.project.segments[0].id)
        self.statusBar().showMessage(
            "Recovered unsaved edits. Use Save As to preserve the currently saved project."
        )

    def restore_previous_save(self) -> None:
        if self.project_path is None:
            QMessageBox.information(
                self,
                "No saved project",
                "Save this project first. A recoverable previous version is created on the next Save.",
            )
            return
        if not self._maybe_save():
            return
        previous = ProjectStore.previous_path(self.project_path)
        if not previous.is_file():
            QMessageBox.information(
                self,
                "No previous save",
                "No previous saved version exists yet. Saving over this project once will create one.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Restore previous save?",
            "Load the previous saved version as unsaved edits? The current saved project will "
            "remain untouched until you choose Save. Use Save As afterward to preserve both versions.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            project = ProjectStore.load(previous)
        except Exception as error:
            QMessageBox.critical(self, "Could not restore previous save", str(error))
            return
        self._set_project(project, self.project_path, mark_dirty=True)
        if project.segments:
            self.select_segment(project.segments[0].id)
        self.statusBar().showMessage(
            "Previous save loaded as unsaved edits. Use Save or Save As when ready."
        )

    def new_from_video(self) -> None:
        diagnostic_event("new_video_requested")
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
        diagnostic_event("video_import_ready", source=source, duration_seconds=info.duration)
        self.statusBar().showMessage(f"Loaded {source.name}. Mark a range and add the first segment.")
        QTimer.singleShot(
            0, lambda: self.open_analysis_dialog(initial_scan=True, auto_start=True)
        )

    def new_from_youtube(self) -> None:
        diagnostic_event("youtube_import_dialog_requested")
        if not self._maybe_save():
            return
        dialog = YouTubeDialog(
            self.media, str(self.settings.value("lastYouTubeDir", "")), self,
            data_root=self.analysis_data_root,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.download_result is None:
            diagnostic_event("youtube_import_dialog_dismissed")
            return
        result = dialog.download_result
        self.settings.setValue("lastYouTubeDir", str(result.video_path.parent.parent))
        project = PackProject(
            title=result.title,
            authors=[getpass.getuser()],
            video_path=str(result.video_path),
            video_duration=result.duration,
            source_url=result.url,
            caption_language=result.language,
            source_captions=list(result.captions),
            import_warnings=list(result.warnings),
        )
        self._set_project(project, None, mark_dirty=True)
        diagnostic_event(
            "youtube_import_ready", video=result.video_path, duration_seconds=result.duration,
            caption_count=len(result.captions), warning_count=len(result.warnings),
        )
        self.statusBar().showMessage(
            f"Downloaded {result.title}; {len(result.captions)} caption(s) ready for review."
        )
        if result.warnings:
            diagnostic_event("youtube_import_notes_shown", warnings=result.warnings)
            QMessageBox.warning(self, "YouTube import notes", "\n\n".join(result.warnings))
        diagnostic_event("youtube_analysis_handoff_scheduled")
        QTimer.singleShot(
            0, lambda: self.open_analysis_dialog(initial_scan=True, auto_start=True)
        )

    def open_analysis_dialog(
        self, *, initial_scan: bool = False, auto_start: bool = False
    ) -> None:
        diagnostic_event(
            "analysis_dialog_requested", initial_scan=initial_scan, auto_start=auto_start,
        )
        if not self.project.video_path or not Path(self.project.video_path).is_file():
            diagnostic_event("analysis_dialog_blocked", reason="missing_source_video")
            QMessageBox.information(
                self,
                "No source video",
                "Load or relink a source video before running local analysis.",
            )
            return
        dialog = AnalysisDialog(
            self.media,
            Path(self.project.video_path),
            self.project.video_duration,
            self.analysis_data_root,
            len(self.project.segments),
            self,
            initial_scan=initial_scan,
            source_captions=self.project.source_captions,
            caption_language=self.project.caption_language,
            auto_start=auto_start,
            youtube_import=bool(self.project.source_url),
            review=self.project.analysis_review,
        )
        dialog.suggestions_accepted.connect(self._add_analysis_suggestions)
        dialog.preview_requested.connect(self._preview_analysis_range)
        dialog.review_changed.connect(self._save_analysis_review)
        dialog.exec()
        diagnostic_event("analysis_dialog_closed")

    @Slot(object)
    def _save_analysis_review(self, value: object) -> None:
        if not isinstance(value, AnalysisReview):
            QMessageBox.critical(self, "Could not keep analysis draft", "Analysis review data was invalid.")
            return
        if value != self.project.analysis_review:
            self.project.analysis_review = value
            self._set_dirty(True)

    @Slot(float, float)
    def _preview_analysis_range(self, start: float, end: float) -> None:
        self.prompt_player.stop()
        self._cancel_stopped_seek(restore_audio=True)
        self._preview_end = end
        self.player.setPosition(round(start * 1000))
        self.player.play()

    @Slot(object)
    def _add_analysis_suggestions(self, value: object) -> None:
        if not isinstance(value, list) or not all(
            isinstance(item, AnalysisSuggestion) for item in value
        ):
            QMessageBox.critical(self, "Could not add suggestions", "Analysis data was invalid.")
            return
        existing_ranges = {
            (round(segment.start, 3), round(segment.end, 3), segment.caption.casefold())
            for segment in self.project.segments
        }
        added: list[Segment] = []
        for suggestion in value:
            key = (
                round(suggestion.start, 3),
                round(suggestion.end, 3),
                suggestion.caption.casefold(),
            )
            near_existing = any(
                abs(round(existing.start * 1000) - round(suggestion.start * 1000)) <= 50
                and abs(round(existing.end * 1000) - round(suggestion.end * 1000)) <= 50
                for existing in self.project.segments
            )
            if key in existing_ranges or near_existing:
                continue
            segment = Segment(
                start=suggestion.start,
                end=suggestion.end,
                caption=suggestion.caption,
                characters=[],
                audio_mode="video",
                source_range_known=True,
            )
            self.project.segments.append(segment)
            existing_ranges.add(key)
            added.append(segment)
        if not added:
            diagnostic_event("analysis_suggestions_applied", added=0, requested=len(value))
            self.statusBar().showMessage("No new analysis suggestions were added.")
            return
        self.project.sort_segments()
        diagnostic_event("analysis_suggestions_applied", added=len(added), requested=len(value))
        self._set_dirty(True)
        self.segments_section.set_collapsed(False)
        self.selected_section.set_collapsed(False)
        self._refresh_table(added[0].id)
        self.select_segment(added[0].id)
        self.speakers_edit.setFocus()
        self.statusBar().showMessage(
            f"Added {len(added)} review suggestion(s). Assign speakers and verify every caption/range."
        )

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
            previous = ProjectStore.previous_path(path) if not path.is_dir() else None
            if previous and previous.is_file():
                answer = QMessageBox.question(
                    self,
                    "Current project could not be opened",
                    f"{error}\n\nA previous saved version is available. Load it as unsaved "
                    "recovery data? The current file will not be overwritten unless Save is chosen.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    try:
                        recovered = ProjectStore.load(previous)
                        self._set_project(recovered, path.resolve(), mark_dirty=True)
                        self.statusBar().showMessage(
                            "Loaded the previous save as unsaved edits. Use Save As to preserve both files."
                        )
                        return
                    except Exception as previous_error:
                        error = RuntimeError(
                            f"Current project: {error}\n\nPrevious save: {previous_error}"
                        )
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
        original_project_path = self.project_path
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
        replacing_existing = destination.is_file()
        try:
            ProjectStore.save(self.project, destination)
        except Exception as error:
            QMessageBox.critical(self, "Could not save project", str(error))
            return False
        self.project_path = destination.resolve()
        self.settings.setValue("lastProjectDir", str(self.project_path.parent))
        self._clear_recovery_snapshot()
        self._set_dirty(False)
        saved_as_distinct_copy = (
            save_as
            and original_project_path is not None
            and original_project_path.resolve() != self.project_path
        )
        if saved_as_distinct_copy:
            suffix = " A previous target version was also retained." if replacing_existing else ""
            self.statusBar().showMessage(
                f"Saved a new project copy to {self.project_path}; the original was not changed.{suffix}"
            )
        elif replacing_existing:
            self.statusBar().showMessage(
                f"Saved project to {self.project_path} · previous save retained for recovery"
            )
        else:
            self.statusBar().showMessage(f"Saved project to {self.project_path}")
        return True

    def _set_project(
        self,
        project: PackProject,
        project_path: Path | None,
        mark_dirty: bool,
    ) -> None:
        if self._discard_recovery_on_transition:
            self._clear_recovery_snapshot()
        self._cancel_waveform_workers()
        if hasattr(self, "player"):
            self._reset_transport_state()
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
        self._cancel_waveform_workers()
        self._waveform_request_id += 1
        request_id = self._waveform_request_id
        worker = WaveformWorker(self.media, request_id, path, duration)
        self._waveform_workers.append(worker)
        worker.completed.connect(self._waveform_ready)
        worker.failed.connect(self._waveform_failed)
        worker.finished.connect(self._retire_waveform_worker)
        self.progress_label.setText("Reading waveform…")
        worker.start()

    def _cancel_waveform_workers(self) -> None:
        self._waveform_request_id += 1
        for worker in self._waveform_workers:
            worker.requestInterruption()

    @Slot()
    def _retire_waveform_worker(self) -> None:
        worker = self.sender()
        if not isinstance(worker, WaveformWorker):
            return
        if worker in self._waveform_workers:
            self._waveform_workers.remove(worker)
        worker.deleteLater()

    @Slot(int, str, float, list)
    def _waveform_ready(
        self, request_id: int, path: str, duration: float, peaks: list[float]
    ) -> None:
        if request_id == self._waveform_request_id and path == self.project.video_path:
            self.project.video_duration = duration
            self.timeline.set_duration(duration)
            self.mark_in_spin.setMaximum(duration)
            self.mark_out_spin.setMaximum(duration)
            self.timeline.set_waveform(peaks)
            self.progress_label.setText(f"Waveform ready · {len(peaks):,} peaks")

    @Slot(int, str, str)
    def _waveform_failed(self, request_id: int, path: str, message: str) -> None:
        if request_id == self._waveform_request_id and path == self.project.video_path:
            self.progress_label.setText("Waveform unavailable")
            self.statusBar().showMessage(message)

    def toggle_playback(self) -> None:
        self.prompt_player.stop()
        if self._stopped_seek_active:
            self._cancel_stopped_seek(restore_audio=True)
            self.player.play()
            self._playback_state_changed(self.player.playbackState())
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self._preview_end = None
            self.player.play()

    def stop_playback(self) -> None:
        self.prompt_player.stop()
        self._reset_transport_state()
        self.player.pause()
        self.seek(0.0)

    def seek(self, seconds: float) -> None:
        target_ms = int(max(0.0, seconds) * 1000)
        if (
            self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
            or self._stopped_seek_active
        ) and not self.player.source().isEmpty():
            if not self._stopped_seek_active:
                self._stopped_seek_audio_was_muted = self.audio_output.isMuted()
                self.audio_output.setMuted(True)
            self._stopped_seek_active = True
            self._stopped_seek_target_ms = target_ms
            self.player.setPosition(target_ms)
            self._stopped_seek_debounce_timer.start()
            return
        self.player.setPosition(target_ms)

    @Slot()
    def _start_stopped_seek_decode(self) -> None:
        if not self._stopped_seek_active:
            return
        self.player.setPosition(self._stopped_seek_target_ms)
        if self.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            self.player.play()
        self._stopped_seek_timer.start()

    @Slot(QVideoFrame)
    def _video_frame_changed(self, frame: QVideoFrame) -> None:
        if not self._stopped_seek_active or not frame.isValid():
            return
        frame_start_ms = frame.startTime() / 1000 if frame.startTime() >= 0 else -1
        frame_end_ms = frame.endTime() / 1000 if frame.endTime() >= 0 else frame_start_ms
        target_ms = self._stopped_seek_target_ms
        if frame_start_ms >= 0 and (
            frame_start_ms <= target_ms < max(frame_start_ms + 1, frame_end_ms)
            or abs(frame_start_ms - target_ms) <= 15
        ):
            self._finish_stopped_seek()

    @Slot()
    def _finish_stopped_seek(self) -> None:
        if not self._stopped_seek_active:
            return
        target_ms = self._stopped_seek_target_ms
        self._stopped_seek_active = False
        self._stopped_seek_timer.stop()
        self._stopped_seek_debounce_timer.stop()
        self.player.pause()
        self.audio_output.setMuted(self._stopped_seek_audio_was_muted)
        self.timeline.set_playhead(target_ms / 1000.0)
        self._playback_state_changed(self.player.playbackState())

    @Slot()
    def _stopped_seek_timed_out(self) -> None:
        if not self._stopped_seek_active:
            return
        target_ms = self._stopped_seek_target_ms
        self.player.pause()
        self._cancel_stopped_seek(restore_audio=True)
        self.statusBar().showMessage(
            f"Could not decode the preview frame at {target_ms / 1000:.3f}s. Try seeking nearby."
        )

    def _cancel_stopped_seek(self, *, restore_audio: bool = False) -> None:
        was_active = self._stopped_seek_active
        self._stopped_seek_active = False
        self._stopped_seek_timer.stop()
        self._stopped_seek_debounce_timer.stop()
        if restore_audio and was_active:
            self.audio_output.setMuted(self._stopped_seek_audio_was_muted)

    def _reset_transport_state(self) -> None:
        self._preview_end = None
        self._cancel_stopped_seek(restore_audio=True)

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
            "❚❚ Pause"
            if state == QMediaPlayer.PlaybackState.PlayingState
            and not self._stopped_seek_active
            else "▶ Play"
        )

    def _player_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        diagnostic_event("video_player_error", error_code=_error.name, message=message)
        self._reset_transport_state()
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
        original_start, original_end = segment.start, segment.end
        was_dirty = self.dirty
        segment.start, segment.end = start, end
        self._complete_segment_range_edit(
            segment,
            original_start,
            original_end,
            was_dirty,
            review_file_audio=True,
        )

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
                "Range, and choose Yes when asked whether to regenerate prompt audio. Then split it.",
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
            self._reset_transport_state()
            self.player.pause()
            self.prompt_player.setSource(QUrl.fromLocalFile(segment.audio_path))
            self.prompt_player.play()
            self.statusBar().showMessage(f"Auditioning preserved prompt: {Path(segment.audio_path).name}")
            return
        self.prompt_player.stop()
        self._preview_end = segment.end
        self._cancel_stopped_seek(restore_audio=True)
        self.player.setPosition(int(segment.start * 1000))
        self.player.play()

    def select_segment(self, segment_id: str) -> None:
        segment = self.project.segment_by_id(segment_id)
        if not segment:
            return
        self.prompt_player.stop()
        self.selected_segment_id = segment_id
        self.timeline.set_selected(segment_id)
        self._select_table_row(segment_id)
        self._sync_selected_editor()
        self._syncing = True
        try:
            self.mark_in_spin.setValue(segment.start)
            self.mark_out_spin.setValue(segment.end)
        finally:
            self._syncing = False
        self.timeline.set_marks(segment.start, segment.end, segment.id)
        self._preview_end = None
        self.seek(segment.start)

    def selected_segment(self) -> Segment | None:
        return self.project.segment_by_id(self.selected_segment_id)

    def _timeline_range_edit_started(
        self, segment_id: str, original_start: float, original_end: float
    ) -> None:
        self._range_edit_record = (
            segment_id,
            original_start,
            original_end,
            self.dirty,
        )

    def _timeline_range_changed(self, segment_id: str, start: float, end: float) -> None:
        self._syncing = True
        try:
            self.mark_in_spin.setValue(start)
            self.mark_out_spin.setValue(end)
        finally:
            self._syncing = False
        self.timeline.set_marks(start, end, segment_id)
        if not segment_id:
            return
        segment = self.project.segment_by_id(segment_id)
        if not segment:
            return
        segment.start, segment.end = start, end
        self.selected_segment_id = segment_id
        self._set_dirty(True)
        row = self._row_for_segment(segment_id)
        if row >= 0:
            self.segment_table.item(row, 1).setText(format_time(start))
            self.segment_table.item(row, 2).setText(format_time(end))
        self._refresh_validation_label()

    def _timeline_range_edit_finished(
        self,
        segment_id: str,
        original_start: float,
        original_end: float,
        final_start: float,
        final_end: float,
    ) -> None:
        record = self._range_edit_record
        self._range_edit_record = None
        was_dirty = record[3] if record and record[0] == segment_id else self.dirty
        if not segment_id:
            self.statusBar().showMessage(
                f"Range set to {final_start:.3f}–{final_end:.3f}s. Add a segment when ready."
            )
            return
        segment = self.project.segment_by_id(segment_id)
        if not segment:
            return
        segment.start, segment.end = final_start, final_end
        self._complete_segment_range_edit(
            segment,
            original_start,
            original_end,
            was_dirty,
            review_file_audio=(
                abs(final_start - original_start) > 0.0005
                or abs(final_end - original_end) > 0.0005
            ),
        )

    def _complete_segment_range_edit(
        self,
        segment: Segment,
        original_start: float,
        original_end: float,
        was_dirty: bool,
        *,
        review_file_audio: bool,
    ) -> bool:
        changed = (
            abs(segment.start - original_start) > 0.0005
            or abs(segment.end - original_end) > 0.0005
        )
        if not changed and not review_file_audio:
            self._restore_dirty_after_canceled_range(was_dirty)
            self._refresh_table(segment.id)
            return False

        audio_result = "preserve" if segment.audio_mode == "file" else "regenerate"
        if segment.audio_mode == "file" and review_file_audio:
            if not self.project.video_path or not Path(self.project.video_path).is_file():
                QMessageBox.warning(
                    self,
                    "Source video unavailable",
                    "The range changed, but the source video is unavailable, so the preserved "
                    "prompt audio cannot be regenerated yet. The existing file will remain "
                    "unchanged. Relink the video before switching this prompt to source audio.",
                )
                audio_result = "preserve"
            else:
                answer = QMessageBox.question(
                    self,
                    "Update prompt audio for this range?",
                    f"The range changed from {original_start:.3f}–{original_end:.3f}s to "
                    f"{segment.start:.3f}–{segment.end:.3f}s, but this segment currently uses "
                    "a preserved audio file.\n\n"
                    "Yes — regenerate the prompt MP3 from the source video on the next export.\n"
                    "No — keep the existing recording unchanged; Out will match its decoded duration.\n"
                    "Cancel — undo this range edit.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if answer == QMessageBox.StandardButton.Cancel:
                    segment.start, segment.end = original_start, original_end
                    self._restore_dirty_after_canceled_range(was_dirty)
                    self.project.sort_segments()
                    self._refresh_table(segment.id)
                    self.select_segment(segment.id)
                    self.statusBar().showMessage("Range edit undone; preserved audio was not changed.")
                    return False
                audio_result = (
                    "regenerate" if answer == QMessageBox.StandardButton.Yes else "preserve"
                )

        if segment.audio_mode == "video" or audio_result == "regenerate":
            segment.audio_mode = "video"
            segment.audio_path = ""
            segment.source_range_known = True
        elif audio_result == "preserve" and segment.audio_path:
            try:
                segment.end = round(
                    segment.start + self.media.probe_audio_duration(Path(segment.audio_path)),
                    3,
                )
            except Exception as error:
                QMessageBox.warning(
                    self,
                    "Could not measure preserved audio",
                    f"The recording remains selected, but its exact duration could not be read. "
                    f"The edited Out time was retained.\n\n{error}",
                )

        self.project.sort_segments()
        self._set_dirty(True)
        self._refresh_table(segment.id)
        self.select_segment(segment.id)
        if segment.audio_mode == "video":
            self.statusBar().showMessage(
                "Segment range updated. Its prompt MP3 will be regenerated on the next export."
            )
        else:
            self.statusBar().showMessage(
                "Segment range updated; the selected prompt audio file will remain unchanged."
            )
        return True

    def _restore_dirty_after_canceled_range(self, was_dirty: bool) -> None:
        self._set_dirty(was_dirty)
        if was_dirty:
            self._recovery_timer.start()
        else:
            self._clear_recovery_snapshot()

    def _refresh_table(self, selected_id: str | None = None) -> None:
        selected = selected_id if selected_id is not None else self.selected_segment_id
        timeline_warnings = audit_timeline_overlaps(self.project.segments)
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
            self._apply_timeline_review_highlights(timeline_warnings)
        finally:
            self.segment_table.blockSignals(False)
        self.timeline.set_segments(self.project.segments)
        if selected:
            self._select_table_row(selected)
        self._refresh_validation_label()

    def _apply_timeline_review_highlights(self, warnings: list[TimelineOverlap]) -> None:
        warning_ids = {
            segment_id
            for warning in warnings
            for segment_id in (warning.first_id, warning.second_id)
        }
        if not warning_ids:
            return
        brush = QBrush(QColor("#49351d"))
        for row in range(self.segment_table.rowCount()):
            identity = self.segment_table.item(row, 0)
            if not identity or identity.data(Qt.ItemDataRole.UserRole) not in warning_ids:
                continue
            for column in range(self.segment_table.columnCount()):
                item = self.segment_table.item(row, column)
                if item:
                    item.setBackground(brush)
                    existing = item.toolTip()
                    note = "Potential timeline overlap — review against the source."
                    item.setToolTip(f"{existing}\n\n{note}".strip())

    def _timeline_review_details(self, warnings: list[TimelineOverlap]) -> list[str]:
        indexes = {segment.id: index for index, segment in enumerate(self.project.segments, 1)}
        details: list[str] = []
        for warning in warnings:
            first = self.project.segment_by_id(warning.first_id)
            second = self.project.segment_by_id(warning.second_id)
            if not first or not second:
                continue
            details.append(
                f"Segments {indexes[first.id]:03d} ({first.primary_character}) and "
                f"{indexes[second.id]:03d} ({second.primary_character}) overlap by "
                f"{warning.seconds:.3f}s."
            )
        return details

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
                self.segment_audio_help.setText(
                    "Select a segment to edit its range, prompt source, and still image."
                )
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
            if segment.audio_mode == "video":
                self.segment_audio_help.setText(
                    "Range edits automatically rebuild this prompt MP3 from the source video "
                    "during the next export."
                )
            else:
                self.segment_audio_help.setText(
                    "This audio file is preserved unchanged. Editing the range will ask whether "
                    "to keep it or regenerate from the source video."
                )
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
        self._reset_transport_state()
        self.player.stop()
        self.prompt_player.stop()
        if str(source) != self.project.video_path:
            self.project.source_url = ""
            self.project.caption_language = ""
            self.project.source_captions = []
            self.project.analysis_review = None
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
        self._reset_transport_state()
        self.player.stop()
        self._cancel_waveform_workers()
        self.player.setSource(QUrl())
        self.project.video_path = ""
        self.project.source_url = ""
        self.project.caption_language = ""
        self.project.source_captions = []
        self.project.analysis_review = None
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
                "choose Yes when asked whether to regenerate prompt audio.",
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

    @Slot(object)
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

    @Slot(str)
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
            self.action_youtube,
            self.action_open,
            self.action_import,
            self.action_save,
            self.action_save_as,
            self.action_restore_previous,
            self.action_analyze,
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
        timeline_warnings = audit_timeline_overlaps(self.project.segments)
        warning_details = self._timeline_review_details(timeline_warnings)
        self.validation_label.setToolTip("\n".join(warning_details))
        if not self.project.segments:
            self.validation_label.setText("No segments yet. Set In/Out points, then add a segment.")
            self.validation_label.setStyleSheet("color: #7f91a8;")
        elif errors:
            self.validation_label.setText(
                f"{len(self.project.segments)} segments · {len(errors)} item(s) need attention"
                + (
                    f" · {len(timeline_warnings)} overlap(s) to review"
                    if timeline_warnings
                    else ""
                )
            )
            self.validation_label.setStyleSheet("color: #ffad7a;")
        elif timeline_warnings:
            self.validation_label.setText(
                f"Ready to export · {len(self.project.segments)} segments · "
                f"{len(timeline_warnings)} potential overlap(s) to review"
            )
            self.validation_label.setStyleSheet("color: #ffbf69;")
        else:
            self.validation_label.setText(
                f"Ready to export · {len(self.project.segments)} segments · "
                f"{len(self.project.speakers)} speakers"
            )
            self.validation_label.setStyleSheet("color: #66ddb0;")

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        if dirty and self.recovery_store:
            self._discard_recovery_on_transition = False
            self._recovery_timer.start()
        name = self.project_path.name if self.project_path else self.project.title
        self.setWindowTitle(
            f"{'*' if dirty else ''}{name} — Choicer Voicer Pack Creator"
        )

    def _maybe_save(self) -> bool:
        self._discard_recovery_on_transition = False
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
        self._discard_recovery_on_transition = True
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
            "<p>Optional video analysis uses deterministic audio-energy scanning and can download "
            "a pinned local whisper.cpp CPU runtime/model. No media is uploaded. Transcripts and "
            "timestamps are editable suggestions, never correctness claims.</p>"
            "<p>Project files store paths and edit decisions only. Source media remains yours.</p>",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        diagnostic_event("window_close_requested", dirty=self.dirty)
        if not self.updater.can_close():
            event.ignore()
            return
        if self._export_worker and self._export_worker.isRunning():
            QMessageBox.information(self, "Export running", "Wait for the current export to finish.")
            event.ignore()
            return
        if not self._maybe_save():
            event.ignore()
            return
        self._save_layout_state()
        self._reset_transport_state()
        self.player.stop()
        for worker in self._waveform_workers:
            worker.requestInterruption()
        for worker in self._waveform_workers:
            if not worker.wait(3500):
                QMessageBox.warning(
                    self,
                    "Waveform analysis is stopping",
                    "The source-media analyzer is still shutting down. Wait a moment, then close again.",
                )
                event.ignore()
                return
        if not self.updater.install_on_close():
            event.ignore()
            return
        if self._discard_recovery_on_transition:
            self._clear_recovery_snapshot()
        diagnostic_event("window_close_accepted")
        event.accept()
