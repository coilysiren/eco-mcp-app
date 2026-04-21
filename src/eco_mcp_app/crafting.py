"""Crafting Activity Atlas — aggregate Eco action-exporter CSVs into a card.

The `/api/v1/exporter/actions?actionName=ItemCraftedAction` CSV grows to
multi-megabyte sizes late-cycle (295 KB on day 3 → tens of MB by meteor).
We stream-parse via `httpx.AsyncClient.stream(...)` + `csv.reader` on a line
iterator so we never materialize the whole body in memory.

Aggregation runs in a single pass across four action types that all describe
"production" in Eco:

    - ItemCraftedAction        (bench crafts)
    - HarvestOrHunt            (plant/animal harvest; damage=1 is a kill)
    - ChopTree                 (forestry — felled=true is a full chop)
    - DigOrMine                (excavation; output item is the block)

Everything is keyed on the Day 3 sparse-state — "no events yet" is a valid
response, not an error.

Cache: a tiny SQLite under `~/.cache/eco-mcp-app/crafting.sqlite` holds the
last successful aggregation + a fetched-at timestamp, TTL 5 min. Cache is
per (base_url, api_key_hash) so swapping servers doesn't cross-contaminate.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# Action types we aggregate. ItemCraftedAction is the canonical "crafted",
# others flesh out the full production picture for the sankey / leaderboard.
# If an admin disables one via a mod, the 401/404 on that endpoint is skipped
# rather than fatal — partial data is still useful.
CRAFT_ACTION_TYPES = (
    "ItemCraftedAction",
    "HarvestOrHunt",
    "ChopTree",
    "DigOrMine",
)

DEFAULT_BASE_URL = os.environ.get("ECO_ADMIN_BASE_URL", "http://eco.coilysiren.me:3001")
DEFAULT_CACHE_TTL_S = float(os.environ.get("ECO_CRAFTING_CACHE_TTL", "300"))

# Max rows per action type before we bail out (defensive for pathological late-
# cycle CSVs). 500k is ~50 MB of CSV; well past the ~20 MB end-cycle estimate,
# and still sub-second to aggregate.
MAX_ROWS_PER_ACTION = int(os.environ.get("ECO_CRAFTING_MAX_ROWS", "500000"))


@dataclass
class CraftingAtlas:
    """Shape consumed by the Jinja2 template. JSON-serializable."""

    fetched_at_iso: str
    source_base_url: str
    total_events: int = 0
    by_item: list[tuple[str, float]] = field(default_factory=list)
    by_station: list[tuple[str, int]] = field(default_factory=list)
    by_citizen: list[tuple[str, float]] = field(default_factory=list)
    # Sankey edges: (source_station, target_item, total_count)
    flows: list[tuple[str, str, float]] = field(default_factory=list)
    # Per-action-type row count, so the UI can say "4 types fed the atlas".
    per_action_counts: dict[str, int] = field(default_factory=dict)
    # Non-fatal fetch problems — shown as a hint under the card.
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetchedAtISO": self.fetched_at_iso,
            "sourceBaseUrl": self.source_base_url,
            "totalEvents": self.total_events,
            "byItem": [[n, c] for n, c in self.by_item],
            "byStation": [[n, c] for n, c in self.by_station],
            "byCitizen": [[n, c] for n, c in self.by_citizen],
            "flows": [[s, t, c] for s, t, c in self.flows],
            "perActionCounts": dict(self.per_action_counts),
            "warnings": list(self.warnings),
        }


def _cache_dir() -> Path:
    root = Path(os.environ.get("ECO_CACHE_DIR") or (Path.home() / ".cache" / "eco-mcp-app"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_path() -> Path:
    return _cache_dir() / "crafting.sqlite"


def _cache_key(base_url: str, api_key: str | None) -> str:
    token = hashlib.sha256((api_key or "").encode()).hexdigest()[:12]
    return f"{base_url}|{token}"


def _cache_get(base_url: str, api_key: str | None, ttl_s: float) -> CraftingAtlas | None:
    try:
        conn = sqlite3.connect(_cache_path())
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS atlas "
                "(key TEXT PRIMARY KEY, stored_at REAL, payload TEXT)"
            )
            row = conn.execute(
                "SELECT stored_at, payload FROM atlas WHERE key = ?",
                (_cache_key(base_url, api_key),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    stored_at, payload = row
    if (time.time() - float(stored_at)) > ttl_s:
        return None
    data = json.loads(payload)
    return CraftingAtlas(
        fetched_at_iso=data["fetchedAtISO"],
        source_base_url=data["sourceBaseUrl"],
        total_events=int(data["totalEvents"]),
        by_item=[(n, float(c)) for n, c in data.get("byItem", [])],
        by_station=[(n, int(c)) for n, c in data.get("byStation", [])],
        by_citizen=[(n, float(c)) for n, c in data.get("byCitizen", [])],
        flows=[(s, t, float(c)) for s, t, c in data.get("flows", [])],
        per_action_counts=dict(data.get("perActionCounts", {})),
        warnings=list(data.get("warnings", [])),
    )


def _cache_put(base_url: str, api_key: str | None, atlas: CraftingAtlas) -> None:
    try:
        conn = sqlite3.connect(_cache_path())
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS atlas "
                "(key TEXT PRIMARY KEY, stored_at REAL, payload TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO atlas (key, stored_at, payload) VALUES (?, ?, ?)",
                (
                    _cache_key(base_url, api_key),
                    time.time(),
                    json.dumps(atlas.to_dict()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        # A disk-full or permission error on the cache shouldn't fail the tool.
        return


def _normalize_admin_base(base: str | None) -> str:
    """`host`, `host:port`, `http://host`, full URL → `scheme://host:port`."""
    s = (base or DEFAULT_BASE_URL).strip()
    if "://" not in s:
        s = f"http://{s}"
    parsed = urlparse(s)
    host = parsed.hostname or ""
    port = parsed.port or 3001
    return f"{parsed.scheme or 'http'}://{host}:{port}"


async def _stream_csv_rows(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> AsyncIterator[list[str]]:
    """Stream a CSV endpoint line-by-line without buffering the whole body.

    Yields row lists (including the header). Raises httpx.HTTPStatusError on
    non-2xx.
    """
    async with client.stream("GET", url, headers=headers) as resp:
        resp.raise_for_status()

        async def _line_iter() -> AsyncIterator[str]:
            async for line in resp.aiter_lines():
                yield line

        # csv.reader wants a sync iterable; pull lines into a bounded buffer
        # one at a time. This stays streaming — we only hold `MAX_ROWS` cap.
        pending: list[str] = []
        async for line in _line_iter():
            pending.append(line)
            # Flush every 4 KB worth of rows to the csv parser so we don't
            # build up more than a few-hundred-line buffer.
            if len(pending) >= 256:
                for row in csv.reader(pending):
                    yield row
                pending.clear()
        if pending:
            for row in csv.reader(pending):
                yield row


def aggregate_rows(
    action_name: str,
    rows: Iterable[list[str]],
    atlas: CraftingAtlas,
    max_rows: int = MAX_ROWS_PER_ACTION,
) -> int:
    """Fold one action's CSV rows into the running atlas.

    Returns the number of data rows consumed (excluding header).

    Action shapes (columns we care about, see cycle-13 live capture):
      ItemCraftedAction: WorldObjectItem, Citizen, ItemUsed, Count
      HarvestOrHunt:     Species, Citizen, Count   (Species is the item)
      ChopTree:          Species, Citizen, Count   (Species is the tree)
      DigOrMine:         BlockItemOnDestroy, Citizen, Count (or ItemUsed)

    The exporter occasionally gives us slightly different column orders per
    action type, so we key off the header row instead of fixed positions.
    """
    it = iter(rows)
    try:
        header = next(it)
    except StopIteration:
        return 0

    col = {name: i for i, name in enumerate(header)}

    def pick(row: list[str], *candidates: str) -> str | None:
        for c in candidates:
            if c in col and col[c] < len(row):
                v = row[col[c]].strip()
                if v:
                    return v
        return None

    # Accumulators live on the atlas's per-pass dicts; we store them on the
    # call-side via closure-like bags so each call can fold into shared totals.
    by_item: dict[str, float] = dict(atlas.by_item)
    by_station: dict[str, int] = dict(atlas.by_station)
    by_citizen: dict[str, float] = dict(atlas.by_citizen)
    flows: dict[tuple[str, str], float] = {(s, t): c for s, t, c in atlas.flows}

    consumed = 0
    for row in it:
        if not row:
            continue
        if consumed >= max_rows:
            atlas.warnings.append(
                f"{action_name}: truncated at {max_rows} rows (late-cycle size cap)"
            )
            break
        try:
            count = float(pick(row, "Count") or "0")
        except ValueError:
            count = 0.0
        citizen = pick(row, "Citizen") or ""
        # Item: for crafts the output is ItemUsed; for harvests/chops the
        # Species IS the produced stack; for mining the block destroyed.
        item = (
            pick(
                row,
                "ItemUsed",
                "Species",
                "BlockItemOnDestroy",
                "BlockDestroyed",
            )
            or ""
        )
        # Station: only crafts have a distinct WorldObjectItem; harvests are
        # hand/tool-driven, record the tool when we have it.
        station = pick(row, "WorldObjectItem", "ToolUsed") or "(hand)"

        if item:
            by_item[item] = by_item.get(item, 0.0) + count
        if station:
            by_station[station] = by_station.get(station, 0) + 1
        if citizen:
            by_citizen[citizen] = by_citizen.get(citizen, 0.0) + count
        if station and item:
            flows[(station, item)] = flows.get((station, item), 0.0) + count
        consumed += 1

    atlas.total_events += consumed
    atlas.per_action_counts[action_name] = atlas.per_action_counts.get(action_name, 0) + consumed
    atlas.by_item = sorted(by_item.items(), key=lambda kv: kv[1], reverse=True)
    atlas.by_station = sorted(by_station.items(), key=lambda kv: kv[1], reverse=True)
    atlas.by_citizen = sorted(by_citizen.items(), key=lambda kv: kv[1], reverse=True)
    atlas.flows = sorted(
        ((s, t, c) for (s, t), c in flows.items()),
        key=lambda edge: edge[2],
        reverse=True,
    )
    return consumed


async def fetch_atlas(
    base_url: str | None = None,
    api_key: str | None = None,
    cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
    client: httpx.AsyncClient | None = None,
) -> CraftingAtlas:
    """Stream all configured action CSVs and fold them into a single atlas.

    `client` is injectable so tests can hand in a pre-stubbed httpx client
    (respx plays nice with `httpx.AsyncClient()`). When omitted we construct
    one with a 30 s timeout — individual CSV fetches can legitimately take
    a few seconds late-cycle.
    """
    normalized = _normalize_admin_base(base_url)
    cached = _cache_get(normalized, api_key, cache_ttl_s)
    if cached is not None:
        return cached

    atlas = CraftingAtlas(
        fetched_at_iso=_now_iso(),
        source_base_url=normalized,
    )
    headers = {"X-API-Key": api_key} if api_key else {}

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    try:
        for action in CRAFT_ACTION_TYPES:
            url = f"{normalized}/api/v1/exporter/actions?actionName={action}"
            try:
                # Batch-fold every N rows so we never hold more than that many
                # in Python-land at once; pass a shrinking `remaining` budget
                # so the per-action cap is respected across batches.
                remaining = MAX_ROWS_PER_ACTION
                header: list[str] | None = None
                batch: list[list[str]] = []
                async for row in _stream_csv_rows(http, url, headers):
                    if header is None:
                        header = row
                        batch = [row]
                        continue
                    batch.append(row)
                    if len(batch) >= 1024:
                        consumed = aggregate_rows(action, batch, atlas, max_rows=remaining)
                        remaining -= consumed
                        if remaining <= 0:
                            break
                        batch = [header]
                if header is not None and len(batch) > 1 and remaining > 0:
                    aggregate_rows(action, batch, atlas, max_rows=remaining)
                # Record a zero-count entry on success-but-empty so callers
                # can tell "we fetched it and it was empty" apart from "never
                # fetched / errored" (the latter shows up in `warnings`).
                atlas.per_action_counts.setdefault(action, 0)
            except httpx.HTTPStatusError as e:
                atlas.warnings.append(f"{action}: HTTP {e.response.status_code}")
            except httpx.HTTPError as e:
                atlas.warnings.append(f"{action}: {type(e).__name__}: {e}")
    finally:
        if owns_client:
            await http.aclose()

    _cache_put(normalized, api_key, atlas)
    return atlas


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def prettify_eco_name(name: str) -> str:
    """`CampfireItem` → `Campfire`; `BunWulfRawMeatItem` → `Bun Wulf Raw Meat`.

    Heuristic only — Eco item IDs are PascalCase `FooItem` by convention.
    Vanilla + mod items both follow it; we do not rely on a lookup table so
    this survives unknown mods.
    """
    if not name:
        return ""
    stem = name
    for suffix in ("Item", "Species", "Block"):
        if stem.endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    # Split camel case — insert a space between a lowercase letter or digit
    # and an uppercase letter.
    out = []
    for i, ch in enumerate(stem):
        if i > 0 and ch.isupper() and (stem[i - 1].islower() or stem[i - 1].isdigit()):
            out.append(" ")
        out.append(ch)
    return "".join(out)


def atlas_template_context(
    atlas: CraftingAtlas,
    top_items: int = 20,
    top_stations: int = 15,
    top_citizens: int = 10,
    top_flows: int = 30,
) -> dict[str, Any]:
    """Shape for the Jinja2 partial. All SVG is rendered server-side."""
    items = atlas.by_item[:top_items]
    stations = atlas.by_station[:top_stations]
    citizens = atlas.by_citizen[:top_citizens]
    flows = atlas.flows[:top_flows]

    max_item = max((c for _, c in items), default=0.0) or 1.0
    max_station = max((c for _, c in stations), default=0) or 1
    max_citizen = max((c for _, c in citizens), default=0.0) or 1.0

    sankey = _build_sankey_layout(flows, width=720, height=420) if flows else None

    return {
        "empty": atlas.total_events == 0,
        "fetched_at_iso": atlas.fetched_at_iso,
        "source_base_url": atlas.source_base_url,
        "total_events": atlas.total_events,
        "per_action_counts": [
            (name, count) for name, count in atlas.per_action_counts.items() if count
        ],
        "top_items": [
            {
                "name": name,
                "pretty": prettify_eco_name(name),
                "count": count,
                "pct": (count / max_item) * 100.0,
            }
            for name, count in items
        ],
        "top_stations": [
            {
                "name": name,
                "pretty": prettify_eco_name(name),
                "count": count,
                "pct": (count / max_station) * 100.0,
            }
            for name, count in stations
        ],
        "top_citizens": [
            {
                "name": name,
                "count": count,
                "pct": (count / max_citizen) * 100.0,
            }
            for name, count in citizens
        ],
        "sankey": sankey,
        "warnings": list(atlas.warnings),
    }


def _build_sankey_layout(
    flows: list[tuple[str, str, float]],
    width: int,
    height: int,
) -> dict[str, Any]:
    """Minimal two-column sankey layout, rendered inline as SVG.

    d3-sankey would be ~20 KB of JS for a visualization we compute once and
    never re-layout. A static SVG satisfies CSP trivially and is easier to
    visually inspect in tests. Edges are drawn in decreasing thickness so the
    "top 5 crossings" acceptance check is easy to satisfy — the layout sorts
    both axes by total flow, which minimizes crossings for this shape.
    """
    if not flows:
        return {}

    # Sum per-source and per-target weights; rank from heaviest down so the
    # thickest edges sit at the top of each column.
    src_weight: dict[str, float] = defaultdict(float)
    tgt_weight: dict[str, float] = defaultdict(float)
    for s, t, c in flows:
        src_weight[s] += c
        tgt_weight[t] += c

    sources = sorted(src_weight.items(), key=lambda kv: kv[1], reverse=True)
    targets = sorted(tgt_weight.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(src_weight.values()) or 1.0

    pad = 6
    node_w = 10
    left_x = 0
    right_x = width - node_w

    # Vertical layout: pad-separated bars whose height is proportional to weight.
    def _lay(col: list[tuple[str, float]]) -> dict[str, tuple[float, float]]:
        avail = height - pad * (len(col) + 1)
        pos: dict[str, tuple[float, float]] = {}
        y: float = float(pad)
        for name, w in col:
            h = max(6.0, (w / total) * avail * (len(col)))
            pos[name] = (y, h)
            y += h + pad
        return pos

    left = _lay(sources)
    right = _lay(targets)

    # Edge paths — cubic bezier from right-edge of source bar to left-edge of
    # target bar. Stroke width proportional to edge count.
    max_edge = max((c for _, _, c in flows), default=1.0) or 1.0
    edges = []
    # Running consumption of each node's bar so parallel edges stack.
    left_off: dict[str, float] = defaultdict(float)
    right_off: dict[str, float] = defaultdict(float)
    for s, t, c in flows:
        if s not in left or t not in right:
            continue
        ly, lh = left[s]
        ry, rh = right[t]
        share = c / (src_weight[s] or 1.0)
        sh = lh * share
        share_r = c / (tgt_weight[t] or 1.0)
        rh2 = rh * share_r
        y1 = ly + left_off[s] + sh / 2
        y2 = ry + right_off[t] + rh2 / 2
        left_off[s] += sh
        right_off[t] += rh2
        x1 = left_x + node_w
        x2 = right_x
        cx = (x1 + x2) / 2
        path = f"M{x1:.1f},{y1:.1f} C{cx:.1f},{y1:.1f} {cx:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
        edges.append(
            {
                "path": path,
                "width": max(1.0, (c / max_edge) * 14.0),
                "count": c,
                "source": s,
                "target": t,
            }
        )

    return {
        "width": width,
        "height": height,
        "node_w": node_w,
        "left_x": left_x,
        "right_x": right_x,
        "left_nodes": [
            {
                "name": name,
                "pretty": prettify_eco_name(name),
                "y": left[name][0],
                "h": left[name][1],
                "count": src_weight[name],
            }
            for name, _ in sources
        ],
        "right_nodes": [
            {
                "name": name,
                "pretty": prettify_eco_name(name),
                "y": right[name][0],
                "h": right[name][1],
                "count": tgt_weight[name],
            }
            for name, _ in targets
        ],
        "edges": edges,
    }
