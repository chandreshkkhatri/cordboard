"""The spec ships with the package, so it can drift from the code. These stop that.

Prose is not testable, but the things agents parse out of the spec are: the board
schema, the command names, the workflow tables, and the wait exit codes.
"""

from __future__ import annotations

import json
import os
import re

from cordboard import SCHEMA_VERSION, __version__, cli
from cordboard.templates import blank_board
from cordboard.workflows import (
    ADVANCE_MODES,
    ADVANCE_PHASES,
    ADVANCE_REFUSALS,
    BRIEF_DISPOSITIONS,
    BRIEF_GUIDANCE,
    FINISH_REFUSALS,
    PHASES,
    TAKE_MODES,
    TAKE_PHASES,
    TAKE_REFUSALS,
)

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


def test_spec_heading_and_package_metadata_versions_match():
    assert f"schema v{SCHEMA_VERSION}" in spec_text().splitlines()[0]
    with open(os.path.join(_ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        match = re.search(r'^version = "([^"]+)"$', handle.read(), re.M)
    assert match
    assert match.group(1) == __version__


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


def test_every_documented_phase_has_compact_agent_guidance():
    assert "cordboard brief <agent>" in spec_text()
    for workflow, phases in PHASES.items():
        assert set(BRIEF_GUIDANCE[workflow]) == set(phases)
        for phase in phases:
            assert BRIEF_GUIDANCE[workflow][phase]["instructions"]
    for disposition in BRIEF_DISPOSITIONS:
        assert f"`{disposition}`" in spec_text()


def test_take_contract_is_documented_from_code_owned_tables():
    text = spec_text()
    for workflow, mapping in TAKE_PHASES.items():
        assert workflow in text
        for source, target in mapping.items():
            assert f"`{source}`" in text
            assert f"`{target}`" in text
    for value in (*TAKE_MODES, *TAKE_REFUSALS):
        assert f"`{value}`" in text


def test_advance_contract_is_documented_from_code_owned_tables():
    text = spec_text()
    assert "cordboard advance <agent> --rev" in text
    for workflow, mapping in ADVANCE_PHASES.items():
        assert workflow in text
        for source, targets in mapping.items():
            assert f"`{source}`" in text
            for target in targets:
                assert f"`{target}`" in text
    for value in (*ADVANCE_MODES, *ADVANCE_REFUSALS):
        assert f"`{value}`" in text
    assert "resolved path stays\ninside that current task directory" in text


def test_commit_scope_does_not_depend_on_incremental_agent_notes():
    text = spec_text()
    assert "records that file list" not in text
    assert "note; stage exactly" not in text
    assert "task plan and review artifacts" in text
    assert "Ambiguity means no auto-commit" in text
    assert "human_required" in text


def test_degraded_ancillary_context_contract_is_documented():
    text = spec_text()
    assert "board.warnings" in text
    assert "`source`, `code`, and `message`" in text
    assert "missing chat file is healthy empty context" in text
    assert "`chat.available=false`" in text
    assert "does not prevent an otherwise safe active acquisition" in text
    assert "At `done`, a missing or\ninvalid TODO stack" in text
    assert "neither infers\na fallback task from TODO" in text


def test_finish_contract_is_documented():
    text = spec_text()
    assert "cordboard finish <agent> --rev" in text
    assert "status-then-TODO order" in text
    assert "reports `recovered=true`" in text
    assert "has no `--next` option" in text
    assert "remains the human override" in text
    for reason in FINISH_REFUSALS:
        assert f"`{reason}`" in text


def test_bounded_factual_recap_contract_is_documented():
    text = spec_text()
    assert "cordboard recap [TASK_ID] [--json]" in text
    assert "same atomic `status.json` write" in text
    assert "at most 128 events" in text
    assert "`earliest_available_rev`" in text
    assert "`available=false`" in text
    assert "creates no lock" in text
    assert "Event text is never\naccepted from an agent" in text
    assert "Chat, notes, artifact content, summaries, prose, and git diff content" in text
    assert "`reviews` view contains decision events only" in text
    assert "do not create duplicate reviews" in text


def test_no_references_to_the_repo_it_came_from():
    """cordboard is a library now; a leaked host-repo path is a packaging bug."""
    text = spec_text().lower()
    for leak in ("recompose", "collab/bin", "cb-status", "cb-set", "cb-wait", "agent.md"):
        assert leak not in text, f"spec still refers to {leak!r}"
