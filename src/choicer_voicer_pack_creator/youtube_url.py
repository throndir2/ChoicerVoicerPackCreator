"""Public video identity without downloader dependencies or tracking parameters."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit


def canonical_youtube_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.username or parsed.password or parsed.port
    ):
        raise ValueError("Enter an http(s) YouTube video URL without credentials or a port.")
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    video_id = ""
    if host in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
        video_id = parts[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts == ["watch"]:
            ids = parse_qs(parsed.query).get("v", [])
            if len(ids) == 1:
                video_id = ids[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Enter a single YouTube video URL, not a channel or playlist URL.")
    return f"https://www.youtube.com/watch?v={video_id}"
