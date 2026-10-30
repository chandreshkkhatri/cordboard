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
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 6
HISTORY_CAP = 128
HISTORY_FIELDS = frozenset(
    {"events", "truncated", "earliest_rev", "incomplete_through_rev"}
)
HISTORY_EVENT_FIELDS = frozenset(
    {
        "rev",
        "at",
        "task_id",
        "task_title",
        "actor",
        "action",
        "from_phase",
        "phase",
        "mode",
        "verdict",
        "round",
        "outcome",
        "lease",
        "artifacts",
    }
)

DEFAULT_MAX_ROUNDS = 3
DEFAULT_LEASE_SECONDS = 5400

#: A backstop schedule for an agent whose watcher is primary. Slow on purpose: it
#: exists to catch a missed handoff, not to drive the loop.
DEFAULT_TICK = "*/20 * * * *"

#: How long the baton may sit with an agent before we call the handoff missed.
#: Longer than a watcher takes to react (seconds) and shorter than the backstop
#: interval, so a stall is visible before the tick papers over it.
DEFAULT_STALL_AFTER_SECONDS = 600
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


def validation_errors(data: dict[str, Any]) -> list[str]:
    """Return schema and safety violations without mutating the board."""
    from .workflows import ADVANCE_MODES, PHASES, ROLES, TAKE_MODES, VERDICTS

    errors: list[str] = []
    if data.get("version") != SCHEMA_VERSION:
        errors.append(
            f"version must be {SCHEMA_VERSION}, got {data.get('version')!r}; "
            "run `cordboard init --migrate`"
        )
    if not isinstance(data.get("enabled"), bool):
        errors.append("enabled must be true or false")

    flow = data.get("workflow")
    if flow not in PHASES:
        errors.append(f"workflow must be one of {sorted(PHASES)}, got {flow!r}")
    known_phases = set().union(*(phases.keys() for phases in PHASES.values()))
    history_modes = {
        "take": set(TAKE_MODES),
        "advance": set(ADVANCE_MODES),
        "finish": {"next", "parked"},
    }

    for key in ("max_rounds", "lease_seconds", "stall_after_seconds"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"{key} must be a positive integer")
    revision = data.get("rev")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        errors.append("rev must be a non-negative integer")

    history = data.get("history")
    if not isinstance(history, dict):
        errors.append("history must be an object")
    else:
        if set(history) != HISTORY_FIELDS:
            errors.append("history must contain exactly the bounded history fields")
        events = history.get("events")
        truncated = history.get("truncated")
        earliest = history.get("earliest_rev")
        incomplete_through = history.get("incomplete_through_rev")
        if not isinstance(events, list):
            errors.append("history.events must be an array")
            events = []
        elif len(events) > HISTORY_CAP:
            errors.append(f"history.events must contain at most {HISTORY_CAP} events")
        if not isinstance(truncated, bool):
            errors.append("history.truncated must be true or false")
        if incomplete_through is not None and (
            isinstance(incomplete_through, bool)
            or not isinstance(incomplete_through, int)
            or incomplete_through < 0
        ):
            errors.append(
                "history.incomplete_through_rev must be a non-negative integer or null"
            )
        elif isinstance(revision, int) and (
            isinstance(incomplete_through, int) and incomplete_through > revision
        ):
            errors.append("history.incomplete_through_rev cannot exceed board rev")
        if truncated is False and incomplete_through is not None:
            errors.append(
                "history.incomplete_through_rev must be null when history is not truncated"
            )
        if truncated is True and incomplete_through is None:
            errors.append(
                "history.incomplete_through_rev is required when history is truncated"
            )
        if earliest is not None and (
            isinstance(earliest, bool) or not isinstance(earliest, int) or earliest < 0
        ):
            errors.append("history.earliest_rev must be a non-negative integer or null")
        expected_earliest = (
            events[0].get("rev") if events and isinstance(events[0], dict) else None
        )
        if earliest != expected_earliest:
            errors.append("history.earliest_rev must match the first event revision")
        previous_rev = -1
        for index, event in enumerate(events):
            prefix = f"history.events[{index}]"
            if not isinstance(event, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if set(event) != HISTORY_EVENT_FIELDS:
                errors.append(f"{prefix} must contain exactly the factual event fields")
            event_rev = event.get("rev")
            if (
                isinstance(event_rev, bool)
                or not isinstance(event_rev, int)
                or event_rev < 1
                or event_rev <= previous_rev
            ):
                errors.append(f"{prefix}.rev must be a positive increasing integer")
            else:
                previous_rev = event_rev
                if isinstance(revision, int) and event_rev > revision:
                    errors.append(f"{prefix}.rev cannot exceed board rev")
            for key in ("at", "task_id", "actor", "action", "from_phase", "phase", "mode"):
                if not isinstance(event.get(key), str) or not event[key]:
                    errors.append(f"{prefix}.{key} must be a non-empty string")
            event_at = event.get("at")
            if isinstance(event_at, str):
                try:
                    datetime.strptime(event_at, TIMESTAMP)
                except ValueError:
                    errors.append(f"{prefix}.at must be a UTC timestamp")
            title = event.get("task_title")
            if title is not None and not isinstance(title, str):
                errors.append(f"{prefix}.task_title must be a string or null")
            action = event.get("action")
            if action not in history_modes:
                errors.append(f"{prefix}.action must be take, advance, or finish")
            elif event.get("mode") not in history_modes[action]:
                errors.append(f"{prefix}.mode is invalid for action {action}")
            for phase_key in ("from_phase", "phase"):
                if event.get(phase_key) not in known_phases:
                    errors.append(f"{prefix}.{phase_key} must be a known phase")
            if event.get("verdict") is not None and event.get("verdict") not in VERDICTS:
                errors.append(f"{prefix}.verdict must be a known verdict or null")
            event_round = event.get("round")
            if event_round is not None and (
                isinstance(event_round, bool)
                or not isinstance(event_round, int)
                or event_round < 0
            ):
                errors.append(f"{prefix}.round must be a non-negative integer or null")
            if event.get("outcome") not in {None, "clean", "issues"}:
                errors.append(f"{prefix}.outcome must be clean, issues, or null")
            if event.get("lease") not in {"claimed", "retained", "released"}:
                errors.append(f"{prefix}.lease must be claimed, retained, or released")
            artifacts = event.get("artifacts")
            if not isinstance(artifacts, list) or not all(
                isinstance(path, str) and path for path in artifacts
            ):
                errors.append(f"{prefix}.artifacts must be an array of non-empty paths")

    mapping = data.get("roles")
    agents_block = data.get("agents")
    if not isinstance(mapping, dict):
        errors.append("roles must be an object")
        mapping = {}
    if not isinstance(agents_block, dict):
        errors.append("agents must be an object")
        agents_block = {}
    for role in ROLES:
        agent = mapping.get(role)
        if not isinstance(agent, str) or not agent:
            errors.append(f"roles.{role} must name an agent")
        elif agent not in agents_block:
            errors.append(f"roles.{role} names missing agent {agent!r}")
    for name, block in agents_block.items():
        if not isinstance(block, dict):
            errors.append(f"agents.{name} must be an object")
            continue
        watch = block.get("watch")
        tick = block.get("tick")
        if not isinstance(watch, bool):
            errors.append(f"agents.{name}.watch must be true or false")
        if tick is not None and (not isinstance(tick, str) or not tick.strip()):
            errors.append(f"agents.{name}.tick must be a schedule string or null")
        if watch is False and not tick:
            errors.append(f"agents.{name} has neither watch nor tick and cannot wake")
        cursor = block.get("chat_cursor")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            errors.append(f"agents.{name}.chat_cursor must be a non-negative integer")

    lock = data.get("lock")
    if not isinstance(lock, dict):
        errors.append("lock must be an object")
    else:
        holder = lock.get("holder")
        if holder is not None and holder not in agents_block:
            errors.append(f"lock.holder names missing agent {holder!r}")

    task_block = data.get("task")
    if not isinstance(task_block, dict):
        errors.append("task must be an object")
    else:
        current = task_block.get("phase")
        if flow in PHASES and current not in PHASES[flow]:
            errors.append(f"task.phase {current!r} is not part of workflow {flow!r}")
        verdict = task_block.get("verdict")
        if verdict is not None and verdict not in VERDICTS:
            errors.append(f"task.verdict must be one of {VERDICTS} or null")
        rounds = task_block.get("round")
        if not isinstance(rounds, dict):
            errors.append("task.round must be an object")
        else:
            for gate in ("plan", "impl"):
                value = rounds.get(gate)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    errors.append(f"task.round.{gate} must be a non-negative integer")
    return errors


def validate(data: dict[str, Any]) -> None:
    """Raise one actionable error containing every known board violation."""
    errors = validation_errors(data)
    if errors:
        raise StateError("invalid board: " + "; ".join(errors))


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
    secondary_lock: Callable[[], AbstractContextManager[Any]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Read-modify-write the board under a lock, with a rev compare-and-swap.

    ``mutate(data)`` receives a deep copy and edits it in place. Returning ``False``
    aborts the write — which is how a guard refuses, and how an idle tick says
    "nothing to do" without bumping ``rev``.

    When supplied, ``secondary_lock`` is acquired after the status lock and held
    through mutation and state commit. Multi-file operations use this to keep one
    global lock order without exposing the status lock implementation.

    Returns ``(state, written)``.
    """
    lock_file = lock_path(root)
    os.makedirs(root, exist_ok=True)
    for attempt in range(retries):
        with open(lock_file, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                context = secondary_lock() if secondary_lock else nullcontext()
                with context:
                    data = load(root)
                    validate(data)
                    base_rev = data["rev"]
                    draft = copy.deepcopy(data)
                    if mutate(draft) is False:
                        return data, False
                    # Re-read inside both locks: another writer may have landed between
                    # the caller's own earlier read and this critical section.
                    latest = load(root)
                    validate(latest)
                    if latest["rev"] != base_rev:
                        continue
                    # Stamped here rather than by callers: every phase change goes
                    # through this function, and a hand-maintained timestamp is one
                    # an agent eventually forgets on the path that matters.
                    before = (data.get("task") or {}).get("phase")
                    after = (draft.get("task") or {}).get("phase")
                    if after != before:
                        draft.setdefault("task", {})["handoff_at"] = now()
                    draft["rev"] = base_rev + 1
                    draft["updated_at"] = now()
                    draft["updated_by"] = actor
                    validate(draft)
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


def claim_lease(
    data: dict[str, Any],
    agent: str,
    seconds: int | None = None,
    *,
    force: bool = False,
) -> None:
    """Take or renew the work lease. Call inside :func:`update`.

    A live lease belongs to its holder. Another agent may only take it after it
    expires, or when an operator has deliberately requested a forced recovery.
    """
    holder = (data.get("lock") or {}).get("holder")
    if holder and holder != agent and not lease_expired(data) and not force:
        raise StateError(f"lease is held by {holder!r}; wait for expiry or use --force")
    ttl = seconds or int(data.get("lease_seconds") or DEFAULT_LEASE_SECONDS)
    start = datetime.now(timezone.utc)
    data["lock"] = {
        "holder": agent,
        "acquired_at": start.strftime(TIMESTAMP),
        "expires_at": datetime.fromtimestamp(start.timestamp() + ttl, tz=timezone.utc).strftime(
            TIMESTAMP
        ),
    }


def release_lease(
    data: dict[str, Any], agent: str | None = None, *, force: bool = False
) -> None:
    """Release a lease, refusing an agent that does not own a live one.

    ``agent=None`` is the human/operator path and remains available for recovery.
    """
    holder = (data.get("lock") or {}).get("holder")
    if agent and holder and holder != agent and not lease_expired(data) and not force:
        raise StateError(f"lease is held by {holder!r}; only its holder may release it")
    data["lock"] = {"holder": None, "acquired_at": None, "expires_at": None}


# --------------------------------------------------------------------------- stalls


def stall_after(data: dict[str, Any]) -> int:
    return int(data.get("stall_after_seconds") or DEFAULT_STALL_AFTER_SECONDS)


def waiting_seconds(data: dict[str, Any]) -> int | None:
    """How long the baton has sat in the current phase, or None if unknown.

    Unknown is the honest answer for a board written before `handoff_at` existed,
    and for one whose stamp is unparseable. Callers must not read it as zero.
    """
    stamp = (data.get("task") or {}).get("handoff_at")
    if not stamp:
        return None
    try:
        entered = datetime.strptime(stamp, TIMESTAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - entered).total_seconds()))


def is_stalled(data: dict[str, Any]) -> bool:
    """The baton is owed to somebody who has not taken it in time.

    This is the only signal that a watcher missed its event: a healthy `watch`
    agent claims within seconds, so anything past the threshold means the wake
    never arrived and the backstop tick is what will have to catch it.
    """
    if not data.get("enabled"):
        return False
    waited = waiting_seconds(data)
    if waited is None:
        return False
    return waited >= stall_after(data)


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


def record_history_event(
    data: dict[str, Any],
    *,
    task_id: str,
    task_title: str | None,
    actor: str,
    action: str,
    from_phase: str,
    phase: str,
    mode: str,
    verdict: str | None = None,
    round_number: int | None = None,
    outcome: str | None = None,
    lease: str,
    artifacts: list[str] | tuple[str, ...] = (),
) -> None:
    """Append one bounded machine event to the current state transaction."""
    history = data.setdefault(
        "history",
        {
            "events": [],
            "truncated": False,
            "earliest_rev": None,
            "incomplete_through_rev": None,
        },
    )
    events = history.setdefault("events", [])
    events.append(
        {
            "rev": int(data["rev"]) + 1,
            "at": now(),
            "task_id": task_id,
            "task_title": task_title,
            "actor": actor,
            "action": action,
            "from_phase": from_phase,
            "phase": phase,
            "mode": mode,
            "verdict": verdict,
            "round": round_number,
            "outcome": outcome,
            "lease": lease,
            "artifacts": list(dict.fromkeys(path for path in artifacts if path)),
        }
    )
    if len(events) > HISTORY_CAP:
        discarded = events[: len(events) - HISTORY_CAP]
        del events[: len(discarded)]
        history["truncated"] = True
        prior_boundary = history.get("incomplete_through_rev")
        history["incomplete_through_rev"] = max(
            discarded[-1]["rev"],
            prior_boundary if isinstance(prior_boundary, int) else 0,
        )
    history["earliest_rev"] = events[0]["rev"] if events else None
