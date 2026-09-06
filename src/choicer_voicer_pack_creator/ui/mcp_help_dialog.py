from __future__ import annotations

import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


def mcp_client_configuration(*, headless: bool = False) -> dict[str, Any]:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        command = str(executable.with_name("Choicer Voicer MCP.exe"))
        arguments: list[str] = []
    else:
        command = str(executable)
        arguments = ["-m", "choicer_voicer_pack_creator", "--mcp"]
    if headless:
        arguments.append("--headless")
    return {"mcpServers": {"choicer-voicer": {"command": command, "args": arguments}}}


class McpHelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("LLM / MCP Help")
        self.resize(860, 780)
        self.setMinimumSize(640, 560)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Connect a compatible assistant to this editor using a local MCP stdio process. "
            "Copying configuration does not start a connection."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.help_browser = QTextBrowser()
        self.help_browser.setAccessibleName("MCP setup and safety guide")
        self.help_browser.setOpenExternalLinks(False)
        self.help_browser.setStyleSheet(
            "QTextBrowser { background: #111b28; color: #e7edf7; "
            "border: 1px solid #2a3c52; border-radius: 5px; "
            "selection-background-color: #19617c; }"
        )
        self.help_browser.document().setDocumentMargin(16)
        self.help_browser.setMarkdown(
            files("choicer_voicer_pack_creator")
            .joinpath("resources", "mcp-help.md")
            .read_text(encoding="utf-8")
        )
        layout.addWidget(self.help_browser, 3)

        self.headless_check = QCheckBox("Run without an editor window (--headless)")
        self.headless_check.setToolTip(
            "An independent in-memory project. Save explicitly before the client disconnects."
        )
        layout.addWidget(self.headless_check)
        self.mode_label = QLabel()
        self.mode_label.setWordWrap(True)
        layout.addWidget(self.mode_label)

        config_label = QLabel("&Client configuration (mcpServers)")
        layout.addWidget(config_label)
        self.configuration = QPlainTextEdit()
        self.configuration.setReadOnly(True)
        self.configuration.setAccessibleName("MCP client configuration JSON")
        self.configuration.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.configuration.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.configuration.setMinimumHeight(145)
        config_label.setBuddy(self.configuration)
        layout.addWidget(self.configuration, 1)

        client_note = QLabel(
            'VS Code uses "servers" instead of "mcpServers"; add "type": "stdio" inside '
            "the server entry. Merge with your existing client configuration."
        )
        client_note.setWordWrap(True)
        layout.addWidget(client_note)

        controls = QHBoxLayout()
        self.copy_button = QPushButton("&Copy configuration")
        self.copy_button.setObjectName("primary")
        self.copy_button.clicked.connect(self._copy_configuration)
        controls.addWidget(self.copy_button)
        self.copy_status = QLabel()
        controls.addWidget(self.copy_status, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        controls.addWidget(buttons)
        layout.addLayout(controls)

        self.headless_check.toggled.connect(self._refresh_configuration)
        self._refresh_configuration()

    def _refresh_configuration(self) -> None:
        headless = self.headless_check.isChecked()
        self.configuration.setPlainText(
            json.dumps(mcp_client_configuration(headless=headless), indent=2)
        )
        self.mode_label.setText(
            "Headless: no window or GUI connection. Do not edit the same project file "
            "in another process. Unsaved work ends with this process."
            if headless
            else "Live editor (default): the client opens a visible editor. Save your work "
            "and close this editor before connecting; it will not attach to this window."
        )
        self.copy_status.clear()

    def _copy_configuration(self) -> None:
        QApplication.clipboard().setText(self.configuration.toPlainText())
        self.copy_status.setText("Configuration copied.")
