"""Preference candidates (DN007 §6.3): the pure rules, in process."""

from __future__ import annotations

from typing import Any

import pytest

from tsw.errors import TswError
from tsw.events import Event
from tsw.machine import candidates
from tsw.machine.schemas import SCHEMAS


def material(ref: str, text: str, sequence: int) -> dict[str, Any]:
    return {
        "ref": ref,
        "kind": "k",
        "channel": "c",
        "verified": True,
        "sequence": sequence,
        "time": None,
        "text": text,
    }


@pytest.mark.use_case("UC-CONTEXT-007")
def test_quotes_compare_without_spacing() -> None:
    assert candidates.compact("使用 diagram  skill\n") == "使用diagramskill"
    assert candidates.compact("以后，  一律存草稿") in candidates.compact("以后，一律存草稿。")
    assert candidates.normalized(" 画图  主题 ") == "画图 主题"


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_prompt_keeps_the_goal_and_the_newest_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(candidates, "MAX_MATERIAL_CHARS", 30)
    goal = material("goal@r1", "g" * 10, 1)
    found = [
        goal,
        material("M-1", "a" * 10, 2),
        material("M-2", "b" * 8, 3),
        material("M-3", "c" * 8, 4),
    ]
    kept, truncated = candidates._trimmed(found)  # pyright: ignore[reportPrivateUsage]
    assert [m["ref"] for m in kept] == ["goal@r1", "M-2", "M-3"] and truncated
    kept, truncated = candidates._trimmed(found[:2])  # pyright: ignore[reportPrivateUsage]
    assert [m["ref"] for m in kept] == ["goal@r1", "M-1"] and not truncated


@pytest.mark.use_case("UC-CONTEXT-007")
def test_a_candidate_defaults_to_global_and_is_checked_field_by_field() -> None:
    entry = candidates._candidate_input(  # pyright: ignore[reportPrivateUsage]
        {"quote": " 一律存草稿 ", "source_ref": "M-1", "topic": "邮件 ", "reason": "长期 意图"}, 0
    )
    assert entry == {
        "quote": "一律存草稿",
        "source_ref": "M-1",
        "scope": "global",
        "topic": "邮件",
        "reason": "长期 意图",
        "supersedes": [],
    }
    bad = [
        {"quote": "x", "source_ref": "M-1", "topic": "t", "reason": "r", "scope": "team:a"},
        {"quote": "", "source_ref": "M-1", "topic": "t", "reason": "r"},
        {"quote": "x" * 2001, "source_ref": "M-1", "topic": "t", "reason": "r"},
        {"quote": "x", "source_ref": "M-1", "topic": "t"},
        "not an object",
    ]
    for raw in bad:
        with pytest.raises(TswError) as exc:
            candidates._candidate_input(raw, 0)  # pyright: ignore[reportPrivateUsage]
        assert exc.value.details["reason"] == "invalid_input", raw
    skipped = candidates._skipped_input(  # pyright: ignore[reportPrivateUsage]
        {"source_ref": "M-2", "category": "same_as", "same_as": "P-3"}, 0
    )
    assert skipped == {"source_ref": "M-2", "category": "same_as", "reason": "", "same_as": "P-3"}
    with pytest.raises(TswError):
        candidates._skipped_input(  # pyright: ignore[reportPrivateUsage]
            {"source_ref": "M-2", "category": "preference"}, 0
        )


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_subagent_instructions_say_what_is_not_a_preference() -> None:
    text = candidates.prompt_text()
    # The rule and its counterexamples (DN007 §6): the user's own way of working only.
    for words in (
        "换一个仓库、换一个项目",
        "不确定时宁可不提",
        "项目的设计、技术、环境决定",
        "主会话可以自我更新",
        "对某个具体任务的要求",
        "默认 `global`",
    ):
        assert words in text, words
    for category in candidates.SKIP_CATEGORIES:
        assert f"`{category}`" in text, category
    # Read-only: the subagent records nothing itself.
    assert "不调用任何写工具" in text


@pytest.mark.use_case("UC-CONTEXT-007")
def test_the_tool_schemas_match_the_rules() -> None:
    record = SCHEMAS["preference_candidates_record"]
    assert record["required"] == ["task_id", "scanned_through", "candidates"]
    skipped = record["properties"]["skipped"]["items"]["properties"]["category"]["enum"]
    assert tuple(skipped) == candidates.SKIP_CATEGORIES
    confirm = SCHEMAS["preference_record"]
    assert confirm["required"] == ["scope", "user_source"] and "candidate" in confirm["properties"]
    assert SCHEMAS["preference_candidate_dismiss"]["required"] == [
        "candidate",
        "reason",
        "user_source",
    ]


def event(sequence: int, kind: str, task_id: str | None, **data: Any) -> Event:
    return Event(
        schema=1,
        sequence=sequence,
        time="2026-11-16T00:00:00Z",
        type=kind,
        actor={"role": "orchestrator"},
        prev="",
        checksum="",
        task_id=task_id,
        data=data,
    )


def said(text: str, ref: str = "m-1") -> dict[str, Any]:
    return {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": ref,
        "text": text,
        "target": ["T9", 1, "answer"],
    }


@pytest.mark.use_case("UC-CONTEXT-007")
def test_words_follow_a_renumbered_task_and_injected_messages_are_never_words() -> None:
    """PR #54 review: what an old number said before a renumbering is the renamed task's; a
    message a host injected is never the user's, even if recorded before the hook knew it."""
    events = [
        event(1, "input.resolved", "T9", source=said("以后报告先写结论")),
        event(2, "task.renumbered", "T19", **{"from": "T9"}),
        event(3, "task.registered", "T9", goal="新任务"),
        event(4, "input.resolved", "T9", source=said("这个任务用英文")),
        event(5, "input.resolved", "T19", source=said("<agent-message>假的</agent-message>")),
        event(6, "input.resolved", "T19", source=said("用 B", ref="M-T19-1")),
    ]
    renames = candidates.renames_in(events)
    assert renames == [(2, "T9", "T19")]
    assert [candidates.owner(e, renames) for e in events] == [
        "T19",
        "T19",
        "T9",
        "T9",
        "T19",
        "T19",
    ]
    messages: dict[str, Any] = {
        "M-T19-1": {"sequence": 5, "text": '<agent-message from="a">x</agent-message>'},
        "M-T19-2": {"sequence": 7, "text": "以后图都用 diagram skill"},
    }
    words, goal_seq = candidates._event_words(  # pyright: ignore[reportPrivateUsage]
        events, "T19", messages, renames
    )
    # The answer citing a hook record stands for that record, injected or not.
    assert [(w["ref"], w["text"]) for w in words] == [("E1", "以后报告先写结论")]
    assert goal_seq is None
    other, goal = candidates._event_words(  # pyright: ignore[reportPrivateUsage]
        events, "T9", {}, renames
    )
    assert [w["ref"] for w in other] == ["E4"] and goal == 3
    shown = candidates._message_words(messages)  # pyright: ignore[reportPrivateUsage]
    assert [m["ref"] for m in shown] == ["M-T19-2"]
