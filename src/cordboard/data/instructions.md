# cordboard — protocol specification (schema v4)

This document defines the coordination protocol, state machine, and safety rules that let two
coding agents work a shared task backlog without stepping on each other.

It is written in terms of **roles**, never in terms of particular tools. Agents are named by you in
config; nothing in the phase graph knows or cares which one is which.

---

## The two axes

A running board is described by two independent settings in `status.json`:

| Axis | Field | Values | Chooses |
|---|---|---|---|
| **Workflow** | `workflow` | `relay` \| `redline` | Which phase graph is in play — who holds the pen |
| **Wake** | `agents.<name>.watch` + `.tick` | bool + schedule | How that agent finds out. **Per agent**, not global |

They compose freely. `redline`, with both agents watching, is the intended default.

There is no third "mode" axis. A board where both roles point at one agent *is* the solo
configuration — see Roles below.

---

## Roles

Two roles, mapped to agents by `roles` in `status.json`:

```json
"roles": { "reviewer": "cc", "author": "ag" },
"agents": {
  "cc": { "status": "paused", "watch": true, "tick": "*/20 * * * *", "heartbeat": null },
  "ag": { "status": "paused", "watch": true, "tick": "*/20 * * * *", "heartbeat": null }
}
```

Agent names are yours. They are keys under `agents`, so they cannot collide with a config field, and
renaming one is a config edit rather than a code change.

- **`author`** — writes code. In `redline`, also writes the plan.
- **`reviewer`** — gates the work. In `relay`, also writes the plan.

**That last difference is the entire difference between the two workflows.** The role→agent mapping
is identical in both, so switching workflow never requires remapping roles.

### Solo is a mapping, not a mode

Point both roles at the same agent and it owns every phase, because every phase's role resolves to
it. One session then drives the whole board — useful when the second agent is offline or out of
quota, and the simplest way to prove a workflow end to end.

This is deliberately *not* a setting. The predecessor of this spec had a `mode` field with
`solo_cc` / `solo_ag` values, and every phase algorithm needed a clause explaining how to run "the
other agent's tick, substituting field names". Expressing solo as a role mapping deleted the field
and every one of those clauses.

---

## Layout

`cordboard init` writes this into the host repo. The default location is `./cordboard/`; override it
with `--dir` or `$CORDBOARD_DIR`.

```
cordboard/
  instructions.md        This spec, as the agents read it. Commit it.
  TODO.md                LIFO task stack — bottom task is picked first. Commit it.
  .gitignore             Ignores the runtime files below. Commit it.
  status.json            The baton. Local.
  chat.md                Agent back-channel. Local.
  plans/<task-id>/       Per-task plan + redlines. Local.
  .status.lock           flock target. Local.
  .chat.lock             chat rotation lock. Local.
  .todo.lock             task completion lock. Local.
```

The split is deliberate: the spec and the stack are shared facts about the work, and the baton is
one machine's runtime state. Committing `status.json` would mean merge conflicts on every handoff.

---

## The board file

`status.json`, in full. Written only through `cordboard set`.

```json
{
  "version": 4,
  "enabled": false,
  "workflow": "redline",
  "auto_commit": true,
  "max_rounds": 3,
  "lease_seconds": 5400,
  "stall_after_seconds": 600,
  "rev": 0,
  "roles": {
    "reviewer": "cc",
    "author": "ag"
  },
  "agents": {
    "cc": {
      "status": "paused",
      "watch": true,
      "tick": "*/20 * * * *",
      "heartbeat": null,
      "chat_cursor": 0,
      "note": ""
    },
    "ag": {
      "status": "paused",
      "watch": true,
      "tick": "*/20 * * * *",
      "heartbeat": null,
      "chat_cursor": 0,
      "note": ""
    }
  },
  "lock": {
    "holder": null,
    "acquired_at": null,
    "expires_at": null
  },
  "task": {
    "id": null,
    "title": null,
    "kind": "implement",
    "phase": "idle",
    "dir": null,
    "plan_file": null,
    "last_redline": null,
    "verdict": null,
    "handoff_at": null,
    "round": {
      "plan": 0,
      "impl": 0
    }
  },
  "updated_at": null,
  "updated_by": "user"
}
```

| Field | Meaning |
|---|---|
| `enabled` | Master switch. False makes every tick a no-op and every `wait` exit 10. |
| `workflow` | `relay` or `redline` — which phase graph applies. |
| `rev` | Bumped on every write. The compare-and-swap reads it; never set it by hand. |
| `roles` | Role → agent name. Both roles must be filled for the board to run. |
| `agents.<name>.watch` | Whether this agent reacts to handoffs via `cordboard wait`. Default true. |
| `agents.<name>.tick` | Backstop schedule, or null. An agent needs at least one of these two. |
| `task.handoff_at` | When the current phase was entered — what a stall is measured from. |
| `stall_after_seconds` | How long the baton may wait before the handoff counts as missed. |
| `agents.<name>.chat_cursor` | Line count of `chat.md` this agent has read. |
| `agents.<name>.note` | Free text from that agent's last tick — where a blocked agent says why. |
| `lock` | The work lease. An expired lease is stealable; that is the crash recovery. |
| `task.kind` | `implement`, or `plan_only` to finish at the plan and skip the build. |
| `task.dir` | The task's directory under `plans/`, holding `plan.md` and its redlines. |
| `task.verdict` | The last redline's verdict: `approve`, `revise`, or `reject`. |
| `task.round` | Rounds spent at each gate, against `max_rounds`. |
| `auto_commit` | Whether a finishing tick commits, per **Commit Scope** below. |
| `lease_seconds` | Lease TTL. Long enough to outlast a slow phase, short enough to recover. |

Per-task directory:

```
cordboard/plans/2026-08-22-a1-foo/
  plan.md                 author-owned — the author is the sole writer
  redline-plan-r1.md      reviewer-owned
  redline-impl-r1.md      reviewer-owned
```

**One writer per file, always.** The reviewer never edits `plan.md`; it writes a sibling redline and
the author folds the comments in. This is what makes `watch` mode safe once the old 15-minute timer
offset is gone.

---

## State: never hand-edit `status.json`

Every mutation goes through `cordboard set`, which takes an `flock` and does a `rev`
compare-and-swap. Hand-editing races with a live agent and silently loses writes.

| Command | Purpose |
|---|---|
| `cordboard status` | Read state and queue summary. `--json` adds computed `_task_stack` context |
| `cordboard brief <agent>` | Return that agent's bounded, strictly read-only handoff |
| `cordboard take <agent>` | Atomically acquire actionable work and return its post-take brief |
| `cordboard advance <agent> --rev REV` | Atomically finish one already-taken work beat |
| `cordboard set` | Write state. Refuses out-of-turn moves and illegal transitions unless `--force` |
| `cordboard wait <agent>` | Block until it is that agent's turn. This is `watch` |
| `cordboard say` | Append to / read the chat back-channel |
| `cordboard task list\|start\|complete` | Inspect (`list --json`) or advance the LIFO task stack |
| `cordboard doctor` | Validate board state and the TODO stack without writing |
| `cordboard init` | Create a board, or `--migrate` a v2 one |
| `cordboard spec` | Print this document |

Every command takes `--dir <path>`, before or after the subcommand, and honours `$CORDBOARD_DIR`.
Without either, the board is `./cordboard/` relative to the working directory — so a tick that runs
from somewhere else must say where the board is.

Typical agent write:

```bash
cordboard set --agent ag --phase drafting --claim --status working
# ... do the work ...
cordboard set --agent ag --phase plan_submitted --release --status paused
```

`--claim` takes the work lease; `--release` drops it.

### The compact agent brief

The full specification is bootstrap and recovery material, not a per-wake context tax.
`cordboard brief <agent>` returns five stable sections: `board`, `task`, `turn`, `action`, and
`chat`. They contain the current task facts, ownership decision, phase instructions, executable
safe transitions, and unread chat. `--json` emits the same contract for wrappers.

Brief is strictly read-only: it creates no lock, advances no chat cursor, and changes no file. It
reads and validates the board, TODO stack, and chat before printing anything; malformed input exits
`2` without a partial brief. An unknown agent exits `1` without stdout.

Wrappers branch on `turn.disposition`, evaluated in this precedence order:

| Disposition | Meaning |
|---|---|
| `disabled` | The master switch is off. |
| `parked` | The board is idle. |
| `human_required` | The task is blocked or a review handoff lacks required metadata. |
| `act` | This agent owns a stable handoff phase. |
| `resume` | This agent owns an in-progress phase left by an interrupted turn. |
| `wait` | Another agent owns the current phase. |

An actionable brief resolves plan paths, redline gate/round/path and verdict commands, the relay
`plan_only` branch, both clean and issue review outcomes, and the `auto_commit` policy. A blocked
brief includes non-empty agent notes. Agents therefore use the spec only when bootstrapping or
recovering an exceptional state.

### Atomic take

After an actionable brief, `cordboard take <agent>` performs acquisition in one state revision. It
rechecks ownership under the lock, claims or renews the lease, marks the agent working, and applies
this explicit stable-to-active mapping:

| Workflow | Stable handoff | Active phase |
|---|---|---|
| relay | `awaiting_plan` | `planning` |
| relay | `plan_ready` | `implementing` |
| relay | `awaiting_review` | `reviewing` |
| redline | `awaiting_draft` | `drafting` |
| redline | `plan_submitted` | `plan_redlining` |
| redline | `impl_submitted` | `impl_redlining` |

An in-progress phase resumes in place. `plan_redlined` and `impl_redlined` are claimed in place
because their verdict determines the successor. Planning/drafting acquisition fills missing
`task.dir` and `task.plan_file` with `plans/<task-id>` and `plan.md`, without creating either path.

`take --json` always returns `mode`, `started`, `resumed`, `claimed`, `reason`, `message`,
`taken_from`, `rev`, `phase`, `lease_holder`, `lease_expires_at`, and `brief`. Successful mode is
exactly one of `started`, `resumed`, or `claimed`. Expected refusals exit `1`, write no state, and
use one stable reason: `unknown_agent`, `disabled`, `parked`, `human_required`, `bookkeeping`,
`not_owner`, `missing_task`, `lease_held`, or `unsupported_phase`. Invalid board, TODO, or chat is
exit `2` without partial output.

Chat and the candidate post-take brief are read and validated before commit, so successful state
cannot be followed by an ambiguous output failure. The nested brief is the full committed context
with already-performed claim and metadata commands removed. Human output renders the same context,
instructions, and remaining transitions. Take does not consume chat, edit TODO, create artifacts,
touch git or notes, or require a summary.

### Atomic advance

After doing the active phase's work, run the exact revision-bound command printed by `take`:

```bash
cordboard advance <agent> --rev <take-revision>
```

`advance` supports only active work phases and selects their semantic successor:

| Workflow | Active phase | Successor |
|---|---|---|
| relay | `planning` | `plan_ready`, or `done` for `plan_only` |
| relay | `implementing` | `awaiting_review` |
| relay | `reviewing` | `done` with `--outcome clean`; `plan_ready` with `--outcome issues` |
| redline | `drafting` | `plan_submitted` |
| redline | `plan_redlining` | `plan_redlined` with `--verdict approve\|revise\|reject` |
| redline | `plan_redlined` | stored `reject` → `plan_submitted`; otherwise → `implementing` |
| redline | `implementing` | `impl_submitted` |
| redline | `impl_redlining` | `impl_redlined` with `--verdict approve\|revise\|reject` |
| redline | `impl_redlined` | stored `reject` → `impl_submitted`; otherwise → `done` |

Inside one locked update it rechecks the board, known/enabled agent, exact `--rev`, active phase,
owner, task id, live agent-owned lease, review metadata, option applicability, and round cap. The
expected revision makes a retried command refuse instead of silently skipping the next phase.

The current `task.dir` must resolve to a directory inside the board's `plans/` directory. Every
phase requires its stored plan file to be a regular, non-empty UTF-8 file whose resolved path stays
inside that current task directory. A redlining phase also requires the inferred
`redline-<gate>-r<round>.md`, or an alternative contained file passed with `--artifact`. Its
round, verdict, and artifact path are recorded atomically. Response phases follow the stored
verdict and revalidate the stored redline file. Cross-task paths and symlinks that resolve into
another task directory are refused.

Success mode is exactly one of `handed_off`, `continued`, or `finished`. Cross-owner handoffs and
`done` release the lease and pause the actor; a same-agent continuation retains both. Expected
refusals exit `1` without changing state and use one of `unknown_agent`, `disabled`,
`revision_mismatch`, `unsupported_phase`, `not_owner`, `missing_task`, `lease_missing`,
`lease_expired`, `lease_foreign`, `verdict_required`, `verdict_not_allowed`, `outcome_required`,
`outcome_not_allowed`, `artifact_not_allowed`, `review_metadata`, `round_cap`, or
`artifact_invalid`. Invalid board state exits `2` without partial output.

`advance --json` returns the expected and committed revisions, source/result phases, mode, next
owner, release and lease facts, and the plan/review facts used. Rendering is pure after commit.
Advance creates no summary or note, edits no TODO/chat/artifact/git data, and does not auto-take the
next owner.

### What `cordboard set` refuses

Two independent guards, both bypassable with `--force` (the human override):

1. **Turn** — an agent may only act when the current phase resolves to one of its roles.
2. **Transition** — the target phase must be a legal successor of the current one. Without this an
   agent holding the baton could skip a review gate entirely (jump `drafting → impl_submitted`, say).
   `blocked` is reachable from anywhere; leaving it needs a human.

`cordboard set` also refuses `--verdict reject` once the gate is at `max_rounds`, telling the reviewer to
set `blocked` instead. The round cap is therefore a real constraint, not just advice in this file.

---

## Workflow: `relay`

Each agent owns a different leg, and the reviewer holds the pen for the plan.

```
awaiting_plan ─(reviewer)─▶ planning ─(reviewer)─▶ plan_ready ─(author)─▶ implementing
                                                                              │
                                                                          (author)
                                                                              ▼
   done ◀─ clean ─ reviewing ◀─(reviewer)─ awaiting_review ◀────────────────────┘
    │                  │
 (complete task)    issues ─▶ writes a fix-plan ─▶ plan_ready
    ▼
awaiting_plan (next) / idle
```

| Phase | Owner |
|---|---|
| `awaiting_plan`, `planning`, `awaiting_review`, `reviewing`, `done` | reviewer |
| `plan_ready`, `implementing` | author |
| `blocked`, `idle` | nobody — a human moves the baton out |

**Reviewer tick.** At `awaiting_plan`: read the task installed by `cordboard task start` (split it
first if it is too large, appending subtasks at the bottom), write `plan.md`, move to `plan_ready`. At
`awaiting_review`: audit `git diff`, run the plan's verification gates; if clean, move to `done`,
then apply the conservative commit-scope policy (when `auto_commit`) and run
`cordboard task complete --next`; if not, write a fix-plan and return to `plan_ready`.

**Author tick.** At `plan_ready`: read `plan.md`, implement, run the plan's verification steps, move
to `awaiting_review` — or to `blocked` with a note if stuck.

If `task.kind` is `plan_only`, the flow is `awaiting_plan → planning → done` and the author is never
involved.

---

## Workflow: `redline`

The author holds the pen for everything; the reviewer produces nothing but commentary, at two gates.

```
awaiting_draft ─(author)─▶ drafting ─(author)─▶ plan_submitted
                                                      │
                                               (reviewer writes redline-plan-rN)
                                                      ▼
                                                plan_redlined
                                                      │
                        ┌───── verdict=reject ────────┤
                        │   (author revises, re-review)│ approve / revise
                        ▼                              ▼
                 plan_submitted                   implementing ─(author)─▶ impl_submitted
                                                                                │
                                                        (reviewer writes redline-impl-rN)
                                                                                ▼
                                                                          impl_redlined
                                                                                │
                                     ┌───── verdict=reject ──────────────────────┤
                                     │  (author revises, re-review)               │ approve / revise
                                     ▼                                            ▼
                              impl_submitted                    done ─(task complete --next)─▶ awaiting_draft / idle
```

| Phase | Owner |
|---|---|
| `awaiting_draft`, `drafting`, `plan_redlined`, `implementing`, `impl_redlined`, `done` | author |
| `plan_submitted`, `plan_redlining`, `impl_submitted`, `impl_redlining` | reviewer |
| `blocked`, `idle` | nobody — a human moves the baton out |

`done` is not terminal: it is the bookkeeping beat where the finishing agent conservatively
determines commit ownership, commits only when allowed and unambiguous, runs
`cordboard task complete --next`, and either queues the next task or rests. `idle` is the resting
state. This bookkeeping can reconstruct scope from durable task artifacts and the current diff; it
does not depend on incremental summaries.

### Author tick

* **`awaiting_draft`** — claim the lease, move to `drafting`. Read the task identity installed by
  `cordboard task start`. Create `cordboard/plans/<task-id>/`, write `plan.md`, set `task.dir` and
  `task.plan_file`. Move to `plan_submitted`, release.
* **`plan_redlined`** — claim, read `task.last_redline`. Answer **every** comment in `plan.md` under
  a `## Redline responses` heading (see below), revising the plan accordingly. Then branch on
  `task.verdict`:
  * `reject` → back to `plan_submitted` for another round.
  * `approve` / `revise` → move to `implementing`.
* **`implementing`** — implement `plan.md` and run its verification steps. Move to
  `impl_submitted`, or to `blocked` with a concise reason if stuck. No touched-file note or
  incremental summary is required; durable task artifacts and the final diff support later
  bookkeeping.
* **`impl_redlined`** — claim, apply the comments in `task.last_redline` to the code. Then:
  * `reject` → back to `impl_submitted` for another round.
  * `approve` / `revise` → move to `done`, apply the conservative commit-scope policy (when
    `auto_commit`), then run `cordboard task complete --next`.
* **`drafting` / `implementing` found on entry** — watchdog: the previous tick died mid-phase.
  Refresh the heartbeat and resume the same work.

### Reviewer tick

* **`plan_submitted`** — claim, move to `plan_redlining`. Read `plan.md` **and the code it claims
  things about** — a redline that only reads the plan is worthless. Bump `task.round.plan`, write
  `redline-plan-r<N>.md`, set `task.last_redline` and `task.verdict`. Move to `plan_redlined`,
  release.
* **`impl_submitted`** — claim, move to `impl_redlining`. Read `git diff`, run the plan's
  verification gates. Bump `task.round.impl`, write `redline-impl-r<N>.md`, set `task.last_redline`
  and `task.verdict`. Move to `impl_redlined`, release.
* **`plan_redlining` / `impl_redlining` found on entry** — watchdog: resume writing the redline.

The reviewer is the sole bumper of round counters, so the round number always equals the redline
file number.

### Verdicts

Every redline carries exactly one, in its header. This is what bounds the loop.

| Verdict | Meaning |
|---|---|
| `approve` | Nothing to change. Proceed to the next stage. |
| `revise` | Apply the comments, then **continue without re-review**. The default. |
| `reject` | Apply the comments and come back for another round. For structural problems only. |

**Round cap.** `max_rounds` (default 3) bounds each gate. At the cap, `cordboard set` refuses a further
`reject` and the reviewer must set `blocked` with a note — the loop asks the human rather than
ping-ponging forever.

### Redline file format

```markdown
# Redline — plan r1
**Task:** 2026-08-22-a1-foo   **Target:** plan.md   **Reviewer:** cc
**Verdict:** revise

## C1 · blocking · plan.md:L23-L31 — Index already exists
The plan adds two single-field indexes, but `database.py:88-91` already creates a
compound index that serves both access patterns.
**Asked change:** drop step 2; verify with `db.jobs.getIndexes()` instead.

## C2 · advisory · plan.md:L55
Naming nit: `job_idx` reads as a column. `idx_jobs_user_created` matches the rest of the file.
**Asked change:** rename, or say why not.
```

Each comment needs: an **id** (`C1`, `C2`, …), a **severity** (`blocking` | `advisory` |
`question`), a **file:line anchor**, and an explicit **Asked change**. Detail as deep as the problem
warrants — a blocking comment should show the evidence, not just assert.

### Redline responses

The author answers every comment in the revised `plan.md`:

```markdown
## Redline responses (plan r1)
- **C1 accepted** — dropped step 2; verification now reads `getIndexes()`.
- **C2 declined** — `job_idx` matches the three existing names in `database.py:71-84`.
```

Declining is legitimate; declining silently is not. This is the "review the review" step, made
checkable.

---

## Chat back-channel

`cordboard say` appends to `cordboard/chat.md`:

```bash
cordboard say cc ag "C3 is advisory — skip it if the refactor lands first" --re C3
cordboard say ag human "quota nearly out; pausing after this task"
cordboard say --read ag                   # unread entries, then advance the cursor
```

Rules:

- **Advisory only.** Chat never carries baton state. Anything that changes what happens next belongs
  in a redline comment or in `status.json`.
- **Append-only.** Never rewrite or delete entries. A dedicated `.chat.lock` serializes rotation
  with each `O_APPEND` write, so concurrent senders cannot strand or interleave an entry.
- **Read first.** Every tick starts with `cordboard brief <self>`, which peeks at unread chat before
  presenting the phase action. Consume incorporated messages with `cordboard say --read <self>`.
- `human` is a valid addressee for things that need operator attention but do not justify `blocked`.
- The file rotates to `chat.<date>.md` past ~200 KB once every registered agent has consumed it.
  An unread agent defers rotation, because delivery is more important than the soft size limit.

---

## Triggers

`watch` and `tick` are **not alternatives**. Watch is how a handoff is noticed; tick is the backstop
that catches one the watcher slept through. A healthy agent has both.

### `watch` — primary, and the default

`cordboard wait <agent>` blocks until the baton is that agent's, then exits. A handoff is picked up
in seconds.

### `tick` — the backstop

A cron or timer runs the same tick prompt on a schedule. Its job is not to drive the loop; it is to
notice that the loop did not move. Watchers fail in ordinary ways — a session ends, a host does not
re-invoke, a process is killed — and none of those announce themselves. Without a tick behind it, a
dropped watcher is indistinguishable from an idle board.

Keep it **slow**; `*/20` is the default. A fast backstop is polling with extra steps, and it hides
the very failure it exists to reveal.

**An agent with neither is unreachable**, and `cordboard set` refuses to leave one that way.

### Telling a caught handoff from a routine wake

Every phase change stamps `task.handoff_at`. A watching agent claims within seconds, so a baton that
has sat longer than `stall_after_seconds` (default 600) means the wake never arrived:

```bash
cordboard status --stalled     # exit 0 = this handoff was missed
```

`cordboard status` also prints the waiting time and flags a stall. A backstop tick that finds this is
doing the job it exists for; one that finds it *often* means the watcher does not work on this host,
which is worth knowing before it matters.

### `watch` — state-driven

`cordboard wait <agent>` blocks until the baton is that agent's, then exits. **The watcher polls; the agent
blocks** — no model turn runs while waiting, so idling costs nothing.

| Exit | Meaning |
|---|---|
| `0` | Your turn. Read `status.json` and run your tick. |
| `10` | Board disabled. Stand down; do not re-arm. |
| `11` | Timed out. Re-arm and keep waiting. |
| `2` | State file unusable. Stop and tell the human. |

How you wire that up depends on the host:

- **A host that re-invokes the agent when a background command exits** can run `cordboard wait` in the
  background and treat the exit as the event. The tick then ends by re-arming the watcher.
- **A host with a blocking foreground call** can run `cordboard wait` directly and act when it returns.
- **A host with neither** sets `watch: false` and relies on its tick alone. Because both fields are
  per agent, a mixed pair is a normal configuration rather than a compromise.

**Keep a slow `tick` armed alongside `watch`** (hourly is plenty) as a heartbeat, so a dropped
watcher degrades to slow rather than dead.

---

## Concurrency

Two agents write one file, and under `watch` they can act seconds apart. Offsetting timers is not
concurrency control, it is a hope — so there are two real mechanisms, both in `cordboard.state`:

- **`flock` + `rev` CAS.** Every write takes an exclusive lock on `.status.lock`, re-reads `rev`
  inside the critical section, and writes only if it is unchanged — then `os.replace` puts the new
  file in place atomically. Readers therefore never see a partial file, and a stale writer retries
  instead of clobbering.
- **Lease lock.** `lock: {holder, acquired_at, expires_at}` is held across a phase via
  `cordboard set --claim` / `--release`. A live lease may only be renewed or released by its holder
  unless an operator uses `--force`. An **expired** lease is stealable: the other agent may reset
  the phase to the last stable state, note it, and carry on. That is what makes a crashed agent
  recoverable without a human, and it works at second-scale as well as at hour-scale. Default TTL
  is `lease_seconds` (5400).

---

## Commit Scope (whenever the board commits)

The working tree may hold changes unrelated to the current task (other agents, out-of-band edits,
queued work). A commit must therefore be **scoped to the task** without relying on a potentially
stale touched-file note or incremental summary:

- **Derive, then reconcile.** Read the task plan and review artifacts to derive a conservative
  candidate path set, then reconcile it with the current git diff/status. Stage only verified
  changes whose ownership by this task is unambiguous. **Never `git add -A` / `git add .`.**
- **Verify before committing:** `git diff --cached --name-only` must exactly match the reconciled
  task-owned set. If anything unexpected is staged, unstage it (`git restore --staged <file>`).
- **Ambiguity means no auto-commit.** Do not guess, commit, or complete the task when any changed
  path's ownership is unclear. Use the brief's `blocked` transition; its next brief is
  `human_required` and asks an operator for a scope decision.
- **Never bundle board bookkeeping into a task commit.** `cordboard/status.json`,
  `cordboard/plans/*` and `cordboard/chat.md` are ignored runtime state, and a `cordboard/TODO.md`
  check-off is board bookkeeping rather than part of the implementation — leave those out.
- **Follow the host repo's commit conventions.** If it has a contributor guide, that governs the
  message. A completed, verified task under `auto_commit` is a self-contained unit of work, which is
  what makes it committable without asking first — anything less finished is not.

---

## Human Overrides

- `cordboard set --start` moves the baton to the current workflow's entry phase. This is how a run
  begins: no role owns `idle`, so no agent can start itself.
- `cordboard task start` is the normal queued-work path: it installs the bottom pending TODO task,
  resets its lifecycle fields, and moves to the workflow entry phase in one state update.
- `cordboard set --enabled false` halts every tick immediately, and makes live `cordboard wait` calls
  exit 10. The baton stays exactly where it is, so resuming costs nothing.
- `cordboard set --role author=<agent>` reassigns a role. Point both roles at one agent to go solo.
- `cordboard set --workflow redline` (or `relay`) switches the phase graph. Do it at `idle` — switching
  mid-task strands the baton in a phase the new graph does not have, and `cordboard set` will refuse it.
- Edit `cordboard/TODO.md` freely to add, reorder, or remove tasks.
- `cordboard set --force --phase <phase>` reassigns the baton by hand. `--force` skips both guards, so
  it is the tool for recovering a stuck board — not the tool for ordinary moves.

---

## Session Bootstrap

Timers and watchers are **session-only** — both die with the session that created them, so each
fresh session must re-arm.

### The tick prompt

Both agents get the same prompt, differing only in their own name. Give it to whatever schedules the
agent — a cron entry, a recurring timer, a wrapper script:

> You are `<agent-name>` in a cordboard loop. Run `cordboard brief <agent-name>`. If actionable work,
> run `cordboard take <agent-name>`, follow its post-claim instructions and safe transitions, then consume incorporated messages with
> `cordboard say --read <agent-name>`. Read `cordboard/instructions.md` only for bootstrap or
> recovery when the brief is insufficient. Do nothing outside the current task.

Reading the back-channel **first** is part of the algorithm: a message from the other agent may
change how you handle the phase you are about to act on.

### Arming the watcher — for both agents

> Read `cordboard/instructions.md`, then run `cordboard status`. If the board is enabled, start
> `cordboard wait <agent-name>` in the background and report the state back to me. When it exits 0,
> run the tick for the current phase, then re-arm the watcher.

Exit 11 (timeout) means re-arm and keep waiting. Exit 10 means the board was disabled: stand down,
and do **not** re-arm.

### Arming the backstop — also for both agents

Schedule the tick prompt at the agent's `tick`. It runs the same algorithm, so most of the time it
finds nothing to do and costs one cheap wake. When it *does* find work waiting,
`cordboard status --stalled` exits 0: the watcher missed that handoff, and the agent should say so in
its `note`. Those notes are the only record you get that `watch` is unreliable on this host.

An agent with `watch: false` has no watcher to back up — its tick is the whole mechanism, so tighten
the schedule, and offset it (`5,35` vs `20,50`) against any other non-watching agent so a handoff
lands in the gap rather than both waking together. The lock and CAS make a simultaneous pair safe;
offsetting just wastes fewer ticks.

### Go live

The board is inert until `enabled` is true, and nothing happens with an empty stack. In order:

```bash
# 1. queue work — bottom of the file is picked first
$EDITOR cordboard/TODO.md

# 2. arm both agents (above), then go live
cordboard set --enabled true

# 3. install the bottom task and hand the baton to the workflow's first phase
cordboard task start
cordboard status              # confirms whose turn it is
```

Reverse it to stop: `cordboard set --enabled false`. The agents stay armed and every tick becomes a
no-op, so flipping it back resumes mid-task.
