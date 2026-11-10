# Tool inputs are untyped JSON checked field by field; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""A worker asks the user in its own session; after a timeout the orchestrator takes over
(DN006 §8.2–§8.5, DN002 T11 / T12).

- ``ask_user`` (worker): T11, the question stays in the worker's session.
- The ``UserPromptSubmit`` hook records each user message in that session while the question is
  open (``input.session_message``); ``user_answer`` (worker) cites one of them, so the user's
  words come from the host, never from the worker: T12, or ``input.late_response`` when the
  question was already answered elsewhere.
- No reply within ``input.in_session_timeout_minutes``: the routine board's ``handoff-scan`` or
  the system rule in ``sync`` records ``input.handed_off`` (``by: system``), and the routine
  session passes it on to the orchestrator (DN006 §8.9). ``input_handoff`` (worker) is kept.
- The orchestrator then answers within its authority (``handoff_resolve``, T12) or relays the
  question to the user (``input_relay``: in its own session or through Teams, DN006 §8.7; the
  answer comes back through ``input_resolve``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.lease import format_time, parse_time
from tsw.machine.base import (
    ToolEnv,
    flag,
    integer,
    items,
    opt_text,
    strings,
    task_and_attempt,
    text,
    tool,
)
from tsw.machine.model import (
    current_attempt,
    loads,
    next_item_id,
    require_status,
    review_cycle,
    row_dict,
    task,
)

IN_SESSION = "in_session"  # input_requests.kind
CHANNEL = "in-session"
# A message the host injects into a session on behalf of something other than the user: another
# session, an app event, a scheduled task. Never recorded as the user's words.
AUTOMATED_PREFIXES = (
    "<cross-session-message",
    "<side-sessions-event",
    "<task-notification",
    "<ci-monitor-event",
    "<scheduled-task",
    "<system-reminder",
)


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def timeout_minutes(config: EffectiveConfig) -> int:
    return int(config.get("input.in_session_timeout_minutes"))


def open_request(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    """The task's pending in-session question, with its routing state."""
    return row_dict(
        conn.execute(
            "SELECT r.*, s.session_ref, s.asked_at, s.handoff_after, s.reserved, s.responded_at, "
            "s.handed_off_at, s.relayed_to FROM input_requests r "
            "JOIN in_session_requests s ON s.request_id = r.request_id "
            "WHERE r.task_id = ? AND r.status = 'pending' AND r.kind = ?",
            (task_id, IN_SESSION),
        ).fetchone()
    )


def _request(conn: sqlite3.Connection, task_id: str, request_id: str) -> dict[str, Any]:
    row = row_dict(
        conn.execute(
            "SELECT r.*, s.session_ref, s.asked_at, s.handoff_after, s.reserved, s.responded_at, "
            "s.handed_off_at, s.relayed_to FROM input_requests r "
            "JOIN in_session_requests s ON s.request_id = r.request_id "
            "WHERE r.request_id = ? AND r.task_id = ?",
            (request_id, task_id),
        ).fetchone()
    )
    if row is None:
        raise rejected("没有这个会话内问题", reason="request_not_found", request_id=request_id)
    return row


REMINDER_OPEN, REMINDER_CLOSE = "<system-reminder>", "</system-reminder>"


def user_words(prompt: str) -> str:
    """The prompt without the ``<system-reminder>`` blocks the host puts in front of it.

    The Claude app prefixes a user's own message with reminders of its own (for example the
    linked sessions of a side session), so the payload of the ``UserPromptSubmit`` hook starts
    with ``<system-reminder>`` even when the rest is the user's message (found by the live
    ``host-e2e`` scenario). Only complete leading blocks are removed; what follows decides.
    """
    text = prompt.lstrip()
    while text.startswith(REMINDER_OPEN):
        end = text.find(REMINDER_CLOSE)
        if end < 0:
            return text
        text = text[end + len(REMINDER_CLOSE) :].lstrip()
    return text


def is_automated(prompt: str) -> bool:
    """A message the host injected (another session, an app event, a scheduled task), or one
    with nothing but host reminders in it: never the user's own words."""
    words = user_words(prompt)
    return not words or words.startswith(AUTOMATED_PREFIXES)


def session_message(
    conn: sqlite3.Connection, task_id: str, session_ref: str, prompt: str
) -> EventDraft | None:
    """What the ``UserPromptSubmit`` hook records: the user's message in the worker's session
    about its latest question, while that question has no answer from the user in this session
    yet. That includes a question the orchestrator or another channel already answered: the
    user's own reply there is a late answer that prevails (DN006 §8.5)."""
    # Only the session's latest question counts: once it is answered here, or a newer question
    # replaced it, older questions never collect messages again.
    request = row_dict(
        conn.execute(
            "SELECT s.request_id, s.answered_in_session, r.status, r.binding "
            "FROM in_session_requests s JOIN input_requests r ON r.request_id = s.request_id "
            "WHERE s.task_id = ? AND s.session_ref = ? "
            "ORDER BY r.rowid DESC LIMIT 1",
            (task_id, session_ref),
        ).fetchone()
    )
    if (
        request is None
        or request["answered_in_session"]
        or request["status"] not in ("pending", "resolved")
        or loads(request["binding"], {}).get("revision") != int(task(conn, task_id)["revision"])
        or is_automated(prompt)
    ):
        return None
    prompt = user_words(prompt)
    message_id = next_item_id(conn, "session_messages", "message_id", "M", task_id)
    row = task(conn, task_id)
    return EventDraft(
        type="input.session_message",
        task_id=task_id,
        revision=int(row["revision"]),
        data={
            "message_id": message_id,
            "request_id": request["request_id"],
            "session_ref": session_ref,
            "text": prompt,
        },
    )


def handoff_due(conn: sqlite3.Connection, now: str) -> list[dict[str, Any]]:
    """Open in-session questions with no reply from the user past their handoff time."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT r.request_id, r.task_id, s.handoff_after FROM input_requests r "
            "JOIN in_session_requests s ON s.request_id = r.request_id "
            "WHERE r.status = 'pending' AND r.kind = ? AND s.handed_off_at IS NULL "
            "AND s.responded_at IS NULL AND s.handoff_after <= ? ORDER BY s.handoff_after",
            (IN_SESSION, now),
        )
    ]


def handed_off_event(request: Mapping[str, Any], by: str) -> EventDraft:
    return EventDraft(
        type="input.handed_off",
        task_id=str(request["task_id"]),
        data={
            "request_id": request["request_id"],
            "by": by,
            "handoff_after": request["handoff_after"],
        },
    )


def _findings(
    conn: sqlite3.Connection, task_id: str, attempt_id: str, cycle: int, refs: list[Any]
) -> list[dict[str, Any]]:
    checked = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise rejected("findings 元素应为 {round, finding}", reason="invalid_input")
        found = conn.execute(
            "SELECT 1 FROM findings WHERE task_id = ? AND attempt = ? AND cycle = ? AND "
            "round = ? AND finding_id = ? AND blocking = 1",
            (task_id, attempt_id, cycle, ref.get("round"), ref.get("finding")),
        ).fetchone()
        if found is None:
            raise rejected("本周期没有这个阻断项", reason="finding_not_found", ref=ref)
        checked.append({"round": ref["round"], "finding": ref["finding"]})
    return checked


def _resolution(
    ctx: CommitContext,
    row: Mapping[str, Any],
    request: Mapping[str, Any],
    data: dict[str, Any],
) -> list[EventDraft] | None:
    """T12 for a pending in-session question, or None when the binding no longer holds."""
    require_status(dict(row), "waiting_user")
    binding = loads(request["binding"], {})
    attempt_row = current_attempt(ctx.conn, str(row["task_id"]))
    if (
        binding.get("revision") != int(row["revision"])
        or attempt_row is None
        or binding.get("attempt") != attempt_row["attempt"]
        or binding.get("generation") != int(attempt_row["generation"])
    ):
        return None
    data.update(
        transition="T12",
        new_cycle=row["waiting_revision"] is not None
        and int(row["waiting_revision"]) != int(row["revision"]),
        resume_substate=binding.get("substate") or "executing",
        resolves=binding.get("judgments", []),
    )
    return [
        EventDraft(
            type="input.resolved",
            task_id=str(row["task_id"]),
            revision=int(row["revision"]),
            data=data,
        )
    ]


def _review_decisions(
    ctx: CommitContext,
    inp: Mapping[str, Any],
    row: Mapping[str, Any],
    data: Mapping[str, Any],
    base: Mapping[str, Any],
    *,
    extension_limit: int | None,
) -> list[EventDraft]:
    """Extra rounds, approved deferrals and overruled findings that come with an answer."""
    task_id, revision = str(row["task_id"]), int(row["revision"])
    attempt_row = current_attempt(ctx.conn, task_id)
    assert attempt_row is not None
    cycle = int(row["cycle"]) + (1 if data.get("new_cycle") else 0)
    drafts: list[EventDraft] = []
    rounds = int(inp.get("extend_rounds") or 0)
    if rounds:
        integer(inp, "extend_rounds", minimum=1)
        if extension_limit is not None:
            cyc = review_cycle(ctx.conn, task_id, str(attempt_row["attempt"]), cycle)
            if int(cyc["orchestrator_extension"]) + rounds > extension_limit:
                raise rejected(
                    "主会话累计追加超过上限，只能交给人类",
                    reason="extension_limit",
                    limit=extension_limit,
                )
        drafts.append(
            EventDraft(
                type="review.budget_extended",
                task_id=task_id,
                revision=revision,
                data={**base, "attempt": attempt_row["attempt"], "cycle": cycle, "rounds": rounds},
            )
        )
    for key, event in (
        ("defer_findings", "review.defer_approved"),
        ("overrule_findings", "review.finding_overruled"),
    ):
        refs = items(inp, key, required=False) or []
        if refs:
            drafts.append(
                EventDraft(
                    type=event,
                    task_id=task_id,
                    revision=revision,
                    data={
                        **base,
                        "attempt": attempt_row["attempt"],
                        "cycle": cycle,
                        "findings": _findings(
                            ctx.conn, task_id, str(attempt_row["attempt"]), cycle, refs
                        ),
                    },
                )
            )
    return drafts


# ---------------------------------------------------------------------- worker


@tool(
    "ask_user",
    "worker",
    description="在本会话中直接问用户（T11）；超时无回复时转交主会话（DN006 §8.2）",
)
def ask_user(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    question = text(inp, "question")
    item_ids = strings(items(inp, "items", required=False) or [], "items")
    reserved = flag(inp, "reserved")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        if row["status"] != "in_progress":
            raise rejected("只有执行中才能提问", reason="invalid_state")
        triggers = []
        for item_id in item_ids:
            found = row_dict(
                ctx.conn.execute(
                    "SELECT * FROM judgments WHERE item_id = ? AND task_id = ?", (item_id, task_id)
                ).fetchone()
            )
            if found is None or found["status"] != "open":
                raise rejected("没有待处理的事项", reason="judgment_not_open", item=item_id)
            triggers.append(str(found["trigger"]))
        revision, cycle = int(row["revision"]), int(row["cycle"])
        request_id = next_item_id(ctx.conn, "input_requests", "request_id", "Q", task_id)
        handoff_after = format_time(
            parse_time(ctx.now) + timedelta(minutes=timeout_minutes(env.config))
        )
        return [
            EventDraft(
                type="input.escalated",
                task_id=task_id,
                revision=revision,
                attempt=str(current["attempt"]),
                data={
                    "transition": "T11",
                    "request_id": request_id,
                    "kind": IN_SESSION,
                    "channel": CHANNEL,
                    "trigger": ",".join(triggers) or "worker.question",
                    "question": question,
                    "reserved": reserved,
                    "session_ref": env.caller.session_id,
                    "handoff_after": handoff_after,
                    "binding": {
                        "revision": revision,
                        "attempt": current["attempt"],
                        "generation": int(current["generation"]),
                        "cycle": cycle,
                        "substate": row["substate"],
                        "judgments": item_ids,
                    },
                    "resolves": item_ids,
                },
            )
        ]

    return _cmd(env, "ask_user", inp, build)


@tool(
    "user_answer",
    "worker",
    description="登记用户在本会话中的答复（T12）：引用 hook 记下的用户消息编号",
)
def user_answer(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    request_id = text(inp, "request_id")
    message_id = text(inp, "message_id")
    answer = text(inp, "answer")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, _ = task_and_attempt(ctx.conn, env, task_id)
        request = _request(ctx.conn, task_id, request_id)
        message = row_dict(
            ctx.conn.execute(
                "SELECT * FROM session_messages WHERE message_id = ? AND task_id = ?",
                (message_id, task_id),
            ).fetchone()
        )
        if (
            message is None
            or message["request_id"] != request_id
            or message["session_ref"] != env.caller.session_id
        ):
            raise rejected(
                "引用的不是本会话中记录的用户消息",
                reason="message_not_found",
                message_id=message_id,
            )
        source = {
            "channel": CHANNEL,
            "principal": "session-owner",
            "message_ref": message_id,
            "text": message["text"],
            "request_id": request_id,
            "target": [task_id, int(row["revision"]), "answer"],
        }
        data: dict[str, Any] = {
            "request_id": request_id,
            "source": source,
            "source_kind": "user",
            "answer": answer,
        }
        resolved = _resolution(ctx, row, request, data) if request["status"] == "pending" else None
        if resolved is None:
            # Answered elsewhere first (the orchestrator, another channel) or the binding moved
            # on: recorded, and the user's own words prevail from here on (DN006 §8.5).
            earlier = loads(request["resolution"], None)
            return [
                EventDraft(
                    type="input.late_response",
                    task_id=task_id,
                    revision=int(row["revision"]),
                    data={**data, "supersedes": earlier},
                )
            ]
        base = {"decision_id": request_id, "resolves": [], "user_source": source}
        return [
            *resolved,
            *_review_decisions(
                ctx, inp, row, data, {**base, "source_kind": "user"}, extension_limit=None
            ),
        ]

    return _cmd(env, "user_answer", inp, build)


@tool(
    "input_handoff",
    "worker",
    description="会话内问题超时无回复：转交主会话判断（DN006 §8.3）",
)
def input_handoff(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    request_id = text(inp, "request_id")

    def build(ctx: CommitContext) -> list[EventDraft]:
        task_and_attempt(ctx.conn, env, task_id)
        request = _request(ctx.conn, task_id, request_id)
        if request["status"] != "pending" or request["handed_off_at"] is not None:
            raise rejected("问题已答复或已转交", reason="request_not_pending")
        if request["responded_at"] is not None:
            raise rejected("用户已在会话中回复，不转交", reason="user_responded")
        if parse_time(ctx.now) < parse_time(str(request["handoff_after"])):
            raise rejected(
                "还没到转交时间", reason="handoff_not_due", handoff_after=request["handoff_after"]
            )
        return [handed_off_event(request, "worker")]

    return _cmd(env, "input_handoff", inp, build)


# ---------------------------------------------------------------------- orchestrator


def _handed_off(ctx: CommitContext, task_id: str, request_id: str) -> dict[str, Any]:
    request = _request(ctx.conn, task_id, request_id)
    if request["status"] != "pending":
        raise rejected("问题已答复", reason="request_not_pending", request_id=request_id)
    if request["handed_off_at"] is None:
        raise rejected(
            "问题仍在 worker 会话中等待用户，还没有转交主会话",
            reason="not_handed_off",
            handoff_after=request["handoff_after"],
        )
    return request


@tool(
    "handoff_resolve",
    "orchestrator",
    description="对转交的会话内问题代为决定（T12）：回答、追加轮次、认定不成立或批准延后",
)
def handoff_resolve(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    request_id = text(inp, "request_id")
    decision = text(inp, "decision")
    rationale = text(inp, "rationale")
    basis = strings(items(inp, "basis"), "basis")
    confidence = text(inp, "confidence")
    answer = text(inp, "answer")
    if decision not in ("answer", "extend", "overrule", "defer"):
        raise rejected("未知的决定", reason="invalid_input", field="decision")
    if confidence not in ("high", "medium", "low"):
        raise rejected("confidence 必须是 high、medium 或 low", reason="invalid_input")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        request = _handed_off(ctx, task_id, request_id)
        if request["reserved"]:
            raise rejected(
                "worker 标明只有用户能决定：只能转达给用户（input_relay）或继续等待",
                reason="reserved_for_user",
            )
        decision_id = next_item_id(ctx.conn, "decisions", "decision_id", "D", task_id)
        data: dict[str, Any] = {
            "request_id": request_id,
            "source": None,
            "source_kind": "orchestrator",
            "decision_id": decision_id,
            "answer": answer,
        }
        resolved = _resolution(ctx, row, request, data)
        if resolved is None:
            raise rejected("问题的上下文已变化（revision 或 attempt）", reason="binding_changed")
        expected = {
            "extend": "extend_rounds",
            "defer": "defer_findings",
            "overrule": "overrule_findings",
        }
        if decision in expected and not inp.get(expected[decision]):
            raise rejected("决定缺少对应的内容", reason="invalid_input", field=expected[decision])
        record = EventDraft(
            type="orchestrator.decision",
            task_id=task_id,
            revision=int(row["revision"]),
            data={
                "decision_id": decision_id,
                "trigger": "input.handed_off",
                "decision": decision,
                "rationale": rationale,
                "basis": basis,
                "confidence": confidence,
                "answer": answer,
                "resolves": [],
                "request_id": request_id,
            },
        )
        base = {"decision_id": decision_id, "resolves": [], "source_kind": "orchestrator"}
        return [
            record,
            *resolved,
            *_review_decisions(
                ctx,
                inp,
                row,
                data,
                base,
                extension_limit=int(env.config.get("review.orchestrator_extension_limit")),
            ),
        ]

    return _cmd(env, "handoff_resolve", inp, build)


# The orchestrator's own session as a relay target: what input.relayed and the user's source
# record for the ``in-session`` human channel, and the name input_relay accepted before T256.
MAIN_SESSION = "main-session"


def _loaded_channels(config: EffectiveConfig) -> list[str]:
    from tsw.plugins.catalog import CHANNELS  # noqa: PLC0415  (catalog imports config)

    loaded = [c for c in config.get("plugins.human_channels") or [] if c in CHANNELS]
    return [c for c in loaded if CHANNELS[c].request_response]


def relay_channels(config: EffectiveConfig) -> list[str]:
    """Where the orchestrator may pass a handed-off question on to the user (DN006 §8.6): the
    human channels ``plugins.human_channels`` loads, nothing else. ``in-session`` is the
    orchestrator asking in its own session, also accepted under its old name ``main-session``;
    with only ``teams-chat`` loaded the orchestrator's session is not a way to the user. The
    worker asking in its own session is not a human channel and needs none of them."""
    loaded = _loaded_channels(config)
    return [*loaded, *([MAIN_SESSION] if CHANNEL in loaded else [])]


def user_channels(config: EffectiveConfig) -> list[str]:
    """The human channels a handed-off question for the user goes to (DN006 §8.1b): the loaded
    ones in ``input.channels`` (by default every loaded one). Empty when none is loaded: the
    question then stays with the worker's session."""
    loaded = _loaded_channels(config)
    return [c for c in config.get("input.channels") or [] if c in loaded]


@tool(
    "input_relay",
    "orchestrator",
    description=(
        "把转交的会话内问题转达给用户：只用 plugins.human_channels 加载的人类渠道"
        "（in-session 为在主会话中直接问，teams-chat 等按渠道的 skill）；答复用 input_resolve 记录"
    ),
)
def input_relay(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    request_id = text(inp, "request_id")
    channel = text(inp, "channel")

    def build(ctx: CommitContext) -> list[EventDraft]:
        request = _handed_off(ctx, task_id, request_id)
        allowed = relay_channels(env.config)
        if channel not in allowed:
            raise rejected(
                "不能转达到这个渠道：转达只用 plugins.human_channels 加载的人类渠道",
                reason="invalid_channel",
                allowed=allowed,
            )
        # The record keeps naming where the answer will come from (DN006 §8.6): the
        # orchestrator's session is main-session, as in every earlier input.relayed; the human
        # channel used is ``plugin``.
        in_main = channel in (CHANNEL, MAIN_SESSION)
        data: dict[str, Any] = {
            "request_id": request["request_id"],
            "channel": MAIN_SESSION if in_main else channel,
            "plugin": CHANNEL if in_main else channel,
            "note": opt_text(inp, "note"),
        }
        if channel == "teams-chat":
            # The card the orchestrator sends next, then registers with channel_sent (DN006 §8.7).
            # card_html is what goes out (Teams shows a text message on one line, CC006 §7).
            from tsw.machine.routine import card, card_html  # noqa: PLC0415  (imports this module)

            data |= {
                "card": card(ctx.conn, request),
                "card_html": card_html(ctx.conn, request),
                "chat": env.config.get("input.teams.chat"),
            }
        return [EventDraft(type="input.relayed", task_id=task_id, data=data)]

    return _cmd(env, "input_relay", inp, build)
