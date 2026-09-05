from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from choicer_voicer_pack_creator.config_format import render_clip_metadata, render_pack_info
from choicer_voicer_pack_creator.diagnostics import (
    DiagnosticProgress,
    diagnostic_event,
    diagnostic_exception,
    diagnostic_operation,
)
from choicer_voicer_pack_creator.media import MediaTools
from choicer_voicer_pack_creator.models import PackProject, Segment
from choicer_voicer_pack_creator.validation import PackValidator

ProgressCallback = Callable[[str], None]


def safe_name(value: str, fallback: str = "Dub Pack") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", value).strip().rstrip(".")
    return cleaned or fallback


def slug(value: str, fallback: str = "Voice") -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-")
    return cleaned[:32] or fallback


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_same_or_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


@dataclass(slots=True)
class ExportResult:
    pack_path: Path
    zip_path: Path | None
    validation: dict[str, Any]
    file_hashes: dict[str, str]
    warnings: list[str]


class PackExporter:
    def __init__(self, media: MediaTools) -> None:
        self.media = media
        self.validator = PackValidator(media)

    @diagnostic_operation("pack_export")
    def export(
        self,
        project: PackProject,
        output_parent: Path,
        create_zip: bool = True,
        progress: ProgressCallback | None = None,
    ) -> ExportResult:
        diagnostic_event(
            "pack_export_requested", output_parent=output_parent, create_zip=create_zip,
            source_video=project.video_path, segment_count=len(project.segments),
            preserve_source_video=project.preserve_source_video,
            has_backing_track=bool(project.backing_track_path), has_icon=bool(project.icon_path),
        )
        logged_progress = DiagnosticProgress("pack_export_progress")

        def notify(
            message: str, *, diagnostic_message: str | None = None, fraction: float | None = None,
        ) -> None:
            logged_progress.report(diagnostic_message or message, fraction)
            if progress:
                progress(message)

        notify("Inspecting source video and audio...")
        source_video = Path(project.video_path).resolve()
        source_info = self.media.probe(source_video)
        if not source_info.has_audio:
            raise ValueError("The source video has no audio stream, so prompts cannot be created.")
        notify("Checking project metadata and segment timings...")
        validated_project = PackProject.from_dict(project.to_dict())
        validated_project.video_duration = source_info.duration
        errors = validated_project.validate()
        if errors:
            diagnostic_event("pack_export_project_invalid", error_count=len(errors))
            raise ValueError("Cannot export this project:\n\n" + "\n".join(f"• {item}" for item in errors))

        notify("Checking export destination and protecting source assets...")
        parent = output_parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        folder_name = safe_name(project.title)
        target = parent / folder_name
        target_zip = parent / f"{folder_name}.zip" if create_zip else None
        protected_paths = [source_video]
        for value in (
            project.source_pack_path,
            project.backing_track_path,
            project.icon_path,
        ):
            if value:
                protected_paths.append(Path(value).resolve())
        for segment in project.segments:
            for value in (segment.audio_path, segment.image_path):
                if value:
                    protected_paths.append(Path(value).resolve())
        endangered = [path for path in protected_paths if is_same_or_within(path, target)]
        if endangered:
            diagnostic_event("pack_export_assets_protected", path_count=len(endangered))
            preview = "\n".join(f"• {path}" for path in endangered[:8])
            raise ValueError(
                "Refusing to replace an output folder that contains source or project assets. "
                "Choose another export directory or change the pack title. Endangered paths:\n"
                + preview
            )

        with tempfile.TemporaryDirectory(prefix=f".{folder_name}.staging-", dir=parent) as temporary:
            temporary_root = Path(temporary)
            stage = temporary_root / folder_name
            stage.mkdir()
            diagnostic_event("pack_export_staged", path=stage, target=target, zip_path=target_zip)
            notify("Writing pack metadata…")
            (stage / "_pack_info.ini").write_bytes(
                render_pack_info(project.title.strip(), "icon.png", project.authors, project.readme)
            )

            output_video = stage / "dub_video.ogv"
            if project.preserve_source_video and source_video.suffix.casefold() == ".ogv" and (
                source_info.video_codec == "theora"
                and source_info.audio_codec == "vorbis"
                and source_info.pixel_format == "yuv420p"
                and source_info.height == project.video_height
                and abs(source_info.fps - project.video_fps) < 0.01
                and source_info.audio_sample_rate in {44100, 48000}
                and source_info.audio_channels in {1, 2}
            ):
                notify("Preserving existing Ogg video…")
                shutil.copy2(source_video, output_video)
                diagnostic_event("pack_export_video_preserved")
            else:
                self.media.convert_video(
                    source_video,
                    output_video,
                    project.video_height,
                    project.video_fps,
                    notify,
                )
            notify("Inspecting exported Ogg video...")
            output_video_info = self.media.probe(output_video)

            notify("Creating pack icon...")
            icon_source = Path(project.icon_path).resolve() if project.icon_path else source_video
            self.media.make_icon(icon_source, stage / "icon.png", is_video=not project.icon_path)

            if project.backing_track_path:
                notify("Preparing backing track…")
                backing_source = Path(project.backing_track_path).resolve()
                backing_info = self.media.probe_audio(backing_source)
                if (
                    backing_info.codec == "mp3"
                    and backing_info.sample_rate == 44100
                    and backing_info.channels == 2
                    and abs(backing_info.duration - output_video_info.duration) <= 0.25
                ):
                    shutil.copy2(backing_source, stage / "_backing_track.mp3")
                    diagnostic_event("pack_export_backing_preserved")
                else:
                    self.media.convert_audio(
                        backing_source,
                        stage / "_backing_track.mp3",
                        mono=False,
                        duration=output_video_info.duration,
                    )
            else:
                notify("Creating silent backing track…")
                self.media.create_silent_backing(
                    stage / "_backing_track.mp3", output_video_info.duration
                )

            segments = sorted(project.segments, key=lambda item: (item.start, item.end))
            total = len(segments)
            for index, segment in enumerate(segments, start=1):
                prompt_status = f"Prompt {index}/{total}"
                notify(
                    f"{prompt_status}: preparing audio for {segment.primary_character}",
                    diagnostic_message=f"{prompt_status}: preparing audio",
                    fraction=(index - 1) / total,
                )
                base = f"{index:03d}_{slug(segment.primary_character)}"
                audio_path = stage / f"{base}.mp3"
                image_path = stage / f"{base}.png"
                timestamp = self._write_audio(project, segment, source_video, audio_path, source_info.duration)
                if segment.audio_mode == "video":
                    notify(f"{prompt_status}: checking audio duration, padding, and audibility...")
                    actual_head = min(segment.start, project.head_padding)
                    actual_tail = min(
                        project.tail_padding,
                        max(0.0, source_info.duration - segment.end),
                    )
                    expected_duration = segment.duration + actual_head + actual_tail
                    stats = self.media.decoded_audio_stats(audio_path)
                    if abs(stats.duration - expected_duration) > 0.026:
                        raise RuntimeError(
                            f"{audio_path.name} lost source audio during encoding: expected "
                            f"{expected_duration:.3f}s, decoded {stats.duration:.3f}s"
                        )
                    if stats.leading_quiet + 0.020 < actual_head:
                        raise RuntimeError(
                            f"{audio_path.name} has insufficient physical head padding"
                        )
                    if stats.trailing_quiet + 0.020 < actual_tail:
                        raise RuntimeError(
                            f"{audio_path.name} has insufficient physical tail padding"
                        )
                    if not stats.has_activity:
                        raise RuntimeError(
                            f"{audio_path.name} contains no audible source content"
                        )
                notify(f"{prompt_status}: preparing still image...")
                self._write_image(
                    segment,
                    source_video,
                    image_path,
                    output_video_info.width,
                    output_video_info.height,
                )
                notify(f"{prompt_status}: writing caption and character metadata...")
                (stage / f"{base}.txt").write_bytes(
                    render_clip_metadata(
                        segment.caption.strip(),
                        image_path.name,
                        timestamp,
                        [name.strip() for name in segment.characters],
                    )
                )
            diagnostic_event("pack_export_prompts_built", segment_count=total)

            notify("Validating staged pack…")
            with diagnostic_operation("pack_export_validation", path=stage, expected_clips=total):
                validation = self.validator.validate_folder(
                    stage, expected_clips=total,
                    progress=lambda message: notify(f"Validating staged pack: {message}"),
                )
            files = sorted(path for path in stage.iterdir() if path.is_file())
            file_hashes = {}
            for index, path in enumerate(files, start=1):
                notify(f"Hashing staged file {index}/{len(files)}...")
                file_hashes[path.name] = sha256(path)

            staged_zip: Path | None = None
            if create_zip:
                notify("Creating and testing ZIP archive…")
                staged_zip = temporary_root / f"{folder_name}.zip"
                with zipfile.ZipFile(staged_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for index, path in enumerate(files, start=1):
                        notify(f"Creating ZIP: compressing file {index}/{len(files)}...")
                        archive.write(path, f"{folder_name}/{path.name}")
                notify("Testing staged ZIP integrity and file inventory...")
                self.validator.validate_zip(staged_zip, folder_name, set(file_hashes))
                diagnostic_event("pack_export_zip_validated", file_count=len(file_hashes))

            notify("Publishing and revalidating pack…")
            validation, publish_warnings = self._publish_verified(
                stage,
                target,
                staged_zip,
                target_zip,
                folder_name,
                file_hashes,
                total,
                progress=notify,
            )
            notify("Cleaning up export staging files...")
            logged_progress.report("Pack export ready", 1.0)
            diagnostic_event(
                "pack_export_ready", path=target, zip_path=target_zip,
                file_count=len(file_hashes), segment_count=total, warning_count=len(publish_warnings),
            )
            return ExportResult(
                pack_path=target,
                zip_path=target_zip,
                validation=validation,
                file_hashes=file_hashes,
                warnings=publish_warnings,
            )

    def _write_audio(
        self,
        project: PackProject,
        segment: Segment,
        source_video: Path,
        destination: Path,
        video_duration: float,
    ) -> float:
        if segment.audio_mode == "file":
            source = Path(segment.audio_path).resolve()
            audio_info = self.media.probe_audio(source)
            if (
                audio_info.codec == "mp3"
                and audio_info.sample_rate == 48000
                and audio_info.channels == 1
            ):
                shutil.copy2(source, destination)
            else:
                self.media.convert_audio(source, destination, mono=True)
            return segment.start
        tail = min(project.tail_padding, max(0.0, video_duration - segment.end))
        return self.media.extract_prompt(
            source_video,
            segment.start,
            segment.end,
            project.head_padding,
            tail,
            destination,
        )

    def _write_image(
        self,
        segment: Segment,
        source_video: Path,
        destination: Path,
        width: int,
        height: int,
    ) -> None:
        if segment.image_path:
            source = Path(segment.image_path).resolve()
            source_dimensions = self.media.probe_image_dimensions(source)
            if source.suffix.casefold() == ".png" and source_dimensions == (width, height):
                shutil.copy2(source, destination)
            else:
                self.media.convert_image(source, destination, width, height)
            return
        extracted = destination.with_name(f".{destination.stem}.source.png")
        try:
            self.media.extract_frame(
                source_video,
                segment.start + segment.duration / 2.0,
                extracted,
            )
            if self.media.probe_image_dimensions(extracted) == (width, height):
                os.replace(extracted, destination)
            else:
                self.media.convert_image(extracted, destination, width, height)
        finally:
            extracted.unlink(missing_ok=True)

    @diagnostic_operation("pack_publish")
    def _publish_verified(
        self,
        stage: Path,
        target: Path,
        staged_zip: Path | None,
        target_zip: Path | None,
        folder_name: str,
        file_hashes: dict[str, str],
        expected_clips: int,
        progress: ProgressCallback | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        diagnostic_event(
            "pack_publish_requested", stage=stage, target=target, zip_path=target_zip,
            file_count=len(file_hashes), expected_clips=expected_clips,
        )

        def notify(message: str) -> None:
            if progress:
                progress(message)

        token = uuid.uuid4().hex
        backup = target.with_name(f".{target.name}.previous-{token}")
        zip_backup = target_zip.with_name(f".{target_zip.name}.previous-{token}") if target_zip else None
        if staged_zip:
            notify("Hashing validated ZIP before publishing...")
        staged_zip_hash = sha256(staged_zip) if staged_zip else None
        pack_backed_up = False
        zip_backed_up = False
        pack_published = False
        zip_published = False
        try:
            notify("Publishing: retaining existing output as rollback backups...")
            if target.exists():
                os.replace(target, backup)
                pack_backed_up = True
                diagnostic_event("pack_publish_backup_created", path=backup)
            if target_zip and target_zip.exists() and zip_backup:
                os.replace(target_zip, zip_backup)
                zip_backed_up = True
                diagnostic_event("pack_publish_zip_backup_created", path=zip_backup)
            notify("Publishing validated pack folder and ZIP...")
            os.replace(stage, target)
            pack_published = True
            if staged_zip and target_zip:
                os.replace(staged_zip, target_zip)
                zip_published = True
            notify("Checking published pack file inventory...")
            actual_names = {path.name for path in target.iterdir() if path.is_file()}
            if actual_names != set(file_hashes):
                raise RuntimeError("Published pack inventory differs from validated staging")
            for index, (filename, expected_hash) in enumerate(file_hashes.items(), start=1):
                notify(f"Verifying published file {index}/{len(file_hashes)}...")
                if sha256(target / filename) != expected_hash:
                    raise RuntimeError(f"Published file differs from validated staging: {filename}")
            validation = self.validator.validate_folder(
                target, expected_clips=expected_clips,
                progress=lambda message: notify(f"Revalidating published pack: {message}"),
            )
            if staged_zip_hash is not None:
                notify("Verifying published ZIP checksum...")
                if target_zip is None or not target_zip.is_file():
                    raise RuntimeError("The validated ZIP was not published")
                if sha256(target_zip) != staged_zip_hash:
                    raise RuntimeError("Published ZIP differs from validated staging")
                notify("Testing published ZIP integrity and file inventory...")
                self.validator.validate_zip(target_zip, folder_name, set(file_hashes))
        except Exception as publish_error:
            diagnostic_exception("pack_publish_validation_failed", publish_error, target=target)
            diagnostic_event(
                "pack_rollback_started", pack_published=pack_published, zip_published=zip_published,
                pack_backed_up=pack_backed_up, zip_backed_up=zip_backed_up,
            )
            notify("Publishing failed: restoring previous output from rollback backups...")
            rollback_errors: list[str] = []
            if pack_published and target.exists():
                try:
                    shutil.rmtree(target)
                except OSError as error:
                    diagnostic_exception("pack_rollback_remove_failed", error, path=target)
                    rollback_errors.append(f"could not remove failed pack: {error}")
            if pack_backed_up and backup.exists():
                try:
                    os.replace(backup, target)
                except OSError as error:
                    diagnostic_exception("pack_rollback_restore_failed", error, path=backup)
                    rollback_errors.append(f"could not restore previous pack: {error}")
            if zip_published and target_zip and target_zip.exists():
                try:
                    target_zip.unlink()
                except OSError as error:
                    diagnostic_exception("pack_rollback_zip_remove_failed", error, path=target_zip)
                    rollback_errors.append(f"could not remove failed ZIP: {error}")
            if target_zip and zip_backed_up and zip_backup and zip_backup.exists():
                try:
                    os.replace(zip_backup, target_zip)
                except OSError as error:
                    diagnostic_exception("pack_rollback_zip_restore_failed", error, path=zip_backup)
                    rollback_errors.append(f"could not restore previous ZIP: {error}")
            if rollback_errors:
                diagnostic_event("pack_rollback_incomplete", error_count=len(rollback_errors))
                raise RuntimeError(
                    f"Publishing failed ({publish_error}); rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from publish_error
            diagnostic_event("pack_rollback_completed", target=target)
            raise

        cleanup_warnings: list[str] = []
        notify("Removing previous export backups...")
        try:
            if backup.exists():
                shutil.rmtree(backup)
        except OSError as error:
            diagnostic_exception("pack_backup_cleanup_failed", error, path=backup)
            cleanup_warnings.append(f"Validated export succeeded, but old pack cleanup failed: {error}")
        try:
            if zip_backup and zip_backup.exists():
                zip_backup.unlink()
        except OSError as error:
            diagnostic_exception("pack_zip_backup_cleanup_failed", error, path=zip_backup)
            cleanup_warnings.append(f"Validated export succeeded, but old ZIP cleanup failed: {error}")
        diagnostic_event(
            "pack_published", target=target, zip_path=target_zip,
            file_count=len(file_hashes), warning_count=len(cleanup_warnings),
        )
        return validation, cleanup_warnings
