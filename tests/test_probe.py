"""Unit tests for ffprobe JSON parsing."""

from __future__ import annotations

import pytest

from phone_video_sync.ffmpeg import FFmpegError, parse_ffprobe_json


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
