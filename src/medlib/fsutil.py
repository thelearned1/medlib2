"""Filesystem helpers."""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Tuple, Union

META_DIR = ".medlib"


def norm_path(path: Union[str, Path]) -> Path:
    """Absolute path with the parent resolved but the final component kept
    as-is (so a symlink is addressed, not followed)."""
    p = Path(os.path.abspath(str(path)))
    return Path(os.path.realpath(str(p.parent))) / p.name


def points_to(link: Path, target: Path) -> bool:
    """True if ``link`` is a symlink whose (possibly dangling) target is
    ``target``."""
    if not link.is_symlink():
        return False
    dest = os.readlink(str(link))
    full = os.path.normpath(os.path.join(os.path.realpath(str(link.parent)), dest))
    return full == str(target)


def hash_file(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def copy_hash(src: Path, dst: Path) -> Tuple[str, int]:
    """Copy ``src`` to ``dst`` hashing the bytes actually copied."""
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(str(src), str(dst))
    return h.hexdigest(), size


def set_writable(path: Path, writable: bool) -> None:
    mode = path.stat().st_mode & 0o7777
    mode = (mode | 0o200) if writable else (mode & ~0o222)
    os.chmod(str(path), mode)


def sibling_temp(path: Path) -> Path:
    return path.parent / f".{path.name}.medlib-{uuid.uuid4().hex[:8]}"


def safe_filename(name: str, ext: str) -> str:
    name = name.replace("/", "_").replace("\0", "_") or "file"
    if ext and not name.lower().endswith("." + ext):
        name = f"{name}.{ext}"
    return name
