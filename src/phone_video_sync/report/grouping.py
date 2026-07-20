"""Group pending videos by folder or size bucket."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from phone_video_sync.models import VideoRecord
from phone_video_sync.report.types import GroupStats


def group_records(
    records: list[VideoRecord],
    key_fn: Callable[[VideoRecord], str],
    *,
    order: list[str] | None = None,
) -> list[GroupStats]:
    """Aggregate records by key_fn; optional fixed key order (e.g. size buckets)."""
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
