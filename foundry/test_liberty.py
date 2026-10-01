#!/usr/bin/env python3
"""test_liberty.py - the PDK cells as Liberty stubs, checked.

usage: test_liberty.py <dump-fixture.json>
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import liberty
import netlist
import pdk
import solve


class LibertyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = solve.Data(sys.argv[1] if len(sys.argv) > 1 else "unused")
        cls.pdk = pdk.Pdk(cls.data)
        cells = [
            cls.pdk.cell("assembling-machine-3", "electronic-circuit"),
            cls.pdk.cell("electric-furnace", "iron-plate"),
        ]
        cls.cells = [c for c in cells if c is not None]
        cls.text = liberty.write_liberty(cls.cells, "/dev/null")

    def test_check_liberty_clean(self) -> None:
        self.assertEqual(liberty.check_liberty(self.cells, self.text), [])

    def test_cell_shape(self) -> None:
        self.assertIn("cell (assembling_machine_3__electronic_circuit) {", self.text)
        self.assertIn("area : 9.000 ;", self.text)
        self.assertIn("pin (iron_plate) {", self.text)
        self.assertIn("pin (copper_cable) {", self.text)
        self.assertIn("direction : output ;", self.text)

    def test_area_is_footprint(self) -> None:
        cell = self.pdk.cell("oil-refinery", "advanced-oil-processing")
        assert cell is not None
        text = liberty.write_liberty([cell], "/dev/null")
        self.assertIn("area : 25.000 ;", text)

    def test_craft_delay_is_recipe_duration(self) -> None:
        # assembling-machine-3 has crafting speed 1.25.
        # electronic-circuit has energy 0.5 s. Delay = 0.5 / 1.25 = 0.4000 s.
        self.assertIn("delay_model : table_lookup ;", self.text)
        self.assertIn('cell_rise (scalar) { values ("0.4000") ; }', self.text)
        self.assertIn('rise_transition (scalar) { values ("0.0400") ; }', self.text)

    def test_leakage_is_the_power_draw(self) -> None:
        # The electric furnace draws 180 kW. The unit is 1mW.
        self.assertIn("cell_leakage_power : 180.000000 ;", self.text)

    def test_merge_cell_has_a_view(self) -> None:
        merge = netlist.merge_cell()
        text = liberty.write_liberty([merge], "/dev/null")
        self.assertEqual(liberty.check_liberty([merge], text), [])
        self.assertIn("cell (belt_merge2) {", text)
        self.assertIn("pin (a) {", text)
        self.assertIn("pin (y) {", text)

    def test_full_cell_table(self) -> None:
        """Every cell the dump offers gets a consistent Liberty view."""
        cells = [netlist.merge_cell()]
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
        text = liberty.write_liberty(cells, "/dev/null")
        self.assertEqual(liberty.check_liberty(cells, text), [])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
