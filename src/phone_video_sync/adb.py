"""Cross-platform ADB abstraction for phone video sync."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


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
            # First tick after 5s so short commands stay quiet
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
                # Let ADB print transfer progress to the terminal (stderr).
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
        """Recursively list video files under remote_root via a single adb find.

        One filesystem walk (not per-extension) with a Rich/heartbeat progress
        so the CLI does not look frozen during long scans.
        """
        self._notify("Resolving device storage path…")
        root = self.resolve_path(remote_root)
        archive_norm = self.resolve_path(archive_root)
        ext_set = {ext.lstrip(".").lower() for ext in extensions}

        name_clauses: list[str] = []
        for ext in sorted(ext_set):
            name_clauses.append(f"-name '*.{ext}'")
            name_clauses.append(f"-name '*.{ext.upper()}'")
        name_expr = " -o ".join(name_clauses)

        # Single walk of the tree — previously we ran ~12 separate finds.
        find_cmd = (
            f"find {root} "
            f"\\( -path {root}/Android -o -path '{root}/Android/*' \\) -prune -o "
            f"-type f \\( {name_expr} \\) "
            f"-printf '%s %T@ %p\\n' "
            f"2>/dev/null"
        )

        self._notify(
            f"Scanning {root} for videos (one pass — can take 1–5+ min on large storage)…"
        )
        stdout = self.shell(
            find_cmd,
            timeout=max(self.timeout, 900),
            check=False,
            label=f"adb find under {root}",
        )

        # Fallback if -printf is unsupported (rare on modern Android toybox)
        if not stdout.strip():
            self._notify("Retrying scan with find -exec stat (slower fallback)…")
            find_cmd = (
                f"find {root} "
                f"\\( -path {root}/Android -o -path '{root}/Android/*' \\) -prune -o "
                f"-type f \\( {name_expr} \\) "
                f"-exec stat -c '%s %Y %n' {{}} \\; "
                f"2>/dev/null"
            )
            stdout = self.shell(
                find_cmd,
                timeout=max(self.timeout, 900),
                check=False,
                label=f"adb find+stat under {root}",
            )

        self._notify("Parsing scan results…")
        skip = [p.lstrip("/") for p in skip_prefixes]
        files: list[RemoteFile] = []
        seen: set[str] = set()

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # %T@ may be float; accept int or float mtime
            match = re.match(r"^(\d+)\s+(\d+(?:\.\d+)?)\s+(.+)$", line)
            if not match:
                continue
            size = int(match.group(1))
            mtime = int(float(match.group(2)))
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
        self._notify(f"Scan complete — {len(files)} video file(s) found.")
        return files

    def pull(self, remote_path: str, local_path: Path, *, timeout: int | None = None) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            local_path.unlink()
        name = Path(remote_path).name
        self.run(
            ["pull", remote_path, str(local_path)],
            timeout=timeout or max(self.timeout, 600),
            stream_stderr=True,
            label=f"Pulling {name}…",
        )
        if not local_path.is_file():
            raise AdbError(f"Pull did not create local file: {local_path}")

    def push(self, local_path: Path, remote_path: str, *, timeout: int | None = None) -> None:
        if not local_path.is_file():
            raise AdbError(f"Local file missing for push: {local_path}")
        remote_dir = remote_path.rsplit("/", 1)[0]
        self.shell(f"mkdir -p {_shell_quote(remote_dir)}", label=None)
        name = Path(remote_path).name
        self.run(
            ["push", str(local_path), remote_path],
            timeout=timeout or max(self.timeout, 600),
            stream_stderr=True,
            label=f"Pushing {name}…",
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
        self.shell(
            f"mv {_shell_quote(remote_path)} {_shell_quote(dest)}",
            label=f"Archiving {Path(remote_path).name}…",
        )
        return dest

    def delete_remote(self, remote_path: str) -> None:
        self.shell(
            f"rm -f {_shell_quote(remote_path)}",
            label=f"Deleting {Path(remote_path).name}…",
        )

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
        self.shell(
            f"rm -rf {_shell_quote(root)}",
            label=f"Purging archive {root}…",
        )


def _shell_quote(path: str) -> str:
    """Single-quote a path for adb shell (POSIX)."""
    return "'" + path.replace("'", "'\\''") + "'"
