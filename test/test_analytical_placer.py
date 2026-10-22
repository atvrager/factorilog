"""Unit and integration tests for AnalyticalPlacer and directed legalization.

Tests quadratic wirelength minimization, West-to-East topological ordering,
halo clearances, chassis core containment, HPWL reduction, and 3D maze routing.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from foundry import netlist, solve
from foundry.analytical_placer import (
    AnalyticalPlacer,
    compute_def_hpwl,
    untangle_splitter_merger_ports,
)
from foundry.chassis import CityBlockChassis
from foundry.def2bp import build_entities
from foundry.def_parser import parse_def
from foundry.entity_helpers import check_collisions, check_underground_pairing


def _get_comp_dims(
    comp: dict[str, Any],
    inst_map: dict[str, dict[str, Any]],
    cells_map: dict[str, dict[str, Any]],
) -> tuple[int, int, int]:
    """Return (width, height, horizontal_padding) for a placed component."""
    cname = comp["name"]
    macro = inst_map.get(cname, {}).get("cell", comp.get("macro", ""))
    cmeta = cells_map.get(macro, {})
    is_small = "split" in macro or "merge" in macro
    w = int(cmeta.get("width", 1 if is_small else 3))
    h = int(cmeta.get("height", 2 if is_small else 3))
    pad = 16 if "merge" in cname else (10 if "split" in cname else 8)
    return w, h, pad


def _simulate_rank_row_placement(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
) -> dict[str, Any]:
    """Simulate legacy rank-based linear row placement on a DEF dictionary for comparison."""
    import copy

    ranked_def = copy.deepcopy(def_data)
    dbu = int(ranked_def.get("units", 1000))
    cells_map = netlist_data.get("cells", {})
    inst_map = {i["name"]: i for i in netlist_data.get("instances", [])}

    by_rank: dict[int, list[dict[str, Any]]] = {}
    for comp in ranked_def.get("components", []):
        r = int(inst_map.get(comp["name"], {}).get("rank", 0))
        by_rank.setdefault(r, []).append(comp)

    channel_h = 12
    core_margin = 8
    cur_y = core_margin + channel_h
    max_x = 30

    for r in sorted(by_rank.keys()):
        rx = core_margin + 2
        row_h = 3
        for comp in by_rank[r]:
            w, h, pad = _get_comp_dims(comp, inst_map, cells_map)
            row_h = max(row_h, h)
            comp["x"] = int(rx * dbu)
            comp["y"] = int(cur_y * dbu)
            comp["orient"] = "N"
            rx += w + pad
            max_x = max(max_x, rx)
        cur_y += row_h + channel_h

    die_w = max_x + core_margin
    die_h = cur_y + core_margin
    ranked_def["die_area"] = [0, 0, die_w * dbu, die_h * dbu]
    return ranked_def


class TestAnalyticalPlacer(unittest.TestCase):
    """Test suite for AnalyticalPlacer and router-aware directed legalization."""

    @classmethod
    def setUpClass(cls) -> None:
        fixture_candidates = [
            Path("test/data/dump-fixture.json"),
            Path(__file__).resolve().parent / "data" / "dump-fixture.json",
        ]
        dump_path = next((p for p in fixture_candidates if p.is_file()), None)
        if dump_path is None:
            raise RuntimeError("dump-fixture.json not found")
        cls.data = solve.Data(str(dump_path))
        cls.allowed = cls.data.allowed_recipes(["nauvis"])
        cls.demand = {"automation-science-pack": 60.0}
        cls.nl = netlist.build(cls.data, cls.demand, cls.allowed)
        cls.merge_plans = netlist.merge_plans_of(cls.nl)
        cls.split_plans = netlist.split_plans_of(cls.nl, synthesize_trees=True)
        cls.balancer_plans = netlist.balancer_plans_of(cls.nl)

    def setUp(self) -> None:
        self.placer = AnalyticalPlacer()
        self.chassis = CityBlockChassis(pitch=100, core_width=94, core_height=94, channel_width=6)

    def test_directed_flow_and_non_overlapping_legalization(self) -> None:
        """Verify West-to-East topological ordering and zero component overlaps."""
        res = self.placer.place_netlist(
            self.nl,
            merge_plans=self.merge_plans,
            split_plans=self.split_plans,
            balancer_plans=self.balancer_plans,
            use_chassis=True,
            grid_w=1,
            grid_h=1,
        )
        nodes, edges, _ins, _outs = self.placer.build_graph_from_netlist(
            self.nl,
            self.merge_plans,
            self.split_plans,
            self.balancer_plans,
        )

        # Every instance must have legalized coordinates
        self.assertEqual(len(res.coords), len(self.nl.instances))

        # Check West-to-East flow ordering for intra-stage splitters -> machines -> mergers
        for e in edges:
            if e.source in res.coords and e.sink in res.coords:
                u_node = nodes[e.source]
                v_node = nodes[e.sink]
                if u_node.rank <= v_node.rank:
                    ux, _uy = res.coords[e.source]
                    vx, _vy = res.coords[e.sink]
                    self.assertLess(
                        ux,
                        vx,
                        f"Expected source {e.source} (x={ux}) West of sink {e.sink} (x={vx})",
                    )

        # Check zero footprint overlaps and minimum clearance halos
        placed_boxes: list[tuple[str, int, int, int, int]] = []
        for name, (ix, iy) in res.coords.items():
            nd = nodes[name]
            placed_boxes.append((name, ix, iy, ix + nd.width, iy + nd.height))

        for i in range(len(placed_boxes)):
            n1, x1_lo, y1_lo, x1_hi, y1_hi = placed_boxes[i]
            for j in range(i + 1, len(placed_boxes)):
                n2, x2_lo, y2_lo, x2_hi, y2_hi = placed_boxes[j]
                overlap_x = min(x1_hi, x2_hi) - max(x1_lo, x2_lo)
                overlap_y = min(y1_hi, y2_hi) - max(y1_lo, y2_lo)
                self.assertFalse(
                    overlap_x > 0 and overlap_y > 0,
                    f"Components {n1} and {n2} overlap: {(x1_lo, y1_lo, x1_hi, y1_hi)} vs {(x2_lo, y2_lo, x2_hi, y2_hi)}",
                )

    def test_chassis_core_containment_1x1_2x2_3x3(self) -> None:
        """Verify all cells sit strictly inside 94x94 cores across 1x1, 2x2, and 3x3 grids."""
        nodes, _edges, _ins, _outs = self.placer.build_graph_from_netlist(
            self.nl,
            self.merge_plans,
            self.split_plans,
            self.balancer_plans,
        )
        netlist_data = {
            "cells": {nd.macro: {"width": nd.width, "height": nd.height} for nd in nodes.values()}
        }

        for gw, gh in ((1, 1), (2, 2), (3, 3)):
            for distribute in (False, True):
                res = self.placer.place_netlist(
                    self.nl,
                    merge_plans=self.merge_plans,
                    split_plans=self.split_plans,
                    balancer_plans=self.balancer_plans,
                    use_chassis=True,
                    grid_w=gw,
                    grid_h=gh,
                    distribute_across_blocks=distribute,
                )
                def_data = {
                    "units": 1000,
                    "components": [
                        {
                            "name": name,
                            "macro": nodes[name].macro,
                            "x": ix * 1000,
                            "y": iy * 1000,
                        }
                        for name, (ix, iy) in res.coords.items()
                    ],
                }
                violations = self.chassis.verify_cells_in_cores(
                    def_data, grid_w=gw, grid_h=gh, netlist_data=netlist_data
                )
                self.assertEqual(
                    violations,
                    [],
                    f"Expected zero core violations on {gw}x{gh} (distribute={distribute}), got {violations}",
                )
                if distribute and gw * gh > 1:
                    used_blocks = set(res.block_assignments.values())
                    self.assertGreater(len(used_blocks), 1)
                if gw == 3 and gh == 3:
                    # Center block (1, 1) must remain empty
                    self.assertNotIn((1, 1), set(res.block_assignments.values()))

    def test_hpwl_reduction_vs_rank_row_placement(self) -> None:
        """Verify analytical placement achieves lower HPWL than rank-based linear row placement."""
        def_path = Path("flow/automation_science_chassis.def")
        json_path = Path("flow/automation_science_chassis_netlist.json")
        if not def_path.is_file() or not json_path.is_file():
            self.skipTest("automation_science fixtures not found")

        raw_def = parse_def(def_path.read_text(encoding="utf-8"))
        netlist_data = json.loads(json_path.read_text(encoding="utf-8"))

        rank_def = _simulate_rank_row_placement(raw_def, netlist_data)
        rank_hpwl = compute_def_hpwl(rank_def, netlist_data)

        analytical_def, _res = self.placer.apply_to_def_data(
            raw_def,
            netlist_data,
            use_chassis=False,
        )
        analytical_hpwl = compute_def_hpwl(analytical_def, netlist_data)

        self.assertLess(
            analytical_hpwl,
            rank_hpwl,
            f"Expected analytical HPWL ({analytical_hpwl}) < rank row HPWL ({rank_hpwl})",
        )

    def test_untangle_splitter_merger_ports(self) -> None:
        """Verify symmetric splitter and merger ports swap to eliminate wire crossings."""
        sample_def: dict[str, Any] = {
            "units": 1000,
            "components": [
                {"name": "u_split", "macro": "belt_split2", "x": 10000, "y": 20000, "orient": "N"},
                {"name": "u_low", "macro": "assembler", "x": 25000, "y": 10000, "orient": "N"},
                {"name": "u_high", "macro": "assembler", "x": 25000, "y": 30000, "orient": "N"},
            ],
            "pins": [],
            "nets": [
                {
                    "name": "net_to_high",
                    "connections": [("u_split", "y1"), ("u_high", "in")],
                    "wires": [],
                    "vias": [],
                },
                {
                    "name": "net_to_low",
                    "connections": [("u_split", "y2"), ("u_low", "in")],
                    "wires": [],
                    "vias": [],
                },
            ],
        }
        netlist_meta: dict[str, Any] = {
            "cells": {
                "belt_split2": {"width": 1, "height": 2, "inputs": ["a"], "outputs": ["y1", "y2"]},
                "assembler": {"width": 3, "height": 3, "inputs": ["in"], "outputs": ["out"]},
            },
            "instances": [
                {"name": "u_split", "cell": "belt_split2"},
                {"name": "u_low", "cell": "assembler"},
                {"name": "u_high", "cell": "assembler"},
            ],
        }
        untangle_splitter_merger_ports(sample_def, netlist_meta)
        conns_high = dict(sample_def["nets"][0]["connections"])
        conns_low = dict(sample_def["nets"][1]["connections"])
        self.assertEqual(conns_low["u_split"], "y1")
        self.assertEqual(conns_high["u_split"], "y2")

    def test_emit_analytical_tcl_preserves_ranks(self) -> None:
        """Verify emitted TCL includes both rank lists and analytical coordinate dictionaries."""
        ranks_tcl = netlist.emit_ranks_tcl(
            self.nl, self.merge_plans, self.split_plans, self.balancer_plans
        )
        analytical_tcl = self.placer.emit_analytical_tcl(
            self.nl, self.merge_plans, self.split_plans, self.balancer_plans
        )
        combined = ranks_tcl + analytical_tcl
        self.assertIn("set num_ranks", combined)
        self.assertIn("set rank_instances(0)", combined)
        self.assertIn("set analytical_coords [dict create]", combined)
        self.assertIn("set analytical_chassis_1x1 [dict create]", combined)
        self.assertIn("set analytical_chassis_2x2 [dict create]", combined)
        self.assertIn("set analytical_chassis_3x3 [dict create]", combined)

    def test_end_to_end_maze_router_on_analytical_placement(self) -> None:
        """Verify 100 percent route completion and zero DRC violations with DualLaneMazeRouter."""
        def_path = Path("flow/automation_science_chassis.def")
        json_path = Path("flow/automation_science_chassis_netlist.json")
        if not def_path.is_file() or not json_path.is_file():
            self.skipTest("automation_science fixtures not found")

        raw_def = parse_def(def_path.read_text(encoding="utf-8"))
        netlist_data = json.loads(json_path.read_text(encoding="utf-8"))

        # Test both single-core and 1x1 chassis analytical placement through full def2bp + router
        for use_chassis in (False, True):
            placed_def, _res = self.placer.apply_to_def_data(
                raw_def,
                netlist_data,
                use_chassis=use_chassis,
                grid_w=1,
                grid_h=1,
            )
            if use_chassis:
                violations = self.chassis.verify_cells_in_cores(
                    placed_def, grid_w=1, grid_h=1, netlist_data=netlist_data
                )
                self.assertEqual(violations, [])

            entities, _wires = build_entities(
                placed_def,
                netlist_data,
                strict_drc=True,
                use_maze_router=True,
            )
            self.assertGreater(len(entities), 0)
            self.assertEqual(check_collisions(entities), [])
            self.assertEqual(check_underground_pairing(entities), [])

    def test_compute_auto_chassis_grid_dimensions(self) -> None:
        """Verify auto-sizing computes minimal square city-block grids for 1 to 5 sciences."""
        expected = [
            ({"automation-science-pack": 60.0}, (1, 1)),
            ({"automation-science-pack": 60.0, "logistic-science-pack": 60.0}, (2, 2)),
            (
                {
                    "automation-science-pack": 60.0,
                    "logistic-science-pack": 60.0,
                    "military-science-pack": 60.0,
                },
                (3, 3),
            ),
            (
                {
                    "automation-science-pack": 60.0,
                    "logistic-science-pack": 60.0,
                    "military-science-pack": 60.0,
                    "chemical-science-pack": 60.0,
                },
                (4, 4),
            ),
            (
                {
                    "automation-science-pack": 60.0,
                    "logistic-science-pack": 60.0,
                    "military-science-pack": 60.0,
                    "chemical-science-pack": 60.0,
                    "production-science-pack": 60.0,
                },
                (7, 7),
            ),
        ]
        for demand, exp_grid in expected:
            nl = netlist.build(self.data, demand, self.allowed)
            mp = netlist.merge_plans_of(nl)
            sp = netlist.split_plans_of(nl, synthesize_trees=True)
            bp = netlist.balancer_plans_of(nl)
            self.assertEqual(
                self.placer.compute_auto_chassis_grid(nl, mp, sp, bp),
                exp_grid,
            )

    def test_tarjan_scc_cycle_condensation(self) -> None:
        """Verify Tarjan's SCC condenses cyclic feedback loops into a single atomic block."""
        mach_names = ["u_feed", "u_loop_a", "u_loop_b", "u_loop_c", "u_product"]
        mach_adj = {
            "u_feed": {"u_loop_a": 1.0},
            "u_loop_a": {"u_loop_b": 1.0},
            "u_loop_b": {"u_loop_c": 1.0, "u_product": 0.5},
            "u_loop_c": {"u_loop_a": 1.0},
            "u_product": {},
        }
        sccs = self.placer._find_tarjan_sccs(mach_names, mach_adj)
        loop_sccs = [set(scc) for scc in sccs if len(scc) > 1]
        self.assertEqual(loop_sccs, [{"u_loop_a", "u_loop_b", "u_loop_c"}])

    def test_boundary_pin_spreading_and_2d_block_assignment(self) -> None:
        """Verify 6x6 2D block assignment and PAV pin spreading on 5-science factory."""
        demand = {
            "automation-science-pack": 60.0,
            "logistic-science-pack": 60.0,
            "military-science-pack": 60.0,
            "chemical-science-pack": 60.0,
            "production-science-pack": 60.0,
        }
        nl = netlist.build(self.data, demand, self.allowed)
        mp = netlist.merge_plans_of(nl)
        sp = netlist.split_plans_of(nl, synthesize_trees=True)
        bp = netlist.balancer_plans_of(nl)
        res = self.placer.place_netlist(nl, mp, sp, bp, use_chassis=True, grid_w=6, grid_h=6)
        # All 6 input balancers must be placed on the West boundary column (bx == 0), 1 per block
        bal_blocks = {
            (x // 100, y // 100) for name, (x, y) in res.coords.items() if name.startswith("u_bal_")
        }
        self.assertEqual(bal_blocks, {(0, by) for by in range(6)})
        # All 6 oil refineries must be placed together in the same West boundary block (bx == 0)
        ref_blocks = {
            (x // 100, y // 100)
            for name, (x, y) in res.coords.items()
            if name.startswith("u_advanced_oil_processing_")
        }
        self.assertEqual(len(ref_blocks), 1)
        self.assertEqual(next(iter(ref_blocks))[0], 0)
        # Paired balancer pins must have a tight 4000-DBU center-to-center spacing
        self.assertEqual(
            res.pin_coords["copper_ore__bus1"][1] - res.pin_coords["copper_ore__bus0"][1],
            4000,
        )


if __name__ == "__main__":
    unittest.main()
