"""
Integration tests for Tier 3: upload_media, check_references, export_backup,
ping and the explicit BACKEND UNREACHABLE error.

Only run against a local scratch Gramps Web.
"""

import base64
import json
import os
import struct
import zlib
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest
from dotenv import load_dotenv

from src.gramps_mcp.client import GrampsAPIError, GrampsWebAPIClient
from src.gramps_mcp.server import TOOL_REGISTRY
from src.gramps_mcp.tools.integrity import analyse

load_dotenv()

_host = urlparse(os.environ.get("GRAMPS_API_URL", "")).hostname
pytestmark = pytest.mark.skipif(
    _host not in ("localhost", "127.0.0.1", "::1")
    and os.environ.get("GRAMPS_ALLOW_DESTRUCTIVE_TESTS") != "1",
    reason="destructive tests only run against a local scratch Gramps Web",
)


def png_bytes() -> bytes:
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + chunk(b"IEND", b"")
    )


PNG_B64 = base64.b64encode(png_bytes()).decode()


async def call(tool: str, args: dict) -> str:
    return (await TOOL_REGISTRY[tool]["handler"](args))[0].text


def result_json(text: str) -> dict:
    return json.loads(text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])


@pytest.fixture
async def api():
    client = GrampsWebAPIClient()
    yield client
    await client.close()


async def post(api, collection, obj):
    result = await api.raw_request("POST", f"{collection}/", json_data=obj)
    return next(r["new"] for r in result if r.get("new") and r["_class"] == obj["_class"])


async def media_count(api) -> int:
    return len(await api.raw_request("GET", "media/", params={"keys": "handle"}))


async def citation(api):
    src = await post(api, "sources", {"_class": "Source", "title": "Census 1860"})
    return await post(api, "citations", {"_class": "Citation", "source_handle": src["handle"]})


# -- item 11: upload_media -------------------------------------------------------


async def test_upload_and_attach_with_region(api):
    cit = await citation(api)
    text = await call(
        "upload_media",
        {
            "data_base64": PNG_B64,
            "filename": "census-1860-p7.png",
            "desc": "1860 census p. 7",
            "date": {"start": "1860-07"},
            "attach_to": {"type": "citation", "id": cit["gramps_id"], "rect": [10, 20, 60, 45]},
        },
    )
    assert "Verified by read-back" in text, text
    body = result_json(text)
    media = body["stored"][0]
    assert media["desc"] == "1860 census p. 7" and media["mime"] == "image/png"
    assert media["date"]["dateval"][:3] == [0, 7, 1860]
    stored_cit = await api.raw_request("GET", f"citations/{cit['handle']}")
    assert stored_cit["media_list"][0]["ref"] == media["handle"]
    assert stored_cit["media_list"][0]["rect"] == [10, 20, 60, 45]
    assert body["file"]["bytes"] == len(png_bytes())


async def test_upload_dry_run_and_validation(api):
    before = await media_count(api)
    text = await call("upload_media", {"data_base64": PNG_B64, "filename": "x.png", "dry_run": True})
    assert "Dry run" in text and result_json(text)["file"]["mime"] == "image/png"
    bad = await call("upload_media", {"data_base64": "not base64!!", "filename": "x.png"})
    assert bad.startswith("Error:") and "base64" in bad
    rect = await call(
        "upload_media",
        {"data_base64": PNG_B64, "filename": "x.png", "attach_to": {"type": "event", "id": "E1", "rect": [50, 0, 10, 10]}},
    )
    assert rect.startswith("Error:") and "x1 < x2" in rect
    missing = await call(
        "upload_media", {"data_base64": PNG_B64, "filename": "x.png", "attach_to": {"type": "event", "id": "E99999"}}
    )
    assert missing.startswith("Error:")
    assert await media_count(api) == before


async def test_upload_rolled_back_when_attach_fails(api):
    cit = await citation(api)
    before = await media_count(api)
    original = GrampsWebAPIClient.raw_request

    async def failing(self, method, endpoint, *args, **kwargs):
        if method == "PUT" and endpoint.startswith("citations/"):
            raise GrampsAPIError("Request rejected (400): simulated failure")
        return await original(self, method, endpoint, *args, **kwargs)

    with patch.object(GrampsWebAPIClient, "raw_request", failing):
        text = await call(
            "upload_media",
            {"data_base64": PNG_B64, "filename": "x.png", "attach_to": {"type": "citation", "id": cit["handle"]}},
        )
    assert text.startswith("Error:") and "deleted again" in text, text
    assert await media_count(api) == before


# -- item 12: check_references ------------------------------------------------------


async def test_check_references_finds_injected_problems(api):
    note = await post(api, "notes", {"_class": "Note", "text": {"_class": "StyledText", "string": "n"}})
    orphan_cit = await citation(api)
    ev = await post(api, "events", {"_class": "Event", "type": "Birth"})
    # bypass validation the way the old tools allowed: a note handle and a
    # dangling handle in citation_list
    raw = await api.raw_request("GET", f"events/{ev['handle']}")
    raw["citation_list"] = [note["handle"], "0000dangling00000000000000"]
    await api.raw_request("PUT", f"events/{ev['handle']}", json_data=raw)
    kid = await post(api, "people", {"_class": "Person", "gender": 1, "primary_name": {"_class": "Name", "first_name": "K"}})
    fam = await post(api, "families", {"_class": "Family", "child_ref_list": [{"_class": "ChildRef", "ref": kid["handle"]}]})
    raw_kid = await api.raw_request("GET", f"people/{kid['handle']}")
    await api.raw_request("PUT", f"people/{kid['handle']}", json_data={**raw_kid, "parent_family_list": []})

    text = await call("check_references", {})
    findings = result_json(text)
    assert any(f["handle"] == ev["handle"] and f["actual"] == "note" for f in findings["wrong_type"])
    assert any(f["handle"] == ev["handle"] and f["ref"].startswith("0000dangling") for f in findings["dangling"])
    assert any(f["handle"] == orphan_cit["handle"] for f in findings["orphan_citations"])
    assert any(f["handle"] == ev["handle"] for f in findings["orphan_events"])
    assert any(
        f["family_handle"] == fam["handle"] and f["person_handle"] == kid["handle"]
        for f in findings["family_link_mismatches"]
    )
    assert text.startswith("Checked ")


def test_analyse_nested_citation_refs():
    """citation_list inside a name / event ref is checked too."""
    objects = {
        "person": [{"handle": "p", "gramps_id": "I1", "family_list": ["f"], "parent_family_list": [],
                    "primary_name": {"citation_list": ["n"]}, "event_ref_list": [{"ref": "e", "citation_list": ["c"]}]}],
        "family": [{"handle": "f", "gramps_id": "F1", "father_handle": "p", "child_ref_list": []}],
        "event": [{"handle": "e", "gramps_id": "E1"}],
        "citation": [{"handle": "c", "gramps_id": "C1"}],
        "note": [{"handle": "n", "gramps_id": "N1"}],
        "tag": [],
    }
    findings = analyse(objects)
    assert [f["field"] for f in findings["wrong_type"]] == ["primary_name.citation_list[0]"]
    assert not findings["dangling"] and not findings["orphan_citations"]
    assert not findings["family_link_mismatches"]


# -- item 13: export_backup ------------------------------------------------------------


async def test_export_backup(api, tmp_path, monkeypatch):
    monkeypatch.setenv("GRAMPS_BACKUP_DIR", str(tmp_path))
    text = await call("export_backup", {})
    assert "Verified: object counts match" in text, text
    files = list(tmp_path.glob("*.gramps"))
    assert len(files) == 1 and files[0].stat().st_size > 0
    assert str(files[0]) in text and f"{files[0].stat().st_size:,} bytes" in text


async def test_export_backup_requires_configured_dir(monkeypatch):
    monkeypatch.delenv("GRAMPS_BACKUP_DIR", raising=False)
    text = await call("export_backup", {})
    assert text.startswith("Error:") and "GRAMPS_BACKUP_DIR" in text


# -- item 14: ping and unreachable backend ------------------------------------------------


async def test_ping_ok():
    text = await call("ping", {})
    assert "reachable" in text and "Login: OK" in text and "Tree:" in text, text


async def test_unreachable_backend_is_explicit():
    async def refuse(self, *args, **kwargs):
        raise httpx.ConnectError("Connection refused")

    with patch.object(httpx.AsyncClient, "get", refuse):
        text = await call("ping", {})
    assert text.startswith("Error: BACKEND UNREACHABLE"), text

    with patch.object(httpx.AsyncClient, "request", refuse), patch.object(httpx.AsyncClient, "post", refuse):
        text = await call("get_by_gramps_id", {"gramps_id": "E0001"})
    assert text.startswith("Error: BACKEND UNREACHABLE"), text
    assert "Nothing was read or written" in text
