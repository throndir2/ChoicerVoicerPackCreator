# Singing-preserving separation experiment

This is an optional, local-only developer experiment, **not a new application backend**.
It compares the current HTDemucs instrumental backing with singing-aware BandIt and
SAM Audio's speech-removal residual. The editor, automatic generation, portable build,
and application dependencies are unchanged.

The initial target is native Windows, Python 3.11, and an NVIDIA RTX 5080 (16 GB).
CUDA 12.8 builds support this GPU; old cu121/cu124 installs are not substitutes.
The GPU environment installation, actual inference, and VRAM fit must be established on
that machine. The preparation machine has no CUDA GPU. Source inspection and synthetic
audio tests do not establish model quality or guarantee native-Windows compatibility.

## Models and licenses

| Candidate | Retained audio | Important qualification |
| --- | --- | --- |
| Current HTDemucs ONNX | Drums + bass + other | Deliberately excludes singing along with other vocals. CPU baseline, same pinned model as the app. |
| BandIt singing-aware combined | Music + sound effects | The [Facing the Music](https://arxiv.org/html/2408.03588v2) training target explicitly includes singing in music. Do not substitute the ordinary DnR-v3 checkpoint, whose music training data excludes vocals. |
| SAM Audio small, prompt `speech` | Predicted residual | Experimental, generative, mono output. It is not exact subtraction or a guarantee that singing survives. |

BandIt architecture code is Apache-2.0; its notices and original-source hashes are in
`scripts\_bandit`. The [singing-aware weights](https://zenodo.org/records/13327983)
are **CC BY-NC 4.0**: use this profile for non-commercial research/evaluation, not as
an unrestricted production feature.

SAM code and weights use the [SAM License](https://github.com/facebookresearch/sam-audio/blob/bb4c6999d2677c7402360e426afc01ddfad6dce0/LICENSE).
Its standard dependency installation includes ImageBind, licensed **CC BY-NC-SA 4.0**.
Keep those packages in a separate evaluation environment, outside the portable app.
Review the individual upstream licenses before installation/use. No SAM source or model
weights are included in this repository or its application build.

## Prepare the common input

Use authorized local media. Do not commit videos, audio excerpts, weights, credentials,
environments, or results. The app's current MCP API does not expose YouTube downloading;
the editor's importer or its underlying yt-dlp technology can download public videos.

For the requested [Genshin short](https://www.youtube.com/watch?v=tN5JACOEJFM),
the listening interval is **01:09-01:40**. Prepare 01:04-01:45 as model input, retaining
five seconds of context on each side:

```powershell
ffmpeg -nostdin -n -ss 64 -i C:\AudioComparison\source.mkv -t 41 -map 0:a:0 -vn -ac 2 -ar 44100 -c:a pcm_f32le C:\AudioComparison\input-with-context.wav
```

All commands below run from the repository/transfer-bundle root. The default crop is
input-relative 5-36 seconds, yielding exactly 31 seconds of output. For a different
excerpt, explicitly set `--crop-start` and `--crop-duration`; input is limited to 120 seconds.

## Install and preflight BandIt

Install 64-bit Python 3.11, Git, and a current NVIDIA driver. Use a new environment,
not the editor's environment or a `--system-site-packages` environment. Setup may
download several GB of packages and stops on any installation failure.

The setup script defaults to the Windows `py -3.11` launcher. If Python was installed
without that launcher (for example, with `uv`), pass
`-PythonExecutable C:\Path\To\Python311\python.exe` to either setup profile. The script
checks for 64-bit Python 3.11 before creating the environment. An isolated `uv` install
can stay entirely inside the experiment without changing PATH or registering Python:

```powershell
$env:UV_PYTHON_INSTALL_DIR = "C:\AudioComparison\python"
uv python install 3.11 --no-bin --no-registry
uv python find 3.11 --managed-python --no-python-downloads
```

Use the interpreter path printed by that command for `-PythonExecutable`; do not use
the installed application's interpreter or modify global packages.

Some Windows applications set a user-level `PYTHONPATH` to their own Python DLLs
(for example, SVP's Python 3.12). That can break even a Python 3.11 virtual environment
with `Module use of python312.dll conflicts with this version of Python`. Setup uses
Python's isolated mode (`-I`) for its interpreter and pip commands. The comparison
commands below use `-E -s` to ignore ambient Python overrides and user packages while
still allowing the repository's `scripts` module. Neither changes global settings.

```powershell
.\tools\separation-comparison\Setup-Gpu.ps1 -Backend BandIt -Environment C:\AudioComparison\venv-bandit
& C:\AudioComparison\venv-bandit\Scripts\python.exe -E -s -m scripts.compare_separation --backend bandit --preflight
```

Obtain `bandit-combined.ckpt` from the official record:

```powershell
curl.exe --fail --location --output C:\AudioComparison\bandit-combined.ckpt "https://zenodo.org/records/13327983/files/bandit-combined.ckpt?download=1"
```

Expected size: 446,680,129 bytes. Published MD5: `d04760e77bb947668d8f5582d36b45a0`.
SHA-256 established after matching that publication:
`ebcd8a3c8c783aa8f3379c0cab925b76987f4f8959dfb595a84426817e1ffb60`.
The loader verifies the checkpoint before weights-only, strict state loading.

```powershell
& C:\AudioComparison\venv-bandit\Scripts\python.exe -E -s -m scripts.compare_separation --backend bandit --input C:\AudioComparison\input-with-context.wav --model C:\AudioComparison\bandit-combined.ckpt --output C:\AudioComparison\results-bandit
```

BandIt uses 48 kHz, eight-second windows with a one-second hop, batch size one, float32,
and sequential inference on left/right channels. No training dependencies, GPU compilation,
or automatic CPU fallback are used.

For the current-model baseline, obtain the exact ONNX file described in
`src\choicer_voicer_pack_creator\resources\backing-separation.json` (the prepared transfer
bundle already contains it), then:

```powershell
& C:\AudioComparison\venv-bandit\Scripts\python.exe -E -s -m scripts.compare_separation --backend htdemucs --input C:\AudioComparison\input-with-context.wav --model C:\AudioComparison\htdemucs.onnx --output C:\AudioComparison\results-htdemucs
```

This baseline deliberately stays on ONNX Runtime CPU, like the app. It uses the same
model and chunk layout, but preserves raw floating-point stems for comparison instead
of applying the app's final backing-only safety gain.

## Install and preflight SAM Audio

The native Windows profile requires **FFmpeg 7 shared libraries**, including
`avcodec-61.dll`, `avformat-61.dll`, and `avutil-59.dll`. A static `ffmpeg.exe` alone
does not satisfy TorchCodec. Use an independently installed FFmpeg 7 shared `bin`
directory; do not replace the app's FFmpeg 9 installation. A current Microsoft Visual
C++ x64 runtime may also be needed.

```powershell
.\tools\separation-comparison\Setup-Gpu.ps1 -Backend SamAudio -Environment C:\AudioComparison\venv-sam
& C:\AudioComparison\venv-sam\Scripts\python.exe -E -s -m scripts.compare_separation --backend sam-audio --preflight --ffmpeg-bin C:\FFmpeg7Shared\bin
```

The profile pins torch 2.9.1/cu128, matching TorchAudio/TorchVision/xformers, TorchCodec
0.8.1, and upstream Git snapshots. It is not a fully frozen transitive dependency lock.
Preflight checks actual CUDA/BF16 kernels and native imports without downloading/loading
model weights. An import, DLL, resolver, or driver failure is a blocker, not permission
to silently downgrade packages or use CPU.

Request access to [facebook/sam-audio-small](https://huggingface.co/facebook/sam-audio-small)
in your browser and personally review/accept its terms. After approval, authenticate
privately on the GPU machine; never send a token to an assistant or include it in a bundle.
Successful login does not grant gated model approval: an HTTP 403 saying the request
is awaiting review is a blocker until the model's authors approve it. Continue the
other comparisons without trying to bypass that gate.
The SAM checkpoint is about 5.1 GB; T5 adds about 0.9 GB. Model loading also needs
substantial free system RAM in addition to VRAM.

```powershell
& C:\AudioComparison\venv-sam\Scripts\python.exe -I -m huggingface_hub.cli.hf auth login
& C:\AudioComparison\venv-sam\Scripts\python.exe -I -m huggingface_hub.cli.hf download facebook/sam-audio-small --revision 20b65f56888142eebe7c37448c6f6b3b32600e9b --include config.json checkpoint.pt LICENSE README.md --local-dir C:\AudioComparison\models\sam-audio-small
& C:\AudioComparison\venv-sam\Scripts\python.exe -I -m huggingface_hub.cli.hf download google-t5/t5-base --revision a9723ea7f1b39c1eae772870f3b547bf6ef7e6c1 --include "*.json" "*.model" model.safetensors --local-dir C:\AudioComparison\models\t5-base
& C:\AudioComparison\venv-sam\Scripts\python.exe -E -s -m scripts.compare_separation --backend sam-audio --input C:\AudioComparison\input-with-context.wav --sam-model C:\AudioComparison\models\sam-audio-small --t5-model C:\AudioComparison\models\t5-base --ffmpeg-bin C:\FFmpeg7Shared\bin --output C:\AudioComparison\results-sam
```

Inference sets Hugging Face/Transformers offline mode before imports and uses only local
models. Optional span predictors and rankers are disabled **before construction**.
No Judge, ImageBind, or separate PE model weights need downloading for this profile.
The unused built-in vision encoder still resides in the stock SAM model.

SAM uses BF16, eight-second windows, two-second overlap, one candidate, and seed 1234.
This is a conservative starting profile, not a measured 16 GB memory guarantee.
If CUDA reports out-of-memory, explicitly retry with `--sam-chunk-seconds 5` and a
new output directory. Do not silently change settings between comparisons.
Waveform overlap-add is an approximation, not the paper's latent MultiDiffusion method.

## Listen and interpret

Each successful run creates a new directory containing:

- `original.wav`, `backing.wav`, `removed.wav`: unnormalized float WAVs.
- `*-listen.wav`: PCM listening copies sharing one safety gain **within that run**.
- `report.json`: input/checkpoint hashes, model/runtime settings, duration, timing, and GPU memory.

Existing directories are never overwritten. Failed runs do not publish a success directory.
All 48 kHz results must have 1,488,000 frames; the HTDemucs 44.1 kHz baseline has 1,367,100.
SAM's reference and results are mono; compare against its mono reference, not just the
untouched stereo video. Independent-channel BandIt processing can also affect spatial coherence.
Listening gains can differ between runs: use the raw files at a common playback gain for
level-sensitive judgments, rather than mistaking attenuation for better separation.

For this excerpt, assess singing preservation during 69-97.672 seconds separately from
the caption-indicated dialogue near 97.672-100 seconds. Captions are not proof that every
other sound is non-speech. Listen to both retained and removed tracks: singing in the
removed track is evidence of the failure we want to avoid. Also listen for dialogue leakage,
missing effects, altered timbre, and chunk-boundary artifacts.

There are no clean reference stems. Neither loudness nor reconstruction error can establish
singing preservation, and SDR claims would be unjustified. Do not enable a production
"preserve singing" option until actual listening results, hardware behavior, and licensing
support it.
