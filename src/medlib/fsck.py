"""Consistency check and repair for a repository."""
from __future__ import annotations

import filecmp
import os
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional

from .fsutil import META_DIR, hash_file, points_to, set_writable, sibling_temp
from .models import Issue

if TYPE_CHECKING:
    from .library import Library


def fsck(lib: "Library", repair: bool = False, prune: bool = False, verify: bool = False) -> List[Issue]:
    """Check the repository.

    ``repair`` fixes what can be fixed without losing anything (restore a
    missing blob from a leftover copy, finish an interrupted ``add``,
    recreate a missing tracked link, clear stale temp files). ``prune``
    forgets tracked links that are gone or were replaced, and deletes
    dangling symlinks left behind by ``remove``. ``verify`` re-hashes
    every blob."""
    issues: List[Issue] = []
    rows = lib.db.execute("SELECT * FROM files ORDER BY id").fetchall()
    known: Dict[Path, int] = {}
    temps = sorted(p for p in lib.tmp_dir.iterdir()) if lib.tmp_dir.is_dir() else []

    # 1. blobs
    for row in rows:
        fid = row["id"]
        blob = lib.blob_path(fid, row["ext"])
        known[blob] = fid
        if not blob.is_file():
            source = _find_blob_source(lib, row, temps) if repair else None
            if source is not None:
                blob.parent.mkdir(parents=True, exist_ok=True)
                tmp = sibling_temp(blob)
                shutil.copy2(str(source), str(tmp))
                os.replace(str(tmp), str(blob))
                set_writable(blob, False)
                issues.append(
                    Issue("missing-blob", f"file {fid}: blob restored from {source}", True)
                )
            else:
                issues.append(Issue("missing-blob", f"file {fid}: blob {blob} is missing"))
            continue
        size = blob.stat().st_size
        if size != row["size"]:
            issues.append(
                Issue(
                    "corrupt-blob",
                    f"file {fid}: blob is {size} bytes, expected {row['size']}",
                )
            )
        elif verify and hash_file(blob)[0] != row["sha256"]:
            issues.append(Issue("corrupt-blob", f"file {fid}: blob hash mismatch"))

    # 2. blobs without a row
    if lib.files_dir.is_dir():
        for bucket in sorted(lib.files_dir.iterdir()):
            entries = sorted(bucket.iterdir()) if bucket.is_dir() else [bucket]
            for entry in entries:
                if entry not in known:
                    issues.append(
                        Issue("orphan-blob", f"{entry} belongs to no file in the database")
                    )

    # 3. stale temp files
    for tmp in temps:
        if repair:
            tmp.unlink()
        issues.append(Issue("stale-temp", f"leftover temp file {tmp}", repair))

    # 4. tracked links
    tracked = set()
    for link_row in lib.db.execute(
        "SELECT l.file_id, l.path, f.ext, f.size, f.sha256 FROM file_links l"
        " JOIN files f ON f.id = l.file_id"
        " ORDER BY l.file_id, l.path"
    ).fetchall():
        fid, stored = link_row["file_id"], link_row["path"]
        link = lib._load_path(stored)
        blob = lib.blob_path(fid, link_row["ext"])
        tracked.add(link)
        if points_to(link, blob):
            continue
        if not os.path.lexists(str(link)):
            if repair and link.parent.is_dir() and blob.is_file():
                lib._make_link(link, blob)
                issues.append(
                    Issue("missing-link", f"file {fid}: link {link} recreated", True)
                )
            elif prune:
                _forget_link(lib, fid, stored)
                issues.append(
                    Issue("missing-link", f"file {fid}: link {link} is gone; forgotten", True)
                )
            else:
                issues.append(Issue("missing-link", f"file {fid}: link {link} is gone"))
        elif _is_copy(link, blob, link_row["size"], link_row["sha256"]):
            # 'add' was interrupted before the source became a symlink.
            fix = repair and blob.is_file()
            if fix:
                lib._make_link(link, blob)
            issues.append(
                Issue(
                    "unlinked-copy",
                    f"file {fid}: {link} is a copy of the blob, not a link"
                    + ("; replaced with a link" if fix else ""),
                    fix,
                )
            )
        else:
            if prune:
                _forget_link(lib, fid, stored)
            issues.append(
                Issue(
                    "replaced-link",
                    f"file {fid}: {link} no longer links to the blob"
                    + ("; forgotten" if prune else ""),
                    prune,
                )
            )

    # 5. dangling symlinks into the blob store (left behind by 'remove')
    for link in _walk_symlinks(lib):
        if link in tracked:
            continue
        dest = Path(
            os.path.normpath(os.path.join(str(link.parent), os.readlink(str(link))))
        )
        if lib.files_dir in dest.parents and not dest.exists():
            if prune:
                link.unlink()
            issues.append(
                Issue(
                    "dangling-link",
                    f"{link} points to a file that is no longer in the library"
                    + ("; deleted" if prune else ""),
                    prune,
                )
            )

    # 6. tags
    for r in lib.db.execute(
        "SELECT id FROM tags WHERE id NOT IN (SELECT tag_id FROM tag_aliases)"
    ).fetchall():
        issues.append(Issue("tag-no-alias", f"tag {r[0]} has no alias"))

    # 7. schema_version file mirrors the database
    if lib._version_file() != lib.schema_version():
        if repair:
            lib.version_path.write_text(f"{lib.schema_version()}\n")
        issues.append(
            Issue(
                "schema-version",
                f"{lib.version_path} is missing or disagrees with the database"
                + ("; rewritten" if repair else ""),
                repair,
            )
        )

    if repair or prune:
        lib._log(
            "fsck",
            repair=repair,
            prune=prune,
            fixed=[i.message for i in issues if i.fixed],
        )
    return issues


def _find_blob_source(lib: "Library", row: sqlite3.Row, temps: List[Path]) -> Optional[Path]:
    """A regular file with the exact content of a missing blob, if any."""
    candidates = list(temps)
    if row["original_path"]:
        candidates.append(lib._load_path(row["original_path"]))
    for r in lib.db.execute(
        "SELECT path FROM file_links WHERE file_id = ?", (row["id"],)
    ).fetchall():
        candidates.append(lib._load_path(r[0]))
    for c in candidates:
        if c.is_symlink() or not c.is_file() or c.stat().st_size != row["size"]:
            continue
        if hash_file(c)[0] == row["sha256"]:
            return c
    return None


def _is_copy(path: Path, blob: Path, size: int, sha: str) -> bool:
    """``path`` is a regular file with the blob's content (compared with
    the blob itself, or with the recorded hash if the blob is missing)."""
    if path.is_symlink() or not path.is_file():
        return False
    if blob.is_file():
        return filecmp.cmp(str(path), str(blob), shallow=False)
    return path.stat().st_size == size and hash_file(path)[0] == sha


def _forget_link(lib: "Library", file_id: int, stored: str) -> None:
    with lib._tx():
        lib.db.execute(
            "DELETE FROM file_links WHERE file_id = ? AND path = ?", (file_id, stored)
        )


def _walk_symlinks(lib: "Library") -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(str(lib.root)):
        if dirpath == str(lib.root) and META_DIR in dirnames:
            dirnames.remove(META_DIR)
        for name in list(dirnames) + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                yield p
