"""The committed Markdown TODO stack."""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

from . import state

ENTRY = re.compile(
    r"^- \[(?P<mark>[ xX])\] (?P<id>[A-Za-z0-9][A-Za-z0-9_.-]*)\s+—\s+(?P<title>\S.*)$"
)


@dataclass(frozen=True)
class StackTask:
    id: str
    title: str
    done: bool
    line: int


def read(root: str) -> list[StackTask]:
    """Read and validate checkbox entries, preserving their file order."""
    path = state.stack_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError as exc:
        raise state.StateError(f"no task stack at {path}") from exc

    result: list[StackTask] = []
    seen: dict[str, int] = {}
    for number, line in enumerate(lines, 1):
        if not line.startswith("- ["):
            continue
        match = ENTRY.fullmatch(line)
        if not match:
            raise state.StateError(
                f"invalid task at {path}:{number}; expected `- [ ] <id> — <title>`"
            )
        task_id = match.group("id")
        if task_id in seen:
            raise state.StateError(
                f"duplicate task id {task_id!r} at {path}:{seen[task_id]} and :{number}"
            )
        seen[task_id] = number
        result.append(
            StackTask(
                id=task_id,
                title=match.group("title"),
                done=match.group("mark").lower() == "x",
                line=number,
            )
        )
    return result


def next_pending(root: str) -> StackTask | None:
    """Return the bottom-most unchecked task, which is the LIFO head."""
    return next((item for item in reversed(read(root)) if not item.done), None)


def summary(root: str) -> dict[str, Any]:
    """A small computed view for status displays; it is never persisted."""
    entries = read(root)
    next_up = next((item for item in reversed(entries) if not item.done), None)
    return {
        "total": len(entries),
        "pending": sum(not item.done for item in entries),
        "completed": sum(item.done for item in entries),
        "next": (
            {"id": next_up.id, "title": next_up.title, "line": next_up.line}
            if next_up
            else None
        ),
    }


def complete(root: str, task_id: str) -> bool:
    """Atomically check off ``task_id``. Return false when it was already done."""
    path = state.stack_path(root)
    lock_path = os.path.join(root, ".todo.lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            entries = read(root)
            item = next((candidate for candidate in entries if candidate.id == task_id), None)
            if item is None:
                raise state.StateError(f"active task {task_id!r} is not in {path}")
            if item.done:
                return False

            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines(keepends=True)
            lines[item.line - 1] = lines[item.line - 1].replace("- [ ]", "- [x]", 1)

            fd, temporary = tempfile.mkstemp(dir=root, prefix=".TODO.", suffix=".tmp")
            try:
                os.fchmod(fd, 0o644)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.writelines(lines)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except BaseException:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
