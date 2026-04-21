"""Wikidata + Wikipedia lookup for `explain_eco_item`.

Backs the `explain_eco_item` MCP tool. Flow:

1. If `category` is provided, run a SPARQL query against `query.wikidata.org`
   scoped to that category's instance-of class and pull out the image (P18),
   short description, and a handful of category-specific facts (atomic number,
   taxon rank, Mohs hardness, etc.).
2. Otherwise, start with Wikipedia's REST `/page/summary/{name}` endpoint —
   it's cheap and always returns *something* (or a 404) — and only fall back
   to SPARQL if the page is a disambiguation stub or unreachable.
3. Cache everything in SQLite at `~/.cache/eco-mcp-app/wikidata.sqlite` with a
   7-day TTL. Wikidata's SPARQL endpoint rate-limits aggressively; the cache
   is not optional.
4. External image URLs are fetched server-side and inlined as `data:` URIs
   so Claude Desktop's CSP (see `claude-ai-mcp#40`) doesn't block them.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{name}"

USER_AGENT = "eco-mcp-app/0.1 (coilysiren@gmail.com)"

# 7 days. Wikidata facts are stable; Wikipedia summaries occasionally change
# but not fast enough to matter for a game ecopedia card.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60

# Wikidata instance-of (P31) classes used to disambiguate a bare item name.
# Restricted to the five categories the `explain_eco_item` tool advertises.
CATEGORY_INSTANCE_OF: dict[str, list[str]] = {
    # Q11344 = chemical element. Eco's "materials" are mostly elements
    # (Iron, Copper, Gold) plus a few compounds; the element filter is a
    # pragmatic default that matches all the vanilla "Bar" items.
    "material": ["Q11344"],
    # Q16521 = taxon. All living things are taxa in Wikidata's ontology.
    "plant": ["Q16521"],
    "animal": ["Q16521"],
    # Q7946 = mineral.
    "mineral": ["Q7946"],
    # Q2095 = food. Also matches many "ingredient" / "dish" subclasses.
    "food": ["Q2095"],
}

SUPPORTED_CATEGORIES = frozenset(CATEGORY_INSTANCE_OF.keys())

# Category -> list of (label, wikidata property id) pairs to surface in the
# rendered facts table. Only the properties actually returned by SPARQL make
# it into the card; missing fields are elided.
CATEGORY_FACTS: dict[str, list[tuple[str, str]]] = {
    "material": [("Atomic number", "P1086")],
    "plant": [("Taxon rank", "P105")],
    "animal": [("Taxon rank", "P105"), ("Conservation status", "P141")],
    "mineral": [("Mohs hardness", "P1088")],
    "food": [("Main food source", "P186")],
}


def cache_path() -> Path:
    """`~/.cache/eco-mcp-app/wikidata.sqlite` — overridable via env for tests."""
    override = os.environ.get("ECO_MCP_WIKIDATA_CACHE")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "eco-mcp-app" / "wikidata.sqlite"


def _connect() -> sqlite3.Connection:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    return conn


def _cache_get(key: str, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> Any | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT value, fetched_at FROM cache WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    value_json, fetched_at = row
    if (time.time() - float(fetched_at)) > ttl_seconds:
        return None
    try:
        return json.loads(value_json)
    except json.JSONDecodeError:
        return None


def _cache_put(key: str, value: Any) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class EcopediaCard:
    """Rendered payload for the `explain_eco_item` tool."""

    name: str
    category: str | None
    title: str = ""
    description: str = ""
    image_data_uri: str | None = None
    image_credit: str | None = None
    facts: list[tuple[str, str]] = field(default_factory=list)
    source: str = ""
    source_url: str | None = None
    not_found: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["facts"] = [list(p) for p in self.facts]
        return d


def _sparql_query(name: str, category: str) -> str:
    """Build a SPARQL query that returns at most one row for `name` in `category`.

    The query is intentionally conservative: exact English label match, instance
    of (or subclass of) the category's anchor class, and a single row via LIMIT 1
    so we don't surprise the rate limiter. The item label always comes back in
    English — non-English servers can add a fallback later.
    """
    anchors = CATEGORY_INSTANCE_OF.get(category, [])
    if not anchors:
        raise ValueError(f"Unsupported category: {category}")
    values_clause = " ".join(f"wd:{qid}" for qid in anchors)
    facts = CATEGORY_FACTS.get(category, [])
    optional_clauses = "\n".join(
        f"  OPTIONAL {{ ?item wdt:{pid} ?{label.replace(' ', '_')}. }}" for label, pid in facts
    )
    select_vars = " ".join(f"?{label.replace(' ', '_')}" for label, _pid in facts)
    name_escaped = name.replace('"', '\\"')
    return f"""
SELECT ?item ?itemLabel ?itemDescription ?image {select_vars} WHERE {{
  VALUES ?anchor {{ {values_clause} }}
  ?item wdt:P31/wdt:P279* ?anchor.
  ?item rdfs:label "{name_escaped}"@en.
  OPTIONAL {{ ?item wdt:P18 ?image. }}
{optional_clauses}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 1
""".strip()


async def _fetch_sparql(
    client: httpx.AsyncClient, name: str, category: str
) -> dict[str, Any] | None:
    """Run the SPARQL query. Returns the first binding or None."""
    query = _sparql_query(name, category)
    cache_key = f"sparql::{category}::{name.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached else None

    try:
        resp = await client.get(
            WIKIDATA_SPARQL_URL,
            params={"query": query, "format": "json"},
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/sparql-results+json",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None

    bindings = data.get("results", {}).get("bindings", [])
    first = bindings[0] if bindings else None
    # Cache empty result as `{}` (falsy) so we don't re-hammer the endpoint
    # looking up a name that legitimately has no hit.
    _cache_put(cache_key, first or {})
    return first


async def _fetch_wikipedia_summary(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    """GET the Wikipedia REST summary. Returns the parsed body or None on miss."""
    cache_key = f"wiki::{name.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached if cached else None

    url = WIKIPEDIA_SUMMARY_URL.format(name=name.replace(" ", "_"))
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=10.0,
        )
        if resp.status_code == 404:
            _cache_put(cache_key, {})
            return None
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None

    _cache_put(cache_key, data)
    return data


async def _inline_image(client: httpx.AsyncClient, image_url: str) -> str | None:
    """Fetch an external image and return it as a `data:` URI.

    Claude Desktop's CSP blocks external origins (claude-ai-mcp#40), so any
    image the iframe renders has to be inlined. Cached with the same 7-day
    TTL because Wikimedia thumbnails are content-addressed and don't change
    under the same URL.
    """
    cache_key = f"img::{image_url}"
    cached = _cache_get(cache_key)
    if isinstance(cached, str) and cached:
        return cached
    try:
        resp = await client.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    b64 = base64.b64encode(resp.content).decode()
    data_uri = f"data:{mime};base64,{b64}"
    _cache_put(cache_key, data_uri)
    return data_uri


def _extract_sparql_value(binding: dict[str, Any], var: str) -> str | None:
    v = binding.get(var)
    if isinstance(v, dict):
        val = v.get("value")
        if isinstance(val, str) and val:
            return val
    return None


def _build_card_from_sparql(name: str, category: str, binding: dict[str, Any]) -> EcopediaCard:
    title = _extract_sparql_value(binding, "itemLabel") or name
    description = _extract_sparql_value(binding, "itemDescription") or ""
    entity = _extract_sparql_value(binding, "item")
    facts: list[tuple[str, str]] = []
    for label, _pid in CATEGORY_FACTS.get(category, []):
        value = _extract_sparql_value(binding, label.replace(" ", "_"))
        if value:
            # Wikidata returns URIs for linked entities; surface the trailing
            # segment rather than the full `http://www.wikidata.org/entity/Q...`.
            if value.startswith("http://") or value.startswith("https://"):
                value = value.rsplit("/", 1)[-1]
            facts.append((label, value))
    return EcopediaCard(
        name=name,
        category=category,
        title=title,
        description=description,
        facts=facts,
        source="Wikidata",
        source_url=entity,
    )


def _build_card_from_wikipedia(name: str, data: dict[str, Any]) -> EcopediaCard:
    title = str(data.get("title") or name)
    description = str(data.get("extract") or data.get("description") or "")
    # `type == "disambiguation"` means the name is ambiguous — treat it as a
    # miss so the caller can fall back to SPARQL with an explicit category.
    page_type = data.get("type")
    source_url = None
    content_urls = data.get("content_urls") or {}
    desktop = content_urls.get("desktop") or {}
    if isinstance(desktop, dict):
        source_url = desktop.get("page")
    return EcopediaCard(
        name=name,
        category=None,
        title=title,
        description=description,
        facts=[],
        source="Wikipedia",
        source_url=source_url,
        not_found=(page_type == "disambiguation"),
    )


async def build_ecopedia_card(name: str, category: str | None = None) -> EcopediaCard:
    """Main entry point. Returns an EcopediaCard, with `not_found=True` on total miss.

    Shape of the flow matches todo/06-wikidata-ecopedia.md:
      - category given -> SPARQL first, Wikipedia as fallback for description
      - no category -> Wikipedia first (cheap), SPARQL only if disambiguation
    """
    name = name.strip()
    if not name:
        return EcopediaCard(name="", category=category, not_found=True)

    if category and category not in SUPPORTED_CATEGORIES:
        return EcopediaCard(
            name=name,
            category=category,
            title=name,
            description=(
                f"Unsupported category '{category}'. "
                f"Try one of: {', '.join(sorted(SUPPORTED_CATEGORIES))}."
            ),
            not_found=True,
        )

    async with httpx.AsyncClient() as client:
        card: EcopediaCard | None = None
        sparql_binding: dict[str, Any] | None = None

        if category:
            sparql_binding = await _fetch_sparql(client, name, category)
            if sparql_binding:
                card = _build_card_from_sparql(name, category, sparql_binding)

        if card is None or not card.description:
            wiki = await _fetch_wikipedia_summary(client, name)
            if wiki:
                wiki_card = _build_card_from_wikipedia(name, wiki)
                if card is None:
                    # No SPARQL result — if Wikipedia returned a disambiguation
                    # page and we don't have a category, fall through to SPARQL
                    # with each supported category until something hits.
                    if wiki_card.not_found and not category:
                        for cat in ("material", "mineral", "food", "plant", "animal"):
                            binding = await _fetch_sparql(client, name, cat)
                            if binding:
                                card = _build_card_from_sparql(name, cat, binding)
                                sparql_binding = binding
                                break
                    if card is None:
                        card = wiki_card
                else:
                    # SPARQL title + facts, but prefer Wikipedia's human-friendly
                    # prose over the Wikidata one-liner description.
                    if wiki_card.description:
                        card.description = wiki_card.description
                    if not card.source_url:
                        card.source_url = wiki_card.source_url

        if card is None:
            return EcopediaCard(name=name, category=category, title=name, not_found=True)

        image_url: str | None = None
        if sparql_binding:
            image_url = _extract_sparql_value(sparql_binding, "image")
        if not image_url:
            # Wikipedia summary body: thumbnail.source / originalimage.source.
            # Only populated if we fetched via Wikipedia, so re-check the cache.
            wiki_cached = _cache_get(f"wiki::{name.lower()}")
            if isinstance(wiki_cached, dict) and wiki_cached:
                thumb = wiki_cached.get("thumbnail") or {}
                if isinstance(thumb, dict):
                    image_url = thumb.get("source")
        if image_url:
            card.image_data_uri = await _inline_image(client, image_url)

        return card
