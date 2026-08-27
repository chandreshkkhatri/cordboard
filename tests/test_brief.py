"""The bounded, read-only handoff an agent consumes on every wake."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from cordboard import chat, state
from cordboard.cli import EXIT_BAD_STATE, EXIT_REFUSED, main
from cordboard.workflows import (
    BRIEF_DISPOSITIONS,
    BRIEF_GUIDANCE,
    IN_PROGRESS,
    PHASES,
    TRANSITIONS,
)

SECTIONS = {"board", "task", "turn", "action", "chat"}


def add_evidence(root: str, data: dict, task_id: str, *, redline: bool = False) -> None:
    task_dir = Path(root) / "plans" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "plan.md").write_text("# Plan\n\nVerify this task.\n")
    data["task"].update(
        {"dir": f"plans/{task_id}", "plan_file": f"plans/{task_id}/plan.md"}
    )
    if redline:
        (task_dir / "redline.md").write_text("# Redline\n\nReviewed.\n")
        data["task"]["last_redline"] = f"plans/{task_id}/redline.md"


def brief_json(root: str, agent: str, capsys) -> dict:
    assert main(["--dir", root, "brief", agent, "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert set(result) == SECTIONS
    return result


def test_guidance_covers_every_phase_of_every_workflow():
    assert set(BRIEF_GUIDANCE) == set(PHASES)
    for workflow, phases in PHASES.items():
        assert set(BRIEF_GUIDANCE[workflow]) == set(phases)
        for phase in phases:
            assert BRIEF_GUIDANCE[workflow][phase]["instructions"]


@pytest.mark.parametrize("fixture_name", ("board", "relay_board"))
def test_every_phase_produces_a_stable_brief(request, fixture_name, capsys):
    root = request.getfixturevalue(fixture_name)
    data = state.load(root)
    workflow = data["workflow"]
    add_evidence(root, data, "all-phases", redline=True)
    for phase, role in PHASES[workflow].items():
        data["task"].update({"id": "all-phases", "phase": phase})
        if phase in {"plan_redlined", "impl_redlined"}:
            data["task"].update(
                {"last_redline": "plans/all-phases/redline.md", "verdict": "revise"}
            )
        state.write_atomic(root, data)
        agent = data["roles"].get(role, "ag")
        output = brief_json(root, agent, capsys)
        assert output["task"]["phase"] == phase
        assert output["action"]["instructions"]
        assert output["turn"]["disposition"] in BRIEF_DISPOSITIONS
        assert output["turn"]["actionable"] is (role is not None)


@pytest.mark.parametrize(
    ("enabled", "phase", "agent", "disposition"),
    (
        (False, "idle", "ag", "disabled"),
        (True, "idle", "ag", "parked"),
        (True, "blocked", "ag", "human_required"),
        (True, "awaiting_draft", "ag", "act"),
        (True, "drafting", "ag", "resume"),
        (True, "awaiting_draft", "cc", "wait"),
    ),
)
def test_disposition_precedence(board, capsys, enabled, phase, agent, disposition):
    data = state.load(board)
    data["enabled"] = enabled
    data["task"].update({"id": "disposition", "phase": phase})
    state.write_atomic(board, data)
    output = brief_json(board, agent, capsys)
    assert output["turn"]["disposition"] == disposition
    assert output["turn"]["actionable"] is (disposition in {"act", "resume"})
    if not output["turn"]["actionable"]:
        assert output["action"]["transitions"] == []
        assert output["action"]["instructions"][0].startswith("Do not act:")
        assert not any(
            "Claim the lease" in item for item in output["action"]["instructions"]
        )


@pytest.mark.parametrize(
    ("enabled", "agent", "disposition"),
    ((False, "ag", "disabled"), (True, "cc", "wait")),
)
def test_nonactionable_agent_never_receives_phase_owner_instructions(
    board, capsys, enabled, agent, disposition
):
    data = state.load(board)
    data["enabled"] = enabled
    data["task"].update({"id": "not-yours", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    output = brief_json(board, agent, capsys)
    instructions = output["action"]["instructions"]
    assert output["turn"]["disposition"] == disposition
    assert output["action"]["transitions"] == []
    assert instructions and all(item.startswith("Do not act:") for item in instructions)
    assert not any("Claim the lease" in item or "Write " in item for item in instructions)


def test_json_has_stable_board_task_turn_action_chat_sections(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "shape", "title": "Stable shape", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    output = brief_json(board, "ag", capsys)
    assert set(output["board"]) == {
        "enabled",
        "workflow",
        "rev",
        "auto_commit",
        "max_rounds",
        "warnings",
    }
    assert set(output["turn"]) == {"agent", "owner", "disposition", "actionable", "reason"}
    assert set(output["action"]) == {"instructions", "transitions"}
    assert set(output["chat"]) == {"available", "unread", "cursor", "available_through"}
    assert output["board"]["warnings"] == []
    assert output["chat"]["available"] is True
    assert {
        "id",
        "title",
        "kind",
        "phase",
        "dir",
        "plan_file",
        "last_redline",
        "verdict",
        "round",
        "next_round",
        "redline_path",
        "notes",
    } == set(output["task"])


def test_brief_peeks_at_chat_without_advancing_cursor(board, capsys):
    chat.append(board, "cc", "ag", "Remember compatibility")
    before = state.load(board)
    output = brief_json(board, "ag", capsys)
    assert "Remember compatibility" in output["chat"]["unread"]
    assert output["chat"]["cursor"] == before["agents"]["ag"]["chat_cursor"]
    assert state.load(board) == before


def test_brief_creates_no_locks_and_modifies_no_files(board, capsys):
    root = Path(board)
    assert not (root / ".chat.lock").exists()
    assert not (root / ".todo.lock").exists()
    names = {path.name for path in root.iterdir()}
    snapshots = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.iterdir()
        if path.is_file()
    }
    brief_json(board, "ag", capsys)
    assert {path.name for path in root.iterdir()} == names
    for name, snapshot in snapshots.items():
        path = root / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == snapshot


def test_unknown_agent_is_exit_1_without_stdout(board, capsys):
    assert main(["--dir", board, "brief", "stranger", "--json"]) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown agent" in captured.err


@pytest.mark.parametrize("cursor", (-1, True, "0", None))
def test_invalid_chat_cursor_is_exit_2_without_stdout(board, capsys, cursor):
    data = state.load(board)
    data["agents"]["ag"]["chat_cursor"] = cursor
    state.write_atomic(board, data)
    assert main(["--dir", board, "brief", "ag", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "chat_cursor must be a non-negative integer" in captured.err


def test_invalid_board_never_produces_a_partial_brief(board, capsys):
    data = state.load(board)
    data["task"]["phase"] = "invented"
    state.write_atomic(board, data)
    assert main(["--dir", board, "brief", "ag", "--json"]) == EXIT_BAD_STATE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("cordboard:")


@pytest.mark.parametrize("damage", ("missing", "invalid"))
def test_active_brief_degrades_when_todo_is_unavailable(board, capsys, damage):
    root = Path(board)
    data = state.load(board)
    data["task"].update({"id": "keep-working", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    if damage == "missing":
        (root / "TODO.md").unlink()
    else:
        (root / "TODO.md").write_text("- [ ] malformed\n")
    output = brief_json(board, "ag", capsys)
    assert output["turn"]["disposition"] == "act"
    assert output["action"]["transitions"]
    assert output["board"]["warnings"] == [
        {
            "source": "todo",
            "code": damage,
            "message": output["board"]["warnings"][0]["message"],
        }
    ]


def test_missing_chat_is_healthy_available_empty_context_in_json(board, capsys):
    missing = brief_json(board, "ag", capsys)
    assert missing["chat"] == {
        "available": True,
        "unread": "",
        "cursor": 0,
        "available_through": 0,
    }
    assert missing["board"]["warnings"] == []


def test_missing_chat_renders_as_normal_empty_context_for_humans(board, capsys):
    assert not Path(state.chat_path(board)).exists()
    assert main(["--dir", board, "brief", "ag"]) == 0
    output = capsys.readouterr().out
    assert "chat:\n  (none)\n" in output
    assert "(unavailable)" not in output


def test_available_empty_chat_has_the_same_healthy_contract(board, capsys):
    Path(state.chat_path(board)).write_text("")
    available = brief_json(board, "ag", capsys)
    assert available["chat"]["available"] is True
    assert available["chat"]["unread"] == ""
    assert available["chat"]["available_through"] == 0


def test_malformed_chat_is_a_stable_unavailable_warning(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "keep-working", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    Path(state.chat_path(board)).write_bytes(b"\xff")
    output = brief_json(board, "ag", capsys)
    assert output["turn"]["disposition"] == "act"
    assert output["chat"]["available"] is False
    assert output["chat"]["unread"] == ""
    assert output["board"]["warnings"][0]["source"] == "chat"
    assert output["board"]["warnings"][0]["code"] == "invalid_utf8"
    assert set(output["board"]["warnings"][0]) == {"source", "code", "message"}


def test_simultaneous_todo_and_chat_failures_have_deterministic_exact_warnings(
    board, capsys
):
    root = Path(board)
    data = state.load(board)
    data["task"].update({"id": "two-warnings", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    (root / "TODO.md").write_text("- [ ] malformed\n")
    Path(state.chat_path(board)).write_bytes(b"\xff")
    output = brief_json(board, "ag", capsys)
    assert output["board"]["warnings"] == [
        {
            "source": "todo",
            "code": "invalid",
            "message": (
                f"invalid task at {root / 'TODO.md'}:1; "
                "expected `- [ ] <id> — <title>`"
            ),
        },
        {
            "source": "chat",
            "code": "invalid_utf8",
            "message": f"{state.chat_path(board)} is not valid UTF-8",
        },
    ]
    assert output["chat"] == {
        "available": False,
        "unread": "",
        "cursor": 0,
        "available_through": None,
    }


def test_unknown_agent_is_checked_before_broken_ancillary_files(board, capsys):
    Path(board, "TODO.md").write_text("- [ ] malformed\n")
    Path(state.chat_path(board)).write_bytes(b"\xff")
    assert main(["--dir", board, "brief", "stranger", "--json"]) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown agent" in captured.err


def test_done_with_invalid_todo_requires_human_and_omits_completion(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "done-invalid-queue", "phase": "done"})
    add_evidence(board, data, "done-invalid-queue")
    state.write_atomic(board, data)
    Path(board, "TODO.md").write_text("- [ ] malformed\n")
    output = brief_json(board, "ag", capsys)
    assert output["turn"]["disposition"] == "human_required"
    assert output["action"]["transitions"] == []
    assert "valid TODO stack" in output["turn"]["reason"]


@pytest.mark.parametrize("phase", ("plan_submitted", "plan_redlined"))
def test_missing_required_artifacts_are_a_hard_brief_gate(board, capsys, phase):
    data = state.load(board)
    data["task"].update(
        {"id": "missing-evidence", "phase": phase, "verdict": "revise"}
    )
    state.write_atomic(board, data)
    agent = "cc" if phase == "plan_submitted" else "ag"
    output = brief_json(board, agent, capsys)
    assert output["turn"]["disposition"] == "human_required"
    assert output["action"]["transitions"] == []
    assert "task.dir does not exist" in output["turn"]["reason"]


def test_plan_context_is_executable_without_the_spec(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "draft-me", "phase": "awaiting_draft"})
    state.write_atomic(board, data)
    output = brief_json(board, "ag", capsys)
    assert output["task"]["dir"] == "plans/draft-me"
    assert output["task"]["plan_file"] == "plans/draft-me/plan.md"
    commands = output["action"]["transitions"]
    assert any("--key task.dir" in command for command in commands)
    assert any("--key task.plan_file" in command for command in commands)
    assert any("plans/draft-me/plan.md" in item for item in output["action"]["instructions"])


@pytest.mark.parametrize(
    ("phase", "gate"), (("plan_submitted", "plan"), ("impl_submitted", "impl"))
)
def test_redline_gate_includes_round_verdict_and_path(board, capsys, phase, gate):
    data = state.load(board)
    data["task"].update(
        {"id": "redline", "phase": phase, "dir": "plans/redline", "round": {"plan": 1, "impl": 0}}
    )
    add_evidence(board, data, "redline")
    state.write_atomic(board, data)
    output = brief_json(board, "cc", capsys)
    expected = data["task"]["round"][gate] + 1
    assert output["task"]["next_round"] == expected
    assert output["task"]["redline_path"].endswith(f"redline-{gate}-r{expected}.md")
    handoffs = [item for item in output["action"]["transitions"] if "--verdict" in item]
    assert {"approve", "revise"} <= {
        shlex.split(item)[shlex.split(item).index("--verdict") + 1] for item in handoffs
    }
    assert all(f"--bump-round {gate}" in item for item in handoffs)
    assert all("--key task.last_redline" in item for item in handoffs)


def test_relay_plan_only_finishes_without_author_handoff(relay_board, capsys):
    data = state.load(relay_board)
    data["task"].update({"id": "plan-only", "kind": "plan_only", "phase": "awaiting_plan"})
    state.write_atomic(relay_board, data)
    output = brief_json(relay_board, "cc", capsys)
    commands = output["action"]["transitions"]
    assert any("plan_only" in item for item in output["action"]["instructions"])
    assert any("--phase done" in item for item in commands)
    assert not any("--phase plan_ready" in item for item in commands)


def test_relay_review_includes_clean_and_issue_branches(relay_board, capsys):
    data = state.load(relay_board)
    data["task"].update({"id": "review", "phase": "awaiting_review"})
    add_evidence(relay_board, data, "review")
    state.write_atomic(relay_board, data)
    output = brief_json(relay_board, "cc", capsys)
    targets = {
        parts[parts.index("--phase") + 1]
        for command in output["action"]["transitions"]
        if "--phase" in (parts := shlex.split(command))
    }
    assert targets == {"reviewing", "done", "plan_ready"}
    instructions = output["action"]["instructions"]
    assert any("Clean → done" in item and "verification" in item for item in instructions)
    assert any(
        "Issues → plan_ready" in item and "write a fix-plan" in item for item in instructions
    )


@pytest.mark.parametrize("auto_commit", (True, False))
def test_done_makes_auto_commit_policy_explicit(board, capsys, auto_commit):
    data = state.load(board)
    data["auto_commit"] = auto_commit
    data["task"].update({"id": "done", "phase": "done"})
    add_evidence(board, data, "done")
    state.write_atomic(board, data)
    output = brief_json(board, "ag", capsys)
    expected = "enabled" if auto_commit else "disabled"
    assert expected in output["action"]["instructions"][0]
    assert output["action"]["transitions"][0].endswith(
        f"finish ag --rev {output['board']['rev']}"
    )
    assert not any("task complete" in item for item in output["action"]["transitions"])
    if auto_commit:
        guidance = " ".join(output["action"]["instructions"])
        assert "task plan and review artifacts" in guidance
        assert "git diff/status" in guidance
        assert "never infer ownership from an agent note or summary" in guidance
        assert "human-required" in guidance
        assert any("--phase blocked" in item for item in output["action"]["transitions"])
    else:
        assert not any("--phase blocked" in item for item in output["action"]["transitions"])


def test_blocked_brief_surfaces_agent_note(board, capsys):
    data = state.load(board)
    data["task"].update({"id": "blocked", "phase": "blocked"})
    data["agents"]["ag"]["note"] = "Need a compatibility decision"
    state.write_atomic(board, data)
    output = brief_json(board, "ag", capsys)
    assert output["turn"]["disposition"] == "human_required"
    assert output["task"]["notes"] == {"ag": "Need a compatibility decision"}


def test_solo_mapping_can_act_for_reviewer_phase(board, capsys):
    data = state.load(board)
    data["roles"] = {"reviewer": "ag", "author": "ag"}
    data["task"].update({"id": "solo", "phase": "plan_submitted"})
    add_evidence(board, data, "solo")
    state.write_atomic(board, data)
    output = brief_json(board, "ag", capsys)
    assert output["turn"]["owner"] == "ag"
    assert output["turn"]["disposition"] == "act"


def test_every_emitted_phase_transition_is_reachable(request, capsys):
    for fixture_name in ("board", "relay_board"):
        root = request.getfixturevalue(fixture_name)
        data = state.load(root)
        workflow = data["workflow"]
        add_evidence(root, data, "legal", redline=True)
        for phase, role in PHASES[workflow].items():
            data["task"].update(
                {
                    "id": "legal",
                    "phase": phase,
                    "last_redline": "plans/legal/redline.md",
                    "verdict": "revise",
                }
            )
            state.write_atomic(root, data)
            agent = data["roles"].get(role, "ag")
            output = brief_json(root, agent, capsys)
            sources = {phase}
            for command in output["action"]["transitions"]:
                parts = shlex.split(command)
                if "--phase" not in parts:
                    continue
                target = parts[parts.index("--phase") + 1]
                assert target in PHASES[workflow]
                assert any(
                    target == "blocked"
                    or target == source
                    or target in TRANSITIONS[workflow].get(source, set())
                    for source in sources
                ), f"illegal brief transition {workflow}: {sources} -> {target}"
                if target in IN_PROGRESS or target == phase:
                    sources.add(target)
