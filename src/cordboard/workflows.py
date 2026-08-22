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
# finished commits, pops the task off the stack, and queues the next one.
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
