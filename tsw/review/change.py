"""Changing the reviewer, its model and effort from a terminal (DN004 §3.4).

``tsw reviewer set`` is the user's own command outside any session (DN006 §8.10): what is not
given on the command line is picked from the reviewer's model list (arrow keys in a console,
numbered lines otherwise), checked against that list, shown before and after, confirmed, and
written with the same validation and ``config.changed`` event as ``config_change``; the source
is the command itself on the ``user-cli`` channel (DN003 §7.2). Standard library only.
"""

from __future__ import annotations

import getpass
import os
import platform
import shlex
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from tsw.config import load_config
from tsw.errors import ErrorCode, TswError
from tsw.events import utc_now
from tsw.home import Home
from tsw.ledger import Ledger
from tsw.machine import config_tools
from tsw.review import models

# pick(title, values, labels, default) -> the value picked
Pick = Callable[[str, Sequence[str], Mapping[str, str], "str | None"], str]
Confirm = Callable[[str], None]


def _principal() -> str:
    return f"{getpass.getuser()}@{platform.node()}"


def plan(
    values: Mapping[str, Any],
    *,
    reviewer: str | None,
    model: str | None,
    effort: str | None,
    unlisted: bool,
    pick: Pick | None,
    lister: Callable[[str, str | None], models.ModelList] = models.list_models,
) -> dict[str, Any]:
    """The change ``tsw reviewer set`` would make: the reviewer, model and effort (picked when
    not given and ``pick`` is there), checked against the list; ``ops`` empty when nothing
    changes."""
    now = models.current(values)
    if reviewer is None:
        if pick is None:
            reviewer = str(now["reviewer"] or "")
        else:
            names = tuple(models.LISTERS)
            reviewer = pick(
                "评审器", names, {}, now["reviewer"] if now["reviewer"] in names else None
            )
    if reviewer not in models.LISTERS:
        raise TswError(
            ErrorCode.REJECTED,
            f"评审器须为 {' / '.join(models.LISTERS)} 之一",
            reason="invalid_input",
            reviewer=reviewer,
        )
    same = reviewer == now["reviewer"]
    found = lister(reviewer, models.resolve(reviewer, now["executable"] if same else None))
    if (model is None or effort is None) and pick is None:
        raise TswError(
            ErrorCode.REJECTED, "非交互运行时须给出 --model 与 --effort", reason="invalid_input"
        )
    if model is None:
        assert pick is not None
        if not found.available:
            raise TswError(
                ErrorCode.REJECTED,
                f"取不到 {reviewer} 的模型列表（{found.error}）：用 --model 给出模型 ID，"
                "并加 --unlisted",
                reason="model_list_unavailable",
            )
        default = models.preselect_model(found, now["model"] if same else None)
        labels = {m.id: _label(m) for m in found.models}
        model = pick("评审模型", [m.id for m in found.models], labels, default)
    if effort is None:
        assert pick is not None
        default = models.preselect_effort(found, model, now["effort"] if same else None)
        efforts = models.efforts_for(found, model)
        if efforts:
            effort = pick("推理强度", efforts, {}, default)
        elif default:
            effort = default
        else:
            raise TswError(
                ErrorCode.REJECTED,
                "取不到这个模型支持的推理强度：用 --effort 给出",
                reason="invalid_input",
            )
    model, effort, notes = models.choose(found, model, effort, unlisted=unlisted)
    return {
        "before": {k: now[k] for k in ("reviewer", "model", "effort")},
        "after": {"reviewer": reviewer, "model": model, "effort": effort},
        "ops": models.ops(values, reviewer, model, effort),
        "notes": notes,
        "listed": found.available,
    }


def _label(model: models.Model) -> str:
    name = model.name if model.name and model.name != model.id else ""
    also = "、".join(model.also)
    return f"{name}（{also}）" if name and also else name or also


def words(after: Mapping[str, Any], unlisted: bool) -> str:
    """The command line the user ran, as it is recorded (with what was picked)."""
    argv = ["tsw", "reviewer", "set"]
    for key in ("reviewer", "model", "effort"):
        argv += [f"--{key}", str(after[key])]
    if unlisted:
        argv.append("--unlisted")
    return shlex.join(argv)


def apply(
    home: Home,
    ledger: Ledger,
    planned: Mapping[str, Any],
    *,
    unlisted: bool,
    confirm: Confirm,
) -> dict[str, Any]:
    """Confirm and write the planned change (DN003 §7.1 under the ledger's commit lock)."""
    before, after = planned["before"], planned["after"]
    if not planned["ops"]:
        return {"changed": False, "before": before, "after": after, "notes": planned["notes"]}
    shown = "、".join(
        f"{key} {before[key]} → {after[key]}" for key in after if before[key] != after[key]
    )
    confirm(f"修改评审配置（roles.reviewer）：{shown}？")
    text = words(after, unlisted)
    command = config_tools.user_change(
        list(planned["ops"]),
        f"用户在终端更换评审器配置：{shown}",
        actor={"role": "user"},
        principal=_principal(),
        message_ref=f"cli:{utc_now()}:{os.getpid()}",
        words=text,
    )
    event = ledger.commit(command).events[-1]
    config = load_config(home)
    return {
        "changed": True,
        "change_id": event.data["change_id"],
        "before": before,
        "after": after,
        "notes": planned["notes"],
        "warnings": config.warnings,
    }
