"""Regressions for review round 1 of the first milestone (projection history, appends racing
outside the commit lock, recoverable derived files, consistent audits and reads)."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.support import TEST_ACTOR, child_env, register_task, spec
from tsw.errors import ErrorCode, TswError
from tsw.events import GENESIS, EventDraft, seal
from tsw.faults import CRASH_EXIT_CODE
from tsw.home import Home, init_home
from tsw.ledger import Ledger
from tsw.storage import fs as fs_module
from tsw.storage.fs import FsStorage
from tsw.tasks import register


@pytest.mark.use_case("UC-LEDGER-003")
def test_projection_from_another_history_with_the_same_length_is_refused(
    home: Home, ledger: Ledger, tmp_path: Path
) -> None:
    register_task(ledger, title="本目录")
    other_home, _ = init_home(tmp_path / "other")
    other = Ledger(other_home)
    register_task(other, title="另一个目录")
    ledger.projection.path.write_bytes(other.projection.path.read_bytes())
    with pytest.raises(TswError) as info, ledger.view():
        pass
    assert info.value.details["reason"] == "chain_mismatch"
    with pytest.raises(TswError):
        ledger.commit(register(spec(task_id="T050"), actor=TEST_ACTOR))
    assert not (home.root / "todo" / "T050.md").exists()  # refused before any guard or write


def _event(sequence: int, prev: str, tag: str) -> Any:
    return seal(
        EventDraft(type="task.note", data={"tag": tag}),
        sequence=sequence,
        prev=prev,
        time="2026-11-05T00:00:00Z",
        actor={"role": "cli"},
        command_id=None,
        command_digest=None,
    )


@pytest.mark.use_case("UC-STORE-002")
def test_direct_appends_racing_on_one_expected_sequence_never_both_succeed(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic: the first appender is held inside its replace until the second has
    reached the critical section (the append lock, or the replace itself without the lock)."""
    storage = FsStorage(home)
    second_reached = threading.Event()
    first_replacing = threading.Event()
    real_write = fs_module.atomic_write
    real_lock = fs_module.FileLock

    def gated_write(path: Path, data: bytes) -> None:
        if threading.current_thread().name == "first":
            first_replacing.set()  # A holds the append path and is about to replace
            assert second_reached.wait(10), "second appender never reached the critical section"
        else:
            second_reached.set()
        real_write(path, data)

    class SignallingLock(real_lock):
        def acquire(self) -> None:
            if self.path.name == "append.lock" and threading.current_thread().name == "second":
                second_reached.set()
            super().acquire()

    monkeypatch.setattr(fs_module, "atomic_write", gated_write)
    monkeypatch.setattr(fs_module, "FileLock", SignallingLock)
    outcomes: dict[str, object] = {}

    def append(tag: str) -> None:
        try:
            outcomes[tag] = storage.append([_event(1, GENESIS, tag)], expected_sequence=0)
        except TswError as exc:
            outcomes[tag] = exc

    first = threading.Thread(target=append, args=("a",), name="first")
    first.start()
    assert first_replacing.wait(10)
    second = threading.Thread(target=append, args=("b",), name="second")
    second.start()
    first.join(20)
    second.join(20)
    assert outcomes["a"] == 1
    error = outcomes["b"]
    assert isinstance(error, TswError)
    assert error.code is ErrorCode.REJECTED
    assert error.details["reason"] == "sequence_conflict"
    assert [e.data["tag"] for e in storage.read()] == ["a"]


@pytest.mark.use_case("UC-LEDGER-002")
def test_sealing_waits_for_a_reader_holding_the_segment_open(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows an open reader makes the seal's delete fail with a sharing violation; the
    writer must retry until the reader closes instead of failing the command."""
    ledger = Ledger(home, FsStorage(home, segment_max_bytes=600))
    register_task(ledger)
    active = FsStorage(home).segments()[-1].path
    conflict_seen = threading.Event()
    real_sleep = fs_module.time.sleep

    class ObservedTime:
        monotonic = staticmethod(fs_module.time.monotonic)

        @staticmethod
        def sleep(seconds: float) -> None:
            conflict_seen.set()  # only reached after a PermissionError from a real delete
            real_sleep(seconds)

    monkeypatch.setattr(fs_module, "time", ObservedTime)
    done: list[object] = []

    def write() -> None:
        try:
            done.append(register_task(ledger, title="触发封存" + "x" * 400))
        except Exception as exc:  # recorded for the assertion below
            done.append(exc)

    reader = active.open("rb")  # a concurrent reader (e.g. `tsw ledger tail`) keeps it open
    writer = threading.Thread(target=write)
    try:
        writer.start()
        if sys.platform == "win32":
            assert conflict_seen.wait(10), "the seal never hit the reader's sharing violation"
            assert writer.is_alive()
    finally:
        reader.close()
        writer.join(20)
    assert done == ["T002"]
    names = [seg.path.name for seg in FsStorage(home).segments()]
    assert names[0].endswith(".jsonl.gz")
    assert ledger.audit()["ok"]


_REGISTER_SCRIPT = """
import sys
from pathlib import Path
from tsw.home import open_home
from tsw.ledger import Ledger
from tsw.tasks import TaskSpec, register
spec = TaskSpec(title="中断的登记", repo="demo", kind="code", goal="g", acceptance=["a"])
Ledger(open_home(Path(sys.argv[1]))).commit(register(spec, actor={"role": "cli"}, command_id="c-9"))
"""


@pytest.mark.use_case("UC-TASK-001")
@pytest.mark.transition("T01")
def test_registration_interrupted_after_append_recovers_the_index_on_retry(
    home: Home, ledger: Ledger
) -> None:
    register_task(ledger, title="已有任务")
    proc = subprocess.run(
        [sys.executable, "-c", _REGISTER_SCRIPT, str(home.root)],
        env=child_env(TSW_TEST_FAULTS="ledger.commit.after_append:exit"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == CRASH_EXIT_CODE
    index = home.root / "todo" / "TODO.md"
    assert "中断的登记" not in index.read_text(encoding="utf-8")  # stale after the crash
    retry = ledger.commit(
        register(
            spec(title="中断的登记", repo="demo", kind="code", goal="g", acceptance=["a"]),
            actor=TEST_ACTOR,
            command_id="c-9",
        )
    )
    assert retry.duplicate
    assert [e.type for e in FsStorage(home).read()] == ["task.registered", "task.registered"]
    assert "| [T002](T002.md) | 中断的登记 |" in index.read_text(encoding="utf-8")


@pytest.mark.use_case("UC-LEDGER-003")
def test_rebuild_regenerates_derived_files(home: Home, ledger: Ledger) -> None:
    register_task(ledger, title="重建")
    (home.root / "todo" / "TODO.md").unlink()
    ledger.rebuild()
    assert "重建" in (home.root / "todo" / "TODO.md").read_text(encoding="utf-8")


@pytest.mark.use_case("UC-LEDGER-002")
def test_audit_reads_ledger_and_projection_under_the_commit_lock(home: Home) -> None:
    ledger = Ledger(home, FsStorage(home, lock_timeout=0.2))
    register_task(ledger)
    with ledger.storage.lock("commit", ttl=5):
        with pytest.raises(TswError) as info:
            ledger.audit()
        assert info.value.code is ErrorCode.UNAVAILABLE
    assert ledger.audit()["ok"]


@pytest.mark.use_case("UC-LEDGER-002")
def test_reads_retry_when_a_segment_disappears_mid_read(
    home: Home, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_task(ledger)
    register_task(ledger)
    real = FsStorage._segment_bytes
    failures = [1]

    def flaky(seg: Any) -> bytes:
        if failures:
            failures.pop()
            raise FileNotFoundError(seg.path)
        return real(seg)

    monkeypatch.setattr(FsStorage, "_segment_bytes", staticmethod(flaky))
    assert [e.sequence for e in FsStorage(home).read()] == [1, 2]
    failures.append(1)
    assert FsStorage(home).tail().sequence == 2
