import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from unittest import mock

from video_merger import plan as planning
from video_merger.discover import group_files, loose_part_from_name, part_from_name
from video_merger.plan import build_plan, size_mismatch, size_tolerance, size_window, sizes_text
from video_merger.probe import (MediaFile, ProbeError, container_name, dump_bytes, hevc_settings, mp4_payload,
                                parse_dmm_comment, parse_duration, parse_probe)

from .helpers import AAC, H264, VP9, media


def hvcc(*units: bytes, flag: int = 0x80, length_size: int = 4) -> bytes:
    """An HEVC header as MP4 writes it (Matroska: flag 0), one array per NAL unit."""
    arrays = b"".join(bytes([flag | (unit[0] >> 1) & 0x3F]) + (1).to_bytes(2, "big") + len(unit).to_bytes(2, "big") + unit
                      for unit in units)
    return b"\x01" + bytes(20) + bytes([0xFC | (length_size - 1), len(units)]) + arrays


def ffprobe_dump(data: bytes) -> str:
    """Bytes as ffprobe's -show_data writes them."""
    lines = []
    for at in range(0, len(data), 16):
        chunk = data[at:at + 16]
        pairs = " ".join(chunk[i:i + 2].hex() for i in range(0, len(chunk), 2))
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{at:08x}: {pairs:<40} {text}")
    return "\n" + "\n".join(lines) + "\n"


class PartNameTest(unittest.TestCase):
    def test_part_patterns(self):
        self.assertEqual(part_from_name("abc00123-pt2"), ("abc00123", 2))
        self.assertEqual(part_from_name("abc00123 part 10"), ("abc00123", 10))
        self.assertEqual(part_from_name("abc00123.cd1"), ("abc00123", 1))
        self.assertEqual(part_from_name("abc00123_3"), ("abc00123", 3))
        self.assertEqual(part_from_name("My Video Part02"), ("My Video", 2))
        self.assertEqual(part_from_name("ABC-123-2"), ("ABC-123", 2))
        self.assertEqual(part_from_name("ABC-123 B"), ("ABC-123", 2))
        self.assertEqual(part_from_name("ABC-123_1"), ("ABC-123", 1))
        self.assertEqual(part_from_name("h_1133gone00123_2"), ("h_1133gone00123", 2))
        self.assertEqual(part_from_name("sone096_1"), ("sone096", 1))  # never padded/unpadded

    def test_not_parts(self):
        for stem in (
            "ABC-012", "abc00123", "h_51cmn001", "h_1133gone00123", "ABP-01", "abc00123_012", "2023_1",
            "FC2-PPV-1234567", "ABC-123A", "ABC-123-E", "abc00123hhb1", "1jera000102d_v1_drm_a_4k1",
        ):
            self.assertIsNone(part_from_name(stem), stem)

    def test_any_letters_and_roman_numbers(self):
        self.assertEqual(part_from_name("(見本スタジオ)(4037-399)見本動画185_(見本花子)_2"), ("(見本スタジオ)(4037-399)見本動画185_(見本花子)", 2))
        self.assertEqual(part_from_name("動画 part 3"), ("動画", 3))
        self.assertEqual(part_from_name("My Video Part II"), ("My Video", 2))
        self.assertEqual(part_from_name("My Video - pt iv"), ("My Video", 4))
        for stem in ("abcd00001", "hzpt00002", "adapti", "Script", "My Video Partii"):
            self.assertIsNone(part_from_name(stem), stem)  # "cd", "pt" inside a word: no part

    def test_looser_names(self):
        for stem, expected in (
            ("film006-1", ("film006", 1)),
            ("film278-2", ("film278", 2)),
            ("Film 2", ("Film", 2)),
            ("My Video - 3", ("My Video", 3)),
            ("FC2-PPV-1234567-2", ("FC2-PPV-1234567", 2)),
            ("[PPV1007] Sample Studio (iii)(Hanako Mihon)[720p]", ("[PPV1007] Sample Studio (Hanako Mihon)[720p]", 3)),
            ("Name [Part 2] 1080p", ("Name 1080p", 2)),
        ):
            self.assertIsNone(part_from_name(stem), stem)  # not a clear name: counted only with its other parts
            self.assertEqual(loose_part_from_name(stem), expected, stem)
        for stem in ("Video (2)", "Film 541", "ABC-012", "2023 1", "Interview 245", "abc00123", "Name (Actress)", "Hanako Age 019"):
            self.assertIsNone(loose_part_from_name(stem), stem)


class ProbeParseTest(unittest.TestCase):
    def test_dmm_comment(self):
        self.assertEqual(parse_dmm_comment('{"file_id":"1JERA00010","part":"2"}'), ("1jera00010", 2))
        self.assertEqual(parse_dmm_comment('{"file_id":"abc00123"}'), ("abc00123", None))
        self.assertEqual(parse_dmm_comment('{"file_id":"../x","part":"0"}'), (None, None))
        self.assertEqual(parse_dmm_comment("not json"), (None, None))

    def test_durations(self):
        self.assertAlmostEqual(parse_duration("02:28:59.798000000"), 8939.798)
        self.assertAlmostEqual(parse_duration("8939.798000"), 8939.798)
        self.assertIsNone(parse_duration("N/A"))

    def test_parse_probe_reads_matroska_tags(self):
        data = {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "vp9", "width": 3840, "height": 2160, "tags": {"DURATION": "00:01:40.000000000"}},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
            ],
            "format": {"format_name": "matroska,webm", "duration": "100.0", "tags": {"COMMENT": '{"file_id":"1jera00010","part":"2"}'}},
        }
        result = parse_probe(Path("x.mkv"), 5, data)
        self.assertEqual((result.file_id, result.part, result.duration, result.container), ("1jera00010", 2, 100.0, "Matroska"))
        self.assertEqual(result.video.duration, 100.0)

    def test_the_kind_of_file_is_what_ffprobe_found(self):
        for format_name, name in (("matroska,webm", "Matroska"), ("mov,mp4,m4a,3gp,3g2,mj2", "MP4"), ("asf", "ASF"),
                                  ("mpegts", "MPEGTS"), ("", ""), (None, "")):
            self.assertEqual(container_name(format_name), name, format_name)

    def test_hevc_settings_are_the_parameter_sets_of_the_header(self):
        vps, sps, pps = b"\x40\x01\x0c\x01\xff", b"\x42\x01\x01\x01\x60", b"\x44\x01\xc1\x72"
        sei = b"\x4e\x01\x05x265 - options: frame-threads=4"
        base = hevc_settings(hvcc(vps, sps, pps, sei))
        self.assertEqual(len(base), 64)
        self.assertEqual(hevc_settings(hvcc(vps, sps, pps, b"\x4e\x01\x05x265 - options: frame-threads=1")), base)
        self.assertEqual(hevc_settings(hvcc(vps, sps, pps, flag=0)), base)  # Matroska's hvcC: other array flags
        self.assertNotEqual(hevc_settings(hvcc(vps, sps, b"\x44\x01\xc1\x73")), base)  # another PPS
        self.assertNotEqual(hevc_settings(hvcc(vps, sps, pps, length_size=2)), base)
        self.assertEqual(hevc_settings(b"\0\0\0\x01" + vps + b"\0\0\x01" + sps + b"\0\0\0\x01" + pps),
                         hevc_settings(b"\0\0\x01" + pps + b"\0\0\x01" + sps + b"\0\0\x01" + vps + b"\0\0\x01" + sei))
        self.assertEqual(hevc_settings(b"\x01\x02"), hevc_settings(b"\x01\x02"))  # unreadable: the whole header
        self.assertNotEqual(hevc_settings(b"\x01\x02"), hevc_settings(b"\x01\x03"))
        self.assertEqual(hevc_settings(None), "")

    def test_parse_probe_reads_the_hevc_header_dump(self):
        header = hvcc(b"\x40\x01\x0c", b"\x42\x01\x01", b"\x44\x01\xc1", b"\x4e\x01" + bytes(range(40)))
        self.assertEqual(dump_bytes(ffprobe_dump(header)), header)
        self.assertIsNone(dump_bytes("not a dump"))
        streams = [{"index": 0, "codec_type": "video", "codec_name": codec, "extradata": ffprobe_dump(header)}
                   for codec in ("hevc", "h264")]
        hevc, h264 = parse_probe(Path("x.mkv"), 5, {"streams": streams, "format": {}}).streams
        self.assertEqual(hevc.settings, hevc_settings(header))
        self.assertEqual(h264.settings, "")  # H.264 parts with other settings join fine

    def test_an_mp4_files_packets_are_what_its_mdat_boxes_hold(self):
        folder = Path(tempfile.mkdtemp(prefix="vm boxes ")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)

        def box(kind: bytes, body: bytes, size64: bool = False, to_end: bool = False) -> bytes:
            if size64:
                return (1).to_bytes(4, "big") + kind + (16 + len(body)).to_bytes(8, "big") + body
            return (0 if to_end else 8 + len(body)).to_bytes(4, "big") + kind + body

        def payload(data: bytes):
            path = folder / "x.mp4"
            path.write_bytes(data)
            return mp4_payload(path)

        head = box(b"ftyp", b"isom" + bytes(8)) + box(b"moov", bytes(500)) + box(b"free", bytes(30))
        self.assertEqual(payload(head + box(b"mdat", bytes(1000))), 1000)
        # Fragments (moof + mdat), a 64-bit size, and a last box that runs to the end of the file.
        self.assertEqual(payload(head + box(b"moof", bytes(90)) + box(b"mdat", bytes(700), size64=True)
                                 + box(b"moof", bytes(90)) + box(b"mdat", bytes(300), to_end=True)), 1000)
        self.assertIsNone(payload(head + box(b"mdat", bytes(1000))[:-1]))  # cut short: the box doesn't fit
        self.assertIsNone(payload(head + b"\0\0\0\x04mdat"))  # a box smaller than its header
        self.assertIsNone(payload(head + b"\0\0"))
        self.assertIsNone(mp4_payload(folder / "missing.mp4"))

    def test_mkvmerge_statistics_count_only_in_files_mkvmerge_wrote(self):
        def streams(language: str = ""):
            tags = [{f"NUMBER_OF_FRAMES{language}": "3596", f"NUMBER_OF_BYTES{language}": "4434102"},
                    {f"NUMBER_OF_FRAMES{language}": "5626", f"NUMBER_OF_BYTES{language}": "964448"}]
            return [{"index": 0, "codec_type": "video", "codec_name": "h264", "tags": tags[0]},
                    {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": tags[1]}]

        def read(writer: str, raw: list) -> MediaFile:
            return parse_probe(Path("x.mkv"), 5_441_330, {"streams": raw, "format": {"format_name": "matroska,webm",
                                                                                     "tags": {"encoder": writer}}})

        mkvmerge = "libebml v1.4.5 + libmatroska v1.7.1"
        for raw in (streams(), streams("-eng")):  # older mkvmerge: with a language
            media = read(mkvmerge, raw)
            self.assertEqual((media.payload, [s.frames for s in media.streams]), (5_398_550, [3596, 5626]))
        # ffmpeg copies the tags of the file it read into the file it writes (a join keeps part 1's): never counted.
        media = read("Lavf62.19.101", streams())
        self.assertEqual((media.payload, [s.frames for s in media.streams]), (None, [0, 0]))
        raw = streams()
        del raw[1]["tags"]["NUMBER_OF_BYTES"]
        self.assertIsNone(read(mkvmerge, raw).payload)  # a track without its bytes: not known
        mp4 = parse_probe(Path("x.mp4"), 5, {"streams": [{"index": 0, "codec_type": "video", "nb_frames": "7192"}],
                                             "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}})
        self.assertEqual(mp4.video.frames, 7192)


class GroupTest(unittest.TestCase):
    def test_groups_by_comment_in_numeric_order(self):
        files = [media(f"d/x{n}.mkv", "abc00123", n) for n in (10, 2, 1, 3, 4, 5, 6, 7, 8, 9)]
        groups, singles = group_files(files)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].numbers, list(range(1, 11)))
        self.assertEqual(groups[0].name, "abc00123")
        self.assertEqual(singles, [])

    def test_groups_by_name_per_folder(self):
        files = [media("a/abc00123_2.mkv"), media("a/abc00123_1.mkv"), media("b/abc00123_1.mkv"), media("b/abc00123_2.mkv")]
        groups, _ = group_files(files)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(g.numbers == [1, 2] for g in groups))

    def test_missing_and_duplicate_parts(self):
        files = [media("a/1.mkv", "abc00123", 1), media("a/3.mkv", "abc00123", 3), media("b/3.mkv", "abc00123", 3)]
        groups, _ = group_files(files)
        problems = " ".join(groups[0].problems)
        self.assertIn("part 3 was found 2 times", problems)
        self.assertIn("missing part(s) 2", problems)

    def test_padding_variants_are_different_videos(self):
        files = [media(f"d/{n}.mkv", cid, n) for cid in ("sone096", "sone00096") for n in (1, 2)]
        files += [media(f"e/{cid}_{n}.mkv") for cid in ("sone096", "sone00096") for n in (1, 2)]
        groups, _ = group_files(files)
        self.assertEqual(sorted(g.name for g in groups), ["sone00096", "sone00096", "sone096", "sone096"])
        self.assertTrue(all(g.numbers == [1, 2] and not g.problems for g in groups))

    def test_dmm_suffixes_grouped_by_comment(self):
        names = ["1jera000102d_v1_drm_a_4k2", "1jera000102d_v1_drm_a_4k1"]
        names2 = ["1mist00432re01hhbs1", "1mist00432re01hhbs2"]
        files = [media(f"d/{s}.mkv", "1jera00010", int(s[-1])) for s in names]
        files += [media(f"d/{s}.mkv", "1mist00432re01", int(s[-1])) for s in names2]
        groups, _ = group_files(files)
        self.assertEqual([(g.name, g.numbers) for g in groups], [("1jera00010", [1, 2]), ("1mist00432re01", [1, 2])])
        self.assertTrue(groups[0].parts[0].path.name.endswith("4k1.mkv"))

    def test_dmm_suffixes_without_comment_are_left_alone(self):
        groups, singles = group_files([media("d/abc00123hhb1.mkv"), media("d/abc00123hhb2.mkv")])
        self.assertEqual((groups, len(singles)), ([], 2))

    def test_whole_videos_are_not_parts(self):
        files = [media("a/abc00123.mkv", "abc00123"), media("a/xyz00001.mkv", "xyz00001", 1), media("a/other.mp4")]
        groups, singles = group_files(files)
        self.assertEqual(groups, [])
        self.assertEqual(len(singles), 3)

    def test_a_tagged_file_without_part_is_a_whole_video_whatever_its_name(self):
        # DMM's whole video twice, the copy named like a part by a download program.
        files = [media(f"a/abc00123_{n}.mkv", "abc00123") for n in (1, 2)] + [media("a/abc00123 part 2.mkv", "abc00123")]
        files += [media(f"b/xyz00001-pt{n}.mkv", "xyz00001", n) for n in (1, 2)]  # the tag's part numbers count
        groups, singles = group_files(files)
        self.assertEqual([(g.name, g.numbers, g.problems) for g in groups], [("xyz00001", [1, 2], [])])
        self.assertEqual(sorted(s.path.name for s in singles), ["abc00123 part 2.mkv", "abc00123_1.mkv", "abc00123_2.mkv"])

    def test_only_a_later_part(self):
        groups, _ = group_files([media("a/x.mkv", "abc00123", 2)])
        self.assertIn("missing part(s) 1", groups[0].problems)

    def test_looser_names_need_part_1_and_another_part(self):
        names = ["film006-2", "film006-1", "Film 2", "Episode 5", "Episode 6", "Name (ii) x", "Name (i) x", "Video", "Video (1)"]
        files = [media(f"a/{name}.mkv") for name in names] + [media("b/film006-2.mkv")]
        files += [media(f"a/abc00123 {n}.mkv", "abc00123") for n in (1, 2)]  # DMM's whole video twice: not parts
        groups, singles = group_files(files)
        self.assertEqual([(g.name, g.numbers, g.problems) for g in groups], [("film006", [1, 2], []), ("Name x", [1, 2], [])])
        self.assertEqual(groups[0].parts[0].path.name, "film006-1.mkv")
        self.assertEqual(sorted(s.path.as_posix() for s in singles), sorted(
            ["a/Film 2.mkv", "a/Episode 5.mkv", "a/Episode 6.mkv", "a/Video.mkv", "a/Video (1).mkv", "b/film006-2.mkv",
             "a/abc00123 1.mkv", "a/abc00123 2.mkv"]))

    def test_looser_names_with_a_gap_or_a_clear_part(self):
        groups, _ = group_files([media("a/x-1.mkv"), media("a/x-3.mkv"), media("a/y_1.mkv"), media("a/y 2.mkv")])
        self.assertEqual([(g.name, g.numbers, g.problems) for g in groups], [("x", [1, 3], ["missing part(s) 2"]), ("y", [1, 2], [])])


class PlanTest(unittest.TestCase):
    def plan(self, files, **kwargs):
        groups, _ = group_files(files)
        return build_plan(groups, kwargs.pop("out", None), **kwargs)

    def test_merge_target_is_content_id(self):
        item = self.plan([media("d/a.mkv", "abc00123", 1), media("d/b.mkv", "abc00123", 2)])[0]
        self.assertEqual(item.action, "merge")
        self.assertEqual(item.target, Path("d/abc00123.mkv"))

    def test_vp9_stays_separate(self):
        item = self.plan([media("d/a.mkv", "abc00123", n, streams=(VP9, AAC)) for n in (1, 2)])[0]
        self.assertEqual(item.action, "skip")
        self.assertIn("VP9", item.reason)

    def test_mismatched_parts(self):
        small = H264.__class__(0, "video", "h264", "High", 1280, 720, "yuv420p", "30000/1001")
        item = self.plan([media("d/a.mkv", "abc00123", 1), media("d/b.mkv", "abc00123", 2, streams=(small, AAC))])[0]
        self.assertEqual(item.action, "problem")
        self.assertIn("1280x720", item.reason)

    def test_hevc_parts_encoded_with_other_settings(self):
        def hevc(settings):
            return H264.__class__(0, "video", "hevc", "Main", 1920, 1080, "yuv420p", "30000/1001", settings=settings)

        item = self.plan([media("d/a.mkv", "abc00123", 1, streams=(hevc("aa"), AAC)), media("d/b.mkv", "abc00123", 2, streams=(hevc("bb"), AAC))])[0]
        self.assertEqual((item.action, item.kind), ("problem", "mismatch"))
        self.assertEqual(item.reason, "the parts don't match (part 2's HEVC video was encoded with other settings than part 1's)")
        item = self.plan([media(f"d/{n}.mkv", "abc00123", n, streams=(hevc("aa"), AAC)) for n in (1, 2)])[0]
        self.assertEqual(item.action, "merge")

    def test_frame_rate_estimates_of_one_source_match_but_real_differences_dont(self):
        def rate(value):
            return H264.__class__(0, "video", "h264", "High", 1920, 1080, "yuv420p", value, duration=100.0)

        # ffprobe's estimates from Matroska timestamps, as seen in DMM parts (29.957-29.970, 29.999 / 30).
        for first, second in (("16543/552", "30000/1001"), ("4853/162", "989/33"), ("15373/513", "29309/978"),
                              ("30/1", "29999/1000"), ("2997/125", "24000/1001")):
            parts = [media("d/a.mkv", "abc00123", 1, streams=(rate(first), AAC)),
                     media("d/b.mkv", "abc00123", 2, streams=(rate(second), AAC))]
            self.assertEqual(self.plan(parts)[0].action, "merge", (first, second))
        for first, second in (("30000/1001", "30/1"), ("4853/162", "30/1"), ("24000/1001", "24/1"), ("25/1", "30/1"),
                              ("27/1", "28/1")):
            parts = [media("d/a.mkv", "abc00123", 1, streams=(rate(first), AAC)),
                     media("d/b.mkv", "abc00123", 2, streams=(rate(second), AAC))]
            self.assertEqual(self.plan(parts)[0].action, "problem", (first, second))

    def test_audio_drift_warning(self):
        late = AAC.__class__(1, "audio", "aac", "LC", sample_rate="48000", channels=2, duration=95.0)
        item = self.plan([media("d/a.mkv", "abc00123", 1), media("d/b.mkv", "abc00123", 2, streams=(H264, late))])[0]
        self.assertEqual(item.action, "merge")
        self.assertIn("part 2", item.warnings[0])

    def test_only_web_dl_parts_are_merged(self):
        from video_merger.probe import Stream

        cases = {
            "pcm_bluray": (H264, Stream(1, "audio", "pcm_bluray")),
            "truehd": (H264, Stream(1, "audio", "truehd")),
            "WMV/VC-1": (Stream(0, "video", "vc1"), AAC),
            "mpeg2video": (Stream(0, "video", "mpeg2video"), Stream(1, "audio", "ac3")),
            "hdmv_pgs_subtitle": (H264, AAC, Stream(2, "subtitle", "hdmv_pgs_subtitle")),
        }
        for reason, streams in cases.items():
            item = self.plan([media(f"d/a{n}.mkv", "abc00123", n, streams=streams) for n in (1, 2)])[0]
            self.assertEqual(item.action, "skip", reason)
            self.assertIn(reason, item.reason)

    def test_remux_names_are_not_merged(self):
        for name in ("ABC-123 BD Remux", "ABC-123.BluRay", "abc00123 DVDRip", "ABC-123 BD50", "ABC-123 DVD"):
            item = self.plan([media(f"d/{name} part{n}.mkv") for n in (1, 2)])[0]
            self.assertEqual(item.action, "skip", name)
            self.assertIn("Remux", item.reason)
        item = self.plan([media(f"BD Remux/{n}.mkv", "abc00123", n) for n in (1, 2)])[0]
        self.assertEqual(item.action, "skip")
        # Content ids that merely contain "bd" or "dvd" are fine.
        for cid in ("bdsr00123", "dvdes00123", "1bd00012"):
            item = self.plan([media(f"d/{n}.mkv", cid, n) for n in (1, 2)])[0]
            self.assertEqual(item.action, "merge", cid)

    def test_a_file_under_the_outputs_name_must_be_this_video(self):
        folder = Path(tempfile.mkdtemp(prefix="vm plan ")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        for name in ("abc00123.mkv", "My Video.mkv", "stu00901.wmv"):
            (folder / name).write_bytes(b"x")
        found = {}

        def read(path):
            if path.name not in found:
                raise ProbeError("Invalid data found when processing input")
            return found[path.name]

        files = [media(str(folder / f"t{n}.mkv"), "abc00123", n) for n in (1, 2)]  # 100 s each
        files += [media(str(folder / f"My Video part{n}.mkv")) for n in (1, 2)]
        files += [media(str(folder / f"s{n}.wmv"), "stu00901", n, streams=()) for n in (1, 2)]

        def plan(overwrite=False):
            groups, _ = group_files(files)
            return {i.group.name: i for i in build_plan(groups, None, overwrite=overwrite, read=read)}

        items = plan(overwrite=True)  # can't be read: may be another video
        self.assertEqual({n: (i.action, i.kind) for n, i in items.items()},
                         {"abc00123": ("problem", "taken"), "My Video": ("problem", "taken"), "stu00901": ("problem", "taken")})
        self.assertEqual(items["abc00123"].reason, f"{folder / 'abc00123.mkv'} is already there and can't be read "
                                                   "(Invalid data found when processing input), so it may be another video")
        found["abc00123.mkv"] = media("abc00123.mkv", "xyz00001", duration=200.0)
        found["My Video.mkv"] = media("My Video.mkv", duration=150.0)
        found["stu00901.wmv"] = media("stu00901.wmv", duration=100.0)  # AsfBin's result may have no tag: its length
        items = plan(overwrite=True)
        self.assertEqual([i.kind for i in items.values()], ["taken", "taken", "taken"])
        self.assertTrue(items["abc00123"].reason.endswith("is already there, but it is another video (its DMM tag says xyz00001)"))
        self.assertTrue(items["My Video"].reason.endswith("is already there, but it is another video (0:02:30 long, the parts together 0:03:20)"))
        found["My Video.mkv"] = MediaFile(Path("My Video.mkv"), 30_000_000, 200.0, (H264, AAC))
        self.assertTrue(plan()["My Video"].reason.endswith("another video (30.0 MB, the parts together 0.0 MB)"))
        found["abc00123.mkv"] = media("abc00123.mkv", "abc00123", duration=50.0)  # this video: its tag
        found["My Video.mkv"] = media("My Video.mkv", duration=201.5)  # this video: as long as the parts (2 s)
        found["stu00901.wmv"] = media("stu00901.wmv", duration=199.0)
        self.assertEqual([(i.action, i.kind) for i in plan().values()], [("skip", "already")] * 3)
        items = plan(overwrite=True)
        self.assertEqual([(i.action, i.kind) for i in items.values()], [("merge", "again")] * 3)
        stat = (folder / "abc00123.mkv").stat()
        self.assertEqual(items["abc00123"].replaces, (stat.st_size, stat.st_mtime_ns))  # merge() replaces only this file
        found["abc00123.mkv"] = media("abc00123.mkv")
        self.assertEqual(plan()["abc00123"].kind, "taken")  # no tag (and no .wmv): not taken for this video
        groups, _ = group_files(files[:2])
        self.assertEqual(build_plan(groups, None, overwrite=True)[0].kind, "taken")  # not read: never skipped nor replaced

    def test_parts_in_different_containers(self):
        # What ffprobe found inside, not the extension: ffmpeg's concat drops what follows part 1 of another kind.
        mkv, mp4 = (replace(media(f"d/{n}.mkv", "abc00123", n), container=kind) for n, kind in ((1, "Matroska"), (2, "MP4")))
        item = self.plan([mkv, mp4])[0]
        self.assertEqual((item.action, item.kind, item.target), ("problem", "containers", None))
        self.assertEqual(item.reason, "part 2 is MP4 and part 1 is Matroska: remux them to the same kind first")
        item = self.plan([replace(media(f"d/x part{n}.{ext}"), container="MP4") for n, ext in ((1, "mkv"), (2, "mp4"))])[0]
        self.assertEqual(item.action, "merge")  # an MP4 named .mkv joins an MP4

    def test_two_groups_same_target(self):
        files = [media(f"{d}/abc00123_{n}.mkv") for d in ("a", "b") for n in (1, 2)]
        items = self.plan(files, out=Path("out"))
        self.assertEqual([i.action for i in items], ["problem", "problem"])


def sized(name: str, container: str, size: int, payload: int | None = None, video: int = 0, audio: int = 0,
          seconds: float = 1800.0) -> MediaFile:
    """A 640x360 H.264 + AAC file of this size; payload, video and audio packets as ffprobe reads them (0: unknown)."""
    streams = (replace(H264, width=640, height=360, duration=seconds, frames=video), replace(AAC, duration=seconds, frames=audio))
    return MediaFile(Path(name), size, seconds, streams, container=container, payload=payload)


class SizeCheckTest(unittest.TestCase):
    """The merged file's size against the parts': the same streams, in another kind of file (measured with ffmpeg)."""

    # Two 30-minute MP4 parts at 300 kbit/s video + 64 kbit/s AAC (one of DMM's low bitrates), joined with ffmpeg.
    PARTS = [sized("tst00001_low1.mp4", "MP4", 83_921_646, 81_974_683, 53_946, 84_376),
             sized("tst00001_low2.mp4", "MP4", 83_921_747, 81_974_784, 53_946, 84_376)]
    AS_MKV = sized("tst00001.mkv", "Matroska", 165_927_448, seconds=3600.0)
    AS_MP4 = sized("tst00001.mp4", "MP4", 167_858_581, 163_965_491, 107_892, 168_752, 3600.0)

    def test_mp4_parts_make_a_smaller_matroska_file(self):
        total = sum(part.size for part in self.PARTS)
        self.assertGreater(total - self.AS_MKV.size, size_tolerance(total))  # 1.1% smaller: refused by the parts' size
        self.assertIsNone(size_mismatch(self.PARTS, self.AS_MKV))
        self.assertIsNone(size_mismatch(self.PARTS, self.AS_MP4))
        self.assertAlmostEqual(size_window(self.PARTS, self.AS_MKV)[0], self.AS_MKV.size, delta=self.AS_MKV.size / 5000)
        # 2% of the packets lost (their bytes, from the file and an MP4's count): refused, with sizes that read apart.
        lost = self.AS_MP4.payload // 50
        self.assertEqual(size_mismatch(self.PARTS, replace(self.AS_MKV, size=self.AS_MKV.size - lost)),
                         ("162.6 MB", "167.8 MB", "about 165.9 MB as Matroska"))
        self.assertEqual(size_mismatch(self.PARTS, replace(self.AS_MP4, size=self.AS_MP4.size - lost,
                                                           payload=self.AS_MP4.payload - lost)), ("164.6 MB", "167.8 MB", ""))

    def test_a_low_bitrate_matroska_file_made_earlier_is_this_video(self):
        folder = Path(tempfile.mkdtemp(prefix="vm sizes ")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "My Video.mkv").write_bytes(b"x")
        parts = [replace(part, path=folder / f"My Video part{n}.mp4", file_id=None, part=None)
                 for n, part in enumerate(self.PARTS, 1)]
        groups, _ = group_files(parts)
        for there, kind in ((self.AS_MKV, "already"), (replace(self.AS_MKV, size=self.AS_MKV.size - (4 << 20)), "taken")):
            item = build_plan(groups, None, read=lambda path: there)[0]
            self.assertEqual(item.kind, kind)
        self.assertEqual(item.reason, f"{folder / 'My Video.mkv'} is already there, but it is another video "
                                      "(161.7 MB, the parts together 167.8 MB, about 165.9 MB as Matroska)")

    @mock.patch.object(planning, "SIZE_FLOOR", 0)  # small clips: checked as a real video, 1% only
    def test_matroska_parts_make_a_bigger_mp4_file(self):
        # Two 2-minute parts at 300 kbit/s + 64 kbit/s: written by ffmpeg (their bytes not known), or by mkvmerge (it
        # writes their bytes, and packs audio packets: they take less room).
        by_ffmpeg = [sized("a1.mkv", "Matroska", 5_459_237, seconds=120.021),
                     sized("a2.mkv", "Matroska", 5_466_021, seconds=120.021)]
        by_mkvmerge = [sized("b1.mkv", "Matroska", 5_441_330, 5_398_550, 3596, 5626, 120.021),
                       sized("b2.mkv", "Matroska", 5_437_784, 5_395_002, 3596, 5626, 120.021)]
        for parts, joined in ((by_ffmpeg, sized("a.mp4", "MP4", 11_101_064, 10_794_058, 7192, 11_252, 240.042)),
                              (by_mkvmerge, sized("b.mp4", "MP4", 11_139_384, 10_794_702, 7192, 11_252, 240.042)),
                              (by_mkvmerge, sized("b.mkv", "Matroska", 10_926_753, seconds=240.041))):
            total = sum(part.size for part in parts)
            if joined.container == "MP4":
                self.assertGreater(joined.size - total, size_tolerance(total))  # 1.6% / 2.4% bigger
            self.assertIsNone(size_mismatch(parts, joined), joined.path)
            lost = 10_794_000 // 50
            payload = joined.payload and joined.payload - lost
            self.assertIsNotNone(size_mismatch(parts, replace(joined, size=joined.size - lost, payload=payload)), joined.path)

    def test_without_the_bytes_of_the_packets_the_parts_size(self):
        # A .wmv (AsfBin), or an MP4 whose boxes can't be read: the merged file is as big as the parts, within 1%.
        for kind, payload in (("ASF", None), ("MP4", None)):
            parts = [replace(part, container=kind, payload=payload) for part in self.PARTS]
            self.assertIsNone(size_mismatch(parts, replace(self.AS_MP4, container=kind, payload=payload)))
            self.assertEqual(size_window(parts, replace(self.AS_MKV, container=kind))[0], 167_843_393)
        self.assertEqual(size_mismatch(self.PARTS[:1], self.AS_MP4), ("167.9 MB", "83.9 MB", ""))

    def test_sizes_read_apart(self):
        self.assertEqual(sizes_text(165_927_448, 167_843_393), ["165.9 MB", "167.8 MB"])  # not "0.17 GB" twice
        self.assertEqual(sizes_text(2_431_000_000, 2_455_000_000), ["2.43 GB", "2.46 GB"])
        self.assertEqual(sizes_text(2_431_000_000, 2_434_000_000), ["2.431 GB", "2.434 GB"])
        self.assertEqual(sizes_text(5_000_000, 5_040_000, 5_000_000), ["5.00 MB", "5.04 MB", "5.00 MB"])


if __name__ == "__main__":
    unittest.main()
