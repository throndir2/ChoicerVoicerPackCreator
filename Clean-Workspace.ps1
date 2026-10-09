<#
.SYNOPSIS
Reports or removes large generated output from a ChoicerVoicerPackCreator checkout.

.DESCRIPTION
The script runs as a dry run by default: it lists each target and its size and
deletes nothing. Add -Apply to delete the listed targets.

The script removes only untracked paths that Git ignores. It never touches
tracked files, uncommitted changes, links or junctions, other worktree folders,
or application user data. See docs\CLEANUP.md.

.PARAMETER Include
Categories to clean. The default is Build, Dist, and Caches.
  Build     - build\ and root *.spec files (PyInstaller work, FFmpeg staging,
              smoke-test scratch, task-owned build environments)
  Dist      - dist\ except dist\v<current version> (portable folders and ZIPs)
  Caches    - .cache\, .pytest_cache\, .ruff_cache\, __pycache__\, htmlcov\, .coverage
  Venvs     - .venv\ and .build-venv\
  Worktrees - stale Git worktree records whose folders no longer exist
  Branches  - local branches already merged into origin/main
  All       - all categories

.PARAMETER IncludeCurrentDist
Also removes dist\v<current version>.

.PARAMETER Path
Checkout to clean. The default is the checkout that contains this script.

.PARAMETER Apply
Deletes the targets. Without this switch, the script only reports.

.EXAMPLE
.\Clean-Workspace.ps1

.EXAMPLE
.\Clean-Workspace.ps1 -Apply

.EXAMPLE
.\Clean-Workspace.ps1 -Include All -IncludeCurrentDist -Apply
#>
[CmdletBinding()]
param(
    [ValidateSet("Build", "Dist", "Caches", "Venvs", "Worktrees", "Branches", "All")]
    [string[]] $Include = @("Build", "Dist", "Caches"),
    [switch] $IncludeCurrentDist,
    [string] $Path,
    [switch] $Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Include -contains "All") {
    $Include = @("Build", "Dist", "Caches", "Venvs", "Worktrees", "Branches")
}
if ([string]::IsNullOrWhiteSpace($Path)) {
    $Path = $PSScriptRoot
}
$root = $Path

function Invoke-Git {
    # Windows PowerShell can turn native stderr into a terminating error.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git -C $root @args 2>$null
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return $output
}

function Invoke-GitWithMessages {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git -C $root @args 2>&1 | ForEach-Object { [string] $_ }
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return $output
}

$topLevel = Invoke-Git rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($topLevel)) {
    throw "Not a Git checkout: $Path"
}
$root = [IO.Path]::GetFullPath(([string] $topLevel).Trim()).TrimEnd("\")

function Get-LongPath([string] $FullPath) {
    if ($FullPath.StartsWith("\\?\")) {
        return $FullPath
    }
    if ($FullPath.StartsWith("\\")) {
        return "\\?\UNC\" + $FullPath.Substring(2)
    }
    return "\\?\" + $FullPath
}

function Format-Size([long] $Bytes) {
    if ($Bytes -ge 1GB) { return "{0:N2} GB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N0} MB" -f ($Bytes / 1MB) }
    return "{0:N0} KB" -f ($Bytes / 1KB)
}

function Measure-Tree([string] $FullPath, [switch] $ClearReadOnly) {
    $longPath = Get-LongPath $FullPath
    if ([IO.File]::Exists($longPath)) {
        return ([IO.FileInfo]::new($longPath)).Length
    }
    $total = [long] 0
    $pending = New-Object System.Collections.Generic.Stack[IO.DirectoryInfo]
    $pending.Push([IO.DirectoryInfo]::new($longPath))
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        try {
            $entries = $directory.GetFileSystemInfos()
        }
        catch {
            continue
        }
        foreach ($entry in $entries) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                continue
            }
            if ($ClearReadOnly -and ($entry.Attributes -band [IO.FileAttributes]::ReadOnly)) {
                $entry.Attributes = $entry.Attributes -band (-bnot [IO.FileAttributes]::ReadOnly)
            }
            if ($entry -is [IO.DirectoryInfo]) {
                $pending.Push($entry)
            }
            else {
                $total += $entry.Length
            }
        }
    }
    return $total
}

function Get-UnsafeReason([string] $Relative) {
    $fullPath = [IO.Path]::GetFullPath((Join-Path $root $Relative)).TrimEnd("\")
    if (-not $fullPath.StartsWith($root + "\", [StringComparison]::OrdinalIgnoreCase)) {
        return "it is outside the checkout"
    }
    if ($fullPath -match '(^|\\)\.git(\\|$)') {
        return "it is Git metadata"
    }
    $current = $fullPath
    while ($current.Length -gt $root.Length) {
        $attributes = [IO.File]::GetAttributes((Get-LongPath $current))
        if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
            return "it is or is inside a link or junction"
        }
        $current = Split-Path -Parent $current
    }
    $tracked = @(Invoke-Git ls-files -- $Relative) | Select-Object -First 1
    if ($null -ne $tracked) {
        return "it contains tracked files"
    }
    return $null
}

function Remove-Target([string] $FullPath) {
    $longPath = Get-LongPath $FullPath
    if ([IO.File]::Exists($longPath)) {
        [IO.File]::SetAttributes($longPath, [IO.FileAttributes]::Normal)
        [IO.File]::Delete($longPath)
        return
    }
    try {
        # Directory.Delete removes links without recursing into their targets.
        [IO.Directory]::Delete($longPath, $true)
    }
    catch {
        [void] (Measure-Tree $FullPath -ClearReadOnly)
        [IO.Directory]::Delete($longPath, $true)
    }
}

function Get-Category([string] $Relative) {
    $leaf = ($Relative -split "/")[-1]
    if ($Relative -eq "build" -or $Relative -match '^[^/]+\.spec$') { return "Build" }
    if ($Relative -eq "dist") { return "Dist" }
    if ($Relative -in @(".venv", ".build-venv")) { return "Venvs" }
    if ($Relative -in @(".cache", "htmlcov", ".coverage")) { return "Caches" }
    if ($leaf -in @("__pycache__", ".pytest_cache", ".ruff_cache") -or $leaf -match '\.py[cod]$') {
        return "Caches"
    }
    return $null
}

$version = $null
$versionLine = Select-String -LiteralPath (Join-Path $root "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' |
    Select-Object -First 1
if ($null -ne $versionLine) {
    $version = $versionLine.Matches[0].Groups[1].Value
}

if ($Apply) {
    Write-Host "Cleaning $root" -ForegroundColor Cyan
}
else {
    Write-Host "Dry run for $root (nothing is deleted)" -ForegroundColor Cyan
}

$targets = New-Object System.Collections.Generic.List[object]
$notTouched = New-Object System.Collections.Generic.List[string]
$ignored = (Invoke-Git ls-files -z --others --ignored --exclude-standard --directory) -join "" -split "`0" |
    Where-Object { $_ } |
    ForEach-Object { $_.TrimEnd("/") }

foreach ($relative in $ignored) {
    $category = Get-Category $relative
    if ($null -eq $category) {
        $notTouched.Add($relative)
        continue
    }
    if ($Include -notcontains $category) {
        continue
    }
    if ($category -eq "Dist") {
        $distribution = [IO.DirectoryInfo]::new((Get-LongPath (Join-Path $root "dist")))
        foreach ($child in $distribution.GetFileSystemInfos()) {
            if ($child.Name -eq "v$version" -and -not $IncludeCurrentDist) {
                Write-Host "Keeping the current version folder dist\v$version (add -IncludeCurrentDist to remove it)."
                continue
            }
            $targets.Add([pscustomobject]@{ Category = "Dist"; Relative = "dist/$($child.Name)" })
        }
        continue
    }
    $targets.Add([pscustomobject]@{ Category = $category; Relative = $relative })
}

$totalBytes = [long] 0
$failures = 0
foreach ($target in $targets) {
    $windowsRelative = $target.Relative.Replace("/", "\")
    $fullPath = Join-Path $root $windowsRelative
    $reason = Get-UnsafeReason $target.Relative
    if ($null -ne $reason) {
        Write-Warning "Skipping $windowsRelative because $reason."
        continue
    }
    $bytes = Measure-Tree $fullPath
    Write-Host ("  {0,-9} {1,10}  {2}" -f $target.Category, (Format-Size $bytes), $windowsRelative)
    if ($Apply) {
        try {
            Remove-Target $fullPath
            $totalBytes += $bytes
        }
        catch {
            $failures++
            Write-Warning "Could not remove $windowsRelative. Close programs that use it and try again. $($_.Exception.Message)"
        }
    }
    else {
        $totalBytes += $bytes
    }
}

if ($notTouched.Count -gt 0) {
    Write-Host "Other ignored paths are not touched: $(($notTouched | ForEach-Object { $_.Replace('/', '\') }) -join ', ')"
}

if ($Include -contains "Worktrees") {
    Write-Host "Worktrees:" -ForegroundColor Cyan
    $stale = @(Invoke-GitWithMessages worktree prune --dry-run --verbose | Where-Object { $_ })
    if ($stale.Count -eq 0) {
        Write-Host "  No stale worktree records."
    }
    else {
        $stale | ForEach-Object { Write-Host "  $_" }
        if ($Apply) {
            [void] (Invoke-GitWithMessages worktree prune)
            Write-Host "  Pruned stale worktree records."
        }
    }
    Write-Host "  Worktree folders are not removed. Archive finished sessions in the Copilot app, or see docs\CLEANUP.md."
}

if ($Include -contains "Branches") {
    Write-Host "Branches:" -ForegroundColor Cyan
    $mainCommit = Invoke-Git rev-parse --verify --quiet refs/remotes/origin/main
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($mainCommit)) {
        Write-Warning "origin/main is not available. Run 'git fetch origin main' and try again."
    }
    else {
        $checkedOut = @(Invoke-Git worktree list --porcelain |
            Where-Object { $_ -like "branch refs/heads/*" } |
            ForEach-Object { $_.Substring("branch refs/heads/".Length) })
        $mergedPullRequestHeads = @{}
        if (Get-Command gh -ErrorAction SilentlyContinue) {
            Push-Location $root
            $previous = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                $heads = & gh pr list --state merged --limit 1000 --json headRefOid --jq ".[].headRefOid" 2>$null
                $ghExitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previous
                Pop-Location
            }
            if ($ghExitCode -eq 0) {
                foreach ($head in @($heads)) { $mergedPullRequestHeads[([string] $head).Trim()] = $true }
            }
            else {
                Write-Warning "Could not read merged pull requests with gh. Only branches contained in origin/main are found."
            }
        }
        else {
            Write-Warning "gh is not installed. Only branches contained in origin/main are found."
        }

        $kept = 0
        $removed = 0
        foreach ($line in @(Invoke-Git for-each-ref refs/heads --format "%(refname:short)`t%(objectname)")) {
            $name, $commit = $line -split "`t"
            if ($name -eq "main" -or $checkedOut -contains $name) {
                $kept++
                continue
            }
            [void] (Invoke-Git merge-base --is-ancestor $commit $mainCommit)
            $inMain = $LASTEXITCODE -eq 0
            if (-not $inMain -and -not $mergedPullRequestHeads.ContainsKey($commit)) {
                $kept++
                continue
            }
            $how = if ($inMain) { "in origin/main" } else { "merged pull request" }
            if ($Apply) {
                [void] (Invoke-Git branch -D $name)
                if ($LASTEXITCODE -ne 0) {
                    $failures++
                    Write-Warning "Could not delete branch $name."
                    continue
                }
                Write-Host "  Deleted   $name ($how)"
            }
            else {
                Write-Host "  Would delete $name ($how)"
            }
            $removed++
        }
        $verb = if ($Apply) { "deleted" } else { "to delete" }
        Write-Host "  $removed merged branches $verb; $kept branches kept (main, checked out, or not merged)."
    }
}

if ($Apply) {
    Write-Host "Freed $(Format-Size $totalBytes)." -ForegroundColor Green
    if ($failures -gt 0) {
        Write-Warning "$failures items could not be removed."
        exit 1
    }
}
else {
    Write-Host "Dry run: $(Format-Size $totalBytes) would be freed. Run again with -Apply to delete." -ForegroundColor Green
}
