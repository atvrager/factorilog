"""Unit tests for circuit logic system, combinator cells, and telemetry taps."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Add directories to python path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "foundry"))
sys.path.insert(0, str(REPO_ROOT / "flow"))

import blueprint
import check_blueprint
import def2bp
import lef
import liberty
import netlist
import pdk
import solve


class TestCircuitTelemetry(unittest.TestCase):
    """Test circuit layer definitions, combinator PDK cells, and telemetry taps."""

    @classmethod
    def setUpClass(cls) -> None:
        fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
        cls.data = solve.Data(str(fixture_path))
        cls.pdk = pdk.Pdk(cls.data)
        cls.allowed = cls.data.allowed_recipes(["nauvis"])

    def test_pdk_and_lef_circuit_layers(self) -> None:
        """Verify circuit layers and combinator macros in LEF and Liberty."""
        self.assertEqual(pdk.LAYER_CIRCUIT_RED, "M5")
        self.assertEqual(pdk.LAYER_CIRCUIT_GREEN, "M6")

        comb_cells = [
            pdk.display_panel_cell(),
            pdk.arithmetic_combinator_cell(),
            pdk.decider_combinator_cell(),
            pdk.constant_combinator_cell(),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            lef_path = Path(tmp) / "tech.lef"
            lib_path = Path(tmp) / "tech.lib"
            lef_text = lef.write_lef(self.pdk, comb_cells, str(lef_path))
            lib_text = liberty.write_liberty(comb_cells, str(lib_path))

            # Verify layers and vias exist in LEF
            self.assertIn("LAYER M5", lef_text)
            self.assertIn("LAYER M6", lef_text)
            self.assertIn("VIA CIRCUIT_DROP_VIA", lef_text)
            self.assertIn("VIA CIRCUIT_TURN_VIA", lef_text)

            # Structural checks
            lef_errs = lef.check_lef(comb_cells, lef_text)
            self.assertEqual(lef_errs, [])

            lib_errs = liberty.check_liberty(comb_cells, lib_text)
            self.assertEqual(lib_errs, [])

    def test_netlist_telemetry_insertion(self) -> None:
        """Verify telemetry tap insertion and Verilog emission."""
        demand = {
            "automation-science-pack": 15.0,
            "logistic-science-pack": 15.0,
        }
        nl = netlist.build(self.data, demand, self.allowed)
        plans = netlist.merge_plans_of(nl)

        records = netlist.insert_telemetry(
            nl,
            tap_scope="io,merges",
            enable_display=True,
            enable_circuit=True,
            merge_plans=plans,
        )
        self.assertGreater(len(records), 0)

        # Verify instances exist
        inst_keys = {i.cell_key for i in nl.instances}
        self.assertIn(pdk.DISPLAY_PANEL_CELL, inst_keys)
        self.assertIn(pdk.ARITHMETIC_COMBINATOR_CELL, inst_keys)

        verilog = netlist.emit_verilog(nl, plans)
        self.assertIn("display_panel u_disp_", verilog)
        self.assertIn("arithmetic_combinator u_acc_", verilog)
        self.assertIn("wire c_out_", verilog)

        cells_v = netlist.emit_cells_v(nl)
        self.assertIn("module display_panel", cells_v)
        self.assertIn("module arithmetic_combinator", cells_v)

    def test_blueprint_circuit_wiring(self) -> None:
        """Verify DEF to blueprint conversion of circuit telemetry and control behaviors."""
        def_text = """VERSION 5.8 ;
UNITS DISTANCE MICRONS 1000 ;
COMPONENTS 2 ;
- u_disp_iron_plate display_panel + PLACED ( 1000 1000 ) N ;
- u_acc_iron_plate arithmetic_combinator + PLACED ( 1000 3000 ) N ;
END COMPONENTS
NETS 1 ;
- iron_plate ( * )
  + ROUTED M1 ( 2000 1000 ) ( 3000 1000 ) ;
END NETS
END DESIGN
"""
        netlist_data = {
            "fluids": [],
            "cells": {
                pdk.DISPLAY_PANEL_CELL: pdk.display_panel_cell(),
                pdk.ARITHMETIC_COMBINATOR_CELL: pdk.arithmetic_combinator_cell(),
            },
            "instances": [
                {
                    "name": "u_disp_iron_plate",
                    "cell": pdk.DISPLAY_PANEL_CELL,
                    "recipe": "iron-plate",
                },
                {
                    "name": "u_acc_iron_plate",
                    "cell": pdk.ARITHMETIC_COMBINATOR_CELL,
                    "recipe": "iron-plate",
                },
            ],
            "telemetry": [
                {
                    "material": "iron-plate",
                    "display_instance": "u_disp_iron_plate",
                    "circuit_instance": "u_acc_iron_plate",
                }
            ],
        }

        bp_str = def2bp.def_to_blueprint(def_text, netlist_data, "test_telemetry")
        entities = blueprint.entities_of(bp_str)

        # Verify entities placed
        names = {e["name"] for e in entities}
        self.assertIn("display-panel", names)
        self.assertIn("arithmetic-combinator", names)
        self.assertIn("transport-belt", names)

        # Verify control behavior and wire connections
        tapped_belt = next(e for e in entities if e["name"] == "transport-belt")
        self.assertTrue(tapped_belt.get("control_behavior", {}).get("circuit_read_hand_contents"))

        disp = next(e for e in entities if e["name"] == "display-panel")
        self.assertEqual(disp.get("text"), "iron-plate")
        self.assertIn("connections", disp)

        acc = next(e for e in entities if e["name"] == "arithmetic-combinator")
        self.assertIn("connections", acc)
        # Verify accumulator feedback loop
        conn_1 = acc["connections"].get("1", {})
        conn_2 = acc["connections"].get("2", {})
        self.assertIn("green", conn_1)
        self.assertIn("green", conn_2)

        # Validate with headless Factorio if available
        try:
            game_bin = check_blueprint.find_factorio_binary()
            with tempfile.TemporaryDirectory(dir="/tmp", prefix="test-telemetry-") as tmp:
                results = check_blueprint.validate_blueprint(bp_str, game_bin, Path(tmp))
                self.assertEqual(results.get("RESULT_UNPLACED_TOTAL"), 0)
                self.assertEqual(results.get("RESULT_MATCHED"), results.get("RESULT_TOTAL"))
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    unittest.main()
