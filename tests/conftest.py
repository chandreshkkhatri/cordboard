"""Shared fixtures. Every test gets its own board in a tmp dir."""

from __future__ import annotations

import pytest

from cordboard import roles, state
from cordboard.templates import init_board


@pytest.fixture
def board(tmp_path):
    """A fresh, enabled `redline` board with agents named cc (reviewer) and ag (author)."""
    root = str(tmp_path / "cordboard")
    init_board(root, workflow="redline", reviewer="cc", author="ag")
    state.update(root, lambda d: d.update({"enabled": True}))
    return root


@pytest.fixture
def relay_board(tmp_path):
    root = str(tmp_path / "cordboard")
    init_board(root, workflow="relay", reviewer="cc", author="ag")
    state.update(root, lambda d: d.update({"enabled": True}))
    return root


def move(root: str, agent: str | None, phase: str, force: bool = False, **extra):
    """Attempt a phase move the way the CLI does. Returns True when it was written."""

    def mutate(data):
        if not force:
            if agent and not roles.is_my_turn(data, agent):
                return False
            allowed, _ = roles.can_move(data, phase)
            if not allowed:
                return False
        data.setdefault("task", {})["phase"] = phase
        for key, value in extra.items():
            data["task"][key] = value
        return None

    _, written = state.update(root, mutate, actor=agent or "user")
    return written
