"""The ``claude-code`` host integration's install operations (DN006 §6.2, DN009 §3, CC009).

- :func:`install_plugin`: point the ``task-steward`` marketplace at the product checkout and
  install the plugin from it. ``claude plugin marketplace add <checkout>`` re-points a marketplace
  of the same name that came from elsewhere (the session path's GitHub marketplace) without
  uninstalling the plugin (T305 E10); the terminal path registers it for the first time.
- :func:`apply_user_settings`: write the data directory (``env.TSW_HOME``), the interpreter the
  plugin's MCP server and hooks run (``env.TSW_PYTHON``) and the three auto mode rules
  (CC008 §6) into the user's Claude Code settings, after a backup; rules are recognized by name
  and their old text replaced; nothing else is touched.
- :func:`remove_user_settings` and :func:`remove_plugin`: undo what the install recorded as its
  own.

Claude Code is a platform outside this repository: every ``claude`` call goes through
:func:`run_claude`, which tests point at a stand-in command; its files (``known_marketplaces.json``,
``installed_plugins.json``, ``settings.json``) are read where ``CLAUDE_CONFIG_DIR`` says.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from tsw.events import utc_now
from tsw.host.claude_code import AUTO_MODE_RULES
from tsw.host.plugin_copy import PLUGIN_ID, installed_copies, plugins_root
from tsw.storage.fs import atomic_write

MARKETPLACE = "task-steward"
CLAUDE_TIMEOUT_SECONDS = 300
# Where the Claude desktop app installs its command line (CC009 §3); not always on PATH.
APP_CLAUDE = ("Programs", "ClaudeCode", "claude.exe")
# The user-settings keys the install owns (CC009 §5).
ENV_KEYS = ("TSW_HOME", "TSW_PYTHON")


def rule_name(rule: str) -> str:
    return rule.split(":", 1)[0].strip()


RULE_NAMES = tuple(rule_name(rule) for rule in AUTO_MODE_RULES)


def _norm(path: str | Path) -> str:
    from tsw.setup.record import norm  # noqa: PLC0415  (the install's own helper)

    return norm(path)


def find_claude(env: Mapping[str, str], explicit: str | None = None) -> str | None:
    """The ``claude`` command line: the one given, else on ``PATH``, else the desktop app's."""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = shutil.which("claude", path=env.get("PATH"))
    if found:
        return found
    local = env.get("LOCALAPPDATA")
    if local:
        candidate = Path(local).joinpath(*APP_CLAUDE)
        if candidate.is_file():
            return str(candidate)
    return None


def run_claude(claude: str, env: Mapping[str, str], *args: str) -> dict[str, Any]:
    """Run ``claude <args>``; the command (without the program path), code and output tail."""
    command = "claude " + " ".join(args)
    try:
        proc = subprocess.run(
            [claude, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env),
            timeout=CLAUDE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "returncode": None, "error": str(exc)}
    lines = [line for line in f"{proc.stdout}\n{proc.stderr}".splitlines() if line.strip()]
    return {"command": command, "returncode": proc.returncode, "output": lines[-10:]}


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


# ---------------------------------------------------------------------- plugin


def marketplace_source(env: Mapping[str, str]) -> dict[str, Any] | None:
    """The ``task-steward`` marketplace's source as Claude Code recorded it, or None."""
    known = _read(plugins_root(env) / "known_marketplaces.json") or {}
    entry: Any = known.get(MARKETPLACE)
    source: Any = cast("dict[str, Any]", entry).get("source") if isinstance(entry, dict) else None
    return cast("dict[str, Any]", source) if isinstance(source, dict) else None


def points_at(source: Mapping[str, Any] | None, checkout: Path) -> bool:
    if source is None or source.get("source") != "directory":
        return False
    path = source.get("path")
    return isinstance(path, str) and _norm(path) == _norm(checkout)


def plugin_state(env: Mapping[str, str], checkout: Path) -> dict[str, Any]:
    source = marketplace_source(env)
    # Only a user-scope install reaches every session in any folder (T308 review r1 F6); other
    # scopes are left as they are.
    copies = [c for c in installed_copies(env) if c.get("scope") == "user"]
    return {
        "marketplace": source,
        "points_at_checkout": points_at(source, checkout),
        "installed": bool(copies),
        "current": bool(copies) and all(c.get("current") for c in copies),
        "copies": copies,
    }


def plugin_actions(state: Mapping[str, Any], checkout: Path) -> list[str]:
    """The ``claude`` commands :func:`install_plugin` would run in this state."""
    planned: list[str] = []
    if not state.get("points_at_checkout"):
        planned.append(f"claude plugin marketplace add {checkout}")
    if not state.get("installed"):
        planned.append(f"claude plugin install {PLUGIN_ID} --scope user")
    return planned


def install_plugin(env: Mapping[str, str], claude: str, checkout: Path) -> dict[str, Any]:
    """Register the checkout as the plugin source and install the plugin; idempotent."""
    before = plugin_state(env, checkout)
    ran: list[dict[str, Any]] = []
    if not before["points_at_checkout"]:
        ran.append(run_claude(claude, env, "plugin", "marketplace", "add", str(checkout)))
    if not plugin_state(env, checkout)["installed"]:
        ran.append(run_claude(claude, env, "plugin", "install", PLUGIN_ID, "--scope", "user"))
    after = plugin_state(env, checkout)
    return {
        "before": {k: before[k] for k in ("marketplace", "points_at_checkout", "installed")},
        "ran": ran,
        "after": {k: after[k] for k in ("marketplace", "points_at_checkout", "installed")},
        "ok": bool(after["points_at_checkout"] and after["installed"]),
    }


def source_location(source: Mapping[str, Any] | None) -> str | None:
    """What ``claude plugin marketplace add`` takes to register ``source`` again."""
    if source is None:
        return None
    kind = source.get("source")
    if kind == "directory" and isinstance(source.get("path"), str):
        return str(source["path"])
    ref = source.get("ref")
    suffix = f"#{ref}" if isinstance(ref, str) and ref else ""
    if kind in ("git", "url") and isinstance(source.get("url"), str):
        return f"{source['url']}{suffix}"
    if kind == "github" and isinstance(source.get("repo"), str):
        return f"https://github.com/{source['repo']}.git{suffix}"  # HTTPS, not SSH (CC009 §3)
    return None


def _same_source(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    return source_location(a) == source_location(b) if a and b else a is None and b is None


def remove_plugin(env: Mapping[str, str], claude: str, owned: Mapping[str, Any]) -> dict[str, Any]:
    """Put the plugin back as the install found it, as ``owned`` (its record) says (DN009 §10).

    The marketplace the install pointed at the checkout goes back to its old source, or is
    removed when the install added it. Claude Code only re-points a marketplace with a
    ``marketplace add`` of a source its settings accept, and removing a marketplace uninstalls
    its plugins (T308 live), so a restore removes the marketplace, adds the old source and, when
    the user-scope plugin was installed before, installs it again from there. A user-scope
    plugin the install added is uninstalled. Installs in other scopes are never touched.
    """
    before: Any = owned.get("marketplace_before")
    source = cast("dict[str, Any]", before) if isinstance(before, dict) else None
    had_plugin = bool(owned.get("installed_before"))
    ran: list[dict[str, Any]] = []
    notes: list[str] = []
    current = marketplace_source(env)
    location = source_location(source)
    if source is not None and location is None:
        notes.append("安装前的插件市场来源无法识别，保持现状")
    elif not _same_source(current, source):
        if current is not None:
            ran.append(run_claude(claude, env, "plugin", "marketplace", "remove", MARKETPLACE))
        if location is not None:
            ran.append(run_claude(claude, env, "plugin", "marketplace", "add", location))
            notes.append(f"插件市场改回安装前的来源：{location}")
    user = [c for c in installed_copies(env) if c.get("scope") == "user"]
    if had_plugin and not user and location is not None:
        ran.append(run_claude(claude, env, "plugin", "install", PLUGIN_ID, "--scope", "user"))
    elif not had_plugin and user:
        ran.append(run_claude(claude, env, "plugin", "uninstall", PLUGIN_ID, "--scope", "user"))
    if had_plugin:
        notes.append(
            "插件在安装前就已安装（用户范围），已按原来的来源保留；要完全移除时运行 "
            f"claude plugin uninstall {PLUGIN_ID} 与 claude plugin marketplace remove {MARKETPLACE}"
        )
    user = [c for c in installed_copies(env) if c.get("scope") == "user"]
    restored = (source is not None and location is None) or _same_source(
        marketplace_source(env), source
    )
    return {"ran": ran, "notes": notes, "ok": restored and bool(user) == had_plugin}


# ---------------------------------------------------------------------- user settings


def settings_path(env: Mapping[str, str]) -> Path:
    """The user's Claude Code settings: ``$CLAUDE_CONFIG_DIR/settings.json`` or
    ``~/.claude/settings.json``."""
    config = env.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "settings.json"


def read_settings(env: Mapping[str, str]) -> tuple[dict[str, Any] | None, bool]:
    """The settings object and whether the file exists; ``(None, True)`` when it is unreadable."""
    path = settings_path(env)
    if not path.is_file():
        return {}, False
    return _read(path), True


def _container(data: dict[str, Any], key: str) -> dict[str, Any]:
    found: Any = data.get(key)
    if not isinstance(found, dict):
        found = {}
        data[key] = found
    return cast("dict[str, Any]", found)


def _allow(data: dict[str, Any]) -> list[Any]:
    auto = _container(data, "autoMode")
    found: Any = auto.get("allow")
    if not isinstance(found, list):
        found = []
        auto["allow"] = found
    return cast("list[Any]", found)


def desired_env(home: Path, python: str) -> dict[str, str]:
    return {"TSW_HOME": str(home), "TSW_PYTHON": python}


def merged_settings(
    data: Mapping[str, Any], env_values: Mapping[str, str], rules: Sequence[str]
) -> dict[str, Any]:
    """``data`` with our env keys set and our rules in place (old text of a rule replaced)."""
    result: dict[str, Any] = json.loads(json.dumps(dict(data)))
    _container(result, "env").update(env_values)
    allow = _allow(result)
    for rule in rules:
        name = rule_name(rule)
        at = [i for i, x in enumerate(allow) if isinstance(x, str) and rule_name(x) == name]
        if at:
            allow[at[0]] = rule
            for extra in reversed(at[1:]):
                del allow[extra]
        else:
            allow.append(rule)
    return result


def settings_diff(
    data: Mapping[str, Any], env_values: Mapping[str, str], rules: Sequence[str]
) -> list[dict[str, Any]]:
    """What :func:`apply_user_settings` would change: one entry per key or rule."""
    changes: list[dict[str, Any]] = []
    found_env: Any = data.get("env")
    current_env = cast("dict[str, Any]", found_env) if isinstance(found_env, dict) else {}
    for key, value in env_values.items():
        before = current_env.get(key)
        if before != value:
            changes.append({"key": f"env.{key}", "before": before, "after": value})
    found_auto: Any = data.get("autoMode")
    auto = cast("dict[str, Any]", found_auto) if isinstance(found_auto, dict) else {}
    found_allow: Any = auto.get("allow")
    allow = (
        [x for x in cast("list[Any]", found_allow) if isinstance(x, str)]
        if isinstance(found_allow, list)
        else []
    )
    for rule in rules:
        same = [x for x in allow if rule_name(x) == rule_name(rule)]
        if same != [rule]:
            changes.append(
                {
                    "key": f"autoMode.allow[{rule_name(rule)}]",
                    "before": "missing" if not same else "outdated",
                    "after": "current",
                }
            )
    return changes


def settings_ownership(env: Mapping[str, str]) -> dict[str, Any]:
    """What the user settings held before the install touched them (recorded first, DN009 §5)."""
    data, _ = read_settings(env)
    data = data or {}
    found_env: Any = data.get("env")
    current_env = cast("dict[str, Any]", found_env) if isinstance(found_env, dict) else {}
    found_auto: Any = data.get("autoMode")
    auto = cast("dict[str, Any]", found_auto) if isinstance(found_auto, dict) else {}
    found_allow: Any = auto.get("allow")
    names: set[str] = (
        {rule_name(x) for x in cast("list[Any]", found_allow) if isinstance(x, str)}
        if isinstance(found_allow, list)
        else set()
    )
    return {
        "file": str(settings_path(env)),
        "env_before": {key: current_env.get(key) for key in ENV_KEYS},
        "rules_before": sorted(n for n in RULE_NAMES if n in names),
    }


def _backup(path: Path, backups: Path) -> str | None:
    if not path.is_file():
        return None
    backups.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "").replace(".", "")
    target = backups / f"claude-settings.{stamp}.json"
    shutil.copy2(path, target)
    return str(target)


def _write(path: Path, data: Mapping[str, Any]) -> None:
    text = json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n"
    atomic_write(path, text.encode("utf-8"))


def apply_user_settings(
    env: Mapping[str, str],
    env_values: Mapping[str, str],
    rules: Sequence[str],
    backups: Path,
) -> dict[str, Any]:
    """Write our keys and rules into the user settings after a backup; idempotent."""
    path = settings_path(env)
    data, _ = read_settings(env)
    if data is None:
        from tsw.errors import rejected  # noqa: PLC0415  (only on the error path)

        raise rejected("Claude Code 用户设置不是合法的 JSON，没有修改；请先修正它", path=str(path))
    changes = settings_diff(data, env_values, rules)
    if not changes:
        return {"file": str(path), "changed": [], "backup": None}
    backup = _backup(path, backups)
    _write(path, merged_settings(data, env_values, rules))
    return {"file": str(path), "changed": [c["key"] for c in changes], "backup": backup}


def remove_user_settings(
    env: Mapping[str, str], owned: Mapping[str, Any], backups: Path
) -> dict[str, Any]:
    """Remove what the install added: an env key still holding what it wrote goes back to what
    was there before (absent: deleted); a rule it added (absent before) is deleted."""
    path = settings_path(env)
    data, exists = read_settings(env)
    if not exists or data is None:
        return {"file": str(path), "changed": [], "backup": None}
    result: dict[str, Any] = json.loads(json.dumps(data))
    changed: list[str] = []
    written: Any = owned.get("written_env") or {}
    before: Any = owned.get("env_before") or {}
    found_env: Any = result.get("env")
    if isinstance(found_env, dict):
        current_env = cast("dict[str, Any]", found_env)
        for key, value in cast("dict[str, Any]", written).items():
            if current_env.get(key) != value:
                continue  # changed since: the user's now
            previous: Any = cast("dict[str, Any]", before).get(key)
            if previous is None:
                current_env.pop(key, None)
            else:
                current_env[key] = previous
            changed.append(f"env.{key}")
        if not current_env:
            result.pop("env")
    kept_names = set(cast("list[str]", owned.get("rules_before") or []))
    found_auto: Any = result.get("autoMode")
    if isinstance(found_auto, dict):
        auto = cast("dict[str, Any]", found_auto)
        found_allow: Any = auto.get("allow")
        if isinstance(found_allow, list):
            allow = cast("list[Any]", found_allow)
            ours = {n for n in cast("list[str]", owned.get("rules") or []) if n not in kept_names}
            removed = {rule_name(x) for x in allow if isinstance(x, str) and rule_name(x) in ours}
            remaining = [x for x in allow if not (isinstance(x, str) and rule_name(x) in ours)]
            if removed:
                changed.extend(f"autoMode.allow[{n}]" for n in sorted(removed))
                auto["allow"] = remaining
                if not remaining:
                    auto.pop("allow")
                if not auto:
                    result.pop("autoMode")
    if not changed:
        return {"file": str(path), "changed": [], "backup": None}
    backup = _backup(path, backups)
    _write(path, result)
    return {"file": str(path), "changed": changed, "backup": backup}


def manual_settings_text(env_values: Mapping[str, str], rules: Sequence[str]) -> str:
    """The JSON a user merges by hand when the install may not maintain the settings."""
    return json.dumps(
        {"env": dict(env_values), "autoMode": {"allow": list(rules)}},
        ensure_ascii=False,
        indent=2,
    )
