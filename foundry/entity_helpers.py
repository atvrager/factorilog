"""Helper functions for entity geometry, power, collisions, and belt pruning.

Provides bounding box calculation, collision checks, electric pole checks,
and dead-end belt removal.
"""

from __future__ import annotations

import math
from typing import Any

# Factorio 2.0 direction values: North=0, East=4, South=8, West=12.
DIRECTION_NORTH = 0
DIRECTION_EAST = 4
DIRECTION_SOUTH = 8
DIRECTION_WEST = 12

DIR_STEPS: dict[int, tuple[int, int]] = {
    DIRECTION_EAST: (1, 0),
    DIRECTION_WEST: (-1, 0),
    DIRECTION_SOUTH: (0, 1),
    DIRECTION_NORTH: (0, -1),
}

OPPOSITE_DIR: dict[int, int] = {
    DIRECTION_NORTH: DIRECTION_SOUTH,
    DIRECTION_SOUTH: DIRECTION_NORTH,
    DIRECTION_EAST: DIRECTION_WEST,
    DIRECTION_WEST: DIRECTION_EAST,
}

INSERTER_OFFSETS: dict[int, tuple[tuple[int, int], tuple[int, int]]] = {
    DIRECTION_NORTH: ((0, -1), (0, 1)),
    DIRECTION_EAST: ((1, 0), (-1, 0)),
    DIRECTION_SOUTH: ((0, 1), (0, -1)),
    DIRECTION_WEST: ((-1, 0), (1, 0)),
}


def _segment_direction(x1: int, y1: int, x2: int, y2: int) -> int:
    """Determine Factorio 2.0 cardinal direction from endpoint coordinates."""
    if x1 == x2:
        return DIRECTION_SOUTH if y2 >= y1 else DIRECTION_NORTH
    return DIRECTION_EAST if x2 >= x1 else DIRECTION_WEST


_PROTOTYPE_BOXES: dict[str, tuple[float, float]] = {}
_PROTOTYPE_COLLISIONS: dict[str, tuple[float, float]] = {}


def entity_bounding_box(proto: dict[str, Any]) -> tuple[float, float]:
    """Derive entity width and height from prototype collision box."""
    box = proto.get("collision_box", [[-0.4, -0.4], [0.4, 0.4]])
    w = math.ceil(float(box[1][0]) - float(box[0][0]))
    h = math.ceil(float(box[1][1]) - float(box[0][1]))
    return float(max(1, w)), float(max(1, h))


def register_prototype_boxes(prototypes: dict[str, Any]) -> None:
    """Register prototype dimensions and collision half-sizes from prototype data."""
    for key, value in prototypes.items():
        if isinstance(value, dict):
            if "collision_box" in value:
                box = value["collision_box"]
                if len(box) >= 2:
                    w, h = entity_bounding_box(value)
                    _PROTOTYPE_BOXES[key] = (w, h)
                    _PROTOTYPE_COLLISIONS[key] = (
                        abs(float(box[1][0])),
                        abs(float(box[1][1])),
                    )
            else:
                for sub_key, sub_val in value.items():
                    if isinstance(sub_val, dict) and "collision_box" in sub_val:
                        box = sub_val["collision_box"]
                        if len(box) >= 2:
                            w, h = entity_bounding_box(sub_val)
                            _PROTOTYPE_BOXES[sub_key] = (w, h)
                            _PROTOTYPE_COLLISIONS[sub_key] = (
                                abs(float(box[1][0])),
                                abs(float(box[1][1])),
                            )


def entity_size(name: str) -> tuple[float, float]:
    """Return width and height of named entity."""
    if name in _PROTOTYPE_BOXES:
        return _PROTOTYPE_BOXES[name]
    for key, box in _PROTOTYPE_BOXES.items():
        if key in name or name in key:
            return box
    if "rocket-silo" in name:
        return 9.0, 9.0
    if "oil-refinery" in name or "nuclear-reactor" in name:
        return 5.0, 5.0
    if (
        "assembling-machine" in name
        or "electric-furnace" in name
        or "chemical-plant" in name
        or "centrifuge" in name
        or "lab" in name
    ):
        return 3.0, 3.0
    if (
        "steel-furnace" in name
        or "stone-furnace" in name
        or "power-switch" in name
        or "boiler" in name
    ):
        return 2.0, 2.0
    if "pump" in name:
        return 1.0, 2.0
    return 1.0, 1.0


def entity_collision_half_size(name: str) -> tuple[float, float]:
    """Return half width and half height of entity collision box."""
    if name in _PROTOTYPE_COLLISIONS:
        return _PROTOTYPE_COLLISIONS[name]
    for key, coll in _PROTOTYPE_COLLISIONS.items():
        if key in name or name in key:
            return coll
    if "rocket-silo" in name:
        return 4.2, 4.2
    if "oil-refinery" in name:
        return 2.2, 2.2
    if (
        "assembling-machine" in name
        or "electric-furnace" in name
        or "chemical-plant" in name
        or "centrifuge" in name
        or "lab" in name
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
                "combinator",
                "lab",
                "radar",
            )
        )
    ]
    unpowered = []
    for e in powered:
        ex = e["position"]["x"]
        ey = e["position"]["y"]
        cx, cy = entity_collision_half_size(e.get("name", ""))
        max_dist = supply_radius + max(cx, cy)
        covered = False
        for p in poles:
            px = p["position"]["x"]
            py = p["position"]["y"]
            if abs(ex - px) > max_dist or abs(ey - py) > max_dist:
                continue
            if (
                max(
                    max(0.0, abs(ex - px) - cx),
                    max(0.0, abs(ey - py) - cy),
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


def prune_dead_end_belts(
    entities: list[dict[str, Any]],
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prune dead-end stub belts that do not reach any downstream sink."""
    try:
        from foundry.lef import FLUID_MATERIALS, get_macro_port_slots
    except ImportError:
        from lef import FLUID_MATERIALS, get_macro_port_slots

    scale = 1.0 / float(def_data.get("units", 1000))
    fluids = set(netlist_data.get("fluids") or []) | set(FLUID_MATERIALS)

    output_pins: set[tuple[float, float]] = set()
    for pin in def_data.get("pins", []):
        if pin.get("direction") == "OUTPUT":
            px = math.floor(pin["x"] * scale) + 0.5
            py = math.floor(pin["y"] * scale) + 0.5
            output_pins.add((round(px, 1), round(py, 1)))

    splitter_entry_targets: set[tuple[float, float, int]] = set()
    for e in entities:
        if e.get("name") == "splitter":
            sx, sy = e["position"]["x"], e["position"]["y"]
            d = e.get("direction", DIRECTION_NORTH)
            if d == DIRECTION_EAST:
                splitter_entry_targets.add((round(sx, 1), round(sy - 0.5, 1), DIRECTION_EAST))
                splitter_entry_targets.add((round(sx, 1), round(sy + 0.5, 1), DIRECTION_EAST))
            elif d == DIRECTION_WEST:
                splitter_entry_targets.add((round(sx, 1), round(sy - 0.5, 1), DIRECTION_WEST))
                splitter_entry_targets.add((round(sx, 1), round(sy + 0.5, 1), DIRECTION_WEST))
            elif d == DIRECTION_NORTH:
                splitter_entry_targets.add((round(sx - 0.5, 1), round(sy, 1), DIRECTION_NORTH))
                splitter_entry_targets.add((round(sx + 0.5, 1), round(sy, 1), DIRECTION_NORTH))
            elif d == DIRECTION_SOUTH:
                splitter_entry_targets.add((round(sx - 0.5, 1), round(sy, 1), DIRECTION_SOUTH))
                splitter_entry_targets.add((round(sx + 0.5, 1), round(sy, 1), DIRECTION_SOUTH))

    ug_entry_targets: set[tuple[float, float, int]] = set()
    for e in entities:
        if "underground-belt" in e.get("name", "") and e.get("type") == "input":
            ux, uy = e["position"]["x"], e["position"]["y"]
            d = e.get("direction", DIRECTION_NORTH)
            ug_entry_targets.add((round(ux, 1), round(uy, 1), d))

    protected_inserter_tiles: set[tuple[float, float]] = set()
    for comp in def_data.get("components", []):
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = {i["name"]: i for i in netlist_data.get("instances", [])}.get(comp["name"], {})
        cell_id = inst.get("cell", comp["macro"])
        if cell_id in ("belt_split2", "belt_merge2", "belt_balancer2"):
            continue
        cell = netlist_data.get("cells", {}).get(cell_id, {})
        inputs = cell.get("inputs", [])
        outputs = cell.get("outputs", [])
        w = float(cell.get("width", 3))
        h = float(cell.get("height", 3))
        in_slots, out_slots = get_macro_port_slots(cell)
        for idx, port in enumerate(sorted(inputs)):
            if port in fluids:
                continue
            cy = in_slots.get(port, (h - len(inputs)) // 2 + idx if len(inputs) <= h else idx) + 0.5
            protected_inserter_tiles.add((round(gx - 0.5, 1), round(gy + cy, 1)))
        for idx, port in enumerate(sorted(outputs)):
            if port in fluids:
                continue
            cy = (
                out_slots.get(port, (h - len(outputs)) // 2 + idx if len(outputs) <= h else idx)
                + 0.5
            )
            protected_inserter_tiles.add((round(gx + w + 0.5, 1), round(gy + cy, 1)))

    for e in entities:
        if "inserter" in e.get("name", ""):
            d = e.get("direction", DIRECTION_NORTH)
            mult = 2 if "long-handed" in e.get("name", "") else 1
            offsets = INSERTER_OFFSETS.get(d, ((-1, 0), (1, 0)))
            p_dx = offsets[0][0] * mult
            p_dy = offsets[0][1] * mult
            pos = e.get("position", {})
            cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            pu_pos = (round(cx + p_dx, 1), round(cy + p_dy, 1))
            protected_inserter_tiles.add(pu_pos)

    if not (output_pins or protected_inserter_tiles or splitter_entry_targets or ug_entry_targets):
        return entities

    dirs = {
        DIRECTION_NORTH: (0, -1),
        DIRECTION_EAST: (1, 0),
        DIRECTION_SOUTH: (0, 1),
        DIRECTION_WEST: (-1, 0),
    }
    opp = {
        DIRECTION_NORTH: DIRECTION_SOUTH,
        DIRECTION_SOUTH: DIRECTION_NORTH,
        DIRECTION_EAST: DIRECTION_WEST,
        DIRECTION_WEST: DIRECTION_EAST,
    }

    belt_map: dict[tuple[float, float], dict[str, Any]] = {
        (round(e["position"]["x"], 1), round(e["position"]["y"], 1)): e
        for e in entities
        if e.get("name") == "transport-belt"
    }

    while True:
        to_remove = set()
        for pos, ent in belt_map.items():
            if pos in output_pins or pos in protected_inserter_tiles:
                continue
            d = ent.get("direction", DIRECTION_NORTH)
            if d not in dirs:
                continue
            dx, dy = dirs[d]
            target = (round(pos[0] + dx, 1), round(pos[1] + dy, 1))
            if (target[0], target[1], d) in splitter_entry_targets:
                continue
            if (target[0], target[1], d) in ug_entry_targets:
                continue
            if target in belt_map:
                td = belt_map[target].get("direction", DIRECTION_NORTH)
                if td != opp[d]:
                    continue
            to_remove.add(pos)
        if not to_remove:
            break
        for pos in to_remove:
            del belt_map[pos]

    clean_entities = [e for e in entities if e.get("name") != "transport-belt"]
    clean_entities.extend(belt_map.values())
    return clean_entities
