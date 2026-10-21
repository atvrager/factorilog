"""DEF to Factorio blueprint converter.

Reads placed and routed OpenROAD DEF files and netlist JSON metadata,
then generates Factorio 2.0 blueprint exchange strings.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

try:
    from foundry import blueprint, lef
    from foundry.belt_drc import BeltDrcChecker
    from foundry.chassis import CityBlockChassis
    from foundry.circuit import wire_telemetry
    from foundry.def_parser import extract_pins, merge_collinear_wires, parse_def
    from foundry.def_untangle import untangle_def
    from foundry.entity_helpers import (
        DEFAULT_DEF_UNITS,
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        TILE_HALF,
        check_collisions,
        check_power,
        check_underground_pairing,
        entity_collision_half_size,
        entity_size,
        get_compatible_underground_belt,
        get_pole_supply_radius,
        get_pole_wire_reach,
        get_underground_span,
        prune_dead_end_belts,
    )
    from foundry.macro_floorplan import InterBlockRoute, MacroFloorplanner
    from foundry.native_engine import engine as native_engine_mod
    from foundry.native_engine import verifier as verifier_mod
    from foundry.pdn import generate_pdn
    from foundry.placement import PlacementResult, RouteResult, place_components
    from foundry.pump_insertion import insert_pipeline_pumps
    from foundry.router import maze_router as maze_router_mod
    from foundry.router.grid import base_net_name
    from foundry.router.types import RouterNetSpec
except ImportError:
    import blueprint
    import lef
    from belt_drc import BeltDrcChecker
    from chassis import CityBlockChassis
    from circuit import wire_telemetry
    from def_parser import extract_pins, merge_collinear_wires, parse_def
    from def_untangle import untangle_def
    from entity_helpers import (
        DEFAULT_DEF_UNITS,
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        TILE_HALF,
        check_collisions,
        check_power,
        check_underground_pairing,
        entity_collision_half_size,
        entity_size,
        get_compatible_underground_belt,
        get_pole_supply_radius,
        get_pole_wire_reach,
        get_underground_span,
        prune_dead_end_belts,
    )
    from macro_floorplan import InterBlockRoute, MacroFloorplanner
    from native_engine import engine as native_engine_mod
    from native_engine import verifier as verifier_mod
    from pdn import generate_pdn
    from placement import PlacementResult, RouteResult, place_components
    from pump_insertion import insert_pipeline_pumps
    from router import maze_router as maze_router_mod
    from router.grid import base_net_name
    from router.types import RouterNetSpec

__all__ = [
    "DIRECTION_EAST",
    "DIRECTION_NORTH",
    "DIRECTION_SOUTH",
    "DIRECTION_WEST",
    "Def2BpConfig",
    "build_entities",
    "check_collisions",
    "check_power",
    "check_underground_pairing",
    "def_to_blueprint",
    "entity_collision_half_size",
    "entity_size",
    "extract_pins",
    "jumper_belts_across_chassis_poles",
    "main",
    "merge_collinear_wires",
    "parse_def",
    "prune_dead_end_belts",
    "route_with_maze_router",
    "untangle_def",
]

JsonMap: TypeAlias = Mapping[str, Any]
MutableJsonMap: TypeAlias = dict[str, Any]
EntityDict: TypeAlias = dict[str, Any]
EngineRef: TypeAlias = Any
TileCoord: TypeAlias = tuple[int, int]
GridCoord: TypeAlias = tuple[float, float]
TerminalSpec: TypeAlias = tuple[TileCoord, bool, int | None, int | None]

COORD_PRECISION: int = 2
TIME_PRECISION: int = 3
BOX_EPSILON: float = 1e-6
HALF_DIVISOR: float = 2.0
DIE_X_MAX_IDX: int = 2
DIE_Y_MAX_IDX: int = 3
DIE_AREA_LEN: int = 4
MERGE_FAR_OFFSET: int = 3
PUMP_OUT_OFFSET: int = 2
PORT_PAD_EXTENT: int = 2
PIN_CLEARANCE_RADIUS: float = 3.5
BORDER_POLE_MARGIN: float = 4.5
CORNER_POLE_MODS: tuple[float, float] = (5.0, 95.0)
BIG_POLE_DELTAS: tuple[float, float] = (-0.5, 0.5)
RADAR_DELTAS: tuple[float, float, float] = (-1.0, 0.0, 1.0)
BAY_Y_MIN: int = 3
BAY_Y_MAX: int = 16
BAY_Y_STOP: int = 17
MIN_PROGRAMMATIC_BLOCKS: int = 4
PROGRAMMATIC_CHANNEL_W: int = 8
FALLBACK_CORE_DIM: int = 94
FALLBACK_CHANNEL_W: int = 6
CHANNEL_IN_OFFSET: float = 1.5
PIN_DBU_SHIFT: int = 500
LAMP_BUCKET_STEP: int = 4
NEIGHBOR_BUCKET_DELTAS: tuple[int, int, int] = (-1, 0, 1)
PIN_GUARD_OFFSETS: tuple[int, int, int] = (1, 2, 3)
WIRE_COPPER_ID: int = 5
WIRE_RED_ID: int = 1
WIRE_GREEN_ID: int = 2
WIRE_EDGE_LEN: int = 4
WIRE_PORT_A_IDX: int = 1
WIRE_PORT_B_IDX: int = 3
WIRE_NODE_B_IDX: int = 2
POLE_ALIGN_TOLERANCE: float = 0.1
POLE_REACH_SLACK: float = 0.5
JUMPER_SPAN_PADDING: int = 3
MAX_LOGGED_FAILURES: int = 25
MAX_ERROR_SUMMARY: int = 5
CARDINAL_NEIGHBORS: tuple[TileCoord, ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
NORTH_SOUTH_DIRS: tuple[int, int] = (DIRECTION_NORTH, DIRECTION_SOUTH)
WEST_EAST_ORIENT: tuple[str, str] = ("W", "E")
DRIVER_PIN_NAMES: frozenset[str] = frozenset({"y", "y1", "y2", "out"})

FACE_INWARD_DIR: dict[str, int] = {
    "E": DIRECTION_WEST,
    "W": DIRECTION_EAST,
    "N": DIRECTION_SOUTH,
    "S": DIRECTION_NORTH,
}

ORIENT_SPLITTER_DIR: dict[str, int] = {
    "W": DIRECTION_NORTH,
    "E": DIRECTION_SOUTH,
    "S": DIRECTION_WEST,
    "N": DIRECTION_EAST,
}

ORIENT_PUMP_DIR: dict[str, int] = {
    "W": DIRECTION_WEST,
    "E": DIRECTION_EAST,
    "S": DIRECTION_SOUTH,
    "N": DIRECTION_NORTH,
}

# (orient, normalized_port) -> (dx, dy, is_driver)
MERGE2_PORT_SPECS: dict[tuple[str, str], tuple[int, int, bool]] = {
    ("W", "a"): (0, MERGE_FAR_OFFSET, False),
    ("W", "a1"): (0, MERGE_FAR_OFFSET, False),
    ("W", "b"): (1, MERGE_FAR_OFFSET, False),
    ("W", "a2"): (1, MERGE_FAR_OFFSET, False),
    ("W", "y"): (1, -1, True),
    ("E", "a"): (0, -1, False),
    ("E", "a1"): (0, -1, False),
    ("E", "b"): (1, -1, False),
    ("E", "a2"): (1, -1, False),
    ("E", "y"): (1, MERGE_FAR_OFFSET, True),
    ("S", "a"): (MERGE_FAR_OFFSET, 0, False),
    ("S", "a1"): (MERGE_FAR_OFFSET, 0, False),
    ("S", "b"): (MERGE_FAR_OFFSET, 1, False),
    ("S", "a2"): (MERGE_FAR_OFFSET, 1, False),
    ("S", "y"): (-1, 1, True),
    ("N", "a"): (-1, 0, False),
    ("N", "a1"): (-1, 0, False),
    ("N", "b"): (-1, 1, False),
    ("N", "a2"): (-1, 1, False),
    ("N", "y"): (MERGE_FAR_OFFSET, 1, True),
}

# (is_west_east, normalized_port) -> (dx, dy, is_driver)
SPLIT2_PORT_SPECS: dict[tuple[bool, str], tuple[int, int, bool]] = {
    (True, "a"): (0, -1, False),
    (True, "a1"): (0, -1, False),
    (True, "b"): (1, -1, False),
    (True, "a2"): (1, -1, False),
    (True, "y1"): (0, 1, True),
    (True, "y2"): (1, 1, True),
    (False, "a"): (-1, 0, False),
    (False, "a1"): (-1, 0, False),
    (False, "b"): (-1, 1, False),
    (False, "a2"): (-1, 1, False),
    (False, "y1"): (1, 0, True),
    (False, "y2"): (1, 1, True),
}

PUMP_PORT_SPECS: dict[str, tuple[int, int, bool]] = {
    "a": (0, -1, False),
    "y": (0, PUMP_OUT_OFFSET, True),
}

PIN_PAD_OFFSETS: dict[str, tuple[TileCoord, ...]] = {
    "INPUT": ((-1, 0), (-2, 0), (-1, -1), (-1, 1)),
    "OUTPUT": ((1, 0), (2, 0), (1, -1), (1, 1)),
}


@dataclass(frozen=True)
class Def2BpConfig:
    """Runtime configuration parameters for DEF-to-blueprint compilation."""

    default_units: float = DEFAULT_DEF_UNITS
    block_pitch: int = 100
    highway_max_iterations: int = 60
    verifier_ticks: int = 60
    slow_block_sec: float = 0.5
    cityblock_filename: str = "cityblock.bp"
    big_pole_wire_reach: float = get_pole_wire_reach("big-electric-pole")
    med_pole_wire_reach: float = get_pole_wire_reach("medium-electric-pole")
    big_pole_supply_radius: float = get_pole_supply_radius("big-electric-pole")
    med_pole_supply_radius: float = get_pole_supply_radius("medium-electric-pole")
    max_underground_span: int = get_underground_span("express-underground-belt")
    mid_underground_span: int = get_underground_span("fast-underground-belt")


DEFAULT_DEF2BP_CONFIG = Def2BpConfig()


def _as_map(val: object) -> JsonMap:
    return val if isinstance(val, Mapping) else {}


def _as_list(val: object) -> list[object]:
    return list(val) if isinstance(val, (list, tuple, set)) else []


def _as_float(val: object, default: float = 0.0) -> float:
    return float(val) if isinstance(val, (int, float, str)) and val != "" else default


def _as_int(val: object, default: int = 0) -> int:
    return int(float(val)) if isinstance(val, (int, float, str)) and val != "" else default


def _as_str(val: object, default: str = "") -> str:
    return str(val) if val is not None and val != "" else default


def _entity_xy(ent: JsonMap) -> GridCoord:
    pos = ent.get("position", {})
    if isinstance(pos, Mapping):
        return _as_float(pos.get("x")), _as_float(pos.get("y"))
    if isinstance(pos, Sequence) and len(pos) >= 2:
        return _as_float(pos[0]), _as_float(pos[1])
    return 0.0, 0.0


def _pin_inward_dir(pos: TileCoord, fallback: int | None) -> int | None:
    if pos[0] == 0:
        return DIRECTION_WEST
    if pos[1] == 0:
        return DIRECTION_NORTH
    return fallback


def _is_fluid_name(name: str, clean_fluids: set[str] | None) -> bool:
    if not clean_fluids:
        return False
    norm = name.replace("-", "_")
    return name in clean_fluids or norm in clean_fluids


def _resolve_pin_terminal(
    port: str,
    net_name: str,
    pin_dict: Mapping[str, TileCoord],
    valid_in_pins: set[GridCoord],
    clean_fluids: set[str] | None,
) -> TerminalSpec | None:
    pos = pin_dict.get(net_name) or pin_dict.get(port)
    if pos is None:
        return None
    is_driver = (float(pos[0]), float(pos[1])) in valid_in_pins
    base_comm = net_name.split("__")[0]
    if _is_fluid_name(base_comm, clean_fluids) or _is_fluid_name(port, clean_fluids):
        return (pos, is_driver, None, _pin_inward_dir(pos, None))
    if is_driver:
        pin_head = DIRECTION_SOUTH if (pos[0] != 0 and pos[1] == 0) else DIRECTION_EAST
        return (pos, True, pin_head, pin_head)
    sink_head = _pin_inward_dir(pos, DIRECTION_EAST)
    return (pos, False, sink_head, sink_head)


def _macro_port_headings(
    geom: JsonMap,
    is_output: bool,
    is_fluid_port: bool,
) -> tuple[int | None, int | None]:
    default_face = "E" if is_output else "W"
    face = str(geom.get("face", default_face))
    inward = FACE_INWARD_DIR.get(face, DIRECTION_NORTH)
    raw_dir = geom.get("approach_dir")
    approach_dir = raw_dir if isinstance(raw_dir, int) else None
    if is_fluid_port:
        return (None, inward)
    if is_output:
        return (approach_dir, None)
    sink_head = approach_dir if face in WEST_EAST_ORIENT else None
    return (None, sink_head)


def _resolve_macro_cell_port(
    cell: JsonMap,
    gx: float,
    gy: float,
    port: str,
    clean_fluids: set[str] | None,
    is_output: bool,
) -> TerminalSpec | None:
    port_key = "outputs" if is_output else "inputs"
    raw_ports = [_as_str(p) for p in _as_list(cell.get(port_key))]
    norm_ports = [p.replace("-", "_") for p in raw_ports]
    clean_port = port.replace("-", "_")
    if clean_port not in norm_ports:
        return None
    orig_name = raw_ports[norm_ports.index(clean_port)]
    geom = lef.get_macro_port_geometry(dict(cell), orig_name, is_output=is_output)
    is_fluid_port = _is_fluid_name(orig_name, clean_fluids)
    pos_key = "inserter_pos" if is_fluid_port else "approach_pos"
    ax, ay = geom[pos_key]
    coord = (math.floor(gx + ax), math.floor(gy + ay))
    head, term_head = _macro_port_headings(geom, is_output, is_fluid_port)
    return (coord, is_output, head, term_head)


def _format_fixed_port_spec(
    igx: int,
    igy: int,
    spec: tuple[int, int, bool],
    heading: int,
) -> TerminalSpec:
    dx, dy, is_drv = spec
    term_head = None if is_drv else heading
    return ((igx + dx, igy + dy), is_drv, heading, term_head)


def _resolve_special_macro_terminal(
    cell_id: str,
    macro_name: str,
    orient: str,
    port: str,
    igx: int,
    igy: int,
) -> tuple[bool, TerminalSpec | None]:
    names = {cell_id, macro_name}
    if "belt_merge2" in names:
        spec = MERGE2_PORT_SPECS.get((orient, port))
        s_dir = ORIENT_SPLITTER_DIR.get(orient, DIRECTION_EAST)
        return True, _format_fixed_port_spec(igx, igy, spec, s_dir) if spec else None
    if names & {"belt_split2", "belt_balancer2"}:
        spec = SPLIT2_PORT_SPECS.get((orient in WEST_EAST_ORIENT, port))
        s_dir = ORIENT_SPLITTER_DIR.get(orient, DIRECTION_EAST)
        return True, _format_fixed_port_spec(igx, igy, spec, s_dir) if spec else None
    if "pump" in names:
        spec = PUMP_PORT_SPECS.get(port)
        p_dir = ORIENT_PUMP_DIR.get(orient, DIRECTION_NORTH)
        return True, _format_fixed_port_spec(igx, igy, spec, p_dir) if spec else None
    return False, None


def _resolve_terminal(
    inst_name: str,
    port: str,
    net_name: str,
    comp_map: Mapping[str, JsonMap],
    inst_map: Mapping[str, JsonMap],
    cell_map: Mapping[str, JsonMap],
    pin_dict: Mapping[str, TileCoord],
    valid_in_pins: set[GridCoord],
    scale: float,
    clean_fluids: set[str] | None = None,
) -> TerminalSpec | None:
    """Resolve grid coordinate, driver role, and headings for a net connection."""
    if inst_name == "PIN":
        return _resolve_pin_terminal(port, net_name, pin_dict, valid_in_pins, clean_fluids)
    if inst_name not in comp_map:
        return None

    comp = comp_map[inst_name]
    gx = _as_float(comp.get("x")) * scale
    gy = _as_float(comp.get("y")) * scale
    igx, igy = round(gx), round(gy)
    inst = inst_map.get(inst_name, {})
    macro_name = _as_str(comp.get("macro"))
    cell_id = _as_str(inst.get("cell"), macro_name)
    orient = _as_str(comp.get("orient"), "N")

    matched_special, special_res = _resolve_special_macro_terminal(
        cell_id, macro_name, orient, port, igx, igy
    )
    if matched_special:
        return special_res or ((igx, igy), False, None, None)

    cell = cell_map.get(cell_id, {})
    res = _resolve_macro_cell_port(cell, gx, gy, port, clean_fluids, is_output=True)
    if res is None:
        res = _resolve_macro_cell_port(cell, gx, gy, port, clean_fluids, is_output=False)
    return res or ((igx, igy), False, None, None)


def _extract_pins_and_obstacles(
    def_data: JsonMap,
    placement: PlacementResult,
    scale: float,
) -> tuple[set[TileCoord], dict[str, TileCoord], set[GridCoord], set[GridCoord]]:
    obstacles = {(math.floor(pos[0]), math.floor(pos[1])) for pos in placement.occupied_positions}
    pin_dict: dict[str, TileCoord] = {}
    valid_in_pins: set[GridCoord] = set()
    valid_out_pins: set[GridCoord] = set()
    for pin_obj in _as_list(def_data.get("pins")):
        pin = _as_map(pin_obj)
        px = math.floor(_as_float(pin.get("x")) * scale)
        py = math.floor(_as_float(pin.get("y")) * scale)
        pname = _as_str(pin.get("name"), _as_str(pin.get("net")))
        pin_dict[pname] = (px, py)
        direction = _as_str(pin.get("direction"))
        if direction == "INPUT":
            valid_in_pins.add((float(px), float(py)))
        elif direction == "OUTPUT":
            valid_out_pins.add((float(px), float(py)))
        for dx, dy in PIN_PAD_OFFSETS.get(direction, ()):
            obstacles.add((px + dx, py + dy))
    return obstacles, pin_dict, valid_in_pins, valid_out_pins


def _build_fluid_mst_specs(
    nname: str,
    commodity: str,
    terms: Sequence[tuple[TileCoord, int | None, int | None]],
) -> list[RouterNetSpec]:
    seen_nodes: dict[TileCoord, tuple[TileCoord, int | None, int | None]] = {}
    for t in terms:
        seen_nodes.setdefault(t[0], t)
    nodes = list(seen_nodes.values())
    if len(nodes) < 2:
        return []
    in_tree = [nodes[0]]
    remaining = set(nodes[1:])
    specs: list[RouterNetSpec] = []
    while remaining:
        u, v = min(
            ((u_node, v_node) for u_node in in_tree for v_node in remaining),
            key=lambda pair: (
                abs(pair[0][0][0] - pair[1][0][0]) + abs(pair[0][0][1] - pair[1][0][1])
            ),
        )
        in_tree.append(v)
        remaining.remove(v)
        specs.append(
            RouterNetSpec(
                name=f"{nname}_mst_{len(specs)}",
                source=v[0],
                sink=u[0],
                commodity=commodity,
                is_fluid=True,
                start_heading=v[1],
                end_heading=None,
                terminal_heading=None,
                source_inward_dir=v[2],
                sink_inward_dir=u[2],
            )
        )
    return specs


def _build_point_to_point_specs(
    nname: str,
    commodity: str,
    is_fluid: bool,
    drivers: Sequence[tuple[TileCoord, int | None, int | None]],
    sinks: Sequence[tuple[TileCoord, int | None, int | None]],
) -> list[RouterNetSpec]:
    multi = len(drivers) > 1 or len(sinks) > 1
    return [
        RouterNetSpec(
            name=f"{nname}_d{d_idx}_s{s_idx}" if multi else nname,
            source=d_pos,
            sink=s_pos,
            commodity=commodity,
            is_fluid=is_fluid,
            start_heading=d_head,
            end_heading=s_head,
            terminal_heading=None if is_fluid else s_term,
            source_inward_dir=d_term if is_fluid else None,
            sink_inward_dir=s_term if is_fluid else None,
        )
        for d_idx, (d_pos, d_head, d_term) in enumerate(drivers)
        for s_idx, (s_pos, s_head, s_term) in enumerate(sinks)
        if d_pos != s_pos
    ]


def _partition_net_terminals(
    conns: Sequence[object],
    nname: str,
    comp_map: Mapping[str, JsonMap],
    inst_map: Mapping[str, JsonMap],
    cell_map: Mapping[str, JsonMap],
    pin_dict: Mapping[str, TileCoord],
    valid_in_pins: set[GridCoord],
    scale: float,
    clean_fluids: set[str],
) -> tuple[
    list[tuple[TileCoord, int | None, int | None]], list[tuple[TileCoord, int | None, int | None]]
]:
    drivers: list[tuple[TileCoord, int | None, int | None]] = []
    sinks: list[tuple[TileCoord, int | None, int | None]] = []
    for conn in conns:
        pair = _as_list(conn)
        if len(pair) < 2:
            continue
        term = _resolve_terminal(
            _as_str(pair[0]),
            _as_str(pair[1]),
            nname,
            comp_map,
            inst_map,
            cell_map,
            pin_dict,
            valid_in_pins,
            scale,
            clean_fluids=clean_fluids,
        )
        if term is None:
            continue
        coord, is_drv, head, term_head = term
        target_list = drivers if is_drv else sinks
        target_list.append((coord, head, term_head))
    if not drivers and sinks:
        drivers.append(sinks.pop(0))
    elif not sinks and drivers:
        sinks.append(drivers.pop())
    return drivers, sinks


def _build_net_specs_for_net(
    net: JsonMap,
    comp_map: Mapping[str, JsonMap],
    inst_map: Mapping[str, JsonMap],
    cell_map: Mapping[str, JsonMap],
    pin_dict: Mapping[str, TileCoord],
    valid_in_pins: set[GridCoord],
    scale: float,
    clean_fluids: set[str],
) -> list[RouterNetSpec]:
    nname = _as_str(net.get("name"))
    conns = _as_list(net.get("connections"))
    if len(conns) < 2:
        return []
    drivers, sinks = _partition_net_terminals(
        conns, nname, comp_map, inst_map, cell_map, pin_dict, valid_in_pins, scale, clean_fluids
    )
    base_comm = nname.split("__")[0].replace("-", "_")
    is_fluid = _is_fluid_name(base_comm, clean_fluids)
    commodity = base_comm.replace("_", "-")
    if is_fluid and (len(drivers) > 1 or len(sinks) > 1):
        return _build_fluid_mst_specs(nname, commodity, [*drivers, *sinks])
    return _build_point_to_point_specs(nname, commodity, is_fluid, drivers, sinks)


def _bind_pin_constraint(
    tile: TileCoord,
    heading: int | None,
    valid_pins: set[GridCoord],
    pin_no_hood_tiles: set[TileCoord],
    component_headings: dict[TileCoord, int],
) -> None:
    if (float(tile[0]), float(tile[1])) not in valid_pins:
        return
    pin_no_hood_tiles.add(tile)
    if heading is not None:
        component_headings[tile] = heading


def _record_pin_spec_constraints(
    net_specs: Sequence[RouterNetSpec],
    valid_in_pins: set[GridCoord],
    valid_out_pins: set[GridCoord],
    pin_no_hood_tiles: set[TileCoord],
    component_headings: dict[TileCoord, int],
) -> None:
    for nspec in net_specs:
        if nspec.is_fluid:
            continue
        _bind_pin_constraint(
            nspec.source, nspec.start_heading, valid_in_pins, pin_no_hood_tiles, component_headings
        )
        _bind_pin_constraint(
            nspec.sink, nspec.end_heading, valid_out_pins, pin_no_hood_tiles, component_headings
        )


def _record_single_entity_constraint(
    ent: EntityDict,
    pin_no_hood_tiles: set[TileCoord],
    component_headings: dict[TileCoord, int],
    fluid_port_approaches: dict[TileCoord, set[str]],
) -> None:
    ename = _as_str(ent.get("name"))
    cx, cy = _entity_xy(ent)
    d = _as_int(ent.get("direction"), DIRECTION_NORTH)
    if "splitter" in ename:
        is_ns = d in NORTH_SOUTH_DIRS
        offsets = (
            ((-TILE_HALF, 0.0), (TILE_HALF, 0.0))
            if is_ns
            else ((0.0, -TILE_HALF), (0.0, TILE_HALF))
        )
        for ox, oy in offsets:
            component_headings[(math.floor(cx + ox), math.floor(cy + oy))] = d
        return
    if "transport-belt" in ename:
        tx, ty = math.floor(cx), math.floor(cy)
        component_headings[(tx, ty)] = d
        dx, dy = DIR_STEPS.get(d, (0, 0))
        pin_no_hood_tiles.add((tx + dx, ty + dy))
        return
    if ename == "pipe" and ent.get("fluid"):
        tx, ty = math.floor(cx), math.floor(cy)
        p_comm = _as_str(ent.get("fluid")).replace("_", "-")
        for odx, ody in ((0, 0), *CARDINAL_NEIGHBORS):
            fluid_port_approaches.setdefault((tx + odx, ty + ody), set()).add(p_comm)


def _collect_router_constraints(
    net_specs: Sequence[RouterNetSpec],
    entities: Sequence[EntityDict],
    valid_in_pins: set[GridCoord],
    valid_out_pins: set[GridCoord],
) -> tuple[set[TileCoord], dict[TileCoord, int], dict[TileCoord, set[str]]]:
    pin_no_hood_tiles: set[TileCoord] = set()
    component_headings: dict[TileCoord, int] = {}
    fluid_port_approaches: dict[TileCoord, set[str]] = {}
    _record_pin_spec_constraints(
        net_specs, valid_in_pins, valid_out_pins, pin_no_hood_tiles, component_headings
    )
    for ent in entities:
        _record_single_entity_constraint(
            ent, pin_no_hood_tiles, component_headings, fluid_port_approaches
        )
    return pin_no_hood_tiles, component_headings, fluid_port_approaches


def _check_and_raise_failed_routes(
    routes: Mapping[str, object],
    strict_drc: bool,
    prefix: str,
) -> list[object]:
    failed = [r for r in routes.values() if not getattr(r, "is_success", True)]
    if failed and strict_drc:
        for f in failed[:MAX_LOGGED_FAILURES]:
            sys.stderr.write(
                f"factorilog [maze_router]: NET ROUTE FAILED: {getattr(f, 'net_name', '')}: {getattr(f, 'error_message', '')}\n"
            )
        names = ", ".join(str(getattr(r, "net_name", "")) for r in failed[:MAX_ERROR_SUMMARY])
        raise ValueError(f"{prefix} failed on {len(failed)} net(s): {names}")
    return failed


def _find_bay_blocks(
    components: Sequence[object],
    scale: float,
    grid_w: int,
    grid_h: int,
    pitch: int,
) -> set[TileCoord]:
    bay_blocks: set[TileCoord] = set()
    for comp_obj in components:
        comp = _as_map(comp_obj)
        cx_t = round(_as_float(comp.get("x")) * scale)
        cy_t = round(_as_float(comp.get("y")) * scale)
        bx_c, by_c = cx_t // pitch, cy_t // pitch
        if 0 <= bx_c < grid_w and 0 <= by_c < grid_h and BAY_Y_MIN <= (cy_t % pitch) <= BAY_Y_MAX:
            bay_blocks.add((bx_c, by_c))
    return bay_blocks


def _build_block_core_obstacles(
    b_min_x: int,
    b_min_y: int,
    b_max_x: int,
    b_max_y: int,
    obstacles: set[TileCoord],
    blk_nets: Sequence[RouterNetSpec],
) -> set[TileCoord]:
    blk_obs = {
        pt for pt in obstacles if b_min_x <= pt[0] <= b_max_x and b_min_y <= pt[1] <= b_max_y
    }
    for rx in range(b_min_x, b_max_x + 1):
        blk_obs.add((rx, b_min_y))
        blk_obs.add((rx, b_max_y))
    for ry in range(b_min_y, b_max_y + 1):
        blk_obs.add((b_min_x, ry))
        blk_obs.add((b_max_x, ry))
    for ns in blk_nets:
        blk_obs.discard(ns.source)
        blk_obs.discard(ns.sink)
    return blk_obs


def _build_highway_obstacles(
    grid_w: int,
    grid_h: int,
    margin: int,
    pitch: int,
    bay_blocks: set[TileCoord],
    obstacles: set[TileCoord],
    highway_specs: Sequence[RouterNetSpec],
) -> set[TileCoord]:
    hwy_obs = {
        (bx * pitch + dx, by * pitch + dy)
        for bx in range(grid_w)
        for by in range(grid_h)
        for dx in range(margin, pitch - margin)
        for dy in range(margin, pitch - margin)
    }
    for bx_c, by_c in bay_blocks:
        for nx_t in range(bx_c * pitch + margin, bx_c * pitch + pitch - margin):
            for ny_t in range(by_c * pitch + margin, by_c * pitch + BAY_Y_STOP):
                hwy_obs.discard((nx_t, ny_t))
    hwy_obs.update(obstacles)
    for ns in highway_specs:
        hwy_obs.discard(ns.source)
        hwy_obs.discard(ns.sink)
    return hwy_obs


def _route_stage1_blocks(
    intra_block_specs: Mapping[TileCoord, list[RouterNetSpec]],
    macro_planner: MacroFloorplanner,
    bay_blocks: set[TileCoord],
    margin: int,
    obstacles: set[TileCoord],
    prototypes: dict[str, object],
    merged_headings: dict[TileCoord, int],
    port_no_hood_tiles: set[TileCoord],
    fluid_port_approaches: dict[TileCoord, set[str]],
    strict_drc: bool,
    cfg: Def2BpConfig,
) -> tuple[dict[str, object], dict[GridCoord, EntityDict], list[RouterNetSpec]]:
    routes: dict[str, object] = {}
    entities_by_pos: dict[GridCoord, EntityDict] = {}
    all_routed_specs: list[RouterNetSpec] = []
    total_intra = sum(len(ns) for ns in intra_block_specs.values())
    t_stage1 = time.perf_counter()
    print(
        f"[def2bp] Stage 1: routing {total_intra} intra-block nets across {len(intra_block_specs)} blocks (margin={margin})...",
        file=sys.stderr,
        flush=True,
    )
    for (bx, by), blk_nets in sorted(intra_block_specs.items()):
        if not blk_nets:
            continue
        t_blk = time.perf_counter()
        b_min_x, b_min_y, b_max_x, b_max_y = macro_planner._block_core_bbox(
            (bx, by), bay_blocks, margin=margin
        )
        blk_obs = _build_block_core_obstacles(
            b_min_x, b_min_y, b_max_x, b_max_y, obstacles, blk_nets
        )
        blk_router = maze_router_mod.DualLaneMazeRouter(
            min_x=b_min_x,
            max_x=b_max_x,
            min_y=b_min_y,
            max_y=b_max_y,
            engine=None,
            prototypes=prototypes,
            obstacles=blk_obs,
            component_headings=merged_headings,
            no_hood_tiles=port_no_hood_tiles,
            fluid_port_approaches=fluid_port_approaches,
        )
        b_routes, b_entities, _ = blk_router.route(blk_nets, verify=False)
        dt_blk = time.perf_counter() - t_blk
        b_failed = _check_and_raise_failed_routes(
            b_routes, strict_drc, f"Stage 1 block ({bx},{by}) router"
        )
        if dt_blk >= cfg.slow_block_sec or b_failed:
            print(
                f"[def2bp]   block ({bx},{by}): {len(blk_nets)} nets in {dt_blk:.2f}s (failed={len(b_failed)})",
                file=sys.stderr,
                flush=True,
            )
        routes.update(b_routes)
        all_routed_specs.extend(blk_nets)
        for ent in b_entities:
            ex, ey = _entity_xy(ent)
            entities_by_pos[(round(ex, COORD_PRECISION), round(ey, COORD_PRECISION))] = ent
    print(
        f"[def2bp] Stage 1 completed in {time.perf_counter() - t_stage1:.2f}s",
        file=sys.stderr,
        flush=True,
    )
    return routes, entities_by_pos, all_routed_specs


def _route_hierarchical(
    def_data: MutableJsonMap,
    net_specs: list[RouterNetSpec],
    grid_w: int,
    grid_h: int,
    scale: float,
    obstacles: set[TileCoord],
    prototypes: dict[str, object],
    component_headings: dict[TileCoord, int],
    pin_no_hood_tiles: set[TileCoord],
    fluid_port_approaches: dict[TileCoord, set[str]],
    engine: EngineRef = None,
    strict_drc: bool = False,
    cfg: Def2BpConfig = DEFAULT_DEF2BP_CONFIG,
) -> tuple[dict[str, object], list[EntityDict], list[RouterNetSpec], object | None]:
    pitch = cfg.block_pitch
    xs = [c for ns in net_specs for c in (ns.source[0], ns.sink[0])]
    ys = [c for ns in net_specs for c in (ns.source[1], ns.sink[1])]
    hwy_min_x = min([0, *xs])
    hwy_min_y = min([0, *ys])
    hwy_max_x = max([grid_w * pitch - 1, *xs])
    hwy_max_y = max([grid_h * pitch - 1, *ys])
    bay_blocks = _find_bay_blocks(
        _as_list(def_data.get("components")), scale, grid_w, grid_h, pitch
    )

    macro_planner = MacroFloorplanner()
    (
        intra_block_specs,
        highway_specs,
        port_no_hood_tiles,
        port_headings,
        h_floorplan,
    ) = macro_planner.plan_hierarchical_nets(
        net_specs=net_specs,
        grid_w=grid_w,
        grid_h=grid_h,
        obstacles=obstacles,
        min_x=hwy_min_x,
        min_y=hwy_min_y,
        max_x=hwy_max_x,
        max_y=hwy_max_y,
        bay_blocks=bay_blocks,
    )
    port_no_hood_tiles.update(pin_no_hood_tiles)
    def_data["_macro_floorplan"] = h_floorplan
    def_data["_bay_blocks"] = bay_blocks
    def_data["_highway_specs"] = highway_specs
    merged_headings = {**component_headings, **port_headings}
    margin = h_floorplan.chassis.core_margin_x

    routes, entities_by_pos, all_routed_specs = _route_stage1_blocks(
        intra_block_specs,
        macro_planner,
        bay_blocks,
        margin,
        obstacles,
        prototypes,
        merged_headings,
        port_no_hood_tiles,
        fluid_port_approaches,
        strict_drc,
        cfg,
    )

    if highway_specs:
        t_stage2 = time.perf_counter()
        print(
            f"[def2bp] Stage 2: routing {len(highway_specs)} highway nets across {grid_w}x{grid_h} street grid...",
            file=sys.stderr,
            flush=True,
        )
        hwy_obs = _build_highway_obstacles(
            grid_w, grid_h, margin, pitch, bay_blocks, obstacles, highway_specs
        )
        hwy_router = maze_router_mod.DualLaneMazeRouter(
            min_x=hwy_min_x,
            max_x=hwy_max_x,
            min_y=hwy_min_y,
            max_y=hwy_max_y,
            engine=None,
            prototypes=prototypes,
            obstacles=hwy_obs,
            component_headings=merged_headings,
            no_hood_tiles=port_no_hood_tiles,
            fluid_port_approaches=fluid_port_approaches,
        )
        h_routes, h_entities, _ = hwy_router.route(
            highway_specs, max_iterations=cfg.highway_max_iterations, verify=False
        )
        h_failed = _check_and_raise_failed_routes(h_routes, strict_drc, "Stage 2 highway router")
        print(
            f"[def2bp] Stage 2 completed in {time.perf_counter() - t_stage2:.2f}s (failed={len(h_failed)})",
            file=sys.stderr,
            flush=True,
        )
        routes.update(h_routes)
        all_routed_specs.extend(highway_specs)
        for ent in h_entities:
            ex, ey = _entity_xy(ent)
            entities_by_pos[(round(ex, COORD_PRECISION), round(ey, COORD_PRECISION))] = ent

    entities = list(entities_by_pos.values())
    for idx, ent in enumerate(entities, start=1):
        ent["entity_number"] = idx

    failed = [res for res in routes.values() if not getattr(res, "is_success", True)]
    verif_res = None
    if engine is not None and entities and not failed:
        t_verif = time.perf_counter()
        verifier = verifier_mod.NativeLayoutVerifier(engine, prototypes=prototypes)
        verifier_nets = [
            verifier_mod.NetSpec(
                name=n.name,
                commodity=n.commodity,
                source_pos=(n.source[0] + TILE_HALF, n.source[1] + TILE_HALF),
                sink_pos=(n.sink[0] + TILE_HALF, n.sink[1] + TILE_HALF),
                is_fluid=n.is_fluid,
            )
            for n in net_specs
        ]
        verif_res = verifier.verify_blueprint(
            entities, nets=verifier_nets, ticks=cfg.verifier_ticks
        )
        print(
            f"[def2bp] NativeLayoutVerifier completed in {time.perf_counter() - t_verif:.2f}s",
            file=sys.stderr,
            flush=True,
        )
    return routes, entities, all_routed_specs, verif_res


def _resolve_router_engine(engine: EngineRef) -> EngineRef:
    if engine is False:
        return None
    if engine is not None:
        return engine
    try:
        return native_engine_mod.FactorioNativeEngine()
    except Exception:
        return None


def _report_router_diagnostics(
    routes: Mapping[str, object],
    verif_res: object | None,
    strict_drc: bool,
) -> None:
    if verif_res is not None and not getattr(verif_res, "is_valid", True):
        for err in getattr(verif_res, "lvs_errors", ()):
            sys.stderr.write(
                f"factorilog [maze_router]: NATIVE LVS ERROR: {err.net_name}: {err.message}\n"
            )
        for drc in getattr(verif_res, "drc_violations", ()):
            sys.stderr.write(
                f"factorilog [maze_router]: NATIVE DRC ERROR: [{drc.code}] at {drc.position}: {drc.message}\n"
            )
    failed = [res for res in routes.values() if not getattr(res, "is_success", True)]
    for f in failed[:MAX_LOGGED_FAILURES]:
        sys.stderr.write(
            f"factorilog [maze_router]: NET ROUTE FAILED: {getattr(f, 'net_name', '')}: {getattr(f, 'error_message', '')}\n"
        )
    if failed and strict_drc:
        names = ", ".join(str(getattr(r, "net_name", "")) for r in failed[:MAX_ERROR_SUMMARY])
        raise ValueError(f"Router failed on {len(failed)} net(s): {names}")


def _extract_clean_fluids(netlist_data: JsonMap, placement: PlacementResult) -> set[str]:
    raw_fluids = {_as_str(f) for f in _as_list(netlist_data.get("fluids"))}
    if not raw_fluids and placement.fluids:
        raw_fluids = set(placement.fluids)
    underscored = {f.replace("-", "_") for f in raw_fluids}
    hyphenated = {f.replace("_", "-") for f in raw_fluids}
    return underscored | hyphenated | raw_fluids


def _extract_lookup_maps(
    def_data: JsonMap,
    netlist_data: JsonMap,
) -> tuple[dict[str, JsonMap], dict[str, JsonMap], dict[str, JsonMap]]:
    comp_map = {
        _as_str(_as_map(c).get("name")): _as_map(c) for c in _as_list(def_data.get("components"))
    }
    inst_map = {
        _as_str(_as_map(i).get("name")): _as_map(i) for i in _as_list(netlist_data.get("instances"))
    }
    cell_map = {k: _as_map(v) for k, v in _as_map(netlist_data.get("cells")).items()}
    return comp_map, inst_map, cell_map


def _build_surface_tile_maps(
    routes: Mapping[str, object],
) -> tuple[dict[str, dict[GridCoord, int]], dict[GridCoord, set[str]]]:
    net_surface_tiles: dict[str, dict[GridCoord, int]] = {}
    tile_to_nets: dict[GridCoord, set[str]] = {}
    for nname, route_res in routes.items():
        base_name = base_net_name(nname)
        net_surface_tiles.setdefault(base_name, {})
        for node in getattr(route_res, "nodes", ()):
            if node.z == 0:
                k = (node.x + TILE_HALF, node.y + TILE_HALF)
                net_surface_tiles[base_name][k] = node.heading
                tile_to_nets.setdefault(k, set()).add(base_name)
    return net_surface_tiles, tile_to_nets


def _parse_die_area(def_data: JsonMap, units: float, cfg: Def2BpConfig) -> list[float]:
    default_span = cfg.block_pitch * units
    die_area = [_as_float(v) for v in _as_list(def_data.get("die_area"))]
    if len(die_area) < DIE_AREA_LEN:
        return [0.0, 0.0, default_span, default_span]
    return die_area


def route_with_maze_router(
    def_data: MutableJsonMap,
    netlist_data: JsonMap,
    placement: PlacementResult,
    engine: object | None = None,
    strict_drc: bool = False,
    cfg: Def2BpConfig = DEFAULT_DEF2BP_CONFIG,
) -> RouteResult:
    """Route DEF nets using the 3D multi-layer DualLaneMazeRouter."""
    units = _as_float(def_data.get("units"), cfg.default_units)
    scale = 1.0 / units
    die_area = _parse_die_area(def_data, units, cfg)
    min_x, min_y = math.floor(die_area[0] * scale), math.floor(die_area[1] * scale)
    max_x = math.ceil(die_area[DIE_X_MAX_IDX] * scale)
    max_y = math.ceil(die_area[DIE_Y_MAX_IDX] * scale)

    obstacles, pin_dict, valid_in_pins, valid_out_pins = _extract_pins_and_obstacles(
        def_data, placement, scale
    )
    clean_fluids = _extract_clean_fluids(netlist_data, placement)
    comp_map, inst_map, cell_map = _extract_lookup_maps(def_data, netlist_data)

    net_specs: list[RouterNetSpec] = []
    for net_obj in _as_list(def_data.get("nets")):
        net_specs.extend(
            _build_net_specs_for_net(
                _as_map(net_obj),
                comp_map,
                inst_map,
                cell_map,
                pin_dict,
                valid_in_pins,
                scale,
                clean_fluids,
            )
        )

    for nspec in net_specs:
        obstacles.discard(nspec.source)
        obstacles.discard(nspec.sink)
        min_x = min(min_x, nspec.source[0] - PORT_PAD_EXTENT, nspec.sink[0] - PORT_PAD_EXTENT)
        max_x = max(max_x, nspec.source[0] + PORT_PAD_EXTENT, nspec.sink[0] + PORT_PAD_EXTENT)
        min_y = min(min_y, nspec.source[1] - PORT_PAD_EXTENT, nspec.sink[1] - PORT_PAD_EXTENT)
        max_y = max(max_y, nspec.source[1] + PORT_PAD_EXTENT, nspec.sink[1] + PORT_PAD_EXTENT)

    pin_no_hood_tiles, component_headings, fluid_port_approaches = _collect_router_constraints(
        net_specs, placement.entities, valid_in_pins, valid_out_pins
    )
    resolved_engine = _resolve_router_engine(engine)
    prototypes = verifier_mod.load_game_prototypes()

    grid_w = max(1, round((die_area[DIE_X_MAX_IDX] / units) / float(cfg.block_pitch)))
    grid_h = max(1, round((die_area[DIE_Y_MAX_IDX] / units) / float(cfg.block_pitch)))
    if bool(def_data.get("blockages")) and (grid_w * grid_h > 1):
        routes, entities, all_routed_specs, verif_res = _route_hierarchical(
            def_data,
            net_specs,
            grid_w,
            grid_h,
            scale,
            obstacles,
            prototypes,
            component_headings,
            pin_no_hood_tiles,
            fluid_port_approaches,
            resolved_engine,
            strict_drc,
            cfg,
        )
    else:
        router = maze_router_mod.DualLaneMazeRouter(
            min_x=min_x,
            max_x=max_x,
            min_y=min_y,
            max_y=max_y,
            engine=resolved_engine,
            prototypes=prototypes,
            obstacles=obstacles,
            component_headings=component_headings,
            no_hood_tiles=pin_no_hood_tiles,
            fluid_port_approaches=fluid_port_approaches,
        )
        routes, entities, verif_res = router.route(net_specs, verify=resolved_engine is not None)
        all_routed_specs = net_specs

    placement.__dict__["router_routes"] = routes
    placement.__dict__["router_net_specs"] = all_routed_specs
    placement.__dict__["router_verif_res"] = verif_res
    _report_router_diagnostics(routes, verif_res, strict_drc)

    for ent in entities:
        placement.entities.append(ent)
        ex, ey = _entity_xy(ent)
        pos_key = (round(ex, COORD_PRECISION), round(ey, COORD_PRECISION))
        placement.occupied_positions.add(pos_key)
        placement.grid_entities[pos_key] = ent

    net_surface_tiles, tile_to_nets = _build_surface_tile_maps(routes)
    all_pins = valid_in_pins | valid_out_pins
    return RouteResult(
        net_surface_tiles=net_surface_tiles,
        tile_to_nets=tile_to_nets,
        adjacent_fluid_conflicts=set(),
        pin_tiles=all_pins,
        valid_input_pins=valid_in_pins,
        valid_output_pins=valid_out_pins,
    )


def _select_cityblock_chassis(grid_w: int, grid_h: int, cfg: Def2BpConfig) -> CityBlockChassis:
    pitch = cfg.block_pitch
    if grid_w * grid_h >= MIN_PROGRAMMATIC_BLOCKS:
        return CityBlockChassis(
            pitch=pitch, channel_width=PROGRAMMATIC_CHANNEL_W, programmatic=True
        )
    repo_root = Path(__file__).resolve().parent.parent
    for bp_cand in (Path(cfg.cityblock_filename), repo_root / cfg.cityblock_filename):
        if bp_cand.is_file():
            try:
                return CityBlockChassis.from_blueprint_file(bp_cand)
            except Exception:
                pass
    return CityBlockChassis(
        pitch=pitch,
        core_width=FALLBACK_CORE_DIM,
        core_height=FALLBACK_CORE_DIM,
        channel_width=FALLBACK_CHANNEL_W,
    )


def _chassis_entity_footprint(bname: str, bx: float, by: float) -> list[GridCoord]:
    if bname == "big-electric-pole":
        return [
            (round(bx + dx, COORD_PRECISION), round(by + dy, COORD_PRECISION))
            for dx in BIG_POLE_DELTAS
            for dy in BIG_POLE_DELTAS
        ]
    if bname == "radar":
        return [
            (round(bx + dx, COORD_PRECISION), round(by + dy, COORD_PRECISION))
            for dx in RADAR_DELTAS
            for dy in RADAR_DELTAS
        ]
    return [(round(bx, COORD_PRECISION), round(by, COORD_PRECISION))]


def _should_skip_chassis_obstacle(
    bname: str,
    bx: float,
    by: float,
    is_multi: bool,
    pitch: float,
    die_w: float,
    die_h: float,
    pin_coords: Sequence[GridCoord],
) -> bool:
    if is_multi:
        is_corner = (
            bname == "big-electric-pole"
            and (bx % pitch) in CORNER_POLE_MODS
            and (by % pitch) in CORNER_POLE_MODS
        )
        if bname != "radar" and not is_corner:
            return True
    elif (
        bx <= BORDER_POLE_MARGIN
        or bx >= die_w - BORDER_POLE_MARGIN
        or by <= BORDER_POLE_MARGIN
        or by >= die_h - BORDER_POLE_MARGIN
    ):
        return True
    return any(
        abs(bx - px) <= PIN_CLEARANCE_RADIUS and abs(by - py) <= PIN_CLEARANCE_RADIUS
        for px, py in pin_coords
    )


def _register_chassis_router_obstacles(
    def_data: JsonMap,
    placement: PlacementResult,
    chassis_base_entities: Sequence[EntityDict],
    grid_w: int,
    grid_h: int,
    die_w: float,
    die_h: float,
    scale: float,
    cfg: Def2BpConfig,
) -> None:
    pitch = float(cfg.block_pitch)
    pin_coords = [
        (
            math.floor(_as_float(_as_map(p).get("x")) * scale) + TILE_HALF,
            math.floor(_as_float(_as_map(p).get("y")) * scale) + TILE_HALF,
        )
        for p in _as_list(def_data.get("pins"))
    ]
    is_multi = grid_w * grid_h >= MIN_PROGRAMMATIC_BLOCKS
    for be in chassis_base_entities:
        if "position" not in be:
            continue
        bx, by = _entity_xy(be)
        bname = _as_str(be.get("name"))
        if _should_skip_chassis_obstacle(bname, bx, by, is_multi, pitch, die_w, die_h, pin_coords):
            continue
        placement.occupied_positions.update(_chassis_entity_footprint(bname, bx, by))


def _relocate_legacy_input_pin(
    def_data: MutableJsonMap,
    net_name: str,
    ch_x: float,
    dbu: float,
) -> None:
    for pin_obj in _as_list(def_data.get("pins")):
        if isinstance(pin_obj, dict) and net_name in (pin_obj.get("net"), pin_obj.get("name")):
            pin_obj["x"] = int(ch_x * dbu - PIN_DBU_SHIFT)
            pin_obj["y"] = 0


def _emit_legacy_l_channel_belts(
    placement: PlacementResult,
    ch_x: float,
    target_x: float,
    target_y: float,
) -> list[EntityDict]:
    emitted: list[EntityDict] = []
    cur_y = TILE_HALF
    while cur_y < target_y:
        b_ent: EntityDict = {
            "name": "transport-belt",
            "position": {"x": ch_x, "y": cur_y},
            "direction": DIRECTION_SOUTH,
        }
        emitted.append(b_ent)
        placement.entities.append(b_ent)
        placement.occupied_positions.add((ch_x, cur_y))
        cur_y += 1.0
    cur_x = ch_x
    while cur_x <= target_x:
        b_ent = {
            "name": "transport-belt",
            "position": {"x": cur_x, "y": target_y},
            "direction": DIRECTION_EAST,
        }
        emitted.append(b_ent)
        placement.entities.append(b_ent)
        placement.occupied_positions.add((cur_x, target_y))
        cur_x += 1.0
    return emitted


def _route_legacy_external_input_net(
    net: MutableJsonMap,
    def_data: MutableJsonMap,
    placement: PlacementResult,
    inst_to_block: Mapping[str, TileCoord],
    cfg: Def2BpConfig,
) -> list[EntityDict]:
    conns = [_as_list(c) for c in _as_list(net.get("connections"))]
    has_pin = any(len(c) >= 2 and _as_str(c[0]) == "PIN" for c in conns)
    sink_insts = [_as_str(c[0]) for c in conns if len(c) >= 2 and _as_str(c[0]) in inst_to_block]
    if not has_pin or not sink_insts or inst_to_block[sink_insts[0]][0] <= 0:
        return []
    sink_ent = placement.inst_to_entity.get(sink_insts[0])
    if not sink_ent:
        return []

    target_block = inst_to_block[sink_insts[0]]
    net["wires"] = []
    net["vias"] = []
    ch_x = target_block[0] * float(cfg.block_pitch) - CHANNEL_IN_OFFSET
    dbu = _as_float(def_data.get("units"), cfg.default_units)
    _relocate_legacy_input_pin(def_data, _as_str(net.get("name")), ch_x, dbu)

    sx, sy = _entity_xy(sink_ent)
    is_spl = sink_ent.get("name") == "splitter"
    target_x = sx - 1.0 if is_spl else sx - TILE_HALF
    target_y = sy - TILE_HALF if is_spl else sy
    return _emit_legacy_l_channel_belts(placement, ch_x, target_x, target_y)


def _classify_interblock_net(
    net_obj: MutableJsonMap,
    inst_to_block: Mapping[str, TileCoord],
    inst_to_cell: Mapping[str, str],
    cell_table: JsonMap,
) -> InterBlockRoute | None:
    conns = [_as_list(c) for c in _as_list(net_obj.get("connections"))]
    blocks = {
        inst_to_block[_as_str(c[0])]
        for c in conns
        if len(c) >= 2 and _as_str(c[0]) in inst_to_block
    }
    if len(blocks) <= 1:
        return None
    net_obj["wires"] = []
    net_obj["vias"] = []
    driver_inst: str | None = None
    sink_insts: list[str] = []
    for c in conns:
        if len(c) < 2:
            continue
        iname, pin = _as_str(c[0]), _as_str(c[1])
        cell_meta = _as_map(cell_table.get(inst_to_cell.get(iname, "")))
        outs = [_as_str(o) for o in _as_list(cell_meta.get("outputs"))]
        clean_outs = {o.replace("_", "-") for o in outs} | set(outs)
        if pin in clean_outs or pin.replace("_", "-") in clean_outs or pin in DRIVER_PIN_NAMES:
            driver_inst = iname
        else:
            sink_insts.append(iname)
    if not (driver_inst and sink_insts):
        return None
    nname = _as_str(net_obj.get("name"))
    return InterBlockRoute(
        net_name=nname,
        source_block=inst_to_block[driver_inst],
        target_blocks=[inst_to_block[si] for si in sink_insts if si in inst_to_block],
        source_instance=driver_inst,
        target_instances=sink_insts,
    )


def _plan_legacy_macro_floorplan(
    def_data: MutableJsonMap,
    netlist_data: JsonMap,
    placement: PlacementResult,
    chassis: CityBlockChassis,
    grid_w: int,
    grid_h: int,
    cfg: Def2BpConfig,
) -> tuple[object, list[EntityDict], list[EntityDict]]:
    macro_planner = MacroFloorplanner(chassis=chassis)
    floorplan = macro_planner.plan(dict(netlist_data), grid_w=grid_w, grid_h=grid_h)
    floorplan.routes.clear()
    for ch in floorplan.channels.values():
        ch.clear_allocations()
    for inter in floorplan.intersections.values():
        inter.clear()

    pitch = float(cfg.block_pitch)
    inst_to_block: dict[str, TileCoord] = {}
    for iname, ent in placement.inst_to_entity.items():
        ex, ey = _entity_xy(ent)
        blk = (int(ex // pitch), int(ey // pitch))
        inst_to_block[iname] = blk
        floorplan.instance_to_block[iname] = blk

    inst_to_cell = {
        _as_str(_as_map(i).get("name")): _as_str(_as_map(i).get("cell"))
        for i in _as_list(netlist_data.get("instances"))
    }
    cell_table = _as_map(netlist_data.get("cells"))
    input_pin_nets = {
        _as_str(_as_map(p).get("net"), _as_str(_as_map(p).get("name")))
        for p in _as_list(def_data.get("pins"))
        if _as_map(p).get("direction") == "INPUT"
    }
    input_channel_entities: list[EntityDict] = []

    for net_obj in _as_list(def_data.get("nets")):
        if not isinstance(net_obj, dict):
            continue
        ib_route = _classify_interblock_net(net_obj, inst_to_block, inst_to_cell, cell_table)
        if ib_route is not None:
            floorplan.routes[ib_route.net_name] = ib_route
        if _as_str(net_obj.get("name")) in input_pin_nets:
            input_channel_entities.extend(
                _route_legacy_external_input_net(net_obj, def_data, placement, inst_to_block, cfg)
            )

    fabric_entities = floorplan.emit_fabric_entities(placement=placement)
    for fe in fabric_entities:
        placement.entities.append(fe)
        fx, fy = _entity_xy(fe)
        placement.occupied_positions.add((round(fx, COORD_PRECISION), round(fy, COORD_PRECISION)))
    return floorplan, fabric_entities, input_channel_entities


def _mark_entity_occupied_tiles(
    ex: float,
    ey: float,
    w: float,
    h: float,
    occupied_tiles: set[GridCoord],
) -> None:
    half_w, half_h = w / HALF_DIVISOR, h / HALF_DIVISOR
    min_tx = math.floor(ex - half_w - BOX_EPSILON)
    max_tx = math.floor(ex + half_w - BOX_EPSILON)
    min_ty = math.floor(ey - half_h - BOX_EPSILON)
    max_ty = math.floor(ey + half_h - BOX_EPSILON)
    for tx in range(min_tx, max_tx + 2):
        for ty in range(min_ty, max_ty + 2):
            overlaps_x = max(ex - half_w, tx) < min(ex + half_w, tx + 1.0)
            overlaps_y = max(ey - half_h, ty) < min(ey + half_h, ty + 1.0)
            if overlaps_x and overlaps_y:
                occupied_tiles.add(
                    (round(tx + TILE_HALF, COORD_PRECISION), round(ty + TILE_HALF, COORD_PRECISION))
                )
    occupied_tiles.add((round(ex, COORD_PRECISION), round(ey, COORD_PRECISION)))


def _add_pole_to_lamp_buckets(
    ename: str,
    ex: float,
    ey: float,
    lamp_pole_buckets: dict[TileCoord, list[tuple[float, float, float]]],
    cfg: Def2BpConfig,
) -> None:
    if ename not in ("big-electric-pole", "medium-electric-pole"):
        return
    is_big = ename == "big-electric-pole"
    r = cfg.big_pole_supply_radius if is_big else cfg.med_pole_supply_radius
    bkey = (int(ex // LAMP_BUCKET_STEP), int(ey // LAMP_BUCKET_STEP))
    lamp_pole_buckets.setdefault(bkey, []).append((ex, ey, r))


def _collect_occupied_tiles_and_pole_buckets(
    entities: Sequence[EntityDict],
    cfg: Def2BpConfig,
) -> tuple[set[GridCoord], dict[TileCoord, list[tuple[float, float, float]]]]:
    occupied_tiles: set[GridCoord] = set()
    lamp_pole_buckets: dict[TileCoord, list[tuple[float, float, float]]] = {}
    for e in entities:
        if "position" not in e:
            continue
        ex, ey = _entity_xy(e)
        ename = _as_str(e.get("name"))
        w, h = entity_size(ename, _as_int(e.get("direction"), DIRECTION_NORTH))
        _mark_entity_occupied_tiles(ex, ey, w, h, occupied_tiles)
        _add_pole_to_lamp_buckets(ename, ex, ey, lamp_pole_buckets, cfg)
    return occupied_tiles, lamp_pole_buckets


def _is_lamp_near_pole(
    bx: float,
    by: float,
    lamp_pole_buckets: Mapping[TileCoord, Sequence[tuple[float, float, float]]],
) -> bool:
    bk_x, bk_y = int(bx // LAMP_BUCKET_STEP), int(by // LAMP_BUCKET_STEP)
    return any(
        abs(px - bx) <= r and abs(py - by) <= r
        for dx_b in NEIGHBOR_BUCKET_DELTAS
        for dy_b in NEIGHBOR_BUCKET_DELTAS
        for px, py, r in lamp_pole_buckets.get((bk_x + dx_b, bk_y + dy_b), ())
    )


def _merge_chassis_base_entities(
    entities: list[EntityDict],
    chassis_base_entities: Sequence[EntityDict],
    use_maze_router: bool,
    cfg: Def2BpConfig,
) -> tuple[list[EntityDict], set[GridCoord]]:
    if not use_maze_router:
        entities = jumper_belts_across_chassis_poles(entities, chassis_base_entities, cfg=cfg)
    occupied_tiles, lamp_pole_buckets = _collect_occupied_tiles_and_pole_buckets(entities, cfg)
    big_first = [e for e in chassis_base_entities if e.get("name") == "big-electric-pole"]
    other_base = [e for e in chassis_base_entities if e.get("name") != "big-electric-pole"]
    for be in [*big_first, *other_base]:
        if "position" not in be:
            continue
        bx, by = _entity_xy(be)
        bname = _as_str(be.get("name"))
        b_tiles = _chassis_entity_footprint(bname, bx, by)
        if any(t in occupied_tiles for t in b_tiles):
            continue
        if bname == "small-lamp" and not _is_lamp_near_pole(bx, by, lamp_pole_buckets):
            continue
        occupied_tiles.update(b_tiles)
        new_be = {
            k: v for k, v in be.items() if k not in ("neighbours", "connections", "entity_number")
        }
        entities.append(new_be)
        _add_pole_to_lamp_buckets(bname, bx, by, lamp_pole_buckets, cfg)
    return entities, occupied_tiles


def _try_wire_big_pole_pair(
    bp1: EntityDict,
    bp2: EntityDict,
    wires: list[list[int]],
    seen_pairs: set[TileCoord],
    max_big_dist: float,
) -> None:
    x1, y1 = _entity_xy(bp1)
    x2, y2 = _entity_xy(bp2)
    dx, dy = abs(x1 - x2), abs(y1 - y2)
    is_aligned = dx < POLE_ALIGN_TOLERANCE or dy < POLE_ALIGN_TOLERANCE
    if math.hypot(dx, dy) > max_big_dist or not is_aligned:
        return
    p1_num, p2_num = _as_int(bp1.get("entity_number")), _as_int(bp2.get("entity_number"))
    pair = (min(p1_num, p2_num), max(p1_num, p2_num))
    if pair in seen_pairs:
        return
    seen_pairs.add(pair)
    wires.extend(
        [[pair[0], wid, pair[1], wid] for wid in (WIRE_COPPER_ID, WIRE_RED_ID, WIRE_GREEN_ID)]
    )


def _connect_aligned_big_poles(
    big_poles: Sequence[EntityDict],
    wires: list[list[int]],
    seen_pairs: set[TileCoord],
    max_big_dist: float,
) -> None:
    for i, bp1 in enumerate(big_poles):
        for bp2 in big_poles[i + 1 :]:
            _try_wire_big_pole_pair(bp1, bp2, wires, seen_pairs, max_big_dist)


def _wire_chassis_big_and_medium_poles(
    entities: Sequence[EntityDict],
    wires: list[list[int]],
    cfg: Def2BpConfig,
) -> tuple[list[EntityDict], list[EntityDict]]:
    big_poles = [e for e in entities if e.get("name") == "big-electric-pole"]
    med_poles = [e for e in entities if e.get("name") == "medium-electric-pole"]
    seen_pairs: set[TileCoord] = set()
    _connect_aligned_big_poles(
        big_poles, wires, seen_pairs, cfg.big_pole_wire_reach + POLE_REACH_SLACK
    )
    if not big_poles:
        return big_poles, med_poles

    for mp in med_poles:
        mp_num = _as_int(mp.get("entity_number"))
        mx, my = _entity_xy(mp)
        best_bp = min(
            big_poles, key=lambda bp: math.hypot(mx - _entity_xy(bp)[0], my - _entity_xy(bp)[1])
        )
        bx, by = _entity_xy(best_bp)
        if math.hypot(mx - bx, my - by) <= cfg.med_pole_wire_reach:
            bp_num = _as_int(best_bp.get("entity_number"))
            pair = (min(mp_num, bp_num), max(mp_num, bp_num))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                wires.append([pair[0], WIRE_COPPER_ID, pair[1], WIRE_COPPER_ID])
    return big_poles, med_poles


def _bfs_pole_component(
    start_id: int,
    all_pole_by_num: Mapping[int, EntityDict],
    copper_adj: Mapping[int, set[int]],
    visited: set[int],
) -> set[int]:
    comp_ids = {start_id}
    visited.add(start_id)
    queue = [start_id]
    while queue:
        for nxt_id in copper_adj.get(queue.pop(), ()):
            if nxt_id in all_pole_by_num and nxt_id not in visited:
                visited.add(nxt_id)
                comp_ids.add(nxt_id)
                queue.append(nxt_id)
    return comp_ids


def _find_largest_pole_component(
    all_pole_by_num: Mapping[int, EntityDict],
    copper_adj: Mapping[int, set[int]],
) -> set[int]:
    visited: set[int] = set()
    largest: set[int] = set()
    for start_id in all_pole_by_num:
        if start_id not in visited:
            comp_ids = _bfs_pole_component(start_id, all_pole_by_num, copper_adj, visited)
            if len(comp_ids) > len(largest):
                largest = comp_ids
    return largest


def _step_bridge_pole_toward(
    cur_x: float,
    cur_y: float,
    ux: float,
    uy: float,
    occupied_tiles: set[GridCoord],
    med_reach: float,
) -> GridCoord | None:
    base_ix, base_iy = math.floor(cur_x), math.floor(cur_y)
    reach_int = int(med_reach)
    candidates = [
        (
            round(base_ix + dx + TILE_HALF, COORD_PRECISION),
            round(base_iy + dy + TILE_HALF, COORD_PRECISION),
        )
        for dx in range(-reach_int, reach_int + 1)
        for dy in range(-reach_int, reach_int + 1)
    ]
    valid = [
        pt
        for pt in candidates
        if pt not in occupied_tiles and 1.0 <= math.hypot(pt[0] - cur_x, pt[1] - cur_y) <= med_reach
    ]
    if not valid:
        return None
    return min(valid, key=lambda pt: math.hypot(ux - pt[0], uy - pt[1]))


def _build_copper_adjacency(wires: Sequence[Sequence[int]]) -> dict[int, set[int]]:
    copper_adj: dict[int, set[int]] = {}
    for w_edge in wires:
        if (
            len(w_edge) >= WIRE_EDGE_LEN
            and int(w_edge[WIRE_PORT_A_IDX]) == WIRE_COPPER_ID
            and int(w_edge[WIRE_PORT_B_IDX]) == WIRE_COPPER_ID
        ):
            u_id, v_id = int(w_edge[0]), int(w_edge[WIRE_NODE_B_IDX])
            copper_adj.setdefault(u_id, set()).add(v_id)
            copper_adj.setdefault(v_id, set()).add(u_id)
    return copper_adj


def _reserve_pin_guard_tiles(routes: RouteResult, occupied_tiles: set[GridCoord]) -> None:
    for px, py in routes.pin_tiles | routes.valid_input_pins | routes.valid_output_pins:
        p_pt = (
            round(math.floor(px) + TILE_HALF, COORD_PRECISION),
            round(math.floor(py) + TILE_HALF, COORD_PRECISION),
        )
        occupied_tiles.add(p_pt)
        for k in PIN_GUARD_OFFSETS:
            occupied_tiles.add((round(p_pt[0] - k, COORD_PRECISION), p_pt[1]))
            occupied_tiles.add((round(p_pt[0] + k, COORD_PRECISION), p_pt[1]))
            occupied_tiles.add((p_pt[0], round(p_pt[1] - k, COORD_PRECISION)))
            occupied_tiles.add((p_pt[0], round(p_pt[1] + k, COORD_PRECISION)))


def _place_intermediate_bridge_poles(
    u_p: EntityDict,
    v_p: EntityDict,
    v_num: int,
    entities: list[EntityDict],
    wires: list[list[int]],
    all_pole_by_num: dict[int, EntityDict],
    reached: set[int],
    occupied_tiles: set[GridCoord],
    cfg: Def2BpConfig,
) -> int:
    ux, uy = _entity_xy(u_p)
    cur_x, cur_y = _entity_xy(v_p)
    prev_num = v_num
    both_big = v_p.get("name") == "big-electric-pole" and u_p.get("name") == "big-electric-pole"
    cur_reach = cfg.big_pole_wire_reach if both_big else cfg.med_pole_wire_reach
    while math.hypot(ux - cur_x, uy - cur_y) > cur_reach:
        best_cand = _step_bridge_pole_toward(
            cur_x, cur_y, ux, uy, occupied_tiles, cfg.med_pole_wire_reach
        )
        if best_cand is None:
            break
        occupied_tiles.add(best_cand)
        new_num = len(entities) + 1
        bridge_pole: EntityDict = {
            "name": "medium-electric-pole",
            "position": {"x": best_cand[0], "y": best_cand[1]},
            "entity_number": new_num,
        }
        entities.append(bridge_pole)
        all_pole_by_num[new_num] = bridge_pole
        reached.add(new_num)
        wires.append(
            [min(prev_num, new_num), WIRE_COPPER_ID, max(prev_num, new_num), WIRE_COPPER_ID]
        )
        prev_num = new_num
        cur_x, cur_y = best_cand
        cur_reach = cfg.med_pole_wire_reach
    return prev_num


def _bridge_disconnected_chassis_poles(
    entities: list[EntityDict],
    wires: list[list[int]],
    big_poles: Sequence[EntityDict],
    med_poles: Sequence[EntityDict],
    routes: RouteResult,
    occupied_tiles: set[GridCoord],
    cfg: Def2BpConfig,
) -> None:
    if not big_poles and not med_poles:
        return
    copper_adj = _build_copper_adjacency(wires)
    all_pole_by_num = {_as_int(p.get("entity_number")): p for p in [*big_poles, *med_poles]}
    reached = _find_largest_pole_component(all_pole_by_num, copper_adj)
    _reserve_pin_guard_tiles(routes, occupied_tiles)

    unreached_nums = set(all_pole_by_num.keys()) - reached
    while unreached_nums and reached:
        u_num, v_num = min(
            ((u, v) for u in unreached_nums for v in reached),
            key=lambda pair: math.hypot(
                _entity_xy(all_pole_by_num[pair[0]])[0] - _entity_xy(all_pole_by_num[pair[1]])[0],
                _entity_xy(all_pole_by_num[pair[0]])[1] - _entity_xy(all_pole_by_num[pair[1]])[1],
            ),
        )
        prev_num = _place_intermediate_bridge_poles(
            all_pole_by_num[u_num],
            all_pole_by_num[v_num],
            v_num,
            entities,
            wires,
            all_pole_by_num,
            reached,
            occupied_tiles,
            cfg,
        )
        wires.append([min(prev_num, u_num), WIRE_COPPER_ID, max(prev_num, u_num), WIRE_COPPER_ID])
        copper_adj.setdefault(prev_num, set()).add(u_num)
        copper_adj.setdefault(u_num, set()).add(prev_num)
        _bfs_pole_component(u_num, all_pole_by_num, copper_adj, reached)
        unreached_nums -= reached


def _net_fluid_commodity(net_name: str, fluids: set[str]) -> str | None:
    c = net_name.split("__")[0]
    if c in fluids:
        return c
    c_dash = c.replace("_", "-")
    return c_dash if c_dash in fluids else None


def _run_drc_and_verify(
    entities: list[EntityDict],
    wires: list[list[int]],
    routes: RouteResult,
    placement: PlacementResult,
    def_data: JsonMap,
    netlist_data: JsonMap,
    scale: float,
    strict_drc: bool,
) -> None:
    remaining_conflicts = [
        pos
        for pos, nets in routes.tile_to_nets.items()
        if (
            len(nets) > 1
            and not (
                all(_net_fluid_commodity(n, placement.fluids) is not None for n in nets)
                and len({_net_fluid_commodity(n, placement.fluids) for n in nets}) == 1
            )
        )
        or pos in routes.adjacent_fluid_conflicts
    ]
    pins = [_as_map(p) for p in _as_list(def_data.get("pins"))]
    drc_in_pins = [
        (_as_float(p.get("x")) * scale, _as_float(p.get("y")) * scale)
        for p in pins
        if p.get("direction") == "INPUT"
    ]
    drc_out_pins = [
        (_as_float(p.get("x")) * scale, _as_float(p.get("y")) * scale)
        for p in pins
        if p.get("direction") == "OUTPUT"
    ]
    drc = BeltDrcChecker()
    violations = drc.check_entities(
        entities,
        input_pins=drc_in_pins,
        output_pins=drc_out_pins,
        unresolved_crossings=remaining_conflicts,
        wires=wires,
        tile_to_nets=routes.tile_to_nets,
        fluids=placement.fluids,
    )
    if def_data.get("_macro_floorplan") is not None:
        raw_hwy = _as_list(def_data.get("_highway_specs"))
        hwy_specs = [s for s in raw_hwy if isinstance(s, RouterNetSpec)]
        raw_bays = _as_list(def_data.get("_bay_blocks"))
        bay_set: set[TileCoord] = {
            (_as_int(b[0]), _as_int(b[1])) for b in raw_bays if isinstance(b, tuple) and len(b) == 2
        }
        violations.extend(
            drc.check_chassis_matrix_discipline(
                entities=entities,
                macro_floorplan=def_data["_macro_floorplan"],
                router_routes=getattr(placement, "router_routes", None),
                highway_specs=hwy_specs,
                def_data=dict(def_data),
                netlist_data=dict(netlist_data),
                bay_blocks=bay_set,
            )
        )
    for v in violations:
        sys.stderr.write(f"DRC {v.severity}: [{v.code}] at ({v.x}, {v.y}): {v.message}\n")
    if not strict_drc:
        return

    fatal = [v for v in violations if v.severity == "ERROR"]
    if fatal:
        raise ValueError(f"Physical DRC failed with {len(fatal)} violation(s)")

    router_routes = getattr(placement, "router_routes", None) or {}
    _check_and_raise_failed_routes(router_routes, strict_drc=True, prefix="Router")

    verif_res = getattr(placement, "router_verif_res", None)
    if verif_res is not None and not getattr(verif_res, "is_valid", True):
        lvs_cnt = len(getattr(verif_res, "lvs_errors", ()))
        drc_cnt = len(getattr(verif_res, "drc_violations", ()))
        raise ValueError(
            f"Native router verification failed with {lvs_cnt} LVS error(s) and {drc_cnt} DRC violation(s)"
        )


def _setup_blockage_chassis(
    def_data: MutableJsonMap,
    netlist_data: JsonMap,
    placement: PlacementResult,
    use_maze_router: bool,
    units: float,
    scale: float,
    cfg: Def2BpConfig,
) -> tuple[object | None, list[EntityDict], list[EntityDict], list[EntityDict]]:
    die_area = _parse_die_area(def_data, units, cfg)
    die_w, die_h = die_area[DIE_X_MAX_IDX] / units, die_area[DIE_Y_MAX_IDX] / units
    grid_w = max(1, round(die_w / float(cfg.block_pitch)))
    grid_h = max(1, round(die_h / float(cfg.block_pitch)))
    chassis = _select_cityblock_chassis(grid_w, grid_h, cfg)
    base_bp = _as_map(chassis.emit_base_blueprint(grid_w, grid_h).get("blueprint"))
    chassis_base_entities = [dict(_as_map(e)) for e in _as_list(base_bp.get("entities"))]
    def_data["_chassis_tiles"] = _as_list(base_bp.get("tiles"))
    if use_maze_router:
        _register_chassis_router_obstacles(
            def_data, placement, chassis_base_entities, grid_w, grid_h, die_w, die_h, scale, cfg
        )
        return None, [], [], chassis_base_entities
    floorplan, fabric_entities, input_channel_entities = _plan_legacy_macro_floorplan(
        def_data, netlist_data, placement, chassis, grid_w, grid_h, cfg
    )
    return floorplan, fabric_entities, input_channel_entities, chassis_base_entities


def _assert_physical_integrity(entities: list[EntityDict]) -> None:
    collision_pairs = check_collisions(entities)
    if collision_pairs:
        raise RuntimeError(
            f"Collision check failed: {len(collision_pairs)} overlapping entity pairs detected: {collision_pairs[:10]}"
        )
    unpaired_hoods = check_underground_pairing(entities)
    if unpaired_hoods:
        raise RuntimeError(
            f"Underground check failed: {len(unpaired_hoods)} unpaired underground hoods detected: {unpaired_hoods}"
        )


def build_entities(
    def_data: MutableJsonMap,
    netlist_data: JsonMap,
    strict_drc: bool = True,
    use_maze_router: bool = True,
    engine: object | None = None,
    cfg: Def2BpConfig = DEFAULT_DEF2BP_CONFIG,
) -> tuple[list[EntityDict], list[list[int]]]:
    """Build Factorio entities and wire connections from DEF and netlist data."""
    def_data["_netlist_data"] = netlist_data
    untangle_def(def_data, dict(netlist_data), pins_only=use_maze_router)

    units = _as_float(def_data.get("units"), cfg.default_units)
    scale = 1.0 / units
    placement = place_components(def_data, netlist_data)
    def_data["_placement"] = placement

    fabric_entities: list[EntityDict] = []
    input_channel_entities: list[EntityDict] = []
    floorplan: object | None = None
    chassis_base_entities: list[EntityDict] = []
    has_blockages = bool(def_data.get("blockages"))

    if has_blockages:
        floorplan, fabric_entities, input_channel_entities, chassis_base_entities = (
            _setup_blockage_chassis(
                def_data, netlist_data, placement, use_maze_router, units, scale, cfg
            )
        )

    routes = route_with_maze_router(
        def_data, netlist_data, placement, engine=engine, strict_drc=strict_drc, cfg=cfg
    )
    if floorplan is not None:
        for ent in [*fabric_entities, *input_channel_entities]:
            ex, ey = _entity_xy(ent)
            routes.tile_to_nets.setdefault(
                (round(ex, COORD_PRECISION), round(ey, COORD_PRECISION)), set()
            )

    insert_pipeline_pumps(placement, routes, def_data, netlist_data)
    pole_entities, mst_connections = generate_pdn(
        placement.entities,
        placement.occupied_positions,
        routes.pin_tiles,
        routes.valid_input_pins,
        routes.valid_output_pins,
    )
    _assert_physical_integrity(placement.entities)

    if use_maze_router:
        entities = list(placement.entities)
    else:
        entities = prune_dead_end_belts(placement.entities, dict(def_data), dict(netlist_data))
    occupied_tiles: set[GridCoord] = set()
    if has_blockages:
        entities, occupied_tiles = _merge_chassis_base_entities(
            entities, chassis_base_entities, use_maze_router, cfg
        )

    grid_entities = {
        (round(_entity_xy(e)[0], COORD_PRECISION), round(_entity_xy(e)[1], COORD_PRECISION)): e
        for e in entities
    }
    wires = (
        wire_telemetry(
            entities,
            dict(netlist_data),
            placement.inst_to_entity,
            routes.net_surface_tiles,
            grid_entities,
            mst_connections,
            pole_entities,
        )
        or []
    )
    if has_blockages:
        big_poles, med_poles = _wire_chassis_big_and_medium_poles(entities, wires, cfg)
        _bridge_disconnected_chassis_poles(
            entities, wires, big_poles, med_poles, routes, occupied_tiles, cfg
        )

    _run_drc_and_verify(
        entities, wires, routes, placement, def_data, netlist_data, scale, strict_drc
    )
    return entities, wires


def _convert_chained_south_pole_jumper(
    ent_map: dict[GridCoord, EntityDict],
    to_remove: set[GridCoord],
    e_in: EntityDict,
    x: float,
    y_lo: float,
    y_hi: float,
    out_pos: GridCoord,
    cfg: Def2BpConfig,
) -> bool:
    for dist in range(1, cfg.max_underground_span + 1):
        cand = ent_map.get((x, round(out_pos[1] + dist, COORD_PRECISION)))
        if (
            cand
            and cand.get("name") == "underground-belt"
            and cand.get("type") == "output"
            and cand.get("direction") == DIRECTION_SOUTH
        ):
            span_req = dist + JUMPER_SPAN_PADDING
            preferred = (
                "fast-underground-belt"
                if span_req <= cfg.mid_underground_span
                else "express-underground-belt"
            )
            tier = get_compatible_underground_belt(span_req, preferred_name=preferred)
            e_in["name"] = tier
            e_in["type"] = "input"
            cand["name"] = tier
            cand["type"] = "output"
            for r_pos in ((x, y_lo), (x, y_hi), out_pos):
                to_remove.add(r_pos)
                ent_map.pop(r_pos, None)
            return True
    return False


def _try_convert_pole_jumper_line(
    ent_map: dict[GridCoord, EntityDict],
    to_remove: set[GridCoord],
    lo_pos: GridCoord,
    hi_pos: GridCoord,
    in_pos: GridCoord,
    out_pos: GridCoord,
    expected_dir: int,
    allow_chained_upgrade: bool,
    cfg: Def2BpConfig,
) -> None:
    e_lo, e_hi = ent_map.get(lo_pos), ent_map.get(hi_pos)
    if not (
        e_lo
        and e_hi
        and e_lo.get("name") == "transport-belt"
        and e_hi.get("name") == "transport-belt"
        and e_lo.get("direction") == expected_dir
        and e_hi.get("direction") == expected_dir
    ):
        return
    e_in, e_out = ent_map.get(in_pos), ent_map.get(out_pos)
    if not (e_in and e_in.get("direction") == expected_dir and e_out):
        return
    if (
        allow_chained_upgrade
        and e_out.get("name") == "underground-belt"
        and e_out.get("type") == "input"
    ):
        _convert_chained_south_pole_jumper(
            ent_map, to_remove, e_in, lo_pos[0], lo_pos[1], hi_pos[1], out_pos, cfg
        )
    elif e_out.get("direction") == expected_dir:
        e_in["name"] = "underground-belt"
        e_in["type"] = "input"
        e_out["name"] = "underground-belt"
        e_out["type"] = "output"
        for r_pos in (lo_pos, hi_pos):
            to_remove.add(r_pos)
            ent_map.pop(r_pos, None)


def jumper_belts_across_chassis_poles(
    entities: Sequence[EntityDict],
    base_entities: Sequence[EntityDict],
    cfg: Def2BpConfig = DEFAULT_DEF2BP_CONFIG,
) -> list[EntityDict]:
    """Convert straight transport belts that collide with chassis big electric poles into underground jumpers."""
    ent_map: dict[GridCoord, EntityDict] = {
        (round(_entity_xy(e)[0], COORD_PRECISION), round(_entity_xy(e)[1], COORD_PRECISION)): e
        for e in entities
        if "position" in e
    }
    to_remove: set[GridCoord] = set()

    for be in base_entities:
        if be.get("name") != "big-electric-pole":
            continue
        bx, by = _entity_xy(be)
        for dx in BIG_POLE_DELTAS:
            x = round(bx + dx, COORD_PRECISION)
            lo_v = (x, round(by - TILE_HALF, COORD_PRECISION))
            hi_v = (x, round(by + TILE_HALF, COORD_PRECISION))
            s_in = (x, round(by - CHANNEL_IN_OFFSET, COORD_PRECISION))
            s_out = (x, round(by + CHANNEL_IN_OFFSET, COORD_PRECISION))
            _try_convert_pole_jumper_line(
                ent_map, to_remove, lo_v, hi_v, s_in, s_out, DIRECTION_SOUTH, True, cfg
            )
            _try_convert_pole_jumper_line(
                ent_map, to_remove, lo_v, hi_v, s_out, s_in, DIRECTION_NORTH, False, cfg
            )
        for dy in BIG_POLE_DELTAS:
            y = round(by + dy, COORD_PRECISION)
            lo_h = (round(bx - TILE_HALF, COORD_PRECISION), y)
            hi_h = (round(bx + TILE_HALF, COORD_PRECISION), y)
            e_in = (round(bx - CHANNEL_IN_OFFSET, COORD_PRECISION), y)
            e_out = (round(bx + CHANNEL_IN_OFFSET, COORD_PRECISION), y)
            _try_convert_pole_jumper_line(
                ent_map, to_remove, lo_h, hi_h, e_in, e_out, DIRECTION_EAST, False, cfg
            )
            _try_convert_pole_jumper_line(
                ent_map, to_remove, lo_h, hi_h, e_out, e_in, DIRECTION_WEST, False, cfg
            )

    if not to_remove:
        return list(entities)
    return [
        e
        for e in entities
        if (round(_entity_xy(e)[0], COORD_PRECISION), round(_entity_xy(e)[1], COORD_PRECISION))
        not in to_remove
    ]


def def_to_blueprint(
    def_text: str,
    netlist_data: JsonMap,
    label: str = "factory",
    strict_drc: bool = True,
    use_maze_router: bool = True,
) -> str:
    """Convert DEF text and netlist metadata into a blueprint exchange string."""
    def_data = parse_def(def_text)
    entities, wires = build_entities(
        def_data,
        netlist_data,
        strict_drc=strict_drc,
        use_maze_router=use_maze_router,
    )
    tiles = def_data.get("_chassis_tiles")
    tile_list = [dict(_as_map(t)) for t in _as_list(tiles)] if tiles else None
    return blueprint.encode(entities, label, wires=wires, tiles=tile_list)


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert placed and routed DEF to Factorio blueprint string."
    )
    parser.add_argument("def_file", type=Path, help="Path to input DEF file")
    parser.add_argument("netlist_file", type=Path, help="Path to input netlist JSON file")
    parser.add_argument(
        "-o", "--out", type=Path, default=None, help="Path to write blueprint output"
    )
    parser.add_argument(
        "--pins-out", type=Path, default=None, help="Path to write extracted pins JSON"
    )
    parser.add_argument("--label", type=str, default="factory", help="Blueprint label text")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw blueprint JSON document instead of exchange string",
    )
    parser.add_argument(
        "--time-out", type=Path, default=None, help="Path to write execution time JSON"
    )
    parser.add_argument(
        "--no-strict-drc", action="store_true", help="Do not abort on physical DRC violations"
    )
    parser.add_argument(
        "--use-maze-router",
        action="store_true",
        default=True,
        help="Route nets using 3D multi-layer DualLaneMazeRouter (default: True, Plan of Record)",
    )
    parser.add_argument(
        "--no-maze-router",
        action="store_false",
        dest="use_maze_router",
        help="Disable maze router",
    )
    return parser.parse_args()


def _format_blueprint_output(
    entities: list[EntityDict],
    wires: list[list[int]],
    tiles: list[EntityDict] | None,
    label: str,
    as_json: bool,
) -> str:
    if not as_json:
        return blueprint.encode(entities, label, wires=wires, tiles=tiles)
    bp_body: MutableJsonMap = {
        "item": "blueprint",
        "label": label,
        "version": blueprint.VERSION_2_0,
        "entities": entities,
    }
    if wires:
        bp_body["wires"] = wires
    if tiles:
        bp_body["tiles"] = tiles
    return json.dumps({"blueprint": bp_body}, indent=2)


def main() -> int:
    """Main CLI entrypoint."""
    args = _parse_cli_args()
    t0 = time.perf_counter()
    def_data = parse_def(args.def_file.read_text())
    netlist_data = json.loads(args.netlist_file.read_text())

    entities, wires = build_entities(
        def_data,
        netlist_data,
        strict_drc=not args.no_strict_drc,
        use_maze_router=args.use_maze_router,
    )
    raw_tiles = def_data.get("_chassis_tiles")
    tiles = [dict(_as_map(t)) for t in _as_list(raw_tiles)] if raw_tiles else None
    output_text = _format_blueprint_output(entities, wires, tiles, args.label, args.json)

    if args.out:
        args.out.write_text(output_text + "\n")
    else:
        sys.stdout.write(output_text + "\n")

    if args.pins_out:
        pins_data = extract_pins(def_data, netlist_data)
        args.pins_out.parent.mkdir(parents=True, exist_ok=True)
        args.pins_out.write_text(json.dumps(pins_data, indent=2) + "\n")

    if args.time_out:
        elapsed = round(time.perf_counter() - t0, TIME_PRECISION)
        args.time_out.parent.mkdir(parents=True, exist_ok=True)
        args.time_out.write_text(
            json.dumps({"stage": "bp", "elapsed_seconds": elapsed}, indent=2) + "\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
