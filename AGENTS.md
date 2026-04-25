# Agent instructions

See `../AGENTS.md` for workspace-level conventions (git workflow, test/lint autonomy, readonly ops, writing voice, deploy knowledge). This file covers only what's specific to this repo.

---

## Project layout

- `src/eco_mcp_app/server.py` - transport-agnostic MCP server. One tool: `get_eco_server_status` with optional `server` arg (host, host:port, or full URL - bare IPs are common for public Eco servers).
- `src/eco_mcp_app/__main__.py` - stdio entry point for Claude Desktop.
- `src/eco_mcp_app/http_app.py` - Starlette ASGI app wrapping the same MCP server via `StreamableHTTPSessionManager` (stateless). Routes: `/`, `/healthz`, `/mcp/`. Used by the homelab deploy.
- `src/eco_mcp_app/ui/eco.html` - the iframe rendered by MCP Apps hosts; hand-rolled handshake, no bundler. Eco's Steam banner is inlined as a data URI (external image origins are blocked by Claude Desktop's CSP per `claude-ai-mcp#40`).
- `scripts/install-desktop-config.py` - registers this server in Claude Desktop's config.
- `static/harness.html` - browser-based MCP Apps host simulator for iterating on the iframe without restarting Claude Desktop. Also wired into `.claude/launch.json` as the `eco-harness` preview.
- `tasks.py` - `inv smoke`, `inv http`, `inv harness`, `inv ruff`, `inv fmt`, `inv precommit`, `inv install-desktop`.
- `Dockerfile` / `Makefile` / `config.yml` / `deploy/main.yml` / `.github/workflows/build-and-publish.yml` - homelab deploy rig, cloned from `coilysiren/backend`.
- `investigation/` - chronological post-mortem of the debugging session that produced this repo. Read these before questioning a decision that looks weird.

## Dev loop

- `uv sync --group dev` - install runtime + dev deps.
- `pre-commit install` (once) - ruff + mypy run on every `git commit`.
- `inv smoke` - stdio smoke test: initialize → list tools → read resource → call tool.
- `inv http` - run the HTTP transport locally on `:4000`. Endpoint: `POST /mcp/`.
- `inv harness` - serve the dev harness at `http://localhost:8765/static/harness.html` for iframe work.
- `inv ruff` / `inv fmt` - lint/format check vs apply.
- `make build-docker` / `make deploy` - build/push the image and roll out to k3s (needs kubectl + AWS SSM access for bootstrap).

After each commit to `main`, run the test suite (or confirm it was just run). If tests pass, `git push` immediately. If tests fail, fix them before pushing.

## Sibling Eco repos

This project depends on the user's Eco (Strange Loop Games) repo ecosystem, which live as siblings under `/Users/kai/projects/coilysiren` on Mac. Read from them directly rather than asking the user for Eco domain details.

| Dir | Visibility | Purpose |
|---|---|---|
| `backend` | public | The canonical deploy template for k3s + GHCR + Tailscale + cert-manager. `Dockerfile` / `Makefile` / `deploy/main.yml` / `.github/workflows/build-and-publish.yml` in this repo were cloned from there. |
| `kai-server` | public | Claude-driver repo for the homelab k3s box (`ssh kai@kai-server`, Tailscale `100.69.164.66`). Documents the readonly kubectl wrapper, the GH Actions → cluster path, the secrets/external-secrets + cert-manager setup, and why Claude can't run write-kubectl directly. Read this before troubleshooting a deploy. |
| `eco-cycle-prep` | public | Per-cycle setup (worldgen, Discord announcements, mod sync). Pyinvoke-driven, same pattern as this repo's `tasks.py`. |
| `eco-mods` | private | Third-party mods installed on the user's private Eco server + configs. C#. |
| `eco-mods-public` | public | User's own C# mods (BunWulf family + others). |
| `eco-configs` | private | Server config diffs. |
| `infrastructure` | public | k3s + pyinvoke + external-secrets + Traefik. Low-level homelab cluster config; reference for how the cluster itself is wired. |

## Key references

- MCP Apps spec (2026-01-26): https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
- Eco `/info` endpoint: live at `http://eco.coilysiren.me:3001/info`.
- Relevant upstream issues: `claude-ai-mcp#71` (dual `_meta.ui.resourceUri` forms), `claude-ai-mcp#61` (handshake causes), `claude-ai-mcp#40` (CSP), `claude-ai-mcp#69` (size-changed vs documentElement).

## Adversarial testing

When asked to harden this service, run it through gauntlet against `https://eco-mcp.coilysiren.me/` following `../gauntlet/docs/hardening-prompt-template.md`. The worked example at the bottom of that template is the filled-in version for this repo; replay it as-is. Authorized: Kai owns both repos.
