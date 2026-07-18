"""Rich console + rotating file logging setup."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_CONSOLE = Console(stderr=True)


def get_console() -> Console:
    return _CONSOLE


def setup_logging(
    log_dir: Path,
    *,
    level: int = logging.INFO,
    run_name: str | None = None,
) -> Path:
    """Configure root logger with Rich console + rotating/timestamped file.

    Returns the path of the run-specific log file.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_label = run_name or "run"
    run_log = log_dir / f"{run_label}-{ts}.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    rich_handler = RichHandler(
        console=_CONSOLE,
        show_path=False,
        rich_tracebacks=True,
        markup=True,
    )
    rich_handler.setLevel(level)
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(rich_handler)

    file_handler = RotatingFileHandler(
        run_log,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    # Also keep a rotating aggregate log
    aggregate = RotatingFileHandler(
        log_dir / "phone-sync.log",
        maxBytes=10_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    aggregate.setLevel(logging.DEBUG)
    aggregate.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(aggregate)

    logging.getLogger("phone_video_sync").debug("Logging initialized -> %s", run_log)
    return run_log
