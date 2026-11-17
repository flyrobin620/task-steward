"""The routine board's deterministic pieces (DN006 §8.9): cron expressions and the Teams script."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from tsw.machine.routine import (
    CARD_PREFIX,
    TEAMS_CHECKED_KEPT,
    TEAMS_FETCH_ALERT_AFTER,
    body_arguments,
    checked_messages,
    cron_for,
    failing_fetches,
    fetch_failure,
    fetched_messages,
    first_step,
    full_text_path,
    id_time,
    list_path,
    note_checked,
    note_fetch,
    plain_text,
    prune_checked,
    question_html,
    read_arguments,
    select_replies,
    starting_task,
    teams_progress,
    tool_text,
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


@pytest.mark.use_case("UC-CHANNEL-001")
def test_question_html_follows_the_questions_layout() -> None:
    question = (
        "报告标题用哪一个？\n"
        "背景：评审说 <标题> 太长 & 不清楚。\n"
        "\n"
        "A. 方案 A：保持原样\n"
        "B) 方案 B：缩短\n"
        "\n"
        "- 影响 README\n"
        "* 影响设计文档\n"
        "\n"
        "```\n"
        "title = 'x < y'\n"
        "  indented\n"
        "```\n"
        "e.g. 不是选项；1.5 秒也不是。"
    )
    assert question_html(question) == (
        "<p>报告标题用哪一个？<br>背景：评审说 &lt;标题&gt; 太长 &amp; 不清楚。</p>"
        "<p>A. 方案 A：保持原样<br>B) 方案 B：缩短</p>"
        "<ul><li>影响 README</li><li>影响设计文档</li></ul>"
        "<codeblock><code>title = &#x27;x &lt; y&#x27;\n  indented</code></codeblock>"
        "<p>e.g. 不是选项；1.5 秒也不是。</p>"
    )
    # Chinese numbering, Windows line ends and an unclosed fence.
    assert question_html("1、红色\r\n2、蓝色\r\n```\nx") == (
        "<p>1、红色<br>2、蓝色</p><codeblock><code>x</code></codeblock>"
    )
    assert question_html("  ") == "<p></p>"
    # A rich-text card still reads as [TaskSteward] …, never as a reply.
    words = plain_text("<p><b>[TaskSteward] T243 Q-T243-1</b></p>" + question_html(question))
    assert words.startswith(f"{CARD_PREFIX} T243 Q-T243-1 报告标题用哪一个？")
    assert starting_task(words, ["T243", "Q-T243-1"]) is None


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
    assert fetch_failure(text) is None


def _listed(*times: str, count: int | None = None) -> dict[str, Any]:
    """A listed page as mcp__workiq__fetch returns it (T286): IDs and creation times only."""
    value = [{"id": _teams_id(t), "createdDateTime": t.replace("Z", ".000Z")} for t in times]
    data: dict[str, Any] = {"@odata.count": len(value) if count is None else count, "value": value}
    if len(value) >= 10:  # a next link only while there is more
        data["@odata.nextLink"] = "/me/chats/c/messages?$skiptoken=abc"
    return {"note": "Results limited", "results": [{"data": data, "statusCode": 200}]}


def _job(
    *,
    outbound: set[str] | None = None,
    seen: set[str] | None = None,
    requests: list[str] | None = None,
    sender_id: str | None = "u1",
    since: str = WINDOW,
) -> dict[str, Any]:
    return {
        "chat_id": "48:notes",
        "since": since,
        "sender_id": sender_id,
        "requests": [{"request_id": r} for r in (requests or ["Q-T001-1"])],
        "outbound": outbound or set(),
        "seen": seen or set(),
    }


def _bodies(*messages: dict[str, Any], gone: int = 0, status: int = 404) -> dict[str, Any]:
    results: list[dict[str, Any]] = [{"data": m, "statusCode": 200} for m in messages]
    results += [{"data": {"error": {"code": "NotFound"}}, "statusCode": status}] * gone
    return {"results": results}


@pytest.mark.use_case("UC-CHANNEL-001")
def test_the_read_paths_list_ids_first_and_bodies_by_id() -> None:
    # T286: a page is listed with IDs and times only, newest first; the next page is the one
    # before the oldest message, written by tsw (no long next link to copy); bodies by ID.
    assert read_arguments("48:notes") == {
        "entityUrls": [
            "/me/chats/48:notes/messages?$top=10&$orderby=createdDateTime+desc"
            "&$select=id,createdDateTime,lastModifiedDateTime"
        ]
    }
    assert first_step("48:notes") == {"stage": "list", "arguments": read_arguments("48:notes")}
    assert list_path("c", "2026-11-05T00:01:02.003Z") == (
        "/me/chats/c/messages?$top=10&$orderby=createdDateTime+desc"
        "&$select=id,createdDateTime,lastModifiedDateTime&$filter=createdDateTime+lt+2026-11-05T00:01:02.003Z"
    )
    assert body_arguments("c", ["1", "2"]) == {
        "entityUrls": [
            "/me/chats/c/messages/1?$select=id,from,body",
            "/me/chats/c/messages/2?$select=id,from,body",
        ]
    }
    assert full_text_path("c", "1") == "/me/chats/c/messages/1?$select=id,from,body"


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_listed_page_leaves_out_tsws_cards_before_reading_bodies() -> None:
    times = [f"2026-11-05T00:{m:02d}:00Z" for m in range(20, 10, -1)]  # a full page, newest first
    ids = [_teams_id(t) for t in times]
    job = _job(outbound={ids[0], ids[2]}, seen={ids[3]})
    out = teams_progress(job, first_step("48:notes"), _listed(*times))
    assert out["failure"] is None and out["page_done"] is False
    step = out["next"]
    wanted = [i for n, i in enumerate(ids) if n not in (0, 2, 3)]
    assert step["stage"] == "bodies" and step["ids"] == wanted
    assert step["arguments"] == body_arguments("48:notes", wanted)
    # tsw's own cards are never read: their IDs are not among the paths.
    assert not any(ids[0] in url or ids[2] in url for url in step["arguments"]["entityUrls"])
    # The page is full and still inside the window: the next listing comes after the bodies.
    assert step["after"] == {
        "stage": "list",
        "arguments": {"entityUrls": [list_path("48:notes", "2026-11-05T00:11:00.000Z")]},
    }
    # The bodies come back: the page is done and the next listing follows.
    bodies = [_msg(i, "随手记") for i in wanted]
    done = teams_progress(job, step, _bodies(*bodies))
    assert done == {
        "failure": None,
        "page_done": True,
        "next": step["after"],
        "checks": step["checks"],
    }
    assert list(done["checks"]) == wanted  # to remember once the script has run (T293)
    # Text after the JSON, as a hook may keep it, reads the same.
    assert teams_progress(job, step, json.dumps(_bodies(*bodies)) + "\n") == done


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_listed_page_ends_the_read_or_goes_on() -> None:
    job = _job()
    # Fewer than a page: the chat has nothing older; only cards: nothing to read, page done.
    few = ["2026-11-05T00:12:00Z", "2026-11-05T00:11:00Z"]
    out = teams_progress(_job(outbound={_teams_id(t) for t in few}), first_step("c"), _listed(*few))
    assert out == {"failure": None, "page_done": True, "next": None}
    # A full page reaching back before the window's start: the last one.
    full = [f"2026-11-0{5 if m else 3}T00:{m:02d}:00Z" for m in range(10)]
    out = teams_progress(job, first_step("c"), _listed(*full))
    assert out["next"]["stage"] == "bodies" and out["next"]["after"] is None
    # Older messages than the window are not candidates.
    assert _teams_id(full[0]) not in out["next"]["ids"]
    # A full page of cards inside the window: straight to the next listing.
    inside = [f"2026-11-05T00:{m:02d}:00Z" for m in range(30, 20, -1)]
    cards = _job(outbound={_teams_id(t) for t in inside})
    out = teams_progress(cards, first_step("c"), _listed(*inside))
    assert out["page_done"] is True and out["next"]["stage"] == "list"
    assert (
        "createdDateTime+lt+2026-11-05T00:21:00.000Z" in out["next"]["arguments"]["entityUrls"][0]
    )
    # An empty chat.
    assert teams_progress(job, first_step("c"), _listed()) == {
        "failure": None,
        "page_done": True,
        "next": None,
    }


@pytest.mark.use_case("UC-CHANNEL-001")
def test_what_was_fetched_is_checked() -> None:
    job = _job()
    times = ["2026-11-05T00:12:00Z", "2026-11-05T00:11:00Z"]
    # The page's own count disagrees with its messages.
    out = teams_progress(job, first_step("c"), _listed(*times, count=3))
    assert out["next"] is None and "条数" in out["failure"]
    # An ID that does not stand for its creation time.
    page = _listed(*times)
    page["results"][0]["data"]["value"][1]["createdDateTime"] = "2026-11-05T00:11:00.500Z"
    out = teams_progress(job, first_step("c"), page)
    assert out["page_done"] is False and "不一致" in out["failure"]
    # Whole seconds (the Teams tools) compare at whole seconds.
    seconds = {"value": [{"id": "1793869200050", "createdDateTime": "2026-11-05T09:00:00Z"}]}
    assert teams_progress(job, first_step("c"), seconds)["failure"] is None
    # A failed read.
    throttled = {"results": [{"data": {"error": {"code": "TooMany"}}, "statusCode": 429}]}
    out = teams_progress(job, first_step("c"), throttled)
    assert out == {"failure": "HTTP 429（TooMany）", "page_done": False, "next": None}
    # Bodies: exactly the candidates come back, but those deleted since (HTTP 404).
    step = {"stage": "bodies", "ids": ["a", "b", "c"], "arguments": {}, "after": None}
    two = _bodies(_msg("a", "x"), _msg("b", "y"), gone=1)
    assert teams_progress(job, step, two) == {
        "failure": None,
        "page_done": True,
        "next": None,
        "checks": {},  # a step kept before T293 has nothing to remember
    }
    missing = teams_progress(job, step, _bodies(_msg("a", "x"), _msg("b", "y")))
    assert missing["page_done"] is False and "请求的候选" in missing["failure"]
    extra = _bodies(_msg("a", "x"), _msg("b", "y"), _msg("z", "z"))
    assert "请求的候选" in teams_progress(job, step, extra)["failure"]
    twice = _bodies(_msg("a", "x"), _msg("a", "x"), _msg("b", "y"))
    assert "请求的候选" in teams_progress(job, step, twice)["failure"]
    failed = teams_progress(job, step, _bodies(_msg("a", "x"), _msg("b", "y"), gone=1, status=429))
    assert failed["failure"] == "HTTP 429（NotFound）" and failed["next"] is None
    assert teams_progress(job, step, None)["failure"] == "没有取数结果"


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_page_with_bodies_is_read_at_once() -> None:
    # The one-step read of routine sessions started before T286: no bodies step after it.
    reply = _msg(_teams_id("2026-11-05T00:12:00Z"), "T001 用 B", at="2026-11-05T00:12:00.000Z")
    out = teams_progress(_job(), first_step("c"), {"value": [reply]})
    assert out == {
        "failure": None,
        "page_done": True,
        "next": None,
        "checks": {reply["id"]: {"at": "2026-11-05T00:12:00Z", "modified": None}},
    }


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_long_message_is_cut_to_the_cap() -> None:
    requests = [_q("T001", 1, "card-1", "2026-11-04T23:00:00Z")]
    long_reply = "T001 " + "很长的答复" * 100
    messages = [_msg("2", f"<p>{long_reply}</p>"), _msg("3", "<p>T001 好</p>")]
    found = select_replies(
        messages, requests=requests, sender_id="u1", since=WINDOW, exclude=set(), cap=100
    )
    assert [(m["message_id"], m["text"]) for m in found] == [
        ("2", long_reply[:100]),
        ("3", "T001 好"),
    ]
    assert found[0]["truncated"] is True and found[0]["length"] == len(long_reply)
    assert "truncated" not in found[1] and "length" not in found[1]


@pytest.mark.use_case("UC-CHANNEL-001")
def test_tool_text_reads_what_a_hook_sees() -> None:
    text = '{"results": []}'
    assert tool_text([{"type": "text", "text": text}]) == text
    assert tool_text(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ) == ("ab")
    assert tool_text(text) == text
    assert tool_text({"results": []}) == {"results": []}
    assert tool_text([]) == []


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_workiq_fetch_result() -> None:
    # What mcp__workiq__fetch returns for one chat path (T253 live): the page is the ``data`` of
    # the path's result.
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
    # A failed fetch holds no messages.
    failed = {"results": [{"data": {"error": {"code": "x"}}, "statusCode": 404}]}
    assert fetched_messages(failed) == []
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
    # A step with more to read after it leaves the count as it is (T286): bodies or a later
    # page that fail every time still reach the alert.
    assert note_fetch(kept, "c", None, "listed", read_through=False) is None
    assert kept["fetch_failures"]["c"]["count"] == TEAMS_FETCH_ALERT_AFTER + 1
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


@pytest.mark.use_case("UC-CHANNEL-001")
def test_bodies_must_come_back_with_their_text() -> None:
    # Review F1 of T286: an ID alone or a listing given back is not an empty message.
    job = _job()
    step = {"stage": "bodies", "ids": ["a"], "arguments": {}, "after": None}
    only_id = teams_progress(job, step, {"results": [{"data": {"id": "a"}, "statusCode": 200}]})
    assert only_id["next"] is None and "没有正文" in only_id["failure"]
    no_content = {"id": "a", "body": {"contentType": "html"}}
    out = teams_progress(job, step, {"results": [{"data": no_content, "statusCode": 200}]})
    assert "没有正文" in out["failure"]
    listing = {"id": "a", "createdDateTime": "2026-11-05T00:12:00.000Z"}
    out = teams_progress(
        job, step, {"results": [{"data": {"value": [listing]}, "statusCode": 200}]}
    )
    assert "没有正文" in out["failure"]
    empty = {"id": "a", "body": {"contentType": "html", "content": ""}}
    out = teams_progress(job, step, {"results": [{"data": empty, "statusCode": 200}]})
    assert out == {"failure": None, "page_done": True, "next": None, "checks": {}}
    assert teams_progress(job, step, [{"id": "a", "content": "T001 好"}])["failure"] is None


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_listing_or_whole_page_that_misses_fields_fails() -> None:
    # PR #51: a listing without IDs or times, or a whole page whose candidate has no body, is
    # not read through (it would drop a reply and end the run of failures).
    job = _job()
    no_id = {"@odata.count": 1, "value": [{"createdDateTime": "2026-11-05T00:12:00.000Z"}]}
    assert teams_progress(job, first_step("c"), no_id)["failure"] == "列出的消息缺少 ID"
    no_time = {"@odata.count": 1, "value": [{"id": "x1"}]}
    assert "创建时间" in teams_progress(job, first_step("c"), no_time)["failure"]
    # A real Teams ID stands for a time, but the listing must still give its own (review F4).
    real = _teams_id("2026-11-05T00:12:00Z")
    for listed in ({"id": real}, {"id": real, "createdDateTime": None}):
        page = {"@odata.count": 1, "value": [listed]}
        assert "创建时间" in teams_progress(job, first_step("c"), page)["failure"]
    card = _msg("card-1", f"{CARD_PREFIX} T001 Q-T001-1")
    bare = {"id": "r1", "createdDateTime": "2026-11-05T00:12:00Z", "body": {"contentType": "html"}}
    out = teams_progress(_job(outbound={"card-1"}), first_step("c"), {"value": [card, bare]})
    assert out["next"] is None and "没有正文" in out["failure"]
    # The card has no body to read: only candidates need one.
    whole = [{**card, "body": {}}, _msg("r1", "T001 好")]
    out = teams_progress(_job(outbound={"card-1"}), first_step("c"), {"value": whole})
    assert out == {
        "failure": None,
        "page_done": True,
        "next": None,
        "checks": {"r1": {"at": "2026-11-05T00:05:00Z", "modified": None}},
    }


def _listed_changed(times: list[str], modified: dict[str, str]) -> dict[str, Any]:
    """A listed page whose messages carry their last change (T293)."""
    page = _listed(*times)
    for message in page["results"][0]["data"]["value"]:
        message["lastModifiedDateTime"] = modified.get(message["id"], message["createdDateTime"])
    return page


@pytest.mark.use_case("UC-CHANNEL-001")
def test_a_message_judged_before_is_not_read_again() -> None:
    # T293: a note in the user's self chat was read in every run while a question waited.
    times = ["2026-11-05T00:12:00Z", "2026-11-05T00:11:00Z", "2026-11-05T00:10:00Z"]
    ids = [_teams_id(t) for t in times]
    job = _job()
    page = _listed_changed(times, {})
    first = teams_progress(job, first_step("48:notes"), page)
    assert first["next"]["ids"] == ids
    checks = first["next"]["checks"]
    assert checks[ids[0]] == {"at": "2026-11-05T00:12:00Z", "modified": "2026-11-05T00:12:00.000Z"}
    done = teams_progress(job, first["next"], _bodies(*(_msg(i, "随手记") for i in ids)))
    kept: dict[str, Any] = {}
    note_checked(kept, job, done["checks"], passed_on={ids[2]})  # the oldest was a reply
    assert set(kept["teams_checked"]["48:notes"]["messages"]) == {ids[0], ids[1]}
    judged = checked_messages(kept, job)
    assert judged == {ids[0]: "2026-11-05T00:12:00.000Z", ids[1]: "2026-11-05T00:11:00.000Z"}
    # The next run: the notes are not candidates (the reply is left out as seen, by the ledger).
    again = teams_progress(_job(seen={ids[2]}), first_step("48:notes"), page, judged)
    assert again == {"failure": None, "page_done": True, "next": None}
    # A note edited since (or reacted to) is read again; one listed without its last change too.
    edited = _listed_changed(times, {ids[1]: "2026-11-05T01:00:00.000Z"})
    out = teams_progress(_job(seen={ids[2]}), first_step("48:notes"), edited, judged)
    assert out["next"]["ids"] == [ids[1]]
    plain = teams_progress(_job(seen={ids[2]}), first_step("48:notes"), _listed(*times), judged)
    assert plain["next"]["ids"] == [ids[0], ids[1]]
    # A failed step leaves nothing to remember.
    throttled = {"results": [{"data": {"error": {"code": "TooMany"}}, "statusCode": 429}]}
    assert "checks" not in teams_progress(job, first["next"], throttled)


@pytest.mark.use_case("UC-CHANNEL-001")
def test_judgements_hold_while_their_basis_does() -> None:
    kept: dict[str, Any] = {}
    two = _job(requests=["Q-T001-1", "Q-T002-1"])
    note = {"1": {"at": "2026-11-05T00:12:00Z", "modified": "m"}}
    note_checked(kept, two, note, passed_on=())
    assert kept["teams_checked"]["48:notes"]["basis"] == {
        "requests": ["Q-T001-1", "Q-T002-1"],
        "sender_id": "u1",
    }
    # Fewer questions: whatever answered none of them answers none of fewer.
    assert checked_messages(kept, _job(requests=["Q-T002-1"])) == {"1": "m"}
    # A question added since, or another user: read again.
    assert checked_messages(kept, _job(requests=["Q-T001-1", "Q-T003-1"])) == {}
    assert checked_messages(kept, _job(requests=["Q-T001-1"], sender_id="u2")) == {}
    # Recorded on fewer questions, then a question comes: the old judgements are dropped.
    note_checked(kept, _job(requests=["Q-T002-1"]), {"2": {"at": "2026-11-05T00:13:00Z"}}, ())
    assert kept["teams_checked"]["48:notes"]["basis"]["requests"] == ["Q-T002-1"]
    note_checked(kept, _job(requests=["Q-T002-1", "Q-T004-1"]), {}, ())
    assert kept["teams_checked"]["48:notes"]["messages"] == {}
    # A damaged record holds nothing.
    assert checked_messages({"teams_checked": {"48:notes": {"basis": "x"}}}, _job()) == {}
    assert checked_messages({"teams_checked": "x"}, _job()) == {}


@pytest.mark.use_case("UC-CHANNEL-001")
def test_judgements_before_the_window_or_of_chats_no_longer_read_are_dropped() -> None:
    kept: dict[str, Any] = {}
    checks = {
        "old": {"at": "2026-11-05T00:01:00Z", "modified": None},
        "new": {"at": "2026-11-05T00:20:00Z", "modified": None},
        "bad": {"at": "not a time", "modified": None},
    }
    note_checked(kept, _job(), checks, passed_on=())
    assert set(kept["teams_checked"]["48:notes"]["messages"]) == {"old", "new"}
    later = _job(since="2026-11-05T00:10:00Z")
    assert checked_messages(kept, later) == {"new": None}
    prune_checked(kept, [later])
    assert set(kept["teams_checked"]["48:notes"]["messages"]) == {"new"}
    # The chat has no waiting question any more: its row is gone, and so is its record.
    prune_checked(kept, [])
    assert kept["teams_checked"] == {}
    prune_checked({}, [])  # nothing kept: nothing to do


@pytest.mark.use_case("UC-CHANNEL-001")
def test_only_the_newest_judgements_are_kept() -> None:
    kept: dict[str, Any] = {}
    checks = {
        f"m{n:04d}": {"at": f"2026-11-05T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"}
        for n in range(TEAMS_CHECKED_KEPT + 5)
    }
    note_checked(kept, _job(), checks, passed_on=())
    messages = kept["teams_checked"]["48:notes"]["messages"]
    assert len(messages) == TEAMS_CHECKED_KEPT
    assert "m0004" not in messages and "m0005" in messages
