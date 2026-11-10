"""The routine board's deterministic pieces (DN006 §8.9): cron expressions and the Teams script."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from tsw.machine.routine import (
    CARD_PREFIX,
    TEAMS_FETCH_ALERT_AFTER,
    cron_for,
    failing_fetches,
    fetch_failure,
    fetched_messages,
    id_time,
    next_page,
    note_fetch,
    plain_text,
    read_arguments,
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
    assert next_page(text, since=WINDOW) == {"entityUrls": ["p2"]}


@pytest.mark.use_case("UC-CHANNEL-001")
def test_next_page_reads_the_whole_window_one_page_at_a_time() -> None:
    def page(*times: str, link: str | None = "p-next") -> dict[str, Any]:
        out: dict[str, Any] = {"messages": [_msg(str(i), "x", at=t) for i, t in enumerate(times)]}
        if link:
            out["nextLink"] = link
        return out

    # Every message on the page is inside the window and there is more: read on.
    inside = page("2026-11-05T00:09:00Z", "2026-11-04T00:00:00Z")
    assert next_page(inside, since=WINDOW) == {"entityUrls": ["p-next"]}
    # The page reaches back past the window's start: done.
    assert next_page(page("2026-11-05T00:09:00Z", "2026-11-03T23:59:59Z"), since=WINDOW) is None
    # The last page of the chat: done.
    assert next_page(page("2026-11-05T00:09:00Z", link=None), since=WINDOW) is None
    # A page without dated messages tells nothing about the window: read on.
    assert next_page(page(), since=WINDOW) == {"entityUrls": ["p-next"]}
    # A list of pages from an older routine session: the last page decides.
    older = [inside, page("2026-11-03T00:00:00Z")]
    assert next_page(older, since=WINDOW) is None
    assert next_page([page("2026-11-03T00:00:00Z"), inside], since=WINDOW) == {
        "entityUrls": ["p-next"]
    }
    # Plain messages and nothing at all name no next page.
    assert next_page([_msg("1", "x")], since=WINDOW) is None
    assert next_page(None, since=WINDOW) is None
    assert next_page("not json", since=WINDOW) is None


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_workiq_fetch_result() -> None:
    # What mcp__workiq__fetch returns for one chat path (T253 live): the page is the ``data`` of
    # the path's result, its next page an ``@odata.nextLink`` path to fetch as it is.
    first = read_arguments("48:notes")
    assert first == {
        "entityUrls": [
            "/me/chats/48:notes/messages?$top=10&$orderby=createdDateTime+desc"
            "&$select=id,createdDateTime,from,body"
        ]
    }
    card = _msg(_teams_id("2026-11-05T00:10:00Z"), f"{CARD_PREFIX} T001 Q-T001-1<br>问题")
    reply = {
        "id": _teams_id("2026-11-05T00:12:00Z"),
        "createdDateTime": "2026-11-05T00:12:00Z",
        "from": {"application": None, "user": {"id": "u1", "displayName": "U"}},
        "body": {"contentType": "html", "content": "<p>T001 用 B</p>"},
    }

    def result(link: str | None, *messages: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {"@odata.count": len(messages), "value": list(messages)}
        if link:
            data["@odata.nextLink"] = link
        return {"note": "Results limited", "results": [{"data": data, "statusCode": 200}]}

    link = "/me/chats/48:notes/messages?$top=10&$skiptoken=abc"
    fetched = result(link, reply, card)
    assert fetched_messages(fetched) == [reply, card]
    assert fetched_messages(json.dumps(fetched, ensure_ascii=False)) == [reply, card]
    assert next_page(fetched, since=WINDOW) == {"entityUrls": [link]}
    # A link given in full is cut back to the path WorkIQ takes.
    full = result("https://graph.microsoft.com/v1.0" + link, reply)
    assert next_page(full, since=WINDOW) == {"entityUrls": [link]}
    # The last page names no next one; a failed fetch holds no messages and no next page.
    assert next_page(result(None, reply), since=WINDOW) is None
    failed = {"results": [{"data": {"error": {"code": "x"}}, "statusCode": 404}]}
    assert fetched_messages(failed) == [] and next_page(failed, since=WINDOW) is None
    # Several pages as an older routine session collected them.
    assert fetched_messages([fetched, result(None, card)]) == [reply, card, card]
    requests = [_q("T001", 1, card["id"], "2026-11-05T00:10:00Z")]
    found = select_replies(fetched, requests=requests, sender_id="u1", since=WINDOW, exclude=set())
    assert [(m["message_id"], m["text"]) for m in found] == [(reply["id"], "T001 用 B")]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_fetch_failure_tells_a_failed_read_from_a_page() -> None:
    page = {"results": [{"data": {"value": [], "@odata.count": 0}, "statusCode": 200}]}
    assert fetch_failure(page) is None
    assert fetch_failure(json.dumps(page)) is None
    assert fetch_failure({"messages": []}) is None
    assert fetch_failure([page, {"value": []}]) is None
    assert fetch_failure(json.dumps({"messages": []}) + "CorrelationId: abc") is None
    # What failed tools gave: an error text, a WorkIQ status, a Graph error object.
    assert fetch_failure("Session not found") == "Session not found"
    long_text = "  Error:\n HTTP 429 " + "x" * 500
    assert fetch_failure(long_text) == ("Error: HTTP 429 " + "x" * 500)[:200]
    assert fetch_failure(None) == "没有取数结果"
    assert fetch_failure("") == "取数结果为空"
    assert fetch_failure(7) == "取数结果不是 JSON 对象或数组"
    throttled = {
        "results": [
            {"data": {"error": {"code": "TooManyRequests", "message": "slow"}}, "statusCode": 429}
        ]
    }
    assert fetch_failure(throttled) == "HTTP 429（TooManyRequests：slow）"
    assert fetch_failure({"results": [{"data": None, "statusCode": 503}]}) == "HTTP 503"
    assert fetch_failure({"error": {"message": "denied"}}) == "错误（denied）"
    assert fetch_failure([page, throttled]) == "HTTP 429（TooManyRequests：slow）"


@pytest.mark.use_case("UC-CHANNEL-001")
def test_note_fetch_counts_failures_in_a_row_per_chat() -> None:
    kept: dict[str, Any] = {"generation": 1}
    alerts = [note_fetch(kept, "c", "HTTP 429", f"t{i}") for i in range(TEAMS_FETCH_ALERT_AFTER)]
    assert alerts[:-1] == [None] * (TEAMS_FETCH_ALERT_AFTER - 1)
    assert alerts[-1] == {
        "count": TEAMS_FETCH_ALERT_AFTER,
        "first": "t0",
        "last": f"t{TEAMS_FETCH_ALERT_AFTER - 1}",
        "reason": "HTTP 429",
    }
    assert note_fetch(kept, "c", "HTTP 429", "later") is None  # told once per run of failures
    assert note_fetch(kept, "other", "x", "t") is None  # counted per chat
    assert [f["chat_id"] for f in failing_fetches(kept)] == ["c"]
    assert note_fetch(kept, "c", None, "ok") is None
    assert failing_fetches(kept) == [] and "c" not in kept["fetch_failures"]
    assert failing_fetches({}) == [] and failing_fetches({"fetch_failures": []}) == []


def _teams_id(at: str) -> str:
    """A Teams message ID: the time the message was created, in milliseconds."""
    return str(int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000))


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_quick_reply_on_a_newer_page_than_its_card() -> None:
    # Teams created the card at 09:00:00; tsw recorded it at 09:00:30; the user replied at
    # 09:00:10. Read one page at a time, the reply's page does not hold the card.
    card = _teams_id("2026-11-05T09:00:00Z")
    assert id_time(card) == datetime(2026, 11, 5, 9, tzinfo=timezone.utc)
    assert id_time("card-1") is None and id_time("0000000000001") is None
    reply = [_msg("r", "T001 用 C", at="2026-11-05T09:00:10Z")]
    waiting = _q("T001", 2, card, "2026-11-05T09:00:30Z")
    older = _q("T001", 1, "card-1", "2026-11-05T08:00:00Z", status="resolved")
    for requests in ([waiting], [older, waiting]):
        found = select_replies(
            reply, requests=requests, sender_id=None, since=WINDOW, exclude=set()
        )
        assert [(m["request_id"], m["pending"]) for m in found] == [("Q-T001-2", True)]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_reply_in_the_same_second_as_its_card() -> None:
    # The tool gives whole seconds; the IDs give milliseconds. Both sides use the IDs.
    card = "1793869200050"  # 2026-11-05T09:00:00.050Z
    reply = _msg("1793869200900", "T001 用 C", at="2026-11-05T09:00:00Z")  # .900, same second
    waiting = _q("T001", 2, card, "2026-11-05T09:00:30Z")
    older = _q("T001", 1, "card-1", "2026-11-05T08:00:00Z", status="resolved")
    card_msg = _msg(card, f"{CARD_PREFIX} T001 Q-T001-2", at="2026-11-05T09:00:00Z")
    for requests in ([waiting], [older, waiting]):
        for page in ([reply], [reply, card_msg]):  # the card on another page, or on this one
            found = select_replies(
                page, requests=requests, sender_id=None, since=WINDOW, exclude={card}
            )
            assert [(m["request_id"], m["pending"]) for m in found] == [("Q-T001-2", True)]
    # Before the card, within the same second: not an answer to it.
    early = _msg("1793869200010", "T001 先说一句", at="2026-11-05T09:00:00Z")
    found = select_replies([early], requests=[waiting], sender_id=None, since=WINDOW, exclude=set())
    assert found == []
