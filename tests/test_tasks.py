"""The Markdown TODO stack and its CLI lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

from cordboard import state, tasks
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main


def add_tasks(board: str, text: str) -> None:
    path = Path(state.stack_path(board))
    path.write_text(path.read_text() + text)


def test_bottom_pending_task_is_next(board, capsys):
    add_tasks(board, "\n- [ ] later — Do later\n- [x] old — Already done\n- [ ] first — Do first\n")
    assert tasks.next_pending(board).id == "first"

    assert main(["--dir", board, "task", "list"]) == 0
    assert "first — Do first  ← next" in capsys.readouterr().out


def test_list_json_is_machine_readable(board, capsys):
    add_tasks(board, "\n- [ ] later — Do later\n- [ ] first — Do first\n")
    assert main(["--dir", board, "task", "list", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["pending"] == 2
    assert output["summary"]["next"]["id"] == "first"
    assert [item["next"] for item in output["tasks"]] == [False, True]


def test_start_installs_and_resets_the_next_task(board):
    add_tasks(board, "\n- [ ] second — Second task\n- [ ] first — First task\n")
    state.update(
        board,
        lambda d: d["task"].update(
            {"phase": "done", "id": "old", "verdict": "reject", "round": {"plan": 2, "impl": 1}}
        ),
    )

    assert main(["--dir", board, "task", "start"]) == 0
    task = state.task(state.load(board))
    assert task["id"] == "first"
    assert task["title"] == "First task"
    assert task["phase"] == "awaiting_draft"
    assert task["verdict"] is None
    assert task["round"] == {"plan": 0, "impl": 0}
    assert task["handoff_at"]


def test_start_refuses_to_replace_active_work(board):
    add_tasks(board, "\n- [ ] next — Next task\n")
    state.update(board, lambda d: d["task"].update({"phase": "drafting", "id": "active"}))
    assert main(["--dir", board, "task", "start"]) == EXIT_REFUSED
    assert state.task(state.load(board))["id"] == "active"


def test_stack_rejects_malformed_and_duplicate_entries(board):
    add_tasks(board, "\n- [ ] missing separator\n")
    assert main(["--dir", board, "task", "list"]) == EXIT_BAD_STATE

    path = Path(state.stack_path(board))
    path.write_text("- [ ] same — One\n- [x] same — Two\n")
    assert main(["--dir", board, "task", "start"]) == EXIT_BAD_STATE


def test_complete_checks_off_the_done_task_idempotently(board):
    add_tasks(board, "\n- [ ] current — Current task\n")
    state.update(board, lambda d: d["task"].update({"phase": "done", "id": "current"}))

    # Simulate a crash after the TODO write but before the phase handoff.
    assert tasks.complete(board, "current") is True
    assert main(["--dir", board, "task", "complete"]) == 0
    assert tasks.read(board)[0].done is True
    assert state.phase(state.load(board)) == "idle"


def test_complete_can_start_the_next_lifo_task(board):
    add_tasks(board, "\n- [ ] next — Next task\n- [ ] current — Current task\n")
    state.update(board, lambda d: d["task"].update({"phase": "done", "id": "current"}))

    assert main(["--dir", board, "task", "complete", "--next"]) == 0
    data = state.load(board)
    assert state.task(data)["id"] == "next"
    assert state.phase(data) == "awaiting_draft"
    assert {item.id: item.done for item in tasks.read(board)} == {"next": False, "current": True}


def test_complete_refuses_before_done_without_touching_todo(board):
    add_tasks(board, "\n- [ ] active — Active task\n")
    state.update(board, lambda d: d["task"].update({"phase": "implementing", "id": "active"}))
    assert main(["--dir", board, "task", "complete"]) == EXIT_REFUSED
    assert tasks.read(board)[0].done is False


def test_complete_without_next_parks_even_when_more_work_is_queued(board):
    add_tasks(board, "\n- [ ] next — Next task\n- [ ] current — Current task\n")
    state.update(board, lambda d: d["task"].update({"phase": "done", "id": "current"}))
    assert main(["--dir", board, "task", "complete"]) == 0
    assert state.phase(state.load(board)) == "idle"
    assert tasks.next_pending(board).id == "next"
