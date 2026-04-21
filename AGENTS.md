## File Access

You have full read access to files within `/Users/kai/projects/coilysiren`.

## Autonomy

- Run tests after every change without asking.
- Fix lint errors automatically.
- If tests fail, debug and fix without asking.
- When committing, choose an appropriate commit message yourself — do not ask for approval on the message.
- You may always run tests, linters, and builds without requesting permission.
- Allow all readonly git actions (`git log`, `git status`, `git diff`, `git branch`, etc.) without asking.
- Allow `cd` into any `/Users/kai/projects/coilysiren` folder without asking.
- Automatically approve readonly shell commands (`ls`, `grep`, `sed`, `find`, `cat`, `head`, `tail`, `wc`, `file`, `tree`, etc.) without asking.
- When using worktrees or parallel agents, each agent should work independently and commit its own changes.
- Do not open pull requests unless explicitly asked.

## Git workflow

Commit directly to `main` without asking for confirmation, including `git add`. Do not open pull requests unless explicitly asked.

Commit whenever a unit of work feels sufficiently complete — after fixing a bug, adding a feature, passing tests, or reaching any other natural stopping point. Don't wait for the user to ask.

## Project layout

- `src/eco_mcp_app/server.py` — MCP server (stdio, low-level SDK). One tool: `get_eco_server_status`.
- `src/eco_mcp_app/ui/eco.html` — the iframe rendered by MCP Apps hosts; hand-rolled handshake, no bundler.
- `scripts/install-desktop-config.py` — registers this server in Claude Desktop's config.
- `static/harness.html` — browser-based MCP Apps host simulator for iterating on the iframe without restarting Claude Desktop. Also wired into `.claude/launch.json` as the `eco-harness` preview.
- `tasks.py` — `inv smoke`, `inv harness`, `inv ruff`, `inv fmt`, `inv install-desktop`.
- `investigation/` — chronological post-mortem of the debugging session that produced this repo. Read these before questioning a decision that looks weird.

## Dev loop

- `uv sync --group dev` — install runtime + dev deps.
- `pre-commit install` (once) — ruff + mypy run on every `git commit`.
- `inv smoke` — stdio smoke test: initialize → list tools → read resource → call tool.
- `inv harness` — serve the dev harness at `http://localhost:8765/static/harness.html` for iframe work.
- `inv ruff` / `inv fmt` — lint/format check vs apply.

## Sibling Eco repos

This project depends on the user's Eco (Strange Loop Games) repo ecosystem, which live as siblings under `/Users/kai/projects/coilysiren` on Mac. Read from them directly rather than asking the user for Eco domain details.

| Dir | Visibility | Purpose |
|---|---|---|
| `eco-agent` | public | Python/FastAPI service (Discord + OpenTelemetry + AWS SSM), deployed to eco.coilysiren.me. Shares its k3s target with the eventual HTTP deployment of this repo. |
| `eco-cycle-prep` | public | Per-cycle setup (worldgen, Discord announcements, mod sync). Pyinvoke-driven, same pattern as this repo's `tasks.py`. |
| `eco-mods` | private | Third-party mods installed on the user's private Eco server + configs. C#. |
| `eco-mods-public` | public | User's own C# mods (BunWulf family + others). |
| `eco-configs` | private | Server config diffs. |
| `infrastructure` | public | k3s + pyinvoke + external-secrets + Traefik. Canonical deploy pattern for the homelab; this repo will follow it when the HTTP transport lands. |

## Key references

- MCP Apps spec (2026-01-26): https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
- Eco `/info` endpoint: live at `http://eco.coilysiren.me:3001/info`.
- Relevant upstream issues: `claude-ai-mcp#71` (dual `_meta.ui.resourceUri` forms), `claude-ai-mcp#61` (handshake causes), `claude-ai-mcp#40` (CSP), `claude-ai-mcp#69` (size-changed vs documentElement).
