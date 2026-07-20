"""Listing cache and MediaStore metadata persistence for discover()."""

from __future__ import annotations

from phone_video_sync.adb import RemoteFile
from phone_video_sync.db import Database


def listing_cache_key(remote_root: str, extensions: tuple[str, ...] | list[str]) -> str:
    """Stable key for SQLite listing cache (root + sorted extensions)."""
    exts = ",".join(sorted(extensions))
    return f"{remote_root}|{exts}"


def remote_files_from_cache(payload: list[dict]) -> list[RemoteFile]:
    """Deserialize listing-cache JSON rows into RemoteFile objects."""
    files: list[RemoteFile] = []
    for row in payload:
        path = row.get("path")
        if not path:
            continue
        files.append(
            RemoteFile(
                path=path,
                size=int(row.get("size") or 0),
                mtime=int(row.get("mtime") or 0),
                width=row.get("width"),
                height=row.get("height"),
                duration_ms=row.get("duration_ms"),
                mime_type=row.get("mime_type"),
                title=row.get("title"),
                resolution=row.get("resolution"),
            )
        )
    return files


def remote_files_to_cache_payload(files: list[RemoteFile]) -> list[dict]:
    """Serialize RemoteFile list for listing-cache storage."""
    return [
        {
            "path": rf.path,
            "size": rf.size,
            "mtime": rf.mtime,
            "width": rf.width,
            "height": rf.height,
            "duration_ms": rf.duration_ms,
            "mime_type": rf.mime_type,
            "title": rf.title,
            "resolution": rf.resolution,
        }
        for rf in files
    ]


def _merge_meta(rf: RemoteFile, meta: dict) -> RemoteFile:
    """Overlay SQLite media_meta onto a RemoteFile when size+mtime match."""
    duration_sec = meta.get("duration_sec")
    duration_ms = (
        int(duration_sec * 1000) if duration_sec is not None else rf.duration_ms
    )
    return RemoteFile(
        path=rf.path,
        size=rf.size,
        mtime=rf.mtime,
        width=meta.get("width") if meta.get("width") is not None else rf.width,
        height=meta.get("height") if meta.get("height") is not None else rf.height,
        duration_ms=duration_ms,
        mime_type=meta.get("mime_type") or rf.mime_type,
        title=meta.get("title") or rf.title,
        resolution=meta.get("resolution") or rf.resolution,
    )


def apply_media_meta_cache(db: Database, files: list[RemoteFile]) -> list[RemoteFile]:
    """Merge cached probe/MediaStore fields onto a remote file list."""
    cached = db.get_media_meta_many([(rf.path, rf.size, rf.mtime) for rf in files])
    if not cached:
        return files
    return [
        _merge_meta(rf, cached[rf.path]) if rf.path in cached else rf for rf in files
    ]


def persist_media_meta_from_remote(db: Database, files: list[RemoteFile]) -> None:
    """Save MediaStore dimensions/duration from a fresh ADB listing scan."""
    rows = []
    for rf in files:
        if not any([rf.width, rf.height, rf.duration_ms, rf.mime_type, rf.resolution]):
            continue
        rows.append(
            {
                "remote_path": rf.path,
                "size": rf.size,
                "mtime": rf.mtime,
                "width": rf.width,
                "height": rf.height,
                "duration_sec": (rf.duration_ms / 1000.0) if rf.duration_ms else None,
                "mime_type": rf.mime_type,
                "title": rf.title,
                "resolution": rf.resolution
                or (f"{rf.width}x{rf.height}" if rf.width and rf.height else None),
                "source": "mediastore",
            }
        )
    if rows:
        db.upsert_media_meta_batch(rows)
