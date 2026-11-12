# Tool inputs and host tool results are untyped JSON walked field by field.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Worker session titles and the archive after acceptance (DN002 §5.8, T272).

A session's title can only be changed by a session in the host, never by tsw itself, and the
host does not tell whether the user set a title by hand: the Claude app only asks the user to
approve when a title they set is about to be replaced, even in a turn a timer started (T272
live test). So tsw records the title it gave each session, and the main session renames a
session only while it still has that title. Any other title is the user's and is left alone.

- ``base_title`` / ``creation_title``: the title ``attempt_start`` hands out.
- ``wanted``: the title the task's state calls for; ``due`` lists the sessions whose title or
  archive has not caught up (``attention`` ``sessions``, the routine job ``session-scan`` and
  the orchestrator's Stop hook).
- ``session_check`` compares what ``get_session`` returned and says what to do next;
  ``session_record`` records what the host tool did. Neither changes a task's state.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.machine.base import ToolEnv, flag, opt_text, text, tool
from tsw.machine.model import attempt, task

STANDBY_SUFFIX = "（待命）"
BASE_LIMIT = 50  # characters of a default base title (task id and task title)
# Attempt phases whose session may still be there and is tsw's to look after.
WITH_SESSION = ("standby", "live", "stopped")
ACTIONS = ("title", "archive")
EVIDENCE_CHARS = 500  # of a host tool's result kept in the event


def base_title(task_id: str, task_title: str, given: str | None) -> str:
    """The base a session's titles are made of: the main session's short title, or the task's
    id and title cut to ``BASE_LIMIT`` characters."""
    if given and given.strip():
        return given.strip()
    return f"{task_id} {task_title}".strip()[:BASE_LIMIT].rstrip()


def creation_title(base: str, purpose: str) -> str:
    """The title a session is created with: a prestarted one says it is on standby."""
    return f"{base}{STANDBY_SUFFIX}" if purpose == "prestart" else base


def _prefixed(prefix: Any, base: str) -> str:
    chosen = str(prefix or "").strip()
    return f"{chosen} {base}" if chosen else base


def wanted(status: str, phase: str, base: str, config: EffectiveConfig) -> tuple[str, str]:
    """The title the task's state calls for, and why (DN002 §5.8)."""
    if phase == "standby":
        return creation_title(base, "prestart"), "standby"
    if status == "accepted":
        return _prefixed(config.get("sessions.title_on_accept"), base), "accepted"
    if status == "cancelled":
        return _prefixed(config.get("sessions.title_on_cancel"), base), "cancelled"
    return base, "dispatch"


def tsw_titles(row: Mapping[str, Any]) -> set[str]:
    """The titles this session may show as tsw left it: the one tsw last knew it to have, and the
    one tsw last asked the main session to set (``asked``), whose rename may have happened
    without a record (a crash, or the task moved on before the record). Any other title is the
    user's, including one that merely looks like a title tsw would make (T272 review r2)."""
    return {str(t) for t in (row["known"], row["asked"]) if t}


def _rows(conn: sqlite3.Connection, where: str = "", args: tuple[Any, ...] = ()) -> list[Any]:
    phases = ", ".join(f"'{p}'" for p in WITH_SESSION)
    # A closed attempt of a finished task may keep its session, for instance a standby session
    # closed before the task was cancelled (sessions.archive_on_archive false): it still gets
    # its prefix. get_session tells whether it is still there.
    return conn.execute(
        "SELECT s.*, a.phase, a.session_ref, t.status, t.revision FROM session_titles s "
        "JOIN attempts a ON a.task_id = s.task_id AND a.attempt = s.attempt "
        "JOIN tasks t ON t.task_id = s.task_id "
        f"WHERE a.session_ref IS NOT NULL AND (a.phase IN ({phases}) OR (a.phase = 'closed' "
        "AND t.status IN ('accepted', 'cancelled'))) "
        f"AND t.status != 'archived' AND s.missing = 0 {where} "
        "ORDER BY s.task_id, s.attempt",
        args,
    ).fetchall()


def _next(row: Mapping[str, Any], config: EffectiveConfig) -> dict[str, Any] | None:
    """What a session still needs, if anything: its title first, then (accepted tasks with
    ``sessions.archive_on_accept``) the archive. ``state`` is ``failed`` once a host tool failed
    on exactly this step."""
    title, reason = wanted(str(row["status"]), str(row["phase"]), str(row["base"]), config)
    item: dict[str, Any] = {
        "task_id": row["task_id"],
        "attempt": row["attempt"],
        "session_id": row["session_ref"],
        "reason": reason,
    }
    title_item = {**item, "action": "title", "title": title}
    open_title = row["user_title"] is None and title != row["known"]
    if open_title and row["failed_title"] != title:
        return {**title_item, "state": "due"}
    archive = row["status"] == "accepted" and row["phase"] != "closed"
    if archive and config.get("sessions.archive_on_accept"):
        # Archived whatever became of the title: the user turned the archive on.
        return {**item, "action": "archive", "state": "failed" if row["archive_failed"] else "due"}
    if open_title:
        return {**title_item, "state": "failed"}
    return None


def due(conn: sqlite3.Connection, config: EffectiveConfig) -> list[dict[str, Any]]:
    """The sessions whose title or archive has not caught up with their task (DN002 §5.8).

    Items a host tool already failed on are listed with ``state: failed``: nothing reminds of
    them again, and the main session may try once more. Sessions created before T272 have no
    recorded title and are not listed.
    """
    found = (_next(row, config) for row in _rows(conn))
    return [item for item in found if item is not None]


def pending(conn: sqlite3.Connection, config: EffectiveConfig) -> list[dict[str, Any]]:
    """The items still to do (not failed): what the Stop hook and ``session-scan`` act on."""
    return [item for item in due(conn, config) if item["state"] == "due"]


def _current(conn: sqlite3.Connection, task_id: str, attempt_id: str) -> dict[str, Any]:
    rows = _rows(conn, "AND s.task_id = ? AND s.attempt = ?", (task_id, attempt_id))
    if not rows:
        task(conn, task_id)
        if attempt(conn, task_id, attempt_id) is None:
            raise rejected("attempt 不存在", reason="invalid_attempt")
        raise rejected(
            "这个 attempt 没有 tsw 记下的会话标题（T272 之前创建），或会话已不在 tsw 管理之下",
            reason="no_session_title",
        )
    return dict(rows[0])


def _observed(raw: Any) -> dict[str, Any] | None:
    """The ``get_session`` result as an object (``sessionId``, ``title``, ``isArchived``); a
    text result is parsed as JSON. None when it is not a session."""
    value = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, Mapping) and isinstance(value.get("title"), str):
        return dict(value)
    return None


def _evidence(raw: Any) -> str:
    shown = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    return str(shown)[:EVIDENCE_CHARS]


def _event(row: Mapping[str, Any], **data: Any) -> EventDraft:
    return EventDraft(
        type="attempt.session_updated",
        task_id=str(row["task_id"]),
        revision=int(row["revision"]),
        attempt=str(row["attempt"]),
        data={"attempt": row["attempt"], "session_ref": row["session_ref"], **data},
    )


def _closed(row: Mapping[str, Any], evidence: str, disposition: str) -> EventDraft:
    """``attempt.closed`` after the archive ``sessions.archive_on_accept`` asks for."""
    return EventDraft(
        type="attempt.closed",
        task_id=str(row["task_id"]),
        revision=int(row["revision"]),
        attempt=str(row["attempt"]),
        data={
            "attempt": row["attempt"],
            "evidence": evidence,
            "disposition": disposition,
            "via": "sessions.archive_on_accept",
        },
    )


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


@tool(
    "session_check",
    "orchestrator",
    description=(
        "worker 会话的标题与验收后归档（DN002 §5.8）：交上 get_session 的返回原样，tsw 比较当前"
        "标题与它记下的标题，返回下一步（rename / archive / none）；用户改过的标题、会话不存在、"
        "已是应有标题时直接记录，不再调用宿主工具"
    ),
)
def session_check(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    attempt_id = text(inp, "attempt")
    raw = inp.get("observed")
    gone = flag(inp, "missing")
    output: dict[str, Any] = {}

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = _current(ctx.conn, task_id, attempt_id)
        item = _next(row, env.config)
        if item is None:
            output.update(action="none", note="这个会话的标题与归档都已跟上任务状态")
            return []
        seen = _observed(raw)
        if seen is not None and seen.get("sessionId") not in (None, row["session_ref"]):
            raise rejected(
                "observed 不是这个 attempt 的会话",
                reason="session_mismatch",
                session_ref=row["session_ref"],
            )
        if gone or (seen is not None and seen.get("isArchived") is True):
            output.update(action="none", outcome="missing")
            closed = _closed(row, f"会话已不存在或已归档：{_evidence(raw)}", "missing")
            if item["action"] == "archive":
                # Nothing left to archive: the receipt says why (DN002 §5.8).
                return [closed]
            missing = _event(
                row,
                action=item["action"],
                outcome="missing",
                reason=item["reason"],
                evidence=_evidence(raw),
            )
            # Gone before its title was done: the archive it was still due is settled too, or
            # the attempt would stay live with no receipt (PR #43 review).
            archive_due = row["status"] == "accepted" and row["phase"] != "closed"
            if archive_due and env.config.get("sessions.archive_on_accept"):
                return [missing, closed]
            return [missing]
        if seen is None:
            raise rejected(
                "observed 应为 get_session 的返回（含 title）；会话不存在时给 missing: true",
                reason="invalid_input",
                field="observed",
            )
        if item["action"] == "archive":
            output.update(action="archive", session_id=row["session_ref"])
            return []
        title, current = str(item["title"]), str(seen["title"])
        if current == title:
            outcome = "already"
            output.update(action="none", outcome=outcome, title=title)
        elif current in tsw_titles(row):
            output.update(action="rename", session_id=row["session_ref"], title=title)
            # Remember what was asked for: its rename may happen without a record.
            return [
                _event(
                    row,
                    action="title",
                    outcome="asked",
                    title=title,
                    observed=current,
                    reason=item["reason"],
                )
            ]
        else:
            # Someone else, the user as a rule, chose this title: leave it and never ask.
            outcome = "user_title"
            output.update(action="none", outcome=outcome, observed=current)
        return [
            _event(
                row,
                action="title",
                outcome=outcome,
                title=title,
                observed=current,
                reason=item["reason"],
            )
        ]

    command = _cmd(env, "session_check", inp, build)
    command.output = output
    return command


@tool(
    "session_record",
    "orchestrator",
    description=(
        "记录 set_session_title / archive_session 的结果（DN002 §5.8）：改名成功或失败、归档成功"
        "（attempt.closed）或失败；不改变任务状态"
    ),
)
def session_record(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    attempt_id = text(inp, "attempt")
    action = text(inp, "action")
    result = text(inp, "result")
    ok = flag(inp, "ok")
    if action not in ACTIONS:
        raise rejected("action 必须是 title 或 archive", reason="invalid_input", field="action")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = _current(ctx.conn, task_id, attempt_id)
        item = _next(row, env.config)
        if action == "title":
            title = text(inp, "title")
            ours = row["user_title"] is None and title != row["known"]
            if ok and ours and title == row["asked"]:
                # A rename that happened is recorded even when the task moved on before its
                # record (accepted in a terminal meanwhile): tsw then knows the title it left,
                # and the next check renames again instead of taking it for the user's.
                # The item may be the archive by now (a prefix emptied meanwhile): no title.
                current = item is not None and item.get("title") == title
                reason = item["reason"] if item is not None and current else None
                return [
                    _event(
                        row,
                        action="title",
                        outcome="renamed",
                        title=title,
                        reason=reason or "superseded",
                        result=result[:EVIDENCE_CHARS],
                    )
                ]
            if item is None or item["action"] != "title":
                raise rejected(
                    "这个会话现在不需要改标题（先 session_check）",
                    reason="not_due",
                    due=item and item["action"],
                )
            if title != item["title"]:
                raise rejected(
                    "title 不是现在应有的标题（任务状态已变化？先 session_check）",
                    reason="title_outdated",
                    wanted=item["title"],
                )
            return [
                _event(
                    row,
                    action="title",
                    outcome="renamed" if ok else "failed",
                    title=title,
                    reason=item["reason"],
                    result=result[:EVIDENCE_CHARS],
                )
            ]
        if item is None or item["action"] != action:
            raise rejected(
                "这个会话现在不需要这一步（先 session_check）",
                reason="not_due",
                due=item and item["action"],
            )
        if ok:
            # The tool receipt of the configured disposition (DN002 §5.8, §6).
            return [_closed(row, result[:EVIDENCE_CHARS], "archived")]
        return [
            _event(
                row,
                action="archive",
                outcome="failed",
                reason=item["reason"],
                result=result[:EVIDENCE_CHARS],
            )
        ]

    return _cmd(env, "session_record", inp, build)
