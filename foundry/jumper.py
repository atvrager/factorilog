"""Routing track rasterization and underground jumper resolution.

DEPRECATION NOTICE:
This module is deprecated.
Use foundry.router.maze_router instead.
DualLaneMazeRouter provides full 3D multi-layer maze routing with
dynamic prototypes and native simulation oracle verification.
"""

from __future__ import annotations

import collections
import heapq
import math
import warnings
from dataclasses import dataclass
from typing import Any

warnings.warn(
    "foundry.jumper is deprecated; use foundry.router.maze_router instead",
    DeprecationWarning,
    stacklevel=2,
)

try:
    from foundry import pdk
    from foundry.entity_helpers import (
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        OPPOSITE_DIR,
    )
    from foundry.lef import get_macro_port_geometry
    from foundry.placement import PlacementResult
except ImportError:
    import pdk
    from entity_helpers import (
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        OPPOSITE_DIR,
    )
    from lef import get_macro_port_geometry
    from placement import PlacementResult

DEFAULT_UNDERGROUND_SPANS: dict[str, int] = {
    "underground-belt": 5,
    "fast-underground-belt": 7,
    "express-underground-belt": 9,
    "pipe-to-ground": 10,
}


def get_layer_roles(num_tiers: int = 3) -> tuple[frozenset[str], frozenset[str]]:
    red, green = pdk.circuit_layers(num_tiers)
    max_ug = 2 * max(1, num_tiers) + 2
    ug_layers = frozenset(f"M{i}" for i in range(3, max_ug + 1))
    circuit_layers = frozenset({red, green})
    return ug_layers, circuit_layers


ALL_UG_LAYERS, CIRCUIT_LAYERS = get_layer_roles(3)


@dataclass
class RouteResult:
    """Surface routes, conflict tiles, and pin locations."""

    net_surface_tiles: dict[str, dict[tuple[float, float], int]]
    tile_to_nets: dict[tuple[float, float], set[str]]
    adjacent_fluid_conflicts: set[tuple[float, float]]
    pin_tiles: set[tuple[float, float]]
    valid_input_pins: set[tuple[float, float]]
    valid_output_pins: set[tuple[float, float]]


def route_and_resolve_jumpers(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
    placement: PlacementResult,
    debug_trace: dict[tuple[float, float], list[dict[str, Any]]] | None = None,
) -> RouteResult:
    """Rasterize DEF routes and resolve surface crossings with underground jumpers."""
    scale = 1.0 / float(def_data.get("units", 1000))
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})

    entities = placement.entities
    occupied_positions = placement.occupied_positions
    grid_entities = placement.grid_entities
    fluids = placement.fluids
    has_fluid_metadata = placement.has_fluid_metadata

    def add_entity(
        name: str,
        x: float,
        y: float,
        direction: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pos_key = (round(x, 2), round(y, 2))
        if pos_key in occupied_positions:
            return grid_entities.get(pos_key, {})
        occupied_positions.add(pos_key)
        entity: dict[str, Any] = {
            "name": name,
            "position": {"x": pos_key[0], "y": pos_key[1]},
        }
        if direction is not None:
            entity["direction"] = direction
        if extra:
            entity.update(extra)
        entities.append(entity)
        grid_entities[pos_key] = entity
        return entity

    def is_fluid_wire(net: dict[str, Any], layer: str) -> bool:
        if has_fluid_metadata:
            net_name = net.get("name", "")
            base = net_name.split("__")[0].replace("_", "-")
            return base in fluids
        return layer in ("M3", "M4")

    tier_spans = dict(DEFAULT_UNDERGROUND_SPANS)
    if netlist_data and "underground_spans" in netlist_data:
        tier_spans.update(netlist_data["underground_spans"])

    def choose_belt_tier(span_tiles: int) -> str:
        for name, max_span in sorted(tier_spans.items(), key=lambda kv: kv[1]):
            if span_tiles <= max_span:
                return name
        return max(tier_spans.items(), key=lambda kv: kv[1])[0]

    def segment_run(
        start: float,
        end: float,
        max_allowed_span: int,
        axis_coord: float = 0.0,
        is_horizontal: bool = True,
        nname: str = "",
        blocked_tiles: set[tuple[float, float]] | None = None,
    ) -> list[tuple[float, float, int]]:
        length = round(abs(end - start))
        step = 1 if end > start else -1
        if length <= max_allowed_span:
            return [(start, end, length)]
        blocked = blocked_tiles
        hops = []
        curr = start
        while True:
            rem = round(abs(end - curr))
            if rem <= max_allowed_span:
                hops.append((curr, end, rem))
                break
            best_span = None
            for span in range(min(max_allowed_span, rem - 2), 0, -1):
                exit_pos = round(curr + step * span, 2)
                next_in = round(exit_pos + step, 2)
                p_exit = (exit_pos, axis_coord) if is_horizontal else (axis_coord, exit_pos)
                p_next = (next_in, axis_coord) if is_horizontal else (axis_coord, next_in)
                if blocked is not None:
                    exit_blocked = p_exit in blocked
                    next_blocked = p_next in blocked
                else:
                    exit_blocked = any(n != nname for n in tile_to_nets.get(p_exit, ()))
                    next_blocked = any(n != nname for n in tile_to_nets.get(p_next, ()))
                if not exit_blocked and not next_blocked:
                    best_span = span
                    break
            if best_span is None:
                best_span = min(max_allowed_span, rem - 2)
                if best_span < 1:
                    best_span = max_allowed_span
            exit_pos = round(curr + step * best_span, 2)
            hops.append((curr, exit_pos, best_span))
            curr = round(exit_pos + step, 2)
        return hops

    def get_pin_offset(cell_id: str, pin: str) -> tuple[float, float]:
        if cell_id == "belt_merge2":
            merge_pins = {
                "a": (0.25, 0.5),
                "b": (0.25, 1.5),
                "y": (0.75, 0.5),
            }
            return merge_pins.get(pin, (0.5, 0.5))
        if cell_id == "belt_split2":
            split_pins = {
                "a": (0.25, 0.5),
                "b": (0.25, 1.5),
                "y1": (0.75, 0.5),
                "y2": (0.75, 1.5),
            }
            return split_pins.get(pin, (0.5, 0.5))
        if cell_id == "belt_balancer2":
            balancer_pins = {
                "a1": (0.25, 0.5),
                "a2": (0.25, 1.5),
                "y1": (0.75, 0.5),
                "y2": (0.75, 1.5),
            }
            return balancer_pins.get(pin, (0.5, 0.5))
        if cell_id == "pump":
            pump_pins = {
                "a": (0.5, 0.25),
                "y": (0.5, 1.75),
            }
            return pump_pins.get(pin, (0.5, 1.0))
        c = cell_map.get(cell_id, {})
        width = float(c.get("width", 3))
        height = float(c.get("height", 3))
        outputs = [o.replace("-", "_") for o in c.get("outputs", [])]
        if pin in outputs:
            orig_outputs = c.get("outputs", [])
            idx = outputs.index(pin)
            orig_name = orig_outputs[idx] if idx < len(orig_outputs) else ""
            if orig_name:
                geom = get_macro_port_geometry(c, orig_name, is_output=True)
                r = geom["rect"]
                return ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0)
            return (width - 0.25, height / 2.0)
        return (width / 2.0, height / 2.0)

    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    input_pins: dict[str, tuple[float, float]] = {}
    for p in def_data.get("pins", []):
        if p.get("direction") == "INPUT":
            input_pins[p["net"]] = (p["x"] * scale, p["y"] * scale)

    pin_tiles: set[tuple[float, float]] = set()
    for p in def_data.get("pins", []):
        pin_tiles.add(
            (
                round(math.floor(p["x"] * scale) + 0.5, 2),
                round(math.floor(p["y"] * scale) + 0.5, 2),
            )
        )
    connected_pins: set[tuple[str, str]] = set()
    for net in def_data.get("nets", []):
        for inst, pin in net.get("connections", []):
            connected_pins.add((inst, pin))

    for comp in def_data.get("components", []):
        cname = comp["name"]
        macro = comp["macro"]
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", macro)
        cell = cell_map.get(cell_id, {})
        if cell_id == "belt_merge2":
            pin_tiles.add((round(gx - 0.5, 2), round(gy + 0.5, 2)))
            pin_tiles.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            pin_tiles.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
        elif cell_id == "belt_split2":
            if (cname, "b") in connected_pins:
                pin_tiles.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            if (cname, "a") in connected_pins or (cname, "b") not in connected_pins:
                pin_tiles.add((round(gx - 0.5, 2), round(gy + 0.5, 2)))
            pin_tiles.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
            pin_tiles.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))
        elif cell_id == "belt_balancer2":
            pin_tiles.add((round(gx - 0.5, 2), round(gy + 0.5, 2)))
            pin_tiles.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            pin_tiles.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
            pin_tiles.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))
        elif cell_id == "pump":
            pin_tiles.add((round(gx + 0.5, 2), round(gy - 0.5, 2)))
            pin_tiles.add((round(gx + 0.5, 2), round(gy + 2.5, 2)))
        else:
            inputs = cell.get("inputs", [])
            outputs = cell.get("outputs", [])
            for port in inputs:
                geom = get_macro_port_geometry(cell, port, is_output=False)
                ix, iy = geom["inserter_pos"]
                ax, ay = geom["approach_pos"]
                pin_tiles.add((round(gx + ix, 2), round(gy + iy, 2)))
                pin_tiles.add((round(gx + ax, 2), round(gy + ay, 2)))
            for port in outputs:
                geom = get_macro_port_geometry(cell, port, is_output=True)
                ix, iy = geom["inserter_pos"]
                ax, ay = geom["approach_pos"]
                pin_tiles.add((round(gx + ix, 2), round(gy + iy, 2)))
                pin_tiles.add((round(gx + ax, 2), round(gy + ay, 2)))

    net_dists: dict[str, dict[tuple[float, float], float]] = {}
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        wires = net.get("wires", [])
        if not wires:
            continue
        graph = collections.defaultdict(list)
        for w in wires:
            p1 = (round(w["x1"] * scale, 2), round(w["y1"] * scale, 2))
            p2 = (round(w["x2"] * scale, 2), round(w["y2"] * scale, 2))
            d = abs(p2[0] - p1[0]) + abs(p2[1] - p1[1])
            graph[p1].append((p2, d))
            graph[p2].append((p1, d))

        drivers: list[tuple[float, float]] = []
        if nname in input_pins:
            drivers.append(input_pins[nname])
        for inst, pin in net.get("connections", []):
            if inst in comp_map:
                c = comp_map[inst]
                cell_id = inst_map.get(inst, {}).get("cell", c.get("macro", ""))
                if cell_id in ("belt_merge2", "pump"):
                    outputs = ["y"]
                elif cell_id in ("belt_split2", "belt_balancer2"):
                    outputs = ["y1", "y2"]
                else:
                    outputs = [
                        o.replace("-", "_") for o in cell_map.get(cell_id, {}).get("outputs", [])
                    ]
                if pin in outputs:
                    po = get_pin_offset(cell_id, pin)
                    drivers.append((c["x"] * scale + po[0], c["y"] * scale + po[1]))

        if not drivers:
            drivers = [next(iter(graph.keys()))]

        dist: dict[tuple[float, float], float] = {}
        pq: list[tuple[float, tuple[float, float]]] = []
        for drv in drivers:
            closest = min(graph.keys(), key=lambda p: abs(p[0] - drv[0]) + abs(p[1] - drv[1]))
            init_d = abs(closest[0] - drv[0]) + abs(closest[1] - drv[1])
            dist[closest] = init_d
            heapq.heappush(pq, (init_d, closest))

        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, 1e9):
                continue
            for v, weight in graph[u]:
                if d + weight < dist.get(v, 1e9):
                    dist[v] = d + weight
                    heapq.heappush(pq, (d + weight, v))
        net_dists[nname] = dist

    perp_conn: dict[tuple[str, tuple[float, float]], set[str]] = collections.defaultdict(set)
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        for w in net.get("wires", []):
            if w.get("layer") in CIRCUIT_LAYERS:
                continue
            p1 = (
                round(math.floor(w["x1"] * scale) + 0.5, 2),
                round(math.floor(w["y1"] * scale) + 0.5, 2),
            )
            p2 = (
                round(math.floor(w["x2"] * scale) + 0.5, 2),
                round(math.floor(w["y2"] * scale) + 0.5, 2),
            )
            axis = "H" if p1[1] == p2[1] else "V"
            perp_conn[(nname, p1)].add(axis)
            perp_conn[(nname, p2)].add(axis)

    net_surface_tiles: dict[str, dict[tuple[float, float], int]] = collections.defaultdict(dict)
    tile_to_nets: dict[tuple[float, float], set[str]] = collections.defaultdict(set)
    underground_entities: list[dict[str, Any]] = []

    m3_runs_by_net: dict[str, dict[float, list[tuple[float, float, int]]]] = (
        collections.defaultdict(dict)
    )
    m4_runs_by_net: dict[str, dict[float, list[tuple[float, float, int]]]] = (
        collections.defaultdict(dict)
    )

    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        dist = net_dists.get(nname, {})

        for wire in net.get("wires", []):
            layer = wire["layer"]
            x1, y1 = wire["x1"], wire["y1"]
            x2, y2 = wire["x2"], wire["y2"]
            if (x1, y1) == (x2, y2):
                continue

            p1 = (round(x1 * scale, 2), round(y1 * scale, 2))
            p2 = (round(x2 * scale, 2), round(y2 * scale, 2))
            from_p, to_p = (p1, p2) if dist.get(p1, 0) <= dist.get(p2, 0) else (p2, p1)

            t1_x = math.floor(from_p[0]) + 0.5
            t1_y = math.floor(from_p[1]) + 0.5
            t2_x = math.floor(to_p[0]) + 0.5
            t2_y = math.floor(to_p[1]) + 0.5

            if from_p[0] == to_p[0]:
                d = DIRECTION_SOUTH if to_p[1] >= from_p[1] else DIRECTION_NORTH
            else:
                d = DIRECTION_EAST if to_p[0] >= from_p[0] else DIRECTION_WEST

            if layer in CIRCUIT_LAYERS:
                continue

            if layer in ALL_UG_LAYERS:
                if from_p[0] == to_p[0]:
                    dx, dy = (0, 1) if d == DIRECTION_SOUTH else (0, -1)
                    while (t1_x, t1_y) in occupied_positions:
                        t1_y = round(t1_y + dy, 2)
                    while (t2_x, t2_y) in occupied_positions:
                        t2_y = round(t2_y - dy, 2)
                    if (t2_y - t1_y) * dy >= 1:
                        m4_runs_by_net[nname].setdefault(t1_x, []).append((t1_y, t2_y, d))
                    elif (t1_x, t1_y) == (t2_x, t2_y) and (t1_x, t1_y) not in occupied_positions:
                        net_surface_tiles[nname][(t1_x, t1_y)] = d
                        tile_to_nets[(t1_x, t1_y)].add(nname)
                else:
                    dx, dy = (1, 0) if d == DIRECTION_EAST else (-1, 0)
                    while (t1_x, t1_y) in occupied_positions:
                        t1_x = round(t1_x + dx, 2)
                    while (t2_x, t2_y) in occupied_positions:
                        t2_x = round(t2_x - dx, 2)
                    if (t2_x - t1_x) * dx >= 1:
                        m3_runs_by_net[nname].setdefault(t1_y, []).append((t1_x, t2_x, d))
                    elif (t1_x, t1_y) == (t2_x, t2_y) and (t1_x, t1_y) not in occupied_positions:
                        net_surface_tiles[nname][(t1_x, t1_y)] = d
                        tile_to_nets[(t1_x, t1_y)].add(nname)
            else:
                if from_p[0] == to_p[0]:
                    tx = t1_x
                    y_start = math.floor(from_p[1])
                    y_end = math.floor(to_p[1])
                    step = 1 if y_end >= y_start else -1
                    for ty in range(y_start, y_end + step, step):
                        pos = (tx, ty + 0.5)
                        if pos in occupied_positions:
                            continue
                        tile_to_nets[pos].add(nname)
                        if ty != y_end or pos not in net_surface_tiles[nname]:
                            net_surface_tiles[nname][pos] = d
                else:
                    ty = t1_y
                    x_start = math.floor(from_p[0])
                    x_end = math.floor(to_p[0])
                    step = 1 if x_end >= x_start else -1
                    for tx in range(x_start, x_end + step, step):
                        pos = (tx + 0.5, ty)
                        if pos in occupied_positions:
                            continue
                        tile_to_nets[pos].add(nname)
                        if tx != x_end or pos not in net_surface_tiles[nname]:
                            net_surface_tiles[nname][pos] = d

    # Process M3 and M4 runs with obstacle-aware segmentation
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        is_fluid = is_fluid_wire(net, "M3")

        for cy, segs in m3_runs_by_net.get(nname, {}).items():
            for s1, s2, d in segs:
                if s1 == s2:
                    continue
                step = 1 if s2 >= s1 else -1
                start_x, end_x = s1, s2
                ug_start = round(start_x, 2)
                ug_end = round(end_x, 2)
                start_is_turn = "V" in perp_conn.get((nname, (start_x, cy)), set())
                end_is_turn = "V" in perp_conn.get((nname, (end_x, cy)), set())

                if start_is_turn:
                    cand_start = round(start_x + step, 2)
                    if (cand_start, cy) not in occupied_positions and len(
                        tile_to_nets.get((cand_start, cy), set())
                    ) == 0:
                        net_surface_tiles[nname][(start_x, cy)] = d
                        tile_to_nets[(start_x, cy)].add(nname)
                        ug_start = cand_start

                if end_is_turn:
                    cand_end = round(end_x - step, 2)
                    if (cand_end, cy) not in occupied_positions and len(
                        tile_to_nets.get((cand_end, cy), set())
                    ) == 0:
                        if (end_x, cy) not in occupied_positions:
                            if (end_x, cy) not in net_surface_tiles[nname]:
                                net_surface_tiles[nname][(end_x, cy)] = d
                            tile_to_nets[(end_x, cy)].add(nname)
                        ug_end = cand_end

                if not is_fluid:
                    while (ug_start, cy) in pin_tiles and (ug_end - ug_start) * step >= 0:
                        net_surface_tiles[nname][(ug_start, cy)] = d
                        tile_to_nets[(ug_start, cy)].add(nname)
                        ug_start = round(ug_start + step, 2)

                    while (ug_end, cy) in pin_tiles and (ug_end - ug_start) * step >= 0:
                        net_surface_tiles[nname][(ug_end, cy)] = d
                        tile_to_nets[(ug_end, cy)].add(nname)
                        ug_end = round(ug_end - step, 2)

                if start_is_turn and ug_start == start_x:
                    ug_end = ug_start
                if end_is_turn and ug_end == end_x:
                    ug_end = ug_start

                min_span = 1 if is_fluid else 2
                if (ug_end - ug_start) * step < min_span:
                    cur = ug_start
                    while (ug_end - cur) * step >= 0:
                        net_surface_tiles[nname][(cur, cy)] = d
                        tile_to_nets[(cur, cy)].add(nname)
                        cur = round(cur + step, 2)
                else:
                    max_pipe_span = tier_spans.get("pipe-to-ground", 10)
                    max_belt_span = max(
                        (v for k, v in tier_spans.items() if "belt" in k), default=5
                    )
                    max_allowed_span = max_pipe_span if is_fluid else max_belt_span
                    hops = segment_run(ug_start, ug_end, max_allowed_span, cy, True, nname=nname)
                    for h_start, h_end, h_span in hops:
                        tile_to_nets[(h_start, cy)].add(nname)
                        tile_to_nets[(h_end, cy)].add(nname)
                        if is_fluid:
                            d_in = DIRECTION_WEST if d == DIRECTION_EAST else DIRECTION_EAST
                            d_out = DIRECTION_EAST if d == DIRECTION_EAST else DIRECTION_WEST
                            underground_entities.append(
                                {
                                    "name": "pipe-to-ground",
                                    "position": {"x": h_start, "y": cy},
                                    "direction": d_in,
                                }
                            )
                            underground_entities.append(
                                {
                                    "name": "pipe-to-ground",
                                    "position": {"x": h_end, "y": cy},
                                    "direction": d_out,
                                }
                            )
                        else:
                            ent_name = choose_belt_tier(h_span)
                            underground_entities.append(
                                {
                                    "name": ent_name,
                                    "position": {"x": h_start, "y": cy},
                                    "direction": d,
                                    "type": "input",
                                }
                            )
                            underground_entities.append(
                                {
                                    "name": ent_name,
                                    "position": {"x": h_end, "y": cy},
                                    "direction": d,
                                    "type": "output",
                                }
                            )

        for cx, segs in m4_runs_by_net.get(nname, {}).items():
            for s1, s2, d in segs:
                if s1 == s2:
                    continue
                step = 1 if s2 >= s1 else -1
                start_y, end_y = s1, s2
                ug_start = round(start_y, 2)
                ug_end = round(end_y, 2)
                start_is_turn = "H" in perp_conn.get((nname, (cx, start_y)), set())
                end_is_turn = "H" in perp_conn.get((nname, (cx, end_y)), set())

                if start_is_turn:
                    cand_start = round(start_y + step, 2)
                    if (cx, cand_start) not in occupied_positions and len(
                        tile_to_nets.get((cx, cand_start), set())
                    ) == 0:
                        net_surface_tiles[nname][(cx, start_y)] = d
                        tile_to_nets[(cx, start_y)].add(nname)
                        ug_start = cand_start

                if end_is_turn:
                    cand_end = round(end_y - step, 2)
                    if (cx, cand_end) not in occupied_positions and len(
                        tile_to_nets.get((cx, cand_end), set())
                    ) == 0:
                        if (cx, end_y) not in occupied_positions:
                            if (cx, end_y) not in net_surface_tiles[nname]:
                                net_surface_tiles[nname][(cx, end_y)] = d
                            tile_to_nets[(cx, end_y)].add(nname)
                        ug_end = cand_end

                if not is_fluid:
                    while (cx, ug_start) in pin_tiles and (ug_end - ug_start) * step >= 0:
                        net_surface_tiles[nname][(cx, ug_start)] = d
                        tile_to_nets[(cx, ug_start)].add(nname)
                        ug_start = round(ug_start + step, 2)

                    while (cx, ug_end) in pin_tiles and (ug_end - ug_start) * step >= 0:
                        net_surface_tiles[nname][(cx, ug_end)] = d
                        tile_to_nets[(cx, ug_end)].add(nname)
                        ug_end = round(ug_end - step, 2)

                if start_is_turn and ug_start == start_y:
                    ug_end = ug_start
                if end_is_turn and ug_end == end_y:
                    ug_end = ug_start

                min_span = 1 if is_fluid else 2
                if (ug_end - ug_start) * step < min_span:
                    cur = ug_start
                    while (ug_end - cur) * step >= 0:
                        net_surface_tiles[nname][(cx, cur)] = d
                        tile_to_nets[(cx, cur)].add(nname)
                        cur = round(cur + step, 2)
                else:
                    max_pipe_span = tier_spans.get("pipe-to-ground", 10)
                    max_belt_span = max(
                        (v for k, v in tier_spans.items() if "belt" in k), default=5
                    )
                    max_allowed_span = max_pipe_span if is_fluid else max_belt_span
                    hops = segment_run(ug_start, ug_end, max_allowed_span, cx, False, nname=nname)
                    for h_start, h_end, h_span in hops:
                        tile_to_nets[(cx, h_start)].add(nname)
                        tile_to_nets[(cx, h_end)].add(nname)
                        if is_fluid:
                            d_in = DIRECTION_NORTH if d == DIRECTION_SOUTH else DIRECTION_SOUTH
                            d_out = DIRECTION_SOUTH if d == DIRECTION_SOUTH else DIRECTION_NORTH
                            underground_entities.append(
                                {
                                    "name": "pipe-to-ground",
                                    "position": {"x": cx, "y": h_start},
                                    "direction": d_in,
                                }
                            )
                            underground_entities.append(
                                {
                                    "name": "pipe-to-ground",
                                    "position": {"x": cx, "y": h_end},
                                    "direction": d_out,
                                }
                            )
                        else:
                            ent_name = choose_belt_tier(h_span)
                            underground_entities.append(
                                {
                                    "name": ent_name,
                                    "position": {"x": cx, "y": h_start},
                                    "direction": d,
                                    "type": "input",
                                }
                            )
                            underground_entities.append(
                                {
                                    "name": ent_name,
                                    "position": {"x": cx, "y": h_end},
                                    "direction": d,
                                    "type": "output",
                                }
                            )

    net_map = {n.get("name", ""): n for n in def_data.get("nets", [])}
    jumpers: list[tuple[str, tuple[float, float], tuple[float, float], int, bool]] = []
    used_underground_endpoints: dict[tuple[float, float], int] = {}
    used_underground_spans_h: set[tuple[float, float]] = set()
    used_underground_spans_v: set[tuple[float, float]] = set()
    for ue in underground_entities:
        pos_u = (round(ue["position"]["x"], 2), round(ue["position"]["y"], 2))
        used_underground_endpoints[pos_u] = ue.get("direction", 0)

    ug_ins = [
        ue
        for ue in underground_entities
        if ue.get("type") == "input" or ue.get("name") == "pipe-to-ground"
    ]
    ug_outs = [
        ue
        for ue in underground_entities
        if ue.get("type") == "output" or ue.get("name") == "pipe-to-ground"
    ]
    ug_outs_by_pos = {
        (round(ue["position"]["x"], 2), round(ue["position"]["y"], 2)): ue for ue in ug_outs
    }
    for u_in in ug_ins:
        d = u_in.get("direction", 0)
        is_pipe = u_in.get("name") == "pipe-to-ground"
        ug_d = OPPOSITE_DIR.get(d, d) if is_pipe else d
        if ug_d not in DIR_STEPS:
            continue
        dx, dy = DIR_STEPS[ug_d]
        ix = round(u_in["position"]["x"], 2)
        iy = round(u_in["position"]["y"], 2)
        max_s = tier_spans.get(u_in.get("name", "pipe-to-ground"), 10) if is_pipe else 10
        expected_out_dir = ug_d if is_pipe else d
        for s in range(1, max_s + 1):
            chk_pos = (round(ix + dx * s, 2), round(iy + dy * s, 2))
            u_out = ug_outs_by_pos.get(chk_pos)
            if u_out is not None and u_out.get("direction", 0) == expected_out_dir:
                span_set = (
                    used_underground_spans_h
                    if ug_d in (DIRECTION_EAST, DIRECTION_WEST)
                    else used_underground_spans_v
                )
                for step_s in range(1, s):
                    span_set.add((round(ix + dx * step_s, 2), round(iy + dy * step_s, 2)))
                break

    valid_output_pins: set[tuple[float, float]] = set()
    valid_input_pins: set[tuple[float, float]] = set()
    valid_fluid_output_pins: set[tuple[float, float]] = set()
    valid_fluid_input_pins: set[tuple[float, float]] = set()
    machine_input_pins: set[tuple[float, float]] = set()
    machine_output_pins: set[tuple[float, float]] = set()
    for comp in def_data.get("components", []):
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(comp["name"], {})
        cell_id = inst.get("cell", comp["macro"])
        cell = cell_map.get(cell_id, {})
        if cell_id == "belt_merge2":
            valid_input_pins.add((round(gx - 0.5, 2), round(gy + 0.5, 2)))
            valid_input_pins.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            valid_output_pins.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
        elif cell_id == "belt_split2":
            if (comp["name"], "b") in connected_pins:
                valid_input_pins.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            if (comp["name"], "a") in connected_pins or (comp["name"], "b") not in connected_pins:
                valid_input_pins.add((round(gx - 0.5, 2), round(gy + 0.5, 2)))
            valid_output_pins.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
            valid_output_pins.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))
        elif cell_id == "belt_split4":
            valid_input_pins.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            for ty in range(4):
                valid_output_pins.add((round(gx + 2.5, 2), round(gy + ty + 0.5, 2)))
        elif cell_id == "belt_balancer2":
            valid_input_pins.add((round(gx - 0.5, 2), round(gy + 0.5, 2)))
            valid_input_pins.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            valid_output_pins.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
            valid_output_pins.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))
        elif cell_id == "pump":
            valid_fluid_input_pins.add((round(gx + 0.5, 2), round(gy - 0.5, 2)))
            valid_fluid_output_pins.add((round(gx + 0.5, 2), round(gy + 2.5, 2)))
        else:
            for port in cell.get("inputs", []):
                geom = get_macro_port_geometry(cell, port, is_output=False)
                ax, ay = geom["approach_pos"]
                pt = (round(gx + ax, 2), round(gy + ay, 2))
                if port in fluids:
                    valid_fluid_input_pins.add(pt)
                else:
                    valid_input_pins.add(pt)
                machine_input_pins.add(pt)
            for port in cell.get("outputs", []):
                geom = get_macro_port_geometry(cell, port, is_output=True)
                ax, ay = geom["approach_pos"]
                pt = (round(gx + ax, 2), round(gy + ay, 2))
                if port in fluids:
                    valid_fluid_output_pins.add(pt)
                else:
                    valid_output_pins.add(pt)
                machine_output_pins.add(pt)

    splitter_feeds: dict[tuple[float, float], set[int]] = collections.defaultdict(set)
    for comp in def_data.get("components", []):
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(comp["name"], {})
        cell_id = inst.get("cell", comp["macro"])
        if cell_id in ("belt_split2", "belt_merge2", "belt_balancer2"):
            splitter_feeds[(round(gx + 1.5, 2), round(gy + 0.5, 2))].add(DIRECTION_EAST)
            splitter_feeds[(round(gx + 1.5, 2), round(gy + 1.5, 2))].add(DIRECTION_EAST)
        elif cell_id == "belt_split4":
            for ty in range(4):
                splitter_feeds[(round(gx + 2.5, 2), round(gy + ty + 0.5, 2))].add(DIRECTION_EAST)

    fluid_nets: set[str] = {
        n.get("name", "") for n in def_data.get("nets", []) if is_fluid_wire(n, "M1")
    }

    def check_adj_fluid(pos: tuple[float, float]) -> bool:
        nets = tile_to_nets.get(pos)
        if not nets or len(nets) != 1:
            return False
        n1 = next(iter(nets))
        if n1 not in fluid_nets:
            return False
        cx, cy = pos
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            npos = (round(cx + dx, 2), round(cy + dy, 2))
            adj_nets = tile_to_nets.get(npos)
            if adj_nets and any(n2 != n1 and n2 in fluid_nets for n2 in adj_nets):
                return True
        return False

    multi_net_conflicts: set[tuple[float, float]] = {
        pos for pos, nets in tile_to_nets.items() if len(nets) > 1
    }
    adjacent_fluid_conflicts: set[tuple[float, float]] = set()
    for pos, nets in tile_to_nets.items():
        if len(nets) == 1 and next(iter(nets)) in fluid_nets and check_adj_fluid(pos):
            adjacent_fluid_conflicts.add(pos)

    def update_tile_conflict(pos: tuple[float, float]) -> None:
        nets = tile_to_nets.get(pos)
        if nets and len(nets) > 1:
            multi_net_conflicts.add(pos)
        else:
            multi_net_conflicts.discard(pos)
        cx, cy = pos
        check_tiles = (
            pos,
            (round(cx + 1, 2), cy),
            (round(cx - 1, 2), cy),
            (round(cx, 2), round(cy + 1, 2)),
            (round(cx, 2), round(cy - 1, 2)),
        )
        for p in check_tiles:
            if check_adj_fluid(p):
                adjacent_fluid_conflicts.add(p)
            else:
                adjacent_fluid_conflicts.discard(p)

    def evaluate_options(pos: tuple[float, float]) -> list[tuple[float, str, Any]]:
        nets = tile_to_nets.get(pos)
        if not nets:
            return []
        if len(nets) <= 1 and pos not in adjacent_fluid_conflicts:
            return []
        cx, cy = pos
        net_list = list(nets)
        options: list[tuple[float, str, Any]] = []

        for candidate in net_list:
            is_fluid = candidate in fluid_nets
            d = net_surface_tiles[candidate].get(pos)
            if d not in DIR_STEPS:
                continue
            dx, dy = DIR_STEPS[d]
            opp_d = OPPOSITE_DIR[d]
            max_belt_span = max((v for k, v in tier_spans.items() if "belt" in k), default=5)
            max_pipe_span = tier_spans.get("pipe-to-ground", 10)
            max_allowed_span = max_pipe_span if is_fluid else max_belt_span

            for bspan in range(1, max_allowed_span):
                in_pos = (round(cx - dx * bspan, 2), round(cy - dy * bspan, 2))
                prev_pos = (round(in_pos[0] - dx, 2), round(in_pos[1] - dy, 2))
                fail_in = []
                if in_pos not in net_surface_tiles[candidate]:
                    fail_in.append("not_in_net")
                elif net_surface_tiles[candidate][in_pos] != d and not (
                    in_pos in machine_output_pins
                    and d in (DIRECTION_SOUTH, DIRECTION_NORTH, DIRECTION_EAST)
                ):
                    fail_in.append(f"dir_mismatch({net_surface_tiles[candidate][in_pos]}!={d})")
                if len(tile_to_nets.get(in_pos, set())) > 1:
                    fail_in.append(f"in_pos_has_conflict({tile_to_nets.get(in_pos)})")
                if in_pos in used_underground_endpoints:
                    fail_in.append("used_endpoint")
                if in_pos in used_underground_spans_h or in_pos in used_underground_spans_v:
                    fail_in.append("in_underground_span")
                allowed_out = valid_fluid_output_pins if is_fluid else valid_output_pins
                if in_pos in pin_tiles:
                    allowed = in_pos in allowed_out and (
                        d == DIRECTION_EAST
                        or (
                            not is_fluid
                            and in_pos in machine_output_pins
                            and d in (DIRECTION_SOUTH, DIRECTION_NORTH)
                        )
                    )
                    if not allowed:
                        fail_in.append("in_pin_tiles")
                if any(s_dir != d for s_dir in splitter_feeds.get(in_pos, set())):
                    fail_in.append("flank_feed_from_splitter")
                side_dirs = (
                    (DIRECTION_EAST, DIRECTION_WEST)
                    if d in (DIRECTION_NORTH, DIRECTION_SOUTH)
                    else (DIRECTION_NORTH, DIRECTION_SOUTH)
                )
                for s_dir in side_dirs:
                    sdx, sdy = DIR_STEPS[s_dir]
                    nb_pos = (round(in_pos[0] - sdx, 2), round(in_pos[1] - sdy, 2))
                    nb_nets = tile_to_nets.get(nb_pos)
                    if nb_nets and any(
                        net_surface_tiles[c_net].get(nb_pos) == s_dir
                        for c_net in nb_nets
                        if c_net != candidate
                    ):
                        fail_in.append("perpendicular_belt_feed")
                        break
                    if is_fluid and nb_nets and candidate in nb_nets:
                        fail_in.append("perpendicular_fluid_branch")
                        break
                if in_pos in occupied_positions:
                    fail_in.append("in_occupied")
                if (
                    is_fluid
                    and in_pos not in pin_tiles
                    and prev_pos not in net_surface_tiles[candidate]
                ):
                    fail_in.append("in_prev_not_in_net")
                if used_underground_endpoints.get(prev_pos) == opp_d:
                    fail_in.append("opp_prev")
                if fail_in:
                    if debug_trace is not None:
                        debug_trace.setdefault(pos, []).append(
                            {
                                "candidate": candidate,
                                "dir": d,
                                "bspan": bspan,
                                "fspan": None,
                                "in_pos": in_pos,
                                "out_pos": None,
                                "fail_in": list(fail_in),
                                "fail_out": [],
                                "valid_span": False,
                                "invalid_reasons": [],
                            }
                        )
                    continue

                in_detour = None
                if not is_fluid:
                    side_feed = False
                    in_turn_dir = None
                    for b_dir, (b_dx, b_dy) in DIR_STEPS.items():
                        if b_dir == d:
                            continue
                        n_pos = (round(in_pos[0] - b_dx, 2), round(in_pos[1] - b_dy, 2))
                        if net_surface_tiles[candidate].get(n_pos) == b_dir:
                            side_feed = True
                            in_turn_dir = b_dir
                            break
                    if side_feed:
                        corner_in = (round(n_pos[0] - dx, 2), round(n_pos[1] - dy, 2))
                        entry_tile = prev_pos
                        detour_fails = []
                        if in_turn_dir is None or abs(in_turn_dir - d) % 8 != 4:
                            detour_fails.append(f"turn_dir({in_turn_dir})")
                        if corner_in in occupied_positions:
                            detour_fails.append(f"corner_in_occ({corner_in})")
                        if corner_in in pin_tiles and corner_in not in valid_output_pins:
                            detour_fails.append(f"corner_in_pin({corner_in})")
                        if corner_in in used_underground_endpoints:
                            detour_fails.append(f"corner_in_ug({corner_in})")
                        if not (tile_to_nets.get(corner_in, set()) <= {candidate}):
                            detour_fails.append(f"corner_in_net({tile_to_nets.get(corner_in)})")
                        if entry_tile in occupied_positions:
                            detour_fails.append(f"entry_occ({entry_tile})")
                        if entry_tile in pin_tiles:
                            detour_fails.append(f"entry_pin({entry_tile})")
                        if entry_tile in used_underground_endpoints:
                            detour_fails.append(f"entry_ug({entry_tile})")
                        if not (tile_to_nets.get(entry_tile, set()) <= {candidate}):
                            detour_fails.append(f"entry_net({tile_to_nets.get(entry_tile)})")

                        if not detour_fails:
                            in_detour = (n_pos, opp_d, corner_in, in_turn_dir, entry_tile, d)
                        else:
                            if debug_trace is not None:
                                debug_trace.setdefault(pos, []).append(
                                    {
                                        "candidate": candidate,
                                        "dir": d,
                                        "bspan": bspan,
                                        "fspan": None,
                                        "in_pos": in_pos,
                                        "out_pos": None,
                                        "fail_in": [f"in_detour_blocked:{','.join(detour_fails)}"],
                                        "fail_out": [],
                                        "valid_span": False,
                                        "invalid_reasons": [],
                                    }
                                )
                            continue

                for fspan in range(1, max_allowed_span):
                    if bspan + fspan > max_allowed_span:
                        break
                    out_pos = (round(cx + dx * fspan, 2), round(cy + dy * fspan, 2))
                    next_pos = (round(out_pos[0] + dx, 2), round(out_pos[1] + dy, 2))
                    fail_out = []
                    turn_detour = None
                    if out_pos not in net_surface_tiles[candidate]:
                        fail_out.append("out_not_in_net")
                    elif net_surface_tiles[candidate][out_pos] != d and not (
                        out_pos in machine_input_pins
                        and d in (DIRECTION_SOUTH, DIRECTION_NORTH, DIRECTION_EAST)
                    ):
                        turn_dir = net_surface_tiles[candidate][out_pos]
                        if not is_fluid and turn_dir in DIR_STEPS and abs(turn_dir - d) % 8 == 4:
                            tdx, tdy = DIR_STEPS[turn_dir]
                            exit_tile = (round(out_pos[0] + dx, 2), round(out_pos[1] + dy, 2))
                            corner_tile = (
                                round(exit_tile[0] + tdx, 2),
                                round(exit_tile[1] + tdy, 2),
                            )
                            next_in_net = (
                                round(out_pos[0] + tdx, 2),
                                round(out_pos[1] + tdy, 2),
                            )
                            next_in_net_dir = net_surface_tiles[candidate].get(next_in_net)
                            if next_in_net_dir == d:
                                target_corner_d = d
                                bypass_tile = next_in_net
                                downstream_ok = (
                                    corner_tile in net_surface_tiles[candidate]
                                    or corner_tile in valid_input_pins
                                    or (
                                        round(corner_tile[0] + dx, 2),
                                        round(corner_tile[1] + dy, 2),
                                    )
                                    in net_surface_tiles[candidate]
                                )
                            else:
                                target_corner_d = opp_d
                                bypass_tile = None
                                downstream_ok = next_in_net in net_surface_tiles[candidate]

                            if (
                                exit_tile not in occupied_positions
                                and (
                                    exit_tile not in pin_tiles
                                    or (
                                        exit_tile in valid_input_pins
                                        and (
                                            turn_dir == DIRECTION_EAST
                                            or exit_tile in machine_input_pins
                                        )
                                    )
                                )
                                and exit_tile not in used_underground_endpoints
                                and tile_to_nets.get(exit_tile, set()) <= {candidate}
                                and corner_tile not in occupied_positions
                                and (
                                    corner_tile not in pin_tiles
                                    or (
                                        corner_tile in valid_input_pins
                                        and (
                                            target_corner_d == DIRECTION_EAST
                                            or corner_tile in machine_input_pins
                                        )
                                    )
                                )
                                and corner_tile not in used_underground_endpoints
                                and tile_to_nets.get(corner_tile, set()) <= {candidate}
                                and downstream_ok
                            ):
                                turn_detour = (
                                    exit_tile,
                                    turn_dir,
                                    corner_tile,
                                    target_corner_d,
                                    bypass_tile,
                                )
                            else:
                                detour_reasons = []
                                if exit_tile in occupied_positions:
                                    detour_reasons.append(f"exit_occ({exit_tile})")
                                if exit_tile in pin_tiles and not (
                                    exit_tile in valid_input_pins
                                    and (
                                        turn_dir == DIRECTION_EAST
                                        or exit_tile in machine_input_pins
                                    )
                                ):
                                    detour_reasons.append(f"exit_pin({exit_tile})")
                                if exit_tile in used_underground_endpoints:
                                    detour_reasons.append(f"exit_ug({exit_tile})")
                                if not (tile_to_nets.get(exit_tile, set()) <= {candidate}):
                                    detour_reasons.append(
                                        f"exit_net({tile_to_nets.get(exit_tile)})"
                                    )
                                if corner_tile in occupied_positions:
                                    detour_reasons.append(f"corner_occ({corner_tile})")
                                if corner_tile in pin_tiles and not (
                                    corner_tile in valid_input_pins
                                    and (
                                        target_corner_d == DIRECTION_EAST
                                        or corner_tile in machine_input_pins
                                    )
                                ):
                                    detour_reasons.append(f"corner_pin({corner_tile})")
                                if corner_tile in used_underground_endpoints:
                                    detour_reasons.append(f"corner_ug({corner_tile})")
                                if not (tile_to_nets.get(corner_tile, set()) <= {candidate}):
                                    detour_reasons.append(
                                        f"corner_net({tile_to_nets.get(corner_tile)})"
                                    )
                                if not downstream_ok:
                                    detour_reasons.append("downstream_not_ok")
                                fail_out.append(f"detour_fail:{','.join(detour_reasons)}")
                        else:
                            fail_out.append(
                                f"out_dir_mismatch({net_surface_tiles[candidate][out_pos]}!={d})"
                            )
                    if len(tile_to_nets.get(out_pos, set())) > 1:
                        fail_out.append("out_has_conflict")
                    if out_pos in used_underground_endpoints:
                        fail_out.append("out_used_endpoint")
                    if out_pos in used_underground_spans_h or out_pos in used_underground_spans_v:
                        fail_out.append("out_underground_span")
                    allowed_in = valid_fluid_input_pins if is_fluid else valid_input_pins
                    if out_pos in pin_tiles:
                        allowed = out_pos in allowed_in and (
                            d == DIRECTION_EAST
                            or (
                                not is_fluid
                                and out_pos in machine_input_pins
                                and d in (DIRECTION_SOUTH, DIRECTION_NORTH)
                            )
                        )
                        if not allowed:
                            fail_out.append("out_in_pin_tiles")
                    if out_pos in occupied_positions:
                        fail_out.append("out_in_occupied")
                    if splitter_feeds.get(out_pos):
                        fail_out.append("feed_into_exit_hood")
                    for s_dir in side_dirs:
                        sdx, sdy = DIR_STEPS[s_dir]
                        nb_pos = (round(out_pos[0] - sdx, 2), round(out_pos[1] - sdy, 2))
                        nb_nets = tile_to_nets.get(nb_pos)
                        if nb_nets and any(
                            net_surface_tiles[c_net].get(nb_pos) == s_dir
                            for c_net in nb_nets
                            if c_net != candidate
                        ):
                            fail_out.append("perpendicular_belt_feed")
                            break
                        if is_fluid and nb_nets and candidate in nb_nets:
                            fail_out.append("perpendicular_fluid_branch")
                            break
                    rdx, rdy = DIR_STEPS[d]
                    rear_pos = (round(out_pos[0] - rdx, 2), round(out_pos[1] - rdy, 2))
                    rear_nets = tile_to_nets.get(rear_pos)
                    if rear_nets and any(
                        net_surface_tiles[c_net].get(rear_pos) == d
                        for c_net in rear_nets
                        if c_net != candidate
                    ):
                        fail_out.append("rear_belt_feed")
                    if (
                        turn_detour is None
                        and out_pos not in pin_tiles
                        and next_pos not in net_surface_tiles[candidate]
                    ):
                        fail_out.append("out_next_not_in_net")
                    if turn_detour is None and used_underground_endpoints.get(next_pos) == opp_d:
                        fail_out.append("out_opp_next")
                    if fail_out:
                        if debug_trace is not None:
                            debug_trace.setdefault(pos, []).append(
                                {
                                    "candidate": candidate,
                                    "dir": d,
                                    "bspan": bspan,
                                    "fspan": fspan,
                                    "in_pos": in_pos,
                                    "out_pos": out_pos,
                                    "fail_in": [],
                                    "fail_out": list(fail_out),
                                    "valid_span": False,
                                    "invalid_reasons": [],
                                }
                            )
                        continue

                    valid_span = True
                    conflicts_cleared = 0
                    invalid_reasons = []
                    is_horiz = d in (DIRECTION_EAST, DIRECTION_WEST)
                    for s in range(-bspan + 1, fspan):
                        it = (round(cx + dx * s, 2), round(cy + dy * s, 2))
                        if (
                            it in net_surface_tiles[candidate]
                            and net_surface_tiles[candidate][it] != d
                        ):
                            valid_span = False
                            invalid_reasons.append(f"it_dir_mismatch({it})")
                            break
                        if it in occupied_positions:
                            valid_span = False
                            invalid_reasons.append(f"it_occupied({it})")
                            break
                        if it in used_underground_endpoints:
                            valid_span = False
                            invalid_reasons.append(f"it_used_endpoint({it})")
                            break
                        if is_horiz and it in used_underground_spans_h:
                            valid_span = False
                            invalid_reasons.append(f"it_used_span_h({it})")
                            break
                        if not is_horiz and it in used_underground_spans_v:
                            valid_span = False
                            invalid_reasons.append(f"it_used_span_v({it})")
                            break
                        if is_fluid:
                            has_branch = False
                            for s_dir in side_dirs:
                                sdx, sdy = DIR_STEPS[s_dir]
                                nb_pos = (round(it[0] - sdx, 2), round(it[1] - sdy, 2))
                                if candidate in tile_to_nets.get(nb_pos, set()):
                                    has_branch = True
                                    break
                            if has_branch:
                                valid_span = False
                                invalid_reasons.append(f"it_fluid_branch({it})")
                                break
                        if len(tile_to_nets.get(it, set())) > 1 or it in adjacent_fluid_conflicts:
                            conflicts_cleared += 1
                    if not valid_span:
                        if debug_trace is not None:
                            debug_trace.setdefault(pos, []).append(
                                {
                                    "candidate": candidate,
                                    "dir": d,
                                    "bspan": bspan,
                                    "fspan": fspan,
                                    "in_pos": in_pos,
                                    "out_pos": out_pos,
                                    "fail_in": [],
                                    "fail_out": [],
                                    "valid_span": False,
                                    "invalid_reasons": list(invalid_reasons),
                                }
                            )
                        continue

                    detour_penalty = (20 if turn_detour else 0) + (20 if in_detour else 0)
                    penalty = (
                        100 if len(tile_to_nets.get(next_pos, set())) > 1 else 0
                    ) + detour_penalty
                    score = conflicts_cleared * 50 - penalty - (bspan + fspan)
                    jump_tuple = (
                        in_pos,
                        out_pos,
                        bspan,
                        fspan,
                        d,
                        is_fluid,
                        in_detour,
                        turn_detour,
                    )
                    options.append((score, candidate, jump_tuple))
                    if debug_trace is not None:
                        debug_trace.setdefault(pos, []).append(
                            {
                                "candidate": candidate,
                                "dir": d,
                                "bspan": bspan,
                                "fspan": fspan,
                                "in_pos": in_pos,
                                "out_pos": out_pos,
                                "fail_in": [],
                                "fail_out": [],
                                "valid_span": True,
                                "invalid_reasons": [],
                                "score": score,
                            }
                        )

        return options

    cached_options: dict[tuple[float, float], list[tuple[float, str, Any]]] = {}

    while True:
        active_conflicts = multi_net_conflicts | adjacent_fluid_conflicts
        if not active_conflicts:
            break

        # Remove stale entries from cached_options for positions that are no longer conflicts
        for p in list(cached_options.keys()):
            if p not in active_conflicts:
                del cached_options[p]

        # Evaluate options for conflicts not yet cached
        for p in sorted(active_conflicts):
            if p not in cached_options:
                cached_options[p] = evaluate_options(p)

        available_options = {p: opts for p, opts in cached_options.items() if opts}
        if not available_options:
            break

        multi_options = [
            (opt[0], p, opt[1], opt[2])
            for p in sorted(available_options.keys())
            for opt in available_options[p]
            if opt[0] >= 80
        ]
        if multi_options:
            _, best_pos, best_candidate, best_jump = max(multi_options, key=lambda x: x[0])
        else:
            best_pos = None
            for p in sorted(active_conflicts):
                if p in available_options:
                    best_pos = p
                    _, best_candidate, best_jump = max(available_options[p], key=lambda x: x[0])
                    break
            if best_pos is None:
                break

        cx, cy = best_pos
        in_pos, out_pos, bspan, fspan, d, is_fluid, in_detour, turn_detour = best_jump
        dx, dy = DIR_STEPS[d]
        used_underground_endpoints[in_pos] = d
        used_underground_endpoints[out_pos] = d
        jumpers.append((best_candidate, in_pos, out_pos, d, is_fluid))
        if debug_trace is not None:
            debug_trace.setdefault(best_pos, []).append(
                {
                    "action": "jump_committed",
                    "candidate": best_candidate,
                    "in_pos": in_pos,
                    "out_pos": out_pos,
                    "dir": d,
                    "bspan": bspan,
                    "fspan": fspan,
                    "turn_detour": turn_detour is not None,
                    "in_detour": in_detour is not None,
                }
            )
        span_set = (
            used_underground_spans_h
            if d in (DIRECTION_EAST, DIRECTION_WEST)
            else used_underground_spans_v
        )

        touched_tiles: set[tuple[float, float]] = {in_pos, out_pos}

        for s in range(-bspan + 1, fspan):
            it = (round(cx + dx * s, 2), round(cy + dy * s, 2))
            span_set.add(it)
            touched_tiles.add(it)
            if it in net_surface_tiles[best_candidate]:
                del net_surface_tiles[best_candidate][it]
            if best_candidate in tile_to_nets.get(it, set()):
                tile_to_nets[it].remove(best_candidate)
                if not tile_to_nets[it]:
                    del tile_to_nets[it]
                update_tile_conflict(it)

        if turn_detour:
            exit_tile, exit_d, corner_tile, corner_d, bypass_tile = turn_detour
            touched_tiles.update({exit_tile, corner_tile, out_pos})
            if out_pos in net_surface_tiles[best_candidate]:
                del net_surface_tiles[best_candidate][out_pos]
            if best_candidate in tile_to_nets.get(out_pos, set()):
                tile_to_nets[out_pos].remove(best_candidate)
                if not tile_to_nets[out_pos]:
                    del tile_to_nets[out_pos]
                update_tile_conflict(out_pos)
            if bypass_tile:
                touched_tiles.add(bypass_tile)
                if bypass_tile in net_surface_tiles[best_candidate]:
                    del net_surface_tiles[best_candidate][bypass_tile]
                if best_candidate in tile_to_nets.get(bypass_tile, set()):
                    tile_to_nets[bypass_tile].remove(best_candidate)
                    if not tile_to_nets[bypass_tile]:
                        del tile_to_nets[bypass_tile]
                    update_tile_conflict(bypass_tile)
            net_surface_tiles[best_candidate][exit_tile] = exit_d
            tile_to_nets[exit_tile].add(best_candidate)
            update_tile_conflict(exit_tile)
            net_surface_tiles[best_candidate][corner_tile] = corner_d
            tile_to_nets[corner_tile].add(best_candidate)
            update_tile_conflict(corner_tile)

        if in_detour:
            n_pos, n_dir, corner_in, corner_d, entry_tile, entry_d = in_detour
            touched_tiles.update({n_pos, corner_in, entry_tile})
            net_surface_tiles[best_candidate][n_pos] = n_dir
            net_surface_tiles[best_candidate][corner_in] = corner_d
            tile_to_nets[corner_in].add(best_candidate)
            update_tile_conflict(corner_in)
            net_surface_tiles[best_candidate][entry_tile] = entry_d
            tile_to_nets[entry_tile].add(best_candidate)
            update_tile_conflict(entry_tile)

        # Invalidate cached options within Manhattan distance 13 of any touched tile
        for p in list(cached_options.keys()):
            px, py = p
            if any(abs(px - tx) + abs(py - ty) <= 13 for tx, ty in touched_tiles):
                del cached_options[p]

    for _candidate, in_pos, out_pos, d, is_fluid in jumpers:
        span = round(abs(out_pos[0] - in_pos[0]) + abs(out_pos[1] - in_pos[1]))
        if is_fluid:
            if d == DIRECTION_EAST:
                d_in, d_out = DIRECTION_WEST, DIRECTION_EAST
            elif d == DIRECTION_WEST:
                d_in, d_out = DIRECTION_EAST, DIRECTION_WEST
            elif d == DIRECTION_SOUTH:
                d_in, d_out = DIRECTION_NORTH, DIRECTION_SOUTH
            else:
                d_in, d_out = DIRECTION_SOUTH, DIRECTION_NORTH
            underground_entities.append(
                {
                    "name": "pipe-to-ground",
                    "position": {"x": in_pos[0], "y": in_pos[1]},
                    "direction": d_in,
                }
            )
            underground_entities.append(
                {
                    "name": "pipe-to-ground",
                    "position": {"x": out_pos[0], "y": out_pos[1]},
                    "direction": d_out,
                }
            )
        else:
            ent_name = choose_belt_tier(span)
            underground_entities.append(
                {
                    "name": ent_name,
                    "position": {"x": in_pos[0], "y": in_pos[1]},
                    "direction": d,
                    "type": "input",
                }
            )
            underground_entities.append(
                {
                    "name": ent_name,
                    "position": {"x": out_pos[0], "y": out_pos[1]},
                    "direction": d,
                    "type": "output",
                }
            )

    for ue in underground_entities:
        add_entity(
            ue["name"],
            ue["position"]["x"],
            ue["position"]["y"],
            direction=ue.get("direction"),
            extra={"type": ue["type"]} if "type" in ue else None,
        )

    for nname, tiles in net_surface_tiles.items():
        is_fluid = is_fluid_wire(net_map.get(nname, {}), "M1")
        entity_name = "pipe" if is_fluid else "transport-belt"
        for pos, d in tiles.items():
            add_entity(entity_name, pos[0], pos[1], direction=d if entity_name != "pipe" else None)

    pin_directions: dict[tuple[float, float], int] = {}
    forbidden_west_tiles: set[tuple[float, float]] = set()

    for comp in def_data.get("components", []):
        cname = comp["name"]
        macro = comp["macro"]
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", macro)
        if cell_id in ("belt_split2", "belt_balancer2"):
            pin_directions[(round(gx - 0.5, 2), round(gy + 0.5, 2))] = DIRECTION_EAST
            pin_directions[(round(gx - 0.5, 2), round(gy + 1.5, 2))] = DIRECTION_EAST
            forbidden_west_tiles.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
            forbidden_west_tiles.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))
        elif cell_id == "belt_merge2":
            pin_directions[(round(gx - 0.5, 2), round(gy + 0.5, 2))] = DIRECTION_EAST
            pin_directions[(round(gx - 0.5, 2), round(gy + 1.5, 2))] = DIRECTION_EAST
            forbidden_west_tiles.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
        elif cell_id == "pump":
            pin_directions[(round(gx + 0.5, 2), round(gy - 0.5, 2))] = DIRECTION_NORTH
            pin_directions[(round(gx + 0.5, 2), round(gy + 2.5, 2))] = DIRECTION_NORTH
        else:
            cell = cell_map.get(cell_id, {})
            inputs = cell.get("inputs", [])
            outputs = cell.get("outputs", [])
            for port in inputs:
                geom = get_macro_port_geometry(cell, port, is_output=False)
                ax, ay = geom["approach_pos"]
                adir = geom["approach_dir"]
                pin_directions[(round(gx + ax, 2), round(gy + ay, 2))] = adir
            for port in outputs:
                geom = get_macro_port_geometry(cell, port, is_output=True)
                ax, ay = geom["approach_pos"]
                forbidden_west_tiles.add((round(gx + ax, 2), round(gy + ay, 2)))

            machine = cell.get("machine", "")
            if machine == "rocket-silo" or "rocket-silo" in cell_id or "rocket_silo" in macro:
                mid_pickup_pos = (round(gx - 0.5, 2), round(gy + 4.5, 2))
                if mid_pickup_pos not in grid_entities:
                    add_entity(
                        "transport-belt",
                        mid_pickup_pos[0],
                        mid_pickup_pos[1],
                        direction=DIRECTION_EAST,
                    )
                else:
                    pin_directions[mid_pickup_pos] = DIRECTION_EAST

                mid_turn_pos = (round(gx - 1.5, 2), round(gy + 4.5, 2))
                if mid_turn_pos not in grid_entities:
                    add_entity(
                        "transport-belt",
                        mid_turn_pos[0],
                        mid_turn_pos[1],
                        direction=DIRECTION_EAST,
                    )
                else:
                    pin_directions[mid_turn_pos] = DIRECTION_EAST

    for pos, req_dir in pin_directions.items():
        if pos in grid_entities and "transport-belt" in grid_entities[pos].get("name", ""):
            cur_d = grid_entities[pos].get("direction")
            if cur_d in DIR_STEPS:
                dx, dy = DIR_STEPS[cur_d]
                next_pos = (round(pos[0] + dx, 2), round(pos[1] + dy, 2))
                if next_pos in grid_entities and "transport-belt" in grid_entities[next_pos].get(
                    "name", ""
                ):
                    continue
            grid_entities[pos]["direction"] = req_dir

    for pos in forbidden_west_tiles:
        if (
            pos in grid_entities
            and "transport-belt" in grid_entities[pos].get("name", "")
            and grid_entities[pos].get("direction") == DIRECTION_WEST
        ):
            grid_entities[pos]["direction"] = DIRECTION_EAST

    for pos, ent in list(grid_entities.items()):
        if "transport-belt" not in ent.get("name", ""):
            continue
        d = ent.get("direction", 0)
        if d not in DIR_STEPS:
            continue
        dx, dy = DIR_STEPS[d]
        tpos = (round(pos[0] + dx, 2), round(pos[1] + dy, 2))
        if tpos in grid_entities:
            target = grid_entities[tpos]
            if "transport-belt" in target.get("name", ""):
                td = target.get("direction", 0)
                diff = abs(td - d) % 16
                if diff in (4, 12):
                    tdx, tdy = DIR_STEPS.get(td, (0, 0))
                    behind_pos = (round(tpos[0] - tdx, 2), round(tpos[1] - tdy, 2))
                    if behind_pos in grid_entities and "transport-belt" in grid_entities[
                        behind_pos
                    ].get("name", ""):
                        from_nets = tile_to_nets.get(pos, set())
                        target_nets = tile_to_nets.get(tpos, set())
                        from_mats = {n.split("__")[0] for n in from_nets}
                        target_mats = {n.split("__")[0] for n in target_nets}
                        if from_mats and target_mats and not from_mats.intersection(target_mats):
                            net = next(iter(from_nets))
                            for cand_d, (cdx, cdy) in DIR_STEPS.items():
                                np = (round(pos[0] + cdx, 2), round(pos[1] + cdy, 2))
                                if net in tile_to_nets.get(np, set()) and np != tpos:
                                    ent["direction"] = cand_d
                                    break

    return RouteResult(
        net_surface_tiles=net_surface_tiles,
        tile_to_nets=tile_to_nets,
        adjacent_fluid_conflicts=adjacent_fluid_conflicts,
        pin_tiles=pin_tiles,
        valid_input_pins=valid_input_pins,
        valid_output_pins=valid_output_pins,
    )
