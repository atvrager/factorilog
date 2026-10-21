"""Unit tests and scale benchmarks for the 3D A* pathfinder and negotiated congestion router.

Tests state packing, MST indexing, single-net A* search, and multi-net rip-up and reroute.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import unittest
import unittest.mock
from collections.abc import Iterator

from foundry.router.grid import RoutingGrid
from foundry.router.pathfinder import (
    heuristic,
    mst_index,
    pack_state,
    route_all_nets,
    route_single_net,
    unpack_state,
)
from foundry.router.types import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    LANE_BOTH,
    LANE_LEFT,
    LANE_RIGHT,
    LAYER_SURFACE,
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
)


class TestPathfinder(unittest.TestCase):
    """Unit test suite for foundry.router.pathfinder."""

    def test_mst_index_and_state_packing(self) -> None:
        """Verify mst_index parsing, state integer packing/unpacking, and Manhattan heuristic."""
        self.assertIsNone(mst_index("iron_plate"))
        self.assertIsNone(mst_index("water_mst_bad"))
        self.assertEqual(mst_index("water_mst_0"), 0)
        self.assertEqual(mst_index("water_hwy_mst_12"), 12)
        # Verify cached lookup
        self.assertEqual(mst_index("water_hwy_mst_12"), 12)

        for x, y, z, h in ((-15, -20, 0, DIRECTION_NORTH), (140, 260, 2, DIRECTION_WEST)):
            h_idx = h >> 2
            packed = pack_state(x, y, z, h_idx)
            ux, uy, uz, uh, uh_idx = unpack_state(packed)
            self.assertEqual((ux, uy, uz, uh, uh_idx), (x, y, z, h, h_idx))

        node = RouteNode(3, 7, LAYER_SURFACE, DIRECTION_EAST)
        self.assertEqual(heuristic(node, (10, 2)), 12.0)

    def test_route_single_net_trivial_straight_and_turn(self) -> None:
        """Verify trivial zero-length, straight, and orthogonal-turn single-net routes."""
        grid = RoutingGrid(min_x=0, max_x=12, min_y=0, max_y=12)

        trivial = RouterNetSpec(
            name="net_trivial",
            source=(3, 3),
            sink=(3, 3),
            start_heading=DIRECTION_EAST,
        )
        res_triv = route_single_net(trivial, grid)
        self.assertTrue(res_triv.is_success)
        self.assertEqual(len(res_triv.nodes), 1)
        self.assertEqual(len(res_triv.edges), 0)
        self.assertEqual(res_triv.cost, 0.0)

        straight = RouterNetSpec(
            name="net_straight",
            source=(1, 2),
            sink=(5, 2),
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        grid.register_terminals([straight])
        res_st = route_single_net(straight, grid)
        self.assertTrue(res_st.is_success)
        self.assertEqual(res_st.nodes[0].pos, (1, 2))
        self.assertEqual(res_st.nodes[-1].pos, (5, 2))
        self.assertTrue(all(e.move_type == "straight" for e in res_st.edges))

        turn = RouterNetSpec(
            name="net_turn",
            source=(1, 1),
            sink=(4, 4),
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_SOUTH,
        )
        grid.register_terminals([turn])
        res_turn = route_single_net(turn, grid)
        self.assertTrue(res_turn.is_success)
        self.assertTrue(any(e.move_type == "turn" for e in res_turn.edges))

    def test_route_single_net_blocked_source_sink_and_enclosed(self) -> None:
        """Verify failure diagnostics when source, sink front tile, or path is blocked."""
        grid = RoutingGrid(
            min_x=0,
            max_x=10,
            min_y=0,
            max_y=10,
            obstacles={(1, 1), (6, 2), (4, 5), (6, 5), (5, 4), (5, 6)},
            component_headings={(6, 2): DIRECTION_WEST},
            no_hood_tiles={(5, 5)},
        )
        blocked_src = RouterNetSpec(
            name="blocked_src",
            source=(1, 1),
            sink=(4, 1),
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        res_src = route_single_net(blocked_src, grid)
        self.assertFalse(res_src.is_success)
        self.assertIn("Source", res_src.error_message or "")

        blocked_sink = RouterNetSpec(
            name="blocked_sink",
            source=(1, 2),
            sink=(5, 2),
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        res_sink = route_single_net(blocked_sink, grid)
        self.assertFalse(res_sink.is_success)
        self.assertIn("Sink", res_sink.error_message or "")

        enclosed = RouterNetSpec(
            name="enclosed",
            source=(1, 5),
            sink=(5, 5),
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
            corridor_bbox=(0, 4, 7, 6),
        )
        res_enc = route_single_net(enclosed, grid)
        self.assertFalse(res_enc.is_success)
        self.assertIn("No route found", res_enc.error_message or "")
        self.assertGreater(len(res_enc.failed_frontier), 0)

    def test_route_single_net_fluid_mst_and_highway(self) -> None:
        """Verify fluid MST branch goal sharing and highway net bay routing."""
        grid = RoutingGrid(min_x=0, max_x=220, min_y=0, max_y=120)
        mst0 = RouterNetSpec(
            name="water_mst_0",
            source=(2, 10),
            sink=(12, 10),
            commodity="water",
            is_fluid=True,
        )
        mst1 = RouterNetSpec(
            name="water_mst_1",
            source=(7, 5),
            sink=(12, 10),
            commodity="water",
            is_fluid=True,
        )
        grid.register_terminals([mst0, mst1])
        res0 = route_single_net(mst0, grid)
        self.assertTrue(res0.is_success)
        grid.place_net(mst0.name, res0)

        res1 = route_single_net(mst1, grid)
        self.assertTrue(res1.is_success)
        # Branch 1 should connect directly to the nearest tile of branch 0 at y=10
        self.assertEqual(res1.nodes[-1].y, 10)

        hwy = RouterNetSpec(
            name="iron_plate_hwy",
            source=(20, 10),
            sink=(110, 10),
            commodity="iron-plate",
            start_heading=DIRECTION_NORTH,
            end_heading=DIRECTION_SOUTH,
            corridor_bbox=(0, 0, 150, 30),
        )
        grid.register_terminals([hwy])
        res_hwy = route_single_net(hwy, grid)
        self.assertTrue(res_hwy.is_success)

    def test_route_all_nets_crossing_dual_lane_and_weaving(self) -> None:
        """Verify negotiated congestion on crossing nets, dual lanes, and multi-tier weaving."""
        grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=10)
        net_h = RouterNetSpec(
            name="net_horiz",
            source=(0, 3),
            sink=(6, 3),
            commodity="iron-plate",
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        net_v = RouterNetSpec(
            name="net_vert",
            source=(3, 0),
            sink=(3, 6),
            commodity="copper-plate",
            start_heading=DIRECTION_SOUTH,
            end_heading=DIRECTION_SOUTH,
        )
        grid.register_terminals([net_h, net_v])
        routes = route_all_nets([net_h, net_v], grid, max_iterations=20)
        self.assertTrue(routes["net_horiz"].is_success)
        self.assertTrue(routes["net_vert"].is_success)

        # Dual-lane co-routing
        grid_lane = RoutingGrid(min_x=0, max_x=8, min_y=0, max_y=6)
        net_l = RouterNetSpec(
            name="net_l",
            source=(0, 2),
            sink=(4, 2),
            lane=LANE_LEFT,
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        net_r = RouterNetSpec(
            name="net_r",
            source=(0, 2),
            sink=(4, 2),
            lane=LANE_RIGHT,
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        grid_lane.register_terminals([net_l, net_r])
        lane_routes = route_all_nets([net_l, net_r], grid_lane, max_iterations=10)
        self.assertTrue(lane_routes["net_l"].is_success)
        self.assertTrue(lane_routes["net_r"].is_success)

    def test_perf_cases_smoke(self) -> None:
        """Smoke-test all pathfinder perf_* generator benchmarks."""
        for fn in (
            perf_pathfinder_astar_leaps,
            perf_pathfinder_route_single_net_fluid_mst,
            perf_pathfinder_route_all_nets,
        ):
            for _ in fn(16):
                pass

    def test_mutant_boundary_conditions_pathfinder(self) -> None:
        """Verify boundary conditions across pathfinder helper functions."""
        from foundry.router.pathfinder import (
            _AStarContext,
            _calc_highway_heuristic,
            _can_complete_leap_at,
            _can_initiate_dive,
            _collect_same_commodity_fluid_goals,
            _compute_fallback_search_bbox,
            _has_over_capacity_node,
            _is_belt_sink_front_blocked,
            _is_goal_reached,
            _is_leap_edge_over_capacity,
            _mark_unresolved_conflict_failures,
            _probe_over_capacity_neighbors,
            _probe_subterranean_tile_blockers,
            _reroute_single_congested_net,
            _seed_initial_open_states,
            _should_terminate_congestion_loop,
            net_sort_key,
            pack_state,
        )

        # 1. net_sort_key Manhattan span (dx + dy)
        s_short = RouterNetSpec("n_short", (0, 0), (1, 4))
        s_long = RouterNetSpec("n_long", (0, 0), (0, 6))
        self.assertLess(net_sort_key(s_short), net_sort_key(s_long))
        self.assertEqual(net_sort_key(s_short)[1], 5)

        # 2. _is_belt_sink_front_blocked same-base vs foreign hood at min_x == 0
        grid = RoutingGrid(min_x=0, max_x=200, min_y=0, max_y=200)
        grid.active_hoods[(6, 5)] = "belt_a_d0_s0"
        self.assertFalse(
            _is_belt_sink_front_blocked(
                (6, 5), "belt_a", RouterNetSpec("belt_a_d1_s0", (1, 5), (5, 5)), grid
            )
        )
        self.assertTrue(
            _is_belt_sink_front_blocked(
                (6, 5), "belt_b", RouterNetSpec("belt_b_d1_s0", (1, 5), (5, 5)), grid
            )
        )

        # 3. _calc_highway_heuristic south street coordinate (by_h * 100 + 96)
        hwy_net = RouterNetSpec("iron_hwy", (50, 50), (150, 150))
        ctx = _AStarContext(
            net=hwy_net,
            grid=grid,
            sx=50,
            sy=50,
            tx=150,
            ty=150,
            sink_pos=(150, 150),
            net_name="iron_hwy",
            b_name="iron",
            my_mst=None,
            lane=LANE_BOTH,
            is_fluid=False,
            tier_name_surface="transport-belt",
            tier_ug_keys=grid.belt_tier_keys,
            ug_min_steps={1: 2},
            max_ug_span=5,
            bx0=0,
            by0=0,
            bx1=200,
            by1=200,
            existing_fluid_goals={(12, 10)},
            fluid_goals_rest=(),
            is_hwy=True,
            src_bay=None,
            snk_bay=None,
            snk_h_band=1,
            snk_v_band=1,
            bay_rows=set(),
            valid_end_headings={DIRECTION_EAST},
            open_set=[],
            g_score={},
            came_from={},
            parent_state={},
            path_hoods={},
            closed_set=set(),
            closed_xy=set(),
        )
        self.assertAlmostEqual(
            _calc_highway_heuristic(ctx, 50, 90), 1.4 * (100 + (96 - 90) + (150 - 96))
        )

        # 4. _seed_initial_open_states fluid source blocked on surface by orthogonal neighbor but hood allowed
        f_grid = RoutingGrid(min_x=0, max_x=20, min_y=0, max_y=20)
        f_grid.register_terminals(
            [
                RouterNetSpec("water_1", (5, 5), (10, 5), commodity="water", is_fluid=True),
                RouterNetSpec("steam_1", (5, 6), (10, 6), commodity="steam", is_fluid=True),
            ]
        )
        f_ctx = _AStarContext(
            net=RouterNetSpec(
                "water_1",
                (5, 5),
                (10, 5),
                commodity="water",
                is_fluid=True,
                start_heading=DIRECTION_EAST,
            ),
            grid=f_grid,
            sx=5,
            sy=5,
            tx=10,
            ty=5,
            sink_pos=(10, 5),
            net_name="water_1",
            b_name="water_1",
            my_mst=None,
            lane=LANE_BOTH,
            is_fluid=True,
            tier_name_surface="pipe",
            tier_ug_keys=f_grid.pipe_tier_keys,
            ug_min_steps={10: 1},
            max_ug_span=10,
            bx0=0,
            by0=0,
            bx1=20,
            by1=20,
            existing_fluid_goals={(8, 5)},
            fluid_goals_rest=(),
            is_hwy=False,
            src_bay=None,
            snk_bay=None,
            snk_h_band=0,
            snk_v_band=0,
            bay_rows=set(),
            valid_end_headings=set(),
            open_set=[],
            g_score={},
            came_from={},
            parent_state={},
            path_hoods={},
            closed_set=set(),
            closed_xy=set(),
        )
        self.assertFalse(
            f_grid.can_occupy_surface(
                (5, 5), "water_1", LANE_BOTH, heading=DIRECTION_EAST, is_hood=False
            )
        )
        self.assertTrue(
            f_grid.can_occupy_surface(
                (5, 5), "water_1", LANE_BOTH, heading=DIRECTION_EAST, is_hood=True
            )
        )
        _seed_initial_open_states(f_ctx)
        self.assertEqual(len(f_ctx.open_set), 1)

        # 5. _is_goal_reached on leap into existing_fluid_goals
        self.assertTrue(
            _is_goal_reached(
                f_ctx, 0, 7, 5, LAYER_SURFACE, DIRECTION_EAST, "leap", True, frozenset()
            )
        )
        self.assertFalse(
            _is_goal_reached(
                f_ctx, 0, 6, 5, LAYER_SURFACE, DIRECTION_EAST, "leap", True, frozenset()
            )
        )

        # 6. _can_initiate_dive rejects turn/leap prev_move
        self.assertFalse(
            _can_initiate_dive(ctx, (5, 5), 5, 5, DIRECTION_EAST, 1, 0, "turn", True, frozenset())
        )

        # 7. _can_complete_leap_at rejects invalid end heading at belt sink
        self.assertFalse(_can_complete_leap_at(ctx, 0, 150, 150, DIRECTION_SOUTH, 0, 1, 2))
        self.assertTrue(_can_complete_leap_at(ctx, 0, 150, 150, DIRECTION_EAST, 1, 0, 2))

        # 8. Vertical subterranean leap (dy != 0)
        v_grid = RoutingGrid(min_x=0, max_x=15, min_y=0, max_y=15, obstacles={(5, 4), (5, 5)})
        v_net = RouterNetSpec(
            "v_belt", (5, 2), (5, 8), start_heading=DIRECTION_SOUTH, end_heading=DIRECTION_SOUTH
        )
        v_res = route_single_net(v_net, v_grid)
        self.assertTrue(v_res.is_success)
        self.assertEqual(v_res.nodes[-1].pos, (5, 8))

        # 9. _compute_fallback_search_bbox max_y + wide
        fb_box = _compute_fallback_search_bbox(v_net, v_grid, 5, 5, 5, 2, 8)
        self.assertEqual(fb_box[3], 15)

        # 10. _probe_subterranean_tile_blockers only probes z_l > LAYER_SURFACE
        p_grid = RoutingGrid(min_x=0, max_x=15, min_y=0, max_y=15)
        p_grid.layer_occupancy[LAYER_SURFACE] = {(3, 3): "surf_only"}
        p_grid.layer_occupancy[1] = {(3, 3): "ug_net"}
        c_set: set[str] = set()
        f_set: set[str] = set()
        p_set: set[str] = set()
        _probe_subterranean_tile_blockers((3, 3), p_grid, c_set, f_set, p_set)
        self.assertEqual(c_set, {"ug_net"})

        # 11. _reroute_single_congested_net corridor_bbox expansion at iter_idx >= 3 vs < 3
        c_grid = RoutingGrid(
            min_x=0,
            max_x=40,
            min_y=0,
            max_y=40,
            obstacles={(5, y_obs) for y_obs in range(16)},
        )
        c_net = RouterNetSpec(
            "c_net",
            (2, 5),
            (8, 5),
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
            corridor_bbox=(0, 0, 40, 40),
        )
        c_grid.register_terminals([c_net])
        c_grid.belt_tiers.clear()
        c_grid.belt_tier_keys = ()
        with unittest.mock.patch(
            "foundry.router.pathfinder.route_single_net", wraps=route_single_net
        ) as mock_route:
            _reroute_single_congested_net("c_net", c_net, c_grid, 0, set(), {}, set())
            self.assertEqual(mock_route.call_args.kwargs.get("margin"), 10)
            c_grid.rip_up_net("c_net")
            _reroute_single_congested_net("c_net", c_net, c_grid, 1, set(), {}, set())
            self.assertEqual(mock_route.call_args.kwargs.get("margin"), 35)

        # 12. _should_terminate_congestion_loop when all failed nets hit MAX_LOCAL_FAIL_RETRIES
        self.assertTrue(_should_terminate_congestion_loop(0, ["f1"], {"f1": 12}, 1, 2, 0))
        self.assertFalse(_should_terminate_congestion_loop(0, ["f1"], {"f1": 11}, 1, 2, 0))

        # 13. Uncovered loop helpers: _collect_same_commodity_fluid_goals, _has_over_capacity_node,
        # _is_leap_edge_over_capacity, _mark_unresolved_conflict_failures, _probe_over_capacity_neighbors
        f_grid.surface_occupancy[(7, 5)] = {"water_1_mst_0"}
        self.assertEqual(
            _collect_same_commodity_fluid_goals(
                RouterNetSpec("water_1_mst_1", (5, 5), (10, 5), commodity="water", is_fluid=True),
                f_grid,
                "water_1",
            ),
            {(7, 5)},
        )
        n_a = RouteNode(2, 2, LAYER_SURFACE, DIRECTION_EAST)
        n_b = RouteNode(3, 2, LAYER_SURFACE, DIRECTION_EAST)
        r_ab = RouteResult(
            "r_ab", [n_a, n_b], [RouteEdge(n_a, n_b, "straight", "transport-belt", 1.0)], 1.0
        )
        p_grid.place_net("r_ab", r_ab)
        self.assertTrue(_has_over_capacity_node(r_ab, p_grid))
        p_grid.usage_count[(2, 2, LAYER_SURFACE)] = 3
        p_grid.surface_occupancy[(2, 3)] = {"nb_occ"}
        nb_c: set[str] = set()
        nb_b: set[str] = set()
        _probe_over_capacity_neighbors(r_ab, p_grid, nb_c, nb_b)
        self.assertIn("nb_occ", nb_b)
        p_grid.net_routes["r_ab"] = r_ab
        _mark_unresolved_conflict_failures(p_grid, 1)
        self.assertFalse(p_grid.net_routes["r_ab"].is_success)
        l_start = RouteNode(1, 4, 1, DIRECTION_EAST)
        l_end = RouteNode(3, 4, LAYER_SURFACE, DIRECTION_EAST)
        l_edge = RouteEdge(l_start, l_end, "leap", "underground-belt", 6.0)
        p_grid.usage_count[(2, 4, 1)] = 2
        self.assertTrue(_is_leap_edge_over_capacity(l_edge, p_grid))

        # 14. Vertical subterranean leap (dy != 0) to verify ey = cy + dy * step
        v_leap_grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=15, obstacles={(5, 4)})
        v_leap_net = RouterNetSpec(
            "v_leap",
            (5, 2),
            (5, 6),
            start_heading=DIRECTION_SOUTH,
            end_heading=DIRECTION_SOUTH,
        )
        v_leap_grid.register_terminals([v_leap_net])
        v_leap_res = route_single_net(v_leap_net, v_leap_grid)
        self.assertTrue(v_leap_res.is_success)
        self.assertTrue(any(e.move_type == "leap" for e in v_leap_res.edges))
        _ = pack_state(1, 1, 0, 0)


def perf_pathfinder_astar_leaps(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (32, 64, 128, 256),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for 3D A* pathfinding across obstacle-interrupted corridors."""
    obstacles = {(x, 12) for x in range(14, n + 10, 4)}
    grid = RoutingGrid(min_x=10, max_x=n + 16, min_y=10, max_y=16, obstacles=obstacles)
    net = RouterNetSpec(
        name="perf_corridor",
        source=(10, 12),
        sink=(n + 10, 12),
        commodity="iron-plate",
        start_heading=DIRECTION_EAST,
        end_heading=DIRECTION_EAST,
    )
    yield
    route_single_net(net, grid)


def perf_pathfinder_route_single_net_fluid_mst(
    n: int = 32,
    *,
    _scales: tuple[int, ...] = (16, 32, 64, 128),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for fluid MST branch routing with ancestor goal discovery."""
    grid = RoutingGrid(min_x=0, max_x=n + 20, min_y=0, max_y=20)
    trunk = RouterNetSpec(
        name="water_mst_0",
        source=(2, 10),
        sink=(n + 10, 10),
        commodity="water",
        is_fluid=True,
    )
    branch = RouterNetSpec(
        name="water_mst_1",
        source=(n // 2 + 2, 4),
        sink=(n + 10, 10),
        commodity="water",
        is_fluid=True,
    )
    grid.register_terminals([trunk, branch])
    trunk_res = route_single_net(trunk, grid)
    grid.place_net(trunk.name, trunk_res)
    yield
    route_single_net(branch, grid)


def perf_pathfinder_route_all_nets(
    n: int = 16,
    *,
    _scales: tuple[int, ...] = (8, 16, 32, 64),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for multi-net routing with rip-up and reroute in route_all_nets."""
    grid = RoutingGrid(
        min_x=0,
        max_x=30,
        min_y=0,
        max_y=n * 2 + 10,
        obstacles={(6, 1)},
        no_hood_tiles={(2, 1), (3, 1), (4, 1), (5, 1)},
    )
    nets = [
        RouterNetSpec(
            name=f"net_{i}",
            source=(2, i * 2 + 2),
            sink=(10, i * 2 + 2),
            commodity="iron-plate",
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        for i in range(n)
    ]
    nets.append(
        RouterNetSpec(
            name="net_blocked_corridor",
            source=(2, 1),
            sink=(8, 1),
            commodity="copper-plate",
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
            corridor_bbox=(2, 1, 8, 1),
        )
    )
    grid.register_terminals(nets)
    yield
    route_all_nets(nets, grid, max_iterations=1)


if __name__ == "__main__":
    unittest.main()
