"""Component and inserter placement from DEF and netlist data.

Places standard cells, splitters, pumps, combinators, machines, and inserters
on the Factorio tile grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from foundry.entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
    from foundry.lef import FLUID_MATERIALS, get_macro_port_geometry
except ImportError:
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
    from lef import FLUID_MATERIALS, get_macro_port_geometry


@dataclass
class PlacementResult:
    """Entities and occupancy resulting from component placement."""

    entities: list[dict[str, Any]]
    occupied_positions: set[tuple[float, float]]
    grid_entities: dict[tuple[float, float], dict[str, Any]]
    inst_to_entity: dict[str, dict[str, Any]]
    fluids: set[str]
    has_fluid_metadata: bool


def place_components(def_data: dict[str, Any], netlist_data: dict[str, Any]) -> PlacementResult:
    """Place all macro components, inserters, and control entities."""
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
            recipe = inst.get("recipe", "")
            if recipe:
                sig_type = (
                    "fluid"
                    if recipe
                    in (
                        "crude-oil",
                        "petroleum-gas",
                        "heavy-oil",
                        "light-oil",
                        "sulfuric-acid",
                        "water",
                        "steam",
                    )
                    else "item"
                )
                default_cb = {
                    "arithmetic_conditions": {
                        "first_signal": {"type": sig_type, "name": recipe},
                        "operation": "+",
                        "second_constant": 0,
                        "output_signal": {"type": sig_type, "name": recipe},
                    }
                }
            else:
                default_cb = {
                    "arithmetic_conditions": {
                        "first_signal": {"type": "virtual", "name": "signal-each"},
                        "operation": "+",
                        "second_constant": 0,
                        "output_signal": {"type": "virtual", "name": "signal-each"},
                    }
                }
            cb = inst.get("control_behavior") or default_cb
            extra = {"control_behavior": cb}
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
            cb = inst.get("control_behavior") or {
                "decider_conditions": {
                    "first_signal": {"type": "virtual", "name": "signal-each"},
                    "comparator": ">",
                    "constant": 0,
                    "output_signal": {"type": "virtual", "name": "signal-each"},
                    "copy_count_from_input": True,
                }
            }
            extra = {"control_behavior": cb}
            ent = add_entity(
                "decider-combinator", center_x, center_y, direction=DIRECTION_NORTH, extra=extra
            )
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))
        elif macro == "constant_combinator" or cell_id == "constant_combinator":
            center_x, center_y = gx + 0.5, gy + 0.5
            cb = inst.get("control_behavior")
            extra = {"control_behavior": cb} if cb else None
            ent = add_entity(
                "constant-combinator", center_x, center_y, direction=DIRECTION_NORTH, extra=extra
            )
            inst_to_entity[cname] = ent
            occupied_positions.add((round(gx + 0.5, 2), round(gy + 0.5, 2)))
        elif macro == "power_switch" or cell_id == "power_switch":
            cw, ch = 2, 2
            center_x, center_y = gx + 1.0, gy + 1.0
            cb = inst.get("control_behavior") or {
                "circuit_enabled": True,
                "circuit_condition": inst.get(
                    "circuit_condition",
                    {
                        "first_signal": {"type": "virtual", "name": "signal-T"},
                        "comparator": "<",
                        "constant": 30,
                    },
                ),
            }
            extra = {
                "control_behavior": cb,
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
            in_offset = 1.0 if (cw > m_w) else 0.0
            y_offset = 1.0 if (ch > m_h) else 0.0

            direction = int(cell.get("direction", DIRECTION_NORTH))
            ent = add_entity(
                machine,
                gx + in_offset + m_w / 2.0,
                gy + y_offset + m_h / 2.0,
                direction=direction,
                extra=extra,
            )
            inst_to_entity[cname] = ent

            for port in sorted(inputs):
                if port in fluids:
                    continue
                geom = get_macro_port_geometry(cell, port, is_output=False)
                ix, iy = geom["inserter_pos"]
                idir = geom["inserter_dir"]
                add_entity("fast-inserter", gx + ix, gy + iy, direction=idir)
                occupied_positions.add((round(gx + ix, 2), round(gy + iy, 2)))

            for port in sorted(outputs):
                if port in fluids:
                    continue
                geom = get_macro_port_geometry(cell, port, is_output=True)
                ix, iy = geom["inserter_pos"]
                idir = geom["inserter_dir"]
                add_entity("fast-inserter", gx + ix, gy + iy, direction=idir)
                occupied_positions.add((round(gx + ix, 2), round(gy + iy, 2)))

            for tx in range(m_w):
                for ty in range(m_h):
                    occupied_positions.add(
                        (round(gx + in_offset + tx + 0.5, 2), round(gy + y_offset + ty + 0.5, 2))
                    )

    return PlacementResult(
        entities=entities,
        occupied_positions=occupied_positions,
        grid_entities=grid_entities,
        inst_to_entity=inst_to_entity,
        fluids=fluids,
        has_fluid_metadata=has_fluid_metadata,
    )
