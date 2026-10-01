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
batch: many writes in one all-or-nothing database transaction.
find_by_tag: every object carrying a tag, across all types.
"""

from typing import Any, Dict, List, Literal, Optional

from mcp.types import TextContent
from pydantic import BaseModel, Field

from ..batch import BatchPlan, _new_handle
from ..client import GrampsAPIError
from ..raw_objects import TypeMaps, to_raw
from ..references import OBJECT_TYPES, strip_for_compare
from ..write_support import WriteOptions, change_entry, find_tag, render
from .editing import _error, verify_readback
from .search_basic import with_client

EntityName = Literal[
    "person",
    "family",
    "event",
    "place",
    "source",
    "citation",
    "note",
    "media",
    "repository",
]
MAX_OPS = 200
_TYPE_MAPS: Dict[str, TypeMaps] = {}


class BatchOp(BaseModel):
    """One operation inside a batch."""

    op: Literal["create", "update", "delete"] = Field(description="Operation")
    type: EntityName = Field(description="Object type")
    id: Optional[str] = Field(
        None,
        description=(
            "Alias for an object created by this op; later ops refer to it as "
            '"$<id>" (in handle, ref, data values or add_refs)'
        ),
    )
    ref: Optional[str] = Field(
        None, description="Target an object created earlier in the batch, by its id"
    )
    handle: Optional[str] = Field(None, description="Target handle (update/delete)")
    gramps_id: Optional[str] = Field(
        None, description="Target Gramps ID (update/delete)"
    )
    data: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "create: the object's fields in the same JSON form find/get return "
            '(e.g. {"type": "Birth", "date": {"modifier": "about", "start": '
            '"1850"}, "citation_list": ["C0014"]}). gramps_id is assigned if omitted'
        ),
    )
    set_fields: Optional[Dict[str, Any]] = Field(
        None, description="update: as update_<type>"
    )
    clear_fields: Optional[List[str]] = Field(
        None, description="update: as update_<type>"
    )
    add_refs: Optional[Dict[str, List[str]]] = Field(
        None, description="update: as update_<type>; values may be $aliases"
    )
    remove_refs: Optional[Dict[str, List[str]]] = Field(
        None, description="update: as update_<type>"
    )


class BatchParams(WriteOptions):
    """Parameters for batch."""

    ops: List[BatchOp] = Field(
        description=f"Operations, applied in order (1-{MAX_OPS})",
        min_length=1,
        max_length=MAX_OPS,
    )
    message: Optional[str] = Field(
        None, description="Label for the change in the Gramps undo history"
    )


class FindByTagParams(BaseModel):
    """Parameters for find_by_tag."""

    tag: str = Field(description="Tag name")
    types: Optional[List[EntityName]] = Field(
        None, description="Limit to these object types (default: all)"
    )


async def _type_maps(client) -> TypeMaps:
    """Type tables, cached per server."""
    if client.base_url not in _TYPE_MAPS:
        _TYPE_MAPS[client.base_url] = await TypeMaps.load(client)
    return _TYPE_MAPS[client.base_url]


async def _plan(client, params: BatchParams) -> BatchPlan:
    """Simulate every op; raises ValueError on the first invalid op."""
    plan = BatchPlan(client)
    for index, op in enumerate(params.ops):
        try:
            if op.op == "create":
                if op.data is None:
                    raise ValueError("create needs data")
                await plan.create(op.type, op.data, op.id)
                continue
            handle = await plan.locate(op.type, op.handle, op.gramps_id, op.ref)
            if op.op == "update":
                edits = op.model_dump(
                    include={"set_fields", "clear_fields", "add_refs", "remove_refs"}
                )
                if not any(edits.values()):
                    raise ValueError(
                        "update needs set_fields, clear_fields, add_refs or remove_refs"
                    )
                await plan.update(op.type, handle, edits)
            else:
                await plan.delete(op.type, handle)
        except (ValueError, GrampsAPIError) as e:
            raise ValueError(f"op {index} ({op.op} {op.type}): {e}") from e

    if params.tag:
        tag = await find_tag(client, params.tag)
        tag_handle = tag["handle"] if tag else _new_handle()
        if not tag:
            plan.extra_items.append(
                {
                    "type": "add",
                    "_class": "Tag",
                    "handle": tag_handle,
                    "old": None,
                    "new": {
                        "_class": "Tag",
                        "handle": tag_handle,
                        "name": params.tag,
                        "color": "#000000000000",
                        "priority": 0,
                        "change": 0,
                    },
                }
            )
        for handle in plan.targets:
            new = plan.objects[handle]["new"]
            if new is not None and tag_handle not in (new.get("tag_list") or []):
                new["tag_list"] = list(new.get("tag_list") or []) + [tag_handle]
                plan.changed_fields.setdefault(handle, set()).add("tag_list")
        plan.new_tag = None if tag else tag_handle
    await plan.validate()
    return plan


def _changes(plan: BatchPlan) -> List[Dict]:
    """Object-level changes, creates first and deletes last."""
    order = {"create": 0, "update": 1, "delete": 2}
    out = []
    for state in plan.objects.values():
        old, new = state["old"], state["new"]
        if old is None and new is None:
            continue
        if (
            old is not None
            and new is not None
            and strip_for_compare(old) == strip_for_compare(new)
        ):
            continue
        op = "create" if old is None else "delete" if new is None else "update"
        out.append(change_entry(op, state["type"], old, new))
    return sorted(out, key=lambda c: order[c["op"]])


async def _payload(client, plan: BatchPlan) -> List[Dict]:
    """Raw transaction items for POST /transactions/."""
    types = await _type_maps(client)
    items = list(plan.extra_items)
    kinds = {"create": "add", "update": "update", "delete": "delete"}
    for change in _changes(plan):
        state = plan.objects[change["handle"]]
        class_name = OBJECT_TYPES[state["type"]][1]
        old = to_raw(state["old"], class_name, types) if state["old"] else None
        new = to_raw(state["new"], class_name, types) if state["new"] else None
        items.append(
            {
                "type": kinds[change["op"]],
                "_class": class_name,
                "handle": change["handle"],
                "old": old,
                "new": new,
            }
        )
    return items


@with_client
async def batch_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Run create/update/delete ops in one database transaction.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): BatchParams fields.

    Returns:
        List[TextContent]: Headline plus JSON result, or an error. On any
        error nothing has been written.
    """
    try:
        params = BatchParams(**arguments)
        plan = await _plan(client, params)
        changes = _changes(plan)
        counts = {
            k: sum(c["op"] == k for c in changes)
            for k in ("create", "update", "delete")
        }
        summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "no changes"
        if params.dry_run or not changes:
            headline = f"Dry run - nothing written. The batch would make: {summary}."
            if not changes:
                headline = "Nothing to do: the batch makes no changes."
            return [TextContent(type="text", text=render(headline, True, changes))]

        payload = await _payload(client, plan)
        message = params.message or f"MCP batch ({len(params.ops)} ops)"
        if params.tag:
            message += f" [tag: {params.tag}]"
        await client.raw_request(
            "POST", "transactions/", params={"message": message}, json_data=payload
        )

        stored, problems = [], []
        for change in changes:
            state = plan.objects[change["handle"]]
            path = f"{OBJECT_TYPES[state['type']][0]}/{change['handle']}"
            try:
                after = await client.raw_request("GET", path)
            except GrampsAPIError as e:
                if change["op"] == "delete" and "not found" in str(e).lower():
                    continue
                raise
            if change["op"] == "delete":
                problems.append(f"{change['type']} {change['gramps_id']} still exists")
                continue
            stored.append(after)
            fields = sorted(
                plan.changed_fields.get(change["handle"], set()) - {"handle"}
            )
            mismatched = verify_readback(state["new"], after, fields)
            if mismatched:
                problems.append(
                    f"{change['type']} {change['gramps_id']}: read-back differs in "
                    + ", ".join(mismatched)
                )
        headline = f"Batch committed in one transaction: {summary}. " + (
            "Verified by read-back." if not problems else "Error: read-back problems."
        )
        return [
            TextContent(
                type="text",
                text=render(headline, False, changes, stored=stored, warnings=problems),
            )
        ]
    except Exception as e:
        return _error(e, "batch (nothing was written)")


@with_client
async def find_by_tag_tool(client, arguments: Dict) -> List[TextContent]:
    """
    List every object carrying a tag.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): FindByTagParams fields.

    Returns:
        List[TextContent]: One line per object, grouped by type.
    """
    try:
        params = FindByTagParams(**arguments)
        tag = await find_tag(client, params.tag)
        if not tag:
            return [TextContent(type="text", text=f"No tag named '{params.tag}'.")]
        lines, total = [], 0
        for entity_type in params.types or list(OBJECT_TYPES):
            collection = OBJECT_TYPES[entity_type][0]
            rows = await client.raw_request(
                "GET", f"{collection}/", params={"keys": "handle,gramps_id,tag_list"}
            )
            hits = [r for r in rows or [] if tag["handle"] in (r.get("tag_list") or [])]
            if hits:
                lines.append(f"{entity_type} ({len(hits)}):")
                lines += [f"- {r.get('gramps_id')} [{r['handle']}]" for r in hits]
                total += len(hits)
        head = f"Tag '{params.tag}' [{tag['handle']}]: {total} object(s)"
        return [TextContent(type="text", text="\n".join([head, *lines]))]
    except Exception as e:
        return _error(e, "find_by_tag")
