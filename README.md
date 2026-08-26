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
cordboard brief ag      # Read-only, token-efficient handoff briefing
cordboard take ag       # Atomically claim the lease and enter active phase
cordboard advance ag --rev 7  # Atomically finish the work beat and hand off
```

---

## Highlights

- 📁 **100% File-Based & Local**: State lives in `cordboard/status.json`, tasks in `cordboard/TODO.md`, backchannel in `cordboard/chat.md`, and plans in `cordboard/plans/`. Everything is inspectable with `cat` and reviewable in `git diff`.
- 🔒 **Rock-Solid Concurrency & Crash Recovery**: State updates use advisory `flock`, compare-and-swap (CAS) revision counters, and atomic `os.replace` tempfiles. Leases expire automatically, allowing recovery from crashed agents without human intervention.
- ⚡ **Token-Efficient Agent Turns**: Agents don't need to re-read a 600-line specification or narrate every step. `brief`, `take`, and revision-bound `advance` provide bounded context and atomic handoffs; a summary can be written later.
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
git clone https://github.com/chandresh/cordboard && cd cordboard
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
# 1. Check turn and read unread backchannel messages
cordboard brief ag

# 2. If actionable work is ready, atomically claim the lease
cordboard take ag

# 3. Perform the work (draft plan, implement, review diff, etc.)

# 4. Execute the revision-bound transition printed by take
cordboard advance ag --rev 4

# 5. Consume read chat messages
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

Rather than ingesting the entire `instructions.md` on every cycle, an agent calls `cordboard brief <agent>` (or `take <agent>`).

### Dispositions

The brief inspects board state and returns one of six deterministic dispositions (in precedence order):

| Disposition | Meaning | Action Needed |
|---|---|---|
| `disabled` | Master switch is off (`enabled: false`) | Sleep / do nothing |
| `parked` | Queue is empty (`phase: idle`) | Sleep / do nothing |
| `human_required` | Task is blocked (`phase: blocked`) or missing review data | Alert human operator |
| `act` | Agent owns a stable handoff phase (`awaiting_draft`, `plan_ready`, etc.) | Call `cordboard take <agent>` |
| `resume` | Agent owns an in-progress phase (`drafting`, `implementing`, etc.) | Call `cordboard take <agent>` to resume |
| `wait` | Another agent owns the current phase | Wait for turn (via `cordboard wait <agent>`) |

### Atomic Work Acquisition: `cordboard take`

When an agent receives `act` or `resume`, `cordboard take <agent>`:
1. Locks state and re-verifies ownership under `flock`.
2. Claims/renews the lease (`lock.holder` & `lock.expires_at`).
3. Transitions phase from stable handoff to active work (`awaiting_draft` → `drafting`).
4. Fills missing plan directory/file metadata.
5. Returns the post-take briefing with exact safe transition commands.

### Atomic Work Completion: `cordboard advance`

The post-take brief prints the exact `advance <agent> --rev <revision>` command for the active
phase. `advance` rechecks the revision, owner, live lease, task, phase, and required artifacts inside
one locked state update, then either completes the beat once or writes nothing. Relay reviews add
`--outcome clean|issues`; redline gates add `--verdict approve|revise|reject` and may use
`--artifact <path>` for a non-default redline file.

Plan and redline artifacts must be regular, non-empty UTF-8 files contained in the board's
`plans/` directory. Cross-agent handoffs and `done` release the lease; same-agent continuations keep
it. The command does not edit TODO, chat, notes, artifacts, or git, and does not require an
incremental summary.

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
> 1. Run `cordboard brief <agent-name>`.
> 2. If actionable work is ready, run `cordboard take <agent-name>`.
> 3. Follow its post-take instructions and execute the provided safe transitions.
> 4. Consume incorporated backchannel messages with `cordboard say --read <agent-name>`.
> 5. Read `cordboard/instructions.md` only for bootstrap or recovery if the brief is insufficient.
> Do nothing outside the current task.

---

## CLI Reference

| Command | Description |
|---|---|
| `cordboard init [--migrate]` | Initialize `./cordboard/` in the repository. `--migrate` imports legacy v2 boards. |
| `cordboard status [--json] [--stalled]` | Show board status, lease, and task queue. `--json` includes computed `_task_stack`. |
| `cordboard brief <agent> [--json]` | Read-only compact briefing (board, task, turn, action, chat). |
| `cordboard take <agent> [--json]` | Atomically acquire actionable work, claim lease, and output post-take brief. |
| `cordboard advance <agent> --rev REV [options]` | Atomically finish one leased work beat without retry-skipping a phase. |
| `cordboard set [options]` | Update state/baton (e.g., `--phase`, `--claim`, `--release`, `--verdict`, `--force`). |
| `cordboard wait <agent> [--timeout N]` | Block until it is `<agent>`'s turn. Exit codes: `0` (turn), `10` (disabled), `11` (timeout). |
| `cordboard say [options]` | Back-channel chat. `say "msg"` appends; `say --read <agent>` reads & advances cursor. |
| `cordboard task list [--json]` | List tasks from `TODO.md` with pending/done status and LIFO head indicator. |
| `cordboard task start [--task ID]` | Install the bottom-most pending task and hand off its entry phase. |
| `cordboard task complete [--next]` | Check off the current task in `TODO.md` and start next item (or park at `idle`). |
| `cordboard doctor` | Validate `status.json` schema, agent wake configurations, and `TODO.md` integrity. |
| `cordboard spec` | Print the full protocol specification (`instructions.md`). |

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

[MIT](LICENSE)
