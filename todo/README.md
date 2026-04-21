# todo/ — worktree task backlog

Each markdown file in this folder is a self-contained task spec for one worktree session. Open a new Claude session pointed at one of them and it should have everything needed to execute.

**Deferred (do not start)**: task #1 (`01-grafana-prometheus-exporter.md`). Spec exists for reference, but the homelab doesn't have a usable Grafana/Prometheus stack yet. A fresh session pointed at `01-*.md` should stop and flag it as on hold.

## Files

| # | File | MCP tool produced | Status |
|---|---|---|---|
| 1 | `01-grafana-prometheus-exporter.md` | `get_grafana_snapshot` | **deferred** |
| 2 | `02-economic-dashboard.md` | `get_eco_economy` | ready |
| 3 | `03-map-property-overlay.md` | `get_eco_map` | ready |
| 4 | `04-milestone-tracker.md` | `get_eco_milestones` | ready |
| 5 | `05-species-profile.md` | `get_eco_species(name)` | ready |
| 6 | `06-wikidata-ecopedia.md` | `explain_eco_item(name, category?)` | ready |
| 7 | `07-crafting-atlas.md` | `get_eco_crafting_atlas` | ready |
| 8 | `08-fair-price-advisor.md` | `fair_price(item)` | ready |
| 9 | `09-government-orgchart.md` | `get_eco_government` | ready |
| 10 | `10-ecoregion-biodiversity.md` | `get_eco_ecoregion` | ready |

## Cross-cutting concerns (apply to every task)

### Project conventions — read first

- `CLAUDE.md` is authoritative for layout and dev loop. **Caveat**: CLAUDE.md says `src/eco_mcp_app/ui/eco.html`, but actual path is `src/eco_mcp_app/templates/eco.html` with partials in `src/eco_mcp_app/templates/partials/`. Use the real path.
- MCP tool registration: add to `src/eco_mcp_app/server.py`.
- HTTP transport: `src/eco_mcp_app/http_app.py` (port 4000, endpoint `POST /mcp/`).
- Dev loop: `inv http` (HTTP server), `inv smoke` (stdio smoke test), `inv harness` (browser iframe harness at `http://localhost:8765/static/harness.html`).
- Tests use `respx` for HTTP mocking — follow the pattern from the sibling `eco-spec-tracker` repo.
- Commit directly to the worktree branch when the work feels complete. Don't open PRs unless asked.

### AWS / SSM

- **All 53 eco-mcp-app SSM params are in `us-east-1`**. AWS CLI default is `us-west-2` and will return `ParameterNotFound` silently. Pin the region explicitly in every call:
  - Python (boto3): `boto3.client("ssm", region_name="us-east-1")`
  - Shell: `aws ssm get-parameter --name /x --region us-east-1 --with-decryption`
- The two secrets most likely to matter:
  - `/eco-mcp-app/api-admin-token` — SecureString, goes in `X-API-Key` header for Eco admin endpoints
  - `/eco-mcp-app/fred-api-key` — SecureString, query-string `api_key=` for FRED
- Fetch at app-start, not per-request. Cache in-process.

### Eco server (live reference)

- Base URL: `http://eco.coilysiren.me:3001`
- Public endpoints verified on Day 3 of Cycle 13 (current cycle):
  - `GET /info` → 200 (3 KB)
  - `GET /datasets/flatlist` → 200 (81 KB) — **205 stat definitions**, each with `Name`, `DisplayName`, `Unit`, `ValueKey` fields. `TotalCulture` is **not** a dataset; it's a top-level field on `/info`.
  - `GET /datasets/get?dataset={Name}&dayStart=0&dayEnd={n}` → 200 when Name is in flatlist, else **500 `"No stat named X was found"`**.
  - `GET /api/v1/worldlayers/layers` → 200 (85 KB) — list of 7 categories, each `{Category, List:[{LayerName, LayerDisplayName, Summary, Tooltip, Category, DisplayRow}]}`.
  - `GET /api/v1/map/property` → 200 (~20 KB) — dict `"<deed name>, Owner: <name>": [{x,y},...]`.
  - `GET /api/v1/map/dimension` → 200 `{"x":720,"y":200,"z":720}`. **Note**: `y` is elevation (0–200); map is 720×720 in x/z.
  - `GET /api/v1/elections/titles` → 200 (~4 KB)
  - `GET /api/v1/elections` → 200 **`[]` empty on Day 3** — handle gracefully
  - `GET /api/v1/laws?byStates=Active` → 200 (40 KB) — values contain Eco markup tokens `<link=...>`, `<icon name=... type=...>`, `<color=...>`, `<style=...>` — strip with regex.
  - `GET /Layers/WorldPreview.gif` → 200 (50 KB animated GIF, the canonical world preview).

- Admin endpoints (require `X-API-Key` header — value from SSM):
  - `GET /api/v1/users?hoursPlayedGte=0` → 200 (7 KB)
  - `GET /api/v1/exporter/specieslist` → 200 — **newline-delimited plain text**, NOT JSON. Parse via `resp.text.splitlines()`.
  - `GET /api/v1/exporter/species?speciesName=X` → 200 CSV `"Time","Value"\n"1","137"\n"600","137"\n...`. `Time` is **seconds since cycle start** at 600s (10-min) sample cadence.
  - `GET /api/v1/exporter/actionlist` → 200 — **newline-delimited plain text**, same shape as specieslist.
  - `GET /api/v1/exporter/actions?actionName=X` → 200 CSV. `ItemCraftedAction` was **295 KB on Day 3** — will grow to many MB by end-cycle. Stream-parse, don't load whole thing into memory for late-cycle analyses.

### Iframe / MCP Apps host gotchas

- Claude Desktop's CSP blocks external image origins (see `claude-ai-mcp#40`). Any image the iframe renders must be **inlined as a data URI** — that's why `src/eco_mcp_app/templates/eco.html` does this with the Steam banner.
- Dual `_meta.ui.resourceUri` forms — see `claude-ai-mcp#71`. The existing card already handles this; mirror the pattern.
- Handshake: hand-rolled in `templates/eco.html`, no bundler. Don't introduce one.
- Chart.js, d3, mermaid, etc. — inline from a CDN or bundle into the HTML; don't link to a CDN at runtime (CSP).

### Data thinness — it's Day 3 of Cycle 13

- The server is early in its cycle. Elections, contracts, loans, crafting events are sparse.
- Build **robust empty states**: "no elections active", "no crafting events recorded", etc. — not crash-on-zero.
- Acceptance criteria in task files account for this; don't over-index on late-cycle assumptions.

### Testing

- stdio smoke test: `inv smoke` — every new tool should smoke-test clean (initialize → list tools → read resource → call tool).
- HTTP transport smoke test: `inv http` then `curl -X POST http://localhost:4000/mcp/ ...`.
- Iframe visual: `inv harness` and load in browser.
- Tests: add unit tests using `respx`. Mirror the eco-spec-tracker patterns (sibling repo).

### Caching

- Default cache dir: `~/.cache/eco-mcp-app/`. SQLite files per external service (e.g. `inat.sqlite`, `wikidata.sqlite`, `fred.sqlite`).
- TTLs: stable external facts = 7d; volatile live Eco data = per-tool decision (often no cache, just honor 30–60s cadence).

### Key references

- MCP Apps spec (2026-01-26): https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
- Eco `/info` live: `http://eco.coilysiren.me:3001/info`
- Upstream issues to know about: `claude-ai-mcp#71` (dual `_meta.ui.resourceUri`), `claude-ai-mcp#61` (handshake causes), `claude-ai-mcp#40` (CSP), `claude-ai-mcp#69` (size-changed vs documentElement).
