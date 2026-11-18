"""Session labels for an embedding portal: ``POST /workdeck/labels`` (DN006 §8.10, T297).

The contract is the embedding portal's (workdeck DN005 §10, decision 0023): the portal asks for
the sessions it lists, ``{"sessions": [{"id": "<session id>"}, ...]}``, and shows the plain-text
labels the plugin answers, ``{"labels": {"<session id>": [{"text", "title", "link"}]}}``. The
labels are the ones workdeck's in-process tsw annotator made from this data directory's
projection (``readAnnotations`` in its ``src/annotators/tsw.ts``), which the endpoint replaces:

=============================  =========================  ===================
session                        text                       link
=============================  =========================  ===================
``attempts.session_ref``       ``<task> · <attempt> ·     ``/tasks/<task>``
                               worker``
``leases.holder_session``      ``orchestrator``           ``/`` (the board)
``routine_sessions.session_    ``routine g<generation>``  none
ref``
=============================  =========================  ===================

``title`` is what the portal showed as the annotator label's tooltip: its ``detail``, one
``key: value`` per line. Reading is all this does; nothing is written to the ledger.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any, cast

from tsw.errors import rejected
from tsw.host.status import task_href

LABELS_PATH = "/workdeck/labels"
# The contract's limits (workdeck DN005 §10).
MAX_LABELS = 64
MAX_TEXT = 256
MAX_ID = 512
# A full refresh lists every session the portal shows; ids are short, so this is generous.
MAX_LABELS_BODY = 1 << 20

Label = dict[str, str]


def requested_ids(body: Any) -> list[str]:
    """The session ids asked for, in order and without repeats.

    Unknown fields are ignored (the contract only grows); an entry whose ``id`` is not a string of
    1-512 characters is skipped. A body that is not an object with a ``sessions`` array is
    refused, so the portal keeps the labels it had.
    """
    fields = cast("dict[str, Any]", body) if isinstance(body, dict) else {}
    sessions: Any = fields.get("sessions")
    if not isinstance(sessions, list):
        raise rejected("请求体应为带 sessions 数组的 JSON 对象", reason="invalid_input")
    out: dict[str, None] = {}
    for entry in cast("list[Any]", sessions):
        found: Any = cast("dict[str, Any]", entry).get("id") if isinstance(entry, dict) else None
        if isinstance(found, str) and 0 < len(found) <= MAX_ID:
            out[found] = None
    return list(out)


def _title(detail: Iterable[tuple[str, Any]]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in detail)


def _label(text: str, title: str, link: str | None) -> Label:
    out = {"text": text[:MAX_TEXT], "title": title[:MAX_TEXT]}
    if link is not None:
        out["link"] = link
    return out


def all_labels(conn: sqlite3.Connection) -> dict[str, list[Label]]:
    """Every session's labels by host session id: workers, then orchestrators, then routines."""
    out: dict[str, list[Label]] = {}

    def add(session: Any, label: Label) -> None:
        if session:
            out.setdefault(str(session), []).append(label)

    for row in conn.execute(
        "SELECT task_id, attempt, phase, session_ref FROM attempts "
        "WHERE session_ref IS NOT NULL ORDER BY rowid"
    ):
        task, attempt = str(row["task_id"]), str(row["attempt"])
        detail = (("task", task), ("attempt", attempt), ("phase", row["phase"]), ("role", "worker"))
        add(
            row["session_ref"],
            _label(f"{task} · {attempt} · worker", _title(detail), "/" + task_href(task)),
        )
    for row in conn.execute(
        "SELECT name, holder_session FROM leases WHERE holder_session IS NOT NULL ORDER BY rowid"
    ):
        detail = (("role", "orchestrator"), ("lease", row["name"]))
        add(row["holder_session"], _label("orchestrator", _title(detail), "/"))
    for row in conn.execute(
        "SELECT generation, status, session_ref FROM routine_sessions "
        "WHERE session_ref IS NOT NULL ORDER BY generation"
    ):
        generation = row["generation"]
        detail = (("role", "routine"), ("generation", generation), ("phase", row["status"]))
        add(row["session_ref"], _label(f"routine g{generation}", _title(detail), None))
    return out


def answer(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, Any]:
    """The contract's answer: every session asked for, an empty list when it has no label."""
    found: Mapping[str, list[Label]] = all_labels(conn)
    return {"labels": {sid: list(found.get(sid, []))[:MAX_LABELS] for sid in ids}}
