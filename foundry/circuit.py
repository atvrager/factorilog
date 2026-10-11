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


def find_nearest_pole(
    pos: tuple[float, float],
    pole_entities: dict[tuple[float, float], dict[str, Any]],
    max_reach: float = 9.0,
) -> dict[str, Any] | None:
    """Find the closest electric pole within maximum wire reach distance."""
    best_pole = None
    best_dist = math.inf
    for p_pos, pole in pole_entities.items():
        d = math.hypot(p_pos[0] - pos[0], p_pos[1] - pos[1])
        if d <= max_reach and d < best_dist:
            best_dist = d
            best_pole = pole
    return best_pole


def resolve_connector_id(entity: dict[str, Any], port: int, color: str = "red") -> int:
    """Resolve entity port number and color to Factorio 2.0 wire connector ID."""
    ename = entity.get("name", "")
    if "combinator" in ename and ename != "constant-combinator":
        if port == WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED:
            return WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED
        if port == WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN:
            return WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN
        if port == 2:
            if color == "red":
                return WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED
            return WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN
        if port == 1:
            if color == "green":
                return WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN
            return WIRE_CONNECTOR_COMBINATOR_INPUT_RED
        return WIRE_CONNECTOR_COMBINATOR_INPUT_RED
    if ename == "power-switch":
        if port in (
            WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER,
            WIRE_CONNECTOR_POWER_SWITCH_RIGHT_COPPER,
        ):
            return port
        return WIRE_CONNECTOR_CIRCUIT_GREEN if color == "green" else WIRE_CONNECTOR_CIRCUIT_RED
    if "electric-pole" in ename:
        if port == WIRE_CONNECTOR_POLE_COPPER:
            return WIRE_CONNECTOR_POLE_COPPER
        return WIRE_CONNECTOR_CIRCUIT_GREEN if color == "green" else WIRE_CONNECTOR_CIRCUIT_RED
    return WIRE_CONNECTOR_CIRCUIT_GREEN if color == "green" else WIRE_CONNECTOR_CIRCUIT_RED


def wire_telemetry(
    entities: list[dict[str, Any]],
    netlist_data: dict[str, Any],
    inst_to_entity: dict[str, dict[str, Any]],
    net_surface_tiles: dict[str, dict[tuple[float, float], int]],
    grid_entities: dict[tuple[float, float], dict[str, Any]],
    mst_connections: list[tuple[tuple[float, float], tuple[float, float]]],
    pole_entities: dict[tuple[float, float], dict[str, Any]],
) -> list[list[int]]:
    """Wire circuit telemetry, combinators, and power switches into 2.0 wires array."""
    # 1. Assign sequential 1-based entity numbers
    for idx, ent in enumerate(entities, 1):
        ent["entity_number"] = idx

    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    wires: list[list[int]] = []
    seen_wires: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    def add_wire(
        ent_a: dict[str, Any],
        port_a: int,
        ent_b: dict[str, Any],
        port_b: int,
        color: str = "red",
    ) -> None:
        num_a = ent_a.get("entity_number")
        num_b = ent_b.get("entity_number")
        if not num_a or not num_b:
            return
        cid_a = resolve_connector_id(ent_a, port_a, color)
        cid_b = resolve_connector_id(ent_b, port_b, color)
        key = (min((num_a, cid_a), (num_b, cid_b)), max((num_a, cid_a), (num_b, cid_b)))
        if key in seen_wires:
            return
        seen_wires.add(key)
        wires.append([num_a, cid_a, num_b, cid_b])

        # Also store in ent["connections"] for Factorio 1.0 schema compatibility
        if color in ("red", "green"):
            is_comb_a = (
                "combinator" in ent_a.get("name", "") and ent_a.get("name") != "constant-combinator"
            )
            is_comb_b = (
                "combinator" in ent_b.get("name", "") and ent_b.get("name") != "constant-combinator"
            )
            pk_a = ("1" if cid_a in (1, 2) else "2") if is_comb_a else str(cid_a)
            pk_b = ("1" if cid_b in (1, 2) else "2") if is_comb_b else str(cid_b)
            if pk_a in ("1", "2") and pk_b in ("1", "2"):
                link_cid_a = (1 if cid_a in (1, 2) else 2) if is_comb_a else cid_a
                link_cid_b = (1 if cid_b in (1, 2) else 2) if is_comb_b else cid_b

                conns_a = (
                    ent_a.setdefault("connections", {}).setdefault(pk_a, {}).setdefault(color, [])
                )
                conns_b = (
                    ent_b.setdefault("connections", {}).setdefault(pk_b, {}).setdefault(color, [])
                )
                conns_a.append({"entity_id": num_b, "circuit_id": link_cid_b})
                conns_b.append({"entity_id": num_a, "circuit_id": link_cid_a})

    # 2. Power switch copper connections
    power_switch_ents = [e for e in entities if e.get("name") == "power-switch"]
    switch_isolated_copper_edges: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for ps in power_switch_ents:
        ps_pos = (ps["position"]["x"], ps["position"]["y"])
        if not pole_entities:
            continue
        aon_poles = [
            (pos, ent) for pos, ent in pole_entities.items() if ent.get("power_domain") == "aon"
        ]
        switched_poles = [
            (pos, ent)
            for pos, ent in pole_entities.items()
            if ent.get("power_domain") == "switched"
        ]
        if aon_poles and switched_poles:
            p_left_pos, p_left_ent = min(
                aon_poles,
                key=lambda item: math.hypot(item[0][0] - ps_pos[0], item[0][1] - ps_pos[1]),
            )
            p_right_pos, p_right_ent = min(
                switched_poles,
                key=lambda item: math.hypot(item[0][0] - ps_pos[0], item[0][1] - ps_pos[1]),
            )
            add_wire(
                p_left_ent,
                WIRE_CONNECTOR_POLE_COPPER,
                ps,
                WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER,
                color="copper",
            )
            add_wire(
                ps,
                WIRE_CONNECTOR_POWER_SWITCH_RIGHT_COPPER,
                p_right_ent,
                WIRE_CONNECTOR_POLE_COPPER,
                color="copper",
            )
            # Bridge circuit bus (red/green) between controller and factory without copper
            add_wire(
                p_left_ent,
                WIRE_CONNECTOR_CIRCUIT_RED,
                p_right_ent,
                WIRE_CONNECTOR_CIRCUIT_RED,
                color="red",
            )
            add_wire(
                p_left_ent,
                WIRE_CONNECTOR_CIRCUIT_GREEN,
                p_right_ent,
                WIRE_CONNECTOR_CIRCUIT_GREEN,
                color="green",
            )
            # Connect power switch control terminal to aon pole red circuit wire
            add_wire(
                ps,
                WIRE_CONNECTOR_CIRCUIT_RED,
                p_left_ent,
                WIRE_CONNECTOR_CIRCUIT_RED,
                color="red",
            )
        else:
            sorted_poles = sorted(
                pole_entities.items(),
                key=lambda item: math.hypot(item[0][0] - ps_pos[0], item[0][1] - ps_pos[1]),
            )
            if len(sorted_poles) >= 2:
                p_left_pos, p_left_ent = sorted_poles[0]
                p_right_pos, p_right_ent = sorted_poles[1]
                add_wire(
                    p_left_ent,
                    WIRE_CONNECTOR_POLE_COPPER,
                    ps,
                    WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER,
                    color="copper",
                )
                add_wire(
                    ps,
                    WIRE_CONNECTOR_POWER_SWITCH_RIGHT_COPPER,
                    p_right_ent,
                    WIRE_CONNECTOR_POLE_COPPER,
                    color="copper",
                )
                edge_key = (min(p_left_pos, p_right_pos), max(p_left_pos, p_right_pos))
                switch_isolated_copper_edges.add(edge_key)
            elif len(sorted_poles) == 1:
                _p_pos, p_ent = sorted_poles[0]
                add_wire(
                    p_ent,
                    WIRE_CONNECTOR_POLE_COPPER,
                    ps,
                    WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER,
                    color="copper",
                )

    # 3. Electric pole MST connections
    for p1, p2 in mst_connections:
        ent1 = pole_entities.get(p1)
        ent2 = pole_entities.get(p2)
        if not ent1 or not ent2:
            continue
        edge_key = (min(p1, p2), max(p1, p2))
        if edge_key not in switch_isolated_copper_edges:
            add_wire(
                ent1,
                WIRE_CONNECTOR_POLE_COPPER,
                ent2,
                WIRE_CONNECTOR_POLE_COPPER,
                color="copper",
            )
        add_wire(
            ent1,
            WIRE_CONNECTOR_CIRCUIT_RED,
            ent2,
            WIRE_CONNECTOR_CIRCUIT_RED,
            color="red",
        )
        add_wire(
            ent1,
            WIRE_CONNECTOR_CIRCUIT_GREEN,
            ent2,
            WIRE_CONNECTOR_CIRCUIT_GREEN,
            color="green",
        )

    # 4. Telemetry sensors and readout displays
    for rec in netlist_data.get("telemetry", []):
        mat = rec.get("material", "")
        disp_name = rec.get("display_instance")
        circ_name = rec.get("circuit_instance")
        disp_ent = inst_to_entity.get(disp_name) if disp_name else None
        circ_ent = inst_to_entity.get(circ_name) if circ_name else None

        ref_pos = None
        if circ_ent:
            ref_pos = (circ_ent["position"]["x"], circ_ent["position"]["y"])
        elif disp_ent:
            ref_pos = (disp_ent["position"]["x"], disp_ent["position"]["y"])

        if not ref_pos:
            continue

        mat_bare = mat.replace("-", "_").replace(".", "__")
        candidate_tiles = set(net_surface_tiles.get(mat, {}).keys())
        candidate_tiles.update(net_surface_tiles.get(mat_bare, {}).keys())
        for k, v in net_surface_tiles.items():
            if k.startswith(mat_bare + "__"):
                candidate_tiles.update(v.keys())

        best_ent = None
        best_dist = math.inf
        for pos_tile in candidate_tiles:
            cand = grid_entities.get(pos_tile)
            if cand and "transport-belt" in cand.get("name", ""):
                d_ref = math.hypot(pos_tile[0] - ref_pos[0], pos_tile[1] - ref_pos[1])
                pole = find_nearest_pole(pos_tile, pole_entities, max_reach=9.0)
                if (d_ref <= 9.0 or pole is not None) and d_ref < best_dist:
                    best_dist = d_ref
                    best_ent = cand

        if best_ent is None:
            for inst_name, cand in inst_to_entity.items():
                if cand.get("name") == "splitter":
                    inst_obj = inst_map.get(inst_name, {})
                    if inst_obj.get("recipe") == mat:
                        cand_pos = (cand["position"]["x"], cand["position"]["y"])
                        d_ref = math.hypot(cand_pos[0] - ref_pos[0], cand_pos[1] - ref_pos[1])
                        pole = find_nearest_pole(cand_pos, pole_entities, max_reach=9.0)
                        if (d_ref <= 9.0 or pole is not None) and d_ref < best_dist:
                            best_dist = d_ref
                            best_ent = cand

        if best_ent is not None:
            best_ent.setdefault("control_behavior", {})["circuit_read_hand_contents"] = True
            best_ent["control_behavior"]["circuit_contents_read_mode"] = 0

            target_ent = circ_ent if circ_ent else disp_ent
            tgt_pos = (
                (target_ent["position"]["x"], target_ent["position"]["y"])
                if target_ent
                else ref_pos
            )
            cand_dist = math.hypot(
                best_ent["position"]["x"] - tgt_pos[0],
                best_ent["position"]["y"] - tgt_pos[1],
            )

            if cand_dist <= 9.0:
                if target_ent:
                    add_wire(best_ent, 1, target_ent, 1, color="red")
            elif pole_entities:
                src_pos = (best_ent["position"]["x"], best_ent["position"]["y"])
                pole_src = find_nearest_pole(src_pos, pole_entities, max_reach=9.0)
                pole_dst = find_nearest_pole(tgt_pos, pole_entities, max_reach=9.0)
                if pole_src and pole_dst:
                    add_wire(best_ent, 1, pole_src, 1, color="red")
                    if target_ent:
                        add_wire(target_ent, 1, pole_dst, 1, color="red")

            if circ_ent:
                # Accumulator self-feedback loop
                add_wire(
                    circ_ent,
                    WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN,
                    circ_ent,
                    WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN,
                    color="green",
                )
                if disp_ent:
                    d_cd = math.hypot(
                        circ_ent["position"]["x"] - disp_ent["position"]["x"],
                        circ_ent["position"]["y"] - disp_ent["position"]["y"],
                    )
                    if d_cd <= 9.0:
                        add_wire(
                            circ_ent,
                            WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED,
                            disp_ent,
                            WIRE_CONNECTOR_CIRCUIT_RED,
                            color="red",
                        )

    # 5. Controller circuit connections
    for c_conn in netlist_data.get("circuit_connections", []):
        src_name = c_conn.get("source")
        tgt_name = c_conn.get("target")
        color = c_conn.get("color", "red")
        src_port = c_conn.get("source_port", 1)
        tgt_port = c_conn.get("target_port", 1)
        ent_src = inst_to_entity.get(src_name)
        ent_tgt = inst_to_entity.get(tgt_name)
        if not ent_src or not ent_tgt:
            continue
        dist = math.hypot(
            ent_src["position"]["x"] - ent_tgt["position"]["x"],
            ent_src["position"]["y"] - ent_tgt["position"]["y"],
        )
        if dist > 9.0 and pole_entities:
            p_src = find_nearest_pole(
                (ent_src["position"]["x"], ent_src["position"]["y"]), pole_entities
            )
            p_tgt = find_nearest_pole(
                (ent_tgt["position"]["x"], ent_tgt["position"]["y"]), pole_entities
            )
            pole_port = (
                WIRE_CONNECTOR_CIRCUIT_GREEN if color == "green" else WIRE_CONNECTOR_CIRCUIT_RED
            )
            if p_src and p_tgt:
                add_wire(ent_src, src_port, p_src, pole_port, color=color)
                add_wire(ent_tgt, tgt_port, p_tgt, pole_port, color=color)
            else:
                add_wire(ent_src, src_port, ent_tgt, tgt_port, color=color)
        else:
            add_wire(ent_src, src_port, ent_tgt, tgt_port, color=color)

    # 6. Connect parameter constant combinator to nearest pole to broadcast on circuit bus
    const_params_ent = inst_to_entity.get("u_const_params")
    if const_params_ent and pole_entities:
        p = find_nearest_pole(
            (const_params_ent["position"]["x"], const_params_ent["position"]["y"]),
            pole_entities,
        )
        if p:
            add_wire(
                const_params_ent,
                WIRE_CONNECTOR_CIRCUIT_RED,
                p,
                WIRE_CONNECTOR_CIRCUIT_RED,
                color="red",
            )

    # 7. Connect controller primary outputs to nearest pole to broadcast on circuit bus
    driven_sources = {
        c_conn.get("source") for c_conn in netlist_data.get("circuit_connections", [])
    }
    for inst in netlist_data.get("instances", []):
        iname = inst.get("name", "")
        icell = inst.get("cell", "")
        ent = inst_to_entity.get(iname)
        if not ent or not pole_entities:
            continue
        if (
            "combinator" in icell
            and icell != "constant_combinator"
            and iname not in driven_sources
            and "u_acc_" not in iname
        ):
            p = find_nearest_pole((ent["position"]["x"], ent["position"]["y"]), pole_entities)
            if p:
                add_wire(
                    ent,
                    WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED,
                    p,
                    WIRE_CONNECTOR_CIRCUIT_RED,
                    color="red",
                )

    return wires
