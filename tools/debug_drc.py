"""Diagnose physical DRC violations and routing conflicts on Factorio layouts.

Loads placed DEF and netlist metadata.
Traces jumper synthesis and conflict resolution.
Reports root cause diagnostics for DRC violations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
import tempfile
from pathlib import Path
from typing import Any

from foundry.belt_drc import BeltDrcChecker
from foundry.def2bp import prune_dead_end_belts, route_with_maze_router
from foundry.def_parser import parse_def
from foundry.def_untangle import untangle_def
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
        "--trace-downstream",
        type=str,
        default="",
        help="Trace downstream belt path forward from 'X,Y' (e.g. '443,775')",
    )
    parser.add_argument(
        "--trace-conduit",
        type=str,
        default="",
        help="Trace fluid conduit connectivity from 'X,Y' (e.g. '35,2365')",
    )
    parser.add_argument(
        "--no-untangle",
        action="store_true",
        help="Skip iterative untangling passes (keep only unrouted signal routing)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed evaluation logs for every attempted underground span",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable disk caching of resolved entities and DRC results",
    )
    return parser.parse_args(argv)


def trace_upstream(
    start_tile: tuple[int, int],
    entities: list[dict[str, Any]],
    max_steps: int = 5000,
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


def trace_downstream(
    start_tile: tuple[int, int],
    entities: list[dict[str, Any]],
    max_steps: int = 500,
) -> None:
    """Trace forward from a tile along downstream belts and splitters."""
    import math

    from foundry.entity_helpers import (
        DIR_STEPS,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
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

    print(f"\n[DOWNSTREAM TRACE FROM {start_tile}]")
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

        outgoing: list[tuple[tuple[int, int], dict[str, Any]]] = []
        for e in ents:
            ename = e.get("name", "")
            edir = e.get("direction", DIRECTION_NORTH)
            edx, edy = DIR_STEPS.get(edir, (0, 0))
            if "inserter" in ename:
                mult = 2 if "long-handed" in ename else 1
                from foundry.belt_library import INSERTER_OFFSETS

                offsets = INSERTER_OFFSETS.get(edir, ((0, -1), (0, 1)))
                dr_dx = offsets[1][0] * mult
                dr_dy = offsets[1][1] * mult
                nxt = (curr[0] + dr_dx, curr[1] + dr_dy)
                outgoing.append((nxt, e))
            elif "transport-belt" in ename:
                nxt = (curr[0] + edx, curr[1] + edy)
                outgoing.append((nxt, e))
            elif "underground-belt" in ename and e.get("type") == "input":
                for span in range(1, 15):
                    out_pos = (curr[0] + edx * span, curr[1] + edy * span)
                    for cand in tile_map.get(out_pos, []):
                        if (
                            cand.get("name") == ename
                            and cand.get("direction") == edir
                            and cand.get("type") == "output"
                        ):
                            nxt = out_pos
                            outgoing.append((nxt, cand))
                            break
                    if outgoing:
                        break
            elif "underground-belt" in ename and e.get("type") == "output":
                nxt = (curr[0] + edx, curr[1] + edy)
                outgoing.append((nxt, e))
            elif "splitter" in ename:
                nxt_straight = (curr[0] + edx, curr[1] + edy)
                pos = e.get("position", {})
                cx = float(pos.get("x", 0)) if isinstance(pos, dict) else float(pos[0])
                cy = float(pos.get("y", 0)) if isinstance(pos, dict) else float(pos[1])
                if edir in (DIRECTION_NORTH, DIRECTION_SOUTH):
                    other_lane = (
                        (math.floor(cx + 0.5), math.floor(cy))
                        if curr[0] == math.floor(cx - 0.5)
                        else (math.floor(cx - 0.5), math.floor(cy))
                    )
                else:
                    other_lane = (
                        (math.floor(cx), math.floor(cy + 0.5))
                        if curr[1] == math.floor(cy - 0.5)
                        else (math.floor(cx), math.floor(cy - 0.5))
                    )
                nxt_other = (other_lane[0] + edx, other_lane[1] + edy)
                if nxt_straight in tile_map:
                    outgoing.append((nxt_straight, e))
                elif nxt_other in tile_map:
                    outgoing.append((nxt_other, e))
                else:
                    outgoing.append((nxt_straight, e))

        if not outgoing:
            print("    STOP: No outgoing belt connection from this tile.")
            break
        fed_strs = [f"to {pt} by {e.get('name')}" for pt, e in outgoing]
        print(f"    Leads to: {fed_strs}")
        next_pt = outgoing[0][0]
        if next_pt in visited_trace:
            print(f"    STOP: Cycle detected at {next_pt}.")
            break
        curr = next_pt


def trace_conduit(start_tile: tuple[float, float], entities: list[dict[str, Any]]) -> None:
    """Trace fluid conduit component connectivity."""
    import collections
    import math

    from foundry.pump_insertion import build_conduit_graph

    adj, _tile_map, _pipe_entities = build_conduit_graph(entities)
    sx, sy = math.floor(start_tile[0]), math.floor(start_tile[1])
    start_node = ("pipe", sx, sy)
    print(f"\n[CONDUIT TRACE FROM ({sx}, {sy})]")
    if start_node not in adj:
        found_nodes = [
            k
            for k in adj
            if (len(k) == 3 and k[1] == sx and k[2] == sy)
            or (len(k) == 4 and k[2] == sx and k[3] == sy)
        ]
        print(f"  Start node {start_node} not in graph! Found nodes at tile: {found_nodes}")
        if found_nodes:
            start_node = found_nodes[0]
        else:
            return
    visited: set[Any] = set()
    queue = collections.deque([start_node])
    visited.add(start_node)
    while queue:
        curr = queue.popleft()
        for nb in adj.get(curr, ()):
            if nb not in visited:
                visited.add(nb)
                queue.append(nb)
    print(f"  Connected component has {len(visited)} nodes.")
    xs = [n[1] if len(n) == 3 else n[2] for n in visited]
    ys = [n[2] if len(n) == 3 else n[3] for n in visited]
    print(f"  X bounds: [{min(xs)}, {max(xs)}], Y bounds: [{min(ys)}, {max(ys)}]")
    comp_pumps = [n for n in visited if "pump" in n[0]]
    print(f"  Pumps in component ({len(comp_pumps)}): {comp_pumps}")


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
    trace_downstream_tile: tuple[int, int] | None = None,
    trace_conduit_tile: tuple[float, float] | None = None,
    no_untangle: bool = False,
    no_cache: bool = False,
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

    cache_file: Path | None = None
    cache_loaded = False
    if not no_cache:
        try:
            mtime_str = f"{def_path.resolve()}_{def_path.stat().st_mtime_ns}_{netlist_path.resolve()}_{netlist_path.stat().st_mtime_ns}_{no_untangle}_{verbose}"
            cache_hash = hashlib.sha256(mtime_str.encode("utf-8")).hexdigest()[:16]
            cache_file = Path(tempfile.gettempdir()) / f"debug_drc_{def_path.stem}_{cache_hash}.pkl"
            if cache_file.is_file():
                with cache_file.open("rb") as f:
                    cdata = pickle.load(f)
                def_data = cdata["def_data"]
                netlist_data = cdata["netlist_data"]
                placement = cdata["placement"]
                routes = cdata["routes"]
                entities = cdata["entities"]
                violations = cdata["violations"]
                unresolved_conflicts = cdata["unresolved_conflicts"]
                debug_trace = cdata.get("debug_trace", {})
                cache_loaded = True
        except Exception:
            cache_loaded = False

    if not cache_loaded:
        def_data = parse_def(def_path.read_text(encoding="utf-8"))
        netlist_data = json.loads(netlist_path.read_text(encoding="utf-8"))
        # 1. Pre-routing untangling (three-phase strategy: topological, geometric, physical)
        if no_untangle:
            from foundry.untangle.channels import route_unrouted_signal_nets

            route_unrouted_signal_nets(def_data, netlist_data)
        else:
            untangle_def(def_data, netlist_data)

        # 2. Placement
        placement = place_components(def_data, netlist_data)

        # 3. Route nets with maze router
        debug_trace: dict[tuple[float, float], list[dict[str, Any]]] = {}
        routes = route_with_maze_router(
            def_data,
            netlist_data,
            placement,
            strict_drc=False,
        )

        # 3b. Pipeline pump insertion
        from foundry.pump_insertion import insert_pipeline_pumps

        insert_pipeline_pumps(placement, routes, def_data, netlist_data)

        # 4. Prune dead-end belts
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

        if cache_file is not None:
            try:
                cdata = {
                    "def_data": def_data,
                    "netlist_data": netlist_data,
                    "placement": placement,
                    "routes": routes,
                    "entities": entities,
                    "violations": violations,
                    "unresolved_conflicts": unresolved_conflicts,
                    "debug_trace": debug_trace,
                }
                with cache_file.open("wb") as f:
                    pickle.dump(cdata, f)
            except Exception:
                pass

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

    pumps = [e for e in entities if e.get("name") == "pump"]
    if pumps:
        print(f"\n[INSERTED PUMPS: {len(pumps)}]")
        for p in pumps:
            pos = p.get("position", {})
            d = p.get("direction")
            px = pos.get("x", 0)
            py = pos.get("y", 0)
            nets1 = routes.tile_to_nets.get((round(px - 0.5, 1), round(py, 1)), set())
            nets2 = routes.tile_to_nets.get((round(px, 1), round(py - 0.5, 1)), set())
            nets3 = routes.tile_to_nets.get((round(px, 1), round(py + 0.5, 1)), set())
            p_nets = sorted(nets1 | nets2 | nets3)
            print(f"  - pump at ({px}, {py}) dir={d} nets={p_nets}")

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
            if abs(ex - fx) <= 5.0 and abs(ey - fy) <= 5.0:
                found_near = True
                u_type = f" type={ent.get('type')}" if "type" in ent else ""
                nets = routes.tile_to_nets.get((round(ex, 1), round(ey, 1)), set())
                print(
                    f"  - {ent.get('name')} dir={ent.get('direction')}{u_type} at ({ex:.1f}, {ey:.1f}) nets={sorted(nets)}"
                )
        nearby = format_nearby_components(fx, fy, def_data.get("components", []))
        if nearby:
            print("  Nearby components:")
            for n in nearby:
                print(f"  {n}")
        if not found_near:
            print("  (no entities found within radius 5.0)")

        scale = 1.0 / float(def_data.get("units", 1000))
        print(f"\n[WIRES NEAR ({fx:.1f}, {fy:.1f})]")
        found_wire = False
        for net in def_data.get("nets", []):
            nname = net.get("name", "")
            for w in net.get("wires", []):
                x1 = w["x1"] * scale
                y1 = w["y1"] * scale
                x2 = w["x2"] * scale
                y2 = w["y2"] * scale
                min_wx, max_wx = min(x1, x2), max(x1, x2)
                min_wy, max_wy = min(y1, y2), max(y1, y2)
                if min_wx - 1.0 <= fx <= max_wx + 1.0 and min_wy - 1.0 <= fy <= max_wy + 1.0:
                    found_wire = True
                    print(
                        f"  - net {nname} {w.get('layer')} ({x1:.1f}, {y1:.1f}) -> ({x2:.1f}, {y2:.1f})"
                    )
        if not found_wire:
            print("  (no wires found within radius 1.0)")

    # DRC violations breakdown
    if violations:
        print("\n[DRC VIOLATIONS]")
        for v in violations:
            print(f"  - [{v.code}] at ({v.x:.1f}, {v.y:.1f}): {v.message}")

    if trace_reachability:
        trace_upstream(trace_reachability, entities)

    if trace_downstream_tile:
        trace_downstream(trace_downstream_tile, entities)

    if trace_conduit_tile:
        trace_conduit(trace_conduit_tile, entities)

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
    trace_down_tile = None
    if args.trace_downstream:
        parts = args.trace_downstream.split(",")
        if len(parts) == 2:
            trace_down_tile = (int(parts[0]), int(parts[1]))
    trace_conduit_pt = None
    if args.trace_conduit:
        parts = args.trace_conduit.split(",")
        if len(parts) == 2:
            trace_conduit_pt = (float(parts[0]), float(parts[1]))
    return run_diagnostics(
        args.def_file,
        args.netlist_json,
        filter_tile=filter_tile,
        verbose=args.verbose,
        trace_reachability=trace_tile,
        trace_downstream_tile=trace_down_tile,
        trace_conduit_tile=trace_conduit_pt,
        no_untangle=args.no_untangle,
        no_cache=args.no_cache,
    )


if __name__ == "__main__":
    sys.exit(main())
