"""The reviewer model lists and the choice from them (DN004 §3.4): parsing the hosts' recorded
answers, what is offered first, what a choice may be, and the install's model questions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tsw.config import DEFAULTS
from tsw.errors import TswError
from tsw.home import Home
from tsw.host import guard
from tsw.review import models
from tsw.review.models import Model, ModelList
from tsw.setup import questions as q

pytestmark = pytest.mark.use_case("UC-REVIEW-004")

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "models"


def sample(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def codex() -> ModelList:
    return models.parse_codex([sample("codex-model-list.json")])


def copilot() -> ModelList:
    return models.parse_copilot(sample("copilot-session-new.json")["result"])


def claude() -> ModelList:
    return models.parse_claude(sample("claude-initialize.json")["response"]["response"])


def test_codex_lists_visible_models_with_their_own_efforts() -> None:
    found = codex()
    ids = [m.id for m in found.models]
    assert "gpt-6-astra" in ids and found.host_default == "gpt-6.1-sol"
    # Hidden models (includeHidden returned them) are left out.
    hidden = [d["id"] for d in sample("codex-model-list.json")["data"] if d.get("hidden")]
    assert hidden and not set(hidden) & set(ids)
    # Efforts differ per model: one has "ultra", one has no "max".
    efforts = {m.id: m.efforts for m in found.models}
    assert "ultra" in efforts["gpt-5.6-terra"] and "max" not in efforts["gpt-5.5"]
    assert found.find("gpt-6-astra") is not None and found.available


def test_codex_follows_pages() -> None:
    data = sample("codex-model-list.json")["data"]
    found = models.parse_codex([{"data": data[:2]}, {"data": data[2:]}])
    assert [m.id for m in found.models] == [m.id for m in codex().models]


def test_copilot_drops_auto_and_takes_the_session_efforts() -> None:
    found = copilot()
    ids = [m.id for m in found.models]
    assert "auto" not in ids and len(ids) == len(set(ids))
    assert found.host_default == "gpt-6.2-sol" and "gpt-6.1-sol" in ids
    assert all(m.efforts == ("low", "medium", "high", "xhigh", "max") for m in found.models)
    assert all(m.default_effort == "medium" for m in found.models)
    disabled = {
        "models": {
            "availableModels": [
                {"modelId": "x-1", "_meta": {"copilotEnablement": "disabled"}},
                {"modelId": "x-2"},
            ]
        }
    }
    assert [m.id for m in models.parse_copilot(disabled).models] == ["x-2"]


def test_claude_drops_default_and_keeps_what_an_alias_stands_for() -> None:
    found = claude()
    ids = [m.id for m in found.models]
    assert "default" not in ids
    assert ids[:4] == ["opus", "fable", "sonnet", "haiku"]
    # A context suffix is not part of a configured model ID (DN003 §1).
    assert "claude-opus-5" in ids and not any("[" in i for i in ids)
    sonnet = found.find("sonnet")
    assert sonnet is not None and sonnet.also == ("claude-sonnet-5-5",)
    # The full ID (in any of the host's spellings) finds the alias's entry.
    assert found.find("claude-sonnet-5.5[1m]") is sonnet
    assert found.find("claude-sonnet-5-5") is sonnet


def test_preselection_prefers_the_configuration_then_the_default_then_the_host() -> None:
    found = copilot()
    assert models.preselect_model(found, "gpt-6.2-astra") == "gpt-6.2-astra"
    assert models.preselect_model(found, "gone-1") == "gpt-6.1-sol"  # the built-in default
    only = ModelList("copilot-cli", [Model("a-1"), Model("b-1")], host_default="b-1")
    assert models.preselect_model(only, None) == "b-1"
    assert models.preselect_model(ModelList("copilot-cli", [Model("a-1")]), None) == "a-1"
    # Without a list: the configuration, else the built-in default.
    empty = ModelList("codex-app-server", error="找不到 codex 命令行")
    assert models.preselect_model(empty, None) == DEFAULTS["roles"]["reviewer"]["model"]
    assert models.preselect_model(empty, "gpt-x") == "gpt-x"
    # Claude's built-in default is a full ID: its alias's entry is offered.
    assert models.preselect_model(claude(), None) == "sonnet"


def test_effort_preselection_stays_within_the_model() -> None:
    found = codex()
    assert models.preselect_effort(found, "gpt-6-astra", "high") == "high"
    assert models.preselect_effort(found, "gpt-6-astra", "ultra") == "medium"  # built-in
    lone = ModelList("copilot-cli", [Model("m-1", efforts=("low", "max"), default_effort="max")])
    assert models.preselect_effort(lone, "m-1", None) == "max"
    first = ModelList("copilot-cli", [Model("m-1", efforts=("low", "high"))])
    assert models.preselect_effort(first, "m-1", None) == "low"


def test_a_choice_is_checked_against_the_list() -> None:
    found = codex()
    assert models.choose(found, "gpt-6-astra", "high") == ("gpt-6-astra", "high", [])
    for model, effort, reason in (
        ("gpt-9", "high", "model_not_listed"),
        ("gpt-5.5", "max", "effort_not_supported"),
        ("bad id", "high", "invalid_input"),
    ):
        with pytest.raises(TswError) as refused:
            models.choose(found, model, effort)
        assert refused.value.details["reason"] == reason
    # --unlisted writes it, saying it is not verified.
    model, effort, notes = models.choose(found, "gpt-9", "high", unlisted=True)
    assert (model, effort) == ("gpt-9", "high") and "第一轮评审时核实" in notes[0]
    missing = ModelList("copilot-cli", error="找不到 copilot 命令行")
    with pytest.raises(TswError) as refused:
        models.choose(missing, "gpt-6.2-sol", "high")
    assert refused.value.details["reason"] == "model_list_unavailable"
    assert "未经核实" in models.choose(missing, "gpt-6.2-sol", "high", unlisted=True)[2][0]


def test_the_operations_change_only_what_differs() -> None:
    values: dict[str, Any] = {
        "roles": {
            "reviewer": {"session": "codex-app-server", "model": "gpt-6-astra", "effort": "medium"}
        }
    }
    assert models.ops(values, "codex-app-server", "gpt-6-astra", "high") == [
        {"op": "set", "path": "roles.reviewer.effort", "value": "high"}
    ]
    moved = models.ops(values, "copilot-cli", "gpt-6.2-sol", "medium")
    assert moved == [
        {"op": "set", "path": "roles.reviewer.session", "value": "copilot-cli"},
        {"op": "set", "path": "roles.reviewer.fallback_session", "value": None},
        {"op": "set", "path": "roles.reviewer.model", "value": "gpt-6.2-sol"},
    ]
    assert models.ops(values, "codex-app-server", "gpt-6-astra", "medium") == []


def test_an_unknown_reviewer_is_not_listed() -> None:
    assert models.list_models("codex-exec", "codex").error is not None
    assert "找不到 copilot" in str(models.list_models("copilot-cli", None).error)
    with pytest.raises(TswError):
        models.offer({"roles": {"reviewer": {"session": "codex-exec"}}})


# ---------------------------------------------------------------------- the install's questions


def question(qid: str) -> q.Question:
    return next(x for x in q.questions({"PATH": ""}, Path.cwd()) if x.id == qid)


def test_the_model_questions_follow_the_reviewer_answer() -> None:
    lists = {"codex-app-server": codex(), "copilot-cli": copilot()}
    seen: list[str] = []

    def find(reviewer: str) -> ModelList:
        seen.append(reviewer)
        return lists.get(reviewer) or ModelList(reviewer, error="找不到 claude 命令行")

    model_q, effort_q = question("reviewer_model"), question("reviewer_effort")
    assert model_q.to_dict()["depends_on"] == "reviewer"
    # Not asked without a reviewer, or without a lister (an answers file).
    assert q.bind(model_q, {"reviewer": "none"}, find) is None
    assert q.bind(model_q, {"reviewer": "copilot-cli"}, None) is None
    bound = q.bind(model_q, {"reviewer": "copilot-cli"}, find)
    assert bound is not None and bound.kind == "choice"
    assert "auto" not in bound.choices and bound.default == "gpt-6.1-sol"
    assert bound.labels["gpt-6.2-sol"] == "GPT-6.2 Sol"
    effort = q.bind(effort_q, {"reviewer": "codex-app-server", "reviewer_model": "gpt-5.5"}, find)
    assert effort is not None and effort.choices == ("low", "medium", "high", "xhigh")
    assert effort.default == "medium"
    # No list: a typed model ID, the reason in the note.
    typed = q.bind(model_q, {"reviewer": "claude-headless"}, find)
    assert typed is not None and typed.kind == "text" and typed.default == "claude-sonnet-5-5"
    assert "找不到 claude" in typed.note
    assert seen == ["copilot-cli", "codex-app-server", "claude-headless"]


def test_the_terminal_asks_the_model_and_effort_after_the_reviewer() -> None:
    qs = [
        x
        for x in q.questions({"PATH": ""}, Path.cwd())
        if x.id in ("reviewer", "reviewer_model", "reviewer_effort", "language")
    ]
    replies = iter(["", "copilot-cli", "gpt-6.2-astra", "max"])
    said: list[str] = []
    answers = q.ask(qs, {}, lambda _: next(replies), said.append, lambda _: copilot())
    assert answers == {
        "language": "zh-CN",
        "reviewer": "copilot-cli",
        "reviewer_model": "gpt-6.2-astra",
        "reviewer_effort": "max",
    }
    assert any("gpt-6.2-astra：GPT-6.2 Astra" in line for line in said)
    # none: the model questions are not asked.
    replies = iter(["", "none"])
    answers = q.ask(qs, {}, lambda _: next(replies), said.append, lambda _: copilot())
    assert "reviewer_model" not in answers and answers["reviewer"] == "none"


def test_model_answers_are_checked_and_optional() -> None:
    qs = q.questions({"PATH": ""}, Path.cwd())
    home = str(Path.cwd() / "data")
    answers, problems = q.normalize({"home": home}, qs)
    assert problems == [] and "reviewer_model" not in answers
    _, problems = q.normalize({"home": home, "reviewer_model": "a b"}, qs)
    assert any(p.startswith("reviewer_model：") for p in problems)
    answers, problems = q.normalize(
        {"home": home, "reviewer_model": "gpt-6.2-sol", "reviewer_effort": "high"}, qs
    )
    assert problems == [] and answers["reviewer_model"] == "gpt-6.2-sol"


def test_differences_name_the_model_and_effort() -> None:
    values = {
        "roles": {
            "reviewer": {"session": "copilot-cli", "model": "gpt-6.1-sol", "effort": "medium"}
        }
    }
    found = q.config_differences(
        values,
        {
            "language": "zh-CN",
            "host": "claude-code",
            "human_channels": "in-session",
            "dashboard_port": 47800,
            "reviewer": "copilot-cli",
            "reviewer_model": "gpt-6.2-sol",
        },
    )
    assert any(d.startswith("roles.reviewer.model：配置为 'gpt-6.1-sol'") for d in found)
    assert not any(d.startswith("roles.reviewer.effort") for d in found)


@pytest.mark.parametrize(
    ("command", "worker", "orchestrator", "refused"),
    [
        ("tsw reviewer models --json", True, False, False),
        ("tsw reviewer models --reviewer copilot-cli", False, True, False),
        ("tsw reviewer set --model gpt-5.5 --effort high --yes", False, True, True),
        ("tsw reviewer set --model gpt-5.5 --effort high --yes", True, False, True),
    ],
)
def test_sessions_may_list_but_not_change_the_reviewer(
    home: Home, command: str, worker: bool, orchestrator: bool, refused: bool
) -> None:
    """Listing only reads; changing is the user's own command in a terminal (DN006 §8.10)."""
    problem = guard.bash_problem(
        command,
        home=home,
        push_allowed=False,
        pr_allowed=False,
        worker=worker,
        orchestrator=orchestrator,
    )
    assert (problem is not None) is refused
    if refused:
        assert problem is not None and "不能运行 tsw reviewer set" in problem
