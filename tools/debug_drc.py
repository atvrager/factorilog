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
    eliminate_staircase_jogs,
    eliminate_u_detours,
    fix_reverse_splitter_inputs,
    relocate_blocked_splitter_inputs,
    separate_overlapping_channel_routes,
    shift_congested_pin_turns,
    simplify_orthogonal_routes,
    straighten_collinear_routes,
    untangle_interlocking_corners,
    untangle_machine_inputs,
    untangle_merger_inputs,
    untangle_shared_merger_tracks,
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
        "--trace-reachability",
        type=str,
        default="",
        help="Trace upstream belt supply backwards from 'X,Y' (e.g. '269,419')",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed evaluation logs for every attempted underground span",
    )
    return parser.parse_args(argv)


def trace_upstream(
    start_tile: tuple[int, int],
    entities: list[dict[str, Any]],
    max_steps: int = 500,
) -> None:
    """Trace backwards from a tile along upstream belts and splitters."""
    import math

    from foundry.entity_helpers import (
        DIR_STEPS,
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )

    tile_map: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for ent in entities:
        pos = ent.get("position", {})
        cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
        cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
        name = ent.get("name", "")
        if "splitter" in name:
            d = ent.get("direction", DIRECTION_NORTH)
            if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                tile_map.setdefault((math.floor(cx - 0.5), math.floor(cy)), []).append(ent)
                tile_map.setdefault((math.floor(cx + 0.5), math.floor(cy)), []).append(ent)
            else:
                tile_map.setdefault((math.floor(cx), math.floor(cy - 0.5)), []).append(ent)
                tile_map.setdefault((math.floor(cx), math.floor(cy + 0.5)), []).append(ent)
        else:
            tile_map.setdefault((math.floor(cx), math.floor(cy)), []).append(ent)

    print(f"\n[UPSTREAM TRACE FROM {start_tile}]")
    curr = start_tile
    visited_trace: set[tuple[int, int]] = set()
    for step in range(max_steps):
        visited_trace.add(curr)
        ents = tile_map.get(curr, [])
        ent_strs = [
            f"{e.get('name')} (dir={e.get('direction')}, type={e.get('type')}, pos={e.get('position')})"
            for e in ents
        ]
        print(f"  Step {step}: at {curr} -> {ent_strs}")
        if not ents:
            print("    STOP: No entity at tile.")
            break

        incoming: list[tuple[tuple[int, int], dict[str, Any]]] = []
        for (dx, dy), d_towards in [
            ((1, 0), DIRECTION_WEST),
            ((-1, 0), DIRECTION_EAST),
            ((0, 1), DIRECTION_NORTH),
            ((0, -1), DIRECTION_SOUTH),
        ]:
            prev_tile = (curr[0] + dx, curr[1] + dy)
            for pe in tile_map.get(prev_tile, []):
                pname = pe.get("name", "")
                pdir = pe.get("direction", 0)
                if (
                    "transport-belt" in pname
                    or ("underground-belt" in pname and pe.get("type") == "output")
                    or "splitter" in pname
                ) and pdir == d_towards:
                    incoming.append((prev_tile, pe))

        for e in ents:
            if "underground-belt" in e.get("name", "") and e.get("type") == "output":
                edir = e.get("direction", 0)
                edx, edy = DIR_STEPS.get(edir, (0, 0))
                for span in range(1, 15):
                    in_pos = (curr[0] - edx * span, curr[1] - edy * span)
                    incoming.extend(
                        (in_pos, cand)
                        for cand in tile_map.get(in_pos, [])
                        if cand.get("name") == e.get("name")
                        and cand.get("direction") == edir
                        and cand.get("type") == "input"
                    )

        if not incoming:
            print("    STOP: No incoming belt/splitter/underground feeds into this tile.")
            break
        fed_strs = [f"{pe.get('name')} at {pt}" for pt, pe in incoming]
        print(f"    Fed by: {fed_strs}")
        next_pt = incoming[0][0]
        if next_pt in visited_trace:
            print(f"    STOP: Cycle detected at {next_pt}.")
            break
        curr = next_pt


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
    trace_reachability: tuple[int, int] | None = None,
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
    def_data["_netlist_data"] = netlist_data

    # 1. Pre-routing untangling
    untangle_shared_merger_tracks(def_data)
    untangle_merger_inputs(def_data)
    # shortcut_backward_splitter_detours(def_data)
    simplify_orthogonal_routes(def_data)
    straighten_collinear_routes(def_data)
    eliminate_staircase_jogs(def_data)
    eliminate_u_detours(def_data)
    untangle_splitter_outputs(def_data)
    fix_reverse_splitter_inputs(def_data)
    relocate_blocked_splitter_inputs(def_data)
    untangle_interlocking_corners(def_data)
    shift_congested_pin_turns(def_data)
    separate_overlapping_channel_routes(def_data)
    untangle_machine_inputs(def_data, netlist_data)

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
    if committed and verbose and not filter_tile:
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
        print(f"\n[CONFLICT SUMMARY: {len(displayed_conflicts)} conflicts]")
        for pos in sorted(displayed_conflicts):
            cx, cy = pos
            nets = sorted(routes.tile_to_nets.get(pos, set()))
            print(f"  - ({cx:.1f}, {cy:.1f}): {nets}")

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
                net_match = next(
                    (n for n in def_data.get("nets", []) if n.get("name") == cand), None
                )
                if net_match:
                    print(f"    Wires: {net_match.get('wires')}")
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
        nearby = format_nearby_components(fx, fy, def_data.get("components", []))
        if nearby:
            print("  Nearby components:")
            for n in nearby:
                print(f"  {n}")
        if not found_near:
            print("  (no entities found within radius 3.0)")

    # DRC violations breakdown
    if violations:
        print("\n[DRC VIOLATIONS]")
        for v in violations:
            print(f"  - [{v.code}] at ({v.x:.1f}, {v.y:.1f}): {v.message}")

    if trace_reachability:
        trace_upstream(trace_reachability, entities)

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
    trace_tile = None
    if args.trace_reachability:
        parts = args.trace_reachability.split(",")
        if len(parts) == 2:
            trace_tile = (int(parts[0]), int(parts[1]))
    return run_diagnostics(
        args.def_file,
        args.netlist_json,
        filter_tile=filter_tile,
        verbose=args.verbose,
        trace_reachability=trace_tile,
    )


if __name__ == "__main__":
    sys.exit(main())
