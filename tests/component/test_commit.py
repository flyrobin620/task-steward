from __future__ import annotations

import multiprocessing
import subprocess
import sys
from pathlib import Path

import pytest

from tests.component import _workers
from tests.support import TEST_ACTOR, child_env, register_task, spec
from tsw.errors import ErrorCode, TswError
from tsw.faults import CRASH_EXIT_CODE, FaultInjected
from tsw.home import Home
from tsw.ledger import Ledger
from tsw.locks import FileLock
from tsw.storage.fs import FsStorage
from tsw.tasks import note, register, task_row


@pytest.mark.use_case("UC-TASK-001")
@pytest.mark.transition("T01")
def test_register_writes_event_spec_and_index(home: Home, ledger: Ledger) -> None:
    task_id = register_task(ledger, title="导出 CSV")
    assert task_id == "T001"
    with ledger.view() as conn:
        row = task_row(conn, task_id)
    assert row["status"] == "intake"
    assert row["revision"] == 1
    spec_text = (home.root / "todo" / "T001.md").read_text(encoding="utf-8")
    assert spec_text.startswith("### T001：导出 CSV")
    index = (home.root / "todo" / "TODO.md").read_text(encoding="utf-8")
    assert "| [T001](T001.md) | 导出 CSV | demo | 代码 | 待受理 |" in index
    event = next(FsStorage(home).read())
    assert event.type == "task.registered"
    assert event.data["spec_path"] == "todo/T001.md"


@pytest.mark.use_case("UC-TASK-001")
@pytest.mark.transition("T01")
def test_register_assigns_increasing_ids_and_refuses_reused_ids(ledger: Ledger) -> None:
    assert register_task(ledger) == "T001"
    assert register_task(ledger, task_id="T010") == "T010"
    assert register_task(ledger) == "T011"
    with pytest.raises(TswError) as info:
        register_task(ledger, task_id="T010")
    assert info.value.details["reason"] == "task_exists"


@pytest.mark.use_case("UC-LEDGER-001")
def test_repeated_command_id_is_idempotent_and_conflicts_are_rejected(ledger: Ledger) -> None:
    first = ledger.commit(register(spec(), actor=TEST_ACTOR, command_id="c-1"))
    again = ledger.commit(register(spec(), actor=TEST_ACTOR, command_id="c-1"))
    assert again.duplicate
    assert [e.sequence for e in again.events] == [e.sequence for e in first.events]
    with pytest.raises(TswError) as info:
        ledger.commit(register(spec(title="另一个"), actor=TEST_ACTOR, command_id="c-1"))
    assert info.value.details["reason"] == "command_conflict"
    assert FsStorage(ledger.home).tail().sequence == 1


@pytest.mark.use_case("UC-LEDGER-001")
def test_idempotency_survives_a_projection_rebuild(ledger: Ledger) -> None:
    ledger.commit(register(spec(), actor=TEST_ACTOR, command_id="c-1"))
    ledger.rebuild()
    assert ledger.commit(register(spec(), actor=TEST_ACTOR, command_id="c-1")).duplicate
    with pytest.raises(TswError):
        ledger.commit(register(spec(title="变了"), actor=TEST_ACTOR, command_id="c-1"))


@pytest.mark.use_case("UC-TASK-002")
def test_note_checks_expected_version(ledger: Ledger) -> None:
    task_id = register_task(ledger)
    ledger.commit(note(task_id, "第一条", actor=TEST_ACTOR, expected_version=1))
    with pytest.raises(TswError) as info:
        ledger.commit(note(task_id, "过期", actor=TEST_ACTOR, expected_version=1))
    assert info.value.details == {"reason": "version_conflict", "expected": 1, "actual": 2}
    with ledger.view() as conn:
        assert [n["text"] for n in task_row(conn, task_id)["notes"]] == ["第一条"]


@pytest.mark.use_case("UC-TASK-002")
def test_note_on_unknown_task_is_rejected(ledger: Ledger) -> None:
    with pytest.raises(TswError) as info:
        ledger.commit(note("T404", "x", actor=TEST_ACTOR))
    assert info.value.details["reason"] == "task_not_found"


@pytest.mark.use_case("UC-LEDGER-001")
def test_failed_guard_appends_nothing(ledger: Ledger) -> None:
    with pytest.raises(TswError):
        ledger.commit(note("T404", "x", actor=TEST_ACTOR))
    assert FsStorage(ledger.home).tail().sequence == 0


@pytest.mark.use_case("UC-LEDGER-001")
def test_interruption_after_append_is_caught_up_by_the_next_reader(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = register_task(ledger)
    monkeypatch.setenv("TSW_TEST_FAULTS", "ledger.commit.after_append")
    with pytest.raises(FaultInjected):
        ledger.commit(note(task_id, "已提交但未归约", actor=TEST_ACTOR))
    monkeypatch.delenv("TSW_TEST_FAULTS")
    assert FsStorage(ledger.home).tail().sequence == 2
    with ledger.view() as conn:
        assert [n["text"] for n in task_row(conn, task_id)["notes"]] == ["已提交但未归约"]


_CRASH_SCRIPT = """
import sys
from pathlib import Path
from tsw.home import open_home
from tsw.ledger import Ledger
from tsw.tasks import note
Ledger(open_home(Path(sys.argv[1]))).commit(note(sys.argv[2], "crash", actor={"role": "cli"}))
"""


@pytest.mark.use_case("UC-LEDGER-001")
@pytest.mark.parametrize(
    ("point", "committed"),
    [("fs.append.before_replace", False), ("fs.append.after_replace", True)],
)
def test_crash_during_append_leaves_a_consistent_ledger(
    ledger: Ledger, point: str, committed: bool
) -> None:
    task_id = register_task(ledger)
    proc = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, str(ledger.home.root), task_id],
        env=child_env(TSW_TEST_FAULTS=f"{point}:exit"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == CRASH_EXIT_CODE
    assert FsStorage(ledger.home).tail().sequence == (2 if committed else 1)
    report = ledger.audit()
    assert report["ok"], report
    register_task(ledger)  # the next writer proceeds normally
    assert ledger.audit()["ok"]


@pytest.mark.use_case("UC-LEDGER-001")
def test_concurrent_writers_produce_one_contiguous_chain(ledger: Ledger) -> None:
    task_id = register_task(ledger)
    workers, per_worker = 4, 5
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(workers)
    procs = [
        ctx.Process(
            target=_workers.commit_notes,
            args=(str(ledger.home.root), task_id, per_worker, barrier, f"w{n}"),
        )
        for n in range(workers)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
        assert proc.exitcode == 0
    report = ledger.audit()
    assert report["ok"], report
    assert report["events"] == 1 + workers * per_worker
    with ledger.view() as conn:
        row = task_row(conn, task_id)
    assert len(row["notes"]) == workers * per_worker
    assert row["version"] == 1 + workers * per_worker


@pytest.mark.use_case("UC-LEDGER-001")
def test_lock_held_by_a_dead_process_is_released(home: Home, tmp_path: Path) -> None:
    path = home.locks_dir / "commit.lock"
    ready = tmp_path / "ready"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_workers.hold_lock_then_die, args=(str(path), str(ready)))
    proc.start()
    proc.join(timeout=60)
    assert ready.read_text(encoding="utf-8") == "held"
    with FileLock(path, timeout=5):
        pass


@pytest.mark.use_case("UC-LEDGER-001")
def test_shared_gate_blocks_exclusive_until_released(home: Home) -> None:
    path = home.locks_dir / "write-gate.lock"
    with FileLock(path, shared=True), FileLock(path, shared=True):
        with pytest.raises(TswError) as info:
            FileLock(path, shared=False, timeout=0.2).acquire()
        assert info.value.code is ErrorCode.UNAVAILABLE
    with FileLock(path, shared=False, timeout=1):
        pass
