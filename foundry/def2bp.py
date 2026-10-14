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


def _resolve_terminal(
    inst_name: str,
    port: str,
    net_name: str,
    comp_map: dict[str, Any],
    inst_map: dict[str, Any],
    cell_map: dict[str, Any],
    pin_dict: dict[str, tuple[int, int]],
    valid_in_pins: set[tuple[float, float]],
    scale: float,
    clean_fluids: set[str] | None = None,
) -> tuple[tuple[int, int], bool, int | None, int | None] | None:
    if inst_name == "PIN":
        pos = pin_dict.get(net_name) or pin_dict.get(port)
        if pos is None:
            return None
        is_driver = (float(pos[0]), float(pos[1])) in valid_in_pins
        inward_dir = DIRECTION_WEST if pos[0] == 0 else DIRECTION_NORTH if pos[1] == 0 else None
        return (pos, is_driver, None, inward_dir)

    if inst_name not in comp_map:
        return None

    comp = comp_map[inst_name]
    gx = comp["x"] * scale
    gy = comp["y"] * scale
    inst = inst_map.get(inst_name, {})
    cell_id = inst.get("cell", comp.get("macro", ""))
    cell = cell_map.get(cell_id, {})
    orient = comp.get("orient", "N")

    igx = round(gx)
    igy = round(gy)

    if orient in ("W", "E"):
        splitter_dir = DIRECTION_NORTH if orient == "W" else DIRECTION_SOUTH
        pump_dir = DIRECTION_WEST if orient == "W" else DIRECTION_EAST
    else:
        splitter_dir = DIRECTION_WEST if orient == "S" else DIRECTION_EAST
        pump_dir = DIRECTION_SOUTH if orient == "S" else DIRECTION_NORTH

    if cell_id == "belt_merge2" or comp.get("macro") == "belt_merge2":
        if orient in ("W", "E"):
            if port in ("a", "a1"):
                return ((igx, igy - 1), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx + 1, igy - 1), False, splitter_dir, splitter_dir)
            if port == "y":
                return ((igx, igy + 1), True, splitter_dir, None)
        else:
            if port in ("a", "a1"):
                return ((igx - 1, igy), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx - 1, igy + 1), False, splitter_dir, splitter_dir)
            if port == "y":
                return ((igx + 1, igy), True, splitter_dir, None)

    elif cell_id in ("belt_split2", "belt_balancer2") or comp.get("macro") in (
        "belt_split2",
        "belt_balancer2",
    ):
        if orient in ("W", "E"):
            if port in ("a", "a1"):
                return ((igx, igy - 1), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx + 1, igy - 1), False, splitter_dir, splitter_dir)
            if port == "y1":
                return ((igx, igy + 1), True, splitter_dir, None)
            if port == "y2":
                return ((igx + 1, igy + 1), True, splitter_dir, None)
        else:
            if port in ("a", "a1"):
                return ((igx - 1, igy), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx - 1, igy + 1), False, splitter_dir, splitter_dir)
            if port == "y1":
                return ((igx + 1, igy), True, splitter_dir, None)
            if port == "y2":
                return ((igx + 1, igy + 1), True, splitter_dir, None)

    elif cell_id == "pump" or comp.get("macro") == "pump":
        if port == "a":
            return ((igx, igy - 1), False, pump_dir, pump_dir)
        if port == "y":
            return ((igx, igy + 2), True, pump_dir, None)

    else:
        outputs = [o.replace("-", "_") for o in cell.get("outputs", [])]
        clean_port = port.replace("-", "_")
        try:
            from foundry.lef import get_macro_port_geometry
        except ImportError:
            from lef import get_macro_port_geometry

        if clean_port in outputs:
            orig_outputs = cell.get("outputs", [])
            idx = outputs.index(clean_port)
            orig_name = orig_outputs[idx] if idx < len(orig_outputs) else clean_port
            geom = get_macro_port_geometry(cell, orig_name, is_output=True)
            is_fluid_port = bool(clean_fluids and orig_name in clean_fluids)
            pos_key = "inserter_pos" if is_fluid_port else "approach_pos"
            ax, ay = geom[pos_key]
            head = geom.get("approach_dir") if not is_fluid_port else None
            face = geom.get("face", "E")
            inward_dir = (
                (
                    DIRECTION_WEST
                    if face == "E"
                    else DIRECTION_EAST
                    if face == "W"
                    else DIRECTION_SOUTH
                    if face == "N"
                    else DIRECTION_NORTH
                )
                if is_fluid_port
                else None
            )
            return ((math.floor(gx + ax), math.floor(gy + ay)), True, head, inward_dir)

        inputs = [i.replace("-", "_") for i in cell.get("inputs", [])]
        if clean_port in inputs:
            orig_inputs = cell.get("inputs", [])
            idx = inputs.index(clean_port)
            orig_name = orig_inputs[idx] if idx < len(orig_inputs) else clean_port
            geom = get_macro_port_geometry(cell, orig_name, is_output=False)
            is_fluid_port = bool(clean_fluids and orig_name in clean_fluids)
            pos_key = "inserter_pos" if is_fluid_port else "approach_pos"
            ax, ay = geom[pos_key]
            face = geom.get("face", "W")
            if is_fluid_port:
                approach_head = (
                    DIRECTION_WEST
                    if face == "E"
                    else DIRECTION_EAST
                    if face == "W"
                    else DIRECTION_SOUTH
                    if face == "N"
                    else DIRECTION_NORTH
                )
            else:
                approach_head = geom.get("approach_dir") if face in ("W", "E") else None
            return ((math.floor(gx + ax), math.floor(gy + ay)), False, None, approach_head)

    return ((igx, igy), False, None, None)


def route_with_maze_router(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
    placement: Any,
    engine: Any | None = None,
) -> RouteResult:
    """Route DEF nets using the 3D multi-layer DualLaneMazeRouter.

    Extract terminal coordinates for each logical net from components and pins.
    Execute the multi-layer router across surface and subterranean layers.
    Add routed physical entities directly to the placement record.
    """
    try:
        from foundry.native_engine.verifier import load_game_prototypes
        from foundry.router.maze_router import DualLaneMazeRouter
        from foundry.router.types import RouterNetSpec
    except ImportError:
        from native_engine.verifier import load_game_prototypes
        from router.maze_router import DualLaneMazeRouter
        from router.types import RouterNetSpec

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
        px = math.floor(pin.get("x", 0) * scale)
        py = math.floor(pin.get("y", 0) * scale)
        pname = pin.get("name", pin.get("net", ""))
        pin_dict[pname] = (px, py)
        direction = pin.get("direction", "")
        if direction == "INPUT":
            valid_in_pins.add((float(px), float(py)))
            obstacles.update(((px - 1, py), (px - 2, py), (px - 1, py - 1), (px - 1, py + 1)))
        elif direction == "OUTPUT":
            valid_out_pins.add((float(px), float(py)))
            obstacles.update(((px + 1, py), (px + 2, py), (px + 1, py - 1), (px + 1, py + 1)))

    fluids = set(netlist_data.get("fluids", [])) or set(placement.fluids or [])
    clean_fluids = (
        {f.replace("-", "_") for f in fluids} | {f.replace("_", "-") for f in fluids} | set(fluids)
    )
    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})

    net_specs: list[RouterNetSpec] = []

    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        conns = net.get("connections", [])
        if len(conns) < 2:
            continue

        drivers: list[tuple[tuple[int, int], int | None, int | None]] = []
        sinks: list[tuple[tuple[int, int], int | None, int | None]] = []

        for inst_name, port in conns:
            term = _resolve_terminal(
                inst_name,
                port,
                nname,
                comp_map,
                inst_map,
                cell_map,
                pin_dict,
                valid_in_pins,
                scale,
                clean_fluids=clean_fluids,
            )
            if term is not None:
                coord, is_drv, head, term_head = term
                if is_drv:
                    drivers.append((coord, head, term_head))
                else:
                    sinks.append((coord, head, term_head))

        if not drivers and sinks and len(conns) >= 2:
            drivers.append(sinks.pop(0))
        elif not sinks and drivers and len(conns) >= 2:
            sinks.append(drivers.pop())

        is_fluid = any(cf in nname.replace("-", "_") for cf in clean_fluids)
        commodity = nname.split("__")[0].replace("_", "-")

        if is_fluid and (len(drivers) > 1 or len(sinks) > 1):
            all_terms = drivers + sinks
            seen_nodes: dict[tuple[int, int], tuple[tuple[int, int], int | None, int | None]] = {}
            for t in all_terms:
                if t[0] not in seen_nodes:
                    seen_nodes[t[0]] = t
            nodes = list(seen_nodes.values())
            if len(nodes) >= 2:
                in_tree = [nodes[0]]
                remaining = set(nodes[1:])
                edge_idx = 0
                while remaining:
                    best_dist = float("inf")
                    best_pair = None
                    for u in in_tree:
                        ux, uy = u[0]
                        for v in remaining:
                            vx, vy = v[0]
                            dist = abs(ux - vx) + abs(uy - vy)
                            if dist < best_dist:
                                best_dist = dist
                                best_pair = (u, v)
                    if best_pair is None:
                        break
                    u, v = best_pair
                    in_tree.append(v)
                    remaining.remove(v)
                    net_specs.append(
                        RouterNetSpec(
                            name=f"{nname}_mst_{edge_idx}",
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
                    edge_idx += 1
        else:
            for d_idx, (d_pos, d_head, d_term) in enumerate(drivers):
                for s_idx, (s_pos, s_head, s_term) in enumerate(sinks):
                    if d_pos != s_pos:
                        sub_name = nname
                        if len(drivers) > 1 or len(sinks) > 1:
                            sub_name = f"{nname}_d{d_idx}_s{s_idx}"
                        net_specs.append(
                            RouterNetSpec(
                                name=sub_name,
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
                        )
    # Ensure source and sink tiles are not blocked by fixed obstacles
    for nspec in net_specs:
        obstacles.discard(nspec.source)
        obstacles.discard(nspec.sink)
        min_x = min(min_x, nspec.source[0] - 2, nspec.sink[0] - 2)
        max_x = max(max_x, nspec.source[0] + 2, nspec.sink[0] + 2)
        min_y = min(min_y, nspec.source[1] - 2, nspec.sink[1] - 2)
        max_y = max(max_y, nspec.source[1] + 2, nspec.sink[1] + 2)

    component_headings: dict[tuple[int, int], int] = {}
    for ent in placement.entities:
        ename = ent.get("name", "")
        if "splitter" in ename:
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            d = ent.get("direction", 0)
            if d in (0, 8):  # North, South
                component_headings[(math.floor(cx - 0.5), math.floor(cy))] = d
                component_headings[(math.floor(cx + 0.5), math.floor(cy))] = d
            else:  # East, West
                component_headings[(math.floor(cx), math.floor(cy - 0.5))] = d
                component_headings[(math.floor(cx), math.floor(cy + 0.5))] = d

    if engine is False:
        engine = None
    elif engine is None:
        try:
            from foundry.native_engine.engine import FactorioNativeEngine

            engine = FactorioNativeEngine()
        except Exception:
            try:
                from native_engine.engine import FactorioNativeEngine

                engine = FactorioNativeEngine()
            except Exception:
                pass

    prototypes = load_game_prototypes()
    router = DualLaneMazeRouter(
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        engine=engine,
        prototypes=prototypes,
        obstacles=obstacles,
        component_headings=component_headings,
    )
    routes, entities, verif_res = router.route(net_specs, verify=engine is not None)
    placement.router_routes = routes
    placement.router_net_specs = net_specs
    placement.router_verif_res = verif_res
    if verif_res and not verif_res.is_valid:
        for err in verif_res.lvs_errors:
            sys.stderr.write(
                f"factorilog [maze_router]: NATIVE LVS ERROR: {err.net_name}: {err.message}\n"
            )
        for drc in verif_res.drc_violations:
            sys.stderr.write(
                f"factorilog [maze_router]: NATIVE DRC ERROR: [{drc.code}] at {drc.position}: {drc.message}\n"
            )
    failed = [res for res in routes.values() if not res.is_success]
    if failed:
        for f in failed[:10]:
            sys.stderr.write(
                f"factorilog [maze_router]: NET ROUTE FAILED: {f.net_name}: {f.error_message}\n"
            )

    for ent in entities:
        placement.entities.append(ent)
        epos = ent.get("position", {})
        pos_key = (round(float(epos.get("x", 0.0)), 2), round(float(epos.get("y", 0.0)), 2))
        placement.occupied_positions.add(pos_key)
        placement.grid_entities[pos_key] = ent

    net_surface_tiles: dict[str, dict[tuple[float, float], int]] = {}
    tile_to_nets: dict[tuple[float, float], set[str]] = {}
    for nname, route_res in routes.items():
        base_name = nname
        if "_d" in nname and "_s" in nname:
            base_name = nname.split("_d")[0]
        elif "_mst_" in nname:
            base_name = nname.split("_mst_")[0]
        net_surface_tiles.setdefault(base_name, {})
        for node in route_res.nodes:
            if node.z == 0:
                k = (float(node.x), float(node.y))
                net_surface_tiles[base_name][k] = node.heading
                tile_to_nets.setdefault(k, set()).add(base_name)

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
    use_maze_router: bool = True,
    engine: Any | None = None,
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """Build Factorio entities and wire connections from DEF and netlist data."""
    def_data["_netlist_data"] = netlist_data

    # 1. Untangle routing tracks (topological pin swaps and iterative geometric/physical loop)
    untangle_def(def_data, netlist_data)

    scale = 1.0 / float(def_data.get("units", 1000))

    # 2. Place components, machines, and inserters
    placement = place_components(def_data, netlist_data)
    def_data["_placement"] = placement

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
        routes = route_with_maze_router(def_data, netlist_data, placement, engine=engine)
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

    # 6. Prune dead-end stub belts (only needed for legacy rasterizer)
    if not use_maze_router:
        entities = prune_dead_end_belts(placement.entities, def_data, netlist_data)
    else:
        entities = list(placement.entities)
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

    if strict_drc:
        router_routes = getattr(placement, "router_routes", None) or {}
        failed_routes = [r for r in router_routes.values() if not r.is_success]
        if failed_routes:
            raise ValueError(
                f"Router failed on {len(failed_routes)} net(s): "
                + ", ".join(r.net_name for r in failed_routes[:5])
            )
        verif_res = getattr(placement, "router_verif_res", None)
        if verif_res is not None and not verif_res.is_valid:
            raise ValueError(
                f"Native router verification failed with {len(verif_res.lvs_errors)} LVS error(s) "
                f"and {len(verif_res.drc_violations)} DRC violation(s)"
            )

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
        default=True,
        help="Route nets using 3D multi-layer DualLaneMazeRouter (default: True, Plan of Record)",
    )
    parser.add_argument(
        "--no-maze-router",
        action="store_false",
        dest="use_maze_router",
        help="Disable maze router",
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
