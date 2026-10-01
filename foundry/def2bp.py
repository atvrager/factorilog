"""DEF to Factorio blueprint converter.

Reads placed and routed OpenROAD DEF files and netlist JSON metadata,
then generates Factorio 2.0 blueprint exchange strings.
"""

from __future__ import annotations

import argparse
import collections
import heapq
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import blueprint

# Factorio 2.0 direction values: North=0, East=4, South=8, West=12.
DIRECTION_NORTH = 0
DIRECTION_EAST = 4
DIRECTION_SOUTH = 8
DIRECTION_WEST = 12


def parse_def(text: str) -> dict[str, Any]:
    """Parse DEF text into components, nets, pins, and design units."""
    units = 1000
    m_units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text)
    if m_units:
        units = int(m_units.group(1))

    comp_pat = re.compile(
        r"^\s*-\s+(\S+)\s+(\S+)\s+\+\s+(?:PLACED|FIXED)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)",
        re.MULTILINE,
    )
    components: list[dict[str, Any]] = [
        {
            "name": m.group(1),
            "macro": m.group(2),
            "x": int(m.group(3)),
            "y": int(m.group(4)),
            "orient": m.group(5),
        }
        for m in comp_pat.finditer(text)
    ]

    pin_pat = re.compile(
        r"-\s+(\S+)\s+\+\s+NET\s+(\S+)\s+\+\s+DIRECTION\s+(\S+).*?PLACED\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)",
        re.DOTALL,
    )
    pins: list[dict[str, Any]] = [
        {
            "name": m.group(1),
            "net": m.group(2),
            "direction": m.group(3),
            "x": int(m.group(4)),
            "y": int(m.group(5)),
        }
        for m in pin_pat.finditer(text)
    ]

    # Parse NETS section
    nets: list[dict[str, Any]] = []
    nets_match = re.search(r"NETS\s+\d+\s*;(.*?)END\s+NETS", text, re.DOTALL)
    if nets_match:
        nets_text = nets_match.group(1)
        # Split on net declarations: lines starting with "- <net_name>"
        net_chunks = re.split(r"(?m)^\s*-\s+", nets_text)
        for chunk in net_chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            lines = chunk.splitlines()
            header = lines[0].split()
            net_name = header[0]
            wires: list[dict[str, Any]] = []
            vias: list[dict[str, Any]] = []

            for line in lines:
                line = line.strip()
                # Check for layer cut pattern: ( x y ) cut_name
                m_via = re.search(
                    r"(?:ROUTED|NEW)\s+(\S+)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+([A-Za-z0-9_]+)",
                    line,
                )
                if m_via and not m_via.group(4).isdigit() and m_via.group(4) != "*":
                    vias.append(
                        {
                            "layer": m_via.group(1),
                            "x": int(m_via.group(2)),
                            "y": int(m_via.group(3)),
                            "via": m_via.group(4),
                        }
                    )
                    continue

                # Check for wire segment: ( x1 y1 [ext] ) ( x2 y2 [ext] )
                m_wire = re.search(
                    r"(?:ROUTED|NEW)\s+(\S+)\s+\(\s*(-?\d+)\s+(-?\d+)(?:\s+-?\d+)?\s*\)\s+\(\s*(\*|-?\d+)\s+(\*|-?\d+)",
                    line,
                )
                if m_wire:
                    layer = m_wire.group(1)
                    x1 = int(m_wire.group(2))
                    y1 = int(m_wire.group(3))
                    x2_str = m_wire.group(4)
                    y2_str = m_wire.group(5)
                    x2 = x1 if x2_str == "*" else int(x2_str)
                    y2 = y1 if y2_str == "*" else int(y2_str)
                    wires.append(
                        {
                            "layer": layer,
                            "x1": x1,
                            "y1": y1,
                            "x2": x2,
                            "y2": y2,
                        }
                    )

            conns = [
                (c[0], c[1])
                for c in re.findall(r"\(\s*([A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s*\)", chunk)
                if not (c[1].isdigit() or c[0].isdigit() or c[1] == "*" or c[0] == "*")
            ]

            nets.append(
                {
                    "name": net_name,
                    "wires": wires,
                    "vias": vias,
                    "connections": conns,
                }
            )

    return {
        "units": units,
        "components": components,
        "pins": pins,
        "nets": nets,
    }


def _segment_direction(x1: int, y1: int, x2: int, y2: int) -> int:
    """Determine Factorio 2.0 cardinal direction from endpoint coordinates."""
    if x1 == x2:
        return DIRECTION_SOUTH if y2 >= y1 else DIRECTION_NORTH
    return DIRECTION_EAST if x2 >= x1 else DIRECTION_WEST


def build_entities(def_data: dict[str, Any], netlist_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate parsed DEF and netlist into Factorio blueprint entities."""
    scale = 1.0 / float(def_data.get("units", 1000))
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})

    entities: list[dict[str, Any]] = []
    occupied_positions: set[tuple[float, float]] = set()

    grid_entities: dict[tuple[float, float], dict[str, Any]] = {}

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

    fluids = set(netlist_data.get("fluids", []))

    def is_fluid_wire(net: dict[str, Any], layer: str) -> bool:
        if fluids:
            net_name = net.get("name", "")
            base = net_name.split("__")[0].replace("_", "-")
            return base in fluids
        return layer in ("M3", "M4")

    # 1. Place components
    inst_to_entity: dict[str, dict[str, Any]] = {}
    for comp in def_data.get("components", []):
        cname = comp["name"]
        macro = comp["macro"]
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", "")
        cell = cell_map.get(cell_id, {})

        if macro == "belt_merge2" or cell_id == "belt_merge2":
            # Splitter: 1 tile wide, 2 tiles high when facing East
            orient = comp.get("orient", "N")
            if orient in ("W", "E"):
                cw, ch = 2, 1
                center_x, center_y = gx + 1.0, gy + 0.5
                direction = DIRECTION_NORTH if orient == "W" else DIRECTION_SOUTH
            else:
                cw, ch = 1, 2
                center_x, center_y = gx + 0.5, gy + 1.0
                direction = DIRECTION_WEST if orient == "S" else DIRECTION_EAST
            ent = add_entity("splitter", center_x, center_y, direction=direction)
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))
        elif macro == "pump" or cell_id == "pump":
            # Pump: 1 tile wide, 2 tiles high when facing North
            orient = comp.get("orient", "N")
            if orient in ("W", "E"):
                cw, ch = 2, 1
                center_x, center_y = gx + 1.0, gy + 0.5
                direction = DIRECTION_WEST if orient == "W" else DIRECTION_EAST
            else:
                cw, ch = 1, 2
                center_x, center_y = gx + 0.5, gy + 1.0
                direction = DIRECTION_SOUTH if orient == "S" else DIRECTION_NORTH
            ent = add_entity("pump", center_x, center_y, direction=direction)
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))
        elif macro == "display_panel" or cell_id == "display_panel":
            center_x, center_y = gx + 0.5, gy + 0.5
            mat = inst.get("recipe", "")
            extra = {"text": mat}
            if mat:
                extra["custom_icon"] = {"type": "item", "name": mat}
            ent = add_entity(
                "display-panel", center_x, center_y, direction=DIRECTION_NORTH, extra=extra
            )
            inst_to_entity[cname] = ent
            occupied_positions.add((round(gx + 0.5, 2), round(gy + 0.5, 2)))
        elif macro == "arithmetic_combinator" or cell_id == "arithmetic_combinator":
            cw, ch = 1, 2
            center_x, center_y = gx + 0.5, gy + 1.0
            extra = {
                "control_behavior": {
                    "arithmetic_conditions": {
                        "first_signal": {"type": "virtual", "name": "signal-each"},
                        "operation": "+",
                        "second_constant": 0,
                        "output_signal": {"type": "virtual", "name": "signal-each"},
                    }
                }
            }
            ent = add_entity(
                "arithmetic-combinator", center_x, center_y, direction=DIRECTION_NORTH, extra=extra
            )
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))
        elif macro == "decider_combinator" or cell_id == "decider_combinator":
            cw, ch = 1, 2
            center_x, center_y = gx + 0.5, gy + 1.0
            extra = {
                "control_behavior": {
                    "decider_conditions": {
                        "first_signal": {"type": "virtual", "name": "signal-each"},
                        "comparator": ">",
                        "constant": 0,
                        "output_signal": {"type": "virtual", "name": "signal-each"},
                        "copy_count_from_input": True,
                    }
                }
            }
            ent = add_entity(
                "decider-combinator", center_x, center_y, direction=DIRECTION_NORTH, extra=extra
            )
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))
        elif macro == "constant_combinator" or cell_id == "constant_combinator":
            center_x, center_y = gx + 0.5, gy + 0.5
            ent = add_entity("constant-combinator", center_x, center_y, direction=DIRECTION_NORTH)
            inst_to_entity[cname] = ent
            occupied_positions.add((round(gx + 0.5, 2), round(gy + 0.5, 2)))
        else:
            cw = int(cell.get("width", 3))
            ch = int(cell.get("height", 3))
            machine = cell.get("machine", "assembling-machine-3")
            recipe = inst.get("recipe", cell.get("recipe", ""))
            extra = {"recipe": recipe} if recipe else None
            ent = add_entity(
                machine,
                gx + cw / 2.0,
                gy + ch / 2.0,
                direction=DIRECTION_NORTH,
                extra=extra,
            )
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))

    def choose_belt_tier(span_tiles: int) -> str:
        if span_tiles <= 5:
            return "underground-belt"
        if span_tiles <= 7:
            return "fast-underground-belt"
        if span_tiles <= 9:
            return "express-underground-belt"
        return "turbo-underground-belt"

    def segment_run(
        start: float,
        end: float,
        max_allowed_span: int,
        axis_coord: float = 0.0,
        is_horizontal: bool = True,
        blocked_tiles: set[tuple[float, float]] | None = None,
    ) -> list[tuple[float, float, int]]:
        length = round(abs(end - start))
        step = 1 if end > start else -1
        if length <= max_allowed_span:
            return [(start, end, length)]
        blocked = blocked_tiles or set()
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
                if p_exit not in blocked and p_next not in blocked:
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
        """Calculate relative (x, y) pin center on a macro."""
        if cell_id == "belt_merge2":
            merge_pins = {
                "a": (0.25, 0.5),
                "b": (0.25, 1.5),
                "y": (0.75, 0.5),
            }
            return merge_pins.get(pin, (0.5, 0.5))
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
            idx = outputs.index(pin)
            total = len(outputs)
            if total <= height:
                offset = (height - total) // 2
                return (width - 0.25, offset + idx + 0.5)
            if idx < height:
                return (width - 0.25, idx + 0.5)
            if idx < height + width:
                return (idx - height + 0.5, 0.25)
            if idx < 2 * height + width:
                return (0.25, idx - height - width + 0.5)
            return ((idx - 2 * height - width) % width + 0.5, height - 0.25)
        return (width / 2.0, height / 2.0)

    # Compute Dijkstra distance maps from drivers for all nets
    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    input_pins: dict[str, tuple[float, float]] = {}
    for p in def_data.get("pins", []):
        if p.get("direction") == "INPUT":
            input_pins[p["net"]] = (p["x"] * scale, p["y"] * scale)

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
                outputs = (
                    ["y"]
                    if cell_id in ("belt_merge2", "pump")
                    else [o.replace("-", "_") for o in cell_map.get(cell_id, {}).get("outputs", [])]
                )
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

    # Track layer connectivity for turns
    perp_conn: dict[tuple[str, tuple[float, float]], set[str]] = collections.defaultdict(set)
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        for w in net.get("wires", []):
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

    # 2. Extract surface belts (M1 and M2) and collect underground runs (M3 and M4)
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

            if layer in ("M3", "M4"):
                if from_p[0] == to_p[0]:
                    m4_runs_by_net[nname].setdefault(t1_x, []).append((t1_y, t2_y, d))
                else:
                    m3_runs_by_net[nname].setdefault(t1_y, []).append((t1_x, t2_x, d))
            else:
                # Surface wire M1 / M2
                if from_p[0] == to_p[0]:
                    tx = t1_x
                    y_start = math.floor(from_p[1])
                    y_end = math.floor(to_p[1])
                    step = 1 if y_end >= y_start else -1
                    for ty in range(y_start, y_end + step, step):
                        pos = (tx, ty + 0.5)
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
                        tile_to_nets[pos].add(nname)
                        if tx != x_end or pos not in net_surface_tiles[nname]:
                            net_surface_tiles[nname][pos] = d

    # 3. Process M3 and M4 runs with obstacle-aware segmentation
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        is_fluid = is_fluid_wire(net, "M3")
        blocked_tiles = {pos for pos, nets in tile_to_nets.items() if any(n != nname for n in nets)}

        # Process horizontal runs (M3)
        for cy, segs in m3_runs_by_net.get(nname, {}).items():
            for s1, s2, d in segs:
                if s1 == s2:
                    continue
                step = 1 if s2 >= s1 else -1
                start_x, end_x = s1, s2

                start_is_turn = "V" in perp_conn.get((nname, (start_x, cy)), set())
                end_is_turn = "V" in perp_conn.get((nname, (end_x, cy)), set())

                if start_is_turn:
                    net_surface_tiles[nname][(start_x, cy)] = d
                    tile_to_nets[(start_x, cy)].add(nname)

                if end_is_turn:
                    if (end_x, cy) not in net_surface_tiles[nname]:
                        net_surface_tiles[nname][(end_x, cy)] = d
                    tile_to_nets[(end_x, cy)].add(nname)

                ug_start = round(start_x + (step if start_is_turn else 0), 2)
                ug_end = round(end_x - (step if end_is_turn else 0), 2)

                if (ug_end - ug_start) * step < 1:
                    cur = ug_start
                    while (ug_end - cur) * step >= 0:
                        net_surface_tiles[nname][(cur, cy)] = d
                        tile_to_nets[(cur, cy)].add(nname)
                        cur = round(cur + step, 2)
                else:
                    max_allowed_span = 10 if is_fluid else 11
                    hops = segment_run(ug_start, ug_end, max_allowed_span, cy, True, blocked_tiles)
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

        # Process vertical runs (M4)
        for cx, segs in m4_runs_by_net.get(nname, {}).items():
            for s1, s2, d in segs:
                if s1 == s2:
                    continue
                step = 1 if s2 >= s1 else -1
                start_y, end_y = s1, s2

                start_is_turn = "H" in perp_conn.get((nname, (cx, start_y)), set())
                end_is_turn = "H" in perp_conn.get((nname, (cx, end_y)), set())

                if start_is_turn:
                    net_surface_tiles[nname][(cx, start_y)] = d
                    tile_to_nets[(cx, start_y)].add(nname)

                if end_is_turn:
                    if (cx, end_y) not in net_surface_tiles[nname]:
                        net_surface_tiles[nname][(cx, end_y)] = d
                    tile_to_nets[(cx, end_y)].add(nname)

                ug_start = round(start_y + (step if start_is_turn else 0), 2)
                ug_end = round(end_y - (step if end_is_turn else 0), 2)

                if (ug_end - ug_start) * step < 1:
                    cur = ug_start
                    while (ug_end - cur) * step >= 0:
                        net_surface_tiles[nname][(cx, cur)] = d
                        tile_to_nets[(cx, cur)].add(nname)
                        cur = round(cur + step, 2)
                else:
                    max_allowed_span = 10 if is_fluid else 11
                    hops = segment_run(ug_start, ug_end, max_allowed_span, cx, False, blocked_tiles)
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

    # 4. Resolve surface conflicts using legal straight-run jumpers
    jumpers: list[tuple[str, tuple[float, float], tuple[float, float], int, bool]] = []
    used_underground_endpoints: set[tuple[float, float]] = set()
    for ue in underground_entities:
        pos_u = (ue["position"]["x"], ue["position"]["y"])
        used_underground_endpoints.add(pos_u)

    DIR_STEPS = {
        DIRECTION_EAST: (1, 0),
        DIRECTION_WEST: (-1, 0),
        DIRECTION_SOUTH: (0, 1),
        DIRECTION_NORTH: (0, -1),
    }

    conflicts = [pos for pos, nets in tile_to_nets.items() if len(nets) > 1]
    for pos in conflicts:
        if len(tile_to_nets[pos]) <= 1:
            continue
        cx, cy = pos
        net_list = list(tile_to_nets[pos])
        resolved = False
        for candidate in net_list:
            if resolved:
                break
            d = net_surface_tiles[candidate].get(pos)
            if d not in DIR_STEPS:
                continue
            dx, dy = DIR_STEPS[d]
            is_fluid = is_fluid_wire(net_map.get(candidate, {}), "M1")

            for bspan in range(1, 5):
                if resolved:
                    break
                in_pos = (round(cx - dx * bspan, 2), round(cy - dy * bspan, 2))
                if (
                    in_pos not in net_surface_tiles[candidate]
                    or net_surface_tiles[candidate][in_pos] != d
                    or len(tile_to_nets[in_pos]) > 1
                    or in_pos in used_underground_endpoints
                ):
                    continue
                for fspan in range(1, 10):
                    if bspan + fspan > 10:
                        break
                    out_pos = (round(cx + dx * fspan, 2), round(cy + dy * fspan, 2))
                    if (
                        out_pos not in net_surface_tiles[candidate]
                        or net_surface_tiles[candidate][out_pos] != d
                        or len(tile_to_nets[out_pos]) > 1
                        or out_pos in used_underground_endpoints
                    ):
                        continue
                    next_pos = (round(out_pos[0] + dx, 2), round(out_pos[1] + dy, 2))
                    if next_pos not in net_surface_tiles[candidate]:
                        continue
                    if len(tile_to_nets.get(next_pos, set())) > 1 and fspan < 9:
                        continue

                    # Verify straight run
                    valid_span = True
                    for s in range(-bspan + 1, fspan):
                        it = (round(cx + dx * s, 2), round(cy + dy * s, 2))
                        if (
                            it in net_surface_tiles[candidate]
                            and net_surface_tiles[candidate][it] != d
                        ):
                            valid_span = False
                            break
                    if not valid_span:
                        continue

                    used_underground_endpoints.add(in_pos)
                    used_underground_endpoints.add(out_pos)
                    jumpers.append((candidate, in_pos, out_pos, d, is_fluid))
                    for s in range(-bspan + 1, fspan):
                        it = (round(cx + dx * s, 2), round(cy + dy * s, 2))
                        if it in net_surface_tiles[candidate]:
                            del net_surface_tiles[candidate][it]
                        if candidate in tile_to_nets[it]:
                            tile_to_nets[it].remove(candidate)
                    resolved = True
                    break

    # Add underground jumpers to underground_entities
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

    # 5. Place all underground entities
    for ue in underground_entities:
        add_entity(
            ue["name"],
            ue["position"]["x"],
            ue["position"]["y"],
            direction=ue.get("direction"),
            extra={"type": ue["type"]} if "type" in ue else None,
        )

    # 6. Place remaining surface belts and pipes
    for nname, tiles in net_surface_tiles.items():
        is_fluid = is_fluid_wire(net_map.get(nname, {}), "M1")
        entity_name = "pipe" if is_fluid else "transport-belt"
        for pos, d in tiles.items():
            add_entity(entity_name, pos[0], pos[1], direction=d if entity_name != "pipe" else None)

    # 7. Prevent unintended sideloading into perpendicular belts of different nets
    grid_ents = {(round(e["position"]["x"], 2), round(e["position"]["y"], 2)): e for e in entities}
    for pos, ent in grid_ents.items():
        if "transport-belt" not in ent.get("name", ""):
            continue
        d = ent.get("direction", 0)
        if d not in DIR_STEPS:
            continue
        dx, dy = DIR_STEPS[d]
        tpos = (round(pos[0] + dx, 2), round(pos[1] + dy, 2))
        if tpos in grid_ents:
            target = grid_ents[tpos]
            if "transport-belt" in target.get("name", ""):
                td = target.get("direction", 0)
                diff = abs(td - d) % 16
                if diff in (4, 12):
                    tdx, tdy = DIR_STEPS.get(td, (0, 0))
                    behind_pos = (round(tpos[0] - tdx, 2), round(tpos[1] - tdy, 2))
                    if behind_pos in grid_ents and "transport-belt" in grid_ents[behind_pos].get(
                        "name", ""
                    ):
                        from_nets = tile_to_nets.get(pos, set())
                        target_nets = tile_to_nets.get(tpos, set())
                        if from_nets and target_nets and not from_nets.intersection(target_nets):
                            net = next(iter(from_nets))
                            for cand_d, (cdx, cdy) in DIR_STEPS.items():
                                np = (round(pos[0] + cdx, 2), round(pos[1] + cdy, 2))
                                if net in tile_to_nets.get(np, set()) and np != tpos:
                                    ent["direction"] = cand_d
                                    break

    # 8. Wire circuit telemetry connections
    def connect_wire(
        ent_a: dict[str, Any], port_a: int, ent_b: dict[str, Any], port_b: int, color: str
    ) -> None:
        conns_a = (
            ent_a.setdefault("connections", {}).setdefault(str(port_a), {}).setdefault(color, [])
        )
        conns_b = (
            ent_b.setdefault("connections", {}).setdefault(str(port_b), {}).setdefault(color, [])
        )
        conns_a.append({"_target": ent_b, "circuit_id": port_b})
        conns_b.append({"_target": ent_a, "circuit_id": port_a})

    for rec in netlist_data.get("telemetry", []):
        mat = rec.get("material", "")
        disp_name = rec.get("display_instance")
        circ_name = rec.get("circuit_instance")
        disp_ent = inst_to_entity.get(disp_name) if disp_name else None
        circ_ent = inst_to_entity.get(circ_name) if circ_name else None

        ref_pos = None
        if disp_ent:
            ref_pos = (disp_ent["position"]["x"], disp_ent["position"]["y"])
        elif circ_ent:
            ref_pos = (circ_ent["position"]["x"], circ_ent["position"]["y"])

        if not ref_pos:
            continue

        mat_bare = mat.replace("-", "_").replace(".", "__")
        candidate_tiles = set(net_surface_tiles.get(mat, {}).keys())
        candidate_tiles.update(net_surface_tiles.get(mat_bare, {}).keys())

        best_ent = None
        best_dist = 1e9
        for pos_tile in candidate_tiles:
            cand = grid_entities.get(pos_tile)
            if cand and "transport-belt" in cand.get("name", ""):
                cand_dist = math.hypot(pos_tile[0] - ref_pos[0], pos_tile[1] - ref_pos[1])
                if cand_dist < best_dist:
                    best_dist = cand_dist
                    best_ent = cand

        if best_ent is None:
            for inst_name, cand in inst_to_entity.items():
                if cand.get("name") == "splitter":
                    inst_obj = inst_map.get(inst_name, {})
                    if inst_obj.get("recipe") == mat:
                        cand_dist = math.hypot(
                            cand["position"]["x"] - ref_pos[0], cand["position"]["y"] - ref_pos[1]
                        )
                        if cand_dist < best_dist:
                            best_dist = cand_dist
                            best_ent = cand

        if best_ent is not None:
            best_ent.setdefault("control_behavior", {})["circuit_read_hand_contents"] = True
            best_ent["control_behavior"]["circuit_contents_read_mode"] = 0

            if disp_ent and not circ_ent:
                connect_wire(best_ent, 1, disp_ent, 1, "red")
            elif circ_ent and not disp_ent:
                connect_wire(best_ent, 1, circ_ent, 1, "red")
                connect_wire(circ_ent, 2, circ_ent, 1, "green")
            elif disp_ent and circ_ent:
                connect_wire(best_ent, 1, circ_ent, 1, "red")
                connect_wire(circ_ent, 2, circ_ent, 1, "green")
                connect_wire(circ_ent, 2, disp_ent, 1, "red")

    # Assign 1-based sequential entity numbers
    for idx, ent in enumerate(entities, 1):
        ent["entity_number"] = idx

    # Resolve wire connections to entity_id
    for ent in entities:
        for colors in ent.get("connections", {}).values():
            for color, links in list(colors.items()):
                resolved_links = []
                for link in links:
                    target = link.get("_target")
                    cid = link.get("circuit_id", 1)
                    if target and "entity_number" in target:
                        resolved_links.append(
                            {"entity_id": target["entity_number"], "circuit_id": cid}
                        )
                    elif "entity_id" in link:
                        resolved_links.append(link)
                colors[color] = resolved_links

    return entities


def def_to_blueprint(def_text: str, netlist_data: dict[str, Any], label: str = "factory") -> str:
    """Convert DEF text and netlist metadata into a blueprint exchange string."""
    def_data = parse_def(def_text)
    entities = build_entities(def_data, netlist_data)
    return blueprint.encode(entities, label)


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

    args = parser.parse_args()

    def_text = args.def_file.read_text()
    netlist_data = json.loads(args.netlist_file.read_text())

    def_data = parse_def(def_text)
    entities = build_entities(def_data, netlist_data)

    if args.json:
        document = {
            "blueprint": {
                "item": "blueprint",
                "label": args.label,
                "version": blueprint.VERSION_2_0,
                "entities": entities,
            }
        }
        output_text = json.dumps(document, indent=2)
    else:
        output_text = blueprint.encode(entities, args.label)

    if args.out:
        args.out.write_text(output_text + "\n")
    else:
        sys.stdout.write(output_text + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
