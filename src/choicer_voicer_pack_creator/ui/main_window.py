from __future__ import annotations

import getpass
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QItemSelectionModel,
    QSettings,
    QSignalBlocker,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QBrush, QCloseEvent, QColor, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame
from PySide6.QtWidgets import (
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
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from choicer_voicer_pack_creator import __version__
from choicer_voicer_pack_creator.analysis import AnalysisSuggestion
from choicer_voicer_pack_creator.diagnostics import diagnostic_event, diagnostic_exception
from choicer_voicer_pack_creator.export_progress import ExportProgress, format_time
from choicer_voicer_pack_creator.exporter import (
    ExportResult,
    PackExporter,
    safe_name,
    sha256,
)
from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import AnalysisReview, PackProject, Segment
from choicer_voicer_pack_creator.operations import OperationCancelled
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.project_io import ProjectStore, RecoveryStore, WorkspaceStore
from choicer_voicer_pack_creator.project_session import ProjectSession, canonical_project_path
from choicer_voicer_pack_creator.timeline_audit import (
    TimelineOverlap,
    audit_timeline_overlaps,
)
from choicer_voicer_pack_creator.ui.analysis_dialog import (
    AnalysisDialog,
    open_diagnostic_logs,
    save_diagnostic_logs,
)
from choicer_voicer_pack_creator.ui.backing_dialog import BackingDialog
from choicer_voicer_pack_creator.ui.collapsible import CollapsibleSection
from choicer_voicer_pack_creator.ui.export_dialog import ExportProgressDialog
from choicer_voicer_pack_creator.ui.job_worker import JobWorker
from choicer_voicer_pack_creator.ui.readable_table import ReadableTableWidget
from choicer_voicer_pack_creator.ui.setup_consent import SetupConsent
from choicer_voicer_pack_creator.ui.subtitles import SubtitleVideoWidget
from choicer_voicer_pack_creator.ui.tasks_panel import TasksPanel
from choicer_voicer_pack_creator.ui.timeline import TimelineWidget
from choicer_voicer_pack_creator.ui.update_controller import UpdateController
from choicer_voicer_pack_creator.ui.youtube_dialog import YouTubeDialog


class WaveformWorker(JobWorker):
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

    def _emit_job_failure(self, message: str) -> None:
        self.failed.emit(self.request_id, self.path, message)


class ExportWorker(JobWorker):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    canceled = Signal()

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
            if not isinstance(result, ExportResult):
                raise TypeError("Exporter returned an unexpected result")
            self.completed.emit(result)
        except OperationCancelled:
            self.canceled.emit()
        except Exception as error:
            diagnostic_exception("export_worker_failed", error)
            self.failed.emit(str(error))


class ProjectEditor(QWidget):
    def __init__(
        self,
        media: MediaTools,
        initial_path: Path | None = None,
        *,
        settings: QSettings | None = None,
        recovery_store: RecoveryStore | None = None,
        analysis_data_root: Path | None = None,
        workspace: MainWindow,
        session: ProjectSession,
    ) -> None:
        super().__init__(workspace)
        self.setObjectName("projectEditor")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.workspace = workspace
        self.session = session
        self._document_layout = QVBoxLayout(self)
        self._document_layout.setContentsMargins(0, 0, 0, 0)
        self._menu_bar = QMenuBar(self)
        self._document_layout.addWidget(self._menu_bar)
        self._status_bar = QStatusBar(self)
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
        self.project = session.project
        self.project_path = session.path
        self._saved_project_hash: str | None = None
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
        self._export_dialog: ExportProgressDialog | None = None
        self._backing_dialog: BackingDialog | None = None
        self._analysis_dialog: AnalysisDialog | None = None
        self._source_request = 0
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

        self._build_actions()
        self._build_ui()
        self._connect_player()
        self._set_project(self.project, self.project_path, mark_dirty=session.dirty)
        self._document_layout.addWidget(self._status_bar)
        QTimer.singleShot(0, self._restore_layout_state)

        if initial_path:
            QTimer.singleShot(0, lambda: self._open_initial_path(initial_path))

    def menuBar(self) -> QMenuBar:  # noqa: N802
        return self._menu_bar

    def statusBar(self) -> QStatusBar:  # noqa: N802
        return self._status_bar

    def addToolBar(self, toolbar: QToolBar) -> None:  # noqa: N802
        self._document_layout.addWidget(toolbar)

    def setCentralWidget(self, widget: QWidget) -> None:  # noqa: N802
        scroll = QScrollArea(self)
        scroll.setObjectName("projectEditorScroll")
        scroll.verticalScrollBar().setObjectName("projectEditorScrollbar")
        scroll.verticalScrollBar().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        self.editor_scroll = scroll
        self._document_layout.addWidget(scroll, 1)

    @property
    def project(self) -> PackProject:
        return self.session.project

    @project.setter
    def project(self, value: PackProject) -> None:
        self.session.project = value

    @property
    def project_path(self) -> Path | None:
        return self.session.path

    @project_path.setter
    def project_path(self, value: Path | None) -> None:
        self.session.path = value

    @property
    def dirty(self) -> bool:
        return self.session.dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        if value:
            self.session.revision += 1
            self.workspace._exit_discarded.discard(self.session.id)
        else:
            self.session.saved_revision = self.session.revision

    @property
    def updater(self) -> UpdateController:
        return self.workspace.updater

    @property
    def _automation_active(self) -> bool:
        return self.workspace._automation_active

    @property
    def _automation_disconnected(self) -> bool:
        return self.workspace._automation_disconnected

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
        self.action_clear_recent = QAction("Clear Recent Projects", self)
        self.action_clear_recent.triggered.connect(lambda: self._set_recent_project_paths([]))
        self.action_import = QAction("Import Existing Pack…", self)
        self.action_import.setShortcut(QKeySequence("Ctrl+I"))
        self.action_import.triggered.connect(self.import_pack)
        self.action_import_zip = QAction("Import Pack ZIP...", self)
        self.action_import_zip.triggered.connect(self.import_pack_zip)
        self.action_save = QAction("Save Project", self)
        self.action_save.setObjectName("saveProject")
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)
        self.action_save.triggered.connect(self.save_project)
        self.action_save_as = QAction("Save Project As…", self)
        self.action_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.action_save_as.triggered.connect(lambda: self.save_project(save_as=True))
        self.action_restore_previous = QAction("Restore Previous Save…", self)
        self.action_restore_previous.triggered.connect(self.restore_previous_save)
        self.action_export = QAction("Export Pack + ZIP…", self)
        self.action_export.setObjectName("exportProject")
        self.action_export.setShortcut(QKeySequence("Ctrl+E"))
        self.action_export.triggered.connect(self.export_pack)
        self.action_exit = QAction("Exit", self)
        self.action_exit.triggered.connect(self.workspace.close)
        self.action_analyze = QAction("Analyze Video && Suggest Segments…", self)
        self.action_analyze.setObjectName("analyzeProject")
        self.action_analyze.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.action_analyze.triggered.connect(lambda: self.open_analysis_dialog())
        self.action_backing = QAction("Generate Backing Track...", self)
        self.action_backing.triggered.connect(lambda: self.generate_backing_track())

        self.action_add = QAction("Add Segment", self)
        self.action_add.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.action_add.triggered.connect(self.add_segment)
        self.action_split = QAction("Split at Playhead", self)
        self.action_split.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.action_split.triggered.connect(self.split_segment)
        self.action_combine = QAction("Combine Selected Segments", self)
        self.action_combine.setShortcut(QKeySequence("Ctrl+Shift+M"))
        self.action_combine.setToolTip(
            "Select multiple rows with Ctrl or Shift, then combine their ranges and lines."
        )
        self.action_combine.triggered.connect(self.combine_segments)
        self.action_delete = QAction("Delete Segment", self)
        self.action_delete.setShortcuts(
            [QKeySequence(Qt.Key.Key_Backspace), QKeySequence("Ctrl+Delete")]
        )
        self.action_delete.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.addAction(self.action_delete)
        self.action_delete.setAutoRepeat(False)
        self.action_delete.triggered.connect(self.delete_segment)
        self.action_duplicate = QAction("Duplicate Segment", self)
        self.action_duplicate.setShortcut(QKeySequence("Ctrl+D"))
        self.action_duplicate.triggered.connect(self.duplicate_segment)

        file_menu = self.menuBar().addMenu("&File")
        file_menu.addActions([self.action_new, self.action_youtube, self.action_open])
        self.recent_projects_menu = file_menu.addMenu("Open &Recent")
        self.recent_projects_menu.setToolTipsVisible(True)
        self.recent_projects_menu.aboutToShow.connect(self._refresh_recent_projects_menu)
        for editor in self.workspace.editors.values():
            editor._refresh_recent_projects_menu()
        file_menu.addActions([self.action_import, self.action_import_zip])
        file_menu.addSeparator()
        file_menu.addActions([self.action_save, self.action_save_as, self.action_restore_previous])
        file_menu.addSeparator()
        file_menu.addAction(self.action_export)
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)
        edit_menu = self.menuBar().addMenu("&Segments")
        edit_menu.addActions(
            [
                self.action_add, self.action_split, self.action_combine,
                self.action_duplicate, self.action_delete,
            ]
        )
        tools_menu = self.menuBar().addMenu("&Tools")
        tools_menu.addAction(self.action_analyze)
        tools_menu.addAction(self.action_backing)
        tools_menu.addAction(self.workspace.tasks_panel.toggleViewAction())
        help_menu = self.menuBar().addMenu("&Help")
        self.help_menu = help_menu
        self.action_mcp_help = help_menu.addAction("LLM / MCP Help")
        self.action_mcp_help.triggered.connect(self.show_mcp_help)
        if hasattr(self.workspace, "updater"):
            help_menu.addActions([
                self.updater.check_action, self.updater.auto_action,
                self.updater.prerelease_action,
            ])
            help_menu.addSeparator()
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
        root.setObjectName("projectEditorContent")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
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
        left.setObjectName("projectEditorPanel")
        left.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        left.setFrameShape(QFrame.Shape.StyledPanel)
        left.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(7, 7, 7, 7)
        left_layout.setSpacing(7)

        self.video_widget = SubtitleVideoWidget(left)
        self.video_widget.setObjectName("videoPreview")
        self.video_widget.setMinimumHeight(96)
        self.video_widget.setStyleSheet(
            "QGraphicsView#videoPreview { background: #000; border: 1px solid #26384d; }"
        )
        left_layout.addWidget(self.video_widget, 1)

        transport = QHBoxLayout()
        self.play_button = QPushButton("▶ Play")
        self.play_button.setToolTip("Play / pause (Space)")
        self.play_button.clicked.connect(self.toggle_playback)
        self.play_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.play_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.play_shortcut.setAutoRepeat(False)
        self.play_shortcut.activated.connect(self.toggle_playback)
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
        self.title_edit.setObjectName("projectTitle")
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
        self.generate_backing_button = QPushButton("Generate backing...")
        self.generate_backing_button.setToolTip(
            "Create music/effects backing from the video without changing dialogue or prompt files."
        )
        self.generate_backing_button.clicked.connect(lambda: self.generate_backing_track())
        project_form.addRow("", self.generate_backing_button)
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
        self.project_section.set_content(
            project_content, scrollable=True, scrollbar_name="projectDetailsScrollbar",
        )
        self.inspector_splitter.addWidget(self.project_section)

        self.segments_section = CollapsibleSection("SEGMENTS", self.inspector_splitter)
        segment_content = QWidget(self.segments_section)
        segment_layout = QVBoxLayout(segment_content)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        self.segment_table = ReadableTableWidget(0, 6)
        self.segment_table.setObjectName("segmentsTable")
        self.segment_table.setHorizontalHeaderLabels(
            ["#", "In", "Out", "Speaker(s)", "Line", "Audio"]
        )
        self.segment_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.segment_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.segment_table.setToolTip("Use Ctrl-click or Shift-click to select segments to combine.")
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
        self.segment_table.cellClicked.connect(
            lambda _row, _column: self.select_segment(self.selected_segment_id)
        )
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
        self.combine_button = QPushButton("Combine")
        self.combine_button.setToolTip(self.action_combine.toolTip())
        self.combine_button.clicked.connect(self.action_combine.trigger)
        self.action_combine.changed.connect(
            lambda: self.combine_button.setEnabled(self.action_combine.isEnabled())
        )
        row_buttons.addWidget(self.combine_button)
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
        self.caption_edit.setObjectName("segmentCaption")
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
        self.selected_section.set_content(
            editor_content, scrollable=True, scrollbar_name="selectedSegmentScrollbar",
        )
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
        self.player.setVideoSink(self.video_widget.videoSink())
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

    def _recent_project_paths(self) -> list[Path]:
        return [
            Path(value) for value in self.settings.value("recentProjects", [], type=list)
        ][:10]

    def _set_recent_project_paths(self, paths: list[Path]) -> None:
        self.settings.setValue("recentProjects", [str(path) for path in paths[:10]])
        self.settings.sync()
        self._refresh_recent_projects_menu()
        if self.settings.status() != QSettings.Status.NoError:
            QMessageBox.warning(
                self,
                "Could not save recent projects",
                "The recent-project list could not be saved to your application settings. "
                "Project files are not affected.\n\n"
                f"Settings location: {self.settings.fileName()}",
            )

    def _remember_recent_project(self, path: Path) -> None:
        path = path.resolve()
        self._set_recent_project_paths(
            [path] + [recent for recent in self._recent_project_paths() if recent != path]
        )

    def _refresh_recent_projects_menu(self) -> None:
        self.recent_projects_menu.clear()
        paths = self._recent_project_paths()
        for index, path in enumerate(paths, start=1):
            label = f"{index}. {path.name} ({path.parent})".replace("&", "&&")
            action = self.recent_projects_menu.addAction(label)
            action.setData(str(path))
            action.setToolTip(str(path))
            action.setStatusTip(str(path))
            action.triggered.connect(
                lambda _checked=False, path=path: self._open_recent_project(path)
            )
        if not paths:
            self.recent_projects_menu.addAction("No Recent Projects").setEnabled(False)
        self.recent_projects_menu.addSeparator()
        self.action_clear_recent.setEnabled(bool(paths))
        self.recent_projects_menu.addAction(self.action_clear_recent)

    def _open_recent_project(self, path: Path) -> None:
        self.workspace.open_path(path)

    def _write_recovery_snapshot(self) -> None:
        if not self.recovery_store or not self.dirty:
            return
        snapshot, path, store = self.session.snapshot(), self.project_path, self.recovery_store
        job = self.workspace.job_manager.submit(
            self.session.id, "recovery", "Saving recovery",
            lambda _ctx: store.save(snapshot, path), resource_class="io",
            resource_keys=(f"document-save:{self.session.id}",),
            write_paths=(store.path, store.previous_path),
            read_paths=(path,) if path else (),
            source_snapshot={"revision": self.session.revision},
        )
        job.failed.connect(lambda message: self.statusBar().showMessage(
            f"Could not update recovery snapshot: {message}"
        ))

    def _clear_recovery_snapshot(self) -> None:
        self._recovery_timer.stop()
        self._discard_recovery_on_transition = False
        if not self.recovery_store:
            return
        store = self.recovery_store
        job = self.workspace.job_manager.submit(
            self.session.id, "recovery", "Clearing saved recovery",
            lambda _ctx: store.clear(), resource_class="io",
            resource_keys=(f"document-save:{self.session.id}",),
            write_paths=(store.path, store.previous_path),
        )
        job.failed.connect(lambda message: self.statusBar().showMessage(
            f"Could not remove recovery snapshot: {message}"
        ))

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
        if self._automation_disconnected:
            return
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
        self.workspace.offer_previous_save(self, self.project_path)

    def new_from_video(self) -> None:
        self.workspace.new_from_video()

    def _choose_new_video(self) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose source video",
            str(self.settings.value("lastVideoDir", "")),
            "Video files (*.mp4 *.mkv *.mov *.webm *.ogv *.avi);;All files (*)",
        )
        if not path:
            return None
        return Path(path).resolve()

    def new_from_youtube(self) -> None:
        diagnostic_event("youtube_import_dialog_requested")
        self.workspace.new_from_youtube()

    def _youtube_ready(self, dialog: YouTubeDialog) -> None:
        if dialog.download_result is None or self.session.id in self.workspace._closed_ids:
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
        self.statusBar().showMessage(
            f"Downloaded {result.title}; {len(result.captions)} caption(s) ready for review."
        )
        if result.warnings:
            self.workspace.notice("YouTube import notes", "\n\n".join(result.warnings))
        QTimer.singleShot(0, lambda: self._finish_new_import(project))

    def _start_youtube_import(self) -> None:
        dialog = YouTubeDialog(
            self.media, str(self.settings.value("lastYouTubeDir", "")), self,
            data_root=self.analysis_data_root,
            job_manager=self.workspace.job_manager, project_id=self.session.id,
            source_snapshot={"source_revision": self.session.source_revision},
        )
        self._youtube_dialog = dialog
        token = self.session.source_token()
        dialog.accepted.connect(
            lambda: self._youtube_ready(dialog) if token == self.session.source_token() else None
        )
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.show()

    def _finish_new_import(self, project: PackProject) -> None:
        if self.project is not project:
            diagnostic_event("new_import_handoff_skipped", reason="project_changed")
            return
        if self.project is project:
            self.open_analysis_dialog(initial_scan=True, auto_start=True)
        if not project.backing_track_path:
            self.generate_backing_track()

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
        if self._analysis_dialog is not None:
            self._analysis_dialog.show()
            self._analysis_dialog.raise_()
            return
        token = self.session.source_token()
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
            job_manager=self.workspace.job_manager, project_id=self.session.id,
            source_snapshot={"source_revision": self.session.source_revision},
        )
        self._analysis_dialog = dialog
        dialog.suggestions_accepted.connect(
            lambda value: self._add_analysis_suggestions(value)
            if token == self.session.source_token() and self._analysis_dialog is dialog else None
        )
        dialog.preview_requested.connect(
            lambda start, end: self._preview_analysis_range(start, end)
            if token == self.session.source_token() and self._analysis_dialog is dialog else None
        )
        dialog.review_changed.connect(
            lambda value: self._save_analysis_review(value)
            if token == self.session.source_token() and self._analysis_dialog is dialog else None
        )
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.show()

    @Slot(object)
    def _save_analysis_review(self, value: object) -> None:
        if not isinstance(value, AnalysisReview):
            QMessageBox.critical(self, "Could not keep analysis draft", "Analysis review data was invalid.")
            return
        if value != self.project.analysis_review:
            self.project.analysis_review = value
            self.session.draft_revision += 1
            self._set_dirty(True)

    @Slot(float, float)
    def _preview_analysis_range(self, start: float, end: float) -> None:
        self.workspace.pause_other_previews(self)
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
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Pack Creator project",
            str(self.settings.value("lastProjectDir", "")),
            "Pack Creator projects (*.cvpack.json *.json)",
        )
        if path:
            self.workspace.open_path(Path(path))

    def open_path(self, path: Path) -> None:
        self.workspace.open_path(path)

    def import_pack(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose an existing Choicer Voicer pack folder",
            str(self.settings.value("lastPackDir", "")),
        )
        if not folder:
            return
        self.settings.setValue("lastPackDir", str(Path(folder).parent))
        self.workspace.open_path(Path(folder))

    def import_pack_zip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import an existing Choicer Voicer pack ZIP",
            str(self.settings.value("lastPackDir", "")),
            "Choicer Voicer packs (*.zip)",
        )
        if not path:
            return
        self.settings.setValue("lastPackDir", str(Path(path).parent))
        self.workspace.open_path(Path(path))

    def _show_pack_recovery_hint(self) -> None:
        self.statusBar().showMessage(
            f"Imported {len(self.project.segments)} segments with existing prompt media. "
            "Missing music? Use Generate backing, then Save Project As and export to a new location."
        )

    def save_project(self, save_as: bool = False) -> bool:
        return self.workspace.save_editor(self, save_as=save_as)

    def _set_project(
        self,
        project: PackProject,
        project_path: Path | None,
        mark_dirty: bool,
        *,
        preserve_view: bool = False,
    ) -> None:
        if self._discard_recovery_on_transition:
            self._clear_recovery_snapshot()
        preserve_view = preserve_view and (
            self.project.video_path == project.video_path
            and self.project.video_duration == project.video_duration
        )
        if project.backing_track_path != self.project.backing_track_path:
            self.session.backing_revision += 1
        if project.analysis_review != self.project.analysis_review:
            self.session.draft_revision += 1
            if self._analysis_dialog is not None:
                self._analysis_dialog.hide()
                self._analysis_dialog = None
        if not preserve_view:
            self._source_request += 1
            self.session.source_revision += 1
            self.workspace.cancel_source_jobs(self.session.id)
            if self._analysis_dialog is not None:
                self._analysis_dialog.hide()
                self._analysis_dialog = None
            self._cancel_waveform_workers()
            if hasattr(self, "player"):
                self._reset_transport_state()
                self.player.stop()
        self.project = project
        self.project.sort_segments()
        if self.project_path != project_path:
            self._saved_project_hash = None
        self.project_path = project_path
        if not preserve_view or project.segment_by_id(self.selected_segment_id) is None:
            self.selected_segment_id = ""
        self._syncing = True
        try:
            self.title_edit.setText(project.title)
            self.authors_edit.setText(", ".join(project.authors))
            self.readme_edit.setPlainText(project.readme)
            self.video_path_label.setText(project.video_path or "No video loaded")
            self.video_path_label.setToolTip(project.video_path)
            self._refresh_backing_controls()
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
            if not preserve_view:
                self.mark_in_spin.setValue(0.0)
                self.mark_out_spin.setValue(min(3.0, duration))
            self.timeline.set_duration(duration)
            if not preserve_view:
                self.timeline.set_waveform([])
            self.timeline.set_segments(project.segments)
            self.timeline.set_marks(self.mark_in_spin.value(), self.mark_out_spin.value())
            self._refresh_table(self.selected_segment_id)
            self._sync_selected_editor()
        finally:
            self._syncing = False
        if not preserve_view:
            if project.video_path and Path(project.video_path).is_file():
                self.player.setSource(QUrl.fromLocalFile(project.video_path))
                self._start_waveform(project.video_path, project.video_duration)
            else:
                self.player.setSource(QUrl())
        self.video_widget.set_position(self.current_position())
        self._set_dirty(mark_dirty)
        self._refresh_validation_label()

    # ---------- Media and timeline ----------

    def _start_waveform(self, path: str, duration: float) -> None:
        self._cancel_waveform_workers()
        self._waveform_request_id += 1
        request_id = self._waveform_request_id
        worker = WaveformWorker(self.media, request_id, path, duration)
        worker.configure_job(
            self.workspace.job_manager, self.session.id, "waveform", "Reading waveform",
            read_paths=(Path(path),),
            source_snapshot={"source_revision": self.session.source_revision, "path": path},
        )
        self._waveform_workers.append(worker)
        worker.completed.connect(self._waveform_ready)
        worker.failed.connect(self._waveform_failed)
        worker.finished.connect(self._retire_waveform_worker)
        self.progress_label.setText("Reading waveform…")
        worker.start()

    def _cancel_waveform_workers(self) -> None:
        self._waveform_request_id += 1
        for worker in tuple(self._waveform_workers):
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
        self.workspace.pause_other_previews(self)
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
        self.video_widget.set_position(target_ms / 1000.0)
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
        self.video_widget.set_position(target_ms / 1000.0)
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
        self.video_widget.set_position(
            self._stopped_seek_target_ms / 1000.0 if self._stopped_seek_active else position
        )
        if not self._slider_dragging and self.project.video_duration > 0:
            self.seek_slider.setValue(int(position / self.project.video_duration * 100_000))
        self.position_label.setText(
            f"{format_time(position)} / {format_time(self.project.video_duration)}"
        )
        if self._preview_end is not None and position >= self._preview_end:
            self.player.pause()
            self._preview_end = None
        self._follow_playback_segment(position)

    def _follow_playback_segment(self, position: float) -> None:
        if (
            self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
            or self._stopped_seek_active
            or self._preview_end is not None
            or self._range_edit_record is not None
            or self._syncing
            or len(self._selected_table_ids()) > 1
        ):
            return
        # Follow the latest start, keeping simultaneous-speaker selections stable.
        segment = max(
            (item for item in self.project.segments if item.start <= position < item.end),
            key=lambda item: (item.start, item.id == self.selected_segment_id),
            default=None,
        )
        if segment is not None and segment.id != self.selected_segment_id:
            self._show_selected_segment(segment)

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
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._follow_playback_segment(self.current_position())

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

    def combine_segments(self) -> None:
        identifiers = self._selected_table_ids()
        selected = sorted(
            (segment for segment in self.project.segments if segment.id in identifiers),
            key=lambda segment: (segment.start, segment.end),
        )
        images = list(dict.fromkeys(segment.image_path for segment in selected if segment.image_path))
        discard_other_images = False
        if len(images) > 1 and all(
            segment.audio_mode == "video" and segment.source_range_known for segment in selected
        ):
            answer = QMessageBox.question(
                self,
                "Choose the combined still image",
                f"The selected segments use different still images. Keep {Path(images[0]).name}, "
                "the first custom still in timeline order, for the combined segment?\n\n"
                "The other still images will no longer be attached to these segments. "
                "No image files will be deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            discard_other_images = True
        try:
            combined = self.project.combine_segments(
                identifiers, discard_other_images=discard_other_images
            )
        except ValueError as error:
            QMessageBox.information(self, "Cannot combine segments", str(error))
            return
        self._set_dirty(True)
        self._refresh_table(combined.id)
        self.select_segment(combined.id)
        self.statusBar().showMessage(
            f"Combined {len(selected)} segments. Lines and speakers were joined in timeline order; "
            "the source-video range includes any gaps."
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
        self.workspace.pause_other_previews(self)
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
        self._show_selected_segment(segment)
        self._preview_end = None
        self.seek(segment.start)

    def _show_selected_segment(self, segment: Segment) -> None:
        self.selected_segment_id = segment.id
        self.timeline.set_selected(segment.id)
        self._select_table_row(segment.id)
        self._update_combine_action()
        self._sync_selected_editor()
        self._syncing = True
        try:
            self.mark_in_spin.setValue(segment.start)
            self.mark_out_spin.setValue(segment.end)
        finally:
            self._syncing = False
        self.timeline.set_marks(segment.start, segment.end, segment.id)

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
        self.video_widget.set_segments(self.project.segments)
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
        if self._analysis_dialog is not None:
            self._analysis_dialog.existing_segments = len(self.project.segments)
        selected = self._selected_table_ids()
        if selected_id is not None or len(selected) < 2:
            selected = [selected_id if selected_id is not None else self.selected_segment_id]
        timeline_warnings = audit_timeline_overlaps(self.project.segments)
        self.segment_table.blockSignals(True)
        try:
            self.segment_table.clearSelection()
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
                if segment.id in selected:
                    self.segment_table.selectionModel().select(
                        self.segment_table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
            self._apply_timeline_review_highlights(timeline_warnings)
        finally:
            self.segment_table.blockSignals(False)
        self.timeline.set_segments(self.project.segments)
        self.video_widget.set_segments(self.project.segments)
        if selected_id is None:
            self._table_selection_changed()
        self._update_combine_action()
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
                    self.segment_table.selectionModel().setCurrentIndex(
                        self.segment_table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.ClearAndSelect
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
                self.segment_table.scrollToItem(item)
                return

    def _table_selection_changed(self) -> None:
        if self._syncing:
            return
        identifiers = self._selected_table_ids()
        self._update_combine_action()
        if len(identifiers) == 1:
            if identifiers[0] != self.selected_segment_id:
                self.select_segment(identifiers[0])
        else:
            self.prompt_player.stop()
            self._preview_end = None
            self.selected_segment_id = ""
            self.timeline.set_selected("")
            self.timeline.set_marks(self.mark_in_spin.value(), self.mark_out_spin.value())
            self._sync_selected_editor()

    def _selected_table_ids(self) -> list[str]:
        return [
            str(self.segment_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole))
            for index in self.segment_table.selectionModel().selectedRows()
        ]

    def _update_combine_action(self) -> None:
        self.action_combine.setEnabled(
            self.editor_splitter.isEnabled() and len(self._selected_table_ids()) >= 2
        )

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
                count = len(self._selected_table_ids())
                self.segment_audio_help.setText(
                    f"{count} segments selected. Use Combine to join their ranges and lines, "
                    "or select a single segment to edit it."
                    if count > 1 else
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
        if self.speakers_edit.text() != ", ".join(segment.characters):
            names = [item.strip() for item in self.speakers_edit.text().split(",") if item.strip()]
            segment.characters = list(dict.fromkeys(names))
            self._set_dirty(True)
        self._refresh_table(segment.id)

    def _selected_speakers_typed(self) -> None:
        if self._syncing:
            return
        segment = self.selected_segment()
        if not segment or self.speakers_edit.text() == ", ".join(segment.characters):
            return
        segment.characters = list(
            dict.fromkeys(
                item.strip() for item in self.speakers_edit.text().split(",") if item.strip()
            )
        )
        row = self._row_for_segment(segment.id)
        if row >= 0:
            self.segment_table.item(row, 3).setText(", ".join(segment.characters))
        self.video_widget.set_segments(self.project.segments)
        self._set_dirty(True)
        self._refresh_validation_label()

    def _selected_caption_changed(self) -> None:
        if self._syncing:
            return
        segment = self.selected_segment()
        if not segment:
            return
        segment.caption = self.caption_edit.toPlainText()
        self.video_widget.set_segments(self.project.segments)
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
        self._commit_editors()
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
        if self.title_edit.text() != self.project.title:
            self.project.title = self.title_edit.text().strip()
        if self.authors_edit.text() != ", ".join(self.project.authors):
            self.project.authors = [
                item.strip() for item in self.authors_edit.text().split(",") if item.strip()
            ]
        self.project.readme = self.readme_edit.toPlainText()
        if self.head_pad_spin.value() != round(self.project.head_padding, 3):
            self.project.head_padding = self.head_pad_spin.value()
        if self.tail_pad_spin.value() != round(self.project.tail_padding, 3):
            self.project.tail_padding = self.tail_pad_spin.value()
        self.project.video_height = self.height_spin.value()
        self.project.video_fps = self.fps_spin.value()
        self.project.preserve_source_video = self.preserve_video_check.isChecked()
        segment = self.selected_segment()
        if segment:
            if self.speakers_edit.text() != ", ".join(segment.characters):
                segment.characters = list(
                    dict.fromkeys(
                        item.strip()
                        for item in self.speakers_edit.text().split(",")
                        if item.strip()
                    )
                )
            segment.caption = self.caption_edit.toPlainText()
        if self.project.to_dict() != before:
            self.video_widget.set_segments(self.project.segments)
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
        self._source_request += 1
        request = self._source_request
        job = self.workspace.job_manager.submit(
            self.session.id, "probe", f"Checking {source.name}",
            lambda _ctx: self.media.probe(source), resource_class="io",
            read_paths=(source,),
        )
        job.completed.connect(
            lambda info: self._apply_source_video(source, info)
            if request == self._source_request
            and self.session.id not in self.workspace._closed_ids
            and self.workspace.editors.get(self.session.id) is self else None
        )
        job.failed.connect(
            lambda message: self.workspace.notice("Could not open video", message)
            if request == self._source_request
            and self.session.id not in self.workspace._closed_ids
            and self.workspace.editors.get(self.session.id) is self else None
        )

    def _apply_source_video(self, source: Path, info) -> None:
        self.session.source_revision += 1
        if self._analysis_dialog is not None:
            self._analysis_dialog.hide()
            self._analysis_dialog = None
        self.workspace.cancel_source_jobs(self.session.id)
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
            self.workspace.notice(
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
        self._source_request += 1
        self.session.source_revision += 1
        self.workspace.cancel_source_jobs(self.session.id)
        if self._analysis_dialog is not None:
            self._analysis_dialog.hide()
            self._analysis_dialog = None
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
            self.session.backing_revision += 1
            self.project.backing_track_path = str(Path(path).resolve())
            self._refresh_backing_controls()
            self._set_dirty(True)

    def clear_backing_track(self) -> None:
        self.session.backing_revision += 1
        self.project.backing_track_path = ""
        self._refresh_backing_controls()
        self._set_dirty(True)

    def _refresh_backing_controls(self) -> None:
        path = self.project.backing_track_path
        self.backing_path_label.setText(Path(path).name if path else "None (no music)")
        self.backing_path_label.setToolTip(path or "Generate backing to keep music under recordings.")
        self.generate_backing_button.setText("Regenerate backing..." if path else "Generate backing...")

    def generate_backing_track(self, *, after_success: Callable[[], None] | None = None) -> bool:
        if self._backing_dialog is not None:
            self._backing_dialog.show()
            self._backing_dialog.raise_()
            return False
        self._commit_editors()
        project = self.project
        source = Path(project.video_path)
        if not project.video_path or not source.is_file():
            QMessageBox.warning(
                self, "Source video needed",
                "Open or relink the source video first. For an exported pack, import its folder "
                "or ZIP to use the included dub_video.ogv without changing your dialogue.",
            )
            return False
        if project.backing_track_path:
            answer = QMessageBox.question(
                self,
                "Regenerate backing track?",
                "Generate new backing from the video? Only this project's backing selection will "
                "change after successful generation. The existing backing file, captions, speakers, "
                "timings and prompt files will be preserved.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        token = (self.session.source_token(), self.session.backing_revision, project.backing_track_path)
        dialog = BackingDialog(
            self.media, source.resolve(), self.analysis_data_root.parent / "backing", self,
            job_manager=self.workspace.job_manager, project_id=self.session.id,
            source_snapshot={"source_revision": self.session.source_revision},
        )
        self._backing_dialog = dialog

        def apply_result() -> None:
            if self.session.id in self.workspace._closed_ids:
                return
            result = dialog.backing_path
            current = (
                self.session.source_token(), self.session.backing_revision,
                self.project.backing_track_path,
            )
            if result is None:
                return
            if token != current:
                self.statusBar().showMessage(
                    f"Backing saved at {result}; your newer source/backing choice was kept."
                )
                return
            self.project.backing_track_path = str(result)
            self.session.backing_revision += 1
            self._refresh_backing_controls()
            self._set_dirty(True)
            self.statusBar().showMessage("Backing generated; dialogue and prompts preserved.")
            if after_success is not None:
                QTimer.singleShot(0, after_success)

        dialog.accepted.connect(apply_result)
        dialog.finished.connect(lambda _result: setattr(self, "_backing_dialog", None))
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.show()
        return True

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
        if self._export_dialog is not None:
            self._export_dialog.show()
            self._export_dialog.raise_()
            self._export_dialog.activateWindow()
            return
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
        if not self._confirm_backing_export():
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
        assets = [
            Path(value) for value in (
                self.project.video_path, self.project.backing_track_path, self.project.icon_path,
                *(segment.audio_path for segment in self.project.segments),
                *(segment.image_path for segment in self.project.segments),
            ) if value
        ]
        worker.configure_job(
            self.workspace.job_manager, self.session.id, "export", "Export pack and ZIP",
            read_paths=assets, write_paths=(output_folder, output_zip),
            source_snapshot={"revision": self.session.revision},
        )
        self._export_worker = worker
        dialog = ExportProgressDialog(output_folder, self, background=True)
        self._export_dialog = dialog
        dialog.finished.connect(self._export_dialog_closed)
        worker.progress.connect(self._export_progress)
        worker.progress.connect(dialog.report_progress)
        worker.completed.connect(self._export_completed)
        worker.failed.connect(self._export_failed)
        worker.canceled.connect(self._export_cancelled)
        worker.finished.connect(self._export_finished)
        worker.finished.connect(worker.deleteLater)
        dialog.show()
        worker.start()
        self.workspace.tasks_panel.register_detail(worker.job_handle.id, dialog)
        token = self.session.source_token()

        def retry_export() -> None:
            if self._export_dialog is not None:
                self._export_dialog.close()
                self._export_dialog = None
            self.export_pack()

        self.workspace.tasks_panel.register_retry(
            worker.job_handle.id, retry_export,
            available=lambda: self._export_worker is None and self.session.source_token() == token,
        )

    @Slot(object)
    def _export_progress(self, update: ExportProgress) -> None:
        self.progress_label.setText(update.message.splitlines()[0])

    def _confirm_backing_export(self) -> bool:
        if self.project.backing_track_path:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("This pack has no backing music")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "Without a backing track, dubbed playback will contain only the players' recordings. "
            "Generate music/effects backing from the video before exporting?"
        )
        generate = box.addButton("Generate backing", QMessageBox.ButtonRole.AcceptRole)
        silent = box.addButton("Export without music", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(generate)
        box.exec()
        if box.clickedButton() is generate:
            self.generate_backing_track(after_success=self.export_pack)
            return False
        return box.clickedButton() is silent

    @Slot(object)
    def _export_completed(self, value: object) -> None:
        result = value
        if not isinstance(result, ExportResult):
            self._export_failed("Exporter returned an unexpected result")
            return
        if self._export_dialog is not None:
            self._export_dialog.show_result(result)
        self.statusBar().showMessage(f"Exported {result.pack_path.name}")

    @Slot(str)
    def _export_failed(self, message: str) -> None:
        if self._export_dialog is not None:
            self._export_dialog.show_error(message)
        self.statusBar().showMessage("Export failed")

    @Slot()
    def _export_cancelled(self) -> None:
        if self._export_dialog is not None:
            self._export_dialog.show_cancelled()
        self.statusBar().showMessage("Export cancelled")

    @Slot()
    def _export_finished(self) -> None:
        if self._export_dialog is not None:
            self._export_dialog.worker_finished()
            self._set_busy(False, self._export_dialog.progress_label.text())
        self._export_worker = None

    @Slot(int)
    def _export_dialog_closed(self, _result: int) -> None:
        if self._export_worker is None:
            self._export_dialog = None

    def _set_busy(self, busy: bool, message: str) -> None:
        self.progress_label.setText(message)
        self.progress_bar.setRange(0, 0 if busy else 1)
        self.progress_bar.setValue(0 if busy else 1)
        self.progress_bar.setVisible(busy)
        self.action_export.setEnabled(not busy and not self.session.loading)
        self._update_combine_action()

    def _set_loading(self, loading: bool) -> None:
        self.session.loading = loading
        self.editor_splitter.setEnabled(not loading)
        for action in (
            self.action_save, self.action_save_as, self.action_analyze, self.action_add,
            self.action_split, self.action_delete, self.action_duplicate,
        ):
            action.setEnabled(not loading)
        self.action_export.setEnabled(not loading and self._export_worker is None)
        self._update_combine_action()
        if loading:
            self.action_combine.setEnabled(False)

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
        self.workspace.refresh_tabs()

    def _maybe_save(self) -> bool:
        if self._automation_disconnected:
            return False
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
        if self._automation_disconnected or answer == QMessageBox.StandardButton.Cancel:
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

    def show_mcp_help(self) -> None:
        from choicer_voicer_pack_creator.ui.mcp_help_dialog import McpHelpDialog

        McpHelpDialog(self).exec()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        event.ignore()
        index = self.workspace.tabs.indexOf(self)
        if index >= 0:
            self.workspace.close_project_tab(index)


class MainWindow(QMainWindow):
    """Application services and a tabbed collection of reusable document editors."""

    editor_type = ProjectEditor

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
        self._active_editor: ProjectEditor | None = None
        self._automation_active = False
        self._automation_disconnected = False
        self.media = media
        self.settings = settings or QSettings(
            "ChoicerVoicerCommunity", "ChoicerVoicerPackCreator"
        )
        self.recovery_store = recovery_store
        self.analysis_data_root = (
            analysis_data_root.resolve() if analysis_data_root else
            Path(QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )) / "analysis"
        )
        self.editors: dict[str, ProjectEditor] = {}
        self.job_manager = JobManager(self, limits={
            "cpu": 2 if (os.cpu_count() or 1) >= 4 else 1, "io": 2, "network": 2,
        })
        self._opening_paths: dict[str, str] = {}
        self._save_targets: dict[str, str] = {}
        self._save_jobs: dict[str, set[str]] = {}
        self._save_tokens: dict[str, str] = {}
        self._restore_started = False
        self.workspace_store = (
            WorkspaceStore(recovery_store.path.parent / "workspace-v1.json")
            if recovery_store else None
        )
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("projectTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_project_tab)
        self.setCentralWidget(self.tabs)
        self.setWindowTitle("Choicer Voicer Pack Creator")
        self.resize(1500, 950)
        self.setMinimumSize(1050, 680)
        self._decisions: list[QMessageBox] = []
        self._closing = False
        self._close_approved = False
        self._closed_ids: set[str] = set()
        self._exit_discarded: set[str] = set()
        self.tasks_panel = TasksPanel(self.job_manager, self)
        self.setup_consent = SetupConsent(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.tasks_panel)
        self.tasks_panel.hide()
        self.job_manager.changed.connect(
            lambda record: QTimer.singleShot(0, self._retire_closed_editors)
            if not record.active else None
        )
        editor = self.add_project(PackProject(authors=[getpass.getuser()]), dirty=False)
        self._initial_editor_id = editor.session.id
        self.updater = UpdateController(self, editor.help_menu)
        if initial_path:
            QTimer.singleShot(0, lambda: self.open_path(initial_path))
        if recovery_store:
            QTimer.singleShot(0, self.restore_workspace)

    def __getattr__(self, name: str):
        editor = self.__dict__.get("_active_editor")
        if editor is not None:
            return getattr(editor, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        # Compatibility for existing editor integrations; async code always keeps its editor.
        editor = self.__dict__.get("_active_editor")
        if (
            editor is not None and name not in self.__dict__
            and name not in {"updater", "job_manager", "tasks_panel"}
            and not hasattr(type(self), name) and hasattr(editor, name)
        ):
            setattr(editor, name, value)
        else:
            super().__setattr__(name, value)

    @property
    def active_editor(self) -> ProjectEditor:
        if self._active_editor is None:
            raise RuntimeError("The workspace has no active project")
        return self._active_editor

    @property
    def project_sessions(self) -> list[ProjectSession]:
        return [
            editor.session for editor in self.editors.values()
            if editor.session.id not in self._closed_ids
        ]

    def editor_for_project(self, project_id: str) -> ProjectEditor:
        return self.editors[project_id]

    def menuBar(self) -> QMenuBar:  # noqa: N802
        return self.active_editor.menuBar()

    def statusBar(self) -> QStatusBar:  # noqa: N802
        return self.active_editor.statusBar()

    def add_project(
        self, project: PackProject, path: Path | None = None, *,
        dirty: bool = True, session_id: str | None = None, focus: bool = True,
    ) -> ProjectEditor:
        if path is not None:
            for existing in self.editors.values():
                if existing.session.id in self._closed_ids:
                    continue
                if existing.project_path is not None and (
                    canonical_project_path(existing.project_path) == canonical_project_path(path)
                ):
                    self.focus_project(existing.session.id)
                    return existing
        session = ProjectSession(project, path=path)
        if session_id is not None:
            session.id = session_id
        editor = self.editor_type(
            self.media, settings=self.settings,
            recovery_store=self.recovery_store.for_session(session.id) if self.recovery_store else None,
            analysis_data_root=self.analysis_data_root, workspace=self, session=session,
        )
        self.editors[session.id] = editor
        index = self.tabs.addTab(editor, project.title)
        editor._set_dirty(dirty)
        if focus:
            self.tabs.setCurrentIndex(index)
        initial_id = self.__dict__.get("_initial_editor_id")
        initial = self.editors.get(initial_id)
        if (
            initial is not None and initial is not editor and not initial.dirty
            and initial.project_path is None and not initial.project.video_path
            and not initial.project.segments and initial.project.title == "Untitled Dub Pack"
            and not self.job_manager.active_jobs(initial_id)
        ):
            self.tabs.removeTab(self.tabs.indexOf(initial))
            self.editors.pop(initial_id)
            initial.deleteLater()
            self._initial_editor_id = None
        self.refresh_tabs()
        return editor

    def focus_project(self, project_id: str) -> None:
        if project_id in self._closed_ids or project_id not in self.editors:
            self.notice("Project is closed", "This project was closed. Open its saved file again.")
            return
        editor = self.editor_for_project(project_id)
        if self.tabs.indexOf(editor) < 0:
            editor.session.hidden = False
            self.tabs.addTab(editor, editor.project.title)
        self.tabs.setCurrentWidget(editor)

    def _tab_changed(self, index: int) -> None:
        previous = self._active_editor
        current = self.tabs.widget(index)
        if previous is not None and previous is not current:
            previous._commit_editors()
            previous._cancel_stopped_seek(restore_audio=True)
            previous.player.pause()
            previous.prompt_player.stop()
        self._active_editor = current if isinstance(current, ProjectEditor) else None
        if "tasks_panel" in self.__dict__:
            self.tasks_panel.project_id = current.session.id if self._active_editor else None
            self.tasks_panel.refresh()
        self.refresh_tabs()

    def refresh_tabs(self) -> None:
        for editor in self.editors.values():
            index = self.tabs.indexOf(editor)
            if index >= 0:
                label = editor.project.title or "Untitled Dub Pack"
                active = [
                    job for job in self.job_manager.active_jobs(editor.session.id)
                    if job.kind not in {"recovery", "workspace"}
                ]
                suffix = " [working]" if active else ""
                if editor.session.attention:
                    suffix += " [!]"
                self.tabs.setTabText(index, f"{'* ' if editor.dirty else ''}{label}{suffix}")
                self.tabs.setTabToolTip(index, str(editor.project_path or "Unsaved project"))
        if self._active_editor is not None:
            self.setWindowTitle(self._active_editor.windowTitle())

    def project_for_path(self, path: Path) -> ProjectEditor | None:
        key = canonical_project_path(path)
        for owner in (self._save_targets.get(key), self._opening_paths.get(key)):
            if owner is not None and owner not in self._closed_ids:
                editor = self.editors.get(owner)
                if editor is not None:
                    return editor
        for editor in self.editors.values():
            if editor.session.id in self._closed_ids:
                continue
            if editor.project_path is not None and (
                canonical_project_path(editor.project_path) == key
            ):
                return editor
        return None

    def open_path(self, path: Path) -> None:
        path = path.resolve()
        key = canonical_project_path(path)
        existing = self.project_for_path(path)
        if existing is not None:
            self.focus_project(existing.session.id)
            if existing.project_path is not None and canonical_project_path(existing.project_path) == key:
                existing._remember_recent_project(existing.project_path)
            return
        if key in self._save_targets:
            self.notice("Project save in progress", "Wait for this project's save before opening it.")
            return
        editor = self.add_project(PackProject(title=path.stem), dirty=False)
        self._opening_paths[key] = editor.session.id
        editor._set_loading(True)

        def load(_context):
            if path.is_dir():
                result = editor.importer.import_folder(path)
                return result.project, None, True, result.warnings, None
            if path.suffix.casefold() == ".zip":
                result = editor.importer.import_zip(
                    path, self.analysis_data_root.parent / "imported-packs"
                )
                return result.project, None, True, result.warnings, None
            return ProjectStore.load(path), path, False, [], sha256(path)

        def complete(value):
            if editor.session.id in self._closed_ids:
                return
            project, project_path, dirty, warnings, saved_hash = value
            editor._set_project(project, project_path, mark_dirty=dirty)
            editor._saved_project_hash = saved_hash
            if project_path is not None:
                editor._remember_recent_project(project_path)
            self.settings.setValue("lastProjectDir", str(path.parent))
            if warnings:
                editor.session.attention = "\n".join(warnings)
                editor.statusBar().showMessage(editor.session.attention)

        def failed(message: str) -> None:
            editor.session.attention = message
            self.notice("Could not open project", message)
            if path.suffix.casefold() == ".json":
                self.offer_previous_save(editor, path, message)

        job = self.job_manager.submit(
            editor.session.id, "open", f"Opening {path.name}", load,
            resource_class="io", read_paths=(path,),
        )
        job.completed.connect(complete)
        job.failed.connect(failed)

        def finished():
            if self._opening_paths.get(key) == editor.session.id:
                self._opening_paths.pop(key)
            editor._set_loading(False)
        job.finished.connect(finished)

    def new_from_video(self, source: Path | None = None, *, auto_process: bool = True) -> None:
        source = source or self.active_editor._choose_new_video()
        if source is None:
            return
        source = source.resolve()
        self.settings.setValue("lastVideoDir", str(source.parent))
        editor = self.add_project(PackProject(
            title=source.stem, authors=[getpass.getuser()],
        ), dirty=False)
        editor._set_loading(True)
        token = editor.session.source_token()

        def ready(info):
            if token != editor.session.source_token() or editor.session.id in self._closed_ids:
                return
            editor._set_project(PackProject(
                title=source.stem, authors=[getpass.getuser()],
                video_path=str(source), video_duration=info.duration,
            ), None, mark_dirty=True)
            if auto_process:
                QTimer.singleShot(0, lambda: editor._finish_new_import(editor.project))

        job = self.job_manager.submit(
            editor.session.id, "probe", f"Checking {source.name}",
            lambda _ctx: self.media.probe(source), resource_class="io", read_paths=(source,),
        )
        job.completed.connect(ready)
        job.failed.connect(lambda message: self.notice("Could not open video", message))
        job.finished.connect(lambda: editor._set_loading(False))

    def new_from_youtube(self) -> None:
        editor = self.add_project(PackProject(title="YouTube import"), dirty=False)
        editor._start_youtube_import()

    def pause_other_previews(self, active: ProjectEditor) -> None:
        for editor in self.editors.values():
            if editor is not active:
                editor._cancel_stopped_seek(restore_audio=True)
                editor.player.pause()
                editor.prompt_player.stop()

    def cancel_source_jobs(self, project_id: str) -> None:
        for job in self.job_manager.active_jobs(project_id):
            if job.kind in {"waveform", "analysis", "refinement", "backing"}:
                self.job_manager.cancel(job.id)

    def notice(self, title: str, message: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        self._show_decision(box)

    def _show_decision(self, box: QMessageBox) -> None:
        self._decisions.append(box)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.finished.connect(lambda _result: self._decisions.remove(box))
        box.finished.connect(box.deleteLater)
        box.show()

    def save_editor(
        self, editor: ProjectEditor, *, save_as: bool = False,
        destination: Path | None = None, on_saved: Callable[[], None] | None = None,
    ) -> bool:
        editor._commit_editors()
        destination = destination or (None if save_as else editor.project_path)
        if destination is None:
            filename, _ = QFileDialog.getSaveFileName(
                self, "Save Pack Creator project",
                str(Path(self.settings.value("lastProjectDir", str(Path.home())))
                    / f"{editor.project.title}.cvpack.json"),
                "Pack Creator projects (*.cvpack.json)",
            )
            if not filename:
                return False
            destination = Path(filename)
            if not destination.name.casefold().endswith(".cvpack.json"):
                destination = destination.with_name(destination.name + ".cvpack.json")
        destination = destination.resolve()
        try:
            reservation = self.reserve_project_save(editor.session.id, destination)
        except ValueError as error:
            self.notice("Could not save project", str(error))
            return False
        snapshot, revision = editor.session.snapshot(), editor.session.revision

        def save(_context):
            ProjectStore.save(snapshot, destination)
            return sha256(destination)

        def saved(saved_hash):
            self.complete_project_save(editor.session.id, destination, revision, saved_hash)
            if on_saved is not None:
                on_saved()

        try:
            job = self.job_manager.submit(
                editor.session.id, "save", f"Saving {snapshot.title}", save,
                resource_class="io", write_paths=(destination, ProjectStore.previous_path(destination)),
                resource_keys=(f"document-save:{editor.session.id}",),
                source_snapshot={"revision": revision},
            )
        except (RuntimeError, ValueError):
            self.release_project_save(reservation)
            raise
        job.completed.connect(saved)
        def failed(message):
            self._closing = False
            self.updater.cancel_close_update()
            self.notice("Could not save project", message)

        def finished():
            self.release_project_save(reservation)
            if job.record.state == "cancelled":
                self._closing = False
                self.updater.cancel_close_update()
                editor.statusBar().showMessage("Save cancelled; unsaved changes were kept.")
        job.failed.connect(failed)
        job.finished.connect(finished)
        return True

    def reserve_project_save(self, project_id: str, destination: Path) -> str:
        editor = self.editor_for_project(project_id)
        if editor.session.loading:
            raise ValueError("Wait for this project to finish opening before saving it.")
        key = canonical_project_path(destination)
        owner = self._save_targets.get(key)
        if owner is None:
            opening_id = self._opening_paths.get(key)
            if opening_id not in self._closed_ids:
                owner = opening_id
        if owner is None:
            owner = next((
                other.session.id for other in self.editors.values()
                if other is not editor and other.session.id not in self._closed_ids
                and other.project_path is not None
                and canonical_project_path(other.project_path) == key
            ), None)
        if owner is not None and owner != project_id:
            raise ValueError("Choose another save path or use the existing project's tab.")
        token = uuid.uuid4().hex
        self._save_targets[key] = project_id
        self._save_jobs.setdefault(key, set()).add(token)
        self._save_tokens[token] = key
        return token

    def release_project_save(self, token: str) -> None:
        key = self._save_tokens.pop(token)
        pending = self._save_jobs[key]
        pending.remove(token)
        if not pending:
            self._save_jobs.pop(key)
            self._save_targets.pop(key)

    def complete_project_save(
        self, project_id: str, destination: Path, revision: int, saved_hash: str,
    ) -> None:
        editor = self.editor_for_project(project_id)
        editor.project_path = destination
        editor._saved_project_hash = saved_hash
        editor.session.saved_revision = revision
        self.settings.setValue("lastProjectDir", str(destination.parent))
        editor._remember_recent_project(destination)
        if project_id in self._closed_ids:
            pass
        elif not editor.dirty:
            editor._clear_recovery_snapshot()
        else:
            editor._write_recovery_snapshot()
        editor.statusBar().showMessage(f"Saved revision {revision} to {destination}")
        self.refresh_tabs()

    def offer_previous_save(self, editor: ProjectEditor, path: Path, message: str = "") -> None:
        previous = ProjectStore.previous_path(path)
        job = self.job_manager.submit(
            editor.session.id, "recovery", "Checking previous save",
            lambda _ctx: ProjectStore.load(previous), resource_class="io", read_paths=(previous,),
        )

        def offer(project):
            box = QMessageBox(
                QMessageBox.Icon.Question, "Open previous save?",
                f"{message}\n\nOpen the previous save in a separate unsaved tab? "
                "Neither the current tab nor the saved file will be replaced.", parent=self,
            )
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            box.finished.connect(
                lambda result: self.add_project(project, dirty=True)
                if result == QMessageBox.StandardButton.Yes else None
            )
            self._show_decision(box)
        job.completed.connect(offer)
        job.failed.connect(lambda error: editor.statusBar().showMessage(
            f"No readable previous save: {error}"
        ))

    def _view_state(self, editor: ProjectEditor) -> dict:
        return {
            "selected_segment_id": editor.selected_segment_id,
            "position": editor.current_position(),
            "mark_in": editor.mark_in_spin.value(), "mark_out": editor.mark_out_spin.value(),
            "zoom": editor.zoom_slider.value(),
            "editor_sizes": editor.editor_splitter.sizes(),
            "inspector_sizes": editor.inspector_splitter.sizes(),
        }

    @staticmethod
    def _restore_view(editor: ProjectEditor, view: dict) -> None:
        selected = view.get("selected_segment_id")
        if selected and any(segment.id == selected for segment in editor.project.segments):
            editor.select_segment(selected)
        editor.mark_in_spin.setValue(float(view.get("mark_in", 0)))
        editor.mark_out_spin.setValue(float(view.get("mark_out", 3)))
        editor.zoom_slider.setValue(int(view.get("zoom", 10)))
        editor.player.setPosition(round(float(view.get("position", 0)) * 1000))
        for key, splitter in (
            ("editor_sizes", editor.editor_splitter),
            ("inspector_sizes", editor.inspector_splitter),
        ):
            if isinstance(view.get(key), list):
                splitter.setSizes(view[key])

    def save_workspace_state(self, on_saved: Callable[[], None] | None = None) -> None:
        if self.workspace_store is None or self.recovery_store is None:
            if on_saved is not None:
                on_saved()
            return
        documents, snapshots = [], []
        for editor in self.editors.values():
            if editor.session.id in self._closed_ids:
                continue
            editor._commit_editors()
            documents.append({
                "id": editor.session.id,
                "path": str(editor.project_path) if editor.project_path else "",
                "hidden": editor.session.hidden, "view": self._view_state(editor),
            })
            if (
                (editor.dirty or editor.project_path is None)
                and editor.session.id not in self._exit_discarded
            ):
                snapshots.append((editor.recovery_store, editor.session.snapshot(), editor.project_path))
        active_id = self._active_editor.session.id if self._active_editor else None
        store = self.workspace_store
        keys = tuple(f"document-save:{editor.session.id}" for editor in self.editors.values())

        def persist(_context):
            for recovery, snapshot, path in snapshots:
                recovery.save(snapshot, path)
            store.save(documents, active_id)

        job = self.job_manager.submit(
            None, "workspace", "Saving workspace", persist, resource_class="io",
            resource_keys=keys + ("workspace-state",),
            write_paths=(store.path, self.recovery_store.path.parent / "recovery"),
        )
        if on_saved is not None:
            job.completed.connect(lambda _result: on_saved())
        def failed(message):
            self._closing = False
            self.notice("Could not save workspace", message)
        job.failed.connect(failed)
        job.finished.connect(
            lambda: failed("Workspace save was cancelled; the application remains open.")
            if job.record.state == "cancelled" else None
        )

    def restore_workspace(self) -> None:
        if self._restore_started or self.workspace_store is None or self.recovery_store is None:
            return
        self._restore_started = True
        store, root = self.workspace_store, self.recovery_store

        def load(_context):
            notices = []
            try:
                manifest = store.load()
            except (OSError, ValueError, TypeError) as error:
                notices.append(f"Workspace list could not be restored: {error}")
                manifest = {"documents": [], "active_id": None}
            documents = {item["id"]: item for item in manifest["documents"]}
            recoveries = dict(root.session_records(notices))
            restored = []
            for identity in dict.fromkeys([*documents, *recoveries]):
                item = documents.get(identity, {})
                record = recoveries.get(identity)
                if record is not None:
                    changed = root.saved_project_changed(record)
                    restored.append((
                        identity, record.project,
                        None if changed else record.project_path, True, item,
                    ))
                    if changed:
                        notices.append(
                            f"{record.project.title}: the saved file changed; recovery opened "
                            "as a separate unsaved project. The newer saved file was not replaced."
                        )
                elif item.get("path"):
                    path = Path(item["path"])
                    try:
                        restored.append((identity, ProjectStore.load(path), path, False, item))
                    except (OSError, ValueError) as error:
                        notices.append(f"Could not restore {path}: {error}")
            try:
                legacy = root.load()
            except (OSError, ValueError, TypeError) as error:
                notices.append(f"Legacy recovery was retained but could not be read: {error}")
                legacy = None
            return restored, notices, legacy, manifest.get("active_id")

        def ready(value):
            restored, notices, legacy, active_id = value
            for identity, project, path, dirty, item in restored:
                if identity in self.editors:
                    continue
                if path is not None:
                    key = canonical_project_path(path)
                    existing = key in self._opening_paths or any(
                        editor.project_path is not None
                        and canonical_project_path(editor.project_path) == key
                        for editor in self.editors.values()
                    )
                    if existing:
                        if not dirty:
                            continue
                        path = None
                        notices.append(
                            f"{project.title}: recovered edits opened as a separate unsaved tab "
                            "because the saved project is already open."
                        )
                editor = self.add_project(
                    project, path, dirty=dirty, session_id=identity, focus=False,
                )
                # Task history does not survive restart, so retained documents need visible tabs.
                QTimer.singleShot(0, lambda editor=editor, item=item: self._restore_view(
                    editor, item.get("view", {})
                ))
            if legacy is not None:
                box = QMessageBox(
                    QMessageBox.Icon.Question, "Recover previous workspace?",
                    "A legacy automatic recovery snapshot was found. Open it in a separate tab? "
                    "The snapshot will remain on disk until its new recovery copy is saved.", parent=self,
                )
                box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

                def migrate(result):
                    if result != QMessageBox.StandardButton.Yes:
                        return
                    editor = self.add_project(legacy.project, dirty=True)
                    snapshot, target = editor.session.snapshot(), editor.recovery_store

                    def copy(_ctx):
                        target.save(snapshot, None)
                        root.clear()
                    job = self.job_manager.submit(
                        editor.session.id, "recovery", "Migrating recovered project", copy,
                        resource_class="io", resource_keys=(f"document-save:{editor.session.id}",),
                        write_paths=(root.path, target.path),
                    )
                    job.failed.connect(lambda error: self.notice("Recovery migration failed", error))
                box.finished.connect(migrate)
                self._show_decision(box)
            if active_id in self.editors and not self.editors[active_id].session.hidden:
                self.focus_project(active_id)
            if notices:
                self.notice("Workspace recovery notes", "\n\n".join(notices))
            self.refresh_tabs()

        job = self.job_manager.submit(
            None, "workspace", "Restoring workspace", load, resource_class="io",
            resource_keys=("workspace-state",),
            read_paths=(store.path, root.path, root.path.parent / "recovery"),
        )
        job.completed.connect(ready)
        job.failed.connect(lambda message: self.notice("Could not restore workspace", message))

    def _hide_editor(self, editor: ProjectEditor, *, retain: bool) -> None:
        editor._commit_editors()
        editor._recovery_timer.stop()
        editor._save_layout_state()
        editor._cancel_stopped_seek(restore_audio=True)
        editor.player.pause()
        editor.prompt_player.stop()
        if retain:
            editor._write_recovery_snapshot()
        else:
            editor._source_request += 1
            self._closed_ids.add(editor.session.id)
            self.setup_consent.cancel_project(editor.session.id)
        self.tabs.removeTab(self.tabs.indexOf(editor))
        editor.session.hidden = True
        if not self.tabs.count():
            self.add_project(PackProject(authors=[getpass.getuser()]), dirty=False)
        self.save_workspace_state()
        QTimer.singleShot(0, self._retire_closed_editors)

    def _retire_closed_editors(self) -> None:
        for project_id in tuple(self._closed_ids):
            if self.job_manager.active_jobs(project_id):
                continue
            editor = self.editors.pop(project_id, None)
            if editor is not None:
                editor._recovery_timer.stop()
                editor.player.stop()
                editor.prompt_player.stop()
                for dialog in editor.findChildren(QDialog):
                    dialog.hide()
                editor.deleteLater()
            self._closed_ids.discard(project_id)
            self._exit_discarded.discard(project_id)

    def _decide_dirty(
        self, editor: ProjectEditor, proceed: Callable[[], None], cancel: Callable[[], None],
    ) -> None:
        editor._commit_editors()
        if not editor.dirty or editor.session.id in self._exit_discarded:
            proceed()
            return
        box = QMessageBox(
            QMessageBox.Icon.Question, f"Unsaved changes - {editor.project.title}",
            "Save this project's changes before closing?", parent=self,
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setObjectName("projectDirtyDecision")
        box.setProperty("projectId", editor.session.id)
        for button, name in (
            (QMessageBox.StandardButton.Save, "projectCloseSave"),
            (QMessageBox.StandardButton.Discard, "projectCloseDiscard"),
            (QMessageBox.StandardButton.Cancel, "projectCloseCancel"),
        ):
            box.button(button).setObjectName(name)

        def decided(result):
            if result == QMessageBox.StandardButton.Discard:
                self._exit_discarded.add(editor.session.id)
                editor._clear_recovery_snapshot()
                proceed()
            elif result == QMessageBox.StandardButton.Save:
                if not self.save_editor(editor, on_saved=lambda: self._decide_dirty(
                    editor, proceed, cancel
                )):
                    cancel()
            else:
                cancel()
        box.finished.connect(decided)
        self._show_decision(box)

    def close_project_tab(self, index: int) -> None:
        editor = self.tabs.widget(index)
        if not isinstance(editor, ProjectEditor):
            return
        jobs = [
            job for job in self.job_manager.active_jobs(editor.session.id)
            if job.kind not in {"save", "recovery"}
        ]
        if jobs:
            box = QMessageBox(
                QMessageBox.Icon.Question, "Close a project with running tasks?",
                f"{editor.project.title} has {len(jobs)} queued or running task(s). "
                "Keep processing retains the project and recovery data. "
                "Tasks do not survive exiting the application.", parent=self,
            )
            keep = box.addButton("Keep processing", QMessageBox.ButtonRole.AcceptRole)
            stop = box.addButton("Cancel tasks and close", QMessageBox.ButtonRole.DestructiveRole)
            stay = box.addButton("Keep open", QMessageBox.ButtonRole.RejectRole)
            box.setObjectName("projectCloseDecision")
            box.setProperty("projectId", editor.session.id)
            keep.setObjectName("projectCloseKeepProcessing")
            stop.setObjectName("projectCloseCancelTasks")
            stay.setObjectName("projectCloseKeepOpen")

            def decide(_result):
                if box.clickedButton() is keep:
                    self._hide_editor(editor, retain=True)
                elif box.clickedButton() is stop:
                    for job in jobs:
                        self.job_manager.cancel(job.id)
                    self._decide_dirty(
                        editor, lambda: self._hide_editor(editor, retain=False), lambda: None
                    )
            box.finished.connect(decide)
            self._show_decision(box)
            return
        self._decide_dirty(
            editor, lambda: self._hide_editor(editor, retain=False), lambda: None
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._close_approved:
            self.setup_consent.cancel_all()
            for editor in self.editors.values():
                editor._save_layout_state()
                editor._reset_transport_state()
                editor.player.stop()
                editor.prompt_player.stop()
                editor._recovery_timer.stop()
            self.job_manager.shutdown(cancel=False, wait=True)
            event.accept()
            return
        event.ignore()
        if self._automation_active:
            return
        if self._closing:
            return
        if self._automation_disconnected:
            self._closing = True
            for job in self.job_manager.active_jobs():
                if job.kind not in {"save", "recovery", "workspace"}:
                    self.job_manager.cancel(job.id)
            for editor in self.editors.values():
                editor._recovery_timer.stop()
                editor._write_recovery_snapshot()
            self.save_workspace_state(self._finish_close)
            return
        if not self.updater.can_close():
            return
        active = [job for job in self.job_manager.active_jobs() if job.kind not in {
            "save", "recovery", "workspace"
        }]
        if active:
            box = QMessageBox(
                QMessageBox.Icon.Question, "Tasks are still running",
                f"{len(active)} task(s) are queued or running across the workspace. "
                "Cancel them and exit once they stop? Processing cannot continue after exit.",
                parent=self,
            )
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            self._closing = True

            def stop(result):
                self._closing = False
                if result == QMessageBox.StandardButton.Yes:
                    for job in active:
                        self.job_manager.cancel(job.id)
                    self._wait_to_close()
            box.finished.connect(stop)
            self._show_decision(box)
            return
        self._closing = True
        editors = [
            editor for editor in self.editors.values() if editor.session.id not in self._closed_ids
        ]

        def cancel():
            self._closing = False
            discarded = self._exit_discarded.copy()
            self._exit_discarded.clear()
            self.updater.cancel_close_update()
            for editor in self.editors.values():
                if editor.session.id in discarded:
                    editor._write_recovery_snapshot()

        def next_editor():
            if editors:
                self._decide_dirty(editors.pop(0), next_editor, cancel)
            else:
                for editor in self.editors.values():
                    editor._recovery_timer.stop()
                self.save_workspace_state(self._finish_close)
        next_editor()
        if self._close_approved:
            event.accept()

    def _wait_to_close(self) -> None:
        if self.job_manager.active_jobs():
            QTimer.singleShot(50, self._wait_to_close)
        else:
            self.close()

    def _finish_close(self) -> None:
        if self.job_manager.active_jobs():
            QTimer.singleShot(50, self._finish_close)
            return
        if not self._automation_disconnected and any(
            editor.dirty and editor.session.id not in self._exit_discarded
            and editor.session.id not in self._closed_ids
            for editor in self.editors.values()
        ):
            self._closing = False
            self.close()
            return
        if not self.updater.install_on_close():
            self._closing = False
            return
        for editor in self.editors.values():
            editor._save_layout_state()
            editor._reset_transport_state()
            editor.player.stop()
            editor.prompt_player.stop()
            editor._recovery_timer.stop()
        self.job_manager.shutdown(cancel=False, wait=True)
        self._close_approved = True
        self._closing = False
        QTimer.singleShot(0, self.close)
