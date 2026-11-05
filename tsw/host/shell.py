"""Best-effort reading of a shell command line for the PreToolUse hook (DN006 §9.4).

A command line cannot be fully understood without running it (variables, scripts, aliases), so
this module only recognises the common forms: the programs a line runs, with ``cd`` tracked
across segments, and the paths it writes through redirections and well-known file commands
(POSIX tools and PowerShell cmdlets). Nested ``bash -c`` / ``powershell -Command`` / ``cmd /c``
strings are read recursively. Anything it cannot see is left to the technical acceptance check,
which compares the worktree diff with the task's writable roots.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GIT_BASH_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")
_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1", ".com")
_WRAPPERS = {"command", "env", "sudo", "exec", "nohup", "time", "call", "&", "."}
CHDIR = {"cd", "pushd", "chdir", "set-location", "sl"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_POWERSHELL = {"powershell", "pwsh"}
_DEVICES = {"/dev/null", "nul", "$null", "/dev/stdout", "/dev/stderr", "con"}
# POSIX tools: every operand is written (or removed).
_TOUCHES_ALL = {"rm", "rmdir", "mkdir", "touch", "unlink", "truncate", "shred", "tee", "del", "rd"}
# POSIX tools whose last operand is the destination.
_TOUCHES_LAST = {"cp", "mv", "install", "ln", "rsync", "copy", "move", "xcopy", "robocopy"}
# PowerShell cmdlets (and their usual aliases) that write the paths they are given.
_PS_WRITERS = {
    "set-content",
    "add-content",
    "clear-content",
    "out-file",
    "new-item",
    "ni",
    "remove-item",
    "ri",
    "erase",
    "rename-item",
    "rni",
    "copy-item",
    "cpi",
    "move-item",
    "mi",
    "md",
    "export-csv",
    "set-itemproperty",
}
_PS_PATH_PARAMS = {"-path", "-literalpath", "-filepath", "-destination", "-target", "-lp"}


@dataclass
class Segment:
    words: list[str] = field(default_factory=list[str])
    redirects: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Program:
    name: str  # lower-case base name without an executable suffix
    args: tuple[str, ...]
    cwd: Path


@dataclass
class Facts:
    programs: list[Program] = field(default_factory=list[Program])
    writes: list[Path] = field(default_factory=list[Path])
    unresolved: list[str] = field(default_factory=list[str])  # write targets with variables etc.


def program_name(word: str) -> str:
    base = PureWindowsPath(word.strip("\"'")).name.lower()
    for suffix in _SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def split(command: str, *, literal_backslash: bool = False) -> list[Segment]:
    """Split a command line into segments of words, honouring quotes and redirections."""
    segments: list[Segment] = []
    current = Segment()
    buf: list[str] = []
    have_word = False
    redirect_next = False
    skip_next = False  # the operand of an input redirection
    quote: str | None = None
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal buf, have_word, redirect_next, skip_next
        if have_word:
            word = "".join(buf)
            if redirect_next:
                current.redirects.append(word)
                redirect_next = False
            elif skip_next:
                skip_next = False
            else:
                current.words.append(word)
        buf, have_word = [], False

    def end_segment() -> None:
        nonlocal current
        flush()
        if current.words or current.redirects:
            segments.append(current)
        current = Segment()

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and not literal_backslash and nxt in '"\\$`':
                buf.append(nxt)
                i += 1
            else:
                buf.append(c)
            i += 1
            continue
        if c in "'\"":
            quote, have_word = c, True
        elif c == "\\" and not literal_backslash and nxt:
            buf.append(nxt)
            have_word = True
            i += 1
        elif c in " \t\r":
            flush()
        elif c in "\n;":
            end_segment()
        elif c == "&" and nxt == ">":  # &> file
            flush()
            i += 2 if command[i + 2 : i + 3] != ">" else 3
            redirect_next = True
            continue
        elif c in "&|":
            end_segment()
            if nxt == c:
                i += 1
        elif c == ">" or (c.isdigit() and not have_word and nxt == ">"):
            flush()
            while i < n and command[i].isdigit():
                i += 1
            i += 1  # the '>'
            if i < n and command[i] in ">|":
                i += 1
            if i < n and command[i] == "&":  # 2>&1: a descriptor, not a file
                i += 1
                while i < n and (command[i].isdigit() or command[i] == "-"):
                    i += 1
                continue
            redirect_next = True
            continue
        elif c == "<":
            flush()
            skip_next = True
        else:
            buf.append(c)
            have_word = True
        i += 1
    end_segment()
    return segments


def resolve(word: str, cwd: Path) -> Path | None:
    """The path a word names, or None when it depends on variables or is a device."""
    text = word.strip()
    if not text or text.lower() in _DEVICES or "$" in text or "%" in text or "`" in text:
        return None
    match = _GIT_BASH_DRIVE.match(text)
    if match:
        text = f"{match.group(1).upper()}:/{text[match.end() :]}"
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return Path(os.path.normpath(path))


def analyze(command: str, cwd: Path, *, literal_backslash: bool = False, depth: int = 0) -> Facts:
    facts = Facts()
    if depth > 3:
        return facts
    here = cwd
    for segment in split(command, literal_backslash=literal_backslash):
        _redirects(facts, segment.redirects, here)
        words = _strip_wrappers(segment.words)
        if not words:
            continue
        name, args = program_name(words[0]), words[1:]
        facts.programs.append(Program(name=name, args=tuple(args), cwd=here))
        if name in CHDIR:
            target = next((a for a in args if not a.startswith("-")), "~")
            moved = resolve(target, here)
            if moved is not None:
                here = moved
            continue
        _nested(facts, name, args, here, depth)
        for target in _write_operands(name, args):
            path = resolve(target, here)
            if path is None:
                facts.unresolved.append(target)
            else:
                facts.writes.append(path)
    return facts


def _redirects(facts: Facts, targets: list[str], cwd: Path) -> None:
    for target in targets:
        path = resolve(target, cwd)
        if path is not None:
            facts.writes.append(path)
        elif target.lower() not in _DEVICES:
            facts.unresolved.append(target)


def _strip_wrappers(words: list[str]) -> list[str]:
    out = list(words)
    while out and (_ASSIGNMENT.match(out[0]) or program_name(out[0]) in _WRAPPERS):
        head = program_name(out.pop(0))
        if head == "env":  # env -u NAME / env -i
            while out and out[0].startswith("-"):
                flag = out.pop(0)
                if flag in ("-u", "--unset") and out:
                    out.pop(0)
    return out


def _nested(facts: Facts, name: str, args: list[str], cwd: Path, depth: int) -> None:
    inner: str | None = None
    literal = False
    if name in _SHELLS:
        for i, arg in enumerate(args[:-1]):
            if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg):
                inner = args[i + 1]
                break
    elif name in _POWERSHELL:
        for i, arg in enumerate(args):
            if arg.lower() in ("-command", "-c") and i + 1 < len(args):
                inner, literal = " ".join(args[i + 1 :]), True
                break
    elif name == "cmd":
        for i, arg in enumerate(args):
            if arg.lower() in ("/c", "/k") and i + 1 < len(args):
                inner, literal = " ".join(args[i + 1 :]), True
                break
    if inner:
        nested = analyze(inner, cwd, literal_backslash=literal, depth=depth + 1)
        facts.programs.extend(nested.programs)
        facts.writes.extend(nested.writes)
        facts.unresolved.extend(nested.unresolved)


def _operands(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-") or a == "-"]


def _write_operands(name: str, args: list[str]) -> Iterator[str]:
    if name in _TOUCHES_ALL:
        yield from (a for a in _operands(args) if a != "-")
        return
    if name in _TOUCHES_LAST:
        operands = _operands(args)
        if len(operands) >= 2:
            yield operands[-1]
        return
    if name == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
        operands = _operands(args)
        has_script_flag = any(a in ("-e", "-f") or a.startswith("--expression") for a in args)
        yield from operands if has_script_flag else operands[1:]
        return
    if name == "dd":
        yield from (a[3:] for a in args if a.startswith("of="))
        return
    if name in _PS_WRITERS:
        params = [a.lower() for a in args]
        named = [args[i + 1] for i, a in enumerate(params[:-1]) if a in _PS_PATH_PARAMS]
        yield from named or _operands(args)
        return
    if name == "git":
        sub, rest = git_subcommand(args)
        operands = _operands(rest)
        if sub == "clone" and len(operands) >= 2:
            yield operands[-1]
        elif sub == "worktree" and operands[:1] == ["add"] and len(operands) >= 2:
            yield operands[1]


def git_subcommand(args: tuple[str, ...] | list[str]) -> tuple[str | None, list[str]]:
    """Skip git's global options (``-C dir``, ``-c k=v``, ``--git-dir=...``)."""
    items = list(args)
    i = 0
    while i < len(items):
        arg = items[i]
        if arg in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg.lower(), items[i + 1 :]
    return None, []
