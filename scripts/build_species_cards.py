"""Pre-build ``data/species_profiles.json`` from iNaturalist + Wikipedia.

Build-time companion to ``src/eco_mcp_app/_preload.py``. Walks the vanilla
Eco species list (extracted once from the server zip at
``eco-mods/EcoServerLinux_*.zip``), resolves each against iNat's
``/v1/taxa`` endpoint, inlines the default photo as a ``data:`` URI, and
pulls a Wikipedia summary as fallback prose. The resulting JSON covers
the stable fields of a ``SpeciesPayload`` — everything except the
per-server ``population`` series, which stays live.

Usage::

    uv run python scripts/build_species_cards.py

Roughly ~90 taxa, ~2-3 minutes. iNat's rate limit is 60/min — this script
paces itself via ``_inat_rate_gate`` and should not need a key.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

os.environ["ECO_MCP_PRELOAD_DISABLE"] = "1"

from eco_mcp_app.species import (  # noqa: E402
    INAT_USER_AGENT,
    _extract_taxonomy,
    _fetch_inat_photo_bytes,
    _fetch_inat_taxon,
    _fetch_wikipedia_summary,
    _first_two_sentences,
    _strip_html,
    clean_species_name,
)

OUTPUT = _ROOT / "data" / "species_profiles.json"

# Vanilla Eco species ids, extracted from
# ``eco-mods/EcoServerLinux_v0.12.0.0-beta-staging-3173.zip``
# (``Mods/__core__/AutoGen/{Animal,Plant}/*.cs``). Kept here as a constant so
# this script has no dependency on the live admin exporter (which requires
# an SSM-managed API key) and no dependency on the server zip path.
VANILLA_ANIMAL_IDS: list[str] = [
    "AgoutiSpecies",
    "AlligatorSpecies",
    "BassSpecies",
    "BighornSheepSpecies",
    "BisonSpecies",
    "BlueSharkSpecies",
    "CodSpecies",
    "CoyoteSpecies",
    "CrabSpecies",
    "DeerSpecies",
    "ElkSpecies",
    "FoxSpecies",
    "HareSpecies",
    "JaguarSpecies",
    "MoonJellyfishSpecies",
    "MountainGoatSpecies",
    "OtterSpecies",
    "PacificSardineSpecies",
    "PrairieDogSpecies",
    "SalmonSpecies",
    "SnappingTurtleSpecies",
    "TarantulaSpecies",
    "TortoiseSpecies",
    "TroutSpecies",
    "TunaSpecies",
    "TurkeySpecies",
    "WolfSpecies",
]

VANILLA_PLANT_IDS: list[str] = [
    "AgaveSpecies",
    "AmanitaMushroomSpecies",
    "ArcticWillowSpecies",
    "BarrelCactusSpecies",
    "BeansSpecies",
    "BeetsSpecies",
    "BigBluestemSpecies",
    "BirchSpecies",
    "BoleteMushroomSpecies",
    "BullrushSpecies",
    "BunchgrassSpecies",
    "ButtonbushSpecies",
    "CamasSpecies",
    "CedarSpecies",
    "CeibaSpecies",
    "ClamSpecies",
    "CommonGrassSpecies",
    "CookeinaMushroomSpecies",
    "CornSpecies",
    "CottonSpecies",
    "CreosoteBushSpecies",
    "CriminiMushroomSpecies",
    "DaisySpecies",
    "DeerLichenSpecies",
    "DesertMossSpecies",
    "DwarfWillowSpecies",
    "FernSpecies",
    "FilmyFernSpecies",
    "FirSpecies",
    "FireweedSpecies",
    "FlaxSpecies",
    "HeliconiaSpecies",
    "HuckleberrySpecies",
    "JointfirSpecies",
    "JoshuaSpecies",
    "KelpSpecies",
    "KingFernSpecies",
    "LatticeMushroomSpecies",
    "LupineSpecies",
    "OakSpecies",
    "OceanSpraySpecies",
    "OldGrowthRedwoodSpecies",
    "OrchidSpecies",
    "PalmSpecies",
    "PapayaSpecies",
    "PeatMossSpecies",
    "PineappleSpecies",
    "PitcherPlantSpecies",
    "PricklyPearSpecies",
    "PumpkinSpecies",
    "RedwoodSpecies",
    "RiceSpecies",
    "RoseBushSpecies",
    "SaguaroCactusSpecies",
    "SalalSpecies",
    "SaxifrageSpecies",
    "SeagrassSpecies",
    "SpruceSpecies",
    "SunflowerSpecies",
    "SwitchgrassSpecies",
    "TaroSpecies",
    "TomatoesSpecies",
    "TrilliumSpecies",
    "TulipSpecies",
    "UrchinSpecies",
    "WaterweedSpecies",
    "WheatSpecies",
    "WhiteBursageSpecies",
]

ALL_IDS = VANILLA_ANIMAL_IDS + VANILLA_PLANT_IDS


def _photo_to_data_uri(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def _build_profile(species_id: str) -> dict[str, Any] | None:
    """Return the ``to_dict()`` shape minus population fields, or None on miss."""
    name = clean_species_name(species_id)
    profile: dict[str, Any] = {
        "name": name,
        "speciesId": species_id,
        "photoDataUri": None,
        "photoAttribution": None,
        "wikiExtract": None,
        "wikiUrl": None,
        "source": "none",
        "taxonomy": [],
        "conservationStatus": None,
    }

    taxon: dict[str, Any] | None = None
    try:
        taxon = await _fetch_inat_taxon(name)
    except Exception:
        taxon = None

    if taxon:
        profile["source"] = "inat"
        profile["taxonomy"] = _extract_taxonomy(taxon)
        status = taxon.get("conservation_status") or {}
        if isinstance(status, dict) and status.get("status_name"):
            profile["conservationStatus"] = str(status["status_name"])
        photo = taxon.get("default_photo") or {}
        photo_url = photo.get("medium_url") or photo.get("square_url")
        if photo_url:
            photo_bytes = await _fetch_inat_photo_bytes(photo_url)
            if photo_bytes:
                profile["photoDataUri"] = _photo_to_data_uri(photo_bytes)
                profile["photoAttribution"] = photo.get("attribution")
        profile["wikiUrl"] = taxon.get("wikipedia_url")
        if taxon.get("wikipedia_summary"):
            profile["wikiExtract"] = _first_two_sentences(
                _strip_html(str(taxon["wikipedia_summary"]))
            )

    if not profile["wikiExtract"]:
        wiki = await _fetch_wikipedia_summary(name)
        if wiki and wiki.get("extract"):
            profile["wikiExtract"] = _first_two_sentences(str(wiki["extract"]))
            profile["wikiUrl"] = (wiki.get("content_urls") or {}).get("desktop", {}).get(
                "page"
            ) or profile["wikiUrl"]
            if profile["source"] == "none":
                profile["source"] = "wikipedia"

    if profile["source"] == "none" and not profile["wikiExtract"]:
        return None
    return profile


def _load_existing() -> dict[str, Any]:
    if OUTPUT.exists():
        try:
            with OUTPUT.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


async def _main() -> int:
    print(f"User-Agent: {INAT_USER_AGENT}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    profiles = _load_existing()

    total = len(ALL_IDS)
    hits = 0
    misses: list[str] = []
    for idx, sid in enumerate(ALL_IDS, start=1):
        print(f"[{idx:3}/{total}] {sid} ... ", end="", flush=True)
        try:
            profile = await _build_profile(sid)
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            continue
        if profile is None:
            print("not found")
            misses.append(sid)
            continue
        profiles[sid] = profile
        hits += 1
        src = profile.get("source", "?")
        has_img = "img" if profile.get("photoDataUri") else "no-img"
        print(f"{src} / {has_img}")

    ordered = dict(sorted(profiles.items()))
    with OUTPUT.open("w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    print()
    print(f"Wrote {len(ordered)} entries to {OUTPUT.relative_to(_ROOT)}")
    print(f"Resolved {hits}/{total}; {len(misses)} misses.")
    if misses:
        for sid in misses:
            print(f"  - {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
