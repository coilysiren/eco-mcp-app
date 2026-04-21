# Task 4 — Milestone / Achievement Tracker

**Prereq**: read `todo/README.md` first.

## Goal

Build an MCP tool `get_eco_milestones` rendering an iframe card of server achievement progress.

## Data source

`GET http://eco.coilysiren.me:3001/info` (public, no auth).

The field `ServerAchievementsDict` is an object mapping achievement name → an HTML-ish string, e.g.:

```
"Create 250 total culture as a world.\n<style=\"Culture\"><icon name=\"Culture\" type=\"nobg\"></icon>57.6 Culture</style> from <style=\"Positive\">2</style> works from <style=\"Positive\">1</style> artists."
```

There are ~5 achievements on the current cycle. `/info` also exposes `TotalCulture` as a top-level number (use this directly; do not re-derive from the achievement string).

## Task

1. For each achievement, parse the value to extract:
   - **Target** (first integer in the sentence, e.g. `250`).
   - **Current** (first numeric in the markup block — may be decimal like `57.6`).
2. Strip Eco's inline markup: `<style=...>`, `<icon name=... type=...>`, `<color=...>`, closing `</style>`, `</icon>`, `</color>`. Use a regex pass: `re.sub(r"</?(style|icon|color)(\s[^>]*)?>", "", s)`.
3. Render a ladder of progress bars **sorted by completion % descending** (closest to target at top). Format: `"Cultural Awakening: 57.6 / 250 Culture (23%)"`.
4. Expose `TotalCulture` from `/info` as a top-line stat above the ladder.

## Implementation notes

- Pure HTML + CSS progress bars; no charting lib needed.
- Add MCP tool to `src/eco_mcp_app/server.py`.
- Template lives under `src/eco_mcp_app/templates/partials/` (note: `templates/`, per README).

## Out of scope

- Rendering `TotalCulture` inside this card **and** inside task 2's Economic Dashboard card. Pick one top-line location. If task 2 is already live, suppress it here (read `templates/partials/*.html` first to check).

## Acceptance

- Parser handles all ~5 achievements in the current `/info` payload without crashing on any string.
- Progress bars render in descending closeness-to-target.
- `inv smoke` passes.
