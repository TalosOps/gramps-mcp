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
Object-type metadata, handle resolution and reference validation.

The Gramps Web API accepts any string in a reference field: putting a
note handle (or a typo) in ``citation_list`` is stored as-is and becomes a
dangling reference. Everything here exists so write tools can check that a
newly added reference points at an object of the right type before saving.
"""

import copy
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .client import GrampsAPIError

# entity type -> (API collection, Gramps class name)
OBJECT_TYPES: Dict[str, Tuple[str, str]] = {
    "person": ("people", "Person"),
    "family": ("families", "Family"),
    "event": ("events", "Event"),
    "place": ("places", "Place"),
    "source": ("sources", "Source"),
    "citation": ("citations", "Citation"),
    "note": ("notes", "Note"),
    "media": ("media", "Media"),
    "repository": ("repositories", "Repository"),
}

# Top-level list fields that hold references, and the type they point at.
REF_LIST_FIELDS: Dict[str, str] = {
    "citation_list": "citation",
    "note_list": "note",
    "tag_list": "tag",
    "media_list": "media",
    "event_ref_list": "event",
    "child_ref_list": "person",
    "person_ref_list": "person",
    "reporef_list": "repository",
    "placeref_list": "place",
    "family_list": "family",
    "parent_family_list": "family",
}

# Top-level scalar fields that hold a single handle.
REF_HANDLE_FIELDS: Dict[str, str] = {
    "father_handle": "person",
    "mother_handle": "person",
    "place": "place",
    "source_handle": "source",
}

# List fields whose entries are reference objects ({"ref": handle, ...})
# rather than bare handles, with the class to use for new entries.
REF_OBJECT_CLASSES: Dict[str, str] = {
    "media_list": "MediaRef",
    "event_ref_list": "EventRef",
    "child_ref_list": "ChildRef",
    "person_ref_list": "PersonRef",
    "reporef_list": "RepoRef",
    "placeref_list": "PlaceRef",
}

_COLLECTIONS = {**{k: v[0] for k, v in OBJECT_TYPES.items()}, "tag": "tags"}


def ref_handle(entry: Any) -> Optional[str]:
    """
    Return the handle a list entry points at.

    Args:
        entry (Any): A bare handle string or a reference object dict.

    Returns:
        Optional[str]: The referenced handle, or None if not recognisable.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("ref")
    return None


def new_ref_entry(field: str, handle: str, owner_type: str) -> Any:
    """
    Build a minimal new entry for a reference list.

    Args:
        field (str): List field name, e.g. ``child_ref_list``.
        handle (str): Handle being referenced.
        owner_type (str): Entity type that owns the list.

    Returns:
        Any: A bare handle, or a reference object the API completes with
        its own defaults.
    """
    ref_class = REF_OBJECT_CLASSES.get(field)
    if not ref_class:
        return handle
    entry: Dict[str, Any] = {"_class": ref_class, "ref": handle}
    if field == "event_ref_list":
        entry["role"] = "Family" if owner_type == "family" else "Primary"
    return entry


async def object_exists(client, target_type: str, handle: str) -> bool:
    """
    Check whether an object of the given type exists.

    Args:
        client: Gramps API client instance.
        target_type (str): Entity type (or ``tag``).
        handle (str): Handle to look up.

    Returns:
        bool: True if the object exists, False on 404.
    """
    try:
        await client.raw_request("GET", f"{_COLLECTIONS[target_type]}/{handle}")
        return True
    except GrampsAPIError as e:
        if "not found" in str(e).lower():
            return False
        raise


async def get_by_gramps_id(client, entity_type: str, gramps_id: str) -> Optional[Dict]:
    """
    Fetch a raw object by its Gramps ID.

    Args:
        client: Gramps API client instance.
        entity_type (str): Entity type.
        gramps_id (str): Gramps ID such as ``E0010``.

    Returns:
        Optional[Dict]: The raw object, or None if no object has that ID.
    """
    try:
        result = await client.raw_request(
            "GET", f"{_COLLECTIONS[entity_type]}/", params={"gramps_id": gramps_id}
        )
    except GrampsAPIError as e:
        if "not found" in str(e).lower():
            return None
        raise
    return result[0] if result else None


async def fetch_object(
    client,
    entity_type: str,
    handle: Optional[str] = None,
    gramps_id: Optional[str] = None,
) -> Dict:
    """
    Fetch a complete raw object by handle or Gramps ID.

    Args:
        client: Gramps API client instance.
        entity_type (str): Entity type.
        handle (Optional[str]): Object handle.
        gramps_id (Optional[str]): Gramps ID, used when no handle is given.

    Returns:
        Dict: The raw Gramps JSON object.

    Raises:
        ValueError: If neither identifier is given or nothing matches.
    """
    if handle:
        try:
            return await client.raw_request(
                "GET", f"{OBJECT_TYPES[entity_type][0]}/{handle}"
            )
        except GrampsAPIError as e:
            if "not found" in str(e).lower():
                raise ValueError(f"No {entity_type} with handle {handle}") from e
            raise
    if gramps_id:
        obj = await get_by_gramps_id(client, entity_type, gramps_id)
        if obj is None:
            raise ValueError(f"No {entity_type} with Gramps ID {gramps_id}")
        return obj
    raise ValueError("Provide either handle or gramps_id")


async def resolve_ref_value(
    client, target_type: str, value: str, current_handles: Iterable[str] = ()
) -> str:
    """
    Turn a handle-or-Gramps-ID into a handle.

    A value already present in the list is taken as a handle as-is, so a
    dangling handle can still be removed. Anything else is tried as a
    handle first, then as a Gramps ID of the target type.

    Args:
        client: Gramps API client instance.
        target_type (str): Type the reference must point at.
        value (str): Handle or Gramps ID.
        current_handles (Iterable[str]): Handles currently in the field.

    Returns:
        str: The resolved handle.

    Raises:
        ValueError: If the value matches no object of the target type.
    """
    if value in set(current_handles):
        return value
    if await object_exists(client, target_type, value):
        return value
    if target_type != "tag":
        obj = await get_by_gramps_id(client, target_type, value)
        if obj:
            return obj["handle"]
    raise ValueError(f"'{value}' is not the handle or Gramps ID of any {target_type}")


def _field_handles(obj: Dict, field: str) -> List[str]:
    """Return the handles referenced by a top-level field of a raw object."""
    value = obj.get(field)
    if field in REF_HANDLE_FIELDS:
        return [value] if isinstance(value, str) and value else []
    if isinstance(value, list):
        return [h for h in (ref_handle(e) for e in value) if h]
    return []


async def validate_new_references(
    client, new_obj: Dict, old_obj: Optional[Dict] = None
) -> None:
    """
    Check that references added by a write point at the right object type.

    Only handles not already present in ``old_obj`` are checked, so an
    object holding a pre-existing dangling reference can still be edited
    (including to remove that reference).

    Args:
        client: Gramps API client instance.
        new_obj (Dict): Payload about to be written.
        old_obj (Optional[Dict]): Stored object before the write, if any.

    Raises:
        ValueError: Listing every invalid reference.
    """
    old_obj = old_obj or {}
    problems = []
    checked: Dict[Tuple[str, str], bool] = {}
    for field, target_type in {**REF_LIST_FIELDS, **REF_HANDLE_FIELDS}.items():
        if field not in new_obj:
            continue
        # Reason: Event.place is a handle, but Place has no 'place' field and
        # other classes may reuse the name; only validate string handles.
        if field == "place" and not isinstance(new_obj.get(field), str):
            continue
        existing = set(_field_handles(old_obj, field))
        for handle in _field_handles(new_obj, field):
            if handle in existing:
                continue
            key = (target_type, handle)
            if key not in checked:
                checked[key] = await object_exists(client, target_type, handle)
            if not checked[key]:
                problems.append(f"{field}: '{handle}' is not a {target_type} handle")
    if problems:
        raise ValueError("Invalid references, nothing saved: " + "; ".join(problems))


def strip_for_compare(value: Any) -> Any:
    """
    Drop server-computed keys so a written value can be compared to a read-back.

    Args:
        value (Any): Any JSON value.

    Returns:
        Any: A copy without ``sortval``/``year``/``change`` keys.
    """
    value = copy.deepcopy(value)
    if isinstance(value, dict):
        return {
            k: strip_for_compare(v)
            for k, v in value.items()
            if k not in ("sortval", "year", "change")
        }
    if isinstance(value, list):
        return [strip_for_compare(v) for v in value]
    return value
