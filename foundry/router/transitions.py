"""Canonical transition move generator for the 3D routing graph.

Generates legal moves for straight runs, turns, dives, leaps, and surfaces.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from foundry.router.grid import RoutingGrid
from foundry.router.types import (
    DIR_STEPS,
    LAYER_SURFACE,
    RouteNode,
    RouterNetSpec,
)

TURN_PENALTY = 2.0
HOOD_PENALTY = 4.0
STEP_COST = 1.0
UNDERGROUND_STEP_COST = 0.5


def get_legal_transitions(
    node: RouteNode,
    grid: RoutingGrid,
    net: RouterNetSpec,
) -> list[tuple[RouteNode, float, str, str]]:
    """Return all valid next states, costs, move types, and tier names.

    Returns tuples: (next_node, transition_cost, move_type, tier_name).
    """
    transitions: list[tuple[RouteNode, float, str, str]] = []

    if node.z == LAYER_SURFACE:
        _add_surface_straight(node, grid, net, transitions)
        _add_surface_turns(node, grid, net, transitions)
        _add_underground_dives(node, grid, net, transitions)
    else:
        _add_subterranean_leaps(node, grid, net, transitions)

    return transitions


def _add_surface_straight(
    node: RouteNode,
    grid: RoutingGrid,
    net: RouterNetSpec,
    transitions: list[tuple[RouteNode, float, str, str]],
) -> None:
    """Generate forward straight movement on the surface."""
    dx, dy = DIR_STEPS[node.heading]
    nxt_x = node.x + dx
    nxt_y = node.y + dy

    if not grid.is_in_bounds(nxt_x, nxt_y):
        return

    if not grid.can_occupy_surface((nxt_x, nxt_y), net.name, node.lane):
        return

    nxt_node = RouteNode(nxt_x, nxt_y, LAYER_SURFACE, node.heading, node.lane)
    cost = grid.get_node_cost(nxt_node, STEP_COST)
    tier_name = "pipe" if net.is_fluid else "transport-belt"
    transitions.append((nxt_node, cost, "straight", tier_name))


def _add_surface_turns(
    node: RouteNode,
    grid: RoutingGrid,
    net: RouterNetSpec,
    transitions: list[tuple[RouteNode, float, str, str]],
) -> None:
    """Generate orthogonal 90-degree turns on the surface."""
    turn_headings = [
        (node.heading + 4) % 16,
        (node.heading + 12) % 16,
    ]

    for new_h in turn_headings:
        dx, dy = DIR_STEPS[new_h]
        nxt_x = node.x + dx
        nxt_y = node.y + dy

        if not grid.is_in_bounds(nxt_x, nxt_y):
            continue

        if not grid.can_occupy_surface((nxt_x, nxt_y), net.name, node.lane):
            continue

        nxt_node = RouteNode(nxt_x, nxt_y, LAYER_SURFACE, new_h, node.lane)
        cost = grid.get_node_cost(nxt_node, STEP_COST + TURN_PENALTY)
        tier_name = "pipe" if net.is_fluid else "transport-belt"
        transitions.append((nxt_node, cost, "turn", tier_name))


def _add_underground_dives(
    node: RouteNode,
    grid: RoutingGrid,
    net: RouterNetSpec,
    transitions: list[tuple[RouteNode, float, str, str]],
) -> None:
    """Generate transitions from the surface to subterranean layers."""
    target_layers = list(grid.pipe_tiers.keys()) if net.is_fluid else list(grid.belt_tiers.keys())

    for target_z in target_layers:
        tier_cost = grid.get_tier_cost(target_z)
        dive_node = RouteNode(node.x, node.y, target_z, node.heading, node.lane)
        base_cost = HOOD_PENALTY + tier_cost
        cost = grid.get_node_cost(dive_node, base_cost)
        if net.is_fluid:
            tier_name = grid.pipe_tiers[target_z]["name"]
        else:
            tier_name = grid.belt_tiers[target_z]["name"]
        transitions.append((dive_node, cost, "dive", tier_name))


def _add_subterranean_leaps(
    node: RouteNode,
    grid: RoutingGrid,
    net: RouterNetSpec,
    transitions: list[tuple[RouteNode, float, str, str]],
) -> None:
    """Generate leaps across subterranean spans to surface exits."""
    dx, dy = DIR_STEPS[node.heading]
    max_span = grid.get_tier_span(node.z)

    for step in range(1, max_span + 1):
        exit_x = node.x + dx * step
        exit_y = node.y + dy * step

        if not grid.is_in_bounds(exit_x, exit_y):
            break

        # Check subterranean layer clearance
        span_coords = [(node.x + dx * s, node.y + dy * s) for s in range(1, step + 1)]
        if not grid.can_occupy_subterranean(node.z, span_coords, net.name):
            # Same-tier collision stops further underground leaps
            break

        # Check surface exit legality
        if grid.can_occupy_surface((exit_x, exit_y), net.name, node.lane):
            exit_node = RouteNode(exit_x, exit_y, LAYER_SURFACE, node.heading, node.lane)
            base_cost = step * UNDERGROUND_STEP_COST + HOOD_PENALTY
            cost = grid.get_node_cost(exit_node, base_cost)
            if net.is_fluid:
                tier_name = grid.pipe_tiers.get(node.z, {}).get("name", "pipe-to-ground")
            else:
                tier_name = grid.belt_tiers.get(node.z, {}).get("name", "underground-belt")
            transitions.append((exit_node, cost, "leap", tier_name))
