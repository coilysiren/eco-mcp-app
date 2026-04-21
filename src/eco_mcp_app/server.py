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
# Base URL for non-/info endpoints on the same server. Derived from
# DEFAULT_ECO_INFO_URL at import time so overriding ECO_INFO_URL in tests or
# deploys redirects every endpoint consistently.
DEFAULT_ECO_BASE_URL = DEFAULT_ECO_INFO_URL.rsplit("/info", 1)[0]
STEAM_URL = "https://store.steampowered.com/app/382310/Eco/"
RESOURCE_URI = "ui://eco/status.html"
RESOURCE_MIME = "text/html;profile=mcp-app"

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


# ---------------------------------------------------------------------------
# Government org-chart tool
# ---------------------------------------------------------------------------
#
# Eco law descriptions are authored in TextMeshPro rich-text markup, but the
# government panel doesn't try to color or style them — we just want a plain
# human-readable preview for the footer. This regex strips the four tag
# families we see in practice (`<link=...>`, `<icon ...>`, `<color=...>`,
# `<style=...>`) and leaves surrounding text intact. See
# `format_eco_markup` for the richer renderer used by the /info card title.
# The spec suggests `</?(link|icon|color|style)(\s[^>]*)?>` but actual Eco
# output uses `<style="Header">` / `<color=#FFF>` with no whitespace before
# the attribute, so broaden the post-name character class to `[\s=]` to
# catch the common inline-attribute forms while still terminating cleanly
# on a closing `>`.
_LAW_MARKUP = re.compile(r"</?(link|icon|color|style)([\s=][^>]*)?>", re.IGNORECASE)


def strip_law_markup(s: str | None) -> str:
    """Remove Eco rich-text tags from a law description."""
    if not s:
        return ""
    return _LAW_MARKUP.sub("", s).strip()


# Labels inside each title's Table rows that we care about. Keys are the
# normalized attribute name we expose; values are the exact `Property` labels
# the Eco API emits. These are verified live against
# `/api/v1/elections/titles` on Day 3 of Cycle 13; if upstream relabels them
# we'll start rendering "None" and that's fine — the layout still holds.
_TITLE_ROW_KEYS = {
    "election_process": "Election Process",
    "eligible_candidates": "Eligible Candidates",
    "successor": "Successor",
    "who_can_remove": "Who Can Remove From Office",
    "term_days": "Term Limit Days",
}


def _build_eco_url(base: str | None, path: str) -> str:
    """Compose an endpoint URL from a user-supplied server (or the default)."""
    if not base:
        return f"{DEFAULT_ECO_BASE_URL}{path}"
    normalized = normalize_server_url(base)
    # normalize_server_url always appends `/info` (or whatever path the user
    # supplied). Strip any path off — we want just scheme + host:port.
    parsed = urlparse(normalized)
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


async def fetch_eco_government(server: str | None = None) -> dict[str, Any]:
    """Hit the three civic endpoints in parallel and return raw JSON.

    Returns a dict with keys `titles`, `elections`, `laws`, each the parsed
    JSON body. `elections` may be `[]` (verified empty on Day 3 of the
    current cycle) and callers must handle that. Raises on the first
    non-200 / connect error encountered.
    """
    titles_url = _build_eco_url(server, "/api/v1/elections/titles")
    elections_url = _build_eco_url(server, "/api/v1/elections")
    laws_url = _build_eco_url(server, "/api/v1/laws?byStates=Active")
    async with httpx.AsyncClient(timeout=8.0) as client:
        titles = await _get_json(client, titles_url)
        elections = await _get_json(client, elections_url)
        laws = await _get_json(client, laws_url)
    return {
        "titles": titles,
        "elections": elections,
        "laws": laws,
        "_sourceUrl": titles_url,
    }


def _row_value(table: list[list[str]], label: str) -> str | None:
    """Pull the value cell from a `[property, description, value]` row."""
    for row in table:
        if row and len(row) >= 3 and row[0] == label:
            return row[2]
    return None


def _extract_scope(titles: list[dict[str, Any]]) -> str:
    """Derive the settlement/federation name from title scopes.

    Title names are shaped like `"<Scope> Mayor"` / `"<Scope> Governor"` /
    `"<Scope> Sheriff"`. We take the first title and strip the trailing role
    word. Returns `"Unknown settlement"` if we can't parse it — callers
    render that string directly in the header.
    """
    if not titles:
        return "Unknown settlement"
    first = titles[0].get("Name", "") or ""
    # Strip the last token (role word) — "Foo Bar Mayor" → "Foo Bar".
    parts = first.rsplit(" ", 1)
    if len(parts) == 2 and parts[1]:
        return parts[0]
    return first or "Unknown settlement"


def to_government_payload(
    data: dict[str, Any],
    *,
    fetched_at_iso: str | None = None,
) -> dict[str, Any]:
    """Shape the raw endpoint blob into the view dict the template consumes."""
    titles_raw: list[dict[str, Any]] = data.get("titles") or []
    elections_raw: list[dict[str, Any]] = data.get("elections") or []
    laws_raw: list[dict[str, Any]] = data.get("laws") or []

    titles: list[dict[str, Any]] = []
    for t in titles_raw:
        table = t.get("Table") or []
        titles.append(
            {
                "id": t.get("Id"),
                "name": t.get("Name") or "?",
                "state": t.get("State"),
                "occupants": list(t.get("OccupantNames") or []),
                "successor": _row_value(table, _TITLE_ROW_KEYS["successor"]),
                "who_can_remove": _row_value(table, _TITLE_ROW_KEYS["who_can_remove"]),
                "term_days": _row_value(table, _TITLE_ROW_KEYS["term_days"]),
                "eligible_candidates": _row_value(table, _TITLE_ROW_KEYS["eligible_candidates"]),
            }
        )

    # Elections — server claims to filter `byStates=Active` on laws but
    # doesn't always honour the filter. We don't pass a filter for elections
    # (endpoint accepts no args) — just defensively keep only ones that look
    # open. `EndTime` / `TimeLeft` field naming drifts across Eco versions,
    # so we check a few likely shapes.
    elections: list[dict[str, Any]] = []
    for e in elections_raw:
        ends_in_hours: float | None = None
        if isinstance(e.get("TimeLeft"), int | float):
            ends_in_hours = float(e["TimeLeft"]) / 3600.0
        elif isinstance(e.get("HoursLeft"), int | float):
            ends_in_hours = float(e["HoursLeft"])
        elections.append(
            {
                "id": e.get("Id"),
                "name": e.get("Name") or e.get("TitleName") or "Election",
                "ends_in_hours": ends_in_hours,
                "state": e.get("State"),
            }
        )

    # Client-side filter: the `byStates=Active` query param is advisory —
    # verified upstream returns mixed states anyway.
    active_laws = [law for law in laws_raw if (law.get("State") or "") == "Active"]
    active_laws_count = len(active_laws)

    cleaned_laws = [
        {
            "name": law.get("Name") or "?",
            "clean": strip_law_markup(law.get("Description") or ""),
        }
        for law in active_laws
    ]
    cleaned_laws = [law for law in cleaned_laws if law["clean"]]

    shortest_law: dict[str, Any] | None = None
    longest_law: dict[str, Any] | None = None
    if cleaned_laws:
        shortest = min(cleaned_laws, key=lambda law: len(law["clean"]))
        longest = max(cleaned_laws, key=lambda law: len(law["clean"]))
        shortest_law = {"name": shortest["name"], "preview": _law_preview(shortest["clean"])}
        longest_law = {"name": longest["name"], "preview": _law_preview(longest["clean"])}

    return {
        "view": "eco_government",
        "fetchedAtISO": fetched_at_iso,
        "sourceUrl": data.get("_sourceUrl"),
        "scope": _extract_scope(titles_raw),
        "titles": titles,
        "elections": elections,
        "active_laws_count": active_laws_count,
        "shortest_law": shortest_law,
        "longest_law": longest_law,
    }


def _law_preview(text: str, max_chars: int = 600) -> str:
    """Trim a sanitized law body to a reasonable preview length."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _render_government_card(payload: dict[str, Any]) -> str:
    fetched_at = "—"
    if payload.get("fetchedAtISO"):
        try:
            fetched_at = (
                datetime.fromisoformat(payload["fetchedAtISO"]).astimezone().strftime("%H:%M:%S")
            )
        except ValueError:
            fetched_at = payload["fetchedAtISO"]
    return _JINJA.get_template("partials/government.html").render(
        gov=payload,
        fetched_at=fetched_at,
        source_url=payload.get("sourceUrl"),
    )


def _format_government_markdown(payload: dict[str, Any]) -> str:
    lines = [f"**{payload['scope']} — Government**", ""]
    if payload["titles"]:
        for t in payload["titles"]:
            occs = ", ".join(t["occupants"]) if t["occupants"] else "_vacant_"
            lines.append(f"- **{t['name']}** — {occs}")
    else:
        lines.append("- No civic titles configured")
    lines.append("")
    if payload["elections"]:
        lines.append("**Active elections:**")
        for e in payload["elections"]:
            if e["ends_in_hours"] is not None:
                lines.append(f"- {e['name']} — ends in {round(e['ends_in_hours'])}h")
            else:
                lines.append(f"- {e['name']}")
    else:
        lines.append("_No active elections._")
    lines.append("")
    lines.append(f"Active laws: **{payload['active_laws_count']}**")
    if payload.get("shortest_law"):
        lines.append(f"- shortest: {payload['shortest_law']['name']}")
    if payload.get("longest_law"):
        lines.append(f"- longest: {payload['longest_law']['name']}")
    return "\n".join(lines)


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
                name="get_eco_government",
                title="Eco — government org-chart",
                description=(
                    "Show the civic structure of an Eco server: elected titles "
                    "with occupants, active elections with countdown chips, and "
                    "a count + preview of active laws with Eco markup stripped. "
                    "Handles empty states gracefully (early-cycle servers often "
                    "have no active elections). Defaults to the server "
                    "configured via ECO_INFO_URL; pass `server` to target "
                    "another Eco server by host, host:port, or URL."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "server": {
                            "type": "string",
                            "description": (
                                "Eco server to query. Accepts a bare host, "
                                "host:port, or full URL. Omit to use "
                                "ECO_INFO_URL."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                **{"_meta": UI_META},
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
            )
        ]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        if str(uri) != RESOURCE_URI:
            raise ValueError(f"Unknown resource: {uri}")
        return [ReadResourceContents(content=_render_shell(), mime_type=RESOURCE_MIME)]

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

        if name == "get_eco_government":
            server_arg = arguments.get("server") if arguments else None
            try:
                raw_gov = await fetch_eco_government(server_arg)
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
            gov_payload = to_government_payload(
                raw_gov, fetched_at_iso=datetime.now(UTC).isoformat()
            )
            return CallToolResult(
                content=[
                    TextContent(type="text", text=_format_government_markdown(gov_payload)),
                    TextContent(type="text", text=json.dumps(gov_payload)),
                    TextContent(
                        type="text",
                        text=HTMX_PREFIX + _render_government_card(gov_payload),
                    ),
                ],
                **{"_meta": UI_META},
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
