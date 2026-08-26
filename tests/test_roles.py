"""Role mapping — including the reason `mode` no longer exists."""

from __future__ import annotations

import pytest

from cordboard import roles, state
from cordboard.templates import init_board


def test_phases_resolve_to_the_mapped_agents(board):
    state.update(board, lambda d: d["task"].update({"phase": "awaiting_draft"}))
    assert roles.owner(state.load(board)) == "ag"  # author drafts in redline

    state.update(board, lambda d: d["task"].update({"phase": "plan_submitted"}))
    assert roles.owner(state.load(board)) == "cc"


def test_relay_gives_the_plan_to_the_reviewer(relay_board):
    """The one difference between the workflows: who holds the pen for the plan."""
    state.update(relay_board, lambda d: d["task"].update({"phase": "awaiting_plan"}))
    assert roles.owner(state.load(relay_board)) == "cc"

    state.update(relay_board, lambda d: d["task"].update({"phase": "plan_ready"}))
    assert roles.owner(state.load(relay_board)) == "ag"


def test_both_roles_on_one_agent_is_solo(tmp_path):
    """Solo needs no setting: it is what one name in both roles means."""
    root = str(tmp_path / "cordboard")
    init_board(root, workflow="redline", reviewer="solo", author="solo")
    state.update(root, lambda d: d.update({"enabled": True}))
    data = state.load(root)

    assert roles.is_solo(data) is True
    for phase in ("awaiting_draft", "plan_submitted", "implementing", "impl_submitted"):
        data["task"]["phase"] = phase
        assert roles.owner(data) == "solo"


def test_parked_phases_belong_to_nobody(board):
    for phase in ("idle", "blocked"):
        state.update(board, lambda d, p=phase: d["task"].update({"phase": p}))
        data = state.load(board)
        assert roles.owner(data) is None
        assert roles.is_my_turn(data, "cc") is False
        assert roles.is_my_turn(data, "ag") is False


def test_disabled_board_is_nobody_s_turn(board):
    """What makes the pause switch safe: no agent can act while it is off."""
    def disable_mid_task(data):
        data["enabled"] = False
        data["task"]["phase"] = "awaiting_draft"

    state.update(board, disable_mid_task)
    data = state.load(board)
    assert roles.owner(data) == "ag"  # the phase still has an owner...
    assert roles.is_my_turn(data, "ag") is False  # ...but nobody may act


def test_unassigned_role_is_an_error_not_a_default(board):
    """Guessing an agent name here would hand the baton to a session nobody armed."""
    damaged = state.load(board)
    damaged["roles"].pop("author")
    state.write_atomic(board, damaged)
    with pytest.raises(state.StateError, match="unassigned"):
        roles.role_map(state.load(board))


def test_unknown_phase_is_rejected(board):
    damaged = state.load(board)
    damaged["task"]["phase"] = "awaiting_plan"  # relay-only
    state.write_atomic(board, damaged)
    with pytest.raises(state.StateError, match="not part of"):
        roles.owner(state.load(board))
