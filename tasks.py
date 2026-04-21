"""pyinvoke tasks for eco-mcp-app. Mirrors the pattern in coilysiren/infrastructure."""

from __future__ import annotations

from invoke import task


@task
def sync(c):  # type: ignore[no-untyped-def]
    """Install deps via uv."""
    c.run("uv sync")


@task
def smoke(c):  # type: ignore[no-untyped-def]
    """End-to-end smoke test the server via stdio. Prints tool + resource output."""
    c.run(
        r"""(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{"extensions":{"io.modelcontextprotocol/ui":{"mimeTypes":["text/html;profile=mcp-app"]}}},"clientInfo":{"name":"claude-ai","version":"0.1.0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"resources/read","params":{"uri":"ui://eco/status.html"}}' \
  '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_eco_server_status","arguments":{}}}'; sleep 8) | uv run python -m eco_mcp_app""",
        pty=False,
    )


@task
def install_desktop(c):  # type: ignore[no-untyped-def]
    """Write eco-mcp-app into ~/Library/Application Support/Claude/claude_desktop_config.json."""
    c.run("python scripts/install-desktop-config.py")


@task(help={"port": "Port to bind the harness HTTP server on (default: 8765)."})
def harness(c, port=8765):  # type: ignore[no-untyped-def]
    """Serve the static/harness.html that mimics Claude Desktop's MCP Apps host.

    Opens http://localhost:<port>/static/harness.html — an HTML page that embeds the
    iframe, answers ui/initialize, pushes a canned ui/notifications/tool-result,
    and listens for ui/notifications/size-changed. Useful for iterating on the
    iframe without needing to Cmd+Q Claude Desktop every time.
    """
    print(f"Harness: http://localhost:{port}/static/harness.html")
    c.run(f"python3 -m http.server {port}")


@task
def ruff(c):  # type: ignore[no-untyped-def]
    """Lint + format (check mode)."""
    c.run("uv run ruff check src tasks.py")
    c.run("uv run ruff format --check src tasks.py")


@task
def fmt(c):  # type: ignore[no-untyped-def]
    """Apply ruff formatting."""
    c.run("uv run ruff check --fix src tasks.py")
    c.run("uv run ruff format src tasks.py")


@task
def precommit(c):  # type: ignore[no-untyped-def]
    """Run all pre-commit hooks against every file."""
    c.run("uv run pre-commit run --all-files")
