"""Importing an orchestrator-agent history into a data directory (DN005)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.import_fixture import PR, build
from tests.machine_harness import Harness
from tsw.errors import TswError
from tsw.home import Home
from tsw.importer import orchestrator_agent as importer
from tsw.importer.orchestrator_agent import run_import
from tsw.ledger import Ledger
from tsw.machine import guards
from tsw.tasks import TaskSpec, next_task_id, note, register

pytestmark = pytest.mark.use_case("UC-IMPORT-001")
LEGACY = "legacy/orchestrator-agent"


def rows(ledger: Ledger, sql: str, *params: Any) -> list[dict[str, Any]]:
    with ledger.view() as conn:
        return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params)]  # noqa: SIM118


def types(ledger: Ledger) -> list[str]:
    return [e.type for e in ledger.storage.read()]


@pytest.mark.transition("I01")
@pytest.mark.transition("I02")
@pytest.mark.transition("I03")
@pytest.mark.transition("I04")
@pytest.mark.transition("I05")
@pytest.mark.transition("I06")
def test_every_class_is_imported_with_its_history(home: Home, tmp_path: Path) -> None:
    source, snapshot = build(tmp_path)
    ledger = Ledger(home)
    summary = run_import(home, ledger, snapshot)
    assert summary["classes"] == {
        "archived": 2,
        "cancelled": 1,
        "accepted": 1,
        "done": 1,
        "intake": 1,
        "paused": 3,
    }
    assert summary["todo_only"] == ["T009"]
    assert types(ledger) == ["import.started", *["import.task_imported"] * 9, "import.completed"]
    tasks = {r["task_id"]: r for r in rows(ledger, "SELECT * FROM tasks")}
    assert tasks["T001"]["previous_status"] == "accepted"
    assert tasks["T003"]["accepted_at"] == "2026-10-01T00:00:00Z"
    assert tasks["T004"]["last_activity"] == "2026-10-04T12:00:00Z"  # business time kept
    assert tasks["T004"]["done_at"] == "2026-10-20T00:00:00Z"
    assert tasks["T007"]["attempt_count"] == 2  # a new attempt there is a3
    pauses = {t: json.loads(tasks[t]["pause"])["category"] for t in ("T006", "T007", "T008")}
    assert pauses == {"T006": "external", "T007": "import_handover", "T008": "failure"}
    assert json.loads(tasks["T006"]["pause"])["next_action"] == "等外部"
    assert json.loads(tasks["T004"]["acceptance"]) == ["第一条", "第二条"]
    outputs = rows(ledger, "SELECT output_id, ref FROM outputs WHERE task_id = 'T004'")
    assert {(o["output_id"], o["ref"]) for o in outputs} == {
        ("pr-1", PR),
        ("report", "runs/T004/a1/result.html"),
    }
    deps = rows(ledger, "SELECT dep_id FROM dependencies WHERE task_id = 'T005'")
    assert deps == [{"dep_id": "T004"}]
    # Files: volumes by original bytes, specs, results where they belong, the report.
    volumes = home.root / "ledger" / "imported" / "orchestrator-agent"
    assert (volumes / "events-1-2.jsonl.gz").read_bytes() == (
        source / "runtime" / "events-1-2.jsonl.gz"
    ).read_bytes()
    assert (volumes / "events-3-4.jsonl").read_bytes() == (
        source / "runtime" / "events.jsonl"
    ).read_bytes()
    assert json.loads((volumes / "manifest.json").read_text(encoding="utf-8"))["volumes"]
    assert (home.root / "todo" / "T004.md").is_file()
    assert (home.root / LEGACY / "runs" / "T001" / "a1" / "result.html").is_file()
    assert (home.root / "runs" / "T004" / "a1" / "result.html").is_file()
    assert (home.root / LEGACY / "metadata" / "task-keywords" / "records" / "k.json").is_file()
    assert (home.root / LEGACY / "config" / "repositories.json").is_file()
    report = (home.root / LEGACY / "import" / "report.md").read_text(encoding="utf-8")
    assert "T009" in report and "| T004 | done | done |" in report
    assert (
        (home.root / LEGACY / "import" / "report.html")
        .read_text(encoding="utf-8")
        .startswith("<!doctype html>")
    )
    with ledger.view() as conn:
        assert next_task_id(conn) == "T011"  # new tasks continue after the imported ones


def test_running_the_same_import_again_appends_nothing(home: Home, tmp_path: Path) -> None:
    _, snapshot = build(tmp_path)
    ledger = Ledger(home)
    run_import(home, ledger, snapshot)
    before = types(ledger)
    again = run_import(home, ledger, snapshot)
    assert (again["imported"], again["unchanged"]) == (0, 9)
    assert types(ledger) == before


def test_a_later_snapshot_updates_untouched_tasks_only(home: Home, tmp_path: Path) -> None:
    _, first = build(tmp_path / "one")
    ledger = Ledger(home)
    run_import(home, ledger, first)
    ledger.commit(note("T006", "新系统里的备注", actor={"role": "cli"}))
    _, second = build(tmp_path / "two", tail_events=3)  # a later export of the same source
    snapshot = json.loads((second / "snapshot.json").read_text(encoding="utf-8"))
    snapshot["source"]["root"] = str(tmp_path / "two" / "control")
    snapshot["tasks"]["T005"]["status"] = "ready"
    (second / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    summary = run_import(home, ledger, second)
    assert [c["task_id"] for c in summary["conflicts"]] == ["T006"]
    assert summary["imported"] == 8
    seq = rows(ledger, "SELECT source_sequence FROM imported_tasks WHERE task_id = 'T005'")
    assert seq == [{"source_sequence": 5}]


def test_a_task_of_the_new_system_with_the_same_id_is_not_overwritten(
    home: Home, tmp_path: Path
) -> None:
    Harness(home)  # a valid configuration with repository "demo"; holds the lease as "m1"
    ledger = Ledger(home, machine="m1")
    spec = TaskSpec(title="验收任务", repo="demo", kind="research", goal="g", acceptance=["a"])
    ledger.commit(register(spec, actor={"role": "cli"}))  # T001
    _, snapshot = build(tmp_path)
    summary = run_import(home, ledger, snapshot)
    assert summary["conflicts"] == [
        {"task_id": "T001", "reason": "数据目录中已有同 ID 的任务（非导入）"}
    ]
    assert rows(ledger, "SELECT title FROM tasks WHERE task_id = 'T001'") == [{"title": "验收任务"}]


def test_a_source_that_moved_after_the_export_is_refused(home: Home, tmp_path: Path) -> None:
    source, snapshot = build(tmp_path)
    with (source / "runtime" / "events.jsonl").open("ab") as stream:
        stream.write(b'{"sequence": 5}\n')
    dry = run_import(home, Ledger(home), snapshot, dry_run=True)
    assert dry["volume_problems"] and dry["tasks"] == 9
    with pytest.raises(TswError) as stale:
        run_import(home, Ledger(home), snapshot)
    assert stale.value.details["reason"] == "snapshot_stale"
    assert types(Ledger(home)) == []


def test_imported_tasks_continue_in_the_state_machine(home: Home, tmp_path: Path) -> None:
    h = Harness(home)
    _, snapshot = build(tmp_path)
    run_import(home, Ledger(home, machine="m1"), snapshot)
    # Waiting for the user: accept it directly (T22).
    h.run("user_accept", task_id="T004", user_source=h.source("T004", 1, "accept"))
    assert h.status("T004") == "accepted"
    # Paused, including a handover of a worker that was running: resume to ready (T21) and
    # dispatch a new attempt numbered after the old ones.
    for task_id in ("T006", "T007"):
        h.run("task_resume", task_id=task_id, condition_met=True, note="已核实")
        assert h.status(task_id) == "ready"
    dispatched = h.run("task_dispatch", task_id="T007", workdir="w7", user_turn=True)
    assert dispatched["events"][0]["data"]["attempt"] == "a3"
    # The new attempt's brief carries the resume package (DN005 §5.1).
    brief = (home.root / "runs" / "T007" / "a3" / "brief.md").read_text(encoding="utf-8")
    for expected in (
        "resume 资料包",
        "a2：执行目录 wt/t007；分支 codex/t007；base abc",
        "已有交付报告：runs/T007/a2/result.html",
        "最近反馈：补测试后再交",
        "T004（accepted）：报告 runs/T004/a1/result.html",
        "先对账",
    ):
        assert expected in brief
    # Read-only history stays read-only.
    for task_id in ("T001", "T003"):
        with pytest.raises(TswError):
            h.run("task_dispatch", task_id=task_id, workdir="w", user_turn=True)


def test_snapshot_paths_and_ids_cannot_leave_the_data_directory(home: Home, tmp_path: Path) -> None:
    _, snapshot_dir = build(tmp_path)
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    path = snapshot_dir / "snapshot.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["files"]["config"] = ["../outside.txt"]
    snapshot["tasks"]["../T999"] = snapshot["tasks"].pop("T008")
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(TswError) as unsafe:
        run_import(home, Ledger(home), snapshot_dir)
    assert unsafe.value.details["reason"] == "snapshot_path"
    snapshot["files"]["config"] = []
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    summary = run_import(home, Ledger(home), snapshot_dir)
    assert summary["invalid_task_ids"] == ["../T999"]
    assert not (home.root.parent / "T999").exists()


def test_resources_dependencies_requests_and_references_carry_over(
    home: Home, tmp_path: Path
) -> None:
    h = Harness(home)
    _, snapshot = build(tmp_path)
    run_import(home, Ledger(home, machine="m1"), snapshot)
    claims = rows(
        h.ledger, "SELECT resource, owner_task FROM resource_claims WHERE status = 'held'"
    )
    assert {(c["resource"], c["owner_task"]) for c in claims} == {
        ("res-kept", "T001"),
        ("res-paused", "T006"),
    }
    assert json.loads(h.task("T006")["resources"]) == ["res-paused"]
    bound = rows(h.ledger, "SELECT bound_revision FROM dependencies WHERE task_id = 'T006'")
    assert bound == [{"bound_revision": 1}]
    with h.ledger.view() as conn:
        assert guards.dependency_problems(conn, "T006") == []  # accepted T003 satisfies it
        # Archived without finishing (T010 was paused) does not satisfy a dependency.
        conn.execute(
            "INSERT INTO dependencies(task_id, dep_id, bound_revision, kind, reason) "
            "VALUES('T005', 'T010', 1, 'explicit', 'test')"
        )
        reasons = {p["dep_id"]: p["reason"] for p in guards.dependency_problems(conn, "T005")}
        conn.rollback()
    assert reasons["T010"] == "not_done"
    ref = rows(h.ledger, "SELECT ref FROM outputs WHERE task_id = 'T001'")[0]["ref"]
    assert ref == "legacy://orchestrator-agent/runs/T001/a1/result.html"
    assert (home.root / LEGACY / "runs" / "T001" / "a1" / "result.html").is_file()
    requests = rows(h.ledger, "SELECT request_id, kind, status FROM input_requests")
    assert requests == [
        {"request_id": "orchestrator-agent:req-1", "kind": "imported", "status": "pending"}
    ]
    source = h.source("T006", 1, "answer", "继续", request_id="orchestrator-agent:req-1")
    h.run(
        "input_resolve",
        task_id="T006",
        request_id="orchestrator-agent:req-1",
        answer="继续",
        user_source=source,
    )
    assert rows(h.ledger, "SELECT status FROM input_requests") == [{"status": "resolved"}]
    assert h.status("T006") == "paused"


def test_a_conflict_found_under_the_commit_lock_overwrites_nothing(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, first = build(tmp_path / "one")
    ledger = Ledger(home)
    run_import(home, ledger, first)
    _, second = build(tmp_path / "two", tail_events=3)
    spec = home.root / "todo" / "T006.md"
    before = spec.read_bytes()
    (tmp_path / "two" / "control" / "todo" / "T006.md").write_text("改过的任务书", encoding="utf-8")
    preparing = importer.prepare

    def prepare_then_race(plan: Any, source: Path) -> Any:
        files = preparing(plan, source)
        if plan.task_id == "T006":  # the new system writes to T006 after the import checked it
            ledger.commit(note("T006", "并发备注", actor={"role": "cli"}))
        return files

    monkeypatch.setattr(importer, "prepare", prepare_then_race)
    summary = run_import(home, ledger, second)
    assert {c["task_id"] for c in summary["conflicts"]} == {"T006"}
    assert spec.read_bytes() == before


def test_an_interrupted_import_is_completed_by_the_next_run(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, snapshot = build(tmp_path)
    ledger = Ledger(home)
    complete = importer._commit_completed

    def crash(*_: Any) -> None:
        raise RuntimeError("stopped after the last task")

    monkeypatch.setattr(importer, "_commit_completed", crash)
    with pytest.raises(RuntimeError):
        run_import(home, ledger, snapshot)
    assert "import.completed" not in types(ledger)
    monkeypatch.setattr(importer, "_commit_completed", complete)
    run_import(home, ledger, snapshot)
    assert types(ledger).count("import.completed") == 1
    before = types(ledger)
    run_import(home, ledger, snapshot)
    assert types(ledger) == before
