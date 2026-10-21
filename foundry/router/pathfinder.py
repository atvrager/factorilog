"""Multi-layer A* maze router with negotiated congestion.

Implements iterative rip-up and reroute across the 3D graph.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import dataclasses
import heapq
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from foundry.router.grid import HIGHWAY_SUFFIX, RoutingGrid, _step_sign, base_net_name
from foundry.router.transitions import (
    HOOD_PENALTY,
    STEP_COST,
    TURN_PENALTY,
    UNDERGROUND_STEP_COST,
)
from foundry.router.types import (
    ALL_DIRECTIONS,
    DIR_STEPS,
    LAYER_SURFACE,
    OPPOSITE_DIR,
    PERPENDICULAR_DIRS,
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
)

# Bit-packing constants for 4D (x, y, z, heading_index) states
COORD_BIAS = 32768
COORD_MASK = 65535
STATE_X_SHIFT = 24
STATE_Y_SHIFT = 8
STATE_Z_SHIFT = 2
XY_X_SHIFT = 16
HEADING_SHIFT = 2
HEADING_INDEX_MASK = 3
LAYER_MASK = 63

# Cityblock highway and bay geometry constants
BLOCK_PITCH = 100
BLOCK_MIN_INNER = 4
BLOCK_MAX_INNER = 95
BAY_MAX_Y = 16
INTERIOR_MIN_Y = 17
STREET_WEST_NORTH_COORD = 3
STREET_EAST_SOUTH_COORD = 96
HEURISTIC_WEIGHT = 1.4

# A* search budget and spatial window constants
MAX_FLUID_GOAL_CANDIDATES = 20
LONG_SPAN_THRESHOLD = 120
MIN_BOX_AREA = 100
HIGHWAY_STATE_CAP = 49152
LOCAL_STATE_CAP = 24576
MIN_STATE_BUDGET = 4000
BOX_AREA_STATE_MULTIPLIER = 2
FRONTIER_SAMPLE_LIMIT = 4
MERGED_FRONTIER_LIMIT = 6
ERROR_COORD_SAMPLE_LIMIT = 20
DEFAULT_SEARCH_MARGIN = 10
WIDE_RETRY_MARGIN = 35
FLUID_MIN_MARGIN = 25
MAX_SPAN_MARGIN = 60
SPAN_MARGIN_DIVISOR = 8
HIGHWAY_FALLBACK_PAD = 105
WIDE_FALLBACK_MULTIPLIER = 3
MIN_WIDE_FALLBACK_MARGIN = 45
MIN_BELT_LEAP_STEP = 2

# Negotiated congestion rip-up and reroute constants
DEFAULT_ROUTE_ITERATIONS = 40
MAX_LOCAL_FAIL_RETRIES = 12
LARGE_NETLIST_THRESHOLD = 100
LARGE_PROBE_RADIUS_X = 12
SMALL_PROBE_RADIUS_X = 6
PROBE_RADIUS_Y = 8
PIN_BLOCKER_RADIUS = 2
SURFACE_HIST_RADIUS = 3
SURFACE_FAIL_HIST_INCREMENT = 1.5
HOOD_FAIL_HIST_INCREMENT = 2.5
NEIGHBOR_PROBE_ITERS = (12, 24, 36, 48, 54)
NEIGHBOR_OFFSETS = (-1, 0, 1)
NEIGHBOR_2D_OFFSETS: tuple[tuple[int, int], ...] = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 0),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)
MAX_OVER_CAP_FOR_NEIGHBORS = 25
PRIORITY_PREV_FAILED = 0
PRIORITY_PIN_BLOCKER = 1
PRIORITY_FAILED_BLOCKER = 2
PRIORITY_OVER_CAPACITY = 3
PRIORITY_OTHER_CONGESTED = 4
CORRIDOR_EXPAND_ITER = 1
UNCONDITIONAL_EXPAND_ITER = 3
WIDE_HIGHWAY_ITER = 20
WIDE_HIGHWAY_MAX_OVER_CAP = 8
LATE_BETA_ITER = 4
LATE_GAMMA_ITER = 6
EARLY_CONGESTION_BETA = 0.5
LATE_CONGESTION_BETA = 1.5
EARLY_CONGESTION_GAMMA = 1.5
LATE_CONGESTION_GAMMA = 2.5
VERBOSE_NET_THRESHOLD = 30
FAIL_LOG_SAMPLE_LIMIT = 3
STAGNANT_CONGESTED_THRESHOLD = 15
MIN_ITERS_BEFORE_STAGNATION = 30
MAX_STAGNANT_ITERS = 20
MIN_ITERS_FOR_CONFLICT_FAILURE = 20

_MST_INDEX_CACHE: dict[str, int | None] = {}


def mst_index(net_name: str) -> int | None:
    """Return the integer MST branch index if net_name is an MST branch, else None."""
    if net_name in _MST_INDEX_CACHE:
        return _MST_INDEX_CACHE[net_name]
    res: int | None = None
    if "_mst_" in net_name:
        try:
            res = int(net_name.rsplit("_mst_", 1)[1])
        except ValueError:
            res = None
    _MST_INDEX_CACHE[net_name] = res
    return res


def pack_xy(x: int, y: int) -> int:
    """Pack 2D (x, y) coordinates into an integer key matching state >> 8."""
    return ((x + COORD_BIAS) << XY_X_SHIFT) | (y + COORD_BIAS)


def pack_state(x: int, y: int, z: int, h_idx: int) -> int:
    """Pack 4D state coordinates into an integer."""
    return (
        ((x + COORD_BIAS) << STATE_X_SHIFT)
        | ((y + COORD_BIAS) << STATE_Y_SHIFT)
        | (z << STATE_Z_SHIFT)
        | h_idx
    )


def unpack_state(s: int) -> tuple[int, int, int, int, int]:
    """Unpack state integer into (x, y, z, heading, h_idx)."""
    h_idx = s & HEADING_INDEX_MASK
    h = h_idx << HEADING_SHIFT
    z = (s >> STATE_Z_SHIFT) & LAYER_MASK
    y = ((s >> STATE_Y_SHIFT) & COORD_MASK) - COORD_BIAS
    x = (s >> STATE_X_SHIFT) - COORD_BIAS
    return x, y, z, h, h_idx


def heuristic(node: RouteNode, target: tuple[int, int]) -> float:
    """Calculate Manhattan distance heuristic to the target tile."""
    return float(abs(node.x - target[0]) + abs(node.y - target[1]))


def net_sort_key(n: RouterNetSpec) -> tuple[int, int, int, str]:
    """Return the canonical ordering key for routing net n."""
    m_idx = mst_index(n.name)
    span = abs(n.source[0] - n.sink[0]) + abs(n.source[1] - n.sink[1])
    return (
        1 if n.is_fluid else 0,
        span if not n.is_fluid else 0,
        m_idx if m_idx is not None else -1,
        n.name,
    )


def _is_bay_tile(x: int, y: int) -> bool:
    """Return True if (x, y) lies inside a cityblock bay."""
    return (
        BLOCK_MIN_INNER <= (x % BLOCK_PITCH) <= BLOCK_MAX_INNER
        and BLOCK_MIN_INNER <= (y % BLOCK_PITCH) <= BAY_MAX_Y
    )


@dataclass(slots=True)
class _AStarContext:
    """Mutable state and precomputed bounds for one 3D A* search invocation."""

    net: RouterNetSpec
    grid: RoutingGrid
    sx: int
    sy: int
    tx: int
    ty: int
    sink_pos: tuple[int, int]
    net_name: str
    b_name: str
    my_mst: int | None
    lane: int
    is_fluid: bool
    tier_name_surface: str
    tier_ug_keys: tuple[int, ...]
    ug_min_steps: dict[int, int]
    max_ug_span: int
    bx0: int
    by0: int
    bx1: int
    by1: int
    existing_fluid_goals: set[tuple[int, int]]
    fluid_goals_rest: tuple[tuple[int, int], ...]
    is_hwy: bool
    src_bay: tuple[int, int] | None
    snk_bay: tuple[int, int] | None
    snk_h_band: int
    snk_v_band: int
    bay_rows: set[int]
    valid_end_headings: set[int]
    open_set: list[tuple[float, float, int]]
    g_score: dict[int, float]
    came_from: dict[int, tuple[int, str, str, float]]
    parent_state: dict[int, int]
    path_hoods: dict[int, frozenset[tuple[int, int]]]
    closed_set: set[int]
    closed_xy: set[int]
    start_at_mouth: bool = False
    source_hood_mouth: tuple[int, int] | None = None


def _collect_mst_ancestor_node_goal(
    pos: tuple[int, int],
    source: tuple[int, int],
    b_name: str,
    grid: RoutingGrid,
) -> tuple[int, int] | None:
    """Return a valid surface goal tile from an ancestor MST route node, or None."""
    if pos == source:
        return None
    if pos not in grid.active_hoods:
        return pos
    if pos not in grid.active_hood_types:
        return None
    h_type, h_dir = grid.active_hood_types[pos]
    m_dir = h_dir if h_type == "output" else OPPOSITE_DIR.get(h_dir, 0)
    mdx, mdy = DIR_STEPS[m_dir]
    m_pos = (pos[0] + mdx, pos[1] + mdy)
    if (
        m_pos != source
        and m_pos not in grid.active_hoods
        and any(base_net_name(cn) == b_name for cn in grid.surface_occupancy.get(m_pos, ()))
    ):
        return m_pos
    return None


def _iter_mst_ancestor_surface_positions(
    net: RouterNetSpec,
    grid: RoutingGrid,
    my_mst: int,
) -> Iterator[tuple[int, int]]:
    """Yield surface node coordinates from earlier successful MST branches of net."""
    mst_prefix = net.name.rsplit("_mst_", 1)[0]
    for i in range(my_mst):
        anc_route = grid.net_routes.get(f"{mst_prefix}_mst_{i}")
        if anc_route and anc_route.is_success:
            for node in anc_route.nodes:
                if node.z == LAYER_SURFACE:
                    yield node.pos


def _collect_mst_ancestor_fluid_goals(
    net: RouterNetSpec,
    grid: RoutingGrid,
    b_name: str,
    my_mst: int,
) -> set[tuple[int, int]]:
    """Collect reachable surface tiles from earlier MST branches of the same fluid net."""
    goals: set[tuple[int, int]] = set()
    for pos in _iter_mst_ancestor_surface_positions(net, grid, my_mst):
        goal_pt = _collect_mst_ancestor_node_goal(pos, net.source, b_name, grid)
        if goal_pt is not None:
            goals.add(goal_pt)
    return goals


def _collect_same_commodity_fluid_goals(
    net: RouterNetSpec,
    grid: RoutingGrid,
    b_name: str,
) -> set[tuple[int, int]]:
    """Collect non-hood surface tiles already carrying the same base fluid net."""
    goals: set[tuple[int, int]] = set()
    my_comm = grid.get_net_commodity(net.name)
    for pos, occ in grid.surface_occupancy.items():
        if (
            pos != net.source
            and pos not in grid.active_hoods
            and any(
                grid.is_net_fluid(cn)
                and grid.get_net_commodity(cn) == my_comm
                and base_net_name(cn) == b_name
                for cn in occ
            )
        ):
            goals.add(pos)
    return goals


def _collect_existing_fluid_goals(
    net: RouterNetSpec,
    grid: RoutingGrid,
    b_name: str,
    my_mst: int | None,
) -> set[tuple[int, int]]:
    """Return existing fluid surface tiles that can serve as shared Steiner goals."""
    if not net.is_fluid:
        return set()
    if my_mst is not None and my_mst > 0:
        return _collect_mst_ancestor_fluid_goals(net, grid, b_name, my_mst)
    if my_mst is None:
        return _collect_same_commodity_fluid_goals(net, grid, b_name)
    return set()


def _is_belt_sink_front_blocked(
    front_pos: tuple[int, int],
    b_name: str,
    net: RouterNetSpec,
    grid: RoutingGrid,
) -> bool:
    """Return True if the tile in front of a belt sink is blocked by another net or machine."""
    front_occ = grid.surface_occupancy.get(front_pos, ())
    if any(base_net_name(n) != b_name and not grid.is_net_fluid(n) for n in front_occ):
        return True
    front_res = grid.reserved_terminals.get(front_pos, ())
    if any(base_net_name(n) != b_name and not grid.is_net_fluid(n) for n in front_res):
        return True
    if (
        front_pos in grid.active_hoods
        and (grid.min_x > 0 or base_net_name(grid.active_hoods[front_pos]) != b_name)
        and not grid.is_net_fluid(grid.active_hoods[front_pos])
    ):
        return True
    return front_pos in grid.component_headings and net.terminal_heading is None


def _compute_valid_belt_end_headings(
    net: RouterNetSpec,
    grid: RoutingGrid,
    b_name: str,
    tier_ug_keys: tuple[int, ...],
) -> set[int]:
    """Return legal arrival headings at net.sink for a belt net."""
    valid: set[int] = set()
    sink_pos = net.sink
    tx, ty = sink_pos
    end_headings = [net.end_heading] if net.end_heading is not None else list(ALL_DIRECTIONS)
    for eh in end_headings:
        can_sink = grid.can_occupy_surface(
            sink_pos, net.name, net.lane, heading=eh, is_hood=False
        ) or (
            bool(tier_ug_keys)
            and grid.can_occupy_surface(sink_pos, net.name, net.lane, heading=eh, is_hood=True)
            and grid.can_place_hood(sink_pos, net.name, heading=eh, is_output=True)
        )
        if not can_sink:
            continue
        gdx, gdy = DIR_STEPS[eh]
        if not _is_belt_sink_front_blocked((tx + gdx, ty + gdy), b_name, net, grid):
            valid.add(eh)
    return valid


def _build_ug_min_steps(
    grid: RoutingGrid,
    tier_ug_keys: tuple[int, ...],
    is_fluid: bool,
) -> dict[int, int]:
    """Precompute the minimum leap step for each underground tier key."""
    if is_fluid:
        return dict.fromkeys(tier_ug_keys, 1)
    min_steps: dict[int, int] = {}
    for k_idx, kz in enumerate(tier_ug_keys):
        if k_idx == 0:
            min_steps[kz] = MIN_BELT_LEAP_STEP
        else:
            prev_span = grid.get_tier_span(tier_ug_keys[k_idx - 1])
            min_steps[kz] = max(MIN_BELT_LEAP_STEP, prev_span + 1)
    return min_steps


def _calc_highway_heuristic(ctx: _AStarContext, x: int, y: int) -> float:
    """Compute street-aware Manhattan heuristic for a highway net."""
    tx, ty = ctx.tx, ctx.ty
    dx_h = abs(x - tx)
    dy_h = abs(y - ty)
    if (
        BLOCK_MIN_INNER <= (x % BLOCK_PITCH) <= BLOCK_MAX_INNER
        and (y + BLOCK_MIN_INNER) // BLOCK_PITCH != ctx.snk_h_band
    ):
        bx_h = x // BLOCK_PITCH
        vx_w = bx_h * BLOCK_PITCH + STREET_WEST_NORTH_COORD
        vx_e = min(ctx.grid.max_x, bx_h * BLOCK_PITCH + STREET_EAST_SOUTH_COORD)
        dx_h = min(abs(x - vx_w) + abs(vx_w - tx), abs(x - vx_e) + abs(vx_e - tx))
    if (
        INTERIOR_MIN_Y <= (y % BLOCK_PITCH) <= BLOCK_MAX_INNER
        and (x + BLOCK_MIN_INNER) // BLOCK_PITCH != ctx.snk_v_band
    ):
        by_h = y // BLOCK_PITCH
        north_offset = BAY_MAX_Y if by_h in ctx.bay_rows else STREET_WEST_NORTH_COORD
        hy_n = by_h * BLOCK_PITCH + north_offset
        hy_s = min(ctx.grid.max_y, by_h * BLOCK_PITCH + STREET_EAST_SOUTH_COORD)
        dy_h = max(dy_h, min(abs(y - hy_n) + abs(hy_n - ty), abs(y - hy_s) + abs(hy_s - ty)))
    return HEURISTIC_WEIGHT * float(dx_h + dy_h)


def _calc_h(ctx: _AStarContext, x: int, y: int) -> float:
    """Compute the A* heuristic estimate from (x, y) to the closest goal."""
    if ctx.fluid_goals_rest:
        best_d = abs(x - ctx.tx) + abs(y - ctx.ty)
        for gx, gy in ctx.fluid_goals_rest:
            d = abs(x - gx) + abs(y - gy)
            if d < best_d:
                best_d = d
        return HEURISTIC_WEIGHT * float(best_d)
    if ctx.is_hwy:
        return _calc_highway_heuristic(ctx, x, y)
    return HEURISTIC_WEIGHT * float(abs(x - ctx.tx) + abs(y - ctx.ty))


def _in_ancestor_xy(ctx: _AStarContext, state_id: int, xy_key: int) -> bool:
    """Return True if xy_key appears in the ancestor path of state_id."""
    if xy_key not in ctx.closed_xy:
        return False
    cur: int | None = state_id
    while cur is not None:
        if (cur >> STATE_Y_SHIFT) == xy_key:
            return True
        cur = ctx.parent_state.get(cur)
    return False


def _push_start_state(ctx: _AStarContext, x: int, y: int, heading: int) -> None:
    """Add an initial surface state at (x, y) with heading to ctx.open_set."""
    s = pack_state(x, y, LAYER_SURFACE, heading >> HEADING_SHIFT)
    ctx.path_hoods[s] = frozenset()
    base_start_cost = ctx.grid.get_fast_node_cost(x, y, LAYER_SURFACE, 0.0, ctx.net_name, ctx.lane)
    ctx.g_score[s] = base_start_cost
    h_val = _calc_h(ctx, x, y)
    heapq.heappush(ctx.open_set, (base_start_cost + h_val, h_val, s))


def _try_seed_mouth_start_states(ctx: _AStarContext) -> bool:
    """Seed start states at the mouth of an active source hood if applicable."""
    grid = ctx.grid
    src = ctx.net.source
    if not (
        ctx.is_fluid
        and src in grid.active_hoods
        and base_net_name(grid.active_hoods[src]) == ctx.b_name
    ):
        return False
    h_type, h_dir = grid.active_hood_types.get(src, ("output", 0))
    m_dir = h_dir if h_type == "output" else OPPOSITE_DIR.get(h_dir, 0)
    mdx, mdy = DIR_STEPS[m_dir]
    m_pos = (src[0] + mdx, src[1] + mdy)
    if grid.is_in_bounds(m_pos[0], m_pos[1]):
        ctx.start_at_mouth = True
        ctx.source_hood_mouth = m_pos
        for h in ALL_DIRECTIONS:
            if grid.can_occupy_surface(m_pos, ctx.net_name, ctx.lane, heading=h):
                _push_start_state(ctx, m_pos[0], m_pos[1], h)
    return True


def _seed_initial_open_states(ctx: _AStarContext) -> None:
    """Populate ctx.open_set with legal starting states at net.source or its hood mouth."""
    if _try_seed_mouth_start_states(ctx):
        return
    grid = ctx.grid
    src = ctx.net.source
    start_headings = (
        [ctx.net.start_heading] if ctx.net.start_heading is not None else list(ALL_DIRECTIONS)
    )
    for h in start_headings:
        can_surf = grid.can_occupy_surface(src, ctx.net_name, ctx.lane, heading=h, is_hood=False)
        can_hood = (
            ctx.is_fluid
            and grid.can_occupy_surface(src, ctx.net_name, ctx.lane, heading=h, is_hood=True)
            and grid.can_place_hood(src, ctx.net_name, heading=h, is_output=False)
        )
        if can_surf or can_hood:
            _push_start_state(ctx, ctx.sx, ctx.sy, h)


def _is_sink_goal_reached(
    ctx: _AStarContext,
    current: int,
    ch: int,
    prev_move: str | None,
    can_surf_here: bool,
    curr_hoods: frozenset[tuple[int, int]],
) -> bool:
    """Return True if current at (tx, ty) satisfies terminal heading and hood rules."""
    if ctx.net.end_heading is not None and ch != ctx.net.end_heading:
        return False
    if ctx.is_fluid and prev_move == "leap":
        inward = ctx.grid.fluid_terminal_inward_dir.get(ctx.sink_pos)
        return (inward is None or ch == inward) and (
            ctx.my_mst is None or (ctx.sx, ctx.sy) not in curr_hoods
        )
    if not ctx.is_fluid:
        gdx, gdy = DIR_STEPS[ch]
        f_xy = pack_xy(ctx.tx + gdx, ctx.ty + gdy)
        return ch in ctx.valid_end_headings and (
            ctx.grid.min_x <= 0 or not _in_ancestor_xy(ctx, current, f_xy)
        )
    return can_surf_here


def _is_goal_reached(
    ctx: _AStarContext,
    current: int,
    cx: int,
    cy: int,
    cz: int,
    ch: int,
    prev_move: str | None,
    can_surf_here: bool,
    curr_hoods: frozenset[tuple[int, int]],
) -> bool:
    """Return True if state current on the surface has reached the sink or a shared fluid goal."""
    if cz != LAYER_SURFACE:
        return False
    if (
        cx == ctx.tx
        and cy == ctx.ty
        and _is_sink_goal_reached(ctx, current, ch, prev_move, can_surf_here, curr_hoods)
    ):
        return True
    if not ctx.is_fluid:
        return False
    if (cx, cy) in ctx.existing_fluid_goals and prev_move != "leap" and can_surf_here:
        return True
    if prev_move == "leap":
        mdx, mdy = DIR_STEPS[ch]
        if (cx + mdx, cy + mdy) in ctx.existing_fluid_goals:
            return True
    return False


def _push_neighbor_state(
    ctx: _AStarContext,
    current: int,
    nxt_s: int,
    nx: int,
    ny: int,
    tentative_g: float,
    edge_cost: float,
    move_type: str,
    tier_name: str,
    hoods: frozenset[tuple[int, int]],
) -> None:
    """Record a lower-cost path to nxt_s and push it onto ctx.open_set."""
    ctx.g_score[nxt_s] = tentative_g
    ctx.came_from[nxt_s] = (current, move_type, tier_name, edge_cost)
    ctx.parent_state[nxt_s] = current
    ctx.path_hoods[nxt_s] = hoods
    h_val = _calc_h(ctx, nx, ny)
    heapq.heappush(ctx.open_set, (tentative_g + h_val, h_val, nxt_s))


def _is_forbidden_highway_bay(ctx: _AStarContext, x: int, y: int) -> bool:
    """Return True if highway net ctx may not enter bay tile (x, y)."""
    if not ctx.is_hwy or not _is_bay_tile(x, y):
        return False
    bay = (x // BLOCK_PITCH, y // BLOCK_PITCH)
    return bay != ctx.src_bay and bay != ctx.snk_bay


def _can_step_straight_onto(
    ctx: _AStarContext,
    npos: tuple[int, int],
    ch: int,
    prev_move: str | None,
) -> bool:
    """Return True if net can step straight onto surface tile npos."""
    grid = ctx.grid
    if grid.can_occupy_surface(npos, ctx.net_name, ctx.lane, heading=ch):
        return True
    return (
        ctx.is_fluid
        and prev_move != "leap"
        and grid.can_occupy_surface(npos, ctx.net_name, ctx.lane, heading=ch, is_hood=True)
        and grid.can_place_hood(npos, ctx.net_name, heading=ch, is_output=False)
    )


def _try_surface_straight_step(
    ctx: _AStarContext,
    current: int,
    npos: tuple[int, int],
    nx: int,
    ny: int,
    ch: int,
    ch_idx: int,
    curr_g: float,
    prev_move: str | None,
    next_visited: frozenset[tuple[int, int]],
) -> None:
    """Push a straight surface transition onto npos if legal and lower cost."""
    nxt_s = pack_state(nx, ny, LAYER_SURFACE, ch_idx)
    if nxt_s in ctx.closed_set:
        return
    if not ctx.is_fluid and nx == ctx.tx and ny == ctx.ty and ch not in ctx.valid_end_headings:
        return
    edge_cost = ctx.grid.get_fast_node_cost(
        nx, ny, LAYER_SURFACE, STEP_COST, ctx.net_name, ctx.lane, heading=ch
    )
    tentative_g = curr_g + edge_cost
    if tentative_g < ctx.g_score.get(nxt_s, float("inf")) and _can_step_straight_onto(
        ctx, npos, ch, prev_move
    ):
        _push_neighbor_state(
            ctx,
            current,
            nxt_s,
            nx,
            ny,
            tentative_g,
            edge_cost,
            "straight",
            ctx.tier_name_surface,
            next_visited,
        )


def _try_surface_turn_steps(
    ctx: _AStarContext,
    current: int,
    npos: tuple[int, int],
    nx: int,
    ny: int,
    ch: int,
    curr_g: float,
    next_visited: frozenset[tuple[int, int]],
) -> None:
    """Push 90-degree surface turn transitions onto npos if legal and lower cost."""
    grid = ctx.grid
    for th in PERPENDICULAR_DIRS[ch]:
        if not ctx.is_fluid and nx == ctx.tx and ny == ctx.ty and th not in ctx.valid_end_headings:
            continue
        nxt_turn = pack_state(nx, ny, LAYER_SURFACE, th >> HEADING_SHIFT)
        if nxt_turn in ctx.closed_set:
            continue
        edge_cost = grid.get_fast_node_cost(
            nx, ny, LAYER_SURFACE, STEP_COST + TURN_PENALTY, ctx.net_name, ctx.lane, heading=th
        )
        tentative_g = curr_g + edge_cost
        if tentative_g < ctx.g_score.get(nxt_turn, float("inf")) and grid.can_occupy_surface(
            npos, ctx.net_name, ctx.lane, heading=th
        ):
            _push_neighbor_state(
                ctx,
                current,
                nxt_turn,
                nx,
                ny,
                tentative_g,
                edge_cost,
                "turn",
                ctx.tier_name_surface,
                next_visited,
            )


def _expand_surface_straight_and_turns(
    ctx: _AStarContext,
    current: int,
    cx: int,
    cy: int,
    ch: int,
    ch_idx: int,
    dx: int,
    dy: int,
    curr_g: float,
    prev_move: str | None,
    next_visited: frozenset[tuple[int, int]],
) -> None:
    """Expand straight and 90-degree turn transitions from surface tile (cx, cy)."""
    nx = cx + dx
    ny = cy + dy
    if not (ctx.bx0 <= nx <= ctx.bx1 and ctx.by0 <= ny <= ctx.by1):
        return
    if _is_forbidden_highway_bay(ctx, nx, ny):
        return
    if _in_ancestor_xy(ctx, current, pack_xy(nx, ny)):
        return
    npos = (nx, ny)
    _try_surface_straight_step(
        ctx, current, npos, nx, ny, ch, ch_idx, curr_g, prev_move, next_visited
    )
    _try_surface_turn_steps(ctx, current, npos, nx, ny, ch, curr_g, next_visited)


def _can_initiate_dive(
    ctx: _AStarContext,
    cpos: tuple[int, int],
    cx: int,
    cy: int,
    ch: int,
    dx: int,
    dy: int,
    prev_move: str | None,
    can_surf_here: bool,
    curr_hoods: frozenset[tuple[int, int]],
) -> bool:
    """Return True if a subterranean dive can start at surface tile cpos."""
    if prev_move in ("turn", "leap") or not ctx.tier_ug_keys or cpos in curr_hoods:
        return False
    grid = ctx.grid
    if (
        ctx.is_fluid
        and cpos in grid.fluid_terminal_inward_dir
        and OPPOSITE_DIR.get(ch, 0) != grid.fluid_terminal_inward_dir[cpos]
    ):
        return False
    warrants_dive = (
        ctx.is_fluid
        and (not can_surf_here or abs(cx - ctx.tx) + abs(cy - ctx.ty) <= ctx.max_ug_span)
    ) or grid.should_consider_underground(
        cx, cy, dx, dy, ctx.max_ug_span, ctx.net_name, ctx.lane, target_pos=ctx.sink_pos
    )
    if not warrants_dive or not grid.can_place_hood(
        cpos, ctx.net_name, heading=ch, is_output=False
    ):
        return False
    return not ctx.is_fluid or grid.can_occupy_surface(
        cpos, ctx.net_name, ctx.lane, heading=ch, is_hood=True
    )


def _is_dive_span_blocked_within(
    ctx: _AStarContext,
    cx: int,
    cy: int,
    dx: int,
    dy: int,
    reach: int,
    dive_axis_spans: Mapping[tuple[int, int], str],
    curr_hoods: frozenset[tuple[int, int]],
) -> bool:
    """Return True if a belt dive is blocked within [1, reach] tiles ahead."""
    for s_chk in range(1, reach + 1):
        ex_c = cx + dx * s_chk
        ey_c = cy + dy * s_chk
        if not (ctx.bx0 <= ex_c <= ctx.bx1 and ctx.by0 <= ey_c <= ctx.by1):
            return True
        pt_c = (ex_c, ey_c)
        if pt_c in dive_axis_spans or pt_c in curr_hoods:
            return True
        if pt_c in ctx.grid.active_hoods and not ctx.grid.is_net_fluid(ctx.grid.active_hoods[pt_c]):
            return True
    return False


def _push_dive_tiers(
    ctx: _AStarContext,
    current: int,
    x: int,
    y: int,
    ch_idx: int,
    dx: int,
    dy: int,
    curr_g: float,
    curr_hoods: frozenset[tuple[int, int]],
    new_dive_hoods: frozenset[tuple[int, int]],
    check_early_block: bool,
) -> None:
    """Push dive transitions across ctx.tier_ug_keys at (x, y)."""
    grid = ctx.grid
    dive_axis_spans = grid._domain_span_map(ctx.is_fluid, dx != 0)
    tiers = grid.pipe_tiers if ctx.is_fluid else grid.belt_tiers
    prev_reach = 0
    for target_z in ctx.tier_ug_keys:
        t_reach = grid.get_tier_span(target_z)
        if (
            check_early_block
            and not ctx.is_fluid
            and prev_reach > 0
            and _is_dive_span_blocked_within(
                ctx, x, y, dx, dy, prev_reach, dive_axis_spans, curr_hoods
            )
        ):
            break
        prev_reach = t_reach
        nxt_s = pack_state(x, y, target_z, ch_idx)
        if nxt_s in ctx.closed_set:
            continue
        tier_cost = grid.get_tier_cost(target_z)
        edge_cost = grid.get_fast_node_cost(
            x, y, target_z, HOOD_PENALTY + tier_cost, ctx.net_name, ctx.lane
        )
        tentative_g = curr_g + edge_cost
        if tentative_g < ctx.g_score.get(nxt_s, float("inf")):
            _push_neighbor_state(
                ctx,
                current,
                nxt_s,
                x,
                y,
                tentative_g,
                edge_cost,
                "dive",
                tiers[target_z]["name"],
                new_dive_hoods,
            )


def _expand_underground_dives(
    ctx: _AStarContext,
    current: int,
    cx: int,
    cy: int,
    ch: int,
    ch_idx: int,
    dx: int,
    dy: int,
    curr_g: float,
    prev_move: str | None,
    can_surf_here: bool,
    curr_hoods: frozenset[tuple[int, int]],
) -> None:
    """Expand subterranean dive transitions from surface tile (cx, cy)."""
    cpos = (cx, cy)
    if _can_initiate_dive(ctx, cpos, cx, cy, ch, dx, dy, prev_move, can_surf_here, curr_hoods):
        _push_dive_tiers(
            ctx,
            current,
            cx,
            cy,
            ch_idx,
            dx,
            dy,
            curr_g,
            curr_hoods,
            curr_hoods | {cpos},
            check_early_block=True,
        )

    if prev_move == "leap" and ctx.is_fluid and ctx.tier_ug_keys:
        nx = cx + dx
        ny = cy + dy
        npos = (nx, ny)
        grid = ctx.grid
        if (
            grid.is_in_bounds(nx, ny)
            and npos not in curr_hoods
            and not _in_ancestor_xy(ctx, current, pack_xy(nx, ny))
            and grid.can_place_hood(npos, ctx.net_name, heading=ch, is_output=False)
            and grid.can_occupy_surface(npos, ctx.net_name, ctx.lane, heading=ch, is_hood=True)
        ):
            _push_dive_tiers(
                ctx,
                current,
                nx,
                ny,
                ch_idx,
                dx,
                dy,
                curr_g,
                curr_hoods,
                curr_hoods | {npos},
                check_early_block=False,
            )


def _is_leap_prefix_blocked(
    ctx: _AStarContext,
    cx: int,
    cy: int,
    dx: int,
    dy: int,
    min_step: int,
    axis_spans: Mapping[tuple[int, int], str],
    curr_hoods: frozenset[tuple[int, int]],
) -> bool:
    """Return True if any tile before min_step blocks a subterranean tunnel."""
    for s_pre in range(1, min_step):
        ex_p = cx + dx * s_pre
        ey_p = cy + dy * s_pre
        if not (ctx.bx0 <= ex_p <= ctx.bx1 and ctx.by0 <= ey_p <= ctx.by1):
            return True
        pt_p = (ex_p, ey_p)
        if pt_p in axis_spans or pt_p in curr_hoods:
            return True
        if pt_p in ctx.grid.active_hoods and (
            ctx.grid.is_net_fluid(ctx.grid.active_hoods[pt_p]) == ctx.is_fluid
        ):
            return True
    return False


def _can_complete_leap_at(
    ctx: _AStarContext,
    current: int,
    ex: int,
    ey: int,
    ch: int,
    dx: int,
    dy: int,
    step: int,
) -> bool:
    """Return True if a subterranean leap can surface with an output hood at (ex, ey)."""
    if _is_forbidden_highway_bay(ctx, ex, ey):
        return False
    if not ctx.is_fluid and ex == ctx.tx and ey == ctx.ty and ch not in ctx.valid_end_headings:
        return False
    if _in_ancestor_xy(ctx, current, pack_xy(ex, ey)):
        return False
    if (
        not ctx.is_fluid
        and ctx.grid.min_x > 0
        and step > 1
        and _in_ancestor_xy(ctx, current, pack_xy(ex - dx, ey - dy))
    ):
        return False
    pt = (ex, ey)
    return ctx.grid.can_place_hood(
        pt, ctx.net_name, heading=ch, is_output=True
    ) and ctx.grid.can_occupy_surface(pt, ctx.net_name, ctx.lane, heading=ch, is_hood=True)


def _try_push_leap_exit(
    ctx: _AStarContext,
    current: int,
    cx: int,
    cy: int,
    ex: int,
    ey: int,
    ch: int,
    ch_idx: int,
    dx: int,
    dy: int,
    step: int,
    curr_g: float,
    tier_name: str,
    curr_hoods: frozenset[tuple[int, int]],
) -> None:
    """Push a subterranean leap exit state at (ex, ey) if legal and lower cost."""
    nxt_s = pack_state(ex, ey, LAYER_SURFACE, ch_idx)
    if nxt_s in ctx.closed_set:
        return
    edge_cost = ctx.grid.get_fast_node_cost(
        ex,
        ey,
        LAYER_SURFACE,
        step * UNDERGROUND_STEP_COST + HOOD_PENALTY,
        ctx.net_name,
        ctx.lane,
        heading=ch,
    )
    tentative_g = curr_g + edge_cost
    if tentative_g < ctx.g_score.get(nxt_s, float("inf")) and _can_complete_leap_at(
        ctx, current, ex, ey, ch, dx, dy, step
    ):
        leap_hoods = curr_hoods | {(cx + dx * s, cy + dy * s) for s in range(1, step + 1)}
        _push_neighbor_state(
            ctx, current, nxt_s, ex, ey, tentative_g, edge_cost, "leap", tier_name, leap_hoods
        )


def _is_leap_step_blocked(
    ctx: _AStarContext,
    ex: int,
    ey: int,
    pt: tuple[int, int],
    axis_spans: Mapping[tuple[int, int], str],
    curr_hoods: frozenset[tuple[int, int]],
) -> bool:
    """Return True if tunnel tile pt=(ex, ey) terminates a subterranean leap."""
    if not (ctx.bx0 <= ex <= ctx.bx1 and ctx.by0 <= ey <= ctx.by1):
        return True
    if pt in axis_spans or pt in curr_hoods:
        return True
    return pt in ctx.grid.active_hoods and (
        ctx.grid.is_net_fluid(ctx.grid.active_hoods[pt]) == ctx.is_fluid
    )


def _expand_subterranean_leaps(
    ctx: _AStarContext,
    current: int,
    cx: int,
    cy: int,
    cz: int,
    ch: int,
    ch_idx: int,
    dx: int,
    dy: int,
    curr_g: float,
    curr_hoods: frozenset[tuple[int, int]],
) -> None:
    """Expand subterranean leap transitions from (cx, cy, cz) back to the surface."""
    grid = ctx.grid
    max_span = grid.get_tier_span(cz)
    axis_spans = grid._domain_span_map(ctx.is_fluid, dx != 0)
    min_step = ctx.ug_min_steps.get(cz, MIN_BELT_LEAP_STEP)
    if _is_leap_prefix_blocked(ctx, cx, cy, dx, dy, min_step, axis_spans, curr_hoods):
        return

    tiers = grid.pipe_tiers if ctx.is_fluid else grid.belt_tiers
    default_tier = "pipe-to-ground" if ctx.is_fluid else "underground-belt"
    tier_name = tiers.get(cz, {}).get("name", default_tier)

    for step in range(min_step, max_span + 1):
        ex = cx + dx * step
        ey = cy + dy * step
        pt = (ex, ey)
        if _is_leap_step_blocked(ctx, ex, ey, pt, axis_spans, curr_hoods):
            break
        if pt in grid.active_hoods:
            continue
        _try_push_leap_exit(
            ctx, current, cx, cy, ex, ey, ch, ch_idx, dx, dy, step, curr_g, tier_name, curr_hoods
        )


def _build_failed_route_result(ctx: _AStarContext) -> RouteResult:
    """Construct a diagnostic RouteResult when A* search exhausts open_set without a goal."""
    coords = sorted(
        {(unpack_state(s)[0], unpack_state(s)[1], unpack_state(s)[2]) for s in ctx.closed_set}
    )
    surf_pts = sorted(
        {(cx_pt, cy_pt) for cx_pt, cy_pt, cz_pt in coords if cz_pt == LAYER_SURFACE},
        key=lambda pt: abs(pt[0] - ctx.tx) + abs(pt[1] - ctx.ty),
    )
    frontier = tuple(surf_pts[:FRONTIER_SAMPLE_LIMIT])
    return RouteResult(
        net_name=ctx.net_name,
        nodes=[],
        edges=[],
        cost=float("inf"),
        is_success=False,
        error_message=(
            f"No route found from {ctx.net.source} to {ctx.net.sink}. "
            f"Explored {len(ctx.closed_set)} states. Frontier: {frontier}. "
            f"Coords: {coords[:ERROR_COORD_SAMPLE_LIMIT]}."
        ),
        failed_frontier=frontier,
    )


def _reconstruct_route_result(ctx: _AStarContext, goal_state: int) -> RouteResult:
    """Reconstruct the ordered nodes and edges from ctx.came_from ending at goal_state."""
    curr_s = goal_state
    cx, cy, cz, ch, _ = unpack_state(curr_s)
    nodes: list[RouteNode] = [RouteNode(cx, cy, cz, ch, ctx.lane)]
    edges: list[RouteEdge] = []

    while curr_s in ctx.came_from:
        prev_s, move_type, tier_name, edge_cost = ctx.came_from[curr_s]
        px, py, pz, ph, _ = unpack_state(prev_s)
        prev_node = RouteNode(px, py, pz, ph, ctx.lane)
        edges.append(RouteEdge(prev_node, nodes[-1], move_type, tier_name, edge_cost))
        nodes.append(prev_node)
        curr_s = prev_s

    if ctx.start_at_mouth and ctx.source_hood_mouth is not None and nodes:
        src = ctx.net.source
        h_type, h_dir = ctx.grid.active_hood_types.get(src, ("output", 0))
        m_dir = h_dir if h_type == "output" else OPPOSITE_DIR.get(h_dir, 0)
        first_node = RouteNode(src[0], src[1], LAYER_SURFACE, m_dir, ctx.lane)
        edge_cost = ctx.grid.get_fast_node_cost(
            src[0], src[1], LAYER_SURFACE, STEP_COST, ctx.net_name, ctx.lane
        )
        connecting_edge = RouteEdge(
            first_node, nodes[-1], "straight", ctx.tier_name_surface, edge_cost
        )
        nodes.append(first_node)
    else:
        connecting_edge = None

    nodes.reverse()
    edges.reverse()
    if connecting_edge is not None:
        edges.append(connecting_edge)
    return RouteResult(
        net_name=ctx.net_name,
        nodes=nodes,
        edges=edges,
        cost=ctx.g_score[goal_state],
        is_success=True,
    )


def _create_astar_context(net: RouterNetSpec, grid: RoutingGrid) -> _AStarContext:
    """Initialize and return the _AStarContext for routing net on grid."""
    sx, sy = net.source
    tx, ty = net.sink
    net_name = net.name
    b_name = base_net_name(net_name)
    my_mst = mst_index(net_name)
    is_fluid = net.is_fluid
    tier_ug_keys = grid.pipe_tier_keys if is_fluid else grid.belt_tier_keys
    bx0, by0, bx1, by1 = (
        grid.active_bbox
        if grid.active_bbox is not None
        else (grid.min_x, grid.min_y, grid.max_x, grid.max_y)
    )
    existing_fluid_goals = _collect_existing_fluid_goals(net, grid, b_name, my_mst)
    fluid_goals_rest: tuple[tuple[int, int], ...] = ()
    if is_fluid and existing_fluid_goals:
        sorted_goals = sorted(existing_fluid_goals, key=lambda g: abs(g[0] - sx) + abs(g[1] - sy))
        fluid_goals_rest = tuple(sorted_goals[:MAX_FLUID_GOAL_CANDIDATES])

    src_bay = (sx // BLOCK_PITCH, sy // BLOCK_PITCH) if _is_bay_tile(sx, sy) else None
    snk_bay = (tx // BLOCK_PITCH, ty // BLOCK_PITCH) if _is_bay_tile(tx, ty) else None
    valid_end_headings = (
        set() if is_fluid else _compute_valid_belt_end_headings(net, grid, b_name, tier_ug_keys)
    )

    return _AStarContext(
        net=net,
        grid=grid,
        sx=sx,
        sy=sy,
        tx=tx,
        ty=ty,
        sink_pos=(tx, ty),
        net_name=net_name,
        b_name=b_name,
        my_mst=my_mst,
        lane=net.lane,
        is_fluid=is_fluid,
        tier_name_surface="pipe" if is_fluid else "transport-belt",
        tier_ug_keys=tier_ug_keys,
        ug_min_steps=_build_ug_min_steps(grid, tier_ug_keys, is_fluid),
        max_ug_span=grid.max_pipe_span if is_fluid else grid.max_belt_span,
        bx0=bx0,
        by0=by0,
        bx1=bx1,
        by1=by1,
        existing_fluid_goals=existing_fluid_goals,
        fluid_goals_rest=fluid_goals_rest,
        is_hwy=HIGHWAY_SUFFIX in net_name,
        src_bay=src_bay,
        snk_bay=snk_bay,
        snk_h_band=(ty + BLOCK_MIN_INNER) // BLOCK_PITCH,
        snk_v_band=(tx + BLOCK_MIN_INNER) // BLOCK_PITCH,
        bay_rows={b[1] for b in (src_bay, snk_bay) if b is not None},
        valid_end_headings=valid_end_headings,
        open_set=[],
        g_score={},
        came_from={},
        parent_state={},
        path_hoods={},
        closed_set=set(),
        closed_xy=set(),
    )


def _compute_max_search_states(ctx: _AStarContext) -> int:
    """Compute the state exploration budget for ctx."""
    span = abs(ctx.sx - ctx.tx) + abs(ctx.sy - ctx.ty)
    box_area = max(MIN_BOX_AREA, (ctx.bx1 - ctx.bx0 + 1) * (ctx.by1 - ctx.by0 + 1))
    cap_states = (
        HIGHWAY_STATE_CAP if (ctx.is_hwy or span > LONG_SPAN_THRESHOLD) else LOCAL_STATE_CAP
    )
    return min(cap_states, max(MIN_STATE_BUDGET, box_area * BOX_AREA_STATE_MULTIPLIER))


def _check_pre_search_failure(ctx: _AStarContext) -> RouteResult | None:
    """Return an immediate failure RouteResult if source or belt sink is completely blocked."""
    if not ctx.open_set:
        return RouteResult(
            net_name=ctx.net_name,
            nodes=[],
            edges=[],
            cost=float("inf"),
            is_success=False,
            error_message=f"Source {ctx.net.source} is blocked.",
            failed_frontier=(ctx.net.source,),
        )
    if not ctx.is_fluid and not ctx.valid_end_headings:
        return RouteResult(
            net_name=ctx.net_name,
            nodes=[],
            edges=[],
            cost=float("inf"),
            is_success=False,
            error_message=f"Sink {ctx.sink_pos} or its front tile is blocked.",
            failed_frontier=(ctx.sink_pos,),
        )
    return None


def _expand_popped_state(ctx: _AStarContext, current: int) -> bool:
    """Expand neighbors of current state; return True if current reached the goal."""
    cx, cy, cz, ch, ch_idx = unpack_state(current)
    curr_hoods = ctx.path_hoods.get(current, frozenset())
    prev_move = ctx.came_from[current][1] if current in ctx.came_from else None
    can_surf_here = not (
        cz == LAYER_SURFACE
        and ctx.is_fluid
        and prev_move != "leap"
        and not ctx.grid.can_occupy_surface(
            (cx, cy), ctx.net_name, ctx.lane, heading=ch, is_hood=False
        )
    )
    if _is_goal_reached(ctx, current, cx, cy, cz, ch, prev_move, can_surf_here, curr_hoods):
        return True

    curr_g = ctx.g_score[current]
    dx, dy = DIR_STEPS[ch]
    if cz == LAYER_SURFACE:
        if can_surf_here:
            next_visited = (curr_hoods | {(cx, cy)}) if ctx.is_fluid else curr_hoods
            _expand_surface_straight_and_turns(
                ctx, current, cx, cy, ch, ch_idx, dx, dy, curr_g, prev_move, next_visited
            )
        _expand_underground_dives(
            ctx, current, cx, cy, ch, ch_idx, dx, dy, curr_g, prev_move, can_surf_here, curr_hoods
        )
    else:
        _expand_subterranean_leaps(ctx, current, cx, cy, cz, ch, ch_idx, dx, dy, curr_g, curr_hoods)
    return False


def _a_star_search(
    net: RouterNetSpec,
    grid: RoutingGrid,
) -> RouteResult:
    """Execute directed 3D A* search on the routing grid."""
    if net.source == net.sink:
        heading = net.start_heading if net.start_heading is not None else 0
        node = RouteNode(net.source[0], net.source[1], LAYER_SURFACE, heading, net.lane)
        return RouteResult(net.name, [node], [], 0.0, is_success=True)

    ctx = _create_astar_context(net, grid)
    _seed_initial_open_states(ctx)
    pre_fail = _check_pre_search_failure(ctx)
    if pre_fail is not None:
        return pre_fail

    max_states = _compute_max_search_states(ctx)
    while ctx.open_set and len(ctx.closed_set) < max_states:
        _, _, current = heapq.heappop(ctx.open_set)
        if current in ctx.closed_set:
            continue
        ctx.closed_set.add(current)
        ctx.closed_xy.add(current >> STATE_Y_SHIFT)
        if _expand_popped_state(ctx, current):
            return _reconstruct_route_result(ctx, current)

    return _build_failed_route_result(ctx)


def _find_closest_mst_ancestor_goal(
    net: RouterNetSpec,
    grid: RoutingGrid,
    my_mst: int,
) -> tuple[int, int] | None:
    """Return the nearest surface node from an earlier MST branch of net, or None."""
    closest_goal: tuple[int, int] | None = None
    best_d = float("inf")
    for pos in _iter_mst_ancestor_surface_positions(net, grid, my_mst):
        if pos != net.source and pos not in grid.active_hoods:
            d = abs(pos[0] - net.source[0]) + abs(pos[1] - net.source[1])
            if d < best_d:
                best_d = d
                closest_goal = pos
    return closest_goal


def _compute_initial_search_bbox(
    net: RouterNetSpec,
    grid: RoutingGrid,
    margin: int,
    min_x: int,
    max_x: int,
    min_y: int,
    max_y: int,
) -> tuple[int, int, int, int]:
    """Compute the initial active bounding box for route_single_net."""
    bx_min = max(grid.min_x, min_x - margin)
    by_min = max(grid.min_y, min_y - margin)
    bx_max = min(grid.max_x, max_x + margin)
    by_max = min(grid.max_y, max_y + margin)
    if net.corridor_bbox is None:
        return (bx_min, by_min, bx_max, by_max)
    cbx0, cby0, cbx1, cby1 = net.corridor_bbox
    if HIGHWAY_SUFFIX in net.name:
        return (
            max(grid.min_x, cbx0),
            max(grid.min_y, cby0),
            min(grid.max_x, cbx1),
            min(grid.max_y, cby1),
        )
    return (
        max(cbx0, bx_min),
        max(cby0, by_min),
        min(cbx1, bx_max),
        min(cby1, by_max),
    )


def _compute_fallback_search_bbox(
    net: RouterNetSpec,
    grid: RoutingGrid,
    margin: int,
    min_x: int,
    max_x: int,
    min_y: int,
    max_y: int,
) -> tuple[int, int, int, int]:
    """Compute the widened fallback bounding box when initial search fails."""
    if net.corridor_bbox is not None and HIGHWAY_SUFFIX not in net.name:
        cbx0, cby0, cbx1, cby1 = net.corridor_bbox
        return (
            max(grid.min_x, cbx0),
            max(grid.min_y, cby0),
            min(grid.max_x, cbx1),
            min(grid.max_y, cby1),
        )
    if HIGHWAY_SUFFIX in net.name and net.corridor_bbox is not None:
        cbx0, cby0, cbx1, cby1 = net.corridor_bbox
        return (
            max(grid.min_x, cbx0 - HIGHWAY_FALLBACK_PAD),
            max(grid.min_y, cby0 - HIGHWAY_FALLBACK_PAD),
            min(grid.max_x, cbx1 + HIGHWAY_FALLBACK_PAD),
            min(grid.max_y, cby1 + HIGHWAY_FALLBACK_PAD),
        )
    if HIGHWAY_SUFFIX in net.name:
        return (grid.min_x, grid.min_y, grid.max_x, grid.max_y)
    wide = max(margin * WIDE_FALLBACK_MULTIPLIER, MIN_WIDE_FALLBACK_MARGIN)
    return (
        max(grid.min_x, min_x - wide),
        max(grid.min_y, min_y - wide),
        min(grid.max_x, max_x + wide),
        min(grid.max_y, max_y + wide),
    )


def _compute_effective_margin(net: RouterNetSpec, span: int, margin: int) -> int:
    """Return the search margin adjusted for long spans and fluid nets."""
    eff = margin
    if span > LONG_SPAN_THRESHOLD and HIGHWAY_SUFFIX not in net.name:
        eff = max(eff, min(MAX_SPAN_MARGIN, span // SPAN_MARGIN_DIVISOR))
    if net.is_fluid:
        eff = max(eff, FLUID_MIN_MARGIN)
    return eff


def _compute_net_pin_bounds(
    net: RouterNetSpec,
    grid: RoutingGrid,
) -> tuple[int, int, int, int]:
    """Return (min_x, max_x, min_y, max_y) covering net pins and closest MST ancestor."""
    min_x = min(net.source[0], net.sink[0])
    max_x = max(net.source[0], net.sink[0])
    min_y = min(net.source[1], net.sink[1])
    max_y = max(net.source[1], net.sink[1])
    my_mst = mst_index(net.name)
    if net.is_fluid and my_mst is not None and my_mst > 0:
        closest_goal = _find_closest_mst_ancestor_goal(net, grid, my_mst)
        if closest_goal is not None:
            min_x = min(min_x, closest_goal[0])
            max_x = max(max_x, closest_goal[0])
            min_y = min(min_y, closest_goal[1])
            max_y = max(max_y, closest_goal[1])
    return min_x, max_x, min_y, max_y


def route_single_net(
    net: RouterNetSpec,
    grid: RoutingGrid,
    margin: int = DEFAULT_SEARCH_MARGIN,
) -> RouteResult:
    """Find a minimum-cost directed path for one net with spatial pruning."""
    span = abs(net.source[0] - net.sink[0]) + abs(net.source[1] - net.sink[1])
    eff_margin = _compute_effective_margin(net, span, margin)
    min_x, max_x, min_y, max_y = _compute_net_pin_bounds(net, grid)

    first_bbox = _compute_initial_search_bbox(net, grid, eff_margin, min_x, max_x, min_y, max_y)
    grid.active_bbox = first_bbox
    res = _a_star_search(net, grid)
    if not res.is_success and "is blocked." not in (res.error_message or ""):
        first_frontier = res.failed_frontier
        fallback_bbox = _compute_fallback_search_bbox(
            net, grid, eff_margin, min_x, max_x, min_y, max_y
        )
        if fallback_bbox != first_bbox:
            grid.active_bbox = fallback_bbox
            res = _a_star_search(net, grid)
            if not res.is_success and first_frontier:
                merged_f = tuple(dict.fromkeys((*first_frontier, *res.failed_frontier)))
                res.failed_frontier = merged_f[:MERGED_FRONTIER_LIMIT]
    grid.active_bbox = None
    return res


def _is_leap_edge_over_capacity(edge: RouteEdge, grid: RoutingGrid) -> bool:
    """Return True if any subterranean node along a leap edge exceeds capacity."""
    start = edge.start
    end = edge.end
    dx = _step_sign(end.x - start.x)
    dy = _step_sign(end.y - start.y)
    dist = max(abs(end.x - start.x), abs(end.y - start.y))
    is_horiz = dx != 0
    for s in range(dist + 1):
        key_z = start.z if (s in (0, dist) or is_horiz) else -start.z
        node_key = (start.x + dx * s, start.y + dy * s, key_z)
        if grid.usage_count.get(node_key, 0) > grid.get_capacity(node_key):
            return True
    return False


def _is_net_over_capacity(res: RouteResult, grid: RoutingGrid) -> bool:
    """Return True if res failed or any node/edge on res exceeds grid capacity."""
    if not res.is_success:
        return True
    for node in res.nodes:
        node_key = (node.x, node.y, node.z)
        if grid.usage_count.get(node_key, 0) > grid.get_capacity(node_key):
            return True
    return any(
        edge.move_type == "leap" and _is_leap_edge_over_capacity(edge, grid) for edge in res.edges
    )


def _probe_subterranean_tile_blockers(
    nb: tuple[int, int],
    grid: RoutingGrid,
    congested_nets: set[str],
    failed_blockers: set[str],
    pin_blockers: set[str],
) -> None:
    """Add subterranean nets occupying nb on any underground layer to blocker sets."""
    for z_l, l_occ in grid.layer_occupancy.items():
        if z_l > LAYER_SURFACE and nb in l_occ:
            ug_net = l_occ[nb]
            congested_nets.add(ug_net)
            failed_blockers.add(ug_net)
            pin_blockers.add(ug_net)


def _probe_single_tile_blockers(
    nb: tuple[int, int],
    odx: int,
    ody: int,
    is_pin_pt: bool,
    grid: RoutingGrid,
    congested_nets: set[str],
    failed_blockers: set[str],
    pin_blockers: set[str],
) -> None:
    """Collect blocker nets and bump historical cost at tile nb near a failed pin/frontier."""
    max_off = max(abs(odx), abs(ody))
    near_2 = max_off <= PIN_BLOCKER_RADIUS
    if nb in grid.surface_occupancy:
        occ = grid.surface_occupancy[nb]
        congested_nets |= occ
        failed_blockers |= occ
        if (is_pin_pt and max_off <= 1) or (not is_pin_pt and near_2):
            pin_blockers |= occ
        if max_off <= SURFACE_HIST_RADIUS:
            h_key = (nb[0], nb[1], LAYER_SURFACE)
            grid.historical_cost[h_key] = (
                grid.historical_cost.get(h_key, 0.0) + SURFACE_FAIL_HIST_INCREMENT
            )
            grid.has_congestion = True
    if nb in grid.active_hoods:
        h_net = grid.active_hoods[nb]
        congested_nets.add(h_net)
        failed_blockers.add(h_net)
        if near_2:
            pin_blockers.add(h_net)
        h_key = (nb[0], nb[1], LAYER_SURFACE)
        grid.historical_cost[h_key] = (
            grid.historical_cost.get(h_key, 0.0) + HOOD_FAIL_HIST_INCREMENT
        )
        grid.has_congestion = True
    if near_2:
        _probe_subterranean_tile_blockers(nb, grid, congested_nets, failed_blockers, pin_blockers)


def _probe_failed_net_neighborhood(
    f_spec: RouterNetSpec,
    res: RouteResult,
    rx: int,
    grid: RoutingGrid,
    congested_nets: set[str],
    failed_blockers: set[str],
    pin_blockers: set[str],
) -> None:
    """Probe the neighborhood around a failed net's pins and frontier to identify blockers."""
    pin_pts = {f_spec.source, f_spec.sink}
    if f_spec.end_heading is not None:
        fdx, fdy = DIR_STEPS[f_spec.end_heading]
        pin_pts.add((f_spec.sink[0] + fdx, f_spec.sink[1] + fdy))
    if f_spec.start_heading is not None:
        sdx, sdy = DIR_STEPS[f_spec.start_heading]
        pin_pts.add((f_spec.source[0] + sdx, f_spec.source[1] + sdy))
    for term_pos in (*pin_pts, *res.failed_frontier):
        is_pin_pt = term_pos in pin_pts
        for odx in range(-rx, rx + 1):
            for ody in range(-PROBE_RADIUS_Y, PROBE_RADIUS_Y + 1):
                _probe_single_tile_blockers(
                    (term_pos[0] + odx, term_pos[1] + ody),
                    odx,
                    ody,
                    is_pin_pt,
                    grid,
                    congested_nets,
                    failed_blockers,
                    pin_blockers,
                )


def _add_tile_occupants_and_hood(
    nb: tuple[int, int],
    grid: RoutingGrid,
    congested_nets: set[str],
    neighbor_blockers: set[str],
) -> None:
    """Add surface occupants and active hood net at nb to congested and neighbor sets."""
    if nb in grid.surface_occupancy:
        congested_nets |= grid.surface_occupancy[nb]
        neighbor_blockers |= grid.surface_occupancy[nb]
    if nb in grid.active_hoods:
        congested_nets.add(grid.active_hoods[nb])
        neighbor_blockers.add(grid.active_hoods[nb])


def _probe_over_capacity_neighbors(
    res: RouteResult,
    grid: RoutingGrid,
    congested_nets: set[str],
    neighbor_blockers: set[str],
) -> None:
    """Add immediate 3x3 neighbors of the first over-capacity surface node on res."""
    for node in res.nodes:
        node_key = (node.x, node.y, node.z)
        if node.z == LAYER_SURFACE and grid.usage_count.get(node_key, 0) > grid.get_capacity(
            node_key
        ):
            for odx, ody in NEIGHBOR_2D_OFFSETS:
                _add_tile_occupants_and_hood(
                    (node.x + odx, node.y + ody), grid, congested_nets, neighbor_blockers
                )
            return


def _index_mst_branches_by_prefix(
    nets: Sequence[RouterNetSpec],
) -> dict[str, list[tuple[int, str]]]:
    """Group MST branch (branch_index, net_name) pairs by their MST prefix."""
    by_prefix: dict[str, list[tuple[int, str]]] = {}
    for net in nets:
        idx = mst_index(net.name)
        if idx is not None:
            prefix = net.name.rsplit("_mst_", 1)[0]
            by_prefix.setdefault(prefix, []).append((idx, net.name))
    return by_prefix


def _expand_congested_mst_branches(
    congested_nets: set[str],
    grid: RoutingGrid,
    mst_by_prefix: Mapping[str, Sequence[tuple[int, str]]],
) -> None:
    """Add descendant or sibling MST branches to congested_nets when an MST branch rips up."""
    extra_mst: set[str] = set()
    for cn in congested_nets:
        c_idx = mst_index(cn)
        if c_idx is None:
            continue
        c_prefix = cn.rsplit("_mst_", 1)[0]
        is_fail = not grid.net_routes[cn].is_success
        for o_idx, other_name in mst_by_prefix.get(c_prefix, ()):
            if other_name in grid.net_routes and (is_fail or o_idx >= c_idx):
                extra_mst.add(other_name)
    congested_nets |= extra_mst


def _congested_priority_bucket(
    net_name: str,
    prev_failed: set[str],
    pin_blockers: set[str],
    failed_blockers: set[str],
    over_cap_nets: set[str],
) -> int:
    """Return the integer rip-up priority bucket for net_name."""
    if net_name in prev_failed:
        return PRIORITY_PREV_FAILED
    if net_name in pin_blockers:
        return PRIORITY_PIN_BLOCKER
    if net_name in failed_blockers:
        return PRIORITY_FAILED_BLOCKER
    if net_name in over_cap_nets:
        return PRIORITY_OVER_CAPACITY
    return PRIORITY_OTHER_CONGESTED


def _order_congested_nets(
    congested_nets: set[str],
    prev_failed: set[str],
    pin_blockers: set[str],
    failed_blockers: set[str],
    over_cap_nets: set[str],
    neighbor_blockers: set[str],
    ever_failed: Mapping[str, int],
    net_map: Mapping[str, RouterNetSpec],
    iter_idx: int,
) -> list[str]:
    """Return congested_nets sorted in rip-up and reroute priority order."""
    odd_iter = iter_idx % 2 == 1
    sorted_congested = sorted(
        congested_nets,
        key=lambda n: (
            _congested_priority_bucket(
                n, prev_failed, pin_blockers, failed_blockers, over_cap_nets
            ),
            -ever_failed.get(n, 0),
            1 if (odd_iter and mst_index(n) is None) else 0,
            *net_sort_key(net_map[n]),
        ),
    )
    if not odd_iter:
        return sorted_congested

    eligible = {
        n
        for n in sorted_congested
        if n not in prev_failed
        and n not in pin_blockers
        and n not in neighbor_blockers
        and mst_index(n) is None
    }
    if len(eligible) <= 1:
        return sorted_congested
    rev_iter = iter(reversed([n for n in sorted_congested if n in eligible]))
    return [next(rev_iter) if n in eligible else n for n in sorted_congested]


def _has_over_capacity_node(res: RouteResult, grid: RoutingGrid) -> bool:
    """Return True if any node in res has usage_count >= capacity."""
    return any(
        grid.usage_count.get((nd.x, nd.y, nd.z), 0) >= grid.get_capacity((nd.x, nd.y, nd.z))
        for nd in res.nodes
    )


def _reroute_single_congested_net(
    net_name: str,
    net: RouterNetSpec,
    grid: RoutingGrid,
    iter_idx: int,
    prev_failed: set[str],
    ever_failed: Mapping[str, int],
    over_cap_nets: set[str],
) -> None:
    """Reroute net_name with widened margin and optional highway corridor expansion."""
    use_wide_margin = (
        net_name in prev_failed
        or net_name in ever_failed
        or (
            iter_idx >= CORRIDOR_EXPAND_ITER
            and HIGHWAY_SUFFIX not in net_name
            and (net.corridor_bbox is not None or iter_idx >= UNCONDITIONAL_EXPAND_ITER)
        )
    )
    margin = WIDE_RETRY_MARGIN if use_wide_margin else DEFAULT_SEARCH_MARGIN
    res = route_single_net(net, grid, margin=margin)
    if (
        iter_idx >= WIDE_HIGHWAY_ITER
        and len(over_cap_nets) <= WIDE_HIGHWAY_MAX_OVER_CAP
        and net_name in over_cap_nets
        and HIGHWAY_SUFFIX in net_name
        and net.corridor_bbox is not None
        and res.is_success
        and _has_over_capacity_node(res, grid)
    ):
        cbx0, cby0, cbx1, cby1 = net.corridor_bbox
        wide_net = dataclasses.replace(
            net,
            corridor_bbox=(
                max(grid.min_x, cbx0 - HIGHWAY_FALLBACK_PAD),
                max(grid.min_y, cby0 - HIGHWAY_FALLBACK_PAD),
                min(grid.max_x, cbx1 + HIGHWAY_FALLBACK_PAD),
                min(grid.max_y, cby1 + HIGHWAY_FALLBACK_PAD),
            ),
        )
        alt_res = route_single_net(wide_net, grid, margin=margin)
        if alt_res.is_success and not _has_over_capacity_node(alt_res, grid):
            res = alt_res
    grid.net_routes[net.name] = res
    if res.is_success:
        grid.place_net(net.name, res)


def _classify_route_congestion(
    net_name: str,
    res: RouteResult,
    net_map: Mapping[str, RouterNetSpec],
    iter_idx: int,
    rx: int,
    grid: RoutingGrid,
    ever_failed: dict[str, int],
    congested_nets: set[str],
    failed_bases: set[str],
    failed_blockers: set[str],
    pin_blockers: set[str],
    over_cap_nets: set[str],
    neighbor_blockers: set[str],
) -> None:
    """Classify one route result as failed or over-capacity and update blocker sets."""
    if not res.is_success:
        ever_failed[net_name] = ever_failed.get(net_name, 0) + 1
        if ever_failed[net_name] > MAX_LOCAL_FAIL_RETRIES and HIGHWAY_SUFFIX not in net_name:
            return
        congested_nets.add(net_name)
        failed_bases.add(base_net_name(net_name))
        _probe_failed_net_neighborhood(
            net_map[net_name], res, rx, grid, congested_nets, failed_blockers, pin_blockers
        )
    elif _is_net_over_capacity(res, grid):
        congested_nets.add(net_name)
        over_cap_nets.add(net_name)
        if iter_idx in NEIGHBOR_PROBE_ITERS:
            _probe_over_capacity_neighbors(res, grid, congested_nets, neighbor_blockers)


def _collect_iteration_congested_nets(
    grid: RoutingGrid,
    net_map: Mapping[str, RouterNetSpec],
    iter_idx: int,
    rx: int,
    ever_failed: dict[str, int],
    mst_by_prefix: Mapping[str, Sequence[tuple[int, str]]],
) -> tuple[set[str], set[str], set[str], set[str], set[str], set[str]]:
    """Collect congested, blocker, and over-capacity net sets for one rip-up iteration."""
    congested_nets: set[str] = set()
    failed_bases: set[str] = set()
    failed_blockers: set[str] = set()
    pin_blockers: set[str] = set()
    over_cap_nets: set[str] = set()
    neighbor_blockers: set[str] = set()

    for net_name, res in grid.net_routes.items():
        _classify_route_congestion(
            net_name,
            res,
            net_map,
            iter_idx,
            rx,
            grid,
            ever_failed,
            congested_nets,
            failed_bases,
            failed_blockers,
            pin_blockers,
            over_cap_nets,
            neighbor_blockers,
        )

    if len(over_cap_nets) > MAX_OVER_CAP_FOR_NEIGHBORS:
        neighbor_blockers.clear()
    else:
        neighbor_blockers -= over_cap_nets

    if congested_nets:
        _expand_congested_mst_branches(congested_nets, grid, mst_by_prefix)

    return (
        congested_nets,
        failed_bases,
        failed_blockers,
        pin_blockers,
        over_cap_nets,
        neighbor_blockers,
    )


def _execute_rip_up_and_reroute_pass(
    sorted_congested: Sequence[str],
    prev_failed: set[str],
    pin_blockers: set[str],
    failed_blockers: set[str],
    over_cap_nets: set[str],
    neighbor_blockers: set[str],
    is_flat: bool,
    iter_idx: int,
    ever_failed: Mapping[str, int],
    net_map: Mapping[str, RouterNetSpec],
    grid: RoutingGrid,
) -> None:
    """Rip up pre-selected congested nets and reroute all nets in sorted_congested."""
    pre_rip = (
        prev_failed
        | pin_blockers
        | {n for n in sorted_congested if mst_index(n) is not None}
        | ((over_cap_nets | neighbor_blockers) if (neighbor_blockers or is_flat) else set())
    )
    for net_name in sorted_congested:
        if net_name in pre_rip:
            grid.rip_up_net(net_name)

    for net_name in sorted_congested:
        if net_name not in pre_rip:
            cur_res = grid.net_routes.get(net_name)
            if (
                cur_res is not None
                and not _is_net_over_capacity(cur_res, grid)
                and net_name not in failed_blockers
            ):
                continue
            grid.rip_up_net(net_name)
        _reroute_single_congested_net(
            net_name, net_map[net_name], grid, iter_idx, prev_failed, ever_failed, over_cap_nets
        )


def _log_iteration_progress(
    num_nets: int,
    iter_idx: int,
    max_iterations: int,
    num_rerouted: int,
    num_over_cap: int,
    failed_cur: Sequence[str],
    conflicts: int,
) -> None:
    """Emit negotiated congestion progress to stderr on large netlists."""
    has_failed = bool(failed_cur)
    if num_nets < VERBOSE_NET_THRESHOLD:
        return
    if iter_idx != 0 and (iter_idx + 1) % 2 != 0 and not (conflicts == 0 and not has_failed):
        return
    fail_sample = f" sample={list(failed_cur[:FAIL_LOG_SAMPLE_LIMIT])}" if failed_cur else ""
    print(
        f"factorilog [maze_router]: iter {iter_idx + 1}/{max_iterations}: "
        f"rerouted={num_rerouted} over_cap={num_over_cap} "
        f"failed={len(failed_cur)} conflicts={conflicts}{fail_sample}",
        file=sys.stderr,
        flush=True,
    )


def _should_terminate_congestion_loop(
    conflicts: int,
    failed_cur: Sequence[str],
    ever_failed: Mapping[str, int],
    num_rerouted: int,
    iter_idx: int,
    stagnant_iters: int,
) -> bool:
    """Return True if negotiated congestion has converged or stagnated."""
    has_failed = bool(failed_cur)
    if conflicts == 0 and not has_failed:
        return True
    if (
        conflicts == 0
        and has_failed
        and all(ever_failed.get(n, 0) >= MAX_LOCAL_FAIL_RETRIES for n in failed_cur)
    ):
        return True
    return (
        (has_failed or num_rerouted > STAGNANT_CONGESTED_THRESHOLD)
        and iter_idx >= MIN_ITERS_BEFORE_STAGNATION
        and stagnant_iters >= MAX_STAGNANT_ITERS
    )


def _mark_unresolved_conflict_failures(grid: RoutingGrid, conflicts: int) -> None:
    """Mark remaining over-capacity routes as failed after exhausting iterations."""
    for net_name, cur_res in list(grid.net_routes.items()):
        if cur_res.is_success and _is_net_over_capacity(cur_res, grid):
            grid.net_routes[net_name] = RouteResult(
                net_name=net_name,
                nodes=cur_res.nodes,
                edges=cur_res.edges,
                cost=cur_res.cost,
                is_success=False,
                error_message=f"Unresolved congestion conflict ({conflicts} conflicted tiles)",
            )


def _place_initial_routes(
    nets: Sequence[RouterNetSpec],
    grid: RoutingGrid,
) -> tuple[int, list[str]]:
    """Route all nets in canonical order and return (conflicts, failed_net_names)."""
    for net in sorted(nets, key=net_sort_key):
        res = route_single_net(net, grid)
        grid.net_routes[net.name] = res
        if res.is_success:
            grid.place_net(net.name, res)
    conflicts = grid.update_congestion()
    failed_init = [n for n, r in grid.net_routes.items() if not r.is_success]
    return conflicts, failed_init


def _run_congestion_iteration(
    iter_idx: int,
    max_iterations: int,
    num_nets: int,
    rx: int,
    is_flat: bool,
    grid: RoutingGrid,
    net_map: Mapping[str, RouterNetSpec],
    ever_failed: dict[str, int],
    mst_by_prefix: Mapping[str, Sequence[tuple[int, str]]],
) -> tuple[int, list[str], int] | None:
    """Execute one negotiated-congestion pass; return (conflicts, failed_cur, num_rerouted)."""
    (
        congested_nets,
        failed_bases,
        failed_blockers,
        pin_blockers,
        over_cap_nets,
        neighbor_blockers,
    ) = _collect_iteration_congested_nets(grid, net_map, iter_idx, rx, ever_failed, mst_by_prefix)
    if not congested_nets:
        return None

    prev_failed = {
        n
        for n in congested_nets
        if not grid.net_routes[n].is_success or base_net_name(n) in failed_bases
    }
    sorted_congested = _order_congested_nets(
        congested_nets,
        prev_failed,
        pin_blockers,
        failed_blockers,
        over_cap_nets,
        neighbor_blockers,
        ever_failed,
        net_map,
        iter_idx,
    )
    _execute_rip_up_and_reroute_pass(
        sorted_congested,
        prev_failed,
        pin_blockers,
        failed_blockers,
        over_cap_nets,
        neighbor_blockers,
        is_flat,
        iter_idx,
        ever_failed,
        net_map,
        grid,
    )

    failed_cur = [n for n, r in grid.net_routes.items() if not r.is_success]
    conflicts = grid.update_congestion(
        beta=LATE_CONGESTION_BETA if iter_idx >= LATE_BETA_ITER else EARLY_CONGESTION_BETA,
        gamma=LATE_CONGESTION_GAMMA if iter_idx >= LATE_GAMMA_ITER else EARLY_CONGESTION_GAMMA,
    )
    _log_iteration_progress(
        num_nets,
        iter_idx,
        max_iterations,
        len(sorted_congested),
        len(over_cap_nets),
        failed_cur,
        conflicts,
    )
    return conflicts, failed_cur, len(sorted_congested)


def route_all_nets(
    nets: list[RouterNetSpec],
    grid: RoutingGrid,
    max_iterations: int = DEFAULT_ROUTE_ITERATIONS,
) -> dict[str, RouteResult]:
    """Route all nets with negotiated congestion rip-up and reroute."""
    conflicts, failed_init = _place_initial_routes(nets, grid)
    if conflicts == 0 and not failed_init:
        return grid.net_routes

    net_map = {n.name: n for n in nets}
    mst_by_prefix = _index_mst_branches_by_prefix(nets)
    is_flat = all(n.corridor_bbox is None for n in nets)
    rx = LARGE_PROBE_RADIUS_X if len(nets) > LARGE_NETLIST_THRESHOLD else SMALL_PROBE_RADIUS_X
    ever_failed: dict[str, int] = {}
    best_score = (len(failed_init) + conflicts, conflicts, len(failed_init))
    stagnant_iters = 0

    for iter_idx in range(max_iterations):
        step_out = _run_congestion_iteration(
            iter_idx,
            max_iterations,
            len(nets),
            rx,
            is_flat,
            grid,
            net_map,
            ever_failed,
            mst_by_prefix,
        )
        if step_out is None:
            break
        conflicts, failed_cur, num_rerouted = step_out
        cur_score = (len(failed_cur) + conflicts, conflicts, len(failed_cur))
        if cur_score < best_score:
            best_score = cur_score
            stagnant_iters = 0
        else:
            stagnant_iters += 1
        if _should_terminate_congestion_loop(
            conflicts, failed_cur, ever_failed, num_rerouted, iter_idx, stagnant_iters
        ):
            break

    if conflicts > 0 and max_iterations >= MIN_ITERS_FOR_CONFLICT_FAILURE:
        _mark_unresolved_conflict_failures(grid, conflicts)

    return grid.net_routes
