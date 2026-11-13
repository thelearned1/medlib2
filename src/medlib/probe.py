"""Duration and dimensions of media files, measured with ffprobe (ffmpeg).

ffprobe is optional. Without it nothing is measured and the file stays
unprobed, to be picked up later by ``medlib administer rebuild``. Set the
environment variable ``MEDLIB_FFPROBE`` to use a particular binary; set it to
the empty string to turn probing off.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

TIMEOUT = 120  # seconds; only animated images are read in full

_ENTRIES = (
    "format=duration"
    ":stream=codec_type,width,height"
    ":stream_disposition=attached_pic"
    ":stream_side_data=rotation"
)


@dataclass
class Measurements:
    """What was measured. A field is None when it does not apply (a still
    image has no duration, audio has no size) or could not be read."""

    duration: Optional[float] = None  # seconds
    width: Optional[int] = None  # pixels, as displayed (rotation applied)
    height: Optional[int] = None


def ffprobe_path() -> Optional[str]:
    override = os.environ.get("MEDLIB_FFPROBE")
    if override is not None:
        return shutil.which(override) if override else None
    return shutil.which("ffprobe")


def wants_probe(media_type: str) -> bool:
    return media_type.split("/")[0] in ("image", "video", "audio")


def _number(value: object) -> Optional[float]:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n == n and n >= 0 else None  # drops NaN and negatives


def probe(path: Union[str, Path], media_type: str) -> Optional[Measurements]:
    """Measure ``path``. Returns None if it could not be probed now (no
    ffprobe, or it did not finish) and should be tried again later. A file
    ffprobe cannot make sense of yields empty measurements.

    Durations come from the container's header, so they are instant to read
    but only as good as the file. Images are the exception: every frame is
    read, both to tell an animation from a still and to time it.
    """
    exe = ffprobe_path()
    if exe is None:
        return None
    image = media_type.startswith("image/")
    entries = _ENTRIES + (":packet=pts_time,duration_time" if image else "")
    try:
        done = subprocess.run(
            [exe, "-v", "error", "-show_entries", entries, "-of", "json", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        info = json.loads(done.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        info = {}
    if not isinstance(info, dict):
        info = {}

    found = Measurements()
    for stream in info.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue  # cover art of an audio file
        width, height = _number(stream.get("width")), _number(stream.get("height"))
        if not width or not height:
            continue
        for side in stream.get("side_data_list") or []:
            try:
                turn = round(float(side.get("rotation", 0)))
            except (TypeError, ValueError):
                continue
            if turn % 180 == 90:
                width, height = height, width
        found.width, found.height = int(width), int(height)
        break
    if media_type.startswith("audio/"):
        found.width = found.height = None

    duration = _number((info.get("format") or {}).get("duration"))
    if image:
        packets = info.get("packets") or []
        if len(packets) < 2:
            duration = None  # a still, whatever the header claims
        elif duration is None:  # e.g. animated WebP: add up the frames
            ends = [
                (_number(p.get("pts_time")) or 0.0) + (_number(p.get("duration_time")) or 0.0)
                for p in packets
            ]
            duration = max(ends) or None
    found.duration = duration
    return found
