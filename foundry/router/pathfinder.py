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


def mst_index(net_name: str) -> int | None:
    """Return the integer MST branch index if net_name is an MST branch, else None."""
    if "_mst_" in net_name:
        try:
            return int(net_name.rsplit("_mst_", 1)[1])
        except ValueError:
            return None
    return None


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
        for i in range(my_mst):
            anc_name = f"{b_name}_mst_{i}"
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

    def calc_h(x: int, y: int) -> float:
        if is_fluid and existing_fluid_goals:
            return 1.4 * float(min(abs(x - gx) + abs(y - gy) for gx, gy in target_goals))
        return 1.4 * float(abs(x - tx) + abs(y - ty))

    # open_set stores (f_score, h_val, state_id)
    open_set: list[tuple[float, float, int]] = []
    g_score: dict[int, float] = {}
    came_from: dict[int, tuple[int, str, str, float]] = {}
    path_hoods: dict[int, frozenset[tuple[int, int]]] = {}
    closed_set: set[int] = set()

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

    while open_set:
        if len(closed_set) >= 40000:
            break
        _, _, current = heapq.heappop(open_set)

        if current in closed_set:
            continue
        closed_set.add(current)

        cx, cy, cz, ch, ch_idx = unpack_state(current)
        curr_hoods = path_hoods.get(current, frozenset())

        # Check goal condition
        prev_move = came_from[current][1] if current in came_from else None
        if cz == LAYER_SURFACE:
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
                        and base_net_name(grid.active_hoods[front_pos]) != b_name
                        and not grid.is_net_fluid(grid.active_hoods[front_pos])
                    ):
                        bad_front = True
                    if front_pos in grid.component_headings and net.terminal_heading is None:
                        bad_front = True
                    if not bad_front:
                        goal_state = current
                        break
                else:
                    goal_state = current
                    break
            if is_fluid and (cx, cy) in existing_fluid_goals and prev_move != "leap":
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
            can_surf_here = True
            if cx == sx and cy == sy and is_fluid:
                can_surf_here = grid.can_occupy_surface(
                    net.source, net_name, lane, heading=ch, is_hood=False
                )

            if can_surf_here:
                next_visited = (curr_hoods | {cpos}) if is_fluid else curr_hoods
                # 1. Straight
                nx = cx + dx
                ny = cy + dy
                if bx0 <= nx <= bx1 and by0 <= ny <= by1:
                    npos = (nx, ny)
                    if npos not in curr_hoods:
                        nxt_s = pack_state(nx, ny, LAYER_SURFACE, ch_idx)
                        if nxt_s not in closed_set:
                            edge_cost = grid.get_fast_node_cost(
                                nx, ny, LAYER_SURFACE, STEP_COST, net_name, lane
                            )
                            tentative_g = curr_g + edge_cost
                            if tentative_g < g_score.get(
                                nxt_s, float("inf")
                            ) and grid.can_occupy_surface(npos, net_name, lane, heading=ch):
                                g_score[nxt_s] = tentative_g
                                came_from[nxt_s] = (
                                    current,
                                    "straight",
                                    tier_name_surface,
                                    edge_cost,
                                )
                                path_hoods[nxt_s] = next_visited
                                h_val = calc_h(nx, ny)
                                heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))

                        # 2. Turns
                        for th in ((ch + 4) & 15, (ch + 12) & 15):
                            th_idx = th >> 2
                            nxt_s = pack_state(nx, ny, LAYER_SURFACE, th_idx)
                            if nxt_s not in closed_set:
                                edge_cost = grid.get_fast_node_cost(
                                    nx, ny, LAYER_SURFACE, STEP_COST + TURN_PENALTY, net_name, lane
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
                    is_fluid
                    or grid.should_consider_underground(
                        cx, cy, dx, dy, max_ug_span, net_name, lane, target_pos=sink_pos
                    )
                )
                and grid.can_place_hood(cpos, net_name, heading=ch, is_output=False)
            ):
                new_dive_hoods = curr_hoods | {cpos}
                for target_z in tier_ug_keys:
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
                            path_hoods[nxt_s] = new_dive_hoods
                            h_val = calc_h(cx, cy)
                            heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))

            if prev_move == "leap" and is_fluid and tier_ug_keys:
                nx = cx + dx
                ny = cy + dy
                npos = (nx, ny)
                if (
                    grid.is_in_bounds(nx, ny)
                    and npos not in curr_hoods
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
                                path_hoods[nxt_s] = new_dive_hoods
                                h_val = calc_h(nx, ny)
                                heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))
        else:
            # Subterranean leaps
            max_span = grid.get_tier_span(cz)
            is_horiz = dx != 0
            occ = grid.layer_occupancy.get(cz, {})
            if is_fluid:
                axis_spans = (
                    grid.active_pipe_horiz_spans if is_horiz else grid.active_pipe_vert_spans
                )
            else:
                axis_spans = (
                    grid.active_belt_horiz_spans if is_horiz else grid.active_belt_vert_spans
                )
            tier_name = (
                grid.pipe_tiers.get(cz, {}).get("name", "pipe-to-ground")
                if is_fluid
                else grid.belt_tiers.get(cz, {}).get("name", "underground-belt")
            )

            for step in range(1, max_span + 1):
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
                if pt in occ:
                    break

                if pt in curr_hoods:
                    break

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
                )
                tentative_g = curr_g + edge_cost
                if tentative_g >= g_score.get(nxt_s, float("inf")):
                    continue

                if not grid.can_place_hood(pt, net_name, heading=ch, is_output=True):
                    continue

                if grid.can_occupy_surface(pt, net_name, lane, heading=ch, is_hood=True):
                    g_score[nxt_s] = tentative_g
                    came_from[nxt_s] = (current, "leap", tier_name, edge_cost)
                    path_hoods[nxt_s] = curr_hoods | {pt}
                    h_val = calc_h(ex, ey)
                    heapq.heappush(open_set, (tentative_g + h_val, h_val, nxt_s))

    if goal_state is None:
        coords = sorted(
            {(unpack_state(s)[0], unpack_state(s)[1], unpack_state(s)[2]) for s in closed_set}
        )
        return RouteResult(
            net_name=net.name,
            nodes=[],
            edges=[],
            cost=float("inf"),
            is_success=False,
            error_message=(
                f"No route found from {net.source} to {net.sink}. "
                f"Explored {len(closed_set)} states. Coords: {coords[:40]}."
            ),
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
    if span > 120:
        margin = max(margin, min(60, span // 8))

    my_mst = mst_index(net.name)
    b_name = base_net_name(net.name)
    if net.is_fluid:
        margin = max(margin, 25)
    if net.is_fluid and my_mst is not None and my_mst > 0:
        closest_goal = None
        best_d = float("inf")
        for i in range(my_mst):
            anc_route = grid.net_routes.get(f"{b_name}_mst_{i}")
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

    grid.active_bbox = (
        max(grid.min_x, min_x - margin),
        max(grid.min_y, min_y - margin),
        min(grid.max_x, max_x + margin),
        min(grid.max_y, max_y + margin),
    )
    res = _a_star_search(net, grid)
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
    ever_failed: dict[str, int] = {}

    # Negotiated congestion iterations
    for iter_idx in range(max_iterations):
        congested_nets: set[str] = set()
        failed_bases: set[str] = set()
        for net_name, res in grid.net_routes.items():
            if not res.is_success:
                congested_nets.add(net_name)
                failed_bases.add(base_net_name(net_name))
                ever_failed[net_name] = ever_failed.get(net_name, 0) + 1
                f_spec = net_map[net_name]
                rx = 12 if len(nets) > 100 else 6
                for term_pos in (f_spec.source, f_spec.sink):
                    for odx in range(-rx, rx + 1):
                        for ody in range(-6, 7):
                            nb = (term_pos[0] + odx, term_pos[1] + ody)
                            if nb in grid.surface_occupancy:
                                congested_nets |= grid.surface_occupancy[nb]
                                if abs(odx) <= 3 and abs(ody) <= 3:
                                    h_key = (nb[0], nb[1], 0)
                                    grid.historical_cost[h_key] = (
                                        grid.historical_cost.get(h_key, 0.0) + 1.0
                                    )
                                    grid.has_congestion = True
                            if nb in grid.active_hoods:
                                congested_nets.add(grid.active_hoods[nb])
                                h_key = (nb[0], nb[1], 0)
                                grid.historical_cost[h_key] = (
                                    grid.historical_cost.get(h_key, 0.0) + 2.0
                                )
                                grid.has_congestion = True
            else:
                is_congested = False
                for node in res.nodes:
                    node_key = (node.x, node.y, node.z)
                    if grid.usage_count.get(node_key, 0) > grid.get_capacity(node_key):
                        congested_nets.add(net_name)
                        is_congested = True
                        break
                if not is_congested:
                    for edge in res.edges:
                        if edge.move_type == "leap":
                            start = edge.start
                            end = edge.end
                            dx = 1 if end.x > start.x else (-1 if end.x < start.x else 0)
                            dy = 1 if end.y > start.y else (-1 if end.y < start.y else 0)
                            dist = max(abs(end.x - start.x), abs(end.y - start.y))
                            for s in range(dist + 1):
                                node_key = (start.x + dx * s, start.y + dy * s, start.z)
                                if grid.usage_count.get(node_key, 0) > grid.get_capacity(node_key):
                                    congested_nets.add(net_name)
                                    is_congested = True
                                    break
                            if is_congested:
                                break

        if not congested_nets:
            break

        # When an MST branch rips up, also rip up its descendant MST branches
        # And if an MST branch failed, rip up all branches of its base net so the tree routes before blockers
        extra_mst: set[str] = set()
        for cn in congested_nets:
            c_idx = mst_index(cn)
            if c_idx is not None:
                c_base = base_net_name(cn)
                is_fail = not grid.net_routes[cn].is_success
                for other_name in grid.net_routes:
                    o_idx = mst_index(other_name)
                    if (
                        o_idx is not None
                        and base_net_name(other_name) == c_base
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
                0 if n in prev_failed else 1,
                -ever_failed.get(n, 0),
                *net_sort_key(net_map[n]),
            ),
        )
        for net_name in sorted_congested:
            grid.rip_up_net(net_name)

        for net_name in sorted_congested:
            net = net_map[net_name]
            margin = 35 if (net_name in prev_failed or net_name in ever_failed) else 10
            res = route_single_net(net, grid, margin=margin)
            grid.net_routes[net.name] = res
            if res.is_success:
                grid.place_net(net.name, res)

        failed_cur = [n for n, r in grid.net_routes.items() if not r.is_success]
        has_failed = bool(failed_cur)
        conflicts = grid.update_congestion(beta=1.5 if (iter_idx >= 30 and not has_failed) else 0.5)
        if conflicts == 0 and not has_failed:
            break

    return grid.net_routes
