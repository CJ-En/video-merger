"""End-to-end merges of small clips made with ffmpeg (skipped when ffmpeg isn't installed)."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from unittest import mock

from video_merger import cli, logs, merge as merging, plan as planning, tools
from video_merger.discover import find_video_files, group_files
from video_merger.merge import merge
from video_merger.plan import build_plan
from video_merger.probe import probe

from .helpers import media

try:
    TOOLS = tools.find_tools()
except tools.ToolError:
    TOOLS = None


def encoders() -> str:
    return subprocess.run([str(TOOLS.ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout if TOOLS else ""


ENCODERS = encoders()
VIDEO_CODEC = "libx264" if "libx264" in ENCODERS else "mpeg4"
# ffmpeg's noise filter leaves out the packets an expression picks (FFmpeg 5.1 and newer).
DROPS_PACKETS = TOOLS is not None and "-drop " in subprocess.run(
    [str(TOOLS.ffmpeg), "-hide_banner", "-h", "bsf=noise"], capture_output=True, text=True).stdout


def make_clip(path: Path, seconds: float = 2, size: str = "320x240", codec: str = VIDEO_CODEC, comment: dict | None = None,
              options: tuple = ()) -> Path:
    cmd = [
        str(TOOLS.ffmpeg), "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", codec, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", *options,
    ]
    if codec == "libvpx-vp9":
        cmd += ["-deadline", "realtime", "-cpu-used", "8"]
    if comment:
        cmd += ["-metadata", "comment=" + json.dumps(comment)]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def run_cli(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(args))
    return code, out.getvalue()


@unittest.skipUnless(TOOLS, "ffmpeg/ffprobe not installed")
class MergeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vm test ")).resolve()  # a space and a quote in paths on purpose
        self.parts = self.dir / "it's here"
        self.parts.mkdir()
        patcher = mock.patch.object(logs, "log_dir", return_value=self.dir / "logs")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def dmm_parts(self, count: int = 3, **kwargs) -> list[Path]:
        # Named so that alphabetical order would be wrong (10 before 2).
        names = {1: "z_first", 2: "b_second", 3: "a_third"}
        return [make_clip(self.parts / f"{names.get(n, n)}.mkv", comment={"file_id": "abc00123", "part": str(n)}, **kwargs) for n in range(1, count + 1)]

    def test_merges_parts_in_dmm_order(self):
        self.dmm_parts()
        code, output = run_cli(str(self.parts), "--delete-parts")
        self.assertEqual(code, 0, output)
        merged = self.parts / "abc00123.mkv"
        info = probe(merged, TOOLS.ffprobe)
        self.assertAlmostEqual(info.duration, 6.0, delta=0.5)
        self.assertEqual(info.file_id, "abc00123")
        self.assertEqual(len(info.streams), 2)
        chapters = json.loads(subprocess.run([str(TOOLS.ffprobe), "-v", "error", "-show_chapters", "-of", "json", str(merged)], capture_output=True, text=True).stdout)["chapters"]
        self.assertEqual([c["tags"]["title"] for c in chapters], ["Part 1", "Part 2", "Part 3"])
        self.assertEqual(sorted(p.name for p in self.parts.iterdir()), ["abc00123.mkv"])

        code, output = run_cli(str(self.parts))  # a second run finds a whole video, nothing to do
        self.assertEqual(code, 0, output)
        self.assertIn("No multi-part videos", output)

    def test_parts_kept_and_existing_output_skipped(self):
        self.dmm_parts(2)
        self.assertEqual(run_cli(str(self.parts), "-o", str(self.dir / "out"))[0], 0)
        self.assertTrue((self.dir / "out" / "abc00123.mkv").exists())
        self.assertEqual(len(list(self.parts.iterdir())), 2)
        code, output = run_cli(str(self.parts), "-o", str(self.dir / "out"))
        self.assertIn("already merged", output)
        self.assertEqual(code, 0)

    def test_another_video_merged_under_the_same_name_is_kept(self):
        # "Video 1" and "Video 2" in two folders are different videos, both merged into one folder as Video.mkv.
        for folder, seconds in (("A", 1), ("B", 3)):
            (self.dir / folder).mkdir()
            for n in (1, 2):
                make_clip(self.dir / folder / f"Video part{n}.mkv", seconds=seconds)
        out = self.dir / "out"
        self.assertEqual(run_cli(str(self.dir / "A"), "--out", str(out), "--delete-parts")[0], 0)
        merged = (out / "Video.mkv").read_bytes()
        for options in ((), ("--overwrite", "--delete-parts")):
            code, output = run_cli(str(self.dir / "B"), "--out", str(out), *options)
            self.assertEqual(code, 1, output)
            self.assertIn("is already there, but it is another video (0:00:02 long, the parts together 0:00:06)", output)
        self.assertEqual((out / "Video.mkv").read_bytes(), merged)
        self.assertEqual(len(list((self.dir / "B").iterdir())), 2)

    def plan(self, overwrite=False):
        files, _, _ = find_video_files([self.parts])
        groups, _ = group_files([probe(p, TOOLS.ffprobe) for p in files])
        return build_plan(groups, None, overwrite=overwrite, read=lambda path: probe(path, TOOLS.ffprobe))[0]

    def test_a_file_that_appears_after_the_plan_is_not_replaced(self):
        self.dmm_parts(2)
        target = self.parts / "abc00123.mkv"
        item = self.plan()
        target.write_bytes(b"theirs")
        result = merge(item, TOOLS, delete_parts=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.message, f"{target} is there now (it wasn't when the parts were checked), so it is not replaced")
        target.unlink()
        # While the parts are joined.
        result = merge(item, TOOLS, delete_parts=True, on_step=lambda step: target.write_bytes(b"theirs"))
        self.assertEqual((result.ok, result.message), (False, f"{target} is there now (it wasn't when the parts were checked), "
                                                              "so it is not replaced"))
        self.assertEqual(target.read_bytes(), b"theirs")
        self.assertEqual(sorted(p.name for p in self.parts.iterdir()), ["abc00123.mkv", "b_second.mkv", "z_first.mkv"])  # parts kept

    def test_merge_again_only_replaces_the_file_checked(self):
        self.dmm_parts(2)
        target = self.parts / "abc00123.mkv"
        self.assertEqual(run_cli(str(self.parts))[0], 0)
        item = self.plan(overwrite=True)
        self.assertEqual((item.action, item.kind), ("merge", "again"))
        target.write_bytes(b"changed after the plan")
        result = merge(item, TOOLS)
        self.assertEqual((result.ok, result.message), (False, f"{target} has changed since it was checked, so it is not replaced"))
        self.assertEqual(target.read_bytes(), b"changed after the plan")
        target.unlink()
        self.assertEqual(run_cli(str(self.parts))[0], 0)
        result = merge(self.plan(overwrite=True), TOOLS)
        self.assertTrue(result.ok, result.message)  # unchanged since the plan: made again

    def test_name_based_parts_to_mp4(self):
        for n in (1, 2):
            make_clip(self.parts / f"My Video part{n}.mp4")
        code, output = run_cli(str(self.parts), "--container", "mp4")
        self.assertEqual(code, 0, output)
        info = probe(self.parts / "My Video.mp4", TOOLS.ffprobe)
        self.assertAlmostEqual(info.duration, 4.0, delta=0.5)

    def test_missing_part_is_refused(self):
        paths = self.dmm_parts(3)
        paths[1].unlink()
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 1)
        self.assertIn("missing part(s) 2", output)
        self.assertFalse((self.parts / "abc00123.mkv").exists())

    def test_different_sizes_are_refused(self):
        make_clip(self.parts / "a.mkv", comment={"file_id": "abc00123", "part": "1"})
        make_clip(self.parts / "b.mkv", size="160x120", comment={"file_id": "abc00123", "part": "2"})
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 1)
        self.assertIn("160x120", output)

    def decode_errors(self, path: Path) -> str:
        return subprocess.run([str(TOOLS.ffmpeg), "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True).stderr

    @unittest.skipUnless("libx265" in ENCODERS, "no HEVC encoder")
    def test_hevc_parts_encoded_with_other_settings_are_refused(self):
        # ffmpeg's concat keeps part 1's HEVC header: part 2 would be decoded with the wrong settings.
        for n, settings in ((1, "ctu=64:ref=1"), (2, "ctu=32:ref=4:sao=0")):
            make_clip(self.parts / f"hevc part {n}.mkv", seconds=1, codec="libx265",
                      options=("-x265-params", f"{settings}:log-level=error"))
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 1, output)
        self.assertIn("part 2's HEVC video was encoded with other settings than part 1's", output)
        self.assertFalse((self.parts / "hevc.mkv").exists())

    @unittest.skipUnless("libx265" in ENCODERS, "no HEVC encoder")
    def test_hevc_parts_encoded_alike_are_merged(self):
        # Only the encoder's own message in the header differs: x265 writes its options there, with the thread count.
        for n, threads in ((1, 1), (2, 4)):
            make_clip(self.parts / f"hevc part {n}.mkv", seconds=n, codec="libx265",
                      options=("-x265-params", f"ctu=64:ref=1:frame-threads={threads}:pools=1:log-level=error"))
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 0, output)
        merged = self.parts / "hevc.mkv"
        self.assertAlmostEqual(probe(merged, TOOLS.ffprobe).duration, 3.0, delta=0.5)
        self.assertEqual(self.decode_errors(merged), "")

    def test_parts_in_different_containers_are_refused(self):
        # ffmpeg's concat would drop part 2: an MP4 then a Matroska part give a file as long as part 1.
        make_clip(self.parts / "v part 1.mp4")
        make_clip(self.parts / "v part 2.mkv")
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 1, output)
        self.assertIn("PROBLEM: part 2 is Matroska and part 1 is MP4: remux them to the same kind first", output)
        self.assertEqual(len(list(self.parts.iterdir())), 2)
        # What is inside counts, not the extension: an MP4 named .mkv joins an MP4.
        (self.parts / "v part 2.mkv").unlink()
        make_clip(self.parts / "v part 2.mp4")
        (self.parts / "v part 1.mp4").rename(self.parts / "v part 1.mkv")
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 0, output)
        self.assertAlmostEqual(probe(self.parts / "v.mkv", TOOLS.ffprobe).duration, 4.0, delta=0.5)

    def test_h264_parts_encoded_with_other_settings_are_merged(self):
        # For H.264, ffmpeg's concat carries each part's settings into the joined stream.
        if VIDEO_CODEC != "libx264":
            self.skipTest("no H.264 encoder")
        for n, settings in ((1, "ref=1:bframes=1:weightp=0:keyint=50"), (2, "ref=5:bframes=3:weightp=2:direct=temporal")):
            make_clip(self.parts / f"avc part {n}.mkv", seconds=1, options=("-x264-params", settings))
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.decode_errors(self.parts / "avc.mkv"), "")

    @unittest.skipUnless("libvpx-vp9" in ENCODERS, "no VP9 encoder")
    def test_vp9_stays_separate(self):
        self.dmm_parts(2, codec="libvpx-vp9", seconds=1)
        code, output = run_cli(str(self.parts))
        self.assertEqual(code, 0, output)
        self.assertIn("VP9", output)
        self.assertFalse((self.parts / "abc00123.mkv").exists())

    def test_wmv_files_are_ignored(self):
        make_clip(self.parts / "abc00123 part1.wmv", codec="wmv2")
        make_clip(self.parts / "abc00123 part2.wmv", codec="wmv2")
        code, output = run_cli(str(self.parts))
        self.assertIn("Ignored 2 .wmv file(s)", output)
        self.assertEqual(len(list(self.parts.iterdir())), 2)

    def test_dry_run_changes_nothing(self):
        self.dmm_parts(2)
        code, output = run_cli(str(self.parts), "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("Dry run", output)
        self.assertEqual(len(list(self.parts.iterdir())), 2)

    def test_failed_merge_leaves_no_partial_file(self):
        self.dmm_parts(2)
        from video_merger import merge as merge_module

        real = merge_module.duration_tolerance
        merge_module.duration_tolerance = lambda total: -1.0  # every result "too long"
        try:
            code, output = run_cli(str(self.parts))
        finally:
            merge_module.duration_tolerance = real
        self.assertEqual(code, 1)
        self.assertIn("FAILED", output)
        self.assertEqual(sorted(p.suffix for p in self.parts.iterdir()), [".mkv", ".mkv"])

    def test_a_merged_size_far_from_the_parts_is_refused_and_parts_kept(self):
        self.dmm_parts(2)
        with mock.patch.object(planning, "size_tolerance", return_value=-1):  # every result "too big"
            code, output = run_cli(str(self.parts), "--delete-parts")
        self.assertEqual(code, 1)
        self.assertIn("but the parts add up to", output)
        self.assertEqual(sorted(p.suffix for p in self.parts.iterdir()), [".mkv", ".mkv"])  # parts not deleted

    def low_bitrate_parts(self, folder: Path, extension: str) -> list[Path]:
        """Two 40-second parts at 100 kbit/s video + 32 kbit/s sound: the container's room for each packet is a few
        percent of the file, as for DMM's 300 kbit/s downloads."""
        folder.mkdir()
        return [make_clip(folder / f"low part{n}.{extension}", seconds=40, options=("-b:v", "100k", "-b:a", "32k"),
                          comment={"file_id": "abc00123", "part": str(n)}) for n in (1, 2)]

    @mock.patch.object(planning, "SIZE_FLOOR", 0)  # small clips: within 1%, as a real video (1 MiB at least)
    def test_low_bitrate_parts_made_into_the_other_kind_of_file(self):
        # MP4 needs more room per packet than Matroska: the parts as one .mkv are smaller, as one .mp4 bigger.
        for source, target in (("mp4", "mkv"), ("mkv", "mp4")):
            folder = self.dir / f"{source} parts"
            parts = self.low_bitrate_parts(folder, source)
            code, output = run_cli(str(folder), "--container", target)
            self.assertEqual(code, 0, output)
            total, size = sum(p.stat().st_size for p in parts), (folder / f"abc00123.{target}").stat().st_size
            self.assertGreater(abs(size - total), total / 100, (source, size, total))  # refused by the parts' size

    @unittest.skipUnless(DROPS_PACKETS, "this ffmpeg can't leave packets out")
    @mock.patch.object(planning, "SIZE_FLOOR", 0)
    def test_a_merged_file_missing_packets_is_refused(self):
        real_join = merging._join

        def join_losing_packets(group, target, partial, *args):
            real_join(group, target, partial, *args)
            whole = partial.with_name("whole" + partial.suffix)
            partial.replace(whole)
            # Every 50th packet of each stream left out (key frames kept): 2% of the packets, the same length.
            subprocess.run([str(TOOLS.ffmpeg), "-v", "error", "-i", str(whole), "-map", "0:v", "-map", "0:a",
                            "-map_chapters", "0", "-c", "copy", "-bsf", r"noise=drop=eq(mod(n\,50)\,17)", str(partial)],
                           check=True)
            whole.unlink()

        for source, target, kind in (("mp4", "mkv", "Matroska"), ("mkv", "mp4", "MP4")):
            folder = self.dir / f"{source} parts"
            parts = self.low_bitrate_parts(folder, source)
            with mock.patch.object(merging, "_join", side_effect=join_losing_packets):
                code, output = run_cli(str(folder), "--container", target, "--delete-parts")
            self.assertEqual(code, 1, output)
            self.assertRegex(output, rf"FAILED: the merged file is [0-9.]+ MB, but the parts add up to [0-9.]+ MB "
                                     rf"\(about [0-9.]+ MB as {kind}\)")
            self.assertEqual(sorted(folder.iterdir()), parts)  # nothing kept, the parts not deleted

    def test_files_from_a_list_with_dmm_s_part_count(self):
        paths = self.dmm_parts(3)
        other = self.dir / "other"
        other.mkdir()
        make_clip(other / "x1.mkv", comment={"file_id": "xyz00001", "part": "1"})
        make_clip(other / "x2.mkv", comment={"file_id": "xyz00001", "part": "2"})
        listing = self.dir / "list.txt"
        listing.write_text("# parts to merge\n" + "".join(f"{p}\n" for p in paths), encoding="utf-8")
        dmm = self.dir / "dmm.csv"
        dmm.write_text("ids,parts\nabc00123,3\n", encoding="utf-8")
        code, output = run_cli("--files-from", str(listing), "--dmm-parts", str(dmm), "--delete-parts")
        self.assertEqual(code, 0, output)
        self.assertNotIn("xyz00001", output)  # not listed: not even read
        self.assertEqual([p.name for p in self.parts.iterdir()], ["abc00123.mkv"])
        self.assertEqual(len(list(other.iterdir())), 2)

    def test_the_checked_file_is_on_the_disk_before_its_rename(self):
        self.dmm_parts(2)
        events = []
        real_flush, real_replace = merging._flush, os.replace

        def flush(path, folder=False):
            events.append(("folder" if folder else "file", Path(path).name))
            real_flush(path, folder)

        def replace(source, target):
            if "abc00123" in str(target):
                events.append(("rename", Path(source).name, Path(target).name))
            real_replace(source, target)

        with mock.patch.object(merging, "_flush", side_effect=flush), mock.patch.object(merging.os, "replace", side_effect=replace):
            code, output = run_cli(str(self.parts), "--delete-parts")
        self.assertEqual(code, 0, output)
        self.assertEqual(events, [("file", "abc00123.partial.mkv"), ("rename", "abc00123.partial.mkv", "abc00123.mkv"),
                                  ("folder", "it's here")])
        with mock.patch.object(merging.os, "fsync", wraps=os.fsync) as fsync:
            merging._flush(self.parts / "abc00123.mkv")
            merging._flush(self.parts, folder=True)
        self.assertEqual(fsync.call_count, 1 if os.name == "nt" else 2)  # a folder: Linux and macOS only

    def test_parts_are_only_deleted_when_the_merged_file_is_surely_on_the_disk(self):
        self.dmm_parts(2)
        with mock.patch.object(merging.os, "fsync", side_effect=OSError(5, "Input/output error")):
            code, output = run_cli(str(self.parts), "--delete-parts")
            self.assertEqual(code, 1, output)
            self.assertIn("FAILED: the merged file could not be written to the disk for sure (Input/output error), "
                          "so the parts are kept", output)
            self.assertEqual(sorted(p.name for p in self.parts.iterdir()), ["b_second.mkv", "z_first.mkv"])
            with self.assertLogs("video_merger.merge", "WARNING") as logged:
                code, output = run_cli(str(self.parts))  # the parts stay anyway: merged, the failure logged
        self.assertEqual(code, 0, output)
        self.assertTrue(any("could not flush" in line for line in logged.output))

    def test_files_from_a_list_written_by_windows_powershell(self):
        paths = self.dmm_parts(2)
        listing = self.dir / "list.txt"
        listing.write_bytes(b"\xff\xfe" + "".join(f"{p}\r\n" for p in paths).encode("utf-16-le"))  # PowerShell 5.1: > list.txt
        code, output = run_cli("--files-from", str(listing), "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"-> {self.parts / 'abc00123.mkv'}", output)
        listing.write_bytes(f"{paths[0]}\n".encode("utf-16-le"))  # no byte order mark
        code, output = run_cli("--files-from", str(listing), "--dry-run")
        self.assertEqual(code, cli.EXIT_SETUP, output)
        self.assertIn(f"Can't read the file list {listing}: it looks like UTF-16 text without a byte order mark", output)

    def test_an_output_folder_that_cant_be_made_fails_each_video_not_the_run(self):
        self.dmm_parts(2)
        for n in (1, 2):
            make_clip(self.parts / f"My Video part{n}.mkv")
        (self.dir / "a file").write_bytes(b"")
        code, output = run_cli(str(self.parts), "--out", str(self.dir / "a file" / "Merged"), "--delete-parts")
        self.assertEqual(code, 1, output)
        self.assertEqual(output.count(f"FAILED: can't save in {self.dir / 'a file' / 'Merged'} ("), 2, output)
        self.assertIn("Merged: 0  Failed: 2", output)
        self.assertEqual(len(list(self.parts.iterdir())), 4)

    def test_an_unexpected_error_fails_that_video_only(self):
        self.dmm_parts(2)
        for n in (1, 2):
            make_clip(self.parts / f"My Video part{n}.mkv")
        calls = []

        def first_fails(item, *args, **kwargs):
            calls.append(item.group.name)
            if len(calls) == 1:
                raise RuntimeError("something odd")
            return merge(item, *args, **kwargs)

        with mock.patch.object(cli, "merge", side_effect=first_fails):
            code, output = run_cli(str(self.parts))
        self.assertEqual(code, 1, output)
        self.assertIn("FAILED: unexpected error: something odd", output)
        self.assertIn("Merged: 1  Failed: 1", output)


class MergeErrorTest(unittest.TestCase):
    """Errors around the join fail that one video with a reason (the stand-in tools are never started)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vm errors ")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        groups, _ = group_files([media(str(self.dir / f"x{n}.mkv"), "abc00123", n) for n in (1, 2)])
        self.item = build_plan(groups, None)[0]
        self.tools = tools.Tools(Path("ffmpeg"), Path("ffprobe"))

    def test_free_space_or_work_folder_that_cant_be_read_or_made(self):
        with mock.patch.object(merging.shutil, "disk_usage", side_effect=PermissionError(13, "Access is denied")):
            result = merge(self.item, self.tools)
        self.assertEqual((result.ok, result.message), (False, f"can't save in {self.dir} (Access is denied)"))
        with mock.patch.object(merging.tempfile, "mkdtemp", side_effect=OSError(28, "No space left on device")):
            result = merge(self.item, self.tools)
        self.assertFalse(result.ok)
        self.assertRegex(result.message, r"^can't make a folder for the work files in .+ \(No space left on device\)$")

    def test_an_unfinished_file_that_cant_be_deleted(self):
        partial = merging.partial_path(self.item.target)
        real_unlink = Path.unlink

        def join(group, target, partial_file, *args):
            partial_file.write_bytes(b"half")
            raise merging.MergeFailed("ffmpeg failed (exit code 1)")

        def unlink(path, missing_ok=False):
            if path == partial:
                raise PermissionError(13, "The file is in use by another program")
            real_unlink(path, missing_ok=missing_ok)

        with mock.patch.object(merging, "_join", side_effect=join), mock.patch.object(Path, "unlink", unlink), \
                self.assertLogs("video_merger.merge", "WARNING") as logged:
            result = merge(self.item, self.tools)
        self.assertEqual((result.ok, result.message), (False, "ffmpeg failed (exit code 1)"))
        self.assertTrue(any("could not delete" in line and "in use" in line for line in logged.output))


if __name__ == "__main__":
    unittest.main()
