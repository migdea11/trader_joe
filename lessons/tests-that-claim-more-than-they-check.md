# Tests that claim more than they check

A green test proves something only about the mutation that would red it. Everything else a test
appears to say — its name, its docstring, its assertion messages — is prose, and prose does not
run. When the prose claims more than the assertions establish, the test becomes a *source of false
confidence*: worse than no test, because no test invites someone to write one.

Three instances of this were caught on one branch, by three different mutations, in three different
places. They are the same defect wearing three hats. The point of this file is the shape, not the
branch.

## The shape

> An assertion that passes in both worlds proves nothing, and the thing that tells you it passes in
> both worlds is never the test run. It is a mutation.

Each instance below was invisible to a full green suite. Each was found by changing production code
and observing that nothing went red.

### 1. The claim lived in the name

A test named `..._reaches_the_handler` asserted a route's status *by exclusion*: not 404, not 422,
not 401, under 500. When a cross-cutting authentication dependency was later added to the write
routes, the gate's own status code silently joined the set of answers the exclusion list tolerated.
The test still passed. It had stopped checking that anything reached the handler at all.

Nobody noticed because the *malformed* cases in the same file went loudly red and were correctly
fixed. Attention went to the noisy half. The well-formed half failed in the quiet direction — it
kept passing — and **a test that goes from meaningful to vacuous emits no signal whatsoever.**

### 2. The file had only refusal cases

A guard was checked by several tests, all of which drove it to *refuse*. Changing the comparison
from `!=` to `is not` — an identity comparison where equality was meant — left every one of them
green, because a refusal is still a refusal when the guard refuses too much. What it broke was the
legitimate caller: an HTTP-parsed string is never the interned constant, so the *rightful* owner
started being turned away.

**A file made only of refusal cases cannot see a guard that refuses too much.** The success case is
not symmetry or decoration; it is the only witness for an entire class of regression.

### 3. The mechanism was stated once for a group

A parametrized test covering three fields carried one sentence explaining what produced the
rejection. It was true for one field and false for the other two, which were guarded twice over — by
an annotation *and* by a before-validator that raised first. Relaxing either guard alone left the
suite green, so the case pinned neither of them individually for those two fields, while its
docstring said it pinned the annotation for all three.

The cost is not a missed regression; it is a reader who relaxes one guard, sees green, and believes
the test blessed it.

## What to do about it

Four items. Each has a diagnosis half and an action half, and the action half is the one that
survives being skimmed.

**1. When a cross-cutting dependency is added to routes, re-read every exclusion-style status
assertion in the suite.** Adding a gate hands its status code to every `!= 404` / `< 500` /
`not in (...)` assertion as a newly acceptable answer. *Re-running the passing tests tells you
nothing — passing is the symptom.* Action: at the moment the gate lands, grep for negative status
assertions and read each one against the new code. Inventory them at the top of the file that holds
them, so the next person adding a gate finds the list instead of having to derive it.
`data/store/tests/test_http_smoke.py` today carries four inside a single test —
`not in UNREACHABLE_STATUSES` (404/405), `!= 422`, `!= 401` and `< 500` — and says so nowhere.

**2. Every layer that has refusal cases needs one success case at that same layer.** Not one
somewhere in the codebase — at the layer. A crud-level success case does not witness a route-level
guard that refuses too much, and vice versa. Action: when adding a refusal test, add or point at the
success case beside it, and say in the docstring which mutation the success case is the witness for.
If you cannot name one, the success case probably *is* decoration and should be argued for or
dropped.

**3. Measure a mechanism claim per parameter, never once for the group.** A parametrized test's
docstring describes N tests, and a sentence true of one parameter is a false claim about the others.
Action: relax each guard **alone** and record the failed set by name for each; then relax them in
combination. If a parameter reds only when two guards are relaxed together, the docstring must say
that parameter is doubly guarded and that the case pins neither guard by itself. The matrix is the
evidence, and it belongs in the docstring, not only in a commit message.

**4. An assertion message may name only regressions a mutation actually reds.** Assertion messages
are where hazards get described, which makes them where over-claiming hides — the docstring gets
audited and the message does not. Action: for every regression a message names, run it. If it comes
back green, either add the case that reds it, or strike the claim and say why it is out of reach.
*Prefer striking the claim when the only way to catch it would be to pin undesigned behaviour* — a
test that freezes an accident is worse than a message that admits a limit.

## The discipline underneath all four

**State a gap as measured or not at all.** "This is covered" and "this is not covered" are both
claims about a mutation's failed set, and both are cheap to get wrong from reading. Run the
mutation, compare failed sets **by name and not by count**, and write down which pre-existing tests
also red — because "my new test is the witness" and "my new test is *a* witness" are different
claims, and only one of them is usually true.

The correction that matters most is the one made against your own work. A test author who mutates
their own production code and reports "13 red, 12 of them pre-existing, so my case is the witness at
this layer and not the only witness" has written a docstring that will still be true in a year.
