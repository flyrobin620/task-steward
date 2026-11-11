"""The Dashboard's task flow and count tiles (UC-DIRECT-004, DN006 §8.10, T273)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.host.flow import FIRST_COLUMN, FIRST_ROW, LANE_CAPACITY, FlowTask, lane_order, place
from tsw.host.status import TITLE, AppView, render
from tsw.host.status_assets import SCRIPT
from tsw.projection import SCHEMA_SQL

mark = pytest.mark


def task(n: int, repo: str = "a", status: str = "in_progress", activity: str = "") -> FlowTask:
    return FlowTask(
        f"T{n:03d}", f"title {n}", repo, "code", status, n, activity or f"2026-11-{n:02d}"
    )


def columns(tasks: list[FlowTask], deps: dict[str, list[str]], **kw: Any) -> dict[str, int]:
    layout = place(tasks, deps, **kw)
    return {tid: cell[0] - FIRST_COLUMN for tid, cell in layout.cells.items()}


# ---------------------------------------------------------------------- the layout


@mark.use_case("UC-DIRECT-004")
def test_unrelated_tasks_share_the_first_column_newest_on_top() -> None:
    layout = place([task(1), task(2), task(3)], {})
    assert {c for c, _ in layout.cells.values()} == {FIRST_COLUMN}
    rows = {tid: row for tid, (_, row) in layout.cells.items()}
    assert rows["T003"] < rows["T002"] < rows["T001"]
    assert layout.columns == 1


@mark.use_case("UC-DIRECT-004")
def test_a_dependency_is_right_of_every_task_depending_on_it() -> None:
    tasks = [task(n) for n in range(1, 7)]
    deps = {"T006": ["T004"], "T005": ["T004"], "T004": ["T001"], "T003": ["T002"]}
    col = columns(tasks, deps)
    for dependent, ds in deps.items():
        for d in ds:
            assert col[d] > col[dependent], (dependent, d)
    # Newest left: an older task is never left of a newer one (the column only moves right).
    ordered = [col[f"T{n:03d}"] for n in range(6, 0, -1)]
    assert ordered == sorted(ordered)


@mark.use_case("UC-DIRECT-004")
def test_a_dependency_registered_after_its_dependent_still_goes_right() -> None:
    # Imported records can name a dependency with a later registration.
    col = columns([task(1), task(2), task(3)], {"T001": ["T003"]})
    assert col["T003"] > col["T001"]


@mark.use_case("UC-DIRECT-004")
def test_a_lane_holds_a_bounded_number_of_tasks_per_column() -> None:
    tasks = [task(n) for n in range(1, 2 * LANE_CAPACITY + 2)]
    layout = place(tasks, {})
    per_column: dict[int, int] = {}
    for c, _ in layout.cells.values():
        per_column[c] = per_column.get(c, 0) + 1
    assert max(per_column.values()) == LANE_CAPACITY
    assert layout.columns == 3
    # Another repository is not pushed by a full lane: it shares the columns.
    mixed = columns([task(1, "a"), task(2, "b"), task(3, "a")], {}, capacity=1)
    assert mixed["T002"] == 0 and mixed["T003"] == 0 and mixed["T001"] == 1


@mark.use_case("UC-DIRECT-004")
def test_lanes_follow_the_repositories_latest_activity_and_cross_lane_edges_keep_order() -> None:
    tasks = [
        task(1, "old", activity="2026-11-01"),
        task(2, "new", activity="2026-11-09"),
        task(3, "mid", activity="2026-11-05"),
        task(4, "old", activity="2026-11-02"),
    ]
    assert lane_order(tasks) == ["new", "mid", "old"]
    layout = place(tasks, {"T002": ["T001"]})
    assert [(lane.repo, lane.first_row) for lane in layout.lanes] == [
        ("new", FIRST_ROW),
        ("mid", FIRST_ROW + 1),
        ("old", FIRST_ROW + 2),
    ]
    # T001 goes right of T002 in another lane, so it shares its lane's row with T004.
    assert layout.lanes[-1].end_row == FIRST_ROW + 3
    assert layout.cells["T001"][0] > layout.cells["T002"][0]
    assert layout.cells["T001"][1] == layout.cells["T004"][1]


@mark.use_case("UC-DIRECT-004")
def test_a_task_takes_the_row_of_the_tasks_depending_on_it() -> None:
    tasks = [task(1), task(2), task(3), task(4)]
    # T004 is alone at the top of column 0; T003 and T002 sit below it; T001 follows T002.
    layout = place(tasks, {"T002": ["T001"]})
    assert layout.cells["T001"][1] == layout.cells["T002"][1]


@mark.use_case("UC-DIRECT-004")
def test_a_cycle_and_unknown_tasks_do_not_stop_the_layout() -> None:
    layout = place([task(1), task(2)], {"T001": ["T002", "T404"], "T002": ["T001"]})
    assert set(layout.cells) == {"T001", "T002"}
    assert place([], {}).columns == 0


# ---------------------------------------------------------------------- the page


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


def _style(node: str) -> dict[str, int]:
    style = re.search(r'style="([^"]*)"', node)
    assert style
    return {k: int(v) for k, v in re.findall(r"--(\w+):(\d+)", style.group(1))}


def _board(db: sqlite3.Connection) -> None:
    _task(db, "T001", "archived", 1, repo="alpha")  # depended on by a board task: context
    _task(db, "T002", "archived", 2, repo="beta")  # unrelated: only with the archived ones
    _task(db, "T003", "accepted", 3, repo="alpha")
    _task(db, "T004", "in_progress", 4, repo="beta", title="<b>flow</b>")
    _dep(db, "T003", "T001")
    _dep(db, "T004", "T003")  # across repositories
    db.execute(
        "INSERT INTO follow_ups(follow_up_id, task_id, revision, kind, title, detail, status, "
        "recorded_seq, recorded_at, recorded_by, converted_to) VALUES('F-T003-1', 'T003', 1, "
        "'remaining', 'x', 'x', 'converted', 1, '2026-11-01T00:00:00Z', 'worker', 'T004')"
    )


@mark.use_case("UC-DIRECT-004")
def test_the_flow_has_a_node_per_task_with_its_relations_and_two_layouts(
    conn: sqlite3.Connection,
) -> None:
    _board(conn)
    page = render(conn)
    flow = page.split('id="flow"')[1]
    assert re.search(r'<section class="flow" id="flow" hidden', page)
    t4 = _node(flow, "T004")
    assert 'data-deps="T003"' in t4 and 'data-from="T003"' in t4 and 'data-repo="beta"' in t4
    assert "&lt;b&gt;flow&lt;/b&gt;" in flow and "<b>flow</b>" not in flow
    # The board's tasks and the archived one they depend on are in the default layout ...
    for tid in ("T001", "T003", "T004"):
        assert "no-live" not in _node(flow, tid), tid
        assert {"gc", "gr", "agc", "agr"} <= set(_style(_node(flow, tid))), tid
    # ... an unrelated archived task only in the layout with every task.
    t2 = _node(flow, "T002")
    assert "no-live" in t2 and set(_style(t2)) == {"agc", "agr"}
    # Dependencies are right of their dependents, across repositories too.
    gc = {tid: _style(_node(flow, tid))["gc"] for tid in ("T001", "T003", "T004")}
    assert gc["T004"] < gc["T003"] < gc["T001"]
    # The static page links a board task to its card; an archived one has no card to go to.
    assert t4.startswith("<a ") and 'href="#task-T004"' in t4
    assert _node(flow, "T001").startswith("<span ")
    # Lanes: one per repository, alpha (no newer activity) after beta by name.
    assert re.findall(r'<div class="lane(?: no-live)?" data-repo="(\w+)"', flow) == [
        "alpha",
        "beta",
    ]
    assert "A 依赖 B" in flow and "follow-up 转来" in flow


@mark.use_case("UC-DIRECT-004")
def test_in_the_app_every_node_opens_its_task_view(conn: sqlite3.Connection) -> None:
    _board(conn)
    page = render(conn, AppView(token="t", actions={}))
    for tid in ("T001", "T002", "T003", "T004"):
        assert f'href="tasks/{tid}"' in _node(page, tid), tid


@mark.use_case("UC-DIRECT-004")
def test_the_dashboard_name_and_the_three_views(conn: sqlite3.Connection) -> None:
    _board(conn)
    page = render(conn)
    assert TITLE == "TaskSteward Dashboard"
    assert f"<title>{TITLE}</title>" in page and f"<h1>{TITLE}</h1>" in page
    tools = page.split('id="tools"')[1].split("</section>")[0]
    for view, label in (
        ("view-status", "按状态"),
        ("timeline", "按时间顺序"),
        ("view-flow", "任务流"),
    ):
        assert re.search(rf'<button[^>]*id="{view}"[^>]*>{label}</button>', tools), view
    # The task flow is a view of the page's state: it is kept with the filters.
    assert 'p.set("view", s.view)' in SCRIPT and 'p.set("arch", "1")' in SCRIPT


@mark.use_case("UC-DIRECT-004")
def test_tiles_show_only_nonzero_counts_and_every_status_on_the_board(
    conn: sqlite3.Connection,
) -> None:
    for n in range(1, 4):
        _task(conn, f"T{n:03d}", "accepted", n)
    _task(conn, "T004", "in_progress", 4)
    _task(conn, "T005", "done", 5)
    _task(conn, "T006", "archived", 6)
    page = render(conn)
    tiles = page.split('<section class="tiles">')[1].split("</section>")[0]
    shown = re.findall(r"<b>(\d+)</b><span>([^<]+)</span>", tiles)
    assert ("0", "待启动") not in shown
    assert all(n != "0" for n, _ in shown)
    assert ("3", "已验收") in shown and ("1", "执行中") in shown
    assert ("1", "完成·待用户验收") in shown and ("1", "已归档") in shown
    assert ("1", "占用名额") in shown
    for absent in ("待启动", "待判断", "待用户答复", "待处理 follow-up", "暂停"):
        assert absent not in tiles, absent
    # The tiles, the bar and the picks count the same statuses.
    bar = re.findall(r'<i class="st-(\w+)" style="flex:(\d+)"', page)
    states = tiles.split('aria-label="状态"')[1]
    tile_counts = re.findall(r'data-tile-st="(\w+)"[^>]*><b>(\d+)</b>', states)
    assert tile_counts == bar == [("in_progress", "1"), ("done", "1"), ("accepted", "3")]
    assert 'data-tile-archived="1"' in states
    # A status with no task keeps a hidden pick, so a chosen filter survives a reload.
    assert re.search(r'data-st="paused"[^>]*data-n="0"[^>]*hidden', page)
    assert not re.search(r'data-st="accepted"[^>]*hidden', page)


@mark.use_case("UC-DIRECT-004")
def test_a_reload_keeps_the_choices_and_does_not_follow_the_anchor_again() -> None:
    # The automatic refresh marks its reload; a reload neither follows a task anchor again nor
    # drops a filter that has no task at the moment (the script keeps it chosen).
    assert 'session.set("reload", "1")' in SCRIPT
    assert 'navigation?.type === "reload"' in SCRIPT
    assert "} else if (anchor) reveal(anchor);" in SCRIPT
    assert "repo.add(new Option(state.repo, state.repo))" in SCRIPT
    assert 'session.set("flow-scroll", flow.scroll())' in SCRIPT
