"""Handing the orchestrator over (DN001 §6.4, T254): the holder releases the lease, another
session takes it, the session that gave it up does not take it back by itself, and workers wake
whichever session holds the lease rather than the one that started them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.component.test_host import guard, worker_task
from tests.machine_harness import Harness
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.identity import SESSION_ENV
from tsw.host.mcp import Server
from tsw.ledger import Ledger
from tsw.machine.base import Caller

mark = pytest.mark
ACTOR = {"role": "orchestrator"}
SOURCE = {
    "channel": "main-session",
    "principal": "owner",
    "message_ref": "msg-1",
    "text": "原主会话已经停了，接管吧",
}


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)  # orch-1 holds the lease on m1


def server(h: Harness, session: str, cwd: Path) -> Server:
    s = Server(h.home, {SESSION_ENV: session}, cwd)
    s.machine = "m1"
    return s


def holder(h: Harness) -> str | None:
    with h.ledger.view() as conn:
        row = lease.lease_row(conn)
    return None if row is None else str(row["holder_session"])


def stood(h: Harness, session: str) -> dict[str, Any] | None:
    with h.ledger.view() as conn:
        return lease.stood_down(conn, session)


def context_of(out: dict[str, Any] | None) -> str:
    assert out is not None
    return str(out["hookSpecificOutput"]["additionalContext"])


@mark.use_case("UC-LEASE-001")
@mark.transition("G-LEASE")
def test_release_stands_the_holder_down_until_it_acquires_on_purpose(h: Harness) -> None:
    released = h.ledger.commit(lease.release("orch-1", machine="m1", actor=ACTOR)).events
    assert [e.type for e in released] == [
        "controller.lease_stood_down",
        "controller.lease_released",
    ]
    assert released[0].data == {"session_id": "orch-1", "machine": "m1", "via": "release"}
    assert holder(h) is None
    record = stood(h, "orch-1")
    assert record is not None and record["via"] == "release"
    with h.ledger.view() as conn:
        assert lease.orchestrator_session(conn) is None
    # Another session takes the lease at once: no expiry to wait for, no takeover.
    h.ledger.commit(lease.acquire("orch-2", machine="m1", lease_minutes=60, actor=ACTOR))
    assert holder(h) == "orch-2"
    with h.ledger.view() as conn:
        assert lease.orchestrator_session(conn) == "orch-2"
    with pytest.raises(TswError) as info:
        h.ledger.commit(lease.acquire("orch-1", machine="m1", lease_minutes=60, actor=ACTOR))
    assert info.value.details["reason"] == "lease_held"
    assert stood(h, "orch-1") is not None  # a refused acquire changes nothing
    h.clock.now = "2026-11-05T02:00:00Z"  # orch-2's lease has expired
    with h.ledger.view() as conn:
        assert lease.orchestrator_session(conn) == "orch-2"  # still the one to wake
    h.ledger.commit(lease.acquire("orch-1", machine="m1", lease_minutes=60, actor=ACTOR))
    assert holder(h) == "orch-1" and stood(h, "orch-1") is None
    # Rebuilding the projection from the ledger gives the same answer.
    h.ledger.rebuild()
    assert holder(h) == "orch-1" and stood(h, "orch-1") is None


@mark.use_case("UC-LEASE-001")
@mark.transition("G-LEASE")
def test_a_session_taken_over_stands_down_too(h: Harness) -> None:
    taken = h.ledger.commit(
        lease.takeover(
            "orch-2",
            machine="m1",
            from_session="orch-1",
            user_source=SOURCE,
            lease_minutes=60,
            from_machine=False,
            actor=ACTOR,
        )
    ).events
    assert [e.type for e in taken] == ["controller.lease_stood_down", "controller.lease_taken_over"]
    assert taken[0].data == {
        "session_id": "orch-1",
        "machine": "m1",
        "via": "takeover",
        "by": "orch-2",
    }
    record = stood(h, "orch-1")
    assert record is not None and record["via"] == "takeover"
    assert stood(h, "orch-2") is None
    # A holder taking over its own lease does not stand itself down.
    again = h.ledger.commit(
        lease.takeover(
            "orch-2",
            machine="m1",
            from_session="orch-2",
            user_source=SOURCE,
            lease_minutes=60,
            from_machine=False,
            actor=ACTOR,
        )
    ).events
    assert [e.type for e in again] == ["controller.lease_taken_over"]
    assert stood(h, "orch-2") is None


@mark.use_case("UC-HOST-001")
@mark.use_case("UC-HOST-002")
@mark.use_case("UC-LEASE-001")
def test_handing_over_between_two_orchestrators(h: Harness, tmp_path: Path) -> None:
    session, _ = worker_task(h, tmp_path)
    w = Caller("worker", session, "m1")
    h.run("question", w, task_id="T001", text="跨任务的问题")  # one report waits in the inbox
    old = server(h, "orch-1", h.home.root)
    assert old.call("lease_status", {})["stood_down"] is None
    out = old.call("lease_release", {})
    assert out["released"] is True and out["stood_down"] is True
    status = old.call("lease_status", {})
    assert status["lease"] is None and status["stood_down"]["via"] == "release"
    with pytest.raises(TswError) as info:
        old.call("inbox_ack", {"sequences": [1]})
    assert info.value.details["reason"] == "lease_missing"

    # The session that gave the lease up does not take it back when it starts again, and its
    # turns are no longer held up by reports it may not handle.
    text = context_of(guard(h, "orch-1", h.home.root).run("SessionStart"))
    assert "让出调度租约" in text and "lease_acquire" in text and "已持有调度租约" not in text
    assert holder(h) is None
    assert guard(h, "orch-1", h.home.root).run("Stop") is None

    # The new orchestrator takes it at session start (or with lease_acquire) and is reminded.
    new_text = context_of(guard(h, "orch-2", h.home.root).run("SessionStart"))
    assert "已持有调度租约" in new_text
    assert holder(h) == "orch-2"
    stop = guard(h, "orch-2", h.home.root).run("Stop")
    assert stop is not None and stop["decision"] == "block" and "inbox" in stop["reason"]

    # Even once the new lease has expired, the old session stays read-only at session start.
    h.clock.now = "2027-01-01T00:00:00Z"
    again = context_of(guard(h, "orch-1", h.home.root).run("SessionStart"))
    assert "orch-2" in again and holder(h) == "orch-2"
    # Until the user asks it to lead again (the server's clock is past orch-2's lease too).
    assert server(h, "orch-1", h.home.root).call("lease_acquire", {})["expires_at"]
    assert holder(h) == "orch-1" and stood(h, "orch-1") is None
    assert "已持有调度租约" in context_of(guard(h, "orch-1", h.home.root).run("SessionStart"))


@mark.use_case("UC-HOST-001")
@mark.use_case("UC-HOST-002")
def test_workers_wake_the_session_that_holds_the_lease(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)  # started under orch-1
    worker = server(h, session, workdir)
    assert worker.call("whoami", {})["orchestrator_session"] == "orch-1"
    asked = worker.call("question", {"text": "资源冲突"})
    wake = asked["wake"]
    assert wake["send"] is True and wake["session_id"] == "orch-1"
    seq = asked["events"][0]["sequence"]
    assert f"T001 worker.question（事件 {seq}）" in wake["message"]
    assert "wake" not in worker.call("progress_note", {"text": "进展"})  # not reportable

    server(h, "orch-1", h.home.root).call("lease_release", {})
    assert worker.call("whoami", {})["orchestrator_session"] is None
    idle = worker.call("question", {"text": "没有主会话时"})["wake"]
    assert idle["send"] is False and "账本" in idle["note"]
    report = guard(h, session, workdir, prompt="[report] 结论").run("UserPromptSubmit")
    assert "不发送" in context_of(report)

    server(h, "orch-2", h.home.root).call("lease_acquire", {})
    assert worker.call("whoami", {})["orchestrator_session"] == "orch-2"
    moved = worker.call("question", {"text": "交接之后"})["wake"]
    assert moved["send"] is True and moved["session_id"] == "orch-2"
    report = guard(h, session, workdir, prompt="[report] 结论").run("UserPromptSubmit")
    assert "orch-2" in context_of(report)
    start = context_of(guard(h, session, workdir).run("SessionStart"))
    assert "wake.session_id" in start and 'session_id="parent"' not in start
    # The routine session's forwards follow the same holder.
    with Ledger(h.home, clock=h.clock, machine="m1").view() as conn:
        assert lease.orchestrator_session(conn) == "orch-2"


@mark.use_case("UC-HOST-001")
def test_no_wake_when_the_configuration_turns_worker_messages_off(
    home: Home, tmp_path: Path
) -> None:
    """T254 review r1 F2: scheduling.wake.worker_message_parent=false sends nothing."""
    h = Harness(home, {"scheduling": {"wake": {"worker_message_parent": False}}})
    session, workdir = worker_task(h, tmp_path)
    wake = server(h, session, workdir).call("question", {"text": "q"})["wake"]
    assert wake["send"] is False and "worker_message_parent" in wake["note"]
    report = guard(h, session, workdir, prompt="[report] 结论").run("UserPromptSubmit")
    assert "不发送" in context_of(report)
