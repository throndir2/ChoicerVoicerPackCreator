"""Lightweight, strictly validated backing-generation choices (no Qt or ML imports)."""
from __future__ import annotations

from typing import Literal, cast

BackingMode = Literal["remove_all_vocals", "keep_singing"]
REMOVE_ALL_VOCALS: BackingMode = "remove_all_vocals"
KEEP_SINGING: BackingMode = "keep_singing"


def validate_backing_mode(value: object) -> BackingMode:
    if not isinstance(value, str) or value not in (REMOVE_ALL_VOCALS, KEEP_SINGING):
        raise ValueError(f"Unsupported backing generation mode: {value!r}")
    return cast(BackingMode, value)
