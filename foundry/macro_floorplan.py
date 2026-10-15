"""Level 1 macro floorplanner for recipe clustering and channel routing.

Partitions netlist instances into recipe clusters, allocates clusters to
discrete city block cores on the chassis grid, handles multi-block allocation
for oversized stages, and routes inter-block nets through street channel lanes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from foundry.channel_router import RouterIntersection, StreetChannel
from foundry.chassis import CityBlockChassis
from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    entity_size,
)


@dataclass
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


@dataclass
class ChannelRouteSegment:
    """A channel segment traversed by an inter-block net."""

    channel_id: str
    lane_index: int
    orientation: str
    direction: int


@dataclass
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


@dataclass
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

    def emit_def_blockages(self, dbu: int = 1000) -> str:
        """Emit DEF BLOCKAGES section covering all street channels."""
        return self.chassis.emit_def_blockages(
            self.grid_w, self.grid_h, dbu=dbu, empty_blocks=self.empty_blocks
        )

    def emit_channel_entities(self) -> list[dict[str, Any]]:
        """Emit all physical belts, pipes, and router switchboxes for inter-block routes."""
        entities: list[dict[str, Any]] = []
        occupied_tiles: set[tuple[float, float]] = set()

        # Emit intersection switchbox entities first (crossings, undergrounds, turns, splitters)
        for intersection in self.intersections.values():
            for ent in intersection.emit_intersection_entities():
                pos = ent.get("position", {})
                key = (
                    round(float(pos.get("x", 0.0)), 2),
                    round(float(pos.get("y", 0.0)), 2),
                )
                if key not in occupied_tiles:
                    occupied_tiles.add(key)
                    entities.append(ent)

        # Emit straight belts and pipes for all allocated channel lanes where not blocked by intersection entities
        for channel in self.channels.values():
            for ent in channel.emit_straight_belts():
                pos = ent.get("position", {})
                key = (
                    round(float(pos.get("x", 0.0)), 2),
                    round(float(pos.get("y", 0.0)), 2),
                )
                if key not in occupied_tiles:
                    occupied_tiles.add(key)
                    entities.append(ent)

        return entities

    def emit_port_connections(self, placement: Any = None) -> list[dict[str, Any]]:
        """Emit physical belts connecting block interiors to the street channel fabric."""
        entities: list[dict[str, Any]] = []
        if placement is None:
            return entities

        inst_to_ent = getattr(placement, "inst_to_entity", {})
        if not inst_to_ent:
            return entities

        occupied_indices: dict[tuple[float, float], int] = {}

        def add_belt(
            name: str, x: float, y: float, direction: int, btype: str | None = None
        ) -> None:
            key = (round(float(x), 2), round(float(y), 2))
            ent: dict[str, Any] = {
                "name": name,
                "position": {"x": key[0], "y": key[1]},
                "direction": direction,
            }
            if btype:
                ent["type"] = btype
            if key in occupied_indices:
                entities[occupied_indices[key]] = ent
            else:
                occupied_indices[key] = len(entities)
                entities.append(ent)

        def make_path(
            waypoints: list[tuple[float, float]],
            end_dir: int = DIRECTION_EAST,
            tier: str = "fast-transport-belt",
        ) -> None:
            tiles: list[tuple[float, float]] = []
            for i in range(len(waypoints) - 1):
                x1, y1 = waypoints[i]
                x2, y2 = waypoints[i + 1]
                if abs(y1 - y2) < 0.01:
                    dx = 1.0 if x2 > x1 else -1.0
                    cx = x1
                    while cx <= x2 if dx > 0 else cx >= x2:
                        tiles.append((round(cx, 2), round(y1, 2)))
                        cx += dx
                elif abs(x1 - x2) < 0.01:
                    dy = 1.0 if y2 > y1 else -1.0
                    cy = y1
                    while cy <= y2 if dy > 0 else cy >= y2:
                        tiles.append((round(x1, 2), round(cy, 2)))
                        cy += dy
            dedup: list[tuple[float, float]] = []
            for t in tiles:
                if not dedup or dedup[-1] != t:
                    dedup.append(t)
            for i in range(len(dedup)):
                curr = dedup[i]
                if i < len(dedup) - 1:
                    nxt = dedup[i + 1]
                    dx = nxt[0] - curr[0]
                    dy = nxt[1] - curr[1]
                    if dx > 0.5:
                        d = DIRECTION_EAST
                    elif dx < -0.5:
                        d = DIRECTION_WEST
                    elif dy > 0.5:
                        d = DIRECTION_SOUTH
                    elif dy < -0.5:
                        d = DIRECTION_NORTH
                    else:
                        d = end_dir
                else:
                    d = end_dir
                add_belt(tier, curr[0], curr[1], d)

        for route_idx, route in enumerate(self.routes.values()):
            if route.source_block is None or not route.target_blocks:
                continue
            src_inst = route.source_instance
            if not src_inst or src_inst not in inst_to_ent:
                continue

            src_ent = inst_to_ent[src_inst]
            src_pos = src_ent.get("position", {})
            sx = float(src_pos.get("x", 0.0))
            sy = float(src_pos.get("y", 0.0))
            tier = route.tier

            if src_ent.get("name") == "splitter":
                out_x = sx + 1.0
                out_y = sy - 0.5
            else:
                out_x = sx + 3.0
                out_y = sy

            lane_idx = route_idx % 4
            lane_off = lane_idx * 1.0

            for tgt_block in route.target_blocks:
                tgt_insts = [
                    ti
                    for ti in route.target_instances
                    if self.instance_to_block.get(ti) == tgt_block
                ]
                if not tgt_insts:
                    continue

                tgt_inst = tgt_insts[0]
                if tgt_inst not in inst_to_ent:
                    continue

                tgt_ent = inst_to_ent[tgt_inst]
                tpos = tgt_ent.get("position", {})
                tx = float(tpos.get("x", 0.0))
                ty = float(tpos.get("y", 0.0))

                if tgt_ent.get("name") == "splitter":
                    in_x = tx - 1.0
                    in_y = ty - 0.5
                else:
                    in_x = tx - 3.0
                    in_y = ty

                src_blk = route.source_block

                # Case 1: Adjacent East on same row through vertical channel
                if src_blk[0] + 1 == tgt_block[0] and src_blk[1] == tgt_block[1]:
                    v_lane_x = (
                        src_blk[0] * self.chassis.pitch_x
                        + self.chassis.core_margin_x
                        + self.chassis.core_width
                        + 0.5
                        + lane_off
                    )
                    waypoints = [
                        (out_x, out_y),
                        (v_lane_x, out_y),
                        (v_lane_x, in_y),
                        (in_x, in_y),
                    ]
                    make_path(waypoints, end_dir=DIRECTION_EAST, tier=tier)

                # Case 2: Adjacent South on same column through vertical corridor and crossroad intersection
                elif src_blk[0] == tgt_block[0] and src_blk[1] + 1 == tgt_block[1]:
                    ix = src_blk[0]
                    iy = tgt_block[1]
                    center_x = ix * self.chassis.pitch_x
                    center_y = iy * self.chassis.pitch_y
                    v_lane_x = center_x + 1.5
                    h_lane_y = center_y - 0.5

                    lead_y = min(
                        out_y - 2.0,
                        src_blk[1] * self.chassis.pitch_y + self.chassis.core_margin_y + 8.5,
                    )
                    cross_y = out_y
                    p_lead = [
                        (out_x, out_y),
                        (out_x, lead_y),
                        (v_lane_x, lead_y),
                        (v_lane_x, cross_y - 1.0),
                    ]
                    make_path(p_lead, end_dir=DIRECTION_SOUTH, tier=tier)
                    add_belt(
                        "fast-underground-belt", v_lane_x, cross_y - 1.0, DIRECTION_SOUTH, "input"
                    )
                    add_belt(
                        "fast-underground-belt", v_lane_x, cross_y + 1.0, DIRECTION_SOUTH, "output"
                    )

                    p_to_tgt = [
                        (v_lane_x, cross_y + 2.0),
                        (v_lane_x, h_lane_y),
                        (in_x, h_lane_y),
                        (in_x, in_y - 5.0),
                    ]
                    make_path(p_to_tgt, end_dir=DIRECTION_SOUTH, tier=tier)
                    add_belt("fast-underground-belt", in_x, in_y - 5.0, DIRECTION_SOUTH, "input")
                    add_belt("fast-underground-belt", in_x, in_y, DIRECTION_SOUTH, "output")
                    add_belt(tier, in_x, in_y + 1.0, DIRECTION_EAST)

                # Case 3: Target block is East and South
                elif src_blk[0] < tgt_block[0] and src_blk[1] < tgt_block[1]:
                    h_lane_y = (
                        src_blk[1] * self.chassis.pitch_y
                        + self.chassis.core_margin_y
                        + self.chassis.core_height
                        + 1.5
                    )
                    v_lane_x = tgt_block[0] * self.chassis.pitch_x - 1.5

                    # 1. Lead from source to horizontal channel
                    make_path(
                        [(out_x, out_y), (out_x, h_lane_y)], end_dir=DIRECTION_EAST, tier=tier
                    )

                    # 2. Traverse horizontal channel, adding underground jumpers across intermediate intersections
                    curr_x = out_x
                    for col in range(src_blk[0] + 1, tgt_block[0]):
                        cx = col * self.chassis.pitch_x
                        make_path(
                            [(curr_x, h_lane_y), (cx - 4.5, h_lane_y)],
                            end_dir=DIRECTION_EAST,
                            tier=tier,
                        )
                        add_belt(
                            "fast-underground-belt", cx - 3.5, h_lane_y, DIRECTION_EAST, "input"
                        )
                        add_belt(
                            "fast-underground-belt", cx + 3.5, h_lane_y, DIRECTION_EAST, "output"
                        )
                        curr_x = cx + 4.5

                    # 3. Approach and turn inside target intersection
                    make_path(
                        [(curr_x, h_lane_y), (v_lane_x, h_lane_y)],
                        end_dir=DIRECTION_EAST,
                        tier=tier,
                    )
                    make_path(
                        [(v_lane_x, h_lane_y), (v_lane_x, in_y)], end_dir=DIRECTION_SOUTH, tier=tier
                    )
                    make_path([(v_lane_x, in_y), (in_x, in_y)], end_dir=DIRECTION_EAST, tier=tier)

                # Fallback: general orthogonal routing
                else:
                    h_lane_y = (
                        src_blk[1] * self.chassis.pitch_y
                        + self.chassis.core_margin_y
                        + self.chassis.core_height
                        + 0.5
                        + lane_off
                    )
                    turn_x = (
                        tgt_block[0] * self.chassis.pitch_x
                        + self.chassis.core_margin_x
                        - 3.5
                        + lane_off
                    )
                    waypoints = [
                        (out_x, out_y),
                        (out_x, h_lane_y),
                        (turn_x, h_lane_y),
                        (turn_x, in_y),
                        (in_x, in_y),
                    ]
                    make_path(waypoints, end_dir=DIRECTION_EAST, tier=tier)

        return entities

    def emit_fabric_entities(self, placement: Any = None) -> list[dict[str, Any]]:
        """Emit all physical belts, intersections, and port structures for the chassis fabric."""
        entities = self.emit_channel_entities()
        if placement is not None:
            port_ents = self.emit_port_connections(placement=placement)
            port_by_pos = {
                (
                    round(float(pe["position"]["x"]), 2),
                    round(float(pe["position"]["y"]), 2),
                ): pe
                for pe in port_ents
                if "position" in pe
            }
            result: list[dict[str, Any]] = []
            for e in entities:
                pos = e.get("position", {})
                key = (round(float(pos.get("x", 0.0)), 2), round(float(pos.get("y", 0.0)), 2))
                if key in port_by_pos:
                    result.append(port_by_pos.pop(key))
                else:
                    result.append(e)
            result.extend(port_by_pos.values())
            return result
        return entities


class MacroFloorplanner:
    """Hierarchical Level 1 macro floorplanner for Factorilog city block layouts."""

    def __init__(
        self,
        chassis: CityBlockChassis | None = None,
        max_block_area: float = 8836.0,
        max_block_width: float = 80.0,
        default_belt_tier: str = "fast-transport-belt",
    ) -> None:
        self.chassis = chassis or CityBlockChassis(
            pitch=100, core_width=94, core_height=94, channel_width=6
        )
        self.max_block_area = max_block_area
        self.max_block_width = max_block_width
        self.default_belt_tier = default_belt_tier

    def _cluster_width(
        self,
        inst_list: list[dict[str, Any]],
        cell_table: dict[str, Any],
    ) -> float:
        """Calculate total linear row placement width for a list of instances."""
        total_w = 0.0
        for inst in inst_list:
            cell_id = inst.get("cell", "")
            cell_meta = cell_table.get(cell_id, {})
            cw = float(cell_meta.get("width", 0))
            if cw <= 0:
                cw, _ = entity_size(cell_id)
            iname = inst.get("name", "")
            if "merge" in iname:
                pad = 16.0
            elif "split" in iname or "bal" in iname:
                pad = 10.0
            elif any(
                k in iname for k in ("disp", "acc", "comb", "dec", "latch", "switch", "params")
            ):
                pad = 2.0
            else:
                pad = 8.0
            total_w += cw + pad
        return total_w

    def _cluster_area(
        self,
        inst_list: list[dict[str, Any]],
        cell_table: dict[str, Any],
    ) -> float:
        """Calculate total machine footprint area for a list of instances."""
        total_a = 0.0
        for inst in inst_list:
            cell_id = inst.get("cell", "")
            cell_meta = cell_table.get(cell_id, {})
            cw = float(cell_meta.get("width", 0))
            ch = float(cell_meta.get("height", 0))
            if cw <= 0 or ch <= 0:
                cw, ch = entity_size(cell_id)
            total_a += cw * ch
        return total_a

    def _split_cluster_recursive(
        self,
        inst_list: list[dict[str, Any]],
        cell_table: dict[str, Any],
        max_area: float,
        max_width: float,
    ) -> list[list[dict[str, Any]]]:
        """Recursively bisect cluster instances until both area and row width fit."""
        area = self._cluster_area(inst_list, cell_table)
        width = self._cluster_width(inst_list, cell_table)

        # Base case: return list if it fits or if only one instance remains
        if (area <= max_area and width <= max_width) or len(inst_list) <= 1:
            return [inst_list]

        mid = len(inst_list) // 2
        left_splits = self._split_cluster_recursive(
            inst_list[:mid], cell_table, max_area, max_width
        )
        right_splits = self._split_cluster_recursive(
            inst_list[mid:], cell_table, max_area, max_width
        )
        return left_splits + right_splits

    def cluster_netlist(self, netlist_data: dict[str, Any]) -> list[RecipeCluster]:
        """Group netlist instances into recipe clusters and divide oversized stages."""
        instances = netlist_data.get("instances", [])
        cell_table = netlist_data.get("cells", {})

        # Group instances by recipe name
        recipe_groups: dict[str, list[dict[str, Any]]] = {}
        for inst in instances:
            recipe = inst.get("recipe", "")
            if not recipe:
                recipe = inst.get("cell", "generic")
            recipe_groups.setdefault(recipe, []).append(inst)

        clusters: list[RecipeCluster] = []
        for recipe, inst_list in sorted(recipe_groups.items()):
            inputs: set[str] = set()
            outputs: set[str] = set()
            ranks: list[int] = []

            for inst in inst_list:
                cell_id = inst.get("cell", "")
                cell_meta = cell_table.get(cell_id, {})
                for inp in cell_meta.get("inputs", []):
                    if inp not in ("a", "b", "a1", "a2"):
                        inputs.add(inp)
                for out in cell_meta.get("outputs", []):
                    if out not in ("y", "y1", "y2"):
                        outputs.add(out)
                if "rank" in inst:
                    ranks.append(int(inst["rank"]))

            avg_rank = round(sum(ranks) / len(ranks)) if ranks else 0

            # Recursively split cluster instances until both area and row width fit
            splits = self._split_cluster_recursive(
                inst_list, cell_table, self.max_block_area, self.max_block_width
            )

            total_splits = len(splits)
            if total_splits > 1:
                for i, split_insts in enumerate(splits):
                    split_names = [inst["name"] for inst in split_insts]
                    split_area = self._cluster_area(split_insts, cell_table)
                    clusters.append(
                        RecipeCluster(
                            name=f"cluster_{recipe}_part_{i}",
                            recipe=recipe,
                            instances=split_names,
                            total_area=split_area,
                            rank=avg_rank,
                            inputs=set(inputs),
                            outputs=set(outputs),
                            sub_cluster_index=i,
                            total_sub_clusters=total_splits,
                        )
                    )
            else:
                inst_names = [inst["name"] for inst in inst_list]
                total_area = self._cluster_area(inst_list, cell_table)
                clusters.append(
                    RecipeCluster(
                        name=f"cluster_{recipe}",
                        recipe=recipe,
                        instances=inst_names,
                        total_area=total_area,
                        rank=avg_rank,
                        inputs=inputs,
                        outputs=outputs,
                    )
                )

        return clusters

    def _grid_allocation_order(self, w: int, h: int) -> list[tuple[int, int]]:
        """Order grid cells from outermost perimeter to innermost center rings."""
        cells: list[tuple[int, int]] = []
        min_x, max_x = 0, w - 1
        min_y, max_y = 0, h - 1

        while min_x <= max_x and min_y <= max_y:
            if min_x == max_x and min_y == max_y:
                cells.append((min_x, min_y))
                break
            if min_x == max_x:
                cells.extend((min_x, y) for y in range(min_y, max_y + 1))
                break
            if min_y == max_y:
                cells.extend((x, min_y) for x in range(min_x, max_x + 1))
                break

            # South edge (left to right)
            cells.extend((x, min_y) for x in range(min_x, max_x))
            # East edge (bottom to top)
            cells.extend((max_x, y) for y in range(min_y, max_y))
            # North edge (right to left)
            cells.extend((x, max_y) for x in range(max_x, min_x, -1))
            # West edge (top to bottom)
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
        num_blocks = len(clusters)

        if (
            grid_w is not None
            and grid_h is not None
            and grid_w > 0
            and grid_h > 0
            and grid_w * grid_h >= num_blocks
        ):
            w = grid_w
            h = grid_h
        elif grid_w is not None and grid_w > 0:
            w = grid_w
            h = max(1, math.ceil(num_blocks / float(w)))
        elif grid_h is not None and grid_h > 0:
            h = grid_h
            w = max(1, math.ceil(num_blocks / float(h)))
        else:
            s = max(1, math.ceil(math.sqrt(num_blocks)))
            w = s
            h = s

        floorplan = MacroFloorplan(
            grid_w=w,
            grid_h=h,
            chassis=self.chassis,
            clusters=clusters,
        )

        # Sort clusters by rank and recipe to place pipeline stages in sequence
        sorted_clusters = sorted(
            clusters,
            key=lambda c: (c.rank, c.recipe, c.sub_cluster_index),
        )

        alloc_order = self._grid_allocation_order(w, h)

        # Assign each cluster to grid coordinates (block_x, block_y)
        for idx, cluster in enumerate(sorted_clusters):
            bx, by = alloc_order[idx]
            cluster.assigned_block = (bx, by)
            floorplan.block_to_clusters.setdefault((bx, by), []).append(cluster)
            for inst_name in cluster.instances:
                floorplan.instance_to_block[inst_name] = (bx, by)

        # Record all unassigned blocks as empty_blocks (for example in the middle)
        for bx, by in alloc_order[len(sorted_clusters) :]:
            floorplan.empty_blocks.add((bx, by))

        # Instantiate horizontal street channels between adjacent rows
        for by in range(h - 1):
            for bx in range(w):
                ch = self.chassis.get_horizontal_channel(
                    bx, by, default_tier=self.default_belt_tier
                )
                floorplan.channels[ch.channel_id] = ch

        # Instantiate vertical street channels between adjacent columns
        for bx in range(w - 1):
            for by in range(h):
                ch = self.chassis.get_vertical_channel(bx, by, default_tier=self.default_belt_tier)
                floorplan.channels[ch.channel_id] = ch

        # Instantiate router intersections at 4-way crossroads
        for ix in range(1, w):
            for iy in range(1, h):
                h_ch = floorplan.channels.get(f"h_channel_{ix - 1}_{iy - 1}")
                v_ch = floorplan.channels.get(f"v_channel_{ix - 1}_{iy - 1}")
                intersection = self.chassis.get_intersection(ix, iy, h_channel=h_ch, v_channel=v_ch)
                floorplan.intersections[(ix, iy)] = intersection

        return floorplan

    def assign_inter_block_routes(
        self,
        floorplan: MacroFloorplan,
        netlist_data: dict[str, Any],
    ) -> None:
        """Find inter-block nets and allocate channel lanes along transit paths."""
        fluids = set(netlist_data.get("fluids", []))
        cell_table = netlist_data.get("cells", {})

        # Map each net to its driving block and consuming blocks
        net_producers: dict[str, tuple[int, int]] = {}
        net_consumers: dict[str, set[tuple[int, int]]] = {}
        net_producer_inst: dict[str, str] = {}
        net_consumer_insts: dict[str, list[str]] = {}

        for inst in netlist_data.get("instances", []):
            iname = inst.get("name", "")
            block = floorplan.instance_to_block.get(iname)
            if block is None:
                continue

            cell_meta = cell_table.get(inst.get("cell", ""), {})
            for out_net in cell_meta.get("outputs", []):
                if out_net not in ("y", "y1", "y2"):
                    net_producers[out_net] = block
                    net_producer_inst[out_net] = iname

            for in_net in cell_meta.get("inputs", []):
                if in_net not in ("a", "b", "a1", "a2"):
                    net_consumers.setdefault(in_net, set()).add(block)
                    net_consumer_insts.setdefault(in_net, []).append(iname)

        # Route every net that connects different blocks
        all_nets = set(net_producers.keys()) | set(net_consumers.keys())
        for net_name in sorted(all_nets):
            source_block = net_producers.get(net_name)
            target_blocks = sorted(net_consumers.get(net_name, set()))

            # If no source or no targets, skip
            if source_block is None and not target_blocks:
                continue

            # Check if net stays purely inside a single block
            if source_block is not None and all(tb == source_block for tb in target_blocks):
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

            # Route from source_block to each target_block
            for target_block in target_blocks:
                if source_block is None or source_block == target_block:
                    continue

                sx, sy = source_block
                tx, ty = target_block

                # Horizontal transit
                if sx != tx:
                    dir_h = DIRECTION_EAST if tx > sx else DIRECTION_WEST
                    # Select horizontal channel along sy row (or sy - 1 if top row)
                    ch_y = sy if sy < floorplan.grid_h - 1 else max(0, sy - 1)
                    start_col = min(sx, tx)
                    end_col = max(sx, tx)

                    for col in range(start_col, end_col):
                        ch_id = f"h_channel_{col}_{ch_y}"
                        channel = floorplan.channels.get(ch_id)
                        if channel:
                            lane = channel.allocate_lane(
                                net_name,
                                direction=dir_h,
                                item_type=net_name,
                                tier=tier,
                            )
                            route.segments.append(
                                ChannelRouteSegment(
                                    channel_id=ch_id,
                                    lane_index=lane.lane_index,
                                    orientation="H",
                                    direction=dir_h,
                                )
                            )

                # Vertical transit
                if sy != ty:
                    dir_v = DIRECTION_NORTH if ty > sy else DIRECTION_SOUTH
                    # Select vertical channel along tx column (or tx - 1 if rightmost)
                    ch_x = tx if tx < floorplan.grid_w - 1 else max(0, tx - 1)
                    start_row = min(sy, ty)
                    end_row = max(sy, ty)

                    for row in range(start_row, end_row):
                        ch_id = f"v_channel_{ch_x}_{row}"
                        channel = floorplan.channels.get(ch_id)
                        if channel:
                            lane = channel.allocate_lane(
                                net_name,
                                direction=dir_v,
                                item_type=net_name,
                                tier=tier,
                            )
                            route.segments.append(
                                ChannelRouteSegment(
                                    channel_id=ch_id,
                                    lane_index=lane.lane_index,
                                    orientation="V",
                                    direction=dir_v,
                                )
                            )

                # Add turning connection at intersection if net changes direction
                if sx != tx and sy != ty:
                    corner_x = tx
                    corner_y = sy if sy < floorplan.grid_h - 1 else max(1, sy)
                    intersection = floorplan.intersections.get((corner_x, corner_y))
                    if intersection:
                        h_lane_idx = 0
                        v_lane_idx = 0
                        # Find assigned lanes in adjacent channels
                        for seg in route.segments:
                            if seg.orientation == "H":
                                h_lane_idx = seg.lane_index
                            elif seg.orientation == "V":
                                v_lane_idx = seg.lane_index
                        intersection.add_turn("H", h_lane_idx, "V", v_lane_idx)
                        route.turns.append((corner_x, corner_y, "H", h_lane_idx, "V", v_lane_idx))

            floorplan.routes[net_name] = route

    def plan(
        self,
        netlist_data: dict[str, Any],
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
    ) -> tuple[int, int] | None:
        """Return (bx, by) if pos lies inside a city block core (excluding matrix bays), else None."""
        x, y = pos
        bx = x // 100
        by = y // 100
        if 0 <= bx < grid_w and 0 <= by < grid_h and 3 <= (x % 100) <= 96:
            y_lo = 17 if (bay_blocks and (bx, by) in bay_blocks) else 3
            if y_lo <= (y % 100) <= 96:
                return (bx, by)
        return None

    @staticmethod
    def _block_core_bbox(
        blk: tuple[int, int],
        bay_blocks: set[tuple[int, int]] | None = None,
    ) -> tuple[int, int, int, int]:
        """Return the (min_x, min_y, max_x, max_y) tile bounds for a cell core."""
        bx, by = blk
        y_lo = by * 100 + (17 if (bay_blocks and blk in bay_blocks) else 3)
        return (bx * 100 + 3, y_lo, bx * 100 + 96, by * 100 + 96)

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
    ) -> tuple[str, int, int] | None:
        """Return the canonical street segment for a street channel or Matrix Switchbox Bay coordinate."""
        x, y = pos
        bx = x // 100
        by = y // 100
        if (
            bay_blocks
            and (bx, by) in bay_blocks
            and 0 <= bx < grid_w
            and 0 <= by < grid_h
            and 3 <= (x % 100) <= 96
            and 3 <= (y % 100) <= 16
        ):
            return ("H", bx, by)
        in_v = (x + 3) % 100 < 6
        in_h = (y + 3) % 100 < 6
        if in_v and not in_h and 0 <= by < grid_h:
            return ("V", (x + 3) // 100, by)
        if in_h and not in_v and 0 <= bx < grid_w:
            return ("H", bx, (y + 3) // 100)
        return None

    @staticmethod
    def _highway_corridor_bbox(
        src: tuple[int, int],
        snk: tuple[int, int],
        min_x: int,
        min_y: int,
        max_x: int,
        max_y: int,
    ) -> tuple[int, int, int, int]:
        """Return the street channel bounding box enclosing src and snk."""
        sx, sy = src
        tx, ty = snk
        x_lo, x_hi = min(sx, tx), max(sx, tx)
        y_lo, y_hi = min(sy, ty), max(sy, ty)
        bx_lo = ((x_lo // 100) + 1) * 100 - 3 if (x_lo % 100) >= 97 else (x_lo // 100) * 100 - 3
        bx_hi = (x_hi // 100) * 100 + 2 if (x_hi % 100) <= 2 else ((x_hi // 100) + 1) * 100 + 2
        by_lo = ((y_lo // 100) + 1) * 100 - 3 if (y_lo % 100) >= 97 else (y_lo // 100) * 100 - 3
        by_hi = (y_hi // 100) * 100 + 16 if (y_hi % 100) <= 16 else ((y_hi // 100) + 1) * 100 + 2
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
        if edge == "W":
            core_pos = (bx * 100 + 3, c)
            channel_pos = (bx * 100 + 2, c)
            in_pos = (bx * 100 + 4, c)
            out_pos = (bx * 100 + 1, c)
            port_dir = DIRECTION_WEST if port_type == "export" else DIRECTION_EAST
            opp_key = (bx - 1, by, "E")
        elif edge == "E":
            core_pos = (bx * 100 + 96, c)
            channel_pos = (bx * 100 + 97, c)
            in_pos = (bx * 100 + 95, c)
            out_pos = (bx * 100 + 98, c)
            port_dir = DIRECTION_EAST if port_type == "export" else DIRECTION_WEST
            opp_key = (bx + 1, by, "W")
        elif edge == "N":
            core_pos = (c, by * 100 + 3)
            channel_pos = (c, by * 100 + 2)
            in_pos = (c, by * 100 + 4)
            out_pos = (c, by * 100 + 1)
            port_dir = DIRECTION_NORTH if port_type == "export" else DIRECTION_SOUTH
            opp_key = (bx, by - 1, "S")
        else:
            core_pos = (c, by * 100 + 96)
            channel_pos = (c, by * 100 + 97)
            in_pos = (c, by * 100 + 95)
            out_pos = (c, by * 100 + 98)
            port_dir = DIRECTION_SOUTH if port_type == "export" else DIRECTION_NORTH
            opp_key = (bx, by + 1, "N")
        return core_pos, channel_pos, in_pos, out_pos, port_dir, opp_key

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
    ) -> tuple[BlockPort, tuple[int, int], tuple[int, int]]:
        """Allocate a conflict-free BlockPort on the boundary of blk."""
        bx, by = blk
        ix, iy = internal_pt
        tx, ty = target_pt
        y_min_off = 19 if has_north_bay else 5

        def _edge_score(e: str) -> float:
            if e == "N":
                px = max(bx * 100 + 5, min(bx * 100 + 94, ix))
                py = by * 100 + 2
                opp_e_key = (bx, by - 1, "S")
            elif e == "S":
                px = max(bx * 100 + 5, min(bx * 100 + 94, ix))
                py = by * 100 + 97
                opp_e_key = (bx, by + 1, "N")
            elif e == "W":
                px = bx * 100 + 2
                py = max(by * 100 + y_min_off, min(by * 100 + 94, iy))
                opp_e_key = (bx - 1, by, "E")
            else:
                px = bx * 100 + 97
                py = max(by * 100 + y_min_off, min(by * 100 + 94, iy))
                opp_e_key = (bx + 1, by, "W")

            in_cost = 1.4 * float(abs(ix - px)) + 2.4 * float(abs(iy - py))
            if (port_type == "import" and e == "W") or (port_type == "export" and e == "E"):
                in_cost -= 8.0
            if e in ("W", "E") and abs(px - tx) > 6:
                vert_d = min(
                    abs(py - (by * 100 + 2)) + abs((by * 100 + 2) - ty),
                    abs(py - (by * 100 + 97)) + abs((by * 100 + 97) - ty),
                )
                out_cost = float(abs(px - tx) + vert_d)
            elif e in ("N", "S") and abs(py - ty) > 6:
                horiz_d = min(
                    abs(px - (bx * 100 + 2)) + abs((bx * 100 + 2) - tx),
                    abs(px - (bx * 100 + 97)) + abs((bx * 100 + 97) - tx),
                )
                out_cost = float(horiz_d + abs(py - ty))
            else:
                out_cost = float(abs(px - tx) + abs(py - ty))

            here_cnt = len(used_edge_coords.get((bx, by, e), []))
            opp_cnt = len(used_edge_coords.get(opp_e_key, []))
            transit_cnt = 0.0
            if e in ("W", "E") and v_seg_transit_load is not None and 1 <= bx <= grid_w - 2:
                vx_e = bx if e == "W" else bx + 1
                vx_opp = bx + 1 if e == "W" else bx
                excess_transit = v_seg_transit_load.get((vx_e, by), 0.0) - v_seg_transit_load.get(
                    (vx_opp, by), 0.0
                )
                if excess_transit >= 1.5:
                    transit_cnt = excess_transit
            seg_cnt = here_cnt + opp_cnt + round(transit_cnt)
            load_pen = (
                16.0 * float(here_cnt)
                + 14.0 * float(opp_cnt)
                + 20.0 * float(transit_cnt)
                + (
                    45.0
                    if seg_cnt == 4
                    else (45.0 + 90.0 * float(seg_cnt - 4) if seg_cnt >= 5 else 0.0)
                )
            )
            if e == "S" and has_south_bay_neighbor:
                load_pen += 12.0
            if e == "S" and by * 100 <= ty <= by * 100 + 16:
                load_pen += 180.0
            rim_pen = 0.0
            if grid_w * grid_h > 1:
                if e == "W" and bx == 0:
                    rim_pen = -200.0 if (tx <= 2 and by * 100 <= ty <= by * 100 + 99) else 180.0
                elif e == "E" and bx == grid_w - 1:
                    if tx >= grid_w * 100 - 3 and by * 100 <= ty <= by * 100 + 99:
                        rim_pen = -200.0
                    elif (
                        port_type == "export"
                        and here_cnt == 0
                        and by == 0
                        and (ix % 100) >= 60
                        and (iy % 100) <= 55
                    ):
                        rim_pen = 20.0
                    elif (
                        by * 100 <= ty <= by * 100 + 16
                        and (tx % 100) >= 65
                        and (iy % 100) <= 50
                        and all(
                            uc > by * 100 + 50 for uc, _ in used_edge_coords.get((bx, by, e), [])
                        )
                    ) or (
                        (by + 1) * 100 <= ty <= (by + 1) * 100 + 16
                        and (tx % 100) >= 65
                        and 50 < (iy % 100) <= 55
                        and all(
                            uc <= by * 100 + 50 for uc, _ in used_edge_coords.get((bx, by, e), [])
                        )
                    ):
                        rim_pen = -60.0
                    else:
                        rim_pen = 180.0
                elif e == "N" and by == 0:
                    rim_pen = -200.0 if (ty <= 2 and bx * 100 <= tx <= bx * 100 + 99) else 120.0
                elif e == "S" and by == grid_h - 1:
                    rim_pen = (
                        -200.0
                        if (ty >= grid_h * 100 - 3 and bx * 100 <= tx <= bx * 100 + 99)
                        else 120.0
                    )
            return in_cost + out_cost + load_pen + rim_pen

        all_edges = ["N", "S", "E", "W"]
        forbid = set(forbidden_edges) if forbidden_edges else set()
        if has_north_bay:
            forbid.add("N")
        allowed_edges = [e for e in all_edges if e not in forbid]
        if not allowed_edges:
            allowed_edges = [e for e in all_edges if not (has_north_bay and e == "N")] or all_edges

        sorted_by_score = sorted(allowed_edges, key=_edge_score)
        if preferred_edge is not None and preferred_edge in allowed_edges:
            edge_order = [preferred_edge] + [e for e in sorted_by_score if e != preferred_edge]
        else:
            edge_order = sorted_by_score

        for spacing in (3, 2, 1):
            for edge in edge_order:
                base = by * 100 if edge in ("W", "E") else bx * 100
                c_lo_off = y_min_off if edge in ("W", "E") else 5
                if preferred_c is not None and base + c_lo_off <= preferred_c <= base + 94:
                    ideal_c = preferred_c
                elif edge in ("W", "E"):
                    if by * 100 + c_lo_off <= ty <= by * 100 + 94 and (
                        (edge == "W" and tx <= bx * 100 + 2 and bx * 100 - tx <= 6)
                        or (edge == "E" and tx >= bx * 100 + 97 and tx - (bx * 100 + 99) <= 6)
                    ):
                        ideal_c = ty
                    else:
                        ideal_c = iy
                else:
                    ideal_c = ix
                ideal_c = max(base + c_lo_off, min(base + 94, ideal_c))

                candidates = sorted(
                    range(base + c_lo_off, base + 95),
                    key=lambda c_val: (abs(c_val - ideal_c), c_val),
                )
                edge_key = (bx, by, edge)
                used_here = used_edge_coords.get(edge_key, [])
                pin_here = pin_reserved_coords.get(edge_key, [])

                for c_val in candidates:
                    if any(abs(c_val - uc) < spacing for uc, _ in used_here):
                        continue
                    if any(
                        abs(c_val - pc) < 2 and p_pos not in (internal_pt, target_pt)
                        for pc, p_pos in pin_here
                    ):
                        continue
                    core_pos, channel_pos, in_pos, out_pos, port_dir, opp_key = (
                        self._edge_port_geometry(blk, edge, port_type, c_val)
                    )
                    used_opp = used_edge_coords.get(opp_key, [])
                    if any(abs(c_val - uc) < spacing and unet != net_name for uc, unet in used_opp):
                        continue
                    if any(pt in obstacles for pt in (core_pos, channel_pos, in_pos, out_pos)):
                        continue
                    if any(
                        pt in all_terminals and pt not in (internal_pt, target_pt)
                        for pt in (core_pos, channel_pos, in_pos, out_pos)
                    ):
                        continue

                    used_edge_coords.setdefault(edge_key, []).append((c_val, net_name))
                    ch_id = (
                        f"v_channel_{max(0, bx - 1 if edge == 'W' else bx)}_{by}"
                        if edge in ("W", "E")
                        else f"h_channel_{bx}_{max(0, by - 1 if edge == 'N' else by)}"
                    )
                    port = BlockPort(
                        name=f"{net_name}_{port_type}_{bx}_{by}_{edge}_{c_val}",
                        block=blk,
                        net_name=net_name,
                        port_type=port_type,
                        edge=edge,
                        position=(float(channel_pos[0]), float(channel_pos[1])),
                        channel_id=ch_id,
                        lane_index=0,
                        direction=port_dir,
                        tier=tier,
                        internal_pos=(float(core_pos[0]), float(core_pos[1])),
                    )
                    return port, core_pos, channel_pos

        # Fallback if all candidates were constrained
        edge = edge_order[0]
        base = by * 100 if edge in ("W", "E") else bx * 100
        c_lo_off = y_min_off if edge in ("W", "E") else 5
        c_val = max(base + c_lo_off, min(base + 94, iy if edge in ("W", "E") else ix))
        core_pos, channel_pos, _in_p, _out_p, port_dir, _opp = self._edge_port_geometry(
            blk, edge, port_type, c_val
        )
        used_edge_coords.setdefault((bx, by, edge), []).append((c_val, net_name))
        port = BlockPort(
            name=f"{net_name}_{port_type}_{bx}_{by}_{edge}_{c_val}",
            block=blk,
            net_name=net_name,
            port_type=port_type,
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
        net_specs: list[Any],
        grid_w: int,
        grid_h: int,
        obstacles: set[tuple[int, int]],
        min_x: int = 0,
        min_y: int = 0,
        max_x: int | None = None,
        max_y: int | None = None,
        bay_blocks: set[tuple[int, int]] | None = None,
    ) -> tuple[
        dict[tuple[int, int], list[Any]],
        list[Any],
        set[tuple[int, int]],
        dict[tuple[int, int], int],
        MacroFloorplan,
    ]:
        """Decompose flat RouterNetSpecs into intra-block core specs and street highway specs.

        Returns:
            intra_block_specs: Mapping of (bx, by) -> list of RouterNetSpec inside that 94x94 core.
            highway_specs: List of RouterNetSpec for the 6-tile street channels and switchboxes.
            port_no_hood_tiles: Set of (x, y) boundary tiles where underground hoods are forbidden.
            port_headings: Mapping of (x, y) -> heading for allocated belt port tiles.
            floorplan: Populated MacroFloorplan with channels, intersections, ports, and routes.
        """
        try:
            from foundry.router.types import RouterNetSpec
        except ImportError:
            from router.types import RouterNetSpec

        if max_x is None:
            max_x = grid_w * 100
        if max_y is None:
            max_y = grid_h * 100

        if bay_blocks is None:
            bay_blocks = set()
            for spec in net_specs:
                for pt in (spec.source, spec.sink):
                    bx_p, by_p = pt[0] // 100, pt[1] // 100
                    if (
                        0 <= bx_p < grid_w
                        and 0 <= by_p < grid_h
                        and 12 <= (pt[0] % 100) <= 88
                        and 4 <= (pt[1] % 100) <= 15
                    ):
                        bay_blocks.add((bx_p, by_p))

        floorplan = self.plan_grid([], grid_w=grid_w, grid_h=grid_h)
        floorplan.empty_blocks.clear()

        intra_block_specs: dict[tuple[int, int], list[Any]] = {
            (bx, by): [] for bx in range(grid_w) for by in range(grid_h)
        }
        highway_specs: list[Any] = []
        port_no_hood_tiles: set[tuple[int, int]] = set()
        port_headings: dict[tuple[int, int], int] = {}
        used_edge_coords: dict[tuple[int, int, str], list[tuple[int, str]]] = {}
        pin_reserved_coords: dict[tuple[int, int, str], list[tuple[int, tuple[int, int]]]] = {}

        all_terminals: set[tuple[int, int]] = set()
        for spec in net_specs:
            all_terminals.add(spec.source)
            all_terminals.add(spec.sink)
            for pt in (spec.source, spec.sink):
                if self._block_of_coord(pt, grid_w, grid_h, bay_blocks) is None:
                    px, py = pt
                    by_pin = max(0, min(grid_h - 1, py // 100))
                    bx_pin = max(0, min(grid_w - 1, px // 100))
                    if px <= 2:
                        pin_reserved_coords.setdefault((0, by_pin, "W"), []).append((py, pt))
                    elif px >= grid_w * 100 - 3:
                        pin_reserved_coords.setdefault((grid_w - 1, by_pin, "E"), []).append(
                            (py, pt)
                        )
                    if py <= 2:
                        pin_reserved_coords.setdefault((bx_pin, 0, "N"), []).append((px, pt))
                    elif py >= grid_h * 100 - 3:
                        pin_reserved_coords.setdefault((bx_pin, grid_h - 1, "S"), []).append(
                            (px, pt)
                        )

        def _record_inter_block_channel_route(
            net_name: str,
            src_blk: tuple[int, int] | None,
            dst_blk: tuple[int, int] | None,
            is_fluid: bool,
            src_port: BlockPort | None = None,
            dst_port: BlockPort | None = None,
        ) -> None:
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
                sx, sy = src_blk
                tx, ty = dst_blk
                if sx != tx:
                    dir_h = DIRECTION_EAST if tx > sx else DIRECTION_WEST
                    ch_y = sy if sy < grid_h - 1 else max(0, sy - 1)
                    for col in range(min(sx, tx), max(sx, tx)):
                        ch_id = f"h_channel_{col}_{ch_y}"
                        channel = floorplan.channels.get(ch_id)
                        if channel:
                            try:
                                lane = channel.allocate_lane(
                                    net_name, direction=dir_h, item_type=net_name, tier=tier
                                )
                                route.segments.append(
                                    ChannelRouteSegment(
                                        channel_id=ch_id,
                                        lane_index=lane.lane_index,
                                        orientation="H",
                                        direction=dir_h,
                                    )
                                )
                            except RuntimeError:
                                pass
                if sy != ty:
                    dir_v = DIRECTION_NORTH if ty > sy else DIRECTION_SOUTH
                    ch_x = tx if tx < grid_w - 1 else max(0, tx - 1)
                    for row in range(min(sy, ty), max(sy, ty)):
                        ch_id = f"v_channel_{ch_x}_{row}"
                        channel = floorplan.channels.get(ch_id)
                        if channel:
                            try:
                                lane = channel.allocate_lane(
                                    net_name, direction=dir_v, item_type=net_name, tier=tier
                                )
                                route.segments.append(
                                    ChannelRouteSegment(
                                        channel_id=ch_id,
                                        lane_index=lane.lane_index,
                                        orientation="V",
                                        direction=dir_v,
                                    )
                                )
                            except RuntimeError:
                                pass
                if sx != tx and sy != ty:
                    corner_x = tx
                    corner_y = sy if sy < grid_h - 1 else max(1, sy)
                    intersection = floorplan.intersections.get((corner_x, corner_y))
                    if intersection:
                        h_lane_idx = 0
                        v_lane_idx = 0
                        for seg in route.segments:
                            if seg.orientation == "H":
                                h_lane_idx = seg.lane_index
                            elif seg.orientation == "V":
                                v_lane_idx = seg.lane_index
                        intersection.add_turn("H", h_lane_idx, "V", v_lane_idx)
                        route.turns.append((corner_x, corner_y, "H", h_lane_idx, "V", v_lane_idx))
            floorplan.routes[net_name] = route

        # Separate solid nets and fluid nets (grouping fluid MST branches by base fluid net)
        solid_specs = [s for s in net_specs if not s.is_fluid]
        fluid_specs = [s for s in net_specs if s.is_fluid]

        v_seg_transit_load: dict[tuple[int, int], float] = {}

        def _record_v_transit(
            p1: tuple[int, int],
            p2: tuple[int, int],
        ) -> None:
            if grid_w <= 1:
                return
            sx, sy = p1
            tx, ty = p2
            y_lo, y_hi = min(sy, ty), max(sy, ty)
            best_vx = min(
                range(1, grid_w),
                key=lambda vx: abs(sx - vx * 100) + abs(tx - vx * 100),
            )
            for r in range(grid_h):
                if y_lo <= r * 100 + 16 and y_hi >= r * 100 + 97:
                    v_seg_transit_load[(best_vx, r)] = (
                        v_seg_transit_load.get((best_vx, r), 0.0) + 1.0
                    )

        for net in solid_specs:
            src_b = self._block_of_coord(net.source, grid_w, grid_h, bay_blocks)
            dst_b = self._block_of_coord(net.sink, grid_w, grid_h, bay_blocks)
            if src_b is None or dst_b is None or src_b != dst_b:
                _record_v_transit(net.source, net.sink)

        for net in solid_specs:
            src_blk = self._block_of_coord(net.source, grid_w, grid_h, bay_blocks)
            dst_blk = self._block_of_coord(net.sink, grid_w, grid_h, bay_blocks)

            if src_blk is not None and src_blk == dst_blk:
                bbox = self._block_core_bbox(src_blk, bay_blocks)
                intra_block_specs[src_blk].append(
                    RouterNetSpec(
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
                continue

            if src_blk is None and dst_blk is None:
                highway_specs.append(
                    RouterNetSpec(
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
                            net.source, net.sink, min_x, min_y, max_x, max_y
                        ),
                    )
                )
                continue

            p_out: BlockPort | None = None
            p_in: BlockPort | None = None
            hwy_src = net.source
            hwy_start_h = net.start_heading
            hwy_snk = net.sink
            hwy_end_h = net.end_heading
            hwy_term_h = net.terminal_heading

            src_seg: tuple[str, int, int] | None = self._street_segment_of_coord(
                net.source, grid_w, grid_h, bay_blocks
            )
            snk_seg_init: tuple[str, int, int] | None = self._street_segment_of_coord(
                net.sink, grid_w, grid_h, bay_blocks
            )

            if src_blk is not None:
                src_forbid: set[str] = set()
                if snk_seg_init is not None:
                    for e in ("N", "S", "E", "W"):
                        if self._street_segment_of_edge(src_blk[0], src_blk[1], e) == snk_seg_init:
                            src_forbid.add(e)
                p_out, out_core, out_chan = self._allocate_block_port(
                    blk=src_blk,
                    net_name=net.name,
                    port_type="export",
                    internal_pt=net.source,
                    target_pt=net.sink,
                    obstacles=obstacles,
                    used_edge_coords=used_edge_coords,
                    pin_reserved_coords=pin_reserved_coords,
                    all_terminals=all_terminals,
                    forbidden_edges=src_forbid,
                    has_north_bay=(src_blk in bay_blocks),
                    has_south_bay_neighbor=((src_blk[0], src_blk[1] + 1) in bay_blocks),
                    tier=self.default_belt_tier,
                    grid_w=grid_w,
                    grid_h=grid_h,
                    v_seg_transit_load=v_seg_transit_load,
                )
                floorplan.ports.append(p_out)
                port_no_hood_tiles.add(out_core)
                port_headings[out_core] = p_out.direction
                port_headings[out_chan] = p_out.direction
                all_terminals.add(out_core)
                all_terminals.add(out_chan)

                intra_block_specs[src_blk].append(
                    RouterNetSpec(
                        name=f"{net.name}_export_{src_blk[0]}_{src_blk[1]}",
                        source=net.source,
                        sink=out_core,
                        commodity=net.commodity,
                        lane=net.lane,
                        is_fluid=False,
                        start_heading=net.start_heading,
                        end_heading=p_out.direction,
                        terminal_heading=p_out.direction,
                        corridor_bbox=self._block_core_bbox(src_blk, bay_blocks),
                    )
                )
                hwy_src = out_chan
                hwy_start_h = p_out.direction
                src_seg = self._street_segment_of_edge(src_blk[0], src_blk[1], p_out.edge)

            if dst_blk is not None:
                dst_forbid: set[str] = set()
                if src_seg is not None:
                    for e in ("N", "S", "E", "W"):
                        if self._street_segment_of_edge(dst_blk[0], dst_blk[1], e) == src_seg:
                            dst_forbid.add(e)
                p_in, in_core, in_chan = self._allocate_block_port(
                    blk=dst_blk,
                    net_name=net.name,
                    port_type="import",
                    internal_pt=net.sink,
                    target_pt=hwy_src,
                    obstacles=obstacles,
                    used_edge_coords=used_edge_coords,
                    pin_reserved_coords=pin_reserved_coords,
                    all_terminals=all_terminals,
                    forbidden_edges=dst_forbid,
                    has_north_bay=(dst_blk in bay_blocks),
                    has_south_bay_neighbor=((dst_blk[0], dst_blk[1] + 1) in bay_blocks),
                    tier=self.default_belt_tier,
                    grid_w=grid_w,
                    grid_h=grid_h,
                    v_seg_transit_load=v_seg_transit_load,
                )
                floorplan.ports.append(p_in)
                port_no_hood_tiles.add(in_core)
                port_headings[in_core] = p_in.direction
                port_headings[in_chan] = p_in.direction
                all_terminals.add(in_core)
                all_terminals.add(in_chan)

                intra_block_specs[dst_blk].append(
                    RouterNetSpec(
                        name=f"{net.name}_import_{dst_blk[0]}_{dst_blk[1]}",
                        source=in_core,
                        sink=net.sink,
                        commodity=net.commodity,
                        lane=net.lane,
                        is_fluid=False,
                        start_heading=p_in.direction,
                        end_heading=net.end_heading,
                        terminal_heading=net.terminal_heading,
                        corridor_bbox=self._block_core_bbox(dst_blk, bay_blocks),
                    )
                )
                hwy_snk = in_chan
                hwy_end_h = p_in.direction
                hwy_term_h = p_in.direction

            highway_specs.append(
                RouterNetSpec(
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
                        hwy_src, hwy_snk, min_x, min_y, max_x, max_y
                    ),
                )
            )
            _record_inter_block_channel_route(net.name, src_blk, dst_blk, False, p_out, p_in)

        # Group fluid specs by base fluid net name
        fluid_groups: dict[str, list[Any]] = {}
        for fspec in fluid_specs:
            b_name = fspec.name.split("_mst_")[0]
            if "_d" in b_name and "_s" in b_name:
                b_name = b_name.split("_d")[0]
            fluid_groups.setdefault(b_name, []).append(fspec)

        for b_name, f_list in fluid_groups.items():
            comm = f_list[0].commodity
            term_map: dict[tuple[int, int], tuple[tuple[int, int], int | None, int | None]] = {}
            for fs in f_list:
                if fs.sink not in term_map:
                    term_map[fs.sink] = (fs.sink, fs.end_heading, fs.sink_inward_dir)
                if fs.source not in term_map:
                    term_map[fs.source] = (fs.source, fs.start_heading, fs.source_inward_dir)

            by_blk: dict[
                tuple[int, int] | None, list[tuple[tuple[int, int], int | None, int | None]]
            ] = {}
            for t_pos, t_info in term_map.items():
                blk_id = self._block_of_coord(t_pos, grid_w, grid_h, bay_blocks)
                by_blk.setdefault(blk_id, []).append(t_info)

            pin_terms = by_blk.get(None, [])
            blk_terms = {k: v for k, v in by_blk.items() if k is not None}

            if not pin_terms and len(blk_terms) <= 1:
                only_blk = next(iter(blk_terms.keys()), None)
                if only_blk is not None:
                    bbox = self._block_core_bbox(only_blk, bay_blocks)
                    for fs in f_list:
                        intra_block_specs[only_blk].append(
                            RouterNetSpec(
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
                else:
                    highway_specs.extend(
                        RouterNetSpec(
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
                                fs.source, fs.sink, min_x, min_y, max_x, max_y
                            ),
                        )
                        for fs in f_list
                    )
                continue

            hwy_nodes: list[tuple[tuple[int, int], int | None, int | None]] = list(pin_terms)
            sorted_blks = sorted(blk_terms.keys())
            first_blk: tuple[int, int] | None = sorted_blks[0] if sorted_blks else None
            used_fluid_segs: set[tuple[str, int, int]] = set()

            for blk in sorted_blks:
                t_list = blk_terms[blk]
                other_pts = [p[0] for p in pin_terms] + [
                    p[0] for ob, ol in blk_terms.items() if ob != blk for p in ol
                ]
                if other_pts:
                    avg_tx = round(sum(p[0] for p in other_pts) / len(other_pts))
                    avg_ty = round(sum(p[1] for p in other_pts) / len(other_pts))
                    target_pt = min(
                        other_pts, key=lambda p: abs(p[0] - avg_tx) + abs(p[1] - avg_ty)
                    )
                else:
                    target_pt = (blk[0] * 100 + 50, blk[1] * 100 + 50)

                rep_t = min(
                    t_list,
                    key=lambda t_item: (
                        abs(t_item[0][0] - target_pt[0]) + abs(t_item[0][1] - target_pt[1])
                    ),
                )
                f_forbid = {
                    e
                    for e in ("N", "S", "E", "W")
                    if self._street_segment_of_edge(blk[0], blk[1], e) in used_fluid_segs
                }
                f_port, f_core, f_chan = self._allocate_block_port(
                    blk=blk,
                    net_name=b_name,
                    port_type="import" if pin_terms or blk != first_blk else "export",
                    internal_pt=rep_t[0],
                    target_pt=target_pt,
                    obstacles=obstacles,
                    used_edge_coords=used_edge_coords,
                    pin_reserved_coords=pin_reserved_coords,
                    all_terminals=all_terminals,
                    forbidden_edges=f_forbid,
                    has_north_bay=(blk in bay_blocks),
                    has_south_bay_neighbor=((blk[0], blk[1] + 1) in bay_blocks),
                    tier="pipe",
                    grid_w=grid_w,
                    grid_h=grid_h,
                )
                used_fluid_segs.add(self._street_segment_of_edge(blk[0], blk[1], f_port.edge))
                floorplan.ports.append(f_port)
                port_no_hood_tiles.add(f_core)
                port_no_hood_tiles.add(f_chan)
                all_terminals.add(f_core)
                all_terminals.add(f_chan)

                # Build intra-block Prim's MST rooted at the boundary port f_core
                local_nodes: list[tuple[tuple[int, int], int | None, int | None]] = [
                    (f_core, None, None),
                    *t_list,
                ]
                in_tree = [local_nodes[0]]
                remaining = set(local_nodes[1:])
                edge_idx = 0
                bbox = self._block_core_bbox(blk, bay_blocks)
                while remaining:
                    best_dist = float("inf")
                    best_pair = None
                    for u in in_tree:
                        for v in remaining:
                            d = abs(u[0][0] - v[0][0]) + abs(u[0][1] - v[0][1])
                            if d < best_dist:
                                best_dist = d
                                best_pair = (u, v)
                    if best_pair is None:
                        break
                    u, v = best_pair
                    in_tree.append(v)
                    remaining.remove(v)
                    intra_block_specs[blk].append(
                        RouterNetSpec(
                            name=f"{b_name}_blk_{blk[0]}_{blk[1]}_mst_{edge_idx}",
                            source=v[0],
                            sink=u[0],
                            commodity=comm,
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

                hwy_nodes.append((f_chan, None, None))
                if first_blk is not None and blk != first_blk:
                    _record_inter_block_channel_route(b_name, first_blk, blk, True, None, f_port)

            if len(hwy_nodes) >= 2:
                in_tree = [hwy_nodes[0]]
                remaining = set(hwy_nodes[1:])
                edge_idx = 0
                while remaining:
                    best_dist = float("inf")
                    best_pair = None
                    for u in in_tree:
                        for v in remaining:
                            d = abs(u[0][0] - v[0][0]) + abs(u[0][1] - v[0][1])
                            if d < best_dist:
                                best_dist = d
                                best_pair = (u, v)
                    if best_pair is None:
                        break
                    u, v = best_pair
                    in_tree.append(v)
                    remaining.remove(v)
                    highway_specs.append(
                        RouterNetSpec(
                            name=f"{b_name}_hwy_mst_{edge_idx}",
                            source=v[0],
                            sink=u[0],
                            commodity=comm,
                            is_fluid=True,
                            start_heading=v[1],
                            end_heading=None,
                            terminal_heading=None,
                            source_inward_dir=v[2],
                            sink_inward_dir=u[2],
                            corridor_bbox=self._highway_corridor_bbox(
                                v[0], u[0], min_x, min_y, max_x, max_y
                            ),
                        )
                    )
                    edge_idx += 1

        return intra_block_specs, highway_specs, port_no_hood_tiles, port_headings, floorplan
