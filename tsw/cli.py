"""The ``tsw`` command line.

Exit codes: 0 success; 2 usage error; 3 REJECTED; 4 QUIESCED; 5 UNAVAILABLE; 6 UNKNOWN;
7 UNSUPPORTED; 8 AUTH; 1 unexpected error. With ``--json`` results go to stdout as one JSON
object and errors go to stderr as one JSON object.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from tsw import __version__, lease
from tsw.config import EffectiveConfig, load_config
from tsw.errors import EXIT_CODES, ErrorCode, TswError
from tsw.events import utc_now
from tsw.home import Home, init_home, open_home, resolve_home
from tsw.ledger import Ledger
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
    return {"home": str(home.root), "created": created}


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


def _cmd_lease_acquire(args: argparse.Namespace) -> dict[str, Any]:
    ledger, config = _writer(args)
    command = lease.acquire(
        args.session,
        machine=args.machine,
        lease_minutes=int(config.get("scheduling.lease_minutes")),
        actor={"role": "orchestrator", "session_id": args.session},
        command_id=args.command_id,
    )
    event = ledger.commit(command).events[-1]
    return {"session_id": args.session, "expires_at": event.data["expires_at"]}


def _cmd_lease_release(args: argparse.Namespace) -> dict[str, Any]:
    ledger, _ = _writer(args)
    ledger.commit(
        lease.release(
            args.session,
            machine=args.machine,
            actor={"role": "orchestrator", "session_id": args.session},
        )
    )
    return {"released": True}


def _cmd_lease_takeover(args: argparse.Namespace) -> dict[str, Any]:
    ledger, config = _writer(args)
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
        lease_minutes=int(config.get("scheduling.lease_minutes")),
        from_machine=args.from_machine,
        actor={"role": "orchestrator", "session_id": args.session},
        command_id=args.command_id,
    )
    event = ledger.commit(command).events[-1]
    return {"session_id": args.session, "expires_at": event.data["expires_at"]}


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


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler = args.handler
    try:
        result = handler(args)
    except TswError as exc:
        if args.json:
            print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        else:
            detail = f" {json.dumps(exc.details, ensure_ascii=False)}" if exc.details else ""
            print(f"错误 [{exc.code.value}] {exc.message}{detail}", file=sys.stderr)
        return EXIT_CODES[exc.code]
    ok = bool(result.pop("ok", True))
    if args.json:
        print(json.dumps({"ok": ok, **result}, ensure_ascii=False))
    else:
        _print_human(result)
    return 0 if ok else EXIT_CODES[ErrorCode.REJECTED]
