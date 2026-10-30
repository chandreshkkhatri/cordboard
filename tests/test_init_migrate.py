"""`cordboard init`, and importing a v2 board so a live loop survives the move."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cordboard import roles, state
from cordboard.cli import EXIT_BAD_STATE, main
from cordboard.templates import init_board, migrate_board, migrate_v2

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


@pytest.mark.parametrize("version", [3, 4, 5])
def test_public_migration_supports_every_modern_legacy_schema(tmp_path, version, capsys):
    root = str(tmp_path / f"v{version}")
    init_board(root, reviewer="cc", author="ag")
    old = state.load(root)
    old["version"] = version
    old["rev"] = 7
    old["task"].update({"id": "live", "title": "Live", "phase": "awaiting_draft"})
    if version == 3:
        old.pop("history")
        for agent in old["agents"].values():
            agent["trigger"] = "watch" if agent.pop("watch") else "tick"
            agent.pop("tick")
    elif version == 4:
        old.pop("history")
    else:
        old["history"].pop("incomplete_through_rev")
    state.write_atomic(root, old)

    assert main(["--dir", root, "init", "--migrate"]) == 0
    migrated = state.load(root)
    assert migrated["version"] == 6
    assert migrated["rev"] == 7
    assert migrated["task"]["id"] == "live"
    assert migrated["roles"] == {"reviewer": "cc", "author": "ag"}
    assert os.path.exists(state.status_path(root) + f".v{version}.bak")
    state.validate(migrated)


def test_cross_root_migration_preserves_workspace_and_installs_current_spec(tmp_path):
    legacy = tmp_path / "legacy"
    destination = tmp_path / "cordboard"
    legacy.mkdir()
    destination.mkdir()
    (legacy / "status.json").write_text(json.dumps(V2_BOARD))
    (legacy / "TODO.md").write_text("legacy todo\n")
    (legacy / "chat.md").write_text("legacy chat\n")
    (legacy / "plans" / "live").mkdir(parents=True)
    (legacy / "plans" / "live" / "plan.md").write_text("legacy plan\n")
    (destination / "TODO.md").write_text("destination todo\n")
    (destination / "plans" / "live").mkdir(parents=True)
    (destination / "plans" / "live" / "plan.md").write_text("destination plan\n")

    migrate_board(str(destination), str(legacy))

    assert (destination / "TODO.md").read_text() == "destination todo\n"
    assert (destination / "chat.md").read_text() == "legacy chat\n"
    assert (destination / "plans" / "live" / "plan.md").read_text() == "destination plan\n"
    packaged = Path(__file__).parents[1] / "src/cordboard/data/instructions.md"
    assert (destination / "instructions.md").read_text() == packaged.read_text()


def test_public_v2_migration_normalizes_artifacts_and_can_immediately_work(tmp_path, capsys):
    legacy = tmp_path / "collab"
    destination = tmp_path / "cordboard"
    plan = legacy / "plans" / "2026-08-22-a1" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n\nResume this review.\n")
    redline = plan.parent / "redline-plan-r1.md"
    redline.write_text("# Redline\n\nPrior feedback.\n")
    old = json.loads(json.dumps(V2_BOARD))
    old["enabled"] = True
    old["task"]["dir"] = "collab/plans/2026-08-22-a1"
    old["task"]["last_redline"] = "collab/plans/2026-08-22-a1/redline-plan-r1.md"
    (legacy / "status.json").write_text(json.dumps(old))

    command = [
        "--dir", str(destination), "init", "--migrate", "--legacy", str(legacy),
    ]
    assert main(command) == 0
    capsys.readouterr()
    migrated = state.load(str(destination))
    assert migrated["task"]["dir"] == "plans/2026-08-22-a1"
    assert migrated["task"]["plan_file"] == "plans/2026-08-22-a1/plan.md"
    assert migrated["task"]["last_redline"] == (
        "plans/2026-08-22-a1/redline-plan-r1.md"
    )

    assert main(["--dir", str(destination), "work", "claude_code", "--json"]) == 0
    acquired = json.loads(capsys.readouterr().out)
    assert acquired["acquired"] is True
    assert acquired["phase"] == "plan_redlining"


def test_public_v2_in_place_migration_backs_up_original_status(tmp_path, capsys):
    root = tmp_path / "collab"
    root.mkdir()
    original = json.dumps(V2_BOARD)
    (root / "status.json").write_text(original)

    assert main(["--dir", str(root), "init", "--migrate"]) == 0
    capsys.readouterr()
    assert (root / "status.json.v2.bak").read_text() == original
    assert state.load(str(root))["version"] == state.SCHEMA_VERSION


def test_public_current_schema_migration_is_idempotent_and_zero_write(tmp_path, capsys):
    root = tmp_path / "cordboard"
    init_board(str(root))

    def snapshot() -> dict[Path, bytes]:
        return {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    before = snapshot()

    assert main(["--dir", str(root), "init", "--migrate"]) == 0
    assert "nothing to migrate" in capsys.readouterr().out
    assert snapshot() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1},
        {"version": 99},
        {"version": "5"},
        {"version": 4, "roles": "broken"},
    ],
)
def test_unsupported_or_malformed_migration_never_modifies_destination(
    tmp_path, payload, capsys
):
    legacy = tmp_path / "legacy"
    destination = tmp_path / "destination"
    legacy.mkdir()
    destination.mkdir()
    (legacy / "status.json").write_text(json.dumps(payload))
    legacy_before = {path.name: path.read_bytes() for path in legacy.iterdir()}
    marker = destination / "keep"
    marker.write_text("unchanged")

    command = ["--dir", str(destination), "init", "--migrate", "--legacy", str(legacy)]
    assert main(command) == EXIT_BAD_STATE
    assert list(destination.iterdir()) == [marker]
    assert marker.read_text() == "unchanged"
    assert {path.name: path.read_bytes() for path in legacy.iterdir()} == legacy_before


def test_missing_and_invalid_json_migrations_do_not_create_destination(tmp_path, capsys):
    for index, content in enumerate((None, "{not json")):
        legacy = tmp_path / f"legacy-{index}"
        destination = tmp_path / f"destination-{index}"
        legacy.mkdir()
        if content is not None:
            (legacy / "status.json").write_text(content)
        legacy_before = {path.name: path.read_bytes() for path in legacy.iterdir()}
        assert main([
            "--dir", str(destination), "init", "--migrate", "--legacy", str(legacy)
        ]) == EXIT_BAD_STATE
        assert not destination.exists()
        assert {path.name: path.read_bytes() for path in legacy.iterdir()} == legacy_before
