<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal; each role adds its own Bash-scope line in its role section -->

## Tool Usage

| Need | Use |
|---|---|
| Find files by name or glob | The glob tool |
| Search file contents | The search tool |
| Read a file | The read tool — not `cat`, `head`, or `tail` |
| Edit a file | The edit tool — not `sed`, `awk`, or shell redirection |

Prefer the dedicated tool over its shell equivalent: shell variants each need their own permission entry, and their output isn't tracked the way a tool result is.

Independent calls belong in one message so they run concurrently. Calls that depend on an earlier result do not.

Delegate anything that would take many calls or dump large output — log retrieval, broad searches, test runs. Your context is the scarce resource, and exhausting it costs the whole task rather than the step.
