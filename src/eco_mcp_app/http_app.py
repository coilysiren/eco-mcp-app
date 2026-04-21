"""ASGI wrapper that exposes the MCP server over Streamable-HTTP.

Used for the homelab deploy (eco-mcp.coilysiren.me). Claude Desktop still
talks to this process over stdio via `__main__.py`; HTTP is for hosts that
connect to an MCP server by URL.

Built on Starlette (not plain FastAPI) so `Mount("/mcp", ...)` cleanly
matches both `/mcp` and `/mcp/` without the trailing-slash redirect FastAPI
inserts by default. Middleware normalizes bare-path `/mcp` to the mount path
so both forms work.

Also exposes `/preview` — a dev-only route that renders the iframe shell
with the Jinja2 card already spliced in. Useful for hot-reload iteration on
the templates without going through the MCP Apps handshake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import BaseRoute, Mount, Route

from .livereload import DEBUG, livereload_route
from .server import (
    _render_card,
    _render_error,
    _render_shell,
    build_server,
    fetch_eco_info,
    redact,
    to_payload,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class NormalizeMcpPath:
    """ASGI middleware — rewrites scope.path `/mcp` → `/mcp/` before routing.

    Starlette's `Mount("/mcp", ...)` matches `/mcp/` and `/mcp/anything` but
    not bare `/mcp`. Some MCP clients POST straight to `/mcp` and don't follow
    redirects. Normalizing here is less invasive than two overlapping routes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


def create_app() -> Starlette:
    mcp_server = build_server()
    # stateless=True: every request gets a fresh transport. Fits the tool shape
    # — each call is a one-shot /info fetch, no long-lived session state.
    session_manager = StreamableHTTPSessionManager(app=mcp_server, stateless=True)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    async def root(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": "eco-mcp-app",
                "mcp": "/mcp/",
                "preview": "/preview",
                "health": "/healthz",
            }
        )

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def preview(request: Request) -> HTMLResponse:
        """Render the iframe shell + Jinja2 card inline, bypassing MCP handshake."""
        server_arg = request.query_params.get("server")
        try:
            raw = await fetch_eco_info(server_arg)
        except httpx.HTTPError as e:
            fragment = _render_error(str(e))
        else:
            info = redact(raw)
            info["_fetchedAtISO"] = datetime.now(UTC).isoformat()
            fragment = _render_card(to_payload(info))
        return HTMLResponse(_render_shell(prerendered=fragment))

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    routes: list[BaseRoute] = [
        Route("/", root, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/preview", preview, methods=["GET"]),
        Mount("/mcp", app=handle_mcp),
    ]
    if DEBUG:
        routes.append(livereload_route)
    inner = Starlette(lifespan=lifespan, routes=routes)
    inner.add_middleware(NormalizeMcpPath)
    return inner


app = create_app()
