"""Reading a video's streams, length and DMM part data with ffprobe."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import tools

# DMM content ids, e.g. "abc00123", "1sdde00301", "h_51cmn001".
CONTENT_ID_RE = re.compile(r"^[a-z0-9_]{1,80}$")
PROBE_TIMEOUT = 120
# HEVC NAL unit types of the parameter sets (VPS, SPS, PPS): what a decoder needs from the stream's header.
HEVC_PARAMETER_SETS = {32, 33, 34}


class ProbeError(Exception):
    """ffprobe could not read a file."""


@dataclass(frozen=True)
class Stream:
    index: int
    kind: str  # video, audio, subtitle, ...
    codec: str
    profile: str = ""
    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    frame_rate: str = ""
    sample_rate: str = ""
    channels: int = 0
    duration: float | None = None
    # HEVC: a hash of the parameter sets in the stream's header (hevc_settings). ffmpeg's concat keeps part 1's header
    # for the whole file, which only works for HEVC parts encoded with the same settings (for H.264 it puts each
    # part's settings into the stream itself). Empty for other codecs.
    settings: str = ""

    def signature(self) -> tuple:
        """What must be equal in every part for a merge without re-encoding (the frame rate: see same_rate)."""
        if self.kind == "video":
            return (self.kind, self.codec, self.profile, self.width, self.height, self.pix_fmt, self.settings)
        if self.kind == "audio":
            return (self.kind, self.codec, self.profile, self.sample_rate, self.channels)
        return (self.kind, self.codec)

    def describe(self) -> str:
        if self.kind == "video":
            fps = format_rate(self.frame_rate)
            return f"video {self.codec} {self.profile} {self.width}x{self.height} {self.pix_fmt} {fps} fps".replace("  ", " ")
        if self.kind == "audio":
            return f"audio {self.codec} {self.profile} {self.sample_rate} Hz {self.channels} ch".replace("  ", " ")
        return f"{self.kind} {self.codec}"


@dataclass(frozen=True)
class MediaFile:
    path: Path
    size: int
    duration: float
    streams: tuple[Stream, ...]
    file_id: str | None = None  # from DMM's comment tag in its downloads
    part: int | None = None
    container: str = ""  # what ffprobe found inside, whatever the extension: "Matroska", "MP4", ... (container_name)

    @property
    def video(self) -> Stream | None:
        return next((s for s in self.streams if s.kind == "video"), None)

    @property
    def audio(self) -> Stream | None:
        return next((s for s in self.streams if s.kind == "audio"), None)


def container_name(format_name) -> str:
    """The kind of file from ffprobe's format_name ("matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "asf"): "Matroska",
    "MP4", "ASF", else ffprobe's first name in capitals; "" when ffprobe gave none."""
    names = [name.strip() for name in str(format_name or "").split(",") if name.strip()]
    if "matroska" in names:
        return "Matroska"
    if "mp4" in names or "mov" in names:
        return "MP4"
    return names[0].upper() if names else ""


def parse_duration(value) -> float | None:
    """Seconds from ffprobe's "8939.798" or Matroska's tag "02:28:59.798000000"."""
    if value in (None, "", "N/A"):
        return None
    text = str(value).strip()
    try:
        if ":" in text:
            hours, minutes, seconds = text.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return float(text)
    except ValueError:
        return None


def format_rate(rate: str) -> str:
    try:
        value = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return rate or "?"
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


# ffprobe estimates a Matroska file's frame rate from its millisecond timestamps, so one 29.97 fps
# source shows as 30000/1001, 16543/552 or 4853/162 (29.970, 29.969, 29.957) in different parts.
# Real differences (29.97 vs 30, 23.976 vs 24) are only 0.1% apart, so an estimate near a standard
# rate counts as that rate; other rates must match within RATE_TOLERANCE.
RATE_TOLERANCE = 1e-4
STANDARD_RATES = tuple(
    rate
    for n in (10, 12, 15, 24, 25, 30, 48, 50, 60, 72, 90, 100, 120)
    for rate in (Fraction(n), Fraction(n * 1000, 1001))
)
SNAP_DISTANCE = Fraction(1, 200)  # 0.5%


def nominal_rate(rate: Fraction) -> Fraction:
    """The standard rate closest to an estimate (within 0.5%), or the estimate itself."""
    best = min(STANDARD_RATES, key=lambda standard: abs(rate - standard))
    return best if abs(rate - best) <= best * SNAP_DISTANCE else rate


def same_rate(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        x, y = Fraction(a), Fraction(b)
    except (ValueError, ZeroDivisionError):
        return False
    if x <= 0 or y <= 0:
        return False
    x, y = nominal_rate(x), nominal_rate(y)
    return abs(x - y) / max(x, y) <= RATE_TOLERANCE


def dump_bytes(dump) -> bytes | None:
    """The bytes of ffprobe's -show_data dump ("00000000: 0101 6000 ...  ..`..."), or None when it isn't one."""
    if not isinstance(dump, str):
        return None
    data = bytearray()
    for line in dump.splitlines():
        if not line.strip():
            continue
        # "%08x: ", then up to 16 bytes as hex in pairs, padded to 41 characters, then the bytes as text.
        if not re.fullmatch(r"[0-9a-f]{8}: ", line[:10]):
            return None
        hex_text = line[10:51].replace(" ", "")
        if not re.fullmatch(r"(?:[0-9a-f]{2}){1,16}", hex_text):
            return None
        data += bytes.fromhex(hex_text)
    return bytes(data) or None


def _hevc_units(data: bytes) -> tuple[int, list[bytes]] | None:
    """The NAL length size and the NAL units of an HEVC header: hvcC (MP4, Matroska) or start codes, or None."""
    if data[:1] == b"\x01":  # hvcC: 23 bytes of fields, then arrays of NAL units
        if len(data) < 23:
            return None
        units, at = [], 23
        for _ in range(data[22]):
            if at + 3 > len(data):
                return None
            count = int.from_bytes(data[at + 1:at + 3], "big")
            at += 3
            for _ in range(count):
                length = int.from_bytes(data[at:at + 2], "big")
                unit = data[at + 2:at + 2 + length]
                if at + 2 > len(data) or len(unit) != length:
                    return None
                units.append(unit)
                at += 2 + length
        return (data[21] & 3) + 1, units
    if data[:3] == b"\0\0\x01" or data[:4] == b"\0\0\0\x01":
        # A NAL unit never ends with a zero byte: trailing zeros belong to the next start code ("00 00 00 01").
        return 0, [unit.rstrip(b"\0") for unit in data.split(b"\0\0\x01") if unit.rstrip(b"\0")]
    return None


def hevc_settings(data: bytes | None) -> str:
    """A hash of what an HEVC decoder takes from the stream's header: the parameter sets and the NAL length size.

    Encoder messages in the header are left out (x265 writes its options there, with the computer's thread count),
    and so are the flags that MP4 and Matroska write differently. A header that can't be read is hashed whole."""
    if not data:
        return ""
    found = _hevc_units(data)
    if found is None:
        return hashlib.sha256(data).hexdigest()
    length_size, units = found
    sets = sorted(unit for unit in units if len(unit) >= 2 and (unit[0] >> 1) & 0x3F in HEVC_PARAMETER_SETS)
    digest = hashlib.sha256(bytes([length_size]))
    for unit in sets:
        digest.update(len(unit).to_bytes(4, "big") + unit)
    return digest.hexdigest()


def parse_dmm_comment(text) -> tuple[str | None, int | None]:
    """DMM files carry {"file_id": "<content id>", "part": "2"} in their comment tag."""
    if not text:
        return None, None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    file_id = str(data.get("file_id") or "").strip().lower()
    file_id = file_id if CONTENT_ID_RE.fullmatch(file_id) else None
    try:
        part = int(str(data.get("part")).strip())
    except (TypeError, ValueError):
        part = None
    if part is not None and part < 1:
        part = None
    return file_id, part


def _lower_keys(tags) -> dict:
    return {str(k).lower(): v for k, v in (tags or {}).items()}


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_probe(path: Path, size: int, data: dict) -> MediaFile:
    fmt = data.get("format") or {}
    streams = []
    for raw in data.get("streams") or []:
        tags = _lower_keys(raw.get("tags"))
        streams.append(
            Stream(
                index=_int(raw.get("index")),
                kind=str(raw.get("codec_type") or "unknown"),
                codec=str(raw.get("codec_name") or "unknown"),
                profile=str(raw.get("profile") or ""),
                width=_int(raw.get("width")),
                height=_int(raw.get("height")),
                pix_fmt=str(raw.get("pix_fmt") or ""),
                frame_rate=str(raw.get("r_frame_rate") or ""),
                sample_rate=str(raw.get("sample_rate") or ""),
                channels=_int(raw.get("channels")),
                duration=parse_duration(raw.get("duration")) or parse_duration(tags.get("duration")),
                settings=hevc_settings(dump_bytes(raw.get("extradata"))) if raw.get("codec_name") == "hevc" else "",
            )
        )
    file_id, part = parse_dmm_comment(_lower_keys(fmt.get("tags")).get("comment"))
    return MediaFile(
        path=path,
        size=size,
        duration=parse_duration(fmt.get("duration")) or 0.0,
        streams=tuple(streams),
        file_id=file_id,
        part=part,
        container=container_name(fmt.get("format_name")),
    )


def _run_ffprobe(cmd: list) -> dict:
    try:
        result = tools.run(cmd, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe took longer than {PROBE_TIMEOUT} s") from exc
    except OSError as exc:
        raise ProbeError(f"ffprobe could not be started: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ProbeError(message[-1] if message else f"ffprobe failed (exit code {result.returncode})")
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ProbeError("ffprobe gave unreadable output") from exc
    return data if isinstance(data, dict) else {}


def probe(path: Path, ffprobe: Path) -> MediaFile:
    data = _run_ffprobe([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    streams = data.get("streams") or []
    if any(raw.get("codec_name") == "hevc" for raw in streams):
        # The HEVC header's bytes, for the video streams only (a Matroska file's attached fonts count as "extradata"
        # too, and can be megabytes).
        cmd = [ffprobe, "-v", "error", "-print_format", "json", "-select_streams", "v",
               "-show_entries", "stream=index,extradata", "-show_data", path]
        dumps = {raw.get("index"): raw.get("extradata") for raw in _run_ffprobe(cmd).get("streams") or []}
        for raw in streams:
            if raw.get("codec_name") == "hevc":
                raw["extradata"] = dumps.get(raw.get("index"))
    return parse_probe(path, path.stat().st_size, data)
