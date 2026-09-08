"""Read-only, bounded discovery of installed packs and recorded takes."""

from __future__ import annotations

import math
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from choicer_voicer_pack_creator.config_format import parse_config_text
from choicer_voicer_pack_creator.operations import SourceSnapshot, check_cancelled, report
from choicer_voicer_pack_creator.pack_io import _WINDOWS_RESERVED_NAME, _safe_pack_reference

_MAX_ENTRIES = 10_000
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_LIBRARY_METADATA_BYTES = 32 * 1024 * 1024
_PREFIX = "_dubrecord_"
_VIDEOS = ("dub_video.ogv", "dub_video.mp4")
_BACKINGS = ("_backing_track.mp3", "_backing_track.wav", "backing_track.wav")


class DiscoveryLimitError(ValueError):
    """Discovery stopped before processing an unbounded or oversized library."""


@dataclass(frozen=True, slots=True)
class GameLocation:
    root: Path
    packs_path: Path
    recordings_path: Path


@dataclass(frozen=True, slots=True)
class PromptInfo:
    metadata_path: Path
    stem: str
    timestamps: tuple[float, ...]
    characters: tuple[str, ...]
    caption: str = ""


@dataclass(frozen=True, slots=True)
class PackInfo:
    path: Path
    title: str
    video_path: Path | None
    backing_path: Path | None
    prompts: tuple[PromptInfo, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    snapshot: SourceSnapshot | None = field(default=None, repr=False)
    inventory: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class TakeInfo:
    path: Path
    pack_name: str
    name: str
    recordings: tuple[Path, ...]
    warnings: tuple[str, ...] = ()
    snapshot: SourceSnapshot | None = field(default=None, repr=False)
    inventory: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class RecordingLibrary:
    packs: tuple[PackInfo, ...]
    takes: tuple[TakeInfo, ...]
    warnings: tuple[str, ...] = ()


@dataclass
class _Budget:
    entries: int = 0
    metadata_bytes: int = 0
    timestamps: int = 0

    def entry(self) -> None:
        check_cancelled()
        self.entries += 1
        if self.entries > _MAX_ENTRIES:
            raise DiscoveryLimitError(f"Recording discovery exceeds the {_MAX_ENTRIES:,}-entry limit.")


def _safe_path(path: Path, *, directory: bool = False) -> Path:
    """Reject links before resolving, including Windows junction/reparse ancestors."""
    path = Path(os.path.abspath(path))
    if any(
        ":" in part or _WINDOWS_RESERVED_NAME.fullmatch(part.split(".", 1)[0].rstrip(" "))
        for part in path.parts[1:]
    ):
        raise ValueError(f"Unsafe filesystem path: {path}")
    for component in (*reversed(path.parents), path):
        check_cancelled()
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Links and reparse points are not allowed: {component}")
    if directory and not path.is_dir():
        raise ValueError(f"Folder does not exist: {path}")
    return path


def _entries(folder: Path, budget: _Budget) -> tuple[Path, ...]:
    _safe_path(folder, directory=True)
    result = []
    with os.scandir(folder) as entries:
        for entry in entries:
            budget.entry()
            result.append(folder / entry.name)
    return tuple(sorted(result, key=lambda path: (path.name.casefold(), path.name)))


def _inventory(folder: Path) -> tuple[str, ...]:
    return tuple(path.name for path in _entries(folder, _Budget()))


def _regular_file(path: Path) -> bool:
    _safe_path(path)
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) and not stat.S_ISDIR(value.st_mode):
        raise ValueError(f"Only regular files and folders are supported: {path}")
    return stat.S_ISREG(value.st_mode)


def _config(path: Path, budget: _Budget) -> dict:
    _safe_path(path)
    if not _regular_file(path):
        raise ValueError(f"Metadata is not a regular file: {path}")
    remaining = _MAX_LIBRARY_METADATA_BYTES - budget.metadata_bytes
    if remaining <= 0:
        raise DiscoveryLimitError("Library metadata exceeds the 32 MiB discovery limit.")
    with path.open("rb") as source:
        content = source.read(min(_MAX_METADATA_BYTES, remaining) + 1)
    budget.metadata_bytes += len(content)
    if len(content) > _MAX_METADATA_BYTES:
        raise ValueError(f"Metadata exceeds the {_MAX_METADATA_BYTES:,}-byte limit: {path.name}")
    if budget.metadata_bytes > _MAX_LIBRARY_METADATA_BYTES:
        raise DiscoveryLimitError("Library metadata exceeds the 32 MiB discovery limit.")
    text = content.decode("utf-8-sig")
    # The shared parser supports Godot values, but duplicate declarations must not
    # silently replace the identity/timing used to match a recording.
    section = ""
    declarations: set[tuple[str, str]] = set()
    for raw in text.splitlines():
        check_cancelled()
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            key = (section, "")
        else:
            key = (section, line.split("=", 1)[0].strip())
        if key in declarations:
            raise ValueError(f"Duplicate metadata declaration in {path.name}: {line}")
        declarations.add(key)
    return parse_config_text(text)


def _reference(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty relative filename.")
    parts = value.replace("\\", "/").split("/")
    if any(
        part in {"", ".", ".."} or part.endswith((".", " "))
        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        or _WINDOWS_RESERVED_NAME.fullmatch(part.split(".", 1)[0].rstrip(" "))
        for part in parts
    ):
        raise ValueError(f"Unsafe {label}: {value!r}")
    # Check the lexical path before the shared reference helper resolves symlinks.
    _safe_path(root.joinpath(*parts))
    return _safe_pack_reference(root, os.path.join(*parts), label)


def _unknown(config: dict, keys: set[str], label: str) -> list[str]:
    warnings = []
    if sections := set(config) - {"data"}:
        warnings.append(f"{label}: unsupported sections: {', '.join(sorted(sections))}.")
    if extra := set(config.get("data", {})) - keys:
        warnings.append(f"{label}: unsupported fields: {', '.join(sorted(extra))}.")
    return warnings


def default_game_location() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidate = Path(appdata) / "YeahMaybe" / "ChoicerVoicer" / "game"
    try:
        return resolve_game_location(candidate).root
    except (OSError, ValueError):
        # Optional discovery only: explicit selection reports the actual error.
        return None


def resolve_game_location(path: Path) -> GameLocation:
    selected = _safe_path(path)
    if selected.is_file():
        if selected.suffix.casefold() != ".exe":
            raise ValueError("Select the game folder, its known parent, or the game executable.")
        selected = selected.parent
    if not selected.is_dir():
        raise ValueError(f"Game location does not exist: {path}")
    candidates = (
        selected, selected / "game", selected / "ChoicerVoicer" / "game",
        selected / "YeahMaybe" / "ChoicerVoicer" / "game",
    )
    matches = []
    for root in candidates:
        check_cancelled()
        if not root.exists():
            continue
        _safe_path(root, directory=True)
        packs = _safe_path(root / "packs_voice")
        recordings = _safe_path(root / "recordings" / "dub_recordings")
        if packs.is_dir() or recordings.is_dir():
            matches.append(GameLocation(root, packs, recordings))
    if len(matches) > 1:
        raise ValueError("Multiple game locations found; select the exact game folder.")
    if not matches:
        raise ValueError(
            "No Choicer Voicer game data found. Select the game folder containing "
            "packs_voice or recordings\\dub_recordings."
        )
    return matches[0]


def read_pack(folder: Path) -> PackInfo:
    return _read_pack(_safe_path(folder, directory=True), _Budget())


def _read_pack(root: Path, budget: _Budget) -> PackInfo:
    paths = _entries(root, budget)
    files = tuple(path for path in paths if _regular_file(path))
    file_set = set(files)
    snapshot = SourceSnapshot.capture(files)
    config = _config(root / "_pack_info.ini", budget)
    data = config.get("data", {})
    title = data.get("title", root.name)
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"Invalid pack title in {root}")
    warnings = _unknown(config, {"title", "icon", "authors", "readme"}, "_pack_info.ini")
    if "icon" in data:
        icon = _reference(root, data["icon"], "pack icon")
        if not icon.is_file():
            warnings.append(f"Missing pack icon: {icon.name}.")
    videos = [path for path in files if path.name.casefold() in _VIDEOS]
    backings = [path for path in files if path.name.casefold() in _BACKINGS]
    if len(videos) > 1 or len(backings) > 1:
        raise ValueError(f"Ambiguous video or backing tracks in {root}; select an unambiguous pack.")
    if not videos:
        warnings.append("No dub_video.ogv or dub_video.mp4 was found; this pack cannot be rendered.")
    prompts = []
    stems: set[str] = set()
    for path in files:
        check_cancelled()
        if path.suffix.casefold() not in {".ini", ".txt"} or path.name.casefold() == "_pack_info.ini":
            continue
        report(f"Reading recording metadata: {path.name}")
        config = _config(path, budget)
        data = config.get("data", {})
        if "dub_timestamps" not in data:
            if path.suffix.casefold() == ".ini":
                raise ValueError(f"Missing dub_timestamps in {path.name}.")
            warnings.append(f"Unrecognized text metadata, not used for recording timing: {path.name}.")
            continue
        timestamps = data["dub_timestamps"]
        if (
            not isinstance(timestamps, list) or not timestamps
            or any(
                isinstance(value, bool) or not isinstance(value, (float, int))
                or value < 0 or value > sys.float_info.max or not math.isfinite(value)
                for value in timestamps
            )
        ):
            raise ValueError(f"Invalid dub_timestamps in {path.name}; expected finite nonnegative times.")
        if len(set(timestamps)) != len(timestamps):
            raise ValueError(f"Duplicate dub_timestamps in {path.name} would play the same voice twice.")
        budget.timestamps += len(timestamps)
        if budget.timestamps > _MAX_ENTRIES:
            raise DiscoveryLimitError(
                f"Recording discovery exceeds the {_MAX_ENTRIES:,}-timestamp limit."
            )
        characters = data.get("dub_characters", [])
        if (
            not isinstance(characters, list)
            or any(not isinstance(value, str) or not value.strip() for value in characters)
        ):
            raise ValueError(f"Invalid dub_characters in {path.name}.")
        caption = data.get("caption", "")
        if not isinstance(caption, str):
            raise ValueError(f"Invalid caption in {path.name}.")
        if "image" in data:
            image = _reference(root, data["image"], f"image in {path.name}")
            if not image.is_file():
                warnings.append(f"{path.name}: missing image {image.name}.")
        if path.stem.casefold() in stems:
            raise ValueError(f"Ambiguous prompt metadata stem: {path.stem}")
        stems.add(path.stem.casefold())
        warnings.extend(_unknown(
            config, {"caption", "image", "dub_timestamps", "dub_characters"}, path.name,
        ))
        prompts.append(PromptInfo(
            path, path.stem, tuple(float(value) for value in timestamps), tuple(characters), caption,
        ))
    if not prompts:
        raise ValueError(f"No prompt metadata with dub_timestamps found in {root}.")
    for path in paths:
        if path not in file_set:
            warnings.append(f"Pack subfolder was not scanned: {path.name}.")
    snapshot.verify()
    if tuple(path.name for path in paths) != _inventory(root):
        raise ValueError("Pack inventory changed during discovery; refresh the library.")
    return PackInfo(
        root, title, videos[0] if videos else None, backings[0] if backings else None,
        tuple(prompts), tuple(warnings), snapshot=snapshot,
        inventory=tuple(path.name for path in paths),
    )


def recording_stem(path: Path) -> str:
    """Only the game's prefix is stripped. Numeric/timestamp-looking text is opaque."""
    if path.suffix.casefold() != ".wav" or not path.stem.startswith(_PREFIX):
        raise ValueError(f"Unrecognized recording filename: {path.name}; expected _dubrecord_*.wav.")
    stem = path.stem.removeprefix(_PREFIX)
    if not stem:
        raise ValueError(f"Recording filename has no prompt identity: {path.name}")
    return stem


def _take(root: Path, files: tuple[Path, ...], paths: tuple[Path, ...]) -> TakeInfo:
    recordings = tuple(
        path for path in files if path.suffix.casefold() == ".wav" or path.name.startswith(_PREFIX)
    )
    recording_set = set(recordings)
    warnings = []
    for path in files:
        if path not in recording_set:
            warnings.append(f"Unrecognized take file, not mixed: {path.name}.")
        else:
            try:
                recording_stem(path)
            except ValueError as error:
                warnings.append(str(error))
    if not recordings:
        warnings.append("This take contains no recording WAV files.")
    if set(paths) - set(files):
        warnings.append("Nested take folders are not mixed into this take.")
    return TakeInfo(
        root, root.parent.name, root.name, recordings, tuple(warnings),
        SourceSnapshot.capture(files), tuple(path.name for path in paths),
    )


def find_takes(folder: Path) -> tuple[TakeInfo, ...]:
    return _find_takes(_safe_path(folder, directory=True), _Budget())


def _find_takes(root: Path, budget: _Budget) -> tuple[TakeInfo, ...]:
    takes = []

    def visit(folder: Path, depth: int) -> None:
        paths = _entries(folder, budget)
        files = tuple(path for path in paths if _regular_file(path))
        file_set = set(files)
        children = tuple(path for path in paths if path not in file_set)
        if files or not children:
            takes.append(_take(folder, files, paths))
        if children and depth >= 2:
            raise ValueError(f"Unexpected nested recording folders below {folder}; select a take.")
        for child in children:
            visit(child, depth + 1)

    visit(root, 0)
    return tuple(takes)


def match_take(take: TakeInfo, packs: tuple[PackInfo, ...]) -> tuple[PackInfo, ...]:
    try:
        stems = [recording_stem(path) for path in take.recordings]
    except ValueError:
        return ()
    if not stems or len(stems) != len(set(stems)):
        return ()
    return tuple(
        pack for pack in packs
        if not pack.errors and set(stems) <= {prompt.stem for prompt in pack.prompts}
    )


def scan_library(location: GameLocation) -> RecordingLibrary:
    resolved = resolve_game_location(location.root)
    if resolved != location:
        raise ValueError("Game location paths do not match its known game layout.")
    budget = _Budget()
    packs = []
    takes = ()
    warnings = []
    if location.packs_path.is_dir():
        for path in _entries(location.packs_path, budget):
            check_cancelled()
            report(f"Discovering pack: {path.name}")
            try:
                if _regular_file(path):
                    warnings.append(f"Unrecognized file in packs_voice: {path.name}.")
                    continue
                packs.append(_read_pack(path, budget))
            except DiscoveryLimitError:
                raise
            except (OSError, ValueError, UnicodeError) as error:
                message = f"Cannot read pack {path.name}: {error}"
                warnings.append(message)
                packs.append(PackInfo(path, path.name, None, None, (), (message,), (message,)))
    else:
        warnings.append(f"Pack directory does not exist: {location.packs_path}")
    if location.recordings_path.is_dir():
        takes = _find_takes(location.recordings_path, budget)
    else:
        warnings.append(f"Recordings directory does not exist: {location.recordings_path}")
    for take in takes:
        matches = match_take(take, tuple(packs))
        if not matches:
            warnings.append(f"{take.pack_name}/{take.name}: no matching pack; select its source pack.")
        elif len(matches) > 1:
            warnings.append(f"{take.pack_name}/{take.name}: ambiguous pack match; select its source pack.")
        warnings.extend(f"{take.name}: {message}" for message in take.warnings)
    return RecordingLibrary(tuple(packs), takes, tuple(warnings))
