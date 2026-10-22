"""Unit and integration tests for DualLaneMazeRouter.

Tests straight runs, turns, obstacle avoidance, crossing dives, and weaving.
All docstrings follow Simplified Technical English (ASD-STE100).
"""

from __future__ import annotations

import json
import math
import os
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

from foundry.native_engine.engine import FactorioNativeEngine
from foundry.native_engine.symbol_finder import resolve_factorio_binary
from foundry.router import (
    DIRECTION_EAST,
    DualLaneMazeRouter,
    LANE_LEFT,
    LANE_RIGHT,
    RouteEdge,
    RouteNode,
    RouteResult,
    RouterNetSpec,
    build_blueprint_entities,
)
from foundry.router.grid import RoutingGrid
from foundry.router.transitions import get_legal_transitions
from foundry.router.types import LAYER_SURFACE


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
        # Block the surface intersection so both nets must dive underground across distinct spans
        obstacles = {(2, 3), (3, 3), (4, 3), (5, 3), (6, 3), (4, 2), (4, 4)}

        net_horiz = RouterNetSpec(
            name="net_horiz",
            source=(0, 3),
            sink=(8, 3),
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

    def test_router_scale_performance(self) -> None:
        """Benchmark router performance on 50 concurrent long-distance nets."""
        import time

        # Create row obstacles resembling machine blocks
        obstacles: set[tuple[int, int]] = set()
        for row in (20, 40, 60, 80):
            for col in range(10, 90):
                if col % 15 != 0:  # gaps for routing channels
                    obstacles.add((col, row))
                    obstacles.add((col, row + 1))
                    obstacles.add((col, row + 2))

        nets = []
        for i in range(25):
            # Horizontal nets spanning across gaps
            nets.append(
                RouterNetSpec(
                    name=f"net_h_{i}",
                    source=(5, 10 + i * 3),
                    sink=(95, 10 + i * 3),
                    commodity="iron-plate",
                    start_heading=4,
                    end_heading=4,
                )
            )
            # Vertical nets crossing horizontal obstacles
            nets.append(
                RouterNetSpec(
                    name=f"net_v_{i}",
                    source=(12 + i * 3, 5),
                    sink=(12 + i * 3, 95),
                    commodity="copper-plate",
                    start_heading=8,
                    end_heading=8,
                )
            )

        router = DualLaneMazeRouter(
            min_x=0,
            max_x=100,
            min_y=0,
            max_y=100,
            engine=self.engine,
            obstacles=obstacles,
        )

        t0 = time.perf_counter()
        routes, entities, _ = router.route(nets, verify=False, max_iterations=3)
        dt = time.perf_counter() - t0

        failed = [name for name, r in routes.items() if not r.is_success]
        print(f"\nBENCHMARK: 50 nets routed in {dt:.3f}s. Failed: {len(failed)}")
        self.assertEqual(len(failed), 0, f"Failed nets: {failed}")
        self.assertLess(dt, 5.0, f"Routing took {dt:.2f}s, expected < 5s")

    def test_logistic_science_factory(self) -> None:
        """Route the logistic science factory and verify DRC violations."""
        import json
        import math
        import os
        from pathlib import Path

        def_path = None
        netlist_path = None
        for base in [
            Path("."),
            Path("flow"),
            Path("bazel-bin/flow"),
            Path(os.environ.get("RUNFILES_DIR", "")) / "_main/flow",
            Path(os.environ.get("TEST_SRCDIR", "")) / "_main/flow",
        ]:
            c_def = (
                base / "logistic_science.def"
                if base.name != "flow"
                else base.parent / "flow/logistic_science.def"
            )
            c_net = (
                base / "logistic_science_netlist.json"
                if base.name != "flow"
                else base.parent / "flow/logistic_science_netlist.json"
            )
            if c_def.exists() and c_net.exists():
                def_path = c_def
                netlist_path = c_net
                break

        if not def_path or not netlist_path:
            self.skipTest(f"logistic_science build artifacts not present (searched {base})")

        from foundry.def2bp import parse_def, build_entities

        def_data = parse_def(def_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())

        try:
            entities, wires = build_entities(
                def_data, netlist_data, strict_drc=True, use_maze_router=True
            )
            print(f"build_entities succeeded! Total entities: {len(entities)}, wires: {len(wires)}")

            scale = 1.0 / float(def_data.get("units", 1000))
            drc_in = [
                (p["x"] * scale, p["y"] * scale)
                for p in def_data.get("pins", [])
                if p.get("direction") == "INPUT"
            ]
            drc_out = [
                (p["x"] * scale, p["y"] * scale)
                for p in def_data.get("pins", [])
                if p.get("direction") == "OUTPUT"
            ]
            from foundry.belt_drc import BeltDrcChecker

            drc = BeltDrcChecker()
            violations = drc.check_entities(
                entities, input_pins=drc_in, output_pins=drc_out, wires=wires
            )
            self.assertEqual(
                len(violations), 0, f"DRC violations remain: {[v.message for v in violations]}"
            )
            self.assertGreater(len(entities), 0)
        except Exception as e:
            import traceback

            raise

    @staticmethod
    def _find_science_artifacts(stem: str) -> tuple[Path | None, Path | None]:
        """Locate DEF and netlist JSON artifacts for a science factory."""
        for base in (
            Path("flow"),
            Path("bazel-bin/flow"),
            Path(__file__).parent.parent / "flow",
            Path(__file__).parent.parent / "bazel-bin/flow",
        ):
            flow_dir = base if base.name == "flow" else base.parent / "flow"
            c_def = flow_dir / f"{stem}.def"
            c_net = flow_dir / f"{stem}_netlist.json"
            if c_def.exists() and c_net.exists():
                return c_def, c_net
        return None, None

    def test_military_science_factory(self) -> None:
        """Route the military science factory and diagnose DRC violations."""
        def_path, netlist_path = self._find_science_artifacts("military_science")
        if not def_path or not netlist_path:
            self.skipTest("military_science build artifacts not present")

        from foundry.def2bp import build_entities, parse_def

        def_data = parse_def(def_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())

        entities, _wires = build_entities(
            def_data, netlist_data, strict_drc=False, use_maze_router=True, engine=self.engine
        )
        placement = def_data["_placement"]

        failed_nets = [n for n, r in placement.router_routes.items() if not r.is_success]
        self.assertEqual(len(failed_nets), 0, f"Failed nets in military_science: {failed_nets}")
        self.assertGreater(len(entities), 0)

    def test_automation_science_factory(self) -> None:
        """Route the automation science factory and diagnose machine starvation."""
        def_path = None
        netlist_path = None
        for base in [
            Path("flow"),
            Path("bazel-bin/flow"),
            Path(os.environ.get("RUNFILES_DIR", "")) / "_main/flow",
            Path(os.environ.get("TEST_SRCDIR", "")) / "_main/flow",
            Path(__file__).parent.parent / "flow",
            Path(__file__).parent.parent / "bazel-bin/flow",
        ]:
            c_def = (
                base / "automation_science.def"
                if base.name != "flow"
                else base.parent / "flow/automation_science.def"
            )
            c_net = (
                base / "automation_science_netlist.json"
                if base.name != "flow"
                else base.parent / "flow/automation_science_netlist.json"
            )
            if c_def.exists() and c_net.exists():
                def_path = c_def
                netlist_path = c_net
                break

        if not def_path or not netlist_path:
            self.skipTest("automation_science build artifacts not present")

        from foundry.def2bp import parse_def, build_entities

        def_data = parse_def(def_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())

        entities, wires = build_entities(
            def_data, netlist_data, strict_drc=True, use_maze_router=True, engine=self.engine
        )
        placement = def_data["_placement"]
        failed = {rn: r for rn, r in placement.router_routes.items() if not r.is_success}
        self.assertEqual(len(failed), 0, f"Router failed for nets: {list(failed.keys())}")
        self.assertTrue(placement.router_verif_res.is_valid)
        self.assertGreater(len(entities), 0)

    def test_chemical_science_factory(self) -> None:
        """Route the chemical science factory and diagnose any routing failures."""
        def_path = None
        netlist_path = None
        for base in [
            Path("flow"),
            Path("bazel-bin/flow"),
            Path(os.environ.get("RUNFILES_DIR", "")) / "_main/flow",
            Path(os.environ.get("TEST_SRCDIR", "")) / "_main/flow",
            Path(__file__).parent.parent / "flow",
            Path(__file__).parent.parent / "bazel-bin/flow",
        ]:
            c_def = (
                base / "chemical_science.def"
                if base.name != "flow"
                else base.parent / "flow/chemical_science.def"
            )
            c_net = (
                base / "chemical_science_netlist.json"
                if base.name != "flow"
                else base.parent / "flow/chemical_science_netlist.json"
            )
            if c_def.exists() and c_net.exists():
                def_path = c_def
                netlist_path = c_net
                break

        if not def_path or not netlist_path:
            self.skipTest("chemical_science build artifacts not present")

        from foundry.def2bp import parse_def, build_entities

        def_data = parse_def(def_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())

        entities, wires = build_entities(
            def_data, netlist_data, strict_drc=True, use_maze_router=True, engine=self.engine
        )
        placement = def_data["_placement"]
        failed = {rn: r for rn, r in placement.router_routes.items() if not r.is_success}
        self.assertEqual(len(failed), 0, f"Router failed for nets: {list(failed.keys())}")
        self.assertTrue(placement.router_verif_res.is_valid)

    def test_production_science_factory(self) -> None:
        """Route the production science factory and diagnose routing performance."""
        import time

        def_path = None
        netlist_path = None
        for base in [
            Path("flow"),
            Path("bazel-bin/flow"),
            Path(os.environ.get("RUNFILES_DIR", "")) / "_main/flow",
            Path(os.environ.get("TEST_SRCDIR", "")) / "_main/flow",
            Path(__file__).parent.parent / "flow",
            Path(__file__).parent.parent / "bazel-bin/flow",
        ]:
            c_def = (
                base / "production_science.def"
                if base.name != "flow"
                else base.parent / "flow/production_science.def"
            )
            c_net = (
                base / "production_science_netlist.json"
                if base.name != "flow"
                else base.parent / "flow/production_science_netlist.json"
            )
            if c_def.exists() and c_net.exists():
                def_path = c_def
                netlist_path = c_net
                break

        if not def_path or not netlist_path:
            self.skipTest("production_science build artifacts not present")

        from foundry.def2bp import parse_def, build_entities

        t0 = time.perf_counter()
        def_data = parse_def(def_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())
        sys.stderr.write(f"[perf] parse_def took {time.perf_counter() - t0:.2f}s\n")

        entities, wires = build_entities(
            def_data, netlist_data, strict_drc=True, use_maze_router=True, engine=self.engine
        )
        placement = def_data["_placement"]
        failed = {rn: r for rn, r in placement.router_routes.items() if not r.is_success}
        for rn, r in list(failed.items())[:25]:
            sys.stderr.write(f"  FAILED: {rn}: {r.error_message}\n")

        self.assertEqual(
            len(failed), 0, f"Router failed for {len(failed)} nets: {list(failed.keys())[:20]}"
        )
        self.assertTrue(placement.router_verif_res.is_valid)

    def test_science_chassis_factory(self) -> None:
        """Route the 7-science chassis factory and verify routing performance and DRC."""
        import time

        def_path = None
        netlist_path = None
        for base in [
            Path("flow"),
            Path("bazel-bin/flow"),
            Path(os.environ.get("RUNFILES_DIR", "")) / "_main/flow",
            Path(os.environ.get("TEST_SRCDIR", "")) / "_main/flow",
            Path(__file__).parent.parent / "flow",
            Path(__file__).parent.parent / "bazel-bin/flow",
        ]:
            c_def = (
                base / "science_chassis.def"
                if base.name != "flow"
                else base.parent / "flow/science_chassis.def"
            )
            c_net = (
                base / "science_chassis_netlist.json"
                if base.name != "flow"
                else base.parent / "flow/science_chassis_netlist.json"
            )
            if c_def.exists() and c_net.exists():
                def_path = c_def
                netlist_path = c_net
                break

        if not def_path or not netlist_path:
            self.skipTest("science_chassis build artifacts not present")

        from foundry.def2bp import build_entities, parse_def

        t0 = time.perf_counter()
        def_data = parse_def(def_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())
        sys.stderr.write(f"[perf] parse_def took {time.perf_counter() - t0:.2f}s\n")
        sys.stderr.flush()

        entities, _wires = build_entities(
            def_data, netlist_data, strict_drc=True, use_maze_router=True, engine=self.engine
        )
        placement = def_data["_placement"]
        failed = {rn: r for rn, r in placement.router_routes.items() if not r.is_success}
        for rn, r in list(failed.items())[:25]:
            sys.stderr.write(f"  FAILED: {rn}: {r.error_message}\n")
        sys.stderr.flush()

        self.assertEqual(
            len(failed), 0, f"Router failed for {len(failed)} nets: {list(failed.keys())[:20]}"
        )
        self.assertTrue(placement.router_verif_res.is_valid)
        self.assertGreater(len(entities), 0)

    def test_perf_cases_smoke(self) -> None:
        """Smoke-test router component perf_* generator benchmarks."""
        for _ in perf_build_blueprint_entities(16):
            pass
        for _ in perf_get_legal_transitions(16):
            pass


class TestRouterComponents(unittest.TestCase):
    """Hermetic unit tests for build_blueprint_entities and get_legal_transitions."""

    def test_build_blueprint_entities_and_transitions(self) -> None:
        """Verify blueprint entity synthesis and legal transition generation."""
        grid = RoutingGrid(min_x=0, max_x=12, min_y=0, max_y=12, obstacles={(3, 2)})
        belt_z = grid.belt_tier_keys[0]
        net = RouterNetSpec(
            name="net_belt",
            source=(1, 2),
            sink=(5, 2),
            commodity="iron-plate",
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        grid.register_terminals([net])
        surf_node = RouteNode(2, 2, LAYER_SURFACE, DIRECTION_EAST)
        moves = get_legal_transitions(surf_node, grid, net)
        self.assertTrue(any(m[2] == "dive" for m in moves))

        dive_node = RouteNode(2, 2, belt_z, DIRECTION_EAST)
        leap_moves = get_legal_transitions(dive_node, grid, net)
        self.assertTrue(any(m[2] == "leap" for m in leap_moves))

        n0 = RouteNode(1, 2, LAYER_SURFACE, DIRECTION_EAST)
        n1 = RouteNode(2, 2, LAYER_SURFACE, DIRECTION_EAST)
        n2 = RouteNode(2, 2, belt_z, DIRECTION_EAST)
        n3 = RouteNode(4, 2, LAYER_SURFACE, DIRECTION_EAST)
        n4 = RouteNode(5, 2, LAYER_SURFACE, DIRECTION_EAST)
        edges = [
            RouteEdge(n0, n1, "straight", "transport-belt", 1.0),
            RouteEdge(n1, n2, "dive", "underground-belt", 4.0),
            RouteEdge(n2, n3, "leap", "underground-belt", 6.0),
            RouteEdge(n3, n4, "straight", "transport-belt", 1.0),
        ]
        routes = {"net_belt": RouteResult("net_belt", [n0, n1, n2, n3, n4], edges, 12.0)}
        entities = build_blueprint_entities(routes, [net])
        self.assertEqual(len(entities), 4)
        self.assertEqual([e["entity_number"] for e in entities], [1, 2, 3, 4])


def perf_build_blueprint_entities(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (32, 64, 128, 256),
    _max_complexity: str = "O(N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for build_blueprint_entities across multi-segment routes."""
    routes: dict[str, RouteResult] = {}
    specs: list[RouterNetSpec] = []
    for i in range(n):
        name = f"net_{i}"
        y = i * 2
        n0 = RouteNode(0, y, LAYER_SURFACE, DIRECTION_EAST)
        n1 = RouteNode(1, y, LAYER_SURFACE, DIRECTION_EAST)
        n2 = RouteNode(1, y, 1, DIRECTION_EAST)
        n3 = RouteNode(4, y, LAYER_SURFACE, DIRECTION_EAST)
        edges = [
            RouteEdge(n0, n1, "straight", "transport-belt", 1.0),
            RouteEdge(n1, n2, "dive", "underground-belt", 4.0),
            RouteEdge(n2, n3, "leap", "underground-belt", 6.0),
        ]
        routes[name] = RouteResult(name, [n0, n1, n2, n3], edges, 11.0)
        specs.append(RouterNetSpec(name=name, source=(0, y), sink=(4, y)))
    yield
    build_blueprint_entities(routes, specs)


def perf_get_legal_transitions(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (32, 64, 128, 256),
    _max_complexity: str = "O(N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for get_legal_transitions across surface and subterranean nodes."""
    obstacles = {(i * 3 + 2, 4) for i in range(n)}
    grid = RoutingGrid(min_x=0, max_x=n * 3 + 10, min_y=0, max_y=10, obstacles=obstacles)
    belt_z = grid.belt_tier_keys[0]
    net = RouterNetSpec("net_0", (0, 4), (n * 3 + 5, 4))
    nodes = [
        RouteNode(i * 3 + 1, 4, LAYER_SURFACE if i % 2 == 0 else belt_z, DIRECTION_EAST)
        for i in range(n)
    ]
    yield
    for node in nodes:
        get_legal_transitions(node, grid, net)


def perf_maze_router_route(
    n: int = 16,
    *,
    _scales: tuple[int, ...] = (8, 16, 32, 64),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for DualLaneMazeRouter.route orchestration without native simulation."""
    base_grid = RoutingGrid(min_x=0, max_x=10, min_y=0, max_y=10)
    router = DualLaneMazeRouter(
        min_x=0,
        max_x=20,
        min_y=0,
        max_y=n * 2 + 10,
        prototypes=base_grid.prototypes,
    )
    nets = [
        RouterNetSpec(
            name=f"net_{i}",
            source=(2, i * 2 + 2),
            sink=(6, i * 2 + 2),
            commodity="iron-plate",
            start_heading=DIRECTION_EAST,
            end_heading=DIRECTION_EAST,
        )
        for i in range(n)
    ]
    yield
    router.route(nets, max_iterations=2, verify=False)


if __name__ == "__main__":
    filt = os.environ.get("TESTBRIDGE_TEST_ONLY")
    if filt:
        unittest.main(defaultTest=f"TestDualLaneMazeRouter.{filt.strip('*')}")
    else:
        unittest.main()
