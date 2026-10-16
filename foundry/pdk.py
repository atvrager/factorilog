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
    from foundry.entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
except ImportError:
    import solve
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )

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
PIPE_TO_GROUND_TABLE = "pipe-to-ground"


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
LAYER_CIRCUIT_RED = "M9"
LAYER_CIRCUIT_GREEN = "M10"


def circuit_layers(num_tiers: int = 3) -> tuple[str, str]:
    """Derive horizontal red and vertical green layers after all active underground tiers."""
    t = max(1, num_tiers)
    return f"M{2 * t + 3}", f"M{2 * t + 4}"


DISPLAY_PANEL_CELL = "display_panel"
ARITHMETIC_COMBINATOR_CELL = "arithmetic_combinator"
DECIDER_COMBINATOR_CELL = "decider_combinator"
CONSTANT_COMBINATOR_CELL = "constant_combinator"
POWER_SWITCH_CELL = "power_switch"

# Factorio 2.0 wire connector IDs.
WIRE_CONNECTOR_CIRCUIT_RED = 1
WIRE_CONNECTOR_CIRCUIT_GREEN = 2
WIRE_CONNECTOR_COMBINATOR_INPUT_RED = 1
WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN = 2
WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED = 3
WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN = 4
WIRE_CONNECTOR_POLE_COPPER = 5
WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER = 5
WIRE_CONNECTOR_POWER_SWITCH_RIGHT_COPPER = 6


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


# Pipe-to-ground default span in tiles.
DEFAULT_PIPE_TO_GROUND_SPAN = 10
PIPE_TO_GROUND_SPAN = DEFAULT_PIPE_TO_GROUND_SPAN


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

    def __init__(self, data: solve.Data, core_only: bool = False) -> None:
        self.data = data
        self.belts = data.prototypes.get(BELT_TABLE, {})
        self.undergrounds = data.prototypes.get(UNDERGROUND_TABLE, {})
        self.underground_pipes = data.prototypes.get(PIPE_TO_GROUND_TABLE, {})
        self.inserters = data.prototypes.get(INSERTER_TABLE, {})
        self.poles = data.prototypes.get(POLE_TABLE, {})
        self.machines: dict[str, dict[str, Any]] = {}
        for table in MACHINE_TABLES:
            for name, proto in data.prototypes.get(table, {}).items():
                if not core_only or name in CORE_MACHINES:
                    self.machines[name] = proto
        try:
            from foundry.entity_helpers import register_prototype_boxes
        except ImportError:
            from entity_helpers import register_prototype_boxes

        register_prototype_boxes(data.prototypes)

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

    def compound_cell(self, machine: str, combo_name: str) -> dict[str, Any] | None:
        """Composite PDK macro cell for coupled 1:1 producer-consumer pairs."""
        proto = self.machines.get(machine)
        if proto is None:
            return None
        speed = float(proto.get("crafting_speed", 1.0))
        watts = watts_of(proto.get("energy_usage", 0)) * 2.0

        if combo_name == "gear_belt_combo":
            belt_recipe = self.data.recipes.get("transport-belt", {})
            seconds = solve.Data.craft_seconds(belt_recipe) if belt_recipe else 0.5
            cpm = 60.0 * speed / seconds
            return {
                "cell": f"{machine}.gear_belt_combo",
                "machine": machine,
                "recipe": "gear_belt_combo",
                "width": 6,
                "height": 7,
                "machine_width": 3,
                "machine_height": 3,
                "watts": watts,
                "crafts_per_minute": cpm,
                "inputs": ["iron-plate"],
                "outputs": ["transport-belt"],
                "compound": True,
            }
        if combo_name == "gear_inserter_combo":
            ins_recipe = self.data.recipes.get("inserter", {})
            seconds = solve.Data.craft_seconds(ins_recipe) if ins_recipe else 0.5
            cpm = 60.0 * speed / seconds
            return {
                "cell": f"{machine}.gear_inserter_combo",
                "machine": machine,
                "recipe": "gear_inserter_combo",
                "width": 6,
                "height": 7,
                "machine_width": 3,
                "machine_height": 3,
                "watts": watts,
                "crafts_per_minute": cpm,
                "inputs": ["iron-plate", "electronic-circuit"],
                "outputs": ["inserter"],
                "compound": True,
            }
        return None

    def cell(self, machine: str, recipe: str) -> dict[str, Any] | None:
        """One MACRO: (building, recipe) with footprint, ports, power.

        None when the building does not exist or cannot hold the recipe.
        """
        if recipe in ("gear_belt_combo", "gear_inserter_combo"):
            return self.compound_cell(machine, recipe)
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
        # Check for fluidboxes and compute port mapping
        fluid_table = self.data.prototypes.get("fluid", {})
        fluid_boxes = proto.get("fluid_boxes", [])
        ingredients_raw = solve.Data.ingredients_of(recipe_proto)
        results_raw = solve.Data.results_of(recipe_proto)
        has_fluids = any(solve.name_of(e) in fluid_table for e in ingredients_raw) or any(
            solve.name_of(e) in fluid_table for e in results_raw
        )

        direction = DIRECTION_NORTH
        in_slots: dict[str, int] = {}
        out_slots: dict[str, int] = {}
        port_faces: dict[str, str] | None = None

        if fluid_boxes and has_fluids:
            in_fluidboxes = [fb for fb in fluid_boxes if fb.get("production_type") == "input"]
            out_fluidboxes = [fb for fb in fluid_boxes if fb.get("production_type") == "output"]

            # Choose orientation to place fluid inputs on West (x < 0) and outputs on East (x > 0)
            target_conns: list[dict[str, Any]] = []
            target_is_input = True
            if in_fluidboxes and in_fluidboxes[0].get("pipe_connections"):
                target_conns = in_fluidboxes[0]["pipe_connections"]
                target_is_input = True
            elif out_fluidboxes and out_fluidboxes[0].get("pipe_connections"):
                target_conns = out_fluidboxes[0]["pipe_connections"]
                target_is_input = False

            if target_conns:
                tpos = target_conns[0].get("position", [0, 0])
                tx, ty = float(tpos[0]), float(tpos[1])
                if target_is_input:
                    if ty < 0:
                        direction = DIRECTION_WEST
                    elif ty > 0:
                        direction = DIRECTION_EAST
                    elif tx > 0:
                        direction = DIRECTION_SOUTH
                    else:
                        direction = DIRECTION_NORTH
                else:
                    if ty > 0:
                        direction = DIRECTION_WEST
                    elif ty < 0:
                        direction = DIRECTION_EAST
                    elif tx < 0:
                        direction = DIRECTION_SOUTH
                    else:
                        direction = DIRECTION_NORTH

            if direction in (DIRECTION_EAST, DIRECTION_WEST):
                placed_w = machine_height
                placed_h = machine_width
            else:
                placed_w = machine_width
                placed_h = machine_height

            def _rotate_and_slot(bx: float, by: float) -> int:
                if direction == DIRECTION_NORTH:
                    ry = by
                elif direction == DIRECTION_EAST:
                    ry = bx
                elif direction == DIRECTION_SOUTH:
                    ry = -by
                elif direction == DIRECTION_WEST:
                    ry = -bx
                else:
                    ry = by
                return math.floor(ry + placed_h / 2.0)

            y_offset = 1
            width = placed_w + 2
            height = placed_h + 2

            in_fb_slots = [
                _rotate_and_slot(
                    fb["pipe_connections"][0]["position"][0],
                    fb["pipe_connections"][0]["position"][1],
                )
                + y_offset
                for fb in in_fluidboxes
                if fb.get("pipe_connections")
            ]
            out_fb_slots = [
                _rotate_and_slot(
                    fb["pipe_connections"][0]["position"][0],
                    fb["pipe_connections"][0]["position"][1],
                )
                + y_offset
                for fb in out_fluidboxes
                if fb.get("pipe_connections")
            ]

            # Map input fluid ingredients
            fluid_in_count = 0
            item_inputs = []
            for ing in ingredients_raw:
                p = solve.name_of(ing)
                if p in fluid_table:
                    fb_idx = ing.get("fluidbox_index")
                    slot_idx = (fb_idx - 1) if fb_idx is not None else fluid_in_count
                    fluid_in_count += 1
                    if 0 <= slot_idx < len(in_fb_slots):
                        in_slots[p] = in_fb_slots[slot_idx]
                    elif in_fb_slots:
                        in_slots[p] = in_fb_slots[min(slot_idx, len(in_fb_slots) - 1)]
                else:
                    item_inputs.append(p)

            # Map input item ingredients to non-pipe slots on West face (within machine span: y_offset to y_offset + placed_h - 1)
            all_in_pipes = set(in_fb_slots)
            avail_item_in = [
                s for s in range(y_offset, y_offset + placed_h) if s not in all_in_pipes
            ]
            for s in range(y_offset, y_offset + placed_h):
                if s not in in_slots.values() and s not in avail_item_in:
                    avail_item_in.append(s)
            for idx, p in enumerate(item_inputs):
                if idx < len(avail_item_in):
                    in_slots[p] = avail_item_in[idx]
                else:
                    in_slots[p] = y_offset + (idx % placed_h)

            # Map output fluid results
            fluid_out_count = 0
            item_outputs = []
            for res in results_raw:
                p = solve.name_of(res)
                if p in fluid_table:
                    fb_idx = res.get("fluidbox_index")
                    slot_idx = (fb_idx - 1) if fb_idx is not None else fluid_out_count
                    fluid_out_count += 1
                    if 0 <= slot_idx < len(out_fb_slots):
                        out_slots[p] = out_fb_slots[slot_idx]
                    elif out_fb_slots:
                        out_slots[p] = out_fb_slots[min(slot_idx, len(out_fb_slots) - 1)]
                else:
                    item_outputs.append(p)

            # Map output item results to non-pipe slots on East face (within machine span: y_offset to y_offset + placed_h - 1)
            all_out_pipes = set(out_fb_slots)
            avail_item_out = [
                s for s in range(y_offset, y_offset + placed_h) if s not in all_out_pipes
            ]
            for s in range(y_offset, y_offset + placed_h):
                if s not in out_slots.values() and s not in avail_item_out:
                    avail_item_out.append(s)
            for idx, p in enumerate(item_outputs):
                if idx < len(avail_item_out):
                    out_slots[p] = avail_item_out[idx]
                else:
                    out_slots[p] = y_offset + (idx % placed_h)

            port_faces = dict.fromkeys(inputs, "W")
            port_faces.update(dict.fromkeys(outputs, "E"))
        elif proto.get("fixed_recipe") and proto.get("fixed_recipe") == recipe_proto.get(
            "fixed_recipe"
        ):
            pad_proto = next(iter(self.data.prototypes.get("cargo-landing-pad", {}).values()), None)
            placed_w = machine_width
            placed_h = machine_height
            if pad_proto is not None:
                pad_box = pad_proto["collision_box"]
                pad_w = math.ceil(pad_box[1][0] - pad_box[0][0])
                pad_h = math.ceil(pad_box[1][1] - pad_box[0][1])
                pad_grid = int(pad_proto.get("build_grid_size", 2))
                width = (
                    (1 if inputs else 0)
                    + machine_width
                    + (pad_grid - 1)
                    + pad_w
                    + (1 if outputs else 0)
                )
                height = max(machine_height, pad_h + (pad_grid - 1))
            else:
                width = machine_width + (1 if inputs else 0) + (1 if outputs else 0)
                height = machine_height
            if len(inputs) > 0 and height >= 2 * len(inputs):
                for idx, p in enumerate(inputs):
                    in_slots[p] = 1 + 2 * idx
            else:
                offset_in = (height - len(inputs)) // 2 if len(inputs) <= height else 0
                for idx, p in enumerate(inputs):
                    in_slots[p] = offset_in + idx
            offset_out = (height - len(outputs)) // 2 if len(outputs) <= height else 0
            for idx, p in enumerate(outputs):
                out_slots[p] = offset_out + idx
        else:
            placed_w = machine_width
            placed_h = machine_height
            width = machine_width + (1 if inputs else 0) + (1 if outputs else 0)
            height = machine_height
            offset_in = (height - len(inputs)) // 2 if len(inputs) <= height else 0
            for idx, p in enumerate(inputs):
                in_slots[p] = offset_in + idx
            offset_out = (height - len(outputs)) // 2 if len(outputs) <= height else 0
            for idx, p in enumerate(outputs):
                out_slots[p] = offset_out + idx

        # One machine crafts speed * 60 / craft_seconds per minute. The
        # solver's count formula is the inverse of this number.
        res_cell: dict[str, Any] = {
            "cell": f"{machine}.{recipe}",
            "machine": machine,
            "recipe": recipe,
            "width": width,
            "height": height,
            "machine_width": machine_width,
            "machine_height": machine_height,
            "direction": direction,
            "in_slots": in_slots,
            "out_slots": out_slots,
            "has_fluids": has_fluids,
            "watts": watts_of(proto.get("active_energy_usage") or proto.get("energy_usage", 0)),
            "crafts_per_minute": 60.0 * speed / seconds,
            "inputs": inputs,
            "outputs": outputs,
        }
        if proto.get("fixed_recipe") and proto.get("fixed_recipe") == recipe_proto.get(
            "fixed_recipe"
        ):
            res_cell["fixed_recipe"] = proto["fixed_recipe"]
            pad_proto = next(iter(self.data.prototypes.get("cargo-landing-pad", {}).values()), None)
            if pad_proto is not None:
                pad_box = pad_proto["collision_box"]
                res_cell["landing_pad"] = pad_proto["name"]
                res_cell["landing_pad_width"] = math.ceil(pad_box[1][0] - pad_box[0][0])
                res_cell["landing_pad_height"] = math.ceil(pad_box[1][1] - pad_box[0][1])
                res_cell["landing_pad_grid_size"] = int(pad_proto.get("build_grid_size", 2))
        if port_faces is not None:
            res_cell["port_faces"] = port_faces
        return res_cell

    def pipe_span(self, name: str = "pipe-to-ground") -> int:
        """Get maximum underground span for pipe-to-ground entity from prototype data."""
        proto = self.underground_pipes.get(name, {})
        fluid_box = proto.get("fluid_box", {})
        for conn in fluid_box.get("pipe_connections", []):
            if conn.get("connection_type") == "underground" and "max_underground_distance" in conn:
                return int(conn["max_underground_distance"])
        return DEFAULT_PIPE_TO_GROUND_SPAN

    def underground_tier_spans(self) -> dict[str, int]:
        """Return dictionary of maximum spans for underground belts and pipes."""
        spans = {
            tier["underground"]: int(tier["span"])
            for tier in self.belt_tiers()
            if tier.get("underground")
        }
        for name in self.underground_pipes:
            spans[name] = self.pipe_span(name)
        if "pipe-to-ground" not in spans:
            spans["pipe-to-ground"] = self.pipe_span("pipe-to-ground")
        return spans

    def circuit_layers(self) -> tuple[str, str]:
        """Derive horizontal red and vertical green layers after all active underground tiers."""
        return circuit_layers(len(self.belt_tiers()))

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
        for idx, tier in enumerate(tiers):
            h_layer = f"M{2 * idx + 3}"
            v_layer = f"M{2 * idx + 4}"
            ug_name = tier.get("underground", "underground-belt")
            span = int(tier.get("span", 5))
            belt_name = tier.get("belt", "transport-belt")
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

        red_layer, green_layer = self.circuit_layers()
        layers[red_layer] = {
            "direction": "HORIZONTAL",
            "level": "circuit",
            "wire": "red",
        }
        layers[green_layer] = {
            "direction": "VERTICAL",
            "level": "circuit",
            "wire": "green",
        }
        return layers

    def tech_json(self) -> dict[str, Any]:
        """The tech layer as a JSON tree. The flow scripts read this."""
        layers_dict: dict[str, dict[str, Any]] = {
            LAYER_SURFACE: {"via": "none"},
            LAYER_SURFACE_V: {"via": "surface-turn"},
        }
        for idx, tier in enumerate(self.belt_tiers()):
            h_layer = f"M{2 * idx + 3}"
            v_layer = f"M{2 * idx + 4}"
            layers_dict[h_layer] = {
                "via": "tunnel-pair",
                "tiers": self.belt_tiers(),
                "span": self.pipe_span() if idx == 0 else tier.get("span", 5),
            }
            layers_dict[v_layer] = {"via": "tunnel-turn"}
        red_layer, green_layer = self.circuit_layers()
        layers_dict[red_layer] = {"via": "circuit-drop"}
        layers_dict[green_layer] = {"via": "circuit-turn"}

        return {
            "layers": layers_dict,
            "layer_stack": self.layer_stack(),
            "underground_spans": self.underground_tier_spans(),
            "circuit_layers": list(self.circuit_layers()),
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
