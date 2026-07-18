"""Pipeline orchestrator: discover, diff, confirm, parallel stages, report."""

from __future__ import annotations

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from phone_video_sync.adb import AdbClient, DeviceError, RemoteFile
from phone_video_sync.config import Config, ensure_runtime_dirs, resolve_tools
from phone_video_sync.db import Database
from phone_video_sync.ffmpeg import encode, ensure_encoder_available, get_media_info
from phone_video_sync.logging_setup import get_console
from phone_video_sync.models import RunReport, VideoRecord, VideoStatus
from phone_video_sync.verify import check

logger = logging.getLogger(__name__)


def _safe_local_name(remote_path: str) -> str:
    return remote_path.lstrip("/").replace("/", "__").replace("\\", "__")


def _output_remote_path(remote_path: str, suffix: str) -> str:
    path = Path(remote_path)
    stem = path.stem
    # Always push as mp4 for HEVC+AAC in MP4 container
    parent = str(path.parent).replace("\\", "/")
    if parent in {".", ""}:
        return f"{stem}{suffix}.mp4"
    return f"{parent}/{stem}{suffix}.mp4"


def _format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


class Pipeline:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        ensure_runtime_dirs(cfg)
        self.tools = resolve_tools(cfg)
        self.db = Database(cfg.db_path)
        self.adb = AdbClient(
            self.tools["adb"],
            timeout=min(cfg.subprocess_timeout_sec, 300),
        )
        self.console = get_console()

    def discover(self, *, require_encoder: bool = True) -> tuple[list[RemoteFile], list[VideoRecord]]:
        device = self.adb.get_device()
        self.adb.serial = device.serial
        logger.info("Using device %s (%s)", device.serial, device.state)

        if require_encoder:
            ensure_encoder_available(self.tools["ffmpeg"], self.cfg.video_encoder)

        remote_files = self.adb.list_videos(
            self.cfg.remote_root,
            self.cfg.extensions,
            self.cfg.skip_prefixes,
            self.cfg.archive_root,
        )
        logger.info("Discovered %d remote video(s)", len(remote_files))

        reset = self.db.reconcile_on_start()
        if reset:
            logger.info("Reconciled %d interrupted job(s) for resume", reset)

        for rf in remote_files:
            self.db.upsert_discovered(rf.path, rf.size, rf.mtime)

        pending = self.db.pending_work(self.cfg.max_attempts)
        # Only process files that still exist on device among pending
        remote_set = {rf.path for rf in remote_files}
        pending = [p for p in pending if p.remote_path in remote_set]
        # Skip already done (pending_work shouldn't include done)
        pending = [p for p in pending if p.status != VideoStatus.DONE]
        return remote_files, pending

    def print_summary(
        self,
        remote_files: list[RemoteFile],
        pending: list[VideoRecord],
        *,
        title: str = "Dry-run summary",
    ) -> None:
        total_bytes = sum(p.size for p in pending)
        # Rough estimate: ~40% of original for HEVC
        est_out = int(total_bytes * 0.4)
        table = Table(title=title)
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Videos on device", str(len(remote_files)))
        table.add_row("Pending / to process", str(len(pending)))
        table.add_row("Pending bytes", _format_bytes(total_bytes))
        table.add_row("Est. output (≈40%)", _format_bytes(est_out))
        table.add_row("Est. savings", _format_bytes(max(0, total_bytes - est_out)))
        table.add_row("Encoder", self.cfg.video_encoder)
        table.add_row("Workers", str(self.cfg.encode_workers))
        table.add_row("Delete mode", self.cfg.delete_mode)
        self.console.print(table)

        if pending:
            preview = Table(title="Pending files (first 20)")
            preview.add_column("Remote path")
            preview.add_column("Size", justify="right")
            preview.add_column("Status")
            for item in pending[:20]:
                preview.add_row(item.remote_path, _format_bytes(item.size), item.status.value)
            if len(pending) > 20:
                preview.add_row(f"... and {len(pending) - 20} more", "", "")
            self.console.print(preview)

    def process_one(self, record: VideoRecord) -> None:
        """Pull → encode → verify → push → archive/delete. Raises on failure."""
        remote = record.remote_path
        local_name = _safe_local_name(remote)
        local_in = self.cfg.work_in / local_name
        local_out = self.cfg.work_out / f"{Path(local_name).stem}{self.cfg.output_suffix}.mp4"
        remote_out = _output_remote_path(remote, self.cfg.output_suffix)

        try:
            self.db.set_status(remote, VideoStatus.PULLING, local_path=str(local_in))
            logger.info("Pulling %s", remote)
            self.adb.pull(remote, local_in, timeout=self.cfg.subprocess_timeout_sec)

            self.db.set_status(remote, VideoStatus.PULLED)
            src_info = get_media_info(
                local_in,
                self.tools["ffprobe"],
                timeout=min(self.cfg.subprocess_timeout_sec, 120),
            )
            self.db.set_status(
                remote,
                VideoStatus.ENCODING,
                src_duration=src_info.duration_sec,
                src_width=src_info.width,
                src_height=src_info.height,
                output_path=str(local_out),
            )

            logger.info("Encoding %s -> %s", local_in.name, local_out.name)
            encode(local_in, local_out, self.cfg, self.tools["ffmpeg"])

            self.db.set_status(remote, VideoStatus.VERIFYING)
            out_info = get_media_info(
                local_out,
                self.tools["ffprobe"],
                timeout=min(self.cfg.subprocess_timeout_sec, 120),
            )
            result = check(src_info, out_info, self.cfg, out_path=local_out)
            if not result.ok:
                raise RuntimeError("; ".join(result.reasons))

            self.db.set_status(
                remote,
                VideoStatus.PUSHING,
                out_duration=out_info.duration_sec,
                out_width=out_info.width,
                out_height=out_info.height,
                out_size=out_info.size_bytes,
            )
            logger.info("Pushing %s", remote_out)
            self.adb.push(local_out, remote_out, timeout=self.cfg.subprocess_timeout_sec)
            self.adb.set_remote_mtime(remote_out, record.mtime)

            self.db.set_status(remote, VideoStatus.FINALIZING)
            if self.cfg.delete_mode == "archive":
                archived = self.adb.move_to_archive(
                    remote, self.cfg.archive_root, self.cfg.remote_root
                )
                logger.info("Archived original -> %s", archived)
            else:
                self.adb.delete_remote(remote)
                logger.info("Deleted original %s", remote)

            saved = max(0, record.size - out_info.size_bytes)
            self.db.record_result(
                remote,
                out_size=out_info.size_bytes,
                saved_bytes=saved,
                remote_output_path=remote_out,
                out_duration=out_info.duration_sec,
                out_width=out_info.width,
                out_height=out_info.height,
            )

            # Cleanup local work files on success
            for path in (local_in, local_out):
                if path.exists():
                    path.unlink()

        except Exception as exc:
            self._handle_failure(remote, local_out, exc)
            raise

    def _handle_failure(self, remote: str, local_out: Path, exc: Exception) -> None:
        logger.error("Failed %s: %s", remote, exc)
        self.db.mark_failed(remote, str(exc))
        if local_out.exists():
            dest = self.cfg.work_failed / local_out.name
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(local_out), str(dest))
            except OSError as move_exc:
                logger.warning("Could not move failed output: %s", move_exc)

    def run(
        self,
        *,
        dry_run: bool = False,
        yes: bool = False,
        limit: int | None = None,
    ) -> RunReport:
        report = RunReport()
        try:
            remote_files, pending = self.discover(require_encoder=not dry_run)
        except DeviceError as exc:
            self.console.print(f"[bold red]Device error:[/bold red] {exc}")
            report.errors.append(str(exc))
            return report

        if limit is not None:
            pending = pending[:limit]

        # Count already-done for skipped
        counts = self.db.count_by_status()
        report.skipped = counts.get(VideoStatus.DONE.value, 0)

        self.print_summary(
            remote_files,
            pending,
            title="Scan summary" if dry_run else "Process plan",
        )

        if dry_run:
            self.console.print("[cyan]Scan only — no pull/push/delete performed.[/cyan]")
            return report

        if not pending:
            self.console.print("[green]Nothing to process.[/green]")
            return report

        if not yes:
            confirmed = self.console.input(
                f"Process {len(pending)} video(s)? [y/N] "
            ).strip().lower()
            if confirmed not in {"y", "yes"}:
                self.console.print("[yellow]Aborted.[/yellow]")
                return report

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self.console,
        ) as progress:
            task_id = progress.add_task("Processing", total=len(pending))

            def _worker(rec: VideoRecord) -> tuple[VideoRecord, Exception | None]:
                attempts_left = self.cfg.max_attempts - rec.attempts
                last_exc: Exception | None = None
                for attempt in range(max(1, attempts_left)):
                    try:
                        # Refresh record attempts from DB
                        current = self.db.get(rec.remote_path) or rec
                        if current.status == VideoStatus.DONE:
                            return current, None
                        if (
                            current.status == VideoStatus.FAILED
                            and current.attempts >= self.cfg.max_attempts
                        ):
                            return current, RuntimeError("max attempts exceeded")
                        self.process_one(current)
                        return current, None
                    except Exception as exc:  # noqa: BLE001 — per-item isolation
                        last_exc = exc
                        backoff = self.cfg.retry_backoff_base_sec * (2**attempt)
                        logger.warning(
                            "Retry %s in %.1fs (%s)",
                            rec.remote_path,
                            backoff,
                            exc,
                        )
                        time.sleep(backoff)
                return rec, last_exc

            with ThreadPoolExecutor(max_workers=self.cfg.encode_workers) as pool:
                futures = {pool.submit(_worker, rec): rec for rec in pending}
                for fut in as_completed(futures):
                    rec, err = fut.result()
                    if err is None:
                        done_rec = self.db.get(rec.remote_path)
                        if done_rec and done_rec.status == VideoStatus.DONE:
                            report.done += 1
                            report.saved_bytes += done_rec.saved_bytes or 0
                        else:
                            report.failed += 1
                    else:
                        report.failed += 1
                        report.errors.append(f"{rec.remote_path}: {err}")
                    progress.advance(task_id)

        self._print_report(report)
        return report

    def scan(self) -> RunReport:
        """Discover videos and update SQLite without transferring."""
        return self.run(dry_run=True, yes=True)

    def process(self, *, yes: bool = False, limit: int | None = None) -> RunReport:
        """Pull, encode, verify, push, and archive/delete originals."""
        return self.run(dry_run=False, yes=yes, limit=limit)

    def watch(
        self,
        *,
        interval: float | None = None,
        once: bool = False,
        yes: bool = True,
        limit: int | None = None,
    ) -> None:
        """Poll for an authorized device, then process pending videos."""
        poll = interval if interval is not None else self.cfg.watch_interval_sec
        if poll < 1:
            poll = 1.0
        self.console.print(
            f"[cyan]Watching for ADB device every {poll:.0f}s "
            f"({'once' if once else 'continuous'}). Ctrl+C to stop.[/cyan]"
        )
        last_serial: str | None = None
        while True:
            try:
                device = self.adb.get_device()
            except DeviceError as exc:
                if last_serial is not None:
                    self.console.print("[yellow]Device disconnected.[/yellow]")
                    last_serial = None
                logger.debug("Waiting for device: %s", exc)
                time.sleep(poll)
                continue

            if device.serial == last_serial and once:
                self.console.print("[dim]Already processed this connect; waiting…[/dim]")
                time.sleep(poll)
                continue

            if device.serial != last_serial:
                self.console.print(
                    f"[green]Device connected:[/green] {device.serial}"
                )
                last_serial = device.serial
                report = self.process(yes=yes, limit=limit)
                if report.errors and report.done == 0 and report.failed:
                    self.console.print("[red]Processing finished with errors.[/red]")
                if once:
                    self.console.print("[cyan]Watch --once complete.[/cyan]")
                    return

            time.sleep(poll)

    def verify(self, *, limit: int | None = None) -> dict[str, int]:
        """Verify done items still have compressed outputs on the phone."""
        device = self.adb.get_device()
        self.adb.serial = device.serial

        done = self.db.done_records()
        if limit is not None:
            done = done[:limit]

        ok = 0
        missing_output = 0
        missing_path = 0
        checked = 0

        table = Table(title="Verify results")
        table.add_column("Remote original")
        table.add_column("Compressed")
        table.add_column("Result")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self.console,
        ) as progress:
            task_id = progress.add_task("Verifying", total=len(done))
            for rec in done:
                checked += 1
                out_path = rec.remote_output_path
                if not out_path:
                    missing_path += 1
                    table.add_row(rec.remote_path, "—", "[yellow]no output path in DB[/yellow]")
                    progress.advance(task_id)
                    continue
                if self.adb.remote_exists(out_path):
                    ok += 1
                    if checked <= 30:
                        table.add_row(rec.remote_path, out_path, "[green]ok[/green]")
                else:
                    missing_output += 1
                    table.add_row(rec.remote_path, out_path, "[red]missing[/red]")
                progress.advance(task_id)

        if done:
            self.console.print(table)
        else:
            self.console.print("[yellow]No done videos in the database to verify.[/yellow]")

        summary = Table(title="Verify summary")
        summary.add_column("Metric")
        summary.add_column("Count", justify="right")
        summary.add_row("Checked", str(checked))
        summary.add_row("OK", str(ok))
        summary.add_row("Missing compressed file", str(missing_output))
        summary.add_row("Missing DB output path", str(missing_path))
        self.console.print(summary)
        return {
            "checked": checked,
            "ok": ok,
            "missing_output": missing_output,
            "missing_path": missing_path,
        }

    def clean(
        self,
        *,
        work: bool = True,
        archive: bool = False,
        yes: bool = False,
    ) -> None:
        """Remove local work files and optionally purge the on-phone archive."""
        if work:
            cleared = 0
            for folder in (self.cfg.work_in, self.cfg.work_out, self.cfg.work_failed):
                if not folder.exists():
                    continue
                for path in folder.iterdir():
                    if path.is_file():
                        path.unlink()
                        cleared += 1
                    elif path.is_dir():
                        shutil.rmtree(path)
                        cleared += 1
            self.console.print(f"[green]Cleared {cleared} item(s) from work dirs.[/green]")

        if archive:
            self.purge_archive(yes=yes)
        elif not work:
            self.console.print("[yellow]Nothing to clean (pass --work and/or --archive).[/yellow]")

    def stats(self) -> None:
        """Print detailed tracking statistics."""
        counts = self.db.count_by_status()
        sizes = self.db.sum_size_by_status()
        original, output, saved = self.db.compression_totals()
        pending = self.db.pending_work(self.cfg.max_attempts)
        pending_bytes = sum(p.size for p in pending)
        total = sum(counts.values())

        table = Table(title="phone-sync stats")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Tracked videos", str(total))
        for status in VideoStatus:
            n = counts.get(status.value, 0)
            if n:
                table.add_row(
                    f"  {status.value}",
                    f"{n} ({_format_bytes(sizes.get(status.value, 0))})",
                )
        table.add_row("Pending now", f"{len(pending)} ({_format_bytes(pending_bytes)})")
        table.add_row("Original bytes (done)", _format_bytes(original))
        table.add_row("Compressed bytes (done)", _format_bytes(output))
        table.add_row("Bytes saved (all time)", _format_bytes(saved))
        if original > 0:
            ratio = (1 - output / original) * 100
            table.add_row("Avg compression", f"{ratio:.1f}% smaller")
        table.add_row("Encoder", self.cfg.video_encoder)
        table.add_row("Delete mode", self.cfg.delete_mode)
        self.console.print(table)

        failures = self.db.failed_records()[:10]
        if failures:
            fail_table = Table(title="Recent failures (up to 10)")
            fail_table.add_column("Path")
            fail_table.add_column("Attempts", justify="right")
            fail_table.add_column("Error")
            for rec in failures:
                fail_table.add_row(
                    rec.remote_path,
                    str(rec.attempts),
                    (rec.last_error or "")[:80],
                )
            self.console.print(fail_table)

    def _print_report(self, report: RunReport) -> None:
        table = Table(title="Run report")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Done", str(report.done))
        table.add_row("Failed", str(report.failed))
        table.add_row("Previously done (skipped)", str(report.skipped))
        table.add_row("Bytes saved (this run)", _format_bytes(report.saved_bytes))
        table.add_row("Bytes saved (all time)", _format_bytes(self.db.total_saved_bytes()))
        self.console.print(table)

    def status_table(self) -> None:
        """Compact status summary (alias of a lighter stats view)."""
        counts = self.db.count_by_status()
        table = Table(title="Video status")
        table.add_column("Status")
        table.add_column("Count", justify="right")
        for status in VideoStatus:
            table.add_row(status.value, str(counts.get(status.value, 0)))
        table.add_row("Total saved", _format_bytes(self.db.total_saved_bytes()))
        self.console.print(table)

    def purge_archive(self, *, yes: bool = False) -> None:
        device = self.adb.get_device()
        self.adb.serial = device.serial
        if not yes:
            confirmed = self.console.input(
                f"Permanently delete on-phone archive {self.cfg.archive_root}? [y/N] "
            ).strip().lower()
            if confirmed not in {"y", "yes"}:
                self.console.print("[yellow]Aborted.[/yellow]")
                return
        self.adb.purge_archive(self.cfg.archive_root)
        self.console.print(f"[green]Purged {self.cfg.archive_root}[/green]")
