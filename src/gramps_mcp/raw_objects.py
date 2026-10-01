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
Convert the API's simplified object JSON into raw Gramps dicts.

The object endpoints (GET/POST/PUT /people/ ...) use a simplified form:
Gramps types are plain strings ("Birth") and ``_class`` keys are dropped.
The raw ``POST /transactions/`` endpoint, which applies many writes in one
database transaction, needs the full form instead. This is a port of
``fix_object_dict`` from gramps-web-api (AGPL-3.0), with the string -> value
type tables fetched from the server's own ``/types/default/<type>/map``
rather than importing the Gramps library (which pulls in GTK/GLib).
"""

from typing import Any, Dict, Optional

# Keys the API emits on read but that cannot be stored (computed properties).
COMPUTED_KEYS = {"Date": {"year"}}

# Simplified-form Gramps type class -> /types/default/<datatype>
TYPE_DATATYPES = {
    "EventType": "event_types",
    "EventRoleType": "event_role_types",
    "FamilyRelType": "family_relation_types",
    "ChildRefType": "child_reference_types",
    "NameType": "name_types",
    "NameOriginType": "name_origin_types",
    "AttributeType": "attribute_types",
    "SrcAttributeType": "source_attribute_types",
    "NoteType": "note_types",
    "PlaceType": "place_types",
    "RepositoryType": "repository_types",
    "SourceMediaType": "source_media_types",
    "UrlType": "url_types",
}

# StyledTextTagType is not exposed by /types/; values from gramps.gen.lib.
STYLED_TEXT_TAG_TYPES = {
    "bold": 0,
    "italic": 1,
    "underline": 2,
    "fontface": 3,
    "fontsize": 4,
    "fontcolor": 5,
    "highlight": 6,
    "superscript": 7,
    "link": 8,
    "strikethrough": 9,
    "subscript": 10,
}

_CHILD_CLASSES = {
    "date": "Date",
    "media_list": "MediaRef",
    "child_ref_list": "ChildRef",
    "event_ref_list": "EventRef",
    "address_list": "Address",
    "urls": "Url",
    "lds_ord_list": "LdsOrd",
    "person_ref_list": "PersonRef",
    "surname_list": "Surname",
    "text": "StyledText",
    "place_type": "PlaceType",
    "alt_loc": "Location",
    "reporef_list": "RepoRef",
    "placeref_list": "PlaceRef",
    "tags": "StyledTextTag",
    "alt_names": "PlaceName",
    "primary_name": "Name",
    "alternate_names": "Name",
}


def _child_class(parent: str, key: str) -> Optional[str]:
    """Class name of an embedded object, mirroring gramps-web-api."""
    if key == "name" and parent == "Place":
        return "PlaceName"
    if key == "attribute_list":
        return "SrcAttribute" if parent in ("Citation", "Source") else "Attribute"
    return _CHILD_CLASSES.get(key)


def _type_class(parent: str, key: str) -> Optional[str]:
    """Gramps type class for a type-valued key, or None if not a type key."""
    if key in ("type", "place_type", "media_type", "frel", "mrel"):
        if parent == "Family":
            return "FamilyRelType"
        if parent == "RepoRef":
            return "SourceMediaType"
        if key in ("frel", "mrel"):
            return "ChildRefType"
        if key == "place_type":
            return "PlaceType"
        return f"{parent}Type"
    if key == "role":
        return "EventRoleType"
    if key == "origintype":
        return "NameOriginType"
    if key == "name" and parent == "StyledTextTag":
        return "StyledTextTagType"
    return None


class TypeMaps:
    """String -> value tables for Gramps types, loaded once from the server."""

    def __init__(self, tables: Dict[str, Dict[str, int]]):
        """
        Args:
            tables (Dict[str, Dict[str, int]]): type class -> {name: value}.
        """
        self.tables = {"StyledTextTagType": STYLED_TEXT_TAG_TYPES, **tables}

    @classmethod
    async def load(cls, client) -> "TypeMaps":
        """Fetch every type table from /types/default/<datatype>/map."""
        tables = {}
        for type_class, datatype in TYPE_DATATYPES.items():
            mapping = await client.raw_request("GET", f"types/default/{datatype}/map")
            tables[type_class] = {name: int(value) for value, name in mapping.items()}
        return cls(tables)

    def to_raw(self, type_class: str, value: Any) -> Any:
        """Turn a type string into Gramps' {_class, value, string} dict."""
        if not isinstance(value, str):
            return value
        table = self.tables.get(type_class, {})
        # Reason: the custom code differs per type (EventType 0, FamilyRelType
        # 4) and some types have none (StyledTextTagType 0 is "bold").
        custom = table.get("Custom")
        for candidate in (value, value.lower()):
            lookup = (
                table
                if candidate == value
                else {k.lower(): v for k, v in table.items()}
            )
            if candidate in lookup and lookup[candidate] != custom:
                return {"_class": type_class, "value": lookup[candidate], "string": ""}
        if custom is None:
            raise ValueError(f"Unknown {type_class} '{value}'")
        return {"_class": type_class, "value": custom, "string": value}


def to_raw(obj: Dict[str, Any], class_name: str, types: TypeMaps) -> Dict[str, Any]:
    """
    Restore an object in simplified form to raw Gramps form.

    Args:
        obj (Dict[str, Any]): Object as returned by GET or built by a tool.
        class_name (str): Gramps class of ``obj`` (e.g. "Person").
        types (TypeMaps): Type tables from the server.

    Returns:
        Dict[str, Any]: Raw dict accepted by POST /transactions/.
    """
    out: Dict[str, Any] = {"_class": class_name}
    dropped = COMPUTED_KEYS.get(class_name, set())
    for key, value in obj.items():
        if key == "_class" or key in dropped:
            continue
        type_class = _type_class(class_name, key)
        if type_class:
            out[key] = types.to_raw(type_class, value)
        elif key in ("rect", "mother_handle", "father_handle", "famc") and not value:
            out[key] = None
        elif key == "date" and value is None:
            out[key] = {"_class": "Date", "dateval": [0, 0, 0, False]}
        elif isinstance(value, dict):
            child = _child_class(class_name, key) or value.get("_class")
            out[key] = to_raw(value, child, types) if child else value
        elif isinstance(value, list):
            child = _child_class(class_name, key)
            out[key] = [
                (
                    to_raw(item, child or item.get("_class"), types)
                    if isinstance(item, dict) and (child or item.get("_class"))
                    else item
                )
                for item in value
            ]
        else:
            out[key] = value
    return out
