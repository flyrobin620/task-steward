# Tool inputs are untyped JSON checked field by field; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Follow-ups of a task (DN002 §5.7): what the task leaves for later, recorded by its worker or
the orchestrator, then turned into a task or dismissed. None of it changes the task's state."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.machine import guards
from tsw.machine.base import (
    ToolEnv,
    items,
    mapping,
    opt_text,
    strings,
    text,
    tool,
    worker_attempt,
)
from tsw.machine.model import loads, row_dict, task
from tsw.tasks import TaskSpec, registration

KINDS = ("remaining", "out_of_scope", "idea")
SEVERITIES = ("high", "medium", "low")
# A worker records follow-ups while its session holds the task, until the user accepts it.
WORKER_STATES = ("in_progress", "waiting_user", "verifying", "done")
# A task taking follow-ups in: one still to be done (DN002 §5.7).
CLOSED = ("accepted", "cancelled", "archived")
TARGET = "follow_up"


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def _choice(value: str | None, allowed: tuple[str, ...], field_name: str) -> str | None:
    if value is not None and value not in allowed:
        raise rejected("字段取值无效", reason="invalid_input", field=field_name, allowed=allowed)
    return value


def _date(value: str | None) -> str | None:
    """A recheck date as ``YYYY-MM-DD`` exactly: ``attention`` compares it as text with today."""
    if value is None:
        return None
    try:
        parsed: dt.date | None = dt.date.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise rejected("复查日期应为 YYYY-MM-DD", reason="invalid_input", field="recheck_after")
    return value


def _severity_given(kind: str | None, severity: str | None, field_name: str) -> None:
    """An out-of-scope finding says how serious it is (DN002 §5.7, T264 A12)."""
    if kind == "out_of_scope" and severity is None:
        raise rejected(
            "范围外发现必须给出严重度（severity）", reason="invalid_input", field=field_name
        )


def _item(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise rejected("follow-up 应为对象", reason="invalid_input", field=f"items[{index}]")
    title = text(raw, "title").strip()
    detail = text(raw, "detail").strip()
    if "\n" in title:
        raise rejected("标题只能有一行", reason="invalid_input", field=f"items[{index}].title")
    kind = _choice(opt_text(raw, "kind"), KINDS, "kind") or "remaining"
    severity = _choice(opt_text(raw, "severity"), SEVERITIES, "severity")
    _severity_given(kind, severity, f"items[{index}].severity")
    return {"title": title, "detail": detail, "kind": kind, "severity": severity}


def _ids(conn: sqlite3.Connection, task_id: str, count: int) -> list[str]:
    """``F-<task>-<n>`` after the task's own follow-ups, skipping ids used anywhere (a renumbered
    task keeps the ids it was given under its old number)."""
    number = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM follow_ups WHERE task_id = ?", (task_id,)
        ).fetchone()["n"]
    )
    out: list[str] = []
    while len(out) < count:
        number += 1
        candidate = f"F-{task_id}-{number}"
        if (
            conn.execute("SELECT 1 FROM follow_ups WHERE follow_up_id = ?", (candidate,)).fetchone()
            is None
        ):
            out.append(candidate)
    return out


def source(
    ctx: CommitContext,
    env: ToolEnv,
    row: Mapping[str, Any],
    message_ids: list[str],
    user_source: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The user's words behind a follow-up operation, if any (DN002 §5.7).

    Messages the host recorded for the task (``input.session_message``) are cited by number and
    the source is built from the recorded words: a worker's only from its own session, the
    orchestrator's from any session of the task. The orchestrator may instead give the user's
    words in its own session as a ``VerifiedSource`` aimed at ``[task, revision, "follow_up"]``.
    """
    task_id = str(row["task_id"])
    if message_ids and user_source is not None:
        raise rejected(
            "message_ids 与 user_source 只能给一个", reason="invalid_input", field="user_source"
        )
    if user_source is not None:
        if env.caller.role != "orchestrator":
            raise rejected(
                "worker 只能引用 hook 记录的消息编号", reason="invalid_input", field="user_source"
            )
        return guards.g_user(
            user_source, task_id=task_id, revision=int(row["revision"]), kind=TARGET
        )
    if not message_ids:
        return None
    texts: list[str] = []
    for message_id in dict.fromkeys(message_ids):
        message = ctx.conn.execute(
            "SELECT * FROM session_messages WHERE message_id = ? AND task_id = ?",
            (message_id, task_id),
        ).fetchone()
        own = env.caller.role != "worker" or (
            env.caller.session_id is not None
            and message is not None
            and message["session_ref"] == env.caller.session_id
        )
        if message is None or not own:
            raise rejected(
                "引用的不是宿主为该任务记录的用户消息"
                + ("（worker 只能引用本会话的消息）" if env.caller.role == "worker" else ""),
                reason="g_user",
                check="message",
                message_id=message_id,
            )
        texts.append(str(message["text"]))
    return {
        "channel": "in-session",
        "principal": "session-owner",
        "message_ref": ",".join(dict.fromkeys(message_ids)),
        "text": "\n\n".join(texts),
        "request_id": None,
        "target": [task_id, int(row["revision"]), TARGET],
    }


def _writable(ctx: CommitContext, env: ToolEnv, task_id: str) -> tuple[dict[str, Any], str | None]:
    """The task, if the caller may record or change its follow-ups now, and the caller's attempt."""
    row = task(ctx.conn, task_id)
    status = str(row["status"])
    if env.caller.role == "worker":
        current = worker_attempt(ctx.conn, env, task_id)
        if status not in WORKER_STATES:
            raise rejected(
                "worker 只能在任务被用户验收之前、自己持有任务时记录 follow-up；"
                "之后请用户在主会话中提出",
                reason="invalid_state",
                status=status,
            )
        return row, str(current["attempt"])
    if status == "archived":
        raise rejected(
            "已归档的任务不能再记录或修改 follow-up：请登记新任务", reason="invalid_state"
        )
    return row, None


def _open(conn: sqlite3.Connection, follow_up_id: str) -> dict[str, Any]:
    found = row_dict(
        conn.execute("SELECT * FROM follow_ups WHERE follow_up_id = ?", (follow_up_id,)).fetchone()
    )
    if found is None:
        raise rejected("follow-up 不存在", reason="follow_up_not_found", follow_up_id=follow_up_id)
    if found["status"] != "open":
        raise rejected(
            "follow-up 已处理（已转为任务或已驳回）",
            reason="follow_up_closed",
            follow_up_id=follow_up_id,
            status=found["status"],
            converted_to=found["converted_to"],
        )
    return found


def _by(env: ToolEnv, attempt: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"role": env.caller.role}
    if attempt is not None:
        out["attempt"] = attempt
        out["session_ref"] = env.caller.session_id
    return out


# ---------------------------------------------------------------------- record and update


@tool(
    "follow_up_record",
    "worker",
    "orchestrator",
    description=(
        "记录任务的 follow-up（执行中发现、本任务不做、将来可能变成任务的事，先停放以免忘记；"
        "验收请求、本任务自己的待办、等待中的状态、没有具体问题的设想都不是；DN002 §5.7）："
        "worker 只记本任务、用户验收前；主会话除已归档外都可以。不改变任务状态"
    ),
)
def follow_up_record(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    raw = items(inp, "items")
    if not raw:
        raise rejected("至少记录一条 follow-up", reason="invalid_input", field="items")
    entries = [_item(r, i) for i, r in enumerate(raw)]
    message_ids = strings(items(inp, "message_ids", required=False), "message_ids")
    user_source = mapping(inp, "user_source", required=False)

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, attempt = _writable(ctx, env, task_id)
        cited = source(ctx, env, row, message_ids, user_source)
        ids = _ids(ctx.conn, task_id, len(entries))
        data: dict[str, Any] = {
            "items": [
                {"follow_up_id": fid, **entry} for fid, entry in zip(ids, entries, strict=True)
            ],
            "by": _by(env, attempt),
        }
        if cited is not None:
            data["source"] = cited
        return [
            EventDraft(
                type="follow_up.recorded",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt,
                data=data,
            )
        ]

    return _cmd(env, "follow_up_record", inp, build)


@tool(
    "follow_up_update",
    "worker",
    "orchestrator",
    description=(
        "修改 open 的 follow-up：标题、说明、类别、严重度；主会话还可以记下用户“暂不处理”的决定"
        "与复查日期（recheck_after，附用户来源）（DN002 §5.7）"
    ),
)
def follow_up_update(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    follow_up_id = text(inp, "follow_up_id")
    reason = text(inp, "reason")
    changes: dict[str, Any] = {}
    for key in ("title", "detail"):
        value = opt_text(inp, key)
        if value is not None:
            if not value.strip() or (key == "title" and "\n" in value):
                raise rejected("字段无效", reason="invalid_input", field=key)
            changes[key] = value.strip()
    kind = _choice(opt_text(inp, "kind"), KINDS, "kind")
    if kind is not None:
        changes["kind"] = kind
    severity = _choice(opt_text(inp, "severity"), SEVERITIES, "severity")
    if severity is not None:
        changes["severity"] = severity
    recheck = _date(opt_text(inp, "recheck_after"))
    if recheck is not None:
        changes["recheck_after"] = recheck
    if not changes:
        raise rejected("没有要修改的字段", reason="invalid_input")
    message_ids = strings(items(inp, "message_ids", required=False), "message_ids")
    user_source = mapping(inp, "user_source", required=False)

    def build(ctx: CommitContext) -> list[EventDraft]:
        found = _open(ctx.conn, follow_up_id)
        task_id = str(found["task_id"])
        if inp.get("task_id") not in (None, task_id):
            raise rejected(
                "follow-up 不属于该任务", reason="invalid_input", follow_up_id=follow_up_id
            )
        row, attempt = _writable(ctx, env, task_id)
        _severity_given(
            changes.get("kind", found["kind"]),
            changes.get("severity", found["severity"]),
            "severity",
        )
        if env.caller.role == "worker":
            if loads(found["recorded_by"], {}).get("role") != "worker":
                raise rejected("worker 只能修改 worker 记录的 follow-up", reason="role_denied")
            if recheck is not None:
                raise rejected(
                    "复查日期记录用户“暂不处理”的决定，由主会话设置",
                    reason="role_denied",
                    field="recheck_after",
                )
        cited = source(ctx, env, row, message_ids, user_source)
        if recheck is not None and cited is None:
            raise rejected(
                "暂不处理是用户的决定：设置复查日期时附用户来源",
                reason="g_user",
                field="recheck_after",
            )
        data: dict[str, Any] = {
            "follow_up_id": follow_up_id,
            "changes": changes,
            "reason": reason,
            "by": _by(env, attempt),
        }
        if cited is not None:
            data["source"] = cited
        return [
            EventDraft(
                type="follow_up.updated",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt,
                data=data,
            )
        ]

    return _cmd(env, "follow_up_update", inp, build)


# ---------------------------------------------------------------------- convert and dismiss


def _open_many(conn: sqlite3.Connection, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        raise rejected("至少给出一条 follow-up", reason="invalid_input", field="follow_up_ids")
    found = [_open(conn, fid) for fid in dict.fromkeys(ids)]
    for row in found:
        if task(conn, str(row["task_id"]))["status"] == "archived":
            raise rejected(
                "已归档任务的 follow-up 不能再处理",
                reason="invalid_state",
                follow_up_id=row["follow_up_id"],
            )
    return found


def _by_task(found: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for row in found:
        grouped.setdefault(str(row["task_id"]), []).append(str(row["follow_up_id"]))
    return grouped


def default_goal(found: list[dict[str, Any]]) -> str:
    """The goal of a task made from follow-ups when none is given: where they come from and each
    follow-up's number, title and description."""
    sources = "、".join(_by_task(found))
    lines = [f"来自 {sources} 的 follow-up："]
    for row in found:
        lines.append(f"- {row['follow_up_id']} {row['title']}：{row['detail']}")
    return "\n".join(lines)


def default_acceptance(found: list[dict[str, Any]]) -> list[str]:
    return [f"完成 {row['follow_up_id']}：{row['title']}" for row in found]


def _spec(
    spec_in: Mapping[str, Any], found: list[dict[str, Any]], conn: sqlite3.Connection
) -> TaskSpec:
    sources = [task(conn, tid) for tid in _by_task(found)]
    repo = opt_text(spec_in, "repo")
    kind = opt_text(spec_in, "kind")
    for key, given in (("repo", repo), ("kind", kind)):
        if given is None and len({str(s[key]) for s in sources}) > 1:
            raise rejected(
                "follow-up 来自不同仓库或类型的任务：请给出新任务的 " + key,
                reason="invalid_input",
                field=f"task.{key}",
            )
    title = opt_text(spec_in, "title")
    if title is None:
        if len(found) > 1:
            raise rejected(
                "多条 follow-up 转为一个任务时请给出标题",
                reason="invalid_input",
                field="task.title",
            )
        title = str(found[0]["title"])
    acceptance_in = spec_in.get("acceptance")
    acceptance = (
        strings(items(spec_in, "acceptance"), "task.acceptance")
        if acceptance_in is not None
        else default_acceptance(found)
    )
    return TaskSpec(
        task_id=opt_text(spec_in, "task_id"),
        title=title,
        repo=repo or str(sources[0]["repo"]),
        kind=kind or str(sources[0]["kind"]),
        goal=opt_text(spec_in, "goal") or default_goal(found),
        acceptance=acceptance,
    )


@tool(
    "follow_up_convert",
    "orchestrator",
    description=(
        "把一条或多条 open 的 follow-up 转为任务（DN002 §5.7）：给 task 时登记新任务（T01，"
        "记录 follow_up_of；不给目标与验收时由 follow-up 生成），给 into_task 时并入已登记的任务；"
        "来源 follow-up 标为 converted"
    ),
)
def follow_up_convert(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    follow_up_ids = strings(items(inp, "follow_up_ids"), "follow_up_ids")
    spec_in = mapping(inp, "task", required=False)
    into = opt_text(inp, "into_task")
    if (spec_in is None) == (into is None):
        raise rejected("task（新任务）与 into_task（已有任务）恰好给一个", reason="invalid_input")
    message_ids = strings(items(inp, "message_ids", required=False), "message_ids")
    user_source = mapping(inp, "user_source", required=False)

    def build(ctx: CommitContext) -> list[EventDraft]:
        found = _open_many(ctx.conn, follow_up_ids)
        grouped = _by_task(found)
        # The user's words, when given, are about the first follow-up's task (DN002 §5.7).
        cited = source(ctx, env, task(ctx.conn, next(iter(grouped))), message_ids, user_source)
        follow_up_of = [
            {"task_id": str(r["task_id"]), "follow_up_id": str(r["follow_up_id"])} for r in found
        ]
        drafts: list[EventDraft] = []
        if spec_in is not None:
            new = registration(ctx, _spec(spec_in, found, ctx.conn), {"follow_up_of": follow_up_of})
            drafts.append(new)
            target, mode = str(new.task_id), "new"
        else:
            assert into is not None
            existing = task(ctx.conn, into)
            if existing["status"] in CLOSED:
                raise rejected(
                    "只能并入还没有完成的任务（不是已验收、已取消或已归档）",
                    reason="invalid_state",
                    task_id=into,
                    status=existing["status"],
                )
            if into in grouped:
                raise rejected(
                    "follow-up 不能并入它自己的任务", reason="invalid_input", task_id=into
                )
            target, mode = into, "existing"
        for source_task, ids in grouped.items():
            data: dict[str, Any] = {"follow_up_ids": ids, "to_task": target, "mode": mode}
            if cited is not None:
                data["source"] = cited
            drafts.append(
                EventDraft(
                    type="follow_up.converted",
                    task_id=source_task,
                    revision=int(task(ctx.conn, source_task)["revision"]),
                    data=data,
                )
            )
        return drafts

    return _cmd(env, "follow_up_convert", inp, build)


@tool(
    "follow_up_dismiss",
    "orchestrator",
    description=(
        "驳回一条或多条 open 的 follow-up（DN002 §5.7）：附理由，写明去向；用户决定的附用户来源"
    ),
)
def follow_up_dismiss(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    follow_up_ids = strings(items(inp, "follow_up_ids"), "follow_up_ids")
    reason = text(inp, "reason")
    message_ids = strings(items(inp, "message_ids", required=False), "message_ids")
    user_source = mapping(inp, "user_source", required=False)

    def build(ctx: CommitContext) -> list[EventDraft]:
        found = _open_many(ctx.conn, follow_up_ids)
        grouped = _by_task(found)
        cited = source(ctx, env, task(ctx.conn, next(iter(grouped))), message_ids, user_source)
        drafts: list[EventDraft] = []
        for source_task, ids in grouped.items():
            data: dict[str, Any] = {"follow_up_ids": ids, "reason": reason}
            if cited is not None:
                data["source"] = cited
            drafts.append(
                EventDraft(
                    type="follow_up.dismissed",
                    task_id=source_task,
                    revision=int(task(ctx.conn, source_task)["revision"]),
                    data=data,
                )
            )
        return drafts

    return _cmd(env, "follow_up_dismiss", inp, build)


# ---------------------------------------------------------------------- reads


def open_count(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(r["task_id"]): int(r["n"])
        for r in conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM follow_ups WHERE status = 'open' GROUP BY task_id"
        )
    }


def due(conn: sqlite3.Connection, today: str) -> list[dict[str, Any]]:
    """Open follow-ups waiting for a decision: never put off, or past their recheck date."""
    return [
        {
            "follow_up_id": r["follow_up_id"],
            "task_id": r["task_id"],
            "kind": r["kind"],
            "severity": r["severity"],
            "title": r["title"],
            **({"recheck_after": r["recheck_after"]} if r["recheck_after"] else {}),
        }
        for r in conn.execute(
            "SELECT * FROM follow_ups WHERE status = 'open' "
            "AND (recheck_after IS NULL OR recheck_after <= ?) "
            "ORDER BY task_id, recorded_seq, follow_up_id",
            (today,),
        )
    ]


def has_open(conn: sqlite3.Connection, task_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM follow_ups WHERE task_id = ? AND status = 'open' LIMIT 1", (task_id,)
        ).fetchone()
        is not None
    )
