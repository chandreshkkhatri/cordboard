"""Board validation prevents corrupt writes and diagnoses existing damage."""

from __future__ import annotations

from pathlib import Path

from cordboard import state
from cordboard.cli import EXIT_BAD_STATE, main


def test_doctor_accepts_a_fresh_board(board, capsys):
    assert main(["--dir", board, "doctor"]) == 0
    assert "healthy" in capsys.readouterr().out


def test_invalid_arbitrary_set_is_not_committed(board):
    before = state.load(board)["rev"]
    assert main(
        ["--dir", board, "set", "--key", "lease_seconds", "--value", "0", "--quiet"]
    ) == EXIT_BAD_STATE
    data = state.load(board)
    assert data["rev"] == before
    assert data["lease_seconds"] > 0


def test_doctor_reports_all_existing_state_problems(board, capsys):
    damaged = state.load(board)
    damaged["workflow"] = "mystery"
    damaged["roles"].pop("author")
    damaged["agents"]["cc"]["watch"] = False
    damaged["agents"]["cc"]["tick"] = None
    state.write_atomic(board, damaged)

    assert main(["--dir", board, "doctor"]) == EXIT_BAD_STATE
    error = capsys.readouterr().err
    assert "workflow" in error
    assert "roles.author" in error
    assert "cannot wake" in error


def test_doctor_includes_stack_errors(board, capsys):
    path = Path(state.stack_path(board))
    path.write_text(path.read_text() + "\n- [ ] malformed\n")
    assert main(["--dir", board, "doctor"]) == EXIT_BAD_STATE
    assert "invalid task" in capsys.readouterr().err
