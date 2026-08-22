"""The compare-and-swap is what lets two agents share one file."""

from __future__ import annotations

import os
import subprocess
import sys

from cordboard import state


def test_concurrent_writers_all_land(board):
    """20 processes, 20 increments, no lost updates and always-valid JSON."""
    start = int(state.load(board)["rev"])
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    script = (
        "import sys;"
        f"sys.path.insert(0, {src!r});"
        "from cordboard import state;"
        f"state.update({board!r}, lambda d: d.setdefault('probe', {{}}).update({{sys.argv[1]: 1}}))"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, f"n{i}"]) for i in range(20)
    ]
    for proc in procs:
        assert proc.wait() == 0

    data = state.load(board)
    assert int(data["rev"]) == start + 20
    assert len(data["probe"]) == 20


def test_aborted_mutation_does_not_bump_rev(board):
    """A guard that refuses must leave no trace — that is how `set` reports refusal."""
    before = state.load(board)["rev"]
    data, written = state.update(board, lambda d: False)
    assert written is False
    assert data["rev"] == before


def test_write_is_atomic(board):
    """No temp files left behind, and the board still parses."""
    state.update(board, lambda d: d.update({"enabled": True}))
    leftovers = [n for n in os.listdir(board) if n.startswith(".status.") and n.endswith(".tmp")]
    assert leftovers == []
    assert state.load(board)["enabled"] is True


def test_missing_board_is_a_clear_error(tmp_path):
    try:
        state.load(str(tmp_path))
    except state.StateError as exc:
        assert "cordboard init" in str(exc)
    else:
        raise AssertionError("expected StateError")
