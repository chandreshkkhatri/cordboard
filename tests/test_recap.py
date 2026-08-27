"""Bounded, machine-authored lifecycle history and its read-only recap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cordboard import state
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main
from cordboard.templates import init_board


def run_json(root: str, *args: str, capsys, expected: int = 0) -> dict:
    assert main(["--dir", root, *args, "--json"]) == expected
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def prepare_draft(root: str) -> None:
    data = state.load(root)
    data["task"].update(
        {
            "id": "history-task",
            "title": "History task",
            "phase": "awaiting_draft",
            "dir": "plans/history-task",
            "plan_file": "plans/history-task/plan.md",
        }
    )
    state.write_atomic(root, data)


def test_recap_reconstructs_lifecycle_handoffs_reviews_and_artifacts(board, capsys):
    prepare_draft(board)

    taken = run_json(board, "take", "ag", capsys=capsys)
    task_dir = Path(board, "plans", "history-task")
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text("# Plan\n\nImplement and verify.\n")
    submitted = run_json(
        board,
        "advance",
        "ag",
        "--rev",
        str(taken["rev"]),
        capsys=capsys,
    )
    reviewing = run_json(board, "take", "cc", capsys=capsys)
    redline = task_dir / "redline-plan-r1.md"
    redline.write_text("# Redline\n\nRevise one detail.\n")
    revised = run_json(
        board,
        "advance",
        "cc",
        "--rev",
        str(reviewing["rev"]),
        "--verdict",
        "revise",
        capsys=capsys,
    )

    payload = run_json(board, "recap", "history-task", capsys=capsys)
    assert payload["available"] is True
    assert payload["task_id"] == "history-task"
    assert payload["event_count"] == 4
    assert [event["action"] for event in payload["lifecycle"]] == [
        "take",
        "advance",
        "take",
        "advance",
    ]
    assert [event["rev"] for event in payload["lifecycle"]] == [
        taken["rev"],
        submitted["rev"],
        reviewing["rev"],
        revised["rev"],
    ]
    assert [event["phase"] for event in payload["handoffs"]] == [
        "plan_submitted",
        "plan_redlined",
    ]
    assert len(payload["reviews"]) == 1
    assert payload["reviews"][0]["verdict"] == "revise"
    assert payload["reviews"][0]["round"] == 1
    assert payload["completion"] is None
    assert payload["artifacts"] == [
        "plans/history-task/plan.md",
        "plans/history-task/redline-plan-r1.md",
    ]
    assert all(set(event) == state.HISTORY_EVENT_FIELDS for event in payload["lifecycle"])
    forbidden = {"chat", "note", "content", "diff", "prose", "summary"}
    assert all(not (set(event) & forbidden) for event in payload["lifecycle"])


def test_full_redline_cycle_counts_only_the_two_gate_decisions_as_reviews(board, capsys):
    todo = Path(state.stack_path(board))
    todo.write_text(todo.read_text() + "\n- [ ] history-task — History task\n")
    prepare_draft(board)
    task_dir = Path(board, "plans", "history-task")

    draft = run_json(board, "take", "ag", capsys=capsys)
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text("# Plan\n\nImplement and verify.\n")
    plan_submitted = run_json(
        board, "advance", "ag", "--rev", str(draft["rev"]), capsys=capsys
    )
    plan_review = run_json(board, "take", "cc", capsys=capsys)
    (task_dir / "redline-plan-r1.md").write_text("# Plan review\n\nApproved.\n")
    plan_decision = run_json(
        board,
        "advance",
        "cc",
        "--rev",
        str(plan_review["rev"]),
        "--verdict",
        "approve",
        capsys=capsys,
    )
    plan_response = run_json(board, "take", "ag", capsys=capsys)
    implementing = run_json(
        board,
        "advance",
        "ag",
        "--rev",
        str(plan_response["rev"]),
        capsys=capsys,
    )
    implementation = run_json(
        board,
        "advance",
        "ag",
        "--rev",
        str(implementing["rev"]),
        capsys=capsys,
    )
    impl_review = run_json(board, "take", "cc", capsys=capsys)
    (task_dir / "redline-impl-r1.md").write_text("# Implementation review\n\nApproved.\n")
    impl_decision = run_json(
        board,
        "advance",
        "cc",
        "--rev",
        str(impl_review["rev"]),
        "--verdict",
        "approve",
        capsys=capsys,
    )
    impl_response = run_json(board, "take", "ag", capsys=capsys)
    done = run_json(
        board,
        "advance",
        "ag",
        "--rev",
        str(impl_response["rev"]),
        capsys=capsys,
    )
    run_json(board, "finish", "ag", "--rev", str(done["rev"]), capsys=capsys)

    payload = run_json(board, "recap", "history-task", capsys=capsys)
    assert [event["from_phase"] for event in payload["reviews"]] == [
        "plan_redlining",
        "impl_redlining",
    ]
    assert [event["rev"] for event in payload["reviews"]] == [
        plan_decision["rev"],
        impl_decision["rev"],
    ]
    assert [event["verdict"] for event in payload["reviews"]] == [
        "approve",
        "approve",
    ]
    assert plan_submitted["phase"] == "plan_submitted"
    assert implementation["phase"] == "impl_submitted"
    assert all(
        event["verdict"] is None
        for event in payload["lifecycle"]
        if event["action"] == "take"
    )


def test_relay_review_outcome_is_a_decision_event(relay_board, capsys):
    root = Path(relay_board)
    task_dir = root / "plans" / "relay-review"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text("# Plan\n\nReview implementation.\n")

    def reviewing(data):
        data["task"].update(
            {
                "id": "relay-review",
                "title": "Relay review",
                "phase": "reviewing",
                "dir": "plans/relay-review",
                "plan_file": "plans/relay-review/plan.md",
            }
        )
        state.claim_lease(data, "cc")

    ready, _ = state.update(relay_board, reviewing, actor="cc")
    decision = run_json(
        relay_board,
        "advance",
        "cc",
        "--rev",
        str(ready["rev"]),
        "--outcome",
        "issues",
        capsys=capsys,
    )
    payload = run_json(relay_board, "recap", "relay-review", capsys=capsys)

    assert len(payload["reviews"]) == 1
    assert payload["reviews"][0]["rev"] == decision["rev"]
    assert payload["reviews"][0]["from_phase"] == "reviewing"
    assert payload["reviews"][0]["outcome"] == "issues"


def test_finish_records_completion_before_parking_and_recap_survives_task_change(
    board, capsys
):
    todo = Path(state.stack_path(board))
    todo.write_text(todo.read_text() + "\n- [ ] completed — Completed task\n")

    def done(data):
        data["task"].update(
            {
                "id": "completed",
                "title": "Completed task",
                "phase": "done",
                "dir": "plans/completed",
                "plan_file": "plans/completed/plan.md",
                "last_redline": "plans/completed/redline-impl-r1.md",
                "verdict": "approve",
                "round": {"plan": 1, "impl": 1},
            }
        )
        state.claim_lease(data, "ag")
        state.touch(data, "ag", status="working")

    ready, _ = state.update(board, done, actor="ag")
    result = run_json(
        board, "finish", "ag", "--rev", str(ready["rev"]), capsys=capsys
    )
    payload = run_json(board, "recap", "completed", capsys=capsys)

    assert result["parked"] is True
    assert payload["completion"] == payload["lifecycle"][-1]
    assert payload["completion"]["action"] == "finish"
    assert payload["completion"]["from_phase"] == "done"
    assert payload["completion"]["phase"] == "idle"
    assert payload["completion"]["mode"] == "parked"
    assert payload["completion"]["lease"] == "released"
    assert payload["completion"]["rev"] == result["rev"]
    assert payload["artifacts"] == [
        "plans/completed/plan.md",
        "plans/completed/redline-impl-r1.md",
    ]
    default_payload = run_json(board, "recap", capsys=capsys)
    assert default_payload["available"] is True
    assert default_payload["task_id"] == "completed"


def test_empty_recap_is_explicitly_unavailable_and_human_output_is_stable(board, capsys):
    payload = run_json(board, "recap", "never-seen", capsys=capsys)
    assert payload == {
        "available": False,
        "task_id": "never-seen",
        "truncated": False,
        "earliest_available_rev": None,
        "latest_available_rev": None,
        "event_count": 0,
        "lifecycle": [],
        "handoffs": [],
        "reviews": [],
        "completion": None,
        "artifacts": [],
    }
    assert main(["--dir", board, "recap", "never-seen"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "task=never-seen available=no events=0" in output.out


def test_recap_is_zero_write_and_creates_no_locks(tmp_path, capsys):
    root = tmp_path / "cordboard"
    init_board(str(root))
    before = {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert not any(path.name.endswith(".lock") for path in root.iterdir())

    run_json(str(root), "recap", capsys=capsys)

    after = {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not any(path.name.endswith(".lock") for path in root.iterdir())


def test_history_ring_is_bounded_and_reports_the_honest_boundary():
    data = {
        "rev": 0,
        "history": {"events": [], "truncated": False, "earliest_rev": None},
    }
    for _index in range(state.HISTORY_CAP + 2):
        state.record_history_event(
            data,
            task_id="ring",
            task_title="Ring task",
            actor="ag",
            action="take",
            from_phase="awaiting_draft",
            phase="drafting",
            mode="started",
            lease="claimed",
        )
        data["rev"] += 1

    history = data["history"]
    assert len(history["events"]) == state.HISTORY_CAP
    assert history["truncated"] is True
    assert history["earliest_rev"] == 3
    assert history["events"][0]["rev"] == 3
    assert history["events"][-1]["rev"] == state.HISTORY_CAP + 2


def test_refusal_and_failed_atomic_write_do_not_leave_history_events(
    board, capsys, monkeypatch
):
    prepare_draft(board)
    before = state.load(board)
    assert run_json(
        board, "take", "cc", capsys=capsys, expected=EXIT_REFUSED
    )["reason"] == "not_owner"
    assert state.load(board) == before

    original_write = state.write_atomic

    def crash(*args, **kwargs):
        raise OSError("simulated history write crash")

    monkeypatch.setattr(state, "write_atomic", crash)
    assert main(["--dir", board, "take", "ag", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "simulated history write crash" in captured.err
    assert state.load(board) == before
    monkeypatch.setattr(state, "write_atomic", original_write)

    taken = run_json(board, "take", "ag", capsys=capsys)
    after = state.load(board)
    assert len(after["history"]["events"]) == 1
    assert after["history"]["events"][0]["rev"] == taken["rev"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("phase", "invented phase prose", ".phase must be a known phase"),
        ("from_phase", "invented phase prose", ".from_phase must be a known phase"),
        ("mode", "agent supplied prose", ".mode is invalid for action take"),
    ),
)
def test_malformed_history_has_no_partial_recap_output(
    board, capsys, field, value, message
):
    prepare_draft(board)
    run_json(board, "take", "ag", capsys=capsys)
    data = state.load(board)
    data["history"]["events"][0][field] = value
    state.write_atomic(board, data)

    assert main(["--dir", board, "recap", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid board" in captured.err
    assert message in captured.err
