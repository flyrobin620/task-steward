from __future__ import annotations

import json
from pathlib import Path

import pytest

from tsw.errors import EXIT_CODES, ErrorCode, TswError
from tsw.faults import FaultInjected, fault
from tsw.home import (
    HOME_VERSION,
    MARKER,
    claim_sessions,
    init_home,
    open_home,
    resolve_home,
)


@pytest.mark.use_case("UC-HOME-001")
def test_resolve_prefers_environment(tmp_path: Path) -> None:
    env = {"TSW_HOME": str(tmp_path / "a")}
    assert resolve_home(env, repo_root=tmp_path, cwd=tmp_path) == tmp_path / "a"


@pytest.mark.use_case("UC-HOME-001")
def test_resolve_uses_repo_pointer_then_default(tmp_path: Path) -> None:
    pointer = tmp_path / ".local" / "tsw-home"
    pointer.parent.mkdir()
    pointer.write_text(str(tmp_path / "b") + "\n", encoding="utf-8")
    assert resolve_home({}, repo_root=tmp_path, cwd=tmp_path) == tmp_path / "b"
    pointer.write_text("", encoding="utf-8")
    assert resolve_home({}, repo_root=tmp_path, cwd=tmp_path) == Path("~/.tsw").expanduser()


@pytest.mark.use_case("UC-HOME-001")
def test_a_data_directory_that_claims_sessions_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session working inside a claiming data directory uses it whatever TSW_HOME says
    (decision 0003: no settings file in the session's folder); the nearest data directory
    decides, and one that does not claim leaves TSW_HOME in charge."""
    real, _ = init_home(tmp_path / "real")
    run, _ = init_home(tmp_path / "run")
    workdir = run.root / "work" / "T001" / "a1"
    workdir.mkdir(parents=True)
    env = {"TSW_HOME": str(real.root)}
    assert resolve_home(env, cwd=workdir) == real.root
    claim_sessions(run)
    claim_sessions(run)  # idempotent
    assert json.loads((run.root / MARKER).read_text(encoding="utf-8"))["claim_sessions"] is True
    assert resolve_home(env, cwd=workdir) == run.root
    assert resolve_home(env, cwd=run.root) == run.root
    assert resolve_home(env, cwd=tmp_path) == real.root
    # Without an explicit directory, the current directory is the session's.
    monkeypatch.chdir(workdir)
    assert resolve_home(env) == run.root
    # A data directory nested in the claiming one decides for its own sessions.
    inner, _ = init_home(workdir / "inner")
    assert resolve_home(env, cwd=inner.root) == real.root


@pytest.mark.use_case("UC-HOME-001")
def test_init_creates_layout_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "home"
    home, created = init_home(root)
    assert created
    assert (root / "ledger" / "events").is_dir()
    assert json.loads((root / MARKER).read_text(encoding="utf-8"))["home_version"] == HOME_VERSION
    again, created_again = init_home(root)
    assert again == home
    assert not created_again


@pytest.mark.use_case("UC-HOME-001")
def test_init_refuses_a_non_empty_foreign_directory(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(TswError) as info:
        init_home(tmp_path)
    assert info.value.code is ErrorCode.REJECTED


@pytest.mark.use_case("UC-HOME-001")
def test_open_rejects_unknown_home_version(tmp_path: Path) -> None:
    init_home(tmp_path / "h")
    (tmp_path / "h" / MARKER).write_text(json.dumps({"home_version": 99}), encoding="utf-8")
    with pytest.raises(TswError) as info:
        open_home(tmp_path / "h")
    assert info.value.details["home_version"] == 99


@pytest.mark.use_case("UC-LEDGER-001")
def test_every_error_code_has_a_distinct_exit_code() -> None:
    assert set(EXIT_CODES) == set(ErrorCode)
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES)
    assert 0 not in EXIT_CODES.values()
    assert 2 not in EXIT_CODES.values()  # reserved for usage errors


@pytest.mark.use_case("UC-LEDGER-001")
def test_faults_are_inert_without_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    fault("anything")
    monkeypatch.setenv("TSW_TEST_FAULTS", "other,point")
    with pytest.raises(FaultInjected):
        fault("point")
    fault("not-listed")
