"""The routine board's deterministic pieces (DN006 §8.9): cron expressions and the Teams script."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tsw.machine.routine import (
    CARD_PREFIX,
    cron_for,
    fetched_messages,
    next_page,
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
def test_plain_text_and_prefix() -> None:
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


def _q(task_id: str, n: int, card: str, sent_at: str, status: str = "pending") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "request_id": f"Q-{task_id}-{n}",
        "message_id": card,
        "sent_at": sent_at,
        "status": status,
    }


WINDOW = "2026-11-04T00:00:00Z"


@pytest.mark.use_case("UC-CHANNEL-001")
def test_select_replies_keeps_only_the_users_answers() -> None:
    requests = [
        _q("T001", 1, "card-1", "2026-11-04T23:00:00Z"),
        _q("T002", 1, "card-2", "2026-11-04T23:00:00Z"),
    ]
    messages = [
        _msg("card-1", f"{CARD_PREFIX} T001 Q-T001-1<br>任务：…", at="2026-11-04T23:00:00Z"),
        _msg("2", "<p>T001 用方案 A</p>"),
        _msg("3", "随手记一条笔记"),  # the user's own notes in the chat
        _msg("4", "T002 好的", sender="someone-else"),
        _msg("5", "T001 迟一点", at="2026-11-03T00:00:00Z"),  # before the read window
        _msg("6", "T003 不在等待中的任务"),
        _msg("7", "T002 用 B", at="2026-11-05T00:01:00Z"),
        _msg("8", "T001 已转过的"),
        _msg("9", "T001 tsw 发的提醒"),  # tsw's own message, recorded when sent
    ]
    found = select_replies(
        {"value": messages},
        requests=requests,
        sender_id="u1",
        since=WINDOW,
        exclude={"card-1", "card-2", "8", "9"},
    )
    assert [(m["message_id"], m["request_id"], m["text"], m["pending"]) for m in found] == [
        ("7", "Q-T002-1", "T002 用 B", True),
        ("2", "Q-T001-1", "T001 用方案 A", True),
    ]
    # Without a known sender every message in the chat counts as the user's (a self chat).
    found = select_replies(
        json.dumps(messages),
        requests=requests[1:],
        sender_id=None,
        since=WINDOW,
        exclude=set(),
    )
    assert [m["message_id"] for m in found] == ["7", "4"]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_reply_goes_to_the_question_it_answers() -> None:
    # T001's first question was answered elsewhere; a second one is waiting now.
    requests = [
        _q("T001", 1, "card-1", "2026-11-05T00:00:30Z", status="resolved"),
        _q("T001", 2, "card-2", "2026-11-05T01:00:30Z"),
    ]
    messages = [
        _msg("card-1", f"{CARD_PREFIX} T001 Q-T001-1", at="2026-11-05T00:00:00Z"),
        _msg("old", "T001 用 A", at="2026-11-05T00:20:00Z"),  # written before the second card
        _msg("card-2", f"{CARD_PREFIX} T001 Q-T001-2", at="2026-11-05T01:00:00Z"),
        _msg("new", "T001 用 C", at="2026-11-05T01:10:00Z"),
        _msg("num", "Q-T001-1：还是 A", at="2026-11-05T01:20:00Z"),  # names its question
    ]
    found = select_replies(
        messages, requests=requests, sender_id=None, since=WINDOW, exclude={"card-1", "card-2"}
    )
    assert [(m["message_id"], m["request_id"], m["pending"]) for m in found] == [
        ("old", "Q-T001-1", False),
        ("new", "Q-T001-2", True),
        ("num", "Q-T001-1", False),
    ]
    # Before the first card nothing can answer: not passed on.
    early = [_msg("x", "T001 先说一句", at="2026-11-04T22:00:00Z")]
    assert (
        select_replies(early, requests=requests, sender_id=None, since=WINDOW, exclude=set()) == []
    )


@pytest.mark.use_case("UC-CHANNEL-001")
def test_fetched_messages_accepts_envelopes_and_pages() -> None:
    one = {"id": "1", "content": "T001 a"}
    two = {"id": "2", "content": "T001 b"}
    assert fetched_messages([one]) == [one]
    assert fetched_messages({"messages": [one]}) == [one]
    assert fetched_messages({"items": [one, "noise"]}) == [one]
    assert fetched_messages(one) == [one]
    assert fetched_messages("not json") == []
    assert fetched_messages(None) == []
    # Several pages of the chat, newest first, as the routine session passes them.
    pages = [{"messages": [two], "nextLink": "p2"}, json.dumps({"messages": [one]})]
    assert fetched_messages(pages) == [two, one]
    requests = [_q("T001", 1, "card", "2026-11-04T23:00:00Z")]
    found = select_replies(pages, requests=requests, sender_id="u1", since=WINDOW, exclude=set())
    assert [m["message_id"] for m in found] == ["1", "2"]  # no sender, no time: kept


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_tool_result_with_text_after_the_json() -> None:
    # The Teams tools append a correlation line after the JSON they return.
    one = {"id": "1", "content": "T001 a"}
    text = json.dumps({"messages": [one], "nextLink": "p2"}) + "CorrelationId: abc, TimeStamp: x"
    assert fetched_messages(text) == [one]
    assert next_page(text, chat_id="c", since=WINDOW) == {"chatId": "c", "nextLink": "p2"}


@pytest.mark.use_case("UC-CHANNEL-001")
def test_next_page_reads_the_whole_window_one_page_at_a_time() -> None:
    def page(*times: str, link: str | None = "p-next") -> dict[str, Any]:
        out: dict[str, Any] = {"messages": [_msg(str(i), "x", at=t) for i, t in enumerate(times)]}
        if link:
            out["nextLink"] = link
        return out

    # Every message on the page is inside the window and there is more: read on.
    inside = page("2026-11-05T00:09:00Z", "2026-11-04T00:00:00Z")
    assert next_page(inside, chat_id="c", since=WINDOW) == {"chatId": "c", "nextLink": "p-next"}
    # The page reaches back past the window's start: done.
    assert (
        next_page(page("2026-11-05T00:09:00Z", "2026-11-03T23:59:59Z"), chat_id="c", since=WINDOW)
        is None
    )
    # The last page of the chat: done.
    assert next_page(page("2026-11-05T00:09:00Z", link=None), chat_id="c", since=WINDOW) is None
    # A page without dated messages tells nothing about the window: read on.
    assert next_page(page(), chat_id="c", since=WINDOW) == {"chatId": "c", "nextLink": "p-next"}
    # A list of pages from an older routine session: the last page decides.
    older = [inside, page("2026-11-03T00:00:00Z")]
    assert next_page(older, chat_id="c", since=WINDOW) is None
    assert next_page([page("2026-11-03T00:00:00Z"), inside], chat_id="c", since=WINDOW) == {
        "chatId": "c",
        "nextLink": "p-next",
    }
    # Plain messages and nothing at all name no next page.
    assert next_page([_msg("1", "x")], chat_id="c", since=WINDOW) is None
    assert next_page(None, chat_id="c", since=WINDOW) is None
    assert next_page("not json", chat_id="c", since=WINDOW) is None
