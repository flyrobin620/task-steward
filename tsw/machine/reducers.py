"""Reducers of the state-machine events (DN002 §7).

Reducers apply what a command already decided; they do not re-run guards. They do check the
transition table, so a ledger can never move a task along a transition DN002 does not define.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from tsw.errors import rejected
from tsw.events import Event
from tsw.machine.model import TRANSITIONS, WORKER_TRANSITIONS, dumps, loads
from tsw.projection import Reducer


def _tid(event: Event) -> str:
    assert event.task_id is not None
    return event.task_id


def _status(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise rejected("账本引用了不存在的任务", reason="ledger_inconsistent", task_id=task_id)
    return str(row["status"])


def _move(conn: sqlite3.Connection, event: Event) -> str | None:
    """Apply ``data.transition`` if present; returns the transition id."""
    tid = event.data.get("transition")
    if tid is None:
        return None
    transition = TRANSITIONS[str(tid)]
    task_id = _tid(event)
    current = _status(conn, task_id)
    if current not in transition.sources:
        raise rejected(
            "账本中的迁移与当前状态不符",
            reason="ledger_inconsistent",
            task_id=task_id,
            transition=tid,
            status=current,
        )
    conn.execute(
        "UPDATE tasks SET previous_status = status, status = ? WHERE task_id = ?",
        (transition.target, task_id),
    )
    return str(tid)


def _substate(conn: sqlite3.Connection, event: Event, wid: str) -> None:
    transition = WORKER_TRANSITIONS[wid]
    task_id = _tid(event)
    row = conn.execute("SELECT substate FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row["substate"] not in transition.sources:
        raise rejected(
            "账本中的子状态迁移与当前子状态不符",
            reason="ledger_inconsistent",
            task_id=task_id,
            transition=wid,
            substate=row["substate"],
        )
    conn.execute("UPDATE tasks SET substate = ? WHERE task_id = ?", (transition.target, task_id))


def _set(conn: sqlite3.Connection, task_id: str, **values: Any) -> None:
    columns = ", ".join(f"{key} = ?" for key in values)
    conn.execute(f"UPDATE tasks SET {columns} WHERE task_id = ?", (*values.values(), task_id))


def _touch(conn: sqlite3.Connection, event: Event, business: bool = True) -> None:
    conn.execute("UPDATE tasks SET version = version + 1 WHERE task_id = ?", (_tid(event),))
    if business:
        _set(conn, _tid(event), business_changed_at=event.time)


def _new_cycle(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET cycle = cycle + 1, substate = 'executing', submission = NULL "
        "WHERE task_id = ?",
        (task_id,),
    )


def _attempt(conn: sqlite3.Connection, task_id: str, attempt: str, **values: Any) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO attempts(task_id, attempt, phase) VALUES(?, ?, 'planned')",
        (task_id, attempt),
    )
    if values:
        columns = ", ".join(f"{key} = ?" for key in values)
        conn.execute(
            f"UPDATE attempts SET {columns} WHERE task_id = ? AND attempt = ?",
            (*values.values(), task_id, attempt),
        )


def _release_claims(
    conn: sqlite3.Connection, task_id: str, resources: list[str] | None = None
) -> None:
    if resources is None:
        conn.execute(
            "UPDATE resource_claims SET status = 'released' WHERE owner_task = ?", (task_id,)
        )
        return
    for resource in resources:
        conn.execute(
            "UPDATE resource_claims SET status = 'released' WHERE owner_task = ? AND resource = ?",
            (task_id, resource),
        )


def _claim(conn: sqlite3.Connection, task_id: str, resources: list[str]) -> None:
    for resource in resources:
        conn.execute(
            "INSERT INTO resource_claims(resource, owner_task, status) VALUES(?, ?, 'held') "
            "ON CONFLICT(resource, owner_task) DO UPDATE SET status = 'held'",
            (resource, task_id),
        )


def _close_requests(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute(
        "UPDATE input_requests SET status = ? WHERE task_id = ? AND status = 'pending'",
        (status, task_id),
    )


# ---------------------------------------------------------------------- planning


def task_planned(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    _move(conn, event)
    conn.execute("DELETE FROM dependencies WHERE task_id = ?", (task_id,))
    for dep in event.data["dependencies"]:
        conn.execute(
            "INSERT INTO dependencies(task_id, dep_id, bound_revision, kind, reason) "
            "VALUES(?, ?, ?, ?, ?)",
            (task_id, dep["task_id"], dep["bound_revision"], dep["kind"], dep["reason"]),
        )
    _set(conn, task_id, resources=dumps(event.data["resources"]))
    _touch(conn, event)


def requirement_changed(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    data = event.data
    tid = _move(conn, event)
    _set(
        conn,
        task_id,
        revision=data["revision"],
        goal=data["goal"],
        acceptance=dumps(data["acceptance"]),
        spec_sha256=data["spec_sha256"],
        stale=0,
    )
    status = _status(conn, task_id)
    if tid == "T17" or (tid is None and status == "in_progress"):
        _new_cycle(conn, task_id)  # E07 inside in_progress; T17 from verifying
    if tid == "T23":
        _set(conn, task_id, submission=None, done_at=None, substate=None)
    if "brief" in data:
        brief = data["brief"]
        _attempt(
            conn,
            task_id,
            brief["attempt"],
            brief_revision=brief["revision"],
            # Set when the change wrote the first brief of an older prestarted session (T244).
            **({"brief_path": brief["path"]} if brief.get("path") else {}),
            **({"read_set": dumps(brief["read_set"])} if "read_set" in brief else {}),
        )
    if status == "paused":
        pause = loads(
            conn.execute("SELECT pause FROM tasks WHERE task_id = ?", (task_id,)).fetchone()[
                "pause"
            ],
            {},
        )
        if pause.get("resume_target") == "verifying":
            pause["resume_target"] = None
            pause["submission_invalidated"] = True
            _set(conn, task_id, pause=dumps(pause), submission=None)
    _close_requests(conn, task_id, "superseded")
    _touch(conn, event)


def input_requested(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    _move(conn, event)
    conn.execute(
        "INSERT INTO input_requests(request_id, task_id, kind, status, question, binding) "
        "VALUES(?, ?, ?, 'pending', ?, ?)",
        (data["request_id"], _tid(event), data["kind"], data["question"], dumps(data["binding"])),
    )
    if data["kind"] == "stale":
        _set(conn, _tid(event), stale=1)
    _touch(conn, event)


def input_escalated(conn: sqlite3.Connection, event: Event) -> None:
    input_requested(conn, event)
    if event.data.get("transition") == "T11":
        _set(conn, _tid(event), waiting_revision=event.data["binding"]["revision"])
    if event.data.get("kind") == "in_session":
        conn.execute(
            "INSERT INTO in_session_requests(request_id, task_id, session_ref, asked_at, "
            "handoff_after, reserved) VALUES(?, ?, ?, ?, ?, ?)",
            (
                event.data["request_id"],
                _tid(event),
                event.data["session_ref"],
                event.time,
                event.data["handoff_after"],
                1 if event.data.get("reserved") else 0,
            ),
        )
    for item in event.data.get("resolves", []):
        conn.execute("UPDATE judgments SET status = 'escalated' WHERE item_id = ?", (item,))


def input_resolved(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    task_id = _tid(event)
    conn.execute(
        "UPDATE input_requests SET status = 'resolved', resolution = ? WHERE request_id = ?",
        (
            dumps(
                {
                    "source": data["source"],
                    "answer": data.get("answer"),
                    **{k: data[k] for k in ("source_kind", "decision_id") if k in data},
                }
            ),
            data["request_id"],
        ),
    )
    _answered_in_session(conn, event)
    tid = _move(conn, event)
    if tid == "T12":
        if data["new_cycle"]:
            _new_cycle(conn, task_id)
        else:
            _set(conn, task_id, substate=data["resume_substate"])
    for item in data.get("resolves", []):
        conn.execute(
            "UPDATE judgments SET status = 'resolved', decision_ref = ? WHERE item_id = ?",
            (data["request_id"], item),
        )
    _touch(conn, event)


def input_late_response(conn: sqlite3.Connection, event: Event) -> None:
    _answered_in_session(conn, event)
    _touch(conn, event, business=False)


def _answered_in_session(conn: sqlite3.Connection, event: Event) -> None:
    source = event.data.get("source")
    channel = source.get("channel") if isinstance(source, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if channel == "in-session":
        conn.execute(
            "UPDATE in_session_requests SET answered_in_session = 1 WHERE request_id = ?",
            (event.data["request_id"],),
        )


def input_session_message(conn: sqlite3.Connection, event: Event) -> None:
    """The user wrote in the worker's session while a question was open (DN006 §8.2)."""
    data = event.data
    conn.execute(
        "INSERT INTO session_messages(message_id, task_id, request_id, session_ref, text, "
        "sequence) VALUES(?, ?, ?, ?, ?, ?)",
        (
            data["message_id"],
            _tid(event),
            data["request_id"],
            data["session_ref"],
            data["text"],
            event.sequence,
        ),
    )
    conn.execute(
        "UPDATE in_session_requests SET responded_at = COALESCE(responded_at, ?) "
        "WHERE request_id = ?",
        (event.time, data["request_id"]),
    )
    _touch(conn, event, business=False)


def input_handed_off(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "UPDATE in_session_requests SET handed_off_at = ? WHERE request_id = ?",
        (event.time, event.data["request_id"]),
    )
    _touch(conn, event, business=False)


def input_relayed(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "UPDATE in_session_requests SET relayed_to = ? WHERE request_id = ?",
        (event.data["channel"], event.data["request_id"]),
    )
    _touch(conn, event, business=False)


# ---------------------------------------------------------------------- dependencies


def dependency_satisfied(conn: sqlite3.Connection, event: Event) -> None:
    _move(conn, event)
    _touch(conn, event)


def dependency_invalidated(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    tid = _move(conn, event)
    if tid == "T16":
        _set(conn, task_id, substate=None, submission=None)
        _release_claims(conn, task_id, event.data.get("released_resources") or [])
        conn.execute("UPDATE integrations SET status = 'withdrawn' WHERE task_id = ?", (task_id,))
        _close_requests(conn, task_id, "cancelled")
    _touch(conn, event)


def dependency_rebound(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "UPDATE dependencies SET bound_revision = ? WHERE task_id = ? AND dep_id = ?",
        (event.data["revision"], _tid(event), event.data["dep_id"]),
    )
    _touch(conn, event)


def dependency_stale_cleared(conn: sqlite3.Connection, event: Event) -> None:
    _set(conn, _tid(event), stale=0, stale_ack=dumps(event.data["acknowledged"]))
    conn.execute(
        "UPDATE input_requests SET status = 'resolved', resolution = ? "
        "WHERE task_id = ? AND kind = 'stale' AND status = 'pending'",
        (dumps({"source": event.data["user_source"], "answer": "keep"}), _tid(event)),
    )
    _touch(conn, event)


def resource_released(conn: sqlite3.Connection, event: Event) -> None:
    _release_claims(conn, _tid(event), list(event.data["resources"]))
    _touch(conn, event)


# ---------------------------------------------------------------------- dispatch and attempts


def dispatch_started(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    data = event.data
    _move(conn, event)
    _attempt(
        conn,
        task_id,
        data["attempt"],
        workdir=data["workdir"],
        branch=data.get("branch"),
        brief_path=data["brief_path"],
        brief_revision=data["brief_revision"],
        read_set=dumps(data["read_set"]),
    )
    if data["attempt_mode"] == "new":
        conn.execute(
            "UPDATE tasks SET attempt_count = attempt_count + 1 WHERE task_id = ?", (task_id,)
        )
    _set(conn, task_id, current_attempt=data["attempt"])
    _claim(conn, task_id, data["resources"])
    _touch(conn, event)


def attempt_start_requested(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    data = event.data
    known = conn.execute(
        "SELECT 1 FROM attempts WHERE task_id = ? AND attempt = ?", (task_id, data["attempt"])
    ).fetchone()
    _attempt(
        conn,
        task_id,
        data["attempt"],
        phase="starting",
        generation=data["generation"],
        generation_valid=1,
        session_name=data["session_name"],
        purpose=data["purpose"],
        controller=data["controller"],
        adapter=data["adapter"],
        **({"workdir": data["workdir"]} if data.get("workdir") else {}),
        # A prestart writes the brief (T244); events from before that carry none.
        **(
            {
                "brief_path": data["brief_path"],
                "brief_revision": data["brief_revision"],
                "read_set": dumps(data["read_set"]),
            }
            if data.get("brief_path")
            else {}
        ),
    )
    if known is None:
        conn.execute(
            "UPDATE tasks SET attempt_count = attempt_count + 1, current_attempt = ? "
            "WHERE task_id = ?",
            (data["attempt"], task_id),
        )
    _touch(conn, event, business=False)


def attempt_prestarted(conn: sqlite3.Connection, event: Event) -> None:
    _attempt(
        conn,
        _tid(event),
        event.data["attempt"],
        phase="standby",
        session_ref=event.data["session_ref"],
    )
    _touch(conn, event, business=False)


def attempt_not_created(conn: sqlite3.Connection, event: Event) -> None:
    task_id, attempt_id = _tid(event), event.data["attempt"]
    row = conn.execute(
        "SELECT invalid_generations FROM attempts WHERE task_id = ? AND attempt = ?",
        (task_id, attempt_id),
    ).fetchone()
    invalid = loads(row["invalid_generations"], [])
    invalid.append(event.data["generation"])
    _attempt(
        conn,
        task_id,
        attempt_id,
        phase="planned",
        generation_valid=0,
        invalid_generations=dumps(invalid),
    )
    _touch(conn, event, business=False)


def dispatch_abandoned(conn: sqlite3.Connection, event: Event) -> None:
    _move(conn, event)
    _release_claims(conn, _tid(event))
    _touch(conn, event)


def attempt_bound(conn: sqlite3.Connection, event: Event) -> None:
    """T08 for a new session, a standby session or a continued session."""
    task_id = _tid(event)
    data = event.data
    tid = _move(conn, event)
    values: dict[str, Any] = {"phase": "live"}
    if "session_ref" in data:
        values["session_ref"] = data["session_ref"]
    if "receipt" in data:
        values["receipt"] = dumps(data["receipt"])
    if "read_set" in data:
        values["read_set"] = dumps(data["read_set"])
    _attempt(conn, task_id, data["attempt"], **values)
    if tid == "T08":
        _new_cycle(conn, task_id)
    _touch(conn, event)


def attempt_late_session(conn: sqlite3.Connection, event: Event) -> None:
    task_id, attempt_id = _tid(event), event.data["attempt"]
    row = conn.execute(
        "SELECT late_sessions FROM attempts WHERE task_id = ? AND attempt = ?",
        (task_id, attempt_id),
    ).fetchone()
    late = loads(row["late_sessions"] if row else None, [])
    late.append({"generation": event.data["generation"], "session_ref": event.data["session_ref"]})
    _attempt(conn, task_id, attempt_id, late_sessions=dumps(late))
    _touch(conn, event, business=False)


def attempt_phase(phase: str) -> Reducer:
    def reduce(conn: sqlite3.Connection, event: Event) -> None:
        _attempt(conn, _tid(event), event.data["attempt"], phase=phase)
        _touch(conn, event, business=False)

    return reduce


def attempt_lost(conn: sqlite3.Connection, event: Event) -> None:
    _move(conn, event)
    _attempt(conn, _tid(event), event.data["attempt"], phase="replaced")
    _set(conn, _tid(event), substate=None)
    _touch(conn, event)


# ---------------------------------------------------------------------- pause and resume


def task_paused(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    _move(conn, event)
    _set(conn, task_id, pause=dumps(event.data["pause"]))
    if event.data.get("attempt_stopped"):
        _attempt(conn, task_id, event.data["attempt_stopped"], phase="stopped")
    _touch(conn, event)


def task_resumed(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    tid = _move(conn, event)
    _set(conn, task_id, pause=None)
    if tid in ("T20", "T27"):
        _claim(conn, task_id, event.data.get("resources") or [])
    if tid == "T20":
        _attempt(conn, task_id, event.data["attempt"], phase="live")
        _new_cycle(conn, task_id)
    elif tid == "T21":
        _set(conn, task_id, substate=None, submission=None)
    _touch(conn, event)


# ---------------------------------------------------------------------- worker and review


def judgment_opened(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "INSERT INTO judgments(item_id, task_id, trigger, status, data) VALUES(?, ?, ?, 'open', ?)",
        (event.data["item_id"], _tid(event), event.type, dumps(dict(event.data))),
    )
    _touch(conn, event)


def worker_observation(conn: sqlite3.Connection, event: Event) -> None:
    _touch(conn, event, business=False)


def _cycle_row(conn: sqlite3.Connection, task_id: str, attempt_id: str, cycle: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO review_cycles(task_id, attempt, cycle) VALUES(?, ?, ?)",
        (task_id, attempt_id, cycle),
    )


def review_started(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    data = event.data
    _substate(conn, event, data["worker_transition"])
    _cycle_row(conn, task_id, data["attempt"], data["cycle"])
    conn.execute(
        "UPDATE review_cycles SET rounds = rounds + 1, last_round = ?, retries = 0, "
        "last_decision = NULL "
        "WHERE task_id = ? AND attempt = ? AND cycle = ?",
        (data["round"], task_id, data["attempt"], data["cycle"]),
    )
    conn.execute(
        "INSERT INTO review_rounds(task_id, attempt, round, cycle, head, reviewer) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (
            task_id,
            data["attempt"],
            data["round"],
            data["cycle"],
            data["head"],
            dumps(data["reviewer"]),
        ),
    )
    _touch(conn, event)


def review_failed(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "UPDATE review_cycles SET retries = retries + 1 WHERE task_id = ? AND attempt = ? AND "
        "cycle = ?",
        (_tid(event), data["attempt"], data["cycle"]),
    )
    _touch(conn, event, business=False)


def review_completed(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    data = event.data
    _substate(conn, event, "E03")
    conn.execute(
        "UPDATE review_rounds SET verdict = ?, reviewed_head = ? WHERE task_id = ? AND attempt = "
        "? AND round = ?",
        (data["verdict"], data["reviewed_head"], task_id, data["attempt"], data["round"]),
    )
    conn.execute(
        "UPDATE review_cycles SET last_reviewed_head = ? WHERE task_id = ? AND attempt = ? AND "
        "cycle = ?",
        (data["reviewed_head"], task_id, data["attempt"], data["cycle"]),
    )
    for finding in data["findings"]:
        conn.execute(
            "INSERT INTO findings(task_id, attempt, round, finding_id, cycle, severity, blocking) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                data["attempt"],
                data["round"],
                finding["id"],
                data["cycle"],
                finding["severity"],
                1 if finding["blocking"] else 0,
            ),
        )
    _touch(conn, event)


def review_triaged(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    for item in data["items"]:
        conn.execute(
            "UPDATE findings SET action = ?, reason = ?, verification = ?, "
            "disposition = COALESCE(disposition, ?) "
            "WHERE task_id = ? AND attempt = ? AND round = ? AND finding_id = ?",
            (
                item["action"],
                item.get("reason"),
                item.get("verification"),
                item["action"],
                _tid(event),
                data["attempt"],
                data["round"],
                item["finding"],
            ),
        )
    _touch(conn, event)


def review_decision(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    data = event.data
    if data["decision"] == "iterate":
        _substate(conn, event, "E04")
    conn.execute(
        "UPDATE review_rounds SET decision = ? WHERE task_id = ? AND attempt = ? AND round = ?",
        (data["decision"], task_id, data["attempt"], data["round"]),
    )
    conn.execute(
        "UPDATE review_cycles SET last_decision = ? WHERE task_id = ? AND attempt = ? AND cycle = "
        "?",
        (data["decision"], task_id, data["attempt"], data["cycle"]),
    )
    _touch(conn, event)


def review_budget_extended(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    column = "orchestrator_extension" if data["source_kind"] == "orchestrator" else "user_extension"
    _cycle_row(conn, _tid(event), data["attempt"], data["cycle"])
    conn.execute(
        f"UPDATE review_cycles SET {column} = {column} + ? "
        "WHERE task_id = ? AND attempt = ? AND cycle = ?",
        (data["rounds"], _tid(event), data["attempt"], data["cycle"]),
    )
    _resolve_judgments(conn, event)
    _touch(conn, event)


def _resolve_judgments(conn: sqlite3.Connection, event: Event) -> None:
    for item in event.data.get("resolves", []):
        conn.execute(
            "UPDATE judgments SET status = 'resolved', decision_ref = ? WHERE item_id = ?",
            (event.data.get("decision_id"), item),
        )


def finding_disposition(disposition: str) -> Reducer:
    def reduce(conn: sqlite3.Connection, event: Event) -> None:
        data = event.data
        for ref in data["findings"]:
            conn.execute(
                "UPDATE findings SET disposition = ?, decision_ref = ? "
                "WHERE task_id = ? AND attempt = ? AND round = ? AND finding_id = ?",
                (
                    disposition,
                    data["decision_id"],
                    _tid(event),
                    data["attempt"],
                    ref["round"],
                    ref["finding"],
                ),
            )
        _resolve_judgments(conn, event)
        _touch(conn, event)

    return reduce


def self_check_failed(conn: sqlite3.Connection, event: Event) -> None:
    _substate(conn, event, "E06")
    # A self-check reopens the work: an earlier finalize no longer stands.
    conn.execute(
        "UPDATE review_cycles SET last_decision = NULL WHERE task_id = ? AND cycle = "
        "(SELECT cycle FROM tasks WHERE task_id = ?)",
        (_tid(event), _tid(event)),
    )
    _touch(conn, event)


def orchestrator_decision(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "INSERT INTO decisions(decision_id, task_id, trigger, decision, rationale, basis, "
        "confidence, sequence) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["decision_id"],
            _tid(event),
            data["trigger"],
            data["decision"],
            data["rationale"],
            dumps(data["basis"]),
            data["confidence"],
            event.sequence,
        ),
    )
    _resolve_judgments(conn, event)
    _touch(conn, event)


def orchestrator_feedback(conn: sqlite3.Connection, event: Event) -> None:
    tid = _move(conn, event)
    if tid == "T17":
        _new_cycle(conn, _tid(event))
    _resolve_judgments(conn, event)
    _touch(conn, event)


def worker_submitted(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    _move(conn, event)
    _set(conn, task_id, submission=dumps(dict(event.data["submission"])), substate=None)
    _touch(conn, event)


# ---------------------------------------------------------------------- verification and acceptance


def integration(status: str) -> Reducer:
    def reduce(conn: sqlite3.Connection, event: Event) -> None:
        conn.execute(
            "INSERT INTO integrations(task_id, repo, status) VALUES(?, ?, ?) "
            "ON CONFLICT(task_id, repo) DO UPDATE SET status = excluded.status",
            (_tid(event), event.data["repo"], status),
        )
        _touch(conn, event)

    return reduce


def output_registered(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "INSERT INTO outputs(task_id, output_id, type, ref, role, delivery, verified_at, evidence) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, output_id) DO UPDATE SET "
        "type = excluded.type, ref = excluded.ref, role = excluded.role, "
        "delivery = excluded.delivery, verified_at = excluded.verified_at, evidence = "
        "excluded.evidence",
        (
            _tid(event),
            data["output_id"],
            data["type"],
            data["ref"],
            data.get("role"),
            data.get("delivery"),
            data.get("verified_at"),
            dumps(data.get("evidence", [])),
        ),
    )
    _touch(conn, event)


def task_accepted(conn: sqlite3.Connection, event: Event) -> None:
    _move(conn, event)
    _set(conn, _tid(event), done_at=event.time)
    _release_claims(conn, _tid(event))
    _touch(conn, event)


def task_user_accepted(conn: sqlite3.Connection, event: Event) -> None:
    _move(conn, event)
    _set(conn, _tid(event), accepted_at=event.time)
    _touch(conn, event)


def task_user_rejected(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    _move(conn, event)
    _set(
        conn, task_id, revision=event.data["revision"], submission=None, done_at=None, substate=None
    )
    _close_requests(conn, task_id, "superseded")
    _touch(conn, event)


def task_cancelled(conn: sqlite3.Connection, event: Event) -> None:
    task_id = _tid(event)
    _move(conn, event)
    _set(conn, task_id, substate=None)
    _release_claims(conn, task_id, event.data.get("released_resources") or [])
    conn.execute("UPDATE integrations SET status = 'withdrawn' WHERE task_id = ?", (task_id,))
    _close_requests(conn, task_id, "cancelled")
    _touch(conn, event)


def task_archived(conn: sqlite3.Connection, event: Event) -> None:
    _move(conn, event)
    _touch(conn, event)


# ---------------------------------------------------------------------- routine (DN006 §8.9)


def routine_start_requested(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "INSERT INTO routine_sessions(generation, session_name, workdir, status, requested_at) "
        "VALUES(?, ?, ?, 'starting', ?)",
        (int(data["generation"]), data["session_name"], data["workdir"], event.time),
    )


def routine_bound(conn: sqlite3.Connection, event: Event) -> None:
    generation = int(event.data["generation"])
    conn.execute(
        "UPDATE routine_sessions SET status = 'replaced' WHERE generation < ? "
        "AND status IN ('starting', 'bound')",
        (generation,),
    )
    conn.execute(
        "UPDATE routine_sessions SET status = 'bound', session_ref = ?, bound_at = ? "
        "WHERE generation = ?",
        (event.data["session_ref"], event.time, generation),
    )


def routine_armed(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "UPDATE routine_sessions SET armed_ref = ?, armed_cron = ?, armed_at = ? "
        "WHERE generation = ?",
        (event.data["job_ref"], event.data["cron"], event.time, int(event.data["generation"])),
    )


def routine_detached(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO routine_detached(generation, detached_at, note) VALUES(?, ?, ?)",
        (int(event.data["generation"]), event.time, event.data.get("note")),
    )


def routine_job_added(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO routine_jobs(job_id, script, input, added_seq) VALUES(?, ?, ?, ?)",
        (event.data["job_id"], event.data["script"], dumps(event.data["input"]), event.sequence),
    )


def routine_job_updated(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "UPDATE routine_jobs SET input = ? WHERE job_id = ?",
        (dumps(event.data["input"]), event.data["job_id"]),
    )


def routine_job_removed(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute("DELETE FROM routine_jobs WHERE job_id = ?", (event.data["job_id"],))


def channel_message_sent(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "INSERT OR REPLACE INTO channel_messages(message_id, channel, task_id, request_id, "
        "chat_id, sender_id, sent_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            data["message_id"],
            data["channel"],
            _tid(event),
            data["request_id"],
            data["chat_id"],
            data.get("sender_id"),
            event.time,
        ),
    )


def channel_reply_seen(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "INSERT OR IGNORE INTO channel_replies(chat_id, message_id, job_id, task_id, request_id, "
        "text, sent_at, sequence) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["chat_id"],
            data["message_id"],
            data["job_id"],
            _tid(event),
            data["request_id"],
            data["text"],
            data.get("sent_at"),
            event.sequence,
        ),
    )


REDUCERS: dict[str, Reducer] = {
    "task.planned": task_planned,
    "task.requirement_changed": requirement_changed,
    "input.requested": input_requested,
    "input.escalated": input_escalated,
    "input.resolved": input_resolved,
    "input.late_response": input_late_response,
    "input.session_message": input_session_message,
    "input.handed_off": input_handed_off,
    "input.relayed": input_relayed,
    "dependency.satisfied": dependency_satisfied,
    "dependency.invalidated": dependency_invalidated,
    "dependency.rebound": dependency_rebound,
    "dependency.stale_cleared": dependency_stale_cleared,
    "resource.released": resource_released,
    "task.dispatch_started": dispatch_started,
    "attempt.start_requested": attempt_start_requested,
    "attempt.prestarted": attempt_prestarted,
    "attempt.not_created": attempt_not_created,
    "attempt.dispatch_abandoned": dispatch_abandoned,
    "attempt.bound": attempt_bound,
    "attempt.brief_delivered": attempt_bound,
    "attempt.continued": attempt_bound,
    "attempt.late_session": attempt_late_session,
    "attempt.stopped": attempt_phase("stopped"),
    "attempt.replaced": attempt_phase("replaced"),
    "attempt.closed": attempt_phase("closed"),
    "attempt.lost": attempt_lost,
    "task.paused": task_paused,
    "task.resumed": task_resumed,
    "worker.progress": worker_observation,
    "worker.turn_ended": worker_observation,
    "worker.question": judgment_opened,
    "review.budget_exhausted": judgment_opened,
    "review.defer_requested": judgment_opened,
    "review.unavailable": judgment_opened,
    "review.started": review_started,
    "review.failed": review_failed,
    "review.completed": review_completed,
    "review.triaged": review_triaged,
    "review.decision": review_decision,
    "review.budget_extended": review_budget_extended,
    "review.finding_overruled": finding_disposition("overruled"),
    "review.defer_approved": finding_disposition("deferred_approved"),
    "worker.self_check_failed": self_check_failed,
    "worker.submitted": worker_submitted,
    "orchestrator.decision": orchestrator_decision,
    "orchestrator.feedback": orchestrator_feedback,
    "integration.started": integration("started"),
    "integration.conflict": integration("conflict"),
    "integration.completed": integration("completed"),
    "integration.withdrawn": integration("withdrawn"),
    "pr.registered": output_registered,
    "output.registered": output_registered,
    "task.accepted": task_accepted,
    "task.user_accepted": task_user_accepted,
    "task.user_rejected": task_user_rejected,
    "task.cancelled": task_cancelled,
    "task.archived": task_archived,
    "routine.start_requested": routine_start_requested,
    "routine.bound": routine_bound,
    "routine.armed": routine_armed,
    "routine.detached": routine_detached,
    "routine.job_added": routine_job_added,
    "routine.job_updated": routine_job_updated,
    "routine.job_removed": routine_job_removed,
    "channel.message_sent": channel_message_sent,
    "channel.reply_seen": channel_reply_seen,
}
