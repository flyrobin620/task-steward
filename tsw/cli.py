"""The ``tsw`` command line.

Exit codes: 0 success; 2 usage error; 3 REJECTED; 4 QUIESCED; 5 UNAVAILABLE; 6 UNKNOWN;
7 UNSUPPORTED; 8 AUTH; 1 unexpected error. With ``--json`` results go to stdout as one JSON
object and errors go to stderr as one JSON object.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from tsw import __version__, lease
from tsw.config import EffectiveConfig, lease_minutes, load_config
from tsw.errors import EXIT_CODES, ErrorCode, TswError
from tsw.events import utc_now
from tsw.home import Home, init_home, open_home, record_product_repo, resolve_home
from tsw.host.claude_code import install_hint, render_instructions
from tsw.host.guard import run as run_guard
from tsw.host.identity import HOST_CLI_COMMANDS, SESSION_ENV
from tsw.host.identity import resolve as resolve_identity
from tsw.host.mcp import serve
from tsw.importer import renumber
from tsw.importer.orchestrator_agent import run_import as run_import_orchestrator_agent
from tsw.ledger import Ledger
from tsw.machine.base import REGISTRY, Caller
from tsw.machine.config_tools import CONFIG_TOOLS
from tsw.machine.runner import run_tool
from tsw.review.run import run_round
from tsw.storage.fs import FsStorage, restore
from tsw.tasks import KINDS, TaskSpec, list_tasks, note, register, task_row

CLI_ACTOR = {"role": "cli"}
REPO_ROOT = Path(__file__).resolve().parent.parent

Handler = Callable[[argparse.Namespace], dict[str, Any]]


def _home(args: argparse.Namespace) -> Home:
    root = Path(args.home) if args.home else resolve_home(repo_root=REPO_ROOT)
    return open_home(root)


def _ledger_for(home: Home, config: EffectiveConfig, machine: str | None = None) -> Ledger:
    storage = FsStorage(home, segment_max_bytes=int(config.get("ledger.segment_max_mb")) << 20)
    return Ledger(
        home,
        storage,
        checkpoint_every=int(config.get("ledger.checkpoint_every")),
        machine=machine,
    )


def _ledger(args: argparse.Namespace) -> Ledger:
    """A ledger for read commands; an invalid configuration falls back to defaults for reads."""
    home = _home(args)
    config = load_config(home)
    return _ledger_for(home, config) if config.valid else Ledger(home)


def _writer(args: argparse.Namespace) -> tuple[Ledger, EffectiveConfig]:
    """A ledger for write commands: refused while the configuration is invalid (DN003 §4)."""
    home = _home(args)
    config = load_config(home).require_valid()
    return _ledger_for(home, config, getattr(args, "machine", None)), config


def _cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.home) if args.home else resolve_home(repo_root=REPO_ROOT)
    home, created = init_home(root)
    record_product_repo(home, REPO_ROOT)
    return {"home": str(home.root), "created": created, "product_repo": str(REPO_ROOT)}


def _cmd_home(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.home) if args.home else resolve_home(repo_root=REPO_ROOT)
    return {"home": str(root), "initialized": (root / "tsw-home.json").is_file()}


def _cmd_task_register(args: argparse.Namespace) -> dict[str, Any]:
    spec = TaskSpec(
        task_id=args.task_id,
        title=args.title,
        repo=args.repo,
        kind=args.kind,
        goal=args.goal,
        acceptance=args.acceptance or [],
    )
    result = _writer(args)[0].commit(register(spec, actor=CLI_ACTOR, command_id=args.command_id))
    event = result.events[0]
    return {"task_id": event.task_id, "sequence": event.sequence, "duplicate": result.duplicate}


def _cmd_task_note(args: argparse.Namespace) -> dict[str, Any]:
    command = note(
        args.task_id,
        args.text,
        actor=CLI_ACTOR,
        expected_version=args.expected_version,
        command_id=args.command_id,
    )
    result = _writer(args)[0].commit(command)
    return {
        "task_id": args.task_id,
        "sequence": result.last_sequence,
        "duplicate": result.duplicate,
    }


def _cmd_task_list(args: argparse.Namespace) -> dict[str, Any]:
    with _ledger(args).view() as conn:
        return {"tasks": list_tasks(conn)}


def _cmd_task_show(args: argparse.Namespace) -> dict[str, Any]:
    with _ledger(args).view() as conn:
        return {"task": task_row(conn, args.task_id)}


def _cmd_ledger_tail(args: argparse.Namespace) -> dict[str, Any]:
    tail = FsStorage(_home(args)).tail()
    return {"sequence": tail.sequence, "checksum": tail.checksum}


def _cmd_ledger_audit(args: argparse.Namespace) -> dict[str, Any]:
    return _ledger(args).audit()


def _cmd_ledger_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    tail = _writer(args)[0].rebuild()
    return {"projected_seq": tail.sequence}


def _cmd_backup(args: argparse.Namespace) -> dict[str, Any]:
    manifest = FsStorage(_home(args)).snapshot(Path(args.dest))
    return {
        "dest": args.dest,
        "ledger_tail": manifest["ledger_tail"],
        "files": len(manifest["files"]),
    }


def _cmd_restore(args: argparse.Namespace) -> dict[str, Any]:
    dest = Path(args.home) if args.home else resolve_home(repo_root=REPO_ROOT)
    problems = restore(Path(args.src), dest, allow_unverified=args.allow_unverified)
    ledger = Ledger(open_home(dest))
    report = ledger.audit()
    if not report["ok"]:
        return {"restored": str(dest), "audit": report, "ok": False}
    tail = ledger.rebuild()
    return {"restored": str(dest), "accepted_problems": problems, "projected_seq": tail.sequence}


def _cmd_config_show(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(_home(args))
    return {
        "valid": config.valid,
        "values": config.values,
        "sources": config.sources,
        "problems": config.problems,
        "warnings": config.warnings,
    }


def _cmd_config_validate(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(_home(args))
    return {"ok": config.valid, "problems": config.problems, "warnings": config.warnings}


def _cmd_lease_show(args: argparse.Namespace) -> dict[str, Any]:
    with _ledger(args).view() as conn:
        row = lease.lease_row(conn)
    if row is None:
        return {"lease": None}
    return {"lease": {**row, "expired": lease.is_expired(row, utc_now())}}


def _lease_ledger(args: argparse.Namespace) -> tuple[Ledger, EffectiveConfig]:
    """Lease commands work while the configuration is invalid: repairing it with
    ``config_change`` needs the lease (DN003 §7.1)."""
    home = _home(args)
    config = load_config(home)
    machine = getattr(args, "machine", None)
    return (
        _ledger_for(home, config, machine) if config.valid else Ledger(home, machine=machine)
    ), config


def _cmd_lease_acquire(args: argparse.Namespace) -> dict[str, Any]:
    ledger, config = _lease_ledger(args)
    command = lease.acquire(
        args.session,
        machine=args.machine,
        lease_minutes=lease_minutes(config),
        actor={"role": "orchestrator", "session_id": args.session},
        command_id=args.command_id,
    )
    event = ledger.commit(command).events[-1]
    return {"session_id": args.session, "expires_at": event.data["expires_at"]}


def _cmd_lease_release(args: argparse.Namespace) -> dict[str, Any]:
    ledger, _ = _lease_ledger(args)
    ledger.commit(
        lease.release(
            args.session,
            machine=args.machine,
            actor={"role": "orchestrator", "session_id": args.session},
        )
    )
    return {"released": True}


def _cmd_lease_takeover(args: argparse.Namespace) -> dict[str, Any]:
    ledger, config = _lease_ledger(args)
    try:
        source = json.loads(args.user_source)
    except json.JSONDecodeError as exc:
        raise TswError(ErrorCode.REJECTED, "--user-source 必须是 JSON 对象") from exc
    if not isinstance(source, dict):
        raise TswError(ErrorCode.REJECTED, "--user-source 必须是 JSON 对象")
    command = lease.takeover(
        args.session,
        machine=args.machine,
        from_session=args.from_session,
        user_source=source,  # pyright: ignore[reportUnknownArgumentType]
        lease_minutes=lease_minutes(config),
        from_machine=args.from_machine,
        actor={"role": "orchestrator", "session_id": args.session},
        command_id=args.command_id,
    )
    event = ledger.commit(command).events[-1]
    return {"session_id": args.session, "expires_at": event.data["expires_at"]}


def _cmd_tool_list(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "tools": [
            {"name": name, "roles": list(spec.roles), "description": spec.description}
            for name, spec in sorted(REGISTRY.tools.items())
        ]
        + [{"name": "sync", "roles": ["orchestrator", "system"], "description": "系统规则"}]
    }


def _cmd_tool_run(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.input
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TswError(ErrorCode.REJECTED, "--input 必须是 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise TswError(ErrorCode.REJECTED, "--input 必须是 JSON 对象")
    if args.name in CONFIG_TOOLS:
        # Configuration tools repair an invalid configuration; they validate their own result.
        ledger, config = _lease_ledger(args)
    else:
        ledger, config = _writer(args)
    caller = Caller(role=args.role, session_id=args.session, machine=args.machine)
    return run_tool(ledger, config, caller, args.name, payload)  # pyright: ignore[reportUnknownArgumentType]


def _cmd_mcp(args: argparse.Namespace) -> dict[str, Any]:
    serve(_home(args).root)
    return {"_silent": True}


def _cmd_guard(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.home) if args.home else resolve_home(repo_root=REPO_ROOT)
    if not (root / "tsw-home.json").is_file():
        return {"_silent": True}  # the plugin is installed but this machine has no data directory
    output = run_guard(args.event, open_home(root), dict(os.environ), sys.stdin.read())
    if output:
        print(output)
    return {"_silent": True}


def _cmd_host_render(args: argparse.Namespace) -> dict[str, Any]:
    home = _home(args)
    with FsStorage(home).writer():
        written = render_instructions(home)
    return {"written": [str(p) for p in written]}


def _cmd_host_install_hint(args: argparse.Namespace) -> dict[str, Any]:
    return {"hint": install_hint(_home(args))}


def _cmd_review_run(args: argparse.Namespace) -> dict[str, Any]:
    notes = args.notes
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8")
    if not os.environ.get(SESSION_ENV):
        raise TswError(
            ErrorCode.REJECTED, "需要在绑定任务的 worker 会话中运行", reason="role_denied"
        )
    return run_round(
        _home(args), os.environ, Path.cwd(), head=args.head, base=args.base, notes=notes
    )


def _cmd_import_orchestrator_agent(args: argparse.Namespace) -> dict[str, Any]:
    ledger, _ = _writer(args)
    return run_import_orchestrator_agent(
        ledger.home,
        ledger,
        Path(args.snapshot),
        dry_run=args.dry_run,
        renumber_local=renumber.parse(args.renumber_local),
        exclude=args.exclude,
    )


def _cmd_quiesce(args: argparse.Namespace) -> dict[str, Any]:
    manifest = FsStorage(_home(args)).quiesce()
    return {"quiesced": True, "ledger_tail": manifest["ledger_tail"]}


def _cmd_resume_writes(args: argparse.Namespace) -> dict[str, Any]:
    FsStorage(_home(args)).resume_writes()
    return {"quiesced": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsw", description="TaskSteward 命令行")
    parser.add_argument("--version", action="version", version=f"tsw {__version__}")
    parser.add_argument("--home", help="数据目录（默认按 TSW_HOME 等规则定位）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    # The global options are also accepted after the command (``tsw task list --json``).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS, help="数据目录")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON 输出")

    def add(target: Any, name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        command: argparse.ArgumentParser = target.add_parser(name, help=help_text, parents=[common])
        command.set_defaults(handler=handler)
        return command

    add(sub, "init", _cmd_init, "初始化数据目录")
    add(sub, "home", _cmd_home, "显示数据目录位置")

    task = sub.add_parser("task", help="任务").add_subparsers(
        dest="task_command", required=True, metavar="<task command>"
    )
    reg = add(task, "register", _cmd_task_register, "登记任务（T01）")
    reg.add_argument("--task-id")
    reg.add_argument("--title", required=True)
    reg.add_argument("--repo", required=True)
    reg.add_argument("--kind", required=True, choices=sorted(KINDS))
    reg.add_argument("--goal", required=True)
    reg.add_argument("--acceptance", action="append", help="验收项，可重复")
    reg.add_argument("--command-id")
    nt = add(task, "note", _cmd_task_note, "记录任务备注")
    nt.add_argument("task_id")
    nt.add_argument("--text", required=True)
    nt.add_argument("--expected-version", type=int)
    nt.add_argument("--command-id")
    add(task, "list", _cmd_task_list, "列出任务")
    show = add(task, "show", _cmd_task_show, "显示任务")
    show.add_argument("task_id")

    ledger = sub.add_parser("ledger", help="账本").add_subparsers(
        dest="ledger_command", required=True, metavar="<ledger command>"
    )
    add(ledger, "tail", _cmd_ledger_tail, "账本末端序号与摘要")
    add(ledger, "audit", _cmd_ledger_audit, "全量审计摘要链与投影")
    add(ledger, "rebuild", _cmd_ledger_rebuild, "丢弃投影并从账本重建")

    backup = add(sub, "backup", _cmd_backup, "在线备份到空目录")
    backup.add_argument("dest")
    rest = add(sub, "restore", _cmd_restore, "从备份恢复到空的数据目录")
    rest.add_argument("src")
    rest.add_argument("--allow-unverified", action="store_true")
    config = sub.add_parser("config", help="统一配置").add_subparsers(
        dest="config_command", required=True, metavar="<config command>"
    )
    show_config = add(config, "show", _cmd_config_show, "显示有效配置及每个值的来源")
    show_config.add_argument("--effective", action="store_true", help="（默认即为有效配置）")
    add(config, "validate", _cmd_config_validate, "校验配置")

    lease_cmd = sub.add_parser("lease", help="调度租约").add_subparsers(
        dest="lease_command", required=True, metavar="<lease command>"
    )
    add(lease_cmd, "show", _cmd_lease_show, "显示调度租约")
    for name, handler, text in (
        ("acquire", _cmd_lease_acquire, "领取或续期调度租约"),
        ("release", _cmd_lease_release, "释放调度租约"),
        ("takeover", _cmd_lease_takeover, "在用户确认后接管调度租约"),
    ):
        command = add(lease_cmd, name, handler, text)
        command.add_argument("--session", required=True, help="主会话 ID")
        command.add_argument("--machine", default=lease.this_machine())
        if name != "release":
            command.add_argument("--command-id")
        if name == "takeover":
            command.add_argument("--from-session", required=True)
            command.add_argument("--from-machine", action="store_true")
            command.add_argument(
                "--user-source",
                required=True,
                help='用户确认来源（JSON：{"channel","principal","message_ref","text"}）',
            )

    tool_cmd = sub.add_parser("tool", help="状态机工具（与 MCP 共用实现）").add_subparsers(
        dest="tool_command", required=True, metavar="<tool command>"
    )
    add(tool_cmd, "list", _cmd_tool_list, "列出工具")
    run = add(tool_cmd, "run", _cmd_tool_run, "运行工具")
    run.add_argument("name")
    run.add_argument("--input", required=True, help="JSON 对象，或 @文件路径")
    run.add_argument("--role", required=True, choices=["orchestrator", "worker", "system"])
    run.add_argument("--session", help="调用者的会话 ID")
    run.add_argument("--machine", default=lease.this_machine())

    add(sub, "mcp", _cmd_mcp, "以 stdio 运行 tsw MCP server（由插件启动）")
    guard = add(
        sub, "guard", _cmd_guard, "Claude Code hooks 入口（由插件调用，从 stdin 读取 payload）"
    )
    guard.add_argument("event", choices=["SessionStart", "PreToolUse", "UserPromptSubmit", "Stop"])
    host = sub.add_parser("host", help="宿主集成").add_subparsers(
        dest="host_command", required=True, metavar="<host command>"
    )
    add(host, "render", _cmd_host_render, "在数据目录生成主会话的 CLAUDE.md 与 orchestrate skill")
    add(host, "install-hint", _cmd_host_install_hint, "显示插件安装步骤")

    review = sub.add_parser("review", help="worker 自评审").add_subparsers(
        dest="review_command", required=True, metavar="<review command>"
    )
    review_run = add(review, "run", _cmd_review_run, "运行一轮自评审（与 MCP 的 review_run 相同）")
    review_run.add_argument("--head", help="评审的 HEAD；git 执行目录默认取 HEAD")
    review_run.add_argument("--base", help="评审范围的起点（base..HEAD）")
    review_run.add_argument("--notes", help="已执行的验证与结果摘要")
    review_run.add_argument("--notes-file", help="从文件读取说明")

    importing = sub.add_parser("import", help="导入其他任务系统的历史（DN005）").add_subparsers(
        dest="import_command", required=True, metavar="<import source>"
    )
    oa = add(
        importing,
        "orchestrator-agent",
        _cmd_import_orchestrator_agent,
        "导入 orchestrator-agent 的历史（先在来源运行 python -m orchestrator export-snapshot）",
    )
    oa.add_argument("--snapshot", required=True, help="export-snapshot 写出的快照目录")
    oa.add_argument("--dry-run", action="store_true", help="只核对与分类，不写入")
    oa.add_argument(
        "--renumber-local",
        action="append",
        metavar="OLD=NEW",
        help="先把与导入编号冲突的已结束本地任务改到新编号（可重复），例如 T001=T238",
    )
    oa.add_argument(
        "--exclude",
        action="append",
        metavar="ID",
        help="按用户决定排除只有任务书、旧账本未登记的任务（可重复）：不导入，也不列为待决定",
    )

    add(sub, "quiesce", _cmd_quiesce, "静止数据目录（之后可直接复制）")
    add(sub, "resume-writes", _cmd_resume_writes, "解除静止")
    return parser


def iter_commands(parser: argparse.ArgumentParser | None = None) -> list[str]:
    """All leaf commands, e.g. ``"task register"`` (used by the use-case inventory check)."""
    root = parser or build_parser()
    found: list[str] = []

    def walk(current: argparse.ArgumentParser, prefix: list[str]) -> None:
        for action in current._actions:  # pyright: ignore[reportPrivateUsage]
            if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                choices: dict[str, argparse.ArgumentParser] = action.choices  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                for name, child in choices.items():
                    walk(child, [*prefix, name])
                return
        found.append(" ".join(prefix))

    walk(root, [])
    return sorted(found)


def _print_human(result: dict[str, Any]) -> None:
    for key, value in result.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            print(f"{key}: {value}")


def command_path(args: argparse.Namespace) -> str:
    sub = getattr(args, f"{args.command}_command", None)
    return f"{args.command} {sub}" if sub else str(args.command)


def _check_host_session(args: argparse.Namespace) -> None:
    """Inside a TaskSteward session the caller is the session: no ``--role`` on the command line.

    A session bound to the data directory (orchestrator, worker, or a revoked worker) is
    identified by the MCP server and the hooks; a command line that names its own role could
    claim any of them, so such sessions only get the commands that resolve the caller
    themselves or only read (DN001 §7.1). Unrelated Claude sessions are not affected.
    """
    if not os.environ.get(SESSION_ENV) or command_path(args) in HOST_CLI_COMMANDS:
        return
    root = Path(args.home) if args.home else resolve_home(repo_root=REPO_ROOT)
    if not (root / "tsw-home.json").is_file():
        return
    home = open_home(root)
    with Ledger(home).view() as conn:
        who = resolve_identity(conn, home, os.environ, Path.cwd())
    if who.role != "none" or who.pending:
        raise TswError(
            ErrorCode.REJECTED,
            "Claude Code 会话中不能用命令行执行该操作，请使用 tsw MCP 工具",
            reason="host_session_cli",
            command=command_path(args),
        )


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler = args.handler
    try:
        _check_host_session(args)
        result = handler(args)
    except TswError as exc:
        if args.json:
            print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        else:
            detail = f" {json.dumps(exc.details, ensure_ascii=False)}" if exc.details else ""
            print(f"错误 [{exc.code.value}] {exc.message}{detail}", file=sys.stderr)
        return EXIT_CODES[exc.code]
    if result.pop("_silent", False):
        return 0
    ok = bool(result.pop("ok", True))
    if args.json:
        print(json.dumps({"ok": ok, **result}, ensure_ascii=False))
    else:
        _print_human(result)
    return 0 if ok else EXIT_CODES[ErrorCode.REJECTED]
