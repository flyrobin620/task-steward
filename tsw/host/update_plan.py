"""Which steps a self-update takes, from what changed between two product commits (T283).

DN006 §9.8 sets the principles, CC006 §11 the Claude Code specifics. ``tsw host update-plan`` is
read-only: it compares the product repository's files between the oldest version actually running
(the commit ``host render`` recorded, the commits the main session's MCP server reports) and the
target, puts every changed file into categories, turns the categories into ordered actions, and
reports the current state (render record, plugin copies, the hosted Dashboard, the user's auto
mode rules). Run again after the update, its ``pending`` list is the verification.

The category rules are pure functions over paths and file texts, unit-tested; git, the ledger and
the host's files are read by :func:`build`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from tsw.config import get as config_get
from tsw.errors import rejected
from tsw.home import Home, read_marker
from tsw.host.claude_code import AUTO_MODE_RULES
from tsw.host.plugin_copy import installed_copies
from tsw.host.version import REPO_ROOT, commit_of, git
from tsw.machine import routine as routine_mod

# Categories in the order their actions run (DN006 §9.8).
CATEGORIES = (
    "projection",
    "install",
    "rendered",
    "instructions",
    "plugin",
    "dashboard",
    "host_rules",
    "runtime",
    "index",
    "docs",
)
# Runtime modules the MCP server never imports: a change to them is in effect for the command line
# and the hooks as soon as it is pulled, and needs no session reload. A unit test imports the MCP
# server in a fresh interpreter and checks none of these is loaded, so the list cannot go stale.
NOT_IN_MCP = frozenset(
    {
        "tsw/__main__.py",
        "tsw/cli.py",
        "tsw/host/guard.py",
        "tsw/host/status_app.py",
        "tsw/host/update_plan.py",
        "tsw/host/dashboard_service.py",
    }
)
# Files whose output ``host render`` writes into the data directory or the plugin copy.
RENDER_SOURCES = frozenset({"tsw/host/claude_code.py", "tsw/roles.py"})
CLAUDE_CODE = "tsw/host/claude_code.py"
PROJECTION = "tsw/projection.py"
INDEX = "tsw/context/index.py"
DOC_SUFFIXES = (".md", ".html")
# Skill sources and the data-directory skill each one renders to (CC006 §2, §11.3).
SKILLS = {
    "tsw/host/orchestrate_skill.md": "orchestrate",
    "tsw/host/handle_input_skill.md": "handle-input",
    "tsw/host/task_intake_skill.md": "task-intake",
    "tsw/host/gh_account_skill.md": "gh-account",
    "tsw/host/teams_chat_skill.md": "teams-chat",
    "tsw/host/update_skill.md": "tsw-update",
}
ACTIVE_PHASES = ("starting", "standby", "live")
TASK_ID = re.compile(r"(?<![A-Za-z0-9_])T\d{3,6}(?![A-Za-z0-9_])")

Git = Callable[..., "str | None"]


# ---------------------------------------------------------------------- pure rules


def classify(path: str) -> list[str]:
    """The categories of one changed file (repository-relative, ``/`` separators).

    Version constants (``projection``, ``index``) and the ``CLAUDE.md`` template
    (``instructions``) depend on file contents and are decided by :func:`categorize`.
    """
    found: list[str] = []
    top = path.split("/", 1)[0]
    if top in ("docs", "tests", "tools", ".github") or (
        "/" not in path and path.endswith(DOC_SUFFIXES)
    ):
        return ["docs"]
    if path == "pyproject.toml":
        return ["install"]
    if top == "plugin":
        return ["rendered"] if path.startswith("plugin/roles/") else ["plugin"]
    if top == "tsw":
        if path in SKILLS or path.endswith("_skill.md"):
            return ["rendered"]
        if path in RENDER_SOURCES:
            found.append("rendered")
        found.append("runtime")
        found.append("dashboard")
        return found
    return ["docs"]


def constant(text: str | None, name: str) -> str | None:
    """The right-hand side of a module-level ``NAME = value`` line, or None."""
    if text is None:
        return None
    match = re.search(rf"^{re.escape(name)}\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def claude_md_template(text: str | None) -> str | None:
    """The ``CLAUDE_MD`` template in ``tsw/host/claude_code.py``, or None."""
    if text is None:
        return None
    match = re.search(r'^CLAUDE_MD = """(.*?)"""', text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else None


def without_template(text: str | None) -> str | None:
    """``tsw/host/claude_code.py`` without its ``CLAUDE_MD`` template: what the MCP server runs."""
    if text is None:
        return None
    return re.sub(r'^CLAUDE_MD = """.*?"""', "", text, count=1, flags=re.MULTILINE | re.DOTALL)


def categorize(
    paths: Iterable[str], show: Callable[[str, str], str | None]
) -> dict[str, list[str]]:
    """Changed files by category; ``show(side, path)`` is a file's text at ``from`` / ``to``."""
    result: dict[str, list[str]] = {}

    def add(category: str, path: str) -> None:
        result.setdefault(category, [])
        if path not in result[category]:
            result[category].append(path)

    for path in sorted(set(paths)):
        for category in classify(path):
            add(category, path)
        if path == PROJECTION and constant(show("from", path), "REDUCER_VERSION") != constant(
            show("to", path), "REDUCER_VERSION"
        ):
            add("projection", path)
        if path == INDEX and constant(show("from", path), "SCHEMA_VERSION") != constant(
            show("to", path), "SCHEMA_VERSION"
        ):
            add("index", path)
        if path == CLAUDE_CODE:
            before, after = show("from", path), show("to", path)
            if claude_md_template(before) != claude_md_template(after):
                add("instructions", path)
                if without_template(before) == without_template(after):
                    # Only the main session's instructions changed: render and reload them
                    # (update.instructions); no running code changed (DN003 §3.6).
                    for category in ("runtime", "dashboard"):
                        result[category].remove(path)
    result = {category: paths for category, paths in result.items() if paths}
    return {category: result[category] for category in CATEGORIES if category in result}


def needs_reload(categories: Mapping[str, Sequence[str]]) -> list[str]:
    """Changed files that only a session reload brings into effect: the MCP server's own code and
    the plugin package (hooks registration, launcher, MCP registration)."""
    runtime = [path for path in categories.get("runtime", []) if path not in NOT_IN_MCP]
    return [*runtime, *categories.get("plugin", [])]


def _field(value: Any, key: str) -> Any:
    """``value[key]`` when ``value`` is a JSON object, else None."""
    return cast("dict[str, Any]", value).get(key) if isinstance(value, dict) else None


def task_ids(subjects: Iterable[str]) -> list[str]:
    """Task numbers named in commit subjects, in first-seen order."""
    seen: list[str] = []
    for subject in subjects:
        for task in TASK_ID.findall(subject):
            if task not in seen:
                seen.append(task)
    return seen


def rule_name(rule: str) -> str:
    return rule.split(":", 1)[0].strip()


def missing_rules(allowed: Sequence[str], wanted: Sequence[str]) -> list[dict[str, str]]:
    """Auto mode rules not present word for word: ``missing`` or ``outdated`` (same name)."""
    present = set(allowed)
    names = {rule_name(rule) for rule in allowed}
    return [
        {"rule": rule_name(rule), "state": "outdated" if rule_name(rule) in names else "missing"}
        for rule in wanted
        if rule not in present
    ]


def actions(
    categories: Mapping[str, Sequence[str]],
    *,
    config: Mapping[str, Any],
    state: Mapping[str, Any],
    to: str,
) -> list[dict[str, Any]]:
    """The ordered actions for these categories and this state (DN006 §9.8, CC006 §11.2).

    Each action says its impact (``none``, ``session``, ``user``, ``blocking``), who runs it and
    how, and whether it is still ``pending`` given the state. Actions no state can show as done
    (re-reading skills, compacting or clearing the main session) are ``manual``: never pending,
    the main session confirms them itself.
    """
    out: list[dict[str, Any]] = []
    reload_files = needs_reload(categories)
    reload_mode = str(config_get(dict(config), "update.reload") or "restart_host")
    instructions_mode = str(config_get(dict(config), "update.instructions") or "compact")
    rendered = cast("Mapping[str, Any]", state.get("render") or {})
    render_current = rendered.get("commit") == to
    projection = state.get("projection")
    if "projection" in categories or projection == "rebuild_required":
        out.append(
            {
                "action": "ledger_rebuild",
                "impact": "blocking",
                "who": "user",
                "how": "投影版本变了：新的命令行与 hooks 拒绝旧投影，旧的 MCP 拒绝重建后的投影。"
                "先在旧投影可读时做生效前的记录（new_session 时先建接班会话并让出租约），再请用户"
                "在会话外的终端运行 tsw ledger rebuild，然后重新加载会话，由新的 MCP 验证",
                # Done once this (new) code reads the projection; unknown counts as not done.
                "pending": projection != "readable",
            }
        )
    if "install" in categories:
        out.append(
            {
                "action": "reinstall",
                "impact": "user",
                "who": "user",
                "how": f"在产品仓库 {REPO_ROOT} 中运行 python -m pip install -e .",
                # No state shows a reinstall; the main session confirms it with the user.
                "pending": False,
                "manual": True,
            }
        )
    if {"rendered", "instructions", "plugin"} & set(categories) or not render_current:
        out.append(
            {
                "action": "render",
                "impact": "none",
                "who": "orchestrator",
                "how": "在数据目录运行 tsw host render（生成说明、skill、worker 说明与设置，"
                "更新插件副本）",
                "pending": not render_current,
            }
        )
    plugin = cast("Mapping[str, Any]", state.get("plugin") or {})
    if plugin.get("installed") and not plugin.get("current"):
        out.append(
            {
                "action": "plugin_copy",
                "impact": "none",
                "who": "orchestrator",
                "how": "插件副本与 plugin/ 不一致：运行 tsw host render；仍不一致时按其输出的 next "
                "先卸载再安装",
                "pending": True,
            }
        )
    skills = [SKILLS[path] for path in categories.get("rendered", []) if path in SKILLS]
    if skills:
        out.append(
            {
                "action": "reread_skills",
                "impact": "none",
                "who": "orchestrator",
                "how": "render 后用 Read 重读本会话已调用过的这些 skill（.claude/skills/<名>/"
                "SKILL.md）；没调用过的不用读",
                "skills": skills,
                "pending": False,
                "manual": True,
            }
        )
    dashboard = cast("Mapping[str, Any]", state.get("dashboard") or {})
    if dashboard.get("installed") and ("dashboard" in categories or dashboard.get("commit") != to):
        out.append(
            {
                "action": "dashboard_restart",
                "impact": "none",
                "who": "orchestrator",
                "how": "运行 tsw dashboard restart（不读取令牌）",
                "pending": dashboard.get("commit") != to,
            }
        )
    rules = cast("list[Any]", state.get("host_rules") or [])
    if rules:
        out.append(
            {
                "action": "host_rules",
                "impact": "user",
                "who": "user",
                "how": "请用户按 tsw host install-hint 的文本更新用户设置中的 auto 模式规则",
                "rules": rules,
                "pending": True,
            }
        )
    running = [str(c) for c in cast("list[Any]", state.get("running") or [])]
    if reload_files:
        out.append(
            {
                "action": "reload",
                "mode": reload_mode,
                "impact": "session",
                "who": "user" if reload_mode == "restart_host" else "orchestrator",
                "how": (
                    "请用户重启 Claude app（会打断各会话正在进行的一轮；先报告执行中的会话）"
                    if reload_mode == "restart_host"
                    else "创建接班主会话并让出调度租约（CC006 §11.4）"
                ),
                "files": reload_files,
                "pending": not running or any(c != to for c in running),
            }
        )
    elif "instructions" in categories:
        mode = reload_mode if instructions_mode == "reload" else instructions_mode
        how = {
            "compact": "更新的最后一步，请用户在主会话输入 /compact",
            "clear": "记账后调用 clear_session(self)，本轮结束后清空",
            "restart_host": "请用户重启 Claude app",
            "new_session": "创建接班主会话并让出调度租约（CC006 §11.4）",
        }[mode]
        out.append(
            {
                "action": "instructions",
                "mode": mode,
                "impact": "session",
                "who": "user" if mode in ("compact", "restart_host") else "orchestrator",
                "how": how,
                "pending": mode in ("restart_host", "new_session") and running != [to],
                "manual": mode in ("compact", "clear"),
            }
        )
    return out


# ---------------------------------------------------------------------- git and state


def _oldest(repo: Path, commits: Sequence[str]) -> str | None:
    if len(commits) == 1:
        return commits[0]
    out = git(repo, "merge-base", "--octopus", *commits)
    return out.strip() if out else None


def _active_sessions(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT task_id, attempt, phase, session_ref, session_name FROM attempts "
        "WHERE phase IN (?, ?, ?) ORDER BY task_id, attempt",
        ACTIVE_PHASES,
    ).fetchall()
    newest = routine_mod.latest(conn)
    return {
        "workers": [dict(row) for row in rows],
        "routine": None
        if newest is None
        else {k: newest.get(k) for k in ("generation", "status", "session_id")},
    }


def user_auto_rules(env: Mapping[str, str]) -> list[str] | None:
    """``autoMode.allow`` in the user's Claude Code settings, or None when unreadable."""
    config = env.get("CLAUDE_CONFIG_DIR")
    path = (Path(config).expanduser() if config else Path.home() / ".claude") / "settings.json"
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    allow: Any = _field(_field(data, "autoMode"), "allow")
    return [str(x) for x in cast("list[Any]", allow)] if isinstance(allow, list) else []


def build(
    home: Home,
    *,
    env: Mapping[str, str],
    config: Mapping[str, Any],
    conn: sqlite3.Connection | None,
    to: str | None = None,
    start: str | None = None,
    running: Sequence[str] = (),
    repo: Path = REPO_ROOT,
    dashboard: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The update plan between the running version and ``to`` (``HEAD`` by default).

    ``running`` are what the sessions loaded: the MCP server's commit and the plugin copy's
    version (``whoami``'s ``runtime``); one that is not a commit of the product repository (a
    plugin loaded in place, say) is listed as ``unresolved``. Without ``conn`` (the projection
    needs a rebuild first) the sessions are not listed and ``ledger_rebuild`` comes first.
    """
    target = commit_of(repo, to or "HEAD")
    if target is None:
        raise rejected("找不到终点提交", reason="invalid_input", field="to", value=to or "HEAD")
    marker = read_marker(home.root)
    rendered: Any = marker.get("rendered")
    render_commit = _field(rendered, "commit")
    copies = installed_copies(env)
    sources: list[dict[str, str]] = []
    unresolved: list[str] = []
    if start:
        resolved = commit_of(repo, start)
        if resolved is None:
            raise rejected("找不到起点提交", reason="invalid_input", field="from", value=start)
        base, sources = resolved, [{"source": "from", "commit": resolved}]
    else:
        for commit in running:
            resolved = commit_of(repo, commit) if commit.strip() else None
            if resolved is None:
                unresolved.append(commit)
                continue
            sources.append({"source": "running", "commit": resolved})
        if isinstance(render_commit, str) and commit_of(repo, render_commit):
            sources.append({"source": "rendered", "commit": str(commit_of(repo, render_commit))})
        if not sources:
            for copy in copies:
                version = copy.get("version")
                resolved = commit_of(repo, str(version)) if version else None
                if resolved:
                    sources.append({"source": "plugin_version", "commit": resolved})
        if not sources:
            raise rejected(
                "不知道运行中的版本：没有 render 记录、--running 或可识别的插件副本版本；"
                "请用 --from 给出起点",
                reason="from_unknown",
            )
        base = _oldest(repo, [s["commit"] for s in sources]) or sources[0]["commit"]
    names = git(repo, "diff", "--name-only", f"{base}..{target}") or ""
    changed = [line.strip() for line in names.splitlines() if line.strip()]

    def show(side: str, path: str) -> str | None:
        return git(repo, "show", f"{base if side == 'from' else target}:{path}")

    categories = categorize(changed, show)
    subjects = (git(repo, "log", "--format=%s", f"{base}..{target}") or "").splitlines()
    tasks = [
        {
            "task_id": task,
            "reports": sorted(
                p.relative_to(home.root).as_posix()
                for p in (home.root / "runs" / task).glob("*/result.md")
            ),
        }
        for task in task_ids(subjects)
    ]
    allowed = user_auto_rules(env)
    rules = [] if allowed is None else missing_rules(allowed, AUTO_MODE_RULES)
    state: dict[str, Any] = {
        "render": rendered if isinstance(rendered, dict) else None,
        "plugin": {
            "installed": bool(copies),
            "current": bool(copies) and all(c.get("current") for c in copies),
            "copies": copies,
        },
        "dashboard": dict(dashboard) if dashboard is not None else None,
        "host_rules": rules,
        "host_rules_readable": allowed is not None,
        "running": [s["commit"] for s in sources if s["source"] == "running"],
        "projection": "readable" if conn is not None else "rebuild_required",
    }
    planned = actions(categories, config=config, state=state, to=target)
    return {
        "from": base,
        "to": target,
        "from_sources": sources,
        "unresolved": unresolved,
        "commits": len(subjects),
        "changed": changed,
        "categories": categories,
        "actions": planned,
        "pending": [a["action"] for a in planned if a.get("pending")],
        "manual": [a["action"] for a in planned if a.get("manual")],
        "tasks": tasks,
        "sessions": _active_sessions(conn) if conn is not None else {"unavailable": True},
        "state": state,
    }
