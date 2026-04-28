"""Tests for the species profile tool.

Exercises name cleaning, population CSV parsing, external fetch happy path,
and the graceful fallback for modded species that iNat can't find.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import mcp.types as mt
import pytest
import respx

from eco_mcp_app import species as species_mod
from eco_mcp_app.server import _resolve_species_id, build_server
from eco_mcp_app.species import (
    ECO_BASE_URL,
    INAT_BASE_URL,
    PopulationSample,
    _fetch_inat_taxon,
    build_species_payload,
    clean_species_name,
    fetch_species_population,
)


@pytest.fixture(autouse=True)
def _isolate_cache_and_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the SQLite cache at a tmp dir, stub the admin key, reset rate limiter."""
    from aiolimiter import AsyncLimiter

    monkeypatch.setenv(species_mod._CACHE_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("ECO_ADMIN_API_KEY", "test-key")
    # Bypass the committed data/species_profiles.json preload — these
    # tests exercise the live iNat + Wikipedia fetch paths via respx.
    monkeypatch.setenv("ECO_MCP_PRELOAD_DISABLE", "1")
    # Each test gets a fresh limiter so it never sees tokens carried over
    # from the previous test's fan-out.
    species_mod._inat_limiter = AsyncLimiter(
        species_mod._INAT_RATE_MAX, species_mod._INAT_RATE_WINDOW_S
    )


# --- Name cleaning --------------------------------------------------------


@pytest.mark.parametrize(
    ("species_id", "expected"),
    [
        ("WheatSpecies", "Wheat"),
        ("BisonSpecies", "Bison"),
        ("BighornSheepSpecies", "Bighorn Sheep"),
        ("MoonJellyfishSpecies", "Moon Jellyfish"),
        ("SnappingTurtleSpecies", "Snapping Turtle"),
        # Override — bare "Joshua" would miss, real name is Joshua Tree.
        ("JoshuaSpecies", "Joshua Tree"),
        ("DwarfWillowSpecies", "Dwarf Willow"),
        ("PacificSardineSpecies", "Pacific Sardine"),
    ],
)
def test_clean_species_name(species_id: str, expected: str) -> None:
    assert clean_species_name(species_id) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WheatSpecies", "WheatSpecies"),
        ("Wheat", "WheatSpecies"),
        ("Snapping Turtle", "SnappingTurtleSpecies"),
        ("moon jellyfish", "MoonJellyfishSpecies"),
        ("", ""),
    ],
)
def test_resolve_species_id(raw: str, expected: str) -> None:
    assert _resolve_species_id(raw) == expected


# --- Population CSV parsing -----------------------------------------------


@respx.mock
async def test_fetch_species_population_parses_csv() -> None:
    csv_body = '"Time","Value"\n"1","137"\n"600","139"\n"1200","141"\n'
    respx.get(f"{ECO_BASE_URL}/api/v1/exporter/species").mock(
        return_value=httpx.Response(200, text=csv_body)
    )
    samples = await fetch_species_population("WheatSpecies")
    assert len(samples) == 3
    # Seconds → days conversion.
    assert samples[0] == PopulationSample(day=1 / 86400.0, value=137)
    assert samples[2].value == 141
    assert samples[2].day == pytest.approx(1200 / 86400.0)


@respx.mock
async def test_fetch_species_population_empty_response() -> None:
    respx.get(f"{ECO_BASE_URL}/api/v1/exporter/species").mock(
        return_value=httpx.Response(200, text='"Time","Value"\n')
    )
    samples = await fetch_species_population("BisonSpecies")
    assert samples == []


# --- build_species_payload end-to-end -------------------------------------


_FAKE_INAT_TAXON = {
    "results": [
        {
            "id": 1,
            "name": "Bison bison",
            "preferred_common_name": "American Bison",
            "rank": "species",
            "wikipedia_url": "https://en.wikipedia.org/wiki/American_bison",
            "wikipedia_summary": (
                "<p>The American bison is a large mammal. "
                "It is the national mammal of the United States. "
                "More detail follows.</p>"
            ),
            "default_photo": {
                "medium_url": "https://static.inaturalist.org/photos/fake/medium.jpg",
                "attribution": "CC-BY",
            },
            "conservation_status": {"status_name": "near threatened"},
            "ancestors": [
                {"rank": "kingdom", "name": "Animalia", "preferred_common_name": "Animals"},
                {"rank": "class", "name": "Mammalia", "preferred_common_name": "Mammals"},
            ],
        }
    ]
}


@respx.mock
async def test_build_species_payload_happy_path() -> None:
    respx.get(f"{ECO_BASE_URL}/api/v1/exporter/species").mock(
        return_value=httpx.Response(
            200, text='"Time","Value"\n"600","40"\n"1200","39"\n"1800","41"\n'
        )
    )
    respx.get(f"{INAT_BASE_URL}/taxa").mock(return_value=httpx.Response(200, json=_FAKE_INAT_TAXON))
    respx.get("https://static.inaturalist.org/photos/fake/medium.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff-fakejpeg")
    )
    payload = await build_species_payload("BisonSpecies")
    d = payload.to_dict()
    assert d["name"] == "Bison"
    assert d["source"] == "inat"
    assert d["photoDataUri"] and d["photoDataUri"].startswith("data:image/jpeg;base64,")
    assert d["conservationStatus"] == "near threatened"
    assert len(d["population"]) == 3
    assert d["populationFirst"] == 40
    assert d["populationLatest"] == 41
    assert d["populationDelta"] == 1
    # Taxonomy includes ancestors + species row.
    names = [t["name"] for t in d["taxonomy"]]
    assert "Animals" in names
    assert "American Bison" in names
    # Extract was stripped and truncated.
    assert d["wikiExtract"]
    assert "<p>" not in d["wikiExtract"]


@respx.mock
async def test_build_species_payload_modded_fallback() -> None:
    """Modded species: iNat zero hits, Wikipedia zero hits — render cleanly."""
    respx.get(f"{ECO_BASE_URL}/api/v1/exporter/species").mock(
        return_value=httpx.Response(200, text='"Time","Value"\n"600","7"\n')
    )
    respx.get(f"{INAT_BASE_URL}/taxa").mock(return_value=httpx.Response(200, json={"results": []}))
    respx.get(host="en.wikipedia.org").mock(return_value=httpx.Response(404, json={}))
    payload = await build_species_payload("BunWulfSpecies")
    d = payload.to_dict()
    assert d["source"] == "none"
    assert d["photoDataUri"] is None
    assert d["taxonomy"] == []
    assert d["populationLatest"] == 7


@respx.mock
async def test_build_species_payload_wikipedia_fallback() -> None:
    """iNat misses, Wikipedia hits."""
    respx.get(f"{ECO_BASE_URL}/api/v1/exporter/species").mock(
        return_value=httpx.Response(200, text='"Time","Value"\n')
    )
    respx.get(f"{INAT_BASE_URL}/taxa").mock(return_value=httpx.Response(200, json={"results": []}))
    respx.get(host="en.wikipedia.org").mock(
        return_value=httpx.Response(
            200,
            json={
                "extract": "Joshua Tree is a yucca. It grows in the desert. Extra sentence.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Joshua_Tree"}},
            },
        )
    )
    payload = await build_species_payload("JoshuaSpecies")
    d = payload.to_dict()
    assert d["source"] == "wikipedia"
    assert d["wikiExtract"].startswith("Joshua Tree is a yucca.")
    assert "Extra sentence" not in d["wikiExtract"]
    assert d["wikiUrl"] == "https://en.wikipedia.org/wiki/Joshua_Tree"


@respx.mock
async def test_inat_response_is_cached() -> None:
    respx.get(f"{ECO_BASE_URL}/api/v1/exporter/species").mock(
        return_value=httpx.Response(200, text='"Time","Value"\n')
    )
    inat_route = respx.get(f"{INAT_BASE_URL}/taxa").mock(
        return_value=httpx.Response(200, json=_FAKE_INAT_TAXON)
    )
    respx.get(host="static.inaturalist.org").mock(return_value=httpx.Response(200, content=b"jpg"))
    await build_species_payload("BisonSpecies")
    await build_species_payload("BisonSpecies")
    # Second call must hit the SQLite cache, not iNat.
    assert inat_route.call_count == 1


# --- iNat re-ranking ------------------------------------------------------


@respx.mock
async def test_fetch_inat_taxon_bison_prefers_genus_over_grass() -> None:
    """`q=Bison` on iNat returns a grass first; we must pick the Bison genus."""
    payload = {
        "results": [
            {
                "id": 999,
                "name": "Anthoxanthum odoratum",
                "preferred_common_name": "Sweet Vernal Grass",
                "matched_term": "bison grass",
                "rank": "species",
                "ancestors": [
                    {"rank": "kingdom", "name": "Plantae"},
                    {"rank": "family", "name": "Poaceae"},
                ],
            },
            {
                "id": 42158,
                "name": "Bison",
                "preferred_common_name": "Bison",
                "matched_term": "Bison",
                "rank": "genus",
                "ancestors": [
                    {"rank": "kingdom", "name": "Animalia"},
                    {"rank": "family", "name": "Bovidae"},
                ],
            },
        ]
    }
    respx.get(f"{INAT_BASE_URL}/taxa").mock(return_value=httpx.Response(200, json=payload))
    taxon = await _fetch_inat_taxon("Bison")
    assert taxon is not None
    assert taxon["name"] == "Bison"
    assert taxon["rank"] == "genus"
    assert any(a.get("name") == "Bovidae" for a in taxon["ancestors"])


# --- MCP integration ------------------------------------------------------


@respx.mock
async def test_get_eco_species_tool_returns_card_blocks() -> None:
    respx.get(f"{ECO_BASE_URL}/api/v1/exporter/species").mock(
        return_value=httpx.Response(200, text='"Time","Value"\n"600","22"\n"1200","24"\n')
    )
    respx.get(f"{INAT_BASE_URL}/taxa").mock(return_value=httpx.Response(200, json=_FAKE_INAT_TAXON))
    respx.get(host="static.inaturalist.org").mock(return_value=httpx.Response(200, content=b"jpg"))

    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_species", arguments={"name": "Bison"}),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 2
    for b in blocks:
        assert isinstance(b, mt.TextContent)
    md, raw_json = blocks[0].text, blocks[1].text
    assert result.root.meta is not None
    fragment = result.root.meta["ui"]["fragment"]
    assert "Bison" in md
    payload = json.loads(raw_json)
    assert payload["speciesId"] == "BisonSpecies"
    assert payload["populationLatest"] == 24
    assert 'class="species"' in fragment
    assert "<svg" in fragment  # sparkline rendered
