"""Rich console tables for scan and process-planning reports."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from phone_video_sync.adb import RemoteFile
from phone_video_sync.models import VideoRecord
from phone_video_sync.report import ScanBreakdown, format_bytes, sort_recommended


def render_scan_overview(
    console: Console,
    *,
    title: str,
    remote_count: int,
    pending: list[VideoRecord],
    output_suffix: str,
    encoder: str,
    encode_workers: int,
    delete_mode: str,
) -> None:
    """Print the high-level metrics table at the top of a scan report."""
    total_bytes = sum(p.size for p in pending)
    est_out = int(total_bytes * 0.4)

    overview = Table(title=title)
    overview.add_column("Metric")
    overview.add_column("Value", justify="right")
    overview.add_row("Videos on device", str(remote_count))
    overview.add_row("Pending / to process", str(len(pending)))
    overview.add_row("Pending bytes", format_bytes(total_bytes))
    overview.add_row("Est. output (≈40%)", format_bytes(est_out))
    overview.add_row("Est. savings", format_bytes(max(0, total_bytes - est_out)))
    overview.add_row("Output suffix", output_suffix)
    overview.add_row("Encoder", encoder)
    overview.add_row("Workers", str(encode_workers))
    overview.add_row("Delete mode", delete_mode)
    console.print(overview)


def render_size_breakdown(console: Console, breakdown: ScanBreakdown) -> None:
    """Pending files grouped by size bucket."""
    table = Table(title="Pending by size")
    table.add_column("#", justify="right")
    table.add_column("Bucket")
    table.add_column("Files", justify="right")
    table.add_column("Bytes", justify="right")
    table.add_column("Est. save", justify="right")
    for i, group in enumerate(breakdown.by_size, start=1):
        table.add_row(
            str(i),
            group.key,
            str(group.count),
            format_bytes(group.bytes),
            format_bytes(group.est_savings),
        )
    console.print(table)


def render_folder_breakdown(console: Console, breakdown: ScanBreakdown, *, limit: int = 25) -> None:
    """Pending files grouped by parent folder (top N)."""
    table = Table(title=f"Pending by folder (top {limit})")
    table.add_column("#", justify="right")
    table.add_column("Folder")
    table.add_column("Files", justify="right")
    table.add_column("Bytes", justify="right")
    table.add_column("Est. save", justify="right")
    for i, group in enumerate(breakdown.by_folder[:limit], start=1):
        table.add_row(
            str(i),
            group.key,
            str(group.count),
            format_bytes(group.bytes),
            format_bytes(group.est_savings),
        )
    if len(breakdown.by_folder) > limit:
        table.add_row(
            "",
            f"… and {len(breakdown.by_folder) - limit} more folders",
            "",
            "",
            "",
        )
    console.print(table)


def render_recommendation_summary(console: Console, breakdown: ScanBreakdown) -> None:
    """One-line recommendation reason plus recommended set size."""
    rec_bytes = sum(r.size for r in breakdown.recommended)
    table = Table(title="Recommendation")
    table.add_column("Detail")
    table.add_row(breakdown.recommend_reason)
    table.add_row(
        f"[bold]Recommended set:[/bold] {len(breakdown.recommended)} file(s), "
        f"{format_bytes(rec_bytes)} (est. save {format_bytes(int(rec_bytes * 0.6))})"
    )
    console.print(table)


def _format_vcodec(meta: object) -> str:
    vcodec = getattr(meta, "video_codec", None) or "?"
    pix_fmt = getattr(meta, "pix_fmt", None)
    if pix_fmt:
        vcodec = f"{vcodec}/{pix_fmt}"
    return vcodec


def _format_profile(meta: object) -> str:
    profile = getattr(meta, "profile", None) or "?"
    level = getattr(meta, "level", None)
    if level and level not in {"?", "-99", "0"}:
        return f"{profile}@{level}"
    return profile


def render_recommended_meta_table(
    console: Console,
    breakdown: ScanBreakdown,
    *,
    output_suffix: str,
) -> None:
    """Full metadata table for every recommended file."""
    if not breakdown.recommended:
        return

    table = Table(
        title=(
            f"All recommended ({len(breakdown.recommended)}) — "
            f"resolution / quality / codec / type "
            f"(output suffix '{output_suffix}')"
        ),
        show_lines=False,
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Size", justify="right")
    table.add_column("Dur")
    table.add_column("Quality")
    table.add_column("Resolution")
    table.add_column("V-Codec")
    table.add_column("A-Codec")
    table.add_column("Bitrate")
    table.add_column("FPS")
    table.add_column("Type")
    table.add_column("Profile")
    table.add_column("Modified")
    table.add_column("Name")
    table.add_column("Output")
    table.add_column("Folder")

    ordered = sort_recommended(
        breakdown.recommended,
        breakdown.metas,
        output_suffix=output_suffix,
    )
    for i, item in enumerate(ordered, start=1):
        meta = breakdown.metas.get(item.remote_path)
        if not meta:
            continue
        folder = meta.folder
        if len(folder) > 40:
            folder = "…" + folder[-37:]
        mime = meta.mime_type or (meta.container or meta.extension)
        table.add_row(
            str(i),
            meta.size_label,
            meta.duration_label,
            meta.quality,
            meta.resolution,
            _format_vcodec(meta),
            meta.audio_codec or "?",
            meta.bitrate_label,
            meta.fps or "?",
            str(mime),
            _format_profile(meta),
            meta.modified,
            meta.name,
            meta.output_name,
            folder,
        )
    console.print(table)


def render_scan_report(
    console: Console,
    *,
    title: str,
    remote_files: list[RemoteFile],
    pending: list[VideoRecord],
    breakdown: ScanBreakdown,
    output_suffix: str,
    encoder: str,
    encode_workers: int,
    delete_mode: str,
) -> None:
    """Render the full scan / dry-run report to the console."""
    render_scan_overview(
        console,
        title=title,
        remote_count=len(remote_files),
        pending=pending,
        output_suffix=output_suffix,
        encoder=encoder,
        encode_workers=encode_workers,
        delete_mode=delete_mode,
    )
    if not pending:
        return

    render_size_breakdown(console, breakdown)
    render_folder_breakdown(console, breakdown)
    render_recommendation_summary(console, breakdown)
    render_recommended_meta_table(console, breakdown, output_suffix=output_suffix)
