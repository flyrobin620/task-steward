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
# Copies write their destination; moves also remove their sources.
_COPIES = {"cp", "install", "ln", "rsync", "copy"}
_MOVES = {"mv", "move"}
_COPY_VALUE_OPTIONS = {
    "-t",
    "--target-directory",
    "-S",
    "--suffix",
    "-m",
    "--mode",
    "-o",
    "--owner",
    "-g",
    "--group",
}
_WINDOWS_COPIES = {"xcopy", "robocopy"}  # source destination [files] /flags
_WINDOWS_FLAG = re.compile(r"^/[A-Za-z?]{1,10}(:\S*)?$")
_SED_VALUE_OPTIONS = {"-e", "--expression", "-f", "--file", "-l", "--line-length"}
_SED_IN_PLACE = re.compile(r"^(-[A-Za-z]*i|--in-place)")
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
    "md",
    "export-csv",
    "set-itemproperty",
}
_PS_COPIES = {"copy-item", "cpi"}
_PS_MOVES = {"move-item", "mi", "rename-item", "rni"}
_PS_PATH_PARAMS = {"-path", "-literalpath", "-filepath", "-lp"}
_PS_DEST_PARAMS = {"-destination", "-target"}
_PS_SWITCHES = {
    "-force",
    "-recurse",
    "-nonewline",
    "-passthru",
    "-append",
    "-whatif",
    "-confirm",
    "-noclobber",
    "-asbytestream",
    "-notypeinformation",
    "-container",
}
_GIT_GLOBAL_VALUE_OPTIONS = {
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "--config-env",
}
_GIT_CLONE_VALUE_OPTIONS = {
    "-b",
    "--branch",
    "-o",
    "--origin",
    "-u",
    "--upload-pack",
    "--template",
    "--reference",
    "--reference-if-able",
    "--separate-git-dir",
    "--depth",
    "--shallow-since",
    "--shallow-exclude",
    "-c",
    "--config",
    "--filter",
    "-j",
    "--jobs",
    "--server-option",
    "--bundle-uri",
}
_GIT_WORKTREE_VALUE_OPTIONS = {"-b", "-B", "--reason"}
_GIT_INIT_VALUE_OPTIONS = {
    "-b",
    "--initial-branch",
    "--template",
    "--separate-git-dir",
    "--object-format",
    "--ref-format",
}


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


def split(command: str, *, literal_backslash: bool = False, comments: bool = True) -> list[Segment]:
    """Split a command line into segments of words, honouring quotes and redirections."""
    segments: list[Segment] = []
    current = Segment()
    buf: list[str] = []
    have_word = False
    redirect_next = False
    skip_next = False  # the operand of an input redirection
    heredocs: list[tuple[str, bool]] = []  # (delimiter, strip leading tabs) on this line
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
        elif c == "#" and comments and not have_word:  # a comment (not in cmd)
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        elif c == "\n":
            end_segment()
            if heredocs:  # the bodies follow this line: data, not commands
                i = _skip_heredocs(command, i + 1, heredocs)
                heredocs.clear()
                continue
        elif c == ";":
            end_segment()
        elif c == "<" and command.startswith("<<<", i):  # here-string: a word of data
            flush()
            skip_next = True
            i += 3
            continue
        elif c == "<" and nxt == "<":  # heredoc: remember the delimiter
            flush()
            i += 2
            strip_tabs = i < n and command[i] == "-"
            i += 1 if strip_tabs else 0
            while i < n and command[i] in " \t":
                i += 1
            delimiter, i = _heredoc_word(command, i)
            heredocs.append((delimiter, strip_tabs))
            continue
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


def _heredoc_word(command: str, i: int) -> tuple[str, int]:
    """The heredoc delimiter at ``i`` after quote removal (``'EOF'``, ``\\EOF``, ``E"O"F``)."""
    out: list[str] = []
    quote: str | None = None
    n = len(command)
    while i < n:
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            else:
                out.append(c)
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(command[i])
        elif c in " \t\r\n;&|<>()":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out), i


def _skip_heredocs(command: str, start: int, heredocs: list[tuple[str, bool]]) -> int:
    """Index just past the bodies of the pending heredocs, which begin at ``start``.

    A body whose delimiter never appears is not skipped: the rest is then read as commands, so
    that a misread delimiter can only cause a refusal, never hide a command.
    """
    i = start
    for delimiter, strip_tabs in heredocs:
        found = False
        while i < len(command):
            end = command.find("\n", i)
            line = command[i:] if end < 0 else command[i:end]
            i = len(command) if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line).rstrip("\r") == delimiter:
                found = True
                break
        if not found or not delimiter:
            return start
    return i


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


def analyze(
    command: str,
    cwd: Path,
    *,
    literal_backslash: bool = False,
    comments: bool = True,
    depth: int = 0,
) -> Facts:
    facts = Facts()
    if depth > 3:
        return facts
    here = cwd
    for segment in split(command, literal_backslash=literal_backslash, comments=comments):
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
        for target, base in _write_operands(name, args, here):
            path = resolve(target, base)
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
        nested = analyze(
            inner, cwd, literal_backslash=literal, comments=name != "cmd", depth=depth + 1
        )
        facts.programs.extend(nested.programs)
        facts.writes.extend(nested.writes)
        facts.unresolved.extend(nested.unresolved)


def split_options(
    args: list[str] | tuple[str, ...], takes_value: set[str]
) -> tuple[list[str], dict[str, list[str]]]:
    """Operands and option values, consuming the values of the options in ``takes_value``."""
    operands: list[str] = []
    values: dict[str, list[str]] = {}
    items = list(args)
    i = 0
    rest_are_operands = False
    while i < len(items):
        arg = items[i]
        if rest_are_operands or not arg.startswith("-") or arg == "-":
            operands.append(arg)
        elif arg == "--":
            rest_are_operands = True
        elif arg.startswith("--") and "=" in arg:
            key, value = arg.split("=", 1)
            values.setdefault(key, []).append(value)
        elif arg in takes_value:
            if i + 1 < len(items):
                values.setdefault(arg, []).append(items[i + 1])
            i += 1
        elif not arg.startswith("--") and len(arg) > 2 and arg[:2] in takes_value:
            values.setdefault(arg[:2], []).append(arg[2:])  # -tDIR
        i += 1
    return operands, values


def _write_operands(name: str, args: list[str], here: Path) -> Iterator[tuple[str, Path]]:
    """(word, directory it is relative to) for every path the program writes or removes."""
    if name in _TOUCHES_ALL:
        operands, _ = split_options(args, set())
        yield from ((a, here) for a in operands if a != "-")
        return
    if name in _COPIES or name in _MOVES:
        operands, values = split_options(args, _COPY_VALUE_OPTIONS)
        target = values.get("-t", []) + values.get("--target-directory", [])
        if target:
            destinations, sources = target, operands
        elif len(operands) >= 2:
            destinations, sources = operands[-1:], operands[:-1]
        else:
            return
        yield from ((d, here) for d in destinations)
        if name in _MOVES:  # a move removes its sources
            yield from ((src, here) for src in sources)
        return
    if name in _WINDOWS_COPIES:
        operands = [a for a in args if not _WINDOWS_FLAG.match(a)]
        if len(operands) >= 2:
            yield operands[1], here
            if name == "robocopy" and any(a.lower() in ("/mov", "/move") for a in args):
                yield operands[0], here
        return
    if name == "sed":
        if not any(_SED_IN_PLACE.match(a) for a in args):
            return
        operands, values = split_options(args, _SED_VALUE_OPTIONS)
        scripted = any(k in values for k in ("-e", "--expression", "-f", "--file"))
        yield from ((a, here) for a in (operands if scripted else operands[1:]))
        return
    if name == "dd":
        yield from ((a[3:], here) for a in args if a.startswith("of="))
        return
    if name in _PS_WRITERS or name in _PS_COPIES or name in _PS_MOVES:
        yield from ((a, here) for a in _powershell_paths(name, args))
        return
    if name == "git":
        sub, rest, base = git_invocation(args, here)
        if sub == "clone":
            operands, _ = split_options(rest, _GIT_CLONE_VALUE_OPTIONS)
            if len(operands) >= 2:
                yield operands[1], base
            elif operands:
                repo = operands[0].rstrip("/\\").replace("\\", "/").split("/")[-1]
                yield repo.split(":")[-1].removesuffix(".git"), base
        elif sub == "worktree" and rest[:1] == ["add"]:
            operands, _ = split_options(rest[1:], _GIT_WORKTREE_VALUE_OPTIONS)
            if operands:
                yield operands[0], base
        elif sub == "init":
            operands, _ = split_options(rest, _GIT_INIT_VALUE_OPTIONS)
            if operands:
                yield operands[0], base


def _powershell_paths(name: str, args: list[str]) -> list[str]:
    """Path arguments of a PowerShell cmdlet: named path parameters, else the positional ones."""
    paths: list[str] = []
    destinations: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        lowered = arg.lower()
        if lowered.startswith("-") and len(arg) > 1 and not lowered[1:2].isdigit():
            key, _, attached = lowered.partition(":")
            value: str | None
            if attached:
                value = arg.split(":", 1)[1]
            elif key in _PS_SWITCHES or i + 1 >= len(args):
                value = None
            else:
                value = args[i + 1]
                i += 1
            if value is not None and key in _PS_PATH_PARAMS:
                paths.append(value)
            elif value is not None and key in _PS_DEST_PARAMS:
                destinations.append(value)
        else:
            positional.append(arg.rstrip(","))
        i += 1
    # Positional arguments fill the parameters not given by name, in order: Path, Destination.
    unbound = iter(positional)
    if not paths:
        paths = [p for p in [next(unbound, None)] if p is not None]
    if name in _PS_COPIES or name in _PS_MOVES:
        if not destinations:
            destinations = [d for d in [next(unbound, None)] if d is not None]
        # A copy only reads its source; a move removes it.
        return destinations if name in _PS_COPIES else paths + destinations
    return paths


def git_invocation(
    args: tuple[str, ...] | list[str], cwd: Path
) -> tuple[str | None, list[str], Path]:
    """git's subcommand, its arguments, and the directory it runs in (after ``-C``)."""
    items = list(args)
    here = cwd
    i = 0
    while i < len(items):
        arg = items[i]
        if arg == "-C" and i + 1 < len(items):
            here = resolve(items[i + 1], here) or here
            i += 2
            continue
        if arg in _GIT_GLOBAL_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg.lower(), items[i + 1 :], here
    return None, [], here


def git_subcommand(args: tuple[str, ...] | list[str]) -> tuple[str | None, list[str]]:
    """Skip git's global options (``-C dir``, ``-c k=v``, ``--git-dir=...``)."""
    sub, rest, _ = git_invocation(args, Path())
    return sub, rest
