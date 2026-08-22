"""The spec ships with the package, so it can drift from the code. These stop that.

Prose is not testable, but the things agents parse out of the spec are: the board
schema, the command names, the workflow tables, and the wait exit codes.
"""

from __future__ import annotations

import json
import os
import re

from cordboard import cli
from cordboard.templates import blank_board
from cordboard.workflows import PHASES

_ROOT = os.path.dirname(os.path.dirname(__file__))
SPEC = os.path.join(_ROOT, "src", "cordboard", "data", "instructions.md")


def spec_text() -> str:
    with open(SPEC, encoding="utf-8") as handle:
        return handle.read()


def _keys(data: dict, prefix: str = "") -> set[str]:
    found = set()
    for key, value in data.items():
        found.add(prefix + key)
        if isinstance(value, dict):
            found |= _keys(value, f"{prefix}{key}.")
    return found


def test_documented_schema_matches_a_fresh_board():
    """The JSON block in the spec is what `init` actually writes — every key, both ways."""
    block = re.search(r"`status\.json`, in full.*?```json\n(.*?)```", spec_text(), re.S)
    assert block, "the spec no longer contains the board schema block"
    documented = json.loads(block.group(1))
    actual = blank_board("redline", "cc", "ag")

    assert _keys(documented) == _keys(actual)
    for field in ("version", "enabled", "workflow", "max_rounds", "lease_seconds", "auto_commit"):
        assert documented[field] == actual[field], field


def test_every_subcommand_is_documented():
    text = spec_text()
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = {name for action in actions for name in action.choices if isinstance(name, str)}
    for command in commands:
        assert f"cordboard {command}" in text, f"`cordboard {command}` is undocumented"


def test_wait_exit_codes_are_documented():
    """An agent's tick branches on these numbers, so the table must not go stale."""
    text = spec_text()
    for code in (cli.WAIT_TURN, cli.WAIT_DISABLED, cli.WAIT_TIMEOUT, cli.EXIT_BAD_STATE):
        assert f"| `{code}` |" in text, f"exit {code} missing from the spec's table"


def test_every_phase_of_every_workflow_is_named():
    text = spec_text()
    for workflow, phases in PHASES.items():
        assert f"## Workflow: `{workflow}`" in text, workflow
        for phase in phases:
            assert f"`{phase}`" in text, f"{workflow}.{phase} is undocumented"


def test_no_references_to_the_repo_it_came_from():
    """cordboard is a library now; a leaked host-repo path is a packaging bug."""
    text = spec_text().lower()
    for leak in ("recompose", "collab/bin", "cb-status", "cb-set", "cb-wait", "agent.md"):
        assert leak not in text, f"spec still refers to {leak!r}"
