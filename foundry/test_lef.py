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
        self.assertIn("PROPERTY SPAN 11 ;", self.text)
        self.assertIn("TURBO_TRANSPORT_BELT_VIA", self.text)
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


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
