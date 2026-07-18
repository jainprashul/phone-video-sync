"""Unit tests for YAML config loading and validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from phone_video_sync.config import (
    ConfigError,
    config_from_mapping,
    load_config,
    resolve_tool,
    validate_config,
)


def test_defaults_without_file(tmp_path: Path) -> None:
    cfg = load_config(project_root=tmp_path)
    assert cfg.video_encoder == "hevc_nvenc"
    assert cfg.delete_mode == "archive"
    assert cfg.encode_workers == 2
    assert cfg.watch_interval_sec == 5.0
    assert "mp4" in cfg.extensions


def test_watch_interval_validation() -> None:
    with pytest.raises(ConfigError, match="watch_interval_sec"):
        config_from_mapping({"watch_interval_sec": 0.5})


def test_load_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.dump(
            {
                "cq": 30,
                "encode_workers": 4,
                "delete_mode": "delete",
                "extensions": [".MP4", "mov"],
                "db_path": "data/custom.db",
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path, project_root=tmp_path)
    assert cfg.cq == 30
    assert cfg.encode_workers == 4
    assert cfg.delete_mode == "delete"
    assert cfg.extensions == ["mp4", "mov"]
    assert cfg.db_path == (tmp_path / "data" / "custom.db").resolve()


def test_invalid_delete_mode() -> None:
    with pytest.raises(ConfigError, match="delete_mode"):
        config_from_mapping({"delete_mode": "shred"})


def test_invalid_cq() -> None:
    with pytest.raises(ConfigError, match="cq"):
        config_from_mapping({"cq": 99})


def test_validate_workers() -> None:
    with pytest.raises(ConfigError, match="encode_workers"):
        config_from_mapping({"encode_workers": 0})


def test_resolve_tool_override(tmp_path: Path) -> None:
    fake = tmp_path / "adb.exe"
    fake.write_text("", encoding="utf-8")
    assert resolve_tool("adb", str(fake)) == str(fake)


def test_resolve_tool_missing() -> None:
    with pytest.raises(ConfigError, match="not found"):
        resolve_tool("definitely-not-a-real-binary-xyz", None)


def test_validate_config_tools_mocked(tmp_path: Path) -> None:
    cfg = load_config(project_root=tmp_path)
    with patch("phone_video_sync.config.resolve_tools", return_value={"adb": "x", "ffmpeg": "y", "ffprobe": "z"}):
        issues = validate_config(cfg, check_tools=True)
    assert issues == []
