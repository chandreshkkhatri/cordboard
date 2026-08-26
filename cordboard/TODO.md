# Task stack

**LIFO — the bottom-most task is picked first.** Add new work at the bottom; the agents pop from
there. Keep each entry to one line plus optional indented notes; anything longer belongs in a plan.

Format: `- [ ] <short-id> — <what needs to be true when this is done>`

## Queue

<!-- add tasks below this line; the bottom one runs next -->

- [x] iter-10 — Finish and polish docs, release metadata, packaged artifacts, and final verification
- [x] iter-09 — Surface pending and next-task context in status and machine-readable output
- [x] iter-08 — Preserve unread chat correctly across log rotation and concurrent senders
- [x] iter-07 — Harden CLI input boundaries so invalid usage never leaks a traceback
- [x] iter-06 — Add board validation and actionable diagnostics for unsafe or corrupt state
- [x] iter-05 — Complete active tasks through the CLI and advance the LIFO queue safely
- [x] iter-04 — Start the bottom TODO task atomically through a first-class task command
