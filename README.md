# eco-mcp-app

An inline Claude Desktop widget for any public **Eco** game server [4] — point it
at the "Eco via Sirens" [1] server (the default) or any other Eco server by IP
or hostname. Ask Claude "what's the Eco server doing?" and you get a live card
back: meteor countdown, online/total players, world size, laws, economy,
Discord CTA, a link to Eco on Steam. No screenshots, no tab-switching. Cards
you don't have data for just aren't rendered.

It's also a tech demo — a minimal, hand-rolled MCP Apps implementation [2]
without a bundler or React, so the whole iframe is one 300-line HTML file.
Useful as a reference for anyone else building an MCP App in Python rather
than the default TypeScript/ext-apps [3] stack.

![](https://img.shields.io/badge/python-3.13-3776ab)
![](https://img.shields.io/badge/mcp-1.14+-ff6b35)
![](https://img.shields.io/badge/MCP_Apps-spec_2026--01--26-7cb342)

## What it renders

```
┌─ Eco via Sirens ─────────── Established · day 2 · HighCollaboration · Slow ─ ● online ─┐
│                                                                                       │
│  DAYS UNTIL METEOR ☄                                          ┌─────┐                  │
│  57 days                                                      │ 57  │  (cycle ring,   │
│  Server running for 2 days · 5% through the cycle             │ left│  fills as days  │
│                                                               └─────┘   tick down)    │
│                                                                                       │
│  ┌ Players online ┐ ┌ World       ┐ ┌ Cycle progress  ┐ ┌ Economy & culture ┐        │
│  │ 7 / 67         │ │ 0.52 km²    │ │ day 2           │ │ 473 trades,       │        │
│  │ peak 38        │ │ 96k plants  │ │ 57d until ☄     │ │   0 contracts     │        │
│  │ ░░░░█░░░░░░░░░ │ │ 0 animals   │ │ ██░░░░░░░░░░░░░ │ │ 171.0 culture     │        │
│  └────────────────┘ └─────────────┘ └─────────────────┘ └───────────────────┘        │
│                                                                                       │
│  [v 0.13.0.2] [English] [open] [admin online]         Fetched 4:12 PM · [Join Discord]│
└───────────────────────────────────────────────────────────────────────────────────────┘

          · · · .        ·     .                 . ·
     .        ·   .    *   .          ·   . (animated starfield, twinkling)
       *              .         *                 ·
                                                         ☄ (meteor, floats)
                                                       ↙
                                                     ↙
```

## How it works

The server (`src/eco_mcp_app/server.py`) exposes one tool,
`get_eco_server_status`, which hits `http://eco.coilysiren.me:3001/info` (the
public `/info` endpoint Eco [4] servers expose by default), redacts player
names, and returns two content blocks: a markdown fallback for text-only
hosts, and a JSON payload for the iframe. The tool's `_meta.ui.resourceUri`
points at `ui://eco/status.html`, which is the iframe HTML registered as a
resource.

The iframe (`src/eco_mcp_app/ui/eco.html`) is plain HTML/CSS/JS — no build
step, no bundler, no React. It hand-rolls the MCP Apps initialization
handshake per the spec [5]:

1. Iframe → host: `ui/initialize` (request, with `protocolVersion: 2026-01-26`)
2. Host → iframe: initialize result
3. Iframe → host: `ui/notifications/initialized` (notification)
4. Host → iframe: `ui/notifications/tool-result` whenever a matching tool fires

The handshake is ~30 lines. The ext-apps SDK [3] does more (auto-resize,
capability negotiation), but for a read-only dashboard we don't need any of
it — and writing it out makes the spec readable.

## See also

This repo sits next to a small Eco ecosystem: `eco-cycle-prep` [6] runs
per-cycle setup (worldgen, Discord announcements, mod sync); `eco-mods-public` [8]
is where the gameplay mods live. The deploy pattern (Dockerfile, Makefile,
k8s manifest, GH Actions) is cloned from `coilysiren/backend` [7], which is
the canonical template for the homelab k3s + GHCR + Tailscale + cert-manager
stack. Canonical Eco references: ModKit [10], modding docs [11], Eco wiki
modding page [12], the Discord bridge plugin [13], and mod catalog [14].

## Install (local, Claude Desktop)

Claude Desktop only loads MCPs at startup, so install + restart:

```sh
cd /Users/kai/projects/coilysiren/eco-mcp-app
uv sync
python scripts/install-desktop-config.py
```

Then fully quit Claude Desktop (⌘Q) and relaunch. In a fresh chat:

> *Use eco-mcp-app to show me the Eco server status.*

You should get the meteor card inline.

## Deploy (homelab)

Target: `eco-mcp.coilysiren.me` on the k3s cluster, following the template in
`coilysiren/backend` [7] (same Dockerfile/Makefile/deploy shape). The server
speaks MCP over Streamable-HTTP at `/mcp/` via `src/eco_mcp_app/http_app.py`
(Starlette + `StreamableHTTPSessionManager` in stateless mode). Health probe
at `/healthz`.

Pipeline: `.github/workflows/build-and-publish.yml` builds the image and
pushes to `ghcr.io/coilysiren/eco-mcp-app/...` on every push to `main`, then a
second job uses Tailscale to reach the cluster and applies `deploy/main.yml`
via `make .deploy`. The manifest is self-bootstrapping — the `Namespace` lives
at the top of `deploy/main.yml` so the first deploy creates it alongside the
Deployment / Service / Ingress in a single `kubectl apply`. No manual cluster
prep needed.

After the first `git push` publishes the image to GHCR, make the package
public at
<https://github.com/users/coilysiren/packages/container/eco-mcp-app%2Fcoilysiren-eco-mcp-app/settings>
(Package settings → Change visibility → Public). Packages inherit from the
repo but only on first push; they default to private. With a public image, no
`imagePullSecrets` is needed. If you flip the package back to private later,
run `make deploy-secrets-docker-repo` once (pulls the GHCR PAT from
`aws ssm /github/pat` without reading it into your shell) and add the pull-secret
line back to `deploy/main.yml`.

Runtime has no secrets — the `/info` endpoint of the upstream Eco server is
public, and the tool accepts the target server as an argument so a single
deployment can query any public Eco server.

## Smoke test

The whole MCP → iframe → render flow is testable via stdio without Claude:

```sh
inv smoke
```

Look for: `_meta.ui.resourceUri` in both forms on `id=2`, a real-sized HTML
resource on `id=3`, and a JSON payload with `"view":"eco_status"` on `id=4`.

## Dev harness (iterate on the iframe without restarting Claude)

`static/harness.html` is a minimal HTML page that mimics Claude Desktop's MCP Apps
host so the iframe can be developed in a normal browser — no ⌘Q / relaunch
cycle per change. The harness:

1. Loads `src/eco_mcp_app/ui/eco.html` as an iframe (`visibility: hidden`).
2. Listens for `ui/initialize` from the iframe and responds with a valid
   `McpUiInitializeResult` (protocolVersion, hostInfo, hostCapabilities,
   hostContext).
3. On `ui/notifications/initialized`, reveals the iframe.
4. Listens for `ui/notifications/size-changed` and applies the reported
   `{width, height}` to `iframe.style.height`. This is the mechanism Claude
   Desktop actually uses — not the `documentElement.height` read that
   [claude-ai-mcp#69](https://github.com/anthropics/claude-ai-mcp/issues/69)
   describes.
5. After reveal, pushes a canned `ui/notifications/tool-result` with a mock
   Eco `/info` payload so `render()` runs.

Run it with:

```sh
inv harness
# then open http://localhost:8765/static/harness.html
```

The status bar at the top of the harness shows the last `size-changed` value
so you can see whether the iframe is telling the host to resize. If it says
"Loading…" forever, either the handshake failed or the iframe's script threw
before reaching `connect()` — check DevTools console.

The harness is also usable from Claude Code's Preview panel via the
`eco-harness` entry in `.claude/launch.json`.

## MCP Apps — non-obvious things I learned building this

- `_meta.ui.resourceUri` must be set in **both** nested (`ui.resourceUri`) and
  flat (`ui/resourceUri`) forms — some hosts only honor one [15].
- The MIME type has to be exactly `text/html;profile=mcp-app`; plain
  `text/html` does not trigger MCP Apps rendering.
- With no client-side JS running the handshake, Claude Desktop correctly
  leaves the iframe container at `visibility: hidden`. This means a no-script
  test HTML is not a valid isolation — it will look identical to a broken
  app [16].
- Claude Desktop's sandbox iframe enforces a hardcoded CSP that ignores
  `_meta.ui.csp` extensions [17]. External image origins get blocked. If you
  need thumbnails, inline them server-side as `data:image/...;base64,...`
  URIs — those are always permitted.
- Only Claude Desktop chat UI (`clientInfo.name = "claude-ai"`) advertises
  the `io.modelcontextprotocol/ui` extension capability. Claude Code
  Desktop's agent harness (`clientInfo.name = "local-agent-mode-*"`) does
  not, so iframes never render there — use its Launch preview panel
  (triggered by a `Write` or `Edit` tool call on a local HTML file) as the
  fallback inline-visualization path.

## License

MIT.

## References

1. <https://www.coilysiren.me/>
2. <https://modelcontextprotocol.io/docs/concepts/apps>
3. <https://github.com/modelcontextprotocol/ext-apps>
4. <https://play.eco/>
5. <https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx>
6. <https://github.com/coilysiren/eco-cycle-prep>
7. <https://github.com/coilysiren/backend>
8. <https://github.com/coilysiren/eco-mods-public>
9. <https://github.com/coilysiren/infrastructure>
10. <https://github.com/StrangeLoopGames/EcoModKit>
11. <https://docs.play.eco/>
12. <https://wiki.play.eco/en/Modding>
13. <https://github.com/Eco-DiscordLink/EcoDiscordPlugin>
14. <https://mod.io/g/eco>
15. <https://github.com/anthropics/claude-ai-mcp/issues/71>
16. <https://github.com/anthropics/claude-ai-mcp/issues/61#issuecomment-4283640203>
17. <https://github.com/anthropics/claude-ai-mcp/issues/40>

<!-- reference definitions below are invisible in rendered Markdown;
     they make the [N] tokens in the body clickable without a second visible list. -->

[1]: https://www.coilysiren.me/
[2]: https://modelcontextprotocol.io/docs/concepts/apps
[3]: https://github.com/modelcontextprotocol/ext-apps
[4]: https://play.eco/
[5]: https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
[6]: https://github.com/coilysiren/eco-cycle-prep
[7]: https://github.com/coilysiren/backend
[8]: https://github.com/coilysiren/eco-mods-public
[9]: https://github.com/coilysiren/infrastructure
[10]: https://github.com/StrangeLoopGames/EcoModKit
[11]: https://docs.play.eco/
[12]: https://wiki.play.eco/en/Modding
[13]: https://github.com/Eco-DiscordLink/EcoDiscordPlugin
[14]: https://mod.io/g/eco
[15]: https://github.com/anthropics/claude-ai-mcp/issues/71
[16]: https://github.com/anthropics/claude-ai-mcp/issues/61#issuecomment-4283640203
[17]: https://github.com/anthropics/claude-ai-mcp/issues/40
