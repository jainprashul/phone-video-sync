"""ADB client core: command execution, device selection, progress heartbeats."""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from pathlib import Path

from phone_video_sync.adb.listing import (
    enrich_with_mediastore,
    list_videos,
    query_mediastore_videos,
    resolve_path,
)
from phone_video_sync.adb.transfer import (
    delete_remote,
    move_to_archive,
    pull,
    push,
    purge_archive,
    remote_exists,
    remote_file_size,
    set_remote_mtime,
    stream_byte_range,
    stream_head_and_tail,
    stream_header_bytes,
)
from phone_video_sync.adb.types import AdbError, DeviceError, DeviceInfo, ProgressFn, RemoteFile

logger = logging.getLogger(__name__)


class AdbClient:
    """Cross-platform ADB wrapper for phone video sync."""

    def __init__(
        self,
        adb_path: str,
        *,
        serial: str | None = None,
        timeout: int = 120,
        on_progress: ProgressFn | None = None,
    ) -> None:
        self.adb_path = adb_path
        self.serial = serial
        self.timeout = timeout
        self.on_progress = on_progress

    def _notify(self, message: str) -> None:
        if self.on_progress:
            logger.debug("%s", message)
            try:
                self.on_progress(message)
            except Exception:  # noqa: BLE001 — never break ADB on UI callback
                logger.debug("progress callback failed", exc_info=True)
        else:
            logger.info("%s", message)

    def _base_cmd(self) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd.extend(["-s", self.serial])
        return cmd

    def run(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        check: bool = True,
        stream_stderr: bool = False,
        label: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = self._base_cmd() + args
        logger.debug("ADB: %s", " ".join(cmd))
        if label:
            self._notify(label)

        wait = timeout or self.timeout
        stop_heartbeat = threading.Event()

        def _heartbeat() -> None:
            started = time.monotonic()
            if not stop_heartbeat.wait(5.0):
                while not stop_heartbeat.wait(10.0):
                    elapsed = int(time.monotonic() - started)
                    self._notify(
                        f"Still working… {label or args[0]} ({elapsed}s elapsed)"
                    )

        heart: threading.Thread | None = None
        if wait >= 30:
            heart = threading.Thread(target=_heartbeat, daemon=True)
            heart.start()

        try:
            if stream_stderr:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    timeout=wait,
                    check=False,
                )
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=wait,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB timed out after {wait}s: {' '.join(cmd)}") from exc
        except FileNotFoundError as exc:
            raise AdbError(f"ADB binary not found: {self.adb_path}") from exc
        finally:
            stop_heartbeat.set()
            if heart is not None:
                heart.join(timeout=0.2)

        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise AdbError(f"ADB failed ({result.returncode}): {detail}")
        return result

    def shell(
        self,
        command: str,
        *,
        timeout: int | None = None,
        check: bool = True,
        label: str | None = None,
    ) -> str:
        result = self.run(
            ["shell", command],
            timeout=timeout,
            check=check,
            label=label,
        )
        return result.stdout or ""

    def devices(self) -> list[DeviceInfo]:
        result = self.run(["devices"], check=True, label="Checking ADB devices…")
        devices: list[DeviceInfo] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                devices.append(DeviceInfo(serial=parts[0], state=parts[1]))
        return devices

    def get_device(self) -> DeviceInfo:
        """Return the single authorized device or raise DeviceError."""
        all_devices = self.devices()
        if not all_devices:
            raise DeviceError(
                "No ADB devices found. Plug in the phone and enable USB debugging."
            )

        unauthorized = [d for d in all_devices if d.state == "unauthorized"]
        offline = [d for d in all_devices if d.state == "offline"]
        ready = [d for d in all_devices if d.state == "device"]

        if not ready:
            if unauthorized:
                serials = ", ".join(d.serial for d in unauthorized)
                raise DeviceError(
                    f"Device unauthorized ({serials}). "
                    "Tap 'Allow USB debugging' on the phone, then retry."
                )
            if offline:
                raise DeviceError("Device is offline. Replug USB and retry.")
            states = ", ".join(f"{d.serial}:{d.state}" for d in all_devices)
            raise DeviceError(f"No usable device. States: {states}")

        if self.serial:
            match = next((d for d in ready if d.serial == self.serial), None)
            if match is None:
                raise DeviceError(f"Configured serial not ready: {self.serial}")
            return match

        if len(ready) > 1:
            serials = ", ".join(d.serial for d in ready)
            raise DeviceError(
                f"Multiple devices connected ({serials}). "
                "Disconnect extras or pass a serial."
            )
        return ready[0]

    def resolve_path(self, remote_path: str) -> str:
        return resolve_path(self, remote_path)

    def list_videos(
        self,
        remote_root: str,
        extensions: list[str],
        skip_prefixes: list[str],
        archive_root: str,
    ) -> list[RemoteFile]:
        return list_videos(self, remote_root, extensions, skip_prefixes, archive_root)

    def enrich_with_mediastore(self, files: list[RemoteFile]) -> list[RemoteFile]:
        return enrich_with_mediastore(self, files)

    def query_mediastore_videos(self) -> dict[str, dict]:
        return query_mediastore_videos(self)

    def remote_file_size(self, remote_path: str) -> int:
        return remote_file_size(self, remote_path)

    def stream_byte_range(self, remote_path: str, *, offset: int, length: int) -> bytes:
        return stream_byte_range(self, remote_path, offset=offset, length=length)

    def stream_header_bytes(self, remote_path: str, *, max_bytes: int = 4_194_304) -> bytes:
        return stream_header_bytes(self, remote_path, max_bytes=max_bytes)

    def stream_head_and_tail(
        self,
        remote_path: str,
        *,
        head_bytes: int = 2 * 1024 * 1024,
        tail_bytes: int = 12 * 1024 * 1024,
    ) -> tuple[bytes, bytes, int]:
        return stream_head_and_tail(
            self, remote_path, head_bytes=head_bytes, tail_bytes=tail_bytes
        )

    def pull(
        self, remote_path: str, local_path: Path, *, timeout: int | None = None
    ) -> None:
        return pull(self, remote_path, local_path, timeout=timeout)

    def push(
        self, local_path: Path, remote_path: str, *, timeout: int | None = None
    ) -> None:
        return push(self, local_path, remote_path, timeout=timeout)

    def move_to_archive(
        self, remote_path: str, archive_root: str, remote_root: str
    ) -> str:
        return move_to_archive(self, remote_path, archive_root, remote_root)

    def delete_remote(self, remote_path: str) -> None:
        return delete_remote(self, remote_path)

    def set_remote_mtime(self, remote_path: str, mtime: int) -> None:
        return set_remote_mtime(self, remote_path, mtime)

    def remote_exists(self, remote_path: str) -> bool:
        return remote_exists(self, remote_path)

    def purge_archive(self, archive_root: str) -> None:
        return purge_archive(self, archive_root)
