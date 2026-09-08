<#
.SYNOPSIS
Builds and smoke-tests the self-contained Windows package.

.DESCRIPTION
Creates an isolated Python 3.12 build environment, installs the pinned build
dependencies, invokes the canonical Python packager, and verifies that the
packaged application and a fresh ZIP extraction select their bundled FFmpeg
and FFprobe executables.

.PARAMETER ResetBuildEnvironment
Deletes and recreates a build environment previously created by this script.

.PARAMETER BuildEnvironment
Optional task-owned virtual environment directory. Defaults to local application data.
#>
[CmdletBinding()]
param(
    [switch] $ResetBuildEnvironment,
    [string] $BuildEnvironment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($repositoryRoot)) {
    $repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$localApplicationData = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::LocalApplicationData
)
if ([string]::IsNullOrWhiteSpace($localApplicationData)) {
    throw "Could not locate the current user's local application-data directory."
}
if ([string]::IsNullOrWhiteSpace($BuildEnvironment)) {
    $BuildEnvironment = Join-Path `
        $localApplicationData `
        "ChoicerVoicerPackCreator\BuildEnvironments\python-3.12-x64"
}
if (-not [IO.Path]::IsPathRooted($BuildEnvironment)) {
    $BuildEnvironment = Join-Path $repositoryRoot $BuildEnvironment
}
$buildEnvironment = [IO.Path]::GetFullPath($BuildEnvironment).TrimEnd("\")
$environmentMarker = Join-Path $buildEnvironment ".cvpc-build-environment.json"
$buildPython = Join-Path $buildEnvironment "Scripts\python.exe"
$pythonProbe = "import struct, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string] $FilePath,

        [Parameter(Mandatory = $true)]
        [string[]] $ArgumentList,

        [Parameter(Mandatory = $true)]
        [string] $Description
    )

    Write-Host "==> $Description" -ForegroundColor Cyan
    & $FilePath @ArgumentList
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode."
    }
}

function Get-Sha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path
    )

    $stream = [IO.File]::OpenRead($Path)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
        }
        finally {
            $algorithm.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Get-BootstrapPython {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Probe
    )

    $candidates = New-Object System.Collections.Generic.List[object]
    $developmentPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $developmentPython -PathType Leaf) {
        [void] $candidates.Add(
            [pscustomobject]@{
                FilePath = $developmentPython
                PrefixArguments = [string[]] @()
            }
        )
    }
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $launcher) {
        [void] $candidates.Add(
            [pscustomobject]@{
                FilePath = $launcher.Source
                PrefixArguments = [string[]] @("-3.12")
            }
        )
    }
    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $python) {
        [void] $candidates.Add(
            [pscustomobject]@{
                FilePath = $python.Source
                PrefixArguments = [string[]] @()
            }
        )
    }

    foreach ($candidate in $candidates) {
        $candidatePath = [string] $candidate.FilePath
        $prefixArguments = @($candidate.PrefixArguments)
        $probeExitCode = -1
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            # Windows PowerShell can promote stderr from the Microsoft Store's
            # placeholder python.exe into a terminating NativeCommandError.
            $ErrorActionPreference = "Continue"
            & $candidatePath @prefixArguments -c $Probe 1>$null 2>$null
            $probeExitCode = $LASTEXITCODE
        }
        catch {
            $probeExitCode = -1
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($probeExitCode -eq 0) {
            return $candidate
        }
    }

    throw @"
A 64-bit Python 3.12 installation is required to build the Windows package.
Install Python 3.12 x64 from https://www.python.org/downloads/windows/ and
include the Python Launcher, then run this script again. Python is needed only
on the build computer; people using the finished package do not need it.
"@
}

function Assert-SafeBuildEnvironment {
    $root = [IO.Path]::GetPathRoot($buildEnvironment).TrimEnd("\")
    $repository = [IO.Path]::GetFullPath($repositoryRoot).TrimEnd("\")
    $homeDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if ($buildEnvironment.TrimEnd("\") -eq $root -or $buildEnvironment -eq $homeDirectory -or
        $repository -eq $buildEnvironment -or
        $repository.StartsWith($buildEnvironment + "\", [StringComparison]::OrdinalIgnoreCase) -or
        $buildEnvironment -match '(^|\\)\.git(\\|$)') {
        throw "Unsafe build environment path: $buildEnvironment"
    }
    $ancestor = $buildEnvironment
    while (-not [string]::IsNullOrWhiteSpace($ancestor)) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if (-not $item.PSIsContainer -or
                ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Build environment paths must be ordinary directories, not links: $ancestor"
            }
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    if ($ResetBuildEnvironment -and (Test-Path -LiteralPath $buildEnvironment)) {
        if (-not (Test-Path -LiteralPath $environmentMarker -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $buildEnvironment "pyvenv.cfg") -PathType Leaf)) {
            throw "Refusing to reset an unmarked environment. Choose a new -BuildEnvironment directory."
        }
        $marker = Get-Content -LiteralPath $environmentMarker -Raw | ConvertFrom-Json
        if ($marker.version -ne 1 -or $marker.path -ne $buildEnvironment -or
            $marker.repository -ne $repository) {
            throw "The build environment ownership marker is invalid."
        }
        $links = Get-ChildItem -LiteralPath $buildEnvironment -Recurse -Force |
            Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
            Select-Object -First 1
        if ($null -ne $links) {
            throw "Refusing to reset a build environment containing links or junctions."
        }
    }
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "The portable package can only be built on 64-bit Windows."
}

Assert-SafeBuildEnvironment
if ($ResetBuildEnvironment -and (Test-Path -LiteralPath $buildEnvironment)) {
    Write-Host "==> Removing the isolated build environment" -ForegroundColor Cyan
    Remove-Item -LiteralPath $buildEnvironment -Recurse -Force
}

if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
    if ((Test-Path -LiteralPath $buildEnvironment) -and
        (Get-ChildItem -LiteralPath $buildEnvironment -Force | Select-Object -First 1)) {
        throw "Refusing to create a build environment in a nonempty directory: $buildEnvironment"
    }
    $bootstrap = Get-BootstrapPython -Probe $pythonProbe
    $bootstrapPath = [string] $bootstrap.FilePath
    $createArguments = @($bootstrap.PrefixArguments)
    $createArguments += @("-m", "venv", $buildEnvironment)
    Invoke-CheckedCommand `
        -FilePath $bootstrapPath `
        -ArgumentList $createArguments `
        -Description "Creating the isolated Python 3.12 build environment"
    @{ version = 1; path = $buildEnvironment; repository = $repositoryRoot } | ConvertTo-Json |
        Set-Content -LiteralPath $environmentMarker -Encoding UTF8
}

& $buildPython -c $pythonProbe 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "The build environment is not 64-bit Python 3.12. Choose a new -BuildEnvironment, or reset an environment previously created by this script."
}

Push-Location $repositoryRoot
$extractedSmokeRoot = $null
$previousPipConfig = $env:PIP_CONFIG_FILE
try {
    # Disable all pip config files as well as environment/index overrides (--isolated).
    $env:PIP_CONFIG_FILE = "nul"
    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @(
            "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-input",
            "--no-index", "--no-deps", "--require-hashes", "-r", "tools\singing-cpu-wheels.txt"
        ) `
        -Description "Installing SHA-256-pinned official PyTorch and TorchAudio CPU wheels"
    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @(
            "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-input",
            "--index-url", "https://pypi.org/simple", "--editable", ".[build,singing]"
        ) `
        -Description "Installing the pinned application and packaging dependencies"
    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @("-m", "pip", "--isolated", "check") `
        -Description "Checking installed dependency consistency"
    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @(
            "-c",
            "import pathlib,tomllib; from choicer_voicer_pack_creator import __version__; expected=tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version']; raise SystemExit(0 if __version__ == expected else 1)"
        ) `
        -Description "Verifying source and package versions match"
    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @("scripts\build.py") `
        -Description "Building an unpromoted portable application and ZIP candidate"

    $versionOutput = & $buildPython -c "import pathlib, tomllib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not determine the packaged application version."
    }
    $version = (($versionOutput | ForEach-Object { [string] $_ }) -join "").Trim()
    $distributionRoot = Join-Path $repositoryRoot "dist\v$version"
    $manifestPath = Join-Path $distributionRoot "pending-portable.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "The pending portable-build manifest was not generated: $manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ([string] $manifest.version -ne $version) {
        throw "The portable build manifest has the wrong application version."
    }
    $applicationDirectory = Join-Path $repositoryRoot ([string] $manifest.application_directory)
    $applicationExecutable = Join-Path $repositoryRoot ([string] $manifest.executable)
    $candidateArchive = Join-Path $repositoryRoot ([string] $manifest.candidate_archive)
    if (-not (Test-Path -LiteralPath $applicationExecutable -PathType Leaf)) {
        throw "The expected portable executable was not generated: $applicationExecutable"
    }
    if (-not (Test-Path -LiteralPath $candidateArchive -PathType Leaf)) {
        throw "The expected portable ZIP candidate was not generated: $candidateArchive"
    }

    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @("scripts\smoke_packaged.py", $applicationExecutable, "--update-smoke") `
        -Description "Smoke-testing the application, bundled FFmpeg, and in-place updater"

    $extractedSmokeRoot = Join-Path `
        (Join-Path $repositoryRoot "build") `
        ("cvpc-portable-smoke-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $extractedSmokeRoot | Out-Null
    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @("-m", "zipfile", "-e", $candidateArchive, $extractedSmokeRoot) `
        -Description "Extracting the distributable ZIP into a clean temporary directory"
    $extractedExecutable = Join-Path `
        $extractedSmokeRoot `
        "Choicer Voicer Pack Creator\Choicer Voicer Pack Creator.exe"
    if (-not (Test-Path -LiteralPath $extractedExecutable -PathType Leaf)) {
        throw "The distributable ZIP does not contain the expected executable."
    }
    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @("scripts\smoke_packaged.py", $extractedExecutable) `
        -Description "Smoke-testing a fresh extraction of the distributable ZIP"

    Invoke-CheckedCommand `
        -FilePath $buildPython `
        -ArgumentList @("scripts\build.py", "--promote", ([string] $manifest.build_id)) `
        -Description "Promoting the validated ZIP to the stable share filename"

    $latestManifestPath = Join-Path $distributionRoot "latest-portable.json"
    if (-not (Test-Path -LiteralPath $latestManifestPath -PathType Leaf)) {
        throw "The validated portable-build manifest was not published: $latestManifestPath"
    }
    $latestManifest = Get-Content -LiteralPath $latestManifestPath -Raw | ConvertFrom-Json
    if ([string] $latestManifest.build_id -ne [string] $manifest.build_id) {
        throw "The published portable-build manifest does not match the validated generation."
    }
    $archive = Join-Path $repositoryRoot ([string] $latestManifest.archive)
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        throw "The validated portable ZIP was not promoted: $archive"
    }
    $archiveHash = Get-Sha256 -Path $archive

    Write-Host ""
    Write-Host "Portable build is ready." -ForegroundColor Green
    Write-Host "Run locally: $applicationExecutable"
    Write-Host "Share this ZIP: $archive"
    Write-Host "ZIP SHA-256: $archiveHash"
    Write-Host ""
    Write-Host "Recipients only need to extract the entire ZIP and run the EXE; no installation is required."
    Write-Host "Older portable-* generation folders may be deleted when no copy of the app is running."
}
finally {
    $env:PIP_CONFIG_FILE = $previousPipConfig
    if ($null -ne $extractedSmokeRoot -and (Test-Path -LiteralPath $extractedSmokeRoot)) {
        Remove-Item -LiteralPath $extractedSmokeRoot -Recurse -Force
    }
    Pop-Location
}