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
Shared pieces for every write tool: dry runs, tags and structured results.

Every write tool answers with a short headline followed by one JSON block:

    {"dry_run": bool,
     "changes": [{"op": "create|update|delete", "type", "handle", "gramps_id",
                  "fields": {name: {"before", "after"}}}],
     "stored":  [<object re-read from the database after the write>],
     "warnings": [...]}

``stored`` is always a fresh GET after the write, never the request echo.
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .client import GrampsAPIError
from .references import strip_for_compare


class WriteOptions(BaseModel):
    """Options accepted by every write tool."""

    dry_run: bool = Field(
        False,
        description="Return the before/after diff without writing anything",
    )
    tag: Optional[str] = Field(
        None,
        description=(
            "Tag name to add to every object this call creates or changes "
            "(the tag is created if missing), e.g. a batch name to find or "
            "revert the change later with find_by_tag"
        ),
    )


def with_write_options(schema: type) -> type:
    """Return a subclass of a tool schema that also accepts WriteOptions."""
    if issubclass(schema, WriteOptions):
        return schema
    return type(schema.__name__, (schema, WriteOptions), {"__doc__": schema.__doc__})


def split_options(arguments: Dict) -> tuple:
    """Separate WriteOptions from the tool's own arguments."""
    arguments = dict(arguments or {})
    options = WriteOptions(
        dry_run=bool(arguments.pop("dry_run", False)), tag=arguments.pop("tag", None)
    )
    return options, arguments


async def find_tag(client, name: str) -> Optional[Dict]:
    """Return the tag with this name, or None."""
    for tag in await client.raw_request("GET", "tags/") or []:
        if tag.get("name") == name:
            return tag
    return None


async def ensure_tag(client, name: str, dry_run: bool = False) -> Optional[str]:
    """
    Return the handle of a tag, creating the tag if needed.

    Args:
        client: Gramps API client instance.
        name (str): Tag name.
        dry_run (bool): If true, never create; returns None for a missing tag.

    Returns:
        Optional[str]: Tag handle (None only for a missing tag in a dry run).
    """
    name = name.strip()
    if not name:
        raise ValueError("Tag name must not be empty")
    tag = await find_tag(client, name)
    if tag:
        return tag["handle"]
    if dry_run:
        return None
    result = await client.raw_request("POST", "tags/", json_data={"name": name})
    created = [r["new"] for r in result or [] if r.get("new")]
    if not created:
        raise GrampsAPIError(f"Creating tag '{name}' returned nothing")
    return created[0]["handle"]


def add_tag(obj: Dict, tag_handle: Optional[str]) -> Dict:
    """Return a copy of an object with the tag handle in its tag_list."""
    if not tag_handle:
        return obj
    tags = list(obj.get("tag_list") or [])
    if tag_handle not in tags:
        tags.append(tag_handle)
    return {**obj, "tag_list": tags}


def field_diff(before: Optional[Dict], after: Optional[Dict]) -> Dict[str, Dict]:
    """
    Top-level fields that differ between two versions of an object.

    Server-computed keys (change, sortval...) are ignored.
    """
    before, after = before or {}, after or {}
    out = {}
    for key in sorted(set(before) | set(after)):
        if key in ("change", "_class"):
            continue
        old, new = before.get(key), after.get(key)
        if strip_for_compare(old) != strip_for_compare(new):
            out[key] = {
                "before": strip_for_compare(old),
                "after": strip_for_compare(new),
            }
    return out


def change_entry(
    op: str, entity_type: str, before: Optional[Dict], after: Optional[Dict]
) -> Dict[str, Any]:
    """Describe one object-level change for the JSON result."""
    ref = after or before or {}
    entry: Dict[str, Any] = {
        "op": op,
        "type": entity_type,
        "handle": ref.get("handle"),
        "gramps_id": ref.get("gramps_id"),
    }
    if op == "create":
        entry["after"] = strip_for_compare(after)
    elif op == "delete":
        entry["before"] = strip_for_compare(before)
    else:
        entry["fields"] = field_diff(before, after)
    return entry


def render(
    headline: str,
    dry_run: bool,
    changes: List[Dict],
    stored: Optional[List[Dict]] = None,
    warnings: Optional[List[str]] = None,
    extra: Optional[Dict] = None,
) -> str:
    """Headline plus the JSON result block."""
    body: Dict[str, Any] = {"dry_run": dry_run, "changes": changes}
    if not dry_run:
        body["stored"] = stored or []
    if warnings:
        body["warnings"] = warnings
    if extra:
        body.update(extra)
    return (
        f"{headline}\n\n```json\n{json.dumps(body, ensure_ascii=False, indent=1)}\n```"
    )
