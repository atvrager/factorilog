"""Circuit network and telemetry wiring.

Connects telemetry sensors, display panels, combinators, and electric poles
using red and green circuit wires and copper power cables.
"""

from __future__ import annotations

import math
from typing import Any

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


def wire_telemetry(
    entities: list[dict[str, Any]],
    netlist_data: dict[str, Any],
    inst_to_entity: dict[str, dict[str, Any]],
    net_surface_tiles: dict[str, dict[tuple[float, float], int]],
    grid_entities: dict[tuple[float, float], dict[str, Any]],
    mst_connections: list[tuple[tuple[float, float], tuple[float, float]]],
    pole_entities: dict[tuple[float, float], dict[str, Any]],
) -> list[list[int]]:
    """Wire circuit telemetry and assign sequential entity numbers."""
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}

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

    return wires
