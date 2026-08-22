"""Reading and writing the baton file.

Every mutation goes through :func:`update`, which pairs an ``flock`` critical
section (so two writers cannot interleave) with a ``rev`` compare-and-swap (so an
agent that read state in an earlier turn cannot clobber a change it never saw).

Both are needed. The lock alone would let a stale writer win; the CAS alone has a
window between read and write.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import random
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 3

DEFAULT_MAX_ROUNDS = 3
DEFAULT_LEASE_SECONDS = 5400
TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"

#: Directory name created inside the host repo. Visible rather than dot-hidden:
#: plans and redlines are written to be read by a human.
DIR_NAME = "cordboard"
ENV_DIR = "CORDBOARD_DIR"


class StateError(RuntimeError):
    """The state file is missing, unparseable, or a CAS could not be completed."""


# --------------------------------------------------------------------------- paths


def board_dir(explicit: str | None = None) -> str:
    """Where the board lives: an explicit path, then $CORDBOARD_DIR, then ./cordboard."""
    if explicit:
        return os.path.abspath(explicit)
    from_env = os.environ.get(ENV_DIR)
    if from_env:
        return os.path.abspath(from_env)
    return os.path.abspath(DIR_NAME)


def status_path(root: str) -> str:
    return os.path.join(root, "status.json")


def lock_path(root: str) -> str:
    return os.path.join(root, ".status.lock")


def chat_path(root: str) -> str:
    return os.path.join(root, "chat.md")


def plans_dir(root: str) -> str:
    return os.path.join(root, "plans")


def stack_path(root: str) -> str:
    return os.path.join(root, "TODO.md")


def now() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP)


# --------------------------------------------------------------------------- io


def load(root: str) -> dict[str, Any]:
    path = status_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise StateError(f"no board at {path} — run `cordboard init`") from exc
    except json.JSONDecodeError as exc:
        raise StateError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"{path} must contain a JSON object")
    return data


def write_atomic(root: str, data: dict[str, Any]) -> None:
    """Replace the state file in one step, so a reader never sees a partial write."""
    path = status_path(root)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def update(
    root: str,
    mutate: Callable[[dict[str, Any]], Any],
    actor: str = "user",
    retries: int = 40,
) -> tuple[dict[str, Any], bool]:
    """Read-modify-write the board under a lock, with a rev compare-and-swap.

    ``mutate(data)`` receives a deep copy and edits it in place. Returning ``False``
    aborts the write — which is how a guard refuses, and how an idle tick says
    "nothing to do" without bumping ``rev``.

    Returns ``(state, written)``.
    """
    lock_file = lock_path(root)
    os.makedirs(root, exist_ok=True)
    for attempt in range(retries):
        with open(lock_file, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                data = load(root)
                base_rev = int(data.get("rev", 0))
                draft = copy.deepcopy(data)
                if mutate(draft) is False:
                    return data, False
                # Re-read inside the lock: another writer may have landed between
                # the caller's own earlier read and this critical section.
                if int(load(root).get("rev", 0)) != base_rev:
                    continue
                draft["rev"] = base_rev + 1
                draft["updated_at"] = now()
                draft["updated_by"] = actor
                write_atomic(root, draft)
                return draft, True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
    raise StateError(f"could not commit a state update after {retries} attempts")


# --------------------------------------------------------------------------- leases


def lease_expired(data: dict[str, Any]) -> bool:
    """True when no live lease is held. An unparseable or absent lease is expired."""
    expires = (data.get("lock") or {}).get("expires_at")
    if not expires:
        return True
    try:
        deadline = datetime.strptime(expires, TIMESTAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) >= deadline


def claim_lease(data: dict[str, Any], agent: str, seconds: int | None = None) -> None:
    """Take the work lease. Call inside :func:`update`."""
    ttl = seconds or int(data.get("lease_seconds") or DEFAULT_LEASE_SECONDS)
    start = datetime.now(timezone.utc)
    data["lock"] = {
        "holder": agent,
        "acquired_at": start.strftime(TIMESTAMP),
        "expires_at": datetime.fromtimestamp(start.timestamp() + ttl, tz=timezone.utc).strftime(
            TIMESTAMP
        ),
    }


def release_lease(data: dict[str, Any]) -> None:
    data["lock"] = {"holder": None, "acquired_at": None, "expires_at": None}


# --------------------------------------------------------------------------- accessors


def agents(data: dict[str, Any]) -> dict[str, Any]:
    """Per-agent blocks. Nested under `agents` so a name cannot collide with config."""
    block = data.get("agents")
    return block if isinstance(block, dict) else {}


def agent_names(data: dict[str, Any]) -> list[str]:
    return sorted(agents(data))


def touch(
    data: dict[str, Any],
    agent: str,
    status: str | None = None,
    note: str | None = None,
) -> None:
    """Refresh an agent's heartbeat, and optionally its status and note."""
    block = data.setdefault("agents", {}).setdefault(agent, {})
    block["heartbeat"] = now()
    if status is not None:
        block["status"] = status
    if note is not None:
        block["note"] = note


def task(data: dict[str, Any]) -> dict[str, Any]:
    block = data.get("task")
    return block if isinstance(block, dict) else {}


def phase(data: dict[str, Any]) -> str:
    return str(task(data).get("phase") or "idle")


def round_of(data: dict[str, Any], gate: str) -> int:
    return int((task(data).get("round") or {}).get(gate, 0))


def max_rounds(data: dict[str, Any]) -> int:
    return int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS)
