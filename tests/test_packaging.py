"""The Windows build only replaces its own files in the program folder (packaging/windows/install-build.ps1), refuses
to finish when it names the computer it was made on (check_dist.py), never bundles FFmpeg or AsfBin, and its icon is
the page's mark (make_icon.py)."""

import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "packaging" / "windows"
SCRIPT = HERE / "install-build.ps1"
CHECK_DIST = HERE / "check_dist.py"
NOTICES = HERE / "notices.py"
WEB = ROOT / "video_merger" / "web"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(POWERSHELL, "PowerShell not installed")
class InstallBuildTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vm build ")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.build, self.app = self.dir / "build", self.dir / "dist" / "VideoMerger"
        self.write(self.build, {"VideoMerger.exe": "new", "_internal/python.dll": "new", "_internal/video_merger/web/index.html": "new",
                                "LICENSE.txt": "license", "README.txt": "readme"})

    def write(self, folder, files):
        for name, text in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")

    def install(self):
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                               "-From", str(self.build), "-To", str(self.app)], capture_output=True, text=True, timeout=120)

    def test_only_the_builds_files_are_replaced(self):
        mine = {"ffmpeg.exe": "ffmpeg", "ffprobe.exe": "ffprobe", "asfbin.exe": "asfbin", "bin/ffmpeg.exe": "ffmpeg",
                "videos/abc00123.mkv": "video", "notes.txt": "mine"}
        self.write(self.app, {"VideoMerger.exe": "old", "_internal/old.dll": "old", "README.txt": "old", **mine})
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for name, text in mine.items():
            self.assertEqual((self.app / name).read_text(encoding="utf-8"), text, name)
        self.assertEqual((self.app / "VideoMerger.exe").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.app / "README.txt").read_text(encoding="utf-8"), "readme")
        self.assertTrue((self.app / "_internal" / "python.dll").exists())
        self.assertFalse((self.app / "_internal" / "old.dll").exists())  # the old build's own folder is replaced whole

    def test_a_build_with_someone_elses_programs_changes_nothing(self):
        for program in ("asfbin.exe", "ffmpeg.exe"):
            self.write(self.build, {program: "x"})
            self.write(self.app, {"VideoMerger.exe": "old", program: "theirs"})
            result = self.install()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((self.app / "VideoMerger.exe").read_text(encoding="utf-8"), "old")
            self.assertEqual((self.app / program).read_text(encoding="utf-8"), "theirs")
            (self.build / program).unlink()

    def test_first_build_makes_the_folder(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(sorted(p.name for p in self.app.iterdir()), ["LICENSE.txt", "README.txt", "VideoMerger.exe", "_internal"])


class NoticesTest(unittest.TestCase):
    """THIRD-PARTY-NOTICES.txt covers every library the build carries, with its license text."""

    def setUp(self):
        self.notices = load(NOTICES)
        self.dir = Path(tempfile.mkdtemp(prefix="vm notices ")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.internal = self.dir / "_internal"
        for name in ("python314.dll", "base_library.zip", "_ssl.pyd", "unicodedata.pyd", "libssl-3.dll", "libcrypto-3.dll",
                     "libffi-8.dll", "VCRUNTIME140.dll", "ucrtbase.dll", "api-ms-win-core-file-l1-1-0.dll",
                     "video_merger/web/index.html", "video_merger/web/app.js"):
            (self.internal / name).parent.mkdir(parents=True, exist_ok=True)
            (self.internal / name).write_bytes(b"x")

    def test_every_file_of_a_real_build_is_covered(self):
        self.assertEqual(self.notices.uncovered(self.internal), [])

    def test_a_new_library_stops_the_build(self):
        (self.internal / "sqlite3.dll").write_bytes(b"x")
        (self.internal / "certifi").mkdir()
        (self.internal / "certifi" / "cacert.pem").write_bytes(b"x")
        self.assertEqual(self.notices.uncovered(self.internal), ["certifi/cacert.pem", "sqlite3.dll"])
        self.assertEqual(self.notices.main([str(self.dir), str(self.dir / "out.txt")]), 1)
        self.assertFalse((self.dir / "out.txt").exists())

    def test_the_text_names_each_part_and_carries_its_license(self):
        text = self.notices.notices_text("3.14.6", "PSF LICENSE TEXT", "OpenSSL 3.0.15", "6.22.3", "PYINSTALLER COPYING",
                                         self.notices.APACHE.read_text(encoding="utf-8"))
        for words in ("MIT license (LICENSE.txt)", "Python 3.14.6", "OpenSSL 3.0.15 (libssl-3.dll, libcrypto-3.dll): Apache License 2.0",
                      "PyInstaller 6.22.3", "Bootloader Exception", "PSF LICENSE TEXT", "PYINSTALLER COPYING",
                      "Apache License\n                           Version 2.0", "Not included", "FFmpeg", "AsfBin"):
            self.assertIn(words, text)
        lines = (ROOT / "LICENSE").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line for line in lines if line.startswith("Copyright")], ["Copyright (c) 2026 Video Merger contributors"])


class CheckDistTest(unittest.TestCase):
    """The build refuses files that name the computer it was built on, or private words (names it must not carry)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vm dist ")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def check(self, *words, script=CHECK_DIST, env=None):
        return subprocess.run([sys.executable, str(script), str(self.dir), *words], capture_output=True, text=True, timeout=60,
                              env=env)

    def test_clean_build(self):
        (self.dir / "a.txt").write_text("nothing personal", encoding="utf-8")
        self.assertEqual(self.check("someuser").returncode, 0)

    def test_given_words_are_refused(self):
        (self.dir / "a.txt").write_text(r"C:\Users\SomeUser\x", encoding="utf-8")
        self.assertEqual(self.check("someuser").returncode, 1)
        (self.dir / "a.txt").write_text("C:/Users/SomeUser/x", encoding="utf-16-le")
        self.assertEqual(self.check(r"C:\Users\someuser").returncode, 1)  # slashes and UTF-16 too

    def test_private_words_from_the_file_next_to_it(self):
        # Kept out of the repository: refused-words.txt next to check_dist.py, ignored by git.
        tool = Path(tempfile.mkdtemp(prefix="vm dist tool ")).resolve()
        self.addCleanup(shutil.rmtree, tool, True)
        script = tool / CHECK_DIST.name
        shutil.copy(CHECK_DIST, script)
        (tool / "refused-words.txt").write_text("# never in a build\nsecretproject\n\nother-name\n", encoding="utf-8-sig")
        env = {name: value for name, value in os.environ.items() if name != "VIDEO_MERGER_REFUSED_WORDS"}
        for word in ("secretproject", "other-name"):
            for data in (f"made by {word.title()}".encode(), f"x{word.upper()}x".encode(), f"x {word} x".encode("utf-16-le")):
                (self.dir / "a.bin").write_bytes(data)
                result = self.check(script=script, env=env)
                self.assertEqual(result.returncode, 1, data)
                self.assertIn("NOT CLEAN", result.stdout)
        (self.dir / "a.bin").write_bytes(b"never in a build")  # a comment line is no word
        self.assertEqual(self.check(script=script, env=env).returncode, 0)
        self.assertIn("packaging/windows/refused-words.txt", (ROOT / ".gitignore").read_text(encoding="utf-8").split())

    def test_private_words_from_the_environment(self):
        (self.dir / "a.txt").write_text("built for SecretProject", encoding="utf-8")
        result = self.check(env={**os.environ, "VIDEO_MERGER_REFUSED_WORDS": "nothing-here;secretproject"})
        self.assertEqual(result.returncode, 1, result.stdout)
        module = load(CHECK_DIST)
        self.assertEqual(module.private_words(self.dir / "missing.txt", {"VIDEO_MERGER_REFUSED_WORDS": " a b ; ;c "}), ["a b", "c"])

    def test_no_private_word_is_written_in_the_script(self):
        text = CHECK_DIST.read_text(encoding="utf-8")
        self.assertNotIn("fromhex", text)  # they once were, as hex
        self.assertIsNone(re.search(r"[0-9a-f]{12,}", text))

    def test_the_page_and_the_windows_guide_are_clean(self):
        # Copied as they are into the build (not compressed like the code): they must pass the check themselves.
        for path in [*WEB.iterdir(), HERE / "README-windows.txt"]:
            shutil.copy(path, self.dir / path.name)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)


class BuildFilesTest(unittest.TestCase):
    def test_spec_icon_and_page(self):
        spec = (HERE / "video-merger.spec").read_text(encoding="utf-8")
        self.assertIn('ICON = str(HERE / "video-merger.ico")', spec)
        self.assertIn('name="VideoMerger"', spec)
        self.assertIn("console=True", spec)
        self.assertIn('"video_merger/web"', spec)  # the page goes with the program
        self.assertIn("main(launcher=True)", (HERE / "video_merger_app.py").read_text(encoding="utf-8"))

    def test_ffmpeg_and_asfbin_are_never_bundled(self):
        build = (HERE / "build.ps1").read_text(encoding="utf-8")
        install = SCRIPT.read_text(encoding="utf-8")
        for program in ("ffmpeg.exe", "ffprobe.exe", "asfbin.exe"):
            self.assertIn(f'"{program}"', build)
            self.assertIn(f'"{program}"', install)
        self.assertIn("VideoMerger-$version-windows-x64.zip", build)
        self.assertIn("check_dist.py", build)
        self.assertIn('Join-Path $build "venv"', build)

    def test_windows_guide(self):
        text = (HERE / "README-windows.txt").read_text(encoding="utf-8")
        for words in ("VideoMerger.exe", "ffmpeg.exe", "ffprobe.exe", "asfbin.exe", "http://www.radioactivepages.com",
                      "https://www.gyan.dev/ffmpeg/builds/", "Closing it", "never changed", "Always a .wmv"):
            self.assertIn(words, text)
        self.assertLessEqual(len(text.splitlines()), 70)  # short

    def test_gitignore_covers_the_build(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("build/", ignored)
        self.assertIn("dist/", ignored)


class TextsTest(unittest.TestCase):
    """What the page and the guides say."""

    @staticmethod
    def text(path: Path) -> str:
        return " ".join(path.read_text(encoding="utf-8").split())  # lines joined as they read

    def test_no_text_says_that_every_part_is_checked(self):
        # The part numbers show a part missing in between (1, 3), never a missing last part.
        for path in (WEB / "index.html", HERE / "README-windows.txt", ROOT / "README.md"):
            text = self.text(path)
            self.assertNotIn("every part is there", text, path.name)
            self.assertIn("when no part is missing in between", text, path.name)
            self.assertIn("A missing last part can't be noticed", text, path.name)

    def test_what_to_do_when_smart_app_control_blocks_the_program(self):
        for path in (HERE / "README-windows.txt", ROOT / "README.md"):
            text = self.text(path)
            self.assertIn("With Windows' Smart App Control on,", text, path.name)
            self.assertIn('without "Run anyway"', text, path.name)
            self.assertIn("python -m video_merger", text, path.name)

    def test_the_readme_lists_this_versions_changes(self):
        version = re.search(r'__version__ = "(.+)"', (ROOT / "video_merger" / "__init__.py").read_text(encoding="utf-8"))
        self.assertEqual(version.group(1), "1.0.1")
        self.assertIn("\n## Changes in 1.0.1\n", (ROOT / "README.md").read_text(encoding="utf-8"))


def ico_images(data: bytes) -> dict[int, bytes]:
    """The images of an .ico by size, checked to be PNGs of that size (what make_icon.py writes)."""
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "not an icon file"
    images = {}
    for n in range(count):
        width, height, colors, _, planes, bits, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * n:22 + 16 * n])
        image = data[offset:offset + size]
        assert len(image) == size and image[:8] == b"\x89PNG\r\n\x1a\n", f"image {n} is not a whole PNG"
        assert image[12:16] == b"IHDR"
        png_width, png_height, depth, color = struct.unpack(">IIBB", image[16:26])
        assert (png_width, png_height, depth, color) == (width or 256, height or 256, 8, 6), f"image {n}: {png_width}"
        assert (colors, planes, bits) == (0, 1, 32)
        images[png_width] = image
    return images


def png_pixels(image: bytes) -> bytes:
    """The RGBA pixels of a PNG as make_icon.py writes it (filter "none" on every row), rows top to bottom. Compared
    instead of the file's bytes: how the pixels are compressed depends on the zlib Python was built with."""
    width, height = struct.unpack(">II", image[16:24])
    data, at = b"", 8
    while at < len(image):
        length, kind = struct.unpack(">I4s", image[at:at + 8])
        if kind == b"IDAT":
            data += image[at + 8:at + 8 + length]
        at += 12 + length
    raw = zlib.decompress(data)
    stride = width * 4 + 1  # each row starts with its filter byte
    assert len(raw) == stride * height, "not RGBA, 8 bits"
    assert all(raw[row * stride] == 0 for row in range(height)), "a row filter other than none"
    return b"".join(raw[row * stride + 1:(row + 1) * stride] for row in range(height))


def svg_polygons(path: str) -> list[list[tuple[float, float]]]:
    """The polygons of an SVG path made of M, L, H, V and Z (absolute), as the page's mark is written."""
    shapes, x, y = [], 0.0, 0.0
    for command, numbers in re.findall(r"([MLHVZ])([^MLHVZ]*)", path):
        values = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", numbers)]
        if command == "M":
            x, y = values
            shapes.append([(x, y)])
        elif command == "L":
            x, y = values
            shapes[-1].append((x, y))
        elif command == "H":
            x = values[0]
            shapes[-1].append((x, y))
        elif command == "V":
            y = values[0]
            shapes[-1].append((x, y))
    return shapes


class IconTest(unittest.TestCase):
    """The program's icon (make_icon.py) and the page's mark are the same drawing."""

    SIZES = [16, 24, 32, 48, 64, 128, 256]

    def test_the_icon_in_the_repository(self):
        self.assertEqual(sorted(ico_images((HERE / "video-merger.ico").read_bytes())), self.SIZES)

    def test_make_icon_writes_every_size(self):
        make_icon = load(HERE / "make_icon.py")
        folder = Path(tempfile.mkdtemp(prefix="vm icon ")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        images = ico_images(make_icon.make(folder / "test.ico").read_bytes())
        self.assertEqual(sorted(images), self.SIZES)
        in_git = ico_images((HERE / "video-merger.ico").read_bytes())
        for size in self.SIZES:  # the icon in git is make_icon.py's: the same pixels in every size
            self.assertEqual(png_pixels(images[size]), png_pixels(in_git[size]), size)
        # The 16 px image: the tile's round corner is partly see-through, its left edge is the accent color, the
        # first part is ink.
        pixels = png_pixels(images[16])

        def pixel(x, y):
            at = (y * 16 + x) * 4
            return pixels[at:at + 4]

        self.assertTrue(0 < pixel(0, 0)[3] < 255)
        self.assertEqual(pixel(0, 8), bytes([*make_icon.TILE, 255]))
        self.assertEqual(pixel(4, 5), bytes([*make_icon.INK, 255]))

    def test_page_favicon_and_icon_draw_the_same_mark(self):
        make_icon = load(HERE / "make_icon.py")
        shapes = [list(shape) for shape in make_icon.INK_SHAPES]
        page = (WEB / "index.html").read_text(encoding="utf-8")
        favicon = (WEB / "favicon.svg").read_text(encoding="utf-8")
        ink = re.search(r'<path class="ink" d="([^"]+)"', page).group(1)
        self.assertEqual(svg_polygons(ink), shapes)
        self.assertIn(f'<path d="{ink}" fill="#04121f"/>', favicon)
        self.assertIn('fill="#5ec4fa"', favicon)


if __name__ == "__main__":
    unittest.main()
