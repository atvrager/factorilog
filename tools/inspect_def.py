"""DEF inspection tool for routing and placement debugging.

This tool inspects nets, components, and tiles in routed DEF files.
It replaces ad-hoc file searches with structured path analysis.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from foundry.def_parser import parse_def


def find_components_at(
    components: list[dict[str, Any]],
    tx: float,
    ty: float,
    scale: float,
    radius: float = 0.5,
) -> list[dict[str, Any]]:
    """Find components near a target tile coordinate."""
    found: list[dict[str, Any]] = []
    for c in components:
        cx = c["x"] * scale
        cy = c["y"] * scale
        if abs(cx - tx) <= radius and abs(cy - ty) <= radius:
            found.append(c)
    return found


def find_wires_at(
    nets: list[dict[str, Any]],
    tx: float,
    ty: float,
    scale: float,
) -> list[dict[str, Any]]:
    """Find wire segments that pass through a given tile."""
    found: list[dict[str, Any]] = []
    for net in nets:
        nname = net.get("name", "")
        for w in net.get("wires", []):
            x1 = w["x1"] * scale
            y1 = w["y1"] * scale
            x2 = w["x2"] * scale
            y2 = w["y2"] * scale
            layer = w.get("layer", "")
            is_h = abs(y1 - ty) < 0.01 and min(x1, x2) - 0.01 <= tx <= max(x1, x2) + 0.01
            is_v = abs(x1 - tx) < 0.01 and min(y1, y2) - 0.01 <= ty <= max(y1, y2) + 0.01
            if is_h or is_v:
                found.append(
                    {
                        "net": nname,
                        "layer": layer,
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                    }
                )
    return found


def inspect_net(net: dict[str, Any], scale: float) -> None:
    """Print detailed routing path and connections for a single net."""
    name = net.get("name", "")
    print(f"Net: {name}")
    conns = net.get("connections", [])
    print("  Connections:")
    for inst, pin in conns:
        print(f"    - {inst} . {pin}")

    wires = net.get("wires", [])
    print(f"  Wire segments ({len(wires)} total):")
    for idx, w in enumerate(wires):
        layer = w.get("layer", "")
        x1 = w["x1"] * scale
        y1 = w["y1"] * scale
        x2 = w["x2"] * scale
        y2 = w["y2"] * scale
        dx = x2 - x1
        dy = y2 - y1
        length = abs(dx) + abs(dy)
        print(
            f"    [{idx + 1:2d}] {layer:4s} from ({x1:6.1f}, {y1:6.1f}) "
            f"to ({x2:6.1f}, {y2:6.1f}) [len={length:4.1f}]"
        )


def inspect_component(
    comp: dict[str, Any],
    nets: list[dict[str, Any]],
    scale: float,
) -> None:
    """Print component details and its connected nets."""
    cname = comp.get("name", "")
    macro = comp.get("macro", "")
    cx = comp.get("x", 0) * scale
    cy = comp.get("y", 0) * scale
    orient = comp.get("orient", "")
    print(f"Component: {cname}")
    print(f"  Macro:       {macro}")
    print(f"  Position:    ({cx:.1f}, {cy:.1f})")
    print(f"  Orientation: {orient}")

    # Connected nets
    connected_nets: list[tuple[str, str]] = []
    for net in nets:
        nname = net.get("name", "")
        for inst, pin in net.get("connections", []):
            if inst == cname:
                connected_nets.append((pin, nname))

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
    parser.add_argument("--list-nets", action="store_true", help="List all net names in the DEF")
    parser.add_argument(
        "--list-components", action="store_true", help="List all components in the DEF"
    )
    return parser.parse_args(argv)


def run_inspect(argv: list[str] | None = None) -> int:
    """Execute DEF inspection."""
    import os

    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    def_path = args.def_file
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace and not def_path.is_absolute() and not def_path.is_file():
        candidate = Path(workspace) / def_path
        if candidate.is_file():
            def_path = candidate

    if not def_path.is_file():
        print(f"ERROR: DEF file not found: {def_path}", file=sys.stderr)
        return 2

    def_data = parse_def(def_path.read_text(encoding="utf-8"))
    scale = 1.0 / float(def_data.get("units", 1000))
    nets = def_data.get("nets", [])
    components = def_data.get("components", [])

    if args.list_nets:
        print(f"Nets in {def_path.name} ({len(nets)} total):")
        for net in nets:
            print(f"  {net.get('name')}")
        return 0

    if args.list_components:
        print(f"Components in {def_path.name} ({len(components)} total):")
        for c in components:
            cx = c.get("x", 0) * scale
            cy = c.get("y", 0) * scale
            print(f"  {c.get('name'):35s} {c.get('macro'):35s} at ({cx:6.1f}, {cy:6.1f})")
        return 0

    if args.net:
        target_net = next((n for n in nets if n.get("name") == args.net), None)
        if not target_net:
            # Fuzzy match
            matches = [n for n in nets if args.net in n.get("name", "")]
            if len(matches) == 1:
                target_net = matches[0]
            elif matches:
                print(f"Ambiguous net '{args.net}', matches:", file=sys.stderr)
                for m in matches:
                    print(f"  {m.get('name')}", file=sys.stderr)
                return 1
            else:
                print(f"ERROR: Net '{args.net}' not found.", file=sys.stderr)
                return 1
        inspect_net(target_net, scale)
        return 0

    if args.component:
        target_comp = next((c for c in components if c.get("name") == args.component), None)
        if not target_comp:
            matches = [c for c in components if args.component in c.get("name", "")]
            if len(matches) == 1:
                target_comp = matches[0]
            elif matches:
                print(f"Ambiguous component '{args.component}', matches:", file=sys.stderr)
                for m in matches:
                    print(f"  {m.get('name')}", file=sys.stderr)
                return 1
            else:
                print(f"ERROR: Component '{args.component}' not found.", file=sys.stderr)
                return 1
        inspect_component(target_comp, nets, scale)
        return 0

    if args.tile:
        parts = args.tile.split(",")
        if len(parts) != 2:
            print("ERROR: Tile must be formatted as X,Y", file=sys.stderr)
            return 2
        tx, ty = float(parts[0]), float(parts[1])
        print(f"Inspection at tile ({tx:.1f}, {ty:.1f}):")
        nearby_comps = find_components_at(components, tx, ty, scale, radius=3.0)
        if nearby_comps:
            print("  Nearby components:")
            for c in nearby_comps:
                cx = c["x"] * scale
                cy = c["y"] * scale
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

    print("Please specify --net, --component, --tile, --list-nets, or --list-components.")
    return 1


def main() -> int:
    """CLI entry point."""
    return run_inspect()


if __name__ == "__main__":
    sys.exit(main())
