"""The agent back-channel.

Append-only, with rotation and cursor consumption serialized around ``O_APPEND``
writes. Chat is advisory: it never carries baton state. Anything that changes
what happens next belongs in a redline comment or in status.json.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import state

ROTATE_BYTES = 200_000
HUMAN = "human"


def lock_path(root: str) -> str:
    return os.path.join(root, ".chat.lock")


@contextmanager
def locked(root: str) -> Iterator[None]:
    """Serialize operations that couple the active log to chat cursors."""
    with open(lock_path(root), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def rotate_if_large(root: str) -> None:
    path = state.chat_path(root)
    if not os.path.exists(path) or os.path.getsize(path) < ROTATE_BYTES:
        return
    with open(path, encoding="utf-8") as handle:
        line_count = sum(1 for _ in handle)
    data = state.load(root)
    blocks = state.agents(data)
    if any(int((block or {}).get("chat_cursor") or 0) < line_count for block in blocks.values()):
        # Rotating now would strand unread entries in an archive that cursors do
        # not address. Size is a soft limit; delivery is the hard invariant.
        return

    if blocks:
        def reset_cursors(draft: dict[str, Any]) -> None:
            for block in state.agents(draft).values():
                block["chat_cursor"] = 0

        state.update(
            root,
            reset_cursors,
            actor="chat-rotation",
        )
    stamp = state.now()[:10]
    target = os.path.join(root, f"chat.{stamp}.md")
    suffix = 1
    while os.path.exists(target):
        suffix += 1
        target = os.path.join(root, f"chat.{stamp}.{suffix}.md")
    os.rename(path, target)


def append(
    root: str,
    sender: str,
    to: str,
    body: str,
    task_id: str | None = None,
    ref: str | None = None,
) -> str:
    """Add one entry. Returns the header written, so a caller can echo it."""
    header = f"## {state.now()} · {sender} → {to}"
    if task_id:
        header += f" · task {task_id}"
    if ref:
        header += f" · re {ref}"
    entry = f"\n{header}\n{body.rstrip()}\n"
    payload = entry.encode("utf-8")

    # Rotation and append are one transaction. O_APPEND protects an ordinary
    # append, but without this lock one writer can rename the file after another
    # has selected it and split messages unpredictably across archives.
    with locked(root):
        rotate_if_large(root)
        fd = os.open(
            state.chat_path(root), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError("could not append to chat log")
                view = view[written:]
        finally:
            os.close(fd)
    return header


def unread(root: str, data: dict[str, Any], agent: str) -> tuple[str, int]:
    """Everything after this agent's cursor, plus the new cursor position."""
    path = state.chat_path(root)
    if not os.path.exists(path):
        return "", 0
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except UnicodeDecodeError as exc:
        raise state.StateError(f"{path} is not valid UTF-8") from exc
    cursor = int((state.agents(data).get(agent) or {}).get("chat_cursor") or 0)
    if cursor > len(lines):
        cursor = 0  # rotated under us; re-read from the top rather than skip it
    return "\n".join(lines[cursor:]).strip(), len(lines)


def consume(root: str, agent: str, *, peek: bool = False) -> tuple[str, int]:
    """Read unread chat and, unless peeking, advance the cursor under the chat lock."""
    with locked(root):
        data = state.load(root)
        text, cursor = unread(root, data, agent)
        if not peek:
            from .templates import blank_agent

            state.update(
                root,
                lambda draft: draft.setdefault("agents", {})
                .setdefault(agent, blank_agent())
                .update({"chat_cursor": cursor}),
                actor=agent,
            )
        return text, cursor
