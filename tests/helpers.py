from __future__ import annotations

import json
from pathlib import Path

from video_merger.probe import MediaFile, Stream

H264 = Stream(0, "video", "h264", "High", 1920, 1080, "yuv420p", "30000/1001", duration=100.0)
AAC = Stream(1, "audio", "aac", "LC", sample_rate="48000", channels=2, duration=100.0)
VP9 = Stream(0, "video", "vp9", "Profile 0", 3840, 2160, "yuv420p", "20000/667", duration=100.0)


def media(path: str, file_id: str | None = None, part: int | None = None, streams=(H264, AAC), duration: float = 100.0) -> MediaFile:
    return MediaFile(Path(path), 1000, duration, tuple(streams), file_id, part)


def dmm_comment(file_id: str, part: int | None = None) -> str:
    data = {"file_id": file_id}
    if part is not None:
        data["part"] = str(part)
    return json.dumps(data)
