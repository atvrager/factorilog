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

import argparse
import collections
import json
import os
import re

import numpy as np
from scipy.optimize import linprog

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
]

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

# Offshore pump output, water per minute.
PUMP_PER_MIN = 1200.0 * 60.0


def power_watts(text):
    """'375kW' -> 375000.0"""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"([0-9.]+)([kMGT]?)[WJ]", str(text))
    if m is None:
        raise ValueError(f"no power in {text!r}")
    scale = {"": 1.0, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2)]
    return float(m.group(1)) * scale


def energy_joules(text):
    """'4MJ' or '2kJ' -> Joules."""
    return power_watts(text)


def amount_of(entry):
    """One ingredient or result -> amount per craft."""
    if isinstance(entry, list):
        return float(entry[1])
    if "amount" in entry:
        chance = float(entry.get("probability", 1.0))
        chance *= float(entry.get("independent_probability", 1.0))
        return float(entry["amount"]) * chance
    if "amount_min" in entry:
        return (float(entry["amount_min"]) + float(entry["amount_max"])) / 2.0
    raise ValueError(f"no amount in {entry}")


def name_of(entry):
    return entry[0] if isinstance(entry, list) else entry["name"]


class Data:
    def __init__(self, path):
        with open(path) as stream:
            raw = json.load(stream)
        # Drop the blueprint placeholders: they produce nothing.
        self.recipes = {
            name: recipe
            for name, recipe in raw["recipe"].items()
            if "results" in recipe or "result" in recipe
        }
        self.techs = raw["technology"]
        self.items = set(raw["item"]) | set(raw["fluid"])
        self.resources = raw.get("resource", {})
        # Every prototype type, for the tools that place entities.
        self.prototypes = raw
        self.machines = {}
        for proto_type in ("assembling-machine", "furnace", "rocket-silo", "boiler", "generator"):
            for m_name, m_proto in raw.get(proto_type, {}).items():
                self.machines[m_name] = dict(m_proto)
        self.drills = raw.get("mining-drill", {})

        self.ground = set()
        # Synthesize rocket launch recipes dynamically from rocket-silo and item prototypes.
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
            for item_name, item_proto in raw.get("item", {}).items():
                launch_products = item_proto.get("rocket_launch_products")
                if not launch_products:
                    continue
                launch_ings = [
                    {
                        "type": ing.get("type", "item"),
                        "name": name_of(ing),
                        "amount": round(amount_of(ing) * crafts_per_launch),
                    }
                    for ing in self.ingredients_of(part_recipe)
                ]
                launch_ings.append({"type": "item", "name": item_name, "amount": 1})
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

        # Synthesize boiler-steam and steam-power recipes dynamically from prototypes.
        boiler_proto = raw.get("boiler", {}).get("boiler")
        se_proto = raw.get("generator", {}).get("steam-engine")
        water_proto = raw.get("fluid", {}).get("water")
        steam_proto = raw.get("fluid", {}).get("steam")
        coal_proto = raw.get("item", {}).get("coal")
        if boiler_proto and se_proto and water_proto and steam_proto and coal_proto:
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

            fluid_per_sec = float(se_proto["fluid_usage_per_tick"]) * 60.0
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

        self.producers = collections.defaultdict(list)
        for name, recipe in self.recipes.items():
            for res in self.results_of(recipe):
                self.producers[name_of(res)].append(name)

        # The technology that unlocks one recipe, for the recipe set of a
        # plan and for the research order. A starter recipe has no entry.
        self.unlocked_by = {}
        for name, tech in self.techs.items():
            for effect in tech.get("effects", []):
                if effect.get("type") == "unlock-recipe":
                    self.unlocked_by.setdefault(effect["recipe"], name)
        for name, recipe in self.recipes.items():
            if "fixed_recipe" in recipe and name not in self.unlocked_by:
                fixed = recipe["fixed_recipe"]
                if fixed in self.unlocked_by:
                    self.unlocked_by[name] = self.unlocked_by[fixed]

    @staticmethod
    def ingredients_of(recipe):
        return recipe.get("ingredients", [])

    @staticmethod
    def results_of(recipe):
        if "results" in recipe:
            return recipe["results"]
        return [{"name": recipe["result"], "amount": recipe.get("result_count", 1)}]

    @staticmethod
    def craft_seconds(recipe):
        return float(recipe.get("energy_required", 0.5))

    def machine_for(self, recipe):
        """Best machine that holds the recipe categories."""
        # A recipe without a `categories` list runs in the default category.
        categories = recipe.get("categories") or DEFAULT_RECIPE_CATEGORY
        for name in MACHINE_PREFERENCE:
            proto = self.machines.get(name)
            if proto is None:
                continue
            if set(categories) & set(proto.get("crafting_categories", [])):
                return (name, float(proto.get("crafting_speed", 1.0)))
        return None

    def tech_order(self, target):
        """Prerequisites first, then the target. The research order."""
        order, seen = [], set()

        def walk(name):
            if name in seen:
                return
            seen.add(name)
            for pre in self.techs[name].get("prerequisites", []):
                if pre in self.techs:
                    walk(pre)
            order.append(name)

        walk(target)
        return order

    def allowed_recipes(self, planets, unlocked_by=None):
        """Recipes the plan may use.

        Two filters. Reachability: every ingredient must come from the ground
        of an allowed planet or from an allowed recipe. Unlock: a recipe that
        a technology unlocks is allowed only when that technology is in the
        closure of `unlocked_by`.
        """
        ground = set()
        for planet in planets:
            ground |= PLANET_RAW.get(planet, set())

        self.ground = ground
        reachable = set(ground)
        allowed = set()
        changed = True
        while changed:
            changed = False
            for name, recipe in self.recipes.items():
                if name in allowed:
                    continue
                if any(name_of(e) not in reachable for e in self.ingredients_of(recipe)):
                    continue
                allowed.add(name)
                changed = True
                for entry in self.results_of(recipe):
                    if name_of(entry) not in reachable:
                        reachable.add(name_of(entry))
                        changed = True

        if unlocked_by is None:
            return allowed

        known = set(self.tech_order(unlocked_by))
        return {
            name
            for name in allowed
            if name not in self.unlocked_by or self.unlocked_by[name] in known
        }


def closure(data, demand, allowed):
    """Every recipe that may enter the plan, and every item it touches."""
    items = set(demand)
    chosen = {}
    if "steam-power" in allowed and "boiler-steam" in allowed and "steam-power" in data.recipes:
        chosen["steam-power"] = data.recipes["steam-power"]
        chosen["boiler-steam"] = data.recipes["boiler-steam"]
        items.update(["steam", "water", "coal"])
    changed = True
    while changed:
        changed = False
        for item in list(items):
            for recipe_name in data.producers.get(item, []):
                if recipe_name not in allowed or recipe_name in chosen:
                    continue
                if recipe_name == "coal-liquefaction" and "crude-oil" in data.ground:
                    continue
                chosen[recipe_name] = data.recipes[recipe_name]
                for entry in data.ingredients_of(data.recipes[recipe_name]):
                    if name_of(entry) not in items:
                        items.add(name_of(entry))
                        changed = True
                for entry in data.results_of(data.recipes[recipe_name]):
                    if name_of(entry) not in items:
                        items.add(name_of(entry))
                        changed = True
    return chosen, items


def machine_peak_watts(m_proto):
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
        drain = active / 30.0
    else:
        drain = 0.0
    return active + drain


def inserter_peak_watts(data, ins_name="fast-inserter"):
    """Active electric power draw of one inserter from prototype properties."""
    ins_proto = data.prototypes.get("inserter", {}).get(ins_name, {})
    es = ins_proto.get("energy_source", {})
    if es.get("type") == "burner":
        return 0.0
    rot_j = energy_joules(
        ins_proto.get("energy_per_rotation") or ins_proto.get("energy_per_movement", "7kJ")
    )
    ext_j = energy_joules(ins_proto.get("energy_per_movement", "7kJ"))
    rot_s = float(ins_proto.get("rotation_speed", 0.04))
    ext_s = float(ins_proto.get("extension_speed", 0.1))
    drain = power_watts(es.get("drain", 0)) if "drain" in es else 0.0
    return (rot_j * rot_s + 0.25 * ext_j * ext_s) * 60.0 + drain


def chassis_power_watts(data, grid_w, grid_h):
    """Total electric power draw of chassis base entities (radars and lamps) for a grid_w x grid_h chassis."""
    if grid_w <= 0 or grid_h <= 0:
        return 0.0
    from pathlib import Path

    try:
        from foundry.chassis import CityBlockChassis
    except ImportError:
        from chassis import CityBlockChassis

    chassis = None
    if grid_w * grid_h >= 4:
        chassis = CityBlockChassis(pitch=100, channel_width=8, programmatic=True)
    else:
        for cand in (
            Path("cityblock.bp"),
            Path(__file__).resolve().parent.parent / "cityblock.bp",
        ):
            if cand.is_file():
                try:
                    chassis = CityBlockChassis.from_blueprint_file(cand)
                    break
                except Exception:
                    pass
        if chassis is None:
            chassis = CityBlockChassis(
                pitch=100, core_width=94, core_height=94, channel_width=6, programmatic=True
            )
    base_bp = chassis.emit_base_blueprint(grid_w, grid_h)
    entities = base_bp.get("blueprint", {}).get("entities", [])
    radars = data.prototypes.get("radar", {})
    lamps = data.prototypes.get("lamp", {})
    total = 0.0
    for ent in entities:
        ename = ent.get("name", "")
        if ename in radars:
            total += power_watts(radars[ename].get("energy_usage", 0))
        elif ename in lamps:
            val_str = str(lamps[ename].get("energy_usage_per_tick", 0)).strip()
            if val_str.endswith("J"):
                total += energy_joules(val_str) * 60.0
            else:
                total += power_watts(val_str)
    return total


def solve_rates(data, demand, allowed, extra_power_watts=0.0):
    """Recipe rates per minute that meet the demand at the least raw cost."""
    recipes, items = closure(data, demand, allowed)
    names = sorted(recipes)
    item_list = sorted(items)
    index = {name: i for i, name in enumerate(item_list)}
    net = np.zeros((len(item_list), len(names)))
    cost = np.zeros(len(names))
    for col, name in enumerate(names):
        recipe = recipes[name]
        for entry in data.results_of(recipe):
            net[index[name_of(entry)], col] += amount_of(entry)
        for entry in data.ingredients_of(recipe):
            net[index[name_of(entry)], col] -= amount_of(entry)
        # Break ties between two recipes that cost the same.
        cost[col] = 1e-6 * Data.craft_seconds(recipe)

    # An item that no recipe of the closure yields comes from the ground.
    # It needs its own variable, or its balance cannot hold.
    raw_names = [
        name for name in item_list if name in data.ground or not (net[index[name]] > 1e-9).any()
    ]
    net = np.hstack([net, np.zeros((len(item_list), len(raw_names)))])
    cost = np.concatenate([cost, np.array([RAW_WEIGHT.get(n, 1.0) for n in raw_names])])
    for j, name in enumerate(raw_names):
        col = len(names) + j
        net[index[name], col] += 1.0
        # Uranium ore needs sulfuric acid at the drill.
        minable = data.resources.get(name, {}).get("minable", {})
        fluid = minable.get("required_fluid")
        if fluid and fluid in index:
            net[index[fluid], col] -= float(minable.get("fluid_amount", 0))

    # net @ x - surplus = demand, x >= 0, surplus >= 0
    rows = len(item_list)
    cols = len(names) + len(raw_names)
    a_eq = np.hstack([net, -np.eye(rows)])
    b_eq = np.array([demand.get(item, 0.0) for item in item_list])

    a_ub = None
    b_ub = None
    if "steam-power" in recipes and "boiler-steam" in recipes:
        ins_watts = inserter_peak_watts(data)
        power_row = np.zeros(cols + rows)
        for col, name in enumerate(names):
            recipe = recipes[name]
            gen_watts = float(recipe.get("power_output_watts", 0.0))
            if gen_watts > 0.0:
                # 1 generator running 60 s/min executes 60 / craft_seconds crafts/min.
                power_row[col] = -gen_watts * (Data.craft_seconds(recipe) / 60.0)
                continue
            pick = data.machine_for(recipe)
            if pick is None:
                continue
            machine, speed = pick
            m_proto = data.machines.get(machine, {})
            watts = machine_peak_watts(m_proto)
            solid_ports = sum(
                1
                for e in (data.ingredients_of(recipe) + data.results_of(recipe))
                if not isinstance(e, dict) or e.get("type", "item") != "fluid"
            )
            if watts <= 0.0 and solid_ports == 0:
                continue
            cell_watts = watts + solid_ports * ins_watts
            power_row[col] = (Data.craft_seconds(recipe) / (60.0 * speed)) * cell_watts
        if (power_row > 0).any() or extra_power_watts > 0.0:
            a_ub = power_row.reshape(1, -1)
            b_ub = np.array([-float(extra_power_watts)])

    result = linprog(
        np.concatenate([cost, np.full(rows, SURPLUS_WEIGHT)]),
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=(0, None),
        method="highs",
    )
    if not result.success:
        raise RuntimeError(result.message)

    rates = {name: result.x[i] for i, name in enumerate(names) if result.x[i] > 1e-6}
    raw = {
        name: result.x[len(names) + j]
        for j, name in enumerate(raw_names)
        if result.x[len(names) + j] > 1e-6
    }
    surplus = {item_list[i]: result.x[cols + i] for i in range(rows) if result.x[cols + i] > 1e-6}
    return rates, raw, surplus


def machine_counts(data, rates):
    """Machines per recipe, and the power of the machine list."""
    lines = []
    power = 0.0
    for name, rate in sorted(rates.items(), key=lambda kv: -kv[1]):
        recipe = data.recipes[name]
        pick = data.machine_for(recipe)
        if pick is None:
            lines.append((name, rate, "no machine", 0.0, 0.0))
            continue
        machine, speed = pick
        count = rate / 60.0 / speed * Data.craft_seconds(recipe)
        watts = power_watts(data.machines[machine].get("energy_usage", 0))
        power += count * watts
        lines.append((name, rate, machine, count, count * watts))
    return lines, power


def print_plan(data, title, rates, raw, surplus):
    lines, power = machine_counts(data, rates)
    print(f"\n=== {title} ===")
    print(f"{'recipe':34} {'crafts/min':>9}  {'machine':22} {'count':>7} {'kW':>9}")
    for name, rate, machine, count, watts in lines:
        print(f"{name:34} {rate:9.2f}  {machine:22} {count:7.2f} {watts / 1000:9.1f}")
    print(f"{'TOTAL':34} {'':9}  {'':22} {sum(l[3] for l in lines):7.2f} {power / 1000:9.1f}")

    print(f"\n{'raw':28} {'/min':>9}  {'source':22} {'count':>7}")
    for name, rate in sorted(raw.items(), key=lambda kv: -kv[1]):
        resource = data.resources.get(name, {})
        minable = resource.get("minable", {})
        if name == "crude-oil":
            pump = float(data.drills["pumpjack"]["mining_speed"]) * 60.0
            print(f"{name:28} {rate:9.1f}  {'pumpjack (100%)':22} {rate / pump:7.2f}")
        elif name == "water":
            print(f"{name:28} {rate:9.1f}  {'offshore-pump':22} {rate / PUMP_PER_MIN:7.2f}")
        elif "mining_time" in minable:
            drill = data.drills["electric-mining-drill"]
            per_min = float(drill["mining_speed"]) * 60.0 / float(minable["mining_time"])
            print(f"{name:28} {rate:9.1f}  {'electric-mining-drill':22} {rate / per_min:7.2f}")
        else:
            print(f"{name:28} {rate:9.1f}  {'(free input)':22} {'-':>7}")
    if surplus:
        print(
            "\nbyproduct surplus (/min): " + ", ".join(f"{k} {v:.2f}" for k, v in surplus.items())
        )


def research_plan(data, target, minutes):
    """The technologies of one target, in order, and the science demand.

    The pack totals are the sum of the unit costs of every technology in the
    prerequisite closure. Spread over the wall time of the run, they are the
    demand that the production plan must meet.
    """
    order = data.tech_order(target)
    packs = collections.defaultdict(float)
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
    demand = {item: total / (minutes * 60.0) * 60.0 for item, total in packs.items()}
    print(
        f"\nscience packs /min to finish in {minutes:.0f} minutes "
        f"({lab_seconds / (minutes * 60.0):.1f} labs at full speed):"
    )
    for item, per_min in sorted(demand.items(), key=lambda kv: -kv[1]):
        print(f"  {item:32} {per_min:8.2f}")
    return demand


def check_balance(data, demand, rates, raw):
    """Recompute every item balance from the rates. Report any violation.

    An item the plan extracts is not produced by a recipe, so its gap is the
    extraction rate and not an error.
    """
    prod = collections.defaultdict(float)
    cons = collections.defaultdict(float)
    for name, rate in rates.items():
        recipe = data.recipes[name]
        for entry in data.results_of(recipe):
            prod[name_of(entry)] += amount_of(entry) * rate
        for entry in data.ingredients_of(recipe):
            cons[name_of(entry)] += amount_of(entry) * rate
    bad = []
    for item in set(prod) | set(cons):
        if item in raw:
            continue
        gap = prod[item] - cons[item] - demand.get(item, 0.0)
        if abs(gap) > 1e-6:
            bad.append((item, prod[item], cons[item], gap))
    print(f"balance check: {len(bad)} items off; {len(raw)} raw items extracted")
    for item, p, c, gap in bad[:10]:
        print(f"  {item:28} produced {p:9.2f} consumed {c:9.2f} gap {gap:9.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["item", "tech"])
    parser.add_argument("target")
    parser.add_argument("amount", type=float, default=60.0, nargs="?")
    parser.add_argument("--minutes", type=float, default=60.0)
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
