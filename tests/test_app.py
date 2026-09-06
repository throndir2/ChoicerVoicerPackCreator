from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from choicer_voicer_pack_creator.app import parse_editor_arguments
from choicer_voicer_pack_creator.single_instance import SingleInstance


@pytest.mark.parametrize("flag", ["--data-root", "--test-data-root"])
@pytest.mark.parametrize("equals", [True, False])
def test_visible_isolation_requires_explicit_root_and_preserves_all_files(tmp_path, flag, equals):
    root = tmp_path / "visible validation"
    root_arguments = [f"{flag}={root}"] if equals else [flag, str(root)]
    paths = [tmp_path / "first.cvpack.json", tmp_path / "second.cvpack.json"]
    options = parse_editor_arguments([
        "editor", *root_arguments, "--update-result=result.json", *map(str, paths),
    ])
    assert options.data_root == root
    assert options.paths == tuple(paths)
    assert not options.smoke_test
    assert options.arguments == [
        "editor", "--update-result=result.json", *map(str, paths),
    ]
    assert "--headless" not in options.arguments


@pytest.mark.parametrize("arguments", [
    ["--data-root"], ["--test-data-root"], ["--data-root="],
    ["--data-root=relative"], ["--test-data-root=relative"],
    ["--data-root=https://example.com/data"], ["--data-root=path\0name"],
])
def test_invalid_data_roots_fail_closed(arguments):
    with pytest.raises(ValueError, match="absolute"):
        parse_editor_arguments(["editor", *arguments])


def test_duplicate_roots_and_smoke_combination_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="only one"):
        parse_editor_arguments([
            "editor", f"--data-root={tmp_path}", f"--test-data-root={tmp_path}",
        ])
    with pytest.raises(ValueError, match="cannot be combined"):
        parse_editor_arguments(["editor", "--smoke-test", f"--data-root={tmp_path}"])


def test_normal_startup_keeps_smoke_and_updater_options_out_of_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    options = parse_editor_arguments([
        "editor", "--smoke-test", "--update-result=updater.json", "a.cvpack.json",
        "b.cvpack.json", "--", "--literal-filename.cvpack.json",
    ])
    assert options.smoke_test
    assert options.data_root is None
    assert options.paths == (
        tmp_path / "a.cvpack.json", tmp_path / "b.cvpack.json",
        tmp_path / "--literal-filename.cvpack.json",
    )


def test_bootstrap_passes_isolated_settings_recovery_diagnostics_and_live_hook(tmp_path):
    root = tmp_path / "isolated"
    regular_root = tmp_path / "regular"
    report = tmp_path / "bootstrap.json"
    paths = [tmp_path / "one.cvpack.json", tmp_path / "two.cvpack.json"]
    code = """
import json, sys
from pathlib import Path
from PySide6.QtCore import QSettings
from choicer_voicer_pack_creator import app
app.QStandardPaths.writableLocation = lambda _: sys.argv[2]
hook = lambda window: None
def run(arguments, application, data_root, **kwargs):
    settings = kwargs["settings"]
    settings.setValue("isolated-marker", "test-only")
    settings.sync()
    Path(sys.argv[3]).write_text(json.dumps({
        "data_root": str(data_root),
        "settings_path": settings.fileName(),
        "ini": settings.format() == QSettings.Format.IniFormat,
        "fallbacks": settings.fallbacksEnabled(),
        "live": kwargs["start_automation"] is hook,
        "isolated_property": application.property("isolatedDataRoot"),
        "smoke": kwargs["smoke_test"],
        "paths": [str(path) for path in kwargs["initial_paths"]],
        "listening": kwargs["single_instance"].server.isListening(),
    }), encoding="utf-8")
    return 0
app._run_application = run
raise SystemExit(app.run_editor(
    ["editor", "--data-root=" + sys.argv[1], *sys.argv[4:]], start_automation=hook
))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), str(regular_root), str(report), *map(str, paths)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    assert Path(data["data_root"]) == root
    assert Path(data["settings_path"]) == root / "settings.ini"
    assert data["ini"] and not data["fallbacks"]
    assert data["live"] and not data["smoke"] and data["listening"]
    assert Path(data["isolated_property"]) == root
    assert data["paths"] == list(map(str, paths))
    assert not regular_root.exists()
    assert (root / "settings.ini").is_file()
    assert (root / "analysis" / "logs" / "application.log").is_file()
    assert not (root / "application-instance.lock").exists()


@pytest.mark.parametrize("live", [False, True], ids=["forward-all-files", "refuse-unrelated-mcp"])
def test_secondary_bootstrap_never_starts_another_workspace(qtbot, tmp_path, live):
    primary = SingleInstance(tmp_path)
    assert primary.try_acquire()
    primary.listen()
    received = []
    primary.set_open_handler(received.append)
    paths = [tmp_path / "one.cvpack.json", tmp_path / "two.cvpack.json"]
    code = """
import sys
from choicer_voicer_pack_creator import app
app.QStandardPaths.writableLocation = lambda _: sys.argv[1]
def run(*args, **kwargs):
    raise AssertionError("A secondary process must not construct another workspace")
app._run_application = run
raise SystemExit(app.run_editor(
    ["editor", *sys.argv[3:]],
    start_automation=(lambda window: None) if sys.argv[2] == "live" else None,
))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), "live" if live else "normal", *map(str, paths)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        qtbot.waitUntil(lambda: process.poll() is not None, timeout=10000)
        stdout, stderr = process.communicate(timeout=5)
        if live:
            assert process.returncode == 3, stdout + stderr
            assert "cannot attach" in stderr
            assert not received
        else:
            assert process.returncode == 0, stdout + stderr
            qtbot.waitUntil(lambda: bool(received))
            assert received == [paths]
        assert not (tmp_path / "analysis").exists()
        assert not (tmp_path / "settings.ini").exists()
        assert primary.lock.isLocked() and primary.server.isListening()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        primary.close()


@pytest.mark.parametrize("fail_first", [False, True])
def test_run_application_opens_every_path_restores_workspace_and_keeps_live_runtime(
    qapp, tmp_path, monkeypatch, fail_first,
):
    from choicer_voicer_pack_creator import app, media
    from choicer_voicer_pack_creator.ui import main_window

    calls = []
    windows = []
    runtime = object()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

    class Window:
        def __init__(self, tools, initial_path=None, **kwargs):
            assert initial_path is None
            assert kwargs["settings"] is settings
            assert kwargs["recovery_store"].path == tmp_path / "recovery-v2.json"
            assert kwargs["analysis_data_root"] == tmp_path / "analysis"
            windows.append(self)

        def show(self):
            calls.append("shown")

        def restore_workspace(self):
            calls.append("restore")

        def open_path(self, path):
            calls.append(path)
            if fail_first and path.name == "one.cvpack.json":
                raise OSError("Could not read the path")

        def notice(self, title, message):
            assert title == "Could not open project"
            assert "Could not read the path" in message

        def isMinimized(self):
            return False

        def raise_(self):
            calls.append("raise")

        def activateWindow(self):
            calls.append("activate")

    class Application:
        def setStyle(self, _style):
            pass

        def setStyleSheet(self, _style):
            pass

        def setWindowIcon(self, _icon):
            pass

        def property(self, _name):
            return None

        def exec(self):
            qapp.processEvents()
            return 9

    class Media:
        ffmpeg = "ffmpeg"
        ffprobe = "ffprobe"

    class Instance:
        def set_open_handler(self, callback):
            self.callback = callback

    def start(window):
        assert window is windows[0]
        assert calls[:2] == ["shown", "restore"]
        return runtime

    monkeypatch.delenv("CHOICER_VOICER_SMOKE_REPORT", raising=False)
    monkeypatch.setattr(media, "MediaTools", Media)
    monkeypatch.setattr(main_window, "MainWindow", Window)
    instance = Instance()
    paths = [tmp_path / "one.cvpack.json", tmp_path / "two.cvpack.json"]
    assert app._run_application(
        ["editor"], Application(), tmp_path, smoke_test=False,
        initial_paths=paths, settings=settings, single_instance=instance, start_automation=start,
    ) == 9
    assert windows[0].automation_runtime is runtime
    assert calls == ["shown", "restore", *paths, "raise", "activate"]
    instance.callback([tmp_path / "three.cvpack.json", tmp_path / "four.cvpack.json"])
    assert calls[-4:] == [
        tmp_path / "three.cvpack.json", tmp_path / "four.cvpack.json", "raise", "activate",
    ]
