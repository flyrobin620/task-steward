"""The routine board's deterministic pieces (DN006 §8.9): cron expressions and the Teams script."""

from __future__ import annotations

import json

import pytest

from tsw.machine.routine import (
    CARD_PREFIX,
    cron_for,
    fetched_messages,
    plain_text,
    select_replies,
    starting_task,
)


@pytest.mark.use_case("UC-ROUTINE-001")
def test_cron_for_every_allowed_interval() -> None:
    assert cron_for(10) == "*/10 * * * *"
    assert cron_for(1) == "*/1 * * * *"
    assert cron_for(30) == "*/30 * * * *"
    assert cron_for(60) == "0 * * * *"
    assert cron_for(120) == "0 */2 * * *"
    assert cron_for(1440) == "0 0 * * *"


@pytest.mark.use_case("UC-CHANNEL-001")
def test_plain_text_and_task_prefix() -> None:
    assert plain_text("<p>T243&nbsp;用 <b>A</b></p>") == "T243 用 A"
    assert starting_task("T243 用 A", ["T243"]) == "T243"
    assert starting_task("t243：用 A", ["T243"]) == "T243"
    assert starting_task("T243", ["T243"]) == "T243"
    # A longer ID is not a reply to the shorter one, and the ID must come first.
    assert starting_task("T2431 用 A", ["T243"]) is None
    assert starting_task("T2431 用 A", ["T243", "T2431"]) == "T2431"
    assert starting_task("关于 T243：用 A", ["T243"]) is None
    assert starting_task(f"{CARD_PREFIX} T243 Q-T243-1", ["T243"]) is None


def _msg(mid: str, body: str, *, sender: str = "u1", at: str = "2026-11-05T00:05:00Z") -> dict:
    return {
        "id": mid,
        "createdDateTime": at,
        "from": {"user": {"id": sender, "displayName": "me"}},
        "body": {"contentType": "html", "content": body},
    }


@pytest.mark.use_case("UC-CHANNEL-001")
def test_select_replies_keeps_only_the_users_answers() -> None:
    messages = [
        _msg("1", f"{CARD_PREFIX} T001 Q-T001-1<br>任务：…"),  # the card itself
        _msg("2", "<p>T001 用方案 A</p>"),
        _msg("3", "随手记一条笔记"),  # the user's own notes in the chat
        _msg("4", "T002 好的", sender="someone-else"),
        _msg("5", "T001 迟一点", at="2026-11-03T00:00:00Z"),  # before the lookback window
        _msg("6", "T003 不在等待中的任务"),
        _msg("7", "T002 用 B", at="2026-11-05T00:01:00Z"),
        _msg("8", "T001 已转过的"),
    ]
    found = select_replies(
        {"value": messages},
        tasks=["T001", "T002"],
        sender_id="u1",
        since="2026-11-04T00:00:00Z",
        seen={"8"},
    )
    assert [(m["message_id"], m["task_id"], m["text"]) for m in found] == [
        ("7", "T002", "T002 用 B"),
        ("2", "T001", "T001 用方案 A"),
    ]
    # Without a known sender every message in the chat counts as the user's (a self chat).
    found = select_replies(
        json.dumps(messages),
        tasks=["T002"],
        sender_id=None,
        since="2026-11-04T00:00:00Z",
        seen=set(),
    )
    assert [m["message_id"] for m in found] == ["7", "4"]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_fetched_messages_accepts_the_common_envelopes() -> None:
    one = {"id": "1", "content": "T001 a"}
    assert fetched_messages([one]) == [one]
    assert fetched_messages({"messages": [one]}) == [one]
    assert fetched_messages({"items": [one, "noise"]}) == [one]
    assert fetched_messages(one) == [one]
    assert fetched_messages("not json") == []
    assert fetched_messages(None) == []
    found = select_replies(
        [one], tasks=["T001"], sender_id="u1", since="2026-11-04T00:00:00Z", seen=set()
    )
    assert [m["message_id"] for m in found] == ["1"]  # no sender, no time: kept
