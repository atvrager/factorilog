"""Tests for tools/inspect_cell.py."""

from __future__ import annotations

import unittest
from pathlib import Path

from foundry import solve
from tools import inspect_cell

FIXTURE_PATH = Path("test/data/dump-fixture.json")


class TestInspectCell(unittest.TestCase):
    """Verify PDK cell inspector tool."""

    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE_PATH.is_file():
            raise unittest.SkipTest(f"Fixture not found: {FIXTURE_PATH}")
        cls.data = solve.Data(str(FIXTURE_PATH))

    def test_inspect_chemical_plant_plastic(self) -> None:
        """Verify chemical plant with plastic bar has proper ports and direction."""
        spec = inspect_cell.inspect_cell_spec(self.data, "chemical-plant", "plastic-bar")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec["direction"], 12)  # West
        self.assertTrue(spec["has_fluids"])
        inputs = {inp["port"]: inp for inp in spec["inputs"]}
        self.assertIn("petroleum-gas", inputs)
        self.assertEqual(inputs["petroleum-gas"]["slot"], 3)
        self.assertEqual(inputs["petroleum-gas"]["type"], "fluid")
        self.assertIn("coal", inputs)
        self.assertEqual(inputs["coal"]["slot"], 2)
        self.assertEqual(inputs["coal"]["type"], "item")

    def test_inspect_oil_refinery_basic(self) -> None:
        """Verify oil refinery with basic oil processing has proper ports and direction."""
        spec = inspect_cell.inspect_cell_spec(self.data, "oil-refinery", "basic-oil-processing")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec["direction"], 4)  # East
        self.assertTrue(spec["has_fluids"])
        inputs = {inp["port"]: inp for inp in spec["inputs"]}
        self.assertEqual(inputs["crude-oil"]["slot"], 4)
        self.assertEqual(inputs["crude-oil"]["type"], "fluid")
        outputs = {out["port"]: out for out in spec["outputs"]}
        self.assertEqual(outputs["petroleum-gas"]["slot"], 5)
        self.assertEqual(outputs["petroleum-gas"]["type"], "fluid")

    def test_format_cell_spec(self) -> None:
        """Verify readable technical text formatting."""
        spec = inspect_cell.inspect_cell_spec(self.data, "chemical-plant", "plastic-bar")
        assert spec is not None
        text = inspect_cell.format_cell_spec(spec)
        self.assertIn("Cell: chemical-plant.plastic-bar", text)
        self.assertIn("petroleum-gas", text)
        self.assertIn("coal", text)


if __name__ == "__main__":
    unittest.main()
