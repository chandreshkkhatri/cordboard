# cordboard

Pass work between coding agents with a file-based baton — no server, no broker, just a spec and a
JSON file in your repo.

Two coding agents, whatever they are, work a shared task stack without stepping on each other. One
holds the baton at a time; the board says who, and refuses moves that would skip a review gate.

```bash
uvx cordboard init            # or: pipx run cordboard init
uvx cordboard set --start
uvx cordboard set --enabled true
```

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

## The two triggers

Set per agent, because hosts differ.

- **`tick`** — a timer wakes the agent on a schedule.
- **`watch`** — `cordboard wait <agent>` blocks until the baton is that agent's, then exits.

`watch` is the reason handoffs take a second instead of half a timer interval. The watcher polls and
the agent blocks, so waiting costs no tokens: no model turn is running until `wait` returns. Its
exit codes are the contract — `0` your turn, `10` board disabled, `11` timed out, `2` unusable board.

A mixed pair is normal: put the agent whose host can react to a process exit on `watch`, leave the
other on `tick`.

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

## Commands

| Command | Purpose |
|---|---|
| `cordboard init` | Create a board. `--migrate` imports a v2 `collab/` board. |
| `cordboard status` | Read it. `--json`, `--get task.phase`, `--turn <agent>` (exit 0 = its turn). |
| `cordboard set` | Move the baton or change config. `--start` kicks off; `--force` overrides a guard. |
| `cordboard wait <agent>` | Block until it is that agent's turn. |
| `cordboard say` | Append to or read the back-channel. |
| `cordboard spec` | Print the protocol spec the agents follow. |

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
```

The spec and the stack are shared facts about the work. The baton is one machine's runtime state —
committing it would mean a merge conflict on every handoff.

## Status

Early. The protocol has run real tasks in the repo it was extracted from; the package itself is new.
`cordboard init --migrate` exists to move an in-flight v2 board across without losing the task.

## License

MIT
