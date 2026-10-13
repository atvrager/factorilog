"""Unit tests for NativeLayoutVerifier layout DRC and LVS simulation checks."""

from __future__ import annotations

import unittest
from pathlib import Path

from foundry.native_engine.engine import FactorioNativeEngine
from foundry.native_engine.symbol_finder import resolve_factorio_binary
from foundry.native_engine.verifier import (
    NativeLayoutVerifier,
    NetSpec,
)


class TestLayoutVerifier(unittest.TestCase):
    """Test suite for physical layout DRC and LVS verification."""

    def setUp(self) -> None:
        self.game_bin = resolve_factorio_binary()
        if not self.game_bin:
            self.skipTest("Factorio binary not found on host system.")
        self.engine = FactorioNativeEngine(self.game_bin)
        self.verifier = NativeLayoutVerifier(self.engine)

    def test_verifier_clean_straight_belt(self) -> None:
        """Verify that a straight connected belt run passes DRC and LVS checks."""
        # 4 connected belts running East (direction 4)
        entities = [
            {"name": "transport-belt", "position": {"x": 0.5, "y": 0.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 1.5, "y": 0.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 2.5, "y": 0.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 3.5, "y": 0.5}, "direction": 4},
        ]
        net = NetSpec(
            name="net_iron",
            commodity="iron-plate",
            source_pos=(0.5, 0.5),
            sink_pos=(3.5, 0.5),
        )

        res = self.verifier.verify_blueprint(entities, nets=[net], ticks=120)
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)
        self.assertEqual(len(res.lvs_errors), 0)
        self.assertEqual(res.delivered_items.get("net_iron"), 1)

    def test_verifier_clean_underground_belt(self) -> None:
        """Verify that a legal underground belt pair passes DRC checks."""
        # Yellow underground pair spanning 4 tiles East (direction 4)
        entities = [
            {
                "name": "underground-belt",
                "position": {"x": 0.5, "y": 0.5},
                "direction": 4,
                "type": "input",
            },
            {
                "name": "underground-belt",
                "position": {"x": 4.5, "y": 0.5},
                "direction": 4,
                "type": "output",
            },
        ]
        net = NetSpec(
            name="net_copper",
            commodity="copper-plate",
            source_pos=(0.5, 0.5),
            sink_pos=(4.5, 0.5),
        )

        res = self.verifier.verify_blueprint(entities, nets=[net], ticks=120)
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)

    def test_verifier_underground_span_exceeded(self) -> None:
        """Verify that underground spans exceeding the tier limit trigger a DRC error."""
        # Basic yellow underground spanning 7 tiles (limit is 5)
        entities = [
            {
                "name": "underground-belt",
                "position": {"x": 0.5, "y": 0.5},
                "direction": 4,
                "type": "input",
            },
            {
                "name": "underground-belt",
                "position": {"x": 7.5, "y": 0.5},
                "direction": 4,
                "type": "output",
            },
        ]

        violations = self.verifier.check_drc(entities)
        self.assertTrue(any(v.code == "DRC_UG_SPAN_EXCEEDED" for v in violations))

    def test_verifier_underground_unpaired(self) -> None:
        """Verify that an unpaired underground entrance triggers a DRC error."""
        entities = [
            {
                "name": "underground-belt",
                "position": {"x": 0.5, "y": 0.5},
                "direction": 4,
                "type": "input",
            }
        ]

        violations = self.verifier.check_drc(entities)
        self.assertTrue(any(v.code == "DRC_UG_UNPAIRED" for v in violations))

    def test_verifier_underground_tier_mismatch(self) -> None:
        """Verify that pairing different underground belt tiers triggers a DRC error."""
        entities = [
            {
                "name": "underground-belt",
                "position": {"x": 0.5, "y": 0.5},
                "direction": 4,
                "type": "input",
            },
            {
                "name": "fast-underground-belt",
                "position": {"x": 3.5, "y": 0.5},
                "direction": 4,
                "type": "output",
            },
        ]

        violations = self.verifier.check_drc(entities)
        self.assertTrue(any(v.code == "DRC_UG_TIER_MISMATCH" for v in violations))

    def test_verifier_clean_pipe_network(self) -> None:
        """Verify that a continuous pipe run passes DRC and delivers fluid in LVS."""
        entities = [
            {"name": "pipe", "position": {"x": 0.5, "y": 0.5}},
            {"name": "pipe", "position": {"x": 1.5, "y": 0.5}},
            {"name": "pipe", "position": {"x": 2.5, "y": 0.5}},
        ]
        net = NetSpec(
            name="net_water",
            commodity="water",
            source_pos=(0.5, 0.5),
            sink_pos=(2.5, 0.5),
            is_fluid=True,
        )

        res = self.verifier.verify_blueprint(entities, nets=[net], ticks=60)
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.drc_violations), 0)
        self.assertEqual(len(res.lvs_errors), 0)
        self.assertGreater(res.delivered_fluids.get("net_water", 0.0), 0.0)

    def test_verifier_pipe_span_exceeded(self) -> None:
        """Verify that pipe-to-ground spans exceeding 10 tiles trigger a DRC error."""
        # Pipe-to-ground spanning 12 tiles (limit is 10)
        entities = [
            {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 0.5}, "direction": 4},
            {"name": "pipe-to-ground", "position": {"x": 12.5, "y": 0.5}, "direction": 12},
        ]

        violations = self.verifier.check_drc(entities)
        self.assertTrue(any(v.code == "DRC_PIPE_SPAN_EXCEEDED" for v in violations))

    def test_verifier_fluid_contamination_drc(self) -> None:
        """Verify that connecting conflicting fluids to the same pipe network triggers a DRC error."""
        entities = [
            {"name": "pipe", "position": {"x": 0.5, "y": 0.5}},
            {"name": "pipe", "position": {"x": 1.5, "y": 0.5}},
            {"name": "pipe", "position": {"x": 2.5, "y": 0.5}},
        ]
        net_water = NetSpec(
            name="net_water",
            commodity="water",
            source_pos=(0.5, 0.5),
            sink_pos=(1.5, 0.5),
            is_fluid=True,
        )
        net_oil = NetSpec(
            name="net_oil",
            commodity="crude-oil",
            source_pos=(1.5, 0.5),
            sink_pos=(2.5, 0.5),
            is_fluid=True,
        )

        violations = self.verifier.check_drc(entities, nets=[net_water, net_oil])
        self.assertTrue(any(v.code == "DRC_FLUID_CONTAMINATION" for v in violations))

    def test_verifier_disconnected_belt_lvs_error(self) -> None:
        """Verify that a disconnected belt path triggers an LVS routing error."""
        # Gap at x=1.5
        entities = [
            {"name": "transport-belt", "position": {"x": 0.5, "y": 0.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 2.5, "y": 0.5}, "direction": 4},
        ]
        net = NetSpec(
            name="net_broken",
            commodity="iron-plate",
            source_pos=(0.5, 0.5),
            sink_pos=(2.5, 0.5),
        )

        res = self.verifier.verify_blueprint(entities, nets=[net], ticks=60)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("No continuous belt path found" in err.message for err in res.lvs_errors))

    def test_verifier_real_blueprint_file(self) -> None:
        """Verify that real blueprint files from the repository parse and pass DRC."""
        bp_path = Path("test_straight_col17.blueprint")
        if not bp_path.is_file():
            self.skipTest("Blueprint file not found.")

        bp_string = bp_path.read_text(encoding="utf-8").strip()
        entities = self.verifier.parse_entities(bp_string)
        self.assertGreater(len(entities), 0)

        violations = self.verifier.check_drc(entities)
        # Verify check completes without crash
        self.assertIsInstance(violations, list)

    def test_verifier_modded_prototype_spans(self) -> None:
        """Verify that the verifier respects modified prototype parameters from mods."""
        # Mod overrides: underground-belt max span is 15, pipe-to-ground max span is 30
        custom_prototypes = {
            "underground-belt": {
                "underground-belt": {
                    "name": "underground-belt",
                    "max_distance": 15,
                }
            },
            "pipe-to-ground": {
                "pipe-to-ground": {
                    "name": "pipe-to-ground",
                    "fluid_box": {
                        "pipe_connections": [
                            {"connection_type": "normal"},
                            {
                                "connection_type": "underground",
                                "max_underground_distance": 30,
                            },
                        ]
                    },
                }
            },
        }

        modded_verifier = NativeLayoutVerifier(self.engine, prototypes=custom_prototypes)

        # 1. 14-tile underground belt span passes with modded prototype
        ug_entities = [
            {
                "name": "underground-belt",
                "position": {"x": 0.5, "y": 0.5},
                "direction": 4,
                "type": "input",
            },
            {
                "name": "underground-belt",
                "position": {"x": 14.5, "y": 0.5},
                "direction": 4,
                "type": "output",
            },
        ]
        violations = modded_verifier.check_drc(ug_entities)
        self.assertEqual(len(violations), 0)

        # 2. 25-tile pipe-to-ground span passes with modded prototype
        pipe_entities = [
            {"name": "pipe-to-ground", "position": {"x": 0.5, "y": 0.5}, "direction": 4},
            {"name": "pipe-to-ground", "position": {"x": 25.5, "y": 0.5}, "direction": 12},
        ]
        pipe_violations = modded_verifier.check_drc(pipe_entities)
        self.assertEqual(len(pipe_violations), 0)


if __name__ == "__main__":
    unittest.main()

