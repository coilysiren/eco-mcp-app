# Task 6 — Wikidata Ecopedia

**Prereq**: read `todo/README.md` first.

## Goal

Build an MCP tool `explain_eco_item(name, category?)` rendering a card about any Eco item using Wikidata + Wikipedia.

## Flow

1. `category` is optional; it disambiguates the SPARQL query. Example: `name="Iron"`, `category="material"` filters to Q677 (chemical element) rather than mythological figures named Iron.
2. Query `https://query.wikidata.org/sparql` for the item's Wikidata entity; extract:
   - image (P18)
   - short description
   - category-specific facts (P-codes vary):
     - `material` (elements): atomic number P1086
     - `plant`: taxon rank P105
     - `animal`: taxon rank P105, conservation status P141
     - `mineral`: Mohs hardness P1088
     - `food`: main food source P186
3. Fallback: `GET https://en.wikipedia.org/api/rest_v1/page/summary/{name}` for a short text summary when SPARQL yields nothing usable.
4. Render a card: image, 2-line description, a facts table.

## Required request headers (both Wikidata and Wikipedia)

```
User-Agent: eco-mcp-app/0.1 (coilysiren@gmail.com)
Accept: application/sparql-results+json    # Wikidata
Accept: application/json                    # Wikipedia
```

## Caching

- SQLite at `~/.cache/eco-mcp-app/wikidata.sqlite`.
- 7-day TTL on all external responses.
- Wikidata SPARQL endpoint rate-limits aggressively (commonly 60 req/min per IP); **never call without caching**.

## Default category

If no `category` provided: try Wikipedia REST `/page/summary/{name}` first (cheap, always returns something or 404). Only hit SPARQL when that fails or returns an ambiguous disambiguation page.

## Categories to support

- `material`
- `plant`
- `animal`
- `mineral`
- `food`

## Implementation notes

- Inline the image as a `data:` URI (CSP).
- Add MCP tool to `src/eco_mcp_app/server.py`.
- Template under `src/eco_mcp_app/templates/partials/`.

## Acceptance

- Tool works on `["Iron", "Oak", "Bison", "Wheat", "Quartz"]` and returns a card with image + description + facts for each.
- Second call for any of those is served from SQLite (check `~/.cache/eco-mcp-app/wikidata.sqlite` exists and contains rows).
- `inv smoke` passes.
