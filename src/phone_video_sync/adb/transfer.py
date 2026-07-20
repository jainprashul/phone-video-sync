"""File transfer, streaming, and remote filesystem operations via ADB."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from phone_video_sync.adb.listing import resolve_path
from phone_video_sync.adb.shell import shell_quote
from phone_video_sync.adb.types import AdbError

if TYPE_CHECKING:
    from phone_video_sync.adb.client import AdbClient


def remote_file_size(client: AdbClient, remote_path: str) -> int:
    quoted = shell_quote(remote_path)
    out = client.shell(f"stat -c '%s' {quoted}", check=True).strip()
    try:
        return int(out.splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise AdbError(f"Could not read size for {remote_path}: {out!r}") from exc


def stream_byte_range(
    client: AdbClient,
    remote_path: str,
    *,
    offset: int,
    length: int,
) -> bytes:
    """Read [offset, offset+length) from a remote file via adb exec-out dd."""
    if length <= 0:
        return b""
    quoted = shell_quote(remote_path)
    block = 4096
    skip = offset // block
    lead = offset % block
    count = (lead + length + block - 1) // block
    cmd = client._base_cmd() + [
        "exec-out",
        f"dd if={quoted} bs={block} skip={skip} count={count} 2>/dev/null",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=max(client.timeout, 180),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"Range read timed out: {remote_path}") from exc
    data = result.stdout or b""
    if not data:
        raise AdbError(f"Empty range read for {remote_path} @ {offset}+{length}")
    return data[lead : lead + length]


def stream_header_bytes(
    client: AdbClient, remote_path: str, *, max_bytes: int = 4_194_304
) -> bytes:
    """Read the first N bytes of a remote file (for ffprobe)."""
    return stream_byte_range(client, remote_path, offset=0, length=max_bytes)


def stream_head_and_tail(
    client: AdbClient,
    remote_path: str,
    *,
    head_bytes: int = 2 * 1024 * 1024,
    tail_bytes: int = 12 * 1024 * 1024,
) -> tuple[bytes, bytes, int]:
    """Return (head, tail, total_size). Tail is empty if the file fits in head+tail."""
    size = remote_file_size(client, remote_path)
    if size <= 0:
        raise AdbError(f"Remote file empty: {remote_path}")
    if size <= head_bytes + tail_bytes:
        whole = stream_byte_range(client, remote_path, offset=0, length=size)
        return whole, b"", size
    head = stream_byte_range(client, remote_path, offset=0, length=head_bytes)
    tail = stream_byte_range(
        client, remote_path, offset=size - tail_bytes, length=tail_bytes
    )
    return head, tail, size


def pull(
    client: AdbClient, remote_path: str, local_path: Path, *, timeout: int | None = None
) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        local_path.unlink()
    name = Path(remote_path).name
    client.run(
        ["pull", remote_path, str(local_path)],
        timeout=timeout or max(client.timeout, 600),
        stream_stderr=True,
        label=f"Pulling {name}…",
    )
    if not local_path.is_file():
        raise AdbError(f"Pull did not create local file: {local_path}")


def push(
    client: AdbClient,
    local_path: Path,
    remote_path: str,
    *,
    timeout: int | None = None,
) -> None:
    if not local_path.is_file():
        raise AdbError(f"Local file missing for push: {local_path}")
    remote_dir = remote_path.rsplit("/", 1)[0]
    client.shell(f"mkdir -p {shell_quote(remote_dir)}", label=None)
    name = Path(remote_path).name
    client.run(
        ["push", str(local_path), remote_path],
        timeout=timeout or max(client.timeout, 600),
        stream_stderr=True,
        label=f"Pushing {name}…",
    )


def move_to_archive(
    client: AdbClient,
    remote_path: str,
    archive_root: str,
    remote_root: str,
) -> str:
    root = resolve_path(client, remote_root)
    archive = resolve_path(client, archive_root)
    if remote_path.startswith(root + "/"):
        rel = remote_path[len(root) + 1 :]
    elif remote_path.startswith("/sdcard/"):
        rel = remote_path[len("/sdcard/") :]
    else:
        rel = remote_path.lstrip("/")
    dest = f"{archive.rstrip('/')}/{rel}"
    dest_dir = dest.rsplit("/", 1)[0]
    client.shell(f"mkdir -p {shell_quote(dest_dir)}")
    client.shell(
        f"mv {shell_quote(remote_path)} {shell_quote(dest)}",
        label=f"Archiving {Path(remote_path).name}…",
    )
    return dest


def delete_remote(client: AdbClient, remote_path: str) -> None:
    client.shell(
        f"rm -f {shell_quote(remote_path)}",
        label=f"Deleting {Path(remote_path).name}…",
    )


def set_remote_mtime(client: AdbClient, remote_path: str, mtime: int) -> None:
    quoted = shell_quote(remote_path)
    result = client.run(["shell", f"touch -d @{mtime} {quoted}"], check=False)
    if result.returncode != 0:
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        stamp = dt.strftime("%Y%m%d%H%M.%S")
        client.shell(f"touch -t {stamp} {quoted}")


def remote_exists(client: AdbClient, remote_path: str) -> bool:
    result = client.run(
        ["shell", f"test -e {shell_quote(remote_path)} && echo YES || echo NO"],
        check=False,
    )
    return "YES" in (result.stdout or "")


def purge_archive(client: AdbClient, archive_root: str) -> None:
    root = resolve_path(client, archive_root).rstrip("/")
    if not root or root in {
        "/",
        "/sdcard",
        "/storage",
        "/storage/emulated/0",
        "/storage/self/primary",
    }:
        raise AdbError(f"Refusing to purge unsafe archive_root: {archive_root}")
    client.shell(
        f"rm -rf {shell_quote(root)}",
        label=f"Purging archive {root}…",
    )
