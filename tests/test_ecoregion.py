"""Tests for the biodiversity + ecoregion-match tool.

Uses respx to stub the public worldlayers endpoint and the admin exporter
routes, so we verify:

  - biome percentages parse out of the categorized response
  - the vector normalizes to sum=1.0 (acceptance)
  - cosine similarity is deterministic + sensible (acceptance)
  - drift ranking handles empty / sparse series without crashing
  - the full MCP tool call handles a missing API key gracefully
"""

from __future__ import annotations

import json
import math

import httpx
import mcp.types as mt
import pytest
import respx

from eco_mcp_app import ecoregion as eco
from eco_mcp_app.server import (
    DEFAULT_ECO_INFO_URL,
    _render_ecoregion_card,
    build_server,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    eco._clear_caches()


# ---- pure-function tests ----


def test_extract_biome_percents_picks_only_biome_category() -> None:
    cats = [
        {
            "Category": "Biome",
            "List": [
                {"LayerName": "TaigaBiome", "Summary": "1%"},
                {"LayerName": "ForestBiome", "Summary": "4.5%"},
                {"LayerName": "UnknownBiome", "Summary": "99%"},
            ],
        },
        {
            "Category": "Animal",
            "List": [{"LayerName": "TaigaBiome", "Summary": "50%"}],
        },
    ]
    out = eco.extract_biome_percents(cats)
    assert out["TaigaBiome"] == 1.0
    assert out["ForestBiome"] == 4.5
    # Every expected biome key is present, even when absent from the response.
    assert set(out) == set(eco.BIOME_LAYERS)
    # Animal-category rows must not leak through.
    assert out["DesertBiome"] == 0.0


def test_normalize_vector_sums_to_one() -> None:
    raw = {"A": 10.0, "B": 30.0, "C": 60.0}
    norm = eco.normalize_vector(raw)
    assert math.isclose(sum(norm.values()), 1.0)
    assert math.isclose(norm["B"], 0.3)


def test_normalize_vector_zero_input_returns_zero() -> None:
    raw = {"A": 0.0, "B": 0.0}
    norm = eco.normalize_vector(raw)
    assert all(v == 0.0 for v in norm.values())


def test_cosine_similarity_identical_vectors_is_one() -> None:
    a = {"X": 0.3, "Y": 0.7}
    assert math.isclose(eco.cosine_similarity(a, a), 1.0)


def test_cosine_similarity_orthogonal_is_zero() -> None:
    a = {"X": 1.0, "Y": 0.0}
    b = {"X": 0.0, "Y": 1.0}
    assert eco.cosine_similarity(a, b) == 0.0


def test_top_ecoregions_is_deterministic_and_ranked() -> None:
    # Craft a world that's obviously desert-y.
    normalized = eco.normalize_vector(
        {
            "DesertBiome": 60.0,
            "GrasslandBiome": 20.0,
            "OceanBiome": 10.0,
            "TaigaBiome": 0.0,
        }
    )
    regions = [
        {
            "name": "Sahara",
            "description": "desert",
            "biome_vector": {"DesertBiome": 0.9, "GrasslandBiome": 0.1},
        },
        {
            "name": "Taiga",
            "description": "conifer",
            "biome_vector": {"TaigaBiome": 1.0},
        },
        {
            "name": "Grassland",
            "description": "plain",
            "biome_vector": {"GrasslandBiome": 0.8, "DesertBiome": 0.2},
        },
    ]
    matches = eco.top_ecoregions(normalized, regions, n=3)
    # Sahara should beat Grassland should beat Taiga.
    assert [m.name for m in matches] == ["Sahara", "Grassland", "Taiga"]
    # Deterministic across calls.
    again = eco.top_ecoregions(normalized, regions, n=3)
    assert [m.name for m in again] == [m.name for m in matches]


def test_compute_drift_handles_single_sample() -> None:
    assert eco.compute_drift([(0, 100.0)]) is None


def test_compute_drift_relative_delta() -> None:
    d = eco.compute_drift([(0, 100.0), (600, 150.0)])
    assert d is not None
    assert math.isclose(d.delta_rel, 0.5)
    assert d.first == 100.0
    assert d.latest == 150.0


def test_compute_drift_sorts_by_time() -> None:
    d = eco.compute_drift([(1200, 80.0), (0, 100.0), (600, 90.0)])
    assert d is not None
    assert d.first == 100.0
    assert d.latest == 80.0


def test_rank_drift_splits_boom_and_bust() -> None:
    series = {
        "Rising": [(0, 100.0), (600, 200.0)],
        "Falling": [(0, 100.0), (600, 50.0)],
        "Flat": [(0, 100.0), (600, 100.0)],
    }
    boom, bust = eco.rank_drift(series, n=5)
    assert [d.name for d in boom] == ["Rising"]
    assert [d.name for d in bust] == ["Falling"]


def test_load_ecoregions_bundled_returns_committed_fixture() -> None:
    regions = eco._load_ecoregions_bundled()
    # At least a handful of regions committed so the match section always
    # renders at least top-3.
    assert len(regions) >= 3
    # Every entry has the shape the payload builder assumes.
    for r in regions:
        assert "name" in r and "biome_vector" in r


# ---- integration through the MCP tool surface ----


@respx.mock
async def test_gather_ecoregion_payload_public_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No API key available → drift section renders an empty state, tool still succeeds."""
    monkeypatch.delenv("ECO_ADMIN_TOKEN", raising=False)
    respx.get("http://eco.coilysiren.me:3001/api/v1/worldlayers/layers").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "Category": "Biome",
                    "List": [
                        {"LayerName": "OceanBiome", "Summary": "13%"},
                        {"LayerName": "ForestBiome", "Summary": "4%"},
                        {"LayerName": "DesertBiome", "Summary": "4%"},
                    ],
                }
            ],
        )
    )
    payload = await eco.gather_ecoregion_payload(DEFAULT_ECO_INFO_URL, api_key=None)
    assert payload["view"] == "eco_ecoregion"
    ocean = next(b for b in payload["biomes"] if b["name"] == "OceanBiome")
    assert ocean["percent"] == 13.0
    assert math.isclose(payload["rawSumPercent"], 21.0)
    assert math.isclose(payload["unclassifiedPercent"], 79.0)
    assert payload["adminAvailable"] is False
    assert payload["drift"]["boom"] == []
    assert payload["drift"]["bust"] == []


@respx.mock
async def test_gather_ecoregion_payload_with_admin() -> None:
    respx.get("http://eco.coilysiren.me:3001/api/v1/worldlayers/layers").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "Category": "Biome",
                    "List": [
                        {"LayerName": "ForestBiome", "Summary": "10%"},
                        {"LayerName": "GrasslandBiome", "Summary": "5%"},
                    ],
                }
            ],
        )
    )
    respx.get("http://eco.coilysiren.me:3001/api/v1/exporter/specieslist").mock(
        return_value=httpx.Response(200, text="Deer\nWolf\n\nRabbit\n"),
    )
    respx.get(
        "http://eco.coilysiren.me:3001/api/v1/exporter/species",
        params={"speciesName": "Deer"},
    ).mock(
        return_value=httpx.Response(
            200,
            text='"Time","Value"\n"0","100"\n"600","200"\n',
        )
    )
    respx.get(
        "http://eco.coilysiren.me:3001/api/v1/exporter/species",
        params={"speciesName": "Wolf"},
    ).mock(
        return_value=httpx.Response(
            200,
            text='"Time","Value"\n"0","50"\n"600","25"\n',
        )
    )
    respx.get(
        "http://eco.coilysiren.me:3001/api/v1/exporter/species",
        params={"speciesName": "Rabbit"},
    ).mock(
        return_value=httpx.Response(
            200,
            text='"Time","Value"\n"0","200"\n"600","200"\n',
        )
    )
    payload = await eco.gather_ecoregion_payload(DEFAULT_ECO_INFO_URL, api_key="test-token")
    assert payload["adminAvailable"] is True
    assert payload["drift"]["speciesSeen"] == 3
    boom_names = [d["name"] for d in payload["drift"]["boom"]]
    bust_names = [d["name"] for d in payload["drift"]["bust"]]
    assert "Deer" in boom_names
    assert "Wolf" in bust_names
    assert "Rabbit" not in boom_names and "Rabbit" not in bust_names


@respx.mock
async def test_gather_ecoregion_handles_admin_403() -> None:
    """A 4xx on the admin endpoint should degrade, not crash."""
    respx.get("http://eco.coilysiren.me:3001/api/v1/worldlayers/layers").mock(
        return_value=httpx.Response(
            200,
            json=[{"Category": "Biome", "List": [{"LayerName": "OceanBiome", "Summary": "13%"}]}],
        )
    )
    respx.get("http://eco.coilysiren.me:3001/api/v1/exporter/specieslist").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    payload = await eco.gather_ecoregion_payload(DEFAULT_ECO_INFO_URL, api_key="bad-token")
    assert payload["adminAvailable"] is False
    assert payload["drift"]["boom"] == []


def test_render_ecoregion_card_smoke() -> None:
    """The card template renders against a minimal payload without errors."""
    payload = {
        "view": "eco_ecoregion",
        "sourceUrl": "http://eco.example.com:3001/info",
        "biomes": [
            {"name": "OceanBiome", "display": "Ocean", "percent": 13.0, "color": "#4a9cb8"},
            {"name": "ForestBiome", "display": "Forest", "percent": 4.0, "color": "#5a8a3a"},
        ],
        "unclassifiedPercent": 83.0,
        "rawSumPercent": 17.0,
        "ecoregionMatches": [
            {"name": "Indo-Pacific archipelago", "description": "islands", "similarity": 0.82},
        ],
        "drift": {
            "boom": [],
            "bust": [],
            "speciesSeen": 0,
            "speciesWithDrift": 0,
        },
        "adminAvailable": False,
    }
    html = _render_ecoregion_card(payload)
    assert "Biodiversity" in html
    assert "Indo-Pacific archipelago" in html
    # Empty state for drift when admin unavailable.
    assert "Admin endpoints unavailable" in html


@respx.mock
async def test_mcp_tool_call_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ECO_ADMIN_TOKEN", raising=False)
    respx.get("http://eco.coilysiren.me:3001/api/v1/worldlayers/layers").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "Category": "Biome",
                    "List": [{"LayerName": "OceanBiome", "Summary": "13%"}],
                }
            ],
        )
    )
    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_ecoregion", arguments={}),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 3
    assert isinstance(blocks[0], mt.TextContent)
    assert isinstance(blocks[1], mt.TextContent)
    assert isinstance(blocks[2], mt.TextContent)
    md = blocks[0].text
    payload = json.loads(blocks[1].text)
    fragment = blocks[2].text
    assert "Biome composition" in md
    assert payload["view"] == "eco_ecoregion"
    assert fragment.startswith("HTMX:")
    assert "ecoregion" in fragment
