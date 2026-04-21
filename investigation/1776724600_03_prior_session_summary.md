# Prior session (c952c964) final state — what was already learned

Timestamp: 2026-04-20 ~15:23 PT
Session dates: 2026-04-20T21:26:16 → 22:05:59Z

## What the prior session confirmed

Quoting Claude's final summary in that session:

> **Root cause identified.** Claude Code Desktop's MCP client
> (`clientInfo.name = "local-agent-mode-sibling-mcp-app"`) initializes with only
> `{"roots":{"listChanged":true}}` — it does **not** advertise
> `extensions.io.modelcontextprotocol/ui`. The MCP Apps iframe spec only works
> in Claude Desktop's chat UI (`clientInfo.name = "claude-ai"`), which gates
> iframe rendering on the `claudeai_mcp_a6k_enabled` GrowthBook flag. Confirmed
> by teeing the JSON-RPC traffic via a `MCP_RPC_LOG` env var.

This confirms:

1. **Claude Code Desktop's MCP client does NOT support MCP Apps** — it doesn't
   even advertise the capability in initialize.
2. **Claude Desktop (chat UI) supports MCP Apps** — but behind a GrowthBook
   feature flag called `claudeai_mcp_a6k_enabled`.

## What was implemented as the fix

> **Path A (Claude Desktop chat UI):** `_meta.ui.resourceUri` + `registerAppResource`
> → iframe renders automatically.
>
> **Path B (Claude Code Desktop):** on every tool call, `server.ts` regenerates
> a self-contained dark-themed HTML page at
> `~/projects/projects.html`, and the tool description
> tells Claude to apply a trivial `Edit` to that file — the built-in
> `PostToolUse:Edit` hook then opens it in the Launch preview panel. Verified
> end-to-end: grid of 8 projects, real thumbnails, click-through cards.

So Kai's own sibling-mcp-app has TWO rendering strategies, and Path B is what
"worked a week ago" in Claude Code Desktop. Not MCP Apps iframe at all.

## The current question's real shape

Kai's current session `dcef779b` wants an **Amplitude** chart rendered inline
in Claude Code Desktop. The Amplitude MCP server emits `shouldRenderUI: true`
but the `_meta.ui.resourceUri` is invisible to the LLM view and likely not
present anyway — that's an Amplitude-custom field, not MCP Apps spec.

Even if Amplitude's MCP does emit `_meta.ui.resourceUri`, Claude Code Desktop
would not render an iframe because:
1. CCD doesn't advertise `io.modelcontextprotocol/ui`
2. CCD has no MCP Apps iframe renderer at all

So the **only** path to "inline Amplitude chart in this UI (Claude Code Desktop)"
is something like sibling-mcp-app's Path B fallback:

- Build a small local MCP server or wrapper that, after calling
  `render_chart`, writes the chart data as a self-contained HTML file to disk
  and tells Claude to `Edit` it to trigger the Launch preview hook.
- Or use Amplitude's existing chart screenshot API (if any) and display as
  image in markdown (CCD renders markdown images? needs check).

## Open questions still to answer

1. Does Amplitude MCP have an `export_chart_image` or equivalent? If yes,
   pure-markdown embed might work.
2. Does Claude Code Desktop's Launch preview render markdown or HTML? Is there
   a similar mechanism for images?
3. Are there new Amplitude MCP tools (`render_chart` is one of them) that hide
   additional render options?

## Next step

- Scan session `f3a122e0` (the earlier one, started at 14:25) for any additional
  context about rendering approaches tried.
- Check whether `render_chart` result includes an HTML preview URL (saw
  `chartEditUrl` but that's an external link to amplitude.com, not an
  embedded preview).
- Check other Amplitude MCP tools for image export.
