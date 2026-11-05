from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import run_json, run_tsw


@pytest.fixture
def initialized() -> Path:
    code, out = run_json("init")
    assert code == 0
    return Path(out["home"])


@pytest.mark.use_case("UC-CONFIG-001")
def test_config_show_and_validate(initialized: Path) -> None:
    code, out = run_json("config", "validate")
    assert (code, out["ok"], out["problems"]) == (0, True, [])
    code, shown = run_json("config", "show", "--effective")
    assert code == 0
    assert shown["values"]["roles"]["reviewer"]["model"] == "gpt-6-astra"
    assert shown["sources"]["roles.reviewer.model"] == "default"


@pytest.mark.use_case("UC-CONFIG-001")
def test_invalid_config_refuses_writes_but_not_reads(initialized: Path) -> None:
    (initialized / "config" / "task-steward.json").write_text(
        json.dumps({"version": 1, "bogus": True}), encoding="utf-8"
    )
    proc = run_tsw("config", "validate", "--json")
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["problems"] == ["bogus: 未知字段"]
    code, err = run_json(
        "task",
        "register",
        "--title",
        "t",
        "--repo",
        "r",
        "--kind",
        "code",
        "--goal",
        "g",
        "--acceptance",
        "a",
    )
    assert (code, err["details"]["reason"]) == (3, "config_invalid")
    code, listing = run_json("task", "list")
    assert (code, listing["tasks"]) == (0, [])


@pytest.mark.use_case("UC-LEASE-001")
@pytest.mark.transition("G-LEASE")
def test_lease_acquire_show_takeover_release(initialized: Path) -> None:
    code, out = run_json("lease", "show")
    assert (code, out["lease"]) == (0, None)
    code, out = run_json("lease", "acquire", "--session", "s1", "--machine", "m1")
    assert code == 0
    code, err = run_json("lease", "acquire", "--session", "s2", "--machine", "m1")
    assert (code, err["details"]["reason"]) == (3, "lease_held")
    code, err = run_json(
        "lease",
        "takeover",
        "--session",
        "s2",
        "--machine",
        "m1",
        "--from-session",
        "s1",
        "--user-source",
        "{}",
    )
    assert (code, err["details"]["reason"]) == (3, "user_source_missing")
    source = json.dumps(
        {"channel": "main-session", "principal": "owner", "message_ref": "m", "text": "接管"},
        ensure_ascii=False,
    )
    code, out = run_json(
        "lease",
        "takeover",
        "--session",
        "s2",
        "--machine",
        "m1",
        "--from-session",
        "s1",
        "--user-source",
        source,
    )
    assert code == 0
    code, shown = run_json("lease", "show")
    assert shown["lease"]["holder_session"] == "s2"
    assert shown["lease"]["expired"] is False
    code, _ = run_json("lease", "release", "--session", "s2", "--machine", "m1")
    assert code == 0
    code, shown = run_json("lease", "show")
    assert shown["lease"] is None
