"""Blueprint builder to convert routed graph paths into Factorio entities.

Assembles physical transport belts, underground pairs, and pipes.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from typing import Any

from foundry.router.types import (
    OPPOSITE_DIR,
    RouteResult,
    RouterNetSpec,
)


def build_blueprint_entities(
    routes: dict[str, RouteResult],
    net_specs: list[RouterNetSpec] | None = None,
) -> list[dict[str, Any]]:
    """Convert routed paths into a list of Factorio blueprint entities."""
    net_map: dict[str, RouterNetSpec] = {}
    if net_specs:
        for net in net_specs:
            net_map[net.name] = net

    entities_by_pos: dict[tuple[int, int], dict[str, Any]] = {}

    for net_name, res in routes.items():
        if not res.is_success or not res.edges:
            continue

        net = net_map.get(net_name)
        is_fluid = net.is_fluid if net else False

        for edge in res.edges:
            start = edge.start
            end = edge.end
            move = edge.move_type

            if move in ("straight", "turn"):
                pos = (start.x, start.y)
                if pos in entities_by_pos:
                    existing = entities_by_pos[pos]
                    if existing.get("type") == "output" or "pipe-to-ground" in existing.get(
                        "name", ""
                    ):
                        continue
                heading = end.heading if move == "turn" else start.heading
                name = edge.tier_name
                entities_by_pos[pos] = {
                    "name": name,
                    "position": {"x": pos[0] + 0.5, "y": pos[1] + 0.5},
                    "direction": heading,
                }
            elif move == "dive":
                # Entrance hood
                pos = (start.x, start.y)
                name = edge.tier_name
                ent: dict[str, Any] = {
                    "name": name,
                    "position": {"x": pos[0] + 0.5, "y": pos[1] + 0.5},
                    "direction": start.heading,
                }
                if not is_fluid:
                    ent["type"] = "input"
                entities_by_pos[pos] = ent
            elif move == "leap":
                # Exit hood
                pos = (end.x, end.y)
                name = edge.tier_name
                ent = {
                    "name": name,
                    "position": {"x": pos[0] + 0.5, "y": pos[1] + 0.5},
                }
                if is_fluid:
                    # Pipe to ground faces toward entrance
                    ent["direction"] = OPPOSITE_DIR.get(end.heading, 0)
                else:
                    ent["direction"] = end.heading
                    ent["type"] = "output"
                entities_by_pos[pos] = ent

        # Check final node placement
        last_edge = res.edges[-1]
        if last_edge.move_type not in ("leap",):
            goal = res.nodes[-1]
            pos = (goal.x, goal.y)
            if pos not in entities_by_pos:
                name = "pipe" if is_fluid else "transport-belt"
                entities_by_pos[pos] = {
                    "name": name,
                    "position": {"x": pos[0] + 0.5, "y": pos[1] + 0.5},
                    "direction": goal.heading,
                }

    # Assign entity_number indices
    entity_list: list[dict[str, Any]] = []
    idx = 1
    for ent in entities_by_pos.values():
        ent["entity_number"] = idx
        entity_list.append(ent)
        idx += 1

    return entity_list
