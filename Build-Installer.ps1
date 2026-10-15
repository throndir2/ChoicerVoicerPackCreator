<#
.SYNOPSIS
Builds the Windows Setup installer from the validated portable application folder.

.DESCRIPTION
Run Build-Portable.ps1 first. This script reads dist\v<version>\latest-portable.json,
compiles installer\ChoicerVoicerPackCreator.iss with Inno Setup 6, and writes
Choicer-Voicer-Pack-Creator-<version>-Setup.exe and its .sha256 file beside the ZIP.

.PARAMETER InnoSetupCompiler
Optional path to ISCC.exe. By default the script looks in the standard Inno Setup 6 folders.
#>
[CmdletBinding()]
param(
    [string] $InnoSetupCompiler
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($repositoryRoot)) {
    $repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}

if ([string]::IsNullOrWhiteSpace($InnoSetupCompiler)) {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        $candidates = @($command.Source) + $candidates
    }
    $InnoSetupCompiler = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($InnoSetupCompiler) -or
    -not (Test-Path -LiteralPath $InnoSetupCompiler -PathType Leaf)) {
    throw "Inno Setup 6 is required. Install it from https://jrsoftware.org/isinfo.php or run: winget install --id JRSoftware.InnoSetup -e --scope user"
}

$projectText = Get-Content -LiteralPath (Join-Path $repositoryRoot "pyproject.toml") -Raw
$match = [regex]::Match($projectText, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $match.Success) {
    throw "Could not read the project version from pyproject.toml."
}
$version = $match.Groups[1].Value
$numeric = [regex]::Match($version, '^(\d+)\.(\d+)\.(\d+)')
if (-not $numeric.Success) {
    throw "The project version is not a supported semantic version: $version"
}
$fileVersion = "$($numeric.Groups[1].Value).$($numeric.Groups[2].Value).$($numeric.Groups[3].Value).0"

$distributionRoot = Join-Path $repositoryRoot "dist\v$version"
$latestManifestPath = Join-Path $distributionRoot "latest-portable.json"
if (-not (Test-Path -LiteralPath $latestManifestPath -PathType Leaf)) {
    throw "No validated portable build was found. Run Build-Portable.ps1 first: $latestManifestPath"
}
$manifest = Get-Content -LiteralPath $latestManifestPath -Raw | ConvertFrom-Json
if ([string] $manifest.version -ne $version) {
    throw "The validated portable build has the wrong version."
}
$applicationDirectory = [IO.Path]::GetFullPath((Join-Path $repositoryRoot ([string] $manifest.application_directory)))
if (-not (Test-Path -LiteralPath (Join-Path $applicationDirectory "Choicer Voicer Pack Creator.exe") -PathType Leaf)) {
    throw "The validated portable application folder is incomplete: $applicationDirectory"
}
if (Test-Path -LiteralPath (Join-Path $applicationDirectory "setup-install.json")) {
    throw "The portable application folder must not contain the Setup marker."
}

$setupName = "Choicer-Voicer-Pack-Creator-$version-Setup.exe"
$setupPath = Join-Path $distributionRoot $setupName
Remove-Item -LiteralPath $setupPath, "$setupPath.sha256" -Force -ErrorAction SilentlyContinue

# ISCC cannot read paths over 260 characters, so read the deep build folder through a short link.
$shortSource = Join-Path ([IO.Path]::GetTempPath()) ("cvpc-iss-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Junction -Path $shortSource -Target $applicationDirectory | Out-Null
Write-Host "==> Compiling the Setup installer with Inno Setup" -ForegroundColor Cyan
$started = Get-Date
try {
    & $InnoSetupCompiler /Q `
        "/DAppVersion=$version" `
        "/DFileVersion=$fileVersion" `
        "/DSourceDir=$shortSource" `
        "/DOutputDir=$distributionRoot" `
        (Join-Path $repositoryRoot "installer\ChoicerVoicerPackCreator.iss")
    $compilerExitCode = $LASTEXITCODE
}
finally {
    # Delete only the link, never the files it points to.
    [IO.Directory]::Delete($shortSource, $false)
}
if ($compilerExitCode -ne 0) {
    throw "Inno Setup failed with exit code $compilerExitCode."
}
if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
    throw "Inno Setup did not produce $setupPath"
}

$hash = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $setupName" | Out-File -FilePath "$setupPath.sha256" -Encoding ascii
$size = (Get-Item -LiteralPath $setupPath).Length

Write-Host ""
Write-Host "Setup installer is ready." -ForegroundColor Green
Write-Host "Installer: $setupPath"
Write-Host ("Size: {0:N0} bytes; compile time: {1:N0} s" -f $size, ((Get-Date) - $started).TotalSeconds)
Write-Host "SHA-256: $hash"
