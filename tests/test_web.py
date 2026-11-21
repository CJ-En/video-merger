"""The browser page's server: who may use it, the places to look in, a look, options that plan again, a merge with
progress and "Stop after this one", the folder browser and Quit. Files are read and merged by stand-ins here, except
in RealFilesTest (FFmpeg clips, and the AsfBin stand-in of tests/fake_asfbin.py)."""

import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from video_merger import cli, logs, merge as merging, settings as config, tools, web
from video_merger.merge import MergeResult
from video_merger.probe import MediaFile, ProbeError, Stream

from .helpers import AAC, H264, VP9
from .test_merge_ffmpeg import ENCODERS, TOOLS, make_clip
from .test_wmv import FAKE, make_wmv

WMV2 = Stream(0, "video", "wmv2", "", 320, 240, "yuv420p", "25/1")
WMA = Stream(1, "audio", "wmav2", "", sample_rate="44100", channels=1)
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
NODE = shutil.which("node")
PAGE_STAND_IN = Path(__file__).resolve().with_name("page_stand_in.js")


class Client:
    """A browser for the tests (standard library only): keeps the cookie the key gives."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie = ""

    def request(self, method: str, path: str, body=None, headers=None):
        sent = dict(headers or {})
        if self.cookie:
            sent.setdefault("Cookie", self.cookie)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, data, sent)
            answer = connection.getresponse()
            content = answer.read()
            cookie = answer.getheader("Set-Cookie")
            if cookie:
                self.cookie = cookie.split(";", 1)[0]
            return answer.status, answer, content
        finally:
            connection.close()


class PageTest(unittest.TestCase):
    """A server on a free port with its settings, log and programs in a temporary folder."""

    asfbin = False  # whether AsfBin is "found"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vm web ")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.patch(config, "config_dir", return_value=self.dir / "config")
        self.patch(logs, "log_dir", return_value=self.dir / "logs")
        self.patch(tools, "find_tool", side_effect=self.find_tool)
        self.patch(tools, "version", return_value="ffmpeg version 7.1-essentials_build Copyright (c) 2000-2024")
        self.patch(tools, "file_version", return_value="1.8.3")
        self.settings = config.Settings()
        self.start_server()

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def find_tool(self, name, override=None):
        if override:
            raise tools.ToolError(f"{name} was not found at {override}.")
        if name == "asfbin" and not self.asfbin:
            raise tools.ToolError("asfbin was not found.")
        return self.dir / f"{name}.exe"

    def start_server(self):
        self.server = web.make_server(self.settings, port=0)
        self.app = self.server.app
        self.app.startup()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.client = Client(self.server.port)

    def stop_server(self):
        self.app.shutdown(wait=10)
        self.server.shutdown()
        self.server.server_close()

    def sign_in(self):
        status, answer, _ = self.client.request("GET", f"/?key={self.server.token}")
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly; SameSite=Strict", answer.getheader("Set-Cookie"))

    def post(self, path, body=None, expect=200):
        status, _, content = self.client.request("POST", path, body or {}, {web.HEADER: "1"})
        self.assertEqual(status, expect, content)
        return json.loads(content)

    def get(self, path, expect=200):
        status, _, content = self.client.request("GET", path)
        self.assertEqual(status, expect, content)
        return json.loads(content)

    def state(self):
        return self.get("/api/state")

    def wait_for(self, check, seconds=30):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            state = self.state()
            if check(state):
                return state
            time.sleep(0.05)
        self.fail(f"timed out; last state: {json.dumps(state)[:2000]}")

    def look(self):
        self.post("/api/look")
        return self.wait_for(lambda s: s["scanned"] and not s["scan"]["running"])

    def rows(self, state):
        return {row["name"]: row for row in state["rows"]}


class WebTest(PageTest):
    def setUp(self):
        super().setUp()
        self.probed = []
        self.merges = []
        self.hold = threading.Event()  # set: the stand-in merge waits for self.release
        self.release = threading.Event()
        self.patch(web, "probe", side_effect=self.fake_probe)
        self.patch(web, "merge", side_effect=self.fake_merge)
        self.videos = self.dir / "videos"
        self.specs = {}
        self.containers = {}  # file name -> what fake_probe "finds" inside ("" for every other file)
        self.file("DMM/abc00123hhb1.mp4", "abc00123", 1)
        self.file("DMM/abc00123hhb2.mp4", "abc00123", 2)
        self.file("DMM/ghi00789hhb1.mp4", "ghi00789", 1)
        self.file("DMM/ghi00789hhb3.mp4", "ghi00789", 3)
        self.file("DMM/jkl000122d_v1_drm_a_4k1.mkv", "jkl00012", 1, (VP9, AAC))
        self.file("DMM/jkl000122d_v1_drm_a_4k2.mkv", "jkl00012", 2, (VP9, AAC))
        self.file("DMM/vwx00234.mp4", "vwx00234")
        self.file("DMM/sub/def00456hhb1.mp4", "def00456", 1)
        self.file("DMM/sub/def00456hhb2.mp4", "def00456", 2)
        self.file("DMM/old.avi")
        self.file("DMM/broken.mkv", probe=False)
        self.file("DMM/stu00901_1.wmv", streams=(WMV2, WMA))
        self.file("DMM/stu00901_2.wmv", streams=(WMV2, WMA))
        self.dmm = self.videos / "DMM"
        self.sign_in()

    def file(self, name, file_id=None, part=None, streams=(H264, AAC), probe=True):
        path = self.videos / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 1000)
        if probe:
            self.specs[path.name] = (file_id, part, tuple(streams))
        return path

    def fake_probe(self, path, ffprobe):
        self.probed.append(path.name)
        if path.name not in self.specs:
            raise ProbeError("Invalid data found when processing input")
        file_id, part, streams = self.specs[path.name]
        return MediaFile(path, path.stat().st_size, 100.0, streams, file_id, part, container=self.containers.get(path.name, ""))

    def fake_merge(self, item, tool_paths, delete_parts=False, on_progress=None, on_step=None, **kwargs):
        self.merges.append((item.group.name, item.target, delete_parts))
        on_progress(0.5)
        if self.hold.is_set():
            self.release.wait(20)
        on_step(merging.CHECKING)
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.target.write_bytes(b"merged")
        deleted = []
        if delete_parts:
            for part in item.group.parts:
                part.path.unlink()
                deleted.append(part.path)
        return MergeResult(item, True, f"merged {len(item.group.parts)} parts", output=item.target, deleted=deleted,
                           duration=item.group.duration, size=6)

    def test_only_the_pages_browser_gets_in(self):
        other = Client(self.server.port)
        self.assertEqual(other.request("GET", "/api/state")[0], 403)
        self.assertEqual(other.request("GET", "/?key=wrong")[0], 403)
        self.assertEqual(other.request("POST", "/api/look", {}, {web.HEADER: "1"})[0], 403)  # no cookie
        self.assertEqual(self.client.request("GET", "/api/state")[0], 200)
        self.assertEqual(self.client.request("POST", "/api/look", {})[0], 403)  # no header of ours
        self.assertEqual(self.client.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.client.request("POST", "/api/look", {}, {web.HEADER: "1", "Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.client.request("POST", "/api/look", None, {web.HEADER: "1", "Content-Type": "text/plain"})[0], 415)
        status, _, page = self.client.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Video <b>Merger</b>", page)
        status, answer, _ = self.client.request("GET", "/app.js")
        self.assertIn("default-src 'none'", answer.getheader("Content-Security-Policy"))

    def test_places_are_added_and_removed(self):
        self.post("/api/places/add", {"path": f'"{self.dmm}"'})  # "Copy as path" puts quotes around it
        self.assertEqual(self.state()["places"], [{"path": str(self.dmm), "kind": "folder"}])
        self.assertIn("Already in the list", self.post("/api/places/add", {"path": str(self.dmm)}, expect=409)["error"])
        self.assertIn("full path", self.post("/api/places/add", {"path": "DMM"}, expect=409)["error"])
        self.assertIn("Not found", self.post("/api/places/add", {"path": str(self.videos / "nowhere")}, expect=409)["error"])
        (self.videos / "list.txt").write_text("x")
        self.assertIn("Only folders", self.post("/api/places/add", {"path": str(self.videos / "list.txt")}, expect=409)["error"])
        part = self.dmm / "stu00901_1.wmv"
        self.post("/api/places/add", {"path": str(part)})
        self.assertEqual(self.state()["places"][1], {"path": str(part), "kind": "file"})
        self.post("/api/places/remove", {"path": str(self.dmm)})
        self.assertEqual([p["path"] for p in self.state()["places"]], [str(part)])
        self.post("/api/places/remove", {"path": str(self.dmm)}, expect=409)

    def test_parts_in_different_containers_cant_be_merged(self):
        self.containers.update({"def00456hhb1.mp4": "Matroska", "def00456hhb2.mp4": "MP4"})  # part 1: an MKV named .mp4
        self.post("/api/places/add", {"path": str(self.dmm / "sub")})
        row = self.rows(self.look())["def00456"]
        self.assertEqual((row["state"], row["why"]), ("problem", "Part 2 is MP4 and part 1 is Matroska: remux them to the same kind first."))
        self.assertEqual(row["fix"], "What is inside counts, not the extension. A remux (e.g. MKVToolNix, or ffmpeg -c copy) "
                                     "changes it without re-encoding; then “Look again”.")

    def test_a_look_finds_the_videos_in_parts(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        state = self.look()
        rows = self.rows(state)
        self.assertEqual(sorted(rows), ["abc00123", "def00456", "ghi00789", "jkl00012"])  # def00456: a subfolder
        abc = rows["abc00123"]
        self.assertEqual((abc["state"], abc["by"], abc["wmv"]), ("ready", "dmm", False))
        self.assertEqual(abc["chips"], ["H.264", "1920×1080", "29.97 fps", "AAC"])
        self.assertEqual(abc["target"], str(self.dmm / "abc00123.mkv"))
        self.assertEqual(abc["chapters"], ["Part 1", "Part 2"])
        self.assertEqual([p["number"] for p in abc["parts"]], [1, 2])
        ghi = rows["ghi00789"]
        self.assertEqual((ghi["state"], ghi["why"]), ("problem", "Missing part(s) 2."))
        self.assertIn("Download the missing part", ghi["fix"])
        jkl = rows["jkl00012"]
        self.assertEqual((jkl["state"], jkl["label"]), ("skip", "Stays in parts"))
        others = state["others"]
        self.assertEqual(others["whole"], [str(self.dmm / "vwx00234.mp4")])
        self.assertEqual(sorted(Path(p).name for p in others["ignored"]), ["old.avi", "stu00901_1.wmv", "stu00901_2.wmv"])
        self.assertFalse(others["wmv"])  # no AsfBin: the page says where to get it
        self.assertEqual([Path(u["path"]).name for u in others["unreadable"]], ["broken.mkv"])
        self.assertIn("found 4 video(s) in parts: 2 to merge", [line[2] for line in state["log"]])

    def test_options_plan_again_without_reading_again(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        self.look()
        read = len(self.probed)
        self.post("/api/options", {"container": "mp4"})
        self.assertEqual(self.rows(self.state())["abc00123"]["target"], str(self.dmm / "abc00123.mp4"))
        out = self.dir / "merged"
        self.post("/api/options", {"save_to": "folder", "out_dir": str(out)})
        self.assertEqual(self.rows(self.state())["abc00123"]["target"], str(out / "abc00123.mp4"))
        out.mkdir()
        (out / "abc00123.mp4").write_bytes(b"x")
        self.specs["abc00123.mp4"] = ("abc00123", None, (H264, AAC))  # this video, merged earlier (its DMM tag)
        self.post("/api/options", {})  # (anything) plans again: the file is there now
        row = self.rows(self.state())["abc00123"]
        self.assertEqual((row["state"], row["why"]), ("already", "abc00123.mp4 is already there."))
        self.post("/api/options", {"merge_again": True})
        self.assertEqual(self.rows(self.state())["abc00123"]["state"], "again")
        self.assertEqual(self.probed[read:], ["abc00123.mp4"])  # no part was read again, the merged file once

        self.assertTrue(self.post("/api/options", {"subfolders": False})["look_again"])  # only a new look uses it
        self.assertIn("def00456", self.rows(self.state()))
        self.post("/api/options", {"container": "avi"}, expect=409)
        self.post("/api/options", {"out_dir": "relative"}, expect=409)
        saved = config.load()
        self.assertEqual((saved.container, saved.save_to, saved.out_dir, saved.merge_again, saved.subfolders),
                         ("mp4", "folder", str(out), True, False))
        self.assertEqual(self.app.settings.container, "mp4")
        self.assertEqual(len(self.rows(self.look())), 3)  # no subfolders now

    def test_another_video_under_the_outputs_name_is_never_skipped_nor_replaced(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        self.file("DMM/abc00123.mkv", "xyz00001")  # another video's merge, named like this one's
        self.file("DMM/sub/def00456.mkv")  # no DMM tag
        self.post("/api/options", {"merge_again": True})
        rows = self.rows(self.look())
        for name, folder, tag in (("abc00123", self.dmm, "its DMM tag says xyz00001"), ("def00456", self.dmm / "sub", "it has no DMM tag")):
            self.assertEqual(rows[name]["state"], "problem", name)
            self.assertEqual(rows[name]["why"], f"{folder / name}.mkv is already there, but it is another video ({tag}).")
            self.assertEqual(rows[name]["fix"], "Move or rename that file (or save the merged videos in another folder), then “Look again”.")
        self.assertEqual(self.post("/api/start", expect=409)["error"], "No video is ready to merge.")
        self.assertEqual((self.dmm / "abc00123.mkv").read_bytes(), b"\0" * 1000)

    def test_a_new_look_reads_the_file_under_the_outputs_name_again(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        target = self.file("DMM/abc00123.mkv", probe=False)  # can't be read (yet)
        self.assertEqual(self.rows(self.look())["abc00123"]["state"], "problem")
        self.specs[target.name] = ("abc00123", None, (H264, AAC))  # the same file, readable now
        self.post("/api/options", {})
        self.assertEqual(self.rows(self.state())["abc00123"]["state"], "problem")  # options: not read again
        self.assertEqual(self.rows(self.look())["abc00123"]["state"], "already")

    def test_a_file_that_appears_after_the_look_is_not_replaced(self):
        self.patch(web, "merge", side_effect=merging.merge)  # the real one; the join never starts
        self.post("/api/places/add", {"path": str(self.dmm / "abc00123hhb1.mp4")})
        self.post("/api/places/add", {"path": str(self.dmm / "abc00123hhb2.mp4")})
        self.look()
        (self.dmm / "abc00123.mkv").write_bytes(b"made meanwhile")
        self.post("/api/start")
        row = self.wait_for(lambda s: not s["running"])["rows"][0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["why"], f"{self.dmm / 'abc00123.mkv'} is there now (it wasn't when the parts were checked), "
                                     "so it is not replaced.")
        self.assertEqual((self.dmm / "abc00123.mkv").read_bytes(), b"made meanwhile")

    def test_wmv_parts_only_with_asfbin(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        self.post("/api/options", {"container": "mp4"})
        self.assertNotIn("stu00901", self.rows(self.look()))
        self.asfbin = True
        self.post("/api/doctor")  # "Check setup again"
        state = self.state()
        self.assertEqual((state["tools"]["asfbin"], state["tools"]["versions"]["asfbin"]), (str(self.dir / "asfbin.exe"), "1.8.3"))
        state = self.look()
        row = self.rows(state)["stu00901"]
        self.assertEqual((row["state"], row["wmv"], row["by"]), ("ready", True, "name"))
        self.assertEqual(row["target"], str(self.dmm / "stu00901.wmv"))  # .wmv whatever MP4/MKV is chosen
        self.assertEqual(row["chips"], ["WMV2", "320×240", "25 fps", "WMA"])
        self.assertTrue(state["others"]["wmv"])
        self.assertEqual([Path(p).name for p in state["others"]["ignored"]], ["old.avi"])

    def test_merge_with_progress_and_stop_after_this_one(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        self.look()
        self.hold.set()
        self.assertEqual(self.post("/api/start")["started"], 2)
        state = self.wait_for(lambda s: any(r["state"] == "running" for r in s["rows"]))
        running = next(r for r in state["rows"] if r["state"] == "running")
        self.assertEqual(running["steps"], ["joining 2 parts (copied, not re-encoded)"])
        self.wait_for(lambda s: any(r["progress"] == 0.5 for r in s["rows"]))
        self.post("/api/options", {"container": "mp4"}, expect=409)  # not while merging
        self.post("/api/places/remove", {"path": str(self.dmm)}, expect=409)
        self.post("/api/look", expect=409)
        self.post("/api/start", expect=409)
        self.post("/api/stop")
        self.assertTrue(self.state()["stopping"])
        self.release.set()
        state = self.wait_for(lambda s: not s["running"])
        rows = self.rows(state)
        done = rows[running["name"]]
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["steps"], ["joining 2 parts (copied, not re-encoded)", "checking the result: length, size, every track",
                                         f"saved as {running['name']}.mkv"])
        self.assertEqual((done["saved_as"], done["deleted"]), (done["target"], 0))
        other = rows["def00456" if running["name"] == "abc00123" else "abc00123"]
        self.assertEqual(other["state"], "ready")  # not started: ready again
        self.assertEqual(len(self.merges), 1)

        self.hold.clear()
        self.post("/api/options", {"delete_parts": True})
        self.post("/api/start")
        state = self.wait_for(lambda s: not s["running"])
        second = self.rows(state)[other["name"]]
        self.assertEqual((second["state"], second["deleted"], second["steps"][-1]), ("done", 2, "deleted the 2 parts"))
        self.assertEqual(self.merges[-1][2], True)
        self.assertEqual(self.rows(state)[running["name"]]["state"], "done")  # results stay on the rows

    def test_a_video_can_be_left_out_and_put_back(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        state = self.look()
        self.post("/api/leave-out", {"id": self.rows(state)["abc00123"]["id"]})
        state = self.state()
        self.assertEqual(self.rows(state)["abc00123"]["state"], "left_out")
        self.post("/api/leave-out", {"id": self.rows(state)["ghi00789"]["id"]}, expect=409)  # can't be merged anyway
        self.post("/api/leave-out", {"id": 999}, expect=409)
        state = self.look()  # kept after a new look (the rows get new numbers)
        self.assertEqual(self.rows(state)["abc00123"]["state"], "left_out")
        self.assertIn("found 4 video(s) in parts: 1 to merge (1 left out)", [line[2] for line in state["log"]])
        self.assertEqual(self.post("/api/start")["started"], 1)
        state = self.wait_for(lambda s: not s["running"])
        self.assertEqual([m[0] for m in self.merges], ["def00456"])
        self.assertEqual(self.rows(state)["abc00123"]["state"], "left_out")
        self.assertFalse((self.dmm / "abc00123.mkv").exists())
        abc = self.rows(state)["abc00123"]["id"]
        self.post("/api/leave-out", {"id": abc, "out": False})
        self.assertEqual(self.rows(self.state())["abc00123"]["state"], "ready")
        self.post("/api/leave-out", {"id": abc, "out": False}, expect=409)  # not left out

    def test_leaving_out_a_waiting_video_during_a_merge(self):
        self.post("/api/places/add", {"path": str(self.dmm)})
        self.look()
        self.hold.set()
        self.assertEqual(self.post("/api/start")["started"], 2)
        state = self.wait_for(lambda s: any(r["state"] == "running" for r in s["rows"]))
        running = next(r for r in state["rows"] if r["state"] == "running")
        waiting = next(r for r in state["rows"] if r["state"] == "waiting")
        self.post("/api/leave-out", {"id": running["id"]}, expect=409)  # being merged
        self.post("/api/leave-out", {"id": waiting["id"]})
        self.assertEqual(self.rows(self.state())[waiting["name"]]["state"], "left_out")
        self.post("/api/leave-out", {"id": waiting["id"], "out": False})  # back in the queue
        self.assertEqual(self.rows(self.state())[waiting["name"]]["state"], "waiting")
        self.post("/api/leave-out", {"id": waiting["id"]})
        self.release.set()
        state = self.wait_for(lambda s: not s["running"])
        self.assertEqual([m[0] for m in self.merges], [running["name"]])
        self.assertEqual(self.rows(state)[waiting["name"]]["state"], "left_out")

    def test_a_failed_merge_stays_on_its_row(self):
        message = "ffmpeg failed (exit code 1):\n[mp4 @ 0x1] Could not write header"
        with mock.patch.object(web, "merge", side_effect=lambda item, *a, **k: MergeResult(item, False, message)):
            self.post("/api/places/add", {"path": str(self.dmm / "abc00123hhb1.mp4")})
            self.post("/api/places/add", {"path": str(self.dmm / "abc00123hhb2.mp4")})
            self.look()
            self.post("/api/start")
            row = self.wait_for(lambda s: not s["running"])["rows"][0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["why"], "ffmpeg failed (exit code 1).")
        self.assertEqual(row["fix"], "The parts were not changed. Details are in the Log tab.")
        self.assertTrue(any("Could not write header" in line[2] for line in self.state()["log"]))
        self.post("/api/options", {"container": "mp4"})
        self.assertEqual(self.state()["rows"][0]["state"], "failed")  # until the next look

    def test_programs_and_their_overrides(self):
        state = self.state()
        self.assertEqual(state["tools"]["versions"]["ffmpeg"], "7.1")
        self.assertEqual(state["tools"]["asfbin"], "")
        self.post("/api/settings", {"asfbin": f'"{self.dir / "nowhere"}"'})
        state = self.state()
        self.assertEqual(state["settings"]["asfbin"], str(self.dir / "nowhere"))
        self.assertEqual(state["tools"]["errors"]["asfbin"], f"not found at {self.dir / 'nowhere'}")
        self.assertEqual(config.load().asfbin, str(self.dir / "nowhere"))
        self.post("/api/settings", {"ffmpeg": str(self.dir / "nowhere")})
        self.post("/api/places/add", {"path": str(self.dmm)})
        self.assertIn("FFmpeg", self.post("/api/look", expect=409)["error"])
        self.post("/api/settings", {"ffmpeg": "", "asfbin": ""})
        self.assertEqual(self.state()["tools"]["errors"], {"asfbin": "not found"})

    def test_browse_lists_folders_and_video_files(self):
        (self.dmm / "abc00123.partial.mkv").write_bytes(b"")
        listing = self.get(f"/api/browse?path={quote(str(self.dmm))}&files=video")
        names = [(e["name"], e["dir"]) for e in listing["entries"]]
        self.assertEqual(names[0], ("sub", True))  # folders first
        self.assertIn(("abc00123hhb1.mp4", False), names)
        self.assertIn(("stu00901_1.wmv", False), names)
        self.assertNotIn(("old.avi", False), names)
        self.assertNotIn(("abc00123.partial.mkv", False), names)
        self.assertEqual(listing["parent"], str(self.videos))
        self.assertEqual([e["name"] for e in self.get(f"/api/browse?path={quote(str(self.dmm))}")["entries"]], ["sub"])
        self.assertEqual(self.get(f"/api/browse?path={quote(str(self.dmm / 'gone' / 'deeper'))}")["path"], str(self.dmm))  # nearest folder
        self.assertEqual(self.get(f"/api/browse?path={quote(str(self.dmm / 'old.avi'))}")["path"], str(self.dmm))

    def test_quit_stops_the_server(self):
        self.post("/api/quit")
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.app.quit.is_set())

    def test_quit_during_a_merge_ends_the_program_and_removes_the_unfinished_file(self):
        started = threading.Event()
        seen = {}

        def slow_merge(item, tool_paths, **kwargs):
            partial = merging.partial_path(item.target)
            partial.write_bytes(b"unfinished")  # as ffmpeg writes it
            program = tools.Popen(SLEEPER, creationflags=tools.NO_WINDOW)
            seen["program"] = program
            started.set()
            program.wait()  # ended by Quit
            return MergeResult(item, False, "ffmpeg failed (exit code 1)")  # without removing its file: Quit does

        with mock.patch.object(web, "merge", side_effect=slow_merge):
            self.post("/api/places/add", {"path": str(self.dmm)})
            self.look()
            self.post("/api/start")
            self.assertTrue(started.wait(20))
            self.post("/api/quit")
            self.thread.join(20)
        self.assertFalse(self.thread.is_alive())
        self.assertIsNotNone(seen["program"].poll())  # the program was ended
        self.assertEqual(list(self.dmm.glob("*.partial.*")), [])
        self.assertTrue((self.dmm / "abc00123hhb1.mp4").exists())

    @unittest.skipUnless(os.name == "nt", "Windows console")
    def test_closing_the_window_stops_like_quit(self):
        import ctypes

        web.stop_on_close(self.app, self.server)
        handler = web._close_handlers[-1]
        self.addCleanup(ctypes.windll.kernel32.SetConsoleCtrlHandler, handler, False)
        self.assertFalse(handler(0))  # Ctrl+C: Python's own handler (KeyboardInterrupt)
        self.assertTrue(handler(2))  # the window was closed
        self.assertTrue(self.app.quit.is_set())
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())


@unittest.skipUnless(NODE, "Node.js not installed")
class PageScriptTest(PageTest):
    """The page's own script (app.js), run by Node.js in a stand-in for the browser (tests/page_stand_in.js)."""

    def test_the_log_shows_each_line_once(self):
        # Two answers asked for at once (a poll and an action's refresh) carry the same new lines.
        self.app.note("first line")
        self.app.note("second line")
        state = self.dir / "state.json"
        state.write_text(json.dumps(self.app.state()), encoding="utf-8")
        result = subprocess.run([NODE, str(PAGE_STAND_IN), str(web.web_dir() / "app.js"), str(state)],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = json.loads(result.stdout)
        self.assertEqual(shown["requests"], 2)
        self.assertEqual((shown["log"].count("first line"), shown["log"].count("second line")), (1, 1), shown["log"])
        self.assertLess(shown["log"].index("first line"), shown["log"].index("second line"))


class StartTest(PageTest):
    def test_places_given_at_the_start(self):
        folder = self.dir / "given"
        folder.mkdir()
        app = web.App(self.settings, [str(folder), str(self.dir / "missing"), str(self.dir)])
        self.assertEqual([p["path"] for p in app.places], [str(folder), str(self.dir)])
        self.assertTrue(any("not added" in line[2] and "missing" in line[2] for line in app.lines))

    def test_saved_options_are_used(self):
        config.save(config.Settings(save_to="folder", out_dir=str(self.dir / "out"), container="mp4", subfolders=False))
        loaded = config.load()
        self.assertEqual((loaded.save_to, loaded.container, loaded.subfolders), ("folder", "mp4", False))
        text = config.settings_path().read_text(encoding="utf-8")
        self.assertIn("# Save merged videos", text)  # the comments of the template stay
        self.assertIn("container = mp4", text)
        self.assertEqual(web.App(loaded).state()["options"]["out_dir"], str(self.dir / "out"))
        self.assertEqual(web.App(config.Settings()).state()["options"]["out_dir"], str(config.default_out_dir()))


class CliStartTest(unittest.TestCase):
    """Double-click and drag and drop open the page; a terminal keeps merging there."""

    def run_main(self, argv, launcher=False, env=None):
        environ = {name: value for name, value in os.environ.items() if name != cli.LAUNCHER_ENV}
        with mock.patch.object(cli, "cmd_gui", return_value=0) as gui, mock.patch.object(cli, "cmd_merge", return_value=0) as merge, \
                mock.patch.object(cli, "owns_console", return_value=False), mock.patch.dict(os.environ, {**environ, **(env or {})}, clear=True):
            self.assertEqual(cli.main(argv, launcher=launcher), 0)
            self.assertNotIn(cli.LAUNCHER_ENV, os.environ)  # not passed on to ffmpeg & co.
        if gui.called:
            return "gui", gui.call_args[0][0]
        return "merge", merge.call_args[0][0]

    def test_no_arguments_open_the_page(self):
        command, args = self.run_main([])
        self.assertEqual((command, args.paths), ("gui", []))

    def test_paths_dropped_on_the_program_open_the_page(self):
        command, args = self.run_main([r"D:\Downloads\DMM", r"D:\x.wmv"], launcher=True)
        self.assertEqual((command, args.paths), ("gui", [r"D:\Downloads\DMM", r"D:\x.wmv"]))
        command, args = self.run_main([r"D:\Downloads\DMM"], env={cli.LAUNCHER_ENV: "1"})  # run.bat
        self.assertEqual(command, "gui")

    def test_a_terminal_merges_there(self):
        command, args = self.run_main([r"D:\Downloads\DMM", "--dry-run"])
        self.assertEqual((command, args.paths, args.dry_run), ("merge", [Path(r"D:\Downloads\DMM")], True))
        command, _ = self.run_main([r"D:\Downloads\DMM"])
        self.assertEqual(command, "merge")
        command, args = self.run_main([r"D:\Downloads\DMM", "-r"], launcher=True)  # options: the terminal
        self.assertEqual((command, args.recursive), ("merge", True))
        command, args = self.run_main(["gui", "--port", "9000", "--no-browser"])
        self.assertEqual((command, args.port, args.no_browser), ("gui", 9000, True))

    def test_the_page_is_served_from_the_command(self):
        with mock.patch("video_merger.web.serve", return_value=0) as serve, mock.patch.object(cli, "owns_console", return_value=False), \
                mock.patch.object(logs, "setup", return_value=None), mock.patch.object(config, "config_dir", return_value=Path("nowhere-test")):
            self.assertEqual(cli.main(["gui", "D:\\Videos", "--no-browser"]), 0)
        self.assertEqual(serve.call_args.kwargs["places"], ["D:\\Videos"])
        self.assertFalse(serve.call_args.kwargs["open_browser"])


@unittest.skipUnless(TOOLS and "wmv2" in ENCODERS and "wmav2" in ENCODERS, "ffmpeg with WMV2/WMAv2 encoders not installed")
class RealFilesTest(PageTest):
    """Clips made with FFmpeg, read and merged for real (AsfBin played by tests/fake_asfbin.py)."""

    asfbin = True

    def find_tool(self, name, override=None):
        return {"ffmpeg": TOOLS.ffmpeg, "ffprobe": TOOLS.ffprobe, "asfbin": FAKE}[name]

    def setUp(self):
        super().setUp()
        self.patch(merging, "asfbin_command", side_effect=lambda path: [sys.executable, str(path)])
        patcher = mock.patch.dict(os.environ, {"FAKE_ASFBIN_FFMPEG": str(TOOLS.ffmpeg)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.parts = self.dir / "it's here"
        self.parts.mkdir()
        self.sign_in()

    def test_look_and_merge_real_clips(self):
        for n in (1, 2):
            make_clip(self.parts / f"abc00123hhb{n}.mkv", comment={"file_id": "abc00123", "part": str(n)})
            make_wmv(self.parts / f"stu00901_{n}.wmv")
        self.post("/api/places/add", {"path": str(self.parts)})
        self.post("/api/options", {"container": "mp4"})
        rows = self.rows(self.look())
        self.assertEqual({name: row["state"] for name, row in rows.items()}, {"abc00123": "ready", "stu00901": "ready"})
        self.post("/api/start")
        state = self.wait_for(lambda s: not s["running"], seconds=60)
        rows = self.rows(state)
        for name, extension in (("abc00123", ".mp4"), ("stu00901", ".wmv")):
            row = rows[name]
            self.assertEqual(row["state"], "done", row)
            self.assertEqual(Path(row["saved_as"]).name, name + extension)
            self.assertAlmostEqual(row["merged_secs"], row["secs"], delta=0.5)
        self.assertEqual(sorted(p.name for p in self.parts.iterdir()),
                         ["abc00123.mp4", "abc00123hhb1.mkv", "abc00123hhb2.mkv", "stu00901.wmv", "stu00901_1.wmv", "stu00901_2.wmv"])

    def test_quit_during_a_wmv_merge(self):
        for n in (1, 2):
            make_wmv(self.parts / f"stu00901_{n}.wmv")
        self.post("/api/places/add", {"path": str(self.parts)})
        self.look()
        with mock.patch.dict(os.environ, {"FAKE_ASFBIN_MODE": "stall"}):
            self.post("/api/start")
            partial = self.parts / "stu00901.partial.wmv"
            end = time.monotonic() + 30
            while not partial.exists() and time.monotonic() < end:
                time.sleep(0.05)
            self.assertTrue(partial.exists())
            self.assertTrue((self.parts / "stu00901.partial.markers.txt").exists())
            self.post("/api/quit")
            self.thread.join(30)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(sorted(p.name for p in self.parts.iterdir()), ["stu00901_1.wmv", "stu00901_2.wmv"])


if __name__ == "__main__":
    unittest.main()
