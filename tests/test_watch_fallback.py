"""Watch is primary; tick is the backstop that catches a missed handoff.

The two are not alternatives, and the tests that matter are the ones proving an
agent cannot end up with no way to wake at all.
"""

from __future__ import annotations

import json

from cordboard import state
from cordboard.cli import EXIT_REFUSED, main
from cordboard.templates import blank_agent, migrate_v2, trigger_to_watch_tick


def test_a_new_agent_watches_by_default(board):
    for name in state.agent_names(state.load(board)):
        block = state.agents(state.load(board))[name]
        assert block["watch"] is True
        assert block["tick"], "a watching agent still needs a backstop"


def test_watch_and_tick_are_independent(board):
    main(["--dir", board, "set", "--agent", "cc", "--no-watch", "--quiet"])
    block = state.agents(state.load(board))["cc"]
    assert block["watch"] is False
    assert block["tick"], "dropping the watcher must not drop the tick"


def test_an_agent_cannot_be_left_with_no_way_to_wake(board):
    """The one configuration that silently parks the board forever."""
    code = main(["--dir", board, "set", "--agent", "cc", "--no-watch", "--tick", "none", "--quiet"])
    assert code == EXIT_REFUSED
    assert state.agents(state.load(board))["cc"]["watch"] is True  # unchanged


def test_dropping_the_tick_is_fine_while_watching(board):
    code = main(["--dir", board, "set", "--agent", "cc", "--tick", "none", "--quiet"])
    assert code == 0
    assert state.agents(state.load(board))["cc"]["tick"] is None


def test_handoff_is_stamped_on_every_phase_change(board):
    main(["--dir", board, "set", "--start", "--quiet"])
    first = state.task(state.load(board))["handoff_at"]
    assert first

    # A write that does not change the phase must not restamp it, or the waiting
    # clock resets every time an agent touches its heartbeat.
    main(["--dir", board, "set", "--agent", "ag", "--note", "still going", "--quiet"])
    assert state.task(state.load(board))["handoff_at"] == first


def test_stall_is_detected_once_the_baton_sits_too_long(board):
    main(["--dir", board, "set", "--start", "--quiet"])
    assert state.is_stalled(state.load(board)) is False

    state.update(board, lambda d: d["task"].update({"handoff_at": "2020-01-01T00:00:00Z"}))
    data = state.load(board)
    assert state.is_stalled(data) is True
    assert state.waiting_seconds(data) > 0
    assert main(["--dir", board, "status", "--stalled"]) == 0


def test_a_disabled_board_is_never_stalled(board):
    """Paused is not stalled; reporting it would train the operator to ignore this."""
    main(["--dir", board, "set", "--start", "--quiet"])
    state.update(board, lambda d: d["task"].update({"handoff_at": "2020-01-01T00:00:00Z"}))
    state.update(board, lambda d: d.update({"enabled": False}))
    assert state.is_stalled(state.load(board)) is False


def test_an_unstamped_board_is_unknown_not_zero(board):
    state.update(board, lambda d: d["task"].update({"handoff_at": None}))
    assert state.waiting_seconds(state.load(board)) is None
    assert state.is_stalled(state.load(board)) is False


def test_trigger_upgrade_does_not_invent_a_watcher():
    """`tick` usually meant "this host cannot watch". Assuming otherwise is a guess."""
    assert trigger_to_watch_tick("watch") == (True, state.DEFAULT_TICK)
    assert trigger_to_watch_tick("tick") == (False, state.DEFAULT_TICK)
    assert trigger_to_watch_tick(None) == (True, state.DEFAULT_TICK)


def test_v3_board_migrates_to_watch_primary(tmp_path):
    root = str(tmp_path / "cordboard")
    legacy = blank_agent()
    legacy.pop("watch"), legacy.pop("tick")
    v3 = {
        "version": 3,
        "enabled": True,
        "workflow": "redline",
        "rev": 7,
        "roles": {"reviewer": "cc", "author": "ag"},
        "agents": {
            "cc": dict(legacy, trigger="watch"),
            "ag": dict(legacy, trigger="tick"),
        },
        "task": {"phase": "idle", "round": {"plan": 0, "impl": 0}},
    }
    import os

    os.makedirs(root, exist_ok=True)
    with open(state.status_path(root), "w", encoding="utf-8") as handle:
        json.dump(v3, handle)

    report = migrate_v2(root, root)
    data = state.load(root)

    assert data["version"] == state.SCHEMA_VERSION
    assert data["rev"] == 7, "an in-flight board keeps its revision"
    assert data["agents"]["cc"]["watch"] is True
    assert data["agents"]["ag"]["watch"] is False
    assert all(a["tick"] for a in data["agents"].values())
    assert "trigger" not in data["agents"]["cc"]
    assert data["stall_after_seconds"]
    assert "migrated v3" in report
