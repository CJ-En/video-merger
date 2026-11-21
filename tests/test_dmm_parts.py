import tempfile
import unittest
from pathlib import Path

from video_merger import dmm_parts
from video_merger.discover import group_files
from video_merger.plan import build_plan

from .helpers import media

# A DMM parts list: one line per part, or one per video when only the part count (or nothing) is known. Other columns
# ("note") are ignored.
PARTS_CSV = """ids,page_id,rates,parts,part,duration_s,file_id,note,error
172xrw344 172xrw00344,172xrw00344,3000:3 300:3,3,1,3000.0,172xrw00344,anything,
172xrw344 172xrw00344,172xrw00344,3000:3 300:3,3,2,3000.0,172xrw00344,,
172xrw344 172xrw00344,172xrw00344,3000:3 300:3,3,3,,,,length not known
abc00001,,,2,,,,,part count only
gone00001,,,,,,,,not found on DMM
"""


def load(text=PARTS_CSV):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "parts.csv"
        path.write_text(text, encoding="utf-8")
        return dmm_parts.load(path)


def parts(file_id, numbers, duration=3000.0):
    return [media(f"/x/{file_id}hhb{n}.mkv", file_id, n, duration=duration) for n in numbers]


class LoadTest(unittest.TestCase):
    def test_every_id_of_a_line_finds_the_video(self):
        dmm = load()
        video = dmm["172xrw00344"]
        self.assertIs(dmm["172xrw344"], video)
        self.assertEqual((video.parts, video.rates, video.lengths), (3, "3000:3 300:3", {1: 3000.0, 2: 3000.0}))
        self.assertEqual((dmm["abc00001"].parts, dmm["abc00001"].lengths), (2, {}))  # count only
        self.assertIsNone(dmm["gone00001"].parts)
        self.assertEqual(dmm["gone00001"].error, "not found on DMM")

    def test_a_later_line_gives_the_count_an_earlier_one_couldn_t(self):
        # Two lists joined into one file: no count, then a count.
        dmm = load(PARTS_CSV + "gone00001,,,2,,,,counted by hand,\n")
        self.assertEqual((dmm["gone00001"].parts, dmm["gone00001"].error), (2, ""))


class PlanWithDmmTest(unittest.TestCase):
    def plan(self, files, dmm=None):
        groups, singles = group_files(files)
        return build_plan(groups, None, dmm=load() if dmm is None else dmm), singles

    def test_all_parts_as_long_as_dmm_s_are_merged(self):
        (item,), _ = self.plan(parts("172xrw00344", [1, 2, 3]))
        self.assertEqual(item.action, "merge")  # part 3's length unknown: the count is enough

    def test_missing_last_part_is_refused(self):
        # Parts 1 and 2 look complete by their numbers alone; only DMM knows there is a part 3.
        (item,), _ = self.plan(parts("172xrw00344", [1, 2]))
        self.assertEqual((item.action, item.reason), ("problem", "DMM has 3 part(s), found part(s) 1, 2"))
        (item,), _ = self.plan(parts("abc00001", [1, 2, 3]))
        self.assertEqual(item.reason, "DMM has 2 part(s), found part(s) 1, 2, 3")

    def test_a_part_shorter_than_dmm_s_is_refused(self):
        files = parts("172xrw00344", [1, 3]) + parts("172xrw00344", [2], duration=2400.0)
        (item,), _ = self.plan(files)
        self.assertEqual(item.reason, "part 2 is 0:40:00 long, DMM's part 2 is 0:50:00")
        (item,), _ = self.plan(parts("172xrw00344", [1, 3]) + parts("172xrw00344", [2], duration=3001.5))
        self.assertEqual(item.action, "merge")  # within 2 seconds

    def test_videos_dmm_can_t_vouch_for_are_refused(self):
        (item,), _ = self.plan(parts("zzz00001", [1, 2]))
        self.assertIn("not in the DMM parts list", item.reason)
        (item,), _ = self.plan(parts("gone00001", [1, 2]))
        self.assertEqual(item.reason, "DMM's part count is unknown (not found on DMM)")
        (item,), _ = self.plan([media("/x/My Video-pt1.mkv"), media("/x/My Video-pt2.mkv")])
        self.assertIn("no DMM comment tag", item.reason)

    def test_without_the_list_nothing_changes(self):
        groups, _ = group_files(parts("172xrw00344", [1, 2]))
        self.assertEqual(build_plan(groups, None)[0].action, "merge")

    def test_bitrates_with_other_part_counts_are_shown(self):
        dmm = load(PARTS_CSV.replace("3000:3 300:3", "3000:3 300:2"))
        (item,), _ = self.plan(parts("172xrw00344", [1, 2]), dmm)
        self.assertIn("(DMM's parts per bitrate: 3000:3 300:2)", item.reason)

    def test_the_only_part_here_is_reported(self):
        dmm = load()
        plan, singles = self.plan(parts("abc00001", [1]) + [media("/x/whole.mkv", "abc00002")], dmm)
        self.assertEqual(plan, [])
        found = dmm_parts.lone_parts(singles, dmm)
        self.assertEqual([(m.path.name, reason) for m, reason in found],
                         [("abc00001hhb1.mkv", "only part 1 of abc00001 is here, DMM has 2 parts")])


if __name__ == "__main__":
    unittest.main()
