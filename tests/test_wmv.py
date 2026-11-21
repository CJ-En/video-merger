"""WMV parts: joined with AsfBin into a .wmv (a stand-in, tests/fake_asfbin.py, plays AsfBin; the real one is never run)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from video_merger import logs, merge as merging, tools
from video_merger.discover import find_video_files, group_files
from video_merger.plan import build_plan
from video_merger.probe import Stream, probe

from .helpers import AAC, H264, media
from .test_merge_ffmpeg import ENCODERS, TOOLS, run_cli

FAKE = Path(__file__).resolve().with_name("fake_asfbin.py")
WMV2 = Stream(0, "video", "wmv2", "", 320, 240, "yuv420p", "25/1", duration=100.0)
WMA = Stream(1, "audio", "wmav2", "", sample_rate="44100", channels=1, duration=100.0)


def wmv(path: str, file_id=None, part=None, streams=(WMV2, WMA)):
    return media(path, file_id, part, streams=streams)


class PlanTest(unittest.TestCase):
    def plan(self, files, **kwargs):
        groups, _ = group_files(files)
        return build_plan(groups, kwargs.pop("out", None), **kwargs)

    def test_wmv_parts_always_give_a_wmv(self):
        for container in ("mkv", "mp4"):
            item = self.plan([wmv("d/a.wmv", "abc00123", 1), wmv("d/b.wmv", "abc00123", 2)], container=container)[0]
            self.assertEqual((item.action, item.target), ("merge", Path("d/abc00123.wmv")), container)
        item = self.plan([wmv("d/My Video part1.wmv"), wmv("d/My Video part2.wmv")], container="mp4")[0]
        self.assertEqual(item.target, Path("d/My Video.wmv"))

    def test_windows_media_codecs_are_accepted(self):
        for video, audio in (("wmv1", "wmav1"), ("wmv3", "wmav2"), ("vc1", "wmapro")):
            streams = (Stream(0, "video", video, "", 1280, 720, "yuv420p", "30000/1001"), Stream(1, "audio", audio, "", sample_rate="48000", channels=2))
            item = self.plan([wmv(f"d/{n}.wmv", "abc00123", n, streams) for n in (1, 2)])[0]
            self.assertEqual(item.action, "merge", video)

    def test_other_codecs_inside_a_wmv_are_not_merged(self):
        for streams, words in (((H264, WMA), "h264 video inside a .wmv"), ((WMV2, AAC), "aac sound inside a .wmv"),
                               ((Stream(0, "video", "msmpeg4v3"), WMA), "msmpeg4v3 video")):
            item = self.plan([wmv(f"d/{n}.wmv", "abc00123", n, streams) for n in (1, 2)])[0]
            self.assertEqual((item.action, item.kind), ("problem", "codecs"), words)
            self.assertIn(words, item.reason)

    def test_parts_in_different_formats(self):
        item = self.plan([wmv("d/abc00123_1.wmv"), media("d/abc00123_2.mp4")])[0]
        self.assertEqual((item.action, item.kind, item.target), ("problem", "formats", None))
        self.assertEqual(item.reason, "the parts are in different formats (WMV and MP4)")
        item = self.plan([wmv("d/abc00123_1.wmv"), media("d/abc00123_2.mp4"), media("d/abc00123_3.mkv")])[0]
        self.assertIn("(WMV, MKV and MP4)", item.reason)
        # .mkv and .mp4 names together are not a WMV question (what ffprobe finds inside counts: test_discover's
        # test_parts_in_different_containers).
        self.assertEqual(self.plan([media("d/abc00123_1.mkv"), media("d/abc00123_2.mp4")])[0].action, "merge")

    def test_the_same_checks_as_other_parts(self):
        item = self.plan([wmv("d/ABC-123 DVD part1.wmv"), wmv("d/ABC-123 DVD part2.wmv")])[0]
        self.assertEqual((item.action, item.kind), ("skip", "not_web_dl"))
        item = self.plan([wmv("d/a.wmv", "abc00123", 1), wmv("d/c.wmv", "abc00123", 3)])[0]
        self.assertEqual((item.action, item.reason), ("problem", "missing part(s) 2"))
        small = Stream(0, "video", "wmv2", "", 160, 120, "yuv420p", "25/1")
        item = self.plan([wmv("d/a.wmv", "abc00123", 1), wmv("d/b.wmv", "abc00123", 2, (small, WMA))])[0]
        self.assertEqual(item.kind, "mismatch")
        self.assertIn("160x120", item.reason)
        # WMV video in an .mkv is still not merged (only .wmv files go to AsfBin).
        item = self.plan([media(f"d/a{n}.mkv", "abc00123", n, streams=(Stream(0, "video", "wmv3"), AAC)) for n in (1, 2)])[0]
        self.assertEqual(item.action, "skip")
        self.assertIn("WMV/VC-1", item.reason)

    def test_the_joined_file_is_not_a_part(self):
        # AsfBin may keep the first part's comment tag ("part": "1") in abc00123.wmv.
        files = [wmv("d/x1.wmv", "abc00123", 1), wmv("d/x2.wmv", "abc00123", 2), wmv("d/abc00123.wmv", "abc00123", 1)]
        groups, singles = group_files(files)
        self.assertEqual([(g.numbers, g.problems) for g in groups], [([1, 2], [])])
        self.assertEqual([s.path.name for s in singles], ["abc00123.wmv"])


class FindTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vm wmv ")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name in ("a_1.wmv", "a_2.wmv", "b.mkv", "c.avi", "a.partial.wmv", "a.partial.markers.txt"):
            (self.dir / name).write_bytes(b"")

    def test_wmv_files_only_with_asfbin(self):
        files, _, ignored = find_video_files([self.dir])
        self.assertEqual(sorted(p.name for p in files), ["b.mkv"])
        self.assertEqual(sorted(p.name for p in ignored), ["a_1.wmv", "a_2.wmv", "c.avi"])
        files, _, ignored = find_video_files([self.dir], wmv=True)
        self.assertEqual(sorted(p.name for p in files), ["a_1.wmv", "a_2.wmv", "b.mkv"])
        self.assertEqual([p.name for p in ignored], ["c.avi"])  # an unfinished .partial.wmv is neither

    def test_asfbin_is_found_like_ffmpeg(self):
        name = "asfbin.exe" if os.name == "nt" else "asfbin"
        empty = self.dir / "empty"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}), mock.patch.object(tools, "program_dir", return_value=self.dir):
            self.assertIsNone(tools.find_asfbin())  # not there: WMV files are left alone
            with self.assertRaises(tools.ToolError):
                tools.find_asfbin(empty)  # a place given that doesn't hold it: an error
            program = self.dir / name
            program.write_bytes(b"")
            program.chmod(0o755)
            self.assertEqual(tools.find_asfbin(), program.resolve())  # next to the program
            self.assertEqual(tools.find_asfbin(self.dir), program.resolve())

    @unittest.skipUnless(os.name == "nt", "Windows file properties")
    def test_version_from_the_file_properties(self):
        self.assertRegex(tools.file_version(Path(sys.executable)), r"^\d+\.\d+\.\d+$")
        (self.dir / "plain.exe").write_bytes(b"MZ")
        self.assertEqual(tools.file_version(self.dir / "plain.exe"), "")


class AsfBinTextTest(unittest.TestCase):
    def test_markers_at_each_part(self):
        parts = [media("a.wmv", duration=120.0), media("b.wmv", duration=3561.5), media("c.wmv", duration=12.25)]
        self.assertEqual(merging.markers_text(parts, [1, 2, 3]), "0 Part 1\r\n120 Part 2\r\n3681.5 Part 3\r\n")

    def test_names_asfbin_can_read(self):
        folder = Path(tempfile.mkdtemp(prefix="vm names ")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        japanese = folder / "動画" / "abc00123_1.wmv"  # a part: there
        japanese.parent.mkdir()
        japanese.write_bytes(b"")
        output = folder / "動画" / "abc00123.partial.wmv"  # the output: not there yet
        with mock.patch.object(tools, "ansi_codepage", return_value=None):
            self.assertEqual(merging.asfbin_name(japanese), str(japanese))  # a Unicode setting: as it is
        with mock.patch.object(tools, "ansi_codepage", return_value="cp1252"):
            plain = Path("C:/videos/Café/abc00123_1.wmv")
            self.assertEqual(merging.asfbin_name(plain), str(plain))
            with mock.patch.object(tools, "short_path", return_value="C:\\X\\4A1B~1\\ABC001~1.WMV"):
                self.assertEqual(merging.asfbin_name(japanese), "C:\\X\\4A1B~1\\ABC001~1.WMV")
            with mock.patch.object(tools, "short_path", return_value="C:\\X\\4A1B~1"):
                self.assertEqual(merging.asfbin_name(output), os.path.join("C:\\X\\4A1B~1", "abc00123.partial.wmv"))
            with mock.patch.object(tools, "short_path", side_effect=str), self.assertRaises(merging.MergeFailed) as caught:
                merging.asfbin_name(japanese)  # no short name on this drive
            self.assertIn("Rename the folder or the files", str(caught.exception))

    def test_working_names_asfbin_can_be_given(self):
        target = Path("C:/videos/見本動画185.wmv")
        with mock.patch.object(tools, "ansi_codepage", return_value="cp1252"):
            partial, markers = merging.partial_path(target), merging.markers_path(target)
            self.assertRegex(partial.name, r"^merging-[0-9a-f]{10}\.partial\.wmv$")
            self.assertEqual(markers.name, partial.name.replace(".wmv", ".markers.txt"))
            self.assertEqual(partial.parent, target.parent)
            self.assertEqual(merging.partial_path(Path("C:/videos/Café.wmv")).name, "Café.partial.wmv")
            self.assertEqual(merging.partial_path(target.with_suffix(".mkv")).name, "見本動画185.partial.mkv")  # FFmpeg: any name
        with mock.patch.object(tools, "ansi_codepage", return_value=None):
            self.assertEqual(merging.partial_path(target).name, "見本動画185.partial.wmv")

    def test_a_file_that_is_not_a_program(self):
        folder = Path(tempfile.mkdtemp(prefix="vm not asfbin ")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "asfbin.exe").write_bytes(b"")
        groups, _ = group_files([wmv(str(folder / f"abc00123_{n}.wmv")) for n in (1, 2)])
        result = merging.merge(build_plan(groups, None)[0], tools.Tools(Path("ffmpeg"), Path("ffprobe"), folder / "asfbin.exe"))
        self.assertFalse(result.ok)
        self.assertIn("AsfBin could not be started", result.message)
        self.assertEqual([p.name for p in folder.iterdir()], ["asfbin.exe"])  # no marker list or output left


def make_wmv(path: Path, seconds: float = 2, comment: dict | None = None) -> Path:
    cmd = [str(TOOLS.ffmpeg), "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", "wmv2", "-c:a", "wmav2", "-shortest"]
    if comment:
        cmd += ["-metadata", "comment=" + json.dumps(comment)]
    subprocess.run(cmd + [str(path)], check=True)
    return path


@unittest.skipUnless(TOOLS and "wmv2" in ENCODERS and "wmav2" in ENCODERS, "ffmpeg with WMV2/WMAv2 encoders not installed")
class AsfBinMergeTest(unittest.TestCase):
    """Real WMV2/WMAv2 clips made with FFmpeg, joined by the stand-in, then the real result checks."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vm asf ")).resolve()  # a space and a quote in paths on purpose
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.parts = self.dir / "it's here"
        self.parts.mkdir()
        self.record = self.dir / "asfbin.json"
        patches = [
            mock.patch.object(logs, "log_dir", return_value=self.dir / "logs"),
            mock.patch.object(merging, "asfbin_command", side_effect=lambda path: [sys.executable, str(path)]),
            mock.patch.dict(os.environ, {"FAKE_ASFBIN_FFMPEG": str(TOOLS.ffmpeg), "FAKE_ASFBIN_RECORD": str(self.record)}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        if os.name != "nt":
            FAKE.chmod(0o755)  # --asfbin wants a program

    def make_parts(self, count=2):
        names = {1: "z_first", 2: "b_second", 3: "a_third"}
        return [make_wmv(self.parts / f"{names[n]}.wmv", comment={"file_id": "abc00123", "part": str(n)}) for n in range(1, count + 1)]

    def recorded(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_join_with_markers_then_the_usual_checks(self):
        parts = self.make_parts()
        lengths = [probe(p, TOOLS.ffprobe).duration for p in parts]
        code, output = run_cli(str(self.parts), "--asfbin", str(FAKE), "--container", "mp4", "--delete-parts")
        self.assertEqual(code, 0, output)
        merged = self.parts / "abc00123.wmv"  # .wmv although MP4 was chosen
        self.assertEqual([p.name for p in self.parts.iterdir()], ["abc00123.wmv"])  # parts deleted, nothing left over
        info = probe(merged, TOOLS.ffprobe)
        self.assertAlmostEqual(info.duration, sum(lengths), delta=0.5)
        self.assertEqual([s.codec for s in info.streams], ["wmv2", "wmav2"])

        seen = self.recorded()
        folder = self.parts.resolve()
        partial, markers = folder / "abc00123.partial.wmv", folder / "abc00123.partial.markers.txt"
        self.assertEqual(seen["args"], ["-i", str(parts[0].resolve()), "-i", str(parts[1].resolve()), "-o", str(partial),
                                        "-y", "-m", str(markers)])
        self.assertEqual(seen["markers"], f"0 Part 1\r\n{merging.seconds_text(lengths[0])} Part 2\r\n")

    def test_a_name_asfbin_cant_be_given(self):
        """Japanese names on a Western European Windows: the parts by their short names, the output under a plain
        name, renamed to the video's name once it passed the checks."""
        parts = [make_wmv(self.parts / f"(見本スタジオ)見本185_(見本花子)_{n}.wmv", seconds=n + 1) for n in (1, 2)]
        links = []

        def short_path(path):
            path = Path(path)
            if path.name.isascii():
                return str(path)
            link = path.with_name(f"PART{len(links) + 1}~1.WMV")  # what Windows' short name would be
            os.link(path, link)
            links.append(link)
            return str(link)

        with mock.patch.object(tools, "ansi_codepage", return_value="cp1252"), \
                mock.patch.object(tools, "short_path", side_effect=short_path):
            code, output = run_cli(str(self.parts), "--asfbin", str(FAKE))
        self.assertEqual(code, 0, output)
        merged = self.parts / "(見本スタジオ)見本185_(見本花子).wmv"
        self.assertAlmostEqual(probe(merged, TOOLS.ffprobe).duration, sum(probe(p, TOOLS.ffprobe).duration for p in parts), delta=0.5)
        args = self.recorded()["args"]
        self.assertEqual(args[:4], ["-i", str(links[0]), "-i", str(links[1])])
        self.assertRegex(Path(args[5]).name, r"^merging-[0-9a-f]{10}\.partial\.wmv$")
        self.assertRegex(Path(args[8]).name, r"^merging-[0-9a-f]{10}\.partial\.markers\.txt$")
        self.assertEqual(sorted(p.name for p in self.parts.iterdir() if p.name.startswith("merging-")), [])  # nothing left

    def test_a_second_look_finds_the_joined_file(self):
        self.make_parts()
        self.assertEqual(run_cli(str(self.parts), "--asfbin", str(FAKE))[0], 0)
        code, output = run_cli(str(self.parts), "--asfbin", str(FAKE))  # the joined file keeps part 1's comment
        self.assertEqual(code, 0, output)
        self.assertIn("already merged", output)
        self.assertNotIn("found 2 times", output)

    def test_without_asfbin_wmv_files_are_left_alone(self):
        self.make_parts()
        with mock.patch.object(tools, "find_asfbin", return_value=None):
            code, output = run_cli(str(self.parts))
        self.assertIn("Ignored 2 .wmv file(s)", output)
        self.assertIn("AsfBin (Windows) lets video-merger join WMV parts", output)
        self.assertFalse(self.record.exists())
        self.assertEqual(len(list(self.parts.iterdir())), 2)

    def test_asfbin_failing_keeps_the_parts(self):
        self.make_parts()
        with mock.patch.dict(os.environ, {"FAKE_ASFBIN_MODE": "fail"}):
            code, output = run_cli(str(self.parts), "--asfbin", str(FAKE), "--delete-parts")
        self.assertEqual(code, 1)
        self.assertIn("AsfBin failed (exit code 1)", output)
        self.assertIn("ERROR: the input file is damaged.", output)  # its last lines
        self.assertIn("Processing: 20%", output)  # a line it redrew with a carriage return
        self.assertEqual(sorted(p.name for p in self.parts.iterdir()), ["b_second.wmv", "z_first.wmv"])
        log_text = (self.dir / "logs" / "video-merger.log").read_text(encoding="utf-8")
        self.assertIn("ERROR: the input file is damaged.", log_text)

    def test_a_result_that_fails_the_checks_is_removed(self):
        self.make_parts()
        with mock.patch.dict(os.environ, {"FAKE_ASFBIN_MODE": "short"}):  # only part 1 in the "joined" file
            code, output = run_cli(str(self.parts), "--asfbin", str(FAKE), "--delete-parts")
        self.assertEqual(code, 1)
        self.assertIn("but the parts add up to", output)
        self.assertEqual(sorted(p.name for p in self.parts.iterdir()), ["b_second.wmv", "z_first.wmv"])

    def plan(self):
        files, _, _ = find_video_files([self.parts], wmv=True)
        groups, _ = group_files([probe(p, TOOLS.ffprobe) for p in files])
        return build_plan(groups, None)[0]

    def test_progress_is_the_output_size(self):
        self.make_parts()
        item = self.plan()
        seen, steps = [], []
        with mock.patch.dict(os.environ, {"FAKE_ASFBIN_MODE": "slow"}):
            result = merging.merge(item, tools.Tools(TOOLS.ffmpeg, TOOLS.ffprobe, FAKE), on_progress=seen.append, on_step=steps.append)
        self.assertTrue(result.ok, result.message)
        self.assertGreater(len(set(seen)), 2)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(steps, [merging.CHECKING])
        self.assertAlmostEqual(result.duration, item.group.duration, delta=0.5)
        self.assertEqual(result.size, (self.parts / "abc00123.wmv").stat().st_size)

    def test_a_stalled_asfbin_is_stopped(self):
        self.make_parts()
        item = self.plan()
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"FAKE_ASFBIN_MODE": "stall"}):
            result = merging.merge(item, tools.Tools(TOOLS.ffmpeg, TOOLS.ffprobe, FAKE), stall_timeout=1.5)
        self.assertLess(time.monotonic() - started, 60)
        self.assertFalse(result.ok)
        self.assertIn("AsfBin made no progress", result.message)
        self.assertEqual(sorted(p.name for p in self.parts.iterdir()), ["b_second.wmv", "z_first.wmv"])  # no .partial left


if __name__ == "__main__":
    unittest.main()
