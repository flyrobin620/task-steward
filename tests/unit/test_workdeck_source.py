"""Messages the user sends through workdeck are the user's own words (DN006 §8.11, CC001 §6.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import REPO_ROOT
from tsw.errors import TswError
from tsw.host import transcript
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


# --- T338: the turn's source in the host's transcript (CC001 §6.1) ---------------------------


def _line(prompt_id: str, origin: dict[str, Any] | None, content: Any = "words") -> bytes:
    entry: dict[str, Any] = {"type": "user", "promptId": prompt_id, "message": {"content": content}}
    if origin is not None:
        entry["origin"] = origin
    return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")


WORKDECK_ORIGIN = {"kind": "peer", "from": "workdeck", "verifiedLocal": True}
PEER = {"kind": "peer", "from": "local_8c4ffa9b", "name": "T002", "hostInjected": True}


def test_the_turns_origin_is_found_by_its_prompt_id(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(
        _line("p-1", {"kind": "human"})
        + b'{"type":"queue-operation","operation":"enqueue","content":"p-2"}\n'
        + _line("p-2", WORKDECK_ORIGIN, "用 A")
        + b"not json p-2\n"
        + _line("p-2", None, [{"type": "tool_result"}])  # the turn's tool results: no origin
        + b'{"type":"assistant","promptId":"p-2"}\n'
        + _line("p-3", PEER)
    )
    found = transcript.prompt_origin(str(path), "p-2")
    assert found == WORKDECK_ORIGIN and transcript.via(found) == "workdeck"
    assert transcript.via(transcript.prompt_origin(str(path), "p-1")) is None
    assert transcript.via(transcript.prompt_origin(str(path), "p-3")) is None
    for missing in ("p-9", "", None, 7):
        assert transcript.prompt_origin(str(path), missing) is None
    for bad_path in (None, "", str(tmp_path / "missing.jsonl"), str(tmp_path)):
        assert transcript.prompt_origin(bad_path, "p-2") is None


def test_the_search_reads_across_chunks_and_stops_at_its_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcript, "CHUNK", 7)  # every line is cut between chunks
    path = tmp_path / "t.jsonl"
    filler = b"".join(_line(f"f-{i}", {"kind": "human"}) for i in range(20))
    path.write_bytes(_line("p-1", WORKDECK_ORIGIN) + filler + _line("p-2", WORKDECK_ORIGIN))
    assert transcript.prompt_origin(str(path), "p-2") == WORKDECK_ORIGIN
    assert transcript.prompt_origin(str(path), "p-1") == WORKDECK_ORIGIN  # the first line
    # An entry further back than the limit is not looked for; neither is one cut by it.
    size = path.stat().st_size
    assert transcript.prompt_origin(str(path), "p-1", limit=size - 1) is None
    assert transcript.prompt_origin(str(path), "p-1", limit=size) == WORKDECK_ORIGIN
    # No trailing newline: the last line is still read.
    path.write_bytes(filler + _line("p-3", WORKDECK_ORIGIN).rstrip(b"\n"))
    assert transcript.prompt_origin(str(path), "p-3") == WORKDECK_ORIGIN


@pytest.mark.parametrize(
    "origin",
    [
        {"kind": "human"},
        PEER,
        {"kind": "peer", "from": "Workdeck"},
        {"kind": "human", "from": "workdeck"},
        {"from": "workdeck"},
        None,
    ],
)
def test_only_the_hosts_workdeck_origin_counts(origin: dict[str, Any] | None) -> None:
    assert transcript.via(origin) is None
