from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from choicer_voicer_pack_creator.jobs import JobManager, JobRecord
from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.project_session import ProjectSession
from choicer_voicer_pack_creator.ui.processing import ProcessingDialog, ProcessingModel


@pytest.mark.parametrize(("state", "label", "action", "enabled"), [
    ("idle", "Start", "retry", True),
    ("off", "Start", "retry", True),
    ("ready", "Start", "retry", True),
    ("failed", "Retry", "retry", True),
    ("cancelled", "Resume", "retry", True),
    ("queued", "Cancel", "cancel", True),
    ("waiting", "Cancel", "cancel", True),
    ("running", "Cancel", "cancel", True),
    ("consent", "Cancel", "cancel", True),
    ("cancelling", "Cancel", "cancel", False),
])
def test_speaker_processing_retains_manual_controls(qtbot, state, label, action, enabled):
    parent = QWidget()
    qtbot.addWidget(parent)
    manager = JobManager(parent)
    model = ProcessingModel(manager, ProjectSession(PackProject()), parent)
    panel = ProcessingDialog(model, parent)
    actions = []
    panel.action_requested.connect(lambda *args: actions.append(args))
    model.set_status("speakers", state, "Speaker matching.")
    control = panel.rows["voices"][3]
    assert control.text() == label
    assert control.isEnabled() == enabled
    control.click()
    assert actions == ([("voices", action)] if enabled else [])
    for kind, group in (("analysis", "transcript"), ("backing", "backing")):
        model.set_status(kind, "ready", "Ready.")
        assert not panel.rows[group][3].isEnabled()
    assert not panel.isVisible()
    manager.shutdown(wait=True)


def test_popup_shares_job_states_and_actions_without_opening_automatically(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    session = ProjectSession(PackProject(video_path="video.mp4"))
    manager = JobManager(parent)
    model = ProcessingModel(manager, session, parent)
    panel = ProcessingDialog(model, parent)
    actions = []
    panel.action_requested.connect(lambda group, action: actions.append((group, action)))
    model.set_status("speaker-preparation", "consent", "Permission to download the voice model.")
    label, message, _progress, control = panel.rows["voices"]
    assert label.text() == "Needs permission"
    assert message.toolTip() == "Permission to download the voice model."
    assert control.text() == "Cancel"
    control.click()
    assert actions == [("voices", "cancel")]
    assert not parent.isVisible()

    def fail(_context):
        raise ValueError("Unavailable source")

    job = manager.submit(
        session.id, "backing", "Backing", fail,
        source_snapshot={"source_revision": session.source_revision},
    )
    qtbot.waitUntil(lambda: not job.record.active)
    label, message, _progress, control = panel.rows["backing"]
    assert label.text() == "Failed"
    assert "Unavailable source" in message.toolTip()
    assert control.text() == "Retry"
    control.click()
    assert actions[-1] == ("backing", "retry")
    assert not panel.isVisible()
    panel.show_processing()
    assert panel.isVisible()
    assert panel.windowModality() == Qt.WindowModality.NonModal
    assert session.project.title in panel.windowTitle()
    assert panel.rows["backing"][0].text() == "Failed"
    panel.close()
    assert not panel.isVisible()
    manager.shutdown(wait=True)


def test_source_reset_ignores_old_job_and_restores_saved_outputs(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    session = ProjectSession(PackProject(video_path="old.mp4"))
    manager = JobManager(parent)
    model = ProcessingModel(manager, session, parent)
    old = manager.submit(
        session.id, "analysis", "Old transcript", lambda _ctx: None,
        source_snapshot={"source_revision": session.source_revision},
    )
    session.source_revision += 1
    session.project = PackProject(video_path="new.mp4", backing_track_path="kept.wav")
    model.reset()
    qtbot.waitUntil(lambda: not old.record.active)
    assert model.group_state("transcript").state == "idle"
    assert model.group_state("backing").state == "ready"
    manager.shutdown(wait=True)


def test_derived_progress_errors_and_cancellation_obey_publication_guard(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    session = ProjectSession(PackProject(video_path="source.mp4"))
    manager = JobManager(parent)
    model = ProcessingModel(manager, session, parent)
    generation = 1
    blocked = False
    model.publication_guard = lambda record: (
        record.source_snapshot["derived_generation"] == generation and not blocked
    )
    old = JobRecord(
        "old", session.id, "speakers", "Compare", "io",
        {"source_revision": session.source_revision, "derived_generation": 1},
    )
    model._job_changed(old)
    generation = 2
    model.set_status("speakers", "waiting", "Waiting for edits")
    for state in ("running", "failed", "cancelled", "succeeded"):
        model._job_changed(replace(old, state=state, message="obsolete", error="obsolete error"))
        assert model.group_state("voices").message == "Waiting for edits"
    current = replace(
        old, id="new", source_snapshot={
            "source_revision": session.source_revision, "derived_generation": 2,
        },
    )
    model._job_changed(current)
    blocked = True
    model._job_changed(replace(current, state="running", message="Held during gesture"))
    assert model.group_state("voices").state == "queued"
    blocked = False
    model._job_changed(replace(current, state="running", message="Current progress"))
    assert model.group_state("voices").message == "Current progress"
    model._job_changed(replace(current, state="failed", error="Current error"))
    assert model.group_state("voices").message == "Current error"
    manager.shutdown(wait=True)


def test_transcript_group_keeps_independent_refinement_status(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    manager = JobManager(parent)
    model = ProcessingModel(manager, ProjectSession(PackProject()), parent)
    model.set_status("analysis", "consent", "Waiting for Whisper permission.")
    model.set_status("refinement", "ready", "YouTube draft is ready.")
    state = model.group_state("transcript")
    assert state.state == "consent"
    assert "YouTube draft is ready" in state.message
    model.set_status("analysis", "running", "Transcribing.", 0.4)
    assert model.group_state("transcript").fraction == 0.4
    manager.shutdown(wait=True)


def test_summary_counts_groups_and_keeps_attention_during_parallel_work(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    manager = JobManager(parent)
    model = ProcessingModel(manager, ProjectSession(PackProject()), parent)
    assert model.status_summary() == ""
    model.set_status("analysis", "running", "Transcribing.")
    model.set_status("refinement", "queued", "Waiting.")
    assert model.status_summary() == "Background: 1 active"
    model.set_status("refinement", "consent", "Permission needed.")
    model.set_status("speaker-preparation", "failed", "Model missing.")
    model.set_status("speakers", "running", "Comparing cached voices.")
    model.set_status("backing", "waiting", "Waiting for CPU.")
    assert model.status_summary() == "Background: 3 active, 2 need attention"
    model.set_status("analysis", "ready", "Ready.")
    model.set_status("refinement", "ready", "Ready.")
    model.set_status("speaker-preparation", "off", "Off.")
    model.set_status("speakers", "cancelled", "Paused.")
    model.set_status("backing", "ready", "Ready.")
    assert model.status_summary() == ""
    manager.shutdown(wait=True)
