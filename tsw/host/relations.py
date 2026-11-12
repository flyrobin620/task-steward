"""References between tasks, for the Dashboard (DN006 §8.10, T278).

Task A references task B when A's work takes B as background or a model without waiting for it
(waiting is a dependency). It is worked out from the projection when a page is generated; no
event records it:

1. B is in the recommended read set of one of A's attempts (``read_set``, chosen by the
   orchestrator when it dispatches or continues A, DN007 §7.2), or
2. A's title, goal or acceptance at its current revision names B by its task ID.

A task ID is ``T`` and 3 to 6 digits with no letter, digit or underscore on either side, and only
IDs of tasks in the ledger count, so DN002's transition numbers (``T01``-``T28``) are not tasks.
A pair already joined by a dependency or a follow-up, either way, is not also a reference: that
line says more. Reports, notes, findings and questions are not read: they mention other tasks in
passing too often.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from tsw.machine.model import loads

MENTION = re.compile(r"(?<![A-Za-z0-9_])T\d{3,6}(?![A-Za-z0-9_])")
# Where a reference was found: the read set (with its attempt) or a field of the task.
FIELD_LABELS = {"title": "标题", "goal": "目标", "acceptance": "验收"}


def mentions(text: str) -> list[str]:
    """The task IDs named in a text, in order of first mention."""
    seen: dict[str, None] = {}
    for m in MENTION.finditer(text):
        seen.setdefault(m.group(0), None)
    return list(seen)


def label(via: str) -> str:
    """How a reference was found, for the user: ``read_set:a1`` → 推荐读取集合（a1）."""
    if via.startswith("read_set:"):
        return f"推荐读取集合（{via.split(':', 1)[1]}）"
    return FIELD_LABELS.get(via, via)


def find_references(
    tasks: Iterable[Mapping[str, Any]],
    read_sets: Mapping[str, Mapping[str, Iterable[str]]],
    joined: Iterable[tuple[str, str]],
) -> dict[str, dict[str, list[str]]]:
    """Each referring task's references: ``{A: {B: [how A came to reference B, …]}}``.

    ``tasks`` are projection rows (``task_id``, ``title``, ``goal``, ``acceptance``),
    ``read_sets`` maps a task to its attempts' read sets, and ``joined`` lists the pairs already
    related by a dependency or a follow-up (in either order).
    """
    rows = list(tasks)
    known = {str(t["task_id"]) for t in rows}
    skip = {frozenset((a, b)) for a, b in joined}
    out: dict[str, dict[str, list[str]]] = {}

    def add(a: str, b: str, via: str) -> None:
        if b == a or b not in known or frozenset((a, b)) in skip:
            return
        found = out.setdefault(a, {}).setdefault(b, [])
        if via not in found:
            found.append(via)

    for t in rows:
        a = str(t["task_id"])
        for attempt, names in sorted(read_sets.get(a, {}).items()):
            for b in sorted(str(n) for n in names):
                add(a, b, f"read_set:{attempt}")
        acceptance: list[Any] = loads(t.get("acceptance"), []) or []
        fields = (
            ("title", str(t.get("title") or "")),
            ("goal", str(t.get("goal") or "")),
            ("acceptance", "\n".join(str(x) for x in acceptance)),
        )
        for name, text in fields:
            for b in mentions(text):
                add(a, b, name)
    return out


def references(conn: sqlite3.Connection) -> dict[str, dict[str, list[str]]]:
    """Every task's references, from the projection (see the module's rules)."""
    tasks = [dict(r) for r in conn.execute("SELECT task_id, title, goal, acceptance FROM tasks")]
    read_sets: dict[str, dict[str, list[str]]] = {}
    for r in conn.execute("SELECT task_id, attempt, read_set FROM attempts"):
        names: list[Any] = loads(r["read_set"], []) or []
        read_sets.setdefault(str(r["task_id"]), {})[str(r["attempt"])] = [str(n) for n in names]
    joined = [
        (str(r["task_id"]), str(r["dep_id"]))
        for r in conn.execute("SELECT task_id, dep_id FROM dependencies")
    ] + [
        (str(r["task_id"]), str(r["converted_to"]))
        for r in conn.execute(
            "SELECT task_id, converted_to FROM follow_ups WHERE converted_to IS NOT NULL"
        )
    ]
    return find_references(tasks, read_sets, joined)
