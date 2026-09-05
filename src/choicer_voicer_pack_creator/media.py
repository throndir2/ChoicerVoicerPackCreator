from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from choicer_voicer_pack_creator.diagnostics import (
    diagnostic_event,
    diagnostic_exception,
    diagnostic_operation,
    diagnostic_text,
)

ProgressCallback = Callable[[str], None]


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str
    audio_codec: str
    pixel_format: str
    audio_sample_rate: int
    audio_channels: int

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else 16 / 9


@dataclass(frozen=True, slots=True)
class AudioInfo:
    duration: float
    codec: str
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class DecodedAudioStats:
    duration: float
    leading_quiet: float
    trailing_quiet: float
    has_activity: bool


class MediaTools:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
        diagnostic_event(
            "media_tools_requested", ffmpeg=ffmpeg, ffprobe=ffprobe,
            explicit_paths=ffmpeg is not None or ffprobe is not None,
        )
        if (ffmpeg is None) != (ffprobe is None):
            raise MediaError("Provide both ffmpeg and ffprobe, or neither")
        if ffmpeg and ffprobe:
            self.ffmpeg = str(Path(ffmpeg).resolve())
            self.ffprobe = str(Path(ffprobe).resolve())
        else:
            self.ffmpeg, self.ffprobe = self._find_tool_pair()
        diagnostic_event("media_tools_selected", ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        self._verify_capabilities()

    @staticmethod
    def _find_tool_pair() -> tuple[str, str]:
        suffix = ".exe" if sys.platform == "win32" else ""
        executable_dir = Path(sys.executable).resolve().parent
        application_dir = Path(sys.argv[0]).resolve().parent
        directories = [
            application_dir,
            application_dir / "bin",
            executable_dir,
            executable_dir / "bin",
        ]
        path_ffmpeg = shutil.which("ffmpeg")
        path_ffprobe = shutil.which("ffprobe")
        if path_ffmpeg:
            directories.append(Path(path_ffmpeg).resolve().parent)
        if path_ffprobe:
            directories.append(Path(path_ffprobe).resolve().parent)
        checked: set[Path] = set()
        for directory in directories:
            directory = directory.resolve()
            if directory in checked:
                continue
            checked.add(directory)
            ffmpeg = directory / f"ffmpeg{suffix}"
            ffprobe = directory / f"ffprobe{suffix}"
            if ffmpeg.is_file() and ffprobe.is_file():
                return str(ffmpeg), str(ffprobe)
        diagnostic_event("media_tools_not_found", checked_directories=sorted(map(str, checked)))
        raise MediaError(
            "A paired ffmpeg/ffprobe installation was not found. Put both tools on PATH or "
            "beside the application."
        )

    def _verify_capabilities(self) -> None:
        encoders = self.run(
            [self.ffmpeg, "-hide_banner", "-encoders"], "Checking FFmpeg encoders"
        ).stdout
        missing = [
            encoder
            for encoder in ("libtheora", "libvorbis", "libmp3lame")
            if encoder not in encoders
        ]
        diagnostic_event(
            "media_encoder_capabilities", required=["libtheora", "libvorbis", "libmp3lame"],
            missing=missing,
        )
        if missing:
            raise MediaError(
                "The selected FFmpeg build lacks required encoders: " + ", ".join(missing)
            )
        version = self.run([self.ffprobe, "-version"], "Checking FFprobe").stdout
        diagnostic_event(
            "media_tools_verified", ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            ffprobe_version=version.splitlines()[0][:512] if version else "",
        )

    @staticmethod
    def _startup_info() -> subprocess.STARTUPINFO | None:  # type: ignore[name-defined]
        if sys.platform != "win32":
            return None
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return startup

    @staticmethod
    def _command_started(command: Sequence[str], description: str) -> tuple[float, str]:
        command_id = uuid.uuid4().hex[:12]
        diagnostic_event(
            "media_command_started", command=[str(item) for item in command], description=description,
            command_id=command_id,
        )
        return time.monotonic(), command_id

    @staticmethod
    def _command_finished(
        returncode: int, stderr: str | bytes, started: tuple[float, str], *, canceled: bool = False,
    ) -> None:
        truncated = len(stderr) > 4096
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        diagnostic_event(
            "media_command_canceled" if canceled else (
                "media_command_completed" if returncode == 0 else "media_command_failed"
            ),
            returncode=returncode, returncode_hex=f"0x{returncode & 0xFFFFFFFF:08X}",
            duration_seconds=round(time.monotonic() - started[0], 3), command_id=started[1],
            stderr=diagnostic_text(stderr, limit=4096), stderr_truncated=truncated,
        )

    def run(self, command: Sequence[str], description: str) -> subprocess.CompletedProcess[str]:
        started = self._command_started(command, description)
        try:
            completed = subprocess.run(
                [str(item) for item in command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=self._startup_info(),
                check=False,
            )
        except OSError as error:
            diagnostic_exception(
                "media_command_launch_failed", error, command=[str(item) for item in command],
                duration_seconds=round(time.monotonic() - started[0], 3), command_id=started[1],
            )
            raise
        self._command_finished(completed.returncode, completed.stderr, started)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Unknown FFmpeg error"
            raise MediaError(f"{description} failed: {detail}")
        return completed

    @diagnostic_operation("media_probe")
    def probe(self, path: Path) -> MediaInfo:
        diagnostic_event("media_probe_requested", path=path)
        completed = self.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            f"Probing {path.name}",
        )
        data = json.loads(completed.stdout)
        streams = data.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if video is None:
            raise MediaError(f"{path.name} does not contain a video stream")
        duration_text = data.get("format", {}).get("duration") or video.get("duration") or 0
        duration = float(duration_text)
        rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
        numerator, denominator = (rate.split("/", 1) + ["1"])[:2]
        fps = float(numerator) / max(1.0, float(denominator))
        result = MediaInfo(
            duration=duration,
            width=int(video.get("width", 0)),
            height=int(video.get("height", 0)),
            fps=fps,
            has_audio=audio is not None,
            video_codec=str(video.get("codec_name", "")),
            audio_codec=str(audio.get("codec_name", "")) if audio else "",
            pixel_format=str(video.get("pix_fmt", "")),
            audio_sample_rate=int(audio.get("sample_rate", 0)) if audio else 0,
            audio_channels=int(audio.get("channels", 0)) if audio else 0,
        )
        diagnostic_event(
            "media_probed", path=path, duration=result.duration, width=result.width, height=result.height,
            fps=result.fps, has_audio=result.has_audio, video_codec=result.video_codec,
            audio_codec=result.audio_codec, pixel_format=result.pixel_format,
            audio_sample_rate=result.audio_sample_rate, audio_channels=result.audio_channels,
        )
        return result

    def probe_audio_duration(self, path: Path) -> float:
        return self.probe_audio(path).duration

    @diagnostic_operation("media_audio_probe")
    def probe_audio(self, path: Path) -> AudioInfo:
        diagnostic_event("media_audio_probe_requested", path=path)
        completed = self.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,duration,sample_rate,channels:format=duration",
                "-of",
                "json",
                str(path),
            ],
            f"Probing audio {path.name}",
        )
        data = json.loads(completed.stdout)
        streams = data.get("streams", [])
        value = streams[0].get("duration") if streams else None
        value = value or data.get("format", {}).get("duration")
        if value is None:
            raise MediaError(f"Could not determine audio duration for {path.name}")
        stream = streams[0]
        result = AudioInfo(
            duration=float(value),
            codec=str(stream.get("codec_name", "")),
            sample_rate=int(stream.get("sample_rate", 0)),
            channels=int(stream.get("channels", 0)),
        )
        diagnostic_event(
            "media_audio_probed", path=path, duration=result.duration, codec=result.codec,
            sample_rate=result.sample_rate, channels=result.channels,
        )
        return result

    def decoded_audio_stats(
        self,
        path: Path,
        sample_rate: int = 48000,
        quiet_threshold_dbfs: float = -60.0,
    ) -> DecodedAudioStats:
        command = [
            self.ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-c:a",
            "pcm_s16le",
            "pipe:1",
        ]
        started = self._command_started(command, "Decoding audio statistics")
        try:
            completed = subprocess.run(
                command, capture_output=True, startupinfo=self._startup_info(), check=False,
            )
        except OSError as error:
            diagnostic_exception(
                "media_command_launch_failed", error, command=command,
                duration_seconds=round(time.monotonic() - started[0], 3), command_id=started[1],
            )
            raise
        self._command_finished(completed.returncode, completed.stderr, started)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise MediaError(f"Decoding {path.name} failed: {detail}")
        samples = array("h")
        samples.frombytes(completed.stdout)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            raise MediaError(f"{path.name} decoded to no audio samples")
        threshold = round(32767 * 10 ** (quiet_threshold_dbfs / 20.0))
        first = next((index for index, value in enumerate(samples) if abs(value) >= threshold), None)
        last = next(
            (
                len(samples) - 1 - reverse_index
                for reverse_index, value in enumerate(reversed(samples))
                if abs(value) >= threshold
            ),
            None,
        )
        if first is None or last is None:
            quiet = len(samples) / sample_rate
            return DecodedAudioStats(quiet, quiet, quiet, False)
        return DecodedAudioStats(
            duration=len(samples) / sample_rate,
            leading_quiet=first / sample_rate,
            trailing_quiet=(len(samples) - 1 - last) / sample_rate,
            has_activity=True,
        )

    @diagnostic_operation("media_image_probe")
    def probe_image_dimensions(self, path: Path) -> tuple[int, int]:
        diagnostic_event("media_image_probe_requested", path=path)
        completed = self.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            f"Probing image {path.name}",
        )
        streams = json.loads(completed.stdout).get("streams", [])
        if not streams:
            raise MediaError(f"{path.name} does not contain a decodable image")
        width, height = int(streams[0].get("width", 0)), int(streams[0].get("height", 0))
        diagnostic_event("media_image_probed", path=path, width=width, height=height)
        return width, height

    def waveform_peaks(
        self,
        path: Path,
        duration: float,
        target_peaks: int = 2400,
        sample_rate: int = 2000,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[float]:
        if duration <= 0:
            diagnostic_event("media_waveform_skipped", reason="nonpositive_duration", duration=duration)
            return []
        command = [
            self.ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "pipe:1",
        ]
        started = self._command_started(command, "Extracting waveform")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                startupinfo=self._startup_info(),
            )
        except OSError as error:
            diagnostic_exception(
                "media_command_launch_failed", error, command=command,
                duration_seconds=round(time.monotonic() - started[0], 3), command_id=started[1],
            )
            raise
        diagnostic_event("media_process_started", pid=process.pid, command_id=started[1])
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if not cancelled or not cancelled():
                    continue
                diagnostic_event(
                    "media_process_cancel_requested", pid=process.pid, command_id=started[1],
                )
                process.terminate()
                try:
                    _, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    diagnostic_event(
                        "media_process_kill_requested", pid=process.pid, command_id=started[1],
                    )
                    process.kill()
                    _, stderr = process.communicate()
                self._command_finished(process.returncode, stderr, started, canceled=True)
                return []
        self._command_finished(process.returncode, stderr, started)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            raise MediaError(f"Extracting waveform failed: {detail}")
        samples = array("f")
        samples.frombytes(stdout)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return []
        bucket = max(1, math.ceil(len(samples) / target_peaks))
        peaks = [
            min(1.0, max(abs(value) for value in samples[index : index + bucket]))
            for index in range(0, len(samples), bucket)
        ]
        diagnostic_event("media_waveform_ready", path=path, peak_count=len(peaks), duration=duration)
        return peaks

    def convert_video(
        self,
        source: Path,
        destination: Path,
        height: int,
        fps: int,
        progress: ProgressCallback | None = None,
    ) -> None:
        if progress:
            progress("Converting video to Ogg Theora/Vorbis…")
        destination.parent.mkdir(parents=True, exist_ok=True)
        scale = (
            f"scale=w='min(1920,iw)':h='min({height},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,"
            f"fps={fps}"
        )
        self.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                scale,
                "-c:v",
                "libtheora",
                "-q:v",
                "7",
                "-c:a",
                "libvorbis",
                "-q:a",
                "5",
                str(destination),
            ],
            "Video conversion",
        )

    def convert_audio(
        self,
        source: Path,
        destination: Path,
        mono: bool,
        duration: float | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        duration_filter = (
            ["-af", f"apad=whole_dur={duration:.6f},atrim=duration={duration:.6f}"]
            if duration is not None
            else []
        )
        self.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                *duration_filter,
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                "-ar",
                "48000" if mono else "44100",
                "-ac",
                "1" if mono else "2",
                str(destination),
            ],
            f"Converting {source.name}",
        )

    def create_silent_backing(self, destination: Path, duration: float) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=44100:cl=stereo:d={duration:.6f}",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(destination),
            ],
            "Creating silent backing track",
        )

    def extract_prompt(
        self,
        video: Path,
        start: float,
        end: float,
        head_padding: float,
        tail_padding: float,
        destination: Path,
    ) -> float:
        content_duration = end - start
        if content_duration <= 0:
            raise MediaError("Prompt end must be after its start")
        if not self.has_audio_activity(video, start, end):
            raise MediaError(
                f"The selected source range {start:.3f}–{end:.3f}s contains no audible content"
            )
        seek = max(0.0, start - 0.25)
        offset = start - seek
        read_duration = content_duration + offset + 0.50
        actual_head = min(start, head_padding)
        filter_graph = (
            f"anullsrc=r=48000:cl=mono:d={max(actual_head, 0.000001):.6f}[head];"
            f"[0:a]atrim=start={offset:.6f}:end={offset + content_duration:.6f},"
            "asetpts=N/SR/TB,loudnorm=I=-16:TP=-1.5:LRA=11,"
            "aformat=sample_rates=48000:channel_layouts=mono,asetpts=N/SR/TB,"
            f"apad=whole_dur={content_duration:.6f},atrim=duration={content_duration:.6f},"
            "asetpts=N/SR/TB[voice];"
            f"anullsrc=r=48000:cl=mono:d={max(tail_padding, 0.000001):.6f}[tail];"
            "[head][voice][tail]concat=n=3:v=0:a=1,asetpts=N/SR/TB[out]"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{seek:.6f}",
                "-t",
                f"{read_duration:.6f}",
                "-i",
                str(video),
                "-filter_complex",
                filter_graph,
                "-map",
                "[out]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "1",
                str(destination),
            ],
            f"Extracting prompt at {start:.3f}s",
        )
        return start - actual_head

    def has_audio_activity(
        self,
        source: Path,
        start: float,
        end: float,
        threshold_dbfs: float = -60.0,
    ) -> bool:
        duration = end - start
        if duration <= 0:
            return False
        command = [
            self.ffmpeg,
            "-v",
            "error",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "-c:a",
            "pcm_s16le",
            "pipe:1",
        ]
        started = self._command_started(command, "Checking source audio activity")
        try:
            completed = subprocess.run(
                command, capture_output=True, startupinfo=self._startup_info(), check=False,
            )
        except OSError as error:
            diagnostic_exception(
                "media_command_launch_failed", error, command=command,
                duration_seconds=round(time.monotonic() - started[0], 3), command_id=started[1],
            )
            raise
        self._command_finished(completed.returncode, completed.stderr, started)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise MediaError(f"Checking source audio activity failed: {detail}")
        samples = array("h")
        samples.frombytes(completed.stdout)
        if sys.byteorder != "little":
            samples.byteswap()
        threshold = round(32767 * 10 ** (threshold_dbfs / 20.0))
        return any(abs(value) >= threshold for value in samples)

    def extract_frame(self, video: Path, timestamp: float, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video),
                "-ss",
                f"{max(0.0, timestamp):.6f}",
                "-frames:v",
                "1",
                str(destination),
            ],
            f"Extracting frame at {timestamp:.3f}s",
        )
        if not destination.is_file():
            raise MediaError(f"Extracting frame at {timestamp:.3f}s produced no image")

    def convert_image(
        self,
        source: Path,
        destination: Path,
        width: int,
        height: int,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                str(destination),
            ],
            f"Converting image {source.name}",
        )

    def make_icon(self, source: Path, destination: Path, is_video: bool) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        input_args = ["-ss", "0.500", "-i", str(source)] if is_video else ["-i", str(source)]
        self.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *input_args,
                "-frames:v",
                "1",
                "-vf",
                "scale=660:364:force_original_aspect_ratio=increase,crop=660:364",
                str(destination),
            ],
            "Creating pack icon",
        )

    def decode(self, path: Path) -> None:
        self.run(
            [self.ffmpeg, "-v", "error", "-i", str(path), "-map", "0", "-f", "null", os.devnull],
            f"Decoding {path.name}",
        )
