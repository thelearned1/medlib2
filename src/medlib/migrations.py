"""The database schema and its migrations.

Every change to ``SCHEMA`` needs, in this same file:

1. ``SCHEMA_VERSION`` bumped, and
2. an entry in ``MIGRATIONS`` keyed by the version being upgraded FROM, holding
   the SQL statements that turn that version into the next one.

``medlib administer rebuild`` applies the missing steps in order. Statements
should be idempotent (``IF NOT EXISTS``) where SQLite allows it.
"""
from __future__ import annotations

from typing import Dict, List

SCHEMA_VERSION = 4

MIGRATIONS: Dict[int, List[str]] = {
    # v1 -> v2: index for media-type lookups (query --type, completion)
    1: ["CREATE INDEX IF NOT EXISTS files_media_type ON files (media_type)"],
    # v2 -> v3: a tag may have several parents (tags.parent_id -> tag_parents)
    2: [
        "CREATE TABLE tag_parents ("
        " tag_id INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,"
        " parent_id INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,"
        " PRIMARY KEY (tag_id, parent_id)) WITHOUT ROWID",
        "CREATE INDEX tag_parents_parent ON tag_parents (parent_id)",
        "INSERT INTO tag_parents (tag_id, parent_id)"
        " SELECT id, parent_id FROM tags WHERE parent_id IS NOT NULL",
        "DROP INDEX IF EXISTS tags_parent",
        "ALTER TABLE tags DROP COLUMN parent_id",
    ],
    # v3 -> v4: duration and dimensions. The columns start empty; 'rebuild'
    # then measures every file not yet marked as probed (see admin.probe_pending).
    3: [
        "ALTER TABLE files ADD COLUMN duration REAL",
        "ALTER TABLE files ADD COLUMN width INTEGER",
        "ALTER TABLE files ADD COLUMN height INTEGER",
        "ALTER TABLE files ADD COLUMN probed INTEGER NOT NULL DEFAULT 0",
    ],
}

# The latest schema, as created by 'medlib init'.
SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL,
    media_type    TEXT NOT NULL,
    ext           TEXT NOT NULL,
    original_name TEXT NOT NULL,
    original_path TEXT,
    added_at      TEXT NOT NULL,
    phash         BLOB,
    duration      REAL,     -- seconds; NULL for stills and when unknown
    width         INTEGER,  -- pixels as displayed; NULL for audio
    height        INTEGER,
    probed        INTEGER NOT NULL DEFAULT 0  -- 1 once measured (or nothing to measure)
);
CREATE INDEX files_sha256 ON files (sha256);
CREATE INDEX files_media_type ON files (media_type);

CREATE TABLE file_aliases (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    UNIQUE (file_id, alias_key)
);
CREATE INDEX file_aliases_key ON file_aliases (alias_key);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);

-- The tag hierarchy: a tag may sit under several parents (no cycles).
CREATE TABLE tag_parents (
    tag_id    INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    parent_id INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (tag_id, parent_id)
) WITHOUT ROWID;
CREATE INDEX tag_parents_parent ON tag_parents (parent_id);

CREATE TABLE tag_aliases (
    id        INTEGER PRIMARY KEY,
    tag_id    INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    alias_key TEXT NOT NULL UNIQUE
);
CREATE INDEX tag_aliases_tag ON tag_aliases (tag_id);

CREATE TABLE tag_files (
    tag_id  INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    PRIMARY KEY (tag_id, file_id)
) WITHOUT ROWID;
CREATE INDEX tag_files_file ON tag_files (file_id);

CREATE TABLE file_links (
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    path    TEXT NOT NULL,
    PRIMARY KEY (file_id, path)
) WITHOUT ROWID;
"""
