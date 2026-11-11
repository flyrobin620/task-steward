"""The STATUS app's task view and the session links (UC-DIRECT-003, DN006 §8.10)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.events import Event
from tsw.host.status import AppView, Revision, render, session_href, session_link
from tsw.host.status_assets import APP_CSP, APP_SCRIPT, SCRIPT
from tsw.host.task_view import TIMELINE_LIMIT, Step, not_found, render_task, timeline
from tsw.projection import SCHEMA_SQL

mark = pytest.mark
APP = AppView(token="tok-1", actions={})


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


_seq = iter(range(1, 1000))


def _insert(db: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    db.execute(f"INSERT INTO {table}({names}) VALUES({marks})", tuple(row.values()))


def _task(db: sqlite3.Connection, task_id: str, status: str, **fields: Any) -> None:
    _insert(
        db,
        "tasks",
        {
            "task_id": task_id,
            "title": f"title {task_id}",
            "repo": "demo",
            "kind": "code",
            "status": status,
            "revision": 1,
            "version": 1,
            "spec_path": f"todo/{task_id}.md",
            "spec_sha256": "x",
            "registered_seq": next(_seq),
            "last_activity": "2026-11-01T08:00:00Z",
            "source_seq": 1,
            **fields,
        },
    )


def _attempt(db: sqlite3.Connection, task_id: str, attempt: str, **fields: Any) -> None:
    _insert(db, "attempts", {"task_id": task_id, "attempt": attempt, "phase": "live", **fields})


def _card(page: str, task_id: str) -> str:
    match = re.search(rf'<article [^>]*id="task-{task_id}".*?</article>', page, re.S)
    assert match, task_id
    return match.group(0)


def _section(page: str, key: str) -> str:
    match = re.search(rf'<section class="panel" id="{key}">.*?</section>', page, re.S)
    assert match, key
    return match.group(0)


# ---------------------------------------------------------------------- session links


@mark.use_case("UC-DIRECT-003")
def test_a_claude_app_session_links_to_the_desktop_app_with_its_attributes() -> None:
    assert session_href("claude-app", "local_ab-1") == "claude://claude.ai/epitaxy/local_ab-1"
    assert session_href(None, "local_ab-1") == "claude://claude.ai/epitaxy/local_ab-1"
    assert session_href("claude-app", "a/b c") == "claude://claude.ai/epitaxy/a%2Fb%20c"
    assert session_href("codex-app-server", "thr-1") is None
    attempt = {"attempt": "a2", "session_ref": "local_ab-1", "session_name": "t9-a2-g1"}
    link = session_link("T9", {**attempt, "adapter": "claude-app"})
    assert link == (
        '<a class="session" data-tsw-session="local_ab-1" data-tsw-task="T9" '
        'data-tsw-attempt="a2" data-tsw-adapter="claude-app" '
        'href="claude://claude.ai/epitaxy/local_ab-1" '
        'title="在宿主中打开会话 local_ab-1">t9-a2-g1</a>'
    )
    # No adapter recorded (an import, an older record): a claude-app session.
    assert 'data-tsw-adapter="claude-app"' in session_link("T9", attempt)
    # An adapter without an address keeps the attributes and has no link.
    other = session_link("T9", {**attempt, "adapter": "codex-app-server"})
    assert other.startswith('<span class="session" data-tsw-session="local_ab-1"')
    assert "href" not in other
    # No session, no link; a session without a name shows its reference.
    assert session_link("T9", {"attempt": "a1", "session_ref": None}) == ""
    assert session_link("T9", None) == ""
    assert ">local_x</a>" in session_link("T9", {"attempt": "a1", "session_ref": "local_x"})
    # A hostile reference is escaped in the attributes and the address.
    bad = session_link("T9", {"attempt": "a1", "session_ref": '"><x'})
    assert '"><x' not in bad and "%22%3E%3Cx" in bad


@mark.use_case("UC-DIRECT-003")
def test_the_card_links_its_session_and_in_the_app_its_task_view(
    conn: sqlite3.Connection,
) -> None:
    _task(conn, "T001", "in_progress", current_attempt="a1")
    _attempt(
        conn, "T001", "a1", session_ref="local_s1", session_name="t001-a1-g1", adapter="claude-app"
    )
    _task(conn, "T002", "ready")
    offline = render(conn)
    card = _card(offline, "T001")
    assert 'data-tsw-session="local_s1" data-tsw-task="T001" data-tsw-attempt="a1"' in card
    assert 'href="claude://claude.ai/epitaxy/local_s1"' in card
    assert "data-tsw-session" not in _card(offline, "T002")
    # The offline page is a file, not a server: it has no task view to link to.
    assert 'class="detail"' not in offline
    app = render(conn, APP)
    assert '<a class="detail" href="tasks/T001">详情</a>' in _card(app, "T001")
    assert '<a class="detail" href="tasks/T002">详情</a>' in _card(app, "T002")


# ---------------------------------------------------------------------- the task view


def _full_task(conn: sqlite3.Connection) -> None:
    submission = {
        "revision": 2,
        "cycle": 1,
        "head": "abc1234",
        "rounds": [1, 2],
        "report": {"md_path": "runs/T005/a2/result.md", "html_path": "runs/T005/a2/result.html"},
        "worktree_clean": True,
        "merge": {
            "pr": "https://example.test/o/r/pull/9",
            "merge_commit": "def5678",
            "ci": {
                "commit": "def5678",
                "conclusion": "success",
                "url": "https://example.test/o/r/actions/runs/1",
            },
            "live": "not needed",
        },
    }
    _task(conn, "T001", "accepted")
    _task(
        conn,
        "T005",
        "done",
        revision=2,
        current_attempt="a2",
        attempt_count=2,
        cycle=1,
        goal="把详细页做出来",
        acceptance=json.dumps(["能打开", "能返回"]),
        resources=json.dumps(["repo:demo"]),
        submission=json.dumps(submission),
        done_at="2026-11-02T00:00:00Z",
    )
    _task(conn, "T006", "waiting_dependency")
    _attempt(conn, "T005", "a1", phase="closed", session_ref="local_old", adapter="claude-app")
    _attempt(
        conn,
        "T005",
        "a2",
        phase="closed",
        session_ref="local_new",
        session_name="t005-a2-g1",
        adapter="claude-app",
        workdir="work/T005/a2/demo",
        branch="tsw/t005-a2",
        brief_path="runs/T005/a2/brief.md",
        brief_revision=2,
        purpose="dispatch",
    )
    _attempt(conn, "T005", "a10", phase="planned")
    _insert(
        conn,
        "dependencies",
        {
            "task_id": "T005",
            "dep_id": "T001",
            "bound_revision": 1,
            "kind": "explicit",
            "reason": "先要它",
        },
    )
    _insert(
        conn,
        "dependencies",
        {
            "task_id": "T006",
            "dep_id": "T005",
            "bound_revision": 2,
            "kind": "explicit",
            "reason": "x",
        },
    )
    _insert(
        conn, "resource_claims", {"resource": "repo:demo", "owner_task": "T005", "status": "held"}
    )
    _insert(conn, "integrations", {"task_id": "T005", "repo": "demo", "status": "merged"})
    _insert(
        conn,
        "outputs",
        {
            "task_id": "T005",
            "output_id": "pr-9",
            "type": "pr",
            "ref": "https://example.test/o/r/pull/9",
            "role": "current",
            "delivery": "merged",
            "verified_at": "2026-11-02T00:00:00Z",
            "evidence": "[]",
        },
    )
    _insert(
        conn,
        "review_cycles",
        {
            "task_id": "T005",
            "attempt": "a2",
            "cycle": 1,
            "rounds": 2,
            "last_decision": "finalize",
            "last_reviewed_head": "abc1234",
        },
    )
    for round_no, verdict in ((1, "changes_requested"), (2, "pass")):
        _insert(
            conn,
            "review_rounds",
            {
                "task_id": "T005",
                "attempt": "a2",
                "round": round_no,
                "cycle": 1,
                "head": "abc1234",
                "reviewer": json.dumps({"session": "codex-app-server", "model": "gpt-6-astra"}),
                "verdict": verdict,
            },
        )
    _insert(
        conn,
        "findings",
        {
            "task_id": "T005",
            "attempt": "a2",
            "round": 1,
            "finding_id": "F1",
            "cycle": 1,
            "severity": "major",
            "blocking": 1,
        },
    )
    _insert(
        conn,
        "input_requests",
        {
            "request_id": "Q-T005-1",
            "task_id": "T005",
            "kind": "in_session",
            "status": "resolved",
            "question": "要不要分页？",
            "binding": "{}",
            "resolution": json.dumps(
                {"answer": "不要", "source": {"channel": "in-session", "text": "不用分页"}}
            ),
        },
    )
    _insert(
        conn,
        "input_requests",
        {
            "request_id": "Q-T005-2",
            "task_id": "T005",
            "kind": "in_session",
            "status": "pending",
            "question": "颜色用哪套？",
            "binding": "{}",
        },
    )
    _insert(
        conn,
        "in_session_requests",
        {
            "request_id": "Q-T005-2",
            "task_id": "T005",
            "session_ref": "local_new",
            "asked_at": "2026-11-02T01:00:00Z",
            "handoff_after": "2026-11-02T02:00:00Z",
            "reserved": 0,
        },
    )
    _insert(
        conn,
        "decisions",
        {
            "decision_id": "D-1",
            "task_id": "T005",
            "trigger": "worker.question",
            "decision": "继续",
            "rationale": "在授权内",
            "basis": "[]",
            "confidence": "high",
            "sequence": 40,
        },
    )
    _insert(conn, "task_notes", {"task_id": "T005", "sequence": 30, "text": "写完了第一版"})
    _insert(conn, "task_notes", {"task_id": "T005", "sequence": 41, "text": "修完评审意见"})


@mark.use_case("UC-DIRECT-003")
def test_the_task_view_shows_more_than_the_card(conn: sqlite3.Connection) -> None:
    _full_task(conn)
    history = [
        Revision(1, "2026-11-01T00:00:00Z", "登记", "旧目标", ("x",)),
        Revision(2, "2026-11-01T05:00:00Z", "用户退回", "旧目标", ("x",), "还缺返回"),
    ]
    app = AppView(
        token="tok-1", actions={"T005": (("accept", "reject"), None)}, history={"T005": history}
    )
    steps = [
        Step(30, "2026-11-01T09:00:00Z", "task.note", "a2", None),
        Step(41, "2026-11-02T00:00:00Z", "worker.submitted", "a2", "T13"),
    ]
    page = render_task(conn, "T005", app, steps)
    assert page is not None
    # The embedder's contract: the view names its task; every attempt with a session links it.
    assert '<main class="task-view" data-tsw-task="T005">' in page
    assert '<a class="back" href="../#task-T005">← 返回看板</a>' in page
    attempts = _section(page, "attempts")
    assert [m for m in re.findall(r"<tr><td>(a\d+)</td>", attempts)] == ["a1", "a2", "a10"]
    assert 'data-tsw-session="local_old" data-tsw-task="T005" data-tsw-attempt="a1"' in attempts
    assert 'href="claude://claude.ai/epitaxy/local_new"' in attempts
    assert "<code>work/T005/a2/demo</code>" in attempts and "<code>tsw/t005-a2</code>" in attempts
    assert '<a href="../runs/T005/a2/brief.md">brief.md（revision 2）</a>' in attempts
    # The current attempt's session is in the header too, with the same buttons as the card.
    head = page.split("</header>")[0]
    assert 'class="sess">会话：<a class="session" data-tsw-session="local_new"' in head
    assert '<div class="acts" data-task="T005" data-revision="2">' in head
    assert 'data-act="accept"' in head and "待用户处理" in head and "等待用户（Q-T005-2" in head
    # Requirement with its revisions.
    requirement = _section(page, "requirement")
    assert '<details class="brief" open>' in requirement and "把详细页做出来" in requirement
    assert (
        "能返回" in requirement
        and "还缺返回" in requirement
        and "<small>当前</small>" in requirement
    )
    # Submission, merge and review.
    submission = _section(page, "submission")
    assert "<code>abc1234</code>" in submission
    assert '<a href="../runs/T005/a2/result.html">报告</a>' in submission
    assert '<a href="https://example.test/o/r/actions/runs/1">success</a>' in submission
    assert "<code>def5678</code>" in submission and "not needed" in submission
    assert (
        "changes_requested" in submission
        and "1（阻塞 1）" in submission
        and "finalize" in submission
    )
    # The recorded reviewer and round list read as text, not as raw JSON.
    assert "<td>codex-app-server · gpt-6-astra</td>" in submission and "{" not in submission
    assert "<dt>评审轮次</dt><dd>1、2</dd>" in submission
    # Outputs, relations, questions, notes, timeline.
    assert 'href="https://example.test/o/r/pull/9"' in _section(page, "outputs")
    relations = _section(page, "relations")
    assert '<a href="T001">T001</a>' in relations and '<a href="T006">T006</a>' in relations
    assert "<code>repo:demo</code>" in relations and "held" in relations and "merged" in relations
    questions = _section(page, "questions")
    assert questions.index("Q-T005-2") < questions.index("Q-T005-1")  # the pending one first
    assert "待答 1" in questions and "不用分页" in questions and "转交期限" in questions
    assert "D-1" in questions and "在授权内" in questions
    notes = _section(page, "notes")
    assert notes.index("修完评审意见") < notes.index("写完了第一版")
    assert '<time datetime="2026-11-01T09:00:00Z">' in notes
    events = _section(page, "timeline")
    assert events.index("worker.submitted") < events.index("task.note")
    assert '<p class="none">未暂停</p>' in _section(page, "pause")


@mark.use_case("UC-DIRECT-003")
def test_the_task_view_is_relative_escaped_and_keeps_the_app_policy(
    conn: sqlite3.Connection,
) -> None:
    _full_task(conn)
    conn.execute("UPDATE tasks SET title = '<script>alert(1)</script>' WHERE task_id = 'T005'")
    page = render_task(conn, "T005", APP, [])
    assert page is not None
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    # The same head as the board: its policy, token and scripts, nothing else runs.
    assert f'content="{APP_CSP}"' in page.replace("&#x27;", "'")
    assert '<meta name="tsw-token" content="tok-1">' in page
    assert re.findall(r"<script>(.*?)</script>", page, re.S) == [SCRIPT, APP_SCRIPT]
    # No host or port anywhere; in-app links are relative so a path prefix keeps working.
    hrefs = re.findall(r'href="([^"]*)"', page)
    assert hrefs
    for href in hrefs:
        assert not href.startswith("/"), href
        assert href.startswith(("../", "claude://", "https://example.test/")) or re.fullmatch(
            r"T\d+", href
        ), href
    assert "127.0.0.1" not in page and "localhost" not in page


@mark.use_case("UC-DIRECT-003")
def test_a_task_with_nothing_recorded_yet_shows_empty_sections(conn: sqlite3.Connection) -> None:
    _task(conn, "T007", "intake")
    page = render_task(conn, "T007", APP, [])
    assert page is not None
    for key in ("attempts", "outputs", "relations", "questions", "notes", "timeline"):
        assert '<p class="none">无</p>' in _section(page, key), key
    assert "当前没有提交" in _section(page, "submission")
    assert 'class="session"' not in page and 'class="acts"' not in page
    # An archived task is not on the board but still has its view.
    _task(conn, "T008", "archived")
    assert render_task(conn, "T008", APP, []) is not None


@mark.use_case("UC-DIRECT-003")
def test_an_unknown_task_has_no_view_and_the_way_back(conn: sqlite3.Connection) -> None:
    assert render_task(conn, "T404", APP, []) is None
    page = not_found("<T404>", APP)
    assert '<a class="back" href="../">← 返回看板</a>' in page
    assert "&lt;T404&gt;" in page and "<T404>" not in page


def _event(seq: int, kind: str, task_id: str | None, **data: Any) -> Event:
    return Event(
        schema=1,
        sequence=seq,
        time=f"2026-11-10T00:{seq // 60:02d}:{seq % 60:02d}Z",
        type=kind,
        actor={"role": "cli"},
        prev="",
        checksum="",
        task_id=task_id,
        attempt="a1" if kind == "worker.submitted" else None,
        data=data,
    )


@mark.use_case("UC-DIRECT-003")
def test_the_timeline_follows_a_renumbered_task_and_shows_the_newest(
    conn: sqlite3.Connection,
) -> None:
    events = [
        _event(1, "task.registered", "T001"),
        _event(2, "controller.lease_acquired", None),
        _event(3, "worker.submitted", "T001", transition="T13"),
        _event(4, "task.renumbered", "T238", **{"from": "T001", "to": "T238"}),
        _event(5, "task.registered", "T001"),
    ]
    found = timeline(events)
    assert [s.sequence for s in found["T238"]] == [1, 3, 4]
    assert [s.sequence for s in found["T001"]] == [5]
    assert found["T238"][1] == Step(3, "2026-11-10T00:00:03Z", "worker.submitted", "a1", "T13")
    many = [_event(i, "task.note", "T009") for i in range(1, TIMELINE_LIMIT + 6)]
    _task(conn, "T009", "in_progress")
    page = render_task(conn, "T009", APP, timeline(many)["T009"])
    assert page is not None
    shown = _section(page, "timeline")
    assert shown.count("<code>task.note</code>") == TIMELINE_LIMIT
    assert f"只列出最近 {TIMELINE_LIMIT} 条（共 {TIMELINE_LIMIT + 5} 条）" in shown
    assert f"<td>{TIMELINE_LIMIT + 5}</td>" in shown and "<td>5</td>" not in shown
