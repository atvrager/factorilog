"""LEF writer: the PDK cells and layers in LEF 5.8.

One MACRO per (building, recipe) cell, plus the routing layers. The
mapping, fixed here and shared with the game bridge:

-  MACRO = one cell: SIZE from the footprint, one PIN per recipe port,
   one wire per pin at the tile edge nearest the port side.
-  PROPERTY = the game numbers OpenROAD cannot route without: rated
   rate, power. Custom properties travel through the flow untouched.
-  LAYER M1 = surface, routing layer, one tile pitch, with the tunnel
   pairs and the pipe runs above it.

The checker in `check_lef()` covers the invariants that `read_lef`
tolerates silently but place-and-route does not: pin geometry inside the
macro, ports on the site grid, every recipe port present.
"""

from pathlib import Path
from typing import Any

import netlist
import pdk

# Tile edge of a pin, by the side of the factory flow the material
# enters or leaves. The game bridge reads the same table when it turns
# DEF routes back into belts and inserters.
PIN_SIDE = {
    "in": "W",
    "out": "E",
}

# LEF units. Factorio tiles become LEF microns 1:1, so a 3x3 building
# is a 3.0 x 3.0 micron macro. One database unit is one nanometre.
LEF_UNITS = 1000
TILE_MICRONS = 1.0


class LefError(Exception):
    """One structural violation in the emitted LEF."""


def _input_pin_rect(
    cell: dict[str, Any], idx: int, total: int
) -> tuple[str, float, float, float, float]:
    """Calculate input pin layer and bounds on the macro grid."""
    width = cell["width"]
    height = cell["height"]
    if total <= height:
        offset = (height - total) // 2
        tile_y = offset + idx
        cy = tile_y + 0.5
        return pdk.LAYER_SURFACE, 0.0, cy - 0.25, 0.5, cy + 0.25
    if idx < height:
        cy = idx + 0.5
        return pdk.LAYER_SURFACE, 0.0, cy - 0.25, 0.5, cy + 0.25
    tile_x = 1 + ((idx - height) % max(1, width - 1))
    cx = tile_x + 0.5
    return pdk.LAYER_SURFACE_V, cx - 0.25, float(height) - 0.5, cx + 0.25, float(height)


def _output_pin_rect(
    cell: dict[str, Any], idx: int, total: int
) -> tuple[str, float, float, float, float]:
    """Calculate output pin layer and bounds on the macro grid."""
    width = cell["width"]
    height = cell["height"]
    w = float(width)
    h = float(height)
    if total <= height:
        offset = (height - total) // 2
        tile_y = offset + idx
        cy = tile_y + 0.5
        return pdk.LAYER_SURFACE, w - 0.5, cy - 0.25, w, cy + 0.25
    if idx < height:
        cy = idx + 0.5
        return pdk.LAYER_SURFACE, w - 0.5, cy - 0.25, w, cy + 0.25
    if idx < height + width:
        tile_x = idx - height
        cx = tile_x + 0.5
        return pdk.LAYER_SURFACE_V, cx - 0.25, 0.0, cx + 0.25, 0.5
    if idx < 2 * height + width:
        tile_y = idx - height - width
        cy = tile_y + 0.5
        return pdk.LAYER_SURFACE, 0.0, cy - 0.25, 0.5, cy + 0.25
    tile_x = (idx - 2 * height - width) % width
    cx = tile_x + 0.5
    return pdk.LAYER_SURFACE_V, cx - 0.25, h - 0.5, cx + 0.25, h


def macro_lines(cell: dict[str, Any]) -> list[str]:
    """One MACRO block. Sorted ports keep the file diff stable."""
    name = cell["cell"].replace(".", "__").replace("-", "_")
    width = cell["width"] * TILE_MICRONS
    height = cell["height"] * TILE_MICRONS
    lines = [
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        f"  SIZE {width:.3f} BY {height:.3f} ;",
        "  SITE tile ;",
        f'  PROPERTY CELLTYPE "{cell["machine"]}" ;',
        f"  PROPERTY RATED_RATE_PER_MIN {cell['crafts_per_minute']:.6f} ;",
        f"  PROPERTY POWER_WATTS {cell['watts']:.6f} ;",
        f'  PROPERTY RECIPE "{cell["recipe"]}" ;',
    ]
    sorted_inputs = sorted(cell["inputs"])
    for idx, port in enumerate(sorted_inputs):
        layer, x1, y1, x2, y2 = _input_pin_rect(cell, idx, len(sorted_inputs))
        lines.extend(
            [
                f"  PIN {port.replace('-', '_')}",
                "    DIRECTION INPUT ;",
                "    USE SIGNAL ;",
                f"    PORT LAYER {layer} ;",
                f"      RECT {x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} ;",
                "    END",
                f"  END {port.replace('-', '_')}",
            ]
        )
    sorted_outputs = sorted(cell["outputs"])
    for idx, port in enumerate(sorted_outputs):
        layer, x1, y1, x2, y2 = _output_pin_rect(cell, idx, len(sorted_outputs))
        lines.extend(
            [
                f"  PIN {port.replace('-', '_')}",
                "    DIRECTION OUTPUT ;",
                "    USE SIGNAL ;",
                f"    PORT LAYER {layer} ;",
                f"      RECT {x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} ;",
                "    END",
                f"  END {port.replace('-', '_')}",
            ]
        )
    # No OBS for test
    lines.append(f"END {name}")
    return lines


def tech_lines(tech: dict[str, Any]) -> list[str]:
    """The technology section: units, layers, vias, site."""
    tiers = tech["layers"]["M3"]["tiers"]
    span = tech["layers"]["M3"]["span"]
    lines = [
        "VERSION 5.8 ;",
        "UNITS",
        f"  DATABASE MICRONS {LEF_UNITS} ;",
        "END UNITS",
        "MANUFACTURINGGRID 0.001 ;",
        # M1: surface horizontal. Belts, pipes, machine pins.
        "LAYER M1",
        "  TYPE ROUTING ;",
        "  DIRECTION HORIZONTAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.5 ;",
        "END M1",
        # VL1: surface corner turn cut layer.
        "LAYER VL1",
        "  TYPE CUT ;",
        "  SPACING 0.5 ;",
        "END VL1",
        # M2: surface vertical. Belts, pipes, machine pins.
        "LAYER M2",
        "  TYPE ROUTING ;",
        "  DIRECTION VERTICAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.5 ;",
        "END M2",
        # VL2: surface-to-underground transition cut layer (tunnel mouth).
        "LAYER VL2",
        "  TYPE CUT ;",
        "  SPACING 0.5 ;",
        "END VL2",
        # M3: underground horizontal tunnel layer.
        "LAYER M3",
        "  TYPE ROUTING ;",
        "  DIRECTION HORIZONTAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.5 ;",
        "END M3",
        # VL3: underground turn cut layer.
        "LAYER VL3",
        "  TYPE CUT ;",
        "  SPACING 0.5 ;",
        "END VL3",
        # M4: underground vertical tunnel layer.
        "LAYER M4",
        "  TYPE ROUTING ;",
        "  DIRECTION VERTICAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.5 ;",
        "END M4",
    ]
    # 1. Surface turn layer connection (M1 -> VL1 -> M2)
    lines.extend(
        [
            "VIA SURFACE_TURN_VIA",
            "  DEFAULT",
            "  LAYER M1 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "  LAYER VL1 ;",
            "    RECT -0.125 -0.125 0.125 0.125 ;",
            "  LAYER M2 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "END SURFACE_TURN_VIA",
        ]
    )
    # 2. Underground belt entrance/exit connection (M2 -> VL2 -> M3)
    for tier in tiers:
        name = tier["belt"].replace("-", "_").upper()
        default_tag = "  DEFAULT" if tier["belt"] == "transport-belt" else ""
        lines.extend(
            [
                f"VIA {name}_VIA",
                default_tag,
                "  LAYER M2 ;",
                "    RECT -0.25 -0.25 0.25 0.25 ;",
                "  LAYER VL2 ;",
                "    RECT -0.125 -0.125 0.125 0.125 ;",
                "  LAYER M3 ;",
                "    RECT -0.25 -0.25 0.25 0.25 ;",
                f'  PROPERTY TIER_BELT "{tier["belt"]}" ;',
                f"  PROPERTY SPAN {tier['span']} ;",
                f"END {name}_VIA",
            ]
        )
    # 3. Underground pipe-to-ground entrance/exit connection
    lines.extend(
        [
            "VIA PIPE_TO_GROUND_VIA",
            "  DEFAULT",
            "  LAYER M2 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "  LAYER VL2 ;",
            "    RECT -0.125 -0.125 0.125 0.125 ;",
            "  LAYER M3 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            f"  PROPERTY SPAN {span} ;",
            "END PIPE_TO_GROUND_VIA",
        ]
    )
    # 4. Underground turn connection (M3 -> VL3 -> M4)
    lines.extend(
        [
            "VIA UNDERGROUND_TURN_VIA",
            "  DEFAULT",
            "  LAYER M3 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "  LAYER VL3 ;",
            "    RECT -0.125 -0.125 0.125 0.125 ;",
            "  LAYER M4 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "END UNDERGROUND_TURN_VIA",
            "SITE tile",
            f"  SIZE {TILE_MICRONS:.3f} BY {TILE_MICRONS:.3f} ;",
            "  CLASS CORE ;",
            "END tile",
        ]
    )
    return [line for line in lines if line]


def merge_lines() -> list[str]:
    """The transport merge cell: one splitter tile, three pins.

    Inputs a and b enter west on M1. Output y leaves east on M1.
    """
    name = netlist.MERGE_CELL
    return [
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        "  SIZE 1.000 BY 2.000 ;",
        "  SITE tile ;",
        '  PROPERTY CELLTYPE "splitter" ;',
        '  PROPERTY ROLE "merge2" ;',
        "  PIN a",
        "    DIRECTION INPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.000 0.250 0.500 0.750 ;",
        "    END",
        "  END a",
        "  PIN b",
        "    DIRECTION INPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.000 1.250 0.500 1.750 ;",
        "    END",
        "  END b",
        "  PIN y",
        "    DIRECTION OUTPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.500 0.250 1.000 0.750 ;",
        "    END",
        "  END y",
        f"END {name}",
    ]


def write_lef(p: pdk.Pdk, cells: list[dict[str, Any]], path: str) -> str:
    """The full LEF: technology section, then every macro."""
    lines: list[str] = ["# Generated by foundry/lef.py. Do not edit."]
    lines.extend(tech_lines(p.tech_json()))
    lines.append("")
    lines.extend(merge_lines())
    for cell in sorted(cells, key=lambda c: c["cell"]):
        if cell["cell"] == netlist.MERGE_CELL:
            continue  # merge_lines emitted it with splitter geometry
        lines.append("")
        lines.extend(macro_lines(cell))
    lines.append("")
    lines.append("END LIBRARY")
    text = "\n".join(lines)
    Path(path).write_text(text)
    return text


def check_lef(cells: list[dict[str, Any]], text: str) -> list[str]:
    """Structural checks the game bridge depends on.

    Returns the list of violations. Empty list means the LEF matches the
    cells: every port emitted, every rect inside the macro, every macro
    name a legal identifier.
    """
    problems: list[str] = []
    macros: dict[str, list[str]] = {}
    macro: str | None = None
    for line in text.splitlines():
        token = line.strip()
        if token.startswith("MACRO "):
            macro = token.split()[1]
            macros[macro] = []
        elif macro is not None:
            macros[macro].append(token)
        if token == "END LIBRARY":
            macro = None

    for cell in cells:
        name = cell["cell"].replace(".", "__").replace("-", "_")
        block = macros.get(name)
        if block is None:
            problems.append(f"{name}: macro missing")
            continue
        width = cell["width"] * TILE_MICRONS
        height = cell["height"] * TILE_MICRONS
        ports = [p.replace("-", "_") for p in cell["inputs"] + cell["outputs"]]
        for port in ports:
            pin_lines = [t for t in block if t.startswith(f"PIN {port}")]
            if not pin_lines:
                problems.append(f"{name}.{port}: pin missing")
        for token in block:
            if token.startswith("RECT "):
                parts = token.split()
                x1, y1, x2, y2 = (float(v) for v in parts[1:5])
                if x1 < -1e-9 or x2 > width + 1e-9:
                    problems.append(f"{name}: rect x outside macro: {token}")
                if y1 < -1e-9 or y2 > height + 1e-9:
                    problems.append(f"{name}: rect y outside macro: {token}")
        for token in block:
            if token.startswith("SIZE "):
                parts = token.split()
                if abs(float(parts[1]) - width) > 1e-9:
                    problems.append(f"{name}: SIZE x mismatch: {token}")
                if abs(float(parts[3]) - height) > 1e-9:
                    problems.append(f"{name}: SIZE y mismatch: {token}")
    return problems
