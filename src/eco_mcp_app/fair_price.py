"""Fair-price advisor: maps Eco items to real-world FRED commodity series.

This module is intentionally transport- and framework-agnostic. The MCP tool
layer in `server.py` imports `fair_price_card` / `fetch_fair_price` and formats
the result; everything else — SSM lookup, HTTP fetch, SQLite cache, math — is
self-contained here so it can be unit-tested with `respx` without standing up
an MCP server.

Design notes:

- **SSM is optional at runtime.** In local dev / CI we accept `FRED_API_KEY`
  from the environment so tests don't need AWS creds. In the deployed
  homelab k3s pod, `boto3` pulls `/eco-mcp-app/fred-api-key` (region pinned
  to `us-east-1` — the eco-mcp-app namespace lives there and AWS CLI
  defaults to `us-west-2`, which will `ParameterNotFound` silently).
- **FRED cadence matters.** Copper, wheat, iron, board are monthly; WTI oil
  is daily. Computing "7-day % change" against a monthly series either
  produces 0 (same observation repeating) or garbage (when the observation
  flips). Branching on `frequency_short` gives honest labels.
- **SQLite cache at `~/.cache/eco-mcp-app/fred.sqlite`** with a 6-hour TTL
  for observations and a 7-day TTL for the `/series` metadata (cadence
  rarely changes). Keyed by series id.
- **Day 3 of Cycle 13 reality.** The eco server has thin economy data; the
  card surfaces the real-world trend without pretending to know in-game
  medians. Calibration is best-effort and omitted when the calibration
  file doesn't have the cycle yet.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred"

# Internal mapping: Eco item -> FRED series + a human-readable label.
# The `display_unit` captures what FRED's observations are denominated in,
# so the narrative can say "$4.12/lb" for copper vs "$/bbl" for oil. Pulled
# from FRED's series metadata pages (stable reference data, not user-facing).
ITEM_MAP: dict[str, dict[str, str]] = {
    "Copper": {
        "series_id": "PCOPPUSDM",
        "eco_item": "CopperIngot",
        "display_name": "copper",
        "display_unit": "USD / metric ton",
    },
    "Wheat": {
        "series_id": "PWHEAMTUSDM",
        "eco_item": "Wheat",
        "display_name": "wheat",
        "display_unit": "USD / metric ton",
    },
    "Board": {
        "series_id": "WPU0811",
        "eco_item": "Board",
        "display_name": "lumber (PPI)",
        "display_unit": "PPI index",
    },
    "Iron": {
        "series_id": "PIORECRUSDM",
        "eco_item": "IronIngot",
        "display_name": "iron ore",
        "display_unit": "USD / metric ton",
    },
    "Oil": {
        "series_id": "DCOILWTICO",
        "eco_item": "Oil",
        "display_name": "WTI crude oil",
        "display_unit": "USD / bbl",
    },
}

# Case-insensitive alias table so `fair_price({"item": "copper"})` and
# `fair_price({"item": "CopperIngot"})` both resolve. Matches on the map key,
# the in-game item name, and a few common lowercase shorthands.
_ITEM_ALIASES: dict[str, str] = {}
for key, meta in ITEM_MAP.items():
    _ITEM_ALIASES[key.lower()] = key
    _ITEM_ALIASES[meta["eco_item"].lower()] = key
_ITEM_ALIASES["copperingot"] = "Copper"
_ITEM_ALIASES["ironingot"] = "Iron"
_ITEM_ALIASES["lumber"] = "Board"
_ITEM_ALIASES["crude"] = "Oil"


def resolve_item(item: str | None) -> str | None:
    if not item:
        return None
    return _ITEM_ALIASES.get(item.strip().lower())


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------


_SSM_PARAM = "/eco-mcp-app/fred-api-key"
_SSM_REGION = "us-east-1"

# Process-level cache. SSM calls aren't free and the key doesn't rotate
# within a process lifetime.
_fred_api_key: str | None = None


def get_fred_api_key() -> str | None:
    """Return the FRED API key or None if unavailable.

    Resolution order:
      1. `FRED_API_KEY` env var (dev / CI / Docker env injection)
      2. SSM `/eco-mcp-app/fred-api-key` via boto3 (if installed)

    None is a valid return — the tool gracefully degrades to an "API key not
    configured" empty state rather than crashing the MCP server.
    """
    global _fred_api_key
    if _fred_api_key is not None:
        return _fred_api_key
    env = os.environ.get("FRED_API_KEY")
    if env:
        _fred_api_key = env.strip()
        return _fred_api_key
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        client = boto3.client("ssm", region_name=_SSM_REGION)
        resp = client.get_parameter(Name=_SSM_PARAM, WithDecryption=True)
        _fred_api_key = resp["Parameter"]["Value"].strip()
        return _fred_api_key
    except Exception as e:  # defensive: SSM should never crash the tool
        logger.warning("SSM fred-api-key lookup failed: %s", e)
        return None


def _reset_api_key_cache() -> None:
    """Test hook — clears the process-level FRED key cache."""
    global _fred_api_key
    _fred_api_key = None


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------


def default_cache_dir() -> Path:
    return Path(os.environ.get("ECO_MCP_CACHE_DIR", str(Path.home() / ".cache" / "eco-mcp-app")))


def _cache_db_path() -> Path:
    d = default_cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "fred.sqlite"


# TTLs (seconds). Observations refresh 6h — FRED updates daily series overnight,
# monthly series once a month, so 6h is comfortably under the fastest cadence.
# Metadata is effectively immutable over the week.
OBSERVATIONS_TTL_S = 6 * 60 * 60
METADATA_TTL_S = 7 * 24 * 60 * 60


def _open_cache() -> sqlite3.Connection:
    conn = sqlite3.connect(_cache_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fred_cache (
            kind TEXT NOT NULL,
            series_id TEXT NOT NULL,
            fetched_at REAL NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (kind, series_id)
        )
        """
    )
    return conn


def _cache_get(kind: str, series_id: str, ttl_s: float) -> Any | None:
    with _open_cache() as conn:
        row = conn.execute(
            "SELECT fetched_at, payload FROM fred_cache WHERE kind = ? AND series_id = ?",
            (kind, series_id),
        ).fetchone()
    if row is None:
        return None
    fetched_at, payload = row
    if (time.time() - fetched_at) > ttl_s:
        return None
    return json.loads(payload)


def _cache_put(kind: str, series_id: str, payload: Any) -> None:
    with _open_cache() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fred_cache (kind, series_id, fetched_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (kind, series_id, time.time(), json.dumps(payload)),
        )


# ---------------------------------------------------------------------------
# FRED HTTP
# ---------------------------------------------------------------------------


async def _fetch_json(client: httpx.AsyncClient, path: str, params: dict[str, str]) -> dict:
    url = f"{FRED_BASE_URL}{path}"
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.json()


async def fetch_series_metadata(series_id: str, api_key: str) -> dict[str, Any]:
    """Return the FRED series record, cached for 7 days.

    We only actually consume `frequency_short` and `units_short`, but the full
    payload gets cached in case downstream rendering wants more later.
    """
    cached = _cache_get("meta", series_id, METADATA_TTL_S)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=10.0) as client:
        data = await _fetch_json(
            client,
            "/series",
            {"series_id": series_id, "api_key": api_key, "file_type": "json"},
        )
    series = (data.get("seriess") or [{}])[0]
    _cache_put("meta", series_id, series)
    return series


async def fetch_observations(
    series_id: str,
    api_key: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return observations newest-first truncated to `limit`.

    `limit` covers ~7 months of daily data (plenty for 90-day lookback) or
    ~16 years of monthly data. 6-hour cache.
    """
    cached = _cache_get("obs", series_id, OBSERVATIONS_TTL_S)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=10.0) as client:
        data = await _fetch_json(
            client,
            "/series/observations",
            {
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": str(limit),
            },
        )
    obs = data.get("observations") or []
    _cache_put("obs", series_id, obs)
    return obs


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


@dataclass
class FairPriceResult:
    """Structured result returned by `fetch_fair_price`.

    `error` is set when the tool can't render a useful narrative (no API key,
    no observations, unknown item). UI code should check `error` first and
    render an empty state.
    """

    item: str | None
    series_id: str | None
    display_name: str | None
    display_unit: str | None
    frequency: str | None
    latest_value: float | None
    latest_date: str | None
    changes: dict[str, float | None]
    changes_label: str
    narrative: str
    cached: bool
    error: str | None = None


def _parse_obs_value(raw: str) -> float | None:
    # FRED encodes missing values as "." — the JSON has them as strings even
    # when the value column is numeric, so we parse defensively.
    if raw is None or raw == "." or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _clean_observations(obs: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """Return (date, value) pairs oldest-first, dropping missing values."""
    cleaned: list[tuple[str, float]] = []
    # FRED returned newest-first; flip so "latest" math reads naturally.
    for o in reversed(obs):
        v = _parse_obs_value(o.get("value", ""))
        if v is None:
            continue
        cleaned.append((o.get("date", ""), v))
    return cleaned


def _pct(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return (new - old) / old * 100.0


def _pct_at_offset(obs: list[tuple[str, float]], offset: int) -> float | None:
    """Percent change between the newest obs and the one `offset` samples back."""
    if len(obs) <= offset:
        return None
    new = obs[-1][1]
    old = obs[-1 - offset][1]
    return _pct(new, old)


def _pct_by_days(obs: list[tuple[str, float]], days: int) -> float | None:
    """Percent change vs the observation closest to `days` ago.

    Daily FRED series have gaps (weekends, holidays), so an exact day offset
    into the list isn't the same as "N days ago". We walk backwards from the
    newest date until the gap >= `days`, then compare.
    """
    if len(obs) < 2:
        return None
    latest_date_s, latest_val = obs[-1]
    try:
        latest_date = datetime.fromisoformat(latest_date_s)
    except ValueError:
        return None
    for date_s, val in reversed(obs[:-1]):
        try:
            d = datetime.fromisoformat(date_s)
        except ValueError:
            continue
        if (latest_date - d).days >= days:
            return _pct(latest_val, val)
    return None


def latest_pct_changes(
    obs: list[tuple[str, float]], frequency: str
) -> tuple[dict[str, float | None], str]:
    """Compute cadence-appropriate % changes.

    Returns `(changes, label)` where `label` is human-readable shorthand for
    the cadence ("daily" / "monthly" / "weekly" / other). The caller uses the
    label in the narrative so we never mis-label a monthly figure as "7-day".
    """
    freq = (frequency or "").upper()
    if freq == "D":
        return (
            {
                "7d": _pct_by_days(obs, 7),
                "30d": _pct_by_days(obs, 30),
                "90d": _pct_by_days(obs, 90),
            },
            "daily",
        )
    if freq == "M":
        return (
            {
                "1m": _pct_at_offset(obs, 1),
                "3m": _pct_at_offset(obs, 3),
                "12m": _pct_at_offset(obs, 12),
            },
            "monthly",
        )
    if freq == "W":
        return (
            {
                "1w": _pct_at_offset(obs, 1),
                "4w": _pct_at_offset(obs, 4),
                "52w": _pct_at_offset(obs, 52),
            },
            "weekly",
        )
    # Unknown cadence: fall back to simple offset math with generic keys so
    # the caller at least sees *something*.
    return (
        {
            "prev": _pct_at_offset(obs, 1),
            "3-back": _pct_at_offset(obs, 3),
            "12-back": _pct_at_offset(obs, 12),
        },
        freq.lower() or "unknown",
    )


# ---------------------------------------------------------------------------
# Calibration (cycle-specific in-game -> real ratio)
# ---------------------------------------------------------------------------


def _calibration_path() -> Path:
    return default_cache_dir() / "price-calib.json"


def load_calibration(cycle_id: str) -> dict[str, float] | None:
    """Load the per-cycle calibration dict or None if the cycle isn't known."""
    p = _calibration_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    entry = data.get(cycle_id)
    return entry if isinstance(entry, dict) else None


def save_calibration(cycle_id: str, ratios: dict[str, float]) -> None:
    p = _calibration_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            data = {}
    existing = data.get(cycle_id) or {}
    existing.update(ratios)
    data[cycle_id] = existing
    p.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Narrative assembly
# ---------------------------------------------------------------------------


def _format_pct(p: float | None) -> str:
    if p is None:
        return "n/a"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"


def _format_value(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 100:
        return f"{v:,.2f}"
    return f"{v:.2f}"


def _build_narrative(
    *,
    display_name: str,
    display_unit: str,
    latest_value: float | None,
    latest_date: str | None,
    changes: dict[str, float | None],
    changes_label: str,
    eco_item: str,
    calibrated_price: float | None,
) -> str:
    if latest_value is None:
        return (
            f"Real {display_name}: no recent observations from FRED. "
            f"Fair-price guidance for {eco_item} unavailable — try again later."
        )
    # Pick the two most meaningful deltas per cadence for the headline.
    if changes_label == "daily":
        short, long = changes.get("7d"), changes.get("30d")
        short_label, long_label = "7d", "30d"
    elif changes_label == "monthly":
        short, long = changes.get("1m"), changes.get("12m")
        short_label, long_label = "vs prior month", "YoY"
    else:
        keys = list(changes.keys())
        short = changes.get(keys[0]) if keys else None
        long = changes.get(keys[-1]) if keys else None
        short_label = keys[0] if keys else ""
        long_label = keys[-1] if keys else ""
    date_str = latest_date or "latest"
    cadence_phrase = f"{changes_label} series, latest {date_str}"
    headline = (
        f"Real {display_name}: {_format_value(latest_value)} {display_unit} ({cadence_phrase})."
    )
    trend_parts: list[str] = []
    if short is not None:
        trend_parts.append(f"{_format_pct(short)} {short_label}")
    if long is not None and long_label != short_label:
        trend_parts.append(f"{_format_pct(long)} {long_label}")
    trend_line = "Trend: " + ", ".join(trend_parts) + "." if trend_parts else "Trend: unavailable."
    # Advisory line. Day 3 of Cycle 13 — the server has thin market data, so
    # the calibration path is usually empty and we surface that honestly.
    direction = (
        "trending up"
        if (short is not None and short > 0)
        else "trending down"
        if (short is not None and short < 0)
        else "flat"
    )
    if calibrated_price is not None:
        advisory = (
            f"In-cycle fair price for {eco_item} {direction} — "
            f"~{calibrated_price:.2f} currency per unit (calibrated)."
        )
    else:
        advisory = (
            f"In-cycle fair price for {eco_item} {direction} — "
            "calibration not yet recorded for this cycle. "
            "Advisory only; no in-game enforcement."
        )
    return f"{headline} {trend_line} {advisory}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def fetch_fair_price(item: str | None, *, cycle_id: str | None = None) -> FairPriceResult:
    """Top-level: resolve item, fetch metadata + observations, build narrative.

    Robustness: every failure mode (unknown item, no API key, FRED 4xx/5xx,
    no observations) produces a `FairPriceResult` with `error` set and a
    narrative suitable for the empty-state UI — we never raise through to
    the MCP layer because Day 3 of Cycle 13 means thin data is normal.
    """
    resolved = resolve_item(item)
    if resolved is None:
        known = ", ".join(sorted(ITEM_MAP.keys()))
        return FairPriceResult(
            item=item,
            series_id=None,
            display_name=None,
            display_unit=None,
            frequency=None,
            latest_value=None,
            latest_date=None,
            changes={},
            changes_label="",
            narrative=f"Unknown item '{item}'. Try one of: {known}.",
            cached=False,
            error="unknown_item",
        )
    meta = ITEM_MAP[resolved]
    series_id = meta["series_id"]
    api_key = get_fred_api_key()
    if not api_key:
        return FairPriceResult(
            item=resolved,
            series_id=series_id,
            display_name=meta["display_name"],
            display_unit=meta["display_unit"],
            frequency=None,
            latest_value=None,
            latest_date=None,
            changes={},
            changes_label="",
            narrative=(
                f"Real-world pricing for {meta['display_name']} unavailable — "
                "FRED API key not configured (set FRED_API_KEY or provision "
                f"SSM {_SSM_PARAM} in {_SSM_REGION})."
            ),
            cached=False,
            error="no_api_key",
        )
    # Track whether we hit cache for both calls — useful for "second call
    # hits the cache" acceptance and for debugging rate-limit suspicions.
    obs_cached_before = _cache_get("obs", series_id, OBSERVATIONS_TTL_S) is not None
    meta_cached_before = _cache_get("meta", series_id, METADATA_TTL_S) is not None
    try:
        series_meta = await fetch_series_metadata(series_id, api_key)
        raw_obs = await fetch_observations(series_id, api_key)
    except httpx.HTTPError as e:
        return FairPriceResult(
            item=resolved,
            series_id=series_id,
            display_name=meta["display_name"],
            display_unit=meta["display_unit"],
            frequency=None,
            latest_value=None,
            latest_date=None,
            changes={},
            changes_label="",
            narrative=f"FRED request failed: {e}. Fair price for {resolved} unavailable.",
            cached=False,
            error="fred_http_error",
        )
    frequency = series_meta.get("frequency_short") or ""
    cleaned = _clean_observations(raw_obs)
    if not cleaned:
        return FairPriceResult(
            item=resolved,
            series_id=series_id,
            display_name=meta["display_name"],
            display_unit=meta["display_unit"],
            frequency=frequency,
            latest_value=None,
            latest_date=None,
            changes={},
            changes_label=frequency.lower(),
            narrative=(
                f"No FRED observations for {meta['display_name']} — "
                f"series {series_id} returned empty. Advisory unavailable."
            ),
            cached=obs_cached_before and meta_cached_before,
            error="no_observations",
        )
    latest_date, latest_value = cleaned[-1]
    changes, changes_label = latest_pct_changes(cleaned, frequency)
    calibrated_price: float | None = None
    if cycle_id:
        calib = load_calibration(cycle_id)
        if calib and resolved in calib:
            # Calibration stored as (in-game price / real price) at first call.
            calibrated_price = calib[resolved] * latest_value
    narrative = _build_narrative(
        display_name=meta["display_name"],
        display_unit=meta["display_unit"],
        latest_value=latest_value,
        latest_date=latest_date,
        changes=changes,
        changes_label=changes_label,
        eco_item=meta["eco_item"],
        calibrated_price=calibrated_price,
    )
    return FairPriceResult(
        item=resolved,
        series_id=series_id,
        display_name=meta["display_name"],
        display_unit=meta["display_unit"],
        frequency=frequency,
        latest_value=latest_value,
        latest_date=latest_date,
        changes=changes,
        changes_label=changes_label,
        narrative=narrative,
        cached=obs_cached_before and meta_cached_before,
    )


def to_payload(result: FairPriceResult) -> dict[str, Any]:
    """JSON-shaped result for the tool's second content block."""
    return {
        "view": "fair_price",
        "fetchedAtISO": datetime.now(UTC).isoformat(),
        "item": result.item,
        "seriesId": result.series_id,
        "displayName": result.display_name,
        "displayUnit": result.display_unit,
        "frequency": result.frequency,
        "latestValue": result.latest_value,
        "latestDate": result.latest_date,
        "changes": result.changes,
        "changesLabel": result.changes_label,
        "narrative": result.narrative,
        "cached": result.cached,
        "error": result.error,
    }
