"""Deciding what to do with each group: merge, skip or report a problem."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import dmm_parts
from .discover import WMV_EXTENSION, Group
from .probe import MediaFile, ProbeError, same_rate

AV_DRIFT_WARNING = 2.0  # seconds between a part's audio and video length

# Only WEB-DL parts are merged (DMM: H.264 or HEVC video, AAC audio). Disc Remuxes/rips and WMV are not.
WEB_VIDEO = {"h264", "hevc"}
WEB_AUDIO = {"aac"}
WMV_VIDEO = {"wmv1", "wmv2", "wmv3", "vc1", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3"}
# .wmv parts are joined with AsfBin (merge.py) when they hold Windows Media video and sound only.
ASF_VIDEO = {"wmv1", "wmv2", "wmv3", "vc1"}
ASF_AUDIO = {"wmav1", "wmav2", "wmapro"}
DISC_SUBTITLES = {"hdmv_pgs_subtitle", "dvd_subtitle"}
DISC_WORDS = re.compile(
    r"(?<![a-z0-9])(remux|bdremux|blu-?ray|bdrip|bdmv|bd(?:25|50|66|100)?|dvd(?:rip|remux|5|9)?|iso)(?![a-z0-9])",
    re.IGNORECASE,
)


@dataclass
class PlanItem:
    group: Group
    target: Path | None
    action: str  # "merge", "skip" or "problem"
    reason: str = ""
    warnings: tuple[str, ...] = ()
    # Which rule decided (the page explains each): "" or "again" (the output is there and is made again) for a
    # merge; "vp9", "not_web_dl", "already" for a skip; "formats", "containers", "codecs", "parts", "dmm", "mismatch",
    # "empty", "name_is_part", "clash", "taken" (a file of the output's name is another video) for a problem.
    kind: str = ""
    # "again": the size and modification time of the output checked to be this video. merge() only replaces that
    # file (file_identity), never one that appeared or changed after the plan.
    replaces: tuple[int, int] | None = None


def duration_tolerance(total: float) -> float:
    """How far the merged length may be from the parts' sum."""
    return max(2.0, total * 0.005)


def size_tolerance(total: int) -> int:
    """How far the merged size may be from the parts' sum: the streams are copied, only the container differs."""
    return max(1 << 20, total // 100)


def file_identity(path: Path) -> tuple[int, int] | None:
    """A file's size and modification time (to see that it is still the file checked earlier), or None."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def is_vp9(group: Group) -> bool:
    return any(part.video and part.video.codec == "vp9" for part in group.parts)


def is_wmv(group: Group) -> bool:
    """Every part is a .wmv: joined with AsfBin into a .wmv (never re-encoded into MKV/MP4)."""
    return all(part.path.suffix.lower() == WMV_EXTENSION for part in group.parts)


def mixed_formats(group: Group) -> str | None:
    """ "WMV and MP4" when some parts are .wmv and others aren't (they can't be joined without re-encoding), or None."""
    kinds = {part.path.suffix.lower().lstrip(".").upper() for part in group.parts}
    if "WMV" not in kinds or len(kinds) == 1:
        return None
    names = ["WMV", *sorted(kinds - {"WMV"})]
    return ", ".join(names[:-1]) + " and " + names[-1]


def mixed_containers(group: Group) -> str | None:
    """Why the parts can't be joined as they are because they are different kinds of file inside (Matroska and MP4,
    whatever their extensions), or None. ffmpeg's concat then drops what follows the first part (also for H.264)."""
    first = group.parts[0].container
    for number, part in zip(group.numbers[1:], group.parts[1:]):
        if first and part.container and part.container != first:
            return f"part {number} is {part.container} and part {group.numbers[0]} is {first}: remux them to the same kind first"
    return None


def disc_name(group: Group) -> str | None:
    """Why the names say Blu-ray/DVD Remux or rip, or None."""
    for part in group.parts:
        for name in (part.path.stem, part.path.parent.name):
            match = DISC_WORDS.search(name)
            if match:
                return f'"{match.group(0)}" in the name: Blu-ray/DVD Remuxes and rips are not merged'
    return None


def not_asf(group: Group) -> str | None:
    """Why .wmv parts can't be joined with AsfBin (other codecs inside), or None."""
    for part in group.parts:
        for stream in part.streams:
            if stream.kind == "video" and stream.codec not in ASF_VIDEO:
                return f"{stream.codec} video inside a .wmv file: only WMV or VC-1 video with WMA sound is joined"
            if stream.kind == "audio" and stream.codec not in ASF_AUDIO:
                return f"{stream.codec} sound inside a .wmv file: only WMV or VC-1 video with WMA sound is joined"
    return None


def not_web_dl(group: Group) -> str | None:
    """Why these parts look like a disc Remux/rip or WMV rather than a WEB-DL, or None."""
    disc = disc_name(group)
    if disc:
        return disc
    for part in group.parts:
        for stream in part.streams:
            if stream.kind == "video" and stream.codec in WMV_VIDEO:
                return f"{stream.codec} video (WMV/VC-1) is not merged"
            if stream.kind == "video" and stream.codec not in WEB_VIDEO:
                return f"{stream.codec} video is not merged (only H.264/HEVC WEB-DLs)"
            if stream.kind == "audio" and stream.codec not in WEB_AUDIO:
                return f"{stream.codec} audio looks like a Blu-ray/DVD Remux, not merged (only AAC WEB-DLs)"
            if stream.kind == "subtitle" and stream.codec in DISC_SUBTITLES:
                return f"{stream.codec} subtitles look like a Blu-ray/DVD Remux, not merged"
    return None


def stream_mismatch(group: Group) -> str | None:
    """Why the parts can't be joined without re-encoding, or None."""
    first = group.parts[0]
    for number, part in zip(group.numbers[1:], group.parts[1:]):
        if len(part.streams) != len(first.streams):
            return f"part {number} has {len(part.streams)} streams, part {group.numbers[0]} has {len(first.streams)}"
        for a, b in zip(first.streams, part.streams):
            if a.signature() != b.signature() or not same_rate(a.frame_rate, b.frame_rate):
                only_settings = replace(a, settings=b.settings).signature() == b.signature() and same_rate(a.frame_rate, b.frame_rate)
                if a.settings != b.settings and only_settings:
                    return f"part {number}'s HEVC video was encoded with other settings than part {group.numbers[0]}'s"
                return f"part {number} has {b.describe()}, part {group.numbers[0]} has {a.describe()}"
    return None


def drift_warnings(group: Group) -> tuple[str, ...]:
    warnings = []
    for number, part in zip(group.numbers, group.parts):
        video, audio = part.video, part.audio
        if video and audio and video.duration and audio.duration:
            gap = abs(video.duration - audio.duration)
            if gap > AV_DRIFT_WARNING:
                warnings.append(
                    f"part {number}: sound and picture lengths differ by {gap:.1f} s, "
                    "so the sound may be out of sync after the join"
                )
    return tuple(warnings)


def other_video(group: Group, target: Path, read: Callable[[Path], MediaFile] | None) -> str | None:
    """Why the file already at `target` is not this video, or None when it is: for parts found by DMM's tag, the same
    content id in its tag; for parts found by name, as long and as big as the parts together, as merge() checks its
    result (so is a .wmv without a tag: AsfBin may not copy it into the joined file). Another video is never taken for
    this one merged, nor replaced."""
    if read is None:
        return f"{target} is already there and wasn't read, so it may be another video"
    try:
        there = read(target)
    except (ProbeError, OSError) as exc:
        return f"{target} is already there and can't be read ({exc}), so it may be another video"
    if group.from_comment and (there.file_id or target.suffix.lower() != WMV_EXTENSION):
        if there.file_id != group.name:
            tag = f"its DMM tag says {there.file_id}" if there.file_id else "it has no DMM tag"
            return f"{target} is already there, but it is another video ({tag})"
    elif abs(there.duration - group.duration) > duration_tolerance(group.duration):
        return (f"{target} is already there, but it is another video ({dmm_parts.hms(there.duration)} long, "
                f"the parts together {dmm_parts.hms(group.duration)})")
    elif abs(there.size - group.size) > size_tolerance(group.size):
        return (f"{target} is already there, but it is another video ({there.size / 1e9:.2f} GB, "
                f"the parts together {group.size / 1e9:.2f} GB)")
    return None


def plan_group(group: Group, out_dir: Path | None, container: str, overwrite: bool,
               dmm: dict[str, dmm_parts.DmmVideo] | None = None, read: Callable[[Path], MediaFile] | None = None) -> PlanItem:
    formats = mixed_formats(group)
    if formats:
        return PlanItem(group, None, "problem", f"the parts are in different formats ({formats})", kind="formats")
    wmv = is_wmv(group)
    # WMV parts always give a .wmv (AsfBin, no re-encoding), whatever MKV/MP4 is chosen.
    target = (out_dir or group.folder) / f"{group.name}.{'wmv' if wmv else container}"
    if wmv:
        disc = disc_name(group)
        if disc:
            return PlanItem(group, None, "skip", disc, kind="not_web_dl")
        codecs = not_asf(group)
        if codecs:
            return PlanItem(group, None, "problem", codecs, kind="codecs")
    else:
        if is_vp9(group):
            return PlanItem(group, None, "skip", "VP9 video: the parts stay separate files", kind="vp9")
        other = not_web_dl(group)
        if other:
            return PlanItem(group, None, "skip", other, kind="not_web_dl")
    containers = mixed_containers(group)
    if containers:
        return PlanItem(group, None, "problem", containers, kind="containers")
    if group.problems:
        return PlanItem(group, target, "problem", "; ".join(group.problems), kind="parts")
    if dmm is not None:
        differs = dmm_parts.check_group(group, dmm)
        if differs:
            return PlanItem(group, target, "problem", differs, kind="dmm")
    mismatch = stream_mismatch(group)
    if mismatch:
        return PlanItem(group, target, "problem", f"the parts don't match ({mismatch})", kind="mismatch")
    empty = [str(n) for n, p in zip(group.numbers, group.parts) if p.duration <= 0]
    if empty:
        return PlanItem(group, target, "problem", f"part(s) {', '.join(empty)} have no length (damaged?)", kind="empty")
    if any(p.path.resolve() == target.resolve() for p in group.parts):
        return PlanItem(group, target, "problem", f"the output name {target.name} is one of the parts; use --out",
                        kind="name_is_part")
    if target.exists():
        identity = file_identity(target)  # before reading: a file changed after this is never replaced
        other = other_video(group, target, read)
        if other:
            return PlanItem(group, target, "problem", other, kind="taken")
        if not overwrite:
            return PlanItem(group, target, "skip", f"already merged: {target} (use --overwrite to redo it)", kind="already")
        return PlanItem(group, target, "merge", warnings=drift_warnings(group), kind="again", replaces=identity)
    return PlanItem(group, target, "merge", warnings=drift_warnings(group))


def build_plan(groups: list[Group], out_dir: Path | None, container: str = "mkv", overwrite: bool = False,
               dmm: dict[str, dmm_parts.DmmVideo] | None = None, read: Callable[[Path], MediaFile] | None = None) -> list[PlanItem]:
    """The plan for each group. `read` reads a file already there under an output's name (probe.probe), to see whether
    it is this video merged; without it such a file is never skipped nor replaced."""
    items = [plan_group(g, out_dir, container, overwrite, dmm, read) for g in groups]
    # Two groups writing the same file (same name in one output folder): refuse both.
    targets: dict[str, list[PlanItem]] = {}
    for item in items:
        if item.action == "merge":
            targets.setdefault(str(item.target).lower(), []).append(item)
    for clashing in targets.values():
        if len(clashing) > 1:
            for item in clashing:
                item.action, item.kind = "problem", "clash"
                item.reason = f"{len(clashing)} different videos would be saved as {item.target}"
    return items
