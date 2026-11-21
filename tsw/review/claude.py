"""The ``claude-headless`` reviewer (CC004): one ``claude -p`` run per review round, started by tsw.

The prompt only points at the request file (the request is not put on the command line). The
model gets nothing but the read-only file tools (``--tools``), everything else that could write
is denied and anything that would need approval is refused (``dontAsk``); the user's plugins,
hooks and MCP servers are not loaded (``--safe-mode``, ``--strict-mcp-config``) and only the
user's own settings are read. The variables the process inherits from a Claude Code host are
removed, so the command line uses the user's own login. tsw names the session
(``--session-id``) so a later round can continue it (``--resume``). The stream-json output is
read for the final answer, the models that produced it and whether the run failed: a model other
than the configured one fails the round, there is no fallback. Standard library only.
"""

# The CLI's stream-json events are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tsw.errors import ErrorCode, TswError
from tsw.review.codex import ReviewRun
from tsw.review.process import ProcessTree

DEFAULT_EXECUTABLE = "claude"
READ_TOOLS = ("Read", "Grep", "Glob")
# Denied as well, so a default tool a later host adds cannot write indirectly (CC004 §3).
DENIED = (
    "Bash",
    "PowerShell",
    "Edit",
    "Write",
    "NotebookEdit",
    "Agent",
    "Skill",
    "Workflow",
    "WebFetch",
    "WebSearch",
)
# Variables a Claude Code host (or tsw) hands down; the reviewer must not inherit them.
STRIPPED_PREFIXES = ("CLAUDE", "ANTHROPIC", "TSW")
# The model names the command line accepts for "the current model of this family".
ALIASES = ("sonnet", "opus", "haiku", "fable")
SYNTHETIC = "<synthetic>"  # the CLI's own messages, such as API errors


def command(
    executable: str,
    prompt: str,
    *,
    session: str,
    resume: bool,
    model: str,
    effort: str,
    add_dirs: Iterable[Path],
) -> list[str]:
    """The ``claude`` command line of one round (CC004 §3)."""
    argv = [executable, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    argv += ["--resume", session] if resume else ["--session-id", session]
    argv += ["--model", model, "--effort", effort]
    argv += ["--tools", ",".join(READ_TOOLS), "--disallowedTools", ",".join(DENIED)]
    argv += ["--permission-mode", "dontAsk", "--strict-mcp-config", "--safe-mode"]
    argv += ["--setting-sources", "user"]
    for folder in add_dirs:
        argv += ["--add-dir", str(folder)]
    return argv


def child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The reviewer's environment: this process's, without the host's and tsw's variables."""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if not k.upper().startswith(STRIPPED_PREFIXES)}


def run_review(
    prompt: str,
    *,
    cwd: Path,
    model: str,
    effort: str,
    timeout_seconds: int,
    executable: str | None = None,
    resume_thread: str | None = None,
    add_dirs: Iterable[Path] = (),
    **_: Any,
) -> ReviewRun:
    """One read-only round; ``resume_thread`` continues the session of an earlier round."""
    session = resume_thread or str(uuid.uuid4())
    argv = command(
        executable or DEFAULT_EXECUTABLE,
        prompt,
        session=session,
        resume=resume_thread is not None,
        model=model,
        effort=effort,
        add_dirs=add_dirs,
    )
    tree = ProcessTree(
        argv,
        cwd=str(cwd),
        env=child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    errors: list[str] = []
    readers = [
        threading.Thread(target=_drain, args=(tree.proc.stdout, lines), daemon=True),
        threading.Thread(target=_drain, args=(tree.proc.stderr, errors), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        tree.proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        tree.end()
        for reader in readers:
            reader.join(timeout=10)
        # What it printed so far is kept as evidence (rK-raw.txt); timedOut keeps it from being
        # taken as an answer.
        return ReviewRun(
            thread_id=session,
            status="timedOut",
            final_message="".join(lines) or None,
            model=None,
            error=f"claude 在 {timeout_seconds} 秒内没有结束",
        )
    finally:
        tree.end()
    for reader in readers:
        reader.join(timeout=10)
    return read_output(
        lines,
        session=session,
        model=model,
        exit_code=tree.proc.returncode,
        stderr="".join(errors),
    )


def _drain(stream: Any, into: list[str]) -> None:
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        for line in stream:
            into.append(line)
    with contextlib.suppress(OSError):
        stream.close()


def canonical(model: str) -> str:
    """A model name as the host writes it in any of its events: no ``[1m]`` suffix, lower case,
    ``.`` as ``-`` (``claude-sonnet-5.5`` and ``claude-sonnet-5-5[1m]`` are the same model)."""
    return re.sub(r"\[[^\]]*\]$", "", model.strip()).lower().replace(".", "-")


def matches(configured: str, used: str) -> bool:
    """Whether ``used`` is the configured model: the same ID, or of the alias's family."""
    wanted, actual = canonical(configured), canonical(used)
    if wanted in ALIASES:
        return actual.startswith(f"claude-{wanted}-")
    return actual == wanted


def read_output(
    lines: Iterable[str],
    *,
    session: str,
    model: str,
    exit_code: int | None = 0,
    stderr: str = "",
) -> ReviewRun:
    """The round's outcome from the stream-json events (CC004 §4)."""
    events: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(json.JSONDecodeError):
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
    used: list[str] = []
    fallback: str | None = None
    result: dict[str, Any] | None = None
    for event in events:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and event.get("model"):
            used.append(str(event["model"]))
        elif kind == "assistant":
            message = event.get("message") or {}
            name = message.get("model")
            if name and name != SYNTHETIC:
                used.append(str(name))
            text = "".join(
                str(part.get("text") or "")
                for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if text.strip():
                fallback = text
        elif kind == "result":
            result = event
    problems: list[str] = []
    others = sorted({m for m in used if not matches(model, m)})
    if not used:
        # Nothing says which model answered: it cannot be checked, so it is not accepted.
        problems.append("claude 输出中没有实际模型（init 或回复的 model），无法核实")
    elif others:
        problems.append(f"Claude 使用的模型（{'、'.join(others)}）与配置的 {model} 不一致")
    if result is None:
        problems.append(
            "claude 输出中没有 result 事件" + (f"：{stderr.strip()[:500]}" if stderr else "")
        )
    else:
        if result.get("is_error"):
            problems.append(f"claude 报告失败：{str(result.get('result') or '')[:500]}")
        reported = result.get("session_id")
        if reported is not None and reported != session:
            problems.append(f"claude 的会话 ID（{reported}）与请求的 {session} 不一致")
    if exit_code not in (0, None):
        problems.append(f"claude 以退出码 {exit_code} 结束")
    answer = result.get("result") if result is not None else None
    if not isinstance(answer, str) or not answer.strip():
        answer = fallback
    distinct = sorted({canonical(m) for m in used})
    return ReviewRun(
        thread_id=session,
        status="failed" if problems else "completed",
        final_message=answer,
        model=distinct[0] if len(distinct) == 1 else (", ".join(distinct) or None),
        error="；".join(problems) or None,
    )


def resolve(configured: str | None) -> str:
    from shutil import which  # noqa: PLC0415

    found = which(configured or DEFAULT_EXECUTABLE)
    if found is None and configured and Path(configured).is_file():
        found = configured
    if found is None:
        raise TswError(
            ErrorCode.UNAVAILABLE,
            "找不到评审器命令（roles.reviewer.executable 或 PATH 中的 claude）",
            reason="reviewer_unavailable",
            executable=configured or DEFAULT_EXECUTABLE,
        )
    return found
