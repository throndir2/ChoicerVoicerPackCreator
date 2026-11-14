# Knowledge-base contract

**Status:** Partial
**Last Updated:** 2026-11-13
**Audience:** Developers and maintainers
**Last verified:** 2026-11-13 against `f905faf` plus the bootstrap changes
**Code:** `AGENTS.md`, `.github/copilot-instructions.md`, `CONTRIBUTING.md`, `scripts/kb/check_kb.py`, `scripts/kb/test_check_kb.py`, `.repo-kb.json`

This page defines the repository's documentation rules. The contract, top-level map, and local path and index checker exist.
Page generation does not exist.

## Agent bootstrap and pull requests

[AGENTS.md](../AGENTS.md) and [Copilot instructions](../.github/copilot-instructions.md)
require agents to search the KB before exploring the repository. They also require PRs
to update affected KB pages and indexes, or explain why no doc change is needed.
[Contributing](../CONTRIBUTING.md#pull-requests) requires the local diff check before merge.

## Page metadata

Each KB page starts with an H1 title, followed by these fields in this order:

1. `**Status:**` with one value: Implemented, Partial, Planned, or Superseded.
2. `**Last Updated:**` with the date of the last content change in `YYYY-MM-DD` form.
3. `**Audience:**` with the intended readers.
4. `**Last verified:**` with a date and short Git commit ID, for example `2026-11-13 against \`17a278b\``.

A page about built code also has a `**Code:**` line. List its main source paths on that line. This line maps the KB page to the code it describes.

## Describe current behavior

Describe what the code does now. Do not describe intended behavior as if it already exists.

Put behavior that is not built in a section named `Planned`. Mark its status as Planned.
Update the page when behavior changes. Set its verification date and commit ID after checking the code.

## Indexes and maps

Each documentation folder has a `README.md` index. Add one line for every page.
Name the page and state the question it answers.

`docs/README.md` is the top-level documentation map. It identifies where each kind of knowledge lives and lists the main code areas and their folders.

## Generated pages

Scripts must generate KB pages from their source data. Do not edit generated pages by hand.
Change their source data and run the generator.

## Local checker

Run `python scripts\kb\check_kb.py` from the repository root.
The checker uses only the Python standard library. It needs no package installation.
Run `python scripts\kb\check_kb.py --diff origin/main` to add branch warnings.
The diff compares the named local Git reference with tracked working-tree changes, including staged changes.
Fetch the reference before this check. Untracked files are not in the diff.

The checker reports these results:

- Error: A path in a `**Code:**` line does not exist.
- Error: A folder index does not link to a KB page or a KB subfolder.
- Warning: A changed non-Markdown file has no Code path that names the file or a parent folder.
- Warning: A deletion or rename removes a path named by a current or base-reference Code line.

Errors return exit status 1. Warnings do not change the exit status.
An unreadable file, invalid configuration, or failed Git command is an error.
Code paths must be repository-relative. Use backticks, Markdown link targets, or comma-separated plain paths.
Backticks support spaces in paths. Both slash styles work. Parent traversal and absolute paths are errors.
The checker does not search other folders to repair invalid paths. It ignores Code examples inside fenced blocks.

### Discovery boundaries

`.repo-kb.json` supplies the indexed pages. Root Markdown guides and Markdown siblings of indexed guides are also pages.
This includes `THIRD_PARTY_NOTICES.md`, `MCP/README.md`, and the bundled resource index and help.
Standalone `.github` instruction files get Code checks but do not require a `.github/README.md`.
The checker checks all folders below `docs`, including empty folders.
Outside `docs`, it checks guide folders and subfolders that contain indexed pages, not resource-only folders.
The root index links to root pages and the top-level guide indexes. Each guide index links to its direct pages and KB subfolders.
Directory links and links to `README.md` both count. External URLs do not replace local index links.

The checker skips `.git`, `.cache`, `.venv`, `.build-venv`, `.pytest_cache`, `.ruff_cache`, and `__pycache__`.
It also skips `build`, `dist`, `htmlcov`, `node_modules`, and `_internal` folders.
An explicit Code path must still exist, even if it names generated output.

Run the focused tests with `python -m unittest discover -s scripts\kb -p test_check_kb.py`.
No active local Git hooks exist. This step does not create a hook or change hosted workflows.

## Planned local support

- Page generation and checks for metadata and verification dates are Planned. No generator or metadata check exists.
- A local evaluation of the map against code and documentation is Planned. No evaluation exists.
