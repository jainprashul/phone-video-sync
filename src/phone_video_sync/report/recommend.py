"""Recommendation scoring and selection for HEVC re-encode candidates."""

from __future__ import annotations

from math import log10

from phone_video_sync.models import VideoRecord, VideoStatus
from phone_video_sync.report.format import folder_of, format_bytes, size_bucket_of
from phone_video_sync.report.types import (
    FileMeta,
    HIGH_VALUE_BUCKETS,
    PREFERRED_FOLDER_HINTS,
    ScanBreakdown,
)

# Codecs that usually still benefit from HEVC re-encode
_INEFFICIENT_CODECS = frozenset(
    {
        "h264",
        "avc",
        "avc1",
        "mpeg4",
        "msmpeg4v3",
        "mpeg2video",
        "mpeg1video",
        "vp8",
        "wmv3",
        "wmv2",
        "rv40",
        "flv1",
        "mjpeg",
    }
)
# Already-efficient — deprioritize unless bitrate is extreme
_EFFICIENT_CODECS = frozenset(
    {
        "hevc",
        "h265",
        "hev1",
        "hvc1",
        "av1",
        "vp9",
        "vvc",
        "h266",
    }
)

_QUALITY_SCORE = {
    "8K": 30.0,
    "4K": 25.0,
    "1440p": 18.0,
    "1080p": 12.0,
    "720p": 6.0,
    "480p": 2.0,
    "360p": 1.0,
}


def folder_priority(path: str) -> int:
    """Lower index = preferred folder (camera roll, DCIM, etc.)."""
    lower = path.lower()
    for i, hint in enumerate(PREFERRED_FOLDER_HINTS):
        if hint in lower:
            return i
    return len(PREFERRED_FOLDER_HINTS)


def codec_family(codec: str | None) -> str:
    """Classify codec as efficient, inefficient, or other/unknown."""
    if not codec:
        return "unknown"
    c = codec.lower().split("/")[0].strip()
    for name in _EFFICIENT_CODECS:
        if name in c:
            return "efficient"
    for name in _INEFFICIENT_CODECS:
        if name in c:
            return "inefficient"
    return "other"


def legacy_size_folder_set(pending: list[VideoRecord]) -> list[VideoRecord]:
    """Original rule: all ≥100 MB, else largest quartile. Folder used for ranking."""
    medium_plus = [r for r in pending if size_bucket_of(r.size) in HIGH_VALUE_BUCKETS]
    if medium_plus:
        return sorted(
            medium_plus,
            key=lambda r: (folder_priority(r.remote_path), -r.size),
        )
    if not pending:
        return []
    ordered = sorted(
        pending,
        key=lambda r: (folder_priority(r.remote_path), -r.size),
    )
    n = max(1, len(ordered) // 4)
    return ordered[:n]


def recommendation_score(
    rec: VideoRecord,
    meta: FileMeta | None,
    *,
    output_suffix: str = "_hevc",
) -> float:
    """Higher = better candidate for HEVC re-encode (space savings)."""
    score = 0.0
    size_mb = max(rec.size / (1024 * 1024), 0.1)
    bucket = size_bucket_of(rec.size)

    # Size (classic baseline; strongest axis for space savings)
    score += min(50.0, log10(size_mb) * 16.0)
    if bucket in HIGH_VALUE_BUCKETS:
        score += 18.0
    elif size_mb < 20:
        score -= 12.0
    if size_mb < 5:
        score -= 20.0

    # Folder (camera-roll preference)
    score += max(0.0, 20.0 - 4.0 * folder_priority(rec.remote_path))

    if rec.status == VideoStatus.FAILED:
        score += 40.0

    if meta is None:
        return score

    if output_suffix and output_suffix in meta.name:
        return -1000.0

    family = codec_family(meta.video_codec)
    if family == "efficient":
        score -= 28.0
    elif family == "inefficient":
        score += 30.0
    else:
        score += 8.0

    score += _QUALITY_SCORE.get(meta.quality, 3.0 if meta.quality != "?" else 0.0)

    if meta.bitrate and meta.width and meta.height:
        megapixels = (meta.width * meta.height) / 1_000_000.0
        mbps = meta.bitrate / 1_000_000.0
        if megapixels > 0:
            ratio = mbps / megapixels
            if ratio >= 10:
                score += 22.0
            elif ratio >= 5:
                score += 14.0
            elif ratio >= 2.5:
                score += 7.0
            elif family == "efficient" and ratio < 1.5:
                score -= 12.0

    if meta.duration_sec and meta.duration_sec >= 30:
        score += min(12.0, meta.duration_sec / 100.0)

    if size_mb < 20 and family == "efficient":
        score -= 20.0

    return score


def recommend(
    pending: list[VideoRecord],
    *,
    metas: dict[str, FileMeta] | None = None,
    output_suffix: str = "_hevc",
    min_score: float = 25.0,
) -> tuple[list[VideoRecord], str]:
    """Recommend videos likely to shrink most with HEVC re-encode."""
    if not pending:
        return [], "nothing pending"

    size_set = legacy_size_folder_set(pending)
    size_paths = {r.remote_path for r in size_set}

    scored: list[tuple[float, VideoRecord]] = []
    for rec in pending:
        meta = metas.get(rec.remote_path) if metas else None
        scored.append(
            (recommendation_score(rec, meta, output_suffix=output_suffix), rec)
        )
    scored.sort(key=lambda t: t[0], reverse=True)

    meta_set = [rec for score, rec in scored if score >= min_score]

    by_path: dict[str, VideoRecord] = {}
    for rec in size_set:
        by_path[rec.remote_path] = rec
    for rec in meta_set:
        by_path[rec.remote_path] = rec

    if metas:
        for path in list(by_path):
            m = metas.get(path)
            if m and output_suffix and output_suffix in m.name:
                by_path.pop(path, None)

    selected = list(by_path.values())
    selected.sort(
        key=lambda r: recommendation_score(
            r,
            metas.get(r.remote_path) if metas else None,
            output_suffix=output_suffix,
        ),
        reverse=True,
    )

    if len(selected) > 500:
        selected = selected[:500]

    if not selected:
        return [], "nothing recommended"

    from_size = sum(1 for r in selected if r.remote_path in size_paths)
    from_meta_only = len(selected) - from_size
    inefficient = hi_res = efficient_deprioritized = 0
    for r in selected:
        m = metas.get(r.remote_path) if metas else None
        if not m:
            continue
        if codec_family(m.video_codec) == "inefficient":
            inefficient += 1
        if m.quality in {"8K", "4K", "1440p", "1080p"}:
            hi_res += 1
        if codec_family(m.video_codec) == "efficient":
            efficient_deprioritized += 1

    used_medium_plus = any(
        size_bucket_of(r.size) in HIGH_VALUE_BUCKETS for r in size_set
    )
    size_note = "≥100 MB" if used_medium_plus else "largest quartile"
    reason = (
        f"{len(selected)} file(s) "
        f"({format_bytes(sum(r.size for r in selected))}) — "
        f"size/folder baseline ({size_note}: {from_size})"
        f"{f' + {from_meta_only} metadata pick(s)' if from_meta_only else ''}"
        f"; ranked by size + folder + codec/resolution"
        f"{f'; {inefficient} inefficient codec' if inefficient else ''}"
        f"{f'; {hi_res} ≥1080p' if hi_res else ''}"
        f"{f'; {efficient_deprioritized} efficient ranked lower' if efficient_deprioritized else ''}"
    )
    return selected, reason


def refresh_recommendations(
    breakdown: ScanBreakdown,
    *,
    output_suffix: str = "_hevc",
    min_score: float = 25.0,
) -> ScanBreakdown:
    """Recompute recommended set after MediaStore/probe metas are filled."""
    recommended, reason = recommend(
        breakdown.pending,
        metas=breakdown.metas,
        output_suffix=output_suffix,
        min_score=min_score,
    )
    rec_paths = {r.remote_path for r in recommended}
    new_metas: dict[str, FileMeta] = {}
    for path, meta in breakdown.metas.items():
        meta.recommended = path in rec_paths
        new_metas[path] = meta
    breakdown.recommended = recommended
    breakdown.recommend_reason = reason
    breakdown.metas = new_metas
    return breakdown


def probe_candidates(breakdown: ScanBreakdown) -> list[VideoRecord]:
    """Files worth probing for codec before final recommendation scoring."""
    candidates: list[VideoRecord] = []
    seen: set[str] = set()

    def _maybe_add(rec: VideoRecord) -> None:
        if rec.remote_path in seen:
            return
        meta = breakdown.metas.get(rec.remote_path)
        if meta and meta.video_codec:
            return
        size_ok = size_bucket_of(rec.size) in HIGH_VALUE_BUCKETS
        hi_res = bool(meta and meta.quality in {"8K", "4K", "1440p", "1080p"})
        if size_ok or hi_res:
            candidates.append(rec)
            seen.add(rec.remote_path)

    for rec in breakdown.pending:
        _maybe_add(rec)
    for rec in breakdown.recommended:
        _maybe_add(rec)
    return candidates


def sort_recommended(
    records: list[VideoRecord],
    metas: dict[str, FileMeta],
    *,
    output_suffix: str = "_hevc",
) -> list[VideoRecord]:
    """Order by recommendation score (highest first)."""
    return sorted(
        records,
        key=lambda r: recommendation_score(
            r, metas.get(r.remote_path), output_suffix=output_suffix
        ),
        reverse=True,
    )
