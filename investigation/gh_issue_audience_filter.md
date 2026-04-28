# Widget renders empty placeholder in Claude Desktop after `audience=["user"]` annotation on the HTMX block

## Symptom

In Claude Desktop, calling any tool that ships an HTMX widget fragment (e.g. `get_eco_server_status`) renders only the iframe shell's empty state:

> Eco server status
> Ask Claude to check the server status.

The model still receives the markdown + JSON content blocks correctly, so the chat reply has the right data — but the iframe never hydrates with the rendered card.

## Cause

Commit [`7542e67`](https://github.com/coilysiren/eco-mcp-app/commit/7542e67) ("feat: hide widget HTML from the LLM via audience=['user']") added `Annotations(audience=["user"])` to the HTMX-prefixed `TextContent` block via the new `_htmx_content()` helper in `src/eco_mcp_app/server.py`. The commit message asserted:

> Claude Desktop's handshake (templates/eco.html) reads the block by prefix, not annotation, so the widget keeps rendering.

That assumption appears to be wrong against Claude Desktop's current build. Claude Desktop seems to filter `audience=["user"]` blocks out of the `ui/notifications/tool-result` payload it forwards to the iframe — not just out of the model's context.

End-to-end behavior:

1. Tool returns three content blocks: markdown, JSON, and the `HTMX:`-prefixed widget HTML (annotated `audience=["user"]`).
2. Claude Desktop sends `ui/notifications/tool-result` to the iframe with the user-annotated block stripped.
3. The iframe's `extractHtmlFragment()` (`src/eco_mcp_app/templates/eco.html`) walks `result.content`, finds no `HTMX:`-prefixed text, returns `null`.
4. `onToolResult` early-exits; the empty-state `partials/empty.html` never gets replaced.

## Reproduction

1. `git checkout main` (any commit `7542e67` or later).
2. Run the MCP server in Claude Desktop.
3. Call `get_eco_server_status` (or any other tool with an HTMX fragment).
4. Observe: chat reply has correct data; iframe shows the empty placeholder.

## Suggested verification

Drop the annotation and rerun:

```python
def _htmx_content(fragment: str) -> TextContent:
    return TextContent(
        type="text",
        text=HTMX_PREFIX + fragment,
        # annotations=_WIDGET_AUDIENCE,   # <-- remove
    )
```

If the widget hydrates, the audience-filter hypothesis is confirmed.

## Proposed fix

Move the HTML fragment off the `content` array entirely and into `_meta`. We're already using `_meta` for `ui.resourceUri`; `_meta` is host-scoped and not forwarded to the model by compliant clients, which gets us the original goal (keeping 70KB of inlined-image HTML out of the model's context) without depending on host-side audience-filter semantics.

Sketch:

```python
# server.py — instead of _htmx_content() in the content array
return CallToolResult(
    content=[
        TextContent(type="text", text=_format_markdown(payload)),
        TextContent(type="text", text=json.dumps(payload)),
    ],
    **{"_meta": {**UI_META, "ui": {**UI_META["ui"], "fragment": _render_card(payload)}}},
)
```

```js
// templates/eco.html — read fragment from _meta first, fall back to content scan
function extractHtmlFragment(result) {
  const meta = result?._meta?.ui;
  if (meta && typeof meta.fragment === "string") return meta.fragment;
  for (const b of result?.content || []) {
    if (b?.type === "text" && typeof b.text === "string" && b.text.startsWith("HTMX:")) {
      return b.text.slice("HTMX:".length);
    }
  }
  return null;
}
```

Keep the content-block fallback for one release so a host that delivers an older tool result (or an older client running against a newer server) still renders.

## Alternatives considered

- **`Annotations(priority=0.0)`** instead of `audience=["user"]`. Advisory rather than filtering; well-behaved clients deprioritize for the model without dropping the block. Less guaranteed than `_meta` for keeping the bytes out of the LLM context.
- **Per-host conditional annotation.** The MCP server doesn't see host capabilities at tool-call time, so this would need plumbing through `ui/initialize` echoes — not worth the complexity.

## Related

- Commit `7542e67` introduced the regression.
- `investigation/1776735420_08_eco_mcp_app_debug_chain.md` documents the prior round of MCP Apps debugging and is the right home to append a "Finding #5" once this is resolved.
- Side-issue spotted while debugging: `static/harness.html` line 24 still references `../src/eco_mcp_app/ui/eco.html`, which moved to `templates/eco.html` in `de5d899` (Apr 20). The harness hasn't worked since that refactor. Worth a separate PR.
