# cordboard

Pass work between coding agents with a file-based baton — no server, no broker, just a spec and a
JSON file in your repo.

Two coding agents, whatever they are, work a shared task stack without stepping on each other. One
holds the baton at a time; the board says who, and refuses moves that would skip a review gate.

## Install

Not on PyPI yet — from source for now:

```bash
git clone https://github.com/chandresh/cordboard && cd cordboard
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Once published, `uvx cordboard` / `pipx run cordboard` will work without an install.

## Use

```bash
cd /your/repo
cordboard init                 # writes ./cordboard/
$EDITOR cordboard/TODO.md      # queue work; the bottom entry runs first
cordboard set --enabled true
cordboard task start           # install the bottom task and hand off its first phase
cordboard status               # whose turn is it
```

Every command takes `--dir <path>` (before or after the subcommand) and honours `$CORDBOARD_DIR`,
so the board does not have to live at `./cordboard`.

## Why a file

Everything is a file in your repo, which means the whole protocol is inspectable with `cat` and
reviewable in a diff. There is nothing to deploy, nothing to authenticate against, and no service
that can be down. Two agents on one machine share a directory; that is the entire transport.

## The two workflows

| Workflow | Shape |
|---|---|
| **`relay`** | Each agent owns a different leg. The reviewer plans, the author builds, the reviewer audits. |
| **`redline`** | The author holds the pen for plan *and* code; the reviewer produces nothing but commentary, at two gates. |

`redline` is the interesting one. The author drafts a plan, the reviewer marks it up with anchored
comments and a verdict, the author answers every comment and implements, the reviewer marks up the
diff, the author applies and commits. Verdicts bound it: `approve` proceeds, `revise` means apply
and carry on, `reject` sends it back for another round — and a round cap turns the third `reject`
into `blocked` rather than a loop.

## How an agent finds out

Two settings per agent, and they are **not** alternatives:

- **`watch`** (default on) — `cordboard wait <agent>` blocks until the baton is that agent's, then
  exits. Handoffs are picked up in seconds. The watcher polls and the agent blocks, so waiting costs
  no tokens: no model turn runs until `wait` returns. Exit codes are the contract — `0` your turn,
  `10` board disabled, `11` timed out, `2` unusable board.
- **`tick`** (default `*/20`) — the backstop. It does not drive the loop; it notices that the loop
  did not move. Watchers fail quietly: a session ends, a host does not re-invoke, a process is
  killed. Without a tick behind it, a dropped watcher looks exactly like an idle board.

Keep the tick slow. A fast one is polling with extra steps, and it hides the failure it exists to
reveal. An agent whose host cannot watch at all sets `watch: false` and runs on its tick alone —
that is the case to tighten. An agent with neither is unreachable, and `cordboard set` refuses it.

Every phase change stamps `task.handoff_at`, so a missed handoff is measurable rather than a
suspicion:

```bash
cordboard status --stalled     # exit 0 = the watcher missed this one
```

## Roles, and why there is no "solo mode"

Phases belong to roles — `author` and `reviewer` — and roles are mapped to agents you name:

```json
"roles": { "reviewer": "cc", "author": "ag" }
```

Point both roles at one agent and it owns every phase, because every phase's role resolves to it.
That is the solo configuration, and it is a config shape rather than a setting. An earlier version of
this had a `mode` field with `solo_cc` / `solo_ag` values, and every phase algorithm carried a clause
explaining how to run "the other agent's tick, substituting field names". Expressing solo as a role
mapping deleted the field and all of those clauses.

## Two agents write one file

Offsetting timers is not concurrency control. Every write takes an `flock`, re-reads a `rev` counter
inside the critical section, and only commits if it is unchanged, then lands via `os.replace` so a
reader never sees a partial file. A separate lease (`lock.holder`, with an expiry) is held across a
phase, and an expired lease is stealable — which is what makes a crashed agent recoverable without a
human.

At `done`, `cordboard task complete --next` checks off the active TODO entry and starts the next
bottom-most pending item, or parks the board at `idle` when the queue is empty. If execution is
interrupted between the check-off and handoff, rerunning completion is safe.

## Commands

| Command | Purpose |
|---|---|
| `cordboard init` | Create a board. `--migrate` imports a v2 board. |
| `cordboard status` | Read state and queue context. `--json` includes computed `_task_stack`. |
| `cordboard set` | Move the baton or change config. `--start` kicks off; `--force` overrides a guard. |
| `cordboard wait <agent>` | Block until it is that agent's turn. Exit `0`/`10`/`11`/`2`. |
| `cordboard say` | Append to or read the back-channel. |
| `cordboard task list\|start\|complete` | Inspect (`list --json`) or advance the LIFO stack. |
| `cordboard doctor` | Validate board state and the TODO stack without writing. |
| `cordboard spec` | Print the protocol spec the agents follow. |

`cordboard spec` is the whole protocol: the phase graphs, both tick algorithms, the redline comment
format, and the board schema. It is what you give an agent to read at the top of a tick.

Never hand-edit `status.json` while agents are live — `cordboard set` locks and compare-and-swaps;
an editor does neither.

## Layout

`cordboard init` writes this into your repo:

```
cordboard/
  instructions.md   The spec, as the agents read it.   commit
  TODO.md           LIFO stack — bottom is next.       commit
  .gitignore        Ignores the runtime files below.   commit
  status.json       The baton.                         local
  chat.md           Agent back-channel.                local
  plans/<task-id>/  plan.md + redline-*.md per task.   local
  .status.lock      State update lock.                 local
  .chat.lock        Chat append/rotation lock.         local
  .todo.lock        Task completion lock.              local
```

The spec and the stack are shared facts about the work. The baton is one machine's runtime state —
committing it would mean a merge conflict on every handoff.

## Development

```bash
.venv/bin/python -m pytest      # run the test suite
.venv/bin/python -m ruff check .
```

`tests/test_docs.py` asserts the shipped spec still matches the code — the board schema key-for-key,
every subcommand, the `wait` exit codes, and every phase of every workflow. The spec is package data
that agents parse, so drift there is a bug rather than a typo.

## Status

Early, and honest about it. The protocol has run real tasks in the repo it was extracted from, but
the package is new: the phase machine, guards, CAS and migration are covered by tests, while the
`watch` trigger's integration with any *particular* agent host is not — that depends on whether the
host re-invokes an agent when a background process exits.

CI runs the suite and lint on every supported Python version, from 3.10 through 3.14.

`cordboard init --migrate` moves an in-flight v2 board across without losing the task.

## License

MIT
