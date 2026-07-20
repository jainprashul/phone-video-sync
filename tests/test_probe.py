"""Unit tests for ffprobe JSON parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from phone_video_sync.ffmpeg import (
    FFmpegError,
    _color_cli_args,
    collect_preserve_tags,
    parse_ffprobe_json,
    write_head_tail_probe_file,
)


SAMPLE = {
    "format": {
        "duration": "12.345",
        "size": "1048576",
        "bit_rate": "800000",
        "tags": {"creation_time": "2024-01-15T10:00:00.000000Z"},
    },
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
        },
    ],
}


def test_parse_ffprobe_json_basic() -> None:
    info = parse_ffprobe_json(SAMPLE, "/tmp/clip.mp4")
    assert info.duration_sec == pytest.approx(12.345)
    assert info.width == 1920
    assert info.height == 1080
    assert info.size_bytes == 1048576
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.creation_time == "2024-01-15T10:00:00.000000Z"
    assert info.bitrate == 800000


def test_parse_ffprobe_json_duration_from_stream() -> None:
    data = {
        "format": {"size": "100"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 640,
                "height": 360,
                "duration": "5.5",
            }
        ],
    }
    info = parse_ffprobe_json(data, "x.mp4")
    assert info.duration_sec == pytest.approx(5.5)
    assert info.audio_codec is None


def test_parse_ffprobe_json_no_video_raises() -> None:
    data = {
        "format": {"duration": "1"},
        "streams": [{"codec_type": "audio", "codec_name": "aac"}],
    }
    with pytest.raises(FFmpegError, match="No video stream"):
        parse_ffprobe_json(data, "audio.m4a")


def test_write_head_tail_probe_file(tmp_path: Path) -> None:
    dest = tmp_path / "probe.bin"
    head = b"HEAD" + b"\x00" * 100
    tail = b"TAIL" + b"\x11" * 50
    total = 10_000
    write_head_tail_probe_file(dest, head=head, tail=tail, total_size=total)
    data = dest.read_bytes()
    assert len(data) == total
    assert data[: len(head)] == head
    assert data[-len(tail) :] == tail


def test_collect_preserve_tags_keeps_phone_fields() -> None:
    raw = {
        "format": {
            "tags": {
                "major_brand": "mp42",
                "creation_time": "2025-07-12T14:18:58.000000Z",
                "location": "+12.3456+078.9012/",
                "location-eng": "+12.3456+078.9012/",
                "com.android.version": "14",
                "encoder": "Lavf60",
            }
        },
        "streams": [
            {
                "codec_type": "video",
                "tags": {"creation_time": "2025-07-12T14:18:58.000000Z", "handler_name": "Video"},
            }
        ],
    }
    tags = collect_preserve_tags(raw)
    assert tags["creation_time"] == "2025-07-12T14:18:58.000000Z"
    assert tags["location"] == "+12.3456+078.9012/"
    assert tags["com.android.version"] == "14"
    assert "major_brand" not in tags
    assert "encoder" not in tags
    assert "handler_name" not in tags


def test_color_cli_args_from_stream() -> None:
    raw = {
        "streams": [
            {
                "codec_type": "video",
                "color_primaries": "bt709",
                "color_transfer": "bt709",
                "color_space": "bt709",
                "color_range": "tv",
            }
        ]
    }
    args = _color_cli_args(raw)
    assert args == [
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-color_range",
        "tv",
    ]
