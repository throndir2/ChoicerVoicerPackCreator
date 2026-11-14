# Copilot repository instructions

**Status:** Implemented
**Last Updated:** 2026-11-13
**Audience:** Coding agents
**Last verified:** 2026-11-13 against `f905faf` plus the bootstrap changes
**Code:** `.github/copilot-instructions.md`, `AGENTS.md`, `scripts/kb/check_kb.py`

Read and follow [`AGENTS.md`](../AGENTS.md) for the complete repository-wide agent workflow,
and [`CONTRIBUTING.md`](../CONTRIBUTING.md) for development and design requirements.
Use [docs/README.md](../docs/README.md) to find the knowledge base (KB).

## Knowledge Base First (Required)

To find, search, or move through the repository, call `kb_search` on the `repo-kb` MCP
server. Open the cited sections with `kb_read_section`. Use their `**Code:**` lines to
find the source, then read only the code you need. Do not use broad grep, glob, directory
listings, or file reads to explore. Use them only to find an exact name given by the KB.
If the KB has no answer or a wrong answer, find it in the code and fix the doc in the same PR.

## Keep the KB true (Required)

A PR that adds, renames, moves, or deletes code, config, scripts, commands, or behavior
must update the KB in the same PR. Update the page, its `**Code:**` and
`**Last verified:**` lines, and the folder index. Give a new feature a page or section.
Delete a removed feature or mark it Superseded. If no doc needs a change, put
`KB: no change needed` and the reason in the PR body. Before merge, run
`python scripts\kb\check_kb.py --diff origin/main` and fix every error.

For every repository-changing task, use a fresh isolated feature branch/worktree, fetch
the latest `origin/main` before editing and before PR submission/merge, validate and
commit the change, push it, submit a non-draft PR to `main`, and merge it unless the user
explicitly limits the scope or repository requirements block completion. A fresh
task-specific worktree provided by the app counts as the required isolated worktree.
Never modify another session's worktree or the shared main checkout. Confirm the PR is
actually merged and fetch the latest `origin/main` afterward; report blockers explicitly.
