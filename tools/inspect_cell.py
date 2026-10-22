"""PDK cell and port inspection tool.

Inspects generated macro cells, physical port slot mappings, layers,
and machine orientations from Factorio prototype dumps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from foundry import pdk, solve

DIR_NORTH = 0
DIR_EAST = 4
DIR_SOUTH = 8
DIR_WEST = 12
WATTS_PER_KILOWATT = 1000.0
DIRECTION_NAMES = {
    DIR_NORTH: "North (0)",
    DIR_EAST: "East (4)",
    DIR_SOUTH: "South (8)",
    DIR_WEST: "West (12)",
}


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

    inputs_detail = [
        {
            "port": port,
            "slot": in_slots.get(port),
            "face": port_faces.get(port, "W"),
            "type": "fluid" if port in fluids else "item",
        }
        for port in cell.get("inputs", [])
    ]

    outputs_detail = [
        {
            "port": port,
            "slot": out_slots.get(port),
            "face": port_faces.get(port, "E"),
            "type": "fluid" if port in fluids else "item",
        }
        for port in cell.get("outputs", [])
    ]

    return {
        "cell": cell.get("cell"),
        "machine": machine_name,
        "recipe": recipe_name,
        "width": cell.get("width"),
        "height": cell.get("height"),
        "machine_width": cell.get("machine_width"),
        "machine_height": cell.get("machine_height"),
        "direction": cell.get("direction", DIR_NORTH),
        "has_fluids": cell.get("has_fluids", False),
        "watts": cell.get("watts", 0.0),
        "crafts_per_minute": cell.get("crafts_per_minute", 0.0),
        "inputs": inputs_detail,
        "outputs": outputs_detail,
        "raw_fluid_boxes": p.machines.get(machine_name, {}).get("fluid_boxes", []),
        "recipe_raw": data.recipes.get(recipe_name, {}),
    }


def format_cell_spec(spec: Mapping[str, object]) -> str:
    """Format cell specification into readable technical text."""
    direction = spec["direction"]
    dir_code = direction if isinstance(direction, int) else 0
    watts_raw = spec["watts"]
    watts = float(watts_raw) if isinstance(watts_raw, (int, float)) else 0.0
    cpm_raw = spec["crafts_per_minute"]
    crafts_per_min = float(cpm_raw) if isinstance(cpm_raw, (int, float)) else 0.0
    inputs = spec["inputs"] if isinstance(spec["inputs"], list) else []
    outputs = spec["outputs"] if isinstance(spec["outputs"], list) else []
    lines = [
        f"Cell: {spec['cell']}",
        f"  Machine: {spec['machine']}",
        f"  Recipe:  {spec['recipe']}",
        f"  Macro Dimensions: {spec['width']}x{spec['height']} tiles",
        f"  Machine Footprint: {spec['machine_width']}x{spec['machine_height']} tiles",
        f"  Direction: {DIRECTION_NAMES.get(dir_code, str(dir_code))}",
        f"  Has Fluids: {spec['has_fluids']}",
        f"  Power: {watts / WATTS_PER_KILOWATT:.1f} kW",
        f"  Crafts/min: {crafts_per_min:.2f}",
        "  Inputs:",
    ]
    lines.extend(
        f"    - {inp['port']!s:<20} slot={inp['slot']} face={inp['face']} type={inp['type']}"
        for inp in inputs
        if isinstance(inp, Mapping)
    )
    lines.append("  Outputs:")
    lines.extend(
        f"    - {out['port']!s:<20} slot={out['slot']} face={out['face']} type={out['type']}"
        for out in outputs
        if isinstance(out, Mapping)
    )
    raw_boxes = spec.get("raw_fluid_boxes")
    if isinstance(raw_boxes, list):
        lines.append("  Fluid Boxes:")
        for fb in raw_boxes:
            if isinstance(fb, Mapping):
                ptype = fb.get("production_type")
                conns = fb.get("pipe_connections", [])
                lines.append(f"    - {ptype}: {conns}")
    if "recipe_raw" in spec:
        lines.append(f"  Recipe Ingredients: {spec['recipe_raw']}")
    return "\n".join(lines)


def _resolve_dump_path(dump_path: Path, env: Mapping[str, str] = os.environ) -> Path:
    """Resolve dump file path against workspace or runfiles directories."""
    if dump_path.is_file():
        return dump_path
    workspace = env.get("BUILD_WORKSPACE_DIRECTORY", "")
    if workspace:
        candidate = Path(workspace) / dump_path
        if candidate.is_file():
            return candidate
    runfiles = env.get("PYTHON_RUNFILES") or env.get("TEST_SRCDIR", "")
    if runfiles:
        candidate = Path(runfiles) / "_main" / dump_path
        if candidate.is_file():
            return candidate
    return dump_path


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
        "--machine", type=str, required=True, help="Machine name (for example chemical-plant)"
    )
    parser.add_argument(
        "--recipe", type=str, required=True, help="Recipe name (for example plastic-bar)"
    )
    parser.add_argument("--json", action="store_true", help="Output specification in JSON format")

    args = parser.parse_args(argv)
    dump_path = _resolve_dump_path(args.dump)
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
