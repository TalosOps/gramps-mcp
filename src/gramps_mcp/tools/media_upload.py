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
upload_media: send a file from the client (base64), create the Media object,
and optionally attach it to a citation, event, person, family, source or place
with a region.

The Gramps Web server stores the uploaded bytes under the tree's media path.
If anything after the upload fails, the new media object is deleted again so
no half-finished object is left behind.
"""

import base64
import binascii
import hashlib
import mimetypes
from typing import Any, Dict, List, Literal, Optional

from mcp.types import TextContent
from pydantic import BaseModel, Field, field_validator

from ..client import GrampsAPIError
from ..dates import normalize_date
from ..references import OBJECT_TYPES, fetch_object, ref_handle
from ..write_support import WriteOptions, add_tag, change_entry, ensure_tag, render
from .editing import _error
from .search_basic import with_client

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AttachTarget(BaseModel):
    """Where to attach the new media."""

    type: Literal["citation", "event", "person", "family", "source", "place"] = Field(
        description="Object type to attach to"
    )
    id: str = Field(description="Handle or Gramps ID of that object")
    rect: Optional[List[int]] = Field(
        None,
        description=(
            "Region of the image as [x1, y1, x2, y2] in percent (0-100) of "
            "width/height, e.g. the part of a page showing the entry"
        ),
    )

    @field_validator("rect")
    @classmethod
    def _check_rect(cls, value):
        if value is None:
            return value
        if len(value) != 4 or not all(0 <= v <= 100 for v in value):
            raise ValueError("rect must be [x1, y1, x2, y2] with values 0-100")
        if value[0] >= value[2] or value[1] >= value[3]:
            raise ValueError("rect needs x1 < x2 and y1 < y2")
        return value


class UploadMediaParams(WriteOptions):
    """Parameters for upload_media."""

    data_base64: str = Field(description="File content, base64-encoded")
    filename: str = Field(description="Original file name, e.g. 'census-1860-p7.jpg'")
    mime: Optional[str] = Field(None, description="MIME type; guessed from filename")
    desc: Optional[str] = Field(None, description="Title/description of the media")
    date: Optional[Dict[str, Any]] = Field(
        None, description="Date of the document (structured or raw Gramps date)"
    )
    attach_to: Optional[AttachTarget] = Field(
        None, description="Optionally attach the media to an object, with a region"
    )


def _decode(data: str) -> bytes:
    """Decode base64 (data: URLs accepted) and enforce the size limit."""
    if data.startswith("data:") and "," in data:
        data = data.split(",", 1)[1]
    try:
        content = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"data_base64 is not valid base64: {e}") from e
    if not content:
        raise ValueError("The file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File is {len(content):,} bytes; the limit is {MAX_UPLOAD_BYTES:,}"
        )
    return content


@with_client
async def upload_media_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Upload a file, create its Media object and optionally attach it.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): UploadMediaParams fields.

    Returns:
        List[TextContent]: Headline plus JSON result, or an error.
    """
    media_handle = None
    try:
        params = UploadMediaParams(**arguments)
        content = _decode(params.data_base64)
        mime = params.mime or mimetypes.guess_type(params.filename)[0]
        if not mime:
            raise ValueError("Could not guess the MIME type; pass mime")
        meta: Dict[str, Any] = {}
        if params.desc is not None:
            meta["desc"] = params.desc
        if params.date is not None:
            meta["date"] = normalize_date(params.date)

        target = None
        if params.attach_to:
            collection = OBJECT_TYPES[params.attach_to.type][0]
            try:
                target = await fetch_object(
                    client, params.attach_to.type, handle=params.attach_to.id
                )
            except ValueError:
                target = await fetch_object(
                    client, params.attach_to.type, gramps_id=params.attach_to.id
                )

        file_info = {
            "filename": params.filename,
            "mime": mime,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if params.dry_run:
            changes = [change_entry("create", "media", None, {**meta, "mime": mime})]
            if target:
                changes.append(
                    {
                        "op": "update",
                        "type": params.attach_to.type,
                        "handle": target["handle"],
                        "gramps_id": target.get("gramps_id"),
                        "fields": {
                            "media_list": {"add": {"rect": params.attach_to.rect}}
                        },
                    }
                )
            return [
                TextContent(
                    type="text",
                    text=render(
                        "Dry run - nothing uploaded.",
                        True,
                        changes,
                        extra={"file": file_info},
                    ),
                )
            ]

        uploaded = await client.upload_media_file(content, mime)
        created = [
            r["new"]
            for r in uploaded or []
            if (r.get("new") or {}).get("_class") == "Media"
        ]
        if not created:
            raise GrampsAPIError("The upload did not return a new media object")
        media_handle = created[0]["handle"]

        media = await client.raw_request("GET", f"media/{media_handle}")
        tag_handle = await ensure_tag(client, params.tag) if params.tag else None
        new_media = add_tag({**media, **meta}, tag_handle)
        await client.raw_request("PUT", f"media/{media_handle}", json_data=new_media)

        changes, stored = [], []
        if target:
            ref = {
                "_class": "MediaRef",
                "ref": media_handle,
                "rect": params.attach_to.rect,
            }
            media_list = list(target.get("media_list") or [])
            if media_handle not in [ref_handle(m) for m in media_list]:
                media_list.append(ref)
            new_target = add_tag({**target, "media_list": media_list}, tag_handle)
            await client.raw_request(
                "PUT", f"{collection}/{target['handle']}", json_data=new_target
            )
            stored_target = await client.raw_request(
                "GET", f"{collection}/{target['handle']}"
            )
            if media_handle not in [ref_handle(m) for m in stored_target["media_list"]]:
                raise GrampsAPIError("Attaching the media did not persist")
            changes.append(
                change_entry("update", params.attach_to.type, target, stored_target)
            )
            stored.append(stored_target)

        stored_media = await client.raw_request("GET", f"media/{media_handle}")
        changes.insert(0, change_entry("create", "media", None, stored_media))
        stored.insert(0, stored_media)
        where = (
            f" and attached to {params.attach_to.type} {target.get('gramps_id')}"
            if target
            else ""
        )
        headline = (
            f"Uploaded {params.filename} ({len(content):,} bytes) as media "
            f"{stored_media.get('gramps_id')}{where}. Verified by read-back."
        )
        media_handle = None  # success: nothing to roll back
        return [
            TextContent(
                type="text",
                text=render(
                    headline, False, changes, stored=stored, extra={"file": file_info}
                ),
            )
        ]
    except Exception as e:
        if media_handle:
            try:
                await client.raw_request("DELETE", f"media/{media_handle}")
                e = GrampsAPIError(f"{e} (the uploaded media was deleted again)")
            except GrampsAPIError as cleanup_error:
                e = GrampsAPIError(
                    f"{e}; ALSO failed to delete the uploaded media "
                    f"{media_handle}: {cleanup_error}"
                )
        return _error(e, "media upload")
