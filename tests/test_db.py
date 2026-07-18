"""Unit tests for SQLite DB state machine."""

from __future__ import annotations

from pathlib import Path

from phone_video_sync.db import Database
from phone_video_sync.models import VideoStatus


def test_upsert_and_get(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    rec = db.upsert_discovered("/sdcard/DCIM/a.mp4", size=1000, mtime=111)
    assert rec.status == VideoStatus.DISCOVERED
    assert rec.remote_path == "/sdcard/DCIM/a.mp4"
    got = db.get("/sdcard/DCIM/a.mp4")
    assert got is not None
    assert got.size == 1000


def test_size_mtime_change_resets_done(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.upsert_discovered("/sdcard/a.mp4", 100, 1)
    db.record_result(
        "/sdcard/a.mp4",
        out_size=40,
        saved_bytes=60,
        remote_output_path="/sdcard/a_hevc.mp4",
    )
    assert db.get("/sdcard/a.mp4").status == VideoStatus.DONE  # type: ignore[union-attr]

    again = db.upsert_discovered("/sdcard/a.mp4", 200, 2)
    assert again.status == VideoStatus.DISCOVERED
    assert again.attempts == 0
    assert again.saved_bytes is None


def test_reconcile_resets_in_progress(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.upsert_discovered("/sdcard/a.mp4", 100, 1)
    db.set_status("/sdcard/a.mp4", VideoStatus.ENCODING)
    n = db.reconcile_on_start()
    assert n == 1
    assert db.get("/sdcard/a.mp4").status == VideoStatus.DISCOVERED  # type: ignore[union-attr]


def test_pending_work_excludes_exhausted_failures(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.upsert_discovered("/sdcard/ok.mp4", 100, 1)
    db.upsert_discovered("/sdcard/fail.mp4", 100, 1)
    db.mark_failed("/sdcard/fail.mp4", "boom")
    db.mark_failed("/sdcard/fail.mp4", "boom")
    db.mark_failed("/sdcard/fail.mp4", "boom")
    failed = db.get("/sdcard/fail.mp4")
    assert failed is not None
    assert failed.attempts == 3
    assert failed.status == VideoStatus.FAILED

    pending = db.pending_work(max_attempts=3)
    paths = {p.remote_path for p in pending}
    assert "/sdcard/ok.mp4" in paths
    assert "/sdcard/fail.mp4" not in paths


def test_pending_includes_retriable_failure(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.upsert_discovered("/sdcard/fail.mp4", 100, 1)
    db.mark_failed("/sdcard/fail.mp4", "once")
    pending = db.pending_work(max_attempts=3)
    assert len(pending) == 1
    assert pending[0].remote_path == "/sdcard/fail.mp4"


def test_status_transitions_and_saved(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.upsert_discovered("/sdcard/a.mp4", 1000, 1)
    db.set_status("/sdcard/a.mp4", VideoStatus.PULLING, local_path="/tmp/a")
    db.set_status("/sdcard/a.mp4", VideoStatus.PULLED)
    db.set_status("/sdcard/a.mp4", VideoStatus.ENCODING)
    db.record_result(
        "/sdcard/a.mp4",
        out_size=400,
        saved_bytes=600,
        remote_output_path="/sdcard/a_hevc.mp4",
        out_duration=1.0,
        out_width=640,
        out_height=360,
    )
    rec = db.get("/sdcard/a.mp4")
    assert rec is not None
    assert rec.status == VideoStatus.DONE
    assert rec.saved_bytes == 600
    assert db.total_saved_bytes() == 600
    assert db.count_by_status()["done"] == 1
    original, output, saved = db.compression_totals()
    assert original == 1000
    assert output == 400
    assert saved == 600
    assert db.sum_size_by_status()["done"] == 1000
    assert len(db.done_records()) == 1


def test_failed_records(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    db.upsert_discovered("/sdcard/a.mp4", 100, 1)
    db.mark_failed("/sdcard/a.mp4", "encode error")
    failed = db.failed_records()
    assert len(failed) == 1
    assert failed[0].last_error == "encode error"


def test_upsert_batch(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    progress: list[tuple[int, int]] = []
    n = db.upsert_discovered_batch(
        [
            ("/sdcard/a.mp4", 100, 1),
            ("/sdcard/b.mp4", 200, 2),
            ("/sdcard/c.mp4", 300, 3),
        ],
        on_progress=lambda d, t: progress.append((d, t)),
    )
    assert n == 3
    assert db.get("/sdcard/b.mp4") is not None
    assert db.get("/sdcard/b.mp4").size == 200  # type: ignore[union-attr]
    # Unchanged re-upsert should not reset done
    db.record_result(
        "/sdcard/a.mp4",
        out_size=40,
        saved_bytes=60,
        remote_output_path="/sdcard/a_hevc.mp4",
    )
    db.upsert_discovered_batch([("/sdcard/a.mp4", 100, 1)])
    assert db.get("/sdcard/a.mp4").status.value == "done"  # type: ignore[union-attr]
    # Changed size resets
    db.upsert_discovered_batch([("/sdcard/a.mp4", 999, 1)])
    assert db.get("/sdcard/a.mp4").status.value == "discovered"  # type: ignore[union-attr]
