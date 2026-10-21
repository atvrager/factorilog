"""Unit tests and scale benchmarks for the 3D A* pathfinder and negotiated congestion router.

Tests state packing, MST indexing, single-net A* search, and multi-net rip-up and reroute.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import unittest
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
    LANE_LEFT,
    LANE_RIGHT,
    LAYER_SURFACE,
    RouteNode,
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
