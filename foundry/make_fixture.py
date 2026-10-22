#!/usr/bin/env python3
"""make_fixture.py - shrink the prototype dump to a hermetic test fixture.

Keeps the recipes reachable from one demand set, the prototypes those
recipes place, and the technologies whose research units use only the kept
science packs. The result is small enough to commit, so `bazel test` never
reads `~/.factorio` or runs the game.

usage: make_fixture.py <data-raw-dump.json> <out.json>
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solve

# Demand that defines the kept recipe set: one rocket per minute of science.
DEMAND = {
    "automation-science-pack": 10.0,
    "logistic-science-pack": 10.0,
    "military-science-pack": 10.0,
    "chemical-science-pack": 10.0,
    "production-science-pack": 10.0,
    "utility-science-pack": 10.0,
    "space-science-pack": 10.0,
    "rocket-part": 50.0,
}

# Prototype tables the tools read. Belts, undergrounds, inserters, and poles
# live in their own top-level tables in 2.0. The PDK tech layer needs them.
TABLES = [
    "recipe",
    "item",
    "fluid",
    "assembling-machine",
    "furnace",
    "rocket-silo",
    "lab",
    "mining-drill",
    "resource",
    "technology",
    "transport-belt",
    "underground-belt",
    "splitter",
    "inserter",
    "electric-pole",
    "storage-tank",
    "pipe",
    "pipe-to-ground",
    "cargo-landing-pad",
    "boiler",
    "generator",
    "pump",
    "radar",
    "lamp",
    "arithmetic-combinator",
    "decider-combinator",
]
TRANSPORT_TABLES = frozenset(TABLES[TABLES.index("transport-belt") :])
BYTES_PER_KIB = 1024


def _collect_kept_names(
    recipes: solve.RecipeMap,
    data: solve.Data,
    demand: dict[str, float],
) -> set[str]:
    """Collect entity and item names that kept recipes place or produce."""
    keep: set[str] = set()
    for recipe in recipes.values():
        proto_machine = data.machine_for(recipe)
        if proto_machine:
            keep.add(proto_machine[0])
        entries = solve.Data.results_of(recipe)
        ingredients = solve.Data.ingredients_of(recipe)
        if isinstance(ingredients, dict):
            # Degenerate placeholder shapes. Nothing to keep.
            continue
        for entry in ingredients + entries:
            keep.add(solve.name_of(entry))
    keep |= set(demand) | set(solve.PLANET_RAW["nauvis"])
    return keep


def _filter_technologies(
    raw_techs: dict[str, dict[str, object]],
    packs: set[str],
) -> dict[str, dict[str, object]]:
    """Keep technologies whose research unit requires only kept science packs."""
    techs: dict[str, dict[str, object]] = {}
    for name, tech in raw_techs.items():
        unit = tech.get("unit")
        if isinstance(unit, dict):
            ingredients = unit.get("ingredients", [])
            if isinstance(ingredients, list) and all(
                solve.name_of(e) in packs for e in ingredients
            ):
                techs[name] = tech
    return techs


def main() -> int:
    """Build a minimal prototype dump fixture from a full Factorio data dump."""
    src, out = sys.argv[1], sys.argv[2]
    with Path(src).open() as stream:
        raw = json.load(stream)

    data = solve.Data(src)
    planets = list(solve.PLANET_RAW)
    allowed = data.allowed_recipes(planets)
    demand = {k: v for k, v in DEMAND.items() if k in data.recipes}
    recipes, _items = solve.closure(data, demand, allowed)
    kept_names = _collect_kept_names(recipes, data, demand)

    proto = {}
    for table in TABLES:
        full = raw.get(table, {})
        if table == "recipe":
            proto[table] = {n: p for n, p in full.items() if n in recipes}
        elif table in TRANSPORT_TABLES:
            # Small tables. The tech layer needs them whole.
            proto[table] = full
        elif table != "technology":
            proto[table] = {n: p for n, p in full.items() if n in kept_names}

    # Technologies whose research unit asks only for kept packs.
    packs = set(demand) & set(raw.get("item", {}))
    techs = _filter_technologies(raw.get("technology", {}), packs)
    proto["technology"] = techs

    # Keep all base-game achievement tables so achievement rules and limits
    # are available hermetically in the fixture.
    for key, table_data in raw.items():
        if key.endswith("achievement") and isinstance(table_data, dict):
            base_entries = {
                name: entry
                for name, entry in table_data.items()
                if isinstance(entry, dict) and "__base__" in str(entry.get("icon", ""))
            }
            if base_entries:
                proto[key] = base_entries

    Path(out).write_text(json.dumps(proto))
    print(
        f"fixture: {out} {Path(out).stat().st_size / BYTES_PER_KIB:.0f} KiB, "
        f"{len(recipes)} recipes, {len(techs)} technologies"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
