"""Revision-replacing lease renewal for already-acquired work."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cordboard.cli as cli
from cordboard import chat, state, tasks
from cordboard.cli import EXIT_REFUSED, main
from cordboard.workflows import RENEW_PHASES, RENEW_REFUSALS

ENVELOPE_KEYS = {
    "renewed",
    "reason",
    "message",
    "expected_rev",
    "rev",
    "agent",
    "phase",
    "lease_holder",
    "lease_expires_at",
    "superseded_rev",
    "old_advance_rev_valid",
    "advance_commands",
}


def renew_json(root: str, agent: str, rev: int, capsys, expected: int = 0) -> dict:
    assert (
        main(["--dir", root, "renew", agent, "--rev", str(rev), "--json"])
        == expected
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert set(payload) == ENVELOPE_KEYS
    return payload


def prepare(root: str, phase: str, agent: str, *, ttl: int = 5400) -> int:
    data = state.load(root)
    data["lease_seconds"] = ttl
    data["task"].update(
        {
            "id": "renew-work",
            "title": "Renew work",
            "phase": phase,
            "dir": "plans/renew-work",
            "plan_file": "plans/renew-work/plan.md",
            "verdict": "revise" if phase in {"plan_redlined", "impl_redlined"} else None,
        }
    )
    state.claim_lease(data, agent)
    state.touch(data, agent, status="working")
    state.write_atomic(root, data)
    return data["rev"]


@pytest.mark.parametrize(
    ("fixture_name", "phase", "agent", "command_count", "option"),
    (
        ("relay_board", "planning", "cc", 1, None),
        ("relay_board", "implementing", "ag", 1, None),
        ("relay_board", "reviewing", "cc", 2, "--outcome"),
        ("board", "drafting", "ag", 1, None),
        ("board", "plan_redlining", "cc", 3, "--verdict"),
        ("board", "plan_redlined", "ag", 1, None),
        ("board", "implementing", "ag", 1, None),
        ("board", "impl_redlining", "cc", 3, "--verdict"),
        ("board", "impl_redlined", "ag", 1, None),
    ),
)
def test_renew_supports_every_acquired_phase_and_returns_new_advance_commands(
    request, fixture_name, phase, agent, command_count, option, capsys
):
    root = request.getfixturevalue(fixture_name)
    rev = prepare(root, phase, agent)
    before = state.load(root)

    result = renew_json(root, agent, rev, capsys)
    after = state.load(root)

    assert result["renewed"] is True
    assert result["reason"] == "renewed"
    assert result["expected_rev"] == result["superseded_rev"] == rev
    assert result["rev"] == rev + 1 == after["rev"]
    assert result["old_advance_rev_valid"] is False
    assert result["phase"] == phase == state.phase(after)
    assert result["lease_holder"] == agent == after["lock"]["holder"]
    assert result["lease_expires_at"] == after["lock"]["expires_at"]
    assert len(result["advance_commands"]) == command_count
    assert all(
        f"advance {agent} --rev {result['rev']}" in command
        for command in result["advance_commands"]
    )
    assert all(f"--rev {rev} " not in f"{command} " for command in result["advance_commands"])
    if option:
        assert all(option in command for command in result["advance_commands"])
    assert after["history"] == before["history"]
    assert after["task"]["handoff_at"] == before["task"]["handoff_at"]
    assert after["agents"][agent]["status"] == "working"
    assert after["agents"][agent]["heartbeat"] is not None


def test_renew_honors_custom_board_ttl(board, capsys):
    rev = prepare(board, "drafting", "ag", ttl=137)
    data = state.load(board)
    data["lock"]["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=10)
    ).strftime(state.TIMESTAMP)
    state.write_atomic(board, data)

    result = renew_json(board, "ag", rev, capsys)

    heartbeat = datetime.strptime(
        state.load(board)["agents"]["ag"]["heartbeat"], state.TIMESTAMP
    ).replace(tzinfo=timezone.utc)
    expiry = datetime.strptime(result["lease_expires_at"], state.TIMESTAMP).replace(
        tzinfo=timezone.utc
    )
    assert (expiry - heartbeat).total_seconds() == 137


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("unknown", "unknown_agent"),
        ("disabled", "disabled"),
        ("stale", "revision_mismatch"),
        ("nonowner", "not_owner"),
        ("missing_task", "missing_task"),
        ("unsupported", "unsupported_phase"),
        ("missing_lease", "lease_missing"),
        ("expired", "lease_expired"),
        ("foreign", "lease_foreign"),
    ),
)
def test_renew_refusals_are_stable_and_write_nothing(board, capsys, case, reason):
    rev = prepare(board, "drafting", "ag")
    data = state.load(board)
    agent, requested_rev = "ag", rev
    if case == "unknown":
        agent = "stranger"
    elif case == "disabled":
        data["enabled"] = False
    elif case == "stale":
        requested_rev -= 1
    elif case == "nonowner":
        agent = "cc"
    elif case == "missing_task":
        data["task"]["id"] = None
    elif case == "unsupported":
        data["task"]["phase"] = "awaiting_draft"
    elif case == "missing_lease":
        data["lock"] = {"holder": None, "acquired_at": None, "expires_at": None}
    elif case == "expired":
        data["lock"]["expires_at"] = "2000-01-01T00:00:00Z"
    elif case == "foreign":
        data["lock"]["holder"] = "cc"
    state.write_atomic(board, data)
    before = state.load(board)

    result = renew_json(
        board, agent, requested_rev, capsys, expected=EXIT_REFUSED
    )

    assert result["renewed"] is False
    assert result["reason"] == reason
    assert reason in RENEW_REFUSALS
    assert result["superseded_rev"] is None
    assert result["old_advance_rev_valid"] is None
    assert result["advance_commands"] == []
    assert state.load(board) == before
    if reason in {"lease_missing", "lease_expired", "lease_foreign"}:
        assert "work or take" in result["message"]


def test_lost_response_retry_and_old_advance_revision_are_safe(board, capsys):
    rev = prepare(board, "drafting", "ag")
    renewed = renew_json(board, "ag", rev, capsys)
    after = state.load(board)

    retry = renew_json(board, "ag", rev, capsys, expected=EXIT_REFUSED)
    assert retry["reason"] == "revision_mismatch"
    assert state.load(board) == after

    assert (
        main(["--dir", board, "advance", "ag", "--rev", str(rev), "--json"])
        == EXIT_REFUSED
    )
    stale_advance = json.loads(capsys.readouterr().out)
    assert stale_advance["reason"] == "revision_mismatch"
    assert stale_advance["rev"] == renewed["rev"]
    assert state.load(board) == after


def test_same_revision_concurrency_renews_once(board):
    rev = prepare(board, "drafting", "ag")
    source = str(Path(__file__).parents[1] / "src")
    script = (
        "import sys;"
        f"sys.path.insert(0, {source!r});"
        "from cordboard.cli import main;"
        f"raise SystemExit(main(['--dir', {board!r}, 'renew', 'ag', "
        f"'--rev', {str(rev)!r}, '--json']))"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    results = [(process.wait(), json.loads(process.stdout.read())) for process in processes]

    assert sorted(code for code, _payload in results) == [0, EXIT_REFUSED]
    assert sum(payload["renewed"] for _code, payload in results) == 1
    after = state.load(board)
    assert after["rev"] == rev + 1
    assert after["history"]["events"] == []


def test_renew_changes_only_lease_agent_liveness_and_revision(
    board, capsys, monkeypatch
):
    rev = prepare(board, "drafting", "ag")
    root = Path(board)
    todo = Path(state.stack_path(board))
    todo_before = todo.read_bytes()
    chat_path = Path(state.chat_path(board))
    chat_path.write_text("do not consume\n")
    artifact = root / "plans" / "renew-work" / "plan.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("do not read or change\n")
    artifact_before = artifact.read_bytes()
    before = state.load(board)

    def unexpected(*args, **kwargs):
        raise AssertionError("renew read an out-of-scope source")

    monkeypatch.setattr(tasks, "read", unexpected)
    monkeypatch.setattr(chat, "unread", unexpected)
    monkeypatch.setattr(cli, "_validate_artifact", unexpected)
    result = renew_json(board, "ag", rev, capsys)
    after = state.load(board)

    assert result["renewed"] is True
    assert todo.read_bytes() == todo_before
    assert chat_path.read_text() == "do not consume\n"
    assert artifact.read_bytes() == artifact_before
    assert after["history"] == before["history"]
    assert after["task"] == before["task"]
    assert after["auto_commit"] == before["auto_commit"]
    assert "brief" not in result and "summary" not in result


def test_human_renew_reports_replacement_revision_and_exact_commands(board, capsys):
    rev = prepare(board, "plan_redlining", "cc")

    assert main(["--dir", board, "renew", "cc", "--rev", str(rev)]) == 0
    output = capsys.readouterr()

    assert output.err == ""
    assert f"superseded_rev={rev} old_advance_rev_valid=no" in output.out
    assert f"advance cc --rev {rev + 1} --verdict approve" in output.out
    assert f"advance cc --rev {rev + 1} --verdict revise" in output.out
    assert f"advance cc --rev {rev + 1} --verdict reject" in output.out


def test_renew_phase_table_is_exact():
    assert set(RENEW_PHASES) == {
        "planning",
        "implementing",
        "reviewing",
        "drafting",
        "plan_redlining",
        "plan_redlined",
        "impl_redlining",
        "impl_redlined",
    }
