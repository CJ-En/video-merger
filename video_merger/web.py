"""The browser page: a small web server on 127.0.0.1 that only this computer's browser can use.

The page (web/index.html, app.css, app.js) asks this server for the state (the places to look in, the videos found,
the merge running) and sends what the user does. Only the browser opened by Video Merger gets in: the first address
carries a random key, which becomes a cookie for this port; other web pages can't send requests that pass (cookie
with SameSite=Strict, a header of our own, the Host checked against DNS rebinding).

"Look for videos" reads the files in the background (discover, probe, plan); changing an option plans the videos
found again without reading them again. "Merge" merges one video at a time with merge.merge(). Quit, Ctrl+C or
closing the window ends the programs started (children.py) and removes an unfinished merged file; the parts are
never changed.
"""

from __future__ import annotations

import collections
import errno
import json
import logging
import os
import re
import secrets
import signal
import string
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, children, logs, tools
from . import settings as config
from .discover import Group, find_video_files, group_files
from .merge import CHECKING, MergeResult, markers_path, merge, partial_path
from .plan import PlanItem, build_plan, is_wmv
from .probe import MediaFile, ProbeError, format_rate, probe

log = logging.getLogger(__name__)

PORT = 8767  # any free port when it is taken
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
HEADER = "X-Video-Merger"
MAX_BODY = 64 * 1024
PLACE_FILES = {".mkv", ".mp4", ".wmv"}  # files that can be added as places, and that the folder browser lists
TOOL_NAMES = ("ffmpeg", "ffprobe", "asfbin")
SWITCHES = ("subfolders", "merge_again", "delete_parts")

CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1", "vc1": "VC-1", "wmv1": "WMV1",
               "wmv2": "WMV2", "wmv3": "WMV3", "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4", "aac": "AAC", "wmav1": "WMA",
               "wmav2": "WMA", "wmapro": "WMA Pro", "ac3": "AC-3", "eac3": "E-AC-3", "truehd": "TrueHD", "dts": "DTS",
               "mp3": "MP3", "opus": "Opus", "flac": "FLAC", "vorbis": "Vorbis"}
LOWER_NAMES = {"ffmpeg", "ffprobe", *CODEC_NAMES}  # stay as written at the start of a sentence


class Refused(Exception):
    """The request can't be done; the message is shown on the page."""


def web_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", tools.program_dir())) / "video_merger" / "web"
    return Path(__file__).resolve().parent / "web"


def sentence(text: str) -> str:
    """A message as a sentence: a capital first letter (not for program and codec names: "ffmpeg", "h264") and a
    full stop."""
    text = str(text or "").strip().rstrip(":").strip()
    if not text:
        return ""
    first = re.split(r"[\s(:,;]", text, maxsplit=1)[0]
    if first.isalpha() and first.lower() not in LOWER_NAMES:
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def codec_name(codec: str) -> str:
    return "LPCM" if codec.startswith("pcm_") else CODEC_NAMES.get(codec, codec.upper())


def chips(media: MediaFile) -> list[str]:
    """The first part's video and sound in a few words: "H.264", "1920×1080", "29.97 fps", "AAC"."""
    found = []
    video, audio = media.video, media.audio
    if video:
        found.append(codec_name(video.codec))
        if video.width and video.height:
            found.append(f"{video.width}×{video.height}")
        rate = format_rate(video.frame_rate) if video.frame_rate not in ("", "0/0") else ""
        if rate and rate != "?":
            found.append(f"{rate} fps")
    if audio:
        found.append(codec_name(audio.codec))
    return found


def explain(item: PlanItem) -> tuple[str, str]:
    """Why a video isn't merged, and what to do about it, for the page."""
    kind, reason, target = item.kind, item.reason, item.target
    if kind == "vp9":
        return "VP9 video: DMM's 4K VP9 parts stay separate files.", ""
    if kind == "already" and target is not None:
        return f"{target.name} is already there.", "Tick “Merge again videos already merged” to make it again."
    if kind == "formats":
        return sentence(reason), ("The parts of one video must all be .wmv, or all .mkv or .mp4: "
                                  "they are joined as they are, never re-encoded.")
    if kind == "containers":
        return sentence(reason), ("What is inside counts, not the extension. A remux (e.g. MKVToolNix, or ffmpeg -c copy) "
                                  "changes it without re-encoding; then “Look again”.")
    if kind == "codecs":
        return sentence(reason), "Such a file may be damaged, or another format renamed to .wmv."
    if kind == "parts":
        if "missing part" in reason:
            return sentence(reason), "Download the missing part (or add the folder it is in), then “Look again”."
        return sentence(reason), "The same part is in two places: remove one copy (or one of the places), then “Look again”."
    if kind == "mismatch":
        return sentence(reason), "Parts that differ can't be joined without re-encoding, which this tool never does."
    if kind == "empty":
        return sentence(reason), "Download that part again."
    if kind == "name_is_part" and target is not None:
        return (f"The merged file would have the same name as one of its parts ({target.name}).",
                "Choose “All in one folder” to save it somewhere else.")
    if kind == "clash":
        return sentence(reason), "Save them next to their parts, or keep the videos in different folders."
    if kind == "taken":
        return sentence(reason), "Move or rename that file (or save the merged videos in another folder), then “Look again”."
    return sentence(reason), ""


@dataclass
class Row:
    """One video in parts found by a look."""

    id: int
    group: Group
    item: PlanItem
    state: str = "planned"  # planned (see item), waiting, running, done, failed
    steps: list[str] = field(default_factory=list)
    progress: float | None = None
    result: MergeResult | None = None


def group_key(group: Group) -> str:
    """Which video a row is, the same after "Look again" (its rows get new numbers): folder and name."""
    return os.path.normcase(str(group.folder)) + "\0" + group.name.casefold()


def empty_others() -> dict:
    return {"whole": [], "ignored": [], "unreadable": [], "missing": [], "wmv": False}


class App:
    def __init__(self, cfg: config.Settings, places=()) -> None:
        self.lock = threading.RLock()
        self.settings = cfg
        self.places: list[dict] = []
        self.groups: list[Group] = []
        self.rows: list[Row] = []
        self.left_out: set[str] = set()  # group_key of the videos left out of the merging, until the program closes
        self.next_id = 1
        self.scanned = False
        self.scan = {"running": False, "done": 0, "total": 0}
        self.others = empty_others()
        self.running = False
        self.stop_after = False
        self.current: Row | None = None
        self.worker: threading.Thread | None = None
        self.lines: collections.deque = collections.deque(maxlen=2000)
        self.line_no = 0
        self.found: dict[str, Path | None] = {name: None for name in TOOL_NAMES}
        self.versions: dict[str, str] = {}
        self.tool_errors: dict[str, str] = {}
        self.tool_paths: tools.Tools | None = None
        self.outputs: dict[tuple, MediaFile | str] = {}  # files under the outputs' names, read once per version
        self.quit = threading.Event()
        self.ready = threading.Event()  # programs looked for
        for place in places:
            try:
                self.add_place(str(place))
            except Refused as exc:
                self.note(f"not added: {place} ({exc})")

    def startup(self) -> None:
        try:
            self.find_tools()
            self.note(f"setup: {self.tools_summary()}")
        finally:
            self.ready.set()

    # -- log -------------------------------------------------------------------------------

    def note(self, text: str) -> None:
        log.info("%s", text)
        with self.lock:
            self.line_no += 1
            self.lines.append((self.line_no, time.strftime("%H:%M:%S"), text))

    # -- programs --------------------------------------------------------------------------

    def find_tools(self) -> None:
        s = self.settings
        found, errors, versions = {}, {}, {}
        for name in TOOL_NAMES:
            override = getattr(s, name)
            try:
                found[name] = tools.find_tool(name, override or None)
            except tools.ToolError:
                found[name] = None
                errors[name] = f"not found at {override}" if override else "not found"
        for name in ("ffmpeg", "ffprobe"):
            if found[name]:
                versions[name] = tools.short_version(tools.version(found[name]))
        if found["asfbin"]:
            versions["asfbin"] = tools.file_version(found["asfbin"])
        with self.lock:
            self.found, self.tool_errors, self.versions = found, errors, versions
            ok = found["ffmpeg"] and found["ffprobe"]
            self.tool_paths = tools.Tools(found["ffmpeg"], found["ffprobe"], found["asfbin"]) if ok else None

    def tools_summary(self) -> str:
        found, versions = self.found, self.versions
        if found["ffmpeg"] and found["ffprobe"]:
            text = " ".join(filter(None, ["FFmpeg", versions.get("ffmpeg")])) + " found"
        else:
            text = " and ".join(n for n in ("ffmpeg", "ffprobe") if not found[n]) + " not found (needed)"
        if found["asfbin"]:
            text += "; " + " ".join(filter(None, ["AsfBin", versions.get("asfbin")])) + " found (WMV parts are joined)"
        else:
            text += "; AsfBin not found (WMV files are left alone)"
        return text

    def save_settings(self, data: dict) -> None:
        if self.running:
            raise Refused("Wait until the merging is done.")
        texts = {name: str(data[name] or "").strip().strip('"').strip() for name in TOOL_NAMES if name in data}
        with self.lock:
            for name, value in texts.items():
                setattr(self.settings, name, value)
        self.save()
        self.find_tools()
        self.note(f"programs saved: {self.tools_summary()}")

    def doctor(self) -> None:
        if self.running:
            raise Refused("Wait until the merging is done.")
        self.find_tools()
        self.note(f"setup checked: {self.tools_summary()}")

    def save(self) -> None:
        try:
            config.save(self.settings)
        except OSError as exc:
            self.note(f"the settings could not be saved ({exc.strerror or exc})")

    # -- places ----------------------------------------------------------------------------

    def add_place(self, text: str) -> dict:
        value = str(text or "").strip().strip('"').strip()
        if not value:
            raise Refused("Paste a folder or a video file first.")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise Refused("Paste a full path, e.g. " + ("D:\\Downloads\\DMM" if os.name == "nt" else "/home/me/Videos"))
        if path.is_dir():
            kind = "folder"
        elif path.is_file():
            if path.suffix.lower() not in PLACE_FILES:
                raise Refused("Only folders and .mkv, .mp4 or .wmv files can be added.")
            kind = "file"
        else:
            raise Refused(f"Not found: {path}")
        key = os.path.normcase(str(path))
        with self.lock:
            if any(os.path.normcase(place["path"]) == key for place in self.places):
                raise Refused("Already in the list.")
            self.places.append({"path": str(path), "kind": kind})
        self.note(f"added {path}")
        return {"added": str(path)}

    def remove_place(self, text: str) -> None:
        key = os.path.normcase(str(text or ""))
        with self.lock:
            if self.running:
                raise Refused("Wait until the merging is done.")
            if self.scan["running"]:
                raise Refused("Wait until the look is done.")
            for place in self.places:
                if os.path.normcase(place["path"]) == key:
                    self.places.remove(place)
                    break
            else:
                raise Refused("That place is no longer in the list.")
        self.note(f"removed {text}")

    # -- looking ---------------------------------------------------------------------------

    def out_dir(self) -> Path | None:
        """The folder for all merged videos, or None: next to their parts."""
        s = self.settings
        if s.save_to != "folder":
            return None
        return Path(s.out_dir).expanduser() if s.out_dir else config.default_out_dir()

    def look(self) -> None:
        with self.lock:
            if self.running:
                raise Refused("Wait until the merging is done.")
            if self.scan["running"]:
                raise Refused("Already looking.")
            if not self.places:
                raise Refused("Add a folder or a video file to look in first.")
            if not self.ready.is_set():
                raise Refused("Starting… try again in a moment.")
            if self.tool_paths is None:
                raise Refused("FFmpeg (ffmpeg and ffprobe) is needed to read the videos: see the Settings tab.")
            self.scan = {"running": True, "done": 0, "total": 0}
            places = [Path(place["path"]) for place in self.places]
            recursive, tool_paths = self.settings.subfolders, self.tool_paths
        threading.Thread(target=self._look, args=(places, recursive, tool_paths), name="look", daemon=True).start()

    def _look(self, places: list[Path], recursive: bool, tool_paths: tools.Tools) -> None:
        try:
            wmv = tool_paths.asfbin is not None
            files, missing, ignored = find_video_files(places, recursive, wmv=wmv)
            with self.lock:
                self.scan["total"] = len(files)
            media, unreadable = [], []
            for number, path in enumerate(files, 1):
                if self.quit.is_set():
                    return
                try:
                    media.append(probe(path, tool_paths.ffprobe))
                except ProbeError as exc:
                    unreadable.append({"path": str(path), "error": str(exc)})
                with self.lock:
                    self.scan["done"] = number
            groups, singles = group_files(media)
            with self.lock:
                self.groups = groups
                self.rows = []
                for group in groups:
                    self.rows.append(Row(self.next_id, group, PlanItem(group, None, "skip")))
                    self.next_id += 1
                self.others = {"whole": [str(m.path) for m in singles], "ignored": [str(p) for p in ignored],
                               "unreadable": unreadable, "missing": missing, "wmv": wmv}
                self.scanned = True
                self.outputs.clear()  # a new look reads the files under the outputs' names again
                self._replan()
                ready = sum(1 for row in self.rows if self._will_merge(row))
                skipped = sum(1 for row in self.rows if row.item.action == "merge" and group_key(row.group) in self.left_out)
            self.note(f"looked in {', '.join(map(str, places))}" + (" and their subfolders" if recursive else "")
                      + f": {len(files)} video file(s) read" + (f", {len(ignored)} ignored" if ignored else ""))
            for path in missing:
                self.note(f"not found: {path}")
            for entry in unreadable:
                self.note(f"can't read {entry['path']}: {entry['error']}")
            self.note(f"found {len(groups)} video(s) in parts: {ready} to merge" + (f" ({skipped} left out)" if skipped else ""))
        except Exception as exc:  # the page must not stay "looking" forever
            log.exception("looking failed")
            self.note(f"looking failed: {exc}")
        finally:
            with self.lock:
                self.scan["running"] = False

    def _replan(self) -> None:
        """Plan the videos found again with the options now chosen (the lock is held)."""
        if not self.groups:
            return
        s = self.settings
        items = build_plan(self.groups, self.out_dir(), s.container, s.merge_again, read=self.read_output)
        for row, item in zip(self.rows, items):
            if row.state == "planned":
                row.item = item

    def read_output(self, path: Path) -> MediaFile:
        """A file already under an output's name (is it this video?), read with ffprobe once per size and time:
        the videos are planned again on every option change."""
        stat = path.stat()
        key = (os.path.normcase(str(path)), stat.st_size, stat.st_mtime_ns)
        if key not in self.outputs:
            try:
                if self.tool_paths is None:
                    raise ProbeError("ffprobe was not found")
                self.outputs[key] = probe(path, self.tool_paths.ffprobe)
            except ProbeError as exc:
                self.outputs[key] = str(exc)
        found = self.outputs[key]
        if isinstance(found, str):
            raise ProbeError(found)
        return found

    def set_options(self, data: dict) -> dict:
        s = self.settings
        with self.lock:
            if self.running:
                raise Refused("Wait until the merging is done (or use “Stop after this one”).")
            # Every value is checked before any is used: a refused change changes nothing.
            save_to = str(data.get("save_to", s.save_to))
            if save_to not in ("next", "folder"):
                raise Refused("Unknown choice.")
            container = str(data.get("container", s.container))
            if container not in ("mkv", "mp4"):
                raise Refused("Choose MKV or MP4.")
            out_dir = s.out_dir
            if "out_dir" in data:
                out_dir = str(data["out_dir"] or "").strip().strip('"').strip()
                if out_dir and not Path(out_dir).expanduser().is_absolute():
                    raise Refused("Use a full path for the folder, e.g. "
                                  + ("D:\\Videos\\Merged" if os.name == "nt" else "/home/me/Videos/Merged"))
            switches = {name: bool(data[name]) for name in SWITCHES if name in data}
            look_again = self.scanned and "subfolders" in switches and switches["subfolders"] != s.subfolders
            s.save_to, s.container, s.out_dir = save_to, container, out_dir
            for name, value in switches.items():
                setattr(s, name, value)
            if not self.scan["running"]:
                self._replan()
        self.save()
        return {"look_again": look_again}

    def clear(self) -> None:
        with self.lock:
            if self.running:
                raise Refused("Wait until the merging is done.")
            if self.scan["running"]:
                raise Refused("Wait until the look is done.")
            self.groups, self.rows, self.scanned, self.others = [], [], False, empty_others()

    # -- merging ---------------------------------------------------------------------------

    def _will_merge(self, row: Row) -> bool:
        """Ready to merge and not left out (the lock is held)."""
        return row.state == "planned" and row.item.action == "merge" and group_key(row.group) not in self.left_out

    def leave_out(self, data: dict) -> dict:
        """Leave a video out of the merging (e.g. "Film 1" and "Film 2" are two videos, not parts), or put it back.

        Only the list changes, never the files. Kept until the program closes, also after "Look again". During a
        merge, a waiting video leaves the queue, and one put back joins it."""
        out = data.get("out", True) is not False
        with self.lock:
            row = next((r for r in self.rows if str(r.id) == str(data.get("id"))), None)
            if row is None:
                raise Refused("That video is no longer in the list.")
            key = group_key(row.group)
            if out:
                if row.state == "running":
                    raise Refused("This video is being merged now: “Stop after this one” stops after it.")
                if not (row.state == "waiting" or (row.state == "planned" and row.item.action == "merge")):
                    raise Refused("Only a video that is ready to merge can be left out.")
                self.left_out.add(key)
                if row.state == "waiting":
                    row.state = "planned"
            else:
                if key not in self.left_out:
                    raise Refused("This video is not left out.")
                self.left_out.discard(key)
                if self.running and not self.stop_after and self._will_merge(row):
                    row.state, row.steps, row.progress, row.result = "waiting", [], None, None
        self.note(f"{row.group.name}: " + ("left out, not merged" if out else "put back"))
        return {"left_out": out}

    def start(self) -> int:
        with self.lock:
            if self.running:
                raise Refused("Already merging.")
            if self.scan["running"]:
                raise Refused("Wait until the look is done.")
            if self.tool_paths is None:
                raise Refused("FFmpeg is missing: see the Settings tab.")
            todo = [row for row in self.rows if self._will_merge(row)]
            if not todo:
                raise Refused("No video is ready to merge.")
            for row in todo:
                row.state, row.steps, row.progress, row.result = "waiting", [], None, None
            self.running, self.stop_after = True, False
            delete = self.settings.delete_parts
            self.worker = threading.Thread(target=self._work, args=(self.tool_paths, delete), name="merge", daemon=True)
            self.worker.start()
        self.note(f"merging {len(todo)} video(s), one at a time" + ("; the parts are deleted after each checked merge" if delete else ""))
        return len(todo)

    def stop(self) -> None:
        with self.lock:
            if not self.running:
                raise Refused("Nothing is merging.")
            self.stop_after = True
        self.note("stopping after the current video")

    def _work(self, tool_paths: tools.Tools, delete: bool) -> None:
        try:
            while not self.stop_after and not self.quit.is_set():
                with self.lock:
                    row = next((r for r in self.rows if r.state == "waiting"), None)
                    if row is None:
                        break
                    count = len(row.group.parts)
                    row.state, row.progress = "running", 0.0
                    row.steps = [f"joining {count} parts (copied, not re-encoded)"]
                    self.current = row
                target = row.item.target
                self.note(f"{row.group.name}: joining {count} parts" + (" with AsfBin" if is_wmv(row.group) else "")
                          + f" into {target}")
                result = self._merge(row, tool_paths, delete)
                with self.lock:
                    self.current = None
                    row.result = result
                    if result.ok:
                        row.state, row.progress = "done", None
                        row.steps.append(f"saved as {result.output.name}")
                        if result.deleted:
                            row.steps.append(f"deleted the {len(result.deleted)} parts")
                    else:
                        row.state, row.progress = "failed", None
                if result.ok:
                    self.note(f"{row.group.name}: saved {result.output}" + (f", {len(result.deleted)} parts deleted" if result.deleted else "")
                              + (f" ({result.message})" if "could not be deleted" in result.message else ""))
                else:
                    self.note(f"{row.group.name}: failed: {result.message}")
        finally:
            with self.lock:
                for row in self.rows:
                    if row.state == "waiting":
                        row.state = "planned"
                quitting, stopped = self.quit.is_set(), self.stop_after
                self.running, self.stop_after, self.current = False, False, None
                self._replan()  # a video merged now exists, e.g.
            self.note("stopped" if quitting else "stopped after the current video" if stopped else "all done")

    def _merge(self, row: Row, tool_paths: tools.Tools, delete: bool) -> MergeResult:
        def progress(fraction: float) -> None:
            row.progress = fraction

        def step(name: str) -> None:
            if name == CHECKING:
                with self.lock:
                    row.steps.append("checking the result: length, size, every track")
                    row.progress = 1.0

        if self.quit.is_set():
            return MergeResult(row.item, False, "stopped")
        try:
            return merge(row.item, tool_paths, delete_parts=delete, on_progress=progress, on_step=step)
        except Exception as exc:  # never let one video stop the others
            log.exception("merge failed")
            return MergeResult(row.item, False, f"unexpected error: {exc}")

    def shutdown(self, wait: float = 15.0) -> None:
        """Quit, Ctrl+C or the window closed: end the programs started (ffmpeg, ffprobe, AsfBin), let the merge stop
        and remove its unfinished file. The parts are never changed."""
        self.quit.set()
        with self.lock:
            self.stop_after = True
            row, worker = self.current, self.worker
        children.stop_all()
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(wait)
            children.stop_all()  # one started just before the first stop
        if row is not None and row.item.target is not None:
            for path in (partial_path(row.item.target), markers_path(row.item.target)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- what the page shows ---------------------------------------------------------------

    def row_view(self, row: Row) -> dict:
        group, item = row.group, row.item
        state = row.state
        if state == "planned":
            if item.action == "merge":
                state = "left_out" if group_key(group) in self.left_out else "again" if item.kind == "again" else "ready"
            elif item.action == "problem":
                state = "problem"
            else:
                state = "already" if item.kind == "already" else "skip"
        why, fix = explain(item) if state in ("problem", "skip", "already") else ("", "")
        label = {"vp9": "Stays in parts", "not_web_dl": "Not a WEB-DL"}.get(item.kind, "Not merged")
        view = {
            "id": row.id, "name": group.name, "by": "dmm" if group.from_comment else "name", "folder": str(group.folder),
            "wmv": is_wmv(group), "chips": chips(group.parts[0]), "state": state, "label": label, "why": why, "fix": fix,
            "parts": [{"number": n, "file": str(p.path), "secs": p.duration, "bytes": p.size} for n, p in zip(group.numbers, group.parts)],
            "secs": group.duration, "bytes": group.size,
            "warnings": [sentence(w) + " Check the merged file around the chapter marks." for w in item.warnings],
            "target": str(item.target) if item.target is not None else "",
            "chapters": [f"Part {n}" for n in group.numbers],
            "steps": list(row.steps), "progress": row.progress,
        }
        result = row.result
        if result is not None:
            first, _, rest = result.message.partition("\n")
            view.update(saved_as=str(result.output or ""), deleted=len(result.deleted), merged_secs=result.duration,
                        merged_bytes=result.size)
            if not result.ok:
                view.update(why=sentence(first), fix="The parts were not changed." + (" Details are in the Log tab." if rest else ""))
            elif "could not be deleted" in result.message:
                view["note"] = sentence(result.message.split("; ", 1)[1])
        return view

    def state(self, after: int = 0) -> dict:
        s = self.settings
        with self.lock:
            out_dir = self.out_dir() or (Path(s.out_dir).expanduser() if s.out_dir else config.default_out_dir())
            return {
                "version": __version__,
                "starting": not self.ready.is_set(),
                "places": [dict(place) for place in self.places],
                "scanned": self.scanned,
                "scan": dict(self.scan),
                "running": self.running,
                "stopping": self.running and self.stop_after,
                "rows": [self.row_view(row) for row in self.rows],
                "others": self.others,
                "options": {"save_to": s.save_to, "out_dir": str(out_dir), "container": s.container,
                            "subfolders": s.subfolders, "merge_again": s.merge_again, "delete_parts": s.delete_parts},
                "settings": {"ffmpeg": s.ffmpeg, "ffprobe": s.ffprobe, "asfbin": s.asfbin,
                             "settings_path": str(config.settings_path()), "log_path": str(logs.log_dir() / "video-merger.log")},
                "tools": {**{name: str(path or "") for name, path in self.found.items()},
                          "versions": dict(self.versions), "errors": dict(self.tool_errors)},
                "log": [list(line) for line in self.lines if line[0] > after],
            }


# -- folders ------------------------------------------------------------------------------------


def drives() -> list[str]:
    """Windows' drive letters, without touching the drives (an empty card reader would make us wait)."""
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        mask = (1 << 26) - 1
    return [f"{letter}:\\" for bit, letter in enumerate(string.ascii_uppercase) if mask >> bit & 1]


def browse(path: str, files: str = "") -> dict:
    """Folders (and .mkv, .mp4 and .wmv files when `files` is "video") in `path`, or in the nearest folder above it
    that exists; the drives on Windows, or home, when there is none."""
    folder = Path(path).expanduser() if path else None
    while folder is not None and not folder.is_dir():
        folder = folder.parent if folder.parent != folder else None
    if folder is None:
        if os.name == "nt":
            return {"path": "", "parent": None, "entries": [{"name": d, "path": d, "dir": True} for d in drives()]}
        folder = Path.home()
    try:
        children_ = list(folder.iterdir())
    except OSError as exc:
        raise Refused(f"Can't open {folder} ({exc.strerror or exc})") from None
    entries = []
    for child in children_:
        if child.name.startswith((".", "$")):
            continue
        try:
            if getattr(child.stat(), "st_file_attributes", 0) & 6:  # Windows' hidden and system files and folders
                continue
            is_dir = child.is_dir()
            if not is_dir and not (files == "video" and child.suffix.lower() in PLACE_FILES and ".partial." not in child.name.lower()):
                continue
            entries.append({"name": child.name, "path": str(child), "dir": is_dir, "size": None if is_dir else child.stat().st_size})
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = str(folder.parent) if folder.parent != folder else ("" if os.name == "nt" else None)
    return {"path": str(folder), "parent": parent, "entries": entries}


# -- HTTP ---------------------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # Linux/macOS: a quick restart gets the same port; Windows would share it

    app: App

    def __init__(self, address, token: str) -> None:
        super().__init__(address, Handler)
        self.token = token
        self.port = self.server_address[1]
        self.cookie = f"video_merger_{self.port}"


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "video-merger"
    sys_version = ""

    def log_message(self, format, *args) -> None:  # noqa: A002 (the base class's name)
        pass

    def _headers(self, status: int, content_type: str, length: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                                    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send(self, status: int, payload, content_type: str = "application/json", extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self._headers(status, content_type, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self) -> bool:
        # Only this computer's names (another site's name = DNS rebinding); any port, for SSH tunnels.
        host = (self.headers.get("Host") or "").lower()
        return re.fullmatch(r"(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?", host) is not None

    def _cookie_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.server.cookie and secrets.compare_digest(value, self.server.token):
                return True
        return False

    def _refuse(self, text: str) -> None:
        self._send(HTTPStatus.FORBIDDEN, f"<!doctype html><meta charset=utf-8><title>Video Merger</title>"
                   f"<p>{text}</p>".encode(), "text/html; charset=utf-8")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._refuse("Wrong address.")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/" and "key" in query:
            if not secrets.compare_digest(query["key"][0], self.server.token):
                return self._refuse("This link is from an earlier start. Use the link in Video Merger's window.")
            cookie = f"{self.server.cookie}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
        if not self._cookie_ok():
            return self._refuse("Open Video Merger with the link shown in its window.")
        if url.path in STATIC:
            name, content_type = STATIC[url.path]
            try:
                return self._send(HTTPStatus.OK, (web_dir() / name).read_bytes(), content_type)
            except OSError:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "missing file"})
        app = self.server.app
        try:
            if url.path == "/api/state":
                return self._send(HTTPStatus.OK, app.state(int((query.get("after") or ["0"])[0] or 0)))
            if url.path == "/api/browse":
                return self._send(HTTPStatus.OK, browse((query.get("path") or [""])[0], (query.get("files") or [""])[0]))
        except Refused as exc:
            return self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok() or not self._cookie_ok() or self.headers.get(HEADER) != "1":
            return self._send(HTTPStatus.FORBIDDEN, {"error": "refused"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON only"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
        if length > MAX_BODY:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
        app = self.server.app
        path = urlparse(self.path).path
        actions = {
            "/api/places/add": lambda: app.add_place(str(data.get("path") or "")),
            "/api/places/remove": lambda: app.remove_place(str(data.get("path") or "")),
            "/api/look": app.look,
            "/api/options": lambda: app.set_options(data),
            "/api/clear": app.clear,
            "/api/leave-out": lambda: app.leave_out(data),
            "/api/start": lambda: {"started": app.start()},
            "/api/stop": app.stop,
            "/api/settings": lambda: app.save_settings(data),
            "/api/doctor": app.doctor,
            "/api/quit": self._quit,
        }
        action = actions.get(path)
        if action is None:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            answer = action()
        except Refused as exc:
            return self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            log.exception("request %s failed", path)
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"unexpected error: {exc}"})
        self._send(HTTPStatus.OK, answer if isinstance(answer, dict) else {"ok": True})

    do_HEAD = do_GET

    def _quit(self) -> None:
        """Stops everything, also a merge in progress (its unfinished file is removed; the parts are never changed)."""
        app, server = self.server.app, self.server
        app.note("quit")
        app.quit.set()

        def stop() -> None:
            app.shutdown()
            server.shutdown()

        threading.Thread(target=stop, name="quit", daemon=True).start()


class PortError(Exception):
    """The port asked for (--port) can't be used; the message says what to do."""


def make_server(cfg: config.Settings, places=(), port: int = 0) -> Server:
    token = secrets.token_urlsafe(32)
    server = None
    for candidate in ([port] if port else [PORT, 0]):
        try:
            server = Server(("127.0.0.1", candidate), token)
            break
        except OSError as exc:
            if not port:
                continue  # taken (another copy?): any free port
            if exc.errno == errno.EADDRINUSE:
                raise PortError(f"Port {port} is in use, maybe by another copy of Video Merger: close it, "
                                "or start without --port to use a free port.") from None
            raise PortError(f"Port {port} can't be used ({exc.strerror or exc}): start without --port to use a free port.") from None
    if server is None:
        raise OSError("no free port")
    server.app = App(cfg, places)
    return server


def stop_on_close(app: App, server: Server):
    """Closing the console window (Windows), the terminal (SIGHUP) or SIGTERM: stop like Quit before the program ends.

    Windows ends the program a few seconds after the window is closed: the merge is stopped and its unfinished
    file removed within that time."""
    def stop() -> None:
        app.shutdown(wait=3)
        threading.Thread(target=server.shutdown, daemon=True).start()

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            def handler(event: int) -> bool:
                # Ctrl+Break, window closed, logging off, shutting down. Ctrl+C: Python's own KeyboardInterrupt.
                if event in (1, 2, 5, 6):
                    stop()
                    return True
                return False

            callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)
            _close_handlers.append(callback)  # kept for good: Windows may call it until the program has ended
        except (OSError, AttributeError):
            pass
        return
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name) and threading.current_thread() is threading.main_thread():
            signal.signal(getattr(signal, name), lambda *_: threading.Thread(target=stop, daemon=True).start())


_close_handlers: list = []


def serve(cfg: config.Settings, places=(), port: int = 0, open_browser: bool = True) -> int:
    server = make_server(cfg, places, port)
    app = server.app
    threading.Thread(target=app.startup, name="startup", daemon=True).start()
    url = f"http://127.0.0.1:{server.port}/?key={server.token}"
    app.note(f"Video Merger {__version__} started")
    print(f"Video Merger {__version__} is running in your browser:\n\n    {url}\n")
    print("Keep this window open while you merge. Closing it (or Ctrl+C) stops Video Merger:\n"
          "a merge in progress is removed, the parts are never changed.", flush=True)
    stop_on_close(app, server)
    has_screen = os.name == "nt" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if open_browser and has_screen:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    elif open_browser:
        print(f"No screen here: open the address above in a browser on your computer. Over SSH, first run there: "
              f"ssh -L {server.port}:127.0.0.1:{server.port} <you>@<this machine>", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        app.shutdown()
    return 0
