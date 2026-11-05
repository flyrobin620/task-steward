"""Guards of DN002 §4 against an in-memory projection."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tsw.config import load_config
from tsw.errors import TswError
from tsw.home import Home
from tsw.machine import guards
from tsw.projection import SCHEMA_SQL


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


def _task(db: sqlite3.Connection, task_id: str, status: str, **extra: Any) -> None:
    values: dict[str, Any] = {
        "task_id": task_id,
        "title": "t",
        "repo": "demo",
        "kind": "code",
        "status": status,
        "revision": 1,
        "version": 1,
        "spec_path": "todo/x.md",
        "spec_sha256": "x",
        "registered_seq": 1,
        "last_activity": "t",
        "source_seq": 1,
        "goal": "g",
        "acceptance": '["a"]',
    }
    values.update(extra)
    cols = ", ".join(values)
    db.execute(
        f"INSERT INTO tasks({cols}) VALUES({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _reason(call: Any) -> str:
    with pytest.raises(TswError) as info:
        call()
    return str(info.value.details["reason"])


def _config(home: Home, **settings: Any) -> Any:
    base: dict[str, Any] = {"version": 1, "repositories": {"demo": {"aliases": ["d"]}}}
    base.update(settings)
    home.path("config/task-steward.json").write_text(json.dumps(base), encoding="utf-8")
    return load_config(home).require_valid()


@pytest.mark.transition("G-PLAN")
def test_g_plan(conn: sqlite3.Connection, home: Home) -> None:
    config = _config(home)
    _task(conn, "T001", "intake", repo="d")
    row = dict(conn.execute("SELECT * FROM tasks").fetchone())
    assert guards.g_plan(row, config) == "demo"
    assert _reason(lambda: guards.g_plan({**row, "repo": "nope"}, config)) == "g_plan"
    assert _reason(lambda: guards.g_plan({**row, "acceptance": "[]"}, config)) == "g_plan"
    assert _reason(lambda: guards.g_plan({**row, "kind": "ops"}, config)) == "g_plan"


@pytest.mark.transition("G-DEP")
@pytest.mark.parametrize(
    ("upstream", "reason"),
    [
        ({"status": "in_progress"}, "not_done"),
        ({"status": "cancelled"}, "cancelled"),
        ({"status": "archived", "previous_status": "cancelled"}, "cancelled"),
        ({"status": "done", "revision": 2}, "revision_changed"),
        ({"status": "done", "stale": 1}, "stale"),
        ({"status": "done"}, None),
        ({"status": "accepted"}, None),
        ({"status": "archived", "previous_status": "accepted"}, None),
    ],
)
def test_g_dep(conn: sqlite3.Connection, upstream: dict[str, Any], reason: str | None) -> None:
    _task(conn, "T001", **{"status": "done", **upstream})
    _task(conn, "T002", "ready")
    conn.execute("INSERT INTO dependencies VALUES('T002', 'T001', 1, 'explicit', 'r')")
    problems = guards.dependency_problems(conn, "T002")
    assert [p["reason"] for p in problems] == ([reason] if reason else [])


@pytest.mark.transition("G-SLOT")
def test_g_slot(conn: sqlite3.Connection, home: Home) -> None:
    config = _config(home, scheduling={"max_workers": 2})
    _task(conn, "T001", "in_progress")
    _task(conn, "T002", "paused")
    guards.g_slot(conn, config)
    _task(conn, "T003", "waiting_user")
    assert _reason(lambda: guards.g_slot(conn, config)) == "g_slot"


@pytest.mark.transition("G-RES")
def test_g_res(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO resource_claims VALUES('db', 'T001', 'held')")
    conn.execute("INSERT INTO resource_claims VALUES('cache', 'T001', 'released')")
    assert _reason(lambda: guards.g_res(conn, "T002", ["db"])) == "g_res"
    guards.g_res(conn, "T001", ["db"])  # the holder may continue to use its own claim
    guards.g_res(conn, "T002", ["cache"])


@pytest.mark.transition("G-WINDOW")
def test_g_window(home: Home) -> None:
    config = _config(home)
    assert _reason(lambda: guards.g_window(config, user_turn=False)) == "g_window"
    guards.g_window(config, user_turn=True)


@pytest.mark.transition("G-ONE-ATTEMPT")
@pytest.mark.parametrize("phase", ["standby", "starting", "live", "stopped"])
def test_g_one_attempt(conn: sqlite3.Connection, phase: str) -> None:
    conn.execute("INSERT INTO attempts(task_id, attempt, phase) VALUES('T001', 'a1', ?)", (phase,))
    assert _reason(lambda: guards.g_one_attempt(conn, "T001")) == "g_one_attempt"
    guards.g_one_attempt(conn, "T001", allowed="a1")


@pytest.mark.transition("G-PAUSE")
def test_g_pause() -> None:
    complete = {
        "category": "external",
        "owner": "用户",
        "next_action": "等",
        "resume_condition": "审批",
    }
    guards.g_pause(complete)
    assert _reason(lambda: guards.g_pause({**complete, "owner": " "})) == "g_pause"
    assert _reason(lambda: guards.g_pause({**complete, "category": "whatever"})) == "g_pause"


SOURCE = {
    "channel": "main-session",
    "principal": "owner",
    "message_ref": "m",
    "text": "好",
    "target": ["T001", 1, "accept"],
}


@pytest.mark.transition("G-USER")
@pytest.mark.parametrize(
    "change",
    [
        {"text": ""},
        {"channel": "worker"},
        {"target": ["T001", 2, "accept"]},
        {"target": ["T001", 1, "reject"]},
    ],
)
def test_g_user_rejects_incomplete_or_mismatched_sources(change: dict[str, Any]) -> None:
    guards.g_user(SOURCE, task_id="T001", revision=1, kind="accept")
    assert (
        _reason(
            lambda: guards.g_user({**SOURCE, **change}, task_id="T001", revision=1, kind="accept")
        )
        == "g_user"
    )


@pytest.mark.transition("G-ACCEPT")
def test_g_accept_items() -> None:
    expected = ["功能可用", "测试通过"]
    done = [{"item": "功能可用", "status": "done", "evidence": "截图"}]
    replaced = {
        "item": "测试通过",
        "status": "replaced_by_user",
        "user_source": {**SOURCE, "target": ["T001", 1, "replace"]},
    }
    checked = guards.g_accept_items(expected, [*done, replaced], task_id="T001", revision=1)
    assert [c["status"] for c in checked] == ["done", "replaced_by_user"]
    assert (
        _reason(lambda: guards.g_accept_items(expected, done, task_id="T001", revision=1))
        == "g_accept"
    )
    no_evidence = [{"item": i, "status": "done", "evidence": ""} for i in expected]
    assert (
        _reason(lambda: guards.g_accept_items(expected, no_evidence, task_id="T001", revision=1))
        == "g_accept"
    )
    bad_source = [done[0], {**replaced, "user_source": SOURCE}]
    assert (
        _reason(lambda: guards.g_accept_items(expected, bad_source, task_id="T001", revision=1))
        == "g_user"
    )


@pytest.mark.transition("G-SUBMIT")
def test_g_submit(conn: sqlite3.Connection, home: Home, tmp_path: Path) -> None:
    config = _config(home)
    _task(conn, "T001", "in_progress", cycle=1)
    row = dict(conn.execute("SELECT * FROM tasks").fetchone())
    assert (
        _reason(lambda: guards.g_submit(conn, config, row, "a1", "h2")) == "g_submit"
    )  # no review
    conn.execute(
        "INSERT INTO review_cycles(task_id, attempt, cycle, rounds, last_round, "
        "last_reviewed_head, last_decision) VALUES('T001', 'a1', 1, 1, 1, 'h1', 'finalize')"
    )
    conn.execute(
        "INSERT INTO review_rounds VALUES('T001', 'a1', 1, 1, 'h1', ?, 'pass', 'h1', 'finalize')",
        (json.dumps({"session": "codex-app-server"}),),
    )
    assert (
        _reason(lambda: guards.g_submit(conn, config, row, "a1", "h2")) == "g_submit"
    )  # head moved
    assert guards.g_submit(conn, config, row, "a1", "h1")["rounds"] == [1]
    conn.execute(
        "INSERT INTO findings(task_id, attempt, round, finding_id, cycle, severity, blocking, "
        "action) VALUES('T001', 'a1', 1, 'F1', 1, 'high', 1, 'fix')"
    )
    assert (
        _reason(lambda: guards.g_submit(conn, config, row, "a1", "h1")) == "g_submit"
    )  # fix not verified
    conn.execute("UPDATE findings SET verification = 'test ok'")
    guards.g_submit(conn, config, row, "a1", "h1")
    conn.execute("UPDATE review_rounds SET reviewer = ?", (json.dumps({"session": "claude-app"}),))
    assert (
        _reason(lambda: guards.g_submit(conn, config, row, "a1", "h1")) == "g_submit"
    )  # not the reviewer
