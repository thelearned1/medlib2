"""Lookups for shell completion.

Prefix lookups are range scans on the alias_key indexes, so the cost is
bounded by ``limit`` however large the library is.

When every candidate is an alias of the same tag (or file), offering them all
only gets in the way: the shell stops to ask which, although any would do. So
by default such a list collapses to the oldest of those aliases.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

from .aliases import alias_key

if TYPE_CHECKING:
    from .library import Library


def _prefix_range(prefix: str) -> Tuple[str, str]:
    lo = alias_key(prefix)
    return lo, lo + "\U0010ffff"


def _oldest_if_same(rows: list, collapse: bool) -> list:
    """``rows`` are ``(alias, owner_id, alias_row_id)``. If they all belong
    to one owner, keep only the alias created first."""
    if collapse and len(rows) > 1 and len({r[1] for r in rows}) == 1:
        return [min(rows, key=lambda r: r[2])]
    return rows


def complete_tags(
    lib: "Library",
    prefix: str = "",
    limit: int = 50,
    file_id: Optional[int] = None,
    collapse: bool = True,
) -> List[Tuple[str, int]]:
    """Up to ``limit`` ``(alias, tag_id)`` pairs whose alias starts with
    ``prefix``; restricted to the tags on ``file_id`` if given. If all of
    them name one tag, only its oldest matching alias (unless ``collapse``
    is off, for commands where the alias itself is what matters)."""
    lo, hi = _prefix_range(prefix)
    sql = "SELECT alias, tag_id, id FROM tag_aliases WHERE alias_key >= ? AND alias_key < ?"
    params: List[object] = [lo, hi]
    if file_id is not None:
        sql += " AND tag_id IN (SELECT tag_id FROM tag_files WHERE file_id = ?)"
        params.append(file_id)
    sql += " ORDER BY alias_key LIMIT ?"
    params.append(limit)
    rows = lib.db.execute(sql, params).fetchall()
    return [(r[0], r[1]) for r in _oldest_if_same(rows, collapse)]


def complete_files(lib: "Library", prefix: str = "", limit: int = 50) -> List[Tuple[str, int, int]]:
    """Up to ``limit`` ``(alias, file_id, n_files)`` rows whose alias
    starts with ``prefix`` (one row per alias; ``file_id`` is the lowest).
    If all of them name one and the same file, only its oldest alias."""
    lo, hi = _prefix_range(prefix)
    rows = lib.db.execute(
        "SELECT MIN(alias), MIN(file_id), COUNT(*), MIN(id) FROM file_aliases"
        " WHERE alias_key >= ? AND alias_key < ?"
        " GROUP BY alias_key ORDER BY alias_key LIMIT ?",
        (lo, hi, limit),
    ).fetchall()
    if all(r[2] == 1 for r in rows):  # no alias shared between files
        keep = {r[2] for r in _oldest_if_same([(r[0], r[1], r[3]) for r in rows], True)}
        rows = [r for r in rows if r[3] in keep]
    return [(r[0], r[1], r[2]) for r in rows]


def complete_file_aliases(lib: "Library", file_id: int, prefix: str = "", limit: int = 50) -> List[str]:
    """Aliases of one file starting with ``prefix``."""
    lo, hi = _prefix_range(prefix)
    rows = lib.db.execute(
        "SELECT alias FROM file_aliases WHERE file_id = ? AND alias_key >= ?"
        " AND alias_key < ? ORDER BY alias_key LIMIT ?",
        (file_id, lo, hi, limit),
    )
    return [r[0] for r in rows]
