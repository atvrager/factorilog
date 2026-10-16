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

    def test_drc_splitter_into_underground_hood_flank(self) -> None:
        """Rule DRC-03 detects splitter output feeding into underground hood flank."""
        entities = [
            {
                "name": "underground-belt",
                "direction": DIRECTION_NORTH,
                "type": "input",
                "position": {"x": 2, "y": 2},
            },
            {
                "name": "splitter",
                "direction": DIRECTION_EAST,
                "position": {"x": 1.0, "y": 2.5},
            },
        ]
        checker = BeltDrcChecker(allow_peeling=False)
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-03" for v in violations))

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
        self.assertIn("M5", stack)
        self.assertIn("M7", stack)
        self.assertIn("M9", stack)
        self.assertIn("M10", stack)
        self.assertNotIn("M11", stack)
        self.assertEqual(stack["M7"]["entity"], "express-underground-belt")
        self.assertEqual(stack["M9"]["level"], "circuit")
        self.assertEqual(stack["M9"]["wire"], "red")
        self.assertEqual(stack["M10"]["wire"], "green")

        lef_lines = lef.tech_lines(p.tech_json())
        lef_text = "\n".join(lef_lines)
        self.assertIn("LAYER M7", lef_text)
        self.assertIn("LAYER M9", lef_text)
        self.assertIn("LAYER M10", lef_text)
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

    def test_drc_circuit_wire_distance_exceeded_flags_drc09(self) -> None:
        """Rule DRC-09 flags wires exceeding maximum reach distance of 9.0 tiles."""
        entities = [
            {"entity_number": 1, "name": "medium-electric-pole", "position": {"x": 0.0, "y": 0.0}},
            {"entity_number": 2, "name": "medium-electric-pole", "position": {"x": 12.0, "y": 0.0}},
        ]
        wires = [[1, 5, 2, 5]]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, wires=wires)
        self.assertTrue(
            any(v.code == "DRC-09" and "exceeds 9.0 tiles limit" in v.message for v in violations)
        )

    def test_drc_combinator_unconnected_inputs_outputs_flags_drc10(self) -> None:
        """Rule DRC-10 flags combinators with unconnected inputs or outputs."""
        entities = [
            {
                "entity_number": 1,
                "name": "decider-combinator",
                "position": {"x": 0.0, "y": 0.0},
            },
            {
                "entity_number": 2,
                "name": "arithmetic-combinator",
                "position": {"x": 2.0, "y": 0.0},
            },
        ]
        # Only input of entity 1 is wired to output of entity 2
        wires = [[1, 1, 2, 3]]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, wires=wires)
        # Entity 1 missing output, Entity 2 missing input
        self.assertTrue(
            any(
                v.code == "DRC-10" and "unconnected outputs" in v.message and "(#1)" in v.message
                for v in violations
            )
        )
        self.assertTrue(
            any(
                v.code == "DRC-10" and "unconnected inputs" in v.message and "(#2)" in v.message
                for v in violations
            )
        )

    def test_drc_constant_combinator_unconnected_flags_drc11(self) -> None:
        """Rule DRC-11 flags constant combinators with no wire connections."""
        entities = [
            {
                "entity_number": 1,
                "name": "constant-combinator",
                "position": {"x": 0.0, "y": 0.0},
            }
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, wires=[])
        self.assertTrue(any(v.code == "DRC-11" for v in violations))

    def test_drc_power_switch_unconnected_flags_drc12(self) -> None:
        """Rule DRC-12 flags power switches missing circuit or copper terminals."""
        entities = [
            {"entity_number": 1, "name": "medium-electric-pole", "position": {"x": 0.0, "y": 0.0}},
            {"entity_number": 2, "name": "power-switch", "position": {"x": 2.0, "y": 0.0}},
        ]
        # Only left copper terminal connected
        wires = [[1, 5, 2, 5]]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, wires=wires)
        self.assertTrue(
            any(v.code == "DRC-12" and "no circuit wire connection" in v.message for v in violations)
        )
        self.assertTrue(
            any(v.code == "DRC-12" and "missing copper terminal" in v.message for v in violations)
        )

    def test_drc_clean_circuit_connectivity_passes(self) -> None:
        """Fully connected circuit and power switch entities pass with zero DRC violations."""
        entities = [
            {"entity_number": 1, "name": "medium-electric-pole", "position": {"x": 0.0, "y": 0.0}},
            {"entity_number": 2, "name": "power-switch", "position": {"x": 2.0, "y": 0.0}},
            {"entity_number": 3, "name": "medium-electric-pole", "position": {"x": 4.0, "y": 0.0}},
            {"entity_number": 4, "name": "decider-combinator", "position": {"x": 2.0, "y": 2.0}},
            {"entity_number": 5, "name": "constant-combinator", "position": {"x": 0.0, "y": 2.0}},
        ]
        wires = [
            [1, 5, 2, 5],  # Pole 1 to Switch left copper
            [2, 6, 3, 5],  # Switch right copper to Pole 3
            [5, 1, 4, 1],  # Constant 5 to Decider 4 input red
            [4, 3, 2, 1],  # Decider 4 output red to Switch 2 circuit red
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, wires=wires)
        circ_violations = [v for v in violations if v.code in ("DRC-09", "DRC-10", "DRC-11", "DRC-12")]
        self.assertEqual(circ_violations, [])

    def test_drc_fluid_mixing_adjacent_pipes(self) -> None:
        """Rule DRC-13 flags adjacent surface pipes carrying different fluids."""
        entities = [
            {"name": "pipe", "position": {"x": 0.0, "y": 0.0}, "fluid": "water"},
            {"name": "pipe", "position": {"x": 1.0, "y": 0.0}, "fluid": "crude-oil"},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-13" for v in violations))

    def test_drc_fluid_clean_separate_pipes(self) -> None:
        """Pipes with matching fluids or without orthogonal contact pass DRC-13."""
        entities = [
            {"name": "pipe", "position": {"x": 0.0, "y": 0.0}, "fluid": "water"},
            {"name": "pipe", "position": {"x": 1.0, "y": 0.0}, "fluid": "water"},
            {"name": "pipe", "position": {"x": 0.0, "y": 2.0}, "fluid": "crude-oil"},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertFalse(any(v.code == "DRC-13" for v in violations))

    def test_drc_pipe_to_ground_valid_pairing(self) -> None:
        """Matching pipe-to-ground entities facing each other within span pass DRC."""
        # Entrance at (0, 0) direction 12 (West) points underground East
        # Exit at (4, 0) direction 4 (East) points underground West
        entities = [
            {"name": "pipe-to-ground", "direction": DIRECTION_WEST, "position": {"x": 0.0, "y": 0.0}},
            {"name": "pipe-to-ground", "direction": DIRECTION_EAST, "position": {"x": 4.0, "y": 0.0}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        pipe_violations = [v for v in violations if v.code in ("DRC-14", "DRC-15")]
        self.assertEqual(pipe_violations, [])

    def test_drc_pipe_to_ground_max_span_ten_passes(self) -> None:
        """Matching pipe-to-ground entities separated by exactly 10 tiles pass DRC."""
        entities = [
            {"name": "pipe-to-ground", "direction": DIRECTION_WEST, "position": {"x": 0.0, "y": 0.0}},
            {"name": "pipe-to-ground", "direction": DIRECTION_EAST, "position": {"x": 10.0, "y": 0.0}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        pipe_violations = [v for v in violations if v.code in ("DRC-14", "DRC-15")]
        self.assertEqual(pipe_violations, [])

    def test_drc_pipe_to_ground_unpaired(self) -> None:
        """Rule DRC-14 flags pipe-to-ground with no matching pair."""
        entities = [
            {"name": "pipe-to-ground", "direction": DIRECTION_WEST, "position": {"x": 0.0, "y": 0.0}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-14" for v in violations))

    def test_drc_pipe_to_ground_span_eleven_exceeded(self) -> None:
        """Rule DRC-14 flags pipe-to-ground pairs separated by 11 tiles."""
        entities = [
            {"name": "pipe-to-ground", "direction": DIRECTION_WEST, "position": {"x": 0.0, "y": 0.0}},
            {"name": "pipe-to-ground", "direction": DIRECTION_EAST, "position": {"x": 11.0, "y": 0.0}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-14" for v in violations))

    def test_drc_pipe_to_ground_span_overlap(self) -> None:
        """Rule DRC-15 flags pipe-to-ground colliding with active underground pipe span."""
        entities = [
            {"name": "pipe-to-ground", "direction": DIRECTION_WEST, "position": {"x": 0.0, "y": 0.0}},
            {"name": "pipe-to-ground", "direction": DIRECTION_EAST, "position": {"x": 6.0, "y": 0.0}},
            {"name": "pipe-to-ground", "direction": DIRECTION_NORTH, "position": {"x": 3.0, "y": 0.0}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertTrue(any(v.code == "DRC-15" for v in violations))

    def test_drc_pipeline_extent_clean(self) -> None:
        """Rule DRC-16 passes when pipeline extent does not exceed 320 tiles."""
        entities = [
            {"name": "pipe", "position": {"x": float(i), "y": 0.0}}
            for i in range(50)
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        self.assertFalse(any(v.code == "DRC-16" for v in violations))

    def test_drc_pipeline_extent_exceeded(self) -> None:
        """Rule DRC-16 flags pipeline networks exceeding maximum allowed extent (320 tiles)."""
        entities = [
            {"name": "pipe", "position": {"x": float(i), "y": 0.0}}
            for i in range(350)
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        drc16 = [v for v in violations if v.code == "DRC-16"]
        self.assertEqual(len(drc16), 1)
        self.assertIn("exceeds maximum allowed extent 320", drc16[0].message)

    def test_drc_pipeline_extent_pump_isolated(self) -> None:
        """Inline pump divides pipeline into separate segments, passing DRC-16."""
        entities = []
        # First 175 tiles: x in [0..174], y = 0
        for i in range(175):
            entities.append({"name": "pipe", "position": {"x": float(i) + 0.5, "y": 0.5}})
        # Pump at tiles 175, 176 facing East (direction 4), center = (176.0, 0.5)
        entities.append({"name": "pump", "position": {"x": 176.0, "y": 0.5}, "direction": DIRECTION_EAST})
        # Remaining 175 tiles: x in [177..351], y = 0
        for i in range(177, 352):
            entities.append({"name": "pipe", "position": {"x": float(i) + 0.5, "y": 0.5}})

        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        drc16 = [v for v in violations if v.code == "DRC-16"]
        self.assertEqual(drc16, [])

    def test_drc_power_coverage_unpowered_inserter_flags_drc17(self) -> None:
        """Rule DRC-17 flags electric consumers outside electric pole supply area."""
        entities = [
            {"entity_number": 1, "name": "medium-electric-pole", "position": {"x": 0.5, "y": 0.5}},
            {"entity_number": 2, "name": "fast-inserter", "position": {"x": 1.5, "y": 0.5}},
            {"entity_number": 3, "name": "fast-inserter", "position": {"x": 10.5, "y": 0.5}},
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities)
        drc17 = [v for v in violations if v.code == "DRC-17"]
        self.assertEqual(len(drc17), 1)
        self.assertIn("Unpowered entity fast-inserter at (10.5, 0.5)", drc17[0].message)

    def test_drc_power_coverage_disconnected_pole_flags_drc17(self) -> None:
        """Rule DRC-17 flags consumers covered only by poles disconnected from the main PDN."""
        entities = [
            {"entity_number": 1, "name": "medium-electric-pole", "position": {"x": 0.5, "y": 0.5}},
            {"entity_number": 2, "name": "medium-electric-pole", "position": {"x": 6.5, "y": 0.5}},
            {"entity_number": 3, "name": "medium-electric-pole", "position": {"x": 20.5, "y": 0.5}},
            {"entity_number": 4, "name": "fast-inserter", "position": {"x": 1.5, "y": 0.5}},
            {"entity_number": 5, "name": "fast-inserter", "position": {"x": 21.5, "y": 0.5}},
        ]
        # Poles 1 and 2 form the main PDN. Pole 3 is an unwired island.
        wires = [[1, 5, 2, 5]]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, wires=wires)
        drc17 = [v for v in violations if v.code == "DRC-17"]
        self.assertEqual(len(drc17), 1)
        self.assertIn("disconnected electric pole", drc17[0].message)

    def test_drc_power_coverage_duplicate_entity_number_flags_drc17(self) -> None:
        """Rule DRC-17 flags duplicate entity numbers that corrupt copper wire connections."""
        entities = [
            {"entity_number": 1, "name": "medium-electric-pole", "position": {"x": 0.5, "y": 0.5}},
            {"entity_number": 2, "name": "medium-electric-pole", "position": {"x": 6.5, "y": 0.5}},
            {"entity_number": 2, "name": "small-lamp", "position": {"x": 1.5, "y": 0.5}},
        ]
        wires = [[1, 5, 2, 5]]
        checker = BeltDrcChecker()
        violations = checker.check_entities(entities, wires=wires)
        drc17 = [v for v in violations if v.code == "DRC-17"]
        self.assertTrue(any("Duplicate entity_number #2" in v.message for v in drc17))

    def test_drc_external_input_pin_blocked_loader_flags_drc05(self) -> None:
        """Rule DRC-05 flags West boundary input pin whose belt points North into another pin."""
        entities = [
            {
                "name": "transport-belt",
                "direction": DIRECTION_NORTH,
                "position": {"x": 0.5, "y": 204.5},
            },
            {
                "name": "transport-belt",
                "direction": DIRECTION_EAST,
                "position": {"x": 0.5, "y": 206.5},
            },
        ]
        checker = BeltDrcChecker()
        violations = checker.check_entities(
            entities,
            input_pins=[(0.0, 204.0), (0.0, 206.0)],
        )
        drc05 = [v for v in violations if v.code == "DRC-05"]
        self.assertEqual(len(drc05), 1)
        self.assertIn("External input pin at (0, 204)", drc05[0].message)


if __name__ == "__main__":
    unittest.main()


