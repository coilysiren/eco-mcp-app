"""Tests for the `get_eco_government` tool and its helpers.

Mocks the three civic endpoints with respx and exercises both the payload
shaping functions and the end-to-end tool path through the MCP server.
Fixture shapes mirror real responses observed against
`http://eco.coilysiren.me:3001` on Day 3 of Cycle 13 (elections empty,
laws contain markup tokens, titles have `OccupantNames` top-level).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import mcp.types as mt
import pytest
import respx

from eco_mcp_app.server import (
    DEFAULT_ECO_BASE_URL,
    build_server,
    fetch_eco_government,
    strip_law_markup,
    to_government_payload,
)

_TITLES_URL = f"{DEFAULT_ECO_BASE_URL}/api/v1/elections/titles"
_ELECTIONS_URL = f"{DEFAULT_ECO_BASE_URL}/api/v1/elections"
_LAWS_URL = f"{DEFAULT_ECO_BASE_URL}/api/v1/laws?byStates=Active"

_LAW_DESC = (
    '<style="Header">Roads</style>\n'
    "<color=#FFFFFFFF><i>Road Protection Laws</i></color>\n"
    'On event <style="InfoLight"><link="view:283:-1">'
    '<icon name="ClaimOrUnclaimProperty" type="">Claim Or Unclaim Property</icon>'
    "</link></style> then Prevent"
)

_FAKE_TITLES: list[dict[str, Any]] = [
    {
        "Table": [
            ["Property", "Description", "Steamtide Cay Foundation Mayor"],
            ["Election Process", "…", "Steamtide Foundation Election Process"],
            ["Eligible Candidates", "…", "Citizens of Steamtide"],
            ["Successor", "…", "None"],
            ["Who Can Remove From Office", "…", "None"],
            ["Term Limit Days", "…", "2"],
        ],
        "OccupantNames": ["Scuba Steve"],
        "Id": 339903,
        "Name": "Steamtide Cay Foundation Mayor",
        "State": "Active",
    }
]

_FAKE_LAWS: list[dict[str, Any]] = [
    {
        "Description": _LAW_DESC,
        "Id": 1,
        "Name": "Road Law",
        "State": "Active",
        "Creator": "alice",
    },
    {
        "Description": "old description " + _LAW_DESC,
        "Id": 2,
        "Name": "Old Law",
        "State": "Removed",  # must be filtered out client-side
        "Creator": "bob",
    },
]


def _mock_all(
    *,
    titles: list[dict[str, Any]] | None = None,
    elections: list[dict[str, Any]] | None = None,
    laws: list[dict[str, Any]] | None = None,
) -> None:
    respx.get(_TITLES_URL).mock(
        return_value=httpx.Response(200, json=titles if titles is not None else _FAKE_TITLES)
    )
    respx.get(_ELECTIONS_URL).mock(
        return_value=httpx.Response(200, json=elections if elections is not None else [])
    )
    respx.get(_LAWS_URL).mock(
        return_value=httpx.Response(200, json=laws if laws is not None else _FAKE_LAWS)
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_strip_law_markup_removes_all_four_families() -> None:
    src = (
        '<style="Header">Roads</style>'
        "<color=#FFFFFFFF>white text</color>"
        '<link="view:283:-1">anchor</link>'
        '<icon name="Claim" type="">icon body</icon>'
    )
    out = strip_law_markup(src)
    assert "<" not in out
    assert ">" not in out
    assert "Roads" in out
    assert "white text" in out
    assert "anchor" in out
    assert "icon body" in out


def test_strip_law_markup_strips_all_live_day3_tags() -> None:
    # Every tag family observed on the live Day-3 server law descriptions.
    src = (
        '<style="Header">Header</style>'
        "<color=#FFFFFFFF>colored</color>"
        '<link="view:283:-1">link body</link>'
        '<icon name="Claim" type="">icon body</icon>'
        "<i>italic</i><u>underline</u>"
        "<linktext>linktext body</linktext>"
        "<foldout>foldout body</foldout>"
        "<title>title body</title>"
    )
    out = strip_law_markup(src)
    assert "<" not in out, out
    assert ">" not in out, out
    for body in (
        "Header",
        "colored",
        "link body",
        "icon body",
        "italic",
        "underline",
        "linktext body",
        "foldout body",
        "title body",
    ):
        assert body in out, f"{body!r} missing from {out!r}"


def test_strip_law_markup_handles_none_and_empty() -> None:
    assert strip_law_markup(None) == ""
    assert strip_law_markup("") == ""
    assert strip_law_markup("plain text") == "plain text"


def test_to_government_payload_extracts_occupant_and_scope() -> None:
    payload = to_government_payload({"titles": _FAKE_TITLES, "elections": [], "laws": _FAKE_LAWS})
    assert payload["scope"] == "Steamtide Cay Foundation"
    assert len(payload["titles"]) == 1
    title = payload["titles"][0]
    assert title["occupants"] == ["Scuba Steve"]
    assert title["name"] == "Steamtide Cay Foundation Mayor"
    assert title["successor"] == "None"
    assert title["term_days"] == "2"


def test_to_government_payload_empty_elections_renders_no_active() -> None:
    payload = to_government_payload({"titles": _FAKE_TITLES, "elections": [], "laws": []})
    assert payload["elections"] == []
    assert payload["active_laws_count"] == 0
    assert payload["shortest_law"] is None
    assert payload["longest_law"] is None


def test_to_government_payload_filters_inactive_laws() -> None:
    payload = to_government_payload({"titles": _FAKE_TITLES, "elections": [], "laws": _FAKE_LAWS})
    # Only the `Active` law counts.
    assert payload["active_laws_count"] == 1
    assert payload["shortest_law"] is not None
    assert payload["shortest_law"]["name"] == "Road Law"
    # The four target tag families must be stripped from the preview.
    preview = payload["shortest_law"]["preview"]
    for token in (
        "<link",
        "</link>",
        "<icon",
        "</icon>",
        "<color",
        "</color>",
        "<style",
        "</style>",
    ):
        assert token not in preview, f"{token!r} not removed from {preview!r}"
    assert "Roads" in preview


def test_to_government_payload_no_titles_reports_unknown_scope() -> None:
    payload = to_government_payload({"titles": [], "elections": [], "laws": []})
    assert payload["scope"] == "Unknown settlement"
    assert payload["titles"] == []


def test_to_government_payload_election_countdown_from_timeleft_seconds() -> None:
    payload = to_government_payload(
        {
            "titles": _FAKE_TITLES,
            "elections": [{"Id": 1, "Name": "Mayor race", "TimeLeft": 7200}],
            "laws": [],
        }
    )
    assert payload["elections"][0]["ends_in_hours"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# fetch_eco_government (HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_eco_government_hits_all_three_endpoints() -> None:
    _mock_all()
    data = await fetch_eco_government()
    assert data["titles"] == _FAKE_TITLES
    assert data["elections"] == []
    assert data["laws"] == _FAKE_LAWS
    assert data["_sourceUrl"].endswith("/api/v1/elections/titles")


@pytest.mark.asyncio
@respx.mock
async def test_fetch_eco_government_forwards_server_arg() -> None:
    titles_route = respx.get("http://eco.example.com:5679/api/v1/elections/titles").mock(
        return_value=httpx.Response(200, json=_FAKE_TITLES)
    )
    respx.get("http://eco.example.com:5679/api/v1/elections").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("http://eco.example.com:5679/api/v1/laws?byStates=Active").mock(
        return_value=httpx.Response(200, json=[])
    )
    await fetch_eco_government("eco.example.com:5679")
    assert titles_route.called


@pytest.mark.asyncio
@respx.mock
async def test_fetch_eco_government_raises_on_5xx() -> None:
    respx.get(_TITLES_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_eco_government()


# ---------------------------------------------------------------------------
# End-to-end through the MCP server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_includes_government() -> None:
    mcp = build_server()
    handler = mcp.request_handlers[mt.ListToolsRequest]
    result = await handler(mt.ListToolsRequest(method="tools/list"))
    names = {tool.name for tool in result.root.tools}
    assert "get_eco_government" in names


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_get_eco_government_happy_path() -> None:
    _mock_all()
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_government", arguments={}),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 2
    for b in blocks:
        assert isinstance(b, mt.TextContent)
    md = blocks[0].text
    payload = json.loads(blocks[1].text)
    assert result.root.meta is not None
    html = result.root.meta["ui"]["fragment"]
    assert "Steamtide Cay Foundation" in md
    assert "Scuba Steve" in md
    assert payload["scope"] == "Steamtide Cay Foundation"
    assert "Scuba Steve" in html
    assert "No active elections" in html
    # Markup tokens must be scrubbed in the rendered HTML law preview.
    assert "<link=" not in html
    assert "<icon " not in html


@pytest.mark.asyncio
@respx.mock
async def test_call_tool_get_eco_government_handles_upstream_error() -> None:
    respx.get(_TITLES_URL).mock(side_effect=httpx.ConnectError("refused"))
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_government", arguments={}),
    )
    result = await handler(req)
    assert result.root.isError is True
    blocks = result.root.content
    assert isinstance(blocks[0], mt.TextContent)
    assert "unreachable" in blocks[0].text.lower()
