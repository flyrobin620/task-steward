"""The task flow's layout G3 (UC-DIRECT-004, DN006 §8.10 items 2, 3, 8 and 9, T304).

Dependencies first: a task depending on another, directly or not, is left of it; the tasks free
of that go newest left by their latest ledger record. Every line has its own ports and channels;
references never move a card.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterable, Sequence
from itertools import combinations, pairwise

import pytest

from tsw.host.flow import (
    CARD_H,
    CARD_W,
    LANE_CAPACITY,
    FlowTask,
    Layout,
    Line,
    flow_layouts,
    lay_out,
    layout_key,
    relation_choices,
    sequence,
)

mark = pytest.mark
Relation = dict[str, list[str]]


def task(n: int, repo: str = "a", when: str = "", status: str = "in_progress") -> FlowTask:
    return FlowTask(
        f"T{n:03d}", f"title {n}", repo, "code", status, n, "2026-11-01", when or f"2026-11-{n:02d}"
    )


def s1() -> tuple[list[FlowTask], Relation]:
    """The user's screenshot (T303's sample S1): nine tasks, nine dependencies, two lanes."""
    when = {
        294: "03:14:38", 295: "01:57:43", 296: "02:01:44", 297: "03:09:16", 298: "02:01:47",
        299: "02:03:02", 300: "02:01:51", 301: "02:03:24", 302: "03:25:26",
    }  # fmt: skip
    tasks = [
        FlowTask(
            f"T{n}",
            "t",
            "tool" if n in (297, 302) else "app",
            "code",
            "in_progress",
            n,
            f"2026-11-18T{w}Z",  # the screenshot's lanes: the tool lane was active last
            f"2026-11-18T{w}Z",
        )
        for n, w in when.items()
    ]
    deps = {
        "T295": ["T294"],
        "T296": ["T295"],
        "T298": ["T296", "T297"],
        "T299": ["T294"],
        "T300": ["T296", "T299"],
        "T301": ["T299", "T300"],
    }
    return tasks, deps


def synthetic(
    n: int, deps_per: float, refs_per: float, seed: int = 7
) -> tuple[list[FlowTask], Relation, Relation, Relation]:
    """A deterministic board of ``n`` tasks in a few repositories: dependencies on older
    tasks, a few follow-ups, and many references (like the user's whole ledger)."""
    rng = random.Random(seed)
    tasks = [
        FlowTask(
            f"T{k:04d}",
            "t",
            f"repo{rng.randrange(6)}",
            "code",
            "archived" if k < n // 2 else "accepted",
            k,
            f"2026-{1 + k * 11 // n:02d}-01",
            f"2026-{1 + rng.randrange(11):02d}-{1 + rng.randrange(28):02d}"
            f"T{rng.randrange(24):02d}:00Z",
        )
        for k in range(n)
    ]
    deps: Relation = {}
    fus: Relation = {}
    refs: Relation = {}
    for k in range(1, n):
        tid = f"T{k:04d}"
        if rng.random() < deps_per:
            deps[tid] = [f"T{rng.randrange(max(0, k - 30), k):04d}"]
        if rng.random() < 0.02:
            fus[tid] = [f"T{rng.randrange(k):04d}"]
        for _ in range(int(refs_per) + (rng.random() < refs_per % 1)):
            other = f"T{rng.randrange(k):04d}"
            if other not in deps.get(tid, []) and other not in fus.get(tid, []):
                refs.setdefault(tid, []).append(other)
    return tasks, deps, fus, refs


# ---------------------------------------------------------------------- geometry checks


def segments(lines: Iterable[Line]) -> list[tuple[int, float, float, float, float]]:
    out = []
    for k, ln in enumerate(lines):
        for (x0, y0), (x1, y1) in zip(ln.points, ln.points[1:], strict=False):
            out.append((k, x0, y0, x1, y1))
    return out


def crossings(lines: Sequence[Line]) -> int:
    """How often two different lines cross (a horizontal through a vertical, both strictly)."""
    found = 0
    segs = segments(lines)
    horizontal = [s for s in segs if s[2] == s[4]]
    vertical = [s for s in segs if s[1] == s[3]]
    for k, hx0, hy, hx1, _ in horizontal:
        for m, vx, vy0, _, vy1 in vertical:
            if k != m and min(hx0, hx1) < vx < max(hx0, hx1) and min(vy0, vy1) < hy < max(vy0, vy1):
                found += 1
    return found


def shared_channels(lines: Sequence[Line]) -> list[tuple[int, int]]:
    """Pairs of lines with vertical runs at one x whose spans overlap."""
    vertical = [s for s in segments(lines) if s[1] == s[3] and s[2] != s[4]]
    bad = []
    for (k, x, a0, _, a1), (m, x2, b0, _, b1) in combinations(vertical, 2):
        if k != m and x == x2 and min(a0, a1) < max(b0, b1) and min(b0, b1) < max(a0, a1):
            bad.append((k, m))
    return bad


def shared_ports(lines: Sequence[Line]) -> list[tuple[float, float]]:
    """Points where two lines meet a card at the same place."""
    seen: dict[tuple[float, float], int] = {}
    bad = []
    for k, ln in enumerate(lines):
        for end in (ln.points[0], ln.points[-1]):
            if end in seen and seen[end] != k:
                bad.append(end)
            seen[end] = k
    return bad


def through_cards(layout: Layout, lines: Sequence[Line], wide: bool = False) -> list[str]:
    """Lines passing through a card that is not one of their ends."""
    boxes = {
        t: (layout.x(t, wide), top, layout.x(t, wide) + CARD_W, top + CARD_H)
        for t, (_, top) in layout.cards.items()
    }
    bad = []
    for ln in lines:
        for (x0, y0), (x1, y1) in zip(ln.points, ln.points[1:], strict=False):
            for t, (bx0, by0, bx1, by1) in boxes.items():
                if t in (ln.source, ln.target):
                    continue
                if (
                    min(x0, x1) < bx1
                    and max(x0, x1) > bx0
                    and min(y0, y1) < by1
                    and max(y0, y1) > by0
                ):
                    bad.append(f"{ln.source}->{ln.target} through {t}")
    return bad


def needs(deps: Relation) -> dict[str, set[str]]:
    """What each task depends on, directly or not."""
    out: dict[str, set[str]] = {}

    def below(t: str, seen: set[str]) -> set[str]:
        if t in out:
            return out[t]
        found: set[str] = set()
        for d in deps.get(t, []):
            if d not in seen:
                found |= {d} | below(d, seen | {d})
        out[t] = found
        return found

    for t in list(deps):
        below(t, {t})
    return out


# ---------------------------------------------------------------------- the order and columns


@mark.use_case("UC-DIRECT-004")
def test_a_task_is_left_of_every_task_it_depends_on_directly_or_not() -> None:
    tasks, deps, fus, refs = synthetic(160, 0.3, 2)
    closure = needs(deps)
    for chosen in relation_choices():
        for everything in (False, True):
            layout = flow_layouts(tasks, deps, fus, refs)[layout_key(everything, chosen)]
            col = {t: c for t, (c, _) in layout.cards.items()}
            for a, below in closure.items():
                for b in below:
                    if a in col and b in col:
                        assert col[a] < col[b], (chosen, a, b)
    # The user's screenshot: the flow completes from right to left.
    tasks, deps = s1()
    col = {t: c for t, (c, _) in lay_out(tasks, deps).cards.items()}
    for a, below in needs(deps).items():
        for b in below:
            assert col[a] < col[b], (a, b)


@mark.use_case("UC-DIRECT-004")
def test_tasks_free_of_dependencies_go_newest_left() -> None:
    # Unrelated tasks: newest first, by the latest ledger record and not by registration.
    tasks = [task(1, when="2026-11-09"), task(2, when="2026-11-03"), task(3, when="2026-11-05")]
    assert sequence(tasks, {}) == ["T001", "T003", "T002"]
    # A tie goes to the later registration; the time falls back to the latest activity.
    tied = [task(1, when="2026-11-05"), task(2, when="2026-11-05")]
    assert sequence(tied, {}) == ["T002", "T001"]
    plain = FlowTask("T009", "t", "a", "code", "accepted", 9, "2026-12-01")
    assert sequence([*tied, plain], {})[0] == "T009"
    # A dependency wins over time: T001 (old) depends on T003 (new), so it comes first.
    assert sequence(tasks, {"T001": ["T002"], "T002": ["T003"]}) == ["T001", "T002", "T003"]
    # Elsewhere no swap of neighbours can put them more in time order: every neighbour out of
    # time order is joined to it by a dependency path.
    tasks, deps, _, _ = synthetic(200, 0.3, 0)
    order = sequence(tasks, deps)
    when = {t.task_id: (t.when, t.seq) for t in tasks}
    closure = needs(deps)
    assert sorted(order) == sorted(when)
    for u, v in pairwise(order):
        if when[u] < when[v]:
            assert v in closure.get(u, set()), (u, v)
    # The columns keep that order from left to right.
    layout = lay_out(tasks, deps)
    flat = [t for column in layout.columns for t in column]
    assert flat == order


@mark.use_case("UC-DIRECT-004")
def test_a_task_made_from_a_follow_up_is_left_of_its_source_when_shown() -> None:
    # T285's case (T352): its follow-ups became T292 and T297, but archiving it made it the
    # newest. Shown, the follow-ups put it right of both; unticked, time alone orders them.
    tasks = [
        task(285, when="2026-11-24T14:58"),
        task(292, when="2026-11-18T03:00"),
        task(297, when="2026-11-18T05:00"),
        task(300, when="2026-11-20T00:00"),
    ]
    fus = {"T292": ["T285"], "T297": ["T285"]}
    assert sequence(tasks, {}) == ["T285", "T300", "T297", "T292"]
    assert sequence(tasks, {}, fus) == ["T300", "T297", "T292", "T285"]
    shown = lay_out(tasks, {}, fus)
    col = {t: c for t, (c, _) in shown.cards.items()}
    assert col["T292"] < col["T285"] and col["T297"] < col["T285"]
    assert [t for c in lay_out(tasks, {}).columns for t in c] == sequence(tasks, {})
    # A dependency wins over a follow-up: the source depending on the task made from its
    # follow-up stays left of it, and the rest still holds.
    deps = {"T285": ["T292"]}
    order = sequence(tasks, deps, fus)
    assert order.index("T285") < order.index("T292")
    assert order.index("T297") < order.index("T285")
    # Through a chain of follow-ups and dependencies alike.
    chain = sequence(tasks, {"T292": ["T300"]}, {"T300": ["T285"]})
    assert chain.index("T292") < chain.index("T300") < chain.index("T285")
    # On a larger board: every follow-up shown (they agree with the dependencies there) puts
    # its task left of the source, and a neighbour out of time order is joined to it by a path
    # of dependencies and follow-ups.
    tasks, deps, fus, refs = synthetic(200, 0.3, 0)
    assert sum(len(v) for v in fus.values()) > 2
    layout = flow_layouts(tasks, deps, fus, refs)["all:dep,fu"]
    col = {t: c for t, (c, _) in layout.cards.items()}
    for a, bs in fus.items():
        for b in bs:
            assert col[a] < col[b], (a, b)
    joined = {t: list(deps.get(t, [])) + list(fus.get(t, [])) for t in set(deps) | set(fus)}
    closure = needs(joined)
    order = sequence(tasks, deps, fus)
    when = {t.task_id: (t.when, t.seq) for t in tasks}
    for u, v in pairwise(order):
        if when[u] < when[v]:
            assert v in closure.get(u, set()), (u, v)
    assert [t for c in layout.columns for t in c] == order


@mark.use_case("UC-DIRECT-004")
def test_no_line_within_a_column_and_a_lane_holds_six_per_column() -> None:
    tasks, deps, fus, refs = synthetic(160, 0.3, 1)
    layouts = flow_layouts(tasks, deps, fus, refs)
    for chosen in relation_choices():
        layout = layouts[layout_key(True, chosen)]
        col = {t: c for t, (c, _) in layout.cards.items()}
        for a, bs in deps.items():
            for b in bs:
                assert col[a] != col[b], (a, b)
        for a, bs in fus.items():
            for b in bs:
                # A follow-up keeps its ends apart only while it is shown.
                if "fu" in chosen:
                    assert col[a] != col[b], (a, b)
        repo = {t.task_id: t.repo for t in tasks}
        for column in layout.columns:
            for r in set(repo[t] for t in column):
                assert sum(1 for t in column if repo[t] == r) <= LANE_CAPACITY
    # Unrelated tasks of one repository fill columns of six; another repository shares them.
    tasks = [task(n) for n in range(1, 2 * LANE_CAPACITY + 2)]
    assert [len(c) for c in lay_out(tasks, {}).columns] == [6, 6, 1]
    mixed = lay_out([task(1, "a"), task(2, "b"), task(3, "a")], {}, capacity=1)
    assert mixed.columns == [["T003", "T002"], ["T001"]]


@mark.use_case("UC-DIRECT-004")
def test_lanes_follow_the_repositories_latest_activity() -> None:
    tasks = [
        FlowTask("T001", "t", "old", "code", "accepted", 1, "2026-11-01"),
        FlowTask("T002", "t", "new", "code", "accepted", 2, "2026-11-09"),
        FlowTask("T003", "t", "mid", "code", "accepted", 3, "2026-11-05"),
    ]
    layout = lay_out(tasks, {"T002": ["T001"]})
    assert [lane.repo for lane in layout.lanes] == ["new", "mid", "old"]
    for upper, lower in zip(layout.lanes, layout.lanes[1:], strict=False):
        assert upper.bottom == lower.top
    for t, (_, top) in layout.cards.items():
        lane = next(
            ln for ln in layout.lanes if ln.repo == next(x.repo for x in tasks if x.task_id == t)
        )
        assert lane.top < top and top + CARD_H < lane.bottom


# ---------------------------------------------------------------------- the lines


@mark.use_case("UC-DIRECT-004")
def test_arrows_run_in_the_order_of_the_work() -> None:
    tasks, deps = s1()
    layout = lay_out(tasks, deps, references={"T302": ["T297"]})
    by_pair = {(ln.source, ln.target): ln for ln in layout.lines}
    # From the task depended on to the task depending on it: every arrow points left, into
    # the right side of the dependent and out of the left side of the one it depends on.
    assert set(by_pair) == {(b, a) for a, bs in deps.items() for b in bs}
    for (b, a), ln in by_pair.items():
        assert ln.kind == "dep"
        assert ln.points[0] == (layout.x(b), ln.points[0][1])
        assert ln.points[-1] == (layout.x(a) + CARD_W, ln.points[-1][1])
        assert ln.points[-2][0] > ln.points[-1][0]
    # A reference runs from the referenced task to the task referencing it.
    (ref,) = [ln for ln in layout.wide if ln.kind == "ref"]
    assert (ref.source, ref.target) == ("T297", "T302")
    # A follow-up from its source to the task made from it.
    fu = lay_out([task(1), task(2)], {}, follow_ups={"T002": ["T001"]}).lines
    assert [(ln.kind, ln.source, ln.target) for ln in fu] == [("fu", "T001", "T002")]


@mark.use_case("UC-DIRECT-004")
def test_the_screenshot_has_no_crossings_and_nothing_shared() -> None:
    tasks, deps = s1()
    layout = lay_out(tasks, deps)
    assert len(layout.lines) == 9
    assert crossings(layout.lines) == 0
    assert shared_ports(layout.lines) == []
    assert shared_channels(layout.lines) == []
    assert through_cards(layout, layout.lines) == []
    # Orthogonal: every move is horizontal or vertical.
    for ln in layout.lines:
        for (x0, y0), (x1, y1) in zip(ln.points, ln.points[1:], strict=False):
            assert x0 == x1 or y0 == y1


@mark.use_case("UC-DIRECT-004")
def test_every_line_has_its_own_ports_and_channels_with_and_without_references() -> None:
    tasks, deps, fus, refs = synthetic(120, 0.3, 1.5)
    layout = lay_out(tasks, deps, fus, refs)
    assert layout.peek is not layout.lines and any(ln.kind == "ref" for ln in layout.wide)
    for name, lines, wide in (
        ("lines", layout.lines, False),
        ("peek", layout.peek, False),
        ("wide", layout.wide, True),
    ):
        assert shared_ports(lines) == [], name
        assert shared_channels(lines) == [], name
        assert through_cards(layout, lines, wide) == [], name


@mark.use_case("UC-DIRECT-004")
def test_references_never_move_a_card() -> None:
    tasks, deps, fus, refs = synthetic(120, 0.3, 1.5)
    bare = lay_out(tasks, deps, fus)
    layout = lay_out(tasks, deps, fus, refs)
    # The same columns, order and heights with or without references to route.
    assert layout.columns == bare.columns and layout.cards == bare.cards
    assert layout.xs == bare.xs and layout.lines == bare.lines
    # Shown, the gaps only widen: the columns keep their order and only move right.
    assert len(layout.xs_wide) == len(layout.xs)
    assert all(w >= x for w, x in zip(layout.xs_wide, layout.xs, strict=True))
    assert all(w >= g for w, g in zip(layout.gaps_wide, layout.gaps, strict=True))
    assert layout.xs_wide != layout.xs  # some gap needed more channels
    # Hovered (the peek routes) the gaps stay as they are: the lines fit in them.
    for ln in layout.peek:
        for x, _ in ln.points:
            assert x <= layout.width
    # Every route keeps to its gaps: between a column's right side and the next one's left.
    starts = [*layout.xs, layout.width]
    for ln in layout.lines + layout.peek:
        for (x0, _), (x1, _) in zip(ln.points, ln.points[1:], strict=False):
            if x0 == x1:  # a channel
                c = max(i for i, s in enumerate(layout.xs) if s <= x0)
                assert layout.xs[c] + CARD_W < x0 < starts[c + 1]


@mark.use_case("UC-DIRECT-004")
def test_the_choices_the_page_offers() -> None:
    assert relation_choices() == [("dep", "fu", "ref"), ("dep", "fu"), ("dep", "ref"), ("dep",)]
    assert layout_key(False, ("fu",)) == "live:dep,fu"
    tasks = [task(1, status="archived"), task(2, status="archived"), task(3, status="archived")]
    tasks += [task(n) for n in range(4, 9)]
    deps = {"T004": ["T001"]}
    fus = {"T005": ["T002"], "T008": ["T007"]}
    refs = {"T006": ["T003"]}
    layouts = flow_layouts(tasks, deps, fus, refs)
    assert len(layouts) == 8
    for chosen in relation_choices():
        live = layouts[layout_key(False, chosen)]
        every = layouts[layout_key(True, chosen)]
        assert set(every.cards) == {t.task_id for t in tasks}
        # Without the archived tasks: those a dependency, or a follow-up shown, joins to a board
        # task are context; a reference never makes context (the user's choice, T304).
        expected = (
            {f"T{n:03d}" for n in range(4, 9)} | {"T001"} | ({"T002"} if "fu" in chosen else set())
        )
        assert set(live.cards) == expected, chosen
        kinds = {ln.kind for ln in every.lines}
        assert kinds == ({"dep", "fu"} if "fu" in chosen else {"dep"}), chosen
    # References do not change where the cards are: one layout for both, four in all.
    for everything in (False, True):
        assert (
            layouts[layout_key(everything, ("fu", "ref"))]
            is layouts[layout_key(everything, ("fu",))]
        )
        assert layouts[layout_key(everything, ("ref",))] is layouts[layout_key(everything, ())]
    assert len({id(layout) for layout in layouts.values()}) == 4
    # Unticked, a follow-up neither draws a line nor keeps its two ends apart.
    col = {t: c for t, (c, _) in layouts["all:dep"].cards.items()}
    assert col["T008"] == col["T007"]
    col = {t: c for t, (c, _) in layouts["all:dep,fu"].cards.items()}
    assert col["T008"] != col["T007"]


@mark.use_case("UC-DIRECT-004")
def test_the_layout_is_deterministic() -> None:
    tasks, deps, fus, refs = synthetic(120, 0.3, 1.5)
    first = flow_layouts(tasks, deps, fus, refs)
    again = flow_layouts(list(reversed(tasks)), dict(reversed(deps.items())), fus, refs)
    for key in first:
        assert first[key] == again[key], key


@mark.use_case("UC-DIRECT-004")
def test_a_cycle_unknown_tasks_and_nothing_do_not_stop_the_layout() -> None:
    layout = lay_out([task(1), task(2)], {"T001": ["T002", "T404"], "T002": ["T001"]})
    assert set(layout.cards) == {"T001", "T002"}
    assert len(layout.lines) == 2
    assert lay_out([], {}).columns == []
    # References within one column turn back in the gap to its right.
    layout = lay_out([task(1), task(2)], {}, references={"T002": ["T001"]})
    assert len(layout.columns) == 1
    (ref,) = layout.wide
    assert ref.points[0][0] == ref.points[-1][0] == layout.xs[0] + CARD_W
    assert shared_ports(layout.wide) == []


# CPU seconds the reference work below takes on the development machine, idle.
REFERENCE_SECONDS = 0.2


def reference_work() -> int:
    """Plain Python work (a dictionary and a sort) to scale the time limit to the machine."""
    rng = random.Random(1)
    counts: dict[int, int] = {}
    for i in range(400_000):
        k = rng.randrange(5000)
        counts[k] = counts.get(k, 0) + i % 7
    return len(sorted(counts.items(), key=lambda kv: (kv[1], kv[0])))


@mark.use_case("UC-DIRECT-004")
def test_every_task_and_every_choice_in_under_a_second() -> None:
    # About the user's whole ledger (T303's S4: 298 tasks, 710 references). The limit is one
    # second on the development machine (where the layouts take about 0.5 s), scaled by how
    # long the reference work takes here and now: the gate runs the tests in parallel, which
    # slows every process down alike.
    tasks, deps, fus, refs = synthetic(300, 0.2, 2.4)
    assert sum(len(v) for v in refs.values()) > 650
    started = time.process_time()
    reference_work()
    scale = max(1.0, (time.process_time() - started) / REFERENCE_SECONDS)
    started = time.process_time()
    layouts = flow_layouts(tasks, deps, fus, refs)
    spent = time.process_time() - started
    assert len(layouts) == 8
    assert spent < 1.0 * scale, (spent, scale)
