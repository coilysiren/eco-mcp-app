"""Committed, offline-first cache for ecopedia cards and species profiles.

Two JSON blobs live under `data/` at the repo root and ship with the
package:

* ``data/ecopedia.json`` — fully-resolved `EcopediaCard` dicts, pre-built
  once from Wikidata + Wikipedia by ``scripts/build_ecopedia_cache.py``.
  Keyed by ``{category_or_empty}::{name_lower}``.
* ``data/species_profiles.json`` — partial `SpeciesPayload` dicts (everything
  except the per-server ``population`` series), pre-built by
  ``scripts/build_species_cards.py`` from iNaturalist + Wikipedia. Keyed
  by the raw Eco species id (e.g. ``ElkSpecies``).

The runtime consults these before hitting SQLite or the network, so the
happy path for vanilla Eco content is zero network calls and zero
Wikidata rate-limit exposure. Set ``ECO_MCP_PRELOAD_DISABLE=1`` to bypass
the preload (used by tests so they can drive the respx mocks).
"""

from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path
from typing import Any

_DISABLE_ENV = "ECO_MCP_PRELOAD_DISABLE"


def _disabled() -> bool:
    return bool(os.environ.get(_DISABLE_ENV))


def _load(filename: str) -> dict[str, Any]:
    """Load a committed JSON blob from either the installed package or the
    repo-root ``data/`` dir.

    Mirrors the discovery pattern in ``ecoregion._load_ecoregions_bundled``:
    wheels carry the file under ``eco_mcp_app/data/`` via the
    ``force-include`` mapping in pyproject.toml, while source checkouts
    keep it at repo-root ``data/`` where the build scripts write it.
    """
    try:
        packaged = files("eco_mcp_app").joinpath("data", filename)
        if packaged.is_file():
            data = json.loads(packaged.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    here = Path(__file__).resolve().parent
    for parent in (here.parent.parent, here.parent, here):
        candidate = parent / "data" / filename
        if candidate.exists():
            try:
                with candidate.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


_ECOPEDIA: dict[str, Any] | None = None
_SPECIES: dict[str, Any] | None = None


def _ecopedia() -> dict[str, Any]:
    global _ECOPEDIA
    if _ECOPEDIA is None:
        _ECOPEDIA = _load("ecopedia.json")
    return _ECOPEDIA


def _species() -> dict[str, Any]:
    global _SPECIES
    if _SPECIES is None:
        _SPECIES = _load("species_profiles.json")
    return _SPECIES


def ecopedia_key(name: str, category: str | None) -> str:
    return f"{category or ''}::{name.strip().lower()}"


def get_ecopedia_card(name: str, category: str | None) -> dict[str, Any] | None:
    """Return a pre-built ecopedia card dict or None.

    Falls back to a category-less lookup when ``category`` is supplied but
    the preload only has a generic Wikipedia entry for the name. That way
    ``explain_eco_item(name="Oak", category="plant")`` still hits the
    preloaded generic Oak card instead of going live.
    """
    if _disabled():
        return None
    table = _ecopedia()
    hit = table.get(ecopedia_key(name, category))
    if hit is None and category:
        hit = table.get(ecopedia_key(name, None))
    return hit if isinstance(hit, dict) else None


def get_species_profile(species_id: str) -> dict[str, Any] | None:
    """Return the stable (population-less) species profile for an Eco id."""
    if _disabled():
        return None
    hit = _species().get(species_id)
    return hit if isinstance(hit, dict) else None


def reset_for_tests() -> None:
    """Force the next lookup to re-read from disk. Only for tests."""
    global _ECOPEDIA, _SPECIES
    _ECOPEDIA = None
    _SPECIES = None
