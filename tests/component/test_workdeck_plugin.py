"""The Dashboard as a workdeck plugin on real files, sockets and subprocesses (T309).

UC-DIRECT-009 (DN006 §8.10): the Dashboard serves its manifest at
``/.well-known/workdeck-plugin.json``. UC-SETUP-004 (DN009 §5 step 7, CC006 §13): ``tsw setup``
hands the manifest to ``workdeck plugin add``. workdeck is the stand-in
``tests/fixtures/fake_workdeck.py`` (simulated); Claude Code's plugin commands, the user ``Path``
and Task Scheduler are stand-ins as in ``test_setup.py``.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.setup_harness import (
    fake_claude,
    fake_workdeck,
    make_product,
    record,
    run_setup,
    setup_env,
    workdeck_calls,
    write_answers,
)
from tsw.home import Home
from tsw.host import workdeck
from tsw.host.status_app import StatusApp, make_server
from tsw.host.version import REPO_ROOT

mark = pytest.mark
UC = "UC-DIRECT-009"
SETUP = "UC-SETUP-004"


@pytest.fixture
def served(home: Home) -> Iterator[StatusApp]:
    app = StatusApp(home, token="tok-w")
    server = make_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app
    finally:
        server.shutdown()
        server.server_close()


def get(app: StatusApp, path: str, host: str | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{app.port}"})
    response = conn.getresponse()
    out = (response.status, dict(response.getheaders()), response.read())
    conn.close()
    return out


@mark.use_case(UC)
def test_the_dashboard_serves_its_manifest_without_a_token(home: Home, served: StatusApp) -> None:
    status, headers, body = get(served, workdeck.WELL_KNOWN_PATH)
    assert status == 200 and headers["Content-Type"] == "application/json; charset=utf-8"
    value = json.loads(body)
    assert value == workdeck.manifest(home.root, python=sys.executable, checkout=REPO_ROOT)
    assert value["spawn"]["args"][-2:] == ["--home", str(home.root.resolve())]
    # No token, no cookie and no CORS header: only what the manifest describes.
    assert "tok-w" not in body.decode("utf-8")
    assert "Set-Cookie" not in headers and "Access-Control-Allow-Origin" not in headers
    # The Host check still applies (DNS rebinding), and the board still needs the token.
    assert get(served, workdeck.WELL_KNOWN_PATH, host=f"evil.example:{served.port}")[0] == 403
    assert get(served, "/")[0] == 403


# ---------------------------------------------------------------------- tsw setup


def install(product: Path, answers: Path, env: dict[str, str], claude: Path) -> dict[str, Any]:
    code, out, text = run_setup(
        product,
        "install",
        "--answers",
        str(answers),
        "--claude",
        str(claude),
        "--no-browser",
        env=env,
    )
    assert out, text
    out["_code"] = code
    return out


def step(out: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in out["steps"] if s["step"] == name)


def entries(config: Path) -> dict[str, Any]:
    data = json.loads(config.read_text(encoding="utf-8"))
    return {p["id"]: p for p in data["plugins"]}


@mark.use_case(SETUP)
def test_setup_adds_replaces_and_removes_the_workdeck_plugin(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    config = fake_workdeck(tmp_path, env)
    # The user's own entry under the same id (T306 wrote one by hand).
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"port": 1, "plugins": [{"id": "tsw", "spawn": {"command": "tsw"}}]}),
        encoding="utf-8",
    )
    answers = write_answers(tmp_path, home, workdeck=True)
    code, planned, text = run_setup(
        product, "plan", "--answers", str(answers), "--claude", str(claude), env=env
    )
    assert code == 0, text
    # Before the data directory exists the plan already says what will start and be replaced,
    # in the actions the terminal shows before asking (PR #65 review).
    planned_step = step(planned, "workdeck")
    assert planned_step["state"] == "todo" and planned_step["replaces"] is True
    assert planned_step["starts"].endswith(f"--home {home.resolve()}")
    assert "替换 workdeck 中已有的插件 tsw" in planned_step["actions"][0]
    out = install(product, answers, env, claude)
    assert out["_code"] == 0, out
    done = step(out, "workdeck")
    assert done["state"] == "done" and done["replaces"] is True
    calls = workdeck_calls(tmp_path)
    assert calls[:2] == ["plugin list", "plugin list"]  # the plan's, then the install's
    added = calls[2].split(" ")
    assert added[:2] == ["plugin", "add"] and added[-1] == "--replace"
    assert not Path(added[2]).exists()  # the temporary manifest is gone
    wanted = workdeck.manifest(home, python=sys.executable, checkout=product)
    entry = entries(config)["tsw"]
    assert entry == {k: v for k, v in wanted.items() if k != "workdeckPlugin"}
    assert json.loads(config.read_text(encoding="utf-8"))["port"] == 1  # the rest is kept
    owned = record(env)["steps"]["workdeck"]
    assert owned["added"] is True and owned["existed_before"] is True
    assert owned["digest"] == workdeck.digest(wanted)
    checks = {c["check"]: c for c in out["verify"]["checks"]}
    assert checks["workdeck"]["ok"] is True and out["verify"]["ok"] is True
    # A rerun with the same manifest does not call workdeck again.
    again = install(product, answers, env, claude)
    assert step(again, "workdeck")["state"] == "ok"
    assert len(workdeck_calls(tmp_path)) == 3
    # Uninstall removes the entry the install added; the data directory stays.
    code, removed, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    assert removed["removed"]["workdeck"]["ok"] is True
    assert workdeck_calls(tmp_path)[-1] == "plugin remove tsw"
    assert "tsw" not in entries(config) and (home / "tsw-home.json").is_file()


@mark.use_case(SETUP)
def test_a_workdeck_failure_is_reported_and_the_install_goes_on(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path, FAKE_WORKDECK_FAIL="add")
    config = fake_workdeck(tmp_path, env)
    out = install(product, write_answers(tmp_path, home, workdeck=True), env, claude)
    failed = step(out, "workdeck")
    assert failed["state"] == "failed" and "workdeck plugin add" in failed["problem"]
    assert "workdeck-manifest --out" in failed["manual"]
    assert any("failed (stand-in)" in line for line in failed["output"])
    assert record(env)["status"] == "installed" and not config.exists()
    # verify says so; nothing else failed.
    assert out["verify"]["failed"] == ["workdeck"] and out["_code"] == 3
    # Fixed: the rerun adds it.
    env.pop("FAKE_WORKDECK_FAIL")
    again = install(product, write_answers(tmp_path, home, workdeck=True), env, claude)
    assert step(again, "workdeck")["state"] == "done" and again["verify"]["ok"] is True
    assert "tsw" in entries(config)


@mark.use_case(SETUP)
def test_no_workdeck_and_older_saved_answers_leave_workdeck_alone(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    # Without the command the answer defaults to no and the step is skipped.
    out = install(product, write_answers(tmp_path, home), env, claude)
    assert out["_code"] == 0, out
    assert step(out, "workdeck")["state"] == "skipped"
    assert "workdeck" not in record(env)["steps"]
    # Answers saved before the question existed: a bare rerun does not wire workdeck, even
    # where workdeck is installed now.
    saved = Path(env["TSW_STATE_DIR"]) / "setup-answers.json"
    old = json.loads(saved.read_text(encoding="utf-8"))
    old.pop("workdeck", None)
    saved.write_text(json.dumps(old), encoding="utf-8")
    fake_workdeck(tmp_path, env)
    code, rerun, text = run_setup(product, "install", "--no-browser", env=env)
    assert code == 0, text
    assert step(rerun, "workdeck")["state"] == "skipped"
    assert workdeck_calls(tmp_path) == []
    # The same old answers given with --answers (review T309 r1 F1).
    given = tmp_path / "old-answers.json"
    given.write_text(json.dumps(old), encoding="utf-8")
    again = install(product, given, env, claude)
    assert again["_code"] == 0, again
    assert step(again, "workdeck")["state"] == "skipped"
    assert workdeck_calls(tmp_path) == []


@mark.use_case(SETUP)
def test_an_add_that_failed_after_workdeck_wrote_it_is_still_removed(tmp_path: Path) -> None:
    # workdeck writes its configuration, then its daemon does not come back (review T309 r1 F2).
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path, FAKE_WORKDECK_FAIL="restart")
    config = fake_workdeck(tmp_path, env)
    out = install(product, write_answers(tmp_path, home, workdeck=True), env, claude)
    assert step(out, "workdeck")["state"] == "failed"
    assert "tsw" in entries(config)
    owned = record(env)["steps"]["workdeck"]
    assert owned["added"] is True and owned["failed_at"]
    assert out["verify"]["failed"] == ["workdeck"]
    # A rerun: workdeck finds the entry unchanged and does not restart its daemon, which still
    # does not run the plugin; the step stays failed (review T309 r2 F3).
    env.pop("FAKE_WORKDECK_FAIL")
    answers = tmp_path / "answers.json"
    again = install(product, answers, env, claude)
    assert step(again, "workdeck")["state"] == "failed"
    assert "没有在运行" in step(again, "workdeck")["problem"]
    assert again["verify"]["failed"] == ["workdeck"]
    # Once the daemon runs it, the rerun clears the failure.
    env["FAKE_WORKDECK_RUNNING"] = "1"
    fixed = install(product, answers, env, claude)
    assert step(fixed, "workdeck")["state"] == "done" and fixed["verify"]["ok"] is True
    assert (
        "failed_at" not in record(env)["steps"]["workdeck"]
        or not record(env)["steps"]["workdeck"]["failed_at"]
    )
    code, removed, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    assert removed["removed"]["workdeck"]["ok"] is True
    assert "tsw" not in entries(config)


@mark.use_case(SETUP)
def test_uninstall_keeps_an_entry_the_user_changed_since(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    config = fake_workdeck(tmp_path, env)
    out = install(product, write_answers(tmp_path, home, workdeck=True), env, claude)
    assert out["_code"] == 0, out
    # The user points the tsw plugin elsewhere by hand.
    data = json.loads(config.read_text(encoding="utf-8"))
    data["plugins"] = [{"id": "tsw", "name": "mine", "spawn": {"command": "other"}}]
    config.write_text(json.dumps(data), encoding="utf-8")
    code, removed, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    assert "保留" in removed["removed"]["workdeck"]["note"]
    assert entries(config)["tsw"]["name"] == "mine"
    assert not any(call.startswith("plugin remove") for call in workdeck_calls(tmp_path))


@mark.use_case(SETUP)
def test_uninstall_removes_nothing_when_it_cannot_read_the_list(tmp_path: Path) -> None:
    # Without the list our entry cannot be told from the user's (review T309 r2 F4).
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    config = fake_workdeck(tmp_path, env)
    out = install(product, write_answers(tmp_path, home, workdeck=True), env, claude)
    assert out["_code"] == 0, out
    data = json.loads(config.read_text(encoding="utf-8"))
    data["plugins"] = [{"id": "tsw", "name": "mine", "spawn": {"command": "other"}}]
    config.write_text(json.dumps(data), encoding="utf-8")
    env["FAKE_WORKDECK_FAIL"] = "list"
    code, removed, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    result = removed["removed"]["workdeck"]
    assert result["ok"] is False and "workdeck plugin remove tsw" in result["note"]
    assert entries(config)["tsw"]["name"] == "mine"
    assert not any(call.startswith("plugin remove") for call in workdeck_calls(tmp_path))
    assert record(env)["steps"]["workdeck"]["added"] is True
