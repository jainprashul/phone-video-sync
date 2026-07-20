"""Pipeline orchestrator: discover, diff, confirm, parallel stages, report."""

from __future__ import annotations

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.status import Status
from rich.table import Table

from phone_video_sync.adb import AdbClient, DeviceError, RemoteFile
from phone_video_sync.config import Config, ensure_runtime_dirs, resolve_tools
from phone_video_sync.db import Database
from phone_video_sync.ffmpeg import encode, ensure_encoder_available, get_media_info
from phone_video_sync.logging_setup import get_console
from phone_video_sync.models import RunReport, VideoRecord, VideoStatus
from phone_video_sync.pipeline.cache import (
    apply_media_meta_cache,
    listing_cache_key,
    persist_media_meta_from_remote,
    remote_files_from_cache,
    remote_files_to_cache_payload,
)
from phone_video_sync.pipeline.display import render_scan_report
from phone_video_sync.pipeline.paths import output_remote_path, safe_local_name
from phone_video_sync.pipeline.probe import probe_breakdown_codecs
from phone_video_sync.report import (
    ScanBreakdown,
    apply_remote_map,
    attach_failed_records,
    build_scan_breakdown,
    filter_pending,
    format_bytes,
    parse_size,
    refresh_recommendations,
)
from phone_video_sync.report.export import save_scan_report
from phone_video_sync.select_ui import interactive_select as ui_select
from phone_video_sync.verify import check

logger = logging.getLogger(__name__)


class Pipeline:
    """
    End-to-end phone video sync workflow.

    Stages: discover on device → build scan report → optional selection →
    parallel pull/encode/verify/push → archive or delete originals.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        ensure_runtime_dirs(cfg)
        self.tools = resolve_tools(cfg)
        self.db = Database(cfg.db_path)
        self.console = get_console()
        self._status: Status | None = None
        self.adb = AdbClient(
            self.tools["adb"],
            timeout=min(cfg.subprocess_timeout_sec, 300),
            on_progress=self._on_adb_progress,
        )

    def _on_adb_progress(self, message: str) -> None:
        """Update live Rich status so long ADB calls don't look frozen."""
        if self._status is not None:
            self._status.update(f"[cyan]{message}[/cyan]")
        else:
            self.console.print(f"[dim]{message}[/dim]")

    def _update_status(self, message: str) -> None:
        """Unified status callback for sub-modules during discover/report."""
        if self._status is not None:
            self._status.update(message)

    def discover(
        self,
        *,
        require_encoder: bool = True,
        refresh: bool = False,
    ) -> tuple[list[RemoteFile], list[VideoRecord]]:
        """
        Connect to the phone, list videos, reconcile DB, return pending work.

        Uses SQLite listing cache when ``refresh`` is false and TTL allows.
        """
        used_cache = False

        with Status(
            "[cyan]Connecting to device…[/cyan]",
            console=self.console,
            spinner="dots",
        ) as status:
            self._status = status
            try:
                device = self.adb.get_device()
                self.adb.serial = device.serial
                status.update(
                    f"[cyan]Using device {device.serial} ({device.state})…[/cyan]"
                )
                logger.info("Using device %s (%s)", device.serial, device.state)

                if require_encoder:
                    status.update("[cyan]Checking encoder…[/cyan]")
                    ensure_encoder_available(self.tools["ffmpeg"], self.cfg.video_encoder)

                cache_key = f"{device.serial}|{listing_cache_key(self.cfg.remote_root, self.cfg.extensions)}"
                remote_files: list[RemoteFile] | None = None

                if not refresh and self.cfg.listing_cache_ttl_sec > 0:
                    cached = self.db.get_listing_cache(
                        cache_key, max_age_sec=self.cfg.listing_cache_ttl_sec
                    )
                    if cached is not None:
                        payload, scanned_at = cached
                        age_min = (
                            datetime.now(timezone.utc) - scanned_at
                        ).total_seconds() / 60.0
                        status.update(
                            f"[cyan]Loading cached listing "
                            f"({len(payload)} files, {age_min:.0f}m old)…[/cyan]"
                        )
                        remote_files = remote_files_from_cache(payload)
                        used_cache = True
                        self.console.print(
                            f"[dim]Using cached search from {scanned_at.astimezone():%H:%M:%S} "
                            f"({age_min:.0f} min ago). Pass --refresh to rescan the phone.[/dim]"
                        )

                if remote_files is None:
                    remote_files = self.adb.list_videos(
                        self.cfg.remote_root,
                        self.cfg.extensions,
                        self.cfg.skip_prefixes,
                        self.cfg.archive_root,
                    )
                    persist_media_meta_from_remote(self.db, remote_files)
                    self.db.save_listing_cache(
                        cache_key,
                        device_serial=device.serial,
                        files=remote_files_to_cache_payload(remote_files),
                    )

                # Merge probe/MediaStore rows (covers fresh scans and cache hits).
                remote_files = apply_media_meta_cache(self.db, remote_files)

                logger.info(
                    "Discovered %d remote video(s)%s",
                    len(remote_files),
                    " (cached)" if used_cache else "",
                )

                status.update(
                    f"[cyan]Updating database ({len(remote_files)} files)…[/cyan]"
                )
                reset = self.db.reconcile_on_start()
                if reset:
                    logger.info("Reconciled %d interrupted job(s) for resume", reset)

                def _db_progress(done: int, total: int) -> None:
                    status.update(f"[cyan]Updating database… {done}/{total}[/cyan]")

                self.db.upsert_discovered_batch(
                    [(rf.path, rf.size, rf.mtime) for rf in remote_files],
                    on_progress=_db_progress,
                )

                status.update("[cyan]Computing pending work…[/cyan]")
                remote_set = {rf.path for rf in remote_files}
                pending = [
                    p
                    for p in self.db.pending_work(self.cfg.max_attempts)
                    if p.remote_path in remote_set and p.status != VideoStatus.DONE
                ]
            finally:
                self._status = None

        self.console.print(
            f"[green]Ready:[/green] {len(remote_files)} on device, "
            f"{len(pending)} pending"
            + (" [cached listing]" if used_cache else "")
            + "."
        )
        return remote_files, pending

    def print_summary(
        self,
        remote_files: list[RemoteFile],
        pending: list[VideoRecord],
        *,
        title: str = "Dry-run summary",
    ) -> ScanBreakdown:
        """Build breakdown, probe codecs for scoring, render Rich tables, save report."""
        with Status(
            f"[cyan]Building report for {len(pending)} pending video(s)…[/cyan]",
            console=self.console,
            spinner="dots",
        ) as status:
            self._status = status
            try:
                status.update("[cyan]Grouping by size and folder…[/cyan]")
                breakdown = build_scan_breakdown(
                    pending, output_suffix=self.cfg.output_suffix
                )
                remote_by_path = {rf.path: rf for rf in remote_files}
                apply_remote_map(
                    breakdown,
                    remote_by_path,
                    output_suffix=self.cfg.output_suffix,
                )

                probe_breakdown_codecs(
                    breakdown,
                    adb=self.adb,
                    db=self.db,
                    ffprobe_path=self.tools["ffprobe"],
                    work_dir=self.cfg.work_dir / "probe",
                    output_suffix=self.cfg.output_suffix,
                    remote_by_path=remote_by_path,
                    on_status=self._update_status,
                    console=self.console,
                )

                status.update(
                    "[cyan]Scoring recommendations (codec/resolution/size)…[/cyan]"
                )
                refresh_recommendations(breakdown, output_suffix=self.cfg.output_suffix)
                remote_set = {rf.path for rf in remote_files}
                failed_on_device = [
                    rec
                    for rec in self.db.failed_records()
                    if rec.remote_path in remote_set
                ]
                attach_failed_records(
                    breakdown,
                    failed_on_device,
                    remote_by_path,
                    output_suffix=self.cfg.output_suffix,
                )
                status.update("[cyan]Rendering tables…[/cyan]")
            finally:
                self._status = None

        render_scan_report(
            self.console,
            title=title,
            remote_files=remote_files,
            pending=pending,
            breakdown=breakdown,
            output_suffix=self.cfg.output_suffix,
            encoder=self.cfg.video_encoder,
            encode_workers=self.cfg.encode_workers,
            delete_mode=self.cfg.delete_mode,
            max_attempts=self.cfg.max_attempts,
        )

        report_path = save_scan_report(
            self.cfg.log_dir,
            title=title,
            remote_files=remote_files,
            pending=pending,
            breakdown=breakdown,
            output_suffix=self.cfg.output_suffix,
            encoder=self.cfg.video_encoder,
            delete_mode=self.cfg.delete_mode,
            max_attempts=self.cfg.max_attempts,
        )
        self.console.print(f"[green]Report saved:[/green] {report_path}")
        if breakdown.recommended:
            csv_sibling = report_path.with_name(report_path.stem + "-recommended.csv")
            if csv_sibling.exists():
                self.console.print(f"[green]CSV saved:[/green] {csv_sibling}")
        if breakdown.failed:
            failed_csv = report_path.with_name(report_path.stem + "-failed.csv")
            if failed_csv.exists():
                self.console.print(f"[yellow]Failed CSV saved:[/yellow] {failed_csv}")

        return breakdown

    def interactive_select(self, breakdown: ScanBreakdown) -> list[VideoRecord] | None:
        """Radio + checkbox selection via questionary."""
        if not breakdown.pending:
            return []
        try:
            return ui_select(breakdown)
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"[red]Interactive select failed:[/red] {exc}")
            return None

    def process_one(self, record: VideoRecord) -> None:
        """Pull → encode → verify → push → archive/delete. Raises on failure."""
        remote = record.remote_path
        local_name = safe_local_name(remote)
        local_in = self.cfg.work_in / local_name
        local_out = self.cfg.work_out / f"{Path(local_name).stem}{self.cfg.output_suffix}.mp4"
        remote_out = output_remote_path(remote, self.cfg.output_suffix)

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
            encode(
                local_in,
                local_out,
                self.cfg,
                self.tools["ffmpeg"],
                src_info=src_info,
            )

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
        select: bool = False,
        folders: list[str] | None = None,
        min_size: str | None = None,
        max_size: str | None = None,
        recommend: bool = False,
        refresh: bool = False,
    ) -> RunReport:
        """Shared entry for scan (dry_run) and process commands."""
        report = RunReport()
        try:
            remote_files, pending = self.discover(
                require_encoder=not dry_run, refresh=refresh
            )
        except DeviceError as exc:
            self.console.print(f"[bold red]Device error:[/bold red] {exc}")
            report.errors.append(str(exc))
            return report

        min_bytes = parse_size(min_size) if min_size else None
        max_bytes = parse_size(max_size) if max_size else None
        # Folder/size filters first; --recommend applied after codec-aware scoring
        pending = filter_pending(
            pending,
            folders=folders,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
        )

        report.skipped = self.db.count_by_status().get(VideoStatus.DONE.value, 0)

        breakdown = self.print_summary(
            remote_files,
            pending,
            title="Scan report" if dry_run else "Process plan",
        )

        if recommend:
            pending = list(breakdown.recommended)
            self.console.print(
                f"[cyan]Using recommended set:[/cyan] {len(pending)} file(s) "
                f"({format_bytes(sum(p.size for p in pending))}) — "
                f"{breakdown.recommend_reason}"
            )

        if limit is not None:
            pending = pending[:limit]

        if dry_run and not select:
            self.console.print(
                "[cyan]Scan only — no pull/push/delete. "
                "Use [bold]phone-sync scan --select[/bold] to choose a subset, "
                "or [bold]phone-sync process --recommend[/bold].[/cyan]"
            )
            return report

        if select:
            chosen = self.interactive_select(breakdown)
            if chosen is None:
                self.console.print("[yellow]No selection — aborted.[/yellow]")
                return report
            pending = chosen
            if not pending:
                self.console.print("[green]Nothing selected.[/green]")
                return report
            self.console.print(
                f"[cyan]Selected {len(pending)} file(s) "
                f"({format_bytes(sum(p.size for p in pending))}).[/cyan]"
            )
            if dry_run:
                # scan --select: ask whether to process now
                go = self.console.input("Process selected files now? [y/N] ").strip().lower()
                if go not in {"y", "yes"}:
                    self.console.print(
                        "[cyan]Selection noted in report only — run process with filters later.[/cyan]"
                    )
                    return report
                # fall through to process selected
                yes = True

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

        self._process_batch(pending, report)
        self._print_report(report)
        return report

    def _process_batch(self, pending: list[VideoRecord], report: RunReport) -> None:
        """Encode pending videos with retries and a Rich progress bar."""
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

    def scan(self, *, select: bool = False, refresh: bool = False) -> RunReport:
        """Discover videos and update SQLite without transferring (unless --select processes)."""
        return self.run(dry_run=True, yes=True, select=select, refresh=refresh)

    def process(
        self,
        *,
        yes: bool = False,
        limit: int | None = None,
        select: bool = False,
        folders: list[str] | None = None,
        min_size: str | None = None,
        max_size: str | None = None,
        recommend: bool = False,
        refresh: bool = False,
    ) -> RunReport:
        """Pull, encode, verify, push, and archive/delete originals."""
        return self.run(
            dry_run=False,
            yes=yes,
            limit=limit,
            select=select,
            folders=folders,
            min_size=min_size,
            max_size=max_size,
            recommend=recommend,
            refresh=refresh,
        )

    def watch(
        self,
        *,
        interval: float | None = None,
        once: bool = False,
        yes: bool = True,
        limit: int | None = None,
    ) -> None:
        """Poll for an authorized device, then process pending videos."""
        poll = max(1.0, interval if interval is not None else self.cfg.watch_interval_sec)
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
                self.console.print(f"[green]Device connected:[/green] {device.serial}")
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

        ok = missing_output = missing_path = checked = 0
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
                    f"{n} ({format_bytes(sizes.get(status.value, 0))})",
                )
        table.add_row("Pending now", f"{len(pending)} ({format_bytes(pending_bytes)})")
        table.add_row("Original bytes (done)", format_bytes(original))
        table.add_row("Compressed bytes (done)", format_bytes(output))
        table.add_row("Bytes saved (all time)", format_bytes(saved))
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
        table.add_row("Bytes saved (this run)", format_bytes(report.saved_bytes))
        table.add_row("Bytes saved (all time)", format_bytes(self.db.total_saved_bytes()))
        self.console.print(table)

    def status_table(self) -> None:
        """Compact status summary (alias of a lighter stats view)."""
        counts = self.db.count_by_status()
        table = Table(title="Video status")
        table.add_column("Status")
        table.add_column("Count", justify="right")
        for status in VideoStatus:
            table.add_row(status.value, str(counts.get(status.value, 0)))
        table.add_row("Total saved", format_bytes(self.db.total_saved_bytes()))
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
