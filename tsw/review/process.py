"""A child process whose whole tree can be ended, even after the child itself has exited.

On Windows the child is started suspended, put into a Job Object that kills its members when
the job is closed, and then resumed, so every descendant belongs to the job from the start; if
the job cannot be set up the child is ended before it ever runs (UNAVAILABLE). On
other platforms the child leads a new process group. :meth:`ProcessTree.end` ends whatever is
left of the tree; a direct child that has already exited does not mean its descendants have.
"""

# ctypes calls into kernel32 / ntdll are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from tsw.errors import ErrorCode, TswError

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # Handles are pointer-sized: declare every signature so they are never truncated.
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


class ProcessTree:
    def __init__(self, argv: Sequence[str], **popen: Any) -> None:
        self._job: Any = None
        if sys.platform == "win32":
            flags = int(popen.pop("creationflags", 0)) | _CREATE_SUSPENDED
            self.proc = subprocess.Popen(list(argv), creationflags=flags, **popen)
            # Popen's own process handle has full access; it stays valid while proc lives.
            handle = int(getattr(self.proc, "_handle"))  # noqa: B009
            self._job = _job_for(handle)
            if not self._job:  # no reliable ownership: end it before any of its code runs
                with contextlib.suppress(OSError):
                    self.proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=10)
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    with contextlib.suppress(OSError):
                        if stream:
                            stream.close()
                raise TswError(
                    ErrorCode.UNAVAILABLE,
                    "无法把评审器进程放入可整体结束的作业，未启动评审器",
                    reason="reviewer_unavailable",
                )
            _ntdll.NtResumeProcess(handle)
        else:
            self.proc = subprocess.Popen(list(argv), start_new_session=True, **popen)

    def end(self) -> None:
        """End every process still in the tree, then reap the direct child."""
        if sys.platform == "win32":
            if self._job:
                _kernel32.TerminateJobObject(self._job, 1)
                _kernel32.CloseHandle(self._job)
                self._job = None
        else:
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=10)


def _job_for(process: int) -> Any:
    """Put the (still suspended) process into a kill-on-close job; None if that is refused."""
    job = _kernel32.CreateJobObjectW(None, None)
    try:
        if not job:
            return None
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = _kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if configured and _kernel32.AssignProcessToJobObject(job, process):
            owned, job = job, None
            return owned
        return None
    finally:
        if job:
            _kernel32.CloseHandle(job)
