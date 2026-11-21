"""The browser page's settings file: the merge options and the programs' places, kept for the next start.

The terminal commands don't read it: their options are given on the command line."""

from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP = "VideoMerger"
SLUG = "video-merger"
SECTION = "settings"

TEMPLATE = """\
# Video Merger settings (the browser page writes them). Empty values use the default.

[settings]
# Save merged videos: "next" (next to their parts) or "folder" (all in out_dir).
save_to = next
# The folder for "folder" (default: Videos\\Merged in your home folder).
out_dir =

# File type of merged videos: mkv or mp4. WMV parts always give a .wmv.
container = mkv

# Also look in subfolders of the folders added: yes or no.
subfolders = yes

# Merge again videos already merged (replaces the merged file): yes or no.
merge_again = no

# Delete the parts after a merge that passed every check: yes or no.
delete_parts = no

# ffmpeg, ffprobe and asfbin (a file or its folder; default: next to Video Merger, then PATH).
ffmpeg =
ffprobe =
asfbin =
"""


def config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / SLUG


def settings_path() -> Path:
    return config_dir() / "settings.ini"


def default_out_dir() -> Path:
    return Path.home() / "Videos" / "Merged"


@dataclass
class Settings:
    save_to: str = "next"  # next, folder
    out_dir: str = ""
    container: str = "mkv"
    subfolders: bool = True
    merge_again: bool = False
    delete_parts: bool = False
    ffmpeg: str = ""
    ffprobe: str = ""
    asfbin: str = ""


TEXTS = ("out_dir", "ffmpeg", "ffprobe", "asfbin")
SWITCHES = ("subfolders", "merge_again", "delete_parts")


def load(path: Path | None = None) -> Settings:
    path = path or settings_path()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if path.exists():
            parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        pass  # unreadable: the defaults
    values = parser[SECTION] if parser.has_section(SECTION) else {}
    settings = Settings()

    def text(key: str) -> str:
        return str(values.get(key, "") or "").strip().strip('"')

    for key in TEXTS:
        setattr(settings, key, text(key))
    if text("save_to").lower() in ("next", "folder"):
        settings.save_to = text("save_to").lower()
    if text("container").lower() in ("mkv", "mp4"):
        settings.container = text("container").lower()
    for key in SWITCHES:
        if text(key):
            setattr(settings, key, text(key).lower() in ("yes", "true", "1", "on"))
    return settings


def save(settings: Settings, path: Path | None = None) -> Path:
    """Write the settings into the file, keeping its comments."""
    path = path or settings_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = TEMPLATE.splitlines()
    values = {key: ("yes" if value else "no") if isinstance(value, bool) else str(value) for key, value in vars(settings).items()}
    written = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith(("#", ";")) and key in values:
            lines[index] = f"{key} = {values[key]}"
            written.add(key)
    if f"[{SECTION}]" not in (line.strip() for line in lines):
        lines.append(f"[{SECTION}]")
    lines += [f"{key} = {value}" for key, value in values.items() if key not in written]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
