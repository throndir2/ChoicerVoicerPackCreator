from __future__ import annotations

from PySide6.QtWidgets import QWidget

from choicer_voicer_pack_creator.jobs import JobManager
from choicer_voicer_pack_creator.models import PackProject
from choicer_voicer_pack_creator.project_session import ProjectSession
from choicer_voicer_pack_creator.ui.processing import ProcessingModel, ProcessingPanel


def test_panel_shares_job_states_and_actions_without_opening_details(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    session = ProjectSession(PackProject(video_path="video.mp4"))
    manager = JobManager(parent)
    model = ProcessingModel(manager, session, parent)
    panel = ProcessingPanel(model, parent)
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
