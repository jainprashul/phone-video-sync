"""Scan report: group pending videos by folder and size, rank recommendations."""

from __future__ import annotations

from phone_video_sync.models import VideoRecord
from phone_video_sync.report.format import (
    choice_label,
    folder_of,
    format_bytes,
    format_mtime,
    output_name_for,
    parse_size,
    size_bucket_of,
)
from phone_video_sync.report.grouping import group_records
from phone_video_sync.report.meta import (
    apply_probe_to_meta,
    apply_remote_map,
    attach_failed_records,
    build_file_meta,
)
from phone_video_sync.report.recommend import (
    probe_candidates,
    recommend,
    recommendation_score,
    refresh_recommendations,
    sort_recommended,
)
from phone_video_sync.report.types import (
    SIZE_BUCKETS,
    FileMeta,
    GroupStats,
    PREFERRED_FOLDER_HINTS,
    ScanBreakdown,
)

__all__ = [
    "SIZE_BUCKETS",
    "PREFERRED_FOLDER_HINTS",
    "FileMeta",
    "GroupStats",
    "ScanBreakdown",
    "folder_of",
    "size_bucket_of",
    "format_bytes",
    "format_mtime",
    "output_name_for",
    "build_file_meta",
    "choice_label",
    "apply_remote_map",
    "apply_probe_to_meta",
    "attach_failed_records",
    "recommendation_score",
    "recommend",
    "refresh_recommendations",
    "probe_candidates",
    "sort_recommended",
    "build_scan_breakdown",
    "filter_pending",
    "parse_size",
]


def build_scan_breakdown(
    pending: list[VideoRecord],
    *,
    output_suffix: str = "_hevc",
) -> ScanBreakdown:
    """Initial breakdown; recommendations refreshed after metadata in pipeline."""
    by_folder = group_records(pending, lambda r: folder_of(r.remote_path))
    bucket_order = [label for label, _, _ in SIZE_BUCKETS]
    by_size = group_records(
        pending, lambda r: size_bucket_of(r.size), order=bucket_order
    )
    recommended, reason = recommend(pending, metas=None, output_suffix=output_suffix)
    rec_paths = {r.remote_path for r in recommended}
    metas = {
        r.remote_path: build_file_meta(
            r, recommended_paths=rec_paths, output_suffix=output_suffix
        )
        for r in pending
    }
    return ScanBreakdown(
        pending=pending,
        by_folder=by_folder,
        by_size=by_size,
        recommended=recommended,
        recommend_reason=reason,
        metas=metas,
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
    """Apply CLI filters to a pending work list."""
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
