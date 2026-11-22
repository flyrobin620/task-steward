"""Messages the user sends through workdeck are the user's own words (DN006 §8.11, CC001 §6.1)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support import REPO_ROOT
from tsw.errors import TswError
from tsw.machine import guards
from tsw.machine.ask_user import AUTOMATED_PREFIXES, is_automated

pytestmark = pytest.mark.use_case("UC-WORKDECK-001")

SOURCE = {
    "channel": "main-session",
    "principal": "session-owner",
    "message_ref": "workdeck:orch-1:2026-11-21T15:00:00Z",
    "text": "T001 验收",
    "via": "workdeck",
    "target": ["T001", 1, "accept"],
}
REMINDER = "<system-reminder>\nLinked sessions now: x = local_1 (idle)\n</system-reminder>\n"


def _reason(source: dict[str, Any]) -> str:
    with pytest.raises(TswError) as exc:
        guards.g_user(source, task_id="T001", revision=1, kind="accept")
    return str(exc.value.details.get("reason"))


@pytest.mark.transition("G-USER")
def test_the_main_session_records_words_that_came_through_workdeck() -> None:
    recorded = guards.g_user(SOURCE, task_id="T001", revision=1, kind="accept")
    assert recorded["via"] == "workdeck" and recorded["channel"] == "main-session"
    # Without via the source is the main session's as before.
    plain = {k: v for k, v in SOURCE.items() if k != "via"}
    assert "via" not in guards.g_user(plain, task_id="T001", revision=1, kind="accept")


@pytest.mark.transition("G-USER")
@pytest.mark.parametrize(
    "change",
    [
        {"via": "teams"},
        {"via": ""},
        {"via": 1},
        {"channel": "in-session"},
        {"channel": "teams-chat"},
        {"target": ["T001", 2, "accept"]},
    ],
)
def test_via_is_only_workdeck_and_only_on_the_main_session(change: dict[str, Any]) -> None:
    assert _reason({**SOURCE, **change}) == "g_user"


def test_the_direct_channels_take_no_via() -> None:
    with pytest.raises(TswError) as exc:
        guards.g_user(
            {**SOURCE, "channel": "user-cli"},
            task_id="T001",
            revision=1,
            kind="accept",
            role="user",
        )
    assert exc.value.details.get("reason") == "g_user"


@pytest.mark.parametrize(
    "prompt",
    [
        '[Cross-session idle notice] "t001" which you asked to be notified about, is idle now',
        '[Cross-session idle notice] No idle signal arrived from "t001" within 4 hours',
        "[Cross-session delivery notice] Your message to another session was held by that session",
        REMINDER + "[Cross-session delivery notice] Do not resend now: one of your messages …",
    ],
)
def test_the_hosts_cross_session_notices_are_not_the_users_words(prompt: str) -> None:
    assert is_automated(prompt)


FIRST = "Background (from the session that started this one):\n任务标题：T001\n\nTask:\n"


@pytest.mark.parametrize(
    "prompt",
    [
        FIRST + "# TaskSteward worker\n\n你是 TaskSteward 任务的 worker 子会话。",
        "# TaskSteward worker\n\n你是 TaskSteward 任务的 worker 子会话。",
        REMINDER + FIRST,
    ],
)
def test_the_first_prompt_the_orchestrator_wrote_is_not_the_users_words(prompt: str) -> None:
    """T332 live: the first prompt of a worker the orchestrator started reached the hook after
    the binding and was recorded as the user's message."""
    assert is_automated(prompt)


def test_the_worker_instructions_open_with_the_marker() -> None:
    worker = (REPO_ROOT / "plugin" / "roles" / "worker.md").read_text(encoding="utf-8")
    assert is_automated(worker)


@pytest.mark.parametrize(
    "prompt",
    [
        "T308 验收，follow up 1 和 3 转任务",  # what the hook receives from workdeck (T327 E8)
        REMINDER + "用方案 B",
        "说明一下 [Cross-session idle notice] 是什么",  # a marker inside the words is still words
    ],
)
def test_plain_words_from_the_user_or_workdeck_are_recorded(prompt: str) -> None:
    assert not is_automated(prompt)


def test_no_marker_names_workdeck() -> None:
    assert not any("workdeck" in prefix.lower() for prefix in AUTOMATED_PREFIXES)
