"""Inline pump insertion pass for physical fluid pipelines.

Splits fluid networks exceeding the maximum pipeline extent limit (320 tiles in Factorio 2.0)
into smaller segments using inline powered pumps.
"""

from __future__ import annotations

import collections
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

try:
    from foundry.entity_helpers import (
        DEFAULT_DEF_UNITS,
        DEFAULT_PIPE_UNDERGROUND_SPAN,
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        OPPOSITE_DIR,
        TILE_HALF,
        get_underground_span,
    )
    from foundry.jumper import RouteResult
    from foundry.lef import get_macro_port_geometry
    from foundry.placement import PlacementResult
except ImportError:
    from entity_helpers import (
        DEFAULT_DEF_UNITS,
        DEFAULT_PIPE_UNDERGROUND_SPAN,
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        OPPOSITE_DIR,
        TILE_HALF,
        get_underground_span,
    )
    from jumper import RouteResult
    from lef import get_macro_port_geometry
    from placement import PlacementResult

TileCoord: TypeAlias = tuple[int, int]
GridCoord: TypeAlias = tuple[float, float]
ConduitNode: TypeAlias = tuple[str, int, int] | tuple[str, int, int, int]
ConduitGraph: TypeAlias = dict[ConduitNode, set[ConduitNode]]
EntityRecord: TypeAlias = dict[str, object]
JsonMap: TypeAlias = Mapping[str, object]
TileEntityMap: TypeAlias = dict[TileCoord, list[EntityRecord]]
PipeEntityMap: TypeAlias = dict[TileCoord, EntityRecord]

PUMP_NODE_LEN = 4
PUMP_NODE_X_IDX = 2
PUMP_NODE_Y_IDX = 3
PIPE_NODE_X_IDX = 1
PIPE_NODE_Y_IDX = 2

CARDINAL_OFFSETS: tuple[TileCoord, ...] = ((0, 1), (0, -1), (1, 0), (-1, 0))
COMPRESSION_AXES: tuple[tuple[int, int, int, int], ...] = (
    (1, 0, DIRECTION_WEST, DIRECTION_EAST),
    (0, 1, DIRECTION_NORTH, DIRECTION_SOUTH),
)
PASSTHROUGH_OFFSETS: dict[str, TileCoord] = {
    "boiler": (1, 4),
    "steam-engine": (2, 6),
}


@dataclass(frozen=True)
class PumpInsertionConfig:
    """Configuration parameters for inline fluid pipeline pump insertion."""

    default_units: float = DEFAULT_DEF_UNITS
    default_pipe_span: int = DEFAULT_PIPE_UNDERGROUND_SPAN
    max_segment_extent: int = 300
    max_pumps: int = 200
    target_extent_margin: int = 40
    min_target_distance: int = 50
    min_compress_run_length: int = 8
    min_compress_inner_span: int = 4


DEFAULT_PUMP_CONFIG = PumpInsertionConfig()


@dataclass
class _UndergroundPipePair:
    """A matched pair of facing underground pipe-to-ground entities."""

    ent_u: EntityRecord
    ent_v: EntityRecord
    ux: int
    uy: int
    direction: int
    ug_direction: int
    dx: int
    dy: int
    span: int
    span_tiles: set[TileCoord]


@dataclass(frozen=True)
class _ComponentFlowProfile:
    """Source/sink weights and BFS distance map for one fluid component."""

    sources: set[ConduitNode]
    sinks: set[ConduitNode]
    raw_src: dict[ConduitNode, int]
    raw_sink: dict[ConduitNode, int]
    total_srcs: int
    total_sinks: int
    use_sources: bool
    dist: dict[ConduitNode, int]


@dataclass(frozen=True)
class _PumpCandidate:
    """A scored candidate pipe-pair replacement location for an inline pump."""

    score: tuple[int, float, float, int]
    in_tile: TileCoord
    out_tile: TileCoord
    direction: int
    center_x: float
    center_y: float


def _as_float(val: object, default: float = 0.0) -> float:
    return float(val) if isinstance(val, (int, float, str)) and val != "" else default


def _as_int(val: object, default: int = 0) -> int:
    return int(float(val)) if isinstance(val, (int, float, str)) and val != "" else default


def _as_str(val: object, default: str = "") -> str:
    return str(val) if val is not None and val != "" else default


def _as_map(val: object) -> JsonMap:
    return val if isinstance(val, Mapping) else {}


def _as_list(val: object) -> list[object]:
    return list(val) if isinstance(val, (list, tuple, set)) else []


def _entity_coords(ent: JsonMap) -> GridCoord:
    pos = ent.get("position", {})
    if isinstance(pos, Mapping):
        return _as_float(pos.get("x", 0.0)), _as_float(pos.get("y", 0.0))
    if isinstance(pos, Sequence) and len(pos) >= 2:
        return _as_float(pos[0]), _as_float(pos[1])
    return 0.0, 0.0


def _entity_tile(ent: JsonMap) -> TileCoord:
    cx, cy = _entity_coords(ent)
    return math.floor(cx), math.floor(cy)


def _tile_center_key(tile: TileCoord) -> GridCoord:
    return round(tile[0] + TILE_HALF, 2), round(tile[1] + TILE_HALF, 2)


def _node_tile(node: ConduitNode) -> TileCoord:
    if len(node) == PUMP_NODE_LEN:
        return int(node[PUMP_NODE_X_IDX]), int(node[PUMP_NODE_Y_IDX])
    return int(node[PIPE_NODE_X_IDX]), int(node[PIPE_NODE_Y_IDX])


def _get_pump_tiles(cx: float, cy: float, direction: int) -> tuple[TileCoord, TileCoord]:
    """Return the (in_tile, out_tile) for a pump entity."""
    dx, dy = DIR_STEPS.get(direction, (0, -1))
    in_tile = (math.floor(cx - dx * TILE_HALF), math.floor(cy - dy * TILE_HALF))
    out_tile = (math.floor(cx + dx * TILE_HALF), math.floor(cy + dy * TILE_HALF))
    return in_tile, out_tile


def _add_edge(adj: ConduitGraph, u: ConduitNode, v: ConduitNode) -> None:
    adj[u].add(v)
    adj[v].add(u)


def _connect_pump_neighbor(
    adj: ConduitGraph,
    source_node: ConduitNode,
    source_tile: TileCoord,
    neighbor_tile: TileCoord,
    pump_ent: EntityRecord,
) -> None:
    """Connect a pipe or pipe-to-ground node to an adjacent pump input or output."""
    pump_d = _as_int(pump_ent.get("direction"), DIRECTION_NORTH)
    pcx, pcy = _entity_coords(pump_ent)
    in_t, out_t = _get_pump_tiles(pcx, pcy, pump_d)
    p_dx, p_dy = DIR_STEPS.get(pump_d, (0, 0))
    if neighbor_tile == in_t and source_tile == (in_t[0] - p_dx, in_t[1] - p_dy):
        _add_edge(adj, source_node, ("pump_in", id(pump_ent), in_t[0], in_t[1]))
    elif neighbor_tile == out_t and source_tile == (out_t[0] + p_dx, out_t[1] + p_dy):
        _add_edge(adj, source_node, ("pump_out", id(pump_ent), out_t[0], out_t[1]))


def _connect_surface_pipe_neighbor(
    adj: ConduitGraph,
    node: ConduitNode,
    pipe_tile: TileCoord,
    nb_tile: TileCoord,
    nb: EntityRecord,
) -> None:
    """Connect a surface pipe node to one adjacent tile entity if fluid-compatible."""
    nb_name = _as_str(nb.get("name"))
    if nb_name == "pipe":
        _add_edge(adj, node, ("pipe", nb_tile[0], nb_tile[1]))
    elif nb_name == "pipe-to-ground":
        nb_d = _as_int(nb.get("direction"), DIRECTION_NORTH)
        surf_dx, surf_dy = DIR_STEPS.get(nb_d, (0, 0))
        if (nb_tile[0] + surf_dx, nb_tile[1] + surf_dy) == pipe_tile:
            _add_edge(adj, node, ("pipe-to-ground", nb_tile[0], nb_tile[1]))
    elif nb_name == "pump":
        _connect_pump_neighbor(adj, node, pipe_tile, nb_tile, nb)


def _connect_p2g_surface_mouth(
    adj: ConduitGraph,
    node: ConduitNode,
    p2g_tile: TileCoord,
    direction: int,
    tile_map: TileEntityMap,
) -> None:
    """Connect a pipe-to-ground surface mouth to a facing pipe-to-ground or pump."""
    surf_dx, surf_dy = DIR_STEPS.get(direction, (0, 0))
    surf_pos = (p2g_tile[0] + surf_dx, p2g_tile[1] + surf_dy)
    for c in tile_map.get(surf_pos, ()):
        c_name = _as_str(c.get("name"))
        if c_name == "pipe-to-ground":
            cd = _as_int(c.get("direction"), DIRECTION_NORTH)
            c_dx, c_dy = DIR_STEPS.get(cd, (0, 0))
            if (surf_pos[0] + c_dx, surf_pos[1] + c_dy) == p2g_tile:
                _add_edge(adj, node, ("pipe-to-ground", surf_pos[0], surf_pos[1]))
        elif c_name == "pump":
            _connect_pump_neighbor(adj, node, p2g_tile, surf_pos, c)


def _connect_p2g_underground_span(
    adj: ConduitGraph,
    node: ConduitNode,
    p2g_tile: TileCoord,
    ug_direction: int,
    tile_map: TileEntityMap,
    max_pipe_span: int,
) -> None:
    """Connect a pipe-to-ground underground side to the nearest facing pipe-to-ground within span."""
    dx, dy = DIR_STEPS.get(ug_direction, (0, 0))
    for span in range(1, max_pipe_span + 1):
        chk_pos = (p2g_tile[0] + dx * span, p2g_tile[1] + dy * span)
        match = next(
            (
                c
                for c in tile_map.get(chk_pos, ())
                if _as_str(c.get("name")) == "pipe-to-ground"
                and _as_int(c.get("direction"), DIRECTION_NORTH) == ug_direction
            ),
            None,
        )
        if match is not None:
            _add_edge(adj, node, ("pipe-to-ground", chk_pos[0], chk_pos[1]))
            return


def _index_conduit_entities(
    entities: Sequence[EntityRecord],
) -> tuple[TileEntityMap, PipeEntityMap, list[EntityRecord]]:
    """Classify fluid entities into tile_map, pipe_entities, and pipe-to-ground list."""
    tile_map: TileEntityMap = {}
    pipe_entities: PipeEntityMap = {}
    p2gs: list[EntityRecord] = []
    for ent in entities:
        name = _as_str(ent.get("name"))
        if name == "pipe":
            t = _entity_tile(ent)
            tile_map.setdefault(t, []).append(ent)
            pipe_entities[t] = ent
        elif name == "pipe-to-ground":
            tile_map.setdefault(_entity_tile(ent), []).append(ent)
            p2gs.append(ent)
        elif name == "pump":
            cx, cy = _entity_coords(ent)
            in_t, out_t = _get_pump_tiles(cx, cy, _as_int(ent.get("direction"), DIRECTION_NORTH))
            tile_map.setdefault(in_t, []).append(ent)
            tile_map.setdefault(out_t, []).append(ent)
    return tile_map, pipe_entities, p2gs


def build_conduit_graph(
    entities: Sequence[EntityRecord],
) -> tuple[ConduitGraph, TileEntityMap, PipeEntityMap]:
    """Build the undirected graph of fluid conduit connections.

    Pumps separate upstream and downstream networks by creating distinct
    pump_in and pump_out nodes with no connecting edge between them.
    """
    tile_map, pipe_entities, p2gs = _index_conduit_entities(entities)
    adj: ConduitGraph = collections.defaultdict(set)

    for px, py in pipe_entities:
        node: ConduitNode = ("pipe", px, py)
        adj.setdefault(node, set())
        for nb_tile, nb in (
            ((px + dx, py + dy), ent)
            for dx, dy in CARDINAL_OFFSETS
            for ent in tile_map.get((px + dx, py + dy), ())
        ):
            _connect_surface_pipe_neighbor(adj, node, (px, py), nb_tile, nb)

    max_pipe_span = get_underground_span("pipe-to-ground", DEFAULT_PUMP_CONFIG.default_pipe_span)
    for p in p2gs:
        px, py = _entity_tile(p)
        d = _as_int(p.get("direction"), DIRECTION_NORTH)
        ug_d = OPPOSITE_DIR.get(d, d)
        node = ("pipe-to-ground", px, py)
        adj.setdefault(node, set())
        _connect_p2g_surface_mouth(adj, node, (px, py), d, tile_map)
        _connect_p2g_underground_span(adj, node, (px, py), ug_d, tile_map, max_pipe_span)

    return adj, tile_map, pipe_entities


def find_fluid_components(adj: Mapping[ConduitNode, set[ConduitNode]]) -> list[set[ConduitNode]]:
    """Return all connected components of conduit nodes."""
    visited: set[ConduitNode] = set()
    components: list[set[ConduitNode]] = []

    for node in adj:
        if node in visited:
            continue
        comp: set[ConduitNode] = set()
        queue = collections.deque([node])
        visited.add(node)
        while queue:
            curr = queue.popleft()
            comp.add(curr)
            for nb in adj.get(curr, ()):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        components.append(comp)

    return components


def component_extent(comp: set[ConduitNode]) -> tuple[float, float, float, float, float]:
    """Calculate (extent, min_x, max_x, min_y, max_y) of a component."""
    if not comp:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    coords = [_node_tile(node) for node in comp]
    xs = [pt[0] for pt in coords]
    ys = [pt[1] for pt in coords]
    min_x, max_x = float(min(xs)), float(max(xs))
    min_y, max_y = float(min(ys)), float(max(ys))
    return max(max_x - min_x, max_y - min_y), min_x, max_x, min_y, max_y


def _collect_blocked_ug_corridors(
    entities: Sequence[EntityRecord],
    max_span: int,
) -> set[TileCoord]:
    """Return all tiles lying within existing pipe-to-ground corridors."""
    blocked: set[TileCoord] = set()
    for ent in entities:
        if _as_str(ent.get("name")) != "pipe-to-ground":
            continue
        px, py = _entity_tile(ent)
        d = _as_int(ent.get("direction"), DIRECTION_NORTH)
        dx, dy = DIR_STEPS.get(d, (0, 0))
        for s in range(-max_span, max_span + 1):
            blocked.add((px + dx * s, py + dy * s))
    return blocked


def _find_collinear_pipe_runs(
    pipe_entities: PipeEntityMap,
    adj: ConduitGraph,
    protected_tiles: set[TileCoord],
    blocked_ug_tiles: set[TileCoord],
    dx: int,
    dy: int,
    min_run_len: int,
) -> list[list[TileCoord]]:
    """Find contiguous straight surface pipe runs along (dx, dy) of at least min_run_len."""
    excluded = protected_tiles | blocked_ug_tiles
    eligible = {
        (x, y)
        for x, y in pipe_entities
        if (x, y) not in excluded
        and len(adj.get(("pipe", x, y), ())) == 2
        and {_node_tile(nb) for nb in adj.get(("pipe", x, y), ())}
        == {(x - dx, y - dy), (x + dx, y + dy)}
    }
    runs: list[list[TileCoord]] = []
    for x, y in sorted(pt for pt in eligible if (pt[0] - dx, pt[1] - dy) not in eligible):
        run: list[TileCoord] = []
        cx, cy = x, y
        while (cx, cy) in eligible:
            run.append((cx, cy))
            cx += dx
            cy += dy
        if len(run) >= min_run_len:
            runs.append(run)
    return runs


def _compress_single_pipe_run(
    run: Sequence[TileCoord],
    dir_neg: int,
    dir_pos: int,
    max_span: int,
    min_inner_span: int,
    pipe_entities: PipeEntityMap,
    placement: PlacementResult,
    to_remove_ids: set[int],
) -> None:
    """Convert interior pipes of one straight run into underground pipe-to-ground hops."""
    inner = run[1:-1]
    m = len(inner)
    i = 0
    while m - i >= min_inner_span:
        span = min(max_span, m - 1 - i)
        ent_start = pipe_entities.get(inner[i])
        ent_end = pipe_entities.get(inner[i + span])
        if ent_start is not None and ent_end is not None:
            ent_start["name"] = "pipe-to-ground"
            ent_start["direction"] = dir_neg
            ent_end["name"] = "pipe-to-ground"
            ent_end["direction"] = dir_pos
            for j in range(i + 1, i + span):
                ent_mid = pipe_entities.get(inner[j])
                if ent_mid is not None:
                    to_remove_ids.add(id(ent_mid))
                    placement.grid_entities.pop(_tile_center_key(inner[j]), None)
        i += span + 1


def _compress_straight_pipes(
    placement: PlacementResult,
    protected_tiles: set[TileCoord],
    cfg: PumpInsertionConfig = DEFAULT_PUMP_CONFIG,
) -> None:
    """Convert interior tiles of long straight surface pipe runs into pipe-to-ground pairs."""
    adj, _tile_map, pipe_entities = build_conduit_graph(placement.entities)
    max_span = get_underground_span("pipe-to-ground", cfg.default_pipe_span)
    blocked_ug_tiles = _collect_blocked_ug_corridors(placement.entities, max_span)
    to_remove_ids: set[int] = set()

    for dx, dy, dir_neg, dir_pos in COMPRESSION_AXES:
        runs = _find_collinear_pipe_runs(
            pipe_entities,
            adj,
            protected_tiles,
            blocked_ug_tiles,
            dx,
            dy,
            cfg.min_compress_run_length,
        )
        for run in runs:
            _compress_single_pipe_run(
                run,
                dir_neg,
                dir_pos,
                max_span,
                cfg.min_compress_inner_span,
                pipe_entities,
                placement,
                to_remove_ids,
            )

    if to_remove_ids:
        placement.entities[:] = [e for e in placement.entities if id(e) not in to_remove_ids]


def _match_underground_pipe_pairs(
    entities: Sequence[EntityRecord],
    tile_map: TileEntityMap,
    max_span: int,
) -> list[_UndergroundPipePair]:
    """Pair facing pipe-to-ground entities along their underground spans."""
    p2gs = [e for e in entities if _as_str(e.get("name")) == "pipe-to-ground"]
    matched: set[int] = set()
    pairs: list[_UndergroundPipePair] = []

    for p in p2gs:
        if id(p) in matched:
            continue
        ux, uy = _entity_tile(p)
        d = _as_int(p.get("direction"), DIRECTION_NORTH)
        ug_d = OPPOSITE_DIR.get(d, d)
        dx, dy = DIR_STEPS.get(ug_d, (0, 0))
        for span in range(1, max_span + 1):
            chk_pos = (ux + dx * span, uy + dy * span)
            found = next(
                (
                    c
                    for c in tile_map.get(chk_pos, ())
                    if _as_str(c.get("name")) == "pipe-to-ground"
                    and _as_int(c.get("direction"), DIRECTION_NORTH) == ug_d
                    and id(c) not in matched
                ),
                None,
            )
            if found is not None:
                matched.add(id(p))
                matched.add(id(found))
                span_set = {(ux + dx * s, uy + dy * s) for s in range(1, span)}
                pairs.append(
                    _UndergroundPipePair(p, found, ux, uy, d, ug_d, dx, dy, span, span_set)
                )
                break
    return pairs


def _has_perpendicular_fluid(
    tx: int,
    ty: int,
    step_dx: int,
    step_dy: int,
    protected_tiles: set[TileCoord],
    tile_map: TileEntityMap,
) -> bool:
    """Return True if either perpendicular neighbor tile has a fluid entity or is protected."""
    for nbx, nby in ((tx + step_dy, ty + step_dx), (tx - step_dy, ty - step_dx)):
        if (nbx, nby) in protected_tiles:
            return True
        if any(
            _as_str(e.get("name")) in ("pipe", "pipe-to-ground", "pump")
            for e in tile_map.get((nbx, nby), ())
        ):
            return True
    return False


def _place_unrolled_surface_pipes(
    ux: int,
    uy: int,
    dx: int,
    dy: int,
    steps: range,
    placement: PlacementResult,
    tile_map: TileEntityMap,
) -> None:
    """Insert surface pipes along the specified step indices of an unrolled span."""
    for s in steps:
        tile = (ux + dx * s, uy + dy * s)
        pkey = _tile_center_key(tile)
        new_p: EntityRecord = {"name": "pipe", "position": {"x": pkey[0], "y": pkey[1]}}
        placement.entities.append(new_p)
        placement.occupied_positions.add(pkey)
        placement.grid_entities[pkey] = new_p
        tile_map.setdefault(tile, []).append(new_p)


def _relocate_p2g_hood(
    ent: EntityRecord,
    new_pos: TileCoord,
    placement: PlacementResult,
    tile_map: TileEntityMap,
) -> None:
    """Move an underground pipe hood entity to a new tile coordinate."""
    old_pos = _entity_tile(ent)
    new_key = _tile_center_key(new_pos)
    if old_pos in tile_map and ent in tile_map[old_pos]:
        tile_map[old_pos].remove(ent)
    ent["position"] = {"x": new_key[0], "y": new_key[1]}
    placement.occupied_positions.add(new_key)
    placement.grid_entities[new_key] = ent
    tile_map.setdefault(new_pos, []).append(ent)


def _evaluate_span_unroll_eligibility(
    pair: _UndergroundPipePair,
    other_ug_tiles: set[TileCoord],
    protected_tiles: set[TileCoord],
    placement: PlacementResult,
    tile_map: TileEntityMap,
) -> tuple[list[bool], list[bool]]:
    """Compute (can_be_pipe, can_be_hood) flags for each step 0..span of an underground pair."""
    can_be_pipe: list[bool] = []
    can_be_hood: list[bool] = []
    for s in range(pair.span + 1):
        tx, ty = pair.ux + pair.dx * s, pair.uy + pair.dy * s
        pkey = _tile_center_key((tx, ty))
        no_perp = not _has_perpendicular_fluid(tx, ty, pair.dx, pair.dy, protected_tiles, tile_map)
        if s in (0, pair.span):
            can_be_hood.append(True)
            can_be_pipe.append((tx, ty) not in protected_tiles and no_perp)
        else:
            is_free = (
                pkey not in placement.occupied_positions
                and (tx, ty) not in tile_map
                and (tx, ty) not in protected_tiles
            )
            can_be_hood.append(is_free and (tx, ty) not in other_ug_tiles)
            can_be_pipe.append(is_free and no_perp)
    return can_be_pipe, can_be_hood


def _shrink_single_pair(
    pair: _UndergroundPipePair,
    other_ug_tiles: set[TileCoord],
    protected_tiles: set[TileCoord],
    placement: PlacementResult,
    tile_map: TileEntityMap,
) -> None:
    """Unroll an underground pipe pair completely or partially where surface tiles are free."""
    can_be_pipe, can_be_hood = _evaluate_span_unroll_eligibility(
        pair, other_ug_tiles, protected_tiles, placement, tile_map
    )
    if all(can_be_pipe):
        pair.ent_u["name"] = "pipe"
        pair.ent_u.pop("direction", None)
        pair.ent_v["name"] = "pipe"
        pair.ent_v.pop("direction", None)
        _place_unrolled_surface_pipes(
            pair.ux, pair.uy, pair.dx, pair.dy, range(1, pair.span), placement, tile_map
        )
        pair.span = 0
        pair.span_tiles = set()
        return

    s_u = max(
        (s for s in range(1, pair.span) if all(can_be_pipe[:s]) and can_be_hood[s]),
        default=0,
    )
    s_v = min(
        (
            s
            for s in range(pair.span - 1, s_u, -1)
            if all(can_be_pipe[s + 1 : pair.span + 1]) and can_be_hood[s]
        ),
        default=pair.span,
    )
    if s_u == 0 and s_v == pair.span:
        return

    new_u_pos = (pair.ux + pair.dx * s_u, pair.uy + pair.dy * s_u)
    new_v_pos = (pair.ux + pair.dx * s_v, pair.uy + pair.dy * s_v)

    if s_u > 0:
        _relocate_p2g_hood(pair.ent_u, new_u_pos, placement, tile_map)
        _place_unrolled_surface_pipes(
            pair.ux, pair.uy, pair.dx, pair.dy, range(s_u), placement, tile_map
        )

    if s_v < pair.span:
        _relocate_p2g_hood(pair.ent_v, new_v_pos, placement, tile_map)
        _place_unrolled_surface_pipes(
            pair.ux, pair.uy, pair.dx, pair.dy, range(s_v + 1, pair.span + 1), placement, tile_map
        )

    pair.span_tiles = {(pair.ux + pair.dx * s, pair.uy + pair.dy * s) for s in range(s_u + 1, s_v)}
    pair.ux, pair.uy = new_u_pos
    pair.span = s_v - s_u


def _other_pair_tiles(pairs: Sequence[_UndergroundPipePair], exclude_idx: int) -> set[TileCoord]:
    """Return all underground span tiles belonging to underground pairs other than exclude_idx."""
    tiles: set[TileCoord] = set()
    for j, other in enumerate(pairs):
        if j != exclude_idx:
            tiles.update(other.span_tiles)
    return tiles


def _shrink_underground_pipe_spans(
    placement: PlacementResult,
    protected_tiles: set[TileCoord],
    max_segment_extent: int,
    cfg: PumpInsertionConfig = DEFAULT_PUMP_CONFIG,
) -> None:
    """Unroll unobstructed sections of underground pipe-to-ground spans in overextended components."""
    adj, tile_map, _pipe_entities = build_conduit_graph(placement.entities)
    overextended_nodes = {
        node
        for comp in find_fluid_components(adj)
        if component_extent(comp)[0] > float(max_segment_extent)
        for node in comp
    }
    if not overextended_nodes:
        return

    max_span = get_underground_span("pipe-to-ground", cfg.default_pipe_span)
    pairs = _match_underground_pipe_pairs(placement.entities, tile_map, max_span)

    for idx, pair in enumerate(pairs):
        if pair.span >= 2 and ("pipe-to-ground", pair.ux, pair.uy) in overextended_nodes:
            other_ug_tiles = _other_pair_tiles(pairs, idx)
            _shrink_single_pair(pair, other_ug_tiles, protected_tiles, placement, tile_map)


def _matches_fluid(name: str, fluids: set[str]) -> bool:
    return name in fluids or any(f in name for f in fluids)


def _collect_component_fluid_ports(
    comp: JsonMap,
    inst_map: dict[str, JsonMap],
    cell_map: JsonMap,
    scale: float,
    fluids: set[str],
    fluid_source_tiles: set[TileCoord],
    fluid_sink_tiles: set[TileCoord],
    unused_passthrough_tiles: set[TileCoord],
) -> None:
    """Record fluid source, sink, and passthrough tiles for one placed component."""
    cname = _as_str(comp.get("name"))
    inst = inst_map.get(cname, {})
    cell_id = _as_str(inst.get("cell"), _as_str(comp.get("macro")))
    cell = _as_map(cell_map.get(cell_id))
    gx = _as_float(comp.get("x")) * scale
    gy = _as_float(comp.get("y")) * scale
    machine_type = _as_str(cell.get("machine"))
    if machine_type in PASSTHROUGH_OFFSETS:
        ox, oy = PASSTHROUGH_OFFSETS[machine_type]
        unused_passthrough_tiles.add((math.floor(gx + ox), math.floor(gy + oy)))

    for raw_port in _as_list(cell.get("outputs")):
        port = _as_str(raw_port)
        if _matches_fluid(port, fluids):
            geom = get_macro_port_geometry(cell, port, is_output=True)
            ax, ay = geom["inserter_pos"]
            fluid_source_tiles.add((math.floor(gx + ax), math.floor(gy + ay)))

    for raw_port in _as_list(cell.get("inputs")):
        port = _as_str(raw_port)
        if _matches_fluid(port, fluids):
            geom = get_macro_port_geometry(cell, port, is_output=False)
            ax, ay = geom["inserter_pos"]
            fluid_sink_tiles.add((math.floor(gx + ax), math.floor(gy + ay)))


def _collect_pin_fluid_terminals(
    pins: Sequence[object],
    scale: float,
    fluids: set[str],
) -> tuple[set[TileCoord], set[TileCoord]]:
    """Extract (fluid_source_tiles, fluid_sink_tiles) from DEF boundary pins."""
    sources: set[TileCoord] = set()
    sinks: set[TileCoord] = set()
    for raw_pin in pins:
        pin = _as_map(raw_pin)
        pname = _as_str(pin.get("name"), _as_str(pin.get("net")))
        if not _matches_fluid(pname, fluids):
            continue
        pt = (
            math.floor(_as_float(pin.get("x")) * scale),
            math.floor(_as_float(pin.get("y")) * scale),
        )
        pin_dir = _as_str(pin.get("direction"))
        if pin_dir == "INPUT":
            sources.add(pt)
        elif pin_dir == "OUTPUT":
            sinks.add(pt)
    return sources, sinks


def _collect_fluid_terminals_and_protected_tiles(
    placement: PlacementResult,
    routes: RouteResult,
    def_data: JsonMap,
    netlist_data: JsonMap,
    cfg: PumpInsertionConfig,
) -> tuple[set[TileCoord], set[TileCoord], set[TileCoord]]:
    """Collect (fluid_source_tiles, fluid_sink_tiles, protected_tiles) from layout metadata."""
    scale = 1.0 / _as_float(def_data.get("units"), cfg.default_units)
    raw_fluids = {_as_str(f) for f in _as_list(netlist_data.get("fluids"))} | set(
        placement.fluids or set()
    )
    fluids = (
        {f.replace("-", "_") for f in raw_fluids}
        | {f.replace("_", "-") for f in raw_fluids}
        | raw_fluids
    )
    inst_map = {
        _as_str(_as_map(inst).get("name")): _as_map(inst)
        for inst in _as_list(netlist_data.get("instances"))
    }
    cell_map = _as_map(netlist_data.get("cells"))

    fluid_source_tiles, fluid_sink_tiles = _collect_pin_fluid_terminals(
        _as_list(def_data.get("pins")), scale, fluids
    )
    unused_passthrough_tiles: set[TileCoord] = set()

    for raw_comp in _as_list(def_data.get("components")):
        _collect_component_fluid_ports(
            _as_map(raw_comp),
            inst_map,
            cell_map,
            scale,
            fluids,
            fluid_source_tiles,
            fluid_sink_tiles,
            unused_passthrough_tiles,
        )

    route_pins = {
        (math.floor(px_f), math.floor(py_f))
        for px_f, py_f in (routes.pin_tiles | routes.valid_input_pins | routes.valid_output_pins)
    }
    protected_tiles = (
        set(fluid_source_tiles) | set(fluid_sink_tiles) | unused_passthrough_tiles | route_pins
    )
    for bport in getattr(def_data.get("_macro_floorplan"), "block_ports", ()):
        protected_tiles.add((int(bport.core_pos[0]), int(bport.core_pos[1])))
        protected_tiles.add((int(bport.channel_pos[0]), int(bport.channel_pos[1])))

    return fluid_source_tiles, fluid_sink_tiles, protected_tiles


def _build_full_conduit_graph(
    adj: ConduitGraph,
    entities: Sequence[EntityRecord],
) -> ConduitGraph:
    """Copy the conduit graph and bridge pump_in and pump_out nodes across existing pumps."""
    full_adj: ConduitGraph = {node: set(nbs) for node, nbs in adj.items()}
    for ent in entities:
        if _as_str(ent.get("name")) != "pump":
            continue
        pcx, pcy = _entity_coords(ent)
        pump_d = _as_int(ent.get("direction"), DIRECTION_NORTH)
        in_t, out_t = _get_pump_tiles(pcx, pcy, pump_d)
        in_node: ConduitNode = ("pump_in", id(ent), in_t[0], in_t[1])
        out_node: ConduitNode = ("pump_out", id(ent), out_t[0], out_t[1])
        if in_node in full_adj and out_node in full_adj:
            _add_edge(full_adj, in_node, out_node)
    return full_adj


def _count_external_pump_terminals(
    start_node: ConduitNode,
    worst_comp: set[ConduitNode],
    full_adj: ConduitGraph,
    fluid_source_tiles: set[TileCoord],
    fluid_sink_tiles: set[TileCoord],
) -> tuple[int, int]:
    """Count true fluid sources and sinks reachable beyond a pump node outside worst_comp."""
    visited: set[ConduitNode] = {start_node}
    queue: collections.deque[ConduitNode] = collections.deque([start_node])
    while queue:
        curr = queue.popleft()
        unseen = (full_adj.get(curr, set()) - worst_comp) - visited
        visited.update(unseen)
        queue.extend(unseen)

    pipe_tiles = [
        _node_tile(nb) for nb in visited if nb != start_node and nb[0] in ("pipe", "pipe-to-ground")
    ]
    ext_srcs = sum(1 for pt in pipe_tiles if pt in fluid_source_tiles)
    ext_sinks = sum(1 for pt in pipe_tiles if pt in fluid_sink_tiles)
    return ext_srcs, ext_sinks


def _bfs_component_distances(
    ref_nodes: Sequence[ConduitNode],
    worst_comp: set[ConduitNode],
    adj: ConduitGraph,
) -> dict[ConduitNode, int]:
    """Compute shortest-path hop distances from ref_nodes within worst_comp."""
    dist: dict[ConduitNode, int] = dict.fromkeys(ref_nodes, 0)
    queue: collections.deque[tuple[ConduitNode, int]] = collections.deque((s, 0) for s in ref_nodes)
    while queue:
        curr, d = queue.popleft()
        for nb in adj.get(curr, set()) & worst_comp:
            if nb not in dist:
                dist[nb] = d + 1
                queue.append((nb, d + 1))
    return dist


def _record_node_terminals(
    node: ConduitNode,
    worst_comp: set[ConduitNode],
    full_adj: ConduitGraph,
    fluid_source_tiles: set[TileCoord],
    fluid_sink_tiles: set[TileCoord],
    sources: list[ConduitNode],
    sinks: list[ConduitNode],
    raw_src: dict[ConduitNode, int],
    raw_sink: dict[ConduitNode, int],
) -> None:
    """Classify one conduit node as a fluid source and/or sink and record its terminal weights."""
    ntype = node[0]
    if ntype == "pump_out":
        ext_srcs, ext_sinks = _count_external_pump_terminals(
            node, worst_comp, full_adj, fluid_source_tiles, fluid_sink_tiles
        )
        sources.append(node)
        raw_src[node] = max(1, ext_srcs)
        raw_sink[node] = ext_sinks
        return
    if ntype == "pump_in":
        ext_srcs, ext_sinks = _count_external_pump_terminals(
            node, worst_comp, full_adj, fluid_source_tiles, fluid_sink_tiles
        )
        sinks.append(node)
        raw_src[node] = ext_srcs
        raw_sink[node] = max(1, ext_sinks)
        return
    pt = _node_tile(node)
    if pt in fluid_source_tiles:
        sources.append(node)
        raw_src[node] = 1
    if pt in fluid_sink_tiles:
        sinks.append(node)
        raw_sink[node] = 1


def _compute_component_flow_profile(
    worst_comp: set[ConduitNode],
    adj: ConduitGraph,
    full_adj: ConduitGraph,
    fluid_source_tiles: set[TileCoord],
    fluid_sink_tiles: set[TileCoord],
) -> _ComponentFlowProfile:
    """Identify sources, sinks, terminal weights, and BFS distances for a component."""
    sources: list[ConduitNode] = []
    sinks: list[ConduitNode] = []
    raw_src: dict[ConduitNode, int] = {}
    raw_sink: dict[ConduitNode, int] = {}

    for node in worst_comp:
        _record_node_terminals(
            node,
            worst_comp,
            full_adj,
            fluid_source_tiles,
            fluid_sink_tiles,
            sources,
            sinks,
            raw_src,
            raw_sink,
        )

    use_sources = bool(sources) or not bool(sinks)
    ref_nodes = sources or sinks or [min(worst_comp, key=_node_tile)]
    dist = _bfs_component_distances(ref_nodes, worst_comp, adj)

    return _ComponentFlowProfile(
        sources=set(sources),
        sinks=set(sinks),
        raw_src=raw_src,
        raw_sink=raw_sink,
        total_srcs=sum(raw_src.values()),
        total_sinks=sum(raw_sink.values()),
        use_sources=use_sources,
        dist=dist,
    )


def _is_collinear_degree_two_pair(
    u_node: ConduitNode,
    v_node: ConduitNode,
    ux: int,
    uy: int,
    vx: int,
    vy: int,
    worst_comp: set[ConduitNode],
    adj: ConduitGraph,
) -> bool:
    """Return True if u_node and v_node form a straight degree-2 pipe segment in worst_comp."""
    u_nbs = [nb for nb in adj.get(u_node, ()) if nb in worst_comp]
    v_nbs = [nb for nb in adj.get(v_node, ()) if nb in worst_comp]
    if len(u_nbs) != 2 or len(v_nbs) != 2:
        return False
    dx, dy = vx - ux, vy - uy
    nb_u = u_nbs[0] if u_nbs[1] == v_node else u_nbs[1]
    nb_v = v_nbs[0] if v_nbs[1] == u_node else v_nbs[1]
    if nb_u[0] not in ("pipe", "pipe-to-ground") or nb_v[0] not in ("pipe", "pipe-to-ground"):
        return False
    return _node_tile(nb_u) == (ux - dx, uy - dy) and _node_tile(nb_v) == (vx + dx, vy + dy)


def _partition_without_edge(
    u_node: ConduitNode,
    v_node: ConduitNode,
    worst_comp: set[ConduitNode],
    adj: ConduitGraph,
) -> set[ConduitNode]:
    """Return the connected subset of worst_comp reachable from u_node when (u_node, v_node) is cut."""
    comp_u: set[ConduitNode] = {u_node}
    queue: collections.deque[ConduitNode] = collections.deque([u_node])
    blocked_edges = {(u_node, v_node), (v_node, u_node)}
    while queue:
        cur = queue.popleft()
        for nb in adj.get(cur, ()):
            if nb in worst_comp and nb not in comp_u and (cur, nb) not in blocked_edges:
                comp_u.add(nb)
                queue.append(nb)
    return comp_u


def _determine_cut_flow_direction(
    u_srcs: int,
    u_sinks: int,
    v_srcs: int,
    v_sinks: int,
    default_u_in: bool,
    has_both_terminals: bool,
) -> tuple[bool, int]:
    """Return (is_u_in, sep_penalty) from source/sink counts across a partition cut."""
    if not has_both_terminals:
        return default_u_in, 0
    flow_u_to_v = u_srcs * v_sinks
    flow_v_to_u = v_srcs * u_sinks
    if flow_u_to_v == 0 and flow_v_to_u == 0:
        return default_u_in, 1
    if flow_u_to_v != flow_v_to_u:
        return flow_u_to_v > flow_v_to_u, 0
    return default_u_in, 0


def _resolve_split_flow_and_metrics(
    comp_u: set[ConduitNode],
    v_node: ConduitNode,
    u_dist: int,
    v_dist: int,
    worst_extent: float,
    worst_comp: set[ConduitNode],
    profile: _ComponentFlowProfile,
    max_segment_extent: int,
) -> tuple[bool, int, float, float]:
    """Compute (is_u_in, sep_penalty, over_limit, primary_metric) for a candidate edge cut."""
    default_u_in = (u_dist < v_dist) if profile.use_sources else (u_dist > v_dist)
    if v_node in comp_u:
        return default_u_in, 1, worst_extent, round(worst_extent, 2)

    comp_v = worst_comp - comp_u
    u_srcs = sum(profile.raw_src.get(n, 0) for n in comp_u)
    u_sinks = sum(profile.raw_sink.get(n, 0) for n in comp_u)
    v_srcs = profile.total_srcs - u_srcs
    v_sinks = profile.total_sinks - u_sinks

    is_u_in, sep_penalty = _determine_cut_flow_direction(
        u_srcs, u_sinks, v_srcs, v_sinks, default_u_in, bool(profile.sources and profile.sinks)
    )
    ext_u = component_extent(comp_u)[0]
    ext_v = component_extent(comp_v)[0]
    split_ext = max(ext_u, ext_v)
    over_limit = max(0.0, split_ext - float(max_segment_extent))

    comp_in, comp_out = (comp_u, comp_v) if is_u_in else (comp_v, comp_u)
    in_sinks, out_srcs = (u_sinks, v_srcs) if is_u_in else (v_sinks, u_srcs)
    in_has_sink = in_sinks > 0 or bool(comp_in & profile.sinks)
    out_has_src = out_srcs > 0 or bool(comp_out & profile.sources)
    use_out_ext = over_limit == 0.0 and in_has_sink and not out_has_src
    ext_out = ext_v if is_u_in else ext_u
    primary_metric = round(ext_out if use_out_ext else split_ext, 2)
    return is_u_in, sep_penalty, over_limit, primary_metric


def _pump_placement_for_tiles(
    in_t: TileCoord,
    out_t: TileCoord,
    is_vertical: bool,
) -> tuple[int, float, float]:
    """Return (pump_direction, center_x, center_y) for an inline pump from in_t to out_t."""
    if is_vertical:
        if out_t[1] < in_t[1]:
            return DIRECTION_NORTH, in_t[0] + TILE_HALF, float(in_t[1])
        return DIRECTION_SOUTH, in_t[0] + TILE_HALF, float(in_t[1] + 1.0)
    if out_t[0] > in_t[0]:
        return DIRECTION_EAST, float(in_t[0] + 1.0), in_t[1] + TILE_HALF
    return DIRECTION_WEST, float(in_t[0]), in_t[1] + TILE_HALF


def _score_pipe_pair_candidate(
    ux: int,
    uy: int,
    vx: int,
    vy: int,
    is_vertical: bool,
    worst_extent: float,
    worst_comp: set[ConduitNode],
    adj: ConduitGraph,
    profile: _ComponentFlowProfile,
    target_dist: int,
    max_segment_extent: int,
) -> _PumpCandidate | None:
    """Evaluate one adjacent (u, v) surface pipe pair and return a scored candidate if valid."""
    u_node: ConduitNode = ("pipe", ux, uy)
    v_node: ConduitNode = ("pipe", vx, vy)
    u_dist = profile.dist.get(u_node)
    v_dist = profile.dist.get(v_node)
    if u_dist is None or v_dist is None or u_dist == v_dist:
        return None
    if not _is_collinear_degree_two_pair(u_node, v_node, ux, uy, vx, vy, worst_comp, adj):
        return None

    comp_u = _partition_without_edge(u_node, v_node, worst_comp, adj)
    is_u_in, sep_penalty, over_limit, primary_metric = _resolve_split_flow_and_metrics(
        comp_u, v_node, u_dist, v_dist, worst_extent, worst_comp, profile, max_segment_extent
    )
    in_t, out_t = ((ux, uy), (vx, vy)) if is_u_in else ((vx, vy), (ux, uy))
    base_dist = u_dist if is_u_in else v_dist
    pump_dir, center_x, center_y = _pump_placement_for_tiles(in_t, out_t, is_vertical)
    score = (
        sep_penalty,
        round(over_limit, 2),
        primary_metric,
        abs(base_dist - target_dist),
    )
    return _PumpCandidate(score, in_t, out_t, pump_dir, center_x, center_y)


def _find_component_pump_candidates(
    worst_extent: float,
    worst_comp: set[ConduitNode],
    adj: ConduitGraph,
    full_adj: ConduitGraph,
    pipe_entities: PipeEntityMap,
    protected_tiles: set[TileCoord],
    fluid_source_tiles: set[TileCoord],
    fluid_sink_tiles: set[TileCoord],
    max_segment_extent: int,
    cfg: PumpInsertionConfig,
) -> tuple[list[_PumpCandidate], PipeEntityMap]:
    """Evaluate all legal collinear pipe pairs in worst_comp and return scored pump candidates."""
    profile = _compute_component_flow_profile(
        worst_comp, adj, full_adj, fluid_source_tiles, fluid_sink_tiles
    )
    target_dist = max(
        cfg.min_target_distance,
        min(max_segment_extent - cfg.target_extent_margin, int(worst_extent // 2)),
    )
    comp_pipes: PipeEntityMap = {
        _node_tile(n): pipe_entities[_node_tile(n)]
        for n in worst_comp
        if n[0] == "pipe" and _node_tile(n) in pipe_entities
    }
    unprotected_pipes = set(comp_pipes) - protected_tiles
    candidates: list[_PumpCandidate] = []

    for ux, uy in unprotected_pipes:
        for vx, vy, is_vertical in ((ux, uy + 1, True), (ux + 1, uy, False)):
            if (vx, vy) not in unprotected_pipes:
                continue
            cand = _score_pipe_pair_candidate(
                ux,
                uy,
                vx,
                vy,
                is_vertical,
                worst_extent,
                worst_comp,
                adj,
                profile,
                target_dist,
                max_segment_extent,
            )
            if cand is not None:
                candidates.append(cand)

    return candidates, comp_pipes


def _apply_pump_candidate(
    cand: _PumpCandidate,
    comp_pipes: PipeEntityMap,
    placement: PlacementResult,
) -> None:
    """Replace the two surface pipes of the winning candidate with a pump entity."""
    ent_in = comp_pipes.get(cand.in_tile)
    ent_out = comp_pipes.get(cand.out_tile)
    if ent_in in placement.entities:
        placement.entities.remove(ent_in)
    if ent_out in placement.entities:
        placement.entities.remove(ent_out)

    pump_ent: EntityRecord = {
        "name": "pump",
        "position": {"x": cand.center_x, "y": cand.center_y},
        "direction": cand.direction,
    }
    placement.entities.append(pump_ent)

    pos_in_key = _tile_center_key(cand.in_tile)
    pos_out_key = _tile_center_key(cand.out_tile)
    placement.grid_entities[pos_in_key] = pump_ent
    placement.grid_entities[pos_out_key] = pump_ent
    placement.occupied_positions.add(pos_in_key)
    placement.occupied_positions.add(pos_out_key)


def insert_pipeline_pumps(
    placement: PlacementResult,
    routes: RouteResult,
    def_data: JsonMap,
    netlist_data: JsonMap,
    max_segment_extent: int | None = None,
    max_pumps: int | None = None,
    cfg: PumpInsertionConfig = DEFAULT_PUMP_CONFIG,
) -> int:
    """Insert powered inline pumps to split pipelines exceeding max_segment_extent.

    Returns the number of pumps inserted.
    """
    extent_limit = max_segment_extent if max_segment_extent is not None else cfg.max_segment_extent
    pump_limit = max_pumps if max_pumps is not None else cfg.max_pumps
    fluid_source_tiles, fluid_sink_tiles, protected_tiles = (
        _collect_fluid_terminals_and_protected_tiles(placement, routes, def_data, netlist_data, cfg)
    )
    _shrink_underground_pipe_spans(placement, protected_tiles, extent_limit, cfg)

    pumps_inserted = 0
    for _iteration in range(pump_limit):
        adj, _tile_map, pipe_entities = build_conduit_graph(placement.entities)
        overextended = [
            (ext, comp)
            for comp in find_fluid_components(adj)
            if (ext := component_extent(comp)[0]) > float(extent_limit)
        ]
        if not overextended:
            break

        overextended.sort(key=lambda item: item[0], reverse=True)
        full_adj = _build_full_conduit_graph(adj, placement.entities)

        winning_candidates: list[_PumpCandidate] = []
        winning_pipes: PipeEntityMap = {}
        for worst_extent, worst_comp in overextended:
            winning_candidates, winning_pipes = _find_component_pump_candidates(
                worst_extent,
                worst_comp,
                adj,
                full_adj,
                pipe_entities,
                protected_tiles,
                fluid_source_tiles,
                fluid_sink_tiles,
                extent_limit,
                cfg,
            )
            if winning_candidates:
                break

        if not winning_candidates:
            break

        winning_candidates.sort(key=lambda c: c.score)
        _apply_pump_candidate(winning_candidates[0], winning_pipes, placement)
        pumps_inserted += 1

    _compress_straight_pipes(placement, protected_tiles, cfg)
    return pumps_inserted
