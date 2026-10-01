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
create_<type> tools with dry_run, tag and re-read results.

The payload sent is exactly what the original create tools sent
(``model.model_dump(exclude_none=True)``), and an existing ``handle`` still
merges (see ``merge_for_put``). What is new: dates are validated, new
references are checked, ``dry_run`` shows the diff without writing, ``tag``
labels the object, and the result carries the object re-read from the
database.
"""

import os
from typing import Dict, List

from mcp.types import TextContent

from ..client import GrampsAPIError, merge_for_put
from ..config import get_settings
from ..dates import normalize_dates
from ..handlers.citation_handler import format_citation
from ..handlers.event_handler import format_event
from ..handlers.family_handler import format_family
from ..handlers.media_handler import format_media
from ..handlers.note_handler import format_note
from ..handlers.person_handler import format_person
from ..handlers.place_handler import format_place
from ..handlers.repository_handler import format_repository
from ..handlers.source_handler import format_source
from ..references import OBJECT_TYPES, validate_new_references
from ..write_support import (
    add_tag,
    change_entry,
    ensure_tag,
    render,
    split_options,
)
from .editing import _error
from .search_basic import with_client

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


async def describe(client, entity_type: str, handle: str) -> str:
    """Readable summary of a stored object via the existing formatters."""
    try:
        text = await FORMATTERS[entity_type](
            client, get_settings().gramps_tree_id, handle
        )
        return text.strip()
    except Exception:
        return ""


def _created_object(result, class_name: str) -> Dict:
    """Pick the newly added object of the wanted class from a transaction."""
    for item in result if isinstance(result, list) else []:
        new = item.get("new") or {}
        if item.get("_class", new.get("_class")) == class_name and item.get(
            "type", "add"
        ) in ("add", None):
            return new
    for item in result if isinstance(result, list) else []:
        if (item.get("new") or {}).get("_class") == class_name:
            return item["new"]
    raise GrampsAPIError(f"The API did not report a new {class_name}")


@with_client
async def save_object(
    client, entity_type: str, model_cls, arguments: Dict
) -> List[TextContent]:
    """
    Create an object, or merge into an existing one when ``handle`` is given.

    Args:
        client: Gramps API client instance (injected).
        entity_type (str): Entity type.
        model_cls: Pydantic model of the original create tool.
        arguments (Dict): Tool arguments including WriteOptions.

    Returns:
        List[TextContent]: Headline plus JSON result, or an error.
    """
    try:
        options, args = split_options(arguments)
        collection, class_name = OBJECT_TYPES[entity_type]
        payload = normalize_dates(model_cls(**args).model_dump(exclude_none=True))
        tag_handle = (
            await ensure_tag(client, options.tag, options.dry_run)
            if options.tag
            else None
        )
        warnings = []
        if options.tag and not tag_handle:
            warnings.append(
                f"tag '{options.tag}' does not exist yet; it would be created"
            )

        handle = payload.get("handle")
        if handle:
            existing = await client.raw_request("GET", f"{collection}/{handle}")
            new_obj = add_tag(merge_for_put(existing, payload), tag_handle)
            await validate_new_references(client, new_obj, existing)
            change = change_entry("update", entity_type, existing, new_obj)
            if options.dry_run:
                return [
                    TextContent(
                        type="text",
                        text=render(
                            f"Dry run - not saved: would merge into {entity_type} "
                            f"{existing.get('gramps_id')}",
                            True,
                            [change],
                            warnings=warnings,
                        ),
                    )
                ]
            await client.raw_request("PUT", f"{collection}/{handle}", json_data=new_obj)
            operation = "updated"
        else:
            new_obj = add_tag(payload, tag_handle)
            await validate_new_references(client, new_obj)
            if options.dry_run:
                return [
                    TextContent(
                        type="text",
                        text=render(
                            f"Dry run - not saved: would create {entity_type}",
                            True,
                            [change_entry("create", entity_type, None, new_obj)],
                            warnings=warnings,
                        ),
                    )
                ]
            result = await client.raw_request(
                "POST", f"{collection}/", json_data=new_obj
            )
            handle = _created_object(result, class_name)["handle"]
            operation = "created"

        stored = await client.raw_request("GET", f"{collection}/{handle}")
        before = existing if operation == "updated" else None
        op = "update" if operation == "updated" else "create"
        summary = await describe(client, entity_type, handle)
        return [
            TextContent(
                type="text",
                text=render(
                    f"Successfully {operation} {entity_type}:\n\n{summary}",
                    False,
                    [change_entry(op, entity_type, before, stored)],
                    stored=[stored],
                    warnings=warnings,
                ),
            )
        ]
    except Exception as e:
        return _error(e, f"{entity_type} save")


def make_create_tool(entity_type: str, model_cls):
    """Build the create_<type> handler for one entity type."""

    async def tool(arguments: Dict) -> List[TextContent]:
        return await save_object(entity_type, model_cls, arguments)

    tool.__name__ = f"create_{entity_type}_tool"
    return tool


@with_client
async def create_media_with_options(client, arguments: Dict) -> List[TextContent]:
    """
    create_media with dry_run/tag and a re-read result.

    Without a handle the file at ``file_location`` (on the MCP server) is
    uploaded, then the metadata and tag are saved onto the new media object.
    """
    import mimetypes

    from ..models.parameters.media_params import MediaSaveParams

    try:
        options, args = split_options(arguments)
        file_location = args.pop("file_location", None)
        if args.get("handle"):
            return await save_object(
                "media", MediaSaveParams, {**args, **options.model_dump()}
            )
        if not file_location or not os.path.isfile(file_location):
            raise ValueError(f"File not found on the MCP server: {file_location}")
        meta = normalize_dates(MediaSaveParams(**args).model_dump(exclude_none=True))
        if options.dry_run:
            return [
                TextContent(
                    type="text",
                    text=render(
                        "Dry run - not saved: would upload and create media",
                        True,
                        [
                            change_entry(
                                "create", "media", None, {**meta, "path": file_location}
                            )
                        ],
                    ),
                )
            ]
        mime_type = mimetypes.guess_type(file_location)[0] or "application/octet-stream"
        with open(file_location, "rb") as f:
            content = f.read()
        uploaded = _created_object(
            await client.upload_media_file(content, mime_type), "Media"
        )
        handle = uploaded["handle"]
        media = await client.raw_request("GET", f"media/{handle}")
        tag_handle = await ensure_tag(client, options.tag) if options.tag else None
        new_obj = add_tag({**media, **meta, "handle": handle}, tag_handle)
        await validate_new_references(client, new_obj, media)
        await client.raw_request("PUT", f"media/{handle}", json_data=new_obj)
        stored = await client.raw_request("GET", f"media/{handle}")
        summary = await describe(client, "media", handle)
        return [
            TextContent(
                type="text",
                text=render(
                    f"Successfully created media:\n\n{summary}",
                    False,
                    [change_entry("create", "media", None, stored)],
                    stored=[stored],
                ),
            )
        ]
    except Exception as e:
        return _error(e, "media save")
