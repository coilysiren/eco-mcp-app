"""Fetch + shape data for the `get_eco_map` tool.

The card renders by overlaying SVG polygons on the live WorldPreview.gif
(inlined as a data URI to satisfy Claude Desktop's CSP — see
claude-ai-mcp#40). We deliberately do **not** pull in Pillow just to draw
filled polygons onto the GIF: an SVG overlay handles both the fill and the
hover tooltip cleanly, keeping the dep surface small. The task spec allows
either approach; this picks the lighter one.

Coordinate system — caveats worth internalizing before touching this module:

* The Eco server reports `{x, y, z}` dimensions where `y` is elevation
  (0-200) and the world is `x` by `z` in the horizontal plane. Today the
  server returns `{x:720, y:200, z:720}`.
* The `/api/v1/map/property` payload's `{x, y}` pairs are actually `{x, z}`
  — the 2D projection names the vertical screen axis "y" even though it's
  the world's `z`. We treat the payload's `y` as the world's `z` and scale
  by `dimension.z`.
* The world wraps toroidally. A single deed can contain verts near x=0 and
  verts near x=720 — rendering them as one polygon would cut a line across
  the whole map. When consecutive verts differ by more than half the
  dimension, we split the polygon at the seam so each side renders on its
  own half of the image.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
from typing import Any, cast

import httpx

ECO_BASE_URL_DEFAULT = os.environ.get("ECO_MAP_BASE_URL", "http://eco.coilysiren.me:3001").rstrip(
    "/"
)

# Match the GIF's native framing. The preview is square 256x256-ish (Eco
# renders it at world aspect 1:1); we reproject to this logical size for
# the SVG overlay so polygon coordinates are stable regardless of how the
# browser scales the <img>.
MAP_RENDER_SIZE = 512


def _world_base_url(server: str | None) -> str:
    if not server:
        return ECO_BASE_URL_DEFAULT
    s = server.strip()
    if not s:
        return ECO_BASE_URL_DEFAULT
    if "://" not in s:
        s = f"http://{s}"
    return s.rstrip("/")


async def fetch_map_bundle(server: str | None = None) -> dict[str, Any]:
    """Fetch the three upstream payloads in parallel.

    Returns a dict with:
      * `dimension`: `{x, y, z}` — raw.
      * `property`: `{deed_name: [{x, y}, ...]}` — raw.
      * `preview_gif`: `bytes` of the animated GIF.
      * `base_url`: the base URL used (for display).
    """
    base = _world_base_url(server)
    async with httpx.AsyncClient(timeout=10.0) as client:
        dim_r = await client.get(f"{base}/api/v1/map/dimension")
        dim_r.raise_for_status()
        prop_r = await client.get(f"{base}/api/v1/map/property")
        prop_r.raise_for_status()
        gif_r = await client.get(f"{base}/Layers/WorldPreview.gif")
        gif_r.raise_for_status()
    return {
        "dimension": dim_r.json(),
        "property": prop_r.json(),
        "preview_gif": gif_r.content,
        "base_url": base,
    }


def _parse_deed_key(key: str) -> tuple[str, str]:
    """Split "Foo's Homestead, Owner: Bar" into (deed, owner)."""
    marker = ", Owner: "
    if marker in key:
        deed, owner = key.rsplit(marker, 1)
        return deed, owner
    return key, "Unknown"


def _owner_color(owner: str) -> str:
    """Stable pastel HSL color per owner — 40% alpha so the map shows through."""
    h = int(hashlib.md5(owner.encode("utf-8"), usedforsecurity=False).hexdigest(), 16)
    hue = h % 360
    return f"hsla({hue}, 50%, 50%, 0.4)"


def _owner_stroke(owner: str) -> str:
    h = int(hashlib.md5(owner.encode("utf-8"), usedforsecurity=False).hexdigest(), 16)
    hue = h % 360
    return f"hsla({hue}, 60%, 35%, 0.9)"


def _order_by_polar_angle(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort points around their centroid — the upstream payload is set-like.

    Not a convex hull: deeds can be genuinely non-convex, and polar ordering
    around the centroid is good enough for the small, roughly-round plots
    Eco produces (5-30 verts apiece).
    """
    if len(pts) < 3:
        return pts
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def _seam_crosses(
    pts: list[tuple[float, float]], world_x: float, world_z: float
) -> tuple[bool, bool]:
    """Detect whether the centroid→vert distances exceed half the world on each axis.

    Using the centroid — not consecutive-vert distance — because we call this
    on the *unordered* set. A single oversized span is enough; we don't need
    to know how many times it wraps.
    """
    if len(pts) < 3:
        return (False, False)
    xs = [p[0] for p in pts]
    zs = [p[1] for p in pts]
    crosses_x = (max(xs) - min(xs)) > (world_x / 2.0)
    crosses_z = (max(zs) - min(zs)) > (world_z / 2.0)
    return (crosses_x, crosses_z)


def _unwrap_for_seam(
    pts: list[tuple[float, float]],
    world_x: float,
    world_z: float,
) -> list[tuple[float, float]]:
    """Translate near-edge verts across the seam so the polygon is contiguous.

    When a deed straddles x=0/x=720, some verts are near 0 and some near
    720 — a naive draw cuts a line across the whole map. If we shift the
    low-x verts by +world_x, the set becomes contiguous in `[half, half+world_x]`
    and polar-ordering + drawing it there produces one clean polygon. The
    caller then renders it both as-is (covers the high-x side of the map)
    and translated by -world_x (covers the low-x side); SVG clips to the
    viewBox so the off-screen halves disappear naturally.
    """
    crosses_x, crosses_z = _seam_crosses(pts, world_x, world_z)
    if not crosses_x and not crosses_z:
        return pts
    half_x = world_x / 2.0
    half_z = world_z / 2.0
    out: list[tuple[float, float]] = []
    for x, z in pts:
        if crosses_x and x < half_x:
            x = x + world_x
        if crosses_z and z < half_z:
            z = z + world_z
        out.append((x, z))
    return out


def _split_seam_crossings(
    pts: list[tuple[float, float]],
    world_x: float,
    world_z: float,
) -> list[list[tuple[float, float]]]:
    """Return 1 or more polygons that together cover the seam-crossing deed.

    When a polygon wraps the world edge, we first unwrap it into an
    "extended coordinates" polygon (so it's contiguous), then emit copies
    translated by ±world dimensions so the rendered SVG covers both sides
    of the seam. SVG viewBox clipping handles what ends up off-screen.
    """
    if len(pts) < 3:
        return [pts]
    crosses_x, crosses_z = _seam_crosses(pts, world_x, world_z)
    if not crosses_x and not crosses_z:
        return [pts]
    unwrapped = _unwrap_for_seam(pts, world_x, world_z)
    copies: list[list[tuple[float, float]]] = [unwrapped]
    if crosses_x:
        copies.append([(x - world_x, z) for (x, z) in unwrapped])
    if crosses_z:
        copies.append([(x, z - world_z) for (x, z) in unwrapped])
    if crosses_x and crosses_z:
        copies.append([(x - world_x, z - world_z) for (x, z) in unwrapped])
    return copies


def build_polygons(
    property_data: dict[str, list[dict[str, Any]]],
    dimension: dict[str, Any],
    render_size: int = MAP_RENDER_SIZE,
) -> list[dict[str, Any]]:
    """Turn the raw property payload into render-ready SVG polygon specs.

    Each item is `{owner, deed, fill, stroke, points}` where `points` is an
    SVG `points` attribute string (space-separated "x,y" pairs) scaled by
    `render_size / world_size`. Seam-crossing deeds emit copies with
    coords translated beyond `[0, render_size]`; the SVG viewBox clips
    the out-of-frame halves. Deeds with fewer than 3 verts or empty
    point lists are dropped — they can't form a polygon.
    """
    world_x = float(dimension.get("x") or 720)
    # The payload's "y" is the world's z. Use dimension.z as the z extent.
    world_z = float(dimension.get("z") or 720)
    sx = render_size / world_x
    sz = render_size / world_z
    out: list[dict[str, Any]] = []
    for key, verts in (property_data or {}).items():
        if not verts:
            continue
        deed, owner = _parse_deed_key(key)
        # Work in world coords so the seam-splitter can reason about the wrap.
        pts = [(float(v["x"]), float(v["y"])) for v in verts if "x" in v and "y" in v]
        if len(pts) < 3:
            continue
        ordered = _order_by_polar_angle(pts)
        for sub in _split_seam_crossings(ordered, world_x, world_z):
            if len(sub) < 3:
                continue
            # Re-order after splitting — bucketing can scramble the angle order.
            sub = _order_by_polar_angle(sub)
            scaled = [(x * sx, z * sz) for (x, z) in sub]
            pts_attr = " ".join(f"{x:.1f},{y:.1f}" for (x, y) in scaled)
            out.append(
                {
                    "owner": owner,
                    "deed": deed,
                    "fill": _owner_color(owner),
                    "stroke": _owner_stroke(owner),
                    "points": pts_attr,
                }
            )
    return out


def gif_to_data_uri(gif_bytes: bytes) -> str:
    return f"data:image/gif;base64,{base64.b64encode(gif_bytes).decode()}"


def build_map_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    """Shape the payload the map partial consumes."""
    dim = bundle.get("dimension") or {}
    prop = bundle.get("property") or {}
    polygons = build_polygons(prop, dim)
    owners = sorted({p["owner"] for p in polygons})
    owner_colors = {o: _owner_color(o) for o in owners}
    owner_strokes = {o: _owner_stroke(o) for o in owners}
    return {
        "view": "eco_map",
        "sourceUrl": bundle.get("base_url"),
        "worldDim": {"x": dim.get("x"), "y": dim.get("y"), "z": dim.get("z")},
        "renderSize": MAP_RENDER_SIZE,
        "gifDataUri": gif_to_data_uri(cast(bytes, bundle.get("preview_gif") or b"")),
        "polygons": polygons,
        "deedCount": len({p["deed"] for p in polygons}),
        "ownerCount": len(owners),
        "owners": owners,
        "owner_colors": owner_colors,
        "owner_strokes": owner_strokes,
    }
