"""Scan report: group pending videos by folder and size, rank recommendations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

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


def folder_of(remote_path: str) -> str:
    parent = str(PurePosixPath(remote_path).parent).replace("\\", "/")
    return parent if parent not in {".", ""} else "/"


def size_bucket_of(size: int) -> str:
    for label, lo, hi in SIZE_BUCKETS:
        if size >= lo and (hi is None or size < hi):
            return label
    return SIZE_BUCKETS[-1][0]


def format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def _group(
    records: list[VideoRecord],
    key_fn,
    *,
    order: list[str] | None = None,
) -> list[GroupStats]:
    buckets: dict[str, list[VideoRecord]] = defaultdict(list)
    for rec in records:
        buckets[key_fn(rec)].append(rec)

    keys = order if order is not None else sorted(
        buckets.keys(),
        key=lambda k: sum(r.size for r in buckets[k]),
        reverse=True,
    )
    result: list[GroupStats] = []
    for key in keys:
        items = buckets.get(key, [])
        if not items and order is not None:
            continue
        if not items:
            continue
        result.append(
            GroupStats(
                key=key,
                count=len(items),
                bytes=sum(r.size for r in items),
                records=sorted(items, key=lambda r: r.size, reverse=True),
            )
        )
    # Include any keys not in explicit order
    if order is not None:
        for key, items in buckets.items():
            if key in order:
                continue
            result.append(
                GroupStats(
                    key=key,
                    count=len(items),
                    bytes=sum(r.size for r in items),
                    records=sorted(items, key=lambda r: r.size, reverse=True),
                )
            )
    return result


def _folder_priority(path: str) -> int:
    lower = path.lower()
    for i, hint in enumerate(PREFERRED_FOLDER_HINTS):
        if hint in lower:
            return i
    return len(PREFERRED_FOLDER_HINTS)


def recommend(pending: list[VideoRecord]) -> tuple[list[VideoRecord], str]:
    """Recommend high-value targets: medium+ size, prefer camera folders.

    Falls back to the largest 25% of pending (min 1) if nothing matches.
    """
    if not pending:
        return [], "nothing pending"

    medium_plus = [
        r
        for r in pending
        if size_bucket_of(r.size)
        in {
            "medium (100–500 MB)",
            "large (500 MB–2 GB)",
            "huge (≥2 GB)",
        }
    ]

    if medium_plus:
        # Prefer DCIM/Camera-like paths, then largest first
        ranked = sorted(
            medium_plus,
            key=lambda r: (_folder_priority(r.remote_path), -r.size),
        )
        reason = (
            f"{len(ranked)} file(s) ≥100 MB "
            f"({format_bytes(sum(r.size for r in ranked))}) — "
            "best space savings; camera folders ranked first"
        )
        return ranked, reason

    # All tiny/small: recommend largest quartile
    ordered = sorted(pending, key=lambda r: r.size, reverse=True)
    n = max(1, len(ordered) // 4)
    ranked = ordered[:n]
    reason = (
        f"No files ≥100 MB; recommending largest {n} of {len(pending)} "
        f"({format_bytes(sum(r.size for r in ranked))})"
    )
    return ranked, reason


def build_scan_breakdown(pending: list[VideoRecord]) -> ScanBreakdown:
    by_folder = _group(pending, lambda r: folder_of(r.remote_path))
    bucket_order = [label for label, _, _ in SIZE_BUCKETS]
    by_size = _group(pending, lambda r: size_bucket_of(r.size), order=bucket_order)
    recommended, reason = recommend(pending)
    return ScanBreakdown(
        pending=pending,
        by_folder=by_folder,
        by_size=by_size,
        recommended=recommended,
        recommend_reason=reason,
    )


def filter_pending(
    pending: list[VideoRecord],
    *,
    folders: list[str] | None = None,
    size_labels: list[str] | None = None,
    min_bytes: int | None = None,
    max_bytes: int | None = None,
    paths: set[str] | None = None,
    recommended_only: bool = False,
) -> list[VideoRecord]:
    result = list(pending)
    if recommended_only:
        rec, _ = recommend(result)
        result = rec
    if paths is not None:
        result = [r for r in result if r.remote_path in paths]
    if folders:
        norms = [f.rstrip("/") for f in folders]
        result = [
            r
            for r in result
            if any(
                folder_of(r.remote_path) == n
                or folder_of(r.remote_path).startswith(n + "/")
                or n in folder_of(r.remote_path)
                for n in norms
            )
        ]
    if size_labels:
        labels = set(size_labels)
        result = [r for r in result if size_bucket_of(r.size) in labels]
    if min_bytes is not None:
        result = [r for r in result if r.size >= min_bytes]
    if max_bytes is not None:
        result = [r for r in result if r.size <= max_bytes]
    return result


def parse_size(text: str) -> int:
    """Parse sizes like 100MB, 1.5G, 2048."""
    raw = text.strip().upper().replace(" ", "")
    if not raw:
        raise ValueError("empty size")
    multipliers = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if raw.endswith(suffix):
            num = float(raw[: -len(suffix)])
            return int(num * mult)
    return int(float(raw))
