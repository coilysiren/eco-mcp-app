# Plot twist: which client is this, actually?

Timestamp: 2026-04-20 ~15:18 PT

## The critical client identity

In session `c952c964` (the big 2.2M session from 15:05), Kai told Claude:

> I am in **Claude Code Desktop**. This is visible in the screenshots I have attached.
>
> Go back to the session with the prompt > "I want to work on a sibling MCP App - as a UI
> inside of Claude Code (you) ..." there are a lot of technical details in that session,
> that's the implementation guide you are to follow. When I reference this session (the
> one you are in now - make very clear that the substate is Claude Code ***Desktop***

And in the same session earlier:

> Claude Desktop's MCP Apps iframe spec (returns `_meta.ui.resourceUri` pointing at an
> iframe). Claude Code's CLI doesn't render those iframes — it just showed the markdown
> fallback plus a raw JSON blob leaking through. The **Launch preview panel** does render
> HTML files, so this works here.

So there are THREE separate clients in play, and the user has been using the third:

| Client | clientInfo.name | MCP Apps iframe? | Notes |
|---|---|---|---|
| **Claude Desktop** (chat UI) | `claude-ai` | ✅ Yes | Advertises `extensions.io.modelcontextprotocol/ui` |
| **Claude Code CLI** (terminal) | `claude-code` | ❌ No | Plain text output only |
| **Claude Code Desktop** | `local-agent-mode-sibling-mcp-app` | ❌ No (but has Launch preview) | Renders HTML files in a side panel when Claude applies an Edit to them |

## What the current session is

Kai's current question in THIS conversation (session `dcef779b`):
> Pull a chart from amplitude, display it inline this UI (claude desktop) via the MCP Apps spec

The screenshot he showed in message 2 is **Claude Code Desktop**, not Claude Desktop chat.
He calls it "claude desktop" colloquially but the actual harness is Claude Code Desktop
(the Electron app powered by Claude Code).

## What "worked a week ago" probably refers to

The sibling MCP app's **Launch preview panel** fallback. Not MCP Apps iframe.
In `server.ts:358-367`, every tool call writes `projects.html` to disk.
A `PostToolUse:Edit` hook in Claude Code Desktop opens that file in the Launch
preview panel when Claude applies a trivial Edit to it. The tool descriptions
explicitly instruct Claude to do so.

For Amplitude's `render_chart` tool, there is no equivalent Launch preview
fallback — Amplitude's tool was designed for Claude Desktop chat UI's iframe.
So what he saw was the plain text fallback (the JSON `data` object rendered as text).

## Where this leaves the investigation

Three possible paths:

### Path 1 — Get Claude Desktop chat UI (not CCD) to render the Amplitude iframe

For this to work:
- The user moves to Claude Desktop chat UI
- Amplitude MCP server has to be connected to that client (it's not — checked
  `~/Library/Logs/Claude/`, no mcp-server-amplitude.log there)
- The Amplitude tool has to actually emit `_meta.ui.resourceUri`
- Host and server have to complete the full MCP Apps handshake

Not likely what the user wants given his explicit CCD context.

### Path 2 — Make CCD's Launch preview render the Amplitude chart

- Wrap Amplitude's chart into an HTML file, write to disk, Edit it to trigger preview
- Would need a proxy MCP server (like sibling-mcp-app) for Amplitude data

This is the brute-force path Kai is probably asking for.

### Path 3 — Get CCD itself to support MCP Apps

- CCD's clientInfo is `local-agent-mode-sibling-mcp-app` (customized per MCP)
- It does NOT advertise `extensions.io.modelcontextprotocol/ui`
- This is a client-side Anthropic build; we can't modify it from here

Unlikely unless Anthropic ships a CCD update.

## Next step

1. Verify: is the Amplitude MCP connected in Claude Desktop chat UI? (unlikely)
2. Read session `f3a122e0` to see what rendering tests were done earlier today.
3. Read the big session `c952c964` fully to understand what Kai has already tried.
4. Check `~/Library/Logs/Claude/` for any Amplitude-related MCP server logs.
