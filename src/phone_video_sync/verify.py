"""Integrity checks for compressed video output."""

from __future__ import annotations

from pathlib import Path

from phone_video_sync.models import Config, MediaInfo, VerifyResult


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
    elif (out.width, out.height) != (src.width, src.height):
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
