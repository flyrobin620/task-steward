# Tool inputs and fetched host data are untyped JSON walked field by field.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""The routine session and its routine board (DN006 §8.9).

The routine board is a list of routine jobs. Every run, the routine session reads the jobs that
are due (``routine_board``), runs each job's built-in script (``routine_run``) and passes the
message a script produces on to the orchestrator, word for word. Scripts are deterministic
programs: the routine session decides nothing.

- ``handoff-scan`` (always on the board): in-session questions past their timeout with no reply
  from the user are handed off (``by: system``) and listed for the orchestrator (DN006 §8.1b).
- ``teams-replies`` (added by ``channel_sent``): messages in the Teams chat the question cards
  went to, sent by the user, starting with the ID of a task whose Teams question is still
  waiting (DN006 §8.7). The orchestrator decides whether a message answers the question.

The routine session itself is started by the orchestrator (``routine_start`` / ``routine_bind``)
and keeps its host timer armed (``routine_armed``); ``board`` tells it when to arm, renew or
clear its context (DN006 §8.9.6).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.home import Home
from tsw.lease import format_time, lease_row, parse_time
from tsw.machine.ask_user import handed_off_event, handoff_due
from tsw.machine.base import ToolEnv, flag, integer, opt_text, text, tool
from tsw.machine.model import row_dict, task
from tsw.plugins.catalog import SESSIONS

WORKDIR = "routine"  # the routine session's directory, relative to the data directory
HANDOFF_SCAN = "handoff-scan"
TEAMS_REPLIES = "teams-replies"
TEAMS_CHANNEL = "teams-chat"
CARD_PREFIX = "[TaskSteward]"
WORKER_LINK = "{worker_link}"  # the orchestrator puts the worker session's link here
SCHEDULED_PROMPT = (
    "<scheduled-task>TaskSteward 例行会话：调用 routine_board，逐行执行例行项并原样转发结果；"
    "没有例行项就结束这一轮。</scheduled-task>"
)
START_PROMPT = (
    "你是 TaskSteward 的例行会话。调用 whoami，再调用 routine_board，按返回的内容逐行执行例行项、"
    "原样转发结果，并按维护动作设定定时任务。不作任何判断，不做其他事。"
)
START_BACKGROUND = (
    "TaskSteward 的例行会话：按固定间隔运行数据目录中的例行表，每一行运行一个 tsw 内置脚本"
    "（例如把超时无人回复的会话内问题转交主会话、取出 Teams 中等待中的问题的答复），"
    "把脚本给出的消息原样转给主会话。它不改代码、不运行命令、不建会话，也不作任何判断。"
)
# Host tools for the data a job fetches before its script runs, by host integration (CC006 §6).
FETCH_TOOLS: dict[str, dict[str, str]] = {
    "claude-code": {"teams.read_chat": "mcp__teams__ListChatMessages"},
}
TEAMS_READ_TOP = 50


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


# ---------------------------------------------------------------------- the routine session


def session_name(generation: int) -> str:
    return f"routine-g{generation}"


def latest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The newest routine generation, whatever its status."""
    return row_dict(
        conn.execute("SELECT * FROM routine_sessions ORDER BY generation DESC LIMIT 1").fetchone()
    )


def bound(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The routine session currently in charge: the newest generation that was bound."""
    return row_dict(
        conn.execute(
            "SELECT * FROM routine_sessions WHERE status = 'bound' ORDER BY generation DESC LIMIT 1"
        ).fetchone()
    )


def by_session(conn: sqlite3.Connection, session_ref: str) -> dict[str, Any] | None:
    return row_dict(
        conn.execute(
            "SELECT * FROM routine_sessions WHERE session_ref = ? ORDER BY generation DESC LIMIT 1",
            (session_ref,),
        ).fetchone()
    )


def require_routine(conn: sqlite3.Connection, env: ToolEnv) -> dict[str, Any]:
    """The caller must be the routine session in charge."""
    row = bound(conn)
    if row is None or not env.caller.session_id or row["session_ref"] != env.caller.session_id:
        raise rejected("调用者不是当前的例行会话", reason="not_bound_routine")
    return row


def _hours(value: Any) -> timedelta:
    return timedelta(hours=int(value))


def cron_for(interval_minutes: int) -> str:
    """The cron expression of a run every ``interval_minutes`` (DN003 §3.1b)."""
    if interval_minutes < 60 and 60 % interval_minutes == 0:
        return f"*/{interval_minutes} * * * *"
    hours = interval_minutes // 60
    return "0 * * * *" if hours == 1 else ("0 0 * * *" if hours == 24 else f"0 */{hours} * * *")


def status(
    conn: sqlite3.Connection, config: EffectiveConfig, now: str, cache: Mapping[str, Any]
) -> dict[str, Any]:
    """The routine session's state for ``attention`` (DN006 §8.9.7)."""
    if not config.get("routine.enabled"):
        return {"state": "disabled"}
    newest = latest(conn)
    current = bound(conn)
    if newest is not None and newest["status"] == "starting":
        return {"state": "starting", "session_name": newest["session_name"]}
    if current is None:
        return {"state": "missing"}
    out = {
        "session_name": current["session_name"],
        "session_id": current["session_ref"],
        "armed_at": current["armed_at"],
        "last_run": _last_run(current, cache),
    }
    if current["armed_at"] is None:
        return {"state": "unarmed", **out}
    return {"state": "stale" if _stale(current, config, now, cache) else "ok", **out}


def _last_run(current: Mapping[str, Any], cache: Mapping[str, Any]) -> str | None:
    if cache.get("generation") == current["generation"] and cache.get("last_run"):
        return str(cache["last_run"])
    return None


def _stale(
    current: Mapping[str, Any], config: EffectiveConfig, now: str, cache: Mapping[str, Any]
) -> bool:
    """No run for ``stale_after_intervals`` intervals since the last run or the last arming.

    The last run lives in a cache file; when it is lost the arming time stands in, so the worst
    case is one message too many from the orchestrator (DN006 §8.9.3)."""
    seen = [t for t in (_last_run(current, cache), current["armed_at"]) if t]
    if not seen:
        return False
    last = max(parse_time(str(t)) for t in seen)
    window = timedelta(
        minutes=int(config.get("routine.interval_minutes"))
        * int(config.get("routine.stale_after_intervals"))
    )
    return parse_time(now) - last > window


def board(
    conn: sqlite3.Connection,
    config: EffectiveConfig,
    now: str,
    cache: Mapping[str, Any],
    session_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """What the routine session does in this run, and the cache to keep (DN006 §8.9.3).

    Read-only for the ledger. The cache remembers the time of this run and of the last context
    reset; losing it only makes the next run arm or clear once more.
    """
    current = bound(conn)
    if current is None or not session_id or current["session_ref"] != session_id:
        raise rejected("调用者不是当前的例行会话", reason="not_bound_routine")
    generation = int(current["generation"])
    kept: dict[str, Any] = (
        dict(cache) if cache.get("generation") == generation else {"generation": generation}
    )
    if not config.get("routine.enabled"):
        return {
            "enabled": False,
            "note": "配置关闭了例行会话（routine.enabled=false）：不执行例行项，结束这一轮",
            "maintenance": [],
            "jobs": [],
        }, kept
    maintenance: list[dict[str, Any]] = []
    interval = int(config.get("routine.interval_minutes"))
    renew = timedelta(days=int(config.get("routine.renew_after_days")))
    armed_at = current["armed_at"]
    if (
        armed_at is None
        or parse_time(now) - parse_time(str(armed_at)) > renew
        or _stale(current, config, now, cache)
    ):
        maintenance.append(
            {
                "action": "arm",
                "replace": current["armed_ref"],
                "interval_minutes": interval,
                "cron": cron_for(interval),
                "prompt": SCHEDULED_PROMPT,
            }
        )
    last_reset = str(kept.get("last_reset") or current["bound_at"])
    if parse_time(now) - parse_time(last_reset) > _hours(config.get("routine.context_reset_hours")):
        maintenance.append({"action": "clear_context"})
        kept["last_reset"] = now
    kept["last_run"] = now
    jobs: list[dict[str, Any]] = [
        {"job_id": HANDOFF_SCAN, "script": "handoff_scan", "every": 1, "forward": "if_any"}
    ]
    host = str(config.get("plugins.host_integration"))
    since = format_time(parse_time(now) - _hours(config.get("routine.teams_lookback_hours")))
    # One row per chat that still has a waiting Teams question (cards may go to several chats).
    for teams in teams_jobs(conn):
        jobs.append(
            {
                "job_id": TEAMS_REPLIES,
                "script": "teams_replies",
                "every": 1,
                "forward": "if_any",
                "input": {"chat_id": teams["chat_id"], "tasks": teams["tasks"]},
                # Pass these to routine_run as they are, with what the fetch returned.
                "run": {"job_id": TEAMS_REPLIES, "chat_id": teams["chat_id"]},
                "fetch": {
                    "kind": "teams.read_chat",
                    "chat_id": teams["chat_id"],
                    "since": since,
                    "tool": FETCH_TOOLS.get(host, {}).get("teams.read_chat"),
                    "arguments": {"chatId": teams["chat_id"], "top": TEAMS_READ_TOP},
                    # Newest first, a page at a time: keep reading while the result names a
                    # next page and its oldest message is not older than ``since``; pass all
                    # pages, in order, as a list.
                    "pages": {"next": "nextLink", "argument": "nextLink", "until": since},
                },
            }
        )
    return {
        "enabled": True,
        "generation": generation,
        "maintenance": maintenance,
        "jobs": jobs,
        "orchestrator_session": orchestrator_session(conn, now),
    }, kept


def orchestrator_session(conn: sqlite3.Connection, now: str) -> str | None:
    """Where forwarded results go: the session that holds or last held the scheduling lease.

    A lease that expired while the orchestrator sat idle still names it, and waking it is the
    point: it takes the lease again before it writes. A released lease names nobody; the
    results stay in the ledger for the next orchestrator (DN006 §8.9.3)."""
    del now  # an expired lease still names the orchestrator to wake
    held = lease_row(conn)
    if held is None or not held["holder_session"]:
        return None
    return str(held["holder_session"])


# ---------------------------------------------------------------------- the Teams job


def teams_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The ``teams-replies`` rows, one per chat that has a waiting Teams question.

    Each row's input is derived from the ledger: the chat's tasks whose Teams question is still
    waiting (a task leaves as soon as its question is no longer pending, DN006 §8.7), the Teams
    questions of those tasks in that chat (older ones included: a reply is matched to the
    question it answers, never pinned on whatever question waits now), the user's Teams ID from
    the latest card, and every message tsw sent, to leave out."""
    if (
        conn.execute("SELECT 1 FROM routine_jobs WHERE job_id = ?", (TEAMS_REPLIES,)).fetchone()
        is None
    ):
        return []
    cards = [
        dict(r)
        for r in conn.execute(
            "SELECT m.chat_id, m.task_id, m.request_id, m.message_id, MIN(m.sent_at) AS sent_at, "
            "r.status FROM channel_messages m JOIN input_requests r "
            "ON r.request_id = m.request_id WHERE m.channel = ? "
            "GROUP BY m.chat_id, m.request_id ORDER BY m.chat_id, m.task_id, sent_at",
            (TEAMS_CHANNEL,),
        )
    ]
    jobs: list[dict[str, Any]] = []
    for chat_id in sorted({str(c["chat_id"]) for c in cards}):
        here = [c for c in cards if c["chat_id"] == chat_id]
        waiting = sorted({c["task_id"] for c in here if c["status"] == "pending"})
        if not waiting:
            continue
        sender = conn.execute(
            "SELECT sender_id FROM channel_messages WHERE channel = ? AND chat_id = ? "
            "AND sender_id IS NOT NULL ORDER BY sent_at DESC LIMIT 1",
            (TEAMS_CHANNEL, chat_id),
        ).fetchone()
        # Message IDs are unique within a chat only (Teams uses times): both what tsw sent and
        # what was passed on are kept per chat.
        outbound = {
            str(r["message_id"])
            for r in conn.execute(
                "SELECT message_id FROM channel_messages WHERE channel = ? AND chat_id = ?",
                (TEAMS_CHANNEL, chat_id),
            )
        }
        seen = {
            str(r["message_id"])
            for r in conn.execute(
                "SELECT message_id FROM channel_replies WHERE chat_id = ?", (chat_id,)
            )
        }
        jobs.append(
            {
                "chat_id": chat_id,
                "sender_id": sender["sender_id"] if sender else None,
                "tasks": waiting,
                "requests": [c for c in here if c["task_id"] in waiting],
                "outbound": outbound,
                "seen": seen,
            }
        )
    return jobs


_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def plain_text(body: str) -> str:
    """The text of a Teams message body (HTML): tags removed, entities decoded, spaces folded."""
    return _SPACES.sub(" ", html.unescape(_TAGS.sub(" ", body))).strip()


ENVELOPES = ("messages", "value", "items", "data", "results")


def fetched_messages(fetched: Any) -> list[dict[str, Any]]:
    """The messages in what the host's chat tool returned, whatever envelope it used: one
    result, or a list of pages (each a result), or a plain list of messages."""
    if isinstance(fetched, str):
        try:
            fetched = json.loads(fetched)
        except ValueError:
            return []
    if isinstance(fetched, Mapping):
        for key in ENVELOPES:
            inner = fetched.get(key)
            if isinstance(inner, list):
                return fetched_messages(inner)
        return [dict(fetched)] if "id" in fetched else []
    if not isinstance(fetched, list):
        return []
    out: list[dict[str, Any]] = []
    for item in fetched:
        if isinstance(item, str) or (
            isinstance(item, Mapping) and any(isinstance(item.get(k), list) for k in ENVELOPES)
        ):
            out.extend(fetched_messages(item))  # a page
        elif isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _sender(message: Mapping[str, Any]) -> str | None:
    for key in ("from", "sender"):
        value = message.get(key)
        if isinstance(value, Mapping):
            user = value.get("user")
            if isinstance(user, Mapping) and user.get("id"):
                return str(user["id"])
            if value.get("id"):
                return str(value["id"])
    value = message.get("sender_id") or message.get("senderId")
    return str(value) if value else None


def _body(message: Mapping[str, Any]) -> str:
    body = message.get("body")
    if isinstance(body, Mapping):
        body = body.get("content")
    if not isinstance(body, str):
        body = message.get("content") or message.get("text") or ""
    return str(body)


def _created(message: Mapping[str, Any]) -> datetime | None:
    value = message.get("createdDateTime") or message.get("created") or message.get("time")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def starting_task(words: str, tasks: Iterable[str]) -> str | None:
    """The ID the message starts with, followed by nothing, a space or punctuation."""
    upper = words.upper()
    for task_id in sorted(tasks, key=len, reverse=True):
        if upper.startswith(task_id.upper()):
            rest = words[len(task_id) :]
            if not rest or not rest[0].isalnum():
                return task_id
    return None


def _question_for(
    words: str,
    created: datetime | None,
    requests: Sequence[Mapping[str, Any]],
    card_times: Mapping[str, datetime],
) -> Mapping[str, Any] | None:
    """The question a reply answers (DN008 §9: the reply must lead to the question's number).

    A reply that starts with a question number (``Q-T001-2``) answers that question. One that
    starts with a task ID answers the task's latest Teams question whose card went out before
    the reply was written: an old reply is never pinned on a question asked after it."""
    by_request = {str(r["request_id"]): r for r in requests}
    request_id = starting_task(words, by_request)
    if request_id is not None:
        return by_request[request_id]
    task_id = starting_task(words, {str(r["task_id"]) for r in requests})
    if task_id is None:
        return None
    asked = [r for r in requests if r["task_id"] == task_id]
    if created is not None:
        asked = [
            r
            for r in asked
            if card_times.get(str(r["request_id"]), parse_time(str(r["sent_at"]))) <= created
        ]
    return asked[-1] if asked else None


def select_replies(
    fetched: Any,
    *,
    requests: Sequence[Mapping[str, Any]],
    sender_id: str | None,
    since: str,
    exclude: set[str],
) -> list[dict[str, Any]]:
    """The ``teams_replies`` script (DN006 §8.9.5): messages from the user, within the lookback
    window, starting with a question number or a waiting task's ID, that tsw neither sent nor
    passed on before, each with the question it answers. Oldest first.

    ``requests`` are the Teams questions with their cards (``request_id``, ``task_id``,
    ``message_id`` of the card, ``sent_at``, ``status``), oldest first."""
    messages = fetched_messages(fetched)
    cards = {str(r["message_id"]): str(r["request_id"]) for r in requests}
    card_times: dict[str, datetime] = {}
    for message in messages:
        created = _created(message)
        request_id = cards.get(str(message.get("id") or ""))
        if request_id is not None and created is not None:
            card_times[request_id] = created
    floor = parse_time(since)
    out: list[dict[str, Any]] = []
    for message in messages:
        message_id = str(message.get("id") or "")
        if not message_id or message_id in exclude:
            continue
        sender = _sender(message)
        if sender_id and sender and sender != sender_id:
            continue  # the user's own messages only (user, 2026-11-09)
        created = _created(message)
        if created is not None and created < floor:
            continue
        words = plain_text(_body(message))
        question = _question_for(words, created, requests, card_times)
        if question is None:
            continue
        out.append(
            {
                "message_id": message_id,
                "task_id": str(question["task_id"]),
                "request_id": str(question["request_id"]),
                "pending": question["status"] == "pending",
                "text": words,
                "sent_at": format_time(created) if created else None,
            }
        )
    out.sort(key=lambda m: (m["sent_at"] or "", m["message_id"]))
    return out


def card(conn: sqlite3.Connection, request: Mapping[str, Any]) -> str:
    """The question card the orchestrator sends to Teams (DN006 §8.7 step 1)."""
    task_id = str(request["task_id"])
    row = task(conn, task_id)
    return (
        f"{CARD_PREFIX} {task_id} {request['request_id']}\n"
        f"任务：{row['title']}\n"
        f"问题：{request['question']}\n"
        f"回复时以任务 ID 开头，例如“{task_id} 你的答复”。\n"
        f"worker 会话：{WORKER_LINK}"
    )


# ---------------------------------------------------------------------- forwarding


def forward_text(conn: sqlite3.Connection, job_id: str, events: Iterable[Mapping[str, Any]]) -> str:
    """The message a job's result sends to the orchestrator, or "" when there is none."""
    lines: list[str] = []
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "input.handed_off":
            request = row_dict(
                conn.execute(
                    "SELECT r.question, s.reserved, s.session_ref FROM input_requests r "
                    "JOIN in_session_requests s ON s.request_id = r.request_id "
                    "WHERE r.request_id = ?",
                    (data.get("request_id"),),
                ).fetchone()
            )
            if request is None:
                continue
            reserved = "（reserved：只能转达给用户）" if request["reserved"] else ""
            lines.append(
                f"- {event.get('task_id')} {data.get('request_id')}{reserved}，"
                f"worker 会话 {request['session_ref']}：{request['question']}"
            )
        elif event.get("type") == "channel.reply_seen":
            late = "" if data.get("pending", True) else "（问题已不再待答：按迟到答复处理）"
            lines.append(
                f"- {event.get('task_id')} {data.get('request_id')}{late}，Teams 消息 "
                f"{data.get('message_id')}（{data.get('sent_at') or '时间未知'}）："
                f"{data.get('text')}"
            )
    if not lines:
        return ""
    if job_id == HANDOFF_SCAN:
        head = (
            "TaskSteward 例行项 handoff-scan：以下会话内问题超时无人回复，"
            "已转交给你（by: system）。"
            "按 handle-input skill 处理：在授权内用 handoff_resolve 代答并告诉 worker，"
            "或用 input_relay 问用户。"
        )
    else:
        head = (
            "TaskSteward 例行项 teams-replies：Teams 中有以下以等待中的任务 ID 开头的消息。"
            "按 handle-input skill 判断是否回答了问题；是答复时用 input_resolve 登记"
            "（渠道 teams-chat，message_ref 为消息 ID），再告诉 worker。"
        )
    return "\n".join([head, *lines])


# ---------------------------------------------------------------------- tools


@tool(
    "routine_start",
    "orchestrator",
    description="启动例行会话（routine.start_requested）：返回会话名、执行目录与创建参数",
)
def routine_start(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    user_turn = flag(inp, "user_turn")

    def build(ctx: CommitContext) -> list[EventDraft]:
        if not env.config.get("routine.enabled"):
            raise rejected("配置关闭了例行会话（routine.enabled）", reason="routine_disabled")
        adapter = str(env.config.get("roles.routine.session"))
        caps = SESSIONS.get(adapter)
        if caps is not None and caps.dispatch_window and not user_turn:
            raise rejected("只有用户本人的 turn 能创建例行会话", reason="g_window", adapter=adapter)
        newest = latest(ctx.conn)
        generation = (int(newest["generation"]) if newest else 0) + 1
        path = ctx.home.root / WORKDIR
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise rejected(
                "无法创建例行会话的目录", reason="workdir_unavailable", error=str(exc)
            ) from exc
        return [
            EventDraft(
                type="routine.start_requested",
                data={
                    "generation": generation,
                    "session_name": session_name(generation),
                    "workdir": WORKDIR,
                    "adapter": adapter,
                    "controller": env.caller.session_id,
                    "model": env.config.get("roles.routine.model"),
                    "effort": env.config.get("roles.routine.effort"),
                    "permission_mode": env.config.get("roles.routine.permission_mode"),
                    "prompt": START_PROMPT,
                    "background": START_BACKGROUND,
                },
            )
        ]

    return _cmd(env, "routine_start", inp, build)


@tool(
    "routine_bind", "orchestrator", description="绑定例行会话（routine.bound），之前的代次随之撤销"
)
def routine_bind(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    generation = integer(inp, "generation", minimum=1)
    session_id = text(inp, "session_id")

    def build(ctx: CommitContext) -> list[EventDraft]:
        newest = latest(ctx.conn)
        if newest is None or int(newest["generation"]) != generation:
            raise rejected("只能绑定最新的例行会话代次", reason="invalid_generation")
        if newest["status"] != "starting":
            raise rejected("该代次已绑定", reason="already_bound", status=newest["status"])
        taken = ctx.conn.execute(
            "SELECT 1 FROM attempts WHERE session_ref = ?", (session_id,)
        ).fetchone()
        if taken is not None or by_session(ctx.conn, session_id) is not None:
            raise rejected("该会话已绑定为其他角色", reason="session_in_use")
        return [
            EventDraft(
                type="routine.bound", data={"generation": generation, "session_ref": session_id}
            )
        ]

    return _cmd(env, "routine_bind", inp, build)


@tool("routine_armed", "routine", description="登记已设定的定时任务（routine.armed）")
def routine_armed(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    job_ref = text(inp, "job_ref")
    cron = text(inp, "cron")
    interval = integer(inp, "interval_minutes", minimum=1)

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = require_routine(ctx.conn, env)
        return [
            EventDraft(
                type="routine.armed",
                data={
                    "generation": int(row["generation"]),
                    "job_ref": job_ref,
                    "cron": cron,
                    "interval_minutes": interval,
                },
            )
        ]

    return _cmd(env, "routine_armed", inp, build)


@tool(
    "routine_run",
    "routine",
    description="运行一个例行项的脚本；有 fetch 的行带上取到的原始数据（DN006 §8.9.3）",
)
def routine_run(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    job_id = text(inp, "job_id")
    fetched = inp.get("fetched")

    def build(ctx: CommitContext) -> list[EventDraft]:
        require_routine(ctx.conn, env)
        if job_id == HANDOFF_SCAN:
            return [handed_off_event(due, "system") for due in handoff_due(ctx.conn, ctx.now)]
        if job_id == TEAMS_REPLIES:
            chat_id = opt_text(inp, "chat_id")
            job = next((j for j in teams_jobs(ctx.conn) if j["chat_id"] == chat_id), None)
            if job is None:
                raise rejected(
                    "例行表中没有这一行（chat_id 取自 routine_board 的 run）",
                    reason="unknown_job",
                    job_id=job_id,
                    chat_id=chat_id,
                )
            since = format_time(
                parse_time(ctx.now) - _hours(env.config.get("routine.teams_lookback_hours"))
            )
            found = select_replies(
                fetched,
                requests=job["requests"],
                sender_id=job["sender_id"],
                since=since,
                exclude=job["seen"] | job["outbound"],
            )
            return [
                EventDraft(
                    type="channel.reply_seen",
                    task_id=reply["task_id"],
                    data={
                        "job_id": TEAMS_REPLIES,
                        "channel": TEAMS_CHANNEL,
                        "chat_id": chat_id,
                        "request_id": reply["request_id"],
                        "pending": reply["pending"],
                        "message_id": reply["message_id"],
                        "text": reply["text"],
                        "sent_at": reply["sent_at"],
                    },
                )
                for reply in found
            ]
        raise rejected("例行表中没有这一行", reason="unknown_job", job_id=job_id)

    return _cmd(env, "routine_run", inp, build)


@tool(
    "channel_sent",
    "orchestrator",
    description="登记已发到 Teams 的问题卡片（chat ID、消息 ID、发送者），并加入 Teams 例行项",
)
def channel_sent(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    request_id = text(inp, "request_id")
    chat_id = text(inp, "chat_id")
    message_id = text(inp, "message_id")
    sender_id = opt_text(inp, "sender_id")

    def build(ctx: CommitContext) -> list[EventDraft]:
        request = row_dict(
            ctx.conn.execute(
                "SELECT r.status, s.relayed_to FROM input_requests r "
                "JOIN in_session_requests s ON s.request_id = r.request_id "
                "WHERE r.request_id = ? AND r.task_id = ?",
                (request_id, task_id),
            ).fetchone()
        )
        if request is None:
            raise rejected("没有这个会话内问题", reason="request_not_found", request_id=request_id)
        if request["status"] != "pending":
            raise rejected("问题已答复", reason="request_not_pending", request_id=request_id)
        if request["relayed_to"] != TEAMS_CHANNEL:
            raise rejected(
                "先用 input_relay（channel: teams-chat）转达，再发送卡片", reason="not_relayed"
            )
        drafts = [
            EventDraft(
                type="channel.message_sent",
                task_id=task_id,
                data={
                    "channel": TEAMS_CHANNEL,
                    "request_id": request_id,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "sender_id": sender_id,
                },
            )
        ]
        exists = ctx.conn.execute(
            "SELECT 1 FROM routine_jobs WHERE job_id = ?", (TEAMS_REPLIES,)
        ).fetchone()
        if exists is None:
            # The chats and the tasks to watch come from the cards themselves (teams_jobs).
            drafts.append(
                EventDraft(
                    type="routine.job_added",
                    data={"job_id": TEAMS_REPLIES, "script": "teams_replies", "input": {}},
                )
            )
        return drafts

    return _cmd(env, "channel_sent", inp, build)


def load_cache(home: Home) -> dict[str, Any]:
    try:
        value = json.loads((home.cache_dir / "routine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}
