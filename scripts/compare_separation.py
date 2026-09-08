"""Optional, local-only listening experiment; not an application separation backend."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import distribution, version
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

# This repository-only runner deliberately uses a numerical/GPU environment, not
# the GUI application's dependencies or its CPU-only singing extra.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

Predict = Callable[[np.ndarray], dict[str, np.ndarray]]
STEMS = {"backing", "removed"}
SAM_REVISION = "20b65f56888142eebe7c37448c6f6b3b32600e9b"
SAM_SOURCE_REVISION = "bb4c6999d2677c7402360e426afc01ddfad6dce0"
SAM_SHA256 = "8c44fda9821fd9f2ec8977304e3c0f55290d9eacb6bbf25b4b8fb1f69c2a8c06"
DLL_HANDLES: list[Any] = []


@dataclass
class Backend:
    sample_rate: int
    channels: int
    chunk_frames: int
    overlap_frames: int
    predict: Predict
    metadata: dict[str, Any]
    window: str = "linear"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def require_checkpoint(path: Path, size: int, digest: str) -> None:
    if path.stat().st_size != size or sha256(path) != digest:
        raise ValueError(f"Checkpoint does not match its pinned size/SHA-256: {path.name}")


def read_audio(path: Path, sample_rate: int, channels: int) -> np.ndarray:
    info = sf.info(path)
    if (
        not 0 < info.duration <= 120 or info.channels not in (1, 2)
        or sample_rate <= 0 or channels not in (1, 2)
    ):
        raise ValueError("Use a mono/stereo excerpt of at most 120 seconds.")
    samples, source_rate = sf.read(path, dtype="float32", always_2d=True)
    if not np.isfinite(samples).all():
        raise ValueError("Input audio contains non-finite samples.")
    if channels == 1:
        samples = samples.mean(axis=1, keepdims=True)
    elif samples.shape[1] == 1:
        samples = np.repeat(samples, 2, axis=1)
    if source_rate != sample_rate:
        from scipy.signal import resample_poly

        divisor = math.gcd(source_rate, sample_rate)
        samples = resample_poly(samples, sample_rate // divisor, source_rate // divisor, axis=0)
    return np.asarray(samples, dtype=np.float32)


def separate_chunks(
    samples: np.ndarray, predict: Predict, chunk_frames: int, overlap_frames: int,
    *, window_kind: str = "linear",
) -> dict[str, np.ndarray]:
    if (
        samples.ndim != 2 or len(samples) == 0 or not np.isfinite(samples).all()
        or not 0 < overlap_frames < chunk_frames
    ):
        raise ValueError("Invalid audio or overlap configuration.")
    stride = chunk_frames - overlap_frames
    if window_kind == "hann":
        window = np.hanning(chunk_frames + 2)[1:-1].astype(np.float32)
    elif window_kind == "linear":
        window = np.ones(chunk_frames, dtype=np.float32)
        fade = np.linspace(0, 1, overlap_frames + 2, dtype=np.float32)[1:-1]
        window[:overlap_frames], window[-overlap_frames:] = fade, fade[::-1]
    else:
        raise ValueError(f"Unknown overlap window: {window_kind}")
    weights = np.zeros(len(samples), dtype=np.float32)
    result = {name: np.zeros_like(samples) for name in STEMS}
    starts = range(0, len(samples), stride)
    for index, start in enumerate(starts):
        length = min(chunk_frames, len(samples) - start)
        block = np.zeros((chunk_frames, samples.shape[1]), dtype=np.float32)
        block[:length] = samples[start:start + length]
        print(f"Separating chunk {index + 1}/{len(starts)}", flush=True)
        predictions = predict(block)
        if set(predictions) != STEMS:
            raise ValueError("Backend must return backing and removed stems.")
        for name, output in predictions.items():
            if output.shape != block.shape or not np.isfinite(output).all():
                raise ValueError(f"Invalid {name} output: expected finite {block.shape} audio.")
            result[name][start:start + length] += output[:length] * window[:length, None]
        weights[start:start + length] += window[:length]
    for name in result:
        result[name] /= weights[:, None]
    return result


def crop_audio(
    samples: np.ndarray, sample_rate: int, start: float, duration: float,
) -> np.ndarray:
    if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
        raise ValueError("Crop start must be nonnegative and duration must be positive.")
    first, count = round(start * sample_rate), round(duration * sample_rate)
    if count <= 0 or first + count > len(samples):
        raise ValueError("The requested listening range is outside the input audio.")
    return samples[first:first + count]


def write_results(
    output: Path, audio: dict[str, np.ndarray], sample_rate: int, metadata: dict[str, Any],
) -> None:
    if output.exists():
        raise FileExistsError(f"Choose a new output directory; refusing to replace {output}")
    if set(audio) != {"original", *STEMS}:
        raise ValueError("Results need original, backing, and removed audio.")
    shape = audio["original"].shape
    if len(shape) != 2 or shape[0] == 0 or shape[1] not in (1, 2):
        raise ValueError("Invalid output audio shape.")
    for name, samples in audio.items():
        if samples.shape != shape or not np.isfinite(samples).all():
            raise ValueError(f"Invalid {name} result.")
    peaks = {name: float(np.max(np.abs(samples))) for name, samples in audio.items()}
    preview_gain = min(1.0, 0.98 / max(peaks.values())) if max(peaks.values()) else 1.0
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".separation-comparison-", dir=output.parent) as tmp:
        stage = Path(tmp) / "result"
        stage.mkdir()
        for name, samples in audio.items():
            sf.write(stage / f"{name}.wav", samples, sample_rate, subtype="FLOAT")
            sf.write(
                stage / f"{name}-listen.wav", samples * preview_gain,
                sample_rate, subtype="PCM_16",
            )
        report = {
            **metadata,
            "sample_rate": sample_rate,
            "channels": shape[1],
            "frames": shape[0],
            "seconds": shape[0] / sample_rate,
            "raw_peak": peaks,
            "listening_gain": preview_gain,
            "interpretation": (
                "Raw FLOAT WAVs are not normalized. Listening WAVs share one safety gain within "
                "this run, not necessarily across runs. No clean reference stems are available; "
                "these measurements do not establish dialogue removal or singing preservation."
            ),
        }
        (stage / "report.json").write_text(
            json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8",
        )
        stage.rename(output)


def load_htdemucs(model_path: Path) -> Backend:
    import onnxruntime as ort

    manifest_path = (
        Path(__file__).resolve().parents[1] / "src" / "choicer_voicer_pack_creator"
        / "resources" / "backing-separation.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model = manifest["model"]
    require_checkpoint(model_path, model["bytes"], model["sha256"])
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_path), sess_options=options, providers=["CPUExecutionProvider"],
    )
    frames = manifest["input_shape"][-1]

    def predict(block: np.ndarray) -> dict[str, np.ndarray]:
        predictions = session.run(["stems"], {"mix": block.T[None].copy()})[0]
        if predictions.shape != (1, 4, 2, frames):
            raise ValueError("Unexpected HTDemucs output layout.")
        return {
            "backing": predictions[0, :3].sum(axis=0).T,
            "removed": predictions[0, 3].T,
        }

    return Backend(
        manifest["sample_rate"], 2, frames, frames // 4, predict,
        {
            "backend": "htdemucs",
            "checkpoint": model,
            "runtime": f"onnxruntime {ort.__version__}",
            "device": "cpu",
            "meaning": "drums + bass + other; removed = all vocals, including singing",
        },
    )


def cuda_preflight(*, bf16: bool) -> dict[str, Any]:
    import torch
    from torch.nn.functional import scaled_dot_product_attention

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. Run this backend on the GPU machine; no CPU fallback.")
    capability = torch.cuda.get_device_capability()
    cuda_version = tuple(int(part) for part in (torch.version.cuda or "0.0").split("."))
    if capability >= (12, 0) and cuda_version < (12, 8):
        raise RuntimeError("RTX 50-series GPUs need a compatible CUDA 12.8+ PyTorch build.")
    if bf16 and not torch.cuda.is_bf16_supported():
        raise RuntimeError("This SAM profile requires CUDA BF16 support.")
    dtype = torch.bfloat16 if bf16 else torch.float32
    probe = torch.ones((1, 1, 16, 16), device="cuda", dtype=dtype)
    if not torch.isfinite(probe @ probe).all() or not torch.isfinite(
        scaled_dot_product_attention(probe, probe, probe),
    ).all():
        raise RuntimeError("CUDA kernel preflight produced non-finite values.")
    torch.cuda.synchronize()
    free, total = torch.cuda.mem_get_info()
    return {
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "device": torch.cuda.get_device_name(),
        "capability": list(capability),
        "compiled_architectures": torch.cuda.get_arch_list(),
        "free_vram_bytes": free,
        "total_vram_bytes": total,
        "dtype": "bfloat16" if bf16 else "float32",
    }


def configure_sam_runtime(ffmpeg_bin: Path | None) -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["WANDB_MODE"] = "disabled"
    if os.name == "nt":
        if ffmpeg_bin is None:
            raise ValueError("Native Windows SAM needs --ffmpeg-bin pointing to FFmpeg 7 shared DLLs.")
        directory = ffmpeg_bin.resolve()
        for filename in ("avcodec-61.dll", "avformat-61.dll", "avutil-59.dll"):
            if not (directory / filename).is_file():
                raise FileNotFoundError(f"Missing FFmpeg 7 shared library: {directory / filename}")
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        DLL_HANDLES.append(os.add_dll_directory(str(directory)))


def require_sam_source_pin() -> None:
    direct_url = distribution("sam-audio").read_text("direct_url.json")
    if direct_url is None or json.loads(direct_url).get("vcs_info", {}).get(
        "commit_id",
    ) != SAM_SOURCE_REVISION:
        raise RuntimeError("Install the pinned SAM Git revision using Setup-Gpu.ps1.")


def preflight(backend: str, ffmpeg_bin: Path | None) -> dict[str, Any]:
    if backend == "htdemucs":
        import onnxruntime as ort

        if "CPUExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError("ONNX Runtime CPU provider is unavailable.")
        return {"device": "cpu", "onnxruntime": ort.__version__}
    if backend == "sam-audio":
        configure_sam_runtime(ffmpeg_bin)
    result = cuda_preflight(bf16=backend == "sam-audio")
    if backend == "bandit":
        import librosa  # noqa: F401
        import torchaudio  # noqa: F401

        from choicer_voicer_pack_creator._bandit import load_model  # noqa: F401
    else:
        require_sam_source_pin()
        import sam_audio  # noqa: F401
        import torchcodec  # noqa: F401
        import xformers  # noqa: F401
    return result


def load_bandit(model_path: Path) -> Backend:
    import torch

    from choicer_voicer_pack_creator._bandit import load_model

    model = load_model(model_path, "cuda")

    def predict(block: np.ndarray) -> dict[str, np.ndarray]:
        result = {name: np.zeros_like(block) for name in STEMS}
        with torch.inference_mode():
            # The trained network is mono; process stereo channels sequentially without downmixing.
            for channel in range(block.shape[1]):
                tensor = torch.from_numpy(block[:, channel].copy())[None, None].to("cuda")
                estimates = model({"mixture": {"audio": tensor}})["estimates"]
                result["backing"][:, channel] = (
                    estimates["music"]["audio"] + estimates["sfx"]["audio"]
                )[0, 0].float().cpu().numpy()
                result["removed"][:, channel] = (
                    estimates["speech"]["audio"][0, 0].float().cpu().numpy()
                )
        return result

    return Backend(
        48000, 2, 8 * 48000, 7 * 48000, predict,
        {
            "backend": "bandit-combined",
            "source_revision": "d5563d9031e95fdaa3e5a73d5020b9a0df61adb6",
            "checkpoint_sha256": sha256(model_path),
            "weights_license": "CC BY-NC 4.0; non-commercial uses only",
            "meaning": "music (including singing) + sfx; removed = speech",
            "channel_strategy": "mono network applied separately to left and right",
            "packages": {name: version(name) for name in ("torchaudio", "librosa")},
        }, "hann",
    )


def sam_overrides(model_dir: Path, t5_dir: Path) -> dict[str, Any]:
    config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    required = {"text_encoder", "span_predictor", "text_ranker", "visual_ranker"}
    if not required.issubset(config) or not isinstance(config["text_encoder"], dict):
        raise ValueError("Unexpected SAM config; cannot ensure auxiliary models are disabled.")
    for filename in ("config.json", "model.safetensors"):
        if not (t5_dir / filename).is_file():
            raise FileNotFoundError(f"Missing local T5 asset: {t5_dir / filename}")
    return {
        "text_encoder": {**config["text_encoder"], "name": str(t5_dir.resolve())},
        "span_predictor": None,
        "text_ranker": None,
        "visual_ranker": None,
    }


def load_sam(model_dir: Path, t5_dir: Path, prompt: str, chunk_seconds: float) -> Backend:
    import torch
    from sam_audio import SAMAudio, SAMAudioProcessor

    checkpoint = model_dir / "checkpoint.pt"
    require_checkpoint(checkpoint, 5100547943, SAM_SHA256)
    overrides = sam_overrides(model_dir, t5_dir)
    model = SAMAudio.from_pretrained(str(model_dir.resolve()), **overrides)
    model = model.eval().to(device="cuda", dtype=torch.bfloat16)
    processor = SAMAudioProcessor.from_pretrained(str(model_dir.resolve()))
    if processor.audio_sampling_rate != 48000:
        raise ValueError("Unexpected SAM sample rate.")

    def predict(block: np.ndarray) -> dict[str, np.ndarray]:
        batch = processor(
            audios=[torch.from_numpy(block.T.copy())], descriptions=[prompt],
        ).to("cuda")
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            result = model.separate(batch, predict_spans=False, reranking_candidates=1)
        outputs = {}
        for name, waveform in (("backing", result.residual[0]), ("removed", result.target[0])):
            if waveform.ndim != 1 or waveform.numel() < len(block):
                raise ValueError("SAM returned an unexpected or truncated mono waveform.")
            outputs[name] = waveform[:len(block)].float().cpu().numpy()[:, None]
        return outputs

    return Backend(
        48000, 1, round(chunk_seconds * 48000), 2 * 48000, predict,
        {
            "backend": "sam-audio-small",
            "source_revision": SAM_SOURCE_REVISION,
            "model_revision": SAM_REVISION,
            "checkpoint_sha256": SAM_SHA256,
            "config_sha256": sha256(model_dir / "config.json"),
            "t5_weights_sha256": sha256(t5_dir / "model.safetensors"),
            "prompt": prompt,
            "auxiliary_models": "disabled at construction",
            "meaning": "generatively predicted residual; removed = speech target",
            "channel_strategy": "stereo input downmixed to mono",
            "limitation": "waveform overlap-add is not the paper's latent MultiDiffusion",
        }, "hann",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("htdemucs", "bandit", "sam-audio"), required=True)
    parser.add_argument("--preflight", action="store_true", help="Check runtime without loading weights.")
    parser.add_argument("--input", type=Path, help="Prepared WAV with surrounding context (max 120s).")
    parser.add_argument("--output", type=Path, help="New results directory; never overwritten.")
    parser.add_argument("--model", type=Path, help="Local HTDemucs ONNX or BandIt combined checkpoint.")
    parser.add_argument("--sam-model", type=Path, help="Pinned local sam-audio-small directory.")
    parser.add_argument("--t5-model", type=Path, help="Pinned local t5-base directory.")
    parser.add_argument("--ffmpeg-bin", type=Path, help="FFmpeg 7 shared-build bin directory (Windows SAM).")
    parser.add_argument("--crop-start", type=float, default=5, help="Seconds relative to prepared input.")
    parser.add_argument("--crop-duration", type=float, default=31)
    parser.add_argument("--sam-prompt", default="speech")
    parser.add_argument("--sam-chunk-seconds", type=float, choices=(5, 8), default=8)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args(argv)
    if not args.preflight:
        if args.input is None or args.output is None:
            parser.error("--input and --output are required for inference.")
        if args.output.exists():
            parser.error("--output already exists; choose a new directory.")
        if args.backend == "sam-audio":
            if args.sam_model is None or args.t5_model is None or not args.sam_prompt.strip():
                parser.error("SAM requires --sam-model, --t5-model, and a nonempty prompt.")
        elif args.model is None:
            parser.error("This backend requires --model.")
        info = sf.info(args.input)
        if not 0 < info.duration <= 120 or info.channels not in (1, 2):
            parser.error("Use a mono/stereo input excerpt of at most 120 seconds.")
        if (
            not math.isfinite(args.crop_start) or not math.isfinite(args.crop_duration)
            or args.crop_start < 0 or args.crop_duration <= 0
            or args.crop_start + args.crop_duration > info.duration
        ):
            parser.error("The listening range must be inside the input excerpt.")
    hardware = preflight(args.backend, args.ffmpeg_bin)
    print(json.dumps(hardware, indent=2), flush=True)
    if args.preflight:
        print("Runtime preflight completed. Checkpoint loading and inference have not run.")
        return 0
    started = time.monotonic()
    input_hash = sha256(args.input)
    runner_hash = sha256(Path(__file__))
    if args.backend != "htdemucs":
        import torch

        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        torch.cuda.reset_peak_memory_stats()
    if args.backend == "htdemucs":
        backend = load_htdemucs(args.model)
    elif args.backend == "bandit":
        backend = load_bandit(args.model)
    else:
        backend = load_sam(args.sam_model, args.t5_model, args.sam_prompt, args.sam_chunk_seconds)
    original = read_audio(args.input, backend.sample_rate, backend.channels)
    stems = separate_chunks(
        original, backend.predict, backend.chunk_frames, backend.overlap_frames,
        window_kind=backend.window,
    )
    audio = {
        name: crop_audio(samples, backend.sample_rate, args.crop_start, args.crop_duration)
        for name, samples in {"original": original, **stems}.items()
    }
    metadata = {
        **backend.metadata,
        "status": "completed",
        "hardware": hardware,
        "python": sys.version,
        "platform": platform.platform(),
        "input_sha256": input_hash,
        "runner_sha256": runner_hash,
        "input_seconds": len(original) / backend.sample_rate,
        "crop_start_seconds": args.crop_start,
        "crop_duration_seconds": args.crop_duration,
        "chunk_frames": backend.chunk_frames,
        "overlap_frames": backend.overlap_frames,
        "window": backend.window,
        "seed": args.seed,
        "wall_seconds": time.monotonic() - started,
        "packages": {
            **backend.metadata.get("packages", {}),
            **{name: version(name) for name in ("numpy", "soundfile", "scipy")},
        },
    }
    if args.backend != "htdemucs":
        metadata["peak_cuda_allocated_bytes"] = torch.cuda.max_memory_allocated()
        metadata["peak_cuda_reserved_bytes"] = torch.cuda.max_memory_reserved()
    if sha256(args.input) != input_hash:
        raise RuntimeError("Input audio changed during inference; results were not published.")
    write_results(args.output, audio, backend.sample_rate, metadata)
    print(f"Saved listening files and provenance to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
