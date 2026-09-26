---
name: present-decisions
description: Present outstanding decisions to the user so they can rule on them quickly. Use when work is blocked on choices only the user can make, or when they ask what needs deciding.
---

# present-decisions

Turns the pile of open questions into something the user can answer in one pass. The work is
selection and compression; the value is that they can rule without opening the task store.

## The shape

At most **five** decisions. Five is a soft cap, not a limit to game — if there are nine, present
the five that are blocking and say the rest are parked. A list long enough to need scrolling
gets deferred wholesale, which is worse than presenting three.

Each decision gets four parts, in this order:

**Title — as a question.** "Does `feed` join the bar's natural key?", not "Natural key design".
The user should be able to answer from the title alone if they already know their mind.

**The issue.** One to three sentences. What is actually wrong or undecided, in terms of the system
rather than the plan. Enough that someone who has not been following can rule; no more.

**The options.** Two or three, lettered. Each carries its cost — the reason someone might not pick
it. An option with no stated cost reads as a straw man and makes the whole set less trustworthy.

**The recommendation.** Always. One line, with the reason. A survey without a recommendation moves
the work back onto the person who asked.

Then, on its own line: **what it blocks**. "Blocks: the last task of the current epic" or
"Blocks: nothing, filed as follow-up". This is what lets the user triage by consequence rather
than by reading every entry.

## Rules that matter more than they look

**Include the obvious option.** The most common failure is offering "now, on a separate branch"
and "later" while omitting "fold it into the change already in flight". If the user replies by
naming an option that was not on the list, the list was wrong — say so rather than defending it.

**No internal shorthand in prose.** No record identifiers, no priority codes, no internal task
numbering, unless the user used that shorthand first. These are addressing for the task store and
carry no meaning to a reader. Name the thing: "the record for the orphan sweep", not its id. Where
the user needs an identifier to act on something themselves, give it *with* a description
attached, never alone.

**Separate decisions from actions.** "Push these three branches" is not a decision. Put actions
waiting on the user in their own short section at the end, so the numbered list stays answerable.

**Say what is parked and why.** One line for anything deliberately not being asked about —
deferred to later work, blocked on something else, awaiting a machine the agent cannot reach.
Without it the user cannot tell whether the list is the whole queue.

**Do not re-ask what has been decided.** Before presenting, check the record for each item. A
decision presented twice reads as not having listened, and it is usually a bookkeeping failure —
the ruling was made in conversation and never written down.

**Order by consequence.** What is blocking active work first; what is only unblocking future
planning last.

## When not to use this

A single decision arising mid-task does not need the format — ask it in a sentence and carry on.
This is for the batch: when work stops because several things need rulings, or when the user asks
what is outstanding.

## After the rulings

Record each one where the work lives, not only in the reply. A ruling that exists only in chat is
lost at the session boundary, and the next agent will re-ask it.
