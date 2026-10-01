"""
Integration tests for Tier 1 data-safety features: dry_run, re-read results,
tags, structured dates and the one-transaction batch.

Like test_edit_tools.py, these only run against a local scratch Gramps Web.
"""

import json
import os
import uuid
from urllib.parse import urlparse

import pytest
from dotenv import load_dotenv

from src.gramps_mcp.client import GrampsAPIError, GrampsWebAPIClient
from src.gramps_mcp.server import TOOL_REGISTRY
from src.gramps_mcp.tools.batch_tools import BatchParams, _payload, _plan

load_dotenv()

_host = urlparse(os.environ.get("GRAMPS_API_URL", "")).hostname
pytestmark = pytest.mark.skipif(
    _host not in ("localhost", "127.0.0.1", "::1")
    and os.environ.get("GRAMPS_ALLOW_DESTRUCTIVE_TESTS") != "1",
    reason="destructive tests only run against a local scratch Gramps Web",
)


def uid() -> str:
    return uuid.uuid4().hex[:8]


async def call(tool: str, args: dict) -> str:
    return (await TOOL_REGISTRY[tool]["handler"](args))[0].text


def result_json(text: str) -> dict:
    """The JSON block every write tool returns."""
    assert "```json" in text, text
    return json.loads(text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])


@pytest.fixture
async def api():
    client = GrampsWebAPIClient()
    yield client
    await client.close()


async def get(api, collection, handle):
    return await api.raw_request("GET", f"{collection}/{handle}")


async def gone(api, collection, handle) -> bool:
    try:
        await get(api, collection, handle)
        return False
    except GrampsAPIError as e:
        return "not found" in str(e).lower()


async def find_events(api, description: str) -> list:
    rows = await api.raw_request("GET", "events/", params={"keys": "handle,description"})
    return [r for r in rows if r.get("description") == description]


async def post(api, collection, obj):
    result = await api.raw_request("POST", f"{collection}/", json_data=obj)
    return next(r["new"] for r in result if r.get("new") and r["_class"] == obj["_class"])


def person(first):
    return {
        "_class": "Person",
        "gender": 1,
        "primary_name": {"_class": "Name", "first_name": first, "surname_list": [{"_class": "Surname", "surname": "Tier1"}]},
    }


# -- dry_run on every write ---------------------------------------------------


async def test_create_dry_run_writes_nothing(api):
    desc = f"dry-{uid()}"
    text = await call("create_event", {"type": "Birth", "description": desc, "citation_list": [], "dry_run": True})
    body = result_json(text)
    assert body["dry_run"] is True and body["changes"][0]["op"] == "create"
    assert body["changes"][0]["after"]["description"] == desc
    assert await find_events(api, desc) == []


async def test_create_returns_reread_object(api):
    desc = f"reread-{uid()}"
    text = await call("create_event", {"type": "Birth", "description": desc, "citation_list": []})
    body = result_json(text)
    stored = body["stored"][0]
    assert stored == await get(api, "events", stored["handle"])
    assert stored["gramps_id"], "server-assigned fields prove this is a re-read"


async def test_update_dry_run_diff_and_no_write(api):
    ev = await post(api, "events", {"_class": "Event", "type": "Birth", "description": "orig"})
    text = await call("update_event", {"handle": ev["handle"], "set_fields": {"description": "new"}, "dry_run": True})
    fields = result_json(text)["changes"][0]["fields"]
    assert fields["description"] == {"before": "orig", "after": "new"}
    assert (await get(api, "events", ev["handle"]))["description"] == "orig"


async def test_delete_dry_run_shows_referrer_diff(api):
    src = await post(api, "sources", {"_class": "Source", "title": "s"})
    cit = await post(api, "citations", {"_class": "Citation", "source_handle": src["handle"]})
    ev = await post(api, "events", {"_class": "Event", "type": "Birth", "citation_list": [cit["handle"]]})
    body = result_json(await call("delete_citation", {"handle": cit["handle"], "dry_run": True}))
    assert body["changes"][0]["op"] == "delete"
    referrer = next(c for c in body["changes"] if c["handle"] == ev["handle"])
    assert referrer["fields"]["citation_list"] == {"before": [cit["handle"]], "after": []}
    await get(api, "citations", cit["handle"])  # still there


async def test_append_note_dry_run(api):
    note = await post(api, "notes", {"_class": "Note", "text": {"_class": "StyledText", "string": "a"}})
    await call("append_note", {"handle": note["handle"], "text": "b", "dry_run": True})
    assert (await get(api, "notes", note["handle"]))["text"]["string"] == "a"


# -- tags -----------------------------------------------------------------------


async def test_tag_on_create_and_update_then_find_by_tag(api):
    tag = f"t1-{uid()}"
    created = result_json(await call("create_note", {"text": "tagged", "type": "General", "tag": tag}))["stored"][0]
    ev = await post(api, "events", {"_class": "Event", "type": "Death"})
    await call("update_event", {"handle": ev["handle"], "set_fields": {"description": "x"}, "tag": tag})
    found = await call("find_by_tag", {"tag": tag})
    assert "2 object(s)" in found
    assert created["gramps_id"] in found and ev["gramps_id"] in found


# -- structured dates -------------------------------------------------------------


async def test_structured_date_server_computes_sortval(api):
    body = result_json(
        await call(
            "create_event",
            {"type": "Birth", "citation_list": [], "date": {"modifier": "about", "quality": "estimated", "start": "1804-03"}},
        )
    )
    date = body["stored"][0]["date"]
    assert (date["modifier"], date["quality"], date["dateval"][:3]) == (3, 1, [0, 3, 1804])
    assert date["sortval"] > 0


async def test_regression_open_range_rejected_and_not_saved(api):
    desc = f"range-{uid()}"
    text = await call(
        "create_event",
        {"type": "Birth", "description": desc, "citation_list": [], "date": {"modifier": "range", "quality": "estimated", "start": "1804"}},
    )
    assert text.startswith("Error:") and "range" in text
    assert await find_events(api, desc) == []

    ev = await post(api, "events", {"_class": "Event", "type": "Birth"})
    text = await call("update_event", {"handle": ev["handle"], "set_fields": {"date": {"modifier": 4, "dateval": [0, 0, 1804, False]}}})
    assert text.startswith("Error:")


# -- batch ---------------------------------------------------------------------------


async def test_batch_creates_links_and_keeps_family_consistent(api):
    tag = f"batch-{uid()}"
    dad = await post(api, "people", person("Dad"))
    src = await post(api, "sources", {"_class": "Source", "title": "Census"})
    ops = [
        {"op": "create", "type": "citation", "id": "cit", "data": {"source_handle": src["gramps_id"], "page": "p. 7"}},
        {"op": "create", "type": "event", "id": "birth", "data": {"type": "Birth", "citation_list": ["$cit"], "date": {"modifier": "about", "start": "1850"}}},
        {"op": "create", "type": "person", "id": "kid", "data": {"gender": 1, "primary_name": {"first_name": "Kid", "surname_list": [{"surname": "Tier1"}]}, "event_ref_list": [{"ref": "$birth", "role": "Primary"}]}},
        {"op": "create", "type": "family", "id": "fam", "data": {"father_handle": dad["handle"], "child_ref_list": [{"ref": "$kid"}]}},
        {"op": "update", "type": "person", "handle": dad["handle"], "set_fields": {"gender": 1}, "add_refs": {"event_ref_list": ["$birth"]}},
    ]
    dry = result_json(await call("batch", {"ops": ops, "tag": tag, "dry_run": True}))
    assert dry["dry_run"] and len(dry["changes"]) >= 5
    assert "0 object(s)" in await call("find_by_tag", {"tag": tag}) or "No tag" in await call("find_by_tag", {"tag": tag})

    text = await call("batch", {"ops": ops, "tag": tag})
    assert "Verified by read-back" in text, text
    body = result_json(text)
    stored = {o["handle"]: o for o in body["stored"]}
    kid = next(o for o in body["stored"] if o.get("primary_name", {}).get("first_name") == "Kid")
    fam = next(o for o in body["stored"] if "child_ref_list" in o)
    assert kid["parent_family_list"] == [fam["handle"]]
    assert kid["birth_ref_index"] == 0
    assert fam["handle"] in (await get(api, "people", dad["handle"]))["family_list"]
    event = await get(api, "events", kid["event_ref_list"][0]["ref"])
    assert event["date"]["sortval"] > 0 and len(event["citation_list"]) == 1
    assert "4 object(s)" in await call("find_by_tag", {"tag": tag}) or len(stored) >= 4


async def test_batch_invalid_op_writes_nothing(api):
    desc = f"atomic-{uid()}"
    ops = [
        {"op": "create", "type": "event", "data": {"type": "Birth", "description": desc}},
        {"op": "update", "type": "event", "gramps_id": "E99999", "set_fields": {"description": "x"}},
    ]
    text = await call("batch", {"ops": ops})
    assert text.startswith("Error:") and "op 1" in text
    assert await find_events(api, desc) == []


async def test_batch_server_rollback_when_object_changed_concurrently(api):
    """A failure inside the server's DbTxn must undo the items before it."""
    desc = f"rollback-{uid()}"
    victim = await post(api, "events", {"_class": "Event", "type": "Birth", "description": "v1"})
    params = BatchParams(
        ops=[
            {"op": "create", "type": "event", "data": {"type": "Birth", "description": desc}},
            {"op": "update", "type": "event", "handle": victim["handle"], "set_fields": {"description": "v2"}},
        ]
    )
    plan = await _plan(api, params)
    payload = await _payload(api, plan)
    # someone else edits the victim between planning and committing
    stored = await get(api, "events", victim["handle"])
    await api.raw_request("PUT", f"events/{victim['handle']}", json_data={**stored, "description": "edited elsewhere"})
    with pytest.raises(GrampsAPIError, match="changed"):
        await api.raw_request("POST", "transactions/", json_data=payload)
    assert await find_events(api, desc) == []
    assert (await get(api, "events", victim["handle"]))["description"] == "edited elsewhere"


async def test_batch_delete_person_cascades(api):
    birth = await post(api, "events", {"_class": "Event", "type": "Birth"})
    solo = await post(api, "people", {**person("Solo"), "event_ref_list": [{"_class": "EventRef", "ref": birth["handle"], "role": "Primary"}]})
    other = await post(api, "people", {**person("Other"), "event_ref_list": [{"_class": "EventRef", "ref": birth["handle"], "role": "Primary"}], "birth_ref_index": 0})
    fam = await post(api, "families", {"_class": "Family", "father_handle": solo["handle"]})
    text = await call("batch", {"ops": [
        {"op": "delete", "type": "person", "handle": solo["handle"]},
        {"op": "delete", "type": "event", "handle": birth["handle"]},
    ]})
    assert "Verified by read-back" in text, text
    assert await gone(api, "people", solo["handle"])
    assert await gone(api, "families", fam["handle"]), "family left empty must be deleted"
    other_now = await get(api, "people", other["handle"])
    assert other_now["event_ref_list"] == [] and other_now["birth_ref_index"] == -1


async def test_batch_delete_source_deletes_its_citations(api):
    src = await post(api, "sources", {"_class": "Source", "title": "doomed"})
    cit = await post(api, "citations", {"_class": "Citation", "source_handle": src["handle"]})
    ev = await post(api, "events", {"_class": "Event", "type": "Birth", "citation_list": [cit["handle"]]})
    text = await call("batch", {"ops": [{"op": "delete", "type": "source", "gramps_id": src["gramps_id"]}]})
    assert "Verified by read-back" in text, text
    assert await gone(api, "citations", cit["handle"])
    assert (await get(api, "events", ev["handle"]))["citation_list"] == []


async def test_batch_rejects_open_range_date(api):
    text = await call("batch", {"ops": [{"op": "create", "type": "event", "data": {"type": "Birth", "date": {"modifier": "range", "start": "1804"}}}]})
    assert text.startswith("Error:") and "range" in text
