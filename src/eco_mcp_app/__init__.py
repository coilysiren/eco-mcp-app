"""Eco via Sirens MCP App — a Claude Desktop inline widget for the Eco game server.

Also a reusable Python package. Sibling apps (e.g. `eco-spec-tracker`) depend on
this one to embed the live Eco server card in their own pages. The two public
entry points for that are :func:`render_status_html` and :func:`status_css`.
"""

from __future__ import annotations

from importlib.resources import files as _pkg_files

import httpx as _httpx

from .server import (
    _render_card,
    _render_error,
    fetch_eco_info,
    redact,
    to_payload,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "fetch_eco_info",
    "redact",
    "render_status_html",
    "status_css",
    "to_payload",
]


async def render_status_html(server: str | None = None) -> str:
    """Fetch the Eco server's ``/info`` and render it as an HTML fragment.

    This is the card shown at ``https://eco-mcp.coilysiren.me/preview`` — banner,
    meteor countdown, player/world/cycle/economy stats, chips, and footer links.
    On HTTP failure returns an error fragment (also safe to embed).

    Pair with :func:`status_css` — inject that CSS into the host page once so
    the card renders correctly.

    Args:
        server: host, ``host:port``, or full URL of the Eco server. ``None``
            uses the default configured in :mod:`eco_mcp_app.server`.
    """
    try:
        info = await fetch_eco_info(server)
    except _httpx.HTTPError as e:
        return _render_error(str(e))
    return _render_card(to_payload(redact(info)))


def status_css() -> str:
    """Return the CSS that :func:`render_status_html` output depends on."""
    return _pkg_files("eco_mcp_app.templates").joinpath("eco.css").read_text()
