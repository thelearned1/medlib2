"""Media type detection: magic bytes first, file extension as fallback."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Union

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}


def _from_magic(h: bytes) -> Union[str, None]:
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if h[:4] == b"RIFF" and len(h) >= 12:
        kind = h[8:12]
        if kind == b"WEBP":
            return "image/webp"
        if kind == b"WAVE":
            return "audio/wav"
        if kind == b"AVI ":
            return "video/x-msvideo"
    if h[4:8] == b"ftyp" and len(h) >= 12:
        brand = h[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        return "video/mp4"
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in h else "video/x-matroska"
    if h.startswith(b"OggS"):
        return "audio/ogg"
    if h.startswith(b"fLaC"):
        return "audio/flac"
    if h.startswith(b"ID3"):
        return "audio/mpeg"
    if len(h) >= 2 and h[0] == 0xFF and h[1] in (0xFB, 0xFA, 0xF3, 0xF2):
        return "audio/mpeg"
    if h.startswith(b"%PDF-"):
        return "application/pdf"
    if h.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def sniff(path: Union[str, Path], name: Union[str, None] = None) -> str:
    """Return the media type of ``path``. ``name`` supplies the filename used
    for the extension fallback when ``path`` is a temp file."""
    with open(path, "rb") as f:
        head = f.read(64)
    found = _from_magic(head)
    if found:
        return found
    guess, _ = mimetypes.guess_type(name or str(path))
    return guess or "application/octet-stream"
