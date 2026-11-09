"""Shared pieces of the state-machine tools: callers, the tool registry and input helpers.

A tool turns a JSON input and a caller into a :class:`tsw.commands.Command`. The same registry is
used by the CLI (``tsw tool``) and, later, the MCP server (DN001 §7.2), so both share one
implementation. Orchestrator write tools require the scheduling lease (G-LEASE); worker tools
require the caller to be the session bound to the task's current attempt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.lease import require_lease
from tsw.machine.model import current_attempt, task

ROLES = ("orchestrator", "worker", "routine", "system")


@dataclass(frozen=True)
class Caller:
    role: str
    session_id: str | None
    machine: str

    def actor(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        if self.session_id:
            out["session_id"] = self.session_id
        return out


@dataclass(frozen=True)
class ToolEnv:
    config: EffectiveConfig
    caller: Caller


Handler = Callable[[Mapping[str, Any], ToolEnv], Command]


@dataclass(frozen=True)
class Tool:
    name: str
    roles: tuple[str, ...]
    handler: Handler
    lease: bool = True  # orchestrator callers must hold the scheduling lease
    sync_after: bool = False  # run the dependency rules after the command commits
    description: str = ""


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=lambda: {})

    def add(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self.tools.get(name)
        if tool is None:
            raise rejected("未知的工具", reason="unknown_tool", tool=name)
        return tool


REGISTRY = Registry()


def tool(
    name: str,
    *roles: str,
    lease: bool = True,
    sync_after: bool = False,
    description: str = "",
) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        REGISTRY.add(
            Tool(
                name,
                tuple(roles),
                handler,
                lease=lease,
                sync_after=sync_after,
                description=description,
            )
        )
        return handler

    return register


# ---------------------------------------------------------------------- input helpers


def text(inp: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = inp.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise rejected("缺少或无效的字段", reason="invalid_input", field=key)
    return value


def opt_text(inp: Mapping[str, Any], key: str) -> str | None:
    value = inp.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise rejected("字段应为字符串", reason="invalid_input", field=key)
    return value


def integer(inp: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = inp.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise rejected("字段应为整数", reason="invalid_input", field=key, minimum=minimum)
    return value


def flag(inp: Mapping[str, Any], key: str) -> bool:
    value = inp.get(key, False)
    if not isinstance(value, bool):
        raise rejected("字段应为布尔值", reason="invalid_input", field=key)
    return value


def items(inp: Mapping[str, Any], key: str, *, required: bool = True) -> list[Any]:
    value = inp.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise rejected("字段应为数组", reason="invalid_input", field=key)
    return list(value)  # pyright: ignore[reportUnknownArgumentType]


def mapping(inp: Mapping[str, Any], key: str, *, required: bool = True) -> dict[str, Any] | None:
    value = inp.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, dict):
        raise rejected("字段应为对象", reason="invalid_input", field=key)
    return dict(value)  # pyright: ignore[reportUnknownArgumentType]


def strings(values: list[Any], field_name: str) -> list[str]:
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise rejected("数组元素应为非空字符串", reason="invalid_input", field=field_name)
    return [str(v) for v in values]


# ---------------------------------------------------------------------- caller checks


def check_caller(ctx: CommitContext, env: ToolEnv, spec: Tool) -> None:
    caller = env.caller
    if caller.role not in spec.roles:
        raise rejected(
            "当前角色不能使用该工具", reason="role_denied", role=caller.role, tool=spec.name
        )
    if caller.role == "orchestrator" and spec.lease:
        if not caller.session_id:
            raise rejected("主会话工具需要会话 ID", reason="lease_missing")
        require_lease(ctx.conn, caller.session_id, caller.machine, ctx.now)


def check_version(ctx: CommitContext, inp: Mapping[str, Any]) -> None:
    """Optimistic concurrency (DN001 §7.2): refuse a decision made on an older task version."""
    expected = inp.get("expected_version")
    if expected is None:
        return
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise rejected(
            "expected_version 应为整数", reason="invalid_input", field="expected_version"
        )
    task_id = inp.get("task_id")
    if not isinstance(task_id, str):
        raise rejected("expected_version 需要 task_id", reason="invalid_input", field="task_id")
    actual = int(task(ctx.conn, task_id)["version"])
    if actual != expected:
        raise rejected(
            "任务已被修改（版本不一致）",
            reason="version_conflict",
            expected=expected,
            actual=actual,
        )


def worker_attempt(conn: sqlite3.Connection, env: ToolEnv, task_id: str) -> dict[str, Any]:
    """The caller must be the live session bound to the task's current attempt."""
    row = current_attempt(conn, task_id)
    if (
        row is None
        or row["phase"] != "live"
        or not env.caller.session_id
        or row["session_ref"] != env.caller.session_id
    ):
        raise rejected(
            "调用者不是该任务当前 attempt 绑定的 worker 会话",
            reason="not_bound_worker",
            task_id=task_id,
        )
    return row


def task_and_attempt(
    conn: sqlite3.Connection, env: ToolEnv, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    return task(conn, task_id), worker_attempt(conn, env, task_id)
