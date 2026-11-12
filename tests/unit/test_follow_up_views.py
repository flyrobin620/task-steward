"""Follow-ups in the projection and on the pages (DN002 §5.7, DN006 §8.10): the reducers, the
generated goal of a task made from follow-ups, ``attention``'s due list, the board and the task
view."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.errors import TswError
from tsw.events import Event
from tsw.host.status import AppView, render
from tsw.host.task_view import render_task
from tsw.importer.renumber import reduce as renumber
from tsw.machine import follow_up
from tsw.machine.reducers import REDUCERS
from tsw.projection import SCHEMA_SQL
from tsw.tasks import task_row

mark = pytest.mark
APP = AppView(token="tok-1", actions={})


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for n, (task_id, status) in enumerate(
        (("T001", "accepted"), ("T002", "intake"), ("T003", "in_progress")), start=1
    ):
        db.execute(
            "INSERT INTO tasks(task_id, title, repo, kind, status, revision, version, spec_path, "
            "spec_sha256, registered_seq, last_activity, source_seq, business_changed_at) "
            "VALUES(?, ?, 'demo', 'code', ?, 1, 1, ?, 'x', ?, 't0', 1, 't0')",
            (task_id, f"title {task_id}", status, f"todo/{task_id}.md", n),
        )
    yield db
    db.close()


_seq = iter(range(100, 10_000))


def apply(db: sqlite3.Connection, kind: str, task_id: str, actor: str, **data: Any) -> None:
    sequence = next(_seq)
    REDUCERS[kind](
        db,
        Event(
            schema=1,
            sequence=sequence,
            time=f"2026-11-11T00:{sequence % 60:02d}:00Z",
            type=kind,
            actor={"role": actor},
            prev="",
            checksum="",
            task_id=task_id,
            revision=1,
            data=data,
        ),
    )


def record(db: sqlite3.Connection, task_id: str, *titles: str, **extra: Any) -> None:
    apply(
        db,
        "follow_up.recorded",
        task_id,
        "worker",
        items=[
            {"follow_up_id": f"F-{task_id}-{i}", "title": t, "detail": f"说明 {t}", **extra}
            for i, t in enumerate(titles, start=1)
        ],
        by={"role": "worker", "attempt": "a1", "session_ref": "w-1"},
    )


@mark.use_case("UC-FOLLOWUP-001")
def test_recording_keeps_the_task_state_and_business_time(conn: sqlite3.Connection) -> None:
    record(conn, "T001", "对账", "全量")
    row = task_row(conn, "T001")
    assert row["status"] == "accepted" and row["version"] == 2
    assert row["business_changed_at"] == "t0"
    assert [(f["follow_up_id"], f["status"], f["kind"]) for f in row["follow_ups"]] == [
        ("F-T001-1", "open", "remaining"),
        ("F-T001-2", "open", "remaining"),
    ]
    assert row["follow_ups"][0]["recorded_by"] == {
        "role": "worker",
        "attempt": "a1",
        "session_ref": "w-1",
    }
    apply(
        conn,
        "follow_up.updated",
        "T001",
        "orchestrator",
        follow_up_id="F-T001-2",
        changes={"recheck_after": "2026-12-01", "severity": "low", "status": "dismissed"},
        reason="暂不处理",
    )
    updated = task_row(conn, "T001")["follow_ups"][1]
    # Only the fields a change may touch: never the status.
    assert (updated["recheck_after"], updated["severity"], updated["status"]) == (
        "2026-12-01",
        "low",
        "open",
    )
    assert follow_up.open_count(conn) == {"T001": 2}
    assert [d["follow_up_id"] for d in follow_up.due(conn, "2026-11-30")] == ["F-T001-1"]
    assert [d["follow_up_id"] for d in follow_up.due(conn, "2026-12-01")] == [
        "F-T001-1",
        "F-T001-2",
    ]
    assert follow_up.has_open(conn, "T001") and not follow_up.has_open(conn, "T002")


@mark.use_case("UC-FOLLOWUP-002")
@mark.use_case("UC-FOLLOWUP-003")
def test_converted_and_dismissed_follow_ups_close_and_a_ledger_cannot_reopen_them(
    conn: sqlite3.Connection,
) -> None:
    record(conn, "T001", "对账", "全量", "异常")
    apply(
        conn,
        "follow_up.converted",
        "T001",
        "orchestrator",
        follow_up_ids=["F-T001-1", "F-T001-2"],
        to_task="T002",
        mode="existing",
    )
    apply(
        conn,
        "follow_up.dismissed",
        "T001",
        "orchestrator",
        follow_up_ids=["F-T001-3"],
        reason="未复现",
        source={"channel": "main-session", "text": "不查了"},
    )
    rows = {f["follow_up_id"]: f for f in task_row(conn, "T001")["follow_ups"]}
    assert rows["F-T001-1"]["status"] == "converted" and rows["F-T001-1"]["converted_to"] == "T002"
    assert rows["F-T001-1"]["resolution"]["mode"] == "existing"
    assert rows["F-T001-3"]["status"] == "dismissed" and rows["F-T001-3"]["converted_to"] is None
    assert rows["F-T001-3"]["resolution"]["reason"] == "未复现"
    assert rows["F-T001-3"]["resolution"]["source"]["text"] == "不查了"
    assert [f["follow_up_id"] for f in task_row(conn, "T002")["follow_up_of"]] == [
        "F-T001-1",
        "F-T001-2",
    ]
    assert follow_up.open_count(conn) == {}
    with pytest.raises(TswError) as exc:
        apply(
            conn,
            "follow_up.dismissed",
            "T001",
            "orchestrator",
            follow_up_ids=["F-T001-1"],
            reason="r",
        )
    assert exc.value.details["reason"] == "ledger_inconsistent"
    # A converted follow-up follows its task to a new number.
    renumber(
        conn,
        Event(
            schema=1,
            sequence=999,
            time="t",
            type="task.renumbered",
            actor={},
            prev="",
            checksum="",
            task_id="T020",
            data={"from": "T002", "spec_path": "todo/T020.md", "moved": {}},
        ),
    )
    assert {f["converted_to"] for f in task_row(conn, "T001")["follow_ups"][:2]} == {"T020"}


@mark.use_case("UC-FOLLOWUP-002")
def test_a_task_made_from_follow_ups_lists_them_in_its_goal_and_acceptance(
    conn: sqlite3.Connection,
) -> None:
    record(conn, "T001", "对账", "全量")
    found = [dict(r) for r in conn.execute("SELECT * FROM follow_ups ORDER BY follow_up_id")]
    assert follow_up.default_goal(found) == (
        "来自 T001 的 follow-up：\n- F-T001-1 对账：说明 对账\n- F-T001-2 全量：说明 全量"
    )
    assert follow_up.default_acceptance(found) == ["完成 F-T001-1：对账", "完成 F-T001-2：全量"]


@mark.use_case("UC-FOLLOWUP-004")
def test_the_board_counts_open_follow_ups_and_names_where_a_task_came_from(
    conn: sqlite3.Connection,
) -> None:
    record(conn, "T001", "对账", "全量", "异常")
    record(conn, "T003", "文档")
    apply(
        conn,
        "follow_up.converted",
        "T001",
        "orchestrator",
        follow_up_ids=["F-T001-1"],
        to_task="T002",
        mode="new",
    )
    page = render(conn, APP)
    card = re.search(r'<article [^>]*id="task-T001".*?</article>', page, re.S)
    assert card is not None
    assert 'data-flags="followups"' in card.group(0)
    assert '<a class="fu" href="tasks/T001#follow-ups">2 条待处理</a>' in card.group(0)
    made = re.search(r'<article [^>]*id="task-T002".*?</article>', page, re.S)
    assert made is not None
    assert '<dt>来自</dt><dd><a href="tasks/T001#follow-up-F-T001-1">F-T001-1</a></dd>' in (
        made.group(0)
    )
    assert "followups" not in re.findall(r'data-flags="([^"]*)"', made.group(0))[0]
    # The quick pick after the kinds counts the tasks with open follow-ups, not the items
    # (T273 revision 3), and filters on the flag; it is not a top-level tile.
    with_open = len(re.findall(r'<article [^>]*data-flags="[^"]*followups', page))
    pick = re.search(r'<button[^>]*data-quick="followups"[^>]*>follow-up<small>(\d+)</small>', page)
    assert pick is not None and int(pick.group(1)) == with_open < 3
    assert "follow-up" not in page.split('<section class="tiles">')[1].split("</section>")[0]
    assert '<option value="followups">' in page
    # The static page has no task views: the counts are text, the source is the card.
    static = render(conn)
    assert '<span class="fu">2 条待处理</span>' in static
    assert '<a href="#task-T001">F-T001-1</a>' in static
    assert "tasks/T001" not in static
    # Once the source is archived its card is gone: the static page names it without a link,
    # the app still links its task view.
    conn.execute("UPDATE tasks SET status = 'archived' WHERE task_id = 'T001'")
    static = render(conn)
    assert "<dt>来自</dt><dd>F-T001-1</dd>" in static and 'href="#task-T001"' not in static
    assert 'href="tasks/T001#follow-up-F-T001-1"' in render(conn, APP)


@mark.use_case("UC-FOLLOWUP-004")
def test_the_task_view_lists_follow_ups_open_first_with_their_way_out(
    conn: sqlite3.Connection,
) -> None:
    record(conn, "T001", "对账", "<b>全量</b>", "异常", kind="out_of_scope", severity="high")
    apply(
        conn,
        "follow_up.converted",
        "T001",
        "orchestrator",
        follow_up_ids=["F-T001-1"],
        to_task="T002",
        mode="new",
    )
    apply(
        conn,
        "follow_up.dismissed",
        "T001",
        "orchestrator",
        follow_up_ids=["F-T001-3"],
        reason="未复现",
    )
    apply(
        conn,
        "follow_up.updated",
        "T001",
        "orchestrator",
        follow_up_id="F-T001-2",
        changes={"recheck_after": "2026-12-01"},
        reason="暂不处理",
        source={"channel": "main-session", "text": "先放着"},
    )
    page = render_task(conn, "T001", APP, [])
    assert page is not None
    section = re.search(r'<section class="panel" id="follow-ups">.*?</section>', page, re.S)
    assert section is not None
    body = section.group(0)
    assert "follow-up（待处理 1）" in body
    ids = re.findall(r'<tr id="follow-up-(F-T001-\d)"', body)
    assert ids == ["F-T001-2", "F-T001-1", "F-T001-3"]
    assert "&lt;b&gt;全量&lt;/b&gt;" in body and "<b><b>全量" not in body
    assert "范围外发现<br>严重度 高" in body
    assert '转为 <a href="T002">T002</a>' in body
    assert "理由：未复现" in body
    assert "暂不处理，复查日期 2026-12-01" in body
    assert "revision 1 · worker a1" in body
    made = render_task(conn, "T002", APP, [])
    assert made is not None
    assert '<li><a href="T001#follow-up-F-T001-1">F-T001-1</a> 对账</li>' in made
    empty = render_task(conn, "T003", APP, [])
    assert empty is not None
    assert re.search(
        r'id="follow-ups"><h2>follow-up<span class="n">0</span></h2><p class="none">无', empty
    )


@mark.use_case("UC-FOLLOWUP-001")
def test_attention_shape_has_what_the_orchestrator_needs(conn: sqlite3.Connection) -> None:
    record(conn, "T001", "安全问题", kind="out_of_scope", severity="high")
    assert follow_up.due(conn, "2026-11-11") == [
        {
            "follow_up_id": "F-T001-1",
            "task_id": "T001",
            "kind": "out_of_scope",
            "severity": "high",
            "title": "安全问题",
        }
    ]
    assert json.dumps(follow_up.due(conn, "2026-11-11"), ensure_ascii=False)
