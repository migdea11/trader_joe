<!-- generated from kit ee0119b — edit .claude/workflow.yml and re-render, not this file -->
<!-- delivery: import — universal, pulled into CLAUDE.md via @ -->
<!-- LOCALLY AMENDED 2026-09-22 per ADR tj-aov3ip (worktrees). `update` will flag this file as edited
     rather than overwrite it; send the change upstream with /workflow-promote. -->

## Working Directory

Run every command from the root of your working tree: the repository root, or the worktree you were given. Never `git -C <path>`, never `cd <path> && ...`.

Each command *spelling* needs its own permission-allowlist entry, so a new variant of a command you already have means a fresh approval prompt in the middle of your task. Pin the forms below. If you need an operation not listed, prefer the simplest standard form over inventing a variant.

| Purpose | Form |
|---|---|
| Recent commits | `git log --oneline -N` |
| Working state | `git status --short` |
| Changeset | `git diff <base>..HEAD` |
| Inspect a commit | `git show <sha>` |
| Current branch | `git branch --show-current` |
| Run tests | `make test PATHS=<path>` |
| Lint | `make lint PATHS=<path>` |
| Format | `make lint-fix PATHS=<path>` |

## Worktrees (project amendment, ADR tj-aov3ip)

Every agent that writes to the repository works in its own git worktree, never in the shared checkout. That covers builders, and validators that commit or mutate code. Agents run in parallel when their scopes are disjoint. Dependencies still serialise: work that builds on builder-shared's files waits for them to land. Read-only agents may read the shared checkout, which only ever moves by whole commits.

The harness creates the worktree. The orchestrator launches every writing agent with the Agent tool's `isolation: "worktree"`. That gives the agent its own worktree and branch (`worktree-agent-<id>`), and all of the agent's commands run there. The steps that touch the repository root belong to the orchestrator: fast-forwarding the feature branch and cleaning up.

These mechanics were tested on 2026-09-22. Two alternatives do not work:
- **An agent switching into a different worktree with EnterWorktree(path).** It moves the agent's cwd and write access, but not its shell, which stays locked to the launch worktree.
- **A plain `cd`.** It does not carry over between commands.

| Step | Who | Form |
|---|---|---|
| Launch | orchestrator | Agent tool with `isolation: "worktree"`. Name the feature branch in the prompt. |
| Base on the feature branch, first command | agent | `git merge --ff-only <feature-branch>`. The harness bases the worktree on `origin/main`, which is an ancestor of the feature branch, so this only moves your own branch forward. Confirm with `git log --oneline -1`. If it is refused, stop and report it. |
| Work and commit | agent | As usual, in the worktree. Tests and lint run there. |
| Catch up, before handing back | agent | `git merge --ff-only <feature-branch>` if you have no commits yet, otherwise `git rebase <feature-branch>`. This rebases only your own commits. Re-run tests and lint afterwards, then report your branch name, worktree path and commits. Don't call EnterWorktree or ExitWorktree, and don't touch the feature branch. |
| Integrate | orchestrator, repository root | `git merge --ff-only worktree-agent-<id>` |
| Clean up | orchestrator, repository root | `git worktree remove .claude/worktrees/agent-<id>`, then `git branch -d worktree-agent-<id>`. The harness removes an unchanged worktree by itself. |
| A worktree that will not remove | orchestrator | It is locked while the harness still holds the agent's session, including after a hand-back if that agent had background work of its own. Leave it: `remove -f -f` would pull the tree out from under a live agent. Scans and git already ignore it, and the harness releases it on its own. |
| Mutation testing | validator | Launched isolated like any writing agent. Mutate inside your own worktree and restore each file with `git checkout -- <path>`. Commit only the tests you mean to keep. |

Rules:

- **The orchestrator stays in the shared checkout while agents run.** An isolated session pins the agents it spawned to its own worktree. That strands them, read-only agents included. The orchestrator makes its own file edits only when no agent is running, or hands them to an agent.
- **Read-only and store-only agents launch without isolation.** They read the shared checkout.
- **Refused fast-forward at integration.** Another agent landed first. The orchestrator asks that agent to catch up again.
- **Rebase conflict.** Two scopes overlapped. Stop and report it; don't resolve it.
- **Separate environment.** A new worktree builds its own `.venv` on the first `make test`.
- **Task store.** `bd` finds the shared task store from inside a worktree. Never copy or initialise one.
- **Shared stash.** The stash is shared across worktrees. To set work aside, make a temporary commit on your own branch instead of stashing.
- **Repo-wide scans.** ruff, semgrep, git and the Docker build context all skip `.claude/worktrees`, so a scan from the root never reads another agent's live copy.

## Branch discipline (project amendment)

One session works in one line. The user reviews and merges by hand, so every extra ref is something they have to classify as live, scratch or abandoned — and a branch name they have never seen, appearing mid-task, costs them more than it saves you.

| Rule | Detail |
|---|---|
| One branch per unit of work | Named from its bead, created once, kept until its PR merges. Nothing else exists beside it. |
| No scratch branches | `prep/*`, `tmp/*` and second attempts under a new name are the thing this rule exists to stop. |
| Rewrites happen in place | "Never rewrite the original" is honoured with a **single backup ref**, not a new working branch. Rebuild on the real branch name, verify with an empty `git diff` against the backup, delete the backup in the same turn. |
| Agent worktrees feed the same branch | Already required above: agents fast-forward onto the feature branch and the orchestrator integrates with `merge --ff-only`. A worktree branch is never a second line of work. |
| Rebase first, cherry-pick second | Try `git rebase`. When it is structurally impossible, fall through to cherry-picking the commits that matter and say so — don't stop to deliberate. In this repo "impossible" means a pre-regroup branch whose patches are already upstream in squashed form, so the replay conflicts on the first of many commits. |
| Delete merged branches promptly | Prove redundancy before deleting: ancestry (`git merge-base --is-ancestor`), an empty `git diff` against the target, or a closed bead. Never sweep by name. Local branch deletion may need the user's approval — hand them one command rather than asking per branch. |
