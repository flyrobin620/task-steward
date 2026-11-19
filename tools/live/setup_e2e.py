"""The ``setup-e2e`` live scenario: both install paths from nothing in a clean profile (T308).

    python tools/live.py setup-e2e run --path terminal [--source <checkout or URL>]
    python tools/live.py setup-e2e run --path session  [--source <marketplace URL>]

Each run gets its own folder under ``%TEMP%\\tsw-live-setup`` (not under a data directory: Claude
Code's directory rename fails there, CC009 §8) with its own ``HOME`` / ``USERPROFILE``,
``LOCALAPPDATA`` and ``CLAUDE_CONFIG_DIR``, so the user's real ``~/.claude``, data directory and
plugin install are never read or written. What is real: PowerShell, Python, git, the ``claude``
command line (plugin commands; no model is called), the user ``Path`` in ``HKCU\\Environment``
(the run's shim folder is added and removed again; the run checks the value is restored exactly)
and the tsw runtime of the source. The Dashboard service is not installed (the scheduled task
would run with the real profile, not the run's). The session path runs the session entry's
scripts with the markers a session sets (``CLAUDECODE``, a fresh ``CLAUDE_CODE_HOST_SESSION_ID``):
simulated session markers, real scripts. The run ends with ``tsw setup uninstall`` through the
shim. ``result.json`` and ``evidence.md`` (local paths replaced) go to the run folder.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_URL = "https://github.com/flyrobin620/task-steward.git"
PLUGIN_ID = "task-steward@task-steward"
TIMEOUT = 900


class Run:
    def __init__(self, path: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        self.id = f"live-setup-{path}-{stamp}-{secrets.token_hex(3)}"
        self.root = Path(tempfile.gettempdir()) / "tsw-live-setup" / self.id
        self.user = self.root / "user"
        self.local = self.root / "localappdata"
        self.config = self.root / "claude-config"
        self.home = self.root / "data"
        self.checkout = self.local / "TaskSteward" / "task-steward"
        self.shims = self.local / "TaskSteward" / "bin"
        self.checks: list[dict[str, Any]] = []
        self.log: list[str] = []
        for folder in (self.user, self.local, self.config):
            folder.mkdir(parents=True, exist_ok=True)

    def env(self, *, session: bool = False) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith(("CLAUDE", "ANTHROPIC", "TSW_")) and k != "PYTHONPATH"
        }
        env.update(
            HOME=str(self.user),
            USERPROFILE=str(self.user),
            LOCALAPPDATA=str(self.local),
            CLAUDE_CONFIG_DIR=str(self.config),
            PYTHONUTF8="1",
        )
        if session:  # the markers Claude Code sets in a session's processes (simulated)
            env["CLAUDECODE"] = "1"
            env["CLAUDE_CODE_HOST_SESSION_ID"] = f"{self.id}-session"
        return env

    def run(
        self, args: list[str], *, env: dict[str, str], stdin: str = "", cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(cwd or self.root),
            timeout=TIMEOUT,
            check=False,
        )
        self.log.append(f"$ {' '.join(args)}\n-> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def check(self, ident: str, ok: bool, detail: Any = None) -> bool:
        self.checks.append({"id": ident, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {ident}: {detail if detail is not None else ''}")
        return bool(ok)

    def scrub(self, text: str) -> str:
        for real, shown in (
            (str(self.root), "<run>"),
            (str(self.root).replace("\\", "/"), "<run>"),
            (str(ROOT), "<source>"),
            (os.environ.get("USERPROFILE", "~~~"), "<user>"),
            (os.environ.get("USERNAME", "~~~"), "<name>"),
        ):
            text = text.replace(real, shown)
        return text


def last_json(text: str) -> dict[str, Any]:
    for line in reversed(text.strip().splitlines()):
        if '{"' in line:
            try:
                return json.loads(line[line.index('{"') :])
            except ValueError:
                continue
    return {}


def user_path() -> tuple[str | None, int]:
    import winreg  # noqa: PLC0415

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return None, 0
    return str(value), int(kind)


def machine_path() -> str:
    import winreg  # noqa: PLC0415

    name = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, name) as key:
        return str(winreg.QueryValueEx(key, "Path")[0])


def fresh_path() -> str:
    """The PATH a newly started program gets: the machine's, then the user's (expanded)."""
    user, _ = user_path()
    return os.path.expandvars(";".join(p for p in (machine_path(), user or "") if p))


def answers(run: Run) -> dict[str, Any]:
    return {
        "home": str(run.home),
        "language": "zh-CN",
        "reviewer": "claude-headless",
        "human_channels": "in-session",
        "host": "claude-code",
        "dashboard": False,
        "dashboard_port": 47899,
        "user_settings": True,
    }


def claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ClaudeCode" / "claude.exe"
    if app.is_file():
        return str(app)
    raise SystemExit("live: 找不到 claude 命令行")


# ---------------------------------------------------------------------- the two paths


def terminal_path(run: Run, source: str) -> None:
    """DN009 §4.1: clone, then setup.ps1 asks, shows the plan and installs."""
    env = run.env()
    run.check(
        "S0 克隆产品检出",
        run.run(["git", "clone", "-q", source, str(run.checkout)], env=env).returncode == 0,
    )
    a = answers(run)
    replies = [a["home"], "", "claude-headless", "", "否", "", "", "是"]
    proc = run.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(run.checkout / "setup.ps1"),
            "-NoBrowser",
        ],
        env=env,
        stdin="\n".join(replies) + "\n",
    )
    out = last_json(proc.stdout)
    run.check(
        "S1 setup.ps1 安装完成且安装阶段 verify 通过",
        proc.returncode == 0 and out.get("verify", {}).get("ok") is True,
        {"exit": proc.returncode, "failed": out.get("verify", {}).get("failed")},
    )


def session_path(run: Run, source: str) -> None:
    """DN009 §4.2: /plugin install, then the setup skill's scripts in the session."""
    env = run.env(session=True)
    proc = run.run(
        [claude(), "plugin", "install", "task-steward", "--marketplace", source], env=env
    )
    records = json.loads((run.config / "plugins" / "installed_plugins.json").read_text("utf-8"))
    copy = Path(records["plugins"][PLUGIN_ID][0]["installPath"])
    run.check("S0 一步安装插件（--marketplace）", proc.returncode == 0, copy.name)
    probe = run.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(copy / "setup" / "prereq.ps1"),
            "-Json",
        ],
        env=env,
    )
    found = last_json(probe.stdout)
    python = str((found.get("python") or {}).get("path") or "")
    claude_path = str((found.get("claude") or {}).get("path") or "")
    if not run.check("S0 探测前提", bool(python and claude_path), found.get("missing")):
        raise SystemExit("live: 前提不满足")
    fetched = run.run([python, "-X", "utf8", str(copy / "setup" / "fetch_checkout.py")], env=env)
    got = last_json(fetched.stdout)
    run.check(
        "S0 取检出：克隆并检出插件安装的提交",
        fetched.returncode == 0 and got.get("same_as_plugin") is True,
        {"head": got.get("head"), "created": got.get("created")},
    )
    launcher = run.checkout / "plugin" / "bin" / "tsw_launch.py"
    tsw = [python, "-X", "utf8", str(launcher), "setup"]
    checked = last_json(run.run([*tsw, "check", "--questions", "--json"], env=env).stdout)
    asked = [q["id"] for q in checked.get("questions", []) if q.get("asked_by") == "setup"]
    file = Path(checked.get("answers_file") or run.root / "answers.json")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(answers(run)), encoding="utf-8")
    run.check("S0 问题清单", "home" in asked, asked)
    common = ["--answers", str(file), "--python", python, "--claude", claude_path, "--json"]
    planned = run.run([*tsw, "plan", *common], env=env)
    run.check("S0 计划（只读）", planned.returncode == 0, last_json(planned.stdout).get("todo"))
    proc = run.run([*tsw, "install", *common, "--no-browser"], env=env)
    out = last_json(proc.stdout)
    run.check(
        "S1 会话中（未绑定数据目录）安装完成且安装阶段 verify 通过",
        proc.returncode == 0 and out.get("verify", {}).get("ok") is True,
        {"exit": proc.returncode, "failed": out.get("verify", {}).get("failed")},
    )


# ---------------------------------------------------------------------- shared checks


def after_install(run: Run, original_path: str | None) -> None:
    value, _ = user_path()
    entries = [os.path.normcase(p) for p in (value or "").split(";")]
    run.check("S2 用户级 PATH（HKCU）加入了垫片目录", os.path.normcase(str(run.shims)) in entries)
    record = json.loads((run.local / "TaskSteward" / "install.json").read_text("utf-8"))
    run.check("S2 install.json 记下 PATH 是本次加入的", record["steps"]["path"]["added"] is True)
    known = json.loads((run.config / "plugins" / "known_marketplaces.json").read_text("utf-8"))
    source = known["task-steward"]["source"]
    head = subprocess.run(
        ["git", "-C", str(run.checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    installed = json.loads((run.config / "plugins" / "installed_plugins.json").read_text("utf-8"))
    version = installed["plugins"][PLUGIN_ID][0]["version"]
    run.check(
        "S3 插件来源指向检出、副本为检出 HEAD",
        source.get("source") == "directory"
        and os.path.normcase(source.get("path", "")) == os.path.normcase(str(run.checkout))
        and head.startswith(version),
        {"source": source.get("source"), "version": version},
    )
    settings = json.loads((run.config / "settings.json").read_text("utf-8"))
    names = [r.split(":")[0] for r in settings.get("autoMode", {}).get("allow", [])]
    run.check(
        "S4 用户设置：env.TSW_HOME、env.TSW_PYTHON 与三条规则",
        settings.get("env", {}).get("TSW_HOME") == str(run.home)
        and bool(settings.get("env", {}).get("TSW_PYTHON"))
        and names
        == ["TaskSteward Worker Dispatch", "TaskSteward Worker Merge", "TaskSteward Routine"],
        names,
    )
    env = run.env()
    env["PATH"] = fresh_path()
    mcp = run.run([claude(), "mcp", "list"], env=env, cwd=run.home)
    line = next((x for x in mcp.stdout.splitlines() if "tsw" in x), "")
    run.check(
        "S5 claude mcp list：插件的 tsw MCP server 已连接（TSW_PYTHON、TSW_HOME 来自用户设置）",
        "onnected" in line,
        run.scrub(line.strip()),
    )
    copy = Path(installed["plugins"][PLUGIN_ID][0]["installPath"])
    hooks = json.loads((copy / "hooks" / "hooks.json").read_text("utf-8"))["hooks"]
    command = hooks["SessionStart"][0]["hooks"][0]["command"]
    hook_env = {
        **env,
        "CLAUDE_PLUGIN_ROOT": str(copy).replace("\\", "/"),
        "TSW_HOME": str(run.home),
        "TSW_PYTHON": settings["env"]["TSW_PYTHON"],
        "CLAUDE_CODE_HOST_SESSION_ID": f"{run.id}-main",
    }
    payload = json.dumps({"session_id": f"{run.id}-main", "cwd": str(run.home)})
    hooked = run.run([bash(), "-c", command], env=hook_env, stdin=payload, cwd=run.home)
    run.check(
        "S6 插件副本的 SessionStart hook 命令在 Git Bash 中按 TSW_PYTHON 运行，主会话得到注入",
        hooked.returncode == 0 and "additionalContext" in hooked.stdout,
        hooked.returncode,
    )
    shim = run.run(["cmd", "/c", "where", "tsw"], env=env)
    run.check(
        "S7 新进程的 PATH 中 tsw 解析到垫片（cmd）",
        os.path.normcase(str(run.shims)) in os.path.normcase(shim.stdout),
        run.scrub(shim.stdout.strip()),
    )
    verify = run.run(
        ["cmd", "/c", "tsw", "setup", "verify", "--after-reload", "--running", head, "--json"],
        env=env,
    )
    result = last_json(verify.stdout)
    run.check(
        "S7 重新加载之后的 verify（经垫片）通过",
        verify.returncode == 0 and result.get("ok") is not False and not result.get("failed"),
        {"failed": result.get("failed"), "pending_reload": result.get("pending_reload")},
    )
    posix = env.copy()
    sh = run.run([bash(), "-c", "tsw home --json"], env=posix)
    run.check(
        "S7 Git Bash 中 tsw（sh 垫片）找到数据目录",
        last_json(sh.stdout).get("source") == ".local/tsw-home",
        last_json(sh.stdout).get("source"),
    )
    gone = run.run(["cmd", "/c", "tsw", "setup", "uninstall", "--yes", "--json"], env=env)
    after, _ = user_path()
    left = json.loads((run.config / "settings.json").read_text("utf-8"))
    plugins = json.loads((run.config / "plugins" / "installed_plugins.json").read_text("utf-8"))
    run.check(
        "S8 卸载：PATH 恢复原值，用户设置与插件移除，数据目录保留",
        gone.returncode == 0
        and after == original_path
        and "TSW_HOME" not in left.get("env", {})
        and PLUGIN_ID not in plugins.get("plugins", {})
        and (run.home / "tsw-home.json").is_file(),
        {"exit": gone.returncode, "path_restored": after == original_path},
    )


def bash() -> str:
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("bash")
    if found is None:
        raise SystemExit("live: 找不到 Git Bash")
    return found


def restore_path(original: str | None, kind: int) -> None:
    import winreg  # noqa: PLC0415

    current, _ = user_path()
    if current == original:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        if original is None:
            winreg.DeleteValue(key, "Path")
        else:
            winreg.SetValueEx(key, "Path", 0, kind or winreg.REG_EXPAND_SZ, original)


def main(path: str, source: str | None) -> int:
    if sys.platform != "win32":
        raise SystemExit("live: setup-e2e 只在 Windows 上运行")
    run = Run(path)
    original, kind = user_path()
    (run.root / "manifest.json").write_text(
        json.dumps({"id": run.id, "path": path, "original_user_path": original, "kind": kind}),
        encoding="utf-8",
    )
    print(f"运行 {run.id}（{run.root}）")
    try:
        if path == "terminal":
            terminal_path(run, source or str(ROOT))
        else:
            session_path(run, source or DEFAULT_URL)
        after_install(run, original)
    finally:
        restore_path(original, kind)  # whatever happened, the user's Path is as it was
        final, _ = user_path()
        run.check("S9 清理：用户级 PATH 与运行前相同", final == original)
        ok = all(c["ok"] for c in run.checks)
        result = {"id": run.id, "path": path, "ok": ok, "checks": run.checks}
        (run.root / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [
            f"# setup-e2e {path}（{run.id}）",
            "",
            "真实：PowerShell、Python、git、claude 命令行的插件命令（不调用模型）、"
            "HKCU 用户级 PATH、tsw 运行时；"
            + ("模拟：会话标记（CLAUDECODE 与会话 ID）。" if path == "session" else "无模拟。")
            + "Dashboard 常驻服务不在本场景中安装。",
            "",
            *(f"- {'通过' if c['ok'] else '失败'} {c['id']}：{c['detail']}" for c in run.checks),
        ]
        (run.root / "evidence.md").write_text(run.scrub("\n".join(lines)) + "\n", "utf-8")
        (run.root / "commands.log").write_text(run.scrub("\n\n".join(run.log)), "utf-8")
        print(f"结果：{'通过' if ok else '失败'}；证据 {run.root / 'evidence.md'}")
    return 0 if all(c["ok"] for c in run.checks) else 1
