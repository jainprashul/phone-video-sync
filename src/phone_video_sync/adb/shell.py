"""Shell quoting and path normalization for ADB commands."""


def shell_quote(path: str) -> str:
    """Single-quote a path for adb shell (POSIX)."""
    return "'" + path.replace("'", "'\\''") + "'"


def to_int(value: str | None) -> int | None:
    if value is None or value == "" or value == "null":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_storage_path(path: str) -> str:
    """Map emulated/primary storage paths to a canonical /sdcard prefix."""
    p = path.replace("\\", "/").strip()
    for prefix in ("/storage/emulated/0", "/storage/self/primary", "/sdcard"):
        if p == prefix or p.startswith(prefix + "/"):
            return "/sdcard" + p[len(prefix) :] if p != prefix else "/sdcard"
    return p
