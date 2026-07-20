"""Build and update per-file metadata on a scan breakdown."""

from __future__ import annotations

from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

from phone_video_sync.adb.media import format_duration, quality_label
from phone_video_sync.models import VideoRecord
from phone_video_sync.report.format import (
    folder_of,
    format_bytes,
    format_mtime,
    output_name_for,
    size_bucket_of,
)
from phone_video_sync.report.types import FileMeta, ScanBreakdown


def build_file_meta(
    rec: VideoRecord,
    *,
    recommended_paths: set[str],
    output_suffix: str = "_hevc",
    remote: Any | None = None,
    probe: Any | None = None,
) -> FileMeta:
    """Merge VideoRecord + optional RemoteFile + ffprobe into display metadata."""
    path = PurePosixPath(rec.remote_path)
    ext = path.suffix.lstrip(".").lower() or "?"
    est_out = int(rec.size * 0.4)

    width = getattr(remote, "width", None) if remote else None
    height = getattr(remote, "height", None) if remote else None
    duration_ms = getattr(remote, "duration_ms", None) if remote else None
    mime = getattr(remote, "mime_type", None) if remote else None
    title = getattr(remote, "title", None) if remote else None
    res_str = getattr(remote, "resolution", None) if remote else None

    video_codec = audio_codec = fps = pix_fmt = profile = level = None
    bitrate = None
    duration_sec = (duration_ms / 1000.0) if duration_ms else None

    if probe is not None:
        width = probe.width or width
        height = probe.height or height
        if probe.duration_sec:
            duration_sec = probe.duration_sec
        video_codec = probe.video_codec
        audio_codec = probe.audio_codec
        bitrate = probe.bitrate
        fps = probe.fps
        pix_fmt = probe.pix_fmt
        profile = probe.profile
        level = probe.level

    if width and height:
        res_str = f"{width}x{height}"
    elif not res_str:
        res_str = "?"

    bitrate_label = "?"
    if bitrate:
        bitrate_label = (
            f"{bitrate / 1_000_000:.1f} Mbps"
            if bitrate >= 1_000_000
            else f"{bitrate // 1000} kbps"
        )

    return FileMeta(
        remote_path=rec.remote_path,
        name=path.name,
        folder=folder_of(rec.remote_path),
        extension=ext,
        size=rec.size,
        size_label=format_bytes(rec.size),
        bucket=size_bucket_of(rec.size),
        mtime=rec.mtime,
        modified=format_mtime(rec.mtime),
        status=rec.status.value if hasattr(rec.status, "value") else str(rec.status),
        attempts=rec.attempts,
        recommended=rec.remote_path in recommended_paths,
        output_name=output_name_for(rec.remote_path, output_suffix),
        est_out_bytes=est_out,
        est_save_bytes=max(0, rec.size - est_out),
        width=width,
        height=height,
        resolution=res_str or "?",
        quality=quality_label(width, height),
        duration_sec=duration_sec,
        duration_label=format_duration(duration_sec),
        mime_type=mime,
        container=ext.upper() if ext != "?" else None,
        video_codec=video_codec,
        audio_codec=audio_codec,
        bitrate=bitrate,
        bitrate_label=bitrate_label,
        fps=fps,
        pix_fmt=pix_fmt,
        profile=profile,
        level=level,
        title=title,
    )


def apply_remote_map(
    breakdown: ScanBreakdown,
    remote_by_path: dict[str, Any],
    *,
    output_suffix: str,
) -> None:
    """Refresh metas using MediaStore-enriched RemoteFile objects."""
    rec_paths = {r.remote_path for r in breakdown.recommended}
    for rec in breakdown.pending:
        remote = remote_by_path.get(rec.remote_path)
        prev = breakdown.metas.get(rec.remote_path)
        probe = None
        if prev and prev.video_codec:
            # Preserve prior probe fields when refreshing from MediaStore
            probe = SimpleNamespace(
                width=prev.width,
                height=prev.height,
                duration_sec=prev.duration_sec,
                video_codec=prev.video_codec,
                audio_codec=prev.audio_codec,
                bitrate=prev.bitrate,
                fps=prev.fps,
                pix_fmt=prev.pix_fmt,
                profile=prev.profile,
                level=prev.level,
            )
        breakdown.metas[rec.remote_path] = build_file_meta(
            rec,
            recommended_paths=rec_paths,
            output_suffix=output_suffix,
            remote=remote,
            probe=probe,
        )


def apply_probe_to_meta(
    breakdown: ScanBreakdown,
    remote_path: str,
    probe: Any,
    *,
    output_suffix: str,
    remote: Any | None = None,
) -> None:
    """Update a single file's meta after ffprobe header read."""
    rec = next((r for r in breakdown.pending if r.remote_path == remote_path), None)
    if rec is None:
        return
    rec_paths = {r.remote_path for r in breakdown.recommended}
    breakdown.metas[remote_path] = build_file_meta(
        rec,
        recommended_paths=rec_paths,
        output_suffix=output_suffix,
        remote=remote,
        probe=probe,
    )
