"""Blueprint inspection and comparison tool.

This tool analyzes Factorio blueprint files or exchange strings.
It measures dimensions, entity counts, density, and machine composition.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path
from typing import Any

from foundry.blueprint import decode


def analyze_blueprint(bp_data: dict[str, Any]) -> dict[str, Any]:
    """Analyze blueprint entities and compute factory metrics."""
    bp = bp_data.get("blueprint", {})
    label = bp.get("label", "unnamed")
    entities = bp.get("entities", [])

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

    xs = [e["position"]["x"] for e in entities if "position" in e]
    ys = [e["position"]["y"] for e in entities if "position" in e]

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
        name = e.get("name", "unknown")
        counts[name] += 1
        recipe = e.get("recipe")
        if recipe:
            recipes[recipe] += 1

        if "assembling-machine" in name or "furnace" in name or "chemical-plant" in name:
            machine_count += 1
            machine_area += 9.0
        elif "oil-refinery" in name:
            machine_count += 1
            machine_area += 25.0

    density = (machine_area / area * 100.0) if area > 0 else 0.0

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


def read_blueprint(path_or_str: str) -> dict[str, Any]:
    """Read blueprint data from file or raw string."""
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    path = Path(path_or_str)
    if workspace and not path.is_file() and not path.is_absolute():
        cand = Path(workspace) / path_or_str
        if cand.is_file():
            path = cand

    content = path.read_text(encoding="utf-8").strip() if path.is_file() else path_or_str.strip()
    return decode(content)


def print_single_report(metrics: dict[str, Any], path: str) -> None:
    """Print detailed metric report for one blueprint."""
    print("=" * 80)
    print(f"BLUEPRINT ANALYSIS: {path}")
    print(f"Label: {metrics['label']}")
    print("=" * 80)
    print(
        f"Dimensions        : {metrics['width']:.1f} x {metrics['height']:.1f} tiles "
        f"(area = {metrics['area']:.1f} tiles)"
    )
    if "min_x" in metrics:
        print(
            f"Bounds            : ({metrics['min_x']:.1f}, {metrics['min_y']:.1f}) to ({metrics['max_x']:.1f}, {metrics['max_y']:.1f})"
        )
    print(f"Total entities    : {metrics['entity_count']}")
    print(
        f"Machine entities  : {metrics['machine_count']} machines "
        f"({metrics['machine_area']:.1f} tiles machine footprint)"
    )
    print(f"Packing density   : {metrics['density']:.2f}% (machine footprint / bounding area)")
    print("-" * 80)
    print("Entity breakdown:")
    for name, cnt in sorted(metrics["counts"].items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {cnt:5d} x {name}")
    if metrics["recipes"]:
        print("-" * 80)
        print("Recipe breakdown:")
        for rec, cnt in sorted(metrics["recipes"].items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {cnt:5d} x {rec}")
    print("=" * 80)


def print_comparison_table(results: list[tuple[str, dict[str, Any]]]) -> None:
    """Print side-by-side comparison table for multiple blueprints."""
    print("=" * 90)
    print("BLUEPRINT COMPARISON")
    print("=" * 90)
    header = f"{'Metric':<25s}"
    for path, _ in results:
        short_name = Path(path).name[:18]
        header += f" | {short_name:<18s}"
    print(header)
    print("-" * 90)

    rows = [
        ("Label", lambda m: str(m["label"])[:18]),
        ("Total Entities", lambda m: str(m["entity_count"])),
        ("Dimensions (W x H)", lambda m: f"{m['width']:.0f} x {m['height']:.0f}"),
        ("Total Area (tiles)", lambda m: f"{m['area']:,.0f}"),
        ("Machine Count", lambda m: str(m["machine_count"])),
        ("Machine Area", lambda m: f"{m['machine_area']:.0f}"),
        ("Packing Density", lambda m: f"{m['density']:.2f}%"),
        ("Belts (surface)", lambda m: str(m["counts"].get("transport-belt", 0))),
        ("Underground Belts", lambda m: str(m["counts"].get("underground-belt", 0))),
        ("Splitters", lambda m: str(m["counts"].get("splitter", 0))),
        ("Inserters", lambda m: str(sum(v for k, v in m["counts"].items() if "inserter" in k))),
        ("Poles", lambda m: str(sum(v for k, v in m["counts"].items() if "pole" in k))),
    ]

    for label, fn in rows:
        row_str = f"{label:<25s}"
        for _, m in results:
            val = fn(m)
            row_str += f" | {val:<18s}"
        print(row_str)
    print("=" * 90)


def print_details(bp_data: dict[str, Any]) -> None:
    """Print machine positions and belt bus tracks."""
    bp = bp_data.get("blueprint", {})
    entities = bp.get("entities", [])
    machines = [
        e
        for e in entities
        if "assembling-machine" in e.get("name", "") or "furnace" in e.get("name", "")
    ]
    machines.sort(key=lambda m: (m["position"]["y"], m["position"]["x"]))

    print("-" * 80)
    print(f"MACHINES ({len(machines)} total):")
    for m in machines:
        p = m["position"]
        rec = m.get("recipe", "no-recipe")
        print(f"  at ({p['x']:5.1f}, {p['y']:5.1f}): {m['name']} recipe={rec}")

    inserters = [e for e in entities if "inserter" in e.get("name", "")]
    inserters.sort(key=lambda ins: (ins["position"]["y"], ins["position"]["x"]))
    print("-" * 80)
    print(f"INSERTERS ({len(inserters)} total):")
    for ins in inserters[:20]:
        p = ins["position"]
        d = ins.get("direction", 0)
        print(f"  at ({p['x']:5.1f}, {p['y']:5.1f}): {ins['name']} dir={d}")
    if len(inserters) > 20:
        print(f"  ... and {len(inserters) - 20} more inserters")

    belts = [e for e in entities if "belt" in e.get("name", "")]
    by_y = collections.defaultdict(list)
    for b in belts:
        by_y[round(b["position"]["y"], 1)].append(b)

    print("-" * 80)
    print("BELT ROWS (horizontal channels):")
    for y_coord in sorted(by_y.keys()):
        row_belts = by_y[y_coord]
        xs = [b["position"]["x"] for b in row_belts]
        dirs = [b.get("direction", 0) for b in row_belts]
        dir_summary = collections.Counter(dirs)
        dir_str = ", ".join(f"dir={d}:{cnt}" for d, cnt in sorted(dir_summary.items()))
        print(
            f"  y={y_coord:5.1f}: {len(row_belts):3d} belts from x={min(xs):5.1f} to {max(xs):5.1f} [{dir_str}]"
        )


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
        help="Find entities near X,Y (e.g. '186.5,35.5')",
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=3.0,
        help="Radius for --find-tile search (default 3.0)",
    )
    args = parser.parse_args(argv)

    results = []
    raw_data = []
    for bp_path in args.blueprints:
        try:
            data = read_blueprint(bp_path)
            metrics = analyze_blueprint(data)
            results.append((bp_path, metrics))
            raw_data.append(data)
        except Exception as e:
            print(f"ERROR reading '{bp_path}': {e}", file=sys.stderr)
            return 1

    if len(results) == 1:
        print_single_report(results[0][1], results[0][0])
        if args.details:
            print_details(raw_data[0])
    else:
        print_comparison_table(results)
        if args.details:
            for idx, (path, _) in enumerate(results):
                print(f"\nDETAILS FOR: {path}")
                print_details(raw_data[idx])

    if args.find_tile:
        tx, ty = (float(v) for v in args.find_tile.split(","))
        for bp_path, raw in zip(args.blueprints, raw_data, strict=True):
            print(f"\nENTITIES NEAR ({tx:.1f}, {ty:.1f}) in {bp_path} (radius={args.radius}):")
            ents = raw.get("blueprint", {}).get("entities", [])
            wires = raw.get("blueprint", {}).get("wires", [])
            ent_by_num = {int(e["entity_number"]): e for e in ents if "entity_number" in e}
            matches = [
                e
                for e in ents
                if abs(e.get("position", {}).get("x", 1e9) - tx) <= args.radius
                and abs(e.get("position", {}).get("y", 1e9) - ty) <= args.radius
            ]
            matches.sort(key=lambda e: (e["position"]["x"], e["position"]["y"]))
            for e in matches:
                p = e["position"]
                enum = e.get("entity_number")
                w_list = []
                if enum is not None:
                    for w in wires:
                        if len(w) >= 4 and (w[0] == enum or w[2] == enum):
                            other_id = w[2] if w[0] == enum else w[0]
                            conn_type = w[1] if w[0] == enum else w[3]
                            other_e = ent_by_num.get(int(other_id))
                            other_str = (
                                f"{other_e.get('name')}@({other_e['position']['x']:.1f},{other_e['position']['y']:.1f})"
                                if other_e and "position" in other_e
                                else f"#{other_id}"
                            )
                            w_list.append(f"c{conn_type}->{other_str}(#{other_id})")
                w_str = f" wires=[{', '.join(w_list)}]" if w_list else ""
                print(
                    f"  #{enum} {e.get('name')} at ({p['x']:.1f}, {p['y']:.1f}) dir={e.get('direction', 0)} type={e.get('type')}{w_str}"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
