"""Diagnose physical DRC violations and routing conflicts on Factorio layouts.

Loads placed DEF and netlist metadata.
Traces jumper synthesis and conflict resolution.
Reports root cause diagnostics for DRC violations.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import pickle
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from foundry.belt_drc import BeltDrcChecker
from foundry.belt_library import INSERTER_OFFSETS
from foundry.def2bp import prune_dead_end_belts, route_with_maze_router
from foundry.def_parser import parse_def
from foundry.def_untangle import untangle_def
from foundry.entity_helpers import (
    DIR_STEPS,
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
)
from foundry.placement import RouteResult, place_components
from foundry.pump_insertion import build_conduit_graph, insert_pipeline_pumps
from foundry.untangle.channels import route_unrouted_signal_nets

DEFAULT_UPSTREAM_MAX_STEPS = 5000
DEFAULT_DOWNSTREAM_MAX_STEPS = 500
MAX_UNDERGROUND_SPAN = 15
SPLITTER_HALF_OFFSET = 0.5
CONDUIT_SHORT_NODE_LEN = 3
CONDUIT_LONG_NODE_LEN = 4
CONDUIT_LONG_Y_IDX = 3
DEFAULT_NEARBY_RADIUS = 3.0
NEARBY_ENTITY_RADIUS = 5.0
DEF_UNITS_PER_TILE = 1000.0
DEFAULT_DEF_UNITS = 1000
CACHE_HASH_LEN = 16
EXIT_FILE_ERROR = 2
COORD_PAIR_LEN = 2

UPSTREAM_DIRS = (
    ((1, 0), DIRECTION_WEST),
    ((-1, 0), DIRECTION_EAST),
    ((0, 1), DIRECTION_NORTH),
    ((0, -1), DIRECTION_SOUTH),
)


def _as_float(val: object, default: float = 0.0) -> float:
    """Return val converted to float, or default."""
    if isinstance(val, (int, float)):
        return float(val)
    return default


def _as_int(val: object, default: int = 0) -> int:
    """Return val converted to int, or default."""
    if isinstance(val, (int, float)):
        return int(val)
    return default


def _entity_coords(ent: Mapping[str, object]) -> tuple[float, float]:
    """Extract (x, y) coordinates from an entity dictionary or sequence."""
    pos = ent.get("position", {})
    if isinstance(pos, Mapping):
        return _as_float(pos.get("x")), _as_float(pos.get("y"))
    if isinstance(pos, Sequence) and len(pos) >= COORD_PAIR_LEN:
        return _as_float(pos[0]), _as_float(pos[1])
    return 0.0, 0.0


def _build_entity_tile_map(
    entities: Sequence[Mapping[str, object]],
) -> dict[tuple[int, int], list[Mapping[str, object]]]:
    """Map integer tile coordinates to all entities occupying each tile."""
    tile_map: dict[tuple[int, int], list[Mapping[str, object]]] = {}
    for ent in entities:
        cx, cy = _entity_coords(ent)
        name = str(ent.get("name", ""))
        if "splitter" in name:
            d = _as_int(ent.get("direction"), DIRECTION_NORTH)
            if d in (DIRECTION_NORTH, DIRECTION_SOUTH):
                t1 = (math.floor(cx - SPLITTER_HALF_OFFSET), math.floor(cy))
                t2 = (math.floor(cx + SPLITTER_HALF_OFFSET), math.floor(cy))
            else:
                t1 = (math.floor(cx), math.floor(cy - SPLITTER_HALF_OFFSET))
                t2 = (math.floor(cx), math.floor(cy + SPLITTER_HALF_OFFSET))
            tile_map.setdefault(t1, []).append(ent)
            tile_map.setdefault(t2, []).append(ent)
        else:
            tile_map.setdefault((math.floor(cx), math.floor(cy)), []).append(ent)
    return tile_map


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
        help="Optional specific tile coordinate to inspect, formatted as 'X,Y' (for example '53.5,54.5')",
    )
    parser.add_argument(
        "--trace-reachability",
        type=str,
        default="",
        help="Trace upstream belt supply backwards from 'X,Y' (for example '269,419')",
    )
    parser.add_argument(
        "--trace-downstream",
        type=str,
        default="",
        help="Trace downstream belt path forward from 'X,Y' (for example '443,775')",
    )
    parser.add_argument(
        "--trace-conduit",
        type=str,
        default="",
        help="Trace fluid conduit connectivity from 'X,Y' (for example '35,2365')",
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


def _feeds_direction(ent: Mapping[str, object], target_dir: int) -> bool:
    """Return True if ent outputs items in target_dir."""
    pname = str(ent.get("name", ""))
    pdir = _as_int(ent.get("direction"))
    if pdir != target_dir:
        return False
    return (
        "transport-belt" in pname
        or ("underground-belt" in pname and ent.get("type") == "output")
        or "splitter" in pname
    )


def _find_upstream_underground_inputs(
    curr: tuple[int, int],
    ent: Mapping[str, object],
    tile_map: Mapping[tuple[int, int], Sequence[Mapping[str, object]]],
) -> list[tuple[tuple[int, int], Mapping[str, object]]]:
    """Find matching underground belt input entities for an output underground belt."""
    edir = _as_int(ent.get("direction"))
    edx, edy = DIR_STEPS.get(edir, (0, 0))
    matches: list[tuple[tuple[int, int], Mapping[str, object]]] = []
    for span in range(1, MAX_UNDERGROUND_SPAN):
        in_pos = (curr[0] - edx * span, curr[1] - edy * span)
        matches.extend(
            (in_pos, cand)
            for cand in tile_map.get(in_pos, ())
            if cand.get("name") == ent.get("name")
            and cand.get("direction") == edir
            and cand.get("type") == "input"
        )
    return matches


def _collect_incoming_step(
    curr: tuple[int, int],
    ents: Sequence[Mapping[str, object]],
    tile_map: Mapping[tuple[int, int], Sequence[Mapping[str, object]]],
) -> list[tuple[tuple[int, int], Mapping[str, object]]]:
    """Collect all upstream entities feeding into curr."""
    incoming: list[tuple[tuple[int, int], Mapping[str, object]]] = []
    for (dx, dy), d_towards in UPSTREAM_DIRS:
        prev_tile = (curr[0] + dx, curr[1] + dy)
        incoming.extend(
            (prev_tile, pe) for pe in tile_map.get(prev_tile, ()) if _feeds_direction(pe, d_towards)
        )

    for e in ents:
        if "underground-belt" in str(e.get("name", "")) and e.get("type") == "output":
            incoming.extend(_find_upstream_underground_inputs(curr, e, tile_map))
    return incoming


def trace_upstream(
    start_tile: tuple[int, int],
    entities: Sequence[Mapping[str, object]],
    max_steps: int = DEFAULT_UPSTREAM_MAX_STEPS,
) -> None:
    """Trace backwards from a tile along upstream belts and splitters."""
    tile_map = _build_entity_tile_map(entities)
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

        incoming = _collect_incoming_step(curr, ents, tile_map)
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


def _find_downstream_underground_output(
    curr: tuple[int, int],
    ename: str,
    edir: int,
    edx: int,
    edy: int,
    tile_map: Mapping[tuple[int, int], Sequence[Mapping[str, object]]],
) -> tuple[tuple[int, int], Mapping[str, object]] | None:
    """Find the matching underground output entity downstream from curr."""
    for span in range(1, MAX_UNDERGROUND_SPAN):
        out_pos = (curr[0] + edx * span, curr[1] + edy * span)
        for cand in tile_map.get(out_pos, ()):
            if (
                cand.get("name") == ename
                and cand.get("direction") == edir
                and cand.get("type") == "output"
            ):
                return (out_pos, cand)
    return None


def _splitter_other_lane(
    curr: tuple[int, int],
    ent: Mapping[str, object],
    edir: int,
) -> tuple[int, int]:
    """Compute the sibling lane tile coordinate of a splitter."""
    cx, cy = _entity_coords(ent)
    if edir in (DIRECTION_NORTH, DIRECTION_SOUTH):
        minus_x = math.floor(cx - SPLITTER_HALF_OFFSET)
        plus_x = math.floor(cx + SPLITTER_HALF_OFFSET)
        return (plus_x if curr[0] == minus_x else minus_x, math.floor(cy))
    minus_y = math.floor(cy - SPLITTER_HALF_OFFSET)
    plus_y = math.floor(cy + SPLITTER_HALF_OFFSET)
    return (math.floor(cx), plus_y if curr[1] == minus_y else minus_y)


def _splitter_outgoing_tile(
    curr: tuple[int, int],
    ent: Mapping[str, object],
    edir: int,
    edx: int,
    edy: int,
    tile_map: Mapping[tuple[int, int], Sequence[Mapping[str, object]]],
) -> tuple[int, int]:
    """Resolve the downstream output tile for a splitter entity."""
    nxt_straight = (curr[0] + edx, curr[1] + edy)
    if nxt_straight in tile_map:
        return nxt_straight
    other_lane = _splitter_other_lane(curr, ent, edir)
    nxt_other = (other_lane[0] + edx, other_lane[1] + edy)
    return nxt_other if nxt_other in tile_map else nxt_straight


def _entity_outgoing_steps(
    curr: tuple[int, int],
    ent: Mapping[str, object],
    tile_map: Mapping[tuple[int, int], Sequence[Mapping[str, object]]],
) -> list[tuple[tuple[int, int], Mapping[str, object]]]:
    """Compute downstream step targets for a single entity at curr."""
    ename = str(ent.get("name", ""))
    edir = _as_int(ent.get("direction"), DIRECTION_NORTH)
    edx, edy = DIR_STEPS.get(edir, (0, 0))
    if "inserter" in ename:
        mult = COORD_PAIR_LEN if "long-handed" in ename else 1
        offsets = INSERTER_OFFSETS.get(edir, ((0, -1), (0, 1)))
        return [((curr[0] + offsets[1][0] * mult, curr[1] + offsets[1][1] * mult), ent)]
    if "transport-belt" in ename or ("underground-belt" in ename and ent.get("type") == "output"):
        return [((curr[0] + edx, curr[1] + edy), ent)]
    if "underground-belt" in ename and ent.get("type") == "input":
        found = _find_downstream_underground_output(curr, ename, edir, edx, edy, tile_map)
        return [found] if found is not None else []
    if "splitter" in ename:
        return [(_splitter_outgoing_tile(curr, ent, edir, edx, edy, tile_map), ent)]
    return []


def trace_downstream(
    start_tile: tuple[int, int],
    entities: Sequence[Mapping[str, object]],
    max_steps: int = DEFAULT_DOWNSTREAM_MAX_STEPS,
) -> None:
    """Trace forward from a tile along downstream belts and splitters."""
    tile_map = _build_entity_tile_map(entities)
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

        outgoing: list[tuple[tuple[int, int], Mapping[str, object]]] = []
        for e in ents:
            outgoing.extend(_entity_outgoing_steps(curr, e, tile_map))

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


def _conduit_node_coords(node: tuple[object, ...]) -> tuple[int, int]:
    """Return (x, y) coordinates from a conduit graph node tuple."""
    if len(node) == CONDUIT_SHORT_NODE_LEN:
        return _as_int(node[1]), _as_int(node[2])
    return _as_int(node[2]), _as_int(node[CONDUIT_LONG_Y_IDX])


def _bfs_conduit_component(
    adj: Mapping[tuple[object, ...], Iterable[tuple[object, ...]]],
    start_node: tuple[object, ...],
) -> set[tuple[object, ...]]:
    """Perform breadth-first search on a conduit adjacency map."""
    visited: set[tuple[object, ...]] = {start_node}
    queue = collections.deque([start_node])
    while queue:
        curr = queue.popleft()
        for nb in adj.get(curr, ()):
            if nb not in visited:
                visited.add(nb)
                queue.append(nb)
    return visited


def trace_conduit(
    start_tile: tuple[float, float],
    entities: Sequence[Mapping[str, object]],
) -> None:
    """Trace fluid conduit component connectivity."""
    ent_dicts = [dict(e) for e in entities]
    raw_adj, _tile_map, _pipe_entities = build_conduit_graph(ent_dicts)
    adj: dict[tuple[object, ...], set[tuple[object, ...]]] = {
        tuple(k): {tuple(nb) for nb in v} for k, v in raw_adj.items()
    }
    sx, sy = math.floor(start_tile[0]), math.floor(start_tile[1])
    start_node: tuple[object, ...] = ("pipe", sx, sy)
    print(f"\n[CONDUIT TRACE FROM ({sx}, {sy})]")
    if start_node not in adj:
        found_nodes = [
            k
            for k in adj
            if len(k) in (CONDUIT_SHORT_NODE_LEN, CONDUIT_LONG_NODE_LEN)
            and _conduit_node_coords(k) == (sx, sy)
        ]
        print(f"  Start node {start_node} not in graph! Found nodes at tile: {found_nodes}")
        if not found_nodes:
            return
        start_node = found_nodes[0]

    visited = _bfs_conduit_component(adj, start_node)
    print(f"  Connected component has {len(visited)} nodes.")
    coords = [_conduit_node_coords(n) for n in visited]
    xs = [pt[0] for pt in coords]
    ys = [pt[1] for pt in coords]
    print(f"  X bounds: [{min(xs)}, {max(xs)}], Y bounds: [{min(ys)}, {max(ys)}]")
    comp_pumps = [n for n in visited if "pump" in str(n[0])]
    print(f"  Pumps in component ({len(comp_pumps)}): {comp_pumps}")


def _build_component_bucket_map(
    components: Sequence[Mapping[str, object]],
) -> dict[tuple[int, int], list[tuple[float, float, str]]]:
    """Bucket formatted component entries by integer tile coordinate."""
    buckets: dict[tuple[int, int], list[tuple[float, float, str]]] = collections.defaultdict(list)
    for comp in components:
        comp_x = _as_float(comp.get("x")) / DEF_UNITS_PER_TILE
        comp_y = _as_float(comp.get("y")) / DEF_UNITS_PER_TILE
        line = f"  - {comp.get('name')} ({comp.get('macro')}) at ({comp_x:.1f}, {comp_y:.1f})"
        buckets[(math.floor(comp_x), math.floor(comp_y))].append((comp_x, comp_y, line))
    return buckets


def _lookup_nearby_from_buckets(
    cx: float,
    cy: float,
    buckets: Mapping[tuple[int, int], Sequence[tuple[float, float, str]]],
    radius: float = DEFAULT_NEARBY_RADIUS,
) -> list[str]:
    """Query spatially bucketed components within radius of (cx, cy)."""
    r_int = math.ceil(radius) + 1
    bx_center, by_center = math.floor(cx), math.floor(cy)
    nearby: list[str] = []
    for dx in range(-r_int, r_int + 1):
        for dy in range(-r_int, r_int + 1):
            for comp_x, comp_y, line in buckets.get((bx_center + dx, by_center + dy), ()):
                if abs(comp_x - cx) <= radius and abs(comp_y - cy) <= radius:
                    nearby.append(line)
    return nearby


def format_nearby_components(
    cx: float,
    cy: float,
    components: Sequence[Mapping[str, object]],
    radius: float = DEFAULT_NEARBY_RADIUS,
) -> list[str]:
    """Find and format components located near a target coordinate."""
    buckets = _build_component_bucket_map(components)
    return _lookup_nearby_from_buckets(cx, cy, buckets, radius)


def summarize_failures(attempts: Sequence[Mapping[str, object]]) -> list[str]:
    """Summarize unique reasons why candidate jumper spans were rejected."""
    reasons: dict[str, int] = {}
    buckets = (
        ("fail_in", "Entrance"),
        ("fail_out", "Exit"),
        ("invalid_reasons", "Span"),
    )
    for att in attempts:
        for field_key, label in buckets:
            items = att.get(field_key)
            if isinstance(items, list):
                for r in items:
                    key = f"{label}: {r}"
                    reasons[key] = reasons.get(key, 0) + 1
    return [f"{reason} ({count} span attempts)" for reason, count in sorted(reasons.items())]


def _resolve_workspace_path(path: Path, env: Mapping[str, str]) -> Path:
    """Resolve a relative path against BUILD_WORKSPACE_DIRECTORY if needed."""
    workspace = env.get("BUILD_WORKSPACE_DIRECTORY", "")
    if workspace and not path.is_absolute() and not path.is_file():
        candidate = Path(workspace) / path
        if candidate.is_file():
            return candidate
    return path


def _cache_file_for(
    def_path: Path,
    netlist_path: Path,
    no_untangle: bool,
    verbose: bool,
) -> Path | None:
    """Compute cache file path for diagnostic run state."""
    try:
        mtime_str = (
            f"{def_path.resolve()}_{def_path.stat().st_mtime_ns}_"
            f"{netlist_path.resolve()}_{netlist_path.stat().st_mtime_ns}_"
            f"{no_untangle}_{verbose}"
        )
        cache_hash = hashlib.sha256(mtime_str.encode("utf-8")).hexdigest()[:CACHE_HASH_LEN]
        return Path(tempfile.gettempdir()) / f"debug_drc_{def_path.stem}_{cache_hash}.pkl"
    except Exception:
        return None


def _compute_diagnostic_state(
    def_path: Path,
    netlist_path: Path,
    no_untangle: bool,
) -> dict[str, Any]:
    """Execute placement, routing, pump insertion, and DRC checks."""
    def_data = parse_def(def_path.read_text(encoding="utf-8"))
    netlist_data = json.loads(netlist_path.read_text(encoding="utf-8"))
    if no_untangle:
        route_unrouted_signal_nets(def_data, netlist_data)
    else:
        untangle_def(def_data, netlist_data)

    placement = place_components(def_data, netlist_data)
    debug_trace: dict[tuple[float, float], list[dict[str, Any]]] = {}
    routes = route_with_maze_router(
        def_data,
        netlist_data,
        placement,
        strict_drc=False,
    )
    insert_pipeline_pumps(placement, routes, def_data, netlist_data)
    entities = prune_dead_end_belts(placement.entities, def_data, netlist_data)

    scale = 1.0 / float(def_data.get("units", DEFAULT_DEF_UNITS))
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
    return {
        "def_data": def_data,
        "netlist_data": netlist_data,
        "placement": placement,
        "routes": routes,
        "entities": entities,
        "violations": violations,
        "unresolved_conflicts": unresolved_conflicts,
        "debug_trace": debug_trace,
    }


def _load_or_compute_state(
    def_path: Path,
    netlist_path: Path,
    no_untangle: bool,
    verbose: bool,
    no_cache: bool,
) -> dict[str, Any]:
    """Load cached diagnostic state or compute and cache a fresh state."""
    cache_file = None if no_cache else _cache_file_for(def_path, netlist_path, no_untangle, verbose)
    if cache_file is not None and cache_file.is_file():
        try:
            with cache_file.open("rb") as f:
                cdata = pickle.load(f)
            cdata.setdefault("debug_trace", {})
            return cdata
        except Exception:
            pass

    state = _compute_diagnostic_state(def_path, netlist_path, no_untangle)
    if cache_file is not None:
        try:
            with cache_file.open("wb") as f:
                pickle.dump(state, f)
        except Exception:
            pass
    return state


def _print_jumpers_and_pumps(
    debug_trace: Mapping[tuple[float, float], Sequence[Mapping[str, object]]],
    entities: Sequence[Mapping[str, object]],
    routes: RouteResult,
    verbose: bool,
    filter_tile: tuple[float, float] | None,
) -> None:
    """Print committed jumpers and inserted pumps."""
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
            px, py = _entity_coords(p)
            d = p.get("direction")
            nets1 = routes.tile_to_nets.get(
                (round(px - SPLITTER_HALF_OFFSET, 1), round(py, 1)), set()
            )
            nets2 = routes.tile_to_nets.get(
                (round(px, 1), round(py - SPLITTER_HALF_OFFSET, 1)), set()
            )
            nets3 = routes.tile_to_nets.get(
                (round(px, 1), round(py + SPLITTER_HALF_OFFSET, 1)), set()
            )
            p_nets = sorted(nets1 | nets2 | nets3)
            print(f"  - pump at ({px}, {py}) dir={d} nets={p_nets}")


def _print_candidate_evaluation(
    cand: str,
    pos: tuple[float, float],
    routes: RouteResult,
    net_wires_by_name: Mapping[str, object],
    cand_attempts: Sequence[Mapping[str, object]],
    verbose: bool,
) -> None:
    """Print candidate jumper evaluation details for one net at a conflict tile."""
    d = routes.net_surface_tiles.get(cand, {}).get(pos)
    print(f"  Candidate net: {cand} (travel direction: {d})")
    if cand in net_wires_by_name:
        print(f"    Wires: {net_wires_by_name[cand]}")
    reasons = summarize_failures(cand_attempts)
    if reasons:
        print("    Rejection summary:")
        for r in reasons:
            print(f"      - {r}")
    if verbose:
        print("    Detailed span evaluations:")
        for att in cand_attempts:
            print(
                f"      b={att.get('bspan')} f={att.get('fspan')} "
                f"in={att.get('in_pos')} out={att.get('out_pos')} "
                f"valid={att.get('valid_span')} fail_in={att.get('fail_in')} "
                f"fail_out={att.get('fail_out')} inv={att.get('invalid_reasons')}"
            )


def _group_attempts_by_candidate(
    attempts: Sequence[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    """Group non-action jumper span attempts by candidate net name."""
    by_cand: dict[str, list[Mapping[str, object]]] = collections.defaultdict(list)
    for att in attempts:
        if att.get("action") is None:
            by_cand[str(att.get("candidate", ""))].append(att)
    return by_cand


def _print_conflict_details(
    displayed_conflicts: Sequence[tuple[float, float]],
    routes: RouteResult,
    def_data: Mapping[str, object],
    debug_trace: Mapping[tuple[float, float], Sequence[Mapping[str, object]]],
    verbose: bool,
) -> None:
    """Print summary and detailed breakdown of surface routing conflicts."""
    if not displayed_conflicts:
        return
    print(f"\n[CONFLICT SUMMARY: {len(displayed_conflicts)} conflicts]")
    for pos in sorted(displayed_conflicts):
        cx, cy = pos
        nets = sorted(routes.tile_to_nets.get(pos, set()))
        print(f"  - ({cx:.1f}, {cy:.1f}): {nets}")

    raw_nets = def_data.get("nets")
    net_list = raw_nets if isinstance(raw_nets, list) else []
    net_wires_by_name = {
        str(n.get("name", "")): n.get("wires") for n in net_list if isinstance(n, Mapping)
    }
    raw_comps = def_data.get("components")
    components = (
        [c for c in raw_comps if isinstance(c, Mapping)] if isinstance(raw_comps, list) else []
    )
    comp_buckets = _build_component_bucket_map(components)
    print("\n[CONFLICT DETAIL]")
    for pos in sorted(displayed_conflicts):
        cx, cy = pos
        nets = sorted(routes.tile_to_nets.get(pos, set()))
        print(f"\nConflict at ({cx:.1f}, {cy:.1f}) across {len(nets)} nets: {nets}")
        nearby = _lookup_nearby_from_buckets(cx, cy, comp_buckets)
        if nearby:
            print(" Nearby components:")
            for n in nearby:
                print(n)
        by_cand = _group_attempts_by_candidate(debug_trace.get(pos, ()))
        for cand in nets:
            _print_candidate_evaluation(
                cand, pos, routes, net_wires_by_name, by_cand.get(cand, []), verbose
            )


def _format_wires_near(
    fx: float,
    fy: float,
    nets: Sequence[Mapping[str, object]],
    scale: float,
) -> list[str]:
    """Return formatted wire strings within 1.0 tile of (fx, fy)."""
    wire_lines: list[str] = []
    for net in nets:
        nname = net.get("name", "")
        raw_wires = net.get("wires")
        wires = raw_wires if isinstance(raw_wires, list) else []
        for w in wires:
            if not isinstance(w, Mapping):
                continue
            x1, y1 = _as_float(w.get("x1")) * scale, _as_float(w.get("y1")) * scale
            x2, y2 = _as_float(w.get("x2")) * scale, _as_float(w.get("y2")) * scale
            if (
                min(x1, x2) - 1.0 <= fx <= max(x1, x2) + 1.0
                and min(y1, y2) - 1.0 <= fy <= max(y1, y2) + 1.0
            ):
                wire_lines.append(
                    f"  - net {nname} {w.get('layer')} ({x1:.1f}, {y1:.1f}) -> ({x2:.1f}, {y2:.1f})"
                )
    return wire_lines


def _format_entities_near(
    fx: float,
    fy: float,
    entities: Sequence[Mapping[str, object]],
    routes: RouteResult,
) -> list[str]:
    """Return formatted entity lines within NEARBY_ENTITY_RADIUS of (fx, fy)."""
    lines: list[str] = []
    for ent in entities:
        ex, ey = _entity_coords(ent)
        if abs(ex - fx) <= NEARBY_ENTITY_RADIUS and abs(ey - fy) <= NEARBY_ENTITY_RADIUS:
            u_type = f" type={ent.get('type')}" if "type" in ent else ""
            nets = routes.tile_to_nets.get((round(ex, 1), round(ey, 1)), set())
            lines.append(
                f"  - {ent.get('name')} dir={ent.get('direction')}{u_type} "
                f"at ({ex:.1f}, {ey:.1f}) nets={sorted(nets)}"
            )
    return lines


def _extract_mapping_list(def_data: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    """Extract a list of Mapping items from def_data[key]."""
    raw = def_data.get(key)
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def _print_filter_tile_details(
    filter_tile: tuple[float, float],
    entities: Sequence[Mapping[str, object]],
    routes: RouteResult,
    def_data: Mapping[str, object],
) -> None:
    """Print entities, components, and wires near filter_tile."""
    fx, fy = filter_tile
    print(f"\n[ENTITIES NEAR ({fx:.1f}, {fy:.1f})]")
    ent_lines = _format_entities_near(fx, fy, entities, routes)
    for line in ent_lines:
        print(line)
    nearby = format_nearby_components(fx, fy, _extract_mapping_list(def_data, "components"))
    if nearby:
        print("  Nearby components:")
        for n in nearby:
            print(f"  {n}")
    if not ent_lines:
        print("  (no entities found within radius 5.0)")

    scale = 1.0 / _as_float(def_data.get("units"), float(DEFAULT_DEF_UNITS))
    print(f"\n[WIRES NEAR ({fx:.1f}, {fy:.1f})]")
    wire_lines = _format_wires_near(fx, fy, _extract_mapping_list(def_data, "nets"), scale)
    if wire_lines:
        for line in wire_lines:
            print(line)
    else:
        print("  (no wires found within radius 1.0)")


def _run_optional_traces(
    entities: Sequence[Mapping[str, object]],
    trace_reachability: tuple[int, int] | None,
    trace_downstream_tile: tuple[int, int] | None,
    trace_conduit_tile: tuple[float, float] | None,
) -> None:
    """Execute optional upstream, downstream, and conduit traces."""
    if trace_reachability:
        trace_upstream(trace_reachability, entities)
    if trace_downstream_tile:
        trace_downstream(trace_downstream_tile, entities)
    if trace_conduit_tile:
        trace_conduit(trace_conduit_tile, entities)


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
    env: Mapping[str, str] = os.environ,
) -> int:
    """Run full physical DRC and routing conflict analysis."""
    def_path = _resolve_workspace_path(def_path, env)
    netlist_path = _resolve_workspace_path(netlist_path, env)

    if not def_path.is_file():
        print(f"ERROR: DEF file not found: {def_path}", file=sys.stderr)
        return EXIT_FILE_ERROR
    if not netlist_path.is_file():
        print(f"ERROR: Netlist JSON not found: {netlist_path}", file=sys.stderr)
        return EXIT_FILE_ERROR

    state = _load_or_compute_state(def_path, netlist_path, no_untangle, verbose, no_cache)
    def_data = state["def_data"]
    routes = state["routes"]
    entities = state["entities"]
    violations = state["violations"]
    unresolved_conflicts = state["unresolved_conflicts"]
    debug_trace = state["debug_trace"]

    print("================================================================================")
    print(f"PHYSICAL DRC AND ROUTING DIAGNOSTIC REPORT: {def_path.name}")
    print("================================================================================")
    print(f"Unresolved surface crossings : {len(unresolved_conflicts)}")
    print(f"Physical DRC violations      : {len(violations)}")
    print("--------------------------------------------------------------------------------")

    _print_jumpers_and_pumps(debug_trace, entities, routes, verbose, filter_tile)

    displayed_conflicts = (
        [filter_tile]
        if filter_tile and filter_tile in unresolved_conflicts
        else unresolved_conflicts
    )
    _print_conflict_details(displayed_conflicts, routes, def_data, debug_trace, verbose)

    if filter_tile:
        _print_filter_tile_details(filter_tile, entities, routes, def_data)

    if violations:
        print("\n[DRC VIOLATIONS]")
        for v in violations:
            print(f"  - [{v.code}] at ({v.x:.1f}, {v.y:.1f}): {v.message}")

    _run_optional_traces(entities, trace_reachability, trace_downstream_tile, trace_conduit_tile)

    print("\n--------------------------------------------------------------------------------")
    if violations:
        print(f"FAILED: {len(violations)} physical DRC violation(s) found.")
        return 1

    print("PASSED: 0 physical DRC violations found.")
    return 0


def _parse_float_pair(raw: str) -> tuple[float, float] | None:
    """Parse a comma-separated float pair 'X,Y' or return None."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) == COORD_PAIR_LEN:
        return (float(parts[0]), float(parts[1]))
    return None


def _parse_int_pair(raw: str) -> tuple[int, int] | None:
    """Parse a comma-separated integer pair 'X,Y' or return None."""
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) == COORD_PAIR_LEN:
        return (int(parts[0]), int(parts[1]))
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return run_diagnostics(
        args.def_file,
        args.netlist_json,
        filter_tile=_parse_float_pair(args.tile),
        verbose=args.verbose,
        trace_reachability=_parse_int_pair(args.trace_reachability),
        trace_downstream_tile=_parse_int_pair(args.trace_downstream),
        trace_conduit_tile=_parse_float_pair(args.trace_conduit),
        no_untangle=args.no_untangle,
        no_cache=args.no_cache,
    )


if __name__ == "__main__":
    sys.exit(main())
