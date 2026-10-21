"""Blueprint builder to convert routed graph paths into Factorio entities.

Assembles physical transport belts, underground pairs, and pipes.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from foundry.router.types import (
    OPPOSITE_DIR,
    TILE_CENTER_OFFSET,
    EntityDict,
    RouteEdge,
    RouteResult,
    RouterNetSpec,
)


def _make_tile_entity(
    name: str,
    pos: tuple[int, int],
    direction: int,
    ug_type: str | None = None,
) -> EntityDict:
    """Create a blueprint entity dictionary centered at tile pos."""
    ent: EntityDict = {
        "name": name,
        "position": {
            "x": pos[0] + TILE_CENTER_OFFSET,
            "y": pos[1] + TILE_CENTER_OFFSET,
        },
        "direction": direction,
    }
    if ug_type is not None:
        ent["type"] = ug_type
    return ent


def _is_protected_hood_tile(existing: EntityDict | None) -> bool:
    """Return True if existing entity is an underground output hood or pipe-to-ground."""
    if existing is None:
        return False
    return existing.get("type") == "output" or "pipe-to-ground" in existing.get("name", "")


def _place_route_edge(
    edge: RouteEdge,
    is_fluid: bool,
    entities_by_pos: dict[tuple[int, int], EntityDict],
) -> None:
    """Convert one RouteEdge into a physical tile entity in entities_by_pos."""
    start = edge.start
    end = edge.end
    move = edge.move_type

    if move in ("straight", "turn"):
        pos = (start.x, start.y)
        if _is_protected_hood_tile(entities_by_pos.get(pos)):
            return
        entities_by_pos[pos] = _make_tile_entity(edge.tier_name, pos, start.heading)
        return

    if move == "dive":
        pos = (end.x, end.y)
        direction = OPPOSITE_DIR.get(start.heading, 0) if is_fluid else start.heading
        ug_type = None if is_fluid else "input"
        entities_by_pos[pos] = _make_tile_entity(edge.tier_name, pos, direction, ug_type=ug_type)
        return

    if move == "leap":
        pos = (end.x, end.y)
        ug_type = None if is_fluid else "output"
        entities_by_pos[pos] = _make_tile_entity(edge.tier_name, pos, end.heading, ug_type=ug_type)


def _place_single_route(
    res: RouteResult,
    is_fluid: bool,
    entities_by_pos: dict[tuple[int, int], EntityDict],
) -> None:
    """Populate entities_by_pos with the entities along a single routed path."""
    surface_name = "pipe" if is_fluid else "transport-belt"
    if not res.edges:
        goal = res.nodes[0]
        pos = (goal.x, goal.y)
        if pos not in entities_by_pos:
            entities_by_pos[pos] = _make_tile_entity(surface_name, pos, goal.heading)
        return

    for edge in res.edges:
        _place_route_edge(edge, is_fluid, entities_by_pos)

    if res.edges[-1].move_type != "leap":
        goal = res.nodes[-1]
        pos = (goal.x, goal.y)
        if pos not in entities_by_pos:
            entities_by_pos[pos] = _make_tile_entity(surface_name, pos, goal.heading)


def build_blueprint_entities(
    routes: Mapping[str, RouteResult],
    net_specs: Sequence[RouterNetSpec] | None = None,
) -> list[EntityDict]:
    """Convert routed paths into a list of Factorio blueprint entities."""
    net_map: dict[str, RouterNetSpec] = {net.name: net for net in net_specs} if net_specs else {}
    entities_by_pos: dict[tuple[int, int], EntityDict] = {}

    for net_name, res in routes.items():
        if not res.is_success or not res.nodes:
            continue
        net = net_map.get(net_name)
        is_fluid = net.is_fluid if net is not None else False
        _place_single_route(res, is_fluid, entities_by_pos)

    entity_list: list[EntityDict] = []
    for idx, ent in enumerate(entities_by_pos.values(), start=1):
        ent["entity_number"] = idx
        entity_list.append(ent)
    return entity_list
