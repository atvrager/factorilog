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

        if grid_w is not None and grid_h is not None:
            w = grid_w
            h = grid_h
        elif grid_w is not None:
            w = grid_w
            h = max(1, math.ceil(num_blocks / float(w)))
        elif grid_h is not None:
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
