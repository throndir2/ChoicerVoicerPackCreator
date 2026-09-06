from __future__ import annotations

import json
import os
import sys
import wave
from array import array
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLockFile, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv
    if "--mcp" in arguments:
        from choicer_voicer_pack_creator.mcp_server import main as mcp_main

        return mcp_main([item for item in arguments[1:] if item != "--mcp"])
    return run_editor(arguments)


def run_editor(
    argv: Sequence[str],
    *,
    start_automation: Callable[[MainWindow], object] | None = None,
) -> int:
    arguments = list(argv)
    smoke_test = "--smoke-test" in arguments
    QCoreApplication.setOrganizationName("ChoicerVoicerCommunity")
    QCoreApplication.setApplicationName("Choicer Voicer Pack Creator")
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, False)
    app = QApplication(arguments)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    icon_path = bundle_root / "assets" / "icon.svg"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    app_data: Path | None = None
    instance_lock: QLockFile | None = None
    if not smoke_test:
        app_data = Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
        )
        app_data.mkdir(parents=True, exist_ok=True)
        instance_lock = QLockFile(str(app_data / "application-instance.lock"))
        if not instance_lock.tryLock(100):
            if start_automation is not None:
                print(
                    "The editor is already running. Close it before starting live MCP, "
                    "or use --headless with a separate project.",
                    file=sys.stderr,
                )
                return 3
            QMessageBox.warning(
                None,
                "Choicer Voicer Pack Creator is already running",
                "Close the existing editor window before opening another project. This protects "
                "automatic recovery data from concurrent changes.",
            )
            return 3

    try:
        media = MediaTools()
    except MediaError as error:
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

    smoke_report = os.environ.get("CHOICER_VOICER_SMOKE_REPORT")
    if smoke_report:
        version = media.run([media.ffmpeg, "-version"], "Reading FFmpeg version").stdout
        analysis_manifest = default_manifest_path()
        whisper_license = analysis_manifest.parent / "WhisperCpp-MIT.txt"
        model_license = analysis_manifest.parent / "OpenAI-Whisper-MIT.txt"
        analysis_manager = WhisperManager(Path(smoke_report).parent / "analysis-smoke")
        hardware = detect_hardware()
        activity_probe = Path(smoke_report).with_name("analysis-activity-smoke.wav")
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
        finally:
            activity_probe.unlink(missing_ok=True)
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
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    paths = [item for item in arguments[1:] if not item.startswith("--")]
    initial_path = Path(paths[0]).resolve() if paths else None
    recovery_store = None
    if app_data is not None:
        recovery_store = RecoveryStore(app_data / "recovery-v2.json")
    window = MainWindow(
        media,
        initial_path,
        recovery_store=recovery_store,
        analysis_data_root=app_data / "analysis" if app_data is not None else None,
    )
    window.show()
    if start_automation is not None:
        window.automation_runtime = start_automation(window)
    if smoke_test:
        window.dirty = False
        QTimer.singleShot(350, app.quit)
    return app.exec()
