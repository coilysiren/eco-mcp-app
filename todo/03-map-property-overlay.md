# Task 3 — Map + Property Overlay

**Prereq**: read `todo/README.md` first for cross-cutting conventions.

## Goal

Build an MCP tool `get_eco_map` that renders the live Eco world with property deed boundaries overlaid.

## Data sources (all public, no admin auth)

- `GET http://eco.coilysiren.me:3001/Layers/WorldPreview.gif` — ~50 KB animated GIF, canonical worldgen preview.
- `GET http://eco.coilysiren.me:3001/api/v1/map/property` → dict of `"<deed name>, Owner: <name>": [{x,y},...]`. Verified shape; some deeds have empty arrays (e.g. "yourock17's Small Wood Cart Deed").
- `GET http://eco.coilysiren.me:3001/api/v1/map/dimension` → `{"x":720,"y":200,"z":720}`.

### IMPORTANT: coordinate system

- **The map is 720 × 720 in the x/z plane**. `dimension.y = 200` is the **elevation** range, not a map extent. Do not use `y` as a map axis.
- The `property` endpoint's `{x,y}` pairs are actually `{x, z}` — Eco's 2D map projection names the vertical screen axis `y` even though it's the world's `z`. Treat `dimension.x` as x-extent and `dimension.z` as the analog of the property payload's `y`.
- **Coordinates wrap at the world edge** (toroidal). Observed real data: [Gavin]'s homestead polygon contains `{x:705, y:175}` and also `{x:0, y:195}` — that's a polygon crossing the x=720→0 seam. When consecutive verts differ by more than `dimension/2`, split the polygon at the seam and render two polygons (one on each side) so Pillow/SVG don't draw a straight line across the entire map.

## Implementation

1. Fetch the WorldPreview.gif first frame with Pillow.
2. Scale property polygons by `(image_width/720, image_height/720)`.
3. Convex-hull or order-vertices-by-polar-angle each deed's vert list before drawing (the endpoint returns verts as a set-like list with no winding order).
4. Handle the seam-crossing case described above.
5. Assign each owner a color from `HSL(hash(owner_name) % 360, 50%, 50%)` at 40% alpha.
6. Draw deed polygons onto the GIF frame with Pillow.
7. Inline the composed PNG as a `data:` URI in the iframe (CSP blocks external origins, per `claude-ai-mcp#40`).
8. Overlay an SVG with absolute-positioned `<polygon>`s matching the PNG coords so hover shows owner name + deed name as a tooltip.

## Dead-end (do not attempt)

Per-biome spatial rendering from `/api/v1/worldlayers/layers/{name}` — those return **summary text only** (`"Summary": "4%"`), not grid pixels. Verified dead in a prior spike. If you want biome composition, see task #10 (ecoregion task), which uses the percentages directly without pretending they're tiles.

## Acceptance

- Card shows a recognizable world map with at least 6 deeds highlighted (live data has 12+ deeds today).
- Seam-crossing deeds render as two polygons on the correct sides (test against [Gavin]'s deed specifically).
- Hover on any polygon shows the owner name.
- `inv smoke` passes with the new tool registered.
