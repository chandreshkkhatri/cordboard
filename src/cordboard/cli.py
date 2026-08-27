"""The `cordboard` command.

One entry point with subcommands, rather than the five loose scripts this grew
out of: an agent's tick prompt is easier to write against `cordboard status` than
against a path it has to know.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from typing import Any

from . import chat, roles, state, tasks
from .templates import init_board, migrate_v2
from .workflows import (
    ADVANCE_MODES,
    ADVANCE_PHASES,
    ADVANCE_REFUSALS,
    BRIEF_DISPOSITIONS,
    BRIEF_GUIDANCE,
    IN_PROGRESS,
    PHASES,
    ROLES,
    TAKE_MODES,
    TAKE_PHASES,
    TAKE_REFUSALS,
    VERDICTS,
)

# cordboard wait exit codes. These are the contract an agent's tick script reads,
# so they are documented here and in the spec, and must not be renumbered.
WAIT_TURN = 0
WAIT_DISABLED = 10
WAIT_TIMEOUT = 11
EXIT_REFUSED = 1
EXIT_BAD_STATE = 2


def _root(args: argparse.Namespace) -> str:
    return state.board_dir(getattr(args, "dir", None))


def _dig(data: dict[str, Any], dotted: str) -> Any:
    cursor: Any = data
    for part in dotted.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _coerce(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def _assign(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    if not all(parts):
        raise state.StateError(f"invalid dotted path {dotted!r}")
    cursor = data
    for part in parts[:-1]:
        child = cursor.setdefault(part, {})
        if not isinstance(child, dict):
            raise state.StateError(
                f"cannot set {dotted!r}: {part!r} already contains a non-object value"
            )
        cursor = child
    cursor[parts[-1]] = value


# --------------------------------------------------------------------------- init


def cmd_init(args: argparse.Namespace) -> int:
    root = _root(args)
    if args.migrate:
        report = migrate_v2(root, args.legacy or root)
        print(report)
        return 0
    created = init_board(
        root,
        workflow=args.workflow,
        reviewer=args.reviewer,
        author=args.author,
        force=args.force,
    )
    if created is None:
        print(
            f"cordboard: {state.status_path(root)} already exists (use --force)",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    print(f"cordboard: board ready at {root}")
    print(roles.describe(created))
    return 0


# --------------------------------------------------------------------------- status


def cmd_status(args: argparse.Namespace) -> int:
    root = _root(args)
    data = state.load(root)

    if args.stalled:
        # For a backstop tick to report what it caught: exit 0 means the handoff
        # was missed and this wake is the one doing the work.
        return 0 if state.is_stalled(data) else EXIT_REFUSED
    if args.turn:
        return WAIT_TURN if roles.is_my_turn(data, args.turn) else EXIT_REFUSED
    if args.get:
        value = _dig(data, args.get)
        if value is None:
            return EXIT_REFUSED
        print(value if isinstance(value, str) else json.dumps(value))
        return 0
    if args.json:
        output = dict(data)
        output["_task_stack"] = tasks.summary(root)
        print(json.dumps(output, indent=2))
        return 0

    print(roles.describe(data))

    waited = state.waiting_seconds(data)
    if waited is not None and roles.owner(data):
        flag = "  ** STALLED **" if state.is_stalled(data) else ""
        print(f"  waiting {_duration(waited)} in this phase{flag}")

    mapping = data.get("roles") or {}
    for name in state.agent_names(data):
        block = state.agents(data)[name] or {}
        held = ",".join(sorted(r for r in ROLES if mapping.get(r) == name)) or "—"
        wakes = "watch" if block.get("watch") else "no-watch"
        if block.get("tick"):
            wakes += f"+tick({block['tick']})"
        print(
            f"  {name:<18} {block.get('status', '?'):<8} "
            f"{wakes:<26} roles={held:<16} "
            f"beat={block.get('heartbeat') or '—'}"
        )
    lock = data.get("lock") or {}
    if lock.get("holder"):
        stale = " (EXPIRED)" if state.lease_expired(data) else ""
        print(f"  lease held by {lock['holder']} until {lock.get('expires_at')}{stale}")
    stack = tasks.summary(root)
    next_id = (stack["next"] or {}).get("id") or "—"
    print(
        f"  queue pending={stack['pending']} completed={stack['completed']} next={next_id}"
    )
    return 0


# --------------------------------------------------------------------------- brief


def _safe_task_id(value: Any) -> str:
    cleaned = "".join(ch for ch in str(value or "task") if ch.isalnum() or ch in "-_.")
    return cleaned.strip(".") or "task"


def _gate(phase: str) -> str | None:
    if phase in {"plan_submitted", "plan_redlining", "plan_redlined"}:
        return "plan"
    if phase in {"impl_submitted", "impl_redlining", "impl_redlined"}:
        return "impl"
    return None


def _command(root: str, body: str) -> str:
    return f"cordboard --dir {shlex.quote(root)} {body}"


def _brief_disposition(
    data: dict[str, Any], agent: str, owner: str | None, phase: str
) -> tuple[str, str]:
    if not data.get("enabled"):
        return "disabled", "board is disabled"
    if phase == "idle":
        return "parked", "no task currently holds the baton"
    if phase == "blocked":
        return "human_required", "blocked task needs an operator decision"
    task = state.task(data)
    if phase in {"plan_redlined", "impl_redlined"} and (
        not task.get("last_redline") or not task.get("verdict")
    ):
        return "human_required", "review handoff is missing its verdict or redline path"
    if owner == agent:
        if phase in IN_PROGRESS:
            return "resume", f"{agent} resumes phase {phase}"
        return "act", f"{agent} owns phase {phase}"
    return "wait", f"waiting for {owner}"


def _brief_transitions(
    root: str, data: dict[str, Any], agent: str, task_view: dict[str, Any]
) -> list[str]:
    phase = task_view["phase"]
    flow = data["workflow"]
    who = shlex.quote(agent)
    commands: list[str] = []

    if flow == "relay" and phase in {"awaiting_plan", "planning"}:
        finish = "done" if task_view["kind"] == "plan_only" else "plan_ready"
        commands = [
            f"set --agent {who} --phase planning --claim --status working",
            f"set --agent {who} --key task.dir --value {shlex.quote(task_view['dir'])} --quiet",
            f"set --agent {who} --key task.plan_file "
            f"--value {shlex.quote(task_view['plan_file'])} --quiet",
            f"set --agent {who} --phase {finish} --release --status paused",
        ]
    elif flow == "relay" and phase in {"plan_ready", "implementing"}:
        commands = [
            f"set --agent {who} --phase implementing --claim --status working",
            f"set --agent {who} --phase awaiting_review --release --status paused",
        ]
    elif flow == "relay" and phase in {"awaiting_review", "reviewing"}:
        commands = [
            f"set --agent {who} --phase reviewing --claim --status working",
            f"set --agent {who} --phase done --release --status paused",
            f"set --agent {who} --phase plan_ready --release --status paused",
        ]
    elif flow == "redline" and phase in {"awaiting_draft", "drafting"}:
        commands = [
            f"set --agent {who} --phase drafting --claim --status working",
            f"set --agent {who} --key task.dir --value {shlex.quote(task_view['dir'])} --quiet",
            f"set --agent {who} --key task.plan_file "
            f"--value {shlex.quote(task_view['plan_file'])} --quiet",
            f"set --agent {who} --phase plan_submitted --release --status paused",
        ]
    elif flow == "redline" and phase in {
        "plan_submitted",
        "plan_redlining",
        "impl_submitted",
        "impl_redlining",
    }:
        gate = _gate(phase)
        assert gate is not None
        active = f"{gate}_redlining"
        finished = f"{gate}_redlined"
        commands = [f"set --agent {who} --phase {active} --claim --status working"]
        verdicts = ["approve", "revise"]
        if task_view["next_round"] < data["max_rounds"]:
            verdicts.append("reject")
        for verdict in verdicts:
            commands.append(
                f"set --agent {who} --phase {finished} --verdict {verdict} "
                f"--bump-round {gate} --key task.last_redline "
                f"--value {shlex.quote(task_view['redline_path'])} "
                "--release --status paused"
            )
    elif flow == "redline" and phase in {"plan_redlined", "impl_redlined"}:
        gate = _gate(phase)
        assert gate is not None
        commands = [f"set --agent {who} --phase {phase} --claim --status working"]
        if task_view["verdict"] == "reject":
            commands.append(
                f"set --agent {who} --phase {gate}_submitted --release --status paused"
            )
        else:
            target = "implementing" if gate == "plan" else "done"
            ending = " --release --status paused" if target == "done" else " --status working"
            commands.append(f"set --agent {who} --phase {target}{ending}")
    elif flow == "redline" and phase == "implementing":
        commands = [
            f"set --agent {who} --phase implementing --claim --status working",
            f"set --agent {who} --phase impl_submitted --release --status paused",
        ]
    elif phase == "done":
        commands = ["task complete --next"]
        if data.get("auto_commit"):
            commands.append(
                f"set --agent {who} --phase blocked --note "
                f"{shlex.quote('commit ownership is ambiguous; human scope decision required')} "
                "--status paused"
            )
    return [_command(root, body) for body in commands]


def _advance_commands(
    root: str, data: dict[str, Any], agent: str, phase: str, expected_rev: int
) -> list[str]:
    base = f"advance {shlex.quote(agent)} --rev {expected_rev}"
    if data["workflow"] == "relay" and phase == "reviewing":
        bodies = [f"{base} --outcome clean", f"{base} --outcome issues"]
    elif phase in {"plan_redlining", "impl_redlining"}:
        gate = "plan" if phase == "plan_redlining" else "impl"
        next_round = state.round_of(data, gate) + 1
        verdicts = ["approve", "revise"]
        if next_round < state.max_rounds(data):
            verdicts.append("reject")
        bodies = [f"{base} --verdict {verdict}" for verdict in verdicts]
    else:
        bodies = [base]
    return [_command(root, body) for body in bodies]


def _brief_payload(
    root: str,
    data: dict[str, Any],
    agent: str,
    unread: str,
    available_through: int,
    *,
    acquisition_mode: str | None = None,
) -> dict[str, Any]:
    state.validate(data)
    flow = roles.workflow(data)
    phase = state.phase(data)
    owner = roles.owner(data)
    disposition, reason = _brief_disposition(data, agent, owner, phase)
    assert disposition in BRIEF_DISPOSITIONS
    actionable = disposition in {"act", "resume"}
    raw_task = state.task(data)
    task_id = _safe_task_id(raw_task.get("id"))
    task_dir = raw_task.get("dir") or f"plans/{task_id}"
    plan_file = raw_task.get("plan_file") or f"{task_dir}/plan.md"
    gate = _gate(phase)
    next_round = state.round_of(data, gate) + 1 if gate else None
    pending_gate = phase in {
        "plan_submitted",
        "plan_redlining",
        "impl_submitted",
        "impl_redlining",
    }
    redline_path = (
        f"{task_dir}/redline-{gate}-r{next_round}.md"
        if gate and pending_gate
        else raw_task.get("last_redline")
    )
    notes = {
        name: str(block.get("note"))
        for name, block in state.agents(data).items()
        if isinstance(block, dict) and block.get("note")
    }
    task_view = {
        "id": raw_task.get("id"),
        "title": raw_task.get("title"),
        "kind": raw_task.get("kind"),
        "phase": phase,
        "dir": task_dir,
        "plan_file": plan_file,
        "last_redline": raw_task.get("last_redline"),
        "verdict": raw_task.get("verdict"),
        "round": raw_task.get("round"),
        "next_round": next_round if pending_gate else None,
        "redline_path": redline_path,
        "notes": notes,
    }
    instructions = list(BRIEF_GUIDANCE[flow][phase]["instructions"])
    if phase in {"awaiting_draft", "drafting", "awaiting_plan", "planning"}:
        instructions.append(
            f"Write {os.path.join(root, plan_file)} and set task.dir/task.plan_file as shown."
        )
        if flow == "relay" and task_view["kind"] == "plan_only":
            instructions.append("This is plan_only: finish at done without an author handoff.")
    if pending_gate:
        instructions.append(
            f"Write round {next_round} to {os.path.join(root, redline_path)} and choose one "
            "verdict transition."
        )
    if flow == "relay" and phase in {"awaiting_review", "reviewing"}:
        instructions.extend(
            (
                "Clean → done: after verification passes, use the done transition.",
                f"Issues → plan_ready: write a fix-plan at {os.path.join(root, plan_file)}, "
                "then use the plan_ready transition.",
            )
        )
    if phase == "done":
        if data.get("auto_commit"):
            instructions = [
                "Auto-commit is enabled: derive the candidate file set from the task plan and "
                "review artifacts, then reconcile it with the current git diff/status.",
                "Commit only when every included change is verified and unambiguously owned by "
                "this task; never infer ownership from an agent note or summary.",
                "If any changed file has ambiguous ownership, do not commit or complete the task; "
                "use the blocked transition to surface a human-required scope decision.",
                *instructions,
            ]
        else:
            instructions.insert(0, "Auto-commit is disabled: do not create a commit.")

    if not actionable:
        if disposition == "disabled":
            instructions = ["Do not act: the board is disabled; wait until an operator enables it."]
        elif disposition == "parked":
            instructions = ["Do not act: the board is parked with no active task."]
        elif disposition == "wait":
            instructions = [f"Do not act: {reason}; only the current owner may move this phase."]
        else:
            instructions = [f"Do not act: {reason}; ask an operator to resolve it."]
            if notes:
                instructions.append("Use task.notes as the operator-facing blocker context.")

    block = state.agents(data).get(agent) or {}
    transitions = _brief_transitions(root, data, agent, task_view) if actionable else []
    if acquisition_mode:
        assert acquisition_mode in TAKE_MODES
        normalized = []
        for item in instructions:
            if item.startswith("Claim the lease, "):
                item = item.removeprefix("Claim the lease, ")
                item = item[:1].upper() + item[1:]
            elif item.startswith("Claim the lease and "):
                item = item.removeprefix("Claim the lease and ")
                item = item[:1].upper() + item[1:]
            item = item.replace(", set its task metadata,", "")
            item = item.replace(" and set task.dir/task.plan_file as shown.", ".")
            if acquisition_mode == "started" and item.startswith("Resume "):
                item = "Start " + item.removeprefix("Resume ")
            normalized.append(item)
        instructions = normalized
        transitions = [
            command
            for command in transitions
            if "--claim" not in command
            and "--key task.dir" not in command
            and "--key task.plan_file" not in command
        ]
        transitions = _advance_commands(root, data, agent, phase, data["rev"] + 1)
    return {
        "board": {
            "enabled": data["enabled"],
            "workflow": flow,
            "rev": data["rev"],
            "auto_commit": data["auto_commit"],
            "max_rounds": data["max_rounds"],
        },
        "task": task_view,
        "turn": {
            "agent": agent,
            "owner": owner,
            "disposition": disposition,
            "actionable": actionable,
            "reason": reason,
        },
        "action": {"instructions": instructions, "transitions": transitions},
        "chat": {
            "unread": unread,
            "cursor": block.get("chat_cursor", 0),
            "available_through": available_through,
        },
    }


def _print_brief(payload: dict[str, Any]) -> None:
    board, task, turn = payload["board"], payload["task"], payload["turn"]
    print(
        f"agent={turn['agent']} disposition={turn['disposition']} "
        f"actionable={'yes' if turn['actionable'] else 'no'} reason={turn['reason']}"
    )
    print(
        f"workflow={board['workflow']} rev={board['rev']} task={task['id'] or '—'} "
        f"phase={task['phase']} owner={turn['owner'] or '—'}"
    )
    print(f"title={task['title'] or '—'} kind={task['kind'] or '—'}")
    context_fields = (
        "dir",
        "plan_file",
        "last_redline",
        "verdict",
        "round",
        "next_round",
        "redline_path",
    )
    for key in context_fields:
        value = task[key]
        if value is not None:
            print(f"  {key}={value if isinstance(value, str) else json.dumps(value)}")
    if task["notes"]:
        print("notes:")
        for name, note in task["notes"].items():
            print(f"  {name}: {note}")
    print("chat:")
    print(payload["chat"]["unread"] or "  (none)")
    print("instructions:")
    for item in payload["action"]["instructions"]:
        print(f"  - {item}")
    if payload["action"]["transitions"]:
        print("transitions:")
        for command in payload["action"]["transitions"]:
            print(f"  - {command}")


def cmd_brief(args: argparse.Namespace) -> int:
    root = _root(args)
    data = state.load(root)
    state.validate(data)
    tasks.read(root)
    unread, available_through = chat.unread(root, data, args.agent)
    if args.agent not in state.agents(data):
        print(f"cordboard: unknown agent {args.agent!r}", file=sys.stderr)
        return EXIT_REFUSED
    payload = _brief_payload(root, data, args.agent, unread, available_through)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_brief(payload)
    return 0


# --------------------------------------------------------------------------- take


def _take_envelope(
    *,
    mode: str | None = None,
    started: bool = False,
    resumed: bool = False,
    claimed: bool = False,
    reason: str,
    message: str,
    taken_from: str | None,
    rev: int,
    phase: str,
    lease_holder: str | None,
    lease_expires_at: str | None,
    brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "started": started,
        "resumed": resumed,
        "claimed": claimed,
        "reason": reason,
        "message": message,
        "taken_from": taken_from,
        "rev": rev,
        "phase": phase,
        "lease_holder": lease_holder,
        "lease_expires_at": lease_expires_at,
        "brief": brief,
    }


def _report_take(args: argparse.Namespace, payload: dict[str, Any], code: int) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
    elif code == 0:
        print(
            f"cordboard: {payload['mode']} {payload['brief']['task']['id']} "
            f"at phase={payload['phase']} rev={payload['rev']} "
            f"lease={payload['lease_holder']} until={payload['lease_expires_at']}"
        )
        _print_brief(payload["brief"])
    else:
        print(f"cordboard: {payload['message']}", file=sys.stderr)
    return code


def cmd_take(args: argparse.Namespace) -> int:
    """Atomically convert an actionable handoff into agent-owned work."""
    root = _root(args)
    initial = state.load(root)
    state.validate(initial)
    tasks.read(root)
    initial_unread, initial_available = chat.unread(root, initial, args.agent)
    refusal: dict[str, str | None] = {"reason": None, "message": None, "phase": None}
    outcome: dict[str, Any] = {}

    def refuse(reason: str, message: str, phase: str | None) -> bool:
        assert reason in TAKE_REFUSALS
        refusal.update({"reason": reason, "message": message, "phase": phase})
        return False

    def take(data: dict[str, Any]) -> Any:
        state.validate(data)
        current = state.phase(data)
        refusal["phase"] = current
        if args.agent not in state.agents(data):
            return refuse("unknown_agent", f"unknown agent {args.agent!r}", current)
        if not data.get("enabled"):
            return refuse("disabled", "board is disabled", current)
        if current == "idle":
            return refuse("parked", "no task currently holds the baton", current)
        if current == "blocked":
            return refuse("human_required", "blocked task needs an operator decision", current)
        if current == "done":
            return refuse(
                "bookkeeping",
                "phase done needs `cordboard task complete --next`, not a work lease",
                current,
            )
        owner = roles.owner(data)
        if owner != args.agent:
            return refuse("not_owner", f"waiting for {owner}", current)
        current_task = state.task(data)
        if not isinstance(current_task.get("id"), str) or not current_task["id"].strip():
            return refuse("missing_task", "active work needs a non-empty task id", current)
        disposition, why = _brief_disposition(data, args.agent, owner, current)
        if disposition == "human_required":
            return refuse("human_required", why, current)

        flow = roles.workflow(data)
        if current in IN_PROGRESS:
            target = current
            mode, started, resumed = "resumed", False, True
        elif current in TAKE_PHASES[flow]:
            target = TAKE_PHASES[flow][current]
            if target == current:
                mode, started, resumed = "claimed", False, False
            else:
                mode, started, resumed = "started", True, False
        else:
            return refuse(
                "unsupported_phase", f"phase {current} cannot be acquired with take", current
            )

        holder = (data.get("lock") or {}).get("holder")
        if holder and holder != args.agent and not state.lease_expired(data):
            return refuse("lease_held", f"lease is held by {holder!r}", current)
        allowed, why = roles.can_move(data, target)
        if not allowed:
            raise state.StateError(why)
        current_task["phase"] = target
        if target in {"planning", "drafting"}:
            task_dir = current_task.get("dir") or f"plans/{_safe_task_id(current_task['id'])}"
            current_task["dir"] = task_dir
            current_task["plan_file"] = current_task.get("plan_file") or f"{task_dir}/plan.md"
        state.claim_lease(data, args.agent)
        state.touch(data, args.agent, status="working")
        outcome.update(
            {
                "mode": mode,
                "started": started,
                "resumed": resumed,
                "taken_from": current,
                "brief": _brief_payload(
                    root,
                    data,
                    args.agent,
                    initial_unread,
                    initial_available,
                    acquisition_mode=mode,
                ),
            }
        )
        return None

    data, written = state.update(root, take, actor=args.agent)
    lock = data.get("lock") or {}
    if not written:
        payload = _take_envelope(
            reason=str(refusal["reason"] or "unsupported_phase"),
            message=str(refusal["message"] or "work could not be acquired"),
            taken_from=refusal["phase"],
            rev=data["rev"],
            phase=state.phase(data),
            lease_holder=lock.get("holder"),
            lease_expires_at=lock.get("expires_at"),
        )
        return _report_take(args, payload, EXIT_REFUSED)

    mode = str(outcome["mode"])
    assert mode in TAKE_MODES
    outcome["brief"]["board"]["rev"] = data["rev"]
    payload = _take_envelope(
        mode=mode,
        started=bool(outcome["started"]),
        resumed=bool(outcome["resumed"]),
        claimed=True,
        reason=mode,
        message=f"{mode} task {state.task(data)['id']}",
        taken_from=str(outcome["taken_from"]),
        rev=data["rev"],
        phase=state.phase(data),
        lease_holder=lock.get("holder"),
        lease_expires_at=lock.get("expires_at"),
        brief=outcome["brief"],
    )
    return _report_take(args, payload, 0)


# --------------------------------------------------------------------------- advance


def _validate_task_dir(root: str, candidate: str) -> tuple[str | None, str | None]:
    """Resolve the current task directory inside the board's plans directory."""
    plans = os.path.realpath(state.plans_dir(root))
    selected = candidate if os.path.isabs(candidate) else os.path.join(root, candidate)
    resolved = os.path.realpath(selected)
    try:
        contained = os.path.commonpath((plans, resolved)) == plans
    except ValueError:
        contained = False
    if not contained or resolved == plans:
        return None, f"task.dir {candidate!r} must be inside {state.plans_dir(root)}"
    if not os.path.exists(resolved):
        return None, f"task.dir does not exist: {candidate}"
    if not os.path.isdir(resolved):
        return None, f"task.dir is not a directory: {candidate}"
    return os.path.relpath(resolved, root), None


def _validate_artifact(
    root: str, task_dir: str, candidate: str
) -> tuple[str | None, str | None]:
    """Return evidence only when its resolved file stays in current task.dir."""
    boundary = os.path.realpath(os.path.join(root, task_dir))
    selected = candidate if os.path.isabs(candidate) else os.path.join(root, candidate)
    resolved = os.path.realpath(selected)
    try:
        contained = os.path.commonpath((boundary, resolved)) == boundary
    except ValueError:
        contained = False
    if not contained:
        return None, f"artifact {candidate!r} must be contained in task.dir {task_dir!r}"
    if not os.path.exists(resolved):
        return None, f"artifact does not exist: {candidate}"
    if not os.path.isfile(resolved):
        return None, f"artifact is not a regular file: {candidate}"
    try:
        with open(resolved, encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeError) as exc:
        return None, f"artifact is not readable UTF-8: {candidate}: {exc}"
    if not content:
        return None, f"artifact is empty: {candidate}"
    return os.path.relpath(resolved, root), None


def _advance_envelope(
    *,
    advanced: bool = False,
    mode: str | None = None,
    reason: str,
    message: str,
    expected_rev: int,
    rev: int,
    from_phase: str,
    phase: str,
    agent: str,
    next_owner: str | None,
    released: bool,
    lease_holder: str | None,
    lease_expires_at: str | None,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "advanced": advanced,
        "mode": mode,
        "reason": reason,
        "message": message,
        "expected_rev": expected_rev,
        "rev": rev,
        "from_phase": from_phase,
        "phase": phase,
        "agent": agent,
        "next_owner": next_owner,
        "released": released,
        "lease_holder": lease_holder,
        "lease_expires_at": lease_expires_at,
        "facts": facts or {},
    }


def _report_advance(args: argparse.Namespace, payload: dict[str, Any], code: int) -> int:
    if args.json:
        print(json.dumps(payload, indent=2))
    elif code == 0:
        print(
            f"cordboard: {payload['mode']} {payload['from_phase']} -> {payload['phase']} "
            f"rev={payload['rev']} next={payload['next_owner'] or '—'}"
        )
        facts = payload["facts"]
        if facts:
            print("facts=" + json.dumps(facts, sort_keys=True))
        if payload["released"]:
            print("lease=released")
        else:
            print(
                f"lease={payload['lease_holder']} until={payload['lease_expires_at']}"
            )
    else:
        print(f"cordboard: {payload['message']}", file=sys.stderr)
    return code


def cmd_advance(args: argparse.Namespace) -> int:
    """Atomically finish one already-taken work beat."""
    root = _root(args)
    refusal: dict[str, str | None] = {"reason": None, "message": None, "phase": None}
    outcome: dict[str, Any] = {}

    def refuse(reason: str, message: str, phase: str) -> bool:
        assert reason in ADVANCE_REFUSALS
        refusal.update({"reason": reason, "message": message, "phase": phase})
        return False

    def advance(data: dict[str, Any]) -> Any:
        state.validate(data)
        flow = roles.workflow(data)
        current = state.phase(data)
        refusal["phase"] = current
        if not data.get("enabled"):
            return refuse("disabled", "board is disabled", current)
        if args.agent not in state.agents(data):
            return refuse("unknown_agent", f"unknown agent {args.agent!r}", current)
        if data["rev"] != args.rev:
            return refuse(
                "revision_mismatch",
                f"expected rev {args.rev}, found {data['rev']}; read a fresh brief",
                current,
            )
        if current not in ADVANCE_PHASES[flow]:
            return refuse(
                "unsupported_phase",
                f"phase {current} is not active advance work; use brief/take first",
                current,
            )
        owner = roles.owner(data)
        if owner != args.agent:
            return refuse("not_owner", f"waiting for {owner}", current)
        task = state.task(data)
        if not isinstance(task.get("id"), str) or not task["id"].strip():
            return refuse("missing_task", "active work needs a non-empty task id", current)
        lock = data.get("lock") or {}
        holder = lock.get("holder")
        if not holder:
            return refuse("lease_missing", "active work has no lease; use take", current)
        if state.lease_expired(data):
            return refuse("lease_expired", "work lease expired; use take to recover", current)
        if holder != args.agent:
            return refuse("lease_foreign", f"lease is held by {holder!r}", current)

        gate = "plan" if current == "plan_redlining" else None
        if current == "impl_redlining":
            gate = "impl"
        reviewing_gate = gate is not None
        if reviewing_gate and not args.verdict:
            return refuse("verdict_required", f"phase {current} needs --verdict", current)
        if not reviewing_gate and args.verdict:
            return refuse(
                "verdict_not_allowed", f"--verdict is not valid in phase {current}", current
            )
        relay_review = flow == "relay" and current == "reviewing"
        if relay_review and not args.outcome:
            return refuse("outcome_required", "relay reviewing needs --outcome", current)
        if not relay_review and args.outcome:
            return refuse(
                "outcome_not_allowed", f"--outcome is not valid in phase {current}", current
            )
        if not reviewing_gate and args.artifact:
            return refuse(
                "artifact_not_allowed", f"--artifact is not valid in phase {current}", current
            )

        task_dir_candidate = task.get("dir") or f"plans/{_safe_task_id(task['id'])}"
        task_dir, artifact_error = _validate_task_dir(root, str(task_dir_candidate))
        if artifact_error:
            return refuse("artifact_invalid", artifact_error, current)
        assert task_dir is not None
        plan_candidate = task.get("plan_file") or f"{task_dir}/plan.md"
        plan_file, artifact_error = _validate_artifact(root, task_dir, str(plan_candidate))
        if artifact_error:
            return refuse("artifact_invalid", artifact_error, current)
        assert plan_file is not None

        verdict: str | None = None
        round_number: int | None = None
        redline_file: str | None = None
        if reviewing_gate:
            assert gate is not None and args.verdict is not None
            round_number = state.round_of(data, gate) + 1
            if args.verdict == "reject" and round_number >= state.max_rounds(data):
                return refuse(
                    "round_cap",
                    f"{gate} gate is at round {round_number} of {state.max_rounds(data)}",
                    current,
                )
            candidate = args.artifact or f"{task_dir}/redline-{gate}-r{round_number}.md"
            redline_file, artifact_error = _validate_artifact(root, task_dir, candidate)
            if artifact_error:
                return refuse("artifact_invalid", artifact_error, current)
            verdict = args.verdict
        elif current in {"plan_redlined", "impl_redlined"}:
            gate = "plan" if current == "plan_redlined" else "impl"
            verdict = task.get("verdict")
            stored = task.get("last_redline")
            if verdict not in VERDICTS or not stored:
                return refuse(
                    "review_metadata",
                    "response phase needs a stored verdict and last_redline",
                    current,
                )
            redline_file, artifact_error = _validate_artifact(root, task_dir, str(stored))
            if artifact_error:
                return refuse("artifact_invalid", artifact_error, current)
            round_number = state.round_of(data, gate)

        if flow == "relay":
            if current == "planning":
                target = "done" if task.get("kind") == "plan_only" else "plan_ready"
            elif current == "implementing":
                target = "awaiting_review"
            else:
                target = "done" if args.outcome == "clean" else "plan_ready"
        elif current == "drafting":
            target = "plan_submitted"
        elif current == "plan_redlining":
            target = "plan_redlined"
        elif current == "plan_redlined":
            target = "plan_submitted" if verdict == "reject" else "implementing"
        elif current == "implementing":
            target = "impl_submitted"
        elif current == "impl_redlining":
            target = "impl_redlined"
        else:
            target = "impl_submitted" if verdict == "reject" else "done"

        allowed, why = roles.can_move(data, target)
        if not allowed or target not in ADVANCE_PHASES[flow][current]:
            raise state.StateError(why or f"advance table forbids {current} -> {target}")

        task["dir"] = task_dir
        task["plan_file"] = plan_file
        if reviewing_gate:
            assert gate is not None and round_number is not None and redline_file is not None
            task.setdefault("round", {})[gate] = round_number
            task["last_redline"] = redline_file
            task["verdict"] = verdict
        task["phase"] = target

        target_role = PHASES[flow][target]
        next_owner = (data.get("roles") or {}).get(target_role) if target_role else None
        finished = target == "done"
        released = finished or next_owner != args.agent
        if released:
            state.release_lease(data, args.agent)
            state.touch(data, args.agent, status="paused")
        else:
            state.touch(data, args.agent, status="working")
        mode = "finished" if finished else "handed_off" if released else "continued"
        facts = {
            "plan_file": plan_file,
            "artifact": redline_file,
            "verdict": verdict,
            "round": round_number,
            "outcome": args.outcome,
        }
        outcome.update(
            {
                "mode": mode,
                "from_phase": current,
                "phase": target,
                "next_owner": next_owner,
                "released": released,
                "facts": facts,
            }
        )
        return None

    data, written = state.update(root, advance, actor=args.agent)
    lock = data.get("lock") or {}
    if not written:
        payload = _advance_envelope(
            reason=str(refusal["reason"] or "unsupported_phase"),
            message=str(refusal["message"] or "work could not be advanced"),
            expected_rev=args.rev,
            rev=data["rev"],
            from_phase=str(refusal["phase"] or state.phase(data)),
            phase=state.phase(data),
            agent=args.agent,
            next_owner=roles.owner(data),
            released=False,
            lease_holder=lock.get("holder"),
            lease_expires_at=lock.get("expires_at"),
        )
        return _report_advance(args, payload, EXIT_REFUSED)

    mode = str(outcome["mode"])
    assert mode in ADVANCE_MODES
    payload = _advance_envelope(
        advanced=True,
        mode=mode,
        reason="advanced",
        message=f"advanced {outcome['from_phase']} to {outcome['phase']}",
        expected_rev=args.rev,
        rev=data["rev"],
        from_phase=outcome["from_phase"],
        phase=outcome["phase"],
        agent=args.agent,
        next_owner=outcome["next_owner"],
        released=bool(outcome["released"]),
        lease_holder=lock.get("holder"),
        lease_expires_at=lock.get("expires_at"),
        facts=outcome["facts"],
    )
    return _report_advance(args, payload, 0)


# --------------------------------------------------------------------------- set


def cmd_set(args: argparse.Namespace) -> int:
    root = _root(args)
    problems: list[str] = []

    def mutate(data: dict[str, Any]) -> Any:
        if args.workflow:
            data["workflow"] = args.workflow
        if args.enabled is not None:
            data["enabled"] = args.enabled == "true"
        for pair in args.role or []:
            role, _, who = pair.partition("=")
            if role not in ROLES or not who:
                problems.append(f"--role expects <{'|'.join(ROLES)}>=<agent>, got {pair!r}")
                return False
            data.setdefault("roles", {})[role] = who
            data.setdefault("agents", {}).setdefault(who, _blank_agent())

        target = roles.entry_phase(data) if args.start else args.phase

        if target and not args.force:
            # Who may act, then what they may do. Skipping the second check is how
            # an agent with the baton walks past a review gate.
            if args.agent and not roles.is_my_turn(data, args.agent):
                problems.append(
                    f"not {args.agent}'s turn (phase={state.phase(data)}, "
                    f"owner={roles.owner(data) or '—'}); use --force to override"
                )
                return False
            # `--start` is the operator kicking off from a parked phase, which no
            # role owns; the turn check above cannot apply, but the graph still does.
            allowed, why = roles.can_move(data, target)
            if not allowed:
                problems.append(f"{why}; use --force to override")
                return False

        if args.verdict == "reject" and not args.force:
            gate = "plan" if "plan" in (target or state.phase(data)) else "impl"
            spent = state.round_of(data, gate) + (1 if args.bump_round == gate else 0)
            if spent >= state.max_rounds(data):
                problems.append(
                    f"{gate} gate is at round {spent} of {state.max_rounds(data)}; "
                    f"set --phase blocked instead of another reject"
                )
                return False

        if target:
            data.setdefault("task", {})["phase"] = target
        if args.verdict:
            data.setdefault("task", {})["verdict"] = args.verdict
        if args.bump_round:
            rounds = data.setdefault("task", {}).setdefault("round", {"plan": 0, "impl": 0})
            rounds[args.bump_round] = int(rounds.get(args.bump_round, 0)) + 1
        if args.watch is not None or args.tick is not None:
            if not args.agent:
                problems.append("--watch/--tick need --agent")
                return False
            block = data.setdefault("agents", {}).setdefault(args.agent, _blank_agent())
            if args.watch is not None:
                block["watch"] = args.watch
            if args.tick is not None:
                # "none" clears the backstop; anything else is the schedule.
                block["tick"] = None if args.tick.lower() == "none" else args.tick
            if not block.get("watch") and not block.get("tick"):
                problems.append(
                    f"{args.agent} would have no way to wake: turn on --watch or set --tick"
                )
                return False
        if args.agent:
            state.touch(data, args.agent, status=args.status, note=args.note)
        if args.claim:
            if not args.agent:
                problems.append("--claim needs --agent")
                return False
            state.claim_lease(data, args.agent, force=args.force)
        if args.release:
            state.release_lease(data, args.agent, force=args.force)
        if args.key:
            _assign(data, args.key, _coerce(args.value))
        return None

    try:
        data, written = state.update(root, mutate, actor=args.agent or "user")
    except state.StateError as exc:
        print(f"cordboard: {exc}", file=sys.stderr)
        return EXIT_BAD_STATE

    if not written:
        for problem in problems:
            print(f"cordboard: {problem}", file=sys.stderr)
        return EXIT_REFUSED
    if not args.quiet:
        print(roles.describe(data))
    return 0


def _blank_agent() -> dict[str, Any]:
    from .templates import blank_agent

    return blank_agent()


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# --------------------------------------------------------------------------- wait


def cmd_wait(args: argparse.Namespace) -> int:
    """Block until the baton is this agent's. The watcher polls; the agent blocks.

    Waiting therefore costs no tokens: no model turn runs until this exits.
    """
    root = _root(args)
    deadline = None if args.timeout <= 0 else time.monotonic() + args.timeout
    last = None

    while True:
        try:
            data = state.load(root)
        except state.StateError as exc:
            print(f"cordboard: {exc}", file=sys.stderr)
            return EXIT_BAD_STATE

        if not data.get("enabled"):
            if not args.quiet:
                print(f"cordboard: board disabled at rev {data.get('rev', 0)}")
            return WAIT_DISABLED

        try:
            mine = roles.is_my_turn(data, args.agent)
        except state.StateError as exc:
            # A bad phase or an unassigned role is an operator error, not something
            # to spin on until the timeout.
            print(f"cordboard: {exc}", file=sys.stderr)
            return EXIT_BAD_STATE

        if mine:
            if not args.quiet:
                print(
                    f"cordboard: your turn — phase={state.phase(data)} "
                    f"task={state.task(data).get('id') or '—'} rev={data.get('rev', 0)}"
                )
            return WAIT_TURN

        if not args.quiet:
            signature = (data.get("rev"), state.phase(data))
            if signature != last:
                last = signature
                print(
                    f"cordboard: holding — phase={signature[1]} "
                    f"owner={roles.owner(data) or '—'} rev={signature[0]}",
                    flush=True,
                )

        if deadline is not None and time.monotonic() >= deadline:
            if not args.quiet:
                print("cordboard: timed out")
            return WAIT_TIMEOUT

        nap = args.interval
        if deadline is not None:
            nap = min(nap, max(0.0, deadline - time.monotonic()))
        time.sleep(nap)


# --------------------------------------------------------------------------- say


def cmd_say(args: argparse.Namespace) -> int:
    root = _root(args)

    if args.read:
        text, _ = chat.consume(root, args.read, peek=args.peek)
        print(text or f"(no new chat for {args.read})")
        return 0

    if not (args.sender and args.to and args.body):
        print("cordboard: need SENDER TO BODY, or --read AGENT", file=sys.stderr)
        return EXIT_REFUSED

    data = state.load(root)
    task_id = args.task
    if task_id is None:
        task_id = state.task(data).get("id")
    print(chat.append(root, args.sender, args.to, args.body, task_id, args.ref))
    return 0


# --------------------------------------------------------------------------- task


def cmd_task(args: argparse.Namespace) -> int:
    root = _root(args)
    if args.action == "list":
        entries = tasks.read(root)
        next_up = tasks.next_pending(root)
        if args.json:
            print(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": item.id,
                                "title": item.title,
                                "done": item.done,
                                "line": item.line,
                                "next": bool(next_up and item.id == next_up.id),
                            }
                            for item in entries
                        ],
                        "summary": tasks.summary(root),
                    },
                    indent=2,
                )
            )
            return 0
        if not entries:
            print("(task stack is empty)")
            return 0
        for item in entries:
            marker = "x" if item.done else " "
            suffix = "  ← next" if next_up and item.id == next_up.id else ""
            print(f"- [{marker}] {item.id} — {item.title}{suffix}")
        return 0

    if args.action == "complete":
        data = state.load(root)
        current = state.task(data)
        if state.phase(data) != "done" or not current.get("id"):
            print("cordboard: only an active task in phase=done can be completed", file=sys.stderr)
            return EXIT_REFUSED
        changed = tasks.complete(root, str(current["id"]))
        word = "completed" if changed else "already completed"
        print(f"cordboard: {word} {current['id']}")
        if args.next and tasks.next_pending(root) is not None:
            return _start_next_task(root)
        if args.next:
            print("cordboard: task stack is empty")
        _park_completed_task(root, str(current["id"]))
        return 0

    return _start_next_task(root)


def _start_next_task(root: str) -> int:
    problems: list[str] = []

    def start(data: dict[str, Any]) -> Any:
        current = state.phase(data)
        if current not in {"idle", "done"}:
            problems.append(f"cannot start a task while phase={current}; finish or park it first")
            return False
        item = tasks.next_pending(root)
        if item is None:
            problems.append("no pending task in TODO.md")
            return False
        data["task"] = {
            "id": item.id,
            "title": item.title,
            "kind": "implement",
            "phase": roles.entry_phase(data),
            "dir": None,
            "plan_file": None,
            "last_redline": None,
            "verdict": None,
            "handoff_at": None,
            "round": {"plan": 0, "impl": 0},
        }
        return None

    data, written = state.update(root, start, actor="user")
    if not written:
        for problem in problems:
            print(f"cordboard: {problem}", file=sys.stderr)
        return EXIT_REFUSED
    current = state.task(data)
    print(f"cordboard: started {current['id']} — {current['title']}")
    return 0


def _park_completed_task(root: str, task_id: str) -> None:
    """Leave an enabled board quiet when completion did not start a successor."""
    def park(data: dict[str, Any]) -> Any:
        current = state.task(data)
        if state.phase(data) != "done" or current.get("id") != task_id:
            raise state.StateError(f"task {task_id!r} changed while it was being completed")
        current["phase"] = "idle"
        return None

    state.update(root, park, actor="user")


# --------------------------------------------------------------------------- spec


def cmd_spec(args: argparse.Namespace) -> int:
    """Print the packaged spec. This is what an agent reads at the top of a tick."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "data", "instructions.md"), encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
    return 0


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    root = _root(args)
    data = state.load(root)
    problems = state.validation_errors(data)
    try:
        tasks.read(root)
    except state.StateError as exc:
        problems.append(str(exc))
    if problems:
        print(f"cordboard: found {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_BAD_STATE
    print(
        f"cordboard: healthy — schema v{data['version']}, rev {data['rev']}, "
        f"{len(tasks.read(root))} task(s)"
    )
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cordboard",
        description="Pass work between coding agents with a file-based baton.",
    )
    parser.add_argument("--dir", help=f"board directory (default ./{state.DIR_NAME})")

    # --dir again on every subcommand, so both `cordboard --dir X set` and
    # `cordboard set --dir X` work. SUPPRESS matters: without it the subparser
    # writes its own None over a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("init", parents=[common], help="create a board, or migrate a legacy one")
    p.add_argument("--workflow", choices=sorted(PHASES), default="redline")
    p.add_argument("--reviewer", default="reviewer", help="agent name for the reviewer role")
    p.add_argument("--author", default="author", help="agent name for the author role")
    p.add_argument("--migrate", action="store_true", help="import a v2 collab/ board")
    p.add_argument("--legacy", help="path to the v2 board (default: --dir)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = subs.add_parser("status", parents=[common], help="read the board; never writes")
    p.add_argument("--json", action="store_true")
    p.add_argument("--get", metavar="PATH", help="print one dotted key")
    p.add_argument("--turn", metavar="AGENT", help="exit 0 if it is AGENT's turn")
    p.add_argument(
        "--stalled",
        action="store_true",
        help="exit 0 if the baton has waited past stall_after_seconds (a missed handoff)",
    )
    p.set_defaults(func=cmd_status)

    p = subs.add_parser(
        "brief", parents=[common], help="show one agent its bounded, read-only handoff"
    )
    p.add_argument("agent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_brief)

    p = subs.add_parser("take", parents=[common], help="atomically acquire actionable work")
    p.add_argument("agent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_take)

    p = subs.add_parser("advance", parents=[common], help="finish one already-taken work beat")
    p.add_argument("agent")
    p.add_argument("--rev", type=int, required=True, help="revision returned by take/brief")
    p.add_argument("--verdict", choices=VERDICTS)
    p.add_argument("--outcome", choices=("clean", "issues"))
    p.add_argument("--artifact", help="redline path, relative to the board or absolute")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_advance)

    p = subs.add_parser("set", parents=[common], help="move the baton or change config")
    p.add_argument("--agent", help="who is acting")
    p.add_argument("--phase", help="new task.phase")
    p.add_argument(
        "--start",
        action="store_true",
        help="move to this workflow's entry phase (how an operator kicks off)",
    )
    p.add_argument("--status", choices=("working", "paused"))
    p.add_argument("--note")
    p.add_argument("--verdict", choices=VERDICTS)
    p.add_argument("--bump-round", choices=("plan", "impl"), dest="bump_round")
    lease = p.add_mutually_exclusive_group()
    lease.add_argument("--claim", action="store_true", help="take the work lease")
    lease.add_argument("--release", action="store_true", help="drop the work lease")
    p.add_argument("--enabled", choices=("true", "false"))
    p.add_argument("--workflow", choices=sorted(PHASES))
    p.add_argument("--role", action="append", metavar="ROLE=AGENT", help="assign a role")
    p.add_argument(
        "--watch",
        dest="watch",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="for --agent: react to handoffs via `cordboard wait` (the default)",
    )
    p.add_argument(
        "--tick",
        help="for --agent: backstop schedule, or `none` to drop it",
    )
    p.add_argument("--key", help="dotted path for an arbitrary set")
    p.add_argument("--value", help="value for --key")
    p.add_argument("--force", action="store_true", help="bypass the guards")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_set)

    p = subs.add_parser("wait", parents=[common], help="block until it is AGENT's turn")
    p.add_argument("agent")
    p.add_argument("--timeout", type=float, default=3600.0, help="seconds (0 = forever)")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_wait)

    p = subs.add_parser("say", parents=[common], help="append to or read the back-channel")
    p.add_argument("sender", nargs="?")
    p.add_argument("to", nargs="?")
    p.add_argument("body", nargs="?")
    p.add_argument("--re", dest="ref", help="redline comment id, e.g. C3")
    p.add_argument("--task")
    p.add_argument("--read", metavar="AGENT")
    p.add_argument("--peek", action="store_true", help="with --read: leave the cursor")
    p.set_defaults(func=cmd_say)

    p = subs.add_parser("task", parents=[common], help="inspect or advance the TODO stack")
    p.add_argument("action", choices=("list", "start", "complete"))
    p.add_argument("--next", action="store_true", help="with complete: start the next task")
    p.add_argument("--json", action="store_true", help="with list: emit structured output")
    p.set_defaults(func=cmd_task)

    p = subs.add_parser("spec", parents=[common], help="print the protocol spec agents follow")
    p.set_defaults(func=cmd_spec)

    p = subs.add_parser("doctor", parents=[common], help="validate board state and the TODO stack")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "set" and args.key and args.value is None:
        print("cordboard: --key needs --value", file=sys.stderr)
        return EXIT_REFUSED
    if args.command == "set" and not _set_has_action(args):
        print("cordboard: set needs an action", file=sys.stderr)
        return EXIT_REFUSED
    if args.command == "wait" and (args.timeout < 0 or args.interval <= 0):
        print("cordboard: wait needs --timeout >= 0 and --interval > 0", file=sys.stderr)
        return EXIT_REFUSED
    if args.command == "say":
        if args.read and any((args.sender, args.to, args.body)):
            print("cordboard: --read cannot be combined with a message", file=sys.stderr)
            return EXIT_REFUSED
        if args.peek and not args.read:
            print("cordboard: --peek needs --read", file=sys.stderr)
            return EXIT_REFUSED
    if args.command == "task" and args.next and args.action != "complete":
        print("cordboard: --next is only valid with task complete", file=sys.stderr)
        return EXIT_REFUSED
    if args.command == "task" and args.json and args.action != "list":
        print("cordboard: --json is only valid with task list", file=sys.stderr)
        return EXIT_REFUSED
    try:
        return int(args.func(args))
    except state.StateError as exc:
        print(f"cordboard: {exc}", file=sys.stderr)
        return EXIT_BAD_STATE
    except OSError as exc:
        detail = f"{exc.strerror or str(exc)}"
        if exc.filename:
            detail += f": {exc.filename}"
        print(f"cordboard: {detail}", file=sys.stderr)
        return EXIT_BAD_STATE
    except KeyboardInterrupt:
        return 130


def _set_has_action(args: argparse.Namespace) -> bool:
    """Whether `set` will do more than bump the revision."""
    return any(
        (
            args.agent,
            args.phase,
            args.start,
            args.status,
            args.note is not None,
            args.verdict,
            args.bump_round,
            args.claim,
            args.release,
            args.enabled is not None,
            args.workflow,
            args.role,
            args.watch is not None,
            args.tick is not None,
            args.key,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
