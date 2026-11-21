"""The terminal layer of ``tsw setup install --interactive`` (DN009 §16, CC009 §11).

Colours and cursor movement are ANSI escape sequences; keys are read one at a time from the
console (Windows: ``msvcrt.getwch``). Free text is still read with ``input()``, so the console's
own line editing, paste and input methods keep working. Whether the arrow-key interface is used,
and whether there is colour, is decided once (:func:`decide`); everything else falls back to the
line-by-line questions. Only the standard library is used (AGENTS.md §7).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TextIO

# The keys the interface understands; any other key is its own character.
UP, DOWN, LEFT, RIGHT, HOME, END = "up", "down", "left", "right", "home", "end"
ENTER, ESC, INTERRUPT = "enter", "esc", "interrupt"
_WINDOWS_FUNCTION_KEYS = {"H": UP, "P": DOWN, "K": LEFT, "M": RIGHT, "G": HOME, "O": END}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VT_PROCESSING = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


class Cancelled(Exception):
    """The user pressed Ctrl+C (or the input ended): nothing more is done."""


class Console(Protocol):
    """Where the interface reads keys and lines and writes text: the real console, or the
    scripted one the tests put in its place."""

    def key(self) -> str: ...

    def line(self, prompt: str) -> str: ...

    def write(self, text: str) -> None: ...

    def columns(self) -> int: ...


def decode_windows(read: Callable[[], str]) -> str:
    """One key from ``msvcrt.getwch``: function keys arrive as a ``\\x00`` / ``\\xe0`` prefix
    and a code (CC009 §11)."""
    char = read()
    if char in ("\x00", "\xe0"):
        return _WINDOWS_FUNCTION_KEYS.get(read(), "")
    if char in ("\r", "\n"):
        return ENTER
    if char == "\x1b":
        return ESC
    if char == "\x03":
        return INTERRUPT
    return char


@dataclass(frozen=True)
class Mode:
    """How this run talks to the user: the arrow-key interface or the line questions, with or
    without colour, with the usual symbols or their ASCII stand-ins (DN009 §16.4)."""

    keys: bool
    color: bool
    unicode: bool


GLYPHS = {"cursor": "►", "ok": "√", "bad": "×", "warn": "!", "skip": "–", "doing": "►"}
ASCII_GLYPHS = {"cursor": ">", "ok": "+", "bad": "x", "warn": "!", "skip": "-", "doing": ">"}


def decide(
    env: Mapping[str, str],
    *,
    stdin_tty: bool,
    stdout_tty: bool,
    plain: bool,
    escapes: bool,
    keys_supported: bool,
    encoding: str | None,
) -> Mode:
    """DN009 §16.4: the arrow keys need both streams on a console that handles escape sequences
    and a way to read single keys; colour needs stdout on such a console and no ``NO_COLOR``.
    ``--plain`` and ``TERM=dumb`` turn both off."""
    dumb = env.get("TERM", "").lower() == "dumb"
    usable = stdout_tty and escapes and not plain and not dumb
    keys = usable and stdin_tty and keys_supported
    color = usable and not env.get("NO_COLOR")
    try:
        "".join(GLYPHS.values()).encode(encoding or "ascii")
        unicode = True
    except (UnicodeEncodeError, LookupError):
        unicode = False
    return Mode(keys=keys, color=color, unicode=unicode)


class Style:
    """Colours and symbols; with colour off every method returns the text unchanged (the cursor
    line is shown in reverse video instead, DN009 §16.4)."""

    def __init__(self, mode: Mode) -> None:
        self.color = mode.color
        self.escapes = mode.keys or mode.color
        self.glyph = GLYPHS if mode.unicode else ASCII_GLYPHS

    def _sgr(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color and text else text

    def bold(self, text: str) -> str:
        return self._sgr(text, "1")

    def dim(self, text: str) -> str:
        return self._sgr(text, "2")

    def accent(self, text: str) -> str:
        return self._sgr(text, "36")

    def ok(self, text: str) -> str:
        return self._sgr(text, "32")

    def warn(self, text: str) -> str:
        return self._sgr(text, "33")

    def bad(self, text: str) -> str:
        return self._sgr(text, "31")

    def selected(self, text: str) -> str:
        if self.color:
            return self._sgr(text, "1;36")
        return f"\x1b[7m{text}\x1b[0m" if self.escapes else text

    def mark(self, kind: str) -> str:
        """A status symbol in its colour: ok, bad, warn, skip, doing."""
        paint = {
            "ok": self.ok,
            "bad": self.bad,
            "warn": self.warn,
            "skip": self.dim,
            "doing": self.accent,
        }[kind]
        return paint(self.glyph[kind])


def display_width(text: str) -> int:
    """Columns the text takes on the terminal: escape sequences take none, wide (CJK)
    characters two, combining marks none."""
    width = 0
    for char in _ANSI.sub("", text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def rows(text: str, columns: int) -> int:
    """Terminal rows one line of text takes once it wraps."""
    width = display_width(text)
    return max(1, -(-width // max(columns, 1)))


class Block:
    """Lines written since the block started, so they can be erased and redrawn in place."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.used = 0

    def line(self, text: str = "") -> None:
        self.console.write(text + "\n")
        self.used += rows(text, self.console.columns())

    def ask(self, prompt: str) -> str:
        reply = self.console.line(prompt)
        self.used += rows(prompt + reply, self.console.columns())
        return reply

    def erase(self) -> None:
        if self.used:
            self.console.write(f"\x1b[{self.used}F\x1b[J")
        self.used = 0


@dataclass(frozen=True)
class Option:
    label: str
    detail: str = ""


def choose(
    console: Console,
    style: Style,
    head: Sequence[str],
    options: Sequence[Option],
    start: int,
    *,
    back: bool = False,
    shortcuts: Mapping[str, int] | None = None,
) -> int | None:
    """Arrow-key selection (DN009 §16.3): ↑/↓ (and ←/→) move round the list, a digit jumps to
    that option, a shortcut key chooses at once, Enter confirms. Returns the index, or None for
    Esc when ``back`` is allowed. The block is erased before returning."""
    index = start if 0 <= start < len(options) else 0
    keys = dict(shortcuts or {})
    block = Block(console)
    labels = max(display_width(o.label) for o in options)
    help_text = "↑↓ 选择  回车 确认" + ("  Esc 上一问" if back else "") + "  Ctrl+C 取消"
    console.write("\x1b[?25l")  # hide the cursor while choosing
    try:
        while True:
            for text in head:
                block.line(text)
            for i, option in enumerate(options):
                pad = " " * (labels - display_width(option.label))
                detail = f"  {style.dim(option.detail)}" if option.detail else ""
                if i == index:
                    block.line(
                        f"   {style.accent(style.glyph['cursor'])} "
                        f"{style.selected(option.label)}{pad}{detail}"
                    )
                else:
                    block.line(f"     {option.label}{pad}{detail}")
            block.line(f"   {style.dim(help_text)}")
            key = console.key()
            if key == INTERRUPT:
                raise Cancelled
            if key == ENTER:
                block.erase()
                return index
            if key == ESC and back:
                block.erase()
                return None
            if key in (UP, LEFT):
                index = (index - 1) % len(options)
            elif key in (DOWN, RIGHT):
                index = (index + 1) % len(options)
            elif key == HOME:
                index = 0
            elif key == END:
                index = len(options) - 1
            elif key.isdigit() and 1 <= int(key) <= len(options):
                index = int(key) - 1
            elif key.lower() in keys:
                block.erase()
                return keys[key.lower()]
            block.erase()
    finally:
        console.write("\x1b[?25h")


# ---------------------------------------------------------------------- the real console


class WindowsConsole:
    """The Windows console: ``msvcrt.getwch`` for keys, ``input()`` for lines (CC009 §11)."""

    def __init__(self, out: TextIO) -> None:
        self.out = out

    def key(self) -> str:
        import msvcrt  # noqa: PLC0415  (Windows only)

        try:
            return decode_windows(msvcrt.getwch)
        except KeyboardInterrupt:
            return INTERRUPT

    def line(self, prompt: str) -> str:
        try:
            return input(prompt)
        except (KeyboardInterrupt, EOFError) as exc:
            raise Cancelled from exc

    def write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def columns(self) -> int:
        return shutil.get_terminal_size().columns


def enable_escapes(stream: TextIO) -> Callable[[], None] | None:
    """Turn on escape-sequence processing for the console behind ``stream``; returns how to put
    the console back, or None when the stream is not a console that can do it (CC009 §11)."""
    try:
        if not stream.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    if sys.platform != "win32":
        return lambda: None  # POSIX terminals handle the sequences themselves
    import ctypes  # noqa: PLC0415
    import msvcrt  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
    except (OSError, ValueError):
        return None
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)):
        return None
    before = mode.value
    if before & _VT_PROCESSING:
        return lambda: None
    if not kernel32.SetConsoleMode(wintypes.HANDLE(handle), before | _VT_PROCESSING):
        return None

    def restore() -> None:
        kernel32.SetConsoleMode(wintypes.HANDLE(handle), before)

    return restore


def open_terminal(plain: bool) -> tuple[Mode, Console | None, Callable[[], None]]:
    """Decide the mode for this process's console; returns the mode, the console for the
    arrow-key interface (None for the line questions) and how to restore the console."""
    restore = None if plain else enable_escapes(sys.stdout)
    mode = decide(
        os.environ,
        stdin_tty=_isatty(sys.stdin),
        stdout_tty=_isatty(sys.stdout),
        plain=plain,
        escapes=restore is not None,
        keys_supported=sys.platform == "win32",
        encoding=getattr(sys.stdout, "encoding", None),
    )
    console = WindowsConsole(sys.stdout) if mode.keys else None
    return mode, console, restore or (lambda: None)


def _isatty(stream: TextIO | None) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError):
        return False
