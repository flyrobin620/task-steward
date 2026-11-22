"""Listing each reviewer's models from its command line, and changing the reviewer's model as the
user's own command (DN004 §3.4, CX004 §5, CP004 §8, CC004 §8).

The ``codex``, ``copilot`` and ``claude`` command lines are ``tests/fixtures/fake_model_lists.py``
(platforms outside this repository; ``codex`` and ``copilot`` are first on every test's ``PATH``,
see ``conftest.reviewer_bin``). The process handling, the ledger and the configuration file are
real.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw import confedit
from tsw.config import load_config
from tsw.errors import TswError
from tsw.home import Home
from tsw.review import change, models

pytestmark = pytest.mark.use_case("UC-REVIEW-004")

FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_model_lists.py"


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "models.log"
    monkeypatch.setenv("FAKE_MODELS_LOG", str(log))
    return log


def logged(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def fake_claude(tmp_path: Path) -> str:
    wrapper = tmp_path / "claude.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    return str(wrapper)


def test_codex_is_listed_through_model_list(calls: Path) -> None:
    executable = shutil.which("codex")
    assert executable is not None and "reviewer-bin" in executable  # the stand-in, not Codex
    found = models.list_models("codex-app-server", executable)
    assert found.available and found.error is None
    assert "gpt-6-astra" in [m.id for m in found.models] and found.host_default == "gpt-6.1-sol"
    [call] = logged(calls)
    assert call["argv"] == ["app-server"]
    # Both pages were read; no thread, no turn: no model was called.
    assert call["requests"] == ["initialize", "initialized", "model/list", "model/list"]


def test_copilot_is_listed_through_an_acp_session(calls: Path) -> None:
    found = models.list_models("copilot-cli", shutil.which("copilot"))
    assert found.available and found.host_default == "gpt-6.2-sol"
    assert "auto" not in [m.id for m in found.models]
    [call] = logged(calls)
    assert call["argv"] == ["--acp", "--no-auto-update", "--disable-builtin-mcps"]
    assert call["requests"] == ["initialize", "session/new", "session/close"]
    # The session is opened in an empty temporary folder, which is gone afterwards.
    assert call["session_cwd"] == call["cwd"] and "tsw-models-" in call["cwd"]
    assert not Path(call["cwd"]).exists()


def test_claude_is_listed_through_the_control_protocol(
    calls: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "1")
    monkeypatch.setenv("TSW_HOME", str(tmp_path / "x"))
    found = models.list_models("claude-headless", fake_claude(tmp_path))
    assert found.available and found.find("sonnet") is not None
    [call] = logged(calls)
    argv = call["argv"]
    assert argv[:7] == [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
    ]
    for option in ("--strict-mcp-config", "--safe-mode"):
        assert option in argv
    assert argv[argv.index("--setting-sources") + 1] == "user"
    # The host's and tsw's variables are not inherited (CC004 §3); no user message was sent.
    assert call["inherited"] == [] and call["requests"] == ["control_request"]


@pytest.mark.parametrize("reviewer", ["codex-app-server", "copilot-cli", "claude-headless"])
@pytest.mark.parametrize("mode", ["error", "exit", "empty"])
def test_a_list_that_cannot_be_had_says_why(
    reviewer: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODELS_MODE", mode)
    executable = (
        fake_claude(tmp_path)
        if reviewer == "claude-headless"
        else shutil.which(models.COMMANDS[reviewer])
    )
    found = models.list_models(reviewer, executable)
    assert not found.available and found.error
    if mode == "empty":
        assert "空" in found.error


def test_a_host_that_does_not_answer_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODELS_MODE", "hang")
    found = models.list_models("codex-app-server", shutil.which("codex"), timeout=2)
    assert not found.available and "2 秒内" in str(found.error)


# ---------------------------------------------------------------------- tsw reviewer set


def values(home: Home) -> dict[str, Any]:
    return load_config(home).values


def test_the_user_changes_the_model_as_their_own_command(home: Home) -> None:
    h = Harness(home)
    before = confedit.text_sha(confedit.read_text(home))
    planned = change.plan(
        values(home),
        reviewer="copilot-cli",
        model="gpt-6.2-sol",
        effort="high",
        unlisted=False,
        pick=None,
    )
    assert planned["before"]["reviewer"] == "codex-app-server" and planned["listed"]
    asked: list[str] = []
    out = change.apply(home, h.ledger, planned, unlisted=False, confirm=asked.append)
    assert out["changed"] and "copilot-cli" in asked[0] and "gpt-6.2-sol" in asked[0]
    reviewer = values(home)["roles"]["reviewer"]
    assert (reviewer["session"], reviewer["model"], reviewer["effort"]) == (
        "copilot-cli",
        "gpt-6.2-sol",
        "high",
    )
    assert reviewer.get("fallback_session") is None  # null removes the field (DN003 §5a)
    [event] = confedit.change_events(h.ledger.storage)
    source = event.data["user_source"]
    assert source["channel"] == "user-cli" and source["target"] == ["config", before, "authorize"]
    assert source["text"] == (
        "tsw reviewer set --reviewer copilot-cli --model gpt-6.2-sol --effort high"
    )
    assert "roles.reviewer.model" in event.data["permission"]
    # The same choice again changes nothing and records nothing.
    again = change.plan(
        values(home), reviewer=None, model="gpt-6.2-sol", effort="high", unlisted=False, pick=None
    )
    assert change.apply(home, h.ledger, again, unlisted=False, confirm=asked.append) == {
        "changed": False,
        **{k: again[k] for k in ("before", "after", "notes")},
    }
    assert len(confedit.change_events(h.ledger.storage)) == 1


def test_what_is_not_given_is_picked_from_the_list(home: Home) -> None:
    Harness(home)
    picks: list[tuple[str, tuple[str, ...], str | None]] = []

    def pick(title: str, choices: Any, labels: Any, default: str | None) -> str:
        picks.append((title, tuple(choices), default))
        return {"评审器": "codex-app-server", "评审模型": "gpt-5.5"}.get(title, choices[-1])

    planned = change.plan(
        values(home), reviewer=None, model=None, effort=None, unlisted=False, pick=pick
    )
    assert [p[0] for p in picks] == ["评审器", "评审模型", "推理强度"]
    assert picks[0][2] == "codex-app-server" and picks[1][2] == "gpt-6-astra"
    # The efforts offered are the picked model's own.
    assert picks[2][1] == ("low", "medium", "high", "xhigh") and picks[2][2] == "medium"
    assert planned["after"] == {
        "reviewer": "codex-app-server",
        "model": "gpt-5.5",
        "effort": "xhigh",
    }


def test_a_choice_outside_the_list_needs_unlisted(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(home)
    with pytest.raises(TswError) as refused:
        change.plan(
            values(home),
            reviewer="copilot-cli",
            model="gpt-9",
            effort="high",
            unlisted=False,
            pick=None,
        )
    assert refused.value.details["reason"] == "model_not_listed"
    with pytest.raises(TswError) as missing:
        change.plan(
            values(home), reviewer=None, model="gpt-6-astra", effort=None, unlisted=False, pick=None
        )
    assert missing.value.details["reason"] == "invalid_input"
    monkeypatch.setenv("FAKE_MODELS_MODE", "error")
    with pytest.raises(TswError) as unavailable:
        change.plan(
            values(home),
            reviewer="copilot-cli",
            model="gpt-9",
            effort="high",
            unlisted=False,
            pick=None,
        )
    assert unavailable.value.details["reason"] == "model_list_unavailable"
    planned = change.plan(
        values(home), reviewer="copilot-cli", model="gpt-9", effort="high", unlisted=True, pick=None
    )
    assert planned["notes"] and not planned["listed"]
    out = change.apply(home, h.ledger, planned, unlisted=True, confirm=lambda _: None)
    assert out["changed"] and values(home)["roles"]["reviewer"]["model"] == "gpt-9"
    [event] = confedit.change_events(h.ledger.storage)
    assert event.data["user_source"]["text"].endswith("--unlisted")


def test_the_orchestrator_cannot_use_the_terminal_channel(home: Home) -> None:
    h = Harness(home)
    source = {
        "channel": "user-cli",
        "principal": "owner",
        "message_ref": "cli:1",
        "text": "tsw reviewer set",
        "target": ["config", confedit.text_sha(confedit.read_text(home)), "authorize"],
    }
    with pytest.raises(TswError) as refused:
        h.run(
            "config_change",
            ops=[{"op": "set", "path": "roles.reviewer.model", "value": "gpt-6-sol"}],
            reason="冒充终端",
            user_source=source,
        )
    assert refused.value.details["reason"] == "g_user"
    assert confedit.change_events(h.ledger.storage) == []


def test_offer_marks_the_configured_model(home: Home) -> None:
    Harness(home, {"roles": {"reviewer": {"model": "gpt-5.5", "effort": "high"}}})
    offered = models.offer(values(home))
    assert offered["reviewer"] == "codex-app-server" and offered["available"]
    assert [m["id"] for m in offered["models"] if m["current"]] == ["gpt-5.5"]
    assert offered["preselected"] == {"model": "gpt-5.5", "effort": "high"}
    other = models.offer(values(home), "copilot-cli")
    assert not any(m["current"] for m in other["models"])
    assert other["preselected"] == {"model": "gpt-6.1-sol", "effort": "medium"}
