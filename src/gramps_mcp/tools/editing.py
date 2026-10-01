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
Edit tools with replace semantics: update_<type>, get_note_full, append_note.

Unlike create_<type> with a handle (which merges lists and keeps omitted
fields), these tools fetch the stored object, apply explicit edits to a copy
and PUT the whole object back, so entries can be removed and fields cleared.
Every write is read back and compared before success is reported.
"""

import copy
import logging
from typing import Any, Dict, List, Optional, Tuple

from mcp.types import TextContent

from ..client import GrampsAPIError
from ..dates import normalize_date, normalize_dates
from ..models.parameters.edit_params import (
    NoteAppendParams,
    NoteFullParams,
    UpdateParams,
)
from ..references import (
    OBJECT_TYPES,
    REF_HANDLE_FIELDS,
    REF_LIST_FIELDS,
    fetch_object,
    new_ref_entry,
    ref_handle,
    resolve_ref_value,
    strip_for_compare,
    validate_new_references,
)
from ..write_support import add_tag, change_entry, ensure_tag, render
from .search_basic import with_client

logger = logging.getLogger(__name__)

PROTECTED_FIELDS = {"handle", "_class", "change"}
EMPTY_DATE = {"_class": "Date"}


def _error(error: Exception, operation: str) -> List[TextContent]:
    """Format an exception as an MCP error response."""
    if isinstance(error, (GrampsAPIError, ValueError)):
        error_msg = str(error)
    else:
        error_msg = f"Unexpected error during {operation}: {error}"
    logger.error(f"Tool error in {operation}: {error_msg}")
    return [TextContent(type="text", text=f"Error: {error_msg}")]


def _normalize_set_value(field: str, value: Any) -> Any:
    """Fill in the Gramps JSON wrapper for convenience inputs."""
    if field == "date":
        return normalize_date(value)
    if field == "text" and isinstance(value, str):
        return {"_class": "StyledText", "string": value, "tags": []}
    return normalize_dates(value)


def _cleared_value(field: str, current: Any) -> Any:
    """Return the empty value for a field, or raise if it cannot be cleared."""
    if field == "date":
        return dict(EMPTY_DATE)
    if isinstance(current, list):
        return []
    if isinstance(current, str) or current is None:
        return ""
    if isinstance(current, bool):
        return False
    raise ValueError(
        f"Field '{field}' cannot be cleared; use set_fields to give it a value"
    )


def apply_edits(
    obj: Dict,
    entity_type: str,
    set_fields: Optional[Dict[str, Any]] = None,
    clear_fields: Optional[List[str]] = None,
    add_refs: Optional[Dict[str, List[str]]] = None,
    remove_refs: Optional[Dict[str, List[str]]] = None,
) -> Tuple[Dict, List[str], List[str]]:
    """
    Apply edits to a copy of a raw Gramps object.

    Order: set, then clear, then remove, then add. All reference values must
    already be resolved to handles.

    Args:
        obj (Dict): Stored object as returned by the API.
        entity_type (str): Entity type that owns the object.
        set_fields (Optional[Dict[str, Any]]): Fields to replace.
        clear_fields (Optional[List[str]]): Fields to empty.
        add_refs (Optional[Dict[str, List[str]]]): Handles to append per list.
        remove_refs (Optional[Dict[str, List[str]]]): Handles to drop per list.

    Returns:
        Tuple[Dict, List[str], List[str]]: The edited copy, the changed field
        names, and informational notes (e.g. skipped duplicates).

    Raises:
        ValueError: On unknown/protected fields or removals of absent entries.
    """
    new_obj = copy.deepcopy(obj)
    notes: List[str] = []
    touched = set()

    def check_field(field: str) -> None:
        if field in PROTECTED_FIELDS:
            raise ValueError(f"Field '{field}' cannot be edited")
        if field not in obj:
            valid = ", ".join(sorted(k for k in obj if k not in PROTECTED_FIELDS))
            raise ValueError(
                f"{entity_type} has no field '{field}'. Valid fields: {valid}"
            )

    for field, value in (set_fields or {}).items():
        check_field(field)
        new_obj[field] = _normalize_set_value(field, value)
        touched.add(field)

    for field in clear_fields or []:
        check_field(field)
        new_obj[field] = _cleared_value(field, obj[field])
        touched.add(field)

    for field, handles in (remove_refs or {}).items():
        check_field(field)
        if field not in REF_LIST_FIELDS:
            raise ValueError(f"'{field}' is not a reference list")
        present = [ref_handle(e) for e in new_obj[field]]
        missing = [h for h in handles if h not in present]
        if missing:
            raise ValueError(f"Not in {field}, nothing saved: {', '.join(missing)}")
        drop = set(handles)
        new_obj[field] = [e for e in new_obj[field] if ref_handle(e) not in drop]
        touched.add(field)

    for field, handles in (add_refs or {}).items():
        check_field(field)
        if field not in REF_LIST_FIELDS:
            raise ValueError(f"'{field}' is not a reference list")
        present = {ref_handle(e) for e in new_obj[field]}
        for handle in handles:
            if handle in present:
                notes.append(f"{field}: {handle} already present, skipped")
                continue
            new_obj[field].append(new_ref_entry(field, handle, entity_type))
            present.add(handle)
        touched.add(field)

    changed = [
        f
        for f in sorted(touched)
        if strip_for_compare(new_obj[f]) != strip_for_compare(obj[f])
    ]
    return new_obj, changed, notes


def _date_key(date: Any) -> Tuple:
    """Comparable identity of a Gramps date, filling the server's defaults."""
    date = date if isinstance(date, dict) else {}
    dateval = list(date.get("dateval") or [0, 0, 0, False])
    return (
        dateval,
        date.get("modifier", 0),
        date.get("quality", 0),
        date.get("text", ""),
    )


def _is_subset(intended: Any, actual: Any) -> bool:
    """True if every key/value we sent is present in what the server stored."""
    if isinstance(intended, dict) and isinstance(actual, dict):
        return all(
            k in actual and _is_subset(v, actual[k])
            for k, v in intended.items()
            # Reason: GET omits "_class" on some nested objects (e.g. StyledText).
            if k not in ("_class", "sortval", "year", "change")
        )
    if isinstance(intended, list) and isinstance(actual, list):
        return len(intended) == len(actual) and all(
            _is_subset(i, a) for i, a in zip(intended, actual)
        )
    if isinstance(intended, dict) and isinstance(actual, str):
        # Reason: types such as EventRoleType come back as {"string": ...}
        # but may be sent as plain strings, and vice versa.
        return intended.get("string") == actual
    if isinstance(intended, str) and isinstance(actual, dict):
        return actual.get("string") == intended or str(actual.get("value")) == intended
    return intended == actual or (intended in ("", None) and actual in ("", None))


def verify_readback(intended: Dict, stored: Dict, fields: List[str]) -> List[str]:
    """
    Compare written fields with a fresh read of the object.

    Args:
        intended (Dict): Object that was PUT.
        stored (Dict): Object read back afterwards.
        fields (List[str]): Fields that were changed.

    Returns:
        List[str]: Fields whose stored value differs from what was written.
    """
    mismatched = []
    for field in fields:
        want, got = intended.get(field), stored.get(field)
        if field in REF_LIST_FIELDS:
            ok = [ref_handle(e) for e in want or []] == [
                ref_handle(e) for e in got or []
            ]
        elif field in REF_HANDLE_FIELDS:
            ok = (want or "") == (got or "")
        elif field == "date":
            ok = _date_key(want) == _date_key(got)
        else:
            ok = _is_subset(want, got)
        if not ok:
            mismatched.append(field)
    return mismatched


async def _resolve_refs(
    client, obj: Dict, refs: Optional[Dict[str, List[str]]]
) -> Dict[str, List[str]]:
    """Resolve Gramps IDs in add/remove maps to handles."""
    resolved = {}
    for field, values in (refs or {}).items():
        if field not in REF_LIST_FIELDS:
            raise ValueError(f"'{field}' is not a reference list")
        current = [ref_handle(e) for e in obj.get(field) or []]
        resolved[field] = [
            await resolve_ref_value(client, REF_LIST_FIELDS[field], v, current)
            for v in values
        ]
    return resolved


@with_client
async def update_object(client, entity_type: str, arguments: Dict) -> List[TextContent]:
    """
    Fetch an object, apply edits, PUT the whole object, then verify.

    Args:
        client: Gramps API client instance (injected).
        entity_type (str): Entity type to update.
        arguments (Dict): UpdateParams fields.

    Returns:
        List[TextContent]: Diff, verification result, or an error.
    """
    try:
        params = UpdateParams(**arguments)
        if not any(
            [
                params.set_fields,
                params.clear_fields,
                params.add_refs,
                params.remove_refs,
            ]
        ):
            raise ValueError(
                "Nothing to do: give set_fields, clear_fields, add_refs or remove_refs"
            )
        collection = OBJECT_TYPES[entity_type][0]
        obj = await fetch_object(client, entity_type, params.handle, params.gramps_id)
        handle, gramps_id = obj["handle"], obj.get("gramps_id", "")

        new_obj, changed, notes = apply_edits(
            obj,
            entity_type,
            params.set_fields,
            params.clear_fields,
            await _resolve_refs(client, obj, params.add_refs),
            await _resolve_refs(client, obj, params.remove_refs),
        )

        warnings = list(notes)
        if params.tag:
            tag_handle = await ensure_tag(client, params.tag, params.dry_run)
            if tag_handle:
                tagged = add_tag(new_obj, tag_handle)
                if tagged["tag_list"] != new_obj["tag_list"]:
                    new_obj = tagged
                    changed = sorted(set(changed) | {"tag_list"})
            else:
                warnings.append(
                    f"tag '{params.tag}' does not exist yet; it would be created"
                )
        label = f"{entity_type} {gramps_id} [{handle}]"
        change = change_entry("update", entity_type, obj, new_obj)

        if not changed:
            return [
                TextContent(
                    type="text",
                    text=render(
                        f"No changes to {label}: "
                        "the requested values match what is stored.",
                        True,
                        [],
                        warnings=warnings,
                    ),
                )
            ]
        await validate_new_references(client, new_obj, obj)
        if params.dry_run:
            return [
                TextContent(
                    type="text",
                    text=render(
                        f"Dry run - not saved: {label}",
                        True,
                        [change],
                        warnings=warnings,
                    ),
                )
            ]
        await client.raw_request("PUT", f"{collection}/{handle}", json_data=new_obj)

        stored = await client.raw_request("GET", f"{collection}/{handle}")
        mismatched = verify_readback(new_obj, stored, changed)
        if mismatched:
            headline = (
                f"Error: saved {label}, but read-back differs in: "
                f"{', '.join(mismatched)}"
            )
        else:
            headline = (
                f"Updated {label}. Verified by read-back ({len(changed)} field(s))."
            )
        return [
            TextContent(
                type="text",
                text=render(
                    headline, False, [change], stored=[stored], warnings=warnings
                ),
            )
        ]
    except Exception as e:
        return _error(e, f"{entity_type} update")


def make_update_tool(entity_type: str):
    """Build the update_<type> handler for one entity type."""

    async def tool(arguments: Dict) -> List[TextContent]:
        return await update_object(entity_type, arguments)

    tool.__name__ = f"update_{entity_type}_tool"
    return tool


@with_client
async def get_note_full_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Return a note's complete text, paged by character offset.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): NoteFullParams fields.

    Returns:
        List[TextContent]: Header line plus the requested slice of text.
    """
    try:
        params = NoteFullParams(**arguments)
        note = await fetch_object(client, "note", params.handle, params.gramps_id)
        text = (note.get("text") or {}).get("string", "")
        end = min(params.offset + params.max_chars, len(text))
        header = (
            f"Note {note.get('gramps_id')} [{note['handle']}] "
            f"type: {note.get('type')}, length: {len(text)} chars, "
            f"showing {params.offset}-{end}"
        )
        body = text[params.offset : end]
        if end < len(text):
            body += f"\n\n[More text: call again with offset={end}]"
        return [TextContent(type="text", text=f"{header}\n\n{body}")]
    except Exception as e:
        return _error(e, "note read")


@with_client
async def append_note_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Append text to the end of a note and verify the stored result.

    Existing formatting tags keep their ranges because text is only added
    after them.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): NoteAppendParams fields.

    Returns:
        List[TextContent]: New length and tail of the note, or an error.
    """
    try:
        params = NoteAppendParams(**arguments)
        note = await fetch_object(client, "note", params.handle, params.gramps_id)
        styled = dict(note.get("text") or {"_class": "StyledText", "tags": []})
        old_text = styled.get("string", "")
        new_text = (
            old_text + params.separator + params.text if old_text else params.text
        )
        styled["string"] = new_text
        before = note
        tag_handle = (
            await ensure_tag(client, params.tag, params.dry_run) if params.tag else None
        )
        note = add_tag({**note, "text": styled}, tag_handle)
        change = change_entry("update", "note", before, note)
        label = f"note {note.get('gramps_id')} [{note['handle']}]"
        if params.dry_run:
            return [
                TextContent(
                    type="text",
                    text=render(
                        f"Dry run - not saved: would append {len(params.text)} chars "
                        f"to {label} ({len(old_text)} -> {len(new_text)} chars)",
                        True,
                        [change],
                    ),
                )
            ]
        await client.raw_request("PUT", f"notes/{note['handle']}", json_data=note)

        stored = await client.raw_request("GET", f"notes/{note['handle']}")
        stored_text = (stored.get("text") or {}).get("string", "")
        if stored_text != new_text:
            raise GrampsAPIError(
                f"Saved {label}, but read-back length is "
                f"{len(stored_text)} (expected {len(new_text)})"
            )
        return [
            TextContent(
                type="text",
                text=render(
                    f"Appended {len(params.text)} chars to {label}: "
                    f"{len(old_text)} -> {len(stored_text)} chars. "
                    "Verified by read-back.",
                    False,
                    [change],
                    stored=[stored],
                ),
            )
        ]
    except Exception as e:
        return _error(e, "note append")
