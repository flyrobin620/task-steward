"""Which ``work/`` files the Dashboard serves, on a real ledger and file system (UC-DIRECT-010,
DN006 §8.10, T341)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.machine_harness import Harness
from tsw.home import Home
from tsw.host.status_app import NOT_REGISTERED, WORK_GONE, StatusApp

mark = pytest.mark
PAGE = "work/T001/a1/repo/docs/html/zh-CN/DN015.html"


def link_dir(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (no privilege needed), else a symlink."""
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def register(h: Harness, task_id: str, output_id: str, ref: str) -> None:
    h.run(
        "output_link",
        task_id=task_id,
        output_id=output_id,
        type="design",
        ref=ref,
        delivery="draft",
        evidence=["test"],
        verified_at="2026-11-23T00:00:00Z",
    )


def write(home: Home, ref: str, text: str = "x") -> Path:
    path = home.root.joinpath(*ref.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def h(home: Home) -> Harness:
    harness = Harness(home)
    harness.register("T001")
    harness.register("T002")
    register(harness, "T001", "O-T001-zh", PAGE)
    register(harness, "T001", "O-T001-en", "work/T001/a1/repo/docs/html/en/DN015.html")
    register(harness, "T001", "O-T001-notes", "work/T001/a1/drafts/notes.md")
    # Another task cannot hand out T001's files, nor its own ones by a crooked path.
    register(harness, "T002", "O-T002-stolen", "work/T001/a1/secret.html")
    register(harness, "T002", "O-T002-crooked", "work/T002/a1/../../T001/a1/secret.html")
    for ref in (
        PAGE,
        "work/T001/a1/repo/docs/html/en/DN015.html",
        "work/T001/a1/repo/docs/html/zh-CN/style.css",
        "work/T001/a1/repo/docs/html/zh-CN/img/figure.svg",
        "work/T001/a1/repo/docs/html/zh-CN/img/LOGO.PNG",
        "work/T001/a1/repo/docs/html/zh-CN/DN014.html",
        "work/T001/a1/repo/docs/html/zh-CN/app.js",
        "work/T001/a1/repo/docs/html/zh-CN/data.json",
        "work/T001/a1/repo/docs/html/shared.css",
        "work/T001/a1/repo/.env",
        "work/T001/a1/secret.html",
        "work/T001/a1/drafts/notes.md",
    ):
        write(home, ref)
    return harness


@mark.use_case("UC-DIRECT-010")
def test_only_registered_outputs_and_their_page_resources_are_served(h: Harness) -> None:
    app = StatusApp(h.home)
    root = h.home.root.resolve()
    for ref in (
        PAGE,
        "work/T001/a1/repo/docs/html/en/DN015.html",
        "work/T001/a1/drafts/notes.md",
        "work/T001/a1/repo/docs/html/zh-CN/style.css",
        "work/T001/a1/repo/docs/html/zh-CN/img/figure.svg",  # a subdirectory
        "work/T001/a1/repo/docs/html/zh-CN/img/LOGO.PNG",  # the extension in any case
    ):
        found, why = app.work_file(ref)
        assert (found, why) == (root.joinpath(*ref.split("/")), ""), ref
    for ref in (
        "work/T001/a1/repo/docs/html/zh-CN/DN014.html",  # a page beside it, not registered
        "work/T001/a1/repo/docs/html/zh-CN/app.js",  # not a style, image or font
        "work/T001/a1/repo/docs/html/zh-CN/data.json",
        "work/T001/a1/repo/docs/html/shared.css",  # above the output's directory
        "work/T001/a1/repo/.env",
        "work/T001/a1/secret.html",  # registered only by another task
        "work/T002/a1/../../T001/a1/secret.html",
        "work/T001/a1/repo/docs/html/zh-CN/../../html/zh-CN/DN015.html",
        "work/T001/a1/repo/docs/html/zh-CN/",
        "work/T001/a1/repo/docs/html/zh-CN",
        "work/T001/a1/drafts/notes.md:stream",
        "work/T001/a1/repo\\docs\\html\\zh-CN\\style.css",
        "runs/T001/a1/brief.md",
    ):
        assert app.work_file(ref) == (None, NOT_REGISTERED), ref
    # Registered, but the work directory was cleaned up.
    h.home.root.joinpath(*PAGE.split("/")).unlink()
    assert app.work_file(PAGE) == (None, WORK_GONE)
    assert app.work_file("work/T001/a1/repo/docs/html/zh-CN/style.css")[1] == ""


@mark.use_case("UC-DIRECT-010")
def test_a_link_or_junction_out_of_range_is_refused(h: Harness, tmp_path: Path) -> None:
    app = StatusApp(h.home)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.css").write_text("secret", encoding="utf-8")
    (outside / "leak.html").write_text("secret", encoding="utf-8")
    # A directory link inside the output's directory, pointing outside the data directory.
    link_dir(h.home.root / "work/T001/a1/repo/docs/html/zh-CN/assets", outside)
    assert app.work_file("work/T001/a1/repo/docs/html/zh-CN/assets/leak.css") == (
        None,
        NOT_REGISTERED,
    )
    # A registered output reached through a link to outside its task's work directory.
    link_dir(h.home.root / "work/T001/a1/linked", outside)
    register(h, "T001", "O-T001-linked", "work/T001/a1/linked/leak.html")
    assert app.work_file("work/T001/a1/linked/leak.html") == (None, NOT_REGISTERED)
    # A link to another task's work directory is out of range too.
    write(h.home, "work/T002/a1/other.css")
    link_dir(h.home.root / "work/T001/a1/repo/docs/html/zh-CN/t2", h.home.root / "work/T002/a1")
    assert app.work_file("work/T001/a1/repo/docs/html/zh-CN/t2/other.css") == (
        None,
        NOT_REGISTERED,
    )
    # A link that stays inside the output's directory is followed.
    link_dir(
        h.home.root / "work/T001/a1/repo/docs/html/zh-CN/pics",
        h.home.root / "work/T001/a1/repo/docs/html/zh-CN/img",
    )
    found, why = app.work_file("work/T001/a1/repo/docs/html/zh-CN/pics/figure.svg")
    assert why == "" and found is not None and found.name == "figure.svg"


@mark.use_case("UC-DIRECT-010")
def test_a_file_symlink_out_of_range_is_refused(h: Harness, tmp_path: Path) -> None:
    secret = tmp_path / "id_rsa.png"
    secret.write_text("secret", encoding="utf-8")
    page = h.home.root.joinpath(*PAGE.split("/"))
    page.unlink()
    try:
        (page.parent / "leak.png").symlink_to(secret)
        page.symlink_to(secret)
    except OSError as exc:  # pragma: no cover - Windows without the symlink privilege
        pytest.skip(f"cannot create a symbolic link here: {exc}")
    app = StatusApp(h.home)
    assert app.work_file("work/T001/a1/repo/docs/html/zh-CN/leak.png") == (None, NOT_REGISTERED)
    assert app.work_file(PAGE) == (None, NOT_REGISTERED)


@mark.use_case("UC-DIRECT-010")
def test_a_resource_link_must_lead_to_a_resource(h: Harness) -> None:
    folder = h.home.root.joinpath(*PAGE.split("/")).parent
    try:
        (folder / "style2.css").symlink_to(folder / "DN014.html")
        (folder / "secret.png").symlink_to(h.home.root / "work/T001/a1/repo/.env")
        (folder / "alias.png").symlink_to(folder / "img" / "LOGO.PNG")
    except OSError as exc:  # pragma: no cover - Windows without the symlink privilege
        pytest.skip(f"cannot create a symbolic link here: {exc}")
    app = StatusApp(h.home)
    base = PAGE.rsplit("/", 1)[0]
    assert app.work_file(f"{base}/style2.css") == (None, NOT_REGISTERED)
    assert app.work_file(f"{base}/secret.png") == (None, NOT_REGISTERED)
    found, why = app.work_file(f"{base}/alias.png")
    assert why == "" and found is not None and found.name == "LOGO.PNG"
