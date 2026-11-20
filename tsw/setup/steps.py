"""The steps of ``tsw setup`` (DN009 §5, §9, §10; CC009).

Each step compares the current state with the target first and is skipped when they agree, so
``install`` can be run again after a failure, an upgrade or a repair. A step that changes the
user's environment records its ownership in ``install.json`` before changing anything
(:class:`tsw.setup.record.InstallRecord.begin`). Nothing here ever deletes the data directory.

The order: the machine-level items (interpreter and ``claude`` paths, the ``tsw`` shims, the user
``Path``), the plugin (``install_plugin``), the data directory, the user settings
(``apply_user_settings``), ``host render``, the Dashboard service, the workdeck plugin (T309),
then the install-phase ``verify``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from tsw.config import CONFIG_FILE, DEFAULTS, load_config
from tsw.errors import TswError, rejected
from tsw.events import utc_now
from tsw.home import (
    MARKER,
    POINTER,
    Home,
    init_home,
    locate_home,
    open_home,
    read_marker,
    record_product_repo,
    record_render,
    remember_home,
)
from tsw.host import claude_install, dashboard_service, workdeck
from tsw.host.claude_code import AUTO_MODE_RULES, render_instructions
from tsw.host.plugin_copy import update as update_plugin
from tsw.host.version import REPO_ROOT, head, render_record
from tsw.setup import winenv
from tsw.setup.questions import Question, config_differences, config_layer, normalize, questions
from tsw.setup.record import (
    InstallRecord,
    answers_path,
    default_checkout,
    load_answers,
    norm,
    shim_dir,
)
from tsw.storage.fs import FsStorage, atomic_write

STEPS = (
    "machine",
    "shim",
    "path",
    "plugin",
    "home",
    "user_settings",
    "render",
    "dashboard",
    "workdeck",
)
# `workdeck plugin add` restarts the workdeck daemon and waits for it to connect (T309).
WORKDECK_TIMEOUT_SECONDS = 180
MIN_PYTHON = (3, 10)
HANDSHAKE_TIMEOUT_SECONDS = 60
MCP_PROTOCOL = "2025-06-18"
RESTART = (
    "1. 完全退出并重新打开 Claude app（新的用户设置与插件在重新打开后才生效）；"
    "2. 在数据目录 {home} 中新建一个会话，它是主会话；"
    "3. 在会话中输入 /orchestrate 开始主会话（以后每次开主会话都这样）；新环境里主会话先给你导览，"
    "并提议一个示例任务，看 TaskSteward 怎样执行一个任务（可以跳过，之后随时说“开始示例任务”）。"
    "需要时在主会话或新的终端中运行 tsw setup verify --after-reload 核对重新加载之后的结果"
)
DEMO_PROMPT = "开始示例任务"
# How the user starts every main session (DN009 §15.2, M-T308-8): the orchestrate skill.
START_COMMAND = "/orchestrate"


@dataclass
class Context:
    """What every step reads: the environment, the checkout this runtime runs from, the
    interpreter and ``claude`` paths, the (normalized) answers and the install record."""

    env: dict[str, str]
    checkout: Path
    python: str
    claude: str | None
    answers: dict[str, Any]
    problems: list[str]
    record: InstallRecord
    questions: list[Question] = field(default_factory=list[Question])

    @property
    def home_root(self) -> Path | None:
        value = self.answers.get("home") or self.record.get("home")
        return Path(value) if isinstance(value, str) and value else None

    @property
    def shims(self) -> Path:
        return shim_dir(self.env)

    @property
    def backups(self) -> Path:
        return dashboard_service.state_dir(self.env) / "backups"

    def desired_env(self) -> dict[str, str]:
        root = self.home_root
        return claude_install.desired_env(root or Path(), self.python)


def context(
    env: Mapping[str, str],
    *,
    answers_file: str | None = None,
    answers: Mapping[str, Any] | None = None,
    python: str | None = None,
    claude: str | None = None,
    require_answers: bool = False,
) -> Context:
    """Build the context. Answers come from ``answers``, ``--answers <file>``, or the saved
    answers file; with ``require_answers`` a missing data directory is a problem (DN009 §6)."""
    environ = dict(env)
    record = InstallRecord.load(environ)
    checkout = REPO_ROOT
    qs = questions(environ, checkout)
    raw: dict[str, Any] = {}
    if answers is not None:
        raw = dict(answers)
    elif answers_file:
        raw = load_answers(Path(answers_file))
    elif answers_path(environ).is_file():
        raw = load_answers(answers_path(environ))
    if answers is None and raw:
        # An answers file written before the workdeck question (T309), saved or given with
        # --answers: the install does not add the plugin on the user's behalf; they answer the
        # question in an interactive run (review T309 r1 F1).
        raw.setdefault("workdeck", False)
    normalized, problems = normalize(raw, qs) if (raw or require_answers) else ({}, [])
    if not raw and not require_answers:
        problems = []
    given = normalized.get("checkout")
    if given and norm(given) != norm(checkout):
        problems.append(
            f"checkout：答案中的检出 {given} 不是运行 tsw setup 的检出 {checkout}；"
            "请用那个检出中的 tsw 运行"
        )
    chosen_python = python or sys.executable
    recorded_claude = record.get("claude")
    found_claude = claude_install.find_claude(environ, claude) or (
        recorded_claude
        if isinstance(recorded_claude, str) and Path(recorded_claude).is_file()
        else None
    )
    return Context(
        env=environ,
        checkout=checkout,
        python=chosen_python,
        claude=found_claude,
        answers=normalized,
        problems=problems,
        record=record,
        questions=qs,
    )


# ---------------------------------------------------------------------- check


def python_version(python: str) -> tuple[int, int] | None:
    try:
        proc = subprocess.run(
            [python, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = proc.stdout.split()
    if proc.returncode != 0 or len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def home_state(root: Path | None) -> str:
    """``missing_answer``, ``existing`` (a data directory), ``new`` (absent or empty) or
    ``not_empty`` (a folder with other things in it: refused, as ``tsw init`` does)."""
    if root is None:
        return "missing_answer"
    if (root / MARKER).is_file():
        return "existing"
    if not root.exists() or (root.is_dir() and not any(root.iterdir())):
        return "new"
    return "not_empty"


def check(ctx: Context, *, with_questions: bool = False) -> dict[str, Any]:
    """Read-only: prerequisites, an existing install and data directory, the host's state."""
    version = python_version(ctx.python)
    found_head = head(ctx.checkout)
    root = ctx.home_root
    state = home_state(root)
    problems = list(ctx.problems)
    if version is None or version < MIN_PYTHON:
        problems.append(f"python：需要 Python 3.10 以上（{ctx.python}）")
    if shutil.which("git", path=ctx.env.get("PATH")) is None:
        problems.append("git：找不到 git 命令")
    if ctx.claude is None:
        problems.append(
            "claude：找不到 Claude Code 的命令行（先查 PATH，再查 %LOCALAPPDATA%\\Programs\\"
            "ClaudeCode\\claude.exe）；用 --claude 指定，或先安装 Claude Code"
        )
    if state == "not_empty":
        problems.append(f"home：{root} 不是空目录，也不是 TaskSteward 数据目录")
    if state == "missing_answer":
        problems.append("home：还没有给出数据目录位置（必填，没有默认值）")
    if root is not None and _inside(root, ctx.checkout):
        problems.append("home：数据目录不能放在产品检出之中")
    plugin = claude_install.plugin_state(ctx.env, ctx.checkout)
    value, _ = _user_path(ctx.env)
    result: dict[str, Any] = {
        "checkout": {
            "path": str(ctx.checkout),
            "default": str(default_checkout(ctx.env)),
            "head": found_head.get("commit") if found_head else None,
            "dirty": found_head.get("dirty") if found_head else None,
        },
        "python": {"path": ctx.python, "version": ".".join(map(str, version or ()))},
        "claude": ctx.claude,
        "home": {"path": str(root) if root else None, "state": state},
        "record": {
            "path": str(ctx.record.path),
            "exists": ctx.record.exists,
            "status": ctx.record.get("status"),
        },
        "answers_file": str(answers_path(ctx.env)),
        "answers": ctx.answers,
        "plugin": {k: plugin[k] for k in ("marketplace", "points_at_checkout", "installed")},
        "shims": str(ctx.shims),
        "path_has_shims": winenv.has_entry(value, ctx.shims) if value is not None else False,
        "problems": problems,
        # Not "ok": check only reports, so a problem is not a failure of the command.
        "ready": not problems,
    }
    if with_questions:
        result["questions"] = [q.to_dict() for q in ctx.questions]
    return result


def _inside(path: Path, root: Path) -> bool:
    a, b = norm(path), norm(root)
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def _user_path(env: Mapping[str, str]) -> tuple[str | None, int]:
    try:
        return winenv.read_user_path(env)
    except TswError:
        return None, winenv.EXPAND


# ---------------------------------------------------------------------- steps


Step = Callable[[Context, bool], dict[str, Any]]


def _machine(ctx: Context, apply: bool) -> dict[str, Any]:
    wanted = {
        "checkout": str(ctx.checkout),
        "python": ctx.python,
        "claude": ctx.claude,
        "host": ctx.answers.get("host", "claude-code"),
        "home": str(ctx.home_root) if ctx.home_root else None,
    }
    differs = [key for key, value in wanted.items() if ctx.record.get(key) != value]
    if apply and differs:
        ctx.record.set(**wanted)
    return {"state": "todo" if differs else "ok", "record": wanted, "changes": differs}


def _shim(ctx: Context, apply: bool) -> dict[str, Any]:
    states = winenv.shim_state(ctx.shims, ctx.python, ctx.checkout)
    todo = [name for name, state in states.items() if state != "ok"]
    result: dict[str, Any] = {
        "state": "todo" if todo else "ok",
        "dir": str(ctx.shims),
        "files": states,
    }
    if apply and todo:
        ctx.record.begin("shim", {"dir": str(ctx.shims), "created_dir": not ctx.shims.exists()})
        written = winenv.write_shims(ctx.shims, ctx.python, ctx.checkout)
        ctx.record.finish("shim", files=["tsw.cmd", "tsw"], written=written)
        result["written"] = written
    elif apply and not ctx.record.step("shim"):
        ctx.record.begin("shim", {"dir": str(ctx.shims), "created_dir": False})
        ctx.record.finish("shim", files=["tsw.cmd", "tsw"], written=[])
    return result


def _path(ctx: Context, apply: bool) -> dict[str, Any]:
    value, kind = winenv.read_user_path(ctx.env)
    present = winenv.has_entry(value, ctx.shims)
    result: dict[str, Any] = {
        "state": "ok" if present else "todo",
        "entry": str(ctx.shims),
        "scope": "user",
    }
    if apply:
        ctx.record.begin("path", {"entry": str(ctx.shims), "added": not present})
        if not present:
            winenv.write_user_path(ctx.env, winenv.with_entry(value, ctx.shims), kind)
            result["added"] = True
        ctx.record.finish("path")
    return result


def _plugin(ctx: Context, apply: bool) -> dict[str, Any]:
    state = claude_install.plugin_state(ctx.env, ctx.checkout)
    planned = claude_install.plugin_actions(state, ctx.checkout)
    result: dict[str, Any] = {
        "state": "todo" if planned else "ok",
        "actions": planned,
        "marketplace": state["marketplace"],
    }
    if apply:
        # Ownership first, even with nothing to do: an uninstall leaves alone what was the
        # user's before (DN009 §10, T308 review r1 F2).
        ctx.record.begin(
            "plugin",
            {
                "marketplace_before": state["marketplace"],
                "installed_before": state["installed"],
            },
        )
    if apply and not planned:
        ctx.record.finish("plugin")
    if apply and planned:
        if ctx.claude is None:
            raise rejected("找不到 claude 命令行，不能安装插件", reason="claude_missing")
        report = claude_install.install_plugin(ctx.env, ctx.claude, ctx.checkout)
        if not report["ok"]:
            raise rejected(
                "插件没有装好：市场没有指向检出或插件没有安装",
                reason="plugin_failed",
                report=report,
            )
        ctx.record.finish("plugin", after=report["after"])
        result["report"] = report
    return result


def _home(ctx: Context, apply: bool) -> dict[str, Any]:
    root = ctx.home_root
    if root is None:
        raise rejected("没有数据目录位置（必填）", reason="home_required")
    state = home_state(root)
    if state == "not_empty":
        raise rejected(
            "目录非空且不是 TaskSteward 数据目录", reason="home_not_empty", path=str(root)
        )
    pointer = ctx.checkout / POINTER
    remembered = pointer.read_text(encoding="utf-8").strip() if pointer.is_file() else None
    marker = read_marker(root) if state == "existing" else {}
    todo = (
        state != "existing"
        or marker.get("product_repo") != str(ctx.checkout.resolve())
        or remembered is None
        or norm(remembered) != norm(root)
    )
    result: dict[str, Any] = {"state": "todo" if todo else "ok", "home": str(root), "kind": state}
    if state == "existing":
        config = load_config(open_home(root))
        result["config_differences"] = config_differences(config.values, ctx.answers)
        if not config.valid:
            result["config_problems"] = config.problems
    if not apply:
        if state == "new":
            result["config"] = config_layer(ctx.answers)
        return result
    entry = ctx.record.begin("home", {"home": str(root), "created": state == "new"})
    home, _ = init_home(root)
    record_product_repo(home, ctx.checkout)
    remember_home(ctx.checkout, home.root)
    written = False
    if entry.get("created") and not home.path(CONFIG_FILE).is_file():
        layer = config_layer(ctx.answers)
        config = load_config(home, main_layer=layer)
        if not config.valid:
            raise rejected(
                "按答案生成的配置不合法", reason="config_invalid", problems=config.problems
            )
        text = json.dumps(layer, ensure_ascii=False, indent=2) + "\n"
        atomic_write(home.path(CONFIG_FILE), text.encode("utf-8"))
        written = True
    config = load_config(home)
    ctx.record.finish(
        "home", config_written=written or ctx.record.step("home").get("config_written")
    )
    result["config_written"] = written
    result["config_valid"] = config.valid
    if not config.valid:
        result["config_problems"] = config.problems
    return result


def _user_settings(ctx: Context, apply: bool) -> dict[str, Any]:
    values = ctx.desired_env()
    path = claude_install.settings_path(ctx.env)
    if not ctx.answers.get("user_settings", True):
        return {
            "state": "manual",
            "file": str(path),
            "manual": claude_install.manual_settings_text(values, AUTO_MODE_RULES),
            "note": "你选择了不由安装脚本维护用户设置：把这段 JSON 合并进上面的文件（与已有的 env、"
            "autoMode.allow 合并）",
        }
    data, _ = claude_install.read_settings(ctx.env)
    if data is None:
        raise rejected("Claude Code 用户设置不是合法的 JSON，没有修改", path=str(path))
    diff = claude_install.settings_diff(data, values, AUTO_MODE_RULES)
    result: dict[str, Any] = {"state": "todo" if diff else "ok", "file": str(path), "diff": diff}
    if apply and diff:
        ctx.record.begin(
            "user_settings",
            {
                **claude_install.settings_ownership(ctx.env),
                "rules": list(claude_install.RULE_NAMES),
            },
        )
        # What this run writes: a removal only takes back values still equal to it.
        entry = ctx.record.step("user_settings")
        entry["written_env"] = values
        ctx.record.save()
        report = claude_install.apply_user_settings(ctx.env, values, AUTO_MODE_RULES, ctx.backups)
        backups = [*ctx.record.step("user_settings").get("backups", []), report["backup"]]
        ctx.record.finish("user_settings", backups=[b for b in backups if b])
        result["report"] = report
    return result


def _render(ctx: Context, apply: bool) -> dict[str, Any]:
    root = ctx.home_root
    found = head(ctx.checkout)
    rendered: Any = None
    if root is not None and (root / MARKER).is_file():
        rendered = read_marker(root).get("rendered")
    commit: Any = (
        cast("dict[str, Any]", rendered).get("commit") if isinstance(rendered, dict) else None
    )
    files_ok = root is not None and (root / "CLAUDE.md").is_file()
    plugin = claude_install.plugin_state(ctx.env, ctx.checkout)
    todo = bool(not files_ok or commit != (found or {}).get("commit") or not plugin["current"])
    result: dict[str, Any] = {"state": "todo" if todo else "ok", "rendered": commit}
    if apply and todo and root is not None:
        home = open_home(root)
        with FsStorage(home).writer():
            written = render_instructions(home)
            record_render(home, render_record(ctx.checkout))
        report = update_plugin(ctx.env)
        result["written"] = len(written)
        result["plugin"] = {
            k: report.get(k) for k in ("update", "next", "note") if report.get(k) is not None
        }
        ctx.record.finish("render", commit=(found or {}).get("commit"))
    return result


def _dashboard(ctx: Context, apply: bool, *, open_browser: bool = True) -> dict[str, Any]:
    if not ctx.answers.get("dashboard"):
        return {"state": "skipped", "note": "你选择了不安装 Dashboard 常驻服务"}
    try:
        dashboard_service.scheduler(ctx.env)
    except TswError as exc:
        return {"state": "skipped", "note": exc.message}
    root = ctx.home_root
    if root is None or not (root / MARKER).is_file():
        return {"state": "todo", "note": "数据目录建好之后安装"}
    home = open_home(root)
    key = dashboard_service.home_key(home)
    installed = dashboard_service.installed(ctx.env, key)
    result: dict[str, Any] = {
        "state": "ok" if installed else "todo",
        "task": dashboard_service.task_name(key),
    }
    entry = ctx.record.step("dashboard")
    interrupted = entry.get("state") == "started" and not entry.get("installed")
    # Interrupted after the task was created: it is ours unless it was there before.
    if apply and installed and interrupted and not entry.get("installed_before"):
        # The task may have been created but never started: start it and wait for it as the
        # install would have (T308 review r2 F8), then record the step as done.
        port = _port(home)
        restarted = dashboard_service.restart(home, ctx.env, port)
        ctx.record.finish("dashboard", installed=True, port=port)
        result["recovered"] = True
        result["running"] = restarted.get("running")
    if apply and not installed:
        ctx.record.begin(
            "dashboard",
            {"task": dashboard_service.task_name(key), "installed_before": installed},
        )
        port = _port(home)
        report = dashboard_service.install(home, ctx.env, port)
        opened = False
        if open_browser:
            webbrowser.open(dashboard_service.open_url(home, ctx.env, port))
            opened = True
        ctx.record.finish("dashboard", installed=True, port=port)
        result["installed"] = {k: report.get(k) for k in ("task", "running", "url")}
        result["opened"] = opened
    return result


def _workdeck_command(env: Mapping[str, str]) -> str | None:
    return shutil.which("workdeck", path=env.get("PATH"))


def _run_workdeck(command: str, env: Mapping[str, str], *args: str) -> tuple[int, str]:
    """Run ``workdeck <args>``: its exit code and output (the platform's, CC006 §13)."""
    try:
        proc = subprocess.run(
            [command, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env),
            timeout=WORKDECK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _workdeck_entries(command: str, env: Mapping[str, str]) -> dict[str, str] | None:
    """The configured plugins ``workdeck plugin list`` shows, id -> what it starts or reaches
    (``<id>\\t<name>\\t<kind>\\t<upstream>`` lines), or None when the command fails."""
    code, output = _run_workdeck(command, env, "plugin", "list")
    if code != 0:
        return None
    found: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            found[parts[0].strip()] = parts[3].strip()
    return found


def _tail(output: str, lines: int = 8) -> list[str]:
    return [line for line in output.splitlines() if line.strip()][-lines:]


def _workdeck(ctx: Context, apply: bool) -> dict[str, Any]:
    """Add the Dashboard to the local workdeck portal (DN009 §5 step 7, CC006 §13; T309).

    Skipped when the recorded manifest is the one wanted now. workdeck refusing or failing is
    reported with the command to run by hand; the other steps are not affected.
    """
    if not ctx.answers.get("workdeck"):
        return {"state": "skipped", "note": "你选择了不接入 workdeck（或本机没有 workdeck 命令）"}
    command = _workdeck_command(ctx.env)
    if command is None:
        return {"state": "skipped", "note": "找不到 workdeck 命令"}
    root = ctx.home_root
    if root is None:
        return {"state": "todo", "note": "还没有给出数据目录位置"}
    # The manifest needs only the data directory's path: the plan before the directory exists
    # shows what will be started and replaced as well (PR #65 review).
    value = workdeck.manifest(root, python=ctx.python, checkout=ctx.checkout)
    wanted = workdeck.digest(value)
    plugin_id = str(value["id"])
    entry = ctx.record.step("workdeck")
    if entry.get("added") and entry.get("digest") == wanted and not entry.get("failed_at"):
        return {"state": "ok", "id": plugin_id}
    entries = _workdeck_entries(command, ctx.env)
    existed = None if entries is None else plugin_id in entries
    starts = " ".join([value["spawn"]["command"], *value["spawn"]["args"]])
    replacing = {
        True: f"；替换 workdeck 中已有的插件 {plugin_id}",
        None: "；读不到 workdeck 的插件列表",
    }
    result: dict[str, Any] = {
        "state": "todo",
        "id": plugin_id,
        "starts": starts,
        "replaces": existed,
        "actions": [
            f"workdeck plugin add <manifest> --replace（插件 {plugin_id} 启动：{starts}"
            f"{replacing.get(existed, '')}）"
        ],
    }
    if not apply:
        return result
    ctx.record.begin("workdeck", {"id": plugin_id, "existed_before": existed})
    with tempfile.TemporaryDirectory(prefix="tsw-workdeck-") as folder:
        path = Path(folder) / "tsw-workdeck-plugin.json"
        path.write_text(workdeck.to_json(value), encoding="utf-8", newline="\n")
        code, output = _run_workdeck(command, ctx.env, "plugin", "add", str(path), "--replace")
    if code != 0:
        # workdeck writes its configuration before it restarts the daemon (review T309 r1
        # F2): an add that failed afterwards still left our entry, which uninstall must remove.
        after = _workdeck_entries(command, ctx.env) or {}
        ours = after.get(plugin_id) == workdeck.describe(value)
        ctx.record.finish(
            "workdeck",
            added=ours or bool(entry.get("added")),
            digest=wanted if ours else entry.get("digest"),
            failed_at=utc_now(),
        )
        result.update(
            state="failed",
            problem=f"workdeck plugin add 失败（退出码 {code}）",
            output=_tail(output),
            manual="修正原因后重跑 tsw setup install；或在终端运行 tsw dashboard "
            "workdeck-manifest --out <文件>，再 workdeck plugin add <文件> --replace",
        )
        return result
    if (
        entry.get("failed_at")
        and "unchanged" in output
        and not _workdeck_running(command, ctx.env, plugin_id)
    ):
        # The entry was written before the restart failed; workdeck does not restart for an
        # unchanged entry, so the plugin still is not running (review T309 r2 F3).
        ctx.record.finish("workdeck", added=True, digest=wanted, failed_at=utc_now())
        result.update(
            state="failed",
            problem="workdeck 的配置中已有这个插件，但 workdeck daemon 没有在运行它"
            "（上次重启失败）",
            output=_tail(output),
            manual="重启 workdeck daemon（workdeck setup verify 查看原因）后重跑 tsw setup install",
        )
        return result
    ctx.record.finish("workdeck", added=True, digest=wanted, failed_at=None)
    result.update(state="done", output=_tail(output))
    return result


def _workdeck_running(command: str, env: Mapping[str, str], plugin_id: str) -> bool:
    """Whether ``workdeck plugin list`` says the daemon runs the plugin: ``running: <id> ready``."""
    code, output = _run_workdeck(command, env, "plugin", "list")
    return code == 0 and f"running: {plugin_id} ready" in output.splitlines()


def _port(home: Home) -> int:
    config = load_config(home)
    if not config.valid:
        return int(DEFAULTS["dashboard"]["port"])
    return int(config.get("dashboard.port"))


STEP_FUNCTIONS: dict[str, Step] = {
    "machine": _machine,
    "shim": _shim,
    "path": _path,
    "plugin": _plugin,
    "home": _home,
    "user_settings": _user_settings,
    "render": _render,
}


def _run_step(ctx: Context, name: str, apply: bool, *, open_browser: bool = True) -> dict[str, Any]:
    try:
        if name == "dashboard":
            outcome = _dashboard(ctx, apply, open_browser=open_browser)
        elif name == "workdeck":
            outcome = _workdeck(ctx, apply)
        else:
            outcome = STEP_FUNCTIONS[name](ctx, apply)
    except TswError as exc:
        if not apply:
            return {"step": name, "state": "blocked", "problem": exc.message, **exc.details}
        exc.details.setdefault("step", name)
        raise
    except OSError as exc:
        if not apply:
            return {"step": name, "state": "blocked", "problem": str(exc)}
        raise rejected(f"第 {name} 步失败：{exc}", reason="setup_step_failed", step=name) from exc
    return {"step": name, **outcome}


# ---------------------------------------------------------------------- plan / install


def plan(ctx: Context) -> dict[str, Any]:
    """Read-only: what ``install`` would do, with the user-settings and ``Path`` changes."""
    checked = check(ctx)
    steps = [_run_step(ctx, name, apply=False) for name in STEPS]
    return {
        "checkout": checked["checkout"],
        "home": checked["home"],
        "answers": ctx.answers,
        "steps": steps,
        "todo": [s["step"] for s in steps if s.get("state") in ("todo", "blocked")],
        "problems": checked["problems"],
        "ok": checked["ready"],
        "next": "确认后运行 tsw setup install（终端入口与会话入口会先请你确认）"
        if checked["ready"]
        else "先解决 problems 中的问题",
    }


def install(ctx: Context, *, open_browser: bool = True) -> dict[str, Any]:
    checked = check(ctx)
    if checked["problems"]:
        raise rejected(
            "安装的前提不满足", reason="setup_preconditions", problems=checked["problems"]
        )
    installer = _installer_session(ctx)
    # Recorded before anything changes: a rerun in the same session after a failure, when the
    # session's folder is by then the data directory, is still the installer (review r6 F1).
    ctx.record.set(status="installing", started_at=utc_now(), installer_session=installer)
    results: list[dict[str, Any]] = []
    stood_down: str | None = None
    for name in STEPS:
        results.append(_run_step(ctx, name, apply=True, open_browser=open_browser))
        if name == "home":
            # As soon as the data directory exists: a later step that fails leaves no session
            # that would take the lease at its next start.
            stood_down = _stand_down_installer(ctx, installer)
    ctx.record.set(status="installed", completed_at=utc_now())
    verification = verify(ctx)
    root = ctx.home_root
    out: dict[str, Any] = {
        "installed": True,
        "steps": results,
        "verify": verification,
        "record": str(ctx.record.path),
        "next": RESTART.format(home=root),
        "start_command": START_COMMAND,
        "demo_prompt": DEMO_PROMPT,
        "repositories": "要管理的仓库不在安装中配置：重启后在主会话中告诉它要管理哪些仓库，由主会话"
        "逐个加入配置（DN009 §6.1）",
    }
    if stood_down:
        out["install_session"] = {
            "session_id": stood_down,
            "note": "本会话（安装会话）记为让出租约：之后它在会话开始时不自动领取调度租约；"
            "请在数据目录中新建会话作为主会话（DN009 §15.1）",
        }
    if not verification["ok"]:
        out["ok"] = False
    return out


def _installer_session(ctx: Context) -> str | None:
    """The session this install runs in when it is bound to no data directory (role none): it
    stands down after the install (DN009 §15.1). The main session rerunning the install, and a
    terminal, are not recorded."""
    from tsw.host.identity import SESSION_ENV  # noqa: PLC0415
    from tsw.host.identity import resolve as resolve_identity  # noqa: PLC0415
    from tsw.ledger import Ledger  # noqa: PLC0415

    session = ctx.env.get(SESSION_ENV)
    root = ctx.home_root
    if not session:
        return None
    if ctx.record.get("installer_session") == session:
        return session  # the same install again, after a failure or as a repair
    if root is None or not (root / MARKER).is_file():
        return session
    home = open_home(root)
    with Ledger(home).view() as conn:
        who = resolve_identity(conn, home, ctx.env, Path.cwd())
    return session if who.role == "none" and not who.pending else None


def _stand_down_installer(ctx: Context, session: str | None) -> str | None:
    if session is None or ctx.home_root is None:
        return None
    from tsw import lease  # noqa: PLC0415
    from tsw.ledger import Ledger  # noqa: PLC0415

    home = open_home(ctx.home_root)
    ledger = Ledger(home)
    with ledger.view() as conn:
        held = lease.lease_row(conn)
        if lease.stood_down(conn, session) is not None:
            return session
        if held is not None and held["holder_session"] == session:
            return None
    ledger.commit(
        lease.stand_down(session, machine=lease.this_machine(), via="setup", actor={"role": "cli"})
    )
    return session


# ---------------------------------------------------------------------- verify


def _check(
    name: str, ok: bool | None, detail: Any = None, *, pending: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {"check": name, "ok": ok}
    if pending:
        out["pending_reload"] = True
    if detail is not None:
        out["detail"] = detail
    return out


def handshake(python: str, checkout: Path, home: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Start the tsw MCP server without the user's site-packages and complete ``initialize``."""
    launcher = checkout / "plugin" / "bin" / "tsw_launch.py"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "tsw-setup-verify", "version": "1"},
        },
    }
    child = {k: v for k, v in env.items() if k not in ("TSW_REPO", "PYTHONPATH")}
    child["TSW_HOME"] = str(home)
    try:
        proc = subprocess.run(
            [python, "-s", "-X", "utf8", str(launcher), "mcp"],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child,
            cwd=str(home),
            timeout=HANDSHAKE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    for line in proc.stdout.splitlines():
        try:
            reply: Any = json.loads(line)
        except ValueError:
            continue
        message = cast("dict[str, Any]", reply) if isinstance(reply, dict) else {}
        result: Any = message.get("result") if message.get("id") == 1 else None
        if isinstance(result, dict):
            info: Any = cast("dict[str, Any]", result).get("serverInfo")
            return {"ok": True, "server": info}
    tail = [line for line in proc.stderr.splitlines() if line.strip()][-5:]
    return {"ok": False, "returncode": proc.returncode, "stderr": tail}


def verify(
    ctx: Context, *, after_reload: bool = False, running: Sequence[str] = ()
) -> dict[str, Any]:
    """Read-only: the install phase checks persisted state; ``after_reload`` also checks what
    only a reload makes visible (DN009 §9). Items that need a reload are ``pending_reload``."""
    checks: list[dict[str, Any]] = []
    root = ctx.home_root
    python = str(ctx.record.get("python") or ctx.python)
    states = winenv.shim_state(ctx.shims, python, ctx.checkout)
    checks.append(_check("shims", all(s == "ok" for s in states.values()), states))
    value, _ = _user_path(ctx.env)
    checks.append(_check("path_persisted", winenv.has_entry(value, ctx.shims), str(ctx.shims)))
    plugin = claude_install.plugin_state(ctx.env, ctx.checkout)
    checks.append(_check("plugin_source", plugin["points_at_checkout"], plugin["marketplace"]))
    checks.append(
        _check(
            "plugin_copy",
            plugin["current"],
            [
                {k: c.get(k) for k in ("version", "current", "differs") if k in c}
                for c in plugin["copies"]
            ],
        )
    )
    if root is None or not (root / MARKER).is_file():
        checks.append(_check("home", False, str(root) if root else None))
    else:
        located, how = locate_home(
            {k: v for k, v in ctx.env.items() if k != "TSW_HOME"},
            repo_root=ctx.checkout,
            cwd=ctx.checkout,
        )
        checks.append(
            _check("home", norm(located) == norm(root), {"found": str(located), "by": how})
        )
        config = load_config(open_home(root))
        checks.append(_check("config_valid", config.valid, config.problems or None))
        if ctx.answers.get("user_settings", ctx.record.step("user_settings") != {}):
            data, _ = claude_install.read_settings(ctx.env)
            diff = claude_install.settings_diff(data or {}, ctx.desired_env(), AUTO_MODE_RULES)
            checks.append(_check("user_settings", not diff, diff or None))
        else:
            checks.append(_check("user_settings", None, "由你手工维护（未核对）"))
        if ctx.record.step("dashboard").get("installed"):
            home = open_home(root)
            status = dashboard_service.status(home, ctx.env, _port(home))
            checks.append(
                _check(
                    "dashboard",
                    bool(status.get("installed") and status.get("running")),
                    {k: status.get(k) for k in ("installed", "running", "url")},
                )
            )
        wired = ctx.record.step("workdeck")
        if wired.get("added") or wired.get("failed_at"):
            current = workdeck.digest(workdeck.manifest(root, python=python, checkout=ctx.checkout))
            checks.append(
                _check(
                    "workdeck",
                    bool(wired.get("added"))
                    and wired.get("digest") == current
                    and not wired.get("failed_at"),
                    {"id": wired.get("id"), "recorded": wired.get("digest"), "current": current},
                )
            )
        checks.append(_check("runtime_handshake", **_handshake_check(python, ctx, root)))
    on_path = shutil.which("tsw", path=ctx.env.get("PATH"))
    effective = on_path is not None and _inside(Path(on_path), ctx.shims)
    detail: Any = on_path
    if on_path is not None and not effective:
        # A developer's pip install (Python's Scripts) or another copy comes first (T308 live).
        detail = {
            "found": on_path,
            "note": "PATH 中先找到的 tsw 不是安装脚本的垫片：删除另一个 tsw（例如 pip uninstall "
            "task-steward），或把垫片目录移到 PATH 的前面",
        }
    checks.append(
        _check(
            "path_effective",
            effective if after_reload else (True if effective else None),
            detail,
            pending=not effective and not after_reload,
        )
    )
    if after_reload and root is not None and (root / MARKER).is_file():
        checks.append(_update_plan_check(ctx, root, running))
    failed = [c["check"] for c in checks if c["ok"] is False]
    pending = [c["check"] for c in checks if c.get("pending_reload")]
    return {
        "phase": "after_reload" if after_reload else "install",
        "checks": checks,
        "failed": failed,
        "pending_reload": pending,
        "ok": not failed,
    }


def _handshake_check(python: str, ctx: Context, root: Path) -> dict[str, Any]:
    result = handshake(python, ctx.checkout, root, ctx.env)
    return {"ok": bool(result.get("ok")), "detail": result}


def _update_plan_check(ctx: Context, root: Path, running: Sequence[str]) -> dict[str, Any]:
    from tsw.host.update_plan import build  # noqa: PLC0415  (only after a reload)
    from tsw.ledger import Ledger  # noqa: PLC0415

    home = open_home(root)
    config = load_config(home)
    port = _port(home)
    try:
        with Ledger(home).view() as conn:
            planned = build(
                home,
                env=ctx.env,
                config=config.values if config.valid else DEFAULTS,
                conn=conn,
                running=list(running),
                repo=ctx.checkout,
                dashboard=dashboard_service.status(home, ctx.env, port),
            )
    except TswError as exc:
        return _check("update_plan", False, {"error": exc.message, **exc.details})
    return _check(
        "update_plan",
        not planned["pending"],
        {"pending": planned["pending"], "to": planned["to"], "running": list(running)},
    )


# ---------------------------------------------------------------------- uninstall


def uninstall(ctx: Context) -> dict[str, Any]:
    """Remove what the install record says the install added (DN009 §10); never the data
    directory. Without an install record there is nothing of ours to remove."""
    if not ctx.record.exists:
        raise rejected(
            "没有安装记录（install.json）：没有由 tsw setup 安装的内容可卸载",
            reason="not_installed",
            record=str(ctx.record.path),
        )
    done: dict[str, Any] = {}
    root = ctx.home_root
    board = ctx.record.step("dashboard")
    ours = board.get("installed") or (
        board.get("state") == "started" and not board.get("installed_before")
    )
    if ours and root and (root / MARKER).is_file():
        done["dashboard"] = dashboard_service.uninstall(open_home(root), ctx.env)
        ctx.record.finish("dashboard", installed=False)
    wired = ctx.record.step("workdeck")
    if wired.get("added") or wired.get("state") == "started":
        done["workdeck"] = _remove_workdeck(ctx, wired)
    plugin = ctx.record.step("plugin")
    if not plugin:
        done["plugin"] = {"ok": True, "note": "安装没有动过插件，保留"}
    elif ctx.claude is not None:
        done["plugin"] = claude_install.remove_plugin(ctx.env, ctx.claude, plugin)
    else:
        done["plugin"] = {"ok": False, "note": "找不到 claude 命令行，插件没有卸载"}
    if not done["plugin"].get("ok"):
        # Stop before the settings, the shims and Path: the user keeps a working tsw and the
        # record to run the uninstall again once the cause is fixed (T308 review r3 F9).
        ctx.record.set(status="uninstalling")
        raise rejected(
            "插件没有恢复到安装前的状态，卸载停在这一步（用户设置、垫片与 PATH 未动）；"
            "按 plugin 中的输出修正原因（例如网络）后重新运行 tsw setup uninstall",
            reason="uninstall_incomplete",
            step="plugin",
            removed=done,
        )
    owned = ctx.record.step("user_settings")
    if owned:
        done["user_settings"] = claude_install.remove_user_settings(ctx.env, owned, ctx.backups)
    shims = Path(str(ctx.record.step("shim").get("dir") or ctx.shims))
    done["shims"] = winenv.remove_shims(shims, ctx.env)
    path_entry = ctx.record.step("path")
    if path_entry.get("added"):
        value, kind = winenv.read_user_path(ctx.env)
        if winenv.has_entry(value, shims):
            winenv.write_user_path(ctx.env, winenv.without_entry(value, shims), kind)
            done["path"] = {"removed": str(shims)}
    ctx.record.set(status="uninstalled", uninstalled_at=utc_now())
    return {
        "uninstalled": True,
        "removed": done,
        "kept": {
            "home": str(root) if root else None,
            "checkout": str(ctx.checkout),
        },
        "note": "数据目录不会被删除（账本是你的数据）：需要"
        "时先 tsw backup <空目录> 备份；产品检出由你"
        "自己删除。重启 Claude app 后插件与规则不再生效",
    }


def _remove_workdeck(ctx: Context, wired: Mapping[str, Any]) -> dict[str, Any]:
    """``workdeck plugin remove <id>`` for the entry the install added (DN009 §10); an entry it
    replaced is not brought back (it is in workdeck's backup). Removed only while workdeck still
    has our entry: an entry the user changed since is kept, and an add interrupted after
    workdeck wrote it is removed too. A failure is reported, not fatal."""
    plugin_id = str(wired.get("id") or workdeck.DEFAULT_ID)
    command = _workdeck_command(ctx.env)
    if command is None:
        return {"ok": False, "note": f"找不到 workdeck 命令，插件 {plugin_id} 没有移除"}
    entries = _workdeck_entries(command, ctx.env)
    root = ctx.home_root
    if entries is None or root is None:
        # Without the list there is nothing to tell our entry from one the user put there
        # since: keep it and the record, and say how to finish (review T309 r2 F4).
        return {
            "ok": False,
            "id": plugin_id,
            "note": "读不到 workdeck 的插件列表，插件没有移除；确认它仍是安装加入的那一项后运行 "
            f"workdeck plugin remove {plugin_id}",
        }
    python = str(ctx.record.get("python") or ctx.python)
    mine = workdeck.describe(workdeck.manifest(root, python=python, checkout=ctx.checkout))
    if plugin_id not in entries:
        ctx.record.finish("workdeck", added=False)
        return {"ok": True, "id": plugin_id, "note": "workdeck 中已没有这个插件"}
    if entries[plugin_id] != mine:
        ctx.record.finish("workdeck", added=False)
        return {
            "ok": True,
            "id": plugin_id,
            "note": "workdeck 中的这个插件已不是安装加入的那一项，保留",
        }
    code, output = _run_workdeck(command, ctx.env, "plugin", "remove", plugin_id)
    if code == 0:
        ctx.record.finish("workdeck", added=False)
    return {"ok": code == 0, "id": plugin_id, "output": _tail(output)}


def manual_steps(home_text: str) -> list[str]:
    """The steps a manual install takes, the same as ``install``'s (``host install-hint``)."""
    return [
        "推荐：在终端运行产品检出中的 setup.ps1，或运行 tsw setup install --interactive；"
        "它按下面的步骤做完并核对（tsw setup plan 只读列出要做的事）。手工安装时依次：",
        "1. 机器级安装项：把 tsw 垫片（tsw.cmd 与无扩展名的 sh 脚本，运行检出中的 "
        "plugin/bin/tsw_launch.py）放进一个目录，并把该目录加入用户级 PATH；开发者也可以在产品"
        "检出中运行 python -m pip install -e .",
        "2. 插件：claude plugin marketplace add <产品检出>，再 claude plugin install "
        "task-steward@task-steward（同名市场已从别处添加时，前一条命令把它改指检出）",
        f"3. 数据目录：tsw init --home {home_text} --remember",
        "4. 用户设置（~/.claude/settings.json，设置了 C"
        "LAUDE_CONFIG_DIR 时为其中的 settings.json）："
        "env.TSW_HOME 为数据目录、env.TSW_PYTHON 为解释器的完整路径，autoMode.allow 中加入下面的"
        "三条 auto 模式规则（与已有的合并；同名的旧规则换成新文本）",
        "5. tsw host render：生成数据目录的 CLAUDE.md、skill、worker 说明 roles/worker.md 与设置，"
        "并更新插件副本",
        "6. 可选：tsw dashboard install，再 tsw dashboard open",
        "7. 可选（本机有 workdeck 时）：workdeck plugin add http://127.0.0.1:<Dashboard 端口>/，"
        "或 tsw dashboard workdeck-manifest --out <文件> 后 workdeck plugin add <文件>",
        f"8. 重启 Claude app，在数据目录 {home_text} 中打开一个会话作为主会话",
    ]


def is_windows() -> bool:
    return sys.platform == "win32"
