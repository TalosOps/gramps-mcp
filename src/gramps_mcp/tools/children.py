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
add_child / remove_child: change one entry of a family's child list.

Unlike create_family's child_handles, these never replace the list. The
server keeps the child's parent_family_list in step; the result re-reads
both the family and the child to show it.
"""

from typing import Dict, List, Literal, Optional

from mcp.types import TextContent
from pydantic import Field

from ..references import fetch_object, ref_handle
from ..write_support import WriteOptions, add_tag, change_entry, ensure_tag, render
from .editing import _error
from .search_basic import with_client

Relation = Literal[
    "birth", "adopted", "stepchild", "sponsored", "foster", "unknown", "none"
]
RELATION_NAMES = {
    "birth": "Birth",
    "adopted": "Adopted",
    "stepchild": "Stepchild",
    "sponsored": "Sponsored",
    "foster": "Foster",
    "unknown": "Unknown",
    "none": "None",
}


class ChildParams(WriteOptions):
    """Parameters for add_child / remove_child."""

    family: str = Field(description="Family handle or Gramps ID (e.g. F0038)")
    person: str = Field(description="Child's handle or Gramps ID (e.g. I0087)")


class AddChildParams(ChildParams):
    """Parameters for add_child."""

    rel: Relation = Field(
        "birth", description="Relationship to both parents: birth, adopted, unknown..."
    )
    father_rel: Optional[Relation] = Field(
        None, description="Relationship to the father, if different from rel"
    )
    mother_rel: Optional[Relation] = Field(
        None, description="Relationship to the mother, if different from rel"
    )


async def _locate(client, entity_type: str, value: str) -> Dict:
    """Fetch by handle, falling back to Gramps ID."""
    try:
        return await fetch_object(client, entity_type, handle=value)
    except ValueError:
        return await fetch_object(client, entity_type, gramps_id=value)


async def _save(client, params, family: Dict, new_family: Dict, child: Dict, verb: str):
    """Shared dry-run / write / re-read path."""
    tag_handle = (
        await ensure_tag(client, params.tag, params.dry_run) if params.tag else None
    )
    new_family = add_tag(new_family, tag_handle)
    change = change_entry("update", "family", family, new_family)
    label = f"{child.get('gramps_id')} {verb} family {family.get('gramps_id')}"
    if params.dry_run:
        return render(f"Dry run - not saved: {label}", True, [change])
    await client.raw_request(
        "PUT", f"families/{family['handle']}", json_data=new_family
    )
    stored_family = await client.raw_request("GET", f"families/{family['handle']}")
    stored_child = await client.raw_request("GET", f"people/{child['handle']}")
    in_list = child["handle"] in [
        ref_handle(c) for c in stored_family["child_ref_list"]
    ]
    linked = family["handle"] in (stored_child.get("parent_family_list") or [])
    expected = verb == "added to"
    ok = in_list == expected and linked == expected
    headline = (
        f"Child {label}. Verified: family child list and the child's "
        "parent_family_list agree."
        if ok
        else f"Error: saved, but read-back disagrees (in child list: {in_list}, "
        f"child links family: {linked})"
    )
    return render(
        headline,
        False,
        [change, change_entry("update", "person", child, stored_child)],
        stored=[stored_family, stored_child],
    )


@with_client
async def add_child_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Add one child to a family.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): AddChildParams fields.

    Returns:
        List[TextContent]: Headline plus JSON result, or an error.
    """
    try:
        params = AddChildParams(**arguments)
        family = await _locate(client, "family", params.family)
        child = await _locate(client, "person", params.person)
        if child["handle"] in [
            ref_handle(c) for c in family.get("child_ref_list") or []
        ]:
            raise ValueError(
                f"{child.get('gramps_id')} is already a child of "
                f"{family.get('gramps_id')}"
            )
        if child["handle"] in (
            family.get("father_handle"),
            family.get("mother_handle"),
        ):
            raise ValueError(
                f"{child.get('gramps_id')} is a parent in {family.get('gramps_id')}"
            )
        entry = {
            "_class": "ChildRef",
            "ref": child["handle"],
            "frel": RELATION_NAMES[params.father_rel or params.rel],
            "mrel": RELATION_NAMES[params.mother_rel or params.rel],
        }
        new_family = {
            **family,
            "child_ref_list": list(family.get("child_ref_list") or []) + [entry],
        }
        text = await _save(client, params, family, new_family, child, "added to")
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return _error(e, "add_child")


@with_client
async def remove_child_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Remove one child from a family (the person is not deleted).

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): ChildParams fields.

    Returns:
        List[TextContent]: Headline plus JSON result, or an error.
    """
    try:
        params = ChildParams(**arguments)
        family = await _locate(client, "family", params.family)
        child = await _locate(client, "person", params.person)
        children = list(family.get("child_ref_list") or [])
        kept = [c for c in children if ref_handle(c) != child["handle"]]
        if len(kept) == len(children):
            raise ValueError(
                f"{child.get('gramps_id')} is not a child of {family.get('gramps_id')}"
            )
        new_family = {**family, "child_ref_list": kept}
        text = await _save(client, params, family, new_family, child, "removed from")
        return [TextContent(type="text", text=text)]
    except Exception as e:
        return _error(e, "remove_child")
