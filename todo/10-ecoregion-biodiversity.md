# Task 10 — Biodiversity Drift + Ecoregion Match

**Prereq**: read `todo/README.md` first.

## Goal

Build an MCP tool `get_eco_ecoregion` rendering a card that:
1. Classifies the world's biome composition against real-world ecoregions.
2. Shows per-species population drift since cycle start.

## Data sources

Public:
- `GET http://eco.coilysiren.me:3001/api/v1/worldlayers/layers` — list of 7 top-level categories. Each entry: `{Category, List: [{LayerName, LayerDisplayName, Summary, Tooltip, Category, DisplayRow}]}`.
  - Category strings confirmed in live data include `"Biome"` and `"Animal"`. **Check the other category strings before assuming `"Plant Group"` is one of them — pull the distinct `Category` values from the response first.**
  - Biome layers (current cycle): `TaigaBiome`, `DesertBiome`, `WetlandBiome`, `ColdForestBiome`, `ForestBiome`, `WarmForestBiome`, `TundraBiome`, `DeepOceanBiome`, `OceanBiome`, `GrasslandBiome`, `RainforestBiome`, `IceBiome` — 12 total.
  - Each biome's `Summary` is a string like `"4%"`. Parse with `float(re.match(r"(\d+(?:\.\d+)?)", summary).group(1))`.

Admin (header `X-API-Key` from SSM `/eco-mcp-app/api-admin-token`, **region `us-east-1`**):
- `GET /api/v1/exporter/specieslist` — **newline-delimited plain text, NOT JSON**. Parse `resp.text.splitlines()`.
- `GET /api/v1/exporter/species?speciesName=X` — CSV `"Time","Value"`. `Time` is **seconds since cycle start** (600 s cadence).

Static, committed in-repo:
- A WWF Terrestrial Ecoregions summary table. Fetch **once** and commit to `data/ecoregions.json` (don't fetch at runtime). Source candidates: `https://ecoregions.appspot.com/` (manual download) or WWF's Terrestrial Ecoregions of the World publication. Shape: `[{name, biome_vector: {TaigaBiome: 0.0, DesertBiome: 0.15, ...}, description}, ...]`.

## IMPORTANT: biome percentages do NOT sum to 100

In live data (verified today):

```
ColdForestBiome 4%, DeepOceanBiome 1%, DesertBiome 4%, ForestBiome 0%,
GrasslandBiome 7%, IceBiome 0%, OceanBiome 13%, RainforestBiome 4%,
TaigaBiome 1%, TundraBiome 1%, WarmForestBiome 3%, WetlandBiome 1%
Sum ≈ 39%
```

The summaries are % of world area, but large chunks of the world (mountains, shallow water, transitional zones) don't fall into any named biome. **Normalize the vector** to `sum=1.0` before doing cosine similarity with the WWF vectors. Do not assert on raw-sum >= 95%.

## Card layout

1. **Top**: pie/donut chart of raw biome percentages, labeled with raw values ("Ocean 13%, Grassland 7%, …"). Note somewhere on the card that the remaining % is "unclassified / mixed terrain".
2. **Middle**: closest **3 real-world ecoregions** to the normalized biome vector, with a short description of each. Use cosine similarity: `sim(a, b) = dot(a, b) / (norm(a) * norm(b))`.
3. **Bottom**: biodiversity drift strip — two sub-sections:
   - **Boom** (green): top 5 species by positive `(latest − first) / first` delta.
   - **Bust** (red): top 5 species by negative delta.

## Dead-end (do not attempt)

Rendering world biomes as spatial tiles. The per-layer endpoint only returns summary text, no grid pixels — verified dead in a prior spike. This tool works from percentages only.

## Implementation notes

- Cache species CSVs in-process for 60 s; 92-ish species × 6 KB = ~0.5 MB, re-fetch is cheap.
- Cache the `worldlayers/layers` response for 5 minutes — biome percentages don't change minute-to-minute.
- Bundle chart.js in the HTML; no CDN at runtime (CSP).

## Acceptance

- Biome donut chart renders with raw percentages and a "unclassified" slice filling the gap to 100%.
- Top-3 ecoregion matches are stable across consecutive calls (deterministic for same inputs).
- Drift strip renders `boom` and `bust` lists; on Day 3 both lists may have near-zero deltas — render "drift minimal so far" as a placeholder in that case.
- Unit tests verify normalization and cosine similarity using the committed `ecoregions.json`.
- `inv smoke` passes.
