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
class InterBlockRoute:
    """Routing lane reservations and turn instructions for an inter-block net."""

    net_name: str
    source_block: tuple[int, int] | None
    target_blocks: list[tuple[int, int]]
    segments: list[ChannelRouteSegment] = field(default_factory=list)
    turns: list[tuple[int, int, str, int, str, int]] = field(default_factory=list)
    is_fluid: bool = False
    tier: str = "fast-transport-belt"


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
        self.channels: dict[str, StreetChannel] = {}
        self.intersections: dict[tuple[int, int], RouterIntersection] = {}
        self.routes: dict[str, InterBlockRoute] = {}

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
        return self.chassis.emit_floorplan_tcl(self.grid_w, self.grid_h)

    def emit_def_blockages(self, dbu: int = 1000) -> str:
        """Emit DEF BLOCKAGES section covering all street channels."""
        return self.chassis.emit_def_blockages(self.grid_w, self.grid_h, dbu=dbu)

    def emit_channel_entities(self) -> list[dict[str, Any]]:
        """Emit all physical belts, pipes, and router switchboxes for inter-block routes."""
        entities: list[dict[str, Any]] = []
        seen_keys: set[tuple[float, float, str]] = set()

        # Emit straight belts and pipes for all allocated channel lanes
        for channel in self.channels.values():
            for ent in channel.emit_straight_belts():
                pos = ent.get("position", {})
                key = (
                    round(float(pos.get("x", 0.0)), 2),
                    round(float(pos.get("y", 0.0)), 2),
                    ent.get("name", ""),
                )
                if key not in seen_keys:
                    seen_keys.add(key)
                    entities.append(ent)

        # Emit intersection switchbox entities (crossings, undergrounds, turns, splitters)
        for intersection in self.intersections.values():
            for ent in intersection.emit_intersection_entities():
                pos = ent.get("position", {})
                key = (
                    round(float(pos.get("x", 0.0)), 2),
                    round(float(pos.get("y", 0.0)), 2),
                    ent.get("name", ""),
                )
                if key not in seen_keys:
                    seen_keys.add(key)
                    entities.append(ent)

        return entities


class MacroFloorplanner:
    """Hierarchical Level 1 macro floorplanner for Factorilog city block layouts."""

    def __init__(
        self,
        chassis: CityBlockChassis | None = None,
        max_block_area: float = 8836.0,
        default_belt_tier: str = "fast-transport-belt",
    ) -> None:
        self.chassis = chassis or CityBlockChassis(
            pitch=100, core_width=94, core_height=94, channel_width=6
        )
        self.max_block_area = max_block_area
        self.default_belt_tier = default_belt_tier

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
            # Calculate total area of instances in this cluster
            total_area = 0.0
            inputs: set[str] = set()
            outputs: set[str] = set()
            ranks: list[int] = []

            for inst in inst_list:
                cell_id = inst.get("cell", "")
                cell_meta = cell_table.get(cell_id, {})
                cw = float(cell_meta.get("width", 0))
                ch = float(cell_meta.get("height", 0))
                if cw <= 0 or ch <= 0:
                    cw, ch = entity_size(cell_id)
                total_area += cw * ch

                for inp in cell_meta.get("inputs", []):
                    if inp not in ("a", "b", "a1", "a2"):
                        inputs.add(inp)
                for out in cell_meta.get("outputs", []):
                    if out not in ("y", "y1", "y2"):
                        outputs.add(out)

                if "rank" in inst:
                    ranks.append(int(inst["rank"]))

            avg_rank = round(sum(ranks) / len(ranks)) if ranks else 0
            inst_names = [inst["name"] for inst in inst_list]

            # Check if multi-block allocation is required
            if total_area > self.max_block_area and len(inst_names) > 1:
                k = math.ceil(total_area / self.max_block_area)
                slice_size = math.ceil(len(inst_names) / k)
                for i in range(k):
                    part_names = inst_names[i * slice_size : (i + 1) * slice_size]
                    if not part_names:
                        continue
                    part_area = total_area * (len(part_names) / float(len(inst_names)))
                    clusters.append(
                        RecipeCluster(
                            name=f"cluster_{recipe}_part_{i}",
                            recipe=recipe,
                            instances=part_names,
                            total_area=part_area,
                            rank=avg_rank,
                            inputs=set(inputs),
                            outputs=set(outputs),
                            sub_cluster_index=i,
                            total_sub_clusters=k,
                        )
                    )
            else:
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
            w = max(1, math.ceil(math.sqrt(num_blocks)))
            h = max(1, math.ceil(num_blocks / float(w)))

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

        # Assign each cluster to grid coordinates (block_x, block_y)
        for idx, cluster in enumerate(sorted_clusters):
            bx = idx % w
            by = idx // w
            cluster.assigned_block = (bx, by)
            floorplan.block_to_clusters.setdefault((bx, by), []).append(cluster)
            for inst_name in cluster.instances:
                floorplan.instance_to_block[inst_name] = (bx, by)

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

        for inst in netlist_data.get("instances", []):
            iname = inst.get("name", "")
            block = floorplan.instance_to_block.get(iname)
            if block is None:
                continue

            cell_meta = cell_table.get(inst.get("cell", ""), {})
            for out_net in cell_meta.get("outputs", []):
                if out_net not in ("y", "y1", "y2"):
                    net_producers[out_net] = block

            for in_net in cell_meta.get("inputs", []):
                if in_net not in ("a", "b", "a1", "a2"):
                    net_consumers.setdefault(in_net, set()).add(block)

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
