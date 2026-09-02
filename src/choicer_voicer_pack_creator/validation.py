from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.media import MediaTools


class PackValidationError(RuntimeError):
    pass


class PackValidator:
    def __init__(self, media: MediaTools) -> None:
        self.media = media

    def validate_folder(self, folder: Path, expected_clips: int | None = None) -> dict[str, Any]:
        root = folder.resolve()
        required = [
            root / "_pack_info.ini",
            root / "icon.png",
            root / "dub_video.ogv",
            root / "_backing_track.mp3",
        ]
        missing = [path.name for path in required if not path.is_file()]
        if missing:
            raise PackValidationError(f"Missing required pack files: {', '.join(missing)}")

        pack_data = read_config(root / "_pack_info.ini").get("data", {})
        self._require_canonical_crlf(root / "_pack_info.ini")
        if not str(pack_data.get("title", "")).strip():
            raise PackValidationError("_pack_info.ini has no title")
        icon_name = str(pack_data.get("icon", ""))
        if icon_name != "icon.png":
            raise PackValidationError("_pack_info.ini must reference icon.png")

        video = self.media.probe(root / "dub_video.ogv")
        if video.video_codec != "theora" or video.audio_codec != "vorbis":
            raise PackValidationError(
                f"dub_video.ogv must contain Theora + Vorbis, found "
                f"{video.video_codec or 'none'} + {video.audio_codec or 'none'}"
            )
        if (
            video.width <= 0
            or video.height <= 0
            or video.width % 2
            or video.height % 2
            or not 1 <= video.fps <= 120
            or video.pixel_format != "yuv420p"
            or video.audio_sample_rate not in {44100, 48000}
            or video.audio_channels not in {1, 2}
        ):
            raise PackValidationError(
                "dub_video.ogv must use positive even dimensions, 1–120 fps, yuv420p video, "
                "and one- or two-channel 44.1/48 kHz Vorbis audio"
            )
        self.media.decode(root / "dub_video.ogv")
        if (root / "icon.png").read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise PackValidationError("icon.png does not have a valid PNG signature")
        self.media.decode(root / "icon.png")
        if self.media.probe_image_dimensions(root / "icon.png") != (660, 364):
            raise PackValidationError("icon.png must be 660×364 pixels")

        metadata_files = sorted(root.glob("*.txt"))
        if expected_clips is not None and len(metadata_files) != expected_clips:
            raise PackValidationError(
                f"Expected {expected_clips} clip metadata files, found {len(metadata_files)}"
            )
        expected_names = {
            "_pack_info.ini",
            "icon.png",
            "dub_video.ogv",
            "_backing_track.mp3",
        }
        backing_info = self.media.probe_audio(root / "_backing_track.mp3")
        if (
            backing_info.codec != "mp3"
            or backing_info.sample_rate != 44100
            or backing_info.channels != 2
        ):
            raise PackValidationError("_backing_track.mp3 must be 44.1 kHz stereo MP3 audio")
        if abs(backing_info.duration - video.duration) > 0.25:
            raise PackValidationError("_backing_track.mp3 duration does not match the video")
        self.media.decode(root / "_backing_track.mp3")

        timestamps: list[float] = []
        for metadata_path in metadata_files:
            self._require_canonical_crlf(metadata_path)
            data = read_config(metadata_path).get("data", {})
            caption = data.get("caption")
            characters = data.get("dub_characters")
            clip_timestamps = data.get("dub_timestamps")
            image_name = data.get("image")
            if not isinstance(caption, str) or not caption.strip():
                raise PackValidationError(f"{metadata_path.name} has no caption")
            if not isinstance(characters, list) or not characters or not all(
                isinstance(item, str) and item.strip() for item in characters
            ):
                raise PackValidationError(f"{metadata_path.name} has invalid dub_characters")
            if not isinstance(clip_timestamps, list) or not clip_timestamps:
                raise PackValidationError(f"{metadata_path.name} has invalid dub_timestamps")
            try:
                numeric_timestamps = [float(item) for item in clip_timestamps]
            except (TypeError, ValueError) as error:
                raise PackValidationError(
                    f"{metadata_path.name} contains a non-numeric timestamp"
                ) from error
            if any(item < 0 or item > video.duration + 0.25 for item in numeric_timestamps):
                raise PackValidationError(f"{metadata_path.name} has a timestamp outside the video")
            timestamps.extend(numeric_timestamps)
            if not isinstance(image_name, str) or Path(image_name).name != image_name:
                raise PackValidationError(f"{metadata_path.name} has an unsafe image reference")

            audio_path = metadata_path.with_suffix(".mp3")
            image_path = root / image_name
            if not audio_path.is_file() or not image_path.is_file():
                raise PackValidationError(f"{metadata_path.stem} is missing its MP3 or PNG")
            if image_path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                raise PackValidationError(f"{image_path.name} does not have a valid PNG signature")
            audio_info = self.media.probe_audio(audio_path)
            if (
                audio_info.codec != "mp3"
                or audio_info.sample_rate != 48000
                or audio_info.channels != 1
            ):
                raise PackValidationError(
                    f"{audio_path.name} must be 48 kHz mono MP3 audio"
                )
            if any(
                timestamp + audio_info.duration > video.duration + 0.25
                for timestamp in numeric_timestamps
            ):
                raise PackValidationError(
                    f"{audio_path.name} extends beyond the end of dub_video.ogv"
                )
            if not self.media.decoded_audio_stats(audio_path).has_activity:
                raise PackValidationError(f"{audio_path.name} contains no audible prompt content")
            self.media.decode(audio_path)
            self.media.decode(image_path)
            if self.media.probe_image_dimensions(image_path) != (video.width, video.height):
                raise PackValidationError(
                    f"{image_path.name} dimensions must match dub_video.ogv"
                )
            expected_names.update({metadata_path.name, audio_path.name, image_path.name})

        actual_names = {path.name for path in root.iterdir() if path.is_file()}
        if actual_names != expected_names:
            missing_names = sorted(expected_names - actual_names)
            extra_names = sorted(actual_names - expected_names)
            raise PackValidationError(
                f"Pack inventory mismatch; missing={missing_names}, extra={extra_names}"
            )
        return {
            "status": "passed",
            "title": pack_data["title"],
            "clip_count": len(metadata_files),
            "file_count": len(actual_names),
            "video": {
                "duration": video.duration,
                "width": video.width,
                "height": video.height,
                "fps": video.fps,
                "video_codec": video.video_codec,
                "audio_codec": video.audio_codec,
            },
            "first_timestamp": min(timestamps) if timestamps else None,
            "last_timestamp": max(timestamps) if timestamps else None,
        }

    @staticmethod
    def _require_canonical_crlf(path: Path) -> None:
        raw = path.read_bytes()
        without_crlf = raw.replace(b"\r\n", b"")
        if b"\r" in without_crlf or b"\n" in without_crlf:
            raise PackValidationError(f"{path.name} does not use canonical CRLF line endings")

    @staticmethod
    def validate_zip(path: Path, folder_name: str, expected_files: set[str]) -> None:
        with zipfile.ZipFile(path) as archive:
            bad_file = archive.testzip()
            if bad_file:
                raise PackValidationError(f"ZIP CRC failed for {bad_file}")
            prefix = folder_name.rstrip("/") + "/"
            members = {
                name[len(prefix) :]
                for name in archive.namelist()
                if name.startswith(prefix) and not name.endswith("/")
            }
            if members != expected_files:
                raise PackValidationError("ZIP inventory differs from the validated pack folder")
            unexpected = [
                name for name in archive.namelist() if not name.startswith(prefix) and not name.endswith("/")
            ]
            if unexpected:
                raise PackValidationError(f"ZIP has files outside its pack folder: {unexpected}")

    @staticmethod
    def report_json(report: dict[str, Any]) -> str:
        return json.dumps(report, indent=2, ensure_ascii=False) + "\n"
