"""Messages the user sends through workdeck are the user's own words (DN006 §8.11, CC001 §6.1):
the worker's hook records them, the main session cites them with ``via: workdeck``, and the host's
own notices between sessions are never the user's words."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import MACHINE, Harness
from tests.support import REPO_ROOT
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.guard import WORKDECK_TURN, Guard
from tsw.host.status import AppView
from tsw.host.task_view import render_task, timeline
from tsw.machine import candidates
from tsw.machine.base import Caller
from tsw.roles import worker_prompt

WORKER_MD = REPO_ROOT / "plugin" / "roles" / "worker.md"
APP = AppView(token="t", actions={})

pytestmark = pytest.mark.use_case("UC-WORKDECK-001")

IDLE = '[Cross-session idle notice] "t001-a1-g1" which you asked to be notified about, is idle now'
HELD = "[Cross-session delivery notice] Your message to another session is held by that session"


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


def hook(
    h: Harness, session: str, event: str, prompt: str | None = None, **fields: Any
) -> dict[str, Any] | None:
    env = {"CLAUDE_CODE_HOST_SESSION_ID": session}
    payload: dict[str, Any] = {"cwd": str(h.home.root), "hook_event_name": event, **fields}
    if prompt is not None:
        payload["prompt"] = prompt
    return Guard(h.home, env, payload).run(event)


def recorded(h: Harness) -> list[str]:
    return [r["text"] for r in h.query("SELECT text FROM session_messages ORDER BY sequence")]


def workdeck(task_id: str, kind: str, text: str) -> dict[str, Any]:
    return {
        "channel": "main-session",
        "principal": "session-owner",
        "message_ref": "workdeck:orch-1:2026-11-21T15:00:00Z",
        "text": text,
        "via": "workdeck",
        "target": [task_id, 1, kind],
    }


def reason_of(call: Any) -> str:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason"))


def test_the_worker_hook_records_workdeck_words_but_not_host_notices(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    worker: Caller = h.dispatch("T001")
    session = str(worker.session_id)
    assert hook(h, session, "UserPromptSubmit", IDLE) is None
    assert hook(h, session, "UserPromptSubmit", HELD) is None
    # The session's first prompt, written by the orchestrator, may reach the hook after the
    # binding (T332 live): with or without the app's background in front, it is not the user's
    # words, and the instructions it quotes about [report] do not ask for a report.
    prompt = worker_prompt(WORKER_MD.read_text(encoding="utf-8"), "T001", "a1", "dispatch")
    assert "[report]" in prompt
    background = "Background (from the session that started this one):\n任务标题：T001\n\nTask:\n"
    for first in (prompt, background + prompt):
        out = hook(h, session, "UserPromptSubmit", first)
        assert "[report]" not in str((out or {}).get("hookSpecificOutput", {}))
    assert recorded(h) == []
    # Another session quoting [report] does not ask for one either; the user does (UC-ASK-001).
    quoted = '<cross-session-message from="w">用户说 [report]</cross-session-message>'
    assert hook(h, session, "UserPromptSubmit", quoted) is None
    # What the hook receives from workdeck is the bare text (T327 E8): recorded as the user's.
    hint = hook(h, session, "UserPromptSubmit", "导出也要支持 CSV")
    assert hint is not None and "M-T001-1" in hint["hookSpecificOutput"]["additionalContext"]
    assert recorded(h) == ["导出也要支持 CSV"]
    changed = h.run(
        "requirement_change",
        worker,
        task_id="T001",
        message_ids=["M-T001-1"],
        goal="g，并支持 CSV 导出",
        acceptance=["a", "CSV"],
        summary="加 CSV 导出",
    )
    source = changed["events"][0]["data"]["user_source"]
    assert source["channel"] == "in-session" and source["text"] == "导出也要支持 CSV"


@pytest.mark.transition("T22")
@pytest.mark.transition("G-USER")
def test_the_main_session_acts_on_workdeck_words(home: Home) -> None:
    h = Harness(home)
    h.complete("T001")
    accepted = h.run(
        "user_accept", task_id="T001", user_source=workdeck("T001", "accept", "T001 验收")
    )
    source = accepted["events"][0]["data"]["user_source"]
    assert source["via"] == "workdeck" and source["text"] == "T001 验收"
    assert h.status("T001") == "accepted"
    # The same words may not be dressed as another channel's, nor via something else.
    h.complete("T002")
    wrong = {**workdeck("T002", "accept", "T002 验收"), "channel": "in-session"}
    assert reason_of(lambda: h.run("user_accept", task_id="T002", user_source=wrong)) == "g_user"
    other = {**workdeck("T002", "accept", "T002 验收"), "via": "teams"}
    assert reason_of(lambda: h.run("user_accept", task_id="T002", user_source=other)) == "g_user"
    assert h.status("T002") == "done"


def test_a_turn_begun_by_workdeck_is_told_it_cannot_create_sessions(home: Home) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    # A host notice does not begin the user's turn: no prestart reminder.
    assert hook(h, "orch-1", "UserPromptSubmit", IDLE) is None
    after_notice = hook(h, "orch-1", "Stop")
    assert after_notice is None or "T001" not in after_notice["reason"]
    # A workdeck message cannot be told from the user's typing: the reminder says what to do.
    assert hook(h, "orch-1", "UserPromptSubmit", "开始 T001") is None
    stop = hook(h, "orch-1", "Stop")
    assert stop is not None and stop["decision"] == "block"
    assert "T001" in stop["reason"] and WORKDECK_TURN in stop["reason"]


# --- T338: the turn's source, read from the transcript at Stop (CC001 §6.1) -----------------


def entry(prompt_id: str, origin: dict[str, Any] | None, text: str = "x") -> str:
    """A transcript line as Claude Code writes it for the message that starts a turn: a message
    from another session (workdeck's too) in the host's wording, typed words as text blocks after
    the app's reminder (T332 live, T334 transcripts)."""
    content: Any = [
        {"type": "text", "text": "<system-reminder>\nLinked sessions now\n</system-reminder>"},
        {"type": "text", "text": text},
    ]
    if origin is not None and origin.get("kind") == "peer":
        content = (
            f"Another Claude session sent a message:\n{text}\n\n"
            "This came from another Claude session — not typed by your user"
        )
    line: dict[str, Any] = {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": content},
    }
    if origin is not None:
        line["origin"] = origin
    return json.dumps(line, ensure_ascii=False) + "\n"


WORKDECK_ORIGIN = {"kind": "peer", "from": "workdeck", "verifiedLocal": True}
HUMAN = {"kind": "human"}


def turn(
    h: Harness,
    session: str,
    transcript: Path,
    prompt_id: str,
    prompt: str,
    *,
    origin: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """One turn as the host runs it: UserPromptSubmit before the message is in the transcript,
    the host writes it, then Stop (T338 probes)."""
    fields = {"prompt_id": prompt_id, "transcript_path": str(transcript)}
    started = hook(h, session, "UserPromptSubmit", prompt, **fields)
    if origin is not None:
        with transcript.open("a", encoding="utf-8") as out:
            out.write(entry(prompt_id, origin, prompt))
            # The turn's tool results carry the same promptId but no origin.
            out.write(
                json.dumps(
                    {
                        "type": "user",
                        "promptId": prompt_id,
                        "message": {"content": [{"type": "tool_result"}]},
                    }
                )
                + "\n"
            )
    return started, hook(h, session, "Stop", None, **fields)


def injected(transcript: Path, text: str, origin: dict[str, Any]) -> None:
    """A message the host injects while the turn runs: a queued_command attachment with its
    origin and text, and no promptId (T334 probe C1)."""
    attachment = {"type": "queued_command", "prompt": text, "origin": origin, "isMeta": True}
    with transcript.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"type": "attachment", "attachment": attachment}) + "\n")


def vias(h: Harness) -> list[dict[str, Any]]:
    return h.query("SELECT message_id, via FROM session_message_via ORDER BY sequence")


def test_the_worker_notes_a_message_that_came_through_workdeck(home: Home, tmp_path: Path) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    worker: Caller = h.dispatch("T001")
    session = str(worker.session_id)
    transcript = tmp_path / "worker.jsonl"
    transcript.write_text(entry("p-0", HUMAN, "first"), encoding="utf-8")
    asked = h.run("ask_user", worker, task_id="T001", question="用 A 还是 B？")
    request = asked["events"][0]["data"]["request_id"]
    turn(h, session, transcript, "p-1", "用 A", origin=WORKDECK_ORIGIN)
    recorded_event = [
        e for e in h.ledger.storage.read(since=0) if e.type == "input.session_message"
    ]
    assert recorded_event[-1].data["prompt_id"] == "p-1"
    assert vias(h) == [{"message_id": "M-T001-1", "via": "workdeck"}]
    noted = [e for e in h.ledger.storage.read(since=0) if e.type == "input.session_message_via"]
    assert [dict(e.data) for e in noted] == [
        {"message_id": "M-T001-1", "via": "workdeck", "prompt_id": "p-1"}
    ]
    # The mark was used up: a second Stop in the same turn notes nothing more.
    again = {"prompt_id": "p-1", "transcript_path": str(transcript), "stop_hook_active": True}
    hook(h, session, "Stop", None, **again)
    assert (
        len([e for e in h.ledger.storage.read(since=0) if e.type == "input.session_message_via"])
        == 1
    )
    # Still the user's words: the answer cites them, and the task page says how they came.
    h.run(
        "user_answer", worker, task_id="T001", request_id=request, message_id="M-T001-1", answer="A"
    )
    events = list(h.ledger.storage.read(since=0))
    with h.ledger.view() as conn:
        page = render_task(conn, "T001", APP, timeline(events).get("T001", []))
    assert page is not None and "in-session（经 workdeck） · 原话：用 A" in page
    with h.ledger.view() as conn:
        words = {w["ref"]: w for w in candidates.materials(conn, events, "T001")}
    assert words["M-T001-1"]["via"] == "workdeck"
    # What the preference scan gets (preference_sources): the note survives, in the prompt too.
    with h.ledger.view() as conn:
        shown = candidates.sources(conn, events, "T001")
    by_ref = {m["ref"]: m for m in shown["materials"]}
    assert by_ref["M-T001-1"]["via"] == "workdeck"
    assert '"via": "workdeck"' in shown["prompt"]
    # Typed in the app, from another session, or nothing to read: no note.
    turn(h, session, transcript, "p-2", "再加 CSV", origin=HUMAN)
    turn(h, session, transcript, "p-3", "还有 JSON", origin=None)
    hook(h, session, "UserPromptSubmit", "以及 XML", prompt_id="p-4")
    hook(h, session, "Stop", None, prompt_id="p-4", transcript_path=str(tmp_path / "missing.jsonl"))
    assert recorded(h) == ["用 A", "再加 CSV", "还有 JSON", "以及 XML"]
    assert vias(h) == [{"message_id": "M-T001-1", "via": "workdeck"}]
    # A turn the user began in the app, and a workdeck message injected while it ran (T334):
    # only the injected one came through workdeck.
    fields = {"prompt_id": "p-5", "transcript_path": str(transcript)}
    hook(h, session, "UserPromptSubmit", "先做 A", **fields)
    with transcript.open("a", encoding="utf-8") as out:
        out.write(entry("p-5", HUMAN, "先做 A"))
    hook(h, session, "UserPromptSubmit", "也要 B", **fields)
    injected(transcript, "也要 B", WORKDECK_ORIGIN)
    hook(h, session, "Stop", None, **fields)
    assert recorded(h)[-2:] == ["先做 A", "也要 B"]
    assert vias(h) == [
        {"message_id": "M-T001-1", "via": "workdeck"},
        {"message_id": "M-T001-6", "via": "workdeck"},
    ]
    # The same words typed in the app and then sent through workdeck: only the second is noted.
    fields = {"prompt_id": "p-6", "transcript_path": str(transcript)}
    hook(h, session, "UserPromptSubmit", "继续", **fields)
    with transcript.open("a", encoding="utf-8") as out:
        out.write(entry("p-6", HUMAN, "继续"))
    hook(h, session, "UserPromptSubmit", "继续", **fields)
    injected(transcript, "继续", WORKDECK_ORIGIN)
    hook(h, session, "Stop", None, **fields)
    assert recorded(h)[-2:] == ["继续", "继续"]
    assert [v["message_id"] for v in vias(h)] == ["M-T001-1", "M-T001-6", "M-T001-8"]
    # A Stop that is blocked (review in progress, another hook) continues the same turn: a message
    # injected after it still pairs with this turn's entries (review r4 F4), and the earlier ones
    # are not noted twice.
    fields = {"prompt_id": "p-7", "transcript_path": str(transcript)}
    hook(h, session, "UserPromptSubmit", "开始评审", **fields)
    with transcript.open("a", encoding="utf-8") as out:
        out.write(entry("p-7", HUMAN, "开始评审"))
    hook(h, session, "Stop", None, **fields)
    hook(h, session, "UserPromptSubmit", "也检查 B", **fields)
    injected(transcript, "也检查 B", WORKDECK_ORIGIN)
    hook(h, session, "Stop", None, stop_hook_active=True, **fields)
    hook(h, session, "Stop", None, stop_hook_active=True, **fields)
    assert recorded(h)[-2:] == ["开始评审", "也检查 B"]
    assert [v["message_id"] for v in vias(h)] == ["M-T001-1", "M-T001-6", "M-T001-8", "M-T001-10"]
    # The next turn drops what the last one read: its own words are paired afresh.
    turn(h, session, transcript, "p-8", "也检查 B", origin=WORKDECK_ORIGIN)
    assert [v["message_id"] for v in vias(h)][-1] == "M-T001-11"
    # A turn that never reached Stop (the session stopped) leaves its prompts behind; the next
    # turn, begun by the same words through workdeck, notes only its own (review r5 F5).
    hook(h, session, "UserPromptSubmit", "继续", prompt_id="p-9", transcript_path=str(transcript))
    with transcript.open("a", encoding="utf-8") as out:
        out.write(entry("p-9", HUMAN, "继续"))
    turn(h, session, transcript, "p-10", "继续", origin=WORKDECK_ORIGIN)
    assert recorded(h)[-2:] == ["继续", "继续"]
    assert [v["message_id"] for v in vias(h)][-2:] == ["M-T001-11", "M-T001-13"]
    with h.ledger.view() as conn:
        later = {w["ref"]: w for w in candidates.materials(conn, [], "T001")}
    assert "via" not in later["M-T001-2"]
    with h.ledger.view() as conn:
        shown = candidates.sources(conn, list(h.ledger.storage.read(since=0)), "T001")
    assert "via" not in {m["ref"]: m for m in shown["materials"]}["M-T001-2"]


def test_a_turn_the_transcript_shows_began_by_workdeck_is_not_reminded(
    home: Home, tmp_path: Path
) -> None:
    h = Harness(home, {"routine": {"enabled": False}})
    h.register("T001")
    h.plan("T001")
    transcript = tmp_path / "orch.jsonl"
    transcript.write_text("", encoding="utf-8")
    # The host lets no session be created in this turn: nothing to remind of.
    _, stop = turn(h, "orch-1", transcript, "p-1", "开始 T001", origin=WORKDECK_ORIGIN)
    assert stop is None
    # Typed in the app: reminded, as before.
    _, stop = turn(h, "orch-1", transcript, "p-2", "开始 T001", origin=HUMAN)
    assert stop is not None and "T001" in stop["reason"]
    # The source cannot be read: reminded, and told what a workdeck turn cannot do.
    _, stop = turn(h, "orch-1", transcript, "p-3", "开始 T001", origin=None)
    assert stop is not None and "T001" in stop["reason"] and WORKDECK_TURN in stop["reason"]
    # A workdeck message injected into the user's own turn does not make it a workdeck turn.
    fields = {"prompt_id": "p-4", "transcript_path": str(transcript)}
    hook(h, "orch-1", "UserPromptSubmit", "开始 T001", **fields)
    with transcript.open("a", encoding="utf-8") as out:
        out.write(entry("p-4", HUMAN, "开始 T001"))
    hook(h, "orch-1", "UserPromptSubmit", "顺便看看 T002", **fields)
    injected(transcript, "顺便看看 T002", WORKDECK_ORIGIN)
    stop = hook(h, "orch-1", "Stop", None, **fields)
    assert stop is not None and "T001" in stop["reason"]
