"""The command surface an agent's tick prompt is written against."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_an_agent_cannot_steal_or_release_a_live_lease(board):
    main(["--dir", board, "set", "--start", "--quiet"])
    assert main(["--dir", board, "set", "--agent", "ag", "--claim", "--quiet"]) == 0

    before = state.load(board)["rev"]
    assert main(["--dir", board, "set", "--agent", "cc", "--claim", "--quiet"]) == EXIT_BAD_STATE
    assert main(["--dir", board, "set", "--agent", "cc", "--release", "--quiet"]) == EXIT_BAD_STATE
    data = state.load(board)
    assert data["rev"] == before
    assert data["lock"]["holder"] == "ag"


def test_expired_lease_can_be_recovered_and_force_is_explicit(board):
    state.update(board, lambda d: state.claim_lease(d, "ag"))
    state.update(board, lambda d: d["lock"].update({"expires_at": "2020-01-01T00:00:00Z"}))
    assert main(["--dir", board, "set", "--agent", "cc", "--claim", "--quiet"]) == 0
    assert state.load(board)["lock"]["holder"] == "cc"

    assert main(
        ["--dir", board, "set", "--agent", "ag", "--claim", "--force", "--quiet"]
    ) == 0
    assert state.load(board)["lock"]["holder"] == "ag"


def test_describe_reports_who_is_next(board, capsys):
    main(["--dir", board, "set", "--start", "--quiet"])
    main(["--dir", board, "status"])
    out = capsys.readouterr().out
    assert "next=ag" in out
    assert "dual" in out
    assert "queue pending=0 completed=0 next=—" in out


def test_json_status_includes_a_computed_task_stack_without_persisting_it(board, capsys):
    path = Path(state.stack_path(board))
    path.write_text(path.read_text() + "\n- [ ] queued — Queued work\n")
    assert main(["--dir", board, "status", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["_task_stack"]["next"]["id"] == "queued"
    assert "_task_stack" not in state.load(board)


def test_dir_works_before_and_after_the_subcommand(board):
    """Both are natural to type, so both must work."""
    assert main(["--dir", board, "set", "--start", "--quiet"]) == 0
    assert main(["set", "--dir", board, "--enabled", "true", "--quiet"]) == 0
    assert state.load(board)["enabled"] is True


def test_noop_set_is_refused_without_bumping_revision(board):
    before = state.load(board)["rev"]
    assert main(["--dir", board, "set", "--quiet"]) == EXIT_REFUSED
    assert state.load(board)["rev"] == before


def test_dotted_set_reports_a_scalar_path_conflict(board, capsys):
    before = state.load(board)["rev"]
    assert main(
        ["--dir", board, "set", "--key", "workflow.name", "--value", "redline", "--quiet"]
    ) == EXIT_BAD_STATE
    assert "non-object" in capsys.readouterr().err
    assert state.load(board)["rev"] == before


def test_invalid_wait_bounds_are_refused_immediately(board):
    assert main(["--dir", board, "wait", "cc", "--interval", "0"]) == EXIT_REFUSED
    assert main(["--dir", board, "wait", "cc", "--timeout", "-1"]) == EXIT_REFUSED


def test_say_on_a_missing_board_is_a_stable_error(tmp_path, capsys):
    code = main(["--dir", str(tmp_path / "missing"), "say", "cc", "ag", "hello"])
    assert code == EXIT_BAD_STATE
    assert "run `cordboard init`" in capsys.readouterr().err


def test_say_modes_and_task_next_are_not_ambiguous(board):
    assert main(["--dir", board, "say", "cc", "ag", "hello", "--read", "cc"]) == EXIT_REFUSED
    assert main(["--dir", board, "say", "--peek"]) == EXIT_REFUSED
    assert main(["--dir", board, "task", "list", "--next"]) == EXIT_REFUSED
