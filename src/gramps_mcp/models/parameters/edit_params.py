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
Parameter models for the update, delete and full-note tools.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ObjectRefParams(BaseModel):
    """Identify one object by handle or Gramps ID."""

    handle: Optional[str] = Field(None, description="Object handle")
    gramps_id: Optional[str] = Field(
        None, description="Gramps ID (e.g. I0001, E0010); used if handle is omitted"
    )


class UpdateParams(ObjectRefParams):
    """Parameters for update_<type>: fetch, edit, then PUT the whole object."""

    set_fields: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Top-level fields to REPLACE, in Gramps JSON. Lists given here "
            "replace the stored list entirely. Examples: "
            '{"description": "Baptism"}, {"place": "<place handle>"}, '
            '{"date": {"dateval": [15, 3, 1850, false], "modifier": 0, '
            '"quality": 0}}, {"citation_list": []}'
        ),
    )
    clear_fields: Optional[List[str]] = Field(
        None,
        description=(
            "Fields to empty: 'date' becomes an empty date, text and handle "
            "fields (description, place, father_handle...) become '', lists "
            "become []"
        ),
    )
    add_refs: Optional[Dict[str, List[str]]] = Field(
        None,
        description=(
            "Append references to list fields, by handle or Gramps ID, e.g. "
            '{"citation_list": ["C0014"], "child_ref_list": ["I0087"]}. '
            "Each must be an existing object of the right type"
        ),
    )
    remove_refs: Optional[Dict[str, List[str]]] = Field(
        None,
        description=(
            "Remove references from list fields, by handle or Gramps ID, e.g. "
            '{"citation_list": ["C0014"]}. Works for dangling handles too. '
            "Fails (nothing saved) if an entry is not in the list"
        ),
    )
    dry_run: bool = Field(False, description="Show the changes without saving them")


class DeleteParams(ObjectRefParams):
    """Parameters for delete_<type>."""

    confirm: bool = Field(
        False,
        description=(
            "Must be true to delete. With false (default) the tool only "
            "previews the object and everything that references it"
        ),
    )


class NoteFullParams(ObjectRefParams):
    """Parameters for get_note_full."""

    offset: int = Field(0, ge=0, description="Character offset to start reading at")
    max_chars: int = Field(
        20000, ge=1, le=100000, description="Maximum characters to return"
    )


class NoteAppendParams(ObjectRefParams):
    """Parameters for append_note."""

    text: str = Field(description="Text to add to the end of the note")
    separator: str = Field(
        "\n\n", description="Inserted between the existing text and the new text"
    )
