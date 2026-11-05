"""Cross-platform advisory file locks with shared and exclusive modes.

Windows uses ``LockFileEx`` (through ctypes); POSIX uses ``flock``. The operating system releases
a lock when the holding process dies, so a crashed writer never leaves a stale lock behind.
Each :class:`FileLock` owns its own file handle; two handles conflict even inside one process.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import TracebackType

from tsw.errors import ErrorCode, TswError

_POLL_SECONDS = 0.02

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _UnlockFileEx.restype = wintypes.BOOL
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997

    def _try_lock(fd: int, shared: bool) -> bool:
        handle = msvcrt.get_osfhandle(fd)
        flags = _LOCKFILE_FAIL_IMMEDIATELY | (0 if shared else _LOCKFILE_EXCLUSIVE_LOCK)
        overlapped = _Overlapped()
        if _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            return True
        error = ctypes.get_last_error()
        if error in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            return False
        raise OSError(error, "LockFileEx failed")

    def _unlock(fd: int) -> None:
        handle = msvcrt.get_osfhandle(fd)
        overlapped = _Overlapped()
        if not _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise OSError(ctypes.get_last_error(), "UnlockFileEx failed")

else:
    import fcntl

    def _try_lock(fd: int, shared: bool) -> bool:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """A lock on ``path``. Use as a context manager or call :meth:`acquire` / :meth:`release`."""

    def __init__(self, path: Path, *, shared: bool = False, timeout: float = 30.0) -> None:
        self.path = path
        self.shared = shared
        self.timeout = timeout
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError(f"lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.timeout
        try:
            while not _try_lock(fd, self.shared):
                if time.monotonic() >= deadline:
                    raise TswError(
                        ErrorCode.UNAVAILABLE,
                        "等待锁超时",
                        lock=self.path.name,
                        timeout=self.timeout,
                    )
                time.sleep(_POLL_SECONDS)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
