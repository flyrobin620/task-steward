"""The spare-session pool's pure rules (DN002 §5.9, T363): titles, prompts, messages, age."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tsw.machine import spares


class Config:
    def __init__(self, **values: Any) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@pytest.mark.use_case("UC-SPARE-001")
def test_title_and_prompt() -> None:
    assert spares.spare_title(3, Config(**{"sessions.title_on_spare": "[备用]"})) == "[备用] S3"  # pyright: ignore[reportArgumentType]
    assert spares.spare_title(3, Config(**{"sessions.title_on_spare": ""})) == "S3"  # pyright: ignore[reportArgumentType]
    prompt = spares.spare_prompt("S3")
    # It waits; a dispatch is told by its tag, anything else is the user's conversation.
    assert "备用会话就绪" in prompt and spares.CLAIM_TAG in prompt
    assert "set_session_title" in prompt and "不按任务书工作" in prompt


@pytest.mark.use_case("UC-SPARE-001")
def test_age() -> None:
    config = Config(**{"scheduling.spare.max_age_hours": 24})
    row = {"created_at": "2026-11-05T00:00:00Z", "requested_at": "2026-11-04T23:59:00Z"}
    assert not spares.expired(row, config, "2026-11-05T23:59:00Z")  # pyright: ignore[reportArgumentType]
    assert spares.expired(row, config, "2026-11-06T00:00:01Z")  # pyright: ignore[reportArgumentType]
    assert not spares.expired({}, config, "2030-01-01T00:00:00Z")  # pyright: ignore[reportArgumentType]


@pytest.mark.use_case("UC-SPARE-002")
def test_claim_and_start_messages(tmp_path: Path) -> None:
    target = tmp_path / "work" / "T001" / "a1" / "demo"
    message = spares.claim_message("T001", "a1", "S2", "git -C x worktree add y", target)
    assert message.startswith(spares.CLAIM_TAG)
    # Three steps: make the folder, move, record; then the start message comes.
    assert "git -C x worktree add y" in message and str(target) in message
    assert "mcp__ccd_directory__change_directory" in message and "spare_moved" in message
    assert spares.START_TAG in message

    class Home:
        root = tmp_path

    start = spares.start_message(Home(), "T001", "a1")  # pyright: ignore[reportArgumentType]
    assert start.startswith(spares.START_TAG)
    assert str(tmp_path / "roles" / "worker.md") in start and "runs/T001/a1/brief.md" in start


@pytest.mark.use_case("UC-SPARE-002")
def test_target_problem(tmp_path: Path) -> None:
    assert spares.target_problem(None, None) is not None
    missing = tmp_path / "nope"
    assert "不存在" in str(spares.target_problem({"target_branch": None}, missing))
    tmp_path.joinpath("plain").mkdir()
    assert spares.target_problem({"target_branch": None}, tmp_path / "plain") is None
