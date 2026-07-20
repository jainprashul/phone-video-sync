"""Parallel ffprobe header probing for scan recommendation scoring."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from rich.console import Console

from phone_video_sync.adb import AdbClient, RemoteFile
from phone_video_sync.db import Database
from phone_video_sync.ffmpeg import probe_remote_header
from phone_video_sync.models import VideoRecord
from phone_video_sync.report import ScanBreakdown, apply_probe_to_meta, probe_candidates

logger = logging.getLogger(__name__)

StatusFn = Callable[[str], None]


@dataclass(frozen=True)
class ProbeStats:
    """Summary counts from a codec-probe pass."""

    cache_hits: int
    probed_ok: int
    probed_fail: int
    total_candidates: int


def _probe_info_from_cache(meta: dict) -> SimpleNamespace:
    """Build a probe-like object from a SQLite media_meta row."""
    return SimpleNamespace(
        width=meta.get("width"),
        height=meta.get("height"),
        duration_sec=meta.get("duration_sec"),
        video_codec=meta.get("video_codec"),
        audio_codec=meta.get("audio_codec"),
        bitrate=meta.get("bitrate"),
        fps=meta.get("fps"),
        pix_fmt=meta.get("pix_fmt"),
        profile=meta.get("profile"),
        level=meta.get("level"),
    )


def _persist_probe_result(db: Database, rec: VideoRecord, info: object) -> None:
    """Write ffprobe header results into media_meta for future cache hits."""
    db.upsert_media_meta(
        rec.remote_path,
        rec.size,
        rec.mtime,
        width=getattr(info, "width", None) or None,
        height=getattr(info, "height", None) or None,
        duration_sec=getattr(info, "duration_sec", None) or None,
        video_codec=getattr(info, "video_codec", None),
        audio_codec=getattr(info, "audio_codec", None),
        bitrate=getattr(info, "bitrate", None),
        fps=getattr(info, "fps", None),
        pix_fmt=getattr(info, "pix_fmt", None),
        profile=getattr(info, "profile", None),
        level=getattr(info, "level", None),
        resolution=(
            f"{info.width}x{info.height}"
            if getattr(info, "width", None) and getattr(info, "height", None)
            else None
        ),
        source="probe",
    )


def probe_breakdown_codecs(
    breakdown: ScanBreakdown,
    *,
    adb: AdbClient,
    db: Database,
    ffprobe_path: str,
    work_dir: Path,
    output_suffix: str,
    remote_by_path: dict[str, RemoteFile],
    on_status: StatusFn,
    console: Console,
    max_workers: int = 2,
) -> ProbeStats | None:
    """
    Fill codec fields on breakdown.metas via SQLite cache + parallel ffprobe.

    Returns None when no candidates need probing; otherwise ProbeStats.
    """
    candidates = probe_candidates(breakdown)
    if not candidates:
        return None

    cached_meta = db.get_media_meta_many(
        [(r.remote_path, r.size, r.mtime) for r in candidates]
    )
    to_probe: list[VideoRecord] = []
    cache_hits = 0

    for rec in candidates:
        meta = cached_meta.get(rec.remote_path)
        if meta and meta.get("video_codec"):
            cache_hits += 1
            apply_probe_to_meta(
                breakdown,
                rec.remote_path,
                _probe_info_from_cache(meta),
                output_suffix=output_suffix,
                remote=remote_by_path.get(rec.remote_path),
            )
        else:
            to_probe.append(rec)

    if cache_hits:
        console.print(
            f"[dim]Codec cache: {cache_hits}/{len(candidates)} "
            f"from SQLite (unchanged size+mtime).[/dim]"
        )

    probe_ok = probe_fail = 0
    if to_probe:
        total = len(to_probe)
        on_status(
            f"[cyan]Probing codecs for {total} candidates "
            f"(for recommendation scoring)…[/cyan]"
        )

        def _probe_one(rec: VideoRecord) -> tuple[str, object | None, str | None]:
            try:
                info = probe_remote_header(
                    adb_client=adb,
                    remote_path=rec.remote_path,
                    ffprobe_path=ffprobe_path,
                    header_mb=2,
                    tail_mb=12,
                    work_dir=work_dir,
                )
                return rec.remote_path, info, None
            except Exception as exc:  # noqa: BLE001 — per-file isolation
                return rec.remote_path, None, str(exc)

        workers = min(max_workers, max(1, total))
        done_count = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_probe_one, rec): rec for rec in to_probe}
            for fut in as_completed(futures):
                rec = futures[fut]
                path, info, err = fut.result()
                done_count += 1
                on_status(f"[cyan]Probed {done_count}/{total}: {Path(path).name}[/cyan]")
                if info is not None:
                    probe_ok += 1
                    apply_probe_to_meta(
                        breakdown,
                        path,
                        info,
                        output_suffix=output_suffix,
                        remote=remote_by_path.get(path),
                    )
                    _persist_probe_result(db, rec, info)
                else:
                    probe_fail += 1
                    logger.debug("Probe failed for %s: %s", path, err)

        if probe_fail:
            console.print(
                f"[yellow]Codec probe:[/yellow] {probe_ok} ok, "
                f"{probe_fail} failed (scoring uses MediaStore when present)."
            )
        else:
            console.print(
                f"[green]Codec probe:[/green] {probe_ok}/{total} ok (saved to cache)."
            )

    return ProbeStats(
        cache_hits=cache_hits,
        probed_ok=probe_ok,
        probed_fail=probe_fail,
        total_candidates=len(candidates),
    )
