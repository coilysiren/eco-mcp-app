"""Exercise the tool surface end-to-end through the MCP server."""

from __future__ import annotations

import json

import mcp.types as mt
import pytest

from eco_mcp_app.server import KNOWN_PUBLIC_SERVERS, build_server


@pytest.mark.asyncio
async def test_list_tools_advertises_both_tools() -> None:
    mcp = build_server()
    handler = mcp.request_handlers[mt.ListToolsRequest]
    result = await handler(mt.ListToolsRequest(method="tools/list"))
    names = {tool.name for tool in result.root.tools}
    assert names == {
        "get_eco_server_status",
        "list_public_eco_servers",
        "get_eco_ecoregion",
    }


@pytest.mark.asyncio
async def test_list_public_eco_servers_returns_curated_list() -> None:
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="list_public_eco_servers", arguments={}),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 2
    # Both blocks are TextContent by construction; narrow for mypy.
    assert isinstance(blocks[0], mt.TextContent)
    assert isinstance(blocks[1], mt.TextContent)
    md = blocks[0].text
    payload = json.loads(blocks[1].text)
    assert payload["servers"] == KNOWN_PUBLIC_SERVERS
    for s in KNOWN_PUBLIC_SERVERS:
        assert s["label"] in md
        assert s["host"] in md
