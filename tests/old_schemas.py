"""Frozen copies of past schemas, for migration tests. Never edit these."""

V1 = """
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
    phash         BLOB
);
CREATE INDEX files_sha256 ON files (sha256);

CREATE TABLE file_aliases (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    UNIQUE (file_id, alias_key)
);
CREATE INDEX file_aliases_key ON file_aliases (alias_key);

CREATE TABLE tags (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES tags (id)
);
CREATE INDEX tags_parent ON tags (parent_id);

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

V2 = """
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
    phash         BLOB
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
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES tags (id)
);
CREATE INDEX tags_parent ON tags (parent_id);

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

V3 = """
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
    phash         BLOB
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

BY_VERSION = {1: V1, 2: V2, 3: V3}
