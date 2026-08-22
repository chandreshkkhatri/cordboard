"""The transition graph — what an agent holding the baton may actually do."""

from __future__ import annotations

from conftest import move

from cordboard import roles, state

REDLINE_CYCLE = [
    ("ag", "drafting"),
    ("ag", "plan_submitted"),
    ("cc", "plan_redlining"),
    ("cc", "plan_redlined"),
    ("ag", "implementing"),
    ("ag", "impl_submitted"),
    ("cc", "impl_redlining"),
    ("cc", "impl_redlined"),
    ("ag", "done"),
    ("ag", "awaiting_draft"),
]


def test_full_redline_cycle(board):
    move(board, None, "awaiting_draft", force=True)
    for agent, phase in REDLINE_CYCLE:
        assert move(board, agent, phase), f"{agent} could not reach {phase}"


def test_full_relay_cycle(relay_board):
    move(relay_board, None, "awaiting_plan", force=True)
    for agent, phase in [
        ("cc", "planning"),
        ("cc", "plan_ready"),
        ("ag", "implementing"),
        ("ag", "awaiting_review"),
        ("cc", "reviewing"),
        ("cc", "done"),
        ("cc", "awaiting_plan"),
    ]:
        assert move(relay_board, agent, phase), f"{agent} could not reach {phase}"


def test_cannot_skip_the_plan_gate(board):
    """The whole point of the graph: holding the baton is not permission to skip review."""
    move(board, None, "drafting", force=True)
    assert not move(board, "ag", "impl_submitted")
    assert not move(board, "ag", "implementing")
    assert move(board, "ag", "plan_submitted")


def test_cannot_act_out_of_turn(board):
    move(board, None, "awaiting_draft", force=True)
    assert not move(board, "cc", "drafting")  # author's phase
    assert move(board, "ag", "drafting")


def test_reviewer_cannot_finish_the_task(board):
    move(board, None, "impl_redlining", force=True)
    assert not move(board, "cc", "done")
    assert move(board, "cc", "impl_redlined")


def test_blocked_is_reachable_from_anywhere(board):
    for phase in ("drafting", "plan_submitted", "implementing"):
        move(board, None, phase, force=True)
        data = state.load(board)
        allowed, _ = roles.can_move(data, "blocked")
        assert allowed, f"{phase} should be able to bail out"


def test_leaving_blocked_needs_a_human(board):
    """No role owns `blocked`, so an agent cannot quietly un-block itself."""
    move(board, None, "blocked", force=True)
    assert not move(board, "ag", "awaiting_draft")
    assert not move(board, "cc", "awaiting_draft")
    assert move(board, None, "awaiting_draft", force=True)


def test_re_entering_a_phase_is_a_watchdog_resume(board):
    move(board, None, "drafting", force=True)
    allowed, _ = roles.can_move(state.load(board), "drafting")
    assert allowed


def test_refusal_names_the_legal_moves(board):
    move(board, None, "drafting", force=True)
    allowed, why = roles.can_move(state.load(board), "done")
    assert not allowed
    assert "plan_submitted" in why  # the message has to be actionable
