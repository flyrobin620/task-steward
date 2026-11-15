"""The self-update on real git, ledger and files (DN006 §9.8–§9.9, CC006 §11; T283)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from tests.component.test_host import guard, worker_task
from tests.machine_harness import MACHINE, Harness
from tsw.cli import main as cli_main
from tsw.config import DEFAULTS
from tsw.errors import TswError
from tsw.home import Home, read_marker, record_render
from tsw.host import dashboard_service as ds
from tsw.host import update_plan as up
from tsw.host.identity import SESSION_ENV
from tsw.host.maintenance import Maintenance
from tsw.host.status_app import StatusApp, make_server
from tsw.host.version import REPO_ROOT, head, runtime

UPDATE = "UC-HOST-009"
DASHBOARD = "UC-DIRECT-006"


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return proc.stdout.strip()


def _commit(repo: Path, files: dict[str, str], subject: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def product(tmp_path: Path) -> tuple[Path, str, str]:
    """A product repository with two commits: ``old`` and ``new``."""
    repo = tmp_path / "product"
    repo.mkdir()
    _git(repo, "init", "-q")
    old = _commit(
        repo,
        {
            "tsw/ledger.py": "x = 1\n",
            "tsw/projection.py": "REDUCER_VERSION = 5\n",
            "docs/a.md": "a\n",
        },
        "first",
    )
    new = _commit(
        repo,
        {
            "tsw/ledger.py": "x = 2\n",
            "tsw/projection.py": "REDUCER_VERSION = 6\n",
            "docs/a.md": "b\n",
            "plugin/hooks/hooks.json": "{}\n",
            "tsw/host/orchestrate_skill.md": "skill\n",
        },
        "host: something (T900) (#1)",
    )
    return repo, old, new


def _plan(h: Harness, repo: Path, **kwargs: Any) -> dict[str, Any]:
    with h.ledger.view() as conn:
        return up.build(h.home, env=os.environ, config=DEFAULTS, conn=conn, repo=repo, **kwargs)


@pytest.mark.use_case(UPDATE)
def test_the_plan_starts_at_the_oldest_running_version(
    h: Harness, product: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, old, new = product
    report = h.home.root / "runs" / "T900" / "a1" / "result.md"
    report.parent.mkdir(parents=True)
    report.write_text("# 报告\n", encoding="utf-8")
    worker_task(h, tmp_path)
    record_render(h.home, {"commit": new, "dirty": False, "at": "t"})
    plan = _plan(h, repo, running=[old])
    assert (plan["from"], plan["to"]) == (old, new)
    assert {s["source"] for s in plan["from_sources"]} == {"running", "rendered"}
    categories = plan["categories"]
    assert categories["projection"] == ["tsw/projection.py"]
    assert categories["plugin"] == ["plugin/hooks/hooks.json"]
    assert categories["rendered"] == ["tsw/host/orchestrate_skill.md"]
    assert categories["docs"] == ["docs/a.md"]
    assert plan["tasks"] == [{"task_id": "T900", "reports": ["runs/T900/a1/result.md"]}]
    assert [w["session_ref"] for w in plan["sessions"]["workers"]] == ["w-1"]
    names = [a["action"] for a in plan["actions"]]
    assert names[0] == "ledger_rebuild" and "reload" in names
    assert "reload" in plan["pending"]


@pytest.mark.use_case(UPDATE)
def test_once_everything_runs_the_target_nothing_is_pending(
    h: Harness, product: tuple[Path, str, str]
) -> None:
    repo, _, new = product
    record_render(h.home, {"commit": new, "dirty": False, "at": "t"})
    plan = _plan(h, repo, running=[new])
    assert plan["from"] == plan["to"] == new
    assert plan["changed"] == [] and plan["pending"] == []


@pytest.mark.use_case(UPDATE)
def test_without_a_record_the_start_must_be_given(
    h: Harness, product: tuple[Path, str, str]
) -> None:
    repo, old, new = product
    with pytest.raises(TswError) as refused:
        _plan(h, repo)
    assert refused.value.details["reason"] == "from_unknown"
    plan = _plan(h, repo, start=old, to=new)
    assert plan["from_sources"] == [{"source": "from", "commit": old}]
    with pytest.raises(TswError):
        _plan(h, repo, start="not-a-commit")


@pytest.mark.use_case(UPDATE)
def test_user_auto_rules_are_compared_with_the_install_hint(
    h: Harness, product: tuple[Path, str, str]
) -> None:
    repo, _, new = product
    record_render(h.home, {"commit": new, "dirty": False, "at": "t"})
    settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.write_text(
        json.dumps({"autoMode": {"allow": ["TaskSteward Worker Merge: old"]}}), encoding="utf-8"
    )
    plan = _plan(h, repo, running=[new])
    rules = {r["rule"]: r["state"] for r in plan["state"]["host_rules"]}
    assert rules["TaskSteward Worker Merge"] == "outdated"
    assert rules["TaskSteward Worker Dispatch"] == "missing"
    assert plan["pending"] == ["host_rules"]


@pytest.mark.use_case(UPDATE)
def test_the_mcp_server_never_imports_the_command_line_only_modules() -> None:
    names = sorted(
        path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for path in up.NOT_IN_MCP
    )
    code = (
        "import json, sys, tsw.host.mcp; "
        f"print(json.dumps([m for m in {names!r} if m in sys.modules]))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, check=True
    )
    assert json.loads(proc.stdout) == []


@pytest.mark.use_case(UPDATE)
def test_render_records_the_commit_and_the_runtime_reports_its_own(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SESSION_ENV, "orch-1")
    monkeypatch.chdir(h.home.root)
    assert cli_main(["--home", str(h.home.root), "host", "render", "--json"]) == 0
    capsys.readouterr()
    current = head()
    assert current is not None
    rendered = read_marker(h.home.root)["rendered"]
    assert rendered["commit"] == current["commit"]
    assert set(rendered) == {"commit", "dirty", "at"}
    loaded = runtime({"CLAUDE_PLUGIN_ROOT": "/cache/task-steward/task-steward/7bfb2229dd46"})
    assert loaded["commit"] == current["commit"]
    assert loaded["plugin_version"] == "7bfb2229dd46"


@pytest.mark.use_case(UPDATE)
def test_update_record_is_a_maintenance_event_under_the_lease(h: Harness) -> None:
    tools = Maintenance(h.home, h.ledger, "orch-1", MACHINE)
    out = tools.update_record(
        {
            "from": "aaa",
            "to": "bbb",
            "categories": ["runtime"],
            "actions": ["render", "reload"],
            "pending": [],
            "verified": True,
            "notes": "重启 app",
        }
    )
    assert out["kind"] == "host_update" and out["verified"] is True
    events = [e for e in h.ledger.storage.read() if e.type == "maintenance.performed"]
    assert events[-1].data["to"] == "bbb" and events[-1].data["user_steps"] == []
    with pytest.raises(TswError):
        tools.update_record({"from": "aaa"})
    with pytest.raises(TswError):
        Maintenance(h.home, h.ledger, "orch-2", MACHINE).update_record({"from": "a", "to": "b"})


@pytest.mark.use_case(UPDATE)
def test_the_main_session_may_start_its_successor_in_the_data_directory(
    h: Harness, tmp_path: Path
) -> None:
    successor = guard(
        h,
        "orch-1",
        h.home.root,
        tool_name="mcp__ccd_session__start_session",
        tool_input={"name": "tsw-main-20261115-1200", "cwd": str(h.home.root)},
    )
    assert successor.run("PreToolUse") is None
    # Anywhere else, CC-B9 still asks for an attempt first.
    elsewhere = guard(
        h,
        "orch-1",
        h.home.root,
        tool_name="mcp__ccd_session__start_session",
        tool_input={"name": "tsw-main-x", "cwd": str(h.home.root / "work")},
    )
    refused = elsewhere.run("PreToolUse")
    assert refused is not None and "attempt_start" in json.dumps(refused, ensure_ascii=False)


@pytest.mark.use_case(DASHBOARD)
def test_every_registered_session_is_refused_the_token_folder(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    secret = ds.secret_path(os.environ, "k")
    for who, cwd in (("orch-1", h.home.root), (session, workdir)):
        for tool, tool_input in (
            ("Read", {"file_path": str(secret)}),
            ("Bash", {"command": f'cat "{secret.as_posix()}"'}),
        ):
            out = guard(h, who, cwd, tool_name=tool, tool_input=tool_input).run("PreToolUse")
            assert out is not None, (who, tool)
            assert "CC-B12" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.use_case(DASHBOARD)
def test_a_worker_may_not_restart_the_dashboard(h: Harness, tmp_path: Path) -> None:
    session, workdir = worker_task(h, tmp_path)
    command = {"command": "tsw dashboard restart"}
    refused = guard(h, session, workdir, tool_name="Bash", tool_input=command).run("PreToolUse")
    assert refused is not None
    allowed = guard(h, "orch-1", h.home.root, tool_name="Bash", tool_input=command)
    assert allowed.run("PreToolUse") is None
    status = guard(
        h, session, workdir, tool_name="Bash", tool_input={"command": "tsw dashboard status"}
    )
    assert status.run("PreToolUse") is None


@pytest.fixture
def hosted(home: Home) -> Iterator[tuple[StatusApp, int]]:
    """A hosted Dashboard on a free port, reporting the product's HEAD."""
    current = head()
    health = {
        "service": ds.SERVICE_NAME,
        "commit": current["commit"] if current else None,
        "home_key": ds.home_key(home),
        "started_at": "t",
    }
    app = StatusApp(home, "tok-h", cookie_max_age=ds.COOKIE_MAX_AGE, health=health)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, app.port
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.use_case(DASHBOARD)
def test_the_hosted_service_answers_health_without_a_token_and_keeps_its_cookie(
    hosted: tuple[StatusApp, int],
) -> None:
    app, port = hosted
    answer = ds.health(port)
    assert answer is not None and answer["home_key"] == app.health["home_key"]  # type: ignore[index]
    assert "token" not in json.dumps(answer)
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/?token=tok-h")
    response = conn.getresponse()
    assert response.status == 303
    assert f"Max-Age={ds.COOKIE_MAX_AGE}" in (response.getheader("Set-Cookie") or "")
    conn.close()
    other = HTTPConnection("127.0.0.1", port, timeout=5)
    other.request("GET", "/healthz", headers={"Host": "evil.example"})
    assert other.getresponse().status == 403
    other.close()
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/")  # no cookie, no page


@pytest.mark.use_case(DASHBOARD)
def test_install_restart_status_and_uninstall_through_task_scheduler(
    home: Home, hosted: tuple[StatusApp, int]
) -> None:
    _, port = hosted
    env = dict(os.environ)
    assert ds.status(home, env, port)["installed"] is False
    assert ds.restart(home, env, port)["installed"] is False
    installed = ds.install(home, env, port)
    assert installed["installed"] and installed["token_created"] and installed["running"]
    key = ds.home_key(home)
    token = ds.read_token(env, key)
    assert token and token not in json.dumps(installed)
    fake = json.loads((Path(env[ds.STATE_ENV]) / "fake-schtasks.json").read_text("utf-8"))
    xml = fake["tasks"][ds.task_name(key)]["xml"]
    assert "<LogonTrigger>" in xml and f'--home "{home.root}"' in xml
    assert [c[0] for c in fake["calls"]][-2:] == ["/Create", "/Run"]
    restarted = ds.restart(home, env, port)
    assert restarted["current"] is True and restarted["commit"] == restarted["head"]
    state = ds.status(home, env, port)
    assert (state["installed"], state["running"], state["commit"]) == (True, True, state["head"])
    assert ds.open_url(home, env, port) == f"http://127.0.0.1:{port}/?token={token}"
    # Reinstalling keeps the token: browsers that signed in stay signed in.
    assert ds.install(home, env, port)["token_created"] is False
    assert ds.uninstall(home, env)["uninstalled"] is True
    assert ds.read_token(env, key) is None
    with pytest.raises(TswError):
        ds.open_url(home, env, port)


@pytest.mark.use_case(DASHBOARD)
def test_installing_off_windows_without_task_scheduler_is_unsupported(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ds.SCHTASKS_ENV)
    monkeypatch.setattr(ds.sys, "platform", "linux")
    with pytest.raises(TswError) as refused:
        ds.install(home, dict(os.environ), 47801)
    assert refused.value.details["reason"] == "unsupported_platform"
    assert ds.status(home, dict(os.environ), 47801)["installed"] is False


@pytest.mark.use_case(DASHBOARD)
def test_the_service_without_a_token_logs_why_and_exits(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(["--home", str(home.root), "dashboard", "service"])
    assert code == 3
    log = ds.log_path(os.environ, ds.home_key(home)).read_text(encoding="utf-8")
    assert "tsw dashboard install" in log
    assert "not_installed" in capsys.readouterr().out  # streams are restored afterwards
