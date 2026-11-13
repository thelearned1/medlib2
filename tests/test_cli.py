import os

import pytest
from click.testing import CliRunner

from medlib.cli import cli
from medlib.migrations import SCHEMA_VERSION as V

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def run(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    runner = CliRunner()

    def invoke(*args, ok=True):
        result = runner.invoke(cli, [str(a) for a in args])
        if ok:
            assert result.exit_code == 0, result.output
        else:
            assert result.exit_code != 0, result.output
        return result.output

    invoke("init")
    return invoke


def test_end_to_end(run):
    with open("cat.png", "wb") as f:
        f.write(PNG)
    with open("copy.png", "wb") as f:
        f.write(PNG)

    assert "as file 1" in run("add", "cat.png")
    assert os.path.islink("cat.png")

    # duplicate: without --yes and without a terminal the alias is skipped
    out = run("add", "copy.png")
    assert "duplicate of file 1" in out and "skipped" in out
    out = run("add", "--yes", "copy.png")
    assert "aliased file 1 as 'copy.png'" in out

    run("tag", "add", "animal")
    run("tag", "add", "cat", "--parent", "animal")
    run("file", "tag", "cat.png", "cat")
    run("file", "tag", "1", "newtag", ok=False)  # missing tag, no --yes
    assert "created tag 'meme'" in run("file", "tag", "--yes", "1", "meme")

    assert "cat.png" in run("query", "animal")
    assert run("query", "animal", "--ids").strip() == "1"
    assert run("query", "animal & !cat") == ""
    assert "animal" in run("tag", "tree")
    assert "1 with subtags" in run("tag", "show", "animal")
    assert "image/png" in run("file", "show", "1")

    run("query", "type:image", "--output", "out")
    assert os.path.islink("out/cat.png")

    assert run("fsck").strip() == "ok"
    run("file", "untag", "1", "cat")
    assert run("query", "animal") == ""

    run("remove", "1")
    assert os.path.isfile("cat.png") and not os.path.islink("cat.png")
    # out/cat.png was an untracked link and now dangles
    assert "dangling-link" in run("fsck", ok=False)
    run("fsck", "--prune")
    assert run("fsck").strip() == "ok"


def test_errors_are_clean(run):
    assert "Error:" in run("file", "show", "nope", ok=False)
    assert "Error:" in run("tag", "add", "bad:alias", ok=False)
    assert "Error:" in run("init", ok=False)


def test_outside_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["file", "list"])
    assert result.exit_code != 0 and "medlib init" in result.output


def complete(*words):
    from click.shell_completion import ShellComplete

    comp = ShellComplete(cli, {}, "medlib", "_MEDLIB_COMPLETE")
    return [c.value for c in comp.get_completions(list(words[:-1]), words[-1])]


def test_completion(run, monkeypatch):
    import medlib.cli

    for name in ("cat.png", "car.png"):
        with open(name, "wb") as f:
            f.write(PNG + name.encode())
    run("add", "cat.png", "car.png")
    run("tag", "add", "animal", "anime", "dog")
    run("file", "tag", "cat.png", "animal")

    assert complete("file", "show", "") == ["car.png", "cat.png"]
    assert complete("remove", "CAT") == ["cat.png"]
    assert complete("file", "tag", "cat.png", "an") == ["animal", "anime"]
    assert complete("file", "tag", "cat.png", "animal", "an") == ["anime"]
    assert complete("file", "untag", "cat.png", "") == ["animal"]
    assert complete("file", "unalias", "cat.png", "") == ["cat.png"]
    assert complete("tag", "add", "x", "--parent", "d") == ["dog"]
    assert "tag" in complete("t")

    monkeypatch.setattr(medlib.cli, "COMPLETE_LIMIT", 1)
    assert complete("tag", "show", "") == ["animal"]


def test_completion_outside_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert complete("file", "show", "") == []


def test_completion_script(run):
    assert "#compdef medlib" in run("completion", "zsh")


def test_administer(run):
    assert os.path.isfile(".medlib/schema_version")
    assert f"schema version {V}" in run("administer", "rebuild")
    assert "no similar tags" in run("administer", "vacuum")
    for i in range(3):
        with open(f"m{i}.png", "wb") as f:
            f.write(PNG + bytes([i]))
        run("add", f"m{i}.png")
        run("file", "tag", "--yes", f"m{i}.png", "shrek", "ogre")
    out = run("administer", "vacuum")
    assert "shrek (3)  ~  ogre (3)" in out and "medlib tag merge" in out


def test_backup_commands(run):
    out = run("administer", "backup", "create")
    stamp = out.split()[3]
    assert out.startswith(f"created backup v{V} ")
    assert stamp in run("administer", "backup", "list")
    assert stamp in run("administer", "backup", "list", "--version", f"v{V}")
    assert run("administer", "backup", "list", "--version", "1") == ""
    assert complete("administer", "backup", "remove", "") == [f"v{V}"]
    assert complete("administer", "backup", "remove", f"v{V}", "") == [stamp]
    run("administer", "backup", "remove", "v9", stamp, ok=False)
    run("administer", "backup", "remove", f"v{V}", stamp)
    assert run("administer", "backup", "list") == ""


def test_query_expression_and_tag_list(run):
    with open("a.png", "wb") as f:
        f.write(PNG)
    run("add", "a.png")
    run("tag", "add", "animal")
    run("tag", "add", "cat", "dog", "--parent", "animal")
    run("tag", "add", "siamese", "--parent", "cat")
    run("tag", "alias", "cat", "kitty")
    run("tag", "alias", "cat", "a,b")
    run("tag", "add", "meme")
    run("file", "tag", "a.png", "siamese")

    assert run("query", "-q", "cat", "type:png").strip() == "1"  # words are joined
    assert run("query", "-q", "tag=cat | dog") == ""
    assert "Error: query:" in run("query", "cat &", ok=False)
    assert "Error: no tag" in run("query", "nope", ok=False)

    assert run("tag", "list") == (
        "animal [1] (0)\n"
        '├── cat,kitty,"a,b" [2] (0)\n'
        "│   └── siamese [4] (1)\n"
        "└── dog [3] (0)\n"
        "meme [5] (0)\n"
    )
    assert run("tag", "list", "--depth", "1") == "animal [1] (0)\nmeme [5] (0)\n"
    assert run("tag", "list", "--start", "kitty") == (
        'cat,kitty,"a,b" [2] (0)\n└── siamese [4] (1)\n'
    )
    assert "siamese" not in run("tag", "list", "--start", "animal", "--depth", "1")
    assert "cat" in run("tag", "list", "--start", "animal", "--depth", "1")
    assert run("tag", "tree") == run("tag", "list")


def all_commands(group=cli, path=("medlib",)):
    yield path, group
    for name, cmd in getattr(group, "commands", {}).items():
        yield from all_commands(cmd, path + (name,))


def test_help_pages_render_and_are_complete():
    runner = CliRunner()
    for path, cmd in all_commands():
        result = runner.invoke(cli, list(path[1:]) + ["--help"])
        where = " ".join(path)
        assert result.exit_code == 0, where
        assert cmd.help and len(cmd.help.strip()) > 20, f"{where} has no real help"
        # no summary cut off with "..." in a command list
        assert "..." not in result.output.partition("\nCommands:\n")[2], where
        for line in result.output.splitlines():
            assert len(line) <= 80, f"{where}: line too long: {line!r}"
    listed = runner.invoke(cli, ["tag", "--help"]).output.split("Commands:")[1]
    assert "\n  tree " not in listed  # hidden synonym of 'tag list'
    assert runner.invoke(cli, ["tag", "tree", "--help"]).exit_code == 0


def test_add_with_tags(run):
    for name in ("a.png", "b.png", "dup.png", "c.png"):
        with open(name, "wb") as f:
            f.write(PNG + (b"a" if name == "dup.png" else name.encode()[:1]))
    run("tag", "add", "cat")

    # a missing tag stops everything before any file is added (no --yes, no terminal)
    out = run("add", "--tag", "cat", "--tag", "newtag", "a.png", ok=False)
    assert "no tag with alias 'newtag'" in out and not os.path.islink("a.png")

    out = run("add", "-y", "-t", "cat", "-t", "newtag", "-t", "third", "a.png", "b.png")
    assert "created tag 'newtag'" in out and "file 1: tagged cat, newtag, third" in out
    assert run("query", "-q", "cat & newtag & third").split() == ["1", "2"]

    # a duplicate is not added, but the existing file gets the tags
    out = run("add", "-y", "--tag", "extra", "dup.png")
    assert "duplicate of file 1" in out and "file 1: tagged extra" in out
    assert run("query", "-q", "extra").split() == ["1"]

    assert complete("add", "--tag", "ne") == ["newtag"]
    run("add", "c.png")  # still fine without --tag
    assert run("query", "-q", "untagged").split() == ["3"]


def test_tag_with_two_parents_in_cli(run):
    run("tag", "add", "ogre", "dreamworks")
    run("tag", "add", "shrek", "--parent", "ogre", "--parent", "dreamworks")
    run("tag", "add", "donkey")
    assert "now under 'shrek'" in run("tag", "parent", "donkey", "shrek")
    assert "already under" in run("tag", "parent", "donkey", "shrek")
    assert run("tag", "list") == (
        "dreamworks [2] (0)\n"
        "└── shrek [3] (0)\n"
        "    └── donkey [4] (0)\n"
        "ogre [1] (0)\n"
        "└── shrek [3] (0)\n"
        "    └── donkey [4] (0)\n"
    )
    assert "ogre, dreamworks" in run("tag", "show", "shrek")
    run("tag", "parent", "ogre", "donkey", ok=False)  # loop
    assert "no longer under 'ogre'" in run("tag", "unparent", "shrek", "ogre")
    assert run("tag", "list", "--depth", "1") == "dreamworks [2] (0)\nogre [1] (0)\n"
    assert "root tag" in run("tag", "unparent", "shrek")
    assert run("tag", "list", "--depth", "1").count("\n") == 3


def test_file_retag(run):
    os.mkdir("dogs")
    for name in ("a.png", "b.png", "dogs/c.png", "dogs/d.png", "dogs/e.png"):
        with open(name, "wb") as f:
            f.write(PNG + name.encode())
    run("add", "-y", "-t", "cat", "a.png", "b.png", "dogs/c.png", "dogs/d.png")
    run("add", "-y", "-t", "bird", "dogs/e.png")
    run("tag", "add", "siamese", "--parent", "cat")

    # the "oops" case: a directory glob, only partly mistagged
    out = run("file", "retag", "--yes", "cat", "dog", "dogs/c.png", "dogs/d.png", "dogs/e.png")
    assert "created tag 'dog'" in out
    assert "retagged 2 file(s) from 'cat' to 'dog'" in out
    assert "skipped 1 file(s) not tagged 'cat'" in out
    assert run("query", "-q", "dog").split() == ["3", "4"]
    assert run("query", "-q", "tag=cat").split() == ["1", "2"]
    assert run("query", "-q", "bird").split() == ["5"]

    # by alias and ID; a subtag of OLD does not count as OLD
    run("file", "retag", "cat", "siamese", "a.png")
    out = run("file", "retag", "cat", "dog", "1", "b.png")
    assert "retagged 1 file(s)" in out and "skipped 1 file(s)" in out
    assert run("query", "-q", "tag=siamese").split() == ["1"]

    assert "Missing argument" in run("file", "retag", "cat", "dog", ok=False)  # files required
    run("file", "retag", "cat", "feline", "b.png", ok=False)  # NEW missing, no --yes
    run("file", "retag", "nope", "dog", "b.png", ok=False)
    run("file", "retag", "dog", "dog", "b.png", ok=False)
    assert complete("file", "retag", "si") == ["siamese"]
    assert complete("file", "retag", "cat", "do") == ["dog"]
    assert complete("file", "retag", "cat", "dog", "b") == ["b.png"]


def test_files_can_be_named_by_symlink_path(run):
    os.mkdir("x")
    os.mkdir("y")
    for name in ("x/same.png", "y/same.png"):
        with open(name, "wb") as f:
            f.write(PNG + name.encode())
    run("add", "x/same.png", "y/same.png")
    # the alias 'same.png' is ambiguous, the paths are not
    run("file", "show", "same.png", ok=False)
    assert "id:       2" in run("file", "show", "y/same.png")
    run("file", "tag", "--yes", "x/same.png", "first")
    assert run("query", "-q", "first").split() == ["1"]
    os.chdir("y")
    assert "id:       2" in run("file", "show", "same.png")  # the link right here wins
    os.chdir("..")
    # an ordinary symlink that is not medlib's is not a file reference
    os.symlink("x/same.png", "indirect.png")
    os.symlink("/etc/hosts", "hosts")
    run("file", "show", "hosts", ok=False)


def test_measurements_in_cli(run, media, monkeypatch):
    import shutil

    for name in ("anim.gif", "turned.mp4"):
        shutil.copy(str(media / name), name)
    out = run("add", "anim.gif", "turned.mp4")
    assert "ffprobe" not in out
    shown = run("file", "show", "turned.mp4")
    assert "duration: 3.0s (3.000 s)" in shown and "pixels:   48 x 64" in shown
    assert run("query", "-q", "duration:<2.5").split() == ["1"]
    assert run("query", "-q", "height:>width", ok=False)  # values are numbers
    assert "bad duration 'soon'" in run("query", "duration:soon", ok=False)

    monkeypatch.setenv("MEDLIB_FFPROBE", "")
    with open("late.png", "wb") as f:
        f.write(PNG)
    out = run("add", "late.png")
    assert "added late.png as file 3" in out
    assert "ffprobe not found, so 1 file(s)" in out
    assert "duration: -" in run("file", "show", "late.png")


def test_completion_of_aliases_of_one_tag(run):
    run("tag", "add", "cat")
    run("tag", "alias", "cat", "catto")
    with open("x.png", "wb") as f:
        f.write(PNG)
    run("add", "x.png")
    assert complete("file", "tag", "x.png", "ca") == ["cat"]
    assert complete("query", "") == []  # no completion inside expressions
    # these act on the alias itself, so every alias is offered
    assert complete("tag", "remove", "ca") == ["cat", "catto"]
    assert complete("tag", "rename", "ca") == ["cat", "catto"]
