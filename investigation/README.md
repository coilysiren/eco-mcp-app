# Investigation — MCP Apps in Claude Desktop

These files are the chronological log of an ~8-hour debug session spent figuring
out how Claude Desktop's MCP Apps spec actually works in practice. They were
originally written while debugging a private sibling MCP App (same architecture,
different tenant — TypeScript + React + `@modelcontextprotocol/ext-apps`),
then copied here and scrubbed of the sibling's product/company specifics when
`eco-mcp-app` was built on the same MCP Apps foundation.

## How to read them

Start with [`1776724900_04_master_findings.md`][master] — it's the consolidated
view. If you want the full narrative, read in numeric order:

- **00_bootstrap** — goal, ground rules, and the artifacts I was looking at when
  I started.
- **01_github_issues_digest** — the `anthropics/claude-ai-mcp` issues that
  matter (#40, #61, #69, #71, #102, #142, #149, #165) with enough quotes to use
  offline.
- **02_client_identity_plot_twist** — the moment I realized Claude Desktop chat
  UI and Claude Code Desktop are separate surfaces with different MCP
  capabilities.
- **03_prior_session_summary** — what a previous session had already
  established about CCD's client-side gaps.
- **04_master_findings** — the consolidated "read this one if resuming" state.
- **05_pivot_to_action** — dropping the iframe-in-CCD goal and pivoting to a
  Launch-preview-shaped path.
- **06_path_d_worked** — the pivot paid off.
- **07_actual_fix** — what the real bug turned out to be (hardcoded `img-src`
  CSP, #40) and the data-URI workaround.
- **08_eco_mcp_app_debug_chain** — what building a *second* MCP App in Python
  surfaced that the first one didn't (ui/initialize param shape, size-changed
  mechanism, Python SDK `read_resource` return type, dev harness pattern).
- **issue_61_reply_draft** — a ready-to-post comment on anthropics/claude-ai-mcp#61
  pinging the people in that thread with the relevant findings.

## What was scrubbed

References to the sibling's tRPC endpoints, product terms, tokens, environment
variable names, coworker names, and CDN URLs were replaced with generic
placeholders (`sibling-mcp-app`, `SIBLING_API_TOKEN`, `cdn.example.com`, etc.).
The MCP Apps findings are unchanged.

[master]: 1776724900_04_master_findings.md
