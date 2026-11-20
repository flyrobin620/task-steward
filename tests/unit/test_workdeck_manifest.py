"""The Dashboard's workdeck plugin manifest and the standard session attribute (UC-DIRECT-009,
DN006 §8.10, CC006 §13; T309)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tsw.errors import TswError
from tsw.home import Home
from tsw.host import workdeck
from tsw.host.guard import bash_problem
from tsw.host.session_labels import LABELS_PATH
from tsw.host.status import session_link

mark = pytest.mark
UC = "UC-DIRECT-009"


@mark.use_case(UC)
def test_the_manifest_starts_this_dashboard_and_signs_in_with_its_token(tmp_path: Path) -> None:
    python = tmp_path / "py" / "python.exe"
    home, checkout = tmp_path / "data", tmp_path / "product"
    value = workdeck.manifest(home, python=str(python), checkout=checkout)
    assert value == {
        "workdeckPlugin": 1,
        "id": "tsw",
        "name": "TaskSteward Dashboard",
        "description": workdeck.DESCRIPTION,
        "spawn": {
            "command": str(python),
            "args": [
                "-X",
                "utf8",
                "-m",
                "tsw",
                "status",
                "serve",
                "--no-open",
                "--home",
                str(home.resolve()),
            ],
            "cwd": str(checkout.resolve()),
        },
        "auth": {"type": "query-token"},
        "labels": {"path": LABELS_PATH, "refreshSeconds": 10},
    }
    # The standard attribute marks the session links: no sessionLinks, no credential, no env.
    assert "sessionLinks" not in value and "url" not in value and "env" not in value["spawn"]
    assert "token" not in workdeck.to_json(value).lower().replace("query-token", "")


@mark.use_case(UC)
def test_the_hosted_services_windowless_interpreter_is_swapped_for_its_console_sibling(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "py"
    folder.mkdir()
    (folder / "python.exe").write_bytes(b"")
    (folder / "pythonw.exe").write_bytes(b"")
    assert workdeck.console_python(str(folder / "pythonw.exe")) == str(folder / "python.exe")
    assert workdeck.console_python(str(folder / "python.exe")) == str(folder / "python.exe")
    # Without a sibling the interpreter is kept as it is.
    lone = tmp_path / "lone" / "pythonw.exe"
    assert workdeck.console_python(str(lone)) == str(lone)


@mark.use_case(UC)
def test_the_id_follows_workdecks_rule_and_the_digest_is_stable(tmp_path: Path) -> None:
    def made(home: str, plugin_id: str = "tsw") -> dict[str, Any]:
        return workdeck.manifest(
            tmp_path / home, python="python.exe", checkout=tmp_path / "p", plugin_id=plugin_id
        )

    assert made("d", "tsw-2")["id"] == "tsw-2"
    for bad in ("", "TSW", "-tsw", "a" * 33, "tsw_2", "tsw/x"):
        with pytest.raises(TswError) as caught:
            made("d", bad)
        assert caught.value.details["reason"] == "invalid_plugin_id"
    first = made("d")
    same = dict(reversed(list(made("d").items())))
    assert workdeck.digest(first) == workdeck.digest(same)
    assert workdeck.digest(first) != workdeck.digest(made("e"))


@mark.use_case(UC)
def test_every_session_link_carries_the_standard_attribute() -> None:
    attempt = {"attempt": "a1", "session_ref": "local_ab-1"}
    for adapter in ("claude-app", "codex-app-server"):
        link = session_link("T9", {**attempt, "adapter": adapter})
        assert 'data-tsw-session="local_ab-1"' in link
        assert 'data-workdeck-session="local_ab-1"' in link
    hostile = session_link("T9", {"attempt": "a1", "session_ref": '"><x'})
    assert 'data-workdeck-session="&quot;&gt;&lt;x"' in hostile
    assert session_link("T9", {"attempt": "a1", "session_ref": None}) == ""


@mark.use_case(UC)
def test_a_session_may_print_the_manifest_but_not_install_the_dashboard(tmp_path: Path) -> None:
    home = Home(tmp_path / "home")
    allowed = "tsw dashboard workdeck-manifest"
    for worker in (True, False):

        def problem(command: str, worker: bool = worker) -> str | None:
            return bash_problem(
                command, home=home, push_allowed=True, pr_allowed=True, worker=worker
            )

        assert problem(allowed) is None
        assert problem("python -m tsw dashboard workdeck-manifest --id x") is None
        assert problem("tsw dashboard install") is not None


@mark.use_case(UC)
def test_describe_matches_what_workdeck_lists_for_a_spawned_plugin(tmp_path: Path) -> None:
    value = workdeck.manifest(tmp_path / "d", python="py.exe", checkout=tmp_path / "p")
    # workdeck's describeUpstream: `starts: ${[command, ...args].join(' ')} (in ${cwd})`.
    args = " ".join(value["spawn"]["args"])
    assert workdeck.describe(value) == f"starts: py.exe {args} (in {(tmp_path / 'p').resolve()})"
    bare = {"spawn": {"command": "x"}}
    assert workdeck.describe(bare) == "starts: x"
