from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWidget

from choicer_voicer_pack_creator.ui.status_bar import ProjectStatusBar


@pytest.fixture
def status(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    bar = ProjectStatusBar(parent)
    parent.show()
    yield bar


def test_notices_expire_and_keep_only_latest_confirmation(status, qtbot):
    status.showMessage("Old notice")
    status.showMessage("Project saved", details="Saved revision 5 to project.cvpack.json")
    assert status.currentMessage() == "Project saved"
    assert status._notice_timer.interval() == 5000
    assert "revision 5" in status.toolTip()
    assert "Old notice" not in status.details_text()
    status._notice_timer.start(20)
    qtbot.waitUntil(lambda: status.currentMessage() == "")
    assert "revision 5" in status.details_text()
    assert not status.progress.isVisible()


def test_notice_expiry_cannot_clear_active_work(status, qtbot):
    status.set_activity("waveform", "Reading waveform")
    status.set_activity("export", "Exporting 2/8")
    status.showMessage("Project saved", 20)
    assert status.currentMessage() == "Exporting 2/8"
    assert status.progress.isVisible()
    qtbot.waitUntil(lambda: not status._notice_timer.isActive())
    assert status.currentMessage() == "Exporting 2/8"
    status.clear_activity("export")
    assert status.currentMessage() == "Reading waveform"
    status.clear_activity("waveform")
    assert status.currentMessage() == ""
    assert not status.progress.isVisible()


def test_issues_survive_confirmations_and_active_work_until_resolved(status):
    status.set_issue("waveform", "Waveform unavailable", details="Source audio could not be decoded")
    status.showMessage("Project saved")
    assert status.currentMessage() == "Waveform unavailable"
    assert status.issue_count == 1
    assert "Source audio could not be decoded" in status.details_text()
    status.set_activity("export", "Exporting")
    assert status.currentMessage() == "Exporting"
    assert status.issue_count == 1
    status.clear_activity("export")
    assert status.currentMessage() == "Waveform unavailable"
    status.clear_issue("waveform")
    assert status.currentMessage() == "Project saved"
    assert status.issue_count == 0


def test_acknowledgment_dismisses_issues_without_erasing_readiness_details(status):
    status.set_detail("waveform", "Waveform: Ready - 2,398 peaks")
    status.set_detail("save", "Saved revision 5 to project.cvpack.json")
    status.set_issue("save", "Could not save", details="Disk full")
    status.set_issue("export", "Export failed", details="Encoder unavailable")
    status.acknowledge_issues()
    assert status.currentMessage() == ""
    assert status.issue_count == 0
    assert "Disk full" not in status.details_text()
    assert "2,398 peaks" in status.details_text()
    assert "revision 5" in status.details_text()


def test_activity_and_notices_are_isolated_between_projects(status, qtbot):
    other_parent = QWidget()
    qtbot.addWidget(other_parent)
    other = ProjectStatusBar(other_parent)
    status.set_activity("export", "Exporting")
    other.showMessage("Project saved")
    other.set_issue("waveform", "Waveform unavailable")
    assert status.currentMessage() == "Exporting"
    assert status.issue_count == 0
    assert other.currentMessage() == "Waveform unavailable"
    status.clear_activity("export")
    assert other.issue_count == 1
