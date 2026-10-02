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
export_backup: a complete, timestamped .gramps export saved to a backup dir.
ping: is the MCP server up, is the Gramps backend reachable, does login work.

The backup directory is configured with the GRAMPS_BACKUP_DIR environment
variable on the MCP server (a mounted volume when running in Docker).
"""

import asyncio
import gzip
import hashlib
import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from mcp.types import TextContent
from pydantic import BaseModel

from ..client import GrampsAPIError, GrampsBackendUnreachable
from .editing import _error
from .search_basic import with_client

TASK_TIMEOUT_SECONDS = 600
XML_ELEMENTS = {
    "people": "person",
    "families": "family",
    "events": "event",
    "places": "placeobj",
    "sources": "source",
    "citations": "citation",
    "media": "object",
    "notes": "note",
    "repositories": "repository",
}


class NoParams(BaseModel):
    """Tool takes no arguments."""


def backup_dir() -> str:
    """The configured backup directory, created if needed."""
    path = os.environ.get("GRAMPS_BACKUP_DIR", "").strip()
    if not path:
        raise ValueError(
            "GRAMPS_BACKUP_DIR is not set on the MCP server. Set it to a "
            "directory (a mounted volume in Docker) and restart the server."
        )
    os.makedirs(path, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise ValueError(f"Backup directory {path} is not writable")
    return path


def count_xml_objects(content: bytes) -> Dict[str, int]:
    """Count primary objects inside a .gramps file (gzipped Gramps XML)."""
    xml = gzip.decompress(content).decode("utf-8", errors="replace")
    return {
        key: len(re.findall(rf"<{element}\s+handle=", xml))
        for key, element in XML_ELEMENTS.items()
    }


async def _run_export(client) -> Dict:
    """Start the export and wait for it; returns the API's result object."""
    result = await client.raw_request(
        "POST",
        "exporters/gramps/file",
        params={"compress": "true", "private": "false", "living": "IncludeAll"},
    )
    task = (result or {}).get("task")
    if not task:
        return result  # no task queue: the export ran synchronously
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = await client.raw_request("GET", f"tasks/{task['id']}")
        state = status.get("state")
        if state == "SUCCESS":
            return status.get("result_object") or status.get("result") or {}
        if state in ("FAILURE", "REVOKED"):
            raise GrampsAPIError(f"Export task failed: {status.get('result')}")
        await asyncio.sleep(1)
    raise GrampsAPIError(f"Export did not finish within {TASK_TIMEOUT_SECONDS}s")


@with_client
async def export_backup_tool(client, arguments: Dict) -> List[TextContent]:
    """
    Export the whole tree as .gramps and save it under GRAMPS_BACKUP_DIR.

    Args:
        client: Gramps API client instance (injected).
        arguments (Dict): None.

    Returns:
        List[TextContent]: Path, size, checksum and an object-count check.
    """
    try:
        directory = backup_dir()
        tree = await client.raw_request("GET", "trees/-")
        export = await _run_export(client)
        url = export.get("url") or ""
        if not url:
            raise GrampsAPIError(f"Export returned no file: {export}")
        content = await client.download(url.split("/api/", 1)[-1])
        counts = count_xml_objects(content)
        expected = (await client.raw_request("GET", "metadata/")).get(
            "object_counts"
        ) or {}

        name = re.sub(r"[^A-Za-z0-9._-]+", "-", tree.get("name") or "tree").strip("-")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, f"{name}-{stamp}.gramps")
        partial = path + ".partial"
        with open(partial, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(partial, path)

        missing = {
            k: (counts.get(k, 0), v)
            for k, v in expected.items()
            if k in counts and counts[k] != v
        }
        lines = [
            f"Backup written: {path}",
            f"Size: {len(content):,} bytes  "
            f"sha256: {hashlib.sha256(content).hexdigest()}",
            "Objects in file: "
            + ", ".join(f"{k} {v}" for k, v in counts.items() if v or expected.get(k)),
        ]
        if missing:
            lines.insert(
                0,
                "Error: the backup does not contain every object "
                + ", ".join(
                    f"{k}: {got} of {want}" for k, (got, want) in missing.items()
                ),
            )
        else:
            lines.append("Verified: object counts match the tree.")
        return [TextContent(type="text", text="\n".join(lines))]
    except Exception as e:
        return _error(e, "export_backup")


@with_client
async def ping_tool(client, arguments: Optional[Dict] = None) -> List[TextContent]:
    """
    Check the MCP server, the Gramps backend and login, step by step.

    Args:
        client: Gramps API client instance (injected).
        arguments (Optional[Dict]): None.

    Returns:
        List[TextContent]: One line per check; an explicit BACKEND
        UNREACHABLE error if the backend cannot be reached.
    """
    lines = ["MCP server: OK"]
    started = time.monotonic()
    try:
        response = await client.auth_manager.client.get(
            client._build_url("default", "metadata/"), timeout=10.0
        )
        elapsed = (time.monotonic() - started) * 1000
        lines.append(
            f"Gramps backend {client.base_url}: reachable "
            f"(HTTP {response.status_code}, {elapsed:.0f} ms)"
        )
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        error = GrampsBackendUnreachable(
            f"Gramps backend unreachable at {client.base_url} ({type(e).__name__}: {e})"
        )
        return _error(error, "ping")
    try:
        await client.auth_manager.authenticate()
        lines.append("Login: OK")
    except Exception as e:
        lines.append(f"Login: FAILED ({e})")
        return [TextContent(type="text", text="Error: " + "\n".join(lines))]
    try:
        tree = await client.raw_request("GET", "trees/-")
        metadata = await client.raw_request("GET", "metadata/")
        versions = metadata.get("gramps_webapi", {}).get("version"), (
            metadata.get("gramps", {}).get("version")
        )
        people = (metadata.get("object_counts") or {}).get("people")
        lines.append(f"Tree: {tree.get('name')} ({tree.get('id')}), {people} people")
        lines.append(f"Versions: gramps-webapi {versions[0]}, gramps {versions[1]}")
    except Exception as e:
        lines.append(f"Tree info: FAILED ({e})")
    return [TextContent(type="text", text="\n".join(lines))]
