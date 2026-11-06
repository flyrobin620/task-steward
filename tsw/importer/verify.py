"""The DN005 §6 checks that can be computed after an import, for the import report.

Items 1–4 and 6 are computed from the snapshot, the source files, the data directory and the
projection; item 4 reads the recorded execution directories with read-only git. Items 5 (the old
and new STATUS pages side by side) and 7 (a resume drill) need a person and are listed as such.
"""

# Legacy records are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

import filecmp
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsw.home import Home
from tsw.ledger import Ledger


@dataclass
class Check:
    number: int
    title: str
    ok: bool | None  # None: needs a person
    details: list[str] = field(default_factory=lambda: [])

    def mark(self) -> str:
        return {True: "通过", False: "未通过", None: "需人工"}[self.ok]


def _counts(
    ledger: Ledger, snapshot: Mapping[str, Any], classes: Mapping[str, str], conflicts: list[str]
) -> Check:
    expected = set(snapshot.get("tasks") or {})
    with ledger.view() as conn:
        imported = {
            str(r["task_id"]): (str(r["status"]), int(r["version"]))
            for r in conn.execute(
                "SELECT t.task_id, t.status, t.version FROM imported_tasks i "
                "JOIN tasks t ON t.task_id = i.task_id WHERE i.source = 'orchestrator-agent'"
            )
        }
    missing = sorted(expected - set(imported))
    # A task untouched since its import still has the class the import gave it.
    misclassed = sorted(
        f"{tid}：{status}，应为 {classes[tid]}"
        for tid, (status, version) in imported.items()
        if version == 1 and tid in classes and status != classes[tid]
    )
    counts: dict[str, int] = {}
    for tid in expected & set(imported):
        counts[classes.get(tid, "?")] = counts.get(classes.get(tid, "?"), 0) + 1
    check = Check(1, "任务总数与各分类数与旧审计一致", not missing and not misclassed)
    check.details.append(
        f"旧账本 {len(expected)} 个任务；已导入 {len(set(imported) & expected)}；分类 "
        + "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
    )
    pending = [t for t in missing if t in conflicts]
    if pending:
        check.details.append("编号冲突、尚未导入：" + "、".join(pending))
    if set(missing) - set(pending):
        check.details.append(
            "既未导入也不是冲突：" + "、".join(sorted(set(missing) - set(pending)))
        )
    check.details += [f"分类不一致：{m}" for m in misclassed]
    return check


def _volumes(home: Home, source: Path, problems: list[str]) -> Check:
    directory = home.root / "ledger" / "imported" / "orchestrator-agent"
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return Check(2, "事件卷按原字节复制，来源未改动", False, ["没有 manifest.json"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    different: list[str] = []
    for volume in manifest.get("volumes") or []:
        copy = directory / str(volume["file"])
        original = source / str(volume["path"])
        if not (copy.is_file() and original.is_file()) or not filecmp.cmp(
            copy, original, shallow=False
        ):
            different.append(str(volume["path"]))
    check = Check(2, "事件卷按原字节复制，来源未改动", not different and not problems)
    check.details.append(f"逐字节比对 {len(manifest.get('volumes') or [])} 个事件卷")
    check.details += [f"与来源不一致：{d}" for d in different] + problems
    return check


def _refs(missing: list[str], source: Path) -> Check:
    absent_in_source: list[str] = []
    lost: list[str] = []
    for ref in missing:
        rest = ref.split("/", 3)[3] if ref.startswith("legacy://") else ref
        (absent_in_source if not (source / rest).exists() else lost).append(ref)
    check = Check(3, "成果引用在数据目录内可解析", not lost)
    check.details.append(f"无法解析 {len(missing)} 个")
    check.details += [f"来源中本就缺失：{r}" for r in absent_in_source]
    check.details += [f"来源中存在但未复制：{r}" for r in lost]
    return check


GIT = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false"]


def _git(directory: Path, *args: str) -> tuple[int, str, str]:
    """Read-only git: no optional locks, no fsmonitor hook, no lazy fetch of objects."""
    try:
        proc = subprocess.run(
            [*GIT, "-C", str(directory), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


UNVERIFIED = "无法核实"


def scene(attempt: Mapping[str, Any]) -> list[str]:
    """Differences between an old attempt's recorded execution scene and the disk now.

    A failed git query is reported as ``无法核实：…``, never as agreement.
    """
    place = attempt.get("worktree") or attempt.get("working_directory")
    if not place:
        return []
    directory = Path(str(place))
    if not directory.is_dir():
        return [f"执行目录不存在：{place}"]
    code, _, err = _git(directory, "rev-parse", "--git-dir")
    if code != 0:
        if "not a git repository" in err.lower():
            return []  # a research directory, not a git checkout
        return [f"{UNVERIFIED}：{place}（{err or code}）"]
    out: list[str] = []
    branch = attempt.get("branch")
    code, current, err = _git(directory, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        out.append(f"{UNVERIFIED}：{place} 的分支（{err or code}）")
    elif branch and current != branch:
        out.append(f"分支为 {current}，记录为 {branch}：{place}")
    base = attempt.get("base_commit")
    if base and _git(directory, "cat-file", "-e", f"{base}^{{commit}}")[0] != 0:
        out.append(f"记录的 base {str(base)[:12]} 在该仓库中不存在：{place}")
    elif base:
        code, _, err = _git(directory, "merge-base", "--is-ancestor", str(base), "HEAD")
        if code == 1:
            out.append(f"记录的 base {str(base)[:12]} 不在当前 HEAD 的历史中：{place}")
        elif code != 0:
            out.append(f"{UNVERIFIED}：{place} 的 base {str(base)[:12]}（{err or code}）")
    code, status, err = _git(directory, "status", "--porcelain")
    if code != 0:
        out.append(f"{UNVERIFIED}：{place} 的未提交改动（{err or code}）")
    elif status:
        out.append(f"有未提交改动：{place}")
    return out


def _scenes(ledger: Ledger) -> Check:
    with ledger.view() as conn:
        resumable = [
            (str(r["task_id"]), json.loads(r["legacy"] or "{}"))
            for r in conn.execute("SELECT task_id, legacy FROM imported_tasks WHERE resumable = 1")
        ]
    found: list[str] = []
    for task_id, legacy in resumable:
        attempts = legacy.get("attempts") or []
        if attempts:
            found += [f"{task_id}：{d}" for d in scene(attempts[-1])]
    differences = [f for f in found if UNVERIFIED not in f]
    ok: bool | None = False if differences else (None if found else True)
    check = Check(4, "可 resume 任务的执行现场与记录一致", ok)
    check.details.append(f"核对 {len(resumable)} 个可 resume 任务的最后一个 attempt")
    check.details += found
    return check


def checks(
    home: Home,
    ledger: Ledger,
    snapshot: Mapping[str, Any],
    *,
    classes: Mapping[str, str],
    conflicts: list[str],
    missing: list[str],
    volume_problems: list[str],
    new_events: int,
    rerun: bool,
) -> list[Check]:
    source = Path(str(snapshot["source"]["root"]))
    idempotent = Check(
        6,
        "重复执行导入不产生新事件",
        (new_events == 0) if rerun else None,
        [f"本次运行追加 {new_events} 条事件"]
        + (
            []
            if rerun
            else ["本次有要写入的内容（首次、增量或补导）；用同样的参数再运行一次即得出结论"]
        ),
    )
    return [
        _counts(ledger, snapshot, classes, conflicts),
        _volumes(home, source, volume_problems),
        _refs(missing, source),
        _scenes(ledger),
        Check(5, "新旧 STATUS 页的业务时间、PR、产出、验收标签一致", None, ["人工对照"]),
        idempotent,
        Check(7, "抽取至少 3 个可 resume 任务做 resume 演练（到对账为止）", None, ["人工演练"]),
    ]
