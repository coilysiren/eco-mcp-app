"""Unit tests for the `get_eco_map` tool.

Covers the pure-data helpers (polygon ordering, seam-splitting, payload
shaping) plus the end-to-end tool call wired through the MCP handler with
respx stubbing the Eco server.
"""

from __future__ import annotations

import json

import httpx
import mcp.types as mt
import pytest
import respx

from eco_mcp_app import map as eco_map
from eco_mcp_app.map import (
    ECO_BASE_URL_DEFAULT,
    _order_by_polar_angle,
    _split_seam_crossings,
    build_map_payload,
    build_polygons,
)
from eco_mcp_app.server import build_server

# Minimal 1x1 transparent GIF — enough bytes for the data-uri test to pass
# without pulling in real Eco map art.
_TINY_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
)


def _fake_dimension() -> dict[str, int]:
    return {"x": 720, "y": 200, "z": 720}


def _fake_property() -> dict[str, list[dict[str, int]]]:
    # Three deeds: a normal one, a seam-crosser (Gavin-style, wraps x=720→0),
    # and an empty-verts deed that should be dropped.
    return {
        "Alice's Homestead, Owner: alice": [
            {"x": 100, "y": 100},
            {"x": 120, "y": 100},
            {"x": 120, "y": 120},
            {"x": 100, "y": 120},
        ],
        "Gavin's Edge Plot, Owner: gavin": [
            {"x": 705, "y": 175},
            {"x": 715, "y": 180},
            {"x": 0, "y": 195},
            {"x": 5, "y": 185},
        ],
        "bob's Empty Cart Deed, Owner: bob": [],
    }


# ---- pure helpers ---------------------------------------------------------


def test_order_by_polar_angle_produces_stable_ring() -> None:
    pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    ordered = _order_by_polar_angle(pts)
    # Four distinct corners, still four points, same set.
    assert set(ordered) == set(pts)
    # Consecutive differences should all be short (no diagonal cut across
    # the square — which would happen if ordering were wrong).
    for i in range(len(ordered)):
        dx = ordered[i][0] - ordered[(i + 1) % 4][0]
        dy = ordered[i][1] - ordered[(i + 1) % 4][1]
        assert (dx * dx + dy * dy) ** 0.5 <= 2.001


def test_split_seam_crossings_does_not_split_interior_polygon() -> None:
    pts = [(100.0, 100.0), (120.0, 100.0), (120.0, 120.0), (100.0, 120.0)]
    out = _split_seam_crossings(pts, 720, 720)
    assert len(out) == 1
    assert out[0] == pts


def test_split_seam_crossings_splits_x_wrap() -> None:
    # Mimics Gavin's deed: verts on both sides of x=720→0 seam.
    pts = [(705.0, 175.0), (715.0, 180.0), (0.0, 195.0), (5.0, 185.0)]
    out = _split_seam_crossings(pts, 720, 720)
    # Two sub-polygons — the unwrapped contiguous polygon (high side of the
    # map) and its -world_x translate (low side). All 4 verts preserved
    # per copy; no polygon internally spans more than half the world.
    assert len(out) == 2
    for sub in out:
        assert len(sub) == 4
        xs = [p[0] for p in sub]
        assert (max(xs) - min(xs)) <= 360.0
    # The two copies are offset by exactly world_x on the x axis.
    xs_a = sorted(p[0] for p in out[0])
    xs_b = sorted(p[0] for p in out[1])
    offsets = {round(a - b, 3) for a, b in zip(xs_a, xs_b, strict=True)}
    assert offsets == {720.0}


def test_build_polygons_drops_empty_and_shapes_output() -> None:
    polys = build_polygons(_fake_property(), _fake_dimension(), render_size=512)
    # Alice (1) + Gavin's two halves (2) — empty deed dropped.
    assert len(polys) == 3
    owners = [p["owner"] for p in polys]
    assert owners.count("alice") == 1
    assert owners.count("gavin") == 2
    # Each polygon's points attribute is a non-empty, space-separated list of
    # "x,y" pairs. Seam-crossing copies may have coords outside [0, 512] by
    # design — the SVG viewBox clips them.
    for p in polys:
        assert p["points"]
        for pt in p["points"].split():
            x_s, y_s = pt.split(",")
            float(x_s)  # raises on malformed
            float(y_s)
    # At least one polygon for Alice sits fully inside the render frame.
    alice = next(p for p in polys if p["owner"] == "alice")
    for pt in alice["points"].split():
        x, y = (float(v) for v in pt.split(","))
        assert 0.0 <= x <= 512.0
        assert 0.0 <= y <= 512.0


def test_build_polygons_drops_sub_3_vert_deeds() -> None:
    tiny = {
        "Tiny Deed, Owner: nobody": [{"x": 1, "y": 1}, {"x": 2, "y": 2}],
    }
    assert build_polygons(tiny, _fake_dimension()) == []


def test_build_map_payload_shape() -> None:
    bundle = {
        "dimension": _fake_dimension(),
        "property": _fake_property(),
        "preview_gif": _TINY_GIF,
        "base_url": "http://eco.example.com:3001",
    }
    payload = build_map_payload(bundle)
    assert payload["view"] == "eco_map"
    assert payload["sourceUrl"] == "http://eco.example.com:3001"
    assert payload["worldDim"] == {"x": 720, "y": 200, "z": 720}
    # 2 unique deeds (alice + gavin), 2 unique owners — empty deed dropped.
    assert payload["deedCount"] == 2
    assert payload["ownerCount"] == 2
    assert payload["owners"] == ["alice", "gavin"]
    # Gavin's seam-split doubles the polygon count for that deed.
    assert len(payload["polygons"]) == 3
    # GIF bytes round-trip into a data URI.
    assert payload["gifDataUri"].startswith("data:image/gif;base64,")
    assert set(payload["owner_colors"]) == {"alice", "gavin"}


def test_build_map_payload_handles_no_deeds() -> None:
    bundle = {
        "dimension": _fake_dimension(),
        "property": {},
        "preview_gif": _TINY_GIF,
        "base_url": None,
    }
    payload = build_map_payload(bundle)
    assert payload["deedCount"] == 0
    assert payload["ownerCount"] == 0
    assert payload["polygons"] == []


# ---- upstream fetch -------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_map_bundle_hits_three_endpoints() -> None:
    dim_route = respx.get(f"{ECO_BASE_URL_DEFAULT}/api/v1/map/dimension").mock(
        return_value=httpx.Response(200, json=_fake_dimension())
    )
    prop_route = respx.get(f"{ECO_BASE_URL_DEFAULT}/api/v1/map/property").mock(
        return_value=httpx.Response(200, json=_fake_property())
    )
    gif_route = respx.get(f"{ECO_BASE_URL_DEFAULT}/Layers/WorldPreview.gif").mock(
        return_value=httpx.Response(200, content=_TINY_GIF)
    )
    bundle = await eco_map.fetch_map_bundle()
    assert dim_route.called
    assert prop_route.called
    assert gif_route.called
    assert bundle["dimension"]["x"] == 720
    assert bundle["preview_gif"] == _TINY_GIF
    assert bundle["base_url"] == ECO_BASE_URL_DEFAULT


@pytest.mark.asyncio
@respx.mock
async def test_fetch_map_bundle_respects_server_arg() -> None:
    base = "http://eco.example.com:5679"
    respx.get(f"{base}/api/v1/map/dimension").mock(
        return_value=httpx.Response(200, json=_fake_dimension())
    )
    respx.get(f"{base}/api/v1/map/property").mock(return_value=httpx.Response(200, json={}))
    gif_route = respx.get(f"{base}/Layers/WorldPreview.gif").mock(
        return_value=httpx.Response(200, content=_TINY_GIF)
    )
    bundle = await eco_map.fetch_map_bundle("eco.example.com:5679")
    assert gif_route.called
    assert bundle["base_url"] == base


@pytest.mark.asyncio
@respx.mock
async def test_fetch_map_bundle_raises_on_5xx() -> None:
    respx.get(f"{ECO_BASE_URL_DEFAULT}/api/v1/map/dimension").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await eco_map.fetch_map_bundle()


# ---- MCP tool surface -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_advertises_get_eco_map() -> None:
    mcp = build_server()
    handler = mcp.request_handlers[mt.ListToolsRequest]
    result = await handler(mt.ListToolsRequest(method="tools/list"))
    names = {tool.name for tool in result.root.tools}
    assert "get_eco_map" in names


@pytest.mark.asyncio
@respx.mock
async def test_get_eco_map_call_tool_returns_rendered_fragment() -> None:
    respx.get(f"{ECO_BASE_URL_DEFAULT}/api/v1/map/dimension").mock(
        return_value=httpx.Response(200, json=_fake_dimension())
    )
    respx.get(f"{ECO_BASE_URL_DEFAULT}/api/v1/map/property").mock(
        return_value=httpx.Response(200, json=_fake_property())
    )
    respx.get(f"{ECO_BASE_URL_DEFAULT}/Layers/WorldPreview.gif").mock(
        return_value=httpx.Response(200, content=_TINY_GIF)
    )
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_map", arguments={}),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 2
    assert isinstance(blocks[0], mt.TextContent)
    assert isinstance(blocks[1], mt.TextContent)
    # Markdown block summarizes deeds/owners.
    md = blocks[0].text
    assert "**2**" in md  # the deed count in bold
    assert "alice" in md
    assert "gavin" in md
    # JSON block omits the GIF data URI (it's huge); polygons shape is stable.
    payload = json.loads(blocks[1].text)
    assert "gifDataUri" not in payload
    assert payload["view"] == "eco_map"
    assert payload["deedCount"] == 2
    # Rendered partial (with polygons + image) ships in `_meta.ui.fragment`.
    assert result.root.meta is not None
    fragment = result.root.meta["ui"]["fragment"]
    assert "<polygon" in fragment
    assert "data:image/gif;base64" in fragment
    assert "alice" in fragment
    assert "gavin" in fragment


@pytest.mark.asyncio
@respx.mock
async def test_get_eco_map_call_tool_handles_upstream_failure() -> None:
    respx.get(f"{ECO_BASE_URL_DEFAULT}/api/v1/map/dimension").mock(
        side_effect=httpx.ConnectError("refused")
    )
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_map", arguments={}),
    )
    result = await handler(req)
    assert result.root.isError is True
    blocks = result.root.content
    assert isinstance(blocks[0], mt.TextContent)
    assert "unreachable" in blocks[0].text.lower()
