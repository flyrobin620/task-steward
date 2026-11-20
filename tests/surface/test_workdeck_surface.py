"""The Dashboard's workdeck manifest through the command line, the served app and the hook
(UC-DIRECT-009, DN006 §8.10, CC006 §13; T309): separate processes, real serialization."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import REPO_ROOT, child_env, run_json, run_tsw
from tests.surface.test_direct_surface import App, _serve
from tsw.home import Home
from tsw.host import workdeck

mark = pytest.mark
UC = "UC-DIRECT-009"


@pytest.fixture
def board(home: Home) -> Iterator[App]:
    yield from _serve("dashboard")


@mark.use_case(UC)
def test_the_command_prints_the_manifest_for_this_installation(home: Home) -> None:
    proc = run_tsw("dashboard", "workdeck-manifest")
    assert proc.returncode == 0, proc.stderr
    value = json.loads(proc.stdout)
    assert value == workdeck.manifest(home.root, python=sys.executable, checkout=REPO_ROOT)
    proc = run_tsw("dashboard", "workdeck-manifest", "--id", "tsw-work")
    assert json.loads(proc.stdout)["id"] == "tsw-work"
    code, out = run_json("dashboard", "workdeck-manifest", "--id", "TSW")
    assert code == 3 and out["details"]["reason"] == "invalid_plugin_id"


@mark.use_case(UC)
def test_out_writes_a_file_workdeck_reads_and_is_refused_in_a_session(
    home: Home, tmp_path: Path
) -> None:
    target = tmp_path / "tsw-plugin.json"
    code, out = run_json("dashboard", "workdeck-manifest", "--out", str(target))
    assert code == 0, out
    assert out["written"] == str(target.resolve()) and out["id"] == "tsw"
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    assert json.loads(raw.decode("utf-8"))["workdeckPlugin"] == 1
    # In a host session it prints but does not write: --out is a write the guard cannot see.
    other = tmp_path / "other.json"
    session = child_env(CLAUDECODE="1")
    code, out = run_json("dashboard", "workdeck-manifest", "--out", str(other), env=session)
    assert code == 3 and not other.exists()
    proc = run_tsw("dashboard", "workdeck-manifest", env=session)
    assert proc.returncode == 0 and json.loads(proc.stdout)["id"] == "tsw"


@mark.use_case(UC)
def test_the_running_dashboard_serves_the_manifest_without_a_token(home: Home, board: App) -> None:
    status, headers, raw = board.request("GET", workdeck.WELL_KNOWN_PATH)
    assert (status, headers["Content-Type"]) == (200, "application/json; charset=utf-8")
    value = json.loads(raw)
    assert value["spawn"]["args"][-1] == str(home.root.resolve())
    assert value["auth"] == {"type": "query-token"} and board.token not in raw.decode("utf-8")
    foreign = {"Host": f"evil.example:{board.port}"}
    assert board.request("GET", workdeck.WELL_KNOWN_PATH, headers=foreign)[0] == 403
