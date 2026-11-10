"""The card STATUS page rendered from an in-memory projection (UC-HOST-003)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.host.status import AppView, render
from tsw.host.status_assets import APP_CSP, APP_SCRIPT, CSP, SCRIPT
from tsw.projection import SCHEMA_SQL

mark = pytest.mark


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


_seq = iter(range(1, 1000))


def _task(db: sqlite3.Connection, task_id: str, status: str, **fields: Any) -> None:
    row: dict[str, Any] = {
        "task_id": task_id,
        "title": f"title {task_id}",
        "repo": "demo",
        "kind": "code",
        "status": status,
        "revision": 1,
        "version": 1,
        "spec_path": "todo/x.md",
        "spec_sha256": "x",
        "registered_seq": next(_seq),
        "last_activity": "2026-11-01T08:00:00Z",
        "source_seq": 1,
        **fields,
    }
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    db.execute(f"INSERT INTO tasks({names}) VALUES({marks})", tuple(row.values()))


def _card(page: str, task_id: str) -> str:
    match = re.search(rf'<article [^>]*id="task-{task_id}".*?</article>', page, re.S)
    assert match, task_id
    return match.group(0)


@mark.use_case("UC-HOST-003")
def test_every_task_but_the_archived_has_a_card_in_its_status_group(
    conn: sqlite3.Connection,
) -> None:
    _task(conn, "T001", "in_progress", kind="research", repo="alpha")
    _task(conn, "T002", "accepted")
    _task(conn, "T003", "archived")
    page = render(conn)
    card = _card(page, "T001")
    for attribute in ('data-status="in_progress"', 'data-repo="alpha"', 'data-kind="research"'):
        assert attribute in card
    assert "执行中" in card and "研究" in card and "2026-11-01T08:00:00Z" in card
    assert 'id="task-T003"' not in page
    assert "已归档任务不在此列出（1 个）" in page
    # Finished work is folded until opened; active groups are open.
    assert '<details class="group st-in_progress" open>' in page
    assert '<details class="group st-accepted">' in page
    # Only statuses on the board are offered as filters.
    assert 'data-st="in_progress"' in page and 'data-st="accepted"' in page
    assert 'data-st="paused"' not in page


@mark.use_case("UC-HOST-003")
def test_what_waits_on_the_user_is_flagged_on_the_card(conn: sqlite3.Connection) -> None:
    _task(conn, "T001", "done")
    _task(conn, "T002", "waiting_user")
    conn.execute(
        "INSERT INTO input_requests(request_id, task_id, kind, status, question, binding) "
        "VALUES('Q-T002-1', 'T002', 'question', 'pending', '用 A 还是 B？', '{}')"
    )
    _task(conn, "T003", "paused", pause=json.dumps({"category": "external", "owner": "用户"}))
    _task(conn, "T004", "paused", pause=json.dumps({"category": "external", "owner": "主控"}))
    _task(conn, "T005", "ready")
    page = render(conn)
    assert "待用户验收" in _card(page, "T001")
    asked = _card(page, "T002")
    assert "用 A 还是 B？" in asked and "Q-T002-1" in asked and "请在主会话中回答" in asked
    for task_id, flags in (
        ("T001", "user"),
        ("T002", "asking user"),
        ("T003", "paused user"),
        ("T004", "paused"),
        ("T005", "tostart"),
    ):
        assert f'data-flags="{flags}"' in _card(page, task_id), task_id
    assert 'class="card st-done k-code user"' in page
    assert "待用户处理" in _card(page, "T001")
    assert "待用户处理" not in _card(page, "T004")


@mark.use_case("UC-HOST-003")
def test_links_dependencies_and_details_are_on_the_card(conn: sqlite3.Connection) -> None:
    submission = {"report": {"html_path": "runs/T002/a1/result.html"}}
    _task(conn, "T001", "accepted")
    _task(conn, "T002", "done", submission=json.dumps(submission), goal="把看板做成卡片")
    conn.execute(
        "INSERT INTO dependencies(task_id, dep_id, bound_revision, kind, reason) "
        "VALUES('T002', 'T001', 1, 'explicit', 'x')"
    )
    conn.execute(
        "INSERT INTO outputs(task_id, output_id, type, ref, delivery, evidence) VALUES"
        "('T002', 'pr', 'pr', 'https://example.test/o/r/pull/7', 'merged', '[]'),"
        "('T002', 'data', 'dataset', 'runs/T002/a1/data.csv', NULL, '[]')"
    )
    card = _card(render(conn), "T002")
    assert '<a href="https://example.test/o/r/pull/7">PR #7</a><small>merged</small>' in card
    assert '<a href="../runs/T002/a1/result.html">报告</a>' in card
    assert '<a href="#task-T001">T001</a>' in card
    assert "把看板做成卡片" in card and "其他产出（1）" in card
    assert '<a href="../runs/T002/a1/data.csv">data</a>' in card


@mark.use_case("UC-HOST-003")
def test_the_page_is_offline_escaped_and_runs_only_its_own_script(
    conn: sqlite3.Connection,
) -> None:
    _task(conn, "T001", "in_progress", title="<script>alert(1)</script>", repo='a"b')
    page = render(conn)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'data-repo="a&quot;b"' in page
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts == [SCRIPT]
    digest = base64.b64encode(hashlib.sha256(SCRIPT.encode("utf-8")).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in CSP
    assert f'content="{CSP}"' in page.replace("&#x27;", "'")
    # Nothing is fetched: no external scripts, stylesheets, images or fonts.
    assert not re.search(r"<(link|img|iframe)\b|\bsrc=|@import|url\(", page)
    # Controls that need the script stay hidden until it runs; plain is the default style.
    for control in ("tools", "refresh", "style", "theme"):
        assert re.search(rf'id="{control}"[^>]* hidden', page), control
    assert "data-style" not in page.split("<body>")[1]
    # The page shows recorded times only, so rendering again changes nothing.
    assert render(conn) == page


def test_the_inline_script_cannot_close_its_own_element() -> None:
    assert "</" not in SCRIPT


@mark.use_case("UC-HOST-003")
def test_an_empty_board_says_so(conn: sqlite3.Connection) -> None:
    page = render(conn)
    assert "没有任务" in page
    assert "<article" not in page


@mark.use_case("UC-DIRECT-002")
def test_the_status_app_page_adds_the_direct_actions(conn: sqlite3.Connection) -> None:
    _task(conn, "T001", "done", revision=2)
    _task(conn, "T002", "in_progress")
    _task(conn, "T003", "ready")
    static = render(conn)
    assert 'data-act="' not in static and "tsw-token" not in static
    assert "tsw task accept T001" in _card(static, "T001")
    app = AppView(
        token="tok-1",
        actions={
            "T001": (("accept", "reject"), None),
            "T002": ((), "worker 仍在运行：请在主会话中先停止 worker 再取消"),
            "T003": (("cancel",), None),
        },
    )
    page = render(conn, app)
    first = _card(page, "T001")
    assert '<div class="acts" data-task="T001" data-revision="2">' in first
    assert 'data-act="accept"' in first and 'data-act="reject"' in first
    assert 'data-act="' not in _card(page, "T002")
    assert "先停止 worker" in _card(page, "T002")
    assert 'data-act="cancel"' in _card(page, "T003")
    assert '<meta name="tsw-token" content="tok-1">' in page
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts == [SCRIPT, APP_SCRIPT]
    digest = base64.b64encode(hashlib.sha256(APP_SCRIPT.encode("utf-8")).digest()).decode()
    assert f"'sha256-{digest}'" in APP_CSP and "connect-src 'self'" in APP_CSP
    assert f'content="{APP_CSP}"' in page.replace("&#x27;", "'")
    # The offline page stays as it was: only the app talks to a server.
    assert "connect-src" not in CSP


def test_the_app_script_cannot_close_its_own_element() -> None:
    assert "</" not in APP_SCRIPT
