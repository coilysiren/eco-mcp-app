"""Unit tests for the `get_eco_economy` tool.

Covers: dataset fan-out + KPI computation (happy path), Day-3 empty-data
paths, admin token absent → empty-state branch, classification thresholds,
and the MCP tool wiring end-to-end.

Uses respx to mock both /info and /datasets/get, mirroring the pattern in
tests/test_fetch_eco_info.py and the sibling eco-spec-tracker repo.
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
    ECONOMY_DATASETS,
    build_server,
    compute_economy_payload,
    fetch_economy,
)

# Base URL of the default Eco server, derived the same way server.py derives it.
_DEFAULT_BASE = DEFAULT_ECO_INFO_URL.rsplit("/info", 1)[0]
_DATASET_URL = f"{_DEFAULT_BASE}/datasets/get"


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a clean slate for info + economy caches + admin token."""
    eco_server._info_cache.clear()
    eco_server._economy_cache.clear()
    eco_server._admin_token_cache.clear()
    # An explicit env var sidesteps SSM/boto3 — tests must never reach AWS.
    monkeypatch.setenv("ECO_ADMIN_TOKEN", "test-token")


def _info_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Description": "Eco via Sirens",
        "Category": "Test",
        "DaysRunning": 3,
        "TimeSinceStart": 3 * 3600,
        "EconomyDesc": "524 trades, 0 contracts",
        "TotalCulture": 142.0,
    }
    base.update(overrides)
    return base


def _series_points(vals: list[float]) -> list[dict[str, float]]:
    """Dataset format matches the live endpoint: list of {Time, Value}."""
    return [{"Time": float(i), "Value": float(v)} for i, v in enumerate(vals)]


def _mock_all_datasets(values: dict[str, list[float]]) -> None:
    """Mock /datasets/get, routing by the `dataset` query param."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("dataset", "")
        pts = _series_points(values.get(name, []))
        return httpx.Response(200, json=pts)

    respx.get(_DATASET_URL).mock(side_effect=handler)


# ---------------------------------------------------------------------------
# fetch_economy: wiring + degradation paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_economy_sends_admin_token_header() -> None:
    info_route = respx.get(DEFAULT_ECO_INFO_URL).mock(
        return_value=httpx.Response(200, json=_info_body())
    )
    _mock_all_datasets({name: [1, 2, 3] for name in ECONOMY_DATASETS})

    raw = await fetch_economy()
    # 1 /info + 14 dataset fan-out calls.
    assert info_route.called
    dataset_calls = [c for c in respx.calls if str(c.request.url).startswith(_DATASET_URL)]
    assert len(dataset_calls) == len(ECONOMY_DATASETS)
    for call in dataset_calls:
        assert call.request.headers.get("X-API-Key") == "test-token"
    assert raw["admin_ok"] is True
    assert raw["days_elapsed"] == 3
    assert set(raw["series"].keys()) == set(ECONOMY_DATASETS)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_economy_empty_without_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token → /datasets/get is never called; each series is an empty list."""
    monkeypatch.delenv("ECO_ADMIN_TOKEN", raising=False)
    # Also defeat any ambient SSM/boto3 by making _load_admin_token see no env.
    eco_server._admin_token_cache.clear()
    monkeypatch.setattr(eco_server, "_load_admin_token", lambda: None)

    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(200, json=_info_body()))
    ds_route = respx.get(_DATASET_URL).mock(return_value=httpx.Response(200, json=[]))

    raw = await fetch_economy()
    assert raw["admin_ok"] is False
    assert not ds_route.called
    assert all(pts == [] for pts in raw["series"].values())


@pytest.mark.asyncio
@respx.mock
async def test_fetch_economy_tolerates_per_dataset_500() -> None:
    """A single 500 from one dataset must not blow up the rest of the card."""
    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(200, json=_info_body()))

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("dataset", "")
        if name == "TransferMoney":
            return httpx.Response(500, text="No stat named X was found")
        return httpx.Response(200, json=_series_points([1, 2, 3]))

    respx.get(_DATASET_URL).mock(side_effect=handler)

    raw = await fetch_economy()
    assert raw["series"]["TransferMoney"] == []  # swallowed
    # A healthy series is still populated.
    assert raw["series"]["PayWages"] == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]


@pytest.mark.asyncio
@respx.mock
async def test_fetch_economy_caches_within_ttl() -> None:
    info_route = respx.get(DEFAULT_ECO_INFO_URL).mock(
        return_value=httpx.Response(200, json=_info_body())
    )
    _mock_all_datasets({name: [] for name in ECONOMY_DATASETS})

    await fetch_economy()
    await fetch_economy()
    await fetch_economy()
    # Economy cache short-circuits the second + third calls entirely.
    assert info_route.call_count == 1


# ---------------------------------------------------------------------------
# compute_economy_payload: KPI math + classification
# ---------------------------------------------------------------------------


def _raw(
    series: dict[str, list[int]] | dict[str, list[float]] | None = None,
    days: int = 3,
    econ_desc: str = "524 trades, 0 contracts",
    admin_ok: bool = True,
) -> dict[str, object]:
    pts = {
        name: [(float(i), float(v)) for i, v in enumerate(series.get(name, []))] if series else []
        for name in ECONOMY_DATASETS
    }
    return {
        "info": {
            "Description": "Eco",
            "Category": "Test",
            "EconomyDesc": econ_desc,
            "TotalCulture": 100.0,
            "_sourceUrl": DEFAULT_ECO_INFO_URL,
        },
        "days_elapsed": days,
        "series": pts,
        "admin_ok": admin_ok,
    }


def test_compute_healthy_classification_on_day3_zero_contracts() -> None:
    """The real Day-3 shape: 524 trades, no contracts, no loans → healthy."""
    payload = compute_economy_payload(_raw())
    k = payload["kpis"]
    assert payload["health"] == "healthy"
    assert k["trades_total"] == 524
    assert k["trades_per_day"] == round(524 / 3, 1)
    # Zero resolved → no crash, rates are 0.
    assert k["loan_default_rate"] == 0.0
    assert k["contract_completion_ratio"] == 0.0
    # Narrative is readable, not "None% default rate".
    assert "0.0% default rate" in payload["narrative"]


def test_compute_stressed_on_high_default_rate() -> None:
    series = {
        "DefaultedOnLoanOrBond": [3, 3, 3],  # total 9
        "RepaidLoanOrBond": [1],  # total 1 → default rate 90%
    }
    payload = compute_economy_payload(_raw(series=series))
    assert payload["health"] == "stressed"
    assert payload["kpis"]["loan_default_rate"] == 90.0


def test_compute_stressed_on_high_contract_failure() -> None:
    series = {
        "CompletedContract": [1],
        "FailedContract": [5, 5],  # failure rate > 90%
    }
    payload = compute_economy_payload(_raw(series=series))
    assert payload["health"] == "stressed"
    assert payload["kpis"]["contract_failure_rate"] > 30.0


def test_compute_healthy_when_both_zero() -> None:
    """Day-3 reality: zero contracts, zero loans must not trigger `stressed`."""
    payload = compute_economy_payload(_raw())
    assert payload["health"] == "healthy"


def test_compute_net_tax_flow_signed() -> None:
    series = {
        "PayTax": [100, 50],  # total 150 in
        "ReceiveGovernmentFunds": [30],  # total 30 out
    }
    payload = compute_economy_payload(_raw(series=series))
    assert payload["kpis"]["net_tax_flow"] == 120.0


def test_compute_sparks_pick_most_volatile() -> None:
    """Sparklines prefer high-stddev series (normalized by mean)."""
    series = {
        "PayWages": [10, 10, 10, 10],  # flat → stddev 0
        "TransferMoney": [1, 100, 5, 800],  # spiky
        "PayTax": [5, 5, 5, 5],  # flat
        "PropertyTransfer": [1, 2, 1, 3],  # mildly spiky
    }
    payload = compute_economy_payload(_raw(series=series))
    names = [s["name"] for s in payload["sparks"]]
    # The flat series should NOT be first; the spiky one should.
    assert names[0] == "TransferMoney"
    # SVG is rendered (not empty placeholder path).
    assert "<path" in payload["sparks"][0]["svg"]


def test_compute_sparks_skips_empty_series() -> None:
    """Series with <2 points are excluded (would only render a flat baseline)."""
    payload = compute_economy_payload(_raw())
    assert payload["sparks"] == []


def test_compute_handles_missing_economy_desc() -> None:
    """No EconomyDesc on /info → trades_total is 0, not a crash."""
    payload = compute_economy_payload(_raw(econ_desc=""))
    assert payload["kpis"]["trades_total"] == 0
    assert payload["kpis"]["trades_per_day"] == 0.0


# ---------------------------------------------------------------------------
# MCP tool wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_includes_get_eco_economy() -> None:
    mcp = build_server()
    handler = mcp.request_handlers[mt.ListToolsRequest]
    result = await handler(mt.ListToolsRequest(method="tools/list"))
    names = {tool.name for tool in result.root.tools}
    assert "get_eco_economy" in names


@pytest.mark.asyncio
@respx.mock
async def test_call_get_eco_economy_returns_htmx_fragment() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(return_value=httpx.Response(200, json=_info_body()))
    _mock_all_datasets({name: [1.0, 2.0, 3.0] for name in ECONOMY_DATASETS})

    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_economy", arguments={}),
    )
    result = await handler(req)
    blocks = result.root.content
    assert len(blocks) == 3
    # Block 0: markdown fallback. Block 1: JSON. Block 2: HTMX HTML fragment.
    assert isinstance(blocks[0], mt.TextContent)
    assert isinstance(blocks[1], mt.TextContent)
    assert isinstance(blocks[2], mt.TextContent)

    # Markdown mentions the narrative health.
    assert "economic health" in blocks[0].text.lower()

    # JSON is parseable and carries the computed KPIs.
    payload = json.loads(blocks[1].text)
    assert payload["health"] in {"healthy", "booming", "stressed"}
    assert payload["kpis"]["trades_total"] == 524

    # HTMX fragment starts with the prefix and contains the card markup.
    frag = blocks[2].text
    assert frag.startswith("HTMX:")
    assert "Economic health" in frag


@pytest.mark.asyncio
@respx.mock
async def test_call_get_eco_economy_handles_info_failure() -> None:
    respx.get(DEFAULT_ECO_INFO_URL).mock(side_effect=httpx.ConnectError("refused"))

    mcp = build_server()
    handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="get_eco_economy", arguments={}),
    )
    result = await handler(req)
    # Error path: isError=True, still emits an HTMX error partial.
    assert result.root.isError is True
    blocks = result.root.content
    assert any(isinstance(b, mt.TextContent) and b.text.startswith("HTMX:") for b in blocks)
