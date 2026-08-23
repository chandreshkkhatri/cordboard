"""The `cordboard` command.

One entry point with subcommands, rather than the five loose scripts this grew
out of: an agent's tick prompt is easier to write against `cordboard status` than
against a path it has to know.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from . import chat, roles, state
from .templates import init_board, migrate_v2
from .workflows import PHASES, ROLES, VERDICTS

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
    cursor = data
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
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
        print(json.dumps(data, indent=2))
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
    return 0


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
            state.claim_lease(data, args.agent)
        if args.release:
            state.release_lease(data)
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
        data = state.load(root)
        text, cursor = chat.unread(root, data, args.read)
        print(text or f"(no new chat for {args.read})")
        if not args.peek:
            state.update(
                root,
                lambda d: d.setdefault("agents", {})
                .setdefault(args.read, _blank_agent())
                .update({"chat_cursor": cursor}),
                actor=args.read,
            )
        return 0

    if not (args.sender and args.to and args.body):
        print("cordboard: need SENDER TO BODY, or --read AGENT", file=sys.stderr)
        return EXIT_REFUSED

    task_id = args.task
    if task_id is None:
        try:
            task_id = state.task(state.load(root)).get("id")
        except state.StateError:
            task_id = None
    print(chat.append(root, args.sender, args.to, args.body, task_id, args.ref))
    return 0


# --------------------------------------------------------------------------- spec


def cmd_spec(args: argparse.Namespace) -> int:
    """Print the packaged spec. This is what an agent reads at the top of a tick."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "data", "instructions.md"), encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
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
    p.add_argument("--claim", action="store_true", help="take the work lease")
    p.add_argument("--release", action="store_true", help="drop the work lease")
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

    p = subs.add_parser("spec", parents=[common], help="print the protocol spec agents follow")
    p.set_defaults(func=cmd_spec)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "set" and args.key and args.value is None:
        print("cordboard: --key needs --value", file=sys.stderr)
        return EXIT_REFUSED
    try:
        return int(args.func(args))
    except state.StateError as exc:
        print(f"cordboard: {exc}", file=sys.stderr)
        return EXIT_BAD_STATE
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
