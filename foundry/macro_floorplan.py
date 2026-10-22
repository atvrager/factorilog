"""Level 1 macro floorplanner for recipe clustering and channel routing.

Partitions netlist instances into recipe clusters, allocates clusters to
discrete city block cores on the chassis grid, handles multi-block allocation
for oversized stages, and routes inter-block nets through street channel lanes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from foundry.channel_router import RouterIntersection, StreetChannel
from foundry.chassis import CityBlockChassis
from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    entity_size,
)

if TYPE_CHECKING:
    from foundry.router.types import RouterNetSpec

# Default floorplan and DEF unit constants
DEFAULT_DEF_DBU = 1000
DEFAULT_CHASSIS_PITCH = 100
DEFAULT_CORE_DIM = 94
DEFAULT_CHANNEL_WIDTH = 6
PROGRAMMATIC_GRID_THRESHOLD = 4
PROGRAMMATIC_CORE_DIM = 92
PROGRAMMATIC_CHANNEL_WIDTH = 8
DEFAULT_MAX_BLOCK_AREA = 8836.0
DEFAULT_MAX_BLOCK_WIDTH = 80.0
DEFAULT_CORE_MARGIN = 3
BLOCK_MAX_LOCAL_COORD = 99
BLOCK_CENTER_LOCAL_COORD = 50
COORD_ROUND_DIGITS = 2

# Port connection routing constants
COORD_EPSILON = 0.01
STEP_DIRECTION_THRESHOLD = 0.5
SPLITTER_PORT_X_OFFSET = 1.0
SPLITTER_PORT_Y_OFFSET = 0.5
MACHINE_PORT_X_OFFSET = 3.0
PORT_LANE_COUNT = 4
ADJACENT_EAST_V_LANE_OFFSET = 0.5
ADJACENT_SOUTH_V_LANE_OFFSET = 1.5
ADJACENT_SOUTH_H_LANE_OFFSET = 0.5
ADJACENT_SOUTH_LEAD_OFFSET = 2.0
ADJACENT_SOUTH_CORE_Y_OFFSET = 8.5
CROSSING_UNDERGROUND_OFFSET = 1.0
CROSSING_POST_OFFSET = 2.0
TARGET_APPROACH_UNDERGROUND_OFFSET = 5.0
EAST_SOUTH_H_LANE_OFFSET = 1.5
EAST_SOUTH_V_LANE_OFFSET = 1.5
INTERSECTION_APPROACH_OFFSET = 4.5
INTERSECTION_UNDERGROUND_OFFSET = 3.5
FALLBACK_H_LANE_OFFSET = 0.5
FALLBACK_TURN_X_OFFSET = 3.5

# Cluster width estimation padding constants
MERGE_CELL_PAD = 16.0
SPLIT_BAL_CELL_PAD = 10.0
CIRCUIT_CELL_PAD = 2.0
DEFAULT_CELL_PAD = 8.0

# Matrix switchbox bay and boundary geometry constants
BAY_CORE_Y_LO = 17
BAY_STREET_Y_MAX = 16
BAY_DETECT_X_MIN = 12
BAY_DETECT_X_MAX = 88
BAY_DETECT_Y_MIN = 4
BAY_DETECT_Y_MAX = 15
NORTH_BAY_PORT_Y_MIN = 19
PORT_CORNER_CLEARANCE = 2
MIN_CAP_TRACKS = 4
CAP_TRACK_MARGIN = 2

# Edge scoring weights and penalties
IN_COST_X_WEIGHT = 1.4
IN_COST_Y_WEIGHT = 2.4
PREFERRED_FLOW_EDGE_BONUS = 8.0
EXCESS_TRANSIT_THRESHOLD = 1.5
HERE_USAGE_WEIGHT = 16.0
OPP_USAGE_WEIGHT = 14.0
TRANSIT_USAGE_WEIGHT = 20.0
CAP_TRACK_BASE_PENALTY = 45.0
CAP_TRACK_OVERFLOW_WEIGHT = 90.0
SOUTH_BAY_NEIGHBOR_PENALTY = 12.0
SOUTH_BAY_TARGET_PENALTY = 180.0
RIM_PIN_BONUS = -200.0
RIM_EAST_W_PENALTY = 180.0
RIM_NORTH_S_PENALTY = 120.0
RIM_EAST_EXPORT_PENALTY = 20.0
RIM_EAST_BAY_BONUS = -60.0
RIM_EAST_EXPORT_IX_MIN = 60
RIM_EAST_EXPORT_IY_MAX = 55
RIM_EAST_BAY_TX_MIN = 65
RIM_EAST_BAY_IY_MID = 50

# Port candidate spacing constants
PORT_SPACING_OPTIONS = (3, 2, 1)
PRIMARY_PORT_SPACING = 3
WIDE_GRID_EAST_SPACING = 4
WIDE_GRID_THRESHOLD = 5
PIN_RESERVE_CLEARANCE = 2

# Ignored generic logic pin names
IGNORED_INPUT_PINS = ("a", "b", "a1", "a2")
IGNORED_OUTPUT_PINS = ("y", "y1", "y2")
CIRCUIT_HELPER_KEYWORDS = ("disp", "acc", "comb", "dec", "latch", "switch", "params")
ALL_CARDINAL_EDGES = ("N", "S", "E", "W")


@dataclass(slots=True)
class RecipeCluster:
    """A cluster of machine instances and helper cells belonging to one recipe stage."""

    name: str
    recipe: str
    instances: list[str]
    total_area: float
    rank: int = 0
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    assigned_block: tuple[int, int] | None = None
    sub_cluster_index: int = 0
    total_sub_clusters: int = 1


@dataclass(slots=True)
class ChannelRouteSegment:
    """A channel segment traversed by an inter-block net."""

    channel_id: str
    lane_index: int
    orientation: str
    direction: int


@dataclass(slots=True)
class BlockPort:
    """A physical port on the boundary of a city block core connecting to the fabric."""

    name: str
    block: tuple[int, int]
    net_name: str
    port_type: str  # "export" or "import"
    edge: str  # "N", "S", "E", "W"
    position: tuple[float, float]
    channel_id: str
    lane_index: int
    direction: int
    tier: str = "fast-transport-belt"
    internal_pos: tuple[float, float] | None = None

    @property
    def core_pos(self) -> tuple[int, int]:
        """Return integer (x, y) tile coordinates of the internal core endpoint."""
        if self.internal_pos is not None:
            return (round(self.internal_pos[0]), round(self.internal_pos[1]))
        return (round(self.position[0]), round(self.position[1]))

    @property
    def channel_pos(self) -> tuple[int, int]:
        """Return integer (x, y) tile coordinates of the street channel endpoint."""
        return (round(self.position[0]), round(self.position[1]))

    @property
    def heading(self) -> int:
        """Return the Factorio direction of the port tap."""
        return self.direction

    @property
    def is_fluid(self) -> bool:
        """Return True when this port carries fluid."""
        return "pipe" in self.tier


@dataclass(slots=True)
class InterBlockRoute:
    """Routing lane reservations and turn instructions for an inter-block net."""

    net_name: str
    source_block: tuple[int, int] | None
    target_blocks: list[tuple[int, int]]
    segments: list[ChannelRouteSegment] = field(default_factory=list)
    turns: list[tuple[int, int, str, int, str, int]] = field(default_factory=list)
    is_fluid: bool = False
    tier: str = "fast-transport-belt"
    source_instance: str | None = None
    target_instances: list[str] = field(default_factory=list)
    source_port: BlockPort | None = None
    target_ports: list[BlockPort] = field(default_factory=list)


@dataclass(slots=True)
class _PortPathBuilder:
    """Helper that emits belt entities along orthogonal port connection paths."""

    entities: list[dict[str, Any]] = field(default_factory=list)
    occupied_indices: dict[tuple[float, float], int] = field(default_factory=dict)

    def add_belt(
        self, name: str, x: float, y: float, direction: int, btype: str | None = None
    ) -> None:
        """Insert or overwrite a belt entity at (x, y)."""
        key = (round(float(x), COORD_ROUND_DIGITS), round(float(y), COORD_ROUND_DIGITS))
        ent: dict[str, Any] = {
            "name": name,
            "position": {"x": key[0], "y": key[1]},
            "direction": direction,
        }
        if btype:
            ent["type"] = btype
        if key in self.occupied_indices:
            self.entities[self.occupied_indices[key]] = ent
        else:
            self.occupied_indices[key] = len(self.entities)
            self.entities.append(ent)

    def make_path(
        self,
        waypoints: list[tuple[float, float]],
        end_dir: int = DIRECTION_EAST,
        tier: str = "fast-transport-belt",
    ) -> None:
        """Rasterize orthogonal segments between waypoints and place belt tiles."""
        tiles = _rasterize_waypoints(waypoints)
        for i, curr in enumerate(tiles):
            d = _step_direction(curr, tiles[i + 1], end_dir) if i < len(tiles) - 1 else end_dir
            self.add_belt(tier, curr[0], curr[1], d)


def _rasterize_segment(
    p1: tuple[float, float], p2: tuple[float, float]
) -> list[tuple[float, float]]:
    """Expand an axis-aligned segment into discrete 1-tile coordinates."""
    x1, y1 = p1
    x2, y2 = p2
    if abs(y1 - y2) < COORD_EPSILON:
        dx = 1.0 if x2 > x1 else -1.0
        steps = round(abs(x2 - x1))
        return [
            (round(x1 + s * dx, COORD_ROUND_DIGITS), round(y1, COORD_ROUND_DIGITS))
            for s in range(steps + 1)
        ]
    if abs(x1 - x2) < COORD_EPSILON:
        dy = 1.0 if y2 > y1 else -1.0
        steps = round(abs(y2 - y1))
        return [
            (round(x1, COORD_ROUND_DIGITS), round(y1 + s * dy, COORD_ROUND_DIGITS))
            for s in range(steps + 1)
        ]
    return []


def _rasterize_waypoints(waypoints: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Expand waypoints into deduplicated consecutive tile coordinates."""
    dedup: list[tuple[float, float]] = []
    for i in range(len(waypoints) - 1):
        for pt in _rasterize_segment(waypoints[i], waypoints[i + 1]):
            if not dedup or dedup[-1] != pt:
                dedup.append(pt)
    return dedup


def _step_direction(curr: tuple[float, float], nxt: tuple[float, float], default_dir: int) -> int:
    """Return cardinal Factorio direction from curr tile to nxt tile."""
    dx = nxt[0] - curr[0]
    dy = nxt[1] - curr[1]
    if dx > STEP_DIRECTION_THRESHOLD:
        return DIRECTION_EAST
    if dx < -STEP_DIRECTION_THRESHOLD:
        return DIRECTION_WEST
    if dy > STEP_DIRECTION_THRESHOLD:
        return DIRECTION_SOUTH
    if dy < -STEP_DIRECTION_THRESHOLD:
        return DIRECTION_NORTH
    return default_dir


def _entity_port_offset(ent: object, is_source: bool) -> tuple[float, float]:
    """Compute source output or target input coordinates for a placed entity."""
    ent_map: dict[str, Any] = ent if isinstance(ent, dict) else {}
    pos = ent_map.get("position", {})
    x = float(pos.get("x", 0.0))
    y = float(pos.get("y", 0.0))
    sign = 1.0 if is_source else -1.0
    if ent_map.get("name") == "splitter":
        return (x + sign * SPLITTER_PORT_X_OFFSET, y - SPLITTER_PORT_Y_OFFSET)
    return (x + sign * MACHINE_PORT_X_OFFSET, y)


class MacroFloorplan:
    """Physical floorplan of recipe clusters mapped to a city block chassis grid."""

    def __init__(
        self,
        grid_w: int,
        grid_h: int,
        chassis: CityBlockChassis,
        clusters: list[RecipeCluster],
    ) -> None:
        self.grid_w = grid_w
        self.grid_h = grid_h
        self.chassis = chassis
        self.clusters = clusters

        self.instance_to_block: dict[str, tuple[int, int]] = {}
        self.block_to_clusters: dict[tuple[int, int], list[RecipeCluster]] = {}
        self.empty_blocks: set[tuple[int, int]] = set()
        self.channels: dict[str, StreetChannel] = {}
        self.intersections: dict[tuple[int, int], RouterIntersection] = {}
        self.routes: dict[str, InterBlockRoute] = {}
        self.ports: list[BlockPort] = []

        for c in clusters:
            if c.assigned_block is not None:
                bx, by = c.assigned_block
                self.block_to_clusters.setdefault((bx, by), []).append(c)
                for inst_name in c.instances:
                    self.instance_to_block[inst_name] = (bx, by)

    @property
    def block_ports(self) -> list[BlockPort]:
        """Return all allocated block ports on the floorplan."""
        return self.ports

    def get_block_clusters(self, block_x: int, block_y: int) -> list[RecipeCluster]:
        """Return all recipe clusters assigned to the specified city block."""
        return self.block_to_clusters.get((block_x, block_y), [])

    def get_block_instances(self, block_x: int, block_y: int) -> list[str]:
        """Return all instance names assigned to the specified city block."""
        instances: list[str] = []
        for cluster in self.get_block_clusters(block_x, block_y):
            instances.extend(cluster.instances)
        return instances

    def get_block_area(self, block_x: int, block_y: int) -> float:
        """Return total machine tile area in the specified city block."""
        return sum(c.total_area for c in self.get_block_clusters(block_x, block_y))

    def emit_floorplan_tcl(self) -> str:
        """Emit OpenROAD floorplan TCL commands with blockages for this grid."""
        return self.chassis.emit_floorplan_tcl(
            self.grid_w, self.grid_h, empty_blocks=self.empty_blocks
        )

    def emit_def_blockages(self, dbu: int = DEFAULT_DEF_DBU) -> str:
        """Emit DEF BLOCKAGES section covering all street channels."""
        return self.chassis.emit_def_blockages(
            self.grid_w, self.grid_h, dbu=dbu, empty_blocks=self.empty_blocks
        )

    def emit_channel_entities(self) -> list[dict[str, Any]]:
        """Emit all physical belts, pipes, and router switchboxes for inter-block routes."""
        entities: list[dict[str, Any]] = []
        occupied_tiles: set[tuple[float, float]] = set()

        for intersection in self.intersections.values():
            self._append_unique_entities(
                intersection.emit_intersection_entities(), entities, occupied_tiles
            )

        for channel in self.channels.values():
            self._append_unique_entities(channel.emit_straight_belts(), entities, occupied_tiles)

        return entities

    @staticmethod
    def _append_unique_entities(
        candidates: Sequence[object],
        entities: object,
        occupied_tiles: set[tuple[float, float]],
    ) -> None:
        """Append entities whose rounded (x, y) coordinates are not yet occupied."""
        ent_list: list[dict[str, Any]] = entities if isinstance(entities, list) else []
        for raw_ent in candidates:
            ent: dict[str, Any] = raw_ent if isinstance(raw_ent, dict) else {}
            pos = ent.get("position", {})
            key = (
                round(float(pos.get("x", 0.0)), COORD_ROUND_DIGITS),
                round(float(pos.get("y", 0.0)), COORD_ROUND_DIGITS),
            )
            if key not in occupied_tiles:
                occupied_tiles.add(key)
                ent_list.append(ent)

    def emit_port_connections(self, placement: object = None) -> list[dict[str, Any]]:
        """Emit physical belts connecting block interiors to the street channel fabric."""
        if placement is None:
            return []
        inst_to_ent: dict[str, dict[str, Any]] = getattr(placement, "inst_to_entity", {})
        if not inst_to_ent:
            return []

        builder = _PortPathBuilder()
        for route_idx, route in enumerate(self.routes.values()):
            self._emit_single_route_connections(route_idx, route, inst_to_ent, builder)
        return builder.entities

    def _emit_single_route_connections(
        self,
        route_idx: int,
        route: InterBlockRoute,
        inst_to_ent: object,
        builder: _PortPathBuilder,
    ) -> None:
        """Emit belt paths from a route source instance to all target blocks."""
        ent_map: dict[str, dict[str, Any]] = inst_to_ent if isinstance(inst_to_ent, dict) else {}
        if route.source_block is None or not route.target_blocks:
            return
        src_inst = route.source_instance
        if not src_inst or src_inst not in ent_map:
            return

        out_pt = _entity_port_offset(ent_map[src_inst], is_source=True)
        lane_off = float(route_idx % PORT_LANE_COUNT)

        for tgt_block in route.target_blocks:
            tgt_inst = self._first_block_target_instance(route, tgt_block, ent_map)
            if tgt_inst is None:
                continue
            in_pt = _entity_port_offset(ent_map[tgt_inst], is_source=False)
            self._emit_block_pair_connection(
                route.source_block, tgt_block, out_pt, in_pt, lane_off, route.tier, builder
            )

    def _first_block_target_instance(
        self,
        route: InterBlockRoute,
        tgt_block: tuple[int, int],
        inst_to_ent: object,
    ) -> str | None:
        """Return the first placed target instance of route inside tgt_block."""
        ent_map: dict[str, dict[str, Any]] = inst_to_ent if isinstance(inst_to_ent, dict) else {}
        for ti in route.target_instances:
            if self.instance_to_block.get(ti) == tgt_block and ti in ent_map:
                return ti
        return None

    def _emit_block_pair_connection(
        self,
        src_blk: tuple[int, int],
        tgt_block: tuple[int, int],
        out_pt: tuple[float, float],
        in_pt: tuple[float, float],
        lane_off: float,
        tier: str,
        builder: _PortPathBuilder,
    ) -> None:
        """Dispatch block-pair connection topology between src_blk and tgt_block."""
        out_x, out_y = out_pt
        in_x, in_y = in_pt
        if src_blk[0] + 1 == tgt_block[0] and src_blk[1] == tgt_block[1]:
            v_lane_x = (
                src_blk[0] * self.chassis.pitch_x
                + self.chassis.core_margin_x
                + self.chassis.core_width
                + ADJACENT_EAST_V_LANE_OFFSET
                + lane_off
            )
            waypoints = [(out_x, out_y), (v_lane_x, out_y), (v_lane_x, in_y), (in_x, in_y)]
            builder.make_path(waypoints, end_dir=DIRECTION_EAST, tier=tier)
        elif src_blk[0] == tgt_block[0] and src_blk[1] + 1 == tgt_block[1]:
            self._emit_adjacent_south_connection(src_blk, tgt_block, out_pt, in_pt, tier, builder)
        elif src_blk[0] < tgt_block[0] and src_blk[1] < tgt_block[1]:
            self._emit_east_south_connection(src_blk, tgt_block, out_pt, in_pt, tier, builder)
        else:
            h_lane_y = (
                src_blk[1] * self.chassis.pitch_y
                + self.chassis.core_margin_y
                + self.chassis.core_height
                + FALLBACK_H_LANE_OFFSET
                + lane_off
            )
            turn_x = (
                tgt_block[0] * self.chassis.pitch_x
                + self.chassis.core_margin_x
                - FALLBACK_TURN_X_OFFSET
                + lane_off
            )
            waypoints = [
                (out_x, out_y),
                (out_x, h_lane_y),
                (turn_x, h_lane_y),
                (turn_x, in_y),
                (in_x, in_y),
            ]
            builder.make_path(waypoints, end_dir=DIRECTION_EAST, tier=tier)

    def _emit_adjacent_south_connection(
        self,
        src_blk: tuple[int, int],
        tgt_block: tuple[int, int],
        out_pt: tuple[float, float],
        in_pt: tuple[float, float],
        tier: str,
        builder: _PortPathBuilder,
    ) -> None:
        """Emit connection to an adjacent South block on the same column."""
        out_x, out_y = out_pt
        in_x, in_y = in_pt
        center_x = src_blk[0] * self.chassis.pitch_x
        center_y = tgt_block[1] * self.chassis.pitch_y
        v_lane_x = center_x + ADJACENT_SOUTH_V_LANE_OFFSET
        h_lane_y = center_y - ADJACENT_SOUTH_H_LANE_OFFSET

        lead_y = min(
            out_y - ADJACENT_SOUTH_LEAD_OFFSET,
            src_blk[1] * self.chassis.pitch_y
            + self.chassis.core_margin_y
            + ADJACENT_SOUTH_CORE_Y_OFFSET,
        )
        cross_y = out_y
        p_lead = [
            (out_x, out_y),
            (out_x, lead_y),
            (v_lane_x, lead_y),
            (v_lane_x, cross_y - CROSSING_UNDERGROUND_OFFSET),
        ]
        builder.make_path(p_lead, end_dir=DIRECTION_SOUTH, tier=tier)
        builder.add_belt(
            "fast-underground-belt",
            v_lane_x,
            cross_y - CROSSING_UNDERGROUND_OFFSET,
            DIRECTION_SOUTH,
            "input",
        )
        builder.add_belt(
            "fast-underground-belt",
            v_lane_x,
            cross_y + CROSSING_UNDERGROUND_OFFSET,
            DIRECTION_SOUTH,
            "output",
        )

        p_to_tgt = [
            (v_lane_x, cross_y + CROSSING_POST_OFFSET),
            (v_lane_x, h_lane_y),
            (in_x, h_lane_y),
            (in_x, in_y - TARGET_APPROACH_UNDERGROUND_OFFSET),
        ]
        builder.make_path(p_to_tgt, end_dir=DIRECTION_SOUTH, tier=tier)
        builder.add_belt(
            "fast-underground-belt",
            in_x,
            in_y - TARGET_APPROACH_UNDERGROUND_OFFSET,
            DIRECTION_SOUTH,
            "input",
        )
        builder.add_belt("fast-underground-belt", in_x, in_y, DIRECTION_SOUTH, "output")
        builder.add_belt(tier, in_x, in_y + CROSSING_UNDERGROUND_OFFSET, DIRECTION_EAST)

    def _emit_east_south_connection(
        self,
        src_blk: tuple[int, int],
        tgt_block: tuple[int, int],
        out_pt: tuple[float, float],
        in_pt: tuple[float, float],
        tier: str,
        builder: _PortPathBuilder,
    ) -> None:
        """Emit connection to a block located both East and South of src_blk."""
        out_x, out_y = out_pt
        in_x, in_y = in_pt
        h_lane_y = (
            src_blk[1] * self.chassis.pitch_y
            + self.chassis.core_margin_y
            + self.chassis.core_height
            + EAST_SOUTH_H_LANE_OFFSET
        )
        v_lane_x = tgt_block[0] * self.chassis.pitch_x - EAST_SOUTH_V_LANE_OFFSET

        builder.make_path([(out_x, out_y), (out_x, h_lane_y)], end_dir=DIRECTION_EAST, tier=tier)

        curr_x = out_x
        for col in range(src_blk[0] + 1, tgt_block[0]):
            cx = col * self.chassis.pitch_x
            builder.make_path(
                [(curr_x, h_lane_y), (cx - INTERSECTION_APPROACH_OFFSET, h_lane_y)],
                end_dir=DIRECTION_EAST,
                tier=tier,
            )
            builder.add_belt(
                "fast-underground-belt",
                cx - INTERSECTION_UNDERGROUND_OFFSET,
                h_lane_y,
                DIRECTION_EAST,
                "input",
            )
            builder.add_belt(
                "fast-underground-belt",
                cx + INTERSECTION_UNDERGROUND_OFFSET,
                h_lane_y,
                DIRECTION_EAST,
                "output",
            )
            curr_x = cx + INTERSECTION_APPROACH_OFFSET

        builder.make_path(
            [(curr_x, h_lane_y), (v_lane_x, h_lane_y)], end_dir=DIRECTION_EAST, tier=tier
        )
        builder.make_path(
            [(v_lane_x, h_lane_y), (v_lane_x, in_y)], end_dir=DIRECTION_SOUTH, tier=tier
        )
        builder.make_path([(v_lane_x, in_y), (in_x, in_y)], end_dir=DIRECTION_EAST, tier=tier)

    def emit_fabric_entities(self, placement: object = None) -> list[dict[str, Any]]:
        """Emit all physical belts, intersections, and port structures for the chassis fabric."""
        entities = self.emit_channel_entities()
        if placement is None:
            return entities

        port_ents = self.emit_port_connections(placement=placement)
        port_by_pos = {
            (
                round(float(pe["position"]["x"]), COORD_ROUND_DIGITS),
                round(float(pe["position"]["y"]), COORD_ROUND_DIGITS),
            ): pe
            for pe in port_ents
            if "position" in pe
        }
        result: list[dict[str, Any]] = []
        for e in entities:
            pos = e.get("position", {})
            key = (
                round(float(pos.get("x", 0.0)), COORD_ROUND_DIGITS),
                round(float(pos.get("y", 0.0)), COORD_ROUND_DIGITS),
            )
            if key in port_by_pos:
                result.append(port_by_pos.pop(key))
            else:
                result.append(e)
        result.extend(port_by_pos.values())
        return result


@dataclass(slots=True)
class _EdgeScoreContext:
    """Parameters needed to evaluate candidate port edges on a block boundary."""

    blk: tuple[int, int]
    port_type: str
    internal_pt: tuple[int, int]
    target_pt: tuple[int, int]
    used_edge_coords: dict[tuple[int, int, str], list[tuple[int, str]]]
    v_seg_transit_load: dict[tuple[int, int], float] | None
    has_south_bay_neighbor: bool
    grid_w: int
    grid_h: int
    margin: int
    m_chan: int
    e_chan: int
    c_lo_base: int
    c_hi_off: int
    y_min_off: int
    cap_tracks: int
    channel_width: int


def _instance_row_pad(inst_name: str) -> float:
    """Return horizontal row placement padding for an instance name."""
    if "merge" in inst_name:
        return MERGE_CELL_PAD
    if "split" in inst_name or "bal" in inst_name:
        return SPLIT_BAL_CELL_PAD
    if any(k in inst_name for k in CIRCUIT_HELPER_KEYWORDS):
        return CIRCUIT_CELL_PAD
    return DEFAULT_CELL_PAD


def _instance_area(inst: object, cell_map: object) -> float:
    """Return footprint area of one instance."""
    inst_d: dict[str, Any] = inst if isinstance(inst, dict) else {}
    cells: dict[str, dict[str, Any]] = cell_map if isinstance(cell_map, dict) else {}
    cell_id = str(inst_d.get("cell", ""))
    cw, ch = _resolve_cell_dimensions(cell_id, cells.get(cell_id, {}))
    return cw * ch


def _instance_width_with_pad(inst: object, cell_map: object) -> float:
    """Return horizontal width plus placement pad of one instance."""
    inst_d: dict[str, Any] = inst if isinstance(inst, dict) else {}
    cells: dict[str, dict[str, Any]] = cell_map if isinstance(cell_map, dict) else {}
    cell_id = str(inst_d.get("cell", ""))
    cw, _ = _resolve_cell_dimensions(cell_id, cells.get(cell_id, {}))
    return cw + _instance_row_pad(str(inst_d.get("name", "")))


def _resolve_cell_dimensions(cell_id: str, cell_meta: object) -> tuple[float, float]:
    """Return (width, height) for cell_id from metadata or prototype size."""
    meta: dict[str, Any] = cell_meta if isinstance(cell_meta, dict) else {}
    cw = float(meta.get("width", 0))
    ch = float(meta.get("height", 0))
    if cw <= 0 or ch <= 0:
        ew, eh = entity_size(cell_id)
        if cw <= 0:
            cw = float(ew)
        if ch <= 0:
            ch = float(eh)
    return cw, ch


def _extract_group_pins_and_rank(
    inst_list: Sequence[object], cell_map: object
) -> tuple[set[str], set[str], int]:
    """Collect input nets, output nets, and average rank across a recipe group."""
    cells: dict[str, dict[str, Any]] = cell_map if isinstance(cell_map, dict) else {}
    inputs: set[str] = set()
    outputs: set[str] = set()
    ranks: list[int] = []

    for raw_inst in inst_list:
        inst: dict[str, Any] = raw_inst if isinstance(raw_inst, dict) else {}
        cell_meta = cells.get(str(inst.get("cell", "")), {})
        inputs.update(inp for inp in cell_meta.get("inputs", ()) if inp not in IGNORED_INPUT_PINS)
        outputs.update(
            out for out in cell_meta.get("outputs", ()) if out not in IGNORED_OUTPUT_PINS
        )
        if "rank" in inst:
            ranks.append(int(inst["rank"]))

    avg_rank = round(sum(ranks) / len(ranks)) if ranks else 0
    return inputs, outputs, avg_rank


def _resolve_grid_dimensions(
    num_blocks: int, grid_w: int | None, grid_h: int | None
) -> tuple[int, int]:
    """Determine (w, h) grid dimensions sufficient to hold num_blocks."""
    if (
        grid_w is not None
        and grid_h is not None
        and grid_w > 0
        and grid_h > 0
        and grid_w * grid_h >= num_blocks
    ):
        return grid_w, grid_h
    if grid_w is not None and grid_w > 0:
        return grid_w, max(1, math.ceil(num_blocks / float(grid_w)))
    if grid_h is not None and grid_h > 0:
        return max(1, math.ceil(num_blocks / float(grid_h))), grid_h
    s = max(1, math.ceil(math.sqrt(num_blocks)))
    return s, s


def _collect_inter_block_net_endpoints(
    instances: Sequence[object],
    cell_map: object,
    instance_to_block: dict[str, tuple[int, int]],
) -> tuple[
    dict[str, tuple[int, int]],
    dict[str, set[tuple[int, int]]],
    dict[str, str],
    dict[str, list[str]],
]:
    """Map each net name to its producer block/instance and consumer blocks/instances."""
    cells: dict[str, dict[str, Any]] = cell_map if isinstance(cell_map, dict) else {}
    net_producers: dict[str, tuple[int, int]] = {}
    net_consumers: dict[str, set[tuple[int, int]]] = {}
    net_producer_inst: dict[str, str] = {}
    net_consumer_insts: dict[str, list[str]] = {}

    for raw_inst in instances:
        inst: dict[str, Any] = raw_inst if isinstance(raw_inst, dict) else {}
        iname = str(inst.get("name", ""))
        block = instance_to_block.get(iname)
        if block is None:
            continue
        cell_meta = cells.get(str(inst.get("cell", "")), {})
        _register_instance_nets(
            iname,
            block,
            cell_meta,
            net_producers,
            net_consumers,
            net_producer_inst,
            net_consumer_insts,
        )

    return net_producers, net_consumers, net_producer_inst, net_consumer_insts


def _register_instance_nets(
    iname: str,
    block: tuple[int, int],
    cell_meta: object,
    net_producers: dict[str, tuple[int, int]],
    net_consumers: dict[str, set[tuple[int, int]]],
    net_producer_inst: dict[str, str],
    net_consumer_insts: dict[str, list[str]],
) -> None:
    """Record non-ignored input and output nets for one placed instance."""
    meta: dict[str, Any] = cell_meta if isinstance(cell_meta, dict) else {}
    for out_net in meta.get("outputs", ()):
        if out_net not in IGNORED_OUTPUT_PINS:
            net_producers[out_net] = block
            net_producer_inst[out_net] = iname
    for in_net in meta.get("inputs", ()):
        if in_net not in IGNORED_INPUT_PINS:
            net_consumers.setdefault(in_net, set()).add(block)
            net_consumer_insts.setdefault(in_net, []).append(iname)


def _allocate_L_route_segments(
    floorplan: MacroFloorplan,
    route: InterBlockRoute,
    source_block: tuple[int, int],
    target_block: tuple[int, int],
    tier: str,
    tolerate_full: bool = False,
) -> None:
    """Allocate horizontal and vertical channel segments and turns between two blocks."""
    sx, sy = source_block
    tx, ty = target_block
    if sx != tx:
        _allocate_horizontal_span(floorplan, route, sx, tx, sy, tier, tolerate_full)
    if sy != ty:
        _allocate_vertical_span(floorplan, route, sy, ty, tx, tier, tolerate_full)
    if sx != tx and sy != ty:
        _record_corner_turn(floorplan, route, tx, sy)


def _allocate_horizontal_span(
    floorplan: MacroFloorplan,
    route: InterBlockRoute,
    sx: int,
    tx: int,
    sy: int,
    tier: str,
    tolerate_full: bool,
) -> None:
    """Allocate horizontal street channel segments between sx and tx along row sy."""
    dir_h = DIRECTION_EAST if tx > sx else DIRECTION_WEST
    ch_y = sy if sy < floorplan.grid_h - 1 else max(0, sy - 1)
    for col in range(min(sx, tx), max(sx, tx)):
        _try_allocate_channel_segment(
            floorplan,
            route,
            f"h_channel_{col}_{ch_y}",
            "H",
            dir_h,
            route.net_name,
            tier,
            tolerate_full,
        )


def _allocate_vertical_span(
    floorplan: MacroFloorplan,
    route: InterBlockRoute,
    sy: int,
    ty: int,
    tx: int,
    tier: str,
    tolerate_full: bool,
) -> None:
    """Allocate vertical street channel segments between sy and ty along column tx."""
    dir_v = DIRECTION_NORTH if ty > sy else DIRECTION_SOUTH
    ch_x = tx if tx < floorplan.grid_w - 1 else max(0, tx - 1)
    for row in range(min(sy, ty), max(sy, ty)):
        _try_allocate_channel_segment(
            floorplan,
            route,
            f"v_channel_{ch_x}_{row}",
            "V",
            dir_v,
            route.net_name,
            tier,
            tolerate_full,
        )


def _record_corner_turn(
    floorplan: MacroFloorplan, route: InterBlockRoute, tx: int, sy: int
) -> None:
    """Record an intersection turn where horizontal and vertical route spans meet."""
    corner_y = sy if sy < floorplan.grid_h - 1 else max(1, sy)
    intersection = floorplan.intersections.get((tx, corner_y))
    if intersection:
        h_lane_idx, v_lane_idx = _last_hv_lane_indices(route.segments)
        intersection.add_turn("H", h_lane_idx, "V", v_lane_idx)
        route.turns.append((tx, corner_y, "H", h_lane_idx, "V", v_lane_idx))


def _try_allocate_channel_segment(
    floorplan: MacroFloorplan,
    route: InterBlockRoute,
    ch_id: str,
    orientation: str,
    direction: int,
    net_name: str,
    tier: str,
    tolerate_full: bool,
) -> None:
    """Allocate a lane on ch_id and append a ChannelRouteSegment to route."""
    channel = floorplan.channels.get(ch_id)
    if not channel:
        return
    try:
        lane = channel.allocate_lane(net_name, direction=direction, item_type=net_name, tier=tier)
        route.segments.append(
            ChannelRouteSegment(
                channel_id=ch_id,
                lane_index=lane.lane_index,
                orientation=orientation,
                direction=direction,
            )
        )
    except RuntimeError:
        if not tolerate_full:
            raise


def _last_hv_lane_indices(segments: list[ChannelRouteSegment]) -> tuple[int, int]:
    """Return the latest (h_lane_idx, v_lane_idx) from a list of route segments."""
    h_lane_idx = 0
    v_lane_idx = 0
    for seg in segments:
        if seg.orientation == "H":
            h_lane_idx = seg.lane_index
        elif seg.orientation == "V":
            v_lane_idx = seg.lane_index
    return h_lane_idx, v_lane_idx


class MacroFloorplanner:
    """Hierarchical Level 1 macro floorplanner for Factorilog city block layouts."""

    def __init__(
        self,
        chassis: CityBlockChassis | None = None,
        max_block_area: float = DEFAULT_MAX_BLOCK_AREA,
        max_block_width: float = DEFAULT_MAX_BLOCK_WIDTH,
        default_belt_tier: str = "fast-transport-belt",
    ) -> None:
        self._explicit_chassis = chassis is not None
        self.chassis = chassis or CityBlockChassis(
            pitch=DEFAULT_CHASSIS_PITCH,
            core_width=DEFAULT_CORE_DIM,
            core_height=DEFAULT_CORE_DIM,
            channel_width=DEFAULT_CHANNEL_WIDTH,
        )
        self.max_block_area = max_block_area
        self.max_block_width = max_block_width
        self.default_belt_tier = default_belt_tier

    def _cluster_width(
        self,
        inst_list: Sequence[object],
        cell_table: object,
    ) -> float:
        """Calculate total linear row placement width for a list of instances."""
        cell_map: dict[str, dict[str, Any]] = cell_table if isinstance(cell_table, dict) else {}
        total_w = 0.0
        for raw_inst in inst_list:
            inst: dict[str, Any] = raw_inst if isinstance(raw_inst, dict) else {}
            cell_id = str(inst.get("cell", ""))
            cw, _ = _resolve_cell_dimensions(cell_id, cell_map.get(cell_id, {}))
            pad = _instance_row_pad(str(inst.get("name", "")))
            total_w += cw + pad
        return total_w

    def _cluster_area(
        self,
        inst_list: Sequence[object],
        cell_table: object,
    ) -> float:
        """Calculate total machine footprint area for a list of instances."""
        cell_map: dict[str, dict[str, Any]] = cell_table if isinstance(cell_table, dict) else {}
        total_a = 0.0
        for raw_inst in inst_list:
            inst: dict[str, Any] = raw_inst if isinstance(raw_inst, dict) else {}
            cell_id = str(inst.get("cell", ""))
            cw, ch = _resolve_cell_dimensions(cell_id, cell_map.get(cell_id, {}))
            total_a += cw * ch
        return total_a

    def _split_cluster_recursive(
        self,
        inst_list: Sequence[object],
        cell_table: object,
        max_area: float,
        max_width: float,
    ) -> list[list[dict[str, Any]]]:
        """Recursively bisect cluster instances until both area and row width fit."""
        typed_insts: list[dict[str, Any]] = [inst for inst in inst_list if isinstance(inst, dict)]
        area = sum(_instance_area(inst, cell_table) for inst in typed_insts)
        width = sum(_instance_width_with_pad(inst, cell_table) for inst in typed_insts)

        if (area <= max_area and width <= max_width) or len(typed_insts) <= 1:
            return [typed_insts]

        mid = len(typed_insts) // 2
        left_splits = self._split_cluster_recursive(
            typed_insts[:mid], cell_table, max_area, max_width
        )
        right_splits = self._split_cluster_recursive(
            typed_insts[mid:], cell_table, max_area, max_width
        )
        return left_splits + right_splits

    def cluster_netlist(self, netlist_data: object) -> list[RecipeCluster]:
        """Group netlist instances into recipe clusters and divide oversized stages."""
        net_dict: dict[str, Any] = netlist_data if isinstance(netlist_data, dict) else {}
        instances: list[dict[str, Any]] = net_dict.get("instances", [])
        cell_map: dict[str, dict[str, Any]] = net_dict.get("cells", {})

        recipe_groups: dict[str, list[dict[str, Any]]] = {}
        for inst in instances:
            recipe = str(inst.get("recipe", "") or inst.get("cell", "generic"))
            recipe_groups.setdefault(recipe, []).append(inst)

        clusters: list[RecipeCluster] = []
        for recipe, inst_list in sorted(recipe_groups.items()):
            inputs, outputs, avg_rank = _extract_group_pins_and_rank(inst_list, cell_map)
            clusters.extend(
                self._build_recipe_clusters(recipe, inst_list, inputs, outputs, avg_rank, cell_map)
            )
        return clusters

    def _build_recipe_clusters(
        self,
        recipe: str,
        inst_list: Sequence[object],
        inputs: set[str],
        outputs: set[str],
        avg_rank: int,
        cell_map: object,
    ) -> list[RecipeCluster]:
        """Build one or more RecipeCluster objects for a single recipe group."""
        splits = self._split_cluster_recursive(
            inst_list, cell_map, self.max_block_area, self.max_block_width
        )
        total_splits = len(splits)
        if total_splits == 1:
            only_split = splits[0]
            return [
                RecipeCluster(
                    name=f"cluster_{recipe}",
                    recipe=recipe,
                    instances=[str(inst["name"]) for inst in only_split],
                    total_area=sum(_instance_area(inst, cell_map) for inst in only_split),
                    rank=avg_rank,
                    inputs=inputs,
                    outputs=outputs,
                )
            ]

        return [
            RecipeCluster(
                name=f"cluster_{recipe}_part_{i}",
                recipe=recipe,
                instances=[str(inst["name"]) for inst in split_insts],
                total_area=sum(_instance_area(inst, cell_map) for inst in split_insts),
                rank=avg_rank,
                inputs=set(inputs),
                outputs=set(outputs),
                sub_cluster_index=i,
                total_sub_clusters=total_splits,
            )
            for i, split_insts in enumerate(splits)
        ]

    def _grid_allocation_order(self, w: int, h: int) -> list[tuple[int, int]]:
        """Order grid cells from outermost perimeter to innermost center rings."""
        cells: list[tuple[int, int]] = []
        min_x, max_x = 0, w - 1
        min_y, max_y = 0, h - 1

        while min_x <= max_x and min_y <= max_y:
            if min_x == max_x or min_y == max_y:
                cells.extend(
                    (x, y) for x in range(min_x, max_x + 1) for y in range(min_y, max_y + 1)
                )
                break

            cells.extend((x, min_y) for x in range(min_x, max_x))
            cells.extend((max_x, y) for y in range(min_y, max_y))
            cells.extend((x, max_y) for x in range(max_x, min_x, -1))
            cells.extend((min_x, y) for y in range(max_y, min_y, -1))

            min_x += 1
            max_x -= 1
            min_y += 1
            max_y -= 1

        return cells

    def plan_grid(
        self,
        clusters: list[RecipeCluster],
        grid_w: int | None = None,
        grid_h: int | None = None,
    ) -> MacroFloorplan:
        """Map clusters to city blocks and instantiate channels and crossroads."""
        w, h = _resolve_grid_dimensions(len(clusters), grid_w, grid_h)
        floorplan = MacroFloorplan(
            grid_w=w,
            grid_h=h,
            chassis=self.chassis,
            clusters=clusters,
        )

        sorted_clusters = sorted(
            clusters,
            key=lambda c: (c.rank, c.recipe, c.sub_cluster_index),
        )
        alloc_order = self._grid_allocation_order(w, h)

        for idx, cluster in enumerate(sorted_clusters):
            bx, by = alloc_order[idx]
            cluster.assigned_block = (bx, by)
            floorplan.block_to_clusters.setdefault((bx, by), []).append(cluster)
            for inst_name in cluster.instances:
                floorplan.instance_to_block[inst_name] = (bx, by)

        for bx, by in alloc_order[len(sorted_clusters) :]:
            floorplan.empty_blocks.add((bx, by))

        self._instantiate_channels_and_intersections(floorplan, w, h)
        return floorplan

    def _instantiate_channels_and_intersections(
        self, floorplan: MacroFloorplan, w: int, h: int
    ) -> None:
        """Populate horizontal channels, vertical channels, and 4-way intersections."""
        for by in range(h - 1):
            for bx in range(w):
                ch = self.chassis.get_horizontal_channel(
                    bx, by, default_tier=self.default_belt_tier
                )
                floorplan.channels[ch.channel_id] = ch

        for bx in range(w - 1):
            for by in range(h):
                ch = self.chassis.get_vertical_channel(bx, by, default_tier=self.default_belt_tier)
                floorplan.channels[ch.channel_id] = ch

        for ix in range(1, w):
            for iy in range(1, h):
                h_ch = floorplan.channels.get(f"h_channel_{ix - 1}_{iy - 1}")
                v_ch = floorplan.channels.get(f"v_channel_{ix - 1}_{iy - 1}")
                floorplan.intersections[(ix, iy)] = self.chassis.get_intersection(
                    ix, iy, h_channel=h_ch, v_channel=v_ch
                )

    def assign_inter_block_routes(
        self,
        floorplan: MacroFloorplan,
        netlist_data: object,
    ) -> None:
        """Find inter-block nets and allocate channel lanes along transit paths."""
        net_dict: dict[str, Any] = netlist_data if isinstance(netlist_data, dict) else {}
        fluids = set(net_dict.get("fluids", []))
        cell_map: dict[str, dict[str, Any]] = net_dict.get("cells", {})
        instances: list[dict[str, Any]] = net_dict.get("instances", [])

        net_producers, net_consumers, net_producer_inst, net_consumer_insts = (
            _collect_inter_block_net_endpoints(instances, cell_map, floorplan.instance_to_block)
        )

        all_nets = set(net_producers.keys()) | set(net_consumers.keys())
        for net_name in sorted(all_nets):
            source_block = net_producers.get(net_name)
            target_blocks = sorted(net_consumers.get(net_name, set()))
            if not self._is_inter_block_net(source_block, target_blocks):
                continue

            is_fluid = net_name in fluids
            tier = "pipe" if is_fluid else self.default_belt_tier
            route = InterBlockRoute(
                net_name=net_name,
                source_block=source_block,
                target_blocks=target_blocks,
                is_fluid=is_fluid,
                tier=tier,
                source_instance=net_producer_inst.get(net_name),
                target_instances=net_consumer_insts.get(net_name, []),
            )
            self._route_net_to_targets(floorplan, route, source_block, target_blocks, tier)
            floorplan.routes[net_name] = route

    @staticmethod
    def _route_net_to_targets(
        floorplan: MacroFloorplan,
        route: InterBlockRoute,
        source_block: tuple[int, int] | None,
        target_blocks: list[tuple[int, int]],
        tier: str,
    ) -> None:
        """Allocate L-shaped channel segments from source_block to each distinct target block."""
        if source_block is None:
            return
        for target_block in target_blocks:
            if target_block != source_block:
                _allocate_L_route_segments(floorplan, route, source_block, target_block, tier)

    @staticmethod
    def _is_inter_block_net(
        source_block: tuple[int, int] | None, target_blocks: list[tuple[int, int]]
    ) -> bool:
        """Return True when a net crosses block boundaries."""
        if source_block is None and not target_blocks:
            return False
        return not (source_block is not None and all(tb == source_block for tb in target_blocks))

    def plan(
        self,
        netlist_data: object,
        grid_w: int | None = None,
        grid_h: int | None = None,
    ) -> MacroFloorplan:
        """Run full Level 1 macro floorplanning pipeline."""
        clusters = self.cluster_netlist(netlist_data)
        floorplan = self.plan_grid(clusters, grid_w=grid_w, grid_h=grid_h)
        self.assign_inter_block_routes(floorplan, netlist_data)
        return floorplan

    @staticmethod
    def _block_of_coord(
        pos: tuple[int, int],
        grid_w: int,
        grid_h: int,
        bay_blocks: set[tuple[int, int]] | None = None,
        margin: int = DEFAULT_CORE_MARGIN,
    ) -> tuple[int, int] | None:
        """Return (bx, by) if pos lies inside a city block core (excluding matrix bays), else None."""
        x, y = pos
        bx = x // DEFAULT_CHASSIS_PITCH
        by = y // DEFAULT_CHASSIS_PITCH
        hi = BLOCK_MAX_LOCAL_COORD - margin
        if 0 <= bx < grid_w and 0 <= by < grid_h and margin <= (x % DEFAULT_CHASSIS_PITCH) <= hi:
            y_lo = BAY_CORE_Y_LO if (bay_blocks and (bx, by) in bay_blocks) else margin
            if y_lo <= (y % DEFAULT_CHASSIS_PITCH) <= hi:
                return (bx, by)
        return None

    @staticmethod
    def _block_core_bbox(
        blk: tuple[int, int],
        bay_blocks: set[tuple[int, int]] | None = None,
        margin: int = DEFAULT_CORE_MARGIN,
    ) -> tuple[int, int, int, int]:
        """Return the (min_x, min_y, max_x, max_y) tile bounds for a cell core."""
        bx, by = blk
        hi = BLOCK_MAX_LOCAL_COORD - margin
        y_lo = by * DEFAULT_CHASSIS_PITCH + (
            BAY_CORE_Y_LO if (bay_blocks and blk in bay_blocks) else margin
        )
        return (
            bx * DEFAULT_CHASSIS_PITCH + margin,
            y_lo,
            bx * DEFAULT_CHASSIS_PITCH + hi,
            by * DEFAULT_CHASSIS_PITCH + hi,
        )

    @staticmethod
    def _street_segment_of_edge(
        bx: int,
        by: int,
        edge: str,
    ) -> tuple[str, int, int]:
        """Return the canonical street segment identifier ('V'|'H', ix, iy) for a block wall."""
        if edge == "W":
            return ("V", bx, by)
        if edge == "E":
            return ("V", bx + 1, by)
        if edge == "N":
            return ("H", bx, by)
        return ("H", bx, by + 1)

    @staticmethod
    def _street_segment_of_coord(
        pos: tuple[int, int],
        grid_w: int,
        grid_h: int,
        bay_blocks: set[tuple[int, int]] | None = None,
        margin: int = DEFAULT_CORE_MARGIN,
    ) -> tuple[str, int, int] | None:
        """Return the canonical street segment for a street channel or Matrix Switchbox Bay coordinate."""
        x, y = pos
        bx = x // DEFAULT_CHASSIS_PITCH
        by = y // DEFAULT_CHASSIS_PITCH
        hi = BLOCK_MAX_LOCAL_COORD - margin
        chan_w = 2 * margin
        if (
            bay_blocks
            and (bx, by) in bay_blocks
            and 0 <= bx < grid_w
            and 0 <= by < grid_h
            and margin <= (x % DEFAULT_CHASSIS_PITCH) <= hi
            and margin <= (y % DEFAULT_CHASSIS_PITCH) <= BAY_STREET_Y_MAX
        ):
            return ("H", bx, by)
        in_v = (x + margin) % DEFAULT_CHASSIS_PITCH < chan_w
        in_h = (y + margin) % DEFAULT_CHASSIS_PITCH < chan_w
        if in_v and not in_h and 0 <= by < grid_h:
            return ("V", (x + margin) // DEFAULT_CHASSIS_PITCH, by)
        if in_h and not in_v and 0 <= bx < grid_w:
            return ("H", bx, (y + margin) // DEFAULT_CHASSIS_PITCH)
        return None

    @staticmethod
    def _highway_corridor_bbox(
        src: tuple[int, int],
        snk: tuple[int, int],
        min_x: int,
        min_y: int,
        max_x: int,
        max_y: int,
        margin: int = DEFAULT_CORE_MARGIN,
    ) -> tuple[int, int, int, int]:
        """Return the street channel bounding box enclosing src and snk."""
        sx, sy = src
        tx, ty = snk
        x_lo, x_hi = min(sx, tx), max(sx, tx)
        y_lo, y_hi = min(sy, ty), max(sy, ty)
        m_chan = margin - 1
        m_hi = DEFAULT_CHASSIS_PITCH - margin
        bx_lo = (
            ((x_lo // DEFAULT_CHASSIS_PITCH) + 1) * DEFAULT_CHASSIS_PITCH - margin
            if (x_lo % DEFAULT_CHASSIS_PITCH) >= m_hi
            else (x_lo // DEFAULT_CHASSIS_PITCH) * DEFAULT_CHASSIS_PITCH - margin
        )
        bx_hi = (
            (x_hi // DEFAULT_CHASSIS_PITCH) * DEFAULT_CHASSIS_PITCH + m_chan
            if (x_hi % DEFAULT_CHASSIS_PITCH) <= m_chan
            else ((x_hi // DEFAULT_CHASSIS_PITCH) + 1) * DEFAULT_CHASSIS_PITCH + m_chan
        )
        by_lo = (
            ((y_lo // DEFAULT_CHASSIS_PITCH) + 1) * DEFAULT_CHASSIS_PITCH - margin
            if (y_lo % DEFAULT_CHASSIS_PITCH) >= m_hi
            else (y_lo // DEFAULT_CHASSIS_PITCH) * DEFAULT_CHASSIS_PITCH - margin
        )
        by_hi = (
            (y_hi // DEFAULT_CHASSIS_PITCH) * DEFAULT_CHASSIS_PITCH + BAY_STREET_Y_MAX
            if (y_hi % DEFAULT_CHASSIS_PITCH) <= BAY_STREET_Y_MAX
            else ((y_hi // DEFAULT_CHASSIS_PITCH) + 1) * DEFAULT_CHASSIS_PITCH + m_chan
        )
        return (
            max(min_x, bx_lo),
            max(min_y, by_lo),
            min(max_x, bx_hi),
            min(max_y, by_hi),
        )

    @staticmethod
    def _edge_port_geometry(
        blk: tuple[int, int],
        edge: str,
        port_type: str,
        c: int,
        margin: int = DEFAULT_CORE_MARGIN,
    ) -> tuple[
        tuple[int, int],
        tuple[int, int],
        tuple[int, int],
        tuple[int, int],
        int,
        tuple[int, int, str],
    ]:
        """Return (core_pos, channel_pos, in_pos, out_pos, port_dir, opp_edge_key)."""
        bx, by = blk
        m_chan = margin - 1
        e_core = BLOCK_MAX_LOCAL_COORD - margin
        e_chan = DEFAULT_CHASSIS_PITCH - margin
        is_export = port_type == "export"

        if edge == "W":
            x_base = bx * DEFAULT_CHASSIS_PITCH
            return (
                (x_base + margin, c),
                (x_base + m_chan, c),
                (x_base + margin + 1, c),
                (x_base + m_chan - 1, c),
                DIRECTION_WEST if is_export else DIRECTION_EAST,
                (bx - 1, by, "E"),
            )
        if edge == "E":
            x_base = bx * DEFAULT_CHASSIS_PITCH
            return (
                (x_base + e_core, c),
                (x_base + e_chan, c),
                (x_base + e_core - 1, c),
                (x_base + e_chan + 1, c),
                DIRECTION_EAST if is_export else DIRECTION_WEST,
                (bx + 1, by, "W"),
            )
        if edge == "N":
            y_base = by * DEFAULT_CHASSIS_PITCH
            return (
                (c, y_base + margin),
                (c, y_base + m_chan),
                (c, y_base + margin + 1),
                (c, y_base + m_chan - 1),
                DIRECTION_NORTH if is_export else DIRECTION_SOUTH,
                (bx, by - 1, "S"),
            )
        y_base = by * DEFAULT_CHASSIS_PITCH
        return (
            (c, y_base + e_core),
            (c, y_base + e_chan),
            (c, y_base + e_core - 1),
            (c, y_base + e_chan + 1),
            DIRECTION_SOUTH if is_export else DIRECTION_NORTH,
            (bx, by + 1, "N"),
        )

    def _allocate_block_port(
        self,
        blk: tuple[int, int],
        net_name: str,
        port_type: str,
        internal_pt: tuple[int, int],
        target_pt: tuple[int, int],
        obstacles: set[tuple[int, int]],
        used_edge_coords: dict[tuple[int, int, str], list[tuple[int, str]]],
        pin_reserved_coords: dict[tuple[int, int, str], list[tuple[int, tuple[int, int]]]],
        all_terminals: set[tuple[int, int]],
        preferred_edge: str | None = None,
        preferred_c: int | None = None,
        forbidden_edges: set[str] | None = None,
        has_north_bay: bool = False,
        has_south_bay_neighbor: bool = False,
        tier: str = "fast-transport-belt",
        grid_w: int = 1,
        grid_h: int = 1,
        v_seg_transit_load: dict[tuple[int, int], float] | None = None,
        margin: int | None = None,
    ) -> tuple[BlockPort, tuple[int, int], tuple[int, int]]:
        """Allocate a conflict-free BlockPort on the boundary of blk."""
        eff_margin = self.chassis.core_margin_x if margin is None else margin
        c_lo_base = eff_margin + PORT_CORNER_CLEARANCE
        ctx = _EdgeScoreContext(
            blk=blk,
            port_type=port_type,
            internal_pt=internal_pt,
            target_pt=target_pt,
            used_edge_coords=used_edge_coords,
            v_seg_transit_load=v_seg_transit_load,
            has_south_bay_neighbor=has_south_bay_neighbor,
            grid_w=grid_w,
            grid_h=grid_h,
            margin=eff_margin,
            m_chan=eff_margin - 1,
            e_chan=DEFAULT_CHASSIS_PITCH - eff_margin,
            c_lo_base=c_lo_base,
            c_hi_off=BLOCK_MAX_LOCAL_COORD - eff_margin - PORT_CORNER_CLEARANCE,
            y_min_off=NORTH_BAY_PORT_Y_MIN if has_north_bay else c_lo_base,
            cap_tracks=max(MIN_CAP_TRACKS, self.chassis.channel_width - CAP_TRACK_MARGIN),
            channel_width=self.chassis.channel_width,
        )

        edge_order = _rank_candidate_edges(ctx, forbidden_edges, has_north_bay, preferred_edge)

        for spacing in PORT_SPACING_OPTIONS:
            for edge in edge_order:
                found = self._try_allocate_on_edge(
                    ctx,
                    edge,
                    spacing,
                    net_name,
                    tier,
                    preferred_c,
                    obstacles,
                    pin_reserved_coords,
                    all_terminals,
                )
                if found is not None:
                    return found

        return self._fallback_block_port(ctx, edge_order[0], net_name, tier)

    def _try_allocate_on_edge(
        self,
        ctx: _EdgeScoreContext,
        edge: str,
        spacing: int,
        net_name: str,
        tier: str,
        preferred_c: int | None,
        obstacles: set[tuple[int, int]],
        pin_reserved_coords: dict[tuple[int, int, str], list[tuple[int, tuple[int, int]]]],
        all_terminals: set[tuple[int, int]],
    ) -> tuple[BlockPort, tuple[int, int], tuple[int, int]] | None:
        """Search candidate coordinates along edge for a conflict-free port."""
        bx, by = ctx.blk
        ideal_c, lo_c, hi_c = _ideal_edge_coord(ctx, edge, preferred_c)
        candidates = sorted(
            range(lo_c, hi_c + 1),
            key=lambda c_val: (abs(c_val - ideal_c), c_val),
        )
        edge_key = (bx, by, edge)
        eff_spacing = _effective_port_spacing(spacing, tier, bx, ctx.grid_w)
        forbidden_c = _precompute_forbidden_edge_coords(
            eff_spacing,
            ctx.used_edge_coords.get(edge_key, []),
            pin_reserved_coords.get(edge_key, []),
            ctx.internal_pt,
            ctx.target_pt,
        )
        endpoint_set = {ctx.internal_pt, ctx.target_pt}

        for c_val in candidates:
            if c_val in forbidden_c:
                continue
            geom = self._edge_port_geometry(ctx.blk, edge, ctx.port_type, c_val, margin=ctx.margin)
            core_pos, channel_pos, in_pos, out_pos, port_dir, opp_key = geom
            if not _is_port_geometry_clear(
                c_val,
                spacing,
                net_name,
                (core_pos, channel_pos, in_pos, out_pos),
                ctx.used_edge_coords.get(opp_key, []),
                obstacles,
                all_terminals,
                endpoint_set,
            ):
                continue

            ctx.used_edge_coords.setdefault(edge_key, []).append((c_val, net_name))
            return _build_allocated_block_port(
                ctx, edge, c_val, net_name, tier, core_pos, channel_pos, port_dir
            )
        return None

    def _fallback_block_port(
        self,
        ctx: _EdgeScoreContext,
        edge: str,
        net_name: str,
        tier: str,
    ) -> tuple[BlockPort, tuple[int, int], tuple[int, int]]:
        """Allocate a port at the clamped ideal coordinate when all candidates conflict."""
        bx, by = ctx.blk
        ix, iy = ctx.internal_pt
        base = by * DEFAULT_CHASSIS_PITCH if edge in ("W", "E") else bx * DEFAULT_CHASSIS_PITCH
        c_lo_off = ctx.y_min_off if edge in ("W", "E") else ctx.c_lo_base
        c_val = max(
            base + c_lo_off,
            min(base + ctx.c_hi_off, iy if edge in ("W", "E") else ix),
        )
        core_pos, channel_pos, _in_p, _out_p, port_dir, _opp = self._edge_port_geometry(
            ctx.blk, edge, ctx.port_type, c_val, margin=ctx.margin
        )
        ctx.used_edge_coords.setdefault((bx, by, edge), []).append((c_val, net_name))
        port = BlockPort(
            name=f"{net_name}_{ctx.port_type}_{bx}_{by}_{edge}_{c_val}",
            block=ctx.blk,
            net_name=net_name,
            port_type=ctx.port_type,
            edge=edge,
            position=(float(channel_pos[0]), float(channel_pos[1])),
            channel_id=f"ch_{bx}_{by}_{edge}",
            lane_index=0,
            direction=port_dir,
            tier=tier,
            internal_pos=(float(core_pos[0]), float(core_pos[1])),
        )
        return port, core_pos, channel_pos

    def plan_hierarchical_nets(
        self,
        net_specs: list[RouterNetSpec],
        grid_w: int,
        grid_h: int,
        obstacles: set[tuple[int, int]],
        min_x: int = 0,
        min_y: int = 0,
        max_x: int | None = None,
        max_y: int | None = None,
        bay_blocks: set[tuple[int, int]] | None = None,
    ) -> tuple[
        dict[tuple[int, int], list[RouterNetSpec]],
        list[RouterNetSpec],
        set[tuple[int, int]],
        dict[tuple[int, int], int],
        MacroFloorplan,
    ]:
        """Decompose flat RouterNetSpecs into intra-block core specs and street highway specs.

        Returns:
            intra_block_specs: Mapping of (bx, by) -> list of RouterNetSpec inside that block core.
            highway_specs: List of RouterNetSpec for the street channels and switchboxes.
            port_no_hood_tiles: Set of (x, y) boundary tiles where underground hoods are forbidden.
            port_headings: Mapping of (x, y) -> heading for allocated belt port tiles.
            floorplan: Populated MacroFloorplan with channels, intersections, ports, and routes.
        """
        eff_max_x = grid_w * DEFAULT_CHASSIS_PITCH if max_x is None else max_x
        eff_max_y = grid_h * DEFAULT_CHASSIS_PITCH if max_y is None else max_y

        if not self._explicit_chassis and grid_w * grid_h >= PROGRAMMATIC_GRID_THRESHOLD:
            self.chassis = CityBlockChassis(
                pitch=DEFAULT_CHASSIS_PITCH,
                core_width=PROGRAMMATIC_CORE_DIM,
                core_height=PROGRAMMATIC_CORE_DIM,
                channel_width=PROGRAMMATIC_CHANNEL_WIDTH,
                programmatic=True,
            )
        margin = self.chassis.core_margin_x
        eff_bays = (
            _detect_bay_blocks(net_specs, grid_w, grid_h) if bay_blocks is None else bay_blocks
        )

        floorplan = self.plan_grid([], grid_w=grid_w, grid_h=grid_h)
        floorplan.empty_blocks.clear()

        state = _HierarchicalPlanState.initialize(
            self,
            floorplan=floorplan,
            net_specs=net_specs,
            grid_w=grid_w,
            grid_h=grid_h,
            obstacles=obstacles,
            bounds=(min_x, min_y, eff_max_x, eff_max_y),
            bay_blocks=eff_bays,
            margin=margin,
        )

        solid_specs = [s for s in net_specs if not s.is_fluid]
        fluid_specs = [s for s in net_specs if s.is_fluid]

        self._compute_solid_v_transit_loads(solid_specs, state)
        for net in solid_specs:
            self._plan_single_solid_net(net, state)
        self._plan_all_fluid_groups(fluid_specs, state)

        return (
            state.intra_block_specs,
            state.highway_specs,
            state.port_no_hood_tiles,
            state.port_headings,
            floorplan,
        )

    def _compute_solid_v_transit_loads(
        self, solid_specs: list[RouterNetSpec], state: _HierarchicalPlanState
    ) -> None:
        """Accumulate estimated vertical street transit loads across inter-block solid nets."""
        if state.grid_w <= 1:
            return
        for net in solid_specs:
            src_b = self._block_of_coord(
                net.source, state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
            )
            dst_b = self._block_of_coord(
                net.sink, state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
            )
            if src_b is None or dst_b is None or src_b != dst_b:
                _record_v_transit_pair(net.source, net.sink, state)

    def _plan_single_solid_net(self, net: RouterNetSpec, state: _HierarchicalPlanState) -> None:
        """Decompose a single solid net into intra-block and highway RouterNetSpecs."""
        RouterNetSpecCls = _load_router_net_spec_cls()
        src_blk = self._block_of_coord(
            net.source, state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
        )
        dst_blk = self._block_of_coord(
            net.sink, state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
        )

        if src_blk is not None and src_blk == dst_blk:
            bbox = self._block_core_bbox(src_blk, state.bay_blocks, margin=state.margin)
            state.intra_block_specs[src_blk].append(
                RouterNetSpecCls(
                    name=net.name,
                    source=net.source,
                    sink=net.sink,
                    commodity=net.commodity,
                    lane=net.lane,
                    is_fluid=False,
                    start_heading=net.start_heading,
                    end_heading=net.end_heading,
                    terminal_heading=net.terminal_heading,
                    corridor_bbox=bbox,
                )
            )
            return

        min_x, min_y, max_x, max_y = state.bounds
        if src_blk is None and dst_blk is None:
            state.highway_specs.append(
                RouterNetSpecCls(
                    name=f"{net.name}_hwy",
                    source=net.source,
                    sink=net.sink,
                    commodity=net.commodity,
                    lane=net.lane,
                    is_fluid=False,
                    start_heading=net.start_heading,
                    end_heading=net.end_heading,
                    terminal_heading=net.terminal_heading,
                    corridor_bbox=self._highway_corridor_bbox(
                        net.source, net.sink, min_x, min_y, max_x, max_y, margin=state.margin
                    ),
                )
            )
            return

        p_out, hwy_src, hwy_start_h, src_seg = self._allocate_solid_export(net, src_blk, state)
        p_in, hwy_snk, hwy_end_h, hwy_term_h = self._allocate_solid_import(
            net, dst_blk, hwy_src, src_seg, state
        )

        state.highway_specs.append(
            RouterNetSpecCls(
                name=f"{net.name}_hwy",
                source=hwy_src,
                sink=hwy_snk,
                commodity=net.commodity,
                lane=net.lane,
                is_fluid=False,
                start_heading=hwy_start_h,
                end_heading=hwy_end_h,
                terminal_heading=hwy_term_h,
                corridor_bbox=self._highway_corridor_bbox(
                    hwy_src, hwy_snk, min_x, min_y, max_x, max_y, margin=state.margin
                ),
            )
        )
        self._record_channel_route(state.floorplan, net.name, src_blk, dst_blk, False, p_out, p_in)

    def _allocate_solid_export(
        self,
        net: RouterNetSpec,
        src_blk: tuple[int, int] | None,
        state: _HierarchicalPlanState,
    ) -> tuple[
        BlockPort | None,
        tuple[int, int],
        int | None,
        tuple[str, int, int] | None,
    ]:
        """Allocate source block export port and intra-block spec if src_blk is inside a block."""
        src_seg = self._street_segment_of_coord(
            net.source, state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
        )
        if src_blk is None:
            return None, net.source, net.start_heading, src_seg

        RouterNetSpecCls = _load_router_net_spec_cls()
        snk_seg_init = self._street_segment_of_coord(
            net.sink, state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
        )
        src_forbid = _edges_matching_segment(src_blk, snk_seg_init)
        p_out, out_core, out_chan = self._allocate_block_port(
            blk=src_blk,
            net_name=net.name,
            port_type="export",
            internal_pt=net.source,
            target_pt=net.sink,
            obstacles=state.obstacles,
            used_edge_coords=state.used_edge_coords,
            pin_reserved_coords=state.pin_reserved_coords,
            all_terminals=state.all_terminals,
            forbidden_edges=src_forbid,
            has_north_bay=(src_blk in state.bay_blocks),
            has_south_bay_neighbor=((src_blk[0], src_blk[1] + 1) in state.bay_blocks),
            tier=self.default_belt_tier,
            grid_w=state.grid_w,
            grid_h=state.grid_h,
            v_seg_transit_load=state.v_seg_transit_load,
            margin=state.margin,
        )
        state.register_solid_port(p_out, out_core, out_chan)
        state.intra_block_specs[src_blk].append(
            RouterNetSpecCls(
                name=f"{net.name}_export_{src_blk[0]}_{src_blk[1]}",
                source=net.source,
                sink=out_core,
                commodity=net.commodity,
                lane=net.lane,
                is_fluid=False,
                start_heading=net.start_heading,
                end_heading=p_out.direction,
                terminal_heading=p_out.direction,
                corridor_bbox=self._block_core_bbox(src_blk, state.bay_blocks, margin=state.margin),
            )
        )
        return (
            p_out,
            out_chan,
            p_out.direction,
            self._street_segment_of_edge(src_blk[0], src_blk[1], p_out.edge),
        )

    def _allocate_solid_import(
        self,
        net: RouterNetSpec,
        dst_blk: tuple[int, int] | None,
        hwy_src: tuple[int, int],
        src_seg: tuple[str, int, int] | None,
        state: _HierarchicalPlanState,
    ) -> tuple[BlockPort | None, tuple[int, int], int | None, int | None]:
        """Allocate destination block import port and intra-block spec if dst_blk is inside a block."""
        if dst_blk is None:
            return None, net.sink, net.end_heading, net.terminal_heading

        RouterNetSpecCls = _load_router_net_spec_cls()
        dst_forbid = _edges_matching_segment(dst_blk, src_seg)
        p_in, in_core, in_chan = self._allocate_block_port(
            blk=dst_blk,
            net_name=net.name,
            port_type="import",
            internal_pt=net.sink,
            target_pt=hwy_src,
            obstacles=state.obstacles,
            used_edge_coords=state.used_edge_coords,
            pin_reserved_coords=state.pin_reserved_coords,
            all_terminals=state.all_terminals,
            forbidden_edges=dst_forbid,
            has_north_bay=(dst_blk in state.bay_blocks),
            has_south_bay_neighbor=((dst_blk[0], dst_blk[1] + 1) in state.bay_blocks),
            tier=self.default_belt_tier,
            grid_w=state.grid_w,
            grid_h=state.grid_h,
            v_seg_transit_load=state.v_seg_transit_load,
            margin=state.margin,
        )
        state.register_solid_port(p_in, in_core, in_chan)
        state.intra_block_specs[dst_blk].append(
            RouterNetSpecCls(
                name=f"{net.name}_import_{dst_blk[0]}_{dst_blk[1]}",
                source=in_core,
                sink=net.sink,
                commodity=net.commodity,
                lane=net.lane,
                is_fluid=False,
                start_heading=p_in.direction,
                end_heading=net.end_heading,
                terminal_heading=net.terminal_heading,
                corridor_bbox=self._block_core_bbox(dst_blk, state.bay_blocks, margin=state.margin),
            )
        )
        return p_in, in_chan, p_in.direction, p_in.direction

    def _plan_all_fluid_groups(
        self, fluid_specs: list[RouterNetSpec], state: _HierarchicalPlanState
    ) -> None:
        """Group fluid MST specs by base net name and decompose each group."""
        fluid_groups: dict[str, list[RouterNetSpec]] = {}
        for fspec in fluid_specs:
            b_name = fspec.name.split("_mst_")[0]
            if "_d" in b_name and "_s" in b_name:
                b_name = b_name.split("_d")[0]
            fluid_groups.setdefault(b_name, []).append(fspec)

        for b_name, f_list in fluid_groups.items():
            self._plan_single_fluid_group(b_name, f_list, state)

    def _plan_single_fluid_group(
        self,
        b_name: str,
        f_list: list[RouterNetSpec],
        state: _HierarchicalPlanState,
    ) -> None:
        """Decompose a single fluid net group into intra-block MSTs and a highway MST."""
        comm = f_list[0].commodity
        pin_terms, blk_terms = self._partition_fluid_terminals(f_list, state)

        if not pin_terms and len(blk_terms) <= 1:
            self._emit_local_or_pin_only_fluid_specs(f_list, blk_terms, state)
            return

        hwy_nodes: list[tuple[tuple[int, int], int | None, int | None]] = list(pin_terms)
        sorted_blks = sorted(blk_terms.keys())
        first_blk: tuple[int, int] | None = sorted_blks[0] if sorted_blks else None
        used_fluid_segs: set[tuple[str, int, int]] = {
            seg
            for p in pin_terms
            if (
                seg := self._street_segment_of_coord(
                    p[0], state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
                )
            )
            is not None
        }
        other_pts_by_blk = _precompute_other_fluid_pts(pin_terms, blk_terms)
        single_blk_with_pins = bool(pin_terms and len(blk_terms) == 1)

        for blk in sorted_blks:
            t_list = blk_terms[blk]
            f_forbid = {
                e
                for e in ALL_CARDINAL_EDGES
                if self._street_segment_of_edge(blk[0], blk[1], e) in used_fluid_segs
            }
            f_port, f_core, f_chan = self._allocate_fluid_block_port(
                b_name,
                blk,
                t_list,
                other_pts_by_blk[blk],
                f_forbid,
                is_import=bool(pin_terms or blk != first_blk),
                single_blk_with_pins=single_blk_with_pins,
                state=state,
            )
            used_fluid_segs.add(self._street_segment_of_edge(blk[0], blk[1], f_port.edge))
            state.register_fluid_port(f_port, f_core, f_chan)

            local_nodes: list[tuple[tuple[int, int], int | None, int | None]] = [
                (f_core, None, None),
                *t_list,
            ]
            bbox = self._block_core_bbox(blk, state.bay_blocks, margin=state.margin)
            state.intra_block_specs[blk].extend(
                _build_prim_mst_specs(
                    local_nodes,
                    prefix=f"{b_name}_blk_{blk[0]}_{blk[1]}_mst_",
                    commodity=comm,
                    fixed_bbox=bbox,
                    bounds=state.bounds,
                    margin=state.margin,
                )
            )
            hwy_nodes.append((f_chan, None, None))
            if first_blk is not None and blk != first_blk:
                self._record_channel_route(
                    state.floorplan, b_name, first_blk, blk, True, None, f_port
                )

        if len(hwy_nodes) >= 2:
            state.highway_specs.extend(
                _build_prim_mst_specs(
                    hwy_nodes,
                    prefix=f"{b_name}_hwy_mst_",
                    commodity=comm,
                    fixed_bbox=None,
                    bounds=state.bounds,
                    margin=state.margin,
                )
            )

    def _partition_fluid_terminals(
        self,
        f_list: list[RouterNetSpec],
        state: _HierarchicalPlanState,
    ) -> tuple[
        list[tuple[tuple[int, int], int | None, int | None]],
        dict[tuple[int, int], list[tuple[tuple[int, int], int | None, int | None]]],
    ]:
        """Partition unique fluid terminals into street pin terminals and block core terminals."""
        term_map: dict[tuple[int, int], tuple[tuple[int, int], int | None, int | None]] = {}
        for fs in f_list:
            if fs.sink not in term_map:
                term_map[fs.sink] = (fs.sink, fs.end_heading, fs.sink_inward_dir)
            if fs.source not in term_map:
                term_map[fs.source] = (fs.source, fs.start_heading, fs.source_inward_dir)

        pin_terms: list[tuple[tuple[int, int], int | None, int | None]] = []
        blk_terms: dict[tuple[int, int], list[tuple[tuple[int, int], int | None, int | None]]] = {}
        for t_pos, t_info in term_map.items():
            blk_id = self._block_of_coord(
                t_pos, state.grid_w, state.grid_h, state.bay_blocks, margin=state.margin
            )
            if blk_id is None:
                pin_terms.append(t_info)
            else:
                blk_terms.setdefault(blk_id, []).append(t_info)
        return pin_terms, blk_terms

    def _emit_local_or_pin_only_fluid_specs(
        self,
        f_list: list[RouterNetSpec],
        blk_terms: dict[tuple[int, int], list[tuple[tuple[int, int], int | None, int | None]]],
        state: _HierarchicalPlanState,
    ) -> None:
        """Emit intra-block or highway fluid specs when no multi-block tree is needed."""
        RouterNetSpecCls = _load_router_net_spec_cls()
        only_blk = next(iter(blk_terms.keys()), None)
        if only_blk is not None:
            bbox = self._block_core_bbox(only_blk, state.bay_blocks, margin=state.margin)
            for fs in f_list:
                state.intra_block_specs[only_blk].append(
                    RouterNetSpecCls(
                        name=fs.name,
                        source=fs.source,
                        sink=fs.sink,
                        commodity=fs.commodity,
                        lane=fs.lane,
                        is_fluid=True,
                        start_heading=fs.start_heading,
                        end_heading=fs.end_heading,
                        terminal_heading=fs.terminal_heading,
                        source_inward_dir=fs.source_inward_dir,
                        sink_inward_dir=fs.sink_inward_dir,
                        corridor_bbox=bbox,
                    )
                )
            return

        min_x, min_y, max_x, max_y = state.bounds
        for fs in f_list:
            state.highway_specs.append(
                RouterNetSpecCls(
                    name=f"{fs.name}_hwy",
                    source=fs.source,
                    sink=fs.sink,
                    commodity=fs.commodity,
                    lane=fs.lane,
                    is_fluid=True,
                    start_heading=fs.start_heading,
                    end_heading=fs.end_heading,
                    terminal_heading=fs.terminal_heading,
                    source_inward_dir=fs.source_inward_dir,
                    sink_inward_dir=fs.sink_inward_dir,
                    corridor_bbox=self._highway_corridor_bbox(
                        fs.source, fs.sink, min_x, min_y, max_x, max_y, margin=state.margin
                    ),
                )
            )

    def _allocate_fluid_block_port(
        self,
        b_name: str,
        blk: tuple[int, int],
        t_list: list[tuple[tuple[int, int], int | None, int | None]],
        other_pts: list[tuple[int, int]],
        f_forbid: set[str],
        is_import: bool,
        single_blk_with_pins: bool,
        state: _HierarchicalPlanState,
    ) -> tuple[BlockPort, tuple[int, int], tuple[int, int]]:
        """Select representative fluid terminal and allocate a pipe BlockPort on blk."""
        target_pt = _fluid_target_point(blk, other_pts)
        rep_t, pref_c = _select_fluid_rep_terminal(t_list, target_pt, single_blk_with_pins)
        return self._allocate_block_port(
            blk=blk,
            net_name=b_name,
            port_type="import" if is_import else "export",
            internal_pt=rep_t[0],
            target_pt=target_pt,
            obstacles=state.obstacles,
            used_edge_coords=state.used_edge_coords,
            pin_reserved_coords=state.pin_reserved_coords,
            all_terminals=state.all_terminals,
            forbidden_edges=f_forbid,
            preferred_c=pref_c,
            has_north_bay=(blk in state.bay_blocks),
            has_south_bay_neighbor=((blk[0], blk[1] + 1) in state.bay_blocks),
            tier="pipe",
            grid_w=state.grid_w,
            grid_h=state.grid_h,
            margin=state.margin,
        )

    def _record_channel_route(
        self,
        floorplan: MacroFloorplan,
        net_name: str,
        src_blk: tuple[int, int] | None,
        dst_blk: tuple[int, int] | None,
        is_fluid: bool,
        src_port: BlockPort | None = None,
        dst_port: BlockPort | None = None,
    ) -> None:
        """Record an InterBlockRoute and allocate channel segments with overflow tolerance."""
        tier = "pipe" if is_fluid else self.default_belt_tier
        t_blks = [dst_blk] if dst_blk is not None else []
        route = InterBlockRoute(
            net_name=net_name,
            source_block=src_blk,
            target_blocks=t_blks,
            is_fluid=is_fluid,
            tier=tier,
            source_port=src_port,
            target_ports=[dst_port] if dst_port is not None else [],
        )
        if src_blk is not None and dst_blk is not None and src_blk != dst_blk:
            _allocate_L_route_segments(floorplan, route, src_blk, dst_blk, tier, tolerate_full=True)
        floorplan.routes[net_name] = route


@dataclass(slots=True)
class _HierarchicalPlanState:
    """Mutable working state for hierarchical net decomposition."""

    floorplan: MacroFloorplan
    grid_w: int
    grid_h: int
    obstacles: set[tuple[int, int]]
    bounds: tuple[int, int, int, int]
    bay_blocks: set[tuple[int, int]]
    margin: int
    intra_block_specs: dict[tuple[int, int], list[RouterNetSpec]]
    highway_specs: list[RouterNetSpec] = field(default_factory=list)
    port_no_hood_tiles: set[tuple[int, int]] = field(default_factory=set)
    port_headings: dict[tuple[int, int], int] = field(default_factory=dict)
    used_edge_coords: dict[tuple[int, int, str], list[tuple[int, str]]] = field(
        default_factory=dict
    )
    pin_reserved_coords: dict[tuple[int, int, str], list[tuple[int, tuple[int, int]]]] = field(
        default_factory=dict
    )
    all_terminals: set[tuple[int, int]] = field(default_factory=set)
    v_seg_transit_load: dict[tuple[int, int], float] = field(default_factory=dict)

    @classmethod
    def initialize(
        cls,
        planner: MacroFloorplanner,
        floorplan: MacroFloorplan,
        net_specs: list[RouterNetSpec],
        grid_w: int,
        grid_h: int,
        obstacles: set[tuple[int, int]],
        bounds: tuple[int, int, int, int],
        bay_blocks: set[tuple[int, int]],
        margin: int,
    ) -> _HierarchicalPlanState:
        """Create initial planning state and index boundary pin reservations."""
        intra_block_specs: dict[tuple[int, int], list[RouterNetSpec]] = {
            (bx, by): [] for bx in range(grid_w) for by in range(grid_h)
        }
        state = cls(
            floorplan=floorplan,
            grid_w=grid_w,
            grid_h=grid_h,
            obstacles=obstacles,
            bounds=bounds,
            bay_blocks=bay_blocks,
            margin=margin,
            intra_block_specs=intra_block_specs,
        )
        m_chan = margin - 1
        for spec in net_specs:
            state.all_terminals.add(spec.source)
            state.all_terminals.add(spec.sink)
            for pt in (spec.source, spec.sink):
                if planner._block_of_coord(pt, grid_w, grid_h, bay_blocks, margin=margin) is None:
                    _reserve_boundary_pin(
                        pt, grid_w, grid_h, margin, m_chan, state.pin_reserved_coords
                    )
        return state

    def register_solid_port(
        self, port: BlockPort, core_pos: tuple[int, int], chan_pos: tuple[int, int]
    ) -> None:
        """Record a solid belt port and its boundary terminal constraints."""
        self.floorplan.ports.append(port)
        self.port_no_hood_tiles.add(core_pos)
        self.port_headings[core_pos] = port.direction
        self.port_headings[chan_pos] = port.direction
        self.all_terminals.add(core_pos)
        self.all_terminals.add(chan_pos)

    def register_fluid_port(
        self, port: BlockPort, core_pos: tuple[int, int], chan_pos: tuple[int, int]
    ) -> None:
        """Record a fluid pipe port and its boundary terminal constraints."""
        self.floorplan.ports.append(port)
        self.port_no_hood_tiles.add(core_pos)
        self.port_no_hood_tiles.add(chan_pos)
        self.all_terminals.add(core_pos)
        self.all_terminals.add(chan_pos)


def _load_router_net_spec_cls() -> type[RouterNetSpec]:
    """Import and return RouterNetSpec class."""
    try:
        from foundry.router.types import RouterNetSpec
    except ImportError:
        from router.types import RouterNetSpec
    return RouterNetSpec


def _detect_bay_blocks(
    net_specs: list[RouterNetSpec], grid_w: int, grid_h: int
) -> set[tuple[int, int]]:
    """Detect blocks containing matrix switchbox bay terminals in their north strip."""
    bays: set[tuple[int, int]] = set()
    for spec in net_specs:
        for pt in (spec.source, spec.sink):
            bx_p = pt[0] // DEFAULT_CHASSIS_PITCH
            by_p = pt[1] // DEFAULT_CHASSIS_PITCH
            if (
                0 <= bx_p < grid_w
                and 0 <= by_p < grid_h
                and BAY_DETECT_X_MIN <= (pt[0] % DEFAULT_CHASSIS_PITCH) <= BAY_DETECT_X_MAX
                and BAY_DETECT_Y_MIN <= (pt[1] % DEFAULT_CHASSIS_PITCH) <= BAY_DETECT_Y_MAX
            ):
                bays.add((bx_p, by_p))
    return bays


def _reserve_boundary_pin(
    pt: tuple[int, int],
    grid_w: int,
    grid_h: int,
    margin: int,
    m_chan: int,
    pin_reserved_coords: dict[tuple[int, int, str], list[tuple[int, tuple[int, int]]]],
) -> None:
    """Reserve boundary edge coordinates near external perimeter pins."""
    px, py = pt
    by_pin = max(0, min(grid_h - 1, py // DEFAULT_CHASSIS_PITCH))
    bx_pin = max(0, min(grid_w - 1, px // DEFAULT_CHASSIS_PITCH))
    if px <= m_chan:
        pin_reserved_coords.setdefault((0, by_pin, "W"), []).append((py, pt))
    elif px >= grid_w * DEFAULT_CHASSIS_PITCH - margin:
        pin_reserved_coords.setdefault((grid_w - 1, by_pin, "E"), []).append((py, pt))
    if py <= m_chan:
        pin_reserved_coords.setdefault((bx_pin, 0, "N"), []).append((px, pt))
    elif py >= grid_h * DEFAULT_CHASSIS_PITCH - margin:
        pin_reserved_coords.setdefault((bx_pin, grid_h - 1, "S"), []).append((px, pt))


def _record_v_transit_pair(
    p1: tuple[int, int], p2: tuple[int, int], state: _HierarchicalPlanState
) -> None:
    """Increment vertical transit load for rows spanned between p1 and p2."""
    sx, sy = p1
    tx, ty = p2
    y_lo, y_hi = min(sy, ty), max(sy, ty)
    best_vx = min(
        range(1, state.grid_w),
        key=lambda vx: abs(sx - vx * DEFAULT_CHASSIS_PITCH) + abs(tx - vx * DEFAULT_CHASSIS_PITCH),
    )
    for r in range(state.grid_h):
        r_base = r * DEFAULT_CHASSIS_PITCH
        if y_lo <= r_base + BAY_STREET_Y_MAX and y_hi >= r_base + (
            DEFAULT_CHASSIS_PITCH - state.margin
        ):
            state.v_seg_transit_load[(best_vx, r)] = (
                state.v_seg_transit_load.get((best_vx, r), 0.0) + 1.0
            )


def _edges_matching_segment(blk: tuple[int, int], seg: tuple[str, int, int] | None) -> set[str]:
    """Return cardinal edges of blk whose street segment matches seg."""
    if seg is None:
        return set()
    return {
        e
        for e in ALL_CARDINAL_EDGES
        if MacroFloorplanner._street_segment_of_edge(blk[0], blk[1], e) == seg
    }


def _precompute_other_fluid_pts(
    pin_terms: list[tuple[tuple[int, int], int | None, int | None]],
    blk_terms: dict[tuple[int, int], list[tuple[tuple[int, int], int | None, int | None]]],
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """Precompute terminal coordinates outside each fluid block."""
    pin_pts = [p[0] for p in pin_terms]
    result: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for blk in blk_terms:
        other = list(pin_pts)
        for ob, ol in blk_terms.items():
            if ob != blk:
                other.extend(p[0] for p in ol)
        result[blk] = other
    return result


def _fluid_target_point(blk: tuple[int, int], other_pts: list[tuple[int, int]]) -> tuple[int, int]:
    """Compute target reference point for fluid port placement on blk."""
    if not other_pts:
        return (
            blk[0] * DEFAULT_CHASSIS_PITCH + BLOCK_CENTER_LOCAL_COORD,
            blk[1] * DEFAULT_CHASSIS_PITCH + BLOCK_CENTER_LOCAL_COORD,
        )
    avg_tx = round(sum(p[0] for p in other_pts) / len(other_pts))
    avg_ty = round(sum(p[1] for p in other_pts) / len(other_pts))
    return min(other_pts, key=lambda p: abs(p[0] - avg_tx) + abs(p[1] - avg_ty))


def _select_fluid_rep_terminal(
    t_list: list[tuple[tuple[int, int], int | None, int | None]],
    target_pt: tuple[int, int],
    single_blk_with_pins: bool,
) -> tuple[tuple[tuple[int, int], int | None, int | None], int | None]:
    """Select representative terminal and optional preferred coordinate for a fluid block."""
    if single_blk_with_pins:
        cx_t = round(sum(t_item[0][0] for t_item in t_list) / len(t_list))
        cy_t = round(sum(t_item[0][1] for t_item in t_list) / len(t_list))
        rep_t = min(
            t_list,
            key=lambda t_item: abs(t_item[0][0] - cx_t) + abs(t_item[0][1] - cy_t),
        )
        return rep_t, cy_t
    rep_t = min(
        t_list,
        key=lambda t_item: abs(t_item[0][0] - target_pt[0]) + abs(t_item[0][1] - target_pt[1]),
    )
    return rep_t, None


@dataclass(slots=True)
class _PrimFrontierEntry:
    """Mutable frontier record for Prim's minimum spanning tree construction."""

    node: tuple[tuple[int, int], int | None, int | None]
    dist: int
    parent: tuple[tuple[int, int], int | None, int | None]


def _build_prim_mst_specs(
    nodes: list[tuple[tuple[int, int], int | None, int | None]],
    prefix: str,
    commodity: str,
    fixed_bbox: tuple[int, int, int, int] | None,
    bounds: tuple[int, int, int, int],
    margin: int,
) -> list[RouterNetSpec]:
    """Construct Prim's minimum spanning tree RouterNetSpecs over fluid terminal nodes."""
    RouterNetSpecCls = _load_router_net_spec_cls()
    min_x, min_y, max_x, max_y = bounds
    root = nodes[0]
    nearest: list[_PrimFrontierEntry] = [
        _PrimFrontierEntry(v, abs(root[0][0] - v[0][0]) + abs(root[0][1] - v[0][1]), root)
        for v in nodes[1:]
    ]
    specs: list[RouterNetSpec] = []
    edge_idx = 0

    while nearest:
        best_i = min(range(len(nearest)), key=lambda i: nearest[i].dist)
        chosen = nearest.pop(best_i)
        v = chosen.node
        u = chosen.parent
        _update_prim_frontier(nearest, v)
        bbox = (
            fixed_bbox
            if fixed_bbox is not None
            else MacroFloorplanner._highway_corridor_bbox(
                v[0], u[0], min_x, min_y, max_x, max_y, margin=margin
            )
        )
        specs.append(
            RouterNetSpecCls(
                name=f"{prefix}{edge_idx}",
                source=v[0],
                sink=u[0],
                commodity=commodity,
                is_fluid=True,
                start_heading=v[1],
                end_heading=None,
                terminal_heading=None,
                source_inward_dir=v[2],
                sink_inward_dir=u[2],
                corridor_bbox=bbox,
            )
        )
        edge_idx += 1
    return specs


def _update_prim_frontier(
    nearest: list[_PrimFrontierEntry],
    new_node: tuple[tuple[int, int], int | None, int | None],
) -> None:
    """Update shortest distances from remaining nodes to the newly added tree node."""
    nx, ny = new_node[0]
    for idx in range(len(nearest)):
        entry = nearest[idx]
        w = entry.node
        d = abs(nx - w[0][0]) + abs(ny - w[0][1])
        if d < entry.dist:
            entry.dist = d
            entry.parent = new_node


def _rank_candidate_edges(
    ctx: _EdgeScoreContext,
    forbidden_edges: set[str] | None,
    has_north_bay: bool,
    preferred_edge: str | None,
) -> list[str]:
    """Return candidate block boundary edges ordered by score and preference."""
    forbid = set(forbidden_edges) if forbidden_edges else set()
    if has_north_bay:
        forbid.add("N")
    allowed = [e for e in ALL_CARDINAL_EDGES if e not in forbid]
    if not allowed:
        allowed = [e for e in ALL_CARDINAL_EDGES if not (has_north_bay and e == "N")] or list(
            ALL_CARDINAL_EDGES
        )

    sorted_by_score = sorted(allowed, key=lambda e: _score_candidate_edge(ctx, e))
    if preferred_edge is not None and preferred_edge in allowed:
        return [preferred_edge] + [e for e in sorted_by_score if e != preferred_edge]
    return sorted_by_score


def _score_candidate_edge(ctx: _EdgeScoreContext, edge: str) -> float:
    """Compute total placement score for a block boundary edge."""
    px, py, opp_e_key = _projected_edge_point(ctx, edge)
    in_cost = _edge_internal_cost(ctx, edge, px, py)
    out_cost = _edge_external_cost(ctx, edge, px, py)
    load_pen = _edge_load_penalty(ctx, edge, opp_e_key)
    rim_pen = _edge_rim_penalty(ctx, edge)
    return in_cost + out_cost + load_pen + rim_pen


def _projected_edge_point(
    ctx: _EdgeScoreContext, edge: str
) -> tuple[int, int, tuple[int, int, str]]:
    """Return projected (px, py) on edge and the opposite block edge key."""
    bx, by = ctx.blk
    ix, iy = ctx.internal_pt
    x_base = bx * DEFAULT_CHASSIS_PITCH
    y_base = by * DEFAULT_CHASSIS_PITCH
    if edge == "N":
        return (
            max(x_base + ctx.c_lo_base, min(x_base + ctx.c_hi_off, ix)),
            y_base + ctx.m_chan,
            (bx, by - 1, "S"),
        )
    if edge == "S":
        return (
            max(x_base + ctx.c_lo_base, min(x_base + ctx.c_hi_off, ix)),
            y_base + ctx.e_chan,
            (bx, by + 1, "N"),
        )
    if edge == "W":
        return (
            x_base + ctx.m_chan,
            max(y_base + ctx.y_min_off, min(y_base + ctx.c_hi_off, iy)),
            (bx - 1, by, "E"),
        )
    return (
        x_base + ctx.e_chan,
        max(y_base + ctx.y_min_off, min(y_base + ctx.c_hi_off, iy)),
        (bx + 1, by, "W"),
    )


def _edge_internal_cost(ctx: _EdgeScoreContext, edge: str, px: int, py: int) -> float:
    """Compute internal core Manhattan cost to reach (px, py)."""
    ix, iy = ctx.internal_pt
    in_cost = IN_COST_X_WEIGHT * float(abs(ix - px)) + IN_COST_Y_WEIGHT * float(abs(iy - py))
    if (ctx.port_type == "import" and edge == "W") or (ctx.port_type == "export" and edge == "E"):
        in_cost -= PREFERRED_FLOW_EDGE_BONUS
    return in_cost


def _edge_external_cost(ctx: _EdgeScoreContext, edge: str, px: int, py: int) -> float:
    """Compute street channel distance cost from (px, py) to target_pt."""
    bx, by = ctx.blk
    tx, ty = ctx.target_pt
    x_base = bx * DEFAULT_CHASSIS_PITCH
    y_base = by * DEFAULT_CHASSIS_PITCH
    if edge in ("W", "E") and abs(px - tx) > ctx.channel_width:
        vert_d = min(
            abs(py - (y_base + ctx.m_chan)) + abs((y_base + ctx.m_chan) - ty),
            abs(py - (y_base + ctx.e_chan)) + abs((y_base + ctx.e_chan) - ty),
        )
        return float(abs(px - tx) + vert_d)
    if edge in ("N", "S") and abs(py - ty) > ctx.channel_width:
        horiz_d = min(
            abs(px - (x_base + ctx.m_chan)) + abs((x_base + ctx.m_chan) - tx),
            abs(px - (x_base + ctx.e_chan)) + abs((x_base + ctx.e_chan) - tx),
        )
        return float(horiz_d + abs(py - ty))
    return float(abs(px - tx) + abs(py - ty))


def _edge_load_penalty(ctx: _EdgeScoreContext, edge: str, opp_e_key: tuple[int, int, str]) -> float:
    """Compute congestion penalty for an edge and its opposite wall."""
    bx, by = ctx.blk
    _, ty = ctx.target_pt
    here_cnt = len(ctx.used_edge_coords.get((bx, by, edge), []))
    opp_cnt = len(ctx.used_edge_coords.get(opp_e_key, []))
    transit_cnt = _edge_vertical_transit_excess(ctx, edge)
    seg_cnt = here_cnt + opp_cnt + round(transit_cnt)

    cap_pen = 0.0
    if seg_cnt == ctx.cap_tracks:
        cap_pen = CAP_TRACK_BASE_PENALTY
    elif seg_cnt > ctx.cap_tracks:
        cap_pen = CAP_TRACK_BASE_PENALTY + CAP_TRACK_OVERFLOW_WEIGHT * float(
            seg_cnt - ctx.cap_tracks
        )

    load_pen = (
        HERE_USAGE_WEIGHT * float(here_cnt)
        + OPP_USAGE_WEIGHT * float(opp_cnt)
        + TRANSIT_USAGE_WEIGHT * float(transit_cnt)
        + cap_pen
    )
    if edge == "S" and ctx.has_south_bay_neighbor:
        load_pen += SOUTH_BAY_NEIGHBOR_PENALTY
    if (
        edge == "S"
        and by * DEFAULT_CHASSIS_PITCH <= ty <= by * DEFAULT_CHASSIS_PITCH + BAY_STREET_Y_MAX
    ):
        load_pen += SOUTH_BAY_TARGET_PENALTY
    return load_pen


def _edge_vertical_transit_excess(ctx: _EdgeScoreContext, edge: str) -> float:
    """Return excess vertical transit load on edge relative to the opposite vertical wall."""
    bx, by = ctx.blk
    if edge not in ("W", "E") or ctx.v_seg_transit_load is None or not (1 <= bx <= ctx.grid_w - 2):
        return 0.0
    vx_e = bx if edge == "W" else bx + 1
    vx_opp = bx + 1 if edge == "W" else bx
    excess = ctx.v_seg_transit_load.get((vx_e, by), 0.0) - ctx.v_seg_transit_load.get(
        (vx_opp, by), 0.0
    )
    return excess if excess >= EXCESS_TRANSIT_THRESHOLD else 0.0


def _edge_rim_penalty(ctx: _EdgeScoreContext, edge: str) -> float:
    """Compute outer grid rim penalty or bonus for a boundary edge."""
    if ctx.grid_w * ctx.grid_h <= 1:
        return 0.0
    bx, by = ctx.blk
    if edge == "W" and bx == 0:
        return _west_rim_penalty(ctx)
    if edge == "E" and bx == ctx.grid_w - 1:
        return _east_rim_penalty(ctx)
    if edge == "N" and by == 0:
        return _north_south_rim_penalty(ctx, is_north=True)
    if edge == "S" and by == ctx.grid_h - 1:
        return _north_south_rim_penalty(ctx, is_north=False)
    return 0.0


def _west_rim_penalty(ctx: _EdgeScoreContext) -> float:
    """Compute rim penalty or bonus for the westernmost column W wall."""
    _, by = ctx.blk
    tx, ty = ctx.target_pt
    y_min = max(0, by - 1) * DEFAULT_CHASSIS_PITCH
    y_max = (by + 1) * DEFAULT_CHASSIS_PITCH + BLOCK_MAX_LOCAL_COORD
    if tx <= ctx.m_chan and y_min <= ty <= y_max:
        return RIM_PIN_BONUS
    return RIM_EAST_W_PENALTY


def _north_south_rim_penalty(ctx: _EdgeScoreContext, is_north: bool) -> float:
    """Compute rim penalty or bonus for the northernmost N or southernmost S wall."""
    bx, _ = ctx.blk
    tx, ty = ctx.target_pt
    x_base = bx * DEFAULT_CHASSIS_PITCH
    if not (x_base <= tx <= x_base + BLOCK_MAX_LOCAL_COORD):
        return RIM_NORTH_S_PENALTY
    on_rim_pin = (
        ty <= ctx.m_chan if is_north else ty >= ctx.grid_h * DEFAULT_CHASSIS_PITCH - ctx.margin
    )
    return RIM_PIN_BONUS if on_rim_pin else RIM_NORTH_S_PENALTY


def _east_rim_penalty(ctx: _EdgeScoreContext) -> float:
    """Compute rim penalty or bonus specifically for the easternmost column E wall."""
    bx, by = ctx.blk
    ix, iy = ctx.internal_pt
    tx, ty = ctx.target_pt
    y_base = by * DEFAULT_CHASSIS_PITCH
    if (
        tx >= ctx.grid_w * DEFAULT_CHASSIS_PITCH - ctx.margin
        and y_base <= ty <= y_base + BLOCK_MAX_LOCAL_COORD
    ):
        return RIM_PIN_BONUS

    used_e = ctx.used_edge_coords.get((bx, by, "E"), [])
    if (
        ctx.port_type == "export"
        and not used_e
        and by == 0
        and (ix % DEFAULT_CHASSIS_PITCH) >= RIM_EAST_EXPORT_IX_MIN
        and (iy % DEFAULT_CHASSIS_PITCH) <= RIM_EAST_EXPORT_IY_MAX
    ):
        return RIM_EAST_EXPORT_PENALTY

    if _matches_east_bay_shortcut(by, iy, tx, ty, used_e):
        return RIM_EAST_BAY_BONUS
    return RIM_EAST_W_PENALTY


def _matches_east_bay_shortcut(
    by: int,
    iy: int,
    tx: int,
    ty: int,
    used_e: list[tuple[int, str]],
) -> bool:
    """Return True when East rim shortcut conditions to a north or south bay are met."""
    if (tx % DEFAULT_CHASSIS_PITCH) < RIM_EAST_BAY_TX_MIN:
        return False
    y_base = by * DEFAULT_CHASSIS_PITCH
    iy_mod = iy % DEFAULT_CHASSIS_PITCH
    mid_y = y_base + RIM_EAST_BAY_IY_MID

    same_row_bay = (
        y_base <= ty <= y_base + BAY_STREET_Y_MAX
        and iy_mod <= RIM_EAST_BAY_IY_MID
        and all(uc > mid_y for uc, _ in used_e)
    )
    next_row_base = (by + 1) * DEFAULT_CHASSIS_PITCH
    next_row_bay = (
        next_row_base <= ty <= next_row_base + BAY_STREET_Y_MAX
        and RIM_EAST_BAY_IY_MID < iy_mod <= RIM_EAST_EXPORT_IY_MAX
        and all(uc <= mid_y for uc, _ in used_e)
    )
    return same_row_bay or next_row_bay


def _ideal_edge_coord(
    ctx: _EdgeScoreContext, edge: str, preferred_c: int | None
) -> tuple[int, int, int]:
    """Return (clamped_ideal_c, lo_c, hi_c) coordinate bounds along edge."""
    bx, by = ctx.blk
    ix, iy = ctx.internal_pt
    tx, ty = ctx.target_pt
    base = by * DEFAULT_CHASSIS_PITCH if edge in ("W", "E") else bx * DEFAULT_CHASSIS_PITCH
    c_lo_off = ctx.y_min_off if edge in ("W", "E") else ctx.c_lo_base
    lo_c = base + c_lo_off
    hi_c = base + ctx.c_hi_off

    if preferred_c is not None and lo_c <= preferred_c <= hi_c:
        ideal_c = preferred_c
    elif edge in ("W", "E"):
        ideal_c = (
            ty
            if (lo_c <= ty <= hi_c and _is_target_adjacent_to_vertical_edge(ctx, edge, tx))
            else iy
        )
    else:
        ideal_c = ix
    return max(lo_c, min(hi_c, ideal_c)), lo_c, hi_c


def _is_target_adjacent_to_vertical_edge(ctx: _EdgeScoreContext, edge: str, tx: int) -> bool:
    """Return True when tx lies within the street channel immediately adjacent to W or E."""
    x_base = ctx.blk[0] * DEFAULT_CHASSIS_PITCH
    if edge == "W":
        return tx <= x_base + ctx.m_chan and x_base - tx <= ctx.channel_width
    return tx >= x_base + ctx.e_chan and tx - (x_base + BLOCK_MAX_LOCAL_COORD) <= ctx.channel_width


def _effective_port_spacing(spacing: int, tier: str, bx: int, grid_w: int) -> int:
    """Return effective minimum port coordinate spacing along an edge."""
    if (
        spacing == PRIMARY_PORT_SPACING
        and tier != "pipe"
        and bx == grid_w - 1
        and grid_w >= WIDE_GRID_THRESHOLD
    ):
        return WIDE_GRID_EAST_SPACING
    return spacing


def _precompute_forbidden_edge_coords(
    eff_spacing: int,
    used_here: list[tuple[int, str]],
    pin_here: list[tuple[int, tuple[int, int]]],
    internal_pt: tuple[int, int],
    target_pt: tuple[int, int],
) -> set[int]:
    """Build a set of forbidden edge coordinates around used ports and reserved pins."""
    forbidden: set[int] = set()
    for uc, _ in used_here:
        forbidden.update(range(uc - eff_spacing + 1, uc + eff_spacing))
    for pc, p_pos in pin_here:
        if p_pos not in (internal_pt, target_pt):
            forbidden.update(range(pc - PIN_RESERVE_CLEARANCE + 1, pc + PIN_RESERVE_CLEARANCE))
    return forbidden


def _build_allocated_block_port(
    ctx: _EdgeScoreContext,
    edge: str,
    c_val: int,
    net_name: str,
    tier: str,
    core_pos: tuple[int, int],
    channel_pos: tuple[int, int],
    port_dir: int,
) -> tuple[BlockPort, tuple[int, int], tuple[int, int]]:
    """Construct a BlockPort and return (port, core_pos, channel_pos)."""
    bx, by = ctx.blk
    ch_id = (
        f"v_channel_{max(0, bx - 1 if edge == 'W' else bx)}_{by}"
        if edge in ("W", "E")
        else f"h_channel_{bx}_{max(0, by - 1 if edge == 'N' else by)}"
    )
    port = BlockPort(
        name=f"{net_name}_{ctx.port_type}_{bx}_{by}_{edge}_{c_val}",
        block=ctx.blk,
        net_name=net_name,
        port_type=ctx.port_type,
        edge=edge,
        position=(float(channel_pos[0]), float(channel_pos[1])),
        channel_id=ch_id,
        lane_index=0,
        direction=port_dir,
        tier=tier,
        internal_pos=(float(core_pos[0]), float(core_pos[1])),
    )
    return port, core_pos, channel_pos


def _is_port_geometry_clear(
    c_val: int,
    spacing: int,
    net_name: str,
    geom_pts: tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]],
    used_opp: list[tuple[int, str]],
    obstacles: set[tuple[int, int]],
    all_terminals: set[tuple[int, int]],
    endpoint_set: set[tuple[int, int]],
) -> bool:
    """Return True when opposite edge ports, obstacles, and terminals do not block geom_pts."""
    if any(abs(c_val - uc) < spacing and unet != net_name for uc, unet in used_opp):
        return False
    if any(pt in obstacles for pt in geom_pts):
        return False
    return not any(pt in all_terminals and pt not in endpoint_set for pt in geom_pts)
