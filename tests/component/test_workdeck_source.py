"""Messages the user sends through workdeck are the user's own words (DN006 §8.11, CC001 §6.1):
the worker's hook records them, the main session cites them with ``via: workdeck``, and the host's
own notices between sessions are never the user's words."""

from __future__ import annotations

from typing import Any

import pytest

from tests.machine_harness import MACHINE, Harness
from tests.support import REPO_ROOT
from tsw import lease
from tsw.errors import TswError
from tsw.home import Home
from tsw.host.guard import WORKDECK_TURN, Guard
from tsw.machine.base import Caller
from tsw.roles import worker_prompt

WORKER_MD = REPO_ROOT / "plugin" / "roles" / "worker.md"

pytestmark = pytest.mark.use_case("UC-WORKDECK-001")

IDLE = '[Cross-session idle notice] "t001-a1-g1" which you asked to be notified about, is idle now'
HELD = "[Cross-session delivery notice] Your message to another session is held by that session"


@pytest.fixture(autouse=True)
def same_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lease, "this_machine", lambda: MACHINE)


def hook(h: Harness, session: str, event: str, prompt: str | None = None) -> dict[str, Any] | None:
    env = {"CLAUDE_CODE_HOST_SESSION_ID": session}
    payload: dict[str, Any] = {"cwd": str(h.home.root), "hook_event_name": event}
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
