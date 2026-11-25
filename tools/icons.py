"""The Dashboard's icons: PNGs rasterized from ``tsw/host/icons/icon.svg`` (T355, DN006 §8.10).

    python tools/icons.py render     draw every PNG of tsw.host.pwa.ICONS and write icons.json
    python tools/icons.py check      the PNGs and icons.json match icon.svg (also a unit test)

``render`` runs on the developer's machine: the runtime has no rasterizer (standard library only),
so the PNGs are committed next to their source. It draws with a Chromium browser in headless mode
(Microsoft Edge or Google Chrome; ``--browser`` or ``TSW_BROWSER`` names another), the full-bleed
icons from the source with its background rectangle (``<rect id="bg" ... rx="...">``) squared.
``icons.json`` records the source's SHA-256 and each PNG's size and SHA-256; ``check`` fails when
the source changed without a new ``render`` or a PNG is not the one recorded, like
``tools/docs.py check`` for the HTML.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tsw.host.pwa import ICON_DIR, ICONS, RECORD, SOURCE  # noqa: E402

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
ON_PATH = ("microsoft-edge", "google-chrome", "chromium", "chromium-browser")
BACKGROUND = re.compile(r'(<rect\b[^>]*\bid="bg"[^>]*?)\s+rx="[^"]*"')
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RENDER_SECONDS = 90


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def full_bleed(svg: str) -> str:
    """The source with its background rectangle squared (the ``full`` variant)."""
    out, n = BACKGROUND.subn(r"\1", svg)
    if n != 1:
        raise SystemExit(f'{SOURCE}：应有且只有一个带 rx 的 <rect id="bg">（找到 {n} 个）')
    return out


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG's header, None when it is not a PNG."""
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def find_browser(given: str | None) -> str:
    named = given or os.environ.get("TSW_BROWSER")
    if named:
        return named
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    for name in ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("找不到 Chromium 浏览器（Edge 或 Chrome）：用 --browser 或 TSW_BROWSER 指定")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Ends the browser and its helper processes."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        proc.kill()
    proc.wait(timeout=30)


def rasterize(browser: str, svg: str, size: int, out: Path) -> None:
    """``svg`` drawn at ``size`` × ``size`` into ``out`` with a transparent background. A headless
    Chromium sometimes stays up after writing the screenshot, so the file is awaited and the
    browser ended."""
    with tempfile.TemporaryDirectory(prefix="tsw-icons-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        (work / "icon.svg").write_text(svg, encoding="utf-8")
        page = work / "page.html"
        page.write_text(
            "<!doctype html><style>html,body{margin:0;background:transparent}"
            "img{display:block}</style>"
            f'<img src="icon.svg" width="{size}" height="{size}">',
            encoding="utf-8",
        )
        shot = work / "shot.png"
        proc = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                "--force-device-scale-factor=1",
                "--default-background-color=00000000",
                f"--user-data-dir={work / 'profile'}",
                f"--window-size={size},{size}",
                f"--screenshot={shot}",
                page.as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + RENDER_SECONDS
            last = -1
            while time.monotonic() < deadline:
                done = proc.poll() is not None
                now = shot.stat().st_size if shot.is_file() else -1
                if now > 0 and (done or now == last):
                    break
                if done:
                    raise SystemExit(f"浏览器退出（{proc.returncode}）但没有写出截图")
                last = now
                time.sleep(0.5)
            else:
                raise SystemExit(f"{RENDER_SECONDS} 秒内浏览器没有写出截图")
        finally:
            _stop(proc)
        data = shot.read_bytes()
    if png_size(data) != (size, size):
        raise SystemExit(f"截图尺寸为 {png_size(data)}，应为 {size}×{size}")
    out.write_bytes(data)


def render(browser: str) -> int:
    source = (ICON_DIR / SOURCE).read_text(encoding="utf-8")
    variants = {"any": source, "full": full_bleed(source)}
    files: dict[str, dict[str, object]] = {}
    for icon in ICONS:
        target = ICON_DIR / icon.name
        rasterize(browser, variants[icon.variant], icon.size, target)
        files[icon.name] = {
            "size": icon.size,
            "variant": icon.variant,
            "sha256": sha256(target.read_bytes()),
        }
        print(f"{icon.name}：{icon.size}×{icon.size}（{icon.variant}）")
    record = {"source": SOURCE, "sha256": sha256(source.encode("utf-8")), "files": files}
    (ICON_DIR / RECORD).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return 0


def _object(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def check(icon_dir: Path = ICON_DIR) -> list[str]:
    """What does not match: the source against the record, each PNG against its entry."""
    problems: list[str] = []
    try:
        loaded: Any = json.loads((icon_dir / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{RECORD} 无法读取：{exc}（运行 python tools/icons.py render）"]
    record = _object(loaded)
    source = (icon_dir / SOURCE).read_bytes().decode("utf-8")
    try:
        full_bleed(source)
    except SystemExit as exc:
        problems.append(str(exc))
    if record.get("sha256") != sha256(source.encode("utf-8")):
        problems.append(f"{SOURCE} 改过但 PNG 没有重新生成（运行 python tools/icons.py render）")
    listed: Any = record.get("files")
    files = _object(listed)
    if sorted(files) != sorted(icon.name for icon in ICONS):
        problems.append(f"{RECORD} 的文件与 tsw.host.pwa.ICONS 不一致")
    for icon in ICONS:
        path = icon_dir / icon.name
        if not path.is_file():
            problems.append(f"缺少 {icon.name}")
            continue
        data = path.read_bytes()
        if png_size(data) != (icon.size, icon.size):
            problems.append(f"{icon.name} 的尺寸为 {png_size(data)}，应为 {icon.size}×{icon.size}")
        found: Any = files.get(icon.name)
        entry = _object(found)
        if entry.get("sha256") != sha256(data) or entry.get("variant") != icon.variant:
            problems.append(f"{icon.name} 不是 {RECORD} 记录的那一张")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=("render", "check"))
    parser.add_argument("--browser", help="Chromium 浏览器的可执行文件")
    args = parser.parse_args(argv)
    if args.command == "render":
        return render(find_browser(args.browser))
    problems = check()
    for line in problems:
        print(line)
    print("图标：一致" if not problems else f"图标：{len(problems)} 处不一致")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
