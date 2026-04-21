"""MCP server + UI resource for any public Eco game server.

Rendering flow: server-side Jinja2 templates produce both the initial iframe
shell (served as an MCP resource) and the per-tool-call HTML fragment (shipped
inside the tool result and swapped into #root client-side). HTMX is loaded in
the shell and used to `htmx.process()` new fragments — future interactive bits
can be expressed declaratively with `hx-*` attributes on the partials.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape
from markupsafe import Markup, escape
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
STEAM_URL = "https://store.steampowered.com/app/382310/Eco/"
RESOURCE_URI = "ui://eco/status.html"
RESOURCE_MIME = "text/html;profile=mcp-app"

# The MCP Apps spec puts the resource URI under _meta.ui.resourceUri. Some hosts
# only honor the legacy flat form `_meta["ui/resourceUri"]` (claude-ai-mcp#71),
# so set both — servers that do this render in every host we've tested.
UI_META: dict[str, Any] = {
    "ui": {"resourceUri": RESOURCE_URI},
    "ui/resourceUri": RESOURCE_URI,
}

# Prefix used on the text content block that carries the Jinja2-rendered HTML
# fragment, so the iframe JS can find it without mistaking the markdown
# fallback or the JSON payload for the render source.
HTMX_PREFIX = "HTMX:"


# Eco server descriptions use TextMeshPro-style rich-text markup (the game is
# built in Unity). Public servers routinely ship titles like
#   "<color=green>Eco</color> via <color=blue>Sirens</color> | Cycle 13 ..."
# and also `<b>`, `<i>`, `<size=20>`, `<sprite name="...">`, `<icon name="...">`
# etc. We only translate <color=...> into inline-styled spans (since that's
# the only tag that carries visible meaning in a plain-text card); everything
# else is stripped. Contents are always escape-then-interpolated so the output
# stays XSS-safe even though it's marked Markup.
_TMP_TOKEN = re.compile(r"<color=#?([A-Za-z0-9]+)>|</color>", re.IGNORECASE)
_TMP_OTHER_TAG = re.compile(
    r"</?(?:b|i|u|s|size|sprite|icon|style|mark|lowercase|uppercase|smallcaps)"
    r"(?:\s[^>]*)?/?>",
    re.IGNORECASE,
)

# Map TMP named colors to CSS colors. Unknown names pass through so CSS named
# colors work directly; hex values are handled by prefixing `#` if missing.
_TMP_NAMED_COLORS = {
    "black",
    "white",
    "red",
    "green",
    "blue",
    "yellow",
    "cyan",
    "magenta",
    "gray",
    "grey",
    "orange",
    "purple",
    "pink",
    "brown",
    "lightblue",
    "lightgreen",
    "lightyellow",
    "darkblue",
    "darkgreen",
    "darkred",
    "darkgray",
    "darkgrey",
}


def format_eco_markup(text: str | None) -> Markup:
    """Convert Eco / Unity TextMeshPro markup to safe HTML.

    Keeps <color=...>…</color> as styled spans; strips all other TMP tags.
    Single-pass tokenizer so close tags are handled as tags (not literals) and
    unbalanced markup doesn't leak `</color>` into the output.
    """
    if not text:
        return Markup("")
    # Drop the tags we don't render. Do this before coloring so stripped tags
    # can't nest inside color spans in weird ways.
    text = _TMP_OTHER_TAG.sub("", text)
    out: list[str] = []
    depth = 0
    pos = 0
    for m in _TMP_TOKEN.finditer(text):
        out.append(str(escape(text[pos : m.start()])))
        raw_open = m.group(1)
        if raw_open is not None:
            if raw_open.lower() in _TMP_NAMED_COLORS:
                color = raw_open.lower()
            elif re.fullmatch(r"[0-9a-fA-F]{3,8}", raw_open):
                color = f"#{raw_open}"
            else:
                color = raw_open.lower()
            out.append(f'<span style="color:{escape(color)}">')
            depth += 1
        else:  # </color>
            if depth > 0:
                out.append("</span>")
                depth -= 1
        pos = m.end()
    out.append(str(escape(text[pos:])))
    out.extend(["</span>"] * depth)
    return Markup("".join(out))


def _fmt_number(n: Any) -> str:
    if n is None:
        return "—"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    return f"{n:,}"


def _build_jinja_env() -> Environment:
    """Build a Jinja2 Environment that works from both installed package and src tree."""
    here = Path(__file__).resolve().parent
    loaders = [
        PackageLoader("eco_mcp_app", "templates"),
        FileSystemLoader(here / "templates"),
    ]
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["any"] = lambda *xs: any(
        x is not None and x != "" and x != 0 and x != "0" for x in xs
    )
    env.globals["fmt"] = _fmt_number
    return env


_JINJA = _build_jinja_env()


def _load_asset_data_uri(filename: str, mime: str) -> str:
    """Read a file from templates/assets/ and return it as a data URI.

    Claude Desktop's sandbox CSP blocks external origins (claude-ai-mcp#40), so
    HTMX and the Steam banner must be inlined. Rendered once at startup.
    """
    try:
        asset_bytes = (files("eco_mcp_app.templates.assets") / filename).read_bytes()  # type: ignore[union-attr]
    except (FileNotFoundError, ModuleNotFoundError):
        here = Path(__file__).resolve().parent
        asset_bytes = (here / "templates" / "assets" / filename).read_bytes()
    b64 = base64.b64encode(asset_bytes).decode()
    return f"data:{mime};base64,{b64}"


# Computed once at startup — both are large (banner ~46KB, htmx ~50KB) so
# re-encoding per render is wasteful.
_HTMX_SRC = _load_asset_data_uri("htmx.min.js", "application/javascript")
_BANNER_SRC = _load_asset_data_uri("eco_header.jpg", "image/jpeg")


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


def _render_card(payload: dict[str, Any]) -> str:
    """Render the full card HTML fragment via Jinja2."""
    server = payload["server"]
    players = payload["players"]
    world = payload["world"]
    cycle = payload["cycle"]
    economy = payload["economy"]
    has_meteor = bool(cycle.get("hasMeteor")) and cycle.get("daysUntilMeteor") is not None
    meteor_pct = (
        max(0.0, min(100.0, 100.0 - (cycle["daysUntilMeteor"] / 60.0) * 100.0))
        if has_meteor
        else 0.0
    )
    player_pct = (players["online"] / players["total"] * 100.0) if players.get("total") else 0.0
    fetched_at = "—"
    if payload.get("fetchedAtISO"):
        try:
            fetched_at = (
                datetime.fromisoformat(payload["fetchedAtISO"]).astimezone().strftime("%H:%M:%S")
            )
        except ValueError:
            fetched_at = payload["fetchedAtISO"]
    ctx = {
        "title": format_eco_markup(
            server.get("description") or server.get("category") or "Eco server"
        ),
        "server": server,
        "players": players,
        "world": world,
        "cycle": cycle,
        "economy": economy,
        "has_meteor": has_meteor,
        "meteor_pct": meteor_pct,
        "player_pct": player_pct,
        "fetched_at": fetched_at,
        "achievements_count": len(payload.get("achievements") or []),
        "source_url": payload.get("sourceUrl"),
        "steam_url": STEAM_URL,
        "banner_src": _BANNER_SRC,
    }
    return _JINJA.get_template("partials/card.html").render(**ctx)


def _render_error(message: str) -> str:
    return _JINJA.get_template("partials/error.html").render(message=message)


def _render_shell() -> str:
    """Render the iframe shell — what the MCP resource returns."""
    return _JINJA.get_template("eco.html").render(
        htmx_src=_HTMX_SRC,
        banner_src=_BANNER_SRC,
        steam_url=STEAM_URL,
    )


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
        return [ReadResourceContents(content=_render_shell(), mime_type=RESOURCE_MIME)]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name != "get_eco_server_status":
            raise ValueError(f"Unknown tool: {name}")

        server_arg = arguments.get("server") if arguments else None
        try:
            raw = await fetch_eco_info(server_arg)
        except httpx.HTTPError as e:
            err_payload = {"view": "error", "message": f"Could not reach Eco server: {e}"}
            return CallToolResult(
                content=[
                    TextContent(type="text", text=f"**Eco server unreachable:** {e}"),
                    TextContent(type="text", text=json.dumps(err_payload)),
                    TextContent(type="text", text=HTMX_PREFIX + _render_error(str(e))),
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
                TextContent(type="text", text=HTMX_PREFIX + _render_card(payload)),
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
