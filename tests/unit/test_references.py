"""References in the task flow and the task view, and the index card (UC-DIRECT-005, T278)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.context.index import IndexedCard, outputs_digest
from tsw.host.flow import FIRST_COLUMN, FlowTask, place
from tsw.host.relations import find_references, label, mentions, references
from tsw.host.status import AppView, render
from tsw.host.status_assets import CSS, SCRIPT
from tsw.host.task_view import card_state, render_task
from tsw.projection import SCHEMA_SQL
from tsw.storage.base import LedgerTail

mark = pytest.mark
APP = AppView(token="tok-1", actions={})


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


def _task(db: sqlite3.Connection, task_id: str, status: str, seq: int, **fields: Any) -> None:
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
        "registered_seq": seq,
        "last_activity": "2026-11-01T08:00:00Z",
        "source_seq": 1,
        **fields,
    }
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    db.execute(f"INSERT INTO tasks({names}) VALUES({marks})", tuple(row.values()))


def _attempt(db: sqlite3.Connection, task_id: str, attempt: str, read_set: list[str]) -> None:
    db.execute(
        "INSERT INTO attempts(task_id, attempt, phase, read_set) VALUES(?, ?, 'live', ?)",
        (task_id, attempt, json.dumps(read_set)),
    )


def _dep(db: sqlite3.Connection, task_id: str, dep_id: str) -> None:
    db.execute(
        "INSERT INTO dependencies(task_id, dep_id, bound_revision, kind, reason) "
        "VALUES(?, ?, 1, 'explicit', 'x')",
        (task_id, dep_id),
    )


def _node(page: str, task_id: str) -> str:
    found = re.search(rf'<(a|span) class="fnode[^"]*" data-id="{task_id}"[^>]*>', page)
    assert found, task_id
    return found.group(0)


def _section(page: str, key: str) -> str:
    match = re.search(rf'<section class="panel" id="{key}">.*?</section>', page, re.S)
    assert match, key
    return match.group(0)


# ---------------------------------------------------------------------- the rules


@mark.use_case("UC-DIRECT-005")
def test_task_ids_are_named_with_three_to_six_digits() -> None:
    text = (
        "参考 T162、T170 / T226（T229）与`T247`；T251,T276 再提一次 T162。"
        "迁移 T23 与 T07 不是任务；xT300、T301a、T1234567、T_302 也不是。"
    )
    assert mentions(text) == ["T162", "T170", "T226", "T229", "T247", "T251", "T276"]
    assert label("read_set:a1") == "推荐读取集合（a1）" and label("goal") == "目标"


@mark.use_case("UC-DIRECT-005")
def test_references_come_from_read_sets_and_named_ids_but_not_other_relations() -> None:
    tasks = [
        {"task_id": "T001", "title": "t", "goal": "", "acceptance": "[]"},
        {"task_id": "T002", "title": "t", "goal": "", "acceptance": "[]"},
        {"task_id": "T003", "title": "t", "goal": "", "acceptance": "[]"},
        {"task_id": "T004", "title": "t", "goal": "", "acceptance": "[]"},
        {
            "task_id": "T005",
            "title": "接着 T001 做",
            "goal": "参考 T002 与 T005 自己、T999（不存在）、T003（依赖）、T004（follow-up）",
            "acceptance": json.dumps(["与 T002 一致", "T23 迁移"]),
        },
    ]
    read_sets = {"T005": {"a1": ["T005", "T003", "T002"], "a2": ["T001"]}}
    joined = [("T005", "T003"), ("T004", "T005")]
    found = find_references(tasks, read_sets, joined)
    assert found == {
        "T005": {
            "T002": ["read_set:a1", "goal", "acceptance"],
            "T001": ["read_set:a2", "title"],
        }
    }


@mark.use_case("UC-DIRECT-005")
def test_the_projection_gives_the_references(conn: sqlite3.Connection) -> None:
    _task(conn, "T001", "archived", 1)
    _task(conn, "T002", "accepted", 2)
    _task(conn, "T003", "in_progress", 3, goal="见 T001")
    _attempt(conn, "T003", "a1", ["T003", "T002"])
    _dep(conn, "T003", "T002")  # the dependency says more than the read set
    assert references(conn) == {"T003": {"T001": ["goal"]}}


# ---------------------------------------------------------------------- the task flow


def _referring_board(db: sqlite3.Connection) -> None:
    _task(db, "T001", "archived", 1, repo="hub")  # referenced by a board task: context
    # T002 references a board task: context too.
    _task(db, "T002", "archived", 2, repo="hub", goal="沿用 T004 的做法")
    _task(db, "T003", "archived", 3, repo="hub")  # unrelated: only with the archived ones
    _task(db, "T004", "accepted", 4)
    _task(db, "T005", "in_progress", 5, goal="参考 T001 与 T004")
    _attempt(db, "T005", "a1", ["T005", "T001"])
    _task(db, "T006", "archived", 6, goal="后续见 T005")  # references a board task, newer


@mark.use_case("UC-DIRECT-005")
def test_the_flow_draws_references_and_keeps_referenced_archived_tasks_as_context(
    conn: sqlite3.Connection,
) -> None:
    _referring_board(conn)
    page = render(conn)
    flow = page.split('id="flow"')[1]
    t5 = _node(flow, "T005")
    assert 'data-refs="T001 T004"' in t5 and 'data-deps=""' in t5
    assert 'data-refs="T004"' in _node(flow, "T002")
    assert 'data-refs="T005"' in _node(flow, "T006")
    # Archived tasks joined to a board task by a reference, either way, are in the default
    # layout, like those joined by a dependency or a follow-up; an unrelated one is not.
    for tid in ("T001", "T002", "T006"):
        assert "no-live" not in _node(flow, tid), tid
    assert "no-live" in _node(flow, "T003")
    # A reference puts the referencing task left of the one it references (revision 3).
    gc = {
        tid: int(re.search(r"--gc:(\d+)", _node(flow, tid)).group(1))  # type: ignore[union-attr]
        for tid in ("T001", "T002", "T004", "T005", "T006")
    }
    assert gc["T006"] < gc["T005"] < gc["T004"] and gc["T005"] < gc["T001"]
    assert gc["T002"] < gc["T004"]
    # The legend tells the three lines apart; the script draws and highlights references.
    assert "A 依赖 B" in flow and "follow-up 转来" in flow and "A 引用 B" in flow
    assert 'class="lg ref"' in flow
    assert "ids(n.dataset.refs)" in SCRIPT and 'edge(n, to, "ref")' in SCRIPT
    assert ".edge.ref .line{stroke-dasharray" in CSS and ".lg.ref{" in CSS


# ---------------------------------------------------------------------- the task view


@mark.use_case("UC-DIRECT-005")
def test_the_task_view_lists_references_both_ways(conn: sqlite3.Connection) -> None:
    _referring_board(conn)
    page = render_task(conn, "T005", APP, [])
    assert page is not None
    relations = _section(page, "relations")
    assert "<h2>关系与资源</h2>" in relations
    cited = relations.split("<h3>本任务引用的任务</h3>")[1].split("<h3>")[0]
    assert '<a href="T001">T001</a>' in cited and "推荐读取集合（a1）、目标" in cited
    assert '<a href="T004">T004</a>' in cited and "已归档" in cited
    citing = relations.split("<h3>引用本任务的任务</h3>")[1].split("<h3>")[0]
    assert '<a href="T006">T006</a>' in citing and "<td>目标</td>" in citing
    assert "T002" not in citing
    page = render_task(conn, "T003", APP, [])
    assert page is not None
    assert _section(page, "relations").count('<p class="none">无</p>') >= 4


def _card(**over: Any) -> dict[str, Any]:
    card: dict[str, Any] = {
        "task_id": "T005",
        "title": "old title",
        "repo": "demo",
        "project_ids": ["demo"],
        "kind": "code",
        "status": "in_progress",
        "revision": 1,
        "keywords": ["任务流", "引用关系"],
        "keyword_source": "recorded",
        "summary": "画出引用 <b>线</b>",
        "summary_source": "recorded",
        "tags": ["tsw dashboard"],
        "outcomes": [
            {"type": "pr", "ref": "https://example.test/pull/41", "label": "PR41 · merged"}
        ],
        "dependencies": ["T004"],
        "hard_dependencies": [],
        "card_revision": {"task_revision": 1, "outputs": "abc123", "stale": False},
    }
    card.update(over)
    return card


@mark.use_case("UC-DIRECT-005")
def test_the_task_view_shows_the_index_card_when_it_is_current(conn: sqlite3.Connection) -> None:
    _referring_board(conn)
    found = IndexedCard(LedgerTail(40, "c40"), _card())
    state = card_state(conn, "T005", found, {40: "c40"}, task_seq=38)
    assert state is not None and state.problem is None and not state.lagging
    section = _section(render_task(conn, "T005", APP, [], state) or "", "card")
    assert "<h2>检索卡片</h2>" in section and "索引到账本序号 40" in section
    assert '<span class="chip kw">任务流</span><span class="chip kw">引用关系</span>' in section
    assert "<dd>主会话记录</dd>" in section and "画出引用 &lt;b&gt;线&lt;/b&gt;" in section
    assert "<dt>stale</dt><dd>否</dd>" in section
    assert "revision 1 · 产出摘要 abc123" in section
    assert '<a href="https://example.test/pull/41">PR41 · merged</a>' in section
    assert '<a href="T004">T004</a> <small>（推断）</small>' in section
    # The overview comes first, then the card.
    page = render_task(conn, "T005", APP, [], state) or ""
    assert page.index('id="overview"') < page.index('id="card"') < page.index('id="requirement"')


@mark.use_case("UC-DIRECT-005")
def test_a_stale_card_says_why(conn: sqlite3.Connection) -> None:
    _referring_board(conn)
    conn.execute("UPDATE tasks SET revision = 2 WHERE task_id = 'T005'")
    bound = {"task_revision": 1, "outputs": "abc", "stale": True}
    found = IndexedCard(LedgerTail(40, "c40"), _card(card_revision=bound))
    state = card_state(conn, "T005", found, {40: "c40"}, task_seq=40)
    section = _section(render_task(conn, "T005", APP, [], state) or "", "card")
    assert "是：卡片记录于 revision 1，任务现在是 revision 2" in section
    generated = _card(keyword_source="generated", card_revision=bound)
    state = card_state(conn, "T005", IndexedCard(LedgerTail(40, "c40"), generated), {40: "c40"}, 40)
    section = _section(render_task(conn, "T005", APP, [], state) or "", "card")
    assert "由标题与任务书生成的初版" in section and "等主会话记录" in section


@mark.use_case("UC-DIRECT-005")
def test_a_lagging_index_keeps_its_words_and_takes_the_projections_fields(
    conn: sqlite3.Connection,
) -> None:
    _referring_board(conn)
    bound = {"task_revision": 1, "outputs": outputs_digest([])[:12], "stale": False}
    found = IndexedCard(LedgerTail(40, "c40"), _card(card_revision=bound))
    state = card_state(conn, "T005", found, {40: "c40"}, task_seq=45)
    assert state is not None and state.lagging and state.problem is None
    assert state.card["card_revision"]["stale"] is False  # nothing it was bound to changed
    assert state.card["title"] == "title T005" and state.card["keywords"] == ["任务流", "引用关系"]
    section = _section(render_task(conn, "T005", APP, [], state) or "", "card")
    assert "索引落后" in section and "索引到账本序号 40，本任务最近记录序号 45" in section
    assert "tsw context rebuild" in section
    # The revision changed after the card was indexed: stale, even though the index said not.
    conn.execute("UPDATE tasks SET revision = 2 WHERE task_id = 'T005'")
    state = card_state(conn, "T005", found, {40: "c40"}, task_seq=45)
    assert state is not None and state.card["card_revision"]["stale"] is True
    section = _section(render_task(conn, "T005", APP, [], state) or "", "card")
    assert "是：卡片记录于 revision 1，任务现在是 revision 2" in section
    conn.execute("UPDATE tasks SET revision = 1 WHERE task_id = 'T005'")
    # So do outputs registered since.
    conn.execute(
        "INSERT INTO outputs(task_id, output_id, type, ref, delivery, evidence) "
        "VALUES('T005', 'O-1', 'pr', 'https://example.test/pull/42', 'merged', '{}')"
    )
    state = card_state(conn, "T005", found, {40: "c40"}, task_seq=45)
    assert state is not None and state.card["card_revision"]["stale"] is True
    conn.execute("DELETE FROM outputs")
    # A task the index has no card for yet is behind as well; its words are missing and
    # whether it is stale is not known.
    state = card_state(conn, "T005", IndexedCard(LedgerTail(40, "c40"), None), {40: "c40"}, 10)
    assert state is not None and state.lagging
    section = _section(render_task(conn, "T005", APP, [], state) or "", "card")
    assert "<dt>关键词来源</dt><dd>缺失</dd>" in section
    assert "<dt>stale</dt><dd>未知（索引中还没有这张卡片）</dd>" in section


@mark.use_case("UC-DIRECT-005")
@pytest.mark.parametrize(
    ("found", "checksums", "problem", "words"),
    [
        (IndexedCard(LedgerTail.empty(), None, "missing"), {}, "missing", "检索索引尚未建立"),
        (IndexedCard(LedgerTail.empty(), None, "version"), {}, "version", "其他版本"),
        (IndexedCard(LedgerTail.empty(), None, "unreadable"), {}, "unreadable", "暂时无法读取"),
        # Another history: the index is past the ledger, or the event there differs.
        (IndexedCard(LedgerTail(90, "c90"), _card()), {40: "c40"}, "foreign", "历史不符"),
        (IndexedCard(LedgerTail(40, "old"), _card()), {40: "c40"}, "foreign", "历史不符"),
    ],
)
def test_an_index_that_cannot_be_used_is_said_so_with_the_way_out(
    conn: sqlite3.Connection,
    found: IndexedCard,
    checksums: dict[int, str],
    problem: str,
    words: str,
) -> None:
    _referring_board(conn)
    state = card_state(conn, "T005", found, checksums, task_seq=40)
    assert state is not None and state.problem == problem
    # Only what the projection knows; nothing of a card from another history.
    assert state.card["keywords"] == [] and state.card["keyword_source"] == "missing"
    section = _section(render_task(conn, "T005", APP, [], state) or "", "card")
    assert words in section and "tsw context rebuild" in section
    assert "任务流" not in section


@mark.use_case("UC-DIRECT-005")
def test_no_card_for_an_unknown_task_and_none_read(conn: sqlite3.Connection) -> None:
    found = IndexedCard(LedgerTail(1, "c1"), None)
    assert card_state(conn, "T404", found, {1: "c1"}, 0) is None
    _task(conn, "T001", "intake", 1)
    assert "没有读取检索索引" in _section(render_task(conn, "T001", APP, []) or "", "card")


# ---------------------------------------------------------------------- the layout


def _t(n: int, repo: str = "a") -> FlowTask:
    return FlowTask(f"T{n:03d}", f"t{n}", repo, "code", "in_progress", n, f"2026-11-{n:02d}")


def _columns(
    tasks: list[FlowTask], deps: dict[str, list[str]], refs: dict[str, list[str]], **kw: Any
) -> dict[str, int]:
    layout = place(tasks, deps, references=refs, **kw)
    return {tid: cell[0] - FIRST_COLUMN for tid, cell in layout.cells.items()}


@mark.use_case("UC-DIRECT-005")
def test_a_referencing_task_is_left_of_what_it_references() -> None:
    # T001 (older) references T003 (newer): T003 moves right of it, like a dependency would.
    col = _columns([_t(1), _t(2), _t(3)], {}, {"T001": ["T003"], "T003": ["T002"]})
    assert col["T001"] < col["T003"] < col["T002"]
    # Across repositories as well, and together with dependencies.
    col = _columns(
        [_t(1, "x"), _t(2, "y"), _t(3, "x"), _t(4, "y")],
        {"T004": ["T003"]},
        {"T003": ["T002"], "T002": ["T001"]},
    )
    assert col["T004"] < col["T003"] < col["T002"] < col["T001"]


@mark.use_case("UC-DIRECT-005")
def test_only_tasks_referencing_each_other_share_a_column() -> None:
    tasks = [_t(n) for n in range(1, 6)]
    # T005 <-> T004 mutual; T004 -> T003 one way; T002 <-> T001 mutual and T003 -> T002.
    refs = {
        "T005": ["T004"],
        "T004": ["T005", "T003"],
        "T003": ["T002"],
        "T002": ["T001"],
        "T001": ["T002"],
    }
    col = _columns(tasks, {}, refs)
    assert col["T005"] == col["T004"] < col["T003"] < col["T002"] == col["T001"]
    # A chain of mutual references is one group: one column.
    col = _columns(tasks[:3], {}, {"T001": ["T002"], "T002": ["T001", "T003"], "T003": ["T002"]})
    assert col["T001"] == col["T002"] == col["T003"]
    # A one-way reference alone never shares a column, even between unrelated neighbours.
    col = _columns(tasks[:2], {}, {"T002": ["T001"]})
    assert col["T002"] < col["T001"]


@mark.use_case("UC-DIRECT-005")
def test_a_group_moves_together_and_never_splits() -> None:
    # The group's member T001 must be right of T003 (which references it): T002 follows it.
    col = _columns(
        [_t(1), _t(2), _t(3)], {}, {"T003": ["T001"], "T001": ["T002"], "T002": ["T001"]}
    )
    assert col["T003"] < col["T001"] == col["T002"]
    # Seven tasks of one repository all referencing each other: one column, a taller lane.
    tasks = [_t(n) for n in range(1, 8)]
    everyone = {t.task_id: [o.task_id for o in tasks if o is not t] for t in tasks}
    layout = place(tasks, {}, references=everyone)
    assert len({c for c, _ in layout.cells.values()}) == 1
    assert len({r for _, r in layout.cells.values()}) == 7
    # A task placed after it in the same column goes right: that lane is full there.
    col = _columns([*tasks, _t(8, "a")], {}, everyone)
    assert col["T008"] < col["T001"]
    col = _columns([_t(0, "a"), *tasks], {}, everyone)
    assert col["T000"] > col["T001"]
    # Members in other lanes keep the group's column when theirs has room.
    tasks = [_t(1, "x"), _t(2, "y"), _t(3, "x")]
    col = _columns(tasks, {}, {"T001": ["T002"], "T002": ["T001"]}, capacity=1)
    assert col["T001"] == col["T002"]


@mark.use_case("UC-DIRECT-005")
def test_dependencies_win_over_references() -> None:
    # T003 depends on T001, and T003 <-> T002 <-> T001 reference each other: T003 stays left
    # of T001, so it does not join the group of T001 and T002.
    tasks = [_t(1), _t(2), _t(3)]
    mutual = {"T003": ["T002"], "T002": ["T003", "T001"], "T001": ["T002"]}
    col = _columns(tasks, {"T003": ["T001"]}, mutual)
    assert col["T003"] < col["T001"] == col["T002"]
    # A cycle of a dependency and references gives up a reference, never the dependency.
    col = _columns(tasks, {"T001": ["T003"]}, {"T003": ["T002"], "T002": ["T001"]})
    assert col["T001"] < col["T003"]
    # A dependency through a task outside the pair keeps the pair apart too.
    col = _columns(
        [_t(1), _t(2), _t(3)],
        {"T002": ["T003"], "T003": ["T001"]},
        {"T001": ["T002"], "T002": ["T001"]},
    )
    assert col["T002"] < col["T003"] < col["T001"]


@mark.use_case("UC-DIRECT-005")
def test_a_reference_cycle_is_broken_by_registration_order() -> None:
    # T001 -> T002 -> T003 -> T001 (none mutual): every task placed, the newest leftmost.
    col = _columns(
        [_t(1), _t(2), _t(3)], {}, {"T001": ["T002"], "T002": ["T003"], "T003": ["T001"]}
    )
    assert set(col) == {"T001", "T002", "T003"}
    assert col["T003"] < col["T001"] < col["T002"]
    # The same column arrows go round the side, so the script checks for it.
    assert "const sameColumn =" in SCRIPT
    # Same-column arrows bulge no further than the nodes' side margin (14px).
    assert "c1 = x1 - sign * 12;" in SCRIPT and "margin:7px 14px" in CSS


@mark.use_case("UC-DIRECT-005")
def test_a_large_unrelated_history_is_laid_out_in_order() -> None:
    # Ordering uses heaps: thousands of tasks, with and without relations, place quickly and
    # newest first.
    tasks = [
        FlowTask(f"T{n:05d}", "t", f"r{n % 7}", "code", "accepted", n, "x") for n in range(4000)
    ]
    refs = {f"T{n:05d}": [f"T{n - 3:05d}"] for n in range(3, 4000, 5)}
    col = _columns(tasks, {}, refs)
    assert len(col) == 4000
    assert all(col[f"T{n:05d}"] < col[f"T{n - 3:05d}"] for n in range(3, 4000, 5))


# ---------------------------------------------------------------------- tracks (revision 4)


def _cells_of_tracks(layout: Any) -> set[tuple[str, int, int]]:
    """(lane, grid column, grid row) of every cell a track holds."""
    return {(tr.repo, col, tr.row) for tr in layout.tracks for col in range(tr.first, tr.end)}


def _card_cells(layout: Any, tasks: list[FlowTask]) -> set[tuple[str, int, int]]:
    repo = {t.task_id: t.repo for t in tasks}
    return {(repo[tid], col, row) for tid, (col, row) in layout.cells.items()}


@mark.use_case("UC-DIRECT-005")
def test_a_line_spanning_columns_runs_on_a_row_no_card_holds() -> None:
    # T003 references T001 two columns to its right; T002 sits between them.
    tasks = [_t(1), _t(2), _t(3)]
    layout = place(tasks, {}, references={"T003": ["T002", "T001"], "T002": ["T001"]})
    col = {tid: c - FIRST_COLUMN for tid, (c, _) in layout.cells.items()}
    assert col == {"T003": 0, "T002": 1, "T001": 2}
    [track] = layout.tracks
    assert (track.source, track.first - FIRST_COLUMN, track.end - FIRST_COLUMN) == ("T003", 1, 2)
    # The card in the column it passes gives way: the track holds the top row there.
    assert layout.cells["T002"][1] != track.row
    assert not _cells_of_tracks(layout) & _card_cells(layout, tasks)
    # Neighbouring columns need no track; the lane is as tall as cards and tracks need.
    assert place(tasks[:2], {}, references={"T002": ["T001"]}).tracks == []
    lane = layout.lanes[0]
    assert lane.end_row - lane.first_row == 2


@mark.use_case("UC-DIRECT-005")
def test_the_screenshot_lane_keeps_every_line_off_the_cards() -> None:
    # T279–T272 as on the user's Dashboard (2026-11-14), with T274 in another lane and an older
    # task T262 to the right that T278 and T277 reference.
    ids = [279, 278, 277, 276, 275, 274, 273, 272, 262]
    tasks = [
        FlowTask(
            f"T{n}", "t", "workdeck" if n == 274 else "task-steward", "code", "accepted", n, "x"
        )
        for n in ids
    ]
    refs = {
        "T279": ["T278", "T276", "T275"],
        "T278": ["T277", "T276", "T275", "T273", "T272", "T262"],
        "T277": ["T276", "T262"],
        "T276": ["T275", "T272", "T274"],
        "T275": ["T273", "T272", "T274"],
        "T273": ["T272"],
    }
    layout = place(tasks, {"T272": ["T262"]}, references=refs)
    assert not _cells_of_tracks(layout) & _card_cells(layout, tasks)
    sources = {tr.source for tr in layout.tracks}
    assert {"T279", "T278", "T277", "T276", "T275"} <= sources and "T273" not in sources
    # A track ends before the column of its farthest target and starts right of its task.
    col = {tid: c for tid, (c, _) in layout.cells.items()}
    for tr in layout.tracks:
        assert tr.first == col[tr.source] + 1 and tr.repo == "task-steward"
    t278 = next(tr for tr in layout.tracks if tr.source == "T278")
    assert t278.end == col["T262"]
    # Follow-ups take a track too (they do not move cards; the references set the columns).
    chain = {"T279": ["T278"], "T278": ["T277"]}
    layout = place(tasks[:3], {}, references=chain, follow_ups={"T279": ["T277"]})
    assert [tr.source for tr in layout.tracks] == ["T279"]


@mark.use_case("UC-DIRECT-005")
def test_the_page_carries_the_tracks_of_both_layouts_and_the_script_follows_them(
    conn: sqlite3.Connection,
) -> None:
    _referring_board(conn)
    # T006 references T005 next to it and T004 two columns away: a track over T005's column.
    conn.execute("UPDATE tasks SET goal = '后续见 T005 与 T004' WHERE task_id = 'T006'")
    flow = render(conn).split('id="flow"')[1]
    tracks = re.findall(
        r'<div class="ftrack" data-src="(\w+)" data-layout="(\w+)" data-dir="\w+" '
        r'style="([^"]*)" hidden>',
        flow,
    )
    assert {(src, name) for src, name, _ in tracks} >= {("T006", "live"), ("T006", "all")}
    for _, _, style in tracks:
        assert re.fullmatch(r"--c0:\d+;--c1:\d+;--r:\d+", style)
    assert "function routed(from, to, kind, track, y1, y2, sign)" in SCRIPT
    assert 'data-dir="on"' in flow
    assert "t.dataset.layout !== layout || !shown.has(nodeById.get(t.dataset.src))" in SCRIPT
    assert re.search(r"\.ftrack\{grid-column:var\(--c0\)/var\(--c1\);grid-row:var\(--r\)", CSS)


@mark.use_case("UC-DIRECT-005")
def test_lines_running_left_across_columns_get_a_track_too() -> None:
    # A reference cycle T004 -> T003 -> T002 -> T001 -> T004: one reference is given up and
    # runs left across the two columns between.
    tasks = [_t(1), _t(2), _t(3), _t(4)]
    cycle = {"T004": ["T003"], "T003": ["T002"], "T002": ["T001"], "T001": ["T004"]}
    layout = place(tasks, {}, references=cycle)
    back = [tr for tr in layout.tracks if tr.back]
    assert len(back) == 1
    col = {tid: c for tid, (c, _) in layout.cells.items()}
    (tr,) = back
    target = next(t for t in cycle[tr.source] if col[t] < col[tr.source])
    assert (tr.first, tr.end) == (col[target] + 1, col[tr.source])
    assert not _cells_of_tracks(layout) & _card_cells(layout, tasks)
    # A follow-up to a newer task two columns left of it.
    chain = {"T003": ["T002"], "T002": ["T001"]}
    layout = place(tasks[:3], {}, references=chain, follow_ups={"T001": ["T003"]})
    assert [(tr.source, tr.back) for tr in layout.tracks] == [("T001", True)]
    assert not _cells_of_tracks(layout) & _card_cells(layout, tasks[:3])


@mark.use_case("UC-DIRECT-005")
def test_a_long_sparse_chain_is_laid_out_in_one_pass() -> None:
    # Rows come from per-column buckets: 8000 tasks in a chain of references place quickly.
    tasks = [FlowTask(f"T{n:05d}", "t", "r", "code", "accepted", n, "x") for n in range(8000)]
    chain = {f"T{n:05d}": [f"T{n - 1:05d}"] for n in range(1, 8000)}
    layout = place(tasks, {}, references=chain)
    assert len(layout.cells) == 8000 and layout.tracks == []


@mark.use_case("UC-DIRECT-005")
def test_long_tracks_are_held_as_intervals() -> None:
    # Each task depends on the one before and references the oldest: the tracks are long, yet
    # each is held once (an interval per row), and no card shares a cell with a track.
    n = 300
    tasks = [FlowTask(f"T{k:04d}", "t", "r", "code", "accepted", k, "x") for k in range(n)]
    deps = {f"T{k:04d}": [f"T{k - 1:04d}"] for k in range(1, n)}
    refs = {f"T{k:04d}": ["T0000"] for k in range(2, n)}
    layout = place(tasks, deps, references=refs)
    assert len(layout.tracks) == n - 2
    col = {tid: c for tid, (c, _) in layout.cells.items()}
    assert all(tr.end == col["T0000"] for tr in layout.tracks)
    cards = {(c, r) for c, r in layout.cells.values()}
    for tr in layout.tracks:
        assert not any((k, tr.row) in cards for k in range(tr.first, tr.end))
    rows = {}
    for tr in layout.tracks:  # tracks sharing a row never overlap
        for other in rows.get(tr.row, []):
            assert tr.end <= other[0] or other[1] <= tr.first
        rows.setdefault(tr.row, []).append((tr.first, tr.end))
