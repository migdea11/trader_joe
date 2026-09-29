# handoff

Moves work from this session to the next one without losing state and without the next session
re-deriving decisions that have already been made. The prompt is the smallest part; the work is
flushing state into the store first.

## The order matters

**Record state in the store before writing a word of the prompt.** A prompt is not a record. It is
read once, by one session, and then it is gone — while the store is what every later session reads.
Anything that exists only in conversation is lost at the session boundary, and a handoff is exactly
the boundary.

The test to apply: **if the prompt were lost in transit, could the next session reconstruct the work
from the store alone?** If not, the store is incomplete and the prompt is hiding it.

### 1. Sweep for chat-only state

Go looking for it; it does not announce itself. The usual sources:

| Source | What escapes |
|---|---|
| Rulings the user made in conversation | A decision nobody wrote down gets re-asked, which reads as not having listened |
| Findings inside agent reports | A report is model output, not a record. A finding that never became a bead dies with the session |
| Corrections | Where a bead's body is now wrong, or where an instruction was superseded |
| Commit SHAs and verdicts | Which SHA was gated, what was measured, what is pushed and what is not |
| What is blocked, and on whom | Including anything blocked on the *human* — permissions, host access, a decision |

For each, update the bead or create one. Prefer appending to an existing bead over creating a
near-duplicate.

### 2. Make the store queryable

Run the queries the next session will actually run — `bd ready --label assignee:<role>` for each
role you are handing work to. Then check the two failure modes that make work invisible:

- **A bead in review with no `pending-from:` label is in no queue.** Nothing watches it.
- **A blocking edge whose source can never close** — a decision record, or an edge pointed at an
  epic — freezes its dependents silently.

If a bead does not appear in the query you expect, fix that now. The next session will trust the
query over the prompt, and it should.

### 3. Write the prompt short

**Reference beads; never reiterate their content.** A summary in the prompt is a second copy that
starts drifting immediately, and the next session has `bd show`. If a bead's body is wrong, fix the
bead — do not correct it in the prompt.

Bead ids belong here. This is the inverse of how ids work in conversation with the user: in a
handoff prompt the id *is* the address, and the reader is an agent with the store in front of it.
Still give each id a few words of description, so a stale or renamed bead is obvious rather than
silently wrong.

The shape, in this order:

1. **One line of standing.** What the work is, and where it stands — with the evidence, not the
   adjective. A SHA, a test count, what is pushed and what is not.
2. **The epic, for context.** Its id and the command to read it. One line. Do not summarise it.
3. **The beads for the work ahead**, grouped as *ready*, *blocked* (naming the blocker), and
   *awaiting the human*. One line each: id, a few words, and the owning role.
4. **What is already settled.** A pointer to the decision records, and an explicit instruction not
   to re-litigate them. This is what stops the next session reopening a day of rulings.
5. **What is blocked on the human**, stated as such so the next session does not try to route
   around it. A permission refusal is not a task.
6. **The plan requirement**, below.

Keep it under about 25 lines. A long handoff prompt gets skimmed, and skimming a handoff is how the
next session starts work on the wrong bead.

### 4. Require a plan back — and require it to stop

The next session must **output a plan and then stop**, before dispatching anything. This is what
lets the outgoing session correct a misreading while it still costs one message instead of a
day's work.

Specify the plan's shape, or what comes back cannot be checked:

- Ordered steps, each naming **the bead, the role to dispatch, and the file scope**.
- What it will explicitly **not** touch, and why.
- Anything in the store it read as contradictory or stale.
- Its first command.

Ask for it in one copy-pasteable block, so it can be pasted straight back.

### 5. Hand over

Print the prompt as a **single fenced block with nothing else inside it**, so it copies cleanly.
Say in one line above it what it is. Do not narrate the prompt's contents afterwards — that is the
summary this whole skill exists to avoid.

## Rules worth more than they look

**Never claim a push, a merge or a deployment happened.** State the command and who runs it. A
handoff that asserts work left the machine, when it did not, sends the next session looking for it.

**Say which facts you verified this session, and which you are relaying.** The next session
inherits your confidence along with your claims, and cannot tell them apart unless you mark them.

**A blocked-on-human item goes in its own group, never mixed with ready work.** Otherwise the next
session treats it as a task, tries it, and gets the same refusal.

**One prompt, one branch of work.** If two unrelated efforts are in flight, hand off twice.

## When not to use this

Not for continuing in the same session — the store already has it. Not for a task small enough to
restate in a sentence. This is for a session boundary with live work on the other side of it.
