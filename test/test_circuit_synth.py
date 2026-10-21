"""Unit tests for Verilog controller synthesis into Factorio combinators."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Add directories to python path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "foundry"))
sys.path.insert(0, str(REPO_ROOT / "flow"))

import circuit_synth
import netbuild
import netlist
import pdk
import solve


class TestCircuitSynth(unittest.TestCase):
    """Test Verilog synthesis to combinator cells and netlist integration."""

    @classmethod
    def setUpClass(cls) -> None:
        fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
        cls.data = solve.Data(str(fixture_path))
        cls.pdk = pdk.Pdk(cls.data)
        cls.allowed = cls.data.allowed_recipes(["nauvis"])

    def test_synthesize_power_sequencer_structure(self) -> None:
        """Verify the synthesized structure of the power sequencer FSM."""
        result = circuit_synth.synthesize_power_sequencer()
        self.assertEqual(result.module_name, "power_sequencer")

        # Port assertions
        self.assertIn("accumulator_charge", result.inputs)
        self.assertIn("power_switch_enable", result.outputs)
        self.assertIn("backup_pump_enable", result.outputs)
        self.assertIn("alert_code", result.outputs)

        # Parameter assertions
        self.assertEqual(result.parameters["STATE_NORMAL"], 0)
        self.assertEqual(result.parameters["STATE_THROTTLED"], 1)
        self.assertEqual(result.parameters["STATE_EMERGENCY"], 2)
        self.assertEqual(result.parameters["THRESH_THROTTLED"], 30)
        self.assertEqual(result.parameters["THRESH_RECOVER_NORMAL"], 70)

        # Cell instance assertions
        cell_names = {c.name for c in result.cells}
        self.assertIn("u_const_params", cell_names)
        self.assertIn("u_dec_throttled", cell_names)
        self.assertIn("u_dec_emergency", cell_names)
        self.assertIn("u_dec_rec_normal", cell_names)
        self.assertIn("u_dec_rec_throttled", cell_names)
        self.assertIn("u_state_latch", cell_names)
        self.assertIn("u_out_power_switch", cell_names)
        self.assertIn("u_out_backup_pump", cell_names)
        self.assertIn("u_out_alert_code", cell_names)
        self.assertIn("u_main_power_switch", cell_names)

        # Wire connection assertions
        # Check feedback connection on state latch
        latch_loop = any(
            conn.source == "u_state_latch"
            and conn.target == "u_state_latch"
            and conn.color == "green"
            for conn in result.connections
        )
        self.assertTrue(latch_loop, "State latch feedback loop must exist")

        # Check power switch enable control connection
        ps_ctrl = any(
            conn.source == "u_out_power_switch" and conn.target == "u_main_power_switch"
            for conn in result.connections
        )
        self.assertTrue(ps_ctrl, "Power switch control wire must exist")

    def test_synthesize_verilog_file(self) -> None:
        """Verify synthesis from the power_sequencer.v source file."""
        v_path = REPO_ROOT / "flow" / "controllers" / "power_sequencer.v"
        self.assertTrue(v_path.is_file(), "power_sequencer.v file must exist")

        result = circuit_synth.synthesize_verilog(v_path)
        self.assertEqual(result.module_name, "power_sequencer")
        self.assertGreater(len(result.cells), 0)

    def test_fuse_controller_into_netlist(self) -> None:
        """Verify fusion of synthesized controller into the factory netlist."""
        demand = {
            "automation-science-pack": 15.0,
        }
        nl = netlist.build(self.data, demand, self.allowed)
        initial_inst_count = len(nl.instances)

        synth_result = circuit_synth.synthesize_power_sequencer()
        circuit_synth.fuse_controller_into_netlist(nl, synth_result, base_rank=10)

        # Assert instances added
        self.assertEqual(len(nl.instances), initial_inst_count + len(synth_result.cells))

        # Assert combinator cells registered in cell library
        self.assertIn(pdk.ARITHMETIC_COMBINATOR_CELL, nl.cells)
        self.assertIn(pdk.DECIDER_COMBINATOR_CELL, nl.cells)
        self.assertIn(pdk.CONSTANT_COMBINATOR_CELL, nl.cells)
        self.assertIn(pdk.POWER_SWITCH_CELL, nl.cells)

        # Assert controller bus nets registered
        self.assertIn("c_power_sequencer_in", nl.nets)
        self.assertIn("c_power_sequencer_out", nl.nets)

    def test_generic_verilog_synthesis(self) -> None:
        """Verify generic synthesis of expressions into arithmetic and decider combinators."""
        verilog_code = """
        module test_calc (
            input wire [31:0] sensor_value,
            output wire [31:0] threshold_alert,
            output wire [31:0] scaled_value
        );
            wire alert = (sensor_value > 32'd50);
            wire [31:0] scaled = sensor_value * 2;
        endmodule
        """
        result = circuit_synth.synthesize_verilog(verilog_code)
        self.assertEqual(result.module_name, "test_calc")

        cell_types = [c.cell for c in result.cells]
        self.assertIn(pdk.DECIDER_COMBINATOR_CELL, cell_types)
        self.assertIn(pdk.ARITHMETIC_COMBINATOR_CELL, cell_types)

    def test_netbuild_with_controller_and_telemetry(self) -> None:
        """Verify full netbuild pipeline with both telemetry and Verilog controller."""
        v_path = REPO_ROOT / "flow" / "controllers" / "power_sequencer.v"
        dump_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
        demand = {"automation-science-pack": 15.0}

        with tempfile.TemporaryDirectory() as tmp_dir:
            ret = netbuild.build_all(
                dump_path=dump_path,
                demand=demand,
                out_dir=tmp_dir,
                tap_scope="io,merges",
                enable_display=True,
                enable_circuit=True,
                enable_compound_cells=False,
                synthesize_splitters=True,
                controller_verilog=v_path,
            )
            self.assertEqual(ret, 0)

            # Verify generated output files exist
            out_path = Path(tmp_dir)
            self.assertTrue((out_path / "netlist.v").is_file())
            self.assertTrue((out_path / "cells.v").is_file())
            self.assertTrue((out_path / "netlist.json").is_file())
            self.assertTrue((out_path / "tech.lef").is_file())
            self.assertTrue((out_path / "tech.lib").is_file())
            self.assertTrue((out_path / "ranks.tcl").is_file())

            # Verify sidecar contents
            sidecar = json.loads((out_path / "netlist.json").read_text())
            self.assertIn("circuit_connections", sidecar)
            self.assertGreater(len(sidecar["circuit_connections"]), 0)

            inst_by_name = {inst["name"]: inst for inst in sidecar["instances"]}
            self.assertIn("u_state_latch", inst_by_name)
            self.assertIn("control_behavior", inst_by_name["u_state_latch"])
            self.assertIn("u_main_power_switch", inst_by_name)

            # Verify netlist.v contents
            v_text = (out_path / "netlist.v").read_text()
            self.assertIn("decider_combinator u_state_latch", v_text)
            self.assertIn("power_switch u_main_power_switch", v_text)


if __name__ == "__main__":
    unittest.main()
