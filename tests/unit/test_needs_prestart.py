"""``attention``'s ``needs_prestart`` against an in-memory projection (DN002 §5.6)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.config import EffectiveConfig, load_config
from tsw.home import Home
from tsw.host.inbox import needs_prestart
from tsw.projection import SCHEMA_SQL

mark = pytest.mark


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


def _config(home: Home, **prestart: Any) -> EffectiveConfig:
    settings: dict[str, Any] = {
        "version": 1,
        "repositories": {"demo": {"aliases": ["d"]}},
        "scheduling": {"prestart": {"enabled": True, "max_standby": 10, **prestart}},
    }
    home.path("config/task-steward.json").write_text(json.dumps(settings), encoding="utf-8")
    return load_config(home).require_valid()


_seq = iter(range(1, 1000))


def _task(
    db: sqlite3.Connection,
    task_id: str,
    status: str,
    *,
    deps: tuple[str, ...] = (),
    attempt: str | None = None,
    purpose: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO tasks(task_id, title, repo, kind, status, revision, version, spec_path, "
        "spec_sha256, registered_seq, last_activity, source_seq, current_attempt) "
        "VALUES(?, 't', 'demo', 'code', ?, 1, 1, 'todo/x.md', 'x', ?, 't', 1, ?)",
        (task_id, status, next(_seq), None if attempt is None else "a1"),
    )
    if attempt is not None:
        db.execute(
            "INSERT INTO attempts(task_id, attempt, phase, purpose) VALUES(?, 'a1', ?, ?)",
            (task_id, attempt, purpose),
        )
    for dep in deps:
        db.execute(
            "INSERT INTO dependencies(task_id, dep_id, bound_revision, kind, reason) "
            "VALUES(?, ?, 1, 'hard', 'r')",
            (task_id, dep),
        )


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["task_id"]) for item in items]


@mark.use_case("UC-HOST-003")
def test_ready_tasks_first_then_waiting_ones_upstream_first(
    conn: sqlite3.Connection, home: Home
) -> None:
    # Registered downstream first: the order follows the dependencies, not the registration.
    _task(conn, "T005", "waiting_dependency", deps=("T004",))
    _task(conn, "T004", "waiting_dependency", deps=("T001",))
    _task(conn, "T001", "in_progress", attempt="live")
    _task(conn, "T002", "ready")
    _task(conn, "T003", "ready", attempt="planned")
    items = needs_prestart(conn, _config(home))
    assert _ids(items) == ["T002", "T003", "T004", "T005"]
    assert items[0] == {"task_id": "T002", "status": "ready"}
    assert items[2] == {"task_id": "T004", "status": "waiting_dependency", "waiting_on": ["T001"]}


@mark.use_case("UC-HOST-003")
@pytest.mark.parametrize("phase", ["starting", "standby", "live", "stopped"])
def test_a_task_with_a_session_is_not_listed(
    conn: sqlite3.Connection, home: Home, phase: str
) -> None:
    _task(conn, "T001", "ready", attempt=phase, purpose="prestart")
    assert needs_prestart(conn, _config(home)) == []


@mark.use_case("UC-HOST-003")
@pytest.mark.parametrize("phase", ["planned", "replaced", "closed"])
def test_a_task_whose_session_is_gone_or_not_created_is_listed(
    conn: sqlite3.Connection, home: Home, phase: str
) -> None:
    _task(conn, "T001", "waiting_dependency", deps=("T009",), attempt=phase)
    _task(conn, "T009", "in_progress")
    assert _ids(needs_prestart(conn, _config(home))) == ["T001"]


@mark.use_case("UC-HOST-003")
def test_only_ready_and_waiting_tasks_with_a_live_upstream(
    conn: sqlite3.Connection, home: Home
) -> None:
    for task_id, status in (
        ("T001", "intake"),
        ("T002", "waiting_user"),
        ("T003", "paused"),
        ("T004", "done"),
        ("T005", "cancelled"),
    ):
        _task(conn, task_id, status)
    _task(conn, "T006", "waiting_dependency", deps=("T005",))  # upstream cancelled
    _task(conn, "T007", "waiting_dependency", deps=("T404",))  # upstream missing
    _task(conn, "T008", "waiting_dependency", deps=("T003",))  # upstream paused: may resume
    assert _ids(needs_prestart(conn, _config(home))) == ["T008"]


@mark.use_case("UC-HOST-003")
def test_the_list_fits_the_free_standby_places(conn: sqlite3.Connection, home: Home) -> None:
    _task(conn, "T001", "ready", attempt="standby", purpose="prestart")
    _task(conn, "T002", "ready", attempt="starting", purpose="prestart")
    _task(conn, "T003", "dispatching", attempt="starting", purpose="dispatch")  # not a standby
    for task_id in ("T004", "T005", "T006"):
        _task(conn, task_id, "ready")
    assert _ids(needs_prestart(conn, _config(home, max_standby=4))) == ["T004", "T005"]
    assert needs_prestart(conn, _config(home, max_standby=2)) == []
    assert needs_prestart(conn, _config(home, enabled=False)) == []
