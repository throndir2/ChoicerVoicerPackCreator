from __future__ import annotations

import math
from dataclasses import dataclass

VIDEO_CONVERSION_STEP = "video-conversion"


@dataclass(frozen=True, slots=True)
class ExportStep:
    key: str
    title: str
    kind: str
    estimated_seconds: float


@dataclass(frozen=True, slots=True)
class ExportProgress:
    message: str
    step: str = ""
    fraction: float | None = None
    position: float | None = None
    plan: tuple[ExportStep, ...] = ()
    live: bool = False


@dataclass(frozen=True, slots=True)
class ExportEstimate:
    step_title: str = ""
    step_remaining: float | None = None
    total_remaining: float | None = None
    total_fraction: float | None = None


class ExportEstimator:
    """Refine initial workload estimates using this export's measured timings."""

    def __init__(self) -> None:
        self.plan: tuple[ExportStep, ...] = ()
        self.index: int | None = None
        self.step_started = 0.0
        self.fraction: float | None = None
        self.timings: dict[str, tuple[float, float]] = {}

    def observe(self, update: ExportProgress, elapsed: float) -> None:
        if update.plan:
            self.plan = update.plan
        index = next(
            (index for index, step in enumerate(self.plan) if step.key == update.step), None,
        )
        if index != self.index:
            if self.index is not None:
                previous = self.plan[self.index]
                expected, actual = self.timings.get(previous.kind, (0.0, 0.0))
                self.timings[previous.kind] = (
                    expected + previous.estimated_seconds,
                    actual + max(0.01, elapsed - self.step_started),
                )
            self.index = index
            self.step_started = elapsed
        self.fraction = update.fraction

    def _duration(self, step: ExportStep) -> float:
        measured = self.timings.get(step.kind)
        scale = measured[1] / measured[0] if measured else 1.0
        return step.estimated_seconds * scale

    def estimate(self, elapsed: float) -> ExportEstimate:
        if self.index is None:
            return ExportEstimate()
        step = self.plan[self.index]
        spent = max(0.0, elapsed - self.step_started)
        if self.fraction is not None and self.fraction > 0 and spent > 0:
            remaining = spent * (1 - self.fraction) / self.fraction
        else:
            remaining = self._duration(step) - spent
        # An overdue unmeasured operation has no defensible countdown. Wait for
        # another measurement rather than showing zero while it is still running.
        if remaining <= 0:
            return ExportEstimate(step.title)
        future = sum(self._duration(item) for item in self.plan[self.index + 1:])
        total_remaining = remaining + future
        return ExportEstimate(
            step.title, remaining, total_remaining,
            min(0.99, elapsed / (elapsed + total_remaining)),
        )


def format_remaining(seconds: float | None) -> str:
    if seconds is None:
        return "re-estimating..."
    seconds = max(1, math.ceil(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"about {hours}h {minutes}m remaining"
    if minutes:
        return f"about {minutes}m {seconds}s remaining"
    return f"about {seconds}s remaining"


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:06.3f}"
