"""The models a reviewer can use on this machine (DN004 §3.4).

Each reviewer host publishes a way to list its models without calling one: ``codex app-server``
answers ``model/list`` (CX004 §5), ``copilot --acp`` returns the models of a new session
(CP004 §8), and ``claude -p`` answers the control request ``initialize`` (CC004 §8). The lists
are turned into one shape: each model's ID (the value ``roles.reviewer.model`` takes), name,
description and reasoning efforts, and the host's own default model. Listing never fails: when
the list cannot be had, the result says why and the caller offers the user to type a model.
Standard library only.
"""

# The hosts' protocols are untyped JSON walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import contextlib
import json
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsw.config import CLAUDE_ALIASES, DEFAULTS, EFFORTS, MODEL_ID
from tsw.errors import ErrorCode, TswError
from tsw.review import claude
from tsw.review.codex import AppServer
from tsw.review.process import ProcessTree

TIMEOUT = 60  # seconds a host gets to answer (DN004 §3.4)
# The command line each reviewer is listed with (and reviews with).
COMMANDS = {"codex-app-server": "codex", "claude-headless": "claude", "copilot-cli": "copilot"}
# What each reviewer is offered first when its list has it, and suggested when there is no list
# (DN004 §3.4): Codex keeps the configuration's own default.
BUILTIN: dict[str, tuple[str, str]] = {
    "codex-app-server": (
        str(DEFAULTS["roles"]["reviewer"]["model"]),
        str(DEFAULTS["roles"]["reviewer"]["effort"]),
    ),
    "claude-headless": ("claude-sonnet-5-5", "high"),
    "copilot-cli": ("gpt-6.1-sol", "medium"),
}
# Entries that are not one fixed model: Copilot's ``auto`` picks a model per request, Claude's
# ``default`` follows the user's default model; a review must use exactly the configured model.
NOT_FIXED = frozenset({"auto", "default"})


@dataclass(frozen=True)
class Model:
    id: str
    name: str = ""
    description: str = ""
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    # Other names of the same model (Claude's alias resolves to a full ID).
    also: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "name": self.name, "efforts": list(self.efforts)}
        if self.description:
            out["description"] = self.description
        if self.default_effort:
            out["default_effort"] = self.default_effort
        if self.also:
            out["also"] = list(self.also)
        return out


@dataclass
class ModelList:
    reviewer: str
    models: list[Model] = field(default_factory=list[Model])
    host_default: str | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None and bool(self.models)

    def find(self, model: str | None) -> Model | None:
        """The listed model ``model`` names: its ID or another name of it, compared the way the
        reviewers compare models (no ``[1m]`` suffix, case and ``.`` / ``-`` ignored)."""
        if not model:
            return None
        wanted = canonical(model)
        for entry in self.models:
            if wanted == canonical(entry.id):
                return entry
        for entry in self.models:
            if any(wanted == canonical(name) for name in entry.also):
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer": self.reviewer,
            "available": self.available,
            "models": [m.to_dict() for m in self.models],
            "host_default": self.host_default,
            "error": self.error,
        }


def canonical(model: str) -> str:
    return claude.canonical(model)


def _id(value: Any) -> str | None:
    """A model ID as the configuration writes it: no context suffix such as ``[1m]``."""
    if not isinstance(value, str):
        return None
    found = re.sub(r"\[[^\]]*\]$", "", value.strip())
    return found if found and MODEL_ID.match(found) and found not in NOT_FIXED else None


def _strings(values: Iterable[Any]) -> tuple[str, ...]:
    """Reasoning efforts a reviewer configuration accepts (DN003 §3.1), in the host's order: a
    host's own extra level (Codex's ``ultra``) cannot be written, so it is not offered."""
    return tuple(dict.fromkeys(str(v) for v in values if isinstance(v, str) and v in EFFORTS))


# ---------------------------------------------------------------------- parsing


def parse_codex(pages: Iterable[Mapping[str, Any]]) -> ModelList:
    """``model/list`` results (CX004 §5); hidden models are left out."""
    out = ModelList("codex-app-server")
    for page in pages:
        for item in page.get("data") or []:
            if not isinstance(item, dict) or item.get("hidden"):
                continue
            model = _id(item.get("id") or item.get("model"))
            if model is None or out.find(model):
                continue
            efforts = _strings(
                e.get("reasoningEffort")
                for e in item.get("supportedReasoningEfforts") or []
                if isinstance(e, dict)
            )
            default = item.get("defaultReasoningEffort")
            out.models.append(
                Model(
                    model,
                    str(item.get("displayName") or model),
                    str(item.get("description") or ""),
                    efforts,
                    default if isinstance(default, str) else None,
                )
            )
            if item.get("isDefault"):
                out.host_default = model
    return out


def parse_copilot(session: Mapping[str, Any]) -> ModelList:
    """An ACP ``session/new`` result (CP004 §8): ``auto`` and disabled models are left out; the
    reasoning efforts are the session's, the same for every model."""
    out = ModelList("copilot-cli")
    efforts: tuple[str, ...] = ()
    for option in session.get("configOptions") or []:
        if isinstance(option, dict) and option.get("id") == "reasoning_effort":
            efforts = _strings(
                o.get("value") for o in option.get("options") or [] if isinstance(o, dict)
            )
            current = option.get("currentValue")
            default_effort = current if isinstance(current, str) else None
            break
    else:
        default_effort = None
    models = session.get("models") or {}
    for item in models.get("availableModels") or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("_meta") or {}
        if isinstance(meta, dict) and meta.get("copilotEnablement") not in (None, "enabled"):
            continue
        model = _id(item.get("modelId"))
        if model is None or out.find(model):
            continue
        out.models.append(
            Model(
                model,
                str(item.get("name") or model),
                str(item.get("description") or ""),
                efforts,
                default_effort,
            )
        )
    current_model = _id(models.get("currentModelId"))
    out.host_default = current_model if out.find(current_model) else None
    return out


def parse_claude(response: Mapping[str, Any]) -> ModelList:
    """The ``initialize`` control response (CC004 §8): ``default`` is left out, and so are the
    models the reviewer cannot check (not a ``claude-…`` ID or an alias, CC004 §4: a gateway's
    own names); an alias keeps the full ID it resolves to as another name."""
    out = ModelList("claude-headless")
    for item in response.get("models") or []:
        if not isinstance(item, dict):
            continue
        model = _id(item.get("value"))
        if model is None or out.find(model):
            continue
        if model not in CLAUDE_ALIASES and not model.startswith("claude-"):
            continue
        resolved = _id(item.get("resolvedModel"))
        out.models.append(
            Model(
                model,
                str(item.get("displayName") or model),
                str(item.get("description") or ""),
                _strings(item.get("supportedEffortLevels") or []),
                None,
                (resolved,) if resolved and resolved != model else (),
            )
        )
    return out


# ---------------------------------------------------------------------- the hosts


def _codex(executable: str, timeout: float) -> ModelList:
    server = AppServer(executable)
    try:
        server.call(
            "initialize",
            {"clientInfo": {"name": "tsw-models", "version": "1"}, "capabilities": {}},
            timeout=timeout,
        )
        server.notify("initialized")
        pages: list[dict[str, Any]] = []
        cursor: Any = None
        for _ in range(20):  # a bounded number of pages
            page = server.call("model/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            if not isinstance(page, dict):
                break
            pages.append(page)
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return parse_codex(pages)
    finally:
        server.close()


def _copilot(executable: str, timeout: float) -> ModelList:
    # The folder may still be held for a moment by the ended process tree (Windows).
    with tempfile.TemporaryDirectory(prefix="tsw-models-", ignore_cleanup_errors=True) as folder:
        server = AppServer(
            executable,
            args=("--acp", "--no-auto-update", "--disable-builtin-mcps"),
            cwd=folder,
        )
        try:
            server.call(
                "initialize",
                {"protocolVersion": 1, "clientCapabilities": {}},
                timeout=timeout,
            )
            session = server.call("session/new", {"cwd": folder, "mcpServers": []}, timeout=timeout)
            if not isinstance(session, dict):
                return ModelList("copilot-cli", error="copilot 的 session/new 没有返回会话")
            with contextlib.suppress(TswError, TimeoutError):
                server.call("session/close", {"sessionId": session.get("sessionId")}, timeout=10)
            return parse_copilot(session)
        finally:
            server.close()


def claude_command(executable: str) -> list[str]:
    """The ``claude`` command line that only answers the control protocol (CC004 §8)."""
    return [
        executable,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "Read",
        "--permission-mode",
        "dontAsk",
        "--strict-mcp-config",
        "--safe-mode",
        "--setting-sources",
        "user",
    ]


def _claude(executable: str, timeout: float) -> ModelList:
    with tempfile.TemporaryDirectory(prefix="tsw-models-", ignore_cleanup_errors=True) as folder:
        tree = ProcessTree(
            claude_command(executable),
            cwd=folder,
            env=claude.child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def read() -> None:
            """Queue every line until end of file; the pipe is closed here, by its only
            reader (as ``AppServer`` does)."""
            stdout = tree.proc.stdout
            assert stdout is not None
            try:
                with contextlib.suppress(OSError, ValueError):
                    for line in stdout:
                        lines.put(line)
            finally:
                lines.put(None)
                with contextlib.suppress(OSError):
                    stdout.close()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        request = str(uuid.uuid4())
        try:
            assert tree.proc.stdin is not None
            message = {
                "type": "control_request",
                "request_id": request,
                "request": {"subtype": "initialize"},
            }
            tree.proc.stdin.write(json.dumps(message) + "\n")
            tree.proc.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError from exc
                if line is None:
                    return ModelList("claude-headless", error="claude 没有回应 initialize 就退出了")
                with contextlib.suppress(json.JSONDecodeError):
                    event = json.loads(line)
                    if (
                        isinstance(event, dict)
                        and event.get("type") == "control_response"
                        and (event.get("response") or {}).get("request_id") == request
                    ):
                        response = event["response"]
                        if response.get("subtype") == "error":
                            return ModelList(
                                "claude-headless",
                                error=f"claude 拒绝了 initialize：{response.get('error')}",
                            )
                        return parse_claude(response.get("response") or {})
        finally:
            with contextlib.suppress(OSError):
                if tree.proc.stdin:
                    tree.proc.stdin.close()
            # End of input ends the command line; whatever is left of its tree is ended.
            with contextlib.suppress(subprocess.TimeoutExpired):
                tree.proc.wait(timeout=5)
            tree.end()
            reader.join(timeout=5)


LISTERS: dict[str, Callable[[str, float], ModelList]] = {
    "codex-app-server": _codex,
    "claude-headless": _claude,
    "copilot-cli": _copilot,
}


def list_models(reviewer: str, executable: str | None, *, timeout: float = TIMEOUT) -> ModelList:
    """The models ``reviewer`` can use, read from its command line ``executable`` (already
    resolved; None when it was not found). Never raises: a failure is the result's ``error``."""
    lister = LISTERS.get(reviewer)
    if lister is None:
        return ModelList(reviewer, error=f"{reviewer} 不是本版本实现的评审器")
    if not executable:
        return ModelList(reviewer, error=f"找不到 {COMMANDS[reviewer]} 命令行")
    try:
        found = lister(executable, timeout)
    except TimeoutError:
        return ModelList(
            reviewer, error=f"{COMMANDS[reviewer]} 在 {int(timeout)} 秒内没有给出模型列表"
        )
    except TswError as exc:
        detail = exc.details.get("error")
        reason = f"：{json.dumps(detail, ensure_ascii=False)[:300]}" if detail else ""
        return ModelList(reviewer, error=f"{exc.message}{reason}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ModelList(reviewer, error=f"取模型列表失败：{exc}")
    if found.error is None and not found.models:
        found.error = f"{COMMANDS[reviewer]} 返回的模型列表是空的"
    return found


# ---------------------------------------------------------------------- choosing


def preselect_model(found: ModelList, current: str | None) -> str | None:
    """The model offered first (DN004 §3.4): the current one, else the built-in default, when
    listed; else the host's default; else the first listed. Without a list, the current one or
    the built-in default."""
    builtin = BUILTIN.get(found.reviewer, (None, None))[0]
    if not found.available:
        return current or builtin
    for candidate in (current, builtin, found.host_default):
        entry = found.find(candidate)
        if entry is not None:
            return entry.id
    return found.models[0].id


def preselect_effort(found: ModelList, model: str | None, current: str | None) -> str | None:
    """The reasoning effort offered first: the current one, else the built-in default, when the
    model supports it; else the model's own default; else the first it supports."""
    builtin = BUILTIN.get(found.reviewer, (None, None))[1]
    entry = found.find(model)
    if entry is None or not entry.efforts:
        return current or builtin
    for candidate in (current, builtin, entry.default_effort):
        if candidate in entry.efforts:
            return candidate
    return entry.efforts[0]


def efforts_for(found: ModelList, model: str | None) -> tuple[str, ...]:
    entry = found.find(model)
    return entry.efforts if entry is not None else ()


# ---------------------------------------------------------------------- the configured reviewer


def resolve(reviewer: str, configured: str | None = None) -> str | None:
    """The command line ``reviewer`` is listed with: ``roles.reviewer.executable`` (for the
    configured reviewer) or its command on ``PATH``; None when there is none."""
    import shutil  # noqa: PLC0415

    command = COMMANDS.get(reviewer)
    if command is None:
        return None
    found = shutil.which(configured or command)
    if found is None and configured and Path(configured).is_file():
        found = configured
    return found


def current(values: Mapping[str, Any]) -> dict[str, Any]:
    """The configured reviewer, model and effort (DN003 §3.1)."""
    reviewer = values.get("roles", {}).get("reviewer", {})
    review = values.get("review", {})
    return {
        "reviewer": reviewer.get("session"),
        "model": reviewer.get("model"),
        "effort": reviewer.get("effort"),
        "executable": reviewer.get("executable"),
        "self_review": review.get("self_review"),
    }


def offer(values: Mapping[str, Any], reviewer: str | None = None) -> dict[str, Any]:
    """What ``tsw reviewer models`` and the ``reviewer_models`` tool show (DN004 §3.4): the
    models of ``reviewer`` (default: the configured one), whether its command line was found,
    the configuration, and the answer offered first."""
    now = current(values)
    target = reviewer or str(now["reviewer"] or "")
    if target not in LISTERS:
        raise TswError(
            ErrorCode.REJECTED,
            f"评审器须为 {' / '.join(LISTERS)} 之一",
            reason="invalid_input",
            reviewer=target,
        )
    same = target == now["reviewer"]
    executable = resolve(target, now["executable"] if same else None)
    found = list_models(target, executable)
    model = preselect_model(found, now["model"] if same else None)
    effort = preselect_effort(found, model, now["effort"] if same else None)
    out = found.to_dict()
    # Whether the reviewer's command line was found, apart from whether it listed its models: a
    # list that cannot be had (timeout, protocol error) does not mean the reviewer cannot review
    # (T336 review r1 F1).
    out["command_found"] = executable is not None
    configured = found.find(now["model"]) if same else None
    for entry in out["models"]:
        entry["current"] = configured is not None and configured.id == entry["id"]
    out["current"] = {k: now[k] for k in ("reviewer", "model", "effort", "self_review")}
    out["preselected"] = {"model": model, "effort": effort}
    return out


def choose(
    found: ModelList, model: str, effort: str, *, unlisted: bool = False
) -> tuple[str, str, list[str]]:
    """Check a choice against the list (DN004 §3.4): the model and effort to write, and notes
    for the user. A model the list does not have, or an effort the model does not support, is
    refused unless ``unlisted`` (the host's list may be incomplete); without a list it is
    refused unless ``unlisted`` too, and then written as not verified."""
    notes: list[str] = []
    model = model.strip()
    effort = effort.strip()
    if not MODEL_ID.match(model):
        raise TswError(
            ErrorCode.REJECTED, "模型 ID 格式不正确", reason="invalid_input", model=model
        )
    if not effort:
        raise TswError(ErrorCode.REJECTED, "推理强度不能为空", reason="invalid_input")
    if not found.available:
        if not unlisted:
            raise TswError(
                ErrorCode.REJECTED,
                f"取不到 {found.reviewer} 的模型列表（{found.error}）：确认模型 ID 无误时加 "
                "--unlisted 写入（未核实，第一轮评审时核实）",
                reason="model_list_unavailable",
            )
        notes.append(f"{model} 未经核实：取不到模型列表（{found.error}），第一轮评审时核实")
        return model, effort, notes
    entry = found.find(model)
    if entry is None:
        if not unlisted:
            raise TswError(
                ErrorCode.REJECTED,
                f"{model} 不在 {found.reviewer} 的可用模型中（拼错，或宿主的策略不允许）；"
                "确认要写入列表外的模型时加 --unlisted",
                reason="model_not_listed",
                models=[m.id for m in found.models],
            )
        notes.append(f"{model} 不在 {found.reviewer} 的模型列表中，第一轮评审时核实")
        return model, effort, notes
    if entry.efforts and effort not in entry.efforts:
        if not unlisted:
            raise TswError(
                ErrorCode.REJECTED,
                f"{model} 不支持推理强度 {effort}（可选：{' / '.join(entry.efforts)}）",
                reason="effort_not_supported",
                efforts=list(entry.efforts),
            )
        notes.append(f"{model} 的列表中没有推理强度 {effort}，第一轮评审时核实")
    return model, effort, notes


def ops(
    values: Mapping[str, Any],
    reviewer: str,
    model: str,
    effort: str,
    sources: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The ``config_change`` operations that choose ``reviewer``, ``model`` and ``effort``: a
    new reviewer drops the old one's fallback and command line (``roles.reviewer.executable``
    names the old reviewer's command, DN004 §3.4). The command line set in the machine-local
    override cannot be changed here (DN003 §5): that is refused, saying what to edit."""
    now = current(values)
    out: list[dict[str, Any]] = []
    if reviewer != now["reviewer"]:
        out.append({"op": "set", "path": "roles.reviewer.session", "value": reviewer})
        out.append({"op": "set", "path": "roles.reviewer.fallback_session", "value": None})
        if now["executable"]:
            if (sources or {}).get("roles.reviewer.executable") == "local":
                raise TswError(
                    ErrorCode.REJECTED,
                    "本机覆盖 config/task-steward.local.json 的 roles.reviewer.executable 指向 "
                    f"{now['executable']}（{now['reviewer']} 的命令行），换成 {reviewer} 后评审"
                    "仍会启动它：先在该文件中删去这一项或改为新评审器的命令行，再更换",
                    reason="local_executable",
                    executable=now["executable"],
                )
            out.append({"op": "set", "path": "roles.reviewer.executable", "value": None})
    if model != now["model"]:
        out.append({"op": "set", "path": "roles.reviewer.model", "value": model})
    if effort != now["effort"]:
        out.append({"op": "set", "path": "roles.reviewer.effort", "value": effort})
    return out
