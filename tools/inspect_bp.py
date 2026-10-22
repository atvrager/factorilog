"""Blueprint inspection and comparison tool.

This tool analyzes Factorio blueprint files or exchange strings.
It measures dimensions, entity counts, density, and machine composition.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from foundry.blueprint import decode

STANDARD_MACHINE_AREA = 9.0
REFINERY_MACHINE_AREA = 25.0
PERCENT_SCALE = 100.0
REPORT_RULE_WIDTH = 80
TABLE_RULE_WIDTH = 90
COL_NAME_MAX_LEN = 18
MAX_PREVIEW_INSERTERS = 20
DEFAULT_TILE_SEARCH_RADIUS = 3.0
MISSING_COORD_SENTINEL = 1e9
WIRE_TUPLE_MIN_LEN = 4
WIRE_SECOND_CONN_INDEX = 3


def _as_mapping(val: object) -> Mapping[str, object]:
    """Return val if it is a Mapping, or an empty dictionary."""
    if isinstance(val, Mapping):
        return val
    return {}


def _as_float(val: object, default: float = 0.0) -> float:
    """Return val converted to float, or default."""
    if isinstance(val, (int, float)):
        return float(val)
    return default


def _pos_xy(entity: Mapping[str, object], default: float = 0.0) -> tuple[float, float]:
    """Extract (x, y) coordinates from an entity mapping."""
    pos = _as_mapping(entity.get("position"))
    return _as_float(pos.get("x"), default), _as_float(pos.get("y"), default)


def _entity_machine_area(name: str) -> float:
    """Return machine footprint area in tiles, or 0.0 if not a machine."""
    if "assembling-machine" in name or "furnace" in name or "chemical-plant" in name:
        return STANDARD_MACHINE_AREA
    if "oil-refinery" in name:
        return REFINERY_MACHINE_AREA
    return 0.0


def analyze_blueprint(bp_data: Mapping[str, object]) -> dict[str, Any]:
    """Analyze blueprint entities and compute factory metrics."""
    bp = _as_mapping(bp_data.get("blueprint"))
    label = str(bp.get("label", "unnamed"))
    raw_entities = bp.get("entities")
    entities = (
        [e for e in raw_entities if isinstance(e, Mapping)]
        if isinstance(raw_entities, list)
        else []
    )

    if not entities:
        return {
            "label": label,
            "entity_count": 0,
            "width": 0.0,
            "height": 0.0,
            "area": 0.0,
            "machine_count": 0,
            "machine_area": 0.0,
            "density": 0.0,
            "counts": {},
            "recipes": {},
        }

    xs = [_pos_xy(e)[0] for e in entities if "position" in e]
    ys = [_pos_xy(e)[1] for e in entities if "position" in e]

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    width = round(max_x - min_x + 1.0, 1)
    height = round(max_y - min_y + 1.0, 1)
    area = round(width * height, 1)

    counts: dict[str, int] = collections.defaultdict(int)
    recipes: dict[str, int] = collections.defaultdict(int)
    machine_count = 0
    machine_area = 0.0

    for e in entities:
        name = str(e.get("name", "unknown"))
        counts[name] += 1
        recipe = e.get("recipe")
        if isinstance(recipe, str) and recipe:
            recipes[recipe] += 1
        footprint = _entity_machine_area(name)
        if footprint > 0.0:
            machine_count += 1
            machine_area += footprint

    density = (machine_area / area * PERCENT_SCALE) if area > 0 else 0.0

    return {
        "label": label,
        "entity_count": len(entities),
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
        "width": width,
        "height": height,
        "area": area,
        "machine_count": machine_count,
        "machine_area": machine_area,
        "density": density,
        "counts": dict(counts),
        "recipes": dict(recipes),
    }


def read_blueprint(
    path_or_str: str,
    env: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Read blueprint data from file or raw string."""
    workspace = env.get("BUILD_WORKSPACE_DIRECTORY", "")
    path = Path(path_or_str)
    if workspace and not path.is_file() and not path.is_absolute():
        cand = Path(workspace) / path_or_str
        if cand.is_file():
            path = cand

    content = path.read_text(encoding="utf-8").strip() if path.is_file() else path_or_str.strip()
    return decode(content)


def print_single_report(metrics: Mapping[str, object], path: str) -> None:
    """Print detailed metric report for one blueprint."""
    counts = _as_mapping(metrics.get("counts"))
    recipes = _as_mapping(metrics.get("recipes"))
    width = _as_float(metrics.get("width"))
    height = _as_float(metrics.get("height"))
    area = _as_float(metrics.get("area"))
    machine_area = _as_float(metrics.get("machine_area"))
    density = _as_float(metrics.get("density"))
    print("=" * REPORT_RULE_WIDTH)
    print(f"BLUEPRINT ANALYSIS: {path}")
    print(f"Label: {metrics['label']}")
    print("=" * REPORT_RULE_WIDTH)
    print(f"Dimensions        : {width:.1f} x {height:.1f} tiles (area = {area:.1f} tiles)")
    if "min_x" in metrics:
        min_x = _as_float(metrics.get("min_x"))
        min_y = _as_float(metrics.get("min_y"))
        max_x = _as_float(metrics.get("max_x"))
        max_y = _as_float(metrics.get("max_y"))
        print(f"Bounds            : ({min_x:.1f}, {min_y:.1f}) to ({max_x:.1f}, {max_y:.1f})")
    print(f"Total entities    : {metrics['entity_count']}")
    print(
        f"Machine entities  : {metrics['machine_count']} machines "
        f"({machine_area:.1f} tiles machine footprint)"
    )
    print(f"Packing density   : {density:.2f}% (machine footprint / bounding area)")
    print("-" * REPORT_RULE_WIDTH)
    print("Entity breakdown:")
    count_items = [(str(k), int(_as_float(v))) for k, v in counts.items()]
    for name, cnt in sorted(count_items, key=lambda kv: (-kv[1], kv[0])):
        print(f"  {cnt:5d} x {name}")
    if recipes:
        print("-" * REPORT_RULE_WIDTH)
        print("Recipe breakdown:")
        recipe_items = [(str(k), int(_as_float(v))) for k, v in recipes.items()]
        for rec, cnt in sorted(recipe_items, key=lambda kv: (-kv[1], kv[0])):
            print(f"  {cnt:5d} x {rec}")
    print("=" * REPORT_RULE_WIDTH)


def _count_matching(counts: Mapping[str, object], substring: str) -> int:
    """Sum counts of all keys that contain substring."""
    return sum(int(_as_float(v)) for k, v in counts.items() if substring in k)


def print_comparison_table(results: Sequence[tuple[str, Mapping[str, object]]]) -> None:
    """Print side-by-side comparison table for multiple blueprints."""
    print("=" * TABLE_RULE_WIDTH)
    print("BLUEPRINT COMPARISON")
    print("=" * TABLE_RULE_WIDTH)
    header_cols = [f" | {Path(path).name[:COL_NAME_MAX_LEN]:<18s}" for path, _ in results]
    print(f"{'Metric':<25s}{''.join(header_cols)}")
    print("-" * TABLE_RULE_WIDTH)

    rows = [
        ("Label", lambda m: str(m["label"])[:COL_NAME_MAX_LEN]),
        ("Total Entities", lambda m: str(m["entity_count"])),
        (
            "Dimensions (W x H)",
            lambda m: f"{_as_float(m['width']):.0f} x {_as_float(m['height']):.0f}",
        ),
        ("Total Area (tiles)", lambda m: f"{_as_float(m['area']):,.0f}"),
        ("Machine Count", lambda m: str(m["machine_count"])),
        ("Machine Area", lambda m: f"{_as_float(m['machine_area']):.0f}"),
        ("Packing Density", lambda m: f"{_as_float(m['density']):.2f}%"),
        (
            "Belts (surface)",
            lambda m: str(_as_mapping(m["counts"]).get("transport-belt", 0)),
        ),
        (
            "Underground Belts",
            lambda m: str(_as_mapping(m["counts"]).get("underground-belt", 0)),
        ),
        ("Splitters", lambda m: str(_as_mapping(m["counts"]).get("splitter", 0))),
        ("Inserters", lambda m: str(_count_matching(_as_mapping(m["counts"]), "inserter"))),
        ("Poles", lambda m: str(_count_matching(_as_mapping(m["counts"]), "pole"))),
    ]

    for label, fn in rows:
        val_cols = [f" | {fn(m):<18s}" for _, m in results]
        print(f"{label:<25s}{''.join(val_cols)}")
    print("=" * TABLE_RULE_WIDTH)


def print_details(bp_data: Mapping[str, object]) -> None:
    """Print machine positions and belt bus tracks."""
    bp = _as_mapping(bp_data.get("blueprint"))
    raw_entities = bp.get("entities")
    entities = (
        [e for e in raw_entities if isinstance(e, Mapping)]
        if isinstance(raw_entities, list)
        else []
    )
    machines = [
        e
        for e in entities
        if "assembling-machine" in str(e.get("name", "")) or "furnace" in str(e.get("name", ""))
    ]
    machines.sort(key=lambda m: (_pos_xy(m)[1], _pos_xy(m)[0]))

    print("-" * REPORT_RULE_WIDTH)
    print(f"MACHINES ({len(machines)} total):")
    for m in machines:
        px, py = _pos_xy(m)
        rec = m.get("recipe", "no-recipe")
        print(f"  at ({px:5.1f}, {py:5.1f}): {m['name']} recipe={rec}")

    inserters = [e for e in entities if "inserter" in str(e.get("name", ""))]
    inserters.sort(key=lambda ins: (_pos_xy(ins)[1], _pos_xy(ins)[0]))
    print("-" * REPORT_RULE_WIDTH)
    print(f"INSERTERS ({len(inserters)} total):")
    for ins in inserters[:MAX_PREVIEW_INSERTERS]:
        px, py = _pos_xy(ins)
        d = ins.get("direction", 0)
        print(f"  at ({px:5.1f}, {py:5.1f}): {ins['name']} dir={d}")
    if len(inserters) > MAX_PREVIEW_INSERTERS:
        print(f"  ... and {len(inserters) - MAX_PREVIEW_INSERTERS} more inserters")

    belts = [e for e in entities if "belt" in str(e.get("name", ""))]
    by_y: dict[float, list[Mapping[str, object]]] = collections.defaultdict(list)
    for b in belts:
        by_y[round(_pos_xy(b)[1], 1)].append(b)

    print("-" * REPORT_RULE_WIDTH)
    print("BELT ROWS (horizontal channels):")
    for y_coord in sorted(by_y.keys()):
        row_belts = by_y[y_coord]
        xs = [_pos_xy(b)[0] for b in row_belts]
        dirs = [int(_as_float(b.get("direction", 0))) for b in row_belts]
        dir_summary = collections.Counter(dirs)
        dir_str = ", ".join(f"dir={d}:{cnt}" for d, cnt in sorted(dir_summary.items()))
        print(
            f"  y={y_coord:5.1f}: {len(row_belts):3d} belts "
            f"from x={min(xs):5.1f} to {max(xs):5.1f} [{dir_str}]"
        )


def _format_other_entity(
    other_id: int,
    ent_by_num: Mapping[int, Mapping[str, object]],
) -> str:
    """Format a wire endpoint entity label."""
    other_e = ent_by_num.get(other_id)
    if other_e and "position" in other_e:
        ox, oy = _pos_xy(other_e)
        return f"{other_e.get('name')}@({ox:.1f},{oy:.1f})"
    return f"#{other_id}"


def _build_wire_map(
    wires: Sequence[object],
    ent_by_num: Mapping[int, Mapping[str, object]],
) -> dict[int, list[str]]:
    """Index formatted wire descriptions by entity number."""
    by_entity: dict[int, list[str]] = collections.defaultdict(list)
    for w in wires:
        if not isinstance(w, list) or len(w) < WIRE_TUPLE_MIN_LEN:
            continue
        id0 = int(_as_float(w[0]))
        id1 = int(_as_float(w[2]))
        str1 = _format_other_entity(id1, ent_by_num)
        str0 = _format_other_entity(id0, ent_by_num)
        by_entity[id0].append(f"c{w[1]}->{str1}(#{id1})")
        by_entity[id1].append(f"c{w[WIRE_SECOND_CONN_INDEX]}->{str0}(#{id0})")
    return by_entity


def _print_nearby_entities(
    bp_path: str,
    raw: Mapping[str, object],
    tx: float,
    ty: float,
    radius: float,
) -> None:
    """Print entities and wire connections near (tx, ty) in one blueprint."""
    print(f"\nENTITIES NEAR ({tx:.1f}, {ty:.1f}) in {bp_path} (radius={radius}):")
    bp = _as_mapping(raw.get("blueprint"))
    ents_raw = bp.get("entities")
    ents = [e for e in ents_raw if isinstance(e, Mapping)] if isinstance(ents_raw, list) else []
    wires_raw = bp.get("wires")
    wires = wires_raw if isinstance(wires_raw, list) else []
    ent_by_num = {int(_as_float(e["entity_number"])): e for e in ents if "entity_number" in e}
    wire_map = _build_wire_map(wires, ent_by_num)
    matches = [
        e
        for e in ents
        if abs(_pos_xy(e, MISSING_COORD_SENTINEL)[0] - tx) <= radius
        and abs(_pos_xy(e, MISSING_COORD_SENTINEL)[1] - ty) <= radius
    ]
    matches.sort(key=lambda e: (_pos_xy(e)[0], _pos_xy(e)[1]))
    for e in matches:
        px, py = _pos_xy(e)
        enum = e.get("entity_number")
        w_list = wire_map.get(enum, []) if isinstance(enum, int) else []
        w_str = f" wires=[{', '.join(w_list)}]" if w_list else ""
        print(
            f"  #{enum} {e.get('name')} at ({px:.1f}, {py:.1f}) "
            f"dir={e.get('direction', 0)} type={e.get('type')}{w_str}"
        )


def _display_reports(
    results: Sequence[tuple[str, Mapping[str, object]]],
    raw_data: Sequence[Mapping[str, object]],
    show_details: bool,
) -> None:
    """Display either a single blueprint report or a comparison table."""
    if len(results) == 1:
        print_single_report(results[0][1], results[0][0])
        if show_details:
            print_details(raw_data[0])
        return

    print_comparison_table(results)
    if show_details:
        for idx, (path, _) in enumerate(results):
            print(f"\nDETAILS FOR: {path}")
            print_details(raw_data[idx])


def main(argv: list[str] | None = None) -> int:
    """Run blueprint inspector."""
    parser = argparse.ArgumentParser(description="Inspect and compare Factorio blueprints.")
    parser.add_argument("blueprints", nargs="+", help="Blueprint file paths or raw strings")
    parser.add_argument(
        "--details", action="store_true", help="Print machine positions and belt channels"
    )
    parser.add_argument(
        "--find-tile",
        type=str,
        default="",
        help="Find entities near X,Y (for example '186.5,35.5')",
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=DEFAULT_TILE_SEARCH_RADIUS,
        help="Radius for --find-tile search (default 3.0)",
    )
    args = parser.parse_args(argv)

    results: list[tuple[str, Mapping[str, object]]] = []
    raw_data: list[Mapping[str, object]] = []
    for bp_path in args.blueprints:
        try:
            data = read_blueprint(bp_path)
            metrics = analyze_blueprint(data)
            results.append((bp_path, metrics))
            raw_data.append(data)
        except Exception as e:
            print(f"ERROR reading '{bp_path}': {e}", file=sys.stderr)
            return 1

    _display_reports(results, raw_data, args.details)

    if args.find_tile:
        tx, ty = (float(v) for v in args.find_tile.split(","))
        for bp_path, raw in zip(args.blueprints, raw_data, strict=True):
            _print_nearby_entities(bp_path, raw, tx, ty, args.radius)

    return 0


if __name__ == "__main__":
    sys.exit(main())
