"""Multi-layer A* maze router with negotiated congestion.

Implements iterative rip-up and reroute across the 3D graph.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import heapq

from foundry.router.grid import RoutingGrid
from foundry.router.transitions import get_legal_transitions
from foundry.router.types import (
    ALL_DIRECTIONS,
    LAYER_SURFACE,
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
)


def heuristic(node: RouteNode, target: tuple[int, int]) -> float:
    """Calculate Manhattan distance heuristic to the target tile."""
    dist = abs(node.x - target[0]) + abs(node.y - target[1])
    extra = 0.0
    if node.z != LAYER_SURFACE:
        extra += 2.0
    return float(dist) + extra


def route_single_net(
    net: RouterNetSpec,
    grid: RoutingGrid,
) -> RouteResult:
    """Find a minimum-cost directed path for one net using A* search."""
    if net.source == net.sink:
        heading = net.start_heading if net.start_heading is not None else 0
        node = RouteNode(net.source[0], net.source[1], LAYER_SURFACE, heading, net.lane)
        return RouteResult(net.name, [node], [], 0.0, is_success=True)

    # Initialize start states
    start_headings = [net.start_heading] if net.start_heading is not None else list(ALL_DIRECTIONS)

    open_set: list[tuple[float, int, RouteNode]] = []
    g_score: dict[RouteNode, float] = {}
    came_from: dict[RouteNode, tuple[RouteNode, RouteEdge]] = {}

    tie_counter = 0

    for h in start_headings:
        # Check source tile occupancy
        if not grid.can_occupy_surface(net.source, net.name, net.lane):
            continue
        start_node = RouteNode(net.source[0], net.source[1], LAYER_SURFACE, h, net.lane)
        base_start_cost = grid.get_node_cost(start_node, 0.0)
        g_score[start_node] = base_start_cost
        f_score = base_start_cost + heuristic(start_node, net.sink)
        tie_counter += 1
        heapq.heappush(open_set, (f_score, tie_counter, start_node))

    goal_node: RouteNode | None = None

    while open_set:
        _, _, current = heapq.heappop(open_set)

        # Check goal condition
        if (
            current.pos == net.sink
            and current.z == LAYER_SURFACE
            and (net.end_heading is None or current.heading == net.end_heading)
        ):
            goal_node = current
            break

        curr_g = g_score[current]

        for nxt_node, edge_cost, move_type, tier_name in get_legal_transitions(current, grid, net):
            tentative_g = curr_g + edge_cost
            if nxt_node not in g_score or tentative_g < g_score[nxt_node]:
                g_score[nxt_node] = tentative_g
                edge = RouteEdge(current, nxt_node, move_type, tier_name, edge_cost)
                came_from[nxt_node] = (current, edge)
                f_score = tentative_g + heuristic(nxt_node, net.sink)
                tie_counter += 1
                heapq.heappush(open_set, (f_score, tie_counter, nxt_node))

    if goal_node is None:
        return RouteResult(
            net_name=net.name,
            nodes=[],
            edges=[],
            cost=float("inf"),
            is_success=False,
            error_message=f"No route found from {net.source} to {net.sink}.",
        )

    # Reconstruct path
    nodes: list[RouteNode] = [goal_node]
    edges: list[RouteEdge] = []
    curr = goal_node

    while curr in came_from:
        prev_node, edge = came_from[curr]
        edges.append(edge)
        nodes.append(prev_node)
        curr = prev_node

    nodes.reverse()
    edges.reverse()

    return RouteResult(
        net_name=net.name,
        nodes=nodes,
        edges=edges,
        cost=g_score[goal_node],
        is_success=True,
    )


def route_all_nets(
    nets: list[RouterNetSpec],
    grid: RoutingGrid,
    max_iterations: int = 30,
) -> dict[str, RouteResult]:
    """Route all nets with negotiated congestion rip-up and reroute."""
    # Initial sequential route
    for net in nets:
        res = route_single_net(net, grid)
        grid.net_routes[net.name] = res
        if not res.is_success:
            return grid.net_routes
        grid.place_net(net.name, res)

    conflicts = grid.update_congestion()
    if conflicts == 0:
        return grid.net_routes

    # Negotiated congestion iterations
    for _ in range(max_iterations):
        for net in nets:
            grid.rip_up_net(net.name)
            res = route_single_net(net, grid)
            grid.net_routes[net.name] = res
            if not res.is_success:
                return grid.net_routes
            grid.place_net(net.name, res)

        conflicts = grid.update_congestion()
        if conflicts == 0:
            break

    return grid.net_routes
