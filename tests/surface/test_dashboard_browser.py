"""The Dashboard page in a real browser, at a wide and a narrow window (DN006 §8.10, T353).

The page is the ``views/STATUS.html`` tsw writes after each commit; a headless Edge or Chrome
opens a copy of it with ``tests/fixtures/dashboard_check.js`` added after the page's own script
(the page's Content-Security-Policy is kept, with the check's hash added to it). The check
clicks, scrolls, drags and measures in the page and writes what it saw into the page, which
the browser prints (``--dump-dom``). The layout is the browser's own: nothing is simulated
but the input events.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tsw.home import Home

mark = pytest.mark

CHECK = Path(__file__).resolve().parents[1] / "fixtures" / "dashboard_check.js"
# Windows runners (CI and the developers' machines) have Edge; Chrome or Chromium also work.
# The test environment points ProgramFiles at the test directory, so the folders are literal.
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
REPOS = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")
NAMES = ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium", "chromium-browser")


def browser() -> str:
    """The browser to drive: ``TSW_TEST_BROWSER``, else Edge or Chrome where they install."""
    chosen = os.environ.get("TSW_TEST_BROWSER")
    if chosen:
        return chosen
    for candidate in BROWSERS:
        if Path(candidate).is_file():
            return candidate
    for name in NAMES:
        found = shutil.which(name)
        if found:
            return found
    pytest.fail("没有找到 Edge 或 Chrome：安装其一，或用 TSW_TEST_BROWSER 指定浏览器")


@pytest.fixture
def h(home: Home) -> Harness:
    repos = {name: {"path": f"~/src/{name}"} for name in REPOS}
    return Harness(home, {"repositories": repos})


def board(h: Harness) -> Path:
    """Eight repositories with a chain of dependencies each: wider and taller than a window."""
    lanes = len(REPOS)
    for n in range(1, 6 * lanes + 1):
        h.register(
            f"T{n:03d}", repo=REPOS[n % lanes], title=f"任务 {n} 的标题稍微长一些，用来占两行"
        )
    for n in range(1, 6 * lanes + 1):
        h.plan(f"T{n:03d}", [f"T{n - lanes:03d}"] if n > lanes else [])
    for n in (6 * lanes - 2, 6 * lanes - 1, 6 * lanes):
        h.run("task_cancel", task_id=f"T{n:03d}", user_source=h.source(f"T{n:03d}", 1, "cancel"))
    return h.home.root / "views" / "STATUS.html"


def with_check(page: str) -> str:
    """The page with the check after its own script, and the check's hash in its policy."""
    script = CHECK.read_text(encoding="utf-8")
    assert "</" not in script
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    policy = re.search(r"script-src ([^;]*);", html.unescape(page))
    assert policy, "the page has a script policy"
    page = page.replace(
        "script-src &#x27;sha256-", f"script-src &#x27;sha256-{digest}&#x27; &#x27;sha256-", 1
    )
    return page.replace("</body>", f"<script>{script}</script></body>", 1)


def look(page: Path, tmp: Path, mode: str, size: tuple[int, int]) -> dict[str, Any]:
    target = tmp / f"page-{mode}.html"
    target.write_text(with_check(page.read_text(encoding="utf-8")), encoding="utf-8")
    profile = tmp / f"browser-{mode}"
    done = subprocess.run(
        [
            browser(),
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            f"--window-size={size[0]},{size[1]}",
            "--virtual-time-budget=4000",
            "--dump-dom",
            f"{target.as_uri()}?mode={mode}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    found = re.search(r'<pre id="check-result">(.*?)</pre>', done.stdout, re.S)
    assert found, f"浏览器没有写出结果（退出码 {done.returncode}）：{done.stderr[-2000:]}"
    return json.loads(html.unescape(found.group(1)))


def failed(result: dict[str, Any]) -> list[str]:
    return [f"{c['name']}: {c['detail']}" for c in result["checks"] if not c["ok"]]


@mark.use_case("UC-DIRECT-011")
def test_the_page_in_a_browser_wide_and_narrow(h: Harness, tmp_path: Path) -> None:
    page = board(h)
    wide = look(page, tmp_path, "wide", (1400, 900))
    assert not failed(wide), failed(wide)
    names = {c["name"] for c in wide["checks"]}
    # The checks that matter ran (a check that stopped early leaves them out).
    for name in (
        "filters: nothing covers the panel",
        "filters: × drops a chip",
        "flow: legend folded",
        "flow: wheel down scrolls the page until the area reaches the top",
        "flow: the page ends with the area (no scrolling past it)",
        "flow: up at the area's top scrolls the page",
        "flow: mouse drag pans the view",
        "flow: the click ending a drag opens nothing",
        "flow: a press released outside does not pan later",
        "flow: with a chip above, the area still ends at the window's lower edge",
        "board: footer back",
    ):
        assert name in names, name
    assert wide["sizes"]["filters"] <= 60 and wide["sizes"]["flow_bar"] <= 40, wide["sizes"]

    narrow = look(page, tmp_path, "narrow", (390, 844))
    assert not failed(narrow), failed(narrow)
    names = {c["name"] for c in narrow["checks"]}
    for name in (
        "narrow: header is the title row",
        "narrow: 「⋯」 opens the ledger line and buttons",
        "narrow: the panel is a bottom sheet",
        "flow: the area ends at the window's lower edge (its scrollbar in view)",
    ):
        assert name in names, name
    sizes = narrow["sizes"]
    assert sizes["header"] <= 44 and sizes["filters"] <= 60 and sizes["flow_bar"] <= 40, sizes
