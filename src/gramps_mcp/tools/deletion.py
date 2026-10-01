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
from ..references import OBJECT_TYPES, fetch_object, remove_handle_refs
from ..write_support import change_entry, render
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
    Fetch every object that references this one.

    Returns:
        List[Dict]: Entries with type, handle, gramps_id and the object.
    """
    collection = OBJECT_TYPES[entity_type][0]
    obj = await client.raw_request(
        "GET", f"{collection}/{handle}", params={"backlinks": "true"}
    )
    found = []
    for ref_type, handles in (obj.get("backlinks") or {}).items():
        for ref_handle in handles:
            entry = {
                "type": ref_type,
                "handle": ref_handle,
                "gramps_id": "?",
                "obj": None,
            }
            if ref_type in OBJECT_TYPES:
                try:
                    entry["obj"] = await client.raw_request(
                        "GET", f"{OBJECT_TYPES[ref_type][0]}/{ref_handle}"
                    )
                    entry["gramps_id"] = entry["obj"].get("gramps_id", "?")
                except GrampsAPIError:
                    pass
            found.append(entry)
    return found


def _describe_referrers(referrers: List[Dict]) -> str:
    """Render referrers as one line each."""
    if not referrers:
        return "Referenced by: nothing"
    shown = referrers[:MAX_REFERRERS_SHOWN]
    lines = [f"Referenced by {len(referrers)} object(s):"]
    lines += [f"- {r['type']} {r['gramps_id']} [{r['handle']}]" for r in shown]
    if len(referrers) > len(shown):
        lines.append(f"- ... and {len(referrers) - len(shown)} more")
    return "\n".join(lines)


CASCADE_NOTES = {
    "person": "Families left with no parents and no children are deleted too.",
    "source": "Citations of this source are deleted too, with their references.",
}


@with_client
async def delete_object(client, entity_type: str, arguments: Dict) -> List[TextContent]:
    """
    Preview or delete one object.

    Args:
        client: Gramps API client instance (injected).
        entity_type (str): Entity type to delete.
        arguments (Dict): DeleteParams fields.

    Returns:
        List[TextContent]: Preview or deletion record (headline + JSON), or an error.
    """
    try:
        params = DeleteParams(**arguments)
        collection = OBJECT_TYPES[entity_type][0]
        obj = await fetch_object(client, entity_type, params.handle, params.gramps_id)
        handle, gramps_id = obj["handle"], obj.get("gramps_id", "")
        summary = await _summary(client, entity_type, obj)
        referrers = await _referrers(client, entity_type, handle)
        warnings = [CASCADE_NOTES[entity_type]] if entity_type in CASCADE_NOTES else []

        if params.dry_run or not params.confirm:
            changes = [change_entry("delete", entity_type, obj, None)]
            changes += [
                change_entry(
                    "update", r["type"], r["obj"], remove_handle_refs(r["obj"], handle)
                )
                for r in referrers
                if r["obj"]
            ]
            headline = (
                f"Preview only - nothing deleted. Would delete {entity_type} "
                f"{gramps_id} [{handle}]:\n\n{summary}\n\n"
                f"{_describe_referrers(referrers)}\n\n"
                "References to it will be removed from these objects. "
                "Re-run with confirm=true to delete."
            )
            return [
                TextContent(
                    type="text", text=render(headline, True, changes, warnings=warnings)
                )
            ]

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

        changes = [change_entry("delete", entity_type, obj, None)]
        stored = []
        for r in referrers:
            if r["type"] not in OBJECT_TYPES:
                continue
            try:
                after = await client.raw_request(
                    "GET", f"{OBJECT_TYPES[r['type']][0]}/{r['handle']}"
                )
            except GrampsAPIError:
                changes.append(change_entry("delete", r["type"], r["obj"], None))
                continue
            stored.append(after)
            changes.append(change_entry("update", r["type"], r["obj"], after))
            if handle in json.dumps(after):
                warnings.append(
                    f"{r['type']} {r['gramps_id']} still references the deleted "
                    "handle; remove it with update_<type> remove_refs"
                )
        listed = {c["handle"] for c in changes}
        for item in result if isinstance(result, list) else []:
            if item.get("handle") not in listed and item.get("new") is None:
                old = item.get("old") or {}
                changes.append(
                    {
                        "op": "delete",
                        "type": str(item.get("_class", "")).lower(),
                        "handle": item.get("handle"),
                        "gramps_id": old.get("gramps_id"),
                    }
                )

        headline = (
            f"Deleted {entity_type} {gramps_id} [{handle}]. "
            f"Verified: no longer readable.\n\n{summary}\n\n"
            f"{_describe_referrers(referrers)}\n\n"
            "The deleted object's full JSON is in changes[0].before (for the "
            "log, or to recreate it)."
        )
        return [
            TextContent(
                type="text",
                text=render(headline, False, changes, stored=stored, warnings=warnings),
            )
        ]
    except Exception as e:
        return _error(e, f"{entity_type} delete")


def make_delete_tool(entity_type: str):
    """Build the delete_<type> handler for one entity type."""

    async def tool(arguments: Dict) -> List[TextContent]:
        return await delete_object(entity_type, arguments)

    tool.__name__ = f"delete_{entity_type}_tool"
    return tool
