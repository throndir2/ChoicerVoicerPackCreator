"""Debounced advisory validation; editable state and presentation stay on Qt."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Slot

from choicer_voicer_pack_creator.jobs import JobContext, JobHandle, JobRecord
from choicer_voicer_pack_creator.models import Segment
from choicer_voicer_pack_creator.project_checks import (
    ProjectChecksInput,
    ProjectChecksResult,
    SegmentChecksInput,
    check_project,
)

if TYPE_CHECKING:
    from choicer_voicer_pack_creator.ui.main_window import ProjectEditor


def _run_checks(
    metadata: ProjectChecksInput, segments: tuple[SegmentChecksInput, ...], context: JobContext,
) -> ProjectChecksResult:
    context.check_cancelled()
    return check_project(metadata, segments)


class ProjectChecks(QObject):
    key = "project-checks"

    def __init__(self, editor: ProjectEditor) -> None:
        super().__init__(editor)
        self.editor = editor
        self._metadata: ProjectChecksInput | None = None
        self._segments: dict[str, SegmentChecksInput] = {}
        self._order: tuple[str, ...] = ()
        self._duplicates: tuple[SegmentChecksInput, ...] = ()
        self._source_token: tuple[str, int, str] | None = None
        self.result: ProjectChecksResult | None = None
        self.pending = False
        self._closed = False
        self._indicator = QTimer(self)
        self._indicator.setSingleShot(True)
        self._indicator.setInterval(250)
        self._indicator.timeout.connect(self._show_pending)
        editor.derived_work.cancelled.connect(self._cancelled)

    def changed(
        self, segment: Segment | None = None, *, force: bool = False,
        prepared: ProjectChecksResult | None = None,
    ) -> None:
        if self._closed:
            return
        metadata = ProjectChecksInput.capture(self.editor.project)
        token = self.editor.session.source_token()
        changed = (
            force or prepared is not None or metadata != self._metadata or token != self._source_token
        )
        self._metadata, self._source_token = metadata, token
        if segment is None or segment.id not in self._segments or self._duplicates:
            values = tuple(SegmentChecksInput.capture(item) for item in self.editor.project.segments)
            segments = {
                item.id: item for item in values
            }
            order = tuple(item.id for item in values)
            duplicates = values if len(segments) != len(values) else ()
            changed |= (
                segments != self._segments or order != self._order or duplicates != self._duplicates
            )
            self._segments, self._order = segments, order
            self._duplicates = duplicates
        else:
            value = SegmentChecksInput.capture(segment)
            changed |= self._segments.get(segment.id) != value
            self._segments[segment.id] = value
        if not changed:
            return
        generation = self.editor.derived_work.invalidate(self.key)
        if prepared is not None:
            self._indicator.stop()
            self.pending = False
            self.result = prepared
            self.editor._publish_validation_result(prepared)
            return
        self.pending = True
        self.editor._validation_pending()
        if (
            self.editor.derived_work.current(self.key, generation)
            and not self._indicator.isActive()
        ):
            self._indicator.start()
        self.request()

    def request(self) -> None:
        if self._closed or not self.pending:
            return
        self.editor.derived_work.request(
            self.key, self._start, self._publish, delay_ms=250,
        )

    def _show_pending(self) -> None:
        if (
            self.pending and not self._closed
            and self.editor.derived_work.current(
                self.key, self.editor.derived_work.generation(self.key),
            )
        ):
            self.editor._validation_activity(
                "Waiting for edits" if self.editor._derived_publication_blocked()
                else "Updating checks"
            )

    def _start(self, generation: int) -> JobHandle:
        metadata = self._metadata
        if metadata is None:
            raise RuntimeError("Project checks were requested without input")
        # The tuple and frozen records are the worker's entire input. No transcript,
        # recovery JSON, live Segment objects, widgets, or editor crosses this boundary.
        segments = self._duplicates or tuple(self._segments[identity] for identity in self._order)
        self._show_pending()
        return self.editor.workspace.job_manager.submit(
            self.editor.session.id, "project-checks", "Update project checks",
            partial(_run_checks, metadata, segments), resource_class="io", priority=15,
            source_snapshot={
                "source_revision": self.editor.session.source_revision,
                "derived_key": self.key, "derived_generation": generation,
            },
        )

    def _publish(self, record: JobRecord) -> None:
        if self._closed or self._source_token != self.editor.session.source_token():
            return
        self._indicator.stop()
        if record.state == "succeeded":
            if not isinstance(record.result, ProjectChecksResult):
                raise TypeError("Project checks returned an invalid result")
            self.result = record.result
            self.pending = False
            self.editor._publish_validation_result(record.result)
            self.editor.workspace.job_manager.release_result(record.id)
        else:
            # Cancel is an explicit pause, not a successful/ready validation result.
            if record.state == "cancelled":
                self.editor.derived_work.pause(self.key)
            self.editor._validation_failed(
                "Checks paused" if record.state == "cancelled"
                else f"Checks failed: {record.error or record.message}"
            )
            self._register_retry(record)

    @Slot(str, object)
    def _cancelled(self, key: str, record: JobRecord) -> None:
        if key != self.key or self._closed:
            return
        self._indicator.stop()
        self.pending = True
        self.editor._validation_failed("Checks paused")
        self._register_retry(record)

    def _register_retry(self, record: JobRecord) -> None:
        tasks = self.editor.workspace.tasks_window
        tasks.register_retry(
            record.id, self.retry, available=lambda: not self._closed,
        )
        self.destroyed.connect(lambda: tasks.unregister_retry(record.id))

    def retry(self) -> None:
        self.editor.derived_work.resume(self.key)
        self.changed(force=True)

    def close(self) -> None:
        self._closed = True
        self._indicator.stop()
        self.editor.derived_work.invalidate(self.key)
