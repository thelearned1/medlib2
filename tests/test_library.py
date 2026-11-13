import json
import os

import pytest

from medlib import AmbiguousFile, Library, MedlibError, NotFound
from medlib.migrations import SCHEMA_VERSION

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x01" * 32


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with Library.init(root) as library:
        yield library


def make(lib, name, data=PNG):
    p = lib.root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def kinds(issues):
    return sorted(i.kind for i in issues)


# -- repository --------------------------------------------------------------


def test_init_twice_fails(lib):
    with pytest.raises(MedlibError):
        Library.init(lib.root)


def test_find_from_subdirectory(lib):
    sub = lib.root / "a" / "b"
    sub.mkdir(parents=True)
    with Library.find(sub) as found:
        assert found.root == lib.root


def test_find_outside_fails(tmp_path):
    with pytest.raises(MedlibError):
        Library.find(tmp_path)


# -- add ---------------------------------------------------------------------


def test_add_replaces_source_with_relative_symlink(lib):
    src = make(lib, "memes/cat.png")
    res = lib.add(src)
    assert not res.duplicate and res.linked
    assert src.is_symlink()
    assert not os.path.isabs(os.readlink(src))
    assert src.read_bytes() == PNG
    info = lib.file_show(res.file_id)
    assert info.media_type == "image/png"
    assert info.ext == "png"
    assert info.aliases == ["cat.png"]
    assert info.original_path == "memes/cat.png"
    assert info.links == ["memes/cat.png"]
    assert lib.blob_path(res.file_id, "png").is_file()
    assert lib.fsck() == []


def test_add_keep_local(lib):
    src = make(lib, "cat.png")
    res = lib.add(src, keep_local=True)
    assert not src.is_symlink()
    assert lib.file_show(res.file_id).links == []
    assert lib.fsck() == []


def test_add_name_replaces_filename_alias(lib):
    res = lib.add(make(lib, "IMG_0001.png"), name="grumpy cat")
    assert lib.file_show(res.file_id).aliases == ["grumpy cat"]
    assert lib.resolve_file("GRUMPY CAT") == res.file_id


def test_add_duplicate_is_detected_and_offers_alias(lib):
    first = lib.add(make(lib, "a.png"))
    copy = make(lib, "b.png")
    res = lib.add(copy)
    assert res.duplicate and res.file_id == first.file_id
    assert res.alias == "b.png" and not res.alias_present
    assert not copy.is_symlink()  # skipped, source untouched
    assert len(lib.file_list()) == 1
    assert list(lib.tmp_dir.iterdir()) == []
    lib.file_alias(res.file_id, res.alias)
    assert lib.add(copy).alias_present


def test_hash_collision_does_not_block_add(lib):
    first = lib.add(make(lib, "a.png"))
    other = make(lib, "b.gif", GIF)
    # Forge a collision: pretend the stored file has the newcomer's hash/size.
    import hashlib

    lib.db.execute(
        "UPDATE files SET sha256 = ?, size = ? WHERE id = ?",
        (hashlib.sha256(GIF).hexdigest(), len(GIF), first.file_id),
    )
    res = lib.add(other)
    assert not res.duplicate and res.file_id != first.file_id


def test_add_rejects_symlink_and_digit_name(lib):
    src = make(lib, "a.png")
    lib.add(src)
    with pytest.raises(MedlibError):
        lib.add(src)  # now a symlink
    with pytest.raises(MedlibError):
        lib.add(make(lib, "b.gif", GIF), name="123")


def test_digit_filename_gets_no_alias(lib):
    res = lib.add(make(lib, "12345", GIF))
    assert res.alias is None
    info = lib.file_show(res.file_id)
    assert info.aliases == [] and info.media_type == "image/gif"
    assert lib.blob_path(res.file_id, "").is_file()


# -- file references -----------------------------------------------------------


def test_ambiguous_alias(lib):
    a = lib.add(make(lib, "a.png")).file_id
    b = lib.add(make(lib, "b.gif", GIF)).file_id
    lib.file_alias(a, "funny")
    lib.file_alias(b, "Funny")
    with pytest.raises(AmbiguousFile) as e:
        lib.resolve_file("funny")
    assert [c.id for c in e.value.candidates] == [a, b]
    assert lib.resolve_file(str(a)) == a
    with pytest.raises(NotFound):
        lib.resolve_file("nope")


def test_unalias(lib):
    fid = lib.add(make(lib, "a.png")).file_id
    lib.file_unalias(fid, "A.PNG")
    assert lib.file_show(fid).aliases == []
    with pytest.raises(NotFound):
        lib.file_unalias(fid, "a.png")


# -- remove --------------------------------------------------------------------


def test_remove_restores_file_over_symlink(lib):
    src = make(lib, "memes/cat.png")
    fid = lib.add(src).file_id
    blob = lib.blob_path(fid, "png")
    res = lib.remove(fid)
    assert res.path == src and res.dangling == []
    assert src.is_file() and not src.is_symlink()
    assert src.read_bytes() == PNG
    assert os.access(src, os.W_OK)
    assert not blob.exists()
    assert lib.file_list() == []
    assert lib.fsck() == []
    entry = json.loads(lib.log_path.read_text().splitlines()[-1])
    assert entry["op"] == "remove" and entry["aliases"] == ["cat.png"]
    assert entry["sha256"]


def test_remove_falls_back_to_cwd_and_never_overwrites(lib, tmp_path):
    src = make(lib, "cat.png")
    fid = lib.add(src, keep_local=True).file_id  # original path stays occupied
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "cat.png").write_bytes(b"other")
    with pytest.raises(MedlibError):
        lib.remove(fid, cwd=elsewhere)
    assert lib.blob_path(fid, "png").is_file()
    (elsewhere / "cat.png").unlink()
    assert lib.remove(fid, cwd=elsewhere).path == elsewhere.resolve() / "cat.png"


def test_remove_to_and_dangling_links(lib, tmp_path):
    src = make(lib, "cat.png")
    fid = lib.add(src).file_id
    out = tmp_path / "out"
    out.mkdir()
    res = lib.remove(fid, to=out)
    assert res.path == out.resolve() / "cat.png"
    assert res.dangling == ["cat.png"]
    issues = lib.fsck()
    assert kinds(issues) == ["dangling-link"]
    assert lib.fsck(prune=True)[0].fixed
    assert not os.path.lexists(src)
    assert lib.fsck() == []


# -- tags ----------------------------------------------------------------------


def test_tag_alias_rules(lib):
    for bad in ["", "123", "-x", "a b", "a:b", "a&b", "a|b", "!a", "(a)", 'a"', "a'"]:
        with pytest.raises(MedlibError):
            lib.tag_add(bad)
    tid = lib.tag_add("Cat")
    assert lib.resolve_tag("cat") == tid == lib.resolve_tag(str(tid))
    with pytest.raises(MedlibError):
        lib.tag_add("CAT")
    assert lib.tag_show(tid).aliases == ["Cat"]  # display case preserved
    # NFC: composed and decomposed forms are the same alias
    lib.tag_add("café")
    assert lib.find_tag("café") is not None


def test_query_matches_descendants(lib):
    lib.tag_add("animal")
    lib.tag_add("cat", parents=["animal"])
    lib.tag_add("siamese", parents=["cat"])
    lib.tag_add("dog", parents=["animal"])
    a = lib.add(make(lib, "a.png", PNG + b"a")).file_id
    b = lib.add(make(lib, "b.png", PNG + b"b")).file_id
    c = lib.add(make(lib, "c.gif", GIF)).file_id
    lib.file_tag(a, ["cat"])
    lib.file_tag(b, ["siamese"])
    lib.file_tag(c, ["dog"])

    def ids(expression=""):
        return [f.id for f in lib.query(expression)]

    assert ids() == [a, b, c]
    assert ids("animal") == ids("tag:animal") == ids("TAG:'Animal'") == [a, b, c]
    assert ids("cat") == [a, b]
    assert ids("tag=cat") == [a]  # exact: not via the siamese subtag
    assert ids("tag=animal") == []
    assert ids("cat & !siamese") == ids("cat !siamese") == [a]
    assert ids("siamese | dog") == [b, c]
    assert ids("animal type:image/gif") == [c]
    assert ids("type:image") == [a, b, c]
    assert ids("type:png") == ids("type:.PNG") == [a, b]
    assert ids("type:video") == []
    # precedence: ! over & over |
    assert ids("dog | cat & !siamese") == [a, c]
    assert ids("(dog | cat) & !siamese") == [a, c]
    assert ids("!(dog | cat) | siamese") == [b]
    assert ids("!dog & cat | dog & !cat") == [a, b, c]
    assert ids("!!dog") == [c]
    # alias regex: case-insensitive, unanchored, quoted when it has operators
    assert ids("alias:B.PN") == [b]
    assert ids(r"alias:'\.gif$'") == [c]
    assert ids("alias:^a") == [a]
    assert ids('alias:"^(a|c)\\."') == [a, c]
    assert ids("alias:'^(a|c)' & cat") == [a]
    assert ids("untagged") == []
    assert lib.tag_file_count("animal") == 3

    assert lib.file_untag(b, ["siamese"]) == [lib.resolve_tag("siamese")]
    assert ids("cat") == [a]
    assert ids("untagged") == [b]
    assert ids("!untagged") == [a, c]
    with pytest.raises(NotFound):
        lib.file_tag(a, ["nonexistent"])
    with pytest.raises(NotFound):
        lib.query("cat & nonexistent")
    for bad in ["cat &", "(cat", "cat)", "| cat", "size:3", "alias:'('", "tag:", "'open", "!"]:
        with pytest.raises(MedlibError, match="query|no tag"):
            lib.query(bad)


def test_query_quoting(lib):
    odd = lib.tag_add("untagged")  # a tag that shadows the keyword
    comma = lib.tag_add("a,b")
    fid = lib.add(make(lib, "it's.png")).file_id
    lib.file_tag(fid, [odd, comma])
    assert [f.id for f in lib.query("'untagged'")] == [fid]
    assert [f.id for f in lib.query("tag:untagged")] == [fid]
    assert lib.query("untagged") == []
    assert [f.id for f in lib.query('"a,b"')] == [fid]
    assert [f.id for f in lib.query("alias:'it''s'")] == [fid]  # doubled quote
    assert [f.id for f in lib.query('alias:"it\'s"')] == [fid]


def test_tag_parent_cycle_detection(lib):
    a = lib.tag_add("a")
    b = lib.tag_add("b", parents=["a"])
    lib.tag_add("c", parents=["b"])
    with pytest.raises(MedlibError):
        lib.tag_parent("a", "c")
    with pytest.raises(MedlibError):
        lib.tag_parent("a", "a")
    assert lib.tag_unparent("b") == [a]
    assert lib.tag_parent("a", "c") is True
    assert lib.tag_parent("a", "c") is False  # already there
    assert lib.tag_show(a).parent_ids == [lib.resolve_tag("c")]
    assert lib.tag_show(b).parent_ids == []


def test_tag_with_several_parents(lib):
    ogre = lib.tag_add("ogre")
    dreamworks = lib.tag_add("dreamworks")
    lib.tag_add("studio")
    lib.tag_parent("dreamworks", "studio")
    shrek = lib.tag_add("shrek", parents=["ogre", "dreamworks"])
    donkey = lib.tag_add("donkey", parents=["shrek"])
    assert lib.tag_show(shrek).parent_ids == [ogre, dreamworks]

    fid = lib.add(make(lib, "s.png")).file_id
    lib.file_tag(fid, ["donkey"])
    for broader in ("shrek", "ogre", "dreamworks", "studio"):
        assert [f.id for f in lib.query(broader)] == [fid], broader
    assert lib.query("tag=ogre") == []
    assert lib.tag_file_count("studio") == 1

    # loops are refused through either parent
    with pytest.raises(MedlibError):
        lib.tag_parent("studio", "donkey")
    with pytest.raises(MedlibError):
        lib.tag_parent("ogre", "shrek")
    # overlap that exists only because of the hierarchy is not "similarity":
    # not along either line of parents, nor between ogre and dreamworks,
    # which share the file purely through their common subtag.
    assert lib.similar_tags() == []
    other = lib.add(make(lib, "o.png", PNG + b"o")).file_id
    lib.file_tag(other, ["ogre", "dreamworks"])  # tagged with both directly
    [pair] = [p for p in lib.similar_tags() if {p.a.name, p.b.name} == {"ogre", "dreamworks"}]
    assert (pair.shared, pair.count_a, pair.count_b, pair.jaccard) == (1, 2, 2, 1.0)
    lib.file_untag(other, ["ogre", "dreamworks"])

    # unparent one, or all
    with pytest.raises(MedlibError):
        lib.tag_unparent("shrek", "studio")  # not a direct parent
    assert lib.tag_unparent("shrek", "dreamworks") == [dreamworks]
    assert lib.query("dreamworks") == [] and len(lib.query("ogre")) == 1
    lib.tag_parent("shrek", "dreamworks")

    # removing a tag hands its children to every one of its parents
    lib.tag_remove(shrek)
    assert lib.tag_show(donkey).parent_ids == [ogre, dreamworks]
    assert lib.tag_unparent("donkey") == [ogre, dreamworks]
    assert lib.tag_show(donkey).parent_ids == []


def test_tag_remove_alias_vs_whole_tag(lib):
    top = lib.tag_add("top")
    mid = lib.tag_add("mid", parents=["top"])
    leaf = lib.tag_add("leaf", parents=["mid"])
    fid = lib.add(make(lib, "a.png")).file_id
    lib.file_tag(fid, ["mid"])

    with pytest.raises(MedlibError):
        lib.tag_remove("mid")  # last alias
    lib.tag_alias("mid", "middle")
    assert lib.tag_remove("mid") == (mid, False)
    assert lib.tag_show(mid).aliases == ["middle"]

    assert lib.tag_remove("middle", all=True) == (mid, True)
    assert lib.find_tag("middle") is None
    assert lib.tag_show(leaf).parent_ids == [top]  # reparented
    assert lib.file_show(fid).tags == []  # file untouched, tag link gone
    assert lib.tag_remove(str(top)) == (top, True)  # by ID
    assert lib.tag_show(leaf).parent_ids == []


def test_tag_rename(lib):
    tid = lib.tag_add("kitteh")
    lib.tag_rename("kitteh", "Kitty")
    assert lib.tag_show(tid).aliases == ["Kitty"]
    lib.tag_rename(str(tid), "kitten")
    lib.tag_alias(tid, "cat")
    with pytest.raises(MedlibError):
        lib.tag_rename(str(tid), "x")  # ambiguous with two aliases
    lib.tag_add("dog")
    with pytest.raises(MedlibError):
        lib.tag_rename("cat", "DOG")


def test_tag_merge(lib):
    cat = lib.tag_add("cat")
    kitty = lib.tag_add("kitty", parents=["cat"])
    kitten = lib.tag_add("kitten", parents=["kitty"])
    a = lib.add(make(lib, "a.png", PNG + b"a")).file_id
    b = lib.add(make(lib, "b.png", PNG + b"b")).file_id
    lib.file_tag(a, ["cat", "kitty"])
    lib.file_tag(b, ["kitty"])
    with pytest.raises(MedlibError):
        lib.tag_merge("cat", "kitty")  # parent into its own descendant
    assert lib.tag_merge("kitty", "cat") == cat
    assert lib.resolve_tag("kitty") == cat
    assert lib.tag_show(cat).file_count == 2
    assert lib.tag_show(kitten).parent_ids == [cat]
    assert lib.tag_show(cat).parent_ids == []  # kitty's parent was cat itself
    assert kitty not in [t.id for t in lib.tag_list()]


def test_tag_merge_inherits_parents(lib):
    monster = lib.tag_add("monster")
    film = lib.tag_add("film")
    ogre = lib.tag_add("ogre", parents=["film"])
    lib.tag_add("ogres", parents=["monster"])
    lib.tag_merge("ogres", "ogre")
    assert lib.tag_show(ogre).parent_ids == [monster, film]
    assert lib.resolve_tag("ogres") == ogre


# -- links and fsck --------------------------------------------------------------


def test_file_link_and_export(lib, tmp_path):
    fid = lib.add(make(lib, "a.png"), name="funny cat").file_id
    (lib.root / "album").mkdir()
    link = lib.file_link(fid, lib.root / "album")
    assert link.name == "funny cat.png" and link.read_bytes() == PNG
    assert "album/funny cat.png" in lib.file_show(fid).links
    with pytest.raises(MedlibError):
        lib.file_link(fid, link)
    out = tmp_path / "out"
    made = lib.export_links([fid], out)
    assert made[0].read_bytes() == PNG
    assert len(lib.file_show(fid).links) == 2  # export is untracked
    assert lib.fsck() == []


def test_fsck_missing_link_repair_and_prune(lib):
    src = make(lib, "a.png")
    fid = lib.add(src).file_id
    src.unlink()
    assert kinds(lib.fsck()) == ["missing-link"]
    assert lib.fsck(repair=True)[0].fixed
    assert src.is_symlink() and lib.fsck() == []
    src.unlink()
    assert lib.fsck(prune=True)[0].fixed
    assert lib.file_show(fid).links == [] and lib.fsck() == []


def test_fsck_replaced_link(lib):
    src = make(lib, "a.png")
    fid = lib.add(src).file_id
    src.unlink()
    src.write_bytes(b"something else")
    assert kinds(lib.fsck()) == ["replaced-link"]
    lib.fsck(prune=True)
    assert src.read_bytes() == b"something else"
    assert lib.file_show(fid).links == []


def test_fsck_repairs_interrupted_add(lib):
    """Simulate a crash after the DB commit: row and link record exist, but
    the blob is still a temp file and the source is still a regular file."""
    src = make(lib, "a.png")
    fid = lib.add(src).file_id
    blob = lib.blob_path(fid, "png")
    src.unlink()
    src.write_bytes(PNG)
    os.replace(blob, lib.tmp_dir / "deadbeef")
    assert kinds(lib.fsck()) == ["missing-blob", "stale-temp", "unlinked-copy"]
    assert all(i.fixed for i in lib.fsck(repair=True))
    assert blob.is_file() and src.is_symlink()
    assert list(lib.tmp_dir.iterdir()) == []
    assert lib.fsck(verify=True) == []


def test_fsck_orphan_and_corrupt_blob(lib):
    fid = lib.add(make(lib, "a.png")).file_id
    blob = lib.blob_path(fid, "png")
    os.chmod(blob, 0o644)
    blob.write_bytes(b"\x00" * len(PNG))
    assert lib.fsck() == []  # same size: only --verify notices
    assert kinds(lib.fsck(verify=True)) == ["corrupt-blob"]
    blob.write_bytes(b"short")
    (blob.parent / "999.png").write_bytes(b"x")
    assert kinds(lib.fsck()) == ["corrupt-blob", "orphan-blob"]


def test_blob_is_read_only(lib):
    fid = lib.add(make(lib, "a.png")).file_id
    assert not os.access(lib.blob_path(fid, "png"), os.W_OK)


# -- administration ----------------------------------------------------------------


def test_init_writes_schema_version_file(lib):
    from medlib.migrations import SCHEMA_VERSION

    assert (lib.meta / "schema_version").read_text() == f"{SCHEMA_VERSION}\n"
    assert lib.schema_version() == SCHEMA_VERSION


def test_every_version_has_a_migration():
    from medlib.migrations import MIGRATIONS, SCHEMA_VERSION

    assert sorted(MIGRATIONS) == list(range(1, SCHEMA_VERSION))


def indexes(lib):
    return {r[0] for r in lib.db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def make_old_repo(root, version):
    """A repository as medlib of that schema version would have left it."""
    import sqlite3

    from old_schemas import BY_VERSION

    meta = root / ".medlib"
    (meta / "files").mkdir(parents=True)
    (meta / "tmp").mkdir()
    db = sqlite3.connect(str(meta / "medlib.db"))
    db.executescript(BY_VERSION[version])
    db.execute("INSERT INTO schema_version VALUES (?)", (version,))
    edges = [(1, None), (2, 1), (5, 2)]
    if version < 3:  # one parent per tag, held in tags.parent_id
        db.executemany("INSERT INTO tags (id, parent_id) VALUES (?, ?)", edges)
    else:
        db.executemany("INSERT INTO tags (id) VALUES (?)", [(t,) for t, _ in edges])
        db.executemany("INSERT INTO tag_parents VALUES (?, ?)", [e for e in edges if e[1]])
    db.executemany(
        "INSERT INTO tag_aliases (tag_id, alias, alias_key) VALUES (?, ?, ?)",
        [(1, "animal", "animal"), (2, "Cat", "cat"), (5, "siamese", "siamese")],
    )
    db.execute(
        "INSERT INTO files (id, sha256, size, media_type, ext, original_name, added_at)"
        " VALUES (1, 'x', 1, 'image/png', 'png', 'a.png', '2026-01-01')"
    )
    db.execute("INSERT INTO tag_files VALUES (5, 1)")
    db.commit()
    db.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_rebuild_migrates_old_repository(tmp_path, version):
    from medlib.migrations import SCHEMA_VERSION

    make_old_repo(tmp_path, version)
    with pytest.raises(MedlibError, match="administer rebuild"):
        Library(tmp_path)
    with Library(tmp_path, check_schema=False) as old:
        actions = old.rebuild()
        assert any(f"v{SCHEMA_VERSION - 1} -> v{SCHEMA_VERSION}" in a for a in actions)
        assert "files_media_type" in indexes(old)
        [backup] = old.backup_list()
        assert backup.version == version
        assert backup.path.parent == old.meta / "backups" / f"v{version}"
        assert old.version_path.read_text() == f"{SCHEMA_VERSION}\n"
    with Library(tmp_path) as new:
        # the single-parent column became rows in tag_parents
        assert new.tag_show(2).parent_ids == [1]
        assert new.tag_show(5).parent_ids == [2]
        assert new.tag_show(1).parent_ids == []
        columns = [r[1] for r in new.db.execute("PRAGMA table_info(tags)")]
        assert columns == ["id"]
        assert [f.id for f in new.query("animal")] == [1]
        # measurement columns exist and are empty until the file is probed
        only = new.file_show(1)
        assert (only.duration, only.width, only.height) == (None, None, None)
        assert new.query("duration:>0 | width:>0") == []
        assert new.tag_add("new") == 6  # IDs carry on, none reused
        again = new.rebuild()
        assert not any(a.startswith(("added", "created", "migrated", "backed")) for a in again)


def test_rebuild_adds_missing_columns_and_indexes(lib):
    lib.add(make(lib, "a.png"))
    lib.db.execute("ALTER TABLE files DROP COLUMN phash")
    lib.db.execute("DROP INDEX tag_files_file")
    lib.db.execute("DROP TABLE tag_parents")
    actions = lib.rebuild()
    assert "created table tag_parents" in actions and "tag_parents_parent" in indexes(lib)
    assert "added column files.phash" in actions
    assert "created index tag_files_file" in actions
    assert lib.db.execute("SELECT phash FROM files").fetchone()[0] is None
    again = lib.rebuild()  # idempotent
    assert not any(a.startswith(("added", "created", "migrated", "backed")) for a in again)


def test_rebuild_refuses_newer_schema(lib):
    lib.db.execute("UPDATE schema_version SET version = 999")
    with pytest.raises(MedlibError, match="upgrade medlib"):
        lib.rebuild()
    with pytest.raises(MedlibError, match="upgrade medlib"):
        Library(lib.root)


def test_fsck_schema_version_file(lib):
    lib.version_path.unlink()
    assert kinds(lib.fsck()) == ["schema-version"]
    lib.fsck(repair=True)
    assert lib.fsck() == []


def tag_files(lib, tag, numbers):
    if lib.find_tag(tag) is None:
        lib.tag_add(tag)
    for n in numbers:
        name = f"f{n}.png"
        try:
            fid = lib.resolve_file(name)
        except NotFound:
            fid = lib.add(make(lib, name, PNG + str(n).encode())).file_id
        lib.file_tag(fid, [tag])


def test_similar_tags_weighting(lib):
    # shrek/ogre: 5 files each, identical        -> high
    # tiny/wee:   1 file each, identical         -> same overlap, less evidence
    # few/cat:    5 'few' files all among 40 cat -> low
    tag_files(lib, "shrek", range(0, 5))
    tag_files(lib, "ogre", range(0, 5))
    tag_files(lib, "tiny", [100])
    tag_files(lib, "wee", [100])
    tag_files(lib, "cat", range(200, 240))
    tag_files(lib, "few", range(200, 205))

    pairs = lib.similar_tags()
    names = [(p.a.name, p.b.name) for p in pairs]
    assert names == [("shrek", "ogre"), ("tiny", "wee"), ("cat", "few")]
    top, small, low = pairs
    assert top.jaccard == small.jaccard == 1.0
    assert top.score > 0.8 > small.score > low.score
    assert (low.count_a, low.count_b, low.shared) == (40, 5, 5)
    assert low.jaccard == pytest.approx(0.125) and low.score < 0.125

    assert len(lib.similar_tags(limit=1)) == 1
    assert len(lib.similar_tags(min_score=0.8)) == 1
    assert len(lib.similar_tags(min_shared=5)) == 2


def test_similar_tags_skips_lineage_and_counts_descendants(lib):
    lib.tag_add("animal")
    lib.tag_add("cat", parents=["animal"])
    lib.tag_add("siamese", parents=["cat"])
    tag_files(lib, "siamese", range(0, 4))
    tag_files(lib, "kitty", range(0, 4))
    pairs = {(p.a.name, p.b.name): p for p in lib.similar_tags()}
    # no animal~cat, cat~siamese, animal~siamese; kitty pairs with all three
    assert set(pairs) == {("animal", "kitty"), ("cat", "kitty"), ("siamese", "kitty")}
    assert pairs[("cat", "kitty")].count_a == 4  # via the siamese subtag
    assert lib.similar_tags() != [] and Library  # sanity


def test_backups(lib):
    import sqlite3

    lib.tag_add("cat")
    first = lib.backup_create()
    second = lib.backup_create()  # same second: must not overwrite
    assert first.path != second.path
    assert first.path == lib.meta / "backups" / f"v{SCHEMA_VERSION}" / f"{first.timestamp}.bak"
    assert len(list(first.path.parent.iterdir())) == 2  # no -wal/-shm companions
    db = sqlite3.connect(str(first.path))
    assert db.execute("SELECT alias FROM tag_aliases").fetchall() == [("cat",)]
    db.close()

    assert [b.timestamp for b in lib.backup_list()] == [first.timestamp, second.timestamp]
    assert lib.backup_list(version=1) == []
    assert len(lib.backup_list(version=SCHEMA_VERSION)) == 2

    lib.backup_remove(SCHEMA_VERSION, first.timestamp + ".bak")
    assert [b.timestamp for b in lib.backup_list()] == [second.timestamp]
    with pytest.raises(NotFound):
        lib.backup_remove(SCHEMA_VERSION, first.timestamp)
    with pytest.raises(MedlibError):
        lib.backup_remove(SCHEMA_VERSION, "../../medlib.db")
    lib.backup_remove(SCHEMA_VERSION, second.timestamp)
    assert not (lib.meta / "backups" / f"v{SCHEMA_VERSION}").exists()
    assert lib.fsck() == []


# -- duration and dimensions ------------------------------------------------------


def add_media(lib, media, name):
    import shutil

    shutil.copy(str(media / name), str(lib.root / name))
    return lib.add(lib.root / name).file_id


def test_add_measures_media(lib, media):
    ids = {
        name: add_media(lib, media, name)
        for name in ("anim.gif", "still.gif", "still.png", "clip.mkv", "turned.mp4", "tone.wav")
    }
    plain = lib.add(make(lib, "notes.txt", b"hello")).file_id

    def measured(name):
        f = lib.file_show(ids[name])
        return (f.duration and round(f.duration, 1), f.width, f.height)

    assert measured("anim.gif") == (2.0, 64, 48)
    assert measured("still.gif") == (None, 64, 48)  # one frame: no duration
    assert measured("still.png") == (None, 64, 48)
    assert measured("clip.mkv") == (3.0, 64, 48)
    assert measured("turned.mp4") == (3.0, 48, 64)  # as displayed
    assert measured("tone.wav") == (4.0, None, None)
    assert lib.db.execute("SELECT COUNT(*) FROM files WHERE probed = 0").fetchone()[0] == 0

    def q(expression):
        return sorted(k for k, v in ids.items() if v in [f.id for f in lib.query(expression)])

    assert q("duration:<2.5") == ["anim.gif"]
    assert q("duration:>=3") == ["clip.mkv", "tone.wav", "turned.mp4"]
    assert q("duration:2..3") == ["anim.gif", "clip.mkv", "turned.mp4"]
    assert q("duration:3.5..") == ["tone.wav"]
    assert q("duration:..0:02") == ["anim.gif"]
    assert q("duration:3") == ["clip.mkv", "turned.mp4"]  # about 3 seconds
    assert q("type:image duration:>0") == ["anim.gif"]  # animated
    assert q("width:64") == ["anim.gif", "clip.mkv", "still.gif", "still.png"]
    assert q("height:>48") == ["turned.mp4"]
    assert q("width:<=48 | type:wav") == ["tone.wav", "turned.mp4"]
    # a missing value fails the comparison, so its negation succeeds
    assert q("!duration:>0") == ["still.gif", "still.png"]
    assert plain in [f.id for f in lib.query("!duration:>0 !width:>0")]


def test_duration_values_and_syntax_errors(lib):
    from medlib.querylang import parse_duration

    assert parse_duration("90") == 90
    assert parse_duration("2.5s") == 2.5
    assert parse_duration("1m30") == 90
    assert parse_duration("1h5m") == 3900
    assert parse_duration("1:30") == 90
    assert parse_duration("1:02:03") == 3723
    for bad in ("duration:", "duration:soon", "duration:<", "duration:..", "width:1.5", "height:>big"):
        with pytest.raises(MedlibError, match="query:"):
            lib.query(bad)


def test_rebuild_measures_files_added_without_ffprobe(lib, media, monkeypatch):
    monkeypatch.setenv("MEDLIB_FFPROBE", "")  # as if ffmpeg were not installed
    import shutil

    shutil.copy(str(media / "anim.gif"), str(lib.root / "anim.gif"))
    res = lib.add(lib.root / "anim.gif")
    text = lib.add(make(lib, "notes.txt", b"hello"))
    assert not res.probed and text.probed  # nothing to measure in a text file
    assert lib.file_show(res.file_id).width is None
    assert any("ffprobe not found: 1 file(s)" in a for a in lib.rebuild())

    monkeypatch.delenv("MEDLIB_FFPROBE")
    seen = []
    actions = lib.rebuild(progress=lambda done, total: seen.append((done, total)))
    assert "measured duration and dimensions of 1 file(s)" in actions
    assert seen == [(1, 1)]
    f = lib.file_show(res.file_id)
    assert (round(f.duration, 1), f.width, f.height) == (2.0, 64, 48)
    assert not any("measured" in a for a in lib.rebuild())  # nothing left to do


def test_completion_collapses_aliases_of_one_tag_or_file(lib):
    lib.tag_add("cat")
    lib.tag_alias("cat", "catto")
    lib.tag_alias("cat", "a-cat")  # newer, though it sorts first
    assert [a for a, _ in lib.complete_tags("ca")] == ["cat"]  # the oldest
    assert [a for a, _ in lib.complete_tags("")] == ["cat"]
    assert [a for a, _ in lib.complete_tags("catt")] == ["catto"]
    assert [a for a, _ in lib.complete_tags("ca", collapse=False)] == ["cat", "catto"]
    lib.tag_add("car")
    assert [a for a, _ in lib.complete_tags("ca")] == ["car", "cat", "catto"]

    one = lib.add(make(lib, "shrek.png")).file_id
    lib.file_alias(one, "shrek-angry")
    assert [a for a, _, _ in lib.complete_files("sh")] == ["shrek.png"]
    two = lib.add(make(lib, "sub/shrek.png", GIF)).file_id  # alias now shared
    assert [a for a, _, _ in lib.complete_files("sh")] == ["shrek-angry", "shrek.png"]
    lib.file_alias(two, "zzz")
    assert [a for a, _, _ in lib.complete_files("z")] == ["zzz"]
