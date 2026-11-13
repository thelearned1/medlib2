"""Alias and ID rules shared by tags and files.

An all-digit reference is always an ID; anything else is an alias. Aliases
compare case-insensitively after NFC normalisation and keep their display case.
"""
from __future__ import annotations

import re
import unicodedata

from .models import MedlibError

_ID_RE = re.compile(r"[0-9]+")
_TAG_FORBIDDEN = set(":&|!()\"'")


def is_id(ref: str) -> bool:
    return bool(_ID_RE.fullmatch(ref))


def normalize_alias(alias: str) -> str:
    return unicodedata.normalize("NFC", alias)


def alias_key(alias: str) -> str:
    """Case-insensitive comparison key."""
    return unicodedata.normalize("NFC", normalize_alias(alias).casefold())


def validate_tag_alias(alias: str) -> str:
    alias = normalize_alias(alias)
    if not alias:
        raise MedlibError("tag alias cannot be empty")
    if is_id(alias):
        raise MedlibError(f"tag alias '{alias}' is all digits (reserved for IDs)")
    if alias.startswith("-"):
        raise MedlibError(f"tag alias '{alias}' cannot start with '-'")
    if any(c.isspace() for c in alias):
        raise MedlibError(f"tag alias '{alias}' cannot contain whitespace")
    bad = sorted(set(alias) & _TAG_FORBIDDEN)
    if bad:
        raise MedlibError(
            f"tag alias '{alias}' contains reserved character(s): {' '.join(bad)}"
        )
    return alias


def validate_file_alias(alias: str) -> str:
    alias = normalize_alias(alias)
    if not alias:
        raise MedlibError("file alias cannot be empty")
    if is_id(alias):
        raise MedlibError(f"file alias '{alias}' is all digits (reserved for IDs)")
    return alias
