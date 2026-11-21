"""Finding video files and grouping the parts of each video in order."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .probe import MediaFile

VIDEO_EXTENSIONS = {".mkv", ".mp4"}
WMV_EXTENSION = ".wmv"  # joined with AsfBin when it is there (merge.py), else ignored like the formats below
# Reported as ignored: disc files and other formats aren't DMM WEB-DL parts (WMV only without AsfBin).
IGNORED_VIDEO_EXTENSIONS = {".wmv", ".avi", ".flv", ".mov", ".webm", ".ts", ".m2ts", ".mts", ".vob", ".iso", ".mpg", ".mpeg"}

# Part numbers in file names, for files without DMM's comment tag:
# "abc00123-pt2", "abc00123 part 2", "abc00123.cd2", "name part ii", "abc00123_2", "ABC-123-2", "ABC-123-B".
# "ABC-012" (a DVD id) must not count as part 12: a bare number needs "_", or "-" after a whole
# DVD id, and no leading zero. The name before the part number is kept exactly (no padding
# changes), so "sone096_1" and "sone00096_1" are different videos. The name needs a letter of any
# script ("動画_2" too), so a date like "2023_1" is no part. "part", "cd" ... don't count inside a word
# ("abcd00001" is a content id, not part 1 of "ab").
# DMM downloads are matched by their comment tag instead, whatever DMM's
# suffix in the name ("hhb1", "hhbs2", "mhb1", "2d_v1_drm_a_4k1", ...).
LETTER = r"[^\W\d_]"  # a letter of any script: not a digit, "_" or a symbol
PART_WORD = rf"(?<!{LETTER})(?:part|pt|cd|disc|disk)"
ROMAN = r"(?=[ivx])(?:x|ix|iv|v?i{0,3})"  # i to x
ROMAN_NUMBERS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
NAME_PATTERNS = (
    re.compile(rf"^(?P<base>.*?{LETTER}.*?)[\s._-]*{PART_WORD}[\s._-]*0*(?P<n>[1-9]\d{{0,2}})$", re.IGNORECASE),
    re.compile(rf"^(?P<base>.*?{LETTER}.*?)[\s._-]+{PART_WORD}[\s._-]+(?P<roman>{ROMAN})$", re.IGNORECASE),
    re.compile(rf"^(?P<base>.*?{LETTER}.*?)_(?P<n>[1-9]\d?)$", re.IGNORECASE),
    re.compile(r"^(?P<base>[a-z0-9]*[a-z][a-z0-9]*-\d+[a-z]?)-(?P<n>[1-9]\d?)$", re.IGNORECASE),
    re.compile(r"^(?P<base>[a-z0-9]*[a-z][a-z0-9]*-\d+)[\s_-](?P<letter>[a-d])$", re.IGNORECASE),
)
# Looser forms, counted only when part 1 and at least one more part of the same name are in one folder
# (one file alone is a whole video): "film006-1", "Name 2", "Name - 3" at the end, and a roman numeral or
# "(part 2)" in brackets anywhere: "[PPV1007] Name (ii)(Actress)". A bare "(2)" is not a part: Windows and
# browsers name second copies of a file that way.
LOOSE_END = re.compile(rf"^(?P<base>.*?{LETTER}.*?)(?:\s*[-.]\s*|\s+)(?P<n>[1-9]\d?)$", re.IGNORECASE)
LOOSE_BRACKETS = re.compile(
    rf"[(\[]\s*(?:{PART_WORD}[\s._-]*(?P<n>[1-9]\d?)|(?:{PART_WORD}[\s._-]*)?(?P<roman>{ROMAN}))\s*[)\]]",
    re.IGNORECASE,
)
SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass
class Group:
    """The parts of one video, in order."""

    name: str  # output file name without extension: the content id, or the shared file name
    folder: Path  # folder of the first part
    parts: list[MediaFile]
    numbers: list[int]
    from_comment: bool
    problems: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(p.size for p in self.parts)

    @property
    def duration(self) -> float:
        return sum(p.duration for p in self.parts)


def is_partial(path: Path) -> bool:
    """Unfinished output of this tool ("<name>.partial.mkv"), never a part."""
    return ".partial." in path.name.lower()


def is_candidate(path: Path, wmv: bool = False) -> bool:
    """A file that may be a part: .mkv and .mp4, and .wmv when AsfBin can join them (`wmv`)."""
    suffix = path.suffix.lower()
    return (
        (suffix in VIDEO_EXTENSIONS or (wmv and suffix == WMV_EXTENSION))
        and not path.name.startswith(".")
        and not is_partial(path)
    )


def find_video_files(paths: list[Path], recursive: bool = False, wmv: bool = False) -> tuple[list[Path], list[str], list[Path]]:
    """Video files given directly or inside the given folders, the paths that don't exist, and
    the video files of other formats (e.g. .avi, and .wmv without AsfBin: `wmv` False) that are left alone."""
    found: dict[Path, None] = {}
    ignored: dict[Path, None] = {}
    missing = []

    def consider(path: Path) -> None:
        if is_candidate(path, wmv):
            found[path.resolve()] = None
        elif path.suffix.lower() in IGNORED_VIDEO_EXTENSIONS and not is_partial(path):
            ignored[path.resolve()] = None

    for given in paths:
        if given.is_file():
            consider(given)
        elif given.is_dir():
            entries = given.rglob("*") if recursive else given.iterdir()
            for entry in sorted(entries):
                if entry.is_file():
                    consider(entry)
        else:
            missing.append(str(given))
    return list(found), missing, list(ignored)


def _number(match: re.Match) -> int:
    groups = match.groupdict()
    if groups.get("n"):
        return int(groups["n"])
    if groups.get("roman"):
        return ROMAN_NUMBERS[groups["roman"].lower()]
    return ord(groups["letter"].lower()) - ord("a") + 1


def part_from_name(stem: str) -> tuple[str, int] | None:
    """The name and part number when the name says so clearly ("abc00123-pt2"), or None."""
    for pattern in NAME_PATTERNS:
        match = pattern.match(stem.strip())
        if match:
            base = match.group("base").rstrip(" ._-")
            if base:
                return base, _number(match)
    return None


def loose_part_from_name(stem: str) -> tuple[str, int] | None:
    """The name and part number of the looser forms ("film006-1", "Name 2", "Name (ii) more"), or None.

    Only counted when other parts of the same name are there too (group_files)."""
    stem = stem.strip()
    match = LOOSE_END.match(stem)
    if match:
        base = match.group("base").rstrip(" ._-")
        return (base, _number(match)) if base else None
    found = list(LOOSE_BRACKETS.finditer(stem))
    if found:
        match = found[-1]  # the part comes after the title: "(ii)" in "[PPV1007] Name (ii)(Actress)"
        base = re.sub(r"\s{2,}", " ", stem[:match.start()] + stem[match.end():]).strip(" ._-")
        if re.search(LETTER, base):
            return base, _number(match)
    return None


def safe_name(name: str) -> str:
    return SAFE_NAME_RE.sub("_", name).strip(" .") or "merged"


def joined_wmv(media: MediaFile) -> bool:
    """A .wmv named exactly after the content id in its comment tag: the joined file of an earlier merge.

    AsfBin may copy the first part's comment tag (with "part": "1") into the joined file, which is named
    after the content id; ffmpeg merges write the content id without a part number."""
    return media.path.suffix.lower() == WMV_EXTENSION and media.file_id is not None and media.path.stem.lower() == media.file_id


def group_files(files: list[MediaFile]) -> tuple[list[Group], list[MediaFile]]:
    """Group parts by DMM content id (comment tag) or else by file name.

    Returns the groups and the files that are not parts of a multi-part video. A file with a
    content id but no part number is a whole video (for example an earlier merge), not a part,
    whatever its name ("abc00123_2.mkv" may be a second copy).
    Files named in the looser forms (loose_part_from_name) are parts only when part 1 and at least one
    more part of the same name are there; otherwise they are whole videos.
    """
    buckets: dict[tuple, list[tuple[int, MediaFile]]] = {}
    names: dict[tuple, tuple[str, bool]] = {}
    clear: set[tuple] = set()  # groups with at least one part found by DMM's tag or a clear name
    singles = []
    for media in files:
        if joined_wmv(media):
            singles.append(media)
            continue
        from_name = part_from_name(media.path.stem)
        if media.file_id and media.part:
            key = ("id", media.file_id)
            number = media.part
            names[key] = (media.file_id, True)
            clear.add(key)
        elif not media.file_id and (from_name or (loose := loose_part_from_name(media.path.stem))):
            base, number = from_name or loose
            key = ("name", str(media.path.parent).lower(), base.lower())
            names.setdefault(key, (safe_name(base), False))
            if from_name:
                clear.add(key)
        else:
            singles.append(media)
            continue
        buckets.setdefault(key, []).append((number, media))

    groups = []
    for key, entries in buckets.items():
        entries.sort(key=lambda entry: (entry[0], str(entry[1].path)))
        numbers = [n for n, _ in entries]
        parts = [m for _, m in entries]
        name, from_comment = names[key]
        if key not in clear and (len(entries) < 2 or 1 not in numbers):
            singles.extend(parts)  # "Name 2" alone, or "Episode 5" and "Episode 6": not parts
            continue
        if numbers == [1]:
            singles.append(parts[0])  # a one-part video: nothing to merge
            continue
        group = Group(name=name, folder=parts[0].path.parent, parts=parts, numbers=numbers, from_comment=from_comment)
        seen: dict[int, list[MediaFile]] = {}
        for n, media in entries:
            seen.setdefault(n, []).append(media)
        for n, copies in seen.items():
            if len(copies) > 1:
                group.problems.append(f"part {n} was found {len(copies)} times: " + ", ".join(str(c.path) for c in copies))
        missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        if missing:
            group.problems.append("missing part(s) " + ", ".join(map(str, missing)))
        groups.append(group)
    groups.sort(key=lambda g: (str(g.folder).lower(), g.name.lower()))
    return groups, singles
