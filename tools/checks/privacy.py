"""Privacy scan for a public repository (docs/development/docs.md).

Built-in rules catch local user paths and e-mail addresses other than public no-reply and
example addresses. Extra private terms are read from ``.local/privacy-terms.txt`` (one per line,
case-insensitive, never committed) when the file exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".gz", ".zip", ".sqlite", ".pdf"}
_USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+|/(?:Users|home)/[a-z][\w.-]*/")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ALLOWED_EMAIL = re.compile(
    r"(@users\.noreply\.github\.com|^noreply@anthropic\.com|@example\.(com|org|net))$",
    re.IGNORECASE,
)


def private_terms(root: Path) -> list[str]:
    path = root / ".local" / "privacy-terms.txt"
    if not path.is_file():
        return []
    return [
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def scan(files: Iterable[Path], root: Path) -> list[str]:
    terms = private_terms(root)
    problems: list[str] = []
    for path in files:
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".local/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _USER_PATH.search(line):
                problems.append(f"{rel}:{number}: 本机用户路径")
            for email in _EMAIL.findall(line):
                if not _ALLOWED_EMAIL.search(email):
                    problems.append(f"{rel}:{number}: 邮箱地址")
            lowered = line.lower()
            problems.extend(f"{rel}:{number}: 私有词表命中" for term in terms if term in lowered)
    return problems
