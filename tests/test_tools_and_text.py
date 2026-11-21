import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from video_merger import cli, merge as merging, tools
from video_merger.merge import concat_line, duration_tolerance, metadata_text

from .helpers import media


class ConcatTextTest(unittest.TestCase):
    def test_quotes_and_backslashes(self):
        line = concat_line(Path("/videos/Director's Cut/a b.mkv"))
        self.assertTrue(line.startswith("file '"))
        self.assertIn("Director'\\''s Cut/a b.mkv", line)
        self.assertNotIn("\\D", line)

    def test_metadata_has_chapters_and_comment(self):
        parts = [media("a.mkv", duration=60.5), media("b.mkv", duration=30.25)]
        text = metadata_text(parts, [1, 2], "abc00123")
        self.assertIn('comment={"file_id":"abc00123"}', text)
        self.assertIn("START=60500\nEND=90750\ntitle=Part 2", text)
        self.assertNotIn("comment", metadata_text(parts, [1, 2], None))

    def test_tolerance(self):
        self.assertEqual(duration_tolerance(60), 2.0)
        self.assertEqual(duration_tolerance(10000), 50.0)


class FfmpegStallTest(unittest.TestCase):
    """ffmpeg is stopped when it makes no progress, but not while it finishes the file once the whole length is
    written (an MP4's +faststart rewrites it without progress lines). A stand-in prints ffmpeg's progress lines."""

    def run_stand_in(self, last_us: int, then_sleep: float):
        script = (f"import sys, time; print('out_time_us=500000'); print('out_time_us={last_us}'); sys.stdout.flush(); "
                  f"time.sleep({then_sleep})")
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            code = merging._run_ffmpeg([sys.executable, "-c", script], 10.0, seen.append, 1.0, Path(folder) / "ffmpeg.log")
        return code, seen

    def test_finishing_the_file_is_not_a_stall(self):
        code, seen = self.run_stand_in(10_000_000, 3)
        self.assertEqual((code, seen), (0, [0.05, 1.0]))
        self.assertEqual(self.run_stand_in(8_000_000, 3)[0], 0)  # within the length check's 2 seconds

    def test_no_progress_before_the_end_is_a_stall(self):
        with self.assertRaises(merging.MergeFailed) as caught:
            self.run_stand_in(7_000_000, 60)
        self.assertIn("ffmpeg made no progress", str(caught.exception))


class FileListTest(unittest.TestCase):
    """--files-from: UTF-8 (Notepad, Python), UTF-8 with a byte order mark (PowerShell 7's -Encoding utf8BOM, Out-File
    utf8 in PowerShell 5.1) and UTF-16 with one (PowerShell 5.1's > and Out-File)."""

    def read(self, data: bytes):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "list.txt"
            path.write_bytes(data)
            return cli.read_list(path)

    def test_utf8_and_utf16_lists(self):
        lines = ["# parts", "D:\\動画\\abc00123_1.mkv", "D:\\Café\\abc00123_2.mkv"]
        text = "\r\n".join(lines) + "\r\n"
        for encoding, bom in (("utf-8", b""), ("utf-8-sig", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            self.assertEqual(self.read(bom + text.encode(encoding)), lines, encoding)

    def test_other_text_is_refused_with_a_reason(self):
        for data, reason in (("D:\\Café\\a.mkv".encode("cp1252"), "isn't UTF-8 text, nor UTF-16 with a byte order mark"),
                             ("D:\\a.mkv".encode("utf-16-le"), "UTF-16 text without a byte order mark"),
                             ("D:\\a.mkv".encode("utf-32"), "UTF-32")):
            with self.assertRaises(ValueError) as caught:
                self.read(data)
            self.assertIn(reason, str(caught.exception))
            self.assertIn("save it as UTF-8", str(caught.exception))


class FindToolTest(unittest.TestCase):
    def test_current_folder_is_not_searched(self):
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as empty:
            fake = Path(cwd) / name
            fake.write_text("")
            fake.chmod(0o755)
            old = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([".", "", empty])}), mock.patch.object(tools, "program_dir", return_value=Path(empty)):
                    with self.assertRaises(tools.ToolError):
                        tools.find_tool("ffmpeg")
                    self.assertEqual(tools.find_tool("ffmpeg", cwd), fake.resolve())
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
