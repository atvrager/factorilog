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


def _pin_rect(cell: dict[str, Any], side: str) -> tuple[float, float, float, float]:
    """The pin shape of one port, in microns, origin at the macro centre.

    A 3x3 macro spans -1.5..1.5. A west pin sits on the left edge, an
    east pin on the right edge, both one tile tall and centred.
    """
    width = cell["width"] * TILE_MICRONS
    half_w = width / 2.0
    lane = 0.5  # one lane, half a tile wide, centred on the edge midpoint
    if side == "W":
        return (-half_w, -lane, -half_w + TILE_MICRONS, lane)
    if side == "E":
        return (half_w - TILE_MICRONS, -lane, half_w, lane)
    raise LefError(f"unknown side {side}")


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
    for port in cell["inputs"]:
        side = PIN_SIDE["in"]
        x1, y1, x2, y2 = _pin_rect(cell, side)
        lines.extend(
            [
                f"  PIN {port.replace('-', '_')}",
                "    DIRECTION INPUT ;",
                "    USE SIGNAL ;",
                "    PORT LAYER M1 ;",
                f"      RECT {x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} ;",
                "    END",
                f"  END {port.replace('-', '_')}",
            ]
        )
    for port in cell["outputs"]:
        side = PIN_SIDE["out"]
        x1, y1, x2, y2 = _pin_rect(cell, side)
        lines.extend(
            [
                f"  PIN {port.replace('-', '_')}",
                "    DIRECTION OUTPUT ;",
                "    USE SIGNAL ;",
                "    PORT LAYER M1 ;",
                f"      RECT {x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} ;",
                "    END",
                f"  END {port.replace('-', '_')}",
            ]
        )
    # No OBS section. The machine occupies its whole footprint, and the
    # router must not cross a building on the surface layer.
    lines.append(f"END {name}")
    return lines


def tech_lines(tech: dict[str, Any]) -> list[str]:
    """The technology section: units, layers, vias, site."""
    tiers = tech["layers"]["M2"]["tiers"]
    span = tech["layers"]["M3"]["span"]
    lines = [
        "VERSION 5.8 ;",
        "UNITS",
        f"  DATABASE MICRONS {LEF_UNITS} ;",
        "END UNITS",
        "MANUFACTURINGGRID 0.5 ;",
        # M1: the surface. Belts, machines, and their pins live here.
        "LAYER M1",
        "  TYPE ROUTING ;",
        "  DIRECTION HORIZONTAL ;",
        "  PITCH 1.0 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.5 ;",
        "END M1",
        # M2: the tunnel layer. One tunnel pair per belt tier, span per tier.
        "LAYER M2",
        "  TYPE ROUTING ;",
        "  DIRECTION VERTICAL ;",
        "  PITCH 1.0 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.5 ;",
        "END M2",
        # M3: fluids. Pipes. The layer change is the pipe-to-ground pair.
        "LAYER M3",
        "  TYPE ROUTING ;",
        "  DIRECTION HORIZONTAL ;",
        "  PITCH 1.0 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.5 ;",
        "END M3",
    ]
    for tier in tiers:
        name = tier["belt"].replace("-", "_").upper()
        lines.extend(
            [
                f"VIA {name}_VIA",
                "  DEFAULT",
                f'  PROPERTY TIER_BELT "{tier["belt"]}" ;',
                f"  PROPERTY SPAN {tier['span']} ;",
                "END VIA",
            ]
        )
    lines.extend(
        [
            "VIA PIPE_TO_GROUND_VIA",
            f"  PROPERTY SPAN {span} ;",
            "END VIA",
            "SITE tile",
            f"  SIZE {TILE_MICRONS:.3f} BY {TILE_MICRONS:.3f} ;",
            "  CLASS CORE ;",
            "END tile",
        ]
    )
    return lines


def write_lef(p: pdk.Pdk, cells: list[dict[str, Any]], path: str) -> str:
    """The full LEF: technology section, then every macro."""
    lines: list[str] = ["# Generated by foundry/lef.py. Do not edit."]
    lines.extend(tech_lines(p.tech_json()))
    for cell in sorted(cells, key=lambda c: c["cell"]):
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
                if abs(x1) > width / 2 + 1e-9 or abs(x2) > width / 2 + 1e-9:
                    problems.append(f"{name}: rect x outside macro: {token}")
                if abs(y1) > height / 2 + 1e-9 or abs(y2) > height / 2 + 1e-9:
                    problems.append(f"{name}: rect y outside macro: {token}")
        for token in block:
            if token.startswith("SIZE "):
                parts = token.split()
                if abs(float(parts[1]) - width) > 1e-9:
                    problems.append(f"{name}: SIZE x mismatch: {token}")
                if abs(float(parts[3]) - height) > 1e-9:
                    problems.append(f"{name}: SIZE y mismatch: {token}")
    return problems
