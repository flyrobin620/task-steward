"""A scripted console for the arrow-key interface of ``tsw setup`` (DN009 §16, CC009 §11).

The console is the platform the interface runs on: the tests give it keys and lines from a script
and keep what it writes. :class:`Screen` replays the written text on a small terminal model (the
escape sequences the interface uses: SGR, cursor to previous line, erase below, cursor
visibility) so a test can read what the user would see. It imports nothing from ``tsw``: the
driver ``tests/fixtures/drive_setup_keys.py`` uses it with a product checkout's runtime.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_SEQUENCE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


class ScriptOver(Exception):
    """The script ran out while the interface still waited for input."""


class ScriptedConsole:
    """Keys are the interface's key names (``up``, ``enter``, ``esc``, ``interrupt``, or a
    character); a line is ``("line", text)``."""

    def __init__(self, script: Iterable[str | tuple[str, str]], columns: int = 400) -> None:
        self.script = list(script)
        self.written: list[str] = []
        self.width = columns

    def _next(self) -> str | tuple[str, str]:
        if not self.script:
            raise ScriptOver(self.screen())
        return self.script.pop(0)

    def key(self) -> str:
        item = self._next()
        assert isinstance(item, str), f"a key was read, the script has {item!r}"
        return item

    def line(self, prompt: str) -> str:
        item = self._next()
        assert isinstance(item, tuple), f"a line was read, the script has {item!r}"
        self.written.append(prompt + item[1] + "\n")
        return item[1]

    def write(self, text: str) -> None:
        self.written.append(text)

    def columns(self) -> int:
        return self.width

    def output(self) -> str:
        return "".join(self.written)

    def screen(self) -> str:
        return Screen(self.width).feed(self.output()).text()


def _cells(char: str) -> int:
    import unicodedata  # noqa: PLC0415

    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


class Screen:
    """What the terminal shows after the text: rows of cells with a cursor, wrapping at
    ``columns`` like a console does (a full row wraps on the next character, not on the line
    feed); a wide character takes two cells."""

    def __init__(self, columns: int = 120) -> None:
        self.columns = columns
        self.rows: list[list[str]] = [[]]
        self.row = 0
        self.col = 0

    def feed(self, text: str) -> Screen:
        pos = 0
        for match in _SEQUENCE.finditer(text):
            self._text(text[pos : match.start()])
            self._control(match.group(1), match.group(2))
            pos = match.end()
        self._text(text[pos:])
        return self

    def _down(self) -> None:
        self.row += 1
        self.col = 0
        while len(self.rows) <= self.row:
            self.rows.append([])

    def _text(self, text: str) -> None:
        for char in text:
            if char == "\n":
                self._down()
            elif char == "\r":
                self.col = 0
            else:
                width = _cells(char)
                if self.col + width > self.columns:
                    self._down()
                cells = self.rows[self.row]
                cells.extend(" " * (self.col + width - len(cells)))
                cells[self.col] = char
                if width == 2:
                    cells[self.col + 1] = ""
                self.col += width

    def _control(self, params: str, final: str) -> None:
        if final == "F":  # cursor to the start of a previous line
            self.row = max(0, self.row - int(params or "1"))
            self.col = 0
        elif final == "J":  # erase from the cursor to the end of the screen
            del self.rows[self.row][self.col :]
            del self.rows[self.row + 1 :]
        elif final in ("m", "h", "l"):  # colours and cursor visibility change no text
            return
        else:
            raise AssertionError(f"unexpected escape sequence ESC[{params}{final}")

    def text(self) -> str:
        return "\n".join("".join(cells).rstrip() for cells in self.rows).rstrip("\n")
