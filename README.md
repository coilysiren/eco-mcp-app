# eco-mcp-app

An inline Claude Desktop widget for the **Eco via Sirens** game server [1]. Ask Claude
"what's the Eco server doing?" and you get a live card back: meteor countdown,
online/total players, plants and animals, world size, laws, economy, Discord CTA.
No screenshots, no tab-switching.

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
per-cycle setup (worldgen, Discord announcements, mod sync); `eco-agent` [7]
was an earlier FastAPI companion service for the same server; `eco-mods-public` [8]
is where the gameplay mods live. Server infrastructure is defined in
`infrastructure` [9] (k3s + pyinvoke + external-secrets + Traefik). Canonical
Eco references: ModKit [10], modding docs [11], Eco wiki modding page [12],
the Discord bridge plugin [13], and mod catalog [14].

## Install (local, Claude Desktop)

Claude Desktop only loads MCPs at startup, so install + restart:

```sh
cd /Users/kai/projects/eco-mcp-app
uv sync
python scripts/install-desktop-config.py
```

Then fully quit Claude Desktop (⌘Q) and relaunch. In a fresh chat:

> *Use eco-mcp-app to show me the Eco server status.*

You should get the meteor card inline.

## Deploy (homelab)

The long-term target is `eco-mcp.coilysiren.me` on the same k3s cluster that
already hosts `eco-agent`. Pattern is unchanged from `infrastructure` [9]:

- Build a Docker image (`Dockerfile` TODO)
- Manifests in `deploy/` (Deployment, Service, Ingress, TLS via cert-manager,
  ClusterIssuer already in the infra repo)
- No secrets needed — the `/info` endpoint is public; server runs without env vars

MCP-over-HTTP brings its own spec pitfalls (session-id splits and resource
registration scoping, tracked upstream in ext-apps#481), so the initial
deploy will likely be the same stdio binary wrapped as a Streamable-HTTP
server via the mcp SDK's HTTP transport — that's a later cycle's problem.

## Smoke test

The whole MCP → iframe → render flow is testable via stdio without Claude:

```sh
inv smoke
```

Look for: `_meta.ui.resourceUri` in both forms on `id=2`, a real-sized HTML
resource on `id=3`, and a JSON payload with `"view":"eco_status"` on `id=4`.

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

---

[1]: https://www.coilysiren.me/
[2]: https://modelcontextprotocol.io/docs/concepts/apps
[3]: https://github.com/modelcontextprotocol/ext-apps
[4]: https://play.eco/
[5]: https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
[6]: https://github.com/coilysiren/eco-cycle-prep
[7]: https://github.com/coilysiren/eco-agent
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
