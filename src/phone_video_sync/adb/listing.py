"""Video discovery via adb find and MediaStore enrichment."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from phone_video_sync.adb.media import parse_mediastore_row
from phone_video_sync.adb.shell import normalize_storage_path, shell_quote, to_int
from phone_video_sync.adb.types import RemoteFile

if TYPE_CHECKING:
    from phone_video_sync.adb.client import AdbClient


def resolve_path(client: AdbClient, remote_path: str) -> str:
    """Resolve symlinks (e.g. /sdcard -> /storage/emulated/0) for find."""
    quoted = shell_quote(remote_path)
    for cmd in (f"readlink -f {quoted}", f"realpath {quoted}"):
        result = client.run(["shell", cmd], check=False)
        resolved = (result.stdout or "").strip().splitlines()
        if result.returncode == 0 and resolved and resolved[0].startswith("/"):
            return resolved[0].strip()
    if remote_path.rstrip("/") in {"/sdcard", "/storage/self/primary"}:
        return "/storage/emulated/0"
    return remote_path.rstrip("/") or remote_path


def query_mediastore_videos(client: AdbClient) -> dict[str, dict]:
    """Query content://media/external/video/media once; keyed by normalized path."""
    projection = (
        "_data:mime_type:width:height:duration:_size:date_modified:"
        "resolution:title:_display_name"
    )
    uris = [
        "content://media/external/video/media",
        "content://media/external/file",
    ]
    by_path: dict[str, dict] = {}
    for uri in uris:
        cmd = f'content query --uri {uri} --projection "{projection}"'
        if "file" in uri:
            cmd = (
                'content query --uri content://media/external/file '
                '--projection "_data:mime_type:_size:date_modified:_display_name" '
                '--where "mime_type LIKE \'video/%\'"'
            )
        result = client.run(
            ["shell", cmd],
            timeout=max(client.timeout, 180),
            check=False,
            label=f"MediaStore {uri.split('/')[-1]}…",
        )
        for line in (result.stdout or "").splitlines():
            fields = parse_mediastore_row(line)
            path = fields.get("_data") or fields.get("_display_name")
            if not path or not path.startswith("/"):
                continue
            width = to_int(fields.get("width"))
            height = to_int(fields.get("height"))
            entry = {
                "width": width,
                "height": height,
                "duration_ms": to_int(fields.get("duration")),
                "size": to_int(fields.get("_size")),
                "mtime": to_int(fields.get("date_modified")),
                "mime_type": fields.get("mime_type"),
                "title": fields.get("title") or fields.get("_display_name"),
                "resolution": fields.get("resolution"),
            }
            by_path[normalize_storage_path(path)] = entry
            by_path[path] = entry
        if by_path and "video/media" in uri:
            break
    return by_path


def enrich_with_mediastore(client: AdbClient, files: list[RemoteFile]) -> list[RemoteFile]:
    """Merge Android MediaStore width/height/duration/mime onto find results."""
    if not files:
        return files
    client._notify("Loading MediaStore video metadata…")
    store = query_mediastore_videos(client)
    if not store:
        client._notify("MediaStore returned no rows — resolution/duration may be unknown.")
        return files

    enriched: list[RemoteFile] = []
    hits = 0
    for rf in files:
        key = normalize_storage_path(rf.path)
        meta = store.get(key) or store.get(rf.path)
        if meta is None:
            enriched.append(rf)
            continue
        hits += 1
        enriched.append(
            RemoteFile(
                path=rf.path,
                size=rf.size or meta.get("size") or 0,
                mtime=rf.mtime or meta.get("mtime") or 0,
                width=meta.get("width"),
                height=meta.get("height"),
                duration_ms=meta.get("duration_ms"),
                mime_type=meta.get("mime_type"),
                title=meta.get("title"),
                resolution=meta.get("resolution"),
            )
        )
    client._notify(f"MediaStore matched {hits}/{len(files)} file(s).")
    return enriched


def list_videos(
    client: AdbClient,
    remote_root: str,
    extensions: list[str],
    skip_prefixes: list[str],
    archive_root: str,
) -> list[RemoteFile]:
    """Recursively list video files under remote_root via a single adb find."""
    client._notify("Resolving device storage path…")
    root = resolve_path(client, remote_root)
    archive_norm = resolve_path(client, archive_root)
    ext_set = {ext.lstrip(".").lower() for ext in extensions}

    name_clauses: list[str] = []
    for ext in sorted(ext_set):
        name_clauses.append(f"-name '*.{ext}'")
        name_clauses.append(f"-name '*.{ext.upper()}'")
    name_expr = " -o ".join(name_clauses)

    find_cmd = (
        f"find {root} "
        f"\\( -path {root}/Android -o -path '{root}/Android/*' \\) -prune -o "
        f"-type f \\( {name_expr} \\) "
        f"-printf '%s %T@ %p\\n' "
        f"2>/dev/null"
    )

    client._notify(
        f"Scanning {root} for videos (one pass — can take 1–5+ min on large storage)…"
    )
    stdout = client.shell(
        find_cmd,
        timeout=max(client.timeout, 900),
        check=False,
        label=f"adb find under {root}",
    )

    if not stdout.strip():
        client._notify("Retrying scan with find -exec stat (slower fallback)…")
        find_cmd = (
            f"find {root} "
            f"\\( -path {root}/Android -o -path '{root}/Android/*' \\) -prune -o "
            f"-type f \\( {name_expr} \\) "
            f"-exec stat -c '%s %Y %n' {{}} \\; "
            f"2>/dev/null"
        )
        stdout = client.shell(
            find_cmd,
            timeout=max(client.timeout, 900),
            check=False,
            label=f"adb find+stat under {root}",
        )

    client._notify("Parsing scan results…")
    skip = [p.lstrip("/") for p in skip_prefixes]
    files: list[RemoteFile] = []
    seen: set[str] = set()

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
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
    client._notify(f"Scan complete — {len(files)} video file(s) found.")
    return enrich_with_mediastore(client, files)
