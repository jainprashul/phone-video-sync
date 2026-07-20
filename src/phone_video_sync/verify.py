"""Integrity checks for compressed video output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from phone_video_sync.models import Config, MediaInfo, VerifyResult


def _display_dimensions(info: MediaInfo) -> tuple[int, int]:
    """Return (width, height) after applying any rotation metadata.

    Phone videos are often stored with coded dimensions 1920x1080 plus a
    rotate=90 tag (or side_data display matrix).  Some ffprobe versions report
    the coded dimensions; others report the display-rotated ones.  Normalising
    both src and output to display dimensions before comparing avoids false
    resolution-mismatch failures.
    """
    w, h = info.width, info.height
    raw: dict[str, Any] = info.raw or {}
    streams: list[dict[str, Any]] = raw.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return w, h

    # 1) Explicit rotate tag (most Android phones)
    try:
        rotate = int((video.get("tags") or {}).get("rotate", 0))
    except (TypeError, ValueError):
        rotate = 0

    # 2) side_data_list display matrix (newer ffprobe / H.265 container)
    if rotate == 0:
        for sd in video.get("side_data_list") or []:
            if sd.get("side_data_type") == "Display Matrix":
                try:
                    rotate = int(sd.get("rotation", 0))
                except (TypeError, ValueError):
                    pass

    # Normalise negative angles (-90 == 270)
    rotate = rotate % 360
    if rotate in {90, 270}:
        return h, w
    return w, h


def check(
    src: MediaInfo,
    out: MediaInfo,
    cfg: Config,
    *,
    out_path: Path | None = None,
) -> VerifyResult:
    """Pass requires duration within tolerance, identical resolution, smaller size, readable."""
    reasons: list[str] = []

    if out.width <= 0 or out.height <= 0:
        reasons.append("output has invalid resolution")
    else:
        src_display = _display_dimensions(src)
        out_display = _display_dimensions(out)
        if out_display != src_display:
            reasons.append(
                f"resolution mismatch: src={src.width}x{src.height} out={out.width}x{out.height}"
            )

    duration_delta = abs(out.duration_sec - src.duration_sec)
    if duration_delta > cfg.duration_tolerance_sec:
        reasons.append(
            f"duration delta {duration_delta:.3f}s exceeds "
            f"tolerance {cfg.duration_tolerance_sec}s "
            f"(src={src.duration_sec:.3f} out={out.duration_sec:.3f})"
        )

    out_size = out.size_bytes
    if out_path is not None and out_path.is_file():
        out_size = out_path.stat().st_size
        if out.size_bytes and out.size_bytes != out_size:
            # Prefer on-disk size
            out = MediaInfo(
                path=out.path,
                duration_sec=out.duration_sec,
                width=out.width,
                height=out.height,
                size_bytes=out_size,
                video_codec=out.video_codec,
                audio_codec=out.audio_codec,
                creation_time=out.creation_time,
                bitrate=out.bitrate,
                raw=out.raw,
            )

    if out_size <= 0:
        reasons.append("output size is zero or unknown")
    elif cfg.require_smaller and out_size >= src.size_bytes:
        reasons.append(
            f"output not smaller: src={src.size_bytes} out={out_size}"
        )

    # Playability: MediaInfo already came from a successful ffprobe; require codec present
    if not out.video_codec:
        reasons.append("output has no video codec (ffprobe)")

    return VerifyResult(ok=len(reasons) == 0, reasons=reasons)
