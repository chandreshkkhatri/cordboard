"""Revision-bound, crash-recoverable completion of done tasks."""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cordboard import state, tasks
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main
from cordboard.workflows import FINISH_REFUSALS

ENVELOPE_KEYS = {
    "finished",
    "reason",
    "message",
    "expected_rev",
    "rev",
    "agent",
    "completed_task",
    "todo_changed",
    "recovered",
    "next_task",
    "next_phase",
    "next_owner",
    "parked",
}


def add_tasks(root: str, text: str) -> None:
    path = Path(state.stack_path(root))
    path.write_text(path.read_text() + text)


def prepare(root: str, task_id: str = "current", *, agent: str = "ag") -> int:
    def done(data):
        data["task"].update(
            {"id": task_id, "title": "Current task", "phase": "done"}
        )
        state.claim_lease(data, agent)
        state.touch(data, agent, status="working")

    data, _ = state.update(root, done, actor=agent)
    return data["rev"]


def finish_json(
    root: str,
    agent: str,
    rev: int,
    capsys,
    *,
    expected: int = 0,
) -> dict:
    assert main(["--dir", root, "finish", agent, "--rev", str(rev), "--json"]) == expected
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload) == ENVELOPE_KEYS
    return payload


@pytest.mark.parametrize(
    ("fixture_name", "agent", "entry_phase", "next_owner"),
    (
        ("board", "ag", "awaiting_draft", "ag"),
        ("relay_board", "cc", "awaiting_plan", "cc"),
    ),
)
def test_finish_checks_off_and_installs_next_lifo_task(
    request, fixture_name, agent, entry_phase, next_owner, capsys
):
    root = request.getfixturevalue(fixture_name)
    add_tasks(root, "\n- [ ] next — Next task\n- [ ] current — Current task\n")
    rev = prepare(root, agent=agent)
    result = finish_json(root, agent, rev, capsys)
    after = state.load(root)

    assert result == {
        "finished": True,
        "reason": "finished",
        "message": "finished task current",
        "expected_rev": rev,
        "rev": rev + 1,
        "agent": agent,
        "completed_task": {"id": "current", "title": "Current task"},
        "todo_changed": True,
        "recovered": False,
        "next_task": {"id": "next", "title": "Next task"},
        "next_phase": entry_phase,
        "next_owner": next_owner,
        "parked": False,
    }
    assert after["rev"] == rev + 1
    assert state.task(after)["id"] == "next"
    assert state.phase(after) == entry_phase
    assert after["lock"]["holder"] is None
    assert after["agents"][agent]["status"] == "paused"
    event = after["history"]["events"][-1]
    assert event["rev"] == result["rev"]
    assert event["action"] == "finish"
    assert event["task_id"] == "current"
    assert event["phase"] == entry_phase
    assert event["lease"] == "released"
    assert {item.id: item.done for item in tasks.read(root)} == {
        "next": False,
        "current": True,
    }


def test_finish_parks_when_queue_is_empty(board, capsys):
    add_tasks(board, "\n- [ ] current — Current task\n")
    rev = prepare(board)
    result = finish_json(board, "ag", rev, capsys)
    assert result["parked"] is True
    assert result["next_task"] is None
    assert result["next_phase"] == "idle"
    assert result["next_owner"] is None
    assert state.phase(state.load(board)) == "idle"


def test_finish_recovers_when_todo_was_already_checked(board, capsys):
    add_tasks(board, "\n- [ ] next — Next task\n- [ ] current — Current task\n")
    rev = prepare(board)
    assert tasks.complete(board, "current") is True
    result = finish_json(board, "ag", rev, capsys)
    assert result["todo_changed"] is False
    assert result["recovered"] is True
    assert result["rev"] == rev + 1
    assert result["next_task"]["id"] == "next"


def test_crash_after_todo_write_recovers_on_same_done_revision(
    board, capsys, monkeypatch
):
    add_tasks(board, "\n- [ ] next — Next task\n- [ ] current — Current task\n")
    rev = prepare(board)
    before = state.load(board)
    original_write = state.write_atomic

    def crash(*args, **kwargs):
        raise OSError("simulated state-write crash")

    monkeypatch.setattr(state, "write_atomic", crash)
    assert main(["--dir", board, "finish", "ag", "--rev", str(rev), "--json"]) == 2
    failed = capsys.readouterr()
    assert failed.out == ""
    assert "simulated state-write crash" in failed.err
    assert state.load(board) == before
    assert state.load(board)["history"]["events"] == before["history"]["events"]
    assert {item.id: item.done for item in tasks.read(board)}["current"] is True

    monkeypatch.setattr(state, "write_atomic", original_write)
    result = finish_json(board, "ag", rev, capsys)
    assert result["recovered"] is True
    assert result["todo_changed"] is False
    assert result["next_task"]["id"] == "next"
    events = state.load(board)["history"]["events"]
    assert len(events) == 1
    assert events[0]["action"] == "finish"


def test_postcommit_retry_is_revision_mismatch_and_changes_nothing(board, capsys):
    add_tasks(board, "\n- [ ] current — Current task\n")
    rev = prepare(board)
    first = finish_json(board, "ag", rev, capsys)
    after = state.load(board)
    todo = Path(state.stack_path(board)).read_bytes()
    retry = finish_json(board, "ag", rev, capsys, expected=EXIT_REFUSED)
    assert first["finished"] is True
    assert retry["reason"] == "revision_mismatch"
    assert retry["completed_task"] is None
    assert state.load(board) == after
    assert Path(state.stack_path(board)).read_bytes() == todo


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("disabled", "disabled"),
        ("unknown", "unknown_agent"),
        ("stale", "revision_mismatch"),
        ("not_done", "not_done"),
        ("nonowner", "not_owner"),
        ("missing_task", "missing_task"),
        ("task_not_in_todo", "task_not_in_todo"),
    ),
)
def test_finish_refusals_are_stable_and_change_nothing(board, capsys, case, reason):
    add_tasks(board, "\n- [ ] current — Current task\n")
    rev = prepare(board)
    data = state.load(board)
    agent, requested_rev = "ag", rev
    if case == "disabled":
        data["enabled"] = False
    elif case == "unknown":
        agent = "stranger"
    elif case == "stale":
        requested_rev -= 1
    elif case == "not_done":
        data["task"]["phase"] = "impl_redlined"
    elif case == "nonowner":
        agent = "cc"
    elif case == "missing_task":
        data["task"]["id"] = None
    elif case == "task_not_in_todo":
        data["task"]["id"] = "absent"
    state.write_atomic(board, data)
    before = state.load(board)
    todo = Path(state.stack_path(board)).read_bytes()
    result = finish_json(
        board, agent, requested_rev, capsys, expected=EXIT_REFUSED
    )
    assert result["finished"] is False
    assert result["reason"] == reason
    assert reason in FINISH_REFUSALS
    assert state.load(board) == before
    assert Path(state.stack_path(board)).read_bytes() == todo


@pytest.mark.parametrize("damage", ("missing", "malformed", "duplicate"))
def test_broken_todo_changes_neither_file_and_has_no_partial_output(
    board, capsys, damage
):
    add_tasks(board, "\n- [ ] current — Current task\n")
    rev = prepare(board)
    path = Path(state.stack_path(board))
    if damage == "missing":
        path.unlink()
        before_todo = None
    elif damage == "malformed":
        path.write_text("- [ ] malformed\n")
        before_todo = path.read_bytes()
    else:
        path.write_text("- [ ] current — One\n- [ ] current — Two\n")
        before_todo = path.read_bytes()
    before = state.load(board)
    assert main(["--dir", board, "finish", "ag", "--rev", str(rev), "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("cordboard:")
    assert state.load(board) == before
    assert (path.read_bytes() if path.exists() else None) == before_todo


def test_malformed_board_has_no_partial_output(board, capsys):
    data = state.load(board)
    data["rev"] = "bad"
    state.write_atomic(board, data)
    assert main(["--dir", board, "finish", "ag", "--rev", "0", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid board" in captured.err


def test_finish_preserves_chat_notes_and_artifacts_and_creates_no_next_lease(board, capsys):
    add_tasks(board, "\n- [ ] next — Next task\n- [ ] current — Current task\n")
    rev = prepare(board)
    data = state.load(board)
    data["agents"]["ag"]["note"] = "preserve"
    state.write_atomic(board, data)
    chat = Path(state.chat_path(board))
    chat.write_text("preserve chat\n")
    artifact = Path(board, "plans", "evidence.md")
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("preserve artifact\n")
    finish_json(board, "ag", rev, capsys)
    after = state.load(board)
    assert after["agents"]["ag"]["note"] == "preserve"
    assert after["lock"]["holder"] is None
    assert chat.read_text() == "preserve chat\n"
    assert artifact.read_text() == "preserve artifact\n"


def test_same_revision_contention_finishes_once(board):
    add_tasks(board, "\n- [ ] current — Current task\n")
    rev = prepare(board)
    source = str(Path(__file__).parents[1] / "src")
    script = (
        "import sys;"
        f"sys.path.insert(0, {source!r});"
        "from cordboard.cli import main;"
        f"raise SystemExit(main(['--dir', {board!r}, 'finish', 'ag', "
        f"'--rev', {str(rev)!r}, '--json']))"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    results = [(process.wait(), json.loads(process.stdout.read())) for process in processes]
    assert sorted(code for code, _ in results) == [0, EXIT_REFUSED]
    assert sum(payload["finished"] for _, payload in results) == 1
    assert state.load(board)["rev"] == rev + 1


def test_todo_lock_is_held_through_the_state_commit(board, capsys, monkeypatch):
    add_tasks(board, "\n- [ ] current — Current task\n")
    rev = prepare(board)
    original_write = state.write_atomic
    observed = False

    def assert_locked(root, data):
        nonlocal observed
        with open(Path(root) / ".todo.lock", "a+") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed = True
        original_write(root, data)

    monkeypatch.setattr(state, "write_atomic", assert_locked)
    finish_json(board, "ag", rev, capsys)
    assert observed is True


def test_human_finish_output_reports_recovery_and_next(board, capsys):
    add_tasks(board, "\n- [ ] next — Next task\n- [ ] current — Current task\n")
    rev = prepare(board)
    assert tasks.complete(board, "current") is True
    assert main(["--dir", board, "finish", "ag", "--rev", str(rev)]) == 0
    output = capsys.readouterr().out
    assert "finished current" in output
    assert "recovered TODO write" in output
    assert "next=next phase=awaiting_draft owner=ag" in output


def test_finish_has_no_next_option(board, capsys):
    with pytest.raises(SystemExit) as raised:
        main(["--dir", board, "finish", "ag", "--rev", "0", "--next"])
    assert raised.value.code == 2
    assert "unrecognized arguments: --next" in capsys.readouterr().err
