from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from choicer_voicer_pack_creator.export_progress import format_remaining


class SeparationProgress:
    """Estimate remaining inference from completed, fixed-size stereo chunks."""

    def __init__(
        self, total_chunks: int, label: str,
        progress: Callable[[str, float | None], None],
    ) -> None:
        if total_chunks < 1:
            raise ValueError("Separation needs at least one chunk")
        self.total_chunks = total_chunks
        self.label = label
        self.progress = progress
        self._started_at: float | None = None
        self._next_chunk = 0

    def start_chunk(self, index: int) -> None:
        if index != self._next_chunk or not 0 <= index < self.total_chunks:
            raise ValueError("Separation chunks must be reported in order")
        now = monotonic()
        if self._started_at is None:
            self._started_at = now
        spent = now - self._started_at
        if index == 0 or spent <= 0:
            estimate = "estimating time after the first chunk"
        else:
            remaining = spent / index * (self.total_chunks - index)
            estimate = format_remaining(remaining)
        self.progress(
            f"{self.label}: chunk {index + 1} of {self.total_chunks}... "
            f"Separation: {estimate} (then writing and verification).",
            index / self.total_chunks * 0.9,
        )
        self._next_chunk += 1
