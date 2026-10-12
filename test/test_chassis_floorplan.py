"""Unit tests for chassis OpenROAD floorplanning and placement blockages."""

from __future__ import annotations

import unittest
from typing import Any

from foundry.chassis import CityBlockChassis
from foundry.def_parser import parse_def


SAMPLE_DEF = """VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN test_design ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 50000 50000 ) ;

COMPONENTS 2 ;
    - inst1 assembler ( 10000 10000 ) N ;
    - inst2 inserter ( 20000 20000 ) N ;
END COMPONENTS

NETS 1 ;
    - net1 ( inst1 out ) ( inst2 in ) ;
END NETS

END DESIGN
"""

SAMPLE_DEF_WITH_BLOCKAGES = """VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN test_design ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 100000 100000 ) ;

BLOCKAGES 2 ;
    - PLACEMENT RECT ( 0 0 ) ( 100000 3000 ) ;
    - PLACEMENT RECT ( 0 97000 ) ( 100000 100000 ) ;
END BLOCKAGES

COMPONENTS 1 ;
    - inst1 assembler ( 10000 10000 ) N ;
END COMPONENTS

END DESIGN
"""


class TestChassisFloorplan(unittest.TestCase):
    """Test suite for chassis floorplanning, blockages, and core placement checks."""

    def setUp(self) -> None:
        self.chassis = CityBlockChassis(
            pitch=100,
            core_width=94,
            core_height=94,
            channel_width=6,
        )

    def test_parse_def_blockages(self) -> None:
        """Verify DEF parser extracts placement blockages and die area."""
        parsed = parse_def(SAMPLE_DEF_WITH_BLOCKAGES)
        self.assertEqual(parsed["units"], 1000)
        self.assertEqual(parsed["die_area"], {"x1": 0, "y1": 0, "x2": 100000, "y2": 100000})

        blockages = parsed["blockages"]
        self.assertEqual(len(blockages), 2)
        self.assertEqual(blockages[0], {"x1": 0, "y1": 0, "x2": 100000, "y2": 3000})
        self.assertEqual(blockages[1], {"x1": 0, "y1": 97000, "x2": 100000, "y2": 100000})

    def test_emit_floorplan_tcl_single_block(self) -> None:
        """Verify generated TCL floorplan for single 1x1 city block."""
        tcl = self.chassis.emit_floorplan_tcl(1, 1)
        self.assertIn("initialize_floorplan -die_area [list 0 0 100 100]", tcl)
        self.assertIn("-core_area [list 3 3 97 97]", tcl)
        self.assertIn("-site tile", tcl)

        # Verify margin blockages
        self.assertIn("create_blockage -region [list 0 0 100 3]", tcl)
        self.assertIn("create_blockage -region [list 0 97 100 100]", tcl)
        self.assertIn("create_blockage -region [list 0 0 3 100]", tcl)
        self.assertIn("create_blockage -region [list 97 0 100 100]", tcl)

    def test_emit_floorplan_tcl_multi_block(self) -> None:
        """Verify generated TCL floorplan for 2x2 city block grid."""
        tcl = self.chassis.emit_floorplan_tcl(2, 2)
        self.assertIn("initialize_floorplan -die_area [list 0 0 200 200]", tcl)
        self.assertIn("-core_area [list 3 3 197 197]", tcl)

        # Internal street channels (width 6 between y=97 and y=103, x=97 and x=103)
        self.assertIn("create_blockage -region [list 0 97 200 103]", tcl)
        self.assertIn("create_blockage -region [list 97 0 103 200]", tcl)

    def test_emit_floorplan_tcl_empty_middle_block(self) -> None:
        """Verify generated TCL floorplan blocks empty middle blocks."""
        tcl = self.chassis.emit_floorplan_tcl(3, 3, empty_blocks={(1, 1)})
        # Center core (1, 1) has bounds [103 103 197 197]
        self.assertIn("create_blockage -region [list 103 103 197 197]", tcl)

    def test_emit_def_blockages(self) -> None:
        """Verify DEF blockages section format and DBU scaling."""
        def_blockages = self.chassis.emit_def_blockages(1, 1, dbu=1000)
        self.assertTrue(def_blockages.startswith("BLOCKAGES 4 ;"))
        self.assertTrue(def_blockages.strip().endswith("END BLOCKAGES"))
        self.assertIn("- PLACEMENT RECT ( 0 0 ) ( 100000 3000 ) ;", def_blockages)
        self.assertIn("- PLACEMENT RECT ( 0 97000 ) ( 100000 100000 ) ;", def_blockages)

    def test_apply_floorplan_to_def(self) -> None:
        """Verify applying chassis floorplan updates DIEAREA and inserts BLOCKAGES."""
        updated = self.chassis.apply_floorplan_to_def(SAMPLE_DEF, grid_w=1, grid_h=1)
        parsed = parse_def(updated)

        self.assertEqual(parsed["die_area"], {"x1": 0, "y1": 0, "x2": 100000, "y2": 100000})
        self.assertEqual(len(parsed["blockages"]), 4)

    def test_verify_cells_in_cores_success(self) -> None:
        """Verify placement check passes when all cells sit inside core areas."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {"name": "inst1", "macro": "assembler", "x": 10000, "y": 10000},
                {"name": "inst2", "macro": "inserter", "x": 20000, "y": 20000},
            ],
        }
        netlist_data: dict[str, Any] = {
            "cells": {
                "assembler": {"width": 3, "height": 3},
                "inserter": {"width": 1, "height": 1},
            }
        }
        violations = self.chassis.verify_cells_in_cores(
            def_data, grid_w=1, grid_h=1, netlist_data=netlist_data
        )
        self.assertEqual(len(violations), 0)

    def test_verify_cells_in_cores_violation_margin(self) -> None:
        """Verify placement check detects component placed in outer margin."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {"name": "bad_inst", "macro": "inserter", "x": 1000, "y": 1000},
            ],
        }
        netlist_data: dict[str, Any] = {
            "cells": {
                "inserter": {"width": 1, "height": 1},
            }
        }
        violations = self.chassis.verify_cells_in_cores(
            def_data, grid_w=1, grid_h=1, netlist_data=netlist_data
        )
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["component"], "bad_inst")

    def test_verify_cells_in_cores_violation_street_channel(self) -> None:
        """Verify placement check detects component placed in street transit channel."""
        # In a 2x2 grid, street channel is between x=97 and x=103
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {"name": "street_inst", "macro": "inserter", "x": 98000, "y": 50000},
            ],
        }
        violations = self.chassis.verify_cells_in_cores(
            def_data, grid_w=2, grid_h=2
        )
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["component"], "street_inst")

    def test_verify_cells_in_cores_violation_boundary_spill(self) -> None:
        """Verify placement check detects component crossing from core into channel."""
        # Core edge is at x=97. Cell starts at x=95 with width=3 -> ends at x=98.
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {"name": "spill_inst", "macro": "assembler", "x": 95000, "y": 50000},
            ],
        }
        netlist_data: dict[str, Any] = {
            "cells": {
                "assembler": {"width": 3, "height": 3},
            }
        }
        violations = self.chassis.verify_cells_in_cores(
            def_data, grid_w=1, grid_h=1, netlist_data=netlist_data
        )
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["component"], "spill_inst")

    def test_openroad_chassis_placement_end_to_end(self) -> None:
        """Verify OpenROAD placement with USE_CHASSIS puts all cells strictly in core."""
        import json
        from pathlib import Path

        def_path = Path("flow/automation_science_chassis.def")
        json_path = Path("flow/automation_science_netlist.json")
        if not def_path.exists() or not json_path.exists():
            self.skipTest("OpenROAD generated DEF/JSON fixtures not found")

        def_data = parse_def(def_path.read_text(encoding="utf-8"))
        netlist_data = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertGreater(len(def_data["components"]), 0)
        self.assertEqual(len(def_data["blockages"]), 4)

        violations = self.chassis.verify_cells_in_cores(
            def_data, grid_w=1, grid_h=1, netlist_data=netlist_data
        )
        self.assertEqual(
            violations,
            [],
            f"Expected zero placement violations in street channels, got: {violations}",
        )

    def test_verify_cells_in_cores_3x3(self) -> None:
        """Verify placement check passes for 3x3 layout with cells in cores."""
        def_data: dict[str, Any] = {
            "units": 1000,
            "components": [
                {"name": "inst_blk00", "macro": "furnace", "x": 10000, "y": 10000},
                {"name": "inst_blk10", "macro": "furnace", "x": 110000, "y": 10000},
                {"name": "inst_blk20", "macro": "assembler", "x": 210000, "y": 10000},
                {"name": "inst_blk21", "macro": "assembler", "x": 210000, "y": 110000},
            ],
        }
        netlist_data: dict[str, Any] = {
            "cells": {
                "furnace": {"width": 3, "height": 3},
                "assembler": {"width": 3, "height": 3},
            }
        }
        violations = self.chassis.verify_cells_in_cores(
            def_data, grid_w=3, grid_h=3, netlist_data=netlist_data
        )
        self.assertEqual(len(violations), 0)


