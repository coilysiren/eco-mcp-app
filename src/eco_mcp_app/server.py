"""MCP server + UI resource for any public Eco game server."""

from __future__ import annotations

import json
import os
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    Resource,
    TextContent,
    Tool,
)
from pydantic import AnyUrl

DEFAULT_ECO_INFO_URL = os.environ.get("ECO_INFO_URL", "http://eco.coilysiren.me:3001/info")
DEFAULT_ECO_PORT = int(os.environ.get("ECO_INFO_PORT", "3001"))
RESOURCE_URI = "ui://eco/status.html"
RESOURCE_MIME = "text/html;profile=mcp-app"

# The MCP Apps spec puts the resource URI under _meta.ui.resourceUri. Some hosts
# only honor the legacy flat form `_meta["ui/resourceUri"]` (claude-ai-mcp#71),
# so set both — servers that do this render in every host we've tested.
UI_META: dict[str, Any] = {
    "ui": {"resourceUri": RESOURCE_URI},
    "ui/resourceUri": RESOURCE_URI,
}


def normalize_server_url(server: str | None) -> str:
    """Turn a user-supplied server string into a full /info URL.

    Accepts any of: a full URL (`http://host:3001/info`), host-only
    (`eco.example.com`, `192.168.1.5`), or host:port (`10.0.0.5:4001`).
    Most public Eco servers advertise as bare IPs, so we don't require a
    scheme — we assume http and the default Eco port when missing.
    """
    if not server:
        return DEFAULT_ECO_INFO_URL
    s = server.strip()
    if "://" not in s:
        s = f"http://{s}"
    parsed = urlparse(s)
    host = parsed.hostname or ""
    port = parsed.port or DEFAULT_ECO_PORT
    path = parsed.path if parsed.path and parsed.path != "/" else "/info"
    return urlunparse((parsed.scheme or "http", f"{host}:{port}", path, "", "", ""))


async def fetch_eco_info(server: str | None = None) -> dict[str, Any]:
    """Hit the Eco server /info endpoint. Raises on non-200."""
    url = normalize_server_url(server)
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        data["_sourceUrl"] = url
        return data


def redact(info: dict[str, Any]) -> dict[str, Any]:
    """Strip player names. Counts are fine — individual identities are not."""
    out = dict(info)
    out.pop("OnlinePlayersNames", None)
    return out


def to_payload(info: dict[str, Any]) -> dict[str, Any]:
    """Shape the payload the iframe consumes. Pure subset of /info minus names."""
    per_day = info.get("ExhaustionHoursGainPerWeekday") or {}
    return {
        "view": "eco_status",
        "fetchedAtISO": info.get("_fetchedAtISO"),
        "sourceUrl": info.get("_sourceUrl"),
        "server": {
            "description": info.get("Description", ""),
            "detailedDescription": info.get("DetailedDescription", ""),
            "category": info.get("Category"),
            "discord": info.get("DiscordAddress"),
            "version": info.get("Version"),
            "language": info.get("Language"),
            "paused": bool(info.get("IsPaused")),
            "hasPassword": bool(info.get("HasPassword")),
            "adminOnline": bool(info.get("AdminOnline")),
        },
        "players": {
            "online": int(info.get("OnlinePlayers") or 0),
            "total": int(info.get("TotalPlayers") or 0),
            "activeAndOnline": int(info.get("ActiveAndOnlinePlayers") or 0),
            "peakActive": int(info.get("PeakActivePlayers") or 0),
        },
        "world": {
            "size": info.get("WorldSize"),
            "plants": int(info.get("Plants") or 0),
            "animals": int(info.get("Animals") or 0),
            "laws": int(info.get("Laws") or 0),
            "totalCulture": float(info.get("TotalCulture") or 0.0),
        },
        "cycle": {
            "daysRunning": int(info.get("DaysRunning") or 0),
            "daysUntilMeteor": int(info.get("DaysUntilMeteor") or 0),
            "hasMeteor": bool(info.get("HasMeteor")),
            "collaboration": info.get("CollaborationLevel"),
            "gameSpeed": info.get("GameSpeed"),
            "simulationLevel": info.get("SimulationLevel"),
        },
        "economy": {
            "description": info.get("EconomyDesc", ""),
        },
        "exhaustion": {
            "active": bool(info.get("ExhaustionActive")),
            "afterHours": float(info.get("ExhaustionAfterHours") or 0.0),
            "hoursPerWeekday": {str(k): float(v) for k, v in per_day.items()},
        },
        "playtimesPattern": info.get("Playtimes", ""),
        "achievements": [
            {"name": k, "text": v} for k, v in (info.get("ServerAchievementsDict") or {}).items()
        ],
    }


def _format_markdown(payload: dict[str, Any]) -> str:
    p = payload["players"]
    w = payload["world"]
    c = payload["cycle"]
    s = payload["server"]
    title = s.get("description") or s.get("category") or "Eco server"
    lines = [
        f"**{title}** — {s.get('category', 'Server')} · cycle day {c['daysRunning']}",
        "",
        f"- Online: **{p['online']} / {p['total']}** players"
        f" (peak {p['peakActive']}, active {p['activeAndOnline']})",
        f"- Days until meteor: **{c['daysUntilMeteor']}**" + (" ☄" if c["hasMeteor"] else ""),
        f"- World: {w['size']} · {w['plants']:,} plants · {w['animals']:,} animals"
        f" · {w['laws']} law{'s' if w['laws'] != 1 else ''}"
        f" · culture {w['totalCulture']:.1f}",
        f"- Version: `{s.get('version', '?')}` · {c['collaboration']} · {c['gameSpeed']}",
    ]
    if s.get("discord"):
        lines.append(f"- [Join Discord]({s['discord']})")
    if payload.get("sourceUrl"):
        lines.append(f"- Source: `{payload['sourceUrl']}`")
    return "\n".join(lines)


def _load_ui_html() -> str:
    """Load the iframe HTML from the package. Falls back to src/ for dev."""
    try:
        return (files("eco_mcp_app.ui") / "eco.html").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        # Dev fallback — running from source tree without install
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "ui", "eco.html"), encoding="utf-8") as f:
            return f.read()


def build_server() -> Server:
    """Construct the MCP Server with all handlers registered.

    Separated from `serve()` so it can be mounted in both the stdio transport
    (Claude Desktop) and the Streamable-HTTP transport (homelab FastAPI deploy).
    """
    server: Server = Server("eco-mcp-app")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="get_eco_server_status",
                title="Eco — server status",
                description=(
                    "Show the current state of any public Eco game server inline: "
                    "online players, meteor countdown, world stats, economy, version. "
                    "Defaults to the server configured via ECO_INFO_URL; pass `server` "
                    "(host, host:port, or full URL — IPs are fine, most public Eco "
                    "servers advertise as bare IPs) to target a different one. "
                    "Renders as a visual widget in Claude Desktop via the MCP Apps "
                    "spec; falls back to a plain-text summary in other hosts."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "server": {
                            "type": "string",
                            "description": (
                                "Eco server to query. Accepts a bare host or IP "
                                "(`eco.example.com`, `192.168.1.5`), host:port "
                                "(`10.0.0.5:4001`), or a full `/info` URL. Omit to use "
                                "the server configured via ECO_INFO_URL."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                **{"_meta": UI_META},
            )
        ]

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(
                uri=AnyUrl(RESOURCE_URI),
                name=RESOURCE_URI,
                mimeType=RESOURCE_MIME,
            )
        ]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        if str(uri) != RESOURCE_URI:
            raise ValueError(f"Unknown resource: {uri}")
        html = _load_ui_html()
        return [ReadResourceContents(content=html, mime_type=RESOURCE_MIME)]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name != "get_eco_server_status":
            raise ValueError(f"Unknown tool: {name}")
        from datetime import UTC, datetime

        server_arg = arguments.get("server") if arguments else None
        try:
            raw = await fetch_eco_info(server_arg)
        except httpx.HTTPError as e:
            err_payload = {"view": "error", "message": f"Could not reach Eco server: {e}"}
            return CallToolResult(
                content=[
                    TextContent(type="text", text=f"**Eco server unreachable:** {e}"),
                    TextContent(type="text", text=json.dumps(err_payload)),
                ],
                isError=True,
                **{"_meta": UI_META},
            )

        info = redact(raw)
        info["_fetchedAtISO"] = datetime.now(UTC).isoformat()
        payload = to_payload(info)
        return CallToolResult(
            content=[
                TextContent(type="text", text=_format_markdown(payload)),
                TextContent(type="text", text=json.dumps(payload)),
            ],
            **{"_meta": UI_META},
        )

    return server


def build_initialization_options(server: Server) -> InitializationOptions:
    return InitializationOptions(
        server_name="eco-mcp-app",
        server_version="0.1.0",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


async def serve() -> None:
    """Stdio transport — the Claude Desktop entry point used by __main__.main()."""
    server = build_server()
    options = build_initialization_options(server)
    async with stdio_server() as (read, write):
        await server.run(read, write, options)
