"""Query language parser.

    expr  := and ('|' and)*
    and   := not (['&'] not)*          juxtaposition is AND
    not   := '!' not | '(' expr ')' | term
    term  := tag:NAME | tag=NAME | NAME      tag (':' includes subtags, '=' does not)
           | type:KIND                       image, video, gif, image/png ...
           | alias:REGEX                     case-insensitive, unanchored
           | duration:CMP                    seconds, or 1m30, 1:30, 1h5m
           | width:CMP | height:CMP          pixels
           | untagged
    CMP   := [< | <= | > | >= | =] VALUE | VALUE..VALUE

A bare duration matches to within half a second; a range includes both ends
and either end may be left out. A file with no value for a measurement (a
still image has no duration) fails every comparison on it.

Values may be quoted with ' or "; inside quotes everything is literal and the
quote character is written by doubling it. Precedence: ! then & then |.

``parse`` returns a tree of tuples, which ``to_sql`` turns into a SQL condition:

    ("and", [nodes]) ("or", [nodes]) ("not", node)
    ("tag", name, exact) ("type", kind) ("alias", pattern) ("untagged",) ("all",)
    ("measure", column, op, value)       op is one of < <= > >= = or "~" (about)
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import TYPE_CHECKING, List, Optional, Tuple

from .models import MedlibError

if TYPE_CHECKING:
    from .library import Library

OPERATORS = "&|!()"
QUOTES = "'\""
MEASURES = ("duration", "width", "height")
FIELDS = ("tag", "type", "alias") + MEASURES

Node = tuple


class QuerySyntaxError(ValueError):
    pass


# (kind, value, position): kind is an operator character or "term";
# for terms the value is (field, separator, text, quoted).
Token = Tuple[str, object, int]


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c.isspace():
            i += 1
            continue
        if c in OPERATORS:
            tokens.append((c, c, i))
            i += 1
            continue
        start = i
        field: Optional[str] = None
        separator = ""
        quoted = False
        buf: List[str] = []
        while i < n and not source[i].isspace() and source[i] not in OPERATORS:
            c = source[i]
            if c in QUOTES:
                quoted = True
                i += 1
                while True:
                    if i >= n:
                        raise QuerySyntaxError(f"unterminated {c} quote at position {start + 1}")
                    if source[i] == c:
                        if i + 1 < n and source[i + 1] == c:  # doubled = literal
                            buf.append(c)
                            i += 2
                            continue
                        i += 1
                        break
                    buf.append(source[i])
                    i += 1
                continue
            if field is None and not quoted and c in ":=":
                word = "".join(buf).lower()
                if c == ":" and word not in FIELDS:
                    raise QuerySyntaxError(
                        f"unknown field '{word}:' at position {start + 1} "
                        f"(expected {', '.join(f + ':' for f in FIELDS)})"
                    )
                if c == ":" or word == "tag":
                    field, separator, buf = word, c, []
                    i += 1
                    continue
            buf.append(c)
            i += 1
        tokens.append(("term", (field, separator, "".join(buf), quoted), start))
    return tokens


def _term(value, position: int) -> Node:
    field, separator, text, quoted = value
    where = f"at position {position + 1}"
    if not text:
        raise QuerySyntaxError(f"empty value {where}")
    if field is None:
        if not quoted and text.lower() == "untagged":
            return ("untagged",)
        return ("tag", text, False)
    if field == "tag":
        return ("tag", text, separator == "=")
    if field == "type":
        return ("type", text.lower().lstrip("."))
    if field in MEASURES:
        try:
            return _comparison(field, text)
        except ValueError as e:
            raise QuerySyntaxError(f"{e} {where}")
    pattern = unicodedata.normalize("NFC", text)
    try:
        re.compile(pattern)
    except re.error as e:
        raise QuerySyntaxError(f"bad regular expression {where}: {e}")
    return ("alias", pattern)


_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0}
_CLOCK = re.compile(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")
_SPELLED = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?$")


def parse_duration(text: str) -> float:
    """Seconds from '90', '2.5', '90s', '1m30', '1h5m' or '1:30', '1:02:03'."""
    t = text.strip().lower()
    clock = _CLOCK.match(t)
    if clock:
        h, m, s = clock.groups()
        return int(h or 0) * 3600 + int(m) * 60 + float(s)
    spelled = _SPELLED.match(t)
    if t and spelled:
        h, m, s = spelled.groups()
        return int(h or 0) * 3600 + int(m or 0) * 60 + float(s or 0)
    raise ValueError(f"bad duration '{text}' (expected e.g. 10, 2.5, 1m30 or 1:30)")


def _measure_value(field: str, text: str) -> float:
    if field == "duration":
        return parse_duration(text)
    if not text.isdigit():
        raise ValueError(f"bad {field} '{text}' (expected a number of pixels)")
    return int(text)


def _comparison(field: str, text: str) -> Node:
    """'<10', '>=1m30', '640' or '5..30' for one measurement."""
    if ".." in text:
        low, high = (part.strip() for part in text.split("..", 1))
        nodes = []
        if low:
            nodes.append(("measure", field, ">=", _measure_value(field, low)))
        if high:
            nodes.append(("measure", field, "<=", _measure_value(field, high)))
        if not nodes:
            raise ValueError(f"empty {field} range")
        return nodes[0] if len(nodes) == 1 else ("and", nodes)
    for op in ("<=", ">=", "<", ">", "="):
        if text.startswith(op):
            return ("measure", field, op, _measure_value(field, text[len(op):]))
    # A length typed without an operator means "about that long".
    return ("measure", field, "~" if field == "duration" else "=", _measure_value(field, text))


class _Parser:
    def __init__(self, tokens: List[Token], length: int):
        self.tokens = tokens
        self.pos = 0
        self.length = length

    def peek(self) -> Optional[str]:
        return self.tokens[self.pos][0] if self.pos < len(self.tokens) else None

    def where(self) -> str:
        if self.pos < len(self.tokens):
            return f"at position {self.tokens[self.pos][2] + 1}"
        return "at end of query"

    def parse_or(self) -> Node:
        nodes = [self.parse_and()]
        while self.peek() == "|":
            self.pos += 1
            nodes.append(self.parse_and())
        return nodes[0] if len(nodes) == 1 else ("or", nodes)

    def parse_and(self) -> Node:
        nodes = [self.parse_not()]
        while True:
            kind = self.peek()
            if kind == "&":
                self.pos += 1
            elif kind not in ("term", "!", "("):
                break
            nodes.append(self.parse_not())
        return nodes[0] if len(nodes) == 1 else ("and", nodes)

    def parse_not(self) -> Node:
        kind = self.peek()
        if kind == "!":
            self.pos += 1
            return ("not", self.parse_not())
        if kind == "(":
            self.pos += 1
            node = self.parse_or()
            if self.peek() != ")":
                raise QuerySyntaxError(f"expected ')' {self.where()}")
            self.pos += 1
            return node
        if kind == "term":
            _, value, position = self.tokens[self.pos]
            self.pos += 1
            return _term(value, position)
        found = f"'{kind}'" if kind else "nothing"
        raise QuerySyntaxError(f"expected a term but found {found} {self.where()}")


def parse(source: str) -> Node:
    """Parse a query; the empty query matches everything."""
    tokens = tokenize(source)
    if not tokens:
        return ("all",)
    parser = _Parser(tokens, len(source))
    node = parser.parse_or()
    if parser.peek() is not None:
        raise QuerySyntaxError(f"unexpected '{parser.peek()}' {parser.where()}")
    return node


# -- SQL ------------------------------------------------------------------------


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> "re.Pattern":
    return re.compile(pattern, re.IGNORECASE)


def regexp(pattern: str, value: Optional[str]) -> bool:
    """Backs SQL ``value REGEXP pattern``: case-insensitive, unanchored."""
    return value is not None and _compiled(pattern).search(value) is not None


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def to_sql(lib: "Library", node: tuple, params: List[object]) -> str:
    """Turn a parsed query into a SQL condition on ``files f``."""
    kind = node[0]
    if kind == "all":
        return "1"
    if kind in ("and", "or"):
        parts = [to_sql(lib, child, params) for child in node[1]]
        return "(" + f" {kind.upper()} ".join(parts) + ")"
    if kind == "not":
        return f"(NOT {to_sql(lib, node[1], params)})"
    if kind == "untagged":
        return "f.id NOT IN (SELECT file_id FROM tag_files)"
    if kind == "tag":
        _, name, exact = node
        tag_id = lib.resolve_tag(name)
        ids = [tag_id] if exact else lib.tag_descendants(tag_id)
        inline = ",".join(str(i) for i in ids)  # integers from our own tables
        return f"f.id IN (SELECT file_id FROM tag_files WHERE tag_id IN ({inline}))"
    if kind == "type":
        m = node[1]
        if "/" in m:
            params.append(m)
            return "f.media_type = ?"
        params += [like_escape(m) + "/%", "%/" + like_escape(m), m]
        return (
            "(f.media_type LIKE ? ESCAPE '\\' OR f.media_type LIKE ? ESCAPE '\\'"
            " OR f.ext = ?)"
        )
    if kind == "measure":
        _, column, op, value = node
        assert column in MEASURES and op in ("<", "<=", ">", ">=", "=", "~")
        # IS NOT NULL keeps this a plain true/false, so that !duration:<10
        # also finds the files that have no duration.
        if op == "~":
            params += [value - 0.5, value + 0.5]
            return f"(f.{column} IS NOT NULL AND f.{column} >= ? AND f.{column} < ?)"
        params.append(value)
        return f"(f.{column} IS NOT NULL AND f.{column} {op} ?)"
    if kind == "alias":
        params.append(node[1])
        return "f.id IN (SELECT file_id FROM file_aliases WHERE alias REGEXP ?)"
    raise MedlibError(f"query: unsupported node {kind!r}")
