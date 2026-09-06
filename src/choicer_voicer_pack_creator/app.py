from __future__ import annotations

import json
import os
import sys
import tempfile
import wave
from array import array
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QSettings,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
    Slot,
    qInstallMessageHandler,
    qVersion,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from choicer_voicer_pack_creator.diagnostics import (
    ApplicationDiagnostics,
    diagnostic_event,
    diagnostic_exception,
)
from choicer_voicer_pack_creator.single_instance import SingleInstance, SingleInstanceError

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import MainWindow


class _DiagnosticNotifications(QObject):
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[QMessageBox] = []
        self.failed.connect(self.show_failure)

    @Slot(str)
    def show_failure(self, message: str) -> None:
        box = QMessageBox(
            QMessageBox.Icon.Warning, "Application diagnostics", message,
            QMessageBox.StandardButton.Ok, QApplication.activeWindow(),
        )
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.messages.append(box)
        box.finished.connect(lambda _result: self.messages.remove(box))
        box.open()


def _qt_message(kind, context, message: str) -> None:
    diagnostic_event(
        "qt_message", level=kind.name, category=context.category,
        file=context.file, line=context.line, message=message,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv
    if "--mcp" in arguments:
        from choicer_voicer_pack_creator.mcp_server import main as mcp_main

        return mcp_main([item for item in arguments[1:] if item != "--mcp"])
    return run_editor(arguments)


@dataclass(frozen=True)
class EditorArguments:
    arguments: list[str]
    paths: tuple[Path, ...]
    data_root: Path | None
    smoke_test: bool


def parse_editor_arguments(argv: Sequence[str]) -> EditorArguments:
    arguments = list(argv)
    if not arguments:
        arguments = ["choicer-voicer-pack-creator"]
    qt_arguments = [arguments[0]]
    paths = []
    data_root = None
    smoke_test = False
    positional_only = False
    index = 1
    while index < len(arguments):
        item = arguments[index]
        option, separator, value = item.partition("=")
        if not positional_only and option in {"--data-root", "--test-data-root"}:
            if data_root is not None:
                raise ValueError("Specify only one isolated application data root.")
            if not separator:
                index += 1
                value = arguments[index] if index < len(arguments) else ""
            if not value or "\0" in value or not Path(value).is_absolute():
                raise ValueError(f"{option} requires an absolute local directory path.")
            data_root = Path(value).resolve()
        elif item == "--" and not positional_only:
            positional_only = True
        else:
            qt_arguments.append(item)
            if positional_only or not item.startswith("-"):
                if not item or "\0" in item:
                    raise ValueError("File paths must not be empty or contain NUL characters.")
                paths.append(Path(item).resolve())
            elif item == "--smoke-test":
                smoke_test = True
        index += 1
    if smoke_test and data_root is not None:
        raise ValueError("--smoke-test cannot be combined with an isolated data-root option.")
    return EditorArguments(qt_arguments, tuple(paths), data_root, smoke_test)


def run_editor(
    argv: Sequence[str],
    *,
    start_automation: Callable[[MainWindow], object] | None = None,
) -> int:
    try:
        options = parse_editor_arguments(argv)
    except (ValueError, OSError) as error:
        print(f"Invalid editor arguments: {error}", file=sys.stderr)
        return 2
    arguments, smoke_test = options.arguments, options.smoke_test
    QCoreApplication.setOrganizationName("ChoicerVoicerCommunity")
    QCoreApplication.setApplicationName("Choicer Voicer Pack Creator")
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, False)
    regular_data = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    ).resolve()
    if options.data_root == regular_data:
        print("An isolated data root must differ from the regular application data.", file=sys.stderr)
        return 2
    app_data = options.data_root or regular_data

    startup_notices: list[str] = []
    with ExitStack() as stack:
        data_root = (
            Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cvpc-smoke-")))
            if smoke_test else app_data
        )
        instance = None
        if not smoke_test:
            try:
                data_root.mkdir(parents=True, exist_ok=True)
                instance = SingleInstance(data_root)
                stack.callback(instance.close)
                primary = instance.try_acquire()
            except (OSError, SingleInstanceError) as error:
                if start_automation is not None:
                    print(f"Application data is unavailable: {error}", file=sys.stderr)
                    return 2
                app = QApplication(arguments)
                QMessageBox.critical(None, "Application data is unavailable", str(error))
                return 2
            if not primary:
                if start_automation is not None:
                    print(
                        "The editor is already running. Live MCP cannot attach to an unrelated "
                        "editor. Close it, or use --data-root with a separate absolute directory "
                        "for an isolated visible editor.",
                        file=sys.stderr,
                    )
                    return 3
                app = QApplication(arguments)
                try:
                    instance.forward_paths(options.paths)
                except SingleInstanceError as error:
                    QMessageBox.warning(
                        None, "Could not contact the running editor",
                        f"{error}\n\nThe existing workspace and recovery data were left unchanged.",
                    )
                    return 3
                return 0
        diagnostics = ApplicationDiagnostics(
            data_root / "analysis",
            on_error=None if smoke_test else startup_notices.append,
        )
        try:
            stack.enter_context(diagnostics)
        except OSError as error:
            if start_automation is not None:
                print(f"Diagnostic logging is unavailable: {error}", file=sys.stderr)
                return 2
            app = QApplication(arguments)
            QMessageBox.critical(
                None, "Diagnostic logging is unavailable",
                f"{error}\n\nCheck disk space and access to:\n{diagnostics.path.parent}",
            )
            return 2
        previous_qt_handler = qInstallMessageHandler(_qt_message)
        stack.callback(qInstallMessageHandler, previous_qt_handler)
        diagnostic_event(
            "application_initializing", qt_version=qVersion(), smoke_test=smoke_test,
            data_root=data_root, log_directory=diagnostics.path.parent,
            isolated_data_root=options.data_root is not None,
        )
        app = QApplication(arguments)
        if options.data_root is not None:
            app.setProperty("isolatedDataRoot", str(data_root))
        if instance is not None:
            try:
                instance.listen()
            except SingleInstanceError as error:
                diagnostic_exception("single_instance_unavailable", error)
                if start_automation is not None:
                    print(str(error), file=sys.stderr)
                else:
                    QMessageBox.critical(None, "Local open requests are unavailable", str(error))
                return 2
        settings = (
            QSettings(str(data_root / "settings.ini"), QSettings.Format.IniFormat)
            if options.data_root is not None or smoke_test else None
        )
        if settings is not None:
            settings.setFallbacksEnabled(False)
            stack.callback(settings.sync)
        notifications = _DiagnosticNotifications()
        if not smoke_test:
            diagnostics.on_error = notifications.failed.emit
            for message in startup_notices:
                notifications.failed.emit(message)
        heartbeat = QTimer()
        heartbeat.setInterval(5000)
        heartbeat.timeout.connect(lambda: diagnostic_event("application_heartbeat"))
        heartbeat.start()
        stack.callback(heartbeat.stop)
        diagnostics.exit_code = _run_application(
            arguments, app, data_root, smoke_test=smoke_test,
            start_automation=start_automation, initial_paths=options.paths,
            settings=settings, single_instance=instance,
        )
        return diagnostics.exit_code


def _run_application(
    arguments: list[str], app: QApplication, app_data: Path, *, smoke_test: bool,
    start_automation: Callable[[MainWindow], object] | None = None,
    initial_paths: Sequence[Path] | None = None,
    settings: QSettings | None = None,
    single_instance: SingleInstance | None = None,
) -> int:
    # Import optional/native workflows after the diagnostic sink and exception hooks exist.
    from choicer_voicer_pack_creator.analysis import (
        WhisperManager,
        default_manifest_path,
        detect_hardware,
        scan_audio_activity,
    )
    from choicer_voicer_pack_creator.media import MediaError, MediaTools
    from choicer_voicer_pack_creator.project_io import RecoveryStore
    from choicer_voicer_pack_creator.ui.main_window import MainWindow
    from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET
    from choicer_voicer_pack_creator.youtube import youtube_runtime_path

    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    icon_path = bundle_root / "assets" / "icon.svg"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    diagnostic_event("media_tools_initializing")
    try:
        media = MediaTools()
    except MediaError as error:
        diagnostic_exception("media_tools_unavailable", error)
        if start_automation is not None:
            print(f"FFmpeg is unavailable: {error}", file=sys.stderr)
            return 2
        QMessageBox.critical(
            None,
            "FFmpeg is unavailable",
            f"{error}\n\nThe Windows bundle normally includes FFmpeg. If its bin folder was "
            "removed or quarantined, restore the complete application folder. Source runs may "
            "instead use a compatible ffmpeg/ffprobe pair on PATH.",
        )
        return 2
    diagnostic_event("media_tools_ready", ffmpeg=media.ffmpeg, ffprobe=media.ffprobe)

    smoke_report = os.environ.get("CHOICER_VOICER_SMOKE_REPORT")
    if smoke_report:
        import yt_dlp_ejs.yt.solver as youtube_solver

        from choicer_voicer_pack_creator.youtube import _run_youtube_stage, _YouTubeRequest

        youtube_runtime = youtube_runtime_path()
        youtube_runtime_version = media.run(
            [str(youtube_runtime), "--version"], "Checking YouTube JavaScript runtime"
        ).stdout
        solver_root = Path(youtube_solver.__file__).parent
        version = media.run([media.ffmpeg, "-version"], "Reading FFmpeg version").stdout
        analysis_manifest = default_manifest_path()
        whisper_license = analysis_manifest.parent / "WhisperCpp-MIT.txt"
        model_license = analysis_manifest.parent / "OpenAI-Whisper-MIT.txt"
        analysis_manager = WhisperManager(Path(smoke_report).parent / "analysis-smoke")
        hardware = detect_hardware()
        activity_probe = Path(smoke_report).with_name("analysis-activity-smoke.wav")
        youtube_probe_path = Path(smoke_report).with_name("youtube-worker-smoke.mp4")
        try:
            samples = array(
                "h",
                [0] * 8000
                + [9000 if index % 16 < 8 else -9000 for index in range(8000)],
            )
            with wave.open(str(activity_probe), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(samples.tobytes())
            activity_regions, activity_threshold = scan_audio_activity(
                activity_probe,
                1.0,
                "balanced",
                lambda *_args: None,
                lambda: False,
            )
            media.run([
                media.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=32x32:r=10",
                "-i", str(activity_probe), "-t", "1", "-c:v", "mpeg4", "-c:a", "aac",
                str(youtube_probe_path),
            ], "Creating YouTube worker smoke fixture")
            youtube_probe = _run_youtube_stage(
                _YouTubeRequest(activity_probe.parent, "", media.ffmpeg, youtube_runtime),
                "probe", (media, youtube_probe_path), [],
                lambda *_args: None, lambda: False,
            )
        finally:
            activity_probe.unlink(missing_ok=True)
            youtube_probe_path.unlink(missing_ok=True)
        Path(smoke_report).write_text(
            json.dumps(
                {
                    "ffmpeg": media.ffmpeg,
                    "ffprobe": media.ffprobe,
                    "version": version.splitlines()[0],
                    "analysis_manifest": str(analysis_manifest),
                    "analysis_manifest_present": analysis_manifest.is_file(),
                    "analysis_licenses_present": (
                        whisper_license.is_file() and model_license.is_file()
                    ),
                    "whisper_runtime_build": analysis_manager.runtime["build"],
                    "whisper_models": sorted(analysis_manager.models),
                    "analysis_cpu_threads": hardware.cpu_threads,
                    "activity_scan_regions": len(activity_regions),
                    "activity_scan_threshold_db": activity_threshold,
                    "youtube_runtime": str(youtube_runtime),
                    "youtube_runtime_version": youtube_runtime_version.splitlines()[0],
                    "youtube_worker_probe_duration": youtube_probe.duration,
                    "youtube_ejs_present": all(
                        (solver_root / name).is_file() for name in ("core.min.js", "lib.min.js")
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    paths = tuple(initial_paths) if initial_paths is not None else parse_editor_arguments(
        arguments
    ).paths
    recovery_store = None
    if not smoke_test:
        recovery_store = RecoveryStore(app_data / "recovery-v2.json")
    window = MainWindow(
        media,
        settings=settings,
        recovery_store=recovery_store,
        analysis_data_root=app_data / "analysis",
    )
    window.show()
    if app.property("isolatedDataRoot"):
        isolated_suffix = f" — Isolated data: {app_data}"

        def mark_isolated(title: str) -> None:
            if not title.endswith(isolated_suffix):
                window.setWindowTitle(title + isolated_suffix)

        window.windowTitleChanged.connect(mark_isolated)
        mark_isolated(window.windowTitle())

    def open_paths(received: Sequence[Path]) -> None:
        for path in received:
            try:
                window.open_path(path)
            except (OSError, ValueError, RuntimeError) as error:
                diagnostic_exception("open_request_failed", error)
                window.notice("Could not open project", f"{path}\n\n{error}")
        if window.isMinimized():
            window.showNormal()
        window.raise_()
        window.activateWindow()

    if single_instance is not None:
        single_instance.set_open_handler(open_paths)
    if paths:
        QTimer.singleShot(0, lambda: open_paths(paths))
    if start_automation is not None:
        window.automation_runtime = start_automation(window)
    diagnostic_event("main_window_ready", initial_path=paths[0] if paths else None, paths=len(paths))
    if smoke_test:
        window.dirty = False
        QTimer.singleShot(350, app.quit)
    elif start_automation is None:
        result_argument = next(
            (item.split("=", 1)[1] for item in arguments if item.startswith("--update-result=")),
            None,
        )
        QTimer.singleShot(
            0, lambda: window.updater.startup(Path(result_argument) if result_argument else None)
        )
    return app.exec()
