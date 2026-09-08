param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("BandIt", "SamAudio")]
    [string]$Backend,
    [Parameter(Mandatory = $true)]
    [string]$Environment,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed (exit $LASTEXITCODE). The environment is incomplete."
    }
}

if ($Environment -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+(?:\\|$))') {
    throw "Environment must be an absolute path outside your application installation."
}
if (Test-Path -LiteralPath $Environment) {
    throw "Choose a new environment directory. Existing environments are never modified."
}

$launcherArguments = @()
if (-not $PythonExecutable) {
    $PythonExecutable = "py"
    $launcherArguments = @("-3.11")
}
$pythonProbe = "import struct, sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and struct.calcsize('P') == 8 else 'This profile requires 64-bit Python 3.11.')"
Invoke-Checked $PythonExecutable ($launcherArguments + @("-c", $pythonProbe))
Invoke-Checked $PythonExecutable ($launcherArguments + @("-m", "venv", $Environment))
$python = Join-Path $Environment "Scripts\python.exe"
Invoke-Checked $python @("-m", "pip", "install", "pip==25.3")

if ($Backend -eq "BandIt") {
    Invoke-Checked $python @(
        "-m", "pip", "install", "torch==2.8.0", "torchaudio==2.8.0",
        "--index-url", "https://download.pytorch.org/whl/cu128"
    )
    Invoke-Checked $python @(
        "-m", "pip", "install", "numpy==1.26.4", "scipy==1.15.3",
        "librosa==0.10.2.post1", "soundfile==0.13.1", "onnxruntime==1.26.0"
    )
}
else {
    $constraints = Join-Path $PSScriptRoot "constraints-sam.txt"
    Invoke-Checked $python @(
        "-m", "pip", "install", "-c", $constraints,
        "torch==2.9.1", "torchaudio==2.9.1", "torchvision==0.24.1",
        "xformers==0.0.33.post2", "--index-url", "https://download.pytorch.org/whl/cu128"
    )
    Invoke-Checked $python @(
        "-m", "pip", "install", "-c", $constraints, "torchcodec==0.8.1",
        "--index-url", "https://download.pytorch.org/whl/cpu"
    )
    Invoke-Checked $python @(
        "-m", "pip", "install", "-c", $constraints,
        "git+https://github.com/facebookresearch/dacvae.git@414c20785fc3a28373073ea8ef7a1316eeeaca6e",
        "git+https://github.com/facebookresearch/ImageBind.git@53680b02d7e37b19b124fa37bae4b6c98c38f5be",
        "git+https://github.com/lematt1991/CLAP.git@b85cb7ecac0af7d064cbf03ebd990e709e959fe7",
        "git+https://github.com/facebookresearch/perception_models.git@e72b6810b1133e1c879f2cc965d276eb73803f1f"
    )
    Invoke-Checked $python @(
        "-m", "pip", "install", "-c", $constraints,
        "audiobox-aesthetics", "einops", "pydub", "torchdiffeq",
        "transformers", "huggingface-hub", "soundfile"
    )
    # Dependencies were installed explicitly above so upstream mutable Git URLs cannot replace them.
    Invoke-Checked $python @(
        "-m", "pip", "install", "--no-deps",
        "git+https://github.com/facebookresearch/sam-audio.git@bb4c6999d2677c7402360e426afc01ddfad6dce0"
    )
}

Invoke-Checked $python @("-m", "pip", "check")
Write-Output "Environment installed at $Environment. Run backend preflight before obtaining weights."
