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

# Pipe-to-ground span, tiles. The prototype dump carries no max_distance
# for it, unlike underground belts. One measurement in game fills this.
PIPE_TO_GROUND_SPAN = 11


def items_per_second(speed: float | str) -> float:
    """Belt prototype speed -> items per second on one lane."""
    return float(speed) * TICKS_PER_SECOND * BELT_TILES_PER_ITEM / LANES_PER_BELT


def watts_of(text: float | str) -> float:
    """'375kW' -> 375000.0. Delegates to the solver's parser."""
    return solve.power_watts(text)


class Pdk:
    """The tech layer, read from one prototype dump."""

    def __init__(self, data: solve.Data) -> None:
        self.data = data
        self.belts = data.prototypes.get(BELT_TABLE, {})
        self.undergrounds = data.prototypes.get(UNDERGROUND_TABLE, {})
        self.inserters = data.prototypes.get(INSERTER_TABLE, {})
        self.poles = data.prototypes.get(POLE_TABLE, {})
        self.machines: dict[str, dict[str, Any]] = {}
        for table in MACHINE_TABLES:
            self.machines.update(data.prototypes.get(table, {}))

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
        width = math.ceil(box[1][0] - box[0][0])
        height = math.ceil(box[1][1] - box[0][1])
        recipe_proto = self.data.recipes[recipe]
        speed = float(proto.get("crafting_speed", 1.0))
        seconds = solve.Data.craft_seconds(recipe_proto)
        # One machine crafts speed * 60 / craft_seconds per minute. The
        # solver's count formula is the inverse of this number.
        return {
            "cell": f"{machine}.{recipe}",
            "machine": machine,
            "recipe": recipe,
            "width": width,
            "height": height,
            "watts": watts_of(proto.get("energy_usage", 0)),
            "crafts_per_minute": 60.0 * speed / seconds,
            "inputs": [solve.name_of(e) for e in solve.Data.ingredients_of(recipe_proto)],
            "outputs": [solve.name_of(e) for e in solve.Data.results_of(recipe_proto)],
        }

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
            },
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
