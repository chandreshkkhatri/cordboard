"""Read-only, revision-cursor digests over the bounded factual history."""

from __future__ import annotations

import json
from pathlib import Path

import cordboard.cli as cli
from cordboard import chat, state, tasks
from cordboard.cli import EXIT_REFUSED, main
from cordboard.templates import init_board, migrate_v2

PAYLOAD_KEYS = {
    "requested_since",
    "through_rev",
    "earliest_available_rev",
    "latest_available_rev",
    "truncated",
    "incomplete_through_rev",
    "gap",
    "complete",
    "event_count",
    "tasks",
}
GROUP_KEYS = {
    "task_id",
    "task_title",
    "event_count",
    "lifecycle",
    "handoffs",
    "reviews",
    "completion",
    "artifacts",
}


def digest_json(root: str, since: int, capsys, expected: int = 0) -> dict:
    assert (
        main(["--dir", root, "digest", "--since", str(since), "--json"])
        == expected
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload) == PAYLOAD_KEYS
    return payload


def record(
    root: str,
    *,
    task_id: str,
    action: str,
    from_phase: str,
    phase: str,
    mode: str,
    lease: str,
    verdict: str | None = None,
    outcome: str | None = None,
    artifacts: tuple[str, ...] = (),
) -> int:
    def append(data):
        state.record_history_event(
            data,
            task_id=task_id,
            task_title=f"Title {task_id}",
            actor="ag",
            action=action,
            from_phase=from_phase,
            phase=phase,
            mode=mode,
            verdict=verdict,
            round_number=1 if verdict else None,
            outcome=outcome,
            lease=lease,
            artifacts=artifacts,
        )

    data, _ = state.update(root, append, actor="ag")
    return data["rev"]


def build_interleaved_history(root: str) -> list[int]:
    return [
        record(
            root,
            task_id="alpha",
            action="take",
            from_phase="awaiting_draft",
            phase="drafting",
            mode="started",
            lease="claimed",
            artifacts=("plans/alpha/plan.md",),
        ),
        record(
            root,
            task_id="beta",
            action="take",
            from_phase="awaiting_draft",
            phase="drafting",
            mode="started",
            lease="claimed",
            artifacts=("plans/beta/plan.md",),
        ),
        record(
            root,
            task_id="alpha",
            action="advance",
            from_phase="plan_redlining",
            phase="plan_redlined",
            mode="handed_off",
            lease="released",
            verdict="approve",
            artifacts=("plans/alpha/plan.md", "plans/alpha/redline-plan-r1.md"),
        ),
        record(
            root,
            task_id="beta",
            action="finish",
            from_phase="done",
            phase="idle",
            mode="parked",
            lease="released",
            artifacts=("plans/beta/plan.md",),
        ),
    ]


def test_digest_groups_interleaved_tasks_in_first_event_order(board, capsys):
    revs = build_interleaved_history(board)
    payload = digest_json(board, 1, capsys)

    assert payload["requested_since"] == 1
    assert payload["through_rev"] == revs[-1]
    assert payload["earliest_available_rev"] == revs[0]
    assert payload["latest_available_rev"] == revs[-1]
    assert payload["truncated"] is False
    assert payload["incomplete_through_rev"] is None
    assert payload["gap"] is False
    assert payload["complete"] is True
    assert payload["event_count"] == 4
    assert "events" not in payload
    assert [group["task_id"] for group in payload["tasks"]] == ["alpha", "beta"]
    assert all(set(group) == GROUP_KEYS for group in payload["tasks"])

    alpha, beta = payload["tasks"]
    assert [event["rev"] for event in alpha["lifecycle"]] == [revs[0], revs[2]]
    assert len(alpha["reviews"]) == 1
    assert alpha["reviews"][0]["from_phase"] == "plan_redlining"
    assert alpha["handoffs"] == alpha["reviews"]
    assert alpha["completion"] is None
    assert alpha["artifacts"] == [
        "plans/alpha/plan.md",
        "plans/alpha/redline-plan-r1.md",
    ]
    assert [event["rev"] for event in beta["lifecycle"]] == [revs[1], revs[3]]
    assert beta["reviews"] == []
    assert beta["completion"]["rev"] == revs[3]


def test_since_is_exclusive_and_can_change_first_task_order(board, capsys):
    revs = build_interleaved_history(board)
    payload = digest_json(board, revs[0], capsys)

    assert [group["task_id"] for group in payload["tasks"]] == ["beta", "alpha"]
    assert all(
        event["rev"] > revs[0]
        for group in payload["tasks"]
        for event in group["lifecycle"]
    )
    assert payload["event_count"] == 3


def test_no_event_window_advances_cursor_to_current_board_revision(board, capsys):
    revs = build_interleaved_history(board)
    payload = digest_json(board, revs[-1], capsys)

    assert payload["through_rev"] == revs[-1]
    assert payload["event_count"] == 0
    assert payload["tasks"] == []
    assert payload["complete"] is True
    assert payload["gap"] is False


def test_truncated_ring_reports_partial_and_complete_boundaries(board, capsys):
    data = state.load(board)
    for index in range(state.HISTORY_CAP + 2):
        state.record_history_event(
            data,
            task_id="even" if index % 2 == 0 else "odd",
            task_title=None,
            actor="ag",
            action="take",
            from_phase="awaiting_draft",
            phase="drafting",
            mode="started",
            lease="claimed",
        )
        data["rev"] += 1
    state.write_atomic(board, data)
    earliest = data["history"]["earliest_rev"]
    assert earliest is not None
    boundary = data["history"]["incomplete_through_rev"]
    assert boundary == earliest - 1

    partial = digest_json(board, boundary - 1, capsys)
    complete = digest_json(board, boundary, capsys)

    assert partial["truncated"] is True
    assert partial["incomplete_through_rev"] == boundary
    assert partial["gap"] is True
    assert partial["complete"] is False
    assert complete["gap"] is False
    assert complete["complete"] is True
    assert complete["event_count"] == state.HISTORY_CAP
    assert sum(group["event_count"] for group in complete["tasks"]) == state.HISTORY_CAP


def test_migrated_unknown_window_is_partial_until_its_revision_cursor(tmp_path, capsys):
    root = str(tmp_path / "cordboard")
    init_board(root)
    old = state.load(root)
    old["version"] = 4
    old["rev"] = 7
    old.pop("history")
    state.write_atomic(root, old)
    migrate_v2(root, root)

    partial = digest_json(root, 0, capsys)
    complete = digest_json(root, 7, capsys)

    assert partial["truncated"] is True
    assert partial["incomplete_through_rev"] == 7
    assert partial["earliest_available_rev"] is None
    assert partial["gap"] is True
    assert partial["complete"] is False
    assert partial["tasks"] == []
    assert complete["gap"] is False
    assert complete["complete"] is True


def test_migration_boundary_stays_exact_across_nonhistory_rev_and_first_event(
    tmp_path, capsys
):
    root = str(tmp_path / "cordboard")
    init_board(root)
    old = state.load(root)
    old["version"] = 4
    old["rev"] = 7
    old.pop("history")
    state.write_atomic(root, old)
    migrate_v2(root, root)
    state.update(root, lambda data: data.update({"auto_commit": False}), actor="user")
    event_rev = record(
        root,
        task_id="after-migration",
        action="take",
        from_phase="awaiting_draft",
        phase="drafting",
        mode="started",
        lease="claimed",
    )
    assert event_rev == 9

    complete = digest_json(root, 7, capsys)
    partial = digest_json(root, 6, capsys)

    assert complete["through_rev"] == 9
    assert complete["incomplete_through_rev"] == 7
    assert complete["gap"] is False
    assert complete["complete"] is True
    assert complete["event_count"] == 1
    assert partial["gap"] is True
    assert partial["complete"] is False


def test_digest_is_strictly_read_only_and_reads_no_ancillary_sources(
    tmp_path, capsys, monkeypatch
):
    root = tmp_path / "cordboard"
    init_board(str(root))
    Path(state.stack_path(str(root))).unlink()
    before = {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }

    def unexpected(*args, **kwargs):
        raise AssertionError("digest read an ancillary source")

    monkeypatch.setattr(tasks, "read", unexpected)
    monkeypatch.setattr(chat, "unread", unexpected)
    monkeypatch.setattr(cli, "_validate_artifact", unexpected)
    monkeypatch.setattr(state, "update", unexpected)
    payload = digest_json(str(root), 0, capsys)

    after = {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert payload["event_count"] == 0
    assert after == before
    assert not any(path.name.endswith(".lock") for path in root.iterdir())


def test_negative_since_is_stable_and_does_not_touch_a_board(tmp_path, capsys):
    root = tmp_path / "does-not-exist"
    assert main(["--dir", str(root), "digest", "--since", "-1", "--json"]) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cordboard: --since must be a non-negative revision\n"
    assert not root.exists()


def test_future_since_is_stable_and_read_only(board, capsys):
    before = Path(state.status_path(board)).read_bytes()
    future = state.load(board)["rev"] + 1
    assert (
        main(["--dir", board, "digest", "--since", str(future), "--json"])
        == EXIT_REFUSED
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"cordboard: --since {future} is ahead of current rev {future - 1}\n"
    assert Path(state.status_path(board)).read_bytes() == before


def test_human_digest_is_fixed_factual_output(board, capsys):
    revs = build_interleaved_history(board)
    assert main(["--dir", board, "digest", "--since", "1"]) == 0
    captured = capsys.readouterr()

    assert captured.err == ""
    assert f"since=1 through={revs[-1]} complete=yes" in captured.out
    assert "task=alpha title=Title alpha events=2 reviews=1" in captured.out
    assert f"rev={revs[0]}" in captured.out
    assert "verdict=approve round=1 outcome=—" in captured.out
    assert "artifacts=plans/alpha/plan.md,plans/alpha/redline-plan-r1.md" in captured.out
