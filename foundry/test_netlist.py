#!/usr/bin/env python3
"""test_netlist.py - the plan lowered to Verilog, parsed back by pyslang.

usage: test_netlist.py <dump-fixture.json>
"""

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netlist
import solve

# pyslang moved its API between major versions. Support both layouts.
try:
    import pyslang

    _SYNTAX = getattr(pyslang, "syntax", pyslang)
    _AST = getattr(pyslang, "ast", pyslang)
    SyntaxTree = _SYNTAX.SyntaxTree
    Compilation = _AST.Compilation
    DiagnosticEngine = pyslang.DiagnosticEngine
    HAVE_PYSLANG = True
except ImportError:
    HAVE_PYSLANG = False


class NetlistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = solve.Data(sys.argv[1] if len(sys.argv) > 1 else "unused")
        cls.allowed = cls.data.allowed_recipes(["nauvis"])
        demand = {
            "automation-science-pack": 60.0,
            "logistic-science-pack": 60.0,
        }
        cls.nl = netlist.build(cls.data, demand, cls.allowed)
        cls.plans = netlist.merge_plans_of(cls.nl)
        cls.text = netlist.emit_verilog(cls.nl, cls.plans)
        cls.cells_text = netlist.emit_cells_v(cls.nl)

    def test_machine_instances_match_plan(self) -> None:
        # 60 packs/min needs 4 assemblers at 15/min rated. Merge cells do
        # not count: they are transport, not machines.
        packs = [
            i
            for i in self.nl.instances
            if i.recipe == "automation-science-pack" and i.cell_key != netlist.MERGE_CELL
        ]
        self.assertEqual(len(packs), 4)
        # The fleet shares the recipe rate: 60/min over 4 machines.
        for inst in packs:
            self.assertAlmostEqual(inst.plan_rate, 15.0, places=4)
            self.assertAlmostEqual(inst.rated, 15.0, places=4)
            self.assertLessEqual(inst.plan_rate, inst.rated + 1e-9)

    def test_raw_items_are_inputs(self) -> None:
        names = [netlist.sanitize(m) for m in self.nl.inputs]
        self.assertIn("iron_ore", names)
        self.assertIn("copper_ore", names)

    def test_single_driver_nets(self) -> None:
        for material, net in self.nl.nets.items():
            self.assertLessEqual(len(net.drivers), 1, f"{material} has {net.drivers}")

    def test_verilog_parses_and_elaborates(self) -> None:
        if not HAVE_PYSLANG:
            self.skipTest("pyslang not installed")
        errors = [d for d in SyntaxTree.fromText(self.text).diagnostics if d.isError]
        self.assertEqual(errors, [], _report(None, errors))
        errors = [d for d in SyntaxTree.fromText(self.cells_text).diagnostics if d.isError]
        self.assertEqual(errors, [], _report(None, errors))
        # The netlist instantiates the library cells. Elaborate together.
        compilation = Compilation()
        compilation.addSyntaxTree(SyntaxTree.fromText(self.cells_text))
        compilation.addSyntaxTree(SyntaxTree.fromText(self.text))
        all_diags = list(compilation.getAllDiagnostics())
        errors = [d for d in all_diags if d.isError if hasattr(d, "isError")]
        self.assertEqual(errors, [], _report(compilation.sourceManager, errors))

    def test_netlist_is_flat(self) -> None:
        """The design netlist holds the top module only."""
        modules = [line for line in self.text.splitlines() if line.startswith("module ")]
        self.assertEqual(modules, [f"module {self.nl.label} ("])

    def test_satellite_six_input_gaa_pins(self) -> None:
        """Verify 6-input satellite recipe gets non-overlapping GAA pin coordinates."""
        import lef

        nl_sat = netlist.build(self.data, {"satellite": 1.0}, {"satellite"})
        cell = nl_sat.cells["assembling-machine-3.satellite"]
        self.assertEqual(len(cell["inputs"]), 6)
        inserter_coords = set()
        approach_coords = set()
        for port in cell["inputs"]:
            geom = lef.get_macro_port_geometry(cell, port, is_output=False)
            inserter_coords.add(geom["inserter_pos"])
            approach_coords.add(geom["approach_pos"])
        self.assertEqual(len(inserter_coords), 6)
        self.assertEqual(len(approach_coords), 6)


def _report(manager: Any, diags: list) -> str:
    if not diags:
        return ""
    engine = DiagnosticEngine(manager)
    pieces = []
    for diag in diags[:5]:
        if hasattr(engine, "format"):
            pieces.append(engine.format(diag))
        else:
            pieces.append(DiagnosticEngine.reportAll(manager, [diag]))
    return "\n".join(str(p) for p in pieces)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
