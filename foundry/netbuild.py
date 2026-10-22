#!/usr/bin/env python3
"""netbuild.py - dump and demand to Verilog netlist.

The synthesis front end of the flow:

    dump.json + demand -> plan (LP) -> instances + nets -> Verilog

usage:
  netbuild.py <dump.json> <item> <per_min> [item per_min ...]
  netbuild.py <dump.json> --spc <packs_per_min>

The Verilog names one module per used (building, recipe) cell, one
instance per machine, and one wire per material. `belt_merge2` joins two
producers of one material, because a wire has one driver and the game
joins lanes with a splitter.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TypeAlias

sys.path.insert(0, str(Path(__file__).resolve().parent))
import circuit_synth
import lef
import liberty
import netlist
import pdk
import solve

MergePlansMap: TypeAlias = dict[str, list[tuple[str, str, str, str]]]
SplitPlansMap: TypeAlias = dict[str, list[tuple[str, str, str, str]]]
BalancerPlansMap: TypeAlias = dict[str, list[tuple[str, str, str, str, str]]]

# The science packs of the 60 SPM reference demand.
SPM_PACKS = (
    "automation-science-pack",
    "logistic-science-pack",
    "military-science-pack",
    "chemical-science-pack",
    "production-science-pack",
    "utility-science-pack",
)

MAX_STANDARD_CHASSIS_DIM = 3


def parse_demand(argv: list[str]) -> dict[str, float]:
    """argv after the dump path -> demand as item per_min pairs."""
    if len(argv) % 2 != 0:
        raise SystemExit("demand is item per_min pairs")
    pairs = {}
    for index in range(0, len(argv), 2):
        pairs[argv[index]] = float(argv[index + 1])
    return pairs


def _compute_chassis_extra_power(
    data: solve.Data,
    demand: dict[str, float],
    allowed: set[str],
    enable_compound_cells: bool,
    synthesize_splitters: bool,
    chassis_grid_w: int,
    chassis_grid_h: int,
) -> float:
    """Calculate the extra electrical power required by the chassis grid."""
    gw, gh = chassis_grid_w, chassis_grid_h
    if gw <= 0 or gh <= 0:
        try:
            from analytical_placer import AnalyticalPlacer

            nl_pre = netlist.build(
                data, demand, allowed, enable_compound_cells=enable_compound_cells
            )
            plans_pre = netlist.merge_plans_of(nl_pre)
            splits_pre = netlist.split_plans_of(nl_pre, synthesize_trees=synthesize_splitters)
            bals_pre = netlist.balancer_plans_of(nl_pre)
            gw, gh = AnalyticalPlacer().compute_auto_chassis_grid(
                nl_pre,
                merge_plans=plans_pre,
                split_plans=splits_pre,
                balancer_plans=bals_pre,
            )
        except Exception:
            gw, gh = max(1, gw), max(1, gh)
    return solve.chassis_power_watts(data, gw, gh)


def _fuse_controller_verilog(
    nl: netlist.Netlist, controller_verilog: Path | str | None
) -> list[dict[str, Any]]:
    """Synthesize and fuse an optional Verilog controller into the netlist."""
    if not controller_verilog:
        return []
    c_path = Path(controller_verilog)
    if not c_path.is_file():
        return []
    synth_result = circuit_synth.synthesize_verilog(c_path)
    max_rank = max((inst.rank for inst in nl.instances), default=0)
    circuit_synth.fuse_controller_into_netlist(nl, synth_result, base_rank=max_rank + 1)
    return [
        {
            "source": c.source,
            "target": c.target,
            "color": c.color,
            "source_port": c.source_port,
            "target_port": c.target_port,
        }
        for c in synth_result.connections
    ]


def _append_analytical_tcl(
    ranks_text: str,
    nl: netlist.Netlist,
    plans: MergePlansMap,
    split_plans: SplitPlansMap,
    balancer_plans: BalancerPlansMap,
) -> str:
    """Append auto-chassis grid variables and analytical placement Tcl commands."""
    try:
        from analytical_placer import AnalyticalPlacer

        placer = AnalyticalPlacer()
        auto_w, auto_h = placer.compute_auto_chassis_grid(
            nl,
            merge_plans=plans,
            split_plans=split_plans,
            balancer_plans=balancer_plans,
        )
        ranks_text += (
            f"\n# Auto-calculated square-est chassis grid dimensions\n"
            f"set AUTO_CHASSIS_GRID_W {auto_w}\n"
            f"set AUTO_CHASSIS_GRID_H {auto_h}\n"
            f"set AUTO_NUM_CLUSTERS {auto_w * auto_h}\n"
        )
        extra_grids = (
            [(auto_w, auto_h)]
            if auto_w > MAX_STANDARD_CHASSIS_DIM or auto_h > MAX_STANDARD_CHASSIS_DIM
            else None
        )
        ranks_text += placer.emit_analytical_tcl(
            nl,
            merge_plans=plans,
            split_plans=split_plans,
            balancer_plans=balancer_plans,
            extra_chassis_grids=extra_grids,
        )
    except Exception as exc:
        print(f"netbuild: analytical placer warning: {exc}", file=sys.stderr)
    return ranks_text


def build_all(
    dump_path: Path | str,
    demand: dict[str, float],
    out_dir: Path | str = ".",
    tap_scope: str = "none",
    enable_display: bool = True,
    enable_circuit: bool = False,
    enable_compound_cells: bool = True,
    synthesize_splitters: bool = True,
    controller_verilog: Path | str | None = None,
    only_recipes: list[str] | None = None,
    use_chassis: bool = False,
    chassis_grid_w: int = 0,
    chassis_grid_h: int = 0,
) -> int:
    """Run full netbuild pipeline, emitting Verilog, LEF, Liberty, and JSON."""
    out = Path(out_dir)
    data = solve.Data(str(dump_path))
    tech = pdk.Pdk(data)
    allowed = set(only_recipes) if only_recipes else data.allowed_recipes(["nauvis"])
    extra_power_watts = (
        _compute_chassis_extra_power(
            data,
            demand,
            allowed,
            enable_compound_cells,
            synthesize_splitters,
            chassis_grid_w,
            chassis_grid_h,
        )
        if use_chassis
        else 0.0
    )

    nl = netlist.build(
        data,
        demand,
        allowed,
        enable_compound_cells=enable_compound_cells,
        extra_power_watts=extra_power_watts,
    )
    plans = netlist.merge_plans_of(nl)
    split_plans = netlist.split_plans_of(nl, synthesize_trees=synthesize_splitters)
    telemetry_records = netlist.insert_telemetry(
        nl, tap_scope, enable_display, enable_circuit, plans
    )
    circuit_conns = _fuse_controller_verilog(nl, controller_verilog)

    balancer_plans = netlist.balancer_plans_of(nl)
    text = netlist.emit_verilog(nl, plans, split_plans, balancer_plans)
    (out / "netlist.v").write_text(text)
    (out / "cells.v").write_text(netlist.emit_cells_v(nl))

    cells = [
        *nl.cells.values(),
        netlist.merge_cell(),
        netlist.split_cell(),
        netlist.balancer_cell(),
    ]
    lef_text = lef.write_lef(tech, cells, str(out / "tech.lef"))
    lib_text = liberty.write_liberty(cells, str(out / "tech.lib"))
    problems = lef.check_lef(cells, lef_text) + liberty.check_liberty(cells, lib_text)
    if problems:
        for problem in problems:
            print(f"pdk: {problem}", file=sys.stderr)
        return 1

    sidecar = {
        "demand": demand,
        "fluids": sorted(data.prototypes.get("fluid", {}).keys()),
        "underground_spans": tech.underground_tier_spans(),
        "inputs": nl.inputs,
        "outputs": nl.outputs,
        "raw_rates_per_min": {m: nl.nets[m].rate_per_min for m in nl.inputs},
        "cells": {
            **dict(nl.cells),
            netlist.MERGE_CELL: netlist.merge_cell(),
            netlist.SPLIT_CELL: netlist.split_cell(),
            netlist.BALANCER_CELL: netlist.balancer_cell(),
        },
        "telemetry": telemetry_records,
        "circuit_connections": circuit_conns,
        "instances": [
            {
                "name": i.name,
                "cell": i.cell_key,
                "recipe": i.recipe,
                "plan_rate_per_min": i.plan_rate,
                "rated_per_min": i.rated,
                "rank": i.rank,
                **(
                    {"control_behavior": i.control_behavior}
                    if hasattr(i, "control_behavior")
                    else {}
                ),
            }
            for i in nl.instances
        ],
    }
    (out / "netlist.json").write_text(json.dumps(sidecar, indent=2))
    ranks_text = _append_analytical_tcl(
        netlist.emit_ranks_tcl(nl, plans, split_plans, balancer_plans),
        nl,
        plans,
        split_plans,
        balancer_plans,
    )
    (out / "ranks.tcl").write_text(ranks_text)
    counts: dict[str, int] = {}
    for inst in nl.instances:
        counts[inst.cell_key] = counts.get(inst.cell_key, 0) + 1
    print(
        f"netlist.v + cells.v + netlist.json + tech.lef + tech.lib: "
        f"{len(nl.instances)} instances, {len(nl.cells)} cells, "
        f"{len(nl.nets)} nets"
    )
    for key, count in sorted(counts.items()):
        print(f"  {count:3} x {key}")
    return 0


def main() -> int:
    """Parse CLI arguments and run the netbuild synthesis pipeline."""
    parser = argparse.ArgumentParser()
    parser.add_argument("dump")
    parser.add_argument("demand", nargs="*", help="item per_min pairs")
    parser.add_argument(
        "--spc", type=float, help="science packs per minute; the six pre-space packs at this rate"
    )
    parser.add_argument(
        "--telemetry-taps",
        default="none",
        help="telemetry tap scope: none, io, merges, io,merges, all, or comma-separated nets",
    )
    parser.add_argument(
        "--enable-display-readouts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="instantiate display panel cells for telemetry taps",
    )
    parser.add_argument(
        "--enable-circuit-readouts",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="instantiate arithmetic combinator accumulator cells for telemetry taps",
    )
    parser.add_argument(
        "--enable-compound-cells",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="cluster dedicated 1:1 producer-consumer pairs into compound macro-cells",
    )
    parser.add_argument(
        "--synthesize-splitters",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="synthesize binary trees of splitters for multi-consumer nets",
    )
    parser.add_argument(
        "--controller-verilog",
        default=None,
        help="Path to Verilog controller to synthesize and fuse into factory netlist",
    )
    parser.add_argument(
        "--only-recipes",
        default=None,
        help="Comma-separated list of allowed recipes (treats their inputs as raw)",
    )
    parser.add_argument(
        "--use-chassis",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Include chassis base entity power demand in synthesis",
    )
    parser.add_argument(
        "--chassis-grid-w",
        type=int,
        default=0,
        help="Chassis grid width in city blocks (0 for auto)",
    )
    parser.add_argument(
        "--chassis-grid-h",
        type=int,
        default=0,
        help="Chassis grid height in city blocks (0 for auto)",
    )
    args = parser.parse_args()

    if args.spc is not None:
        demand = dict.fromkeys(SPM_PACKS, args.spc)
    else:
        demand = parse_demand(args.demand)

    only_recipes = (
        [r.strip() for r in args.only_recipes.split(",") if r.strip()]
        if args.only_recipes
        else None
    )

    return build_all(
        args.dump,
        demand,
        Path(),
        tap_scope=args.telemetry_taps,
        enable_display=args.enable_display_readouts,
        enable_circuit=args.enable_circuit_readouts,
        enable_compound_cells=args.enable_compound_cells,
        synthesize_splitters=args.synthesize_splitters,
        controller_verilog=args.controller_verilog,
        only_recipes=only_recipes,
        use_chassis=args.use_chassis,
        chassis_grid_w=args.chassis_grid_w,
        chassis_grid_h=args.chassis_grid_h,
    )


if __name__ == "__main__":
    sys.exit(main())
