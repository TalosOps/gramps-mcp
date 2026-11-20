"""
Unit tests for the GOQL/rules port of find_type (no server needed).

Gramps Web API 3.26 removed the ``gql`` query parameter, so find_type must
never send it: GQL is evaluated locally, and ``goql`` goes to the server as a
``MatchesQuery`` rule.
"""

import json
from unittest.mock import patch

import pytest

from src.gramps_mcp.tools import find as find_mod

EVENTS = [
    {"handle": f"h{i}", "gramps_id": f"E{i:04d}", "description": d}
    for i, d in enumerate(["Berry farm sale", "Kenner deed", "BERRY tavern", "Knox burial"])
]
NOTES = [
    {"handle": "n1", "gramps_id": "N0001", "text": {"string": "Lifted colour, keep"}},
    {"handle": "n2", "gramps_id": "N0002", "text": {"string": "Lifted colour, drop"}},
]


class FakeClient:
    def __init__(self):
        self.requests = []

    def _build_url(self, tree_id, endpoint):
        return endpoint

    async def _make_request(self, method, url, params=None, return_headers=False, **kw):
        self.requests.append(("make", url, dict(params or {})))
        rows = EVENTS[: (params or {}).get("pagesize", 20)]
        return (rows, {"x-total-count": str(len(EVENTS))}) if return_headers else rows

    async def raw_request(self, method, url, params=None, **kw):
        self.requests.append(("raw", url, dict(params or {})))
        data = NOTES if url.startswith("notes") else EVENTS
        page = (params or {}).get("page", 1)
        return data if page == 1 else []

    async def close(self):
        pass


async def fake_describe(client, entity_type, handle):
    return f"{entity_type}:{handle}"


@pytest.fixture
def fake():
    client = FakeClient()
    with patch("src.gramps_mcp.tools.search_basic.GrampsWebAPIClient", return_value=client), patch.object(
        find_mod, "describe", fake_describe
    ):
        yield client


def sent_params(client):
    return [p for _, _, p in client.requests]


@pytest.mark.asyncio
async def test_gql_is_evaluated_locally_and_never_sent(fake):
    out = (await find_mod.find_type_tool({"type": "event", "gql": 'description ~ "berry"'}))[0].text
    assert "Found 2 events" in out and "event:h0" in out and "event:h2" in out
    assert all("gql" not in p and "oql" not in p for p in sent_params(fake))


@pytest.mark.asyncio
async def test_not_and_or_still_work(fake):
    out = (await find_mod.find_type_tool({"type": "event", "gql": 'not description ~ "berry" and description ~ "k"'}))[0].text
    assert "Found 2 events" in out and "event:h1" in out and "event:h3" in out


@pytest.mark.asyncio
async def test_note_text_path_is_renamed(fake):
    out = (await find_mod.find_type_tool({"type": "note", "gql": 'text ~ "keep"'}))[0].text
    assert "Found 1 notes" in out and "note:n1" in out


@pytest.mark.asyncio
async def test_empty_query_lists_one_page(fake):
    out = (await find_mod.find_type_tool({"type": "event", "gql": "", "max_results": 2}))[0].text
    assert "Found 4 events (showing 2)" in out
    assert sent_params(fake) == [{"pagesize": 2}]


@pytest.mark.asyncio
async def test_goql_goes_to_server_as_matches_query_rule(fake):
    await find_mod.find_type_tool({"type": "event", "goql": 'gramps_id == "E0001"', "max_results": 5})
    (params,) = sent_params(fake)
    assert "gql" not in params and params["pagesize"] == 5
    assert json.loads(params["rules"]) == {"rules": [{"name": "MatchesQuery", "values": ['gramps_id == "E0001"']}]}


@pytest.mark.asyncio
async def test_gql_and_goql_together_is_an_error(fake):
    out = (await find_mod.find_type_tool({"type": "event", "gql": "x = 1", "goql": "x == 1"}))[0].text
    assert "either gql or goql" in out
    assert fake.requests == []
