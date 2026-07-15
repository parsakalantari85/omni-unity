"""File-based logging for omni.

File-only on purpose: the full-screen UI owns stdout/stderr. The handler
sits on the root logger so SDK warnings/errors are captured too.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup() -> Path:
    """Attach the rotating file handler (once, at startup); returns the log path."""
    log_file = config.path().parent / "omni.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    get().setLevel(logging.INFO)
    return log_file


def get(name: str | None = None) -> logging.Logger:
    """Logger in the omni namespace: get() -> "omni", get("ui") -> "omni.ui"."""
    return logging.getLogger(f"omni.{name}" if name else "omni")
