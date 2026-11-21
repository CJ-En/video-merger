"""DMM's own part list: how many parts each video has on DMM and how long each one is.

The file names and comment tags only show which parts are here, not whether the last one is missing.
With `--dmm-parts`, a video is only merged when its parts match a CSV list of DMM's parts: the same
number of parts and each part as long as DMM's (when the list has the lengths). Its columns (others
are ignored):

- ids: the video's content ids, separated by spaces; lines with the same first id are one video
- page_id, file_id (optional): more ids the video is found under (DMM's page, the parts' comment tag)
- parts: how many parts DMM has; empty when unknown
- rates (optional): the part count at each bitrate, "3000:3 300:2"
- part, duration_s (optional): a part's number and its length in seconds, one line per part
- error: why the part count (or that part's length) is unknown
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .discover import Group
from .probe import MediaFile


@dataclass
class DmmVideo:
    parts: int | None  # None: DMM didn't say (see error)
    rates: str = ""  # "3000:2 6000:2": parts at each bitrate
    lengths: dict[int, float] = field(default_factory=dict)  # part number -> seconds
    error: str = ""


def load(path: Path) -> dict[str, DmmVideo]:
    """{content id: DmmVideo}, under every id of a line (the ids tried, DMM's page id, the parts' file id)."""
    videos: dict[str, DmmVideo] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ids = row["ids"].lower().split()
            video = videos.get(ids[0]) if ids else None
            if video is None:
                video = DmmVideo(None)
            parts = row.get("parts") or ""
            if video.parts is None and parts.isdigit():  # a later line (another list) may give the count
                video.parts, video.rates, video.error = int(parts), row.get("rates") or "", ""
            if video.parts is None and not video.error:
                video.error = row.get("error") or "no part count"
            part, seconds = row.get("part") or "", row.get("duration_s") or ""
            if part.isdigit() and seconds and not row.get("error"):
                video.lengths[int(part)] = float(seconds)
            for key in (*ids, row.get("page_id"), row.get("file_id")):
                if key:
                    videos[key.lower()] = video
    return videos


def length_tolerance(seconds: float) -> float:
    return max(2.0, seconds * 0.005)


def hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def check(name: str, numbers: list[int], parts: list[MediaFile], dmm: dict[str, DmmVideo]) -> str | None:
    """Why these parts don't match DMM's list, or None."""
    video = dmm.get(name.lower())
    if video is None:
        return "not in the DMM parts list, so it can't be checked against DMM"
    if video.parts is None:
        return f"DMM's part count is unknown ({video.error})"
    expected = list(range(1, video.parts + 1))
    if numbers != expected:
        found = ", ".join(map(str, numbers))
        text = f"DMM has {video.parts} part(s), found part(s) {found}"
        counts = {count for _, _, count in (rate.partition(":") for rate in video.rates.split())}
        if len(counts) > 1:
            text += f" (DMM's parts per bitrate: {video.rates})"
        return text
    for number, part in zip(numbers, parts):
        dmm_seconds = video.lengths.get(number)
        if dmm_seconds and abs(part.duration - dmm_seconds) > length_tolerance(dmm_seconds):
            return f"part {number} is {hms(part.duration)} long, DMM's part {number} is {hms(dmm_seconds)}"
    return None


def check_group(group: Group, dmm: dict[str, DmmVideo]) -> str | None:
    if not group.from_comment:
        return "no DMM comment tag in the parts, so it can't be checked against DMM"
    return check(group.name, group.numbers, group.parts, dmm)


def lone_parts(singles: list[MediaFile], dmm: dict[str, DmmVideo]) -> list[tuple[MediaFile, str]]:
    """Files tagged as a part whose video DMM has in several parts: the only part here (e.g. part 1 of 2)."""
    found = []
    for media in singles:
        video = dmm.get(media.file_id or "")
        if media.part and video is not None and video.parts and video.parts > 1:
            found.append((media, f"only part {media.part} of {media.file_id} is here, DMM has {video.parts} parts"))
    return found
