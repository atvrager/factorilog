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
    from foundry.lef import FLUID_MATERIALS, get_macro_port_slots
except ImportError:
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
    from lef import FLUID_MATERIALS, get_macro_port_slots


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
        elif (
            cell.get("compound") or "gear_belt_combo" in cell_id or "gear_inserter_combo" in cell_id
        ):
            machine = cell.get("machine", "assembling-machine-3")
            # 1. Machine 1: iron-gear-wheel at (gx + 3.5, gy + 1.5)
            ent1 = add_entity(
                machine,
                gx + 3.5,
                gy + 1.5,
                direction=DIRECTION_NORTH,
                extra={"recipe": "iron-gear-wheel"},
            )
            inst_to_entity[f"{cname}__m1"] = ent1
            for tx in range(3):
                for ty in range(3):
                    occupied_positions.add((round(gx + 2.0 + tx + 0.5, 2), round(gy + ty + 0.5, 2)))

            # 2. Machine 2: transport-belt or inserter at (gx + 3.5, gy + 5.5)
            m2_recipe = "transport-belt" if "belt" in cell_id else "inserter"
            ent2 = add_entity(
                machine,
                gx + 3.5,
                gy + 5.5,
                direction=DIRECTION_NORTH,
                extra={"recipe": m2_recipe},
            )
            inst_to_entity[cname] = ent2
            for tx in range(3):
                for ty in range(3):
                    occupied_positions.add(
                        (round(gx + 2.0 + tx + 0.5, 2), round(gy + 4.0 + ty + 0.5, 2))
                    )

            # 3. Direct handoff inserter from Machine 1 to Machine 2 at (gx + 3.5, gy + 3.5)
            add_entity("fast-inserter", gx + 3.5, gy + 3.5, direction=DIRECTION_NORTH)
            occupied_positions.add((round(gx + 3.5, 2), round(gy + 3.5, 2)))

            # 4. Internal transport belt for iron-plate from y = 1.5 to y = 5.5 at x = gx + 0.5
            for ty in (1, 2, 3, 4, 5):
                add_entity("transport-belt", gx + 0.5, gy + ty + 0.5, direction=DIRECTION_SOUTH)
                occupied_positions.add((round(gx + 0.5, 2), round(gy + ty + 0.5, 2)))

            # 5. Feed inserter for Machine 1 (iron-plate) at (gx + 1.5, gy + 1.5)
            add_entity("fast-inserter", gx + 1.5, gy + 1.5, direction=DIRECTION_WEST)
            occupied_positions.add((round(gx + 1.5, 2), round(gy + 1.5, 2)))

            # 6. Feed inserter for Machine 2 (iron-plate) at (gx + 1.5, gy + 5.5)
            add_entity("fast-inserter", gx + 1.5, gy + 5.5, direction=DIRECTION_WEST)
            occupied_positions.add((round(gx + 1.5, 2), round(gy + 5.5, 2)))

            # 7. Feed inserter for electronic-circuit at (gx + 1.5, gy + 6.5) if inserter combo
            if "inserter" in cell_id:
                add_entity("transport-belt", gx + 0.5, gy + 6.5, direction=DIRECTION_EAST)
                occupied_positions.add((round(gx + 0.5, 2), round(gy + 6.5, 2)))
                add_entity("fast-inserter", gx + 1.5, gy + 6.5, direction=DIRECTION_WEST)
                occupied_positions.add((round(gx + 1.5, 2), round(gy + 6.5, 2)))

            # 8. Output inserter for Machine 2 at (gx + 5.5, gy + 5.5)
            add_entity("fast-inserter", gx + 5.5, gy + 5.5, direction=DIRECTION_WEST)
            occupied_positions.add((round(gx + 5.5, 2), round(gy + 5.5, 2)))
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

            for port in sorted(inputs):
                if port in fluids:
                    continue
                cy = in_slots.get(port, 0) + 0.5
                add_entity("fast-inserter", gx + 0.5, gy + cy, direction=DIRECTION_WEST)
                occupied_positions.add((round(gx + 0.5, 2), round(gy + cy, 2)))

            for port in sorted(outputs):
                if port in fluids:
                    continue
                cy = out_slots.get(port, 0) + 0.5
                add_entity("fast-inserter", gx + cw - 0.5, gy + cy, direction=DIRECTION_WEST)
                occupied_positions.add((round(gx + cw - 0.5, 2), round(gy + cy, 2)))

            for tx in range(m_w):
                for ty in range(m_h):
                    occupied_positions.add(
                        (round(gx + in_offset + tx + 0.5, 2), round(gy + ty + 0.5, 2))
                    )

    return PlacementResult(
        entities=entities,
        occupied_positions=occupied_positions,
        grid_entities=grid_entities,
        inst_to_entity=inst_to_entity,
        fluids=fluids,
        has_fluid_metadata=has_fluid_metadata,
    )
