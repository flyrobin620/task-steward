"""The Dashboard as a resident service: a logon task, a fixed port, lasting sign-ins (T283).

DN006 §8.10 ("常驻服务") sets the principles, CC006 §12 the Windows specifics. The user installs
the service in a terminal (``tsw dashboard install``): a Task Scheduler task that starts at the
user's logon and runs ``tsw dashboard service`` without a window. Each ``tsw dashboard open``
(terminal only) makes a new token, opens the browser with it and keeps only its SHA-256, outside
the data directory, in a folder the hooks keep sessions away from (CC008 CC-B12). The service
admits a token whose hash is listed, so a restart signs nobody out, and a session that reads the
folder anyway finds nothing it could sign in with.

Task Scheduler is a platform outside this repository: every ``schtasks`` call goes through
:func:`schtasks`, which a test points at a stand-in with ``TSW_SCHTASKS`` (an executable, or a
``.py`` script run with this interpreter). Without it, installing is only supported on Windows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from xml.sax.saxutils import escape

from tsw.errors import TswError, rejected
from tsw.events import utc_now
from tsw.home import Home
from tsw.host.version import REPO_ROOT, head

SECRET_DIR = "tsw-dashboard-secret"  # CC008 CC-B12 refuses a session's tools on this name
MAX_TOKENS = 8  # sign-ins kept: browsers and the PWA opened with the latest `open` runs
STATE_ENV = "TSW_STATE_DIR"  # tests (and unusual setups) put the per-user state elsewhere
SCHTASKS_ENV = "TSW_SCHTASKS"
HEALTH_PATH = "/healthz"
SERVICE_NAME = "tsw-dashboard"
COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers cap cookie lifetimes at 400 days
HEALTH_TIMEOUT_SECONDS = 2.0
RESTART_WAIT_SECONDS = 30.0
WAIT_ENV = "TSW_DASHBOARD_WAIT_SECONDS"  # tests wait less for a service that never answers
SCHTASKS_TIMEOUT_SECONDS = 60


def _field(value: Any, key: str) -> Any:
    """``value[key]`` when ``value`` is a JSON object, else None."""
    return cast("dict[str, Any]", value).get(key) if isinstance(value, dict) else None


def wait_seconds(env: Mapping[str, str]) -> float:
    """How long install and restart wait for the service to answer."""
    try:
        return float(env.get(WAIT_ENV) or RESTART_WAIT_SECONDS)
    except ValueError:
        return RESTART_WAIT_SECONDS


def home_key(home: Home) -> str:
    """Eight hex digits of the data directory's normalized path: one service per data directory."""
    path = os.path.normcase(str(home.root.resolve()))
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]


def task_name(key: str) -> str:
    return f"TaskSteward Dashboard ({key})"


def state_dir(env: Mapping[str, str]) -> Path:
    """Per-user state outside the data directory: ``%LOCALAPPDATA%\\TaskSteward`` on Windows."""
    explicit = env.get(STATE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    local = env.get("LOCALAPPDATA")
    if local:
        return Path(local) / "TaskSteward"
    xdg = env.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "task-steward"


def secret_path(env: Mapping[str, str], key: str) -> Path:
    return state_dir(env) / SECRET_DIR / f"{key}.json"


def log_path(env: Mapping[str, str], key: str) -> Path:
    return state_dir(env) / "dashboard" / f"{key}.log"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_store(env: Mapping[str, str], key: str) -> dict[str, Any] | None:
    """The sign-in store: the data directory and the hashes of the tokens handed out."""
    try:
        data: Any = json.loads(secret_path(env, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _write_store(env: Mapping[str, str], key: str, data: Mapping[str, Any]) -> None:
    path = secret_path(env, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(data)), encoding="utf-8")
    tmp.replace(path)


def ensure_store(env: Mapping[str, str], key: str, home: Home) -> bool:
    """Create the sign-in store on first install (empty); a reinstall keeps it, so browsers that
    signed in stay signed in. Returns whether it was created."""
    if read_store(env, key) is not None:
        return False
    _write_store(env, key, {"home": str(home.root), "created_at": utc_now(), "hashes": []})
    return True


def issue_token(env: Mapping[str, str], key: str) -> str:
    """A new sign-in token: only its hash is kept (the newest ``MAX_TOKENS``)."""
    store = read_store(env, key)
    if store is None:
        raise rejected(
            "Dashboard 常驻服务没有安装：先运行 tsw dashboard install", reason="not_installed"
        )
    token = secrets.token_urlsafe(32)
    hashes = [h for h in cast("list[Any]", store.get("hashes") or []) if isinstance(h, str)]
    store["hashes"] = [*hashes, digest(token)][-MAX_TOKENS:]
    _write_store(env, key, store)
    return token


def verifier(env: Mapping[str, str], key: str) -> Callable[[str], bool]:
    """Whether a presented token is one ``tsw dashboard open`` handed out (read on each check, so
    a new sign-in works without restarting the service)."""

    def admits(token: str) -> bool:
        store = read_store(env, key)
        hashes = cast("list[Any]", store.get("hashes") or []) if store else []
        wanted = digest(token)
        return any(isinstance(h, str) and hmac.compare_digest(h, wanted) for h in hashes)

    return admits


def pythonw(executable: str = sys.executable) -> str:
    """The windowless interpreter next to this one, else this one."""
    path = Path(executable)
    candidate = path.with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else str(path)


def task_xml(*, user: str, command: str, arguments: str, workdir: str) -> str:
    """The Task Scheduler definition: start at this user's logon, unelevated, never time out,
    one instance, keep running on battery, retry a failed start (CC006 §12)."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>TaskSteward Dashboard (tsw dashboard install)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def service_arguments(home: Home) -> str:
    return f'-m tsw dashboard service --home "{home.root}"'


def scheduler(env: Mapping[str, str]) -> list[str]:
    """The ``schtasks`` command line (or the ``TSW_SCHTASKS`` stand-in); refused off Windows."""
    override = env.get(SCHTASKS_ENV)
    if override:
        return [sys.executable, override] if override.endswith(".py") else [override]
    if sys.platform == "win32":
        return ["schtasks"]
    raise rejected(
        "Dashboard 常驻服务目前只支持 Windows（计划任务）；请在终端运行 tsw dashboard",
        reason="unsupported_platform",
        platform=sys.platform,
    )


def schtasks(env: Mapping[str, str], *args: str) -> tuple[int, str]:
    """Run ``schtasks``; return code and combined output."""
    program = scheduler(env)
    try:
        proc = subprocess.run(
            [*program, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env),
            timeout=SCHTASKS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}".strip()


def installed(env: Mapping[str, str], key: str) -> bool:
    code, _ = schtasks(env, "/Query", "/TN", task_name(key))
    return code == 0


def health(port: int, timeout: float = HEALTH_TIMEOUT_SECONDS) -> dict[str, Any] | None:
    """The ``/healthz`` answer of a Dashboard service on ``port`` (any data directory's), or None
    when nothing answers."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{HEALTH_PATH}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data: Any = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return cast("dict[str, Any]", data) if _field(data, "service") == SERVICE_NAME else None


def ours(answer: Mapping[str, Any] | None, key: str) -> bool:
    """Whether a ``/healthz`` answer comes from this data directory's service."""
    return answer is not None and answer.get("home_key") == key


def _wait(port: int, key: str, commit: str | None, seconds: float) -> dict[str, Any] | None:
    """Wait for this data directory's service (at ``commit`` when given); the last answer."""
    deadline = time.monotonic() + seconds
    found = None
    while True:
        found = health(port)
        if ours(found, key) and (commit is None or _field(found, "commit") == commit):
            return found
        if time.monotonic() >= deadline:
            return found
        time.sleep(0.5)


def _service(answer: Mapping[str, Any] | None, key: str, port: int) -> dict[str, Any]:
    """``running`` only for this data directory's service; another one is a port conflict."""
    if answer is not None and not ours(answer, key):
        return {
            "running": False,
            "conflict": f"端口 {port} 上是另一个数据目录的 Dashboard 服务"
            f"（{answer.get('home_key')}）：改配置 dashboard.port 后重启",
        }
    return {"running": answer is not None}


def status(home: Home, env: Mapping[str, str], port: int) -> dict[str, Any]:
    """Read-only: is the task registered, does the service answer, which commit it runs."""
    key = home_key(home)
    try:
        registered = installed(env, key)
    except TswError as exc:  # an unsupported platform is a state, not an error here
        return {"installed": False, "note": str(exc), "port": port, "home_key": key}
    answer = health(port)
    current = head()
    service = _service(answer, key, port)
    return {
        "installed": registered,
        "task": task_name(key),
        **service,
        "port": port,
        "url": f"http://127.0.0.1:{port}/",
        "commit": _field(answer, "commit") if service["running"] else None,
        "head": current.get("commit") if current else None,
        "home_key": key,
        "log": str(log_path(env, key)),
    }


def install(home: Home, env: Mapping[str, str], port: int) -> dict[str, Any]:
    key = home_key(home)
    scheduler(env)  # an unsupported platform is refused before anything is written
    created = ensure_store(env, key, home)
    user = f"{env.get('USERDOMAIN', '')}\\{env.get('USERNAME', '')}".lstrip("\\")
    xml = task_xml(
        user=user,
        command=pythonw(),
        arguments=service_arguments(home),
        workdir=str(REPO_ROOT),
    )
    definition = state_dir(env) / "dashboard" / f"{key}.task.xml"
    definition.parent.mkdir(parents=True, exist_ok=True)
    definition.write_text(xml, encoding="utf-16")
    try:
        code, output = schtasks(
            env, "/Create", "/TN", task_name(key), "/XML", str(definition), "/F"
        )
    finally:
        definition.unlink(missing_ok=True)
    if code != 0:
        raise rejected("登记计划任务失败", reason="schtasks_failed", output=output)
    schtasks(env, "/Run", "/TN", task_name(key))
    answer = _wait(port, key, None, wait_seconds(env))
    return {
        "installed": True,
        "task": task_name(key),
        "store_created": created,
        **_service(answer, key, port),
        "url": f"http://127.0.0.1:{port}/",
        "next": "在终端运行 tsw dashboard open 用浏览器打开（第一次带令牌，之后可以安装为 PWA）",
        "log": str(log_path(env, key)),
    }


def uninstall(home: Home, env: Mapping[str, str]) -> dict[str, Any]:
    key = home_key(home)
    schtasks(env, "/End", "/TN", task_name(key))
    code, output = schtasks(env, "/Delete", "/TN", task_name(key), "/F")
    secret_path(env, key).unlink(missing_ok=True)
    return {"uninstalled": code == 0, "task": task_name(key), "output": output}


def restart(home: Home, env: Mapping[str, str], port: int) -> dict[str, Any]:
    """End and run the task again, then wait until the service runs the product's HEAD."""
    key = home_key(home)
    if not installed(env, key):
        return {"installed": False, "note": "Dashboard 常驻服务没有安装（tsw dashboard install）"}
    schtasks(env, "/End", "/TN", task_name(key))
    code, output = schtasks(env, "/Run", "/TN", task_name(key))
    if code != 0:
        raise rejected("启动计划任务失败", reason="schtasks_failed", output=output)
    current = head()
    wanted = current.get("commit") if current else None
    answer = _wait(port, key, wanted, wait_seconds(env))
    service = _service(answer, key, port)
    commit = _field(answer, "commit") if service["running"] else None
    return {
        "installed": True,
        "restarted": True,
        **service,
        "commit": commit,
        "head": wanted,
        "current": service["running"] and commit == wanted,
        "log": str(log_path(env, key)),
    }


def open_url(home: Home, env: Mapping[str, str], port: int) -> str:
    """The address with a new token, for the user's browser (``tsw dashboard open``, terminal)."""
    return f"http://127.0.0.1:{port}/?token={issue_token(env, home_key(home))}"
