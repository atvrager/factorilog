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
  + ROUTED M1 ( 13000 21500 ) ( 30000 * )
  NEW M2 ( 30000 21500 ) ( * 40500 )
  NEW M2 ( 30000 21500 ) TRANSPORT_BELT_VIA
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
        entities = def2bp.build_entities(def_data, MINI_NETLIST)

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

        # Check splitter coordinates (origin 30, 40 + 0.5, 1.0 -> 30.5, 41.0)
        spl = next(e for e in entities if e["name"] == "splitter")
        self.assertEqual(spl["position"], {"x": 30.5, "y": 41.0})
        self.assertEqual(spl["direction"], def2bp.DIRECTION_EAST)

        # Check entity numbering is 1-based and sequential
        for idx, ent in enumerate(entities, 1):
            self.assertEqual(ent["entity_number"], idx)

    def test_def_to_blueprint_roundtrip(self) -> None:
        bp_string = def2bp.def_to_blueprint(MINI_DEF, MINI_NETLIST, label="test_rig")
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


if __name__ == "__main__":
    unittest.main()
