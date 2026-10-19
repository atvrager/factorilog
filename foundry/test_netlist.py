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
        self.assertIn("coal", names)
        self.assertIn("water", names)

    def test_power_plant_instances_and_steam_nets(self) -> None:
        boilers = [i for i in self.nl.instances if i.recipe == "boiler-steam"]
        engines = [i for i in self.nl.instances if i.recipe == "steam-power"]
        self.assertGreater(len(boilers), 0)
        self.assertGreater(len(engines), 0)
        self.assertLessEqual(len(engines), 2 * len(boilers))

        # Verify automation-science-pack with 3x3 chassis synthesizes enough steam engines
        # to cover 100% of integer placed machines + inserters + 3x3 chassis base entities.
        chassis_w = solve.chassis_power_watts(self.data, 3, 3)
        nl_auto_chassis = netlist.build(
            self.data,
            {"automation-science-pack": 60.0},
            self.allowed,
            extra_power_watts=chassis_w,
        )
        se_insts = [i for i in nl_auto_chassis.instances if i.recipe == "steam-power"]
        b_insts = [i for i in nl_auto_chassis.instances if i.recipe == "boiler-steam"]
        se_watts = float(self.data.recipes["steam-power"]["power_output_watts"])
        total_supply_watts = len(se_insts) * se_watts

        placed_mach_watts = 0.0
        inserter_ports = 0
        for inst in nl_auto_chassis.instances:
            if inst.recipe in ("boiler-steam", "steam-power"):
                inserter_ports += 1 if inst.recipe == "boiler-steam" else 0
                continue
            cell = nl_auto_chassis.cells[inst.cell_key]
            m_name = cell["machine"]
            m_proto = self.data.machines[m_name]
            placed_mach_watts += solve.machine_peak_watts(m_proto)
            inserter_ports += len(cell["inputs"]) + len(cell["outputs"])
        ins_sustained = solve.inserter_peak_watts(self.data)
        expected_load = placed_mach_watts + inserter_ports * ins_sustained + chassis_w
        self.assertGreaterEqual(total_supply_watts, expected_load)
        self.assertEqual(len(se_insts), 9)
        self.assertEqual(len(b_insts), 5)

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

    def test_net_rate_per_min_not_multiplied_by_instance_count(self) -> None:
        """Internal net rate_per_min equals total item production rate, not count * rate."""
        nl_log = netlist.build(self.data, {"logistic-science-pack": 60.0}, self.allowed)
        self.assertAlmostEqual(nl_log.nets["iron-plate"].rate_per_min, 330.0, places=2)
        self.assertAlmostEqual(nl_log.nets["iron-gear-wheel"].rate_per_min, 90.0, places=2)

    def test_demand_weighted_splitter_tree_balances_branches(self) -> None:
        """Heavy sink (>50% of net) gets dedicated sub-bus and remaining bus balances by demand."""
        nl_log = netlist.build(self.data, {"logistic-science-pack": 60.0}, self.allowed)
        merge_plans = netlist.merge_plans_of(nl_log)
        split_plans = netlist.split_plans_of(nl_log)
        ip_merges = merge_plans["iron-plate"]
        self.assertTrue(
            any(m[3] == "iron_plate__u_iron_gear_wheel_1" for m in ip_merges),
            f"Expected dedicated merge sub-bus for u_iron_gear_wheel_1, got {ip_merges}",
        )
        ip_plan = split_plans["iron-plate"]
        root_entry = next(e for e in ip_plan if e[1] == "iron_plate__bus1")
        _name, _in_w, y1_w, y2_w = root_entry
        self.assertIn("iron_plate__u_electronic_circuit_1", (y1_w, y2_w))

    def test_multibus_intermediate_nets_under_belt_capacity(self) -> None:
        """High-rate internal solid nets partition into multiple sub-buses under belt capacity."""
        nl_prod = netlist.build(self.data, {"production-science-pack": 60.0}, self.allowed)
        merge_plans = netlist.merge_plans_of(nl_prod)
        split_plans = netlist.split_plans_of(nl_prod)
        bal_plans = netlist.balancer_plans_of(nl_prod)
        for mat in ("iron-plate", "copper-plate", "copper-cable"):
            net = nl_prod.nets[mat]
            self.assertGreater(net.rate_per_min, nl_prod.belt_capacity)
            self.assertGreaterEqual(len(net.sub_buses), 2, f"{mat} was not partitioned into buses")
        if HAVE_PYSLANG:
            v_text = netlist.emit_verilog(nl_prod, merge_plans, split_plans, bal_plans)
            c_text = netlist.emit_cells_v(nl_prod)
            compilation = Compilation()
            compilation.addSyntaxTree(SyntaxTree.fromText(c_text))
            compilation.addSyntaxTree(SyntaxTree.fromText(v_text))
            all_diags = list(compilation.getAllDiagnostics())
            errors = [d for d in all_diags if d.isError if hasattr(d, "isError")]
            self.assertEqual(errors, [], _report(compilation.sourceManager, errors))

    def test_seven_pack_lab_netlist(self) -> None:
        """Full 7-pack science demand feeds 18 labs and emits 0 module outputs."""
        packs = [
            solve.name_of(e)
            for e in solve.Data.ingredients_of(self.data.recipes["infinite-research"])
        ]
        full_demand = dict.fromkeys(packs, 60.0)
        nl_full = netlist.build(self.data, full_demand, self.allowed)
        self.assertEqual(nl_full.outputs, [])
        labs = [i for i in nl_full.instances if i.recipe == "infinite-research"]
        self.assertEqual(len(labs), 18)
        merge_plans = netlist.merge_plans_of(nl_full)
        split_plans = netlist.split_plans_of(nl_full)
        bal_plans = netlist.balancer_plans_of(nl_full)
        for pack in packs:
            self.assertIn(pack, split_plans)
            self.assertEqual(len(split_plans[pack]), 17)
        ranks_tcl = netlist.emit_ranks_tcl(nl_full, merge_plans, split_plans, bal_plans)
        self.assertIn("u_infinite_research_18", ranks_tcl)
        from analytical_placer import AnalyticalPlacer

        placer = AnalyticalPlacer()
        auto_w, auto_h = placer.compute_auto_chassis_grid(
            nl_full,
            merge_plans=merge_plans,
            split_plans=split_plans,
            balancer_plans=bal_plans,
        )
        self.assertEqual((auto_w, auto_h), (12, 12))
        if HAVE_PYSLANG:
            v_text = netlist.emit_verilog(nl_full, merge_plans, split_plans, bal_plans)
            c_text = netlist.emit_cells_v(nl_full)
            compilation = Compilation()
            compilation.addSyntaxTree(SyntaxTree.fromText(c_text))
            compilation.addSyntaxTree(SyntaxTree.fromText(v_text))
            all_diags = list(compilation.getAllDiagnostics())
            errors = [d for d in all_diags if d.isError if hasattr(d, "isError")]
            self.assertEqual(errors, [], _report(compilation.sourceManager, errors))


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
