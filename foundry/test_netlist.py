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

    def test_stage_rank_of_and_assign_gaa_pin_faces(self) -> None:
        """Verify stage_rank_of depth calculation and assign_gaa_pin_faces boundary rules."""
        self.assertEqual(netlist.stage_rank_of("iron-plate"), 0)
        depths = {"iron-plate": 1, "copper-plate": 2}
        self.assertEqual(netlist.stage_rank_of("iron-plate", material_depths=depths), 1)
        self.assertEqual(
            netlist.stage_rank_of(
                "electronic-circuit",
                inputs=["iron-plate", "copper-plate"],
                material_depths=depths,
            ),
            3,
        )

        # Default allow_east=False uses only W, N, S faces (3 faces).
        inputs_4 = ["p1", "p2", "p3", "p4"]
        faces_no_east = netlist.assign_gaa_pin_faces(
            inputs_4, depths={"p1": 0, "p2": 0, "p3": 0, "p4": 0}
        )
        self.assertNotIn("E", faces_no_east.values())
        self.assertEqual(faces_no_east["p4"], "W")

        # allow_east=True includes E face on the 4th input.
        faces_with_east = netlist.assign_gaa_pin_faces(
            inputs_4, depths={"p1": 0, "p2": 0, "p3": 0, "p4": 0}, allow_east=True
        )
        self.assertEqual(faces_with_east["p4"], "E")

        # Feeder coordinates boundary checks around mach_y +/- 1.0.
        feeder = {
            "north_port": (0.0, 10.5),
            "north_boundary": (0.0, 10.0),
            "south_port": (0.0, 7.5),
            "south_boundary": (0.0, 8.0),
            "west_port": (0.0, 9.0),
        }
        coord_faces = netlist.assign_gaa_pin_faces(
            list(feeder.keys()), feeder_coords=feeder, mach_y=9.0
        )
        self.assertEqual(coord_faces["north_port"], "N")
        self.assertEqual(coord_faces["north_boundary"], "W")
        self.assertEqual(coord_faces["south_port"], "S")
        self.assertEqual(coord_faces["south_boundary"], "W")
        self.assertEqual(coord_faces["west_port"], "W")

    def test_scaling_modes_and_fluid_headroom(self) -> None:
        """Verify single-pack, multi-pack, and 6-pack scaling and fluid multi-output headroom."""
        # Single pack vs 2-pack vs 5-pack vs 6-pack scaling modes.
        # In 2-pack or 5-pack demand, scale_for_throughput is False (uses crafts_per_minute).
        # In 1-pack or 6-pack demand, scale_for_throughput is True (uses effective_crafts_per_minute).
        # Use a cloned Data where copper-cable requires 20 copper-plate per craft so it is inserter-port-limited
        # (effective_crafts_per_minute < crafts_per_minute).
        import copy

        lim_data = copy.copy(self.data)
        lim_data.recipes = dict(self.data.recipes)
        lim_data.recipes["copper-cable"] = dict(self.data.recipes["copper-cable"])
        lim_data.recipes["copper-cable"]["ingredients"] = [
            {"type": "item", "name": "copper-plate", "amount": 20}
        ]
        nl_two_packs = netlist.build(
            lim_data,
            {"automation-science-pack": 60.0, "logistic-science-pack": 60.0},
            self.allowed,
        )
        nl_five_packs = netlist.build(
            lim_data,
            {
                "automation-science-pack": 60.0,
                "logistic-science-pack": 60.0,
                "military-science-pack": 60.0,
                "chemical-science-pack": 60.0,
                "production-science-pack": 60.0,
            },
            self.allowed,
        )
        nl_six = netlist.build(
            lim_data,
            {
                "automation-science-pack": 60.0,
                "logistic-science-pack": 60.0,
                "military-science-pack": 60.0,
                "chemical-science-pack": 60.0,
                "production-science-pack": 60.0,
                "utility-science-pack": 60.0,
            },
            self.allowed,
        )
        cc_one = [
            i
            for i in netlist.build(
                lim_data, {"logistic-science-pack": 60.0}, self.allowed
            ).instances
            if i.recipe == "copper-cable"
        ]
        cc_two = [i for i in nl_two_packs.instances if i.recipe == "copper-cable"]
        cc_five = [i for i in nl_five_packs.instances if i.recipe == "copper-cable"]
        cc_six = [i for i in nl_six.instances if i.recipe == "copper-cable"]
        self.assertEqual(cc_two[0].rated, cc_five[0].rated)
        self.assertLess(cc_six[0].rated, cc_five[0].rated)
        # Verify count also scales with effective_crafts_per_minute when scale_for_throughput is True.
        self.assertGreater(len(cc_one), len(cc_two))

        # Test machine_override as dict and str.
        nl_ovr_str = netlist.build(
            self.data,
            {"iron-gear-wheel": 30.0},
            {"iron-gear-wheel", "iron-plate"},
            machine_override="assembling-machine-2",
        )
        gear_insts = [i for i in nl_ovr_str.instances if i.recipe == "iron-gear-wheel"]
        self.assertTrue(all("assembling-machine-2" in i.cell_key for i in gear_insts))

        nl_ovr_dict = netlist.build(
            self.data,
            {"iron-gear-wheel": 30.0},
            {"iron-gear-wheel", "iron-plate"},
            machine_override={"iron-gear-wheel": "assembling-machine-1"},
        )
        gear_insts_dict = [i for i in nl_ovr_dict.instances if i.recipe == "iron-gear-wheel"]
        self.assertTrue(all("assembling-machine-1" in i.cell_key for i in gear_insts_dict))

        # Single-pack fluid multi-output recipe (advanced-oil-processing has fluids and >1 outputs).
        nl_chem_high = netlist.build(self.data, {"chemical-science-pack": 60.0}, self.allowed)
        aop_high = [i for i in nl_chem_high.instances if i.recipe == "advanced-oil-processing"]
        nl_chem_low = netlist.build(self.data, {"chemical-science-pack": 5.0}, self.allowed)
        aop_low = [i for i in nl_chem_low.instances if i.recipe == "advanced-oil-processing"]
        # Verify 2-pack demand does not apply single-pack fluid headroom bump even at high load.
        nl_two_chem = netlist.build(
            self.data,
            {"chemical-science-pack": 60.0, "automation-science-pack": 1.0},
            self.allowed,
        )
        aop_two = [i for i in nl_two_chem.instances if i.recipe == "advanced-oil-processing"]
        # At 60/min chemical science, single-pack adds +1 fluid headroom machine over 2-pack plan count.
        self.assertEqual(len(aop_high), len(aop_two) + 1)
        self.assertEqual(len(aop_low), 1)

    def test_high_throughput_driver_partition_and_pow2_promotion(self) -> None:
        """Verify driver count bump for multi-consumer sub-bus partitioning and power-of-2 promotion."""
        import copy
        import math

        import pdk

        tech = pdk.Pdk(self.data)
        belt_tiers = tech.belt_tiers()
        belt_cap = (
            float(belt_tiers[0]["items_per_lane"]) * pdk.LANES_PER_BELT * pdk.TICKS_PER_SECOND
        )
        # Find copper-plate furnace effective crafts per minute dynamically from prototype data.
        cp_proto = self.data.recipes["copper-plate"]
        cp_machine, _ = self.data.machine_for(cp_proto)
        cp_cell = tech.cell(cp_machine, "copper-plate")
        assert cp_cell is not None
        drv_cap = float(cp_cell.get("effective_crafts_per_minute", cp_cell["crafts_per_minute"]))
        max_drvs_bus = max(1, int((0.90 * belt_cap) // drv_cap))

        # Construct a single-pack demand where copper-plate has two consumers:
        # 1) whole_d in [0.50 * b_max_cap, b_max_cap] with fractional drivers
        # 2) split_ds[0] > b_max_cap with k_split >= 2 and fractional drivers
        custom_data = copy.copy(self.data)
        custom_data.recipes = dict(self.data.recipes)
        whole_target_cp = (max_drvs_bus - 0.75) * drv_cap
        split_target_cp = (max_drvs_bus + 0.50) * drv_cap
        custom_data.recipes["automation-science-pack"] = dict(
            self.data.recipes["automation-science-pack"]
        )
        custom_data.recipes["automation-science-pack"]["ingredients"] = [
            {"type": "item", "name": "low-density-structure", "amount": 1},
            {"type": "item", "name": "copper-cable", "amount": 2},
        ]
        custom_data.recipes["low-density-structure"] = dict(
            self.data.recipes["low-density-structure"]
        )
        custom_data.recipes["low-density-structure"]["ingredients"] = [
            {"type": "item", "name": "copper-plate", "amount": whole_target_cp / 10.0}
        ]
        custom_data.recipes["copper-cable"] = dict(self.data.recipes["copper-cable"])
        custom_data.recipes["copper-cable"]["ingredients"] = [
            {"type": "item", "name": "copper-plate", "amount": split_target_cp / 10.0}
        ]
        nl_part = netlist.build(custom_data, {"automation-science-pack": 10.0}, self.allowed)
        cp_part_insts = [i for i in nl_part.instances if i.recipe == "copper-plate"]
        unpartitioned_count = math.ceil((whole_target_cp + split_target_cp) / drv_cap - 1e-9)
        partitioned_count = math.ceil(whole_target_cp / drv_cap - 1e-9) + math.ceil(
            split_target_cp / drv_cap - 1e-9
        )
        self.assertEqual(partitioned_count, unpartitioned_count + 1)
        self.assertEqual(len(cp_part_insts), partitioned_count)

        # Verify exact integer multiple of drv_cap in split_ds[0] does not over-allocate drivers.
        exact_drv_data = copy.copy(self.data)
        exact_drv_data.recipes = dict(self.data.recipes)
        exact_whole = (max_drvs_bus - 0.5) * drv_cap
        exact_split = (max_drvs_bus + 1.0) * drv_cap
        exact_drv_data.recipes["automation-science-pack"] = dict(
            self.data.recipes["automation-science-pack"]
        )
        exact_drv_data.recipes["automation-science-pack"]["ingredients"] = [
            {"type": "item", "name": "low-density-structure", "amount": 1},
            {"type": "item", "name": "copper-cable", "amount": 2},
        ]
        exact_drv_data.recipes["low-density-structure"] = dict(
            self.data.recipes["low-density-structure"]
        )
        exact_drv_data.recipes["low-density-structure"]["ingredients"] = [
            {"type": "item", "name": "copper-plate", "amount": exact_whole / 10.0}
        ]
        exact_drv_data.recipes["copper-cable"] = dict(self.data.recipes["copper-cable"])
        exact_drv_data.recipes["copper-cable"]["ingredients"] = [
            {"type": "item", "name": "copper-plate", "amount": exact_split / 10.0}
        ]
        nl_exact_drv = netlist.build(
            exact_drv_data, {"automation-science-pack": 10.0}, self.allowed
        )
        cp_exact_drv_insts = [i for i in nl_exact_drv.instances if i.recipe == "copper-plate"]
        self.assertEqual(len(cp_exact_drv_insts), 2 * max_drvs_bus + 1)

        # At low rate (automation-science-pack: 0.3), total copper-plate rate is below belt_capacity, so no partition bump.
        nl_part_low = netlist.build(custom_data, {"automation-science-pack": 0.3}, self.allowed)
        cp_low_insts = [i for i in nl_part_low.instances if i.recipe == "copper-plate"]
        low_unpart = math.ceil(0.03 * (whole_target_cp + split_target_cp) / drv_cap - 1e-9)
        self.assertEqual(len(cp_low_insts), low_unpart)

        # Power-of-two promotion check: in military-science-pack at 60/min, netlist.build
        # promotes piercing-rounds-magazine count to a power of two for balanced splitter trees.
        nl_mil = netlist.build(self.data, {"military-science-pack": 60.0}, self.allowed)
        prm_insts = [i for i in nl_mil.instances if i.recipe == "piercing-rounds-magazine"]
        self.assertEqual(len(prm_insts) & (len(prm_insts) - 1), 0)

    def test_insert_telemetry_and_cell_helpers(self) -> None:
        """Verify cell record helpers and telemetry insertion scopes."""
        self.assertEqual(netlist.balancer_cell()["cell"], netlist.BALANCER_CELL)
        self.assertEqual(netlist.split_cell()["cell"], netlist.SPLIT_CELL)
        self.assertEqual(netlist.merge_cell()["cell"], netlist.MERGE_CELL)
        self.assertEqual(netlist.pump_cell()["cell"], netlist.PUMP_CELL)

        nl_tel = netlist.build(self.data, {"automation-science-pack": 60.0}, self.allowed)
        merges = netlist.merge_plans_of(nl_tel)
        self.assertEqual(netlist.insert_telemetry(nl_tel, tap_scope="none"), [])
        self.assertEqual(
            netlist.insert_telemetry(nl_tel, enable_display=False, enable_circuit=False), []
        )
        recs = netlist.insert_telemetry(
            nl_tel,
            tap_scope="io,merges,iron_gear_wheel",
            enable_display=True,
            enable_circuit=True,
            merge_plans=merges,
        )
        tapped_mats = {r["material"] for r in recs}
        self.assertIn("iron-ore", tapped_mats)
        self.assertIn("iron-gear-wheel", tapped_mats)
        v_out = netlist.emit_verilog(nl_tel, merges)
        self.assertIn("u_disp_iron_ore", v_out)
        self.assertIn("u_acc_iron_ore", v_out)


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
