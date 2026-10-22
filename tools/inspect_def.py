"""DEF inspection tool for routing and placement debugging.

This tool inspects nets, components, and tiles in routed DEF files.
It replaces ad-hoc file searches with structured path analysis.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from foundry.def_parser import parse_def

DEFAULT_SEARCH_RADIUS = 0.5
TILE_SEARCH_RADIUS = 3.0
WIRE_TOLERANCE = 0.01
DEFAULT_DEF_UNITS = 1000
EXIT_ERROR = 2


def _as_float(val: object, default: float = 0.0) -> float:
    """Convert an arbitrary object to float safely."""
    if isinstance(val, (int, float)):
        return float(val)
    return default


def find_components_at(
    components: Sequence[Mapping[str, object]],
    tx: float,
    ty: float,
    scale: float,
    radius: float = DEFAULT_SEARCH_RADIUS,
) -> list[dict[str, Any]]:
    """Find components near a target tile coordinate."""
    found: list[dict[str, Any]] = []
    for c in components:
        cx = _as_float(c.get("x")) * scale
        cy = _as_float(c.get("y")) * scale
        if abs(cx - tx) <= radius and abs(cy - ty) <= radius:
            found.append(dict(c))
    return found


def _wire_passes_through_tile(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    tx: float,
    ty: float,
) -> bool:
    """Return True if segment (x1, y1)-(x2, y2) passes through (tx, ty)."""
    is_h = (
        abs(y1 - ty) < WIRE_TOLERANCE
        and min(x1, x2) - WIRE_TOLERANCE <= tx <= max(x1, x2) + WIRE_TOLERANCE
    )
    is_v = (
        abs(x1 - tx) < WIRE_TOLERANCE
        and min(y1, y2) - WIRE_TOLERANCE <= ty <= max(y1, y2) + WIRE_TOLERANCE
    )
    return is_h or is_v


def find_wires_at(
    nets: Sequence[Mapping[str, object]],
    tx: float,
    ty: float,
    scale: float,
) -> list[dict[str, Any]]:
    """Find wire segments that pass through a given tile."""
    found: list[dict[str, Any]] = []
    for net in nets:
        nname = str(net.get("name", ""))
        wires = net.get("wires")
        wire_list = wires if isinstance(wires, list) else []
        for w in wire_list:
            if not isinstance(w, Mapping):
                continue
            x1, y1 = _as_float(w.get("x1")) * scale, _as_float(w.get("y1")) * scale
            x2, y2 = _as_float(w.get("x2")) * scale, _as_float(w.get("y2")) * scale
            if _wire_passes_through_tile(x1, y1, x2, y2, tx, ty):
                found.append(
                    {
                        "net": nname,
                        "layer": str(w.get("layer", "")),
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                    }
                )
    return found


def inspect_net(net: Mapping[str, object], scale: float) -> None:
    """Print detailed routing path and connections for a single net."""
    name = str(net.get("name", ""))
    print(f"Net: {name}")
    conns = net.get("connections")
    print("  Connections:")
    if isinstance(conns, list):
        for inst, pin in conns:
            print(f"    - {inst} . {pin}")

    wires = net.get("wires")
    wire_list = wires if isinstance(wires, list) else []
    print(f"  Wire segments ({len(wire_list)} total):")
    for idx, w in enumerate(wire_list):
        if not isinstance(w, Mapping):
            continue
        layer = str(w.get("layer", ""))
        x1 = _as_float(w.get("x1")) * scale
        y1 = _as_float(w.get("y1")) * scale
        x2 = _as_float(w.get("x2")) * scale
        y2 = _as_float(w.get("y2")) * scale
        length = abs(x2 - x1) + abs(y2 - y1)
        print(
            f"    [{idx + 1:2d}] {layer:4s} from ({x1:6.1f}, {y1:6.1f}) "
            f"to ({x2:6.1f}, {y2:6.1f}) [len={length:4.1f}]"
        )


def inspect_component(
    comp: Mapping[str, object],
    nets: Sequence[Mapping[str, object]],
    scale: float,
) -> None:
    """Print component details and its connected nets."""
    cname = str(comp.get("name", ""))
    macro = str(comp.get("macro", ""))
    cx = _as_float(comp.get("x")) * scale
    cy = _as_float(comp.get("y")) * scale
    orient = str(comp.get("orient", ""))
    print(f"Component: {cname}")
    print(f"  Macro:       {macro}")
    print(f"  Position:    ({cx:.1f}, {cy:.1f})")
    print(f"  Orientation: {orient}")

    connected_nets: list[tuple[str, str]] = []
    for net in nets:
        nname = str(net.get("name", ""))
        conns = net.get("connections")
        if isinstance(conns, list):
            for inst, pin in conns:
                if inst == cname:
                    connected_nets.append((str(pin), nname))

    print(f"  Connected nets ({len(connected_nets)} total):")
    for pin, nname in sorted(connected_nets):
        print(f"    - pin {pin:20s} -> net {nname}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Inspect nets, components, and tiles in a DEF file."
    )
    parser.add_argument("def_file", type=Path, help="Path to routed DEF file")
    parser.add_argument("--net", "-n", type=str, help="Net name to trace")
    parser.add_argument("--component", "-c", type=str, help="Component name to inspect")
    parser.add_argument("--tile", "-t", type=str, help="Tile coordinate as X,Y")
    parser.add_argument("--unrouted", action="store_true", help="List all unrouted nets in the DEF")
    parser.add_argument("--list-nets", action="store_true", help="List all net names in the DEF")
    parser.add_argument(
        "--list-components", action="store_true", help="List all components in the DEF"
    )
    return parser.parse_args(argv)


def _resolve_named_item(
    items: Sequence[Mapping[str, object]],
    query: str,
    kind: str,
) -> Mapping[str, object] | None:
    """Find an item by exact or unambiguous substring match."""
    exact = next((item for item in items if item.get("name") == query), None)
    if exact is not None:
        return exact
    matches = [item for item in items if query in str(item.get("name", ""))]
    if len(matches) == 1:
        return matches[0]
    if matches:
        print(f"Ambiguous {kind.lower()} '{query}', matches:", file=sys.stderr)
        for m in matches:
            print(f"  {m.get('name')}", file=sys.stderr)
        return None
    print(f"ERROR: {kind} '{query}' not found.", file=sys.stderr)
    return None


def _inspect_tile_region(
    tile_arg: str,
    components: Sequence[Mapping[str, object]],
    nets: Sequence[Mapping[str, object]],
    scale: float,
) -> int:
    """Print components and wires near a tile coordinate."""
    parts = tile_arg.split(",")
    if len(parts) != EXIT_ERROR:
        print("ERROR: Tile must be formatted as X,Y", file=sys.stderr)
        return EXIT_ERROR
    tx, ty = float(parts[0]), float(parts[1])
    print(f"Inspection at tile ({tx:.1f}, {ty:.1f}):")
    nearby_comps = find_components_at(components, tx, ty, scale, radius=TILE_SEARCH_RADIUS)
    if nearby_comps:
        print("  Nearby components:")
        for c in nearby_comps:
            cx = _as_float(c.get("x")) * scale
            cy = _as_float(c.get("y")) * scale
            print(f"    - {c.get('name')} ({c.get('macro')}) at ({cx:.1f}, {cy:.1f})")
    else:
        print("  Nearby components: None")

    wires_at_tile = find_wires_at(nets, tx, ty, scale)
    if wires_at_tile:
        print(f"  Wires passing through tile ({len(wires_at_tile)} total):")
        for w in wires_at_tile:
            print(
                f"    - net {w['net']:30s} {w['layer']:4s} "
                f"from ({w['x1']:.1f}, {w['y1']:.1f}) to ({w['x2']:.1f}, {w['y2']:.1f})"
            )
    else:
        print("  Wires passing through tile: None")
    return 0


def _print_list_summary(
    args: argparse.Namespace,
    def_name: str,
    nets: Sequence[Mapping[str, object]],
    components: Sequence[Mapping[str, object]],
    scale: float,
) -> bool:
    """Handle summary listing flags and return True if handled."""
    if args.unrouted:
        unrouted = [n for n in nets if not n.get("wires")]
        print(f"Unrouted nets in {def_name} ({len(unrouted)} total):")
        for net in unrouted:
            conns_raw = net.get("connections")
            conns_list = conns_raw if isinstance(conns_raw, list) else []
            conns = ", ".join(f"{inst}.{pin}" for inst, pin in conns_list)
            print(f"  {net.get('name', '')!s:35s} ({conns})")
        return True
    if args.list_nets:
        print(f"Nets in {def_name} ({len(nets)} total):")
        for net in nets:
            print(f"  {net.get('name')}")
        return True
    if args.list_components:
        print(f"Components in {def_name} ({len(components)} total):")
        for c in components:
            cx = _as_float(c.get("x")) * scale
            cy = _as_float(c.get("y")) * scale
            print(
                f"  {c.get('name', '')!s:35s} {c.get('macro', '')!s:35s} at ({cx:6.1f}, {cy:6.1f})"
            )
        return True
    return False


def _resolve_def_path(def_path: Path, env: Mapping[str, str]) -> Path:
    """Resolve DEF file path against workspace directory if relative."""
    workspace = env.get("BUILD_WORKSPACE_DIRECTORY", "")
    if workspace and not def_path.is_absolute() and not def_path.is_file():
        candidate = Path(workspace) / def_path
        if candidate.is_file():
            return candidate
    return def_path


def run_inspect(
    argv: list[str] | None = None,
    env: Mapping[str, str] = os.environ,
) -> int:
    """Execute DEF inspection."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    def_path = _resolve_def_path(args.def_file, env)

    if not def_path.is_file():
        print(f"ERROR: DEF file not found: {def_path}", file=sys.stderr)
        return EXIT_ERROR

    def_data = parse_def(def_path.read_text(encoding="utf-8"))
    scale = 1.0 / float(def_data.get("units", DEFAULT_DEF_UNITS))
    nets = def_data.get("nets", [])
    components = def_data.get("components", [])

    if _print_list_summary(args, def_path.name, nets, components, scale):
        return 0
    if args.net:
        target_net = _resolve_named_item(nets, args.net, "Net")
        if target_net is not None:
            inspect_net(target_net, scale)
            return 0
        return 1
    if args.component:
        target_comp = _resolve_named_item(components, args.component, "Component")
        if target_comp is not None:
            inspect_component(target_comp, nets, scale)
            return 0
        return 1
    if args.tile:
        return _inspect_tile_region(args.tile, components, nets, scale)

    print("Please specify --net, --component, --tile, --list-nets, or --list-components.")
    return 1


def main() -> int:
    """CLI entry point."""
    return run_inspect()


if __name__ == "__main__":
    sys.exit(main())
