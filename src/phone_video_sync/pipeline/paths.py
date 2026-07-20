"""Path helpers for mapping phone remote paths to local work files and outputs."""

from __future__ import annotations

from pathlib import Path


def safe_local_name(remote_path: str) -> str:
    """Turn a phone path into a flat, filesystem-safe local filename."""
    return remote_path.lstrip("/").replace("/", "__").replace("\\", "__")


def output_remote_path(remote_path: str, suffix: str) -> str:
    """Build the on-phone path for the compressed MP4 (HEVC+AAC container)."""
    path = Path(remote_path)
    stem = path.stem
    parent = str(path.parent).replace("\\", "/")
    if parent in {".", ""}:
        return f"{stem}{suffix}.mp4"
    return f"{parent}/{stem}{suffix}.mp4"
