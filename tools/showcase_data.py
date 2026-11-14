"""Shared data extraction helpers for factorilog showcase artifacts.

Collects provenance, prototype categories, DEF metrics, and compact
blueprint geometry for deterministic SVGs and the interactive die viewer.
"""

from __future__ import annotations

import collections
import json
import math
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from foundry.def_parser import parse_def
from foundry.entity_helpers import (
    DEFAULT_DEF_UNITS,
    DEFAULT_PIPE_UNDERGROUND_SPAN,
    DEFAULT_UNDERGROUND_SPAN,
    DIR_STEPS,
    DIRECTION_NORTH,
    EAST_WEST_DIRS,
    OPPOSITE_DIR,
    entity_size,
    get_underground_span,
    register_prototype_boxes,
    register_underground_span,
)

BENCHMARK_TARGETS: tuple[str, ...] = (
    "automation_science",
    "automation_controlled_science",
    "automation_science_chassis",
    "automation_logistic_chassis",
    "automation_logistic_military_chassis",
    "automation_logistic_military_chemical_chassis",
    "automation_logistic_military_chemical_production_chassis",
    "automation_logistic_military_chemical_production_utility_chassis",
    "science_chassis",
)

PROTOTYPE_CATEGORY_KEYS: tuple[str, ...] = (
    "transport-belt",
    "underground-belt",
    "splitter",
    "pipe",
    "pipe-to-ground",
    "pump",
    "electric-pole",
    "inserter",
    "assembling-machine",
    "furnace",
    "boiler",
    "generator",
    "lab",
    "rocket-silo",
    "cargo-landing-pad",
    "radar",
    "lamp",
    "arithmetic-combinator",
    "decider-combinator",
    "constant-combinator",
    "display-panel",
    "power-switch",
)

UNDERGROUND_PROTO_KEYS: tuple[str, ...] = ("underground-belt", "pipe-to-ground")
CHASSIS_PITCH_TILES = 100
DEFAULT_CELL_DIM = 3
WIRE_ID_POLE_COPPER = 5
WIRE_ID_CIRCUIT_RED = 1
WIRE_ID_CIRCUIT_GREEN = 2
WIRE_ID_OUT_RED = 3
WIRE_ID_OUT_GREEN = 4
WIRE_TUPLE_LEN = 4
WINDOW_MIN_ENTITIES = 25
SHA_SHORT_LEN = 7


def load_prototype_catalog(
    dump_path: Path,
) -> tuple[dict[str, Any], dict[str, str], dict[str, int]]:
    """Load Factorio prototypes, entity categories, and underground spans from dump JSON."""
    prototypes: dict[str, Any] = json.loads(dump_path.read_text(encoding="utf-8"))
    register_prototype_boxes(prototypes)

    cat_map: dict[str, str] = {}
    for category in PROTOTYPE_CATEGORY_KEYS:
        entries = prototypes.get(category)
        if not isinstance(entries, Mapping):
            continue
        for proto_name in entries:
            if isinstance(proto_name, str) and proto_name not in cat_map:
                cat_map[proto_name] = category

    spans: dict[str, int] = {}
    for ug_cat in UNDERGROUND_PROTO_KEYS:
        entries = prototypes.get(ug_cat)
        if not isinstance(entries, Mapping):
            continue
        for proto_name, proto_data in entries.items():
            if not isinstance(proto_name, str) or not isinstance(proto_data, Mapping):
                continue
            span = _extract_span_from_proto(proto_data)
            if span is not None:
                register_underground_span(proto_name, span)
                spans[proto_name] = span
    return prototypes, cat_map, spans


def _extract_span_from_proto(proto: Mapping[str, Any]) -> int | None:
    """Extract underground tunnel distance from a prototype dictionary."""
    max_dist = proto.get("max_distance")
    if isinstance(max_dist, (int, float)) and not isinstance(max_dist, bool):
        return int(max_dist)
    fluid_box = proto.get("fluid_box")
    if isinstance(fluid_box, Mapping):
        for conn in fluid_box.get("pipe_connections", ()):
            if (
                isinstance(conn, Mapping)
                and conn.get("connection_type") == "underground"
                and isinstance(conn.get("max_underground_distance"), (int, float))
            ):
                return int(conn["max_underground_distance"])
    return None


def _run_git(workspace: Path, args: list[str]) -> str:
    """Run a read-only git command and return stripped stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def collect_git_provenance(workspace: Path) -> dict[str, str]:
    """Read short SHA, full SHA, commit date, and origin URL from git."""
    short_sha = _run_git(workspace, ["rev-parse", f"--short={SHA_SHORT_LEN}", "HEAD"])
    full_sha = _run_git(workspace, ["rev-parse", "HEAD"])
    commit_date = _run_git(workspace, ["show", "-s", "--format=%cs", "HEAD"])
    remote_url = _run_git(workspace, ["config", "--get", "remote.origin.url"])
    repo_https = _normalize_github_url(remote_url)
    return {
        "git_commit_short": short_sha or "unknown",
        "git_commit_full": full_sha or "unknown",
        "git_commit_date": commit_date or "unknown",
        "repo_url": repo_https,
    }


def _normalize_github_url(remote_url: str) -> str:
    """Normalize a git remote URL into an HTTPS repository URL."""
    cleaned = remote_url.strip()
    if cleaned.startswith("git@github.com:"):
        cleaned = "https://github.com/" + cleaned.removeprefix("git@github.com:")
    if cleaned.endswith(".git"):
        cleaned = cleaned.removesuffix(".git")
    return cleaned or "https://github.com/atvrager/factorilog"


def collect_cpu_provenance() -> dict[str, Any]:
    """Read host CPU model, physical core count, and logical thread count."""
    cpu_model = "Unknown CPU"
    core_pairs: set[tuple[str, str]] = set()
    cpuinfo_path = Path("/proc/cpuinfo")
    if cpuinfo_path.is_file():
        phys_id = "0"
        core_id = "0"
        for line in cpuinfo_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, val = (part.strip() for part in line.split(":", 1))
            if key == "model name" and cpu_model == "Unknown CPU" and val:
                cpu_model = val
            elif key == "physical id":
                phys_id = val
            elif key == "core id":
                core_id = val
                core_pairs.add((phys_id, core_id))

    logical_threads = os.cpu_count() or 1
    physical_cores = len(core_pairs) if core_pairs else logical_threads
    return {
        "cpu_model": cpu_model,
        "physical_cores": physical_cores,
        "logical_threads": logical_threads,
        "stage_concurrency": {
            "synth": "1 thread (Python LP solver and netlister)",
            "pnr": f"{logical_threads} threads (OpenROAD set_thread_count [exec nproc])",
            "bp": "1 thread (Python 3D maze router and MST power grid)",
            "render": "1 thread (Python Pillow sprite compositor)",
            "save": "1 thread (Headless Factorio save serializer)",
        },
    }


def extract_geometry_layers(
    bp_payload: Mapping[str, Any],
    cat_map: Mapping[str, str],
) -> dict[str, Any]:
    """Extract compacted belt, underground, pipe, and wire vectors from a blueprint."""
    entities = [ent for ent in bp_payload.get("entities", ()) if isinstance(ent, Mapping)]
    belts = _extract_merged_belt_segments(entities, cat_map)
    underground = _extract_underground_belt_spans(entities, cat_map)
    pipes = _extract_pipe_segments(entities, cat_map)
    wires_copper, wires_circuit = _extract_wire_segments(entities, bp_payload.get("wires", ()))
    return {
        "belts": belts,
        "underground": underground,
        "pipes": pipes,
        "copper_wires": wires_copper,
        "circuit_wires": wires_circuit,
    }


def _extract_merged_belt_segments(
    entities: Sequence[Mapping[str, Any]],
    cat_map: Mapping[str, str],
) -> list[list[float]]:
    """Merge collinear adjacent belt tiles pointing along the same axis."""
    belt_points: dict[tuple[int, float], set[float]] = collections.defaultdict(set)
    splitter_lines: list[list[float]] = []

    for ent in entities:
        name = str(ent.get("name", ""))
        cat = cat_map.get(name, "")
        pos = ent.get("position", {})
        if not isinstance(pos, Mapping):
            continue
        x = round(float(pos.get("x", 0.0)), 1)
        y = round(float(pos.get("y", 0.0)), 1)
        direction = int(ent.get("direction", DIRECTION_NORTH))
        if cat == "transport-belt":
            if direction in EAST_WEST_DIRS:
                belt_points[(direction, y)].add(x)
            else:
                belt_points[(direction, x)].add(y)
        elif cat == "splitter":
            if direction in EAST_WEST_DIRS:
                splitter_lines.append([x, round(y - 0.5, 1), x, round(y + 0.5, 1), direction])
            else:
                splitter_lines.append([round(x - 0.5, 1), y, round(x + 0.5, 1), y, direction])

    segments: list[list[float]] = []
    for (direction, fixed_coord), var_coords in sorted(belt_points.items()):
        sorted_vals = sorted(var_coords)
        run_start = sorted_vals[0]
        run_prev = sorted_vals[0]
        for val in sorted_vals[1:]:
            if round(val - run_prev, 1) <= 1.1:
                run_prev = val
            else:
                segments.append(_belt_run_entry(direction, fixed_coord, run_start, run_prev))
                run_start = val
                run_prev = val
        segments.append(_belt_run_entry(direction, fixed_coord, run_start, run_prev))

    segments.extend(splitter_lines)
    return segments


def _belt_run_entry(direction: int, fixed_coord: float, start: float, end: float) -> list[float]:
    """Build `[x1, y1, x2, y2, dir]` oriented from upstream to downstream."""
    dx, dy = DIR_STEPS.get(direction, (0, -1))
    if direction in EAST_WEST_DIRS:
        x1, x2 = (start, end) if dx > 0 else (end, start)
        return [round(x1, 1), round(fixed_coord, 1), round(x2, 1), round(fixed_coord, 1), direction]
    y1, y2 = (start, end) if dy > 0 else (end, start)
    return [round(fixed_coord, 1), round(y1, 1), round(fixed_coord, 1), round(y2, 1), direction]


def _extract_underground_belt_spans(
    entities: Sequence[Mapping[str, Any]],
    cat_map: Mapping[str, str],
) -> list[list[float]]:
    """Pair input and output underground belts into `[x1, y1, x2, y2]` segments."""
    inputs: list[Mapping[str, Any]] = []
    outputs_by_line: dict[tuple[str, int, float], list[Mapping[str, Any]]] = (
        collections.defaultdict(list)
    )
    for ent in entities:
        name = str(ent.get("name", ""))
        if cat_map.get(name) != "underground-belt":
            continue
        pos = ent.get("position", {})
        if not isinstance(pos, Mapping):
            continue
        direction = int(ent.get("direction", DIRECTION_NORTH))
        if ent.get("type") == "output":
            line_coord = round(
                float(pos["y"]) if direction in EAST_WEST_DIRS else float(pos["x"]), 1
            )
            outputs_by_line[(name, direction, line_coord)].append(ent)
        else:
            inputs.append(ent)

    matched_outputs: set[int] = set()
    spans: list[list[float]] = []
    for inp in inputs:
        name = str(inp.get("name", ""))
        direction = int(inp.get("direction", DIRECTION_NORTH))
        dx, dy = DIR_STEPS.get(direction, (0, 0))
        pos = inp["position"]
        ix, iy = float(pos["x"]), float(pos["y"])
        tier_span = get_underground_span(name, DEFAULT_UNDERGROUND_SPAN)
        line_coord = round(iy if direction in EAST_WEST_DIRS else ix, 1)

        best_out: Mapping[str, Any] | None = None
        best_dist = math.inf
        for cand in outputs_by_line.get((name, direction, line_coord), ()):
            if id(cand) in matched_outputs:
                continue
            cpos = cand["position"]
            dist = (float(cpos["x"]) - ix) * dx + (float(cpos["y"]) - iy) * dy
            if 1 <= dist <= tier_span and dist < best_dist:
                best_dist = dist
                best_out = cand

        if best_out is not None:
            matched_outputs.add(id(best_out))
            opos = best_out["position"]
            spans.append(
                [round(ix, 1), round(iy, 1), round(float(opos["x"]), 1), round(float(opos["y"]), 1)]
            )
    return spans


def _extract_pipe_segments(
    entities: Sequence[Mapping[str, Any]],
    cat_map: Mapping[str, str],
) -> list[list[float]]:
    """Extract surface pipe adjacencies and underground pipe jumps as `[x1, y1, x2, y2, ug]`."""
    pipe_tiles: set[tuple[float, float]] = set()
    ptg_by_pos_dir: dict[tuple[float, float, int], Mapping[str, Any]] = {}
    ptg_list: list[Mapping[str, Any]] = []

    for ent in entities:
        name = str(ent.get("name", ""))
        cat = cat_map.get(name, "")
        pos = ent.get("position", {})
        if not isinstance(pos, Mapping):
            continue
        x = round(float(pos.get("x", 0.0)), 1)
        y = round(float(pos.get("y", 0.0)), 1)
        if cat == "pipe":
            pipe_tiles.add((x, y))
        elif cat == "pipe-to-ground":
            direction = int(ent.get("direction", DIRECTION_NORTH))
            ptg_by_pos_dir[(x, y, direction)] = ent
            ptg_list.append(ent)

    segments: list[list[float]] = []
    rows: dict[float, set[float]] = collections.defaultdict(set)
    cols: dict[float, set[float]] = collections.defaultdict(set)
    for px, py in pipe_tiles:
        rows[py].add(px)
        cols[px].add(py)

    for py, xs in sorted(rows.items()):
        for run_start, run_end in _contiguous_runs(sorted(xs)):
            if run_end > run_start or (
                (run_start, round(py + 1.0, 1)) not in pipe_tiles
                and (run_start, round(py - 1.0, 1)) not in pipe_tiles
            ):
                segments.append([run_start, py, run_end, py, 0])
    for px, ys in sorted(cols.items()):
        for run_start, run_end in _contiguous_runs(sorted(ys)):
            if run_end > run_start:
                segments.append([px, run_start, px, run_end, 0])

    matched_ptg: set[int] = set()
    max_pipe_span = get_underground_span("pipe-to-ground", DEFAULT_PIPE_UNDERGROUND_SPAN)
    for ent in ptg_list:
        if id(ent) in matched_ptg:
            continue
        pos = ent["position"]
        px, py = round(float(pos["x"]), 1), round(float(pos["y"]), 1)
        pd = int(ent.get("direction", DIRECTION_NORTH))
        ug_d = OPPOSITE_DIR.get(pd, pd)
        dx, dy = DIR_STEPS.get(ug_d, (0, 0))
        for step in range(1, max_pipe_span + 1):
            cand = ptg_by_pos_dir.get((round(px + dx * step, 1), round(py + dy * step, 1), ug_d))
            if cand is not None and id(cand) not in matched_ptg:
                matched_ptg.add(id(ent))
                matched_ptg.add(id(cand))
                cpos = cand["position"]
                segments.append([px, py, round(float(cpos["x"]), 1), round(float(cpos["y"]), 1), 1])
                break
    return segments


def _contiguous_runs(sorted_vals: Sequence[float]) -> list[tuple[float, float]]:
    """Collapse sorted tile center coordinates into contiguous `(start, end)` intervals."""
    if not sorted_vals:
        return []
    runs: list[tuple[float, float]] = []
    start = sorted_vals[0]
    prev = sorted_vals[0]
    for val in sorted_vals[1:]:
        if round(val - prev, 1) <= 1.1:
            prev = val
        else:
            runs.append((start, prev))
            start = val
            prev = val
    runs.append((start, prev))
    return runs


def _extract_wire_segments(
    entities: Sequence[Mapping[str, Any]],
    wires: Sequence[Any],
) -> tuple[list[list[float]], list[list[object]]]:
    """Split blueprint wires into copper pole lines and red/green circuit lines."""
    pos_by_id: dict[int, tuple[float, float]] = {}
    for idx, ent in enumerate(entities, start=1):
        ent_id = int(ent.get("entity_number", idx))
        pos = ent.get("position", {})
        if isinstance(pos, Mapping):
            pos_by_id[ent_id] = (
                round(float(pos.get("x", 0.0)), 1),
                round(float(pos.get("y", 0.0)), 1),
            )

    copper: list[list[float]] = []
    circuit: list[list[object]] = []
    for wire in wires:
        if not isinstance(wire, Sequence) or len(wire) < WIRE_TUPLE_LEN:
            continue
        p1 = pos_by_id.get(int(wire[0]))
        p2 = pos_by_id.get(int(wire[2]))
        if p1 is None or p2 is None:
            continue
        conn_id = int(wire[1])
        if conn_id == WIRE_ID_POLE_COPPER:
            copper.append([p1[0], p1[1], p2[0], p2[1]])
        elif conn_id in (WIRE_ID_CIRCUIT_RED, WIRE_ID_OUT_RED):
            circuit.append([p1[0], p1[1], p2[0], p2[1], "red"])
        elif conn_id in (WIRE_ID_CIRCUIT_GREEN, WIRE_ID_OUT_GREEN):
            circuit.append([p1[0], p1[1], p2[0], p2[1], "green"])
    return copper, circuit


def select_street_straddle_window(
    bp_payload: Mapping[str, Any],
    cat_map: Mapping[str, str],
    win_w: int,
    win_h: int,
    *,
    prefer_pipes: bool = True,
) -> tuple[int, int, int, int]:
    """Select a window crossing a 100-tile street channel with diverse routed entities."""
    entities = [ent for ent in bp_payload.get("entities", ()) if isinstance(ent, Mapping)]
    if not entities:
        return (0, 0, win_w, win_h)

    xs = [
        float(ent["position"]["x"]) for ent in entities if isinstance(ent.get("position"), Mapping)
    ]
    ys = [
        float(ent["position"]["y"]) for ent in entities if isinstance(ent.get("position"), Mapping)
    ]
    max_x = max(xs)
    max_y = max(ys)

    channels_x = list(range(CHASSIS_PITCH_TILES, int(max_x), CHASSIS_PITCH_TILES))
    channels_y = list(range(CHASSIS_PITCH_TILES, int(max_y), CHASSIS_PITCH_TILES))

    candidates: list[tuple[int, int]] = [
        (cx - win_w // 2, step_y)
        for cx in channels_x
        for step_y in range(8, max(int(max_y) - win_h, 9), 18)
    ]
    candidates.extend(
        (step_x, cy - win_h // 2)
        for cy in channels_y
        for step_x in range(8, max(int(max_x) - win_w, 9), 18)
    )
    if not candidates:
        candidates.append((0, 0))

    best_score = -1.0
    best_win = (0, 0, win_w, win_h)
    for wx0, wy0 in candidates:
        wx1 = wx0 + win_w
        wy1 = wy0 + win_h
        recipes: set[str] = set()
        cats: set[str] = set()
        ug_count = 0
        pipe_count = 0
        split_count = 0
        ent_count = 0
        for ent in entities:
            pos = ent.get("position")
            if not isinstance(pos, Mapping):
                continue
            ex, ey = float(pos.get("x", 0.0)), float(pos.get("y", 0.0))
            if not (wx0 <= ex <= wx1 and wy0 <= ey <= wy1):
                continue
            ent_count += 1
            name = str(ent.get("name", ""))
            cat = cat_map.get(name, "")
            cats.add(cat)
            recipe = ent.get("recipe")
            if isinstance(recipe, str) and recipe:
                recipes.add(recipe)
            if cat == "underground-belt":
                ug_count += 1
            elif cat in ("pipe", "pipe-to-ground"):
                pipe_count += 1
            elif cat == "splitter":
                split_count += 1

        if ent_count < WINDOW_MIN_ENTITIES:
            continue
        pipe_bonus = min(pipe_count, 35) * (3.0 if prefer_pipes else 1.0)
        score = (
            len(recipes) * 18.0
            + len(cats) * 14.0
            + min(ug_count, 30) * 3.0
            + min(split_count, 20) * 2.5
            + pipe_bonus
            + min(ent_count, 500) * 0.25
        )
        if score > best_score:
            best_score = score
            best_win = (wx0, wy0, wx1, wy1)
    return best_win


def build_die_payload(
    def_data: Mapping[str, Any],
    netlist_data: Mapping[str, Any],
    bp_payload: Mapping[str, Any],
    cat_map: Mapping[str, str],
) -> dict[str, Any]:
    """Build compact interactive geometry JSON for the science chassis die viewer."""
    units = float(def_data.get("units", DEFAULT_DEF_UNITS))
    die_area = def_data.get("die_area", [0, 0, 0, 0])
    die_w = round(float(die_area[2]) / units)
    die_h = round(float(die_area[3]) / units)

    cells = netlist_data.get("cells", {})
    inst_map = {
        str(inst["name"]): inst
        for inst in netlist_data.get("instances", ())
        if isinstance(inst, Mapping) and "name" in inst
    }

    machines: list[str] = []
    machine_idx_map: dict[str, int] = {}
    recipes: list[str] = []
    recipe_idx_map: dict[str, int] = {}

    macros: list[list[object]] = []
    for comp in def_data.get("components", ()):
        name = str(comp["name"])
        inst = inst_map.get(name, {})
        cell_key = str(inst.get("cell", comp.get("macro", "")))
        cell = cells.get(cell_key, {}) if isinstance(cells, Mapping) else {}
        machine = str(cell.get("machine", comp.get("macro", "unknown")))
        recipe = str(inst.get("recipe", cell.get("recipe", "unknown")))

        m_idx = machine_idx_map.setdefault(machine, len(machines))
        if m_idx == len(machines):
            machines.append(machine)
        r_idx = recipe_idx_map.setdefault(recipe, len(recipes))
        if r_idx == len(recipes):
            recipes.append(recipe)

        x = round(float(comp["x"]) / units, 1)
        y = round(float(comp["y"]) / units, 1)
        w = int(cell.get("width", DEFAULT_CELL_DIM))
        h = int(cell.get("height", DEFAULT_CELL_DIM))
        plan_rate = round(float(inst.get("plan_rate_per_min", 0.0)), 2)
        rated_rate = round(float(inst.get("rated_per_min", 0.0)), 2)
        rank = int(inst.get("rank", 0))
        macros.append([x, y, w, h, m_idx, r_idx, plan_rate, rated_rate, rank, name])

    blockages = [
        [
            round(float(blk["x1"]) / units, 1),
            round(float(blk["y1"]) / units, 1),
            round(float(blk["x2"]) / units, 1),
            round(float(blk["y2"]) / units, 1),
        ]
        for blk in def_data.get("blockages", ())
        if isinstance(blk, Mapping)
    ]

    pins = [
        [
            str(pin.get("name", "")),
            str(pin.get("direction", "")),
            round(float(pin.get("x", 0.0)) / units, 1),
            round(float(pin.get("y", 0.0)) / units, 1),
        ]
        for pin in def_data.get("pins", ())
        if isinstance(pin, Mapping)
    ]

    layers = extract_geometry_layers(bp_payload, cat_map)
    return {
        "die": {
            "width": die_w,
            "height": die_h,
            "grid_w": max(1, die_w // CHASSIS_PITCH_TILES),
            "grid_h": max(1, die_h // CHASSIS_PITCH_TILES),
            "pitch": CHASSIS_PITCH_TILES,
        },
        "machines": machines,
        "recipes": recipes,
        "macros": macros,
        "blockages": blockages,
        "pins": pins,
        "belts": layers["belts"],
        "underground": layers["underground"],
        "pipes": layers["pipes"],
        "copper_wires": layers["copper_wires"],
    }


def collect_target_benchmarks(
    flow_bin_dir: Path,
    decode_bp_fn: Any,
    target_names: Sequence[str] = BENCHMARK_TARGETS,
) -> list[dict[str, Any]]:
    """Collect verified scaling, timing, and entity metrics across flow targets."""
    rows: list[dict[str, Any]] = []
    for target in target_names:
        def_path = flow_bin_dir / f"{target}.def"
        netlist_path = flow_bin_dir / f"{target}_netlist.json"
        timing_path = flow_bin_dir / f"{target}_timing.json"
        bp_path = flow_bin_dir / f"{target}.blueprint"
        drc_path = flow_bin_dir / f"{target}_route_drc.rpt"

        if not (
            def_path.is_file()
            and netlist_path.is_file()
            and timing_path.is_file()
            and bp_path.is_file()
        ):
            raise FileNotFoundError(f"Missing flow output for target '{target}' in {flow_bin_dir}")

        def_data = parse_def(def_path.read_text(encoding="utf-8"))
        netlist_data = json.loads(netlist_path.read_text(encoding="utf-8"))
        timing_data = json.loads(timing_path.read_text(encoding="utf-8"))
        bp_payload = decode_bp_fn(bp_path.read_text(encoding="utf-8").strip()).get("blueprint", {})

        units = float(def_data.get("units", DEFAULT_DEF_UNITS))
        die_area = def_data.get("die_area", [0, 0, 0, 0])
        die_w = round(float(die_area[2]) / units)
        die_h = round(float(die_area[3]) / units)

        drc_lines = (
            [
                line.strip()
                for line in drc_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if drc_path.is_file()
            else []
        )

        stages_raw = timing_data.get("stages", {})
        stages_sec = {
            key: round(float(stages_raw.get(key, 0.0)), 3)
            for key in ("synth", "pnr", "bp", "render", "save")
        }
        compile_sec = round(stages_sec["synth"] + stages_sec["pnr"] + stages_sec["bp"], 3)

        demand = netlist_data.get("demand", {})
        demand_packs = sorted(demand.keys()) if isinstance(demand, Mapping) else []
        rows.append(
            {
                "target": target,
                "sciences_count": len(demand_packs),
                "demand_packs": demand_packs,
                "die_w": die_w,
                "die_h": die_h,
                "die_tiles": die_w * die_h,
                "macro_instances": len(netlist_data.get("instances", ())),
                "pdk_cells": len(netlist_data.get("cells", {})),
                "bp_entities": len(bp_payload.get("entities", ())),
                "bp_wires": len(bp_payload.get("wires", ())),
                "drc_violations": len(drc_lines),
                "stages_sec": stages_sec,
                "compile_sec": compile_sec,
                "total_sec": round(float(timing_data.get("total_seconds", compile_sec)), 3),
            }
        )
    return rows


def naive_rank_hpwl(
    def_data: Mapping[str, Any],
    netlist_data: Mapping[str, Any],
) -> tuple[dict[str, tuple[float, float]], float, float]:
    """Compute naive topological rank-column placement and compare DEF net HPWL."""
    units = float(def_data.get("units", DEFAULT_DEF_UNITS))
    die_area = def_data.get("die_area", [0, 0, 0, 0])
    die_w = float(die_area[2]) / units
    die_h = float(die_area[3]) / units

    inst_meta = {
        str(inst["name"]): inst
        for inst in netlist_data.get("instances", ())
        if isinstance(inst, Mapping) and "name" in inst
    }
    ranks = sorted({int(inst.get("rank", 0)) for inst in inst_meta.values()})
    max_rank_idx = max(len(ranks) - 1, 1)

    by_rank: dict[int, list[Mapping[str, Any]]] = collections.defaultdict(list)
    for comp in def_data.get("components", ()):
        rank = int(inst_meta.get(str(comp["name"]), {}).get("rank", 0))
        by_rank[rank].append(comp)

    naive_xy: dict[str, tuple[float, float]] = {}
    for rank_idx, rank in enumerate(ranks):
        group = sorted(by_rank.get(rank, ()), key=lambda c: str(c["name"]))
        if not group:
            continue
        x = 12.0 + (rank_idx / max_rank_idx) * max(die_w - 28.0, 1.0)
        step_y = max(die_h - 24.0, 1.0) / len(group)
        for pos_idx, comp in enumerate(group):
            naive_xy[str(comp["name"])] = (x, 12.0 + (pos_idx + 0.5) * step_y)

    legalized_xy = {
        str(comp["name"]): (float(comp["x"]) / units, float(comp["y"]) / units)
        for comp in def_data.get("components", ())
    }
    pin_xy = {
        str(pin["name"]): (float(pin["x"]) / units, float(pin["y"]) / units)
        for pin in def_data.get("pins", ())
    }

    hpwl_naive = _compute_total_hpwl(def_data.get("nets", ()), naive_xy, pin_xy)
    hpwl_legal = _compute_total_hpwl(def_data.get("nets", ()), legalized_xy, pin_xy)
    return naive_xy, round(hpwl_naive, 1), round(hpwl_legal, 1)


def _compute_total_hpwl(
    nets: Sequence[Mapping[str, Any]],
    comp_xy: Mapping[str, tuple[float, float]],
    pin_xy: Mapping[str, tuple[float, float]],
) -> float:
    """Compute summed half-perimeter wirelength across all DEF nets."""
    total = 0.0
    for net in nets:
        pts: list[tuple[float, float]] = []
        for inst_name, pin_name in net.get("connections", ()):
            pt = pin_xy.get(pin_name) if inst_name == "PIN" else comp_xy.get(inst_name)
            if pt is not None:
                pts.append(pt)
        if len(pts) >= 2:
            xs = [pt[0] for pt in pts]
            ys = [pt[1] for pt in pts]
            total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def infer_machine_footprint(machine: str, prototypes: Mapping[str, Any]) -> tuple[float, float]:
    """Query machine entity footprint dynamically using loaded prototype data."""
    if not prototypes:
        return entity_size(machine, DIRECTION_NORTH)
    return entity_size(machine, DIRECTION_NORTH)
