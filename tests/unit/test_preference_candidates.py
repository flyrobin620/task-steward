"""Preference candidates (DN007 §6.3): the pure rules, in process."""

from __future__ import annotations

from typing import Any

import pytest

from tsw.errors import TswError
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
