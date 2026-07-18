"""FFmpeg / ffprobe wrappers: MediaInfo probe and HEVC NVENC encode."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from phone_video_sync.models import Config, MediaInfo

logger = logging.getLogger(__name__)


class FFmpegError(Exception):
    """FFmpeg / ffprobe failure."""


def _run(
    cmd: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    logger.debug("FFmpeg: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"Command timed out: {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise FFmpegError(f"Binary not found: {cmd[0]}") from exc


def list_encoders(ffmpeg_path: str, *, timeout: int = 60) -> set[str]:
    result = _run([ffmpeg_path, "-hide_banner", "-encoders"], timeout=timeout)
    if result.returncode != 0:
        raise FFmpegError(f"ffmpeg -encoders failed: {result.stderr.strip()}")
    encoders: set[str] = set()
    for line in result.stdout.splitlines():
        # lines like: " V....D hevc_nvenc           NVIDIA NVENC hevc encoder"
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.add(parts[1])
    return encoders


def ensure_encoder_available(ffmpeg_path: str, encoder: str, *, timeout: int = 60) -> None:
    encoders = list_encoders(ffmpeg_path, timeout=timeout)
    if encoder not in encoders:
        raise FFmpegError(
            f"Requested encoder '{encoder}' is not available in ffmpeg. "
            f"Install a build with that encoder or change video_encoder in config."
        )


def parse_ffprobe_json(data: dict[str, Any], path: str | Path) -> MediaInfo:
    """Parse ffprobe -show_format -show_streams JSON into MediaInfo."""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise FFmpegError(f"No video stream in {path}")

    duration = _to_float(fmt.get("duration"))
    if duration is None and video.get("duration") is not None:
        duration = _to_float(video.get("duration"))
    if duration is None:
        duration = 0.0

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    size_bytes = int(_to_float(fmt.get("size")) or 0)
    if size_bytes == 0:
        p = Path(path)
        if p.is_file():
            size_bytes = p.stat().st_size

    tags = fmt.get("tags") or {}
    creation_time = tags.get("creation_time") or (video.get("tags") or {}).get("creation_time")
    bitrate = None
    if fmt.get("bit_rate") is not None:
        bitrate = int(_to_float(fmt.get("bit_rate")) or 0) or None

    return MediaInfo(
        path=str(path),
        duration_sec=duration,
        width=width,
        height=height,
        size_bytes=size_bytes,
        video_codec=video.get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
        creation_time=creation_time,
        bitrate=bitrate,
        raw=data,
    )


def get_media_info(
    path: Path | str,
    ffprobe_path: str,
    *,
    timeout: int = 120,
) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise FFmpegError(f"File not found for probe: {path}")

    cmd = [
        ffprobe_path,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = _run(cmd, timeout=timeout)
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"Invalid ffprobe JSON for {path}") from exc
    return parse_ffprobe_json(data, path)


def encode(
    input_path: Path,
    output_path: Path,
    cfg: Config,
    ffmpeg_path: str,
    *,
    timeout: int | None = None,
) -> None:
    """Encode with configured video encoder (default hevc_nvenc) preserving metadata."""
    if not input_path.is_file():
        raise FFmpegError(f"Input missing: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-c:v",
        cfg.video_encoder,
        "-preset",
        cfg.preset,
        "-rc",
        "vbr",
        "-cq",
        str(cfg.cq),
        "-c:a",
        "aac",
        "-b:a",
        cfg.audio_bitrate,
        "-map_metadata",
        "0",
        "-movflags",
        "use_metadata_tags+faststart",
        str(output_path),
    ]
    result = _run(cmd, timeout=timeout or cfg.subprocess_timeout_sec)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise FFmpegError(f"ffmpeg encode failed: {detail}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise FFmpegError(f"Encode produced empty output: {output_path}")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
