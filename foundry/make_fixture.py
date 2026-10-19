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


def main() -> int:
    src, out = sys.argv[1], sys.argv[2]
    with Path(src).open() as stream:
        raw = json.load(stream)

    data = solve.Data(src)
    planets = list(solve.PLANET_RAW)
    allowed = data.allowed_recipes(planets)
    demand = {k: v for k, v in DEMAND.items() if k in data.recipes}
    recipes, _items = solve.closure(data, demand, allowed)

    proto = {}
    for table in TABLES:
        full = raw.get(table, {})
        if table == "recipe":
            keep = set(recipes) & set(full)
        elif table in TRANSPORT_TABLES:
            # Small tables. The tech layer needs them whole.
            proto[table] = full
            continue
        elif table == "technology":
            keep = None  # the code fills this set below, from the packs
        else:
            # Names any kept recipe can place or produce.
            keep = set()
            for name in recipes:
                recipe = recipes[name]
                proto_machine = data.machine_for(recipe)
                if proto_machine:
                    keep.add(proto_machine[0])
                entries = solve.Data.results_of(recipe)
                ingredients = solve.Data.ingredients_of(recipe)
                if isinstance(ingredients, dict):
                    # Degenerate placeholder shapes. Nothing to keep.
                    continue
                for entry in ingredients + entries:
                    item = solve.name_of(entry)
                    keep.add(item)
            keep |= set(demand) | set(solve.PLANET_RAW["nauvis"])
        if keep is not None:
            proto[table] = {n: p for n, p in full.items() if n in keep}

    # Technologies whose research unit asks only for kept packs.
    packs = set(demand) & set(raw.get("item", {}))
    techs = {}
    for name, tech in raw.get("technology", {}).items():
        unit = tech.get("unit")
        if unit and all(solve.name_of(e) in packs for e in unit["ingredients"]):
            techs[name] = tech
    proto["technology"] = techs

    Path(out).write_text(json.dumps(proto))
    print(
        f"fixture: {out} {Path(out).stat().st_size / 1024:.0f} KiB, "
        f"{len(recipes)} recipes, {len(techs)} technologies"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
