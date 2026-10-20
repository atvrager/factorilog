"""Power Distribution Network (PDN) synthesis.

Generates electric pole positions using greedy set cover and connects them
into minimum spanning trees within the maximum pole wire reach.
Supports multi-domain power gating when power switches are present.
"""

from __future__ import annotations

import collections
import itertools
import math
import sys
from collections.abc import Mapping, MutableSequence, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

try:
    from foundry.entity_helpers import (
        DIRECTION_NORTH,
        TILE_HALF,
        check_power,
        entity_collision_half_size,
        entity_size,
        get_pole_supply_radius,
        get_pole_wire_reach,
    )
except ImportError:
    from entity_helpers import (
        DIRECTION_NORTH,
        TILE_HALF,
        check_power,
        entity_collision_half_size,
        entity_size,
        get_pole_supply_radius,
        get_pole_wire_reach,
    )

JsonMap: TypeAlias = Mapping[str, object]
EntityDict: TypeAlias = dict[str, Any]
GridPos: TypeAlias = tuple[float, float]
MstEdge: TypeAlias = tuple[GridPos, GridPos]

COORD_PRECISION: int = 2
BOX_EPSILON: float = 1e-6
HALF_DIVISOR: float = 2.0
PIN_EXCLUSION_REACH: int = 3
CANDIDATE_MAX_SHIFT: int = 3
TERMINAL_SEARCH_RING_DELTAS: dict[int, tuple[int, ...]] = {
    1: (-1, 0, 1),
    2: (-2, -1, 0, 1, 2),
    3: (-3, -2, -1, 0, 1, 2, 3),
    4: (-4, -3, -2, -1, 0, 1, 2, 3, 4),
    5: (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5),
    6: (-6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6),
    7: (-7, -6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7),
    8: (-8, -7, -6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6, 7, 8),
}
TERMINAL_PLACE_RADIUS: float = 8.0
TERMINAL_SEARCH_DELTAS: tuple[tuple[int, int], ...] = tuple(
    (dx, dy)
    for deltas in TERMINAL_SEARCH_RING_DELTAS.values()
    for dx in deltas
    for dy in deltas
    if math.hypot(dx, dy) <= TERMINAL_PLACE_RADIUS
)

POWERED_KEYWORDS: tuple[str, ...] = (
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
    "steam-engine",
)
AON_KEYWORDS: tuple[str, ...] = ("combinator", "display-panel", "steam-engine")


@dataclass(frozen=True)
class PdnConfig:
    """Configuration parameters for PDN synthesis."""

    pole_name: str = "medium-electric-pole"
    supply_radius: float = get_pole_supply_radius("medium-electric-pole")
    wire_reach: float = get_pole_wire_reach("medium-electric-pole")
    grid_step: int = 6
    bbox_margin: int = 12
    terminal_place_radius: float = 8.0
    terminal_search_steps: int = 8


DEFAULT_PDN_CONFIG = PdnConfig()


def _ent_xy(ent: JsonMap) -> GridPos:
    """Extract (x, y) coordinates from an entity mapping."""
    pos = ent.get("position")
    if isinstance(pos, Mapping):
        return float(pos.get("x", 0.0)), float(pos.get("y", 0.0))
    return 0.0, 0.0


def _ent_dir(ent: JsonMap) -> int:
    """Extract cardinal direction integer from an entity mapping."""
    val = ent.get("direction", DIRECTION_NORTH)
    return int(val) if isinstance(val, (int, float)) else DIRECTION_NORTH


def is_aon_entity(ent: JsonMap) -> bool:
    """Return True if entity belongs to the always-on power domain."""
    name = str(ent.get("name", ""))
    return any(k in name for k in AON_KEYWORDS) or ent.get("power_domain") == "aon"


def _is_covered(ent: JsonMap, cand_pos: GridPos, supply_radius: float) -> bool:
    """Return True if cand_pos supplies electric power to ent."""
    ex, ey = _ent_xy(ent)
    cx, cy = entity_collision_half_size(str(ent.get("name", "")), _ent_dir(ent))
    dx = max(0.0, abs(ex - cand_pos[0]) - cx)
    dy = max(0.0, abs(ey - cand_pos[1]) - cy)
    return max(dx, dy) <= supply_radius


def _add_entity_footprint(surface_occupied: set[GridPos], ent: JsonMap) -> None:
    """Add all tiles covered by ent to surface_occupied."""
    ex, ey = _ent_xy(ent)
    ename = str(ent.get("name", ""))
    direction = _ent_dir(ent)
    half_w, half_h = (dim / HALF_DIVISOR for dim in entity_size(ename, direction))
    min_tx = math.floor(ex - half_w + BOX_EPSILON)
    max_tx = math.floor(ex + half_w - BOX_EPSILON)
    min_ty = math.floor(ey - half_h + BOX_EPSILON)
    max_ty = math.floor(ey + half_h - BOX_EPSILON)
    for tx in range(min_tx, max_tx + 1):
        for ty in range(min_ty, max_ty + 1):
            surface_occupied.add(
                (round(tx + TILE_HALF, COORD_PRECISION), round(ty + TILE_HALF, COORD_PRECISION))
            )
    surface_occupied.add((round(ex, COORD_PRECISION), round(ey, COORD_PRECISION)))


def _build_surface_occupied(
    entities: Sequence[JsonMap],
    occupied_positions: set[GridPos],
    all_pins: set[GridPos],
) -> set[GridPos]:
    """Build the set of occupied surface tiles including pin exclusions and footprints."""
    surface_occupied = set(occupied_positions)
    for px, py in all_pins:
        ix = math.floor(px)
        iy = math.floor(py)
        for tx in range(ix - PIN_EXCLUSION_REACH, ix + PIN_EXCLUSION_REACH + 1):
            surface_occupied.add(
                (round(tx + TILE_HALF, COORD_PRECISION), round(iy + TILE_HALF, COORD_PRECISION))
            )
        for ty in range(iy - PIN_EXCLUSION_REACH, iy + PIN_EXCLUSION_REACH + 1):
            surface_occupied.add(
                (round(ix + TILE_HALF, COORD_PRECISION), round(ty + TILE_HALF, COORD_PRECISION))
            )
    for ent in entities:
        _add_entity_footprint(surface_occupied, ent)
    return surface_occupied


def _find_free_grid_candidate(gx: int, gy: int, surface_occupied: set[GridPos]) -> GridPos | None:
    """Return the first unoccupied tile center within CANDIDATE_MAX_SHIFT of (gx, gy)."""
    for cx in range(gx - CANDIDATE_MAX_SHIFT, gx + CANDIDATE_MAX_SHIFT):
        for cy in range(gy - CANDIDATE_MAX_SHIFT, gy + CANDIDATE_MAX_SHIFT):
            cand = (cx + TILE_HALF, cy + TILE_HALF)
            if cand not in surface_occupied:
                return cand
    return None


def _generate_candidate_poles(
    all_positioned: Sequence[JsonMap],
    surface_occupied: set[GridPos],
    cfg: PdnConfig,
) -> list[GridPos]:
    """Generate candidate pole positions across the bounding box grid."""
    coords = [_ent_xy(e) for e in all_positioned]
    xs = [pt[0] for pt in coords]
    ys = [pt[1] for pt in coords]
    min_x = int(min(xs)) - cfg.bbox_margin
    max_x = int(max(xs)) + cfg.bbox_margin
    min_y = int(min(ys)) - cfg.bbox_margin
    max_y = int(max(ys)) + cfg.bbox_margin
    cands: list[GridPos] = []
    for x in range(min_x, max_x, cfg.grid_step):
        for y in range(min_y, max_y, cfg.grid_step):
            found = _find_free_grid_candidate(x, y, surface_occupied)
            if found is not None:
                cands.append(found)
    return cands


def _place_switch_terminal(
    target_pos: GridPos,
    surface_occupied: set[GridPos],
    forbidden_entities: Sequence[JsonMap],
    cfg: PdnConfig,
) -> GridPos:
    """Place a power switch terminal pole near target_pos avoiding forbidden_entities."""
    bx = math.floor(target_pos[0]) + TILE_HALF
    by = math.floor(target_pos[1]) + TILE_HALF
    for dx, dy in TERMINAL_SEARCH_DELTAS:
        cand = (bx + dx, by + dy)
        if cand not in surface_occupied and not any(
            _is_covered(fe, cand, cfg.supply_radius) for fe in forbidden_entities
        ):
            return cand
    raise RuntimeError(f"Could not place power switch terminal near {target_pos}")


def _entity_grid_buckets(ent: JsonMap, cfg: PdnConfig) -> list[tuple[int, int]]:
    """Return all candidate grid bucket keys within supply reach of ent."""
    ex, ey = _ent_xy(ent)
    cx, cy = entity_collision_half_size(str(ent.get("name", "")), _ent_dir(ent))
    reach_x = cx + cfg.supply_radius
    reach_y = cy + cfg.supply_radius
    gx_min = int((ex - reach_x) // cfg.grid_step)
    gx_max = int((ex + reach_x) // cfg.grid_step)
    gy_min = int((ey - reach_y) // cfg.grid_step)
    gy_max = int((ey + reach_y) // cfg.grid_step)
    return [(gx, gy) for gx in range(gx_min, gx_max + 1) for gy in range(gy_min, gy_max + 1)]


def _index_candidate_coverage(
    domain_powered: Sequence[JsonMap],
    valid_cands: Sequence[GridPos],
    cfg: PdnConfig,
) -> dict[GridPos, set[int]]:
    """Build coverage map from valid candidates to covered domain entity indices."""
    cand_grid: dict[tuple[int, int], list[GridPos]] = collections.defaultdict(list)
    for c in valid_cands:
        cand_grid[(int(c[0] // cfg.grid_step), int(c[1] // cfg.grid_step))].append(c)

    cand_covers: dict[GridPos, set[int]] = collections.defaultdict(set)
    for i, ent in enumerate(domain_powered):
        for bucket in _entity_grid_buckets(ent, cfg):
            for c in cand_grid.get(bucket, ()):
                if _is_covered(ent, c, cfg.supply_radius):
                    cand_covers[c].add(i)
    return cand_covers


def _greedy_set_cover(
    num_entities: int,
    cand_covers: Mapping[GridPos, set[int]],
) -> tuple[set[GridPos], set[int]]:
    """Select candidate poles using greedy set cover."""
    uncovered = set(range(num_entities))
    chosen: set[GridPos] = set()
    active_cands = list(cand_covers.keys())
    while uncovered and active_cands:
        best_cand = max(active_cands, key=lambda c: len(cand_covers[c] & uncovered))
        best_covered = cand_covers[best_cand] & uncovered
        if not best_covered:
            break
        chosen.add(best_cand)
        uncovered -= best_covered
    return chosen, uncovered


def _find_fallback_pole_for_entity(
    ent: JsonMap,
    surface_occupied: set[GridPos],
    chosen: set[GridPos],
    forbidden_entities: Sequence[JsonMap],
    cfg: PdnConfig,
) -> GridPos | None:
    """Search the local tile neighborhood around ent for a valid covering pole."""
    px, py = _ent_xy(ent)
    cx, cy = entity_collision_half_size(str(ent.get("name", "")), _ent_dir(ent))
    reach = math.ceil(cfg.supply_radius + max(cx, cy))
    ix = math.floor(px)
    iy = math.floor(py)
    for tx in range(ix - reach, ix + reach + 1):
        for ty in range(iy - reach, iy + reach + 1):
            cand = (tx + TILE_HALF, ty + TILE_HALF)
            if (
                cand not in surface_occupied
                and cand not in chosen
                and _is_covered(ent, cand, cfg.supply_radius)
                and not any(_is_covered(fe, cand, cfg.supply_radius) for fe in forbidden_entities)
            ):
                return cand
    return None


def _cover_remaining_entities(
    domain_name: str,
    domain_powered: Sequence[JsonMap],
    uncovered: set[int],
    chosen: set[GridPos],
    valid_cands: list[GridPos],
    surface_occupied: set[GridPos],
    forbidden_entities: Sequence[JsonMap],
    cfg: PdnConfig,
) -> None:
    """Cover any entities missed by the coarse candidate grid using a local scan."""
    for i in list(uncovered):
        ent = domain_powered[i]
        cand = _find_fallback_pole_for_entity(
            ent, surface_occupied, chosen, forbidden_entities, cfg
        )
        if cand is not None:
            chosen.add(cand)
            if cand not in valid_cands:
                valid_cands.append(cand)
            uncovered.remove(i)
        else:
            px, py = _ent_xy(ent)
            sys.stderr.write(f"UNCOVERED {domain_name} {ent.get('name')} at ({px}, {py})\n")


def _build_wire_adjacency(
    cand_list: Sequence[GridPos], wire_reach: float
) -> dict[GridPos, list[GridPos]]:
    """Build adjacency lists for candidate poles within wire_reach."""
    cand_adj: dict[GridPos, list[GridPos]] = {c: [] for c in cand_list}
    for c1, c2 in itertools.combinations(cand_list, COORD_PRECISION):
        if math.hypot(c1[0] - c2[0], c1[1] - c2[1]) <= wire_reach:
            cand_adj[c1].append(c2)
            cand_adj[c2].append(c1)
    return cand_adj


def _absorb_adjacent_chosen(
    seeds: Sequence[GridPos],
    cand_adj: Mapping[GridPos, Sequence[GridPos]],
    connected: set[GridPos],
    unconnected: set[GridPos],
) -> None:
    """Absorb all unconnected chosen poles reachable from seeds via cand_adj."""
    queue = collections.deque(seeds)
    while queue:
        u_c = queue.popleft()
        for v_c in cand_adj.get(u_c, ()):
            if v_c in unconnected:
                unconnected.remove(v_c)
                connected.add(v_c)
                queue.append(v_c)


def _shortest_bridge_path(
    connected: set[GridPos],
    unconnected: set[GridPos],
    cand_adj: Mapping[GridPos, Sequence[GridPos]],
) -> list[GridPos]:
    """Find shortest candidate pole path from connected to the nearest unconnected pole."""
    visited = set(connected)
    parent: dict[GridPos, GridPos] = {}
    queue: collections.deque[GridPos] = collections.deque(sorted(connected))
    target: GridPos | None = None
    while queue:
        u = queue.popleft()
        if u in unconnected:
            target = u
            break
        for v in cand_adj.get(u, ()):
            if v not in visited:
                visited.add(v)
                parent[v] = u
                queue.append(v)
    if target is None:
        return []
    path: list[GridPos] = []
    curr = target
    while curr not in connected:
        path.append(curr)
        curr = parent[curr]
    return path


def _connect_steiner_poles(
    chosen: set[GridPos],
    cand_adj: Mapping[GridPos, Sequence[GridPos]],
) -> set[GridPos]:
    """Bridge disconnected chosen pole components using candidate Steiner poles."""
    all_poles = set(chosen)
    if not chosen:
        return all_poles
    first = min(chosen)
    connected = {first}
    unconnected = set(chosen) - connected
    _absorb_adjacent_chosen([first], cand_adj, connected, unconnected)

    while unconnected:
        path = _shortest_bridge_path(connected, unconnected, cand_adj)
        if not path:
            break
        target = path[0]
        all_poles.update(path)
        connected.update(path)
        unconnected.discard(target)
        _absorb_adjacent_chosen(path, cand_adj, connected, unconnected)
    return all_poles


def _find_mst_root(parent_mst: list[int], node: int) -> int:
    """Find disjoint-set root with path compression."""
    while parent_mst[node] != node:
        parent_mst[node] = parent_mst[parent_mst[node]]
        node = parent_mst[node]
    return node


def _collect_mst_edges(
    pole_list: Sequence[GridPos], wire_reach: float
) -> list[tuple[float, int, int]]:
    """Collect sorted (distance, u, v) edges for pole pairs within wire_reach."""
    edges = [
        (d, i, j)
        for (i, p_i), (j, p_j) in itertools.combinations(enumerate(pole_list), COORD_PRECISION)
        if (d := math.hypot(p_i[0] - p_j[0], p_i[1] - p_j[1])) <= wire_reach
    ]
    edges.sort()
    return edges


def _build_domain_mst(all_poles: set[GridPos], wire_reach: float) -> list[MstEdge]:
    """Build a Kruskal minimum spanning tree over all_poles within wire_reach."""
    pole_list = sorted(all_poles)
    pole_edges = _collect_mst_edges(pole_list, wire_reach)
    parent_mst = list(range(len(pole_list)))
    domain_mst: list[MstEdge] = []
    for _d, u, v in pole_edges:
        ru = _find_mst_root(parent_mst, u)
        rv = _find_mst_root(parent_mst, v)
        if ru != rv:
            parent_mst[ru] = rv
            domain_mst.append((pole_list[u], pole_list[v]))
    return domain_mst


def _solve_domain(
    domain_name: str,
    domain_powered: Sequence[JsonMap],
    forbidden_entities: Sequence[JsonMap],
    extra_terminals: Sequence[GridPos],
    cands: Sequence[GridPos],
    surface_occupied: set[GridPos],
    cfg: PdnConfig,
) -> tuple[set[GridPos], list[MstEdge]]:
    """Solve pole placement and MST wiring for a single power domain."""
    if not domain_powered and not extra_terminals:
        return set(), []

    valid_cands = [
        c
        for c in cands
        if c not in surface_occupied
        and not any(_is_covered(fe, c, cfg.supply_radius) for fe in forbidden_entities)
    ]
    cand_covers = _index_candidate_coverage(domain_powered, valid_cands, cfg)
    chosen, uncovered = _greedy_set_cover(len(domain_powered), cand_covers)
    if uncovered:
        _cover_remaining_entities(
            domain_name,
            domain_powered,
            uncovered,
            chosen,
            valid_cands,
            surface_occupied,
            forbidden_entities,
            cfg,
        )
    for term in extra_terminals:
        chosen.add(term)
        if term not in valid_cands:
            valid_cands.append(term)

    cand_adj = _build_wire_adjacency(valid_cands, cfg.wire_reach)
    all_poles = _connect_steiner_poles(chosen, cand_adj)
    domain_mst = _build_domain_mst(all_poles, cfg.wire_reach)
    return all_poles, domain_mst


def _emit_domain_poles(
    poles: set[GridPos],
    domain_name: str,
    entities: MutableSequence[EntityDict],
    pole_entities: dict[GridPos, EntityDict],
    surface_occupied: set[GridPos],
    pole_name: str,
) -> None:
    """Append synthesized pole entities for domain_name to entities and pole_entities."""
    for p in sorted(poles):
        pole_ent: EntityDict = {
            "name": pole_name,
            "position": {"x": p[0], "y": p[1]},
            "power_domain": domain_name,
        }
        entities.append(pole_ent)
        pole_entities[p] = pole_ent
        surface_occupied.add(p)


def _solve_multi_domain(
    power_switch_ents: Sequence[JsonMap],
    powered: Sequence[JsonMap],
    cands: Sequence[GridPos],
    surface_occupied: set[GridPos],
    entities: MutableSequence[EntityDict],
    pole_entities: dict[GridPos, EntityDict],
    cfg: PdnConfig,
) -> list[MstEdge]:
    """Solve AON and switched power domains and return combined MST connections."""
    aon_powered = [e for e in powered if is_aon_entity(e)]
    switched_powered = [e for e in powered if not is_aon_entity(e)]

    aon_terminals: list[GridPos] = []
    switched_terminals: list[GridPos] = []
    for ps in power_switch_ents:
        ps_pos = _ent_xy(ps)
        p_aon = _place_switch_terminal(ps_pos, surface_occupied, switched_powered, cfg)
        aon_terminals.append(p_aon)
        surface_occupied.add(p_aon)

        p_sw = _place_switch_terminal(ps_pos, surface_occupied, aon_powered, cfg)
        switched_terminals.append(p_sw)
        surface_occupied.add(p_sw)

    aon_poles, aon_mst = _solve_domain(
        "aon", aon_powered, switched_powered, aon_terminals, cands, surface_occupied, cfg
    )
    surface_occupied.update(aon_poles)

    switched_poles, switched_mst = _solve_domain(
        "switched", switched_powered, aon_powered, switched_terminals, cands, surface_occupied, cfg
    )
    surface_occupied.update(switched_poles)

    _emit_domain_poles(aon_poles, "aon", entities, pole_entities, surface_occupied, cfg.pole_name)
    _emit_domain_poles(
        switched_poles, "switched", entities, pole_entities, surface_occupied, cfg.pole_name
    )
    return aon_mst + switched_mst


def generate_pdn(
    entities: MutableSequence[EntityDict],
    occupied_positions: set[GridPos],
    pin_tiles: set[GridPos],
    valid_input_pins: set[GridPos],
    valid_output_pins: set[GridPos],
    config: PdnConfig = DEFAULT_PDN_CONFIG,
) -> tuple[dict[GridPos, EntityDict], list[MstEdge]]:
    """Synthesize electric pole layout and wire connections."""
    pole_entities: dict[GridPos, EntityDict] = {}
    power_switch_ents = [e for e in entities if e.get("name") == "power-switch"]
    powered = [e for e in entities if any(k in str(e.get("name", "")) for k in POWERED_KEYWORDS)]
    if not powered:
        return pole_entities, []

    all_pins = set(pin_tiles) | set(valid_input_pins) | set(valid_output_pins)
    surface_occupied = _build_surface_occupied(entities, occupied_positions, all_pins)
    cands = _generate_candidate_poles([*powered, *power_switch_ents], surface_occupied, config)

    if not power_switch_ents:
        all_poles, mst_connections = _solve_domain(
            "default", powered, (), (), cands, surface_occupied, config
        )
        _emit_domain_poles(
            all_poles, "default", entities, pole_entities, surface_occupied, config.pole_name
        )
    else:
        mst_connections = _solve_multi_domain(
            power_switch_ents,
            powered,
            cands,
            surface_occupied,
            entities,
            pole_entities,
            config,
        )

    unpowered_ents = check_power(list(entities), supply_radius=config.supply_radius)
    if unpowered_ents:
        raise RuntimeError(
            f"Power check failed: {len(unpowered_ents)} entities lack electric pole coverage"
        )
    return pole_entities, mst_connections
