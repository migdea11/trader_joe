<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal, pulled into CLAUDE.md via @ -->

## Escalation

Stop and ask rather than guess.

| Trigger | Action |
|---|---|
| The task contradicts what the code actually does | Report the contradiction; don't silently pick a side |
| A fix is needed outside your scope | Name the file and the change; don't reach across the seam |
| A decision has no obvious default and changes the design | Ask — with the options, their trade-offs, and your recommendation |
| A signature change would ripple to callers you can't enumerate | Stop. "Out of scope" becomes a named follow-up task, never an assumption |
| Your verification can't run at all | Report the work as unverified and say why |
| Two instructions conflict | Quote both and ask which wins |

When you present a decision, give every option with its pros and cons, then say which you'd take and what it costs. A survey without a recommendation moves the work back onto the person who asked.
