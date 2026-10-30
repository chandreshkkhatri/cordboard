# cordboard

**A file-based baton for multi-agent coding collaboration — no server, no broker, zero runtime dependencies.**

Two or more coding agents work a shared repository task stack without stepping on each other. One agent holds the baton at a time; the board manages ownership, enforces review gates, tracks leases, and prevents race conditions using pure filesystem primitives.

```bash
# Initialize a board in your repo
cordboard init

# Queue tasks in cordboard/TODO.md, then enable and start
cordboard set --enabled true
cordboard task start

# Agent turn lifecycle
cordboard work ag       # Normal wake: observe or atomically acquire work
cordboard advance ag --rev 7  # Atomically finish the work beat and hand off
cordboard recap         # Reconstruct retained machine-authored task history
cordboard digest --since 0    # Discover retained task facts for a later summary
```

---

## Highlights

- 📁 **100% File-Based & Local**: State lives in `cordboard/status.json`, tasks in `cordboard/TODO.md`, backchannel in `cordboard/chat.md`, and plans in `cordboard/plans/`. Everything is inspectable with `cat` and reviewable in `git diff`.
- 🔒 **Rock-Solid Concurrency & Crash Recovery**: State updates use advisory `flock`, compare-and-swap (CAS) revision counters, and atomic `os.replace` tempfiles. Leases expire automatically, allowing recovery from crashed agents without human intervention.
- ⚡ **Token-Efficient Agent Turns**: Agents don't need to re-read a 600-line specification or narrate every step. One `work` call observes or atomically acquires the turn; revision-bound `advance` completes it, and `recap` reconstructs factual history so a summary can be written later.
- 🔄 **Proven Multi-Agent Review Workflows**: Built-in `redline` (author plans & implements with reviewer commentary and round caps) and `relay` (reviewer plans, author builds, reviewer audits).
- 💬 **Backchannel Messaging**: Append-only agent chat (`cordboard say`) with per-agent cursor tracking and automatic log rotation.
- ⏱️ **Zero-Token Waiting (`watch`) with Watchdog (`tick`)**: `cordboard wait <agent>` blocks on file updates without consuming LLM turn tokens. Low-frequency `tick` timers serve as fallback watchdogs if a process drops.
- 📦 **Zero Runtime Dependencies**: Written entirely in Python standard library (`>=3.10`). Runs anywhere—in containers, CI, virtualenvs, or agent sandboxes.

---

## Installation

```bash
# Run directly without installing (once published)
uvx cordboard init
pipx run cordboard init

# Or install from source
git clone https://github.com/chandreshkkhatri/cordboard && cd cordboard
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Every command accepts `--dir <path>` or honors `$CORDBOARD_DIR`, so the board does not have to live at `./cordboard`.

---

## Quickstart

### 1. Initialize the board

Run inside your git repository:

```bash
cordboard init
```

This generates:
- `cordboard/instructions.md`: The complete protocol specification for agents (committed).
- `cordboard/TODO.md`: The LIFO task queue (committed).
- `cordboard/.gitignore`: Ignores runtime state, locks, chat, and task artifacts.
- `cordboard/status.json`: Runtime state file and baton lease (local).
- `cordboard/chat.md`: Agent back-channel log (local).

### 2. Queue work

Edit `cordboard/TODO.md`. Tasks use Markdown checkbox syntax; **the bottom item runs first (LIFO)**:

```markdown
# TODO

- [ ] polish-ui — Clean up table alignments
- [ ] add-tests — Write regression tests for parser
- [ ] auth-flow — Implement token refresh logic
```

### 3. Enable the board and start the first task

```bash
cordboard set --enabled true
cordboard task start
```

`task start` takes the bottom-most pending task (`auth-flow`), initializes its phase (`awaiting_draft` in `redline` or `awaiting_plan` in `relay`), and sets up plan directory metadata.

### 4. Agent Execution Loop

Agents run a compact, token-efficient loop:

```bash
# 1. Observe passive state or atomically acquire actionable work
cordboard work ag

# 2. Perform the work (draft plan, implement, review diff, etc.)
# For a long beat, renew before expiry and replace the revision token
cordboard renew ag --rev 4

# renew succeeds at rev 5; execute the replacement command printed by renew
cordboard advance ag --rev 5

# advance commits rev 6; passive done work prints finish at that current rev
cordboard work ag
cordboard finish ag --rev 6

# 5. Consume read chat messages when needed
cordboard say --read ag
```

---

## Workflows

`cordboard` provides two pre-configured workflows tailored to agent collaboration:

| Workflow | Roles | Dynamic & Review Gates |
|---|---|---|
| **`redline`** *(default)* | Author (`author`), Reviewer (`reviewer`) | **Author holds the pen for plan & code.** The reviewer provides structured feedback at two gates (Plan and Implementation). Reviewer verdicts: `approve` (proceed), `revise` (author updates and proceeds), or `reject` (re-draft). A round limit (`max_rounds`, default 3) guards against infinite revision loops. |
| **`relay`** | Author (`author`), Reviewer (`reviewer`) | **Specialized handoffs.** Reviewer plans the task (`planning`), Author implements the plan (`implementing`), Reviewer audits the scoped diff (`reviewing`). |

### Redline Phase Graph

```
[awaiting_draft] ──(take)──> [drafting] ──(submit)──> [plan_submitted]
                                                            │ (take)
                                                            ▼
[implementing] <──(approve/revise)── [plan_redlined] <── [plan_redlining]
       │                                                    │ (reject)
       ▼                                                    └──> [drafting]
[impl_submitted] ──(take)──> [impl_redlining]
                                   │
      ┌──────────────┬─────────────┘
      │ (approve)    │ (revise)                     (reject)
      ▼              ▼                                  │
    [done] ◄── [impl_redlined] ──(re-implement)─────────┘
```

### Roles & Solo Mode

Phases are mapped to roles (`author` and `reviewer`), which map to agent names in `status.json`:

```json
"roles": {
  "reviewer": "cc",
  "author": "ag"
}
```

To run an agent in **solo mode**, simply map both roles to the same agent name:
```json
"roles": {
  "reviewer": "ag",
  "author": "ag"
}
```
No separate flags or mode settings are needed—the agent will own every phase and gate transition automatically.

---

## Agent Handoffs & The Briefing Contract

Rather than ingesting the entire `instructions.md` on every cycle, an agent starts every normal
wake with `cordboard work <agent>`.

The normal wake is `cordboard work <agent>`. It returns the same bounded brief without writing when
the agent should wait, the board is disabled/parked/blocked, evidence is unsafe, or the task is at
done. When work is actionable, it atomically performs the exact `take` acquisition and returns the
post-take brief in the same response. `brief` and `take` remain available as lower-level primitives.

### Dispositions

The brief inspects board state and returns one of six deterministic dispositions (in precedence order):

| Disposition | Meaning | Action Needed |
|---|---|---|
| `disabled` | Master switch is off (`enabled: false`) | `work` returns `acquired=false`; follow the passive reason and do nothing |
| `parked` | Queue is empty (`phase: idle`) | `work` returns `acquired=false`; wait for queued work |
| `human_required` | Task is blocked (`phase: blocked`) or missing review data | `work` returns `acquired=false`; surface the supplied reason to an operator |
| `act` | Agent owns a stable handoff phase (`awaiting_draft`, `plan_ready`, etc.) | `work` acquires it; with `acquired=true`, begin the returned instructions and do not call `take` |
| `resume` | Agent owns an in-progress phase (`drafting`, `implementing`, etc.) | `work` renews it; with `acquired=true`, resume the returned instructions and do not call `take` |
| `wait` | Another agent owns the current phase | `work` returns `acquired=false`; follow the passive brief and wait |

### Degraded ancillary context

`work`, `brief`, and `take` validate board state and the agent before reading advisory files. Missing or
malformed `TODO.md` and unreadable chat do not strand active work: the brief exposes stable
`board.warnings` objects (`source`, `code`, `message`), while `chat.available` distinguishes an
unavailable log from healthy empty context, including a missing chat file. They never repair,
create, or consume those files. A valid TODO stack becomes mandatory again at `done`; without it the brief is
`human_required` and provides no completion transition. Required plan and redline evidence remains
a hard gate throughout.

### Atomic Work Acquisition: `cordboard take`

`take` is the lower-level diagnostic/integration acquisition primitive used by `work`. When called
directly after a diagnostic brief, `cordboard take <agent>`:
1. Locks state and re-verifies ownership under `flock`.
2. Claims/renews the lease (`lock.holder` & `lock.expires_at`).
3. Transitions phase from stable handoff to active work (`awaiting_draft` → `drafting`).
4. Fills missing plan directory/file metadata.
5. Returns the post-take briefing with exact safe transition commands.

### Atomic Work Completion: `cordboard advance`

The acquired brief returned by `work` (or the lower-level post-take brief) prints the exact
`advance <agent> --rev <revision>` command for the active phase. `advance` rechecks the revision,
owner, live lease, task, phase, and required artifacts inside
one locked state update, then either completes the beat once or writes nothing. Relay reviews add
`--outcome clean|issues`; redline gates add `--verdict approve|revise|reject` and may use
`--artifact <path>` for a non-default redline file.

After a successful renewal, use the replacement advance command returned by `renew`; the earlier
work/take revision has been superseded.

The current `task.dir` must resolve inside the board's `plans/` directory. Plan and redline
artifacts must be regular, non-empty UTF-8 files whose resolved paths remain inside that task
directory, so another task's evidence cannot be substituted through a path or symlink. Cross-agent
handoffs and `done` release the lease; same-agent continuations keep it. The command does not edit
TODO, chat, notes, artifacts, or git, and does not require an incremental summary.

At `done`, an auto-committing agent derives a conservative candidate file set from the task plan
and review artifacts, then reconciles it with the current git diff/status. It commits only changes
with unambiguous task ownership. Ambiguity disables auto-commit for that task: the agent leaves the
task incomplete and uses the supplied blocked transition to request a human scope decision.

### Lease Renewal: `cordboard renew`

For a long acquired phase, run `cordboard renew <agent> --rev REV [--json]` before the lease expires.
It atomically rechecks the enabled board, exact revision, phase owner, task, supported acquired phase,
and live agent-owned lease; then it extends the configured TTL, refreshes heartbeat/working status,
and returns a replacement revision plus the exact `advance` command alternatives.

The prior advance revision is explicitly invalid after success. Retrying a lost-response renewal
with that old revision safely refuses; use the returned revision, or recover an expired/missing
lease through the next normal `work` wake. Renew does not rebrief, record history, inspect TODO/chat
or artifacts/git, create a summary, hand off, or advance work.

### Retry-Safe Task Completion: `cordboard finish`

At `done`, the brief prints `finish <agent> --rev <revision>`. Finish validates the enabled board,
current owner, exact revision, done task, and TODO membership under the status-then-TODO lock order.
It always checks off that task and installs the next LIFO item at its workflow entry phase, or parks
at `idle`; there is no `--next` choice and no automatic lease. If a crash lands the TODO check-off
before the state commit, the same command and revision recover from the already-checked item.

Finish does not invoke git, consume chat, write a summary, or infer a successor outside TODO.
`cordboard task complete [--next]` remains available as a manual human override.

### Bounded Factual History: `cordboard recap`

Each successful `take`, `advance`, and `finish` records one machine-authored lifecycle event in the
same atomic state write. `cordboard recap [TASK_ID] [--json]` turns the retained events into a
deterministic lifecycle, handoff, review, completion, and artifact-reference view. This lets agents
stay focused on work and produce a human summary later.

The ring is capped at 128 events and persists `incomplete_through_rev`, the exact highest revision
through which lifecycle facts may be missing. Events contain only fixed facts—revision/time, task identity, actor/action, phase/mode,
review result, lease disposition, and referenced artifact paths. They never copy chat, notes,
artifact contents, diffs, prose, or agent-supplied event text. `recap` is strictly read-only and
creates no lock or cursor write.

The `reviews` view includes only actual decisions: redline gate advances and relay review outcomes.
Verdicts carried into later response, take, completion, or finish events are not counted again.

### Deferred Multi-Task Digest: `cordboard digest`

`cordboard digest --since REV [--json]` returns retained events with `event.rev > REV`, grouped by
task in first-event order. Each factual group contains its ordered lifecycle, decision-only reviews,
released-lease handoffs, completion, and deduplicated artifact references. The response does not
repeat a top-level event list.

Use `through_rev` as the next cursor even when no lifecycle event occurred. `gap` is true exactly
when `--since < incomplete_through_rev`; such results are explicitly partial. Migration sets the
boundary to its current revision, and eviction advances it to the highest discarded event, so later
non-history revisions cannot blur completeness. Digest rejects negative and future revisions. It
reads only validated `status.json`: no locks, writes, cursor changes, TODO/chat/artifact reads, or
generated prose.

---

## Triggers: `watch` vs `tick`

Each agent can be woken up via two complementary mechanisms:

- **`watch` (`cordboard wait <agent>`)** *(Primary, token-free)*:
  Blocks until the baton belongs to that agent. The process sleeps on filesystem events/polling and exits immediately when it's the agent's turn. **Zero tokens spent while waiting.**
  - Exit `0`: It is your turn.
  - Exit `10`: Board is disabled.
  - Exit `11`: Wait timed out (`--timeout`).
  - Exit `2`: Unusable / invalid board state.

- **`tick`** *(Fallback watchdog)*:
  A slow recurring timer (e.g. `*/20` min cron) that wakes the agent to check if the loop stalled due to an interrupted process or dead session.

```bash
# Check if a handoff was stalled/missed
cordboard status --stalled    # exit 0 = watcher missed handoff
```

---

## Agent Prompt Template

Configure your agent runner, cron job, or loop wrapper with this prompt:

> You are `<agent-name>` in a cordboard loop.
> 1. Start every normal wake with `cordboard work <agent-name>`.
> 2. If `acquired=true`, begin the returned phase instructions and do not call `take` again.
> 3. If `acquired=false`, follow the returned passive brief and reason without changing state.
> 4. Consume incorporated backchannel messages with `cordboard say --read <agent-name>`.
> 5. Use `brief` or `take` only for diagnostics/integration recovery, and read
> `cordboard/instructions.md` only when that compact context is insufficient.
> Do nothing outside the current task.

---

## CLI Reference

| Command | Description |
|---|---|
| `cordboard init [--migrate]` | Initialize `./cordboard/` in the repository. `--migrate` upgrades schema v2-v5 boards. |
| `cordboard status [--json] [--stalled]` | Show board status, lease, and task queue. `--json` includes computed `_task_stack`. |
| `cordboard brief <agent> [--json]` | Read-only compact briefing (board, task, turn, action, chat). |
| `cordboard take <agent> [--json]` | Atomically acquire actionable work, claim lease, and output post-take brief. |
| `cordboard work <agent> [--json]` | Normal wake: return a passive brief or atomically acquire actionable work. |
| `cordboard renew <agent> --rev REV [--json]` | Extend a live acquired lease and replace its revision token. |
| `cordboard advance <agent> --rev REV [options]` | Atomically finish one leased work beat without retry-skipping a phase. |
| `cordboard finish <agent> --rev REV [--json]` | Check off done work and hand off the next LIFO task, retry-safely. |
| `cordboard recap [TASK_ID] [--json]` | Read the retained factual lifecycle for a task without writing. |
| `cordboard digest --since REV [--json]` | Read retained multi-task facts after an exclusive revision cursor. |
| `cordboard set [options]` | Update state/baton (e.g., `--phase`, `--claim`, `--release`, `--verdict`, `--force`). |
| `cordboard wait <agent> [--timeout N]` | Block until it is `<agent>`'s turn. Exit codes: `0` (turn), `10` (disabled), `11` (timeout). |
| `cordboard say [options]` | Back-channel chat. `say "msg"` appends; `say --read <agent>` reads & advances cursor. |
| `cordboard task list [--json]` | List tasks from `TODO.md` with pending/done status and LIFO head indicator. |
| `cordboard task start [--task ID]` | Install the bottom-most pending task and hand off its entry phase. |
| `cordboard task complete [--next]` | Human override for manual check-off/start-next or park behavior. |
| `cordboard doctor` | Validate `status.json` schema, agent wake configurations, and `TODO.md` integrity. |
| `cordboard spec` | Print the full protocol specification (`instructions.md`). |

`cordboard init --migrate [--legacy PATH]` accepts schema v2-v5 and installs schema v6 only after
the complete candidate validates. It preserves revision, task, roles/agents, the honest history
boundary, TODO, chat, and plans; installs the current packaged spec; and backs up an existing
destination status as `status.json.vN.bak`. Missing, malformed, v1, and future schemas are refused
without modifying the destination.

---

## Repository Layout

```
cordboard/
  instructions.md   Protocol specification (committed)
  TODO.md           LIFO task stack (committed)
  .gitignore        Ignores local runtime files (committed)
  status.json       Runtime state baton & leases (local)
  chat.md           Agent back-channel messages (local)
  plans/<task-id>/  Task plans and redline reviews (local)
  .status.lock      Advisory lock for state updates (local)
  .chat.lock        Advisory lock for chat appends/rotations (local)
  .todo.lock        Advisory lock for task list completion (local)
```

---

## Concurrency & Safety Model

- **Advisory File Locking**: All state mutations obtain an exclusive `fcntl.flock` before accessing `status.json`.
- **Compare-And-Swap (CAS)**: State updates verify the `rev` counter inside the critical section. If another actor updated the state concurrently, the mutation safely retries against the latest revision.
- **Atomic File Swaps**: Writes land in a temporary file in the same directory, flushed and synced (`fsync`), and atomically swapped via `os.replace`.
- **Timed Leases**: Active work holds a lease (`lock.expires_at`). If an agent process crashes or hangs, the expired lease can be safely stolen by the partner or reset without manual intervention.
- **Doctor Verification**: `cordboard doctor` verifies state fields, role mappings, valid phases, wake configurations, and TODO syntax.

---

## Development

```bash
# Run test suite
pytest

# Code linting
ruff check .
```

`tests/test_docs.py` guarantees that the shipped specification (`instructions.md`), CLI subcommands, schema definitions, and workflow transitions never drift from the Python implementation.

---

## License

[MIT](https://github.com/chandreshkkhatri/cordboard/blob/main/LICENSE)
