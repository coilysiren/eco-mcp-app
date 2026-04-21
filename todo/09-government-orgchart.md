# Task 9 — Government Org-Chart Card

**Prereq**: read `todo/README.md` first.

## Goal

Build an MCP tool `get_eco_government` rendering a card showing the server's civic structure.

## Data sources (all public, no auth)

- `GET http://eco.coilysiren.me:3001/api/v1/elections/titles` — list of titles with occupants, grouped by settlement/state.
- `GET http://eco.coilysiren.me:3001/api/v1/elections` — active and inactive elections. **Verified empty `[]` on Day 3.** Code must handle this gracefully.
- `GET http://eco.coilysiren.me:3001/api/v1/laws?byStates=Active` — active laws (40 KB on current cycle).

### Titles shape — verify before committing schema

Each title entry has the form `{"Table": [[row, ...], ...]}`. There's no top-level `Occupant` field; the occupant (and things like "Election Process", "Eligible Candidates") are rows inside `Table` with a key/description/value triple. **Before designing the mermaid/graphviz graph, print one full title entry and confirm which row label holds the occupant's name** — it's likely a row whose label contains "Occupant" or "Current" or "Holder", but verify live before coding.

## Rendered card

- **Top**: settlement/federation name extracted from title scopes (e.g. `"Steamtide Cay Foundation"`).
- **Middle**: org chart — render with Mermaid or Graphviz (bundle the lib, don't load at runtime; CSP). Mayor → Governors → Citizens, arrows for succession/removal relationships, which the titles endpoint does encode once you find the right row.
- **Bottom**: active-election chips with an `ends-in-N-hours` countdown. **If `/api/v1/elections` is empty, render a "No active elections" placeholder — don't drop the section.**
- **Footer**: law count and shortest/longest law preview.

## Law markup sanitizer

Laws contain Eco markup tokens like:

- `<link="view:283:-1">...</link>`
- `<icon name="Claim" type="">...</icon>`
- `<color=#abcdef>...</color>`
- `<style="Positive">...</style>`

Strip with one regex: `re.sub(r"</?(link|icon|color|style)(\s[^>]*)?>", "", s)`. Do **not** attempt to render the inline icons — just remove them. Leave surrounding text intact.

## Implementation notes

- Add MCP tool to `src/eco_mcp_app/server.py`.
- Template under `src/eco_mcp_app/templates/partials/`.

## Acceptance

- Mayor title ("Steamtide Cay Foundation Mayor" or whatever scope is active) renders with the occupant name.
- Empty-elections case renders a "No active elections" placeholder, not a blank section.
- Active-election chip(s) render if/when elections exist (may be zero on Day 3).
- Law sanitizer handles all four markup token families without dropping surrounding text.
- `inv smoke` passes.
