"""Core medlib library: the repository, its files and its tags. No prompting
and no printing: the CLI is a thin layer on top of this class.

Maintenance operations (fsck, schema upgrades, backups, tag similarity,
completion lookups) live in their own modules; ``Library`` only forwards to
them."""
from __future__ import annotations

import filecmp
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from . import admin, completion, fsck, probe, querylang, similarity
from .aliases import (
    alias_key,
    is_id,
    normalize_alias,
    validate_file_alias,
    validate_tag_alias,
)
from .fsutil import (
    META_DIR,
    copy_hash,
    norm_path,
    points_to,
    safe_filename,
    set_writable,
    sibling_temp,
)
from .migrations import SCHEMA, SCHEMA_VERSION
from .models import (
    AddResult,
    AmbiguousFile,
    Backup,
    FileInfo,
    Issue,
    MedlibError,
    NotFound,
    RemoveResult,
    SimilarTags,
    TagInfo,
)
from .sniff import sniff

BUCKET = 1000  # blobs per directory under files/

Ref = Union[int, str]


class Library:
    def __init__(self, root: Union[str, Path], check_schema: bool = True):
        self.root = Path(os.path.realpath(str(root)))
        self.meta = self.root / META_DIR
        self.files_dir = self.meta / "files"
        self.tmp_dir = self.meta / "tmp"
        self.log_path = self.meta / "log"
        self.version_path = self.meta / "schema_version"
        self.backups_dir = self.meta / "backups"
        db_path = self.meta / "medlib.db"
        if not db_path.is_file():
            raise MedlibError(f"no medlib repository at {self.root}")
        self.db = sqlite3.connect(str(db_path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.create_function("regexp", 2, querylang.regexp, deterministic=True)
        if check_schema:
            version = self.schema_version()
            if version < SCHEMA_VERSION:
                self.db.close()
                raise MedlibError(
                    f"repository schema is version {version}, this medlib uses "
                    f"{SCHEMA_VERSION}; run 'medlib administer rebuild'"
                )
            if version > SCHEMA_VERSION:
                self.db.close()
                raise MedlibError(
                    f"repository schema is version {version}, newer than this medlib "
                    f"({SCHEMA_VERSION}); upgrade medlib"
                )

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def init(cls, path: Union[str, Path]) -> "Library":
        root = Path(os.path.realpath(str(path)))
        if not root.is_dir():
            raise MedlibError(f"{root} is not a directory")
        meta = root / META_DIR
        if meta.exists():
            raise MedlibError(f"{meta} already exists")
        meta.mkdir()
        (meta / "files").mkdir()
        (meta / "tmp").mkdir()
        db = sqlite3.connect(str(meta / "medlib.db"), isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        finally:
            db.close()
        (meta / "schema_version").write_text(f"{SCHEMA_VERSION}\n")
        lib = cls(root)
        lib._log("init")
        return lib

    @classmethod
    def find(
        cls, start: Union[str, Path, None] = None, check_schema: bool = True
    ) -> "Library":
        """Locate the repository in ``start`` or one of its ancestors."""
        here = Path(os.path.realpath(str(start or os.getcwd())))
        for d in [here, *here.parents]:
            if (d / META_DIR / "medlib.db").is_file():
                return cls(d, check_schema=check_schema)
        raise MedlibError(
            f"not inside a medlib repository (no {META_DIR}/ in {here} or its parents); "
            "run 'medlib init'"
        )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _log(self, op: str, **details) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "op": op,
        }
        entry.update(details)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _store_path(self, path: Path) -> str:
        """Paths inside the repository are stored relative to its root."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _load_path(self, stored: str) -> Path:
        p = Path(stored)
        return p if p.is_absolute() else self.root / p

    def _in_meta(self, path: Path) -> bool:
        return path == self.meta or self.meta in path.parents

    def blob_path(self, file_id: int, ext: str) -> Path:
        name = f"{file_id}.{ext}" if ext else str(file_id)
        return self.files_dir / str(file_id // BUCKET) / name

    def _blob_of(self, file_id: int) -> Path:
        row = self.db.execute("SELECT ext FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise NotFound(f"no file with ID {file_id}")
        return self.blob_path(file_id, row["ext"])

    def _make_link(self, link: Path, blob: Path) -> None:
        """Atomically create or replace ``link`` with a relative symlink."""
        target = os.path.relpath(str(blob), str(link.parent))
        tmp = sibling_temp(link)
        os.symlink(target, str(tmp))
        try:
            os.replace(str(tmp), str(link))
        except BaseException:
            tmp.unlink()
            raise

    # -- reference resolution ---------------------------------------------

    def resolve_file(self, ref: Ref) -> int:
        if isinstance(ref, int) or is_id(ref):
            fid = int(ref)
            if not self.db.execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone():
                raise NotFound(f"no file with ID {fid}")
            return fid
        linked = self._file_from_link(ref)
        if linked is not None:
            return linked
        rows = self.db.execute(
            "SELECT DISTINCT file_id FROM file_aliases WHERE alias_key = ? ORDER BY file_id",
            (alias_key(ref),),
        ).fetchall()
        if not rows:
            raise NotFound(f"no file named '{ref}'")
        if len(rows) > 1:
            raise AmbiguousFile(ref, [self.file_show(r[0]) for r in rows])
        return rows[0][0]

    def _file_from_link(self, ref: str) -> Optional[int]:
        """If ``ref`` is the path of a symlink into this library's store, the
        file it points at. Lets shell globs (``memes/*.png``) name files."""
        try:
            if not os.path.islink(ref):
                return None
            link = norm_path(ref)
            target = Path(os.path.normpath(os.path.join(str(link.parent), os.readlink(str(link)))))
        except (OSError, ValueError):
            return None
        stem = target.name.split(".")[0]
        if self.files_dir not in target.parents or not is_id(stem):
            return None
        row = self.db.execute("SELECT id FROM files WHERE id = ?", (int(stem),)).fetchone()
        return row[0] if row else None

    def find_tag(self, ref: Ref) -> Optional[int]:
        if isinstance(ref, int) or is_id(ref):
            row = self.db.execute("SELECT id FROM tags WHERE id = ?", (int(ref),)).fetchone()
        else:
            row = self.db.execute(
                "SELECT tag_id FROM tag_aliases WHERE alias_key = ?", (alias_key(ref),)
            ).fetchone()
        return row[0] if row else None

    def resolve_tag(self, ref: Ref) -> int:
        tid = self.find_tag(ref)
        if tid is None:
            kind = "ID" if isinstance(ref, int) or is_id(ref) else "alias"
            raise NotFound(f"no tag with {kind} '{ref}'")
        return tid

    # -- files -------------------------------------------------------------

    def add(
        self,
        src: Union[str, Path],
        name: Optional[str] = None,
        keep_local: bool = False,
    ) -> AddResult:
        src = norm_path(src)
        if src.is_symlink():
            raise MedlibError(f"{src} is a symlink; add the file it points to")
        if not src.is_file():
            raise MedlibError(f"{src} is not a regular file")
        if self._in_meta(src):
            raise MedlibError(f"{src} is inside {META_DIR}/")

        if name is not None:
            alias: Optional[str] = validate_file_alias(name)
        else:
            alias = normalize_alias(src.name)
            if is_id(alias):
                alias = None  # an all-digit filename cannot be an alias

        tmp = self.tmp_dir / uuid.uuid4().hex
        self.tmp_dir.mkdir(exist_ok=True)
        try:
            sha, size = copy_hash(src, tmp)
            dup = self._find_duplicate(tmp, sha, size)
            if dup is not None:
                tmp.unlink()
                present = alias is not None and self._has_file_alias(dup, alias)
                return AddResult(dup, True, alias, alias_present=present)

            ext = src.suffix[1:].lower()
            media_type = sniff(tmp, src.name)
            measured: Optional[probe.Measurements] = probe.Measurements()
            if probe.wants_probe(media_type):
                measured = probe.probe(tmp, media_type)
            found = measured or probe.Measurements()
            with self._tx():
                cur = self.db.execute(
                    "INSERT INTO files (sha256, size, media_type, ext, original_name,"
                    " original_path, added_at, duration, width, height, probed)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sha,
                        size,
                        media_type,
                        ext,
                        src.name,
                        self._store_path(src),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        found.duration,
                        found.width,
                        found.height,
                        int(measured is not None),
                    ),
                )
                fid = cur.lastrowid
                if alias is not None:
                    self.db.execute(
                        "INSERT INTO file_aliases (file_id, alias, alias_key) VALUES (?, ?, ?)",
                        (fid, alias, alias_key(alias)),
                    )
                if not keep_local:
                    self.db.execute(
                        "INSERT INTO file_links (file_id, path) VALUES (?, ?)",
                        (fid, self._store_path(src)),
                    )
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise

        # From here on a crash is repaired by fsck (temp file -> blob,
        # identical regular file at a recorded link path -> symlink).
        blob = self.blob_path(fid, ext)
        blob.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(tmp), str(blob))
        set_writable(blob, False)
        if not keep_local:
            self._make_link(src, blob)
        self._log(
            "add",
            id=fid,
            sha256=sha,
            size=size,
            path=self._store_path(src),
            alias=alias,
            keep_local=keep_local,
        )
        return AddResult(fid, False, alias, linked=not keep_local, probed=measured is not None)

    def _find_duplicate(self, candidate: Path, sha: str, size: int) -> Optional[int]:
        """Hash match is only a hint: confirm by size and byte comparison."""
        rows = self.db.execute(
            "SELECT id, ext FROM files WHERE sha256 = ? AND size = ? ORDER BY id",
            (sha, size),
        ).fetchall()
        for row in rows:
            blob = self.blob_path(row["id"], row["ext"])
            if blob.is_file() and filecmp.cmp(str(candidate), str(blob), shallow=False):
                return row["id"]
        return None

    def _has_file_alias(self, file_id: int, alias: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM file_aliases WHERE file_id = ? AND alias_key = ?",
                (file_id, alias_key(alias)),
            ).fetchone()
            is not None
        )

    def remove(
        self,
        ref: Ref,
        to: Union[str, Path, None] = None,
        cwd: Union[str, Path, None] = None,
    ) -> RemoveResult:
        """Move the blob back out of the library and forget the file."""
        fid = self.resolve_file(ref)
        info = self.file_show(fid)
        blob = self.blob_path(fid, info.ext)
        if not blob.is_file():
            raise MedlibError(f"blob for file {fid} is missing ({blob}); run 'medlib fsck'")
        dest = self._remove_dest(info, blob, to, cwd)

        tmp = sibling_temp(dest)
        shutil.move(str(blob), str(tmp))
        set_writable(tmp, True)
        os.replace(str(tmp), str(dest))

        with self._tx():
            self.db.execute("DELETE FROM files WHERE id = ?", (fid,))
        dangling = [
            p
            for p in info.links
            if self._load_path(p) != dest and os.path.lexists(str(self._load_path(p)))
        ]
        self._log(
            "remove",
            id=fid,
            restored_to=str(dest),
            sha256=info.sha256,
            size=info.size,
            media_type=info.media_type,
            original_name=info.original_name,
            original_path=info.original_path,
            added_at=info.added_at,
            aliases=info.aliases,
            tags=[name for _, name in info.tags],
            links=info.links,
        )
        return RemoveResult(fid, dest, dangling)

    def _remove_dest(
        self, info: FileInfo, blob: Path, to: Union[str, Path, None], cwd
    ) -> Path:
        fallback = norm_path(Path(str(cwd or os.getcwd())) / info.original_name)
        if to is not None:
            dest = norm_path(to)
            if dest.is_dir() and not dest.is_symlink():
                dest = dest / info.original_name
        elif info.original_path:
            dest = self._load_path(info.original_path)
            free = not os.path.lexists(str(dest)) and dest.parent.is_dir()
            if not (free or points_to(dest, blob)):
                dest = fallback
        else:
            dest = fallback
        if self._in_meta(dest):
            raise MedlibError(f"cannot restore into {META_DIR}/ ({dest})")
        if not dest.parent.is_dir():
            raise MedlibError(f"directory {dest.parent} does not exist")
        if os.path.lexists(str(dest)) and not points_to(dest, blob):
            raise MedlibError(f"{dest} already exists; choose another place with --to")
        return dest

    def file_show(self, ref: Ref) -> FileInfo:
        fid = ref if isinstance(ref, int) else self.resolve_file(ref)
        row = self.db.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
        if row is None:
            raise NotFound(f"no file with ID {fid}")
        aliases = [
            r[0]
            for r in self.db.execute(
                "SELECT alias FROM file_aliases WHERE file_id = ? ORDER BY id", (fid,)
            )
        ]
        tags = [
            (r["tag_id"], r["alias"])
            for r in self.db.execute(
                "SELECT tf.tag_id, (SELECT alias FROM tag_aliases a WHERE a.tag_id = tf.tag_id"
                " ORDER BY a.id LIMIT 1) AS alias FROM tag_files tf WHERE tf.file_id = ?"
                " ORDER BY alias",
                (fid,),
            )
        ]
        links = [
            r[0]
            for r in self.db.execute(
                "SELECT path FROM file_links WHERE file_id = ? ORDER BY path", (fid,)
            )
        ]
        return FileInfo(
            id=row["id"],
            sha256=row["sha256"],
            size=row["size"],
            media_type=row["media_type"],
            ext=row["ext"],
            original_name=row["original_name"],
            original_path=row["original_path"],
            added_at=row["added_at"],
            aliases=aliases,
            tags=tags,
            links=links,
            duration=row["duration"],
            width=row["width"],
            height=row["height"],
        )

    def file_list(self) -> List[FileInfo]:
        return self.query()

    def file_alias(self, ref: Ref, alias: str) -> bool:
        """Add an alias. Returns False if the file already had it."""
        fid = self.resolve_file(ref)
        alias = validate_file_alias(alias)
        if self._has_file_alias(fid, alias):
            return False
        with self._tx():
            self.db.execute(
                "INSERT INTO file_aliases (file_id, alias, alias_key) VALUES (?, ?, ?)",
                (fid, alias, alias_key(alias)),
            )
        self._log("file alias", id=fid, alias=alias)
        return True

    def file_unalias(self, ref: Ref, alias: str) -> None:
        fid = self.resolve_file(ref)
        with self._tx():
            cur = self.db.execute(
                "DELETE FROM file_aliases WHERE file_id = ? AND alias_key = ?",
                (fid, alias_key(alias)),
            )
        if cur.rowcount == 0:
            raise NotFound(f"file {fid} has no alias '{alias}'")
        self._log("file unalias", id=fid, alias=alias)

    def file_tag(self, ref: Ref, tags: Sequence[Ref]) -> List[int]:
        """Attach tags. Returns the tag IDs that were newly attached."""
        fid = self.resolve_file(ref)
        tids = [self.resolve_tag(t) for t in tags]
        added = []
        with self._tx():
            for tid in tids:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO tag_files (tag_id, file_id) VALUES (?, ?)",
                    (tid, fid),
                )
                if cur.rowcount:
                    added.append(tid)
        if added:
            self._log("file tag", id=fid, tags=added)
        return added

    def file_untag(self, ref: Ref, tags: Sequence[Ref]) -> List[int]:
        """Detach tags (exact tags only, not descendants). Returns removed IDs."""
        fid = self.resolve_file(ref)
        tids = [self.resolve_tag(t) for t in tags]
        removed = []
        with self._tx():
            for tid in tids:
                cur = self.db.execute(
                    "DELETE FROM tag_files WHERE tag_id = ? AND file_id = ?", (tid, fid)
                )
                if cur.rowcount:
                    removed.append(tid)
        if removed:
            self._log("file untag", id=fid, tags=removed)
        return removed

    def file_retag(
        self, old: Ref, new: Ref, files: Sequence[Ref]
    ) -> Tuple[List[int], List[int]]:
        """Swap the tag ``old`` for ``new`` on ``files``. Only a direct
        ``old`` tag counts (not one of its subtags); files without it are
        left alone. Returns ``(retagged_ids, skipped_ids)``."""
        old_id = self.resolve_tag(old)
        new_id = self.resolve_tag(new)
        if old_id == new_id:
            raise MedlibError("the old and new tags are the same tag")
        holders = {
            r[0]
            for r in self.db.execute(
                "SELECT file_id FROM tag_files WHERE tag_id = ?", (old_id,)
            )
        }
        wanted = list(dict.fromkeys(self.resolve_file(f) for f in files))
        retagged = [f for f in wanted if f in holders]
        skipped = [f for f in wanted if f not in holders]
        with self._tx():
            for fid in retagged:
                self.db.execute(
                    "DELETE FROM tag_files WHERE tag_id = ? AND file_id = ?", (old_id, fid)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO tag_files (tag_id, file_id) VALUES (?, ?)",
                    (new_id, fid),
                )
        if retagged:
            self._log("file retag", old=old_id, new=new_id, files=retagged)
        return retagged, skipped

    def file_link(self, ref: Ref, path: Union[str, Path]) -> Path:
        """Create a tracked symlink to the file at ``path`` (or inside it, if
        it is a directory)."""
        fid = self.resolve_file(ref)
        info = self.file_show(fid)
        blob = self.blob_path(fid, info.ext)
        link = norm_path(path)
        if link.is_dir() and not link.is_symlink():
            link = link / safe_filename(info.name, info.ext)
        if self._in_meta(link):
            raise MedlibError(f"cannot link inside {META_DIR}/")
        if os.path.lexists(str(link)):
            raise MedlibError(f"{link} already exists")
        if not link.parent.is_dir():
            raise MedlibError(f"directory {link.parent} does not exist")
        with self._tx():
            self.db.execute(
                "INSERT OR IGNORE INTO file_links (file_id, path) VALUES (?, ?)",
                (fid, self._store_path(link)),
            )
        self._make_link(link, blob)
        self._log("file link", id=fid, path=self._store_path(link))
        return link

    # -- query -------------------------------------------------------------

    def query(self, expression: str = "") -> List[FileInfo]:
        """Files matching a query expression (see querylang.py), by ID. The
        empty expression matches every file.

        ``tag:X`` (or a bare ``X``) matches X and its descendants, ``tag=X``
        only X itself; ``type:`` takes a major type (``image``), an extension
        or subtype (``png``) or a full type (``image/png``); ``alias:`` is a
        case-insensitive regex searched in the file's aliases; ``untagged``
        matches files with no tag; ``duration:``, ``width:`` and ``height:``
        compare measurements (``duration:<10``, ``width:>=1920``,
        ``duration:5..30``). Combine with ``&`` ``|`` ``!`` ``( )``."""
        try:
            tree = querylang.parse(expression)
        except querylang.QuerySyntaxError as e:
            raise MedlibError(f"query: {e}")
        params: List[object] = []
        where = querylang.to_sql(self, tree, params)
        sql = f"SELECT f.id FROM files f WHERE {where} ORDER BY f.id"
        return [self.file_show(r[0]) for r in self.db.execute(sql, params).fetchall()]

    def export_links(self, file_ids: Iterable[int], directory: Union[str, Path]) -> List[Path]:
        """Symlink files into ``directory`` (untracked; for ``query --output``)."""
        out = Path(os.path.realpath(str(directory)))
        if self._in_meta(out):
            raise MedlibError(f"cannot write inside {META_DIR}/")
        out.mkdir(parents=True, exist_ok=True)
        made = []
        for fid in file_ids:
            info = self.file_show(fid)
            blob = self.blob_path(fid, info.ext)
            link = out / safe_filename(info.name, info.ext)
            if os.path.lexists(str(link)) and not points_to(link, blob):
                stem, suffix = os.path.splitext(link.name)
                link = out / f"{stem}-{fid}{suffix}"
                if os.path.lexists(str(link)) and not points_to(link, blob):
                    raise MedlibError(f"{link} already exists")
            self._make_link(link, blob)
            made.append(link)
        return made

    # -- tags --------------------------------------------------------------

    def tag_descendants(self, tag_id: int) -> List[int]:
        """``tag_id`` and all tags below it."""
        rows = self.db.execute(
            "WITH RECURSIVE sub(id) AS (SELECT ? UNION SELECT p.tag_id FROM tag_parents p"
            " JOIN sub ON p.parent_id = sub.id) SELECT id FROM sub",
            (tag_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def _insert_tag_alias(self, tag_id: int, alias: str) -> None:
        key = alias_key(alias)
        owner = self.db.execute(
            "SELECT tag_id FROM tag_aliases WHERE alias_key = ?", (key,)
        ).fetchone()
        if owner is not None:
            raise MedlibError(f"tag alias '{alias}' is already used by tag {owner[0]}")
        self.db.execute(
            "INSERT INTO tag_aliases (tag_id, alias, alias_key) VALUES (?, ?, ?)",
            (tag_id, alias, key),
        )

    def tag_add(self, alias: str, parents: Sequence[Ref] = ()) -> int:
        alias = validate_tag_alias(alias)
        parent_ids = sorted({self.resolve_tag(p) for p in parents})
        with self._tx():
            tid = self.db.execute("INSERT INTO tags DEFAULT VALUES").lastrowid
            self._insert_tag_alias(tid, alias)
            for pid in parent_ids:
                self.db.execute(
                    "INSERT INTO tag_parents (tag_id, parent_id) VALUES (?, ?)", (tid, pid)
                )
        self._log("tag add", id=tid, alias=alias, parents=parent_ids)
        return tid

    def tag_remove(self, ref: Ref, all: bool = False) -> Tuple[int, bool]:
        """With an ID or ``all``: remove the whole tag, moving its children
        up to each of its parents. With a bare alias: drop just that alias,
        unless it is the last one. Files are never touched. Returns
        ``(tag_id, tag_was_removed)``."""
        tid = self.resolve_tag(ref)
        whole = all or isinstance(ref, int) or is_id(ref)
        if not whole:
            count = self.db.execute(
                "SELECT COUNT(*) FROM tag_aliases WHERE tag_id = ?", (tid,)
            ).fetchone()[0]
            if count <= 1:
                raise MedlibError(
                    f"'{ref}' is the last alias of tag {tid}; "
                    "use --all (or the tag ID) to remove the tag itself"
                )
            with self._tx():
                self.db.execute(
                    "DELETE FROM tag_aliases WHERE alias_key = ?", (alias_key(str(ref)),)
                )
            self._log("tag remove", id=tid, alias=str(ref), whole=False)
            return tid, False

        info = self.tag_show(tid)
        with self._tx():
            # every child goes under every parent of the removed tag
            self.db.execute(
                "INSERT OR IGNORE INTO tag_parents (tag_id, parent_id)"
                " SELECT c.tag_id, p.parent_id FROM tag_parents c, tag_parents p"
                " WHERE c.parent_id = ? AND p.tag_id = ?",
                (tid, tid),
            )
            self.db.execute("DELETE FROM tags WHERE id = ?", (tid,))  # edges cascade
        self._log(
            "tag remove", id=tid, aliases=info.aliases, parents=info.parent_ids, whole=True
        )
        return tid, True

    def tag_alias(self, ref: Ref, alias: str) -> None:
        tid = self.resolve_tag(ref)
        alias = validate_tag_alias(alias)
        with self._tx():
            self._insert_tag_alias(tid, alias)
        self._log("tag alias", id=tid, alias=alias)

    def tag_rename(self, ref: Ref, new_alias: str) -> None:
        """Replace one alias. ``ref`` names the alias to replace; an ID is
        accepted only when the tag has a single alias."""
        tid = self.resolve_tag(ref)
        new_alias = validate_tag_alias(new_alias)
        new_key = alias_key(new_alias)
        if isinstance(ref, int) or is_id(ref):
            rows = self.db.execute(
                "SELECT id FROM tag_aliases WHERE tag_id = ?", (tid,)
            ).fetchall()
            if len(rows) != 1:
                raise MedlibError(
                    f"tag {tid} has {len(rows)} aliases; name the alias to rename"
                )
            alias_row = rows[0][0]
        else:
            alias_row = self.db.execute(
                "SELECT id FROM tag_aliases WHERE alias_key = ?", (alias_key(ref),)
            ).fetchone()[0]
        owner = self.db.execute(
            "SELECT id, tag_id FROM tag_aliases WHERE alias_key = ?", (new_key,)
        ).fetchone()
        if owner is not None and owner["id"] != alias_row:
            raise MedlibError(
                f"tag alias '{new_alias}' is already used by tag {owner['tag_id']}"
            )
        with self._tx():
            self.db.execute(
                "UPDATE tag_aliases SET alias = ?, alias_key = ? WHERE id = ?",
                (new_alias, new_key, alias_row),
            )
        self._log("tag rename", id=tid, old=str(ref), new=new_alias)

    def tag_parent(self, ref: Ref, parent: Ref) -> bool:
        """Put a tag under ``parent`` as well as any parents it already has.
        Returns False if it was already there."""
        tid = self.resolve_tag(ref)
        pid = self.resolve_tag(parent)
        if pid in self.tag_descendants(tid):
            raise MedlibError(
                f"cannot make tag {pid} the parent of tag {tid}: that would create a cycle"
            )
        with self._tx():
            cur = self.db.execute(
                "INSERT OR IGNORE INTO tag_parents (tag_id, parent_id) VALUES (?, ?)",
                (tid, pid),
            )
        if cur.rowcount:
            self._log("tag parent", id=tid, parent=pid)
        return bool(cur.rowcount)

    def tag_unparent(self, ref: Ref, parent: Optional[Ref] = None) -> List[int]:
        """Take a tag out from under ``parent``, or from under all its parents
        if none is given. Returns the parent IDs it was removed from."""
        tid = self.resolve_tag(ref)
        current = self.tag_show(tid).parent_ids
        if parent is None:
            removed = current
        else:
            pid = self.resolve_tag(parent)
            if pid not in current:
                raise MedlibError(f"tag {tid} is not directly under tag {pid}")
            removed = [pid]
        with self._tx():
            for pid in removed:
                self.db.execute(
                    "DELETE FROM tag_parents WHERE tag_id = ? AND parent_id = ?", (tid, pid)
                )
        if removed:
            self._log("tag unparent", id=tid, parents=removed)
        return removed

    def tag_merge(self, src: Ref, dst: Ref) -> int:
        """Fold ``src`` into ``dst``: files, aliases and children move to
        ``dst``, ``dst`` also goes under ``src``'s parents, and ``src``
        disappears. Returns the surviving tag ID."""
        sid = self.resolve_tag(src)
        did = self.resolve_tag(dst)
        if sid == did:
            raise MedlibError("cannot merge a tag into itself")
        if did in self.tag_descendants(sid):
            raise MedlibError(
                f"tag {did} is below tag {sid}; merge the other way round or unparent it first"
            )
        with self._tx():
            self.db.execute(
                "INSERT OR IGNORE INTO tag_files (tag_id, file_id)"
                " SELECT ?, file_id FROM tag_files WHERE tag_id = ?",
                (did, sid),
            )
            self.db.execute("UPDATE tag_aliases SET tag_id = ? WHERE tag_id = ?", (did, sid))
            self.db.execute(
                "INSERT OR IGNORE INTO tag_parents (tag_id, parent_id)"
                " SELECT tag_id, ? FROM tag_parents WHERE parent_id = ?",
                (did, sid),
            )
            # dst inherits src's parents, except any that sit at or below dst
            # (which would make a cycle, e.g. when src is itself under dst).
            below = set(self.tag_descendants(did))
            for (pid,) in self.db.execute(
                "SELECT parent_id FROM tag_parents WHERE tag_id = ?", (sid,)
            ).fetchall():
                if pid not in below:
                    self.db.execute(
                        "INSERT OR IGNORE INTO tag_parents (tag_id, parent_id) VALUES (?, ?)",
                        (did, pid),
                    )
            self.db.execute("DELETE FROM tags WHERE id = ?", (sid,))  # edges cascade
        self._log("tag merge", src=sid, dst=did)
        return did

    def tag_show(self, ref: Ref) -> TagInfo:
        tid = ref if isinstance(ref, int) else self.resolve_tag(ref)
        row = self.db.execute("SELECT * FROM tags WHERE id = ?", (tid,)).fetchone()
        if row is None:
            raise NotFound(f"no tag with ID '{tid}'")
        aliases = [
            r[0]
            for r in self.db.execute(
                "SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY id", (tid,)
            )
        ]
        count = self.db.execute(
            "SELECT COUNT(*) FROM tag_files WHERE tag_id = ?", (tid,)
        ).fetchone()[0]
        parents = [
            r[0]
            for r in self.db.execute(
                "SELECT parent_id FROM tag_parents WHERE tag_id = ? ORDER BY parent_id", (tid,)
            )
        ]
        return TagInfo(tid, parents, aliases, count)

    def tag_list(self) -> List[TagInfo]:
        ids = [r[0] for r in self.db.execute("SELECT id FROM tags ORDER BY id")]
        return [self.tag_show(i) for i in ids]

    def tag_file_count(self, ref: Ref) -> int:
        """Distinct files carrying the tag or any descendant."""
        ids = ",".join(str(i) for i in self.tag_descendants(self.resolve_tag(ref)))
        return self.db.execute(
            f"SELECT COUNT(DISTINCT file_id) FROM tag_files WHERE tag_id IN ({ids})"
        ).fetchone()[0]

    # -- schema version ------------------------------------------------------

    def schema_version(self) -> int:
        """The version recorded in the database (the authority; the
        ``schema_version`` file mirrors it for tools that cannot open the DB)."""
        return self.db.execute("SELECT version FROM schema_version").fetchone()[0]

    def _version_file(self) -> Optional[int]:
        try:
            return int(self.version_path.read_text().strip())
        except (OSError, ValueError):
            return None

    # -- maintenance (implemented in admin, similarity, completion, fsck) ----

    def rebuild(self, progress: Optional[Callable[[int, int], None]] = None) -> List[str]:
        """Upgrade to the latest schema, measure unprobed files and rebuild
        indices; see admin.rebuild. ``progress(done, total)`` is called as
        files are measured."""
        return admin.rebuild(self, progress)

    def backup_create(self) -> Backup:
        return admin.backup_create(self)

    def backup_list(self, version: Optional[int] = None) -> List[Backup]:
        return admin.backup_list(self, version)

    def backup_remove(self, version: int, timestamp: str) -> Path:
        return admin.backup_remove(self, version, timestamp)

    def similar_tags(
        self, limit: int = 20, min_score: float = 0.0, min_shared: int = 1
    ) -> List[SimilarTags]:
        """Merge candidates, best first; see similarity.similar_tags."""
        return similarity.similar_tags(self, limit, min_score, min_shared)

    def complete_tags(
        self,
        prefix: str = "",
        limit: int = 50,
        file_id: Optional[int] = None,
        collapse: bool = True,
    ) -> List[Tuple[str, int]]:
        return completion.complete_tags(self, prefix, limit, file_id, collapse)

    def complete_files(self, prefix: str = "", limit: int = 50) -> List[Tuple[str, int, int]]:
        return completion.complete_files(self, prefix, limit)

    def complete_file_aliases(self, file_id: int, prefix: str = "", limit: int = 50) -> List[str]:
        return completion.complete_file_aliases(self, file_id, prefix, limit)

    def fsck(self, repair: bool = False, prune: bool = False, verify: bool = False) -> List[Issue]:
        """Check (and optionally repair) the repository; see fsck.fsck."""
        return fsck.fsck(self, repair, prune, verify)
