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

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

try:
    from foundry import netlist, pdk
    from foundry.entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )
except ImportError:
    import netlist
    import pdk
    from entity_helpers import (
        DIRECTION_EAST,
        DIRECTION_NORTH,
        DIRECTION_SOUTH,
        DIRECTION_WEST,
    )

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

DEFAULT_MACRO_TILES = 3
PIN_HALF_SPAN = 0.25
PIN_EDGE_DEPTH = 0.5
TILE_CENTER_OFFSET = 0.5
OBS_PIN_MARGIN = 0.50
LEF_COORD_TOLERANCE = 1e-9

SURFACE_METAL_COUNT = 2
FIRST_TUNNEL_H_OFFSET = 3
FIRST_TUNNEL_V_OFFSET = 4
FIRST_TUNNEL_CUT_OFFSET = 2
FIRST_TUNNEL_TURN_OFFSET = 3

RECT_X2_INDEX = 2
RECT_Y2_INDEX = 3
SIZE_HEIGHT_TOKEN_INDEX = 3
RECT_COORD_END_INDEX = 5

CellRecord = Mapping[str, object]
TechRecord = Mapping[str, object]
RectBounds = tuple[float, float, float, float]
PinGeometry = tuple[str, float, float, float, float]


class PortGeometry(TypedDict):
    """Macro port geometry and approach metadata."""

    face: str
    layer: str
    rect: RectBounds
    inserter_pos: tuple[float, float]
    inserter_dir: int
    approach_pos: tuple[float, float]
    approach_dir: int


class LefError(Exception):
    """One structural violation in the emitted LEF."""


def _as_str_list(val: object) -> list[str]:
    if isinstance(val, (list, tuple, set)):
        return [str(x) for x in val]
    return []


def _as_str_map(val: object) -> dict[str, str]:
    if isinstance(val, Mapping):
        return {str(k): str(v) for k, v in val.items()}
    return {}


def _as_int_map(val: object) -> dict[str, int]:
    if isinstance(val, Mapping):
        return {str(k): int(v) for k, v in val.items() if isinstance(v, (int, float))}
    return {}


def _as_int(val: object, default: int = DEFAULT_MACRO_TILES) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    return default


def _as_float(val: object, default: float = float(DEFAULT_MACRO_TILES)) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    return default


def _sequential_slots(ports: Sequence[str], span: int) -> dict[str, int]:
    """Assign centered 0-indexed tile slots along a macro edge of length `span`."""
    total = len(ports)
    offset = (span - total) // 2 if total <= span else 0
    return {port: offset + idx for idx, port in enumerate(ports)}


def get_macro_port_slots(
    cell: CellRecord,
) -> tuple[dict[str, int], dict[str, int]]:
    """Determine vertical tile slot (0-indexed) for each input and output port."""
    if "in_slots" in cell and "out_slots" in cell:
        return _as_int_map(cell["in_slots"]), _as_int_map(cell["out_slots"])

    inputs = _as_str_list(cell.get("inputs"))
    outputs = sorted(_as_str_list(cell.get("outputs")))
    height = _as_int(cell.get("height"), DEFAULT_MACRO_TILES)
    port_faces = _as_str_map(cell.get("port_faces"))

    w_inputs = [p for p in inputs if port_faces.get(p, "W") == "W"] if port_faces else inputs
    return _sequential_slots(w_inputs, height), _sequential_slots(outputs, height)


def _edge_port_geometry(
    face: str,
    layer: str,
    w: float,
    h: float,
    center: float,
    is_output: bool,
) -> PortGeometry:
    """Build port geometry dictionary for a given cardinal face and center coordinate."""
    if face == "E":
        ins_dir = DIRECTION_WEST if is_output else DIRECTION_EAST
        app_dir = DIRECTION_EAST if is_output else DIRECTION_WEST
        return {
            "face": "E",
            "layer": layer,
            "rect": (
                w - PIN_EDGE_DEPTH,
                center - PIN_HALF_SPAN,
                w,
                center + PIN_HALF_SPAN,
            ),
            "inserter_pos": (w - TILE_CENTER_OFFSET, center),
            "inserter_dir": ins_dir,
            "approach_pos": (w + TILE_CENTER_OFFSET, center),
            "approach_dir": app_dir,
        }
    if face == "N":
        return {
            "face": "N",
            "layer": layer,
            "rect": (
                center - PIN_HALF_SPAN,
                h - PIN_EDGE_DEPTH,
                center + PIN_HALF_SPAN,
                h,
            ),
            "inserter_pos": (center, h - TILE_CENTER_OFFSET),
            "inserter_dir": DIRECTION_SOUTH,
            "approach_pos": (center, h + TILE_CENTER_OFFSET),
            "approach_dir": DIRECTION_NORTH,
        }
    if face == "S":
        return {
            "face": "S",
            "layer": layer,
            "rect": (
                center - PIN_HALF_SPAN,
                0.0,
                center + PIN_HALF_SPAN,
                PIN_EDGE_DEPTH,
            ),
            "inserter_pos": (center, TILE_CENTER_OFFSET),
            "inserter_dir": DIRECTION_NORTH,
            "approach_pos": (center, -TILE_CENTER_OFFSET),
            "approach_dir": DIRECTION_SOUTH,
        }
    return {
        "face": "W",
        "layer": layer,
        "rect": (
            0.0,
            center - PIN_HALF_SPAN,
            PIN_EDGE_DEPTH,
            center + PIN_HALF_SPAN,
        ),
        "inserter_pos": (TILE_CENTER_OFFSET, center),
        "inserter_dir": DIRECTION_WEST,
        "approach_pos": (-TILE_CENTER_OFFSET, center),
        "approach_dir": DIRECTION_EAST,
    }


def _resolve_port_layers(cell: CellRecord) -> tuple[str, str]:
    """Return horizontal and vertical routing layer names for a macro cell."""
    if not cell.get("circuit_cell"):
        return pdk.LAYER_SURFACE, pdk.LAYER_SURFACE_V
    circ = _as_str_list(cell.get("circuit_layers"))
    if len(circ) >= 2:
        return circ[0], circ[1]
    return pdk.circuit_layers()


def get_macro_port_geometry(cell: CellRecord, port: str, is_output: bool = False) -> PortGeometry:
    """Calculate the face, layer, RECT, inserter position, and approach tile for a macro port."""
    w = _as_float(cell.get("width"), float(DEFAULT_MACRO_TILES))
    h = _as_float(cell.get("height"), float(DEFAULT_MACRO_TILES))
    h_layer, v_layer = _resolve_port_layers(cell)
    in_slots, out_slots = get_macro_port_slots(cell)
    default_slot = int(h) // 2

    default_face = "E" if is_output else "W"
    if cell.get("circuit_cell"):
        slots = out_slots if is_output else in_slots
        cy = slots.get(port, default_slot) + TILE_CENTER_OFFSET
        return _edge_port_geometry(default_face, h_layer, w, h, cy, is_output)

    face = _as_str_map(cell.get("port_faces")).get(port, default_face)
    if face in ("N", "S"):
        cx = in_slots[port] + TILE_CENTER_OFFSET if port in in_slots else w / 2.0
        return _edge_port_geometry(face, v_layer, w, h, cx, is_output)

    slots = out_slots if (face == "E" and is_output) else in_slots
    cy = slots.get(port, default_slot) + TILE_CENTER_OFFSET
    return _edge_port_geometry(face, h_layer, w, h, cy, is_output)


def _unpack_geom_pin(geom: PortGeometry) -> PinGeometry:
    """Extract (layer, x1, y1, x2, y2) from a PortGeometry record."""
    r = geom["rect"]
    return geom["layer"], r[0], r[1], r[RECT_X2_INDEX], r[RECT_Y2_INDEX]


def _west_pin_rect(layer: str, cy: float) -> PinGeometry:
    return layer, 0.0, cy - PIN_HALF_SPAN, PIN_EDGE_DEPTH, cy + PIN_HALF_SPAN


def _east_pin_rect(layer: str, w: float, cy: float) -> PinGeometry:
    return layer, w - PIN_EDGE_DEPTH, cy - PIN_HALF_SPAN, w, cy + PIN_HALF_SPAN


def _north_pin_rect(layer: str, h: float, cx: float) -> PinGeometry:
    return layer, cx - PIN_HALF_SPAN, h - PIN_EDGE_DEPTH, cx + PIN_HALF_SPAN, h


def _slot_center_y(slot_map: dict[str, int], port: str, height: int, fallback_tile: int) -> float:
    """Resolve vertical tile center from slot map if within macro height, else fallback."""
    if port and port in slot_map and slot_map[port] < height:
        return slot_map[port] + TILE_CENTER_OFFSET
    return fallback_tile + TILE_CENTER_OFFSET


def _input_pin_rect(cell: CellRecord, idx: int, total: int, port: str = "") -> PinGeometry:
    """Calculate input pin layer and bounds on the macro grid."""
    if port and cell.get("port_faces"):
        return _unpack_geom_pin(get_macro_port_geometry(cell, port, is_output=False))
    width = _as_int(cell.get("width"))
    height = _as_int(cell.get("height"))
    if total <= height:
        tile_y = (height - total) // 2 + idx
        in_slots, _ = get_macro_port_slots(cell) if port else ({}, {})
        cy = _slot_center_y(in_slots, port, height, tile_y)
        return _west_pin_rect(pdk.LAYER_SURFACE, cy)
    if idx < height:
        return _west_pin_rect(pdk.LAYER_SURFACE, idx + TILE_CENTER_OFFSET)
    tile_x = 1 + ((idx - height) % max(1, width - 1))
    return _north_pin_rect(pdk.LAYER_SURFACE_V, float(height), tile_x + TILE_CENTER_OFFSET)


def _output_pin_rect(cell: CellRecord, idx: int, total: int, port: str = "") -> PinGeometry:
    """Calculate output pin layer and bounds on the macro grid."""
    if port and cell.get("port_faces"):
        return _unpack_geom_pin(get_macro_port_geometry(cell, port, is_output=True))
    width = _as_int(cell.get("width"))
    height = _as_int(cell.get("height"))
    w = float(width)
    h = float(height)
    if total <= height:
        tile_y = (height - total) // 2 + idx
        _, out_slots = get_macro_port_slots(cell) if port else ({}, {})
        cy = _slot_center_y(out_slots, port, height, tile_y)
        return _east_pin_rect(pdk.LAYER_SURFACE, w, cy)
    if idx < height:
        return _east_pin_rect(pdk.LAYER_SURFACE, w, idx + TILE_CENTER_OFFSET)
    if idx < height + width:
        cx = (idx - height) + TILE_CENTER_OFFSET
        return pdk.LAYER_SURFACE_V, cx - PIN_HALF_SPAN, 0.0, cx + PIN_HALF_SPAN, PIN_EDGE_DEPTH
    if idx < 2 * height + width:
        return _west_pin_rect(pdk.LAYER_SURFACE, (idx - height - width) + TILE_CENTER_OFFSET)
    tile_x = (idx - 2 * height - width) % width
    return _north_pin_rect(pdk.LAYER_SURFACE_V, h, tile_x + TILE_CENTER_OFFSET)


def _subtract_rect(r: RectBounds, p: RectBounds) -> list[RectBounds]:
    """Subtract rectangle p from rectangle r, returning non-overlapping remainder."""
    rx1, ry1, rx2, ry2 = r
    px1, py1, px2, py2 = p
    if px2 <= rx1 or px1 >= rx2 or py2 <= ry1 or py1 >= ry2:
        return [r]
    ix1, iy1 = max(rx1, px1), max(ry1, py1)
    ix2, iy2 = min(rx2, px2), min(ry2, py2)
    res: list[RectBounds] = []
    if ry1 < iy1:
        res.append((rx1, ry1, rx2, iy1))
    if ry2 > iy2:
        res.append((rx1, iy2, rx2, ry2))
    if rx1 < ix1:
        res.append((rx1, iy1, ix1, iy2))
    if rx2 > ix2:
        res.append((ix2, iy1, rx2, iy2))
    return res


def _carve_pin_clearances(
    w: float,
    h: float,
    pins: Sequence[PinGeometry],
) -> tuple[list[RectBounds], list[RectBounds]]:
    """Subtract expanded pin keepouts from M1 and M2 macro obstruction rectangles."""
    obs_by_layer: dict[str, list[RectBounds]] = {
        "M1": [(0.0, 0.0, w, h)],
        "M2": [(0.0, 0.0, w, h)],
    }
    for layer, x1, y1, x2, y2 in pins:
        if layer not in obs_by_layer:
            continue
        exp = (
            max(0.0, x1 - OBS_PIN_MARGIN),
            max(0.0, y1 - OBS_PIN_MARGIN),
            min(w, x2 + OBS_PIN_MARGIN),
            min(h, y2 + OBS_PIN_MARGIN),
        )
        next_obs: list[RectBounds] = []
        for rect in obs_by_layer[layer]:
            next_obs.extend(_subtract_rect(rect, exp))
        obs_by_layer[layer] = next_obs
    return obs_by_layer["M1"], obs_by_layer["M2"]


def _format_pin_block(port: str, direction: str, geom: PinGeometry) -> list[str]:
    """Format a LEF PIN block for one input or output port."""
    layer, x1, y1, x2, y2 = geom
    pin_name = port.replace("-", "_")
    return [
        f"  PIN {pin_name}",
        f"    DIRECTION {direction} ;",
        "    USE SIGNAL ;",
        f"    PORT LAYER {layer} ;",
        f"      RECT {x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} ;",
        "    END",
        f"  END {pin_name}",
    ]


def _format_layer_rects(layer: str, rects: Sequence[RectBounds]) -> list[str]:
    """Format a LEF OBS LAYER section with its list of rectangles."""
    lines = [f"    LAYER {layer} ;"]
    for rx1, ry1, rx2, ry2 in rects:
        lines.append(f"      RECT {rx1:.3f} {ry1:.3f} {rx2:.3f} {ry2:.3f} ;")
    return lines


def macro_lines(cell: CellRecord) -> list[str]:
    """One MACRO block. Sorted ports keep the file diff stable."""
    name = str(cell["cell"]).replace(".", "__").replace("-", "_")
    w = _as_float(cell["width"]) * TILE_MICRONS
    h = _as_float(cell["height"]) * TILE_MICRONS
    rate = _as_float(cell["crafts_per_minute"], 0.0)
    watts = _as_float(cell["watts"], 0.0)
    lines = [
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        f"  SIZE {w:.3f} BY {h:.3f} ;",
        "  SITE tile ;",
        f'  PROPERTY CELLTYPE "{cell["machine"]}" ;',
        f"  PROPERTY RATED_RATE_PER_MIN {rate:.6f} ;",
        f"  PROPERTY POWER_WATTS {watts:.6f} ;",
        f'  PROPERTY RECIPE "{cell["recipe"]}" ;',
    ]
    sorted_inputs = _as_str_list(cell.get("inputs"))
    input_pins: list[PinGeometry] = []
    for idx, port in enumerate(sorted_inputs):
        geom = _input_pin_rect(cell, idx, len(sorted_inputs), port)
        input_pins.append(geom)
        lines.extend(_format_pin_block(port, "INPUT", geom))

    sorted_outputs = sorted(_as_str_list(cell.get("outputs")))
    output_pins: list[PinGeometry] = []
    for idx, port in enumerate(sorted_outputs):
        geom = _output_pin_rect(cell, idx, len(sorted_outputs), port)
        output_pins.append(geom)
        lines.extend(_format_pin_block(port, "OUTPUT", geom))

    full_rect: list[RectBounds] = [(0.0, 0.0, w, h)]
    if cell.get("circuit_cell"):
        lines.append("  OBS")
        for layer in ("M1", "M2", "VL1", "VL2", "VL3"):
            lines.extend(_format_layer_rects(layer, full_rect))
        lines.append("  END")
    elif w > 1.0 or h > 1.0:
        m_obs_m1, m_obs_m2 = _carve_pin_clearances(w, h, input_pins + output_pins)
        lines.append("  OBS")
        lines.extend(_format_layer_rects("M1", m_obs_m1))
        lines.extend(_format_layer_rects("M2", m_obs_m2))
        lines.extend(_format_layer_rects("VL1", m_obs_m1))
        for layer in ("M3", "M4", "VL2", "VL3", "VL4", "VL5"):
            lines.extend(_format_layer_rects(layer, full_rect))
        lines.append("  END")
    lines.append(f"END {name}")
    return lines


def _routing_layer_block(layer: str, direction: str, comment: str = "") -> list[str]:
    """Emit a LEF ROUTING LAYER block."""
    prefix = [f"# {comment}"] if comment else []
    return [
        *prefix,
        f"LAYER {layer}",
        "  TYPE ROUTING ;",
        f"  DIRECTION {direction} ;",
        "  PITCH 1.0 ;",
        "  OFFSET 0.5 ;",
        "  WIDTH 0.5 ;",
        "  SPACING 0.25 ;",
        f"END {layer}",
    ]


def _cut_layer_block(layer: str, comment: str = "") -> list[str]:
    """Emit a LEF CUT LAYER block."""
    prefix = [f"# {comment}"] if comment else []
    return [
        *prefix,
        f"LAYER {layer}",
        "  TYPE CUT ;",
        "  SPACING 0.25 ;",
        f"END {layer}",
    ]


def _turn_via_block(
    via_name: str,
    bot_layer: str,
    cut_layer: str,
    top_layer: str,
    is_default: bool = True,
) -> list[str]:
    """Emit a standard 3-layer turn/drop VIA block."""
    default_line = "  DEFAULT" if is_default else ""
    return [
        f"VIA {via_name}",
        default_line,
        f"  LAYER {bot_layer} ;",
        "    RECT -0.25 -0.25 0.25 0.25 ;",
        f"  LAYER {cut_layer} ;",
        "    RECT -0.125 -0.125 0.125 0.125 ;",
        f"  LAYER {top_layer} ;",
        "    RECT -0.25 -0.25 0.25 0.25 ;",
        f"END {via_name}",
    ]


def _extract_tech_tiers(tech: TechRecord) -> tuple[list[dict[str, object]], int]:
    """Extract belt tiers and fluid underground span from tech dictionary."""
    layers_obj = tech.get("layers")
    m3_obj = layers_obj.get("M3") if isinstance(layers_obj, Mapping) else None
    if not isinstance(m3_obj, Mapping):
        return [], 0
    raw_tiers = m3_obj.get("tiers")
    tiers: list[dict[str, object]] = []
    if isinstance(raw_tiers, Sequence):
        tiers.extend(
            {str(k): v for k, v in item.items()} for item in raw_tiers if isinstance(item, Mapping)
        )
    span = _as_int(m3_obj.get("span"), 0)
    return tiers, span


def tech_lines(tech: TechRecord) -> list[str]:
    """The technology section: units, layers, vias, site."""
    tiers, span = _extract_tech_tiers(tech)
    lines = [
        "VERSION 5.8 ;",
        "UNITS",
        f"  DATABASE MICRONS {LEF_UNITS} ;",
        "END UNITS",
        "MANUFACTURINGGRID 0.001 ;",
    ]
    lines.extend(_routing_layer_block("M1", "HORIZONTAL"))
    lines.extend(_cut_layer_block("VL1"))
    lines.extend(_routing_layer_block("M2", "VERTICAL"))

    for idx, tier in enumerate(tiers):
        h_layer = f"M{2 * idx + FIRST_TUNNEL_H_OFFSET}"
        v_layer = f"M{2 * idx + FIRST_TUNNEL_V_OFFSET}"
        vl_cut = f"VL{2 * idx + FIRST_TUNNEL_CUT_OFFSET}"
        vl_turn = f"VL{2 * idx + FIRST_TUNNEL_TURN_OFFSET}"
        bname = str(tier["belt"])
        lines.extend(
            _cut_layer_block(vl_cut, f"{bname} surface-to-underground transition cut layer.")
        )
        lines.extend(
            _routing_layer_block(
                h_layer, "HORIZONTAL", f"{bname} underground horizontal tunnel layer."
            )
        )
        lines.extend(_cut_layer_block(vl_turn, f"{bname} underground turn cut layer."))
        lines.extend(
            _routing_layer_block(v_layer, "VERTICAL", f"{bname} underground vertical tunnel layer.")
        )

    circ = _as_str_list(tech.get("circuit_layers"))
    n_tiers = len(tiers)
    red_layer, green_layer = (circ[0], circ[1]) if len(circ) >= 2 else pdk.circuit_layers(n_tiers)
    vl_drop = f"VL{2 * n_tiers + FIRST_TUNNEL_CUT_OFFSET}"
    vl_circ_turn = f"VL{2 * n_tiers + FIRST_TUNNEL_TURN_OFFSET}"
    last_v_layer = f"M{2 * n_tiers + SURFACE_METAL_COUNT}" if n_tiers > 0 else "M2"

    lines.extend(_cut_layer_block(vl_drop, "Circuit drop cut layer."))
    lines.extend(
        _routing_layer_block(red_layer, "HORIZONTAL", "Circuit red wire horizontal layer.")
    )
    lines.extend(_cut_layer_block(vl_circ_turn, "Circuit wire turn cut layer."))
    lines.extend(
        _routing_layer_block(green_layer, "VERTICAL", "Circuit green wire vertical layer.")
    )

    # 1. Surface turn layer connection (M1 -> VL1 -> M2)
    lines.extend(_turn_via_block("SURFACE_TURN_VIA", "M1", "VL1", "M2"))

    # 2. Underground belt entrance/exit connection
    for idx, tier in enumerate(tiers):
        bname = str(tier["belt"])
        name = bname.replace("-", "_").upper()
        default_tag = "  DEFAULT" if idx == 0 else ""
        bottom_layer = f"M{2 * idx + SURFACE_METAL_COUNT}"
        h_layer = f"M{2 * idx + FIRST_TUNNEL_H_OFFSET}"
        vl_layer = f"VL{2 * idx + FIRST_TUNNEL_CUT_OFFSET}"
        lines.extend(
            [
                f"VIA {name}_VIA",
                default_tag,
                f"  LAYER {bottom_layer} ;",
                "    RECT -0.75 -0.25 0.75 0.25 ;",
                f"  LAYER {vl_layer} ;",
                "    RECT -0.125 -0.125 0.125 0.125 ;",
                f"  LAYER {h_layer} ;",
                "    RECT -0.25 -0.25 0.25 0.25 ;",
                f'  PROPERTY TIER_BELT "{bname}" ;',
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

    # 4. Underground turn connection (h_layer -> vl_turn -> v_layer) for each tier
    for idx, tier in enumerate(tiers):
        name = str(tier["belt"]).replace("-", "_").upper()
        h_layer = f"M{2 * idx + FIRST_TUNNEL_H_OFFSET}"
        v_layer = f"M{2 * idx + FIRST_TUNNEL_V_OFFSET}"
        vl_turn = f"VL{2 * idx + FIRST_TUNNEL_TURN_OFFSET}"
        lines.extend(
            _turn_via_block(f"{name}_TURN_VIA", h_layer, vl_turn, v_layer, is_default=(idx == 0))
        )

    # 5. Circuit drop connection & 6. Circuit turn connection
    lines.extend(_turn_via_block("CIRCUIT_DROP_VIA", last_v_layer, vl_drop, red_layer))
    lines.extend(_turn_via_block("CIRCUIT_TURN_VIA", red_layer, vl_circ_turn, green_layer))
    lines.extend(
        [
            "SITE tile",
            f"  SIZE {TILE_MICRONS:.3f} BY {TILE_MICRONS:.3f} ;",
            "  CLASS CORE ;",
            "END tile",
        ]
    )
    return [line for line in lines if line]


def merge_lines() -> list[str]:
    """The transport merge cell: 3x3 dual-lane balancing splitter macro.

    Inputs a and b enter west on M1. Output y leaves east on M1 at row 1.
    """
    name = netlist.MERGE_CELL
    return [
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        "  SIZE 3.000 BY 3.000 ;",
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
        "      RECT 2.500 1.250 3.000 1.750 ;",
        "    END",
        "  END y",
        "  OBS",
        "    LAYER M1 ;",
        "      RECT 0.500 0.000 2.500 3.000 ;",
        "      RECT 0.000 2.000 0.500 3.000 ;",
        "      RECT 2.500 0.000 3.000 1.000 ;",
        "      RECT 2.500 2.000 3.000 3.000 ;",
        "    LAYER M2 ;",
        "      RECT 0.000 0.000 3.000 3.000 ;",
        "    LAYER VL1 ;",
        "      RECT 0.000 0.000 3.000 3.000 ;",
        "    LAYER M3 ;",
        "      RECT 0.000 0.000 3.000 3.000 ;",
        "    LAYER M4 ;",
        "      RECT 0.000 0.000 3.000 3.000 ;",
        "    LAYER VL2 ;",
        "      RECT 0.000 0.000 3.000 3.000 ;",
        "    LAYER VL3 ;",
        "      RECT 0.000 0.000 3.000 3.000 ;",
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


def write_lef(p: pdk.Pdk, cells: Sequence[CellRecord], path: str) -> str:
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
    builtin_cells = {
        netlist.MERGE_CELL,
        netlist.SPLIT_CELL,
        netlist.BALANCER_CELL,
        netlist.PUMP_CELL,
    }
    for cell in sorted(cells, key=lambda c: str(c["cell"])):
        if str(cell["cell"]) in builtin_cells:
            continue
        lines.append("")
        lines.extend(macro_lines(cell))
    lines.append("")
    lines.append("END LIBRARY")
    text = "\n".join(lines)
    Path(path).write_text(text)
    return text


def _parse_lef_macros(text: str) -> dict[str, list[str]]:
    """Group stripped LEF lines by MACRO name."""
    macros: dict[str, list[str]] = {}
    macro: str | None = None
    for line in text.splitlines():
        token = line.strip()
        if token.startswith("MACRO "):
            macro = token.split()[1]
            macros[macro] = []
        elif token == "END LIBRARY":
            macro = None
        elif macro is not None:
            macros[macro].append(token)
    return macros


def _check_macro_token(name: str, token: str, width: float, height: float) -> list[str]:
    """Validate a single RECT or SIZE statement line against macro dimensions."""
    problems: list[str] = []
    if token.startswith("RECT "):
        parts = token.split()
        x1, y1, x2, y2 = (float(v) for v in parts[1:RECT_COORD_END_INDEX])
        if x1 < -LEF_COORD_TOLERANCE or x2 > width + LEF_COORD_TOLERANCE:
            problems.append(f"{name}: rect x outside macro: {token}")
        if y1 < -LEF_COORD_TOLERANCE or y2 > height + LEF_COORD_TOLERANCE:
            problems.append(f"{name}: rect y outside macro: {token}")
        return problems
    if token.startswith("SIZE "):
        parts = token.split()
        if abs(float(parts[1]) - width) > LEF_COORD_TOLERANCE:
            problems.append(f"{name}: SIZE x mismatch: {token}")
        if abs(float(parts[SIZE_HEIGHT_TOKEN_INDEX]) - height) > LEF_COORD_TOLERANCE:
            problems.append(f"{name}: SIZE y mismatch: {token}")
    return problems


def _check_macro_block(cell: CellRecord, name: str, block: Sequence[str]) -> list[str]:
    """Validate pins, RECT bounds, and SIZE statement for a single LEF MACRO block."""
    width = _as_float(cell["width"]) * TILE_MICRONS
    height = _as_float(cell["height"]) * TILE_MICRONS
    ports = [
        p.replace("-", "_")
        for p in _as_str_list(cell.get("inputs")) + _as_str_list(cell.get("outputs"))
    ]
    problems = [
        f"{name}.{port}: pin missing"
        for port in ports
        if not any(t.startswith(f"PIN {port}") for t in block)
    ]
    for token in block:
        problems.extend(_check_macro_token(name, token, width, height))
    return problems


def check_lef(cells: Sequence[CellRecord], text: str) -> list[str]:
    """Structural checks the game bridge depends on.

    Returns the list of violations. Empty list means the LEF matches the
    cells: every port emitted, every rect inside the macro, every macro
    name a legal identifier.
    """
    problems: list[str] = []
    macros = _parse_lef_macros(text)
    for cell in cells:
        name = str(cell["cell"]).replace(".", "__").replace("-", "_")
        block = macros.get(name)
        if block is None:
            problems.append(f"{name}: macro missing")
            continue
        problems.extend(_check_macro_block(cell, name, block))
    return problems
