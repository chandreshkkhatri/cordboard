"""The back-channel: append-only, concurrent-safe, cursor per agent."""

from __future__ import annotations

import os
import subprocess
import sys

from cordboard import chat, state


def test_append_and_read_advances_the_cursor(board):
    chat.append(board, "cc", "ag", "C3 is advisory", ref="C3")
    text, cursor = chat.unread(board, state.load(board), "ag")
    assert "C3 is advisory" in text

    state.update(board, lambda d: d["agents"]["ag"].update({"chat_cursor": cursor}))
    text, _ = chat.unread(board, state.load(board), "ag")
    assert text == ""


def test_each_agent_has_its_own_cursor(board):
    chat.append(board, "cc", "ag", "first")
    _, cursor = chat.unread(board, state.load(board), "ag")
    state.update(board, lambda d: d["agents"]["ag"].update({"chat_cursor": cursor}))

    # cc has read nothing, so it still sees the message ag has consumed.
    text, _ = chat.unread(board, state.load(board), "cc")
    assert "first" in text


def test_concurrent_appends_do_not_interleave(board):
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    script = (
        "import sys;"
        f"sys.path.insert(0, {src!r});"
        "from cordboard import chat;"
        f"chat.append({board!r}, 'cc', 'ag', 'msg ' + sys.argv[1])"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(i)]) for i in range(15)]
    for proc in procs:
        assert proc.wait() == 0

    with open(state.chat_path(board), encoding="utf-8") as handle:
        body = handle.read()
    assert body.count("## ") == 15
    for i in range(15):
        assert f"msg {i}" in body


def test_a_rotated_log_rereads_rather_than_skipping(board):
    """A cursor past the end means the file was rotated, not that there is nothing new."""
    chat.append(board, "cc", "ag", "after rotation")
    state.update(board, lambda d: d["agents"]["ag"].update({"chat_cursor": 9999}))
    text, _ = chat.unread(board, state.load(board), "ag")
    assert "after rotation" in text
