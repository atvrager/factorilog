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
from pathlib import Path
from typing import Any

try:
    from foundry import blueprint
    from foundry.belt_drc import BeltDrcChecker
    from foundry.circuit import wire_telemetry
    from foundry.def_parser import extract_pins, merge_collinear_wires, parse_def
    from foundry.def_untangle import untangle_def
    from foundry.entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        check_collisions,
        check_power,
        check_underground_pairing,
        entity_collision_half_size,
        entity_size,
        prune_dead_end_belts,
    )
    from foundry.jumper import RouteResult, route_and_resolve_jumpers
    from foundry.pdn import generate_pdn
    from foundry.placement import place_components
    from foundry.pump_insertion import insert_pipeline_pumps
except ImportError:
    import blueprint
    from belt_drc import BeltDrcChecker
    from circuit import wire_telemetry
    from def_parser import extract_pins, merge_collinear_wires, parse_def
    from def_untangle import untangle_def
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        check_collisions,
        check_power,
        check_underground_pairing,
        entity_collision_half_size,
        entity_size,
        prune_dead_end_belts,
    )
    from jumper import RouteResult, route_and_resolve_jumpers
    from pdn import generate_pdn
    from placement import place_components
    from pump_insertion import insert_pipeline_pumps

__all__ = [
    "DIRECTION_EAST",
    "DIRECTION_NORTH",
    "DIRECTION_SOUTH",
    "DIRECTION_WEST",
    "build_entities",
    "check_collisions",
    "check_power",
    "check_underground_pairing",
    "def_to_blueprint",
    "entity_collision_half_size",
    "entity_size",
    "extract_pins",
    "main",
    "merge_collinear_wires",
    "parse_def",
    "prune_dead_end_belts",
    "route_with_maze_router",
    "untangle_def",
]


def route_with_maze_router(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
    placement: Any,
) -> RouteResult:
    """Route DEF nets using the 3D multi-layer DualLaneMazeRouter.

    Extract terminal coordinates for each logical net from components and pins.
    Execute the multi-layer router across surface and subterranean layers.
    Add routed physical entities directly to the placement record.
    """
    from foundry.native_engine.verifier import load_game_prototypes
    from foundry.router.maze_router import DualLaneMazeRouter
    from foundry.router.types import RouterNetSpec

    scale = 1.0 / float(def_data.get("units", 1000))
    die_area = def_data.get("die_area", [0, 0, 100000, 100000])
    min_x = math.floor(die_area[0] * scale)
    min_y = math.floor(die_area[1] * scale)
    max_x = math.ceil(die_area[2] * scale)
    max_y = math.ceil(die_area[3] * scale)

    obstacles = {(math.floor(pos[0]), math.floor(pos[1])) for pos in placement.occupied_positions}

    pin_dict: dict[str, tuple[int, int]] = {}
    valid_in_pins: set[tuple[float, float]] = set()
    valid_out_pins: set[tuple[float, float]] = set()
    for pin in def_data.get("pins", []):
        px = round(pin.get("x", 0) * scale)
        py = round(pin.get("y", 0) * scale)
        pname = pin.get("name", pin.get("net", ""))
        pin_dict[pname] = (px, py)
        direction = pin.get("direction", "")
        if direction == "INPUT":
            valid_in_pins.add((float(px), float(py)))
        elif direction == "OUTPUT":
            valid_out_pins.add((float(px), float(py)))

    fluids = set(netlist_data.get("fluids", [])) or set(placement.fluids or [])
    inst_entities = placement.inst_to_entity
    net_specs: list[RouterNetSpec] = []

    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        conns = net.get("connections", [])
        if len(conns) < 2:
            continue

        src_pos: tuple[int, int] | None = None
        dst_pos: tuple[int, int] | None = None

        for inst_name, port in conns:
            if inst_name == "PIN":
                pos = pin_dict.get(nname) or pin_dict.get(port)
                if pos:
                    if (float(pos[0]), float(pos[1])) in valid_in_pins:
                        src_pos = pos
                    else:
                        dst_pos = pos
            elif inst_name in inst_entities:
                ent = inst_entities[inst_name]
                epos = ent.get("position", {})
                ex = round(float(epos.get("x", 0.0)))
                ey = round(float(epos.get("y", 0.0)))
                if src_pos is None:
                    src_pos = (ex, ey)
                else:
                    dst_pos = (ex, ey)

        if src_pos and dst_pos and src_pos != dst_pos:
            is_fluid = any(f in nname for f in fluids)
            net_specs.append(
                RouterNetSpec(
                    name=nname,
                    source=src_pos,
                    sink=dst_pos,
                    commodity=nname.split("__")[0],
                    is_fluid=is_fluid,
                )
            )

    prototypes = load_game_prototypes()
    router = DualLaneMazeRouter(
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        prototypes=prototypes,
        obstacles=obstacles,
    )
    routes, entities, _ = router.route(net_specs, verify=False)

    for ent in entities:
        placement.entities.append(ent)
        epos = ent.get("position", {})
        pos_key = (round(float(epos.get("x", 0.0)), 2), round(float(epos.get("y", 0.0)), 2))
        placement.occupied_positions.add(pos_key)
        placement.grid_entities[pos_key] = ent

    net_surface_tiles: dict[str, dict[tuple[float, float], int]] = {}
    tile_to_nets: dict[tuple[float, float], set[str]] = {}
    for nname, route_res in routes.items():
        net_surface_tiles[nname] = {}
        for node in route_res.nodes:
            if node.z == 0:
                k = (float(node.x), float(node.y))
                net_surface_tiles[nname][k] = node.heading
                tile_to_nets.setdefault(k, set()).add(nname)

    all_pins = valid_in_pins | valid_out_pins
    return RouteResult(
        net_surface_tiles=net_surface_tiles,
        tile_to_nets=tile_to_nets,
        adjacent_fluid_conflicts=set(),
        pin_tiles=all_pins,
        valid_input_pins=valid_in_pins,
        valid_output_pins=valid_out_pins,
    )


def build_entities(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
    strict_drc: bool = True,
    use_maze_router: bool = False,
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """Build Factorio entities and wire connections from DEF and netlist data."""
    def_data["_netlist_data"] = netlist_data

    # 1. Untangle routing tracks (topological pin swaps and iterative geometric/physical loop)
    untangle_def(def_data, netlist_data)

    scale = 1.0 / float(def_data.get("units", 1000))

    # 2. Place components, machines, and inserters
    placement = place_components(def_data, netlist_data)

    fabric_entities: list[dict[str, Any]] = []
    floorplan = None
    if def_data.get("blockages"):
        try:
            from foundry.chassis import CityBlockChassis
            from foundry.macro_floorplan import InterBlockRoute, MacroFloorplanner
        except ImportError:
            from chassis import CityBlockChassis
            from macro_floorplan import InterBlockRoute, MacroFloorplanner

        units = float(def_data.get("units", 1000))
        die_w = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[2] / units
        die_h = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[3] / units
        grid_w = max(1, round(die_w / 100.0))
        grid_h = max(1, round(die_h / 100.0))

        bp_candidates = [
            Path("cityblock.bp"),
            Path(__file__).resolve().parent.parent / "cityblock.bp",
            Path("/home/atv/src/factorilog/cityblock.bp"),
        ]
        chassis: CityBlockChassis | None = None
        for bp_cand in bp_candidates:
            if bp_cand.is_file():
                try:
                    chassis = CityBlockChassis.from_blueprint_file(bp_cand)
                    break
                except Exception:
                    pass
        if chassis is None:
            chassis = CityBlockChassis(pitch=100, core_width=94, core_height=94, channel_width=6)

        macro_planner = MacroFloorplanner(chassis=chassis)
        floorplan = macro_planner.plan(netlist_data, grid_w=grid_w, grid_h=grid_h)
        floorplan.routes.clear()
        for ch in floorplan.channels.values():
            ch.clear_allocations()
        for inter in floorplan.intersections.values():
            inter.clear()

        inst_to_block: dict[str, tuple[int, int]] = {}
        for iname, ent in placement.inst_to_entity.items():
            pos = ent.get("position", {})
            bx = int(float(pos.get("x", 0.0)) // 100.0)
            by = int(float(pos.get("y", 0.0)) // 100.0)
            inst_to_block[iname] = (bx, by)
            floorplan.instance_to_block[iname] = (bx, by)

        inst_to_cell = {i["name"]: i.get("cell", "") for i in netlist_data.get("instances", [])}
        cell_table = netlist_data.get("cells", {})

        for net in def_data.get("nets", []):
            nname = net.get("name", "")
            conns = net.get("connections", [])
            blocks = {inst_to_block[iname] for iname, _ in conns if iname in inst_to_block}
            if len(blocks) > 1:
                # Inter-block net: remove flat OpenROAD routing wires
                net["wires"] = []
                net["vias"] = []

                driver_inst = None
                sink_insts = []
                for iname, pin in conns:
                    cell_id = inst_to_cell.get(iname, "")
                    cell_meta = cell_table.get(cell_id, {})
                    outs = cell_meta.get("outputs", [])
                    clean_outs = {o.replace("_", "-") for o in outs} | set(outs)
                    if (
                        pin in clean_outs
                        or pin.replace("_", "-") in clean_outs
                        or pin in ("y", "y1", "y2", "out")
                    ):
                        driver_inst = iname
                    else:
                        sink_insts.append(iname)

                if driver_inst and sink_insts:
                    s_blk = inst_to_block[driver_inst]
                    t_blks = [inst_to_block[si] for si in sink_insts if si in inst_to_block]
                    route = InterBlockRoute(
                        net_name=nname,
                        source_block=s_blk,
                        target_blocks=t_blks,
                        source_instance=driver_inst,
                        target_instances=sink_insts,
                    )
                    floorplan.routes[nname] = route

        # Route external inputs entering chassis grid
        input_channel_entities: list[dict[str, Any]] = []
        input_pin_nets = {
            pin.get("net", pin.get("name", ""))
            for pin in def_data.get("pins", [])
            if pin.get("direction") == "INPUT"
        }
        for net in def_data.get("nets", []):
            nname = net.get("name", "")
            if nname not in input_pin_nets:
                continue
            conns = net.get("connections", [])
            has_pin = any(iname == "PIN" for iname, _ in conns)
            if not has_pin:
                continue
            sink_insts = [iname for iname, _ in conns if iname in inst_to_block]
            if not sink_insts:
                continue
            target_block = inst_to_block[sink_insts[0]]
            if target_block[0] > 0:
                net["wires"] = []
                net["vias"] = []
                net_name = net.get("name", "")
                ch_x = target_block[0] * 100.0 - 1.5

                # Update pin position in def_data
                for pin in def_data.get("pins", []):
                    if pin.get("net") == net_name or pin.get("name") == net_name:
                        dbu = float(def_data.get("units", 1000))
                        pin["x"] = int(ch_x * dbu - 500)
                        pin["y"] = 0

                sink_ent = placement.inst_to_entity.get(sink_insts[0])
                if sink_ent:
                    spos = sink_ent.get("position", {})
                    sx = float(spos.get("x", 0.0))
                    sy = float(spos.get("y", 0.0))
                    target_x = sx - 1.0 if sink_ent.get("name") == "splitter" else sx - 0.5
                    target_y = sy - 0.5 if sink_ent.get("name") == "splitter" else sy

                    # Vertical channel belt from entrance to sink row
                    cur_y = 0.5
                    while cur_y < target_y:
                        pos = {"x": ch_x, "y": cur_y}
                        b_ent = {
                            "name": "transport-belt",
                            "position": pos,
                            "direction": DIRECTION_SOUTH,
                        }
                        input_channel_entities.append(b_ent)
                        placement.entities.append(b_ent)
                        placement.occupied_positions.add((ch_x, cur_y))
                        cur_y += 1.0

                    # Turn East into target block
                    cur_x = ch_x
                    while cur_x <= target_x:
                        pos = {"x": cur_x, "y": target_y}
                        b_ent = {
                            "name": "transport-belt",
                            "position": pos,
                            "direction": DIRECTION_EAST,
                        }
                        input_channel_entities.append(b_ent)
                        placement.entities.append(b_ent)
                        placement.occupied_positions.add((cur_x, target_y))
                        cur_x += 1.0

        fabric_entities = floorplan.emit_fabric_entities(placement=placement)
        for fe in fabric_entities:
            placement.entities.append(fe)
            fpos = fe.get("position", {})
            placement.occupied_positions.add(
                (round(float(fpos.get("x", 0.0)), 2), round(float(fpos.get("y", 0.0)), 2))
            )

    # 3. Rasterize routing tracks and resolve surface crossings with underground jumpers
    debug_trace: dict[tuple[float, float], list[dict[str, Any]]] = {}
    if use_maze_router:
        routes = route_with_maze_router(def_data, netlist_data, placement)
    else:
        routes = route_and_resolve_jumpers(
            def_data, netlist_data, placement, debug_trace=debug_trace
        )

    if floorplan:
        for fe in fabric_entities:
            fpos = fe.get("position", {})
            k = (round(float(fpos.get("x", 0.0)), 2), round(float(fpos.get("y", 0.0)), 2))
            routes.tile_to_nets.setdefault(k, set())
        for ie in input_channel_entities:
            ipos = ie.get("position", {})
            k = (round(float(ipos.get("x", 0.0)), 2), round(float(ipos.get("y", 0.0)), 2))
            routes.tile_to_nets.setdefault(k, set())

    # 3b. Insert inline pumps to segment overextended pipelines (<= 320 tiles)
    insert_pipeline_pumps(placement, routes, def_data, netlist_data)

    # 4. Generate power distribution network (PDN)
    pole_entities, mst_connections = generate_pdn(
        placement.entities,
        placement.occupied_positions,
        routes.pin_tiles,
        routes.valid_input_pins,
        routes.valid_output_pins,
    )

    # 5. Sanity checks
    collision_pairs = check_collisions(placement.entities)
    if collision_pairs:
        raise RuntimeError(
            f"Collision check failed: {len(collision_pairs)} overlapping entity pairs detected: {collision_pairs[:10]}"
        )

    unpaired_hoods = check_underground_pairing(placement.entities)
    if unpaired_hoods:
        raise RuntimeError(
            f"Underground check failed: {len(unpaired_hoods)} unpaired underground hoods detected: {unpaired_hoods}"
        )

    # 6. Prune dead-end stub belts
    entities = prune_dead_end_belts(placement.entities, def_data, netlist_data)
    grid_entities = {
        (round(e["position"]["x"], 2), round(e["position"]["y"], 2)): e for e in entities
    }

    # 7. Wire circuit telemetry and assign sequential entity numbers
    wires = wire_telemetry(
        entities,
        netlist_data,
        placement.inst_to_entity,
        routes.net_surface_tiles,
        grid_entities,
        mst_connections,
        pole_entities,
    )

    # 8. Physical Design Rule Check (DRC)
    remaining_conflicts = [
        pos
        for pos, nets in routes.tile_to_nets.items()
        if len(nets) > 1 or pos in routes.adjacent_fluid_conflicts
    ]
    drc_in_pins = [
        (pin["x"] * scale, pin["y"] * scale)
        for pin in def_data.get("pins", [])
        if pin.get("direction") == "INPUT"
    ]
    drc_out_pins = [
        (pin["x"] * scale, pin["y"] * scale)
        for pin in def_data.get("pins", [])
        if pin.get("direction") == "OUTPUT"
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
    if violations:
        for v in violations:
            sys.stderr.write(f"DRC {v.severity}: [{v.code}] at ({v.x}, {v.y}): {v.message}\n")
        if strict_drc:
            fatal = [v for v in violations if v.severity == "ERROR"]
            if fatal:
                raise ValueError(f"Physical DRC failed with {len(fatal)} violation(s)")

    return entities, wires


def jumper_belts_across_chassis_poles(
    entities: list[dict[str, Any]], base_entities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert straight transport belts that collide with chassis big electric poles into underground jumpers."""
    ent_map: dict[tuple[float, float], dict[str, Any]] = {
        (round(e["position"]["x"], 2), round(e["position"]["y"], 2)): e
        for e in entities
        if "position" in e
    }
    to_remove: set[tuple[float, float]] = set()

    for be in base_entities:
        if be.get("name") != "big-electric-pole":
            continue
        bpos = be.get("position", {})
        bx = float(bpos.get("x", 0.0))
        by = float(bpos.get("y", 0.0))

        # Check for vertical colliding belts (direction 8: South, or 0: North)
        for dx in (-0.5, 0.5):
            x = round(bx + dx, 2)
            y_lo = round(by - 0.5, 2)
            y_hi = round(by + 0.5, 2)
            e_lo = ent_map.get((x, y_lo))
            e_hi = ent_map.get((x, y_hi))
            if (
                e_lo
                and e_hi
                and e_lo.get("name") == "transport-belt"
                and e_hi.get("name") == "transport-belt"
                and e_lo.get("direction") == e_hi.get("direction")
            ):
                d = e_lo.get("direction", 8)
                if d == 8:  # South
                    in_pos = (x, round(by - 1.5, 2))
                    out_pos = (x, round(by + 1.5, 2))
                    e_in = ent_map.get(in_pos)
                    e_out = ent_map.get(out_pos)
                    if e_in and e_in.get("direction") == 8:
                        if (
                            e_out
                            and e_out.get("name") == "underground-belt"
                            and e_out.get("type") == "input"
                        ):
                            dist = 1
                            found_out = None
                            while dist <= 9:
                                cand_pos = (x, round(out_pos[1] + dist, 2))
                                cand = ent_map.get(cand_pos)
                                if (
                                    cand
                                    and cand.get("name") == "underground-belt"
                                    and cand.get("type") == "output"
                                    and cand.get("direction") == 8
                                ):
                                    found_out = cand
                                    break
                                dist += 1
                            if found_out:
                                tier = (
                                    "fast-underground-belt"
                                    if dist + 3 <= 7
                                    else "express-underground-belt"
                                )
                                e_in["name"] = tier
                                e_in["type"] = "input"
                                found_out["name"] = tier
                                found_out["type"] = "output"
                                to_remove.add((x, y_lo))
                                to_remove.add((x, y_hi))
                                to_remove.add(out_pos)
                                ent_map.pop((x, y_lo), None)
                                ent_map.pop((x, y_hi), None)
                                ent_map.pop(out_pos, None)
                        elif e_out and e_out.get("direction") == 8:
                            e_in["name"] = "underground-belt"
                            e_in["type"] = "input"
                            e_out["name"] = "underground-belt"
                            e_out["type"] = "output"
                            to_remove.add((x, y_lo))
                            to_remove.add((x, y_hi))
                            ent_map.pop((x, y_lo), None)
                            ent_map.pop((x, y_hi), None)
                elif d == 0:  # North
                    in_pos = (x, round(by + 1.5, 2))
                    out_pos = (x, round(by - 1.5, 2))
                    e_in = ent_map.get(in_pos)
                    e_out = ent_map.get(out_pos)
                    if (
                        e_in
                        and e_out
                        and e_in.get("direction") == 0
                        and e_out.get("direction") == 0
                    ):
                        e_in["name"] = "underground-belt"
                        e_in["type"] = "input"
                        e_out["name"] = "underground-belt"
                        e_out["type"] = "output"
                        to_remove.add((x, y_lo))
                        to_remove.add((x, y_hi))
                        ent_map.pop((x, y_lo), None)
                        ent_map.pop((x, y_hi), None)

        # Check for horizontal colliding belts (direction 4: East, or 12: West)
        for dy in (-0.5, 0.5):
            y = round(by + dy, 2)
            x_lo = round(bx - 0.5, 2)
            x_hi = round(bx + 0.5, 2)
            e_lo = ent_map.get((x_lo, y))
            e_hi = ent_map.get((x_hi, y))
            if (
                e_lo
                and e_hi
                and e_lo.get("name") == "transport-belt"
                and e_hi.get("name") == "transport-belt"
                and e_lo.get("direction") == e_hi.get("direction")
            ):
                d = e_lo.get("direction", 4)
                if d == 4:  # East
                    in_pos = (round(bx - 1.5, 2), y)
                    out_pos = (round(bx + 1.5, 2), y)
                    e_in = ent_map.get(in_pos)
                    e_out = ent_map.get(out_pos)
                    if (
                        e_in
                        and e_out
                        and e_in.get("direction") == 4
                        and e_out.get("direction") == 4
                    ):
                        e_in["name"] = "underground-belt"
                        e_in["type"] = "input"
                        e_out["name"] = "underground-belt"
                        e_out["type"] = "output"
                        to_remove.add((x_lo, y))
                        to_remove.add((x_hi, y))
                        ent_map.pop((x_lo, y), None)
                        ent_map.pop((x_hi, y), None)
                elif d == 12:  # West
                    in_pos = (round(bx + 1.5, 2), y)
                    out_pos = (round(bx - 1.5, 2), y)
                    e_in = ent_map.get(in_pos)
                    e_out = ent_map.get(out_pos)
                    if (
                        e_in
                        and e_out
                        and e_in.get("direction") == 12
                        and e_out.get("direction") == 12
                    ):
                        e_in["name"] = "underground-belt"
                        e_in["type"] = "input"
                        e_out["name"] = "underground-belt"
                        e_out["type"] = "output"
                        to_remove.add((x_lo, y))
                        to_remove.add((x_hi, y))
                        ent_map.pop((x_lo, y), None)
                        ent_map.pop((x_hi, y), None)

    if to_remove:
        entities = [
            e
            for e in entities
            if (
                round(e.get("position", {}).get("x", 0.0), 2),
                round(e.get("position", {}).get("y", 0.0), 2),
            )
            not in to_remove
        ]
    return entities


def def_to_blueprint(
    def_text: str,
    netlist_data: dict[str, Any],
    label: str = "factory",
    strict_drc: bool = True,
    use_maze_router: bool = False,
) -> str:
    """Convert DEF text and netlist metadata into a blueprint exchange string."""
    def_data = parse_def(def_text)
    entities, wires = build_entities(
        def_data,
        netlist_data,
        strict_drc=strict_drc,
        use_maze_router=use_maze_router,
    )
    return blueprint.encode(entities, label, wires=wires)


def main() -> int:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Convert placed and routed DEF to Factorio blueprint string."
    )
    parser.add_argument("def_file", type=Path, help="Path to input DEF file")
    parser.add_argument("netlist_file", type=Path, help="Path to input netlist JSON file")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Path to write blueprint output",
    )
    parser.add_argument(
        "--pins-out",
        type=Path,
        default=None,
        help="Path to write extracted pins JSON",
    )
    parser.add_argument(
        "--label",
        type=str,
        default="factory",
        help="Blueprint label text",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw blueprint JSON document instead of exchange string",
    )
    parser.add_argument(
        "--time-out",
        type=Path,
        default=None,
        help="Path to write execution time JSON",
    )
    parser.add_argument(
        "--no-strict-drc",
        action="store_true",
        help="Do not abort on physical DRC violations",
    )
    parser.add_argument(
        "--use-maze-router",
        action="store_true",
        help="Route nets using 3D multi-layer DualLaneMazeRouter",
    )

    args = parser.parse_args()

    t0 = time.perf_counter()
    def_text = args.def_file.read_text()
    netlist_data = json.loads(args.netlist_file.read_text())

    def_data = parse_def(def_text)
    entities, wires = build_entities(
        def_data,
        netlist_data,
        strict_drc=not args.no_strict_drc,
        use_maze_router=args.use_maze_router,
    )

    tiles: list[dict[str, Any]] | None = None
    if def_data.get("blockages"):
        try:
            from foundry.chassis import CityBlockChassis
        except ImportError:
            from chassis import CityBlockChassis

        units = float(def_data.get("units", 1000))
        die_w = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[2] / units
        die_h = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[3] / units
        grid_w = max(1, round(die_w / 100.0))
        grid_h = max(1, round(die_h / 100.0))

        bp_candidates = [
            Path("cityblock.bp"),
            Path(__file__).resolve().parent.parent / "cityblock.bp",
            Path("/home/atv/src/factorilog/cityblock.bp"),
        ]
        chassis: CityBlockChassis | None = None
        for bp_cand in bp_candidates:
            if bp_cand.is_file():
                try:
                    chassis = CityBlockChassis.from_blueprint_file(bp_cand)
                    break
                except Exception:
                    pass
        if chassis is None:
            chassis = CityBlockChassis(pitch=100, core_width=94, core_height=94, channel_width=6)

        base_bp = chassis.emit_base_blueprint(grid_w, grid_h)
        base_entities = base_bp.get("blueprint", {}).get("entities", [])
        tiles = base_bp.get("blueprint", {}).get("tiles", [])

        entities = jumper_belts_across_chassis_poles(entities, base_entities)

        occupied_tiles: set[tuple[float, float]] = set()
        for e in entities:
            pos = e.get("position", {})
            ex = float(pos.get("x", 0.0))
            ey = float(pos.get("y", 0.0))
            ename = e.get("name", "")
            if "splitter" in ename:
                d = e.get("direction", 0)
                if d in (0, 8):
                    occupied_tiles.add((round(ex - 0.5, 2), round(ey, 2)))
                    occupied_tiles.add((round(ex + 0.5, 2), round(ey, 2)))
                else:
                    occupied_tiles.add((round(ex, 2), round(ey - 0.5, 2)))
                    occupied_tiles.add((round(ex, 2), round(ey + 0.5, 2)))
            elif (
                "assembling-machine" in ename
                or "electric-furnace" in ename
                or "lab" in ename
                or "chemical-plant" in ename
            ):
                for dx in (-1.0, 0.0, 1.0):
                    for dy in (-1.0, 0.0, 1.0):
                        occupied_tiles.add((round(ex + dx, 2), round(ey + dy, 2)))
            elif "oil-refinery" in ename:
                for dx in (-2.0, -1.0, 0.0, 1.0, 2.0):
                    for dy in (-2.0, -1.0, 0.0, 1.0, 2.0):
                        occupied_tiles.add((round(ex + dx, 2), round(ey + dy, 2)))
            else:
                occupied_tiles.add((round(ex, 2), round(ey, 2)))

        for be in base_entities:
            if "position" not in be:
                continue
            bpos = be.get("position", {})
            bx = float(bpos.get("x", 0.0))
            by = float(bpos.get("y", 0.0))
            bname = be.get("name", "")
            b_tiles: list[tuple[float, float]] = []
            if bname == "big-electric-pole":
                b_tiles.extend(
                    (round(bx + dx, 2), round(by + dy, 2))
                    for dx in (-0.5, 0.5)
                    for dy in (-0.5, 0.5)
                )
            elif bname == "radar":
                b_tiles.extend(
                    (round(bx + dx, 2), round(by + dy, 2))
                    for dx in (-1.0, 0.0, 1.0)
                    for dy in (-1.0, 0.0, 1.0)
                )
            else:
                b_tiles.append((round(bx, 2), round(by, 2)))

            if any(t in occupied_tiles for t in b_tiles):
                continue

            for t in b_tiles:
                occupied_tiles.add(t)

            new_be = dict(be)
            new_be.pop("neighbours", None)
            new_be.pop("connections", None)
            new_be["entity_number"] = len(entities) + 1
            entities.append(new_be)

        # Wire chassis big electric poles together into a unified power and circuit bus grid
        big_poles = [e for e in entities if e.get("name") == "big-electric-pole"]
        seen_pairs: set[tuple[int, int]] = set()
        if wires is None:
            wires = []

        for i, bp1 in enumerate(big_poles):
            p1_num = int(bp1.get("entity_number", 0))
            p1_pos = bp1.get("position", {})
            x1, y1 = float(p1_pos.get("x", 0.0)), float(p1_pos.get("y", 0.0))
            for bp2 in big_poles[i + 1 :]:
                p2_num = int(bp2.get("entity_number", 0))
                p2_pos = bp2.get("position", {})
                x2, y2 = float(p2_pos.get("x", 0.0)), float(p2_pos.get("y", 0.0))
                dx = abs(x1 - x2)
                dy = abs(y1 - y2)
                dist = math.hypot(dx, dy)
                if dist <= 30.5 and (dx < 0.1 or dy < 0.1):
                    pair = (min(p1_num, p2_num), max(p1_num, p2_num))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        # 5: copper wire, 1: red wire, 2: green wire
                        wires.append([pair[0], 5, pair[1], 5])
                        wires.append([pair[0], 1, pair[1], 1])
                        wires.append([pair[0], 2, pair[1], 2])

        # Connect medium electric poles to the nearest big electric pole within reach (<= 9.0 tiles)
        med_poles = [e for e in entities if e.get("name") == "medium-electric-pole"]
        for mp in med_poles:
            mp_num = int(mp.get("entity_number", 0))
            mp_pos = mp.get("position", {})
            mx, my = float(mp_pos.get("x", 0.0)), float(mp_pos.get("y", 0.0))
            best_d = float("inf")
            best_bp = None
            for bp in big_poles:
                bp_pos = bp.get("position", {})
                bx, by = float(bp_pos.get("x", 0.0)), float(bp_pos.get("y", 0.0))
                d = math.hypot(mx - bx, my - by)
                if d < best_d:
                    best_d = d
                    best_bp = bp
            if best_bp is not None and best_d <= 9.0:
                bp_num = int(best_bp.get("entity_number", 0))
                pair = (min(mp_num, bp_num), max(mp_num, bp_num))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    wires.append([pair[0], 5, pair[1], 5])

    if args.json:
        document: dict[str, Any] = {
            "blueprint": {
                "item": "blueprint",
                "label": args.label,
                "version": blueprint.VERSION_2_0,
                "entities": entities,
            }
        }
        if wires:
            document["blueprint"]["wires"] = wires
        if tiles:
            document["blueprint"]["tiles"] = tiles
        output_text = json.dumps(document, indent=2)
    else:
        output_text = blueprint.encode(entities, args.label, wires=wires, tiles=tiles)

    if args.out:
        args.out.write_text(output_text + "\n")
    else:
        sys.stdout.write(output_text + "\n")

    if args.pins_out:
        pins_data = extract_pins(def_data, netlist_data)
        args.pins_out.parent.mkdir(parents=True, exist_ok=True)
        args.pins_out.write_text(json.dumps(pins_data, indent=2) + "\n")

    t1 = time.perf_counter()
    elapsed = round(t1 - t0, 3)

    if args.time_out:
        args.time_out.parent.mkdir(parents=True, exist_ok=True)
        time_data = {
            "stage": "bp",
            "elapsed_seconds": elapsed,
        }
        args.time_out.write_text(json.dumps(time_data, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
