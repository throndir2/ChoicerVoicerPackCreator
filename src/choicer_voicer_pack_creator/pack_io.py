from __future__ import annotations

import re
import shutil
import stat
import uuid
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from choicer_voicer_pack_creator.config_format import read_config
from choicer_voicer_pack_creator.diagnostics import (
    DiagnosticProgress,
    diagnostic_event,
    diagnostic_exception,
    diagnostic_operation,
)
from choicer_voicer_pack_creator.media import MediaError, MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.operations import (
    OperationCancelled,
    SourceChangedError,
    SourceSnapshot,
    check_cancelled,
    critical_stage,
    operation_scope,
    path_leases,
    report,
)

_DIGITS = re.compile(r"(\d+)")
_MAX_ZIP_MEMBERS = 10_000
_MAX_ZIP_EXPANDED_BYTES = 8 * 1024**3
_ZIP_BUFFER_SIZE = 1024 * 1024
_WINDOWS_RESERVED_NAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$",
    re.IGNORECASE,
)


def _notify(message: str, fraction: float | None = None) -> None:
    check_cancelled()
    report(message, fraction)
    check_cancelled()


def _zip_member_parts(entry: zipfile.ZipInfo) -> tuple[str, ...]:
    name = entry.orig_filename
    parts = tuple((name[:-1] if entry.is_dir() else name).split("/"))
    if (
        name != entry.filename
        or "\\" in name
        or len(parts) > 64
        or any(
            not part
            or part in {".", ".."}
            or part.endswith((".", " "))
            or any(ord(character) < 32 or character in '<>:"|?*' for character in part)
            or _WINDOWS_RESERVED_NAME.fullmatch(part.split(".", 1)[0].rstrip(" "))
            for part in parts
        )
    ):
        raise ValueError(
            f"Unsafe ZIP entry path: {name!r}. Use relative paths without traversal, "
            "backslashes, Windows-reserved names, or more than 64 nested components."
        )
    return parts


def _zip_inventory(
    archive: zipfile.ZipFile,
) -> tuple[list[tuple[zipfile.ZipInfo, tuple[str, ...]]], tuple[str, ...]]:
    entries = archive.infolist()
    if not entries or len(entries) > _MAX_ZIP_MEMBERS:
        raise ValueError(
            f"The pack ZIP is empty or exceeds the {_MAX_ZIP_MEMBERS:,}-entry import limit."
        )
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    seen_entries: set[tuple[str, ...]] = set()
    nodes: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}
    roots: list[tuple[str, ...]] = []
    total = 0
    for index, entry in enumerate(entries, 1):
        _notify(f"Checking ZIP entry {index}/{len(entries)}", (index - 1) / len(entries))
        parts = _zip_member_parts(entry)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if (
            mode not in {0, stat.S_IFREG, stat.S_IFDIR}
            or entry.external_attr & 0x400
            or (mode == stat.S_IFDIR and not entry.is_dir())
            or (mode == stat.S_IFREG and entry.is_dir())
        ):
            raise ValueError(
                f"The pack ZIP contains a link or special file: {entry.filename!r}. "
                "Only regular files and directories can be imported."
            )
        if entry.flag_bits & (1 | 0x40):
            raise ValueError(
                f"The pack ZIP contains an encrypted entry: {entry.filename!r}. "
                "Create an unencrypted ZIP before importing."
            )
        if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError(
                f"The pack ZIP uses unsupported compression for {entry.filename!r}. "
                "Recreate the ZIP with standard Deflate or Stored compression."
            )
        if entry.file_size < 0 or (entry.is_dir() and entry.file_size):
            raise ValueError(f"The pack ZIP declares an invalid entry size: {entry.filename!r}.")
        total += entry.file_size
        if total > _MAX_ZIP_EXPANDED_BYTES:
            raise ValueError(
                f"The pack ZIP exceeds the {_MAX_ZIP_EXPANDED_BYTES:,}-byte expanded import "
                "limit. Import a smaller pack instead."
            )
        folded = tuple(part.casefold() for part in parts)
        if folded in seen_entries:
            raise ValueError(f"The pack ZIP contains duplicate paths: {entry.filename!r}.")
        seen_entries.add(folded)
        for depth in range(1, len(parts) + 1):
            key = folded[:depth]
            spelling = parts[:depth]
            is_directory = depth < len(parts) or entry.is_dir()
            previous = nodes.get(key)
            if previous is not None and previous != (spelling, is_directory):
                raise ValueError(
                    f"The pack ZIP contains a case-insensitive path collision or file/directory "
                    f"conflict: {entry.filename!r}."
                )
            nodes[key] = (spelling, is_directory)
        if not entry.is_dir() and parts[-1].casefold() == "_pack_info.ini":
            roots.append(parts[:-1])
        members.append((entry, parts))
    if len(roots) != 1:
        raise ValueError(
            "The ZIP must contain exactly one pack root with _pack_info.ini; "
            "multiple packs are ambiguous. Select a single exported pack ZIP."
        )
    root = roots[0]
    if len(root) > 1 or (root and any(parts[:1] != root for _, parts in members)):
        raise ValueError(
            "The ZIP must contain a single top-level pack folder, or the pack files directly "
            "at its root. Recreate the ZIP without extra wrapper folders or sibling files."
        )
    return members, root


def _extract_pack_zip(
    archive: zipfile.ZipFile,
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]],
    destination: Path,
) -> None:
    total = 0
    expected_total = sum(entry.file_size for entry, _ in members)
    for index, (entry, parts) in enumerate(members, 1):
        message = f"Extracting ZIP entry {index}/{len(members)}: {entry.filename}"
        _notify(message, total / max(1, expected_total))
        target = destination.joinpath(*parts)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source:
                if source.read(1):
                    raise ValueError(f"The ZIP directory contains unexpected data: {entry.filename!r}.")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with archive.open(entry) as source, target.open("xb") as output:
            while True:
                check_cancelled()
                chunk = source.read(_ZIP_BUFFER_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                total += len(chunk)
                if total > _MAX_ZIP_EXPANDED_BYTES or written > entry.file_size:
                    raise ValueError(
                        "The pack ZIP expands beyond its declared size or the import size limit. "
                        "Use a smaller, valid pack ZIP."
                    )
                output.write(chunk)
                _notify(message, total / max(1, expected_total))
        if written != entry.file_size:
            raise ValueError(
                f"The pack ZIP has incomplete data for {entry.filename!r}. "
                "Download or create the ZIP again."
            )
    check_cancelled()


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

    @diagnostic_operation("pack_zip_import")
    def import_zip(
        self, archive_path: Path, destination_parent: Path, *,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[str, float | None], None] | None = None,
    ) -> ImportResult:
        """Keep a validated ZIP import in a unique, durable directory owned by this import."""
        with operation_scope(cancelled=cancelled, progress=progress):
            source = archive_path.resolve()
            candidate = destination_parent.resolve() / f"pack-import-{uuid.uuid4().hex}"
            with path_leases(read_paths=[source], write_paths=[candidate]):
                return self._import_zip(source, candidate)

    def _import_zip(self, archive_path: Path, candidate: Path) -> ImportResult:
        source = archive_path.resolve()
        owned_root: Path | None = None
        complete = False
        diagnostic_event("pack_zip_import_requested", path=source)
        try:
            snapshot = SourceSnapshot.capture([source])
            _notify("Inspecting pack ZIP...")
            with zipfile.ZipFile(source) as archive:
                members, pack_parts = _zip_inventory(archive)
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.mkdir()
                owned_root = candidate
                diagnostic_event(
                    "pack_zip_import_staged", path=owned_root, member_count=len(members),
                )
                _extract_pack_zip(archive, members, owned_root)
                result = self.import_folder(owned_root.joinpath(*pack_parts))
            _notify("Verifying source ZIP has not changed...")
            snapshot.verify()
            with critical_stage("Finishing pack ZIP import; cancellation is deferred..."):
                diagnostic_event(
                    "pack_zip_import_ready", path=result.project.source_pack_path,
                    warning_count=len(result.warnings),
                )
                complete = True
                return result
        except (OperationCancelled, SourceChangedError):
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error) as error:
            raise ValueError(
                f"The pack ZIP is damaged or incomplete (CRC/decompression failure): {error}. "
                "Download or create the ZIP again; the original archive was not changed."
            ) from error
        except (NotImplementedError, RuntimeError) as error:
            raise ValueError(
                f"The pack ZIP could not be decompressed: {error}. "
                "Use an unencrypted ZIP with standard Deflate or Stored compression."
            ) from error
        except OSError as error:
            raise ValueError(
                f"Could not read or extract the pack ZIP: {error}. "
                "Check the archive, destination permissions, and available disk space."
            ) from error
        finally:
            if owned_root is not None and not complete:
                shutil.rmtree(owned_root)

    @diagnostic_operation("pack_import")
    def import_folder(
        self, folder: Path, *, cancelled: Callable[[], bool] | None = None,
        progress: Callable[[str, float | None], None] | None = None,
    ) -> ImportResult:
        with operation_scope(cancelled=cancelled, progress=progress):
            root = folder.resolve()
            with path_leases(read_paths=[root]):
                if not root.is_dir():
                    raise ValueError(f"Pack folder does not exist: {root}")
                snapshot = SourceSnapshot.capture([root])
                result = self._import_folder(root)
                check_cancelled()
                snapshot.verify()
                return result

    def _import_folder(self, folder: Path) -> ImportResult:
        root = folder.resolve()
        diagnostic_event("pack_import_requested", path=root)
        if not root.is_dir():
            raise ValueError(f"Pack folder does not exist: {root}")
        warnings: list[str] = []
        pack_info_path = root / "_pack_info.ini"
        if not pack_info_path.is_file():
            raise ValueError("The selected folder does not contain _pack_info.ini")
        _notify("Reading pack metadata...")
        recognized_files = {pack_info_path.resolve()}
        pack_config = read_config(pack_info_path)
        pack_data = pack_config.get("data", {})
        unknown_pack_sections = sorted(set(pack_config) - {"data"})
        unknown_pack_keys = sorted(set(pack_data) - {"title", "icon", "authors", "readme"})
        diagnostic_event(
            "pack_import_metadata_read", unknown_section_count=len(unknown_pack_sections),
            unknown_key_count=len(unknown_pack_keys),
        )
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
            _notify("Inspecting pack video and audio...")
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
                diagnostic_exception("pack_import_video_probe_failed", error, path=video)
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
            diagnostic_exception("pack_import_icon_reference_invalid", error)
            warnings.append(str(error))
            icon = root / "icon.png"
        if not icon.is_file():
            icon = root / "icon.png"
            if not icon.is_file():
                warnings.append("The pack icon is missing; a new icon will be generated on export.")
        if icon.is_file():
            recognized_files.add(icon.resolve())

        _notify("Finding clip metadata...")
        candidates = []
        for pattern in ("*.txt", "*.ini"):
            for path in root.glob(pattern):
                check_cancelled()
                if path.name.casefold() != "_pack_info.ini":
                    candidates.append(path)
        candidates.sort(key=_natural_key)
        diagnostic_event(
            "pack_import_inventory", candidate_count=len(candidates),
            has_video=video is not None, has_backing_track=backing is not None,
        )
        segments: list[Segment] = []
        logged_progress = DiagnosticProgress("pack_import_progress")
        for index, metadata_path in enumerate(candidates, 1):
            _notify(
                f"Reading clip metadata {index}/{len(candidates)}", (index - 1) / len(candidates),
            )
            logged_progress.report(
                f"Reading clip metadata {index}/{len(candidates)}", (index - 1) / len(candidates),
            )
            try:
                config = read_config(metadata_path)
                data = config.get("data", {})
            except (OSError, ValueError) as error:
                diagnostic_exception("pack_import_metadata_skipped", error, path=metadata_path)
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
            caption = str(data.get("caption") or "")
            if not characters:
                warnings.append(f"{metadata_path.name} has no dub_characters value.")
            if not caption.strip():
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
                _notify(
                    f"Inspecting prompt audio {index}/{len(candidates)}",
                    (index - 1) / len(candidates),
                )
                try:
                    audio_duration = self.media.probe_audio_duration(audio)
                except MediaError as error:
                    diagnostic_exception("pack_import_audio_probe_failed", error, path=audio)
                    warnings.append(str(error))
            else:
                warnings.append(f"No prompt audio found for {metadata_path.name}.")

            image_value = str(data.get("image") or f"{metadata_path.stem}.png")
            try:
                image = _safe_pack_reference(root, image_value, f"{metadata_path.name} image")
            except ValueError as error:
                diagnostic_exception("pack_import_image_reference_invalid", error, path=metadata_path)
                warnings.append(str(error))
                image = metadata_path.with_suffix(".png")
            if not image.is_file():
                image = metadata_path.with_suffix(".png")
                if not image.is_file():
                    warnings.append(f"No prompt image found for {metadata_path.name}.")
            if image.is_file():
                recognized_files.add(image.resolve())

            for timestamp in timestamps:
                check_cancelled()
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

        _notify("Checking source pack inventory...")
        unrecognized_files = []
        for path in root.rglob("*"):
            check_cancelled()
            if path.is_file() and path.resolve() not in recognized_files:
                unrecognized_files.append(path.relative_to(root).as_posix())
        unrecognized_files.sort()
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
        _notify("Pack import ready", 1.0)
        logged_progress.report("Pack import ready", 1.0)
        diagnostic_event(
            "pack_import_ready", path=root, segment_count=len(segments), warning_count=len(warnings),
            recognized_file_count=len(recognized_files), unrecognized_file_count=len(unrecognized_files),
            preserve_source_video=preserve_source_video,
        )
        return ImportResult(project=project, warnings=warnings)
