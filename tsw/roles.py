"""Role instructions: one source for every host (DN006 §6.2).

``plugin/roles/worker.md`` in the product repository is the only source of what a worker is
told about its work. ``tsw host render`` (and ``tsw init`` for a data directory that has none)
copies it to ``roles/worker.md`` in the data directory, a file only tsw writes. A worker session
is created with the prompt ``attempt_start`` returns: the instructions first, then the short
instruction that sends it to its brief (or, prestarted, tells it to wait). A missing copy, or
one that differs from the source the running tsw ships with, refuses the start: the product was
updated without ``tsw host render``, and a worker would otherwise get rules the running tsw no
longer follows.

A session keeps the instructions it was created with. Which version that was is kept in
``cache/worker-instructions/<session name>``; the worker's hooks compare it with the copy and
tell a session once, for each newer version, to read the copy again.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tsw.errors import rejected
from tsw.storage.fs import atomic_write

# The product's own copy of the role instructions; not in the Python package, like ``plugin/``
# (an editable install from a checkout, DN006 §9.1).
SOURCE_DIR = Path(__file__).resolve().parent.parent / "plugin" / "roles"
# Where tsw puts them in the data directory (DN006 §9.2): written only by tsw.
RENDERED_DIR = "roles"
WORKER = "worker.md"
RENDER_HINT = "运行 tsw host render（产品仓库更新后都要运行）"
# Which version of the instructions each worker session was given (cache: rebuildable).
GIVEN_DIR = Path("cache") / "worker-instructions"


def worker_source() -> Path:
    return SOURCE_DIR / WORKER


def worker_copy(root: Path) -> Path:
    return root / RENDERED_DIR / WORKER


def render_roles(root: Path) -> list[Path]:
    """Copy the worker instructions into the data directory; returns what was written."""
    copy = worker_copy(root)
    atomic_write(copy, worker_source().read_bytes())
    return [copy]


def ensure_roles(root: Path) -> list[Path]:
    """Write the copy when the data directory has none and the source is at hand (``tsw init``)."""
    if worker_copy(root).is_file() or not worker_source().is_file():
        return []
    return render_roles(root)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def worker_instructions(root: Path) -> tuple[str, str]:
    """The worker instructions a session is created with, and their digest.

    Refused when the copy is missing, or differs from the source next to the running tsw (when
    that is at hand: an install without ``plugin/`` has only the copy).
    """
    copy = worker_copy(root)
    try:
        data = copy.read_bytes()
    except FileNotFoundError:
        raise rejected(
            f"数据目录中没有 worker 说明 {RENDERED_DIR}/{WORKER}，不能创建 worker 会话："
            f"{RENDER_HINT}",
            reason="worker_instructions_missing",
            path=str(copy),
        ) from None
    source = worker_source()
    if source.is_file() and source.read_bytes() != data:
        raise rejected(
            f"数据目录中的 worker 说明 {RENDERED_DIR}/{WORKER} 与产品仓库的 plugin/roles/{WORKER} "
            f"不一致，不能创建 worker 会话：{RENDER_HINT}",
            reason="worker_instructions_stale",
            path=str(copy),
            source=str(source),
        )
    return data.decode("utf-8").replace("\r\n", "\n"), _digest(data)


def session_line(task_id: str, attempt: str, purpose: str) -> str:
    """What follows the instructions in a worker session's prompt (CC001 §3)."""
    brief = f"runs/{task_id}/{attempt}/brief.md"
    if purpose == "prestart":
        return (
            f"你是 TaskSteward 任务 {task_id} 的预启动待命会话（attempt {attempt}）。先调用 "
            f"whoami；可以先阅读任务书 {brief}，但不要开始工作，等主会话派发后发来的消息。"
        )
    return (
        f"你是 TaskSteward 任务 {task_id} 的 worker（attempt {attempt}）。先调用 whoami，"
        f"再按任务书 {brief} 执行。"
    )


def worker_prompt(instructions: str, task_id: str, attempt: str, purpose: str) -> str:
    """A worker session's prompt: the instructions, then the line for this session."""
    return f"{instructions.rstrip()}\n\n---\n\n{session_line(task_id, attempt, purpose)}\n"


def _given_path(root: Path, session_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_name)
    return root / GIVEN_DIR / safe


def record_given(root: Path, session_name: str, digest: str) -> None:
    """Remember which instructions the session named ``session_name`` is (being) given."""
    atomic_write(_given_path(root, session_name), digest.encode("ascii"))


def reread_notice(root: Path, session_name: str | None) -> str | None:
    """Tell a worker session once that the instructions changed since it got them, or None.

    A session created before its prompt carried the instructions has no record: it is told
    too. Nothing is said when the data directory has no copy (``tsw host render`` is due, and
    the orchestrator hears of it when it next starts a session).
    """
    if not session_name:
        return None
    copy = worker_copy(root)
    try:
        current = _digest(copy.read_bytes())
    except FileNotFoundError:
        return None
    path = _given_path(root, session_name)
    try:
        given = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        given = None
    if given == current:
        return None
    atomic_write(path, current.encode("ascii"))
    first = (
        "本会话创建时还没有附上 worker 说明" if given is None else "worker 说明在本会话创建后有更新"
    )
    return (
        f"{first}：现在完整阅读 {copy}，从这一轮起以它为准（它与会话开始时提示语开头的说明"
        "不同时，以这份为准）。"
    )
