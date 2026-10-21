"""Unit tests and scale benchmarks for RoutingGrid and base_net_name.

Tests multi-layer occupancy, hood and surface DRC rules, and congestion updates.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import unittest
from collections.abc import Iterator

from foundry.router.grid import RoutingGrid, base_net_name
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


class TestRoutingGrid(unittest.TestCase):
    """Unit test suite for RoutingGrid and base_net_name."""

    def test_base_net_name_strips_suffixes(self) -> None:
        """Verify that base_net_name strips hierarchy, driver/sink, and MST suffixes."""
        self.assertEqual(base_net_name("iron_plate"), "iron_plate")
        self.assertEqual(base_net_name("iron_plate_hwy"), "iron_plate")
        self.assertEqual(base_net_name("iron_plate_export_1_2"), "iron_plate")
        self.assertEqual(base_net_name("copper_plate_import_0_1"), "copper_plate")
        self.assertEqual(base_net_name("steel_plate_hwy_3"), "steel_plate")
        self.assertEqual(base_net_name("coal_blk_2_2"), "coal")
        self.assertEqual(base_net_name("electronic_circuit_d0_s3"), "electronic_circuit")
        self.assertEqual(base_net_name("water_mst_4"), "water")
        self.assertEqual(base_net_name("water_hwy_mst_2"), "water")
        # Confirm second call returns cached result identically
        self.assertEqual(base_net_name("electronic_circuit_d0_s3"), "electronic_circuit")

    def test_grid_bounds_obstacles_and_tiers(self) -> None:
        """Verify bounds checking, active_bbox clipping, obstacle lookup, and tier discovery."""
        grid = RoutingGrid(min_x=2, max_x=10, min_y=3, max_y=12, obstacles={(4, 5), (20, 20)})
        self.assertEqual(grid.width, 9)
        self.assertEqual(grid.height, 10)
        self.assertTrue(grid.is_in_bounds(2, 3))
        self.assertTrue(grid.is_in_bounds(10, 12))
        self.assertFalse(grid.is_in_bounds(1, 3))
        self.assertFalse(grid.is_in_bounds(10, 13))

        grid.active_bbox = (4, 5, 6, 7)
        self.assertTrue(grid.is_in_bounds(4, 5))
        self.assertFalse(grid.is_in_bounds(3, 5))
        grid.active_bbox = None

        self.assertTrue(grid.is_obstacle(4, 5))
        self.assertFalse(grid.is_obstacle(5, 5))
        self.assertFalse(grid.is_obstacle(20, 20))

        self.assertGreater(len(grid.belt_tier_keys), 0)
        self.assertGreater(len(grid.pipe_tier_keys), 0)
        first_belt_z = grid.belt_tier_keys[0]
        first_pipe_z = grid.pipe_tier_keys[0]
        self.assertGreater(grid.get_tier_span(first_belt_z), 0)
        self.assertGreater(grid.get_tier_span(first_pipe_z), 0)
        self.assertEqual(grid.get_tier_cost(first_belt_z), 0.0)
        self.assertEqual(grid.get_tier_cost(first_pipe_z), 0.0)
        if len(grid.belt_tier_keys) > 1:
            self.assertGreater(grid.get_tier_cost(grid.belt_tier_keys[1]), 0.0)
        self.assertEqual(grid.get_tier_cost(999), 0.0)
        self.assertLessEqual(grid.get_tier_span(999), grid.max_belt_span)

    def test_register_terminals_and_commodity_helpers(self) -> None:
        """Verify terminal registration, fluid port approaches, and commodity normalization."""
        grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=10)
        belt_net = RouterNetSpec(
            name="iron_plate__net_d0_s0",
            source=(1, 1),
            sink=(5, 1),
            commodity="iron_plate",
            is_fluid=False,
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        fluid_net = RouterNetSpec(
            name="sulfuric_acid__net_mst_0",
            source=(2, 4),
            sink=(8, 4),
            commodity="sulfuric_acid",
            is_fluid=True,
            source_inward_dir=DIRECTION_WEST,
            sink_inward_dir=DIRECTION_EAST,
        )
        grid.register_terminals([belt_net, fluid_net])

        self.assertFalse(grid.is_net_fluid("iron_plate__net_d0_s0"))
        self.assertFalse(grid.is_net_fluid("iron_plate__net_d1_s2"))
        self.assertTrue(grid.is_net_fluid("sulfuric_acid__net_mst_0"))
        self.assertTrue(grid.is_net_fluid("sulfuric_acid__net_mst_1"))
        self.assertEqual(grid.get_net_commodity("sulfuric_acid__net_mst_1"), "sulfuric-acid")
        self.assertEqual(grid.get_net_commodity("crude_oil__unregistered"), "crude-oil")
        self.assertEqual(grid.terminal_headings[(1, 1)], DIRECTION_EAST)
        self.assertEqual(grid.terminal_headings[(5, 1)], DIRECTION_EAST)
        self.assertIn("sulfuric-acid", grid.fluid_port_approaches[(3, 4)])
        self.assertIn("sulfuric-acid", grid.fluid_port_approaches[(7, 4)])

    def test_surface_fluid_info_and_hood_mouths(self) -> None:
        """Verify get_surface_fluid_info on empty, belt, surface pipe, and fluid hood tiles."""
        grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=10)
        water_net = RouterNetSpec(
            name="water_net",
            source=(1, 2),
            sink=(6, 2),
            commodity="water",
            is_fluid=True,
        )
        grid.register_terminals([water_net])
        self.assertEqual(grid.get_surface_fluid_info((2, 2)), (False, "", None))

        pipe_z = grid.pipe_tier_keys[0]
        n0 = RouteNode(1, 2, LAYER_SURFACE, DIRECTION_EAST)
        n1 = RouteNode(2, 2, LAYER_SURFACE, DIRECTION_EAST)
        n2 = RouteNode(2, 2, pipe_z, DIRECTION_EAST)
        n3 = RouteNode(5, 2, LAYER_SURFACE, DIRECTION_EAST)
        edges = [
            RouteEdge(n0, n1, "straight", "pipe", 1.0),
            RouteEdge(n1, n2, "dive", "pipe-to-ground", 4.0),
            RouteEdge(n2, n3, "leap", "pipe-to-ground", 7.0),
        ]
        route = RouteResult("water_net", [n0, n1, n2, n3], edges, 12.0, is_success=True)
        grid.place_net("water_net", route)

        self.assertEqual(grid.get_surface_fluid_info((1, 2)), (True, "water", None))
        # Input hood at (2, 2) heading East has mouth to the West at (1, 2)
        self.assertEqual(grid.get_surface_fluid_info((2, 2)), (True, "water", (1, 2)))
        # Output hood at (5, 2) heading East has mouth to the East at (6, 2)
        self.assertEqual(grid.get_surface_fluid_info((5, 2)), (True, "water", (6, 2)))

        grid.rip_up_net("water_net")
        self.assertEqual(grid.get_surface_fluid_info((1, 2)), (False, "", None))
        self.assertEqual(grid.get_surface_fluid_info((2, 2)), (False, "", None))
        self.assertEqual(grid.get_surface_fluid_info((5, 2)), (False, "", None))

    def test_can_place_hood_belt_and_fluid_constraints(self) -> None:
        """Verify DRC-03 belt hood clearance and fluid hood conflict checks."""
        grid = RoutingGrid(
            min_x=0,
            max_x=10,
            min_y=0,
            max_y=10,
            obstacles={(1, 1)},
            no_hood_tiles={(2, 2)},
            component_headings={(4, 5): DIRECTION_SOUTH},
        )
        net_a = RouterNetSpec("belt_a", (0, 5), (8, 5), commodity="iron-plate")
        net_b = RouterNetSpec("belt_b", (0, 6), (8, 6), commodity="copper-plate")
        fluid_w = RouterNetSpec("water_a", (0, 8), (8, 8), commodity="water", is_fluid=True)
        fluid_o = RouterNetSpec("oil_a", (0, 9), (8, 9), commodity="crude-oil", is_fluid=True)
        grid.register_terminals([net_a, net_b, fluid_w, fluid_o])

        self.assertFalse(grid.can_place_hood((1, 1), "belt_a", DIRECTION_EAST))
        self.assertFalse(grid.can_place_hood((2, 2), "belt_a", DIRECTION_EAST))
        self.assertFalse(grid.can_place_hood((0, 6), "belt_a", DIRECTION_EAST))
        # Component at (4, 5) points South into (4, 6), blocking an East-facing hood at (4, 6)
        self.assertFalse(grid.can_place_hood((4, 6), "belt_a", DIRECTION_EAST))
        self.assertTrue(grid.can_place_hood((4, 7), "belt_a", DIRECTION_EAST))

        # Fluid hood mouth pointing into a different fluid terminal is rejected
        self.assertFalse(
            grid.can_place_hood((0, 8), "oil_a", heading=DIRECTION_NORTH, is_output=False)
        )
        self.assertTrue(
            grid.can_place_hood((0, 8), "water_a", heading=DIRECTION_EAST, is_output=False)
        )

    def test_can_occupy_surface_and_subterranean_rules(self) -> None:
        """Verify surface occupancy, sink front tile protection, and subterranean span checks."""
        grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=10, obstacles={(3, 3)})
        net_a = RouterNetSpec("belt_a", (1, 2), (4, 2), commodity="iron-plate")
        net_b = RouterNetSpec("belt_b", (1, 3), (6, 3), commodity="copper-plate")
        grid.register_terminals([net_a, net_b])

        n0 = RouteNode(3, 2, LAYER_SURFACE, DIRECTION_EAST)
        n1 = RouteNode(4, 2, LAYER_SURFACE, DIRECTION_EAST)
        route_a = RouteResult(
            "belt_a", [n0, n1], [RouteEdge(n0, n1, "straight", "transport-belt", 1.0)], 1.0
        )
        grid.place_net("belt_a", route_a)

        # (5, 2) is the front tile of belt_a's sink at (4, 2) heading East
        self.assertFalse(grid.can_occupy_surface((5, 2), "belt_b", LANE_BOTH, DIRECTION_EAST))
        self.assertTrue(grid.can_occupy_surface((5, 2), "belt_a", LANE_BOTH, DIRECTION_EAST))

        # Subterranean layer occupancy and leap span checks
        belt_z = grid.belt_tier_keys[0]
        dn0 = RouteNode(1, 5, LAYER_SURFACE, DIRECTION_EAST)
        dn1 = RouteNode(1, 5, belt_z, DIRECTION_EAST)
        dn2 = RouteNode(4, 5, LAYER_SURFACE, DIRECTION_EAST)
        dive_route = RouteResult(
            "belt_a",
            [dn0, dn1, dn2],
            [
                RouteEdge(dn0, dn1, "dive", "underground-belt", 4.0),
                RouteEdge(dn1, dn2, "leap", "underground-belt", 7.0),
            ],
            11.0,
        )
        grid.rip_up_net("belt_a")
        grid.place_net("belt_a", dive_route)

        self.assertFalse(grid.can_leap_span([(2, 5), (3, 5)], True, "belt_b"))
        self.assertTrue(grid.can_leap_span([(2, 5), (3, 5)], False, "belt_b"))
        self.assertFalse(grid.can_occupy_subterranean(belt_z, [(2, 5)], "belt_b"))
        self.assertTrue(grid.can_occupy_subterranean(belt_z, [(2, 5)], "belt_a"))

        # Check should_consider_underground ahead of obstacle at (3, 3)
        self.assertTrue(
            grid.should_consider_underground(
                1, 3, 1, 0, grid.max_belt_span, "belt_b", LANE_BOTH, target_pos=(8, 3)
            )
        )
        self.assertFalse(
            grid.should_consider_underground(
                1, 7, 1, 0, grid.max_belt_span, "belt_b", LANE_BOTH, target_pos=(8, 7)
            )
        )

    def test_congestion_capacity_and_node_costs(self) -> None:
        """Verify dual-lane capacity, same-fluid sharing capacity, and congestion cost updates."""
        grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=10)
        left_net = RouterNetSpec("left_net", (1, 1), (3, 1), commodity="iron-plate", lane=LANE_LEFT)
        right_net = RouterNetSpec(
            "right_net", (1, 1), (3, 1), commodity="copper-plate", lane=LANE_RIGHT
        )
        both_net = RouterNetSpec("both_net", (1, 1), (3, 1), commodity="coal", lane=LANE_BOTH)
        grid.register_terminals([left_net, right_net, both_net])

        ln0 = RouteNode(1, 1, LAYER_SURFACE, DIRECTION_EAST, LANE_LEFT)
        ln1 = RouteNode(2, 1, LAYER_SURFACE, DIRECTION_EAST, LANE_LEFT)
        rn0 = RouteNode(1, 1, LAYER_SURFACE, DIRECTION_EAST, LANE_RIGHT)
        rn1 = RouteNode(2, 1, LAYER_SURFACE, DIRECTION_EAST, LANE_RIGHT)
        grid.place_net(
            "left_net",
            RouteResult(
                "left_net",
                [ln0, ln1],
                [RouteEdge(ln0, ln1, "straight", "transport-belt", 1.0)],
                1.0,
            ),
        )
        grid.place_net(
            "right_net",
            RouteResult(
                "right_net",
                [rn0, rn1],
                [RouteEdge(rn0, rn1, "straight", "transport-belt", 1.0)],
                1.0,
            ),
        )

        # Two single-lane belts on opposite lanes with identical heading have capacity 2 (0 conflicts)
        self.assertEqual(grid.get_capacity((2, 1, LAYER_SURFACE)), 2)
        self.assertEqual(grid.update_congestion(), 0)

        # Adding a full-belt net on the same tile exceeds capacity
        bn0 = RouteNode(1, 1, LAYER_SURFACE, DIRECTION_EAST, LANE_BOTH)
        bn1 = RouteNode(2, 1, LAYER_SURFACE, DIRECTION_EAST, LANE_BOTH)
        grid.place_net(
            "both_net",
            RouteResult(
                "both_net",
                [bn0, bn1],
                [RouteEdge(bn0, bn1, "straight", "transport-belt", 1.0)],
                1.0,
            ),
        )
        self.assertEqual(grid.get_capacity((2, 1, LAYER_SURFACE)), 1)
        conflicts = grid.update_congestion(beta=0.5, gamma=1.5)
        self.assertGreater(conflicts, 0)
        self.assertTrue(grid.has_congestion)
        self.assertGreater(
            grid.get_node_cost(bn1, 1.0, net_name="other_net", lane=LANE_BOTH),
            1.0,
        )

    def test_perf_cases_smoke(self) -> None:
        """Smoke-test all grid perf_* generator benchmarks."""
        for fn in (
            perf_grid_register_terminals,
            perf_grid_place_and_rip_up_net,
            perf_grid_update_congestion,
            perf_grid_can_occupy_and_hood_checks,
        ):
            for _ in fn(16):
                pass


def perf_grid_register_terminals(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for RoutingGrid initialization and register_terminals."""
    base_grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=10)
    prototypes = base_grid.prototypes
    nets = [
        RouterNetSpec(
            name=f"water_mst_{i}" if i % 2 == 0 else f"iron_plate__blk_{i}_d0_s1",
            source=(i + 1, 2),
            sink=(i + 1, 15),
            commodity="water" if i % 2 == 0 else "iron-plate",
            is_fluid=(i % 2 == 0),
            start_heading=DIRECTION_SOUTH,
            end_heading=DIRECTION_SOUTH,
            source_inward_dir=DIRECTION_NORTH if i % 2 == 0 else None,
            sink_inward_dir=DIRECTION_SOUTH if i % 2 == 0 else None,
        )
        for i in range(n)
    ]
    yield
    grid = RoutingGrid(min_x=0, max_x=n + 20, min_y=0, max_y=20, prototypes=prototypes)
    grid.register_terminals(nets)


def perf_grid_place_and_rip_up_net(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (32, 64, 128, 256),
    _max_complexity: str = "O(N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for RoutingGrid.place_net and rip_up_net across surface and leap edges."""
    grid = RoutingGrid(min_x=0, max_x=n * 4 + 20, min_y=0, max_y=20)
    belt_z = grid.belt_tier_keys[0]
    routes: list[tuple[str, RouteResult]] = []
    for i in range(n):
        x0 = i * 4 + 1
        n0 = RouteNode(x0, 5, LAYER_SURFACE, DIRECTION_EAST)
        n1 = RouteNode(x0 + 1, 5, LAYER_SURFACE, DIRECTION_EAST)
        n2 = RouteNode(x0 + 1, 5, belt_z, DIRECTION_EAST)
        n3 = RouteNode(x0 + 3, 5, LAYER_SURFACE, DIRECTION_EAST)
        edges = [
            RouteEdge(n0, n1, "straight", "transport-belt", 1.0),
            RouteEdge(n1, n2, "dive", "underground-belt", 4.0),
            RouteEdge(n2, n3, "leap", "underground-belt", 6.0),
        ]
        name = f"net_{i}"
        routes.append((name, RouteResult(name, [n0, n1, n2, n3], edges, 11.0)))
    yield
    for name, route in routes:
        grid.place_net(name, route)
    for name, _route in routes:
        grid.rip_up_net(name)


def perf_grid_update_congestion(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for RoutingGrid.update_congestion and get_capacity."""
    grid = RoutingGrid(min_x=0, max_x=n + 10, min_y=0, max_y=10)
    for i in range(n):
        n0 = RouteNode(i + 1, 2, LAYER_SURFACE, DIRECTION_EAST)
        n1 = RouteNode(i + 2, 2, LAYER_SURFACE, DIRECTION_EAST)
        edge = RouteEdge(n0, n1, "straight", "transport-belt", 1.0)
        grid.place_net(f"net_a_{i}", RouteResult(f"net_a_{i}", [n0, n1], [edge], 1.0))
        grid.place_net(f"net_b_{i}", RouteResult(f"net_b_{i}", [n0, n1], [edge], 1.0))
    yield
    grid.update_congestion()


def perf_grid_can_occupy_and_hood_checks(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for can_occupy_surface, can_place_hood, and should_consider_underground."""
    obstacles = {(i * 2 + 2, 4) for i in range(n)}
    grid = RoutingGrid(min_x=0, max_x=n * 2 + 10, min_y=0, max_y=10, obstacles=obstacles)
    belt_z = grid.belt_tier_keys[0]
    nets = [
        RouterNetSpec(
            f"water_{i}", (i * 2 + 1, 2), (i * 2 + 1, 8), commodity="water", is_fluid=True
        )
        for i in range(n)
    ]
    grid.register_terminals(nets)
    yield
    for i in range(n):
        x = i * 2 + 1
        grid.can_occupy_surface((x, 4), f"belt_{i}", LANE_BOTH, heading=DIRECTION_EAST)
        grid.can_place_hood((x, 4), f"belt_{i}", heading=DIRECTION_EAST, is_output=False)
        grid.can_occupy_surface((x, 2), f"water_{i}", LANE_BOTH, heading=DIRECTION_SOUTH)
        grid.can_place_hood((x, 2), f"water_{i}", heading=DIRECTION_SOUTH, is_output=False)
        grid.should_consider_underground(x, 4, 1, 0, grid.max_belt_span, f"belt_{i}", LANE_BOTH)
        grid.can_leap_span([(x, 4)], True, f"belt_{i}")
        grid.can_occupy_subterranean(belt_z, [(x, 4)], f"belt_{i}")


if __name__ == "__main__":
    unittest.main()
