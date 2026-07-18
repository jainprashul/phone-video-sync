"""Scan report: group pending videos by folder and size, rank recommendations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from phone_video_sync.models import VideoRecord

# Size buckets (upper bound exclusive except last)
SIZE_BUCKETS: list[tuple[str, int, int | None]] = [
    ("tiny (<20 MB)", 0, 20 * 1024 * 1024),
    ("small (20–100 MB)", 20 * 1024 * 1024, 100 * 1024 * 1024),
    ("medium (100–500 MB)", 100 * 1024 * 1024, 500 * 1024 * 1024),
    ("large (500 MB–2 GB)", 500 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
    ("huge (≥2 GB)", 2 * 1024 * 1024 * 1024, None),
]

# Prefer these path fragments when ranking recommendations
PREFERRED_FOLDER_HINTS = (
    "/dcim/camera",
    "/dcim/cameraroll",
    "/dcim/",
    "/movies/",
    "/pictures/",
)


@dataclass
class FileMeta:
    """Display metadata: ADB listing + MediaStore + optional ffprobe headers."""

    remote_path: str
    name: str
    folder: str
    extension: str
    size: int
    size_label: str
    bucket: str
    mtime: int
    modified: str
    status: str
    attempts: int
    recommended: bool
    output_name: str
    est_out_bytes: int
    est_save_bytes: int
    width: int | None = None
    height: int | None = None
    resolution: str = "?"
    quality: str = "?"
    duration_sec: float | None = None
    duration_label: str = "?"
    mime_type: str | None = None
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    bitrate: int | None = None
    bitrate_label: str = "?"
    fps: str | None = None
    pix_fmt: str | None = None
    profile: str | None = None
    level: str | None = None
    title: str | None = None


@dataclass
class GroupStats:
    key: str
    count: int
    bytes: int
    records: list[VideoRecord] = field(default_factory=list)

    @property
    def est_savings(self) -> int:
        # Rough HEVC estimate: keep ~40% of original → save ~60%
        return int(self.bytes * 0.6)


@dataclass
class ScanBreakdown:
    pending: list[VideoRecord]
    by_folder: list[GroupStats]
    by_size: list[GroupStats]
    recommended: list[VideoRecord]
    recommend_reason: str
    metas: dict[str, FileMeta] = field(default_factory=dict)


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


def build_file_meta(
    rec: VideoRecord,
    *,
    recommended_paths: set[str],
    output_suffix: str = "_hevc",
    remote: Any | None = None,
    probe: Any | None = None,
) -> FileMeta:
    from phone_video_sync.adb import format_duration, quality_label

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
        bitrate_label = f"{bitrate / 1_000_000:.1f} Mbps" if bitrate >= 1_000_000 else f"{bitrate // 1000} kbps"

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
            # keep prior probe fields via a tiny namespace
            from types import SimpleNamespace

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

def _group(
    records: list[VideoRecord],
    key_fn,
    *,
    order: list[str] | None = None,
) -> list[GroupStats]:
    buckets: dict[str, list[VideoRecord]] = defaultdict(list)
    for rec in records:
        buckets[key_fn(rec)].append(rec)

    keys = order if order is not None else sorted(
        buckets.keys(),
        key=lambda k: sum(r.size for r in buckets[k]),
        reverse=True,
    )
    result: list[GroupStats] = []
    for key in keys:
        items = buckets.get(key, [])
        if not items and order is not None:
            continue
        if not items:
            continue
        result.append(
            GroupStats(
                key=key,
                count=len(items),
                bytes=sum(r.size for r in items),
                records=sorted(items, key=lambda r: r.size, reverse=True),
            )
        )
    # Include any keys not in explicit order
    if order is not None:
        for key, items in buckets.items():
            if key in order:
                continue
            result.append(
                GroupStats(
                    key=key,
                    count=len(items),
                    bytes=sum(r.size for r in items),
                    records=sorted(items, key=lambda r: r.size, reverse=True),
                )
            )
    return result


def _folder_priority(path: str) -> int:
    lower = path.lower()
    for i, hint in enumerate(PREFERRED_FOLDER_HINTS):
        if hint in lower:
            return i
    return len(PREFERRED_FOLDER_HINTS)


def recommend(pending: list[VideoRecord]) -> tuple[list[VideoRecord], str]:
    """Recommend high-value targets: medium+ size, prefer camera folders.

    Falls back to the largest 25% of pending (min 1) if nothing matches.
    """
    if not pending:
        return [], "nothing pending"

    medium_plus = [
        r
        for r in pending
        if size_bucket_of(r.size)
        in {
            "medium (100–500 MB)",
            "large (500 MB–2 GB)",
            "huge (≥2 GB)",
        }
    ]

    if medium_plus:
        ranked = sorted(
            medium_plus,
            key=lambda r: (_folder_priority(r.remote_path), -r.size),
        )
        reason = (
            f"{len(ranked)} file(s) ≥100 MB "
            f"({format_bytes(sum(r.size for r in ranked))}) — "
            "best space savings; camera folders ranked first"
        )
        return ranked, reason

    ordered = sorted(pending, key=lambda r: r.size, reverse=True)
    n = max(1, len(ordered) // 4)
    ranked = ordered[:n]
    reason = (
        f"No files ≥100 MB; recommending largest {n} of {len(pending)} "
        f"({format_bytes(sum(r.size for r in ranked))})"
    )
    return ranked, reason


def build_scan_breakdown(
    pending: list[VideoRecord],
    *,
    output_suffix: str = "_hevc",
) -> ScanBreakdown:
    by_folder = _group(pending, lambda r: folder_of(r.remote_path))
    bucket_order = [label for label, _, _ in SIZE_BUCKETS]
    by_size = _group(pending, lambda r: size_bucket_of(r.size), order=bucket_order)
    recommended, reason = recommend(pending)
    rec_paths = {r.remote_path for r in recommended}
    metas = {
        r.remote_path: build_file_meta(
            r, recommended_paths=rec_paths, output_suffix=output_suffix
        )
        for r in pending
    }
    return ScanBreakdown(
        pending=pending,
        by_folder=by_folder,
        by_size=by_size,
        recommended=recommended,
        recommend_reason=reason,
        metas=metas,
    )


def filter_pending(
    pending: list[VideoRecord],
    *,
    folders: list[str] | None = None,
    size_labels: list[str] | None = None,
    min_bytes: int | None = None,
    max_bytes: int | None = None,
    paths: set[str] | None = None,
    recommended_only: bool = False,
) -> list[VideoRecord]:
    result = list(pending)
    if recommended_only:
        rec, _ = recommend(result)
        result = rec
    if paths is not None:
        result = [r for r in result if r.remote_path in paths]
    if folders:
        norms = [f.rstrip("/") for f in folders]
        result = [
            r
            for r in result
            if any(
                folder_of(r.remote_path) == n
                or folder_of(r.remote_path).startswith(n + "/")
                or n in folder_of(r.remote_path)
                for n in norms
            )
        ]
    if size_labels:
        labels = set(size_labels)
        result = [r for r in result if size_bucket_of(r.size) in labels]
    if min_bytes is not None:
        result = [r for r in result if r.size >= min_bytes]
    if max_bytes is not None:
        result = [r for r in result if r.size <= max_bytes]
    return result


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
