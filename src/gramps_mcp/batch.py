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
Plan a batch of create/update/delete operations as ONE database transaction.

The batch is simulated against a local copy of every object it touches, then
sent to ``POST /transactions/``, which the server applies inside a single
``DbTxn``: if any item fails (including an object changed by someone else
since it was read), nothing is written.

That endpoint writes objects exactly as given, so this module does the
bookkeeping the per-object endpoints normally do on the server:

- family membership: a family's father/mother/children and each person's
  ``family_list`` / ``parent_family_list`` are kept in sync;
- a person's ``birth_ref_index`` / ``death_ref_index`` are recomputed when
  their events (or those events' types) change;
- deletes remove every reference to the deleted object, delete families left
  with no members when a person is deleted, and delete a source's citations
  when the source is deleted.
"""

import copy
import re
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from .client import GrampsAPIError
from .dates import normalize_dates
from .references import (
    OBJECT_TYPES,
    REF_HANDLE_FIELDS,
    REF_LIST_FIELDS,
    get_by_gramps_id,
    object_exists,
    ref_handle,
    remove_handle_refs,
)
from .tools.editing import apply_edits

ID_PREFIXES = {
    "person": "I",
    "family": "F",
    "event": "E",
    "place": "P",
    "source": "S",
    "citation": "C",
    "note": "N",
    "media": "O",
    "repository": "R",
}
CLASS_TO_TYPE = {cls: t for t, (_, cls) in OBJECT_TYPES.items()}


def _new_handle() -> str:
    """A fresh object handle (28 hex chars, like Gramps' own)."""
    return uuid.uuid4().hex[:28]


def substitute_aliases(value: Any, aliases: Dict[str, str]) -> Any:
    """Replace "$alias" strings (anywhere in a value) by the aliased handle."""
    if isinstance(value, str) and value.startswith("$"):
        if value[1:] not in aliases:
            raise ValueError(
                f"Unknown batch alias '{value}' (define it on an earlier op)"
            )
        return aliases[value[1:]]
    if isinstance(value, dict):
        return {k: substitute_aliases(v, aliases) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_aliases(v, aliases) for v in value]
    return value


def _handles_in(obj: Dict) -> Set[str]:
    """Top-level referenced handles of an object."""
    found = set()
    for field in REF_LIST_FIELDS:
        found.update(h for h in (ref_handle(e) for e in obj.get(field) or []) if h)
    for field in REF_HANDLE_FIELDS:
        if isinstance(obj.get(field), str) and obj.get(field):
            found.add(obj[field])
    return found


class BatchPlan:
    """Simulated state of every object a batch touches."""

    def __init__(self, client):
        """
        Args:
            client: Gramps API client instance.
        """
        self.client = client
        # handle -> {"type", "old": stored object or None, "new": object or None}
        self.objects: Dict[str, Dict[str, Any]] = {}
        self.aliases: Dict[str, str] = {}
        self.targets: List[str] = []  # handles named by an op, in order
        self.changed_fields: Dict[str, Set[str]] = {}
        self._ids: Dict[str, Tuple[Set[str], int, int]] = {}
        self.extra_items: List[Dict] = []  # raw items such as a new Tag
        self.new_tag: Optional[str] = None  # handle of a tag created by the batch

    # -- reading ---------------------------------------------------------

    async def get(self, entity_type: str, handle: str) -> Optional[Dict]:
        """Current simulated state of an object (None if deleted)."""
        if handle not in self.objects:
            collection = OBJECT_TYPES[entity_type][0]
            try:
                stored = await self.client.raw_request("GET", f"{collection}/{handle}")
            except GrampsAPIError as e:
                if "not found" in str(e).lower():
                    raise ValueError(f"No {entity_type} with handle {handle}") from e
                raise
            self.objects[handle] = {
                "type": entity_type,
                "old": stored,
                "new": copy.deepcopy(stored),
            }
        state = self.objects[handle]
        if state["type"] != entity_type:
            raise ValueError(f"{handle} is a {state['type']}, not a {entity_type}")
        return state["new"]

    async def locate(
        self,
        entity_type: str,
        handle: Optional[str],
        gramps_id: Optional[str],
        alias: Optional[str],
    ) -> str:
        """Resolve an op's target to a handle."""
        if alias:
            if alias not in self.aliases:
                raise ValueError(f"Unknown batch alias '${alias}'")
            return self.aliases[alias]
        if handle:
            return substitute_aliases(handle, self.aliases)
        if gramps_id:
            for h, state in self.objects.items():
                if (
                    state["new"]
                    and state["new"].get("gramps_id") == gramps_id
                    and (state["type"] == entity_type)
                ):
                    return h
            obj = await get_by_gramps_id(self.client, entity_type, gramps_id)
            if not obj:
                raise ValueError(f"No {entity_type} with Gramps ID {gramps_id}")
            return obj["handle"]
        raise ValueError("Each update/delete op needs handle, gramps_id or ref")

    async def resolve_ref(
        self, target_type: str, value: str, current: List[str]
    ) -> str:
        """Handle-or-Gramps-ID-or-$alias to a handle that exists after the batch."""
        value = substitute_aliases(value, self.aliases)
        if value in current:
            return value
        state = self.objects.get(value)
        if state:
            if state["new"] is None:
                raise ValueError(f"{value} is deleted earlier in this batch")
            return value
        if target_type != "tag" and not re.fullmatch(r"[0-9a-f]{20,}", value):
            obj = await get_by_gramps_id(self.client, target_type, value)
            if obj:
                return obj["handle"]
        if await object_exists(self.client, target_type, value):
            return value
        raise ValueError(
            f"'{value}' is not the handle or Gramps ID of any {target_type}"
        )

    # -- ids -------------------------------------------------------------

    async def next_gramps_id(self, entity_type: str) -> str:
        """Next free Gramps ID, following the tree's existing numbering."""
        prefix = ID_PREFIXES[entity_type]
        if entity_type not in self._ids:
            rows = await self.client.raw_request(
                "GET", f"{OBJECT_TYPES[entity_type][0]}/", params={"keys": "gramps_id"}
            )
            used = {r.get("gramps_id") for r in rows or [] if r.get("gramps_id")}
            numbers = [
                (len(m.group(1)), int(m.group(1)))
                for m in (re.fullmatch(prefix + r"(\d+)", i) for i in used)
                if m
            ]
            width = max([w for w, _ in numbers] or [4])
            top = max([n for _, n in numbers] or [-1])
            self._ids[entity_type] = (used, width, top)
        used, width, top = self._ids[entity_type]
        while True:
            top += 1
            candidate = f"{prefix}{top:0{width}d}"
            if candidate not in used:
                used.add(candidate)
                self._ids[entity_type] = (used, width, top)
                return candidate

    # -- operations --------------------------------------------------------

    def _mark(self, handle: str, fields) -> None:
        self.changed_fields.setdefault(handle, set()).update(fields)

    async def create(self, entity_type: str, data: Dict, alias: Optional[str]) -> str:
        """Add a new object to the plan."""
        obj = normalize_dates(substitute_aliases(copy.deepcopy(data), self.aliases))
        for key in ("handle", "_class", "change"):
            obj.pop(key, None)
        handle = _new_handle()
        obj["handle"] = handle
        obj["gramps_id"] = obj.get("gramps_id") or await self.next_gramps_id(
            entity_type
        )
        for field in REF_LIST_FIELDS:
            if field in obj:
                obj[field] = [
                    (
                        {
                            **e,
                            "ref": await self.resolve_ref(
                                REF_LIST_FIELDS[field], e["ref"], []
                            ),
                        }
                        if isinstance(e, dict)
                        else await self.resolve_ref(REF_LIST_FIELDS[field], e, [])
                    )
                    for e in obj[field]
                ]
        for field, target in REF_HANDLE_FIELDS.items():
            if isinstance(obj.get(field), str) and obj[field]:
                obj[field] = await self.resolve_ref(target, obj[field], [])
        self.objects[handle] = {"type": entity_type, "old": None, "new": obj}
        if alias:
            self.aliases[alias] = handle
        self.targets.append(handle)
        self._mark(handle, obj.keys())
        if entity_type == "family":
            await self._sync_family(handle, None, obj)
        if entity_type == "person":
            await self._set_birth_death(handle)
        return handle

    async def update(self, entity_type: str, handle: str, edits: Dict) -> None:
        """Apply update_<type>-style edits to an object in the plan."""
        current = await self.get(entity_type, handle)
        if current is None:
            raise ValueError(f"{entity_type} {handle} is deleted earlier in this batch")
        refs = {}
        for key in ("add_refs", "remove_refs"):
            resolved = {}
            for field, values in (edits.get(key) or {}).items():
                if field not in REF_LIST_FIELDS:
                    raise ValueError(f"'{field}' is not a reference list")
                present = [ref_handle(e) for e in current.get(field) or []]
                resolved[field] = [
                    await self.resolve_ref(REF_LIST_FIELDS[field], v, present)
                    for v in values
                ]
            refs[key] = resolved
        set_fields = substitute_aliases(edits.get("set_fields") or {}, self.aliases)
        new_obj, changed, _ = apply_edits(
            current,
            entity_type,
            set_fields,
            edits.get("clear_fields"),
            refs["add_refs"],
            refs["remove_refs"],
        )
        self.objects[handle]["new"] = new_obj
        if handle not in self.targets:
            self.targets.append(handle)
        self._mark(handle, changed)
        if entity_type == "family":
            await self._sync_family(handle, current, new_obj)
        if entity_type == "person" and "event_ref_list" in changed:
            await self._set_birth_death(handle)
        if entity_type == "event" and current.get("type") != new_obj.get("type"):
            for other, state in list(self.objects.items()):
                if (
                    state["type"] == "person"
                    and state["new"]
                    and handle in _handles_in(state["new"])
                ):
                    await self._set_birth_death(other)
            for person_handle in await self._backlinks("event", handle, "person"):
                await self._set_birth_death(person_handle)

    async def delete(self, entity_type: str, handle: str) -> None:
        """Remove an object and every reference to it from the plan."""
        current = await self.get(entity_type, handle)
        if current is None:
            return
        if handle not in self.targets:
            self.targets.append(handle)
        if entity_type == "source":
            for citation in await self._backlinks("source", handle, "citation"):
                await self.delete("citation", citation)
        self.objects[handle]["new"] = None

        referrers = set(await self._backlinks(entity_type, handle))
        referrers |= {
            h
            for h, s in self.objects.items()
            if s["new"] and handle in _handles_in(s["new"])
        }
        for ref in referrers:
            ref_type = await self._type_of(ref)
            obj = await self.get(ref_type, ref) if ref_type else None
            if obj is None:
                continue
            cleaned = remove_handle_refs(obj, handle)
            if cleaned != obj:
                self.objects[ref]["new"] = cleaned
                self._mark(ref, [k for k in cleaned if cleaned[k] != obj.get(k)])
                if ref_type == "person" and entity_type == "event":
                    await self._set_birth_death(ref)
                if ref_type == "family" and entity_type == "person":
                    members = (
                        cleaned.get("father_handle"),
                        cleaned.get("mother_handle"),
                        cleaned.get("child_ref_list"),
                    )
                    if not any(members):
                        await self.delete("family", ref)

    # -- bookkeeping ---------------------------------------------------------

    async def _type_of(self, handle: str) -> Optional[str]:
        if handle in self.objects:
            return self.objects[handle]["type"]
        return None

    async def _backlinks(
        self, entity_type: str, handle: str, only: Optional[str] = None
    ) -> List[str]:
        """Handles of stored objects referencing this one (registers their type)."""
        state = self.objects.get(handle)
        if state and state["old"] is None:
            return []  # created in this batch: nothing stored references it
        collection = OBJECT_TYPES[entity_type][0]
        obj = await self.client.raw_request(
            "GET", f"{collection}/{handle}", params={"backlinks": "true"}
        )
        found = []
        for ref_type, handles in (obj.get("backlinks") or {}).items():
            if ref_type not in OBJECT_TYPES or (only and ref_type != only):
                continue
            for h in handles:
                if h not in self.objects:
                    await self.get(ref_type, h)
                found.append(h)
        return found

    async def _sync_family(
        self, handle: str, before: Optional[Dict], after: Dict
    ) -> None:
        """Keep members' family_list / parent_family_list in step with a family."""
        before = before or {}
        for field in ("father_handle", "mother_handle"):
            old, new = before.get(field) or "", after.get(field) or ""
            if old != new:
                if old:
                    await self._edit_person_list(old, "family_list", handle, add=False)
                if new:
                    await self._edit_person_list(new, "family_list", handle, add=True)
        old_kids = {ref_handle(c) for c in before.get("child_ref_list") or []}
        new_kids = {ref_handle(c) for c in after.get("child_ref_list") or []}
        for kid in old_kids - new_kids:
            await self._edit_person_list(kid, "parent_family_list", handle, add=False)
        for kid in new_kids - old_kids:
            await self._edit_person_list(kid, "parent_family_list", handle, add=True)

    async def _edit_person_list(
        self, person: str, field: str, family: str, add: bool
    ) -> None:
        try:
            obj = await self.get("person", person)
        except ValueError:
            if add:
                raise
            return  # already-broken reference: nothing to update
        if obj is None:
            if add:
                raise ValueError(f"Person {person} is deleted earlier in this batch")
            return
        current = list(obj.get(field) or [])
        if add and family not in current:
            current.append(family)
        elif not add and family in current:
            current.remove(family)
        else:
            return
        self.objects[person]["new"] = {**obj, field: current}
        self._mark(person, [field])

    async def _set_birth_death(self, person: str) -> None:
        """Recompute birth/death_ref_index the way Gramps does."""
        obj = await self.get("person", person)
        if obj is None:
            return
        birth = death = -1
        for index, ref in enumerate(obj.get("event_ref_list") or []):
            role = ref.get("role")
            role = role.get("string") if isinstance(role, dict) else role
            if role not in (None, "", "Primary"):
                continue
            try:
                event = await self.get("event", ref["ref"])
            except ValueError:
                continue
            if not event:
                continue
            kind = event.get("type")
            kind = kind.get("string") if isinstance(kind, dict) else kind
            if kind == "Birth" and birth == -1:
                birth = index
            elif kind == "Death" and death == -1:
                death = index
        if (obj.get("birth_ref_index"), obj.get("death_ref_index")) != (birth, death):
            self.objects[person]["new"] = {
                **obj,
                "birth_ref_index": birth,
                "death_ref_index": death,
            }
            self._mark(person, ["birth_ref_index", "death_ref_index"])

    # -- validation ---------------------------------------------------------

    async def validate(self) -> None:
        """Every reference the batch adds must exist afterwards, with the right type."""
        problems = []
        for handle, state in self.objects.items():
            new = state["new"]
            if new is None:
                continue
            old_refs = _handles_in(state["old"] or {})
            for field, target in {**REF_LIST_FIELDS, **REF_HANDLE_FIELDS}.items():
                if field == "place" and not isinstance(new.get(field), str):
                    continue
                values = (
                    [ref_handle(e) for e in new.get(field) or []]
                    if field in REF_LIST_FIELDS
                    else [new.get(field)]
                )
                for ref in values:
                    if not ref or ref in old_refs:
                        continue
                    other = self.objects.get(ref)
                    if target == "tag" and ref == self.new_tag:
                        ok = True
                    elif other:
                        ok = other["new"] is not None and (
                            other["type"] == target or target == "tag"
                        )
                    else:
                        ok = await object_exists(self.client, target, ref)
                    if not ok:
                        problems.append(
                            f"{state['type']} {new.get('gramps_id')}: {field} '{ref}' "
                            f"is not an existing {target}"
                        )
        if problems:
            raise ValueError(
                "Invalid references, nothing saved: " + "; ".join(problems)
            )
