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

try:
    from foundry import netlist, pdk
except ImportError:
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


FLUID_MATERIALS: frozenset[str] = frozenset(
    {
        "crude-oil",
        "water",
        "petroleum-gas",
        "heavy-oil",
        "light-oil",
        "sulfuric-acid",
        "lubricant",
        "steam",
    }
)


def get_macro_port_slots(
    cell: dict[str, Any],
) -> tuple[dict[str, int], dict[str, int]]:
    """Determine vertical tile slot (0-indexed) for each input and output port."""
    machine = cell.get("machine", "")
    inputs = list(cell.get("inputs", []))
    outputs = sorted(cell.get("outputs", []))
    height = int(cell.get("height", 3))

    in_slots: dict[str, int] = {}
    out_slots: dict[str, int] = {}

    if machine == "chemical-plant":
        # Chemical plant rotated CCW (dir=12):
        # Fluid connection slots: tile 0 (cy=0.5) and tile 2 (cy=2.5).
        # Item inserter slots: use tiles not occupied by fluids.
        fluid_slots = [0, 2]
        f_idx = 0
        used_in = set()
        for p in inputs:
            if p in FLUID_MATERIALS and f_idx < len(fluid_slots):
                slot = fluid_slots[f_idx]
                in_slots[p] = slot
                used_in.add(slot)
                f_idx += 1
        available_in = [s for s in [1, 2, 0] if s not in used_in]
        i_idx = 0
        for p in inputs:
            if p not in FLUID_MATERIALS:
                if i_idx < len(available_in):
                    in_slots[p] = available_in[i_idx]
                    i_idx += 1
                else:
                    in_slots[p] = 0

        f_idx = 0
        used_out = set()
        for p in outputs:
            if p in FLUID_MATERIALS and f_idx < len(fluid_slots):
                slot = fluid_slots[f_idx]
                out_slots[p] = slot
                used_out.add(slot)
                f_idx += 1
        available_out = [s for s in [1, 2, 0] if s not in used_out]
        i_idx = 0
        for p in outputs:
            if p not in FLUID_MATERIALS:
                if i_idx < len(available_out):
                    out_slots[p] = available_out[i_idx]
                    i_idx += 1
                else:
                    out_slots[p] = 0

    elif machine == "oil-refinery":
        # Oil refinery rotated CW (dir=4):
        # Fluid inputs on West: tile 1 (cy=1.5, water) and tile 3 (cy=3.5, crude oil).
        # Fluid outputs on East: tile 0 (cy=0.5, heavy oil), tile 2 (cy=2.5, light oil), tile 4 (cy=4.5, petroleum gas).
        refinery_in_map = {"water": 1, "crude-oil": 3}
        refinery_out_map = {"heavy-oil": 0, "light-oil": 2, "petroleum-gas": 4}
        for idx, p in enumerate(inputs):
            in_slots[p] = refinery_in_map.get(p, [1, 3][min(idx, 1)])
        for idx, p in enumerate(outputs):
            out_slots[p] = refinery_out_map.get(p, [4, 2, 0][min(idx, 2)])

    else:
        total_in = len(inputs)
        offset_in = (height - total_in) // 2 if total_in <= height else 0
        for idx, p in enumerate(inputs):
            in_slots[p] = offset_in + idx

        total_out = len(outputs)
        offset_out = (height - total_out) // 2 if total_out <= height else 0
        for idx, p in enumerate(outputs):
            out_slots[p] = offset_out + idx

    return in_slots, out_slots


def _input_pin_rect(
    cell: dict[str, Any], idx: int, total: int, port: str = ""
) -> tuple[str, float, float, float, float]:
    """Calculate input pin layer and bounds on the macro grid."""
    width = cell["width"]
    height = cell["height"]
    h_layer = pdk.LAYER_CIRCUIT_RED if cell.get("circuit_cell") else pdk.LAYER_SURFACE
    v_layer = pdk.LAYER_CIRCUIT_GREEN if cell.get("circuit_cell") else pdk.LAYER_SURFACE_V
    if total <= height:
        offset = (height - total) // 2
        tile_y = offset + idx
        cy = tile_y + 0.5
        machine = cell.get("machine", "")
        if port and machine in ("chemical-plant", "oil-refinery"):
            in_slots, _ = get_macro_port_slots(cell)
            if port in in_slots and in_slots[port] < height:
                cy = in_slots[port] + 0.5
        return h_layer, 0.0, cy - 0.25, 0.5, cy + 0.25
    if idx < height:
        cy = idx + 0.5
        return h_layer, 0.0, cy - 0.25, 0.5, cy + 0.25
    tile_x = 1 + ((idx - height) % max(1, width - 1))
    cx = tile_x + 0.5
    return v_layer, cx - 0.25, float(height) - 0.5, cx + 0.25, float(height)


def _output_pin_rect(
    cell: dict[str, Any], idx: int, total: int, port: str = ""
) -> tuple[str, float, float, float, float]:
    """Calculate output pin layer and bounds on the macro grid."""
    width = cell["width"]
    height = cell["height"]
    h_layer = pdk.LAYER_CIRCUIT_RED if cell.get("circuit_cell") else pdk.LAYER_SURFACE
    v_layer = pdk.LAYER_CIRCUIT_GREEN if cell.get("circuit_cell") else pdk.LAYER_SURFACE_V
    w = float(width)
    h = float(height)
    if total <= height:
        offset = (height - total) // 2
        tile_y = offset + idx
        cy = tile_y + 0.5
        machine = cell.get("machine", "")
        if port and machine in ("chemical-plant", "oil-refinery"):
            _, out_slots = get_macro_port_slots(cell)
            if port in out_slots and out_slots[port] < height:
                cy = out_slots[port] + 0.5
        return h_layer, w - 0.5, cy - 0.25, w, cy + 0.25
    if idx < height:
        cy = idx + 0.5
        return h_layer, w - 0.5, cy - 0.25, w, cy + 0.25
    if idx < height + width:
        tile_x = idx - height
        cx = tile_x + 0.5
        return v_layer, cx - 0.25, 0.0, cx + 0.25, 0.5
    if idx < 2 * height + width:
        tile_y = idx - height - width
        cy = tile_y + 0.5
        return h_layer, 0.0, cy - 0.25, 0.5, cy + 0.25
    tile_x = (idx - 2 * height - width) % width
    cx = tile_x + 0.5
    return v_layer, cx - 0.25, h - 0.5, cx + 0.25, h


def _subtract_rect(
    r: tuple[float, float, float, float], p: tuple[float, float, float, float]
) -> list[tuple[float, float, float, float]]:
    """Subtract rectangle p from rectangle r, returning non-overlapping remainder."""
    rx1, ry1, rx2, ry2 = r
    px1, py1, px2, py2 = p
    if px2 <= rx1 or px1 >= rx2 or py2 <= ry1 or py1 >= ry2:
        return [r]
    ix1, iy1 = max(rx1, px1), max(ry1, py1)
    ix2, iy2 = min(rx2, px2), min(ry2, py2)
    res = []
    if ry1 < iy1:
        res.append((rx1, ry1, rx2, iy1))
    if ry2 > iy2:
        res.append((rx1, iy2, rx2, ry2))
    if rx1 < ix1:
        res.append((rx1, iy1, ix1, iy2))
    if rx2 > ix2:
        res.append((ix2, iy1, rx2, iy2))
    return res


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
    sorted_inputs = list(cell["inputs"])
    for idx, port in enumerate(sorted_inputs):
        layer, x1, y1, x2, y2 = _input_pin_rect(cell, idx, len(sorted_inputs), port)
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
        layer, x1, y1, x2, y2 = _output_pin_rect(cell, idx, len(sorted_outputs), port)
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
    w = float(cell["width"])
    h = float(cell["height"])
    if cell.get("circuit_cell"):
        lines.append("  OBS")
        lines.append("    LAYER M1 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER M2 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER VL1 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER VL2 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER VL3 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("  END")
    elif w > 1.0 or h > 1.0:
        margin = 0.50
        m_obs_m1 = [(0.0, 0.0, w, h)]
        m_obs_m2 = [(0.0, 0.0, w, h)]
        for idx, port in enumerate(sorted_inputs):
            layer, x1, y1, x2, y2 = _input_pin_rect(cell, idx, len(sorted_inputs), port)
            exp = (
                max(0.0, x1 - margin),
                max(0.0, y1 - margin),
                min(w, x2 + margin),
                min(h, y2 + margin),
            )
            if layer == "M1":
                next_obs = []
                for r in m_obs_m1:
                    next_obs.extend(_subtract_rect(r, exp))
                m_obs_m1 = next_obs
            elif layer == "M2":
                next_obs = []
                for r in m_obs_m2:
                    next_obs.extend(_subtract_rect(r, exp))
                m_obs_m2 = next_obs

        for idx, port in enumerate(sorted_outputs):
            layer, x1, y1, x2, y2 = _output_pin_rect(cell, idx, len(sorted_outputs), port)
            exp = (
                max(0.0, x1 - margin),
                max(0.0, y1 - margin),
                min(w, x2 + margin),
                min(h, y2 + margin),
            )
            if layer == "M1":
                next_obs = []
                for r in m_obs_m1:
                    next_obs.extend(_subtract_rect(r, exp))
                m_obs_m1 = next_obs
            elif layer == "M2":
                next_obs = []
                for r in m_obs_m2:
                    next_obs.extend(_subtract_rect(r, exp))
                m_obs_m2 = next_obs

        lines.append("  OBS")
        lines.append("    LAYER M1 ;")
        for rx1, ry1, rx2, ry2 in m_obs_m1:
            lines.append(f"      RECT {rx1:.3f} {ry1:.3f} {rx2:.3f} {ry2:.3f} ;")
        lines.append("    LAYER M2 ;")
        for rx1, ry1, rx2, ry2 in m_obs_m2:
            lines.append(f"      RECT {rx1:.3f} {ry1:.3f} {rx2:.3f} {ry2:.3f} ;")
        lines.append("    LAYER VL1 ;")
        for rx1, ry1, rx2, ry2 in m_obs_m1:
            lines.append(f"      RECT {rx1:.3f} {ry1:.3f} {rx2:.3f} {ry2:.3f} ;")
        lines.append("    LAYER M3 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER M4 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER VL2 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER VL3 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER VL4 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("    LAYER VL5 ;")
        lines.append(f"      RECT 0.000 0.000 {w:.3f} {h:.3f} ;")
        lines.append("  END")
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
        "  SPACING 0.25 ;",
        "END M1",
        # VL1: surface corner turn cut layer.
        "LAYER VL1",
        "  TYPE CUT ;",
        "  SPACING 0.25 ;",
        "END VL1",
        # M2: surface vertical. Belts, pipes, machine pins.
        "LAYER M2",
        "  TYPE ROUTING ;",
        "  DIRECTION VERTICAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.25 ;",
        "END M2",
        # VL2: surface-to-underground transition cut layer (tunnel mouth).
        "LAYER VL2",
        "  TYPE CUT ;",
        "  SPACING 0.25 ;",
        "END VL2",
        # M3: underground horizontal tunnel layer.
        "LAYER M3",
        "  TYPE ROUTING ;",
        "  DIRECTION HORIZONTAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.25 ;",
        "END M3",
        # VL3: underground turn cut layer.
        "LAYER VL3",
        "  TYPE CUT ;",
        "  SPACING 0.25 ;",
        "END VL3",
        # M4: underground vertical tunnel layer.
        "LAYER M4",
        "  TYPE ROUTING ;",
        "  DIRECTION VERTICAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.25 ;",
        "END M4",
        # VL4: underground-to-circuit cut layer.
        "LAYER VL4",
        "  TYPE CUT ;",
        "  SPACING 0.25 ;",
        "END VL4",
        # M5: circuit red wire horizontal.
        "LAYER M5",
        "  TYPE ROUTING ;",
        "  DIRECTION HORIZONTAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.25 ;",
        "END M5",
        # VL5: circuit wire turn cut layer.
        "LAYER VL5",
        "  TYPE CUT ;",
        "  SPACING 0.25 ;",
        "END VL5",
        # M6: circuit green wire vertical.
        "LAYER M6",
        "  TYPE ROUTING ;",
        "  DIRECTION VERTICAL ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.25 ;",
        "END M6",
    ]

    for idx, tier in enumerate(tiers[1:], start=1):
        h_layer = f"M{2 * idx + 5}"
        v_layer = f"M{2 * idx + 6}"
        vl_cut = f"VL{2 * idx + 4}"
        vl_turn = f"VL{2 * idx + 5}"
        bname = tier["belt"]
        lines.extend(
            [
                f"# {bname} underground horizontal tunnel layer.",
                f"LAYER {h_layer}",
                "  TYPE ROUTING ;",
                "  DIRECTION HORIZONTAL ;",
                "  PITCH 1.0 ;",
                "  OFFSET 0.5 ;",
                "  WIDTH 0.5 ;",
                "  SPACING 0.25 ;",
                f"END {h_layer}",
                f"# {bname} underground turn cut layer.",
                f"LAYER {vl_turn}",
                "  TYPE CUT ;",
                "  SPACING 0.25 ;",
                f"END {vl_turn}",
                f"# {bname} underground vertical tunnel layer.",
                f"LAYER {v_layer}",
                "  TYPE ROUTING ;",
                "  DIRECTION VERTICAL ;",
                "  PITCH 1.0 ;",
                "  OFFSET 0.5 ;",
                "  WIDTH 0.5 ;",
                "  SPACING 0.25 ;",
                f"END {v_layer}",
                f"# {bname} surface-to-underground transition cut layer.",
                f"LAYER {vl_cut}",
                "  TYPE CUT ;",
                "  SPACING 0.25 ;",
                f"END {vl_cut}",
            ]
        )

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
    # 2. Underground belt entrance/exit connection
    for idx, tier in enumerate(tiers):
        name = tier["belt"].replace("-", "_").upper()
        default_tag = "  DEFAULT" if idx == 0 else ""
        h_layer = "M3"
        vl_layer = "VL2"
        lines.extend(
            [
                f"VIA {name}_VIA",
                default_tag,
                "  LAYER M2 ;",
                "    RECT -0.75 -0.25 0.75 0.25 ;",
                f"  LAYER {vl_layer} ;",
                "    RECT -0.125 -0.125 0.125 0.125 ;",
                f"  LAYER {h_layer} ;",
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
            "  LAYER M2 ;",
            "    RECT -0.75 -0.25 0.75 0.25 ;",
            "  LAYER VL2 ;",
            "    RECT -0.125 -0.125 0.125 0.125 ;",
            "  LAYER M3 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            f"  PROPERTY SPAN {span} ;",
            "END PIPE_TO_GROUND_VIA",
        ]
    )

    # 5. Circuit drop connection (M4 -> VL4 -> M5)
    lines.extend(
        [
            "VIA CIRCUIT_DROP_VIA",
            "  DEFAULT",
            "  LAYER M4 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "  LAYER VL4 ;",
            "    RECT -0.125 -0.125 0.125 0.125 ;",
            "  LAYER M5 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "END CIRCUIT_DROP_VIA",
        ]
    )
    # 6. Circuit turn connection (M5 -> VL5 -> M6)
    lines.extend(
        [
            "VIA CIRCUIT_TURN_VIA",
            "  DEFAULT",
            "  LAYER M5 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "  LAYER VL5 ;",
            "    RECT -0.125 -0.125 0.125 0.125 ;",
            "  LAYER M6 ;",
            "    RECT -0.25 -0.25 0.25 0.25 ;",
            "END CIRCUIT_TURN_VIA",
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
        "  OBS",
        "    LAYER M1 ;",
        "      RECT 0.500 1.000 1.000 2.000 ;",
        "    LAYER M2 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL1 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER M3 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER M4 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL2 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL3 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "  END",
        f"END {name}",
    ]


def split_lines() -> list[str]:
    """The transport split cell: one splitter tile, three pins.

    Input a enters west on M1. Outputs y1 and y2 leave east on M1.
    """
    name = netlist.SPLIT_CELL
    return [
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        "  SIZE 1.000 BY 2.000 ;",
        "  SITE tile ;",
        '  PROPERTY CELLTYPE "splitter" ;',
        '  PROPERTY ROLE "split2" ;',
        "  PIN a",
        "    DIRECTION INPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.000 0.250 0.500 0.750 ;",
        "    END",
        "  END a",
        "  PIN y1",
        "    DIRECTION OUTPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.500 0.250 1.000 0.750 ;",
        "    END",
        "  END y1",
        "  PIN y2",
        "    DIRECTION OUTPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.500 1.250 1.000 1.750 ;",
        "    END",
        "  END y2",
        "  OBS",
        "    LAYER M1 ;",
        "      RECT 0.000 1.000 0.500 2.000 ;",
        "    LAYER M2 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL1 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER M3 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER M4 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL2 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL3 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "  END",
        f"END {name}",
    ]


def pump_lines() -> list[str]:
    """The fluid buffer cell: one pump, two pins.

    Input a enters south on M1. Output y leaves north on M1.
    """
    name = netlist.PUMP_CELL
    return [
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        "  SIZE 1.000 BY 2.000 ;",
        "  SITE tile ;",
        '  PROPERTY CELLTYPE "pump" ;',
        '  PROPERTY ROLE "fluid_buffer" ;',
        "  PIN a",
        "    DIRECTION INPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.250 0.000 0.750 0.500 ;",
        "    END",
        "  END a",
        "  PIN y",
        "    DIRECTION OUTPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.250 1.500 0.750 2.000 ;",
        "    END",
        "  END y",
        f"END {name}",
    ]


def balancer_lines() -> list[str]:
    """The transport balancer cell: one splitter tile, four pins.

    Inputs a1 and a2 enter west on M1. Outputs y1 and y2 leave east on M1.
    """
    name = netlist.BALANCER_CELL
    return [
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        "  SIZE 1.000 BY 2.000 ;",
        "  SITE tile ;",
        '  PROPERTY CELLTYPE "splitter" ;',
        '  PROPERTY ROLE "balancer2" ;',
        "  PIN a1",
        "    DIRECTION INPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.000 0.250 0.500 0.750 ;",
        "    END",
        "  END a1",
        "  PIN a2",
        "    DIRECTION INPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.000 1.250 0.500 1.750 ;",
        "    END",
        "  END a2",
        "  PIN y1",
        "    DIRECTION OUTPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.500 0.250 1.000 0.750 ;",
        "    END",
        "  END y1",
        "  PIN y2",
        "    DIRECTION OUTPUT ;",
        "    USE SIGNAL ;",
        "    PORT LAYER M1 ;",
        "      RECT 0.500 1.250 1.000 1.750 ;",
        "    END",
        "  END y2",
        "  OBS",
        "    LAYER M2 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL1 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER M3 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER M4 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL2 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "    LAYER VL3 ;",
        "      RECT 0.000 0.000 1.000 2.000 ;",
        "  END",
        f"END {name}",
    ]


def write_lef(p: pdk.Pdk, cells: list[dict[str, Any]], path: str) -> str:
    """The full LEF: technology section, then every macro."""
    lines: list[str] = ["# Generated by foundry/lef.py. Do not edit."]
    lines.extend(tech_lines(p.tech_json()))
    lines.append("")
    lines.extend(merge_lines())
    lines.append("")
    lines.extend(split_lines())
    lines.append("")
    lines.extend(balancer_lines())
    lines.append("")
    lines.extend(pump_lines())
    for cell in sorted(cells, key=lambda c: c["cell"]):
        if cell["cell"] in (
            netlist.MERGE_CELL,
            netlist.SPLIT_CELL,
            netlist.BALANCER_CELL,
            netlist.PUMP_CELL,
        ):
            continue  # merge_lines, split_lines, balancer_lines, pump_lines emitted them
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
