# Disk cleanup

Builds, tests, and agent sessions in this repository make a large quantity of generated
output. This guide tells you what the output is, where it is, and how to remove it safely.

## What this repository makes

All sizes were measured in the main checkout on 2026-10-09. At that time the checkout used
7.5 GB, and 6.9 GB of it was generated output.

| Path | Made by | Measured size | Can I remove it? |
| --- | --- | --- | --- |
| `dist\v<version>\` | `Build-Portable.ps1` and `scripts\build.py`: portable application folders (`portable-*`), candidate and promoted ZIPs, and build manifests | 4.6 GB for 9 versions; 0.4 to 1.3 GB for each version | Yes. GitHub Releases keep the published ZIPs. Keep the current version only if you must share or test that local ZIP. |
| `dist\` other folders | Earlier packaging experiments and runtime checks | 0.1 GB each | Yes. |
| `build\` | PyInstaller work folders (`pyinstaller-v<version>`), FFmpeg staging, smoke-test scratch folders, task-owned build environments (`build\environments\<task>`), and agent screenshots and notes | 1.8 GB | Yes. The next build makes the files again. |
| `.cache\ffmpeg\` | `scripts\ffmpeg_bundle.py`: the downloaded LGPL FFmpeg archive | 0.3 GB | Yes. The next build downloads and verifies it again. |
| `.venv\`, `.build-venv\` | Development setup in `CONTRIBUTING.md` | 0.7 GB | Yes, but you must make it again before you develop. |
| `.pytest_cache\`, `.ruff_cache\`, `__pycache__\`, `htmlcov\`, `.coverage` | `pytest`, `ruff`, and Python | Less than 1 MB | Yes. |
| Root `*.spec` files | Older PyInstaller runs | Less than 1 MB | Yes. |
| `%LOCALAPPDATA%\ChoicerVoicerPackCreator\BuildEnvironments\python-3.12-x64` | `Build-Portable.ps1` default build environment, shared by all checkouts | 0.65 GB | Only when no build runs in any checkout. |
| `%LOCALAPPDATA%\ChoicerVoicerCommunity\Choicer Voicer Pack Creator` | The application: settings, consented model downloads, and the optional CUDA runtime cache (several GiB when installed) | 0.34 GB | No. This is user data. Remove it only when the user asks. |
| App-managed worktrees, for example `C:\Repositories\copilot-worktrees\ChoicerVoicerPackCreator\<session>` | The Copilot app, one for each session | Each worktree can have its own `build\`, `dist\`, and `.venv\` | Only through the app, or when the session is finished. |
| Local branches | One for each session | 114 local branches, 107 of them merged | Yes, when merged into `origin/main`. |

## When to clean

- Before you finish a session, remove the large output that you made in that session.
- After a release is published on GitHub, remove the local `dist\v<version>\` folders.
- After many local builds. Each build adds a new `portable-*` folder and ZIP.
- When the disk is low.

## Clean with the script

`Clean-Workspace.ps1` cleans the checkout that contains it. It is a dry run by default:
it shows each target and its size and deletes nothing.

1. Stop the application, tests, and builds that use files in the checkout.
2. Open PowerShell in the checkout that you want to clean.
3. Run a dry run:

   ```powershell
   .\Clean-Workspace.ps1
   ```

4. Read the list. Make sure that you do not need any of the items.
5. Delete the items:

   ```powershell
   .\Clean-Workspace.ps1 -Apply
   ```

The default categories are `Build`, `Dist`, and `Caches`. The script keeps
`dist\v<current version>` unless you add `-IncludeCurrentDist`.
Use `-Include` to select other categories:

| Category | What it removes |
| --- | --- |
| `Build` | `build\` and root `*.spec` files |
| `Dist` | `dist\` except the current version folder |
| `Caches` | `.cache\`, `.pytest_cache\`, `.ruff_cache\`, `__pycache__\`, `htmlcov\`, `.coverage` |
| `Venvs` | `.venv\` and `.build-venv\` |
| `Worktrees` | Stale Git worktree records whose folders were already deleted |
| `Branches` | Local branches that are in `origin/main` or whose tip is the head of a merged pull request |
| `All` | All of the categories above |

Examples:

```powershell
# Remove everything that the script can remove, including the development environment.
.\Clean-Workspace.ps1 -Include All -IncludeCurrentDist -Apply

# Remove only merged local branches. Fetch first so that origin/main is current.
git fetch origin main
.\Clean-Workspace.ps1 -Include Branches -Apply

# Show what is in another checkout. Use -Apply there only when the user asks.
.\Clean-Workspace.ps1 -Path C:\Repositories\ChoicerVoicerPackCreator
```

### What the script never removes

- Tracked files. If a folder contains a tracked file, the script does not remove that folder.
- Untracked files that Git does not ignore. These can be uncommitted work.
- Ignored files that are not in a category, for example `.env`, `*.local.json`, and editor
  settings. The script lists them as "not touched".
- The targets of links and junctions. The script removes a link, not the folder that it points to.
- Other worktree folders. The script only prunes records for worktrees that are already gone.
- `main`, branches that a worktree has checked out, and branches that are not merged.
- Data outside the checkout, for example the shared build environment and application data.

The `Branches` category uses `gh pr list` to find squash-merged and rebase-merged branches.
Without `gh`, it finds only branches that are contained in `origin/main`.

## Clean by hand

Use these commands when the script is not available. Run them from the checkout root.

1. Make sure that the paths are ignored and have no tracked files:

   ```powershell
   git status --short --ignored
   git ls-files -- build dist .cache .venv
   ```

   The second command must show nothing.

2. Remove the generated output:

   ```powershell
   Remove-Item -LiteralPath build, dist, .cache -Recurse -Force -ErrorAction SilentlyContinue
   ```

3. To remove the shared build environment, make sure that no build runs in any checkout. Then
   either delete it or let the build script make it again:

   ```powershell
   Remove-Item -LiteralPath "$env:LOCALAPPDATA\ChoicerVoicerPackCreator\BuildEnvironments\python-3.12-x64" -Recurse -Force
   # or
   .\Build-Portable.ps1 -ResetBuildEnvironment
   ```

## Worktrees and branches

The Copilot app owns the session worktrees. Archive a finished session in the app to remove
its worktree. Do not remove another session's worktree yourself.

When a worktree is not managed by the app:

1. List the worktrees:

   ```powershell
   git worktree list
   ```

2. Make sure that the worktree has no uncommitted work:

   ```powershell
   git -C <worktree-path> status --short
   ```

   The command must show nothing.

3. Remove the worktree. Do not use `--force`.

   ```powershell
   git worktree remove <worktree-path>
   ```

To find merged branches by hand, use `git branch --merged origin/main`. This command does not
find squash-merged branches. Use the script's `Branches` category for those.
