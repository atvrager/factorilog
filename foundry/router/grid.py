"""Routing grid representation for 3D multi-layer routing.

Maintains multi-layer occupancy, dynamic prototype layer registry, and congestion state.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from typing import Any

from foundry.native_engine.verifier import load_game_prototypes
from foundry.router.types import (
    LANE_BOTH,
    LAYER_SURFACE,
    RouteEdge,
    RouteNode,
    RouteResult,
)


class RoutingGrid:
    """Manages spatial occupancy and congestion costs for 3D routing."""

    def __init__(
        self,
        min_x: int,
        max_x: int,
        min_y: int,
        max_y: int,
        prototypes: dict[str, Any] | None = None,
        obstacles: set[tuple[int, int]] | None = None,
    ) -> None:
        """Initialize the routing grid with dimensions and prototype parameters."""
        self.min_x = min_x
        self.max_x = max_x
        self.min_y = min_y
        self.max_y = max_y

        self.prototypes = prototypes if prototypes is not None else load_game_prototypes()
        self.fixed_obstacles: set[tuple[int, int]] = set(obstacles) if obstacles else set()

        # Dynamic layer configuration
        self.belt_tiers: dict[int, dict[str, Any]] = self._init_belt_tiers()
        self.pipe_tiers: dict[int, dict[str, Any]] = self._init_pipe_tiers()

        # Occupancy tracking: layer -> (x, y) -> net_name
        self.layer_occupancy: dict[int, dict[tuple[int, int], str]] = {
            LAYER_SURFACE: {},
        }
        for z in self.belt_tiers:
            self.layer_occupancy[z] = {}
        for z in self.pipe_tiers:
            self.layer_occupancy[z] = {}

        # Lane-level occupancy on surface: (x, y) -> {lane_idx: net_name}
        self.surface_lanes: dict[tuple[int, int], dict[int, str]] = {}

        # Congestion tracking
        self.usage_count: dict[tuple[int, int, int], int] = {}
        self.historical_cost: dict[tuple[int, int, int], float] = {}
        self.present_cost: dict[tuple[int, int, int], float] = {}

        # Stored net routes: net_name -> RouteResult
        self.net_routes: dict[str, RouteResult] = {}

    def _init_belt_tiers(self) -> dict[int, dict[str, Any]]:
        """Register underground belt tiers dynamically from prototypes."""
        result: dict[int, dict[str, Any]] = {}
        ug_dict = self.prototypes.get("underground-belt", {})
        belt_dict = self.prototypes.get("transport-belt", {})

        # Sort underground prototypes by speed ascending
        valid_ugs = [
            (name, proto)
            for name, proto in ug_dict.items()
            if "speed" in proto and "max_distance" in proto
        ]
        sorted_ugs = sorted(
            valid_ugs,
            key=lambda kv: float(kv[1]["speed"]),
        )

        for idx, (ug_name, ug_proto) in enumerate(sorted_ugs):
            layer_id = idx + 1
            speed = float(ug_proto["speed"])
            span = int(ug_proto["max_distance"])

            # Match surface belt by related_underground_belt or speed
            matching_belt = next(
                (
                    b_name
                    for b_name, b_proto in belt_dict.items()
                    if b_proto.get("related_underground_belt") == ug_name
                    or ("speed" in b_proto and float(b_proto["speed"]) == speed)
                ),
                None,
            )
            if not matching_belt:
                continue

            result[layer_id] = {
                "name": ug_name,
                "belt_name": matching_belt,
                "max_span": span,
                "tier_cost": idx * 1.5,
                "speed": speed,
            }
        return result

    def _init_pipe_tiers(self) -> dict[int, dict[str, Any]]:
        """Register underground pipe tiers dynamically from prototypes."""
        result: dict[int, dict[str, Any]] = {}
        pg_dict = self.prototypes.get("pipe-to-ground", {})
        pipe_dict = self.prototypes.get("pipe", {})
        default_pipe = next(iter(pipe_dict.keys()), "pipe")

        # Sort pipe-to-ground prototypes by name
        sorted_pgs = sorted(pg_dict.items(), key=lambda kv: kv[0])

        for idx, (pg_name, pg_proto) in enumerate(sorted_pgs):
            layer_id = 10 + idx
            fluid_box = pg_proto.get("fluid_box", {})
            conn_dist = None
            for conn in fluid_box.get("pipe_connections", []):
                if (
                    conn.get("connection_type") == "underground"
                    and "max_underground_distance" in conn
                ):
                    conn_dist = int(conn["max_underground_distance"])
                    break
            if conn_dist is None:
                continue

            result[layer_id] = {
                "name": pg_name,
                "pipe_name": default_pipe,
                "max_span": conn_dist,
                "tier_cost": idx * 1.5,
            }
        return result

    def is_in_bounds(self, x: int, y: int) -> bool:
        """Check if coordinates lie inside the grid boundaries."""
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def is_obstacle(self, x: int, y: int) -> bool:
        """Check if a surface tile contains a fixed obstacle."""
        return (x, y) in self.fixed_obstacles

    def get_tier_span(self, layer_id: int) -> int:
        """Return the maximum underground span for a layer."""
        if layer_id in self.belt_tiers:
            return self.belt_tiers[layer_id]["max_span"]
        if layer_id in self.pipe_tiers:
            return self.pipe_tiers[layer_id]["max_span"]
        return 5

    def get_tier_cost(self, layer_id: int) -> float:
        """Return the base upgrade cost for a layer."""
        if layer_id in self.belt_tiers:
            return self.belt_tiers[layer_id]["tier_cost"]
        if layer_id in self.pipe_tiers:
            return self.pipe_tiers[layer_id]["tier_cost"]
        return 0.0

    def get_node_cost(self, node: RouteNode, base_cost: float) -> float:
        """Compute the dynamic traversal cost with negotiated congestion."""
        key = (node.x, node.y, node.z)
        h = self.historical_cost.get(key, 0.0)
        p = self.present_cost.get(key, 1.0)
        return (base_cost + h) * p

    def can_occupy_surface(self, pos: tuple[int, int], net_name: str, lane: int) -> bool:
        """Check if a net can legally place an entity on the surface tile."""
        if self.is_obstacle(pos[0], pos[1]):
            return False

        current_net = self.layer_occupancy[LAYER_SURFACE].get(pos)
        if current_net is None or current_net == net_name:
            return True

        # Check dual-lane sharing
        if lane != LANE_BOTH:
            lanes = self.surface_lanes.get(pos, {})
            existing_lane_net = lanes.get(lane)
            if existing_lane_net is None or existing_lane_net == net_name:
                return len(lanes) < 2
        return False

    def can_occupy_subterranean(
        self,
        layer_id: int,
        span_coords: list[tuple[int, int]],
        net_name: str,
    ) -> bool:
        """Check if an underground leap avoids collisions on its discrete layer."""
        occ = self.layer_occupancy.get(layer_id, {})
        for pt in span_coords:
            existing = occ.get(pt)
            if existing is not None and existing != net_name:
                return False
        return True

    def rip_up_net(self, net_name: str) -> None:
        """Remove an existing routed net from the grid occupancy."""
        route = self.net_routes.pop(net_name, None)
        if not route:
            return

        for edge in route.edges:
            self._unmark_edge(edge, net_name)

    def place_net(self, net_name: str, route: RouteResult) -> None:
        """Add a routed net to the grid occupancy and update usage counts."""
        self.net_routes[net_name] = route
        for edge in route.edges:
            self._mark_edge(edge, net_name)

    def _mark_edge(self, edge: RouteEdge, net_name: str) -> None:
        """Record occupancy and increment usage count for an edge."""
        start = edge.start
        end = edge.end
        z = start.z

        if z == LAYER_SURFACE:
            pos = start.pos
            self.layer_occupancy[LAYER_SURFACE][pos] = net_name
            if start.lane != LANE_BOTH:
                lanes = self.surface_lanes.setdefault(pos, {})
                lanes[start.lane] = net_name
            key = (start.x, start.y, 0)
            self.usage_count[key] = self.usage_count.get(key, 0) + 1
        elif edge.move_type == "leap":
            dx = 1 if end.x > start.x else (-1 if end.x < start.x else 0)
            dy = 1 if end.y > start.y else (-1 if end.y < start.y else 0)
            dist = max(abs(end.x - start.x), abs(end.y - start.y))
            for s in range(dist + 1):
                pt = (start.x + dx * s, start.y + dy * s)
                self.layer_occupancy[z][pt] = net_name
                key = (pt[0], pt[1], z)
                self.usage_count[key] = self.usage_count.get(key, 0) + 1

    def _unmark_edge(self, edge: RouteEdge, net_name: str) -> None:
        """Release occupancy and decrement usage count for an edge."""
        start = edge.start
        end = edge.end
        z = start.z

        if z == LAYER_SURFACE:
            pos = start.pos
            if self.layer_occupancy[LAYER_SURFACE].get(pos) == net_name:
                del self.layer_occupancy[LAYER_SURFACE][pos]
            if pos in self.surface_lanes:
                self.surface_lanes[pos].pop(start.lane, None)
                if not self.surface_lanes[pos]:
                    del self.surface_lanes[pos]
            key = (start.x, start.y, 0)
            if key in self.usage_count:
                self.usage_count[key] = max(0, self.usage_count[key] - 1)
        elif edge.move_type == "leap":
            dx = 1 if end.x > start.x else (-1 if end.x < start.x else 0)
            dy = 1 if end.y > start.y else (-1 if end.y < start.y else 0)
            dist = max(abs(end.x - start.x), abs(end.y - start.y))
            for s in range(dist + 1):
                pt = (start.x + dx * s, start.y + dy * s)
                if self.layer_occupancy[z].get(pt) == net_name:
                    del self.layer_occupancy[z][pt]
                key = (pt[0], pt[1], z)
                if key in self.usage_count:
                    self.usage_count[key] = max(0, self.usage_count[key] - 1)

    def update_congestion(self, beta: float = 0.5, gamma: float = 1.5) -> int:
        """Update historical and present congestion costs across all nodes.

        Returns total number of overused nodes.
        """
        conflicts = 0
        for node_key, usage in self.usage_count.items():
            capacity = 1
            if usage > capacity:
                overuse = usage - capacity
                conflicts += overuse
                self.historical_cost[node_key] = (
                    self.historical_cost.get(node_key, 0.0) + beta * overuse
                )
                self.present_cost[node_key] = 1.0 + gamma * overuse
            else:
                self.present_cost[node_key] = 1.0
        return conflicts
