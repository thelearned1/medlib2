import shutil
import subprocess

import pytest


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch):
    """Most tests add fake files, which there is no point measuring: turn
    ffprobe off. Tests that want it ask for the ``media`` fixture."""
    monkeypatch.setenv("MEDLIB_FFPROBE", "")


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="session")
def media_files(tmp_path_factory):
    """Small real media files, made once with ffmpeg."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("needs ffmpeg and ffprobe")
    d = tmp_path_factory.mktemp("media")
    picture = ["-f", "lavfi", "-i", "testsrc=s=64x48:r=10"]
    _ffmpeg(*picture, "-t", "2", str(d / "anim.gif"))  # 20 frames, 2 s
    _ffmpeg(*picture, "-frames:v", "1", str(d / "still.gif"))
    _ffmpeg(*picture, "-frames:v", "1", str(d / "still.png"))
    _ffmpeg(*picture, "-t", "3", str(d / "clip.mkv"))
    # the same clip, flagged to be shown turned a quarter: 48 wide, 64 high
    _ffmpeg("-display_rotation", "90", "-i", str(d / "clip.mkv"), "-c", "copy", str(d / "turned.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "sine=d=4", str(d / "tone.wav"))
    return d


@pytest.fixture
def media(media_files, monkeypatch):
    """Real media files, with ffprobe switched back on."""
    monkeypatch.delenv("MEDLIB_FFPROBE")
    return media_files
