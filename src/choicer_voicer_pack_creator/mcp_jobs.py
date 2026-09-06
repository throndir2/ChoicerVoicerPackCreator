from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from choicer_voicer_pack_creator.automation import (
    HeadlessProjectAccess,
    PackAutomation,
    local_path,
    protected_assets,
    require_revision,
)
from choicer_voicer_pack_creator.exporter import safe_name

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.jobs import JobRecord
    from choicer_voicer_pack_creator.mcp_gui import EditorBridge


def describe_job(record: JobRecord) -> dict[str, Any]:
    return {
        "job_id": record.id, "project_id": record.project_id,
        "kind": record.kind, "title": record.title, "state": record.state,
        "message": record.message, "fraction": record.fraction,
        "cancel_requested": record.cancel_requested, "active": record.active,
        "source_snapshot": dict(record.source_snapshot) if record.source_snapshot else None,
        "result": record.result if isinstance(record.result, (dict, list, str, int, float)) else None,
        "error": record.error,
    }


class LiveJobs:
    """Submit to the visible workspace's scheduler; never maintain a second task list."""

    def __init__(self, bridge: EditorBridge) -> None:
        self.bridge = bridge

    def list(self, project_id: str | None = None) -> dict[str, Any]:
        def read():
            manager = self.bridge.window.job_manager
            records = manager.tasks() if project_id is None else manager.tasks(project_id)
            if project_id is not None and not records and project_id not in {
                session.id for session in self.bridge.window.project_sessions
            }:
                raise ValueError(f"Unknown project_id: {project_id}")
            return {"jobs": [describe_job(record) for record in records]}
        return self.bridge.call(read)

    def get(self, job_id: str) -> dict[str, Any]:
        return self.bridge.call(
            lambda: describe_job(self.bridge.window.job_manager.handle(job_id).record)
        )

    def cancel(self, job_id: str) -> dict[str, Any]:
        def cancel():
            manager = self.bridge.window.job_manager
            manager.handle(job_id)  # Validate identity before asking the scheduler to cancel.
            manager.cancel(job_id)
            return describe_job(manager.handle(job_id).record)
        return self.bridge.call(cancel)

    def start(
        self, automation: PackAutomation, kind: str, expected_revision: str,
        *, output_parent: str | None = None, overwrite: bool = False,
        use_whisper: bool = False, allow_download: bool = False,
        sensitivity: str = "balanced", model: str = "base", language: str = "auto",
    ) -> dict[str, Any]:
        snapshot = automation.access.snapshot()
        require_revision(snapshot, expected_revision)
        frozen = PackAutomation(
            HeadlessProjectAccess(snapshot), automation.data_root, automation.media
        )
        writes: tuple[Path, ...] = ()
        keys: tuple[str, ...] = ()
        reads = protected_assets(snapshot.project)
        if snapshot.path:
            reads.append(snapshot.path)
        if kind == "export":
            if snapshot.dirty or snapshot.path is None:
                raise ValueError("Save the project before exporting.")
            parent = local_path(output_parent or "", exists=False)
            folder = parent / safe_name(snapshot.project.title)
            writes = (folder, folder.with_name(folder.name + ".zip"))

            def operation(ctx):
                return frozen.export_pack(
                    str(parent), expected_revision, overwrite,
                    lambda detail: ctx.report(detail.message, detail.fraction, detail=detail),
                )
        elif kind == "analysis":
            if use_whisper and not allow_download:
                raise ValueError("Whisper requires explicit allow_download=true permission.")
            local_path(snapshot.project.video_path)
            if use_whisper:
                keys = ("whisper-inference",)

            def operation(ctx):
                return frozen.analyze(
                    use_whisper, allow_download, sensitivity, model, language,
                    ctx.report, ctx.cancelled,
                )
        else:
            raise ValueError(f"Unsupported processing kind: {kind}")

        def submit():
            # A human edit between request capture and GUI scheduling must not silently
            # change which snapshot was accepted.
            require_revision(automation.access.snapshot(), expected_revision)
            handle = self.bridge.window.job_manager.submit(
                snapshot.project_id, kind, f"MCP {kind}: {snapshot.project.title}", operation,
                resource_class="cpu", resource_keys=keys, read_paths=reads, write_paths=writes,
                source_snapshot={"project_id": snapshot.project_id, "revision": snapshot.revision},
            )
            if kind == "export":
                from choicer_voicer_pack_creator.exporter import ExportResult
                from choicer_voicer_pack_creator.ui.export_dialog import ExportProgressDialog

                dialog = ExportProgressDialog(parent, self.bridge.window, background=True)
                dialog.close_button.setObjectName("exportDetailsClose")
                handle.detail.connect(dialog.report_progress)

                def completed(result):
                    dialog.show_result(ExportResult(
                        Path(result["pack_path"]), Path(result["zip_path"]),
                        result["validation"], result["file_hashes"], result["warnings"],
                    ))

                def finished():
                    if handle.record.state == "cancelled":
                        dialog.show_cancelled()
                    elif handle.record.state != "succeeded":
                        dialog.show_error(handle.record.error or handle.record.state)
                    dialog.worker_finished()

                handle.completed.connect(completed)
                handle.finished.connect(finished)
                self.bridge.window.tasks_panel.register_detail(handle.id, dialog)
            return describe_job(handle.record)
        return self.bridge.call(submit)
