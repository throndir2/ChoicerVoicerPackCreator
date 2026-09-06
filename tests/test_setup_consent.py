from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit, QMessageBox, QVBoxLayout

from choicer_voicer_pack_creator.ui.setup_consent import SetupConsent


def test_shared_consent_merges_versions_and_does_not_block_other_editor(qtbot):
    host = QDialog()
    qtbot.addWidget(host)
    entry = QLineEdit(host)
    QVBoxLayout(host).addWidget(entry)
    host.show()
    consent = SetupConsent(host)
    replies = []
    consent.request("a", {"runtime:v1": "Runtime v1"}, lambda yes: replies.append(("a", yes)),
                    lambda: True)
    box = consent.box
    consent.request("b", {"runtime:v1": "Runtime v1", "model:sha1": "Model 1"},
                    lambda yes: replies.append(("b", yes)), lambda: True)
    assert consent.box is box
    assert box.windowModality() == Qt.WindowModality.NonModal
    assert box.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert box.text().count("Runtime v1") == 1
    assert "Model 1" in box.text()
    qtbot.mouseClick(entry, Qt.MouseButton.LeftButton)
    qtbot.keyClicks(entry, "Other project stays editable")
    assert entry.text() == "Other project stays editable"
    box.button(QMessageBox.StandardButton.Yes).click()
    assert replies == [("a", True), ("b", True)]
    consent.request("c", {"model:sha1": "Model 1"}, replies.append, lambda: True)
    assert replies[-1] is True and consent.box is None
    consent.request("c", {"model:sha2": "Model 2"}, replies.append, lambda: True)
    assert consent.box is not None
    consent.box.reject()
    assert replies[-1] is False


def test_cancelling_one_project_preserves_shared_consent_and_rechecks_source(qtbot):
    host = QDialog()
    qtbot.addWidget(host)
    consent = SetupConsent(host)
    replies = []
    current = [True]
    consent.request("a", {"model:sha": "Shared model"}, lambda yes: replies.append(("a", yes)),
                    lambda: True)
    consent.request("b", {"model:sha": "Shared model"}, lambda yes: replies.append(("b", yes)),
                    lambda: current[0])
    box = consent.box
    consent.cancel_project("a")
    assert replies == [("a", False)]
    assert consent.box is box and box.isVisible()
    current[0] = False
    box.button(QMessageBox.StandardButton.Yes).click()
    assert replies == [("a", False), ("b", False)]


def test_declined_consent_can_be_requested_again_and_exit_cancels(qtbot):
    host = QDialog()
    qtbot.addWidget(host)
    consent = SetupConsent(host)
    replies = []
    consent.request("a", {"model:sha": "Model"}, replies.append, lambda: True)
    consent.box.reject()
    assert replies == [False]
    consent.request("a", {"model:sha": "Model"}, replies.append, lambda: True)
    assert consent.box is not None
    consent.cancel_all()
    assert replies == [False, False] and consent.box is None


def test_cancelling_one_request_preserves_other_components_for_the_same_project(qtbot):
    host = QDialog()
    qtbot.addWidget(host)
    consent = SetupConsent(host)
    speaker_replies, backing_replies = [], []
    speaker_callback = speaker_replies.append
    consent.request("a", {"speaker:hash": "Speaker model"}, speaker_callback, lambda: True)
    consent.request("a", {"backing:hash": "Backing model"}, backing_replies.append, lambda: True)
    consent.cancel_request(speaker_callback)
    assert speaker_replies == [False]
    assert backing_replies == []
    assert "Speaker model" not in consent.box.text()
    assert "Backing model" in consent.box.text()
    consent.box.button(QMessageBox.StandardButton.Yes).click()
    assert backing_replies == [True]
