"""Circuit network and telemetry wiring.

Connects telemetry sensors, display panels, combinators, and electric poles
using red and green circuit wires and copper power cables.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, TypeAlias

EntityDict: TypeAlias = dict[str, Any]
NetlistDict: TypeAlias = dict[str, Any]
Position: TypeAlias = tuple[float, float]
EntityMap: TypeAlias = dict[str, EntityDict]
GridEntityMap: TypeAlias = dict[Position, EntityDict]

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

DEFAULT_WIRE_REACH = 9.0
COMBINATOR_PORT_MAP: dict[tuple[int, str], int] = {
    (WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED, "red"): WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED,
    (WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED, "green"): WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED,
    (WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN, "red"): WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN,
    (WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN, "green"): WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN,
    (2, "red"): WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED,
    (2, "green"): WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN,
    (1, "green"): WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN,
    (1, "red"): WIRE_CONNECTOR_COMBINATOR_INPUT_RED,
}


def _entity_pos(ent: EntityDict) -> Position:
    """Return (x, y) position tuple of an entity."""
    return (ent["position"]["x"], ent["position"]["y"])


def _is_active_combinator(ename: str) -> bool:
    """Return True if entity name is a combinator with separate input/output ports."""
    return "combinator" in ename and ename != "constant-combinator"


def _circuit_wire_id(color: str) -> int:
    """Return standard circuit wire connector ID for red or green."""
    return WIRE_CONNECTOR_CIRCUIT_GREEN if color == "green" else WIRE_CONNECTOR_CIRCUIT_RED


def find_nearest_pole(
    pos: Position,
    pole_entities: GridEntityMap,
    max_reach: float = DEFAULT_WIRE_REACH,
) -> EntityDict | None:
    """Find the closest electric pole within maximum wire reach distance."""
    best_pole: EntityDict | None = None
    best_dist = math.inf
    for p_pos, pole in pole_entities.items():
        d = math.hypot(p_pos[0] - pos[0], p_pos[1] - pos[1])
        if d <= max_reach and d < best_dist:
            best_dist = d
            best_pole = pole
    return best_pole


def resolve_connector_id(entity: EntityDict, port: int, color: str = "red") -> int:
    """Resolve entity port number and color to Factorio 2.0 wire connector ID."""
    ename = entity.get("name", "")
    if _is_active_combinator(ename):
        return COMBINATOR_PORT_MAP.get((port, color), WIRE_CONNECTOR_COMBINATOR_INPUT_RED)
    if ename == "power-switch" and port in (
        WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER,
        WIRE_CONNECTOR_POWER_SWITCH_RIGHT_COPPER,
    ):
        return port
    if "electric-pole" in ename and port == WIRE_CONNECTOR_POLE_COPPER:
        return WIRE_CONNECTOR_POLE_COPPER
    return _circuit_wire_id(color)


def _legacy_port_info(ent: EntityDict, cid: int) -> tuple[str, int]:
    """Compute legacy Factorio 1.0 connection port key and circuit_id."""
    if _is_active_combinator(ent.get("name", "")):
        is_in = cid in (WIRE_CONNECTOR_COMBINATOR_INPUT_RED, WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN)
        return ("1" if is_in else "2", 1 if is_in else 2)
    return (str(cid), cid)


def _record_legacy_connection(
    ent_a: EntityDict,
    cid_a: int,
    num_a: int,
    ent_b: EntityDict,
    cid_b: int,
    num_b: int,
    color: str,
) -> None:
    """Store wire link in ent['connections'] for Factorio 1.0 schema compatibility."""
    if color not in ("red", "green"):
        return
    pk_a, link_cid_a = _legacy_port_info(ent_a, cid_a)
    pk_b, link_cid_b = _legacy_port_info(ent_b, cid_b)
    if pk_a not in ("1", "2") or pk_b not in ("1", "2"):
        return
    conns_a = ent_a.setdefault("connections", {}).setdefault(pk_a, {}).setdefault(color, [])
    conns_b = ent_b.setdefault("connections", {}).setdefault(pk_b, {}).setdefault(color, [])
    conns_a.append({"entity_id": num_b, "circuit_id": link_cid_b})
    conns_b.append({"entity_id": num_a, "circuit_id": link_cid_a})


@dataclass(slots=True)
class _TelemetryWireBuilder:
    """Stateful builder that accumulates deduplicated wires and pole lookups."""

    pole_entities: GridEntityMap
    inst_to_entity: EntityMap = field(default_factory=dict)
    inst_map: EntityMap = field(default_factory=dict)
    net_surface_tiles: dict[str, dict[Position, int]] = field(default_factory=dict)
    grid_entities: GridEntityMap = field(default_factory=dict)
    wires: list[list[int]] = field(default_factory=list)
    seen_wires: set[tuple[tuple[int, int], tuple[int, int]]] = field(default_factory=set)
    switch_isolated_copper_edges: set[tuple[Position, Position]] = field(default_factory=set)

    def add_wire(
        self,
        ent_a: EntityDict,
        port_a: int,
        ent_b: EntityDict,
        port_b: int,
        color: str = "red",
    ) -> None:
        """Connect two entity ports with a deduplicated wire of the specified color."""
        num_a = ent_a.get("entity_number")
        num_b = ent_b.get("entity_number")
        if not num_a or not num_b:
            return
        cid_a = resolve_connector_id(ent_a, port_a, color)
        cid_b = resolve_connector_id(ent_b, port_b, color)
        key = (min((num_a, cid_a), (num_b, cid_b)), max((num_a, cid_a), (num_b, cid_b)))
        if key in self.seen_wires:
            return
        self.seen_wires.add(key)
        self.wires.append([num_a, cid_a, num_b, cid_b])
        _record_legacy_connection(ent_a, cid_a, num_a, ent_b, cid_b, num_b, color)

    def nearest_pole(
        self,
        pos: Position,
        max_reach: float = DEFAULT_WIRE_REACH,
    ) -> EntityDict | None:
        """Find nearest pole to pos from stored pole_entities."""
        return find_nearest_pole(pos, self.pole_entities, max_reach=max_reach)

    def _closest_pole_in_list(
        self,
        target_pos: Position,
        poles: list[tuple[Position, EntityDict]],
    ) -> tuple[Position, EntityDict]:
        """Return the (pos, entity) pair closest to target_pos."""
        return min(
            poles,
            key=lambda item: math.hypot(item[0][0] - target_pos[0], item[0][1] - target_pos[1]),
        )

    def _wire_domain_power_switch(
        self,
        ps: EntityDict,
        ps_pos: Position,
        aon_poles: list[tuple[Position, EntityDict]],
        switched_poles: list[tuple[Position, EntityDict]],
    ) -> None:
        """Wire a power switch between always-on and switched power domains."""
        _p_left_pos, p_left_ent = self._closest_pole_in_list(ps_pos, aon_poles)
        _p_right_pos, p_right_ent = self._closest_pole_in_list(ps_pos, switched_poles)
        self.add_wire(
            p_left_ent,
            WIRE_CONNECTOR_POLE_COPPER,
            ps,
            WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER,
            color="copper",
        )
        self.add_wire(
            ps,
            WIRE_CONNECTOR_POWER_SWITCH_RIGHT_COPPER,
            p_right_ent,
            WIRE_CONNECTOR_POLE_COPPER,
            color="copper",
        )
        self.add_wire(
            p_left_ent,
            WIRE_CONNECTOR_CIRCUIT_RED,
            p_right_ent,
            WIRE_CONNECTOR_CIRCUIT_RED,
            color="red",
        )
        self.add_wire(
            p_left_ent,
            WIRE_CONNECTOR_CIRCUIT_GREEN,
            p_right_ent,
            WIRE_CONNECTOR_CIRCUIT_GREEN,
            color="green",
        )
        self.add_wire(
            ps,
            WIRE_CONNECTOR_CIRCUIT_RED,
            p_left_ent,
            WIRE_CONNECTOR_CIRCUIT_RED,
            color="red",
        )

    def _wire_fallback_power_switch(self, ps: EntityDict, ps_pos: Position) -> None:
        """Wire a power switch to the closest 1 or 2 poles when domains are untagged."""
        sorted_poles = sorted(
            self.pole_entities.items(),
            key=lambda item: math.hypot(item[0][0] - ps_pos[0], item[0][1] - ps_pos[1]),
        )
        if not sorted_poles:
            return
        p_left_pos, p_left_ent = sorted_poles[0]
        self.add_wire(
            p_left_ent,
            WIRE_CONNECTOR_POLE_COPPER,
            ps,
            WIRE_CONNECTOR_POWER_SWITCH_LEFT_COPPER,
            color="copper",
        )
        if len(sorted_poles) >= 2:
            p_right_pos, p_right_ent = sorted_poles[1]
            self.add_wire(
                ps,
                WIRE_CONNECTOR_POWER_SWITCH_RIGHT_COPPER,
                p_right_ent,
                WIRE_CONNECTOR_POLE_COPPER,
                color="copper",
            )
            edge_key = (min(p_left_pos, p_right_pos), max(p_left_pos, p_right_pos))
            self.switch_isolated_copper_edges.add(edge_key)

    def wire_power_switches(self, entities: list[EntityDict]) -> None:
        """Connect all power switches to copper and control poles."""
        if not self.pole_entities:
            return
        aon_poles = [
            (pos, ent)
            for pos, ent in self.pole_entities.items()
            if ent.get("power_domain") == "aon"
        ]
        switched_poles = [
            (pos, ent)
            for pos, ent in self.pole_entities.items()
            if ent.get("power_domain") == "switched"
        ]
        for ps in entities:
            if ps.get("name") != "power-switch":
                continue
            ps_pos = _entity_pos(ps)
            if aon_poles and switched_poles:
                self._wire_domain_power_switch(ps, ps_pos, aon_poles, switched_poles)
            else:
                self._wire_fallback_power_switch(ps, ps_pos)

    def wire_pole_mst(
        self,
        mst_connections: list[tuple[Position, Position]],
    ) -> None:
        """Wire electric pole minimum spanning tree edges."""
        for p1, p2 in mst_connections:
            ent1 = self.pole_entities.get(p1)
            ent2 = self.pole_entities.get(p2)
            if not ent1 or not ent2:
                continue
            edge_key = (min(p1, p2), max(p1, p2))
            if edge_key not in self.switch_isolated_copper_edges:
                self.add_wire(
                    ent1,
                    WIRE_CONNECTOR_POLE_COPPER,
                    ent2,
                    WIRE_CONNECTOR_POLE_COPPER,
                    color="copper",
                )
            self.add_wire(
                ent1, WIRE_CONNECTOR_CIRCUIT_RED, ent2, WIRE_CONNECTOR_CIRCUIT_RED, color="red"
            )
            self.add_wire(
                ent1,
                WIRE_CONNECTOR_CIRCUIT_GREEN,
                ent2,
                WIRE_CONNECTOR_CIRCUIT_GREEN,
                color="green",
            )

    def _is_reachable_sensor(self, pos: Position, d_ref: float) -> bool:
        """Check if a sensor candidate at pos can reach reference or a pole."""
        return d_ref <= DEFAULT_WIRE_REACH or self.nearest_pole(pos) is not None

    def _find_belt_sensor(
        self,
        mat: str,
        ref_pos: Position,
    ) -> tuple[EntityDict | None, float]:
        """Find closest reachable transport-belt entity for material mat."""
        mat_bare = mat.replace("-", "_").replace(".", "__")
        candidate_tiles = set(self.net_surface_tiles.get(mat, {}).keys())
        candidate_tiles.update(self.net_surface_tiles.get(mat_bare, {}).keys())
        for k, v in self.net_surface_tiles.items():
            if k.startswith(mat_bare + "__"):
                candidate_tiles.update(v.keys())

        best_ent: EntityDict | None = None
        best_dist = math.inf
        for pos_tile in candidate_tiles:
            cand = self.grid_entities.get(pos_tile)
            if not cand or "transport-belt" not in cand.get("name", ""):
                continue
            d_ref = math.hypot(pos_tile[0] - ref_pos[0], pos_tile[1] - ref_pos[1])
            if d_ref < best_dist and self._is_reachable_sensor(pos_tile, d_ref):
                best_dist = d_ref
                best_ent = cand
        return best_ent, best_dist

    def _find_splitter_sensor(
        self,
        mat: str,
        ref_pos: Position,
        best_dist: float,
    ) -> EntityDict | None:
        """Fallback search for a splitter sensor matching material mat."""
        best_ent: EntityDict | None = None
        for inst_name, cand in self.inst_to_entity.items():
            if cand.get("name") != "splitter":
                continue
            if self.inst_map.get(inst_name, {}).get("recipe") != mat:
                continue
            cand_pos = _entity_pos(cand)
            d_ref = math.hypot(cand_pos[0] - ref_pos[0], cand_pos[1] - ref_pos[1])
            if d_ref < best_dist and self._is_reachable_sensor(cand_pos, d_ref):
                best_dist = d_ref
                best_ent = cand
        return best_ent

    def _wire_sensor_target(
        self,
        best_ent: EntityDict,
        target_ent: EntityDict | None,
        tgt_pos: Position,
    ) -> None:
        """Connect sensor entity directly or via poles to readout target."""
        src_pos = _entity_pos(best_ent)
        cand_dist = math.hypot(src_pos[0] - tgt_pos[0], src_pos[1] - tgt_pos[1])
        if cand_dist <= DEFAULT_WIRE_REACH:
            if target_ent:
                self.add_wire(best_ent, 1, target_ent, 1, color="red")
            return
        if not self.pole_entities:
            return
        pole_src = self.nearest_pole(src_pos)
        pole_dst = self.nearest_pole(tgt_pos)
        if pole_src and pole_dst:
            self.add_wire(best_ent, 1, pole_src, 1, color="red")
            if target_ent:
                self.add_wire(target_ent, 1, pole_dst, 1, color="red")

    def _connect_sensor_to_readout(
        self,
        best_ent: EntityDict,
        circ_ent: EntityDict | None,
        disp_ent: EntityDict | None,
        ref_pos: Position,
    ) -> None:
        """Configure belt readout mode and wire sensor to combinator and display."""
        best_ent.setdefault("control_behavior", {})["circuit_read_hand_contents"] = True
        best_ent["control_behavior"]["circuit_contents_read_mode"] = 0

        target_ent = circ_ent if circ_ent else disp_ent
        tgt_pos = _entity_pos(target_ent) if target_ent else ref_pos
        self._wire_sensor_target(best_ent, target_ent, tgt_pos)

        if not circ_ent:
            return
        self.add_wire(
            circ_ent,
            WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN,
            circ_ent,
            WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN,
            color="green",
        )
        if not disp_ent:
            return
        c_pos = _entity_pos(circ_ent)
        d_pos = _entity_pos(disp_ent)
        if math.hypot(c_pos[0] - d_pos[0], c_pos[1] - d_pos[1]) <= DEFAULT_WIRE_REACH:
            self.add_wire(
                circ_ent,
                WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED,
                disp_ent,
                WIRE_CONNECTOR_CIRCUIT_RED,
                color="red",
            )

    def wire_telemetry_records(
        self,
        telemetry_list: list[EntityDict],
    ) -> None:
        """Wire telemetry belt sensors to accumulator combinators and displays."""
        for rec in telemetry_list:
            mat = rec.get("material", "")
            disp_name = rec.get("display_instance")
            circ_name = rec.get("circuit_instance")
            disp_ent = self.inst_to_entity.get(disp_name) if disp_name else None
            circ_ent = self.inst_to_entity.get(circ_name) if circ_name else None
            ref_ent = circ_ent or disp_ent
            if not ref_ent:
                continue
            ref_pos = _entity_pos(ref_ent)
            best_ent, best_dist = self._find_belt_sensor(mat, ref_pos)
            if best_ent is None:
                best_ent = self._find_splitter_sensor(mat, ref_pos, best_dist)
            if best_ent is not None:
                self._connect_sensor_to_readout(best_ent, circ_ent, disp_ent, ref_pos)

    def wire_controller_connections(
        self,
        circuit_connections: list[EntityDict],
    ) -> None:
        """Wire explicit controller circuit connections directly or via poles."""
        for c_conn in circuit_connections:
            ent_src = self.inst_to_entity.get(c_conn.get("source", ""))
            ent_tgt = self.inst_to_entity.get(c_conn.get("target", ""))
            if not ent_src or not ent_tgt:
                continue
            color = c_conn.get("color", "red")
            src_port = c_conn.get("source_port", 1)
            tgt_port = c_conn.get("target_port", 1)
            s_pos = _entity_pos(ent_src)
            t_pos = _entity_pos(ent_tgt)
            dist = math.hypot(s_pos[0] - t_pos[0], s_pos[1] - t_pos[1])
            if dist > DEFAULT_WIRE_REACH and self.pole_entities:
                p_src = self.nearest_pole(s_pos)
                p_tgt = self.nearest_pole(t_pos)
                pole_port = _circuit_wire_id(color)
                if p_src and p_tgt:
                    self.add_wire(ent_src, src_port, p_src, pole_port, color=color)
                    self.add_wire(ent_tgt, tgt_port, p_tgt, pole_port, color=color)
                    continue
            self.add_wire(ent_src, src_port, ent_tgt, tgt_port, color=color)

    def _connect_entity_to_nearest_pole(self, ent: EntityDict | None, port: int) -> None:
        """Wire entity port to nearest red pole circuit connector if within reach."""
        if not ent:
            return
        p = self.nearest_pole(_entity_pos(ent))
        if p:
            self.add_wire(ent, port, p, WIRE_CONNECTOR_CIRCUIT_RED, color="red")

    def wire_bus_broadcasts(
        self,
        netlist_data: NetlistDict,
    ) -> None:
        """Connect parameter combinator and primary controller outputs to circuit bus."""
        if not self.pole_entities:
            return
        self._connect_entity_to_nearest_pole(
            self.inst_to_entity.get("u_const_params"), WIRE_CONNECTOR_CIRCUIT_RED
        )

        driven_sources = {
            c_conn.get("source") for c_conn in netlist_data.get("circuit_connections", [])
        }
        for inst in netlist_data.get("instances", []):
            iname = inst.get("name", "")
            icell = inst.get("cell", "")
            if (
                "combinator" in icell
                and icell != "constant_combinator"
                and iname not in driven_sources
                and "u_acc_" not in iname
            ):
                self._connect_entity_to_nearest_pole(
                    self.inst_to_entity.get(iname), WIRE_CONNECTOR_COMBINATOR_OUTPUT_RED
                )


def wire_telemetry(
    entities: list[EntityDict],
    netlist_data: NetlistDict,
    inst_to_entity: EntityMap,
    net_surface_tiles: dict[str, dict[Position, int]],
    grid_entities: GridEntityMap,
    mst_connections: list[tuple[Position, Position]],
    pole_entities: GridEntityMap,
) -> list[list[int]]:
    """Wire circuit telemetry, combinators, and power switches into 2.0 wires array."""
    for idx, ent in enumerate(entities, 1):
        ent["entity_number"] = idx

    inst_map: EntityMap = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    builder = _TelemetryWireBuilder(
        pole_entities=pole_entities,
        inst_to_entity=inst_to_entity,
        inst_map=inst_map,
        net_surface_tiles=net_surface_tiles,
        grid_entities=grid_entities,
    )

    builder.wire_power_switches(entities)
    builder.wire_pole_mst(mst_connections)
    builder.wire_telemetry_records(netlist_data.get("telemetry", []))
    builder.wire_controller_connections(netlist_data.get("circuit_connections", []))
    builder.wire_bus_broadcasts(netlist_data)

    return builder.wires
