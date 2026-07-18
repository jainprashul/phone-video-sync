"""Tests for MediaStore parsing and quality labels."""

from __future__ import annotations

from phone_video_sync.adb import (
    format_duration,
    parse_mediastore_row,
    quality_label,
)
from phone_video_sync.report import build_file_meta, output_name_for
from phone_video_sync.models import VideoRecord


def test_parse_mediastore_row() -> None:
    line = (
        "Row: 0 _data=/storage/emulated/0/DCIM/Camera/a.mp4, "
        "mime_type=video/mp4, width=1920, height=1080, duration=12500, "
        "_size=123456, date_modified=1700000000, resolution=1920x1080, title=a"
    )
    fields = parse_mediastore_row(line)
    assert fields["_data"].endswith("a.mp4")
    assert fields["mime_type"] == "video/mp4"
    assert fields["width"] == "1920"
    assert fields["height"] == "1080"
    assert fields["duration"] == "12500"


def test_quality_label() -> None:
    assert quality_label(3840, 2160) == "4K"
    assert quality_label(1920, 1080) == "1080p"
    assert quality_label(1080, 1920) == "1080p"  # vertical
    assert quality_label(None, None) == "?"


def test_format_duration() -> None:
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(None) == "?"


def test_file_meta_with_remote_and_probe() -> None:
    from types import SimpleNamespace

    rec = VideoRecord(remote_path="/sdcard/DCIM/Camera/clip.mp4", size=200_000_000, mtime=1)
    remote = SimpleNamespace(
        width=1920,
        height=1080,
        duration_ms=60000,
        mime_type="video/mp4",
        title="clip",
        resolution="1920x1080",
    )
    probe = SimpleNamespace(
        width=1920,
        height=1080,
        duration_sec=60.0,
        video_codec="h264",
        audio_codec="aac",
        bitrate=8_000_000,
        fps="30.00",
        pix_fmt="yuv420p",
        profile="High",
        level="40",
    )
    meta = build_file_meta(
        rec,
        recommended_paths={rec.remote_path},
        output_suffix="_hevc",
        remote=remote,
        probe=probe,
    )
    assert meta.quality == "1080p"
    assert meta.resolution == "1920x1080"
    assert meta.video_codec == "h264"
    assert meta.audio_codec == "aac"
    assert meta.output_name == "clip_hevc.mp4"
    assert output_name_for(rec.remote_path) == "clip_hevc.mp4"
