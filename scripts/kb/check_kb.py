"""Check local KB paths and folder indexes without third-party dependencies."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

SKIP = {
    ".git",
    ".cache",
    ".venv",
    ".build-venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "_internal",
}
CODE = re.compile(r"^\*\*Code:\*\*\s*(.*)")
TOKENS = re.compile(r"`([^`]+)`|\[[^\]]*\]\(([^)]+)\)|([^,\s;]+)")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def skipped(path: Path) -> bool:
    return any(part in SKIP for part in path.parts)


def code_paths(text: str) -> list[tuple[int, str]]:
    paths = []
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        match = CODE.match(line) if fence is None else None
        if match:
            value = re.sub(r"<br\s*/?>", "", match[1], flags=re.IGNORECASE)
            for token in TOKENS.finditer(value):
                paths.append((number, next(group for group in token.groups() if group is not None)))
    return paths


def relative_path(value: str) -> Path:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or ":" in value:
        raise ValueError("use a repository-relative path without '..'")
    return Path(*path.parts)


def discover(root: Path) -> tuple[list[Path], dict[Path, set[Path]]]:
    config = json.loads((root / ".repo-kb.json").read_text(encoding="utf-8"))
    pages = {
        path.relative_to(root)
        for pattern in config["include"]
        for path in root.glob(pattern)
        if path.is_file() and not skipped(path.relative_to(root))
    }
    # Root guides and siblings of explicitly indexed guides are KB pages.
    guide_dirs = {Path("."), Path("docs")}
    guide_dirs.update(
        page.parent for page in pages if page.parent != Path(".") and page.parts[0] != ".github"
    )
    for folder in guide_dirs:
        pages.update(
            path.relative_to(root)
            for path in (root / folder).glob("*.md")
            if not skipped(path.relative_to(root))
        )
    indexes: dict[Path, set[Path]] = {}
    docs = root / "docs"
    if docs.exists():
        guide_dirs.update(
            path.relative_to(root)
            for path in docs.rglob("*")
            if path.is_dir() and not skipped(path.relative_to(root))
        )
    for folder in guide_dirs:
        required = {page for page in pages if page.parent == folder and page.name != "README.md"}
        if folder == Path("."):
            required.update(
                directory / "README.md"
                for directory in guide_dirs
                if directory != folder
                and not any(
                    parent != Path(".") and parent in guide_dirs for parent in directory.parents
                )
                and (root / directory / "README.md").is_file()
            )
        else:
            required.update(
                child.relative_to(root) / "README.md"
                for child in (root / folder).iterdir()
                if child.is_dir()
                and not skipped(child.relative_to(root))
                and (
                    folder.parts[0] == "docs"
                    or any(page.is_relative_to(child.relative_to(root)) for page in pages)
                )
            )
        indexes[folder / "README.md"] = required
    return sorted(pages), indexes


def index_links(root: Path, page: Path, text: str) -> set[Path]:
    targets = set()
    for match in LINK.finditer(text):
        value = unquote(match[1].strip().strip("<>"))
        url = urlsplit(value)
        if url.scheme or url.netloc or not url.path:
            continue
        target = (root / page.parent / url.path.replace("\\", "/")).resolve()
        if target.is_relative_to(root):
            relative = target.relative_to(root)
            targets.add(relative / "README.md" if target.is_dir() else relative)
    return targets


def check(root: Path) -> tuple[list[str], set[Path], int]:
    pages, indexes = discover(root)
    errors = []
    coverage = set()
    for page in pages:
        text = (root / page).read_text(encoding="utf-8")
        for line, value in code_paths(text):
            try:
                path = relative_path(value)
            except ValueError as exc:
                errors.append(f"{page}:{line}: invalid Code path {value!r}: {exc}")
                continue
            if not (root / path).exists():
                errors.append(f"{page}:{line}: Code path does not exist: {value}")
            else:
                coverage.add(path)
    for index, required in sorted(indexes.items()):
        if not (root / index).is_file():
            errors.append(f"{index}: folder index does not exist")
            continue
        links = index_links(root, index, (root / index).read_text(encoding="utf-8"))
        for missing in sorted(required - links):
            errors.append(f"{index}: index does not list {missing}")
    return errors, coverage, len(pages)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def diff_warnings(root: Path, ref: str, coverage: set[Path]) -> list[str]:
    fields = git(root, "diff", "--name-status", "-z", "--find-renames", ref, "--").split("\0")
    changes = []
    position = 0
    while position < len(fields) and fields[position]:
        status, old = fields[position : position + 2]
        position += 2
        new = old
        if status.startswith(("R", "C")):
            new = fields[position]
            position += 1
        changes.append((status, Path(old), Path(new)))
    # Read old Code lines too: removing a reference must not hide a named deletion.
    named = set(coverage)
    old_files = git(root, "ls-tree", "-r", "--name-only", ref).splitlines()
    patterns = json.loads(git(root, "show", f"{ref}:.repo-kb.json"))["include"]
    old_pages = {
        page
        for page in old_files
        if any(
            fnmatch.fnmatchcase(page, pattern)
            or fnmatch.fnmatchcase(page, pattern.replace("**/", ""))
            for pattern in patterns
        )
    }
    guide_dirs = {Path(".")}
    guide_dirs.update(Path(page).parent for page in old_pages if not page.startswith(".github/"))
    for page in old_files:
        if (
            page.endswith(".md")
            and not skipped(Path(page))
            and (page in old_pages or Path(page).parent in guide_dirs)
        ):
            for _, value in code_paths(git(root, "show", f"{ref}:{page}")):
                try:
                    named.add(relative_path(value))
                except ValueError:
                    continue  # Current-page errors are reported by check().
    warnings = []
    for status, old, new in changes:
        if status.startswith(("D", "R")) and not skipped(old):
            for path in sorted(named):
                if (
                    path == old
                    or path.is_relative_to(old)
                    or (old.is_relative_to(path) and not (root / path).exists())
                ):
                    warnings.append(f"{status}: removed or renamed Code path: {path}")
        if (
            new.suffix.lower() != ".md"
            and not skipped(new)
            and not status.startswith("D")
            and not any(new == path or new.is_relative_to(path) for path in coverage)
        ):
            warnings.append(f"{status}: changed file has no Code coverage: {new}")
    return sorted(set(warnings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", metavar="REF", help="warn about changes against a local Git ref")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        errors, coverage, count = check(root)
        warnings = diff_warnings(root, args.diff, coverage) if args.diff else []
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"ERROR: {detail}")
        return 1
    for error in errors:
        print(f"ERROR: {error}")
    for warning in warnings:
        print(f"WARNING: {warning}")
    print(f"KB check: {count} pages, {len(errors)} errors, {len(warnings)} warnings.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
