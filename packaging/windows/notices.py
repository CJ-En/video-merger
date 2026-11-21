"""Writes THIRD-PARTY-NOTICES.txt for the Windows download, from the build itself.

Run by build.ps1 with the build's own Python (the one PyInstaller used): the notices name that Python's version and
license file, PyInstaller's, and the OpenSSL version. Every file PyInstaller put in _internal must be covered by one
of them: a new library (e.g. after a Python or PyInstaller update) stops the build until its license is added here.

    python notices.py <build folder> <output file>
"""

from __future__ import annotations

import importlib.metadata
import re
import ssl
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APACHE = HERE / "licenses" / "Apache-2.0.txt"
OWN = "video_merger"  # Video Merger itself (LICENSE.txt), in _internal/video_merger

# Which notice covers each file in _internal (names compared in lower case).
COVERED = (
    (re.compile(r"python3\d*\.dll|base_library\.zip|[^/\\]+\.pyd"), "python"),
    (re.compile(r"libffi-\d+\.dll"), "python"),  # libffi's license is in Python's text
    (re.compile(r"vcruntime140(_1)?\.dll|msvcp140\.dll|ucrtbase\.dll|api-ms-win-[a-z0-9-]+\.dll"), "python"),  # Microsoft's
    (re.compile(r"lib(ssl|crypto)-3(-x64)?\.dll"), "openssl"),
)

RULE = "=" * 78


def uncovered(internal: Path) -> list[str]:
    """Files in _internal that no notice covers (Video Merger's own files aside)."""
    left = []
    for path in sorted(internal.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(internal)
        if relative.parts[0] == OWN:
            continue
        name = relative.as_posix().lower()
        if not any(pattern.fullmatch(name) for pattern, _ in COVERED):
            left.append(relative.as_posix())
    return left


def notices_text(python_version: str, python_license: str, openssl_version: str, pyinstaller_version: str,
                 pyinstaller_license: str, apache_license: str) -> str:
    def section(title: str, body: str) -> str:
        return f"\n\n{RULE}\n{title}\n{RULE}\n\n{body.strip()}\n"

    head = f"""Third-party software in Video Merger for Windows
================================================

Video Merger itself is under the MIT license (LICENSE.txt). So that it runs
without Python installed, VideoMerger.exe carries the parts below, each under
its own license (the full texts follow):

- Python {python_version} (python3xx.dll, base_library.zip and the .pyd files in
  _internal): Python Software Foundation License Version 2. Python's text also
  covers what Python for Windows brings along: bzip2, libffi, Zstandard and
  Microsoft's Visual C++ runtime (VCRUNTIME140.dll, ucrtbase.dll and the
  api-ms-win-*.dll files).
- {openssl_version} (libssl-3.dll, libcrypto-3.dll): Apache License 2.0.
- PyInstaller {pyinstaller_version} (the starter inside VideoMerger.exe): GNU GPL 2
  or later with PyInstaller's "Bootloader Exception", which lets any program
  carry it.

Not included in this download, each under its own terms: FFmpeg
(https://ffmpeg.org/legal.html) and AsfBin (free for non-commercial use,
http://www.radioactivepages.com).
"""
    return (head
            + section(f"Python {python_version}", python_license)
            + section(f"{openssl_version}: Apache License 2.0", apache_license)
            + section(f"PyInstaller {pyinstaller_version}", pyinstaller_license))


def pyinstaller_license() -> tuple[str, str]:
    dist = importlib.metadata.distribution("pyinstaller")
    for file in dist.files or ():
        if file.name.upper() == "COPYING.TXT":
            return dist.version, Path(dist.locate_file(file)).read_text(encoding="utf-8")
    raise SystemExit("PyInstaller's license (COPYING.txt) was not found")


def main(argv: list[str]) -> int:
    build, out = Path(argv[0]), Path(argv[1])
    left = uncovered(build / "_internal")
    if left:
        print("No license notice covers these files in the build: " + ", ".join(left), file=sys.stderr)
        print("Add their licenses to packaging/windows/notices.py.", file=sys.stderr)
        return 1
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        print(f"Python's license file was not found: {python_license}", file=sys.stderr)
        return 1
    version, copying = pyinstaller_license()
    openssl = re.sub(r"\s+\d{1,2} \w{3} \d{4}$", "", ssl.OPENSSL_VERSION)  # "OpenSSL 3.0.15 3 Sep 2024" -> "OpenSSL 3.0.15"
    text = notices_text(sys.version.split()[0], python_license.read_text(encoding="utf-8", errors="replace"), openssl,
                        version, copying, APACHE.read_text(encoding="utf-8"))
    out.write_text(text, encoding="utf-8")
    print(f"{out.name}: Python {sys.version.split()[0]}, {openssl}, PyInstaller {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
