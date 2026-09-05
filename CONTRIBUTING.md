# Contributing

Contributions are welcome. This project is an unofficial community utility and is not affiliated with the creators of *The Choicer Voicer*.

## Coding agents and concurrent sessions

LLM/coding agents must follow [AGENTS.md](AGENTS.md). Each independent change uses a fresh
isolated branch/worktree, stays current with `origin/main`, and is delivered through a
submitted PR merged into `main` unless the user limits the scope or a blocker prevents
completion. Never modify another active session's checkout or work directly on `main`.

## Development setup

1. Install Python 3.11 or newer and FFmpeg with `ffmpeg` and `ffprobe` on `PATH`.
2. Create a virtual environment: `python -m venv .venv`.
3. Activate it and install: `python -m pip install -e ".[dev]"`.
4. Run `pytest` and `ruff check .` before submitting a change.

## Design rules

- Preserve imported media unless a user explicitly replaces or regenerates it.
- Stage exports completely before replacing an existing destination.
- Keep the project JSON as the editable source of truth; generated pack files are outputs.
- Validate metadata references, timestamps, codecs, images, ZIP inventory, and decodeability.
- Never silently discard an unsupported field from an imported pack; report it as a warning and
	refuse in-place conversion of the source pack.
- Avoid placing diagnostic files in exported pack folders because the game may interpret them as clip metadata.

## Pull requests

Explain the user-visible behavior, list validation performed, and include tests for format or export changes. Do not include copyrighted source video or voice assets in the repository.

Release candidates should pass `scripts/validate_external.py` with both required flags against an
exported integration fixture, in addition to lint, pytest, and packaged-application smoke tests.
The Windows packaging step must use the pinned LGPL manifest and retain all generated FFmpeg
license/provenance files. Do not substitute a GPL, nonfree, floating, or unchecked binary.

Pushes and pull requests do not run GitHub Actions. Complete all checks locally. After bumping and
committing the release version, a maintainer can manually run **Create GitHub Release** from the
Actions page. That release-only workflow builds the validated portable ZIP and attaches it, together
with a SHA-256 checksum, to a new versioned GitHub Release.

The portable build also generates `portable-files.json` after all application files have been
assembled. Keep this inventory and the exact versioned Windows ZIP/checksum asset names: the
updater uses them to verify releases and distinguish shipped files from user-owned content.
`Build-Portable.ps1` exercises the packaged updater, including its staged, Qt-free helper entry
point, waiting for the old process, preserving extra files, and restarting the updated application.
Keep `--apply-update` and `--update-result=` compatible across releases; source installations do
not self-update. Never embed a GitHub token in the application.
