# Draft reply for [anthropics/claude-ai-mcp#61](https://github.com/anthropics/claude-ai-mcp/issues/61)

For Kai to review before posting. Post as one comment on the issue.

---

Jumping in with a data point after spending several hours today pulling logs, tracing
RPC traffic, and confirming end-to-end rendering against my own server. Short version:
**MCP Apps works end-to-end today in Claude Desktop chat UI on ext-apps@1.6.0 +
stdio + macOS** — at least for my setup, which closely mirrors several reports in
this thread. Sharing the evidence in case it's useful, then pinging specific
folks below.

## My working-reference details

- Claude Desktop (chat UI), macOS, Apple Silicon
- Local stdio MCP server, Node v22.17.0
- [`@modelcontextprotocol/ext-apps`](https://www.npmjs.com/package/@modelcontextprotocol/ext-apps) v1.6.0
- [`@modelcontextprotocol/sdk`](https://www.npmjs.com/package/@modelcontextprotocol/sdk) v1.29.0
- Tool `_meta` sets **both** `ui.resourceUri` (nested, spec form) and `ui/resourceUri`
  (flat, legacy form) — defensive per [#71](https://github.com/anthropics/claude-ai-mcp/issues/71)
- Resource registered via `registerAppResource(...)` with `RESOURCE_MIME_TYPE`
  (`text/html;profile=mcp-app`)
- Iframe uses `useApp` from `@modelcontextprotocol/ext-apps/react` — handshake
  handled by the SDK, not hand-rolled
- Vite build bundles everything single-file; no network fetches from the iframe

RPC trace of a successful render pulled from `~/Library/Logs/Claude/mcp-server-*.log`:

```
23:02:36  client → initialize (protocolVersion 2025-11-25, capability
          extensions.io.modelcontextprotocol/ui with mimeTypes text/html;profile=mcp-app)
23:02:36  server → initialize result (tools+resources listChanged)
23:02:37  client → notifications/initialized
23:02:37  client → tools/list   +   client → resources/list
23:02:37  server → tools (with _meta.ui.resourceUri in both forms)
23:02:37  server → resources (ui://app/preview.html, text/html;profile=mcp-app)
23:04:00  client → tools/call list_recent_projects
23:04:00  client → resources/read ui://app/preview.html
23:04:00  server → resource contents (308KB HTML)
[iframe renders; tool result delivered via ui/notifications/tool-result]
```

Full trace and per-step notes in the session log; happy to share if useful.

## Targeted notes

### @shickle — closest match to my stack

Your environment is effectively the same as mine (ext-apps@1.6.0, stdio, macOS,
`mimeType: "text/html;profile=mcp-app"`, `_meta.ui.resourceUri`), and you're
seeing the empty-iframe-with-`</>` symptom I'd have been seeing too if I'd
used a different bundling path.

Two things that might be worth checking:

1. **React binding vs. manual `app.connect()`** — the single biggest thing my
   iframe does differently from the hand-rolled examples is it uses `useApp`
   from `@modelcontextprotocol/ext-apps/react`, which transparently handles
   `ui/initialize` → `ui/notifications/initialized` for you. If your 705KB
   bundle calls `app.connect()` directly, worth verifying the handshake
   promise actually resolves. @ochafik's DevTools suggestion (find the inner
   iframe under `*.claudemcpcontent.com`, watch the console) is the fastest
   way to see this.
2. **Confirming the no-script test isn't a valid isolation** — @ochafik's
   point holds: with no script, there's no handshake, so `visibility: hidden`
   is correct behavior. My identical stack renders a full app fine, so the
   pipeline works — the delta will be somewhere in the iframe's JS path.

I can share my full `server.ts` + `mcp-app.tsx` as a reference if it'd help.

### @pmgriffin — confirming @ochafik's diagnosis

Concur with @ochafik. Bundling `@modelcontextprotocol/ext-apps` and letting
`app.connect()` (or `useApp` on the React side) run the handshake for you is
dramatically simpler than hand-rolling `postMessage`. My iframe is ~20 lines
of React + `useApp({ onAppCreated: ... })` and handles all the spec mechanics
correctly. If it helps, I can point at my repo.

### @hchang007 — DNS test from @ochafik

If `dig anything.claudemcpcontent.com` does return an IP from your affected
machine but you still see the popup, could you share the DevTools Network
tab while opening an MCP App? Would help confirm whether it's DNS or
something downstream. Separately, note that even when the sandbox loads
successfully, external image origins get blocked by a hardcoded `img-src`
(see the next section) — unrelated to your failure, but worth knowing.

### @nearestnabors — stdio-vs-HTTP delta

My stack is stdio and it works, matching your stdio reference. Not directly
helpful for the HTTP-proxy side, but at least data-confirming the non-HTTP
path. Your self-resolution on [#149](https://github.com/anthropics/claude-ai-mcp/issues/149)
("don't call tools during initialization; set up `ontoolresult` before
`connect()`; MCP Apps are push, not pull") held up for me — my React root
only sets the handler in `onAppCreated` and never pulls tool data itself.

### @ochafik — thanks, plus four more data points

Your [three-cause breakdown](https://github.com/anthropics/claude-ai-mcp/issues/61#issuecomment-4283640203)
was hugely clarifying. I confirmed cause 1 isn't the problem for my stack
(SDK handshake works), and I'm on stdio so cause 3 doesn't apply. Built a
second MCP App in Python with a hand-rolled handshake (no ext-apps SDK) as
a reference for the non-TypeScript path, and hit a few things worth
flagging:

**Addendum to cause 1 — the `ui/initialize` params snippet in your comment
is incomplete.** The literal params you showed
(`{protocolVersion:"2026-01-26", capabilities:{}}`) doesn't match the actual
schema. Reading `@modelcontextprotocol/ext-apps/dist/src/app.js`, the
canonical call is:

```js
this.request({method:"ui/initialize", params:{
  appCapabilities: this._capabilities,
  appInfo: this._appInfo,
  protocolVersion: B,
}}, M)
```

where `M = McpUiInitializeResultSchema` and the request schema
(`McpUiInitializeRequestSchema`) has both `appInfo` and `appCapabilities`
as required, plus `protocolVersion`. A hand-rolled iframe following your
snippet literally sends `{protocolVersion, capabilities}` — schema
validation fails host-side, initialize never resolves, app stays hidden.
Exactly the symptom you described for @pmgriffin. Might be worth editing
the comment or adding an addendum for anyone who lands here without the
SDK.

1. **`_meta.ui.csp` is still ignored for `img-src`** — covered by
   [#40](https://github.com/anthropics/claude-ai-mcp/issues/40). My
   thumbnails live on `cdn.example.com` /
   `storage.googleapis.com/...`. Declaring `resourceDomains` didn't help;
   the enforced sandbox CSP stays `img-src 'self' data: blob:
   https://assets.claude.ai`. Workaround that works: fetch thumbnails
   server-side in the tool handler and inline them as
   `data:image/jpeg;base64,...` before returning the payload. Iframe renders
   them fine because `data:` is already in the allowlist. ~30KB per image
   times 8 cards = ~200KB payload overhead, acceptable. Worth noting for
   anyone hitting the same wall; @antonpk1's comment on #40 implies
   `frame-src` relaxation is intentionally blocked for now, so the
   `img-src` equivalent may be stuck until the security model evolves.

2. **Claude Code Desktop (`clientInfo.name = "local-agent-mode-*"`) does
   not advertise `extensions.io.modelcontextprotocol/ui`** on initialize.
   Only Claude Desktop chat UI (`clientInfo.name = "claude-ai"`) does.
   Confirmed via RPC logging on both surfaces. If this is a deliberate
   product state (CCD uses its Launch preview panel as its
   inline-visualization primitive instead of MCP Apps iframes), fine; just
   flagging it in case it's useful context. Not filing a separate issue —
   sounds more like a feature ask than a bug.

3. **Sizing: Claude Desktop 1.3561 listens for `ui/notifications/size-changed`,
   not `documentElement.style.height`.** Issue
   [#69](https://github.com/anthropics/claude-ai-mcp/issues/69) says the
   host reads `iframe.contentDocument.documentElement.height` directly.
   That's not what my hand-rolled Python MCP App saw. Tested three sizing
   strategies in sequence — CSS `html{height:780px}`, inline
   `style="height:780px"`, inline early-script setting
   `documentElement.style.height="780px"` — all three left the iframe
   clipped. The moment I switched to sending
   `ui/notifications/size-changed {width, height}` after initialized +
   on `ResizeObserver` fires (i.e. mirrored the ext-apps SDK's
   `setupSizeChangedNotifications` exactly), the iframe resized
   correctly. Either #69's reading-documentElement-height behavior was
   a regression fixed between Feb 2026 and now, or it was only true on
   claude.ai web (I'm testing Claude Desktop on macOS). Worth a note
   either way — for anyone reading #69 as a workaround guide, the
   answer today is "just emit size-changed."

### @hiteshgulati — routing question

Didn't want to skip you. No new info without more detail on your transport
and whether ThriveCA uses the ext-apps SDK or hand-rolls postMessage —
that's the same question @ochafik asked. Worth answering since the two
paths have different failure modes.

---

<sub>
Writeup drafted with Claude Code (Opus 4.7) during a multi-hour investigation
into why an Amplitude chart wasn't rendering in Claude Code Desktop. The
investigation concluded that MCP Apps works in Claude Desktop chat UI today,
the CCD gap above is a separate design state, and the only user-visible bug
in my path was the img-src CSP handling in #40.
</sub>
