"""Terminal interface: video-merger [merge] PATH... | doctor | gui [PATH...].

Without arguments (a double-click on run.bat or VideoMerger.exe) it starts the browser page (web.py); so do
folders and files dragged onto run.bat or VideoMerger.exe (`launcher`), which the page then has as places to look."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__, dmm_parts, logs, tools
from .discover import find_video_files, group_files
from .merge import MergeResult, merge
from .plan import build_plan
from .probe import ProbeError, probe

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_SETUP, EXIT_INTERRUPTED = 0, 1, 2, 130
COMMANDS = {"merge", "doctor", "gui"}
# Set by run.bat: started by a double-click or with files dragged onto it, so paths alone open the page.
LAUNCHER_ENV = "VIDEO_MERGER_LAUNCHER"


def hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def size_text(size: int) -> str:
    return f"{size / 1024**3:.2f} GB" if size >= 1024**3 else f"{size / 1024**2:.0f} MB"


class Progress:
    """A progress line (redrawn in a terminal, every 10% when the output goes to a file)."""

    def __init__(self, total: float) -> None:
        self.total = total
        self.tty = sys.stdout.isatty()
        self.last_draw = 0.0
        self.last_step = -1
        self.lock = threading.Lock()

    def __call__(self, fraction: float) -> None:
        with self.lock:
            now = time.monotonic()
            if self.tty:
                if now - self.last_draw < 0.5 and fraction < 1:
                    return
                self.last_draw = now
                filled = int(fraction * 30)
                bar = "#" * filled + "." * (30 - filled)
                print(f"\r    [{bar}] {fraction * 100:5.1f}%  {hms(fraction * self.total)} / {hms(self.total)}", end="", flush=True)
            else:
                step = int(fraction * 10)
                if step > self.last_step:
                    self.last_step = step
                    print(f"    {step * 10}%", flush=True)

    def finish(self) -> None:
        if self.tty and self.last_draw:
            print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-merger",
        description="Merge multi-part videos (e.g. DMM downloads) into one file without re-encoding.",
    )
    parser.add_argument("--version", action="version", version=f"video-merger {__version__}")
    sub = parser.add_subparsers(dest="command")

    m = sub.add_parser("merge", help="merge the parts found in the given files/folders (default command)")
    m.add_argument("paths", nargs="*", type=Path, help="video files and/or folders holding the parts")
    m.add_argument("--files-from", type=Path, metavar="LIST",
                   help="also take the files listed in this text file (one path per line, UTF-8 or UTF-16)")
    m.add_argument("-o", "--out", type=Path, help="save merged videos here (default: the parts' folder)")
    m.add_argument("-r", "--recursive", action="store_true", help="also look in subfolders")
    m.add_argument("-n", "--dry-run", action="store_true", help="only show what would be done")
    m.add_argument("--container", choices=("mkv", "mp4"), default="mkv", help="output format (default: mkv)")
    m.add_argument("--delete-parts", action="store_true", help="delete the parts after a merge that passed every check")
    m.add_argument("--overwrite", action="store_true", help="redo videos that were already merged")
    m.add_argument("--dmm-parts", type=Path, metavar="CSV",
                   help="a CSV of DMM's parts per video (see the README): only merge videos whose parts match it")
    m.add_argument("-v", "--verbose", action="store_true", help="show more detail")
    add_tool_options(m)

    d = sub.add_parser("doctor", help="check that ffmpeg and ffprobe (and AsfBin, for WMV parts) are found and work")
    add_tool_options(d)

    g = sub.add_parser("gui", help="the browser page (the default without arguments)")
    g.add_argument("paths", nargs="*", help="folders and video files the page starts with, as places to look in")
    g.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: 8767, else a free one)")
    g.add_argument("--no-browser", action="store_true", help="only show the address, don't open the browser")
    g.add_argument("-v", "--verbose", action="store_true", help="show the log in this window too")
    return parser


def add_tool_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ffmpeg", help="path to ffmpeg (or its folder)")
    parser.add_argument("--ffprobe", help="path to ffprobe (or its folder)")
    parser.add_argument("--asfbin", help="path to asfbin.exe (or its folder), which joins WMV parts")


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"video-merger {__version__}, Python {sys.version.split()[0]} on {sys.platform}")
    print(f"Log folder: {logs.log_dir()}")
    ok = True
    for name, override in (("ffmpeg", args.ffmpeg), ("ffprobe", args.ffprobe)):
        try:
            path = tools.find_tool(name, override)
            print(f"{name}: {path}\n    {tools.version(path)}")
        except tools.ToolError as exc:
            print(f"{name}: {exc}")
            ok = False
    try:
        asfbin = tools.find_asfbin(args.asfbin)
    except tools.ToolError as exc:
        print(f"asfbin: {exc}")
        ok = False
    else:
        if asfbin:
            version = tools.file_version(asfbin)
            print(f"asfbin: {asfbin}\n    {'AsfBin ' + version if version else 'AsfBin'}: WMV parts are joined")
        else:
            print(f"asfbin: not found, so WMV files are left alone (only needed for WMV parts). {tools.ASFBIN_HINT}")
    print("Everything needed was found." if ok else "Something is missing, see above.")
    return EXIT_OK if ok else EXIT_SETUP


def cmd_gui(args: argparse.Namespace) -> int:
    from . import settings, web

    logs.setup(args.verbose)
    log.info("video-merger %s: the browser page", __version__)
    try:
        return web.serve(settings.load(), places=args.paths, port=args.port, open_browser=not args.no_browser)
    except web.PortError as exc:
        print(exc)
        return EXIT_SETUP


def read_list(path: Path) -> list[str]:
    """The lines of a --files-from list: UTF-8 text (with or without a byte order mark), or UTF-16 with one (Windows
    PowerShell 5.1 writes that). ValueError with what to do for any other text."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe\0\0", b"\0\0\xfe\xff")):
        raise ValueError("it is UTF-32 text; save it as UTF-8")
    try:
        text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("it isn't UTF-8 text, nor UTF-16 with a byte order mark; save it as UTF-8") from None
    if "\0" in text:
        raise ValueError("it looks like UTF-16 text without a byte order mark; save it as UTF-8")
    return text.splitlines()


def cmd_merge(args: argparse.Namespace) -> int:
    log_path = logs.setup(args.verbose)
    log.info("video-merger %s: %s", __version__, sys.argv)
    try:
        tool_paths = tools.find_tools(args.ffmpeg, args.ffprobe)
        tool_paths = dataclasses.replace(tool_paths, asfbin=tools.find_asfbin(args.asfbin))
    except tools.ToolError as exc:
        print(exc)
        return EXIT_SETUP
    wmv = tool_paths.asfbin is not None

    dmm = None
    if args.dmm_parts:
        try:
            dmm = dmm_parts.load(args.dmm_parts)
        except (OSError, KeyError, ValueError) as exc:
            print(f"Can't read the DMM parts list {args.dmm_parts}: {exc}")
            return EXIT_SETUP
        print(f"DMM parts list: {len({id(v) for v in dmm.values()})} video(s).")

    paths = list(args.paths)
    if args.files_from:
        try:
            lines = read_list(args.files_from)
        except (OSError, ValueError) as exc:
            print(f"Can't read the file list {args.files_from}: {exc}")
            return EXIT_SETUP
        paths += [Path(line.strip()) for line in lines if line.strip() and not line.startswith("#")]
    if not paths:
        print("No files or folders given.")
        return EXIT_SETUP
    files, missing, ignored = find_video_files(paths, args.recursive, wmv=wmv)
    for path in missing:
        print(f"Not found: {path}")
    if ignored:
        kinds = sorted({p.suffix.lower() for p in ignored})
        merged = ".mkv, .mp4 and .wmv" if wmv else ".mkv and .mp4"
        print(f"Ignored {len(ignored)} {', '.join(kinds)} file(s): only {merged} parts are merged.")
        if ".wmv" in kinds:
            print(f"    AsfBin (Windows) lets video-merger join WMV parts: {tools.ASFBIN_URL}")
        if args.verbose:
            for path in ignored:
                print(f"  {path}")
    if not files:
        print(f"No {'.mkv, .mp4 or .wmv' if wmv else '.mkv or .mp4'} files found.")
        return EXIT_FAILED

    print(f"Reading {len(files)} video file(s)...")
    media, unreadable = [], 0
    for path in files:
        try:
            media.append(probe(path, tool_paths.ffprobe))
        except ProbeError as exc:
            unreadable += 1
            print(f"  Can't read {path}: {exc}")
    groups, singles = group_files(media)
    plan = build_plan(groups, args.out, args.container, args.overwrite, dmm, read=lambda path: probe(path, tool_paths.ffprobe))
    lone = dmm_parts.lone_parts(singles, dmm) if dmm is not None else []
    if lone:
        print(f"\n{len(lone)} file(s) are the only part here of a video DMM has in several parts:")
        for single, reason in lone:
            print(f"  {single.path}\n      PROBLEM: {reason}")

    if not plan:
        if lone:
            return EXIT_FAILED
        print("No multi-part videos found (every file is a whole video).")
        return EXIT_FAILED if missing or unreadable else EXIT_OK

    print(f"\nFound {len(plan)} multi-part video(s):")
    for item in plan:
        g = item.group
        video = g.parts[0].video
        kind = f"{video.codec} {video.width}x{video.height}" if video else "no video stream"
        print(f"  {g.name}: {len(g.parts)} parts, {kind}, {hms(g.duration)}, {size_text(g.size)}")
        if args.verbose:
            for number, part in zip(g.numbers, g.parts):
                print(f"      part {number}: {part.path}")
        if item.action == "merge":
            print(f"      -> {item.target}")
        else:
            print(f"      {'SKIP' if item.action == 'skip' else 'PROBLEM'}: {item.reason}")
        for warning in item.warnings:
            print(f"      warning: {warning}")
    if args.verbose and singles:
        print(f"\n{len(singles)} file(s) are whole videos, not parts:")
        for single in singles:
            print(f"  {single.path}")

    to_merge = [item for item in plan if item.action == "merge"]
    problems = sum(1 for item in plan if item.action == "problem") + len(lone)
    if args.dry_run:
        print(f"\nDry run: {len(to_merge)} to merge, nothing was changed.")
        return EXIT_FAILED if problems or missing or unreadable else EXIT_OK

    merged = failed = 0
    for index, item in enumerate(to_merge, 1):
        print(f"\n[{index}/{len(to_merge)}] Merging {item.group.name} ({len(item.group.parts)} parts)")
        progress = Progress(item.group.duration)
        try:
            result = merge(item, tool_paths, delete_parts=args.delete_parts, on_progress=progress)
        except Exception as exc:  # never let one video stop the others
            log.exception("merge failed")
            result = MergeResult(item, False, f"unexpected error: {exc}")
        progress.finish()
        if result.ok:
            merged += 1
            print(f"    Done: {result.output} ({result.message})")
            if result.deleted:
                print(f"    Deleted {len(result.deleted)} part file(s).")
        else:
            failed += 1
            print(f"    FAILED: {result.message}")
            print("    The parts were not changed.")

    print(f"\nMerged: {merged}  Failed: {failed}  Problems: {problems}  Skipped: {sum(1 for item in plan if item.action == 'skip')}")
    if log_path:
        print(f"Log: {log_path}")
    return EXIT_FAILED if failed or problems or missing or unreadable else EXIT_OK


def owns_console() -> bool:
    """True when Windows opened a console just for us (double-click or drag and drop onto the
    program), which closes as soon as we exit."""
    if os.name != "nt" or not sys.stdin.isatty():
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) <= 2
    except Exception:
        return False


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Japanese file names on a non-UTF-8 console
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None, launcher: bool = False) -> int:
    """`launcher`: started by VideoMerger.exe or run.bat, where folders and files dragged onto it (paths alone, no
    command or option) open the page with them. In a terminal, `video-merger PATH...` merges there, as always."""
    setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    launcher = os.environ.pop(LAUNCHER_ENV, "") == "1" or launcher
    if not argv:
        argv = ["gui"]  # a double-click: the browser page
    elif launcher and argv[0] not in COMMANDS and not any(arg.startswith("-") for arg in argv):
        argv.insert(0, "gui")
    if argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "merge")
    args = build_parser().parse_args(argv)
    pause = owns_console() and args.command != "gui"  # the page's window closes when it is quit
    try:
        if args.command == "doctor":
            code = cmd_doctor(args)
        elif args.command == "merge":
            code = cmd_merge(args)
        elif args.command == "gui":
            code = cmd_gui(args)
        else:
            build_parser().print_help()
            code = EXIT_SETUP
    except KeyboardInterrupt:
        print("\nStopped. Unfinished output was removed; the parts were not changed.")
        code = EXIT_INTERRUPTED
    if pause:
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code
