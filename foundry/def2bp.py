"""DEF to Factorio blueprint converter.

Reads placed and routed OpenROAD DEF files and netlist JSON metadata,
then generates Factorio 2.0 blueprint exchange strings.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import blueprint

# Factorio 2.0 direction values: North=0, East=4, South=8, West=12.
DIRECTION_NORTH = 0
DIRECTION_EAST = 4
DIRECTION_SOUTH = 8
DIRECTION_WEST = 12


def parse_def(text: str) -> dict[str, Any]:
    """Parse DEF text into components, nets, and design units."""
    units = 1000
    m_units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text)
    if m_units:
        units = int(m_units.group(1))

    comp_pat = re.compile(
        r"^\s*-\s+(\S+)\s+(\S+)\s+\+\s+(?:PLACED|FIXED)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)",
        re.MULTILINE,
    )
    components: list[dict[str, Any]] = [
        {
            "name": m.group(1),
            "macro": m.group(2),
            "x": int(m.group(3)),
            "y": int(m.group(4)),
            "orient": m.group(5),
        }
        for m in comp_pat.finditer(text)
    ]

    # Parse NETS section
    nets: list[dict[str, Any]] = []
    nets_match = re.search(r"NETS\s+\d+\s*;(.*?)END\s+NETS", text, re.DOTALL)
    if nets_match:
        nets_text = nets_match.group(1)
        # Split on net declarations: lines starting with "- <net_name>"
        net_chunks = re.split(r"(?m)^\s*-\s+", nets_text)
        for chunk in net_chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            lines = chunk.splitlines()
            header = lines[0].split()
            net_name = header[0]
            wires: list[dict[str, Any]] = []
            vias: list[dict[str, Any]] = []

            for line in lines:
                line = line.strip()
                # Check for layer cut pattern: ( x y ) cut_name
                m_via = re.search(
                    r"(?:ROUTED|NEW)\s+(\S+)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+([A-Za-z0-9_]+)",
                    line,
                )
                if m_via and not m_via.group(4).isdigit() and m_via.group(4) != "*":
                    vias.append(
                        {
                            "layer": m_via.group(1),
                            "x": int(m_via.group(2)),
                            "y": int(m_via.group(3)),
                            "via": m_via.group(4),
                        }
                    )
                    continue

                # Check for wire segment: ( x1 y1 [ext] ) ( x2 y2 [ext] )
                m_wire = re.search(
                    r"(?:ROUTED|NEW)\s+(\S+)\s+\(\s*(-?\d+)\s+(-?\d+)(?:\s+-?\d+)?\s*\)\s+\(\s*(\*|-?\d+)\s+(\*|-?\d+)",
                    line,
                )
                if m_wire:
                    layer = m_wire.group(1)
                    x1 = int(m_wire.group(2))
                    y1 = int(m_wire.group(3))
                    x2_str = m_wire.group(4)
                    y2_str = m_wire.group(5)
                    x2 = x1 if x2_str == "*" else int(x2_str)
                    y2 = y1 if y2_str == "*" else int(y2_str)
                    wires.append(
                        {
                            "layer": layer,
                            "x1": x1,
                            "y1": y1,
                            "x2": x2,
                            "y2": y2,
                        }
                    )

            nets.append(
                {
                    "name": net_name,
                    "wires": wires,
                    "vias": vias,
                }
            )

    return {
        "units": units,
        "components": components,
        "nets": nets,
    }


def _segment_direction(x1: int, y1: int, x2: int, y2: int) -> int:
    """Determine Factorio 2.0 cardinal direction from endpoint coordinates."""
    if x1 == x2:
        return DIRECTION_SOUTH if y2 >= y1 else DIRECTION_NORTH
    return DIRECTION_EAST if x2 >= x1 else DIRECTION_WEST


def build_entities(def_data: dict[str, Any], netlist_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate parsed DEF and netlist into Factorio blueprint entities."""
    scale = 1.0 / float(def_data.get("units", 1000))
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})

    entities: list[dict[str, Any]] = []
    occupied_positions: set[tuple[float, float]] = set()

    def add_entity(
        name: str,
        x: float,
        y: float,
        direction: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        pos_key = (round(x, 2), round(y, 2))
        if pos_key in occupied_positions:
            return
        occupied_positions.add(pos_key)
        entity: dict[str, Any] = {
            "name": name,
            "position": {"x": pos_key[0], "y": pos_key[1]},
        }
        if direction is not None:
            entity["direction"] = direction
        if extra:
            entity.update(extra)
        entities.append(entity)

    # 1. Place components
    for comp in def_data.get("components", []):
        cname = comp["name"]
        macro = comp["macro"]
        gx = comp["x"] * scale
        gy = comp["y"] * scale
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", "")
        cell = cell_map.get(cell_id, {})

        if macro == "belt_merge2" or cell_id == "belt_merge2":
            # Splitter: 2 tiles wide, 1 tile high, facing East
            add_entity("splitter", gx + 1.0, gy + 0.5, direction=DIRECTION_EAST)
        else:
            width = float(cell.get("width", 3))
            height = float(cell.get("height", 3))
            machine = cell.get("machine", "assembling-machine-3")
            recipe = inst.get("recipe", cell.get("recipe", ""))
            extra = {"recipe": recipe} if recipe else None
            add_entity(
                machine,
                gx + width / 2.0,
                gy + height / 2.0,
                direction=DIRECTION_NORTH,
                extra=extra,
            )

    # 2. Place underground belt tunnels (M2 wire segments)
    for net in def_data.get("nets", []):
        for wire in net.get("wires", []):
            if wire["layer"] == "M2":
                x1, y1 = wire["x1"], wire["y1"]
                x2, y2 = wire["x2"], wire["y2"]
                if (x1, y1) == (x2, y2):
                    continue
                tunnel_dir = _segment_direction(x1, y1, x2, y2)
                add_entity(
                    "underground-belt",
                    x1 * scale,
                    y1 * scale,
                    direction=tunnel_dir,
                    extra={"type": "input"},
                )
                add_entity(
                    "underground-belt",
                    x2 * scale,
                    y2 * scale,
                    direction=tunnel_dir,
                    extra={"type": "output"},
                )

    # 3. Place pipe-to-ground transitions
    for net in def_data.get("nets", []):
        for via in net.get("vias", []):
            if via["via"] == "PIPE_TO_GROUND_VIA":
                vx = via["x"] * scale
                vy = via["y"] * scale
                via_dir = DIRECTION_EAST
                for wire in net.get("wires", []):
                    if wire["layer"] == "M3":
                        if wire["x1"] == via["x"] and wire["y1"] == via["y"]:
                            via_dir = _segment_direction(
                                wire["x1"], wire["y1"], wire["x2"], wire["y2"]
                            )
                            break
                        if wire["x2"] == via["x"] and wire["y2"] == via["y"]:
                            via_dir = _segment_direction(
                                wire["x1"], wire["y1"], wire["x2"], wire["y2"]
                            )
                            break
                add_entity(
                    "pipe-to-ground",
                    vx,
                    vy,
                    direction=via_dir,
                    extra={"type": "input"},
                )

    # 4. Place surface wires (M1 belts and M3 pipes)
    unit_step = round(1.0 / scale)
    for net in def_data.get("nets", []):
        for wire in net.get("wires", []):
            layer = wire["layer"]
            if layer == "M2":
                # M2 is an underground tunnel. Do not place surface entities.
                continue

            entity_name = "pipe" if layer == "M3" else "transport-belt"
            x1, y1 = wire["x1"], wire["y1"]
            x2, y2 = wire["x2"], wire["y2"]

            if x1 == x2:
                # Vertical segment
                step = unit_step if y2 >= y1 else -unit_step
                direction = DIRECTION_SOUTH if y2 >= y1 else DIRECTION_NORTH
                for y_curr in range(y1, y2 + step, step):
                    add_entity(
                        entity_name,
                        x1 * scale,
                        y_curr * scale,
                        direction=direction if entity_name != "pipe" else None,
                    )
            elif y1 == y2:
                # Horizontal segment
                step = unit_step if x2 >= x1 else -unit_step
                direction = DIRECTION_EAST if x2 >= x1 else DIRECTION_WEST
                for x_curr in range(x1, x2 + step, step):
                    add_entity(
                        entity_name,
                        x_curr * scale,
                        y1 * scale,
                        direction=direction if entity_name != "pipe" else None,
                    )

    # Assign 1-based sequential entity numbers
    for idx, ent in enumerate(entities, 1):
        ent["entity_number"] = idx

    return entities


def def_to_blueprint(def_text: str, netlist_data: dict[str, Any], label: str = "factory") -> str:
    """Convert DEF text and netlist metadata into a blueprint exchange string."""
    def_data = parse_def(def_text)
    entities = build_entities(def_data, netlist_data)
    return blueprint.encode(entities, label)


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

    args = parser.parse_args()

    def_text = args.def_file.read_text()
    netlist_data = json.loads(args.netlist_file.read_text())

    def_data = parse_def(def_text)
    entities = build_entities(def_data, netlist_data)

    if args.json:
        document = {
            "blueprint": {
                "item": "blueprint",
                "label": args.label,
                "version": blueprint.VERSION_2_0,
                "entities": entities,
            }
        }
        output_text = json.dumps(document, indent=2)
    else:
        output_text = blueprint.encode(entities, args.label)

    if args.out:
        args.out.write_text(output_text + "\n")
    else:
        sys.stdout.write(output_text + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
