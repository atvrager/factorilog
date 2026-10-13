"""Unit and integration tests for DualLaneMazeRouter.

Tests straight runs, turns, obstacle avoidance, crossing dives, and weaving.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import unittest

from foundry.native_engine.engine import FactorioNativeEngine
from foundry.native_engine.symbol_finder import resolve_factorio_binary
from foundry.router import (
    DualLaneMazeRouter,
    LANE_LEFT,
    LANE_RIGHT,
    RouterNetSpec,
)


class TestDualLaneMazeRouter(unittest.TestCase):
    """Test suite for 3D multi-layer maze router."""

    def setUp(self) -> None:
        self.game_bin = resolve_factorio_binary()
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")
        self.engine = FactorioNativeEngine(self.game_bin)

    def test_router_straight_belt_route(self) -> None:
        """Verify that a straight net routes and delivers items in simulation."""
        net = RouterNetSpec(
            name="net_straight",
            source=(0, 2),
            sink=(4, 2),
            commodity="iron-plate",
            start_heading=4,
            end_heading=4,
        )
        router = DualLaneMazeRouter(
            min_x=0,
            max_x=8,
            min_y=0,
            max_y=6,
            engine=self.engine,
        )

        routes, entities, res = router.route([net], verify=True)
        self.assertTrue(routes["net_straight"].is_success)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)
        self.assertEqual(len(res.lvs_errors), 0)
        self.assertEqual(res.delivered_items.get("net_straight"), 1)

    def test_router_turning_belt_route(self) -> None:
        """Verify that an orthogonal turn routes and delivers items cleanly."""
        net = RouterNetSpec(
            name="net_turn",
            source=(0, 0),
            sink=(3, 3),
            commodity="copper-plate",
            start_heading=4,
            end_heading=8,
        )
        router = DualLaneMazeRouter(
            min_x=0,
            max_x=6,
            min_y=0,
            max_y=6,
            engine=self.engine,
        )

        routes, entities, res = router.route([net], verify=True)
        self.assertTrue(routes["net_turn"].is_success)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)
        self.assertEqual(len(res.lvs_errors), 0)
        self.assertEqual(res.delivered_items.get("net_turn"), 1)

    def test_router_obstacle_avoidance(self) -> None:
        """Verify that the router circumvents a fixed obstacle."""
        obstacles = {(2, 1)}
        net = RouterNetSpec(
            name="net_avoid",
            source=(0, 1),
            sink=(4, 1),
            commodity="steel-plate",
            start_heading=4,
            end_heading=4,
        )
        router = DualLaneMazeRouter(
            min_x=0,
            max_x=8,
            min_y=0,
            max_y=6,
            engine=self.engine,
            obstacles=obstacles,
        )

        routes, entities, res = router.route([net], verify=True)
        self.assertTrue(routes["net_avoid"].is_success)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue(res.is_valid)
        self.assertEqual(res.delivered_items.get("net_avoid"), 1)

        # Confirm no surface belt occupies obstacle tile (2, 1)
        for ent in entities:
            pos = ent["position"]
            if ent.get("name") == "transport-belt":
                self.assertFalse(pos["x"] == 2.5 and pos["y"] == 1.5)

    def test_router_crossing_nets_automatic_dive(self) -> None:
        """Verify that two crossing nets resolve through an underground pair."""
        # Net 1 runs East along y=2
        # Net 2 runs South along x=3
        net1 = RouterNetSpec(
            name="net_horiz",
            source=(0, 2),
            sink=(6, 2),
            commodity="iron-plate",
            start_heading=4,
            end_heading=4,
        )
        net2 = RouterNetSpec(
            name="net_vert",
            source=(3, 0),
            sink=(3, 4),
            commodity="copper-plate",
            start_heading=8,
            end_heading=8,
        )
        router = DualLaneMazeRouter(
            min_x=0,
            max_x=8,
            min_y=0,
            max_y=6,
            engine=self.engine,
        )

        routes, entities, res = router.route([net1, net2], verify=True, max_iterations=20)
        self.assertTrue(routes["net_horiz"].is_success)
        self.assertTrue(routes["net_vert"].is_success)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)
        self.assertEqual(len(res.lvs_errors), 0)
        self.assertEqual(res.delivered_items.get("net_horiz"), 1)
        self.assertEqual(res.delivered_items.get("net_vert"), 1)

        # Confirm at least one net placed an underground belt pair
        ug_entities = [e for e in entities if "underground-belt" in e.get("name", "")]
        self.assertGreater(len(ug_entities), 0)

    def test_router_pipe_and_belt_crossing(self) -> None:
        """Verify that a fluid pipe net and belt net cross without contamination."""
        net_pipe = RouterNetSpec(
            name="net_water",
            source=(0, 2),
            sink=(5, 2),
            commodity="water",
            is_fluid=True,
            start_heading=4,
            end_heading=4,
        )
        net_belt = RouterNetSpec(
            name="net_coal",
            source=(2, 0),
            sink=(2, 4),
            commodity="coal",
            is_fluid=False,
            start_heading=8,
            end_heading=8,
        )
        router = DualLaneMazeRouter(
            min_x=0,
            max_x=8,
            min_y=0,
            max_y=6,
            engine=self.engine,
        )

        routes, entities, res = router.route([net_pipe, net_belt], verify=True, max_iterations=20)
        self.assertTrue(routes["net_water"].is_success)
        self.assertTrue(routes["net_coal"].is_success)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)
        self.assertEqual(len(res.lvs_errors), 0)
        self.assertGreater(res.delivered_fluids.get("net_water", 0.0), 0.0)
        self.assertEqual(res.delivered_items.get("net_coal"), 1)

    def test_router_dual_lane_packing(self) -> None:
        """Verify that two single-lane nets co-route on opposite lanes."""
        net_left = RouterNetSpec(
            name="net_left",
            source=(0, 2),
            sink=(4, 2),
            commodity="iron-plate",
            lane=LANE_LEFT,
            start_heading=4,
            end_heading=4,
        )
        net_right = RouterNetSpec(
            name="net_right",
            source=(0, 2),
            sink=(4, 2),
            commodity="copper-plate",
            lane=LANE_RIGHT,
            start_heading=4,
            end_heading=4,
        )
        router = DualLaneMazeRouter(
            min_x=0,
            max_x=6,
            min_y=0,
            max_y=4,
            engine=self.engine,
        )

        routes, entities, res = router.route([net_left, net_right], verify=False)
        self.assertTrue(routes["net_left"].is_success)
        self.assertTrue(routes["net_right"].is_success)
        # Verify deduplicated physical entities along the corridor
        self.assertEqual(len(entities), 5)

    def test_router_multi_tier_weaving(self) -> None:
        """Verify that conflicting subterranean routes weave across discrete tiers."""
        # Block the surface intersection so both nets must dive underground
        obstacles = {(3, 3), (4, 3), (5, 3), (4, 2), (4, 4)}

        net_horiz = RouterNetSpec(
            name="net_horiz",
            source=(1, 3),
            sink=(7, 3),
            commodity="iron-plate",
            start_heading=4,
            end_heading=4,
        )
        net_vert = RouterNetSpec(
            name="net_vert",
            source=(4, 0),
            sink=(4, 6),
            commodity="copper-plate",
            start_heading=8,
            end_heading=8,
        )

        router = DualLaneMazeRouter(
            min_x=0,
            max_x=9,
            min_y=0,
            max_y=8,
            engine=self.engine,
            obstacles=obstacles,
        )

        routes, entities, res = router.route(
            [net_horiz, net_vert],
            verify=True,
            max_iterations=30,
        )
        self.assertTrue(routes["net_horiz"].is_success)
        self.assertTrue(routes["net_vert"].is_success)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)
        self.assertEqual(len(res.lvs_errors), 0)
        self.assertEqual(res.delivered_items.get("net_horiz"), 1)
        self.assertEqual(res.delivered_items.get("net_vert"), 1)

        # Confirm the router selects distinct tiers for underground weaving
        names = {e.get("name") for e in entities}
        self.assertIn("underground-belt", names)
        self.assertIn("fast-underground-belt", names)

    def test_router_unroutable_enclosed_target(self) -> None:
        """Verify that an impossible route reports failure gracefully."""
        # Enclose target (2, 2) completely with obstacles
        obstacles = {(1, 2), (3, 2), (2, 1), (2, 3)}
        net = RouterNetSpec(
            name="net_blocked",
            source=(0, 0),
            sink=(2, 2),
            commodity="iron-plate",
            start_heading=4,
            end_heading=4,
        )
        router = DualLaneMazeRouter(
            min_x=0,
            max_x=5,
            min_y=0,
            max_y=5,
            engine=self.engine,
            obstacles=obstacles,
        )

        routes, entities, res = router.route([net], verify=False)
        self.assertFalse(routes["net_blocked"].is_success)
        self.assertIn("No route found", routes["net_blocked"].error_message or "")


if __name__ == "__main__":
    unittest.main()
