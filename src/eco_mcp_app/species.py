"""Species profile tool — iNaturalist + Wikipedia + live Eco population curve.

The card combines three data sources:

1. **Eco admin exporter** — `/api/v1/exporter/specieslist` (newline-delimited
   plain text) + `/api/v1/exporter/species?speciesName=X` (CSV of
   `Time,Value` where Time is seconds since cycle start at 600s cadence).
   Both need `X-API-Key` from SSM `/eco-mcp-app/api-admin-token` in `us-east-1`.
2. **iNaturalist** — `GET /v1/taxa?q={name}&rank=species,genus&per_page=10`.
   Public, no auth, 60 req/min, requires a User-Agent header. We ask for
   both species- and genus-level hits because Eco's `BisonSpecies` maps to
   the `Bison` *genus* in iNat — species-only filtering dropped it and
   left a grass taxon ("bison grass") as the top hit.
3. **Wikipedia REST** — `/api/rest_v1/page/summary/{title}` as fallback when
   iNat returns zero taxa.

External facts (iNat / Wikipedia) are cached 7 days in a SQLite at
`~/.cache/eco-mcp-app/inat.sqlite`. The live population CSV is **not** cached
— it changes every Eco cycle day, and the admin endpoint is cheap.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# --- Constants -------------------------------------------------------------

ECO_BASE_URL = os.environ.get("ECO_ADMIN_BASE_URL", "http://eco.coilysiren.me:3001")
INAT_BASE_URL = "https://api.inaturalist.org/v1"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"

# iNat asks public consumers to identify themselves.
INAT_USER_AGENT = "eco-mcp-app/0.1 (coilysiren@gmail.com)"

# 7-day TTL for stable taxonomic facts.
_EXTERNAL_CACHE_TTL_S = 7 * 24 * 3600
# Overrideable for tests — see test_species.py.
_CACHE_DIR_ENV = "ECO_MCP_APP_CACHE_DIR"
_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "eco-mcp-app"

# iNat rate limit: 60 req/min. In-process window.
_INAT_RATE_WINDOW_S = 60.0
_INAT_RATE_MAX = 60

# Species-name override map for ids that CamelCase-cleaning gets wrong.
_SPECIES_NAME_OVERRIDES: dict[str, str] = {
    # "Joshua" alone won't match anything in iNat; the real species is
    # Yucca brevifolia, commonly known as the Joshua Tree.
    "JoshuaSpecies": "Joshua Tree",
}


# --- Data shapes -----------------------------------------------------------


@dataclass
class PopulationSample:
    day: float  # days since cycle start
    value: int


@dataclass
class SpeciesPayload:
    view: str = "eco_species"
    name: str = ""  # Cleaned human-readable name
    species_id: str = ""  # Raw CamelCase id from specieslist
    photo_data_uri: str | None = None
    photo_attribution: str | None = None
    wiki_extract: str | None = None
    wiki_url: str | None = None
    source: str = "none"  # "inat" | "wikipedia" | "none"
    taxonomy: list[dict[str, str]] = field(default_factory=list)
    conservation_status: str | None = None
    population: list[PopulationSample] = field(default_factory=list)
    population_first: int | None = None
    population_latest: int | None = None
    population_delta: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "view": self.view,
            "name": self.name,
            "speciesId": self.species_id,
            "photoDataUri": self.photo_data_uri,
            "photoAttribution": self.photo_attribution,
            "wikiExtract": self.wiki_extract,
            "wikiUrl": self.wiki_url,
            "source": self.source,
            "taxonomy": self.taxonomy,
            "conservationStatus": self.conservation_status,
            "population": [{"day": s.day, "value": s.value} for s in self.population],
            "populationFirst": self.population_first,
            "populationLatest": self.population_latest,
            "populationDelta": self.population_delta,
            "error": self.error,
        }


# --- Name cleaning ---------------------------------------------------------


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def clean_species_name(species_id: str) -> str:
    """Turn `WheatSpecies` into `Wheat`, `SnappingTurtleSpecies` into
    `Snapping Turtle`, `MoonJellyfishSpecies` into `Moon Jellyfish`.

    Respects `_SPECIES_NAME_OVERRIDES` for ids where CamelCase splitting
    produces the wrong common name (e.g. `JoshuaSpecies` → `Joshua Tree`).
    """
    if species_id in _SPECIES_NAME_OVERRIDES:
        return _SPECIES_NAME_OVERRIDES[species_id]
    base = species_id
    if base.endswith("Species"):
        base = base[: -len("Species")]
    if not base:
        return species_id
    # Insert a space before each capital letter that isn't the first char.
    return _CAMEL_BOUNDARY.sub(" ", base).strip()


# --- Admin API key ---------------------------------------------------------


def _get_admin_api_key() -> str | None:
    """Fetch the admin API key.

    Prefer the `ECO_ADMIN_API_KEY` env var (used by tests + local dev), then
    fall back to SSM `/eco-mcp-app/api-admin-token` in `us-east-1`. Returns
    `None` if nothing is available — the exporter call will 401 and we'll
    surface a graceful placeholder.
    """
    env = os.environ.get("ECO_ADMIN_API_KEY")
    if env:
        return env
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        client = boto3.client("ssm", region_name="us-east-1")
        resp = client.get_parameter(Name="/eco-mcp-app/api-admin-token", WithDecryption=True)
        return str(resp["Parameter"]["Value"])
    except Exception:
        return None


# --- Cache (SQLite) --------------------------------------------------------


def _cache_dir() -> Path:
    return Path(os.environ.get(_CACHE_DIR_ENV) or _DEFAULT_CACHE_DIR)


def _cache_db_path() -> Path:
    return _cache_dir() / "inat.sqlite"


def _open_cache_db() -> sqlite3.Connection:
    path = _cache_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS http_cache ("
        "  key TEXT PRIMARY KEY,"
        "  fetched_at REAL NOT NULL,"
        "  body TEXT NOT NULL"
        ")"
    )
    return conn


def _cache_get(key: str) -> Any | None:
    try:
        with _open_cache_db() as conn:
            row = conn.execute(
                "SELECT fetched_at, body FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    fetched_at, body = row
    if time.time() - fetched_at > _EXTERNAL_CACHE_TTL_S:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _cache_put(key: str, value: Any) -> None:
    try:
        with _open_cache_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO http_cache (key, fetched_at, body) VALUES (?, ?, ?)",
                (key, time.time(), json.dumps(value)),
            )
    except sqlite3.DatabaseError:
        pass


# --- iNat rate limiter -----------------------------------------------------


_INAT_WINDOW: deque[float] = deque()


def _inat_rate_gate() -> None:
    now = time.monotonic()
    while _INAT_WINDOW and (now - _INAT_WINDOW[0]) > _INAT_RATE_WINDOW_S:
        _INAT_WINDOW.popleft()
    if len(_INAT_WINDOW) >= _INAT_RATE_MAX:
        # Oldest request is still in the window — sleep until it ages out.
        sleep_s = _INAT_RATE_WINDOW_S - (now - _INAT_WINDOW[0])
        if sleep_s > 0:
            time.sleep(sleep_s)
    _INAT_WINDOW.append(time.monotonic())


# --- External fetch: iNat --------------------------------------------------


async def _fetch_inat_taxon(name: str) -> dict[str, Any] | None:
    """Return the best iNat taxon hit for `name`, or None if zero results.

    Queries both species- and genus-level taxa and re-ranks results to
    prefer an exact (case-insensitive) match on `name`,
    `preferred_common_name`, or `matched_term` before falling back to
    iNat's natural ordering. This avoids iNat's full-text search
    returning an unrelated grass for "Bison" (whose correct hit is the
    `Bison` genus).
    """
    cache_key = f"inat:taxon:{name.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached else None
    _inat_rate_gate()
    url = f"{INAT_BASE_URL}/taxa"
    params = {
        "q": name,
        "rank": "species,genus",
        "per_page": "10",
        "is_active": "true",
        "all_names": "false",
    }
    headers = {"User-Agent": INAT_USER_AGENT}
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results") or []
    taxon = _pick_best_taxon(results, name)
    # Cache both hits and misses so we don't re-query for modded names.
    _cache_put(cache_key, taxon or {})
    return taxon


def _pick_best_taxon(results: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    if not results:
        return None
    q = query.strip().lower()
    for r in results:
        candidates = [
            r.get("name"),
            r.get("preferred_common_name"),
            r.get("matched_term"),
        ]
        if any(isinstance(c, str) and c.lower() == q for c in candidates):
            return r
    return results[0]


async def _fetch_inat_photo_bytes(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": INAT_USER_AGENT})
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPError:
        return None


def _photo_to_data_uri(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# --- External fetch: Wikipedia --------------------------------------------


async def _fetch_wikipedia_summary(name: str) -> dict[str, Any] | None:
    cache_key = f"wiki:summary:{name.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached else None
    title = quote(name.replace(" ", "_"))
    url = f"{WIKIPEDIA_SUMMARY_URL}/{title}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"User-Agent": INAT_USER_AGENT})
            if resp.status_code == 404:
                _cache_put(cache_key, {})
                return None
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        return None
    _cache_put(cache_key, data)
    return data


# --- Eco admin fetch -------------------------------------------------------


async def fetch_species_list() -> list[str]:
    """Return the exporter's newline-delimited species id list."""
    api_key = _get_admin_api_key()
    headers = {"X-API-Key": api_key} if api_key else {}
    url = f"{ECO_BASE_URL}/api/v1/exporter/specieslist"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        text = resp.text
    return [line.strip() for line in text.splitlines() if line.strip()]


async def fetch_species_population(species_id: str) -> list[PopulationSample]:
    """Return population samples (converted to days-since-cycle-start)."""
    api_key = _get_admin_api_key()
    headers = {"X-API-Key": api_key} if api_key else {}
    url = f"{ECO_BASE_URL}/api/v1/exporter/species"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, params={"speciesName": species_id}, headers=headers)
        resp.raise_for_status()
        body = resp.text
    samples: list[PopulationSample] = []
    reader = csv.reader(io.StringIO(body))
    header_seen = False
    for row in reader:
        if not row:
            continue
        if not header_seen:
            header_seen = True
            # Header row is `"Time","Value"`. Skip if first cell isn't a number.
            try:
                float(row[0])
            except (ValueError, IndexError):
                continue
        try:
            seconds = float(row[0])
            value = int(float(row[1]))
        except (ValueError, IndexError):
            continue
        samples.append(PopulationSample(day=seconds / 86400.0, value=value))
    return samples


# --- Orchestration ---------------------------------------------------------


def _extract_taxonomy(taxon: dict[str, Any]) -> list[dict[str, str]]:
    """Flatten iNat `ancestors` + the taxon itself into a breadcrumb."""
    breadcrumb: list[dict[str, str]] = []
    for ancestor in taxon.get("ancestors") or []:
        breadcrumb.append(
            {
                "rank": (ancestor.get("rank") or "").title(),
                "name": ancestor.get("preferred_common_name") or ancestor.get("name") or "",
            }
        )
    breadcrumb.append(
        {
            "rank": (taxon.get("rank") or "species").title(),
            "name": taxon.get("preferred_common_name") or taxon.get("name") or "",
        }
    )
    return [b for b in breadcrumb if b["name"]]


async def build_species_payload(species_id: str) -> SpeciesPayload:
    """Assemble the full card payload for a given Eco species id."""
    name = clean_species_name(species_id)
    payload = SpeciesPayload(name=name, species_id=species_id)

    # 1. In-server population — cheap, try first.
    try:
        samples = await fetch_species_population(species_id)
    except httpx.HTTPError as e:
        payload.error = f"Could not fetch population for {species_id}: {e}"
        samples = []
    payload.population = samples
    if samples:
        payload.population_first = samples[0].value
        payload.population_latest = samples[-1].value
        payload.population_delta = samples[-1].value - samples[0].value

    # 2. iNat lookup for photo + taxonomy.
    taxon: dict[str, Any] | None = None
    try:
        taxon = await _fetch_inat_taxon(name)
    except httpx.HTTPError:
        taxon = None
    if taxon:
        payload.source = "inat"
        payload.taxonomy = _extract_taxonomy(taxon)
        status = taxon.get("conservation_status") or {}
        if isinstance(status, dict) and status.get("status_name"):
            payload.conservation_status = str(status["status_name"])
        photo = taxon.get("default_photo") or {}
        photo_url = photo.get("medium_url") or photo.get("square_url")
        if photo_url:
            photo_bytes = await _fetch_inat_photo_bytes(photo_url)
            if photo_bytes:
                payload.photo_data_uri = _photo_to_data_uri(photo_bytes)
                payload.photo_attribution = photo.get("attribution")
        payload.wiki_url = taxon.get("wikipedia_url")
        # iNat embeds a wiki summary on the taxon itself sometimes.
        if taxon.get("wikipedia_summary"):
            payload.wiki_extract = _first_two_sentences(
                _strip_html(str(taxon["wikipedia_summary"]))
            )

    # 3. Wikipedia fallback if iNat missed or produced no summary.
    if not payload.wiki_extract:
        wiki = await _fetch_wikipedia_summary(name)
        if wiki and wiki.get("extract"):
            payload.wiki_extract = _first_two_sentences(str(wiki["extract"]))
            payload.wiki_url = (wiki.get("content_urls") or {}).get("desktop", {}).get(
                "page"
            ) or payload.wiki_url
            if payload.source == "none":
                payload.source = "wikipedia"

    return payload


_HTML_TAG = re.compile(r"<[^>]+>")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _strip_html(text: str) -> str:
    return _HTML_TAG.sub("", text).strip()


def _first_two_sentences(text: str) -> str:
    parts = _SENTENCE_SPLIT.split(text.strip(), maxsplit=2)
    return " ".join(parts[:2]).strip()
