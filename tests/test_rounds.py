"""The round cap — what stops a `reject` verdict ping-ponging forever."""

from __future__ import annotations

from cordboard import state
from cordboard.cli import main


def test_reject_is_refused_at_the_cap(board):
    for _ in range(3):  # walk the plan gate up to its cap
        code = main(
            [
                "--dir", board, "set", "--force",
                "--phase", "plan_redlined", "--verdict", "reject", "--bump-round", "plan",
                "--quiet",
            ]
        )
        assert code == 0  # --force bypasses, so bump the rounds up first
    # Now at the cap, an unforced reject must be refused.
    main(["--dir", board, "set", "--force", "--phase", "plan_redlining", "--quiet"])
    assert state.round_of(state.load(board), "plan") >= 3
    code = main(
        ["--dir", board, "set", "--agent", "cc", "--phase", "plan_redlined",
         "--verdict", "reject", "--quiet"]
    )
    assert code == 1


def test_revise_is_never_capped(board):
    """`revise` means "apply and carry on", so it cannot loop and needs no cap."""
    state.update(board, lambda d: d["task"].update({"round": {"plan": 9, "impl": 9}}))
    main(["--dir", board, "set", "--force", "--phase", "plan_redlining", "--quiet"])
    code = main(
        ["--dir", board, "set", "--agent", "cc", "--phase", "plan_redlined",
         "--verdict", "revise", "--quiet"]
    )
    assert code == 0


def test_gate_is_chosen_by_the_target_phase(board):
    """A plan-gate reject must not be capped by the impl gate's count, or vice versa."""
    state.update(board, lambda d: d["task"].update({"round": {"plan": 0, "impl": 9}}))
    main(["--dir", board, "set", "--force", "--phase", "plan_redlining", "--quiet"])
    code = main(
        ["--dir", board, "set", "--agent", "cc", "--phase", "plan_redlined",
         "--verdict", "reject", "--quiet"]
    )
    assert code == 0
