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
find_type (GQL with local fallback), get_by_gramps_id and search snippets.
"""

import json
import re
from typing import Any, Dict, List, Optional

from mcp.types import TextContent
from pydantic import BaseModel, Field

from ..client import GrampsAPIError
from ..gql import BoolOp, Compare, GQLError, Not, matches, parse, to_server
from ..references import OBJECT_TYPES, get_by_gramps_id
from .editing import _error
from .save import describe
from .search_basic import with_client

ID_PREFIX_TYPES = {
    "I": "person",
    "F": "family",
    "E": "event",
    "P": "place",
    "S": "source",
    "C": "citation",
    "N": "note",
    "O": "media",
    "R": "repository",
}
LOCAL_SCAN_CAP = 20000
PAGE = 500
SNIPPET_CHARS = 200
GQL_HELP = (
    'Syntax: field = "value", field != "value", field ~ "contains", '
    'field !~ "...", "value" in field, <, >, combined with and / or / not and '
    'parentheses. Strings may contain any character; escape " as \\".'
)


class GetByIdParams(BaseModel):
    """Parameters for get_by_gramps_id."""

    gramps_id: str = Field(
        description="Gramps ID, e.g. I0001, F0038, E0066, N0743, O0004"
    )
    type: Optional[str] = Field(
        None,
        description=(
            "Object type; only needed if the ID does not use the standard "
            "prefix (I F E P S C N O R)"
        ),
    )


def _rename_paths(node, mapping: Dict[str, str]):
    """Rewrite field paths in a parsed query (e.g. note 'text' -> 'text.string')."""
    if isinstance(node, Compare):
        return Compare(mapping.get(node.path, node.path), node.op, node.value)
    if isinstance(node, Not):
        return Not(_rename_paths(node.child, mapping))
    if isinstance(node, BoolOp):
        return BoolOp(node.op, [_rename_paths(c, mapping) for c in node.children])
    return node


async def _scan(client, collection: str, node) -> List[Dict]:
    """Fetch every object of a type and evaluate the query locally."""
    found, page = [], 1
    while (page - 1) * PAGE < LOCAL_SCAN_CAP:
        rows = await client.raw_request(
            "GET", f"{collection}/", params={"page": page, "pagesize": PAGE}
        )
        found.extend(r for r in rows or [] if matches(node, r))
        if not rows or len(rows) < PAGE:
            break
        page += 1
    return found


@with_client
async def find_type_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Search one object type with GQL.

    The query is parsed here first. If gramps-ql can express it, the server
    runs it; otherwise (``not``, ``!~``, a string with both quote kinds) or if
    the server fails on it (some versions return 500 for any GQL on notes),
    objects are scanned and the same query is evaluated locally.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): type, gql, max_results.

    Returns:
        List[TextContent]: Formatted matches, or an error.
    """
    entity_type = getattr(arguments.get("type"), "value", arguments.get("type"))
    max_results = int(arguments.get("max_results") or 20)
    try:
        if entity_type not in OBJECT_TYPES:
            raise ValueError(f"Entity type '{entity_type}' not supported")
        collection = OBJECT_TYPES[entity_type][0]
        try:
            node = parse(arguments.get("gql") or "")
        except GQLError as e:
            raise ValueError(f"Could not parse the GQL query: {e}. {GQL_HELP}") from e
        if entity_type == "note":
            node = _rename_paths(node, {"text": "text.string"})

        server_query = to_server(node)
        note = ""
        rows: Optional[List[Dict]] = None
        if server_query is not None:
            try:
                params = {"pagesize": max_results}
                if server_query:
                    params["gql"] = server_query
                rows, headers = await client._make_request(
                    "GET",
                    client._build_url("default", f"{collection}/"),
                    params=params,
                    return_headers=True,
                )
                total = int(headers.get("x-total-count", len(rows or [])))
            except GrampsAPIError as e:
                if "server error" not in str(e).lower():
                    raise
                note = (
                    "(The server could not run this query; it was evaluated locally.)\n"
                )
        else:
            note = "(gramps-ql cannot express this query; it was evaluated locally.)\n"
        if rows is None:
            rows = await _scan(client, collection, node)
            total = len(rows)

        plural = collection
        if not rows:
            return [TextContent(type="text", text=f"{note}No {plural} found")]
        shown = rows[:max_results]
        header = f"Found {total} {plural}" + (
            f" (showing {len(shown)})" if total > len(shown) else ""
        )
        blocks = [await describe(client, entity_type, r["handle"]) for r in shown]
        return [
            TextContent(type="text", text=f"{note}{header}:\n\n" + "\n\n".join(blocks))
        ]
    except Exception as e:
        return _error(e, f"{entity_type} search")


@with_client
async def get_by_gramps_id_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Fetch any object by Gramps ID, inferring its type from the ID prefix.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): GetByIdParams fields.

    Returns:
        List[TextContent]: Summary plus the complete object JSON (untruncated).
    """
    try:
        params = GetByIdParams(**arguments)
        gramps_id = params.gramps_id.strip()
        if params.type:
            if params.type not in OBJECT_TYPES:
                raise ValueError(f"Unknown type '{params.type}'")
            candidates = [params.type]
        else:
            prefix = re.match(r"[A-Za-z]+", gramps_id)
            guess = ID_PREFIX_TYPES.get(prefix.group(0).upper()) if prefix else None
            candidates = [guess] if guess else list(OBJECT_TYPES)
        for entity_type in candidates:
            obj = await get_by_gramps_id(client, entity_type, gramps_id)
            if obj:
                summary = await describe(client, entity_type, obj["handle"])
                body = json.dumps(obj, ensure_ascii=False, indent=1)
                head = f"{entity_type} {gramps_id} [{obj['handle']}]"
                text = f"{head}\n\n{summary}\n\n```json\n{body}\n```"
                return [TextContent(type="text", text=text)]
        searched = ", ".join(candidates)
        raise ValueError(f"No object with Gramps ID {gramps_id} (searched: {searched})")
    except Exception as e:
        return _error(e, "get_by_gramps_id")


def _strings(value: Any, out: List[str]) -> List[str]:
    """Every human-readable string in an object, skipping handles and ids."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("handle", "ref", "change", "_class") or key.endswith("_handle"):
                continue
            _strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out)
    elif isinstance(value, str) and value and not re.fullmatch(r"[0-9a-f]{20,}", value):
        out.append(value)
    return out


def snippet(obj: Dict, query: str, width: int = SNIPPET_CHARS) -> str:
    """
    About ``width`` characters of the object's text around the first match.

    The full phrase is tried first, then each word of the query.

    Returns:
        str: The snippet with ellipses where cut, or "" if nothing matched.
    """
    text = " | ".join(_strings(obj, []))
    lowered = text.lower()
    terms = [query.strip().strip('"')] + [w.strip('"') for w in query.split()]
    for term in terms:
        if not term:
            continue
        at = lowered.find(term.lower())
        if at < 0:
            continue
        start = max(0, at + len(term) // 2 - width // 2)
        end = min(len(text), start + width)
        start = max(0, end - width)
        cut = text[start:end].replace("\n", " ")
        return ("..." if start else "") + cut + ("..." if end < len(text) else "")
    return ""
