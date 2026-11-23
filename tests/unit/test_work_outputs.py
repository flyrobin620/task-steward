"""Links to the registered outputs in ``work/`` (UC-DIRECT-010, DN006 §8.10, T341)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from tests.support import REPO_ROOT
from tsw.host.status import link_for, outputs_by_task, work_parts
from tsw.host.task_view import _outputs
from tsw.projection import SCHEMA_SQL

mark = pytest.mark
DN015 = "work/T339/a1/workdeck/docs/design/html/zh-CN/DN015-github-copilot-provider.html"


@mark.use_case("UC-DIRECT-010")
def test_a_work_path_is_its_segments_only_when_every_segment_is_plain() -> None:
    assert work_parts(DN015) == DN015.split("/")
    assert work_parts("work/T001/report.md") == ["work", "T001", "report.md"]
    for ref in (
        "work/T001",  # no file below the task
        "work/T001/",  # a directory
        "work//T001/x.html",
        "work/T001/./x.html",
        "work/T001/../T002/x.html",
        "work/T001/a1/..",
        "work/T001/a1\\x.html",
        "work/T001/a1/x.html:stream",
        "work/C:/x.html",
        "work/T001/a\x00/x.html",
        "work/T001/a\x1f/x.html",
        "work/T001/a\x7f/x.html",
        "/work/T001/x.html",
        "runs/T001/a1/brief.md",
        "workspace/T001/x.html",
        "",
    ):
        assert work_parts(ref) is None, ref


@mark.use_case("UC-DIRECT-010")
def test_a_registered_work_output_links_like_a_report() -> None:
    assert link_for(DN015) == "../" + DN015
    assert link_for("work/T001/a1/设计 草稿.md") == (
        "../work/T001/a1/%E8%AE%BE%E8%AE%A1%20%E8%8D%89%E7%A8%BF.md"
    )
    assert link_for("runs/T001/a1/result.html") == "../runs/T001/a1/result.html"
    # From the app's board at its root, the same files without going up.
    assert link_for(DN015, "") == DN015
    assert link_for("runs/T001/a1/result.html", "") == "runs/T001/a1/result.html"
    assert link_for("legacy://old/runs/T1/r.html", "") == "legacy/old/runs/T1/r.html"
    assert link_for("https://example.test/pr/1", "") == "https://example.test/pr/1"
    for ref in ("work/T001/../../tsw-home.json", "work/T001", "C:/work/T001/x.html", "x.html"):
        assert link_for(ref) is None, ref


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for output_id, ref in (
        ("O-T339-design-zh", DN015),
        ("O-T339-odd", "work/T339/a1/../../secret.txt"),
    ):
        db.execute(
            "INSERT INTO outputs(task_id, output_id, type, ref, role, delivery, verified_at, "
            "evidence) VALUES('T339', ?, 'design', ?, NULL, 'draft', NULL, '[]')",
            (output_id, ref),
        )
    yield db
    db.close()


@mark.use_case("UC-DIRECT-010")
def test_the_board_and_the_task_view_link_the_work_output(conn: sqlite3.Connection) -> None:
    card = "".join(o.html("o") for o in outputs_by_task(conn)["T339"])
    assert f'<a href="../{DN015}">O-T339-design-zh</a>' in card
    assert "O-T339-odd" in card and "secret.txt" not in card  # text, no link
    board = "".join(o.html("o", "") for o in outputs_by_task(conn)["T339"])
    assert f'<a href="{DN015}">O-T339-design-zh</a>' in board
    view, count = _outputs(conn, "T339")
    assert count == 2
    assert f'<a href="../{DN015}">O-T339-design-zh</a>' in view
    assert 'href="../work/T339/a1/../' not in view


@mark.use_case("UC-DIRECT-010")
def test_the_worker_registers_the_files_a_question_needs_before_asking() -> None:
    rules = (REPO_ROOT / "plugin" / "roles" / "worker.md").read_text(encoding="utf-8")
    ask = next(line for line in rules.splitlines() if line.startswith("- **问用户**"))
    assert "提问前先用 `output_link` 把这些文件登记为产出" in ask
    assert "问题中写明产出编号" in ask and "DN006 §8.10" in ask
