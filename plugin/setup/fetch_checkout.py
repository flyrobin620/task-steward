"""Get the product checkout for the session install path (DN009 §4.2, CC009 §4).

Run from the plugin's cached copy with the interpreter the probe found:

    python -X utf8 "${CLAUDE_PLUGIN_ROOT}/setup/fetch_checkout.py" [--dest DIR]

Without a checkout at ``--dest`` (default ``%LOCALAPPDATA%\\TaskSteward\\task-steward``) it clones
the product repository there and moves the branch to the commit of the installed plugin
(``gitCommitSha`` in Claude Code's ``installed_plugins.json``), so the checkout and the plugin copy
start from the same commit. An existing checkout is only checked: the same repository, a clean
work tree; it is never changed. Prints one JSON object. Only the standard library: the plugin's
cache has no TaskSteward runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_ID = "task-steward@task-steward"
MARKETPLACE = "task-steward"
DEFAULT_URL = "https://github.com/flyrobin620/task-steward.git"
GIT_TIMEOUT_SECONDS = 600


def plugins_root() -> Path:
    explicit = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config).expanduser() if config else Path.home() / ".claude") / "plugins"


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed_commit() -> str | None:
    records = _read(plugins_root() / "installed_plugins.json")
    installs = (
        (records or {}).get("plugins", {}).get(PLUGIN_ID) if isinstance(records, dict) else None
    )
    for install in installs if isinstance(installs, list) else []:
        sha = install.get("gitCommitSha") if isinstance(install, dict) else None
        if isinstance(sha, str) and sha:
            return sha
    return None


def marketplace_url() -> str:
    """Where the plugin came from: its git marketplace's URL, else the public repository."""
    known = _read(plugins_root() / "known_marketplaces.json")
    entry = known.get(MARKETPLACE) if isinstance(known, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    if isinstance(source, dict):
        if source.get("source") == "git" and isinstance(source.get("url"), str):
            return str(source["url"])
        if source.get("source") == "github" and isinstance(source.get("repo"), str):
            return f"https://github.com/{source['repo']}.git"
    return DEFAULT_URL


def default_dest() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "TaskSteward" / "task-steward"


def same_repository(a: str, b: str) -> bool:
    def key(url: str) -> str:
        url = url.strip().rstrip("/")
        url = url.removesuffix(".git")
        for prefix in ("https://", "http://", "ssh://git@", "git@"):
            url = url.removeprefix(prefix)
        return url.replace(":", "/").lower()

    return key(a) == key(b)


def git(git_cmd: str, *args: str, cwd: Path | None = None) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            [git_cmd, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def fetch(dest: Path, url: str, commit: str | None, git_cmd: str) -> dict[str, Any]:
    result: dict[str, Any] = {"checkout": str(dest), "url": url, "plugin_commit": commit}
    if dest.exists() and any(dest.iterdir()):
        if not (dest / ".git").exists():
            return {**result, "ok": False, "problem": "目录非空且不是 git 检出"}
        code, origin = git(git_cmd, "remote", "get-url", "origin", cwd=dest)
        if code != 0 or not same_repository(origin, url):
            return {**result, "ok": False, "problem": f"已有的检出来自另一个仓库（{origin}）"}
        code, status = git(git_cmd, "status", "--porcelain", "--untracked-files=no", cwd=dest)
        if code != 0 or status:
            return {**result, "ok": False, "problem": "已有的检出有未提交的改动"}
        _, head = git(git_cmd, "rev-parse", "HEAD", cwd=dest)
        return {
            **result,
            "ok": True,
            "created": False,
            "head": head,
            "same_as_plugin": commit is None or head == commit,
        }
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, output = git(git_cmd, "clone", url, str(dest))
    if code != 0:
        return {**result, "ok": False, "problem": "克隆失败", "output": output[-2000:]}
    if commit:
        code, _ = git(git_cmd, "cat-file", "-e", f"{commit}^{{commit}}", cwd=dest)
        if code == 0:
            # The branch at the plugin's commit: a later self-update fast-forwards it (DN006 §9.8).
            git(git_cmd, "reset", "--hard", commit, cwd=dest)
    _, head = git(git_cmd, "rev-parse", "HEAD", cwd=dest)
    return {
        **result,
        "ok": True,
        "created": True,
        "head": head,
        "same_as_plugin": commit is None or head == commit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="取得 TaskSteward 产品检出（会话安装路径）")
    parser.add_argument(
        "--dest", help="检出位置（默认 %%LOCALAPPDATA%%\\TaskSteward\\task-steward）"
    )
    parser.add_argument("--url", help="产品仓库（默认取插件市场的来源）")
    parser.add_argument("--commit", help="要检出的提交（默认取已安装插件的 gitCommitSha）")
    parser.add_argument("--git", default="git", help="git 命令（默认 PATH 中的 git）")
    args = parser.parse_args(argv)
    git_cmd = shutil.which(args.git) or args.git
    dest = Path(args.dest).expanduser() if args.dest else default_dest()
    result = fetch(dest, args.url or marketplace_url(), args.commit or installed_commit(), git_cmd)
    if result.get("ok"):
        launcher = dest / "plugin" / "bin" / "tsw_launch.py"
        result["launcher"] = str(launcher)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
