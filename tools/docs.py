"""Generate and check the HTML views of the README and design documents.

Usage:
    python tools/docs.py render            regenerate every HTML view
    python tools/docs.py check             fail when anything is out of date or broken

``check`` verifies that every HTML view equals a fresh rendering of its Markdown source, that
relative links (and anchors into HTML views) resolve, that the design diagrams pass their own
validation, and runs the privacy scan (docs/development/docs.md).

The renderer supports the Markdown subset the documents use: headings, paragraphs, nested
lists, tables, fenced code, block quotes, images (SVG inlined), links and inline code / bold.
Only the standard library is used.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.checks.privacy import scan as privacy_scan  # noqa: E402
from tools.checks.repo import tracked_files, utf8_stdio  # noqa: E402

CSS = """
:root{--fg:#1f2430;--muted:#5b6475;--line:#d9dee8;--accent:#3b5bdb;--bg-soft:#f6f8fc;--code:#f3f4f8}
*{box-sizing:border-box}
body{margin:0;font:15px/1.7 "Segoe UI","Microsoft YaHei",sans-serif;color:var(--fg);background:#fff}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);max-width:1400px;margin:0 auto}
nav.toc{position:sticky;top:0;align-self:start;max-height:100vh;overflow:auto;padding:28px 18px;border-right:1px solid var(--line);font-size:13px}
nav.toc h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 8px}
nav.toc a{display:block;color:var(--fg);text-decoration:none;padding:2px 0}
nav.toc a.l3{padding-left:14px;color:var(--muted)}
nav.toc a:hover{color:var(--accent)}
main{padding:32px 48px 64px;min-width:0}
h1{font-size:28px;margin:0 0 16px;border-bottom:3px solid var(--accent);padding-bottom:10px}
h2{font-size:21px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:17px;margin:26px 0 10px}
h4{font-size:15px;margin:20px 0 8px}
a{color:var(--accent)}
code{font-family:Consolas,"Cascadia Mono",monospace;font-size:13px;background:var(--code);padding:1px 5px;border-radius:4px}
pre{background:#1e2230;color:#e8eaf2;padding:14px 16px;border-radius:8px;overflow:auto;line-height:1.5}
pre code{background:none;padding:0;color:inherit}
table{border-collapse:collapse;width:100%;margin:12px 0 18px;font-size:13.5px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--bg-soft);white-space:nowrap}
tr:nth-child(even) td{background:#fbfcfe}
blockquote{margin:12px 0;padding:8px 16px;border-left:4px solid var(--accent);background:var(--bg-soft)}
figure{margin:18px 0 8px;padding:12px;border:1px solid var(--line);border-radius:10px;background:#fff}
figure svg{display:block;width:100%;height:auto;max-height:none}
figcaption{font-size:13px;color:var(--muted);margin-top:6px;display:flex;justify-content:space-between;gap:12px}
.meta{color:var(--muted);font-size:13px}
footer{margin-top:48px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:10px}
@media (max-width:900px){.layout{grid-template-columns:1fr}nav.toc{position:static;max-height:none;border-right:0;border-bottom:1px solid var(--line)}main{padding:20px}}
@media print{nav.toc{display:none}.layout{display:block}main{padding:0}a{color:inherit;text-decoration:none}
 pre{white-space:pre-wrap;background:#f3f4f8;color:#111}figure,table,pre{break-inside:avoid}h2,h3{break-after:avoid}
 body{font-size:11pt}@page{margin:16mm 14mm}}
"""

_LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
_LIST_START = re.compile(r"^(\s*)([-*]|\d+\.)\s")


def html_sources(root: Path = ROOT) -> list[Path]:
    """Markdown files that have an HTML view: the README and the design documents."""
    return [root / "README.md", *sorted((root / "docs" / "design").glob("*.md"))]


def _slug(text: str, used: set[str]) -> str:
    cleaned = re.sub(r"[^\w\- ]", "", re.sub(r"[`*]", "", text.strip().lower()))
    base = cleaned.replace(" ", "-") or "section"
    candidate, index = base, 2
    while candidate in used:
        candidate, index = f"{base}-{index}", index + 1
    used.add(candidate)
    return candidate


class Renderer:
    def __init__(self, source: Path, with_html: set[Path]) -> None:
        self.source = source
        self.base = source.parent
        self.with_html = with_html

    def href(self, url: str) -> str:
        if re.match(r"^[a-z]+:", url) or url.startswith("#"):
            return url
        path, _, fragment = url.partition("#")
        if path.endswith(".md") and (self.base / path).resolve() in self.with_html:
            path = path[:-3] + ".html"
        return path + ("#" + fragment if fragment else "")

    def inline(self, text: str) -> str:
        out: list[str] = []
        for part in re.split(r"(`[^`]+`)", text):
            if part.startswith("`") and part.endswith("`") and len(part) > 1:
                out.append("<code>" + html.escape(part[1:-1]) + "</code>")
                continue
            escaped = html.escape(part, quote=False)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
            escaped = re.sub(
                r"\[([^\]]+)\]\(([^)\s]+)\)",
                lambda m: f'<a href="{html.escape(self.href(m.group(2)))}">{m.group(1)}</a>',
                escaped,
            )
            out.append(escaped)
        return "".join(out)

    def figure(self, alt: str, src: str) -> str:
        svg = (self.base / src).read_text(encoding="utf-8")
        svg = re.sub(r"^\s*<\?xml[^>]*>\s*", "", svg)
        svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg)
        return (
            f"<figure>{svg}<figcaption><span>{html.escape(alt)}</span>"
            f'<a href="{html.escape(src)}" target="_blank">在新窗口打开 SVG</a>'
            "</figcaption></figure>"
        )

    def parse_list(self, lines: list[str], index: int, indent: int) -> tuple[str, int]:
        first = _LIST_START.match(lines[index])
        assert first is not None
        tag = "ol" if first.group(2)[0].isdigit() else "ul"
        items: list[str] = []
        while index < len(lines):
            match = _LIST_ITEM.match(lines[index])
            if not match or len(match.group(1)) != indent:
                break
            content = [match.group(3)]
            index += 1
            children = ""
            while index < len(lines):
                line = lines[index]
                if not line.strip():
                    ahead = index + 1
                    while ahead < len(lines) and not lines[ahead].strip():
                        ahead += 1
                    deeper = (
                        len(lines[ahead]) - len(lines[ahead].lstrip()) if ahead < len(lines) else 0
                    )
                    if ahead < len(lines) and deeper > indent:
                        index = ahead
                        continue
                    break
                current = len(line) - len(line.lstrip())
                sub = _LIST_START.match(line)
                if sub and current > indent:
                    nested, index = self.parse_list(lines, index, current)
                    children += nested
                    continue
                if current > indent and not sub:
                    content.append(line.strip())
                    index += 1
                    continue
                break
            items.append("<li>" + self.inline(" ".join(content)) + children + "</li>")
        return f"<{tag}>" + "".join(items) + f"</{tag}>", index

    def render(self) -> str:
        text = self.source.read_text(encoding="utf-8")
        lines = text.splitlines()
        out: list[str] = []
        toc: list[tuple[int, str, str]] = []
        used: set[str] = set()
        title = self.source.stem
        paragraph: list[str] = []
        index = 0

        def flush() -> None:
            if paragraph:
                out.append("<p>" + self.inline(" ".join(paragraph)) + "</p>")
                paragraph.clear()

        while index < len(lines):
            line = lines[index]
            if line.startswith("```"):
                flush()
                lang = line[3:].strip()
                block: list[str] = []
                index += 1
                while index < len(lines) and not lines[index].startswith("```"):
                    block.append(lines[index])
                    index += 1
                index += 1
                out.append(
                    f'<pre><code class="lang-{html.escape(lang)}">'
                    + html.escape("\n".join(block))
                    + "</code></pre>"
                )
                continue
            heading = re.match(r"^(#{1,4})\s+(.*)$", line)
            if heading:
                flush()
                level, label = len(heading.group(1)), heading.group(2).strip()
                if level == 1:
                    title = label
                    out.append(f"<h1>{self.inline(label)}</h1>")
                else:
                    anchor = _slug(label, used)
                    if level in (2, 3):
                        toc.append((level, anchor, label))
                    out.append(f'<h{level} id="{anchor}">{self.inline(label)}</h{level}>')
                index += 1
                continue
            image = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
            if image:
                flush()
                alt, src = image.groups()
                out.append(
                    self.figure(alt, src)
                    if src.endswith(".svg")
                    else f'<figure><img src="{html.escape(src)}" alt="{html.escape(alt)}"></figure>'
                )
                index += 1
                continue
            if (
                line.startswith("|")
                and index + 1 < len(lines)
                and re.match(r"^\|\s*:?-+", lines[index + 1])
            ):
                flush()

                def cells(row: str) -> list[str]:
                    return [c.strip() for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]

                head = cells(line)
                index += 2
                rows: list[list[str]] = []
                while index < len(lines) and lines[index].startswith("|"):
                    rows.append(cells(lines[index]))
                    index += 1
                table = (
                    "<table><thead><tr>"
                    + "".join(f"<th>{self.inline(c)}</th>" for c in head)
                    + "</tr></thead><tbody>"
                )
                for row in rows:
                    table += "<tr>" + "".join(f"<td>{self.inline(c)}</td>" for c in row) + "</tr>"
                out.append(table + "</tbody></table>")
                continue
            if re.match(r"^\s*([-*]|\d+\.)\s+", line):
                flush()
                indent = len(line) - len(line.lstrip())
                rendered, index = self.parse_list(lines, index, indent)
                out.append(rendered)
                continue
            if line.startswith(">"):
                flush()
                quote: list[str] = []
                while index < len(lines) and lines[index].startswith(">"):
                    quote.append(lines[index].lstrip("> ").rstrip())
                    index += 1
                out.append("<blockquote><p>" + self.inline(" ".join(quote)) + "</p></blockquote>")
                continue
            if re.match(r"^(-{3,}|\*{3,})\s*$", line):
                flush()
                out.append("<hr>")
                index += 1
                continue
            if not line.strip():
                flush()
                index += 1
                continue
            paragraph.append(line.strip())
            index += 1
        flush()

        toc_html = "".join(
            f'<a class="l{level}" href="#{anchor}">{html.escape(re.sub(r"[`*]", "", label))}</a>'
            for level, anchor, label in toc
        )
        source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        name = self.source.name
        body = "\n".join(out)
        page_title = html.escape(re.sub(r"[`*]", "", title))
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="tsw-source-sha256" content="{source_sha}">
<title>{page_title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="layout">
<nav class="toc"><h2>目录</h2>{toc_html}</nav>
<main>
{body}
<footer>本页由同目录的 <a href="{name}">{name}</a> 生成，内容与 Markdown 版本一致。</footer>
</main>
</div>
</body>
</html>
"""


def render_all(root: Path = ROOT) -> list[Path]:
    sources = html_sources(root)
    with_html = {path.resolve() for path in sources}
    written: list[Path] = []
    for source in sources:
        target = source.with_suffix(".html")
        target.write_text(Renderer(source, with_html).render(), encoding="utf-8", newline="\n")
        written.append(target)
    return written


# ---------------------------------------------------------------------- checks


def check_views(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    sources = html_sources(root)
    with_html = {path.resolve() for path in sources}
    for source in sources:
        target = source.with_suffix(".html")
        rel = target.relative_to(root).as_posix()
        if not target.is_file():
            problems.append(f"{rel}: 缺少 HTML，运行 python tools/docs.py render")
        elif target.read_text(encoding="utf-8") != Renderer(source, with_html).render():
            problems.append(f"{rel}: 已过期，运行 python tools/docs.py render")
    return problems


def _ids(html_text: str) -> set[str]:
    return set(re.findall(r'\sid="([^"]+)"', html_text))


def check_links(files: Iterable[Path], root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        if path.suffix == ".md":
            targets = [m.group(1) for m in re.finditer(r"\]\(([^)\s]+)\)", _strip_code(text))]
        else:
            targets = re.findall(r'href="([^"]+)"', text)
        for target in targets:
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            file_part, _, fragment = target.partition("#")
            resolved = (path.parent / html.unescape(file_part)).resolve()
            if not resolved.exists():
                problems.append(f"{rel}: 链接目标不存在 {target}")
                continue
            if (
                fragment
                and resolved.suffix == ".html"
                and path.suffix == ".html"
                and fragment not in _ids(resolved.read_text(encoding="utf-8"))
            ):
                problems.append(f"{rel}: 锚点不存在 {target}")
    return problems


def _strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def check_diagrams(root: Path = ROOT) -> list[str]:
    diagrams = root / "docs" / "design" / "diagrams"
    validation = diagrams / "validation.json"
    if not (diagrams / "check.py").is_file():
        return []
    before = validation.read_bytes() if validation.exists() else b""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(diagrams / "check.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=diagrams,
        check=False,
    )
    after = validation.read_bytes() if validation.exists() else b""
    problems: list[str] = []
    if proc.returncode != 0:
        problems.append(f"docs/design/diagrams/check.py 失败：{proc.stderr.strip()[-500:]}")
    if after != before:
        validation.write_bytes(before)
        problems.append(
            "docs/design/diagrams/validation.json 与图不一致：运行 draw.py 与 check.py 后提交结果"
        )
    return problems


def check(root: Path = ROOT) -> list[str]:
    files = tracked_files(root)
    linkable = [p for p in files if p.suffix in (".md", ".html")]
    return [
        *check_views(root),
        *check_links(linkable, root),
        *check_diagrams(root),
        *privacy_scan(files, root),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and check the HTML views")
    parser.add_argument("command", choices=["render", "check"])
    args = parser.parse_args(argv)
    utf8_stdio()
    if args.command == "render":
        for path in render_all():
            print(path.relative_to(ROOT).as_posix())
        return 0
    problems = check()
    for problem in problems:
        print(problem)
    print("docs check: " + ("OK" if not problems else f"{len(problems)} problem(s)"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
