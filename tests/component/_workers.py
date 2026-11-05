"""Functions run in separate processes by component tests (must be importable)."""

from __future__ import annotations

import os
import sys
from multiprocessing.synchronize import Barrier
from pathlib import Path

from tsw.home import open_home
from tsw.ledger import Ledger
from tsw.locks import FileLock
from tsw.tasks import note


def commit_notes(home_root: str, task_id: str, count: int, barrier: Barrier, tag: str) -> None:
    ledger = Ledger(open_home(Path(home_root)))
    barrier.wait()
    for index in range(count):
        ledger.commit(
            note(task_id, f"{tag}-{index}", actor={"role": "cli"}, command_id=f"{tag}-{index}")
        )


def hold_lock_then_die(path: str, ready: str) -> None:
    lock = FileLock(Path(path))
    lock.acquire()
    Path(ready).write_text("held", encoding="utf-8")
    sys.stdout.flush()
    os._exit(0)  # die without releasing; the OS must release the lock
