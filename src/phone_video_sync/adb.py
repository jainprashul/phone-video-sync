"""Cross-platform ADB abstraction for phone video sync."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class AdbError(Exception):
    """ADB command failure."""


class DeviceError(AdbError):
    """No device, unauthorized, or multiple devices."""


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    mtime: int


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str


class AdbClient:
    def __init__(
        self,
        adb_path: str,
        *,
        serial: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.adb_path = adb_path
        self.serial = serial
        self.timeout = timeout

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
    ) -> subprocess.CompletedProcess[str]:
        cmd = self._base_cmd() + args
        logger.debug("ADB: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB timed out: {' '.join(cmd)}") from exc
        except FileNotFoundError as exc:
            raise AdbError(f"ADB binary not found: {self.adb_path}") from exc

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
    ) -> str:
        result = self.run(["shell", command], timeout=timeout, check=check)
        return result.stdout or ""

    def devices(self) -> list[DeviceInfo]:
        result = self.run(["devices"], check=True)
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
        """Resolve symlinks (e.g. /sdcard -> /storage/emulated/0) for find."""
        quoted = _shell_quote(remote_path)
        for cmd in (f"readlink -f {quoted}", f"realpath {quoted}"):
            result = self.run(["shell", cmd], check=False)
            resolved = (result.stdout or "").strip().splitlines()
            if result.returncode == 0 and resolved and resolved[0].startswith("/"):
                return resolved[0].strip()
        # Fallback: known Android primary storage
        if remote_path.rstrip("/") in {"/sdcard", "/storage/self/primary"}:
            return "/storage/emulated/0"
        return remote_path.rstrip("/") or remote_path

    def list_videos(
        self,
        remote_root: str,
        extensions: list[str],
        skip_prefixes: list[str],
        archive_root: str,
    ) -> list[RemoteFile]:
        """Recursively list video files under remote_root via adb shell find.

        Uses plain ``-name`` patterns (Android toybox often lacks ``-iname``)
        and batches ``stat`` via ``find -exec`` to avoid per-file round-trips.
        Resolves ``/sdcard`` symlinks because ``find`` often skips them.
        """
        root = self.resolve_path(remote_root)
        archive_norm = self.resolve_path(archive_root)
        ext_set = {ext.lstrip(".").lower() for ext in extensions}

        # Lines: SIZE MTIME PATH  (path may contain spaces)
        # Prune Android/ to avoid permission errors and huge app caches.
        raw_lines: list[str] = []
        for ext in sorted(ext_set):
            for pattern in (f"*.{ext}", f"*.{ext.upper()}"):
                find_cmd = (
                    f"find {root} "
                    f"\\( -path {root}/Android -o -path '{root}/Android/*' \\) -prune -o "
                    f"-type f -name '{pattern}' "
                    f"-exec stat -c '%s %Y %n' {{}} \\; "
                    f"2>/dev/null"
                )
                try:
                    # find often exits non-zero on permission denials; keep stdout
                    stdout = self.shell(
                        find_cmd,
                        timeout=max(self.timeout, 900),
                        check=False,
                    )
                except AdbError as exc:
                    logger.warning("find failed for %s: %s", pattern, exc)
                    continue
                raw_lines.extend(stdout.splitlines())

        skip = [p.lstrip("/") for p in skip_prefixes]
        files: list[RemoteFile] = []
        seen: set[str] = set()

        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            match = re.match(r"^(\d+)\s+(\d+)\s+(.+)$", line)
            if not match:
                continue
            size = int(match.group(1))
            mtime = int(match.group(2))
            path = match.group(3).strip().replace("\\", "/")
            if not path.startswith("/"):
                path = "/" + path
            if path in seen:
                continue
            seen.add(path)

            lower = path.lower()
            if not any(lower.endswith(f".{ext}") for ext in ext_set):
                continue

            rel = path[len(root) :].lstrip("/") if path.startswith(root) else path.lstrip("/")
            if any(rel.startswith(prefix) or f"/{prefix}" in f"/{rel}" for prefix in skip):
                continue
            if path.startswith(archive_norm + "/") or path == archive_norm:
                continue
            if "/Android/" in path or path.endswith("/Android"):
                continue
            if size <= 0:
                continue

            files.append(RemoteFile(path=path, size=size, mtime=mtime))

        files.sort(key=lambda f: f.path)
        return files

    def _stat_file(self, remote_path: str) -> tuple[int, int]:
        # BusyBox/toybox: stat -c '%s %Y'
        quoted = _shell_quote(remote_path)
        out = self.shell(f"stat -c '%s %Y' {quoted}").strip()
        match = re.match(r"^(\d+)\s+(\d+)$", out)
        if not match:
            # Fallback: ls -l parsing is fragile; try toybox stat alternate
            out2 = self.shell(f"ls -ln {quoted}").strip()
            raise AdbError(f"Could not stat {remote_path}: {out or out2}")
        return int(match.group(1)), int(match.group(2))

    def pull(self, remote_path: str, local_path: Path, *, timeout: int | None = None) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            local_path.unlink()
        self.run(
            ["pull", remote_path, str(local_path)],
            timeout=timeout or max(self.timeout, 600),
        )
        if not local_path.is_file():
            raise AdbError(f"Pull did not create local file: {local_path}")

    def push(self, local_path: Path, remote_path: str, *, timeout: int | None = None) -> None:
        if not local_path.is_file():
            raise AdbError(f"Local file missing for push: {local_path}")
        remote_dir = remote_path.rsplit("/", 1)[0]
        self.shell(f"mkdir -p {_shell_quote(remote_dir)}")
        self.run(
            ["push", str(local_path), remote_path],
            timeout=timeout or max(self.timeout, 600),
        )

    def move_to_archive(self, remote_path: str, archive_root: str, remote_root: str) -> str:
        root = self.resolve_path(remote_root)
        archive_root = self.resolve_path(archive_root)
        if remote_path.startswith(root + "/"):
            rel = remote_path[len(root) + 1 :]
        elif remote_path.startswith("/sdcard/"):
            rel = remote_path[len("/sdcard/") :]
        else:
            rel = remote_path.lstrip("/")
        dest = f"{archive_root.rstrip('/')}/{rel}"
        dest_dir = dest.rsplit("/", 1)[0]
        self.shell(f"mkdir -p {_shell_quote(dest_dir)}")
        self.shell(f"mv {_shell_quote(remote_path)} {_shell_quote(dest)}")
        return dest

    def delete_remote(self, remote_path: str) -> None:
        self.shell(f"rm -f {_shell_quote(remote_path)}")

    def set_remote_mtime(self, remote_path: str, mtime: int) -> None:
        # touch -d @epoch works on many Android shells; fallback to touch -t
        quoted = _shell_quote(remote_path)
        result = self.run(
            ["shell", f"touch -d @{mtime} {quoted}"],
            check=False,
        )
        if result.returncode != 0:
            # busybox touch -t YYYYMMDDhhmm.ss
            dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            stamp = dt.strftime("%Y%m%d%H%M.%S")
            self.shell(f"touch -t {stamp} {quoted}")

    def remote_exists(self, remote_path: str) -> bool:
        result = self.run(
            ["shell", f"test -e {_shell_quote(remote_path)} && echo YES || echo NO"],
            check=False,
        )
        return "YES" in (result.stdout or "")

    def purge_archive(self, archive_root: str) -> None:
        root = self.resolve_path(archive_root).rstrip("/")
        if not root or root in {
            "/",
            "/sdcard",
            "/storage",
            "/storage/emulated/0",
            "/storage/self/primary",
        }:
            raise AdbError(f"Refusing to purge unsafe archive_root: {archive_root}")
        self.shell(f"rm -rf {_shell_quote(root)}")


def _shell_quote(path: str) -> str:
    """Single-quote a path for adb shell (POSIX)."""
    return "'" + path.replace("'", "'\\''") + "'"
