#!/usr/bin/env python3
"""test_lef.py - the PDK cells as LEF, checked.

usage: test_lef.py <dump-fixture.json>
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lef
import pdk
import solve


class LefTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = solve.Data(sys.argv[1] if len(sys.argv) > 1 else "unused")
        cls.pdk = pdk.Pdk(cls.data)
        cells = [
            cls.pdk.cell("assembling-machine-3", "electronic-circuit"),
            cls.pdk.cell("electric-furnace", "iron-plate"),
            cls.pdk.cell("oil-refinery", "advanced-oil-processing"),
        ]
        cls.cells = [c for c in cells if c is not None]
        cls.text = lef.write_lef(cls.pdk, cls.cells, "/dev/null")

    def test_check_lef_clean(self) -> None:
        self.assertEqual(lef.check_lef(self.cells, self.text), [])

    def test_macro_structure(self) -> None:
        self.assertIn("MACRO assembling_machine_3__electronic_circuit", self.text)
        self.assertIn("SIZE 5.000 BY 3.000 ;", self.text)
        self.assertIn("PIN iron_plate", self.text)
        self.assertIn("DIRECTION OUTPUT ;", self.text)

    def test_technology_section(self) -> None:
        self.assertIn("VERSION 5.8 ;", self.text)
        self.assertIn("LAYER M1", self.text)
        self.assertIn("LAYER M2", self.text)
        self.assertIn("LAYER M3", self.text)
        self.assertIn("END LIBRARY", self.text)

    def test_via_spans(self) -> None:
        self.assertIn("PROPERTY SPAN 9 ;", self.text)
        self.assertIn("EXPRESS_TRANSPORT_BELT_VIA", self.text)
        self.assertIn("PIPE_TO_GROUND_VIA", self.text)

    def test_every_fixture_cell_checks(self) -> None:
        """The full cell table of the dump produces a consistent LEF."""
        cells = []
        for machine in (
            "assembling-machine-3",
            "electric-furnace",
            "oil-refinery",
            "chemical-plant",
            "rocket-silo",
        ):
            for recipe in self.data.recipes:
                cell = self.pdk.cell(machine, recipe)
                if cell is not None:
                    cells.append(cell)
        text = lef.write_lef(self.pdk, cells, "/dev/null")
        self.assertEqual(lef.check_lef(cells, text), [])

    def test_get_macro_port_slots_and_faces(self) -> None:
        """Verify slot allocation for explicit slots, port_faces filtering, and overflow."""
        explicit_cell = {
            "in_slots": {"iron": 2},
            "out_slots": {"gear": 1},
        }
        in_s, out_s = lef.get_macro_port_slots(explicit_cell)
        self.assertEqual(in_s, {"iron": 2})
        self.assertEqual(out_s, {"gear": 1})

        # Port faces filtering: only W inputs get vertical slots in w_inputs
        faced_cell = {
            "height": 4,
            "inputs": ["in_w1", "in_n", "in_w2"],
            "outputs": ["out_1", "out_2"],
            "port_faces": {"in_w1": "W", "in_n": "N", "in_w2": "W"},
        }
        in_s, out_s = lef.get_macro_port_slots(faced_cell)
        self.assertEqual(in_s, {"in_w1": 1, "in_w2": 2})
        self.assertEqual(out_s, {"out_1": 1, "out_2": 2})

        # Overflow (> height)
        overflow_cell = {
            "height": 2,
            "inputs": ["i1", "i2", "i3"],
            "outputs": ["o1", "o2", "o3"],
        }
        in_s, out_s = lef.get_macro_port_slots(overflow_cell)
        self.assertEqual(in_s, {"i1": 0, "i2": 1, "i3": 2})
        self.assertEqual(out_s, {"o1": 0, "o2": 1, "o3": 2})

    def test_get_macro_port_geometry_all_faces_and_circuit(self) -> None:
        """Verify port geometry across W, E, N, S faces and circuit cells."""
        circ_cell = {
            "width": 2,
            "height": 2,
            "inputs": ["in_sig"],
            "outputs": ["out_sig"],
            "circuit_cell": True,
            "circuit_layers": ("M9", "M10"),
        }
        g_in = lef.get_macro_port_geometry(circ_cell, "in_sig", is_output=False)
        self.assertEqual(g_in["face"], "W")
        self.assertEqual(g_in["layer"], "M9")
        self.assertEqual(g_in["rect"], (0.0, 0.25, 0.5, 0.75))
        self.assertEqual(g_in["inserter_pos"], (0.5, 0.5))
        self.assertEqual(g_in["approach_pos"], (-0.5, 0.5))

        g_out = lef.get_macro_port_geometry(circ_cell, "out_sig", is_output=True)
        self.assertEqual(g_out["face"], "E")
        self.assertEqual(g_out["layer"], "M9")
        self.assertEqual(g_out["rect"], (1.5, 0.25, 2.0, 0.75))
        self.assertEqual(g_out["inserter_pos"], (1.5, 0.5))
        self.assertEqual(g_out["approach_pos"], (2.5, 0.5))

        cell = {
            "width": 4,
            "height": 4,
            "inputs": ["p_w", "p_e_in", "p_n", "p_s"],
            "outputs": ["p_e_out"],
            "in_slots": {"p_w": 1, "p_e_in": 2, "p_n": 1, "p_s": 2},
            "out_slots": {"p_e_out": 3},
            "port_faces": {
                "p_w": "W",
                "p_e_in": "E",
                "p_n": "N",
                "p_s": "S",
                "p_e_out": "E",
            },
        }
        gw = lef.get_macro_port_geometry(cell, "p_w", is_output=False)
        self.assertEqual(gw["face"], "W")
        self.assertEqual(gw["rect"], (0.0, 1.25, 0.5, 1.75))
        self.assertEqual(gw["inserter_pos"], (0.5, 1.5))
        self.assertEqual(gw["approach_pos"], (-0.5, 1.5))

        ge_in = lef.get_macro_port_geometry(cell, "p_e_in", is_output=False)
        self.assertEqual(ge_in["face"], "E")
        self.assertEqual(ge_in["rect"], (3.5, 2.25, 4.0, 2.75))
        self.assertEqual(ge_in["inserter_pos"], (3.5, 2.5))
        self.assertEqual(ge_in["approach_pos"], (4.5, 2.5))

        ge_out = lef.get_macro_port_geometry(cell, "p_e_out", is_output=True)
        self.assertEqual(ge_out["face"], "E")
        self.assertEqual(ge_out["rect"], (3.5, 3.25, 4.0, 3.75))
        self.assertEqual(ge_out["inserter_pos"], (3.5, 3.5))
        self.assertEqual(ge_out["approach_pos"], (4.5, 3.5))

        gn = lef.get_macro_port_geometry(cell, "p_n", is_output=False)
        self.assertEqual(gn["face"], "N")
        self.assertEqual(gn["layer"], pdk.LAYER_SURFACE_V)
        self.assertEqual(gn["rect"], (1.25, 3.5, 1.75, 4.0))
        self.assertEqual(gn["inserter_pos"], (1.5, 3.5))
        self.assertEqual(gn["approach_pos"], (1.5, 4.5))

        gs = lef.get_macro_port_geometry(cell, "p_s", is_output=False)
        self.assertEqual(gs["face"], "S")
        self.assertEqual(gs["layer"], pdk.LAYER_SURFACE_V)
        self.assertEqual(gs["rect"], (2.25, 0.0, 2.75, 0.5))
        self.assertEqual(gs["inserter_pos"], (2.5, 0.5))
        self.assertEqual(gs["approach_pos"], (2.5, -0.5))

    def test_input_and_output_pin_rect_branches(self) -> None:
        """Verify pin rect calculations with port_faces, without port names, and overflow."""
        cell_faces = {
            "width": 3,
            "height": 3,
            "inputs": ["in_a"],
            "outputs": ["out_a"],
            "port_faces": {"other_port": "N"},
        }
        self.assertEqual(
            lef._input_pin_rect(cell_faces, 0, 1, "in_a"),
            ("M1", 0.0, 1.25, 0.5, 1.75),
        )
        self.assertEqual(
            lef._output_pin_rect(cell_faces, 0, 1, "out_a"),
            ("M1", 2.5, 1.25, 3.0, 1.75),
        )

        # Without port name when total <= height
        cell_basic = {"width": 3, "height": 3, "inputs": ["a", "b"], "outputs": ["x", "y"]}
        self.assertEqual(
            lef._input_pin_rect(cell_basic, 1, 2, ""),
            ("M1", 0.0, 1.25, 0.5, 1.75),
        )
        self.assertEqual(
            lef._output_pin_rect(cell_basic, 0, 2, ""),
            ("M1", 2.5, 0.25, 3.0, 0.75),
        )
        self.assertEqual(
            lef._output_pin_rect(cell_basic, 1, 2, ""),
            ("M1", 2.5, 1.25, 3.0, 1.75),
        )

        # Overflow branches for _input_pin_rect (total > height)
        cell_small = {"width": 3, "height": 2, "inputs": ["i0", "i1", "i2"], "outputs": []}
        self.assertEqual(
            lef._input_pin_rect(cell_small, 0, 3, "i0"),
            ("M1", 0.0, 0.25, 0.5, 0.75),
        )
        self.assertEqual(
            lef._input_pin_rect(cell_small, 2, 3, "i2"),
            ("M2", 1.25, 1.5, 1.75, 2.0),
        )

        # Overflow branches for _output_pin_rect (total > height)
        cell_out_over = {"width": 2, "height": 2, "inputs": [], "outputs": []}
        self.assertEqual(
            lef._output_pin_rect(cell_out_over, 0, 7, ""),
            ("M1", 1.5, 0.25, 2.0, 0.75),
        )
        self.assertEqual(
            lef._output_pin_rect(cell_out_over, 3, 7, ""),
            ("M2", 1.25, 0.0, 1.75, 0.5),
        )
        self.assertEqual(
            lef._output_pin_rect(cell_out_over, 5, 7, ""),
            ("M1", 0.0, 1.25, 0.5, 1.75),
        )
        self.assertEqual(
            lef._output_pin_rect(cell_out_over, 6, 7, ""),
            ("M2", 0.25, 1.5, 0.75, 2.0),
        )

    def test_subtract_rect_and_macro_obs(self) -> None:
        """Verify rectangle subtraction and OBS generation on M1 and M2."""
        self.assertEqual(
            lef._subtract_rect((0.0, 0.0, 3.0, 3.0), (4.0, 4.0, 5.0, 5.0)),
            [(0.0, 0.0, 3.0, 3.0)],
        )
        rem = lef._subtract_rect((0.0, 0.0, 4.0, 4.0), (1.0, 1.0, 3.0, 3.0))
        self.assertEqual(
            rem,
            [
                (0.0, 0.0, 4.0, 1.0),
                (0.0, 3.0, 4.0, 4.0),
                (0.0, 1.0, 1.0, 3.0),
                (3.0, 1.0, 4.0, 3.0),
            ],
        )

        circ_cell = {
            "cell": "decider-combinator.logic",
            "machine": "decider-combinator",
            "recipe": "logic",
            "width": 1,
            "height": 2,
            "crafts_per_minute": 60.0,
            "watts": 1000.0,
            "inputs": ["red_in"],
            "outputs": ["red_out"],
            "circuit_cell": True,
        }
        circ_lines = "\n".join(lef.macro_lines(circ_cell))
        self.assertIn("MACRO decider_combinator__logic", circ_lines)
        self.assertIn("LAYER VL3 ;\n      RECT 0.000 0.000 1.000 2.000 ;", circ_lines)

        multi_layer_cell = {
            "cell": "custom-machine.multi",
            "machine": "custom-machine",
            "recipe": "multi",
            "width": 3,
            "height": 3,
            "crafts_per_minute": 30.0,
            "watts": 500.0,
            "inputs": ["in_w", "in_n"],
            "outputs": ["out_e", "out_s"],
            "in_slots": {"in_w": 1, "in_n": 1},
            "out_slots": {"out_e": 1, "out_s": 1},
            "port_faces": {"in_w": "W", "in_n": "N", "out_e": "E", "out_s": "S"},
        }
        ml_lines = "\n".join(lef.macro_lines(multi_layer_cell))
        self.assertIn("LAYER M1 ;\n      RECT 0.000 0.000 3.000 0.750 ;", ml_lines)
        self.assertIn("RECT 1.000 0.750 2.000 2.250 ;", ml_lines)
        self.assertIn("LAYER M2 ;\n      RECT 0.000 1.000 3.000 2.000 ;", ml_lines)
        self.assertIn("RECT 0.000 2.000 0.750 3.000 ;", ml_lines)
        self.assertIn("RECT 2.250 2.000 3.000 3.000 ;", ml_lines)

    def test_tech_lines_layers_and_vias(self) -> None:
        """Verify exact layer numbering and DEFAULT tags in tech_lines."""
        tech_lines = lef.tech_lines(self.pdk.tech_json())
        tech_text = "\n".join(tech_lines)
        self.assertIn("LAYER VL2\n  TYPE CUT ;", tech_text)
        self.assertIn("LAYER M3\n  TYPE ROUTING ;", tech_text)
        self.assertIn("LAYER VL3\n  TYPE CUT ;", tech_text)
        self.assertIn("LAYER M4\n  TYPE ROUTING ;", tech_text)
        self.assertIn(
            "VIA TRANSPORT_BELT_TURN_VIA\n  DEFAULT\n  LAYER M3 ;\n    RECT -0.25 -0.25 0.25 0.25 ;\n  LAYER VL3 ;\n    RECT -0.125 -0.125 0.125 0.125 ;\n  LAYER M4 ;",
            tech_text,
        )
        self.assertIn(
            "VIA FAST_TRANSPORT_BELT_VIA\n  LAYER M4 ;\n    RECT -0.75 -0.25 0.75 0.25 ;\n  LAYER VL4 ;\n    RECT -0.125 -0.125 0.125 0.125 ;\n  LAYER M5 ;",
            tech_text,
        )

    def test_check_lef_detects_violations(self) -> None:
        """Verify check_lef catches missing macros, missing pins, out-of-bounds rects, and size mismatches."""
        cell = self.cells[0]
        name = cell["cell"].replace(".", "__").replace("-", "_")
        self.assertEqual(lef.check_lef([cell], ""), [f"{name}: macro missing"])

        bad_lef = f"""
MACRO {name}
  SIZE 99.000 BY 99.000 ;
  PIN missing_other_ports
    PORT LAYER M1 ;
      RECT -1.000 -1.000 99.000 99.000 ;
    END
  END missing_other_ports
END {name}
END LIBRARY
"""
        problems = lef.check_lef([cell], bad_lef)
        self.assertTrue(any("pin missing" in p for p in problems))
        self.assertTrue(any("rect x outside macro" in p for p in problems))
        self.assertTrue(any("rect y outside macro" in p for p in problems))
        self.assertTrue(any("SIZE x mismatch" in p for p in problems))
        self.assertTrue(any("SIZE y mismatch" in p for p in problems))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
