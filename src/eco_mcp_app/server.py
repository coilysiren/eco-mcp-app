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
import time
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
ECONOMY_RESOURCE_URI = "ui://eco/economy.html"
RESOURCE_MIME = "text/html;profile=mcp-app"

# Economy dashboard: datasets pulled from the admin /datasets/get endpoint.
# Listed here so both tool wiring and tests share one source of truth; each
# string must appear in `/datasets/flatlist` on the live server.
ECONOMY_DATASETS: tuple[str, ...] = (
    "OfferedLoanOrBond",
    "AcceptedLoanOrBond",
    "RepaidLoanOrBond",
    "DefaultedOnLoanOrBond",
    "PayWages",
    "PayRentOrMoveInFee",
    "PostedContract",
    "CompletedContract",
    "FailedContract",
    "PropertyTransfer",
    "ReputationTransfer",
    "TransferMoney",
    "PayTax",
    "ReceiveGovernmentFunds",
)

# Single source of truth for the public servers surfaced both as "try-others"
# pills on the rendered card and as the `list_public_eco_servers` tool's
# response. Curated from eco-servers.org, chosen for variety in Eco markup
# patterns + ruleset (so the iframe gets exercised against diverse titles).
KNOWN_PUBLIC_SERVERS: list[dict[str, str]] = [
    {
        "label": "Eco via Sirens",
        "host": "eco.coilysiren.me:3001",
        "notes": "Kai's server (default for this MCP). Highly modded, collaborative.",
    },
    {
        "label": "AWLGaming",
        "host": "ecoserver.awlgaming.net:5679",
        "notes": "Hex + named color mix in the TMP title.",
    },
    {
        "label": "GreenLeaf Prime",
        "host": "eco.greenleafserver.com:3021",
        "notes": "<#RRGGBB> shorthand rainbow title.",
    },
    {
        "label": "GreenLeaf Vanilla",
        "host": "eco.greenleafserver.com:3031",
        "notes": "Same host as Prime, vanilla ruleset.",
    },
    {
        "label": "The Dao Kingdom",
        "host": "daokingdom.eu:3001",
        "notes": "Short-form hex + explicit </color> closes.",
    },
    {
        "label": "Peaceful Utopia",
        "host": "eco.bleedcraft.com:3001",
        "notes": "No markup in the title; meteor already passed.",
    },
]

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
# TMP accepts both `<color=…>` and the shorthand `<#RRGGBB>` / `<#RRGGBBAA>`.
# Both open forms share the same color stack and </color> closes either.
# Capture group 1 = value from `<color=…>`, group 2 = value from `<#…>`.
_TMP_TOKEN = re.compile(
    r"<color=#?([A-Za-z0-9]+)>|<#([0-9a-fA-F]{3,8})>|</color>",
    re.IGNORECASE,
)
_TMP_OTHER_TAG = re.compile(
    r"</?(?:b|i|u|s|size|sprite|icon|style|mark|lowercase|uppercase|smallcaps)"
    r"(?:[\s=][^>]*)?/?>",
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
        color_word = m.group(1)  # from <color=...>
        hex_short = m.group(2)  # from <#RRGGBB>
        if color_word is not None or hex_short is not None:
            raw = color_word if color_word is not None else hex_short
            assert raw is not None
            if raw.lower() in _TMP_NAMED_COLORS:
                color = raw.lower()
            elif re.fullmatch(r"[0-9a-fA-F]{3,8}", raw):
                color = f"#{raw}"
            else:
                color = raw.lower()
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
    # Dev-only browser livereload. Empty string in production so the
    # `{{ livereload_script | safe }}` in the shell is a no-op.
    from .livereload import DEBUG as _DEBUG
    from .livereload import LIVERELOAD_SCRIPT as _LIVERELOAD_SCRIPT

    env.globals["livereload_script"] = _LIVERELOAD_SCRIPT if _DEBUG else ""
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
# play.eco's ecofavicon.ico, inlined because Claude Desktop's CSP blocks
# external origins (claude-ai-mcp#40).
_FAVICON_SRC = _load_asset_data_uri("favicon.ico", "image/x-icon")


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
    if not s:
        return DEFAULT_ECO_INFO_URL
    if "://" not in s:
        s = f"http://{s}"
    parsed = urlparse(s)
    host = parsed.hostname or ""
    port = parsed.port or DEFAULT_ECO_PORT
    path = parsed.path if parsed.path and parsed.path != "/" else "/info"
    return urlunparse((parsed.scheme or "http", f"{host}:{port}", path, "", "", ""))


# In-memory cache for /info responses. The /preview route can get hammered by
# refreshes, and each cache miss fans out to a third-party Eco server — without
# this a single tab reloader can DoS a small community server. 30s matches
# Eco's own in-game stats update cadence closely enough that nothing visibly
# stale slips through. Cache key is the normalized URL so the same server
# expressed two ways (`host` vs `host:3001/info`) shares an entry.
_INFO_CACHE_TTL_S = float(os.environ.get("ECO_INFO_CACHE_TTL", "30"))
_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def fetch_eco_info(server: str | None = None) -> dict[str, Any]:
    """Hit the Eco server /info endpoint. Raises on non-200. 30s memoized."""
    url = normalize_server_url(server)
    now = time.monotonic()
    cached = _info_cache.get(url)
    if cached and (now - cached[0]) < _INFO_CACHE_TTL_S:
        return dict(cached[1])
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        data["_sourceUrl"] = url
        _info_cache[url] = (now, dict(data))
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
        "known_servers": KNOWN_PUBLIC_SERVERS,
    }
    return _JINJA.get_template("partials/card.html").render(**ctx)


def _render_error(message: str) -> str:
    return _JINJA.get_template("partials/error.html").render(message=message)


def _render_shell(prerendered: str | None = None) -> str:
    """Render the iframe shell — what the MCP resource returns.

    `prerendered`: if given, placed inside #root instead of the empty state.
    The HTTP /preview endpoint uses this to splice the Jinja2 card into the
    shell directly so a browser sees real data without the MCP handshake.
    """
    return _JINJA.get_template("eco.html").render(
        htmx_src=_HTMX_SRC,
        banner_src=_BANNER_SRC,
        favicon_src=_FAVICON_SRC,
        steam_url=STEAM_URL,
        prerendered=Markup(prerendered) if prerendered else None,
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


##
## Economy dashboard
##
## Separate code path from `/info`: hits the admin /datasets/get endpoint
## (requires X-API-Key header) and /info for cycle-day + EconomyDesc, computes
## KPIs on top, and renders a dedicated card partial with inline SVG sparklines.
##

# Base URL for admin endpoints. We derive the admin base from ECO_INFO_URL so a
# non-default server can be targeted by setting one env var.
_ADMIN_DEFAULT_BASE = os.environ.get(
    "ECO_ADMIN_BASE",
    DEFAULT_ECO_INFO_URL.rsplit("/info", 1)[0],
)

# SSM secret paths — documented in todo/README.md. Region is pinned us-east-1:
# the AWS CLI default is us-west-2 and would silently miss these params.
_SSM_REGION = os.environ.get("AWS_REGION", "us-east-1")
_ECO_ADMIN_SSM_PATH = os.environ.get("ECO_ADMIN_TOKEN_SSM", "/eco-mcp-app/api-admin-token")

# Admin token cache — loaded once per process at first-use, not per-request.
# An explicit env var ECO_ADMIN_TOKEN overrides SSM so tests and local dev
# don't need AWS credentials.
_admin_token_cache: dict[str, str | None] = {}


def _load_admin_token() -> str | None:
    """Return the Eco admin API token or None if unavailable.

    Order: `ECO_ADMIN_TOKEN` env var → SSM `/eco-mcp-app/api-admin-token` in
    us-east-1 → None (caller renders the empty-state card). Cached so we
    don't reach for boto3 on every call.
    """
    if "token" in _admin_token_cache:
        return _admin_token_cache["token"]
    token = os.environ.get("ECO_ADMIN_TOKEN")
    if not token:
        try:
            import boto3  # type: ignore[import-not-found]

            ssm = boto3.client("ssm", region_name=_SSM_REGION)
            resp = ssm.get_parameter(Name=_ECO_ADMIN_SSM_PATH, WithDecryption=True)
            token = resp["Parameter"]["Value"]
        except Exception:
            # boto3 missing, no creds, or param not found — all equivalent for
            # our purposes (we'll render the card with no series).
            token = None
    _admin_token_cache["token"] = token
    return token


# Per-process dataset cache. The dashboard is viewed in bursts (user alt-tabs
# between conversation + iframe), and each render fans out 14 admin requests —
# without this we'd hammer the Eco server's admin endpoint.
_ECONOMY_CACHE_TTL_S = float(os.environ.get("ECO_ECONOMY_CACHE_TTL", "45"))
_economy_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _fetch_dataset(
    client: httpx.AsyncClient,
    base: str,
    name: str,
    day_end: int,
    headers: dict[str, str],
) -> list[tuple[float, float]]:
    """Fetch a single /datasets/get series. Returns [] on any non-200 or shape surprise.

    Day-3 reality: some series are legitimately empty, and malformed stats
    return 500. We shouldn't let a single bad series blow up the whole card.
    """
    try:
        url = f"{base}/datasets/get"
        r = await client.get(
            url,
            params={"dataset": name, "dayStart": 0, "dayEnd": max(day_end, 1)},
            headers=headers,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    # /datasets/get returns either a list of {Time, Value} dicts or a list of
    # two-item [time, value] pairs — tolerate both shapes defensively.
    out: list[tuple[float, float]] = []
    if isinstance(data, list):
        for pt in data:
            if isinstance(pt, dict):
                t = pt.get("Time", pt.get("time"))
                v = pt.get("Value", pt.get("value"))
            elif isinstance(pt, list | tuple) and len(pt) >= 2:
                t, v = pt[0], pt[1]
            else:
                continue
            try:
                out.append((float(t), float(v)))
            except (TypeError, ValueError):
                continue
    elif isinstance(data, dict):
        # Sometimes the endpoint wraps points under a "Values" / "Points" key.
        points = data.get("Values") or data.get("Points") or []
        for pt in points:
            try:
                out.append((float(pt["Time"]), float(pt["Value"])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


async def fetch_economy(server: str | None = None) -> dict[str, Any]:
    """Fetch /info + all ECONOMY_DATASETS series for the given Eco server.

    Shape: `{info, days_elapsed, series: {name: [(t,v), ...]}, admin_ok}`.
    Never raises for admin-token problems — we degrade to an empty series map
    and the card renders an "admin token missing" banner.
    """
    info_url = normalize_server_url(server)
    # Derive admin base from the /info URL so the same `server` arg routes both.
    parsed = urlparse(info_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    cache_key = base
    now = time.monotonic()
    cached = _economy_cache.get(cache_key)
    if cached and (now - cached[0]) < _ECONOMY_CACHE_TTL_S:
        return dict(cached[1])

    info = await fetch_eco_info(server)
    # TimeSinceStart is seconds since cycle start; some servers return a float.
    # One in-game "day" = 3600s by default, but the authoritative number is
    # `DaysRunning` on /info — match what the rest of the UI already shows.
    days_elapsed = int(info.get("DaysRunning") or 0)
    if days_elapsed <= 0:
        tss = info.get("TimeSinceStart")
        try:
            days_elapsed = max(1, int(float(tss) / 3600.0))
        except (TypeError, ValueError):
            days_elapsed = 1

    token = _load_admin_token()
    admin_ok = bool(token)
    series: dict[str, list[tuple[float, float]]] = {}
    if token:
        headers = {"X-API-Key": token}
        async with httpx.AsyncClient(timeout=10.0) as client:
            import asyncio

            results = await asyncio.gather(
                *(
                    _fetch_dataset(client, base, name, days_elapsed, headers)
                    for name in ECONOMY_DATASETS
                ),
                return_exceptions=False,
            )
        series = dict(zip(ECONOMY_DATASETS, results, strict=True))
    else:
        series = {name: [] for name in ECONOMY_DATASETS}

    out: dict[str, Any] = {
        "info": info,
        "days_elapsed": days_elapsed,
        "series": series,
        "admin_ok": admin_ok,
    }
    _economy_cache[cache_key] = (now, dict(out))
    return out


def _series_total(points: list[tuple[float, float]]) -> float:
    """Sum of values (count-type stats are already cumulative/per-event)."""
    return float(sum(v for _, v in points))


def _series_last(points: list[tuple[float, float]]) -> float:
    return float(points[-1][1]) if points else 0.0


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return var**0.5


def _sparkline_svg(points: list[tuple[float, float]], width: int = 180, height: int = 40) -> str:
    """Render a series as a minimal inline SVG sparkline.

    Empty / single-point series render as a flat dashed baseline so we always
    emit a DOM node of the same footprint (prevents layout thrash between
    empty and filled states).
    """
    if len(points) < 2:
        return (
            f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" role="img" aria-label="no data">'
            f'<line x1="0" y1="{height // 2}" x2="{width}" y2="{height // 2}" '
            f'stroke="var(--fg-faint)" stroke-dasharray="3 4" stroke-width="1"/>'
            f"</svg>"
        )
    xs = [t for t, _ in points]
    ys = [v for _, v in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_span = max(x_max - x_min, 1e-9)
    y_span = max(y_max - y_min, 1e-9)
    pad = 2
    coords = []
    for t, v in points:
        x = pad + (t - x_min) / x_span * (width - 2 * pad)
        y = height - pad - (v - y_min) / y_span * (height - 2 * pad)
        coords.append(f"{x:.1f},{y:.1f}")
    path = "M " + " L ".join(coords)
    # Fill area under line for visual weight.
    area = path + f" L {coords[-1].split(',')[0]},{height - pad} L {pad},{height - pad} Z"
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" aria-label="sparkline">'
        f'<path d="{area}" fill="var(--leaf)" fill-opacity="0.18"/>'
        f'<path d="{path}" fill="none" stroke="var(--leaf-bright)" '
        f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
        f"</svg>"
    )


def compute_economy_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a fetch_economy() result into the dict consumed by the card template.

    Classification thresholds (per task spec):
      booming : default rate < 5% AND trades/day up 20% WoW (when we have ≥7d)
      stressed: default rate > 15% OR contract failure rate > 30%
      healthy : otherwise
    """
    info = raw.get("info") or {}
    series: dict[str, list[tuple[float, float]]] = raw.get("series") or {}
    days_elapsed = max(1, int(raw.get("days_elapsed") or 1))

    # KPI primitives.
    offered_loans = _series_total(series.get("OfferedLoanOrBond", []))
    accepted_loans = _series_total(series.get("AcceptedLoanOrBond", []))
    repaid_loans = _series_total(series.get("RepaidLoanOrBond", []))
    defaulted_loans = _series_total(series.get("DefaultedOnLoanOrBond", []))

    posted_contracts = _series_total(series.get("PostedContract", []))
    completed_contracts = _series_total(series.get("CompletedContract", []))
    failed_contracts = _series_total(series.get("FailedContract", []))

    wages = _series_total(series.get("PayWages", []))
    taxes_paid = _series_total(series.get("PayTax", []))
    govt_funds = _series_total(series.get("ReceiveGovernmentFunds", []))
    net_tax_flow = taxes_paid - govt_funds

    # Trades/day: EconomyDesc on /info says "N trades, M contracts" authoritatively.
    # We parse it for the displayed number because /datasets doesn't have a
    # `Trade` series (TransferMoney is money transfers, not goods trades).
    econ_desc = str(info.get("EconomyDesc") or "")
    trades_total = 0
    m = re.search(r"(\d+)\s*trade", econ_desc)
    if m:
        trades_total = int(m.group(1))
    trades_per_day = round(trades_total / days_elapsed, 1) if days_elapsed else 0.0

    # Loan default rate — defaults vs (defaulted + repaid) gives the realized
    # rate; open loans (accepted-but-not-yet-repaid) aren't resolved yet.
    resolved_loans = defaulted_loans + repaid_loans
    default_rate = _pct(defaulted_loans, resolved_loans)

    # Contract completion ratio — completed / (completed + failed). Posted-but-
    # open contracts haven't had a chance to fail yet, so excluding them avoids
    # a cold-start penalty that would wrongly trigger "stressed".
    completion_ratio = _pct(completed_contracts, completed_contracts + failed_contracts)
    failure_rate = _pct(failed_contracts, completed_contracts + failed_contracts)

    # Week-over-week trades/day delta (needs ≥8 days of runtime).
    trades_wow_pct: float | None = None
    if days_elapsed >= 8 and trades_total > 0:
        # We don't have a trades time-series, so this is a best-effort based on
        # assumed uniform rate since cycle start vs. the last 7 days.
        # With only cumulative info, we approximate by comparing the trailing
        # week's implied rate to the overall rate.
        overall_rate = trades_total / days_elapsed
        trailing = trades_total - (overall_rate * (days_elapsed - 7))
        trailing_rate = trailing / 7.0
        if overall_rate > 0:
            trades_wow_pct = round(((trailing_rate / overall_rate) - 1.0) * 100.0, 1)

    # Classify.
    if default_rate > 15.0 or failure_rate > 30.0:
        health = "stressed"
    elif default_rate < 5.0 and (trades_wow_pct is not None and trades_wow_pct >= 20.0):
        health = "booming"
    else:
        health = "healthy"

    narrative = (
        f"Economy is {health} — {default_rate}% default rate, "
        f"{completion_ratio}% contracts completed"
    )

    # Sparkline candidates: pick up to 4 series with the highest normalized
    # stddev (excluding series that have fewer than 2 points). Normalizing by
    # mean puts small-but-volatile series like DefaultedOnLoanOrBond on equal
    # footing with high-volume series like TransferMoney.
    candidates: list[tuple[str, float, list[tuple[float, float]]]] = []
    for name, pts in series.items():
        if len(pts) < 2:
            continue
        values = [v for _, v in pts]
        mean = sum(values) / len(values) if values else 0.0
        sd = _stddev(values)
        norm = sd / mean if mean > 0 else sd
        candidates.append((name, norm, pts))
    candidates.sort(key=lambda x: x[1], reverse=True)
    sparks = [
        {
            "name": name,
            "label": _HUMAN_STAT_LABELS.get(name, name),
            "last": _series_last(pts),
            "total": _series_total(pts),
            "svg": Markup(_sparkline_svg(pts)),
        }
        for name, _sd, pts in candidates[:4]
    ]

    total_culture = float(info.get("TotalCulture") or 0.0)

    return {
        "server": {
            "description": info.get("Description", ""),
            "category": info.get("Category"),
            "sourceUrl": info.get("_sourceUrl"),
        },
        "days_elapsed": days_elapsed,
        "admin_ok": bool(raw.get("admin_ok")),
        "kpis": {
            "trades_per_day": trades_per_day,
            "trades_total": trades_total,
            "contract_completion_ratio": completion_ratio,
            "contract_failure_rate": failure_rate,
            "contracts_posted": int(posted_contracts),
            "contracts_completed": int(completed_contracts),
            "contracts_failed": int(failed_contracts),
            "loan_default_rate": default_rate,
            "loans_offered": int(offered_loans),
            "loans_accepted": int(accepted_loans),
            "loans_repaid": int(repaid_loans),
            "loans_defaulted": int(defaulted_loans),
            "wages_total": wages,
            "taxes_paid": taxes_paid,
            "govt_funds": govt_funds,
            "net_tax_flow": net_tax_flow,
            "total_culture": total_culture,
            "trades_wow_pct": trades_wow_pct,
        },
        "sparks": sparks,
        "health": health,
        "narrative": narrative,
        "economy_desc": econ_desc,
    }


# Human-readable labels for the datasets. Keys match ECONOMY_DATASETS.
_HUMAN_STAT_LABELS: dict[str, str] = {
    "OfferedLoanOrBond": "Loans offered",
    "AcceptedLoanOrBond": "Loans accepted",
    "RepaidLoanOrBond": "Loans repaid",
    "DefaultedOnLoanOrBond": "Loans defaulted",
    "PayWages": "Wages paid",
    "PayRentOrMoveInFee": "Rent & move-in",
    "PostedContract": "Contracts posted",
    "CompletedContract": "Contracts completed",
    "FailedContract": "Contracts failed",
    "PropertyTransfer": "Property transfers",
    "ReputationTransfer": "Reputation transfers",
    "TransferMoney": "Money transfers",
    "PayTax": "Taxes paid",
    "ReceiveGovernmentFunds": "Govt. funds paid out",
}


def _render_economy_card(payload: dict[str, Any]) -> str:
    fetched_at = datetime.now(UTC).astimezone().strftime("%H:%M:%S")
    return _JINJA.get_template("partials/economy_card.html").render(
        server=payload["server"],
        kpis=payload["kpis"],
        sparks=payload["sparks"],
        health=payload["health"],
        narrative=payload["narrative"],
        admin_ok=payload["admin_ok"],
        days_elapsed=payload["days_elapsed"],
        economy_desc=payload["economy_desc"],
        fetched_at=fetched_at,
        steam_url=STEAM_URL,
        banner_src=_BANNER_SRC,
    )


def _format_economy_markdown(payload: dict[str, Any]) -> str:
    k = payload["kpis"]
    server = payload["server"].get("description") or payload["server"].get("category") or "Eco"
    lines = [
        f"**{server} — economic health: {payload['health']}**",
        "",
        payload["narrative"],
        "",
        f"- Trades/day: **{k['trades_per_day']}** (total {k['trades_total']:,})",
        f"- Contracts: {k['contracts_completed']}/{k['contracts_posted']} completed"
        f" · {k['contract_failure_rate']}% failure rate",
        f"- Loans: {k['loans_accepted']} accepted / {k['loans_defaulted']} defaulted"
        f" · {k['loan_default_rate']}% default rate",
        f"- Wages paid: **{k['wages_total']:,.0f}**",
        f"- Net tax flow: **{k['net_tax_flow']:+,.0f}**"
        f" (taxes in {k['taxes_paid']:,.0f} · govt out {k['govt_funds']:,.0f})",
        f"- Total culture: {k['total_culture']:.1f}",
    ]
    if not payload.get("admin_ok"):
        lines.extend(["", "_Admin token unavailable — series data is empty._"])
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
            ),
            Tool(
                name="get_eco_economy",
                title="Eco — economic health dashboard",
                description=(
                    "Show live economic vitals for an Eco server: trades/day, "
                    "contract completion ratio, loan default rate, wages, "
                    "net tax flow, plus sparklines of the most volatile series. "
                    "Pulls /datasets/get (admin) + /info. Optional `server` arg "
                    "targets a non-default server."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "server": {
                            "type": "string",
                            "description": (
                                "Eco server to query (host, host:port, or full "
                                "/info URL). Omit to use the default."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                **{
                    "_meta": {
                        "ui": {"resourceUri": ECONOMY_RESOURCE_URI},
                        "ui/resourceUri": ECONOMY_RESOURCE_URI,
                    }
                },
            ),
            Tool(
                name="list_public_eco_servers",
                title="Eco — list public servers",
                description=(
                    "List the curated set of public Eco game servers known to this "
                    "MCP. Returns label, host:port, and free-form notes for each. "
                    "Feed any `host` back into `get_eco_server_status` as the "
                    "`server` argument to fetch its live status."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
        ]

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(
                uri=AnyUrl(RESOURCE_URI),
                name=RESOURCE_URI,
                mimeType=RESOURCE_MIME,
            ),
            Resource(
                uri=AnyUrl(ECONOMY_RESOURCE_URI),
                name=ECONOMY_RESOURCE_URI,
                mimeType=RESOURCE_MIME,
            ),
        ]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        if str(uri) in (RESOURCE_URI, ECONOMY_RESOURCE_URI):
            return [ReadResourceContents(content=_render_shell(), mime_type=RESOURCE_MIME)]
        raise ValueError(f"Unknown resource: {uri}")

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        if name == "list_public_eco_servers":
            lines = ["**Known public Eco servers:**", ""]
            for s in KNOWN_PUBLIC_SERVERS:
                lines.append(f"- **{s['label']}** — `{s['host']}` · {s['notes']}")
            return CallToolResult(
                content=[
                    TextContent(type="text", text="\n".join(lines)),
                    TextContent(
                        type="text",
                        text=json.dumps({"servers": KNOWN_PUBLIC_SERVERS}),
                    ),
                ],
            )

        if name == "get_eco_economy":
            server_arg = arguments.get("server") if arguments else None
            try:
                raw = await fetch_economy(server_arg)
            except httpx.HTTPError as e:
                err_payload = {"view": "error", "message": f"Could not reach Eco server: {e}"}
                return CallToolResult(
                    content=[
                        TextContent(type="text", text=f"**Eco server unreachable:** {e}"),
                        TextContent(type="text", text=json.dumps(err_payload)),
                        TextContent(type="text", text=HTMX_PREFIX + _render_error(str(e))),
                    ],
                    isError=True,
                    **{
                        "_meta": {
                            "ui": {"resourceUri": ECONOMY_RESOURCE_URI},
                            "ui/resourceUri": ECONOMY_RESOURCE_URI,
                        }
                    },
                )
            payload = compute_economy_payload(raw)
            return CallToolResult(
                content=[
                    TextContent(type="text", text=_format_economy_markdown(payload)),
                    TextContent(type="text", text=json.dumps(payload, default=str)),
                    TextContent(type="text", text=HTMX_PREFIX + _render_economy_card(payload)),
                ],
                **{
                    "_meta": {
                        "ui": {"resourceUri": ECONOMY_RESOURCE_URI},
                        "ui/resourceUri": ECONOMY_RESOURCE_URI,
                    }
                },
            )

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
