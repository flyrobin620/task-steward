"""The brief's "仓库与 worktree" section (CC006 §8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsw.workspace import DEFAULT_INSTRUCTIONS, Workspace, brief_section

pytestmark = pytest.mark.use_case("UC-DISPATCH-002")


def workspace(**overrides: object) -> Workspace:
    values: dict[str, object] = {
        "repo_id": "demo",
        "instructions": DEFAULT_INSTRUCTIONS,
        "checkout": Path("/src/demo"),
        "worktree": Path("/data/work/T001/a1/demo"),
        "branch": "tsw/t001-a1",
        "remote": "origin",
        "base_ref": None,
        "missing": None,
    }
    values.update(overrides)
    return Workspace(**values)  # type: ignore[arg-type]


def test_a_worktree_section_names_the_session_directory_files_and_skills() -> None:
    skills = [("weekly-update-email", "周报邮件", Path("/data/.claude/skills/w/SKILL.md"))]
    text = brief_section(workspace(), skills, "work/T001/a1")
    assert text.startswith("## 仓库与 worktree\n\n") and text.endswith("\n\n")
    assert f"会话目录：`{Path('/data/work/T001/a1/demo')}`" in text
    assert "分支 `tsw/t001-a1`" in text and "不要另建 worktree 或 clone" in text
    assert "执行目录 `work/T001/a1`" in text
    assert "`AGENTS.md`、`CLAUDE.md`、`.github/copilot-instructions.md`（不存在的跳过）" in text
    assert "宿主不会自动加载 AGENTS.md" in text and "报告中写明读了哪些" in text
    assert "  - `weekly-update-email`：周报邮件" in text


def test_without_a_worktree_the_section_says_why_and_lists_no_skills() -> None:
    ws = workspace(checkout=None, worktree=None, branch=None, missing="仓库路径 /src/demo 不存在")
    skills = [("x", "y", Path("/s"))]
    text = brief_section(ws, skills, "work/T001/a1")
    assert "tsw 没有为仓库 demo 创建 worktree：仓库路径 /src/demo 不存在" in text
    assert "仓库指令文件" in text and "`x`" not in text and "会话目录" not in text


def test_configured_instruction_files_replace_the_defaults() -> None:
    text = brief_section(workspace(instructions=("docs/RULES.md",)), [], "work/T001/a1")
    assert "`docs/RULES.md`（不存在的跳过）" in text and "AGENTS.md`" not in text
