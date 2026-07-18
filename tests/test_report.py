"""Unit tests for scan report grouping and recommendations."""

from __future__ import annotations

import pytest

from phone_video_sync.models import VideoRecord
from phone_video_sync.report import (
    build_scan_breakdown,
    filter_pending,
    folder_of,
    parse_size,
    recommend,
    size_bucket_of,
)


def _rec(path: str, size: int) -> VideoRecord:
    return VideoRecord(remote_path=path, size=size, mtime=1)


def test_folder_of() -> None:
    assert folder_of("/storage/emulated/0/DCIM/Camera/a.mp4") == (
        "/storage/emulated/0/DCIM/Camera"
    )


def test_size_buckets() -> None:
    assert "tiny" in size_bucket_of(5 * 1024 * 1024)
    assert "medium" in size_bucket_of(200 * 1024 * 1024)
    assert "huge" in size_bucket_of(3 * 1024 * 1024 * 1024)


def test_recommend_prefers_medium_plus() -> None:
    pending = [
        _rec("/sdcard/WhatsApp/tiny.mp4", 5 * 1024 * 1024),
        _rec("/sdcard/DCIM/Camera/big.mp4", 400 * 1024 * 1024),
        _rec("/sdcard/Download/huge.mp4", 3 * 1024 * 1024 * 1024),
    ]
    rec, reason = recommend(pending)
    paths = {r.remote_path for r in rec}
    assert "/sdcard/DCIM/Camera/big.mp4" in paths
    assert "/sdcard/Download/huge.mp4" in paths
    assert "/sdcard/WhatsApp/tiny.mp4" not in paths
    assert "100 MB" in reason


def test_recommend_camera_ranked_first() -> None:
    pending = [
        _rec("/sdcard/Download/a.mp4", 200 * 1024 * 1024),
        _rec("/sdcard/DCIM/Camera/b.mp4", 200 * 1024 * 1024),
    ]
    rec, _ = recommend(pending)
    assert rec[0].remote_path.endswith("DCIM/Camera/b.mp4")


def test_build_scan_breakdown_groups() -> None:
    pending = [
        _rec("/sdcard/DCIM/Camera/a.mp4", 50 * 1024 * 1024),
        _rec("/sdcard/DCIM/Camera/b.mp4", 150 * 1024 * 1024),
        _rec("/sdcard/Movies/c.mp4", 600 * 1024 * 1024),
    ]
    breakdown = build_scan_breakdown(pending)
    assert len(breakdown.by_folder) == 2
    assert breakdown.by_folder[0].bytes >= breakdown.by_folder[1].bytes
    assert any("medium" in g.key for g in breakdown.by_size)
    assert any("large" in g.key for g in breakdown.by_size)
    assert len(breakdown.recommended) >= 2


def test_filter_folder_and_min_size() -> None:
    pending = [
        _rec("/sdcard/DCIM/Camera/a.mp4", 50 * 1024 * 1024),
        _rec("/sdcard/DCIM/Camera/b.mp4", 200 * 1024 * 1024),
        _rec("/sdcard/Movies/c.mp4", 200 * 1024 * 1024),
    ]
    filtered = filter_pending(
        pending,
        folders=["/sdcard/DCIM/Camera"],
        min_bytes=100 * 1024 * 1024,
    )
    assert len(filtered) == 1
    assert filtered[0].remote_path.endswith("b.mp4")


def test_parse_size() -> None:
    assert parse_size("100MB") == 100 * 1024 * 1024
    assert parse_size("1.5G") == int(1.5 * 1024**3)
    assert parse_size("2048") == 2048
    with pytest.raises(ValueError):
        parse_size("")
