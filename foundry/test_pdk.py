#!/usr/bin/env python3
"""test_pdk.py - one full pass: dump -> plan -> PDK cells -> blueprint.

This is the smoke test of the whole synthesis chain. It solves a 60
science-per-minute demand on the fixture dump, reads the cells back, and
round-trips a blueprint string. No game and no network.

usage: test_pdk.py <dump-fixture.json>
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blueprint
import pdk
import solve


class PdkSmokeTest(unittest.TestCase):
    data: solve.Data
    pdk: pdk.Pdk
    rates: dict[str, float]

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = solve.Data(sys.argv[1] if len(sys.argv) > 1 else "unused")
        cls.pdk = pdk.Pdk(cls.data)
        cls.allowed = cls.data.allowed_recipes(["nauvis"])
        demand = {
            name: 60.0
            for name in (
                "automation-science-pack",
                "logistic-science-pack",
                "military-science-pack",
                "chemical-science-pack",
                "production-science-pack",
                "utility-science-pack",
            )
            if name in cls.data.recipes
        }
        cls.rates, cls.raw, _surplus = solve.solve_rates(cls.data, demand, cls.allowed)

    def test_plan_meets_demand(self) -> None:
        for pack in ("automation-science-pack", "logistic-science-pack"):
            if pack in self.data.recipes:
                self.assertAlmostEqual(self.rates[pack], 60.0, places=4)

    def test_cell_from_plan(self) -> None:
        recipe = "electronic-circuit"
        self.assertIn(recipe, self.rates)
        cell = self.pdk.cell("assembling-machine-3", recipe)
        assert cell is not None
        self.assertEqual(cell["width"], 5)
        self.assertEqual(cell["machine_width"], 3)
        self.assertEqual(cell["height"], 3)
        self.assertIn("iron-plate", cell["inputs"])
        self.assertIn("copper-cable", cell["inputs"])
        self.assertGreater(cell["watts"], 0.0)

    def test_belt_tiers(self) -> None:
        tiers = self.pdk.belt_tiers()
        self.assertEqual(len(tiers), 3)
        self.assertEqual(tiers[0]["span"], 5)
        self.assertEqual(tiers[-1]["span"], 9)
        self.assertAlmostEqual(tiers[-1]["items_per_lane"], 22.5)

    def test_pipe_span_derived_from_prototype(self) -> None:
        span = self.pdk.pipe_span()
        self.assertEqual(span, 10)
        spans = self.pdk.underground_tier_spans()
        self.assertEqual(spans["pipe-to-ground"], 10)

    def test_tech_json(self) -> None:
        tech = self.pdk.tech_json()
        self.assertIn("M2", tech["layers"])
        self.assertIn("substation", tech["poles"])
        self.assertEqual(tech["poles"]["substation"]["supply"], 9)
        self.assertIn("inserter", tech["inserters"])
        self.assertEqual(tech["inserters"]["inserter"]["reach"], 1)
        self.assertEqual(tech["inserters"]["long-handed-inserter"]["reach"], 2)
        self.assertGreater(
            tech["inserters"]["fast-inserter"]["swings_per_second"],
            tech["inserters"]["inserter"]["swings_per_second"],
        )

    def test_blueprint_round_trip(self) -> None:
        cell = self.pdk.cell("assembling-machine-3", "electronic-circuit")
        assert cell is not None
        entities = [
            {
                "entity_number": 1,
                "name": "assembling-machine-3",
                "position": {"x": 0.0, "y": 0.0},
                "recipe": "electronic-circuit",
            }
        ]
        string = blueprint.encode(entities, "smoke")
        read_back = blueprint.entities_of(string)
        self.assertEqual(read_back[0]["recipe"], "electronic-circuit")
        self.assertTrue(string.startswith("0"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
