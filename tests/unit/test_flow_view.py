"""The Dashboard's task flow and count tiles (UC-DIRECT-004, DN006 §8.10, T273)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tests.support import flow_layouts
from tsw.host.flow import CARD_W, layout_key, relation_choices
from tsw.host.status import TITLE, AppView, render
from tsw.host.status_assets import CSS, SCRIPT
from tsw.projection import SCHEMA_SQL

mark = pytest.mark

# The layout itself (dependencies first, newest left, ports and channels) is tested in
# test_flow_layout.py; this file tests the page that carries it.


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
    # The nodes carry no position: the page's layouts do, one per choice the page offers.
    assert "style=" not in t4
    layouts = flow_layouts(page)
    live, every = layouts["live:dep,fu,ref"], layouts["all:dep,fu,ref"]
    # The board's tasks and the archived one they depend on are in the default layout ...
    assert set(live["cells"]) == {"T001", "T003", "T004"}
    # ... an unrelated archived task only in the layout with every task.
    assert set(every["cells"]) == {"T001", "T002", "T003", "T004"}
    # Dependencies are right of their dependents, across repositories too (T304).
    gc = {tid: live["cells"][tid][0] for tid in ("T001", "T003", "T004")}
    assert gc["T004"] < gc["T003"] < gc["T001"]
    # Each card shows its latest ledger record (the time is written by the script).
    assert 'data-when="2026-11-01T08:00:00Z"' in t4 and '<span class="ftime"></span>' in flow
    # The lines run from the task depended on to the task depending on it, ending at its right.
    lines = {(src, dst): pts for kind, src, dst, pts in live["lines"] if kind == "dep"}
    assert set(lines) == {("T001", "T003"), ("T003", "T004")}
    x4 = live["xs"][live["cells"]["T004"][0]]
    assert lines[("T003", "T004")][-1][0] == x4 + CARD_W
    # The static page links a board task to its card; an archived one has no card to go to.
    assert t4.startswith("<a ") and 'href="#task-T004"' in t4
    assert _node(flow, "T001").startswith("<span ")
    # Lanes: one per repository, alpha (no newer activity) after beta by name.
    assert re.findall(r'<div class="lane" data-repo="(\w+)"', flow) == ["alpha", "beta"]
    assert set(live["lanes"]) == set(every["lanes"]) == {"alpha", "beta"}
    assert re.findall(r'<div class="lane-name" data-repo="(\w+)"', flow) == ["alpha", "beta"]
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
        assert re.search(
            rf'<button[^>]*id="{view}"[^>]*><span class="long">{label}</span>', tools
        ), view
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
    for absent in ("运行中", "占用名额", "待启动", "待判断", "待用户答复", "follow-up", "暂停"):
        assert absent not in tiles, absent
    # Running tasks and tasks with open follow-ups are quick picks (revision 3), in the
    # filter panel's 「快捷」 after 「只看待用户处理」 (T353).
    picks = page.split("<h5>快捷</h5>")[1].split("</div>")[0]
    assert re.search(
        r'id="mine"[^>]*>只看待用户处理</button><button[^>]*data-quick="running" data-n="1" '
        r'aria-pressed="false"[^>]*>运行中<small>1</small>',
        picks,
    )
    assert re.search(r'data-quick="followups" data-n="0"[^>]*hidden>follow-up', picks)
    running = re.search(r'data-running="([^"]+)"', page)
    assert running and set(running.group(1).split()) == {
        "dispatching",
        "in_progress",
        "waiting_user",
        "verifying",
    }
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


@mark.use_case("UC-DIRECT-004")
def test_archived_only_repositories_and_kinds_can_be_chosen_in_the_task_flow(
    conn: sqlite3.Connection,
) -> None:
    _task(conn, "T001", "archived", 1, repo="gone", kind="research")
    _task(conn, "T002", "in_progress", 2, repo="live")
    page = render(conn)
    # Offered but hidden on the board; the script shows them with the archived tasks (data-na).
    assert '<option value="gone" data-n="0" data-na="1" hidden>gone</option>' in page
    assert '<option value="live" data-n="1" data-na="1">live</option>' in page
    assert re.search(r'data-kind="research"[^>]*data-n="0" data-na="1"[^>]*hidden', page)
    assert "flowing && state.arch ? el.dataset.na : el.dataset.n" in SCRIPT


def test_the_lane_names_stick_to_the_scrolling_area() -> None:
    # overflow:hidden on the box inside the scroller would capture the sticky lane names.
    assert ".flow-size{position:relative;overflow:clip}" in CSS
    assert re.search(r"\.flow-names\{position:sticky;left:0", CSS)


# ---------------------------------------------------------------------- the choices (T280, T304)


@mark.use_case("UC-DIRECT-004")
def test_the_page_carries_every_layout_once_and_the_legend_ticks_the_relations(
    conn: sqlite3.Connection,
) -> None:
    _board(conn)  # dependencies and a follow-up, no reference
    page = render(conn)
    layouts = flow_layouts(page)
    assert set(layouts) == {
        layout_key(everything, c) for everything in (False, True) for c in relation_choices()
    }
    # Dependencies are always laid out; the references never move a card, so with none on the
    # board, showing them or not gives the same layout: once.
    assert all(":dep" in key for key in layouts)
    assert layouts["live:dep,fu,ref"]["set"] == layouts["live:dep,fu"]["set"]
    assert layouts["live:dep,fu,ref"]["set"] != layouts["live:dep"]["set"]
    assert "T001" in layouts["live:dep"]["cells"]  # the archived task depended on: context
    legend = page.split('id="flow-legend"')[1].split('id="flow-layouts"')[0]
    # No checkbox for the dependencies; the follow-ups ticked, the references not (T304).
    assert re.findall(r'<input type="checkbox" data-rel="(\w+)"( checked)?>', legend) == [
        ("fu", " checked"),
        ("ref", ""),
    ]
    assert "A 依赖 B" in legend and "与筛选结果直接相连的上下文" in legend
    # Polyline or curve, polyline by default.
    bar = page.split('class="flow-bar"')[1].split('id="flow-legend"')[0]
    assert re.findall(r'name="flow-shape" value="(\w+)"( checked)?', bar) == [
        ("poly", " checked"),
        ("curve", ""),
    ]
    # The choices are kept with the filters, so a reload keeps them; reset and tiles too.
    assert 'if (s.hide.length) p.set("hide", s.hide.join(","));' in SCRIPT
    assert 'if (s.refs) p.set("refs", "1");' in SCRIPT
    assert 'if (s.shape === "curve") p.set("line", "curve");' in SCRIPT
    assert 'hide: list("hide").filter(r => r === "fu")' in SCRIPT  # an old hide=dep is ignored
    assert SCRIPT.count("hide: state.hide, refs: state.refs, shape: state.shape") == 3
    assert 'layouts.pick[(state.arch ? "all" : "live") + ":" + showing().join(",")]' in SCRIPT
    assert ".flow-canvas>.out{display:none}" in CSS


@mark.use_case("UC-DIRECT-004")
def test_the_layout_data_cannot_end_its_block(conn: sqlite3.Connection) -> None:
    _task(conn, "T001", "in_progress", 1, repo="</script><b>x</b>")
    page = render(conn)
    block = page.split('id="flow-layouts">')[1].split("</script>")[0]
    assert "<" not in block and ">" not in block
    assert list(flow_layouts(page)["live:dep,fu,ref"]["lanes"]) == ["</script><b>x</b>"]


@mark.use_case("UC-DIRECT-011")
def test_the_page_layout_header_filters_and_flow_toolbar(conn: sqlite3.Connection) -> None:
    _board(conn)
    page = render(conn, AppView(token="t", actions={}))
    # The header: the ledger line and the buttons in one block that 「⋯」 opens on a narrow
    # screen; 「⋯」 needs the script, so it starts hidden.
    hero = page.split('<header class="hero board-hero" id="hero">')[1].split("</header>")[0]
    assert re.search(r'id="hero-more" hidden aria-expanded="false" aria-controls="hero-info"', hero)
    info = hero.split('id="hero-info">')[1]
    assert "账本序号" in info and 'id="prefs"' in info and 'id="theme"' in info
    # The filters: one row (search, 「筛选」, views, count), the chosen chips, the panel.
    tools = page.split('id="tools"')[1].split("</section>")[0]
    bar = tools.split('<div class="fbar">')[1].split("</div>")[0]
    for control in ('id="q"', 'id="filter-open"', 'id="view-flow"', 'id="count"'):
        assert control in bar, control
    assert '<div class="chosen" id="chosen"' in tools
    panel = tools.split('id="filter-pop" role="dialog" aria-label="筛选" hidden>')[1]
    for control in (
        'id="mine"',
        'data-quick="running"',
        "data-st=",
        "data-kind=",
        'id="repo"',
        'id="flag"',
        'id="sort"',
        'id="group"',
        'id="clear"',
        'id="filter-done"',
    ):
        assert control in panel, control
    assert '<div class="sec board-only"><h5>看板显示</h5>' in panel
    # The flow toolbar: one row; the legend and the relation choices in their pop-up.
    flow_bar = page.split('<div class="flow-bar" id="flow-bar">')[1].split("</div>")[0]
    for control in (
        'id="flow-archived"',
        'name="flow-shape"',
        'id="zoom-out"',
        'id="zoom-fit"',
        'id="legend-open"',
    ):
        assert control in flow_bar, control
    assert 'id="flow-legend"' not in flow_bar
    legend = page.split('id="legend-pop" role="dialog" aria-label="图例与关系" hidden>')[1]
    assert legend.startswith('<span class="legend" id="flow-legend">')
    # The task flow's area sits in its slot; the script sizes both.
    assert (
        '<div class="flow-slot" id="flow-slot"><div class="flow-scroll" id="flow-scroll">' in page
    )
    assert ".flow-slot{min-width:0;height:100vh}" in CSS and "max-height:78vh" not in CSS
    assert "function fit()" in SCRIPT and "draggable(scroller, true)" in SCRIPT
    assert 'tools.classList.contains("open")' in SCRIPT  # the open panel holds the refresh
