"""PDK cell and port inspection tool.

Inspects generated macro cells, physical port slot mappings, layers,
and machine orientations from Factorio prototype dumps.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from foundry import pdk, solve


def inspect_cell_spec(
    data: solve.Data,
    machine_name: str,
    recipe_name: str,
) -> dict[str, Any] | None:
    """Extract cell specifications and port assignments from PDK."""
    p = pdk.Pdk(data)
    cell = p.cell(machine_name, recipe_name)
    if cell is None:
        return None

    in_slots = cell.get("in_slots", {})
    out_slots = cell.get("out_slots", {})
    port_faces = cell.get("port_faces", {})
    fluids = set(data.prototypes.get("fluid", {}).keys())

    inputs_detail = []
    for port in cell.get("inputs", []):
        slot = in_slots.get(port)
        face = port_faces.get(port, "W")
        is_fluid = port in fluids
        inputs_detail.append(
            {
                "port": port,
                "slot": slot,
                "face": face,
                "type": "fluid" if is_fluid else "item",
            }
        )

    outputs_detail = []
    for port in cell.get("outputs", []):
        slot = out_slots.get(port)
        face = port_faces.get(port, "E")
        is_fluid = port in fluids
        outputs_detail.append(
            {
                "port": port,
                "slot": slot,
                "face": face,
                "type": "fluid" if is_fluid else "item",
            }
        )

    return {
        "cell": cell.get("cell"),
        "machine": machine_name,
        "recipe": recipe_name,
        "width": cell.get("width"),
        "height": cell.get("height"),
        "machine_width": cell.get("machine_width"),
        "machine_height": cell.get("machine_height"),
        "direction": cell.get("direction", 0),
        "has_fluids": cell.get("has_fluids", False),
        "watts": cell.get("watts", 0.0),
        "crafts_per_minute": cell.get("crafts_per_minute", 0.0),
        "inputs": inputs_detail,
        "outputs": outputs_detail,
        "raw_fluid_boxes": p.machines.get(machine_name, {}).get("fluid_boxes", []),
        "recipe_raw": data.recipes.get(recipe_name, {}),
    }


def format_cell_spec(spec: dict[str, Any]) -> str:
    """Format cell specification into readable technical text."""
    dir_names = {0: "North (0)", 4: "East (4)", 8: "South (8)", 12: "West (12)"}
    lines = [
        f"Cell: {spec['cell']}",
        f"  Machine: {spec['machine']}",
        f"  Recipe:  {spec['recipe']}",
        f"  Macro Dimensions: {spec['width']}x{spec['height']} tiles",
        f"  Machine Footprint: {spec['machine_width']}x{spec['machine_height']} tiles",
        f"  Direction: {dir_names.get(spec['direction'], str(spec['direction']))}",
        f"  Has Fluids: {spec['has_fluids']}",
        f"  Power: {spec['watts'] / 1000.0:.1f} kW",
        f"  Crafts/min: {spec['crafts_per_minute']:.2f}",
        "  Inputs:",
    ]
    lines.extend(
        f"    - {inp['port']:<20} slot={inp['slot']} face={inp['face']} type={inp['type']}"
        for inp in spec["inputs"]
    )
    lines.append("  Outputs:")
    lines.extend(
        f"    - {out['port']:<20} slot={out['slot']} face={out['face']} type={out['type']}"
        for out in spec["outputs"]
    )
    if "raw_fluid_boxes" in spec:
        lines.append("  Fluid Boxes:")
        for fb in spec["raw_fluid_boxes"]:
            ptype = fb.get("production_type")
            conns = fb.get("pipe_connections", [])
            lines.append(f"    - {ptype}: {conns}")
    if "recipe_raw" in spec:
        lines.append(f"  Recipe Ingredients: {spec['recipe_raw']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for PDK cell inspector."""
    parser = argparse.ArgumentParser(
        description="Inspect Factorilog PDK macro cell specifications."
    )
    parser.add_argument(
        "--dump",
        type=Path,
        default=Path("test/data/dump-fixture.json"),
        help="Path to prototype dump JSON fixture",
    )
    parser.add_argument(
        "--machine", type=str, required=True, help="Machine name (e.g. chemical-plant)"
    )
    parser.add_argument("--recipe", type=str, required=True, help="Recipe name (e.g. plastic-bar)")
    parser.add_argument("--json", action="store_true", help="Output specification in JSON format")

    args = parser.parse_args(argv)
    dump_path = args.dump
    if not dump_path.is_file():
        import os

        workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
        if workspace:
            candidate = Path(workspace) / dump_path
            if candidate.is_file():
                dump_path = candidate
        runfiles = os.environ.get("PYTHON_RUNFILES") or os.environ.get("TEST_SRCDIR")
        if runfiles and not dump_path.is_file():
            candidate = Path(runfiles) / "_main" / dump_path
            if candidate.is_file():
                dump_path = candidate

    if not dump_path.is_file():
        sys.stderr.write(f"Error: dump file not found: {args.dump}\n")
        return 1

    data = solve.Data(str(dump_path))
    spec = inspect_cell_spec(data, args.machine, args.recipe)
    if spec is None:
        sys.stderr.write(
            f"Error: cannot build cell for machine '{args.machine}' with recipe '{args.recipe}'\n"
        )
        return 1

    if args.json:
        print(json.dumps(spec, indent=2))
    else:
        print(format_cell_spec(spec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
