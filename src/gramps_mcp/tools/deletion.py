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
delete_<type> tools.

Without confirm=true a call only previews the object and its referrers.
With it, the object is deleted through the API (which also strips references
to it from other objects), the deletion is verified, any references the
server left behind are reported, and the deleted object's full JSON is
returned so the caller can log it or recreate it.
"""

import json
import logging
from typing import Dict, List

from mcp.types import TextContent

from ..client import GrampsAPIError
from ..config import get_settings
from ..handlers.citation_handler import format_citation
from ..handlers.event_handler import format_event
from ..handlers.family_handler import format_family
from ..handlers.media_handler import format_media
from ..handlers.note_handler import format_note
from ..handlers.person_handler import format_person
from ..handlers.place_handler import format_place
from ..handlers.repository_handler import format_repository
from ..handlers.source_handler import format_source
from ..models.parameters.edit_params import DeleteParams
from ..references import OBJECT_TYPES, fetch_object
from .editing import _error
from .search_basic import with_client

logger = logging.getLogger(__name__)

FORMATTERS = {
    "person": format_person,
    "family": format_family,
    "event": format_event,
    "place": format_place,
    "source": format_source,
    "citation": format_citation,
    "note": format_note,
    "media": format_media,
    "repository": format_repository,
}
MAX_REFERRERS_SHOWN = 50


async def _summary(client, entity_type: str, obj: Dict) -> str:
    """One readable block describing the object, via the existing formatters."""
    try:
        text = await FORMATTERS[entity_type](
            client, get_settings().gramps_tree_id, obj["handle"]
        )
        if text.strip():
            return text.strip()
    except Exception as e:
        logger.debug(f"Formatter failed for {entity_type} {obj['handle']}: {e}")
    return f"{entity_type} {obj.get('gramps_id')} [{obj['handle']}]"


async def _referrers(client, entity_type: str, handle: str) -> List[Dict]:
    """
    List objects that reference this one.

    Returns:
        List[Dict]: Entries with type, handle and gramps_id.
    """
    collection = OBJECT_TYPES[entity_type][0]
    obj = await client.raw_request(
        "GET", f"{collection}/{handle}", params={"backlinks": "true"}
    )
    found = []
    for ref_type, handles in (obj.get("backlinks") or {}).items():
        for ref_handle in handles:
            entry = {"type": ref_type, "handle": ref_handle, "gramps_id": "?"}
            if ref_type in OBJECT_TYPES and len(found) < MAX_REFERRERS_SHOWN:
                try:
                    ref_obj = await client.raw_request(
                        "GET", f"{OBJECT_TYPES[ref_type][0]}/{ref_handle}"
                    )
                    entry["gramps_id"] = ref_obj.get("gramps_id", "?")
                except GrampsAPIError:
                    pass
            found.append(entry)
    return found


def _describe_referrers(referrers: List[Dict]) -> str:
    """Render referrers as one line each."""
    if not referrers:
        return "Referenced by: nothing"
    lines = [f"Referenced by {len(referrers)} object(s):"]
    lines += [f"- {r['type']} {r['gramps_id']} [{r['handle']}]" for r in referrers]
    return "\n".join(lines)


async def _still_referencing(client, handle: str, referrers: List[Dict]) -> List[str]:
    """Referrers that still mention the deleted handle after the delete."""
    leftovers = []
    for r in referrers:
        if r["type"] not in OBJECT_TYPES:
            continue
        try:
            ref_obj = await client.raw_request(
                "GET", f"{OBJECT_TYPES[r['type']][0]}/{r['handle']}"
            )
        except GrampsAPIError:
            continue
        if handle in json.dumps(ref_obj):
            leftovers.append(f"{r['type']} {r['gramps_id']} [{r['handle']}]")
    return leftovers


def _describe_transaction(result) -> List[str]:
    """Summarise the API's transaction list: what was deleted vs modified."""
    lines = []
    for item in result if isinstance(result, list) else []:
        old, new = item.get("old") or {}, item.get("new")
        action = "deleted" if new is None else "updated (reference removed)"
        lines.append(
            f"- {item.get('_class')} {old.get('gramps_id', '?')} "
            f"[{item.get('handle')}] {action}"
        )
    return lines


@with_client
async def delete_object(client, entity_type: str, arguments: Dict) -> List[TextContent]:
    """
    Preview or delete one object.

    Args:
        client: Gramps API client instance (injected).
        entity_type (str): Entity type to delete.
        arguments (Dict): DeleteParams fields.

    Returns:
        List[TextContent]: Preview, deletion record, or an error.
    """
    try:
        params = DeleteParams(**arguments)
        collection = OBJECT_TYPES[entity_type][0]
        obj = await fetch_object(client, entity_type, params.handle, params.gramps_id)
        handle, gramps_id = obj["handle"], obj.get("gramps_id", "")
        summary = await _summary(client, entity_type, obj)
        referrers = await _referrers(client, entity_type, handle)

        if not params.confirm:
            text = (
                f"Preview only - nothing deleted. Would delete {entity_type} "
                f"{gramps_id} [{handle}]:\n\n{summary}\n\n"
                f"{_describe_referrers(referrers)}\n\n"
                "References to it will be removed from these objects. "
                "Re-run with confirm=true to delete."
            )
            return [TextContent(type="text", text=text)]

        result = await client.raw_request("DELETE", f"{collection}/{handle}")

        try:
            await client.raw_request("GET", f"{collection}/{handle}")
            raise GrampsAPIError(
                f"Delete of {entity_type} {gramps_id} reported success, "
                "but the object can still be read"
            )
        except GrampsAPIError as e:
            if "not found" not in str(e).lower():
                raise

        lines = [
            f"Deleted {entity_type} {gramps_id} [{handle}]. "
            "Verified: no longer readable.",
            "",
            summary,
            "",
            _describe_referrers(referrers),
        ]
        changes = _describe_transaction(result)
        if changes:
            lines += ["", "Server transaction:", *changes]
        leftovers = await _still_referencing(client, handle, referrers)
        if leftovers:
            lines += [
                "",
                "WARNING - these still reference the deleted handle "
                "(remove with update_<type> remove_refs):",
                *[f"- {x}" for x in leftovers],
            ]
        lines += [
            "",
            "Deleted object JSON (for the log / to recreate):",
            json.dumps(obj, ensure_ascii=False),
        ]
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return _error(e, f"{entity_type} delete")


def make_delete_tool(entity_type: str):
    """Build the delete_<type> handler for one entity type."""

    async def tool(arguments: Dict) -> List[TextContent]:
        return await delete_object(entity_type, arguments)

    tool.__name__ = f"delete_{entity_type}_tool"
    return tool
