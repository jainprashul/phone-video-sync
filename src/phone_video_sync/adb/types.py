"""ADB types: exceptions, device/file dataclasses, progress callback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    mime_type: str | None = None
    title: str | None = None
    resolution: str | None = None


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str
