---
name: status-update
description: Report progress on the work dispatched in this conversation as one grouped table the user can read at a glance. Use when the user asks for status, progress, where things stand, what is done, or what is waiting on them. Read-only — it changes nothing in the store or the repository.
---

# status-update

Tells the user where this conversation's work stands, in one screen. The work is grouping and
compression. The value is that the user can see what is done, what is moving and what is waiting on
them without opening the task store.

## Scope

**Only beads an agent was dispatched to in this conversation.** Beads from earlier sessions,
epics, and other roles' queues stay out, even when they share a feature label. The user asked
how *this* work is going. A store-wide listing answers a different question and buries the answer.

**Strictly read-only.** Run no command that writes to the store or the repository. Every command
below only reads. If the report turns up something broken, such as a bead in no queue, report it.
Fixing it is a separate step that the user or the orchestrator chooses to take.

## Gathering

| Need | Command |
|---|---|
| Each dispatched bead's status, labels and notes | `bd show <id>` |
| Branch | `git branch --show-current` |
| HEAD | `git log --oneline -1` |
| Unpushed commits | `git status --short --branch`. It shows the ahead count against the upstream. With no upstream, nothing on the branch is pushed. Count with `git log --oneline main..HEAD` and say so. |
| Beads in review | `bd list --status in_review` |
| Ready beads | `bd ready` |
| Decision records | `bd list -t decision --all` |

The dispatched beads come from this conversation's own record: the Agent calls made and the ids
given to them. Do not re-derive the list from a label query.

## The shape

In this order, and nothing else:

### 1. Branch header

One short block: branch, HEAD (short SHA and subject), unpushed count. Then the last test, lint
and security results, **each marked *verified this session* or *relayed***. Mark a result verified
only if you ran the command in this conversation and saw its output. Mark it relayed if it came
from an agent's report. Show a check that nobody ran as *not run*, never as passing.

### 2. Legend

One line, directly above the table, exactly this:

✅ done · 🔄 in progress · 👀 in review · ⏳ waiting on you · ⛔ blocked · ⏭️ deferred · ⬜ not started

### 3. The table

A **rendered markdown table, not a code block.** A fenced block shows the pipes as text, and the
table has to be readable at a glance. Three columns:

| Column | Content |
|---|---|
| Description | What the work is and what it produced, in **1-2 sentences**. **No bead ids.** Name the thing, not its address. |
| Status | One icon from the legend. |
| Blocked by | In words, naming what must happen first, not an id. When the user is the blocker, write **you** in bold. Use `—` when nothing blocks the row. |

**Each row groups beads by theme. Rows do not map 1:1 to beads.** A build task, its tests and its
gate verdicts form one row when the user thinks of them as one piece of work. **The table has at
most 10-20 rows.** If the themes come to more than that, merge further rather than letting the
table scroll.

A row's status is its **immediate** blocker. Show ⏳ only when the next step is the user's. Show ⛔
when another piece of work must land first, even if the user comes after it. Otherwise show the
least-finished state among the row's beads, so a row is ✅ only when all its beads are closed.

The target shape:

| Description | Status | Blocked by |
|---|---|---|
| Tests for identity, ownership and the migration's structure: 47 new tests, each shown to catch its defect. | ✅ | — |
| Checks only a real Postgres can prove: the migration, constraints, secret, oversized batch, cascade. | ⛔ | The timezone fix, then **you** on your machine |

### 4. Needs you

A short list of what only the user can do: rulings, pushes, runs on their host, permission
grants. Each item is a description with **the exact command next to it**, ready to copy. Where the
user needs an id to act, give it alongside the description, never on its own.

- Run the Postgres-only checks on your machine: `<exact command>`
- Rule on whether the archive downgrade ships in this PR. The record is `<id>`, read with `bd show <id>`.

When nothing is waiting on the user, write "Nothing needs you right now." Do not omit the section.

### 5. Agents running now

One line per agent dispatched in this conversation that has not yet reported back: its role and
what it is working on. Never state a result for an agent still running. It is still running.

### 6. Store health

One line of counts:

- beads in review with no `pending-from:` label, which puts them in no queue
- ready beads whose body describes a design a later decision superseded
- decision records used as a blocking dependency, which freezes their dependents for good

For example: "Store health: 0 in review with no queue · 1 ready bead on a superseded design · 0
decisions blocking." Name any nonzero count's beads in a following line, each with a description.

## Rules that matter more than they look

**Never claim a push, a merge or a deployment happened.** State what is unpushed and who pushes it.

**Relayed is not verified.** An agent's report of a green suite is a claim. Mark it relayed, so the
user can tell what you saw from what you were told.

**No internal shorthand in the table.** No bead ids, priority codes or task numbering. Ids appear
only in *Needs you* and *Store health*, and always with a description.

## When not to use this

This is not for handing work to the next session. Use `handoff` for that. It is not for asking the
user to rule on open questions either. Use `present-decisions` for that, and point to it from
*Needs you* when rulings are waiting.
