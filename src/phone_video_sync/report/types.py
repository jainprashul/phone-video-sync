"""Dataclasses and constants for scan reports."""

from __future__ import annotations

from dataclasses import dataclass, field

from phone_video_sync.models import VideoRecord

# Size buckets (upper bound exclusive except last)
SIZE_BUCKETS: list[tuple[str, int, int | None]] = [
    ("tiny (<20 MB)", 0, 20 * 1024 * 1024),
    ("small (20–100 MB)", 20 * 1024 * 1024, 100 * 1024 * 1024),
    ("medium (100–500 MB)", 100 * 1024 * 1024, 500 * 1024 * 1024),
    ("large (500 MB–2 GB)", 500 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
    ("huge (≥2 GB)", 2 * 1024 * 1024 * 1024, None),
]

# Prefer these path fragments when ranking recommendations
PREFERRED_FOLDER_HINTS = (
    "/dcim/camera",
    "/dcim/cameraroll",
    "/dcim/",
    "/movies/",
    "/pictures/",
)

# Size buckets that always qualify for the baseline recommendation set
HIGH_VALUE_BUCKETS = frozenset(
    {
        "medium (100–500 MB)",
        "large (500 MB–2 GB)",
        "huge (≥2 GB)",
    }
)


@dataclass
class FileMeta:
    """Display metadata: ADB listing + MediaStore + optional ffprobe headers."""

    remote_path: str
    name: str
    folder: str
    extension: str
    size: int
    size_label: str
    bucket: str
    mtime: int
    modified: str
    status: str
    attempts: int
    recommended: bool
    output_name: str
    est_out_bytes: int
    est_save_bytes: int
    width: int | None = None
    height: int | None = None
    resolution: str = "?"
    quality: str = "?"
    duration_sec: float | None = None
    duration_label: str = "?"
    mime_type: str | None = None
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    bitrate: int | None = None
    bitrate_label: str = "?"
    fps: str | None = None
    pix_fmt: str | None = None
    profile: str | None = None
    level: str | None = None
    title: str | None = None


@dataclass
class GroupStats:
    key: str
    count: int
    bytes: int
    records: list[VideoRecord] = field(default_factory=list)

    @property
    def est_savings(self) -> int:
        # Rough HEVC estimate: keep ~40% of original → save ~60%
        return int(self.bytes * 0.6)


@dataclass
class ScanBreakdown:
    pending: list[VideoRecord]
    by_folder: list[GroupStats]
    by_size: list[GroupStats]
    recommended: list[VideoRecord]
    recommend_reason: str
    failed: list[VideoRecord] = field(default_factory=list)
    metas: dict[str, FileMeta] = field(default_factory=dict)
