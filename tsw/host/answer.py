"""Answering a worker's question from the Dashboard (DN006 §8.10 "在 Dashboard 中回答问题", T357).

The Dashboard records nothing: it delivers the words the user confirmed to the worker's session
as the user's message (:mod:`tsw.host.session_inbox`), where the hook records them and the worker
registers the answer, as if the user had typed them there. This module decides from the
projection whether a question can be answered that way and follows the answer through the ledger:
delivered, received by the worker's session (the hook recorded the same words for the question),
registered (the question was answered by that message).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from tsw.home import Home
from tsw.lease import parse_time
from tsw.machine.ask_user import collecting
from tsw.machine.model import loads
from tsw.storage.fs import atomic_write

ANSWERABLE_KIND = "in_session"

# Why a question cannot take an answer from the page now, in the user's words.
CHANGED = {
    "no_question": "没有这个待答问题",
    "kind": "这个问题由主会话交给你，请在主会话中回答",
    "cancelled": "问题已取消",
    "superseded": "问题已被新的问题或需求变更取代",
    "not_waiting": "任务已不在等待用户",
    "attempt": "提问的 worker 会话已被替换",
    "not_live": "worker 会话不在运行",
    "moved_on": (
        "这个问题已不能在 worker 会话中再回答（之后有了新的问题，或你已在 worker 会话中答复过）"
    ),
}
CHANNELS = {
    "in-session": "worker 会话",
    "main-session": "主会话",
    "teams-chat": "Teams",
    "hub-portal": "Hub Portal",
}


@dataclass(frozen=True)
class Question:
    """A question the page may answer and where its answer goes."""

    task_id: str
    request_id: str
    status: str
    session_ref: str
    resolution: dict[str, Any] | None


class Refused(Exception):
    """The question cannot take the answer: ``code`` says why, ``details`` what the page shows."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def question(conn: sqlite3.Connection, task_id: str, request_id: str) -> Question:
    """The question the page answers; Refused when there is none to answer any more (no such
    question, one for the main session, cancelled or superseded). Whether its answer may go out
    now is :func:`deliverable`'s to say: a retry or an answer given elsewhere is told first."""
    row = conn.execute(
        "SELECT r.request_id, r.kind, r.status, r.resolution, s.session_ref "
        "FROM input_requests r LEFT JOIN in_session_requests s ON s.request_id = r.request_id "
        "WHERE r.request_id = ? AND r.task_id = ?",
        (request_id, task_id),
    ).fetchone()
    if row is None:
        raise Refused("changed", CHANGED["no_question"])
    if row["kind"] != ANSWERABLE_KIND or not row["session_ref"]:
        raise Refused("changed", CHANGED["kind"])
    status = str(row["status"])
    if status in ("cancelled", "superseded"):
        raise Refused("changed", CHANGED[status])
    return Question(
        task_id=task_id,
        request_id=request_id,
        status=status,
        session_ref=str(row["session_ref"]),
        resolution=loads(row["resolution"], None),
    )


def deliverable(conn: sqlite3.Connection, task_id: str, request_id: str) -> Question:
    """The question, when the page may deliver an answer to it now; Refused otherwise.

    A pending question goes to the session it was asked in, which must be the task's current
    attempt's, live, with the task waiting on the user. An answered question is returned too: the
    caller refuses it unless the user chose to send anyway (``late``)."""
    row = conn.execute(
        "SELECT r.request_id, r.kind, r.status, r.binding, r.resolution, s.session_ref "
        "FROM input_requests r LEFT JOIN in_session_requests s ON s.request_id = r.request_id "
        "WHERE r.request_id = ? AND r.task_id = ?",
        (request_id, task_id),
    ).fetchone()
    if row is None:
        raise Refused("changed", CHANGED["no_question"])
    if row["kind"] != ANSWERABLE_KIND or not row["session_ref"]:
        raise Refused("changed", CHANGED["kind"])
    status = str(row["status"])
    if status in ("cancelled", "superseded"):
        raise Refused("changed", CHANGED[status])
    task = conn.execute(
        "SELECT status, revision, current_attempt FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    attempt = conn.execute(
        "SELECT attempt, phase, generation, session_ref FROM attempts "
        "WHERE task_id = ? AND attempt = ?",
        (task_id, task["current_attempt"] if task else None),
    ).fetchone()
    binding = loads(row["binding"], {})
    if (
        attempt is None
        or attempt["session_ref"] != row["session_ref"]
        or binding.get("attempt") != attempt["attempt"]
        or binding.get("generation") != int(attempt["generation"])
    ):
        raise Refused("changed", CHANGED["attempt"])
    if status == "pending" and task["status"] != "waiting_user":
        raise Refused("changed", CHANGED["not_waiting"])
    if attempt["phase"] != "live":
        raise Refused("not_running", CHANGED["not_live"])
    # The hook ties a message in the session to one question only (``collecting``): the words
    # go out only when that is this question, so they never land on another one (review F1).
    if collecting(conn, task_id, str(row["session_ref"]), int(task["revision"])) != request_id:
        raise Refused("changed", CHANGED["moved_on"])
    return Question(
        task_id=task_id,
        request_id=request_id,
        status=status,
        session_ref=str(row["session_ref"]),
        resolution=loads(row["resolution"], None),
    )


def effective(resolution: dict[str, Any] | None) -> dict[str, Any]:
    """What the page shows of the answer that took effect: where it came from and its words."""
    resolution = resolution or {}
    source = resolution.get("source") if isinstance(resolution.get("source"), dict) else None
    if resolution.get("source_kind") == "orchestrator" or source is None:
        return {"by": "主会话代答", "text": str(resolution.get("answer") or "")}
    channel = str(source.get("channel") or "")
    return {
        "by": CHANNELS.get(channel, channel or "其他渠道"),
        "text": str(source.get("text") or resolution.get("answer") or ""),
        "message_ref": source.get("message_ref"),
    }


def _same(a: str, b: str) -> bool:
    return a.strip() == b.strip()


def received(conn: sqlite3.Connection, q: Question, text: str) -> str | None:
    """The message id under which the hook recorded these words for the question in its session,
    or None when it has not (yet)."""
    for row in conn.execute(
        "SELECT message_id, text FROM session_messages "
        "WHERE request_id = ? AND task_id = ? AND session_ref = ? ORDER BY sequence",
        (q.request_id, q.task_id, q.session_ref),
    ):
        if _same(str(row["text"]), text):
            return str(row["message_id"])
    return None


def progress(conn: sqlite3.Connection, task_id: str, request_id: str, text: str) -> dict[str, Any]:
    """How far an answer the page delivered has come, read from the ledger.

    ``sent``: not recorded in the worker's session yet. ``received``: the hook recorded it
    (``message_id``). ``registered``: the worker answered the question with that message.
    ``answered``: the question was answered by something else first (``effective``)."""
    row = conn.execute(
        "SELECT r.status, r.resolution, s.session_ref FROM input_requests r "
        "LEFT JOIN in_session_requests s ON s.request_id = r.request_id "
        "WHERE r.request_id = ? AND r.task_id = ?",
        (request_id, task_id),
    ).fetchone()
    if row is None:
        return {"state": "changed", "message": CHANGED["no_question"]}
    q = Question(task_id, request_id, str(row["status"]), str(row["session_ref"] or ""), None)
    message_id = received(conn, q, text)
    resolution: dict[str, Any] = loads(row["resolution"], None) or {}
    if row["status"] == "resolved":
        found: Any = resolution.get("source")
        ref = cast("dict[str, Any]", found).get("message_ref") if isinstance(found, dict) else None
        if message_id is not None and ref == message_id:
            return {"state": "registered", "message_id": message_id}
        if message_id is not None:
            # Sent after another answer took effect (``late``): the worker handles it as a late
            # answer (DN006 §8.1c), which does not change the question.
            return {"state": "received", "message_id": message_id, "late": True}
        return {"state": "answered", "effective": effective(resolution)}
    if row["status"] != "pending":
        return {"state": "changed", "message": CHANGED.get(str(row["status"]), "问题已变化")}
    if message_id is not None:
        return {"state": "received", "message_id": message_id}
    return {"state": "sent"}


# ---------------------------------------------------------------------- delivery notes
# Which question each delivered answer is for (review r2 F1): the hook records a message in the
# worker's session when the session reads it, and the question may have moved on by then (answered
# elsewhere, a new one asked). The note ties the words to the question they answer; the hook then
# links the message to that question or, when it no longer takes answers there, to none. The
# notes are a cache next to the routine's, not the ledger: losing them only falls back to the
# session's latest question, as for any message the user writes there.
NOTES = "dashboard-answers.json"
NOTES_KEPT = 50
NOTE_TTL = timedelta(hours=1)
NOT_NOTED = object()  # the words are not a delivery the Dashboard noted


def _notes(home: Home, now: str) -> list[dict[str, Any]]:
    try:
        raw: Any = json.loads((home.cache_dir / NOTES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = cast("list[Any]", raw) if isinstance(raw, list) else []
    fresh: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, Any]", item)
        try:
            if parse_time(now) - parse_time(str(entry.get("at"))) <= NOTE_TTL:
                fresh.append(entry)
        except ValueError:
            continue
    return fresh[-NOTES_KEPT:]


def _save(home: Home, items: list[dict[str, Any]]) -> None:
    home.cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(home.cache_dir / NOTES, json.dumps(items, ensure_ascii=False).encode("utf-8"))


def note(home: Home, session_ref: str, request_id: str, text: str, now: str) -> None:
    """Before writing into the session: these words are the answer to this question."""
    items = _notes(home, now)
    items.append({"session": session_ref, "request_id": request_id, "text": text, "at": now})
    _save(home, items[-NOTES_KEPT:])


def unnote(home: Home, session_ref: str, text: str, now: str) -> None:
    """Nothing was written after all: the note must not catch the user's own words later."""
    items = _notes(home, now)
    for i in range(len(items) - 1, -1, -1):
        if items[i].get("session") == session_ref and _same(str(items[i].get("text")), text):
            del items[i]
            _save(home, items)
            return


def take(home: Home, session_ref: str, text: str, now: str) -> object:
    """For the hook: the question the Dashboard delivered these words for (and the note is used
    up), or ``NOT_NOTED``."""
    items = _notes(home, now)
    for i in range(len(items) - 1, -1, -1):
        if items[i].get("session") == session_ref and _same(str(items[i].get("text")), text):
            found = str(items[i].get("request_id"))
            del items[i]
            _save(home, items)
            return found
    return NOT_NOTED
