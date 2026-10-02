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
check_references: a read-only integrity report for the whole tree.

Every object is read once and every reference, at any depth (including
citation_list inside names, event references and attributes), is checked:

- dangling: the handle does not exist at all;
- wrong type: the handle exists but is a different kind of object (e.g. a
  note handle in a citation_list);
- orphans: people in no family, families with no members, events no person
  or family uses, citations nothing cites;
- family link mismatches: a family lists a person whose family_list /
  parent_family_list does not list the family back, or vice versa.
"""

import json
from typing import Any, Dict, Iterator, List, Tuple

from mcp.types import TextContent
from pydantic import BaseModel, Field

from ..references import OBJECT_TYPES, REF_HANDLE_FIELDS, REF_LIST_FIELDS, ref_handle
from .editing import _error
from .search_basic import with_client

PAGE = 500
SHOWN_PER_CATEGORY = 50
NESTED_HANDLE_FIELDS = {**REF_HANDLE_FIELDS, "famc": "family"}


class CheckReferencesParams(BaseModel):
    """Parameters for check_references."""

    max_listed: int = Field(
        SHOWN_PER_CATEGORY,
        ge=1,
        le=1000,
        description="Maximum items listed per category in the text summary",
    )


def iter_refs(value: Any, path: str = "") -> Iterator[Tuple[str, str, str]]:
    """
    Yield (path, target type, handle) for every reference in an object.

    Args:
        value (Any): Object or nested part of one (API JSON form).
        path (str): Location prefix for reporting.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}.{key}" if path else key
            if key in REF_LIST_FIELDS and isinstance(item, list):
                for index, entry in enumerate(item):
                    handle = ref_handle(entry)
                    if handle:
                        yield f"{where}[{index}]", REF_LIST_FIELDS[key], handle
                    if isinstance(entry, dict):
                        yield from iter_refs(entry, f"{where}[{index}]")
            elif key in NESTED_HANDLE_FIELDS and isinstance(item, str):
                if item:
                    yield where, NESTED_HANDLE_FIELDS[key], item
            elif isinstance(item, (dict, list)):
                yield from iter_refs(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_refs(item, f"{path}[{index}]")


async def _load_all(client, collection: str) -> List[Dict]:
    rows, page = [], 1
    while True:
        batch = await client.raw_request(
            "GET", f"{collection}/", params={"page": page, "pagesize": PAGE}
        )
        rows.extend(batch or [])
        if not batch or len(batch) < PAGE:
            return rows
        page += 1


def analyse(objects: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
    """
    Pure integrity analysis over every object of every type.

    Args:
        objects (Dict[str, List[Dict]]): entity type (and "tag") -> objects.

    Returns:
        Dict[str, List[Dict]]: Findings per category.
    """
    owner = {}  # handle -> type
    for entity_type, rows in objects.items():
        for row in rows:
            owner[row["handle"]] = entity_type
    label = {
        row["handle"]: row.get("gramps_id") or row.get("name") or row["handle"]
        for rows in objects.values()
        for row in rows
    }

    findings: Dict[str, List[Dict]] = {
        "dangling": [],
        "wrong_type": [],
        "orphan_people": [],
        "orphan_families": [],
        "orphan_events": [],
        "orphan_citations": [],
        "family_link_mismatches": [],
    }
    used_by: Dict[str, int] = {}
    for entity_type, rows in objects.items():
        if entity_type == "tag":
            continue
        for row in rows:
            for path, target, handle in iter_refs(row):
                found = owner.get(handle)
                used_by[handle] = used_by.get(handle, 0) + 1
                item = {
                    "type": entity_type,
                    "gramps_id": row.get("gramps_id"),
                    "handle": row["handle"],
                    "field": path,
                    "ref": handle,
                    "expected": target,
                }
                if found is None:
                    findings["dangling"].append(item)
                elif found != target:
                    findings["wrong_type"].append(
                        {**item, "actual": found, "actual_id": label.get(handle)}
                    )

    for person in objects.get("person", []):
        if not person.get("family_list") and not person.get("parent_family_list"):
            findings["orphan_people"].append(_brief("person", person))
    for family in objects.get("family", []):
        if not (
            family.get("father_handle")
            or family.get("mother_handle")
            or family.get("child_ref_list")
        ):
            findings["orphan_families"].append(_brief("family", family))
    event_users = {
        ref_handle(r)
        for kind in ("person", "family")
        for row in objects.get(kind, [])
        for r in row.get("event_ref_list") or []
    }
    for event in objects.get("event", []):
        if event["handle"] not in event_users:
            findings["orphan_events"].append(_brief("event", event))
    for citation in objects.get("citation", []):
        if not used_by.get(citation["handle"]):
            findings["orphan_citations"].append(_brief("citation", citation))

    people = {p["handle"]: p for p in objects.get("person", [])}
    for family in objects.get("family", []):
        fh = family["handle"]
        for role in ("father_handle", "mother_handle"):
            member = people.get(family.get(role) or "")
            if member and fh not in (member.get("family_list") or []):
                findings["family_link_mismatches"].append(
                    _mismatch(family, member, f"{role} not in person's family_list")
                )
        for child_ref in family.get("child_ref_list") or []:
            child = people.get(ref_handle(child_ref) or "")
            if child and fh not in (child.get("parent_family_list") or []):
                findings["family_link_mismatches"].append(
                    _mismatch(
                        family, child, "child not linked back (parent_family_list)"
                    )
                )
    families = {f["handle"]: f for f in objects.get("family", [])}
    for person in people.values():
        for fh in person.get("family_list") or []:
            family = families.get(fh)
            if family and person["handle"] not in (
                family.get("father_handle"),
                family.get("mother_handle"),
            ):
                findings["family_link_mismatches"].append(
                    _mismatch(
                        family, person, "family_list entry, but not a parent there"
                    )
                )
        for fh in person.get("parent_family_list") or []:
            family = families.get(fh)
            if family and person["handle"] not in [
                ref_handle(c) for c in family.get("child_ref_list") or []
            ]:
                findings["family_link_mismatches"].append(
                    _mismatch(
                        family,
                        person,
                        "parent_family_list entry, but not a child there",
                    )
                )
    return findings


def _brief(entity_type: str, row: Dict) -> Dict:
    return {
        "type": entity_type,
        "gramps_id": row.get("gramps_id"),
        "handle": row["handle"],
    }


def _mismatch(family: Dict, person: Dict, problem: str) -> Dict:
    return {
        "family": family.get("gramps_id"),
        "family_handle": family["handle"],
        "person": person.get("gramps_id"),
        "person_handle": person["handle"],
        "problem": problem,
    }


def _line(category: str, item: Dict) -> str:
    if category in ("dangling", "wrong_type"):
        text = f"- {item['type']} {item['gramps_id']} {item['field']} -> {item['ref']}"
        if category == "wrong_type":
            text += (
                f" is {item['actual']} {item['actual_id']}, expected {item['expected']}"
            )
        return text
    if category == "family_link_mismatches":
        return f"- family {item['family']} / person {item['person']}: {item['problem']}"
    return f"- {item['type']} {item['gramps_id']} [{item['handle']}]"


@with_client
async def check_references_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Read every object and report reference problems. Never writes.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): CheckReferencesParams fields.

    Returns:
        List[TextContent]: Summary per category plus the full JSON findings.
    """
    try:
        params = CheckReferencesParams(**(arguments or {}))
        objects = {
            entity_type: await _load_all(client, collection)
            for entity_type, (collection, _) in OBJECT_TYPES.items()
        }
        objects["tag"] = await client.raw_request("GET", "tags/") or []
        findings = analyse(objects)
        scanned = sum(len(v) for k, v in objects.items() if k != "tag")
        lines = [f"Checked {scanned:,} objects (read-only)."]
        for category, items in findings.items():
            lines.append(f"\n{category.replace('_', ' ')}: {len(items)}")
            lines += [_line(category, i) for i in items[: params.max_listed]]
            if len(items) > params.max_listed:
                lines.append(f"- ... {len(items) - params.max_listed} more in the JSON")
        body = json.dumps(findings, ensure_ascii=False, indent=1)
        return [
            TextContent(
                type="text", text="\n".join(lines) + f"\n\n```json\n{body}\n```"
            )
        ]
    except Exception as e:
        return _error(e, "check_references")
