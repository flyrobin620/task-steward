"""Text for the context index (DN007 §5): reading source material, query terms and the first
keywords tsw generates for a task nobody has described yet."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path

# A document longer than this is indexed and read only up to here.
MAX_DOC_CHARS = 200_000
MAX_GENERATED = 8
# Splits a query or a title into terms: whitespace and the usual ASCII and CJK punctuation.
_SPLIT = re.compile(r"[\s,;:!?()\[\]{}<>\"'`|/\\，。；：！？、（）【】《》「」『』“”‘’…·]+")
TAG_PATTERN = re.compile(r"\[([^\[\]]{1,40})\]")
_CODE = re.compile(r"`([^`\n]{3,60})`")
_IDENT = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*(?:[_.\-][A-Za-z0-9]+)+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b"
)
_CJK = re.compile(r"[㐀-鿿]")
_SKIP_TAGS = {"script", "style", "head", "svg"}
# Words that say nothing about what a task is about.
_STOP = {
    "实现",
    "研究",
    "支持",
    "修复",
    "新增",
    "增加",
    "改为",
    "以及",
    "并且",
    "任务",
    "按",
    "的",
    "和",
    "与",
    "the",
    "and",
    "for",
    "with",
}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(markup: str) -> str:
    parser = _Text()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup: fall back to dropping the tags
        return html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return "".join(parser.parts)


def squeeze(value: str) -> str:
    lines = (" ".join(line.split()) for line in value.splitlines())
    return "\n".join(line for line in lines if line)


def read_text(path: Path, limit: int = MAX_DOC_CHARS) -> str | None:
    """The text of a Markdown, plain-text or HTML file, or None when it cannot be read."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(limit * 4)
    except OSError:
        return None
    value = raw.decode("utf-8", errors="replace")
    if path.suffix.lower() in (".html", ".htm"):
        value = html_text(value)
    return squeeze(value)[:limit]


def terms(query: str) -> list[str]:
    """Query terms, in order and without duplicates (case-insensitive)."""
    seen: dict[str, str] = {}
    for part in _SPLIT.split(query or ""):
        word = part.strip(".-_")
        if word and word.lower() not in seen:
            seen[word.lower()] = word
    return list(seen.values())


def generated_keywords(title: str, spec: str | None) -> list[str]:
    """First keywords for a task without recorded ones (DN007 §12.2): the title's tags, code
    spans and identifiers, then the title's phrases. They are marked stale by the caller."""
    found: list[str] = []

    def add(values: Iterable[str]) -> None:
        for value in values:
            word = " ".join(value.split()).strip(" .-_")
            if len(word) < 2 or word.lower() in _STOP:
                continue
            if word.lower() not in (f.lower() for f in found):
                found.append(word)

    add(TAG_PATTERN.findall(title))
    body = spec or ""
    add(_CODE.findall(title))
    add(_IDENT.findall(title))
    bare = TAG_PATTERN.sub(" ", title)
    phrases = [p for p in _SPLIT.split(bare) if p]
    add(p for p in phrases if _CJK.search(p) and 2 <= len(p) <= 16)
    add(p for p in phrases if not _CJK.search(p) and len(p) >= 4)
    add(_CODE.findall(body))
    add(_IDENT.findall(body[:20_000]))
    return found[:MAX_GENERATED]


# Connectives inside a CJK phrase: "任务卡片与关键词" also looks for 任务卡片 and 关键词.
_CONNECTIVES = re.compile(r"[的与及和或并]")


def pieces(word: str) -> set[str]:
    """Parts of a long CJK phrase between connectives, at least two characters each."""
    if len(word) <= 3 or not _CJK.search(word):
        return set()
    return {p for p in _CONNECTIVES.split(word) if len(p) >= 2 and p != word}


def contains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def is_short(term: str) -> bool:
    """Too short for the trigram index (DN007 §5.2)."""
    return len(term) < 3
