"""Diagnose physical DRC violations and routing conflicts on Factorio layouts.

Loads placed DEF and netlist metadata.
Traces jumper synthesis and conflict resolution.
Reports root cause diagnostics for DRC violations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from foundry.belt_drc import BeltDrcChecker
from foundry.def_parser import parse_def
from foundry.def_untangle import (
    fix_reverse_splitter_inputs,
    relocate_blocked_splitter_inputs,
    separate_overlapping_channel_routes,
    straighten_collinear_routes,
    untangle_interlocking_corners,
    untangle_merger_inputs,
    untangle_splitter_outputs,
)
from foundry.jumper import route_and_resolve_jumpers
from foundry.placement import place_components


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Diagnose physical DRC violations and routing conflicts."
    )
    parser.add_argument("def_file", type=Path, help="Path to placed DEF file")
    parser.add_argument("netlist_json", type=Path, help="Path to netlist JSON metadata")
    parser.add_argument(
        "--tile",
        type=str,
        default="",
        help="Optional specific tile coordinate to inspect, formatted as 'X,Y' (e.g. '53.5,54.5')",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed evaluation logs for every attempted underground span",
    )
    return parser.parse_args(argv)


def format_nearby_components(
    cx: float, cy: float, components: list[dict[str, Any]], radius: float = 3.0
) -> list[str]:
    """Find and format components located near a target coordinate."""
    nearby: list[str] = []
    for comp in components:
        comp_x = comp["x"] / 1000.0
        comp_y = comp["y"] / 1000.0
        if abs(comp_x - cx) <= radius and abs(comp_y - cy) <= radius:
            nearby.append(
                f"  - {comp.get('name')} ({comp.get('macro')}) at ({comp_x:.1f}, {comp_y:.1f})"
            )
    return nearby


def summarize_failures(attempts: list[dict[str, Any]]) -> list[str]:
    """Summarize unique reasons why candidate jumper spans were rejected."""
    reasons: dict[str, int] = {}
    for att in attempts:
        for r in att.get("fail_in", []):
            key = f"Entrance: {r}"
            reasons[key] = reasons.get(key, 0) + 1
        for r in att.get("fail_out", []):
            key = f"Exit: {r}"
            reasons[key] = reasons.get(key, 0) + 1
        for r in att.get("invalid_reasons", []):
            key = f"Span: {r}"
            reasons[key] = reasons.get(key, 0) + 1
    return [f"{reason} ({count} span attempts)" for reason, count in sorted(reasons.items())]


def run_diagnostics(
    def_path: Path,
    netlist_path: Path,
    filter_tile: tuple[float, float] | None = None,
    verbose: bool = False,
) -> int:
    """Run full physical DRC and routing conflict analysis."""
    import os

    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        if not def_path.is_absolute() and not def_path.is_file():
            candidate = Path(workspace) / def_path
            if candidate.is_file():
                def_path = candidate
        if not netlist_path.is_absolute() and not netlist_path.is_file():
            candidate = Path(workspace) / netlist_path
            if candidate.is_file():
                netlist_path = candidate

    if not def_path.is_file():
        print(f"ERROR: DEF file not found: {def_path}", file=sys.stderr)
        return 2
    if not netlist_path.is_file():
        print(f"ERROR: Netlist JSON not found: {netlist_path}", file=sys.stderr)
        return 2

    def_data = parse_def(def_path.read_text(encoding="utf-8"))
    netlist_data = json.loads(netlist_path.read_text(encoding="utf-8"))

    # 1. Pre-routing untangling
    straighten_collinear_routes(def_data)
    untangle_splitter_outputs(def_data)
    untangle_merger_inputs(def_data)
    fix_reverse_splitter_inputs(def_data)
    relocate_blocked_splitter_inputs(def_data)
    untangle_interlocking_corners(def_data)
    separate_overlapping_channel_routes(def_data)
    # untangle_machine_inputs(def_data, netlist_data)

    # 2. Placement
    placement = place_components(def_data, netlist_data)

    # 3. Jumper routing with trace recording
    debug_trace: dict[tuple[float, float], list[dict[str, Any]]] = {}
    routes = route_and_resolve_jumpers(def_data, netlist_data, placement, debug_trace=debug_trace)

    # 4. Prune dead-end belts
    from foundry.def2bp import prune_dead_end_belts

    entities = prune_dead_end_belts(placement.entities, def_data, netlist_data)

    # 5. DRC verification
    scale = 1.0 / float(def_data.get("units", 1000))
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
    unresolved_conflicts = [
        pos
        for pos, nets in routes.tile_to_nets.items()
        if len(nets) > 1 or pos in routes.adjacent_fluid_conflicts
    ]

    checker = BeltDrcChecker(allow_peeling=False)
    violations = checker.check_entities(
        entities,
        input_pins=drc_in_pins,
        output_pins=drc_out_pins,
        unresolved_crossings=unresolved_conflicts,
        tile_to_nets=routes.tile_to_nets,
        fluids=placement.fluids,
    )

    print("================================================================================")
    print(f"PHYSICAL DRC AND ROUTING DIAGNOSTIC REPORT: {def_path.name}")
    print("================================================================================")
    print(f"Unresolved surface crossings : {len(unresolved_conflicts)}")
    print(f"Physical DRC violations      : {len(violations)}")
    print("--------------------------------------------------------------------------------")

    # Committed jumpers
    committed = [
        att
        for atts in debug_trace.values()
        for att in atts
        if att.get("action") == "jump_committed"
    ]
    if committed:
        print(f"\n[COMMITTED JUMPERS: {len(committed)}]")
        for j in committed:
            print(f"  - net {j['candidate']} dir={j['dir']} from {j['in_pos']} to {j['out_pos']}")

    # Conflict breakdown
    displayed_conflicts = (
        [filter_tile]
        if filter_tile and filter_tile in unresolved_conflicts
        else unresolved_conflicts
    )

    if displayed_conflicts:
        print("\n[CONFLICT DETAIL]")
        for pos in sorted(displayed_conflicts):
            cx, cy = pos
            nets = sorted(routes.tile_to_nets.get(pos, set()))
            print(f"\nConflict at ({cx:.1f}, {cy:.1f}) across {len(nets)} nets: {nets}")

            # Nearby components
            nearby = format_nearby_components(cx, cy, def_data.get("components", []))
            if nearby:
                print(" Nearby components:")
                for n in nearby:
                    print(n)

            # Trace evaluation for each candidate net
            attempts = debug_trace.get(pos, [])
            for cand in nets:
                d = routes.net_surface_tiles.get(cand, {}).get(pos)
                cand_attempts = [
                    a for a in attempts if a.get("candidate") == cand and a.get("action") is None
                ]
                print(f"  Candidate net: {cand} (travel direction: {d})")
                reasons = summarize_failures(cand_attempts)
                if reasons:
                    print("    Rejection summary:")
                    for r in reasons:
                        print(f"      - {r}")
                if verbose:
                    print("    Detailed span evaluations:")
                    for att in cand_attempts:
                        b = att.get("bspan")
                        f = att.get("fspan")
                        fin = att.get("fail_in")
                        fout = att.get("fail_out")
                        inv = att.get("invalid_reasons")
                        v = att.get("valid_span")
                        print(
                            f"      b={b} f={f} in={att.get('in_pos')} out={att.get('out_pos')} valid={v} fail_in={fin} fail_out={fout} inv={inv}"
                        )

    if filter_tile:
        fx, fy = filter_tile
        print(f"\n[ENTITIES NEAR ({fx:.1f}, {fy:.1f})]")
        found_near = False
        for ent in entities:
            pos = ent.get("position", {})
            ex = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
            ey = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
            if abs(ex - fx) <= 3.0 and abs(ey - fy) <= 3.0:
                found_near = True
                u_type = f" type={ent.get('type')}" if "type" in ent else ""
                print(
                    f"  - {ent.get('name')} dir={ent.get('direction')}{u_type} at ({ex:.1f}, {ey:.1f})"
                )
        if not found_near:
            print("  (no entities found within radius 3.0)")

    # DRC violations breakdown
    if violations:
        print("\n[DRC VIOLATIONS]")
        for v in violations:
            print(f"  - [{v.code}] at ({v.x:.1f}, {v.y:.1f}): {v.message}")

    print("\n--------------------------------------------------------------------------------")
    if violations:
        print(f"FAILED: {len(violations)} physical DRC violation(s) found.")
        return 1

    print("PASSED: 0 physical DRC violations found.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    filter_tile = None
    if args.tile:
        parts = args.tile.split(",")
        if len(parts) == 2:
            filter_tile = (float(parts[0]), float(parts[1]))
    return run_diagnostics(
        args.def_file,
        args.netlist_json,
        filter_tile=filter_tile,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
