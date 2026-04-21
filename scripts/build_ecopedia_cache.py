"""Pre-build ``data/ecopedia.json`` from Wikidata + Wikipedia.

This is the build-time companion to ``src/eco_mcp_app/_preload.py``. It
resolves a curated list of vanilla Eco items against Wikidata / Wikipedia
once, inlines their images as ``data:`` URIs (per claude-ai-mcp#40 CSP),
and writes the fully-resolved ``EcopediaCard`` dicts to
``data/ecopedia.json`` keyed by ``{category_or_empty}::{name_lower}``.

Run it after pulling new game content or when Wikipedia summaries drift.
Commit the resulting JSON so ``explain_eco_item`` is offline-first and
doesn't spend first-use latency on Wikidata's aggressive rate limiter.

Usage::

    uv run python scripts/build_ecopedia_cache.py

The script is network-bound (SPARQL + Wikipedia + Wikimedia images) and
takes 2-3 minutes on a warm connection. It will not overwrite entries
that come back ``not_found``; pre-existing entries for those names stay.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make `src/` importable without `pip install -e .`.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

# Disable the preload so build_ecopedia_card runs live network fetches
# even when data/ecopedia.json already has an entry.
os.environ["ECO_MCP_PRELOAD_DISABLE"] = "1"

from eco_mcp_app._preload import ecopedia_key  # noqa: E402
from eco_mcp_app.wikidata import build_ecopedia_card  # noqa: E402

OUTPUT = _ROOT / "data" / "ecopedia.json"

# Curated vanilla Eco item list. Each entry is (name, category_or_None).
# `category` maps to `CATEGORY_INSTANCE_OF` in wikidata.py — only names
# that resolve cleanly to a Wikidata class (chemical element / taxon /
# mineral / food) belong under a non-null category. Everything else goes
# through the categoryless Wikipedia-first path.
#
# Scope: ~common raw materials, the ~dozen foods players ask about most,
# and the full vanilla animal + plant species list. Crafted items (gears,
# wiring, etc.) are intentionally omitted — they have no useful Wikidata
# entry and the Wikipedia fallback returns disambiguation noise.
ITEMS: list[tuple[str, str | None]] = [
    # Chemical elements — Eco's "material" category maps to Q11344.
    ("Iron", "material"),
    ("Copper", "material"),
    ("Gold", "material"),
    ("Sulfur", "material"),
    # Minerals / rocks — Q7946.
    ("Basalt", "mineral"),
    ("Granite", "mineral"),
    ("Sandstone", "mineral"),
    ("Shale", "mineral"),
    ("Limestone", "mineral"),
    ("Gneiss", "mineral"),
    # Raw / common foods — Q2095.
    ("Wheat", "food"),
    ("Corn", "food"),
    ("Beans", "food"),
    ("Rice", "food"),
    ("Tomato", "food"),
    ("Beetroot", "food"),
    ("Pumpkin", "food"),
    ("Bread", "food"),
    # Vanilla animals (Q16521 taxon). Names are the cleaned display form —
    # see `clean_species_name` in species.py.
    ("Agouti", "animal"),
    ("Alligator", "animal"),
    ("Bass", "animal"),
    ("Bighorn Sheep", "animal"),
    ("Bison", "animal"),
    ("Blue Shark", "animal"),
    ("Cod", "animal"),
    ("Coyote", "animal"),
    ("Crab", "animal"),
    ("Deer", "animal"),
    ("Elk", "animal"),
    ("Fox", "animal"),
    ("Hare", "animal"),
    ("Jaguar", "animal"),
    ("Moon Jellyfish", "animal"),
    ("Mountain Goat", "animal"),
    ("Otter", "animal"),
    ("Pacific Sardine", "animal"),
    ("Prairie Dog", "animal"),
    ("Salmon", "animal"),
    ("Snapping Turtle", "animal"),
    ("Tarantula", "animal"),
    ("Tortoise", "animal"),
    ("Trout", "animal"),
    ("Tuna", "animal"),
    ("Turkey", "animal"),
    ("Wolf", "animal"),
    # Vanilla plants (Q16521 taxon).
    ("Agave", "plant"),
    ("Amanita", "plant"),
    ("Arctic Willow", "plant"),
    ("Barrel Cactus", "plant"),
    ("Beans", "plant"),
    ("Beetroot", "plant"),
    ("Big Bluestem", "plant"),
    ("Birch", "plant"),
    ("Bolete", "plant"),
    ("Bulrush", "plant"),
    ("Bunchgrass", "plant"),
    ("Buttonbush", "plant"),
    ("Camas", "plant"),
    ("Cedar", "plant"),
    ("Ceiba", "plant"),
    ("Clam", "plant"),  # Eco classifies clams as a plant species layer.
    ("Creosote Bush", "plant"),
    ("Crimini", "plant"),
    ("Daisy", "plant"),
    ("Reindeer Lichen", "plant"),
    ("Dwarf Willow", "plant"),
    ("Fern", "plant"),
    ("Fir", "plant"),
    ("Fireweed", "plant"),
    ("Flax", "plant"),
    ("Heliconia", "plant"),
    ("Huckleberry", "plant"),
    ("Ephedra", "plant"),
    ("Joshua Tree", "plant"),
    ("Kelp", "plant"),
    ("King Fern", "plant"),
    ("Lupine", "plant"),
    ("Oak", "plant"),
    ("Ocean Spray", "plant"),
    ("Redwood", "plant"),
    ("Orchid", "plant"),
    ("Palm", "plant"),
    ("Papaya", "plant"),
    ("Peat Moss", "plant"),
    ("Pineapple", "plant"),
    ("Pitcher Plant", "plant"),
    ("Prickly Pear", "plant"),
    ("Pumpkin", "plant"),
    ("Rice", "plant"),
    ("Rose", "plant"),
    ("Saguaro", "plant"),
    ("Salal", "plant"),
    ("Saxifrage", "plant"),
    ("Seagrass", "plant"),
    ("Spruce", "plant"),
    ("Sunflower", "plant"),
    ("Switchgrass", "plant"),
    ("Taro", "plant"),
    ("Tomato", "plant"),
    ("Trillium", "plant"),
    ("Tulip", "plant"),
    ("Urchin", "plant"),
    ("Waterweed", "plant"),
    ("Wheat", "plant"),
    ("White Bursage", "plant"),
]


def _load_existing() -> dict:
    if OUTPUT.exists():
        try:
            with OUTPUT.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


async def _resolve_one(name: str, category: str | None) -> dict | None:
    card = await build_ecopedia_card(name, category)
    if card.not_found:
        return None
    return card.to_dict()


async def _main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_existing()
    cards = dict(existing)  # Preserve prior hits; only overwrite on success.

    total = len(ITEMS)
    hits = 0
    misses: list[tuple[str, str | None]] = []
    for idx, (name, category) in enumerate(ITEMS, start=1):
        key = ecopedia_key(name, category)
        print(f"[{idx:3}/{total}] {key} ... ", end="", flush=True)
        try:
            card = await _resolve_one(name, category)
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            continue
        if card is None:
            print("not found")
            misses.append((name, category))
            continue
        cards[key] = card
        hits += 1
        has_img = "img" if card.get("image_data_uri") else "no-img"
        print(f"{card.get('source', '?')} / {has_img}")

    # Sort keys for reproducible diffs.
    ordered = dict(sorted(cards.items()))
    with OUTPUT.open("w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    print()
    print(f"Wrote {len(ordered)} entries to {OUTPUT.relative_to(_ROOT)}")
    print(f"Resolved {hits}/{total} new lookups; {len(misses)} misses.")
    if misses:
        print("Misses:")
        for n, c in misses:
            print(f"  - {n} ({c})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
