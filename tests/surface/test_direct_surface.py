"""The user's terminal commands and the STATUS app as separate processes (DN006 §8.10, §9.1)."""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.machine_harness import MACHINE, ORCH, Harness
from tests.support import REPO_ROOT, child_env, run_json, run_tsw
from tsw import lease
from tsw.home import Home

mark = pytest.mark


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def handed_back(h: Harness) -> None:
    """The harness's orchestrator, on another machine, hands the lease back: this one may write."""
    h.ledger.commit(lease.release("orch-1", machine=MACHINE, actor=ORCH.actor()))


def json_of(proc: Any) -> dict[str, Any]:
    stream = proc.stdout if proc.returncode == 0 else proc.stderr
    return json.loads(stream.strip().splitlines()[-1])


def events(h: Harness, kind: str) -> list[Any]:
    return [e for e in h.ledger.storage.read(since=0) if e.type == kind]


# ---------------------------------------------------------------------- terminal commands


@mark.use_case("UC-DIRECT-001")
@mark.transition("T22")
def test_tsw_task_accept_records_the_users_terminal_decision(h: Harness) -> None:
    h.complete("T001")
    handed_back(h)
    code, out = run_json("task", "accept", "T001", "--yes", "--note", "看过报告，可以")
    assert code == 0, out
    assert (out["event"], out["revision"], out["channel"]) == ("task.user_accepted", 1, "user-cli")
    assert h.status("T001") == "accepted"
    source = events(h, "task.user_accepted")[-1].data["user_source"]
    assert source["channel"] == "user-cli"
    assert source["message_ref"].startswith("cli:")
    assert source["text"] == "tsw task accept T001：看过报告，可以"
    assert source["target"] == ["T001", 1, "accept"]


@mark.use_case("UC-DIRECT-001")
@mark.transition("T23")
@mark.transition("T25")
def test_tsw_task_reject_and_cancel(h: Harness) -> None:
    h.complete("T001")
    h.register("T002")
    handed_back(h)
    assert run_tsw("task", "reject", "T001", "--yes").returncode == 2  # --reason is required
    code, out = run_json("task", "reject", "T001", "--yes", "--reason", "缺少导出")
    assert (code, out["event"]) == (0, "task.user_rejected")
    assert (h.task("T001")["status"], h.task("T001")["revision"]) == ("intake", 2)
    code, out = run_json("task", "cancel", "T002", "--yes")
    assert (code, out["event"]) == (0, "task.cancelled")
    code, err = run_json("task", "accept", "T002", "--yes")
    assert (code, err["details"]["reason"]) == (3, "invalid_state")


@mark.use_case("UC-DIRECT-001")
@pytest.mark.parametrize("marker", ["CLAUDE_CODE_HOST_SESSION_ID", "CLAUDECODE"])
def test_the_users_commands_refuse_inside_any_host_session(h: Harness, marker: str) -> None:
    h.complete("T001")
    env = child_env(**{marker: "1"})
    for args in (
        ("task", "accept", "T001", "--yes"),
        ("task", "reject", "T001", "--yes", "--reason", "x"),
        ("task", "cancel", "T001", "--yes"),
        ("status", "serve", "--no-open"),
        ("lease", "takeover", "--session", "s2", "--from-session", "orch-1"),
    ):
        code, err = run_json(*args, env=env)
        assert (code, err["details"]["reason"]) == (3, "host_session_cli"), args
    assert h.status("T001") == "done"


@mark.use_case("UC-DIRECT-001")
def test_without_a_terminal_the_user_must_confirm_with_yes(h: Harness) -> None:
    h.complete("T001")
    handed_back(h)
    code, err = run_json("task", "accept", "T001")
    assert (code, err["details"]["reason"]) == (3, "not_confirmed")
    assert h.status("T001") == "done"


@mark.use_case("UC-LEASE-001")
def test_the_user_takes_over_the_lease_without_writing_a_source(h: Harness) -> None:
    code, out = run_json(
        "lease", "takeover", "--session", "orch-2", "--from-session", "orch-1", "--machine", "m1"
    )
    assert (code, out["session_id"]) == (0, "orch-2")
    source = events(h, "controller.lease_taken_over")[-1].data["user_source"]
    assert source["channel"] == "user-cli"
    assert "--from-session orch-1" in source["text"]


# ---------------------------------------------------------------------- locating the data directory


def _repo_copy(tmp_path: Path) -> Path:
    """A product repository of this checkout's runtime, so its ``.local`` stays the test's."""
    repo = tmp_path / "product"
    shutil.copytree(REPO_ROOT / "tsw", repo / "tsw", ignore=shutil.ignore_patterns("__pycache__"))
    return repo


@mark.use_case("UC-HOME-001")
def test_init_remember_lets_a_terminal_without_tsw_home_find_the_data_directory(
    tmp_path: Path,
) -> None:
    repo = _repo_copy(tmp_path)
    data = tmp_path / "data-home"
    env = child_env(PYTHONPATH=str(repo))
    env.pop("TSW_HOME", None)

    def tsw(*args: str, extra: dict[str, str] | None = None) -> dict[str, Any]:
        proc = run_tsw(*args, "--json", env={**env, **(extra or {})}, cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        return json_of(proc)

    out = tsw("init", "--home", str(data), "--remember")
    assert Path(out["remembered"]) == repo / ".local" / "tsw-home"
    out = tsw("home")
    assert (Path(out["home"]), out["source"], out["initialized"]) == (data, ".local/tsw-home", True)
    assert Path(out["product_repo"]) == repo
    # TSW_HOME still comes first, and the default is reported as such.
    assert tsw("home", extra={"TSW_HOME": str(tmp_path / "other")})["source"] == "TSW_HOME"
    (repo / ".local" / "tsw-home").unlink()
    assert tsw("home")["source"] == "default"


@mark.use_case("UC-HOME-001")
def test_init_without_remember_leaves_the_repository_alone(tmp_path: Path) -> None:
    repo = _repo_copy(tmp_path)
    env = child_env(PYTHONPATH=str(repo))
    proc = run_tsw("init", "--home", str(tmp_path / "d"), "--json", env=env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert Path(json_of(proc)["product_repo"]) == repo
    assert not (repo / ".local").exists()


# ---------------------------------------------------------------------- the STATUS app


class App:
    def __init__(self, proc: subprocess.Popen[str], url: str) -> None:
        self.proc = proc
        parts = urlsplit(url)
        self.port = int(parts.port or 0)
        self.token = parts.query.removeprefix("token=")
        self.cookie = ""

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            sent["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        conn.request(method, path, body=data, headers=sent)
        response = conn.getresponse()
        out = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return out

    def act(self, task_id: str, action: str, **body: Any) -> tuple[int, dict[str, Any]]:
        status, _, raw = self.request(
            "POST",
            f"/api/tasks/{task_id}/{action}",
            body,
            {"X-TSW-Token": self.token, "Origin": f"http://127.0.0.1:{self.port}"},
        )
        return status, json.loads(raw)


@pytest.fixture
def app(h: Harness) -> Iterator[App]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "tsw", "status", "serve", "--no-open"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**child_env(), "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        found = re.search(r"http://127\.0\.0\.1:\d+/\?token=[\w-]+", line)
        assert found, (line, proc.stderr.read() if proc.poll() is not None and proc.stderr else "")
        yield App(proc, found.group(0))
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@mark.use_case("UC-DIRECT-002")
@mark.transition("T22")
def test_the_status_app_admits_only_its_token_and_accepts_with_one_click(
    h: Harness, app: App
) -> None:
    h.complete("T001")
    h.register("T002")
    handed_back(h)
    # No token, a wrong token, or a foreign host name: nothing is shown.
    assert app.request("GET", "/")[0] == 403
    assert app.request("GET", "/?token=wrong")[0] == 403
    status, headers, _ = app.request("GET", f"/?token={app.token}")
    assert (status, headers["Location"]) == (303, "/")
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    app.cookie = cookie.split(";")[0]
    assert app.request("GET", "/", headers={"Host": f"evil.example:{app.port}"})[0] == 403
    status, _, page = app.request("GET", "/")
    text = page.decode("utf-8")
    assert status == 200
    assert f'<meta name="tsw-token" content="{app.token}">' in text
    assert "connect-src 'self'" in text.replace("&#x27;", "'")
    t1 = re.search(r'<div class="acts" data-task="T001" data-revision="1">.*?</div>', text)
    assert t1 and 'data-act="accept"' in t1.group(0) and 'data-act="reject"' in t1.group(0)
    t2 = re.search(r'<div class="acts" data-task="T002".*?</div>', text)
    assert t2 and 'data-act="cancel"' in t2.group(0) and 'data-act="accept"' not in t2.group(0)
    # A button request needs the header token and this page's origin.
    status, _, _ = app.request(
        "POST",
        "/api/tasks/T001/accept",
        {"revision": 1},
        {"Origin": f"http://127.0.0.1:{app.port}"},
    )
    assert status == 403
    status, _, _ = app.request(
        "POST",
        "/api/tasks/T001/accept",
        {"revision": 1},
        {"X-TSW-Token": app.token, "Origin": "http://evil.example"},
    )
    assert status == 403
    assert h.status("T001") == "done"
    # A page showing an older revision is refused; the current one is accepted.
    status, out = app.act("T001", "accept", revision=7)
    assert (status, out["ok"], out["reason"]) == (409, False, "g_user")
    status, out = app.act("T001", "accept", revision=1)
    assert (status, out["ok"], out["event"]) == (200, True, "task.user_accepted")
    assert h.status("T001") == "accepted"
    source = events(h, "task.user_accepted")[-1].data["user_source"]
    assert source["channel"] == "status-app"
    assert source["message_ref"].startswith("status-app:")
    assert "T001 revision 1" in source["text"]


@mark.use_case("UC-DIRECT-002")
@mark.transition("T23")
@mark.transition("T25")
def test_the_status_app_rejects_and_cancels(h: Harness, app: App) -> None:
    h.complete("T001")
    h.register("T002")
    handed_back(h)
    app.cookie = f"tsw_status={app.token}"
    status, out = app.act("T001", "reject", revision=1)
    assert (status, out["reason"]) == (409, "invalid_input")
    status, out = app.act("T001", "reject", revision=1, reason="缺少导出")
    assert (status, out["event"]) == (200, "task.user_rejected")
    status, out = app.act("T002", "cancel", revision=1)
    assert (status, out["event"]) == (200, "task.cancelled")
    assert (h.status("T001"), h.status("T002")) == ("intake", "cancelled")
    # The rejected revision is now history on the card; the brief shows the current one.
    page = app.request("GET", "/")[2].decode("utf-8")
    card = re.search(r'<article [^>]*id="task-T001".*?</article>', page, re.S)
    assert card and "任务书 · revision 2" in card.group(0)
    assert "历史任务书（1）" in card.group(0) and "revision 1 · 登记" in card.group(0)


@mark.use_case("UC-DIRECT-002")
def test_the_status_app_serves_linked_reports_and_nothing_else(h: Harness, app: App) -> None:
    app.cookie = f"tsw_status={app.token}"
    report = h.home.path("runs", "T009", "a1", "report.html")
    report.parent.mkdir(parents=True)
    report.write_text("<p>报告</p>", encoding="utf-8")
    status, headers, body = app.request("GET", "/runs/T009/a1/report.html")
    assert (status, body.decode("utf-8")) == (200, "<p>报告</p>")
    assert headers["Content-Type"].startswith("text/html")
    assert "script-src 'none'" in headers["Content-Security-Policy"]
    brief = h.home.path("runs", "T009", "a1", "brief.md")
    brief.write_text("# 任务书", encoding="utf-8")
    status, headers, body = app.request("GET", "/runs/T009/a1/brief.md")
    assert (status, headers["Content-Type"]) == (200, "text/plain; charset=utf-8")
    assert body.decode("utf-8") == "# 任务书"
    for path in ("/tsw-home.json", "/config/task-steward.json", "/runs/../tsw-home.json"):
        assert app.request("GET", path)[0] == 404, path
    assert app.request("GET", "/runs/T009/a1/report.html", headers={"Cookie": ""})[0] == 403
