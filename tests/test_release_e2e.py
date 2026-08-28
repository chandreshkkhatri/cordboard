"""Release journeys use only the public JSON command surface for board state."""

from __future__ import annotations

import json
from pathlib import Path

from cordboard.cli import main


def command_json(root: str, capsys, *args: str) -> dict:
    assert main(["--dir", root, *args, "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def create_task(tmp_path, capsys, workflow: str, task_id: str) -> str:
    root = str(tmp_path / workflow)
    assert main([
        "--dir", root, "init", "--workflow", workflow, "--reviewer", "cc",
        "--author", "ag",
    ]) == 0
    capsys.readouterr()
    todo = Path(root, "TODO.md")
    todo.write_text(todo.read_text() + f"\n- [ ] {task_id} — Release journey\n")
    assert main(["--dir", root, "set", "--enabled", "true", "--quiet"]) == 0
    assert main(["--dir", root, "task", "start"]) == 0
    capsys.readouterr()
    return root


def write_artifact(root: str, relative: str, body: str = "# Evidence\n") -> None:
    path = Path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def advance(root: str, capsys, agent: str, rev: int, *options: str) -> dict:
    return command_json(root, capsys, "advance", agent, "--rev", str(rev), *options)


def assert_digest_contract(
    digest: dict,
    status: dict,
    expected: list[tuple[str, str, str, str]],
    artifacts: list[str],
) -> list[dict]:
    assert digest["requested_since"] == 0
    assert digest["through_rev"] == status["rev"]
    assert digest["gap"] is False
    assert digest["complete"] is True
    assert digest["event_count"] == len(expected)
    assert len(digest["tasks"]) == 1
    group = digest["tasks"][0]
    events = group["lifecycle"]
    assert group["event_count"] == digest["event_count"] == len(events)
    assert [(e["action"], e["from_phase"], e["phase"], e["mode"]) for e in events] == expected
    assert len({event["rev"] for event in events}) == len(events)
    assert group["artifacts"] == artifacts
    assert group["completion"] == events[-1]
    assert group["completion"]["action"] == "finish"
    assert group["completion"]["phase"] == "idle"
    assert group["completion"]["mode"] == "parked"
    return events


def test_redline_release_journey_revises_then_approves_without_state_patching(
    tmp_path, capsys
):
    root = create_task(tmp_path, capsys, "redline", "redline-release")

    author = command_json(root, capsys, "work", "ag")
    write_artifact(root, author["brief"]["task"]["plan_file"], "# Plan\n\nShip safely.\n")
    renewed = command_json(root, capsys, "renew", "ag", "--rev", str(author["rev"]))
    advance(root, capsys, "ag", renewed["rev"])

    reviewer = command_json(root, capsys, "work", "cc")
    write_artifact(root, reviewer["brief"]["task"]["redline_path"])
    advance(root, capsys, "cc", reviewer["rev"], "--verdict", "revise")
    response = command_json(root, capsys, "work", "ag")
    implementing = advance(root, capsys, "ag", response["rev"])
    advance(root, capsys, "ag", implementing["rev"])

    reviewer = command_json(root, capsys, "work", "cc")
    write_artifact(root, reviewer["brief"]["task"]["redline_path"])
    advance(root, capsys, "cc", reviewer["rev"], "--verdict", "approve")
    response = command_json(root, capsys, "work", "ag")
    advance(root, capsys, "ag", response["rev"])
    passive = command_json(root, capsys, "work", "ag")
    assert passive["acquired"] is False
    finished = command_json(root, capsys, "finish", "ag", "--rev", str(passive["rev"]))

    digest = command_json(root, capsys, "digest", "--since", "0")
    status = command_json(root, capsys, "status")
    assert finished["next_phase"] == "idle"
    assert status["task"]["phase"] == "idle"
    assert status["_task_stack"]["completed"] == 1
    events = assert_digest_contract(
        digest,
        status,
        [
            ("take", "awaiting_draft", "drafting", "started"),
            ("advance", "drafting", "plan_submitted", "handed_off"),
            ("take", "plan_submitted", "plan_redlining", "started"),
            ("advance", "plan_redlining", "plan_redlined", "handed_off"),
            ("take", "plan_redlined", "plan_redlined", "claimed"),
            ("advance", "plan_redlined", "implementing", "continued"),
            ("advance", "implementing", "impl_submitted", "handed_off"),
            ("take", "impl_submitted", "impl_redlining", "started"),
            ("advance", "impl_redlining", "impl_redlined", "handed_off"),
            ("take", "impl_redlined", "impl_redlined", "claimed"),
            ("advance", "impl_redlined", "done", "finished"),
            ("finish", "done", "idle", "parked"),
        ],
        [
            "plans/redline-release/plan.md",
            "plans/redline-release/redline-plan-r1.md",
            "plans/redline-release/redline-impl-r1.md",
        ],
    )
    assert all(event["action"] != "renew" for event in events)
    decisions = [
        review["verdict"] for group in digest["tasks"] for review in group["reviews"]
    ]
    assert decisions == ["revise", "approve"]


def test_relay_release_journey_records_issues_then_clean_and_finishes(tmp_path, capsys):
    root = create_task(tmp_path, capsys, "relay", "relay-release")

    planner = command_json(root, capsys, "work", "cc")
    write_artifact(root, planner["brief"]["task"]["plan_file"], "# Plan\n\nRelay.\n")
    renewed = command_json(root, capsys, "renew", "cc", "--rev", str(planner["rev"]))
    advance(root, capsys, "cc", renewed["rev"])
    author = command_json(root, capsys, "work", "ag")
    advance(root, capsys, "ag", author["rev"])
    reviewer = command_json(root, capsys, "work", "cc")
    advance(root, capsys, "cc", reviewer["rev"], "--outcome", "issues")
    author = command_json(root, capsys, "work", "ag")
    advance(root, capsys, "ag", author["rev"])
    reviewer = command_json(root, capsys, "work", "cc")
    advance(root, capsys, "cc", reviewer["rev"], "--outcome", "clean")
    passive = command_json(root, capsys, "work", "cc")
    command_json(root, capsys, "finish", "cc", "--rev", str(passive["rev"]))

    digest = command_json(root, capsys, "digest", "--since", "0")
    status = command_json(root, capsys, "status")
    events = assert_digest_contract(
        digest,
        status,
        [
            ("take", "awaiting_plan", "planning", "started"),
            ("advance", "planning", "plan_ready", "handed_off"),
            ("take", "plan_ready", "implementing", "started"),
            ("advance", "implementing", "awaiting_review", "handed_off"),
            ("take", "awaiting_review", "reviewing", "started"),
            ("advance", "reviewing", "plan_ready", "handed_off"),
            ("take", "plan_ready", "implementing", "started"),
            ("advance", "implementing", "awaiting_review", "handed_off"),
            ("take", "awaiting_review", "reviewing", "started"),
            ("advance", "reviewing", "done", "finished"),
            ("finish", "done", "idle", "parked"),
        ],
        ["plans/relay-release/plan.md"],
    )
    reviews = [review for group in digest["tasks"] for review in group["reviews"]]
    assert [event["outcome"] for event in reviews] == ["issues", "clean"]
    assert all(event["action"] != "renew" for event in events)
    assert status["task"]["phase"] == "idle"
    assert status["_task_stack"]["pending"] == 0
    assert status["_task_stack"]["completed"] == 1
