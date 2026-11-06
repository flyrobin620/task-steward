"""``tsw import orchestrator-agent`` as a separate process."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.import_fixture import build
from tests.support import run_json

pytestmark = pytest.mark.use_case("UC-IMPORT-001")


def test_dry_run_then_import_from_the_command_line(tmp_path: Path) -> None:
    code, _ = run_json("init")
    assert code == 0
    _, snapshot = build(tmp_path)
    code, dry = run_json("import", "orchestrator-agent", "--snapshot", str(snapshot), "--dry-run")
    assert code == 0 and dry["dry_run"] and dry["tasks"] == 8
    code, done = run_json("import", "orchestrator-agent", "--snapshot", str(snapshot))
    assert code == 0 and done["imported"] == 8
    code, listed = run_json("task", "list")
    assert code == 0
    statuses = {t["task_id"]: t["status"] for t in listed["tasks"]}
    assert statuses["T003"] == "accepted" and statuses["T005"] == "intake"
    code, again = run_json("import", "orchestrator-agent", "--snapshot", str(snapshot))
    assert code == 0 and again["imported"] == 0
    code, missing = run_json(
        "import", "orchestrator-agent", "--snapshot", str(tmp_path / "nowhere")
    )
    assert code == 3 and missing["details"]["reason"] == "snapshot_unreadable"
