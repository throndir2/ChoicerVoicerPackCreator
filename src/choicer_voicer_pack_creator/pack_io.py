from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.media import MediaError, MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment

_DIGITS = re.compile(r"(\d+)")


def _natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in _DIGITS.split(path.name)]


def _as_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_float_list(value: object) -> list[float]:
    if isinstance(value, list):
        return [float(item) for item in value]
    if isinstance(value, (int, float)):
        return [float(value)]
    return []


def _safe_pack_reference(root: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{label} uses an absolute path: {value}")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} escapes the selected pack folder: {value}") from error
    return resolved


@dataclass(slots=True)
class ImportResult:
    project: PackProject
    warnings: list[str]


class PackImporter:
    def __init__(self, media: MediaTools) -> None:
        self.media = media

    def import_folder(self, folder: Path) -> ImportResult:
        root = folder.resolve()
        if not root.is_dir():
            raise ValueError(f"Pack folder does not exist: {root}")
        warnings: list[str] = []
        pack_info_path = root / "_pack_info.ini"
        if not pack_info_path.is_file():
            raise ValueError("The selected folder does not contain _pack_info.ini")
        recognized_files = {pack_info_path.resolve()}
        pack_config = read_config(pack_info_path)
        pack_data = pack_config.get("data", {})
        unknown_pack_sections = sorted(set(pack_config) - {"data"})
        unknown_pack_keys = sorted(set(pack_data) - {"title", "icon", "authors", "readme"})
        if unknown_pack_sections:
            warnings.append(
                "Unsupported pack metadata sections will not be copied into a canonical export: "
                + ", ".join(unknown_pack_sections)
            )
        if unknown_pack_keys:
            warnings.append(
                "Unsupported pack metadata keys will not be copied into a canonical export: "
                + ", ".join(unknown_pack_keys)
            )
        title = str(pack_data.get("title") or root.name)
        authors = _as_string_list(pack_data.get("authors"))
        if not authors:
            authors = ["Unknown author"]
            warnings.append("The pack did not declare an author; using 'Unknown author'.")
        readme = str(pack_data.get("readme") or "")

        video = next(
            (root / name for name in ("dub_video.ogv", "dub_video.mp4") if (root / name).is_file()),
            None,
        )
        duration = 0.0
        video_height = 720
        video_fps = 30
        preserve_source_video = False
        if video:
            recognized_files.add(video.resolve())
            try:
                video_info = self.media.probe(video)
                duration = video_info.duration
                video_height = video_info.height or 720
                video_fps = max(1, round(video_info.fps or 30))
                preserve_source_video = (
                    video.suffix.casefold() == ".ogv"
                    and video_info.video_codec == "theora"
                    and video_info.audio_codec == "vorbis"
                    and video_info.pixel_format == "yuv420p"
                    and video_info.audio_sample_rate in {44100, 48000}
                    and video_info.audio_channels in {1, 2}
                    and 1 <= video_info.fps <= 120
                )
            except MediaError as error:
                warnings.append(str(error))
        else:
            warnings.append("No dub_video.ogv or dub_video.mp4 was found.")

        backing = next(
            (
                root / name
                for name in ("_backing_track.mp3", "_backing_track.wav", "backing_track.wav")
                if (root / name).is_file()
            ),
            None,
        )
        if backing:
            recognized_files.add(backing.resolve())
        icon_name = str(pack_data.get("icon") or "icon.png")
        try:
            icon = _safe_pack_reference(root, icon_name, "Pack icon")
        except ValueError as error:
            warnings.append(str(error))
            icon = root / "icon.png"
        if not icon.is_file():
            icon = root / "icon.png"
            if not icon.is_file():
                warnings.append("The pack icon is missing; a new icon will be generated on export.")
        if icon.is_file():
            recognized_files.add(icon.resolve())

        candidates = sorted(
            [
                path
                for path in (*root.glob("*.txt"), *root.glob("*.ini"))
                if path.name.casefold() != "_pack_info.ini"
            ],
            key=_natural_key,
        )
        segments: list[Segment] = []
        for metadata_path in candidates:
            try:
                config = read_config(metadata_path)
                data = config.get("data", {})
            except (OSError, ValueError) as error:
                warnings.append(f"Skipped {metadata_path.name}: {error}")
                continue
            timestamps = _as_float_list(data.get("dub_timestamps"))
            if not timestamps:
                continue
            recognized_files.add(metadata_path.resolve())
            unknown_sections = sorted(set(config) - {"data"})
            unknown_keys = sorted(
                set(data) - {"caption", "image", "dub_timestamps", "dub_characters"}
            )
            if unknown_sections:
                warnings.append(
                    f"{metadata_path.name} has unsupported sections that a canonical export will "
                    f"omit: {', '.join(unknown_sections)}"
                )
            if unknown_keys:
                warnings.append(
                    f"{metadata_path.name} has unsupported keys that a canonical export will "
                    f"omit: {', '.join(unknown_keys)}"
                )
            characters = _as_string_list(data.get("dub_characters"))
            caption = str(data.get("caption") or "").strip()
            if not characters:
                warnings.append(f"{metadata_path.name} has no dub_characters value.")
            if not caption:
                warnings.append(f"{metadata_path.name} has no caption value.")

            audio = next(
                (
                    metadata_path.with_suffix(suffix)
                    for suffix in (".mp3", ".wav", ".ogg")
                    if metadata_path.with_suffix(suffix).is_file()
                ),
                None,
            )
            audio_duration = 3.0
            if audio:
                recognized_files.add(audio.resolve())
                try:
                    audio_duration = self.media.probe_audio_duration(audio)
                except MediaError as error:
                    warnings.append(str(error))
            else:
                warnings.append(f"No prompt audio found for {metadata_path.name}.")

            image_value = str(data.get("image") or f"{metadata_path.stem}.png")
            try:
                image = _safe_pack_reference(root, image_value, f"{metadata_path.name} image")
            except ValueError as error:
                warnings.append(str(error))
                image = metadata_path.with_suffix(".png")
            if not image.is_file():
                image = metadata_path.with_suffix(".png")
                if not image.is_file():
                    warnings.append(f"No prompt image found for {metadata_path.name}.")
            if image.is_file():
                recognized_files.add(image.resolve())

            for timestamp in timestamps:
                segment_end = timestamp + audio_duration
                if duration > 0:
                    segment_end = min(duration, segment_end)
                if segment_end <= timestamp:
                    segment_end = timestamp + 0.1
                segments.append(
                    Segment(
                        start=timestamp,
                        end=segment_end,
                        caption=caption,
                        characters=list(characters),
                        audio_mode="file" if audio else "video",
                        audio_path=str(audio) if audio else "",
                        image_path=str(image) if image.is_file() else "",
                        source_range_known=False,
                    )
                )
            if len(timestamps) > 1:
                warnings.append(
                    f"{metadata_path.name} reused one recording at {len(timestamps)} timestamps; "
                    "it was expanded into independent editable segments."
                )

        if not segments:
            raise ValueError("No clip metadata with dub_timestamps was found in the selected folder")

        unrecognized_files = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.resolve() not in recognized_files
        )
        if unrecognized_files:
            preview = ", ".join(unrecognized_files[:8])
            suffix = f" (+{len(unrecognized_files) - 8} more)" if len(unrecognized_files) > 8 else ""
            warnings.append(
                "The source pack contains files outside the canonical pack inventory. They remain "
                f"safe in the source folder but will not be copied to a converted export: {preview}{suffix}"
            )

        project = PackProject(
            title=title,
            authors=authors,
            readme=readme,
            video_path=str(video) if video else "",
            video_duration=duration,
            backing_track_path=str(backing) if backing else "",
            icon_path=str(icon) if icon.is_file() else "",
            segments=segments,
            video_height=video_height,
            video_fps=video_fps,
            source_pack_path=str(root),
            preserve_source_video=preserve_source_video,
            import_warnings=list(warnings),
        )
        project.sort_segments()
        return ImportResult(project=project, warnings=warnings)
