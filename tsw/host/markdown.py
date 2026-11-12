"""Markdown to safe HTML for the Dashboard app's document dialog (DN006 §8.10, T278).

Briefs and reports are Markdown files under ``runs/`` and ``legacy/``. The app shows them in a
dialog on the page instead of as plain text: the page's script asks for the file with
``?view=html`` and puts the fragment rendered here into the dialog.

The renderer covers what the briefs and reports use: ATX and setext headings, paragraphs with hard
breaks, emphasis, strikethrough, code spans, fenced and indented code blocks, block quotes, nested
ordered and unordered lists (with task list boxes), pipe tables with alignment, thematic breaks,
links, autolinks and bare URLs. It is not a full CommonMark implementation; text it does not
understand stays text.

Safety does not depend on the page's Content-Security-Policy, although that policy also stays:
every piece of text is escaped, raw HTML in the source is shown as text, a link keeps its target
only when it is ``http``, ``https``, ``mailto``, relative or an anchor, and an image becomes a link
to it. The output therefore contains only the elements and attributes written here.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass

# Above this size the dialog offers the original file instead (DN006 §8.10).
MAX_SOURCE_CHARS = 2_000_000

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
_ITEM = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])(?:([ \t]+)(.*))?$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_DELIMITER_CELL = re.compile(r"^:?-+:?$")
_TASK_BOX = re.compile(r"^\[([ xX])\][ \t]+")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_SAFE_SCHEMES = ("http", "https", "mailto")
_LANG = re.compile(r"[^A-Za-z0-9_+#.-]")
_BARE_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_AUTOLINK = re.compile(
    r"<((?:https?|mailto):[^\s<>]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>"
)
_PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def safe_href(target: str) -> str | None:
    """The link target when it is safe to keep, else None (its text is shown alone)."""
    probe = re.sub(r"[\x00-\x20\x7f]", "", html.unescape(target))
    if not probe:
        return None
    scheme = _SCHEME.match(probe)
    if scheme and scheme.group(1).lower() not in _SAFE_SCHEMES:
        return None
    return probe


# ---------------------------------------------------------------------- inline


def _link_end(text: str, start: int) -> tuple[str, int] | None:
    """The target of a link whose ``(`` is at ``start``, and the index after its ``)``."""
    i = start + 1
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if i < n and text[i] == "<":
        close = text.find(">", i)
        if close < 0:
            return None
        target = text[i + 1 : close]
        i = close + 1
    else:
        depth = 0
        begin = i
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ch in " \t":
                break
            i += 1
        target = text[begin:i]
    while i < n and text[i] in " \t":
        i += 1
    if i < n and text[i] in "\"'":  # an optional title, not shown
        quote = text[i]
        close = text.find(quote, i + 1)
        if close < 0:
            return None
        i = close + 1
        while i < n and text[i] in " \t":
            i += 1
    if i >= n or text[i] != ")":
        return None
    return target, i + 1


def _bracket_end(text: str, start: int) -> int:
    """The index of the ``]`` closing the ``[`` at ``start``, or -1."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            run = len(text) - len(text[i:].lstrip("`"))
            close = text.find("`" * run, i + run)
            if close > 0:
                i = close + run
                continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _flanking(text: str, start: int, end: int, marker: str) -> bool:
    """Whether ``text[start:end]`` may be emphasized by ``marker`` around it."""
    if start >= end:
        return False
    inner = text[start:end]
    if inner[0].isspace() or inner[-1].isspace():
        return False
    if marker[0] == "_":  # no emphasis inside words, such as snake_case names
        before = text[start - len(marker) - 1] if start - len(marker) - 1 >= 0 else " "
        after = text[end + len(marker)] if end + len(marker) < len(text) else " "
        if before.isalnum() or after.isalnum():
            return False
    return True


def inline(text: str, *, links: bool = True) -> str:
    """Inline Markdown as escaped HTML; ``links=False`` inside a link's own text."""
    out: list[str] = []
    i = 0
    n = len(text)
    plain = 0

    def flush(upto: int) -> None:
        if upto > plain:
            out.append(_text(text[plain:upto], links=links))

    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in _PUNCT:
            flush(i)
            out.append(_e(text[i + 1]))
            i += 2
            plain = i
            continue
        if ch == "\\" and i + 1 == n:
            flush(i)
            out.append("<br>")
            i += 1
            plain = i
            continue
        if ch == "`":
            run = len(text[i:]) - len(text[i:].lstrip("`"))
            close = text.find("`" * run, i + run)
            while close > 0 and close + run < n and text[close + run] == "`":
                close = text.find("`" * run, close + run + 1)
            if close > 0:
                flush(i)
                code = text[i + run : close].replace("\n", " ")
                if len(code) > 2 and code[0] == " " and code[-1] == " " and code.strip():
                    code = code[1:-1]
                out.append(f"<code>{_e(code)}</code>")
                i = close + run
                plain = i
                continue
            i += run
            continue
        if ch == "<" and links:
            auto = _AUTOLINK.match(text, i)
            if auto:
                flush(i)
                target = auto.group(1)
                href = target if ":" in target else f"mailto:{target}"
                out.append(_anchor(href, _e(target)))
                i = auto.end()
                plain = i
                continue
        if ch == "!" and i + 1 < n and text[i + 1] == "[":
            close = _bracket_end(text, i + 1)
            if close > 0 and close + 1 < n and text[close + 1] == "(":
                found = _link_end(text, close + 1)
                if found:
                    flush(i)
                    alt = text[i + 2 : close]
                    label = "图片" + (f"：{_e(alt)}" if alt else "")
                    if links:
                        out.append(_anchor(found[0], label, cls="img"))
                    else:
                        out.append(label)
                    i = found[1]
                    plain = i
                    continue
        if ch == "[":
            close = _bracket_end(text, i)
            if close > 0 and close + 1 < n and text[close + 1] == "(":
                found = _link_end(text, close + 1)
                if found:
                    flush(i)
                    label = inline(text[i + 1 : close], links=False)
                    out.append(_anchor(found[0], label) if links else label)
                    i = found[1]
                    plain = i
                    continue
        if ch in "*_~":
            run = len(text[i:]) - len(text[i:].lstrip(ch))
            for width in (2, 1) if ch != "~" else (2,):
                if run < width:
                    continue
                marker = ch * width
                close = text.find(marker, i + width)
                while close > 0 and not _flanking(text, i + width, close, marker):
                    close = text.find(marker, close + 1)
                if close > 0:
                    flush(i)
                    tag = {2: "strong", 1: "em"}[width] if ch != "~" else "del"
                    body = inline(text[i + width : close], links=links)
                    out.append(f"<{tag}>{body}</{tag}>")
                    i = close + width
                    plain = i
                    break
            else:
                i += run
                continue
            continue
        i += 1
    flush(n)
    return "".join(out)


def _text(text: str, *, links: bool) -> str:
    """Plain text: escaped, hard line breaks kept, bare URLs linked."""
    lines = text.split("\n")
    out: list[str] = []
    for k, line in enumerate(lines):
        last = k == len(lines) - 1
        hard = not last and line.endswith("  ")
        out.append(_urls(line.rstrip(" ") if hard else line, links=links))
        if not last:
            out.append("<br>\n" if hard else "\n")
    return "".join(out)


def _urls(text: str, *, links: bool) -> str:
    if not links:
        return _e(text)
    out: list[str] = []
    last = 0
    for m in _BARE_URL.finditer(text):
        url = m.group(0)
        while url and (
            url[-1] in ".,;:!?'\"" or (url[-1] == ")" and url.count("(") < url.count(")"))
        ):
            url = url[:-1]
        out.append(_e(text[last : m.start()]))
        out.append(_anchor(url, _e(url)))
        last = m.start() + len(url)
    out.append(_e(text[last:]))
    return "".join(out)


def _anchor(target: str, label: str, *, cls: str = "") -> str:
    href = safe_href(target)
    if href is None:
        return label
    extra = f' class="{cls}"' if cls else ""
    return f'<a href="{_e(href)}"{extra}>{label}</a>'


# ---------------------------------------------------------------------- blocks


def _cells(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells: list[str] = []
    current: list[str] = []
    i = 0
    in_code = False
    while i < len(row):
        ch = row[i]
        if ch == "\\" and i + 1 < len(row) and row[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append("".join(current).strip())
    return cells


def _is_delimiter(line: str) -> bool:
    if "-" not in line or ("|" not in line and not line.strip().startswith(":")):
        return False
    cells = _cells(line)
    return bool(cells) and all(_DELIMITER_CELL.match(c.replace(" ", "")) for c in cells)


def _starts_table(lines: list[str], i: int) -> bool:
    return "|" in lines[i] and i + 1 < len(lines) and _is_delimiter(lines[i + 1])


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


@dataclass
class _Item:
    marker: str
    start: int
    lines: list[str]


class _Blocks:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.i = 0
        self.out: list[str] = []

    def run(self) -> str:
        steps: list[Callable[[], bool]] = [
            self._blank,
            self._fence,
            self._indented_code,
            self._heading,
            self._hr,
            self._quote,
            self._list,
            self._table,
            self._paragraph,
        ]
        while self.i < len(self.lines):
            for step in steps:
                if step():
                    break
        return "".join(self.out)

    # Each step consumes lines from ``self.i`` and returns True, or returns False untouched.

    def _blank(self) -> bool:
        if self.lines[self.i].strip():
            return False
        self.i += 1
        return True

    def _fence(self) -> bool:
        m = _FENCE.match(self.lines[self.i])
        if not m:
            return False
        fence = m.group(1)
        lang = _LANG.sub("", m.group(2))[:30]
        indent = _indent(self.lines[self.i])
        body: list[str] = []
        self.i += 1
        while self.i < len(self.lines):
            line = self.lines[self.i]
            stripped = line.strip()
            if stripped.startswith(fence[0] * len(fence)) and not stripped.strip(fence[0]):
                self.i += 1
                break
            body.append(line[min(indent, _indent(line)) :])
            self.i += 1
        cls = f' class="language-{_e(lang)}"' if lang else ""
        self.out.append(f"<pre><code{cls}>{_e(chr(10).join(body))}</code></pre>")
        return True

    def _indented_code(self) -> bool:
        if not self.lines[self.i].startswith("    "):
            return False
        body: list[str] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.startswith("    "):
                body.append(line[4:])
            elif not line.strip():
                body.append("")
            else:
                break
            self.i += 1
        while body and not body[-1]:
            body.pop()
        self.out.append(f"<pre><code>{_e(chr(10).join(body))}</code></pre>")
        return True

    def _heading(self) -> bool:
        m = _ATX.match(self.lines[self.i])
        if not m:
            return False
        level = len(m.group(1))
        self.out.append(f"<h{level}>{inline((m.group(2) or '').strip())}</h{level}>")
        self.i += 1
        return True

    def _hr(self) -> bool:
        if not _HR.match(self.lines[self.i]):
            return False
        self.out.append("<hr>")
        self.i += 1
        return True

    def _quote(self) -> bool:
        if not _QUOTE.match(self.lines[self.i]):
            return False
        body: list[str] = []
        while self.i < len(self.lines):
            m = _QUOTE.match(self.lines[self.i])
            if m:
                body.append(m.group(1))
            elif self.lines[self.i].strip() and body and body[-1].strip():
                body.append(self.lines[self.i])  # a lazy continuation of the paragraph
            else:
                break
            self.i += 1
        self.out.append(f"<blockquote>{_Blocks(body).run()}</blockquote>")
        return True

    def _table(self) -> bool:
        if not _starts_table(self.lines, self.i):
            return False
        head = _cells(self.lines[self.i])
        aligns: list[str] = []
        for spec in _cells(self.lines[self.i + 1]):
            c = spec.replace(" ", "")
            left, right = c.startswith(":"), c.endswith(":")
            aligns.append(
                "center" if left and right else "right" if right else "left" if left else ""
            )
        self.i += 2
        rows: list[list[str]] = []
        while self.i < len(self.lines) and self.lines[self.i].strip() and "|" in self.lines[self.i]:
            rows.append(_cells(self.lines[self.i]))
            self.i += 1

        def cell(tag: str, k: int, text: str) -> str:
            align = aligns[k] if k < len(aligns) else ""
            style = f' style="text-align:{align}"' if align else ""
            return f"<{tag}{style}>{inline(text)}</{tag}>"

        width = len(head)
        thead = "".join(cell("th", k, t) for k, t in enumerate(head))
        tbody = "".join(
            "<tr>" + "".join(cell("td", k, (r + [""] * width)[k]) for k in range(width)) + "</tr>"
            for r in rows
        )
        self.out.append(
            f'<div class="tbl"><table><thead><tr>{thead}</tr></thead>'
            f"<tbody>{tbody}</tbody></table></div>"
        )
        return True

    def _interrupts(self, i: int) -> bool:
        """Whether line ``i`` starts a block that ends a paragraph."""
        line = self.lines[i]
        if not line.strip():
            return True
        if _FENCE.match(line) or _ATX.match(line) or _HR.match(line) or _QUOTE.match(line):
            return True
        item = _ITEM.match(line)
        if item and item.group(4) and (not item.group(2)[0].isdigit() or item.group(2)[:-1] == "1"):
            return True
        return _starts_table(self.lines, i)

    def _paragraph(self) -> bool:
        body = [self.lines[self.i].lstrip(" ")]
        self.i += 1
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if _SETEXT.match(line) and line.strip()[0] in "=-":
                level = 1 if line.strip()[0] == "=" else 2
                self.i += 1
                self.out.append(f"<h{level}>{inline(chr(10).join(body).strip())}</h{level}>")
                return True
            if self._interrupts(self.i):
                break
            body.append(line.lstrip(" "))
            self.i += 1
        self.out.append(f"<p>{inline(chr(10).join(body).rstrip())}</p>")
        return True

    def _list(self) -> bool:
        first = _ITEM.match(self.lines[self.i])
        if not first:
            return False
        ordered = first.group(2)[0].isdigit()
        kind = first.group(2)[-1]
        items: list[_Item] = []
        loose = False
        blank_before = False
        while self.i < len(self.lines):
            line = self.lines[self.i]
            m = _ITEM.match(line)
            same = (
                m is not None
                and m.group(2)[0].isdigit() == ordered
                and m.group(2)[-1] == kind
                and (not items or _indent(line) < items[-1].start)
            )
            if same and m is not None:
                if items and blank_before:
                    loose = True
                spaces = m.group(3) or " "
                width = len(m.group(1)) + len(m.group(2)) + (len(spaces) if len(spaces) <= 4 else 1)
                items.append(_Item(m.group(2), width, [m.group(4) or ""]))
                blank_before = False
                self.i += 1
                continue
            if not items:
                break
            if not line.strip():
                blank_before = True
                items[-1].lines.append("")
                self.i += 1
                continue
            if _indent(line) >= items[-1].start:
                items[-1].lines.append(line[items[-1].start :])
                blank_before = False
                self.i += 1
                continue
            if not blank_before and not self._interrupts(self.i) and not m:
                items[-1].lines.append(line.strip())  # a lazy continuation
                self.i += 1
                continue
            break
        while items and items[-1].lines and not items[-1].lines[-1].strip():
            items[-1].lines.pop()
        rendered: list[str] = []
        for item in items:
            while item.lines and not item.lines[-1].strip():
                item.lines.pop()
            # A blank line between two paragraphs of an item makes the list loose.
            for k in range(1, len(item.lines) - 1):
                after = item.lines[k + 1]
                if not item.lines[k].strip() and after.strip() and not _ITEM.match(after):
                    loose = True
            box = ""
            if item.lines:
                task = _TASK_BOX.match(item.lines[0])
                if task:
                    checked = " checked" if task.group(1) in "xX" else ""
                    box = f'<input type="checkbox" disabled{checked}> '
                    item.lines[0] = item.lines[0][task.end() :]
            body = _Blocks(item.lines).run()
            if not loose and body.startswith("<p>"):
                body = re.sub(r"^<p>(.*?)</p>", r"\1", body, count=1, flags=re.S)
            cls = ' class="task"' if box else ""
            rendered.append(f"<li{cls}>{box}{body}</li>")
        tag = "ol" if ordered else "ul"
        start = int(first.group(2)[:-1]) if ordered else 1
        attr = f' start="{start}"' if ordered and start != 1 else ""
        self.out.append(f"<{tag}{attr}>{''.join(rendered)}</{tag}>")
        return True


def render_markdown(source: str) -> str:
    """The document as an ``<article class="md">`` fragment."""
    text = source.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    if text.startswith("\ufeff"):
        text = text[1:]
    return f'<article class="md">{_Blocks(text.split(chr(10))).run()}</article>'
