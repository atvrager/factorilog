"""Synthesize, route, and render visual PNG images for all PDK machine cells.

This script executes OpenROAD placement and routing for each machine cell,
converts the routed design to a blueprint, and renders a PNG image.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

from foundry import def2bp, lef, liberty, netlist, pdk, render_bp, solve

REPO_ROOT = Path(__file__).resolve().parent.parent

OPENROAD_IMAGE = "docker.io/openroad/orfs:latest"
REAL_USER_HOME = pwd.getpwuid(os.getuid()).pw_dir
TEMP_ROOT_DIR = "/tmp"
FLOW_TCL_SCRIPT = "/repo/flow/factorilog.tcl"

CELL_CONFIGS: list[dict[str, str | float]] = [
    {
        "machine": "assembling-machine-1",
        "recipe": "iron-gear-wheel",
        "output": "iron-gear-wheel",
        "rate": 60.0,
    },
    {
        "machine": "assembling-machine-2",
        "recipe": "electronic-circuit",
        "output": "electronic-circuit",
        "rate": 60.0,
    },
    {
        "machine": "assembling-machine-3",
        "recipe": "automation-science-pack",
        "output": "automation-science-pack",
        "rate": 15.0,
    },
    {
        "machine": "stone-furnace",
        "recipe": "copper-plate",
        "output": "copper-plate",
        "rate": 15.0,
    },
    {
        "machine": "steel-furnace",
        "recipe": "iron-plate",
        "output": "iron-plate",
        "rate": 30.0,
    },
    {
        "machine": "electric-furnace",
        "recipe": "iron-plate",
        "output": "iron-plate",
        "rate": 30.0,
    },
    {
        "machine": "chemical-plant",
        "recipe": "plastic-bar",
        "output": "plastic-bar",
        "rate": 60.0,
    },
    {
        "machine": "oil-refinery",
        "recipe": "basic-oil-processing",
        "output": "petroleum-gas",
        "rate": 60.0,
    },
    {
        "machine": "centrifuge",
        "recipe": "uranium-processing",
        "output": "uranium-238",
        "rate": 5.0,
    },
    {
        "machine": "rocket-silo",
        "recipe": "rocket-part",
        "output": "rocket-part",
        "rate": 20.0,
    },
]


def render_cell(
    cfg: dict[str, str | float],
    data: solve.Data,
    pdk_inst: pdk.Pdk,
    out_dir: Path,
) -> Path:
    """Route a machine cell through OpenROAD and render it to a PNG file."""
    machine_name = str(cfg["machine"])
    recipe_name = str(cfg["recipe"])
    expected_output = str(cfg["output"])
    demand_rate = float(cfg["rate"])

    with tempfile.TemporaryDirectory(dir=TEMP_ROOT_DIR, prefix=f"render-{machine_name}-") as tmp:
        work_dir = Path(tmp)

        allowed = {recipe_name}
        nl = netlist.build(
            data,
            {expected_output: demand_rate},
            allowed,
            machine_override=machine_name,
        )
        plans = netlist.merge_plans_of(nl)
        split_plans = netlist.split_plans_of(nl)

        (work_dir / "netlist.v").write_text(netlist.emit_verilog(nl, plans, split_plans))
        (work_dir / "cells.v").write_text(netlist.emit_cells_v(nl))

        cells = list(nl.cells.values())
        lef.write_lef(pdk_inst, cells, str(work_dir / "tech.lef"))
        liberty.write_liberty(cells, str(work_dir / "tech.lib"))

        fluids = [m for m in (nl.inputs + nl.outputs) if m in data.prototypes.get("fluid", {})]
        sidecar = {
            "demand": {expected_output: demand_rate},
            "fluids": fluids,
            "inputs": nl.inputs,
            "outputs": nl.outputs,
            "cells": dict(nl.cells),
            "instances": [
                {"name": i.name, "cell": i.cell_key, "recipe": i.recipe} for i in nl.instances
            ],
        }
        (work_dir / "netlist.json").write_text(json.dumps(sidecar, indent=2))

        cmd_orfs = [
            "podman",
            "run",
            "--rm",
            "-v",
            f"{REPO_ROOT}:/repo",
            "-v",
            f"{work_dir}:/work",
            "-w",
            "/work",
            OPENROAD_IMAGE,
            "bash",
            "-lc",
            f"source /OpenROAD-flow-scripts/env.sh >/dev/null && openroad -no_init -exit {FLOW_TCL_SCRIPT}",
        ]
        podman_env = {**os.environ, "TMPDIR": TEMP_ROOT_DIR, "HOME": REAL_USER_HOME}
        res = subprocess.run(cmd_orfs, env=podman_env, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"OpenROAD routing failed for {machine_name}:\n{res.stderr}")

        def_text = (work_dir / "factory.def").read_text()
        bp_string = def2bp.def_to_blueprint(def_text, sidecar, label=f"cell_{machine_name}")

        out_file = out_dir / f"{machine_name}.png"
        render_bp.render_blueprint(bp_string, out_file)
        (out_dir / f"{machine_name}.bp.txt").write_text(bp_string)
        return out_file


def main() -> None:
    """Generate PNG renders for all core PDK machine cells."""
    fixture_path = REPO_ROOT / "test" / "data" / "dump-fixture.json"
    data = solve.Data(str(fixture_path))
    pdk_inst = pdk.Pdk(data)

    out_dir = REPO_ROOT / "docs" / "cells"
    out_dir.mkdir(parents=True, exist_ok=True)

    for cfg in CELL_CONFIGS:
        machine = cfg["machine"]
        print(f"Generating cell render for {machine}...")
        img_path = render_cell(cfg, data, pdk_inst, out_dir)
        print(f"  -> {img_path}")

    print("Completed rendering all machine cells.")


if __name__ == "__main__":
    main()
