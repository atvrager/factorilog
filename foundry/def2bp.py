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

try:
    from foundry.lef import FLUID_MATERIALS, get_macro_port_slots
except ImportError:
    from lef import FLUID_MATERIALS, get_macro_port_slots

# Factorio 2.0 direction values: North=0, East=4, South=8, West=12.
DIRECTION_NORTH = 0
DIRECTION_EAST = 4
DIRECTION_SOUTH = 8
DIRECTION_WEST = 12

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


def entity_size(name: str) -> tuple[float, float]:
    """Return width and height of named entity."""
    if "rocket-silo" in name:
        return 9.0, 9.0
    if "oil-refinery" in name:
        return 5.0, 5.0
    if (
        "assembling-machine" in name
        or "electric-furnace" in name
        or "chemical-plant" in name
        or "centrifuge" in name
    ):
        return 3.0, 3.0
    if "steel-furnace" in name or "stone-furnace" in name or "power-switch" in name:
        return 2.0, 2.0
    if "pump" in name:
        return 1.0, 2.0
    return 1.0, 1.0


def entity_collision_half_size(name: str) -> tuple[float, float]:
    """Return half width and half height of entity collision box."""
    if "rocket-silo" in name:
        return 4.2, 4.2
    if "oil-refinery" in name:
        return 2.2, 2.2
    if (
        "assembling-machine" in name
        or "electric-furnace" in name
        or "chemical-plant" in name
        or "centrifuge" in name
    ):
        return 1.2, 1.2
    if "furnace" in name or "power-switch" in name:
        return 0.7, 0.7
    if "pump" in name:
        return 0.29, 0.9
    return 0.15, 0.15


def check_power(entities: list[dict[str, Any]], supply_radius: float = 3.5) -> list[dict[str, Any]]:
    """Return list of powered entities lacking electric pole supply coverage."""
    poles = [e for e in entities if e.get("name") == "medium-electric-pole"]
    powered = [
        e
        for e in entities
        if any(
            k in e.get("name", "")
            for k in (
                "assembling-machine",
                "electric-furnace",
                "chemical-plant",
                "oil-refinery",
                "centrifuge",
                "rocket-silo",
                "inserter",
                "pump",
            )
        )
    ]
    unpowered = []
    for e in powered:
        ex = e["position"]["x"]
        ey = e["position"]["y"]
        w, h = entity_size(e.get("name", ""))
        max_dist = supply_radius + max(w, h) / 2.0
        covered = False
        for p in poles:
            px = p["position"]["x"]
            py = p["position"]["y"]
            if abs(ex - px) > max_dist or abs(ey - py) > max_dist:
                continue
            if (
                max(
                    max(0.0, abs(ex - px) - w / 2.0),
                    max(0.0, abs(ey - py) - h / 2.0),
                )
                <= supply_radius
            ):
                covered = True
                break
        if not covered:
            unpowered.append(e)
    return unpowered


def check_collisions(
    entities: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Check for surface entity collisions. Return list of colliding entity pairs."""
    occupied: dict[tuple[float, float], dict[str, Any]] = {}
    collisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ent in entities:
        epos = ent["position"]
        tiles = [(round(epos["x"], 2), round(epos["y"], 2))]
        ename = ent.get("name", "")
        if ename == "splitter":
            d = ent.get("direction", 0)
            if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                tiles.append((round(epos["x"] + 1.0, 2), round(epos["y"], 2)))
            else:
                tiles.append((round(epos["x"], 2), round(epos["y"] + 1.0, 2)))
        for t in tiles:
            if t in occupied:
                collisions.append((occupied[t], ent))
            else:
                occupied[t] = ent
    return collisions


def check_underground_pairing(
    entities: list[dict[str, Any]], max_span: int = 6
) -> list[dict[str, Any]]:
    """Verify that all underground belt entities pair correctly."""
    ugs = [e for e in entities if e.get("name") == "underground-belt"]
    inputs = [e for e in ugs if e.get("type") == "input"]
    outputs = [e for e in ugs if e.get("type") == "output"]
    matched_outputs = set()
    unpaired: list[dict[str, Any]] = []
    for inp in inputs:
        ix, iy = inp["position"]["x"], inp["position"]["y"]
        d = inp.get("direction", 0)
        found = None
        for o in outputs:
            if id(o) in matched_outputs:
                continue
            ox, oy = o["position"]["x"], o["position"]["y"]
            od = o.get("direction", 0)
            if od != d:
                continue
            if (
                (d == DIRECTION_EAST and iy == oy and 1 <= ox - ix <= max_span)
                or (d == DIRECTION_WEST and iy == oy and 1 <= ix - ox <= max_span)
                or (d == DIRECTION_SOUTH and ix == ox and 1 <= oy - iy <= max_span)
                or (d == DIRECTION_NORTH and ix == ox and 1 <= iy - oy <= max_span)
            ):
                found = o
                break
        if found:
            matched_outputs.add(id(found))
        else:
            unpaired.append(inp)
    unpaired.extend([o for o in outputs if id(o) not in matched_outputs])
    return unpaired


def build_entities(
    def_data: dict[str, Any], netlist_data: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[list[int]]]:
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

    has_fluid_metadata = "fluids" in netlist_data
    fluids = set(netlist_data.get("fluids") or []) | set(FLUID_MATERIALS)

    def is_fluid_wire(net: dict[str, Any], layer: str) -> bool:
        if has_fluid_metadata:
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

        if macro in ("belt_merge2", "belt_split2", "belt_balancer2") or cell_id in (
            "belt_merge2",
            "belt_split2",
            "belt_balancer2",
        ):
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
        elif macro == "power_switch" or cell_id == "power_switch":
            cw, ch = 2, 2
            center_x, center_y = gx + 1.0, gy + 1.0
            cond = inst.get(
                "circuit_condition",
                {
                    "first_signal": {"type": "virtual", "name": "signal-T"},
                    "comparator": "<",
                    "constant": 30,
                },
            )
            extra = {
                "control_behavior": {
                    "circuit_enabled": True,
                    "circuit_condition": cond,
                },
                "switch_state": False,
            }
            ent = add_entity(
                "power-switch", center_x, center_y, direction=DIRECTION_NORTH, extra=extra
            )
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))
        else:
            cw = int(cell.get("width", 3))
            ch = int(cell.get("height", 3))
            m_w = int(cell.get("machine_width", 3))
            m_h = int(cell.get("machine_height", 3))
            machine = cell.get("machine", "assembling-machine-3")
            recipe = inst.get("recipe", cell.get("recipe", ""))
            extra = {"recipe": recipe} if recipe else None
            inputs = cell.get("inputs", [])
            outputs = cell.get("outputs", [])
            in_offset = 1.0 if inputs else 0.0

            # 1. Place the machine centered within its building slot
            direction = DIRECTION_NORTH
            if machine == "chemical-plant":
                direction = DIRECTION_WEST
            elif machine == "oil-refinery":
                direction = DIRECTION_EAST
            ent = add_entity(
                machine,
                gx + in_offset + m_w / 2.0,
                gy + m_h / 2.0,
                direction=direction,
                extra=extra,
            )
            inst_to_entity[cname] = ent

            in_slots, out_slots = get_macro_port_slots(cell)

            # 2. Place input fast-inserters (pickup from West belt, drop into East machine)
            for port in sorted(inputs):
                if port in fluids:
                    continue
                cy = in_slots.get(port, 0) + 0.5
                add_entity("fast-inserter", gx + 0.5, gy + cy, direction=DIRECTION_WEST)
                occupied_positions.add((round(gx + 0.5, 2), round(gy + cy, 2)))

            # 3. Place output fast-inserters (pickup from West machine, drop onto East belt)
            for port in sorted(outputs):
                if port in fluids:
                    continue
                cy = out_slots.get(port, 0) + 0.5
                add_entity("fast-inserter", gx + cw - 0.5, gy + cy, direction=DIRECTION_WEST)
                occupied_positions.add((round(gx + cw - 0.5, 2), round(gy + cy, 2)))

            # 4. Mark machine core footprint as occupied
            for tx in range(m_w):
                for ty in range(m_h):
                    occupied_positions.add(
                        (round(gx + in_offset + tx + 0.5, 2), round(gy + ty + 0.5, 2))
                    )

    def choose_belt_tier(_span_tiles: int) -> str:
        """Select underground belt tier. Use tier 1 yellow belt."""
        return "underground-belt"

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
        if cell_id == "belt_split2":
            split_pins = {
                "a": (0.25, 0.5),
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
            _, out_slots = get_macro_port_slots(c)
            orig_outputs = c.get("outputs", [])
            idx = outputs.index(pin)
            orig_name = orig_outputs[idx] if idx < len(orig_outputs) else ""
            if orig_name in out_slots:
                return (width - 0.25, out_slots[orig_name] + 0.5)
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

    # Collect all pin interface tiles that must never be converted or deleted by jumpers/undergrounds
    pin_tiles: set[tuple[float, float]] = set()
    for p in def_data.get("pins", []):
        pin_tiles.add(
            (
                round(math.floor(p["x"] * scale) + 0.5, 2),
                round(math.floor(p["y"] * scale) + 0.5, 2),
            )
        )
    for comp in def_data.get("components", []):
        cname = comp["name"]
        macro = comp["macro"]
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", macro)
        cell = cell_map.get(cell_id, {})
        if cell_id in ("belt_merge2", "belt_split2", "belt_balancer2"):
            pin_tiles.add((round(gx - 0.5, 2), round(gy + 0.5, 2)))
            pin_tiles.add((round(gx - 0.5, 2), round(gy + 1.5, 2)))
            pin_tiles.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
            pin_tiles.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))
        elif cell_id == "pump":
            pin_tiles.add((round(gx + 0.5, 2), round(gy - 0.5, 2)))
            pin_tiles.add((round(gx + 0.5, 2), round(gy + 2.5, 2)))
        else:
            w = float(cell.get("width", 3))
            h = float(cell.get("height", 3))
            inputs = cell.get("inputs", [])
            outputs = cell.get("outputs", [])
            in_slots, out_slots = get_macro_port_slots(cell)
            for port in inputs:
                cy = in_slots.get(port, 0) + 0.5
                pin_tiles.add((round(gx + 0.5, 2), round(gy + cy, 2)))
                pin_tiles.add((round(gx - 0.5, 2), round(gy + cy, 2)))
            for port in outputs:
                cy = out_slots.get(port, 0) + 0.5
                pin_tiles.add((round(gx + w - 0.5, 2), round(gy + cy, 2)))
                pin_tiles.add((round(gx + w + 0.5, 2), round(gy + cy, 2)))

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

    # Track layer connectivity for turns
    perp_conn: dict[tuple[str, tuple[float, float]], set[str]] = collections.defaultdict(set)
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        for w in net.get("wires", []):
            if w.get("layer") in ("M5", "M6"):
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

            if layer in ("M5", "M6"):
                continue

            if layer in ("M3", "M4"):
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
                # Surface wire M1 / M2
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

    DIR_STEPS = {
        DIRECTION_EAST: (1, 0),
        DIRECTION_WEST: (-1, 0),
        DIRECTION_SOUTH: (0, 1),
        DIRECTION_NORTH: (0, -1),
    }
    net_map = {n.get("name", ""): n for n in def_data.get("nets", [])}

    # 2b. Refine surface belt directions: disabled (wire-graph Dijkstra in Step 2 preserves serpentine paths)
    for nname, tiles in {}.items():
        if not tiles:
            continue
        drivers: list[tuple[float, float]] = []
        if nname in input_pins:
            drivers.append(
                (
                    round(math.floor(input_pins[nname][0]) + 0.5, 2),
                    round(math.floor(input_pins[nname][1]) + 0.5, 2),
                )
            )
        for inst, pin in net_map.get(nname, {}).get("connections", []):
            if inst in comp_map:
                c = comp_map[inst]
                cell_id = inst_map.get(inst, {}).get("cell", c.get("macro", ""))
                gx = c["x"] * scale
                gy = c["y"] * scale
                if cell_id in ("belt_split2", "belt_balancer2"):
                    if pin == "y1":
                        drivers.append((round(gx + 1.5, 2), round(gy + 0.5, 2)))
                    elif pin == "y2":
                        drivers.append((round(gx + 1.5, 2), round(gy + 1.5, 2)))
                elif cell_id == "belt_merge2":
                    if pin == "y":
                        drivers.append((round(gx + 1.5, 2), round(gy + 0.5, 2)))
                elif cell_id == "pump":
                    if pin == "y":
                        drivers.append((round(gx + 0.5, 2), round(gy + 2.5, 2)))
                else:
                    w = float(cell_map.get(cell_id, {}).get("width", 3))
                    h = float(cell_map.get(cell_id, {}).get("height", 3))
                    outputs = cell_map.get(cell_id, {}).get("outputs", [])
                    out_names = [o.replace("-", "_") for o in outputs]
                    if pin in out_names:
                        idx = out_names.index(pin)
                        cy = (h - len(outputs)) // 2 + idx + 0.5 if len(outputs) <= h else idx + 0.5
                        drivers.append((round(gx + w + 0.5, 2), round(gy + cy, 2)))

        if not drivers:
            continue

        tile_dist: dict[tuple[float, float], int] = {}
        q = collections.deque()
        for drv in drivers:
            if drv in tiles:
                tile_dist[drv] = 0
                q.append(drv)
            else:
                closest = min(
                    tiles, key=lambda t: abs(t[0] - drv[0]) + abs(t[1] - drv[1]), default=None
                )
                if closest and (abs(closest[0] - drv[0]) + abs(closest[1] - drv[1])) <= 2:
                    tile_dist[closest] = 0
                    q.append(closest)

        ug_hops = collections.defaultdict(list)
        for cy, segs in m3_runs_by_net.get(nname, {}).items():
            for s1, s2, _ in segs:
                ug_hops[(round(s1, 2), cy)].append((round(s2, 2), cy))
        for cx, segs in m4_runs_by_net.get(nname, {}).items():
            for s1, s2, _ in segs:
                ug_hops[(cx, round(s1, 2))].append((cx, round(s2, 2)))

        while q:
            u = q.popleft()
            d_u = tile_dist[u]
            for dx, dy in DIR_STEPS.values():
                v = (round(u[0] + dx, 2), round(u[1] + dy, 2))
                if v in tiles and v not in tile_dist:
                    tile_dist[v] = d_u + 1
                    q.append(v)
            for v in ug_hops.get(u, []):
                if v in tiles and v not in tile_dist:
                    tile_dist[v] = d_u + int(abs(v[0] - u[0]) + abs(v[1] - u[1]))
                    q.append(v)

        for pos in list(tiles.keys()):
            if pos not in tile_dist:
                continue
            d_pos = tile_dist[pos]
            downstream = []
            for d_val, (dx, dy) in DIR_STEPS.items():
                npos = (round(pos[0] + dx, 2), round(pos[1] + dy, 2))
                if npos in tile_dist and tile_dist[npos] > d_pos:
                    downstream.append((d_val, npos))
            if downstream:
                cur_d = tiles[pos]
                if not any(d_val == cur_d for d_val, _ in downstream):
                    best_d, _ = max(downstream, key=lambda item: tile_dist[item[1]])
                    tiles[pos] = best_d

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

                if (ug_end - ug_start) * step < 2:
                    cur = ug_start
                    while (ug_end - cur) * step >= 0:
                        net_surface_tiles[nname][(cur, cy)] = d
                        tile_to_nets[(cur, cy)].add(nname)
                        cur = round(cur + step, 2)
                else:
                    max_allowed_span = 10 if is_fluid else 5
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

                if (ug_end - ug_start) * step < 2:
                    cur = ug_start
                    while (ug_end - cur) * step >= 0:
                        net_surface_tiles[nname][(cx, cur)] = d
                        tile_to_nets[(cx, cur)].add(nname)
                        cur = round(cur + step, 2)
                else:
                    max_allowed_span = 10 if is_fluid else 5
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

    DIR_STEPS = {
        DIRECTION_EAST: (1, 0),
        DIRECTION_WEST: (-1, 0),
        DIRECTION_SOUTH: (0, 1),
        DIRECTION_NORTH: (0, -1),
    }
    OPPOSITE_DIR = {
        DIRECTION_EAST: DIRECTION_WEST,
        DIRECTION_WEST: DIRECTION_EAST,
        DIRECTION_SOUTH: DIRECTION_NORTH,
        DIRECTION_NORTH: DIRECTION_SOUTH,
    }

    # 4. Resolve surface conflicts using legal straight-run jumpers
    jumpers: list[tuple[str, tuple[float, float], tuple[float, float], int, bool]] = []
    used_underground_endpoints: dict[tuple[float, float], int] = {}
    for ue in underground_entities:
        pos_u = (round(ue["position"]["x"], 2), round(ue["position"]["y"], 2))
        used_underground_endpoints[pos_u] = ue.get("direction", 0)

    while True:
        conflicts = [pos for pos, nets in tile_to_nets.items() if len(nets) > 1]
        resolved_any = False
        for pos in conflicts:
            if len(tile_to_nets[pos]) <= 1:
                continue
            cx, cy = pos
            net_list = list(tile_to_nets[pos])
            best_candidate = None
            best_jump = None
            best_score = -999999

            for candidate in net_list:
                d = net_surface_tiles[candidate].get(pos)
                if d not in DIR_STEPS:
                    continue
                dx, dy = DIR_STEPS[d]
                opp_d = OPPOSITE_DIR.get(d)
                is_fluid = is_fluid_wire(net_map.get(candidate, {}), "M1")
                max_allowed_span = 10 if is_fluid else 5

                for bspan in range(1, max_allowed_span):
                    in_pos = (round(cx - dx * bspan, 2), round(cy - dy * bspan, 2))
                    prev_pos = (round(in_pos[0] - dx, 2), round(in_pos[1] - dy, 2))
                    if (
                        in_pos not in net_surface_tiles[candidate]
                        or net_surface_tiles[candidate][in_pos] != d
                        or len(tile_to_nets[in_pos]) > 1
                        or in_pos in used_underground_endpoints
                        or in_pos in pin_tiles
                        or in_pos in occupied_positions
                        or used_underground_endpoints.get(prev_pos) == opp_d
                    ):
                        continue

                    if not is_fluid:
                        side_feed = False
                        for b_dir, (b_dx, b_dy) in DIR_STEPS.items():
                            if b_dir == d:
                                continue
                            n_pos = (round(in_pos[0] - b_dx, 2), round(in_pos[1] - b_dy, 2))
                            if net_surface_tiles[candidate].get(n_pos) == b_dir:
                                side_feed = True
                                break
                        if side_feed:
                            continue
                    for fspan in range(1, max_allowed_span):
                        if bspan + fspan > max_allowed_span:
                            break
                        out_pos = (round(cx + dx * fspan, 2), round(cy + dy * fspan, 2))
                        next_pos = (round(out_pos[0] + dx, 2), round(out_pos[1] + dy, 2))
                        if (
                            out_pos not in net_surface_tiles[candidate]
                            or net_surface_tiles[candidate][out_pos] != d
                            or len(tile_to_nets[out_pos]) > 1
                            or out_pos in used_underground_endpoints
                            or out_pos in pin_tiles
                            or out_pos in occupied_positions
                            or used_underground_endpoints.get(next_pos) == opp_d
                        ):
                            continue

                        # Verify straight run
                        valid_span = True
                        conflicts_cleared = 0
                        for s in range(-bspan + 1, fspan):
                            it = (round(cx + dx * s, 2), round(cy + dy * s, 2))
                            if (
                                (
                                    it in net_surface_tiles[candidate]
                                    and net_surface_tiles[candidate][it] != d
                                )
                                or it in occupied_positions
                                or it in used_underground_endpoints
                                or it in pin_tiles
                            ):
                                valid_span = False
                                break
                            if len(tile_to_nets.get(it, set())) > 1:
                                conflicts_cleared += 1
                        if not valid_span:
                            continue

                        penalty = 100 if len(tile_to_nets.get(next_pos, set())) > 1 else 0
                        score = conflicts_cleared * 10 - penalty - (bspan + fspan)
                        if score > best_score:
                            best_score = score
                            best_jump = (in_pos, out_pos, bspan, fspan, d, is_fluid)
                            best_candidate = candidate

            if best_jump and best_candidate:
                in_pos, out_pos, bspan, fspan, d, is_fluid = best_jump
                dx, dy = DIR_STEPS[d]
                used_underground_endpoints[in_pos] = d
                used_underground_endpoints[out_pos] = d
                jumpers.append((best_candidate, in_pos, out_pos, d, is_fluid))
                for s in range(-bspan + 1, fspan):
                    it = (round(cx + dx * s, 2), round(cy + dy * s, 2))
                    if it in net_surface_tiles[best_candidate]:
                        del net_surface_tiles[best_candidate][it]
                    if best_candidate in tile_to_nets[it]:
                        tile_to_nets[it].remove(best_candidate)
                resolved_any = True
        if not resolved_any:
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

    # 6.5. Enforce pin chirality: belts feeding input pins must point into the pin
    pin_directions: dict[tuple[float, float], int] = {}
    forbidden_west_tiles: set[tuple[float, float]] = set()

    for comp in def_data.get("components", []):
        cname = comp["name"]
        macro = comp["macro"]
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", macro)
        cell = cell_map.get(cell_id, {})
        if cell_id in ("belt_merge2", "belt_split2", "belt_balancer2"):
            pin_directions[(round(gx - 0.5, 2), round(gy + 0.5, 2))] = DIRECTION_EAST
            pin_directions[(round(gx - 0.5, 2), round(gy + 1.5, 2))] = DIRECTION_EAST
            forbidden_west_tiles.add((round(gx + 1.5, 2), round(gy + 0.5, 2)))
            forbidden_west_tiles.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))
        elif cell_id == "pump":
            pin_directions[(round(gx + 0.5, 2), round(gy - 0.5, 2))] = DIRECTION_NORTH
            pin_directions[(round(gx + 0.5, 2), round(gy + 2.5, 2))] = DIRECTION_NORTH
        else:
            w = float(cell.get("width", 3))
            h = float(cell.get("height", 3))
            inputs = cell.get("inputs", [])
            outputs = cell.get("outputs", [])
            in_slots, out_slots = get_macro_port_slots(cell)
            for port in inputs:
                cy = in_slots.get(port, 0) + 0.5
                pin_directions[(round(gx - 0.5, 2), round(gy + cy, 2))] = DIRECTION_EAST
            for port in outputs:
                cy = out_slots.get(port, 0) + 0.5
                forbidden_west_tiles.add((round(gx + w + 0.5, 2), round(gy + cy, 2)))

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
            grid_entities[pos]["direction"] = req_dir

    for pos in forbidden_west_tiles:
        if (
            pos in grid_entities
            and "transport-belt" in grid_entities[pos].get("name", "")
            and grid_entities[pos].get("direction") == DIRECTION_WEST
        ):
            grid_entities[pos]["direction"] = DIRECTION_EAST

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

    # 7.5. Power Distribution Network (PDN) generation
    mst_connections: list[tuple[tuple[float, float], tuple[float, float]]] = []
    pole_entities: dict[tuple[float, float], dict[str, Any]] = {}
    powered = [
        e
        for e in entities
        if any(
            k in e.get("name", "")
            for k in (
                "assembling-machine",
                "electric-furnace",
                "chemical-plant",
                "oil-refinery",
                "centrifuge",
                "rocket-silo",
                "inserter",
                "pump",
            )
        )
    ]
    if powered:
        surface_occupied = set(occupied_positions)
        for ent in entities:
            ep = ent["position"]
            surface_occupied.add((round(ep["x"], 2), round(ep["y"], 2)))
            ename = ent.get("name", "")
            if ename == "splitter":
                d = ent.get("direction", 0)
                if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                    surface_occupied.add((round(ep["x"] + 1.0, 2), round(ep["y"], 2)))
                else:
                    surface_occupied.add((round(ep["x"], 2), round(ep["y"] + 1.0, 2)))

        xs = [e["position"]["x"] for e in powered]
        ys = [e["position"]["y"] for e in powered]
        min_x, max_x = int(min(xs)) - 1, int(max(xs)) + 4
        min_y, max_y = int(min(ys)) - 1, int(max(ys)) + 4
        cands: list[tuple[float, float]] = []
        for x in range(min_x, max_x, 6):
            for y in range(min_y, max_y, 6):
                cand = (math.floor(x) + 0.5, math.floor(y) + 0.5)
                if cand in surface_occupied:
                    for dx in (0, 1, -1, 2, -2, 3, -3):
                        for dy in (0, 1, -1, 2, -2, 3, -3):
                            c2 = (cand[0] + dx, cand[1] + dy)
                            if c2 not in surface_occupied:
                                cand = c2
                                break
                        if cand not in surface_occupied:
                            break
                if cand not in surface_occupied:
                    cands.append(cand)

        def is_covered(ent: dict[str, Any], cand_pos: tuple[float, float]) -> bool:
            ex = ent["position"]["x"]
            ey = ent["position"]["y"]
            cx, cy = entity_collision_half_size(ent.get("name", ""))
            return (
                max(
                    max(0.0, abs(ex - cand_pos[0]) - cx),
                    max(0.0, abs(ey - cand_pos[1]) - cy),
                )
                <= 3.5
            )

        uncovered = set(range(len(powered)))
        chosen: set[tuple[float, float]] = set()
        while uncovered:
            best_cand = None
            best_covers: set[int] = set()
            for c in cands:
                if c in chosen:
                    continue
                covers = {i for i in uncovered if is_covered(powered[i], c)}
                if len(covers) > len(best_covers):
                    best_covers = covers
                    best_cand = c
            if not best_cand:
                break
            chosen.add(best_cand)
            uncovered -= best_covers

        for i in list(uncovered):
            px, py = powered[i]["position"]["x"], powered[i]["position"]["y"]
            cx, cy = entity_collision_half_size(powered[i].get("name", ""))
            reach = math.ceil(3.5 + max(cx, cy))
            found_cand = False
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    cand = (math.floor(px + dx) + 0.5, math.floor(py + dy) + 0.5)
                    if (
                        cand not in surface_occupied
                        and cand not in chosen
                        and is_covered(powered[i], cand)
                    ):
                        chosen.add(cand)
                        cands.append(cand)
                        uncovered.remove(i)
                        found_cand = True
                        break
                if found_cand:
                    break

        # Connect chosen poles through Steiner MST with maximum wire span 9.0 tiles
        cand_list = list(cands)
        cand_adj: dict[tuple[float, float], list[tuple[float, float]]] = {c: [] for c in cand_list}
        for i in range(len(cand_list)):
            for j in range(i + 1, len(cand_list)):
                if (
                    math.hypot(cand_list[i][0] - cand_list[j][0], cand_list[i][1] - cand_list[j][1])
                    <= 9.0
                ):
                    cand_adj[cand_list[i]].append(cand_list[j])
                    cand_adj[cand_list[j]].append(cand_list[i])

        all_poles = set(chosen)
        if chosen:
            connected = {next(iter(chosen))}
            unconnected = set(chosen) - connected

            while unconnected:
                dist: dict[tuple[float, float], int] = dict.fromkeys(connected, 0)
                parent: dict[tuple[float, float], tuple[float, float]] = {}
                pq: list[tuple[int, tuple[float, float]]] = [(0, p) for p in connected]
                target = None
                while pq:
                    d, u = heapq.heappop(pq)
                    if u in unconnected:
                        target = u
                        break
                    if d > dist.get(u, 1_000_000_000):
                        continue
                    for v in cand_adj.get(u, []):
                        if d + 1 < dist.get(v, 1_000_000_000):
                            dist[v] = d + 1
                            parent[v] = u
                            heapq.heappush(pq, (d + 1, v))
                if not target:
                    break
                curr = target
                while curr not in connected:
                    all_poles.add(curr)
                    connected.add(curr)
                    curr = parent[curr]
                unconnected.remove(target)

        for p in sorted(all_poles):
            pole_ent = add_entity("medium-electric-pole", p[0], p[1])
            pole_entities[p] = pole_ent
            surface_occupied.add(p)

        pole_list = sorted(all_poles)
        pole_edges: list[tuple[float, int, int]] = []
        for i in range(len(pole_list)):
            for j in range(i + 1, len(pole_list)):
                d = math.hypot(
                    pole_list[i][0] - pole_list[j][0],
                    pole_list[i][1] - pole_list[j][1],
                )
                if d <= 9.0:
                    pole_edges.append((d, i, j))
        pole_edges.sort()
        parent_mst = list(range(len(pole_list)))

        def find_root(node: int) -> int:
            if parent_mst[node] == node:
                return node
            parent_mst[node] = find_root(parent_mst[node])
            return parent_mst[node]

        for _d, u, v in pole_edges:
            ru, rv = find_root(u), find_root(v)
            if ru != rv:
                parent_mst[ru] = rv
                mst_connections.append((pole_list[u], pole_list[v]))

    unpowered_ents = check_power(entities)
    if unpowered_ents:
        raise RuntimeError(
            f"Power check failed: {len(unpowered_ents)} entities lack electric pole coverage: {unpowered_ents[:5]}"
        )

    collision_pairs = check_collisions(entities)
    if collision_pairs:
        raise RuntimeError(
            f"Collision check failed: {len(collision_pairs)} overlapping entity pairs detected"
        )

    unpaired_hoods = check_underground_pairing(entities)
    if unpaired_hoods:
        raise RuntimeError(
            f"Underground check failed: {len(unpaired_hoods)} unpaired underground hoods detected: {unpaired_hoods}"
        )

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

    wires: list[list[int]] = []
    for p1, p2 in mst_connections:
        ent1 = pole_entities.get(p1)
        ent2 = pole_entities.get(p2)
        if ent1 and ent2 and "entity_number" in ent1 and "entity_number" in ent2:
            wires.append(
                [
                    ent1["entity_number"],
                    WIRE_CONNECTOR_POLE_COPPER,
                    ent2["entity_number"],
                    WIRE_CONNECTOR_POLE_COPPER,
                ]
            )

    return entities, wires


def extract_pins(
    def_data: dict[str, Any], netlist_data: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Extract input and output pin coordinates and types by material name."""
    scale = 1.0 / float(def_data.get("units", 1000))
    fluids = set(netlist_data.get("fluids") or []) | {
        "crude-oil",
        "water",
        "petroleum-gas",
        "heavy-oil",
        "light-oil",
        "sulfuric-acid",
        "lubricant",
        "steam",
    }
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []

    known_materials = list(netlist_data.get("inputs", [])) + list(netlist_data.get("outputs", []))

    for pin in def_data.get("pins", []):
        raw_net = pin.get("net", pin["name"])
        direction = pin.get("direction", "INPUT").upper()

        mat = raw_net.replace("_", "-")
        base_net = re.sub(r"(__|--)bus\d+$", "", raw_net)
        base_mat = base_net.replace("_", "-")
        for known in known_materials:
            known_base = re.sub(r"(__|--)bus\d+$", "", known).replace("_", "-")
            if known == base_mat or known.replace("-", "_") == base_net or known_base == base_mat:
                mat = known_base
                break

        x = math.floor(pin["x"] * scale) + 0.5
        y = math.floor(pin["y"] * scale) + 0.5
        pin_type = "pipe" if mat in fluids else "belt"

        entry = {
            "name": mat,
            "x": x,
            "y": y,
            "type": pin_type,
        }
        if direction == "INPUT":
            inputs.append(entry)
        elif direction == "OUTPUT":
            outputs.append(entry)

    inputs.sort(key=lambda p: (p["name"], p["x"], p["y"]))
    outputs.sort(key=lambda p: (p["name"], p["x"], p["y"]))

    return {
        "inputs": inputs,
        "outputs": outputs,
    }


def def_to_blueprint(def_text: str, netlist_data: dict[str, Any], label: str = "factory") -> str:
    """Convert DEF text and netlist metadata into a blueprint exchange string."""
    def_data = parse_def(def_text)
    entities, wires = build_entities(def_data, netlist_data)
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

    args = parser.parse_args()

    def_text = args.def_file.read_text()
    netlist_data = json.loads(args.netlist_file.read_text())

    def_data = parse_def(def_text)
    entities, wires = build_entities(def_data, netlist_data)

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
        output_text = json.dumps(document, indent=2)
    else:
        output_text = blueprint.encode(entities, args.label, wires=wires)

    if args.out:
        args.out.write_text(output_text + "\n")
    else:
        sys.stdout.write(output_text + "\n")

    if args.pins_out:
        pins_data = extract_pins(def_data, netlist_data)
        args.pins_out.parent.mkdir(parents=True, exist_ok=True)
        args.pins_out.write_text(json.dumps(pins_data, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
