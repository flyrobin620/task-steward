"""The task flow view of the Dashboard: tasks laid out by their relations (DN006 §8.10).

Every task is a card; the arrows run in the order the work is done (T304): from the task
depended on to the task depending on it, from the source of a follow-up to the task made from
it, and, dotted, from a referenced task to the task referencing it (:mod:`tsw.host.relations`).

Horizontally the dependencies come first, the follow-ups shown second and time last (the
user's decisions of 2026-11-18, T303's layout G3, and of 2026-11-24, T352): a task depending on
another, directly or not, is always left of it, so the flow completes from right to left; a task
made from a follow-up is left of its source unless a dependency says otherwise; among the tasks
free of these, the one whose business changed last is further left (bookkeeping such as
archiving does not count, :mod:`tsw.projection`). Vertically each repository has its own
lane. Every line has its own ports and channels; references never move a card.

The layout is computed here, when the page is generated, so it is tested like any other logic:
the cards' positions and every line's waypoints, for each choice the page offers (the
follow-ups shown or not, the references shown or not, the archived tasks in or not), carried
by the page as data. The page's script only takes the layout of the current choice, shows and
hides cards by the filters and draws the lines along their waypoints.
"""

from __future__ import annotations

import heapq
import html
import itertools
import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from tsw.tasks import KINDS, STATUS_LABELS

# How many cards of one repository share a column.
LANE_CAPACITY = 6
# The relations, in the order of the layout keys. Dependencies are always shown and laid out;
# the legend shows or hides the follow-ups and the references (T304).
RELATIONS = ("dep", "fu", "ref")
CHOICES = ("fu", "ref")
# Geometry, in CSS pixels. The page sizes the cards from ``CARD_W`` and ``CARD_H``.
CARD_W = 178.0
CARD_H = 62.0
LEFT = 18.0  # before the first column
HEAD_H = 30.0  # the column headings above the lanes
LANE_PAD = 16.0  # above the first and below the last item of a lane
GAP_MIN = 44.0  # the narrowest gap between two columns
CHANNEL = 8.0  # between two vertical channels in a gap
EDGE = 6.0  # the least distance from a channel to a column
PORT_STEP = 10.0  # between two ports on a card's side (closer when many)
# Vertical distance between two items in a column of a lane: cards ("n") and the points where a
# line crosses a column ("d").
SEP = {("n", "n"): 18.0, ("n", "d"): 12.0, ("d", "n"): 12.0, ("d", "d"): 8.0}
SWEEPS = 16  # barycentre sweeps ordering the columns
NUDGES = 24  # sweeps placing the items vertically

Item = Any  # a task id, or a ("d", line, column) point where a line crosses a column


@dataclass(frozen=True)
class FlowTask:
    task_id: str
    title: str
    repo: str
    kind: str
    status: str
    seq: int  # registration order: the ledger sequence of the task's registration
    activity: str  # the task's latest activity (``last_activity``): orders the lanes
    # The task's latest change of business (``last_record``, T352); ``activity`` if unset.
    modified: str = ""

    @property
    def when(self) -> str:
        return self.modified or self.activity


@dataclass(frozen=True)
class Line:
    """A line from ``source`` to ``target`` (the arrow's head), along ``points``."""

    kind: str  # "dep", "fu" or "ref"
    source: str
    target: str
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Lane:
    repo: str
    top: float
    bottom: float


def _no_columns() -> list[list[str]]:
    return []


def _no_floats() -> list[float]:
    return []


def _no_lines() -> list[Line]:
    return []


def _no_lanes() -> list[Lane]:
    return []


def _no_place() -> dict[str, tuple[int, float]]:
    return {}


@dataclass
class Layout:
    """Where the cards are and how the lines run.

    ``cards`` gives each task's column and the y of its top; a column's x is in ``xs`` (in
    ``xs_wide`` with the references shown), and the gap after it is ``gaps[c]`` wide
    (``gaps_wide[c]``). ``lines`` run without the references; ``peek`` has every line, the
    references included, in the same gaps (a card hovered); ``wide`` has every line in the gaps
    widened for the references (the references shown).
    """

    columns: list[list[str]] = field(default_factory=_no_columns)
    cards: dict[str, tuple[int, float]] = field(default_factory=_no_place)
    lanes: list[Lane] = field(default_factory=_no_lanes)
    xs: list[float] = field(default_factory=_no_floats)
    gaps: list[float] = field(default_factory=_no_floats)
    xs_wide: list[float] = field(default_factory=_no_floats)
    gaps_wide: list[float] = field(default_factory=_no_floats)
    height: float = HEAD_H
    height_refs: float = HEAD_H
    lines: list[Line] = field(default_factory=_no_lines)
    peek: list[Line] = field(default_factory=_no_lines)
    wide: list[Line] = field(default_factory=_no_lines)

    @property
    def width(self) -> float:
        return _width(self.xs, self.gaps)

    @property
    def width_wide(self) -> float:
        return _width(self.xs_wide, self.gaps_wide)

    def x(self, task_id: str, wide: bool = False) -> float:
        return (self.xs_wide if wide else self.xs)[self.cards[task_id][0]]


def _width(xs: list[float], gaps: list[float]) -> float:
    return xs[-1] + CARD_W + gaps[-1] if xs else LEFT


def lane_order(tasks: Iterable[FlowTask]) -> list[str]:
    """Repositories by their latest activity, most recent first (then by name)."""
    latest: dict[str, str] = {}
    for t in tasks:
        if t.activity > latest.get(t.repo, ""):
            latest[t.repo] = t.activity
        latest.setdefault(t.repo, "")
    return sorted(sorted(latest), key=lambda r: latest[r], reverse=True)


def _pairs(
    relation: Mapping[str, Iterable[str]], known: Mapping[str, FlowTask]
) -> list[tuple[str, str]]:
    """(task, other) for every task related to another known task, once, in a stable order."""
    found = {
        (a, b)
        for a, others in relation.items()
        if a in known
        for b in others
        if b in known and b != a
    }
    return sorted(found)


def _needs(
    by_id: Mapping[str, FlowTask], depends_on: Mapping[str, Iterable[str]]
) -> list[tuple[str, str]]:
    """(a, b) for every task ``a`` that depends on ``b``, both in the layout, directly or through
    tasks outside it (an archived task not shown still orders the tasks around it)."""
    found: set[tuple[str, str]] = set()
    for a in sorted(by_id):
        seen: set[str] = set()
        todo = [d for d in depends_on.get(a, ()) if d != a]
        while todo:
            d = todo.pop()
            if d in seen or d == a:
                continue
            seen.add(d)
            if d in by_id:
                found.add((a, d))
            else:
                todo += depends_on.get(d, ())
    return sorted(found)


def _reaches(edges: Mapping[str, Iterable[str]], start: str, goal: str) -> bool:
    """Whether ``goal`` can be reached from ``start`` along ``edges``."""
    seen = {start}
    todo = [start]
    while todo:
        for b in edges.get(todo.pop(), ()):
            if b == goal:
                return True
            if b not in seen:
                seen.add(b)
                todo.append(b)
    return False


def sequence(
    tasks: Sequence[FlowTask],
    depends_on: Mapping[str, Iterable[str]],
    follow_ups: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """The tasks from left to right: dependencies first, then follow-ups, then the time.

    Every task comes before the tasks it depends on, directly or not; with ``follow_ups`` (a
    task to the tasks it was made from, when the follow-ups are shown), a task made from a
    follow-up also comes before its source, unless a dependency puts them the other way round
    (T352). Among the tasks whose left neighbours are all placed, the one modified last goes
    next (on a tie the one registered later). A last pass swaps two neighbours out of time order
    when no path of these joins them, until there is none. A dependency cycle (only an old
    record could have one) is broken by taking the newest task left.
    """
    by_id = {t.task_id: t for t in tasks}
    newest = sorted(by_id, key=lambda t: (by_id[t].when, by_id[t].seq, t), reverse=True)
    rank = {t: i for i, t in enumerate(newest)}  # 0 is the newest
    needs: dict[str, list[str]] = {t: [] for t in by_id}  # a -> the tasks right of a
    waiting = {t: 0 for t in by_id}  # tasks to be left of a task, not yet placed
    for a, b in _needs(by_id, depends_on):
        needs[a].append(b)
        waiting[b] += 1
    # A follow-up that would close a cycle with what is already there gives way: the
    # dependencies are added first, so they win.
    for a, b in sorted(_pairs(follow_ups or {}, by_id), key=lambda p: (rank[p[0]], rank[p[1]])):
        if b in needs[a] or _reaches(needs, b, a):
            continue
        needs[a].append(b)
        waiting[b] += 1

    ready = [rank[t] for t in by_id if waiting[t] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    done: set[str] = set()
    cursor = 0  # into ``newest``, for breaking a cycle
    while len(order) < len(by_id):
        while ready and newest[ready[0]] in done:
            heapq.heappop(ready)
        if ready:
            t = newest[heapq.heappop(ready)]
        else:
            while newest[cursor] in done:
                cursor += 1
            t = newest[cursor]
        order.append(t)
        done.add(t)
        for b in needs[t]:
            waiting[b] -= 1
            if waiting[b] == 0:
                heapq.heappush(ready, rank[b])
    # The tasks each task must stay left of, directly or not.
    below: dict[str, set[str]] = {}
    for t in reversed(order):  # they are right of it, so already done
        seen: set[str] = set()
        for b in needs[t]:
            if b not in seen:
                seen.add(b)
                seen |= below.get(b, set())
        below[t] = seen
    changed = True
    while changed:
        changed = False
        for i in range(len(order) - 1):
            u, v = order[i], order[i + 1]
            if rank[u] > rank[v] and v not in below[u] and u not in below[v]:
                order[i], order[i + 1] = v, u
                changed = True
    return order


def _cut(
    order: list[str], repo: Mapping[str, str], lines: list[tuple[str, str]], capacity: int
) -> list[list[str]]:
    """Cut the order into columns: no line within a column, at most ``capacity`` cards of a
    repository in a column, then the least total span of the lines and the fewest columns."""
    n = len(order)
    pos = {t: i for i, t in enumerate(order)}
    crossing = [0] * n  # lines crossing the cut after position i
    partners: dict[int, list[int]] = defaultdict(list)  # p -> positions q > p joined to it
    for a, b in lines:
        p, q = sorted((pos[a], pos[b]))
        partners[p].append(q)
        for i in range(p, q):
            crossing[i] += 1  # (a dependency through hidden tasks counts like a line)
    inf = (float("inf"), 0)
    best: list[tuple[float, int]] = [inf] * n  # (span, columns) of order[0..j] cut after j
    start = [0] * n
    for j in range(n):
        count: dict[str, int] = defaultdict(int)
        for s in range(j, -1, -1):  # the last column is order[s..j]
            count[repo[order[s]]] += 1
            if count[repo[order[s]]] > capacity or any(q <= j for q in partners.get(s, ())):
                break
            base = (0.0, 0) if s == 0 else best[s - 1]
            if base == inf:
                continue
            cand = (base[0] + (crossing[s - 1] if s else 0), base[1] + 1)
            if cand < best[j]:
                best[j], start[j] = cand, s
    columns: list[list[str]] = []
    j = n - 1
    while j >= 0:
        columns.append(order[start[j] : j + 1])
        j = start[j] - 1
    columns.reverse()
    return columns


def _pav(target: list[float], weight: list[float]) -> list[float]:
    """Weighted isotonic (non-decreasing) regression by pooling adjacent violators."""
    blocks: list[list[float]] = []  # [value, weight, count]
    for t, w in zip(target, weight, strict=True):
        blocks.append([t, w, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    out: list[float] = []
    for v, _, n in blocks:
        out += [v] * int(n)
    return out


def _sort(item: Item) -> str:
    return "n" if isinstance(item, str) else "d"


@dataclass(frozen=True)
class _Spec:
    """A line to route: from its ``left`` card through ``mids`` (one height per column passed)
    to its ``right`` card; ``same`` when both cards are in one column (a reference)."""

    kind: str
    source: str
    target: str
    left: str
    right: str
    mids: tuple[float, ...]
    same: bool = False


def lay_out(
    tasks: Sequence[FlowTask],
    depends_on: Mapping[str, Iterable[str]],
    follow_ups: Mapping[str, Iterable[str]] | None = None,
    references: Mapping[str, Iterable[str]] | None = None,
    capacity: int = LANE_CAPACITY,
) -> Layout:
    """The layout of ``tasks`` (DN006 §8.10 task flow, items 2 and 3).

    ``depends_on`` maps a task to the tasks it depends on, ``follow_ups`` a task to the tasks
    it was made from (only when the follow-ups are shown) and ``references`` a task to the tasks
    it references. Dependencies and follow-ups are laid out; references are only routed.
    Relations to tasks outside ``tasks`` are left out.
    """
    by_id = {t.task_id: t for t in tasks}
    if not by_id:
        return Layout()
    repo = {t: by_id[t].repo for t in by_id}
    deps = _pairs(depends_on, by_id)
    fus = _pairs(follow_ups or {}, by_id)
    refs = _pairs(references or {}, by_id)
    order = sequence(tasks, depends_on, follow_ups)
    pos = {t: i for i, t in enumerate(order)}
    lanes = lane_order(tasks)
    lane_of = {r: i for i, r in enumerate(lanes)}

    # Lines laid out, as (a, b, kind): ``a`` depends on or came from ``b``; drawn b -> a.
    laid = [(a, b, "dep") for a, b in deps] + [(a, b, "fu") for a, b in fus]
    laid.sort(key=lambda t: (pos[t[0]], pos[t[1]], t[2]))
    # A dependency through tasks outside the layout keeps its two ends apart too.
    apart = sorted({(a, b) for a, b, _ in laid} | set(_needs(by_id, depends_on)))
    columns = _cut(order, repo, apart, capacity)
    ncols = len(columns)
    col: dict[Item, int] = {t: c for c, ts in enumerate(columns) for t in ts}
    column_of = {t: col[t] for t in by_id}

    # One point per column a line passes, every line its own.
    lane: dict[Item, int] = {t: lane_of[repo[t]] for t in by_id}
    chains: list[list[Item]] = []
    for k, (a, b, _) in enumerate(laid):
        left, right = (a, b) if col[a] < col[b] else (b, a)
        chain: list[Item] = [left]
        for c in range(col[left] + 1, col[right]):
            d = ("d", k, c)
            lane[d], col[d] = lane[left], c
            chain.append(d)
        chain.append(right)
        chains.append(chain)
    to_left: dict[Item, list[Item]] = defaultdict(list)
    to_right: dict[Item, list[Item]] = defaultdict(list)
    for chain in chains:
        for x, y in itertools.pairwise(chain):
            to_right[x].append(y)
            to_left[y].append(x)

    # Order within each (column, lane): time order first, then barycentre sweeps, keeping the
    # order with the fewest crossings between neighbouring columns, then swaps of neighbours.
    n = len(order)
    cells: dict[tuple[int, int], list[Item]] = defaultdict(list)
    for item in sorted(col, key=lambda x: pos[x] if isinstance(x, str) else n + x[1] * n + x[2]):
        cells[(col[item], lane[item])].append(item)
    keys = sorted(cells)

    def position(cs: Mapping[tuple[int, int], list[Item]]) -> dict[Item, int]:
        return {it: ln * 100000 + i for (_, ln), items in cs.items() for i, it in enumerate(items)}

    def crossings(cs: Mapping[tuple[int, int], list[Item]]) -> int:
        p = position(cs)
        total = 0
        by_col: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for (c, _), items in cs.items():
            for x in items:
                by_col[c] += [(p[x], p[y]) for y in to_right[x]]
        for segs in by_col.values():
            segs.sort()
            for i, (_, y1) in enumerate(segs):
                for x2, y2 in segs[i + 1 :]:
                    if x2 > segs[i][0] and y2 < y1:
                        total += 1
        return total

    best_cells = {k: list(v) for k, v in cells.items()}
    fewest = crossings(best_cells)
    for sweep in range(SWEEPS if fewest else 0):
        p = position(cells)
        forward = sweep % 2 == 0
        side = to_left if forward else to_right
        for key in keys if forward else reversed(keys):
            items = cells[key]
            keyed = sorted(
                (
                    (sum(p[m] for m in side[it]) / len(side[it]) if side[it] else p[it], i, it)
                    for i, it in enumerate(items)
                ),
                key=lambda k: (k[0], k[1]),
            )
            cells[key] = [k[2] for k in keyed]
            for i, it in enumerate(cells[key]):
                p[it] = key[1] * 100000 + i
        found = crossings(cells)
        if found < fewest:
            best_cells, fewest = {k: list(v) for k, v in cells.items()}, found
        if not fewest:
            break
    cells = best_cells
    for _ in range(12):
        p = position(cells)
        better = False
        for key in keys:
            items = cells[key]
            for i in range(len(items) - 1):
                u, v = items[i], items[i + 1]
                keep = swap = 0
                for side in (to_left, to_right):
                    for s in side[u]:
                        for t in side[v]:
                            keep += p[s] > p[t]
                            swap += p[s] < p[t]
                if swap < keep:
                    items[i], items[i + 1] = v, u
                    p[u], p[v] = p[v], p[u]
                    better = True
        if not better:
            break

    # Heights: every item near the items joined to it, keeping each column's order and spacing.
    size = {it: (CARD_H if isinstance(it, str) else 0.0) for it in col}
    offsets: dict[tuple[int, int], list[float]] = {}
    y: dict[Item, float] = {}
    for key, items in cells.items():
        offs = [0.0]
        for a, b in itertools.pairwise(items):
            offs.append(offs[-1] + size[a] / 2 + SEP[_sort(a), _sort(b)] + size[b] / 2)
        offsets[key] = offs
        for it, o in zip(items, offs, strict=True):
            y[it] = o

    def pull(a: Item, b: Item) -> float:
        da, db = not isinstance(a, str), not isinstance(b, str)
        return 8.0 if da and db else 2.0 if da or db else 1.0

    # The items pulling each item (in its own lane), with their weights.
    pulled = {
        it: [(m, pull(it, m)) for m in to_left[it] + to_right[it] if lane[m] == lane[it]]
        for it in col
    }
    pulls = {it: sum(w for _, w in ms) for it, ms in pulled.items()}
    for sweep in range(NUDGES):
        for key in keys if sweep % 2 == 0 else reversed(keys):
            items, offs = cells[key], offsets[key]
            want: list[float] = []
            weight: list[float] = []
            for i, it in enumerate(items):
                if pulled[it]:
                    total = pulls[it]
                    want.append(sum(w * y[m] for m, w in pulled[it]) / total - offs[i])
                    weight.append(total)
                else:  # nothing pulls it: right below the item above
                    want.append(y[items[i - 1]] - offs[i - 1] if i else y[it] - offs[i])
                    weight.append(0.25)
            for it, z, o in zip(items, _pav(want, weight), offs, strict=True):
                y[it] = z + o
    lane_rows: list[Lane] = []
    top = HEAD_H
    for ln, r in enumerate(lanes):
        mine = [it for it in col if lane[it] == ln]
        lo = min(y[it] - size[it] / 2 for it in mine)
        hi = max(y[it] + size[it] / 2 for it in mine)
        shift = top + LANE_PAD - lo
        for it in mine:
            y[it] = round(y[it] + shift, 1)
        bottom = top + LANE_PAD + (hi - lo) + LANE_PAD
        lane_rows.append(Lane(r, round(top, 1), round(bottom, 1)))
        top = bottom

    specs = [
        _Spec(
            kind,
            b,
            a,
            chain[0],
            chain[-1],
            tuple(y[d] for d in chain[1:-1]),
        )
        for (a, b, kind), chain in zip(laid, chains, strict=True)
    ]
    plan = _plan(specs, y, column_of)
    gaps = [max(GAP_MIN, 28 + (plan.count.get(c, 1) - 1) * CHANNEL) for c in range(ncols)]
    xs = _xs(gaps)
    lines = _route(plan, xs, gaps)

    # References: never laid out. Each runs straight at a height free of cards and of other
    # lines in every column it passes, then has its own ports and channels like any other line.
    rows = _Rows(top)
    for t in by_id:
        rows.block(column_of[t], column_of[t] + 1, y[t] - CARD_H / 2 - 6, y[t] + CARD_H / 2 + 6)
    for spec in specs:
        first = column_of[spec.left] + 1
        for i, mid in enumerate(spec.mids):
            rows.block(first + i, first + i + 1, mid - 5, mid + 5)
    below = top + 12  # runs below the lanes when no height is free
    ref_specs: list[_Spec] = []

    def span(pair: tuple[str, str]) -> tuple[int, tuple[str, str]]:
        return abs(column_of[pair[0]] - column_of[pair[1]]), pair

    for a, b in sorted(refs, key=span):
        left, right = (a, b) if column_of[a] <= column_of[b] else (b, a)
        if column_of[left] == column_of[right] and y[left] > y[right]:
            left, right = right, left
        cl, cr = column_of[left], column_of[right]
        mids: tuple[float, ...] = ()
        if cr - cl >= 2:
            free = rows.free(cl + 1, cr, (y[left] + y[right]) / 2)
            if free is None:
                free, below = below, below + CHANNEL
            else:
                rows.block(cl + 1, cr, free - 5, free + 5)
            mids = (free,) * (cr - cl - 1)
        ref_specs.append(_Spec("ref", b, a, left, right, mids, cl == cr))
    everything = specs + ref_specs
    if ref_specs:
        plan = _plan(everything, y, column_of)
        peek = _route(plan, xs, gaps)
        gaps_wide = [max(g, 28 + (plan.count.get(c, 1) - 1) * CHANNEL) for c, g in enumerate(gaps)]
        xs_wide = _xs(gaps_wide)
        wide = _route(plan, xs_wide, gaps_wide)
    else:
        peek, gaps_wide, xs_wide, wide = lines, gaps, xs, lines
    return Layout(
        columns=columns,
        cards={t: (column_of[t], round(y[t] - CARD_H / 2, 1)) for t in by_id},
        lanes=lane_rows,
        xs=xs,
        gaps=gaps,
        xs_wide=xs_wide,
        gaps_wide=gaps_wide,
        height=round(top, 1),
        height_refs=round(below - CHANNEL + 12 if below > top + 12 else top, 1),
        lines=lines,
        peek=peek,
        wide=wide,
    )


def _xs(gaps: list[float]) -> list[float]:
    xs: list[float] = []
    x = LEFT
    for g in gaps:
        xs.append(round(x, 1))
        x += CARD_W + g
    return xs


class _Rows:
    """The heights each column has taken, as a bit per whole pixel."""

    def __init__(self, top: float) -> None:
        self.top = int(top)
        self.taken: dict[int, int] = defaultdict(int)
        low = int(HEAD_H) + 1  # below the headings, above the bottom of the lanes
        self.inside = ((1 << (self.top - low)) - 1) << low if self.top > low else 0

    def block(self, first: int, end: int, lo: float, hi: float) -> None:
        """Columns [first, end) taken from ``lo`` to ``hi``."""
        a, b = max(0, int(lo)), min(self.top, math.ceil(hi))
        if b < a:
            return
        bits = ((1 << (b - a + 1)) - 1) << a
        for c in range(first, end):
            self.taken[c] |= bits

    def free(self, first: int, end: int, want: float) -> float | None:
        """The whole pixel nearest to ``want`` (the upper one on a tie) inside the lanes that
        none of columns [first, end) has taken; None when there is none."""
        taken = 0
        for c in range(first, end):
            taken |= self.taken.get(c, 0)
        free = self.inside & ~taken
        if not free:
            return None
        start = max(0, round(want))
        above = free & ((1 << (start + 1)) - 1)  # rows at or above ``start`` (smaller y)
        below = free >> start
        up = start - (above.bit_length() - 1) if above else None
        down = (below & -below).bit_length() - 1 if below else None
        if down is None or (up is not None and up <= down):
            return float(start - up) if up is not None else None
        return float(start + down)


_Hop = tuple[int, float, float, int, int, bool]  # (gap, y1, y2, line, hop, U-turn)


def _ports(specs: Sequence[_Spec], y: Mapping[Item, float]) -> dict[tuple[str, str, int], float]:
    """Each line's port on each card side, spread by where the line goes next."""
    sides: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    for k, sp in enumerate(specs):
        if sp.same:
            sides[(sp.left, "r")].append((y[sp.right], k))
            sides[(sp.right, "r")].append((y[sp.left], k))
            continue
        sides[(sp.left, "r")].append((sp.mids[0] if sp.mids else y[sp.right], k))
        sides[(sp.right, "l")].append((sp.mids[-1] if sp.mids else y[sp.left], k))
    port: dict[tuple[str, str, int], float] = {}
    for (t, side), found in sides.items():
        found.sort()
        m = len(found)
        step = min(PORT_STEP, (CARD_H - 12) / (m - 1)) if m > 1 else 0.0
        for i, (_, k) in enumerate(found):
            port[(t, side, k)] = y[t] + (i - (m - 1) / 2) * step
    return port


def _hops(
    specs: Sequence[_Spec], y: Mapping[Item, float], column_of: Mapping[str, int]
) -> tuple[list[list[_Hop]], dict[int, list[_Hop]]]:
    """Each line's hops between neighbouring columns, and the hops turning in each gap."""
    port = _ports(specs, y)
    per_line: list[list[_Hop]] = []
    turning: dict[int, list[_Hop]] = defaultdict(list)
    for k, sp in enumerate(specs):
        first = column_of[sp.left]
        if sp.same:
            hop = (first, port[(sp.left, "r", k)], port[(sp.right, "r", k)], k, 0, True)
            per_line.append([hop])
            turning[first].append(hop)
            continue
        ys = [port[(sp.left, "r", k)], *sp.mids, port[(sp.right, "l", k)]]
        mine: list[_Hop] = []
        for h in range(len(ys) - 1):
            hop = (first + h, ys[h], ys[h + 1], k, h, False)
            mine.append(hop)
            if abs(ys[h] - ys[h + 1]) > 0.5:
                turning[first + h].append(hop)
        per_line.append(mine)
    return per_line, turning


def _assign(turning: Mapping[int, list[_Hop]]) -> tuple[dict[tuple[int, int], int], dict[int, int]]:
    """A vertical channel for every hop turning in a gap: hops whose vertical spans meet never
    share one; the left-to-right order avoids crossings pair by pair. U-turns come first, nearest
    the column they return to."""

    channel: dict[tuple[int, int], int] = {}
    count: dict[int, int] = {}
    for c, segs in turning.items():
        score = [0] * len(segs)
        lows = [min(s[1], s[2]) for s in segs]
        highs = [max(s[1], s[2]) for s in segs]
        spans = sorted(range(len(segs)), key=lambda i: (lows[i], i))
        for q, i in enumerate(spans):
            si, lo_i, hi_i = segs[i], lows[i], highs[i]
            for j in spans[q + 1 :]:
                lo_j = lows[j]
                if lo_j >= hi_i:
                    break
                sj, hi_j = segs[j], highs[j]
                # Crossings with i left of j, and with j left of i.
                ij = (lo_i < sj[1] < hi_i) + (lo_j < si[2] < hi_j)
                ji = (lo_j < si[1] < hi_j) + (lo_i < sj[2] < hi_i)
                d = (ij < ji) - (ij > ji)
                score[i] += d
                score[j] -= d
        ranked = sorted(
            (not s[5], -score[i], s[1] if s[2] > s[1] else -s[1], s[3], s[4], i)
            for i, s in enumerate(segs)
        )
        # Channels taken so far, as the vertical spans each one holds (sorted by their low end).
        held: list[list[tuple[float, float]]] = []
        for *_, i in ranked:
            s = segs[i]
            lo, hi = min(s[1], s[2]) - 4, max(s[1], s[2]) + 4
            ch = 0
            for k in range(len(held) - 1, -1, -1):
                if any(a < hi and lo < b for a, b in held[k]):
                    ch = k + 1
                    break
            while len(held) <= ch:
                held.append([])
            held[ch].append((lo, hi))
            channel[(s[3], s[4])] = ch
        count[c] = len(held)
    return channel, count


@dataclass(frozen=True)
class _Plan:
    """The lines' hops with their channels, and how many channels each gap needs."""

    specs: Sequence[_Spec]
    hops: list[list[_Hop]]
    channel: dict[tuple[int, int], int]
    count: dict[int, int]


def _plan(specs: Sequence[_Spec], y: Mapping[Item, float], column_of: Mapping[str, int]) -> _Plan:
    per_line, turning = _hops(specs, y, column_of)
    channel, count = _assign(turning)
    return _Plan(specs, per_line, channel, count)


def _route(plan: _Plan, xs: list[float], gaps: list[float]) -> list[Line]:
    """The lines along their ports and channels: out of the left card's right side, through the
    gaps and the columns passed, into the right card's left side (a U-turn back into the same
    column's right side), as orthogonal waypoints from the arrow's tail to its head."""
    specs, per_line, channel, count = plan.specs, plan.hops, plan.channel, plan.count

    def gap_x(c: int, ch: int, n: int) -> float:
        """Channel ``ch`` of ``n`` in gap ``c``: evenly spaced, centred, clear of the columns."""
        x1 = xs[c] + CARD_W
        width = gaps[c]
        sp = min(CHANNEL, (width - 2 * EDGE) / (n - 1)) if n > 1 else 0.0
        return x1 + (width - (n - 1) * sp) / 2 + ch * sp

    out: list[Line] = []
    for k, sp in enumerate(specs):
        pts: list[tuple[float, float]] = []
        hops = per_line[k]
        at = hops[0][1]  # the height the line runs at; a hop without a turn keeps it
        for c, _, y2, _, h, uturn in hops:
            x1 = xs[c] + CARD_W
            x2 = x1 if uturn else xs[c + 1]
            if not pts:
                pts.append((x1, at))
            if (k, h) in channel:
                gx = gap_x(c, channel[(k, h)], count[c])
                pts += [(gx, at), (gx, y2), (x2, y2)]
                at = y2
            else:
                pts.append((x2, at))
            if not uturn and h + 1 < len(hops):
                pts.append((x2 + CARD_W, at))
        if sp.left == sp.target:  # the arrow's head is at the left card
            pts.reverse()
        out.append(Line(sp.kind, sp.source, sp.target, _simplify(pts)))
    return out


def _simplify(pts: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Drop repeated and collinear points, then round to tenths of a pixel."""
    out: list[tuple[float, float]] = []
    for p in pts:
        if out and out[-1] == p:
            continue
        if len(out) >= 2:
            (x0, y0), (x1, y1) = out[-2], out[-1]
            if (x0 == x1 == p[0]) or (y0 == y1 == p[1]):
                out[-1] = p
                continue
        out.append(p)
    return tuple((round(x, 1), round(y, 1)) for x, y in out)


def relation_choices() -> list[tuple[str, ...]]:
    """Every choice of the relations shown: dependencies always, then the follow-ups and the
    references shown or not (T304), everything first."""
    return [
        ("dep", *(r for r, on in zip(CHOICES, flags, strict=True) if on))
        for flags in ((True, True), (True, False), (False, True), (False, False))
    ]


def layout_key(everything: bool, shown: Iterable[str]) -> str:
    """The key of a layout in the page: ``live`` or ``all``, then the relations shown."""
    chosen = set(shown) | {"dep"}
    return ("all" if everything else "live") + ":" + ",".join(r for r in RELATIONS if r in chosen)


def flow_layouts(
    tasks: Sequence[FlowTask],
    depends_on: Mapping[str, Iterable[str]],
    follow_up_of: Mapping[str, Iterable[str]],
    references: Mapping[str, Iterable[str]],
) -> dict[str, Layout]:
    """The layouts of the task flow by :func:`layout_key`, for every choice the page offers.

    A follow-up not shown neither keeps its two ends apart nor makes an archived task context;
    a reference never moves a card nor makes a task context (the user's choice of 2026-11-18,
    T304). The layout without the archived tasks (``live``) has the tasks not archived and the
    archived tasks a dependency, or a follow-up shown, joins to one of them directly; the other
    (``all``) has every task. So there are four layouts, the follow-ups shown or not, with or
    without the archived tasks; equal ones are laid out once.
    """
    relation: dict[str, Mapping[str, Iterable[str]]] = {
        "dep": depends_on,
        "fu": follow_up_of,
        "ref": references,
    }
    known = {t.task_id for t in tasks}
    joined: dict[str, dict[str, set[str]]] = {}
    for r, pairs in relation.items():
        near: dict[str, set[str]] = {t: set() for t in known}
        for tid, others in pairs.items():
            if tid not in known:
                continue
            for other in others:
                if other in known and other != tid:
                    near[tid].add(other)
                    near[other].add(tid)
        joined[r] = near
    live = {t.task_id for t in tasks if t.status != "archived"}
    done: dict[tuple[frozenset[str], bool], Layout] = {}
    found: dict[str, Layout] = {}
    for chosen in relation_choices():
        for everything in (False, True):
            fu = "fu" in chosen
            making = ("dep", "fu") if fu else ("dep",)  # the relations that make context
            context = live | {o for r in making for t in live for o in joined[r][t]}
            ids = frozenset(known) if everything else frozenset(context)
            key = (ids, fu)
            if key not in done:
                done[key] = lay_out(
                    [t for t in tasks if t.task_id in ids],
                    depends_on,
                    follow_up_of if fu else None,
                    references,
                )
            found[layout_key(everything, chosen)] = done[key]
    return found


KIND_INDEX = {"dep": 0, "fu": 1, "ref": 2}


def _numbers(points: Sequence[tuple[float, float]]) -> list[float]:
    """Orthogonal waypoints as x0, y0, then each next point's changed coordinate in turn (the
    first move is horizontal, the next vertical, and so on)."""
    out = [points[0][0], points[0][1]]
    for i in range(1, len(points)):
        out.append(points[i][0] if i % 2 else points[i][1])
    return out


def _layout_data(
    layouts: Mapping[str, Layout], ids: Sequence[str], repos: Sequence[str]
) -> dict[str, Any]:
    """The page's copy of the layouts: equal ones once, cards and lanes in the page's order."""
    index = {tid: i for i, tid in enumerate(ids)}

    def lines(found: Sequence[Line]) -> list[list[float]]:
        return [
            [KIND_INDEX[ln.kind], index[ln.source], index[ln.target], *_numbers(ln.points)]
            for ln in found
        ]

    sets: list[dict[str, Any]] = []
    seen: dict[int, int] = {}
    texts: dict[str, int] = {}
    pick: dict[str, int] = {}
    for key, layout in layouts.items():
        if id(layout) not in seen:
            lanes = {lane.repo: lane for lane in layout.lanes}
            data = {
                "cols": len(layout.columns),
                "xs": layout.xs,
                "gaps": layout.gaps,
                "xw": layout.xs_wide,
                "gw": layout.gaps_wide,
                "h": layout.height,
                "hr": layout.height_refs,
                "cards": [list(layout.cards[t]) if t in layout.cards else None for t in ids],
                "lanes": [[lanes[r].top, lanes[r].bottom] if r in lanes else None for r in repos],
                "lines": lines(layout.lines),
                "peek": lines(layout.peek) if layout.peek is not layout.lines else 0,
                "wide": lines(layout.wide) if layout.wide is not layout.lines else 0,
            }
            text = json.dumps(data, sort_keys=True)
            if text not in texts:
                texts[text] = len(sets)
                sets.append(data)
            seen[id(layout)] = texts[text]
        pick[key] = seen[id(layout)]
    return {
        "ids": list(ids),
        "repos": list(repos),
        "card": [CARD_W, CARD_H],
        "head": HEAD_H,
        "pick": pick,
        "sets": sets,
    }


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
    """The task flow section: its controls, lanes and cards (the script places and draws).

    ``depends_on`` maps a task to the tasks it depends on, ``follow_up_of`` a task to the tasks
    it was made from by a follow-up and ``references`` a task to the tasks it references. An
    archived task joined to a board task by a relation shown is in the layout without the
    archived tasks. ``flags`` are the board card's attention flags, so the filters select the
    same tasks here as on the board.

    The cards carry no position: the layouts of :func:`flow_layouts` are in a data block (JSON,
    never run) that the script reads to place the cards and the lanes and to draw the lines for
    the choices made and whether the archived tasks are in.
    """
    known = {t.task_id for t in tasks}
    deps = {tid: sorted(d for d in ds if d in known) for tid, ds in depends_on.items()}
    sources = {tid: sorted(s for s in ss if s in known) for tid, ss in follow_up_of.items()}
    refs = {tid: sorted(r for r in rs if r in known) for tid, rs in (references or {}).items()}
    layouts = flow_layouts(tasks, deps, sources, refs)
    ordered = sorted(tasks, key=lambda t: -t.seq)
    repos = lane_order(tasks)
    data = _layout_data(layouts, [t.task_id for t in ordered], repos)

    names: list[str] = []
    bands: list[str] = []
    for repo in repos:
        style = f"--h:{repo_hue(repo)}"
        bands.append(f'<div class="lane" data-repo="{_e(repo)}" style="{style}"></div>')
        names.append(
            f'<div class="lane-name" data-repo="{_e(repo)}" style="{style}" '
            f'title="{_e(repo)}">{_e(repo)}</div>'
        )

    nodes: list[str] = []
    for t in ordered:
        status = STATUS_LABELS.get(t.status, t.status)
        attrs = (
            f'class="fnode st-{_e(t.status)}" '
            f'data-id="{_e(t.task_id)}" data-repo="{_e(t.repo)}" data-kind="{_e(t.kind)}" '
            f'data-status="{_e(t.status)}" data-seq="{t.seq}" data-when="{_e(t.when)}" '
            f'data-flags="{_e(flags.get(t.task_id, ""))}" '
            f'data-deps="{_e(" ".join(deps.get(t.task_id, [])))}" '
            f'data-from="{_e(" ".join(sources.get(t.task_id, [])))}" '
            f'data-refs="{_e(" ".join(refs.get(t.task_id, [])))}" '
            f'title="{_e(t.task_id)} · {_e(t.title)} · {_e(status)} · '
            f'{_e(KINDS.get(t.kind, t.kind))} · {_e(t.repo)} · 最近修改 {_e(t.when)}"'
        )
        inner = (
            f'<b class="fid">{_e(t.task_id)}</b>'
            '<span class="ftime"></span>'
            f'<span class="fst">{_e(status)}</span>'
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
        '<svg width="34" height="10" aria-hidden="true"><line x1="33" y1="5" x2="7" y2="5" '
        'class="lg {}"/><path d="M7 1 L1 5 L7 9 Z" class="lg-h {}"/></svg>'
    )
    # Dependencies are always shown (T304); the follow-ups have their checkbox, ticked by
    # default, and the references theirs, unticked by default.
    legend = (
        f'<span class="lg-item" title="依赖总是显示，并决定左右：依赖方在左">'
        f"{arrow.format('dep', 'dep')}A 依赖 B（B 完成后做 A）</span>"
        f'<label class="lg-rel" title="显示 follow-up 关系，并按它分列">'
        f'<input type="checkbox" data-rel="fu" checked>{arrow.format("fu", "fu")}'
        "A 由 B 的 follow-up 转来</label>"
        f'<label class="lg-rel" title="显示全部引用线；不显示时悬停卡片显示它的引用。'
        f'引用不移动卡片">'
        f'<input type="checkbox" data-rel="ref">{arrow.format("ref", "ref")}'
        "A 引用 B（显示引用）</label>"
    )
    legend = (
        f'<span class="legend" id="flow-legend"><span class="lg-dir">B → A：</span>{legend}'
        '<span class="lg-item"><i class="lg-ctx"></i>与筛选结果直接相连的上下文</span></span>'
    )
    shape = (
        '<span class="shape seg" role="radiogroup" aria-label="连线形状">'
        '<label><input type="radio" name="flow-shape" value="poly" checked>折线</label>'
        '<label><input type="radio" name="flow-shape" value="curve">曲线</label></span>'
    )
    # The data block is never run; "<", ">" and "&" are escaped so no text can end it.
    payload = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return (
        '<section class="flow" id="flow" hidden aria-label="任务流">'
        # One row (T353): the legend and the relation choices open from 「图例与关系」.
        '<div class="flow-bar" id="flow-bar">'
        '<label class="toggle"><input type="checkbox" id="flow-archived">含已归档</label>'
        f"{shape}"
        '<span class="zoom-group" role="group" aria-label="缩放">'
        '<button type="button" class="ghost" id="zoom-out" title="缩小" aria-label="缩小">'
        "−</button>"
        '<span class="zoom" id="zoom-level"></span>'
        '<button type="button" class="ghost" id="zoom-in" title="放大" aria-label="放大">+</button>'
        '<button type="button" class="ghost" id="zoom-reset">100%</button>'
        '<button type="button" class="ghost" id="zoom-fit" title="缩放到区域宽度">适应宽度</button>'
        "</span>"
        '<button type="button" class="ghost" id="legend-open" aria-haspopup="dialog" '
        'aria-expanded="false" aria-controls="legend-pop">图例与关系 ▾</button></div>'
        '<div class="pop legend-pop" id="legend-pop" role="dialog" aria-label="图例与关系" hidden>'
        f"{legend}</div>"
        '<p class="empty" id="flow-empty" hidden>没有符合筛选条件的任务。</p>'
        f'<script type="application/json" id="flow-layouts">{payload}</script>'
        # The slot is a window tall; the area in it grows as the page scrolls (T353).
        '<div class="flow-slot" id="flow-slot">'
        '<div class="flow-scroll" id="flow-scroll"><div class="flow-size" id="flow-size">'
        '<div class="flow-grid" id="flow-grid">'
        f'<div class="flow-names" id="flow-names">{"".join(names)}</div>'
        '<div class="flow-canvas" id="flow-canvas">'
        f"{''.join(bands)}"
        '<svg class="flow-edges" id="flow-edges" aria-hidden="true"></svg>'
        f"{''.join(nodes)}</div></div></div></div></div>"
        "</section>"
    )
