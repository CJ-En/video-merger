"""A log file per user (rotating), for when something needs to be looked at later."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP = "VideoMerger"


def log_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "video-merger"


def setup(verbose: bool = False) -> Path | None:
    """Log to <log_dir>/video-merger.log; returns its path, or None if it can't be written."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for old in [h for h in root.handlers if getattr(h, "video_merger", False)]:
        root.removeHandler(old)
        old.close()
    if verbose:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter("  [log] %(message)s"))
        console.video_merger = True
        root.addHandler(console)
    path = log_dir() / "video-merger.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.video_merger = True
    root.addHandler(handler)
    return path
