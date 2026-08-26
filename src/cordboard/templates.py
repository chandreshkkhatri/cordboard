"""Creating a board, and importing a legacy one.

The v2 importer exists because the predecessor ran real tasks before this package
was split out, and a board mid-task should survive the move.
"""

from __future__ import annotations

import json
import os
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
picks it and `cordboard task complete` checks it off. Keep each entry to one line plus optional
indented notes; anything longer belongs in a plan.

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


def _migrate_v3(root: str, old: dict[str, Any]) -> str:
    """v3 -> v4: the either/or `trigger` becomes watch-primary plus a backstop tick."""
    fresh = dict(old)
    fresh["version"] = state.SCHEMA_VERSION
    fresh.setdefault("stall_after_seconds", state.DEFAULT_STALL_AFTER_SECONDS)
    fresh.setdefault("task", {}).setdefault("handoff_at", None)

    upgraded = {}
    for name, block in (old.get("agents") or {}).items():
        agent = dict(block)
        agent["watch"], agent["tick"] = trigger_to_watch_tick(agent.pop("trigger", None))
        upgraded[name] = agent
    fresh["agents"] = upgraded

    if os.path.exists(state.status_path(root)):
        shutil.copyfile(state.status_path(root), state.status_path(root) + ".v3.bak")
    state.write_atomic(root, fresh)
    watching = [n for n, a in upgraded.items() if a.get("watch")]
    return (
        f"migrated v3 -> v{state.SCHEMA_VERSION}: watch primary for "
        f"{watching or 'nobody'}, backstop tick on all"
    )


def migrate_v2(root: str, legacy: str) -> str:
    """Import a v2 board. Returns a human-readable report of what carried over."""
    source = state.status_path(legacy)
    if not os.path.exists(source):
        raise state.StateError(f"no v2 board at {source}")
    with open(source, encoding="utf-8") as handle:
        old = json.load(handle)

    version = int(old.get("version", 1))
    if version >= state.SCHEMA_VERSION:
        return f"already at schema v{version}; nothing to migrate"

    if version == 3:
        return _migrate_v3(root, old)

    # v2's `mode` is not carried across: solo is now expressed by pointing both
    # roles at one agent, so the field has nowhere to go and needs nowhere to go.
    mode = old.get("mode", "dual")
    reviewer = "claude_code"
    author = "antigravity"
    if mode == "solo_cc":
        author = reviewer
    elif mode == "solo_ag":
        reviewer = author

    fresh = blank_board(old.get("workflow", "relay"), reviewer, author)
    fresh["enabled"] = bool(old.get("enabled", False))
    fresh["auto_commit"] = bool(old.get("auto_commit", True))
    fresh["max_rounds"] = int(old.get("max_rounds", state.DEFAULT_MAX_ROUNDS))
    fresh["lease_seconds"] = int(old.get("lease_seconds", state.DEFAULT_LEASE_SECONDS))
    fresh["rev"] = int(old.get("rev", 0))
    fresh["lock"] = old.get("lock") or fresh["lock"]

    old_task = old.get("task") or {}
    for key in ("id", "title", "kind", "phase", "dir", "plan_file", "last_redline", "verdict"):
        if old_task.get(key) is not None:
            fresh["task"][key] = old_task[key]
    if isinstance(old_task.get("round"), dict):
        fresh["task"]["round"].update(old_task["round"])

    carried = []
    for name in V2_ROLE_BY_AGENT:
        block = old.get(name)
        if not isinstance(block, dict):
            continue
        target = fresh["agents"].setdefault(name, blank_agent())
        for key in ("status", "heartbeat", "chat_cursor", "note"):
            if block.get(key) is not None:
                target[key] = block[key]
        target["watch"], target["tick"] = trigger_to_watch_tick(block.get("trigger"))
        carried.append(name)

    os.makedirs(state.plans_dir(root), exist_ok=True)
    if os.path.exists(state.status_path(root)):
        shutil.copyfile(state.status_path(root), state.status_path(root) + f".v{version}.bak")
    state.write_atomic(root, fresh)

    return (
        f"migrated v{version} -> v{state.SCHEMA_VERSION}: workflow={fresh['workflow']} "
        f"phase={fresh['task']['phase']} roles=reviewer:{reviewer},author:{author} "
        f"agents carried={carried or 'none'} "
        f"(v2 mode={mode!r} dropped — solo is now a role mapping)"
    )
