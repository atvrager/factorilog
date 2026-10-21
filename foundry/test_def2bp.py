"""Unit tests for the DEF to Factorio blueprint converter."""

from __future__ import annotations

import io
import json
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blueprint
import def2bp
from placement import PlacementResult, RouteResult

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
        entities, wires = def2bp.build_entities(def_data, MINI_NETLIST, strict_drc=False)
        self.assertIsInstance(wires, list)

        # Look up by name
        names = {e["name"] for e in entities}
        self.assertIn("assembling-machine-3", names)
        self.assertIn("splitter", names)
        self.assertIn("transport-belt", names)

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

    def test_resolve_terminal_pins_and_macros(self) -> None:
        """Verify _resolve_terminal for PINs, merge/split/pump macros, and machine faces."""
        pin_dict = {
            "iron_in": (0, 10),
            "coal_in": (10, 0),
            "gear_in": (5, 5),
            "iron_out": (0, 20),
            "coal_out": (20, 0),
            "gear_out": (15, 15),
            "water_in": (0, 30),
            "steam_out": (30, 0),
            "oil_in": (40, 40),
        }
        valid_in_pins = {(0.0, 10.0), (10.0, 0.0), (5.0, 5.0), (0.0, 30.0), (40.0, 40.0)}
        clean_fluids = {"water", "steam", "crude_oil"}

        # Missing pin returns None
        self.assertIsNone(
            def2bp._resolve_terminal(
                "PIN", "missing", "missing", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            )
        )
        # Missing component returns None
        self.assertIsNone(
            def2bp._resolve_terminal(
                "u_missing", "a", "net1", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            )
        )

        # PIN drivers and sinks
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN", "iron_in", "iron_in", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            ),
            ((0, 10), True, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN", "coal_in", "coal_in", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            ),
            ((10, 0), True, def2bp.DIRECTION_SOUTH, def2bp.DIRECTION_SOUTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN", "gear_in", "gear_in", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            ),
            ((5, 5), True, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN", "iron_out", "iron_out", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            ),
            ((0, 20), False, def2bp.DIRECTION_WEST, def2bp.DIRECTION_WEST),
        )
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN", "coal_out", "coal_out", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            ),
            ((20, 0), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN", "gear_out", "gear_out", {}, {}, {}, pin_dict, valid_in_pins, 0.001
            ),
            ((15, 15), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )

        # Fluid PINs
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN",
                "water",
                "water_in",
                {},
                {},
                {},
                pin_dict,
                valid_in_pins,
                0.001,
                clean_fluids=clean_fluids,
            ),
            ((0, 30), True, None, def2bp.DIRECTION_WEST),
        )
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN",
                "steam",
                "steam_out",
                {},
                {},
                {},
                pin_dict,
                valid_in_pins,
                0.001,
                clean_fluids=clean_fluids,
            ),
            ((30, 0), False, None, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal(
                "PIN",
                "crude-oil",
                "oil_in",
                {},
                {},
                {},
                pin_dict,
                valid_in_pins,
                0.001,
                clean_fluids=clean_fluids,
            ),
            ((40, 40), True, None, None),
        )

        # belt_merge2 in W, E, S, N orientations
        comp_map = {
            "m_w": {"name": "m_w", "macro": "belt_merge2", "x": 10000, "y": 20000, "orient": "W"},
            "m_e": {"name": "m_e", "macro": "belt_merge2", "x": 10000, "y": 20000, "orient": "E"},
            "m_s": {"name": "m_s", "macro": "belt_merge2", "x": 10000, "y": 20000, "orient": "S"},
            "m_n": {"name": "m_n", "macro": "belt_merge2", "x": 10000, "y": 20000, "orient": "N"},
            "s_w": {"name": "s_w", "macro": "belt_split2", "x": 10000, "y": 20000, "orient": "W"},
            "s_e": {"name": "s_e", "macro": "belt_split2", "x": 10000, "y": 20000, "orient": "E"},
            "s_s": {"name": "s_s", "macro": "belt_split2", "x": 10000, "y": 20000, "orient": "S"},
            "s_n": {"name": "s_n", "macro": "belt_split2", "x": 10000, "y": 20000, "orient": "N"},
            "p_s": {"name": "p_s", "macro": "pump", "x": 10000, "y": 20000, "orient": "S"},
            "p_n": {"name": "p_n", "macro": "pump", "x": 10000, "y": 20000, "orient": "N"},
            "p_w": {"name": "p_w", "macro": "pump", "x": 10000, "y": 20000, "orient": "W"},
            "p_e": {"name": "p_e", "macro": "pump", "x": 10000, "y": 20000, "orient": "E"},
        }
        # Merge W (splitter_dir = NORTH = 0)
        self.assertEqual(
            def2bp._resolve_terminal("m_w", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 23), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_w", "b", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 23), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_w", "y", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 19), True, def2bp.DIRECTION_NORTH, None),
        )
        # Merge E (splitter_dir = SOUTH = 8)
        self.assertEqual(
            def2bp._resolve_terminal("m_e", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 19), False, def2bp.DIRECTION_SOUTH, def2bp.DIRECTION_SOUTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_e", "b", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 19), False, def2bp.DIRECTION_SOUTH, def2bp.DIRECTION_SOUTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_e", "y", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 23), True, def2bp.DIRECTION_SOUTH, None),
        )
        # Merge S (splitter_dir = WEST = 12)
        self.assertEqual(
            def2bp._resolve_terminal("m_s", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((13, 20), False, def2bp.DIRECTION_WEST, def2bp.DIRECTION_WEST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_s", "b", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((13, 21), False, def2bp.DIRECTION_WEST, def2bp.DIRECTION_WEST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_s", "y", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((9, 21), True, def2bp.DIRECTION_WEST, None),
        )
        # Merge N (splitter_dir = EAST = 4)
        self.assertEqual(
            def2bp._resolve_terminal("m_n", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((9, 20), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_n", "b", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((9, 21), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("m_n", "y", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((13, 21), True, def2bp.DIRECTION_EAST, None),
        )

        # Split W / E
        self.assertEqual(
            def2bp._resolve_terminal("s_w", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 19), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_w", "a1", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 19), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_w", "b", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 19), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_w", "a2", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 19), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_w", "y1", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 21), True, def2bp.DIRECTION_NORTH, None),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_w", "y2", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 21), True, def2bp.DIRECTION_NORTH, None),
        )
        # Split N / S
        self.assertEqual(
            def2bp._resolve_terminal("s_n", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((9, 20), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_n", "a1", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((9, 20), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_n", "b", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((9, 21), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_n", "a2", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((9, 21), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_n", "y1", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 20), True, def2bp.DIRECTION_EAST, None),
        )
        self.assertEqual(
            def2bp._resolve_terminal("s_n", "y2", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((11, 21), True, def2bp.DIRECTION_EAST, None),
        )

        # Pump S, N, W, E
        self.assertEqual(
            def2bp._resolve_terminal("p_s", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 19), False, def2bp.DIRECTION_SOUTH, def2bp.DIRECTION_SOUTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("p_s", "y", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 22), True, def2bp.DIRECTION_SOUTH, None),
        )
        self.assertEqual(
            def2bp._resolve_terminal("p_n", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 19), False, def2bp.DIRECTION_NORTH, def2bp.DIRECTION_NORTH),
        )
        self.assertEqual(
            def2bp._resolve_terminal("p_w", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 19), False, def2bp.DIRECTION_WEST, def2bp.DIRECTION_WEST),
        )
        self.assertEqual(
            def2bp._resolve_terminal("p_e", "a", "n", comp_map, {}, {}, {}, set(), 0.001),
            ((10, 19), False, def2bp.DIRECTION_EAST, def2bp.DIRECTION_EAST),
        )

        # Machine cell fluid and item ports across faces E, W, N, S
        comp_map["u_mach"] = {"name": "u_mach", "macro": "chem", "x": 10000, "y": 20000}
        inst_map = {"u_mach": {"name": "u_mach", "cell": "chem"}}
        cell_map = {
            "chem": {
                "outputs": ["water", "steam", "crude_oil", "heavy_oil", "iron_plate"],
                "inputs": ["water_in", "steam_in", "oil_in", "gas_in", "copper_plate"],
            }
        }
        faces_out = {
            "water": {"inserter_pos": (3.5, 1.5), "approach_pos": (4.5, 1.5), "face": "E"},
            "steam": {"inserter_pos": (-0.5, 1.5), "approach_pos": (-1.5, 1.5), "face": "W"},
            "crude_oil": {"inserter_pos": (1.5, -0.5), "approach_pos": (1.5, -1.5), "face": "N"},
            "heavy_oil": {"inserter_pos": (1.5, 3.5), "approach_pos": (1.5, 4.5), "face": "S"},
            "iron_plate": {
                "inserter_pos": (3.5, 1.5),
                "approach_pos": (4.5, 1.5),
                "face": "E",
                "approach_dir": def2bp.DIRECTION_EAST,
            },
        }
        faces_in = {
            "water_in": {"inserter_pos": (3.5, 1.5), "approach_pos": (4.5, 1.5), "face": "E"},
            "steam_in": {"inserter_pos": (-0.5, 1.5), "approach_pos": (-1.5, 1.5), "face": "W"},
            "oil_in": {"inserter_pos": (1.5, -0.5), "approach_pos": (1.5, -1.5), "face": "N"},
            "gas_in": {"inserter_pos": (1.5, 3.5), "approach_pos": (1.5, 4.5), "face": "S"},
            "copper_plate": {
                "inserter_pos": (-0.5, 1.5),
                "approach_pos": (-1.5, 1.5),
                "face": "W",
                "approach_dir": def2bp.DIRECTION_EAST,
            },
        }
        all_fl = {
            "water",
            "steam",
            "crude_oil",
            "heavy_oil",
            "water_in",
            "steam_in",
            "oil_in",
            "gas_in",
        }
        with mock.patch(
            "foundry.lef.get_macro_port_geometry",
            side_effect=lambda _c, name, is_output=False: (
                faces_out[name] if is_output else faces_in[name]
            ),
        ):
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "water",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((13, 21), True, None, def2bp.DIRECTION_WEST),
            )
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "steam",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((9, 21), True, None, def2bp.DIRECTION_EAST),
            )
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "crude_oil",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((11, 19), True, None, def2bp.DIRECTION_SOUTH),
            )
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "heavy_oil",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((11, 23), True, None, def2bp.DIRECTION_NORTH),
            )
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "water_in",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((13, 21), False, None, def2bp.DIRECTION_WEST),
            )
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "steam_in",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((9, 21), False, None, def2bp.DIRECTION_EAST),
            )
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "oil_in",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((11, 19), False, None, def2bp.DIRECTION_SOUTH),
            )
            self.assertEqual(
                def2bp._resolve_terminal(
                    "u_mach",
                    "gas_in",
                    "n",
                    comp_map,
                    inst_map,
                    cell_map,
                    {},
                    set(),
                    0.001,
                    clean_fluids=all_fl,
                ),
                ((11, 23), False, None, def2bp.DIRECTION_NORTH),
            )

    def test_jumper_belts_across_chassis_poles_all_orientations(self) -> None:
        """Verify jumper_belts_across_chassis_poles converts colliding belts in all 4 directions."""
        base_entities = [{"name": "big-electric-pole", "position": {"x": 10.0, "y": 10.0}}]

        # South (8): normal jumper
        ents_s = [
            {"name": "transport-belt", "position": {"x": 9.5, "y": 8.5}, "direction": 8},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 9.5}, "direction": 8},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 10.5}, "direction": 8},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 11.5}, "direction": 8},
        ]
        res_s = def2bp.jumper_belts_across_chassis_poles(ents_s, base_entities)
        self.assertEqual(len(res_s), 2)
        self.assertEqual(res_s[0]["name"], "underground-belt")
        self.assertEqual(res_s[0]["type"], "input")
        self.assertEqual(res_s[0]["position"], {"x": 9.5, "y": 8.5})
        self.assertEqual(res_s[1]["name"], "underground-belt")
        self.assertEqual(res_s[1]["type"], "output")
        self.assertEqual(res_s[1]["position"], {"x": 9.5, "y": 11.5})

        # South (8): chained underground belt upgraded to fast-underground-belt (dist=2) and express (dist=6)
        ents_s_fast = [
            {"name": "transport-belt", "position": {"x": 9.5, "y": 8.5}, "direction": 8},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 9.5}, "direction": 8},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 10.5}, "direction": 8},
            {
                "name": "underground-belt",
                "type": "input",
                "position": {"x": 9.5, "y": 11.5},
                "direction": 8,
            },
            {
                "name": "underground-belt",
                "type": "output",
                "position": {"x": 9.5, "y": 13.5},
                "direction": 8,
            },
        ]
        res_s_fast = def2bp.jumper_belts_across_chassis_poles(ents_s_fast, base_entities)
        self.assertEqual(len(res_s_fast), 2)
        self.assertEqual(res_s_fast[0]["name"], "fast-underground-belt")
        self.assertEqual(res_s_fast[1]["name"], "fast-underground-belt")

        # South (8): chained underground belt upgraded to express-underground-belt at max_underground_span (dist=9)
        ents_s_exp = [
            {"name": "transport-belt", "position": {"x": 9.5, "y": 8.5}, "direction": 8},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 9.5}, "direction": 8},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 10.5}, "direction": 8},
            {
                "name": "underground-belt",
                "type": "input",
                "position": {"x": 9.5, "y": 11.5},
                "direction": 8,
            },
            {
                "name": "underground-belt",
                "type": "output",
                "position": {"x": 9.5, "y": 13.5},
                "direction": 0,
            },
            {
                "name": "underground-belt",
                "type": "output",
                "position": {"x": 9.5, "y": 20.5},
                "direction": 8,
            },
        ]
        res_s_exp = def2bp.jumper_belts_across_chassis_poles(ents_s_exp, base_entities)
        self.assertEqual(len(res_s_exp), 3)
        self.assertEqual(res_s_exp[0]["name"], "express-underground-belt")
        self.assertEqual(res_s_exp[2]["name"], "express-underground-belt")

        # North (0): normal jumper
        ents_n = [
            {"name": "transport-belt", "position": {"x": 10.5, "y": 11.5}, "direction": 0},
            {"name": "transport-belt", "position": {"x": 10.5, "y": 10.5}, "direction": 0},
            {"name": "transport-belt", "position": {"x": 10.5, "y": 9.5}, "direction": 0},
            {"name": "transport-belt", "position": {"x": 10.5, "y": 8.5}, "direction": 0},
        ]
        res_n = def2bp.jumper_belts_across_chassis_poles(ents_n, base_entities)
        self.assertEqual(len(res_n), 2)
        self.assertEqual(res_n[0]["type"], "input")
        self.assertEqual(res_n[0]["position"], {"x": 10.5, "y": 11.5})
        self.assertEqual(res_n[1]["type"], "output")
        self.assertEqual(res_n[1]["position"], {"x": 10.5, "y": 8.5})

        # East (4): normal jumper
        ents_e = [
            {"name": "transport-belt", "position": {"x": 8.5, "y": 9.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 9.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 10.5, "y": 9.5}, "direction": 4},
            {"name": "transport-belt", "position": {"x": 11.5, "y": 9.5}, "direction": 4},
        ]
        res_e = def2bp.jumper_belts_across_chassis_poles(ents_e, base_entities)
        self.assertEqual(len(res_e), 2)
        self.assertEqual(res_e[0]["type"], "input")
        self.assertEqual(res_e[0]["position"], {"x": 8.5, "y": 9.5})
        self.assertEqual(res_e[1]["type"], "output")
        self.assertEqual(res_e[1]["position"], {"x": 11.5, "y": 9.5})

        # West (12): normal jumper
        ents_w = [
            {"name": "transport-belt", "position": {"x": 11.5, "y": 10.5}, "direction": 12},
            {"name": "transport-belt", "position": {"x": 10.5, "y": 10.5}, "direction": 12},
            {"name": "transport-belt", "position": {"x": 9.5, "y": 10.5}, "direction": 12},
            {"name": "transport-belt", "position": {"x": 8.5, "y": 10.5}, "direction": 12},
        ]
        res_w = def2bp.jumper_belts_across_chassis_poles(ents_w, base_entities)
        self.assertEqual(len(res_w), 2)
        self.assertEqual(res_w[0]["type"], "input")
        self.assertEqual(res_w[0]["position"], {"x": 11.5, "y": 10.5})
        self.assertEqual(res_w[1]["type"], "output")
        self.assertEqual(res_w[1]["position"], {"x": 8.5, "y": 10.5})

    def test_route_with_maze_router_mst_and_headings(self) -> None:
        """Verify fluid MST ordering, pin obstacles, and component headings in route_with_maze_router."""
        def_data = {
            "units": 1000,
            "die_area": [0, 0, 50000, 50000],
            "pins": [
                {"name": "water_in", "net": "water__1", "direction": "INPUT", "x": 0, "y": 10000},
                {
                    "name": "iron_out",
                    "net": "iron__1",
                    "direction": "OUTPUT",
                    "x": 40000,
                    "y": 10000,
                },
            ],
            "components": [
                {"name": "p1", "macro": "pump", "x": 10000, "y": 10000, "orient": "E"},
                {"name": "p2", "macro": "pump", "x": 30000, "y": 10000, "orient": "E"},
                {"name": "s1", "macro": "belt_split2", "x": 20000, "y": 10000, "orient": "N"},
            ],
            "nets": [
                {
                    "name": "water__1",
                    "connections": [("PIN", "water_in"), ("p1", "a"), ("p2", "a")],
                },
                {
                    "name": "iron__1",
                    "connections": [("s1", "y1"), ("PIN", "iron_out")],
                },
            ],
        }
        netlist_data = {"fluids": ["water"], "instances": [], "cells": {}}
        placement = PlacementResult(
            entities=[
                {"name": "splitter", "position": [20.5, 11.0], "direction": 4},
                {"name": "splitter", "position": [25.0, 15.5], "direction": 0},
                {"name": "transport-belt", "position": [15.5, 15.5], "direction": 4},
                {"name": "pipe", "position": [5.5, 5.5], "fluid": "water"},
                {"name": "pipe", "position": [6.5, 5.5], "fluid": "steam"},
                {"name": "pipe", "position": [5.5, 6.5], "fluid": "crude_oil"},
            ],
            occupied_positions=set(),
            grid_entities={},
            inst_to_entity={},
            fluids={"water"},
            has_fluid_metadata=True,
        )

        captured_init: dict[str, Any] = {}
        captured_specs: list[Any] = []

        class FakeRouter:
            def __init__(self, **kwargs: Any) -> None:
                captured_init.update(kwargs)

            def route(self, specs: list[Any], **_kwargs: Any) -> tuple[dict, list, None]:
                captured_specs.extend(specs)
                return {}, [], None

        with mock.patch("foundry.router.maze_router.DualLaneMazeRouter", FakeRouter):
            res = def2bp.route_with_maze_router(def_data, netlist_data, placement, engine=False)

        self.assertEqual(res.valid_input_pins, {(0.0, 10.0)})
        self.assertEqual(res.valid_output_pins, {(40.0, 10.0)})
        # Pin pad offsets for INPUT (-1, 0), (-2, 0), (-1, -1), (-1, 1) and OUTPUT (1, 0), (2, 0), (1, -1), (1, 1)
        self.assertIn((-1, 10), captured_init["obstacles"])
        self.assertIn((-2, 10), captured_init["obstacles"])
        self.assertIn((41, 10), captured_init["obstacles"])
        self.assertIn((42, 10), captured_init["obstacles"])
        # MST for water__1 connects (0, 10) -> (10, 9) first (dist 11), then (10, 9) -> (30, 9) (dist 20)
        mst_specs = [s for s in captured_specs if s.is_fluid]
        self.assertEqual(len(mst_specs), 2)
        self.assertEqual(mst_specs[0].source, (10, 9))
        self.assertEqual(mst_specs[0].sink, (0, 10))
        self.assertEqual(mst_specs[1].source, (30, 9))
        self.assertEqual(mst_specs[1].sink, (10, 9))

        p2p_fluid = def2bp._build_net_specs_for_net(
            {"name": "water__p2p", "connections": [("PIN", "water_in"), ("p1", "a")]},
            {"p1": {"name": "p1", "macro": "pump", "x": 10000, "y": 10000, "orient": "E"}},
            {},
            {},
            {"water_in": (0, 10)},
            {(0.0, 10.0)},
            0.001,
            {"water"},
        )
        self.assertEqual(len(p2p_fluid), 1)
        self.assertEqual(p2p_fluid[0].name, "water__p2p")
        self.assertEqual(p2p_fluid[0].source, (0, 10))
        self.assertEqual(p2p_fluid[0].sink, (10, 9))

        headings = captured_init["component_headings"]
        self.assertEqual(headings[(20, 10)], 4)
        self.assertEqual(headings[(20, 11)], 4)
        self.assertEqual(headings[(24, 15)], 0)
        self.assertEqual(headings[(25, 15)], 0)
        self.assertEqual(headings[(15, 15)], 4)
        self.assertIn((16, 15), captured_init["no_hood_tiles"])
        fluid_approaches = captured_init["fluid_port_approaches"]
        self.assertEqual(fluid_approaches[(5, 5)], {"water", "steam", "crude-oil"})
        self.assertEqual(fluid_approaches[(6, 5)], {"water", "steam"})
        self.assertEqual(fluid_approaches[(4, 5)], {"water"})
        self.assertEqual(fluid_approaches[(7, 5)], {"steam"})
        self.assertEqual(fluid_approaches[(5, 6)], {"water", "crude-oil"})
        self.assertEqual(fluid_approaches[(5, 7)], {"crude-oil"})
        self.assertEqual(fluid_approaches[(5, 4)], {"water"})
        self.assertIsNone(captured_init["engine"])

        # Direct unit check for single pipe fluid_port_approaches with asymmetric odx/ody
        _, _, fpa_single = def2bp._collect_router_constraints(
            [],
            [
                {"name": "pipe", "position": [10.5, 20.5], "fluid": "water"},
                {"name": "pipe", "position": [12.5, 22.5], "fluid": "steam"},
            ],
            set(),
            set(),
        )
        self.assertEqual(fpa_single[(11, 20)], {"water"})
        self.assertEqual(fpa_single[(10, 21)], {"water"})
        self.assertEqual(fpa_single[(11, 22)], {"steam"})
        self.assertEqual(fpa_single[(12, 21)], {"steam"})

        # Also verify max_y / min_x / max_x / min_y pad expansion beyond die_area (10x10)
        def_pad = {
            "units": 1000,
            "die_area": [0, 0, 10000, 10000],
            "pins": [
                {"name": "p_out", "net": "iron__p", "direction": "OUTPUT", "x": 20000, "y": 25000}
            ],
            "components": [
                {"name": "s1", "macro": "belt_split2", "x": 5000, "y": 30000, "orient": "N"}
            ],
            "nets": [{"name": "iron__p", "connections": [("s1", "y1"), ("PIN", "p_out")]}],
        }
        with mock.patch("foundry.router.maze_router.DualLaneMazeRouter", FakeRouter):
            def2bp.route_with_maze_router(def_pad, netlist_data, placement, engine=False)
        self.assertEqual(captured_init["max_x"], 22)
        self.assertEqual(captured_init["max_y"], 32)

    def test_build_entities_chassis_pole_bridging_and_fluid_sharing(self) -> None:
        """Verify chassis pole network bridging, lamp filtering, and same-fluid tile sharing."""
        def_data = {
            "units": 1000,
            "die_area": [0, 0, 200000, 200000],
            "blockages": [{"x1": 0, "y1": 0, "x2": 1000, "y2": 1000}],
            "pins": [
                {"name": "water_in", "net": "water__1", "direction": "INPUT", "x": 0, "y": 50000}
            ],
            "components": [],
            "nets": [],
        }
        netlist_data = {"fluids": ["water"], "instances": [], "cells": {}}

        fake_chassis_entities = [
            {"name": "big-electric-pole", "position": {"x": 5.0, "y": 5.0}},
            {"name": "big-electric-pole", "position": {"x": 5.0, "y": 35.0}},
            {"name": "big-electric-pole", "position": {"x": 20.0, "y": 20.0}},
            {"name": "medium-electric-pole", "position": {"x": 5.5, "y": 50.5}},
            {"name": "small-lamp", "position": {"x": 6.5, "y": 5.5}},
            {"name": "small-lamp", "position": {"x": 11.5, "y": 20.5}},
            {"name": "small-lamp", "position": {"x": 150.5, "y": 150.5}},
            {"name": "radar", "position": {"x": 5.5, "y": 5.5}},
            {"name": "radar", "position": {"x": 12.5, "y": 20.5}},
        ]

        fake_routes = RouteResult(
            net_surface_tiles={"water__1": {(20.5, 20.5): 4}, "water__2": {(20.5, 20.5): 4}},
            tile_to_nets={(20.5, 20.5): {"water__1", "water__2"}},
            adjacent_fluid_conflicts=set(),
            pin_tiles={(2.0, 42.0)},
            valid_input_pins={(2.0, 42.0)},
            valid_output_pins=set(),
        )

        orig_place = def2bp.place_components

        def place_with_assembler(d: dict, n: dict) -> PlacementResult:
            res = orig_place(d, n)
            res.entities.append(
                {
                    "name": "assembling-machine-3",
                    "position": {"x": 10.5, "y": 20.5},
                    "direction": 0,
                }
            )
            res.entities.append(
                {
                    "name": "medium-electric-pole",
                    "position": {"x": 10.5, "y": 17.5},
                }
            )
            return res

        untangle_kwargs: list[dict[str, Any]] = []

        def spy_untangle(_d: dict, _n: dict, **kw: Any) -> None:
            untangle_kwargs.append(kw)

        with (
            mock.patch(
                "foundry.chassis.CityBlockChassis.emit_base_blueprint",
                return_value={"blueprint": {"entities": fake_chassis_entities, "tiles": []}},
            ),
            mock.patch.object(def2bp, "untangle_def", side_effect=spy_untangle),
            mock.patch.object(def2bp, "place_components", side_effect=place_with_assembler),
            mock.patch.object(def2bp, "route_with_maze_router", return_value=fake_routes),
            mock.patch.object(def2bp, "generate_pdn", return_value=([], [])),
        ):
            entities, wires = def2bp.build_entities(def_data, netlist_data, strict_drc=True)

        self.assertEqual(untangle_kwargs[0].get("pins_only"), True)
        big_poles = [e for e in entities if e["name"] == "big-electric-pole"]
        med_poles = [e for e in entities if e["name"] == "medium-electric-pole"]
        lamps = [e for e in entities if e["name"] == "small-lamp"]
        radars = [e for e in entities if e["name"] == "radar"]
        self.assertEqual(len(big_poles), 3)
        self.assertEqual(len(lamps), 1)
        self.assertEqual(lamps[0]["position"], {"x": 6.5, "y": 5.5})
        self.assertGreaterEqual(len(med_poles), 3)
        self.assertEqual(len(radars), 0)
        # Diagonal big pole at (20.0, 20.0) must not get red/green aligned wire (wid 1 or 2)
        diag_num = next(
            e["entity_number"] for e in big_poles if e["position"] == {"x": 20.0, "y": 20.0}
        )
        red_green_nodes = {w[0] for w in wires if w[1] in (1, 2)} | {
            w[2] for w in wires if w[3] in (1, 2)
        }
        self.assertNotIn(diag_num, red_green_nodes)
        # Bridge poles from (5.0, 35.0) to (5.5, 50.5) step on half-integer tiles (.5) and avoid pin guard at (5.5, 42.5)
        med_positions = [(mp["position"]["x"], mp["position"]["y"]) for mp in med_poles]
        for _mx, my in med_positions:
            self.assertEqual(my % 1.0, 0.5)
        # Guard tile (5.5, 43.5) with pin at (2.0, 43.0) so bridge pole shifts away.
        self.assertIn((5.5, 43.5), med_positions)
        self.assertEqual(len(med_poles), 5)

        fake_routes_guard = RouteResult(
            net_surface_tiles={"water__1": {(20.5, 20.5): 4}, "water__2": {(20.5, 20.5): 4}},
            tile_to_nets={(20.5, 20.5): {"water__1", "water__2"}},
            adjacent_fluid_conflicts=set(),
            pin_tiles={(2.0, 43.0)},
            valid_input_pins={(2.0, 43.0)},
            valid_output_pins=set(),
        )
        with (
            mock.patch(
                "foundry.chassis.CityBlockChassis.emit_base_blueprint",
                return_value={"blueprint": {"entities": fake_chassis_entities, "tiles": []}},
            ),
            mock.patch.object(def2bp, "untangle_def"),
            mock.patch.object(def2bp, "place_components", side_effect=place_with_assembler),
            mock.patch.object(def2bp, "route_with_maze_router", return_value=fake_routes_guard),
            mock.patch.object(def2bp, "generate_pdn", return_value=([], [])),
        ):
            entities_g, _ = def2bp.build_entities(def_data, netlist_data, strict_drc=True)
        med_positions_g = [
            (e["position"]["x"], e["position"]["y"])
            for e in entities_g
            if e["name"] == "medium-electric-pole"
        ]
        self.assertNotIn((5.5, 43.5), med_positions_g)
        self.assertIn((6.5, 43.5), med_positions_g)

        # Direct unit assertion for _step_bridge_pole_toward and _reserve_pin_guard_tiles
        step_pt = def2bp._step_bridge_pole_toward(10.0, 20.0, 19.0, 20.5, set(), 9.0)
        self.assertEqual(step_pt, (18.5, 20.5))
        step_pt_y = def2bp._step_bridge_pole_toward(10.0, 20.0, 10.5, 29.0, set(), 9.0)
        self.assertEqual(step_pt_y, (10.5, 28.5))
        guard_occ: set[tuple[float, float]] = set()
        def2bp._reserve_pin_guard_tiles(
            RouteResult({}, {}, set(), {(5.0, 10.0)}, set(), set()),
            guard_occ,
        )
        self.assertIn((4.5, 10.5), guard_occ)
        self.assertIn((6.5, 10.5), guard_occ)
        self.assertIn((5.5, 9.5), guard_occ)
        self.assertIn((5.5, 11.5), guard_occ)

        # Single-item-net tile_to_nets entry must not be flagged as unresolved crossing in DRC
        single_item_routes = RouteResult(
            net_surface_tiles={"iron__1": {(20.5, 20.5): 4}},
            tile_to_nets={(20.5, 20.5): {"iron__1"}},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        drc_kwargs: dict[str, Any] = {}

        def spy_drc(_self: Any, _ents: Any, **kw: Any) -> list[Any]:
            drc_kwargs.update(kw)
            return []

        with mock.patch.object(def2bp.BeltDrcChecker, "check_entities", spy_drc):
            def2bp._run_drc_and_verify(
                [],
                [],
                single_item_routes,
                PlacementResult([], set(), {}, {}, set(), False),
                {
                    "pins": [
                        {"direction": "INPUT", "x": 1000, "y": 2000},
                        {"direction": "OUTPUT", "x": 3000, "y": 4000},
                    ]
                },
                {},
                0.001,
                strict_drc=True,
            )
        self.assertEqual(drc_kwargs["input_pins"], [(1.0, 2.0)])
        self.assertEqual(drc_kwargs["output_pins"], [(3.0, 4.0)])
        self.assertEqual(drc_kwargs["unresolved_crossings"], [])

        merged_lamps, _ = def2bp._merge_chassis_base_entities(
            [{"name": "medium-electric-pole", "position": {"x": 50.5, "y": 50.5}}],
            [
                {"name": "small-lamp", "position": {"x": 53.5, "y": 50.5}},
                {"name": "big-electric-pole", "position": {"x": 80.0, "y": 80.0}},
                {"name": "small-lamp", "position": {"x": 83.0, "y": 80.5}},
            ],
            True,
            def2bp.DEFAULT_DEF2BP_CONFIG,
        )
        lamp_pts = [
            (e["position"]["x"], e["position"]["y"])
            for e in merged_lamps
            if e["name"] == "small-lamp"
        ]
        self.assertEqual(lamp_pts, [(53.5, 50.5)])

    def test_route_with_maze_router_hierarchical_and_verifier(self) -> None:
        """Verify hierarchical Stage 1/2 routing, obstacle masking, and NativeLayoutVerifier."""
        def_data = {
            "units": 1000,
            "die_area": [0, 0, 200000, 200000],
            "blockages": [{"x1": 0, "y1": 0, "x2": 1000, "y2": 1000}],
            "pins": [],
            "components": [
                {"name": "s1", "macro": "belt_split2", "x": 10000, "y": 10000, "orient": "N"},
                {"name": "s2", "macro": "belt_split2", "x": 110000, "y": 110000, "orient": "N"},
            ],
            "nets": [
                {"name": "iron__1", "connections": [("s1", "y1"), ("s2", "a")]},
            ],
        }
        netlist_data = {"fluids": [], "instances": [], "cells": {}}
        placement = PlacementResult(
            entities=[],
            occupied_positions={(50.5, 50.5)},
            grid_entities={},
            inst_to_entity={},
            fluids=set(),
            has_fluid_metadata=False,
        )

        router_instances: list[dict[str, Any]] = []

        class FakeNode:
            def __init__(self, x: int, y: int, z: int, heading: int) -> None:
                self.x = x
                self.y = y
                self.z = z
                self.heading = heading

        class FakeRouteRes:
            is_success = True
            nodes = (FakeNode(10, 10, 0, 4), FakeNode(11, 10, 1, 4))

        class FakeHierRouter:
            def __init__(self, **kwargs: Any) -> None:
                router_instances.append(kwargs)

            def route(self, specs: list[Any], **_kwargs: Any) -> tuple[dict, list, None]:
                routes = {s.name: FakeRouteRes() for s in specs}
                ents = [
                    {
                        "name": "transport-belt",
                        "position": {"x": 10.5 + len(router_instances), "y": 10.5},
                        "direction": 4,
                    }
                ]
                return routes, ents, None

        class FakeVerifRes:
            is_valid = True
            lvs_errors: tuple = ()
            drc_violations: tuple = ()

        class FakeVerifier:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def verify_blueprint(self, *_args: Any, **_kwargs: Any) -> FakeVerifRes:
                return FakeVerifRes()

        stderr_buf = io.StringIO()
        print_kwargs: list[dict[str, Any]] = []

        def tracking_print(*args: Any, **kwargs: Any) -> None:
            print_kwargs.append(kwargs)
            stderr_buf.write(" ".join(str(a) for a in args) + "\n")

        with (
            mock.patch("foundry.router.maze_router.DualLaneMazeRouter", FakeHierRouter),
            mock.patch("foundry.native_engine.verifier.NativeLayoutVerifier", FakeVerifier),
            mock.patch("builtins.print", side_effect=tracking_print),
            mock.patch(
                "time.perf_counter",
                side_effect=[10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
            ),
        ):
            hier_res = def2bp.route_with_maze_router(
                def_data, netlist_data, placement, engine=object()
            )
        self.assertIn((10.5, 10.5), hier_res.tile_to_nets)
        self.assertNotIn((11.5, 10.5), hier_res.tile_to_nets)

        err_out = stderr_buf.getvalue()
        self.assertIn("Stage 1: routing", err_out)
        self.assertIn("Stage 2: routing", err_out)
        self.assertIn("NativeLayoutVerifier completed in 1.00s", err_out)
        self.assertTrue(all(kw.get("flush") is True for kw in print_kwargs))
        # Stage 2 highway router must mask core interiors starting at margin (4, 4) and not street channels at (3, 50)
        hwy_init = router_instances[-1]
        hwy_obs = hwy_init["obstacles"]
        self.assertEqual(hwy_init["max_x"], 199)
        self.assertEqual(hwy_init["max_y"], 199)
        self.assertIn((4, 50), hwy_obs)
        self.assertNotIn((3, 50), hwy_obs)
        self.assertIn((150, 50), hwy_obs)
        # Bay block (0, 0) discards y in [4, 17), and bay block (1, 1) discards y in [104, 117)
        self.assertNotIn((15, 10), hwy_obs)
        self.assertIn((15, 17), hwy_obs)
        self.assertNotIn((115, 105), hwy_obs)
        self.assertNotIn((115, 110), hwy_obs)
        self.assertIn((115, 117), hwy_obs)
        self.assertIn((115, 95), hwy_obs)
        # Direct unit assertion for _build_highway_obstacles with margin=3 vs BAY_Y_STOP=17
        direct_obs = def2bp._build_highway_obstacles(
            2, 2, 3, 100, {(0, 0), (0, 1), (1, 0)}, set(), []
        )
        self.assertNotIn((10, 4), direct_obs)
        self.assertNotIn((96, 10), direct_obs)
        self.assertNotIn((196, 10), direct_obs)
        self.assertNotIn((10, 16), direct_obs)
        self.assertIn((10, 17), direct_obs)
        self.assertIn((10, 96), direct_obs)
        self.assertNotIn((10, 97), direct_obs)
        self.assertNotIn((10, 104), direct_obs)
        self.assertNotIn((10, 116), direct_obs)
        self.assertIn((10, 117), direct_obs)
        self.assertIn((10, 196), direct_obs)
        self.assertNotIn((10, 197), direct_obs)
        self.assertIn((96, 50), direct_obs)
        self.assertNotIn((97, 50), direct_obs)

    def test_build_entities_legacy_floorplan_and_grid_sizes(self) -> None:
        """Verify 2x2 vs 1x1 chassis obstacle handling and legacy floorplan input routing."""
        # 2x2 grid (grid_w * grid_h >= 4) vs 1x1 grid (grid_w * grid_h < 4)
        def_1x1 = {
            "units": 1000,
            "die_area": [0, 0, 100000, 100000],
            "blockages": [{"x1": 0, "y1": 0, "x2": 1000, "y2": 1000}],
            "pins": [],
            "components": [],
            "nets": [],
        }
        netlist_1x1 = {"fluids": [], "instances": [], "cells": {}}
        fake_chassis_1x1 = [
            {"name": "medium-electric-pole", "position": {"x": 50.5, "y": 50.5}},
            {"name": "big-electric-pole", "position": {"x": 5.0, "y": 5.0}},
        ]
        fake_routes = RouteResult(
            net_surface_tiles={},
            tile_to_nets={},
            adjacent_fluid_conflicts=set(),
            pin_tiles=set(),
            valid_input_pins=set(),
            valid_output_pins=set(),
        )
        captured_occ_1x1: set[tuple[float, float]] = set()

        def capture_1x1(_d: dict, _n: dict, pl: PlacementResult, **_kw: object) -> RouteResult:
            captured_occ_1x1.update(pl.occupied_positions)
            return fake_routes

        with (
            mock.patch(
                "foundry.chassis.CityBlockChassis.emit_base_blueprint",
                return_value={"blueprint": {"entities": fake_chassis_1x1, "tiles": []}},
            ),
            mock.patch.object(def2bp, "route_with_maze_router", side_effect=capture_1x1),
            mock.patch.object(def2bp, "generate_pdn", return_value=([], [])),
            mock.patch.object(def2bp, "wire_telemetry", return_value=[]),
        ):
            def2bp.build_entities(def_1x1, netlist_1x1, strict_drc=False, use_maze_router=True)
        # In 1x1 grid, interior medium pole at (50.5, 50.5) is added to occupied_positions
        self.assertIn((50.5, 50.5), captured_occ_1x1)

        # In 2x2 grid, interior medium pole at (50.5, 50.5) is skipped (not corner pole or radar)
        def_2x2 = dict(def_1x1, die_area=[0, 0, 200000, 200000])
        captured_occ_2x2: set[tuple[float, float]] = set()

        def capture_2x2(_d: dict, _n: dict, pl: PlacementResult, **_kw: object) -> RouteResult:
            captured_occ_2x2.update(pl.occupied_positions)
            return fake_routes

        with (
            mock.patch(
                "foundry.chassis.CityBlockChassis.emit_base_blueprint",
                return_value={"blueprint": {"entities": fake_chassis_1x1, "tiles": []}},
            ),
            mock.patch.object(def2bp, "route_with_maze_router", side_effect=capture_2x2),
            mock.patch.object(def2bp, "generate_pdn", return_value=([], [])),
            mock.patch.object(def2bp, "wire_telemetry", return_value=[]),
        ):
            def2bp.build_entities(def_2x2, netlist_1x1, strict_drc=False, use_maze_router=True)
        self.assertNotIn((50.5, 50.5), captured_occ_2x2)
        self.assertIn((4.5, 4.5), captured_occ_2x2)

        # Legacy branch (use_maze_router=False) with external PIN input into block (1, 0)
        def_legacy = {
            "units": 1000,
            "die_area": [0, 0, 200000, 100000],
            "blockages": [{"x1": 0, "y1": 0, "x2": 1000, "y2": 1000}],
            "pins": [
                {"name": "iron_in", "net": "iron_in", "direction": "INPUT", "x": 0, "y": 5000},
                {
                    "name": "no_pin_net",
                    "net": "no_pin_net",
                    "direction": "INPUT",
                    "x": 0,
                    "y": 8000,
                },
            ],
            "components": [
                {"name": "u_s0", "macro": "belt_split2", "x": 10000, "y": 25000, "orient": "N"},
                {"name": "u_s1", "macro": "belt_split2", "x": 110000, "y": 10000, "orient": "N"},
                {"name": "u_s2", "macro": "belt_split2", "x": 110000, "y": 25000, "orient": "N"},
            ],
            "nets": [
                {"name": "iron_in", "connections": [("PIN", "iron_in"), ("u_s1", "a")]},
                {"name": "no_pin_net", "connections": [("u_s1", "b")]},
                {"name": "ib_net", "connections": [("u_s0", "y1"), ("u_s2", "b")]},
            ],
        }
        netlist_legacy = {
            "fluids": [],
            "instances": [
                {"name": "u_s0", "cell": "belt_split2"},
                {"name": "u_s1", "cell": "belt_split2"},
                {"name": "u_s2", "cell": "belt_split2"},
            ],
            "cells": {"belt_split2": {"inputs": ["a", "b"], "outputs": ["y1", "y2"]}},
        }
        with (
            mock.patch(
                "foundry.chassis.CityBlockChassis.emit_base_blueprint",
                return_value={"blueprint": {"entities": [], "tiles": []}},
            ),
            mock.patch.object(def2bp, "untangle_def"),
            mock.patch.object(def2bp, "route_with_maze_router", return_value=fake_routes),
            mock.patch.object(def2bp, "generate_pdn", return_value=([], [])),
            mock.patch.object(def2bp, "wire_telemetry", return_value=[]),
        ):
            ents_leg, _ = def2bp.build_entities(
                def_legacy, netlist_legacy, strict_drc=False, use_maze_router=False
            )
        # Pin x updated to 98000 (98.5 * 1000 - 500), while no_pin_net pin stays at 0
        self.assertEqual(def_legacy["pins"][0]["x"], 98000)
        self.assertEqual(def_legacy["pins"][1]["x"], 0)
        self.assertEqual(def_legacy["nets"][2].get("wires"), [])
        # Legacy L-channel must emit vertical south belts from y=0.5 up to y=9.5 at x=98.5
        leg_positions = [(e["position"]["x"], e["position"]["y"]) for e in ents_leg]
        self.assertIn((98.5, 0.5), leg_positions)
        self.assertIn((98.5, 9.5), leg_positions)
        self.assertIn((109.5, 10.5), leg_positions)

        # Direct unit check for _select_cityblock_chassis programmatic threshold (2x2 >= 4 vs 1x1 < 4)
        ch_2x2 = def2bp._select_cityblock_chassis(2, 2, def2bp.DEFAULT_DEF2BP_CONFIG)
        self.assertEqual(ch_2x2.channel_width, 8)

    def test_perf_cases_smoke(self) -> None:
        """Smoke-test all perf_* scale benchmark generators at small scale."""
        for perf_fn in (
            perf_build_fluid_mst_specs,
            perf_wire_chassis_big_and_medium_poles,
            perf_bridge_disconnected_chassis_poles,
            perf_register_chassis_router_obstacles,
            perf_merge_chassis_base_entities,
            perf_jumper_belts_across_chassis_poles,
            perf_route_stage1_blocks,
            perf_plan_legacy_macro_floorplan,
            perf_build_highway_obstacles,
            perf_collect_router_constraints,
            perf_build_surface_tile_maps,
        ):
            for _ in perf_fn(8):
                pass


def perf_build_fluid_mst_specs(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (32, 64, 128, 256),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for fluid terminal Manhattan MST construction."""
    terms = [
        ((i * 3, (i * 17) % 97), def2bp.DIRECTION_EAST, def2bp.DIRECTION_WEST) for i in range(n)
    ]
    yield
    def2bp._build_fluid_mst_specs("water__1", "water", terms)


def perf_wire_chassis_big_and_medium_poles(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512, 1024),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for chassis big and medium electric pole wiring."""
    entities: list[dict[str, Any]] = []
    for i in range(n):
        gx = float((i % 16) * 30 + 5)
        gy = float((i // 16) * 30 + 5)
        entities.append(
            {
                "name": "big-electric-pole",
                "position": {"x": gx, "y": gy},
                "entity_number": i * 2 + 1,
            }
        )
        entities.append(
            {
                "name": "medium-electric-pole",
                "position": {"x": gx + 3.5, "y": gy + 3.5},
                "entity_number": i * 2 + 2,
            }
        )
    wires: list[list[int]] = []
    yield
    def2bp._wire_chassis_big_and_medium_poles(entities, wires, def2bp.DEFAULT_DEF2BP_CONFIG)


def perf_bridge_disconnected_chassis_poles(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (32, 64, 128, 256),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for bridging disconnected chassis electric pole components."""
    med_poles: list[dict[str, Any]] = [
        {
            "name": "medium-electric-pole",
            "position": {"x": float((i % 16) * 7 + 0.5), "y": float((i // 16) * 7 + 0.5)},
            "entity_number": i + 1,
        }
        for i in range(n)
    ]
    entities = list(med_poles)
    routes = RouteResult({}, {}, set(), set(), set(), set())
    wires: list[list[int]] = [[1, 5, 2, 5]]
    occupied: set[tuple[float, float]] = set()
    yield
    def2bp._bridge_disconnected_chassis_poles(
        entities, wires, [], med_poles, routes, occupied, def2bp.DEFAULT_DEF2BP_CONFIG
    )


def perf_register_chassis_router_obstacles(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512, 1024),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for chassis obstacle registration against pin clearance zones."""
    pins = [
        {"name": f"p_{i}", "net": f"n_{i}", "direction": "INPUT", "x": (i * 13) * 1000, "y": 0}
        for i in range(n)
    ]
    chassis_ents = [
        {
            "name": "radar" if i % 4 == 0 else "big-electric-pole",
            "position": {"x": float((i % 32) * 10 + 5), "y": float((i // 32) * 10 + 5)},
        }
        for i in range(n)
    ]
    placement = PlacementResult([], set(), {}, {}, set(), False)
    def_data = {"pins": pins}
    yield
    def2bp._register_chassis_router_obstacles(
        def_data,
        placement,
        chassis_ents,
        4,
        4,
        400.0,
        400.0,
        0.001,
        def2bp.DEFAULT_DEF2BP_CONFIG,
    )


def perf_merge_chassis_base_entities(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512, 1024),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for merging chassis base entities and spatial lamp filtering."""
    entities: list[dict[str, Any]] = [
        {
            "name": "medium-electric-pole",
            "position": {"x": float((i % 32) * 8 + 1.5), "y": float((i // 32) * 8 + 1.5)},
            "direction": 0,
        }
        for i in range(n)
    ]
    chassis_base: list[dict[str, Any]] = [
        {
            "name": "small-lamp" if i % 2 == 0 else "big-electric-pole",
            "position": {"x": float((i % 32) * 8 + 2.5), "y": float((i // 32) * 8 + 2.5)},
        }
        for i in range(n)
    ]
    yield
    def2bp._merge_chassis_base_entities(entities, chassis_base, True, def2bp.DEFAULT_DEF2BP_CONFIG)


def perf_jumper_belts_across_chassis_poles(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512, 1024),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for converting pole-colliding belts into underground jumpers."""
    belts: list[dict[str, Any]] = []
    poles: list[dict[str, Any]] = []
    for i in range(n):
        px = float((i % 32) * 10 + 10)
        py = float((i // 32) * 10 + 10)
        poles.append({"name": "big-electric-pole", "position": {"x": px, "y": py}})
        belts.extend(
            {
                "name": "transport-belt",
                "position": {"x": px - 0.5, "y": py + dy},
                "direction": def2bp.DIRECTION_SOUTH,
            }
            for dy in (-1.5, -0.5, 0.5, 1.5)
        )
    yield
    def2bp.jumper_belts_across_chassis_poles(belts, poles, cfg=def2bp.DEFAULT_DEF2BP_CONFIG)


def perf_route_stage1_blocks(
    n: int = 32,
    *,
    _scales: tuple[int, ...] = (32, 64, 128, 256),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for Stage 1 intra-block routing and obstacle partitioning."""
    pitch = def2bp.DEFAULT_DEF2BP_CONFIG.block_pitch
    chassis = def2bp.CityBlockChassis(pitch=pitch, channel_width=8, programmatic=True)
    planner = def2bp.MacroFloorplanner(chassis=chassis)
    intra_specs: dict[tuple[int, int], list[def2bp.RouterNetSpec]] = {}
    obstacles: set[tuple[int, int]] = set()
    for bx in range(n):
        ox = bx * pitch
        intra_specs[(bx, 0)] = [
            def2bp.RouterNetSpec(
                name=f"intra_{bx}",
                source=(ox + 10, 10),
                sink=(ox + 10, 10),
                commodity="iron-plate",
                start_heading=def2bp.DIRECTION_EAST,
                end_heading=def2bp.DIRECTION_EAST,
            )
        ]
        for k in range(64):
            obstacles.add((ox + 15 + (k % 60), 15 + (k // 60)))
    prototypes = def2bp.verifier_mod.load_game_prototypes()
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        yield
        def2bp._route_stage1_blocks(
            intra_specs,
            planner,
            set(),
            4,
            obstacles,
            prototypes,
            {},
            set(),
            {},
            False,
            def2bp.DEFAULT_DEF2BP_CONFIG,
        )
    finally:
        sys.stderr = old_stderr


def perf_plan_legacy_macro_floorplan(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for legacy macro floorplanning and external input pin relocation."""
    pins = [
        {"name": f"in_{i}", "net": f"net_{i}", "direction": "INPUT", "x": i * 1000, "y": 0}
        for i in range(n)
    ]
    nets = [
        {
            "name": f"net_{i}",
            "connections": [("PIN", f"in_{i}"), (f"inst_{i}", "in")],
            "wires": [],
            "vias": [],
        }
        for i in range(n)
    ]
    inst_to_entity: dict[str, dict[str, Any]] = {
        f"inst_{i}": {
            "name": "assembling-machine-1",
            "position": {"x": 100.5, "y": 1.5},
        }
        for i in range(n)
    }
    placement = PlacementResult([], set(), {}, inst_to_entity, set(), False)
    def_data: dict[str, Any] = {"units": 1000, "pins": pins, "nets": nets}
    netlist_data: dict[str, Any] = {
        "instances": [{"name": f"inst_{i}", "cell": "c1"} for i in range(n)],
        "cells": {"c1": {"inputs": ["in"], "outputs": ["out"]}},
    }
    chassis = def2bp.CityBlockChassis(pitch=100, channel_width=8, programmatic=True)
    yield
    def2bp._plan_legacy_macro_floorplan(
        def_data, netlist_data, placement, chassis, 2, 1, def2bp.DEFAULT_DEF2BP_CONFIG
    )


def perf_build_highway_obstacles(
    n: int = 16,
    *,
    _scales: tuple[int, ...] = (8, 16, 32, 64),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for highway obstacle mask construction across blocks."""
    pitch = def2bp.DEFAULT_DEF2BP_CONFIG.block_pitch
    comps = [{"x": (bx * pitch + 10) * 1000, "y": 10 * 1000} for bx in range(n)]
    specs = [
        def2bp.RouterNetSpec(
            name=f"hwy_{bx}",
            source=(bx * pitch + 2, 2),
            sink=(bx * pitch + 3, 2),
            commodity="iron-plate",
        )
        for bx in range(n)
    ]
    obstacles = {(bx * pitch + 1, 1) for bx in range(n)}
    yield
    bay_blocks = def2bp._find_bay_blocks(comps, 0.001, n, 1, pitch)
    def2bp._build_highway_obstacles(n, 1, 4, pitch, bay_blocks, obstacles, specs)


def perf_collect_router_constraints(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for router terminal extraction and constraint collection."""
    comps = [
        {"name": f"u_{i}", "macro": "belt_merge2", "x": (i * 10) * 1000, "y": 10000, "orient": "N"}
        for i in range(n)
    ]
    pins = [
        {"name": f"p_{i}", "net": f"n_{i}", "direction": "INPUT", "x": (i * 10) * 1000, "y": 0}
        for i in range(n)
    ]
    nets = [
        {"name": f"iron_plate__{i}", "connections": [("PIN", f"p_{i}"), (f"u_{i}", "a")]}
        for i in range(n)
    ]
    entities: list[dict[str, Any]] = [
        {
            "name": "splitter" if i % 3 == 0 else ("pipe" if i % 3 == 1 else "transport-belt"),
            "position": {"x": float(i * 4 + 0.5), "y": 5.5},
            "direction": def2bp.DIRECTION_NORTH,
            "fluid": "water" if i % 3 == 1 else "",
        }
        for i in range(n)
    ]
    def_data: dict[str, Any] = {"units": 1000, "components": comps, "pins": pins, "nets": nets}
    netlist_data: dict[str, Any] = {
        "fluids": ["water"],
        "instances": [{"name": f"u_{i}", "cell": "belt_merge2"} for i in range(n)],
        "cells": {"belt_merge2": {"inputs": ["a", "b"], "outputs": ["y"]}},
    }
    placement = PlacementResult(entities, set(), {}, {}, {"water"}, True)
    yield
    _obs, pin_dict, val_in, val_out = def2bp._extract_pins_and_obstacles(def_data, placement, 0.001)
    clean_fluids = def2bp._extract_clean_fluids(netlist_data, placement)
    comp_map, inst_map, cell_map = def2bp._extract_lookup_maps(def_data, netlist_data)
    specs: list[def2bp.RouterNetSpec] = []
    for net_obj in nets:
        specs.extend(
            def2bp._build_net_specs_for_net(
                net_obj, comp_map, inst_map, cell_map, pin_dict, val_in, 0.001, clean_fluids
            )
        )
    def2bp._collect_router_constraints(specs, entities, val_in, val_out)


def perf_build_surface_tile_maps(
    n: int = 64,
    *,
    _scales: tuple[int, ...] = (64, 128, 256, 512),
    _max_complexity: str = "O(N log N)",
    _rounds: int = 3,
) -> Iterator[None]:
    """Scale benchmark for surface tile map extraction from routed net results."""
    from router.types import RouteNode

    routes = {
        f"iron_plate__mst_{i}": mock.Mock(
            nodes=[RouteNode(i * 2 + k, 10, 0, def2bp.DIRECTION_EAST, 0) for k in range(4)]
        )
        for i in range(n)
    }
    yield
    def2bp._build_surface_tile_maps(routes)


if __name__ == "__main__":
    unittest.main()
