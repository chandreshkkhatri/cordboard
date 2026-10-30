"""The phase graphs. Data only — no IO, no policy.

A workflow answers two questions: which **role** owns each phase, and which
phases may legally follow it. Naming an agent here would be the mistake the whole
package exists to avoid, so phases are owned by roles and roles are mapped to
agents in config.
"""

from __future__ import annotations

ROLES = ("reviewer", "author")

VERDICTS = ("approve", "revise", "reject")

# Phase -> role that must act. ``None`` means nobody acts: `blocked` and `idle`
# are the two states only a human moves the baton out of.
#
# ``done`` is *not* terminal. It is the bookkeeping beat where the agent that
# finished commits, checks off the task, and queues the next one.
#
# An "in progress" phase is owned by the role that entered it, so an agent that
# died mid-phase resumes its own work instead of handing over something half done.
PHASES: dict[str, dict[str, str | None]] = {
    # Each agent owns a different leg: the reviewer plans and audits, the author
    # builds. The reviewer holding the pen for the plan is what separates this
    # from `redline`.
    "relay": {
        "awaiting_plan": "reviewer",
        "planning": "reviewer",
        "plan_ready": "author",
        "implementing": "author",
        "awaiting_review": "reviewer",
        "reviewing": "reviewer",
        "done": "reviewer",
        "blocked": None,
        "idle": None,
    },
    # The author holds the pen for everything; the reviewer produces nothing but
    # commentary, at two gates.
    "redline": {
        "awaiting_draft": "author",
        "drafting": "author",
        "plan_submitted": "reviewer",
        "plan_redlining": "reviewer",
        "plan_redlined": "author",
        "implementing": "author",
        "impl_submitted": "reviewer",
        "impl_redlining": "reviewer",
        "impl_redlined": "author",
        "done": "author",
        "blocked": None,
        "idle": None,
    },
}

# Legal successors per phase. PHASES says *who* may act; this says *what* they may
# do. Without it an agent holding the baton could skip a review gate outright —
# jump `drafting -> impl_submitted` and never be reviewed.
TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "relay": {
        "idle": {"awaiting_plan"},
        "awaiting_plan": {"planning"},
        "planning": {"plan_ready", "done"},
        "plan_ready": {"implementing"},
        "implementing": {"awaiting_review"},
        "awaiting_review": {"reviewing"},
        "reviewing": {"done", "plan_ready", "awaiting_plan", "idle"},
        "done": {"awaiting_plan", "idle"},
        "blocked": {"awaiting_plan", "idle"},
    },
    "redline": {
        "idle": {"awaiting_draft"},
        "awaiting_draft": {"drafting"},
        "drafting": {"plan_submitted"},
        "plan_submitted": {"plan_redlining"},
        "plan_redlining": {"plan_redlined"},
        "plan_redlined": {"implementing", "plan_submitted"},
        "implementing": {"impl_submitted"},
        "impl_submitted": {"impl_redlining"},
        "impl_redlining": {"impl_redlined"},
        "impl_redlined": {"done", "impl_submitted"},
        "done": {"awaiting_draft", "idle"},
        "blocked": {"awaiting_draft", "idle"},
    },
}

# Phases an agent is actively mid-work in, for lease and watchdog handling.
IN_PROGRESS = frozenset(
    {
        "planning",
        "reviewing",
        "implementing",
        "drafting",
        "plan_redlining",
        "impl_redlining",
    }
)

# Where each workflow picks up a fresh task.
ENTRY_PHASE = {"relay": "awaiting_plan", "redline": "awaiting_draft"}

NAMES = tuple(PHASES)

# Agent-facing outcomes, in precedence order. Wrappers branch on these instead
# of learning phase ownership and watchdog policy themselves.
BRIEF_DISPOSITIONS = (
    "disabled",
    "parked",
    "human_required",
    "act",
    "resume",
    "wait",
)

# Stable handoff -> actively held phase. In-progress phases resume in place;
# redlined phases are claimed in place because their verdict selects the exit.
TAKE_PHASES: dict[str, dict[str, str]] = {
    "relay": {
        "awaiting_plan": "planning",
        "plan_ready": "implementing",
        "awaiting_review": "reviewing",
    },
    "redline": {
        "awaiting_draft": "drafting",
        "plan_submitted": "plan_redlining",
        "plan_redlined": "plan_redlined",
        "impl_submitted": "impl_redlining",
        "impl_redlined": "impl_redlined",
    },
}

TAKE_MODES = ("started", "resumed", "claimed")

# A normal wake observes passive state or performs the same acquisition as take.
WORK_MODES = ("observed", *TAKE_MODES)

TAKE_REFUSALS = (
    "unknown_agent",
    "disabled",
    "parked",
    "human_required",
    "bookkeeping",
    "not_owner",
    "missing_task",
    "lease_held",
    "unsupported_phase",
)

# Active phase -> every semantic successor `advance` may select. Dynamic
# selection (task kind, verdict, or relay review outcome) stays in the CLI, but
# the complete surface remains code-owned and testable against TRANSITIONS.
ADVANCE_PHASES: dict[str, dict[str, tuple[str, ...]]] = {
    "relay": {
        "planning": ("plan_ready", "done"),
        "implementing": ("awaiting_review",),
        "reviewing": ("done", "plan_ready"),
    },
    "redline": {
        "drafting": ("plan_submitted",),
        "plan_redlining": ("plan_redlined",),
        "plan_redlined": ("implementing", "plan_submitted"),
        "implementing": ("impl_submitted",),
        "impl_redlining": ("impl_redlined",),
        "impl_redlined": ("done", "impl_submitted"),
    },
}

ADVANCE_MODES = ("handed_off", "continued", "finished")

# Every phase that can be renewed already represents acquired work and can be
# completed by ``advance``. This includes the claimed-in-place response phases.
RENEW_PHASES = tuple(
    dict.fromkeys(phase for mapping in ADVANCE_PHASES.values() for phase in mapping)
)

RENEW_REFUSALS = (
    "unknown_agent",
    "disabled",
    "revision_mismatch",
    "not_owner",
    "missing_task",
    "unsupported_phase",
    "lease_missing",
    "lease_expired",
    "lease_foreign",
)

ADVANCE_REFUSALS = (
    "unknown_agent",
    "disabled",
    "revision_mismatch",
    "unsupported_phase",
    "not_owner",
    "missing_task",
    "lease_missing",
    "lease_expired",
    "lease_foreign",
    "verdict_required",
    "verdict_not_allowed",
    "outcome_required",
    "outcome_not_allowed",
    "artifact_not_allowed",
    "review_metadata",
    "round_cap",
    "artifact_invalid",
)

FINISH_REFUSALS = (
    "unknown_agent",
    "disabled",
    "revision_mismatch",
    "not_done",
    "not_owner",
    "missing_task",
    "task_not_in_todo",
)


def _brief(*instructions: str, fields: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    return {"instructions": instructions, "task_fields": fields}


# The spec explains the workflows; this bounded table tells an agent what work
# the current phase means. Transition commands are rendered from the phase graph
# and current metadata by the CLI, so they remain executable rather than examples.
BRIEF_GUIDANCE = {
    "relay": {
        "awaiting_plan": _brief(
            "Claim the lease and write an implementable plan with verification steps.",
            fields=("dir", "plan_file"),
        ),
        "planning": _brief(
            "Resume the plan and finish its implementation and verification instructions.",
            fields=("dir", "plan_file"),
        ),
        "plan_ready": _brief(
            "Claim the lease, implement the plan, and run its verification.",
            fields=("plan_file",),
        ),
        "implementing": _brief(
            "Resume implementation and verification, then submit the scoped diff.",
            fields=("plan_file",),
        ),
        "awaiting_review": _brief(
            "Claim the lease, audit the scoped diff, and run the plan's verification.",
            fields=("plan_file",),
        ),
        "reviewing": _brief(
            "Resume the audit; finish clean work or return a fix-plan for issues.",
            fields=("plan_file",),
        ),
        "done": _brief("Finish task bookkeeping and advance or park the queue."),
        "blocked": _brief("Do not act; an operator must resolve the recorded blocker."),
        "idle": _brief("Do not act; no task currently holds the baton."),
    },
    "redline": {
        "awaiting_draft": _brief(
            "Claim the lease and write an implementable plan with verification steps.",
            fields=("dir", "plan_file"),
        ),
        "drafting": _brief(
            "Resume the plan, set its task metadata, and submit it for redline.",
            fields=("dir", "plan_file"),
        ),
        "plan_submitted": _brief(
            "Claim the lease and redline the plan against the relevant code.",
            fields=("dir", "plan_file", "last_redline", "verdict", "round"),
        ),
        "plan_redlining": _brief(
            "Resume the plan redline and return exactly one recorded verdict.",
            fields=("dir", "plan_file", "last_redline", "verdict", "round"),
        ),
        "plan_redlined": _brief(
            "Claim the lease, read the latest redline, and answer every comment in the plan.",
            fields=("plan_file", "last_redline", "verdict", "round"),
        ),
        "implementing": _brief(
            "Implement the approved plan, run verification, and submit the scoped diff.",
            fields=("plan_file",),
        ),
        "impl_submitted": _brief(
            "Claim the lease and redline the scoped diff against the plan.",
            fields=("dir", "plan_file", "last_redline", "verdict", "round"),
        ),
        "impl_redlining": _brief(
            "Resume the implementation redline and return exactly one recorded verdict.",
            fields=("dir", "plan_file", "last_redline", "verdict", "round"),
        ),
        "impl_redlined": _brief(
            "Claim the lease, read the latest redline, and apply every accepted comment.",
            fields=("plan_file", "last_redline", "verdict", "round"),
        ),
        "done": _brief("Finish task bookkeeping and advance or park the queue."),
        "blocked": _brief("Do not act; an operator must resolve the recorded blocker."),
        "idle": _brief("Do not act; no task currently holds the baton."),
    },
}
