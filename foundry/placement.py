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
    from foundry.lef import get_macro_port_geometry
except ImportError:
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
    from lef import get_macro_port_geometry


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
    fluids = set(netlist_data.get("fluids") or [])

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
            orient = comp.get("orient", "N")
            cw, ch = 3, 3
            if orient == "W":
                # Flow North
                ent = add_entity("splitter", gx + 1.0, gy + 2.5, direction=DIRECTION_NORTH)
                add_entity("transport-belt", gx + 0.5, gy + 1.5, direction=DIRECTION_NORTH)
                add_entity("transport-belt", gx + 0.5, gy + 0.5, direction=DIRECTION_EAST)
                add_entity("transport-belt", gx + 1.5, gy + 1.5, direction=DIRECTION_EAST)
                add_entity("transport-belt", gx + 2.5, gy + 1.5, direction=DIRECTION_NORTH)
                add_entity("transport-belt", gx + 2.5, gy + 0.5, direction=DIRECTION_WEST)
                add_entity("transport-belt", gx + 1.5, gy + 0.5, direction=DIRECTION_NORTH)
            elif orient == "E":
                # Flow South
                ent = add_entity("splitter", gx + 1.0, gy + 0.5, direction=DIRECTION_SOUTH)
                add_entity("transport-belt", gx + 0.5, gy + 1.5, direction=DIRECTION_SOUTH)
                add_entity("transport-belt", gx + 0.5, gy + 2.5, direction=DIRECTION_EAST)
                add_entity("transport-belt", gx + 1.5, gy + 1.5, direction=DIRECTION_EAST)
                add_entity("transport-belt", gx + 2.5, gy + 1.5, direction=DIRECTION_SOUTH)
                add_entity("transport-belt", gx + 2.5, gy + 2.5, direction=DIRECTION_WEST)
                add_entity("transport-belt", gx + 1.5, gy + 2.5, direction=DIRECTION_SOUTH)
            elif orient == "S":
                # Flow West
                ent = add_entity("splitter", gx + 2.5, gy + 1.0, direction=DIRECTION_WEST)
                add_entity("transport-belt", gx + 1.5, gy + 0.5, direction=DIRECTION_WEST)
                add_entity("transport-belt", gx + 0.5, gy + 0.5, direction=DIRECTION_SOUTH)
                add_entity("transport-belt", gx + 1.5, gy + 1.5, direction=DIRECTION_SOUTH)
                add_entity("transport-belt", gx + 1.5, gy + 2.5, direction=DIRECTION_WEST)
                add_entity("transport-belt", gx + 0.5, gy + 2.5, direction=DIRECTION_NORTH)
                add_entity("transport-belt", gx + 0.5, gy + 1.5, direction=DIRECTION_WEST)
            else:
                # Default orient == "N": Flow East
                ent = add_entity("splitter", gx + 0.5, gy + 1.0, direction=DIRECTION_EAST)
                add_entity("transport-belt", gx + 1.5, gy + 0.5, direction=DIRECTION_EAST)
                add_entity("transport-belt", gx + 2.5, gy + 0.5, direction=DIRECTION_SOUTH)
                add_entity("transport-belt", gx + 1.5, gy + 1.5, direction=DIRECTION_SOUTH)
                add_entity("transport-belt", gx + 1.5, gy + 2.5, direction=DIRECTION_EAST)
                add_entity("transport-belt", gx + 2.5, gy + 2.5, direction=DIRECTION_NORTH)
                add_entity("transport-belt", gx + 2.5, gy + 1.5, direction=DIRECTION_EAST)
            inst_to_entity[cname] = ent
            for tx in range(cw):
                for ty in range(ch):
                    occupied_positions.add((round(gx + tx + 0.5, 2), round(gy + ty + 0.5, 2)))
        elif macro in ("belt_split2", "belt_balancer2") or cell_id in (
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
                sig_type = "fluid" if recipe in fluids else "item"
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
            recipe = cell.get("fixed_recipe") or inst.get("recipe", cell.get("recipe", ""))
            extra: dict[str, Any] = {}
            if recipe and machine not in ("boiler", "steam-engine"):
                extra["recipe"] = recipe
            if machine == "steam-engine":
                extra["power_domain"] = "aon"
            if cell.get("landing_pad"):
                extra["launch_to_orbit_automatically"] = True
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
                extra=extra or None,
            )
            inst_to_entity[cname] = ent

            def _select_port_inserter(
                inst_rec: dict[str, Any],
                cell_rec: dict[str, Any],
                port_name: str,
                is_out: bool,
            ) -> tuple[str, dict[str, Any] | None]:
                tiers = cell_rec.get("inserter_tiers") or []
                default_name = str(tiers[-1]["name"]) if tiers else "fast-inserter"
                plan_r = float(inst_rec.get("plan_rate_per_min", 0.0))
                rated_r = float(inst_rec.get("rated_per_min", 0.0))
                if (
                    not tiers
                    or plan_r <= 0.0
                    or rated_r <= 0.0
                    or plan_r >= 0.35 * rated_r
                    or cell_rec.get("landing_pad")
                ):
                    return default_name, None
                amt_map = (
                    cell_rec.get("out_amounts") if is_out else cell_rec.get("in_amounts")
                ) or {}
                port_amt = float(amt_map.get(port_name, 1.0))
                req_rate = 1.40 * plan_r * port_amt
                candidates: list[tuple[float, str, int, int]] = []
                for t_entry in tiers:
                    t_name = str(t_entry.get("name", default_name))
                    max_s = int(t_entry.get("max_stack", 3))
                    rate_key = "out_rates_by_stack" if is_out else "rates_by_stack"
                    rate_map = t_entry.get(rate_key) or t_entry.get("rates_by_stack") or {}
                    for s_key, s_rate in rate_map.items():
                        candidates.append((float(s_rate), t_name, int(s_key), max_s))
                candidates.sort(key=lambda c: (c[0], c[2]))
                for c_rate, c_name, c_stack, c_max_s in candidates:
                    if c_rate >= req_rate:
                        ins_extra = {"override_stack_size": c_stack} if c_stack < c_max_s else None
                        return c_name, ins_extra
                return default_name, None

            for port in sorted(inputs):
                if port in fluids:
                    continue
                geom = get_macro_port_geometry(cell, port, is_output=False)
                ix, iy = geom["inserter_pos"]
                idir = geom["inserter_dir"]
                ins_name, ins_extra = _select_port_inserter(inst, cell, port, is_out=False)
                if machine in ("boiler", "steam-engine"):
                    ins_extra = dict(ins_extra or {})
                    ins_extra["power_domain"] = "aon"
                add_entity(ins_name, gx + ix, gy + iy, direction=idir, extra=ins_extra)
                occupied_positions.add((round(gx + ix, 2), round(gy + iy, 2)))

            pad_name = cell.get("landing_pad")
            if pad_name:
                pad_w = int(cell.get("landing_pad_width", 8))
                pad_h = int(cell.get("landing_pad_height", 8))
                pad_grid = int(cell.get("landing_pad_grid_size", 2))
                pad_left = round(gx + in_offset + m_w)
                if ((pad_left + pad_w // 2) % pad_grid) != 0:
                    pad_left += 1
                pad_bottom = round(gy)
                if ((pad_bottom + pad_h // 2) % pad_grid) != 0:
                    pad_bottom += 1
                pad_cx = float(pad_left) + pad_w / 2.0
                pad_cy = float(pad_bottom) + pad_h / 2.0
                add_entity(str(pad_name), pad_cx, pad_cy)
                for tx in range(pad_w):
                    for ty in range(pad_h):
                        occupied_positions.add(
                            (round(pad_left + tx + 0.5, 2), round(pad_bottom + ty + 0.5, 2))
                        )
                pad_right = pad_left + pad_w
                for port in sorted(outputs):
                    if port in fluids:
                        continue
                    geom = get_macro_port_geometry(cell, port, is_output=True)
                    ix, iy = geom["inserter_pos"]
                    idir = geom["inserter_dir"]
                    ins_x = float(pad_right) + 0.5
                    ins_y = gy + iy
                    ins_name, ins_extra = _select_port_inserter(inst, cell, port, is_out=True)
                    add_entity(ins_name, ins_x, ins_y, direction=idir, extra=ins_extra)
                    occupied_positions.add((round(ins_x, 2), round(ins_y, 2)))
                    out_x = gx + ix
                    if round(ins_x, 2) < round(out_x, 2):
                        bdir = int(geom.get("approach_dir", DIRECTION_EAST))
                        add_entity("transport-belt", out_x, ins_y, direction=bdir)
                        occupied_positions.add((round(out_x, 2), round(ins_y, 2)))
            else:
                for port in sorted(outputs):
                    if port in fluids:
                        continue
                    geom = get_macro_port_geometry(cell, port, is_output=True)
                    ix, iy = geom["inserter_pos"]
                    idir = geom["inserter_dir"]
                    ins_name, ins_extra = _select_port_inserter(inst, cell, port, is_out=True)
                    add_entity(ins_name, gx + ix, gy + iy, direction=idir, extra=ins_extra)
                    occupied_positions.add((round(gx + ix, 2), round(gy + iy, 2)))

            for ipipe in cell.get("internal_pipes", []):
                px = gx + float(ipipe["x"])
                py = gy + float(ipipe["y"])
                p_extra = {"fluid": str(ipipe["fluid"])} if "fluid" in ipipe else None
                add_entity("pipe", px, py, extra=p_extra)
                occupied_positions.add((round(px, 2), round(py, 2)))

            for tx in range(m_w):
                for ty in range(m_h):
                    occupied_positions.add(
                        (round(gx + in_offset + tx + 0.5, 2), round(gy + y_offset + ty + 0.5, 2))
                    )
            if not def_data.get("blockages") or machine in ("boiler", "steam-engine"):
                for ux, uy in cell.get("unused_fluid_ports", []):
                    occupied_positions.add((round(gx + float(ux), 2), round(gy + float(uy), 2)))

    return PlacementResult(
        entities=entities,
        occupied_positions=occupied_positions,
        grid_entities=grid_entities,
        inst_to_entity=inst_to_entity,
        fluids=fluids,
        has_fluid_metadata=has_fluid_metadata,
    )
