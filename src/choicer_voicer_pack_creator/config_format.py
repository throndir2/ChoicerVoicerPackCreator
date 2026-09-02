from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SECTION = re.compile(r"^\[([^\]]+)\]$")


def quote_string(value: str) -> str:
    """Encode a string using Godot ConfigFile-compatible JSON escaping."""

    return json.dumps(value, ensure_ascii=False)


def render_array(values: list[Any]) -> str:
    return "[" + ", ".join(render_value(value) for value in values) + "]"


def render_value(value: Any) -> str:
    if isinstance(value, str):
        return quote_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return render_array(value)
    raise TypeError(f"Unsupported ConfigFile value: {type(value).__name__}")


def parse_value(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value.startswith('"') or value.startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid ConfigFile value {value!r}: {error.msg}") from error
    try:
        return float(value) if any(token in value.lower() for token in (".", "e")) else int(value)
    except ValueError:
        return value


def parse_config_text(text: str) -> dict[str, dict[str, Any]]:
    sections: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith((";", "#")):
            continue
        section_match = _SECTION.fullmatch(line)
        if section_match:
            name = section_match.group(1)
            current = sections.setdefault(name, {})
            continue
        if current is None:
            raise ValueError(f"Config value before a section on line {line_number}")
        if "=" not in line:
            raise ValueError(f"Invalid ConfigFile line {line_number}: {raw_line!r}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty ConfigFile key on line {line_number}")
        current[key] = parse_value(raw_value)
    return sections


def read_config(path: Path) -> dict[str, dict[str, Any]]:
    return parse_config_text(path.read_text(encoding="utf-8-sig"))


def render_pack_info(title: str, icon: str, authors: list[str], readme: str) -> bytes:
    lines = [
        "[data]",
        "",
        f"title={quote_string(title)}",
        f"icon={quote_string(icon)}",
        f"authors={render_array(authors)}",
        f"readme={quote_string(readme)}",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


def render_clip_metadata(
    caption: str,
    image: str,
    timestamp: float,
    characters: list[str],
) -> bytes:
    lines = [
        "[data]",
        "",
        f"caption={quote_string(caption)}",
        f"image={quote_string(image)}",
        f"dub_timestamps=[{timestamp:.3f}]",
        f"dub_characters={render_array(characters)}",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")
