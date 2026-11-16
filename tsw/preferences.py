"""The user's standing preferences as data (DN007 §6, §6.1, §6.2).

A preference lives only in the ledger (``user.preference_recorded`` / ``user.preference_revoked``,
projected into ``user_decisions`` and ``preference_revocations``). Everything here reads that
projection when asked, so a preference recorded, replaced or revoked takes effect at the next
retrieval or brief without touching the product: no skill, role text or code names a preference.

- :func:`standing_items`: the preferences that reach a scope and whether each is in force there;
- :func:`brief_lines`: the preferences the orchestrator added at dispatch, and the check of every
  cited one that is no longer the one in force for the task (§6.1).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# A preference number as the goal cites it: ``P-4``, not part of a longer word or number.
REF = re.compile(r"(?<![\w-])P-[1-9]\d*(?![\w-])")

Where = tuple[str, set[str]]
# The brief's preference lines sit between these markers, so a later brief can tell whether
# they changed even when a preference's words span lines (review r3 F5, r4 F6).
BLOCK = ("<!-- tsw:preferences -->", "<!-- /tsw:preferences -->")


def applies(scope: str, where: Where | None) -> bool:
    """Whether a preference of ``scope`` reaches a query about ``where`` = (repo, projects)."""
    if where is None or scope == "global":
        return True
    kind, _, name = scope.partition(":")
    return (kind == "repo" and name == where[0]) or (kind == "project" and name in where[1])


def revocations(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The revoked preferences: ref -> time, reason and the user's words (§6.2)."""
    return {
        str(r["ref"]): {
            "time": r["revoked_at"],
            "reason": r["reason"],
            "source": json.loads(r["verified_source"] or "{}"),
            "source_seq": r["source_seq"],
        }
        for r in conn.execute("SELECT * FROM preference_revocations")
    }


def load(conn: sqlite3.Connection, where: Where | None) -> list[dict[str, Any]]:
    """The standing preferences that reach ``where`` (all of them when None)."""
    return standing_items(
        list(conn.execute("SELECT * FROM user_decisions ORDER BY source_seq")),
        where,
        revocations(conn),
    )


def standing_items(
    rows: Iterable[Mapping[str, Any]],
    where: Where | None,
    revoked: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The standing preferences that reach ``where`` (all of them when None), each saying whether
    it is in force there: replaced by one of the same scope, revoked, or overridden by a narrower
    one that reaches ``where`` too and is itself still in force (DN007 §6, §6.2)."""
    gone = revoked or {}
    items = [_standing(r, gone.get(str(r["ref"]))) for r in rows]
    by_ref = {str(i["ref"]): i for i in items}

    def current(item: dict[str, Any]) -> dict[str, Any]:
        """The preference now in force in ``item``'s scope: follow same-scope replacements."""
        seen: set[str] = set()
        while item.get("superseded_by") and str(item["ref"]) not in seen:
            seen.add(str(item["ref"]))
            item = by_ref.get(str(item["superseded_by"]), item)
        return item

    overriders: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for old in item["supersedes"]:
            overriders.setdefault(str(old), []).append(item)
    out: list[dict[str, Any]] = []
    for item in items:
        if not applies(str(item["scope"]), where):
            continue
        # A narrower override keeps holding when it is itself replaced in its own scope, and
        # stops holding once that scope's preference is revoked.
        narrower = [
            n
            for n in {
                str(n["ref"]): n
                for n in (
                    current(o)
                    for o in overriders.get(str(item["ref"]), [])
                    if o["scope"] != item["scope"]
                )
            }.values()
            if not n.get("revoked")
        ]
        if where is None:
            if narrower:
                item["overridden_in"] = [{"ref": n["ref"], "scope": n["scope"]} for n in narrower]
        else:
            here = [n for n in narrower if applies(str(n["scope"]), where)]
            if here:
                item["overridden_by"] = here[0]["ref"]
                item["usable_as_basis"] = False
        out.append(item)
    return out


def in_force(item: Mapping[str, Any]) -> bool:
    return bool(item.get("usable_as_basis"))


def _standing(row: Mapping[str, Any], revoked: Mapping[str, Any] | None) -> dict[str, Any]:
    superseded = row["superseded_by"]
    item: dict[str, Any] = {
        "ref": row["ref"],
        "kind": row["kind"],
        "scope": row["scope"],
        "task_id": row["task_id"],
        "topic": row["topic"],
        "text": row["text"],
        "source": json.loads(row["verified_source"] or "{}"),
        "supersedes": json.loads(row["supersedes"] or "[]"),
        "superseded_by": superseded,
        "time": row["recorded_at"],
        "source_seq": row["source_seq"],
        "usable_as_basis": False if superseded or revoked else "in_scope",
    }
    if revoked:
        item["revoked"] = {"time": revoked["time"], "reason": revoked["reason"]}
    return item


# ---------------------------------------------------------------------- the brief's check


def cited(goal: str) -> list[str]:
    """The preference numbers a goal cites, in order, each once."""
    return list(dict.fromkeys(m.group(0) for m in REF.finditer(goal)))


def is_ref(name: str) -> bool:
    """Whether a read-set entry is a preference number rather than a task (DN007 §6.1)."""
    return REF.fullmatch(name) is not None


Views = tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]


def _views(conn: sqlite3.Connection, repo: str, refs: Sequence[str]) -> Views:
    """Every preference as recorded, and those reaching a task of ``repo`` with their state there.

    A project preference that is cited is taken to reach the task: the projection does not know a
    task's projects (they live on the L2 card), so only replacement and override are checked.
    """
    rows = list(conn.execute("SELECT * FROM user_decisions ORDER BY source_seq"))
    gone = revocations(conn)
    every = {str(i["ref"]): i for i in standing_items(rows, None, gone)}
    projects = {repo}
    for ref in refs:
        kind, _, name = str((every.get(ref) or {}).get("scope") or "").partition(":")
        if kind == "project":
            projects.add(name)
    here = {str(i["ref"]): i for i in standing_items(rows, (repo, projects), gone)}
    return every, here


def supplied_problems(conn: sqlite3.Connection, repo: str, refs: Sequence[str]) -> list[str]:
    """Why preferences the orchestrator adds at dispatch cannot be added: each must exist and be
    in force for a task of ``repo`` (DN007 §6.1). Empty when all can."""
    if not refs:
        return []
    every, here = _views(conn, repo, refs)
    return [note for ref in refs if (note := _note(ref, every, here, repo))]


def brief_lines(
    conn: sqlite3.Connection, repo: str, goal: str, supplied: Sequence[str] = ()
) -> str:
    """The brief's preference lines (DN007 §6.1), read from the ledger each time a brief is
    written:

    - "派发时补充的适用偏好": the preferences the orchestrator added to the read set at dispatch
      or continuation (the goal is not rewritten for them), with their recorded words;
    - "偏好核对": cited preferences, in the goal or added, that are not the one in force for a
      task of ``repo`` any more (replaced, overridden, out of scope, revoked or unknown).
    """
    added = list(dict.fromkeys(supplied))
    refs = list(dict.fromkeys([*cited(goal), *added]))
    if not refs:
        return ""
    every, here = _views(conn, repo, refs)
    out = ""
    if added:
        shown = [_shown(every[r]) if r in every else f"{r}（不存在）" for r in added]
        out += "- 派发时补充的适用偏好（主会话选定，DN007 §6.1）：" + "；".join(shown) + "\n"
    notes = [note for ref in refs if (note := _note(ref, every, here, repo))]
    if notes:
        out += (
            "- 偏好核对（tsw 按账本当前状态核对目标中引用与派发时补充的长期偏好，DN007 §6.1）："
            + "；".join(notes)
            + "。以现在有效的偏好为准，有疑问时用 `context_decisions` 查或问用户\n"
        )
    return f"{BLOCK[0]}\n{out}{BLOCK[1]}\n" if out else ""


def block(brief: str) -> str:
    """The preference lines a brief holds (between the markers), or empty."""
    start = brief.find(BLOCK[0])
    end = brief.find(BLOCK[1], start)
    return brief[start:end] if start >= 0 and end >= 0 else ""


def _shown(item: Mapping[str, Any]) -> str:
    # The user's words as recorded, spaces and line breaks included (review r4 F6).
    return f"{item['ref']}（{item['scope']}）：“{item['text']}”"


def _note(
    ref: str,
    every: Mapping[str, Mapping[str, Any]],
    here: Mapping[str, Mapping[str, Any]],
    repo: str,
) -> str:
    item = every.get(ref)
    if item is None:
        return f"{ref} 不存在"
    if ref not in here:
        return f"{ref}（{item['scope']}）的范围不包括仓库 {repo}"
    if item.get("revoked"):
        return f"{ref} 已撤销（理由：{item['revoked']['reason']}）"
    # Follow same-scope replacements to the one now in that scope, then see whether a narrower
    # preference overrides that one for this repository (review r1 F2).
    now = item
    seen: set[str] = set()
    while now.get("superseded_by") and str(now["ref"]) not in seen:
        seen.add(str(now["ref"]))
        now = every.get(str(now["superseded_by"]), now)
    replaced = now is not item
    if now.get("revoked"):
        return f"{ref} 已被 {now['ref']} 替代，{now['ref']} 又已撤销"
    overridden = (here.get(str(now["ref"])) or {}).get("overridden_by")
    narrower = f"被更窄范围的 {_shown(every[str(overridden)])}覆盖" if overridden else ""
    if replaced and narrower:
        return f"{ref} 已被 {now['ref']} 替代，{now['ref']} 在仓库 {repo} 中又{narrower}"
    if replaced:
        return f"{ref} 已被替代，现在有效的是 {_shown(now)}"
    if narrower:
        return f"{ref} 在仓库 {repo} 中{narrower}"
    return ""
