# Master findings — consolidated (read this one if resuming)

Timestamp: 2026-04-20 ~15:28 PT

## The single most important fact

Claude Code Desktop (CCD) — the client Kai is using right now — **does not
advertise `extensions.io.modelcontextprotocol/ui` MCP capability**. This was
confirmed in the previous session (`c952c964`) by teeing the JSON-RPC init
traffic via `MCP_RPC_LOG=/Users/kai/.sibling-mcp-app-rpc.log`.

CCD's `clientInfo.name` is `local-agent-mode-sibling-mcp-app` (or similar per
MCP). Its initialize capabilities are only `{"roots":{"listChanged":true}}`.

→ **No MCP Apps iframes will ever render in CCD regardless of what the server sends.**

By contrast, Claude Desktop chat UI (clientInfo.name=`claude-ai`) DOES advertise
the capability (confirmed in current `mcp-server-sibling-mcp-app.log`) — but
Amplitude MCP is NOT configured in Claude Desktop chat UI. It's only in CCD.

## What "worked a week ago" actually was

The prior session's final answer: a **Launch preview panel fallback**,
not MCP Apps iframe. The mechanism (from sibling-mcp-app/server.ts):

1. Tool call runs → server regenerates `~/projects/projects.html`
2. Tool response text tells Claude: "apply any trivial Edit to this file"
3. CCD's built-in `PostToolUse:Edit` hook opens the file in Launch preview
4. User sees the HTML rendered as a side panel

This is CCD-specific and has nothing to do with MCP Apps iframes.

Amplitude's `render_chart` has no such fallback — it returns CSV data + a
`chartEditUrl` that opens amplitude.com in a browser. Nothing inline.

## Amplitude render_chart inspection

What the tool returned (verified earlier in this session):

```json
{
  "success": true,
  "shouldRenderUI": true,
  "data": {"isCsvResponse": true, "csvResponse": {...}, "definition": {...}},
  "metadata": {...},
  "chartEditId": "a5fc25a1",
  "chartEditUrl": "Edit Chart: [Open in Amplitude](https://app.amplitude.com/...)"
}
```

`shouldRenderUI: true` is an **Amplitude-proprietary field**, not MCP Apps spec.
Even if Amplitude's MCP also emits `_meta.ui.resourceUri` (invisible to LLM view
but available to host), CCD would ignore it.

## GitHub issue correlation

- [#165](https://github.com/anthropics/claude-ai-mcp/issues/165) —
  closest report but its stack is Claude **Desktop** (chat UI), not CCD. Handshake
  never fires. @ochafik (Anthropic, Olivier Chafik) active on thread.
- [#61 @ochafik 2026-04-20](https://github.com/anthropics/claude-ai-mcp/issues/61#issuecomment-4283640203) —
  diagnoses three distinct causes of same symptom: (A) missing ui/initialize
  handshake, (B) claudemcpcontent.com DNS block, (C) HTTP transport session ID
  split.
- None of the issues in Anthropic's repo specifically document CCD lacking
  MCP Apps support. Possibly worth filing.

## Three viable paths forward (pick one or try all)

### Path A — Move testing to Claude Desktop chat UI

- Add Amplitude MCP to `~/Library/Application Support/Claude/claude_desktop_config.json`
- Restart Claude Desktop
- Ask in the chat UI: "show me an Amplitude chart inline"
- If Amplitude MCP emits `_meta.ui.resourceUri`, iframe should render
- **Risk:** Amplitude's remote MCP may require OAuth that only CCD performs
- **Risk:** Amplitude's `render_chart` probably doesn't emit `_meta.ui.resourceUri`
  because it was designed for their own UI, not MCP Apps spec
- Needs verification: inspect Amplitude MCP `tools/list` output for `_meta.ui.*`

### Path B — Build a CCD-native Amplitude chart renderer (Launch preview)

- Write a tiny wrapper MCP tool (or extend sibling-mcp-app) that:
  - Calls Amplitude's `query_chart` to get CSV data
  - Renders it as an HTML file with Chart.js or similar
  - Saves to disk at a known path
  - Tells Claude to Edit → opens Launch preview
- **Pros:** Works in CCD, mirrors sibling-mcp-app architecture
- **Cons:** Requires wrapping every Amplitude chart; ongoing maintenance

### Path C — Inline image embed from Amplitude

- If Amplitude MCP can produce a PNG/SVG of a chart, embed it as markdown image
- CCD may or may not render markdown images inline in chat
- Needs verification: does Amplitude have `export_chart_image` or similar?
- Needs verification: does CCD render markdown images?

## Key files touched so far in this investigation

- `~/projects/sibling-mcp-app/investigation/1776723700_00_bootstrap.md`
- `~/projects/sibling-mcp-app/investigation/1776723900_01_github_issues_digest.md`
- `~/projects/sibling-mcp-app/investigation/1776724300_02_client_identity_plot_twist.md`
- `~/projects/sibling-mcp-app/investigation/1776724600_03_prior_session_summary.md`
- `~/projects/sibling-mcp-app/investigation/1776724900_04_master_findings.md` ← this one

## Saved issue JSON dumps

- `/tmp/issue165.json`, `/tmp/issue61.json`, `/tmp/issue149.json`, `/tmp/issue47.json`,
  `/tmp/issue71.json`, `/tmp/issue69.json`, `/tmp/issue102.json`, `/tmp/issue40.json`,
  `/tmp/issue142.json`

## Next step (if resuming)

1. Explore Amplitude MCP tool surface for image export or renderable response:
   call `describe_tool` on render_chart, or check other tools for PNG/preview endpoints.
2. If none: try Path B (build a CCD-native wrapper). Probably what Kai actually wants.
3. If still none: document "CCD doesn't support MCP Apps" as a new issue to file
   against anthropics/claude-ai-mcp.

## Cleanup reminder

The RPC log was cleaned up before this session. If I enable it again via
`main.log` tampering with claude_desktop_config.json, remember to remove
`MCP_RPC_LOG` from the config afterwards.
