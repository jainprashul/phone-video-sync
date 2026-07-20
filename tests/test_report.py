"""Unit tests for scan report grouping and recommendations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from phone_video_sync.models import VideoRecord
from phone_video_sync.report import (
    apply_probe_to_meta,
    build_file_meta,
    build_scan_breakdown,
    filter_pending,
    folder_of,
    output_name_for,
    parse_size,
    recommend,
    recommendation_score,
    refresh_recommendations,
    size_bucket_of,
)


def _rec(path: str, size: int) -> VideoRecord:
    return VideoRecord(remote_path=path, size=size, mtime=1)


def _meta(
    rec: VideoRecord,
    *,
    video_codec: str | None = None,
    width: int | None = None,
    height: int | None = None,
    bitrate: int | None = None,
    duration_sec: float | None = None,
):
    breakdown = build_scan_breakdown([rec])
    apply_probe_to_meta(
        breakdown,
        rec.remote_path,
        SimpleNamespace(
            width=width,
            height=height,
            duration_sec=duration_sec,
            video_codec=video_codec,
            audio_codec="aac",
            bitrate=bitrate,
            fps="30",
            pix_fmt="yuv420p",
            profile=None,
            level=None,
        ),
        output_suffix="_hevc",
    )
    return breakdown.metas[rec.remote_path]


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
    assert "≥100 MB" in reason or "size/folder" in reason


def test_recommend_keeps_large_hevc_in_baseline() -> None:
    """Size baseline still includes ≥100 MB even when already HEVC."""
    hevc = _rec("/sdcard/DCIM/Camera/already.mp4", 400 * 1024 * 1024)
    tiny = _rec("/sdcard/WhatsApp/tiny.mp4", 8 * 1024 * 1024)
    metas = {
        hevc.remote_path: _meta(
            hevc, video_codec="hevc", width=1920, height=1080, bitrate=4_000_000
        ),
        tiny.remote_path: _meta(
            tiny, video_codec="h264", width=1280, height=720, bitrate=8_000_000
        ),
    }
    selected, reason = recommend([hevc, tiny], metas=metas)
    paths = {r.remote_path for r in selected}
    assert hevc.remote_path in paths
    assert "size/folder" in reason


def test_recommend_camera_ranked_first() -> None:
    pending = [
        _rec("/sdcard/Download/a.mp4", 200 * 1024 * 1024),
        _rec("/sdcard/DCIM/Camera/b.mp4", 200 * 1024 * 1024),
    ]
    rec, _ = recommend(pending)
    assert rec[0].remote_path.endswith("DCIM/Camera/b.mp4")


def test_recommend_prefers_h264_over_hevc() -> None:
    h264 = _rec("/sdcard/DCIM/Camera/h264.mp4", 300 * 1024 * 1024)
    hevc = _rec("/sdcard/DCIM/Camera/hevc.mp4", 300 * 1024 * 1024)
    metas = {
        h264.remote_path: _meta(
            h264, video_codec="h264", width=1920, height=1080, bitrate=12_000_000
        ),
        hevc.remote_path: _meta(
            hevc, video_codec="hevc", width=1920, height=1080, bitrate=8_000_000
        ),
    }
    selected, reason = recommend([h264, hevc], metas=metas)
    assert h264.remote_path in {r.remote_path for r in selected}
    assert recommendation_score(h264, metas[h264.remote_path]) > recommendation_score(
        hevc, metas[hevc.remote_path]
    )
    assert "codec" in reason.lower() or "inefficient" in reason.lower() or selected


def test_recommend_skips_lean_hevc_when_h264_available() -> None:
    h264 = _rec("/sdcard/DCIM/Camera/fat_h264.mp4", 500 * 1024 * 1024)
    hevc = _rec("/sdcard/DCIM/Camera/lean_hevc.mp4", 500 * 1024 * 1024)
    metas = {
        h264.remote_path: _meta(
            h264, video_codec="h264", width=3840, height=2160, bitrate=45_000_000
        ),
        hevc.remote_path: _meta(
            hevc, video_codec="hevc", width=1920, height=1080, bitrate=2_000_000
        ),
    }
    selected, _ = recommend([h264, hevc], metas=metas, min_score=20.0)
    paths = {r.remote_path for r in selected}
    assert h264.remote_path in paths
    assert selected[0].remote_path == h264.remote_path


def test_refresh_recommendations_updates_flags() -> None:
    pending = [
        _rec("/sdcard/DCIM/Camera/a.mp4", 200 * 1024 * 1024),
        _rec("/sdcard/Download/b.mp4", 200 * 1024 * 1024),
    ]
    breakdown = build_scan_breakdown(pending)
    apply_probe_to_meta(
        breakdown,
        pending[0].remote_path,
        SimpleNamespace(
            width=1920,
            height=1080,
            duration_sec=60.0,
            video_codec="h264",
            audio_codec="aac",
            bitrate=15_000_000,
            fps="30",
            pix_fmt=None,
            profile=None,
            level=None,
        ),
        output_suffix="_hevc",
    )
    apply_probe_to_meta(
        breakdown,
        pending[1].remote_path,
        SimpleNamespace(
            width=1920,
            height=1080,
            duration_sec=60.0,
            video_codec="hevc",
            audio_codec="aac",
            bitrate=3_000_000,
            fps="30",
            pix_fmt=None,
            profile=None,
            level=None,
        ),
        output_suffix="_hevc",
    )
    refresh_recommendations(breakdown)
    assert breakdown.metas[pending[0].remote_path].recommended is True
    assert breakdown.recommended[0].remote_path == pending[0].remote_path


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


def test_file_meta_and_output_name() -> None:
    assert output_name_for("/sdcard/DCIM/VID_1.mp4", "_hevc") == "VID_1_hevc.mp4"
    rec = _rec("/sdcard/DCIM/Camera/clip.mp4", 150 * 1024 * 1024)
    rec.mtime = 1_700_000_000
    meta = build_file_meta(rec, recommended_paths={rec.remote_path}, output_suffix="_hevc")
    assert meta.recommended is True
    assert meta.extension == "mp4"
    assert meta.output_name == "clip_hevc.mp4"
    assert meta.size_label
    assert "Camera" in meta.folder


def test_parse_size() -> None:
    assert parse_size("100MB") == 100 * 1024 * 1024
    assert parse_size("1.5G") == int(1.5 * 1024**3)
    assert parse_size("2048") == 2048
    with pytest.raises(ValueError):
        parse_size("")
