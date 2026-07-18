"""FFmpeg / ffprobe wrappers: MediaInfo probe and HEVC NVENC encode."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
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
        fps=_fps(video),
        pix_fmt=video.get("pix_fmt"),
        profile=str(video.get("profile")) if video.get("profile") is not None else None,
        level=str(video.get("level")) if video.get("level") is not None else None,
        raw=data,
    )


def _fps(video: dict[str, Any]) -> str | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = video.get(key)
        if not raw or raw in {"0/0", "N/A"}:
            continue
        if isinstance(raw, str) and "/" in raw:
            num, den = raw.split("/", 1)
            try:
                n, d = float(num), float(den)
                if d:
                    return f"{n / d:.2f}"
            except ValueError:
                continue
        return str(raw)
    return None


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


def probe_bytes(
    data: bytes,
    ffprobe_path: str,
    *,
    label: str = "pipe",
    timeout: int = 60,
) -> MediaInfo:
    """ffprobe media headers from in-memory bytes (works for faststart / small files)."""
    if not data:
        raise FFmpegError(f"Empty bytes for probe: {label}")
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-probesize",
        "32M",
        "-analyzeduration",
        "32M",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        "pipe:0",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffprobe timed out for {label}") from exc
    except FileNotFoundError as exc:
        raise FFmpegError(f"Binary not found: {ffprobe_path}") from exc
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise FFmpegError(f"ffprobe failed for {label}: {detail}")
    try:
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"Invalid ffprobe JSON for {label}") from exc
    return parse_ffprobe_json(payload, label)


def _mark_sparse(path: Path) -> None:
    """Best-effort NTFS sparse flag so head+tail probe files don't allocate full size."""
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["fsutil", "sparse", "setflag", str(path)],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def write_head_tail_probe_file(
    dest: Path,
    *,
    head: bytes,
    tail: bytes,
    total_size: int,
) -> None:
    """Write a sparse-ish file with the real head and tail at correct offsets.

    Phone MP4/MOV files usually keep ``moov`` at the *end*, so head-only probes
    fail with "moov atom not found". Placing both ends at the right offsets lets
    ffprobe succeed without downloading the middle.
    """
    if total_size <= 0:
        raise FFmpegError("total_size must be positive")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    dest.touch()
    _mark_sparse(dest)

    with dest.open("r+b") as fh:
        fh.write(head)
        if not tail:
            if len(head) < total_size:
                fh.seek(total_size - 1)
                fh.write(b"\0")
            return

        tail_off = max(len(head), total_size - len(tail))
        # Avoid overlapping head/tail weirdness on tiny gaps
        if tail_off < len(head):
            # File is small enough that head already covered everything useful
            return
        fh.seek(tail_off)
        fh.write(tail)
        # Ensure logical size matches remote (helps some demuxers)
        if fh.tell() < total_size:
            fh.seek(total_size - 1)
            fh.write(b"\0")


def probe_path(
    path: Path,
    ffprobe_path: str,
    *,
    timeout: int = 90,
) -> MediaInfo:
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-probesize",
        "64M",
        "-analyzeduration",
        "64M",
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


def probe_remote_header(
    *,
    adb_client: Any,
    remote_path: str,
    ffprobe_path: str,
    header_mb: int = 2,
    tail_mb: int = 12,
    timeout: int = 90,
    work_dir: Path | None = None,
) -> MediaInfo:
    """Probe a remote video without a full pull.

    Strategy:
    1. Try head-only (faststart MP4 / many MKV/WebM).
    2. On failure, build a head+tail sparse local file (moov-at-end phone MP4/MOV).
    """
    head_bytes = max(1, header_mb) * 1024 * 1024
    tail_bytes = max(1, tail_mb) * 1024 * 1024

    head, tail, total_size = adb_client.stream_head_and_tail(
        remote_path, head_bytes=head_bytes, tail_bytes=tail_bytes
    )

    # 1) Fast path — whole small file or faststart
    try:
        blob = head if not tail else head  # head-only first
        info = probe_bytes(blob, ffprobe_path, label=remote_path, timeout=timeout)
        return _clear_partial_size(info, len(blob))
    except FFmpegError as head_exc:
        logger.debug("Head-only probe failed for %s: %s", remote_path, head_exc)

    # 2) Head + tail sparse file
    if not tail and total_size <= len(head):
        # Already had the whole file and it still failed
        raise FFmpegError(f"ffprobe could not parse {remote_path}")

    tmp_dir = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "_", Path(remote_path).name)[:80]
    tmp_path = tmp_dir / f"pvsync-probe-{os.getpid()}-{safe}"

    try:
        write_head_tail_probe_file(
            tmp_path, head=head, tail=tail or b"", total_size=total_size
        )
        info = probe_path(tmp_path, ffprobe_path, timeout=timeout)
        return _clear_partial_size(info, total_size)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _clear_partial_size(info: MediaInfo, max_trusted: int) -> MediaInfo:
    """Drop misleading size from partial probes."""
    if info.size_bytes and info.size_bytes <= max_trusted:
        return MediaInfo(
            path=info.path,
            duration_sec=info.duration_sec,
            width=info.width,
            height=info.height,
            size_bytes=0,
            video_codec=info.video_codec,
            audio_codec=info.audio_codec,
            creation_time=info.creation_time,
            bitrate=info.bitrate,
            fps=info.fps,
            pix_fmt=info.pix_fmt,
            profile=info.profile,
            level=info.level,
            raw=info.raw,
        )
    return info


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
