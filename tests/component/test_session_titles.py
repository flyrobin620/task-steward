"""Worker session titles and the archive after acceptance (DN002 §5.8, T272), through the real
tools, the projection, the routine job and the orchestrator's Stop hook."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tsw import direct, lease
from tsw.commands import Command
from tsw.errors import TswError
from tsw.events import EventDraft
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.guard import Guard
from tsw.host.identity import SESSION_ENV
from tsw.host.mcp import Server
from tsw.machine import routine as routine_mod
from tsw.machine.base import Caller

mark = pytest.mark
BASE = "T001 短标题"


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks run on the machine that holds the lease (the harness's)."""
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


def reason_of(call: Callable[[], Any]) -> str:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason"))


def sessions(h: Harness) -> list[dict[str, Any]]:
    with h.ledger.view() as conn:
        return inbox_mod.attention(conn, h.ledger.storage, h.config)["sessions"]


def observed(session: str, title: str, **extra: Any) -> dict[str, Any]:
    """What ``get_session`` returns, in the fields tsw reads."""
    return {"sessionId": session, "title": title, "isArchived": False, **extra}


def prestarted(h: Harness, task_id: str = "T001", title: str | None = BASE) -> str:
    """A standby session created with the title attempt_start handed out."""
    h.register(task_id)
    h.plan(task_id)
    extra = {"session_title": title} if title else {}
    out = h.run("attempt_start", task_id=task_id, purpose="prestart", user_turn=True, **extra)
    data = out["events"][0]["data"]
    session = f"w-{task_id}"
    h.run(
        "attempt_reconcile",
        task_id=task_id,
        attempt=data["attempt"],
        generation=data["generation"],
        outcome="created",
        session_ref=session,
    )
    return session


def deliver(h: Harness, task_id: str = "T001") -> Caller:
    h.run("task_dispatch", task_id=task_id, workdir=f"work/{task_id}/a1")
    h.run("attempt_deliver", task_id=task_id)
    return Caller(role="worker", session_id=f"w-{task_id}", machine=MACHINE)


def rename(h: Harness, session: str, current: str, task_id: str = "T001") -> str:
    """The orchestrator's steps: get_session → session_check → set_session_title → record."""
    check = h.run(
        "session_check", task_id=task_id, attempt="a1", observed=observed(session, current)
    )
    assert check["action"] == "rename" and check["session_id"] == session
    title = str(check["title"])
    record = h.run(
        "session_record",
        task_id=task_id,
        attempt="a1",
        action="title",
        title=title,
        ok=True,
        result=f'Renamed session {session} to "{title}" (was "{current}").',
    )
    event = record["events"][0]
    assert (event["type"], event["data"]["outcome"]) == ("attempt.session_updated", "renamed")
    return title


def delivered_and_done(h: Harness, task_id: str = "T001") -> str:
    session = prestarted(h, task_id)
    worker = deliver(h, task_id)
    rename(h, session, f"{BASE}（待命）", task_id)
    h.finalize(worker, task_id)
    h.submit(worker, task_id)
    h.accept(task_id)
    return session


def accept_from_terminal(h: Harness, task_id: str = "T001") -> None:
    direct.perform(
        h.ledger,
        h.config,
        "accept",
        task_id,
        channel="user-cli",
        message_ref="cli:2026-11-12T00:00:00Z:1",
        text=f"tsw task accept {task_id}",
    )


def stop_reason(h: Harness) -> str | None:
    """The orchestrator's Stop hook in a turn that was not the user's."""
    env = {SESSION_ENV: ORCH.session_id or ""}
    decision = Guard(h.home, env, {"cwd": str(h.home.root), "hook_event_name": "Stop"}).run("Stop")
    return None if decision is None else str(decision["reason"])


def reminded(h: Harness) -> bool:
    """Whether the Stop hook reminds of sessions to rename or archive (other reasons aside)."""
    return "attention 的 sessions" in (stop_reason(h) or "")


@mark.use_case("UC-SESSION-001")
def test_titles_follow_the_task_from_standby_to_acceptance(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    out = h.run(
        "attempt_start", task_id="T001", purpose="prestart", user_turn=True, session_title=BASE
    )
    # The session is created with the title tsw hands out, and tsw remembers it.
    assert out["session_title"] == f"{BASE}（待命）"
    data = out["events"][0]["data"]
    assert (data["title_base"], data["session_title"]) == (BASE, f"{BASE}（待命）")
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=data["generation"],
        outcome="created",
        session_ref="w-T001",
    )
    assert sessions(h) == []  # a standby session already has its title
    # Delivered: the standby mark goes, in any turn of the orchestrator.
    worker = deliver(h)
    assert sessions(h) == [
        {
            "task_id": "T001",
            "attempt": "a1",
            "session_id": "w-T001",
            "reason": "dispatch",
            "action": "title",
            "title": BASE,
            "state": "due",
        }
    ]
    assert reminded(h)
    assert rename(h, "w-T001", f"{BASE}（待命）") == BASE
    assert sessions(h) == [] and not reminded(h)
    h.finalize(worker, "T001")
    h.submit(worker, "T001")
    h.accept("T001")
    assert sessions(h) == []  # technically accepted: the user has not accepted yet
    # The user accepts in a terminal: the ledger alone tells what is due.
    accept_from_terminal(h)
    [item] = sessions(h)
    assert (item["action"], item["title"], item["reason"]) == (
        "title",
        f"[已验收] {BASE}",
        "accepted",
    )
    reason = stop_reason(h)
    assert reason is not None and "T001 a1（改标题）" in reason and "session_check" in reason
    assert rename(h, "w-T001", BASE) == f"[已验收] {BASE}"
    assert sessions(h) == [] and not reminded(h)
    # Done twice is harmless: nothing is due, and a stale record is refused.
    again = h.run("session_check", task_id="T001", attempt="a1", observed=observed("w-T001", "x"))
    assert again["action"] == "none" and again["events"] == []
    assert (
        reason_of(
            lambda: h.run(
                "session_record",
                task_id="T001",
                attempt="a1",
                action="title",
                title=f"[已验收] {BASE}",
                ok=True,
                result="Renamed",
            )
        )
        == "not_due"
    )
    # Neither changed the task: no business time, no state.
    assert h.status("T001") == "accepted"
    types = [e.type for e in h.ledger.storage.read(since=0) if e.task_id == "T001"]
    assert types.count("attempt.session_updated") == 2


@mark.use_case("UC-SESSION-001")
def test_a_title_the_user_changed_is_left_alone(home: Home) -> None:
    h = Harness(home)
    session = delivered_and_done(h)
    accept_from_terminal(h)
    check = h.run(
        "session_check", task_id="T001", attempt="a1", observed=observed(session, "我自己的名字")
    )
    assert (check["action"], check["outcome"], check["observed"]) == (
        "none",
        "user_title",
        "我自己的名字",
    )
    event = check["events"][0]
    assert event["data"]["outcome"] == "user_title" and event["data"]["observed"] == "我自己的名字"
    # Never again, whatever the task does next: no reminder, no rename.
    assert sessions(h) == [] and not reminded(h)
    assert (
        reason_of(
            lambda: h.run(
                "session_record",
                task_id="T001",
                attempt="a1",
                action="title",
                title=f"[已验收] {BASE}",
                ok=True,
                result="Renamed",
            )
        )
        == "not_due"
    )


@mark.use_case("UC-SESSION-001")
def test_already_missing_and_failed(home: Home) -> None:
    h = Harness(home)
    for task_id in ("T001", "T002", "T003"):
        session = prestarted(h, task_id, title=None)
        deliver(h, task_id)
        base = f"{task_id} 任务 {task_id}"  # the default base: id and task title
        assert sessions(h)[-1]["title"] == base
        if task_id == "T001":
            # Someone renamed it to the title it should have: recorded, not renamed again.
            out = h.run(
                "session_check", task_id=task_id, attempt="a1", observed=observed(session, base)
            )
            assert (out["action"], out["outcome"]) == ("none", "already")
        elif task_id == "T002":
            out = h.run(
                "session_check",
                task_id=task_id,
                attempt="a1",
                observed="Session not found: w-T002",
                missing=True,
            )
            assert (out["action"], out["outcome"]) == ("none", "missing")
            assert out["events"][0]["data"]["evidence"] == "Session not found: w-T002"
        else:
            out = h.run(
                "session_check",
                task_id=task_id,
                attempt="a1",
                observed=observed(session, f"{base}（待命）"),
            )
            h.run(
                "session_record",
                task_id=task_id,
                attempt="a1",
                action="title",
                title=out["title"],
                ok=False,
                result="Error: something went wrong",
            )
    # A failed step is listed, but nothing reminds of it; it can be tried again.
    assert [(i["task_id"], i["state"]) for i in sessions(h)] == [("T003", "failed")]
    assert not reminded(h)
    assert rename(h, "w-T003", "T003 任务 T003（待命）", "T003") == "T003 任务 T003"
    assert sessions(h) == []
    # Created at dispatch with the title tsw handed out: nothing to do until it is accepted.
    h.register("T004")
    h.plan("T004")
    h.dispatch("T004", session="w-T004")
    assert sessions(h) == []


@mark.use_case("UC-SESSION-001")
def test_observed_must_be_the_session(home: Home) -> None:
    h = Harness(home)
    session = prestarted(h)
    deliver(h)
    assert (
        reason_of(lambda: h.run("session_check", task_id="T001", attempt="a1", observed="not json"))
        == "invalid_input"
    )
    other = observed("w-other", f"{BASE}（待命）")
    assert (
        reason_of(lambda: h.run("session_check", task_id="T001", attempt="a1", observed=other))
        == "session_mismatch"
    )
    # An archived session counts as gone.
    gone = h.run(
        "session_check",
        task_id="T001",
        attempt="a1",
        observed=observed(session, f"{BASE}（待命）", isArchived=True),
    )
    assert gone["outcome"] == "missing" and sessions(h) == []


@mark.use_case("UC-SESSION-001")
def test_archive_after_acceptance_when_configured(home: Home) -> None:
    h = Harness(home, {"sessions": {"archive_on_accept": True}})
    session = delivered_and_done(h)
    assert sessions(h) == []  # only after the user accepts
    h.run("user_accept", task_id="T001", user_source=h.source("T001", 1, "accept"))
    assert [i["action"] for i in sessions(h)] == ["title"]  # the title first
    rename(h, session, BASE)
    [item] = sessions(h)
    assert (item["action"], item["state"]) == ("archive", "due")
    check = h.run(
        "session_check",
        task_id="T001",
        attempt="a1",
        observed=observed(session, f"[已验收] {BASE}"),
    )
    assert check == {**check, "action": "archive", "session_id": session}
    # The host refused (for instance the session is on screen): listed as failed, no reminder.
    h.run("session_record", task_id="T001", attempt="a1", action="archive", ok=False, result="open")
    assert [(i["action"], i["state"]) for i in sessions(h)] == [("archive", "failed")]
    assert not reminded(h)
    done = h.run(
        "session_record",
        task_id="T001",
        attempt="a1",
        action="archive",
        ok=True,
        result=f"Archived session {session}.",
    )
    event = done["events"][0]
    assert event["type"] == "attempt.closed" and event["data"]["disposition"] == "archived"
    assert h.query("SELECT phase FROM attempts WHERE task_id = 'T001'")[0]["phase"] == "closed"
    assert sessions(h) == []


@mark.use_case("UC-SESSION-001")
def test_a_user_title_is_archived_all_the_same(home: Home) -> None:
    h = Harness(home, {"sessions": {"archive_on_accept": True}})
    session = delivered_and_done(h)
    accept_from_terminal(h)
    h.run("session_check", task_id="T001", attempt="a1", observed=observed(session, "我的"))
    assert [i["action"] for i in sessions(h)] == ["archive"]
    # The session is gone by the time it is archived: closed with the reason.
    out = h.run(
        "session_check", task_id="T001", attempt="a1", observed="Session not found", missing=True
    )
    assert out["events"][0]["type"] == "attempt.closed"
    assert out["events"][0]["data"]["disposition"] == "missing"
    assert sessions(h) == []


@mark.use_case("UC-SESSION-001")
@pytest.mark.parametrize(("prefix", "wanted"), [(None, f"[已取消] {BASE}"), ("", None)])
def test_a_cancelled_task(home: Home, prefix: str | None, wanted: str | None) -> None:
    config = {} if prefix is None else {"sessions": {"title_on_cancel": prefix}}
    h = Harness(home, config)
    session = prestarted(h)
    deliver(h)
    rename(h, session, f"{BASE}（待命）")
    h.run(
        "task_cancel",
        task_id="T001",
        user_source=h.source("T001", 1, "cancel"),
        worker_stopped=True,
    )
    due = [i["title"] for i in sessions(h)]
    assert due == ([wanted] if wanted else [])


@mark.use_case("UC-SESSION-001")
def test_a_session_created_before_titles_is_not_touched(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    # attempt.start_requested as written before T272: no title fields.
    h.ledger.commit(
        Command(
            actor=ORCH.actor(),
            input={"op": "old_prestart"},
            build=lambda _ctx: [
                EventDraft(
                    type="attempt.start_requested",
                    task_id="T001",
                    revision=1,
                    attempt="a1",
                    data={
                        "attempt": "a1",
                        "generation": 1,
                        "session_name": "t001-a1-g1",
                        "purpose": "prestart",
                        "controller": "orch-1",
                        "adapter": "claude-app",
                        "workdir": "work/T001/a1",
                    },
                )
            ],
        )
    )
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=1,
        outcome="created",
        session_ref="w-T001",
    )
    deliver(h)
    assert sessions(h) == []
    assert (
        reason_of(
            lambda: h.run(
                "session_check", task_id="T001", attempt="a1", observed=observed("w-T001", "x")
            )
        )
        == "no_session_title"
    )


@mark.use_case("UC-SESSION-001")
def test_the_title_actually_used_is_the_one_compared(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    data = h.run("attempt_start", task_id="T001", purpose="prestart", user_turn=True)["events"][0][
        "data"
    ]
    assert data["session_title"] == "T001 任务 T001（待命）"
    h.run(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=data["generation"],
        outcome="created",
        session_ref="w-T001",
        session_title="T001 别的标题",
    )
    [item] = sessions(h)  # it should say it is on standby
    assert item["title"] == "T001 任务 T001（待命）"
    assert rename(h, "w-T001", "T001 别的标题") == "T001 任务 T001（待命）"


@mark.use_case("UC-SESSION-001")
@mark.use_case("UC-ROUTINE-001")
def test_session_scan_passes_new_items_on_once(home: Home) -> None:
    h = Harness(home)
    out = h.run("routine_start", user_turn=True)
    generation = out["events"][0]["data"]["generation"]
    h.run("routine_bind", generation=generation, session_id="routine-1")
    h.run("routine_detached", generation=generation)
    server = Server(h.home, {SESSION_ENV: "routine-1"}, home.root / "routine")
    jobs = [j["job_id"] for j in server.call("routine_board", {})["jobs"]]
    assert jobs == ["handoff-scan", "session-scan"]
    assert server.call("routine_run", {"job_id": "session-scan"})["forward"] == {"send": False}
    delivered_and_done(h)
    accept_from_terminal(h)
    forward = server.call("routine_run", {"job_id": "session-scan"})["forward"]
    assert forward["send"] is True and forward["session_id"] == "orch-1"
    message = forward["message"]
    assert "session-scan" in message and "w-T001" in message and f"[已验收] {BASE}" in message
    assert "任务已验收" in message and "session_check" in message
    # Passed on once: the next runs stay quiet while the orchestrator has not got to it.
    assert server.call("routine_run", {"job_id": "session-scan"})["forward"] == {"send": False}
    # A lost cache passes it on once more; doing it twice is harmless.
    (h.home.cache_dir / "routine.json").unlink()
    assert server.call("routine_run", {"job_id": "session-scan"})["forward"]["send"] is True
    rename(h, "w-T001", BASE)
    assert server.call("routine_run", {"job_id": "session-scan"})["forward"] == {"send": False}
    with h.ledger.view() as conn:
        assert routine_mod.session_scan(conn, h.config, {}) == []
