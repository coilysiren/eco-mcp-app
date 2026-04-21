# eco-mcp-app debug chain — what the sibling-mcp-app investigation couldn't tell us

Timestamp: 2026-04-20 ~18:00 PT

After the sibling-mcp-app investigation wrapped (files `00_bootstrap` through
`07_actual_fix`), Kai had me build a **second** MCP App — `eco-mcp-app` — for
the "Eco via Sirens" game server he runs. It's a Python + vanilla HTML stack
instead of TypeScript/React/ext-apps, and it exposed three additional things
about the MCP Apps spec that the sibling-mcp-app work never had to touch,
because the sibling uses the ext-apps SDK which papers over them.

Companion: the sibling is a private TypeScript MCP App using ext-apps/React.
This file lives in `coilysiren/eco-mcp-app` — the Python reference,
hand-rolled handshake, no SDK.

## Finding #1 — `ui/initialize` params shape is wrong in @ochafik's issue #61 snippet

@ochafik's rosetta-stone comment on [#61](https://github.com/anthropics/claude-ai-mcp/issues/61#issuecomment-4283640203)
says the manual handshake is:

```
send({jsonrpc:"2.0", id:1, method:"ui/initialize",
      params:{protocolVersion:"2026-01-26", capabilities:{}}})
```

That's a simplification. The real schema (found by reading
`node_modules/@modelcontextprotocol/ext-apps/dist/src/app.js` — search for
`ui/initialize`) is:

```
request({method:"ui/initialize", params:{
  appInfo: this._appInfo,           // { name, version }
  appCapabilities: this._capabilities,
  protocolVersion: B,               // B = "2026-01-26"
}}, M)                              // M = McpUiInitializeResultSchema
```

`appInfo` and `appCapabilities` are **required** (the Zod schema
`McpUiInitializeRequestSchema` has them non-optional). A hand-rolled iframe
following @ochafik's snippet literally sends `{protocolVersion, capabilities}`,
which fails schema validation on the host side, so the `ui/initialize`
response promise never resolves, the View never sends
`ui/notifications/initialized`, and per the spec Claude Desktop keeps the
container at `visibility: hidden`. Symptom is identical to "MCP Apps is
broken" — iframe container renders blank under the tool call header while
the tool result text reaches the LLM fine.

Fix (commit `3d2b30f` in eco-mcp-app):
```js
await request("ui/initialize", {
  appInfo: { name: "eco-mcp-app", version: "0.1.0" },
  appCapabilities: {},
  protocolVersion: "2026-01-26",
});
```

Worth flagging in the reply on #61. Will add to `issue_61_reply_draft.md`.

## Finding #2 — Claude Desktop listens for `ui/notifications/size-changed`, not `documentElement.style.height`

Issue [#69](https://github.com/anthropics/claude-ai-mcp/issues/69) states
Claude.ai reads `iframe.contentDocument.documentElement.height` directly and
ignores `ui/notifications/size-changed`. **This is not what Claude Desktop
1.3561 actually does, at least as of today.** Confirmed by:

1. Setting `html { height: 780px }` in CSS — iframe stayed clipped at ~280px.
2. Setting `<html style="height:780px">` inline in markup + inline `<script>`
   setting `document.documentElement.style.height = "780px"` — iframe stayed
   clipped.
3. Sending `ui/notifications/size-changed { width, height }` after
   `ui/notifications/initialized` — iframe resized immediately to the
   reported height.

The ext-apps SDK's `setupSizeChangedNotifications` is what actually drives
iframe sizing in Claude Desktop. It:

- Fires on `ui/notifications/initialized` completion
- Temporarily sets `documentElement.style.height = "max-content"`, reads
  `getBoundingClientRect().height`, restores prior height
- Sends `ui/notifications/size-changed {width, height}`
- Attaches a `ResizeObserver` to both `documentElement` and `body` so content
  changes (e.g. tool-result rendering) trigger another notification

sibling-mcp-app avoids this whole issue by using `useApp` from
`@modelcontextprotocol/ext-apps/react` which does all of it transparently.

Fix (commit `20a3448` in eco-mcp-app): mirrored the SDK's pattern. The
hand-rolled iframe now sends size-changed on init + rAF-debounced on
observer fires + explicitly after each render.

## Finding #3 — Python MCP SDK: `read_resource` returns `list[ReadResourceContents]`

The Python `mcp` SDK (v1.14+) `@server.read_resource()` decorator expects a
handler returning `list[ReadResourceContents]` (a helper dataclass in
`mcp.server.lowlevel.helper_types`). Returning a `ReadResourceResult`
(from `mcp.types`, which looked like the correct type) causes the SDK to
try to iterate the pydantic model as `(fieldname, value)` tuples, calling
`.content` on a tuple → `AttributeError: 'tuple' object has no attribute 'content'`.

Fix (commit `ac48279` in eco-mcp-app):
```python
from mcp.server.lowlevel.helper_types import ReadResourceContents

@server.read_resource()
async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    ...
    return [ReadResourceContents(content=html, mime_type=RESOURCE_MIME)]
```

Symptom visible in Claude Desktop: "Unable to reach eco-mcp-app" error
badge with the tuple-attribute message, even though the tool call itself
succeeded and returned valid data — the failure was the separate
`resources/read` the host fires to fetch the iframe HTML.

## Finding #4 — dev harness beats blind iteration, catches silent SyntaxErrors

Spent four commits iterating on iframe sizing purely via "change code → ⌘Q
Claude Desktop → relaunch → test" cycles because I didn't realize I could
run a local harness. Once I did, the iframe's silent failure mode revealed
itself in seconds:

```js
preview_eval(`iframe.contentWindow.eval(scripts[1].textContent)`)
// → SyntaxError: Identifier 'pending' has already been declared
```

The iframe had a silent global SyntaxError (my new `measureAndNotify` had
`let pending`, colliding with the existing request-tracking `const pending
= new Map()`). The inline early-script had run (setting `style.height`),
but the main IIFE threw immediately — no stars rendered, no console
errors visible from the parent window. Completely invisible.

The harness (`eco-mcp-app/static/harness.html` — see also `inv harness`)
mimics Claude Desktop's MCP Apps host in ~100 lines:

- Responds to `ui/initialize` with a canned `McpUiInitializeResult`
- On `ui/notifications/initialized`, reveals the iframe
- Listens for `ui/notifications/size-changed`, sets `iframe.style.height`
- After reveal, pushes a canned `ui/notifications/tool-result`

Documented in `eco-mcp-app/README.md` under "Dev harness". Worth borrowing
the pattern for sibling-mcp-app (currently has no equivalent — all testing
goes through live Claude Desktop).

## Commits in eco-mcp-app (for reference when reading this back)

```
aff9f8a feat: initial eco-mcp-app scaffold
ac48279 fix: read_resource must return list[ReadResourceContents]
3bf123b fix: iframe sizing — #69 workaround (documentElement height, not 100vh)    <-- wrong theory
f355888 fix: iframe sizing — hardcode initial height 780px, skip ResizeObserver   <-- wrong theory
3d2b30f fix: ui/initialize params must be { appInfo, appCapabilities, protocolVersion }
741b8ca fix: set documentElement.style.height inline in markup, not via CSS       <-- wrong theory
20a3448 fix: send ui/notifications/size-changed (what Claude Desktop actually observes) <-- this was the real fix
ad40a42 docs: document the dev harness
```

Three of the four sizing commits were wrong theories before I built the
harness. First harness run pinpointed both the SyntaxError AND the
size-changed mechanism.

## Updates to the issue #61 reply draft

See neighboring `issue_61_reply_draft.md` — updating to:

1. Correct the `ui/initialize` param shape note in the @shickle / @pmgriffin
   sections. The snippet @ochafik showed is incomplete; anyone hand-rolling
   needs `appInfo` + `appCapabilities` too.
2. Add a note under @ochafik's section that #69's documentElement-height
   claim doesn't match Claude Desktop 1.3561 behavior — it listens for
   size-changed. Might be version-specific; #69 was filed Feb 2026.
