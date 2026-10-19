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

    def test_rocket_cargo_cell(self) -> None:
        import placement

        cell = self.pdk.cell("rocket-silo", "space-science-pack")
        assert cell is not None
        self.assertEqual(cell["width"], 20)
        self.assertEqual(cell["height"], 9)
        self.assertEqual(cell["machine_width"], 9)
        self.assertEqual(cell["machine_height"], 9)
        self.assertEqual(cell["fixed_recipe"], "rocket-part")
        self.assertEqual(cell["landing_pad"], "cargo-landing-pad")
        self.assertEqual(cell["landing_pad_width"], 8)
        self.assertEqual(cell["landing_pad_height"], 8)
        self.assertEqual(cell["landing_pad_grid_size"], 2)
        self.assertEqual(
            set(cell["inputs"]),
            {"processing-unit", "low-density-structure", "rocket-fuel", "satellite"},
        )
        self.assertEqual(cell["outputs"], ["space-science-pack"])
        res = placement.place_components(
            {
                "units": 1000,
                "components": [
                    {
                        "name": "u_silo",
                        "macro": "rocket_silo_space_science_pack",
                        "x": 3000,
                        "y": 3000,
                        "orient": "N",
                    }
                ],
            },
            {
                "cells": {"rocket-silo.space-science-pack": cell},
                "instances": [
                    {
                        "name": "u_silo",
                        "cell": "rocket-silo.space-science-pack",
                        "recipe": "space-science-pack",
                    }
                ],
            },
        )
        by_name = {e["name"]: e for e in res.entities}
        self.assertIn("rocket-silo", by_name)
        self.assertIn("cargo-landing-pad", by_name)
        silo_ent = by_name["rocket-silo"]
        pad_ent = by_name["cargo-landing-pad"]
        self.assertEqual(silo_ent.get("recipe"), "rocket-part")
        self.assertTrue(silo_ent.get("launch_to_orbit_automatically"))
        self.assertEqual(int(pad_ent["position"]["x"]) % 2, 0)
        self.assertEqual(int(pad_ent["position"]["y"]) % 2, 0)

    def test_power_plant_cells(self) -> None:
        import placement

        b_cell = self.pdk.cell("boiler", "boiler-steam")
        assert b_cell is not None
        self.assertEqual(b_cell["width"], 4)
        self.assertEqual(b_cell["height"], 5)
        self.assertEqual(b_cell["machine_width"], 2)
        self.assertEqual(b_cell["machine_height"], 3)
        self.assertEqual(b_cell["in_slots"], {"water": 0, "coal": 2})
        self.assertEqual(b_cell["out_slots"], {"steam": 2})
        self.assertEqual(b_cell["internal_pipes"], [{"x": 1.5, "y": 0.5, "fluid": "water"}])

        se_cell = self.pdk.cell("steam-engine", "steam-power")
        assert se_cell is not None
        self.assertEqual(se_cell["width"], 7)
        self.assertEqual(se_cell["height"], 5)
        self.assertEqual(se_cell["machine_width"], 5)
        self.assertEqual(se_cell["machine_height"], 3)
        self.assertEqual(se_cell["in_slots"], {"steam": 2})
        self.assertEqual(se_cell["out_slots"], {})

        res = placement.place_components(
            {
                "units": 1000,
                "components": [
                    {
                        "name": "u_boiler",
                        "macro": "boiler_boiler_steam",
                        "x": 0,
                        "y": 0,
                        "orient": "N",
                    },
                    {
                        "name": "u_se",
                        "macro": "steam_engine_steam_power",
                        "x": 10000,
                        "y": 0,
                        "orient": "N",
                    },
                ],
            },
            {
                "cells": {
                    "boiler.boiler-steam": b_cell,
                    "steam-engine.steam-power": se_cell,
                },
                "fluids": ["water", "steam"],
                "instances": [
                    {
                        "name": "u_boiler",
                        "cell": "boiler.boiler-steam",
                        "recipe": "boiler-steam",
                    },
                    {
                        "name": "u_se",
                        "cell": "steam-engine.steam-power",
                        "recipe": "steam-power",
                    },
                ],
            },
        )
        by_name = {e["name"]: e for e in res.entities}
        self.assertIn("boiler", by_name)
        self.assertIn("steam-engine", by_name)
        self.assertIn("pipe", by_name)
        self.assertNotIn("recipe", by_name["boiler"])
        self.assertNotIn("recipe", by_name["steam-engine"])
        self.assertEqual(by_name["boiler"]["position"], {"x": 2.0, "y": 2.5})
        self.assertEqual(by_name["steam-engine"]["position"], {"x": 13.5, "y": 2.5})
        self.assertEqual(by_name["pipe"]["position"], {"x": 1.5, "y": 0.5})

    def test_lab_four_sided_gaa_cell(self) -> None:
        import lef
        import placement

        cell = self.pdk.cell("lab", "infinite-research")
        assert cell is not None
        self.assertEqual(cell["width"], 5)
        self.assertEqual(cell["height"], 5)
        self.assertEqual(cell["machine_width"], 3)
        self.assertEqual(cell["machine_height"], 3)
        self.assertEqual(len(cell["inputs"]), 7)
        self.assertEqual(cell["outputs"], [])
        self.assertEqual(set(cell["port_faces"].values()), {"W", "N", "S", "E"})
        inserter_coords = set()
        approach_coords = set()
        for port in cell["inputs"]:
            geom = lef.get_macro_port_geometry(cell, port, is_output=False)
            inserter_coords.add(geom["inserter_pos"])
            approach_coords.add(geom["approach_pos"])
        self.assertEqual(len(inserter_coords), 7)
        self.assertEqual(len(approach_coords), 7)
        res = placement.place_components(
            {
                "units": 1000,
                "components": [
                    {
                        "name": "u_lab_1",
                        "macro": "lab_infinite_research",
                        "x": 0,
                        "y": 0,
                        "orient": "N",
                    }
                ],
            },
            {
                "cells": {"lab.infinite-research": cell},
                "instances": [
                    {
                        "name": "u_lab_1",
                        "cell": "lab.infinite-research",
                        "recipe": "infinite-research",
                    }
                ],
            },
        )
        by_name = {e["name"]: e for e in res.entities}
        self.assertIn("lab", by_name)
        self.assertNotIn("recipe", by_name["lab"])
        self.assertEqual(by_name["lab"]["position"], {"x": 2.5, "y": 2.5})

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

    def test_module_docstring_and_inserter_edge_cases(self) -> None:
        self.assertIn("-- placement, routing, optimization --", pdk.__doc__ or "")
        zero_spec = pdk.inserter_spec({"rotation_speed": 0.0, "extension_speed": 0.05})
        self.assertEqual(zero_spec["turn_ticks"], 0.0)
        self.assertEqual(zero_spec["swings_per_second"], 0.0)
        pos_spec = pdk.inserter_spec({"rotation_speed": 0.02, "extension_speed": 0.05})
        self.assertAlmostEqual(pos_spec["turn_ticks"], 25.0)

    def test_circuit_macro_cells(self) -> None:
        for fn, expected_cell, expected_watts in (
            (pdk.display_panel_cell, pdk.DISPLAY_PANEL_CELL, 0.0),
            (pdk.arithmetic_combinator_cell, pdk.ARITHMETIC_COMBINATOR_CELL, 1000.0),
            (pdk.decider_combinator_cell, pdk.DECIDER_COMBINATOR_CELL, 1000.0),
            (pdk.constant_combinator_cell, pdk.CONSTANT_COMBINATOR_CELL, 0.0),
            (pdk.power_switch_cell, pdk.POWER_SWITCH_CELL, 0.0),
        ):
            rec = fn()
            self.assertEqual(rec["cell"], expected_cell)
            self.assertIs(rec["circuit_cell"], True)
            self.assertEqual(rec["watts"], expected_watts)

    def test_compound_cells(self) -> None:
        self.assertIsNone(self.pdk.compound_cell("nonexistent-machine", "gear_belt_combo"))
        self.assertIsNone(self.pdk.compound_cell("assembling-machine-2", "unknown_combo"))

        belt_combo = self.pdk.cell("assembling-machine-2", "gear_belt_combo")
        assert belt_combo is not None
        self.assertEqual(belt_combo["cell"], "assembling-machine-2.gear_belt_combo")
        self.assertEqual(belt_combo["recipe"], "gear_belt_combo")
        self.assertIs(belt_combo["compound"], True)
        self.assertEqual(belt_combo["inputs"], ["iron-plate"])
        self.assertEqual(belt_combo["outputs"], ["transport-belt"])

        ins_combo = self.pdk.cell("assembling-machine-2", "gear_inserter_combo")
        assert ins_combo is not None
        self.assertEqual(ins_combo["cell"], "assembling-machine-2.gear_inserter_combo")
        self.assertEqual(ins_combo["recipe"], "gear_inserter_combo")
        self.assertIs(ins_combo["compound"], False)
        self.assertEqual(ins_combo["inputs"], ["iron-plate", "electronic-circuit"])
        self.assertEqual(ins_combo["outputs"], ["inserter"])

    def test_boiler_fluid_box_connection_offsets_and_fallback(self) -> None:
        import copy

        custom_data = copy.copy(self.data)
        custom_data.prototypes = copy.deepcopy(self.data.prototypes)
        custom_data.machines = copy.deepcopy(self.data.machines)
        boiler_proto = custom_data.machines["boiler"]
        boiler_proto["output_fluid_box"] = {
            "pipe_connections": [{"position": [1.0, 0.0], "direction": 0}]
        }
        custom_pdk = pdk.Pdk(custom_data)
        cell_with_offset = custom_pdk.cell("boiler", "boiler-steam")
        assert cell_with_offset is not None
        self.assertEqual(cell_with_offset["out_slots"], {"steam": 3})

        boiler_proto["output_fluid_box"] = {"pipe_connections": []}
        custom_pdk_no_conns = pdk.Pdk(custom_data)
        cell_fallback = custom_pdk_no_conns.cell("boiler", "boiler-steam")
        assert cell_fallback is not None
        self.assertEqual(cell_fallback["out_slots"], {"steam": 2})

    def test_layer_pairs_and_slot_helpers(self) -> None:
        self.assertEqual(self.pdk.circuit_layers(), ("M9", "M10"))
        stack = self.pdk.layer_stack()
        self.assertIn("M3", stack)
        self.assertIn("M4", stack)
        self.assertEqual(stack["M3"]["direction"], "HORIZONTAL")
        self.assertEqual(stack["M4"]["direction"], "VERTICAL")

        silo_cell = self.pdk.cell("rocket-silo", "space-science-pack")
        assert silo_cell is not None
        self.assertEqual(
            silo_cell["in_slots"],
            {
                "processing-unit": 1,
                "low-density-structure": 3,
                "rocket-fuel": 5,
                "satellite": 7,
            },
        )
        self.assertEqual(silo_cell["out_slots"], {"space-science-pack": 4})

        lab_cell = self.pdk.cell("lab", "infinite-research")
        assert lab_cell is not None
        self.assertEqual(
            lab_cell["in_slots"],
            {
                "automation-science-pack": 1,
                "logistic-science-pack": 1,
                "military-science-pack": 2,
                "chemical-science-pack": 1,
                "production-science-pack": 2,
                "utility-science-pack": 3,
                "space-science-pack": 3,
            },
        )

    def test_fluidbox_orientation_and_port_mapping(self) -> None:
        chem_cell = self.pdk.cell("chemical-plant", "sulfur")
        assert chem_cell is not None
        self.assertEqual(chem_cell["direction"], pdk.DIRECTION_WEST)
        self.assertEqual(chem_cell["in_slots"], {"water": 3, "petroleum-gas": 1})
        self.assertEqual(chem_cell["out_slots"], {"sulfur": 2})

        ref_cell = self.pdk.cell("oil-refinery", "advanced-oil-processing")
        assert ref_cell is not None
        self.assertEqual(ref_cell["direction"], pdk.DIRECTION_EAST)
        self.assertEqual(ref_cell["in_slots"], {"water": 2, "crude-oil": 4})
        self.assertEqual(
            ref_cell["out_slots"],
            {"heavy-oil": 1, "light-oil": 3, "petroleum-gas": 5},
        )

        import copy

        custom_data = copy.copy(self.data)
        custom_data.prototypes = copy.deepcopy(self.data.prototypes)
        custom_data.machines = copy.deepcopy(self.data.machines)
        custom_data.recipes = copy.deepcopy(self.data.recipes)
        chem_proto = custom_data.machines["chemical-plant"]

        # Test tx > 0 -> DIRECTION_SOUTH and tx <= 0 -> DIRECTION_NORTH, plus > span item inputs
        chem_proto["fluid_boxes"] = [
            {"production_type": "input", "pipe_connections": [{"position": [1.0, 0.0]}]},
            {"production_type": "input", "pipe_connections": [{"position": [0.0, -1.0]}]},
            {"production_type": "input", "pipe_connections": [{"position": [0.0, 1.0]}]},
        ]
        custom_data.recipes["sulfur"]["ingredients"] = [
            {"type": "fluid", "name": "water", "amount": 30, "fluidbox_index": 2},
            {"type": "item", "name": "iron-plate", "amount": 1},
            {"type": "item", "name": "copper-plate", "amount": 1},
            {"type": "item", "name": "steel-plate", "amount": 1},
            {"type": "item", "name": "coal", "amount": 1},
        ]
        custom_pdk_s = pdk.Pdk(custom_data)
        south_cell = custom_pdk_s.cell("chemical-plant", "sulfur")
        assert south_cell is not None
        self.assertEqual(south_cell["direction"], pdk.DIRECTION_SOUTH)
        self.assertEqual(south_cell["in_slots"]["water"], 3)

        chem_proto["fluid_boxes"] = [
            {"production_type": "input", "pipe_connections": [{"position": [-1.0, 0.0]}]},
            {"production_type": "input", "pipe_connections": [{"position": [0.0, -1.0]}]},
        ]
        custom_pdk_n = pdk.Pdk(custom_data)
        north_cell = custom_pdk_n.cell("chemical-plant", "sulfur")
        assert north_cell is not None
        self.assertEqual(north_cell["direction"], pdk.DIRECTION_NORTH)
        self.assertEqual(north_cell["in_slots"]["water"], 1)

        # Also test solid machine with more inputs than height (count > span in _center_slots)
        custom_data.recipes["electronic-circuit"]["ingredients"] = [
            {"type": "item", "name": "iron-plate", "amount": 1},
            {"type": "item", "name": "copper-cable", "amount": 1},
            {"type": "item", "name": "steel-plate", "amount": 1},
            {"type": "item", "name": "coal", "amount": 1},
        ]
        overflow_cell = custom_pdk_n.cell("assembling-machine-3", "electronic-circuit")
        assert overflow_cell is not None
        self.assertEqual(
            overflow_cell["in_slots"],
            {"iron-plate": 0, "copper-cable": 1, "steel-plate": 2, "coal": 3},
        )


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
