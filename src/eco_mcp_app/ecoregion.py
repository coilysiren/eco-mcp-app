"""Biodiversity drift + ecoregion-match tool implementation.

Pulls three slices of Eco server data and collapses them into a single card:

1. Biome composition from the public worldlayers endpoint — parsed out of the
   per-layer ``Summary`` strings, which look like ``"4%"``. Percentages do NOT
   sum to 100; large chunks of the world (shallow water, mountain, transitional
   terrain) are uncounted — see ``todo/10-ecoregion-biodiversity.md``.
2. Nearest real-world ecoregion match via cosine similarity against a small,
   committed WWF-inspired fixture. The Eco biome vector is normalized to
   ``sum=1`` first so the classifier is comparing *shapes*, not absolute area.
3. Species drift from the admin exporter — per-species CSV where ``Time`` is
   seconds since cycle start, ``Value`` is population count. We bucket species
   into "boom" and "bust" lists by relative change from first to last sample.

Everything here is transport-agnostic so the same functions back both the MCP
call-tool path and the HTTP ``/preview`` route.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx

# Ordered so the donut chart / table always renders in the same order
# regardless of dict-iteration order in the response.
BIOME_LAYERS: tuple[str, ...] = (
    "TaigaBiome",
    "DesertBiome",
    "WetlandBiome",
    "ColdForestBiome",
    "ForestBiome",
    "WarmForestBiome",
    "TundraBiome",
    "DeepOceanBiome",
    "OceanBiome",
    "GrasslandBiome",
    "RainforestBiome",
    "IceBiome",
)

# Stable palette for the donut. Pulled from the CSS theme variables where they
# match intuitively (moss=forest, water=ocean, sun=desert) and filled in with
# neighbors otherwise. Not rigorous — just needs to be readable side-by-side.
BIOME_COLORS: dict[str, str] = {
    "TaigaBiome": "#3c5a3a",
    "ColdForestBiome": "#4a6b44",
    "ForestBiome": "#5a8a3a",
    "WarmForestBiome": "#7aa84a",
    "RainforestBiome": "#2e5e2a",
    "GrasslandBiome": "#a5d14a",
    "WetlandBiome": "#4a7a6a",
    "OceanBiome": "#4a9cb8",
    "DeepOceanBiome": "#2a5a78",
    "DesertBiome": "#e58f2c",
    "TundraBiome": "#c4b896",
    "IceBiome": "#e0eaf2",
}

# Pretty labels for the card — these are what LayerDisplayName tends to be in
# live data, but we don't depend on the upstream name so the card renders even
# if the /worldlayers endpoint truncates one.
BIOME_DISPLAY: dict[str, str] = {
    "TaigaBiome": "Taiga",
    "DesertBiome": "Desert",
    "WetlandBiome": "Wetland",
    "ColdForestBiome": "Cold forest",
    "ForestBiome": "Forest",
    "WarmForestBiome": "Warm forest",
    "TundraBiome": "Tundra",
    "DeepOceanBiome": "Deep ocean",
    "OceanBiome": "Ocean",
    "GrasslandBiome": "Grassland",
    "RainforestBiome": "Rainforest",
    "IceBiome": "Ice",
}

# Drift needs >= 2 samples to compute a delta. With 600s (10-min) cadence
# that means the cycle has to have ticked at least twice — on Day 3 every
# species should meet this, but the guard is cheap.
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)")

_WORLDLAYERS_PATH = "/api/v1/worldlayers/layers"
_SPECIESLIST_PATH = "/api/v1/exporter/specieslist"
_SPECIES_PATH = "/api/v1/exporter/species"

# Live Eco data caches. `worldlayers` is essentially static over a 5-minute
# window (biome % only changes when terrain is physically edited at scale);
# species CSVs change every 600s so 60s is plenty. Keyed by `(base_url, path)`
# so multi-server use doesn't cross-pollinate.
_WORLDLAYERS_TTL_S = float(os.environ.get("ECO_WORLDLAYERS_CACHE_TTL", "300"))
_SPECIES_TTL_S = float(os.environ.get("ECO_SPECIES_CACHE_TTL", "60"))
_worldlayers_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_specieslist_cache: dict[str, tuple[float, list[str]]] = {}
_species_cache: dict[tuple[str, str], tuple[float, list[tuple[int, float]]]] = {}


# ---------- data loading ----------


def _load_ecoregions_bundled() -> list[dict[str, Any]]:
    """Load the committed WWF fixture.

    Looks first on the filesystem (repo checkout) then falls back to the
    packaged copy. Shipping it as package data would require pyproject wiring;
    for a small JSON blob that a single tool reads, on-disk in the repo is
    simpler. If the file is missing we log a warning-style message and return
    an empty list so the card still renders without the middle section.
    """
    here = Path(__file__).resolve().parent
    # Walk up a couple levels looking for data/ecoregions.json — the file
    # lives at repo root so it's a sibling of src/.
    for parent in (here.parent.parent, here.parent, here):
        candidate = parent / "data" / "ecoregions.json"
        if candidate.exists():
            with candidate.open() as f:
                doc = json.load(f)
            return list(doc.get("regions") or [])
    try:
        packaged = files("eco_mcp_app.data") / "ecoregions.json"  # type: ignore[union-attr]
        return list(json.loads(packaged.read_text()).get("regions") or [])
    except (FileNotFoundError, ModuleNotFoundError):
        return []


# ---------- HTTP fetchers ----------


def _base_url_from_info_url(info_url: str) -> str:
    """``http://host:3001/info`` → ``http://host:3001``. Admin routes share the host."""
    # Strip the trailing /info (or any path); keep scheme + netloc.
    from urllib.parse import urlparse, urlunparse

    p = urlparse(info_url)
    return urlunparse((p.scheme or "http", p.netloc, "", "", "", ""))


async def fetch_worldlayers(base_url: str) -> list[dict[str, Any]]:
    """GET /api/v1/worldlayers/layers — 7 categories, cached 5 min."""
    key = base_url
    now = time.monotonic()
    cached = _worldlayers_cache.get(key)
    if cached and (now - cached[0]) < _WORLDLAYERS_TTL_S:
        return list(cached[1])
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(base_url + _WORLDLAYERS_PATH)
        r.raise_for_status()
        data = r.json()
    cats = list(data) if isinstance(data, list) else []
    _worldlayers_cache[key] = (now, cats)
    return cats


async def fetch_specieslist(base_url: str, api_key: str) -> list[str]:
    """GET /api/v1/exporter/specieslist — newline-delimited plain text.

    Not JSON — the endpoint returns one species name per line. Lines are
    stripped and blank lines dropped so downstream code sees a clean list.
    """
    key = base_url
    now = time.monotonic()
    cached = _specieslist_cache.get(key)
    if cached and (now - cached[0]) < _SPECIES_TTL_S:
        return list(cached[1])
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            base_url + _SPECIESLIST_PATH,
            headers={"X-API-Key": api_key},
        )
        r.raise_for_status()
        names = [line.strip() for line in r.text.splitlines() if line.strip()]
    _specieslist_cache[key] = (now, names)
    return names


async def fetch_species_samples(
    base_url: str, species_name: str, api_key: str
) -> list[tuple[int, float]]:
    """GET /api/v1/exporter/species?speciesName=X — CSV ``"Time","Value"``.

    Time is seconds since cycle start at 600s cadence; Value is population.
    Returns an empty list on parse failure so an individual corrupt series
    doesn't kill the whole drift column.
    """
    key = (base_url, species_name)
    now = time.monotonic()
    cached = _species_cache.get(key)
    if cached and (now - cached[0]) < _SPECIES_TTL_S:
        return list(cached[1])
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            base_url + _SPECIES_PATH,
            params={"speciesName": species_name},
            headers={"X-API-Key": api_key},
        )
        r.raise_for_status()
        text = r.text
    samples: list[tuple[int, float]] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        # Skip header row and any other non-numeric pair.
        try:
            t = int(float(row[0].strip().strip('"')))
            v = float(row[1].strip().strip('"'))
        except (ValueError, IndexError):
            continue
        samples.append((t, v))
    _species_cache[key] = (now, samples)
    return samples


# ---------- biome extraction + normalization ----------


def extract_biome_percents(categories: list[dict[str, Any]]) -> dict[str, float]:
    """Pull out the 12 biome layers from the 7-category worldlayers response.

    Returns a dict keyed by LayerName (e.g. ``TaigaBiome``) with the % of
    world area as a float 0..100. Missing layers get 0.0 so the chart always
    has a full row of keys even on a sparse / custom-modded world.
    """
    out = dict.fromkeys(BIOME_LAYERS, 0.0)
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        if cat.get("Category") != "Biome":
            continue
        for entry in cat.get("List") or []:
            name = entry.get("LayerName")
            if name in out:
                summary = entry.get("Summary") or ""
                m = _PERCENT_RE.match(str(summary).strip())
                if m:
                    try:
                        out[name] = float(m.group(1))
                    except ValueError:
                        pass
    return out


def normalize_vector(raw: dict[str, float]) -> dict[str, float]:
    """Scale so the values sum to 1.0. All-zero input maps to all-zero output."""
    total = sum(raw.values())
    if total <= 0:
        return dict.fromkeys(raw, 0.0)
    return {k: v / total for k, v in raw.items()}


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity over the shared key set. 0..1 for non-negative inputs."""
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class EcoregionMatch:
    name: str
    description: str
    similarity: float


def top_ecoregions(
    normalized_biomes: dict[str, float],
    regions: list[dict[str, Any]],
    n: int = 3,
) -> list[EcoregionMatch]:
    """Rank the committed regions by cosine similarity to the world vector.

    Ties are broken alphabetically by name so the ordering is deterministic
    across consecutive calls (acceptance criterion in the spec).
    """
    scored: list[EcoregionMatch] = []
    for r in regions:
        vec = r.get("biome_vector") or {}
        sim = cosine_similarity(normalized_biomes, vec)
        scored.append(
            EcoregionMatch(
                name=r.get("name") or "Unnamed",
                description=r.get("description") or "",
                similarity=sim,
            )
        )
    scored.sort(key=lambda m: (-m.similarity, m.name))
    return scored[:n]


# ---------- drift ----------


@dataclass
class SpeciesDrift:
    name: str
    first: float
    latest: float
    delta_rel: float  # (latest - first) / first; 0 if first == 0


def compute_drift(samples: list[tuple[int, float]]) -> SpeciesDrift | None:
    """Reduce a CSV series to a single first→latest relative change.

    Returns None when fewer than two samples are available (e.g. the cycle
    just ticked over and only one datapoint exists yet). Samples are sorted
    on ``Time`` before reduction so a non-monotonic CSV doesn't blow it up.
    """
    if len(samples) < 2:
        return None
    ordered = sorted(samples, key=lambda s: s[0])
    first_v = ordered[0][1]
    last_v = ordered[-1][1]
    if first_v == 0.0:
        delta_rel = 0.0 if last_v == 0.0 else float("inf")
    else:
        delta_rel = (last_v - first_v) / first_v
    return SpeciesDrift(name="", first=first_v, latest=last_v, delta_rel=delta_rel)


def rank_drift(
    series: dict[str, list[tuple[int, float]]],
    n: int = 5,
) -> tuple[list[SpeciesDrift], list[SpeciesDrift]]:
    """Return (boom, bust) lists of top-n relative movers.

    ``boom`` and ``bust`` are mutually exclusive — a species with ``delta_rel
    == 0`` appears in neither list. On Day 3 many species will have 0 delta;
    the card handles the empty case by rendering a placeholder.
    """
    drifts: list[SpeciesDrift] = []
    for name, samples in series.items():
        d = compute_drift(samples)
        if d is None:
            continue
        d.name = name
        drifts.append(d)
    boom = sorted(
        (d for d in drifts if d.delta_rel > 0),
        key=lambda d: -d.delta_rel,
    )[:n]
    bust = sorted(
        (d for d in drifts if d.delta_rel < 0),
        key=lambda d: d.delta_rel,
    )[:n]
    return boom, bust


# ---------- payload + cache invalidation for tests ----------


def _clear_caches() -> None:
    """Wipe in-process caches — used by the test suite."""
    _worldlayers_cache.clear()
    _specieslist_cache.clear()
    _species_cache.clear()


def build_payload(
    biome_percents: dict[str, float],
    matches: list[EcoregionMatch],
    boom: list[SpeciesDrift],
    bust: list[SpeciesDrift],
    *,
    species_seen: int,
    species_with_drift: int,
    admin_available: bool,
    source_url: str,
) -> dict[str, Any]:
    """Assemble the serializable payload used by both the Jinja card and JSON content."""
    raw_sum = sum(biome_percents.values())
    unclassified = max(0.0, 100.0 - raw_sum)
    return {
        "view": "eco_ecoregion",
        "sourceUrl": source_url,
        "biomes": [
            {
                "name": key,
                "display": BIOME_DISPLAY.get(key, key),
                "percent": biome_percents.get(key, 0.0),
                "color": BIOME_COLORS.get(key, "#888888"),
            }
            for key in BIOME_LAYERS
        ],
        "unclassifiedPercent": unclassified,
        "rawSumPercent": raw_sum,
        "ecoregionMatches": [
            {
                "name": m.name,
                "description": m.description,
                "similarity": m.similarity,
            }
            for m in matches
        ],
        "drift": {
            "boom": [
                {
                    "name": d.name,
                    "first": d.first,
                    "latest": d.latest,
                    "deltaRel": d.delta_rel,
                }
                for d in boom
            ],
            "bust": [
                {
                    "name": d.name,
                    "first": d.first,
                    "latest": d.latest,
                    "deltaRel": d.delta_rel,
                }
                for d in bust
            ],
            "speciesSeen": species_seen,
            "speciesWithDrift": species_with_drift,
        },
        "adminAvailable": admin_available,
    }


async def gather_ecoregion_payload(
    info_url: str,
    *,
    api_key: str | None,
    regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Orchestrator: fetch everything, tolerate admin-endpoint failure.

    The public worldlayers endpoint is required. Admin endpoints
    (specieslist + species) are best-effort — if no API key is configured or
    the server returns a 4xx/5xx, the drift strip renders its empty state
    instead of the whole tool failing.
    """
    base_url = _base_url_from_info_url(info_url)
    if regions is None:
        regions = _load_ecoregions_bundled()

    categories = await fetch_worldlayers(base_url)
    biomes_raw = extract_biome_percents(categories)
    normalized = normalize_vector(biomes_raw)
    matches = top_ecoregions(normalized, regions)

    boom: list[SpeciesDrift] = []
    bust: list[SpeciesDrift] = []
    species_seen = 0
    species_with_drift = 0
    admin_available = False

    if api_key:
        try:
            names = await fetch_specieslist(base_url, api_key)
            admin_available = True
            series: dict[str, list[tuple[int, float]]] = {}
            for name in names:
                try:
                    samples = await fetch_species_samples(base_url, name, api_key)
                except httpx.HTTPError:
                    continue
                if samples:
                    series[name] = samples
            species_seen = len(series)
            boom, bust = rank_drift(series)
            species_with_drift = len(boom) + len(bust)
        except httpx.HTTPError:
            admin_available = False

    return build_payload(
        biomes_raw,
        matches,
        boom,
        bust,
        species_seen=species_seen,
        species_with_drift=species_with_drift,
        admin_available=admin_available,
        source_url=info_url,
    )
