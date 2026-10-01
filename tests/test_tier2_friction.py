"""
Integration tests for Tier 2: GQL escaping and local fallback, get_by_gramps_id,
search snippets, add_child/remove_child and argument-free tree_stats.

Only run against a local scratch Gramps Web.
"""

import json
import os
import uuid
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from dotenv import load_dotenv

from src.gramps_mcp.client import GrampsAPIError, GrampsWebAPIClient
from src.gramps_mcp.server import TOOL_REGISTRY, _make_tool_function

load_dotenv()

_host = urlparse(os.environ.get("GRAMPS_API_URL", "")).hostname
pytestmark = pytest.mark.skipif(
    _host not in ("localhost", "127.0.0.1", "::1")
    and os.environ.get("GRAMPS_ALLOW_DESTRUCTIVE_TESTS") != "1",
    reason="destructive tests only run against a local scratch Gramps Web",
)


async def call(tool: str, args: dict) -> str:
    return (await TOOL_REGISTRY[tool]["handler"](args))[0].text


@pytest.fixture
async def api():
    client = GrampsWebAPIClient()
    yield client
    await client.close()


async def post(api, collection, obj):
    result = await api.raw_request("POST", f"{collection}/", json_data=obj)
    return next(r["new"] for r in result if r.get("new") and r["_class"] == obj["_class"])


def note(text):
    return {"_class": "Note", "type": "Research", "text": {"_class": "StyledText", "string": text}}


def person(first):
    return {
        "_class": "Person",
        "gender": 1,
        "primary_name": {"_class": "Name", "first_name": first, "surname_list": [{"_class": "Surname", "surname": "Tier2"}]},
    }


# -- item 6: GQL --------------------------------------------------------------


async def test_phrase_with_period_on_notes_and_events(api):
    marker = uuid.uuid4().hex[:6]
    n = await post(api, "notes", note(f"Letter from WM. Berry {marker}"))
    ev = await post(api, "events", {"_class": "Event", "type": "Birth", "description": f"Bapt. by WM. {marker}"})
    text = await call("find_type", {"type": "note", "gql": f'text ~ "WM. Berry {marker}"', "max_results": 5})
    assert n["gramps_id"] in text, text
    text = await call("find_type", {"type": "event", "gql": f'description ~ "WM. {marker}"', "max_results": 5})
    assert ev["gramps_id"] in text, text


async def test_not_and_or_on_notes_evaluated_locally(api):
    marker = uuid.uuid4().hex[:6]
    keep = await post(api, "notes", note(f"{marker} keep"))
    drop = await post(api, "notes", note(f"{marker} drop"))
    text = await call("find_type", {"type": "note", "gql": f'text ~ "{marker}" and not text ~ "drop"'})
    assert "evaluated locally" in text and keep["gramps_id"] in text and drop["gramps_id"] not in text
    text = await call("find_type", {"type": "note", "gql": f'text ~ "{marker} keep" or text ~ "{marker} drop"'})
    assert keep["gramps_id"] in text and drop["gramps_id"] in text


async def test_embedded_quotes(api):
    marker = uuid.uuid4().hex[:6]
    ev = await post(api, "events", {"_class": "Event", "type": "Birth", "description": f'called "Rev" {marker}'})
    text = await call("find_type", {"type": "event", "gql": f'description ~ "\\"Rev\\" {marker}"'})
    assert ev["gramps_id"] in text, text


async def test_notes_fall_back_when_server_gql_fails(api):
    """Some Gramps Web versions return 500 for any GQL on /notes."""
    marker = uuid.uuid4().hex[:6]
    n = await post(api, "notes", note(f"fallback WM. {marker}"))
    original = GrampsWebAPIClient._make_request

    async def failing(self, method, url, params=None, *args, **kwargs):
        if "notes" in url and params and "gql" in params:
            raise GrampsAPIError("Server error. Please try again later.")
        return await original(self, method, url, params, *args, **kwargs)

    with patch.object(GrampsWebAPIClient, "_make_request", failing):
        text = await call("find_type", {"type": "note", "gql": f'text ~ "WM. {marker}"'})
    assert "evaluated locally" in text and n["gramps_id"] in text, text


async def test_malformed_gql_explains_syntax(api):
    text = await call("find_type", {"type": "event", "gql": 'description ~ "open'})
    assert text.startswith("Error:") and "Syntax" in text


# -- item 7: get_by_gramps_id ------------------------------------------------------


async def test_get_by_gramps_id_every_type(api):
    src = await post(api, "sources", {"_class": "Source", "title": "t"})
    objs = {
        "person": await post(api, "people", person("Id")),
        "family": await post(api, "families", {"_class": "Family"}),
        "event": await post(api, "events", {"_class": "Event", "type": "Birth"}),
        "place": await post(api, "places", {"_class": "Place", "name": {"_class": "PlaceName", "value": "p"}}),
        "source": src,
        "citation": await post(api, "citations", {"_class": "Citation", "source_handle": src["handle"]}),
        "note": await post(api, "notes", note("x" * 3000)),
        "repository": await post(api, "repositories", {"_class": "Repository", "name": "r"}),
    }
    for entity_type, obj in objs.items():
        text = await call("get_by_gramps_id", {"gramps_id": obj["gramps_id"]})
        assert text.startswith(f"{entity_type} {obj['gramps_id']}"), text
        body = json.loads(text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        assert body["handle"] == obj["handle"]
    assert ("x" * 3000) in await call("get_by_gramps_id", {"gramps_id": objs["note"]["gramps_id"]})
    missing = await call("get_by_gramps_id", {"gramps_id": "E99999"})
    assert missing.startswith("Error:") and "E99999" in missing


# -- item 8: snippets ------------------------------------------------------------------


async def test_find_anything_includes_snippet(api):
    marker = uuid.uuid4().hex[:8]
    long_text = ("filler " * 60) + f"the will of WM. Berry {marker} names Rosa " + ("tail " * 60)
    await post(api, "notes", note(long_text))
    text = await call("find_anything", {"query": marker})
    line = next(l for l in text.splitlines() if l.strip().startswith("Match:"))
    assert marker in line and "WM. Berry" in line
    assert len(line.split("Match: ", 1)[1]) <= 206  # ~200 chars plus ellipses


# -- item 9: add_child / remove_child -------------------------------------------------


async def test_add_and_remove_child(api):
    dad = await post(api, "people", person("Dad"))
    kid1 = await post(api, "people", person("Kid1"))
    kid2 = await post(api, "people", person("Kid2"))
    fam = await post(api, "families", {"_class": "Family", "father_handle": dad["handle"], "child_ref_list": [{"_class": "ChildRef", "ref": kid1["handle"]}]})

    text = await call("add_child", {"family": fam["gramps_id"], "person": kid2["gramps_id"], "rel": "adopted", "dry_run": True})
    assert "Dry run" in text
    assert len((await api.raw_request("GET", f"families/{fam['handle']}"))["child_ref_list"]) == 1

    text = await call("add_child", {"family": fam["gramps_id"], "person": kid2["gramps_id"], "rel": "adopted"})
    assert "Verified" in text, text
    stored = await api.raw_request("GET", f"families/{fam['handle']}")
    assert [c["ref"] for c in stored["child_ref_list"]] == [kid1["handle"], kid2["handle"]]
    assert stored["child_ref_list"][1]["frel"] == "Adopted" == stored["child_ref_list"][1]["mrel"]

    again = await call("add_child", {"family": fam["handle"], "person": kid2["handle"]})
    assert again.startswith("Error:") and "already a child" in again

    text = await call("remove_child", {"family": fam["gramps_id"], "person": kid1["gramps_id"]})
    assert "Verified" in text, text
    stored = await api.raw_request("GET", f"families/{fam['handle']}")
    assert [c["ref"] for c in stored["child_ref_list"]] == [kid2["handle"]]
    assert (await api.raw_request("GET", f"people/{kid1['handle']}"))["parent_family_list"] == []

    missing = await call("remove_child", {"family": fam["gramps_id"], "person": kid1["gramps_id"]})
    assert missing.startswith("Error:") and "not a child" in missing


# -- item 10: tree_stats without arguments -------------------------------------------


async def test_tree_stats_with_no_arguments(api):
    config = TOOL_REGISTRY["tree_stats"]
    tool = _make_tool_function("tree_stats", config["schema"], config["handler"])
    result = await tool()  # what FastMCP does for a call with no arguments
    text = result[0].text
    assert text.startswith("# Family Tree:"), text
    assert "People:" in text and "Notes:" in text
