"""Tests for the fair-price advisor.

Covers: alias resolution, FRED HTTP wiring (respx), cadence-branching math,
empty-state handling (no api key, unknown item, empty observations), SQLite
caching, and the full `fetch_fair_price` narrative for daily + monthly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from eco_mcp_app import fair_price as fp


@pytest.fixture(autouse=True)
def _isolate_cache_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route the sqlite cache + calibration JSON into a tmp dir per test."""
    monkeypatch.setenv("ECO_MCP_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    fp._reset_api_key_cache()
    yield
    fp._reset_api_key_cache()


# ---------------------------------------------------------------------------
# Alias / resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Copper", "Copper"),
        ("copper", "Copper"),
        ("CopperIngot", "Copper"),
        ("copperingot", "Copper"),
        ("IronIngot", "Iron"),
        ("lumber", "Board"),
        ("Board", "Board"),
        ("oil", "Oil"),
        ("crude", "Oil"),
        ("Wheat", "Wheat"),
    ],
)
def test_resolve_item_aliases(raw: str, expected: str) -> None:
    assert fp.resolve_item(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "Gold", "nonsense"])
def test_resolve_item_unknown(raw: str | None) -> None:
    assert fp.resolve_item(raw) is None


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key-abc")
    fp._reset_api_key_cache()
    assert fp.get_fred_api_key() == "test-key-abc"


def test_api_key_missing_returns_none() -> None:
    # Env is cleared by fixture; boto3 may or may not be installed but
    # get_parameter will fail without creds. Either way we should get None.
    assert fp.get_fred_api_key() is None


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def test_pct_at_offset_monthly() -> None:
    obs = [("2026-01-01", 100.0), ("2026-02-01", 110.0), ("2026-03-01", 121.0)]
    # 1m: 121 vs 110 = +10%. 2m: 121 vs 100 = +21%.
    assert fp._pct_at_offset(obs, 1) == pytest.approx(10.0)
    assert fp._pct_at_offset(obs, 2) == pytest.approx(21.0)


def test_pct_at_offset_insufficient() -> None:
    assert fp._pct_at_offset([("2026-01-01", 1.0)], 1) is None


def test_pct_by_days_walks_to_nearest_gap() -> None:
    # Gappy daily series (skip weekends). Latest = 2026-04-20.
    obs = [
        ("2026-01-20", 70.0),  # 90 days back
        ("2026-03-20", 75.0),  # ~31 days back
        ("2026-04-13", 78.0),  # 7 days back
        ("2026-04-17", 79.0),
        ("2026-04-20", 80.0),
    ]
    assert fp._pct_by_days(obs, 7) == pytest.approx((80 - 78) / 78 * 100)
    assert fp._pct_by_days(obs, 30) == pytest.approx((80 - 75) / 75 * 100)
    assert fp._pct_by_days(obs, 90) == pytest.approx((80 - 70) / 70 * 100)


def test_latest_pct_changes_daily_branch() -> None:
    obs = [("2026-01-01", 50.0), ("2026-04-20", 80.0)]
    changes, label = fp.latest_pct_changes(obs, "D")
    assert label == "daily"
    assert set(changes.keys()) == {"7d", "30d", "90d"}


def test_latest_pct_changes_monthly_branch() -> None:
    obs = [(f"2025-{m:02d}-01", 100.0 + m) for m in range(1, 13)]
    obs.append(("2026-01-01", 114.0))
    changes, label = fp.latest_pct_changes(obs, "M")
    assert label == "monthly"
    assert set(changes.keys()) == {"1m", "3m", "12m"}
    # 1m: 114 vs 112 = ~+1.79%
    assert changes["1m"] == pytest.approx((114 - 112) / 112 * 100)


def test_latest_pct_changes_does_not_compute_7d_for_monthly() -> None:
    # The whole point of the cadence branch: no "7d" key for M series.
    obs = [("2026-01-01", 100.0), ("2026-02-01", 110.0)]
    changes, label = fp.latest_pct_changes(obs, "M")
    assert "7d" not in changes
    assert label == "monthly"


def test_clean_observations_drops_missing() -> None:
    raw = [
        {"date": "2026-04-20", "value": "80.0"},  # FRED returns desc
        {"date": "2026-04-19", "value": "."},
        {"date": "2026-04-18", "value": "78.5"},
    ]
    cleaned = fp._clean_observations(raw)
    # Oldest-first, missing dropped.
    assert cleaned == [("2026-04-18", 78.5), ("2026-04-20", 80.0)]


# ---------------------------------------------------------------------------
# End-to-end via respx
# ---------------------------------------------------------------------------


def _monthly_obs_response(values: list[tuple[str, str]]) -> httpx.Response:
    # FRED returns sort_order=desc so newest first.
    return httpx.Response(
        200,
        json={"observations": [{"date": d, "value": v} for d, v in reversed(values)]},
    )


def _meta_response(freq: str = "M") -> httpx.Response:
    return httpx.Response(
        200,
        json={"seriess": [{"id": "X", "frequency_short": freq, "units_short": "USD"}]},
    )


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fair_price_unknown_item_returns_empty_state() -> None:
    result = await fp.fetch_fair_price("Gold")
    assert result.error == "unknown_item"
    assert "Gold" in result.narrative
    assert result.latest_value is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fair_price_no_api_key_returns_empty_state() -> None:
    # Fixture scrubs FRED_API_KEY.
    result = await fp.fetch_fair_price("Copper")
    assert result.error == "no_api_key"
    assert result.series_id == "PCOPPUSDM"
    assert "FRED API key" in result.narrative


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fair_price_monthly_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k")
    fp._reset_api_key_cache()
    values = [
        ("2025-04-01", "8000"),
        ("2025-05-01", "8100"),
        ("2025-06-01", "8200"),
        ("2025-07-01", "8300"),
        ("2025-08-01", "8400"),
        ("2025-09-01", "8500"),
        ("2025-10-01", "8600"),
        ("2025-11-01", "8700"),
        ("2025-12-01", "8800"),
        ("2026-01-01", "8900"),
        ("2026-02-01", "9000"),
        ("2026-03-01", "9100"),
        ("2026-04-01", "9200"),
    ]
    respx.get(f"{fp.FRED_BASE_URL}/series").mock(return_value=_meta_response("M"))
    respx.get(f"{fp.FRED_BASE_URL}/series/observations").mock(
        return_value=_monthly_obs_response(values)
    )
    result = await fp.fetch_fair_price("Copper")
    assert result.error is None
    assert result.series_id == "PCOPPUSDM"
    assert result.frequency == "M"
    assert result.changes_label == "monthly"
    assert result.latest_value == pytest.approx(9200.0)
    assert result.latest_date == "2026-04-01"
    # 1m: 9200 vs 9100. 12m: 9200 vs 8000.
    assert result.changes["1m"] == pytest.approx((9200 - 9100) / 9100 * 100)
    assert result.changes["12m"] == pytest.approx((9200 - 8000) / 8000 * 100)
    assert "monthly series" in result.narrative
    assert "YoY" in result.narrative
    assert not result.cached


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fair_price_daily_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k")
    fp._reset_api_key_cache()
    # 100 trading days ending 2026-04-20. Simple linear ramp.
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    values: list[tuple[str, str]] = []
    for i in range(110):
        d = start + timedelta(days=i)
        # skip weekends to look realistic
        if d.weekday() >= 5:
            continue
        values.append((d.isoformat(), f"{60 + i * 0.2:.2f}"))
    respx.get(f"{fp.FRED_BASE_URL}/series").mock(return_value=_meta_response("D"))
    respx.get(f"{fp.FRED_BASE_URL}/series/observations").mock(
        return_value=_monthly_obs_response(values)
    )
    result = await fp.fetch_fair_price("Oil")
    assert result.error is None
    assert result.changes_label == "daily"
    assert set(result.changes.keys()) == {"7d", "30d", "90d"}
    assert all(v is not None for v in result.changes.values())
    assert "daily series" in result.narrative


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fair_price_caches_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k")
    fp._reset_api_key_cache()
    values = [("2026-03-01", "100"), ("2026-04-01", "110")]
    meta_route = respx.get(f"{fp.FRED_BASE_URL}/series").mock(return_value=_meta_response("M"))
    obs_route = respx.get(f"{fp.FRED_BASE_URL}/series/observations").mock(
        return_value=_monthly_obs_response(values)
    )
    r1 = await fp.fetch_fair_price("Wheat")
    r2 = await fp.fetch_fair_price("Wheat")
    # Second call hits sqlite cache — no additional HTTP.
    assert meta_route.call_count == 1
    assert obs_route.call_count == 1
    assert r1.cached is False
    assert r2.cached is True


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fair_price_empty_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k")
    fp._reset_api_key_cache()
    respx.get(f"{fp.FRED_BASE_URL}/series").mock(return_value=_meta_response("M"))
    respx.get(f"{fp.FRED_BASE_URL}/series/observations").mock(
        return_value=httpx.Response(200, json={"observations": []})
    )
    result = await fp.fetch_fair_price("Iron")
    assert result.error == "no_observations"
    assert result.latest_value is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fair_price_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k")
    fp._reset_api_key_cache()
    respx.get(f"{fp.FRED_BASE_URL}/series").mock(return_value=httpx.Response(500))
    result = await fp.fetch_fair_price("Board")
    assert result.error == "fred_http_error"
    assert "FRED request failed" in result.narrative


@pytest.mark.asyncio
@respx.mock
async def test_calibration_applied_when_cycle_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "k")
    fp._reset_api_key_cache()
    values = [("2026-03-01", "100"), ("2026-04-01", "200")]
    respx.get(f"{fp.FRED_BASE_URL}/series").mock(return_value=_meta_response("M"))
    respx.get(f"{fp.FRED_BASE_URL}/series/observations").mock(
        return_value=_monthly_obs_response(values)
    )
    # Save a calibration: 0.5 currency per real-world unit.
    fp.save_calibration("cycle-13", {"Copper": 0.5})
    result = await fp.fetch_fair_price("Copper", cycle_id="cycle-13")
    # 0.5 * 200 = 100.00 in narrative.
    assert "100.00 currency per unit" in result.narrative
    assert "calibrated" in result.narrative


def test_calibration_load_missing_cycle_returns_none() -> None:
    assert fp.load_calibration("nope") is None


def test_calibration_roundtrip() -> None:
    fp.save_calibration("cycle-13", {"Copper": 0.5})
    fp.save_calibration("cycle-13", {"Wheat": 0.25})
    loaded = fp.load_calibration("cycle-13")
    assert loaded == {"Copper": 0.5, "Wheat": 0.25}


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_tool_registered_and_calls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    import mcp.types as mt

    from eco_mcp_app.server import build_server

    monkeypatch.setenv("FRED_API_KEY", "k")
    fp._reset_api_key_cache()

    values = [("2026-03-01", "100"), ("2026-04-01", "110")]
    respx.get(f"{fp.FRED_BASE_URL}/series").mock(return_value=_meta_response("M"))
    respx.get(f"{fp.FRED_BASE_URL}/series/observations").mock(
        return_value=_monthly_obs_response(values)
    )

    mcp = build_server()
    # Tool listing includes fair_price.
    list_handler = mcp.request_handlers[mt.ListToolsRequest]
    listed = await list_handler(mt.ListToolsRequest(method="tools/list"))
    names = {t.name for t in listed.root.tools}
    assert "fair_price" in names

    call_handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="fair_price", arguments={"item": "Copper"}),
    )
    result = await call_handler(req)
    blocks = result.root.content
    assert len(blocks) == 2
    assert isinstance(blocks[0], mt.TextContent)
    assert isinstance(blocks[1], mt.TextContent)
    assert "copper" in blocks[0].text.lower()
    payload = _json.loads(blocks[1].text)
    assert payload["view"] == "fair_price"
    assert payload["seriesId"] == "PCOPPUSDM"
    assert result.root.meta is not None
    fragment = result.root.meta["ui"]["fragment"]
    assert "fair-price-card" in fragment


@pytest.mark.asyncio
async def test_tool_unknown_item_flags_error() -> None:
    import mcp.types as mt

    from eco_mcp_app.server import build_server

    mcp = build_server()
    call_handler = mcp.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(
        method="tools/call",
        params=mt.CallToolRequestParams(name="fair_price", arguments={"item": "Gold"}),
    )
    result = await call_handler(req)
    assert result.root.isError is True
