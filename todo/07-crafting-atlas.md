# Task 7 — Crafting Activity Atlas

**Prereq**: read `todo/README.md` first.

## Goal

Build an MCP tool `get_eco_crafting_atlas` rendering a card of live crafting activity reconstructed from event logs.

## Data sources (admin, `X-API-Key` from SSM `/eco-mcp-app/api-admin-token`, region `us-east-1`)

- `GET http://eco.coilysiren.me:3001/api/v1/exporter/actionlist`
  - **Newline-delimited plain text, NOT JSON.** Parse via `resp.text.splitlines()`.
- `GET http://eco.coilysiren.me:3001/api/v1/exporter/actions?actionName=ItemCraftedAction`
  - CSV: `ActionLocation,WorldObjectItem,Citizen,ItemUsed,OverrideHierarchyActionsToConsumer,Count,Time`
  - **Size warning**: 295 KB on Day 3; will grow to 20+ MB by end-cycle. Must **stream-parse** (use `csv.reader` on the response iterator), aggregate in a single pass, and never `.text` the whole body into memory.
- Also pull: `ChopTree`, `HarvestOrHunt`, `DigOrMine`, `ConstructOrDeconstruct` for a full production picture. Same streaming discipline.

Before implementing: check whether `/api/v1/exporter/actions` accepts a date or time-range query param that would let the server filter. If it does, use it to cap input size. If it doesn't, stream-aggregate on the client.

## Analyses to render

1. **Top 20 items produced this cycle** — aggregate `Count` grouped by `ItemUsed` (the output item).
2. **Crafting station utilization** — count of events per `WorldObjectItem` (e.g. `CampfireItem`, `WorkbenchItem`, `CarpentryTableItem`). Rank hot → cold.
3. **"Flows into what" sankey** — edges from `WorldObjectItem → ItemUsed`, thickness = sum of `Count`. Use `d3-sankey` — **bundle it into the HTML, don't load from CDN at runtime** (CSP).
4. **Per-citizen leaderboard** — top 10 crafters by total `Count` across all craft-like action types.

## Dead-end (do not attempt)

Static parsing of mod C# source for recipe definitions. Recipes-as-definitions aren't exposed over HTTP. This tool is **observed events only** — which is strictly better because mod items (BunWulf, Nid, vanilla) all appear naturally where they're actually used.

## Implementation notes

- Consider pre-aggregating hourly and caching in a local SQLite `~/.cache/eco-mcp-app/crafting.sqlite` so repeat calls don't re-stream the full CSV. Invalidate on a 5-minute TTL.
- Add MCP tool to `src/eco_mcp_app/server.py`.
- Template under `src/eco_mcp_app/templates/partials/`.

## Acceptance

- Sankey renders without crossing edges more than 5 times for the current cycle.
- Leaderboard has ≥ 3 citizens (Day 3 should comfortably support this).
- Memory use stays under 200 MB even when fed a synthetic 20-MB CSV (stream test; use an in-memory fixture).
- `inv smoke` passes.
