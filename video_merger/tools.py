"""Finding and running ffmpeg, ffprobe and AsfBin."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import children

WINDOWS = os.name == "nt"
# Keeps a console window from flashing up for every tool run when started from the GUI.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# How programs are started: they end when Video Merger ends (children.py).
Popen = children.Popen

INSTALL_HINT = (
    "Install FFmpeg (Windows: https://www.gyan.dev/ffmpeg/builds/ , Linux: sudo apt install ffmpeg), "
    "or put ffmpeg and ffprobe next to video-merger, or give their location with --ffmpeg / --ffprobe."
)
ASFBIN_URL = "http://www.radioactivepages.com"
ASFBIN_HINT = (
    f"AsfBin (Windows only, free for non-commercial use: {ASFBIN_URL}) joins WMV parts: "
    "put asfbin.exe next to video-merger or on PATH, or give its location with --asfbin."
)


class ToolError(Exception):
    """A required program is missing or unusable."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: Path
    ffprobe: Path
    asfbin: Path | None = None  # only needed for WMV parts


def program_dir() -> Path:
    """The folder of the .exe (packaged) or of the source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _file_names(name: str) -> list[str]:
    if WINDOWS and not name.lower().endswith(".exe"):
        return [f"{name}.exe"]
    return [name]


def _usable(path: Path) -> bool:
    return path.is_file() and (WINDOWS or os.access(path, os.X_OK))


def find_tool(name: str, override: str | os.PathLike | None = None) -> Path:
    """Find a program: the given path, next to this program, then PATH.

    The current folder is never searched (unlike Windows' own lookup), so a stray ffmpeg.exe
    sitting next to the videos is not run.
    """
    if override:
        given = Path(override).expanduser()
        candidates = [given / n for n in _file_names(name)] if given.is_dir() else [given]
        for candidate in candidates:
            if _usable(candidate):
                return candidate.resolve()
        raise ToolError(f"{name} was not found at {given}.")

    folders = [program_dir(), program_dir() / "bin"]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and Path(entry).is_absolute():
            folders.append(Path(entry))
    for folder in folders:
        for file_name in _file_names(name):
            candidate = folder / file_name
            if _usable(candidate):
                return candidate.resolve()
    raise ToolError(f"{name} was not found. {ASFBIN_HINT if name == 'asfbin' else INSTALL_HINT}")


def find_tools(ffmpeg: str | None = None, ffprobe: str | None = None) -> Tools:
    return Tools(ffmpeg=find_tool("ffmpeg", ffmpeg), ffprobe=find_tool("ffprobe", ffprobe))


def find_asfbin(override: str | os.PathLike | None = None) -> Path | None:
    """AsfBin, found like ffmpeg, or None when it isn't there (WMV files are then left alone).

    A location given that doesn't hold it is an error (ToolError), as for ffmpeg."""
    try:
        return find_tool("asfbin", override)
    except ToolError:
        if override:
            raise
        return None


def run(cmd: list, timeout: float) -> subprocess.CompletedProcess:
    """Run a short command and capture its output (bytes). Like subprocess.run, but started with Popen above."""
    with Popen([str(part) for part in cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               creationflags=NO_WINDOW) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def version(path: Path) -> str:
    try:
        result = run([path, "-version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"not working ({exc})"
    first_line = result.stdout.decode("utf-8", "replace").splitlines()[:1]
    return first_line[0].strip() if first_line else "unknown version"


def short_version(text: str) -> str:
    """ "7.1" from "ffmpeg version 7.1-essentials_build-www.gyan.dev Copyright ...", the date of a git build, or ""."""
    match = re.search(r"\bversion\s+n?(\d+(?:\.\d+)+)", text) or re.search(r"\bversion\s+(\d{4}-\d\d-\d\d)", text)
    return match.group(1) if match else ""


def file_version(path: Path) -> str:
    """A Windows program's version from its file properties ("1.8.3"), read without starting it; "" if it has none.

    Used for AsfBin, which has no option that only prints its version."""
    if not WINDOWS:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        dll = ctypes.WinDLL("version")
        dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        dll.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
        dll.GetFileVersionInfoW.restype = wintypes.BOOL
        dll.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
        dll.VerQueryValueW.restype = wintypes.BOOL
        dll.VerQueryValueW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT))

        class FixedFileInfo(ctypes.Structure):  # VS_FIXEDFILEINFO
            _fields_ = [(name, wintypes.DWORD) for name in (
                "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS", "dwProductVersionMS",
                "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags", "dwFileOS", "dwFileType", "dwFileSubtype",
                "dwFileDateMS", "dwFileDateLS")]

        size = dll.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        data = ctypes.create_string_buffer(size)
        if not dll.GetFileVersionInfoW(str(path), 0, size, data):
            return ""
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not dll.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)) or not pointer.value:
            return ""
        info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
        if info.dwSignature != 0xFEEF04BD:
            return ""
        return f"{info.dwFileVersionMS >> 16}.{info.dwFileVersionMS & 0xFFFF}.{info.dwFileVersionLS >> 16}"
    except (OSError, AttributeError, ValueError):
        return ""


def ansi_codepage() -> str | None:
    """Windows' code page for programs that aren't Unicode ("cp1252"), or None elsewhere or when it is UTF-8."""
    if not WINDOWS:
        return None
    try:
        import ctypes

        page = ctypes.windll.kernel32.GetACP()
    except (OSError, AttributeError):
        return None
    return None if page == 65001 else f"cp{page}"


def short_path(path: Path) -> str:
    """Windows' short 8.3 form of an existing path ("C:\\PROGRA~1\\..."), or the path itself when there is none."""
    if not WINDOWS:
        return str(path)
    try:
        import ctypes
        from ctypes import wintypes

        function = ctypes.windll.kernel32.GetShortPathNameW
        function.restype = wintypes.DWORD
        function.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(str(path), buffer, len(buffer))
        return buffer.value if 0 < length < len(buffer) else str(path)
    except (OSError, AttributeError):
        return str(path)
