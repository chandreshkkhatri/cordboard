"""The command surface an agent's tick prompt is written against."""

from __future__ import annotations

from cordboard import state
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main


def test_start_kicks_off_without_force(board, capsys):
    """Leaving `idle` is the normal start path, so it must not need the override.

    `--force` should mean "I am overriding a safety check", never "I am starting".
    """
    assert state.phase(state.load(board)) == "idle"
    assert main(["--dir", board, "set", "--start", "--quiet"]) == 0
    assert state.phase(state.load(board)) == "awaiting_draft"


def test_start_uses_the_workflow_s_own_entry_phase(relay_board):
    main(["--dir", relay_board, "set", "--start", "--quiet"])
    assert state.phase(state.load(relay_board)) == "awaiting_plan"


def test_turn_check_is_scriptable(board):
    main(["--dir", board, "set", "--start", "--quiet"])
    assert main(["--dir", board, "status", "--turn", "ag"]) == 0
    assert main(["--dir", board, "status", "--turn", "cc"]) == EXIT_REFUSED


def test_get_reads_one_key(board, capsys):
    main(["--dir", board, "set", "--start", "--quiet"])
    assert main(["--dir", board, "status", "--get", "task.phase"]) == 0
    assert capsys.readouterr().out.strip() == "awaiting_draft"


def test_role_assignment_registers_the_agent(board):
    assert main(["--dir", board, "set", "--role", "author=fresh-agent", "--quiet"]) == 0
    data = state.load(board)
    assert data["roles"]["author"] == "fresh-agent"
    assert "fresh-agent" in state.agents(data)


def test_bad_role_syntax_is_refused(board):
    assert main(["--dir", board, "set", "--role", "nonsense", "--quiet"]) == EXIT_REFUSED


def test_status_on_a_missing_board_is_exit_2(tmp_path):
    assert main(["--dir", str(tmp_path / "nope"), "status"]) == EXIT_BAD_STATE


def test_claim_and_release_the_lease(board):
    main(["--dir", board, "set", "--start", "--quiet"])
    main(["--dir", board, "set", "--agent", "ag", "--phase", "drafting", "--claim", "--quiet"])
    data = state.load(board)
    assert data["lock"]["holder"] == "ag"
    assert state.lease_expired(data) is False

    main(
        ["--dir", board, "set", "--agent", "ag", "--phase", "plan_submitted",
         "--release", "--quiet"]
    )
    assert state.load(board)["lock"]["holder"] is None


def test_describe_reports_who_is_next(board, capsys):
    main(["--dir", board, "set", "--start", "--quiet"])
    main(["--dir", board, "status"])
    out = capsys.readouterr().out
    assert "next=ag" in out
    assert "dual" in out


def test_dir_works_before_and_after_the_subcommand(board):
    """Both are natural to type, so both must work."""
    assert main(["--dir", board, "set", "--start", "--quiet"]) == 0
    assert main(["set", "--dir", board, "--enabled", "true", "--quiet"]) == 0
    assert state.load(board)["enabled"] is True
