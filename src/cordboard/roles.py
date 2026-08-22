"""Who may act, and what they may do.

Two independent questions, and conflating them is the bug this module exists to
prevent: an agent can legitimately hold the baton and still be trying to skip a
review gate.

There is deliberately no "solo mode" setting. Solo is what you get when both roles
map to the same agent — a config shape, not a mode with its own code path. The
predecessor of this package carried a `mode` field whose every branch had to be
special-cased through the phase logic; deleting the field deleted the branches.
"""

from __future__ import annotations

from typing import Any

from . import state
from .workflows import ENTRY_PHASE, PHASES, ROLES, TRANSITIONS


def workflow(data: dict[str, Any]) -> str:
    name = data.get("workflow", "relay")
    if name not in PHASES:
        raise state.StateError(f"unknown workflow {name!r}; expected one of {sorted(PHASES)}")
    return name


def role_map(data: dict[str, Any]) -> dict[str, str]:
    """Role -> agent name. Every role must be filled for the board to be runnable."""
    configured = data.get("roles") or {}
    missing = [role for role in ROLES if not configured.get(role)]
    if missing:
        raise state.StateError(
            f"roles {missing} are unassigned; set them in status.json or via "
            f"`cordboard set --role <role>=<agent>`"
        )
    return {role: str(configured[role]) for role in ROLES}


def is_solo(data: dict[str, Any]) -> bool:
    """One agent wearing every hat — the single-session configuration."""
    return len(set(role_map(data).values())) == 1


def phase_role(data: dict[str, Any]) -> str | None:
    """Which role owns the current phase, or None when nobody does."""
    current = state.phase(data)
    table = PHASES[workflow(data)]
    if current not in table:
        raise state.StateError(
            f"phase {current!r} is not part of the {workflow(data)!r} workflow"
        )
    return table[current]


def owner(data: dict[str, Any]) -> str | None:
    """Which agent must act now, or None when the baton is parked."""
    role = phase_role(data)
    if role is None:
        return None
    return role_map(data)[role]


def is_my_turn(data: dict[str, Any], agent: str) -> bool:
    """A disabled board is nobody's turn, which is what makes the pause switch safe."""
    if not data.get("enabled"):
        return False
    return owner(data) == agent


def roles_of(data: dict[str, Any], agent: str) -> list[str]:
    return [role for role, who in role_map(data).items() if who == agent]


def entry_phase(data: dict[str, Any]) -> str:
    return ENTRY_PHASE[workflow(data)]


def can_move(data: dict[str, Any], target: str) -> tuple[bool, str]:
    """Is `target` a legal next phase from where the baton is now?

    Returns ``(allowed, reason)``; the reason names the legal successors, because
    the message is what an agent has to act on when it gets this wrong.
    """
    flow = workflow(data)
    current = state.phase(data)
    if target not in PHASES[flow]:
        return False, f"phase {target!r} is not part of the {flow!r} workflow"
    if target == "blocked":
        # Any working phase may bail out to blocked; that is the escape hatch.
        return True, ""
    if target == current:
        # Re-entering a phase is a watchdog resume, not a move.
        return True, ""
    allowed = TRANSITIONS[flow].get(current, set())
    if target not in allowed:
        return False, (
            f"{current} -> {target} is not a legal {flow} transition "
            f"(allowed: {', '.join(sorted(allowed)) or 'none'})"
        )
    return True, ""


def describe(data: dict[str, Any]) -> str:
    """One-glance summary: where the baton is and who has to move it."""
    try:
        next_up = owner(data) or "—"
        shape = "solo" if is_solo(data) else "dual"
    except state.StateError as exc:
        next_up, shape = f"unresolved ({exc})", "?"
    return (
        f"[{'enabled' if data.get('enabled') else 'DISABLED'}] "
        f"workflow={data.get('workflow', '?')} {shape} rev={data.get('rev', 0)}\n"
        f"task={state.task(data).get('id') or '—'} phase={state.phase(data)} next={next_up}"
    )
