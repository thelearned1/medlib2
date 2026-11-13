"""Errors and the plain data types returned by the library."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


class MedlibError(Exception):
    """Any user-facing failure."""


class NotFound(MedlibError):
    pass


class AmbiguousFile(MedlibError):
    """A file alias matched more than one file."""

    def __init__(self, ref: str, candidates: List["FileInfo"]):
        ids = ", ".join(str(c.id) for c in candidates)
        super().__init__(f"'{ref}' matches several files (IDs {ids}); use an ID")
        self.ref = ref
        self.candidates = candidates


@dataclass
class FileInfo:
    id: int
    sha256: str
    size: int
    media_type: str
    ext: str
    original_name: str
    original_path: Optional[str]
    added_at: str
    aliases: List[str] = field(default_factory=list)
    tags: List[Tuple[int, str]] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    duration: Optional[float] = None  # seconds; None for stills or unknown
    width: Optional[int] = None  # pixels as displayed; None for audio or unknown
    height: Optional[int] = None

    @property
    def name(self) -> str:
        return self.aliases[0] if self.aliases else self.original_name


@dataclass
class TagInfo:
    id: int
    parent_ids: List[int]
    aliases: List[str]
    file_count: int = 0

    @property
    def name(self) -> str:
        return self.aliases[0] if self.aliases else f"#{self.id}"


@dataclass
class AddResult:
    file_id: int
    duplicate: bool
    # Alias that was added (new file) or that could be added (duplicate).
    alias: Optional[str]
    # For duplicates: the existing file already carries ``alias``.
    alias_present: bool = False
    linked: bool = False
    # False when a media file could not be measured (no ffprobe); 'administer
    # rebuild' picks it up later.
    probed: bool = True


@dataclass
class RemoveResult:
    file_id: int
    path: Path
    dangling: List[str] = field(default_factory=list)


@dataclass
class SimilarTags:
    """Two unrelated tags that mark much the same files (merge candidates).
    Counts include files tagged with descendants."""

    a: TagInfo
    b: TagInfo
    count_a: int
    count_b: int
    shared: int
    jaccard: float
    score: float


@dataclass
class Backup:
    version: int
    timestamp: str
    path: Path
    size: int


@dataclass
class Issue:
    kind: str
    message: str
    fixed: bool = False
