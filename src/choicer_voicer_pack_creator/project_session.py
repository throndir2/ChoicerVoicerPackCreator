from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from choicer_voicer_pack_creator.models import PackProject


def canonical_project_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


@dataclass
class ProjectSession:
    """A document identity, independent of its tab and of any running operation."""

    project: PackProject
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    path: Path | None = None
    revision: int = 0
    saved_revision: int = 0
    source_revision: int = 0
    backing_revision: int = 0
    draft_revision: int = 0
    hidden: bool = False
    attention: str = ""

    @property
    def dirty(self) -> bool:
        return self.revision != self.saved_revision

    def snapshot(self) -> PackProject:
        return PackProject.from_dict(self.project.to_dict())

    def source_token(self) -> tuple[str, int, str]:
        return self.id, self.source_revision, self.project.video_path
