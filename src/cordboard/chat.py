"""The agent back-channel.

Append-only, and appended with a single ``O_APPEND`` write so concurrent senders
need no lock. Chat is advisory: it never carries baton state. Anything that
changes what happens next belongs in a redline comment or in status.json.
"""

from __future__ import annotations

import os
from typing import Any

from . import state

ROTATE_BYTES = 200_000
HUMAN = "human"


def rotate_if_large(root: str) -> None:
    path = state.chat_path(root)
    if not os.path.exists(path) or os.path.getsize(path) < ROTATE_BYTES:
        return
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
    rotate_if_large(root)
    header = f"## {state.now()} · {sender} → {to}"
    if task_id:
        header += f" · task {task_id}"
    if ref:
        header += f" · re {ref}"
    entry = f"\n{header}\n{body.rstrip()}\n"
    # O_APPEND is atomic for a write this size, so two senders cannot interleave.
    fd = os.open(state.chat_path(root), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, entry.encode("utf-8"))
    finally:
        os.close(fd)
    return header


def unread(root: str, data: dict[str, Any], agent: str) -> tuple[str, int]:
    """Everything after this agent's cursor, plus the new cursor position."""
    path = state.chat_path(root)
    if not os.path.exists(path):
        return "", 0
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    cursor = int((state.agents(data).get(agent) or {}).get("chat_cursor") or 0)
    if cursor > len(lines):
        cursor = 0  # rotated under us; re-read from the top rather than skip it
    return "\n".join(lines[cursor:]).strip(), len(lines)
