"""The code's transition tables must equal DN002's tables (§3.2 and §3.3)."""

from __future__ import annotations

import re

import pytest

from tests.support import REPO_ROOT
from tsw.machine.model import OCCUPYING, STATES, TRANSITIONS, WORKER_TRANSITIONS

DN002 = (REPO_ROOT / "docs" / "design" / "DN002-task-state-machine.md").read_text(encoding="utf-8")


def _design_rows(prefix: str) -> dict[str, tuple[frozenset[str], str]]:
    rows: dict[str, tuple[frozenset[str], str]] = {}
    for line in DN002.splitlines():
        match = re.match(rf"^\| ({prefix}\d\d) \| ([^|]+) \| ([^|]+) \|", line)
        if not match:
            continue
        sources = frozenset(
            s.strip() for s in match.group(2).replace("（新建）", "").split("、") if s.strip()
        )
        rows[match.group(1)] = (sources, match.group(3).strip())
    return rows


TASK_ROWS = _design_rows("T")
WORKER_ROWS = _design_rows("E")


@pytest.mark.parametrize(
    "tid",
    [pytest.param(t, marks=pytest.mark.transition(t)) for t in sorted(TASK_ROWS) if t != "T01"],
)
def test_task_transition_matches_design(tid: str) -> None:
    sources, target = TASK_ROWS[tid]
    assert TRANSITIONS[tid].sources == sources
    assert TRANSITIONS[tid].target == target


@pytest.mark.parametrize(
    "wid", [pytest.param(w, marks=pytest.mark.transition(w)) for w in sorted(WORKER_ROWS)]
)
def test_worker_transition_matches_design(wid: str) -> None:
    sources, target = WORKER_ROWS[wid]
    assert WORKER_TRANSITIONS[wid].sources == sources
    assert WORKER_TRANSITIONS[wid].target == target


def test_tables_cover_every_design_row_and_state() -> None:
    assert set(TRANSITIONS) == set(TASK_ROWS) - {"T01"}
    assert set(WORKER_TRANSITIONS) == set(WORKER_ROWS)
    targets = {t.target for t in TRANSITIONS.values()}
    assert targets <= set(STATES)
    assert {"dispatching", "in_progress", "waiting_user", "verifying"} == OCCUPYING
