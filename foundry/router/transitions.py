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

TURN_PENALTY = 3.5
HOOD_PENALTY = 4.0
STEP_COST = 1.0
UNDERGROUND_STEP_COST = 1.0


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

    if not grid.can_occupy_surface((nxt_x, nxt_y), net.name, node.lane, heading=node.heading):
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
    dx, dy = DIR_STEPS[node.heading]
    nxt_x = node.x + dx
    nxt_y = node.y + dy

    if not grid.is_in_bounds(nxt_x, nxt_y):
        return

    turn_headings = [
        (node.heading + 4) % 16,
        (node.heading + 12) % 16,
    ]

    tier_name = "pipe" if net.is_fluid else "transport-belt"
    for new_h in turn_headings:
        if not grid.can_occupy_surface((nxt_x, nxt_y), net.name, node.lane, heading=new_h):
            continue
        nxt_node = RouteNode(nxt_x, nxt_y, LAYER_SURFACE, new_h, node.lane)
        cost = grid.get_node_cost(nxt_node, STEP_COST + TURN_PENALTY)
        transitions.append((nxt_node, cost, "turn", tier_name))


def _add_underground_dives(
    node: RouteNode,
    grid: RoutingGrid,
    net: RouterNetSpec,
    transitions: list[tuple[RouteNode, float, str, str]],
) -> None:
    """Generate transitions from the surface to subterranean layers."""
    dx, dy = DIR_STEPS[node.heading]
    target_layers = grid.pipe_tier_keys if net.is_fluid else grid.belt_tier_keys
    if not target_layers:
        return

    max_ug_span = grid.max_pipe_span if net.is_fluid else grid.max_belt_span
    if not grid.should_consider_underground(
        node.x, node.y, dx, dy, max_ug_span, net.name, node.lane, target_pos=net.sink
    ):
        return

    if not grid.can_place_hood((node.x, node.y), net.name, heading=node.heading, is_output=False):
        return

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
    is_horiz = dx != 0
    occ = grid.layer_occupancy.get(node.z, {})
    axis_spans = grid.active_horiz_spans if is_horiz else grid.active_vert_spans
    tier_name = (
        grid.pipe_tiers.get(node.z, {}).get("name", "pipe-to-ground")
        if net.is_fluid
        else grid.belt_tiers.get(node.z, {}).get("name", "underground-belt")
    )

    for step in range(1, max_span + 1):
        exit_x = node.x + dx * step
        exit_y = node.y + dy * step

        if not (grid.min_x <= exit_x <= grid.max_x and grid.min_y <= exit_y <= grid.max_y):
            break
        if grid.active_bbox is not None:
            bx0, by0, bx1, by1 = grid.active_bbox
            if not (bx0 <= exit_x <= bx1 and by0 <= exit_y <= by1):
                break

        pt = (exit_x, exit_y)

        # Check subterranean layer clearance on this tile
        existing = occ.get(pt)
        if existing is not None and existing != net.name:
            break

        # Check span collision for intermediate tile
        if step > 1:
            prev_pt = (node.x + dx * (step - 1), node.y + dy * (step - 1))
            if prev_pt in grid.active_hoods and grid.active_hoods[prev_pt] != net.name:
                break
            if prev_pt in axis_spans and axis_spans[prev_pt] != net.name:
                break

        if not grid.can_place_hood(pt, net.name, heading=node.heading, is_output=True):
            continue

        if grid.can_occupy_surface(pt, net.name, node.lane, heading=node.heading, is_hood=True):
            exit_node = RouteNode(exit_x, exit_y, LAYER_SURFACE, node.heading, node.lane)
            base_cost = step * UNDERGROUND_STEP_COST + HOOD_PENALTY
            cost = grid.get_node_cost(exit_node, base_cost)

            transitions.append((exit_node, cost, "leap", tier_name))
