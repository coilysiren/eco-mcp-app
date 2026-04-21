# Task 5 — iNaturalist Species Profile + Live Population Curve

**Prereq**: read `todo/README.md` first.

## Goal

Build an MCP tool `get_eco_species(name)` rendering a card combining real-world iNaturalist data with live in-server population.

## Data sources

Admin endpoints (header `X-API-Key` from SSM `/eco-mcp-app/api-admin-token`, region `us-east-1`):

- `GET http://eco.coilysiren.me:3001/api/v1/exporter/specieslist`
  - **Newline-delimited plain text, NOT JSON**. Parse via `resp.text.splitlines()`.
  - Each line is a CamelCase species id like `WheatSpecies`, `BisonSpecies`, `SnappingTurtleSpecies`. Count varies by modset — don't hardcode a number.
- `GET http://eco.coilysiren.me:3001/api/v1/exporter/species?speciesName={name}`
  - Returns CSV with header `"Time","Value"`.
  - **`Time` is seconds since cycle start** at 600 s (10-min) sample cadence. When rendering a line chart, convert to days via `seconds / 86400`.

External, public:

- iNaturalist: `GET https://api.inaturalist.org/v1/taxa?q={cleaned_name}&rank=species&per_page=1`
  - No auth. Required header: `User-Agent: eco-mcp-app/0.1 (coilysiren@gmail.com)`.
  - Rate limit: 60 req/min.
- Wikipedia REST fallback: `GET https://en.wikipedia.org/api/rest_v1/page/summary/{title}` — used when iNat returns zero taxa.

## Name cleaning

1. Strip trailing `Species` suffix.
2. CamelCase → spaced words: insert space before each capital except the first.
3. Examples that work: `WheatSpecies → "Wheat"`, `BighornSheepSpecies → "Bighorn Sheep"`, `MoonJellyfishSpecies → "Moon Jellyfish"`.
4. Known edge cases in current specieslist (verified in live data):
   - `JoshuaSpecies` → "Joshua" — iNat won't find it; real name is **Joshua Tree**. Either keep an override map or accept the Wikipedia fallback path.
   - `DwarfWillowSpecies`, `PacificSardineSpecies` — clean normally.
   - Modded species (e.g. anything from the BunWulf mod family) will have **zero iNat hits**; fall back to Wikipedia, and if that's also empty, render a placeholder panel (no photo, no taxonomy) with just the in-server population chart.

## Card layout

- **Top**: photo from iNat taxon (inline as data URI — CSP blocks external image origins); Wikipedia extract (first 2 sentences).
- **Middle**: taxonomy breadcrumb (`Kingdom > Phylum > ... > Species`), conservation status if iNat has it.
- **Bottom**: line chart of live population from the exporter endpoint. X axis: days since cycle start. Show delta from first sample to latest.

## Caching

- SQLite at `~/.cache/eco-mcp-app/inat.sqlite`.
- iNat taxon responses: 7-day TTL (real-world taxa are stable).
- Wikipedia summary: 7-day TTL.
- **Live population CSV: no cache, OR ≤ 60-second TTL** — changes every cycle day.
- Rate-limit iNat to 60 req/min in-process.

## Out of scope

- iNat OAuth — read-only endpoints don't need it, and app registration has prerequisites the user doesn't meet.

## Acceptance

- Tool succeeds on 5 random species from the current specieslist, rendering a card with photo + taxonomy + live population curve.
- At least one modded/fictional species (a name not in iNat) renders a graceful fallback card without crashing.
- iNat cache populates and subsequent calls hit SQLite.
- `inv smoke` passes.
