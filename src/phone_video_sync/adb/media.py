"""MediaStore parsing and display helpers for video metadata."""

from __future__ import annotations

import re


def parse_mediastore_row(line: str) -> dict[str, str]:
    """Parse a single `content query` Row line into a field dict."""
    # Row: 0 _data=/path/file.mp4, mime_type=video/mp4, width=1920, ...
    if "Row:" not in line:
        return {}
    payload = line.split("Row:", 1)[1]
    payload = re.sub(r"^\s*\d+\s+", "", payload.strip())
    fields: dict[str, str] = {}
    for part in re.split(r", (?=[A-Za-z0-9_]+=)", payload):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def quality_label(width: int | None, height: int | None) -> str:
    """Human-readable resolution tier (4K, 1080p, etc.)."""
    if not width or not height:
        return "?"
    long_edge = max(width, height)
    if long_edge >= 7680:
        return "8K"
    if long_edge >= 3840:
        return "4K"
    if long_edge >= 2560:
        return "1440p"
    if long_edge >= 1920:
        return "1080p"
    if long_edge >= 1280:
        return "720p"
    if long_edge >= 854:
        return "480p"
    if long_edge >= 640:
        return "360p"
    return f"{width}x{height}"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "?"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
