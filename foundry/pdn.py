"""Power Distribution Network (PDN) synthesis.

Generates electric pole positions using greedy set cover and connects them
into a minimum spanning tree with maximum wire span of 9 tiles.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

try:
    from foundry.entity_helpers import (
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        check_power,
        entity_collision_half_size,
    )
except ImportError:
    from entity_helpers import (
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        check_power,
        entity_collision_half_size,
    )


def generate_pdn(
    entities: list[dict[str, Any]],
    occupied_positions: set[tuple[float, float]],
    pin_tiles: set[tuple[float, float]],
    valid_input_pins: set[tuple[float, float]],
    valid_output_pins: set[tuple[float, float]],
) -> tuple[
    dict[tuple[float, float], dict[str, Any]], list[tuple[tuple[float, float], tuple[float, float]]]
]:
    """Synthesize electric pole layout and wire connections."""
    mst_connections: list[tuple[tuple[float, float], tuple[float, float]]] = []
    pole_entities: dict[tuple[float, float], dict[str, Any]] = {}

    powered = [
        e
        for e in entities
        if any(
            k in e.get("name", "")
            for k in (
                "assembling-machine",
                "electric-furnace",
                "chemical-plant",
                "oil-refinery",
                "centrifuge",
                "rocket-silo",
                "inserter",
                "pump",
                "combinator",
                "lab",
                "radar",
            )
        )
    ]
    if not powered:
        return pole_entities, mst_connections

    surface_occupied = set(occupied_positions)
    surface_occupied.update(pin_tiles)
    surface_occupied.update(valid_input_pins)
    surface_occupied.update(valid_output_pins)
    for ent in entities:
        ep = ent["position"]
        surface_occupied.add((round(ep["x"], 2), round(ep["y"], 2)))
        ename = ent.get("name", "")
        if ename == "splitter":
            d = ent.get("direction", 0)
            if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                surface_occupied.add((round(ep["x"] + 1.0, 2), round(ep["y"], 2)))
            else:
                surface_occupied.add((round(ep["x"], 2), round(ep["y"] + 1.0, 2)))

    xs = [e["position"]["x"] for e in powered]
    ys = [e["position"]["y"] for e in powered]
    min_x, max_x = int(min(xs)) - 1, int(max(xs)) + 4
    min_y, max_y = int(min(ys)) - 1, int(max(ys)) + 4
    cands: list[tuple[float, float]] = []
    for x in range(min_x, max_x, 6):
        for y in range(min_y, max_y, 6):
            cand = (math.floor(x) + 0.5, math.floor(y) + 0.5)
            if cand in surface_occupied:
                for dx in (0, 1, -1, 2, -2, 3, -3):
                    for dy in (0, 1, -1, 2, -2, 3, -3):
                        c2 = (cand[0] + dx, cand[1] + dy)
                        if c2 not in surface_occupied:
                            cand = c2
                            break
                    if cand not in surface_occupied:
                        break
            if cand not in surface_occupied:
                cands.append(cand)

    def is_covered(ent: dict[str, Any], cand_pos: tuple[float, float]) -> bool:
        ex = ent["position"]["x"]
        ey = ent["position"]["y"]
        cx, cy = entity_collision_half_size(ent.get("name", ""))
        return (
            max(
                max(0.0, abs(ex - cand_pos[0]) - cx),
                max(0.0, abs(ey - cand_pos[1]) - cy),
            )
            <= 3.5
        )

    uncovered = set(range(len(powered)))
    chosen: set[tuple[float, float]] = set()
    while uncovered:
        best_cand = None
        best_covers: set[int] = set()
        for c in cands:
            if c in chosen:
                continue
            covers = {i for i in uncovered if is_covered(powered[i], c)}
            if len(covers) > len(best_covers):
                best_covers = covers
                best_cand = c
        if not best_cand:
            break
        chosen.add(best_cand)
        uncovered -= best_covers

    for i in list(uncovered):
        px, py = powered[i]["position"]["x"], powered[i]["position"]["y"]
        cx, cy = entity_collision_half_size(powered[i].get("name", ""))
        reach = math.ceil(3.5 + max(cx, cy))
        found_cand = False
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                cand = (math.floor(px + dx) + 0.5, math.floor(py + dy) + 0.5)
                if (
                    cand not in surface_occupied
                    and cand not in chosen
                    and is_covered(powered[i], cand)
                ):
                    chosen.add(cand)
                    cands.append(cand)
                    uncovered.remove(i)
                    found_cand = True
                    break
            if found_cand:
                break

    # Connect chosen poles through Steiner MST with maximum wire span 9.0 tiles
    cand_list = list(cands)
    cand_adj: dict[tuple[float, float], list[tuple[float, float]]] = {c: [] for c in cand_list}
    for i in range(len(cand_list)):
        for j in range(i + 1, len(cand_list)):
            if (
                math.hypot(cand_list[i][0] - cand_list[j][0], cand_list[i][1] - cand_list[j][1])
                <= 9.0
            ):
                cand_adj[cand_list[i]].append(cand_list[j])
                cand_adj[cand_list[j]].append(cand_list[i])

    all_poles = set(chosen)
    if chosen:
        connected = {next(iter(chosen))}
        unconnected = set(chosen) - connected

        while unconnected:
            dist: dict[tuple[float, float], int] = dict.fromkeys(connected, 0)
            parent: dict[tuple[float, float], tuple[float, float]] = {}
            pq: list[tuple[int, tuple[float, float]]] = [(0, p) for p in connected]
            target = None
            while pq:
                d, u = heapq.heappop(pq)
                if u in unconnected:
                    target = u
                    break
                if d > dist.get(u, 1_000_000_000):
                    continue
                for v in cand_adj.get(u, []):
                    if d + 1 < dist.get(v, 1_000_000_000):
                        dist[v] = d + 1
                        parent[v] = u
                        heapq.heappush(pq, (d + 1, v))
            if not target:
                break
            curr = target
            while curr not in connected:
                all_poles.add(curr)
                connected.add(curr)
                curr = parent[curr]
            unconnected.remove(target)

    for p in sorted(all_poles):
        pole_ent = {
            "name": "medium-electric-pole",
            "position": {"x": p[0], "y": p[1]},
        }
        entities.append(pole_ent)
        pole_entities[p] = pole_ent
        surface_occupied.add(p)

    pole_list = sorted(all_poles)
    pole_edges: list[tuple[float, int, int]] = []
    for i in range(len(pole_list)):
        for j in range(i + 1, len(pole_list)):
            d = math.hypot(
                pole_list[i][0] - pole_list[j][0],
                pole_list[i][1] - pole_list[j][1],
            )
            if d <= 9.0:
                pole_edges.append((d, i, j))
    pole_edges.sort()
    parent_mst = list(range(len(pole_list)))

    def find_root(node: int) -> int:
        if parent_mst[node] == node:
            return node
        parent_mst[node] = find_root(parent_mst[node])
        return parent_mst[node]

    for _d, u, v in pole_edges:
        ru, rv = find_root(u), find_root(v)
        if ru != rv:
            parent_mst[ru] = rv
            mst_connections.append((pole_list[u], pole_list[v]))

    unpowered_ents = check_power(entities)
    if unpowered_ents:
        raise RuntimeError(
            f"Power check failed: {len(unpowered_ents)} entities lack electric pole coverage: {unpowered_ents[:5]}"
        )

    return pole_entities, mst_connections
