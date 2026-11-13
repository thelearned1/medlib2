# medlib

Keep media files (memes, mostly) in one place, tag them, and find them again.

- **One copy of everything.** Files live inside a `.medlib/` directory; adding
  the same content twice stores it once.
- **Your folders stay as they are.** `add` leaves a symlink where the file was,
  so it still opens from there.
- **Tags nest.** A file tagged `siamese` is found by a query for `cat`, and a
  tag can sit under several parents.
- **Nothing is deleted.** `remove` hands the file back.

Everything here is also available from the command line: `medlib --help`, and
`--help` on any command.

## Install

Requires Python 3.9 or later. Optional: `ffprobe` (part of
[ffmpeg](https://ffmpeg.org)) on your `PATH`, which medlib uses to record the
duration and pixel size of what you add.

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

That gives you `.venv/bin/medlib`. For tab completion in zsh, add this to
`~/.zshrc` after the `compinit` line:

```bash
eval "$(medlib completion zsh)"
```

## Getting started

```bash
cd ~/Pictures/memes
medlib init                              # creates .medlib/ here
medlib add cat.png shrek.gif             # each becomes a symlink into .medlib/
medlib add --tag dog --tag funny d*.jpg  # tag while adding
medlib file tag cat.png cat funny        # offers to create tags that don't exist
medlib tag add siamese --parent cat      # tags can be nested
medlib query 'cat & !siamese'            # find things
medlib tag list                          # see the tags as a tree
```

Commands work from any directory inside the repository, as with git.

## Concepts

### Files

`medlib add FILE` copies the file into `.medlib/`, then replaces the original
with a relative symlink to the stored copy. The stored copy is marked read
only, because editing it through a link would silently change the library's
file. `--keep-local` copies the file in and leaves the original untouched.

If the content is already in the library, nothing is added: medlib names the
existing file and offers to attach the new name to it as an alias.

Images, video and audio are measured as they are added: playing time, and
width and height as displayed (a phone video filmed upright counts as upright).
A still image has no duration; an animated GIF or WebP does. Durations are read
from the file's header, which is quick but only as accurate as the file.
Without ffprobe files are added unmeasured, and `medlib administer rebuild`
measures them once it is installed.

`medlib remove FILE` moves the stored copy back to where it was added from (or
to `--to PATH`) as an ordinary file, and forgets it.

### IDs and aliases

Files and tags are both referred to in one of two ways:

| | Looks like | Notes |
|---|---|---|
| **ID** | all digits: `42` | Assigned by medlib, never reused. Safe in scripts. |
| **alias** | anything else: `cat.png`, `"grumpy cat"` | Ignores case. |

A file can also be named by the **path of a symlink** medlib made for it, which
is tried before aliases. That makes shell globs work: `medlib file retag cat
dog memes/dogs/*`.

A **file** gets its filename as an alias when added (or `--name`), can have
several, and can share an alias with other files. If an alias matches more than
one file, medlib asks which you mean; when not run from a terminal it stops and
lists the matching IDs.

A **tag** has one or more aliases, all equally valid names for it, and no two
tags can share one. A tag alias must contain a non-digit, must not start with
`-`, and must not contain spaces or any of `: & | ! ( ) " '` (those are the
query language's punctuation).

### Tags

Tags nest, and a tag can sit under more than one parent: `shrek` is both an
`ogre` and a `dreamworks` film. Tag a file with the most specific tag that
applies, and queries for any of the broader tags will find it too.

```bash
medlib tag add shrek --parent ogre --parent dreamworks
medlib tag parent siamese cat            # add a parent later
medlib tag unparent shrek dreamworks     # remove one
```

```
animal [1] (0)
├── cat,kitty [2] (12)
│   └── siamese [4] (3)
└── dog [3] (1)
dreamworks [5] (2)
└── shrek [7] (9)
ogre [6] (0)
└── shrek [7] (9)
```

That is `medlib tag list`: aliases, the ID in brackets, and the number of files
tagged directly in parentheses. A tag with several parents appears under each.
Loops are refused: a tag cannot go under one of its own subtags.

## Commands

| Command | Does |
|---|---|
| `init [PATH]` | Create a repository. |
| `add PATHS...` | Add files (`--tag TAG`, repeatable; `--keep-local`, `--name ALIAS`, `--yes`). Same as `file add`. |
| `remove FILE...` | Give files back (`--to PATH`). Same as `file remove`. |
| `query [EXPRESSION]` | Find files (`--output DIR`, `--ids`). |
| `fsck` | Check the repository (`--repair`, `--prune`, `--verify`). |
| `file tag FILE TAG...` / `file untag` | Attach or detach tags. |
| `file retag OLD NEW FILE...` | Swap one tag for another on the files named; files without OLD are skipped. |
| `file alias FILE ALIAS` / `file unalias` | Add or remove a file's alias. |
| `file link FILE PATH` | Make another symlink to a file. |
| `file show FILE` / `file list` | Inspect files. |
| `tag add ALIAS...` | Create tags (`--parent TAG`, repeatable). |
| `tag alias TAG ALIAS` / `tag rename ALIAS NEW` | Add or change a tag's alias. |
| `tag remove TAG` | Remove one alias, or with `--all` or an ID the whole tag. |
| `tag parent TAG PARENT...` / `tag unparent TAG [PARENT...]` | Add or remove a tag's parents. |
| `tag merge SRC DST` | Fold one tag into another. |
| `tag list` / `tag show TAG` | Inspect tags (`--depth N`, `--start TAG`). |
| `administer rebuild` | Bring the database up to date after upgrading medlib; measure unmeasured files. |
| `administer backup create` / `list` / `remove` | Manage database backups. |
| `administer vacuum` | Suggest tags similar enough to merge. |
| `completion` | Print the zsh completion script. |

## Query language

```bash
medlib query 'cat & !siamese'
medlib query '(shrek | ogre) type:gif'
medlib query 'alias:"^IMG_\d+" | untagged'
medlib query 'type:video duration:<10 width:>=1280'
```

| Term | Matches files that… |
|---|---|
| `cat` or `tag:cat` | are tagged `cat` or any of its subtags |
| `tag=cat` | are tagged `cat` itself, not through a subtag |
| `type:KIND` | are of that kind: `image`, `video`, `audio`; an extension such as `gif`; or a full media type such as `image/png` |
| `alias:REGEX` | have an alias matching the regular expression (ignores case; matches anywhere unless anchored) |
| `duration:<10` | play for under 10 seconds |
| `width:>=1920`, `height:<500` | are that many pixels across or down |
| `untagged` | have no tags at all |

| Operator | Meaning |
|---|---|
| `!a` | not |
| `a & b`, `a b` | and (a space means the same) |
| `a \| b` | or |
| `( )` | grouping |

`!` binds tightest, then `&`, then `|`, so `a | b & !c` means `a | (b & (!c))`.

`duration:`, `width:` and `height:` take a comparison: `<`, `<=`, `>`, `>=`, or
a range such as `5..30` (both ends included; either may be left out). Write a
duration as seconds (`90`, `2.5`), with units (`1m30`, `1h5m`) or as a clock
time (`1:30`). A duration with no operator, `duration:10`, matches to within
half a second.

A file with no value for a measurement fails every comparison on it: a still
image has no duration, audio has no width. So `!duration:<10` finds stills as
well as long clips, and `type:image duration:>0` finds animated images.

Values may be quoted with `'` or `"`. Inside quotes everything is literal, and
the quote character itself is written twice. Quote any regular expression that
contains an operator character. Put the whole expression in single quotes so
the shell leaves it alone.

Naming a tag that does not exist is an error, not an empty result. An empty
expression matches every file.

`--output DIR` also symlinks the matches into a directory, which is handy for
browsing results in a file manager. `--ids` prints only IDs, for scripts.

## Maintenance

**`medlib fsck`** reports inconsistencies and changes nothing unless asked.
`--repair` fixes what can be fixed without losing anything (for example,
finishing an `add` that was interrupted). `--prune` forgets links that are gone
and deletes dangling symlinks left behind by `remove`. `--verify` also re-reads
every stored file to check its hash. `medlib fsck --help` lists every kind of
problem and what fixes it. Exit status is 1 if anything remains unfixed.

**`medlib administer rebuild`** upgrades the database after a medlib upgrade.
medlib tells you when this is needed. The database is backed up first. It also
measures any file not yet measured, so run it after installing ffmpeg, too.

**`medlib administer backup`** copies the database to
`.medlib/backups/v<schema version>/<timestamp>.bak`. A backup holds aliases,
tags and link records, **not the media files**. There is no restore command: to
restore, with nothing else using the repository, copy a backup over
`.medlib/medlib.db` and delete `medlib.db-wal` and `medlib.db-shm` if present.

**`medlib administer vacuum`** lists pairs of tags that mark nearly the same
files, best candidates first, so you can `tag merge` them. The score is the
overlap between the two tags' files, marked down when it rests on only a few
files.

## What is in `.medlib/`

| Path | Contents |
|---|---|
| `medlib.db` | SQLite database: files, aliases, tags, links |
| `files/<n>/<id>.<ext>` | The stored files, 1000 per directory |
| `log` | One JSON line per change, including the details of removed files |
| `schema_version` | The database's schema version |
| `backups/` | Database backups |
| `tmp/` | Scratch space during `add` |

To back up a whole library, copy the directory containing `.medlib/`.

## Development

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The code is in `src/medlib/`:

| Module | Role |
|---|---|
| `cli.py` | The command line; all prompting and printing |
| `library.py` | `Library`: the repository, its files and its tags |
| `querylang.py` | Query parser and its translation to SQL |
| `fsck.py` | Consistency checks and repair |
| `admin.py` | Schema upgrades and backups |
| `similarity.py` | Tag similarity scoring |
| `completion.py` | Lookups behind tab completion |
| `probe.py` | Duration and dimensions, via ffprobe |
| `migrations.py` | The schema and its migrations |
| `models.py`, `aliases.py`, `fsutil.py`, `sniff.py` | Data types, alias rules, filesystem helpers, media type detection |

A change to the schema needs a version bump and a migration script in
`migrations.py`.
