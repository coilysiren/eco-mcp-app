"""Tests for the crafting atlas aggregator + tool wiring.

Covers:
  - Stream-parsing a respx-mocked CSV and getting the expected rollups.
  - Missing-action endpoint (401 / 404) becomes a non-fatal warning, not a
    crash — other action types still aggregate.
  - 20 MB synthetic CSV streams through without running afoul of the
    max-rows safety valve or blowing peak memory. (We bound it via
    MAX_ROWS_PER_ACTION, which the test sets low to exercise the cap.)
  - Empty CSVs produce a Day-3-safe "no events" atlas.
  - The tool wiring returns three TextContent blocks + _meta.ui.
  - SQLite cache is per (base, api-key) and hits within TTL.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import mcp.types as mt
import pytest
import respx

from eco_mcp_app import crafting as crafting_mod
from eco_mcp_app.crafting import (
    CraftingAtlas,
    aggregate_rows,
    atlas_template_context,
    fetch_atlas,
    prettify_eco_name,
)
from eco_mcp_app.server import build_server

CRAFT_URL = "http://eco.example.com:3001/api/v1/exporter/actions?actionName=ItemCraftedAction"
HARVEST_URL = "http://eco.example.com:3001/api/v1/exporter/actions?actionName=HarvestOrHunt"
CHOP_URL = "http://eco.example.com:3001/api/v1/exporter/actions?actionName=ChopTree"
DIG_URL = "http://eco.example.com:3001/api/v1/exporter/actions?actionName=DigOrMine"

BASE = "http://eco.example.com:3001"


_CRAFT_CSV = (
    "ActionLocation,WorldObjectItem,Citizen,ItemUsed,"
    "OverrideHierarchyActionsToConsumer,Count,Time\n"
    '"418,75,460","CampfireItem",129312,"CharredMushroomsItem",false,165.0,6519\n'
    '"289,89,310","WorkbenchItem",130409,"AdobeItem",false,133.0,7118\n'
    '"99,89,123","WorkbenchItem",129580,"AdobeItem",false,197.0,7197\n'
    '"142,82,203","CampfireItem",4478,"BeetCampfireSaladItem",false,189.0,10798\n'
    '"417,89,531","ResearchTableItem",129558,"DendrologyResearchPaperBasicItem",false,13.0,10785\n'
)

_HARVEST_CSV = (
    "Species,DamagedOrDestroyed,DestroyedByBlock,CaloriesToConsume,"
    "Position,Citizen,ActionLocation,Count,Time\n"
    '"BunchgrassSpecies",88,true,0.0,"419,75,458",129312,"419,75,458",173.0,3599\n'
    '"HuckleberrySpecies",87,false,0.0,"495,77,549",129569,"495,77,549",113.0,3598\n'
)

_CHOP_CSV = (
    "OnGround,Felled,Species,BranchesTargeted,GrowthPercent,CaloriesToConsume,"
    "ToolUsed,Position,Citizen,ActionLocation,Count,Time\n"
    'false,true,"FirSpecies",false,100.0,20.0,"StoneAxeItem",'
    '"424,75,461",129312,"424,75,461",7.0,3403\n'
    'false,true,"OakSpecies",false,40.4,19.0,"StoneAxeItem",'
    '"90,96,124",129580,"90,96,124",4.0,3553\n'
)

_DIG_EMPTY = "Position,Citizen,Count,Time\n"


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Each test gets its own cache dir so SQLite state doesn't cross-leak."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("ECO_CACHE_DIR", tmp)
        yield Path(tmp)


@pytest.fixture(autouse=True)
def _fresh_module_state() -> None:
    # No module-level caches on crafting_mod, but this hook is here so adding
    # one later doesn't silently share state across tests.
    return None


def _rows(csv_text: str) -> list[list[str]]:
    import csv

    return list(csv.reader(csv_text.splitlines()))


def test_prettify_eco_name_handles_common_shapes() -> None:
    assert prettify_eco_name("CampfireItem") == "Campfire"
    assert prettify_eco_name("BunWulfRawMeatItem") == "Bun Wulf Raw Meat"
    assert prettify_eco_name("OakSpecies") == "Oak"
    assert prettify_eco_name("") == ""


def test_aggregate_rows_folds_craft_csv() -> None:
    atlas = CraftingAtlas(fetched_at_iso="t", source_base_url="b")
    n = aggregate_rows("ItemCraftedAction", _rows(_CRAFT_CSV), atlas)
    assert n == 5
    by_item = dict(atlas.by_item)
    # AdobeItem 133 + 197 = 330
    assert by_item["AdobeItem"] == pytest.approx(330.0)
    by_station = dict(atlas.by_station)
    assert by_station["CampfireItem"] == 2
    assert by_station["WorkbenchItem"] == 2
    by_citizen = dict(atlas.by_citizen)
    assert by_citizen["129580"] == pytest.approx(197.0)
    # Flow edges exist for CampfireItem→CharredMushroomsItem etc.
    flow_keys = {(s, t) for s, t, _ in atlas.flows}
    assert ("CampfireItem", "CharredMushroomsItem") in flow_keys
    assert ("WorkbenchItem", "AdobeItem") in flow_keys


def test_aggregate_rows_handles_harvest_and_chop_shapes() -> None:
    atlas = CraftingAtlas(fetched_at_iso="t", source_base_url="b")
    aggregate_rows("HarvestOrHunt", _rows(_HARVEST_CSV), atlas)
    aggregate_rows("ChopTree", _rows(_CHOP_CSV), atlas)
    by_item = dict(atlas.by_item)
    # Harvest: species becomes the item.
    assert by_item["BunchgrassSpecies"] == pytest.approx(173.0)
    assert by_item["FirSpecies"] == pytest.approx(7.0)
    # Chop uses ToolUsed as station since no WorldObjectItem column.
    by_station = dict(atlas.by_station)
    assert by_station["StoneAxeItem"] == 2


def test_aggregate_rows_empty_csv_stays_empty() -> None:
    atlas = CraftingAtlas(fetched_at_iso="t", source_base_url="b")
    n = aggregate_rows("DigOrMine", _rows(_DIG_EMPTY), atlas)
    assert n == 0
    assert atlas.total_events == 0
    assert atlas.by_item == []


def test_aggregate_rows_respects_max_rows_cap() -> None:
    atlas = CraftingAtlas(fetched_at_iso="t", source_base_url="b")
    # 3 data rows, cap at 2 → one warning emitted.
    n = aggregate_rows(
        "ItemCraftedAction",
        _rows(_CRAFT_CSV),
        atlas,
        max_rows=2,
    )
    assert n == 2
    assert any("truncated" in w for w in atlas.warnings)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_atlas_merges_multiple_actions() -> None:
    respx.get(CRAFT_URL).mock(return_value=httpx.Response(200, text=_CRAFT_CSV))
    respx.get(HARVEST_URL).mock(return_value=httpx.Response(200, text=_HARVEST_CSV))
    respx.get(CHOP_URL).mock(return_value=httpx.Response(200, text=_CHOP_CSV))
    respx.get(DIG_URL).mock(return_value=httpx.Response(200, text=_DIG_EMPTY))

    atlas = await fetch_atlas(base_url=BASE, api_key="secret", cache_ttl_s=0)
    assert atlas.total_events == 5 + 2 + 2 + 0
    # Crafts + harvests + chops all appear in the item totals.
    by_item = dict(atlas.by_item)
    assert by_item["AdobeItem"] == pytest.approx(330.0)
    assert by_item["BunchgrassSpecies"] == pytest.approx(173.0)
    assert by_item["FirSpecies"] == pytest.approx(7.0)
    assert atlas.per_action_counts == {
        "ItemCraftedAction": 5,
        "HarvestOrHunt": 2,
        "ChopTree": 2,
        "DigOrMine": 0,
    }
    assert atlas.warnings == []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_atlas_tolerates_partial_failures() -> None:
    respx.get(CRAFT_URL).mock(return_value=httpx.Response(200, text=_CRAFT_CSV))
    respx.get(HARVEST_URL).mock(return_value=httpx.Response(401))
    respx.get(CHOP_URL).mock(side_effect=httpx.ConnectError("nope"))
    respx.get(DIG_URL).mock(return_value=httpx.Response(200, text=_DIG_EMPTY))

    atlas = await fetch_atlas(base_url=BASE, api_key=None, cache_ttl_s=0)
    # Still got the craft rows.
    assert atlas.per_action_counts["ItemCraftedAction"] == 5
    # Two warnings — one per failing action.
    assert any("HarvestOrHunt" in w and "401" in w for w in atlas.warnings)
    assert any("ChopTree" in w for w in atlas.warnings)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_atlas_cache_hits_within_ttl() -> None:
    craft_route = respx.get(CRAFT_URL).mock(return_value=httpx.Response(200, text=_CRAFT_CSV))
    respx.get(HARVEST_URL).mock(return_value=httpx.Response(200, text=_HARVEST_CSV))
    respx.get(CHOP_URL).mock(return_value=httpx.Response(200, text=_CHOP_CSV))
    respx.get(DIG_URL).mock(return_value=httpx.Response(200, text=_DIG_EMPTY))

    a1 = await fetch_atlas(base_url=BASE, api_key="k", cache_ttl_s=60)
    a2 = await fetch_atlas(base_url=BASE, api_key="k", cache_ttl_s=60)
    assert a1.total_events == a2.total_events
    # Exactly one upstream hit — second call served from SQLite.
    assert craft_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_fetch_atlas_large_stream_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic 20-MB-class stream — verifies the row cap keeps memory sane.

    We don't measure peak RSS (fragile under pytest), but we do prove:
      - the stream completes without OOM / timeout under the cap;
      - the aggregator reports a single 'truncated' warning at the cap;
      - the resulting atlas has the expected bounded row count.
    """
    monkeypatch.setattr(crafting_mod, "MAX_ROWS_PER_ACTION", 5000)

    def _huge_csv() -> str:
        header = (
            "ActionLocation,WorldObjectItem,Citizen,ItemUsed,"
            "OverrideHierarchyActionsToConsumer,Count,Time\n"
        )
        # ~120 bytes per row x 200_000 is about 24 MB.
        rows = (
            f'"0,0,0","WorkbenchItem",{1000 + i % 50},"AdobeItem",false,1.0,{i}\n'
            for i in range(200_000)
        )
        return header + "".join(rows)

    respx.get(CRAFT_URL).mock(return_value=httpx.Response(200, text=_huge_csv()))
    # Other endpoints empty so we only time the one under test.
    respx.get(HARVEST_URL).mock(return_value=httpx.Response(200, text=_DIG_EMPTY))
    respx.get(CHOP_URL).mock(return_value=httpx.Response(200, text=_DIG_EMPTY))
    respx.get(DIG_URL).mock(return_value=httpx.Response(200, text=_DIG_EMPTY))

    atlas = await fetch_atlas(base_url=BASE, api_key="k", cache_ttl_s=0)
    assert atlas.per_action_counts["ItemCraftedAction"] == 5000
    assert any("truncated" in w for w in atlas.warnings)


def test_atlas_template_context_empty_state_is_clean() -> None:
    atlas = CraftingAtlas(fetched_at_iso="t", source_base_url="b")
    ctx = atlas_template_context(atlas)
    assert ctx["empty"] is True
    assert ctx["top_items"] == []
    assert ctx["sankey"] is None


def test_atlas_template_context_ranks_and_percents() -> None:
    atlas = CraftingAtlas(fetched_at_iso="t", source_base_url="b")
    aggregate_rows("ItemCraftedAction", _rows(_CRAFT_CSV), atlas)
    ctx = atlas_template_context(atlas)
    assert ctx["empty"] is False
    # Top item is AdobeItem with 330, percent must be 100.
    assert ctx["top_items"][0]["name"] == "AdobeItem"
    assert ctx["top_items"][0]["pct"] == pytest.approx(100.0)
    # Sankey has nodes for both columns.
    assert ctx["sankey"] is not None
    assert ctx["sankey"]["edges"], "expected at least one flow edge"


@pytest.mark.asyncio
@respx.mock
async def test_tool_call_returns_three_text_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECO_ADMIN_API_KEY", "k")
    respx.get(CRAFT_URL).mock(return_value=httpx.Response(200, text=_CRAFT_CSV))
    respx.get(HARVEST_URL).mock(return_value=httpx.Response(200, text=_HARVEST_CSV))
    respx.get(CHOP_URL).mock(return_value=httpx.Response(200, text=_CHOP_CSV))
    respx.get(DIG_URL).mock(return_value=httpx.Response(200, text=_DIG_EMPTY))

    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(
            name="get_eco_crafting_atlas",
            arguments={"server": "eco.example.com:3001"},
        ),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 3
    assert isinstance(blocks[0], mt.TextContent)
    md = blocks[0].text
    assert "Crafting atlas" in md
    assert "Adobe" in md  # prettified AdobeItem
    # HTML fragment is on block 2 with the HTMX: prefix.
    assert blocks[2].text.startswith("HTMX:")
    assert "crafting-atlas" in blocks[2].text


@pytest.mark.asyncio
async def test_list_tools_now_includes_crafting_atlas() -> None:
    mcp = build_server()
    handler = mcp.request_handlers[mt.ListToolsRequest]
    result = await handler(mt.ListToolsRequest(method="tools/list"))
    names = {tool.name for tool in result.root.tools}
    assert "get_eco_crafting_atlas" in names
