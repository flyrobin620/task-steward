"""Automation that the rule documents name must exist (docs/development/ci.md).

Commands written as ``python <path>.py …`` in inline code of ``AGENTS.md`` and
``docs/development/*.md`` must name a script of this repository, and each subcommand or option
word after it (``render``, ``--tier``, ``unit``; placeholders such as ``<runId>`` are skipped) must
appear in that script as a string literal. CI labels written as ``ci:<name>`` must be handled by a
workflow under ``.github/workflows/``: the whole label, quoted, outside a YAML comment (where the
workflow compares the pull request's labels with it). Whether a label exists on GitHub is not
checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

_CODE = re.compile(r"`([^`\n]+)`")
_COMMAND = re.compile(r"^python(?:\s+-X\s+\S+)?\s+(\S+\.py)((?:\s+\S+)*)$")
_WORD = re.compile(r"^-{0,2}[a-z][\w-]*$")
_LABEL = re.compile(r"^ci:[a-z0-9][a-z0-9-]*$")


def _without_comment(line: str) -> str:
    """A workflow line without its YAML comment (a ``#`` at the start or after whitespace)."""
    return "" if line.lstrip().startswith("#") else re.split(r"\s#", line, maxsplit=1)[0]


def rule_documents(root: Path) -> list[Path]:
    return [root / "AGENTS.md", *sorted((root / "docs" / "development").glob("*.md"))]


def check(root: Path) -> list[str]:
    workflows = "\n".join(
        _without_comment(line)
        for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    problems: list[str] = []
    for document in rule_documents(root):
        if not document.is_file():
            continue
        rel = document.relative_to(root).as_posix()
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            for raw in _CODE.findall(line):
                span = raw.strip()
                if _LABEL.match(span) and not re.search(f"['\"]{re.escape(span)}['\"]", workflows):
                    problems.append(f"{rel}:{number}: 标签 {span} 没有被 .github/workflows 处理")
                command = _COMMAND.match(span)
                if command is None:
                    continue
                script = root / command.group(1)
                if not script.is_file():
                    problems.append(f"{rel}:{number}: 命令的脚本不存在 {command.group(1)}")
                    continue
                source = script.read_text(encoding="utf-8")
                problems.extend(
                    f"{rel}:{number}: {command.group(1)} 不认识 {word}"
                    for word in command.group(2).split()
                    if _WORD.match(word) and f'"{word}"' not in source
                )
    return problems
