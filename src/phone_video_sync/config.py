"""YAML configuration loading, defaults, validation, and tool discovery."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from phone_video_sync.models import Config

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml")


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


def _as_path(value: Any, default: Path) -> Path:
    if value is None or value == "":
        return default
    return Path(str(value))


def _as_optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _as_str_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list):
        raise ConfigError(f"Expected a list, got {type(value).__name__}")
    return [str(item).lstrip(".").lower() if isinstance(item, str) else str(item) for item in value]


def find_config_path(explicit: Path | None = None, start: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.is_file() else None
    root = start or Path.cwd()
    for name in DEFAULT_CONFIG_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def resolve_tool(name: str, override: str | None) -> str:
    if override:
        path = Path(override)
        if path.is_file():
            return str(path)
        found = shutil.which(override)
        if found:
            return found
        raise ConfigError(f"Configured {name} path not found: {override}")
    found = shutil.which(name)
    if not found:
        raise ConfigError(
            f"Required tool '{name}' not found on PATH. "
            f"Install it or set {name}_path in config.yaml."
        )
    return found


def load_raw(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return data


def config_from_mapping(data: dict[str, Any], project_root: Path | None = None) -> Config:
    root = Path(project_root or Path.cwd()).resolve()
    defaults = Config(project_root=root)

    delete_mode = str(data.get("delete_mode", defaults.delete_mode)).lower()
    if delete_mode not in {"archive", "delete"}:
        raise ConfigError("delete_mode must be 'archive' or 'delete'")

    encode_workers = int(data.get("encode_workers", defaults.encode_workers))
    if encode_workers < 1:
        raise ConfigError("encode_workers must be >= 1")

    max_attempts = int(data.get("max_attempts", defaults.max_attempts))
    if max_attempts < 1:
        raise ConfigError("max_attempts must be >= 1")

    cq = int(data.get("cq", defaults.cq))
    if cq < 0 or cq > 51:
        raise ConfigError("cq must be between 0 and 51")

    duration_tolerance = float(
        data.get("duration_tolerance_sec", defaults.duration_tolerance_sec)
    )
    if duration_tolerance < 0:
        raise ConfigError("duration_tolerance_sec must be >= 0")

    extensions = _as_str_list(data.get("extensions"), defaults.extensions)
    if not extensions:
        raise ConfigError("extensions must not be empty")

    skip_prefixes = data.get("skip_prefixes", defaults.skip_prefixes)
    if not isinstance(skip_prefixes, list):
        raise ConfigError("skip_prefixes must be a list")
    skip_prefixes = [str(p).lstrip("/") for p in skip_prefixes]

    db_path = _as_path(data.get("db_path"), defaults.db_path)
    work_dir = _as_path(data.get("work_dir"), defaults.work_dir)
    log_dir = _as_path(data.get("log_dir"), defaults.log_dir)
    if not db_path.is_absolute():
        db_path = root / db_path
    if not work_dir.is_absolute():
        work_dir = root / work_dir
    if not log_dir.is_absolute():
        log_dir = root / log_dir

    watch_interval = float(
        data.get("watch_interval_sec", defaults.watch_interval_sec)
    )
    if watch_interval < 1:
        raise ConfigError("watch_interval_sec must be >= 1")

    cfg = Config(
        db_path=db_path,
        work_dir=work_dir,
        log_dir=log_dir,
        remote_root=str(data.get("remote_root", defaults.remote_root)).rstrip("/") or "/sdcard",
        archive_root=str(data.get("archive_root", defaults.archive_root)).rstrip("/"),
        extensions=extensions,
        skip_prefixes=skip_prefixes,
        video_encoder=str(data.get("video_encoder", defaults.video_encoder)),
        preset=str(data.get("preset", defaults.preset)),
        cq=cq,
        audio_bitrate=str(data.get("audio_bitrate", defaults.audio_bitrate)),
        duration_tolerance_sec=duration_tolerance,
        require_smaller=bool(data.get("require_smaller", defaults.require_smaller)),
        encode_workers=encode_workers,
        max_attempts=max_attempts,
        retry_backoff_base_sec=float(
            data.get("retry_backoff_base_sec", defaults.retry_backoff_base_sec)
        ),
        subprocess_timeout_sec=int(
            data.get("subprocess_timeout_sec", defaults.subprocess_timeout_sec)
        ),
        delete_mode=delete_mode,
        adb_path=_as_optional_str(data.get("adb_path")),
        ffmpeg_path=_as_optional_str(data.get("ffmpeg_path")),
        ffprobe_path=_as_optional_str(data.get("ffprobe_path")),
        output_suffix=str(data.get("output_suffix", defaults.output_suffix)),
        watch_interval_sec=watch_interval,
        listing_cache_ttl_sec=float(
            data.get("listing_cache_ttl_sec", defaults.listing_cache_ttl_sec)
        ),
        project_root=root,
    )
    if cfg.listing_cache_ttl_sec < 0:
        raise ConfigError("listing_cache_ttl_sec must be >= 0")
    return cfg


def load_config(
    path: Path | None = None,
    *,
    project_root: Path | None = None,
    require_file: bool = False,
) -> Config:
    """Load config from YAML, or defaults if no file and require_file is False."""
    root = Path(project_root or Path.cwd()).resolve()
    config_path = find_config_path(Path(path) if path is not None else None, start=root)
    if config_path is None:
        if require_file and path is not None:
            raise ConfigError(f"Config file not found: {path}")
        if require_file:
            raise ConfigError("No config.yaml found in the project root")
        return Config(project_root=root)

    data = load_raw(config_path)
    return config_from_mapping(data, project_root=root)


def resolve_tools(cfg: Config) -> dict[str, str]:
    """Resolve adb/ffmpeg/ffprobe paths and return a mapping."""
    return {
        "adb": resolve_tool("adb", cfg.adb_path),
        "ffmpeg": resolve_tool("ffmpeg", cfg.ffmpeg_path),
        "ffprobe": resolve_tool("ffprobe", cfg.ffprobe_path),
    }


def ensure_runtime_dirs(cfg: Config) -> None:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.work_in.mkdir(parents=True, exist_ok=True)
    cfg.work_out.mkdir(parents=True, exist_ok=True)
    cfg.work_failed.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)


def validate_config(cfg: Config, *, check_tools: bool = True) -> list[str]:
    """Return a list of validation issues (empty = ok)."""
    issues: list[str] = []
    if cfg.delete_mode not in {"archive", "delete"}:
        issues.append("delete_mode must be 'archive' or 'delete'")
    if cfg.encode_workers < 1:
        issues.append("encode_workers must be >= 1")
    if cfg.max_attempts < 1:
        issues.append("max_attempts must be >= 1")
    if not cfg.extensions:
        issues.append("extensions must not be empty")
    if check_tools:
        try:
            resolve_tools(cfg)
        except ConfigError as exc:
            issues.append(str(exc))
    return issues
