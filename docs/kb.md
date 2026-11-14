# Knowledge-base contract

**Status:** Partial
**Last Updated:** 2026-11-13
**Audience:** Developers and maintainers
**Last verified:** 2026-11-13 against `25a1d36` plus the search evaluation changes
**Code:** `AGENTS.md`, `.github/copilot-instructions.md`, `CONTRIBUTING.md`, `scripts/kb/check_kb.py`, `scripts/kb/test_check_kb.py`, `.repo-kb.json`, `docs/repo-kb-eval.json`

This page defines the repository's documentation rules. The contract, map, local checker, and search evaluation exist.
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

## Local search evaluation

[repo-kb-eval.json](repo-kb-eval.json) contains 30 real engineering questions with one expected
authoritative doc path for each question. The CLI format is a JSON array of
`{"q": "question", "expect": ["docs/path.md"]}` objects.
The published repo-kb CLI's `eval --help` lists the command; its `src/eval.js` defines this format.

Run these commands from the repository root:

```powershell
npx -y github:throndir2/repo-kb index --full
npx -y github:throndir2/repo-kb eval docs\repo-kb-eval.json --json
```

The default evaluation uses hybrid search: keyword matching plus local embeddings.
Wait for a complete embedding index. Confirm that the output says `repo-kb (hybrid)`,
not keyword search. Do not add `--keyword`, `--no-embed`, or `--wkb`.
The repository wiki is disabled; no wiki comparison is required.

Each hit measures whether the expected page appears within the first 1, 3, or 5 ranked files.
The CLI reports `hit@1`, `hit@3`, and `hit@5` as fractions, rounded to three decimal places.
Its `misses` array includes every question not answered at rank 1, including successful lower ranks.

### Measured retrieval quality

The local hybrid evaluation on 2026-11-13 used all 30 unchanged questions.
It covered project formats, import/export, editor state, MCP permissions and privacy,
captions, singing separation, resource admission, packaging, updates, cleanup, and KB work.
Questions ask about engineering tasks, not page titles.

| Measure | Before page improvements | After page improvements |
| --- | --- | --- |
| hit@1 | 0.600 (18/30) | 0.633 (19/30) |
| hit@3 | 0.900 (27/30) | 1.000 (30/30) |
| hit@5 | 1.000 (30/30) | 1.000 (30/30) |
| Mean reciprocal rank | 0.757 | 0.806 |

Mean reciprocal rank averages the reciprocal of each expected page's rank.
Missing pages contribute zero.
The final index contained 14 files and 193 fully embedded chunks.
The local path/index checker counts pages differently; its page count is not the search index count.

The weakest source-ownership results concerned document revisions, caption recognition, and BandIt runtime isolation.
The [code map](CODE_MAP.md) now explains their inputs, operations, source files, and tests.
Those expected paths moved from ranks 4, 5, and 4 to ranks 2, 3, and 1.
All expected paths now appear in the top three; 11 questions still return another page first.
Do not change expected paths or weaken questions merely to improve scores.

These results measure document retrieval, not answer correctness or runtime behavior.
The questions guided the page improvements; this is not an independent unseen-question evaluation.
Historical performance, real GPU behavior, and in-game compatibility were not reproduced.
No product code or hosted workflow changed for this evaluation.

## Planned local support

- Page generation and checks for metadata and verification dates are Planned. No generator or metadata check exists.
