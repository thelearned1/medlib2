"""Repository upkeep: schema upgrades and database backups.

Backups live at ``backups/v<schema version>/<timestamp>.bak``. They hold the
database only (tags, aliases, links), not the blobs.
"""
from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

from . import probe
from .aliases import is_id
from .migrations import MIGRATIONS, SCHEMA, SCHEMA_VERSION
from .models import Backup, MedlibError, NotFound

if TYPE_CHECKING:
    from .library import Library


def rebuild(
    lib: "Library", progress: Optional[Callable[[int, int], None]] = None
) -> List[str]:
    """Bring the database up to the latest schema, measure any file not yet
    probed, and rebuild the indices. Returns a description of each thing done."""
    actions: List[str] = []
    version = lib.schema_version()
    if version > SCHEMA_VERSION:
        raise MedlibError(
            f"repository schema is version {version}, newer than this medlib "
            f"({SCHEMA_VERSION}); upgrade medlib"
        )
    if version < SCHEMA_VERSION:
        actions.append(f"backed up database to {backup_create(lib).path}")
    for v in range(version, SCHEMA_VERSION):
        if v not in MIGRATIONS:
            raise MedlibError(f"no migration script from schema version {v}")
        with lib._tx():
            for statement in MIGRATIONS[v]:
                lib.db.execute(statement)
            lib.db.execute("UPDATE schema_version SET version = ?", (v + 1,))
        actions.append(f"migrated schema v{v} -> v{v + 1}")

    actions += _ensure_schema(lib)
    actions += probe_pending(lib, progress)
    lib.db.execute("REINDEX")
    lib.db.execute("ANALYZE")
    lib.db.execute("VACUUM")
    actions.append("rebuilt indices and statistics, compacted database")

    if lib._version_file() != SCHEMA_VERSION:
        lib.version_path.write_text(f"{SCHEMA_VERSION}\n")
        actions.append(f"wrote {lib.version_path}")
    lib._log("rebuild", from_version=version, to_version=SCHEMA_VERSION, actions=actions)
    return actions


def probe_pending(
    lib: "Library", progress: Optional[Callable[[int, int], None]] = None
) -> List[str]:
    """Measure duration and dimensions of every file not yet marked probed:
    those added before the columns existed, or while ffprobe was missing."""
    pending = lib.db.execute(
        "SELECT id, ext, media_type FROM files WHERE probed = 0 ORDER BY id"
    ).fetchall()
    media = [r for r in pending if probe.wants_probe(r["media_type"])]
    other = [(r["id"],) for r in pending if not probe.wants_probe(r["media_type"])]
    if other:  # nothing to measure
        with lib._tx():
            lib.db.executemany("UPDATE files SET probed = 1 WHERE id = ?", other)
    if not media:
        return []
    if probe.ffprobe_path() is None:
        return [
            f"ffprobe not found: {len(media)} file(s) left without duration and "
            "dimensions (install ffmpeg, then run this again)"
        ]

    def measure(row):
        blob = lib.blob_path(row["id"], row["ext"])
        return row["id"], probe.probe(blob, row["media_type"]) if blob.is_file() else None

    measured = left = 0
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        for n, (file_id, found) in enumerate(pool.map(measure, media), 1):
            if found is None:  # blob missing (see fsck) or ffprobe gave up
                left += 1
            else:
                lib.db.execute(
                    "UPDATE files SET duration = ?, width = ?, height = ?, probed = 1"
                    " WHERE id = ?",
                    (found.duration, found.width, found.height, file_id),
                )
                measured += 1
            if progress is not None:
                progress(n, len(media))
    actions = [f"measured duration and dimensions of {measured} file(s)"]
    if left:
        actions.append(f"could not measure {left} file(s); see 'medlib fsck'")
    return actions


def _ensure_schema(lib: "Library") -> List[str]:
    """Safety net behind the migration scripts: compare against a pristine
    database and add any table, column or index that is missing. Added
    columns carry only type, default and NOT NULL; anything richer needs a
    real migration."""
    actions: List[str] = []
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA)
        wanted = ref.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"  # tables first
        ).fetchall()
        have = {r[0] for r in lib.db.execute("SELECT name FROM sqlite_master")}
        with lib._tx():
            for kind, name, sql in wanted:
                if name not in have:
                    lib.db.execute(sql)
                    actions.append(f"created {kind} {name}")
                    continue
                if kind != "table":
                    continue
                present = {r[1] for r in lib.db.execute(f'PRAGMA table_info("{name}")')}
                for _, col, ctype, notnull, default, _pk in ref.execute(
                    f'PRAGMA table_info("{name}")'
                ):
                    if col in present:
                        continue
                    if notnull and default is None:
                        raise MedlibError(
                            f"column {name}.{col} is NOT NULL without a default; "
                            "it needs a migration script"
                        )
                    definition = f'"{col}" {ctype}'
                    if default is not None:
                        definition += f" DEFAULT {default}"
                    if notnull:
                        definition += " NOT NULL"
                    lib.db.execute(f'ALTER TABLE "{name}" ADD COLUMN {definition}')
                    actions.append(f"added column {name}.{col}")
    finally:
        ref.close()
    return actions


def backup_create(lib: "Library") -> Backup:
    version = lib.schema_version()
    directory = lib.backups_dir / f"v{version}"
    directory.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    timestamp, n = base, 1
    while (directory / f"{timestamp}.bak").exists():
        n += 1
        timestamp = f"{base}-{n}"
    path = directory / f"{timestamp}.bak"
    dst = sqlite3.connect(str(path))
    try:
        lib.db.backup(dst)
        # A single lib-contained file: no -wal/-shm companions.
        dst.execute("PRAGMA journal_mode = DELETE")
    finally:
        dst.close()
    for suffix in ("-wal", "-shm"):
        companion = directory / (path.name + suffix)
        if companion.exists():
            companion.unlink()
    lib._log("backup create", version=version, timestamp=timestamp)
    return Backup(version, timestamp, path, path.stat().st_size)


def backup_list(lib: "Library", version: Optional[int] = None) -> List[Backup]:
    """Backups, oldest first within each schema version."""
    found = []
    if not lib.backups_dir.is_dir():
        return found
    for directory in lib.backups_dir.iterdir():
        name = directory.name
        if not (directory.is_dir() and name.startswith("v") and is_id(name[1:])):
            continue
        v = int(name[1:])
        if version is not None and v != version:
            continue
        for path in directory.glob("*.bak"):
            found.append(Backup(v, path.name[: -len(".bak")], path, path.stat().st_size))
    found.sort(key=lambda b: (b.version, b.timestamp))
    return found


def backup_remove(lib: "Library", version: int, timestamp: str) -> Path:
    """Delete one backup (and its version directory once empty)."""
    if timestamp.endswith(".bak"):
        timestamp = timestamp[: -len(".bak")]
    if not timestamp or "/" in timestamp or os.sep in timestamp or timestamp.startswith("."):
        raise MedlibError(f"invalid backup timestamp '{timestamp}'")
    directory = lib.backups_dir / f"v{version}"
    path = directory / f"{timestamp}.bak"
    if not path.is_file():
        raise NotFound(f"no backup v{version} {timestamp}")
    path.unlink()
    for suffix in ("-wal", "-shm", "-journal"):  # left if someone opened it
        companion = directory / (path.name + suffix)
        if companion.exists():
            companion.unlink()
    if not any(directory.iterdir()):
        directory.rmdir()
    lib._log("backup remove", version=version, timestamp=timestamp)
    return path
