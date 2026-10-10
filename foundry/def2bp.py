"""DEF to Factorio blueprint converter.

Reads placed and routed OpenROAD DEF files and netlist JSON metadata,
then generates Factorio 2.0 blueprint exchange strings.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    from foundry import blueprint
    from foundry.belt_drc import BeltDrcChecker
    from foundry.circuit import wire_telemetry
    from foundry.def_parser import extract_pins, merge_collinear_wires, parse_def
    from foundry.def_untangle import (
        fix_reverse_splitter_inputs,
        relocate_blocked_splitter_inputs,
        untangle_machine_inputs,
        untangle_merge_tree_sinks,
        untangle_merger_inputs,
        untangle_splitter_outputs,
    )
    from foundry.entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        check_collisions,
        check_power,
        check_underground_pairing,
        entity_collision_half_size,
        entity_size,
        prune_dead_end_belts,
    )
    from foundry.jumper import route_and_resolve_jumpers
    from foundry.pdn import generate_pdn
    from foundry.placement import place_components
except ImportError:
    import blueprint
    from belt_drc import BeltDrcChecker
    from circuit import wire_telemetry
    from def_parser import extract_pins, merge_collinear_wires, parse_def
    from def_untangle import (
        fix_reverse_splitter_inputs,
        relocate_blocked_splitter_inputs,
        untangle_machine_inputs,
        untangle_merge_tree_sinks,
        untangle_merger_inputs,
        untangle_splitter_outputs,
    )
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
        check_collisions,
        check_power,
        check_underground_pairing,
        entity_collision_half_size,
        entity_size,
        prune_dead_end_belts,
    )
    from jumper import route_and_resolve_jumpers
    from pdn import generate_pdn
    from placement import place_components

__all__ = [
    "DIRECTION_EAST",
    "DIRECTION_NORTH",
    "DIRECTION_SOUTH",
    "DIRECTION_WEST",
    "build_entities",
    "check_collisions",
    "check_power",
    "check_underground_pairing",
    "def_to_blueprint",
    "entity_collision_half_size",
    "entity_size",
    "extract_pins",
    "fix_reverse_splitter_inputs",
    "main",
    "merge_collinear_wires",
    "parse_def",
    "prune_dead_end_belts",
    "untangle_machine_inputs",
    "untangle_merge_tree_sinks",
    "untangle_merger_inputs",
    "untangle_splitter_outputs",
]


def build_entities(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any],
    strict_drc: bool = True,
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """Build Factorio entities and wire connections from DEF and netlist data."""
    # 1. Untangle symmetric splitter outputs, inputs, and commutative machine inputs
    untangle_splitter_outputs(def_data)
    untangle_merger_inputs(def_data)
    fix_reverse_splitter_inputs(def_data)
    relocate_blocked_splitter_inputs(def_data)

    scale = 1.0 / float(def_data.get("units", 1000))

    # 2. Place components, machines, and inserters
    placement = place_components(def_data, netlist_data)

    # 3. Rasterize routing tracks and resolve surface crossings with underground jumpers
    routes = route_and_resolve_jumpers(def_data, netlist_data, placement)

    # 4. Generate power distribution network (PDN)
    pole_entities, mst_connections = generate_pdn(
        placement.entities,
        placement.occupied_positions,
        routes.pin_tiles,
        routes.valid_input_pins,
        routes.valid_output_pins,
    )

    # 5. Sanity checks
    collision_pairs = check_collisions(placement.entities)
    if collision_pairs:
        raise RuntimeError(
            f"Collision check failed: {len(collision_pairs)} overlapping entity pairs detected"
        )

    unpaired_hoods = check_underground_pairing(placement.entities)
    if unpaired_hoods:
        raise RuntimeError(
            f"Underground check failed: {len(unpaired_hoods)} unpaired underground hoods detected: {unpaired_hoods}"
        )

    # 6. Prune dead-end stub belts
    entities = prune_dead_end_belts(placement.entities, def_data, netlist_data)
    grid_entities = {
        (round(e["position"]["x"], 2), round(e["position"]["y"], 2)): e for e in entities
    }

    # 7. Wire circuit telemetry and assign sequential entity numbers
    wires = wire_telemetry(
        entities,
        netlist_data,
        placement.inst_to_entity,
        routes.net_surface_tiles,
        grid_entities,
        mst_connections,
        pole_entities,
    )

    # 8. Physical Design Rule Check (DRC)
    remaining_conflicts = [
        pos
        for pos, nets in routes.tile_to_nets.items()
        if len(nets) > 1 or pos in routes.adjacent_fluid_conflicts
    ]
    drc_in_pins = [
        (pin["x"] * scale, pin["y"] * scale)
        for pin in def_data.get("pins", [])
        if pin.get("direction") == "INPUT"
    ]
    drc_out_pins = [
        (pin["x"] * scale, pin["y"] * scale)
        for pin in def_data.get("pins", [])
        if pin.get("direction") == "OUTPUT"
    ]
    drc = BeltDrcChecker()
    violations = drc.check_entities(
        entities,
        input_pins=drc_in_pins,
        output_pins=drc_out_pins,
        unresolved_crossings=remaining_conflicts,
    )
    if violations:
        for v in violations:
            sys.stderr.write(f"DRC {v.severity}: [{v.code}] at ({v.x}, {v.y}): {v.message}\n")
        if strict_drc:
            fatal = [v for v in violations if v.severity == "ERROR"]
            if fatal:
                raise ValueError(f"Physical DRC failed with {len(fatal)} violation(s)")

    return entities, wires


def def_to_blueprint(
    def_text: str,
    netlist_data: dict[str, Any],
    label: str = "factory",
    strict_drc: bool = True,
) -> str:
    """Convert DEF text and netlist metadata into a blueprint exchange string."""
    def_data = parse_def(def_text)
    entities, wires = build_entities(def_data, netlist_data, strict_drc=strict_drc)
    return blueprint.encode(entities, label, wires=wires)


def main() -> int:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Convert placed and routed DEF to Factorio blueprint string."
    )
    parser.add_argument("def_file", type=Path, help="Path to input DEF file")
    parser.add_argument("netlist_file", type=Path, help="Path to input netlist JSON file")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Path to write blueprint output",
    )
    parser.add_argument(
        "--pins-out",
        type=Path,
        default=None,
        help="Path to write extracted pins JSON",
    )
    parser.add_argument(
        "--label",
        type=str,
        default="factory",
        help="Blueprint label text",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw blueprint JSON document instead of exchange string",
    )
    parser.add_argument(
        "--time-out",
        type=Path,
        default=None,
        help="Path to write execution time JSON",
    )
    parser.add_argument(
        "--no-strict-drc",
        action="store_true",
        help="Do not abort on physical DRC violations",
    )

    args = parser.parse_args()

    t0 = time.perf_counter()
    def_text = args.def_file.read_text()
    netlist_data = json.loads(args.netlist_file.read_text())

    def_data = parse_def(def_text)
    entities, wires = build_entities(def_data, netlist_data, strict_drc=not args.no_strict_drc)

    if args.json:
        document: dict[str, Any] = {
            "blueprint": {
                "item": "blueprint",
                "label": args.label,
                "version": blueprint.VERSION_2_0,
                "entities": entities,
            }
        }
        if wires:
            document["blueprint"]["wires"] = wires
        output_text = json.dumps(document, indent=2)
    else:
        output_text = blueprint.encode(entities, args.label, wires=wires)

    if args.out:
        args.out.write_text(output_text + "\n")
    else:
        sys.stdout.write(output_text + "\n")

    if args.pins_out:
        pins_data = extract_pins(def_data, netlist_data)
        args.pins_out.parent.mkdir(parents=True, exist_ok=True)
        args.pins_out.write_text(json.dumps(pins_data, indent=2) + "\n")

    t1 = time.perf_counter()
    elapsed = round(t1 - t0, 3)

    if args.time_out:
        args.time_out.parent.mkdir(parents=True, exist_ok=True)
        time_data = {
            "stage": "bp",
            "elapsed_seconds": elapsed,
        }
        args.time_out.write_text(json.dumps(time_data, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
