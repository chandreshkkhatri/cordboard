"""Creating a board, and upgrading supported legacy boards."""

from __future__ import annotations

import copy
import json
import os
import posixpath
import shutil
from typing import Any

from . import state

#: v2 kept each agent as a top-level key and named them after the two tools it
#: happened to use. v3 nests them under `agents` and lets them be called anything.
V2_ROLE_BY_AGENT = {"claude_code": "reviewer", "antigravity": "author"}


def trigger_to_watch_tick(trigger: str | None) -> tuple[bool, str | None]:
    """Upgrade an old either/or `trigger` to the watch-primary pair.

    `watch` gains a backstop it never had, which is the point of the change. `tick`
    keeps its schedule and does not gain a watcher: a host that was put on `tick`
    was usually put there because it cannot watch, and turning one on for it would
    claim a capability nobody verified.
    """
    if trigger == "watch":
        return True, state.DEFAULT_TICK
    if trigger == "tick":
        return False, state.DEFAULT_TICK
    return True, state.DEFAULT_TICK


def blank_agent(watch: bool = True, tick: str | None = state.DEFAULT_TICK) -> dict[str, Any]:
    """A new agent watches by default, with a slow tick behind it.

    The two are not alternatives. `watch` is how a handoff is noticed in seconds;
    `tick` is the backstop that catches one the watcher slept through — a dropped
    subscription, a host that did not re-invoke, a session that was restarted. An
    agent whose host cannot watch sets `watch: false`, and its tick becomes the
    only wake it gets.
    """
    return {
        "status": "paused",
        "watch": watch,
        "tick": tick,
        "heartbeat": None,
        "chat_cursor": 0,
        "note": "",
    }


def blank_board(workflow: str, reviewer: str, author: str) -> dict[str, Any]:
    return {
        "version": state.SCHEMA_VERSION,
        "enabled": False,
        "workflow": workflow,
        "auto_commit": True,
        "max_rounds": state.DEFAULT_MAX_ROUNDS,
        "lease_seconds": state.DEFAULT_LEASE_SECONDS,
        "stall_after_seconds": state.DEFAULT_STALL_AFTER_SECONDS,
        "rev": 0,
        "history": {
            "events": [],
            "truncated": False,
            "earliest_rev": None,
            "incomplete_through_rev": None,
        },
        "roles": {"reviewer": reviewer, "author": author},
        "agents": {
            reviewer: blank_agent(),
            author: blank_agent(),
        },
        "lock": {"holder": None, "acquired_at": None, "expires_at": None},
        "task": {
            "id": None,
            "title": None,
            "kind": "implement",
            "phase": "idle",
            "dir": None,
            "plan_file": None,
            "last_redline": None,
            "verdict": None,
            "handoff_at": None,
            "round": {"plan": 0, "impl": 0},
        },
        "updated_at": None,
        "updated_by": "user",
    }


STACK_TEMPLATE = """# Task stack

**LIFO — the bottom-most task is picked first.** Add new work at the bottom; `cordboard task start`
picks it and agent-facing `cordboard finish` checks it off. Keep each entry to one line plus
optional indented notes; anything longer belongs in a plan.

Format: `- [ ] <short-id> — <what needs to be true when this is done>`

## Queue

<!-- add tasks below this line; the bottom one runs next -->
"""

GITIGNORE_TEMPLATE = """# cordboard runtime state — the board is local, the spec is shared
status.json
status.json.*.bak
.status.lock
.chat.lock
.todo.lock
chat.md
chat.*.md
plans/
"""


def init_board(
    root: str,
    workflow: str = "redline",
    reviewer: str = "reviewer",
    author: str = "author",
    force: bool = False,
) -> dict[str, Any] | None:
    """Create the board directory and its files. None if one is already there."""
    path = state.status_path(root)
    if os.path.exists(path) and not force:
        return None

    os.makedirs(state.plans_dir(root), exist_ok=True)
    data = blank_board(workflow, reviewer, author)
    state.write_atomic(root, data)

    stack = state.stack_path(root)
    if not os.path.exists(stack):
        with open(stack, "w", encoding="utf-8") as handle:
            handle.write(STACK_TEMPLATE)

    # Scoped to the board directory, so dropping cordboard into a repo does not
    # touch the repo's own .gitignore.
    ignore = os.path.join(root, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write(GITIGNORE_TEMPLATE)

    here = os.path.dirname(os.path.abspath(__file__))
    shutil.copyfile(
        os.path.join(here, "data", "instructions.md"),
        os.path.join(root, "instructions.md"),
    )
    return data


def _modern_candidate(old: dict[str, Any], *, convert_triggers: bool = False) -> dict[str, Any]:
    """Normalize a v3-v5 board into the current complete shape without writing."""
    mapping = old.get("roles")
    if not isinstance(mapping, dict):
        raise state.StateError("legacy roles must be an object")
    reviewer, author = mapping.get("reviewer"), mapping.get("author")
    if not isinstance(reviewer, str) or not reviewer:
        raise state.StateError("legacy roles.reviewer must name an agent")
    if not isinstance(author, str) or not author:
        raise state.StateError("legacy roles.author must name an agent")
    fresh = blank_board(str(old.get("workflow") or "redline"), reviewer, author)
    for key in (
        "enabled",
        "auto_commit",
        "max_rounds",
        "lease_seconds",
        "stall_after_seconds",
        "rev",
        "updated_at",
        "updated_by",
    ):
        if key in old:
            fresh[key] = copy.deepcopy(old[key])
    fresh["roles"] = copy.deepcopy(mapping)

    old_agents = old.get("agents")
    if not isinstance(old_agents, dict):
        raise state.StateError("legacy agents must be an object")
    fresh["agents"] = {}
    for name, block in old_agents.items():
        if not isinstance(name, str) or not name or not isinstance(block, dict):
            raise state.StateError("legacy agents must contain named agent objects")
        agent = blank_agent()
        agent.update(copy.deepcopy(block))
        if convert_triggers:
            agent["watch"], agent["tick"] = trigger_to_watch_tick(agent.pop("trigger", None))
        fresh["agents"][name] = agent

    old_lock = old.get("lock")
    if old_lock is not None:
        if not isinstance(old_lock, dict):
            raise state.StateError("legacy lock must be an object")
        fresh["lock"].update(copy.deepcopy(old_lock))
    old_task = old.get("task")
    if not isinstance(old_task, dict):
        raise state.StateError("legacy task must be an object")
    for key, value in old_task.items():
        if key == "round" and isinstance(value, dict):
            fresh["task"]["round"].update(copy.deepcopy(value))
        else:
            fresh["task"][key] = copy.deepcopy(value)
    return fresh


def _migrate_v3(old: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """v3 -> current: add watch-primary backstops and the factual history ring."""
    fresh = _modern_candidate(old, convert_triggers=True)
    fresh["history"] = {
        "events": [],
        "truncated": bool(int(old.get("rev", 0))),
        "earliest_rev": None,
        "incomplete_through_rev": int(old.get("rev", 0)) or None,
    }

    watching = [n for n, a in fresh["agents"].items() if a.get("watch")]
    report = (
        f"migrated v3 -> v{state.SCHEMA_VERSION}: watch primary for "
        f"{watching or 'nobody'}, backstop tick on all"
    )
    return fresh, report


def _migrate_v4(old: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """v4 -> current: add an honest empty history ring without inventing old events."""
    fresh = _modern_candidate(old)
    fresh["history"] = {
        "events": [],
        "truncated": bool(int(old.get("rev", 0))),
        "earliest_rev": None,
        "incomplete_through_rev": int(old.get("rev", 0)) or None,
    }
    return fresh, f"migrated v4 -> v{state.SCHEMA_VERSION}: factual history starts empty"


def _migrate_v5(old: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """v5 -> v6: persist an honest exact boundary for previously unknown history."""
    fresh = _modern_candidate(old)
    old_history = dict(old.get("history") or {})
    old_history["incomplete_through_rev"] = (
        int(old.get("rev", 0)) if old_history.get("truncated") else None
    )
    fresh["history"] = old_history
    return fresh, f"migrated v5 -> v{state.SCHEMA_VERSION}: exact history loss boundary added"


def _v2_artifact_path(value: Any) -> Any:
    """Move the old board-relative ``collab/plans`` namespace into ``plans``."""
    if not isinstance(value, str):
        return copy.deepcopy(value)
    normalized = value.replace("\\", "/")
    if normalized.startswith("collab/plans/"):
        return normalized.removeprefix("collab/")
    return normalized


def _install_migration(root: str, legacy: str, fresh: dict[str, Any], version: int) -> None:
    """Install a fully validated candidate while preserving ancillary workspace files."""
    state.validate(fresh)
    os.makedirs(root, exist_ok=True)
    if os.path.abspath(root) != os.path.abspath(legacy):
        for filename in ("TODO.md", "chat.md"):
            source = os.path.join(legacy, filename)
            target = os.path.join(root, filename)
            if os.path.isfile(source) and not os.path.exists(target):
                shutil.copyfile(source, target)
        source_plans = state.plans_dir(legacy)
        if os.path.isdir(source_plans):
            for directory, _, filenames in os.walk(source_plans):
                relative = os.path.relpath(directory, source_plans)
                target_dir = os.path.join(state.plans_dir(root), relative)
                os.makedirs(target_dir, exist_ok=True)
                for filename in filenames:
                    source = os.path.join(directory, filename)
                    target = os.path.join(target_dir, filename)
                    if not os.path.exists(target):
                        shutil.copyfile(source, target)

    os.makedirs(state.plans_dir(root), exist_ok=True)
    stack = state.stack_path(root)
    if not os.path.exists(stack):
        with open(stack, "w", encoding="utf-8") as handle:
            handle.write(STACK_TEMPLATE)
    ignore = os.path.join(root, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write(GITIGNORE_TEMPLATE)
    here = os.path.dirname(os.path.abspath(__file__))
    shutil.copyfile(
        os.path.join(here, "data", "instructions.md"),
        os.path.join(root, "instructions.md"),
    )
    destination = state.status_path(root)
    if os.path.exists(destination):
        shutil.copyfile(destination, destination + f".v{version}.bak")
    state.write_atomic(root, fresh)


def migrate_board(root: str, legacy: str) -> str:
    """Upgrade a v2-v5 board to the current schema after a no-write preflight."""
    source = state.status_path(legacy)
    if not os.path.exists(source):
        raise state.StateError(f"no board at {source}")
    try:
        with open(source, encoding="utf-8") as handle:
            old = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise state.StateError(f"cannot read legacy board: {exc}") from exc
    if not isinstance(old, dict):
        raise state.StateError("legacy status must be a JSON object")
    version = old.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise state.StateError("legacy version must be an integer")
    if version == state.SCHEMA_VERSION:
        state.validate(old)
        return f"already at schema v{version}; nothing to migrate"
    if version < 2:
        raise state.StateError("schema v1 is not supported; expected v2-v5")
    if version > state.SCHEMA_VERSION:
        raise state.StateError(
            f"schema v{version} is newer than supported v{state.SCHEMA_VERSION}"
        )

    try:
        if version == 3:
            fresh, report = _migrate_v3(old)
            _install_migration(root, legacy, fresh, version)
            return report
        elif version == 4:
            fresh, report = _migrate_v4(old)
            _install_migration(root, legacy, fresh, version)
            return report
        elif version == 5:
            fresh, report = _migrate_v5(old)
            _install_migration(root, legacy, fresh, version)
            return report
    except (TypeError, ValueError) as exc:
        raise state.StateError(f"malformed schema v{version} board: {exc}") from exc

    # v2's `mode` is not carried across: solo is now expressed by pointing both
    # roles at one agent, so the field has nowhere to go and needs nowhere to go.
    mode = old.get("mode", "dual")
    old_roles = old.get("roles") or {}
    if not isinstance(old_roles, dict):
        raise state.StateError("legacy roles must be an object")
    reviewer = old_roles.get("reviewer", "claude_code")
    author = old_roles.get("author", "antigravity")
    if not isinstance(reviewer, str) or not reviewer:
        raise state.StateError("legacy roles.reviewer must name an agent")
    if not isinstance(author, str) or not author:
        raise state.StateError("legacy roles.author must name an agent")
    if mode == "solo_cc":
        author = reviewer
    elif mode == "solo_ag":
        reviewer = author

    fresh = blank_board(old.get("workflow", "relay"), reviewer, author)
    fresh["enabled"] = copy.deepcopy(old.get("enabled", False))
    fresh["auto_commit"] = copy.deepcopy(old.get("auto_commit", True))
    fresh["max_rounds"] = copy.deepcopy(
        old.get("max_rounds", state.DEFAULT_MAX_ROUNDS)
    )
    fresh["lease_seconds"] = copy.deepcopy(
        old.get("lease_seconds", state.DEFAULT_LEASE_SECONDS)
    )
    fresh["stall_after_seconds"] = copy.deepcopy(
        old.get("stall_after_seconds", state.DEFAULT_STALL_AFTER_SECONDS)
    )
    fresh["rev"] = copy.deepcopy(old.get("rev", 0))
    fresh["updated_at"] = copy.deepcopy(old.get("updated_at"))
    fresh["updated_by"] = copy.deepcopy(old.get("updated_by", "user"))
    fresh["history"]["truncated"] = bool(fresh["rev"])
    fresh["history"]["incomplete_through_rev"] = fresh["rev"] or None
    old_lock = old.get("lock")
    if old_lock is not None:
        if not isinstance(old_lock, dict):
            raise state.StateError("legacy lock must be an object")
        fresh["lock"].update(copy.deepcopy(old_lock))

    old_task = old.get("task") or {}
    if not isinstance(old_task, dict):
        raise state.StateError("legacy task must be an object")
    for key in (
        "id", "title", "kind", "phase", "dir", "plan_file", "last_redline",
        "verdict", "handoff_at",
    ):
        if key in old_task:
            value = old_task[key]
            fresh["task"][key] = (
                _v2_artifact_path(value)
                if key in {"dir", "plan_file", "last_redline"}
                else copy.deepcopy(value)
            )
    if fresh["task"]["dir"] is None and fresh["task"]["plan_file"]:
        fresh["task"]["dir"] = posixpath.dirname(fresh["task"]["plan_file"])
    if isinstance(old_task.get("round"), dict):
        fresh["task"]["round"].update(copy.deepcopy(old_task["round"]))

    carried = []
    for name in dict.fromkeys((*V2_ROLE_BY_AGENT, reviewer, author)):
        block = old.get(name)
        if not isinstance(block, dict):
            continue
        target = fresh["agents"].setdefault(name, blank_agent())
        for key in ("status", "heartbeat", "chat_cursor", "note"):
            if block.get(key) is not None:
                target[key] = block[key]
        target["watch"], target["tick"] = trigger_to_watch_tick(block.get("trigger"))
        carried.append(name)

    _install_migration(root, legacy, fresh, version)

    return (
        f"migrated v{version} -> v{state.SCHEMA_VERSION}: workflow={fresh['workflow']} "
        f"phase={fresh['task']['phase']} roles=reviewer:{reviewer},author:{author} "
        f"agents carried={carried or 'none'} "
        f"(v2 mode={mode!r} dropped — solo is now a role mapping)"
    )


# Compatibility for callers of the original v2-only API. The public CLI and
# documentation use the truthful general name.
migrate_v2 = migrate_board
