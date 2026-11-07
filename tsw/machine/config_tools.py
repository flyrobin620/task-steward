"""Orchestrator tools that change the unified configuration (DN003 §7).

They run even while the current configuration is invalid (a broken hand edit must be fixable),
but the configuration they produce must be valid. Keys that grant or withdraw authority need the
user's own words (``user_source``), bound to the file the change starts from.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tsw import confedit
from tsw.commands import Command, CommitContext
from tsw.config import load_config
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.machine.base import ToolEnv, items, mapping, opt_text, text, tool
from tsw.machine.guards import verified_source

CONFIG_TOOLS = ("config_change", "config_revert")
# The user authorizes a configuration change in the orchestrator's session or a human channel;
# a worker session (``in-session``) cannot authorize the orchestrator's configuration.
CONFIG_CHANNELS = ("main-session", "teams-chat", "hub-portal")


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def _check_expected(inp: Mapping[str, Any], current_sha: str) -> None:
    expected = opt_text(inp, "expected_sha256")
    if expected is not None and expected != current_sha:
        raise rejected(
            "配置文件已被修改（摘要不一致）",
            reason="config_conflict",
            expected=expected,
            actual=current_sha,
        )


def _require_unused(ctx: CommitContext, source: Mapping[str, Any]) -> None:
    """One user message authorizes one change: after a change and its exact revert, the same
    words cannot authorize the first change again (a retry uses the same ``command_id``)."""
    wanted = confedit.source_ref(source)
    for event in confedit.change_events(ctx.storage):
        if confedit.source_ref(event.data.get("user_source")) == wanted:
            raise rejected(
                "这条用户原话已经授权过一次配置修改",
                reason="source_reused",
                change_id=event.data.get("change_id"),
            )


def _change_event(
    ctx: CommitContext,
    inp: Mapping[str, Any],
    *,
    kind: str,
    before_text: str | None,
    before: Mapping[str, Any],
    after_text: str | None,
    after: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> EventDraft:
    candidate = load_config(ctx.home, main_layer=dict(after))
    if not candidate.valid:
        raise rejected(
            "修改后的配置无效，未写入", reason="config_invalid", problems=candidate.problems
        )
    changed = confedit.changed_keys(before, after)
    if not changed and before_text == after_text:
        raise rejected("配置没有变化", reason="no_change")
    permission = confedit.permission_keys(before, after, changed)
    before_sha = confedit.text_sha(before_text)
    source = None
    if permission:
        source = verified_source(
            mapping(inp, "user_source", required=False),
            ["config", before_sha, "authorize"],
            channels=CONFIG_CHANNELS,
            mismatch="用户来源应授权这一次修改："
            'target 为 ["config", 修改前文件的摘要, "authorize"]',
        )
        _require_unused(ctx, source)
    return EventDraft(
        type=confedit.CHANGE_EVENT,
        data={
            "change_id": confedit.next_change_id(ctx),
            "kind": kind,
            **extra,
            "reason": text(inp, "reason"),
            "changed": confedit.dotted(changed),
            "changed_keys": [list(k) for k in changed],
            "permission": confedit.dotted(permission),
            "user_source": source,
            "before_sha256": before_sha,
            "after_sha256": confedit.text_sha(after_text),
            "before": before_text,
            "after": after_text,
            "warnings": candidate.warnings,
        },
    )


def _replaces_file(ops: list[Any]) -> bool:
    """Whether the first operation replaces the whole file (``set`` with an empty path)."""
    first: Any = ops[0] if ops else None
    if not isinstance(first, dict):
        return False
    op: dict[str, Any] = first  # pyright: ignore[reportUnknownVariableType]
    return op.get("op") == "set" and confedit.split_path(op.get("path")) == []


@tool(
    "config_change",
    "orchestrator",
    description="修改统一配置（校验通过才写入；权限类键须附用户原话）",
)
def config_change(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    ops = items(inp, "ops")
    text(inp, "reason")

    def build(ctx: CommitContext) -> list[EventDraft]:
        before_text = confedit.read_text(ctx.home)
        _check_expected(inp, confedit.text_sha(before_text))
        before = confedit.parse(before_text)
        replaces = _replaces_file(ops)
        if before is None and not replaces:
            raise rejected(
                "当前配置文件不是 JSON 对象：只能先用空路径的 set 替换整个文件",
                reason="config_unreadable",
            )
        after = confedit.apply_ops(before or {}, ops)
        return [
            _change_event(
                ctx,
                inp,
                kind="change",
                before_text=before_text,
                before=before or {},
                after_text=confedit.render(after),
                after=after,
                extra={"ops": list(ops)},
            )
        ]

    return _cmd(env, "config_change", inp, build)


@tool(
    "config_revert",
    "orchestrator",
    description="把配置文件恢复到某次修改之前的内容（同样校验、记账、检查权限类键）",
)
def config_revert(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    target = text(inp, "to_change")
    text(inp, "reason")

    def build(ctx: CommitContext) -> list[EventDraft]:
        event = confedit.find_change(ctx.storage, target)
        before_text = confedit.read_text(ctx.home)
        _check_expected(inp, confedit.text_sha(before_text))
        after_text = event.data.get("before")
        after = confedit.parse(after_text)
        if after is None:
            raise rejected("该次修改之前的配置不可解析", reason="config_unreadable")
        return [
            _change_event(
                ctx,
                inp,
                kind="revert",
                before_text=before_text,
                before=confedit.parse(before_text) or {},
                after_text=after_text,
                after=after,
                extra={"reverts": target},
            )
        ]

    return _cmd(env, "config_revert", inp, build)
