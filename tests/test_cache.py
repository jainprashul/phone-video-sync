"""Tests for listing + media_meta cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from phone_video_sync.db import Database


def test_listing_cache_ttl(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.save_listing_cache(
        "dev|/sdcard",
        device_serial="dev",
        files=[{"path": "/sdcard/a.mp4", "size": 10, "mtime": 1}],
    )
    hit = db.get_listing_cache("dev|/sdcard", max_age_sec=3600)
    assert hit is not None
    payload, _ = hit
    assert payload[0]["path"].endswith("a.mp4")

    # Force expiry by rewriting scanned_at in the past
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with db.connection() as conn:
        conn.execute(
            "UPDATE listing_cache SET scanned_at = ? WHERE cache_key = ?",
            (old, "dev|/sdcard"),
        )
    assert db.get_listing_cache("dev|/sdcard", max_age_sec=3600) is None


def test_media_meta_cache_invalidates_on_size_change(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.upsert_media_meta(
        "/sdcard/a.mp4",
        100,
        1,
        video_codec="h264",
        width=1920,
        height=1080,
        source="probe",
    )
    assert db.get_media_meta("/sdcard/a.mp4", size=100, mtime=1)["video_codec"] == "h264"
    assert db.get_media_meta("/sdcard/a.mp4", size=200, mtime=1) is None

    # Changing file via upsert_discovered_batch should delete media_meta
    db.upsert_discovered("/sdcard/a.mp4", 100, 1)
    db.upsert_discovered_batch([("/sdcard/a.mp4", 999, 1)])
    assert db.get_media_meta("/sdcard/a.mp4", size=100, mtime=1) is None
