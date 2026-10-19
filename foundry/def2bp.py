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
        DIR_STEPS,
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
        DIR_STEPS,
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
        base_comm = net_name.split("__")[0].replace("-", "_")
        is_fluid_pin = bool(
            clean_fluids and (base_comm in clean_fluids or port.replace("-", "_") in clean_fluids)
        )
        if is_fluid_pin:
            inward_dir = DIRECTION_WEST if pos[0] == 0 else DIRECTION_NORTH if pos[1] == 0 else None
            return (pos, is_driver, None, inward_dir)
        if is_driver:
            pin_head = (
                DIRECTION_EAST
                if pos[0] == 0
                else DIRECTION_SOUTH
                if pos[1] == 0
                else DIRECTION_EAST
            )
            return (pos, True, pin_head, pin_head)
        pin_head = (
            DIRECTION_WEST if pos[0] == 0 else DIRECTION_NORTH if pos[1] == 0 else DIRECTION_EAST
        )
        return (pos, False, pin_head, pin_head)

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
        if orient == "W":
            if port in ("a", "a1"):
                return ((igx, igy + 3), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx + 1, igy + 3), False, splitter_dir, splitter_dir)
            if port == "y":
                return ((igx + 1, igy - 1), True, splitter_dir, None)
        elif orient == "E":
            if port in ("a", "a1"):
                return ((igx, igy - 1), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx + 1, igy - 1), False, splitter_dir, splitter_dir)
            if port == "y":
                return ((igx + 1, igy + 3), True, splitter_dir, None)
        elif orient == "S":
            if port in ("a", "a1"):
                return ((igx + 3, igy), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx + 3, igy + 1), False, splitter_dir, splitter_dir)
            if port == "y":
                return ((igx - 1, igy + 1), True, splitter_dir, None)
        else:
            if port in ("a", "a1"):
                return ((igx - 1, igy), False, splitter_dir, splitter_dir)
            if port in ("b", "a2"):
                return ((igx - 1, igy + 1), False, splitter_dir, splitter_dir)
            if port == "y":
                return ((igx + 3, igy + 1), True, splitter_dir, None)

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
    strict_drc: bool = False,
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

        base_commodity = nname.split("__")[0].replace("-", "_")
        is_fluid = bool(clean_fluids and base_commodity in clean_fluids)
        commodity = base_commodity.replace("_", "-")

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

    pin_no_hood_tiles: set[tuple[int, int]] = set()
    component_headings: dict[tuple[int, int], int] = {}
    fluid_port_approaches: dict[tuple[int, int], set[str]] = {}
    for nspec in net_specs:
        if not nspec.is_fluid:
            if (float(nspec.source[0]), float(nspec.source[1])) in valid_in_pins:
                pin_no_hood_tiles.add(nspec.source)
                if nspec.start_heading is not None:
                    component_headings[nspec.source] = nspec.start_heading
            if (float(nspec.sink[0]), float(nspec.sink[1])) in valid_out_pins:
                pin_no_hood_tiles.add(nspec.sink)
                if nspec.end_heading is not None:
                    component_headings[nspec.sink] = nspec.end_heading
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
        elif "transport-belt" in ename:
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            ed = ent.get("direction", 0)
            tx, ty = math.floor(cx), math.floor(cy)
            component_headings[(tx, ty)] = ed
            dx, dy = DIR_STEPS.get(ed, (0, 0))
            pin_no_hood_tiles.add((tx + dx, ty + dy))
        elif ename == "pipe" and ent.get("fluid"):
            pos = ent.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            tx, ty = math.floor(cx), math.floor(cy)
            p_comm = str(ent["fluid"]).replace("_", "-")
            fluid_port_approaches.setdefault((tx, ty), set()).add(p_comm)
            for odx, ody in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                fluid_port_approaches.setdefault((tx + odx, ty + ody), set()).add(p_comm)

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
    try:
        from foundry.router.grid import base_net_name
    except ImportError:
        from router.grid import base_net_name

    units = float(def_data.get("units", 1000))
    die_w = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[2] / units
    die_h = def_data.get("die_area", [0, 0, 100 * units, 100 * units])[3] / units
    grid_w = max(1, round(die_w / 100.0))
    grid_h = max(1, round(die_h / 100.0))
    use_hierarchical = bool(def_data.get("blockages")) and (grid_w * grid_h > 1)

    if use_hierarchical:
        try:
            from foundry.macro_floorplan import MacroFloorplanner
            from foundry.native_engine.verifier import NativeLayoutVerifier, NetSpec
        except ImportError:
            from macro_floorplan import MacroFloorplanner
            from native_engine.verifier import NativeLayoutVerifier, NetSpec

        hwy_min_x = 0
        hwy_min_y = 0
        hwy_max_x = grid_w * 100 - 1
        hwy_max_y = grid_h * 100 - 1
        for ns in net_specs:
            hwy_min_x = min(hwy_min_x, ns.source[0], ns.sink[0])
            hwy_min_y = min(hwy_min_y, ns.source[1], ns.sink[1])
            hwy_max_x = max(hwy_max_x, ns.source[0], ns.sink[0])
            hwy_max_y = max(hwy_max_y, ns.source[1], ns.sink[1])

        bay_blocks: set[tuple[int, int]] = set()
        for comp in def_data.get("components", []):
            cx_t = round(comp["x"] * scale)
            cy_t = round(comp["y"] * scale)
            bx_c = cx_t // 100
            by_c = cy_t // 100
            if 0 <= bx_c < grid_w and 0 <= by_c < grid_h and 3 <= (cy_t % 100) <= 16:
                bay_blocks.add((bx_c, by_c))

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

        routes: dict[str, Any] = {}
        entities_by_pos: dict[tuple[float, float], dict[str, Any]] = {}
        all_routed_specs: list[RouterNetSpec] = []

        margin = h_floorplan.chassis.core_margin_x
        total_intra = sum(len(ns) for ns in intra_block_specs.values())
        t_stage1_start = time.perf_counter()
        print(
            f"[def2bp] Stage 1: routing {total_intra} intra-block nets across {len(intra_block_specs)} blocks (margin={margin})...",
            file=sys.stderr,
            flush=True,
        )

        # Stage 1: Route each occupied city block core independently
        for (bx, by), blk_nets in sorted(intra_block_specs.items()):
            if not blk_nets:
                continue
            t_blk = time.perf_counter()
            b_min_x, b_min_y, b_max_x, b_max_y = macro_planner._block_core_bbox(
                (bx, by), bay_blocks, margin=margin
            )
            blk_obstacles = {
                pt
                for pt in obstacles
                if b_min_x <= pt[0] <= b_max_x and b_min_y <= pt[1] <= b_max_y
            }
            # Mask the 1-tile outer core ring except at allocated BlockPort core_pos terminals
            for rx in range(b_min_x, b_max_x + 1):
                blk_obstacles.add((rx, b_min_y))
                blk_obstacles.add((rx, b_max_y))
            for ry in range(b_min_y, b_max_y + 1):
                blk_obstacles.add((b_min_x, ry))
                blk_obstacles.add((b_max_x, ry))
            for ns in blk_nets:
                blk_obstacles.discard(ns.source)
                blk_obstacles.discard(ns.sink)

            blk_router = DualLaneMazeRouter(
                min_x=b_min_x,
                max_x=b_max_x,
                min_y=b_min_y,
                max_y=b_max_y,
                engine=None,
                prototypes=prototypes,
                obstacles=blk_obstacles,
                component_headings=merged_headings,
                no_hood_tiles=port_no_hood_tiles,
                fluid_port_approaches=fluid_port_approaches,
            )
            b_routes, b_entities, _ = blk_router.route(blk_nets, verify=False)
            dt_blk = time.perf_counter() - t_blk
            b_failed = [r for r in b_routes.values() if not r.is_success]
            if dt_blk >= 0.5 or b_failed:
                print(
                    f"[def2bp]   block ({bx},{by}): {len(blk_nets)} nets in {dt_blk:.2f}s (failed={len(b_failed)})",
                    file=sys.stderr,
                    flush=True,
                )
            routes.update(b_routes)
            all_routed_specs.extend(blk_nets)
            for ent in b_entities:
                epos = ent.get("position", {})
                pkey = (round(float(epos.get("x", 0.0)), 2), round(float(epos.get("y", 0.0)), 2))
                entities_by_pos[pkey] = ent
            if b_failed and strict_drc:
                for f in b_failed[:25]:
                    sys.stderr.write(
                        f"factorilog [maze_router]: NET ROUTE FAILED: {f.net_name}: {f.error_message}\n"
                    )
                raise ValueError(
                    f"Stage 1 block ({bx},{by}) router failed on {len(b_failed)} net(s): "
                    + ", ".join(r.net_name for r in b_failed[:5])
                )

        print(
            f"[def2bp] Stage 1 completed in {time.perf_counter() - t_stage1_start:.2f}s",
            file=sys.stderr,
            flush=True,
        )

        # Stage 2: Route inter-block highway trunks through street channels, switchboxes, and matrix bays
        if highway_specs:
            t_stage2_start = time.perf_counter()
            print(
                f"[def2bp] Stage 2: routing {len(highway_specs)} highway nets across {grid_w}x{grid_h} street grid...",
                file=sys.stderr,
                flush=True,
            )
            hwy_obstacles: set[tuple[int, int]] = set()
            for bx in range(grid_w):
                for by in range(grid_h):
                    for cx_tile in range(bx * 100 + margin, bx * 100 + 100 - margin):
                        for cy_tile in range(by * 100 + margin, by * 100 + 100 - margin):
                            hwy_obstacles.add((cx_tile, cy_tile))
            for comp in def_data.get("components", []):
                cx_t = round(comp["x"] * scale)
                cy_t = round(comp["y"] * scale)
                bx_c = cx_t // 100
                by_c = cy_t // 100
                if 0 <= bx_c < grid_w and 0 <= by_c < grid_h and 3 <= (cy_t % 100) <= 16:
                    for nx_t in range(bx_c * 100 + margin, bx_c * 100 + 100 - margin):
                        for ny_t in range(by_c * 100 + margin, by_c * 100 + 17):
                            hwy_obstacles.discard((nx_t, ny_t))
            hwy_obstacles.update(obstacles)
            for ns in highway_specs:
                hwy_obstacles.discard(ns.source)
                hwy_obstacles.discard(ns.sink)

            hwy_router = DualLaneMazeRouter(
                min_x=hwy_min_x,
                max_x=hwy_max_x,
                min_y=hwy_min_y,
                max_y=hwy_max_y,
                engine=None,
                prototypes=prototypes,
                obstacles=hwy_obstacles,
                component_headings=merged_headings,
                no_hood_tiles=port_no_hood_tiles,
                fluid_port_approaches=fluid_port_approaches,
            )
            h_routes, h_entities, _ = hwy_router.route(
                highway_specs, max_iterations=60, verify=False
            )
            h_failed = [r for r in h_routes.values() if not r.is_success]
            print(
                f"[def2bp] Stage 2 completed in {time.perf_counter() - t_stage2_start:.2f}s (failed={len(h_failed)})",
                file=sys.stderr,
                flush=True,
            )
            routes.update(h_routes)
            all_routed_specs.extend(highway_specs)
            for ent in h_entities:
                epos = ent.get("position", {})
                pkey = (round(float(epos.get("x", 0.0)), 2), round(float(epos.get("y", 0.0)), 2))
                entities_by_pos[pkey] = ent
            if h_failed and strict_drc:
                for f in h_failed[:25]:
                    sys.stderr.write(
                        f"factorilog [maze_router]: NET ROUTE FAILED: {f.net_name}: {f.error_message}\n"
                    )
                raise ValueError(
                    f"Stage 2 highway router failed on {len(h_failed)} net(s): "
                    + ", ".join(r.net_name for r in h_failed[:5])
                )

        entities = list(entities_by_pos.values())
        for idx, ent in enumerate(entities, start=1):
            ent["entity_number"] = idx

        failed = [res for res in routes.values() if not res.is_success]
        verif_res = None
        if engine is not None and entities and not failed:
            t_verif = time.perf_counter()
            verifier = NativeLayoutVerifier(engine, prototypes=prototypes)
            verifier_nets = [
                NetSpec(
                    name=n.name,
                    commodity=n.commodity,
                    source_pos=(n.source[0] + 0.5, n.source[1] + 0.5),
                    sink_pos=(n.sink[0] + 0.5, n.sink[1] + 0.5),
                    is_fluid=n.is_fluid,
                )
                for n in net_specs
            ]
            verif_res = verifier.verify_blueprint(entities, nets=verifier_nets, ticks=60)
            print(
                f"[def2bp] NativeLayoutVerifier completed in {time.perf_counter() - t_verif:.2f}s",
                file=sys.stderr,
                flush=True,
            )

        placement.router_routes = routes
        placement.router_net_specs = all_routed_specs
        placement.router_verif_res = verif_res
    else:
        router = DualLaneMazeRouter(
            min_x=min_x,
            max_x=max_x,
            min_y=min_y,
            max_y=max_y,
            engine=engine,
            prototypes=prototypes,
            obstacles=obstacles,
            component_headings=component_headings,
            no_hood_tiles=pin_no_hood_tiles,
            fluid_port_approaches=fluid_port_approaches,
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
        for f in failed[:25]:
            sys.stderr.write(
                f"factorilog [maze_router]: NET ROUTE FAILED: {f.net_name}: {f.error_message}\n"
            )
        if strict_drc:
            raise ValueError(
                f"Router failed on {len(failed)} net(s): "
                + ", ".join(r.net_name for r in failed[:5])
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
        base_name = base_net_name(nname)
        net_surface_tiles.setdefault(base_name, {})
        for node in route_res.nodes:
            if node.z == 0:
                k = (node.x + 0.5, node.y + 0.5)
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
    untangle_def(def_data, netlist_data, pins_only=use_maze_router)

    scale = 1.0 / float(def_data.get("units", 1000))

    # 2. Place components, machines, and inserters
    placement = place_components(def_data, netlist_data)
    def_data["_placement"] = placement

    fabric_entities: list[dict[str, Any]] = []
    input_channel_entities: list[dict[str, Any]] = []
    floorplan = None
    chassis_base_entities: list[dict[str, Any]] = []
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

        chassis: CityBlockChassis | None = None
        if grid_w * grid_h >= 4:
            chassis = CityBlockChassis(pitch=100, channel_width=8, programmatic=True)
        else:
            bp_candidates = [
                Path("cityblock.bp"),
                Path(__file__).resolve().parent.parent / "cityblock.bp",
                Path("/home/atv/src/factorilog/cityblock.bp"),
            ]
            for bp_cand in bp_candidates:
                if bp_cand.is_file():
                    try:
                        chassis = CityBlockChassis.from_blueprint_file(bp_cand)
                        break
                    except Exception:
                        pass
            if chassis is None:
                chassis = CityBlockChassis(
                    pitch=100, core_width=94, core_height=94, channel_width=6
                )

        base_bp = chassis.emit_base_blueprint(grid_w, grid_h)
        chassis_base_entities = base_bp.get("blueprint", {}).get("entities", [])
        def_data["_chassis_tiles"] = base_bp.get("blueprint", {}).get("tiles", [])

        if use_maze_router:
            pin_coords = [
                (
                    math.floor(p.get("x", 0) * scale) + 0.5,
                    math.floor(p.get("y", 0) * scale) + 0.5,
                )
                for p in def_data.get("pins", [])
            ]
            for be in chassis_base_entities:
                if "position" not in be:
                    continue
                bpos = be.get("position", {})
                bx = float(bpos.get("x", 0.0))
                by = float(bpos.get("y", 0.0))
                bname = be.get("name", "")
                if grid_w * grid_h >= 4:
                    is_corner_pole = (
                        bname == "big-electric-pole"
                        and (bx % 100.0) in (5.0, 95.0)
                        and (by % 100.0) in (5.0, 95.0)
                    )
                    if bname != "radar" and not is_corner_pole:
                        continue
                elif bx <= 4.5 or bx >= die_w - 4.5 or by <= 4.5 or by >= die_h - 4.5:
                    continue
                if any(abs(bx - px) <= 3.5 and abs(by - py) <= 3.5 for px, py in pin_coords):
                    continue
                if bname == "big-electric-pole":
                    for dx in (-0.5, 0.5):
                        for dy in (-0.5, 0.5):
                            placement.occupied_positions.add((round(bx + dx, 2), round(by + dy, 2)))
                elif bname == "radar":
                    for dx in (-1.0, 0.0, 1.0):
                        for dy in (-1.0, 0.0, 1.0):
                            placement.occupied_positions.add((round(bx + dx, 2), round(by + dy, 2)))
                else:
                    placement.occupied_positions.add((round(bx, 2), round(by, 2)))
        else:
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
        routes = route_with_maze_router(
            def_data, netlist_data, placement, engine=engine, strict_drc=strict_drc
        )
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

    # 6b. Merge chassis base entities before assigning entity numbers and running DRC
    occupied_tiles: set[tuple[float, float]] = set()
    if def_data.get("blockages"):
        if not use_maze_router:
            entities = jumper_belts_across_chassis_poles(entities, chassis_base_entities)

        for e in entities:
            pos = e.get("position", {})
            ex = float(pos.get("x", 0.0))
            ey = float(pos.get("y", 0.0))
            ename = e.get("name", "")
            w, h = entity_size(ename, e.get("direction", DIRECTION_NORTH))
            min_tx = math.floor(ex - w / 2.0 - 1e-6)
            max_tx = math.floor(ex + w / 2.0 - 1e-6)
            min_ty = math.floor(ey - h / 2.0 - 1e-6)
            max_ty = math.floor(ey + h / 2.0 - 1e-6)
            for tx in range(min_tx, max_tx + 2):
                for ty in range(min_ty, max_ty + 2):
                    if max(ex - w / 2.0, tx) < min(ex + w / 2.0, tx + 1.0) and max(
                        ey - h / 2.0, ty
                    ) < min(ey + h / 2.0, ty + 1.0):
                        occupied_tiles.add((round(tx + 0.5, 2), round(ty + 0.5, 2)))
            occupied_tiles.add((round(ex, 2), round(ey, 2)))

        lamp_pole_buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        for e in entities:
            if "position" not in e:
                continue
            ename = e.get("name")
            if ename == "big-electric-pole":
                px, py = float(e["position"]["x"]), float(e["position"]["y"])
                lamp_pole_buckets.setdefault((int(px // 4), int(py // 4)), []).append((px, py, 2.0))
            elif ename == "medium-electric-pole":
                px, py = float(e["position"]["x"]), float(e["position"]["y"])
                lamp_pole_buckets.setdefault((int(px // 4), int(py // 4)), []).append((px, py, 3.5))

        sorted_base_entities = sorted(
            chassis_base_entities,
            key=lambda item: 0 if item.get("name") == "big-electric-pole" else 1,
        )
        for be in sorted_base_entities:
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

            if bname == "small-lamp":
                bk_x, bk_y = int(bx // 4), int(by // 4)
                has_pole = any(
                    abs(px - bx) <= r and abs(py - by) <= r
                    for dx_b in (-1, 0, 1)
                    for dy_b in (-1, 0, 1)
                    for px, py, r in lamp_pole_buckets.get((bk_x + dx_b, bk_y + dy_b), ())
                )
                if not has_pole:
                    continue

            for t in b_tiles:
                occupied_tiles.add(t)

            new_be = dict(be)
            new_be.pop("neighbours", None)
            new_be.pop("connections", None)
            new_be.pop("entity_number", None)
            entities.append(new_be)
            if bname == "big-electric-pole":
                lamp_pole_buckets.setdefault((int(bx // 4), int(by // 4)), []).append((bx, by, 2.0))
            elif bname == "medium-electric-pole":
                lamp_pole_buckets.setdefault((int(bx // 4), int(by // 4)), []).append((bx, by, 3.5))

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

    if def_data.get("blockages"):
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

        # Bridge any disconnected pole component to the main electric pole network
        if big_poles or med_poles:
            copper_adj: dict[int, set[int]] = {}
            for w_edge in wires:
                if len(w_edge) >= 4 and int(w_edge[1]) == 5 and int(w_edge[3]) == 5:
                    u_id, v_id = int(w_edge[0]), int(w_edge[2])
                    copper_adj.setdefault(u_id, set()).add(v_id)
                    copper_adj.setdefault(v_id, set()).add(u_id)

            all_pole_by_num = {int(p.get("entity_number", 0)): p for p in big_poles + med_poles}
            visited_p: set[int] = set()
            reached: set[int] = set()
            for start_id in all_pole_by_num:
                if start_id in visited_p:
                    continue
                comp_ids: set[int] = {start_id}
                visited_p.add(start_id)
                queue = [start_id]
                while queue:
                    curr_id = queue.pop()
                    for nxt_id in copper_adj.get(curr_id, ()):
                        if nxt_id in all_pole_by_num and nxt_id not in visited_p:
                            visited_p.add(nxt_id)
                            comp_ids.add(nxt_id)
                            queue.append(nxt_id)
                if len(comp_ids) > len(reached):
                    reached = comp_ids

            for px, py in routes.pin_tiles | routes.valid_input_pins | routes.valid_output_pins:
                p_pt = (round(math.floor(px) + 0.5, 2), round(math.floor(py) + 0.5, 2))
                occupied_tiles.add(p_pt)
                for k in (1, 2, 3):
                    occupied_tiles.add((round(p_pt[0] - k, 2), p_pt[1]))
                    occupied_tiles.add((round(p_pt[0] + k, 2), p_pt[1]))
                    occupied_tiles.add((p_pt[0], round(p_pt[1] - k, 2)))
                    occupied_tiles.add((p_pt[0], round(p_pt[1] + k, 2)))
            unreached_nums = set(all_pole_by_num.keys()) - reached
            while unreached_nums:
                best_pair_poles: tuple[dict[str, Any], dict[str, Any]] | None = None
                best_dist = float("inf")
                for u_num in unreached_nums:
                    u_p = all_pole_by_num[u_num]
                    ux = float(u_p["position"]["x"])
                    uy = float(u_p["position"]["y"])
                    for v_num in reached:
                        v_p = all_pole_by_num[v_num]
                        vx = float(v_p["position"]["x"])
                        vy = float(v_p["position"]["y"])
                        d = math.hypot(ux - vx, uy - vy)
                        if d < best_dist:
                            best_dist = d
                            best_pair_poles = (u_p, v_p)

                if best_pair_poles is None:
                    break
                u_p, v_p = best_pair_poles
                ux, uy = float(u_p["position"]["x"]), float(u_p["position"]["y"])
                vx, vy = float(v_p["position"]["x"]), float(v_p["position"]["y"])
                cur_x, cur_y = vx, vy
                prev_num = int(v_p["entity_number"])
                max_final_reach = (
                    30.0
                    if (
                        v_p.get("name") == "big-electric-pole"
                        and u_p.get("name") == "big-electric-pole"
                    )
                    else 9.0
                )
                while math.hypot(ux - cur_x, uy - cur_y) > (
                    max_final_reach if prev_num == int(v_p["entity_number"]) else 9.0
                ):
                    best_cand: tuple[float, float] | None = None
                    best_rem = float("inf")
                    base_ix = math.floor(cur_x)
                    base_iy = math.floor(cur_y)
                    for dx in range(-9, 10):
                        for dy in range(-9, 10):
                            cand_pt = (round(base_ix + dx + 0.5, 2), round(base_iy + dy + 0.5))
                            if cand_pt in occupied_tiles:
                                continue
                            step_d = math.hypot(cand_pt[0] - cur_x, cand_pt[1] - cur_y)
                            if step_d < 1.0 or step_d > 9.0:
                                continue
                            rem_d = math.hypot(ux - cand_pt[0], uy - cand_pt[1])
                            if rem_d < best_rem:
                                best_rem = rem_d
                                best_cand = cand_pt
                    if best_cand is None:
                        break
                    occupied_tiles.add(best_cand)
                    new_num = len(entities) + 1
                    bridge_pole = {
                        "name": "medium-electric-pole",
                        "position": {"x": best_cand[0], "y": best_cand[1]},
                        "entity_number": new_num,
                    }
                    entities.append(bridge_pole)
                    all_pole_by_num[new_num] = bridge_pole
                    reached.add(new_num)
                    wires.append([min(prev_num, new_num), 5, max(prev_num, new_num), 5])
                    prev_num = new_num
                    cur_x, cur_y = best_cand[0], best_cand[1]

                u_num = int(u_p["entity_number"])
                wires.append([min(prev_num, u_num), 5, max(prev_num, u_num), 5])
                copper_adj.setdefault(prev_num, set()).add(u_num)
                copper_adj.setdefault(u_num, set()).add(prev_num)
                bfs_q = [u_num]
                reached.add(u_num)
                while bfs_q:
                    c_id = bfs_q.pop()
                    for n_id in copper_adj.get(c_id, ()):
                        if n_id not in reached:
                            reached.add(n_id)
                            bfs_q.append(n_id)
                unreached_nums -= reached

    # 8. Physical Design Rule Check (DRC)
    def _net_fluid_comm(n: str) -> str | None:
        c = n.split("__")[0]
        if c in placement.fluids:
            return c
        c_dash = c.replace("_", "-")
        if c_dash in placement.fluids:
            return c_dash
        return None

    remaining_conflicts = [
        pos
        for pos, nets in routes.tile_to_nets.items()
        if (
            len(nets) > 1
            and not (
                all(_net_fluid_comm(n) is not None for n in nets)
                and len({_net_fluid_comm(n) for n in nets}) == 1
            )
        )
        or pos in routes.adjacent_fluid_conflicts
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
    if def_data.get("_macro_floorplan") is not None:
        violations.extend(
            drc.check_chassis_matrix_discipline(
                entities=entities,
                macro_floorplan=def_data["_macro_floorplan"],
                router_routes=getattr(placement, "router_routes", None),
                highway_specs=def_data.get("_highway_specs"),
                def_data=def_data,
                netlist_data=netlist_data,
                bay_blocks=def_data.get("_bay_blocks"),
            )
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
    return blueprint.encode(entities, label, wires=wires, tiles=def_data.get("_chassis_tiles"))


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

    tiles: list[dict[str, Any]] | None = def_data.get("_chassis_tiles")

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
