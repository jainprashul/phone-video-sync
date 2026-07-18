"""Unit tests for verify tolerance logic."""

from __future__ import annotations

from phone_video_sync.models import Config, MediaInfo
from phone_video_sync.verify import check


def _media(
    *,
    duration: float = 10.0,
    width: int = 1280,
    height: int = 720,
    size: int = 10_000_000,
    codec: str | None = "hevc",
) -> MediaInfo:
    return MediaInfo(
        path="x",
        duration_sec=duration,
        width=width,
        height=height,
        size_bytes=size,
        video_codec=codec,
    )


def test_verify_pass() -> None:
    cfg = Config()
    src = _media()
    out = _media(size=4_000_000, duration=10.2)
    result = check(src, out, cfg)
    assert result.ok
    assert result.reasons == []


def test_verify_duration_fail() -> None:
    cfg = Config(duration_tolerance_sec=1.0)
    src = _media(duration=10.0)
    out = _media(duration=12.5, size=1_000_000)
    result = check(src, out, cfg)
    assert not result.ok
    assert any("duration" in r for r in result.reasons)


def test_verify_resolution_fail() -> None:
    cfg = Config()
    src = _media(width=1920, height=1080)
    out = _media(width=1280, height=720, size=1_000_000)
    result = check(src, out, cfg)
    assert not result.ok
    assert any("resolution" in r for r in result.reasons)


def test_verify_not_smaller_fail() -> None:
    cfg = Config(require_smaller=True)
    src = _media(size=5_000_000)
    out = _media(size=5_000_000)
    result = check(src, out, cfg)
    assert not result.ok
    assert any("smaller" in r for r in result.reasons)


def test_verify_allow_not_smaller_when_disabled() -> None:
    cfg = Config(require_smaller=False)
    src = _media(size=5_000_000)
    out = _media(size=6_000_000)
    result = check(src, out, cfg)
    assert result.ok


def test_verify_missing_codec() -> None:
    cfg = Config()
    src = _media()
    out = _media(size=1_000_000, codec=None)
    result = check(src, out, cfg)
    assert not result.ok
    assert any("codec" in r for r in result.reasons)
