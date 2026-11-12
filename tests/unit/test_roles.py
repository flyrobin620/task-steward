"""The worker session's prompt (DN006 §6.2)."""

from __future__ import annotations

import pytest

from tsw.roles import session_line, worker_prompt

pytestmark = pytest.mark.use_case("UC-HOST-007")


def test_the_prompt_is_the_instructions_then_the_sessions_line() -> None:
    prompt = worker_prompt("# 说明\n\n- 一条\n\n", "T009", "a2", "dispatch")
    assert prompt == (
        "# 说明\n\n- 一条\n\n---\n\n你是 TaskSteward 任务 T009 的 worker（attempt a2）。"
        "先调用 whoami，再按任务书 runs/T009/a2/brief.md 执行。\n"
    )
    standby = session_line("T009", "a2", "prestart")
    assert "待命" in standby and "runs/T009/a2/brief.md" in standby
    assert "不要开始工作" in standby and "等主会话派发" in standby
