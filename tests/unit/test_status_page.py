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

from tsw.events import Event
from tsw.host.status import AppView, Revision, render
from tsw.host.status_app import history
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
    # One switch to a single list, newest change first (the script sets group and sort).
    assert re.search(r'<button[^>]*id="timeline"[^>]*>按时间顺序</button>', page)
    assert 'id="timeline"' in page.split('id="tools"')[1].split("</section>")[0]
    assert '"timeline"' in SCRIPT and 'state.sort = on ? "status" : "activity"' in SCRIPT
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


def test_an_unsent_reason_holds_the_auto_refresh() -> None:
    # The app marks the root (not a card, which the filters may detach); status.js looks for it.
    assert "document.documentElement.dataset.holdRefresh" in APP_SCRIPT
    assert 'document.querySelector("[data-hold-refresh]")' in SCRIPT


@mark.use_case("UC-HOST-003")
def test_long_notes_scroll_and_the_brief_folds_at_the_foot(conn: sqlite3.Connection) -> None:
    _task(
        conn,
        "T001",
        "in_progress",
        revision=3,
        current_attempt="a1",
        goal="做 STATUS 应用",
        acceptance=json.dumps(["一键验收"]),
    )
    conn.execute(
        "INSERT INTO attempts(task_id, attempt, phase, brief_path) "
        "VALUES('T001', 'a1', 'live', 'runs/T001/a1/brief.md')"
    )
    conn.execute(
        "INSERT INTO input_requests(request_id, task_id, kind, status, question, binding) "
        "VALUES('Q-T001-1', 'T001', 'question', 'pending', ?, '{}')",
        ("很长的问题。" * 200,),
    )
    card = _card(render(conn), "T001")
    assert '：<span class="clip">很长的问题。' in card
    brief = re.search(
        r'<details class="brief"><summary>任务书 · revision 3</summary>.*?</details>', card
    )
    assert brief and "做 STATUS 应用" in brief.group(0) and "<li>一键验收</li>" in brief.group(0)
    assert '<a href="../runs/T001/a1/brief.md">' in brief.group(0)
    assert "历史任务书" not in card  # the offline page has the current revision only
    assert card.index('<div class="foot">') > card.index('<dl class="facts">')


@mark.use_case("UC-DIRECT-002")
def test_the_status_app_lists_the_earlier_revisions(conn: sqlite3.Connection) -> None:
    _task(conn, "T001", "in_progress", revision=3, goal="第三版")
    history = {
        "T001": [
            Revision(1, "2026-11-10T01:00:00Z", "登记", "第一版", ("a",)),
            Revision(2, "2026-11-10T02:00:00Z", "需求变更", "第二版", ("a", "b"), "加一个按钮"),
            Revision(3, "2026-11-10T03:00:00Z", "用户退回", "第二版", ("a", "b"), "缺导出"),
        ]
    }
    card = _card(render(conn, AppView(token="t", actions={}, history=history)), "T001")
    past = re.search(
        r'<details class="brief history"><summary>历史任务书（2）</summary>.*?</details>', card
    )
    assert past, card
    text = past.group(0)
    # Newest first; the current revision is the brief above, not history.
    assert text.index("revision 2 · 需求变更") < text.index("revision 1 · 登记")
    assert "加一个按钮" in text and "第一版" in text and "缺导出" not in text


def _event(seq: int, kind: str, task_id: str, rev: int, **data: Any) -> Event:
    return Event(
        schema=1,
        sequence=seq,
        time=f"2026-11-10T00:00:{seq:02d}Z",
        type=kind,
        actor={"role": "cli"},
        prev="",
        checksum="",
        task_id=task_id,
        revision=rev,
        data=data,
    )


@mark.use_case("UC-DIRECT-002")
def test_history_starts_at_an_import_and_follows_a_renumbered_task() -> None:
    events = [
        _event(1, "task.registered", "T001", 1, goal="本地任务", acceptance=["x"]),
        _event(2, "task.user_rejected", "T001", 2, revision=2, reason="不对"),
        _event(3, "task.renumbered", "T238", 2, **{"from": "T001", "to": "T238"}),
        _event(4, "import.task_imported", "T001", 3, goal="导入的任务", acceptance=["y"]),
        _event(
            5,
            "task.requirement_changed",
            "T001",
            4,
            revision=4,
            goal="导入后改过",
            acceptance=["y", "z"],
            user_source={"text": "再加 z"},
        ),
    ]
    past = history(events)
    assert [(r.revision, r.how, r.goal) for r in past["T238"]] == [
        (1, "登记", "本地任务"),
        (2, "用户退回", "本地任务"),
    ]
    assert [(r.revision, r.how, r.goal, r.words) for r in past["T001"]] == [
        (3, "导入", "导入的任务", ""),
        (4, "需求变更", "导入后改过", "再加 z"),
    ]
