"""Programs Video Merger starts (ffmpeg, ffprobe, AsfBin) end when Video Merger ends.

Windows doesn't stop a program's children when the program stops, and programs started without a window don't get
the console window's "close" signal either. An ffmpeg or AsfBin left running would keep writing a merged file after
Video Merger was closed. So on Windows every child goes into one job object made the first time a program is
started, with "kill on job close": Video Merger keeps its handle open as long as it runs, and Windows closes the
handle when Video Merger ends in any way (window closed, Ctrl+C, a crash, Task Manager), which ends every program
in the job, also the ones they started themselves. On Linux each child asks the system for SIGTERM when Video
Merger ends (PR_SET_PDEATHSIG).

Every place that starts a program uses Popen from here (through tools.Popen).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import weakref

log = logging.getLogger(__name__)

WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # the information class SetInformationJobObject is given

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                                                         "OtherOperationCount", "ReadTransferCount",
                                                         "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class Job:
        """A Windows job object whose programs all end when its handle is closed (or Video Merger ends)."""

        def __init__(self) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)  # not inheritable: no child keeps the job alive
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                     ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)
                raise ctypes.WinError(error)
            self.handle = handle

        def add(self, process: subprocess.Popen) -> None:
            # Programs the child starts from now on are in the job too. (In the moment between its start and
            # this call it could start one outside the job; none of the programs used here starts that fast.)
            if not _kernel32.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())

        def contains(self, process: subprocess.Popen) -> bool:
            result = wintypes.BOOL()
            if not _kernel32.IsProcessInJob(int(process._handle), self.handle, ctypes.byref(result)):
                raise ctypes.WinError(ctypes.get_last_error())
            return bool(result.value)

        def kill_all(self) -> None:
            """End every program in the job (the job itself stays usable)."""
            _kernel32.TerminateJobObject(self.handle, 1)

        def close(self) -> None:
            """Ends every program in the job (kill on job close)."""
            if self.handle:
                _kernel32.CloseHandle(self.handle)
                self.handle = None

if LINUX:
    import ctypes

    PR_SET_PDEATHSIG = 1
    try:
        _prctl = ctypes.CDLL(None, use_errno=True).prctl  # looked up now: the child only calls it
    except (OSError, AttributeError):
        _prctl = None
else:
    _prctl = None

_lock = threading.Lock()
_job = None  # Video Merger's job (Windows), made when the first program starts
_job_failed = False
_running: "weakref.WeakSet[Popen]" = weakref.WeakSet()


def job():
    """Video Merger's job object (Windows), or None when Windows refused to make one."""
    global _job, _job_failed
    if not WINDOWS:
        return None
    with _lock:
        if _job is None and not _job_failed:
            try:
                _job = Job()  # kept open until Video Merger ends: Windows closes it then
            except OSError as exc:
                _job_failed = True
                log.warning("Windows didn't make a job object (%s): programs may outlive Video Merger", exc)
        return _job


def _die_with_parent(parent: int):
    """Runs in the child just before the program starts (Linux): SIGTERM when Video Merger ends.

    The system sends it when the thread that started the child ends; Video Merger's threads always wait for the
    programs they start, so in practice that is when Video Merger ends."""
    def preexec() -> None:
        _prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM))
        if os.getppid() != parent:  # Video Merger ended before the line above took effect
            os.kill(os.getpid(), signal.SIGTERM)
    return preexec


class Popen(subprocess.Popen):
    """subprocess.Popen whose program ends when Video Merger ends (see the module's text)."""

    def __init__(self, args, **kwargs) -> None:
        self.own_group = bool(kwargs.get("start_new_session")) or kwargs.get("process_group") == 0
        if _prctl is not None and kwargs.get("preexec_fn") is None:
            kwargs["preexec_fn"] = _die_with_parent(os.getpid())
        super().__init__(args, **kwargs)
        windows_job = job()
        if windows_job is not None:
            try:
                windows_job.add(self)
            except OSError as exc:
                log.warning("%s isn't tied to Video Merger (%s): it may outlive it", self.args, exc)
        with _lock:
            _running.add(self)


def stop_all() -> int:
    """End every program Video Merger started that is still running (when it quits). Returns how many."""
    with _lock:
        running = [p for p in _running if p.poll() is None]
        windows_job = _job
    if windows_job is not None:
        windows_job.kill_all()  # also the programs they started
    for process in running:
        try:
            if process.own_group and not WINDOWS:
                os.killpg(process.pid, signal.SIGTERM)  # the program and the ones it starts
            process.kill()
        except OSError:
            pass
    for process in running:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("%s didn't stop", process.args)
    if running:
        log.info("stopped %d program(s) still running", len(running))
    return len(running)
