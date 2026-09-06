# Repository agent instructions

These instructions apply to every LLM/coding agent working anywhere in this repository.
Read `CONTRIBUTING.md` for development setup, design rules, and release requirements.

## Default completion contract

Unless the user explicitly limits the task to planning, investigation, or local-only work, a
request to change this repository includes the full delivery workflow: use an isolated
branch/worktree, sync with the latest `main`, implement and validate the change, commit and
push it, submit a ready-for-review pull request targeting `main`, and merge that PR.
Do not stop at an uncommitted diff, a local commit, or an unmerged PR when the remaining
steps are available. Respect required reviews, checks, and repository protections; if
anything blocks delivery, report the blocker and PR URL rather than claiming completion.

## Isolate concurrent sessions

- Assume other agents and users are working in parallel at all times.
- Every independent change must use a new, uniquely named feature branch and its own
  worktree based on the latest `origin/main`. Never develop or commit directly on `main`.
- A fresh, task-specific branch/worktree supplied by the hosting app already satisfies
  this requirement. Use it rather than creating a redundant nested worktree. Use the
  app's session and branch-management tools when available.
- Do not reuse a branch whose PR has already merged. Follow-up independent work needs a
  fresh branch/worktree.
- Inspect `git status`, the current branch, and `git worktree list` before modifying files.
  Work only in this task's worktree. Never switch branches, stash, reset, clean, rebase,
  pull, or edit files in another session's worktree or the shared main checkout.
- Preserve unrelated or uncommitted work. Stage only this task's changes, never overwrite
  another agent's work, and do not force-push shared history.
- Do not delete or prune branches/worktrees belonging to other sessions. Leave cleanup of
  app-managed worktrees to the app.

## Sync with main

1. Before starting edits, run `git fetch origin main` from this task's worktree. Create the
   task branch from the fetched `origin/main`, or fast-forward a fresh app-provided branch
   with `git merge --ff-only origin/main`.
2. For an in-progress task branch that has diverged, commit only this task's changes, then
   merge `origin/main` into the task branch. Do not reset the branch or discard commits to
   make it match `main`. Prefer merging over rewriting a branch already pushed for review.
3. Fetch and integrate `origin/main` again before submitting the PR and immediately before
   merging it. Other sessions may have merged since the task started. Resolve conflicts
   in this worktree, preserve both changes' intent, rerun affected validation, and push
   any integration commits before attempting the PR merge.
4. After GitHub confirms the PR is merged, fetch `origin/main` again so this session has
   the latest main history. New tasks must start from that refreshed remote branch.

Use `git pull --ff-only origin main` only in a clean checkout that belongs exclusively to
this task and can be fast-forwarded. Do not switch to or pull in the shared main checkout
to keep it current: fetching `origin/main` in this task's worktree obtains the latest
main without disturbing another session.

## Submit and merge

1. Follow the existing code conventions and `CONTRIBUTING.md`. Run the existing targeted
   checks appropriate to the change and expand validation when needed. Documentation-only
   changes need no build or test run unless the repository has relevant documentation tests.
   Pushes and PRs do not run GitHub Actions here; complete applicable checks locally.
2. Review the diff, commit only task-owned changes with a descriptive message, and push
   the feature branch with upstream tracking.
3. Create and submit a non-draft PR targeting `main` using the hosting app's PR tool when
   available, otherwise the GitHub CLI. Include the user-visible change, validation
   performed (or why it was not applicable), and any remaining limitations.
4. Address review feedback and required checks. After refreshing from `main`, merge the
   PR using a repository-supported merge method. Use a merge queue if required. Never
   bypass protections or required approvals. Auto-merge or queue enrollment is not proof
   that the PR has actually merged.
5. Confirm GitHub reports the PR as merged, refresh `origin/main`, and report the PR URL
   and outcome. If permissions, conflicts, required reviews, or failures prevent merging,
   clearly report the incomplete state instead of silently leaving the task unfinished.
