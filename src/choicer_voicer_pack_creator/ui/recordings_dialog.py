from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from choicer_voicer_pack_creator.diagnostics import diagnostic_exception
from choicer_voicer_pack_creator.exporter import safe_name
from choicer_voicer_pack_creator.jobs import JobContext, JobHandle
from choicer_voicer_pack_creator.operations import SourceSnapshot
from choicer_voicer_pack_creator.pack_io import PackImporter
from choicer_voicer_pack_creator.recording_renderer import (
    RecordingPlan,
    RecordingRenderResult,
    prepare_recording,
    render_recording,
)
from choicer_voicer_pack_creator.recordings import (
    PackInfo,
    RecordingLibrary,
    TakeInfo,
    default_game_location,
    find_takes,
    match_take,
    read_pack,
    resolve_game_location,
    scan_library,
)
from choicer_voicer_pack_creator.ui.readable_table import ReadableTableWidget

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import MainWindow

_T = TypeVar("_T")
GAME_LOCATION_SETTING = "recordings/gameLocation"


def _label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class GameLocationDialog(QDialog):
    location_requested = Signal(str)
    standalone_requested = Signal()

    def __init__(self, current: str, parent: RecordingsDialog) -> None:
        super().__init__(parent)
        self.setWindowTitle("Locate Choicer Voicer")
        self.setObjectName("gameLocationDialog")
        self.setModal(False)
        self.resize(560, 200)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(
            "Choose the game folder containing packs_voice and recordings. "
            "Standalone recording folders do not require an installed game."
        ))
        self.path = QLineEdit(current or str(default_game_location() or ""))
        self.path.setAccessibleName("Choicer Voicer game location")
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        self.browse = QPushButton("Browse...")
        self.browse.clicked.connect(self._browse)
        row.addWidget(self.browse)
        layout.addLayout(row)
        self.error = _label()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.use = buttons.addButton("Use Location", QDialogButtonBox.ButtonRole.AcceptRole)
        self.use.clicked.connect(lambda: self.location_requested.emit(self.path.text().strip()))
        standalone = buttons.addButton(
            "Open Recording Folder...", QDialogButtonBox.ButtonRole.ActionRole,
        )
        standalone.clicked.connect(self.standalone_requested)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choicer Voicer game folder", self.path.text())
        if path:
            self.path.setText(path)


class RecordingsDialog(QDialog):
    """Read-only game library with a separately rendered, single-clock media preview."""

    def __init__(self, workspace: MainWindow) -> None:
        super().__init__(workspace)
        self.workspace = workspace
        self.media = workspace.media
        self.settings = workspace.settings
        self.manager = workspace.job_manager
        self.setWindowTitle("Recordings")
        self.setObjectName("recordingsDialog")
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.resize(1000, 790)
        self._packs: tuple[PackInfo, ...] = ()
        self._takes: tuple[TakeInfo, ...] = ()
        self._take: TakeInfo | None = None
        self._plan: RecordingPlan | None = None
        self._job: JobHandle | None = None
        self._deciding = False
        self._initialized = False
        self._location = ""
        self._standalone_roots: list[Path] = []
        self._custom_backing: Path | None = None
        self._preview_path: Path | None = None
        self._preview_files: set[Path] = set()
        self._pending_seek: int | None = None
        self._autoplay_requested = False
        self._preview_directory: tempfile.TemporaryDirectory | None = None
        self._export_path: Path | None = None
        self._location_dialog: GameLocationDialog | None = None
        self._library_warnings: tuple[str, ...] = ()
        self._status_message = ""
        self._build_ui()
        self._update_controls()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.location_button = QPushButton("Game Location...")
        self.location_button.clicked.connect(self.choose_game_location)
        self.open_button = QPushButton("Open Recording Folder...")
        self.open_button.clicked.connect(self.open_recording_folder)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_library)
        controls.addWidget(self.location_button)
        controls.addWidget(self.open_button)
        controls.addWidget(self.refresh_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.location_label = _label("No game location selected.")
        layout.addWidget(self.location_label)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = ReadableTableWidget(0, 3)
        self.table.setObjectName("recordingsTable")
        self.table.setHorizontalHeaderLabels(["Pack", "Take", "Status"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._select_take)
        splitter.addWidget(self.table)
        self.video = QVideoWidget()
        self.video.setMinimumSize(320, 180)
        self.video.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        splitter.addWidget(self.video)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([150, 350])
        layout.addWidget(splitter, 1)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(lambda value: self.seek.setMaximum(max(0, value)))
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.player.errorOccurred.connect(
            lambda _error, message: self._error(f"Recording playback failed: {message}")
        )
        transport = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_playback)
        transport.addWidget(self.play_button)
        self.time_label = _label("00:00 / 00:00")
        transport.addWidget(self.time_label)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setAccessibleName("Recording playback position")
        self.seek.valueChanged.connect(self.player.setPosition)
        transport.addWidget(self.seek, 1)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setAccessibleName("Listening volume (does not change export)")
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(100)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        transport.addWidget(QLabel("Volume"))
        transport.addWidget(self.volume)
        self.fullscreen_button = QPushButton("Fullscreen")
        self.fullscreen_button.clicked.connect(lambda: self.video.setFullScreen(True))
        transport.addWidget(self.fullscreen_button)
        layout.addLayout(transport)
        for widget in (self.video, self.table):
            shortcut = QShortcut("Space", widget)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.toggle_playback)
        QShortcut("Escape", self.video).activated.connect(lambda: self.video.setFullScreen(False))

        form = QFormLayout()
        pack_row = QHBoxLayout()
        self.pack_combo = QComboBox()
        self.pack_combo.setAccessibleName("Pack used for this recording")
        self.pack_combo.currentIndexChanged.connect(self._pack_changed)
        pack_row.addWidget(self.pack_combo, 1)
        self.pack_button = QPushButton("Choose Pack...")
        menu = QMenu(self.pack_button)
        menu.addAction("Folder...", self.choose_pack_folder)
        menu.addAction("ZIP...", self.choose_pack_zip)
        self.pack_button.setMenu(menu)
        pack_row.addWidget(self.pack_button)
        form.addRow("Original pack", pack_row)
        mix_row = QHBoxLayout()
        self.voices_gain = self._gain("Recorded voices level")
        self.backing_gain = self._gain("Backing track level")
        mix_row.addWidget(QLabel("Voices"))
        mix_row.addWidget(self.voices_gain)
        mix_row.addWidget(QLabel("Backing"))
        mix_row.addWidget(self.backing_gain)
        self.backing_label = _label("Voices only")
        mix_row.addWidget(self.backing_label, 1)
        self.backing_button = QPushButton("Choose Backing...")
        self.backing_button.clicked.connect(self.choose_backing)
        mix_row.addWidget(self.backing_button)
        self.reset_backing_button = QPushButton("Use Pack Backing")
        self.reset_backing_button.clicked.connect(self.reset_backing)
        mix_row.addWidget(self.reset_backing_button)
        form.addRow("Export mix", mix_row)
        layout.addLayout(form)
        self.warning_label = _label()
        self.warning_label.setStyleSheet("color: #ffbf69;")
        layout.addWidget(self.warning_label)
        self.status_label = _label()
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        footer = QHBoxLayout()
        self.details_button = QPushButton("Clip Details")
        self.details_button.clicked.connect(self.show_clip_details)
        footer.addWidget(self.details_button)
        self.cancel_button = QPushButton("Cancel Task")
        self.cancel_button.clicked.connect(self.cancel_task)
        footer.addWidget(self.cancel_button)
        footer.addStretch()
        self.open_output_button = QPushButton("Open Video")
        self.open_output_button.clicked.connect(lambda: self._open_output(False))
        self.show_output_button = QPushButton("Show in Folder")
        self.show_output_button.clicked.connect(lambda: self._open_output(True))
        footer.addWidget(self.open_output_button)
        footer.addWidget(self.show_output_button)
        self.export_button = QPushButton("Export Video...")
        self.export_button.clicked.connect(self.export_video)
        footer.addWidget(self.export_button)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._source_controls = [
            self.location_button, self.open_button, self.refresh_button, self.table,
            self.pack_combo, self.pack_button, self.voices_gain, self.backing_gain,
            self.backing_button, self.reset_backing_button,
        ]

    def _gain(self, name: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setAccessibleName(name)
        spin.setRange(0, 200)
        spin.setSuffix("%")
        spin.setValue(100)
        spin.valueChanged.connect(self._invalidate_preview)
        return spin

    def open_library(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        if self._initialized:
            return
        self._initialized = True
        saved = str(self.settings.value(GAME_LOCATION_SETTING, ""))
        if saved:
            self.load_game_location(saved)
        else:
            self.choose_game_location()

    def choose_game_location(self) -> None:
        if self._location_dialog is None:
            saved = str(self.settings.value(GAME_LOCATION_SETTING, ""))
            dialog = GameLocationDialog(saved, self)
            dialog.location_requested.connect(self.load_game_location)
            dialog.standalone_requested.connect(
                lambda: (dialog.reject(), self.open_recording_folder())
            )
            self._location_dialog = dialog
        self._location_dialog.show()
        self._location_dialog.raise_()

    def load_game_location(self, value: str) -> None:
        if not value:
            self._error("Choose a Choicer Voicer game folder.")
            return

        def load(_context: JobContext):
            location = resolve_game_location(Path(value))
            return location, scan_library(location)

        def loaded(result) -> None:
            location, library = result
            self._location = str(location.root)
            self.settings.setValue(GAME_LOCATION_SETTING, self._location)
            self.settings.sync()
            self.location_label.setText(self._location)
            self._standalone_roots.clear()
            self._apply_library(library)
            if self.settings.status() != self.settings.Status.NoError:
                self._error("Game location is available, but could not be saved in application settings.")
            if self._location_dialog is not None:
                self._location_dialog.accept()

        handle = self._submit("Finding packs and recordings", load, loaded)
        if handle is not None:
            handle.failed.connect(self._location_unavailable)

    def _location_unavailable(self, message: str) -> None:
        if not self.workspace._closing:
            self.choose_game_location()
            self._location_dialog.error.setText(
                f"The game location is unavailable or invalid.\n{message}"
            )

    def refresh_library(self) -> None:
        if self._location:
            self.load_game_location(self._location)
        elif self._standalone_roots:
            roots, packs = tuple(self._standalone_roots), self._packs
            self._submit(
                "Refreshing recordings",
                lambda _ctx: RecordingLibrary(
                    packs, tuple(take for root in roots for take in find_takes(root)), (),
                ),
                self._apply_library,
            )
        else:
            self.choose_game_location()

    def open_recording_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Recording take or recordings folder",
            str(self.settings.value("recordings/lastRecordingFolder", "")),
        )
        if folder:
            self.load_recording_folder(Path(folder))

    def load_recording_folder(self, folder: Path) -> None:
        original = self.workspace.active_editor.project.source_pack_path
        packs = self._packs

        def load(_context: JobContext) -> RecordingLibrary:
            available = packs
            if original and Path(original).is_dir() and all(
                pack.path != Path(original).resolve() for pack in packs
            ):
                available += (read_pack(Path(original)),)
            return RecordingLibrary(available, find_takes(folder), ())

        def loaded(library: RecordingLibrary) -> None:
            self._location = ""
            self._standalone_roots = [folder]
            self.settings.setValue("recordings/lastRecordingFolder", str(folder))
            self.location_label.setText(str(folder))
            self._apply_library(library)

        self._submit("Finding recording takes", load, loaded)

    def _apply_library(self, library: RecordingLibrary) -> None:
        self._packs, self._takes = library.packs, library.takes
        self._library_warnings = library.warnings
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._takes))
        for row, take in enumerate(self._takes):
            candidates = match_take(take, self._packs)
            state = "Ready to review" if len(candidates) == 1 else (
                "Choose matching pack" if candidates else "Pack missing"
            )
            for column, text in enumerate((take.pack_name, take.name, state)):
                item = QTableWidgetItem(text)
                item.setToolTip(str(take.path))
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        if self._takes:
            self.table.selectRow(0)
        self._select_take()
        if not self._takes:
            self.status_label.setText("No recording takes found. Choose another recording folder.")

    def _select_take(self) -> None:
        row = self.table.currentRow()
        self._take = self._takes[row] if 0 <= row < len(self._takes) else None
        self._custom_backing = None
        self.pack_combo.blockSignals(True)
        self.pack_combo.clear()
        candidates = match_take(self._take, self._packs) if self._take else ()
        self.pack_combo.addItem("Choose the original pack...", None)
        for pack in candidates:
            self.pack_combo.addItem(f"{pack.title} ({pack.path})", pack)
        self.pack_combo.setCurrentIndex(1 if len(candidates) == 1 else 0)
        self.pack_combo.blockSignals(False)
        self._pack_changed()

    def _selected_pack(self) -> PackInfo | None:
        value = self.pack_combo.currentData()
        return value if isinstance(value, PackInfo) else None

    def _pack_changed(self) -> None:
        self._custom_backing = None
        self._invalidate_preview()

    def _invalidate_preview(self, *_args: object) -> None:
        self.pause_playback()
        self.player.setSource(QUrl())
        self._pending_seek = None
        self._preview_path = None
        self._plan = None
        self._export_path = None
        pack = self._selected_pack()
        backing = self._custom_backing or (pack.backing_path if pack else None)
        self.backing_label.setText(backing.name if backing else "Voices only - no backing track")
        self.backing_label.setToolTip(str(backing) if backing else "")
        self.warning_label.setText("\n".join(self._library_warnings))
        self.status_label.setText(
            "Preview uses recorded voices and backing only, never the video's original audio."
            if pack else "Choose the original pack to play or export this recording."
        )
        self._update_controls()

    def choose_pack_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Original voice pack folder",
            str(self.settings.value("recordings/lastPackFolder", "")),
        )
        if folder:
            self.load_pack(Path(folder))

    def choose_pack_zip(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(self, "Original voice pack ZIP", "", "ZIP (*.zip)")
        if filename:
            self.load_pack(Path(filename), archive=True)

    def load_pack(self, path: Path, *, archive: bool = False) -> None:
        media = self.media
        extraction = self.workspace.analysis_data_root.parent / "recording-packs"

        def load(_context: JobContext) -> PackInfo:
            if archive:
                result = PackImporter(media).import_zip(path, extraction)
                return read_pack(Path(result.project.source_pack_path))
            return read_pack(path)

        def loaded(pack: PackInfo) -> None:
            self._packs = tuple(item for item in self._packs if item.path != pack.path) + (pack,)
            self.settings.setValue("recordings/lastPackFolder", str(pack.path))
            self.pack_combo.addItem(f"{pack.title} ({pack.path})", pack)
            self.pack_combo.setCurrentIndex(self.pack_combo.count() - 1)

        self._submit("Reading original pack", load, loaded)

    def choose_backing(self) -> None:
        filename, _filter = QFileDialog.getOpenFileName(
            self, "Choose clean backing track", "", "Audio (*.mp3 *.wav *.ogg *.flac *.m4a)",
        )
        if filename:
            self._custom_backing = Path(filename)
            self._invalidate_preview()

    def reset_backing(self) -> None:
        self._custom_backing = None
        self._invalidate_preview()

    def _prepare(self, action: Callable[[RecordingPlan], None]) -> None:
        if self._plan is not None:
            action(self._plan)
            return
        pack, take, media = self._selected_pack(), self._take, self.media
        if pack is None or take is None:
            self._error("Choose a recording take and its original pack first.")
            return
        backing = self._custom_backing or pack.backing_path
        voices_gain, backing_gain = self.voices_gain.value() / 100, self.backing_gain.value() / 100

        def prepared(plan: RecordingPlan) -> None:
            self._plan = plan
            self.warning_label.setText("\n".join((*self._library_warnings, *plan.warnings)))
            action(plan)

        self._submit(
            "Preparing recording",
            lambda _ctx: prepare_recording(
                media, pack, take, backing_path=backing,
                voices_gain=voices_gain, backing_gain=backing_gain,
            ),
            prepared,
            resource_class="cpu", kind="recording-prepare",
        )

    def _confirm_plan(self, plan: RecordingPlan, action: Callable[[], None]) -> None:
        if not plan.warnings:
            action()
            return
        self._deciding = True
        self._update_controls()
        box = QMessageBox(
            QMessageBox.Icon.Warning, "Review recording",
            "\n".join(plan.warnings) + "\n\nContinue with this recording mix?", parent=self,
        )
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)

        def decided(result: int) -> None:
            self._deciding = False
            if result == QMessageBox.StandardButton.Yes and not self.workspace._closing:
                action()
            self._update_controls()

        box.finished.connect(decided)
        self.workspace._show_decision(box)

    def toggle_playback(self) -> None:
        if self._job is not None or self._deciding:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._autoplay_requested = False
            self.player.pause()
        elif self._preview_path is not None:
            self._autoplay_requested = True
            self._play_cached()
        else:
            self._autoplay_requested = True
            self._prepare(lambda plan: self._confirm_plan(plan, lambda: self._render_preview(plan)))

    def _play_cached(self, start: float | None = None) -> None:
        plan = self._plan
        if plan is None:
            self._error("Refresh the recording and prepare playback again.")
            return

        def ready(_result: None) -> None:
            if start is not None:
                self.player.setPosition(round(start * 1000))
            if self._autoplay_requested:
                self._play()

        self._submit(
            "Checking recording sources", lambda _ctx: plan.verify_sources(),
            ready, kind="recording-prepare",
        )

    def _render_preview(self, plan: RecordingPlan, start: float = 0.0) -> None:
        if self._preview_directory is None:
            try:
                self._preview_directory = tempfile.TemporaryDirectory(prefix="cv-recording-preview-")
            except OSError as error:
                diagnostic_exception("recording_preview_directory_failed", error)
                self._error(f"Could not create temporary recording playback storage: {error}")
                return
        destination = Path(self._preview_directory.name) / f"{uuid.uuid4().hex}.mkv"
        media = self.media
        old_previews = tuple(self._preview_files)

        def render(context: JobContext) -> RecordingRenderResult:
            for previous in old_previews:
                context.check_cancelled()
                previous.unlink(missing_ok=True)
            return render_recording(media, plan, destination, format="preview")

        def ready(result: RecordingRenderResult) -> None:
            self._preview_files = {result.path}
            self._preview_path = result.path
            self._pending_seek = round(start * 1000)
            self.player.setSource(QUrl.fromLocalFile(str(result.path)))
            self.status_label.setText("Recording preview ready.")
            if self._autoplay_requested:
                self._play()

        self._submit(
            "Preparing recording playback",
            render, ready, resource_class="cpu", kind="recording-preview",
        )

    def _play(self) -> None:
        if not self.isVisible() or self.workspace._closing:
            return
        for editor in self.workspace.editors.values():
            editor._cancel_stopped_seek(restore_audio=True)
            editor.player.pause()
            editor.prompt_player.stop()
        self.player.play()

    def pause_playback(self) -> None:
        self._autoplay_requested = False
        self.player.pause()
        self.video.setFullScreen(False)

    def _playback_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.play_button.setText(
            "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play"
        )

    def _media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if self._pending_seek is not None and status in {
            QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
        }:
            position, self._pending_seek = self._pending_seek, None
            self.player.setPosition(position)

    def _position_changed(self, position: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.blockSignals(True)
            self.seek.setValue(position)
            self.seek.blockSignals(False)

        def clock(value: int) -> str:
            seconds = max(0, value) // 1000
            return f"{seconds // 60:02d}:{seconds % 60:02d}"

        self.time_label.setText(f"{clock(position)} / {clock(self.player.duration())}")

    def show_clip_details(self) -> None:
        self._prepare(self._show_clip_details)

    def _show_clip_details(self, plan: RecordingPlan) -> None:
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("Recording clip details")
        dialog.resize(820, 420)
        layout = QVBoxLayout(dialog)
        table = ReadableTableWidget(len(plan.clips), 4)
        table.setHorizontalHeaderLabels(["Recorded file", "Speaker", "Start (s)", "Duration (s)"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, clip in enumerate(plan.clips):
            for column, text in enumerate((
                clip.path.name, ", ".join(clip.characters), f"{clip.start:.3f}", f"{clip.duration:.3f}",
            )):
                table.setItem(row, column, QTableWidgetItem(text))
        table.cellDoubleClicked.connect(
            lambda row, _column: self._preview_clip(plan, row, dialog)
        )
        table.setToolTip("Double-click a clip to preview it on the recording timeline.")
        layout.addWidget(table)
        layout.addWidget(_label("\n".join(plan.warnings)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        dialog.show()

    def _preview_clip(self, plan: RecordingPlan, row: int, dialog: QDialog) -> None:
        if plan is not self._plan:
            self.workspace.notice(
                "Recording selection changed", "Reopen Clip Details for the selected recording.",
            )
            return
        if self._job is not None or self._deciding:
            self.workspace.notice(
                "Recording task running", "Wait for the current recording task or cancel it first.",
            )
            return
        self._autoplay_requested = True
        if self._preview_path is None:
            self._confirm_plan(
                plan, lambda: self._render_preview(plan, start=plan.clips[row].start),
            )
            return
        self._play_cached(start=plan.clips[row].start)

    def export_video(self) -> None:
        self._prepare(lambda plan: self._confirm_plan(plan, lambda: self._choose_export(plan)))

    def _choose_export(self, plan: RecordingPlan) -> None:
        folder = str(self.settings.value("recordings/exportFolder", ""))
        filename, _filter = QFileDialog.getSaveFileName(
            self, "Export recording - original resolution and frame rate",
            str(Path(folder) / f"{safe_name(plan.take.pack_name)} - {safe_name(plan.take.name)}.mp4"),
            "MP4 video (*.mp4)", "", QFileDialog.Option.DontConfirmOverwrite,
        )
        if not filename:
            return
        destination = Path(filename)
        if not destination.suffix:
            destination = destination.with_suffix(".mp4")
        if destination.suffix.lower() != ".mp4":
            self._error("Choose an .mp4 filename for recording video export.")
            return
        self.settings.setValue("recordings/exportFolder", str(destination.parent))
        if destination.exists():
            if not destination.is_file():
                self._error("The export destination is not a regular file.")
                return
            try:
                expected = SourceSnapshot.capture([destination])
            except OSError as error:
                diagnostic_exception("recording_destination_snapshot_failed", error)
                self._error(f"Could not inspect the existing output: {error}")
                return
            self._deciding = True
            self._update_controls()
            box = QMessageBox(
                QMessageBox.Icon.Question, "Replace existing video?",
                f"Replace this file after the new video is ready?\n{destination}", parent=self,
            )
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)

            def replace(result: int) -> None:
                self._deciding = False
                if result == QMessageBox.StandardButton.Yes and not self.workspace._closing:
                    self._start_export(plan, destination, expected)
                self._update_controls()

            box.finished.connect(replace)
            self.workspace._show_decision(box)
        else:
            self._start_export(plan, destination)

    def _start_export(
        self, plan: RecordingPlan, destination: Path, expected: SourceSnapshot | None = None,
    ) -> None:
        media = self.media

        def render(_context: JobContext) -> RecordingRenderResult:
            if expected is not None:
                expected.verify()
            return render_recording(
                media, plan, destination, format="mp4", overwrite=expected is not None,
            )

        def exported(result: RecordingRenderResult) -> None:
            self._export_path = result.path
            self.status_label.setText(f"Exported video: {result.path}")
            self.warning_label.setText("\n".join(result.warnings))
            self.workspace.statusBar().showMessage(
                "Recording video exported. Open Tools > Recordings for the video.",
                details=str(result.path),
            )

        self._submit(
            "Exporting recording video",
            render,
            exported, resource_class="cpu", kind="recording-export",
        )

    def _open_output(self, folder: bool) -> None:
        if self._export_path is None:
            return
        target = self._export_path.parent if folder else self._export_path
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            self._error(f"Could not open the output. Open it manually: {target}")

    def _submit(
        self, title: str, operation: Callable[[JobContext], _T], completed: Callable[[_T], None],
        *, resource_class: str = "io", kind: str = "recording-library",
    ) -> JobHandle | None:
        if self._job is not None and self._job.record.active:
            self._error("A recording task is still running. Wait for it or cancel it first.")
            return None
        if self.workspace._closing:
            self._error("The application is closing.")
            return None
        handle = self.manager.submit(
            None, kind, title, operation, resource_class=resource_class,
        )
        self._job = handle
        self._status_message = title
        self.workspace.tasks_window.register_detail(handle.id, self)
        handle.progress.connect(self._progress)
        handle.completed.connect(completed)
        handle.failed.connect(self._error)
        handle.finished.connect(lambda: self._finished(handle))
        self._progress(title, None)
        self._update_controls()
        return handle

    def _progress(self, message: str, fraction: float | None) -> None:
        self.status_label.setText(message)
        self._status_message = message
        self.progress.setRange(0, 0 if fraction is None else 1000)
        if fraction is not None:
            self.progress.setValue(round(fraction * 1000))
        self.sync_workspace_status()

    def sync_workspace_status(self) -> None:
        for editor in self.workspace.editors.values():
            editor.statusBar().clear_activity("recordings")
        if self._job is not None and self._job.record.active:
            self.workspace.statusBar().set_activity("recordings", self._status_message)

    def _finished(self, handle: JobHandle) -> None:
        if self._job is not handle:
            return
        self._job = None
        if handle.record.state == "cancelled":
            self.status_label.setText("Recording task cancelled. Source files are unchanged.")
        self.sync_workspace_status()
        self._update_controls()
        if self._location_dialog is not None:
            self._location_dialog.use.setEnabled(True)

    def _error(self, message: str) -> None:
        self.status_label.setText(message)
        self.workspace.statusBar().showMessage(
            "Recording needs attention - Tools > Recordings", details=message,
        )
        if self._location_dialog is not None and self._location_dialog.isVisible():
            self._location_dialog.error.setText(message)

    def _update_controls(self) -> None:
        busy = self._deciding or bool(self._job is not None and self._job.record.active)
        for widget in self._source_controls:
            widget.setEnabled(not busy)
        selected = self._take is not None and self._selected_pack() is not None
        self.play_button.setEnabled(selected and not busy)
        self.export_button.setEnabled(selected and not busy)
        self.details_button.setEnabled(selected and not busy)
        self.seek.setEnabled(self._preview_path is not None)
        self.fullscreen_button.setEnabled(self._preview_path is not None)
        self.progress.setVisible(self._job is not None)
        self.cancel_button.setVisible(self._job is not None)
        self.open_output_button.setVisible(self._export_path is not None)
        self.show_output_button.setVisible(self._export_path is not None)
        if self._location_dialog is not None:
            self._location_dialog.use.setEnabled(not busy)

    def cancel_task(self) -> None:
        if self._job is not None:
            self._job.cancel()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.pause_playback()
        super().hideEvent(event)

    def shutdown(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.video.setFullScreen(False)
        if self._preview_directory is not None:
            try:
                self._preview_directory.cleanup()
            except OSError as error:
                diagnostic_exception("recording_preview_cleanup_failed", error)
            self._preview_directory = None
        self.close()
