"""Tests for scan report export."""

from __future__ import annotations

from pathlib import Path

from phone_video_sync.adb import RemoteFile
from phone_video_sync.models import VideoRecord, VideoStatus
from phone_video_sync.report import attach_failed_records, build_scan_breakdown
from phone_video_sync.report_export import save_scan_report


def test_save_scan_report_writes_md_and_csv(tmp_path: Path) -> None:
    pending = [
        VideoRecord(
            remote_path="/sdcard/DCIM/Camera/big.mp4",
            size=400 * 1024 * 1024,
            mtime=1_700_000_000,
        ),
        VideoRecord(
            remote_path="/sdcard/Download/huge.mp4",
            size=3 * 1024 * 1024 * 1024,
            mtime=1_700_000_100,
        ),
    ]
    remote = [
        RemoteFile(path=p.remote_path, size=p.size, mtime=p.mtime) for p in pending
    ]
    breakdown = build_scan_breakdown(pending, output_suffix="_hevc")
    path = save_scan_report(
        tmp_path,
        title="Scan report",
        remote_files=remote,
        pending=pending,
        breakdown=breakdown,
        output_suffix="_hevc",
        encoder="hevc_nvenc",
        delete_mode="archive",
    )
    assert path.exists()
    assert path.suffix == ".md"
    text = path.read_text(encoding="utf-8")
    assert "Scan report" in text
    assert "All recommended" in text or "Recommendation" in text
    csv_path = path.with_name(path.stem + "-recommended.csv")
    assert csv_path.exists()
    assert "remote_path" in csv_path.read_text(encoding="utf-8")


def test_save_scan_report_includes_failed_section(tmp_path: Path) -> None:
    pending = [
        VideoRecord(
            remote_path="/sdcard/DCIM/Camera/ok.mp4",
            size=400 * 1024 * 1024,
            mtime=1_700_000_000,
        ),
    ]
    failed = [
        VideoRecord(
            remote_path="/sdcard/DCIM/Camera/broken.mp4",
            size=1_400 * 1024 * 1024,
            mtime=1_700_000_050,
            status=VideoStatus.FAILED,
            attempts=3,
            last_error="resolution mismatch: src=1920x1080 out=1080x1920",
        ),
    ]
    remote = [
        RemoteFile(path=p.remote_path, size=p.size, mtime=p.mtime) for p in pending
    ] + [
        RemoteFile(path=f.remote_path, size=f.size, mtime=f.mtime) for f in failed
    ]
    breakdown = build_scan_breakdown(pending, output_suffix="_hevc")
    attach_failed_records(
        breakdown,
        failed,
        {rf.path: rf for rf in remote},
        output_suffix="_hevc",
    )
    path = save_scan_report(
        tmp_path,
        title="Scan report",
        remote_files=remote,
        pending=pending,
        breakdown=breakdown,
        output_suffix="_hevc",
        encoder="hevc_nvenc",
        delete_mode="archive",
        max_attempts=3,
    )
    text = path.read_text(encoding="utf-8")
    assert "Failed / not processed" in text
    assert "broken.mp4" in text
    assert "resolution mismatch" in text
    failed_csv = path.with_name(path.stem + "-failed.csv")
    assert failed_csv.exists()
    assert "broken.mp4" in failed_csv.read_text(encoding="utf-8")
