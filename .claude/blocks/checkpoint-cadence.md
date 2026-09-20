<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal. Load-bearing: a re-spawned agent sees only the record, never the prompt. -->

## Checkpoint Cadence

Append a progress note after **every substantive sub-step** — not at time intervals.

```
bd update <id> --append-notes "<HH:MM UTC> — <state>"
```

Each note records:

| Field | Content |
|---|---|
| Done | Artifacts produced — files read, edits applied, commits with SHAs, citations captured |
| Next | The immediate next sub-step |
| Open questions | Anything unresolved |
| Files touched | Paths, if edits were applied |

**Atomicity:** the note lands *before* you begin the next sub-step, so an interrupted session leaves the record reflecting everything already finished. For research, a URL with a load-bearing quote lands in a note — URL plus the verbatim quote — before you open the next URL.

A time budget ("checkpoint at 30 minutes") is a floor, not the cadence.

This applies to read-only roles too: read-only constrains the repository, not the task store.
