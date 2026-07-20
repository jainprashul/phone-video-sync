"""Formatting helpers for scan reports and CLI filters."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath

from phone_video_sync.report.types import FileMeta, SIZE_BUCKETS


def folder_of(remote_path: str) -> str:
    parent = str(PurePosixPath(remote_path).parent).replace("\\", "/")
    return parent if parent not in {".", ""} else "/"


def size_bucket_of(size: int) -> str:
    for label, lo, hi in SIZE_BUCKETS:
        if size >= lo and (hi is None or size < hi):
            return label
    return SIZE_BUCKETS[-1][0]


def format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def format_mtime(mtime: int) -> str:
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(mtime)


def output_name_for(remote_path: str, suffix: str = "_hevc") -> str:
    path = PurePosixPath(remote_path)
    return f"{path.stem}{suffix}.mp4"


def choice_label(meta: FileMeta, *, wide: bool = True) -> str:
    """Human-readable line for checkbox/select UIs."""
    flag = "★ " if meta.recommended else "  "
    codec = meta.video_codec or "?"
    if wide:
        return (
            f"{flag}{meta.size_label:>9}  {meta.quality:<5}  {meta.resolution:<10}  "
            f"{meta.duration_label:>7}  {codec:<8}  "
            f"{meta.name}  → {meta.output_name}"
        )
    return f"{flag}{meta.size_label:>9}  {meta.quality}  {meta.name}"


def parse_size(text: str) -> int:
    """Parse sizes like 100MB, 1.5G, 2048."""
    raw = text.strip().upper().replace(" ", "")
    if not raw:
        raise ValueError("empty size")
    multipliers = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if raw.endswith(suffix):
            num = float(raw[: -len(suffix)])
            return int(num * mult)
    return int(float(raw))
