# Tool inputs are untyped JSON checked field by field; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Preference candidates collected when a task ends (DN007 §6.3).

A subagent reads the user's words recorded for a task and proposes what the user said that
should hold for later; the session that started it records them here. A candidate is not a
preference: only the user turns one into a preference (``preference_record`` with ``candidate``)
or turns it down (``preference_candidate_dismiss``).

- :func:`materials`: the task's user words in the ledger, each with a number to cite;
- ``preference_candidates_record`` (worker, orchestrator): one scan and the candidates it found,
  each quote checked to stand in the material it cites;
- ``preference_candidate_dismiss`` (orchestrator, user): the user turns a candidate down;
- :func:`sources`: what ``preference_sources`` hands the session, prompt for the subagent included.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tsw import preferences
from tsw.commands import Command, CommitContext
from tsw.errors import rejected
from tsw.events import Event, EventDraft, sources_in
from tsw.machine import guards
from tsw.machine.ask_user import is_automated
from tsw.machine.base import (
    ToolEnv,
    integer,
    items,
    mapping,
    opt_text,
    strings,
    text,
    tool,
    worker_attempt,
)
from tsw.machine.model import dumps, loads, row_dict, task

MAX_CANDIDATES = 5
MAX_QUOTE_CHARS = 2000
MAX_TOPIC_CHARS = 120
MAX_REASON_CHARS = 800
# What the prompt carries of the task's words; the oldest are dropped first, the goal never.
MAX_MATERIAL_CHARS = 60000
DISMISS_TARGET = "dismiss"
SKIP_CATEGORIES = (
    "same_as",
    "project_decision",
    "product_behavior",
    "task_requirement",
    "not_standing",
)
# A worker scans while its session holds the task, until the user accepts it (as follow-ups).
WORKER_STATES = ("in_progress", "waiting_user", "verifying", "done")
# Events whose user words are about preferences themselves, not the task's words.
OWN_EVENTS = (
    "user.preference_candidate",
    "user.preference_candidate_dismissed",
    "task.preference_scanned",
)
PROMPT_FILE = Path(__file__).resolve().parent.parent / "host" / "preference_scan.md"


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def normalized(value: str) -> str:
    """Runs of white space as one space (topics, reasons)."""
    return " ".join(value.split())


def compact(value: str) -> str:
    """Words compared without their spacing: a model quoting Chinese often adds or drops a space
    the user did not type, which does not make them other words."""
    return "".join(value.split())


# ---------------------------------------------------------------------- scopes


def scope_of(value: str) -> str:
    """``global``, ``repo:<name>`` or ``project:<id>``."""
    kind, _, name = value.partition(":")
    if value == "global" or (kind in ("repo", "project") and name.strip() and name == name.strip()):
        return value
    raise rejected(
        "scope 应为 global、repo:<仓库> 或 project:<项目>", reason="invalid_input", field="scope"
    )


def narrower_or_same(new: str, old: str) -> bool:
    return old in (new, "global")


def user_channels(env: ToolEnv) -> tuple[str, ...]:
    """The user's own terminal commands record their source on the direct channels; a session
    quotes the user from a conversation (DN006 §8.10)."""
    return guards.DIRECT_CHANNELS if env.caller.role == "user" else guards.USER_CHANNELS


def revoked(conn: sqlite3.Connection, ref: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM preference_revocations WHERE ref = ?", (ref,)).fetchone()
        is not None
    )


# ---------------------------------------------------------------------- materials


Renames = list[tuple[int, str, str]]  # (sequence, old id, new id) of each task.renumbered


def renames_in(events: Iterable[Event]) -> Renames:
    return [
        (e.sequence, str(e.data.get("from") or ""), str(e.task_id))
        for e in events
        if e.type == "task.renumbered" and e.task_id is not None and e.data.get("from")
    ]


def owner(event: Event, renames: Renames) -> str | None:
    """The task an event belongs to now: what an old number did before a renumbering belongs to
    the task that took the new number (as the index reads it, DN005 §4.4; PR #54 review)."""
    task_id = event.task_id
    for sequence, old, new in renames:
        if task_id == old and event.sequence < sequence:
            task_id = new
    return task_id


def _messages(conn: sqlite3.Connection, task_id: str) -> dict[str, Mapping[str, Any]]:
    """The hook's records of the user's messages for the task, injected ones included: those
    recorded before the hook knew a marker (a subagent's ``<agent-message``) are never material
    but still stand for the words an answer cites (PR #54 review)."""
    return {
        str(m["message_id"]): dict(m)
        for m in conn.execute(
            "SELECT m.*, v.via FROM session_messages m "
            "LEFT JOIN session_message_via v ON v.message_id = m.message_id "
            "WHERE m.task_id = ? ORDER BY m.sequence",
            (task_id,),
        )
    }


def _event_words(
    events: Iterable[Event],
    task_id: str,
    messages: Mapping[str, Mapping[str, Any]],
    renames: Renames,
) -> tuple[list[dict[str, Any]], int | None]:
    """The user's words the task's events carry, and the sequence of the event that set the goal
    last (None when none of ``events`` did)."""
    found: list[dict[str, Any]] = []
    goal_seq: int | None = None
    for event in events:
        if owner(event, renames) != task_id:
            continue
        if "goal" in event.data or event.type == "task.registered":
            goal_seq = event.sequence
        if event.type in OWN_EVENTS:
            continue
        seen = 0
        for source in sources_in(dict(event.data)):
            cited = [r for r in str(source.get("message_ref") or "").split(",") if r]
            if cited and all(r in messages for r in cited):
                continue  # the hook's own record of these words is cited instead
            if is_automated(str(source.get("text") or "")):
                continue
            seen += 1
            found.append(
                {
                    "ref": f"E{event.sequence}" if seen == 1 else f"E{event.sequence}-{seen}",
                    "kind": event.type,
                    "channel": str(source.get("channel") or ""),
                    "verified": True,
                    "sequence": event.sequence,
                    "time": event.time,
                    "text": str(source.get("text") or ""),
                }
            )
    return found, goal_seq


def _message_words(messages: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ref": message_id,
            "kind": "input.session_message",
            "channel": "in-session",
            "verified": True,
            "sequence": int(message["sequence"]),
            "time": None,
            "text": str(message["text"]),
            # The host said the words came through workdeck: still the user's (DN006 §8.11).
            **({"via": str(message["via"])} if message.get("via") else {}),
        }
        for message_id, message in messages.items()
        if not is_automated(str(message["text"]))
    ]


def materials(
    conn: sqlite3.Connection, events: Iterable[Event], task_id: str
) -> list[dict[str, Any]]:
    """The user's words recorded for the task, in ledger order (DN007 §6.3):

    - ``M-…``: what the user typed in the worker's session, as the hook recorded it;
    - ``E<seq>`` (``E<seq>-<k>`` for a second source in one event): the user's words a task event
      carries as a ``VerifiedSource``, except those made of hook-recorded messages (cited as
      ``M-…``) and those about preferences themselves;
    - ``goal@r<revision>``: the current goal, which the orchestrator wrote (not verified).

    Messages a host injected are never material, even if recorded before the hook knew them.
    """
    row = task(conn, task_id)
    every = list(events)
    messages = _messages(conn, task_id)
    found, goal_seq = _event_words(every, task_id, messages, renames_in(every))
    found += _message_words(messages)
    found.sort(key=lambda m: int(m["sequence"]))
    goal = {
        "ref": f"goal@r{int(row['revision'])}",
        "kind": "goal",
        "channel": "orchestrator",
        "verified": False,
        "sequence": goal_seq if goal_seq is not None else int(row["registered_seq"]),
        "time": None,
        "text": str(row["goal"] or ""),
    }
    return [goal, *found]


def _trimmed(found: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """The materials that fit the prompt: the goal, then the newest words."""
    goal, rest = found[0], list(found[1:])
    budget = MAX_MATERIAL_CHARS - len(goal["text"])
    kept: list[dict[str, Any]] = []
    for item in reversed(rest):
        budget -= len(item["text"])
        if budget < 0:
            break
        kept.append(item)
    kept.reverse()
    return [goal, *kept], len(kept) < len(rest)


# ---------------------------------------------------------------------- candidates as data


def candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    """One candidate as the tools show it."""
    return {
        "ref": row["ref"],
        "task_id": row["task_id"],
        "revision": row["revision"],
        "attempt": row["attempt"],
        "quote": row["quote"],
        "source_ref": row["source_ref"],
        "channel": row["channel"],
        "verified": bool(row["verified"]),
        "scope": row["scope"],
        "topic": row["topic"],
        "reason": row["reason"],
        "supersedes": loads(row["supersedes"], []),
        "status": row["status"],
        "preference": row["preference"],
        "resolution": loads(row["resolution"], None),
        "recorded_by": loads(row["recorded_by"], {}),
        "recorded_at": row["recorded_at"],
        "source_seq": row["source_seq"],
    }


def of_task(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    return [
        candidate(r)
        for r in conn.execute(
            "SELECT * FROM preference_candidates WHERE task_id = ? ORDER BY source_seq",
            (task_id,),
        )
    ]


def open_ones(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every candidate still waiting for the user (``attention``)."""
    return [
        {
            "ref": r["ref"],
            "task_id": r["task_id"],
            "quote": r["quote"],
            "scope": r["scope"],
            "topic": r["topic"],
            "supersedes": loads(r["supersedes"], []),
        }
        for r in conn.execute(
            "SELECT * FROM preference_candidates WHERE status = 'open' ORDER BY source_seq"
        )
    ]


def every(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        candidate(r)
        for r in conn.execute("SELECT * FROM preference_candidates ORDER BY source_seq")
    ]


def scan_status(
    conn: sqlite3.Connection,
    task_id: str,
    read: Callable[[int], Iterable[Event]] | None = None,
) -> dict[str, Any]:
    """Whether the task's words were scanned (DN007 §6.3): ``done``, ``stale`` (the user's words
    reached the task after the scan: a message the hook recorded or, when ``read`` gives the
    ledger from a sequence on, a source in any of its events) or ``missing`` (no scan at the
    current revision)."""
    row = task(conn, task_id)
    last = row_dict(
        conn.execute(
            "SELECT * FROM preference_scans WHERE task_id = ? ORDER BY sequence DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    )
    if last is None or int(last["revision"]) != int(row["revision"]):
        state = "missing"
    else:
        through = int(last["scanned_through"])
        messages = _messages(conn, task_id)
        newer = [m for m in _message_words(messages) if int(m["sequence"]) > through]
        if read is not None and not newer:
            later = list(read(through))
            words, _ = _event_words(later, task_id, messages, renames_in(later))
            newer = [w for w in words if int(w["sequence"]) > through]
        state = "stale" if newer else "done"
    out: dict[str, Any] = {"state": state}
    if last is not None:
        out["last"] = {
            "sequence": last["sequence"],
            "revision": last["revision"],
            "attempt": last["attempt"],
            "scanned_through": last["scanned_through"],
            "candidates": loads(last["candidates"], []),
            **loads(last["skipped"], {}),
            "by": loads(last["by"], {}),
        }
    return out


# ---------------------------------------------------------------------- what the subagent reads


def prompt_text() -> str:
    """The subagent's instructions: one source, read at each call (CC007 §2)."""
    return PROMPT_FILE.read_text(encoding="utf-8")


def sources(
    conn: sqlite3.Connection,
    events: Iterable[Event],
    task_id: str,
    *,
    report_path: str | None = None,
    projects: Iterable[str] = (),
) -> dict[str, Any]:
    """What ``preference_sources`` returns: the task's words, the preferences in force for its
    repository and projects (the card's ``project_ids``, read by the caller), the candidates so
    far, and the prompt for the subagent (DN007 §6.3)."""
    row = task(conn, task_id)
    repo = str(row["repo"])
    where = (repo, {repo, *projects})
    ledger = list(events)
    found = materials(conn, ledger, task_id)
    through = max(int(m["sequence"]) for m in found)
    shown, truncated = _trimmed(found)
    standing = [
        {"ref": p["ref"], "scope": p["scope"], "topic": p["topic"], "text": p["text"]}
        for p in preferences.load(conn, where)
        if preferences.in_force(p)
    ]
    mine = of_task(conn, task_id)
    others = [
        c
        for c in every(conn)
        if c["status"] == "open"
        and c["task_id"] != task_id
        and preferences.applies(str(c["scope"]), where)
    ]
    shown_candidates = [
        {k: c[k] for k in ("ref", "task_id", "quote", "scope", "topic", "status", "preference")}
        for c in [*mine, *others]
    ]
    payload = {
        "task_id": task_id,
        "title": row["title"],
        "repo": repo,
        "revision": int(row["revision"]),
        "report_path": report_path,
        # ``via`` only where the host said the words came through workdeck (DN006 §8.11).
        "materials": [
            {k: m[k] for k in ("ref", "kind", "channel", "verified", "via", "text") if k in m}
            for m in shown
        ],
        "materials_truncated": truncated,
        "preferences_in_force": standing,
        "candidates_so_far": shown_candidates,
    }
    prompt = (
        prompt_text().rstrip()
        + "\n\n## 本任务的材料\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + "\n```\n"
    )
    return {
        **payload,
        "scanned_through": through,
        "scan": scan_status(conn, task_id, lambda since: (e for e in ledger if e.sequence > since)),
        "prompt": prompt,
        "next": "用 Agent 工具启动子代理（general-purpose），prompt 原样使用本结果的 prompt；"
        "拿到 JSON 后调用 preference_candidates_record（scanned_through 用本结果的值），"
        "没有候选也调用一次（CC007 §3）",
    }


# ---------------------------------------------------------------------- tools


def _writable(ctx: CommitContext, env: ToolEnv, task_id: str) -> tuple[dict[str, Any], str | None]:
    row = task(ctx.conn, task_id)
    status = str(row["status"])
    if env.caller.role == "worker":
        current = worker_attempt(ctx.conn, env, task_id)
        if status not in WORKER_STATES:
            raise rejected(
                "worker 只能在任务被用户验收之前、自己持有任务时记录偏好候选",
                reason="invalid_state",
                status=status,
            )
        return row, str(current["attempt"])
    if status == "archived":
        raise rejected("已归档的任务不能再记录偏好候选", reason="invalid_state")
    return row, None


def _candidate_input(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise rejected("候选应为对象", reason="invalid_input", field=f"candidates[{index}]")
    quote = text(raw, "quote").strip()
    topic = normalized(text(raw, "topic"))
    reason = normalized(text(raw, "reason"))
    if len(quote) > MAX_QUOTE_CHARS or len(topic) > MAX_TOPIC_CHARS:
        raise rejected("原话或主题过长", reason="invalid_input", field=f"candidates[{index}]")
    if len(reason) > MAX_REASON_CHARS:
        raise rejected("理由过长", reason="invalid_input", field=f"candidates[{index}].reason")
    return {
        "quote": quote,
        "source_ref": text(raw, "source_ref").strip(),
        "scope": scope_of(opt_text(raw, "scope") or "global"),
        "topic": topic,
        "reason": reason,
        "supersedes": list(
            dict.fromkeys(strings(items(raw, "supersedes", required=False), "supersedes"))
        ),
    }


def _skipped_input(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise rejected("skipped 的元素应为对象", reason="invalid_input", field=f"skipped[{index}]")
    category = text(raw, "category")
    if category not in SKIP_CATEGORIES:
        raise rejected(
            "skipped 的类别无效",
            reason="invalid_input",
            field=f"skipped[{index}].category",
            allowed=list(SKIP_CATEGORIES),
        )
    reason = normalized(text(raw, "reason", required=False))
    if len(reason) > MAX_REASON_CHARS:
        raise rejected("理由过长", reason="invalid_input", field=f"skipped[{index}].reason")
    out = {"source_ref": text(raw, "source_ref").strip(), "category": category, "reason": reason}
    same = opt_text(raw, "same_as")
    if same:
        out["same_as"] = same.strip()
    return out


def check_supersedes(conn: sqlite3.Connection, scope: str, refs: Sequence[str]) -> None:
    """Each preference to replace exists, is in force and is as wide as ``scope`` or wider."""
    for ref in refs:
        old = conn.execute(
            "SELECT scope, superseded_by FROM user_decisions WHERE ref = ?", (ref,)
        ).fetchone()
        if old is None:
            raise rejected("要替代的偏好不存在", reason="invalid_input", ref=ref)
        if old["superseded_by"]:
            raise rejected(
                "要替代的偏好已被替代", reason="invalid_input", ref=ref, by=old["superseded_by"]
            )
        if revoked(conn, ref):
            raise rejected("要替代的偏好已撤销", reason="invalid_input", ref=ref)
        if not narrower_or_same(scope, str(old["scope"])):
            raise rejected(
                "新偏好的范围须与被替代的相同或更窄",
                reason="invalid_input",
                ref=ref,
                scope=old["scope"],
            )


Known = list[tuple[str, str, str]]  # (words without spacing, scope, ref)


def _known_words(conn: sqlite3.Connection) -> Known:
    """Words already an open candidate (in its suggested scope) or a preference in force (in the
    scope and words the user recorded: a candidate the user confirmed counts as the preference it
    became, review r2 F3), with their scope."""
    known: Known = [
        (compact(str(r["quote"])), str(r["scope"]), str(r["ref"]))
        for r in conn.execute(
            "SELECT ref, quote, scope FROM preference_candidates WHERE status = 'open'"
        )
    ]
    known += [
        (compact(str(p["text"])), str(p["scope"]), str(p["ref"]))
        for p in preferences.load(conn, None)
        if preferences.in_force(p)
    ]
    return known


def same_as(known: Known, words: str, scope: str) -> str | None:
    """The candidate or preference that already covers these words in ``scope``: the same words in
    the same scope or a global one. The same words in another repository's scope, or a narrower
    one than ``scope``, do not reach where the user now wants them (review r1 F1)."""
    key = compact(words)
    for known_words, known_scope, ref in known:
        if known_words == key and narrower_or_same(scope, known_scope):
            return ref
    return None


@tool(
    "preference_candidates_record",
    "worker",
    "orchestrator",
    description="记录一次偏好候选的搜集（DN007 §6.3）：子代理按 preference_sources 的材料找出的"
    "候选，每条附原话（从一条材料中原样摘取）、来源编号、建议的范围与主题；没有候选时也调用一次。"
    "候选不是偏好，须经用户确认（preference_record 带 candidate）。worker 只记本任务、用户验收前",
)
def preference_candidates_record(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    through = integer(inp, "scanned_through", minimum=1)
    raw = items(inp, "candidates")
    if len(raw) > MAX_CANDIDATES:
        raise rejected(
            f"一次至多 {MAX_CANDIDATES} 条候选：不确定的不提",
            reason="invalid_input",
            field="candidates",
        )
    entries = [_candidate_input(r, i) for i, r in enumerate(raw)]
    skipped = [_skipped_input(r, i) for i, r in enumerate(items(inp, "skipped", required=False))]

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, attempt = _writable(ctx, env, task_id)
        if through < 1 or through > ctx.tail.sequence:
            raise rejected(
                "scanned_through 应为 preference_sources 返回的账本序号",
                reason="invalid_input",
                field="scanned_through",
            )
        every_material = materials(ctx.conn, ctx.storage.read(since=0), task_id)
        # The goal of the current revision must have been read: a scan of an older revision
        # does not count for this one (review r1 F2).
        if int(every_material[0]["sequence"]) > through:
            raise rejected(
                "任务在读取材料之后改了目标或 revision：重新调用 preference_sources 再搜集",
                reason="stale_scan",
                field="scanned_through",
                revision=int(row["revision"]),
            )
        found = {str(m["ref"]): m for m in every_material if int(m["sequence"]) <= through}
        for i, entry in enumerate(entries):
            material = found.get(entry["source_ref"])
            if material is None:
                raise rejected(
                    "来源编号不是本任务的材料",
                    reason="invalid_input",
                    field=f"candidates[{i}].source_ref",
                    source_ref=entry["source_ref"],
                )
            if compact(entry["quote"]) not in compact(str(material["text"])):
                raise rejected(
                    "原话没有原样出现在所引用的材料中",
                    reason="quote_not_found",
                    field=f"candidates[{i}].quote",
                    source_ref=entry["source_ref"],
                )
            check_supersedes(ctx.conn, entry["scope"], entry["supersedes"])
        for i, item in enumerate(skipped):
            if item["source_ref"] not in found:
                raise rejected(
                    "来源编号不是本任务的材料",
                    reason="invalid_input",
                    field=f"skipped[{i}].source_ref",
                    source_ref=item["source_ref"],
                )
        known = _known_words(ctx.conn)
        count = int(
            ctx.conn.execute("SELECT COUNT(*) AS n FROM preference_candidates").fetchone()["n"]
        )
        revision = int(row["revision"])
        by = env.caller.actor()
        drafts: list[EventDraft] = []
        recorded: list[str] = []
        duplicates: list[dict[str, Any]] = []
        for entry in entries:
            already = same_as(known, entry["quote"], entry["scope"])
            if already is not None:
                duplicates.append({"source_ref": entry["source_ref"], "same_as": already})
                continue
            count += 1
            ref = f"PC-{count}"
            known.append((compact(entry["quote"]), entry["scope"], ref))
            recorded.append(ref)
            material = found[entry["source_ref"]]
            drafts.append(
                EventDraft(
                    type="user.preference_candidate",
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt,
                    data={
                        "ref": ref,
                        **entry,
                        "material": {
                            "channel": material["channel"],
                            "verified": material["verified"],
                            "kind": material["kind"],
                        },
                        "by": by,
                    },
                )
            )
        drafts.append(
            EventDraft(
                type="task.preference_scanned",
                task_id=task_id,
                revision=revision,
                attempt=attempt,
                data={
                    "scanned_through": through,
                    "candidates": recorded,
                    "duplicates": duplicates,
                    "skipped": skipped,
                    "by": by,
                },
            )
        )
        return drafts

    return _cmd(env, "preference_candidates_record", inp, build)


@tool(
    "preference_candidate_dismiss",
    "orchestrator",
    "user",
    description="用户不把一条偏好候选记为偏好（DN007 §6.3）：附用户原话，target 为 "
    '["preference", <候选编号>, "dismiss"]；只能处理未决的候选',
)
def preference_candidate_dismiss(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    ref = text(inp, "candidate").strip()
    reason = normalized(text(inp, "reason"))
    if len(reason) > MAX_REASON_CHARS:
        raise rejected("理由过长", reason="invalid_input", field="reason")
    source = guards.verified_source(
        mapping(inp, "user_source"),
        ["preference", ref, DISMISS_TARGET],
        channels=user_channels(env),
        mismatch='用户来源的决定对象应为 ["preference", <候选编号>, "dismiss"]',
    )

    def build(ctx: CommitContext) -> list[EventDraft]:
        found = open_candidate(ctx.conn, ref)
        return [
            EventDraft(
                type="user.preference_candidate_dismissed",
                task_id=str(found["task_id"]),
                data={"ref": ref, "reason": reason, "source": source},
            )
        ]

    return _cmd(env, "preference_candidate_dismiss", inp, build)


def open_candidate(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    found = row_dict(
        conn.execute("SELECT * FROM preference_candidates WHERE ref = ?", (ref,)).fetchone()
    )
    if found is None:
        raise rejected("偏好候选不存在", reason="invalid_input", candidate=ref)
    if found["status"] != "open":
        raise rejected(
            "偏好候选已处理（已记录或已拒绝）",
            reason="candidate_closed",
            candidate=ref,
            status=found["status"],
            preference=found["preference"],
        )
    return found


# ---------------------------------------------------------------------- reducers


def reduce_candidate(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    material = data.get("material") or {}
    conn.execute(
        "INSERT INTO preference_candidates(ref, task_id, revision, attempt, quote, source_ref, "
        "channel, verified, scope, topic, reason, supersedes, status, preference, resolution, "
        "recorded_by, recorded_at, source_seq) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, ?, ?, ?)",
        (
            str(data["ref"]),
            event.task_id,
            int(event.revision or 0),
            event.attempt,
            str(data.get("quote") or ""),
            str(data.get("source_ref") or ""),
            str(material.get("channel") or ""),
            1 if material.get("verified") else 0,
            str(data.get("scope") or "global"),
            str(data.get("topic") or ""),
            str(data.get("reason") or ""),
            dumps(list(data.get("supersedes") or [])),
            dumps(data.get("by") or {}),
            event.time,
            event.sequence,
        ),
    )


def reduce_dismissed(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "UPDATE preference_candidates SET status = 'dismissed', resolution = ? WHERE ref = ?",
        (
            dumps(
                {
                    "reason": data.get("reason"),
                    "source": data.get("source"),
                    "time": event.time,
                    "sequence": event.sequence,
                }
            ),
            str(data["ref"]),
        ),
    )


def reduce_confirmed(conn: sqlite3.Connection, ref: str, preference: str, event: Event) -> None:
    """``user.preference_recorded`` with ``candidate``: the user turned it into ``preference``."""
    conn.execute(
        "UPDATE preference_candidates SET status = 'recorded', preference = ?, resolution = ? "
        "WHERE ref = ?",
        (
            preference,
            dumps({"time": event.time, "sequence": event.sequence}),
            ref,
        ),
    )


def reduce_scanned(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "INSERT INTO preference_scans(sequence, task_id, revision, attempt, scanned_through, "
        "candidates, skipped, by, scanned_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.sequence,
            event.task_id,
            int(event.revision or 0),
            event.attempt,
            int(data.get("scanned_through") or 0),
            dumps(list(data.get("candidates") or [])),
            dumps(
                {
                    "skipped": list(data.get("skipped") or []),
                    "duplicates": list(data.get("duplicates") or []),
                }
            ),
            dumps(data.get("by") or {}),
            event.time,
        ),
    )


REDUCERS = {
    "user.preference_candidate": reduce_candidate,
    "user.preference_candidate_dismissed": reduce_dismissed,
    "task.preference_scanned": reduce_scanned,
}
