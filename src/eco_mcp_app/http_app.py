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

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx
import mcp.types as mt
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import BaseRoute, Mount, Route

from .livereload import DEBUG, livereload_route
from .map import build_map_payload, fetch_map_bundle
from .server import (
    HTMX_PREFIX,
    _render_card,
    _render_error,
    _render_map,
    _render_shell,
    build_server,
    fetch_eco_info,
    redact,
    to_payload,
)
from .telemetry import init_sentry

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
    init_sentry()
    mcp_server = build_server()
    # stateless=True: every request gets a fresh transport. Fits the tool shape
    # — each call is a one-shot /info fetch, no long-lived session state.
    session_manager = StreamableHTTPSessionManager(app=mcp_server, stateless=True)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    call_tool_handler = mcp_server.request_handlers[mt.CallToolRequest]
    list_tools_handler = mcp_server.request_handlers[mt.ListToolsRequest]

    # Hard-coded default query strings for the tools that need arguments — the
    # preview nav strip is for browser-poking, not full UX, so a sensible
    # default gets each card to render something without a 400. Tools omitted
    # from this map get an empty query string.
    preview_defaults = {
        "get_eco_species": "?name=Bison",
        "explain_eco_item": "?name=Iron&category=material",
        "fair_price": "?item=Copper",
    }

    async def _list_tools() -> mt.ListToolsResult:
        result = await list_tools_handler(mt.ListToolsRequest(method="tools/list"))
        return cast(mt.ListToolsResult, result.root)

    async def _preview_tool_links(current: str | None = None) -> list[dict[str, str]]:
        tools_result = await _list_tools()
        links: list[dict[str, str]] = []
        for tool in sorted(tools_result.tools, key=lambda t: t.name):
            # Tools with no UI fragment (no `_meta.ui.resourceUri`) aren't worth
            # linking — clicking through would just show the empty iframe.
            # pydantic aliases the JSON `_meta` key to the `meta` attribute.
            meta = getattr(tool, "meta", None) or {}
            ui = (meta.get("ui") if isinstance(meta, dict) else None) or {}
            if not ui.get("resourceUri"):
                continue
            # get_eco_server_status is already the bare /preview/ page — the
            # top-nav "./preview" link covers it. Skipping here avoids a
            # duplicate tools-row entry that renders the same card.
            if tool.name == "get_eco_server_status":
                continue
            qs = preview_defaults.get(tool.name, "")
            links.append(
                {
                    "label": tool.name,
                    "href": f"/preview/{tool.name}{qs}",
                    "current": "page" if tool.name == current else "",
                }
            )
        return links

    async def root(_: Request) -> RedirectResponse:
        return RedirectResponse(url="/preview", status_code=302)

    async def service_info(_: Request) -> JSONResponse:
        tools_result = await _list_tools()
        names = sorted(t.name for t in tools_result.tools)
        return JSONResponse(
            {
                "service": "eco-mcp-app",
                "mcp": "/mcp/",
                "health": "/healthz",
                "preview": "/preview",
                "previewJson": "/preview.json",
                "previewMap": "/preview-map",
                "previewMapJson": "/preview-map.json",
                "previewTools": [f"/preview/{name}" for name in names],
                "previewToolsJson": [f"/preview/{name}.json" for name in names],
            }
        )

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def _json_url(path: str, request: Request) -> str:
        qs = request.url.query
        return f"{path}?{qs}" if qs else path

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
        tool_links = await _preview_tool_links()
        return HTMLResponse(
            _render_shell(
                prerendered=fragment,
                preview_tools=tool_links,
                json_url=_json_url("/preview.json", request),
            )
        )

    async def preview_json(request: Request) -> JSONResponse:
        server_arg = request.query_params.get("server")
        try:
            raw = await fetch_eco_info(server_arg)
        except httpx.HTTPError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        info = redact(raw)
        info["_fetchedAtISO"] = datetime.now(UTC).isoformat()
        return JSONResponse(to_payload(info))

    async def preview_map(request: Request) -> HTMLResponse:
        """Render the iframe shell with the map card inline — dev preview."""
        server_arg = request.query_params.get("server")
        try:
            bundle = await fetch_map_bundle(server_arg)
        except httpx.HTTPError as e:
            fragment = _render_error(str(e))
        else:
            fragment = _render_map(build_map_payload(bundle))
        tool_links = await _preview_tool_links(current="get_eco_map")
        return HTMLResponse(
            _render_shell(
                prerendered=fragment,
                preview_tools=tool_links,
                json_url=_json_url("/preview-map.json", request),
            )
        )

    async def preview_map_json(request: Request) -> JSONResponse:
        server_arg = request.query_params.get("server")
        try:
            bundle = await fetch_map_bundle(server_arg)
        except httpx.HTTPError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return JSONResponse(build_map_payload(bundle))

    def _extract_json_block(call_result: mt.CallToolResult) -> Any:
        # Each tool emits markdown + JSON + HTMX TextContent blocks (see
        # server.call_tool). Find the JSON one by skipping HTMX and trying
        # to parse each remaining text block; first that parses wins.
        for block in call_result.content:
            text = getattr(block, "text", "") or ""
            if text.startswith(HTMX_PREFIX):
                continue
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                continue
        return None

    async def preview_tool(request: Request) -> HTMLResponse | JSONResponse:
        """Dispatch any MCP tool by name and splice its HTMX fragment into the shell.

        Query-string args are passed straight through as the tool's `arguments`,
        so `/preview/get_eco_species?name=Bison` and
        `/preview/explain_eco_item?name=Iron&category=material` work out of the
        box. Tools that produce no HTMX fragment (e.g. list_public_eco_servers)
        render the empty iframe shell — still useful as a signal that the tool
        was reachable.

        A `.json` suffix on the tool name (`/preview/get_eco_species.json?...`)
        returns the tool's JSON content block instead of the HTML shell. Same
        dispatch path, different output.
        """
        raw_name = request.path_params["tool"]
        as_json = raw_name.endswith(".json")
        tool_name = raw_name[: -len(".json")] if as_json else raw_name
        args = dict(request.query_params)
        req = mt.CallToolRequest(
            method="tools/call",
            params=mt.CallToolRequestParams(name=tool_name, arguments=args),
        )
        json_url = _json_url(f"/preview/{tool_name}.json", request)
        try:
            result = await call_tool_handler(req)
        except Exception as e:
            if as_json:
                return JSONResponse({"error": str(e)}, status_code=500)
            tool_links = await _preview_tool_links(current=tool_name)
            return HTMLResponse(
                _render_shell(
                    prerendered=_render_error(str(e)),
                    preview_tools=tool_links,
                    json_url=json_url,
                )
            )
        call_result = cast(mt.CallToolResult, result.root)
        if as_json:
            payload = _extract_json_block(call_result)
            if payload is None:
                return JSONResponse(
                    {"error": f"tool '{tool_name}' did not return a JSON content block"},
                    status_code=404,
                )
            return JSONResponse(payload)
        tool_links = await _preview_tool_links(current=tool_name)
        fragment = ""
        for block in call_result.content:
            text = getattr(block, "text", "") or ""
            if text.startswith(HTMX_PREFIX):
                fragment = text[len(HTMX_PREFIX) :]
                break
        return HTMLResponse(
            _render_shell(prerendered=fragment, preview_tools=tool_links, json_url=json_url)
        )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    routes: list[BaseRoute] = [
        Route("/", root, methods=["GET"]),
        Route("/info", service_info, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/preview", preview, methods=["GET"]),
        Route("/preview.json", preview_json, methods=["GET"]),
        Route("/preview-map", preview_map, methods=["GET"]),
        Route("/preview-map.json", preview_map_json, methods=["GET"]),
        Route("/preview/{tool}", preview_tool, methods=["GET"]),
        Mount("/mcp", app=handle_mcp),
    ]
    if DEBUG:
        routes.append(livereload_route)
    inner = Starlette(lifespan=lifespan, routes=routes)
    inner.add_middleware(NormalizeMcpPath)
    return inner


app = create_app()
