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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lef
import liberty
import netlist
import pdk
import solve

# The science packs of the 60 SPM reference demand.
SPM_PACKS = (
    "automation-science-pack",
    "logistic-science-pack",
    "military-science-pack",
    "chemical-science-pack",
    "production-science-pack",
    "utility-science-pack",
)


def parse_demand(argv: list[str]) -> dict[str, float]:
    """argv after the dump path -> demand as item per_min pairs."""
    if len(argv) % 2 != 0:
        raise SystemExit("demand is item per_min pairs")
    pairs = {}
    for index in range(0, len(argv), 2):
        pairs[argv[index]] = float(argv[index + 1])
    return pairs


def build_all(
    dump_path: Path | str,
    demand: dict[str, float],
    out_dir: Path | str = ".",
    tap_scope: str = "none",
    enable_display: bool = True,
    enable_circuit: bool = False,
) -> int:
    """Run full netbuild pipeline, emitting Verilog, LEF, Liberty, and JSON."""
    out = Path(out_dir)
    data = solve.Data(str(dump_path))
    tech = pdk.Pdk(data)
    allowed = data.allowed_recipes(["nauvis"])
    nl = netlist.build(data, demand, allowed)
    plans = netlist.merge_plans_of(nl)
    split_plans = netlist.split_plans_of(nl)
    telemetry_records = netlist.insert_telemetry(
        nl, tap_scope, enable_display, enable_circuit, plans
    )
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
    problems = lef.check_lef(cells, lef_text)
    problems += liberty.check_liberty(cells, lib_text)
    if problems:
        for problem in problems:
            print(f"pdk: {problem}", file=sys.stderr)
        return 1

    sidecar = {
        "demand": demand,
        "fluids": sorted(data.prototypes.get("fluid", {}).keys()),
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
        "instances": [
            {
                "name": i.name,
                "cell": i.cell_key,
                "recipe": i.recipe,
                "plan_rate_per_min": i.plan_rate,
                "rated_per_min": i.rated,
            }
            for i in nl.instances
        ],
    }
    (out / "netlist.json").write_text(json.dumps(sidecar, indent=2))
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
    args = parser.parse_args()

    if args.spc is not None:
        demand = dict.fromkeys(SPM_PACKS, args.spc)
    else:
        demand = parse_demand(args.demand)

    return build_all(
        args.dump,
        demand,
        Path(),
        tap_scope=args.telemetry_taps,
        enable_display=args.enable_display_readouts,
        enable_circuit=args.enable_circuit_readouts,
    )


if __name__ == "__main__":
    sys.exit(main())
