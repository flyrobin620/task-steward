"""The one question list of ``tsw setup`` (DN009 §6).

The terminal entry asks these one by one (``install --interactive``); the session entry reads
them from ``tsw setup check --questions`` and asks the same ones in the conversation. Which
questions there are, which are required, their defaults and where each answer goes are defined
here only. The data directory has no default (the user's decision of 2026-11-19): an empty answer
is asked again, an answers file without it is refused. Repositories and accounts are never asked:
the user adds them in the main session after the install (DN009 §6.1).
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from tsw.config import DEFAULTS, VERSION
from tsw.plugins.catalog import CHANNELS, HOST_INTEGRATIONS

# The reviewer when no independent one (Codex) is found: the host's own read-only form, with a
# model other than the worker's (DN006 §10.2).
CLAUDE_REVIEWER: dict[str, Any] = {
    "session": "claude-headless",
    "fallback_session": None,
    "model": "claude-sonnet-5-5",
    "effort": "high",
}
REVIEWERS = ("codex-app-server", "claude-headless")


@dataclass(frozen=True)
class Question:
    id: str
    prompt: str
    kind: str  # path | text | choice | bool | int
    writes: str
    default: Any = None
    required: bool = False
    choices: tuple[str, ...] = ()
    note: str = ""
    asked_by: str = "setup"  # setup | entry (asked before tsw setup runs)
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "prompt": self.prompt,
            "kind": self.kind,
            "required": self.required,
            "default": self.default,
            "writes": self.writes,
            "asked_by": self.asked_by,
        }
        if self.choices:
            out["choices"] = list(self.choices)
        if self.note:
            out["note"] = self.note
        return out


def implemented_reviewers() -> tuple[str, ...]:
    """The reviewers ``review_run`` implements in this version: only these are offered (the user's
    decision of 2026-11-20, T308; the others are follow-up F-T308-1)."""
    from tsw.review.run import REVIEWERS as IMPLEMENTED  # noqa: PLC0415  (command line only)

    return tuple(name for name in REVIEWERS if name in IMPLEMENTED)


def detected_reviewer(env: Mapping[str, str]) -> str:
    """The first implemented reviewer (Codex); ``check`` warns when its command line is missing."""
    del env
    return implemented_reviewers()[0]


def reviewer_warning(env: Mapping[str, str]) -> str | None:
    """Why tasks could not finish their self-review on this machine, or None."""
    if shutil.which("codex", path=env.get("PATH")):
        return None
    return (
        "codex：找不到 Codex 命令行。本版本的 worker 自评"
        "审只实现了 Codex 评审器（codex-app-server）："
        "请安装并登录 Codex（例如 npm install -g @openai/codex，再 codex login），否则任务完成不了"
        "自评审。安装可以照常完成，之后装好 Codex 即可"
    )


def workdeck_found(env: Mapping[str, str]) -> bool:
    """Whether this machine has the workdeck command line (DN009 §5 step 7, T309)."""
    return shutil.which("workdeck", path=env.get("PATH")) is not None


def questions(env: Mapping[str, str], checkout: Path) -> list[Question]:
    windows = sys.platform == "win32"
    return [
        Question(
            "home",
            "数据目录位置（账本、任务书与报告都放在这里；给出新目录，或已有的数据目录以复用）",
            "path",
            "tsw init --home、Claude Code 用户设置的 env.TSW_HOME",
            required=True,
            note="必填，没有默认值；不要放在产品检出或其他 git 仓库中",
        ),
        Question(
            "checkout",
            "产品检出位置（已有检出时复用）",
            "path",
            "install.json、数据目录的 product_repo",
            default=str(checkout),
            asked_by="entry",
            note="由入口在运行 tsw setup 之前决定：终端入口"
            "为 setup.ps1 所在的检出，会话入口由取检出"
            "脚本克隆到固定位置；tsw setup 使用运行它的检出",
        ),
        Question(
            "language",
            "与你交流使用的语言（BCP 47 标签）",
            "text",
            "control.language",
            default=str(DEFAULTS["control"]["language"]),
        ),
        Question(
            "reviewer",
            "worker 自评审使用的评审器",
            "choice",
            "roles.reviewer",
            default=detected_reviewer(env),
            choices=implemented_reviewers(),
            note="本版本只实现了 Codex 评审器 codex-app-server（需要已登录的 codex 命令行）；"
            "Claude 与 Copilot CLI 评审器以后提供",
        ),
        Question(
            "human_channels",
            "需要你决定的事怎样找到你：只在会话中，或另加 Teams 自聊",
            "choice",
            "plugins.human_channels（input.channels 随之推导）",
            default="in-session",
            choices=("in-session", "teams-chat"),
            note="teams-chat 表示会话中之外另加 Teams 自聊（需要 WorkIQ）",
        ),
        Question(
            "host",
            "宿主",
            "choice",
            "plugins.host_integration、plugins.sessions",
            default=HOST_INTEGRATIONS[0],
            choices=HOST_INTEGRATIONS,
            note="只有一个已实现的宿主时直接选定，不必问",
        ),
        Question(
            "dashboard",
            "是否把 Dashboard 装成登录时启动的常驻服务",
            "bool",
            "tsw dashboard install",
            default=windows,
            note="" if windows else "本操作系统还不支持常驻服务",
        ),
        Question(
            "dashboard_port",
            "Dashboard 常驻服务的端口",
            "int",
            "dashboard.port",
            default=int(DEFAULTS["dashboard"]["port"]),
        ),
        Question(
            "workdeck",
            "是否把 Dashboard 接入本机的 workdeck 门户（workdeck plugin add）",
            "bool",
            "workdeck plugin add、install.json",
            default=workdeck_found(env),
            note="接入后门户为 TaskSteward 的会话显示任务标签，Dashboard 中的会话链接在门户中打开；"
            "workdeck 中已有 id 为 tsw 的插件时替换它（workdeck 先备份配置）"
            if workdeck_found(env)
            else "找不到 workdeck 命令，不接入",
        ),
        Question(
            "user_settings",
            "是否允许安装脚本维护 Claude Code 用户设置（先备份，"
            "只改 env.TSW_HOME、env.TSW_PYTHON 与"
            "三条 auto 模式规则）",
            "bool",
            "install.json",
            default=True,
            note="不允许时安装结束会输出要你手工合并的文本",
        ),
    ]


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("y", "yes", "true", "1", "是", "好"):
            return True
        if lowered in ("n", "no", "false", "0", "否", "不"):
            return False
    return None


def normalize(answers: Mapping[str, Any], qs: list[Question]) -> tuple[dict[str, Any], list[str]]:
    """Answers with defaults filled in, and the problems (missing required, bad values)."""
    known = {q.id: q for q in qs}
    problems = [f"未知的答案：{key}" for key in answers if key not in known]
    out: dict[str, Any] = {}
    for q in qs:
        value = answers.get(q.id)
        if value is None or (isinstance(value, str) and not value.strip()):
            if q.required:
                problems.append(f"{q.id}：必填（{q.prompt}）")
                continue
            value = q.default
        if q.kind == "path":
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                problems.append(f"{q.id}：须为绝对路径（{value}）")
                continue
            out[q.id] = str(path)
        elif q.kind == "bool":
            parsed = _bool(value)
            if parsed is None:
                problems.append(f"{q.id}：须为是或否（{value}）")
                continue
            out[q.id] = parsed
        elif q.kind == "int":
            try:
                number = int(value)
            except (TypeError, ValueError):
                problems.append(f"{q.id}：须为整数（{value}）")
                continue
            if not 1 <= number <= 65535:
                problems.append(f"{q.id}：端口须在 1–65535 之间（{value}）")
                continue
            out[q.id] = number
        elif q.kind == "choice":
            text = str(value).strip()
            if text not in q.choices:
                problems.append(f"{q.id}：须为 {' / '.join(q.choices)} 之一（{value}）")
                continue
            out[q.id] = text
        else:
            out[q.id] = str(value).strip()
    return out, problems


def config_layer(answers: Mapping[str, Any]) -> dict[str, Any]:
    """The initial configuration of a new data directory (DN009 §5 step 3): language, reviewer,
    human channels, host and the Dashboard port; never repositories or accounts (§6.1)."""
    channels = ["in-session"]
    if answers.get("human_channels") == "teams-chat":
        channels.append("teams-chat")
    channels = [c for c in channels if c in CHANNELS]
    layer: dict[str, Any] = {
        "version": VERSION,
        "control": {"language": answers["language"]},
        "plugins": {
            "host_integration": answers["host"],
            "human_channels": channels,
        },
        "dashboard": {"port": answers["dashboard_port"]},
    }
    if answers.get("reviewer") == "claude-headless":
        cast("dict[str, Any]", layer["plugins"])["sessions"] = ["claude-app", "claude-headless"]
        layer["roles"] = {"reviewer": dict(CLAUDE_REVIEWER)}
    return layer


def config_differences(values: Mapping[str, Any], answers: Mapping[str, Any]) -> list[str]:
    """How an existing data directory's effective configuration differs from the answers; the
    install never changes it (DN009 §5 step 3), it only reports."""
    from tsw.config import get  # noqa: PLC0415

    wanted = config_layer(answers)
    found: list[str] = []
    for dotted in (
        "control.language",
        "plugins.host_integration",
        "plugins.human_channels",
        "dashboard.port",
    ):
        mine, theirs = get(wanted, dotted), get(dict(values), dotted)
        if mine != theirs:
            found.append(f"{dotted}：配置为 {theirs!r}，答案为 {mine!r}")
    reviewer = get(dict(values), "roles.reviewer.session")
    if reviewer != answers.get("reviewer"):
        found.append(
            f"roles.reviewer.session：配置为 {reviewer!r}，答案为 {answers.get('reviewer')!r}"
        )
    return found


def ask(
    qs: list[Question],
    previous: Mapping[str, Any],
    read: Callable[[str], str],
    say: Callable[[str], None],
) -> dict[str, Any]:
    """Ask in the terminal; an earlier answer is offered as the default. The entry's own
    questions (``asked_by: entry``) are shown, not asked."""
    answers: dict[str, Any] = {}
    for q in qs:
        if q.asked_by != "setup":
            say(f"{q.prompt}：{q.default}（{q.note}）")
            continue
        if q.id == "host" and len(q.choices) == 1:
            answers[q.id] = q.choices[0]
            continue
        if q.id == "workdeck" and q.default is False:
            answers[q.id] = False  # no workdeck command here: nothing to ask (T309)
            continue
        default = previous.get(q.id, q.default)
        while True:
            hint = ""
            if q.choices:
                hint = f" [{' / '.join(q.choices)}]"
            elif q.kind == "bool":
                hint = " [是 / 否]"
            shown = default
            if isinstance(default, bool):
                shown = "是" if default else "否"
            suffix = f"（默认 {shown}）" if default not in (None, "") else ""
            if q.note:
                say(f"  {q.note}")
            reply = read(f"{q.prompt}{hint}{suffix}：").strip()
            value: Any = reply or default
            if value in (None, ""):
                say("  这一项必须由你给出。")
                continue
            checked, problems = normalize({q.id: value}, [q])
            if problems:
                say("  " + "；".join(problems))
                continue
            answers[q.id] = checked[q.id]
            break
    return answers
