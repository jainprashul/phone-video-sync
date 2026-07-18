"""SQLite tracking: schema, status state machine, upsert/query, resume."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from phone_video_sync.models import (
    IN_PROGRESS_STATUSES,
    PENDING_STATUSES,
    VideoRecord,
    VideoStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    remote_path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    local_path TEXT,
    output_path TEXT,
    remote_output_path TEXT,
    src_duration REAL,
    src_width INTEGER,
    src_height INTEGER,
    out_duration REAL,
    out_width INTEGER,
    out_height INTEGER,
    out_size INTEGER,
    saved_bytes INTEGER,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> VideoRecord:
    return VideoRecord(
        remote_path=row["remote_path"],
        size=row["size"],
        mtime=row["mtime"],
        status=VideoStatus(row["status"]),
        attempts=row["attempts"],
        last_error=row["last_error"],
        local_path=row["local_path"],
        output_path=row["output_path"],
        remote_output_path=row["remote_output_path"],
        src_duration=row["src_duration"],
        src_width=row["src_width"],
        src_height=row["src_height"],
        out_duration=row["out_duration"],
        out_width=row["out_width"],
        out_height=row["out_height"],
        out_size=row["out_size"],
        saved_bytes=row["saved_bytes"],
        discovered_at=row["discovered_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    def get(self, remote_path: str) -> VideoRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE remote_path = ?",
                (remote_path,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def upsert_discovered(self, remote_path: str, size: int, mtime: int) -> VideoRecord:
        """Insert or refresh a discovered video; reset done if size/mtime changed."""
        now = _utc_now()
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM videos WHERE remote_path = ?",
                (remote_path,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO videos (
                        remote_path, size, mtime, status, attempts,
                        discovered_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (remote_path, size, mtime, VideoStatus.DISCOVERED.value, now, now),
                )
            else:
                changed = existing["size"] != size or existing["mtime"] != mtime
                if changed:
                    conn.execute(
                        """
                        UPDATE videos SET
                            size = ?, mtime = ?, status = ?, attempts = 0,
                            last_error = NULL, local_path = NULL, output_path = NULL,
                            remote_output_path = NULL, src_duration = NULL,
                            src_width = NULL, src_height = NULL,
                            out_duration = NULL, out_width = NULL, out_height = NULL,
                            out_size = NULL, saved_bytes = NULL,
                            completed_at = NULL, updated_at = ?
                        WHERE remote_path = ?
                        """,
                        (size, mtime, VideoStatus.DISCOVERED.value, now, remote_path),
                    )
                else:
                    conn.execute(
                        "UPDATE videos SET updated_at = ? WHERE remote_path = ?",
                        (now, remote_path),
                    )
            row = conn.execute(
                "SELECT * FROM videos WHERE remote_path = ?",
                (remote_path,),
            ).fetchone()
        assert row is not None
        return _row_to_record(row)

    def set_status(
        self,
        remote_path: str,
        status: VideoStatus,
        *,
        error: str | None = None,
        increment_attempts: bool = False,
        **fields: Any,
    ) -> None:
        now = _utc_now()
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status.value, now]

        if error is not None:
            assignments.append("last_error = ?")
            values.append(error)
        elif status != VideoStatus.FAILED:
            assignments.append("last_error = NULL")

        if increment_attempts:
            assignments.append("attempts = attempts + 1")

        if status == VideoStatus.DONE:
            assignments.append("completed_at = ?")
            values.append(now)

        allowed = {
            "local_path",
            "output_path",
            "remote_output_path",
            "src_duration",
            "src_width",
            "src_height",
            "out_duration",
            "out_width",
            "out_height",
            "out_size",
            "saved_bytes",
        }
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unsupported field: {key}")
            assignments.append(f"{key} = ?")
            values.append(value)

        values.append(remote_path)
        sql = f"UPDATE videos SET {', '.join(assignments)} WHERE remote_path = ?"
        with self.connection() as conn:
            conn.execute(sql, values)

    def record_result(
        self,
        remote_path: str,
        *,
        out_size: int,
        saved_bytes: int,
        remote_output_path: str,
        out_duration: float | None = None,
        out_width: int | None = None,
        out_height: int | None = None,
    ) -> None:
        self.set_status(
            remote_path,
            VideoStatus.DONE,
            out_size=out_size,
            saved_bytes=saved_bytes,
            remote_output_path=remote_output_path,
            out_duration=out_duration,
            out_width=out_width,
            out_height=out_height,
        )

    def mark_failed(self, remote_path: str, error: str) -> None:
        self.set_status(
            remote_path,
            VideoStatus.FAILED,
            error=error,
            increment_attempts=True,
        )

    def pending_work(self, max_attempts: int) -> list[VideoRecord]:
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        statuses = [s.value for s in PENDING_STATUSES]
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM videos
                WHERE status IN ({placeholders})
                  AND (status != ? OR attempts < ?)
                ORDER BY discovered_at ASC
                """,
                (*statuses, VideoStatus.FAILED.value, max_attempts),
            ).fetchall()
        # Failed with attempts >= max are excluded by the OR clause above only when status is failed
        # Re-filter carefully: include discovered/in-progress always; failed only if attempts < max
        result: list[VideoRecord] = []
        for row in rows:
            rec = _row_to_record(row)
            if rec.status == VideoStatus.FAILED and rec.attempts >= max_attempts:
                continue
            if rec.status == VideoStatus.DONE:
                continue
            result.append(rec)
        return result

    def reconcile_on_start(self) -> int:
        """Reset stuck in-progress rows to discovered so an interrupted run resumes."""
        now = _utc_now()
        statuses = [s.value for s in IN_PROGRESS_STATUSES]
        placeholders = ",".join("?" for _ in statuses)
        with self.connection() as conn:
            cur = conn.execute(
                f"""
                UPDATE videos
                SET status = ?, last_error = COALESCE(last_error, 'interrupted'),
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (VideoStatus.DISCOVERED.value, now, *statuses),
            )
            return cur.rowcount

    def count_by_status(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM videos GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def list_all(self) -> list[VideoRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM videos ORDER BY remote_path"
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def total_saved_bytes(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(saved_bytes), 0) AS total FROM videos WHERE status = ?",
                (VideoStatus.DONE.value,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def sum_size_by_status(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT status, COALESCE(SUM(size), 0) AS total FROM videos GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def done_records(self) -> list[VideoRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE status = ? ORDER BY completed_at DESC",
                (VideoStatus.DONE.value,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def failed_records(self) -> list[VideoRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE status = ? ORDER BY updated_at DESC",
                (VideoStatus.FAILED.value,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def compression_totals(self) -> tuple[int, int, int]:
        """Return (original_bytes, output_bytes, saved_bytes) for done videos."""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(size), 0) AS original,
                    COALESCE(SUM(out_size), 0) AS output,
                    COALESCE(SUM(saved_bytes), 0) AS saved
                FROM videos
                WHERE status = ?
                """,
                (VideoStatus.DONE.value,),
            ).fetchone()
        if not row:
            return 0, 0, 0
        return int(row["original"]), int(row["output"]), int(row["saved"])
