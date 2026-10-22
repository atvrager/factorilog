"""Factorio production solver.

Reads a prototype dump (`factorio --dump-data`). Writes a plan: recipe rates,
machine counts, raw rates, and the research order.

No search and no evolution. The recipe graph fixes the answer. A linear
program covers the only open choice: where a product has several recipes, or
where a recipe yields several products.

usage:
  solve.py item <item> <per_min> [--unlocked-by TECH] [--planets nauvis]
  solve.py tech <technology> <minutes> [--unlocked-by TECH]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeAlias

import numpy as np
from scipy.optimize import linprog

ProtoDict: TypeAlias = dict[str, Any]
ProtoRecord: TypeAlias = Mapping[str, Any]
EntryData: TypeAlias = Sequence[Any] | Mapping[str, Any]
RateMap: TypeAlias = dict[str, float]
RecipeMap: TypeAlias = dict[str, ProtoDict]
MachineLine: TypeAlias = tuple[str, float, str, float, float]

# Where `factorio --dump-data` writes the prototype dump.
DEFAULT_DUMP = os.path.expanduser("~/.factorio/script-output/data-raw-dump.json")

# Machines that the plan may use, best first within one recipe category.
MACHINE_PREFERENCE = [
    "assembling-machine-3",
    "assembling-machine-2",
    "assembling-machine-1",
    "electric-furnace",
    "steel-furnace",
    "stone-furnace",
    "foundry",
    "electromagnetic-plant",
    "chemical-plant",
    "oil-refinery",
    "centrifuge",
    "biochamber",
    "cryogenic-plant",
    "crusher",
    "recycler",
    "rocket-silo",
    "boiler",
    "steam-engine",
    "lab",
]

MACHINE_PROTO_CATEGORIES = (
    "assembling-machine",
    "furnace",
    "rocket-silo",
    "boiler",
    "generator",
    "lab",
)

# Items that the ground of one planet supplies without a recipe.
PLANET_RAW = {
    "nauvis": {
        "iron-ore",
        "copper-ore",
        "coal",
        "stone",
        "uranium-ore",
        "crude-oil",
        "water",
        "wood",
        "raw-fish",
    },
    "vulcanus": {
        "iron-ore",
        "copper-ore",
        "coal",
        "stone",
        "calcite",
        "sulfuric-acid",
        "lava",
        "water",
        "tungsten-ore",
    },
    "fulgora": {"scrap", "holmium-ore", "heavy-oil", "ice", "water"},
    "gleba": {"water", "stone", "yumako-fruit", "jellynut", "carbon"},
    "aquilo": {"ammonia", "lithium-brine", "water", "crude-oil", "fluorine"},
    "space": {
        "metallic-asteroid-chunk",
        "carbonic-asteroid-chunk",
        "oxide-asteroid-chunk",
        "ice",
        "carbon",
        "water",
    },
}

# Weight of one raw item in the objective. Water and steam are nearly free.
RAW_WEIGHT = {"water": 0.0, "steam": 0.0}

# Weight of one item that the plan produces and cannot use.
SURPLUS_WEIGHT = 0.05

# Category of a recipe that declares none.
DEFAULT_RECIPE_CATEGORY = ["crafting"]

SECONDS_PER_MINUTE = 60.0
TICKS_PER_SECOND = 60.0
WATTS_PER_KW = 1000.0
DEFAULT_CRAFT_SECONDS = 0.5
DEFAULT_TECH_UNIT_SECONDS = 60.0
ELECTRIC_DRAIN_DIVISOR = 30.0
DEFAULT_INSERTER_ROTATION_SPEED = 0.04
DEFAULT_INSERTER_EXTENSION_SPEED = 0.1
INSERTER_EXTENSION_WEIGHT = 0.25
MULTI_BLOCK_THRESHOLD = 4
DEFAULT_CHASSIS_PITCH = 100
DEFAULT_CHASSIS_CORE_DIM = 94
WIDE_CHANNEL_WIDTH = 8
STANDARD_CHANNEL_WIDTH = 6
TIE_BREAK_WEIGHT = 1e-6
RATE_EPSILON = 1e-6
NET_YIELD_EPSILON = 1e-9
MACHINE_COUNT_INDEX = 3
MAX_BAD_ITEMS_SHOWN = 10

UNIT_SCALE_MAP: dict[str, float] = {
    "": 1.0,
    "k": 1000.0,
    "M": 1000.0 * 1000.0,
    "G": 1000.0 * 1000.0 * 1000.0,
    "T": 1000.0 * 1000.0 * 1000.0 * 1000.0,
}

# Offshore pump output, water per minute.
PUMP_PER_MIN = 1200.0 * SECONDS_PER_MINUTE


def power_watts(text: str | float) -> float:
    """Convert a power string such as '375kW' to Watts."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"([0-9.]+)([kMGT]?)[WJ]", str(text))
    if m is None:
        raise ValueError(f"no power in {text!r}")
    return float(m.group(1)) * UNIT_SCALE_MAP[m.group(2)]


def energy_joules(text: str | float) -> float:
    """Convert an energy string such as '4MJ' or '2kJ' to Joules."""
    return power_watts(text)


def amount_of(entry: EntryData) -> float:
    """Return expected amount per craft for one ingredient or result entry."""
    if isinstance(entry, Mapping):
        if "amount" in entry:
            chance = float(entry.get("probability", 1.0))
            chance *= float(entry.get("independent_probability", 1.0))
            return float(entry["amount"]) * chance
        if "amount_min" in entry:
            return (float(entry["amount_min"]) + float(entry["amount_max"])) / 2.0
        raise ValueError(f"no amount in {entry}")
    return float(entry[1])


def name_of(entry: EntryData) -> str:
    """Return the prototype name of one ingredient or result entry."""
    return str(entry["name"]) if isinstance(entry, Mapping) else str(entry[0])


class Data:
    """Container for Factorio prototype data and synthesized recipes."""

    def __init__(self, path: str | Path) -> None:
        """Load prototype dump from path and synthesize power, silo, and lab recipes."""
        with open(path) as stream:
            raw: ProtoDict = json.load(stream)
        self.recipes: RecipeMap = {
            name: recipe
            for name, recipe in raw["recipe"].items()
            if "results" in recipe or "result" in recipe
        }
        self.techs: ProtoDict = raw["technology"]
        self.items: set[str] = set(raw["item"]) | set(raw["fluid"])
        self.resources: ProtoDict = raw.get("resource", {})
        self.prototypes: ProtoDict = raw
        self.machines: RecipeMap = self._load_machines(raw)
        self.drills: ProtoDict = raw.get("mining-drill", {})
        self.ground: set[str] = set()

        self._synthesize_rocket_silo_recipes(raw)
        self._synthesize_steam_power_recipes(raw)
        self.producers: dict[str, list[str]] = self._build_producers_map()
        self.unlocked_by: dict[str, str] = self._build_unlock_map()
        self._synthesize_infinite_research_recipe(raw)

    @staticmethod
    def _load_machines(raw: ProtoDict) -> RecipeMap:
        """Collect all machine prototypes across crafting, furnace, power, and lab types."""
        machines: RecipeMap = {}
        for proto_type in MACHINE_PROTO_CATEGORIES:
            for m_name, m_proto in raw.get(proto_type, {}).items():
                machines[m_name] = dict(m_proto)
        return machines

    def _synthesize_silo_item_launches(
        self,
        raw: ProtoDict,
        fixed_name: str,
        part_recipe: ProtoDict,
        crafts_per_launch: float,
        part_categories: list[str],
        part_seconds: float,
    ) -> None:
        """Register synthetic launch recipes for items with rocket_launch_products."""
        base_ings = [
            {
                "type": ing.get("type", "item") if isinstance(ing, Mapping) else "item",
                "name": name_of(ing),
                "amount": round(amount_of(ing) * crafts_per_launch),
            }
            for ing in self.ingredients_of(part_recipe)
        ]
        for item_name, item_proto in raw.get("item", {}).items():
            launch_products = item_proto.get("rocket_launch_products")
            if not launch_products:
                continue
            launch_ings = [*base_ings, {"type": "item", "name": item_name, "amount": 1}]
            for prod in launch_products:
                prod_name = name_of(prod)
                if prod_name not in self.recipes:
                    self.recipes[prod_name] = {
                        "type": "recipe",
                        "name": prod_name,
                        "categories": list(part_categories),
                        "energy_required": part_seconds * crafts_per_launch,
                        "ingredients": launch_ings,
                        "results": list(launch_products),
                        "fixed_recipe": fixed_name,
                    }

    def _synthesize_rocket_silo_recipes(self, raw: ProtoDict) -> None:
        """Synthesize rocket launch recipes dynamically from rocket-silo and item prototypes."""
        for silo_proto in raw.get("rocket-silo", {}).values():
            fixed_name = silo_proto.get("fixed_recipe")
            parts_req = silo_proto.get("rocket_parts_required")
            if not fixed_name or not parts_req or fixed_name not in self.recipes:
                continue
            part_recipe = self.recipes[fixed_name]
            part_results = self.results_of(part_recipe)
            parts_per_craft = (
                sum(amount_of(r) for r in part_results if name_of(r) == fixed_name) or 1.0
            )
            crafts_per_launch = float(parts_req) / parts_per_craft
            part_categories = part_recipe.get("categories") or silo_proto.get(
                "crafting_categories", ["rocket-building"]
            )
            part_seconds = self.craft_seconds(part_recipe)
            self._synthesize_silo_item_launches(
                raw, fixed_name, part_recipe, crafts_per_launch, part_categories, part_seconds
            )

    def _synthesize_steam_power_recipes(self, raw: ProtoDict) -> None:
        """Synthesize boiler-steam and steam-power recipes dynamically from prototypes."""
        boiler_proto = raw.get("boiler", {}).get("boiler")
        se_proto = raw.get("generator", {}).get("steam-engine")
        water_proto = raw.get("fluid", {}).get("water")
        steam_proto = raw.get("fluid", {}).get("steam")
        coal_proto = raw.get("item", {}).get("coal")
        if not (boiler_proto and se_proto and water_proto and steam_proto and coal_proto):
            return

        self.machines["boiler"]["crafting_categories"] = ["boiler-steam"]
        self.machines["boiler"]["crafting_speed"] = 1.0
        self.machines["steam-engine"]["crafting_categories"] = ["steam-power"]
        self.machines["steam-engine"]["crafting_speed"] = 1.0

        water_temp = float(water_proto["default_temperature"])
        steam_temp = float(steam_proto["default_temperature"])
        target_temp = float(boiler_proto["target_temperature"])
        water_hc = energy_joules(water_proto["heat_capacity"])
        steam_hc = energy_joules(steam_proto["heat_capacity"])
        boiler_power = power_watts(boiler_proto["energy_consumption"])
        boiler_eff = float(boiler_proto.get("energy_source", {}).get("effectivity", 1.0))
        coal_fuel = energy_joules(coal_proto["fuel_value"])

        water_rate = boiler_power / ((target_temp - water_temp) * water_hc)
        steam_rate = boiler_power / ((target_temp - steam_temp) * steam_hc)
        coal_rate = boiler_power / (boiler_eff * coal_fuel)

        self.recipes["boiler-steam"] = {
            "type": "recipe",
            "name": "boiler-steam",
            "categories": ["boiler-steam"],
            "energy_required": 1.0,
            "ingredients": [
                {"type": "fluid", "name": "water", "amount": water_rate},
                {"type": "item", "name": "coal", "amount": coal_rate},
            ],
            "results": [
                {"type": "fluid", "name": "steam", "amount": steam_rate},
            ],
        }

        fluid_per_sec = float(se_proto["fluid_usage_per_tick"]) * TICKS_PER_SECOND
        se_eff = float(se_proto.get("effectivity", 1.0))
        op_temp = min(float(se_proto["maximum_temperature"]), target_temp)
        power_out = fluid_per_sec * (op_temp - steam_temp) * steam_hc * se_eff

        self.recipes["steam-power"] = {
            "type": "recipe",
            "name": "steam-power",
            "categories": ["steam-power"],
            "energy_required": 1.0,
            "ingredients": [
                {"type": "fluid", "name": "steam", "amount": fluid_per_sec},
            ],
            "results": [],
            "power_output_watts": power_out,
        }

    def _build_producers_map(self) -> dict[str, list[str]]:
        """Map each produced item or fluid name to the list of recipes yielding it."""
        producers: dict[str, list[str]] = collections.defaultdict(list)
        for name, recipe in self.recipes.items():
            for res in self.results_of(recipe):
                producers[name_of(res)].append(name)
        return producers

    def _build_unlock_map(self) -> dict[str, str]:
        """Map each recipe name to the first technology that unlocks it."""
        unlocked_by: dict[str, str] = {}
        for name, tech in self.techs.items():
            for effect in tech.get("effects", []):
                if effect.get("type") == "unlock-recipe":
                    unlocked_by.setdefault(effect["recipe"], name)
        for name, recipe in self.recipes.items():
            fixed = recipe.get("fixed_recipe")
            if fixed and name not in unlocked_by and fixed in unlocked_by:
                unlocked_by[name] = unlocked_by[fixed]
        return unlocked_by

    def _nauvis_lab_inputs(self, lab_proto: ProtoDict) -> set[str]:
        """Return the subset of lab inputs producible on Nauvis."""
        nauvis_allowed = self.allowed_recipes(["nauvis"])
        nauvis_producible = set(PLANET_RAW["nauvis"])
        for r_name in nauvis_allowed:
            for entry in self.results_of(self.recipes[r_name]):
                nauvis_producible.add(name_of(entry))
        return set(lab_proto.get("inputs", [])) & nauvis_producible

    def _find_infinite_research_spec(
        self,
        lab_inputs: set[str],
    ) -> tuple[list[ProtoDict], float]:
        """Scan infinite technologies to find the broadest pack ingredient list and unit time."""
        best_inf_ings: list[ProtoDict] = []
        max_inf_time = 0.0
        for tech in self.techs.values():
            unit = tech.get("unit") if tech.get("max_level") == "infinite" else None
            if not unit:
                continue
            ings = unit.get("ingredients", [])
            if not all(name_of(e) in lab_inputs for e in ings):
                continue
            max_inf_time = max(max_inf_time, float(unit.get("time", DEFAULT_TECH_UNIT_SECONDS)))
            if len(ings) > len(best_inf_ings):
                best_inf_ings = [
                    {"type": "item", "name": name_of(e), "amount": amount_of(e)} for e in ings
                ]
        return best_inf_ings, max_inf_time

    def _synthesize_infinite_research_recipe(self, raw: ProtoDict) -> None:
        """Synthesize infinite-research recipe dynamically from lab and technology prototypes."""
        lab_proto = raw.get("lab", {}).get("lab")
        if not lab_proto:
            return
        base_lab_speed = float(lab_proto.get("researching_speed", 1.0))
        self.machines["lab"]["crafting_categories"] = ["lab-research"]
        self.machines["lab"]["crafting_speed"] = base_lab_speed * (1.0 + self.lab_speed_modifier())
        lab_inputs = self._nauvis_lab_inputs(lab_proto)
        best_inf_ings, max_inf_time = self._find_infinite_research_spec(lab_inputs)
        if best_inf_ings and max_inf_time > 0.0:
            self.recipes["infinite-research"] = {
                "type": "recipe",
                "name": "infinite-research",
                "categories": ["lab-research"],
                "energy_required": max_inf_time,
                "ingredients": best_inf_ings,
                "results": [],
            }

    @staticmethod
    def ingredients_of(recipe: ProtoRecord) -> Any:
        """Return the ingredient list of a recipe."""
        return list(recipe.get("ingredients", []))

    @staticmethod
    def results_of(recipe: ProtoRecord) -> Any:
        """Return the result list of a recipe."""
        if "results" in recipe:
            return list(recipe["results"])
        return [{"name": recipe["result"], "amount": recipe.get("result_count", 1)}]

    @staticmethod
    def craft_seconds(recipe: ProtoRecord) -> float:
        """Return the crafting time in seconds of a recipe."""
        return float(recipe.get("energy_required", DEFAULT_CRAFT_SECONDS))

    def machine_for(self, recipe: ProtoRecord) -> Any:
        """Return the highest-preference machine name and crafting speed for recipe."""
        categories = recipe.get("categories") or DEFAULT_RECIPE_CATEGORY
        for name in MACHINE_PREFERENCE:
            proto = self.machines.get(name)
            if proto is None:
                continue
            if set(categories) & set(proto.get("crafting_categories", [])):
                return (name, float(proto.get("crafting_speed", 1.0)))
        return None

    def _walk_tech_prereqs(self, name: str, seen: set[str], order: list[str]) -> None:
        """Depth-first post-order traversal of technology prerequisites."""
        if name in seen:
            return
        seen.add(name)
        for pre in self.techs[name].get("prerequisites", []):
            if pre in self.techs:
                self._walk_tech_prereqs(pre, seen, order)
        order.append(name)

    def tech_order(self, target: str) -> list[str]:
        """Return prerequisites first, then target, in topological research order."""
        order: list[str] = []
        seen: set[str] = set()
        self._walk_tech_prereqs(target, seen, order)
        return order

    def lab_speed_modifier(self, unlocked_by: str | None = None) -> float:
        """Sum laboratory-speed technology modifiers unlocked within closure."""
        if unlocked_by is not None:
            tech_names = self.tech_order(unlocked_by)
        else:
            tech_names = [
                name for name, tech in self.techs.items() if tech.get("max_level") != "infinite"
            ]
        bonus = 0.0
        for name in tech_names:
            tech = self.techs.get(name, {})
            for effect in tech.get("effects", []):
                if effect.get("type") == "laboratory-speed":
                    bonus += float(effect.get("modifier", 0.0))
        return bonus

    def is_full_science_demand(self, demand: RateMap) -> bool:
        """Return True when demand includes all science packs of infinite-research."""
        inf_recipe = self.recipes.get("infinite-research")
        if not inf_recipe:
            return False
        inf_packs = {name_of(e) for e in self.ingredients_of(inf_recipe)}
        if not inf_packs:
            return False
        positive_demands = {k for k, v in demand.items() if v > 0}
        return inf_packs.issubset(positive_demands)

    def _try_unlock_reachable_recipe(
        self,
        name: str,
        recipe: ProtoDict,
        allowed: set[str],
        reachable: set[str],
    ) -> bool:
        """Add recipe to allowed if all ingredients are reachable; return True if state changed."""
        if name in allowed:
            return False
        if any(name_of(e) not in reachable for e in self.ingredients_of(recipe)):
            return False
        allowed.add(name)
        for entry in self.results_of(recipe):
            reachable.add(name_of(entry))
        return True

    def allowed_recipes(
        self,
        planets: list[str],
        unlocked_by: str | None = None,
    ) -> set[str]:
        """Return the set of recipes reachable on planets and unlocked by technology."""
        ground: set[str] = set()
        for planet in planets:
            ground |= PLANET_RAW.get(planet, set())

        self.ground = ground
        reachable = set(ground)
        allowed: set[str] = set()
        changed = True
        while changed:
            changed = any(
                self._try_unlock_reachable_recipe(name, recipe, allowed, reachable)
                for name, recipe in self.recipes.items()
            )

        if unlocked_by is None:
            return allowed

        known = set(self.tech_order(unlocked_by))
        return {
            name
            for name in allowed
            if name not in self.unlocked_by or self.unlocked_by[name] in known
        }


def _seed_closure_recipes(
    data: Data,
    demand: RateMap,
    allowed: set[str],
) -> tuple[RecipeMap, set[str]]:
    """Initialize chosen recipe map and item set with demand, power, and research seeds."""
    items = set(demand)
    chosen: RecipeMap = {}
    if "steam-power" in allowed and "boiler-steam" in allowed and "steam-power" in data.recipes:
        chosen["steam-power"] = data.recipes["steam-power"]
        chosen["boiler-steam"] = data.recipes["boiler-steam"]
        items.update(["steam", "water", "coal"])
    if (
        "infinite-research" in allowed
        and "infinite-research" in data.recipes
        and data.is_full_science_demand(demand)
    ):
        chosen["infinite-research"] = data.recipes["infinite-research"]
    return chosen, items


def _add_recipe_to_closure(
    data: Data,
    recipe_name: str,
    chosen: RecipeMap,
    items: set[str],
) -> bool:
    """Insert recipe into chosen and record its items; return True if new items were added."""
    recipe = data.recipes[recipe_name]
    chosen[recipe_name] = recipe
    prev_len = len(items)
    for entry in data.ingredients_of(recipe):
        items.add(name_of(entry))
    for entry in data.results_of(recipe):
        items.add(name_of(entry))
    return len(items) > prev_len


def _expand_item_producers(
    data: Data,
    item: str,
    allowed: set[str],
    chosen: RecipeMap,
    items: set[str],
) -> bool:
    """Add all allowed producer recipes for item to chosen."""
    changed = False
    for recipe_name in data.producers.get(item, []):
        if recipe_name not in allowed or recipe_name in chosen:
            continue
        if recipe_name == "coal-liquefaction" and "crude-oil" in data.ground:
            continue
        if _add_recipe_to_closure(data, recipe_name, chosen, items):
            changed = True
    return changed


def closure(
    data: Data,
    demand: RateMap,
    allowed: set[str],
) -> tuple[RecipeMap, set[str]]:
    """Return every recipe that may enter the plan and every item it touches."""
    chosen, items = _seed_closure_recipes(data, demand, allowed)
    changed = True
    while changed:
        changed = any(
            _expand_item_producers(data, item, allowed, chosen, items) for item in list(items)
        )
    return chosen, items


def machine_peak_watts(m_proto: ProtoDict) -> float:
    """Peak electric power draw of one crafting machine, including passive drain."""
    es = m_proto.get("energy_source", {})
    if es.get("type") == "burner":
        return 0.0
    active = power_watts(m_proto.get("active_energy_usage") or m_proto.get("energy_usage", 0))
    if active <= 0.0:
        return 0.0
    if "drain" in es:
        drain = power_watts(es["drain"])
    elif es.get("type") == "electric":
        drain = active / ELECTRIC_DRAIN_DIVISOR
    else:
        drain = 0.0
    return active + drain


def inserter_peak_watts(data: Data, ins_name: str = "fast-inserter") -> float:
    """Active electric power draw of one inserter from prototype properties."""
    ins_proto = data.prototypes.get("inserter", {}).get(ins_name, {})
    es = ins_proto.get("energy_source", {})
    if es.get("type") == "burner":
        return 0.0
    rot_j = energy_joules(
        ins_proto.get("energy_per_rotation") or ins_proto.get("energy_per_movement", "7kJ")
    )
    ext_j = energy_joules(ins_proto.get("energy_per_movement", "7kJ"))
    rot_s = float(ins_proto.get("rotation_speed", DEFAULT_INSERTER_ROTATION_SPEED))
    ext_s = float(ins_proto.get("extension_speed", DEFAULT_INSERTER_EXTENSION_SPEED))
    drain = power_watts(es.get("drain", 0)) if "drain" in es else 0.0
    return (rot_j * rot_s + INSERTER_EXTENSION_WEIGHT * ext_j * ext_s) * TICKS_PER_SECOND + drain


def _resolve_chassis_for_grid(grid_w: int, grid_h: int) -> Any:
    """Construct or load a CityBlockChassis appropriate for grid_w x grid_h."""
    from foundry.chassis import CityBlockChassis

    if grid_w * grid_h >= MULTI_BLOCK_THRESHOLD:
        return CityBlockChassis(
            pitch=DEFAULT_CHASSIS_PITCH,
            channel_width=WIDE_CHANNEL_WIDTH,
            programmatic=True,
        )
    for cand in (
        Path("cityblock.bp"),
        Path(__file__).resolve().parent.parent / "cityblock.bp",
    ):
        if cand.is_file():
            try:
                return CityBlockChassis.from_blueprint_file(cand)
            except Exception:
                pass
    return CityBlockChassis(
        pitch=DEFAULT_CHASSIS_PITCH,
        core_width=DEFAULT_CHASSIS_CORE_DIM,
        core_height=DEFAULT_CHASSIS_CORE_DIM,
        channel_width=STANDARD_CHANNEL_WIDTH,
        programmatic=True,
    )


def _entity_chassis_watts(
    ename: str,
    radars: ProtoDict,
    lamps: ProtoDict,
) -> float:
    """Return power draw in Watts for one radar or lamp entity."""
    if ename in radars:
        return power_watts(radars[ename].get("energy_usage", 0))
    if ename in lamps:
        val_str = str(lamps[ename].get("energy_usage_per_tick", 0)).strip()
        if val_str.endswith("J"):
            return energy_joules(val_str) * TICKS_PER_SECOND
        return power_watts(val_str)
    return 0.0


def chassis_power_watts(data: Data, grid_w: int, grid_h: int) -> float:
    """Total electric power draw of chassis base entities (radars and lamps) for a grid_w x grid_h chassis."""
    if grid_w <= 0 or grid_h <= 0:
        return 0.0
    chassis = _resolve_chassis_for_grid(grid_w, grid_h)
    base_bp = chassis.emit_base_blueprint(grid_w, grid_h)
    entities = base_bp.get("blueprint", {}).get("entities", [])
    radars = data.prototypes.get("radar", {})
    lamps = data.prototypes.get("lamp", {})
    return sum(_entity_chassis_watts(ent.get("name", ""), radars, lamps) for ent in entities)


def _build_stoichiometry_matrix(
    data: Data,
    recipes: RecipeMap,
    names: list[str],
    item_list: list[str],
    index: dict[str, int],
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build net stoichiometry matrix, cost vector, and raw resource list."""
    net = np.zeros((len(item_list), len(names)))
    cost = np.zeros(len(names))
    for col, name in enumerate(names):
        recipe = recipes[name]
        for entry in data.results_of(recipe):
            net[index[name_of(entry)], col] += amount_of(entry)
        for entry in data.ingredients_of(recipe):
            net[index[name_of(entry)], col] -= amount_of(entry)
        cost[col] = TIE_BREAK_WEIGHT * Data.craft_seconds(recipe)

    raw_names = [
        name
        for name in item_list
        if name in data.ground or not (net[index[name]] > NET_YIELD_EPSILON).any()
    ]
    net = np.hstack([net, np.zeros((len(item_list), len(raw_names)))])
    cost = np.concatenate([cost, np.array([RAW_WEIGHT.get(n, 1.0) for n in raw_names])])
    for j, name in enumerate(raw_names):
        col = len(names) + j
        net[index[name], col] += 1.0
        minable = data.resources.get(name, {}).get("minable", {})
        fluid = minable.get("required_fluid")
        if fluid and fluid in index:
            net[index[fluid], col] -= float(minable.get("fluid_amount", 0))
    return net, cost, raw_names


def _apply_infinite_research_bounds(
    data: Data,
    recipes: RecipeMap,
    names: list[str],
    demand: RateMap,
    cols: int,
    rows: int,
) -> tuple[RateMap, list[tuple[float, float | None]]]:
    """Pin infinite-research rate and adjust science demand when full science is requested."""
    effective_demand = dict(demand)
    bounds: list[tuple[float, float | None]] = [(0.0, None)] * (cols + rows)
    if "infinite-research" in recipes and data.is_full_science_demand(demand):
        inf_recipe = recipes["infinite-research"]
        inf_ings = {name_of(e): amount_of(e) for e in data.ingredients_of(inf_recipe)}
        if inf_ings:
            research_rate = min(effective_demand[p] / amt for p, amt in inf_ings.items())
            col_inf = names.index("infinite-research")
            bounds[col_inf] = (research_rate, research_rate)
            for p, amt in inf_ings.items():
                effective_demand[p] = max(0.0, effective_demand[p] - research_rate * amt)
    return effective_demand, bounds


def _recipe_power_coefficient(data: Data, recipe: ProtoDict, ins_watts: float) -> float:
    """Compute net electric energy per craft (in Watt-minutes) for a recipe."""
    gen_watts = float(recipe.get("power_output_watts", 0.0))
    if gen_watts > 0.0:
        return -gen_watts * (Data.craft_seconds(recipe) / SECONDS_PER_MINUTE)
    pick = data.machine_for(recipe)
    if pick is None:
        return 0.0
    machine, speed = pick
    m_proto = data.machines.get(machine, {})
    watts = machine_peak_watts(m_proto)
    solid_ports = sum(
        1
        for e in (data.ingredients_of(recipe) + data.results_of(recipe))
        if not isinstance(e, dict) or e.get("type", "item") != "fluid"
    )
    if watts <= 0.0 and solid_ports == 0:
        return 0.0
    cell_watts = watts + solid_ports * ins_watts
    return (Data.craft_seconds(recipe) / (SECONDS_PER_MINUTE * speed)) * cell_watts


def _build_power_constraints(
    data: Data,
    recipes: RecipeMap,
    names: list[str],
    cols: int,
    rows: int,
    extra_power_watts: float,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Build inequality constraint matrices for self-powered steam generation."""
    if "steam-power" not in recipes or "boiler-steam" not in recipes:
        return None, None
    ins_watts = inserter_peak_watts(data)
    power_row = np.zeros(cols + rows)
    for col, name in enumerate(names):
        power_row[col] = _recipe_power_coefficient(data, recipes[name], ins_watts)
    if (power_row > 0).any() or extra_power_watts > 0.0:
        return power_row.reshape(1, -1), np.array([-float(extra_power_watts)])
    return None, None


def solve_rates(
    data: Data,
    demand: RateMap,
    allowed: set[str],
    extra_power_watts: float = 0.0,
) -> tuple[RateMap, RateMap, RateMap]:
    """Compute recipe rates per minute that meet demand at minimum raw cost."""
    recipes, items = closure(data, demand, allowed)
    names = sorted(recipes)
    item_list = sorted(items)
    index = {name: i for i, name in enumerate(item_list)}
    net, cost, raw_names = _build_stoichiometry_matrix(data, recipes, names, item_list, index)

    rows = len(item_list)
    cols = len(names) + len(raw_names)
    effective_demand, bounds = _apply_infinite_research_bounds(
        data, recipes, names, demand, cols, rows
    )

    a_eq = np.hstack([net, -np.eye(rows)])
    b_eq = np.array([effective_demand.get(item, 0.0) for item in item_list])
    a_ub, b_ub = _build_power_constraints(data, recipes, names, cols, rows, extra_power_watts)

    result = linprog(
        np.concatenate([cost, np.full(rows, SURPLUS_WEIGHT)]),
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    if not result.success:
        raise RuntimeError(result.message)

    rates = {name: float(result.x[i]) for i, name in enumerate(names) if result.x[i] > RATE_EPSILON}
    raw = {
        name: float(result.x[len(names) + j])
        for j, name in enumerate(raw_names)
        if result.x[len(names) + j] > RATE_EPSILON
    }
    surplus = {
        item_list[i]: float(result.x[cols + i])
        for i in range(rows)
        if result.x[cols + i] > RATE_EPSILON
    }
    return rates, raw, surplus


def machine_counts(data: Data, rates: RateMap) -> tuple[list[MachineLine], float]:
    """Compute machines per recipe and total power draw of the machine list."""
    lines: list[MachineLine] = []
    power = 0.0
    for name, rate in sorted(rates.items(), key=lambda kv: -kv[1]):
        recipe = data.recipes[name]
        pick = data.machine_for(recipe)
        if pick is None:
            lines.append((name, rate, "no machine", 0.0, 0.0))
            continue
        machine, speed = pick
        count = rate / SECONDS_PER_MINUTE / speed * Data.craft_seconds(recipe)
        watts = power_watts(data.machines[machine].get("energy_usage", 0))
        power += count * watts
        lines.append((name, rate, machine, count, count * watts))
    return lines, power


def print_plan(
    data: Data,
    title: str,
    rates: RateMap,
    raw: RateMap,
    surplus: RateMap,
) -> None:
    """Print formatted production plan, machine counts, raw extraction, and surplus."""
    lines, power = machine_counts(data, rates)
    print(f"\n=== {title} ===")
    print(f"{'recipe':34} {'crafts/min':>9}  {'machine':22} {'count':>7} {'kW':>9}")
    for name, rate, machine, count, watts in lines:
        print(f"{name:34} {rate:9.2f}  {machine:22} {count:7.2f} {watts / WATTS_PER_KW:9.1f}")
    total_machines = sum(line[MACHINE_COUNT_INDEX] for line in lines)
    print(f"{'TOTAL':34} {'':9}  {'':22} {total_machines:7.2f} {power / WATTS_PER_KW:9.1f}")

    print(f"\n{'raw':28} {'/min':>9}  {'source':22} {'count':>7}")
    for name, rate in sorted(raw.items(), key=lambda kv: -kv[1]):
        resource = data.resources.get(name, {})
        minable = resource.get("minable", {})
        if name == "crude-oil":
            pump = float(data.drills["pumpjack"]["mining_speed"]) * SECONDS_PER_MINUTE
            print(f"{name:28} {rate:9.1f}  {'pumpjack (100%)':22} {rate / pump:7.2f}")
        elif name == "water":
            print(f"{name:28} {rate:9.1f}  {'offshore-pump':22} {rate / PUMP_PER_MIN:7.2f}")
        elif "mining_time" in minable:
            drill = data.drills["electric-mining-drill"]
            per_min = (
                float(drill["mining_speed"]) * SECONDS_PER_MINUTE / float(minable["mining_time"])
            )
            print(f"{name:28} {rate:9.1f}  {'electric-mining-drill':22} {rate / per_min:7.2f}")
        else:
            print(f"{name:28} {rate:9.1f}  {'(free input)':22} {'-':>7}")
    if surplus:
        print(
            "\nbyproduct surplus (/min): " + ", ".join(f"{k} {v:.2f}" for k, v in surplus.items())
        )


def research_plan(data: Data, target: str, minutes: float) -> RateMap:
    """Compute prerequisite technologies and science pack demand per minute for target."""
    order = data.tech_order(target)
    packs: dict[str, float] = collections.defaultdict(float)
    lab_seconds = 0.0
    for name in order:
        unit = data.techs[name].get("unit")
        if not unit:
            continue
        for entry in unit["ingredients"]:
            packs[name_of(entry)] += amount_of(entry) * unit["count"]
        lab_seconds += unit["count"] * unit["time"]
    print(f"\n=== research: {target} ===")
    print(f"technologies: {len(order)}  lab-seconds: {lab_seconds:,.0f}")
    for name in order:
        unit = data.techs[name].get("unit")
        text = (
            "free"
            if not unit
            else " ".join(
                f"{name_of(e)}x{amount_of(e) * unit['count']:.0f}" for e in unit["ingredients"]
            )
        )
        print(f"  {name:42} {text}")
    if minutes <= 0:
        return {}
    demand = {
        item: total / (minutes * SECONDS_PER_MINUTE) * SECONDS_PER_MINUTE
        for item, total in packs.items()
    }
    print(
        f"\nscience packs /min to finish in {minutes:.0f} minutes "
        f"({lab_seconds / (minutes * SECONDS_PER_MINUTE):.1f} labs at full speed):"
    )
    for item, per_min in sorted(demand.items(), key=lambda kv: -kv[1]):
        print(f"  {item:32} {per_min:8.2f}")
    return demand


def _accumulate_flow_totals(
    data: Data,
    rates: RateMap,
) -> tuple[dict[str, float], dict[str, float]]:
    """Sum total production and consumption per item across all active recipes."""
    prod: dict[str, float] = collections.defaultdict(float)
    cons: dict[str, float] = collections.defaultdict(float)
    for name, rate in rates.items():
        recipe = data.recipes[name]
        for entry in data.results_of(recipe):
            prod[name_of(entry)] += amount_of(entry) * rate
        for entry in data.ingredients_of(recipe):
            cons[name_of(entry)] += amount_of(entry) * rate
    return prod, cons


def _adjusted_balance_demand(
    data: Data,
    demand: RateMap,
    rates: RateMap,
) -> RateMap:
    """Subtract infinite-research science consumption from demand for balance check."""
    effective_demand = dict(demand)
    if "infinite-research" not in rates or "infinite-research" not in data.recipes:
        return effective_demand
    r_inf = rates["infinite-research"]
    for entry in data.ingredients_of(data.recipes["infinite-research"]):
        p = name_of(entry)
        if p in effective_demand:
            effective_demand[p] = max(0.0, effective_demand[p] - r_inf * amount_of(entry))
    return effective_demand


def check_balance(
    data: Data,
    demand: RateMap,
    rates: RateMap,
    raw: RateMap,
) -> None:
    """Recompute every item balance from rates and report any discrepancy."""
    prod, cons = _accumulate_flow_totals(data, rates)
    effective_demand = _adjusted_balance_demand(data, demand, rates)
    bad: list[tuple[str, float, float, float]] = []
    for item in set(prod) | set(cons):
        if item in raw:
            continue
        gap = prod[item] - cons[item] - effective_demand.get(item, 0.0)
        if abs(gap) > RATE_EPSILON:
            bad.append((item, prod[item], cons[item], gap))
    print(f"balance check: {len(bad)} items off; {len(raw)} raw items extracted")
    for item, p, c, gap in bad[:MAX_BAD_ITEMS_SHOWN]:
        print(f"  {item:28} produced {p:9.2f} consumed {c:9.2f} gap {gap:9.2f}")


def main() -> None:
    """Parse CLI arguments and run the Factorio production or research solver."""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["item", "tech"])
    parser.add_argument("target")
    parser.add_argument("amount", type=float, default=SECONDS_PER_MINUTE, nargs="?")
    parser.add_argument("--minutes", type=float, default=SECONDS_PER_MINUTE)
    parser.add_argument("--planets", default="nauvis", help="comma separated, or 'all'")
    parser.add_argument("--unlocked-by", default=None)
    parser.add_argument("--dump", default=DEFAULT_DUMP)
    args = parser.parse_args()

    data = Data(args.dump)
    planets = list(PLANET_RAW) if args.planets == "all" else args.planets.split(",")
    allowed = data.allowed_recipes(planets, args.unlocked_by)
    print(
        f"recipes allowed: {len(allowed)} of {len(data.recipes)} "
        f"(planets={args.planets}, unlocked-by={args.unlocked_by or '-'})"
    )

    if args.mode == "item":
        demand = {args.target: args.amount}
        rates, raw, surplus = solve_rates(data, demand, allowed)
        check_balance(data, demand, rates, raw)
        print_plan(data, f"{args.amount:.2f} {args.target} per minute", rates, raw, surplus)
        return

    demand = research_plan(data, args.target, args.minutes)
    rates, raw, surplus = solve_rates(data, demand, allowed)
    check_balance(data, demand, rates, raw)
    print_plan(data, f"science for {args.target} in {args.minutes:.0f} min", rates, raw, surplus)


if __name__ == "__main__":
    main()
