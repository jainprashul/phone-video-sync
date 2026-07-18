"""Export scan reports to logs/ as markdown (+ optional CSV for recommended)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from phone_video_sync.adb import RemoteFile
from phone_video_sync.models import VideoRecord
from phone_video_sync.report import ScanBreakdown, format_bytes


def save_scan_report(
    log_dir: Path,
    *,
    title: str,
    remote_files: list[RemoteFile],
    pending: list[VideoRecord],
    breakdown: ScanBreakdown,
    output_suffix: str,
    encoder: str,
    delete_mode: str,
) -> Path:
    """Write a markdown scan report under log_dir; return the path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = log_dir / f"report-{ts}.md"

    total_bytes = sum(p.size for p in pending)
    est_out = int(total_bytes * 0.4)
    rec_bytes = sum(r.size for r in breakdown.recommended)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines: list[str] = [
        f"# {title}",
        "",
        f"Generated: `{now}`",
        "",
        "## Overview",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Videos on device | {len(remote_files)} |",
        f"| Pending | {len(pending)} |",
        f"| Pending bytes | {format_bytes(total_bytes)} |",
        f"| Est. output (~40%) | {format_bytes(est_out)} |",
        f"| Est. savings | {format_bytes(max(0, total_bytes - est_out))} |",
        f"| Output suffix | `{output_suffix}` |",
        f"| Encoder | `{encoder}` |",
        f"| Delete mode | `{delete_mode}` |",
        "",
        "## Pending by size",
        "",
        "| # | Bucket | Files | Bytes | Est. save |",
        "|--:|--------|------:|------:|----------:|",
    ]
    for i, group in enumerate(breakdown.by_size, start=1):
        lines.append(
            f"| {i} | {group.key} | {group.count} | "
            f"{format_bytes(group.bytes)} | {format_bytes(group.est_savings)} |"
        )

    lines.extend(
        [
            "",
            "## Pending by folder",
            "",
            "| # | Folder | Files | Bytes | Est. save |",
            "|--:|--------|------:|------:|----------:|",
        ]
    )
    for i, group in enumerate(breakdown.by_folder, start=1):
        lines.append(
            f"| {i} | `{group.key}` | {group.count} | "
            f"{format_bytes(group.bytes)} | {format_bytes(group.est_savings)} |"
        )

    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            breakdown.recommend_reason,
            "",
            f"**Recommended set:** {len(breakdown.recommended)} file(s), "
            f"{format_bytes(rec_bytes)} "
            f"(est. save {format_bytes(int(rec_bytes * 0.6))})",
            "",
        ]
    )

    if breakdown.recommended:
        lines.extend(
            [
                "## All recommended files",
                "",
                "| # | Size | Dur | Quality | Resolution | V-Codec | A-Codec | "
                "Bitrate | FPS | Type | Profile | Modified | Name | Output | Folder | Path |",
                "|--:|-----:|----:|---------|------------|---------|---------|"
                "---------|-----|------|---------|----------|------|--------|--------|------|",
            ]
        )
        ordered = sorted(breakdown.recommended, key=lambda r: r.size, reverse=True)
        for i, item in enumerate(ordered, start=1):
            meta = breakdown.metas.get(item.remote_path)
            if not meta:
                continue
            vcodec = meta.video_codec or "?"
            if meta.pix_fmt:
                vcodec = f"{vcodec}/{meta.pix_fmt}"
            profile = meta.profile or "?"
            if meta.level and meta.level not in {"?", "-99", "0"}:
                profile = f"{profile}@{meta.level}"
            mime = meta.mime_type or (meta.container or meta.extension)
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(i),
                        meta.size_label,
                        meta.duration_label,
                        meta.quality,
                        meta.resolution,
                        vcodec,
                        meta.audio_codec or "?",
                        meta.bitrate_label,
                        meta.fps or "?",
                        str(mime),
                        profile,
                        meta.modified,
                        f"`{meta.name}`",
                        f"`{meta.output_name}`",
                        f"`{meta.folder}`",
                        f"`{meta.remote_path}`",
                    ]
                )
                + " |"
            )

        # Companion CSV for spreadsheets
        csv_path = log_dir / f"report-{ts}-recommended.csv"
        _write_recommended_csv(csv_path, breakdown)
        lines.extend(
            [
                "",
                f"CSV (recommended only): `{csv_path.name}`",
                "",
            ]
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_recommended_csv(path: Path, breakdown: ScanBreakdown) -> None:
    import csv

    ordered = sorted(breakdown.recommended, key=lambda r: r.size, reverse=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "size_bytes",
                "size",
                "duration",
                "quality",
                "resolution",
                "video_codec",
                "audio_codec",
                "bitrate",
                "fps",
                "mime_type",
                "profile",
                "pix_fmt",
                "modified",
                "name",
                "output_name",
                "folder",
                "remote_path",
                "status",
            ]
        )
        for item in ordered:
            meta = breakdown.metas.get(item.remote_path)
            if not meta:
                continue
            writer.writerow(
                [
                    meta.size,
                    meta.size_label,
                    meta.duration_label,
                    meta.quality,
                    meta.resolution,
                    meta.video_codec or "",
                    meta.audio_codec or "",
                    meta.bitrate_label,
                    meta.fps or "",
                    meta.mime_type or "",
                    meta.profile or "",
                    meta.pix_fmt or "",
                    meta.modified,
                    meta.name,
                    meta.output_name,
                    meta.folder,
                    meta.remote_path,
                    meta.status,
                ]
            )
