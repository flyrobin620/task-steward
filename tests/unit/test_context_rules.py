"""Pure rules of the context index (DN007 §5, §6, §12): query terms, generated keywords, HTML
text, user decisions found in events, scoring and scopes."""

from __future__ import annotations

from typing import Any

import pytest

from tsw.context import service
from tsw.context.index import decision_docs, outputs_digest, timeline_row
from tsw.context.text import generated_keywords, html_text, is_short, pieces, terms
from tsw.errors import TswError
from tsw.events import Event
from tsw.machine.context_tools import narrower_or_same, scope_of


def event(type_: str, data: dict[str, Any], task_id: str | None = "T001", seq: int = 7) -> Event:
    return Event(
        schema=1,
        sequence=seq,
        time="2026-11-12T00:00:00Z",
        type=type_,
        actor={"role": "orchestrator"},
        prev="0" * 64,
        checksum="1" * 64,
        task_id=task_id,
        revision=2 if task_id else None,
        data=data,
    )


def source(text: str, target: list[Any]) -> dict[str, Any]:
    return {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "m1",
        "text": text,
        "target": target,
    }


@pytest.mark.use_case("UC-CONTEXT-002")
def test_terms_split_on_ascii_and_cjk_punctuation_without_duplicates() -> None:
    assert terms("CSV 导出，流式写出；csv  [tsw] `reduce_to`") == [
        "CSV",
        "导出",
        "流式写出",
        "tsw",
        "reduce_to",
    ]
    assert terms("") == []
    assert is_short("评审") and not is_short("trigram")
    assert pieces("任务卡片与关键词") == {"任务卡片", "关键词"}
    assert pieces("context_catalog") == set() and pieces("对账") == set()


@pytest.mark.use_case("UC-CONTEXT-001")
def test_generated_keywords_take_tags_identifiers_and_title_phrases() -> None:
    found = generated_keywords(
        "[tsw] 实现 DN007 的 L2 检索：context_catalog 与 FTS5 trigram",
        "说明里有 `context.sqlite` 与 indexQueue。",
    )
    assert found[0] == "tsw"
    assert "context_catalog" in found
    assert "L2 检索" not in found  # phrases are split on spaces and punctuation
    assert "context.sqlite" in found and "indexQueue" in found
    assert "实现" not in found
    assert len(found) <= 8


@pytest.mark.use_case("UC-CONTEXT-001")
def test_html_reports_are_read_as_text() -> None:
    text = html_text(
        "<html><head><style>p{}</style></head><body><h1>报告</h1><p>a &amp; b</p>"
        "<script>x()</script></body></html>"
    )
    assert "报告" in text and "a & b" in text
    assert "p{}" not in text and "x()" not in text


@pytest.mark.use_case("UC-CONTEXT-005")
def test_user_decisions_are_found_in_any_event_and_classified() -> None:
    answered = event(
        "input.resolved",
        {"answer": "不包含隐藏列", "source": source("不要隐藏列", ["T001", 2, "answer"])},
    )
    docs = decision_docs(answered)
    assert [(d["ref"], d["kind"], d["text"], d["detail"]) for d in docs] == [
        ("E7", "task", "不要隐藏列", "不包含隐藏列")
    ]
    nested = event(
        "follow_up.dismissed",
        {"items": [{"resolution": {"source": source("驳回", ["T001", 2, "follow_up"])}}]},
    )
    assert [d["kind"] for d in decision_docs(nested)] == ["task"]
    authorized = event(
        "config.changed",
        {"user_source": source("可以改", ["config", "abc", "authorize"])},
        task_id=None,
    )
    assert [d["kind"] for d in decision_docs(authorized)] == ["one_time"]
    # Standing preferences come from L1, never twice; events without words give nothing.
    assert decision_docs(event("user.preference_recorded", {"source": source("x", [])})) == []
    assert decision_docs(event("worker.progress", {"text": "进展"})) == []


@pytest.mark.use_case("UC-CONTEXT-004")
def test_timeline_rows_say_what_happened() -> None:
    row = timeline_row(event("orchestrator.feedback", {"text": "补齐 live 证据"}))
    assert row is not None and row[3] == "orchestrator.feedback" and row[7] == "补齐 live 证据"
    assert timeline_row(event("config.changed", {}, task_id=None)) is None


@pytest.mark.use_case("UC-CONTEXT-002")
def test_scoring_prefers_recorded_keywords_and_scopes_follow_aliases() -> None:
    aliases = {"tsw": {"task-steward"}, "task-steward": {"tsw"}}
    recorded = {
        "task_id": "T1",
        "title": "x",
        "repo": "task-steward",
        "keywords": ["导出权限"],
        "keyword_source": "recorded",
        "summary": "",
    }
    generated = {**recorded, "task_id": "T2", "keyword_source": "generated"}
    variants = {"导出": {"导出"}}
    _, high_per = service._score(recorded, variants, {})
    _, low_per = service._score(generated, variants, {})
    assert sum(high_per.values()) > sum(low_per.values())
    match, per = service._score(recorded, {"tsw": {"tsw", "task-steward"}}, {})
    assert match == {"title": ["tsw"]} and per == {"tsw": 2}
    assert service._in_scope(recorded, "repo", "tsw", None, aliases)
    assert not service._in_scope(recorded, "repo", "other", None, aliases)
    projected = {**recorded, "project_ids": ["agent-hub"]}
    assert service._in_scope(projected, "project", None, "agent-hub", aliases)
    assert service._match(['a"b', "导出权"]) == '"a""b" OR "导出权"'


@pytest.mark.use_case("UC-CONTEXT-005")
def test_preference_scopes_only_narrow() -> None:
    assert scope_of("global") == "global" and scope_of("repo:demo") == "repo:demo"
    for bad in ("repo:", "team:x", "repo: demo"):
        with pytest.raises(TswError):
            scope_of(bad)
    assert narrower_or_same("repo:demo", "global")
    assert narrower_or_same("repo:demo", "repo:demo")
    assert not narrower_or_same("global", "repo:demo")


@pytest.mark.use_case("UC-CONTEXT-001")
def test_the_output_generation_ignores_row_order() -> None:
    a = {"output_id": "a", "type": "pr", "ref": "r", "delivery": "merged"}
    b = {"output_id": "b", "type": "doc", "ref": "s", "delivery": None}
    assert outputs_digest([a, b]) == outputs_digest([b, a])
    assert outputs_digest([a]) != outputs_digest([{**a, "delivery": "open"}])
