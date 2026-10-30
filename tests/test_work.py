"""One-call normal wake: observe passive state or acquire actionable work."""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cordboard.cli as cli
from cordboard import state
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main
from cordboard.templates import init_board
from cordboard.workflows import IN_PROGRESS, TAKE_PHASES, WORK_MODES

ENVELOPE_KEYS = {
    "acquired",
    "mode",
    "reason",
    "rev",
    "phase",
    "lease_holder",
    "lease_expires_at",
    "brief",
}


def work_json(root: str, agent: str, capsys, expected: int = 0) -> dict:
    assert main(["--dir", root, "work", agent, "--json"]) == expected
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload) == ENVELOPE_KEYS
    assert payload["mode"] in WORK_MODES
    return payload


def add_evidence(root: str, data: dict, task_id: str, *, redline: bool = False) -> None:
    task_dir = Path(root, "plans", task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "plan.md").write_text("# Plan\n\nVerify the work.\n")
    data["task"].update(
        {"dir": f"plans/{task_id}", "plan_file": f"plans/{task_id}/plan.md"}
    )
    if redline:
        (task_dir / "redline.md").write_text("# Redline\n\nRevise this.\n")
        data["task"].update(
            {"last_redline": f"plans/{task_id}/redline.md", "verdict": "revise"}
        )


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
def test_work_acquires_every_stable_handoff_with_take_semantics(
    request, fixture_name, phase, agent, target, mode, capsys
):
    root = request.getfixturevalue(fixture_name)
    data = state.load(root)
    data["task"].update({"id": "work-task", "title": "Work task", "phase": phase})
    add_evidence(root, data, "work-task", redline=True)
    state.write_atomic(root, data)
    before_rev = data["rev"]

    result = work_json(root, agent, capsys)
    after = state.load(root)

    assert result["acquired"] is True
    assert result["mode"] == result["reason"] == mode
    assert result["phase"] == target == state.phase(after)
    assert result["rev"] == before_rev + 1 == after["rev"]
    assert result["lease_holder"] == agent
    assert result["brief"]["board"]["rev"] == result["rev"]
    assert result["brief"]["task"]["phase"] == target
    assert result["brief"]["action"]["transitions"]
    event = after["history"]["events"][-1]
    assert event["action"] == "take"
    assert event["mode"] == mode
    assert event["from_phase"] == phase
    assert event["phase"] == target
    assert event["rev"] == result["rev"]


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
def test_work_resumes_every_in_progress_phase(
    request, fixture_name, phase, agent, capsys
):
    assert phase in IN_PROGRESS
    root = request.getfixturevalue(fixture_name)
    data = state.load(root)
    data["task"].update({"id": "resume-work", "phase": phase})
    add_evidence(root, data, "resume-work")
    state.claim_lease(data, agent)
    state.write_atomic(root, data)

    result = work_json(root, agent, capsys)

    assert result["acquired"] is True
    assert result["mode"] == "resumed"
    assert result["phase"] == phase
    assert state.load(root)["history"]["events"][-1]["mode"] == "resumed"


@pytest.mark.parametrize(
    ("case", "reason", "disposition"),
    (
        ("disabled", "disabled", "disabled"),
        ("idle", "parked", "parked"),
        ("blocked", "human_required", "human_required"),
        ("wait", "wait", "wait"),
        ("hard_evidence", "human_required", "human_required"),
        ("done", "done", "act"),
    ),
)
def test_passive_work_returns_a_normal_brief_without_update(
    board, capsys, monkeypatch, case, reason, disposition
):
    data = state.load(board)
    data["task"].update({"id": "passive", "title": "Passive", "phase": "awaiting_draft"})
    agent = "ag"
    if case == "disabled":
        data["enabled"] = False
    elif case in {"idle", "blocked", "done"}:
        data["task"]["phase"] = case
        if case == "done":
            add_evidence(board, data, "passive")
            todo = Path(state.stack_path(board))
            todo.write_text(todo.read_text() + "\n- [ ] passive — Passive\n")
    elif case == "wait":
        agent = "cc"
    elif case == "hard_evidence":
        data["task"].update(
            {
                "phase": "plan_submitted",
                "dir": "plans/passive",
                "plan_file": "plans/passive/missing.md",
            }
        )
        agent = "cc"
    state.write_atomic(board, data)
    before = state.load(board)

    def must_not_update(*args, **kwargs):
        raise AssertionError("passive work must not acquire the status lock")

    monkeypatch.setattr(state, "update", must_not_update)
    result = work_json(board, agent, capsys)

    assert result["acquired"] is False
    assert result["mode"] == "observed"
    assert result["reason"] == reason
    assert result["rev"] == before["rev"]
    assert result["brief"]["turn"]["disposition"] == disposition
    if case == "done":
        assert any(
            "finish ag --rev" in transition
            for transition in result["brief"]["action"]["transitions"]
        )
    assert state.load(board) == before


def test_done_with_broken_todo_is_passive_human_required(board, capsys, monkeypatch):
    data = state.load(board)
    data["task"].update({"id": "done", "phase": "done"})
    state.write_atomic(board, data)
    Path(state.stack_path(board)).unlink()
    before = state.load(board)
    monkeypatch.setattr(
        state,
        "update",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected update")),
    )

    result = work_json(board, "ag", capsys)

    assert result["acquired"] is False
    assert result["reason"] == "human_required"
    assert result["brief"]["turn"]["disposition"] == "human_required"
    assert result["brief"]["action"]["transitions"] == []
    assert state.load(board) == before


def test_work_preserves_degraded_ancillary_context_on_acquisition(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "degraded", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    Path(state.stack_path(board)).write_text("- [ ] malformed\n")
    Path(state.chat_path(board)).write_bytes(b"\xff")

    result = work_json(board, "ag", capsys)

    assert result["acquired"] is True
    assert [
        (warning["source"], warning["code"])
        for warning in result["brief"]["board"]["warnings"]
    ] == [("todo", "invalid"), ("chat", "invalid_utf8")]
    assert result["brief"]["chat"]["available"] is False


def test_unknown_agent_is_rejected_before_ancillary_reads(board, capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "_ancillary_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected read")),
    )
    result = work_json(board, "stranger", capsys, expected=EXIT_REFUSED)
    assert result["acquired"] is False
    assert result["reason"] == "unknown_agent"
    assert result["brief"] is None


def test_solo_mapping_acquires_reviewer_phase(board, capsys):
    data = state.load(board)
    data["roles"] = {"reviewer": "ag", "author": "ag"}
    data["task"].update({"id": "solo", "phase": "plan_submitted"})
    add_evidence(board, data, "solo")
    state.write_atomic(board, data)

    result = work_json(board, "ag", capsys)

    assert result["acquired"] is True
    assert result["mode"] == "started"
    assert result["phase"] == "plan_redlining"


def test_work_and_take_have_equivalent_acquisition_results(tmp_path, capsys):
    roots = [str(tmp_path / name) for name in ("work-board", "take-board")]
    for root in roots:
        init_board(root, workflow="redline", reviewer="cc", author="ag")
        state.update(root, lambda data: data.update({"enabled": True}))
        data = state.load(root)
        data["task"].update({"id": "equivalent", "phase": "awaiting_draft"})
        state.write_atomic(root, data)

    work = work_json(roots[0], "ag", capsys)
    assert main(["--dir", roots[1], "take", "ag", "--json"]) == 0
    take = json.loads(capsys.readouterr().out)

    assert work["acquired"] is True
    assert work["mode"] == take["mode"] == "started"
    assert work["rev"] == take["rev"]
    assert work["phase"] == take["phase"] == "drafting"
    assert work["brief"]["turn"] == take["brief"]["turn"]
    work_instructions = [
        instruction.replace(roots[0], "<root>")
        for instruction in work["brief"]["action"]["instructions"]
    ]
    take_instructions = [
        instruction.replace(roots[1], "<root>")
        for instruction in take["brief"]["action"]["instructions"]
    ]
    assert work_instructions == take_instructions
    assert state.load(roots[0])["history"]["events"][-1]["mode"] == state.load(
        roots[1]
    )["history"]["events"][-1]["mode"]


def test_concurrent_same_wake_revision_acquires_once(board):
    data = state.load(board)
    data["task"].update({"id": "contended", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    source = str(Path(__file__).parents[1] / "src")
    script = (
        "import sys;"
        f"sys.path.insert(0, {source!r});"
        "from cordboard.cli import main;"
        f"raise SystemExit(main(['--dir', {board!r}, 'work', 'ag', '--json']))"
    )
    with open(state.lock_path(board), "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
            )
            for _ in range(2)
        ]
        time.sleep(0.2)
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)

    results = [(process.wait(), json.loads(process.stdout.read())) for process in processes]
    assert [code for code, _payload in results] == [0, 0]
    assert sum(payload["acquired"] for _code, payload in results) == 1
    assert {payload["mode"] for _code, payload in results} == {"started", "observed"}
    after = state.load(board)
    assert len(after["history"]["events"]) == 1
    assert after["history"]["events"][0]["action"] == "take"
    observed = next(payload for _code, payload in results if not payload["acquired"])
    assert observed["reason"] == "state_changed"
    assert observed["rev"] == after["rev"]
    assert observed["phase"] == state.phase(after) == "drafting"
    assert observed["brief"]["board"]["rev"] == after["rev"]
    assert observed["brief"]["task"]["phase"] == "drafting"


def test_successful_work_has_no_postcommit_reads(board, capsys, monkeypatch):
    data = state.load(board)
    data["task"].update({"id": "pure-output", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    original_load = state.load
    original_write = state.write_atomic
    committed = False
    writes = 0

    def guarded_load(root):
        if committed:
            raise AssertionError("work read state after committing")
        return original_load(root)

    def counted_write(root, candidate):
        nonlocal committed, writes
        original_write(root, candidate)
        writes += 1
        committed = True

    monkeypatch.setattr(state, "load", guarded_load)
    monkeypatch.setattr(state, "write_atomic", counted_write)

    result = work_json(board, "ag", capsys)

    assert result["acquired"] is True
    assert result["rev"] == data["rev"] + 1
    assert writes == 1


def test_human_work_renders_the_uniform_envelope_and_brief(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "human", "title": "Human work", "phase": "awaiting_draft"})
    state.write_atomic(board, data)

    assert main(["--dir", board, "work", "ag"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "cordboard: acquired mode=started reason=started" in output.out
    assert "agent=ag disposition=resume actionable=yes" in output.out
    assert "transitions:" in output.out


def test_malformed_state_has_no_partial_work_output(board, capsys):
    data = state.load(board)
    data["rev"] = "bad"
    state.write_atomic(board, data)

    assert main(["--dir", board, "work", "ag", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid board" in captured.err


def test_work_table_covers_all_take_phases():
    assert set(WORK_MODES) == {"observed", "started", "resumed", "claimed"}
    assert all(mapping for mapping in TAKE_PHASES.values())
