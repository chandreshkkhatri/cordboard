"""`cordboard init`, and importing a v2 board so a live loop survives the move."""

from __future__ import annotations

import json
import os

from cordboard import roles, state
from cordboard.templates import init_board, migrate_v2

V2_BOARD = {
    "version": 2,
    "enabled": False,
    "workflow": "redline",
    "mode": "dual",
    "auto_commit": True,
    "max_rounds": 3,
    "rev": 41,
    "roles": {"reviewer": "claude_code", "author": "antigravity"},
    "task": {
        "id": "2026-08-22-a1",
        "title": "A task mid-flight",
        "kind": "implement",
        "phase": "plan_submitted",
        "plan_file": "collab/plans/2026-08-22-a1/plan.md",
        "round": {"plan": 1, "impl": 0},
    },
    "claude_code": {
        "status": "paused",
        "trigger": "watch",
        "heartbeat": "2026-08-22T09:00:00Z",
        "note": "n1",
    },
    "antigravity": {
        "status": "paused",
        "trigger": "tick",
        "heartbeat": "2026-08-22T08:00:00Z",
        "note": "n2",
    },
}


def test_init_writes_the_shared_files(tmp_path):
    root = str(tmp_path / "cordboard")
    init_board(root, reviewer="cc", author="ag")

    for name in ("status.json", "TODO.md", "instructions.md", ".gitignore"):
        assert os.path.exists(os.path.join(root, name)), name
    assert os.path.isdir(state.plans_dir(root))


def test_init_ignores_the_runtime_files_only(tmp_path):
    """The spec and the stack are shared; the baton is one machine's state."""
    root = str(tmp_path / "cordboard")
    init_board(root)
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as handle:
        ignored = handle.read()
    assert "status.json" in ignored
    assert "plans/" in ignored
    assert "instructions.md" not in ignored
    assert "TODO.md" not in ignored


def test_init_refuses_to_clobber(tmp_path):
    root = str(tmp_path / "cordboard")
    assert init_board(root) is not None
    assert init_board(root) is None
    assert init_board(root, force=True) is not None


def test_agent_names_are_yours(tmp_path):
    root = str(tmp_path / "cordboard")
    init_board(root, reviewer="gemini-cli", author="my-laptop-agent")
    data = state.load(root)
    assert set(state.agent_names(data)) == {"gemini-cli", "my-laptop-agent"}
    assert roles.role_map(data)["reviewer"] == "gemini-cli"


def test_migrate_v2_keeps_the_task_in_flight(tmp_path):
    legacy = tmp_path / "collab"
    legacy.mkdir()
    (legacy / "status.json").write_text(json.dumps(V2_BOARD))

    root = str(tmp_path / "cordboard")
    report = migrate_v2(root, str(legacy))

    data = state.load(root)
    assert data["version"] == state.SCHEMA_VERSION
    assert data["task"]["phase"] == "plan_submitted"
    assert data["task"]["id"] == "2026-08-22-a1"
    assert data["task"]["round"]["plan"] == 1
    assert data["rev"] == 41
    assert "mode" not in data
    assert "claude_code" not in data  # no longer a top-level key
    assert data["agents"]["claude_code"]["note"] == "n1"
    # v2's `trigger: watch` becomes watch-primary *plus* the backstop it never had.
    assert data["agents"]["claude_code"]["watch"] is True
    assert data["agents"]["claude_code"]["tick"]
    # v2's `trigger: tick` does not silently gain a watcher — that host was put on
    # a timer because it could not watch, and claiming otherwise is a guess.
    assert data["agents"]["antigravity"]["watch"] is False
    assert data["agents"]["antigravity"]["tick"]
    assert data["history"] == {
        "events": [],
        "truncated": True,
        "earliest_rev": None,
        "incomplete_through_rev": 41,
    }
    assert "migrated v2" in report


def test_migrate_v2_solo_mode_becomes_a_role_mapping(tmp_path):
    legacy = tmp_path / "collab"
    legacy.mkdir()
    solo = dict(V2_BOARD, mode="solo_cc")
    (legacy / "status.json").write_text(json.dumps(solo))

    root = str(tmp_path / "cordboard")
    migrate_v2(root, str(legacy))
    data = state.load(root)

    assert roles.is_solo(data) is True
    assert roles.role_map(data) == {"reviewer": "claude_code", "author": "claude_code"}


def test_migrate_is_idempotent(tmp_path):
    root = str(tmp_path / "cordboard")
    init_board(root)
    assert "nothing to migrate" in migrate_v2(root, root)


def test_migrate_v4_adds_honest_empty_history_and_backup(tmp_path):
    root = str(tmp_path / "cordboard")
    init_board(root)
    old = state.load(root)
    old["version"] = 4
    old["rev"] = 7
    old.pop("history")
    state.write_atomic(root, old)

    report = migrate_v2(root, root)
    migrated = state.load(root)

    assert report == "migrated v4 -> v6: factual history starts empty"
    assert migrated["version"] == state.SCHEMA_VERSION
    assert migrated["history"] == {
        "events": [],
        "truncated": True,
        "earliest_rev": None,
        "incomplete_through_rev": 7,
    }
    assert os.path.exists(state.status_path(root) + ".v4.bak")
    state.validate(migrated)


def test_migrate_v5_persists_unknown_history_boundary(tmp_path):
    root = str(tmp_path / "cordboard")
    init_board(root)
    old = state.load(root)
    old["version"] = 5
    old["rev"] = 9
    old["history"] = {"events": [], "truncated": True, "earliest_rev": None}
    state.write_atomic(root, old)

    report = migrate_v2(root, root)
    migrated = state.load(root)

    assert report == "migrated v5 -> v6: exact history loss boundary added"
    assert migrated["history"]["incomplete_through_rev"] == 9
    assert os.path.exists(state.status_path(root) + ".v5.bak")
    state.validate(migrated)
