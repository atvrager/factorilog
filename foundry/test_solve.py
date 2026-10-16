#!/usr/bin/env python3
"""test_solve.py - the solver against the shrunk fixture dump.

Hermetic: no game, no ~/.factorio, no network. The fixture pins the recipe
set, so the LP answer is exact and stable.

usage: test_solve.py <dump-fixture.json>
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solve


class SolveTest(unittest.TestCase):
    data: solve.Data
    allowed: set[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.dump = sys.argv[1] if len(sys.argv) > 1 else "unused"
        cls.data = solve.Data(cls.dump)
        cls.allowed = cls.data.allowed_recipes(["nauvis"])

    def test_fixture_loads(self) -> None:
        self.assertGreater(len(self.data.recipes), 200)
        self.assertIn("electronic-circuit", self.data.recipes)

    def test_machine_pick(self) -> None:
        recipe = self.data.recipes["electronic-circuit"]
        machine, speed = self.data.machine_for(recipe)
        self.assertEqual(machine, "assembling-machine-3")
        self.assertEqual(speed, 1.25)

    def test_belt_capacity(self) -> None:
        belts = self.data.prototypes["transport-belt"]
        self.assertEqual(float(belts["transport-belt"]["speed"]) * 60 * 8, 15.0)
        self.assertEqual(float(belts["express-transport-belt"]["speed"]) * 60 * 8, 45.0)

    def test_underground_span(self) -> None:
        belts = self.data.prototypes["underground-belt"]
        self.assertEqual(int(belts["underground-belt"]["max_distance"]), 5)
        self.assertEqual(int(belts["express-underground-belt"]["max_distance"]), 9)

    def test_iron_gear_rate(self) -> None:
        rates, raw, _surplus = solve.solve_rates(self.data, {"iron-gear-wheel": 60.0}, self.allowed)
        self.assertAlmostEqual(rates["iron-gear-wheel"], 60.0, places=4)
        # Two plates per gear.
        self.assertAlmostEqual(rates["iron-plate"], 120.0, places=3)
        self.assertAlmostEqual(raw["iron-ore"], 120.0, places=3)

    def test_science_plan(self) -> None:
        demand = {
            "automation-science-pack": 60.0,
            "logistic-science-pack": 60.0,
        }
        rates, raw, _surplus = solve.solve_rates(self.data, demand, self.allowed)
        for pack in demand:
            self.assertAlmostEqual(rates[pack], 60.0, places=4)
        _lines, power = solve.machine_counts(self.data, rates)
        self.assertGreater(power, 0.0)
        # Every recipe rate recomputes to a clean balance.
        self.assertEqual(self.balance_gaps(demand, rates, raw), [])

    def test_space_science_recipe(self) -> None:
        self.assertIn("space-science-pack", self.data.recipes)
        recipe = self.data.recipes["space-science-pack"]
        self.assertEqual(recipe.get("fixed_recipe"), "rocket-part")
        self.assertAlmostEqual(solve.Data.craft_seconds(recipe), 300.0)
        machine, speed = self.data.machine_for(recipe)
        self.assertEqual(machine, "rocket-silo")
        self.assertEqual(speed, 1.0)
        rates, raw, _surplus = solve.solve_rates(
            self.data, {"space-science-pack": 60.0}, {"space-science-pack"}
        )
        self.assertAlmostEqual(rates["space-science-pack"], 0.06, places=4)
        self.assertAlmostEqual(raw["processing-unit"], 60.0, places=3)
        self.assertAlmostEqual(raw["low-density-structure"], 60.0, places=3)
        self.assertAlmostEqual(raw["rocket-fuel"], 60.0, places=3)
        self.assertAlmostEqual(raw["satellite"], 0.06, places=4)

    def balance_gaps(
        self,
        demand: dict[str, float],
        rates: dict[str, float],
        raw: dict[str, float],
    ) -> list[tuple[str, float]]:
        """Recompute the item balance from the rates. Return the gaps."""
        prod: dict[str, float] = {}
        cons: dict[str, float] = {}
        for name, rate in rates.items():
            recipe = self.data.recipes[name]
            for entry in solve.Data.results_of(recipe):
                prod[solve.name_of(entry)] = (
                    prod.get(solve.name_of(entry), 0.0) + solve.amount_of(entry) * rate
                )
            for entry in solve.Data.ingredients_of(recipe):
                item = solve.name_of(entry)
                cons[item] = cons.get(item, 0.0) + solve.amount_of(entry) * rate
        gaps = []
        for item in set(prod) | set(cons):
            if item in raw:
                continue
            gap = prod.get(item, 0.0) - cons.get(item, 0.0) - demand.get(item, 0.0)
            if abs(gap) > 1e-6:
                gaps.append((item, gap))
        return gaps


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
