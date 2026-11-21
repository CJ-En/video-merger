"""A stand-in for asfbin.exe in the tests (the real AsfBin is never run): it records how it was started (its arguments
and the marker list) in FAKE_ASFBIN_RECORD, then joins the parts with FFmpeg (FAKE_ASFBIN_FFMPEG), or fails or
hangs, as FAKE_ASFBIN_MODE says.

    python fake_asfbin.py -i <part> -i <part> ... -o <output> -y -m <markers>

Modes: join (the default), slow (writes the joined file in four steps), short (only the first part), fail (exit
code 1 with a few lines of text), stall (writes a little, then waits for minutes).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def joined(ffmpeg: str, inputs: list[str]) -> bytes:
    """The parts copied into one ASF file by FFmpeg."""
    with tempfile.TemporaryDirectory() as work:
        listing = Path(work) / "parts.txt"
        out = Path(work) / "joined.wmv"
        lines = ["file '" + Path(p).resolve().as_posix().replace("'", "'\\''") + "'\n" for p in inputs]
        listing.write_text("".join(lines), encoding="utf-8")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)],
                       check=True, stdin=subprocess.DEVNULL)
        return out.read_bytes()


def main(args: list[str]) -> int:
    inputs = [args[i + 1] for i, arg in enumerate(args) if arg == "-i"]
    output = Path(args[args.index("-o") + 1])
    markers = Path(args[args.index("-m") + 1])
    record = os.environ.get("FAKE_ASFBIN_RECORD")
    if record:
        Path(record).write_text(json.dumps({"args": args, "markers": markers.read_bytes().decode("utf-8"), "pid": os.getpid()}),
                                encoding="utf-8")
    mode = os.environ.get("FAKE_ASFBIN_MODE", "join")
    print("AsfBin stand-in", flush=True)
    if mode == "fail":
        print("Processing: 10%\rProcessing: 20%", flush=True)
        print("ERROR: the input file is damaged.", flush=True)
        return 1
    if mode == "stall":
        output.write_bytes(b"\0" * 4096)
        time.sleep(300)
        return 0
    data = joined(os.environ["FAKE_ASFBIN_FFMPEG"], inputs[:1] if mode == "short" else inputs)
    if mode == "slow":
        with open(output, "wb") as out:
            step = len(data) // 4 + 1
            for start in range(0, len(data), step):
                out.write(data[start:start + step])
                out.flush()
                time.sleep(0.8)
    else:
        output.write_bytes(data)
    print("Done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
