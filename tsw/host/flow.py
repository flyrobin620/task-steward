"""The task flow view of the Dashboard: tasks laid out by their relations (DN006 §8.10, T273).

Every task is a node; a dependency is an arrow from the task that depends to the one it depends
on, a task made from a follow-up points, dashed, to the task the follow-up came from, and a task
points, dotted, to the tasks it references (:mod:`tsw.host.relations`, T278).
Horizontally the newest registered task is on the left and a task's dependencies are always to
its right; vertically each repository has its own lane.

The layout is computed here, when the page is generated, so it is tested like any other logic:
each node carries its grid column and row, once for the board's tasks (with the archived tasks
they are directly related to) and once for every task. The page's script only shows and hides
nodes by the filters and draws the arrows between the nodes that are shown.
"""

from __future__ import annotations

import heapq
import html
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from tsw.tasks import KINDS, STATUS_LABELS

# How many nodes of one repository share a column before the next ones move right.
LANE_CAPACITY = 6
# Grid row 1 holds the column headings and grid column 1 the lane names.
FIRST_ROW = 2
FIRST_COLUMN = 2


@dataclass(frozen=True)
class FlowTask:
    task_id: str
    title: str
    repo: str
    kind: str
    status: str
    seq: int  # registration order: the ledger sequence of the task's registration
    activity: str  # the time of the task's latest ledger record


@dataclass(frozen=True)
class Lane:
    repo: str
    first_row: int  # grid rows [first_row, end_row)
    end_row: int


def _no_cells() -> dict[str, tuple[int, int]]:
    return {}


def _no_lanes() -> list[Lane]:
    return []


@dataclass
class Layout:
    """Grid cells (column, row) of the nodes, and the rows of each repository's lane."""

    cells: dict[str, tuple[int, int]] = field(default_factory=_no_cells)
    lanes: list[Lane] = field(default_factory=_no_lanes)
    columns: int = 0


def lane_order(tasks: Iterable[FlowTask]) -> list[str]:
    """Repositories by their latest activity, most recent first (then by name)."""
    latest: dict[str, str] = {}
    for t in tasks:
        if t.activity > latest.get(t.repo, ""):
            latest[t.repo] = t.activity
        latest.setdefault(t.repo, "")
    return sorted(sorted(latest), key=lambda r: latest[r], reverse=True)


def place(
    tasks: Sequence[FlowTask],
    depends_on: Mapping[str, Iterable[str]],
    capacity: int = LANE_CAPACITY,
) -> Layout:
    """Lay the tasks out: newest left, every dependency right of the tasks depending on it.

    Tasks are taken newest registration first, but never before every task depending on them
    has been placed (a dependency registered after its dependent waits for it). Each task goes
    to the current column, or further right when a task depending on it is there or beyond, or
    when its repository already has ``capacity`` tasks in that column; the current column only
    moves right, so older tasks never come left of newer ones. Within its lane a task takes the
    free row nearest to the tasks depending on it, which keeps the arrows short and straight.
    Relations to tasks outside ``tasks`` are ignored; a cycle (which the ledger does not allow)
    is broken by registration order.
    """
    by_id = {t.task_id: t for t in tasks}
    deps: dict[str, list[str]] = {
        tid: sorted({d for d in depends_on.get(tid, ()) if d in by_id and d != tid})
        for tid in by_id
    }
    dependents: dict[str, list[str]] = {tid: [] for tid in by_id}
    for tid, ds in deps.items():
        for d in ds:
            dependents[d].append(tid)
    waiting = {tid: len(ds) for tid, ds in dependents.items()}
    ready = [(-by_id[tid].seq, tid) for tid, n in waiting.items() if n == 0]
    heapq.heapify(ready)
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(by_id):
        if not ready:  # a cycle: take the newest task left
            tid = max((t for t in by_id if t not in done), key=lambda t: (by_id[t].seq, t))
            heapq.heappush(ready, (-by_id[tid].seq, tid))
        _, tid = heapq.heappop(ready)
        if tid in done:
            continue
        done.add(tid)
        order.append(tid)
        for d in deps[tid]:
            waiting[d] -= 1
            if waiting[d] == 0 and d not in done:
                heapq.heappush(ready, (-by_id[d].seq, d))

    column: dict[str, int] = {}
    slot: dict[str, int] = {}
    used: dict[tuple[int, str], set[int]] = {}
    current = 0
    for tid in order:
        task = by_id[tid]
        placed = [m for m in dependents[tid] if m in column]
        col = max([current] + [column[m] + 1 for m in placed])
        while len(used.get((col, task.repo), ())) >= capacity:
            col += 1
        current = col
        taken = used.setdefault((col, task.repo), set())
        near = [slot[m] for m in placed if by_id[m].repo == task.repo]
        want = round(sum(near) / len(near)) if near else 0
        free = [r for r in range(capacity) if r not in taken]
        row = min(free, key=lambda r: (abs(r - want), r))
        taken.add(row)
        column[tid] = col
        slot[tid] = row

    layout = Layout(columns=(max(column.values()) + 1) if column else 0)
    row_at = FIRST_ROW
    for repo in lane_order(tasks):
        height = 1 + max(slot[t.task_id] for t in tasks if t.repo == repo)
        layout.lanes.append(Lane(repo, row_at, row_at + height))
        for t in tasks:
            if t.repo == repo:
                layout.cells[t.task_id] = (
                    FIRST_COLUMN + column[t.task_id],
                    row_at + slot[t.task_id],
                )
        row_at += height
    return layout


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def render_flow(
    tasks: Sequence[FlowTask],
    depends_on: Mapping[str, Iterable[str]],
    follow_up_of: Mapping[str, Iterable[str]],
    *,
    flags: Mapping[str, str],
    app: bool,
    on_board: frozenset[str],
    repo_hue: Callable[[str], int],
    references: Mapping[str, Iterable[str]] | None = None,
) -> str:
    """The task flow section: its controls, lanes and nodes (the script draws the arrows).

    ``depends_on`` maps a task to the tasks it depends on, ``follow_up_of`` a task to the tasks
    it was made from by a follow-up and ``references`` a task to the tasks it references. The
    three relations are alike for the context: an archived task joined to a board task by any of
    them is in the layout without the archived tasks. ``flags`` are the board card's attention
    flags, so the filters select the same tasks here as on the board.
    """
    known = {t.task_id for t in tasks}
    deps = {tid: sorted(d for d in ds if d in known) for tid, ds in depends_on.items()}
    sources = {tid: sorted(s for s in ss if s in known) for tid, ss in follow_up_of.items()}
    refs = {tid: sorted(r for r in rs if r in known) for tid, rs in (references or {}).items()}
    related: dict[str, set[str]] = {t.task_id: set() for t in tasks}
    for relation in (deps, sources, refs):
        for tid, others in relation.items():
            if tid not in related:
                continue
            for other in others:
                related[tid].add(other)
                related[other].add(tid)
    live_ids = {t.task_id for t in tasks if t.status != "archived"}
    live_ids |= {o for tid in list(live_ids) for o in related[tid]}
    live = place([t for t in tasks if t.task_id in live_ids], deps)
    every = place(tasks, deps)

    lanes: list[str] = []
    every_lanes = {lane.repo: lane for lane in every.lanes}
    live_lanes = {lane.repo: lane for lane in live.lanes}
    for repo in lane_order(tasks):
        a = every_lanes[repo]
        style = f"--h:{repo_hue(repo)};--ar0:{a.first_row};--ar1:{a.end_row}"
        lv = live_lanes.get(repo)
        if lv:
            style += f";--r0:{lv.first_row};--r1:{lv.end_row}"
        hide = "" if lv else " no-live"
        lanes.append(
            f'<div class="lane{hide}" data-repo="{_e(repo)}" style="{style}"></div>'
            f'<div class="lane-name{hide}" data-repo="{_e(repo)}" style="{style}" '
            f'title="{_e(repo)}">{_e(repo)}</div>'
        )

    nodes: list[str] = []
    for t in sorted(tasks, key=lambda t: -t.seq):
        ac, ar = every.cells[t.task_id]
        style = f"--agc:{ac};--agr:{ar}"
        cell = live.cells.get(t.task_id)
        if cell:
            style += f";--gc:{cell[0]};--gr:{cell[1]}"
        status = STATUS_LABELS.get(t.status, t.status)
        attrs = (
            f'class="fnode st-{_e(t.status)}{"" if cell else " no-live"}" '
            f'data-id="{_e(t.task_id)}" data-repo="{_e(t.repo)}" data-kind="{_e(t.kind)}" '
            f'data-status="{_e(t.status)}" data-seq="{t.seq}" '
            f'data-flags="{_e(flags.get(t.task_id, ""))}" '
            f'data-deps="{_e(" ".join(deps.get(t.task_id, [])))}" '
            f'data-from="{_e(" ".join(sources.get(t.task_id, [])))}" '
            f'data-refs="{_e(" ".join(refs.get(t.task_id, [])))}" style="{style}" '
            f'title="{_e(t.task_id)} · {_e(t.title)} · {_e(status)} · '
            f'{_e(KINDS.get(t.kind, t.kind))} · {_e(t.repo)}"'
        )
        inner = (
            f'<b class="fid">{_e(t.task_id)}</b><span class="fst">{_e(status)}</span>'
            f'<span class="ftitle">{_e(t.title)}</span>'
        )
        if app:
            href = "tasks/" + quote(t.task_id, safe="")
        elif t.task_id in on_board:
            href = "#task-" + quote(t.task_id, safe="")
        else:
            href = ""
        nodes.append(
            f'<a {attrs} href="{_e(href)}">{inner}</a>' if href else f"<span {attrs}>{inner}</span>"
        )

    arrow = (
        '<svg width="34" height="10" aria-hidden="true"><line x1="1" y1="5" x2="27" y2="5" '
        'class="lg {}"/><path d="M27 1 L33 5 L27 9 Z" class="lg-h"/></svg>'
    )
    legend = (
        f'<span class="legend">{arrow.format("dep")}A 依赖 B（A 在左，B 在右）'
        f"{arrow.format('fu')}由 B 的 follow-up 转来"
        f"{arrow.format('ref')}A 引用 B"
        '<i class="lg-ctx"></i>与筛选结果直接相连的上下文</span>'
    )
    return (
        '<section class="flow" id="flow" hidden aria-label="任务流">'
        '<div class="flow-bar">'
        '<label><input type="checkbox" id="flow-archived"> 含已归档</label>'
        '<span class="sep"></span>'
        '<button type="button" class="ghost" id="zoom-out" title="缩小">缩小</button>'
        '<button type="button" class="ghost" id="zoom-in" title="放大">放大</button>'
        '<button type="button" class="ghost" id="zoom-reset">100%</button>'
        '<button type="button" class="ghost" id="zoom-fit" title="缩放到区域宽度">适应宽度</button>'
        '<span class="zoom" id="zoom-level"></span>'
        f"{legend}</div>"
        '<div class="flow-scroll" id="flow-scroll"><div class="flow-size" id="flow-size">'
        f'<div class="flow-grid" id="flow-grid" style="--cols:{max(live.columns, 1)};'
        f'--acols:{max(every.columns, 1)}">'
        '<svg class="flow-edges" id="flow-edges" aria-hidden="true"></svg>'
        f"{''.join(lanes)}{''.join(nodes)}</div></div></div>"
        '<p class="empty" id="flow-empty" hidden>没有符合筛选条件的任务。</p>'
        "</section>"
    )
