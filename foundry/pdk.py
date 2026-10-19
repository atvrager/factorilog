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
from collections.abc import Mapping
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
SECONDS_PER_MINUTE = 60.0
HALF_TURN_FRACTION = 0.5
TILE_CENTER_OFFSET = 0.5
DIRECTION_MODULO = 16
SURFACE_LAYER_COUNT = 2
VERTICAL_LAYER_OFFSET = 1
DEFAULT_CIRCUIT_TIER_COUNT = 3
COMBINATOR_ACTIVE_WATTS = 1000.0
COMPOUND_CELL_WIDTH = 6
COMPOUND_CELL_HEIGHT = 7
FOUR_SIDED_INPUT_THRESHOLD = 6
MIN_SPACED_PORT_WIDTH = 3

# A belt tile carries two lanes. One lane is one item stream.
LANES_PER_BELT = 2

# Prototype tables that hold placeable buildings.
MACHINE_TABLES = ("assembling-machine", "furnace", "rocket-silo", "boiler", "generator", "lab")
BELT_TABLE = "transport-belt"
UNDERGROUND_TABLE = "underground-belt"
INSERTER_TABLE = "inserter"
POLE_TABLE = "electric-pole"
PIPE_TO_GROUND_TABLE = "pipe-to-ground"

DIRECTION_STEP_DX: dict[int, float] = {
    DIRECTION_EAST: 1.0,
    DIRECTION_WEST: -1.0,
}
DIRECTION_STEP_DY: dict[int, float] = {
    DIRECTION_SOUTH: 1.0,
    DIRECTION_NORTH: -1.0,
}
ROTATED_Y_FACTORS: dict[int, tuple[float, float]] = {
    DIRECTION_NORTH: (0.0, 1.0),
    DIRECTION_EAST: (1.0, 0.0),
    DIRECTION_SOUTH: (0.0, -1.0),
    DIRECTION_WEST: (-1.0, 0.0),
}
COMPOUND_SPECS: dict[str, tuple[str, list[str], list[str], bool]] = {
    "gear_belt_combo": ("transport-belt", ["iron-plate"], ["transport-belt"], True),
    "gear_inserter_combo": (
        "inserter",
        ["iron-plate", "electronic-circuit"],
        ["inserter"],
        False,
    ),
}

ProtoRecord = Mapping[str, object]


def _box_tile_dims(box: list[list[float]]) -> tuple[int, int]:
    """Compute integer tile width and height from a collision box."""
    return math.ceil(box[1][0] - box[0][0]), math.ceil(box[1][1] - box[0][1])


def inserter_spec(proto: ProtoRecord) -> dict[str, Any]:
    """Calculate inserter specifications from Factorio prototype data."""
    pickup: Any = proto.get("pickup_position", (0.0, -1.0))
    insert: Any = proto.get("insert_position", (0.0, 1.0))
    pickup_reach = round(math.hypot(float(pickup[0]), float(pickup[1])))
    insert_reach = round(math.hypot(float(insert[0]), float(insert[1])))
    rot_speed_raw: Any = proto.get("rotation_speed", 0.0)
    ext_speed_raw: Any = proto.get("extension_speed", 0.0)
    rot_speed = float(rot_speed_raw)
    ext_speed = float(ext_speed_raw)
    swings_per_second = rot_speed * TICKS_PER_SECOND
    turn_ticks = HALF_TURN_FRACTION / rot_speed if rot_speed > 0 else 0.0
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


def _tier_layer_pair(tier_index: int) -> tuple[str, str]:
    """Return horizontal and vertical metal layer names for a 0-based tier index."""
    base = SURFACE_LAYER_COUNT + 2 * tier_index + 1
    return f"M{base}", f"M{base + VERTICAL_LAYER_OFFSET}"


def circuit_layers(num_tiers: int = DEFAULT_CIRCUIT_TIER_COUNT) -> tuple[str, str]:
    """Derive horizontal red and vertical green layers after all active underground tiers."""
    return _tier_layer_pair(max(1, num_tiers))


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
        "watts": COMBINATOR_ACTIVE_WATTS,
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
        "watts": COMBINATOR_ACTIVE_WATTS,
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
        "boiler",
        "steam-engine",
        "lab",
    }
)


def _center_slots(ports: list[str], span: int, base_offset: int = 0) -> dict[str, int]:
    """Assign consecutive centered slot indices along a face of length span."""
    count = len(ports)
    start = base_offset + ((span - count) // 2 if count <= span else 0)
    return {port: start + idx for idx, port in enumerate(ports)}


def _spaced_or_centered_slots(ports: list[str], span: int, base_offset: int = 0) -> dict[str, int]:
    """Assign 2-tile-spaced slots when span permits, otherwise centered consecutive slots."""
    if ports and span >= 2 * len(ports):
        return {port: 1 + 2 * idx for idx, port in enumerate(ports)}
    return _center_slots(ports, span, base_offset)


def _east_oriented_pipe_ext(
    conn: ProtoRecord, cx: float, cy: float
) -> tuple[int, float, float, float]:
    """Project a pipe connection on an EAST-oriented machine to external tile coordinates."""
    pos: Any = conn["position"]
    bx, by = float(pos[0]), float(pos[1])
    rx, ry = -by, bx
    raw_dir: Any = conn.get("direction", 0)
    cdir = (int(raw_dir) + DIRECTION_EAST) % DIRECTION_MODULO
    ext_x = round(cx + rx + DIRECTION_STEP_DX.get(cdir, 0.0), 2)
    ext_y = round(cy + ry + DIRECTION_STEP_DY.get(cdir, 0.0), 2)
    return cdir, ry, ext_x, ext_y


def _choose_fluid_orientation(in_fbs: list[ProtoRecord], out_fbs: list[ProtoRecord]) -> int:
    """Select cardinal orientation so fluid inputs face West and fluid outputs face East."""
    conns: Any = []
    sign = 1.0
    if in_fbs and in_fbs[0].get("pipe_connections"):
        in_conns: Any = in_fbs[0]["pipe_connections"]
        conns = in_conns
    elif out_fbs and out_fbs[0].get("pipe_connections"):
        out_conns: Any = out_fbs[0]["pipe_connections"]
        conns = out_conns
        sign = -1.0
    if not conns:
        return DIRECTION_NORTH
    tpos = conns[0].get("position", (0.0, 0.0))
    tx, ty = float(tpos[0]) * sign, float(tpos[1]) * sign
    if ty < 0.0:
        return DIRECTION_WEST
    if ty > 0.0:
        return DIRECTION_EAST
    return DIRECTION_SOUTH if tx > 0.0 else DIRECTION_NORTH


def _project_fluidbox_slots(
    fluidboxes: list[ProtoRecord], direction: int, placed_h: int, y_offset: int
) -> list[int]:
    """Project fluidbox pipe connections onto 1-indexed vertical face slots."""
    fx, fy = ROTATED_Y_FACTORS.get(direction, (0.0, 1.0))
    slots: list[int] = []
    for fb in fluidboxes:
        conns: Any = fb.get("pipe_connections")
        if conns:
            pos = conns[0]["position"]
            ry = float(pos[0]) * fx + float(pos[1]) * fy
            slots.append(math.floor(ry + placed_h / 2.0) + y_offset)
    return slots


def _map_fluid_and_item_ports(
    raw_entries: list[ProtoRecord],
    fluid_table: ProtoRecord,
    fb_slots: list[int],
    placed_h: int,
    y_offset: int,
) -> dict[str, int]:
    """Assign fluid ports to fluidbox slots and solid items to remaining face slots."""
    slots: dict[str, int] = {}
    fluid_entries = [e for e in raw_entries if solve.name_of(dict(e)) in fluid_table]
    item_ports = [
        solve.name_of(dict(e)) for e in raw_entries if solve.name_of(dict(e)) not in fluid_table
    ]
    if fb_slots:
        for fluid_idx, entry in enumerate(fluid_entries):
            fb_idx: Any = entry.get("fluidbox_index")
            raw_idx = (int(fb_idx) - 1) if fb_idx is not None else fluid_idx
            slots[solve.name_of(dict(entry))] = fb_slots[max(0, min(raw_idx, len(fb_slots) - 1))]

    pipe_set = set(fb_slots)
    used_fluid_slots = set(slots.values())
    all_span_slots = range(y_offset, y_offset + placed_h)
    avail = [s for s in all_span_slots if s not in pipe_set]
    avail.extend(s for s in all_span_slots if s in pipe_set and s not in used_fluid_slots)
    slots.update(
        {
            port: avail[idx] if idx < len(avail) else y_offset + (idx % placed_h)
            for idx, port in enumerate(item_ports)
        }
    )
    return slots


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
                    self.machines[name] = data.machines.get(name, proto)
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
        spec = COMPOUND_SPECS.get(combo_name)
        if proto is None or spec is None:
            return None
        target_recipe_name, inputs, outputs, is_compound = spec
        machine_w, machine_h = _box_tile_dims(proto["collision_box"])
        speed = float(proto.get("crafting_speed", 1.0))
        watts = watts_of(proto.get("energy_usage", 0)) * 2.0
        recipe_proto = self.data.recipes.get(target_recipe_name, {})
        seconds = solve.Data.craft_seconds(recipe_proto)
        return {
            "cell": f"{machine}.{combo_name}",
            "machine": machine,
            "recipe": combo_name,
            "width": COMPOUND_CELL_WIDTH,
            "height": COMPOUND_CELL_HEIGHT,
            "machine_width": machine_w,
            "machine_height": machine_h,
            "watts": watts,
            "crafts_per_minute": SECONDS_PER_MINUTE * speed / seconds,
            "inputs": list(inputs),
            "outputs": list(outputs),
            "compound": is_compound,
        }

    def _layout_power_fluid_cell(
        self,
        proto: ProtoRecord,
        machine_w: int,
        machine_h: int,
        inputs: list[str],
        outputs: list[str],
        fluid_table: ProtoRecord,
        in_match_dir: int,
        record_internal_pipes: bool,
    ) -> dict[str, Any]:
        """Compute cell layout for EAST-oriented boiler or steam-engine fluid machines."""
        placed_w, placed_h = machine_h, machine_w
        cx, cy = 1.0 + placed_w / 2.0, 1.0 + placed_h / 2.0
        mid_slot = 1 + placed_h // 2

        out_fb: Any = proto.get("output_fluid_box", {})
        out_conns = out_fb.get("pipe_connections", [])
        out_slot = (
            math.floor(cy + _east_oriented_pipe_ext(out_conns[0], cx, cy)[1])
            if out_conns
            else mid_slot
        )
        out_slots = {p: out_slot for p in outputs if p in fluid_table}

        in_slots = {p: mid_slot for p in inputs if p not in fluid_table}
        fluid_inputs = [p for p in inputs if p in fluid_table]
        unused_fluid_ports: list[tuple[float, float]] = []
        internal_pipes: list[dict[str, Any]] = []
        in_fb: Any = proto.get("fluid_box", {})
        for conn in in_fb.get("pipe_connections", []):
            cdir, _ry, ext_x, ext_y = _east_oriented_pipe_ext(conn, cx, cy)
            if cdir != in_match_dir:
                unused_fluid_ports.append((ext_x, ext_y))
                continue
            in_slots.update(dict.fromkeys(fluid_inputs, math.floor(ext_y)))
            if record_internal_pipes:
                internal_pipes.extend({"x": ext_x, "y": ext_y, "fluid": p} for p in fluid_inputs)

        port_faces = dict.fromkeys(inputs, "W")
        port_faces.update(dict.fromkeys(outputs, "E"))
        return {
            "width": placed_w + 2,
            "height": placed_h + 2,
            "machine_width": placed_w,
            "machine_height": placed_h,
            "direction": DIRECTION_EAST,
            "in_slots": in_slots,
            "out_slots": out_slots,
            "port_faces": port_faces,
            "unused_fluid_ports": unused_fluid_ports,
            "internal_pipes": internal_pipes,
        }

    def _layout_fluidbox_cell(
        self,
        fluid_boxes: list[ProtoRecord],
        machine_w: int,
        machine_h: int,
        inputs: list[str],
        outputs: list[str],
        ingredients_raw: list[ProtoRecord],
        results_raw: list[ProtoRecord],
        fluid_table: ProtoRecord,
    ) -> dict[str, Any]:
        """Compute cell layout for assembling or chemical machines with fluid_boxes."""
        in_fbs = [fb for fb in fluid_boxes if fb.get("production_type") == "input"]
        out_fbs = [fb for fb in fluid_boxes if fb.get("production_type") == "output"]
        direction = _choose_fluid_orientation(in_fbs, out_fbs)
        placed_w, placed_h = (
            (machine_h, machine_w)
            if direction in (DIRECTION_EAST, DIRECTION_WEST)
            else (machine_w, machine_h)
        )
        y_offset = 1
        width, height = placed_w + 2, placed_h + 2
        in_fb_slots = _project_fluidbox_slots(in_fbs, direction, placed_h, y_offset)
        out_fb_slots = _project_fluidbox_slots(out_fbs, direction, placed_h, y_offset)

        in_slots = _map_fluid_and_item_ports(
            ingredients_raw, fluid_table, in_fb_slots, placed_h, y_offset
        )
        out_slots = _map_fluid_and_item_ports(
            results_raw, fluid_table, out_fb_slots, placed_h, y_offset
        )

        used_in = set(in_slots.values())
        used_out = set(out_slots.values())
        unused_in = [
            (TILE_CENTER_OFFSET, float(s) + TILE_CENTER_OFFSET)
            for s in sorted(set(in_fb_slots) - used_in)
        ]
        unused_out = [
            (float(width) - TILE_CENTER_OFFSET, float(s) + TILE_CENTER_OFFSET)
            for s in sorted(set(out_fb_slots) - used_out)
        ]
        port_faces = dict.fromkeys(inputs, "W")
        port_faces.update(dict.fromkeys(outputs, "E"))
        return {
            "width": width,
            "height": height,
            "machine_width": placed_w,
            "machine_height": placed_h,
            "direction": direction,
            "in_slots": in_slots,
            "out_slots": out_slots,
            "port_faces": port_faces,
            "unused_fluid_ports": unused_in + unused_out,
            "internal_pipes": [],
        }

    def _layout_fixed_recipe_cell(
        self,
        proto: ProtoRecord,
        machine_w: int,
        machine_h: int,
        inputs: list[str],
        outputs: list[str],
    ) -> dict[str, Any]:
        """Compute cell layout for fixed-recipe machines such as rocket-silo with landing pad."""
        pad_proto = next(iter(self.data.prototypes.get("cargo-landing-pad", {}).values()), None)
        extra: dict[str, Any] = {"fixed_recipe": proto["fixed_recipe"]}
        if pad_proto is not None:
            pad_w, pad_h = _box_tile_dims(pad_proto["collision_box"])
            pad_grid = int(pad_proto.get("build_grid_size", 2))
            width = bool(inputs) + machine_w + (pad_grid - 1) + pad_w + bool(outputs)
            height = max(machine_h, pad_h + (pad_grid - 1))
            extra.update(
                {
                    "landing_pad": pad_proto["name"],
                    "landing_pad_width": pad_w,
                    "landing_pad_height": pad_h,
                    "landing_pad_grid_size": pad_grid,
                }
            )
        else:
            width = machine_w + bool(inputs) + bool(outputs)
            height = machine_h

        return {
            "width": width,
            "height": height,
            "machine_width": machine_w,
            "machine_height": machine_h,
            "direction": DIRECTION_NORTH,
            "in_slots": _spaced_or_centered_slots(inputs, height),
            "out_slots": _center_slots(outputs, height),
            "port_faces": None,
            "unused_fluid_ports": [],
            "internal_pipes": [],
            "extra": extra,
        }

    def _layout_standard_cell(
        self,
        machine_w: int,
        machine_h: int,
        inputs: list[str],
        outputs: list[str],
    ) -> dict[str, Any]:
        """Compute cell layout for standard solid-item machines and 4-sided GAA labs."""
        if not outputs and len(inputs) > FOUR_SIDED_INPUT_THRESHOLD:
            faces_order = ("W", "N", "S", "E")
            port_faces = {p: faces_order[idx % len(faces_order)] for idx, p in enumerate(inputs)}
            in_slots: dict[str, int] = {}
            for face_dir in ("W", "E"):
                f_inps = [p for p in inputs if port_faces[p] == face_dir]
                in_slots.update(_center_slots(f_inps, machine_h, base_offset=1))
            for face_dir in ("N", "S"):
                f_inps = [p for p in inputs if port_faces[p] == face_dir]
                if len(f_inps) == 2 and machine_w >= MIN_SPACED_PORT_WIDTH:
                    in_slots.update({p: 1 + 2 * idx for idx, p in enumerate(f_inps)})
                else:
                    in_slots.update(_center_slots(f_inps, machine_w, base_offset=1))
            return {
                "width": machine_w + 2,
                "height": machine_h + 2,
                "machine_width": machine_w,
                "machine_height": machine_h,
                "direction": DIRECTION_NORTH,
                "in_slots": in_slots,
                "out_slots": {},
                "port_faces": port_faces,
                "unused_fluid_ports": [],
                "internal_pipes": [],
            }

        height = machine_h
        return {
            "width": machine_w + bool(inputs) + bool(outputs),
            "height": height,
            "machine_width": machine_w,
            "machine_height": machine_h,
            "direction": DIRECTION_NORTH,
            "in_slots": _center_slots(inputs, height),
            "out_slots": _center_slots(outputs, height),
            "port_faces": None,
            "unused_fluid_ports": [],
            "internal_pipes": [],
        }

    def _select_cell_layout(
        self,
        proto: ProtoRecord,
        recipe_proto: ProtoRecord,
        machine_w: int,
        machine_h: int,
        inputs: list[str],
        outputs: list[str],
        ingredients_raw: list[ProtoRecord],
        results_raw: list[ProtoRecord],
        fluid_table: ProtoRecord,
        has_fluids: bool,
    ) -> dict[str, Any]:
        """Dispatch to the appropriate cell geometry layout strategy."""
        ptype = proto.get("type")
        if ptype in ("boiler", "generator") and has_fluids:
            is_boiler = ptype == "boiler"
            return self._layout_power_fluid_cell(
                proto,
                machine_w,
                machine_h,
                inputs,
                outputs,
                fluid_table,
                DIRECTION_NORTH if is_boiler else DIRECTION_WEST,
                is_boiler,
            )
        fluid_boxes: Any = proto.get("fluid_boxes", [])
        if fluid_boxes and has_fluids:
            return self._layout_fluidbox_cell(
                fluid_boxes,
                machine_w,
                machine_h,
                inputs,
                outputs,
                ingredients_raw,
                results_raw,
                fluid_table,
            )
        fixed_rec = proto.get("fixed_recipe")
        if fixed_rec and fixed_rec == recipe_proto.get("fixed_recipe"):
            return self._layout_fixed_recipe_cell(proto, machine_w, machine_h, inputs, outputs)
        return self._layout_standard_cell(machine_w, machine_h, inputs, outputs)

    def cell(self, machine: str, recipe: str) -> dict[str, Any] | None:
        """One MACRO: (building, recipe) with footprint, ports, power.

        None when the building does not exist or cannot hold the recipe.
        """
        if recipe in COMPOUND_SPECS:
            return self.compound_cell(machine, recipe)
        proto = self.machines.get(machine)
        if proto is None:
            return None
        machine_w, machine_h = _box_tile_dims(proto["collision_box"])
        recipe_proto = self.data.recipes[recipe]
        ingredients_raw = solve.Data.ingredients_of(recipe_proto)
        results_raw = solve.Data.results_of(recipe_proto)
        inputs = [solve.name_of(e) for e in ingredients_raw]
        outputs = [solve.name_of(e) for e in results_raw]

        fluid_table = self.data.prototypes.get("fluid", {})
        has_fluids = any(p in fluid_table for p in (*inputs, *outputs))
        layout = self._select_cell_layout(
            proto,
            recipe_proto,
            machine_w,
            machine_h,
            inputs,
            outputs,
            ingredients_raw,
            results_raw,
            fluid_table,
            has_fluids,
        )

        speed = float(proto.get("crafting_speed", 1.0))
        raw_cpm = SECONDS_PER_MINUTE * speed / solve.Data.craft_seconds(recipe_proto)
        in_amounts = {solve.name_of(e): solve.amount_of(e) for e in ingredients_raw}
        out_amounts = {solve.name_of(e): solve.amount_of(e) for e in results_raw}
        solid_amounts = [
            amt for p, amt in (*in_amounts.items(), *out_amounts.items()) if p not in fluid_table
        ]
        max_solid_amt = max(solid_amounts, default=0.0)
        port_cap = self.port_item_capacity_per_min()
        eff_cpm = (
            min(raw_cpm, port_cap / max_solid_amt)
            if (max_solid_amt > 0.0 and port_cap > 0.0)
            else raw_cpm
        )

        res_cell: dict[str, Any] = {
            "cell": f"{machine}.{recipe}",
            "machine": machine,
            "recipe": recipe,
            "width": layout["width"],
            "height": layout["height"],
            "machine_width": layout["machine_width"],
            "machine_height": layout["machine_height"],
            "direction": layout["direction"],
            "in_slots": layout["in_slots"],
            "out_slots": layout["out_slots"],
            "has_fluids": has_fluids,
            "watts": watts_of(proto.get("active_energy_usage") or proto.get("energy_usage", 0)),
            "crafts_per_minute": raw_cpm,
            "effective_crafts_per_minute": eff_cpm,
            "in_amounts": in_amounts,
            "out_amounts": out_amounts,
            "inserter_tiers": self.inserter_tiers(),
            "inputs": inputs,
            "outputs": outputs,
        }
        res_cell.update(layout.get("extra", {}))
        for opt_key in ("port_faces", "unused_fluid_ports", "internal_pipes"):
            if layout[opt_key]:
                res_cell[opt_key] = layout[opt_key]
        return res_cell

    def inserter_stack_size(self) -> int:
        """Compute non-bulk electric inserter hand stack size from technology prototypes."""
        effects = (
            eff
            for tech in self.data.techs.values()
            for eff in tech.get("effects", [])
            if eff.get("type") == "inserter-stack-size-bonus"
        )
        bonus = sum(float(eff.get("modifier", 0.0)) for eff in effects)
        return max(1, round(1.0 + bonus))

    def _is_standard_electric_inserter(self, proto: ProtoRecord, spec: ProtoRecord) -> bool:
        """Check whether an inserter prototype is a 1-tile-reach non-bulk electric inserter."""
        energy_source: Any = proto.get("energy_source", {})
        rot_speed: Any = spec.get("rotation_speed", 0.0)
        return (
            energy_source.get("type") == "electric"
            and not proto.get("bulk", False)
            and spec["pickup_reach"] == 1
            and spec["insert_reach"] == 1
            and float(rot_speed) > 0.0
        )

    def inserter_tiers(self) -> list[dict[str, Any]]:
        """Return 1-tile-reach non-bulk electric inserters sorted slowest to fastest."""
        belt_tiers = self.belt_tiers()
        v_lane = float(belt_tiers[0]["items_per_lane"]) if belt_tiers else 0.0
        v_belt = v_lane * LANES_PER_BELT
        max_stack = self.inserter_stack_size()
        tiers: list[dict[str, Any]] = []
        for name, proto in self.inserters.items():
            spec = inserter_spec(proto)
            if not self._is_standard_electric_inserter(proto, spec):
                continue
            rot_speed = float(spec["rotation_speed"])
            t_rot = 1.0 / (rot_speed * TICKS_PER_SECOND)
            rates_by_stack = {
                s: SECONDS_PER_MINUTE
                * float(s)
                / (t_rot + (float(s - 1) / v_belt if v_belt > 0.0 else 0.0))
                for s in range(1, max_stack + 1)
            }
            tiers.append(
                {
                    "name": name,
                    "rotation_speed": rot_speed,
                    "max_stack": max_stack,
                    "rates_by_stack": rates_by_stack,
                }
            )
        tiers.sort(key=lambda t: float(t["rotation_speed"]))
        return tiers

    def port_item_capacity_per_min(self) -> float:
        """Return maximum sustainable items/min for a single standard port inserter."""
        tiers = self.inserter_tiers()
        if not tiers:
            return 0.0
        fastest = tiers[-1]
        max_stack = int(fastest["max_stack"])
        return float(fastest["rates_by_stack"].get(max_stack, 0.0))

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
            h_layer, v_layer = _tier_layer_pair(idx)
            ug_name = tier.get("underground", "underground-belt")
            span = int(tier.get("span", 0))
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
        tiers = self.belt_tiers()
        for idx, tier in enumerate(tiers):
            h_layer, v_layer = _tier_layer_pair(idx)
            layers_dict[h_layer] = {
                "via": "tunnel-pair",
                "tiers": tiers,
                "span": self.pipe_span() if idx == 0 else int(tier.get("span", 0)),
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
