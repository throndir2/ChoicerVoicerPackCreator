"""Session-only project deltas, with isolated, worker-built undo/redo results."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from functools import lru_cache
from itertools import count
from time import monotonic
from types import MappingProxyType
from typing import Any

from choicer_voicer_pack_creator.models import AnalysisReview, PackProject, Segment

_STATE_IDS = count(1)
_MERGE_SECONDS = 0.75
_PROJECT_FIELDS = tuple(field.name for field in fields(PackProject))
_METADATA_FIELDS = tuple(name for name in _PROJECT_FIELDS if name != "segments")
_SEGMENT_FIELDS = tuple(field.name for field in fields(Segment))


@dataclass(frozen=True, slots=True)
class _ListValue:
    items: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _RecordValue:
    model: type
    values: tuple[Any, ...]


@lru_cache
def _field_names(model: type) -> tuple[str, ...]:
    return tuple(field.name for field in fields(model))


def _immutable(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return True
    if isinstance(value, tuple):
        return all(_immutable(item) for item in value)
    if is_dataclass(value) and value.__dataclass_params__.frozen:
        return all(_immutable(getattr(value, name)) for name in _field_names(type(value)))
    return False


class _TrackedList(list):
    """Invalidate a frozen copy on mutation, including mutations of frozen review lists.

    Plain lists cannot safely be identity-cached. Metadata lists are promoted to this
    list-compatible type on the recording thread; replay constructs them on its worker.
    Only lists with deeply immutable elements are cached.
    """

    __slots__ = ("_snapshot",)

    def __init__(self, values=(), *, snapshot: _ListValue | None = None):
        super().__init__(values)
        self._snapshot = snapshot

    def __setitem__(self, key, value):
        self._snapshot = None
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._snapshot = None
        super().__delitem__(key)

    def __iadd__(self, values):
        self._snapshot = None
        return super().__iadd__(values)

    def __imul__(self, multiplier):
        self._snapshot = None
        return super().__imul__(multiplier)

    def append(self, value):
        self._snapshot = None
        super().append(value)

    def extend(self, values):
        self._snapshot = None
        super().extend(values)

    def insert(self, index, value):
        self._snapshot = None
        super().insert(index, value)

    def pop(self, index=-1):
        self._snapshot = None
        return super().pop(index)

    def remove(self, value):
        self._snapshot = None
        super().remove(value)

    def clear(self):
        self._snapshot = None
        super().clear()

    def reverse(self):
        self._snapshot = None
        super().reverse()

    def sort(self, *, key=None, reverse=False):
        self._snapshot = None
        super().sort(key=key, reverse=reverse)


def _freeze(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    if isinstance(value, list):
        if isinstance(value, _TrackedList) and value._snapshot is not None:
            return value._snapshot
        result = _ListValue(tuple(_freeze(item) for item in value))
        if isinstance(value, _TrackedList) and all(_immutable(item) for item in value):
            value._snapshot = result
        return result
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if is_dataclass(value):
        return _RecordValue(
            type(value), tuple(_freeze(getattr(value, name)) for name in _field_names(type(value)))
        )
    raise TypeError(f"Unsupported project history value: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, _ListValue):
        result = _TrackedList(_thaw(item) for item in value.items)
        if all(_immutable(item) for item in result):
            result._snapshot = value
        return result
    if isinstance(value, _RecordValue):
        return value.model(**{
            name: _thaw(item)
            for name, item in zip(_field_names(value.model), value.values, strict=True)
        })
    if isinstance(value, tuple):
        return tuple(_thaw(item) for item in value)
    return value


def _metadata(project: PackProject, previous: tuple | None = None) -> tuple:
    values = []
    for index, name in enumerate(_METADATA_FIELDS):
        value = getattr(project, name)
        if isinstance(value, list) and not isinstance(value, _TrackedList):
            value = _TrackedList(value)
            setattr(project, name, value)
        elif isinstance(value, AnalysisReview):
            updates = {
                field: _TrackedList(getattr(value, field))
                for field in ("local_rows", "refined_rows")
                if not isinstance(getattr(value, field), _TrackedList)
            }
            if updates:
                value = replace(value, **updates)
                project.analysis_review = value
        frozen = _freeze(value)
        if previous is not None and frozen == previous[index]:
            frozen = previous[index]
        values.append(frozen)
    result = tuple(values)
    return previous if previous is not None and result == previous else result


def _segment(segment: Segment) -> _RecordValue:
    return _RecordValue(Segment, tuple(_freeze(getattr(segment, name)) for name in _SEGMENT_FIELDS))


@dataclass(frozen=True, slots=True)
class _ValueChange:
    key: str | int
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class _OrderChange:
    start: int
    before: tuple[str, ...]
    after: tuple[str, ...]

    def apply(self, order: tuple[str, ...], forward: bool) -> tuple[str, ...]:
        old, new = (self.before, self.after) if forward else (self.after, self.before)
        return order[:self.start] + new + order[self.start + len(old):]


def _order_change(before: tuple[str, ...], after: tuple[str, ...]) -> _OrderChange | None:
    if before is after or before == after:
        return None
    start = 0
    common = min(len(before), len(after))
    while start < common and before[start] == after[start]:
        start += 1
    tail = 0
    while tail < common - start and before[-tail - 1] == after[-tail - 1]:
        tail += 1
    return _OrderChange(
        start, before[start:len(before) - tail], after[start:len(after) - tail],
    )


@dataclass(frozen=True, slots=True)
class _Delta:
    metadata: tuple[_ValueChange, ...] = ()
    segments: tuple[_ValueChange, ...] = ()
    order: _OrderChange | None = None

    @property
    def empty(self) -> bool:
        return not self.metadata and not self.segments and self.order is None


def _combine_values(first: tuple[_ValueChange, ...], second: tuple[_ValueChange, ...]) -> tuple:
    changes = {change.key: change for change in first}
    for change in second:
        previous = changes.get(change.key)
        changes[change.key] = _ValueChange(
            change.key, previous.before if previous is not None else change.before, change.after,
        )
    return tuple(change for change in changes.values() if change.before != change.after)


@dataclass(frozen=True, slots=True)
class _Entry:
    label: str
    state_id: int
    delta: _Delta


@dataclass(frozen=True, slots=True)
class _Snapshot:
    metadata: tuple
    segments: MappingProxyType
    order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    """Immutable replay input. Build on a worker, then accept on the recording thread."""

    target_index: int
    state_id: int
    _owner: object
    _generation: int
    _snapshot: _Snapshot
    _steps: tuple[tuple[_Delta, bool], ...]

    def build_project(self) -> PackProject:
        metadata = list(self._snapshot.metadata)
        segments = dict(self._snapshot.segments)
        order = self._snapshot.order
        for delta, forward in self._steps:
            for change in delta.metadata:
                metadata[change.key] = change.after if forward else change.before
            for change in delta.segments:
                value = change.after if forward else change.before
                if value is None:
                    segments.pop(change.key, None)
                else:
                    segments[change.key] = value
            if delta.order is not None:
                order = delta.order.apply(order, forward)
        snapshot = _Snapshot(tuple(metadata), MappingProxyType(segments), order)
        project = _ReplayProject(
            **{
                name: _thaw(value)
                for name, value in zip(_METADATA_FIELDS, metadata, strict=True)
            },
            segments=[_thaw(segments[identifier]) for identifier in order],
        )
        project._history_request = self
        project._history_snapshot = snapshot
        return project


class _ReplayProject(PackProject):
    """Carry the worker's immutable index until acceptance, without rescanning rows."""

    __slots__ = ("_history_request", "_history_snapshot")

    def __eq__(self, other):
        if not isinstance(other, PackProject):
            return NotImplemented
        return all(getattr(self, name) == getattr(other, name) for name in _PROJECT_FIELDS)

    __hash__ = None


class ProjectHistory:
    """At most ``limit`` edits for one open project, initially marked clean.

    ``record(segment=...)`` captures metadata and that existing segment only; use a
    full record after structural, identifier, or timeline-order changes. ``fields_only``
    records metadata without touching segments. Metadata lists become mutation-tracked
    list subclasses; replace fields or mutate their current lists, not stale aliases.

    Only immutable copies and deltas are retained. ``prepare`` makes a shallow index
    snapshot, not a project clone; ``build_project`` does all reconstruction on a worker.
    Pass its unchanged result to ``accept`` before publishing it as the live project.
    Apart from request building, use this class from one (normally GUI) thread.
    """

    def __init__(self, project: PackProject, limit: int = 100):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("History limit must be a positive integer")
        self.limit = limit
        self._owner = object()
        self._generation = 0
        self.reset(project)

    def reset(self, project: PackProject) -> None:
        metadata = _metadata(project)
        segments = {}
        order = []
        for segment in project.segments:
            if segment.id in segments:
                raise ValueError("Project history requires unique segment identifiers")
            segments[segment.id] = _segment(segment)
            order.append(segment.id)
        self._metadata = metadata
        self._segments = MappingProxyType(segments)
        self._overrides: dict[str, _RecordValue] = {}
        self._order = tuple(order)
        self._entries: list[_Entry] = []
        self._index = 0
        self._base_id = next(_STATE_IDS)
        self._saved_id: int | None = self._base_id
        self._generation += 1
        self.break_merge()

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(entry.label for entry in self._entries)

    @property
    def index(self) -> int:
        return self._index

    @property
    def current_id(self) -> int:
        return self._entries[self._index - 1].state_id if self._index else self._base_id

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index < len(self._entries)

    @property
    def undo_label(self) -> str | None:
        return self._entries[self._index - 1].label if self.can_undo else None

    @property
    def redo_label(self) -> str | None:
        return self._entries[self._index].label if self.can_redo else None

    @property
    def is_clean(self) -> bool:
        return self.current_id == self._saved_id

    def break_merge(self) -> None:
        self._merge_key: str | None = None
        self._merge_time = 0.0

    def mark_saved(self, state_id: int | None = None) -> None:
        self._saved_id = self.current_id if state_id is None else state_id
        self._forget_lost_save()
        self.break_merge()

    def _forget_lost_save(self) -> None:
        if self._saved_id != self._base_id and all(
            entry.state_id != self._saved_id for entry in self._entries
        ):
            self._saved_id = None

    def _known_segment(self, identifier: str) -> _RecordValue | None:
        return self._overrides.get(identifier, self._segments.get(identifier))

    def record(
        self, project: PackProject, label: str, *,
        segment: Segment | None = None, merge_key: str | None = None,
        fields_only: bool = False,
    ) -> bool:
        if fields_only and segment is not None:
            raise ValueError("Choose either segment or fields_only")
        metadata = _metadata(project, self._metadata)
        field_changes = tuple(
            _ValueChange(index, before, after)
            for index, (before, after) in enumerate(zip(self._metadata, metadata, strict=True))
            if before != after
        )
        changes = []
        order = self._order
        scanned = None
        if segment is not None:
            before = self._known_segment(segment.id)
            if before is None:
                raise ValueError("Use a full history record for structural or identifier changes")
            after = _segment(segment)
            if before != after:
                changes.append(_ValueChange(segment.id, before, after))
        elif not fields_only:
            scanned = {}
            identifiers = []
            for item in project.segments:
                if item.id in scanned:
                    raise ValueError("Project history requires unique segment identifiers")
                before = self._known_segment(item.id)
                after = _segment(item)
                if before == after:
                    after = before
                else:
                    changes.append(_ValueChange(item.id, before, after))
                scanned[item.id] = after
                identifiers.append(item.id)
            for identifier in self._order:
                if identifier not in scanned:
                    changes.append(_ValueChange(identifier, self._known_segment(identifier), None))
            order = tuple(identifiers)
        delta = _Delta(field_changes, tuple(changes), _order_change(self._order, order))
        if delta.empty:
            return False

        now = monotonic()
        merge = (
            merge_key is not None and merge_key == self._merge_key
            and self._index == len(self._entries) and self._index > 0
            and now - self._merge_time <= _MERGE_SECONDS
        )
        del self._entries[self._index:]
        if merge:
            previous = self._entries.pop().delta
            old_order = (
                previous.order.apply(self._order, False)
                if previous.order is not None else self._order
            )
            delta = _Delta(
                _combine_values(previous.metadata, delta.metadata),
                _combine_values(previous.segments, delta.segments),
                _order_change(old_order, order),
            )
            self._index -= 1
        if not delta.empty:
            self._entries.append(_Entry(label, next(_STATE_IDS), delta))
            self._index += 1
        if len(self._entries) > self.limit:
            self._base_id = self._entries.pop(0).state_id
            self._index -= 1
        self._metadata = metadata
        if scanned is not None:
            self._segments = MappingProxyType(scanned)
            self._overrides = {}
        elif segment is not None and changes:
            self._overrides[segment.id] = changes[0].after
        self._order = order
        self._generation += 1
        self._forget_lost_save()
        self._merge_key = merge_key if not delta.empty else None
        self._merge_time = now
        return True

    def prepare(self, target_index: int) -> HistoryRequest:
        if (
            isinstance(target_index, bool) or not isinstance(target_index, int)
            or not 0 <= target_index <= len(self._entries)
        ):
            raise ValueError("History target index is out of range")
        segments = dict(self._segments)
        segments.update(self._overrides)
        if target_index >= self._index:
            steps = tuple(
                (entry.delta, True) for entry in self._entries[self._index:target_index]
            )
        else:
            steps = tuple(
                (entry.delta, False)
                for entry in reversed(self._entries[target_index:self._index])
            )
        state_id = self._entries[target_index - 1].state_id if target_index else self._base_id
        return HistoryRequest(
            target_index, state_id, self._owner, self._generation,
            _Snapshot(self._metadata, MappingProxyType(segments), self._order), steps,
        )

    def accept(self, request: HistoryRequest, project: PackProject) -> bool:
        if (
            request._owner is not self._owner or request._generation != self._generation
            or not isinstance(project, _ReplayProject)
            or project._history_request is not request
        ):
            return False
        snapshot = project._history_snapshot
        self._metadata = snapshot.metadata
        self._segments = snapshot.segments
        self._overrides = {}
        self._order = snapshot.order
        self._index = request.target_index
        self._generation += 1
        self.break_merge()
        # Do not retain obsolete request paths on the now-live project.
        project._history_request = None
        project._history_snapshot = None
        return True
