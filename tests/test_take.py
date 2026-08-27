"""Atomic acquisition of actionable work."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import cordboard.cli as cli
from cordboard import state
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main
from cordboard.workflows import (
    IN_PROGRESS,
    TAKE_MODES,
    TAKE_PHASES,
    TAKE_REFUSALS,
    TRANSITIONS,
)

ENVELOPE_KEYS = {
    "mode",
    "started",
    "resumed",
    "claimed",
    "reason",
    "message",
    "taken_from",
    "rev",
    "phase",
    "lease_holder",
    "lease_expires_at",
    "brief",
}


def take_json(root: str, agent: str, capsys, expected: int = 0) -> dict:
    assert main(["--dir", root, "take", agent, "--json"]) == expected
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert set(result) == ENVELOPE_KEYS
    return result


@pytest.mark.parametrize(
    ("fixture_name", "phase", "agent", "target", "mode"),
    (
        ("relay_board", "awaiting_plan", "cc", "planning", "started"),
        ("relay_board", "plan_ready", "ag", "implementing", "started"),
        ("relay_board", "awaiting_review", "cc", "reviewing", "started"),
        ("board", "awaiting_draft", "ag", "drafting", "started"),
        ("board", "plan_submitted", "cc", "plan_redlining", "started"),
        ("board", "plan_redlined", "ag", "plan_redlined", "claimed"),
        ("board", "impl_submitted", "cc", "impl_redlining", "started"),
        ("board", "impl_redlined", "ag", "impl_redlined", "claimed"),
    ),
)
def test_take_acquires_every_stable_work_handoff(
    request, fixture_name, phase, agent, target, mode, capsys
):
    root = request.getfixturevalue(fixture_name)
    data = state.load(root)
    data["task"].update(
        {
            "id": "take-work",
            "title": "Acquire work",
            "phase": phase,
            "last_redline": "plans/take-work/redline.md",
            "verdict": "revise",
        }
    )
    state.write_atomic(root, data)
    before_rev = data["rev"]
    result = take_json(root, agent, capsys)
    after = state.load(root)

    assert result["mode"] == mode
    assert result["mode"] in TAKE_MODES
    assert result["started"] is (mode == "started")
    assert result["resumed"] is False
    assert result["claimed"] is True
    assert result["taken_from"] == phase
    assert result["phase"] == target
    assert result["rev"] == before_rev + 1 == after["rev"]
    assert result["lease_holder"] == agent == after["lock"]["holder"]
    assert result["lease_expires_at"] == after["lock"]["expires_at"]
    assert result["brief"]["board"]["rev"] == after["rev"]
    assert result["brief"]["task"]["phase"] == target
    assert result["brief"]["action"]["instructions"]
    assert all(
        "Claim the lease" not in item
        for item in result["brief"]["action"]["instructions"]
    )
    assert all(
        "task.dir" not in item and "task.plan_file" not in item
        for item in result["brief"]["action"]["instructions"]
    )
    assert all(
        "--claim" not in command
        for command in result["brief"]["action"]["transitions"]
    )


@pytest.mark.parametrize(
    ("fixture_name", "phase", "agent"),
    (
        ("relay_board", "planning", "cc"),
        ("relay_board", "implementing", "ag"),
        ("relay_board", "reviewing", "cc"),
        ("board", "drafting", "ag"),
        ("board", "plan_redlining", "cc"),
        ("board", "implementing", "ag"),
        ("board", "impl_redlining", "cc"),
    ),
)
def test_take_resumes_every_in_progress_phase(request, fixture_name, phase, agent, capsys):
    assert phase in IN_PROGRESS
    root = request.getfixturevalue(fixture_name)
    data = state.load(root)
    data["task"].update({"id": "resume", "phase": phase})
    state.write_atomic(root, data)
    result = take_json(root, agent, capsys)
    assert result["mode"] == "resumed"
    assert result["started"] is False
    assert result["resumed"] is True
    assert result["claimed"] is True
    assert result["phase"] == phase
    assert all(
        "task.dir" not in item and "task.plan_file" not in item
        for item in result["brief"]["action"]["instructions"]
    )


@pytest.mark.parametrize(
    ("fixture_name", "phase", "agent", "target"),
    (
        ("board", "awaiting_draft", "ag", "drafting"),
        ("relay_board", "awaiting_plan", "cc", "planning"),
    ),
)
def test_started_planning_guidance_starts_work_without_resume(
    request, fixture_name, phase, agent, target, capsys
):
    root = request.getfixturevalue(fixture_name)
    data = state.load(root)
    data["task"].update({"id": "start-wording", "phase": phase})
    state.write_atomic(root, data)
    result = take_json(root, agent, capsys)
    instructions = result["brief"]["action"]["instructions"]
    transitions = result["brief"]["action"]["transitions"]
    assert result["mode"] == "started"
    assert result["phase"] == target
    assert any(item.startswith("Start ") for item in instructions)
    assert not any("Resume" in item for item in instructions)
    assert not any("task.dir" in item or "task.plan_file" in item for item in instructions)
    assert transitions == [
        f"cordboard --dir {root} advance {agent} --rev {result['rev']}"
    ]


@pytest.mark.parametrize(
    ("fixture_name", "phase", "agent"),
    (("board", "drafting", "ag"), ("relay_board", "planning", "cc")),
)
def test_true_resume_preserves_resume_wording(request, fixture_name, phase, agent, capsys):
    root = request.getfixturevalue(fixture_name)
    data = state.load(root)
    data["task"].update({"id": "resume-wording", "phase": phase})
    state.write_atomic(root, data)
    result = take_json(root, agent, capsys)
    instructions = result["brief"]["action"]["instructions"]
    assert result["mode"] == "resumed"
    assert any(item.startswith("Resume ") for item in instructions)
    assert not any("task.dir" in item or "task.plan_file" in item for item in instructions)


def test_take_sets_planning_defaults_without_creating_work_files(board, capsys):
    root = Path(board)
    data = state.load(board)
    data["task"].update({"id": "defaults", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    todo = (root / "TODO.md").read_bytes()
    names = {path.name for path in root.iterdir()}
    cursor = data["agents"]["ag"]["chat_cursor"]
    result = take_json(board, "ag", capsys)
    after = state.load(board)

    assert after["task"]["dir"] == "plans/defaults"
    assert after["task"]["plan_file"] == "plans/defaults/plan.md"
    assert result["brief"]["task"]["plan_file"] == "plans/defaults/plan.md"
    assert not (root / "plans" / "defaults").exists()
    assert (root / "TODO.md").read_bytes() == todo
    assert after["agents"]["ag"]["chat_cursor"] == cursor
    assert not (root / ".chat.lock").exists()
    assert {path.name for path in root.iterdir()} == names


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("unknown", "unknown_agent"),
        ("disabled", "disabled"),
        ("idle", "parked"),
        ("blocked", "human_required"),
        ("done", "bookkeeping"),
        ("nonowner", "not_owner"),
        ("missing_task", "missing_task"),
        ("missing_review", "human_required"),
        ("live_lease", "lease_held"),
    ),
)
def test_take_refusals_are_stable_and_do_not_write(board, capsys, case, reason):
    data = state.load(board)
    data["task"].update({"id": "refuse", "phase": "awaiting_draft"})
    agent = "ag"
    if case == "unknown":
        agent = "stranger"
    elif case == "disabled":
        data["enabled"] = False
    elif case in {"idle", "blocked", "done"}:
        data["task"]["phase"] = case
    elif case == "nonowner":
        agent = "cc"
    elif case == "missing_task":
        data["task"]["id"] = None
    elif case == "missing_review":
        data["task"].update(
            {"phase": "plan_redlined", "last_redline": None, "verdict": None}
        )
    elif case == "live_lease":
        state.claim_lease(data, "cc")
    state.write_atomic(board, data)
    before = state.load(board)
    result = take_json(board, agent, capsys, expected=EXIT_REFUSED)

    assert result["mode"] is None
    assert result["started"] is False
    assert result["resumed"] is False
    assert result["claimed"] is False
    assert result["reason"] == reason
    assert reason in TAKE_REFUSALS
    assert result["brief"] is None
    assert state.load(board) == before


def test_human_take_renders_full_nonredundant_post_take_brief(relay_board, capsys):
    data = state.load(relay_board)
    data["task"].update(
        {"id": "human", "title": "Useful output", "phase": "plan_ready"}
    )
    state.write_atomic(relay_board, data)
    Path(state.chat_path(relay_board)).write_text("Remember the focused test\n")
    assert main(["--dir", relay_board, "take", "ag"]) == 0
    output = capsys.readouterr().out
    assert "cordboard: started human" in output
    assert "task=human" in output and "title=Useful output" in output
    assert "Remember the focused test" in output
    assert "instructions:" in output and "transitions:" in output
    assert "advance ag --rev 2" in output
    assert "--phase awaiting_review" not in output
    assert "--claim" not in output


def test_malformed_chat_fails_before_take_commits(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "bad-chat", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    before = state.load(board)
    Path(state.chat_path(board)).write_bytes(b"\xff")
    assert main(["--dir", board, "take", "ag", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not valid UTF-8" in captured.err
    assert state.load(board) == before


def test_candidate_brief_failure_aborts_before_commit(board, capsys, monkeypatch):
    data = state.load(board)
    data["task"].update({"id": "bad-context", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    before = state.load(board)

    def fail(*args, **kwargs):
        raise state.StateError("candidate brief failed")

    monkeypatch.setattr(cli, "_brief_payload", fail)
    assert main(["--dir", board, "take", "ag", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "candidate brief failed" in captured.err
    assert state.load(board) == before


def test_malformed_revision_is_exit_2_without_write(board, capsys):
    data = state.load(board)
    data["rev"] = "bad"
    data["task"].update({"id": "bad-rev", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    assert main(["--dir", board, "take", "ag", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rev must be a non-negative integer" in captured.err
    assert state.load(board)["rev"] == "bad"


def test_same_agent_renews_live_lease(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "renew", "phase": "drafting"})
    data["lock"] = {
        "holder": "ag",
        "acquired_at": "2020-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    state.write_atomic(board, data)
    result = take_json(board, "ag", capsys)
    after = state.load(board)
    assert result["mode"] == "resumed"
    assert after["lock"]["acquired_at"] != "2020-01-01T00:00:00Z"
    assert result["lease_expires_at"] == after["lock"]["expires_at"]


def test_expired_foreign_lease_is_recoverable(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "recover", "phase": "awaiting_draft"})
    state.claim_lease(data, "cc")
    data["lock"]["expires_at"] = "2020-01-01T00:00:00Z"
    state.write_atomic(board, data)
    result = take_json(board, "ag", capsys)
    assert result["claimed"] is True
    assert state.load(board)["lock"]["holder"] == "ag"


def test_custom_planning_paths_are_preserved(board, capsys):
    data = state.load(board)
    data["task"].update(
        {
            "id": "custom",
            "phase": "awaiting_draft",
            "dir": "plans/custom-place",
            "plan_file": "plans/custom-place/design.md",
        }
    )
    state.write_atomic(board, data)
    result = take_json(board, "ag", capsys)
    after = state.load(board)
    assert after["task"]["dir"] == "plans/custom-place"
    assert after["task"]["plan_file"] == "plans/custom-place/design.md"
    assert result["brief"]["task"]["plan_file"] == "plans/custom-place/design.md"


def test_handoff_time_advances_for_start_but_not_resume(board, capsys):
    data = state.load(board)
    data["task"].update(
        {"id": "stamp", "phase": "awaiting_draft", "handoff_at": "2020-01-01T00:00:00Z"}
    )
    state.write_atomic(board, data)
    take_json(board, "ag", capsys)
    assert state.load(board)["task"]["handoff_at"] != "2020-01-01T00:00:00Z"

    data = state.load(board)
    data["task"].update({"phase": "drafting", "handoff_at": "2020-02-02T00:00:00Z"})
    state.release_lease(data, "ag")
    state.write_atomic(board, data)
    take_json(board, "ag", capsys)
    assert state.load(board)["task"]["handoff_at"] == "2020-02-02T00:00:00Z"


def test_solo_role_mapping_can_take_reviewer_work(board, capsys):
    data = state.load(board)
    data["roles"] = {"reviewer": "ag", "author": "ag"}
    data["task"].update({"id": "solo", "phase": "plan_submitted"})
    state.write_atomic(board, data)
    result = take_json(board, "ag", capsys)
    assert result["phase"] == "plan_redlining"
    assert result["brief"]["turn"]["owner"] == "ag"


def test_concurrent_owner_and_nonowner_cannot_both_claim(board):
    data = state.load(board)
    data["task"].update({"id": "race", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    before_rev = data["rev"]
    source = str(Path(__file__).parents[1] / "src")
    script = (
        "import sys;"
        f"sys.path.insert(0, {source!r});"
        "from cordboard.cli import main;"
        f"raise SystemExit(main(['--dir', {board!r}, 'take', sys.argv[1], '--json']))"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, agent], stdout=subprocess.PIPE, text=True
        )
        for agent in ("ag", "cc")
    ]
    results = [(process.wait(), json.loads(process.stdout.read())) for process in processes]
    assert sorted(code for code, _ in results) == [0, EXIT_REFUSED]
    assert sum(payload["claimed"] for _, payload in results) == 1
    assert state.load(board)["rev"] == before_rev + 1


def test_take_mapping_is_explicit_and_legal(board, relay_board):
    roots = {"redline": board, "relay": relay_board}
    for workflow, mapping in TAKE_PHASES.items():
        data = state.load(roots[workflow])
        for source, target in mapping.items():
            data["task"]["phase"] = source
            state.write_atomic(roots[workflow], data)
            assert source == target or target in TRANSITIONS[workflow][source]
