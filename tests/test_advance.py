"""Semantic, revision-bound completion of one taken work beat."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cordboard import state
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main
from cordboard.workflows import ADVANCE_MODES, ADVANCE_PHASES, ADVANCE_REFUSALS, TRANSITIONS

ENVELOPE_KEYS = {
    "advanced",
    "mode",
    "reason",
    "message",
    "expected_rev",
    "rev",
    "from_phase",
    "phase",
    "agent",
    "next_owner",
    "released",
    "lease_holder",
    "lease_expires_at",
    "facts",
}


def prepare(
    root: str,
    phase: str,
    agent: str,
    *,
    kind: str = "implement",
    verdict: str | None = None,
    round_plan: int = 0,
    round_impl: int = 0,
) -> int:
    data = state.load(root)
    task_dir = Path(root) / "plans" / "advance"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "plan.md").write_text("# Plan\n\nVerify the result.\n")
    data["task"].update(
        {
            "id": "advance",
            "title": "Advance active work",
            "kind": kind,
            "phase": phase,
            "dir": "plans/advance",
            "plan_file": "plans/advance/plan.md",
            "round": {"plan": round_plan, "impl": round_impl},
            "verdict": verdict,
        }
    )
    if phase in {"plan_redlining", "impl_redlining"}:
        gate = "plan" if phase == "plan_redlining" else "impl"
        number = data["task"]["round"][gate] + 1
        (task_dir / f"redline-{gate}-r{number}.md").write_text("# Redline\n\nEvidence.\n")
    if phase in {"plan_redlined", "impl_redlined"}:
        gate = "plan" if phase == "plan_redlined" else "impl"
        artifact = task_dir / f"redline-{gate}-r1.md"
        artifact.write_text("# Redline\n\nEvidence.\n")
        data["task"]["last_redline"] = f"plans/advance/{artifact.name}"
        data["task"]["verdict"] = verdict or "revise"
        data["task"]["round"][gate] = max(1, data["task"]["round"][gate])
    state.claim_lease(data, agent)
    state.write_atomic(root, data)
    return data["rev"]


def advance_json(
    root: str,
    agent: str,
    rev: int,
    capsys,
    *extra: str,
    expected: int = 0,
) -> dict:
    argv = ["--dir", root, "advance", agent, "--rev", str(rev), *extra, "--json"]
    assert main(argv) == expected
    captured = capsys.readouterr()
    assert captured.err == ""
    output = json.loads(captured.out)
    assert set(output) == ENVELOPE_KEYS
    return output


@pytest.mark.parametrize(
    ("fixture_name", "phase", "agent", "extra", "target", "mode"),
    (
        ("relay_board", "planning", "cc", (), "plan_ready", "handed_off"),
        ("relay_board", "planning", "cc", (), "done", "finished"),
        ("relay_board", "implementing", "ag", (), "awaiting_review", "handed_off"),
        ("relay_board", "reviewing", "cc", ("--outcome", "clean"), "done", "finished"),
        (
            "relay_board",
            "reviewing",
            "cc",
            ("--outcome", "issues"),
            "plan_ready",
            "handed_off",
        ),
        ("board", "drafting", "ag", (), "plan_submitted", "handed_off"),
        (
            "board",
            "plan_redlining",
            "cc",
            ("--verdict", "revise"),
            "plan_redlined",
            "handed_off",
        ),
        ("board", "plan_redlined", "ag", (), "implementing", "continued"),
        ("board", "plan_redlined", "ag", (), "plan_submitted", "handed_off"),
        ("board", "implementing", "ag", (), "impl_submitted", "handed_off"),
        (
            "board",
            "impl_redlining",
            "cc",
            ("--verdict", "approve"),
            "impl_redlined",
            "handed_off",
        ),
        ("board", "impl_redlined", "ag", (), "done", "finished"),
        ("board", "impl_redlined", "ag", (), "impl_submitted", "handed_off"),
    ),
)
def test_advance_covers_every_phase_and_branch(
    request, fixture_name, phase, agent, extra, target, mode, capsys
):
    root = request.getfixturevalue(fixture_name)
    kind = "plan_only" if phase == "planning" and target == "done" else "implement"
    verdict = "reject" if target in {"plan_submitted", "impl_submitted"} else "revise"
    rev = prepare(root, phase, agent, kind=kind, verdict=verdict)
    result = advance_json(root, agent, rev, capsys, *extra)
    after = state.load(root)

    assert result["advanced"] is True
    assert result["mode"] == mode
    assert result["mode"] in ADVANCE_MODES
    assert result["from_phase"] == phase
    assert result["phase"] == target == state.phase(after)
    assert result["rev"] == rev + 1 == after["rev"]
    assert result["facts"]["plan_file"] == "plans/advance/plan.md"
    event = state.load(root)["history"]["events"][-1]
    assert event["rev"] == result["rev"]
    assert event["action"] == "advance"
    assert event["from_phase"] == phase
    assert event["phase"] == target
    assert event["mode"] == mode
    if mode == "continued":
        assert result["released"] is False
        assert result["lease_holder"] == agent
    else:
        assert result["released"] is True
        assert result["lease_holder"] is None


def test_review_gate_updates_round_verdict_and_path_atomically(board, capsys):
    rev = prepare(board, "plan_redlining", "cc", round_plan=1)
    result = advance_json(board, "cc", rev, capsys, "--verdict", "approve")
    task = state.task(state.load(board))
    assert result["facts"] == {
        "plan_file": "plans/advance/plan.md",
        "artifact": "plans/advance/redline-plan-r2.md",
        "verdict": "approve",
        "round": 2,
        "outcome": None,
    }
    assert task["round"]["plan"] == 2
    assert task["verdict"] == "approve"
    assert task["last_redline"] == "plans/advance/redline-plan-r2.md"


@pytest.mark.parametrize("gate", ("plan", "impl"))
@pytest.mark.parametrize("verdict", ("approve", "revise", "reject"))
def test_every_verdict_is_recorded_at_both_review_gates(board, capsys, gate, verdict):
    phase = f"{gate}_redlining"
    rev = prepare(board, phase, "cc")
    result = advance_json(board, "cc", rev, capsys, "--verdict", verdict)
    task = state.task(state.load(board))
    assert result["phase"] == f"{gate}_redlined"
    assert result["facts"]["verdict"] == verdict
    assert result["facts"]["round"] == 1
    assert task["verdict"] == verdict
    assert task["round"][gate] == 1
    assert task["last_redline"] == f"plans/advance/redline-{gate}-r1.md"


def test_optional_contained_artifact_is_normalized_and_recorded(board, capsys):
    rev = prepare(board, "plan_redlining", "cc")
    custom = Path(board) / "plans" / "advance" / "custom-review.md"
    custom.write_text("# Custom review\n")
    result = advance_json(
        board, "cc", rev, capsys, "--verdict", "revise", "--artifact", str(custom)
    )
    assert result["facts"]["artifact"] == "plans/advance/custom-review.md"
    assert state.task(state.load(board))["last_redline"] == "plans/advance/custom-review.md"


@pytest.mark.parametrize(
    ("case", "reason", "extra"),
    (
        ("unknown", "unknown_agent", ()),
        ("disabled", "disabled", ()),
        ("stale", "revision_mismatch", ()),
        ("stable_phase", "unsupported_phase", ()),
        ("nonowner", "not_owner", ()),
        ("missing_task", "missing_task", ()),
        ("missing_lease", "lease_missing", ()),
        ("expired_lease", "lease_expired", ()),
        ("foreign_lease", "lease_foreign", ()),
        ("verdict_required", "verdict_required", ()),
        ("verdict_extra", "verdict_not_allowed", ("--verdict", "revise")),
        ("outcome_required", "outcome_required", ()),
        ("outcome_extra", "outcome_not_allowed", ("--outcome", "clean")),
        ("artifact_extra", "artifact_not_allowed", ("--artifact", "plans/x.md")),
        ("metadata", "review_metadata", ()),
    ),
)
def test_advance_refusals_are_stable_and_write_nothing(board, capsys, case, reason, extra):
    phase, agent = "drafting", "ag"
    if case == "verdict_required":
        phase, agent = "plan_redlining", "cc"
    elif case == "outcome_required":
        phase, agent = "reviewing", "cc"
        root = board
        data = state.load(root)
        data["workflow"] = "relay"
        state.write_atomic(root, data)
    elif case == "metadata":
        phase = "plan_redlined"
    rev = prepare(board, phase, agent)
    data = state.load(board)
    requested_agent = agent
    requested_rev = rev
    if case == "unknown":
        requested_agent = "stranger"
    elif case == "disabled":
        data["enabled"] = False
    elif case == "stale":
        requested_rev -= 1
    elif case == "stable_phase":
        data["task"]["phase"] = "awaiting_draft"
    elif case == "nonowner":
        requested_agent = "cc" if agent == "ag" else "ag"
    elif case == "missing_task":
        data["task"]["id"] = None
    elif case == "missing_lease":
        state.release_lease(data, agent)
    elif case == "expired_lease":
        data["lock"]["expires_at"] = "2020-01-01T00:00:00Z"
    elif case == "foreign_lease":
        data["lock"]["holder"] = "cc" if agent == "ag" else "ag"
    elif case == "metadata":
        data["task"].update({"last_redline": None, "verdict": None})
    state.write_atomic(board, data)
    before = state.load(board)
    result = advance_json(
        board, requested_agent, requested_rev, capsys, *extra, expected=EXIT_REFUSED
    )
    assert result["advanced"] is False
    assert result["mode"] is None
    assert result["reason"] == reason
    assert reason in ADVANCE_REFUSALS
    assert state.load(board) == before


@pytest.mark.parametrize("damage", ("missing", "directory", "empty", "encoding", "outside"))
def test_invalid_artifact_refuses_without_state_change(board, tmp_path, capsys, damage):
    rev = prepare(board, "plan_redlining", "cc")
    candidate = Path(board) / "plans" / "advance" / "custom.md"
    if damage == "directory":
        candidate.mkdir()
    elif damage == "empty":
        candidate.write_text("")
    elif damage == "encoding":
        candidate.write_bytes(b"\xff")
    elif damage == "outside":
        candidate = tmp_path / "outside.md"
        candidate.write_text("outside")
    before = state.load(board)
    result = advance_json(
        board,
        "cc",
        rev,
        capsys,
        "--verdict",
        "revise",
        "--artifact",
        str(candidate),
        expected=EXIT_REFUSED,
    )
    assert result["reason"] == "artifact_invalid"
    assert state.load(board) == before


@pytest.mark.parametrize(
    "damage",
    (
        "task_dir_outside",
        "plan_cross_task",
        "redline_cross_task",
        "plan_symlink",
        "redline_symlink",
    ),
)
def test_evidence_cannot_escape_resolved_current_task_dir(
    board, tmp_path, capsys, damage
):
    rev = prepare(board, "plan_redlining", "cc")
    root = Path(board)
    other = root / "plans" / "other-task"
    other.mkdir()
    (other / "plan.md").write_text("# Other plan\n")
    (other / "redline.md").write_text("# Other redline\n")
    data = state.load(board)
    extra = ["--verdict", "revise"]
    if damage == "task_dir_outside":
        outside = tmp_path / "outside-task"
        outside.mkdir()
        (outside / "plan.md").write_text("# Outside plan\n")
        data["task"]["dir"] = str(outside)
        data["task"]["plan_file"] = str(outside / "plan.md")
    elif damage == "plan_cross_task":
        data["task"]["plan_file"] = "plans/other-task/plan.md"
    elif damage == "redline_cross_task":
        extra.extend(("--artifact", "plans/other-task/redline.md"))
    elif damage == "plan_symlink":
        linked = root / "plans" / "advance" / "linked-plan.md"
        linked.symlink_to(other / "plan.md")
        data["task"]["plan_file"] = "plans/advance/linked-plan.md"
    else:
        linked = root / "plans" / "advance" / "linked-redline.md"
        linked.symlink_to(other / "redline.md")
        extra.extend(("--artifact", "plans/advance/linked-redline.md"))
    state.write_atomic(board, data)
    before = state.load(board)
    result = advance_json(
        board, "cc", rev, capsys, *extra, expected=EXIT_REFUSED
    )
    assert result["reason"] == "artifact_invalid"
    assert state.load(board) == before


def test_missing_plan_file_refuses_every_phase_before_write(board, capsys):
    rev = prepare(board, "implementing", "ag")
    Path(board, "plans", "advance", "plan.md").unlink()
    before = state.load(board)
    result = advance_json(board, "ag", rev, capsys, expected=EXIT_REFUSED)
    assert result["reason"] == "artifact_invalid"
    assert state.load(board) == before


def test_reject_at_round_cap_is_refused(board, capsys):
    rev = prepare(board, "plan_redlining", "cc", round_plan=2)
    before = state.load(board)
    result = advance_json(
        board, "cc", rev, capsys, "--verdict", "reject", expected=EXIT_REFUSED
    )
    assert result["reason"] == "round_cap"
    assert state.load(board) == before


def test_expected_revision_makes_retry_safe(board, capsys):
    rev = prepare(board, "drafting", "ag")
    first = advance_json(board, "ag", rev, capsys)
    after = state.load(board)
    second = advance_json(board, "ag", rev, capsys, expected=EXIT_REFUSED)
    assert first["advanced"] is True
    assert second["reason"] == "revision_mismatch"
    assert state.load(board) == after


def test_lost_success_response_is_retry_safe_for_solo_continuation(relay_board, capsys):
    data = state.load(relay_board)
    data["roles"] = {"reviewer": "ag", "author": "ag"}
    state.write_atomic(relay_board, data)
    rev = prepare(relay_board, "planning", "ag")
    first = advance_json(relay_board, "ag", rev, capsys)
    after = state.load(relay_board)
    retry = advance_json(
        relay_board, "ag", rev, capsys, expected=EXIT_REFUSED
    )
    assert first["mode"] == "continued"
    assert first["phase"] == "plan_ready"
    assert retry["reason"] == "revision_mismatch"
    assert state.load(relay_board) == after


def test_advance_stamps_handoff_time(board, capsys):
    rev = prepare(board, "drafting", "ag")
    data = state.load(board)
    data["task"]["handoff_at"] = "2020-01-01T00:00:00Z"
    state.write_atomic(board, data)
    advance_json(board, "ag", rev, capsys)
    after = state.load(board)
    assert after["task"]["handoff_at"] != "2020-01-01T00:00:00Z"
    assert after["task"]["handoff_at"]


def test_same_owner_solo_continuation_retains_lease(relay_board, capsys):
    data = state.load(relay_board)
    data["roles"] = {"reviewer": "ag", "author": "ag"}
    state.write_atomic(relay_board, data)
    rev = prepare(relay_board, "planning", "ag")
    result = advance_json(relay_board, "ag", rev, capsys)
    assert result["mode"] == "continued"
    assert result["released"] is False
    assert result["lease_holder"] == "ag"


def test_human_output_contains_transition_facts(board, capsys):
    rev = prepare(board, "drafting", "ag")
    assert main(["--dir", board, "advance", "ag", "--rev", str(rev)]) == 0
    output = capsys.readouterr().out
    assert "handed_off drafting -> plan_submitted" in output
    assert "facts=" in output and "plans/advance/plan.md" in output
    assert "lease=released" in output


def test_malformed_board_exits_two_without_partial_output(board, capsys):
    data = state.load(board)
    data["rev"] = "malformed"
    state.write_atomic(board, data)
    assert (
        main(["--dir", board, "advance", "ag", "--rev", "0", "--json"])
        == EXIT_BAD_STATE
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid board" in captured.err


def test_advance_does_not_touch_todo_chat_notes_or_artifacts(board, capsys):
    rev = prepare(board, "drafting", "ag")
    root = Path(board)
    data = state.load(board)
    data["agents"]["ag"]["note"] = "preserve me"
    state.write_atomic(board, data)
    todo = (root / "TODO.md").read_bytes()
    plan = (root / "plans" / "advance" / "plan.md").read_bytes()
    chat_path = Path(state.chat_path(board))
    chat_path.write_text("preserve chat\n")
    advance_json(board, "ag", rev, capsys)
    after = state.load(board)
    assert after["agents"]["ag"]["note"] == "preserve me"
    assert (root / "TODO.md").read_bytes() == todo
    assert (root / "plans" / "advance" / "plan.md").read_bytes() == plan
    assert chat_path.read_text() == "preserve chat\n"


def test_concurrent_same_revision_only_advances_once(board):
    rev = prepare(board, "drafting", "ag")
    source = str(Path(__file__).parents[1] / "src")
    script = (
        "import sys;"
        f"sys.path.insert(0, {source!r});"
        "from cordboard.cli import main;"
        f"raise SystemExit(main(['--dir', {board!r}, 'advance', 'ag', "
        f"'--rev', {str(rev)!r}, '--json']))"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    results = [(process.wait(), json.loads(process.stdout.read())) for process in processes]
    assert sorted(code for code, _ in results) == [0, EXIT_REFUSED]
    assert sum(payload["advanced"] for _, payload in results) == 1
    assert state.load(board)["rev"] == rev + 1


def test_advance_table_only_contains_legal_transitions():
    for workflow, phases in ADVANCE_PHASES.items():
        for source, targets in phases.items():
            assert set(targets) <= TRANSITIONS[workflow][source]
