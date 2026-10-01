# gramps-mcp - AI-Powered Genealogy Research & Management
# Copyright (C) 2025 cabout.me
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
A small GQL parser: tokenize, parse, evaluate locally, re-serialize safely.

Used so a query is understood the same way whether the server runs it or
it has to be evaluated here (the server's gramps-ql has no ``not``, cannot
escape a quote inside a string, and some Gramps Web versions return HTTP 500
for any GQL on notes).

Grammar::

    query   := or_expr
    or_expr := and_expr ("or" and_expr)*
    and_expr:= unary ("and" unary)*
    unary   := "not" unary | "(" or_expr ")" | compare
    compare := path op value | value "in" path | path
    op      := = | == | != | ~ | !~ | < | <= | > | >=
    value   := "string" | 'string' | number | bareword

Strings may contain any character including ``.``; ``\\"`` and ``\\\\`` are
escapes. ``~`` / ``!~`` are case-insensitive "contains". A path may index
lists (``event_ref_list[0].role``); without an index a comparison matches if
any list element matches.
"""

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Union

OPS = ("==", "!=", "<=", ">=", "!~", "=", "~", "<", ">")
KEYWORDS = {"and", "or", "not", "in"}


class GQLError(ValueError):
    """Raised for a query that cannot be parsed."""


@dataclass
class Compare:
    path: str
    op: str  # one of OPS, "in" (value in path) or "exists"
    value: Any = None


@dataclass
class Not:
    child: "Node"


@dataclass
class BoolOp:
    op: str  # "and" | "or"
    children: List["Node"]


Node = Union[Compare, Not, BoolOp]

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<op>==|!=|<=|>=|!~|=|~|<|>)
      | (?P<paren>[()])
      | (?P<word>[^\s()"'=!~<>]+)
    )""",
    re.VERBOSE,
)


def tokenize(query: str) -> List[tuple]:
    """Split a query into (kind, value) tokens."""
    tokens, pos = [], 0
    query = query.strip()
    while pos < len(query):
        match = _TOKEN.match(query, pos)
        if not match or match.end() == pos:
            raise GQLError(
                f"Cannot read the query at character {pos}: {query[pos:pos + 20]!r}"
            )
        pos = match.end()
        kind = match.lastgroup
        text = match.group(kind)
        if kind == "string":
            text = re.sub(r"\\(.)", r"\1", text[1:-1])
        elif kind == "word" and text.lower() in KEYWORDS:
            kind, text = "kw", text.lower()
        tokens.append((kind, text))
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens, self.i = tokens, 0

    def peek(self, kind=None, value=None):
        if self.i >= len(self.tokens):
            return None
        tok = self.tokens[self.i]
        if (kind and tok[0] != kind) or (value and tok[1] != value):
            return None
        return tok

    def take(self, kind=None, value=None):
        tok = self.peek(kind, value)
        if tok is None:
            got = (
                self.tokens[self.i][1] if self.i < len(self.tokens) else "end of query"
            )
            raise GQLError(f"Expected {value or kind or 'a value'}, got {got!r}")
        self.i += 1
        return tok

    def or_expr(self):
        children = [self.and_expr()]
        while self.peek("kw", "or"):
            self.i += 1
            children.append(self.and_expr())
        return children[0] if len(children) == 1 else BoolOp("or", children)

    def and_expr(self):
        children = [self.unary()]
        while self.peek("kw", "and"):
            self.i += 1
            children.append(self.unary())
        return children[0] if len(children) == 1 else BoolOp("and", children)

    def unary(self):
        if self.peek("kw", "not"):
            self.i += 1
            return Not(self.unary())
        if self.peek("paren", "("):
            self.i += 1
            node = self.or_expr()
            self.take("paren", ")")
            return node
        return self.compare()

    def value(self):
        tok = self.take()
        if tok[0] == "string":
            return tok[1]
        if tok[0] == "word":
            try:
                return int(tok[1])
            except ValueError:
                try:
                    return float(tok[1])
                except ValueError:
                    return tok[1]
        raise GQLError(f"Expected a value, got {tok[1]!r}")

    def compare(self):
        if self.peek("string"):
            value = self.take()[1]
            self.take("kw", "in")
            return Compare(self.take("word")[1], "in", value)
        path = self.take("word")[1]
        if self.peek("op"):
            op = self.take()[1]
            return Compare(path, op, self.value())
        return Compare(path, "exists")


def parse(query: str) -> Optional[Node]:
    """
    Parse a GQL query.

    Returns:
        Optional[Node]: The syntax tree, or None for an empty query.

    Raises:
        GQLError: If the query is malformed.
    """
    tokens = tokenize(query or "")
    if not tokens:
        return None
    parser = _Parser(tokens)
    node = parser.or_expr()
    if parser.i != len(tokens):
        raise GQLError(f"Unexpected {tokens[parser.i][1]!r} after a complete query")
    return node


def uses_not(node: Optional[Node]) -> bool:
    """True if the tree contains a ``not`` (or ``!~``), which gramps-ql lacks."""
    if isinstance(node, Not):
        return True
    if isinstance(node, BoolOp):
        return any(uses_not(c) for c in node.children)
    return isinstance(node, Compare) and node.op == "!~"


def _quote(value: Any) -> Optional[str]:
    """Literal for gramps-ql, or None if it cannot be expressed there."""
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    return None  # both quote kinds: gramps-ql has no escape


def to_server(node: Optional[Node]) -> Optional[str]:
    """
    Serialize for the server, or None if gramps-ql cannot express the query.
    """
    if node is None:
        return ""
    if isinstance(node, Not):
        return None
    if isinstance(node, BoolOp):
        parts = [to_server(c) for c in node.children]
        if any(p is None for p in parts):
            return None
        return f" {node.op} ".join(f"({p})" if " " in p else p for p in parts)
    if node.op == "exists":
        return node.path
    literal = _quote(node.value)
    if literal is None or node.op == "!~":
        return None
    if node.op == "in":
        return f"{node.path} ~ {literal}"
    op = "=" if node.op == "==" else node.op
    return f"{node.path} {op} {literal}"


def _resolve(obj: Any, path: str) -> List[Any]:
    """All values at a dotted path; lists fan out unless indexed."""
    values = [obj]
    for segment in path.split("."):
        match = re.fullmatch(r"([^\[\]]+)(?:\[(\d+|\*)\])?", segment)
        if not match:
            return []
        key, index = match.group(1), match.group(2)
        nxt = []
        for value in values:
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, dict) or key not in item:
                    continue
                child = item[key]
                if index not in (None, "*"):
                    if isinstance(child, list) and int(index) < len(child):
                        nxt.append(child[int(index)])
                    continue
                nxt.append(child)
        values = nxt
    flat = []
    for value in values:
        flat.extend(value if isinstance(value, list) else [value])
    return flat


def _text(value: Any) -> str:
    """Comparable text of a value (Gramps types/StyledText may be dicts)."""
    if isinstance(value, dict):
        return str(value.get("string", value.get("value", "")))
    return "" if value is None else str(value)


def _compare(actual: Any, op: str, wanted: Any) -> bool:
    if op in ("~", "in", "!~"):
        found = str(wanted).lower() in _text(actual).lower()
        return not found if op == "!~" else found
    if isinstance(wanted, (int, float)) and not isinstance(actual, bool):
        try:
            actual_num = float(_text(actual))
        except ValueError:
            return op == "!=" and True
        left, right = actual_num, float(wanted)
    else:
        left, right = _text(actual), str(wanted)
    return {
        "=": left == right,
        "==": left == right,
        "!=": left != right,
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
    }[op]


def matches(node: Optional[Node], obj: Any) -> bool:
    """Evaluate a parsed query against one object (API JSON form)."""
    if node is None:
        return True
    if isinstance(node, Not):
        return not matches(node.child, obj)
    if isinstance(node, BoolOp):
        results = (matches(c, obj) for c in node.children)
        return all(results) if node.op == "and" else any(results)
    path = "text.string" if node.path == "text" else node.path
    values = _resolve(obj, path)
    if node.op == "exists":
        return any(v not in (None, "", [], {}) for v in values)
    if node.op in ("!=", "!~"):
        return all(_compare(v, node.op, node.value) for v in values) if values else True
    return any(_compare(v, node.op, node.value) for v in values)
