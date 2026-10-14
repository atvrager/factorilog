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
from pathlib import Path

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
            c_def = base / "logistic_science.def" if base.name != "flow" else base.parent / "flow/logistic_science.def"
            c_net = base / "logistic_science_netlist.json" if base.name != "flow" else base.parent / "flow/logistic_science_netlist.json"
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
            entities, wires = build_entities(def_data, netlist_data, strict_drc=True, use_maze_router=True)
            print(f"build_entities succeeded! Total entities: {len(entities)}, wires: {len(wires)}")

            scale = 1.0 / float(def_data.get("units", 1000))
            drc_in = [(p["x"] * scale, p["y"] * scale) for p in def_data.get("pins", []) if p.get("direction") == "INPUT"]
            drc_out = [(p["x"] * scale, p["y"] * scale) for p in def_data.get("pins", []) if p.get("direction") == "OUTPUT"]
            from foundry.belt_drc import BeltDrcChecker
            drc = BeltDrcChecker()
            violations = drc.check_entities(entities, input_pins=drc_in, output_pins=drc_out, wires=wires)
            self.assertEqual(len(violations), 0, f"DRC violations remain: {[v.message for v in violations]}")
            self.assertGreater(len(entities), 0)
        except Exception as e:
            import traceback
            raise

    def test_military_science_factory(self) -> None:
        """Route the military science factory and diagnose DRC violations."""
        def_path = None
        netlist_path = None
        for base in [
            Path("flow"),
            Path("bazel-bin/flow"),
            Path(__file__).parent.parent / "flow",
            Path(__file__).parent.parent / "bazel-bin/flow",
        ]:
            c_def = base / "military_science.def" if base.name != "flow" else base.parent / "flow/military_science.def"
            c_net = base / "military_science_netlist.json" if base.name != "flow" else base.parent / "flow/military_science_netlist.json"
            if c_def.exists() and c_net.exists():
                def_path = c_def
                netlist_path = c_net
                break

        if not def_path or not netlist_path:
            self.skipTest(f"military_science build artifacts not present")

        from foundry.def2bp import parse_def, build_entities, place_components, route_with_maze_router
        def_data = parse_def(def_path.read_text())
        netlist_data = json.loads(netlist_path.read_text())

        entities, wires = build_entities(
            def_data, netlist_data, strict_drc=False, use_maze_router=True, engine=self.engine
        )
        placement = def_data["_placement"]

        failed_nets = [n for n, r in placement.router_routes.items() if not r.is_success]
        self.assertEqual(len(failed_nets), 0, f"Failed nets in military_science: {failed_nets}")

        scale = 1.0 / float(def_data.get("units", 1000))
        sys.stderr.write("\nENTITIES AROUND (170.5, 197.5):\n")
        for e in entities:
            pos = e.get("position", {})
            ex, ey = float(pos.get("x", 0)), float(pos.get("y", 0))
            if 168 <= ex <= 173 and 195 <= ey <= 200:
                sys.stderr.write(f"  {e.get('name')} at ({ex}, {ey}) dir={e.get('direction')} type={e.get('type')}\n")
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
            c_def = base / "automation_science.def" if base.name != "flow" else base.parent / "flow/automation_science.def"
            c_net = base / "automation_science_netlist.json" if base.name != "flow" else base.parent / "flow/automation_science_netlist.json"
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
            def_data, netlist_data, strict_drc=False, use_maze_router=True, engine=self.engine
        )
        sys.stderr.write(f"\nBUILD_ENTITIES RETURNED {len(entities)} entities, {len(wires)} wires\n")
        placement = def_data["_placement"]
        ent_by_pos = {(round(e["position"]["x"], 1), round(e["position"]["y"], 1)): e for e in entities}
        for target_net in ["copper_plate__u_automation_science_pack_2", "iron_gear_wheel__u_automation_science_pack_2"]:
            for rn, r in placement.router_routes.items():
                if target_net in rn:
                    sys.stderr.write(f"\n=== NET {rn} ===\n")
                    sys.stderr.write(f"Success: {r.is_success}, Error: {r.error_message}\n")
                    sys.stderr.write(f"Nodes ({len(r.nodes)}):\n")
                    for node in r.nodes:
                        sys.stderr.write(f"  ({node.x}, {node.y}, z={node.z}, head={node.heading})\n")
                    sys.stderr.write(f"Entities along route:\n")
                    for node in r.nodes:
                        epos = (node.x + 0.5, node.y + 0.5)
                        ent = next((e for e in entities if abs(e['position']['x'] - epos[0]) < 0.1 and abs(e['position']['y'] - epos[1]) < 0.1), None)
                        sys.stderr.write(f"  tile ({node.x}, {node.y}): {ent.get('name') if ent else 'MISSING'} dir={ent.get('direction') if ent else ''} type={ent.get('type') if ent else ''}\n")

        sys.stderr.write(f"\nENTITIES AROUND (153.5, 71.5):\n")
        for e in entities:
            epos = e.get("position", {})
            ex, ey = float(epos.get("x", 0)), float(epos.get("y", 0))
            if 150.0 <= ex <= 165.0 and 70.0 <= ey <= 73.0:
                sys.stderr.write(f"  {e.get('name')} at ({ex}, {ey}) dir={e.get('direction')} type={e.get('type')}\n")
        # Find all inserters
        for e in entities:
            if e.get("name") == "fast-inserter":
                ex, ey = e["position"]["x"], e["position"]["y"]
                # Inserter picking up near (145.5, 68.5)
                if abs(ex - 146.5) < 0.5 and abs(ey - 68.5) < 0.5:
                    sys.stderr.write(f"\nFOUND ASM INSERTER: {e}\n")
                    cur_tx, cur_ty = ex - 1.0, ey
                    for step in range(1, 100):
                        cur_b = ent_by_pos.get((round(cur_tx, 1), round(cur_ty, 1)))
                        if not cur_b:
                            # Check if a splitter or something covers this tile
                            cand = next((cand_e for cand_e in entities if "splitter" in cand_e.get("name", "") and abs(cand_e["position"]["x"] - cur_tx) <= 0.6 and abs(cand_e["position"]["y"] - cur_ty) <= 1.1), None)
                            if cand:
                                sys.stderr.write(f"  STEP {step}: Reached splitter {cand['name']} at ({cand['position']['x']}, {cand['position']['y']})!\n")
                                break
                            sys.stderr.write(f"  STEP {step}: UPSTREAM_GAP at ({cur_tx}, {cur_ty})! No entity!\n")
                            break
                        bdir = cur_b.get("direction", 0)
                        btype = cur_b.get("type")
                        bname = cur_b.get("name")
                        sys.stderr.write(f"  STEP {step}: ({cur_tx}, {cur_ty}) {bname} dir={bdir} type={btype}\n")
                        dx, dy = 0, 0
                        if bdir == 0: dy = 1
                        elif bdir == 4: dx = -1
                        elif bdir == 8: dy = -1
                        elif bdir == 12: dx = 1

                        if "underground-belt" in bname and btype == "output":
                            found_in = False
                            for ustep in range(1, 12):
                                unx = cur_tx + dx * ustep
                                uny = cur_ty + dy * ustep
                                ue = ent_by_pos.get((round(unx, 1), round(uny, 1)))
                                if ue and "underground-belt" in ue.get("name", "") and ue.get("type") == "input" and ue.get("direction") == bdir:
                                    cur_tx = unx
                                    cur_ty = uny
                                    found_in = True
                                    break
                            if not found_in:
                                sys.stderr.write(f"  STEP {step}: UPSTREAM_UG_UNPAIRED at ({cur_tx}, {cur_ty})\n")
                                break
                        elif "splitter" in bname:
                            sys.stderr.write(f"  STEP {step}: Reached splitter at ({cur_tx}, {cur_ty})!\n")
                            break
                        else:
                            nxt = (round(cur_tx + dx, 1), round(cur_ty + dy, 1))
                            if nxt in ent_by_pos:
                                cur_tx, cur_ty = nxt
                            else:
                                found_turn = False
                                for side_dx, side_dy, needed_dir in [
                                    (-1, 0, 4),   # West belt pointing East
                                    (1, 0, 12),   # East belt pointing West
                                    (0, -1, 8),   # North belt pointing South
                                    (0, 1, 0),    # South belt pointing North
                                ]:
                                    cand_pos = (round(cur_tx + side_dx, 1), round(cur_ty + side_dy, 1))
                                    cand_ent = ent_by_pos.get(cand_pos)
                                    if cand_ent and cand_ent.get("direction") == needed_dir:
                                        cur_tx, cur_ty = cand_pos
                                        found_turn = True
                                        break
                                if not found_turn:
                                    cur_tx, cur_ty = nxt

        # Print all inserters around u_iron_plate_4 (84.5, 37.5) and u_automation_science_pack_2 (161.5, 68.5)
        sys.stderr.write(f"\nINSERTERS AROUND u_iron_plate_4:\n")
        for e in entities:
            if "inserter" in e.get("name", ""):
                ex, ey = e["position"]["x"], e["position"]["y"]
                if abs(ex - 84.5) < 5.0 and abs(ey - 37.5) < 5.0:
                    sys.stderr.write(f"  {e.get('name')} at ({ex}, {ey}) dir={e.get('direction')}\n")

        sys.stderr.write(f"\nINSERTERS AROUND u_automation_science_pack_2 (161.5, 68.5):\n")
        for e in entities:
            if "inserter" in e.get("name", ""):
                ex, ey = e["position"]["x"], e["position"]["y"]
                if abs(ex - 161.5) < 5.0 and abs(ey - 68.5) < 5.0:
                    sys.stderr.write(f"  {e.get('name')} at ({ex}, {ey}) dir={e.get('direction')}\n")
        for step in range(1, 100):
            cur_b = ent_by_pos.get((round(cur_tx, 1), round(cur_ty, 1)))
            if not cur_b:
                cand = next((cand_e for cand_e in entities if "splitter" in cand_e.get("name", "") and abs(cand_e["position"]["x"] - cur_tx) <= 0.6 and abs(cand_e["position"]["y"] - cur_ty) <= 1.1), None)
                if cand:
                    sys.stderr.write(f"  STEP {step}: Reached splitter {cand['name']} at ({cand['position']['x']}, {cand['position']['y']})!\n")
                    break
                sys.stderr.write(f"  STEP {step}: UPSTREAM_GAP at ({cur_tx}, {cur_ty})! No entity!\n")
                break
            bdir = cur_b.get("direction", 0)
            btype = cur_b.get("type")
            bname = cur_b.get("name")
            sys.stderr.write(f"  STEP {step}: ({cur_tx}, {cur_ty}) {bname} dir={bdir} type={btype}\n")
            dx, dy = 0, 0
            if bdir == 0: dy = 1
            elif bdir == 4: dx = -1
            elif bdir == 8: dy = -1
            elif bdir == 12: dx = 1

            if "underground-belt" in bname and btype == "output":
                found_in = False
                for ustep in range(1, 12):
                    unx = cur_tx + dx * ustep
                    uny = cur_ty + dy * ustep
                    ue = ent_by_pos.get((round(unx, 1), round(uny, 1)))
                    if ue and "underground-belt" in ue.get("name", "") and ue.get("type") == "input" and ue.get("direction") == bdir:
                        cur_tx = unx
                        cur_ty = uny
                        found_in = True
                        break
                if not found_in:
                    sys.stderr.write(f"  STEP {step}: UPSTREAM_UG_UNPAIRED at ({cur_tx}, {cur_ty})\n")
                    break
            elif "splitter" in bname:
                sys.stderr.write(f"  STEP {step}: Reached splitter at ({cur_tx}, {cur_ty})!\n")
                break
            else:
                nxt = (round(cur_tx + dx, 1), round(cur_ty + dy, 1))
                if nxt in ent_by_pos:
                    cur_tx, cur_ty = nxt
                else:
                    found_turn = False
                    for side_dx, side_dy, needed_dir in [
                        (-1, 0, 4),   # West belt pointing East
                        (1, 0, 12),   # East belt pointing West
                        (0, -1, 8),   # North belt pointing South
                        (0, 1, 0),    # South belt pointing North
                    ]:
                        cand_pos = (round(cur_tx + side_dx, 1), round(cur_ty + side_dy, 1))
                        cand_ent = ent_by_pos.get(cand_pos)
                        if cand_ent and cand_ent.get("direction") == needed_dir:
                            cur_tx, cur_ty = cand_pos
                            found_turn = True
                            break
                    if not found_turn:
                        cur_tx, cur_ty = nxt

        # Decode the actual built blueprint file
        bp_path = Path("bazel-bin/flow/automation_science.blueprint")
        if bp_path.is_file():
            from foundry.blueprint import decode
            doc = decode(bp_path.read_text().strip())
            bp_ents = doc.get("blueprint", {}).get("entities", [])
            sys.stderr.write(f"\n=== BLUEPRINT ENTITIES TOTAL: {len(bp_ents)} ===\n")
            # Find entities around world (-17.5, 14.5)
            # In the blueprint, coordinates are already centered!
            for be in bp_ents:
                bpos = be.get("position", {})
                bx, by = float(bpos.get("x", 0)), float(bpos.get("y", 0))
                if abs(bx - -17.5) < 3.0 and abs(by - 14.5) < 3.0:
                    sys.stderr.write(f"  BP ENT: {be.get('name')} at ({bx}, {by}) dir={be.get('direction')} type={be.get('type')}\n")

                if abs(bx - -47.5) < 3.0 and abs(by - -13.5) < 3.0:
                    sys.stderr.write(f"  FURNACE ENT: {be.get('name')} at ({bx}, {by}) dir={be.get('direction')} type={be.get('type')}\n")


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
            c_def = base / "chemical_science.def" if base.name != "flow" else base.parent / "flow/chemical_science.def"
            c_net = base / "chemical_science_netlist.json" if base.name != "flow" else base.parent / "flow/chemical_science_netlist.json"
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
            c_def = base / "production_science.def" if base.name != "flow" else base.parent / "flow/production_science.def"
            c_net = base / "production_science_netlist.json" if base.name != "flow" else base.parent / "flow/production_science_netlist.json"
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

        self.assertEqual(len(failed), 0, f"Router failed for {len(failed)} nets: {list(failed.keys())[:20]}")
        self.assertTrue(placement.router_verif_res.is_valid)


if __name__ == "__main__":
    filt = os.environ.get("TESTBRIDGE_TEST_ONLY")
    if filt:
        unittest.main(defaultTest=f"TestDualLaneMazeRouter.{filt.strip('*')}")
    else:
        unittest.main()



