"""Which product commit is running, and which one ``host render`` rendered (DN006 §9.9, T283).

The MCP server records :func:`runtime` when it starts; ``host render`` records
:func:`render_record` in ``tsw-home.json``. A self-update starts from the oldest of them
(``tsw.host.update_plan``). Only git is read.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tsw.events import utc_now

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GIT_TIMEOUT_SECONDS = 30


def git(repo: Path, *args: str) -> str | None:
    """``git -C repo <args>`` stdout, or None when git fails or is missing."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def commit_of(repo: Path, rev: str) -> str | None:
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return out.strip() if out else None


def head(repo: Path = REPO_ROOT) -> dict[str, Any] | None:
    """The product repository's HEAD commit and whether the work tree has changes."""
    commit = commit_of(repo, "HEAD")
    if commit is None:
        return None
    status = git(repo, "status", "--porcelain", "--untracked-files=no")
    return {"commit": commit, "dirty": bool(status and status.strip())}


def runtime(env: Mapping[str, str], repo: Path = REPO_ROOT) -> dict[str, Any]:
    """What this process loaded (the MCP server records it at start, DN006 §9.9)."""
    found = head(repo) or {}
    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    return {
        "commit": found.get("commit"),
        "dirty": found.get("dirty"),
        "started_at": utc_now(),
        "plugin_version": Path(plugin_root).name if plugin_root else None,
    }


def render_record(repo: Path = REPO_ROOT) -> dict[str, Any] | None:
    """What ``host render`` writes to ``tsw-home.json`` as ``rendered``."""
    found = head(repo)
    if found is None:
        return None
    return {**found, "at": utc_now()}
