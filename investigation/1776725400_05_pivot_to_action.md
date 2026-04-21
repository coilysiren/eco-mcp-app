# Pivot from investigation to action

Timestamp: 2026-04-20 ~15:37 PT

## What we've confirmed

1. **sibling-mcp-app server is 100% correct**. Smoke test via stdio (with
   `claude-ai` clientInfo, `io.modelcontextprotocol/ui` extension capability)
   returns: tools with `_meta.ui.resourceUri` in both forms, resources list with
   correct URI, resource read with mime `text/html;profile=mcp-app`, 308KB HTML.
2. **Built iframe bundle contains all three MCP Apps handshake strings**:
   `ui/initialize`, `ui/notifications/initialized`, `ui/notifications/tool-result`.
   So the iframe is spec-compliant.
3. **Claude Desktop chat UI correctly advertises** `io.modelcontextprotocol/ui`.
4. **Claude Code Desktop does NOT advertise MCP Apps capability**. This was
   confirmed by the prior session via RPC logging. CCD renders no iframes.
5. **sibling-mcp-app server log shows zero `tools/call` / `resources/read`**
   events since the log started (~20:35Z). So we have no ground-truth evidence
   of whether iframe rendering actually works in Claude Desktop chat UI today
   — only that the init handshake completes.
6. **Amplitude MCP has no image/PNG export tool**. Only `render_chart` which
   returns CSV + a chart-edit URL (opens amplitude.com). Path C (markdown
   image embed) is out.

## Decision

**Pivot to Path D**: bypass MCP entirely. Since I already have the chart data
from the earlier render_chart call in this session, I can:

1. Write a self-contained HTML file to disk with that chart data.
2. Use `Edit` on the file (one-char change) to trigger CCD's `PostToolUse:Edit`
   hook — which opens it in the Launch preview panel.
3. User sees the Amplitude chart inline.

This mirrors sibling-mcp-app's Launch preview pattern exactly, but without
needing a new MCP server. The chart data came from Amplitude; the rendering
is pure HTML.

## What we've saved

Chart data (in this session's memory):
- Name: "Export: Error Rate (%)"
- Range: Last 60 days, 2026-02-19 → 2026-04-20
- 60 data points each for:
  - Formula A (trendline): 0.24% → 0.28% smooth rise
  - Formula B (raw): varies 0.12–0.94%, spike 2026-03-26 (0.56) and 2026-03-27 (0.94)

## Next step

Write `~/projects/amplitude-error-rate-chart.html` with an
inline SVG or canvas chart, then Edit it to trigger Launch preview.

If this works, it demonstrates that MCP Apps spec is NOT required for inline
visual rendering in CCD — which solves Kai's ask while sidestepping the
unfix-able CCD client-side gap.
