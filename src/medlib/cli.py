"""Thin Click front end over :class:`medlib.library.Library`."""
from __future__ import annotations

import copy
import os
import sys
from typing import Dict, List, Optional

import click
from click.shell_completion import CompletionItem, get_completion_class

from .aliases import is_id
from .library import Library
from .models import AmbiguousFile, FileInfo, MedlibError, TagInfo


class Group(click.Group):
    """Turns library errors into clean CLI errors."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except MedlibError as e:
            raise click.ClickException(str(e))


def open_library() -> Library:
    return Library.find(os.getcwd())


def interactive() -> bool:
    return sys.stdin.isatty()


def confirm(question: str, yes: bool) -> bool:
    """--yes accepts; without a terminal the answer is no."""
    if yes:
        return True
    if not interactive():
        click.echo(f"{question} -> skipped (pass --yes to accept)")
        return False
    return click.confirm(question, default=True)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def human_duration(seconds: float) -> str:
    """'4.2s', '1:05' or '1:02:03'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = int(round(seconds))
    h, m, s = whole // 3600, whole % 3600 // 60, whole % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def file_line(f: FileInfo) -> str:
    tags = "  [" + ", ".join(name for _, name in f.tags) + "]" if f.tags else ""
    return f"{f.id:>5}  {f.media_type:<16} {human_size(f.size):>8}  {f.name}{tags}"


def pick_file(lib: Library, ref: str) -> int:
    """Resolve a file reference, asking the user when an alias is ambiguous."""
    try:
        return lib.resolve_file(ref)
    except AmbiguousFile as e:
        if not interactive():
            raise
        click.echo(f"'{ref}' matches several files:")
        for c in e.candidates:
            click.echo("  " + file_line(c))
        choice = click.prompt(
            "File ID", type=click.Choice([str(c.id) for c in e.candidates]), show_choices=False
        )
        return int(choice)


# -- shell completion --------------------------------------------------------
# Called by Click on every <Tab>. Each lookup is a LIMITed index range scan,
# and any failure (e.g. not inside a repository) just yields no candidates.

COMPLETE_LIMIT = 50


def _item(value: str, help: Optional[str] = None) -> CompletionItem:
    # Click's zsh script splits "value:description" on colons, so a value
    # containing one is offered without a description.
    return CompletionItem(value, help=None if ":" in value else help)


def _completer(fn):
    def wrapper(ctx, param, incomplete):
        try:
            with open_library() as lib:
                return fn(lib, ctx, param, incomplete)
        except Exception:
            return []

    return wrapper


def _given(ctx, param) -> set:
    """Values already typed for a multi-value parameter."""
    value = ctx.params.get(param.name) or ()
    return {v.casefold() for v in value} if isinstance(value, (tuple, list)) else set()


@_completer
def complete_file(lib, ctx, param, incomplete):
    items = []
    for alias, fid, count in lib.complete_files(incomplete, COMPLETE_LIMIT):
        items.append(_item(alias, f"file {fid}" if count == 1 else f"{count} files"))
    return items


@_completer
def complete_tag(lib, ctx, param, incomplete):
    """Tag aliases; for 'untag', only the tags on the file being untagged."""
    file_id = None
    if ctx.command.name == "untag" and ctx.params.get("file_ref"):
        try:
            file_id = lib.resolve_file(ctx.params["file_ref"])
        except MedlibError:
            pass
    skip = _given(ctx, param)
    return [
        _item(alias, f"tag {tid}")
        for alias, tid in lib.complete_tags(incomplete, COMPLETE_LIMIT, file_id=file_id)
        if alias.casefold() not in skip
    ]


@_completer
def complete_tag_alias(lib, ctx, param, incomplete):
    """For 'tag remove' and 'tag rename', which act on the alias named: every
    alias, even when they all belong to one tag."""
    found = lib.complete_tags(incomplete, COMPLETE_LIMIT, collapse=False)
    return [_item(alias, f"tag {tid}") for alias, tid in found]


@_completer
def complete_own_alias(lib, ctx, param, incomplete):
    """For 'file unalias': the aliases of the file named first."""
    fid = lib.resolve_file(ctx.params["file_ref"])
    return [_item(a) for a in lib.complete_file_aliases(fid, incomplete, COMPLETE_LIMIT)]


def ensure_tags(lib: Library, tags, yes: bool) -> None:
    """Make sure every tag named exists, offering to create missing ones."""
    for t in tags:
        if lib.find_tag(t) is not None:
            continue
        if is_id(t):
            raise click.ClickException(f"no tag with ID '{t}'")
        if not confirm(f"Tag '{t}' does not exist. Create it?", yes):
            raise click.ClickException(f"no tag with alias '{t}'")
        lib.tag_add(t)
        click.echo(f"created tag '{t}'")


def tag_label(t: TagInfo) -> str:
    extra = f" (also: {', '.join(t.aliases[1:])})" if len(t.aliases) > 1 else ""
    return f"{t.name}{extra}"


# --------------------------------------------------------------------------


FILE_NOTE = "FILE is a file ID, alias or symlink path; see 'medlib file --help'."
TAG_NOTE = "TAG is a tag ID or alias; see 'medlib tag --help'."

MAIN_HELP = """Keep media files in one place, tag them, and find them again.

A repository is a directory containing .medlib/, made by 'medlib init'.
Commands work from anywhere inside it, as with git.

'medlib add' moves a file into .medlib/ and leaves a symlink in its place, so
the file still opens from where it was. Identical files are stored only once.
'medlib remove' puts a file back; medlib never deletes your media.

\b
Getting started:
  medlib init
  medlib add cat.png shrek.gif
  medlib file tag cat.png cat funny      # offers to create new tags
  medlib tag add siamese --parent cat    # tags can be nested
  medlib query 'cat & !siamese'
  medlib tag list

Files and tags are referred to by ID (all digits) or by alias (anything
else). 'medlib file --help' and 'medlib tag --help' explain both;
'medlib query --help' describes the query language. Every command has its
own --help.
"""

FILE_HELP = """Work with files: tag them, name them, link to them.

\b
A FILE argument is one of:
  an ID      all digits, as printed by 'file list' and 'query'    42
  a path     to a symlink medlib made for the file     memes/cat.png
  an alias   anything else                       cat.png, "grumpy cat"

A path is tried before an alias, so shell globs work: 'memes/*.png' names
exactly the files linked in that directory.

A file gets its filename as an alias when it is added, and can be given more
with 'file alias'. Aliases ignore case and may contain anything, spaces
included, as long as they are not all digits.

Aliases need not be unique. If one matches several files, medlib shows them
and asks which you mean; when not run from a terminal it stops and lists the
matching IDs instead. IDs are never reused, so they are safe in scripts.
"""

TAG_HELP = """Work with tags: create, rename, arrange and merge them.

\b
A TAG argument is one of:
  an ID      all digits, as printed in brackets by 'tag list'     7
  an alias   anything else                                 cat, Kitty

A tag has one or more aliases, all equally valid names for it. Aliases ignore
case and are unique: no two tags can share one.

\b
A tag alias must
  - contain at least one character that is not a digit
  - not start with '-'
  - not contain spaces or any of  : & | ! ( ) " '

Tags nest: a tag can sit under one or more parent tags ('tag add --parent',
'tag parent'), so 'shrek' can be under both 'ogre' and 'dreamworks'. A file
tagged 'shrek' is then found by a query for either parent, so tag files with
the most specific tag that applies.
"""


@click.group(
    cls=Group,
    help=MAIN_HELP,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 80},
)
@click.version_option(package_name="medlib")
def cli():
    pass


@cli.command()
@click.argument("path", default=".", type=click.Path(file_okay=False))
def init(path):
    """Create a repository in PATH (default: here).

    Makes PATH/.medlib/, which will hold the database, the stored files, a
    log of every change, and database backups. Everything under PATH can
    then be added. Fails if PATH already has a .medlib/.
    """
    lib = Library.init(path)
    click.echo(f"Initialized medlib repository in {lib.meta}")


# -- file ------------------------------------------------------------------


@cli.group(cls=Group, help=FILE_HELP, short_help="Work with files.")
def file():
    pass


@file.command("add", short_help="Add files to the library.")
@click.argument("paths", nargs=-1, required=True, type=click.Path())
@click.option("--keep-local", is_flag=True, help="Copy the file in but leave the original alone (no symlink).")
@click.option("--name", metavar="ALIAS", help="Use this alias instead of the filename (one file only).")
@click.option("-t", "--tag", "tags", metavar="TAG", multiple=True, shell_complete=complete_tag, help="Tag every file added with TAG; may be repeated.")
@click.option("-y", "--yes", is_flag=True, help="Answer yes to questions: create missing tags, alias duplicates.")
def file_add(paths, keep_local, name, tags, yes):
    """Add files to the library.

    Each file is copied into .medlib/ and then replaced by a symlink to the
    stored copy, so it still opens from where it was. The stored copy is
    marked read only, because editing it through a link would silently change the
    library's file. Symlinks themselves cannot be added.

    The filename becomes the file's alias, unless --name supplies another.
    (A filename that is all digits gives no alias; use the file's ID.)

    Images, video and audio are measured as they are added (duration, width
    and height, for use in queries). This needs ffprobe, which comes with
    ffmpeg; without it files are still added, and 'medlib administer
    rebuild' measures them later.

    A file whose content is already in the library is not added again.
    medlib names the existing file and offers to give it this name as an
    additional alias; the duplicate itself is left untouched.

    --tag attaches a tag to every file named, and can be given any number of
    times. A tag that does not exist yet is created, after asking, before
    anything is added. A duplicate is still not added, but the existing file
    receives the tags.

    \b
    Examples:
      medlib add cat.png shrek.gif
      medlib add --tag cat --tag funny cat1.png cat2.png
      medlib add --name "grumpy cat" IMG_0042.jpg
      medlib add --keep-local ~/Downloads/meme.webp
    """
    if name is not None and len(paths) > 1:
        raise click.UsageError("--name can only be used with a single file")
    lib = open_library()
    ensure_tags(lib, tags, yes)

    def apply_tags(file_id: int) -> None:
        if not tags:
            return
        added = lib.file_tag(file_id, tags)
        if added:
            names = ", ".join(lib.tag_show(t).name for t in added)
            click.echo(f"  file {file_id}: tagged {names}")

    failed = False
    unmeasured = 0
    for path in paths:
        try:
            res = lib.add(path, name=name, keep_local=keep_local)
        except MedlibError as e:
            if len(paths) == 1:
                raise
            click.echo(f"error: {e}", err=True)
            failed = True
            continue
        if not res.duplicate:
            unmeasured += not res.probed
            how = "kept in place" if keep_local else "linked"
            click.echo(f"added {path} as file {res.file_id} ({how})")
            apply_tags(res.file_id)
            continue
        existing = lib.file_show(res.file_id)
        click.echo(f"{path} is a duplicate of file {res.file_id} ({existing.name}); not added")
        if res.alias and not res.alias_present:
            if confirm(f"Add '{res.alias}' as an alias of file {res.file_id}?", yes):
                lib.file_alias(res.file_id, res.alias)
                click.echo(f"aliased file {res.file_id} as '{res.alias}'")
        apply_tags(res.file_id)
    if unmeasured:
        click.echo(
            f"note: ffprobe not found, so {unmeasured} file(s) have no duration or "
            "dimensions; install ffmpeg, then run 'medlib administer rebuild'",
            err=True,
        )
    if failed:
        sys.exit(1)


@file.command("remove", short_help="Take files out of the library.", epilog=FILE_NOTE)
@click.argument("files", metavar="FILE...", nargs=-1, required=True, shell_complete=complete_file)
@click.option("--to", "to", type=click.Path(), metavar="PATH", help="Put the file here instead (a path, or a directory).")
def file_remove(files, to):
    """Take files out of the library.

    The stored copy is moved back out as an ordinary, writable file and
    medlib forgets the file. Its tags and aliases go with it, though the log
    in .medlib/log keeps a record of them. Nothing is deleted.

    The file returns to the path it was added from, replacing the symlink
    there. If something else now occupies that path, it goes to the current
    directory under its original name; if that is taken too, medlib stops
    and asks for --to. Existing files are never overwritten.

    Other symlinks to the file are left dangling. medlib warns about the
    ones it made, and 'medlib fsck' finds the rest.

    \b
    Examples:
      medlib remove cat.png
      medlib remove 42 --to ~/Desktop
    """
    if to is not None and len(files) > 1 and not os.path.isdir(to):
        raise click.UsageError("--to must be a directory when removing several files")
    lib = open_library()
    for ref in files:
        fid = pick_file(lib, ref)
        res = lib.remove(fid, to=to, cwd=os.getcwd())
        click.echo(f"removed file {res.file_id}; restored to {res.path}")
        for link in res.dangling:
            click.echo(f"  warning: link {link} now dangles (see 'medlib fsck')", err=True)


@file.command("tag", epilog=FILE_NOTE + " " + TAG_NOTE)
@click.argument("file_ref", metavar="FILE", shell_complete=complete_file)
@click.argument("tags", metavar="TAG...", nargs=-1, required=True, shell_complete=complete_tag)
@click.option("-y", "--yes", is_flag=True, help="Create missing tags without asking.")
def file_tag(file_ref, tags, yes):
    """Attach one or more tags to FILE.

    A tag that does not exist yet is created, after asking (--yes skips the
    question; when not run from a terminal the answer is no). Tags already
    on the file are ignored.

    Use the most specific tag: a file tagged 'siamese' is also found by a
    query for its parent tag 'cat'.

    \b
    Examples:
      medlib file tag cat.png cat funny
      medlib file tag --yes 42 reaction
    """
    lib = open_library()
    fid = pick_file(lib, file_ref)
    ensure_tags(lib, tags, yes)
    added = lib.file_tag(fid, tags)
    names = ", ".join(lib.tag_show(t).name for t in added) or "nothing new"
    click.echo(f"file {fid}: tagged {names}")


@file.command("untag", epilog=FILE_NOTE + " " + TAG_NOTE)
@click.argument("file_ref", metavar="FILE", shell_complete=complete_file)
@click.argument("tags", metavar="TAG...", nargs=-1, required=True, shell_complete=complete_tag)
def file_untag(file_ref, tags):
    """Detach one or more tags from FILE.

    Only the tags named are detached. Untagging 'cat' does not remove a
    'siamese' subtag, so the file may still be found by a query for 'cat'.
    The tags themselves are not affected.
    """
    lib = open_library()
    fid = pick_file(lib, file_ref)
    removed = lib.file_untag(fid, tags)
    names = ", ".join(lib.tag_show(t).name for t in removed) or "nothing"
    click.echo(f"file {fid}: untagged {names}")


@file.command(
    "retag",
    short_help="Swap one tag for another on files.",
    epilog="OLD and NEW are tag IDs or aliases; see 'medlib tag --help'. " + FILE_NOTE,
)
@click.argument("old", shell_complete=complete_tag)
@click.argument("new", shell_complete=complete_tag)
@click.argument("files", metavar="FILE...", nargs=-1, required=True, shell_complete=complete_file)
@click.option("-y", "--yes", is_flag=True, help="Create NEW without asking if it does not exist.")
def file_retag(old, new, files, yes):
    """Replace the tag OLD with the tag NEW on the files named.

    For putting right a batch that was tagged wrongly: of the FILEs given,
    those tagged OLD lose it and gain NEW. The rest are left exactly as they
    are and counted as skipped, so it is safe to name a whole directory that
    is only partly mistagged.

    Only a direct OLD tag counts; a file that merely has a subtag of OLD is
    skipped. Both tags continue to exist. To fold one tag into another for
    good, on every file, use 'medlib tag merge'.

    If NEW does not exist it is created, after asking.

    \b
    Examples:
      medlib file retag cat dog rex.png fido.png
      medlib file retag cat dog ~/memes/dogs/*
    """
    lib = open_library()
    lib.resolve_tag(old)  # fail on a mistyped OLD before offering to create NEW
    ensure_tags(lib, [new], yes)
    ids = [pick_file(lib, f) for f in files]
    retagged, skipped = lib.file_retag(old, new, ids)
    click.echo(f"retagged {len(retagged)} file(s) from '{old}' to '{new}'")
    if skipped:
        click.echo(f"skipped {len(skipped)} file(s) not tagged '{old}'")


@file.command("link", epilog=FILE_NOTE)
@click.argument("file_ref", metavar="FILE", shell_complete=complete_file)
@click.argument("path", type=click.Path())
def file_link(file_ref, path):
    """Create another symlink to FILE.

    PATH is the link to create, or an existing directory to create it in; in
    a directory the link is named after the file's first alias. Nothing is
    overwritten.

    medlib remembers links made this way (and the one 'add' leaves behind),
    so 'medlib fsck' can check them and 'file show' lists them. To link a
    whole set of files into a directory, use 'medlib query --output'.
    """
    lib = open_library()
    link = lib.file_link(pick_file(lib, file_ref), path)
    click.echo(f"linked {link}")


@file.command("alias", epilog=FILE_NOTE)
@click.argument("file_ref", metavar="FILE", shell_complete=complete_file)
@click.argument("alias")
def file_alias(file_ref, alias):
    """Give FILE an additional alias.

    ALIAS can be any text that is not all digits; quote it if it contains
    spaces. The file keeps its existing aliases, and other files may use the
    same one.

    \b
    Example:
      medlib file alias IMG_0042.jpg "grumpy cat"
    """
    lib = open_library()
    fid = pick_file(lib, file_ref)
    if lib.file_alias(fid, alias):
        click.echo(f"aliased file {fid} as '{alias}'")
    else:
        click.echo(f"file {fid} already has alias '{alias}'")


@file.command("unalias", epilog=FILE_NOTE)
@click.argument("file_ref", metavar="FILE", shell_complete=complete_file)
@click.argument("alias", shell_complete=complete_own_alias)
def file_unalias(file_ref, alias):
    """Remove ALIAS from FILE.

    A file may be left with no aliases at all; it can then only be referred
    to by its ID.
    """
    lib = open_library()
    fid = pick_file(lib, file_ref)
    lib.file_unalias(fid, alias)
    click.echo(f"file {fid}: dropped alias '{alias}'")


@file.command("show", epilog=FILE_NOTE)
@click.argument("file_ref", metavar="FILE", shell_complete=complete_file)
def file_show(file_ref):
    """Show everything known about FILE.

    \b
    Prints its ID, aliases, media type, size and SHA-256, then:
      duration   playing time; '-' for a still image, or if not measured
      pixels     width x height as displayed; '-' for audio
      original   the path it was added from (relative to the repository)
      blob       where the stored copy lives inside .medlib/
      tags       the tags attached directly to it
      links      the symlinks medlib has made to it
    """
    lib = open_library()
    f = lib.file_show(pick_file(lib, file_ref))
    rows = [
        ("id", f.id),
        ("aliases", ", ".join(f.aliases) or "-"),
        ("type", f.media_type),
        ("size", f"{f.size} ({human_size(f.size)})"),
        ("duration", "-" if f.duration is None else f"{human_duration(f.duration)} ({f.duration:.3f} s)"),
        ("pixels", "-" if f.width is None else f"{f.width} x {f.height}"),
        ("sha256", f.sha256),
        ("added", f.added_at),
        ("original", f.original_path or f.original_name),
        ("blob", lib.blob_path(f.id, f.ext)),
        ("tags", ", ".join(name for _, name in f.tags) or "-"),
        ("links", ", ".join(f.links) or "-"),
    ]
    for key, value in rows:
        click.echo(f"{key + ':':<10}{value}")


@file.command("list")
def file_list():
    """List all files, in ID order.

    \b
    Columns: ID, media type, size, first alias, [tags]

    The same as 'medlib query' with no expression; use 'query' to filter.
    """
    for f in open_library().file_list():
        click.echo(file_line(f))


# -- tag -------------------------------------------------------------------


@cli.group(cls=Group, help=TAG_HELP, short_help="Work with tags.")
def tag():
    pass


@tag.command("add")
@click.argument("aliases", metavar="ALIAS...", nargs=-1, required=True)
@click.option("--parent", "parents", metavar="TAG", multiple=True, shell_complete=complete_tag, help="Put the new tags under TAG; may be repeated.")
def tag_add(aliases, parents):
    """Create a new tag for each ALIAS.

    \b
    An alias must
      - contain at least one character that is not a digit
      - not start with '-'
      - not contain spaces or any of  : & | ! ( ) " '
      - not already belong to a tag (aliases ignore case)

    To add a second name to an existing tag, use 'tag alias' instead.

    \b
    Examples:
      medlib tag add cat dog
      medlib tag add siamese persian --parent cat
      medlib tag add shrek --parent ogre --parent dreamworks
    """
    lib = open_library()
    for alias in aliases:
        tid = lib.tag_add(alias, parents=parents)
        click.echo(f"created tag {tid} '{alias}'")


@tag.command("remove", short_help="Remove an alias, or a whole tag.", epilog=TAG_NOTE)
@click.argument("tag_ref", metavar="TAG", shell_complete=complete_tag_alias)
@click.option("--all", "all_", is_flag=True, help="Remove the tag itself, not just this alias.")
def tag_remove(tag_ref, all_):
    """Remove one alias of a tag, or the whole tag.

    \b
    What is removed depends on how the tag is named:
      medlib tag remove kitty         just the alias 'kitty'
      medlib tag remove --all kitty   the whole tag that 'kitty' names
      medlib tag remove 7             the whole tag with ID 7

    Removing an alias is refused if it is the tag's last one, since that
    would leave a tag with no name; use --all if you mean to remove the tag.

    Removing a whole tag detaches it from its files and moves its subtags up
    to each of its parents (they become root tags if it had none). Files
    themselves are never touched.
    """
    lib = open_library()
    tid, whole = lib.tag_remove(tag_ref, all=all_)
    if whole:
        click.echo(f"removed tag {tid}")
    else:
        click.echo(f"removed alias '{tag_ref}' from tag {tid}")


@tag.command("alias", epilog=TAG_NOTE)
@click.argument("tag_ref", metavar="TAG", shell_complete=complete_tag)
@click.argument("alias")
def tag_alias(tag_ref, alias):
    """Give TAG an additional alias.

    Afterwards both names refer to the same tag. ALIAS must follow the alias
    rules in 'medlib tag --help' and not be in use by another tag. To
    replace a name rather than add one, use 'tag rename'.
    """
    lib = open_library()
    lib.tag_alias(tag_ref, alias)
    click.echo(f"tag {lib.resolve_tag(tag_ref)}: added alias '{alias}'")


@tag.command("parent", epilog=TAG_NOTE + " So is PARENT.")
@click.argument("tag_ref", metavar="TAG", shell_complete=complete_tag)
@click.argument("parents", metavar="PARENT...", nargs=-1, required=True, shell_complete=complete_tag)
def tag_parent(tag_ref, parents):
    """Put TAG under one or more PARENT tags.

    A tag can have several parents. This adds to them and removes none; use
    'tag unparent' for that. TAG's own subtags stay beneath it. From then
    on, a query for PARENT also finds files tagged TAG.

    Refused if PARENT is TAG itself or sits anywhere below it, which would
    make a loop.

    \b
    Examples:
      medlib tag parent siamese cat
      medlib tag parent shrek ogre dreamworks
    """
    lib = open_library()
    for parent in parents:
        if lib.tag_parent(tag_ref, parent):
            click.echo(f"'{tag_ref}' is now under '{parent}'")
        else:
            click.echo(f"'{tag_ref}' was already under '{parent}'")


@tag.command("unparent", epilog=TAG_NOTE + " So is PARENT.")
@click.argument("tag_ref", metavar="TAG", shell_complete=complete_tag)
@click.argument("parents", metavar="[PARENT...]", nargs=-1, shell_complete=complete_tag)
def tag_unparent(tag_ref, parents):
    """Take TAG out from under PARENT.

    With no PARENT, TAG is removed from under all its parents and becomes a
    root tag. Either way its own subtags stay beneath it.

    \b
    Examples:
      medlib tag unparent shrek dreamworks    # still under ogre
      medlib tag unparent shrek               # no parents at all
    """
    lib = open_library()
    if parents:
        for parent in parents:
            lib.tag_unparent(tag_ref, parent)
            click.echo(f"'{tag_ref}' is no longer under '{parent}'")
    else:
        lib.tag_unparent(tag_ref)
        click.echo(f"'{tag_ref}' is now a root tag")


@tag.command("rename")
@click.argument("tag_ref", metavar="ALIAS", shell_complete=complete_tag_alias)
@click.argument("new_alias", metavar="NEW")
def tag_rename(tag_ref, new_alias):
    """Change one of a tag's aliases from ALIAS to NEW.

    Only that one name changes: the tag keeps its other aliases, its files
    and its place in the tree. NEW must follow the alias rules in 'medlib
    tag --help'. Renaming can also just change capitalisation.

    A tag ID may be given in place of ALIAS, but only for a tag with a
    single alias; otherwise medlib cannot tell which one to replace.

    \b
    Examples:
      medlib tag rename kitteh kitty
      medlib tag rename cat Cat
    """
    open_library().tag_rename(tag_ref, new_alias)
    click.echo(f"renamed '{tag_ref}' to '{new_alias}'")


@tag.command("merge", epilog="SRC and DST are tag IDs or aliases; see 'medlib tag --help'.")
@click.argument("src", shell_complete=complete_tag)
@click.argument("dst", shell_complete=complete_tag)
def tag_merge(src, dst):
    """Merge the tag SRC into the tag DST.

    DST receives SRC's files, aliases and subtags, goes under SRC's parents
    as well as its own, and SRC ceases to exist. Because the aliases move
    too, SRC's old names keep working and now mean DST. This cannot be
    undone, short of restoring a database backup.

    Refused if DST is a subtag of SRC; merge the other way round, or
    'tag unparent' DST first. 'medlib administer vacuum' suggests pairs
    worth merging.

    \b
    Example:
      medlib tag merge kitty cat      # 'kitty' is now another name for cat
    """
    did = open_library().tag_merge(src, dst)
    click.echo(f"merged '{src}' into tag {did}")


def csv_join(values) -> str:
    """Comma-separated, CSV-quoted where a value needs it."""
    out = []
    for v in values:
        if any(c in v for c in ',"\n'):
            v = '"' + v.replace('"', '""') + '"'
        out.append(v)
    return ",".join(out)


@tag.command("list", short_help="Print the tag tree.")
@click.option("--depth", type=click.IntRange(min=1), metavar="N", help="Show at most N levels.")
@click.option("--start", metavar="TAG", shell_complete=complete_tag, help="Show only this tag and what is below it.")
def tag_list(depth, start):
    """Print the tag hierarchy as a tree.

    \b
      animal [1] (0)
      ├── cat,kitty [2] (12)
      │   └── siamese [4] (3)
      └── dog [3] (1)
      dreamworks [5] (2)
      └── shrek [7] (9)
      ogre [6] (0)
      └── shrek [7] (9)

    Each line is a tag: its aliases, its ID in brackets, and in parentheses
    the number of files tagged with it directly (files on its subtags are
    not counted; 'tag show' gives both figures). Aliases are separated by
    commas, and one that itself contains a comma is wrapped in double quotes,
    as in CSV. Root tags start at the left margin. A tag with several
    parents appears under each of them, as 'shrek' does above.

    --depth counts levels as 'tree -L' does. Without --start, --depth 1
    shows only root tags. With --start, the start tag is level 0, so
    --depth 1 shows it and its immediate subtags.
    """
    lib = open_library()
    children: Dict[Optional[int], List[TagInfo]] = {}
    for t in lib.tag_list():
        for parent_id in t.parent_ids or [None]:
            children.setdefault(parent_id, []).append(t)
    for kids in children.values():
        kids.sort(key=lambda t: t.name.casefold())

    def label(t: TagInfo) -> str:
        return f"{csv_join(t.aliases)} [{t.id}] ({t.file_count})"

    def walk(parent: Optional[int], prefix: str, level: int) -> None:
        if depth is not None and level > depth:
            return
        kids = children.get(parent, [])
        for i, t in enumerate(kids):
            last = i == len(kids) - 1
            click.echo(f"{prefix}{'└── ' if last else '├── '}{label(t)}")
            walk(t.id, prefix + ("    " if last else "│   "), level + 1)

    if start is not None:
        root = lib.tag_show(lib.resolve_tag(start))
        click.echo(label(root))
        walk(root.id, "", 1)
        return
    for t in children.get(None, []):
        if depth is None or depth >= 1:
            click.echo(label(t))
            walk(t.id, "", 2)


# 'tag tree' still works, as a hidden synonym for 'tag list'.
_tag_tree = copy.copy(tag_list)
_tag_tree.hidden = True
tag.add_command(_tag_tree, "tree")


@tag.command("show", epilog=TAG_NOTE)
@click.argument("tag_ref", metavar="TAG", shell_complete=complete_tag)
def tag_show(tag_ref):
    """Show one tag: ID, aliases, parents, subtags and file counts.

    Two file counts are given: files tagged with this tag directly, and
    files tagged with it or any tag beneath it (what a query would find).
    """
    lib = open_library()
    t = lib.tag_show(lib.resolve_tag(tag_ref))
    kids = [c for c in lib.tag_list() if t.id in c.parent_ids]
    parents = ", ".join(lib.tag_show(p).name for p in t.parent_ids) or "-"
    rows = [
        ("id", t.id),
        ("aliases", ", ".join(t.aliases)),
        ("parents", parents),
        ("children", ", ".join(c.name for c in kids) or "-"),
        ("files", f"{t.file_count} direct, {lib.tag_file_count(t.id)} with subtags"),
    ]
    for key, value in rows:
        click.echo(f"{key + ':':<10}{value}")


# -- query / fsck ------------------------------------------------------------


@cli.command(short_help="Find files by tag, type or alias.")
@click.argument("expression", nargs=-1)
@click.option("-o", "--output", metavar="DIR", type=click.Path(file_okay=False), help="Also symlink the matches into DIR (created if needed).")
@click.option("-q", "--ids", is_flag=True, help="Print only file IDs, one per line.")
def query(expression, output, ids):
    """Find files matching EXPRESSION; with no expression, list all files.

    \b
    Terms:
      cat, tag:cat     tagged 'cat' or any of its subtags
      tag=cat          tagged 'cat' itself, not through a subtag
      type:KIND        image, video, audio; an extension such as gif;
                       or a full media type such as image/png
      alias:REGEX      some alias matches the regular expression
                       (ignores case; matches anywhere unless anchored)
      duration:<10     plays for under 10 seconds; also <= > >= and the
                       range 5..30. Write times as 90, 2.5, 1m30 or 1:30.
                       A bare duration:10 means within half a second.
      width:>=1920     pixels across, as displayed; same comparisons
      height:<500      pixels down
      untagged         has no tags at all

    \b
    Operators, tightest first:
      !a               not a
      a & b,  a b      both (a space means the same as &)
      a | b            either
      ( )              grouping

    So 'a | b & !c' means 'a | (b & (!c))'.

    A value may be quoted with ' or "; inside quotes everything is literal,
    and the quote character itself is written twice. Quote a regular
    expression that contains any operator character, and quote a tag
    literally named 'untagged'. Naming a tag that does not exist is an
    error, not an empty result.

    A still image has no duration and audio has no width or height; such a
    file fails every comparison on the missing value, so '!duration:<10'
    finds stills as well as long clips. An animated GIF or WebP has a
    duration, which makes 'type:image duration:>0' a test for animation.

    Put the whole expression in single quotes so the shell leaves it alone:

    \b
      medlib query 'cat & !siamese'
      medlib query '(shrek | ogre) type:gif'
      medlib query 'alias:"^IMG_\\d+" | untagged'
      medlib query 'type:video duration:<10 width:>=1280'
      medlib query -q 'type:video' | wc -l

    Output is one file per line, in ID order: ID, media type, size, first
    alias, [tags].

    With --output, each match is also symlinked into DIR under its first
    alias, which is handy for browsing results in a file manager. These
    links are not tracked by medlib; delete the directory when done.
    """
    lib = open_library()
    found = lib.query(" ".join(expression))
    for f in found:
        click.echo(f.id if ids else file_line(f))
    if output is not None:
        made = lib.export_links([f.id for f in found], output)
        click.echo(f"linked {len(made)} file(s) into {output}", err=True)


@cli.command()
@click.option("--repair", is_flag=True, help="Fix what can be fixed without losing anything.")
@click.option("--prune", is_flag=True, help="Forget lost links and delete dangling symlinks.")
@click.option("--verify", is_flag=True, help="Also re-read every stored file and check its hash (slow).")
def fsck(repair, prune, verify):
    """Check the repository for inconsistencies.

    With no options nothing is changed: each problem found is reported on
    its own line, or 'ok' if there are none.

    \b
    Problem          Meaning                                  Fixed by
    missing-blob     a stored file has gone                   --repair *
    corrupt-blob     a stored file has the wrong size         (manual)
                     (or content, with --verify)
    orphan-blob      a file in .medlib/files that the         (manual)
                     database does not know about
    stale-temp       leftover from an interrupted 'add'       --repair
    unlinked-copy    an interrupted 'add' left a copy         --repair
                     where the symlink should be
    missing-link     a symlink medlib made was deleted        --repair (recreates)
                                                              or --prune (forgets)
    replaced-link    something else now sits where a          --prune
                     symlink was                              (forgets it)
    dangling-link    a symlink to a file since removed        --prune
                     from the library                         (deletes it)
    tag-no-alias     a tag with no name                       (manual)
    schema-version   .medlib/schema_version is wrong          --repair

    * only if an identical copy of the file can still be found, for example
    at the path it was added from.

    --repair never discards anything of yours. --prune deletes dangling
    symlinks and forgets links that are gone; it never touches stored files.

    Exits with status 1 if any problem remains unfixed, 0 otherwise.
    """
    issues = open_library().fsck(repair=repair, prune=prune, verify=verify)
    for i in issues:
        click.echo(f"{'fixed' if i.fixed else 'ISSUE':<6}{i.kind}: {i.message}")
    open_count = sum(1 for i in issues if not i.fixed)
    if not issues:
        click.echo("ok")
    elif open_count:
        click.echo(f"{open_count} issue(s) remain", err=True)
        sys.exit(1)


@cli.command()
@click.argument("shell", metavar="[SHELL]", type=click.Choice(["zsh"]), default="zsh")
def completion(shell):
    """Print the shell completion script (zsh only, for now).

    To enable completion, add this to ~/.zshrc after the 'compinit' line:

    \b
      eval "$(medlib completion zsh)"

    Tab then completes commands, options, tag aliases and file aliases, and
    for 'administer backup remove' the backup versions and timestamps. At
    most 50 candidates are offered at a time. IDs are not completed, and
    nothing is completed inside a query expression.

    When everything that matches is an alias of one and the same tag (or
    file), only the oldest of those aliases is offered, so Tab completes it
    straight away. 'tag remove' and 'tag rename' act on the alias named and
    still offer them all.
    """
    cls = get_completion_class(shell)
    click.echo(cls(cli, {}, "medlib", "_MEDLIB_COMPLETE").source())


# -- administer ----------------------------------------------------------------


@cli.group(cls=Group)
def administer():
    """Repository maintenance.

    \b
      rebuild   after upgrading medlib, bring the database up to date
      backup    save, list and delete copies of the database
      vacuum    find tags similar enough to be worth merging

    For consistency checks see 'medlib fsck'.
    """


@administer.command("rebuild", short_help="Upgrade the database schema.")
def administer_rebuild():
    """Bring the database up to date and rebuild its indices.

    Run this when medlib says the repository's schema is out of date, which
    happens after upgrading medlib. It applies each pending schema change in
    turn, adds any table, column or index that is missing, then rebuilds the
    indices and compacts the database file.

    It also measures the duration and dimensions of every file that has not
    been measured yet: those added before medlib recorded them, or while
    ffprobe (part of ffmpeg) was not installed. That reads each such file
    once, so the first run on a large library can take a few minutes.

    If the schema needs upgrading, the database is backed up first (see
    'medlib administer backup'). Stored files are not changed. Running it on
    an up-to-date repository is harmless.
    """
    show = sys.stderr.isatty()

    def progress(done: int, total: int) -> None:
        if show:
            end = "\n" if done == total else ""
            click.echo(f"\rmeasuring files: {done}/{total}{end}", nl=False, err=True)

    with Library.find(os.getcwd(), check_schema=False) as lib:
        for action in lib.rebuild(progress):
            click.echo(action)
        click.echo(f"schema version {lib.schema_version()}")


def parse_version(value: str) -> int:
    """Accepts '2' or 'v2'."""
    digits = value[1:] if value[:1] in ("v", "V") else value
    if not is_id(digits):
        raise click.BadParameter(f"'{value}' is not a schema version (expected e.g. v2)")
    return int(digits)


def _backups(version=None):
    with Library.find(os.getcwd(), check_schema=False) as lib:
        return lib.backup_list(version)


def complete_backup_version(ctx, param, incomplete):
    try:
        names = sorted({f"v{b.version}" for b in _backups()})
        return [CompletionItem(n) for n in names if n.startswith(incomplete)]
    except Exception:
        return []


def complete_backup_timestamp(ctx, param, incomplete):
    try:
        version = parse_version(ctx.params["version"])
        stamps = [b.timestamp for b in _backups(version)][-COMPLETE_LIMIT:]
        return [CompletionItem(t) for t in stamps if t.startswith(incomplete)]
    except Exception:
        return []


@administer.group("backup", cls=Group, short_help="Database backups.")
def administer_backup():
    """Save, list and delete backups of the database.

    A backup is a copy of the database: files' aliases and tags, the tag
    tree, and the record of links. It does NOT contain the media files
    themselves, which stay in .medlib/files.

    \b
    Backups are kept at
      .medlib/backups/v<schema version>/<timestamp>.bak
    with timestamps in UTC, for example
      .medlib/backups/v2/20261112T014522Z.bak

    'administer rebuild' makes one automatically before a schema upgrade.

    There is no restore command. To restore by hand, with nothing else
    using the repository, copy the backup over .medlib/medlib.db and delete
    medlib.db-wal and medlib.db-shm if they exist. A backup from an older
    schema then needs 'medlib administer rebuild'.
    """


@administer_backup.command("create")
def backup_create():
    """Back up the database now.

    Prints the new backup's schema version and timestamp, which are what
    'backup remove' takes.
    """
    with Library.find(os.getcwd(), check_schema=False) as lib:
        b = lib.backup_create()
    click.echo(f"created backup v{b.version} {b.timestamp} ({human_size(b.size)})")


@administer_backup.command("list")
@click.option("--version", metavar="VERSION", shell_complete=complete_backup_version, help="Only backups of this schema version (v2 or 2).")
def backup_list(version):
    """List backups, oldest first within each schema version.

    \b
    Columns: schema version, timestamp (UTC), size
    """
    for b in _backups(parse_version(version) if version is not None else None):
        click.echo(f"v{b.version:<4} {b.timestamp}  {human_size(b.size):>8}")


@administer_backup.command("remove")
@click.argument("version", shell_complete=complete_backup_version)
@click.argument("timestamp", shell_complete=complete_backup_timestamp)
def backup_remove(version, timestamp):
    """Delete one backup, permanently.

    VERSION and TIMESTAMP are the first two columns of 'backup list'.
    VERSION may be written 'v2' or '2'.

    \b
    Example:
      medlib administer backup remove v2 20261112T014522Z
    """
    with Library.find(os.getcwd(), check_schema=False) as lib:
        lib.backup_remove(parse_version(version), timestamp)
    click.echo(f"removed backup v{parse_version(version)} {timestamp}")


@administer.command("vacuum", short_help="Find similar tags to merge.")
@click.option("-n", "--limit", metavar="N", default=20, show_default=True, help="Show at most N pairs.")
@click.option("--min-score", metavar="X", default=0.0, show_default=True, help="Hide pairs scoring below X (0 to 1).")
@click.option("--min-shared", metavar="N", default=1, show_default=True, help="Hide pairs sharing fewer than N files.")
def administer_vacuum(limit, min_score, min_shared):
    """List pairs of tags that mark nearly the same files.

    Such pairs are often one idea under two names, and candidates for
    'medlib tag merge'. This command only reports; it changes nothing.

    \b
      score  overlap  shared  tags (files)
       0.83     100%       5  shrek (5)  ~  ogre (5)
       0.13      21%       3  cat (12)  ~  shrek (5)

    \b
    Columns:
      overlap   files with both tags, as a share of files with either
      shared    the number of files with both tags
      (files)   how many files each tag has
      score     the overlap, marked down when it rests on few files;
                pairs are listed by score, highest first

    The score is why five files sharing two tags outrank a single file
    sharing two tags, though both overlap 100%. And a small tag that sits
    entirely inside a large one scores low, because most of the large tag's
    files lack the small one.

    A tag's files include those of its subtags. Overlap that the hierarchy
    itself creates is ignored: a tag is never paired with a tag above or
    below it, and files that two tags share only through a common subtag
    do not count towards that pair.
    """
    pairs = open_library().similar_tags(limit=limit, min_score=min_score, min_shared=min_shared)
    if not pairs:
        click.echo("no similar tags")
        return
    click.echo(f"{'score':>5}  {'overlap':>7}  {'shared':>6}  tags (files)")
    for p in pairs:
        click.echo(
            f"{p.score:5.2f}  {p.jaccard:7.0%}  {p.shared:>6}  "
            f"{p.a.name} ({p.count_a})  ~  {p.b.name} ({p.count_b})"
        )
    top = pairs[0]
    small, big = (top.a, top.b) if top.count_a <= top.count_b else (top.b, top.a)
    click.echo(f"\nto merge the top pair: medlib tag merge {small.name} {big.name}")


cli.add_command(file_add, "add")
cli.add_command(file_remove, "remove")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
