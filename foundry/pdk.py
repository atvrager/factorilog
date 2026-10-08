"""PDK tech layer: the game numbers, expressed in IC terms.

The PDK is what OpenROAD cannot read from Factorio. It answers three
questions:

1.  Cells. Every (building, recipe) pair is a MACRO with a footprint, the
    recipe's input and output ports, and the power it draws.
2.  Layers. The routing stack: surface, tunnel pair, fluids. A tunnel pair
    is a via between two surface nodes, with a game-measured span.
3.  Numbers. Belt capacity, inserter reach, pole reach. All from the
    prototype dump, none invented.

Everything else -- placement, routing, optimization -- belongs to OpenROAD.
"""

import math
from typing import Any

try:
    from foundry import solve
except ImportError:
    import solve

# Items per second of one belt lane, from the prototype `speed` field:
# speed * 60 ticks/s * 8 items per tile. Verified against the measurements
# in the research notes.
BELT_TILES_PER_ITEM = 8.0
TICKS_PER_SECOND = 60.0

# A belt tile carries two lanes. One lane is one item stream.
LANES_PER_BELT = 2

# Prototype tables that hold placeable buildings.
MACHINE_TABLES = ("assembling-machine", "furnace", "rocket-silo")
BELT_TABLE = "transport-belt"
UNDERGROUND_TABLE = "underground-belt"
INSERTER_TABLE = "inserter"
POLE_TABLE = "electric-pole"


def inserter_spec(proto: dict[str, Any]) -> dict[str, Any]:
    """Calculate inserter specifications from Factorio prototype data."""
    pickup = proto.get("pickup_position", [0.0, -1.0])
    insert = proto.get("insert_position", [0.0, 1.2])
    pickup_reach = round(math.hypot(float(pickup[0]), float(pickup[1])))
    insert_reach = round(math.hypot(float(insert[0]), float(insert[1])))
    rot_speed = float(proto.get("rotation_speed", 0.014))
    ext_speed = float(proto.get("extension_speed", 0.035))
    swings_per_second = rot_speed * TICKS_PER_SECOND
    turn_ticks = 0.5 / rot_speed if rot_speed > 0 else 0.0
    return {
        "reach": max(1, pickup_reach),
        "pickup_position": [float(pickup[0]), float(pickup[1])],
        "insert_position": [float(insert[0]), float(insert[1])],
        "pickup_reach": max(1, pickup_reach),
        "insert_reach": max(1, insert_reach),
        "rotation_speed": rot_speed,
        "extension_speed": ext_speed,
        "swings_per_second": swings_per_second,
        "turn_ticks": turn_ticks,
    }


# Rows of the routing stack, lowest first. Stable handles shared by the
# netlister, the DEF exporter, and the game bridge.
LAYER_SURFACE = "M1"
LAYER_SURFACE_V = "M2"
LAYER_TUNNEL = "M3"
LAYER_TUNNEL_V = "M4"
LAYER_FLUID = "M3"
LAYER_CIRCUIT_RED = "M5"
LAYER_CIRCUIT_GREEN = "M6"

DISPLAY_PANEL_CELL = "display_panel"
ARITHMETIC_COMBINATOR_CELL = "arithmetic_combinator"
DECIDER_COMBINATOR_CELL = "decider_combinator"
CONSTANT_COMBINATOR_CELL = "constant_combinator"
POWER_SWITCH_CELL = "power_switch"


def display_panel_cell() -> dict[str, Any]:
    """The display panel cell as a PDK macro record."""
    return {
        "cell": DISPLAY_PANEL_CELL,
        "machine": "display-panel",
        "recipe": "display",
        "width": 1,
        "height": 1,
        "watts": 0.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a"],
        "outputs": [],
        "circuit_cell": True,
    }


def arithmetic_combinator_cell() -> dict[str, Any]:
    """The arithmetic combinator cell as a PDK macro record."""
    return {
        "cell": ARITHMETIC_COMBINATOR_CELL,
        "machine": "arithmetic-combinator",
        "recipe": "accumulate",
        "width": 1,
        "height": 2,
        "watts": 1000.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a"],
        "outputs": ["y"],
        "circuit_cell": True,
    }


def decider_combinator_cell() -> dict[str, Any]:
    """The decider combinator cell as a PDK macro record."""
    return {
        "cell": DECIDER_COMBINATOR_CELL,
        "machine": "decider-combinator",
        "recipe": "condition",
        "width": 1,
        "height": 2,
        "watts": 1000.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a"],
        "outputs": ["y"],
        "circuit_cell": True,
    }


def constant_combinator_cell() -> dict[str, Any]:
    """The constant combinator cell as a PDK macro record."""
    return {
        "cell": CONSTANT_COMBINATOR_CELL,
        "machine": "constant-combinator",
        "recipe": "constant",
        "width": 1,
        "height": 1,
        "watts": 0.0,
        "crafts_per_minute": 0.0,
        "inputs": [],
        "outputs": ["y"],
        "circuit_cell": True,
    }


def power_switch_cell() -> dict[str, Any]:
    """The power switch cell as a PDK macro record."""
    return {
        "cell": POWER_SWITCH_CELL,
        "machine": "power-switch",
        "recipe": "switch",
        "width": 2,
        "height": 2,
        "watts": 0.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a"],
        "outputs": [],
        "circuit_cell": True,
    }


# Pipe-to-ground span, tiles. The prototype dump carries no max_distance
# for it, unlike underground belts. One measurement in game fills this.
PIPE_TO_GROUND_SPAN = 11


def items_per_second(speed: float | str) -> float:
    """Belt prototype speed -> items per second on one lane."""
    return float(speed) * TICKS_PER_SECOND * BELT_TILES_PER_ITEM / LANES_PER_BELT


def watts_of(text: float | str) -> float:
    """'375kW' -> 375000.0. Delegates to the solver's parser."""
    return solve.power_watts(text)


# Core base game machines (exclude Space Age expansion machines).
CORE_MACHINES: frozenset[str] = frozenset(
    {
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "chemical-plant",
        "oil-refinery",
        "centrifuge",
        "rocket-silo",
    }
)


class Pdk:
    """The tech layer, read from one prototype dump."""

    def __init__(self, data: solve.Data, core_only: bool = True) -> None:
        self.data = data
        self.belts = data.prototypes.get(BELT_TABLE, {})
        self.undergrounds = data.prototypes.get(UNDERGROUND_TABLE, {})
        self.inserters = data.prototypes.get(INSERTER_TABLE, {})
        self.poles = data.prototypes.get(POLE_TABLE, {})
        self.machines: dict[str, dict[str, Any]] = {}
        for table in MACHINE_TABLES:
            for name, proto in data.prototypes.get(table, {}).items():
                if not core_only or name in CORE_MACHINES:
                    self.machines[name] = proto

    def belt_tiers(self) -> list[dict[str, Any]]:
        """Belt tiers, slowest first: name, items/s per lane, tunnel span."""
        tiers = []
        for name, proto in sorted(self.belts.items(), key=lambda kv: float(kv[1]["speed"])):
            underground = proto.get("related_underground_belt", "")
            span = 0
            if underground in self.undergrounds:
                span = int(self.undergrounds[underground]["max_distance"])
            tiers.append(
                {
                    "belt": name,
                    "underground": underground,
                    "items_per_lane": items_per_second(proto["speed"]),
                    "span": span,
                }
            )
        return tiers

    def cell(self, machine: str, recipe: str) -> dict[str, Any] | None:
        """One MACRO: (building, recipe) with footprint, ports, power.

        None when the building does not exist or cannot hold the recipe.
        """
        proto = self.machines.get(machine)
        if proto is None:
            return None
        box = proto["collision_box"]
        machine_width = math.ceil(box[1][0] - box[0][0])
        machine_height = math.ceil(box[1][1] - box[0][1])
        recipe_proto = self.data.recipes[recipe]
        speed = float(proto.get("crafting_speed", 1.0))
        seconds = solve.Data.craft_seconds(recipe_proto)
        inputs = [solve.name_of(e) for e in solve.Data.ingredients_of(recipe_proto)]
        outputs = [solve.name_of(e) for e in solve.Data.results_of(recipe_proto)]
        has_inputs = bool(inputs)
        has_outputs = bool(outputs)
        width = machine_width + (1 if has_inputs else 0) + (1 if has_outputs else 0)
        height = machine_height
        # One machine crafts speed * 60 / craft_seconds per minute. The
        # solver's count formula is the inverse of this number.
        return {
            "cell": f"{machine}.{recipe}",
            "machine": machine,
            "recipe": recipe,
            "width": width,
            "height": height,
            "machine_width": machine_width,
            "machine_height": machine_height,
            "watts": watts_of(proto.get("energy_usage", 0)),
            "crafts_per_minute": 60.0 * speed / seconds,
            "inputs": inputs,
            "outputs": outputs,
        }

    def underground_tier_spans(self) -> dict[str, int]:
        """Map each underground belt entity to its maximum span in tiles."""
        return {
            tier["underground"]: int(tier["span"])
            for tier in self.belt_tiers()
            if tier.get("underground")
        }

    def layer_stack(self) -> dict[str, Any]:
        """Calculate the dynamic routing layer stack for all detected belt tiers."""
        tiers = self.belt_tiers()
        layers: dict[str, dict[str, Any]] = {
            LAYER_SURFACE: {
                "direction": "HORIZONTAL",
                "level": "surface",
                "entity": "transport-belt",
            },
            LAYER_SURFACE_V: {
                "direction": "VERTICAL",
                "level": "surface",
                "entity": "transport-belt",
            },
        }
        base_tier = (
            tiers[0]
            if tiers
            else {
                "belt": "transport-belt",
                "underground": "underground-belt",
                "span": 5,
            }
        )
        layers[LAYER_TUNNEL] = {
            "direction": "HORIZONTAL",
            "level": "underground",
            "entity": base_tier["underground"],
            "span": base_tier["span"],
            "tier_belt": base_tier["belt"],
        }
        layers[LAYER_TUNNEL_V] = {
            "direction": "VERTICAL",
            "level": "underground",
            "entity": base_tier["underground"],
            "span": base_tier["span"],
            "tier_belt": base_tier["belt"],
        }
        layers[LAYER_CIRCUIT_RED] = {
            "direction": "HORIZONTAL",
            "level": "circuit",
            "wire": "red",
        }
        layers[LAYER_CIRCUIT_GREEN] = {
            "direction": "VERTICAL",
            "level": "circuit",
            "wire": "green",
        }

        for idx, tier in enumerate(tiers[1:], start=1):
            h_layer = f"M{2 * idx + 5}"
            v_layer = f"M{2 * idx + 6}"
            ug_name = tier.get("underground", "")
            span = int(tier.get("span", 5))
            belt_name = tier.get("belt", "")
            layers[h_layer] = {
                "direction": "HORIZONTAL",
                "level": "underground",
                "entity": ug_name,
                "span": span,
                "tier_belt": belt_name,
            }
            layers[v_layer] = {
                "direction": "VERTICAL",
                "level": "underground",
                "entity": ug_name,
                "span": span,
                "tier_belt": belt_name,
            }
        return layers

    def tech_json(self) -> dict[str, Any]:
        """The tech layer as a JSON tree. The flow scripts read this."""
        return {
            "layers": {
                LAYER_SURFACE: {"via": "none"},
                LAYER_SURFACE_V: {"via": "surface-turn"},
                LAYER_TUNNEL: {
                    "via": "tunnel-pair",
                    "tiers": self.belt_tiers(),
                    "span": PIPE_TO_GROUND_SPAN,
                },
                LAYER_TUNNEL_V: {"via": "tunnel-turn"},
                LAYER_CIRCUIT_RED: {"via": "circuit-drop"},
                LAYER_CIRCUIT_GREEN: {"via": "circuit-turn"},
            },
            "layer_stack": self.layer_stack(),
            "underground_spans": self.underground_tier_spans(),
            "belts": {name: items_per_second(proto["speed"]) for name, proto in self.belts.items()},
            "inserters": {name: inserter_spec(proto) for name, proto in self.inserters.items()},
            "poles": {
                name: {
                    "supply": proto.get("supply_area_distance"),
                    "wire": proto.get("maximum_wire_distance"),
                }
                for name, proto in self.poles.items()
            },
        }
