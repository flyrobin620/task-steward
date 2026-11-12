# Configuration is untyped JSON walked generically; unknown-type reports carry no signal here.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
"""The unified configuration (DN003).

The effective configuration merges three layers in order: built-in defaults, the data directory's
``config/task-steward.json`` and the machine-local ``config/task-steward.local.json``. Objects merge
field by field, arrays are replaced, and ``null`` removes a field. Validation (DN003 §4) runs on
the merged result; any problem refuses every write command while read commands keep working.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsw.errors import rejected
from tsw.home import Home
from tsw.plugins.catalog import CHANNELS, HOST_INTEGRATIONS, SESSIONS, STORAGE_BACKENDS

CONFIG_FILE = "config/task-steward.json"
LOCAL_FILE = "config/task-steward.local.json"
VERSION = 1
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_ORDER = ("ask", "edits", "auto", "full")
SEVERITIES = ("critical", "high", "medium", "low", "info")
MODEL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
REPOSITORY_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Fields a machine-local override may set (DN003 §5); ``*`` matches one map key.
LOCAL_WHITELIST = (
    "repositories.*.path",
    "worktrees.root",
    "roles.reviewer.executable",
    "accounts.*.auth.config_dir",
    "import.sources.*.root",
)
# Fields that would make review optional; review is an invariant, not a setting (DN003 §3.1).
FORBIDDEN = ("review.required", "review.require_final_head_reviewed", "review.on_unavailable")
REMOVED_WITH_WARNING = ("roles.comparison",)
# Default fields that a configuration may delete with ``null`` (DN003 §5a); every other default
# field must be present in the effective configuration.
OPTIONAL = ("roles.reviewer.fallback_session", "input.decision_digest.channel")
# Map containers whose entries are user data, not required fields.
MAPS = ("repositories", "accounts", "notifications", "import.sources")

DEFAULTS: dict[str, Any] = {
    "version": VERSION,
    "control": {
        "language": "zh-CN",
        "ledger_dir": "ledger",
        "runs_dir": "runs",
        "todo_index": "todo/TODO.md",
        "status_page": "views/STATUS.html",
        "archive_page": "views/ARCHIVE.html",
        "data_dir_sync": {"enabled": False},
    },
    "plugins": {
        "host_integration": "claude-code",
        "sessions": ["claude-app", "claude-headless", "codex-app-server", "codex-exec"],
        "human_channels": ["in-session"],
        "storage": {"use": "fs"},
    },
    "input": {
        "triage": "orchestrator_judgment",
        # A worker asks the user in its own session first; with no reply for this long the
        # question is handed to the orchestrator (DN006 §8.3).
        "in_session_timeout_minutes": 10,
        "decision_digest": {
            "enabled": True,
            "schedule": "daily_and_on_task_done",
            "channel": "local",
        },
        # Where Teams question cards go: "self" is the user's own chat (DN006 §8.7).
        "teams": {"chat": "self"},
    },
    "roles": {
        "orchestrator": {
            "session": "claude-app",
            "model": "claude-opus-5-5",
            "effort": "medium",
            "permission_mode": "auto",
        },
        "worker": {
            "session": "claude-app",
            "model": "claude-opus-5-5",
            "effort": "high",
            "permission_mode": "auto",
            "context": "fresh",
            "allow_in_task_subagents": True,
        },
        "reviewer": {
            "session": "codex-app-server",
            "fallback_session": "codex-exec",
            "model": "gpt-6-astra",
            "effort": "medium",
            "timeout_seconds": 1800,
        },
        # The routine session runs the routine board (DN006 §8.9); it only runs scripts.
        "routine": {
            "session": "claude-app",
            "model": "claude-sonnet-5-5",
            "effort": "low",
            "permission_mode": "auto",
        },
    },
    "routine": {
        "enabled": True,
        "interval_minutes": 10,
        "renew_after_days": 6,
        "context_reset_hours": 24,
        "stale_after_intervals": 3,
        "teams_lookback_hours": 24,  # deprecated: no longer read (DN006 §8.9.5)
    },
    "review": {
        "max_rounds": 3,
        "orchestrator_extension_limit": 3,
        "retry_limit": 2,
        "block_on": ["critical", "high"],
        "incremental": "resume_thread",
    },
    "ledger": {"segment_max_mb": 16, "checkpoint_every": 64},
    "context": {
        "catalog_page_size": 8,
        "max_chars": 20000,
        "fts_tokenizer": "trigram",
        "short_query_fallback": "like_cards_only",
        "require_read_reason": True,
    },
    "scheduling": {
        "max_workers": 5,
        "lease_minutes": 120,
        "prestart": {"enabled": True, "max_standby": 10},
        "wake": {
            "worker_message_parent": True,
            "reportable_events": [
                "worker.submitted",
                "worker.question",
                "input.handed_off",
                "channel.reply_seen",
                "task.paused",
                "report_back",
                "task.requirement_changed",
            ],
        },
    },
    "sessions": {
        "name_pattern": "{task_id_lower}-{attempt}-g{generation}",
        "archive_on_accept": False,
        "archive_on_archive": True,
        # Prefixes of a worker session's title once its task is accepted / cancelled (DN002 §5.8);
        # an empty string adds none.
        "title_on_accept": "[已验收]",
        "title_on_cancel": "[已取消]",
    },
    "worktrees": {
        "root": "~/tsw-worktrees",
        "branch_pattern": "tsw/{task_id_lower}-{attempt}",
        "work_dir": "work/{task_id}/{attempt}",
    },
    "reports": {"formats": ["md", "html"], "html_from_md": True, "print_styles": True},
    "repositories": {},
    "accounts": {},
    "notifications": {},
    "policies": {
        "executable_hashing": "forbidden_without_user_approval",
        "email": "draft_only",
        "pr_default_state": "ready_for_review",
        "user_acceptance_source_required": True,
        "auto_archive_days": 7,
        "reserved_for_user": [
            "send_email",
            "merge_protected_branch",
            "production_deploy",
            "delete_user_data",
        ],
    },
    "import": {"sources": {}},
}

# ---------------------------------------------------------------------- schema

S: dict[str, Any] = {"type": "str"}
B: dict[str, Any] = {"type": "bool"}
STRS: dict[str, Any] = {"type": "list", "item": S}


def _int(minimum: int = 0) -> dict[str, Any]:
    return {"type": "int", "min": minimum}


def _obj(**fields: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "fields": fields}


def _choice(*values: str) -> dict[str, Any]:
    return {"type": "str", "choices": values}


ROLE_COMMON = {"session": S, "model": S, "effort": _choice(*EFFORTS)}
SCHEMA: dict[str, Any] = _obj(
    version=_int(1),
    control=_obj(
        language=S,
        ledger_dir=S,
        runs_dir=S,
        todo_index=S,
        status_page=S,
        archive_page=S,
        data_dir_sync=_obj(enabled=B),
    ),
    plugins=_obj(
        host_integration=S,
        sessions=STRS,
        human_channels=STRS,
        storage=_obj(use=S),
    ),
    input=_obj(
        triage=_choice("orchestrator_judgment", "human_direct"),
        channels=STRS,
        in_session_timeout_minutes=_int(1),
        decision_digest=_obj(enabled=B, schedule=S, channel=S),
        # No effect since the routine board collects Teams replies (DN003 §3.1a); still accepted.
        teams_poll=_obj(interval_minutes=_int(1), max_interval_minutes=_int(1)),
        teams=_obj(chat=S),
    ),
    roles=_obj(
        orchestrator=_obj(**ROLE_COMMON, permission_mode=_choice(*PERMISSION_ORDER)),
        worker=_obj(
            **ROLE_COMMON,
            permission_mode=_choice(*PERMISSION_ORDER),
            context=_choice("fresh"),
            prompt_template=S,
            allow_in_task_subagents=B,
        ),
        reviewer=_obj(
            **ROLE_COMMON,
            fallback_session=S,
            context_window=_int(1),
            timeout_seconds=_int(1),
            executable=S,
        ),
        routine=_obj(**ROLE_COMMON, permission_mode=_choice(*PERMISSION_ORDER)),
    ),
    routine=_obj(
        enabled=B,
        interval_minutes=_int(1),
        renew_after_days=_int(1),
        context_reset_hours=_int(1),
        stale_after_intervals=_int(1),
        teams_lookback_hours=_int(1),  # deprecated (T243 a2): no longer read (DN003 §3.1b)
    ),
    review=_obj(
        max_rounds=_int(1),
        orchestrator_extension_limit=_int(0),
        retry_limit=_int(0),
        block_on={"type": "list", "item": _choice(*SEVERITIES)},
        incremental=_choice("resume_thread", "fresh"),
        prompt_templates=_obj(code=S, design=S, research=S),
        output_schema=S,
    ),
    ledger=_obj(segment_max_mb=_int(1), checkpoint_every=_int(1)),
    context=_obj(
        catalog_page_size=_int(1),
        max_chars=_int(1),
        fts_tokenizer=S,
        short_query_fallback=S,
        require_read_reason=B,
    ),
    scheduling=_obj(
        max_workers=_int(1),
        lease_minutes=_int(1),
        prestart=_obj(enabled=B, max_standby=_int(0)),
        wake=_obj(worker_message_parent=B, reportable_events=STRS),
    ),
    sessions=_obj(
        name_pattern=S,
        archive_on_accept=B,
        archive_on_archive=B,
        title_on_accept=S,
        title_on_cancel=S,
    ),
    worktrees=_obj(root=S, branch_pattern=S, work_dir=S),
    reports=_obj(formats=STRS, html_from_md=B, print_styles=B),
    repositories={
        "type": "map",
        "item": _obj(
            aliases=STRS,
            path=S,
            provider=S,
            host=S,
            repository=S,
            remote=S,
            base_ref=S,
            integration_branch=S,
            auth_profile=S,
            commit_identity=_obj(name=S, email=S),
            instructions=STRS,
            worktree_files={"type": "list", "item": _obj(source=S, destination=S)},
            validation={"type": "list", "item": {"type": "any"}},
            delivery=_obj(push=B, create_pr=B, merge=B),
        ),
    },
    accounts={
        "type": "map",
        "item": _obj(provider=S, host=S, account=S, auth=_obj(kind=S, config_dir=S)),
    },
    notifications={"type": "map", "item": {"type": "any"}},
    policies=_obj(
        executable_hashing=S,
        email=S,
        pr_default_state=S,
        user_acceptance_source_required=B,
        auto_archive_days=_int(1),
        reserved_for_user=STRS,
    ),
    **{"import": _obj(sources={"type": "map", "item": _obj(root=S, mode=S, resumable=STRS)})},
)


def _check(value: Any, spec: dict[str, Any], path: str, problems: list[str]) -> None:
    kind = spec["type"]
    if kind == "any":
        return
    if kind == "object":
        if not isinstance(value, dict):
            problems.append(f"{path}: 应为对象")
            return
        fields: dict[str, Any] = spec["fields"]
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if key not in fields:
                problems.append(f"{child}: 未知字段")
                continue
            _check(item, fields[key], child, problems)
        return
    if kind == "map":
        if not isinstance(value, dict):
            problems.append(f"{path}: 应为对象")
            return
        for key, item in value.items():
            _check(item, spec["item"], f"{path}.{key}", problems)
        return
    if kind == "list":
        if not isinstance(value, list):
            problems.append(f"{path}: 应为数组")
            return
        for index, item in enumerate(value):
            _check(item, spec["item"], f"{path}[{index}]", problems)
        return
    if kind == "str":
        if not isinstance(value, str):
            problems.append(f"{path}: 应为字符串")
        elif "choices" in spec and value not in spec["choices"]:
            problems.append(f"{path}: 取值应为 {list(spec['choices'])} 之一")
        return
    if kind == "bool":
        if not isinstance(value, bool):
            problems.append(f"{path}: 应为布尔值")
        return
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{path}: 应为整数")
        elif value < spec["min"]:
            problems.append(f"{path}: 不能小于 {spec['min']}")


# ---------------------------------------------------------------------- merge


def merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Objects merge by field, arrays and scalars are replaced, ``None`` deletes the field."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict):
            current = out.get(key)
            out[key] = merge(current if isinstance(current, dict) else {}, value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _leaves(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(_leaves(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value}


def _matches(path: str, pattern: str) -> bool:
    parts, wanted = path.split("."), pattern.split(".")
    return len(parts) == len(wanted) and all(
        w in ("*", p) for p, w in zip(parts, wanted, strict=True)
    )


def get(values: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = values
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def normalize_model_id(model: str) -> str:
    """Drop a context suffix such as ``[1m]`` that hosts append to receipts (DN003 §1)."""
    return re.sub(r"\[[^\]]*\]$", "", model.strip())


# ---------------------------------------------------------------------- load


@dataclass
class EffectiveConfig:
    values: dict[str, Any]
    sources: dict[str, str]
    problems: list[str] = field(default_factory=lambda: [])
    warnings: list[str] = field(default_factory=lambda: [])

    @property
    def valid(self) -> bool:
        return not self.problems

    def get(self, dotted: str, default: Any = None) -> Any:
        return get(self.values, dotted, default)

    def require_valid(self) -> EffectiveConfig:
        if self.problems:
            raise rejected(
                "配置无效，拒绝写命令（运行 tsw config validate 查看）",
                reason="config_invalid",
                problems=self.problems,
            )
        return self


def _read_layer(path: Path, problems: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append(f"{path.name}: 不是合法的 JSON（{exc.msg}，第 {exc.lineno} 行）")
        return {}
    if not isinstance(raw, dict):
        problems.append(f"{path.name}: 顶层应为对象")
        return {}
    return raw


def load_config(home: Home, *, main_layer: dict[str, Any] | None = None) -> EffectiveConfig:
    """The effective configuration; ``main_layer`` replaces the data directory's file (used to
    validate a proposed change before it is written)."""
    problems: list[str] = []
    warnings: list[str] = []
    main = (
        copy.deepcopy(main_layer)
        if main_layer is not None
        else _read_layer(home.path(CONFIG_FILE), problems)
    )
    local = _read_layer(home.path(LOCAL_FILE), problems)

    for dotted in REMOVED_WITH_WARNING:
        parent, _, leaf = dotted.rpartition(".")
        container = get(main, parent)
        if isinstance(container, dict) and leaf in container:
            container.pop(leaf)
            warnings.append(f"{dotted}: 已删除的字段，已忽略")

    for dotted, _value in _leaves(local).items():
        if dotted and not any(_matches(dotted, pattern) for pattern in LOCAL_WHITELIST):
            problems.append(f"{LOCAL_FILE}: 不允许覆盖 {dotted}（只允许 {list(LOCAL_WHITELIST)}）")

    values = merge(merge(DEFAULTS, main), local)
    sources = dict.fromkeys(_leaves(values), "default")
    for layer, name in ((main, "config"), (local, "local")):
        for dotted, value in _leaves(layer).items():
            if value is not None and dotted in sources:
                sources[dotted] = name
    effective = EffectiveConfig(
        values=values, sources=sources, problems=problems, warnings=warnings
    )
    _validate(effective, home)
    return effective


# ---------------------------------------------------------------------- validation (DN003 §4)


def _validate(config: EffectiveConfig, home: Home) -> None:
    values, problems = config.values, config.problems
    for dotted in FORBIDDEN:
        if get(values, dotted) is not None:
            problems.append(f"{dotted}: 评审是必经环节，不能配置")
            parent, _, leaf = dotted.rpartition(".")
            container = get(values, parent)
            if isinstance(container, dict):
                container.pop(leaf, None)
    if values.get("version") != VERSION:
        problems.append(f"version: 必须为 {VERSION}")
    structural: list[str] = []
    _check(values, SCHEMA, "", structural)
    for dotted in _leaves(DEFAULTS):
        if dotted in OPTIONAL or any(dotted.startswith(m + ".") for m in MAPS):
            continue
        if get(values, dotted) is None:
            structural.append(f"{dotted}: 缺少必填字段")
    problems.extend(structural)
    if structural:
        # Semantic rules assume the structure is right; report the structure first.
        return

    # Rule 2: model ids and efforts.
    for role in ("orchestrator", "worker", "reviewer", "routine"):
        model = get(values, f"roles.{role}.model")
        if isinstance(model, str) and not MODEL_ID.match(model):
            problems.append(f"roles.{role}.model: 模型 ID 格式不正确")

    # Rule 3: a worker never runs with more permission than the orchestrator.
    orch_mode = get(values, "roles.orchestrator.permission_mode")
    worker_mode = get(values, "roles.worker.permission_mode")
    if (
        orch_mode in PERMISSION_ORDER
        and worker_mode in PERMISSION_ORDER
        and PERMISSION_ORDER.index(worker_mode) > PERMISSION_ORDER.index(orch_mode)
    ):
        problems.append("roles.worker.permission_mode: 不能高于 roles.orchestrator.permission_mode")
    routine_mode = get(values, "roles.routine.permission_mode")
    if (
        orch_mode in PERMISSION_ORDER
        and routine_mode in PERMISSION_ORDER
        and PERMISSION_ORDER.index(routine_mode) > PERMISSION_ORDER.index(orch_mode)
    ):
        if config.sources.get("roles.routine.permission_mode") == "default":
            # A configuration written before the routine session existed must stay valid: the
            # default follows the orchestrator's mode down (DN003 §3.1).
            values["roles"]["routine"]["permission_mode"] = orch_mode
            config.sources["roles.routine.permission_mode"] = "derived"
        else:
            problems.append(
                "roles.routine.permission_mode: 不能高于 roles.orchestrator.permission_mode"
            )
    interval = get(values, "routine.interval_minutes")
    if (
        isinstance(interval, int)
        and not isinstance(interval, bool)
        and interval > 0
        and not (60 % interval == 0 or (interval % 60 == 0 and 1440 % interval == 0))
    ):
        problems.append("routine.interval_minutes: 须能整除 60，或为 60 的整数倍且能整除 1440")

    _validate_plugins(values, problems, config.warnings)
    _validate_input(values, problems, config.sources)
    _validate_paths(values, problems, home)
    _validate_repositories(values, problems, home)


def _validate_plugins(values: dict[str, Any], problems: list[str], warnings: list[str]) -> None:
    host = get(values, "plugins.host_integration")
    if isinstance(host, str) and host not in HOST_INTEGRATIONS:
        problems.append(f"plugins.host_integration: 本版本不提供 {host}")
    storage = get(values, "plugins.storage.use")
    if isinstance(storage, str) and storage not in STORAGE_BACKENDS:
        problems.append(f"plugins.storage.use: 本版本不提供 {storage}")
    loaded = [s for s in get(values, "plugins.sessions", []) if isinstance(s, str)]
    for name in loaded:
        if name not in SESSIONS:
            problems.append(f"plugins.sessions: 本版本不提供会话适配器 {name}")
    for name in get(values, "plugins.human_channels", []) or []:
        if isinstance(name, str) and name not in CHANNELS:
            problems.append(f"plugins.human_channels: 本版本不提供人类渠道 {name}")

    def session(role_path: str) -> str | None:
        name = get(values, role_path)
        if name is None:
            return None
        if not isinstance(name, str) or name not in loaded or name not in SESSIONS:
            problems.append(f"{role_path}: 必须是 plugins.sessions 中已加载的会话适配器")
            return None
        return name

    orchestrator = session("roles.orchestrator.session")
    worker = session("roles.worker.session")
    if worker is not None:
        caps = SESSIONS[worker]
        if not caps.interactive:
            problems.append(f"roles.worker.session: {worker} 不能承载 worker（缺少 interactive）")
        if (
            caps.execution == "delegated"
            and orchestrator is not None
            and SESSIONS[orchestrator].host != caps.host
        ):
            problems.append(f"roles.worker.session: delegated 的 {worker} 必须与主会话在同一宿主")
    routine = session("roles.routine.session") if get(values, "routine.enabled") else None
    if routine is not None:
        caps = SESSIONS[routine]
        if not caps.interactive or not caps.timer:
            problems.append(
                f"roles.routine.session: {routine} 不能承载例行会话（须 interactive 且有 timer）"
            )
        renew = get(values, "routine.renew_after_days")
        interval = get(values, "routine.interval_minutes")
        if (
            caps.timer_days is not None
            and isinstance(renew, int)
            and isinstance(interval, int)
            and renew * 1440 + interval >= caps.timer_days * 1440
        ):
            # The renewal happens at the first run after renew_after_days: it must come before
            # the host drops the timer.
            problems.append(
                f"routine.renew_after_days: 须在 {routine} 的定时任务到期（{caps.timer_days} 天）"
                "前续期（续期天数加一个运行间隔要小于该期限）"
            )
        if (
            caps.execution == "delegated"
            and orchestrator is not None
            and SESSIONS[orchestrator].host != caps.host
        ):
            problems.append(f"roles.routine.session: delegated 的 {routine} 必须与主会话在同一宿主")
    for role_path in ("roles.reviewer.session", "roles.reviewer.fallback_session"):
        reviewer = session(role_path)
        if reviewer is None:
            continue
        caps = SESSIONS[reviewer]
        missing = [
            name
            for name, ok in (
                ("oneshot", caps.oneshot),
                ("direct", caps.execution == "direct"),
                ("read_only_enforced", caps.read_only_enforced),
            )
            if not ok
        ]
        if missing:
            problems.append(f"{role_path}: {reviewer} 不能做评审器（缺少 {', '.join(missing)}）")
    reviewer_model = get(values, "roles.reviewer.model")
    worker_model = get(values, "roles.worker.model")
    if (
        isinstance(reviewer_model, str)
        and isinstance(worker_model, str)
        and normalize_model_id(reviewer_model) == normalize_model_id(worker_model)
    ):
        warnings.append("roles.reviewer.model 与 roles.worker.model 相同，评审视角的独立性下降")


def _validate_input(values: dict[str, Any], problems: list[str], sources: dict[str, str]) -> None:
    loaded = [c for c in get(values, "plugins.human_channels", []) or [] if c in CHANNELS]
    answering = [c for c in loaded if CHANNELS[c].request_response]
    channels = get(values, "input.channels")
    if channels is None:
        values.setdefault("input", {})["channels"] = answering
        sources["input.channels"] = "derived"  # from plugins.human_channels
    elif isinstance(channels, list):
        for name in channels:
            if name not in loaded:
                problems.append(f"input.channels: {name} 未在 plugins.human_channels 中加载")
            elif not CHANNELS[name].request_response:
                problems.append(f"input.channels: {name} 不能接收答复（不是 request_response）")
    if get(values, "input.decision_digest.enabled") is True:
        channel = get(values, "input.decision_digest.channel")
        if channel is None:
            problems.append("input.decision_digest.channel: 启用决定摘要时必须设置")
        elif channel != "local" and (channel not in loaded or not CHANNELS[channel].notify):
            problems.append(
                f"input.decision_digest.channel: {channel} 不是已加载且支持 notify 的渠道"
            )


def _validate_paths(values: dict[str, Any], problems: list[str], home: Home) -> None:
    templates = [("roles.worker.prompt_template", get(values, "roles.worker.prompt_template"))]
    templates += [
        (f"review.prompt_templates.{kind}", get(values, f"review.prompt_templates.{kind}"))
        for kind in ("code", "design", "research")
    ]
    templates.append(("review.output_schema", get(values, "review.output_schema")))
    for dotted, value in templates:
        if isinstance(value, str) and not resolve_path(home, value).is_file():
            problems.append(f"{dotted}: 文件不存在 {value}")


def lease_minutes(config: EffectiveConfig) -> int:
    """The lease length; the default while the configuration is invalid, so that an orchestrator
    can still hold the lease it needs to repair the configuration (DN003 §7.1)."""
    source = config.values if config.valid else DEFAULTS
    return int(get(source, "scheduling.lease_minutes"))


def resolve_path(home: Home, value: str) -> Path:
    """Absolute paths stay; ``~`` expands; other paths are relative to the data directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else home.root / path


def _validate_repositories(values: dict[str, Any], problems: list[str], home: Home) -> None:
    repositories: dict[str, Any] = get(values, "repositories", {}) or {}
    accounts: dict[str, Any] = get(values, "accounts", {}) or {}
    for name, account in accounts.items():
        config_dir = get(account, "auth.config_dir") if isinstance(account, dict) else None
        if isinstance(config_dir, str):
            resolved = resolve_path(home, config_dir).resolve()
            if resolved.is_relative_to(home.root.resolve()):
                problems.append(f"accounts.{name}.auth.config_dir: 凭据目录必须位于数据目录之外")
    seen: dict[str, str] = {name: name for name in repositories}
    for name, repo in repositories.items():
        if not isinstance(repo, dict):
            continue
        delivery = repo.get("delivery")
        if isinstance(delivery, dict) and delivery.get("merge") is True:
            slug = repo.get("repository")
            if not (isinstance(slug, str) and REPOSITORY_SLUG.match(slug)):
                # Merge commands are checked against it: a worker may merge only into the
                # repository its task belongs to (DN002 §3.5).
                problems.append(
                    f"repositories.{name}.repository: delivery.merge 需要写明仓库（owner/name）"
                )
        profile = repo.get("auth_profile")
        if profile is not None:
            account = accounts.get(profile)
            if not isinstance(account, dict):
                problems.append(f"repositories.{name}.auth_profile: accounts 中没有 {profile}")
            else:
                for key in ("provider", "host"):
                    if (
                        repo.get(key) is not None
                        and account.get(key) is not None
                        and repo.get(key) != account.get(key)
                    ):
                        problems.append(
                            f"repositories.{name}.{key}: 与账号 {profile} 的 {key} 不一致"
                        )
        for alias in repo.get("aliases", []) or []:
            owner = seen.get(alias)
            if owner is not None and owner != name:
                problems.append(f"repositories.{name}.aliases: {alias} 已被 {owner} 使用")
            seen[str(alias)] = name


def resolve_repository(config: EffectiveConfig, name_or_alias: str) -> str | None:
    repositories: dict[str, Any] = config.get("repositories", {}) or {}
    if name_or_alias in repositories:
        return name_or_alias
    for name, repo in repositories.items():
        if isinstance(repo, dict) and name_or_alias in (repo.get("aliases") or []):
            return name
    return None
