"""Unit tests for rules-based belt library and physical DRC engine."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from foundry.belt_drc import BeltDrcChecker
from foundry.belt_library import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    PatternType,
    get_dual_merge_pattern,
    get_sideload_pattern,
    get_straight_pattern,
    get_turn_pattern,
    get_underground_pair_pattern,
)
from foundry import lef, pdk, solve
from foundry.def2bp import def_to_blueprint

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestBeltRules(unittest.TestCase):
    """Test suite for physical belt pattern library and DRC checker."""

    def test_pattern_library_definitions(self) -> None:
        """Verify canonical pattern attributes and compatibility."""
        straight = get_straight_pattern(DIRECTION_EAST)
        self.assertEqual(straight.pattern_type, PatternType.STRAIGHT)
        self.assertEqual(straight.direction, DIRECTION_EAST)
        self.assertTrue(straight.is_compatible([DIRECTION_EAST], DIRECTION_EAST))
        self.assertFalse(straight.is_compatible([DIRECTION_NORTH], DIRECTION_EAST))

        turn = get_turn_pattern(DIRECTION_NORTH, DIRECTION_EAST)
        self.assertEqual(turn.pattern_type, PatternType.TURN)
        self.assertEqual(turn.direction, DIRECTION_EAST)
        self.assertTrue(turn.is_compatible([DIRECTION_NORTH], DIRECTION_EAST))

        sideload = get_sideload_pattern(DIRECTION_EAST, DIRECTION_SOUTH)
        self.assertEqual(sideload.pattern_type, PatternType.SIDELOAD)
        self.assertEqual(sideload.lane_mapping[2], 1)

        dual_merge = get_dual_merge_pattern(DIRECTION_SOUTH, DIRECTION_NORTH, DIRECTION_EAST)
        self.assertEqual(dual_merge.pattern_type, PatternType.DUAL_MERGE)

        ug_pair = get_underground_pair_pattern(DIRECTION_EAST, 4)
        self.assertEqual(ug_pair.pattern_type, PatternType.UNDERGROUND_PAIR)

    def test_drc_clean_layout(self) -> None:
        """Clean layouts produce zero DRC violations."""
        entities = [
            {"name": "transport-belt", "direction": DIRECTION_EAST, "position": {"x": 0, "y": 0}},
            {"name": "transport-belt", "direction": DIRECTION_EAST, "position": {"x": 1, "y": 0}},
            {"name": "transport-belt", "direction": DIRECTION_EAST, "position": {"x": 2, "y": 0}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertEqual(len(violations), 0)

    def test_drc_head_to_head_collision(self) -> None:
        """Rule DRC-01 flags opposing belts pointing at each other."""
        entities = [
            {"name": "transport-belt", "direction": DIRECTION_EAST, "position": {"x": 0, "y": 0}},
            {"name": "transport-belt", "direction": DIRECTION_WEST, "position": {"x": 1, "y": 0}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertGreaterEqual(len(violations), 1)
        self.assertTrue(any(v.code == "DRC-01" for v in violations))

    def test_drc_underground_unpaired_input(self) -> None:
        """Rule DRC-02 flags underground inputs with no matching output."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            }
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-02" for v in violations))

    def test_drc_underground_span_exceeded(self) -> None:
        """Rule DRC-02 flags underground pairs exceeding maximum span."""
        # Yellow belt max span is 5 tiles apart
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 10, "y": 0},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-02" for v in violations))

    def test_drc_underground_valid_pairing(self) -> None:
        """Valid underground input and output pairs pass DRC."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 4, "y": 0},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertEqual(len(violations), 0)

    def test_drc_rules_database_configuration(self) -> None:
        """BeltDrcChecker configures span limits from rule database."""
        db = {
            "belt_tiers": {
                "yellow": {
                    "underground": "underground-belt",
                    "max_underground_span_tiles": 2,
                }
            }
        }
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 4, "y": 0},
            },
        ]
        standard_checker = BeltDrcChecker()
        self.assertEqual(len(standard_checker.check_entities(entities)), 0)

        custom_checker = BeltDrcChecker(rules_database=db)
        violations = custom_checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-02" for v in violations))

    def test_drc_hood_side_feed_clearance(self) -> None:
        """Rule DRC-03 detects perpendicular feeds into underground hood sides."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 1, "y": 1},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_SOUTH,
                "position": {"x": 1, "y": 0},
            },
        ]
        checker = BeltDrcChecker(allow_peeling=False)
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-03" for v in violations))

        # Check that allow_peeling suppresses Rule DRC-03
        peeling_checker = BeltDrcChecker(allow_peeling=True)
        peeling_violations = peeling_checker.check_entities(entities)
        self.assertFalse(any(v.code == "DRC-03" for v in peeling_violations))

    def test_drc_splitter_flank_feed(self) -> None:
        """Rule DRC-04 detects perpendicular feeds into splitter flanks."""
        entities = [
            {
                "name": "splitter",
                "direction": DIRECTION_EAST,
                "position": {"x": 2, "y": 2},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_SOUTH,
                "position": {"x": 2, "y": 1},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-04" for v in violations))

    def test_def2bp_strict_drc_abort(self) -> None:
        """def_to_blueprint aborts when strict_drc is enabled and violations exist."""
        # Synthetic minimal DEF with opposing belts on two nets
        bad_def = """
VERSION 5.8 ;
DESIGN bad_factory ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 10000 10000 ) ;
COMPONENTS 0 ;
END COMPONENTS
NETS 2 ;
- net1 + USE SIGNAL
  + ROUTED M1 ( 1500 1500 ) ( 2500 * ) ;
- net2 + USE SIGNAL
  + ROUTED M1 ( 3500 1500 ) ( 2500 * ) ;
END NETS
END DESIGN
"""
        netlist_data = {"cells": {}, "instances": [], "fluids": []}

        # With strict_drc=True, it must raise ValueError
        with self.assertRaises(ValueError):
            def_to_blueprint(bad_def, netlist_data, label="bad_test", strict_drc=True)

        # With strict_drc=False, it produces a blueprint string
        bp_str = def_to_blueprint(bad_def, netlist_data, label="bad_test", strict_drc=False)
        self.assertTrue(bp_str.startswith("0"))

    def test_belt_rules_json_database(self) -> None:
        """Verify the validity of the generated belt_rules.json database."""
        db_path = REPO_ROOT / "test" / "data" / "belt_rules.json"
        self.assertTrue(db_path.is_file(), "belt_rules.json must exist")
        data = json.loads(db_path.read_text())
        self.assertEqual(data.get("metadata", {}).get("version"), "1.2.0")
        self.assertIn("derived_rules", data)
        self.assertGreater(len(data["derived_rules"]), 4)

    def test_dynamic_underground_tier_discovery(self) -> None:
        """Verify that Pdk discovers base underground belt tiers and generates LEF layers."""
        fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
        data = solve.Data(str(fixture_path))
        p = pdk.Pdk(data)
        spans = p.underground_tier_spans()
        self.assertIn("underground-belt", spans)
        self.assertIn("fast-underground-belt", spans)
        self.assertIn("express-underground-belt", spans)
        self.assertNotIn("turbo-underground-belt", spans)
        self.assertEqual(spans["underground-belt"], 5)
        self.assertEqual(spans["fast-underground-belt"], 7)
        self.assertEqual(spans["express-underground-belt"], 9)

        stack = p.layer_stack()
        self.assertIn("M3", stack)
        self.assertIn("M7", stack)
        self.assertIn("M9", stack)
        self.assertNotIn("M11", stack)
        self.assertEqual(stack["M9"]["entity"], "express-underground-belt")

        lef_lines = lef.tech_lines(p.tech_json())
        lef_text = "\n".join(lef_lines)
        self.assertIn("LAYER M7", lef_text)
        self.assertIn("LAYER M9", lef_text)
        self.assertNotIn("LAYER M11", lef_text)
        self.assertIn("VIA EXPRESS_TRANSPORT_BELT_VIA", lef_text)
        self.assertNotIn("VIA TURBO_TRANSPORT_BELT_VIA", lef_text)

    def test_dynamic_drc_express_underground_span(self) -> None:
        """Verify that Rule DRC-02 accepts an 8-tile span for express belt and flags yellow belt."""
        express_entities = [
            {
                "name": "express-underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "express-underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 8, "y": 0},
            },
        ]
        checker = BeltDrcChecker()
        self.assertEqual(len(checker.check_entities(express_entities)), 0)

        yellow_entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 8, "y": 0},
            },
        ]
        violations = checker.check_entities(yellow_entities)
        self.assertTrue(any(v.code == "DRC-02" for v in violations))

    def test_drc_machine_input_connected(self) -> None:
        """Rule DRC-05 accepts machine input inserter with supply belt at pickup."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 11.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 11.5, "y": 11.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_NORTH,
                "position": {"x": 10.5, "y": 11.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertEqual(len(violations), 0)

    def test_drc_machine_input_missing_supply(self) -> None:
        """Rule DRC-05 flags machine input inserter with no supply at pickup."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 11.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 11.5, "y": 11.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-05" for v in violations))

    def test_drc_machine_output_connected(self) -> None:
        """Rule DRC-06 accepts machine output inserter with receiver belt at drop."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 21.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 15.5, "y": 21.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_NORTH,
                "position": {"x": 16.5, "y": 21.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertEqual(len(violations), 0)

    def test_drc_machine_output_missing_receiver(self) -> None:
        """Rule DRC-06 flags machine output inserter with no receiver at drop."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 21.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 15.5, "y": 21.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-06" for v in violations))

    def test_drc_machine_output_dead_end_flags_drc06(self) -> None:
        """Rule DRC-06 flags machine output belt terminating at dead end without reaching sink."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 21.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 15.5, "y": 21.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 16.5, "y": 21.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, output_pins=[(30.0, 30.0)])
        self.assertTrue(
            any(v.code == "DRC-06" and "not reachable" in v.message for v in violations)
        )

    def test_drc_machine_output_reachable_sink(self) -> None:
        """Rule DRC-06 passes when machine output reaches a downstream sink."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 21.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 15.5, "y": 21.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 16.5, "y": 21.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 17.5, "y": 21.5},
            },
            {
                "name": "iron-chest",
                "position": {"x": 18.5, "y": 21.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertEqual(len([v for v in violations if v.code == "DRC-06"]), 0)

    def test_drc_underground_span_hood_collision(self) -> None:
        """Rule DRC-07 flags underground belt hood placed inside an active underground span."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 4, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_SOUTH,
                "type": "input",
                "position": {"x": 2, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_SOUTH,
                "type": "output",
                "position": {"x": 2, "y": 3},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-07" for v in violations))

    def test_drc_underground_parallel_span_overlap(self) -> None:
        """Rule DRC-07 flags overlapping parallel underground spans."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 4, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_WEST,
                "type": "input",
                "position": {"x": 5, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_WEST,
                "type": "output",
                "position": {"x": 1, "y": 0},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-07" for v in violations))

    def test_drc_underground_perpendicular_crossing_allowed(self) -> None:
        """Rule DRC-07 allows perpendicular underground spans to cross underground."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 2},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 4, "y": 2},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_SOUTH,
                "type": "input",
                "position": {"x": 2, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_SOUTH,
                "type": "output",
                "position": {"x": 2, "y": 4},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertEqual(len(violations), 0)

    def test_drc_hood_rear_feed_collision(self) -> None:
        """Rule DRC-03 flags surface belt feeding into rear of underground output hood."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "input",
                "position": {"x": 0, "y": 0},
            },
            {
                "name": "underground-belt",
                "direction": DIRECTION_EAST,
                "type": "output",
                "position": {"x": 3, "y": 0},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 2, "y": 0},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-03" for v in violations))

    def test_drc_machine_input_unreachable_supply(self) -> None:
        """Rule DRC-05 flags machine input inserter disconnected from input pins."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 11.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 11.5, "y": 11.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_NORTH,
                "position": {"x": 10.5, "y": 11.5},
            },
        ]
        # input pin is far away at (0, 0), disconnected from the pickup belt at (10, 11)
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, input_pins=[(0.0, 0.0)])
        self.assertTrue(
            any(v.code == "DRC-05" and "not reachable" in v.message for v in violations)
        )

    def test_drc_machine_input_reachable_supply(self) -> None:
        """Rule DRC-05 passes when machine input inserter is reachable from input pins."""
        entities = [
            {
                "name": "electric-furnace",
                "position": {"x": 13.5, "y": 11.5},
            },
            {
                "name": "fast-inserter",
                "direction": DIRECTION_WEST,
                "position": {"x": 11.5, "y": 11.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 9.5, "y": 11.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 10.5, "y": 11.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, input_pins=[(9.0, 11.0)])
        self.assertEqual(len([v for v in violations if v.code == "DRC-05"]), 0)

    def test_drc_unresolved_surface_crossing_flags_drc08(self) -> None:
        """Rule DRC-08 flags unresolved surface crossings."""
        entities = [
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 5.5, "y": 5.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, unresolved_crossings=[(5.5, 5.5)])
        self.assertTrue(any(v.code == "DRC-08" and "Unresolved" in v.message for v in violations))

    def test_drc_surface_tile_collision_flags_drc08(self) -> None:
        """Rule DRC-08 flags multiple surface entities on the same tile."""
        entities = [
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 5.5, "y": 5.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_NORTH,
                "position": {"x": 5.5, "y": 5.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(
            any(v.code == "DRC-08" and "Surface tile collision" in v.message for v in violations)
        )


if __name__ == "__main__":
    unittest.main()
