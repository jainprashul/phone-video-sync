"""Typer CLI for phone-video-sync (`phone-sync`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.pretty import pprint

from phone_video_sync import __version__
from phone_video_sync.config import (
    ConfigError,
    load_config,
    validate_config,
)
from phone_video_sync.logging_setup import get_console, setup_logging
from phone_video_sync.models import Config
from phone_video_sync.pipeline import Pipeline

app = typer.Typer(
    name="phone-sync",
    help="Detect USB phone videos via ADB, compress with NVENC HEVC, verify, and sync back.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Show or validate configuration.")
app.add_typer(config_app, name="config")


def _load(config: Optional[Path], project_root: Optional[Path]) -> Config:
    root = project_root or Path.cwd()
    try:
        return load_config(config, project_root=root)
    except ConfigError as exc:
        get_console().print(f"[bold red]Config error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _pipeline(ctx: typer.Context, run_name: str, *, level: int | None = None) -> Pipeline:
    cfg = _load(ctx.obj.get("config_path"), None)
    log_level = level
    if log_level is None:
        log_level = logging.DEBUG if ctx.obj.get("verbose") else logging.INFO
    log_path = setup_logging(cfg.log_dir, level=log_level, run_name=run_name)
    get_console().print(f"[dim]Log: {log_path}[/dim]")
    return Pipeline(cfg)


@app.callback()
def main(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config.yaml (default: ./config.yaml)",
        exists=False,
        dir_okay=False,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging"),
) -> None:
    """phone-video-sync CLI."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["verbose"] = verbose


@app.command("scan")
def scan_cmd(
    ctx: typer.Context,
    select: bool = typer.Option(
        False,
        "--select",
        "-s",
        help="After the report, interactively choose folders/sizes/recommended to process",
    ),
) -> None:
    """Discover videos and show a folder/size report (no transfers unless --select)."""
    pipeline = _pipeline(ctx, "scan")
    report = pipeline.scan(select=select)
    if report.errors and report.failed and report.done == 0:
        raise typer.Exit(code=1)


@app.command("process")
def process_cmd(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Process at most N pending videos"
    ),
    select: bool = typer.Option(
        False,
        "--select",
        "-s",
        help="Show report then interactively choose what to process",
    ),
    recommend: bool = typer.Option(
        False,
        "--recommend",
        "-r",
        help="Process only the recommended high-value set (≥100 MB / largest quartile)",
    ),
    folder: Optional[list[str]] = typer.Option(
        None,
        "--folder",
        "-f",
        help="Only process videos under this folder (repeatable)",
    ),
    min_size: Optional[str] = typer.Option(
        None,
        "--min-size",
        help="Minimum file size (e.g. 100MB, 1G)",
    ),
    max_size: Optional[str] = typer.Option(
        None,
        "--max-size",
        help="Maximum file size (e.g. 500MB)",
    ),
) -> None:
    """Pull, compress, verify, push, and archive/delete originals."""
    pipeline = _pipeline(ctx, "process")
    report = pipeline.process(
        yes=yes,
        limit=limit,
        select=select,
        folders=folder,
        min_size=min_size,
        max_size=max_size,
        recommend=recommend,
    )
    if report.errors and report.failed and report.done == 0:
        raise typer.Exit(code=1)


@app.command("watch")
def watch_cmd(
    ctx: typer.Context,
    interval: Optional[float] = typer.Option(
        None,
        "--interval",
        "-i",
        help="Seconds between device polls (default: config watch_interval_sec)",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Process on the next connect, then exit",
    ),
    yes: bool = typer.Option(
        True,
        "--yes/--prompt",
        help="Skip confirmation when processing (default: skip)",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Process at most N pending videos per connect"
    ),
) -> None:
    """Watch for USB/ADB connect and process new videos automatically."""
    pipeline = _pipeline(ctx, "watch")
    try:
        pipeline.watch(interval=interval, once=once, yes=yes, limit=limit)
    except KeyboardInterrupt:
        get_console().print("\n[yellow]Watch stopped.[/yellow]")


@app.command("verify")
def verify_cmd(
    ctx: typer.Context,
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Verify at most N done videos"
    ),
) -> None:
    """Check that compressed outputs for done videos still exist on the phone."""
    pipeline = _pipeline(ctx, "verify")
    try:
        result = pipeline.verify(limit=limit)
    except Exception as exc:  # noqa: BLE001
        get_console().print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if result.get("missing_output") or result.get("missing_path"):
        raise typer.Exit(code=1)


@app.command("clean")
def clean_cmd(
    ctx: typer.Context,
    work: bool = typer.Option(
        True,
        "--work/--no-work",
        help="Clear local work/in, work/out, work/failed (default: yes)",
    ),
    archive: bool = typer.Option(
        False,
        "--archive",
        help="Also permanently delete the on-phone archive folder",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip archive confirmation"),
) -> None:
    """Clean local work files and optionally purge the on-phone archive."""
    pipeline = _pipeline(ctx, "clean")
    try:
        pipeline.clean(work=work, archive=archive, yes=yes)
    except Exception as exc:  # noqa: BLE001
        get_console().print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("stats")
def stats_cmd(ctx: typer.Context) -> None:
    """Show detailed SQLite tracking statistics."""
    pipeline = _pipeline(ctx, "stats", level=logging.WARNING)
    pipeline.stats()


# --- Backward-compatible aliases ---


@app.command("run", hidden=False)
def run_cmd(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Process at most N pending videos (useful for testing)"
    ),
    select: bool = typer.Option(False, "--select", "-s", help="Interactive selection"),
    recommend: bool = typer.Option(False, "--recommend", "-r", help="Recommended set only"),
    folder: Optional[list[str]] = typer.Option(None, "--folder", "-f", help="Folder filter"),
    min_size: Optional[str] = typer.Option(None, "--min-size", help="Min size e.g. 100MB"),
    max_size: Optional[str] = typer.Option(None, "--max-size", help="Max size e.g. 500MB"),
) -> None:
    """Alias for `process`."""
    pipeline = _pipeline(ctx, "run")
    report = pipeline.process(
        yes=yes,
        limit=limit,
        select=select,
        folders=folder,
        min_size=min_size,
        max_size=max_size,
        recommend=recommend,
    )
    if report.errors and report.failed and report.done == 0:
        raise typer.Exit(code=1)


@app.command("dry-run")
def dry_run_cmd(
    ctx: typer.Context,
    select: bool = typer.Option(False, "--select", "-s", help="Interactive selection"),
) -> None:
    """Alias for `scan`."""
    pipeline = _pipeline(ctx, "dry-run")
    pipeline.scan(select=select)


@app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """Compact status summary (see also `stats`)."""
    pipeline = _pipeline(ctx, "status", level=logging.WARNING)
    pipeline.status_table()


@app.command("purge-archive")
def purge_archive_cmd(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete the on-phone compressed archive folder (alias: `clean --no-work --archive`)."""
    pipeline = _pipeline(ctx, "purge")
    try:
        pipeline.purge_archive(yes=yes)
    except Exception as exc:  # noqa: BLE001
        get_console().print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the effective configuration."""
    cfg = _load(ctx.obj.get("config_path"), None)
    data = {
        "db_path": str(cfg.db_path),
        "work_dir": str(cfg.work_dir),
        "log_dir": str(cfg.log_dir),
        "remote_root": cfg.remote_root,
        "archive_root": cfg.archive_root,
        "extensions": cfg.extensions,
        "skip_prefixes": cfg.skip_prefixes,
        "video_encoder": cfg.video_encoder,
        "preset": cfg.preset,
        "cq": cfg.cq,
        "audio_bitrate": cfg.audio_bitrate,
        "duration_tolerance_sec": cfg.duration_tolerance_sec,
        "require_smaller": cfg.require_smaller,
        "encode_workers": cfg.encode_workers,
        "max_attempts": cfg.max_attempts,
        "retry_backoff_base_sec": cfg.retry_backoff_base_sec,
        "subprocess_timeout_sec": cfg.subprocess_timeout_sec,
        "delete_mode": cfg.delete_mode,
        "adb_path": cfg.adb_path,
        "ffmpeg_path": cfg.ffmpeg_path,
        "ffprobe_path": cfg.ffprobe_path,
        "output_suffix": cfg.output_suffix,
        "watch_interval_sec": cfg.watch_interval_sec,
        "project_root": str(cfg.project_root),
        "version": __version__,
    }
    pprint(data)


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate configuration and tool availability."""
    cfg = _load(ctx.obj.get("config_path"), None)
    issues = validate_config(cfg, check_tools=True)
    console = get_console()
    if issues:
        for issue in issues:
            console.print(f"[red]• {issue}[/red]")
        raise typer.Exit(code=1)
    console.print("[green]Configuration OK[/green]")


if __name__ == "__main__":
    app()
