<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal, pulled into CLAUDE.md via @ -->

## Task Record Workflow

Work is tracked in the task store, not in chat. The store is the handoff between sessions — anything that exists only in conversation is lost at the session boundary.

1. Read your assignment: `bd show <id>`.
2. Claim it: `bd update <id> --status in_progress`.
3. Record non-trivial decisions as you make them, marked ephemeral pending curation.
4. Blocking question? Comment `Q:@<role>: <question>`, add the label `pending-from:<role>`, and stop. Don't guess past it.
5. Hand off by setting your role's terminal status — named in your role section.

Rules:

| Rule | Why |
|---|---|
| Only the validator closes work as done | One gate, one owner |
| Finish with `bd close`, never a status | Only the built-in `closed` releases a blocking edge; a custom "done" stalls every dependent, silently |
| Never invent a status | A status with no queue is one nothing watches, and the work disappears |
| Never rewrite a decision record | Append an addendum; the superseded reasoning is the point of having a record |
| Never make a decision record a blocking dependency | It is satisfied by *existing*. `accepted` is frozen and never becomes `closed`, so the edge can never release and every dependent stalls forever. Link it with `relate`. If work truly waits on a decision, gate it on a question bead carrying `pending-from:`, which closes |
| A bead in review with no `pending-from:` label is in no queue | Nothing watches it. Four sat that way for days while the epic was described as blocked |
| Never point a `blocks` edge at an epic | Every child depends on its parent, and the ready query treats that as blocking — so one edge silently freezes the entire child set. Nothing in flight stops, which is why it looks harmless. Block the bead that actually must wait, usually the one that declares the feature done |
| Cite a file, not a record id, for store failures | A pointer into the store is useless when the store is what's broken |
