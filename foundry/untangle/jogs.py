"""Route straightening and jog elimination for DEF layouts."""

from __future__ import annotations

import collections
from typing import Any

try:
    from foundry.def_parser import merge_collinear_wires
    from foundry.untangle.spatial import (
        SpatialWireIndex,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )
except ImportError:
    from def_parser import merge_collinear_wires

    from untangle.spatial import (
        SpatialWireIndex,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )

JsonDict = dict[str, Any]
Coord = tuple[int, int]
Box = tuple[int, int, int, int]

DEFAULT_UNITS = 1000
BELT_SHORT_DIM = 1000
BELT_LONG_DIM = 2000
DEFAULT_COMP_DIM = 5000
MAX_RELAXATION_PASSES = 5
MIN_STAIRCASE_WIRES = 4
DEGREE_TWO = 2
EAST_TURN_STEPS = (1, 2)
WEST_TURN_STEPS = (2, 1)
FORWARD_SPLITTER_STEPS = (2, 3, 4)
SPLITTER_HEIGHT_TILES = 2
SPLITTER_BLOCK_TILES = 5
PIN_EXIT_SEARCH_TILES = 2
BELT_MACROS = ("belt_split2", "belt_merge2", "belt_balancer2")
HORIZONTAL_ORIENTS = ("W", "E")
PLANAR_LAYERS = ("M1", "M2")


def _compute_endpoint_degrees(net: JsonDict) -> dict[Coord, int]:
    """Count the number of wire segment endpoints at each (x, y) coordinate."""
    pt_counts: dict[Coord, int] = collections.defaultdict(int)
    wires = net.get("wires", [])
    for w in wires:
        pt_counts[(w["x1"], w["y1"])] += 1
        pt_counts[(w["x2"], w["y2"])] += 1
    return pt_counts


def _find_planar_terminal_pair(net: JsonDict) -> tuple[Coord, Coord] | None:
    """Return the two degree-1 endpoints if the net is a multi-segment planar M1/M2 route."""
    wires = net.get("wires", [])
    vias = net.get("vias", [])
    if len(wires) <= 1:
        return None
    if any(v.get("via") != "SURFACE_TURN_VIA" for v in vias):
        return None
    if any(w.get("layer") not in PLANAR_LAYERS for w in wires):
        return None
    pt_counts = _compute_endpoint_degrees(net)
    degree_one = [pt for pt, count in pt_counts.items() if count == 1]
    if len(degree_one) != DEGREE_TWO:
        return None
    return degree_one[0], degree_one[1]


def _collinear_comp_dims(c: JsonDict) -> tuple[int, int]:
    """Return approximate (width, height) of a component for collinear track checks."""
    macro = c.get("macro", "")
    orient = c.get("orient", "N")
    if macro in BELT_MACROS:
        if orient in HORIZONTAL_ORIENTS:
            return BELT_LONG_DIM, BELT_SHORT_DIM
        return BELT_SHORT_DIM, BELT_LONG_DIM
    return DEFAULT_COMP_DIM, DEFAULT_COMP_DIM


def _comps_block_h_track(
    def_data: JsonDict, connected_insts: set[str], y_track: int, min_x: int, max_x: int
) -> bool:
    """Return True if any unconnected component overlaps horizontal track (min_x..max_x, y_track)."""
    components = def_data.get("components", [])
    for c in components:
        if c.get("name") in connected_insts:
            continue
        cw, ch = _collinear_comp_dims(c)
        cx, cy = c.get("x", 0), c.get("y", 0)
        if cy <= y_track <= cy + ch and not (cx + cw <= min_x or cx >= max_x):
            return True
    return False


def _comps_block_v_track(
    def_data: JsonDict, connected_insts: set[str], x_track: int, min_y: int, max_y: int
) -> bool:
    """Return True if any unconnected component overlaps vertical track (x_track, min_y..max_y)."""
    components = def_data.get("components", [])
    for c in components:
        if c.get("name") in connected_insts:
            continue
        cw, ch = _collinear_comp_dims(c)
        cx, cy = c.get("x", 0), c.get("y", 0)
        if cx <= x_track <= cx + cw and not (cy + ch <= min_y or cy >= max_y):
            return True
    return False


def _wire_blocks_h_track(ow: JsonDict, y_track: int, min_x: int, max_x: int) -> bool:
    """Return True if wire ow blocks horizontal M1 track at y_track between min_x and max_x."""
    layer = ow.get("layer")
    if layer == "M1" and ow["y1"] == y_track and ow["y2"] == y_track:
        return max(min(ow["x1"], ow["x2"]), min_x) < min(max(ow["x1"], ow["x2"]), max_x)
    if layer == "M2" and ow["x1"] == ow["x2"] and min_x < ow["x1"] < max_x:
        return y_track in (min(ow["y1"], ow["y2"]), max(ow["y1"], ow["y2"]))
    return False


def _wire_blocks_v_track(ow: JsonDict, x_track: int, min_y: int, max_y: int) -> bool:
    """Return True if wire ow blocks vertical M2 track at x_track between min_y and max_y."""
    layer = ow.get("layer")
    if layer == "M2" and ow["x1"] == x_track and ow["x2"] == x_track:
        return max(min(ow["y1"], ow["y2"]), min_y) < min(max(ow["y1"], ow["y2"]), max_y)
    if layer == "M1" and ow["y1"] == ow["y2"] and min_y < ow["y1"] < max_y:
        return x_track in (min(ow["x1"], ow["x2"]), max(ow["x1"], ow["x2"]))
    return False


def _nets_block_h_track(
    def_data: JsonDict, net: JsonDict, y_track: int, min_x: int, max_x: int
) -> bool:
    """Return True if any other net's wire or via blocks horizontal track (min_x..max_x, y_track)."""
    all_nets = def_data.get("nets", [])
    for other_net in all_nets:
        if other_net is net:
            continue
        other_wires = other_net.get("wires", [])
        if any(_wire_blocks_h_track(ow, y_track, min_x, max_x) for ow in other_wires):
            return True
        other_vias = other_net.get("vias", [])
        if any(ov.get("y") == y_track and min_x < ov.get("x", 0) < max_x for ov in other_vias):
            return True
    return False


def _nets_block_v_track(
    def_data: JsonDict, net: JsonDict, x_track: int, min_y: int, max_y: int
) -> bool:
    """Return True if any other net's wire or via blocks vertical track (x_track, min_y..max_y)."""
    all_nets = def_data.get("nets", [])
    for other_net in all_nets:
        if other_net is net:
            continue
        other_wires = other_net.get("wires", [])
        if any(_wire_blocks_v_track(ow, x_track, min_y, max_y) for ow in other_wires):
            return True
        other_vias = other_net.get("vias", [])
        if any(ov.get("x") == x_track and min_y < ov.get("y", 0) < max_y for ov in other_vias):
            return True
    return False


def _try_straighten_h_net(
    def_data: JsonDict, net: JsonDict, pt1: Coord, pt2: Coord, connected_insts: set[str]
) -> bool:
    """Collapse a horizontally collinear net into a single M1 segment if unblocked."""
    wires = net.get("wires", [])
    if pt1[1] != pt2[1] or pt1[0] == pt2[0] or all(w["y1"] == w["y2"] for w in wires):
        return False
    y_track = pt1[1]
    min_x, max_x = min(pt1[0], pt2[0]), max(pt1[0], pt2[0])
    if _comps_block_h_track(def_data, connected_insts, y_track, min_x, max_x):
        return False
    if _nets_block_h_track(def_data, net, y_track, min_x, max_x):
        return False
    net["wires"] = [{"layer": "M1", "x1": min_x, "y1": y_track, "x2": max_x, "y2": y_track}]
    net["vias"] = []
    return True


def _try_straighten_v_net(
    def_data: JsonDict, net: JsonDict, pt1: Coord, pt2: Coord, connected_insts: set[str]
) -> bool:
    """Collapse a vertically collinear net into a single M2 segment if unblocked."""
    wires = net.get("wires", [])
    if pt1[0] != pt2[0] or pt1[1] == pt2[1] or all(w["x1"] == w["x2"] for w in wires):
        return False
    x_track = pt1[0]
    min_y, max_y = min(pt1[1], pt2[1]), max(pt1[1], pt2[1])
    if _comps_block_v_track(def_data, connected_insts, x_track, min_y, max_y):
        return False
    if _nets_block_v_track(def_data, net, x_track, min_y, max_y):
        return False
    net["wires"] = [{"layer": "M2", "x1": x_track, "y1": min_y, "x2": x_track, "y2": max_y}]
    net["vias"] = []
    return True


def straighten_collinear_routes(def_data: JsonDict) -> None:
    """Straighten unblocked collinear connections that router detoured with U-jogs.

    When two connected pins lie on the same coordinate axis (for example horizontal track),
    and the direct planar track between them is clear of components and other net wires,
    collapse any U-shaped detours into a direct straight wire.
    """
    nets = def_data.get("nets", [])
    for net in nets:
        pair = _find_planar_terminal_pair(net)
        if pair is None:
            continue
        pt1, pt2 = pair
        conns = net.get("connections", [])
        connected_insts = {inst for inst, _pin in conns}
        if _try_straighten_h_net(def_data, net, pt1, pt2, connected_insts):
            continue
        _try_straighten_v_net(def_data, net, pt1, pt2, connected_insts)


def _point_in_boxes(x: int, y: int, wire_index: SpatialWireIndex) -> bool:
    """Return True if point (x, y) lies inside or on the boundary of any component box."""
    boxes = wire_index.comp_boxes
    return any(cx <= x <= cx + cw and cy <= y <= cy + ch for cx, cy, cw, ch in boxes)


def _other_wires_touch(net: JsonDict, target: JsonDict, excluded: tuple[JsonDict, ...]) -> bool:
    """Return True if any wire in net outside excluded touches target."""
    excluded_ids = {id(w) for w in excluded}
    wires = net.get("wires", [])
    return any(id(w) not in excluded_ids and wires_touch(w, target) for w in wires)


def _set_wire_coord(w: JsonDict, key1: str, key2: str, old_val: int, new_val: int) -> None:
    """Replace the endpoint coordinate matching old_val with new_val."""
    if w[key1] == old_val:
        w[key1] = new_val
    else:
        w[key2] = new_val


def _is_h_wire(w: JsonDict) -> bool:
    """Return True if w is a horizontal M1 segment."""
    return w.get("layer") == "M1" and w["y1"] == w["y2"]


def _is_v_wire(w: JsonDict) -> bool:
    """Return True if w is a vertical M2 segment."""
    return w.get("layer") == "M2" and w["x1"] == w["x2"]


def _wire_has_x(w: JsonDict, x: int) -> bool:
    """Return True if either x endpoint of w equals x."""
    return w["x1"] == x or w["x2"] == x


def _wire_has_y(w: JsonDict, y: int) -> bool:
    """Return True if either y endpoint of w equals y."""
    return w["y1"] == y or w["y2"] == y


def _other_x(w: JsonDict, x: int) -> int:
    """Return the x endpoint of w opposite to x."""
    return w["x1"] if w["x2"] == x else w["x2"]


def _other_y(w: JsonDict, y: int) -> int:
    """Return the y endpoint of w opposite to y."""
    return w["y1"] if w["y2"] == y else w["y2"]


def _is_strictly_ordered(a: int, b: int, c: int) -> bool:
    """Return True if (a < b < c) or (a > b > c)."""
    return (a < b < c) or (a > b > c)


def _is_same_side(a: int, b: int, pivot: int) -> bool:
    """Return True if a and b lie on the same side of pivot."""
    return (a < pivot and b < pivot) or (a > pivot and b > pivot)


def _apply_h_staircase_a(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    w3: JsonDict,
    coords: tuple[int, int, int, int],
    w3_covers_both: bool,
) -> None:
    """Apply Option A horizontal staircase collapse onto (x2, y1)."""
    x1, x2, y1, y2 = coords
    wires = net.get("wires", [])
    vias = net.get("vias", [])
    _set_wire_coord(w1, "x1", "x2", x1, x2)
    if not w3_covers_both:
        _set_wire_coord(w3, "y1", "y2", y2, y1)
    wires.remove(wj)
    wires.remove(w2)
    new_vias = [
        v
        for v in vias
        if not (
            (v.get("x") == x1 and v.get("y") in (y1, y2)) or (v.get("x") == x2 and v.get("y") == y2)
        )
    ]
    new_vias.append({"layer": "M1", "x": x2, "y": y1, "via": "SURFACE_TURN_VIA"})
    net["vias"] = new_vias
    net["wires"] = merge_collinear_wires(wires, new_vias)


def _apply_h_staircase_b(
    net: JsonDict,
    w0: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    coords: tuple[int, int, int, int],
    w0_covers_both: bool,
) -> None:
    """Apply Option B horizontal staircase collapse onto (xa, y2)."""
    xa, x1, y1, y2 = coords
    wires = net.get("wires", [])
    vias = net.get("vias", [])
    if not w0_covers_both:
        _set_wire_coord(w0, "y1", "y2", y1, y2)
    _set_wire_coord(w2, "x1", "x2", x1, xa)
    wires.remove(wj)
    wires.remove(w1)
    new_vias = [
        v
        for v in vias
        if not (
            (v.get("x") == x1 and v.get("y") in (y1, y2)) or (v.get("x") == xa and v.get("y") == y1)
        )
    ]
    new_vias.append({"layer": "M2", "x": xa, "y": y2, "via": "SURFACE_TURN_VIA"})
    net["vias"] = new_vias
    net["wires"] = merge_collinear_wires(wires, new_vias)


def _is_valid_h_w3_staircase(
    w3: JsonDict, x2: int, y1: int, y2: int, is_fluid: bool
) -> tuple[bool, bool]:
    """Check whether w3 is a valid vertical continuation at x2 for horizontal staircase Option A."""
    if not _is_v_wire(w3) or w3["x1"] != x2:
        return False, False
    covers_both = (
        is_fluid
        and min(w3["y1"], w3["y2"]) <= min(y1, y2)
        and max(w3["y1"], w3["y2"]) >= max(y1, y2)
    )
    if covers_both:
        return True, True
    if not _wire_has_y(w3, y2):
        return False, False
    return _is_strictly_ordered(y1, y2, _other_y(w3, y2)), False


def _try_h_staircase_option_a(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    excluded_idxs: set[int],
    coords: tuple[int, int, int, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Try collapsing horizontal staircase via Option A (extend w1 to x2 at y1)."""
    x1, x2, y1, y2 = coords
    wires = net.get("wires", [])
    for m, w3 in enumerate(wires):
        if m in excluded_idxs:
            continue
        ok, covers_both = _is_valid_h_w3_staircase(w3, x2, y1, y2, is_fluid)
        if not ok:
            continue
        if _other_wires_touch(net, wj, (wj, w1, w2)) or _other_wires_touch(net, w2, (wj, w2, w3)):
            continue
        if wire_index.is_h_clear_detour(y1, x1, x2, net) and (
            covers_both or wire_index.is_v_clear_detour(x2, y1, y2, net)
        ):
            _apply_h_staircase_a(net, w1, wj, w2, w3, coords, covers_both)
            return True
    return False


def _is_valid_h_w0_staircase(
    w0: JsonDict, xa: int, y1: int, y2: int, is_fluid: bool
) -> tuple[bool, bool]:
    """Check whether w0 is a valid vertical continuation at xa for horizontal staircase Option B."""
    if not _is_v_wire(w0) or w0["x1"] != xa:
        return False, False
    covers_both = (
        is_fluid
        and min(w0["y1"], w0["y2"]) <= min(y1, y2)
        and max(w0["y1"], w0["y2"]) >= max(y1, y2)
    )
    if covers_both:
        return True, True
    if not _wire_has_y(w0, y1):
        return False, False
    return _is_strictly_ordered(_other_y(w0, y1), y1, y2), False


def _can_apply_h_option_b(
    net: JsonDict,
    w0: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    coords: tuple[int, int, int, int],
    covers_both: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Return True if horizontal staircase Option B is free of wire touches and obstacles."""
    xa, x1, y1, y2 = coords
    if _other_wires_touch(net, wj, (wj, w1, w2)) or _other_wires_touch(net, w1, (wj, w1, w0)):
        return False
    v_ok = covers_both or wire_index.is_v_clear_detour(xa, y1, y2, net)
    return v_ok and wire_index.is_h_clear_detour(y2, xa, x1, net)


def _try_h_staircase_option_b(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    excluded_idxs: set[int],
    coords: tuple[int, int, int, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Try collapsing horizontal staircase via Option B (extend w0 at xa to y2)."""
    xa, _x1, y1, y2 = coords
    if _point_in_boxes(xa, y1, wire_index):
        return False
    wires = net.get("wires", [])
    for m, w0 in enumerate(wires):
        if m in excluded_idxs:
            continue
        ok, covers_both = _is_valid_h_w0_staircase(w0, xa, y1, y2, is_fluid)
        if ok and _can_apply_h_option_b(net, w0, w1, wj, w2, coords, covers_both, wire_index):
            _apply_h_staircase_b(net, w0, w1, wj, w2, coords, covers_both)
            return True
    return False


def _match_h_staircase_w2(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    ij_set: set[int],
    coords_prefix: tuple[int, int, int, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Find candidate w2 for horizontal staircase (w1, wj) and attempt Option A or Option B."""
    xa, x1, y1, y2 = coords_prefix
    wires = net.get("wires", [])
    for k, w2 in enumerate(wires):
        if k in ij_set or not _is_h_wire(w2) or w2["y1"] != y2 or not _wire_has_x(w2, x1):
            continue
        x2 = _other_x(w2, x1)
        if not _is_strictly_ordered(xa, x1, x2) or _point_in_boxes(x2, y2, wire_index):
            continue
        ijk = ij_set | {k}
        if _try_h_staircase_option_a(net, w1, wj, w2, ijk, (x1, x2, y1, y2), is_fluid, wire_index):
            return True
        if _try_h_staircase_option_b(net, w1, wj, w2, ijk, (xa, x1, y1, y2), is_fluid, wire_index):
            return True
    return False


def _match_h_staircase_wj(
    net: JsonDict,
    w1: JsonDict,
    i: int,
    deg: dict[Coord, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Find candidate vertical jog wj connected to horizontal wire w1."""
    y1 = w1["y1"]
    wires = net.get("wires", [])
    for j, wj in enumerate(wires):
        if i == j or not _is_v_wire(wj):
            continue
        x1 = wj["x1"]
        if not (_wire_has_x(w1, x1) and _wire_has_y(wj, y1)):
            continue
        y2 = _other_y(wj, y1)
        if deg[(x1, y1)] != DEGREE_TWO or deg[(x1, y2)] != DEGREE_TWO:
            continue
        xa = _other_x(w1, x1)
        if _match_h_staircase_w2(net, w1, wj, {i, j}, (xa, x1, y1, y2), is_fluid, wire_index):
            return True
    return False


def _try_collapse_h_staircase(
    net: JsonDict, deg: dict[Coord, int], is_fluid: bool, wire_index: SpatialWireIndex
) -> bool:
    """Scan net wires for a collapsible horizontal staircase jog (M1 -> M2 -> M1 -> M2)."""
    wires = net.get("wires", [])
    for i, w1 in enumerate(wires):
        if _is_h_wire(w1) and _match_h_staircase_wj(net, w1, i, deg, is_fluid, wire_index):
            return True
    return False


def _apply_v_staircase_a(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    w3: JsonDict,
    coords: tuple[int, int, int, int],
    w3_covers_both: bool,
) -> None:
    """Apply Option A vertical staircase collapse onto (x1, y2)."""
    x1, x2, y1, y2 = coords
    wires = net.get("wires", [])
    vias = net.get("vias", [])
    _set_wire_coord(w1, "y1", "y2", y1, y2)
    if not w3_covers_both:
        _set_wire_coord(w3, "x1", "x2", x2, x1)
    wires.remove(wj)
    wires.remove(w2)
    new_vias = [
        v
        for v in vias
        if not (
            (v.get("y") == y1 and v.get("x") in (x1, x2)) or (v.get("y") == y2 and v.get("x") == x2)
        )
    ]
    new_vias.append({"layer": "M2", "x": x1, "y": y2, "via": "SURFACE_TURN_VIA"})
    net["vias"] = new_vias
    net["wires"] = merge_collinear_wires(wires, new_vias)


def _apply_v_staircase_b(
    net: JsonDict,
    w0: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    coords: tuple[int, int, int, int],
    w0_covers_both: bool,
) -> None:
    """Apply Option B vertical staircase collapse onto (x2, ya)."""
    x1, x2, ya, y1 = coords
    wires = net.get("wires", [])
    vias = net.get("vias", [])
    if not w0_covers_both:
        _set_wire_coord(w0, "x1", "x2", x1, x2)
    _set_wire_coord(w2, "y1", "y2", y1, ya)
    wires.remove(wj)
    wires.remove(w1)
    new_vias = [
        v
        for v in vias
        if not (
            (v.get("y") == y1 and v.get("x") in (x1, x2)) or (v.get("y") == ya and v.get("x") == x1)
        )
    ]
    new_vias.append({"layer": "M1", "x": x2, "y": ya, "via": "SURFACE_TURN_VIA"})
    net["vias"] = new_vias
    net["wires"] = merge_collinear_wires(wires, new_vias)


def _is_valid_v_w3_staircase(
    w3: JsonDict, x1: int, x2: int, y2: int, is_fluid: bool
) -> tuple[bool, bool]:
    """Check whether w3 is a valid horizontal continuation at y2 for vertical staircase Option A."""
    if not _is_h_wire(w3) or w3["y1"] != y2:
        return False, False
    covers_both = (
        is_fluid
        and min(w3["x1"], w3["x2"]) <= min(x1, x2)
        and max(w3["x1"], w3["x2"]) >= max(x1, x2)
    )
    if covers_both:
        return True, True
    if not _wire_has_x(w3, x2):
        return False, False
    return _is_strictly_ordered(x1, x2, _other_x(w3, x2)), False


def _try_v_staircase_option_a(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    excluded_idxs: set[int],
    coords: tuple[int, int, int, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Try collapsing vertical staircase via Option A (extend w1 to y2 at x1)."""
    x1, x2, y1, y2 = coords
    wires = net.get("wires", [])
    for m, w3 in enumerate(wires):
        if m in excluded_idxs:
            continue
        ok, covers_both = _is_valid_v_w3_staircase(w3, x1, x2, y2, is_fluid)
        if not ok:
            continue
        if _other_wires_touch(net, wj, (wj, w1, w2)) or _other_wires_touch(net, w2, (wj, w2, w3)):
            continue
        if wire_index.is_v_clear_detour(x1, y1, y2, net) and (
            covers_both or wire_index.is_h_clear_detour(y2, x1, x2, net)
        ):
            _apply_v_staircase_a(net, w1, wj, w2, w3, coords, covers_both)
            return True
    return False


def _is_valid_v_w0_staircase(
    w0: JsonDict, x1: int, x2: int, ya: int, is_fluid: bool
) -> tuple[bool, bool]:
    """Check whether w0 is a valid horizontal continuation at ya for vertical staircase Option B."""
    if not _is_h_wire(w0) or w0["y1"] != ya:
        return False, False
    covers_both = (
        is_fluid
        and min(w0["x1"], w0["x2"]) <= min(x1, x2)
        and max(w0["x1"], w0["x2"]) >= max(x1, x2)
    )
    if covers_both:
        return True, True
    if not _wire_has_x(w0, x1):
        return False, False
    return _is_strictly_ordered(_other_x(w0, x1), x1, x2), False


def _can_apply_v_option_b(
    net: JsonDict,
    w0: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    coords: tuple[int, int, int, int],
    covers_both: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Return True if vertical staircase Option B is free of wire touches and obstacles."""
    x1, x2, ya, y1 = coords
    if _other_wires_touch(net, wj, (wj, w1, w2)) or _other_wires_touch(net, w1, (wj, w1, w0)):
        return False
    h_ok = covers_both or wire_index.is_h_clear_detour(ya, x1, x2, net)
    return h_ok and wire_index.is_v_clear_detour(x2, ya, y1, net)


def _try_v_staircase_option_b(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    excluded_idxs: set[int],
    coords: tuple[int, int, int, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Try collapsing vertical staircase via Option B (extend w0 at ya to x2)."""
    x1, x2, ya, _y1 = coords
    if _point_in_boxes(x1, ya, wire_index):
        return False
    wires = net.get("wires", [])
    for m, w0 in enumerate(wires):
        if m in excluded_idxs:
            continue
        ok, covers_both = _is_valid_v_w0_staircase(w0, x1, x2, ya, is_fluid)
        if ok and _can_apply_v_option_b(net, w0, w1, wj, w2, coords, covers_both, wire_index):
            _apply_v_staircase_b(net, w0, w1, wj, w2, coords, covers_both)
            return True
    return False


def _match_v_staircase_w2(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    ij_set: set[int],
    coords_prefix: tuple[int, int, int, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Find candidate w2 for vertical staircase (w1, wj) and attempt Option A or Option B."""
    x1, x2, ya, y1 = coords_prefix
    wires = net.get("wires", [])
    for k, w2 in enumerate(wires):
        if k in ij_set or not _is_v_wire(w2) or w2["x1"] != x2 or not _wire_has_y(w2, y1):
            continue
        y2 = _other_y(w2, y1)
        if not _is_strictly_ordered(ya, y1, y2) or _point_in_boxes(x2, y2, wire_index):
            continue
        ijk = ij_set | {k}
        if _try_v_staircase_option_a(net, w1, wj, w2, ijk, (x1, x2, y1, y2), is_fluid, wire_index):
            return True
        if _try_v_staircase_option_b(net, w1, wj, w2, ijk, (x1, x2, ya, y1), is_fluid, wire_index):
            return True
    return False


def _match_v_staircase_wj(
    net: JsonDict,
    w1: JsonDict,
    i: int,
    deg: dict[Coord, int],
    is_fluid: bool,
    wire_index: SpatialWireIndex,
) -> bool:
    """Find candidate horizontal jog wj connected to vertical wire w1."""
    x1 = w1["x1"]
    wires = net.get("wires", [])
    for j, wj in enumerate(wires):
        if i == j or not _is_h_wire(wj):
            continue
        y1 = wj["y1"]
        if not (_wire_has_y(w1, y1) and _wire_has_x(wj, x1)):
            continue
        x2 = _other_x(wj, x1)
        if deg[(x1, y1)] != DEGREE_TWO or deg[(x2, y1)] != DEGREE_TWO:
            continue
        ya = _other_y(w1, y1)
        if _match_v_staircase_w2(net, w1, wj, {i, j}, (x1, x2, ya, y1), is_fluid, wire_index):
            return True
    return False


def _try_collapse_v_staircase(
    net: JsonDict, deg: dict[Coord, int], is_fluid: bool, wire_index: SpatialWireIndex
) -> bool:
    """Scan net wires for a collapsible vertical staircase jog (M2 -> M1 -> M2 -> M1)."""
    wires = net.get("wires", [])
    for i, w1 in enumerate(wires):
        if _is_v_wire(w1) and _match_v_staircase_wj(net, w1, i, deg, is_fluid, wire_index):
            return True
    return False


def _collapse_net_staircase_once(net: JsonDict, wire_index: SpatialWireIndex) -> bool:
    """Attempt a single horizontal or vertical staircase collapse on net."""
    wires = net.get("wires", [])
    if len(wires) < MIN_STAIRCASE_WIRES:
        return False
    is_fluid = _is_fluid_net(net)
    deg = _compute_endpoint_degrees(net)
    if _try_collapse_h_staircase(net, deg, is_fluid, wire_index):
        return True
    return _try_collapse_v_staircase(net, deg, is_fluid, wire_index)


def eliminate_staircase_jogs(def_data: JsonDict) -> None:
    """Eliminate unblocked intermediate staircase jogs on routing tracks."""
    units = int(def_data.get("units", DEFAULT_UNITS))
    nets = def_data.get("nets", [])
    wire_index = SpatialWireIndex(def_data, units)

    for _pass in range(MAX_RELAXATION_PASSES):
        any_modified = False
        for net in nets:
            if _collapse_net_staircase_once(net, wire_index):
                any_modified = True
        if not any_modified:
            break
        wire_index.rebuild()


def _apply_v_u_detour(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    w3: JsonDict,
    coords: tuple[int, int, int, int],
) -> None:
    """Collapse a vertical U-detour by reconnecting w1 and w3 at (x2, y1)."""
    x1, x2, y1, y2 = coords
    wires = net.get("wires", [])
    vias = net.get("vias", [])
    _set_wire_coord(w1, "x1", "x2", x1, x2)
    _set_wire_coord(w3, "y1", "y2", y2, y1)
    wires.remove(wj)
    wires.remove(w2)
    new_vias = [
        v
        for v in vias
        if not (
            (v.get("x") == x1 and v.get("y") in (y1, y2)) or (v.get("x") == x2 and v.get("y") == y2)
        )
    ]
    if w3["y1"] == w3["y2"]:
        wires.remove(w3)
    else:
        new_vias.append({"layer": "M1", "x": x2, "y": y1, "via": "SURFACE_TURN_VIA"})
    net["vias"] = new_vias
    net["wires"] = merge_collinear_wires(wires, new_vias)


def _is_v_u_detour_w3_clear(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    w3: JsonDict,
    coords: tuple[int, int, int, int],
    wire_index: SpatialWireIndex,
) -> bool:
    """Return True if w3 forms a valid, unblocked vertical U-detour with (w1, wj, w2)."""
    x1, x2, y1, y2 = coords
    if not _is_v_wire(w3) or w3["x1"] != x2 or not _wire_has_y(w3, y2):
        return False
    y3 = _other_y(w3, y2)
    if not _is_same_side(y1, y3, y2):
        return False
    if _other_wires_touch(net, wj, (wj, w1, w2)) or _other_wires_touch(net, w2, (wj, w2, w3)):
        return False
    if not wire_index.is_h_clear_detour(y1, x1, x2, net):
        return False
    w3_extends = not (min(y2, y3) <= y1 <= max(y2, y3))
    return not w3_extends or wire_index.is_v_clear_detour(x2, min(y1, y2), max(y1, y2), net)


def _try_v_u_detour_w3(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    excluded_idxs: set[int],
    coords: tuple[int, int, int, int],
    wire_index: SpatialWireIndex,
) -> bool:
    """Find candidate w3 for a vertical U-detour and collapse if clear."""
    wires = net.get("wires", [])
    for m, w3 in enumerate(wires):
        if m not in excluded_idxs and _is_v_u_detour_w3_clear(
            net, w1, wj, w2, w3, coords, wire_index
        ):
            _apply_v_u_detour(net, w1, wj, w2, w3, coords)
            return True
    return False


def _match_v_u_detour_w2(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    ij_set: set[int],
    coords_prefix: tuple[int, int, int],
    wire_index: SpatialWireIndex,
) -> bool:
    """Find horizontal bridge w2 for vertical U-detour (w1, wj)."""
    x1, y1, y2 = coords_prefix
    wires = net.get("wires", [])
    for k, w2 in enumerate(wires):
        if k in ij_set or not _is_h_wire(w2) or w2["y1"] != y2 or not _wire_has_x(w2, x1):
            continue
        x2 = _other_x(w2, x1)
        if _try_v_u_detour_w3(net, w1, wj, w2, ij_set | {k}, (x1, x2, y1, y2), wire_index):
            return True
    return False


def _match_v_u_detour_wj(
    net: JsonDict, w1: JsonDict, i: int, deg: dict[Coord, int], wire_index: SpatialWireIndex
) -> bool:
    """Find candidate vertical leg wj and bridge w2 for a vertical U-detour."""
    y1 = w1["y1"]
    wires = net.get("wires", [])
    for j, wj in enumerate(wires):
        if i == j or not _is_v_wire(wj):
            continue
        x1 = wj["x1"]
        if not (_wire_has_x(w1, x1) and _wire_has_y(wj, y1)):
            continue
        y2 = _other_y(wj, y1)
        if (deg[(x1, y1)], deg[(x1, y2)]) != (DEGREE_TWO, DEGREE_TWO):
            continue
        if not _point_in_boxes(x1, y2, wire_index) and _match_v_u_detour_w2(
            net, w1, wj, {i, j}, (x1, y1, y2), wire_index
        ):
            return True
    return False


def _try_collapse_v_u_detour(
    net: JsonDict, deg: dict[Coord, int], wire_index: SpatialWireIndex
) -> bool:
    """Scan net wires for a collapsible vertical U-detour (M1 -> M2 -> M1 -> M2)."""
    wires = net.get("wires", [])
    for i, w1 in enumerate(wires):
        if _is_h_wire(w1) and _match_v_u_detour_wj(net, w1, i, deg, wire_index):
            return True
    return False


def _apply_h_u_detour(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    w3: JsonDict,
    coords: tuple[int, int, int, int],
) -> None:
    """Collapse a horizontal U-detour by reconnecting w1 and w3 at (x1, y2)."""
    x1, x2, y1, y2 = coords
    wires = net.get("wires", [])
    vias = net.get("vias", [])
    _set_wire_coord(w1, "y1", "y2", y1, y2)
    _set_wire_coord(w3, "x1", "x2", x2, x1)
    wires.remove(wj)
    wires.remove(w2)
    new_vias = [
        v
        for v in vias
        if not (
            (v.get("y") == y1 and v.get("x") in (x1, x2)) or (v.get("y") == y2 and v.get("x") == x2)
        )
    ]
    if w3["x1"] == w3["x2"]:
        wires.remove(w3)
    else:
        new_vias.append({"layer": "M2", "x": x1, "y": y2, "via": "SURFACE_TURN_VIA"})
    net["vias"] = new_vias
    net["wires"] = merge_collinear_wires(wires, new_vias)


def _is_h_u_detour_w3_clear(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    w3: JsonDict,
    coords: tuple[int, int, int, int],
    wire_index: SpatialWireIndex,
) -> bool:
    """Return True if w3 forms a valid, unblocked horizontal U-detour with (w1, wj, w2)."""
    x1, x2, y1, y2 = coords
    if not _is_h_wire(w3) or w3["y1"] != y2 or not _wire_has_x(w3, x2):
        return False
    x3 = _other_x(w3, x2)
    if not _is_same_side(x1, x3, x2):
        return False
    if _other_wires_touch(net, wj, (wj, w1, w2)) or _other_wires_touch(net, w2, (wj, w2, w3)):
        return False
    if not wire_index.is_v_clear_detour(x1, y1, y2, net):
        return False
    w3_extends = not (min(x2, x3) <= x1 <= max(x2, x3))
    return not w3_extends or wire_index.is_h_clear_detour(y2, min(x1, x2), max(x1, x2), net)


def _try_h_u_detour_w3(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    w2: JsonDict,
    excluded_idxs: set[int],
    coords: tuple[int, int, int, int],
    wire_index: SpatialWireIndex,
) -> bool:
    """Find candidate w3 for a horizontal U-detour and collapse if clear."""
    wires = net.get("wires", [])
    for m, w3 in enumerate(wires):
        if m not in excluded_idxs and _is_h_u_detour_w3_clear(
            net, w1, wj, w2, w3, coords, wire_index
        ):
            _apply_h_u_detour(net, w1, wj, w2, w3, coords)
            return True
    return False


def _match_h_u_detour_w2(
    net: JsonDict,
    w1: JsonDict,
    wj: JsonDict,
    ij_set: set[int],
    coords_prefix: tuple[int, int, int],
    wire_index: SpatialWireIndex,
) -> bool:
    """Find vertical bridge w2 for horizontal U-detour (w1, wj)."""
    x1, x2, y1 = coords_prefix
    wires = net.get("wires", [])
    for k, w2 in enumerate(wires):
        if k in ij_set or not _is_v_wire(w2) or w2["x1"] != x2 or not _wire_has_y(w2, y1):
            continue
        y2 = _other_y(w2, y1)
        if _try_h_u_detour_w3(net, w1, wj, w2, ij_set | {k}, (x1, x2, y1, y2), wire_index):
            return True
    return False


def _match_h_u_detour_wj(
    net: JsonDict, w1: JsonDict, i: int, deg: dict[Coord, int], wire_index: SpatialWireIndex
) -> bool:
    """Find candidate horizontal leg wj and bridge w2 for a horizontal U-detour."""
    x1 = w1["x1"]
    wires = net.get("wires", [])
    for j, wj in enumerate(wires):
        if i == j or not _is_h_wire(wj):
            continue
        y1 = wj["y1"]
        if not (_wire_has_y(w1, y1) and _wire_has_x(wj, x1)):
            continue
        x2 = _other_x(wj, x1)
        if (deg[(x1, y1)], deg[(x2, y1)]) != (DEGREE_TWO, DEGREE_TWO):
            continue
        if not _point_in_boxes(x2, y1, wire_index) and _match_h_u_detour_w2(
            net, w1, wj, {i, j}, (x1, x2, y1), wire_index
        ):
            return True
    return False


def _try_collapse_h_u_detour(
    net: JsonDict, deg: dict[Coord, int], wire_index: SpatialWireIndex
) -> bool:
    """Scan net wires for a collapsible horizontal U-detour (M2 -> M1 -> M2 -> M1)."""
    wires = net.get("wires", [])
    for i, w1 in enumerate(wires):
        if _is_v_wire(w1) and _match_h_u_detour_wj(net, w1, i, deg, wire_index):
            return True
    return False


def _collapse_net_u_detour_once(net: JsonDict, wire_index: SpatialWireIndex) -> bool:
    """Attempt a single vertical or horizontal U-detour collapse on net."""
    wires = net.get("wires", [])
    if len(wires) < MIN_STAIRCASE_WIRES:
        return False
    deg = _compute_endpoint_degrees(net)
    if _try_collapse_v_u_detour(net, deg, wire_index):
        return True
    return _try_collapse_h_u_detour(net, deg, wire_index)


def eliminate_u_detours(def_data: JsonDict) -> None:
    """Eliminate unblocked U-detour loops across routing channels."""
    units = int(def_data.get("units", DEFAULT_UNITS))
    nets = def_data.get("nets", [])
    wire_index = SpatialWireIndex(def_data, units)

    for _pass in range(MAX_RELAXATION_PASSES):
        any_modified = False
        for net in nets:
            if _collapse_net_u_detour_once(net, wire_index):
                any_modified = True
        if not any_modified:
            break
        wire_index.rebuild()


def _classify_pin_turn_direction(
    w_pin: JsonDict, gx: int, exit_x: int, units: int
) -> tuple[int, tuple[int, int], int] | None:
    """Classify an M1 pin wire as an East exit or West entrance turn candidate."""
    if not _is_h_wire(w_pin):
        return None
    min_wx, max_wx = min(w_pin["x1"], w_pin["x2"]), max(w_pin["x1"], w_pin["x2"])
    is_east = (gx <= min_wx <= exit_x) and (exit_x <= max_wx <= exit_x + units)
    is_west = (gx <= max_wx <= exit_x) and (gx - units <= min_wx <= gx)
    if is_east:
        return max_wx, EAST_TURN_STEPS, 1
    if is_west:
        return min_wx, WEST_TURN_STEPS, -1
    return None


def _find_turn_pair(net: JsonDict, turn_x: int, py: int) -> tuple[JsonDict, JsonDict, int] | None:
    """Locate the vertical M2 turn wire at turn_x and the connected horizontal M1 wire."""
    wires = net.get("wires", [])
    w_turn = next(
        (w for w in wires if _is_v_wire(w) and w["x1"] == turn_x and _wire_has_y(w, py)),
        None,
    )
    if not w_turn:
        return None
    y_other = _other_y(w_turn, py)
    w_other = next(
        (w for w in wires if _is_h_wire(w) and w["y1"] == y_other and _wire_has_x(w, turn_x)),
        None,
    )
    if not w_other:
        return None
    return w_turn, w_other, y_other


def _wire_causes_turn_bottleneck(
    ow: JsonDict, w_other: JsonDict, turn_x: int, py: int, y_other: int, units: int
) -> bool:
    """Return True if wire ow crosses within 1 tile of the pin turn corner."""
    if _is_h_wire(ow) and ow["y1"] != py:
        return (
            min(ow["x1"], ow["x2"]) <= turn_x <= max(ow["x1"], ow["x2"])
            and abs(ow["y1"] - py) <= units
        )
    if _is_v_wire(ow) and ow["x1"] != turn_x:
        in_y = min(ow["y1"], ow["y2"]) <= y_other <= max(ow["y1"], ow["y2"])
        in_x = min(w_other["x1"], w_other["x2"]) <= ow["x1"] <= max(w_other["x1"], w_other["x2"])
        return in_y and in_x and abs(ow["x1"] - turn_x) <= units
    return False


def _has_turn_bottleneck(
    def_data: JsonDict,
    net: JsonDict,
    w_other: JsonDict,
    turn_x: int,
    py: int,
    y_other: int,
    units: int,
) -> bool:
    """Return True if any other net creates a crossing bottleneck near the turn."""
    all_nets = def_data.get("nets", [])
    for on in all_nets:
        if on is net:
            continue
        other_wires = on.get("wires", [])
        if any(
            _wire_causes_turn_bottleneck(ow, w_other, turn_x, py, y_other, units)
            for ow in other_wires
        ):
            return True
    return False


def _can_shift_pin_turn(
    net: JsonDict,
    w_pin: JsonDict,
    w_turn: JsonDict,
    w_other: JsonDict,
    turn_x: int,
    x_new: int,
    py: int,
    y_other: int,
    wire_index: SpatialWireIndex,
) -> bool:
    """Return True if shifting the vertical turn from turn_x to x_new is clear and safe."""
    min_y, max_y = min(w_turn["y1"], w_turn["y2"]), max(w_turn["y1"], w_turn["y2"])
    if not wire_index.is_h_clear_detour(py, turn_x, x_new, net):
        return False
    if not wire_index.is_v_clear_detour(x_new, min_y, max_y, net):
        return False
    other_end_x = _other_x(w_other, turn_x)
    is_expanding = abs(x_new - other_end_x) > abs(turn_x - other_end_x)
    if is_expanding and not wire_index.is_h_clear_detour(y_other, turn_x, x_new, net):
        return False
    if _other_wires_touch(net, w_turn, (w_turn, w_pin, w_other)):
        return False
    if not is_expanding:
        shortened_span = {
            "layer": "M1",
            "y1": y_other,
            "y2": y_other,
            "x1": min(turn_x, x_new),
            "x2": max(turn_x, x_new),
        }
        if _other_wires_touch(net, shortened_span, (w_turn, w_other)):
            return False
    return True


def _apply_pin_turn_shift(
    net: JsonDict,
    w_pin: JsonDict,
    w_turn: JsonDict,
    w_other: JsonDict,
    turn_x: int,
    x_new: int,
    py: int,
    y_other: int,
) -> None:
    """Update wire and via coordinates to shift the pin turn from turn_x to x_new."""
    _set_wire_coord(w_pin, "x1", "x2", turn_x, x_new)
    w_turn["x1"] = x_new
    w_turn["x2"] = x_new
    _set_wire_coord(w_other, "x1", "x2", turn_x, x_new)
    vias = net.get("vias", [])
    for v in vias:
        if v.get("x") == turn_x and v.get("y") in (py, y_other):
            v["x"] = x_new


def _try_shift_single_pin_wire(
    def_data: JsonDict,
    net: JsonDict,
    w_pin: JsonDict,
    gx: int,
    exit_x: int,
    units: int,
    wire_index: SpatialWireIndex,
) -> None:
    """Inspect a single M1 pin wire and shift its turn if congested."""
    classified = _classify_pin_turn_direction(w_pin, gx, exit_x, units)
    if classified is None:
        return
    turn_x, steps, dx_sign = classified
    py = w_pin["y1"]
    pair = _find_turn_pair(net, turn_x, py)
    if pair is None:
        return
    w_turn, w_other, y_other = pair
    if not _has_turn_bottleneck(def_data, net, w_other, turn_x, py, y_other, units):
        return
    for step in steps:
        x_new = turn_x + dx_sign * step * units
        if _can_shift_pin_turn(net, w_pin, w_turn, w_other, turn_x, x_new, py, y_other, wire_index):
            _apply_pin_turn_shift(net, w_pin, w_turn, w_other, turn_x, x_new, py, y_other)
            break


def _shift_turns_for_component(
    def_data: JsonDict, c: JsonDict, units: int, wire_index: SpatialWireIndex
) -> None:
    """Shift congested pin turns for all nets connected to component c."""
    if c.get("orient", "N") not in ("N", ""):
        return
    gx = c.get("x", 0)
    cname = c.get("name", "")
    _cx, _cy, cw, _ch = get_component_box(c, units, def_data.get("_netlist_data"))
    exit_x = gx + cw
    all_nets = def_data.get("nets", [])
    for n in all_nets:
        conns = n.get("connections", [])
        if not any(inst == cname for inst, _p in conns):
            continue
        net_wires = list(n.get("wires", []))
        for w_pin in net_wires:
            _try_shift_single_pin_wire(def_data, n, w_pin, gx, exit_x, units, wire_index)


def shift_congested_pin_turns(def_data: JsonDict) -> None:
    """Shift perpendicular turns that bottleneck immediately at component pin exits."""
    units = int(def_data.get("units", DEFAULT_UNITS))
    wire_index = SpatialWireIndex(def_data, units)
    components = def_data.get("components", [])
    for c in components:
        _shift_turns_for_component(def_data, c, units, wire_index)


def simplify_orthogonal_routes(def_data: JsonDict) -> None:
    """Simplify stepped staircase jogs and U-detours across routing channels."""
    eliminate_staircase_jogs(def_data)
    eliminate_u_detours(def_data)
    shift_congested_pin_turns(def_data)


def _find_backward_detour_segments(
    net: JsonDict, gx: int
) -> tuple[JsonDict, JsonDict, JsonDict, int] | None:
    """Locate the rear horizontal, rear vertical, and channel horizontal wires of a backward detour."""
    wires = net.get("wires", [])
    w_rear = next((w for w in wires if w.get("layer") == "M1" and min(w["x1"], w["x2"]) < gx), None)
    if not w_rear:
        return None
    w_v_rear = next((w for w in wires if w.get("layer") == "M2" and w["x1"] < gx), None)
    if not w_v_rear:
        return None
    y_chan = _other_y(w_v_rear, w_rear["y1"])
    w_chan = next(
        (
            w
            for w in wires
            if w.get("layer") == "M1" and w["y1"] == y_chan and _wire_has_x(w, w_v_rear["x1"])
        ),
        None,
    )
    if not w_chan:
        return None
    return w_rear, w_v_rear, w_chan, y_chan


def _is_forward_splitter_track_blocked(
    def_data: JsonDict, x_fwd: int, y_low: int, y_high: int, units: int
) -> bool:
    """Return True if any placed component blocks the forward vertical track at x_fwd."""
    block_width = SPLITTER_BLOCK_TILES * units
    components = def_data.get("components", [])
    for oc in components:
        ocx, ocy = oc.get("x", 0), oc.get("y", 0)
        if ocx <= x_fwd <= ocx + block_width and not (ocy + block_width <= y_low or ocy >= y_high):
            return True
    return False


def _apply_forward_splitter_shortcut(
    net: JsonDict,
    w_rear: JsonDict,
    w_v_rear: JsonDict,
    w_chan: JsonDict,
    pin_exit_x: int,
    gy: int,
    x_fwd: int,
    y_chan: int,
    units: int,
) -> None:
    """Replace the rear splitter detour segments with a forward vertical connection at x_fwd."""
    wires = net.get("wires", [])
    wires.remove(w_rear)
    wires.remove(w_v_rear)
    max_pin_x = pin_exit_x + PIN_EXIT_SEARCH_TILES * units
    w_pin = next(
        (w for w in wires if w.get("layer") == "M1" and max(w["x1"], w["x2"]) <= max_pin_x),
        None,
    )
    if w_pin:
        if w_pin["x1"] < w_pin["x2"]:
            w_pin["x2"] = x_fwd
        else:
            w_pin["x1"] = x_fwd
        y_pin = w_pin["y1"]
    else:
        y_pin = gy + units // DEGREE_TWO
        wires.append({"layer": "M1", "x1": pin_exit_x, "y1": y_pin, "x2": x_fwd, "y2": y_pin})
    wires.append(
        {
            "layer": "M2",
            "x1": x_fwd,
            "y1": min(y_pin, y_chan),
            "x2": x_fwd,
            "y2": max(y_pin, y_chan),
        }
    )
    _set_wire_coord(w_chan, "x1", "x2", w_v_rear["x1"], x_fwd)


def _shortcut_single_splitter_net(
    def_data: JsonDict, net: JsonDict, splitters: dict[str, JsonDict], units: int
) -> None:
    """Shortcut a backward splitter detour on a single net if a clear forward track exists."""
    conns = net.get("connections", [])
    src_comp_name = next(
        (inst for inst, p in conns if p in ("y1", "y2") and inst in splitters),
        None,
    )
    if not src_comp_name:
        return
    c = splitters[src_comp_name]
    gx, gy = c.get("x", 0), c.get("y", 0)
    pin_exit_x = gx + units
    segs = _find_backward_detour_segments(net, gx)
    if segs is None:
        return
    w_rear, w_v_rear, w_chan, y_chan = segs
    for step in FORWARD_SPLITTER_STEPS:
        x_fwd = pin_exit_x + step * units - units // DEGREE_TWO
        y_low = min(gy, y_chan)
        y_high = max(gy + SPLITTER_HEIGHT_TILES * units, y_chan)
        if _is_forward_splitter_track_blocked(def_data, x_fwd, y_low, y_high, units):
            continue
        _apply_forward_splitter_shortcut(
            net, w_rear, w_v_rear, w_chan, pin_exit_x, gy, x_fwd, y_chan, units
        )
        break


def shortcut_backward_splitter_detours(def_data: JsonDict) -> None:
    """Shortcut backward loops that route around the rear of splitters."""
    units = int(def_data.get("units", DEFAULT_UNITS))
    components = def_data.get("components", [])
    splitters = {c["name"]: c for c in components if c.get("macro") == "belt_split2"}
    nets = def_data.get("nets", [])
    for net in nets:
        _shortcut_single_splitter_net(def_data, net, splitters, units)
