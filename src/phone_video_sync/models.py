"""Shared dataclasses and status constants for phone-video-sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class VideoStatus(str, Enum):
    """Per-video state machine persisted in SQLite."""

    DISCOVERED = "discovered"  # seen on phone, not yet processed
    PULLING = "pulling"
    PULLED = "pulled"
    ENCODING = "encoding"
    VERIFYING = "verifying"
    PUSHING = "pushing"
    FINALIZING = "finalizing"  # archive/delete original on phone
    DONE = "done"
    FAILED = "failed"  # eligible for retry until max_attempts


IN_PROGRESS_STATUSES: frozenset[VideoStatus] = frozenset(
    {
        VideoStatus.PULLING,
        VideoStatus.PULLED,
        VideoStatus.ENCODING,
        VideoStatus.VERIFYING,
        VideoStatus.PUSHING,
        VideoStatus.FINALIZING,
    }
)

PENDING_STATUSES: frozenset[VideoStatus] = frozenset(
    {VideoStatus.DISCOVERED, VideoStatus.FAILED, *IN_PROGRESS_STATUSES}
)


@dataclass
class MediaInfo:
    path: str
    duration_sec: float
    width: int
    height: int
    size_bytes: int
    video_codec: str | None = None
    audio_codec: str | None = None
    creation_time: str | None = None
    bitrate: int | None = None
    fps: str | None = None
    pix_fmt: str | None = None
    profile: str | None = None
    level: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class VideoRecord:
    remote_path: str
    size: int
    mtime: int
    status: VideoStatus = VideoStatus.DISCOVERED
    attempts: int = 0
    last_error: str | None = None
    local_path: str | None = None
    output_path: str | None = None
    remote_output_path: str | None = None
    src_duration: float | None = None
    src_width: int | None = None
    src_height: int | None = None
    out_duration: float | None = None
    out_width: int | None = None
    out_height: int | None = None
    out_size: int | None = None
    saved_bytes: int | None = None
    discovered_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


@dataclass
class Config:
    db_path: Path = Path("data/pvsync.db")
    work_dir: Path = Path("work")
    log_dir: Path = Path("logs")
    remote_root: str = "/sdcard"
    archive_root: str = "/sdcard/.compressed_archive"
    extensions: list[str] = field(
        default_factory=lambda: ["mp4", "mov", "mkv", "3gp", "avi", "webm"]
    )
    skip_prefixes: list[str] = field(
        default_factory=lambda: ["Android/", ".compressed_archive/"]
    )
    video_encoder: str = "hevc_nvenc"
    preset: str = "p5"
    cq: int = 28
    audio_bitrate: str = "128k"
    duration_tolerance_sec: float = 1.0
    require_smaller: bool = True
    encode_workers: int = 2
    max_attempts: int = 3
    retry_backoff_base_sec: float = 2.0
    subprocess_timeout_sec: int = 3600
    delete_mode: str = "archive"  # archive | delete
    adb_path: str | None = None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    output_suffix: str = "_hevc"
    watch_interval_sec: float = 5.0
    # Reuse ADB find results for this many seconds unless --refresh
    listing_cache_ttl_sec: float = 1800.0
    project_root: Path = field(default_factory=Path.cwd)

    @property
    def work_in(self) -> Path:
        return self.work_dir / "in"

    @property
    def work_out(self) -> Path:
        return self.work_dir / "out"

    @property
    def work_failed(self) -> Path:
        return self.work_dir / "failed"


@dataclass
class VerifyResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    done: int = 0
    failed: int = 0
    skipped: int = 0
    saved_bytes: int = 0
    errors: list[str] = field(default_factory=list)
