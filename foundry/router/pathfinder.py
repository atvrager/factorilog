"""Multi-layer A* maze router with negotiated congestion.

Implements iterative rip-up and reroute across the 3D graph.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import heapq

from foundry.router.grid import RoutingGrid, base_net_name
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
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
)

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


def pack_state(x: int, y: int, z: int, h_idx: int) -> int:
    """Pack 4D state coordinates into an integer."""
    return ((x + 32768) << 24) | ((y + 32768) << 8) | (z << 2) | h_idx


def unpack_state(s: int) -> tuple[int, int, int, int, int]:
    """Unpack state integer into (x, y, z, heading, h_idx)."""
    h_idx = s & 3
    h = h_idx << 2
    z = (s >> 2) & 63
    y = ((s >> 8) & 65535) - 32768
    x = (s >> 24) - 32768
    return x, y, z, h, h_idx


def heuristic(node: RouteNode, target: tuple[int, int]) -> float:
    """Calculate Manhattan distance heuristic to the target tile."""
    return float(abs(node.x - target[0]) + abs(node.y - target[1]))


def _a_star_search(
    net: RouterNetSpec,
    grid: RoutingGrid,
) -> RouteResult:
    """Execute directed 3D A* search on the routing grid."""
    if net.source == net.sink:
        heading = net.start_heading if net.start_heading is not None else 0
        node = RouteNode(net.source[0], net.source[1], LAYER_SURFACE, heading, net.lane)
        return RouteResult(net.name, [node], [], 0.0, is_success=True)

    sx, sy = net.source
    tx, ty = net.sink
    sink_pos = (tx, ty)
    net_name = net.name
    b_name = base_net_name(net_name)
    my_mst = mst_index(net_name)
    lane = net.lane
    is_fluid = net.is_fluid
    tier_name_surface = "pipe" if is_fluid else "transport-belt"
    tier_ug_keys = grid.pipe_tier_keys if is_fluid else grid.belt_tier_keys
    max_ug_span = grid.max_pipe_span if is_fluid else grid.max_belt_span

    bbox = grid.active_bbox
    if bbox is not None:
        bx0, by0, bx1, by1 = bbox
    else:
        bx0, by0, bx1, by1 = grid.min_x, grid.min_y, grid.max_x, grid.max_y

    start_headings = [net.start_heading] if net.start_heading is not None else list(ALL_DIRECTIONS)

    existing_fluid_goals: set[tuple[int, int]] = set()
    if is_fluid and my_mst is not None and my_mst > 0:
        mst_prefix = net_name.rsplit("_mst_", 1)[0]
        for i in range(my_mst):
            anc_name = f"{mst_prefix}_mst_{i}"
            anc_route = grid.net_routes.get(anc_name)
            if anc_route and anc_route.is_success:
                for node in anc_route.nodes:
                    if node.z == LAYER_SURFACE and node.pos != net.source:
                        if node.pos not in grid.active_hoods:
                            existing_fluid_goals.add(node.pos)
                        elif node.pos in grid.active_hood_types:
                            h_type, h_dir = grid.active_hood_types[node.pos]
                            m_dir = h_dir if h_type == "output" else OPPOSITE_DIR.get(h_dir, 0)
                            mdx, mdy = DIR_STEPS[m_dir]
                            m_pos = (node.pos[0] + mdx, node.pos[1] + mdy)
                            if (
                                m_pos != net.source
                                and m_pos not in grid.active_hoods
                                and m_pos in grid.surface_occupancy
                                and any(
                                    base_net_name(cn) == b_name
                                    for cn in grid.surface_occupancy[m_pos]
                                )
                            ):
                                existing_fluid_goals.add(m_pos)
    elif is_fluid and my_mst is None:
        my_comm = grid.get_net_commodity(net.name)
        for pos, occ in grid.surface_occupancy.items():
            if pos != net.source and pos not in grid.active_hoods:
                for cn in occ:
                    if grid.is_net_fluid(cn) and grid.get_net_commodity(cn) == my_comm:
                        existing_fluid_goals.add(pos)
                        break

    target_goals = [(tx, ty)]
    if is_fluid and existing_fluid_goals:
        sorted_goals = sorted(existing_fluid_goals, key=lambda g: abs(g[0] - sx) + abs(g[1] - sy))
        target_goals.extend(sorted_goals[:20])
    fluid_goals_rest = tuple(target_goals[1:]) if (is_fluid and existing_fluid_goals) else ()

    is_hwy = "_hwy" in net_name
    src_bay = (sx // 100, sy // 100) if (4 <= (sx % 100) <= 95 and 4 <= (sy % 100) <= 16) else None
    snk_bay = (tx // 100, ty // 100) if (4 <= (tx % 100) <= 95 and 4 <= (ty % 100) <= 16) else None
    snk_h_band = (ty + 4) // 100
    snk_v_band = (tx + 4) // 100
    bay_rows = {b[1] for b in (src_bay, snk_bay) if b is not None}

    def calc_h(x: int, y: int) -> float:
        if fluid_goals_rest:
            best_d = abs(x - tx) + abs(y - ty)
            for gx, gy in fluid_goals_rest:
                d = abs(x - gx) + abs(y - gy)
                if d < best_d:
                    best_d = d
            return 1.4 * float(best_d)
        dx_h = abs(x - tx)
        dy_h = abs(y - ty)
        if is_hwy:
            if 4 <= (x % 100) <= 95 and (y + 4) // 100 != snk_h_band:
                bx_h = x // 100
                vx_w = bx_h * 100 + 3
                vx_e = min(grid.max_x, bx_h * 100 + 96)
                dx_h = min(abs(x - vx_w) + abs(vx_w - tx), abs(x - vx_e) + abs(vx_e - tx))
            if 17 <= (y % 100) <= 95 and (x + 4) // 100 != snk_v_band:
                by_h = y // 100
                hy_n = by_h * 100 + (16 if by_h in bay_rows else 3)
                hy_s = min(grid.max_y, by_h * 100 + 96)
                dy_h = max(
                    dy_h,
                    min(abs(y - hy_n) + abs(hy_n - ty), abs(y - hy_s) + abs(hy_s - ty)),
                )
        return 1.4 * float(dx_h + dy_h)

    # open_set stores (f_score, h_val, state_id)
    open_set: list[tuple[float, float, int]] = []
    g_score: dict[int, float] = {}
    came_from: dict[int, tuple[int, str, str, float]] = {}
    parent_state: dict[int, int] = {}
    path_hoods: dict[int, frozenset[tuple[int, int]]] = {}
    closed_set: set[int] = set()
    closed_xy: set[int] = set()

    def _in_ancestor_xy(state_id: int, xy_key: int) -> bool:
        if xy_key not in closed_xy:
            return False
        cur: int | None = state_id
        while cur is not None:
            if (cur >> 8) == xy_key:
                return True
            cur = parent_state.get(cur)
        return False

    start_at_mouth = False
    source_hood_mouth: tuple[int, int] | None = None
    if (
        is_fluid
        and net.source in grid.active_hoods
        and base_net_name(grid.active_hoods[net.source]) == b_name
    ):
        h_type, h_dir = grid.active_hood_types.get(net.source, ("output", 0))
        m_dir = h_dir if h_type == "output" else OPPOSITE_DIR.get(h_dir, 0)
        mdx, mdy = DIR_STEPS[m_dir]
        m_pos = (net.source[0] + mdx, net.source[1] + mdy)
        if grid.is_in_bounds(m_pos[0], m_pos[1]):
            start_at_mouth = True
            source_hood_mouth = m_pos
            for h in ALL_DIRECTIONS:
                if grid.can_occupy_surface(m_pos, net_name, lane, heading=h):
                    h_idx = h >> 2
                    s = pack_state(m_pos[0], m_pos[1], LAYER_SURFACE, h_idx)
                    path_hoods[s] = frozenset()
                    base_start_cost = grid.get_fast_node_cost(
                        m_pos[0], m_pos[1], LAYER_SURFACE, 0.0, net_name, lane
                    )
                    g_score[s] = base_start_cost
                    h_val = calc_h(m_pos[0], m_pos[1])
                    heapq.heappush(open_set, (base_start_cost + h_val, h_val, s))
    else:
        for h in start_headings:
            can_surf = grid.can_occupy_surface(net.source, net_name, lane, heading=h, is_hood=False)
            can_hood = (
                is_fluid
                and grid.can_occupy_surface(net.source, net_name, lane, heading=h, is_hood=True)
                and grid.can_place_hood(net.source, net_name, heading=h, is_output=False)
            )
            if not can_surf and not can_hood:
                continue
            h_idx = h >> 2
            s = pack_state(sx, sy, LAYER_SURFACE, h_idx)
            path_hoods[s] = frozenset()
            base_start_cost = grid.get_fast_node_cost(sx, sy, LAYER_SURFACE, 0.0, net_name, lane)
            g_score[s] = base_start_cost
            h_val = calc_h(sx, sy)
            heapq.heappush(open_set, (base_start_cost + h_val, h_val, s))

    goal_state: int | None = None
    target_end_h = net.end_heading

    if not open_set:
        return RouteResult(
            net_name=net.name,
            nodes=[],
            edges=[],
            cost=float("inf"),
            is_success=False,
            error_message=f"Source {net.source} is blocked.",
            failed_frontier=(net.source,),
        )

    valid_end_headings: set[int] = set()
    if not is_fluid:
        end_headings = [target_end_h] if target_end_h is not None else list(ALL_DIRECTIONS)
        for eh in end_headings:
            can_sink = grid.can_occupy_surface(
                sink_pos, net_name, lane, heading=eh, is_hood=False
            ) or (
                bool(tier_ug_keys)
                and grid.can_occupy_surface(sink_pos, net_name, lane, heading=eh, is_hood=True)
                and grid.can_place_hood(sink_pos, net_name, heading=eh, is_output=True)
            )
            if not can_sink:
                continue
            gdx, gdy = DIR_STEPS[eh]
            front_pos = (tx + gdx, ty + gdy)
            front_occ = grid.surface_occupancy.get(front_pos)
            front_res = grid.reserved_terminals.get(front_pos)
            bad_front = False
            if front_occ and any(
                base_net_name(n) != b_name and not grid.is_net_fluid(n) for n in front_occ
            ):
                bad_front = True
            if front_res and any(
                base_net_name(n) != b_name and not grid.is_net_fluid(n) for n in front_res
            ):
                bad_front = True
            if (
                front_pos in grid.active_hoods
                and (grid.min_x > 0 or base_net_name(grid.active_hoods[front_pos]) != b_name)
                and not grid.is_net_fluid(grid.active_hoods[front_pos])
            ):
                bad_front = True
            if front_pos in grid.component_headings and net.terminal_heading is None:
                bad_front = True
            if not bad_front:
                valid_end_headings.add(eh)
        if not valid_end_headings:
            return RouteResult(
                net_name=net.name,
                nodes=[],
                edges=[],
                cost=float("inf"),
                is_success=False,
                error_message=f"Sink {sink_pos} or its front tile is blocked.",
                failed_frontier=(sink_pos,),
            )

    span = abs(sx - tx) + abs(sy - ty)
    box_area = max(100, (bx1 - bx0 + 1) * (by1 - by0 + 1))
    cap_states = 48 * 1024 if (is_hwy or span > 120) else 24 * 1024
    max_states = min(cap_states, max(4000, box_area * 2))
    while open_set:
        if len(closed_set) >= max_states:
            break
        _, _, current = heapq.heappop(open_set)

        if current in closed_set:
            continue
        closed_set.add(current)
        closed_xy.add(current >> 8)

        cx, cy, cz, ch, ch_idx = unpack_state(current)
        curr_hoods = path_hoods.get(current, frozenset())

        # Check goal condition
        prev_move = came_from[current][1] if current in came_from else None
        can_surf_here = True
        if cz == LAYER_SURFACE:
            cpos = (cx, cy)
            if is_fluid and prev_move != "leap":
                can_surf_here = grid.can_occupy_surface(
                    cpos, net_name, lane, heading=ch, is_hood=False
                )
            if cx == tx and cy == ty and (target_end_h is None or ch == target_end_h):
                if is_fluid and prev_move == "leap":
                    inward = grid.fluid_terminal_inward_dir.get((tx, ty))
                    if (inward is None or ch == inward) and (
                        my_mst is None or (sx, sy) not in curr_hoods
                    ):
                        goal_state = current
                        break
                elif not is_fluid:
                    gdx, gdy = DIR_STEPS[ch]
                    f_pos = (tx + gdx, ty + gdy)
                    f_xy = ((f_pos[0] + 32768) << 16) | (f_pos[1] + 32768)
                    if ch in valid_end_headings and (
                        grid.min_x <= 0 or not _in_ancestor_xy(current, f_xy)
                    ):
                        goal_state = current
                        break
                elif can_surf_here:
                    goal_state = current
                    break
            if (
                is_fluid
                and (cx, cy) in existing_fluid_goals
                and prev_move != "leap"
                and can_surf_here
            ):
                goal_state = current
                break
            if is_fluid and prev_move == "leap":
                mdx, mdy = DIR_STEPS[ch]
                if (cx + mdx, cy + mdy) in existing_fluid_goals:
                    goal_state = current
                    break

        curr_g = g_score[current]
        dx, dy = DIR_STEPS[ch]

        if cz == LAYER_SURFACE:
            cpos = (cx, cy)
            if can_surf_here:
                next_visited = (curr_hoods | {cpos}) if is_fluid else curr_hoods
                # 1. Straight
                nx = cx + dx
                ny = cy + dy
                if (
                    bx0 <= nx <= bx1
                    and by0 <= ny <= by1
                    and not (
                        is_hwy
                        and 4 <= (nx % 100) <= 95
                        and 4 <= (ny % 100) <= 16
                        and (nx // 100, ny // 100) != src_bay
                        and (nx // 100, ny // 100) != snk_bay
                    )
                ):
                    npos = (nx, ny)
                    n_xy = ((nx + 32768) << 16) | (ny + 32768)
                    if not _in_ancestor_xy(current, n_xy):
                        nxt_s = pack_state(nx, ny, LAYER_SURFACE, ch_idx)
                        if nxt_s not in closed_set and (
                            is_fluid or nx != tx or ny != ty or ch in valid_end_headings
                        ):
                            edge_cost = grid.get_fast_node_cost(
                                nx, ny, LAYER_SURFACE, STEP_COST, net_name, lane, heading=ch
                            )
                            tentative_g = curr_g + edge_cost
                            if tentative_g < g_score.get(nxt_s, float("inf")) and (
                                grid.can_occupy_surface(npos, net_name, lane, heading=ch)
                                or (
                                    is_fluid
                                    and prev_move != "leap"
                                    and grid.can_occupy_surface(
                                        npos, net_name, lane, heading=ch, is_hood=True
                                    )
                                    and grid.can_place_hood(
                                        npos, net_name, heading=ch, is_output=False
                                    )
                                )
                            ):
                                g_score[nxt_s] = tentative_g
                                came_from[nxt_s] = (
                                    current,
                                    "straight",
                                    tier_name_surface,
                                    edge_cost,
                                )
                                parent_state[nxt_s] = current
                                path_hoods[nxt_s] = next_visited
                                h_val = calc_h(nx, ny)
                                heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))

                        # 2. Turns
                        for th in ((ch + 4) & 15, (ch + 12) & 15):
                            if (
                                not is_fluid
                                and nx == tx
                                and ny == ty
                                and th not in valid_end_headings
                            ):
                                continue
                            th_idx = th >> 2
                            nxt_s = pack_state(nx, ny, LAYER_SURFACE, th_idx)
                            if nxt_s not in closed_set:
                                edge_cost = grid.get_fast_node_cost(
                                    nx,
                                    ny,
                                    LAYER_SURFACE,
                                    STEP_COST + TURN_PENALTY,
                                    net_name,
                                    lane,
                                    heading=th,
                                )
                                tentative_g = curr_g + edge_cost
                                if tentative_g < g_score.get(
                                    nxt_s, float("inf")
                                ) and grid.can_occupy_surface(npos, net_name, lane, heading=th):
                                    g_score[nxt_s] = tentative_g
                                    came_from[nxt_s] = (
                                        current,
                                        "turn",
                                        tier_name_surface,
                                        edge_cost,
                                    )
                                    parent_state[nxt_s] = current
                                    path_hoods[nxt_s] = next_visited
                                    h_val = calc_h(nx, ny)
                                    heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))

            # 3. Underground Dives
            if (
                prev_move not in ("turn", "leap")
                and tier_ug_keys
                and cpos not in curr_hoods
                and (
                    not is_fluid
                    or cpos not in grid.fluid_terminal_inward_dir
                    or OPPOSITE_DIR.get(ch, 0) == grid.fluid_terminal_inward_dir[cpos]
                )
                and (
                    (is_fluid and (not can_surf_here or abs(cx - tx) + abs(cy - ty) <= max_ug_span))
                    or grid.should_consider_underground(
                        cx, cy, dx, dy, max_ug_span, net_name, lane, target_pos=sink_pos
                    )
                )
                and grid.can_place_hood(cpos, net_name, heading=ch, is_output=False)
                and (
                    not is_fluid
                    or grid.can_occupy_surface(cpos, net_name, lane, heading=ch, is_hood=True)
                )
            ):
                new_dive_hoods = curr_hoods | {cpos}
                is_horiz_dive = dx != 0
                dive_axis_spans = (
                    (grid.active_pipe_horiz_spans if is_horiz_dive else grid.active_pipe_vert_spans)
                    if is_fluid
                    else (
                        grid.active_belt_horiz_spans
                        if is_horiz_dive
                        else grid.active_belt_vert_spans
                    )
                )
                prev_reach = 0
                for target_z in tier_ug_keys:
                    t_reach = grid.get_tier_span(target_z)
                    if not is_fluid and prev_reach > 0:
                        blocked_early = False
                        for s_chk in range(1, prev_reach + 1):
                            ex_c = cx + dx * s_chk
                            ey_c = cy + dy * s_chk
                            if not (bx0 <= ex_c <= bx1 and by0 <= ey_c <= by1):
                                blocked_early = True
                                break
                            pt_c = (ex_c, ey_c)
                            if pt_c in dive_axis_spans or pt_c in curr_hoods:
                                blocked_early = True
                                break
                            if pt_c in grid.active_hoods and not grid.is_net_fluid(
                                grid.active_hoods[pt_c]
                            ):
                                blocked_early = True
                                break
                        if blocked_early:
                            break
                    prev_reach = t_reach
                    nxt_s = pack_state(cx, cy, target_z, ch_idx)
                    if nxt_s not in closed_set:
                        tier_cost = grid.get_tier_cost(target_z)
                        edge_cost = grid.get_fast_node_cost(
                            cx, cy, target_z, HOOD_PENALTY + tier_cost, net_name, lane
                        )
                        tentative_g = curr_g + edge_cost
                        if tentative_g < g_score.get(nxt_s, float("inf")):
                            g_score[nxt_s] = tentative_g
                            tier_name = (
                                grid.pipe_tiers[target_z]["name"]
                                if is_fluid
                                else grid.belt_tiers[target_z]["name"]
                            )
                            came_from[nxt_s] = (current, "dive", tier_name, edge_cost)
                            parent_state[nxt_s] = current
                            path_hoods[nxt_s] = new_dive_hoods
                            h_val = calc_h(cx, cy)
                            heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))

            if prev_move == "leap" and is_fluid and tier_ug_keys:
                nx = cx + dx
                ny = cy + dy
                npos = (nx, ny)
                n_xy = ((nx + 32768) << 16) | (ny + 32768)
                if (
                    grid.is_in_bounds(nx, ny)
                    and npos not in curr_hoods
                    and not _in_ancestor_xy(current, n_xy)
                    and grid.can_place_hood(npos, net_name, heading=ch, is_output=False)
                    and grid.can_occupy_surface(npos, net_name, lane, heading=ch, is_hood=True)
                ):
                    new_dive_hoods = curr_hoods | {npos}
                    for target_z in tier_ug_keys:
                        nxt_s = pack_state(nx, ny, target_z, ch_idx)
                        if nxt_s not in closed_set:
                            tier_cost = grid.get_tier_cost(target_z)
                            edge_cost = grid.get_fast_node_cost(
                                nx, ny, target_z, HOOD_PENALTY + tier_cost, net_name, lane
                            )
                            tentative_g = curr_g + edge_cost
                            if tentative_g < g_score.get(nxt_s, float("inf")):
                                g_score[nxt_s] = tentative_g
                                tier_name = grid.pipe_tiers[target_z]["name"]
                                came_from[nxt_s] = (current, "dive", tier_name, edge_cost)
                                parent_state[nxt_s] = current
                                path_hoods[nxt_s] = new_dive_hoods
                                h_val = calc_h(nx, ny)
                                heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))
        else:
            # Subterranean leaps
            max_span = grid.get_tier_span(cz)
            is_horiz = dx != 0
            if is_fluid:
                axis_spans = (
                    grid.active_pipe_horiz_spans if is_horiz else grid.active_pipe_vert_spans
                )
                min_step = 1
            else:
                axis_spans = (
                    grid.active_belt_horiz_spans if is_horiz else grid.active_belt_vert_spans
                )
                min_step = 2
                for k_idx, kz in enumerate(tier_ug_keys):
                    if kz == cz and k_idx > 0:
                        min_step = max(2, grid.get_tier_span(tier_ug_keys[k_idx - 1]) + 1)
                        break
            tier_name = (
                grid.pipe_tiers.get(cz, {}).get("name", "pipe-to-ground")
                if is_fluid
                else grid.belt_tiers.get(cz, {}).get("name", "underground-belt")
            )

            blocked_tunnel = False
            for s_pre in range(1, min_step):
                ex_p = cx + dx * s_pre
                ey_p = cy + dy * s_pre
                if not (bx0 <= ex_p <= bx1 and by0 <= ey_p <= by1):
                    blocked_tunnel = True
                    break
                pt_p = (ex_p, ey_p)
                if pt_p in axis_spans or pt_p in curr_hoods:
                    blocked_tunnel = True
                    break
                if pt_p in grid.active_hoods and (
                    grid.is_net_fluid(grid.active_hoods[pt_p]) == is_fluid
                ):
                    blocked_tunnel = True
                    break
            if blocked_tunnel:
                continue

            for step in range(min_step, max_span + 1):
                ex = cx + dx * step
                ey = cy + dy * step
                if not (bx0 <= ex <= bx1 and by0 <= ey <= by1):
                    break

                pt = (ex, ey)
                if pt in grid.active_hoods:
                    if grid.is_net_fluid(grid.active_hoods[pt]) == is_fluid:
                        break
                    continue
                if pt in axis_spans:
                    break

                if pt in curr_hoods:
                    break
                if (
                    is_hwy
                    and 4 <= (ex % 100) <= 95
                    and 4 <= (ey % 100) <= 16
                    and (ex // 100, ey // 100) != src_bay
                    and (ex // 100, ey // 100) != snk_bay
                ):
                    continue
                if not is_fluid and ex == tx and ey == ty and ch not in valid_end_headings:
                    continue
                e_xy = ((ex + 32768) << 16) | (ey + 32768)
                if _in_ancestor_xy(current, e_xy):
                    continue

                nxt_s = pack_state(ex, ey, LAYER_SURFACE, ch_idx)
                if nxt_s in closed_set:
                    continue

                edge_cost = grid.get_fast_node_cost(
                    ex,
                    ey,
                    LAYER_SURFACE,
                    step * UNDERGROUND_STEP_COST + HOOD_PENALTY,
                    net_name,
                    lane,
                    heading=ch,
                )
                tentative_g = curr_g + edge_cost
                if tentative_g >= g_score.get(nxt_s, float("inf")):
                    continue

                if not is_fluid and grid.min_x > 0 and step > 1:
                    r_xy = ((ex - dx + 32768) << 16) | (ey - dy + 32768)
                    if _in_ancestor_xy(current, r_xy):
                        continue

                if not grid.can_place_hood(pt, net_name, heading=ch, is_output=True):
                    continue

                if grid.can_occupy_surface(pt, net_name, lane, heading=ch, is_hood=True):
                    g_score[nxt_s] = tentative_g
                    came_from[nxt_s] = (current, "leap", tier_name, edge_cost)
                    parent_state[nxt_s] = current
                    path_hoods[nxt_s] = curr_hoods | {
                        (cx + dx * s, cy + dy * s) for s in range(1, step + 1)
                    }
                    h_val = calc_h(ex, ey)
                    heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))

    if goal_state is None:
        coords = sorted(
            {(unpack_state(s)[0], unpack_state(s)[1], unpack_state(s)[2]) for s in closed_set}
        )
        surf_pts = sorted(
            {(cx_pt, cy_pt) for cx_pt, cy_pt, cz_pt in coords if cz_pt == LAYER_SURFACE},
            key=lambda pt: abs(pt[0] - tx) + abs(pt[1] - ty),
        )
        frontier = tuple(surf_pts[:4])
        return RouteResult(
            net_name=net.name,
            nodes=[],
            edges=[],
            cost=float("inf"),
            is_success=False,
            error_message=(
                f"No route found from {net.source} to {net.sink}. "
                f"Explored {len(closed_set)} states. Frontier: {frontier}. Coords: {coords[:20]}."
            ),
            failed_frontier=frontier,
        )

    # Reconstruct path
    curr_s = goal_state
    cx, cy, cz, ch, _ = unpack_state(curr_s)
    nodes: list[RouteNode] = [RouteNode(cx, cy, cz, ch, lane)]
    edges: list[RouteEdge] = []

    while curr_s in came_from:
        prev_s, move_type, tier_name, edge_cost = came_from[curr_s]
        px, py, pz, ph, _ = unpack_state(prev_s)
        prev_node = RouteNode(px, py, pz, ph, lane)
        edge = RouteEdge(prev_node, nodes[-1], move_type, tier_name, edge_cost)
        edges.append(edge)
        nodes.append(prev_node)
        curr_s = prev_s

    nodes.reverse()
    edges.reverse()

    if start_at_mouth and source_hood_mouth is not None and nodes:
        h_type, h_dir = grid.active_hood_types.get(net.source, ("output", 0))
        m_dir = h_dir if h_type == "output" else OPPOSITE_DIR.get(h_dir, 0)
        first_node = RouteNode(net.source[0], net.source[1], LAYER_SURFACE, m_dir, lane)
        edge_cost = grid.get_fast_node_cost(
            net.source[0], net.source[1], LAYER_SURFACE, STEP_COST, net_name, lane
        )
        connecting_edge = RouteEdge(first_node, nodes[0], "straight", tier_name_surface, edge_cost)
        nodes.insert(0, first_node)
        edges.insert(0, connecting_edge)

    return RouteResult(
        net_name=net.name,
        nodes=nodes,
        edges=edges,
        cost=g_score[goal_state],
        is_success=True,
    )


def route_single_net(
    net: RouterNetSpec,
    grid: RoutingGrid,
    margin: int = 10,
) -> RouteResult:
    """Find a minimum-cost directed path for one net with spatial pruning."""
    min_x = min(net.source[0], net.sink[0])
    max_x = max(net.source[0], net.sink[0])
    min_y = min(net.source[1], net.sink[1])
    max_y = max(net.source[1], net.sink[1])

    span = (max_x - min_x) + (max_y - min_y)
    if span > 120 and "_hwy" not in net.name:
        margin = max(margin, min(60, span // 8))

    my_mst = mst_index(net.name)
    if net.is_fluid:
        margin = max(margin, 25)
    if net.is_fluid and my_mst is not None and my_mst > 0:
        mst_prefix = net.name.rsplit("_mst_", 1)[0]
        closest_goal = None
        best_d = float("inf")
        for i in range(my_mst):
            anc_route = grid.net_routes.get(f"{mst_prefix}_mst_{i}")
            if anc_route and anc_route.is_success:
                for node in anc_route.nodes:
                    if (
                        node.z == LAYER_SURFACE
                        and node.pos != net.source
                        and node.pos not in grid.active_hoods
                    ):
                        d = abs(node.x - net.source[0]) + abs(node.y - net.source[1])
                        if d < best_d:
                            best_d = d
                            closest_goal = node.pos
        if closest_goal:
            min_x = min(min_x, closest_goal[0])
            max_x = max(max_x, closest_goal[0])
            min_y = min(min_y, closest_goal[1])
            max_y = max(max_y, closest_goal[1])

    bx_min = max(grid.min_x, min_x - margin)
    by_min = max(grid.min_y, min_y - margin)
    bx_max = min(grid.max_x, max_x + margin)
    by_max = min(grid.max_y, max_y + margin)
    if net.corridor_bbox is not None:
        cbx0, cby0, cbx1, cby1 = net.corridor_bbox
        if "_hwy" in net.name:
            bx_min = max(grid.min_x, cbx0)
            by_min = max(grid.min_y, cby0)
            bx_max = min(grid.max_x, cbx1)
            by_max = min(grid.max_y, cby1)
        else:
            bx_min = max(cbx0, bx_min)
            by_min = max(cby0, by_min)
            bx_max = min(cbx1, bx_max)
            by_max = min(cby1, by_max)

    first_bbox = (bx_min, by_min, bx_max, by_max)
    grid.active_bbox = first_bbox
    res = _a_star_search(net, grid)
    if not res.is_success and "is blocked." not in (res.error_message or ""):
        first_frontier = res.failed_frontier
        if net.corridor_bbox is not None and "_hwy" not in net.name:
            cbx0, cby0, cbx1, cby1 = net.corridor_bbox
            fallback_bbox = (
                max(grid.min_x, cbx0),
                max(grid.min_y, cby0),
                min(grid.max_x, cbx1),
                min(grid.max_y, cby1),
            )
        elif "_hwy" in net.name and net.corridor_bbox is not None:
            cbx0, cby0, cbx1, cby1 = net.corridor_bbox
            fb_pad = 105
            fallback_bbox = (
                max(grid.min_x, cbx0 - fb_pad),
                max(grid.min_y, cby0 - fb_pad),
                min(grid.max_x, cbx1 + fb_pad),
                min(grid.max_y, cby1 + fb_pad),
            )
        elif "_hwy" in net.name:
            fallback_bbox = (grid.min_x, grid.min_y, grid.max_x, grid.max_y)
        else:
            wide = max(margin * 3, 45)
            fallback_bbox = (
                max(grid.min_x, min_x - wide),
                max(grid.min_y, min_y - wide),
                min(grid.max_x, max_x + wide),
                min(grid.max_y, max_y + wide),
            )
        if fallback_bbox != first_bbox:
            grid.active_bbox = fallback_bbox
            res = _a_star_search(net, grid)
            if not res.is_success and first_frontier:
                merged_f: list[tuple[int, int]] = list(first_frontier)
                for pt in res.failed_frontier:
                    if pt not in merged_f:
                        merged_f.append(pt)
                res.failed_frontier = tuple(merged_f[:6])
    grid.active_bbox = None
    return res


def route_all_nets(
    nets: list[RouterNetSpec],
    grid: RoutingGrid,
    max_iterations: int = 40,
) -> dict[str, RouteResult]:
    """Route all nets with negotiated congestion rip-up and reroute."""

    def net_sort_key(n: RouterNetSpec) -> tuple[int, int, int, str]:
        m_idx = mst_index(n.name)
        span = abs(n.source[0] - n.sink[0]) + abs(n.source[1] - n.sink[1])
        return (
            1 if n.is_fluid else 0,
            span if not n.is_fluid else 0,
            m_idx if m_idx is not None else -1,
            n.name,
        )

    ordered_nets = sorted(nets, key=net_sort_key)

    # Initial sequential route
    for net in ordered_nets:
        res = route_single_net(net, grid)
        grid.net_routes[net.name] = res
        if res.is_success:
            grid.place_net(net.name, res)

    conflicts = grid.update_congestion()
    failed_init = [n for n, r in grid.net_routes.items() if not r.is_success]
    has_failed = bool(failed_init)
    if conflicts == 0 and not has_failed:
        return grid.net_routes

    net_map = {n.name: n for n in nets}
    is_flat = all(n.corridor_bbox is None for n in nets)
    ever_failed: dict[str, int] = {}
    best_score = (len(failed_init) + conflicts, conflicts, len(failed_init))
    stagnant_iters = 0

    def _is_net_over_capacity(res: RouteResult) -> bool:
        if not res.is_success:
            return True
        for node in res.nodes:
            node_key = (node.x, node.y, node.z)
            if grid.usage_count.get(node_key, 0) > grid.get_capacity(node_key):
                return True
        for edge in res.edges:
            if edge.move_type == "leap":
                start = edge.start
                end = edge.end
                dx = 1 if end.x > start.x else (-1 if end.x < start.x else 0)
                dy = 1 if end.y > start.y else (-1 if end.y < start.y else 0)
                dist = max(abs(end.x - start.x), abs(end.y - start.y))
                is_horiz = dx != 0
                for s in range(dist + 1):
                    key_z = start.z if (s == 0 or s == dist or is_horiz) else -start.z
                    node_key = (start.x + dx * s, start.y + dy * s, key_z)
                    if grid.usage_count.get(node_key, 0) > grid.get_capacity(node_key):
                        return True
        return False

    # Negotiated congestion iterations
    for iter_idx in range(max_iterations):
        congested_nets: set[str] = set()
        failed_bases: set[str] = set()
        failed_blockers: set[str] = set()
        pin_blockers: set[str] = set()
        over_cap_nets: set[str] = set()
        neighbor_blockers: set[str] = set()
        for net_name, res in grid.net_routes.items():
            if not res.is_success:
                ever_failed[net_name] = ever_failed.get(net_name, 0) + 1
                if ever_failed[net_name] > 12 and "_hwy" not in net_name:
                    continue
                congested_nets.add(net_name)
                failed_bases.add(base_net_name(net_name))
                f_spec = net_map[net_name]
                rx = 12 if len(nets) > 100 else 6
                pin_pts = {f_spec.source, f_spec.sink}
                if f_spec.end_heading is not None:
                    fdx, fdy = DIR_STEPS[f_spec.end_heading]
                    pin_pts.add((f_spec.sink[0] + fdx, f_spec.sink[1] + fdy))
                if f_spec.start_heading is not None:
                    sdx, sdy = DIR_STEPS[f_spec.start_heading]
                    pin_pts.add((f_spec.source[0] + sdx, f_spec.source[1] + sdy))
                probe_pts = [*pin_pts, *res.failed_frontier]
                for term_pos in probe_pts:
                    is_pin_pt = term_pos in pin_pts
                    for odx in range(-rx, rx + 1):
                        for ody in range(-6, 7):
                            nb = (term_pos[0] + odx, term_pos[1] + ody)
                            if nb in grid.surface_occupancy:
                                congested_nets |= grid.surface_occupancy[nb]
                                failed_blockers |= grid.surface_occupancy[nb]
                                if is_pin_pt and abs(odx) <= 1 and abs(ody) <= 1:
                                    pin_blockers |= grid.surface_occupancy[nb]
                                if abs(odx) <= 3 and abs(ody) <= 3:
                                    h_key = (nb[0], nb[1], 0)
                                    grid.historical_cost[h_key] = (
                                        grid.historical_cost.get(h_key, 0.0) + 1.5
                                    )
                                    grid.has_congestion = True
                            if nb in grid.active_hoods:
                                h_net = grid.active_hoods[nb]
                                congested_nets.add(h_net)
                                failed_blockers.add(h_net)
                                if abs(odx) <= 2 and abs(ody) <= 2:
                                    pin_blockers.add(h_net)
                                h_key = (nb[0], nb[1], 0)
                                grid.historical_cost[h_key] = (
                                    grid.historical_cost.get(h_key, 0.0) + 2.5
                                )
                                grid.has_congestion = True
            elif _is_net_over_capacity(res):
                congested_nets.add(net_name)
                over_cap_nets.add(net_name)
                if iter_idx in (12, 24, 36, 48):
                    for node in res.nodes:
                        node_key = (node.x, node.y, node.z)
                        if node.z == LAYER_SURFACE and grid.usage_count.get(
                            node_key, 0
                        ) > grid.get_capacity(node_key):
                            for odx in (-1, 0, 1):
                                for ody in (-1, 0, 1):
                                    nb = (node.x + odx, node.y + ody)
                                    if nb in grid.surface_occupancy:
                                        congested_nets |= grid.surface_occupancy[nb]
                                        neighbor_blockers |= grid.surface_occupancy[nb]
                                    if nb in grid.active_hoods:
                                        congested_nets.add(grid.active_hoods[nb])
                                        neighbor_blockers.add(grid.active_hoods[nb])
                            break

        if not congested_nets:
            break

        neighbor_blockers -= over_cap_nets

        # When an MST branch rips up, also rip up its descendant MST branches
        # And if an MST branch failed, rip up all branches of its base net so the tree routes before blockers
        extra_mst: set[str] = set()
        for cn in congested_nets:
            c_idx = mst_index(cn)
            if c_idx is not None:
                c_prefix = cn.rsplit("_mst_", 1)[0]
                is_fail = not grid.net_routes[cn].is_success
                for other_name in grid.net_routes:
                    o_idx = mst_index(other_name)
                    if (
                        o_idx is not None
                        and other_name.rsplit("_mst_", 1)[0] == c_prefix
                        and (is_fail or o_idx >= c_idx)
                    ):
                        extra_mst.add(other_name)
        congested_nets |= extra_mst

        prev_failed = {
            n
            for n in congested_nets
            if not grid.net_routes[n].is_success or base_net_name(n) in failed_bases
        }

        sorted_congested = sorted(
            congested_nets,
            key=lambda n: (
                0
                if n in prev_failed
                else (
                    1
                    if n in pin_blockers
                    else (2 if n in failed_blockers else (3 if n in over_cap_nets else 4))
                ),
                -ever_failed.get(n, 0),
                1 if (iter_idx % 2 == 1 and mst_index(n) is None) else 0,
                *net_sort_key(net_map[n]),
            ),
        )
        if iter_idx % 2 == 1:
            non_fail = [
                n
                for n in sorted_congested
                if n not in prev_failed
                and n not in pin_blockers
                and n not in neighbor_blockers
                and mst_index(n) is None
            ]
            if len(non_fail) > 1:
                non_fail_rev = list(reversed(non_fail))
                nf_iter = iter(non_fail_rev)
                sorted_congested = [
                    next(nf_iter)
                    if (
                        n not in prev_failed
                        and n not in pin_blockers
                        and n not in neighbor_blockers
                        and mst_index(n) is None
                    )
                    else n
                    for n in sorted_congested
                ]

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
                    and not _is_net_over_capacity(cur_res)
                    and net_name not in failed_blockers
                ):
                    continue
                grid.rip_up_net(net_name)
            net = net_map[net_name]
            margin = (
                35
                if (
                    net_name in prev_failed
                    or net_name in ever_failed
                    or (
                        iter_idx >= 1
                        and "_hwy" not in net_name
                        and (net.corridor_bbox is not None or iter_idx >= 3)
                    )
                )
                else 10
            )
            res = route_single_net(net, grid, margin=margin)
            grid.net_routes[net.name] = res
            if res.is_success:
                grid.place_net(net.name, res)

        failed_cur = [n for n, r in grid.net_routes.items() if not r.is_success]
        has_failed = bool(failed_cur)
        conflicts = grid.update_congestion(
            beta=1.5 if iter_idx >= 4 else 0.5,
            gamma=2.5 if iter_idx >= 6 else 1.5,
        )
        if len(nets) >= 30 and (
            iter_idx == 0 or (iter_idx + 1) % 2 == 0 or (conflicts == 0 and not has_failed)
        ):
            import sys

            fail_sample = f" sample={failed_cur[:3]}" if failed_cur else ""
            print(
                f"factorilog [maze_router]: iter {iter_idx + 1}/{max_iterations}: "
                f"rerouted={len(sorted_congested)} over_cap={len(over_cap_nets)} "
                f"failed={len(failed_cur)} conflicts={conflicts}{fail_sample}",
                file=sys.stderr,
                flush=True,
            )
        if conflicts == 0 and not has_failed:
            break
        if conflicts == 0 and has_failed and all(ever_failed.get(n, 0) >= 12 for n in failed_cur):
            break
        cur_score = (len(failed_cur) + conflicts, conflicts, len(failed_cur))
        if cur_score < best_score:
            best_score = cur_score
            stagnant_iters = 0
        else:
            stagnant_iters += 1
        if (
            (len(failed_cur) > 0 or len(sorted_congested) > 15)
            and iter_idx >= 30
            and stagnant_iters >= 20
        ):
            break

    if conflicts > 0 and max_iterations >= 20:
        for net_name, cur_res in list(grid.net_routes.items()):
            if cur_res.is_success and _is_net_over_capacity(cur_res):
                grid.net_routes[net_name] = RouteResult(
                    net_name=net_name,
                    nodes=cur_res.nodes,
                    edges=cur_res.edges,
                    cost=cur_res.cost,
                    is_success=False,
                    error_message=f"Unresolved congestion conflict ({conflicts} conflicted tiles)",
                )

    return grid.net_routes
