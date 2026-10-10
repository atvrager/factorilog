"""Power Distribution Network (PDN) synthesis.

Generates electric pole positions using greedy set cover and connects them
into minimum spanning trees with maximum wire span of 9 tiles.
Supports multi-domain power gating when power switches are present.
"""

from __future__ import annotations

import heapq
import math
import sys
from typing import Any

try:
    from foundry.entity_helpers import (
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        check_power,
        entity_collision_half_size,
        entity_size,
    )
except ImportError:
    from entity_helpers import (
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        check_power,
        entity_collision_half_size,
        entity_size,
    )


def is_aon_entity(ent: dict[str, Any]) -> bool:
    """Return True if entity belongs to always-on power domain."""
    name = ent.get("name", "")
    return "combinator" in name or "display-panel" in name or ent.get("power_domain") == "aon"


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

    power_switch_ents = [e for e in entities if e.get("name") == "power-switch"]

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
                "display-panel",
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
        ename = ent.get("name", "")
        w, h = entity_size(ename)
        ex, ey = ep["x"], ep["y"]
        min_tx = math.floor(ex - w / 2.0 - 1e-6)
        max_tx = math.floor(ex + w / 2.0 - 1e-6)
        min_ty = math.floor(ey - h / 2.0 - 1e-6)
        max_ty = math.floor(ey + h / 2.0 - 1e-6)
        for tx in range(min_tx, max_tx + 2):
            for ty in range(min_ty, max_ty + 2):
                if max(ex - w / 2.0, tx) < min(ex + w / 2.0, tx + 1.0) and max(
                    ey - h / 2.0, ty
                ) < min(ey + h / 2.0, ty + 1.0):
                    surface_occupied.add((round(tx + 0.5, 2), round(ty + 0.5, 2)))
        surface_occupied.add((round(ex, 2), round(ey, 2)))
        if ename == "splitter":
            d = ent.get("direction", 0)
            if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                surface_occupied.add((round(ep["x"] + 1.0, 2), round(ep["y"], 2)))
            else:
                surface_occupied.add((round(ep["x"], 2), round(ep["y"] + 1.0, 2)))

    all_positioned = powered + power_switch_ents
    xs = [e["position"]["x"] for e in all_positioned]
    ys = [e["position"]["y"] for e in all_positioned]
    min_x, max_x = int(min(xs)) - 12, int(max(xs)) + 12
    min_y, max_y = int(min(ys)) - 12, int(max(ys)) + 12

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

    def find_or_place_terminal(
        target_pos: tuple[float, float],
        existing_poles: set[tuple[float, float]],
        forbidden_entities: list[dict[str, Any]],
    ) -> tuple[float, float]:
        close = [
            p
            for p in existing_poles
            if math.hypot(p[0] - target_pos[0], p[1] - target_pos[1]) <= 8.5
        ]
        if close:
            return min(
                close,
                key=lambda p: math.hypot(p[0] - target_pos[0], p[1] - target_pos[1]),
            )
        for r in range(1, 9):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if math.hypot(dx, dy) > 8.0:
                        continue
                    cand = (
                        math.floor(target_pos[0] + dx) + 0.5,
                        math.floor(target_pos[1] + dy) + 0.5,
                    )
                    if cand in surface_occupied:
                        continue
                    if any(is_covered(fe, cand) for fe in forbidden_entities):
                        continue
                    return cand
        raise RuntimeError(f"Could not place power switch terminal near {target_pos}")

    def _solve_domain(
        domain_name: str,
        domain_powered: list[dict[str, Any]],
        forbidden_entities: list[dict[str, Any]],
        extra_terminals: list[tuple[float, float]],
    ) -> tuple[set[tuple[float, float]], list[tuple[tuple[float, float], tuple[float, float]]]]:
        if not domain_powered and not extra_terminals:
            return set(), []

        valid_cands = [
            c
            for c in cands
            if c not in surface_occupied and not any(is_covered(fe, c) for fe in forbidden_entities)
        ]

        uncovered = set(range(len(domain_powered)))
        chosen: set[tuple[float, float]] = set()

        while uncovered:
            best_cand = None
            best_covers: set[int] = set()
            for c in valid_cands:
                if c in chosen:
                    continue
                covers = {i for i in uncovered if is_covered(domain_powered[i], c)}
                if len(covers) > len(best_covers):
                    best_covers = covers
                    best_cand = c
            if not best_cand:
                break
            chosen.add(best_cand)
            uncovered -= best_covers

        for i in list(uncovered):
            px, py = domain_powered[i]["position"]["x"], domain_powered[i]["position"]["y"]
            cx, cy = entity_collision_half_size(domain_powered[i].get("name", ""))
            reach = math.ceil(3.5 + max(cx, cy))
            found_cand = False
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    cand = (math.floor(px + dx) + 0.5, math.floor(py + dy) + 0.5)
                    if not is_covered(domain_powered[i], cand):
                        continue
                    if cand in surface_occupied or cand in chosen:
                        continue
                    if any(is_covered(fe, cand) for fe in forbidden_entities):
                        continue
                    chosen.add(cand)
                    if cand not in valid_cands:
                        valid_cands.append(cand)
                    uncovered.remove(i)
                    found_cand = True
                    break
                if found_cand:
                    break
            if not found_cand:
                sys.stderr.write(
                    f"UNCOVERED {domain_name} {domain_powered[i].get('name')} at ({px}, {py})\n"
                )

        for term in extra_terminals:
            chosen.add(term)
            if term not in valid_cands:
                valid_cands.append(term)

        cand_list = list(valid_cands)
        cand_adj: dict[tuple[float, float], list[tuple[float, float]]] = {c: [] for c in cand_list}
        for i in range(len(cand_list)):
            for j in range(i + 1, len(cand_list)):
                if (
                    math.hypot(
                        cand_list[i][0] - cand_list[j][0],
                        cand_list[i][1] - cand_list[j][1],
                    )
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

        domain_mst: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for _d, u, v in pole_edges:
            ru, rv = find_root(u), find_root(v)
            if ru != rv:
                parent_mst[ru] = rv
                domain_mst.append((pole_list[u], pole_list[v]))

        return all_poles, domain_mst

    if not power_switch_ents:
        all_poles, mst_connections = _solve_domain("default", powered, [], [])
        for p in sorted(all_poles):
            pole_ent = {
                "name": "medium-electric-pole",
                "position": {"x": p[0], "y": p[1]},
                "power_domain": "default",
            }
            entities.append(pole_ent)
            pole_entities[p] = pole_ent
            surface_occupied.add(p)
    else:
        aon_powered = [e for e in powered if is_aon_entity(e)]
        switched_powered = [e for e in powered if not is_aon_entity(e)]

        aon_terminals: list[tuple[float, float]] = []
        switched_terminals: list[tuple[float, float]] = []
        for ps in power_switch_ents:
            ps_pos = (ps["position"]["x"], ps["position"]["y"])
            p_aon = find_or_place_terminal(ps_pos, set(), switched_powered)
            aon_terminals.append(p_aon)
            surface_occupied.add(p_aon)

            p_sw = find_or_place_terminal(ps_pos, set(), aon_powered)
            switched_terminals.append(p_sw)
            surface_occupied.add(p_sw)

        aon_poles, aon_mst = _solve_domain("aon", aon_powered, switched_powered, aon_terminals)
        surface_occupied.update(aon_poles)

        switched_poles, switched_mst = _solve_domain(
            "switched", switched_powered, aon_powered, switched_terminals
        )
        surface_occupied.update(switched_poles)

        for p in sorted(aon_poles):
            pole_ent = {
                "name": "medium-electric-pole",
                "position": {"x": p[0], "y": p[1]},
                "power_domain": "aon",
            }
            entities.append(pole_ent)
            pole_entities[p] = pole_ent

        for p in sorted(switched_poles):
            pole_ent = {
                "name": "medium-electric-pole",
                "position": {"x": p[0], "y": p[1]},
                "power_domain": "switched",
            }
            entities.append(pole_ent)
            pole_entities[p] = pole_ent

        mst_connections = aon_mst + switched_mst

        for p in aon_poles:
            for se in switched_powered:
                if is_covered(se, p):
                    raise RuntimeError(
                        f"Power domain leak: aon pole at {p} covers switched entity {se.get('name')}"
                    )
        for p in switched_poles:
            for ae in aon_powered:
                if is_covered(ae, p):
                    raise RuntimeError(
                        f"Power domain leak: switched pole at {p} covers aon entity {ae.get('name')}"
                    )

    unpowered_ents = check_power(entities)
    if unpowered_ents:
        raise RuntimeError(
            f"Power check failed: {len(unpowered_ents)} entities lack electric pole coverage: {unpowered_ents[:5]}"
        )

    return pole_entities, mst_connections
