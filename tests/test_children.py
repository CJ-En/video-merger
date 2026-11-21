"""Programs started by Video Merger end when it ends (video_merger/children.py)."""

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from video_merger import children
from video_merger import tools

ROOT = Path(__file__).resolve().parents[1]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def alive(pid: int) -> bool:
    """Whether a process with this id is still running (not ended, not a zombie)."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) != 0  # 0 = it has ended
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


def wait_until_gone(pid: int, seconds: float = 15) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


class ChildrenTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" or sys.platform.startswith("linux"), "Windows and Linux only")
    def test_a_program_ends_when_video_merger_is_killed(self):
        # A Video Merger stand-in starts a long program through children.Popen, then is killed like a closed window.
        script = ("import subprocess, sys, time\n"
                  "from video_merger import children\n"
                  f"child = children.Popen({SLEEPER!r}, stdout=subprocess.DEVNULL, creationflags={tools.NO_WINDOW})\n"
                  "print(child.pid, flush=True)\n"
                  "time.sleep(60)\n")
        parent = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                                  creationflags=tools.NO_WINDOW)
        try:
            child = int(parent.stdout.readline())
            self.assertTrue(alive(child))
        finally:
            parent.kill()
            parent.wait()
            parent.stdout.close()
        self.assertTrue(wait_until_gone(child), "the program outlived Video Merger")

    @unittest.skipUnless(os.name == "nt", "Windows job objects")
    def test_a_program_ends_when_its_job_is_closed(self):
        job = children.Job()
        process = subprocess.Popen(SLEEPER, creationflags=tools.NO_WINDOW)
        try:
            job.add(process)
            self.assertTrue(job.contains(process))
            self.assertIsNone(process.poll())
            job.close()
            process.wait(timeout=15)  # it would sleep 60 s (Windows ends job programs with exit code 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    @unittest.skipUnless(os.name == "nt", "Windows job objects")
    def test_every_program_goes_into_video_mergers_job(self):
        process = children.Popen(SLEEPER, creationflags=tools.NO_WINDOW)
        try:
            self.assertTrue(children.job().contains(process))
        finally:
            process.kill()
            process.wait()
        result = tools.run([sys.executable, "-c", "print('hello')"], timeout=30)  # short programs too
        self.assertEqual(result.stdout.strip(), b"hello")

    def test_stop_all_ends_the_running_programs(self):
        process = children.Popen(SLEEPER, creationflags=tools.NO_WINDOW)
        self.assertGreaterEqual(children.stop_all(), 1)
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
