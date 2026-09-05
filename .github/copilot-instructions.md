# Copilot repository instructions

Read and follow [`AGENTS.md`](../AGENTS.md) for the complete repository-wide agent workflow,
and [`CONTRIBUTING.md`](../CONTRIBUTING.md) for development and design requirements.

For every repository-changing task, use a fresh isolated feature branch/worktree, fetch
the latest `origin/main` before editing and before PR submission/merge, validate and
commit the change, push it, submit a non-draft PR to `main`, and merge it unless the user
explicitly limits the scope or repository requirements block completion. A fresh
task-specific worktree provided by the app counts as the required isolated worktree.
Never modify another session's worktree or the shared main checkout. Confirm the PR is
actually merged and fetch the latest `origin/main` afterward; report blockers explicitly.
