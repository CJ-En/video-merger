"""Joining the parts with ffmpeg, or WMV parts with AsfBin (no re-encoding), and checking the result."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import tools
from .plan import PlanItem, duration_tolerance, file_identity, size_tolerance
from .probe import MediaFile, ProbeError, probe

log = logging.getLogger(__name__)

STALL_TIMEOUT = 600.0  # seconds without progress before ffmpeg or AsfBin is stopped
SPACE_MARGIN = 256 * 1024 * 1024
CHECKING = "checking"  # on_step: the parts are joined, the result is being checked


class MergeFailed(Exception):
    """The merge didn't produce a good file."""


@dataclass
class MergeResult:
    item: PlanItem
    ok: bool
    message: str
    output: Path | None = None
    deleted: list[Path] = field(default_factory=list)
    duration: float = 0.0  # of the merged file, as checked
    size: int = 0


def ansi_fits(text: str) -> bool:
    """Whether AsfBin (not a Unicode program) can be given this text: True outside Windows or with a Unicode setting."""
    codepage = tools.ansi_codepage()
    if codepage is None:
        return True
    try:
        text.encode(codepage)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def work_stem(target: Path) -> str:
    """The name of the files written next to the output while it is made: the video's name, or for a .wmv whose name
    AsfBin can't be given (e.g. Japanese letters on a Western European Windows) "merging-<code>", a code made from that
    name. The finished file still gets the video's name (renamed by this program, which can write any name)."""
    if target.suffix.lower() != ".wmv" or ansi_fits(target.stem):
        return target.stem
    return "merging-" + hashlib.sha1(target.name.encode("utf-8")).hexdigest()[:10]


def partial_path(target: Path) -> Path:
    """Where the merged file is written until it passed every check: "<name>.partial.<ext>"."""
    return target.with_name(f"{work_stem(target)}.partial{target.suffix}")


def markers_path(target: Path) -> Path:
    """AsfBin's marker list, next to the output while it runs: "<name>.partial.markers.txt"."""
    return target.with_name(f"{work_stem(target)}.partial.markers.txt")


def content_streams(media: MediaFile) -> int:
    """Streams that must survive the merge (an MP4 adds a data track for its chapters)."""
    return sum(1 for s in media.streams if s.kind in ("video", "audio", "subtitle"))


def concat_line(path: Path) -> str:
    """One line of ffmpeg's concat list. Forward slashes work on Windows too."""
    escaped = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'\n"


def ffmetadata_escape(text: str) -> str:
    for char in ("\\", "=", ";", "#", "\n"):
        text = text.replace(char, "\\" + char)
    return text


def metadata_text(parts: list[MediaFile], numbers: list[int], file_id: str | None) -> str:
    """Chapters "Part N" at each join, and DMM's content id kept in the comment tag."""
    lines = [";FFMETADATA1"]
    if file_id:
        lines.append("comment=" + ffmetadata_escape(json.dumps({"file_id": file_id}, separators=(",", ":"))))
    start = 0.0
    for number, part in zip(numbers, parts):
        end = start + part.duration
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={round(start * 1000)}", f"END={round(end * 1000)}", f"title=Part {number}"]
        start = end
    return "\n".join(lines) + "\n"


def _tail(path: Path, lines: int = 12) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # AsfBin redraws its progress with carriage returns: each one starts a line.
    return "\n".join([line for line in re.split(r"[\r\n]+", text.strip()) if line.strip()][-lines:])


def seconds_text(seconds: float) -> str:
    """ "3561.5" for AsfBin's lists (whole milliseconds at most)."""
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def markers_text(parts: list[MediaFile], numbers: list[int]) -> str:
    """AsfBin's marker list: "<seconds> Part N" where each part starts (the joined file's chapters)."""
    lines, start = [], 0.0
    for number, part in zip(numbers, parts):
        lines.append(f"{seconds_text(start)} Part {number}")
        start += part.duration
    return "\r\n".join(lines) + "\r\n"


def asfbin_command(asfbin: Path) -> list:
    """How AsfBin is started (the tests start a stand-in written in Python instead)."""
    return [asfbin]


def asfbin_name(path: Path) -> str:
    """A path as AsfBin can read it on its command line.

    AsfBin is not a Unicode program: Windows passes its command line in the old code page of the language setting
    (e.g. Western European), where other letters (e.g. Japanese) become "?". Such a path is given in its short
    8.3 form when Windows has one; a name that still can't be written refuses the merge.
    """
    text = str(path)
    if ansi_fits(text):
        return text
    short = tools.short_path(path) if path.exists() else os.path.join(tools.short_path(path.parent), path.name)
    if ansi_fits(short):
        return short
    raise MergeFailed(f"AsfBin can't read the name {path}: it has letters this Windows only shows to Unicode "
                      "programs. Rename the folder or the files with plain letters and digits")


def _run_ffmpeg(cmd: list, total: float, on_progress: Callable[[float], None] | None, stall_timeout: float, err_path: Path) -> int:
    """Run ffmpeg with -progress on stdout; stop it when it makes no progress for too long.

    Once the whole length is written (within what the length check accepts), ffmpeg only finishes the file: for an
    MP4 with +faststart it rewrites the whole file, without progress lines, which can take longer than
    stall_timeout on a slow disk. The stall clock stops then."""
    written = (total - duration_tolerance(total)) * 1_000_000 if total > 0 else None
    with open(err_path, "wb") as err:
        proc = tools.Popen(
            [str(c) for c in cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err,
            creationflags=tools.NO_WINDOW,
        )
        state = {"seen": time.monotonic(), "done": -1}

        def read_progress() -> None:
            for raw in proc.stdout:
                key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
                # out_time_us and (despite its name) out_time_ms are both microseconds.
                if key in ("out_time_us", "out_time_ms") and value.isdigit() and int(value) > state["done"]:
                    state["done"] = int(value)
                    state["seen"] = time.monotonic()
                    if on_progress and total > 0:
                        on_progress(min(1.0, int(value) / 1_000_000 / total))

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            while True:
                try:
                    code = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if written is not None and state["done"] >= written:
                        continue  # finishing the file
                    if time.monotonic() - state["seen"] > stall_timeout:
                        raise MergeFailed(f"ffmpeg made no progress for {round(stall_timeout / 60)} minutes and was stopped")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=5)
            proc.stdout.close()
    return code


def _flush(path: Path, folder: bool = False) -> None:
    """Make sure a file's data, or a folder's list of names after a rename (Linux, macOS; Windows can't and doesn't
    need to), is on the disk and not only in the system's cache: after a power cut, a file under its finished name
    must be the checked file, also when the parts were deleted."""
    if folder and os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY if folder else os.O_RDWR)  # Windows only flushes a file open for writing
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove(path: Path) -> None:
    """Delete a file this program wrote next to the output; one that can't be deleted (another program has it open?)
    is only logged, so the merge's own result still counts."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not delete %s: %s", path, exc)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _run_asfbin(cmd: list, total: int, output: Path, on_progress: Callable[[float], None] | None, stall_timeout: float,
                out_path: Path) -> int:
    """Run AsfBin; its progress is the output file's size against the parts' total size (its own text isn't read).

    It is stopped when neither the output file nor its text grows for stall_timeout seconds."""
    with open(out_path, "wb") as out:
        try:
            proc = tools.Popen(
                [str(c) for c in cmd],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=tools.NO_WINDOW,
            )
        except OSError as exc:
            raise MergeFailed(f"AsfBin could not be started ({exc.strerror or exc}): is {cmd[0]} the real asfbin.exe?") from exc
        seen, sizes = time.monotonic(), (-1, -1)
        try:
            while True:
                try:
                    return proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                now = (_size(output), _size(out_path))
                if now != sizes:
                    if now[0] > sizes[0] and on_progress and total > 0:
                        on_progress(min(1.0, now[0] / total))
                    sizes, seen = now, time.monotonic()
                elif time.monotonic() - seen > stall_timeout:
                    raise MergeFailed(f"AsfBin made no progress for {round(stall_timeout / 60)} minutes and was stopped")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _join_wmv(group, target: Path, partial: Path, asfbin: Path, on_progress, stall_timeout: float, work: Path) -> None:
    """Join .wmv parts with AsfBin into `partial`, with a marker "Part N" where each part starts."""
    markers = markers_path(target)
    try:
        markers.write_bytes(markers_text(group.parts, group.numbers).encode("utf-8"))
        cmd = asfbin_command(asfbin)
        for part in group.parts:
            cmd += ["-i", asfbin_name(part.path)]
        cmd += ["-o", asfbin_name(partial), "-y", "-m", asfbin_name(markers)]
        log.info("merging %s with AsfBin: %s", group.name, " ".join(str(c) for c in cmd))
        code = _run_asfbin(cmd, group.size, partial, on_progress, stall_timeout, work / "asfbin.log")
        output = _tail(work / "asfbin.log")
        if output:
            log.info("AsfBin's last lines for %s:\n%s", group.name, output)
        if code != 0:
            raise MergeFailed(f"AsfBin failed (exit code {code}):\n{output}")
        if not partial.exists():
            raise MergeFailed(f"AsfBin ended without writing the merged file:\n{output}")
        if on_progress:
            on_progress(1.0)
    finally:
        _remove(markers)


def _join(group, target: Path, partial: Path, tool_paths: tools.Tools, on_progress, stall_timeout: float, work: Path) -> None:
    """Join the parts with ffmpeg into `partial`, with a chapter "Part N" at each join."""
    list_path = work / "parts.txt"
    meta_path = work / "chapters.txt"
    list_path.write_text("".join(concat_line(p.path) for p in group.parts), encoding="utf-8")
    meta_path.write_text(metadata_text(group.parts, group.numbers, group.name if group.from_comment else None), encoding="utf-8")
    cmd = [
        tool_paths.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-i", meta_path,
        "-map", "0", "-map_metadata", "1", "-map_chapters", "1",
        "-c", "copy",
    ]
    if target.suffix.lower() == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", partial]
    log.info("merging %s: %s", group.name, " ".join(str(c) for c in cmd))
    code = _run_ffmpeg(cmd, group.duration, on_progress, stall_timeout, work / "ffmpeg.log")
    if code != 0:
        raise MergeFailed(f"ffmpeg failed (exit code {code}):\n{_tail(work / 'ffmpeg.log')}")


def taken(item: PlanItem) -> str | None:
    """Why the merged file can't be saved under its name now, or None: a file of that name appeared after the plan,
    or the one to be made again is no longer the file checked then."""
    target = item.target
    if not os.path.lexists(target):
        return None
    if item.kind == "again":
        if item.replaces is not None and file_identity(target) == item.replaces:
            return None
        return f"{target} has changed since it was checked, so it is not replaced"
    return f"{target} is there now (it wasn't when the parts were checked), so it is not replaced"


def merge(
    item: PlanItem,
    tool_paths: tools.Tools,
    delete_parts: bool = False,
    on_progress: Callable[[float], None] | None = None,
    stall_timeout: float = STALL_TIMEOUT,
    on_step: Callable[[str], None] | None = None,
) -> MergeResult:
    """Merge one planned group into item.target: ffmpeg, or AsfBin for .wmv parts (target "<name>.wmv").

    The joined file is written as "<name>.partial.<ext>"; it is checked (length, size, streams), flushed to the disk
    and only then renamed to the final name, so an interrupted or bad merge never looks finished. The parts are only
    deleted (when asked) after that. on_progress is called from a background thread; on_step(CHECKING) when
    the parts are joined and the result is being checked. A file already under the output's name is only replaced
    when the plan found it to be this video ("again") and it hasn't changed since.
    """
    group, target = item.group, item.target
    assert target is not None and item.action == "merge"
    clash = taken(item)
    if clash:
        return MergeResult(item, False, clash)
    wmv = target.suffix.lower() == ".wmv"
    if wmv and tool_paths.asfbin is None:
        return MergeResult(item, False, f"AsfBin is needed to join WMV parts and was not found. {tools.ASFBIN_HINT}")
    partial = partial_path(target)
    # A folder that can't be made or read fails this video only, never the others in the list.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target.parent).free
    except OSError as exc:
        log.warning("merge of %s failed: %s", group.name, exc)
        return MergeResult(item, False, f"can't save in {target.parent} ({exc.strerror or exc})")
    needed = group.size + SPACE_MARGIN
    if free < needed:
        return MergeResult(item, False, f"not enough free space in {target.parent}: needs {needed / 1e9:.1f} GB, {free / 1e9:.1f} GB free")

    try:
        work = Path(tempfile.mkdtemp(prefix="video-merger-"))
    except OSError as exc:
        log.warning("merge of %s failed: %s", group.name, exc)
        return MergeResult(item, False, f"can't make a folder for the work files in {tempfile.gettempdir()} ({exc.strerror or exc})")
    try:
        try:
            if wmv:
                _join_wmv(group, target, partial, tool_paths.asfbin, on_progress, stall_timeout, work)
            else:
                _join(group, target, partial, tool_paths, on_progress, stall_timeout, work)
            if on_step:
                on_step(CHECKING)
            try:
                merged = probe(partial, tool_paths.ffprobe)
            except ProbeError as exc:
                raise MergeFailed(f"the merged file can't be read: {exc}") from exc
            expected = group.duration
            if abs(merged.duration - expected) > duration_tolerance(expected):
                raise MergeFailed(f"the merged file is {merged.duration:.1f} s long, but the parts add up to {expected:.1f} s")
            size = partial.stat().st_size
            if abs(size - group.size) > size_tolerance(group.size):
                raise MergeFailed(f"the merged file is {size / 1e9:.2f} GB, but the parts add up to {group.size / 1e9:.2f} GB")
            have, want = content_streams(merged), content_streams(group.parts[0])
            if have != want:
                raise MergeFailed(f"the merged file has {have} video/audio/subtitle streams instead of {want}")
            try:
                _flush(partial)
            except OSError as exc:
                if delete_parts:  # only deleted when the merged file is surely on the disk
                    raise MergeFailed(f"the merged file could not be written to the disk for sure ({exc.strerror or exc}), "
                                      "so the parts are kept") from exc
                log.warning("could not flush %s to the disk: %s", partial, exc)
            clash = taken(item)  # the join takes minutes: look again just before
            if clash:
                raise MergeFailed(clash)
            os.replace(partial, target)
            try:
                _flush(target.parent, folder=True)
            except OSError as exc:
                log.warning("could not flush the folder %s to the disk: %s", target.parent, exc)
            checked = (merged.duration, size)
        except MergeFailed as exc:
            log.warning("merge of %s failed: %s", group.name, exc)
            return MergeResult(item, False, str(exc))
        except OSError as exc:
            log.warning("merge of %s failed: %s", group.name, exc)
            return MergeResult(item, False, f"{type(exc).__name__}: {exc}")
        finally:
            _remove(partial)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    result = MergeResult(item, True, f"merged {len(group.parts)} parts", output=target, duration=checked[0], size=checked[1])
    if delete_parts:
        problems = []
        for part in group.parts:
            try:
                part.path.unlink()
                result.deleted.append(part.path)
            except OSError as exc:
                problems.append(f"{part.path}: {exc}")
        if problems:
            result.message += "; some parts could not be deleted: " + "; ".join(problems)
    log.info("merged %s -> %s", group.name, target)
    return result
