# GitHub issues digest — anthropics/claude-ai-mcp

Saved: 2026-04-20 ~15:12 PT. Full JSON dumps at `/tmp/issue{40,47,61,69,71,102,142,149,165}.json`.

## The bullseye match: #165

[#165](https://github.com/anthropics/claude-ai-mcp/issues/165) (OPEN, updated 2026-04-20 18:50Z)
— "MCP Apps UI never renders in Claude Desktop — iframe handshake not initiated after tool call"

Reporter setup:
- ext-apps v1.1.2, sdk v1.29.0, stdio, macOS, Claude Desktop 1.1617.0 (2026-04-09)
- Tool registered with `_meta.ui.resourceUri = "ui://test/mcp-app.html"`
- Resource registered, handshake never fires: tool/call OK, resources/read OK, then silence
- No iframe ever created. `app.connect()` hangs waiting for `ui/initialize` response

Only comment: @dylanb "Also seeing with HTTP too" → @ochafik asks for minimal repro,
points at canonical examples https://example-server.modelcontextprotocol.io/.

## The critical Rosetta-stone comment: #61 @ochafik 2026-04-20 19:20Z

[#61 comment](https://github.com/anthropics/claude-ai-mcp/issues/61#issuecomment-4283640203)
breaks apart three causes that all look like the same symptom:

### Cause A — Missing `ui/initialize` handshake (spec compliance)

> Claude Desktop keeps the app container at `visibility: hidden` until the View
> completes the initialization handshake: the iframe must send a `ui/initialize`
> request, await the response, then send `ui/notifications/initialized`. Only
> then does the host reveal the iframe and start delivering tool-input/tool-result.

**Fix (recommended):** bundle `@modelcontextprotocol/ext-apps` + `app.connect()`.
**Manual:** send `{jsonrpc:"2.0",id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",capabilities:{}}}`,
await response, then `{jsonrpc:"2.0",method:"ui/notifications/initialized"}`,
*then* make tool calls.

Separate tip: no-script test HTML **cannot** rule out the handshake — with no
script, no handshake, container correctly stays hidden. Has to use the real app.

### Cause B — `claudemcpcontent.com` unreachable popup

Apex domain `claudemcpcontent.com` has no A record (by design). Only
`*.claudemcpcontent.com` wildcards resolve. `dig +short claudemcpcontent.com A`
correctly returns empty; `dig +short test.claudemcpcontent.com A` returns `160.79.104.10`.

If a user sees "Check that claudemcpcontent.com is not blocked" their DNS is
blocking the wildcard subdomain (corporate proxy / pi-hole).

### Cause C — stdio works, HTTP doesn't (separate sessions bug)

Claude Desktop opens two MCP sessions against HTTP servers — one for Model,
one for AppBridge. Each has its own `Mcp-Session-Id`. If the server scopes
resource registration per-session, AppBridge's `resources/read ui://...` lands
on a session that never saw it. Workaround: register UI resources statelessly.

Tracked in upstream ext-apps#481.

### Cause D (from @shickle, same thread, not yet triaged by ochafik)

@shickle's report on 2026-04-20 07:42Z is much closer to Kai's stack:
- ext-apps v1.6.0, stdio, macOS (Apple Silicon), Node v20.19.2
- Tool has `_meta.ui.resourceUri`, resource mime `text/html;profile=mcp-app`
- Log shows clean: initialize with `extensions.io.modelcontextprotocol/ui`,
  tools/call, resources/read (~705KB). **No ui/notifications/* after** — nothing.
- Empty iframe container with `</>` code icon, no content painted, text fallback below
- A 7-line no-script HTML shows the same symptom

@ochafik's reply to @shickle: the no-script test is not a valid isolation
(no handshake → correctly hidden). Asks to open DevTools, find the inner
iframe at `*.claudemcpcontent.com`, and share console: does `app.connect()`
run? Does `ui/initialize` get a response?

## Other sibling issues (less relevant but saved)

- **#47** — Claude.ai injects non-JSON-RPC `{type, token, payload}` into
  postMessage stream, iframe unmounts. Desktop untested.
- **#69** — Claude.ai ignores `ui/notifications/size-changed`, reads DOM
  height directly. Workaround: set `document.documentElement.style.height`.
- **#71** — Claude.ai allegedly only recognizes flat `_meta["ui/resourceUri"]`,
  not nested `_meta.ui.resourceUri`. @localden (COLLABORATOR) says "tested and
  both work" 2026-03-04 — possibly stale report. the sibling server already sets
  BOTH forms defensively (server.ts L49-52).
- **#102** — `ui/update-model-context` silently dropped in Claude web. Not our bug.
- **#40** — Claude.ai ignores `_meta.ui.csp.frameDomains` / `connectDomains` /
  `resourceDomains`, hardcoded `frame-src 'self' blob: data:`. @antonpk1 (2026-04-09):
  "can't allow nested iframes on claude.ai at the moment (security concerns)."
- **#142** — Feature req for `mcp://ui/` inline citation links.
- **#149** (CLOSED) — HTTP-transport-only variant of #165. Self-resolved by
  @nearestnabors: *"Don't make tool calls during initialization. Set up
  ontoolresult handler before connect(). Wait for host to push the tool result
  to your app."* MCP Apps are push, not pull.

## Key takeaways for our case

1. **Test MUST be the real app**, not a no-script HTML. The container correctly
   stays hidden when there's no client-side handshake.
2. **Stack for @shickle is closest to Kai's** (ext-apps@1.6.0, stdio, macOS).
   Kai's iframe uses `useApp` from `@modelcontextprotocol/ext-apps/react` which
   *should* handle the handshake correctly.
3. **@ochafik's concrete next step** for #165 / @shickle is: open DevTools,
   find `*.claudemcpcontent.com` inner iframe, check console for `app.connect()`
   and `ui/initialize`.
4. The claimed "working a week ago" is suspicious — need to verify in Kai's
   session history what "working" looked like. Could have been Claude Desktop
   chat UI (where iframes render) vs. Claude Code Desktop (where they never did).
5. The *current* repro used Amplitude's `render_chart` MCP tool — which emits
   `shouldRenderUI: true` (a **custom Amplitude field, not MCP Apps spec**).
   Need to verify: does Amplitude's tool actually emit `_meta.ui.resourceUri`?
   If not, Claude Desktop was never supposed to render it as an iframe; the
   host was just displaying whatever text the tool returned.

## Next step

- Grep today's sessions for exact phrase `shouldRenderUI` to find what renderer
  worked last week.
- Verify whether Amplitude MCP tool responses include `_meta.ui.resourceUri` or
  just the custom `shouldRenderUI` field.
- Check whether Kai is testing in Claude Desktop chat UI or Claude Code Desktop.
