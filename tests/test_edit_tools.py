"""
Integration tests for update_<type>, delete_<type>, get_note_full and
append_note against a real Gramps Web API.

These tests create and delete objects. They only run against a local
scratch server (GRAMPS_API_URL on localhost/127.0.0.1) unless
GRAMPS_ALLOW_DESTRUCTIVE_TESTS=1 is set, so a .env pointing at a real tree
cannot be modified by accident.
"""

import json
import os
import struct
import tempfile
import uuid
import zlib
from urllib.parse import urlparse

import pytest
from dotenv import load_dotenv

from src.gramps_mcp.client import GrampsWebAPIClient
from src.gramps_mcp.server import TOOL_REGISTRY
from src.gramps_mcp.tools.data_management import create_event_tool
from src.gramps_mcp.tools.search_basic import find_type_tool

load_dotenv()

_host = urlparse(os.environ.get("GRAMPS_API_URL", "")).hostname
pytestmark = pytest.mark.skipif(
    _host not in ("localhost", "127.0.0.1", "::1")
    and os.environ.get("GRAMPS_ALLOW_DESTRUCTIVE_TESTS") != "1",
    reason="destructive tests only run against a local scratch Gramps Web",
)

RUN = uuid.uuid4().hex[:6]


async def call(tool: str, args: dict) -> str:
    """Invoke a registered tool the way the MCP layer does and return its text."""
    result = await TOOL_REGISTRY[tool]["handler"](args)
    return result[0].text


@pytest.fixture
async def api():
    client = GrampsWebAPIClient()
    yield client
    await client.close()


async def post(api, collection: str, obj: dict) -> dict:
    """Create an object directly via the API and return the stored object."""
    result = await api.raw_request("POST", f"{collection}/", json_data=obj)
    created = [r["new"] for r in result if r.get("new")]
    wanted = obj["_class"]
    return next(o for o in created if o["_class"] == wanted)


async def get(api, collection: str, handle: str) -> dict:
    return await api.raw_request("GET", f"{collection}/{handle}")


def person(first: str) -> dict:
    return {
        "_class": "Person",
        "gender": 1,
        "primary_name": {
            "_class": "Name",
            "first_name": first,
            "surname_list": [{"_class": "Surname", "surname": f"Scratch{RUN}"}],
        },
    }


async def make_source_and_citation(api, page: str = "p. 1"):
    source = await post(api, "sources", {"_class": "Source", "title": f"Src {RUN}"})
    citation = await post(
        api,
        "citations",
        {"_class": "Citation", "source_handle": source["handle"], "page": page},
    )
    return source, citation


# ---------------------------------------------------------------------------
# update: remove, clear, dangling refs, validation
# ---------------------------------------------------------------------------


async def test_detach_citation_by_gramps_id(api):
    _, c1 = await make_source_and_citation(api, "keep")
    _, c2 = await make_source_and_citation(api, "detach")
    event = await post(
        api,
        "events",
        {"_class": "Event", "type": "Birth", "citation_list": [c1["handle"], c2["handle"]]},
    )
    text = await call(
        "update_event",
        {"gramps_id": event["gramps_id"], "remove_refs": {"citation_list": [c2["gramps_id"]]}},
    )
    assert "Verified by read-back" in text, text
    assert (await get(api, "events", event["handle"]))["citation_list"] == [c1["handle"]]


async def test_clear_date_and_place(api):
    place = await post(
        api, "places", {"_class": "Place", "name": {"_class": "PlaceName", "value": f"P{RUN}"}}
    )
    event = await post(
        api,
        "events",
        {
            "_class": "Event",
            "type": "Death",
            "place": place["handle"],
            "date": {"_class": "Date", "dateval": [3, 4, 1861, False]},
        },
    )
    text = await call("update_event", {"handle": event["handle"], "clear_fields": ["date", "place"]})
    assert "Verified by read-back" in text, text
    stored = await get(api, "events", event["handle"])
    assert stored["date"]["dateval"] == [0, 0, 0, False]
    assert stored["date"]["sortval"] == 0
    assert stored["place"] == ""


async def test_set_date_server_computes_sortval(api):
    event = await post(api, "events", {"_class": "Event", "type": "Birth"})
    text = await call(
        "update_event",
        {"handle": event["handle"], "set_fields": {"date": {"dateval": [0, 0, 1850, False], "modifier": 3}}},
    )
    assert "Verified by read-back" in text, text
    stored = (await get(api, "events", event["handle"]))["date"]
    assert stored["modifier"] == 3 and stored["sortval"] > 0


async def test_dangling_reference_can_be_removed_and_object_still_editable(api):
    event = await post(api, "events", {"_class": "Event", "type": "Birth"})
    # Simulate a legacy dangling reference written before validation existed.
    raw = await get(api, "events", event["handle"])
    raw["citation_list"] = ["0000deadbeef0000dangling"]
    await api.raw_request("PUT", f"events/{event['handle']}", json_data=raw)

    text = await call("update_event", {"handle": event["handle"], "set_fields": {"description": "edited"}})
    assert "Verified by read-back" in text, text

    text = await call(
        "update_event",
        {"handle": event["handle"], "remove_refs": {"citation_list": ["0000deadbeef0000dangling"]}},
    )
    assert "Verified by read-back" in text, text
    assert (await get(api, "events", event["handle"]))["citation_list"] == []


async def test_wrong_type_in_citation_list_rejected_on_update(api):
    event = await post(api, "events", {"_class": "Event", "type": "Birth"})
    note = await post(
        api, "notes", {"_class": "Note", "text": {"_class": "StyledText", "string": "n"}}
    )
    text = await call(
        "update_event",
        {"handle": event["handle"], "set_fields": {"citation_list": [note["handle"]]}},
    )
    assert text.startswith("Error:") and "not a citation" in text, text
    assert (await get(api, "events", event["handle"]))["citation_list"] == []

    text = await call(
        "update_event",
        {"handle": event["handle"], "add_refs": {"citation_list": [note["gramps_id"]]}},
    )
    assert text.startswith("Error:") and "citation" in text, text


async def test_wrong_type_in_citation_list_rejected_on_create(api):
    note = await post(
        api, "notes", {"_class": "Note", "text": {"_class": "StyledText", "string": "n"}}
    )
    text = (
        await create_event_tool(
            {"type": "Birth", "description": f"bad-{RUN}", "citation_list": [note["handle"]]}
        )
    )[0].text
    assert text.startswith("Error:") and "not a citation" in text, text


async def test_remove_absent_entry_saves_nothing(api):
    _, c1 = await make_source_and_citation(api)
    event = await post(
        api, "events", {"_class": "Event", "type": "Birth", "description": "orig", "citation_list": [c1["handle"]]}
    )
    text = await call(
        "update_event",
        {
            "handle": event["handle"],
            "set_fields": {"description": "changed"},
            "remove_refs": {"citation_list": [c1["handle"], "0000notthere0000"]},
        },
    )
    assert text.startswith("Error:"), text
    stored = await get(api, "events", event["handle"])
    assert stored["description"] == "orig" and stored["citation_list"] == [c1["handle"]]


async def test_dry_run_does_not_write(api):
    event = await post(api, "events", {"_class": "Event", "type": "Birth", "description": "orig"})
    text = await call(
        "update_event", {"handle": event["handle"], "set_fields": {"description": "x"}, "dry_run": True}
    )
    assert "Dry run" in text and '"orig" -> "x"' in text, text
    assert (await get(api, "events", event["handle"]))["description"] == "orig"


async def test_update_every_type(api):
    """Smoke test: each update_<type> replaces one field and verifies it."""
    _, citation = await make_source_and_citation(api)
    family = await post(api, "families", {"_class": "Family"})
    repo = await post(api, "repositories", {"_class": "Repository", "name": f"R{RUN}"})
    note = await post(api, "notes", {"_class": "Note", "text": {"_class": "StyledText", "string": "a"}})
    place = await post(api, "places", {"_class": "Place", "name": {"_class": "PlaceName", "value": "a"}})
    who = await post(api, "people", person("Smoke"))
    event = await post(api, "events", {"_class": "Event", "type": "Birth"})
    media = await upload_png(api)
    cases = [
        ("person", who["handle"], {"gender": 0}),
        ("family", family["handle"], {"type": "Unmarried"}),
        ("event", event["handle"], {"description": "e"}),
        ("place", place["handle"], {"title": "t"}),
        ("source", citation["source_handle"], {"title": "renamed"}),
        ("citation", citation["handle"], {"page": "p. 2"}),
        ("note", note["handle"], {"text": "replaced"}),
        ("media", media["handle"], {"desc": "d"}),
        ("repository", repo["handle"], {"name": "renamed"}),
    ]
    for entity_type, handle, fields in cases:
        text = await call(f"update_{entity_type}", {"handle": handle, "set_fields": fields})
        assert "Verified by read-back" in text, f"{entity_type}: {text}"


async def upload_png(api) -> dict:
    """Create a media object from a 1x1 PNG."""

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + chunk(b"IEND", b"")
    )
    result = await api.upload_media_file(png, "image/png")
    return result[0]["new"]


# ---------------------------------------------------------------------------
# family children and family dismantling
# ---------------------------------------------------------------------------


async def test_family_add_and_remove_child_keeps_links_in_sync(api):
    dad = await post(api, "people", person("Dad"))
    kid1 = await post(api, "people", person("Kid1"))
    kid2 = await post(api, "people", person("Kid2"))
    family = await post(
        api,
        "families",
        {"_class": "Family", "father_handle": dad["handle"], "child_ref_list": [{"_class": "ChildRef", "ref": kid1["handle"]}]},
    )
    text = await call(
        "update_family", {"handle": family["handle"], "add_refs": {"child_ref_list": [kid2["gramps_id"]]}}
    )
    assert "Verified by read-back" in text, text
    assert family["handle"] in (await get(api, "people", kid2["handle"]))["parent_family_list"]

    text = await call(
        "update_family", {"handle": family["handle"], "remove_refs": {"child_ref_list": [kid1["handle"]]}}
    )
    assert "Verified by read-back" in text, text
    stored = await get(api, "families", family["handle"])
    assert [c["ref"] for c in stored["child_ref_list"]] == [kid2["handle"]]
    assert (await get(api, "people", kid1["handle"]))["parent_family_list"] == []


async def test_dismantle_family_move_event_to_person_then_delete(api):
    """Mirror of the planned F0038 cleanup: move an event off a family, delete it."""
    dad = await post(api, "people", person("Head"))
    kid = await post(api, "people", person("Child"))
    event = await post(api, "events", {"_class": "Event", "type": "Residence"})
    family = await post(
        api,
        "families",
        {
            "_class": "Family",
            "father_handle": dad["handle"],
            "child_ref_list": [{"_class": "ChildRef", "ref": kid["handle"]}],
            "event_ref_list": [{"_class": "EventRef", "ref": event["handle"], "role": "Family"}],
        },
    )
    text = await call("update_person", {"handle": dad["handle"], "add_refs": {"event_ref_list": [event["gramps_id"]]}})
    assert "Verified by read-back" in text, text
    text = await call("update_family", {"handle": family["handle"], "remove_refs": {"event_ref_list": [event["handle"]]}})
    assert "Verified by read-back" in text, text

    text = await call("delete_family", {"gramps_id": family["gramps_id"], "confirm": True})
    assert text.startswith(f"Deleted family {family['gramps_id']}"), text
    assert "WARNING" not in text, text
    assert (await get(api, "people", kid["handle"]))["parent_family_list"] == []
    assert (await get(api, "people", dad["handle"]))["family_list"] == []
    assert [r["ref"] for r in (await get(api, "people", dad["handle"]))["event_ref_list"]] == [event["handle"]]
    await get(api, "events", event["handle"])  # event survives


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_requires_confirm_and_reports_referrers(api):
    _, citation = await make_source_and_citation(api)
    event = await post(api, "events", {"_class": "Event", "type": "Birth", "citation_list": [citation["handle"]]})

    text = await call("delete_citation", {"gramps_id": citation["gramps_id"]})
    assert text.startswith("Preview only"), text
    assert event["gramps_id"] in text
    await get(api, "citations", citation["handle"])  # still there

    text = await call("delete_citation", {"gramps_id": citation["gramps_id"], "confirm": True})
    assert "Verified: no longer readable" in text, text
    assert event["gramps_id"] in text
    deleted_json = json.loads(text.rsplit("\n", 1)[-1])
    assert deleted_json["handle"] == citation["handle"]
    assert (await get(api, "events", event["handle"]))["citation_list"] == []


async def test_delete_person_with_notes(api):
    notes = [
        await post(api, "notes", {"_class": "Note", "text": {"_class": "StyledText", "string": f"n{i}"}})
        for i in range(2)
    ]
    who = await post(api, "people", {**person("Gone"), "note_list": [n["handle"] for n in notes]})
    text = await call("delete_person", {"handle": who["handle"], "confirm": True})
    assert "Verified: no longer readable" in text, text
    for n in notes:
        text = await call("delete_note", {"handle": n["handle"], "confirm": True})
        assert "Verified: no longer readable" in text, text


async def test_delete_unknown_gramps_id(api):
    text = await call("delete_event", {"gramps_id": "E99999", "confirm": True})
    assert text.startswith("Error:") and "E99999" in text


# ---------------------------------------------------------------------------
# notes: full read and append
# ---------------------------------------------------------------------------


async def test_long_note_full_read_paging_and_truncation_marker(api):
    body = "".join(f"line {i:04d} {RUN}\n" for i in range(600))  # ~9 KB
    note = await post(api, "notes", {"_class": "Note", "text": {"_class": "StyledText", "string": body}})

    text = await call("get_note_full", {"handle": note["handle"]})
    assert text.split("\n\n", 1)[1] == body

    first = await call("get_note_full", {"gramps_id": note["gramps_id"], "max_chars": 4000})
    assert "offset=4000" in first
    second = await call("get_note_full", {"gramps_id": note["gramps_id"], "offset": 4000, "max_chars": 100000})
    assert first.split("\n\n", 1)[1].split("\n\n[More text")[0] + second.split("\n\n", 1)[1] == body

    found = await find_type_tool({"type": "note", "gql": f'gramps_id="{note["gramps_id"]}"', "max_results": 1})
    assert "use get_note_full" in found[0].text


async def test_append_note_keeps_text_and_formatting(api):
    note = await post(
        api,
        "notes",
        {
            "_class": "Note",
            "text": {
                "_class": "StyledText",
                "string": "Index start",
                "tags": [{"_class": "StyledTextTag", "name": "bold", "value": None, "ranges": [[0, 5]]}],
            },
        },
    )
    text = await call("append_note", {"gramps_id": note["gramps_id"], "text": "- new entry"})
    assert "Verified by read-back" in text, text
    stored = (await get(api, "notes", note["handle"]))["text"]
    assert stored["string"] == "Index start\n\n- new entry"
    assert stored["tags"][0]["ranges"] == [[0, 5]]
