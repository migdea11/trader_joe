---
name: prepare-pr
description: Regroup (squash) a branch's commits into logical, reviewable commits and prepare a pull request. Use when asked to squash commits, tidy a branch before review, or open a PR. Never pushes — the human does that.
---

# prepare-pr

Turns a working branch's commit-by-commit history into the handful of commits a
reviewer should read, then produces the PR text. The work is plain git; the
judgement is the grouping, and the grouping is the user's to approve.

Implements `recipes/publish-step.md` from the agent-workflow kit.

## Inputs — resolve these, never assume them

| Input | How |
|---|---|
| Base branch | `protected_branches[0]` in `.claude/workflow.yml`, or an explicit parameter. **Never infer it from the current checkout.** |
| Source branch | The branch being prepared. State it back before doing anything. |
| Publish command | `publish.command` in the manifest. |

Say both out loud in your first message. A regroup onto the wrong base is
discovered only after the diff looks absurd.

## Procedure

### 1. Back up first
`git branch backup/pre-squash-<date> <source>`. Say the name. Everything below
is verified against it, and it is the only thing that makes a mistake cheap.

### 2. Propose the grouping — and stop

Present a table: new commit, which originals it absorbs, and the theme. Then
**wait for approval.** Do not rewrite anything first and present it as done.

Grouping rules:
- **One theme per commit.** Contiguity in history is not a theme. A dependency
  bump, a database upgrade and a bug fix that happen to be adjacent are three
  commits, because a reviewer may want to revert one without the others.
- **Fixups fold into the commit they fix.** A rework belongs with the work.
- **A whole-tree reformat stays alone** — see the blame-ignore rule below.
- Commits that rode along (a docs tweak, a config ratification) get lifted out
  and grouped with their own kind, even if that means reordering.

### 3. Rebuild onto a new branch

Never rewrite the original. `git switch -C <release-branch> <base>`, then per
group:

- **Contiguous run, no reordering:** `git read-tree -u --reset <group-end>` then
  commit. Sets index and worktree to exactly that commit's tree.
- **Reordered or non-contiguous:** `git cherry-pick -n <sha>` for each commit in
  the group, then one commit. Abort and fall back to contiguous grouping if a
  conflict appears — a conflict means the reorder was not as independent as it
  looked.

**Trap:** `git merge --squash` is the wrong tool here. It performs a real
three-way merge and conflicts immediately against a freshly built branch.

**Trap:** `read-tree --reset` restores the original tree, silently reverting any
in-flight fix made during an earlier group. Apply such fixes after the last
group, then verify.

### 4. Fix the blame-ignore file

If `.git-blame-ignore-revs` exists, it names the reformat commit's SHA — and
rewriting history changes that SHA. Update it to the new one, and confirm
`git merge-base --is-ancestor <sha> HEAD`. Leaving it stale ships a file
pointing at a commit that vanishes when the old branch is deleted, and
`git blame` then fails instead of skipping the reformat.

### 5. Verify — content identity, not a vibe

```
git diff <backup> <release-branch>
```

Expect **no output**, with exactly one permitted exception: the single changed
line in `.git-blame-ignore-revs` from step 4. Anything else means the regroup
lost or invented content. Show the command's real output; do not summarise it.

Then also check:
- Every subject ≤ 72 characters, house format, `Bead:` the only trailer.
  Agents re-add `Co-Authored-By` and session links against convention — grep for
  them and strip them.
- **Drop the `[agent-name]` tag from regrouped subjects.** A working commit
  carries the name of the agent that wrote it; a regrouped commit absorbs
  several, so any single tag is false. Keep the `Bead:` trailer — list more than
  one where a group spans beads.
- `make lint` and `make test` on the final tree.
- Each commit self-consistent, if the branch is long enough to warrant it.

### 6. Produce the PR text

Write the body to stand alone: no bead ids, no internal task references — the
reader has neither. Lead with what a reviewer needs to know first (security,
then breakage, then the rest), and include a section naming what is deliberately
*not* fixed.

Prefer a prefilled link from `publish.command`. If the URL exceeds ~8 KB
(GitHub's practical limit), fall back to a title-only link plus a body file at
`.claude/publish/pr-body.md`. That path is scratch: gitignore it, and delete it
once the PR is open.

### 7. Hand over — never push

Print the push command and the link. **No agent pushes.** The human decides when
work leaves the machine, and that is the backstop no injected instruction can
reach.

## Checklist

- [ ] Base branch resolved explicitly, stated back
- [ ] Backup branch created and named
- [ ] Grouping table presented and approved **before** any rewrite
- [ ] Original branch untouched
- [ ] `git diff <backup> <new>` empty but for the blame-ignore line
- [ ] Blame-ignore SHA resolves on the new branch
- [ ] Subjects ≤ 72, `Bead:` the only trailer
- [ ] Lint and tests pass on the final tree
- [ ] PR body stands alone; scratch files cleaned up
- [ ] Nothing pushed
