"""`cordboard wait` — the event trigger. Its exit codes are a contract."""

from __future__ import annotations

import subprocess
import sys
import time

from cordboard import state
from cordboard.cli import WAIT_DISABLED, WAIT_TIMEOUT, WAIT_TURN, main


def test_returns_immediately_when_it_is_your_turn(board):
    state.update(board, lambda d: d["task"].update({"phase": "plan_submitted"}))
    started = time.monotonic()
    assert main(["--dir", board, "wait", "cc", "--timeout", "5", "--quiet"]) == WAIT_TURN
    assert time.monotonic() - started < 1.0


def test_times_out_while_the_other_agent_holds_it(board):
    state.update(board, lambda d: d["task"].update({"phase": "drafting"}))
    code = main(["--dir", board, "wait", "cc", "--timeout", "1", "--interval", "0.1", "--quiet"])
    assert code == WAIT_TIMEOUT


def test_a_disabled_board_stands_the_agent_down(board):
    """Distinct from a timeout: 10 means stop re-arming, 11 means keep waiting."""
    state.update(board, lambda d: d.update({"enabled": False}))
    assert main(["--dir", board, "wait", "cc", "--timeout", "5", "--quiet"]) == WAIT_DISABLED


def test_wakes_when_the_baton_arrives(board, tmp_path):
    """The whole feature: a handoff is noticed in seconds without polling from the model."""
    state.update(board, lambda d: d["task"].update({"phase": "drafting"}))
    proc = subprocess.Popen(
        [sys.executable, "-m", "cordboard.cli", "--dir", board, "wait", "cc",
         "--timeout", "20", "--interval", "0.2", "--quiet"],
        env={"PYTHONPATH": str(tmp_path.parent) + ":" + _src(), "PATH": "/usr/bin:/bin"},
    )
    time.sleep(0.5)
    assert proc.poll() is None, "should still be holding"

    state.update(board, lambda d: d["task"].update({"phase": "plan_submitted"}))
    assert proc.wait(timeout=10) == WAIT_TURN


def _src() -> str:
    import os

    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
