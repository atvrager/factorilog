"""Unit tests for the DEF to Factorio blueprint converter."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blueprint
import def2bp

MINI_DEF = """
VERSION 5.8 ;
DESIGN mini_factory ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 100000 100000 ) ;

COMPONENTS 2 ;
- u_assembler_1 assembling_machine_3__electronic_circuit + PLACED ( 10000 20000 ) N ;
- u_merge_1 belt_merge2 + PLACED ( 30000 40000 ) N ;
END COMPONENTS

NETS 2 ;
- net_wire ( u_assembler_1 electronic_circuit ) ( u_merge_1 a ) + USE SIGNAL
  + ROUTED M2 ( 13000 21500 ) ( * 40500 )
  NEW M1 ( 13000 40500 ) ( 30000 * )
  NEW M2 ( 13000 21500 ) TRANSPORT_BELT_VIA
  NEW M3 ( 35000 40500 ) ( 40000 * ) ;
- crude_oil__1 ( u_assembler_1 crude_oil ) + USE SIGNAL
  + ROUTED M1 ( 13000 22500 ) ( 20000 * )
  NEW M3 ( 20000 22500 ) ( 25000 * )
  NEW M2 ( 20000 22500 ) PIPE_TO_GROUND_VIA ;
END NETS
END DESIGN
"""

MINI_NETLIST = {
    "cells": {
        "assembling-machine-3.electronic-circuit": {
            "machine": "assembling-machine-3",
            "recipe": "electronic-circuit",
            "width": 3,
            "height": 3,
        }
    },
    "instances": [
        {
            "name": "u_assembler_1",
            "cell": "assembling-machine-3.electronic-circuit",
            "recipe": "electronic-circuit",
        },
        {
            "name": "u_merge_1",
            "cell": "belt_merge2",
            "recipe": "electronic-circuit",
        },
    ],
    "fluids": ["crude-oil"],
}


class TestDef2Bp(unittest.TestCase):
    """Test DEF parsing, entity generation, and blueprint encoding."""

    def test_parse_def(self) -> None:
        data = def2bp.parse_def(MINI_DEF)
        self.assertEqual(data["units"], 1000)
        self.assertEqual(len(data["components"]), 2)
        self.assertEqual(data["components"][0]["name"], "u_assembler_1")
        self.assertEqual(data["components"][0]["x"], 10000)
        self.assertEqual(data["components"][0]["y"], 20000)

        self.assertEqual(len(data["nets"]), 2)
        net = data["nets"][0]
        self.assertEqual(net["name"], "net_wire")
        self.assertEqual(len(net["wires"]), 3)
        self.assertEqual(len(net["vias"]), 1)

    def test_build_entities(self) -> None:
        def_data = def2bp.parse_def(MINI_DEF)
        entities, wires = def2bp.build_entities(
            def_data, MINI_NETLIST, strict_drc=False, use_maze_router=False
        )
        self.assertIsInstance(wires, list)

        # Look up by name
        names = {e["name"] for e in entities}
        self.assertIn("assembling-machine-3", names)
        self.assertIn("splitter", names)
        self.assertIn("transport-belt", names)
        self.assertIn("underground-belt", names)
        self.assertIn("pipe-to-ground", names)
        self.assertIn("pipe", names)

        # Check machine coordinates (origin 10, 20 + 1.5, 1.5 -> 11.5, 21.5)
        mach = next(e for e in entities if e["name"] == "assembling-machine-3")
        self.assertEqual(mach["position"], {"x": 11.5, "y": 21.5})
        self.assertEqual(mach["recipe"], "electronic-circuit")
        self.assertNotIn("power_domain", mach)

        # Check splitter coordinates (origin 30, 40 + 0.5, 1.0 -> 30.5, 41.0)
        spl = next(e for e in entities if e["name"] == "splitter")
        self.assertEqual(spl["position"], {"x": 30.5, "y": 41.0})
        self.assertEqual(spl["direction"], def2bp.DIRECTION_EAST)

        # Verify 3x3 belt_merge2 opposing side-load lane balancer belts
        belt_by_pos = {
            (round(e["position"]["x"], 2), round(e["position"]["y"], 2)): e
            for e in entities
            if e["name"] == "transport-belt"
        }
        self.assertEqual(belt_by_pos[(32.5, 40.5)]["direction"], def2bp.DIRECTION_SOUTH)
        self.assertEqual(belt_by_pos[(32.5, 42.5)]["direction"], def2bp.DIRECTION_NORTH)
        self.assertEqual(belt_by_pos[(32.5, 41.5)]["direction"], def2bp.DIRECTION_EAST)

        # Check entity numbering is 1-based and sequential
        for idx, ent in enumerate(entities, 1):
            self.assertEqual(ent["entity_number"], idx)

    def test_def_to_blueprint_roundtrip(self) -> None:
        bp_string = def2bp.def_to_blueprint(
            MINI_DEF, MINI_NETLIST, label="test_rig", strict_drc=False
        )
        self.assertTrue(bp_string.startswith("0"))

        doc = blueprint.decode(bp_string)
        self.assertEqual(doc["blueprint"]["item"], "blueprint")
        self.assertEqual(doc["blueprint"]["label"], "test_rig")
        self.assertGreater(len(doc["blueprint"]["entities"]), 0)

    def test_factory_def_full(self) -> None:
        foundry_dir = Path(__file__).resolve().parent
        factory_def_path = foundry_dir / "factory.def"
        netlist_json_path = foundry_dir / "netlist.json"
        if not factory_def_path.exists() or not netlist_json_path.exists():
            self.skipTest("factory.def or netlist.json missing")

        def_text = factory_def_path.read_text()
        netlist_data = json.loads(netlist_json_path.read_text())

        bp_string = def2bp.def_to_blueprint(def_text, netlist_data, label="full_factory")
        self.assertTrue(bp_string.startswith("0"))
        entities = blueprint.entities_of(bp_string)
        self.assertGreater(len(entities), 1000)

    def test_extract_pins(self) -> None:
        foundry_dir = Path(__file__).resolve().parent
        factory_def_path = foundry_dir / "factory.def"
        netlist_json_path = foundry_dir / "netlist.json"
        if not factory_def_path.exists() or not netlist_json_path.exists():
            self.skipTest("factory.def or netlist.json missing")

        def_text = factory_def_path.read_text()
        netlist_data = json.loads(netlist_json_path.read_text())
        def_data = def2bp.parse_def(def_text)
        pins = def2bp.extract_pins(def_data, netlist_data)

        self.assertIn("inputs", pins)
        self.assertIn("outputs", pins)
        self.assertEqual(len(pins["inputs"]), 6)
        self.assertEqual(len(pins["outputs"]), 6)

        input_names = [p["name"] for p in pins["inputs"]]
        self.assertIn("coal", input_names)
        self.assertIn("copper-ore", input_names)
        self.assertIn("iron-ore", input_names)
        self.assertIn("crude-oil", input_names)

        crude = next(p for p in pins["inputs"] if p["name"] == "crude-oil")
        self.assertEqual(crude["type"], "pipe")

        coal = next(p for p in pins["inputs"] if p["name"] == "coal")
        self.assertEqual(coal["type"], "belt")

    def test_place_components_belt_merge2_orientations(self) -> None:
        from placement import place_components

        expected_layouts = {
            "W": {
                (1.0, 2.5): ("splitter", def2bp.DIRECTION_NORTH),
                (0.5, 1.5): ("transport-belt", def2bp.DIRECTION_NORTH),
                (0.5, 0.5): ("transport-belt", def2bp.DIRECTION_EAST),
                (1.5, 1.5): ("transport-belt", def2bp.DIRECTION_EAST),
                (2.5, 1.5): ("transport-belt", def2bp.DIRECTION_NORTH),
                (2.5, 0.5): ("transport-belt", def2bp.DIRECTION_WEST),
                (1.5, 0.5): ("transport-belt", def2bp.DIRECTION_NORTH),
            },
            "E": {
                (1.0, 0.5): ("splitter", def2bp.DIRECTION_SOUTH),
                (0.5, 1.5): ("transport-belt", def2bp.DIRECTION_SOUTH),
                (0.5, 2.5): ("transport-belt", def2bp.DIRECTION_EAST),
                (1.5, 1.5): ("transport-belt", def2bp.DIRECTION_EAST),
                (2.5, 1.5): ("transport-belt", def2bp.DIRECTION_SOUTH),
                (2.5, 2.5): ("transport-belt", def2bp.DIRECTION_WEST),
                (1.5, 2.5): ("transport-belt", def2bp.DIRECTION_SOUTH),
            },
            "S": {
                (2.5, 1.0): ("splitter", def2bp.DIRECTION_WEST),
                (1.5, 0.5): ("transport-belt", def2bp.DIRECTION_WEST),
                (0.5, 0.5): ("transport-belt", def2bp.DIRECTION_SOUTH),
                (1.5, 1.5): ("transport-belt", def2bp.DIRECTION_SOUTH),
                (1.5, 2.5): ("transport-belt", def2bp.DIRECTION_WEST),
                (0.5, 2.5): ("transport-belt", def2bp.DIRECTION_NORTH),
                (0.5, 1.5): ("transport-belt", def2bp.DIRECTION_WEST),
            },
            "N": {
                (0.5, 1.0): ("splitter", def2bp.DIRECTION_EAST),
                (1.5, 0.5): ("transport-belt", def2bp.DIRECTION_EAST),
                (2.5, 0.5): ("transport-belt", def2bp.DIRECTION_SOUTH),
                (1.5, 1.5): ("transport-belt", def2bp.DIRECTION_SOUTH),
                (1.5, 2.5): ("transport-belt", def2bp.DIRECTION_EAST),
                (2.5, 2.5): ("transport-belt", def2bp.DIRECTION_NORTH),
                (2.5, 1.5): ("transport-belt", def2bp.DIRECTION_EAST),
            },
        }
        for orient, layout in expected_layouts.items():
            res = place_components(
                {
                    "units": 1000,
                    "components": [
                        {
                            "name": f"m_{orient}",
                            "macro": "belt_merge2",
                            "x": 10000,
                            "y": 20000,
                            "orient": orient,
                        }
                    ],
                },
                {},
            )
            self.assertEqual(len(res.entities), 7)
            actual = {
                (round(e["position"]["x"] - 10.0, 2), round(e["position"]["y"] - 20.0, 2)): (
                    e["name"],
                    e["direction"],
                )
                for e in res.entities
            }
            self.assertEqual(actual, layout)
            for tx in range(3):
                for ty in range(3):
                    self.assertIn((10.5 + tx, 20.5 + ty), res.occupied_positions)

    def test_place_components_splitters_pumps_and_controls(self) -> None:
        from placement import place_components

        def_data = {
            "units": 1000,
            "components": [
                {"name": "sp_w", "macro": "belt_split2", "x": 0, "y": 0, "orient": "W"},
                {"name": "sp_e", "macro": "belt_balancer2", "x": 10000, "y": 0, "orient": "E"},
                {"name": "sp_s", "macro": "belt_split2", "x": 20000, "y": 0, "orient": "S"},
                {"name": "sp_n", "macro": "belt_split2", "x": 30000, "y": 0, "orient": "N"},
                {"name": "pm_w", "macro": "pump", "x": 0, "y": 10000, "orient": "W"},
                {"name": "pm_e", "macro": "pump", "x": 10000, "y": 10000, "orient": "E"},
                {"name": "pm_s", "macro": "pump", "x": 20000, "y": 10000, "orient": "S"},
                {"name": "pm_n", "macro": "pump", "x": 30000, "y": 10000, "orient": "N"},
                {"name": "dp_1", "macro": "display_panel", "x": 40000, "y": 0},
                {"name": "ac_fluid", "macro": "arithmetic_combinator", "x": 50000, "y": 0},
                {"name": "ac_default", "macro": "arithmetic_combinator", "x": 60000, "y": 0},
                {"name": "dc_1", "macro": "decider_combinator", "x": 70000, "y": 0},
                {"name": "cc_1", "macro": "constant_combinator", "x": 80000, "y": 0},
                {"name": "ps_1", "macro": "power_switch", "x": 90000, "y": 0},
            ],
        }
        netlist_data = {
            "fluids": ["water"],
            "instances": [
                {"name": "dp_1", "recipe": "iron-plate"},
                {"name": "ac_fluid", "recipe": "water"},
                {"name": "cc_1", "control_behavior": {"sections": []}},
            ],
        }
        res = place_components(def_data, netlist_data)
        self.assertTrue(res.has_fluid_metadata)
        self.assertIn("water", res.fluids)

        # Splitters
        self.assertEqual(res.inst_to_entity["sp_w"]["position"], {"x": 1.0, "y": 0.5})
        self.assertEqual(res.inst_to_entity["sp_w"]["direction"], def2bp.DIRECTION_NORTH)
        self.assertIn((0.5, 0.5), res.occupied_positions)
        self.assertIn((1.5, 0.5), res.occupied_positions)

        self.assertEqual(res.inst_to_entity["sp_e"]["position"], {"x": 11.0, "y": 0.5})
        self.assertEqual(res.inst_to_entity["sp_e"]["direction"], def2bp.DIRECTION_SOUTH)
        self.assertIn((10.5, 0.5), res.occupied_positions)
        self.assertIn((11.5, 0.5), res.occupied_positions)

        self.assertEqual(res.inst_to_entity["sp_s"]["position"], {"x": 20.5, "y": 1.0})
        self.assertEqual(res.inst_to_entity["sp_s"]["direction"], def2bp.DIRECTION_WEST)
        self.assertIn((20.5, 0.5), res.occupied_positions)
        self.assertIn((20.5, 1.5), res.occupied_positions)

        self.assertEqual(res.inst_to_entity["sp_n"]["position"], {"x": 30.5, "y": 1.0})
        self.assertEqual(res.inst_to_entity["sp_n"]["direction"], def2bp.DIRECTION_EAST)
        self.assertIn((30.5, 0.5), res.occupied_positions)
        self.assertIn((30.5, 1.5), res.occupied_positions)

        # Pumps
        self.assertEqual(res.inst_to_entity["pm_w"]["position"], {"x": 1.0, "y": 10.5})
        self.assertEqual(res.inst_to_entity["pm_w"]["direction"], def2bp.DIRECTION_WEST)
        self.assertIn((0.5, 10.5), res.occupied_positions)
        self.assertIn((1.5, 10.5), res.occupied_positions)

        self.assertEqual(res.inst_to_entity["pm_e"]["position"], {"x": 11.0, "y": 10.5})
        self.assertEqual(res.inst_to_entity["pm_e"]["direction"], def2bp.DIRECTION_EAST)

        self.assertEqual(res.inst_to_entity["pm_s"]["position"], {"x": 20.5, "y": 11.0})
        self.assertEqual(res.inst_to_entity["pm_s"]["direction"], def2bp.DIRECTION_SOUTH)
        self.assertIn((20.5, 10.5), res.occupied_positions)
        self.assertIn((20.5, 11.5), res.occupied_positions)

        self.assertEqual(res.inst_to_entity["pm_n"]["position"], {"x": 30.5, "y": 11.0})
        self.assertEqual(res.inst_to_entity["pm_n"]["direction"], def2bp.DIRECTION_NORTH)

        # Display panel
        self.assertEqual(res.inst_to_entity["dp_1"]["position"], {"x": 40.5, "y": 0.5})
        self.assertEqual(
            res.inst_to_entity["dp_1"]["custom_icon"], {"type": "item", "name": "iron-plate"}
        )
        self.assertIn((40.5, 0.5), res.occupied_positions)

        # Arithmetic combinators
        self.assertEqual(res.inst_to_entity["ac_fluid"]["position"], {"x": 50.5, "y": 1.0})
        self.assertEqual(
            res.inst_to_entity["ac_fluid"]["control_behavior"]["arithmetic_conditions"][
                "first_signal"
            ],
            {"type": "fluid", "name": "water"},
        )
        self.assertIn((50.5, 0.5), res.occupied_positions)
        self.assertIn((50.5, 1.5), res.occupied_positions)

        self.assertEqual(
            res.inst_to_entity["ac_default"]["control_behavior"]["arithmetic_conditions"][
                "first_signal"
            ],
            {"type": "virtual", "name": "signal-each"},
        )

        # Decider combinator
        dc_ent = res.inst_to_entity["dc_1"]
        self.assertEqual(dc_ent["position"], {"x": 70.5, "y": 1.0})
        self.assertEqual(dc_ent["control_behavior"]["decider_conditions"]["comparator"], ">")
        self.assertIn((70.5, 0.5), res.occupied_positions)
        self.assertIn((70.5, 1.5), res.occupied_positions)
        self.assertTrue(all(x >= 0.0 and y >= 0.0 for x, y in res.occupied_positions))
        self.assertNotIn((70.0, 0.5), res.occupied_positions)
        self.assertNotIn((69.5, 0.5), res.occupied_positions)
        self.assertNotIn((68.5, 0.5), res.occupied_positions)

        # Constant combinator
        self.assertEqual(res.inst_to_entity["cc_1"]["position"], {"x": 80.5, "y": 0.5})
        self.assertIn((80.5, 0.5), res.occupied_positions)

        # Power switch
        ps_ent = res.inst_to_entity["ps_1"]
        self.assertEqual(ps_ent["position"], {"x": 91.0, "y": 1.0})
        self.assertIs(ps_ent["switch_state"], False)
        self.assertIs(ps_ent["control_behavior"]["circuit_enabled"], True)
        self.assertEqual(ps_ent["control_behavior"]["circuit_condition"]["constant"], 30)
        for tx in range(2):
            for ty in range(2):
                self.assertIn((90.5 + tx, 0.5 + ty), res.occupied_positions)

        # Total occupied positions: 8*(2+1) + 1 + 2*(2+1) + 1*(2+1) + 1 + (4+1) = 40
        self.assertEqual(len(res.occupied_positions), 40)

    def test_place_components_inserter_selection_and_landing_pad(self) -> None:
        from placement import PlacementConfig, place_components

        tiers = [
            {
                "name": "inserter",
                "max_stack": 3,
                "rates_by_stack": {"1": 50.0, "2": 100.0, "3": 150.0},
                "out_rates_by_stack": {"1": 60.0, "2": 120.0, "3": 180.0},
            },
            {
                "name": "fast-inserter",
                "max_stack": 3,
                "rates_by_stack": {"1": 200.0, "2": 300.0, "3": 450.0},
            },
        ]
        def_data = {
            "units": 1000,
            "components": [
                {"name": "u_low_rate", "macro": "asm", "x": 0, "y": 0},
                {"name": "u_zero_rate", "macro": "asm", "x": 20000, "y": 0},
                {"name": "u_high_rate", "macro": "asm", "x": 40000, "y": 0},
                {"name": "u_silo", "macro": "silo", "x": 60000, "y": 0},
                {"name": "u_boiler", "macro": "boiler_cell", "x": 100000, "y": 0},
            ],
        }
        netlist_data = {
            "fluids": ["water"],
            "cells": {
                "asm_cell": {
                    "machine": "assembling-machine-3",
                    "width": 5,
                    "height": 3,
                    "machine_width": 3,
                    "machine_height": 3,
                    "inputs": ["iron-plate", "water"],
                    "outputs": ["iron-gear-wheel"],
                    "in_amounts": {"iron-plate": 2.0},
                    "out_amounts": {"iron-gear-wheel": 3.0},
                    "inserter_tiers": tiers,
                    "internal_pipes": [{"x": 0.5, "y": 2.5, "fluid": "water"}],
                    "unused_fluid_ports": [[1.5, 2.5]],
                },
                "silo_cell": {
                    "machine": "rocket-silo",
                    "width": 20,
                    "height": 9,
                    "machine_width": 9,
                    "machine_height": 9,
                    "inputs": ["rocket-part"],
                    "outputs": ["space-science-pack"],
                    "landing_pad": "cargo-landing-pad",
                    "landing_pad_width": 8,
                    "landing_pad_height": 8,
                    "landing_pad_grid_size": 2,
                    "inserter_tiers": tiers,
                },
                "boiler_cell": {
                    "machine": "boiler",
                    "width": 3,
                    "height": 2,
                    "machine_width": 3,
                    "machine_height": 2,
                    "inputs": ["coal"],
                    "outputs": [],
                    "unused_fluid_ports": [[0.5, 1.5]],
                },
            },
            "instances": [
                {
                    "name": "u_low_rate",
                    "cell": "asm_cell",
                    "recipe": "iron-gear-wheel",
                    "plan_rate_per_min": 30.0,
                    "rated_per_min": 200.0,
                },
                {
                    "name": "u_zero_rate",
                    "cell": "asm_cell",
                    "recipe": "iron-gear-wheel",
                    "plan_rate_per_min": 0.0,
                    "rated_per_min": 200.0,
                },
                {
                    "name": "u_high_rate",
                    "cell": "asm_cell",
                    "recipe": "iron-gear-wheel",
                    "plan_rate_per_min": 70.0,
                    "rated_per_min": 200.0,
                },
                {
                    "name": "u_silo",
                    "cell": "silo_cell",
                    "recipe": "rocket-part",
                    "plan_rate_per_min": 10.0,
                    "rated_per_min": 200.0,
                },
                {
                    "name": "u_boiler",
                    "cell": "boiler_cell",
                    "recipe": "steam",
                },
            ],
        }
        res = place_components(def_data, netlist_data)

        # Verify all occupied positions have non-negative x and y
        self.assertTrue(all(x >= 0.0 and y >= 0.0 for x, y in res.occupied_positions))
        self.assertNotIn((99.5, 1.5), res.occupied_positions)

        # u_low_rate: machine position is gx(0) + in_offset(1.0) + m_w(3)/2.0 = 2.5, gy(0) + 1.5
        self.assertEqual(res.inst_to_entity["u_low_rate"]["position"], {"x": 2.5, "y": 1.5})

        # u_low_rate: plan_r=30 < 0.35*200=70.
        # Input iron-plate req_rate = 1.4 * 30 * 2 = 84.0 -> inserter stack 2 (rate 100.0 < max_stack 3 -> override_stack_size=2)
        in_ins_low = res.grid_entities[(0.5, 0.5)]
        self.assertEqual(in_ins_low["name"], "inserter")
        self.assertEqual(in_ins_low.get("override_stack_size"), 2)
        self.assertIn((0.5, 0.5), res.occupied_positions)

        # Output iron-gear-wheel req_rate = 1.4 * 30 * 3 = 126.0 -> inserter stack 3 (rate 180.0 == max_stack 3 -> no override_stack_size)
        out_ins_low = res.grid_entities[(4.5, 1.5)]
        self.assertEqual(out_ins_low["name"], "inserter")
        self.assertNotIn("override_stack_size", out_ins_low)
        self.assertIn((4.5, 1.5), res.occupied_positions)

        # Internal pipe & unused fluid port on u_low_rate
        pipe_ent = res.grid_entities[(0.5, 2.5)]
        self.assertEqual(pipe_ent["name"], "pipe")
        self.assertEqual(pipe_ent["fluid"], "water")
        self.assertIn((1.5, 2.5), res.occupied_positions)

        # u_zero_rate: plan_r=0.0 <= 0.0 -> default fast-inserter with no override
        in_ins_zero = res.grid_entities[(20.5, 0.5)]
        self.assertEqual(in_ins_zero["name"], "fast-inserter")
        self.assertNotIn("override_stack_size", in_ins_zero)

        # u_high_rate: plan_r=70.0 >= 0.35*200=70.0 -> default fast-inserter
        in_ins_high = res.grid_entities[(40.5, 0.5)]
        self.assertEqual(in_ins_high["name"], "fast-inserter")
        self.assertNotIn("override_stack_size", in_ins_high)

        # u_silo with landing pad:
        # gx=60, in_offset=1, m_w=9 -> pad_left = 70 (even, 70+4=74 % 2 == 0), pad_w=8 -> pad_cx=74.0, pad_cy=4.0, pad_right=78
        silo_ent = res.inst_to_entity["u_silo"]
        self.assertEqual(silo_ent["position"], {"x": 65.5, "y": 4.5})
        self.assertTrue(silo_ent.get("launch_to_orbit_automatically"))
        pad_ent = res.grid_entities[(74.0, 4.0)]
        self.assertEqual(pad_ent["name"], "cargo-landing-pad")
        # Output inserter at ins_x = pad_right + 0.5 = 78.5, out_x = 60 + 19.5 = 79.5, ins_y = 4.5
        pad_ins = res.grid_entities[(78.5, 4.5)]
        self.assertEqual(pad_ins["name"], "fast-inserter")
        self.assertIn((78.5, 4.5), res.occupied_positions)
        # Because ins_x (78.5) < out_x (79.5), place transport-belt at (79.5, 4.5)
        pad_belt = res.grid_entities[(79.5, 4.5)]
        self.assertEqual(pad_belt["name"], "transport-belt")
        self.assertIn((79.5, 4.5), res.occupied_positions)

        # u_boiler: boiler has power_domain="aon" on its input inserter and no recipe on boiler entity
        boiler_ent = res.inst_to_entity["u_boiler"]
        self.assertNotIn("recipe", boiler_ent)
        boiler_ins = res.grid_entities[(100.5, 0.5)]
        self.assertEqual(boiler_ins.get("power_domain"), "aon")
        self.assertIn((100.5, 1.5), res.occupied_positions)

        # Verify custom PlacementConfig overrides
        custom_cfg = PlacementConfig(default_power_switch_threshold=75)
        ps_custom = place_components(
            {"components": [{"name": "ps_c", "macro": "power_switch", "x": 0, "y": 0}]},
            {},
            config=custom_cfg,
        )
        self.assertEqual(
            ps_custom.inst_to_entity["ps_c"]["control_behavior"]["circuit_condition"]["constant"],
            75,
        )

    def test_entity_helpers_directions_and_consumers(self) -> None:
        import entity_helpers as eh

        self.assertEqual(eh._segment_direction(5, 1, 5, 4), eh.DIRECTION_SOUTH)
        self.assertEqual(eh._segment_direction(5, 4, 5, 1), eh.DIRECTION_NORTH)
        self.assertEqual(eh._segment_direction(1, 5, 4, 5), eh.DIRECTION_EAST)
        self.assertEqual(eh._segment_direction(4, 5, 1, 5), eh.DIRECTION_WEST)

        self.assertFalse(eh.is_electric_consumer(""))
        self.assertFalse(eh.is_electric_consumer("burner-inserter"))
        self.assertFalse(eh.is_electric_consumer("constant-combinator"))
        self.assertFalse(eh.is_electric_consumer("display-panel"))
        self.assertFalse(eh.is_electric_consumer("transport-belt"))
        self.assertTrue(eh.is_electric_consumer("fast-inserter"))
        self.assertTrue(eh.is_electric_consumer("assembling-machine-3"))
        self.assertTrue(eh.is_electric_pole("medium-electric-pole"))
        self.assertTrue(eh.is_electric_pole("substation"))
        self.assertFalse(eh.is_electric_pole("transport-belt"))

        self.assertEqual(eh.get_pole_wire_reach("medium-electric-pole"), 9.0)
        self.assertEqual(eh.get_pole_wire_reach("unknown-pole", default=12.5), 12.5)
        self.assertEqual(
            eh.get_compatible_underground_belt(4, "underground-belt"), "underground-belt"
        )
        self.assertEqual(
            eh.get_compatible_underground_belt(6, "underground-belt"), "fast-underground-belt"
        )
        self.assertEqual(eh.get_compatible_underground_belt(8), "express-underground-belt")
        self.assertEqual(eh.get_compatible_underground_belt(99, "custom-ug"), "custom-ug")

    def test_entity_helpers_prototypes_and_sizes(self) -> None:
        import entity_helpers as eh

        protos = {
            "custom-ug-belt": {
                "collision_box": [[-0.4, -0.4], [0.4, 0.4]],
                "max_distance": 11,
            },
            "custom-pipe-ground": {
                "fluid_box": {
                    "pipe_connections": [
                        {"connection_type": "normal", "max_underground_distance": 99},
                        {"connection_type": "underground", "max_underground_distance": 14},
                    ]
                }
            },
            "custom-pole": {
                "collision_box": [[-0.65, -0.65], [0.65, 0.65]],
                "supply_area_distance": 4.5,
                "maximum_wire_distance": 11.5,
            },
            "nested-table": {
                "nested-machine": {
                    "collision_box": [[-1.4, -2.4], [1.4, 2.4]],
                    "max_distance": 6,
                    "supply_area_distance": 5.5,
                    "maximum_wire_distance": 13.5,
                }
            },
        }
        eh.register_prototype_boxes(protos)
        self.assertEqual(eh.get_underground_span("custom-ug-belt"), 11)
        self.assertEqual(eh.get_underground_span("custom-pipe-ground"), 14)
        self.assertEqual(eh.get_pole_supply_radius("custom-pole"), 4.5)
        self.assertEqual(eh.get_pole_wire_reach("custom-pole"), 11.5)
        self.assertEqual(eh.entity_size("custom-pole"), (2.0, 2.0))
        self.assertEqual(eh.entity_collision_half_size("custom-pole"), (0.65, 0.65))
        self.assertEqual(eh.entity_size("nested-machine", eh.DIRECTION_NORTH), (3.0, 5.0))
        self.assertEqual(eh.entity_size("nested-machine", eh.DIRECTION_EAST), (5.0, 3.0))
        self.assertEqual(
            eh.entity_collision_half_size("nested-machine", eh.DIRECTION_NORTH), (1.4, 2.4)
        )
        self.assertEqual(
            eh.entity_collision_half_size("nested-machine", eh.DIRECTION_WEST), (2.4, 1.4)
        )

        # Fallback entity_size and entity_collision_half_size checks
        self.assertEqual(eh.entity_size("pump", eh.DIRECTION_NORTH), (1.0, 2.0))
        self.assertEqual(eh.entity_size("pump", eh.DIRECTION_EAST), (2.0, 1.0))
        self.assertEqual(eh.entity_size("custom-pump", eh.DIRECTION_NORTH), (1.0, 2.0))
        self.assertEqual(eh.entity_size("custom-pump", eh.DIRECTION_WEST), (2.0, 1.0))
        self.assertEqual(eh.entity_size("rocket-silo"), (9.0, 9.0))
        self.assertEqual(eh.entity_size("oil-refinery"), (5.0, 5.0))
        self.assertEqual(eh.entity_size("steam-engine", eh.DIRECTION_NORTH), (3.0, 5.0))
        self.assertEqual(eh.entity_size("steam-engine", eh.DIRECTION_EAST), (5.0, 3.0))
        self.assertEqual(eh.entity_size("boiler", eh.DIRECTION_NORTH), (3.0, 2.0))
        self.assertEqual(eh.entity_size("boiler", eh.DIRECTION_EAST), (2.0, 3.0))
        self.assertEqual(eh.entity_size("assembling-machine-3"), (3.0, 3.0))
        self.assertEqual(eh.entity_size("steel-furnace"), (2.0, 2.0))
        self.assertEqual(eh.entity_size("transport-belt"), (1.0, 1.0))

        self.assertEqual(eh.entity_collision_half_size("pump", eh.DIRECTION_NORTH), (0.29, 0.9))
        self.assertEqual(eh.entity_collision_half_size("pump", eh.DIRECTION_EAST), (0.9, 0.29))
        self.assertEqual(
            eh.entity_collision_half_size("custom-pump", eh.DIRECTION_NORTH), (0.29, 0.9)
        )
        self.assertEqual(
            eh.entity_collision_half_size("custom-pump", eh.DIRECTION_EAST), (0.9, 0.29)
        )
        self.assertEqual(eh.entity_collision_half_size("rocket-silo"), (4.2, 4.2))
        self.assertEqual(eh.entity_collision_half_size("oil-refinery"), (2.2, 2.2))
        self.assertEqual(
            eh.entity_collision_half_size("steam-engine", eh.DIRECTION_NORTH), (1.25, 2.35)
        )
        self.assertEqual(
            eh.entity_collision_half_size("steam-engine", eh.DIRECTION_EAST), (2.35, 1.25)
        )
        self.assertEqual(eh.entity_collision_half_size("boiler", eh.DIRECTION_NORTH), (1.29, 0.79))
        self.assertEqual(eh.entity_collision_half_size("boiler", eh.DIRECTION_EAST), (0.79, 1.29))
        self.assertEqual(eh.entity_collision_half_size("assembling-machine-3"), (1.2, 1.2))
        self.assertEqual(eh.entity_collision_half_size("steel-furnace"), (0.7, 0.7))
        self.assertEqual(eh.entity_collision_half_size("transport-belt"), (0.15, 0.15))

    def test_entity_helpers_power_collisions_and_underground(self) -> None:
        import entity_helpers as eh

        # check_power across bucket boundaries (bucket size 16)
        consumer_near = {
            "name": "assembling-machine-3",
            "position": {"x": 14.5, "y": 14.5},
            "direction": eh.DIRECTION_NORTH,
        }
        consumer_far = {
            "name": "assembling-machine-3",
            "position": {"x": 50.0, "y": 50.0},
            "direction": eh.DIRECTION_NORTH,
        }
        # Place pole at (19.0, 19.0) in the next bucket so box edge distance is 3.3 <= 3.5.
        pole = {
            "name": "medium-electric-pole",
            "position": {"x": 19.0, "y": 19.0},
        }
        self.assertEqual(
            eh.check_power([consumer_near, consumer_far]), [consumer_near, consumer_far]
        )
        self.assertEqual(eh.check_power([pole, consumer_near, consumer_far]), [consumer_far])

        # check_collisions for splitters and pumps in N/S and E/W orientations
        ents = [
            {"name": "splitter", "position": {"x": 5.0, "y": 5.5}, "direction": eh.DIRECTION_NORTH},
            {"name": "transport-belt", "position": {"x": 4.5, "y": 5.5}},
            {"name": "transport-belt", "position": {"x": 5.5, "y": 5.5}},
            {
                "name": "splitter",
                "position": {"x": 10.5, "y": 10.0},
                "direction": eh.DIRECTION_EAST,
            },
            {"name": "transport-belt", "position": {"x": 10.5, "y": 9.5}},
            {"name": "transport-belt", "position": {"x": 10.5, "y": 10.5}},
            {"name": "pump", "position": {"x": 20.5, "y": 20.0}, "direction": eh.DIRECTION_NORTH},
            {"name": "pipe", "position": {"x": 20.5, "y": 19.5}},
            {"name": "pipe", "position": {"x": 20.5, "y": 20.5}},
            {"name": "pump", "position": {"x": 30.0, "y": 30.5}, "direction": eh.DIRECTION_EAST},
            {"name": "pipe", "position": {"x": 29.5, "y": 30.5}},
            {"name": "pipe", "position": {"x": 30.5, "y": 30.5}},
        ]
        cols = eh.check_collisions(ents)
        self.assertEqual(len(cols), 8)
        self.assertEqual(
            eh.check_collisions(
                [
                    {
                        "name": "splitter",
                        "position": {"x": 5.0, "y": 5.5},
                        "direction": eh.DIRECTION_NORTH,
                    }
                ]
            ),
            [],
        )
        self.assertEqual(
            eh.check_collisions(
                [
                    {
                        "name": "pump",
                        "position": {"x": 20.5, "y": 20.0},
                        "direction": eh.DIRECTION_NORTH,
                    }
                ]
            ),
            [],
        )

        # check_underground_pairing for all 4 directions (E, W, S, N) and pipe-to-ground
        ug_ents = [
            # E pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 1.5, "y": 1.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 4.5, "y": 1.5},
            },
            # W pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 10.5, "y": 2.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 7.5, "y": 2.5},
            },
            # S pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_SOUTH,
                "position": {"x": 3.5, "y": 1.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_SOUTH,
                "position": {"x": 3.5, "y": 4.5},
            },
            # N pair
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_NORTH,
                "position": {"x": 5.5, "y": 10.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_NORTH,
                "position": {"x": 5.5, "y": 7.5},
            },
            # Pipe-to-ground pair
            {
                "name": "pipe-to-ground",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 20.5, "y": 1.5},
            },
            {
                "name": "pipe-to-ground",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 25.5, "y": 1.5},
            },
        ]
        self.assertEqual(eh.check_underground_pairing(ug_ents), [])
        # Break W pair by moving output to wrong direction or span
        bad_w = [
            {
                "name": "underground-belt",
                "type": "input",
                "direction": eh.DIRECTION_WEST,
                "position": {"x": 10.5, "y": 2.5},
            },
            {
                "name": "underground-belt",
                "type": "output",
                "direction": eh.DIRECTION_EAST,
                "position": {"x": 7.5, "y": 2.5},
            },
        ]
        self.assertEqual(len(eh.check_underground_pairing(bad_w)), 2)

    def test_prune_dead_end_belts_all_sinks(self) -> None:
        import entity_helpers as eh

        # Test output pins, splitters in E/W/N/S, underground input, inserter pickup, and opposing belt head-on
        entities = [
            # Belt on output pin (0.5, 2.5) -> kept
            {
                "name": "transport-belt",
                "position": {"x": 0.5, "y": 2.5},
                "direction": eh.DIRECTION_EAST,
            },
            # Splitters in E, W, N, S and belts feeding both halves of each splitter -> kept
            {"name": "splitter", "position": {"x": 10.5, "y": 5.0}, "direction": eh.DIRECTION_EAST},
            {
                "name": "transport-belt",
                "position": {"x": 9.5, "y": 4.5},
                "direction": eh.DIRECTION_EAST,
            },
            {
                "name": "transport-belt",
                "position": {"x": 9.5, "y": 5.5},
                "direction": eh.DIRECTION_EAST,
            },
            {"name": "splitter", "position": {"x": 20.5, "y": 5.0}, "direction": eh.DIRECTION_WEST},
            {
                "name": "transport-belt",
                "position": {"x": 21.5, "y": 4.5},
                "direction": eh.DIRECTION_WEST,
            },
            {
                "name": "transport-belt",
                "position": {"x": 21.5, "y": 5.5},
                "direction": eh.DIRECTION_WEST,
            },
            {
                "name": "splitter",
                "position": {"x": 30.0, "y": 5.5},
                "direction": eh.DIRECTION_NORTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 29.5, "y": 6.5},
                "direction": eh.DIRECTION_NORTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 30.5, "y": 6.5},
                "direction": eh.DIRECTION_NORTH,
            },
            {
                "name": "splitter",
                "position": {"x": 40.0, "y": 5.5},
                "direction": eh.DIRECTION_SOUTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 39.5, "y": 4.5},
                "direction": eh.DIRECTION_SOUTH,
            },
            {
                "name": "transport-belt",
                "position": {"x": 40.5, "y": 4.5},
                "direction": eh.DIRECTION_SOUTH,
            },
            # Inserter at (50.5, 5.5) facing EAST -> pickup at (51.5, 5.5) -> belt at (51.5, 5.5) kept
            {"name": "inserter", "position": {"x": 50.5, "y": 5.5}, "direction": eh.DIRECTION_EAST},
            {
                "name": "transport-belt",
                "position": {"x": 51.5, "y": 5.5},
                "direction": eh.DIRECTION_NORTH,
            },
            # Belt feeding into belt at (51.5, 5.5) with same/valid direction vs opposing direction
            {
                "name": "transport-belt",
                "position": {"x": 51.5, "y": 6.5},
                "direction": eh.DIRECTION_NORTH,
            },
            # Put belt at (60.5, 5.5) facing EAST into (61.5, 5.5) facing WEST so prune_dead_end_belts removes both.
            {
                "name": "transport-belt",
                "position": {"x": 60.5, "y": 5.5},
                "direction": eh.DIRECTION_EAST,
            },
            {
                "name": "transport-belt",
                "position": {"x": 61.5, "y": 5.5},
                "direction": eh.DIRECTION_WEST,
            },
            # Belt at macro output inserter position (73.5, 1.5) for component at (70, 0) with w=3, h=3 and 1 output -> slot=1
            {
                "name": "transport-belt",
                "position": {"x": 73.5, "y": 1.5},
                "direction": eh.DIRECTION_EAST,
            },
        ]
        def_data = {
            "units": 1000,
            "pins": [{"direction": "OUTPUT", "x": 500, "y": 2500}],
            "components": [{"name": "c1", "macro": "custom_mac", "x": 70000, "y": 0}],
        }
        netlist_data = {
            "cells": {
                "custom_mac": {
                    "width": 3,
                    "height": 3,
                    "inputs": [],
                    "outputs": ["out1"],
                    "in_slots": {},
                    "out_slots": {},
                }
            }
        }
        pruned = eh.prune_dead_end_belts(entities, def_data, netlist_data)
        pruned_belt_positions = {
            (round(e["position"]["x"], 1), round(e["position"]["y"], 1))
            for e in pruned
            if e["name"] == "transport-belt"
        }
        self.assertIn((0.5, 2.5), pruned_belt_positions)
        self.assertIn((9.5, 4.5), pruned_belt_positions)
        self.assertIn((9.5, 5.5), pruned_belt_positions)
        self.assertIn((21.5, 4.5), pruned_belt_positions)
        self.assertIn((21.5, 5.5), pruned_belt_positions)
        self.assertIn((29.5, 6.5), pruned_belt_positions)
        self.assertIn((30.5, 6.5), pruned_belt_positions)
        self.assertIn((39.5, 4.5), pruned_belt_positions)
        self.assertIn((40.5, 4.5), pruned_belt_positions)
        self.assertIn((51.5, 5.5), pruned_belt_positions)
        self.assertIn((51.5, 6.5), pruned_belt_positions)
        self.assertIn((73.5, 1.5), pruned_belt_positions)
        self.assertNotIn((60.5, 5.5), pruned_belt_positions)
        self.assertNotIn((61.5, 5.5), pruned_belt_positions)


if __name__ == "__main__":
    unittest.main()
