<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal completion protocol; each role names its own terminal status -->

## When Done

Before reporting complete:

1. Tests pass — `make test PATHS=<path>`.
2. Lint is clean — `make lint PATHS=<path>`.
3. Your changes are committed in a self-consistent state.
4. The task record carries a final checkpoint note.
5. The record is set to your role's terminal status.

Then report in this shape:

| Section | Content |
|---|---|
| Outcome | One line: what now exists that didn't before |
| Files | Paths touched, with commit SHAs |
| Verification | The commands you ran and their actual output — not a claim that they passed |
| Coverage | Every new public symbol your diff adds, and the test file that reaches it — or `none` |
| Follow-ups | Work you deliberately left undone, each as a named task |

**`none` is an acceptable answer and often the correct one**; tests belong to the validator. The row exists because "is this covered?" is answered by the wrong evidence otherwise. Three separate pieces of production code shipped untested in one epic — a batch guard, a 112-line dependency, a helper with two of three call sites pinned — and each *looked* covered because something adjacent was: a test file in the same directory naming the same path but stopping a layer short, a scheduled test bead that would have been closed as already-satisfied, a symbol whose other callers were pinned. Naming the symbols turns the reviewer's job from reading a diff and imagining the gap into checking a list.

**To establish that a gap is real, mutate and show the rest of the suite does not notice.** A test that has never been red proves nothing; a mutation that reds only the new tests while the other forty stay green proves the gap existed.

Report failures as failures. A test that fails, a step you skipped, a check you couldn't run — say so, with the output. Never describe unverified work as verified, and never infer success from a command you didn't run.
