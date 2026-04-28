"""Unit coverage for the milestone tracker tool.

Mirrors the eco-spec-tracker sibling's respx pattern: stub the upstream
`/info` fetch so the test is hermetic, then drive the MCP `CallToolRequest`
handler and inspect both the JSON payload and the HTML fragment.
"""

from __future__ import annotations

import json

import httpx
import mcp.types as mt
import pytest
import respx

from eco_mcp_app import server as eco_server
from eco_mcp_app.server import (
    DEFAULT_ECO_INFO_URL,
    build_milestones_payload,
    build_server,
    parse_achievement,
)

# Sample shape matching the live Day-3 /info response. Keep the strings
# byte-accurate so regressions in the markup stripper surface here.
_ACHIEVEMENTS = {
    "Cultural Awakening": (
        "Create 250 total culture as a world.\n"
        '<style="Culture"><icon name="Culture" type="nobg"></icon>57.6 Culture</style>'
        ' from <style="Positive">2</style> works from <style="Positive">1</style> artists.'
    ),
    "Sparkling Canvas": (
        "Create 50 total culture as a world.\n"
        '<style="Culture"><icon name="Culture" type="nobg"></icon>22.72 Culture</style>'
        ' from <style="Positive">1</style> work from <style="Positive">1</style> artist.'
    ),
    "Cultural Trailblazers ": (
        "Create 100 total culture as a world.\n"
        '<style="Culture"><icon name="Culture" type="nobg"></icon>29.36 Culture</style>'
        ' from <style="Positive">1</style> work from <style="Positive">1</style> artist.'
    ),
    "Incipient Renaissance": (
        "Create 500 total culture as a world.\n"
        '<style="Culture"><icon name="Culture" type="nobg"></icon>57.04 Culture</style>'
        ' from <style="Positive">4</style> works from <style="Positive">3</style> artists.'
    ),
    "Cultural Vanguard": (
        "Create 1000 total culture as a world.\n"
        '<style="Culture"><icon name="Culture" type="nobg"></icon>122.35 Culture</style>'
        ' from <style="Positive">4</style> works from <style="Positive">3</style> artists.'
    ),
}

_FAKE_INFO: dict[str, object] = {
    "Description": "Eco via Sirens",
    "TotalCulture": 166.61038,
    "ServerAchievementsDict": _ACHIEVEMENTS,
}


@pytest.fixture(autouse=True)
def _clear_info_cache() -> None:
    eco_server._info_cache.clear()


def test_parse_achievement_extracts_target_and_current() -> None:
    row = parse_achievement(
        "Cultural Awakening",
        _ACHIEVEMENTS["Cultural Awakening"],
    )
    assert row["name"] == "Cultural Awakening"
    assert row["target"] == 250
    assert row["current"] == pytest.approx(57.6)
    # 57.6 / 250 ~= 23.04%
    assert 23.0 <= row["pct"] <= 23.1


def test_parse_achievement_strips_all_eco_markup() -> None:
    row = parse_achievement("X", _ACHIEVEMENTS["Sparkling Canvas"])
    stripped = row["stripped"]
    assert "<style" not in stripped
    assert "</style>" not in stripped
    assert "<icon" not in stripped
    assert "</icon>" not in stripped
    # Content survives
    assert "22.72 Culture" in stripped


def test_parse_achievement_handles_missing_numbers() -> None:
    # Nothing to parse — both fields end up None and pct is 0.0 rather than
    # crashing. Empty-state handling in the template depends on this.
    row = parse_achievement("weird", "Make culture happen.")
    assert row["target"] is None
    assert row["current"] is None
    assert row["pct"] == 0.0


def test_parse_achievement_handles_empty_string() -> None:
    row = parse_achievement("blank", "")
    assert row["target"] is None
    assert row["current"] is None
    assert row["pct"] == 0.0
    assert row["stripped"] == ""


def test_build_milestones_payload_sorts_by_pct_descending() -> None:
    info = {
        "TotalCulture": 166.61,
        "ServerAchievementsDict": _ACHIEVEMENTS,
    }
    payload = build_milestones_payload(info)
    assert payload["view"] == "eco_milestones"
    assert payload["totalCulture"] == pytest.approx(166.61)
    pcts = [m["pct"] for m in payload["milestones"]]
    assert pcts == sorted(pcts, reverse=True)
    # Sparkling Canvas (22.72/50 = 45.4%) should be on top.
    assert payload["milestones"][0]["name"] == "Sparkling Canvas"


def test_build_milestones_payload_handles_empty_achievements() -> None:
    payload = build_milestones_payload({"TotalCulture": 0})
    assert payload["milestones"] == []
    assert payload["totalCulture"] == 0.0


@pytest.mark.asyncio
async def test_list_tools_advertises_milestones() -> None:
    mcp = build_server()
    handler = mcp.request_handlers[mt.ListToolsRequest]
    result = await handler(mt.ListToolsRequest(method="tools/list"))
    names = {tool.name for tool in result.root.tools}
    assert "get_eco_milestones" in names


@pytest.mark.asyncio
@respx.mock
async def test_call_get_eco_milestones_happy_path() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(200, json=_FAKE_INFO))
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_milestones", arguments={}),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 2
    for b in blocks:
        assert isinstance(b, mt.TextContent)

    md, json_block = blocks
    assert isinstance(md, mt.TextContent)
    assert isinstance(json_block, mt.TextContent)
    assert "TotalCulture" in md.text
    payload = json.loads(json_block.text)
    assert payload["view"] == "eco_milestones"
    assert len(payload["milestones"]) == 5
    # HTML fragment now travels in `_meta.ui.fragment`, off the content array.
    assert result.root.meta is not None
    html = result.root.meta["ui"]["fragment"]
    # Top-line total culture rendered.
    assert "166.6" in html
    # Markup stripped — no Eco inline tags leak into rendered HTML.
    assert "<style=" not in html
    assert "<icon " not in html
    # Sorted top entry matches payload[0].
    top_name = payload["milestones"][0]["name"]
    assert top_name in html


@pytest.mark.asyncio
@respx.mock
async def test_call_get_eco_milestones_empty_achievements() -> None:
    # Early-cycle server: /info returns but ServerAchievementsDict is {}.
    respx.get(DEFAULT_ECO_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={"TotalCulture": 0.0, "ServerAchievementsDict": {}},
        )
    )
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_milestones", arguments={}),
    )
    result = await handler(req)
    assert result.root.meta is not None
    html = result.root.meta["ui"]["fragment"]
    # Empty state copy, not a crash.
    assert "No milestones" in html or "no milestones" in html.lower()


@pytest.mark.asyncio
@respx.mock
async def test_call_get_eco_milestones_upstream_error_renders_error_fragment() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(side_effect=httpx.ConnectError("refused"))
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_milestones", arguments={}),
    )
    result = await handler(req)
    assert result.root.isError is True
    blocks = result.root.content
    assert isinstance(blocks[0], mt.TextContent)
    assert "unreachable" in blocks[0].text.lower()


@pytest.mark.asyncio
@respx.mock
async def test_call_get_eco_milestones_forwards_server_arg() -> None:
    route = respx.get("http://eco.example.com:5679/info").mock(
        return_value=httpx.Response(200, json=_FAKE_INFO)
    )
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(
            name="get_eco_milestones",
            arguments={"server": "eco.example.com:5679"},
        ),
    )
    await handler(req)
    assert route.called
