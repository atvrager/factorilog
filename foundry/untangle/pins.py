"""Pin untangling and commutativity optimization for DEF layouts."""

from __future__ import annotations

import collections
from typing import Any

try:
    from foundry.def_parser import merge_collinear_wires
    from foundry.lef import get_macro_port_slots
    from foundry.untangle.spatial import get_component_box
except ImportError:
    from def_parser import merge_collinear_wires
    from lef import get_macro_port_slots

    from untangle.spatial import get_component_box

JsonDict = dict[str, Any]
Coord = tuple[int, int]
ConnMap = dict[tuple[str, str], JsonDict]

DEFAULT_UNITS = 1000
SPLITTER_OUT_X_OFFSET = 0.75
SPLITTER_IN_X_OFFSET = 0.25
PIN_LOW_Y_OFFSET = 0.5
PIN_HIGH_Y_OFFSET = 1.5
PICKUP_X_OFFSET = 0.5
PIN_A_X_DBU = 250
PIN_A_Y_DBU = 500
PIN_B_Y_DBU = 1500
REVERSE_X_THRESHOLD_DBU = 1000
ADJACENT_TRUNK_UNITS = 2
MIN_MACHINE_INPUTS = 2
PICKUP_BUCKET_SIZE = 250


def _index_inst_pin_nets(def_data: JsonDict) -> ConnMap:
    """Build an (instance_name, pin_name) to net lookup dictionary."""
    conn_map: ConnMap = {}
    for n in def_data.get("nets", []):
        for inst, pin in n.get("connections", []):
            conn_map[(inst, pin)] = n
    return conn_map


def _find_pin_m1_wire(net: JsonDict, pt: Coord) -> JsonDict | None:
    """Find the M1 wire segment that connects to the given pin point."""
    px, py = pt
    for w in net.get("wires", []):
        if w.get("layer") != "M1":
            continue
        if (w["x1"] == px and w["y1"] == py) or (w["x2"] == px and w["y2"] == py):
            return w
    return None


def _other_wire_end(w: JsonDict, pt: Coord) -> Coord:
    """Return the endpoint of wire w that is not pt."""
    if (w["x1"], w["y1"]) == pt:
        return (w["x2"], w["y2"])
    return (w["x1"], w["y1"])


def _find_vertical_m2_at_end(net: JsonDict, end_pt: Coord) -> JsonDict | None:
    """Find the vertical M2 wire segment that connects to end_pt."""
    ex, ey = end_pt
    for w in net.get("wires", []):
        if w.get("layer") != "M2" or w["y1"] == w["y2"]:
            continue
        if (w["x1"] == ex and w["y1"] == ey) or (w["x2"] == ex and w["y2"] == ey):
            return w
    return None


def _other_y(w: JsonDict, y_val: int) -> int:
    """Return the Y coordinate of wire w that does not equal y_val."""
    if w["y1"] == y_val:
        return int(w["y2"])
    return int(w["y1"])


def _other_x(w: JsonDict, x_val: int) -> int:
    """Return the X coordinate of wire w that does not equal x_val."""
    if w["x1"] == x_val:
        return int(w["x2"])
    return int(w["x1"])


def _get_m1_y_span_bounds(net: JsonDict, y_val: int, fallback_w: JsonDict) -> Coord:
    """Find the minimum and maximum X coordinates of M1 wires on net at y_val."""
    xs = [
        x
        for w in net.get("wires", [])
        if w.get("layer") == "M1" and w["y1"] == y_val and w["y2"] == y_val
        for x in (w["x1"], w["x2"])
    ]
    if xs:
        return (min(xs), max(xs))
    return (min(fallback_w["x1"], fallback_w["x2"]), max(fallback_w["x1"], fallback_w["x2"]))


def _find_other_conn(net: JsonDict, exclude_inst: str) -> tuple[str | None, str | None]:
    """Find the first connection in net that does not belong to exclude_inst."""
    for inst, pin in net.get("connections", []):
        if inst != exclude_inst:
            return (inst, pin)
    return (None, None)


def _can_swap_collinear_dest(
    comp: JsonDict,
    dst_comp: JsonDict | None,
    dst_inst: str | None,
    dst_pin: str | None,
    req_pin: str,
    free_pin: str,
    conn_map: ConnMap,
) -> bool:
    """Check if the destination component can swap its input pin from req_pin to free_pin."""
    if not dst_comp or dst_inst is None:
        return False
    if dst_comp.get("macro") not in ("belt_split2", "belt_merge2"):
        return False
    return (
        dst_comp.get("y") == comp.get("y")
        and dst_pin == req_pin
        and (dst_inst, free_pin) not in conn_map
    )


def _is_m1_span_blocked_in_net(net: JsonDict, y_val: int, x_start: int, x_end: int) -> bool:
    """Check if net has a horizontal M1 wire at y_val that overlaps (x_start, x_end)."""
    for ow in net.get("wires", []):
        if ow.get("layer") != "M1" or ow["y1"] != y_val or ow["y2"] != y_val:
            continue
        if max(ow["x1"], ow["x2"]) > x_start and min(ow["x1"], ow["x2"]) < x_end:
            return True
    return False


def _is_m1_span_blocked(
    def_data: JsonDict, n1: JsonDict, n2: JsonDict, y_val: int, x_start: int, x_end: int
) -> bool:
    """Check if any other net has a horizontal M1 wire at y_val overlapping (x_start, x_end)."""
    all_nets = def_data.get("nets", [])
    for other_net in all_nets:
        if other_net is n1 or other_net is n2:
            continue
        if _is_m1_span_blocked_in_net(other_net, y_val, x_start, x_end):
            return True
    return False


def _shift_turn_net_to_pin(
    net: JsonDict,
    w_pin: JsonDict,
    m2_wire: JsonDict,
    end_x: int,
    old_y: int,
    new_y: int,
    name: str,
    old_pin: str,
    new_pin: str,
) -> None:
    """Shift a turning splitter output net from old_pin at old_y to new_pin at new_y."""
    w_pin["y1"] = new_y
    w_pin["y2"] = new_y
    if m2_wire["y1"] == old_y:
        m2_wire["y1"] = new_y
    else:
        m2_wire["y2"] = new_y
    for v in net.get("vias", []):
        if v.get("x") == end_x and v.get("y") == old_y:
            v["y"] = new_y
    net["connections"] = [
        (name, new_pin) if (inst == name and p == old_pin) else (inst, p)
        for inst, p in net.get("connections", [])
    ]


def _map_straight_net_conn(
    inst: str,
    p: str,
    name: str,
    old_pin: str,
    new_pin: str,
    dst_inst: str | None,
    old_dst_pin: str,
    new_dst_pin: str,
) -> tuple[str, str]:
    """Map a single (inst, pin) pair when shifting a straight splitter output net."""
    if inst == name and p == old_pin:
        return (name, new_pin)
    if dst_inst is not None and inst == dst_inst and p == old_dst_pin:
        return (dst_inst, new_dst_pin)
    return (inst, p)


def _shift_straight_net_to_pin(
    net: JsonDict,
    old_y: int,
    new_y: int,
    name: str,
    old_pin: str,
    new_pin: str,
    dst_inst: str | None,
    old_dst_pin: str,
    new_dst_pin: str,
) -> None:
    """Shift a straight horizontal net from old_y to new_y and update its connections."""
    for w in net.get("wires", []):
        if w.get("layer") == "M1" and w["y1"] == old_y and w["y2"] == old_y:
            w["y1"] = new_y
            w["y2"] = new_y
    for v in net.get("vias", []):
        if v.get("y") == old_y:
            v["y"] = new_y
    net["connections"] = [
        _map_straight_net_conn(inst, p, name, old_pin, new_pin, dst_inst, old_dst_pin, new_dst_pin)
        for inst, p in net.get("connections", [])
    ]


def _try_swap_single_m2_turn(
    c: JsonDict,
    name: str,
    n_straight: JsonDict,
    n_turn: JsonDict,
    w_straight: JsonDict,
    w_turn: JsonDict,
    m2_turn: JsonDict,
    end_turn: Coord,
    py_straight: Coord,
    py_turn: Coord,
    pin_straight: str,
    pin_turn: str,
    req_dst_pin: str,
    free_dst_pin: str,
    turn_crosses: bool,
    comp_by_name: dict[str, JsonDict],
    conn_map: ConnMap,
    def_data: JsonDict,
) -> bool:
    """Swap a straight splitter output and a turning splitter output if the turn crosses."""
    if not turn_crosses:
        return False
    min_x, max_x = _get_m1_y_span_bounds(n_straight, py_straight[1], w_straight)
    if not (min_x <= end_turn[0] <= max_x):
        return False
    dst_inst, dst_pin = _find_other_conn(n_straight, name)
    dst_comp = comp_by_name.get(dst_inst) if dst_inst is not None else None
    if not _can_swap_collinear_dest(
        c, dst_comp, dst_inst, dst_pin, req_dst_pin, free_dst_pin, conn_map
    ):
        return False
    if _is_m1_span_blocked(def_data, n_straight, n_turn, py_turn[1], end_turn[0], max_x):
        return False

    _shift_turn_net_to_pin(
        n_turn,
        w_turn,
        m2_turn,
        end_turn[0],
        py_turn[1],
        py_straight[1],
        name,
        pin_turn,
        pin_straight,
    )
    _shift_straight_net_to_pin(
        n_straight,
        py_straight[1],
        py_turn[1],
        name,
        pin_straight,
        pin_turn,
        dst_inst,
        req_dst_pin,
        free_dst_pin,
    )
    conn_map[(name, pin_straight)] = n_turn
    conn_map[(name, pin_turn)] = n_straight
    if dst_inst is not None:
        conn_map.pop((dst_inst, req_dst_pin), None)
        conn_map[(dst_inst, free_dst_pin)] = n_straight
    n_straight["wires"] = merge_collinear_wires(
        n_straight.get("wires", []), n_straight.get("vias", [])
    )
    n_turn["wires"] = merge_collinear_wires(n_turn.get("wires", []), n_turn.get("vias", []))
    return True


def _find_next_m1_wire(
    net: JsonDict, y_val: int, x_val: int, exclude_w: JsonDict
) -> JsonDict | None:
    """Find an M1 wire in net at y_val connected to x_val that is not exclude_w."""
    for w in net.get("wires", []):
        if w is exclude_w or w.get("layer") != "M1" or w["y1"] != y_val:
            continue
        if w["x1"] == x_val or w["x2"] == x_val:
            return w
    return None


def _is_same_dir_nested_cross(
    dy1: str, dy2: str, other_y1: int, other_y2: int, x1: int, x2: int, go_east: bool
) -> bool:
    """Check if two same-direction horizontal departures from vertical trunks cross."""
    if dy1 != dy2:
        return False
    y1_inner = (other_y1 > other_y2) if dy1 == "S" else (other_y1 < other_y2)
    y2_inner = (other_y2 > other_y1) if dy1 == "S" else (other_y2 < other_y1)
    x1_first = (x1 < x2) if go_east else (x1 > x2)
    x2_first = (x2 < x1) if go_east else (x2 > x1)
    return (y1_inner and x1_first) or (y2_inner and x2_first)


def _check_swap_trunks_needed(
    next_m1_1: JsonDict | None,
    next_m1_2: JsonDict | None,
    end1_x: int,
    end2_x: int,
    dy1: str,
    dy2: str,
    other_y1: int,
    other_y2: int,
) -> tuple[bool, str, str]:
    """Determine if two M2 output trunks should swap X positions and return their X directions."""
    if not next_m1_1 or not next_m1_2:
        return (False, "", "")
    dir_x1 = "E" if _other_x(next_m1_1, end1_x) > end1_x else "W"
    dir_x2 = "E" if _other_x(next_m1_2, end2_x) > end2_x else "W"
    opp_cross = (dir_x1 == "W" and dir_x2 == "E" and end1_x > end2_x) or (
        dir_x1 == "E" and dir_x2 == "W" and end1_x < end2_x
    )
    same_e = (
        dir_x1 == "E"
        and dir_x2 == "E"
        and _is_same_dir_nested_cross(dy1, dy2, other_y1, other_y2, end1_x, end2_x, True)
    )
    same_w = (
        dir_x1 == "W"
        and dir_x2 == "W"
        and _is_same_dir_nested_cross(dy1, dy2, other_y1, other_y2, end1_x, end2_x, False)
    )
    return (opp_cross or same_e or same_w, dir_x1, dir_x2)


def _select_inner_outer_bundles(
    dir_x1: str,
    dir_x2: str,
    dy1: str,
    other_y1: int,
    other_y2: int,
    b1: tuple[JsonDict, int, int, JsonDict, JsonDict, JsonDict],
    b2: tuple[JsonDict, int, int, JsonDict, JsonDict, JsonDict],
) -> tuple[
    tuple[JsonDict, int, int, JsonDict, JsonDict, JsonDict],
    tuple[JsonDict, int, int, JsonDict, JsonDict, JsonDict],
]:
    """Select which net bundle takes the inner X track and which takes the outer X track."""
    if dir_x1 == "W" and dir_x2 == "E":
        return (b1, b2)
    if dir_x1 == "E" and dir_x2 == "W":
        return (b2, b1)
    y1_closer = (dy1 == "S" and other_y1 > other_y2) or (dy1 == "N" and other_y1 < other_y2)
    if dir_x1 == "E" and dir_x2 == "E":
        return (b2, b1) if y1_closer else (b1, b2)
    return (b1, b2) if y1_closer else (b2, b1)


def _wire_blocks_m1_inclusive(ow: JsonDict, y_val: int, x_min: int, x_max: int) -> bool:
    """Check if wire ow is a horizontal M1 wire at y_val overlapping [x_min, x_max]."""
    if ow.get("layer") != "M1" or ow["y1"] != y_val or ow["y2"] != y_val:
        return False
    return not (max(ow["x1"], ow["x2"]) < x_min or min(ow["x1"], ow["x2"]) > x_max)


def _wire_blocks_m2_inclusive(ow: JsonDict, x_val: int, y_min: int, y_max: int) -> bool:
    """Check if wire ow is a vertical M2 wire at x_val overlapping [y_min, y_max]."""
    if ow.get("layer") != "M2" or ow["x1"] != x_val or ow["x2"] != x_val:
        return False
    return not (max(ow["y1"], ow["y2"]) < y_min or min(ow["y1"], ow["y2"]) > y_max)


def _net_blocks_box_swap(
    net: JsonDict,
    m1_a: tuple[int, int, int],
    m1_b: tuple[int, int, int],
    m2_a: tuple[int, int, int],
    m2_b: tuple[int, int, int],
) -> bool:
    """Check if any wire in net blocks the given M1 or M2 intervals (inclusive)."""
    for ow in net.get("wires", []):
        if _wire_blocks_m1_inclusive(ow, m1_a[0], m1_a[1], m1_a[2]):
            return True
        if _wire_blocks_m1_inclusive(ow, m1_b[0], m1_b[1], m1_b[2]):
            return True
        if _wire_blocks_m2_inclusive(ow, m2_a[0], m2_a[1], m2_a[2]):
            return True
        if _wire_blocks_m2_inclusive(ow, m2_b[0], m2_b[1], m2_b[2]):
            return True
    return False


def _any_net_blocks_box_swap(
    def_data: JsonDict,
    n1: JsonDict,
    n2: JsonDict,
    m1_a: tuple[int, int, int],
    m1_b: tuple[int, int, int],
    m2_a: tuple[int, int, int],
    m2_b: tuple[int, int, int],
) -> bool:
    """Check if any net other than n1 and n2 blocks the specified M1/M2 intervals."""
    all_nets = def_data.get("nets", [])
    for other_net in all_nets:
        if other_net is n1 or other_net is n2:
            continue
        if _net_blocks_box_swap(other_net, m1_a, m1_b, m2_a, m2_b):
            return True
    return False


def _comp_box_blocks_seg(
    box: tuple[int, int, int, int], fixed_val: int, span_min: int, span_max: int, is_vert: bool
) -> bool:
    """Check if a component bounding box intersects a vertical or horizontal segment."""
    cx, cy, cw, ch = box
    if is_vert:
        return cx <= fixed_val <= cx + cw and not (cy + ch <= span_min or cy >= span_max)
    return cy <= fixed_val <= cy + ch and not (cx + cw <= span_min or cx >= span_max)


def _components_block_trunk_swap(
    def_data: JsonDict,
    name: str,
    x_inner: int,
    x_outer: int,
    py_in: Coord,
    py_out: Coord,
    other_y_in: int,
    other_y_out: int,
) -> bool:
    """Check if any component bounding box blocks the swapped trunk or pin segments."""
    netlist = def_data.get("_netlist_data")
    units = int(def_data.get("units", DEFAULT_UNITS))
    y_min_in, y_max_in = min(py_in[1], other_y_in), max(py_in[1], other_y_in)
    y_min_out, y_max_out = min(py_out[1], other_y_out), max(py_out[1], other_y_out)
    x_min_in, x_max_in = min(py_in[0], x_inner), max(py_in[0], x_inner)
    x_min_out, x_max_out = min(py_out[0], x_outer), max(py_out[0], x_outer)

    all_comps = def_data.get("components", [])
    for comp in all_comps:
        if comp.get("name") == name:
            continue
        box = get_component_box(comp, units, netlist)
        if (
            _comp_box_blocks_seg(box, x_inner, y_min_in, y_max_in, True)
            or _comp_box_blocks_seg(box, x_outer, y_min_out, y_max_out, True)
            or _comp_box_blocks_seg(box, py_in[1], x_min_in, x_max_in, False)
            or _comp_box_blocks_seg(box, py_out[1], x_min_out, x_max_out, False)
        ):
            return True
    return False


def _apply_trunk_bundle_update(
    bundle: tuple[JsonDict, int, int, JsonDict, JsonDict, JsonDict],
    new_x: int,
    py_target: Coord,
    pin_name: str,
    name: str,
    py1_y: int,
    py2_y: int,
) -> None:
    """Update wires, connections, and vias for one net bundle during a trunk swap."""
    net, old_x, other_y, w_pin, m2_w, next_m1 = bundle
    if w_pin["x1"] == py_target[0]:
        w_pin["x2"] = new_x
    else:
        w_pin["x1"] = new_x
    w_pin["y1"] = py_target[1]
    w_pin["y2"] = py_target[1]

    m2_w["x1"] = new_x
    m2_w["x2"] = new_x
    if m2_w["y1"] == other_y:
        m2_w["y2"] = py_target[1]
    else:
        m2_w["y1"] = py_target[1]

    if next_m1["x1"] == old_x:
        next_m1["x1"] = new_x
    else:
        next_m1["x2"] = new_x

    conns = [conn for conn in net.get("connections", []) if conn[0] != name]
    net["connections"] = [(name, pin_name), *conns]

    for v in net.get("vias", []):
        if v.get("x") == old_x:
            v["x"] = new_x
        if v.get("y") in (py1_y, py2_y):
            v["y"] = py_target[1]


def _try_swap_splitter_trunks(
    def_data: JsonDict,
    name: str,
    n_y1: JsonDict,
    n_y2: JsonDict,
    py1: Coord,
    py2: Coord,
    end1: Coord,
    end2: Coord,
    other_y1: int,
    other_y2: int,
    dy1: str,
    dir_x1: str,
    dir_x2: str,
    w1: JsonDict,
    w2: JsonDict,
    m2_1: JsonDict,
    m2_2: JsonDict,
    next_m1_1: JsonDict,
    next_m1_2: JsonDict,
) -> bool:
    """Attempt to swap both the X tracks and pin assignments of two splitter output trunks."""
    x_inner, x_outer = min(end1[0], end2[0]), max(end1[0], end2[0])
    b1 = (n_y1, end1[0], other_y1, w1, m2_1, next_m1_1)
    b2 = (n_y2, end2[0], other_y2, w2, m2_2, next_m1_2)
    b_in, b_out = _select_inner_outer_bundles(dir_x1, dir_x2, dy1, other_y1, other_y2, b1, b2)
    other_y_in, other_y_out = b_in[2], b_out[2]

    if dy1 == "N":
        py_in, pin_in, py_out, pin_out = py2, "y2", py1, "y1"
    else:
        py_in, pin_in, py_out, pin_out = py1, "y1", py2, "y2"

    m1_a = (py1[1], py1[0], x_outer)
    m1_b = (py2[1], py2[0], x_outer)
    m2_a = (x_inner, min(py_in[1], other_y_in), max(py_in[1], other_y_in))
    m2_b = (x_outer, min(py_out[1], other_y_out), max(py_out[1], other_y_out))

    if _any_net_blocks_box_swap(def_data, n_y1, n_y2, m1_a, m1_b, m2_a, m2_b):
        return False
    if _components_block_trunk_swap(
        def_data, name, x_inner, x_outer, py_in, py_out, other_y_in, other_y_out
    ):
        return False

    _apply_trunk_bundle_update(b_in, x_inner, py_in, pin_in, name, py1[1], py2[1])
    _apply_trunk_bundle_update(b_out, x_outer, py_out, pin_out, name, py1[1], py2[1])
    return True


def _apply_splitter_cross_pin_swap(
    name: str,
    n_y1: JsonDict,
    n_y2: JsonDict,
    py1: Coord,
    py2: Coord,
    end1: Coord,
    end2: Coord,
    w1: JsonDict,
    w2: JsonDict,
    m2_1: JsonDict,
    m2_2: JsonDict,
    conn_map: ConnMap,
) -> None:
    """Swap the downstream wire trees and connections of two splitter output nets."""
    if (w1["x1"], w1["y1"]) == py1:
        w1["x2"] = end2[0]
    else:
        w1["x1"] = end2[0]
    if (w2["x1"], w2["y1"]) == py2:
        w2["x2"] = end1[0]
    else:
        w2["x1"] = end1[0]

    if (m2_1["x1"], m2_1["y1"]) == end1:
        m2_1["y1"] = py2[1]
    else:
        m2_1["y2"] = py2[1]
    if (m2_2["x1"], m2_2["y1"]) == end2:
        m2_2["y1"] = py1[1]
    else:
        m2_2["y2"] = py1[1]

    wires1 = [w for w in n_y1["wires"] if w is not w1]
    wires2 = [w for w in n_y2["wires"] if w is not w2]
    n_y1["wires"] = [w1, *wires2]
    n_y2["wires"] = [w2, *wires1]

    conns1 = [conn for conn in n_y1.get("connections", []) if conn[0] != name]
    conns2 = [conn for conn in n_y2.get("connections", []) if conn[0] != name]
    n_y1["connections"] = [(name, "y1"), *conns2]
    n_y2["connections"] = [(name, "y2"), *conns1]
    for dst_inst, dst_pin in conns2:
        conn_map[(dst_inst, dst_pin)] = n_y1
    for dst_inst, dst_pin in conns1:
        conn_map[(dst_inst, dst_pin)] = n_y2


def _untangle_dual_m2_splitter_outputs(
    def_data: JsonDict,
    name: str,
    n_y1: JsonDict,
    n_y2: JsonDict,
    py1: Coord,
    py2: Coord,
    w1: JsonDict,
    w2: JsonDict,
    end1: Coord,
    end2: Coord,
    m2_1: JsonDict,
    m2_2: JsonDict,
    conn_map: ConnMap,
) -> None:
    """Untangle splitter outputs when both y1 and y2 connect to vertical M2 segments."""
    other_y1 = _other_y(m2_1, end1[1])
    other_y2 = _other_y(m2_2, end2[1])
    dy1 = "N" if other_y1 > end1[1] else "S"
    dy2 = "N" if other_y2 > end2[1] else "S"
    cross_pin = (
        (dy1 == "N" and dy2 == "S")
        or (dy1 == "N" and dy2 == "N" and end1[0] < end2[0])
        or (dy1 == "S" and dy2 == "S" and end2[0] < end1[0])
    )

    next_m1_1 = _find_next_m1_wire(n_y1, other_y1, end1[0], w1)
    next_m1_2 = _find_next_m1_wire(n_y2, other_y2, end2[0], w2)
    swap_trunks, dir_x1, dir_x2 = _check_swap_trunks_needed(
        next_m1_1, next_m1_2, end1[0], end2[0], dy1, dy2, other_y1, other_y2
    )
    if not cross_pin and not swap_trunks:
        return

    if (
        swap_trunks
        and next_m1_1
        and next_m1_2
        and _try_swap_splitter_trunks(
            def_data,
            name,
            n_y1,
            n_y2,
            py1,
            py2,
            end1,
            end2,
            other_y1,
            other_y2,
            dy1,
            dir_x1,
            dir_x2,
            w1,
            w2,
            m2_1,
            m2_2,
            next_m1_1,
            next_m1_2,
        )
    ):
        return

    if not cross_pin:
        return

    m1_a = (py1[1], min(py1[0], end2[0]), max(py1[0], end2[0]))
    m1_b = (py2[1], min(py2[0], end1[0]), max(py2[0], end1[0]))
    m2_a = (end1[0], min(py2[1], other_y1), max(py2[1], other_y1))
    m2_b = (end2[0], min(py1[1], other_y2), max(py1[1], other_y2))
    if _any_net_blocks_box_swap(def_data, n_y1, n_y2, m1_a, m1_b, m2_a, m2_b):
        return

    _apply_splitter_cross_pin_swap(
        name, n_y1, n_y2, py1, py2, end1, end2, w1, w2, m2_1, m2_2, conn_map
    )


def _dispatch_splitter_m2_turns(
    c: JsonDict,
    name: str,
    n_y1: JsonDict,
    n_y2: JsonDict,
    py1: Coord,
    py2: Coord,
    w1: JsonDict,
    w2: JsonDict,
    end1: Coord,
    end2: Coord,
    m2_1: JsonDict | None,
    m2_2: JsonDict | None,
    comp_by_name: dict[str, JsonDict],
    conn_map: ConnMap,
    def_data: JsonDict,
) -> None:
    """Dispatch single-turn or dual-turn untangling for splitter output pins y1 and y2."""
    if not m2_1 and m2_2:
        other_y2 = _other_y(m2_2, end2[1])
        _try_swap_single_m2_turn(
            c,
            name,
            n_y1,
            n_y2,
            w1,
            w2,
            m2_2,
            end2,
            py1,
            py2,
            "y1",
            "y2",
            "a",
            "b",
            other_y2 < py1[1],
            comp_by_name,
            conn_map,
            def_data,
        )
        return

    if not m2_2 and m2_1:
        other_y1 = _other_y(m2_1, end1[1])
        _try_swap_single_m2_turn(
            c,
            name,
            n_y2,
            n_y1,
            w2,
            w1,
            m2_1,
            end1,
            py2,
            py1,
            "y2",
            "y1",
            "b",
            "a",
            other_y1 > py2[1],
            comp_by_name,
            conn_map,
            def_data,
        )
        return

    if m2_1 and m2_2:
        _untangle_dual_m2_splitter_outputs(
            def_data, name, n_y1, n_y2, py1, py2, w1, w2, end1, end2, m2_1, m2_2, conn_map
        )


def _untangle_single_splitter_output(
    c: JsonDict,
    scale: float,
    comp_by_name: dict[str, JsonDict],
    conn_map: ConnMap,
    def_data: JsonDict,
) -> None:
    """Check and untangle the y1 and y2 output pins of a single splitter or balancer."""
    if c.get("macro") not in ("belt_split2", "belt_balancer2"):
        return
    name = c.get("name", "")
    n_y1 = conn_map.get((name, "y1"))
    n_y2 = conn_map.get((name, "y2"))
    if not n_y1 or not n_y2:
        return

    gx = c.get("x", 0) * scale
    gy = c.get("y", 0) * scale
    py1 = (
        round((gx + SPLITTER_OUT_X_OFFSET) * DEFAULT_UNITS),
        round((gy + PIN_LOW_Y_OFFSET) * DEFAULT_UNITS),
    )
    py2 = (
        round((gx + SPLITTER_OUT_X_OFFSET) * DEFAULT_UNITS),
        round((gy + PIN_HIGH_Y_OFFSET) * DEFAULT_UNITS),
    )
    w1 = _find_pin_m1_wire(n_y1, py1)
    w2 = _find_pin_m1_wire(n_y2, py2)
    if not w1 or not w2:
        return

    end1 = _other_wire_end(w1, py1)
    end2 = _other_wire_end(w2, py2)
    m2_1 = _find_vertical_m2_at_end(n_y1, end1)
    m2_2 = _find_vertical_m2_at_end(n_y2, end2)
    _dispatch_splitter_m2_turns(
        c,
        name,
        n_y1,
        n_y2,
        py1,
        py2,
        w1,
        w2,
        end1,
        end2,
        m2_1,
        m2_2,
        comp_by_name,
        conn_map,
        def_data,
    )


def untangle_splitter_outputs(def_data: JsonDict) -> None:
    """Untangle symmetric splitter outputs whose initial routing segments cross.

    Splitter outputs y1 and y2 carry identical items at identical rates.
    When the router connects y1 and y2 such that one output turns across the other,
    swap the initial segments and net connections to eliminate the crossing.
    """
    scale = 1.0 / float(def_data.get("units", DEFAULT_UNITS))
    components = def_data.get("components", [])
    comp_by_name = {c.get("name", ""): c for c in components}
    conn_map = _index_inst_pin_nets(def_data)
    for c in components:
        _untangle_single_splitter_output(c, scale, comp_by_name, conn_map, def_data)


def _is_reverse_splitter_wire(w: JsonDict, px: int, py: int, max_valid_x: int) -> bool:
    """Check if wire w is an M1 segment entering pin (px, py) from East of max_valid_x."""
    if w.get("layer") != "M1":
        return False
    return (w["x1"] == px and w["y1"] == py and w["x2"] > max_valid_x) or (
        w["x2"] == px and w["y2"] == py and w["x1"] > max_valid_x
    )


def _fix_component_reverse_inputs(c: JsonDict, conn_map: ConnMap) -> None:
    """Remove reverse East-entering input segments for one splitter or merger component."""
    if c.get("macro", "") not in ("belt_merge2", "belt_split2", "belt_balancer2"):
        return
    if c.get("orient", "N") not in ("N", ""):
        return
    gx = c.get("x", 0)
    gy = c.get("y", 0)
    name = c.get("name", "")
    max_x = gx + REVERSE_X_THRESHOLD_DBU
    for pin, py in (("a", gy + PIN_A_Y_DBU), ("b", gy + PIN_B_Y_DBU)):
        n = conn_map.get((name, pin))
        if n is not None:
            px = gx + PIN_A_X_DBU
            n["wires"] = [
                w for w in n.get("wires", []) if not _is_reverse_splitter_wire(w, px, py, max_x)
            ]


def fix_reverse_splitter_inputs(def_data: JsonDict) -> None:
    """Remove reverse input segments that enter East-facing splitters from the East.

    Splitters face East and only accept inputs on their West face. When an input net
    approaches from the East into an input pin, it creates an invalid backward segment.
    Remove this segment so that the vertical belt side-loads directly into the output.
    """
    conn_map = _index_inst_pin_nets(def_data)
    for c in def_data.get("components", []):
        _fix_component_reverse_inputs(c, conn_map)


def _check_merger_entrance_cross(
    dy_a: str, dy_b: str, enda: Coord, endb: Coord, pa: Coord, pb: Coord
) -> bool:
    """Check if two merger input M2 trunks cross near the component pins."""
    if dy_a == "N" and dy_b == "S":
        return True
    if dy_a == "N" and dy_b == "N" and (min(endb[0], pb[0]) < enda[0] < max(endb[0], pb[0])):
        return True
    return dy_a == "S" and dy_b == "S" and (min(enda[0], pa[0]) < endb[0] < max(enda[0], pa[0]))


def _app_wire_crosses_trunk(
    m1_app: JsonDict | None, app_y: int, trunk_x: int, trunk_y1: int, trunk_y2: int
) -> bool:
    """Check if an horizontal M1 approach wire crosses a vertical M2 trunk."""
    if not m1_app:
        return False
    if not (min(trunk_y1, trunk_y2) <= app_y <= max(trunk_y1, trunk_y2)):
        return False
    return min(m1_app["x1"], m1_app["x2"]) < trunk_x < max(m1_app["x1"], m1_app["x2"])


def _wire_blocks_m1_strict(ow: JsonDict, y_val: int, x_min: int, x_max: int) -> bool:
    """Check if wire ow is on M1 at y_val and strictly overlaps (x_min, x_max)."""
    if ow.get("layer") != "M1" or ow["y1"] != y_val:
        return False
    return not (max(ow["x1"], ow["x2"]) <= x_min or min(ow["x1"], ow["x2"]) >= x_max)


def _wire_blocks_m2_strict(ow: JsonDict, x_val: int, y_min: int, y_max: int) -> bool:
    """Check if wire ow is on M2 at x_val and strictly overlaps (y_min, y_max)."""
    if ow.get("layer") != "M2" or ow["x1"] != x_val:
        return False
    return not (max(ow["y1"], ow["y2"]) <= y_min or min(ow["y1"], ow["y2"]) >= y_max)


def _net_blocks_strict_swap(
    net: JsonDict,
    m1_a: tuple[int, int, int],
    m1_b: tuple[int, int, int],
    m2_a: tuple[int, int, int],
    m2_b: tuple[int, int, int],
) -> bool:
    """Check if any wire in net strictly overlaps the given M1 or M2 intervals."""
    for ow in net.get("wires", []):
        if _wire_blocks_m1_strict(ow, m1_a[0], m1_a[1], m1_a[2]):
            return True
        if _wire_blocks_m1_strict(ow, m1_b[0], m1_b[1], m1_b[2]):
            return True
        if _wire_blocks_m2_strict(ow, m2_a[0], m2_a[1], m2_a[2]):
            return True
        if _wire_blocks_m2_strict(ow, m2_b[0], m2_b[1], m2_b[2]):
            return True
    return False


def _any_net_blocks_strict_swap(
    def_data: JsonDict,
    n1: JsonDict,
    n2: JsonDict,
    m1_a: tuple[int, int, int],
    m1_b: tuple[int, int, int],
    m2_a: tuple[int, int, int],
    m2_b: tuple[int, int, int],
) -> bool:
    """Check if any net other than n1 and n2 strictly overlaps the M1/M2 intervals."""
    all_nets = def_data.get("nets", [])
    for other_net in all_nets:
        if other_net is n1 or other_net is n2:
            continue
        if _net_blocks_strict_swap(other_net, m1_a, m1_b, m2_a, m2_b):
            return True
    return False


def _swap_merger_net_pins(
    name: str,
    pin_a_name: str,
    pin_b_name: str,
    n_a: JsonDict,
    n_b: JsonDict,
    conn_map: ConnMap,
) -> None:
    """Swap pin_a_name and pin_b_name on instance name between n_a and n_b."""
    conns_a = n_a.get("connections", [])
    conns_b = n_b.get("connections", [])
    n_a["connections"] = [
        (inst, pin_b_name if inst == name and p == pin_a_name else p) for inst, p in conns_a
    ]
    n_b["connections"] = [
        (inst, pin_a_name if inst == name and p == pin_b_name else p) for inst, p in conns_b
    ]
    conn_map[(name, pin_b_name)] = n_a
    conn_map[(name, pin_a_name)] = n_b


def _update_merger_pin_stub_and_m2(
    wa: JsonDict, m2_a: JsonDict, other_ya: int, new_x: int, target_p: Coord
) -> None:
    """Update M2 trunk Y-endpoint and M1 pin stub to connect at new_x and target_p."""
    if m2_a["y1"] == other_ya:
        m2_a["y2"] = target_p[1]
    else:
        m2_a["y1"] = target_p[1]
    wa["y1"] = target_p[1]
    wa["y2"] = target_p[1]
    wa["x1"] = min(new_x, target_p[0])
    wa["x2"] = max(new_x, target_p[0])


def _try_merger_adjacent_track_swap(
    def_data: JsonDict,
    name: str,
    pin_a_name: str,
    pin_b_name: str,
    n_a: JsonDict,
    n_b: JsonDict,
    pa: Coord,
    pb: Coord,
    enda: Coord,
    endb: Coord,
    other_ya: int,
    other_yb: int,
    wa: JsonDict,
    wb: JsonDict,
    m2_a: JsonDict,
    m2_b: JsonDict,
    m1_app_a: JsonDict,
    m1_app_b: JsonDict,
    conn_map: ConnMap,
) -> bool:
    """Swap both the vertical M2 tracks and pins for adjacent crossing merger inputs."""
    x_min, x_max = min(enda[0], endb[0]), max(enda[0], endb[0])
    m1_a = (other_ya, x_min, x_max)
    m1_b = (other_yb, x_min, x_max)
    m2_a_chk = (endb[0], min(other_ya, pb[1]), max(other_ya, pb[1]))
    m2_b_chk = (enda[0], min(other_yb, pa[1]), max(other_yb, pa[1]))
    if _any_net_blocks_strict_swap(def_data, n_a, n_b, m1_a, m1_b, m2_a_chk, m2_b_chk):
        return False

    if m1_app_a["x1"] == enda[0]:
        m1_app_a["x1"] = endb[0]
    else:
        m1_app_a["x2"] = endb[0]
    if m1_app_b["x1"] == endb[0]:
        m1_app_b["x1"] = enda[0]
    else:
        m1_app_b["x2"] = enda[0]

    m2_a["x1"] = m2_a["x2"] = endb[0]
    m2_b["x1"] = m2_b["x2"] = enda[0]
    _update_merger_pin_stub_and_m2(wa, m2_a, other_ya, endb[0], pb)
    _update_merger_pin_stub_and_m2(wb, m2_b, other_yb, enda[0], pa)
    _swap_merger_net_pins(name, pin_a_name, pin_b_name, n_a, n_b, conn_map)
    return True


def _resolve_merger_input_crossing(
    def_data: JsonDict,
    units: int,
    name: str,
    pin_a_name: str,
    pin_b_name: str,
    n_a: JsonDict,
    n_b: JsonDict,
    pa: Coord,
    pb: Coord,
    wa: JsonDict,
    wb: JsonDict,
    enda: Coord,
    endb: Coord,
    m2_a: JsonDict,
    m2_b: JsonDict,
    conn_map: ConnMap,
) -> None:
    """Resolve entrance or approach crossings for two merger input vertical trunks."""
    other_ya = _other_y(m2_a, enda[1])
    other_yb = _other_y(m2_b, endb[1])
    dy_a = "N" if other_ya > enda[1] else "S"
    dy_b = "N" if other_yb > endb[1] else "S"

    entrance_cross = _check_merger_entrance_cross(dy_a, dy_b, enda, endb, pa, pb)
    m1_app_a = _find_next_m1_wire(n_a, other_ya, enda[0], wa)
    m1_app_b = _find_next_m1_wire(n_b, other_yb, endb[0], wb)
    approach_cross = _app_wire_crosses_trunk(
        m1_app_a, other_ya, endb[0], other_yb, pb[1]
    ) or _app_wire_crosses_trunk(m1_app_b, other_yb, enda[0], other_ya, pa[1])

    if not entrance_cross and not approach_cross:
        return

    if (
        approach_cross
        and abs(enda[0] - endb[0]) <= ADJACENT_TRUNK_UNITS * units
        and m1_app_a
        and m1_app_b
        and _try_merger_adjacent_track_swap(
            def_data,
            name,
            pin_a_name,
            pin_b_name,
            n_a,
            n_b,
            pa,
            pb,
            enda,
            endb,
            other_ya,
            other_yb,
            wa,
            wb,
            m2_a,
            m2_b,
            m1_app_a,
            m1_app_b,
            conn_map,
        )
    ):
        return

    m1_a_chk = (pb[1], min(enda[0], pb[0]), max(enda[0], pb[0]))
    m1_b_chk = (pa[1], min(endb[0], pa[0]), max(endb[0], pa[0]))
    m2_a_chk = (enda[0], min(other_ya, pb[1]), max(other_ya, pb[1]))
    m2_b_chk = (endb[0], min(other_yb, pa[1]), max(other_yb, pa[1]))
    if _any_net_blocks_strict_swap(def_data, n_a, n_b, m1_a_chk, m1_b_chk, m2_a_chk, m2_b_chk):
        return

    _update_merger_pin_stub_and_m2(wa, m2_a, other_ya, enda[0], pb)
    _update_merger_pin_stub_and_m2(wb, m2_b, other_yb, endb[0], pa)
    _swap_merger_net_pins(name, pin_a_name, pin_b_name, n_a, n_b, conn_map)


def _untangle_single_merger_input(
    c: JsonDict, scale: float, units: int, conn_map: ConnMap, def_data: JsonDict
) -> None:
    """Check and untangle crossing inputs on a single merger or balancer component."""
    macro = c.get("macro", "")
    if macro not in ("belt_merge2", "belt_balancer2"):
        return
    pin_a_name = "a1" if macro == "belt_balancer2" else "a"
    pin_b_name = "a2" if macro == "belt_balancer2" else "b"
    name = c.get("name", "")
    n_a = conn_map.get((name, pin_a_name))
    n_b = conn_map.get((name, pin_b_name))
    if not n_a or not n_b or n_a is n_b:
        return

    gx = c.get("x", 0) * scale
    gy = c.get("y", 0) * scale
    pa = (round((gx + SPLITTER_IN_X_OFFSET) * units), round((gy + PIN_LOW_Y_OFFSET) * units))
    pb = (round((gx + SPLITTER_IN_X_OFFSET) * units), round((gy + PIN_HIGH_Y_OFFSET) * units))
    wa = _find_pin_m1_wire(n_a, pa)
    wb = _find_pin_m1_wire(n_b, pb)
    if not wa or not wb:
        return

    enda = _other_wire_end(wa, pa)
    endb = _other_wire_end(wb, pb)
    m2_a = _find_vertical_m2_at_end(n_a, enda)
    m2_b = _find_vertical_m2_at_end(n_b, endb)
    if m2_a and m2_b:
        _resolve_merger_input_crossing(
            def_data,
            units,
            name,
            pin_a_name,
            pin_b_name,
            n_a,
            n_b,
            pa,
            pb,
            wa,
            wb,
            enda,
            endb,
            m2_a,
            m2_b,
            conn_map,
        )


def untangle_merger_inputs(def_data: JsonDict) -> None:
    """Untangle symmetric merger and balancer inputs whose routing segments cross."""
    scale = 1.0 / float(def_data.get("units", DEFAULT_UNITS))
    units = int(def_data.get("units", DEFAULT_UNITS))
    conn_map = _index_inst_pin_nets(def_data)
    for c in def_data.get("components", []):
        _untangle_single_merger_input(c, scale, units, conn_map, def_data)


def _find_pin_m1_wire_by_x(net: JsonDict, pt: Coord) -> JsonDict | None:
    """Find an M1 wire in net at y=pt[1] that has x1==pt[0] or x2==pt[0]."""
    px, py = pt
    wires = net.get("wires", [])
    for w in wires:
        if w.get("layer") == "M1" and w["y1"] == py and (w["x1"] == px or w["x2"] == px):
            return w
    return None


def _find_m2_at_track(net: JsonDict, x_track: int, y_end: int) -> JsonDict | None:
    """Find an M2 wire in net at x1==x_track that connects to y_end."""
    wires = net.get("wires", [])
    for w in wires:
        if w.get("layer") == "M2" and w["x1"] == x_track and (w["y1"] == y_end or w["y2"] == y_end):
            return w
    return None


def _update_shared_track_branch(
    m2_w: JsonDict, w_pin: JsonDict, new_x: int, other_y: int, target_p: Coord
) -> None:
    """Update M2 trunk X/Y and M1 pin wire for one branch of a shared merger track."""
    m2_w["x1"] = m2_w["x2"] = new_x
    if m2_w["y1"] == other_y:
        m2_w["y2"] = target_p[1]
    else:
        m2_w["y1"] = target_p[1]
    w_pin["x1"] = new_x
    w_pin["x2"] = target_p[0]
    w_pin["y1"] = w_pin["y2"] = target_p[1]


def _update_shifted_track_vias(
    net_shift: JsonDict, x_track: int, x_shift: int, other_y: int, old_pin_y: int, new_pin_y: int
) -> None:
    """Shift via coordinates for the merger input net that moves to x_shift."""
    vias_shift = net_shift.get("vias", [])
    for v in vias_shift:
        if v.get("x") != x_track:
            continue
        if v.get("y") == other_y:
            v["x"] = x_shift
        elif v.get("y") == old_pin_y:
            v["x"] = x_shift
            v["y"] = new_pin_y


def _shift_shared_track_wires(
    net_shift: JsonDict,
    net_keep: JsonDict,
    w_arr_shift: JsonDict,
    m2_shift: JsonDict,
    w_pin_shift: JsonDict,
    other_y_shift: int,
    old_pin_y_shift: int,
    target_p_shift: Coord,
    m2_keep: JsonDict,
    w_pin_keep: JsonDict,
    other_y_keep: int,
    old_pin_y_keep: int,
    target_p_keep: Coord,
    x_track: int,
    x_shift: int,
) -> None:
    """Shift one shared-track merger net to x_shift and swap both pin landing Y coordinates."""
    if w_arr_shift["x1"] == x_track:
        w_arr_shift["x1"] = x_shift
    else:
        w_arr_shift["x2"] = x_shift

    _update_shared_track_branch(m2_shift, w_pin_shift, x_shift, other_y_shift, target_p_shift)
    _update_shared_track_branch(m2_keep, w_pin_keep, x_track, other_y_keep, target_p_keep)
    _update_shifted_track_vias(
        net_shift, x_track, x_shift, other_y_shift, old_pin_y_shift, target_p_shift[1]
    )

    vias_keep = net_keep.get("vias", [])
    for v in vias_keep:
        if v.get("x") == x_track and v.get("y") == old_pin_y_keep:
            v["y"] = target_p_keep[1]


def _apply_shared_merger_shift_if_crossed(
    cname: str,
    n_a: JsonDict,
    n_b: JsonDict,
    pa: Coord,
    pb: Coord,
    wa: JsonDict,
    wb: JsonDict,
    enda: Coord,
    endb: Coord,
    m2_a: JsonDict,
    m2_b: JsonDict,
    x_track: int,
    units: int,
    conn_map: ConnMap,
) -> None:
    """Shift one of the shared-track merger inputs West by one unit when their trunks overlap."""
    other_ya = _other_y(m2_a, enda[1])
    other_yb = _other_y(m2_b, endb[1])
    x_shift = x_track - units

    if other_ya < enda[1] and other_yb < endb[1] and other_yb < other_ya:
        w_arr_a = _find_pin_m1_wire_by_x(n_a, (x_track, other_ya))
        if w_arr_a:
            _shift_shared_track_wires(
                n_a,
                n_b,
                w_arr_a,
                m2_a,
                wa,
                other_ya,
                pa[1],
                pb,
                m2_b,
                wb,
                other_yb,
                pb[1],
                pa,
                x_track,
                x_shift,
            )
            _swap_merger_net_pins(cname, "a", "b", n_a, n_b, conn_map)
    elif other_ya > enda[1] and other_yb > endb[1] and other_ya > other_yb:
        w_arr_b = _find_pin_m1_wire_by_x(n_b, (x_track, other_yb))
        if w_arr_b:
            _shift_shared_track_wires(
                n_b,
                n_a,
                w_arr_b,
                m2_b,
                wb,
                other_yb,
                pb[1],
                pa,
                m2_a,
                wa,
                other_ya,
                pa[1],
                pb,
                x_track,
                x_shift,
            )
            _swap_merger_net_pins(cname, "a", "b", n_a, n_b, conn_map)


def _untangle_single_shared_merger(c: JsonDict, units: int, conn_map: ConnMap) -> None:
    """Check and untangle a single merger whose inputs share the same vertical M2 track."""
    if c.get("macro") != "belt_merge2":
        return
    cname = c.get("name", "")
    n_a = conn_map.get((cname, "a"))
    n_b = conn_map.get((cname, "b"))
    if not n_a or not n_b or n_a is n_b:
        return

    gx, gy = c.get("x", 0), c.get("y", 0)
    pa = (gx + int(SPLITTER_IN_X_OFFSET * units), gy + int(PIN_LOW_Y_OFFSET * units))
    pb = (gx + int(SPLITTER_IN_X_OFFSET * units), gy + int(PIN_HIGH_Y_OFFSET * units))
    wa = _find_pin_m1_wire_by_x(n_a, pa)
    wb = _find_pin_m1_wire_by_x(n_b, pb)
    if not wa or not wb:
        return

    enda = _other_wire_end(wa, pa)
    endb = _other_wire_end(wb, pb)
    if enda[0] != endb[0]:
        return
    x_track = enda[0]
    m2_a = _find_m2_at_track(n_a, x_track, enda[1])
    m2_b = _find_m2_at_track(n_b, x_track, endb[1])
    if m2_a and m2_b:
        _apply_shared_merger_shift_if_crossed(
            cname, n_a, n_b, pa, pb, wa, wb, enda, endb, m2_a, m2_b, x_track, units, conn_map
        )


def untangle_shared_merger_tracks(def_data: JsonDict) -> None:
    """Untangle merger inputs that share the same vertical track to prevent overlap."""
    units = int(def_data.get("units", DEFAULT_UNITS))
    conn_map = _index_inst_pin_nets(def_data)
    for c in def_data.get("components", []):
        _untangle_single_shared_merger(c, units, conn_map)


def _find_machine_crossing_wires(
    n0: JsonDict, n1: JsonDict, py0: int, py1: int, pickup_x: int
) -> tuple[JsonDict | None, JsonDict | None, JsonDict | None, list[JsonDict]]:
    """Find the crossing M2/M1 wires for a pair of assembling machine input pins."""
    w_m2_cross = next(
        (
            w
            for w in n1.get("wires", [])
            if w.get("layer") == "M2"
            and abs(w.get("x1", 0) - pickup_x) <= PICKUP_BUCKET_SIZE
            and min(w["y1"], w["y2"]) <= py0
            and max(w["y1"], w["y2"]) == py1
        ),
        None,
    )
    w_m1_term1 = next(
        (
            w
            for w in n1.get("wires", [])
            if w.get("layer") == "M1"
            and (w["y1"] == py1 or w["y2"] == py1)
            and min(w["x1"], w["x2"]) <= pickup_x <= max(w["x1"], w["x2"])
        ),
        None,
    )
    w_m2_app0 = next(
        (
            w
            for w in n0.get("wires", [])
            if w.get("layer") == "M2"
            and w.get("x1", 0) < pickup_x
            and (w["y1"] == py0 or w["y2"] == py0)
        ),
        None,
    )
    w_m1_term0_list = [
        w
        for w in n0.get("wires", [])
        if w.get("layer") == "M1"
        and (w["y1"] == py0 and w["y2"] == py0)
        and max(w["x1"], w["x2"]) >= pickup_x
    ]
    return (w_m2_cross, w_m1_term1, w_m2_app0, w_m1_term0_list)


def _swap_machine_pin_pair(
    cname: str,
    p0: str,
    p1: str,
    py0: int,
    py1: int,
    n0: JsonDict,
    n1: JsonDict,
    w_m2_cross: JsonDict,
    w_m1_term1: JsonDict,
    w_m2_app0: JsonDict,
    w_m1_term0_list: list[JsonDict],
    port_to_net: dict[str, JsonDict],
    conn_map: ConnMap,
) -> None:
    """Swap two crossing input pins and their terminal wires on an assembling machine."""
    conns0 = [conn for conn in n0.get("connections", []) if conn[0] != cname]
    conns1 = [conn for conn in n1.get("connections", []) if conn[0] != cname]
    n0["connections"] = [(cname, p1), *conns0]
    n1["connections"] = [(cname, p0), *conns1]
    port_to_net[p0] = n1
    port_to_net[p1] = n0
    conn_map[(cname, p0)] = n1
    conn_map[(cname, p1)] = n0

    if w_m2_cross["y1"] == py1:
        w_m2_cross["y1"] = py0
    if w_m2_cross["y2"] == py1:
        w_m2_cross["y2"] = py0
    w_m1_term1["y1"] = py0
    w_m1_term1["y2"] = py0

    if w_m2_app0["y1"] == py0:
        w_m2_app0["y1"] = py1
    if w_m2_app0["y2"] == py0:
        w_m2_app0["y2"] = py1
    for w in w_m1_term0_list:
        w["y1"] = py1
        w["y2"] = py1

    for via in n1.get("vias", []):
        if via.get("y") == py1:
            via["y"] = py0
    for via in n0.get("vias", []):
        if via.get("y") == py0:
            via["y"] = py1


def _try_untangle_machine_pin_pair(
    cname: str,
    p0: str,
    p1: str,
    slot0: int,
    slot1: int,
    gy: int,
    pickup_x: int,
    units: int,
    port_to_net: dict[str, JsonDict],
    conn_map: ConnMap,
) -> None:
    """Check and untangle a single pair of input ports on an assembling machine."""
    if slot0 > slot1:
        p0, p1 = p1, p0
        slot0, slot1 = slot1, slot0

    py0 = gy + int((slot0 + PIN_LOW_Y_OFFSET) * units)
    py1 = gy + int((slot1 + PIN_LOW_Y_OFFSET) * units)
    n0 = port_to_net.get(p0)
    n1 = port_to_net.get(p1)
    if not n0 or not n1 or n0 is n1:
        return

    w_m2_cross, w_m1_term1, w_m2_app0, w_m1_term0_list = _find_machine_crossing_wires(
        n0, n1, py0, py1, pickup_x
    )
    if w_m2_cross and w_m1_term1 and w_m2_app0 and w_m1_term0_list:
        _swap_machine_pin_pair(
            cname,
            p0,
            p1,
            py0,
            py1,
            n0,
            n1,
            w_m2_cross,
            w_m1_term1,
            w_m2_app0,
            w_m1_term0_list,
            port_to_net,
            conn_map,
        )


def _untangle_single_machine_inputs(
    c: JsonDict,
    scale: float,
    units: int,
    inst_map: dict[str, JsonDict],
    cell_map: dict[str, JsonDict],
    fluids: set[str],
    conn_map: ConnMap,
) -> None:
    """Untangle crossing West-facing item inputs for a single assembling machine."""
    cname = c.get("name", "")
    inst = inst_map.get(cname, {})
    cell_id = inst.get("cell", c.get("macro", ""))
    cell = cell_map.get(cell_id, {})
    if "assembling-machine" not in cell.get("machine", ""):
        return
    port_faces = cell.get("port_faces") or {}
    inputs = [
        p.replace("-", "_")
        for p in cell.get("inputs", [])
        if p not in fluids and port_faces.get(p, "W") == "W"
    ]
    if len(inputs) < MIN_MACHINE_INPUTS:
        return

    gx = round(c.get("x", 0) * scale * units)
    gy = round(c.get("y", 0) * scale * units)
    in_slots, _ = get_macro_port_slots(cell)
    port_to_net: dict[str, JsonDict] = {
        pin: n for pin in inputs if (n := conn_map.get((cname, pin))) is not None
    }
    pickup_x = gx - int(PICKUP_X_OFFSET * units)

    for i in range(len(inputs)):
        for j in range(i + 1, len(inputs)):
            p0, p1 = inputs[i], inputs[j]
            _try_untangle_machine_pin_pair(
                cname,
                p0,
                p1,
                in_slots.get(p0, i),
                in_slots.get(p1, j),
                gy,
                pickup_x,
                units,
                port_to_net,
                conn_map,
            )


def untangle_machine_inputs(def_data: JsonDict, netlist_data: JsonDict) -> None:
    """Untangle symmetric assembling machine inputs whose incoming nets cross.

    Assembling machines in Factorio accept recipe ingredients through any
    connected input inserter. When two feeder nets approach an assembling
    machine such that their vertical or horizontal tracks cross in front of
    the machine, swap the pin assignments and initial wire endpoints to
    eliminate the crossing before jumper synthesis.
    """
    scale = 1.0 / float(def_data.get("units", DEFAULT_UNITS))
    units = int(def_data.get("units", DEFAULT_UNITS))
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})
    fluids = set(netlist_data.get("fluids", []))
    conn_map = _index_inst_pin_nets(def_data)

    for c in def_data.get("components", []):
        _untangle_single_machine_inputs(c, scale, units, inst_map, cell_map, fluids, conn_map)


def _index_wires_for_relocation(
    def_data: JsonDict,
) -> tuple[dict[int, list[tuple[JsonDict, JsonDict]]], dict[int, list[tuple[JsonDict, JsonDict]]]]:
    """Build spatial bucket indices of M2 and horizontal M1 wires for pin relocation."""
    m2_by_x_bucket: dict[int, list[tuple[JsonDict, JsonDict]]] = collections.defaultdict(list)
    m1_by_y: dict[int, list[tuple[JsonDict, JsonDict]]] = collections.defaultdict(list)
    for n in def_data.get("nets", []):
        for w in n.get("wires", []):
            layer = w.get("layer")
            if layer == "M2":
                m2_by_x_bucket[w["x1"] // PICKUP_BUCKET_SIZE].append((n, w))
            elif layer == "M1" and w["y1"] == w["y2"]:
                m1_by_y[w["y1"]].append((n, w))
    return (m2_by_x_bucket, m1_by_y)


def _is_pin_a_front_blocked(
    net_a: JsonDict,
    front_x: int,
    py_a: int,
    m2_by_x_bucket: dict[int, list[tuple[JsonDict, JsonDict]]],
) -> bool:
    """Check if any other net has a vertical M2 wire crossing in front of pin a."""
    fx_bucket = front_x // PICKUP_BUCKET_SIZE
    for bx in (fx_bucket - 1, fx_bucket, fx_bucket + 1):
        for other_net, w in m2_by_x_bucket.get(bx, ()):
            if (
                other_net is not net_a
                and abs(w["x1"] - front_x) <= PICKUP_BUCKET_SIZE
                and min(w["y1"], w["y2"]) <= py_a <= max(w["y1"], w["y2"])
            ):
                return True
    return False


def _is_pin_b_relocation_blocked(
    net_a: JsonDict,
    py_a: int,
    py_b: int,
    start_x: int,
    end_x: int,
    m2_by_x_bucket: dict[int, list[tuple[JsonDict, JsonDict]]],
    m1_by_y: dict[int, list[tuple[JsonDict, JsonDict]]],
) -> bool:
    """Check if shifting net_a from pin a to pin b is blocked on M1 or M2."""
    for other_net, ow in m1_by_y.get(py_b, ()):
        if (
            other_net is not net_a
            and ow["y1"] == py_b
            and ow["y2"] == py_b
            and not (max(ow["x1"], ow["x2"]) <= start_x or min(ow["x1"], ow["x2"]) >= end_x)
        ):
            return True

    ext_min_y, ext_max_y = min(py_a, py_b), max(py_a, py_b)
    for other_net, ow in m2_by_x_bucket.get(start_x // PICKUP_BUCKET_SIZE, ()):
        if (
            other_net is not net_a
            and ow["x1"] == start_x
            and not (max(ow["y1"], ow["y2"]) <= ext_min_y or min(ow["y1"], ow["y2"]) >= ext_max_y)
        ):
            return True
    return False


def _apply_pin_a_to_b_relocation(
    cname: str,
    net_a: JsonDict,
    m2_feeder: JsonDict,
    incoming_m1: list[JsonDict],
    py_a: int,
    py_b: int,
    start_x: int,
    end_x: int,
    m1_by_y: dict[int, list[tuple[JsonDict, JsonDict]]],
    conn_map: ConnMap,
) -> None:
    """Move net_a wires, vias, and pin connection from splitter pin a to pin b."""
    if m2_feeder["y1"] == py_a:
        m2_feeder["y1"] = py_b
    elif m2_feeder["y2"] == py_a:
        m2_feeder["y2"] = py_b

    for w in incoming_m1:
        w["y1"] = py_b
        w["y2"] = py_b
        m1_by_y[py_b].append((net_a, w))

    for v in net_a.get("vias", []):
        if v.get("y") == py_a and start_x <= v.get("x", 0) <= end_x:
            v["y"] = py_b

    net_a["connections"] = [
        (inst, "b") if (inst == cname and pin == "a") else (inst, pin)
        for inst, pin in net_a.get("connections", [])
    ]
    conn_map.pop((cname, "a"), None)
    conn_map[(cname, "b")] = net_a


def _relocate_single_splitter_input(
    c: JsonDict,
    units: int,
    conn_map: ConnMap,
    m2_by_x_bucket: dict[int, list[tuple[JsonDict, JsonDict]]],
    m1_by_y: dict[int, list[tuple[JsonDict, JsonDict]]],
) -> None:
    """Check and relocate blocked input pin a to pin b for a single splitter."""
    if c.get("macro") != "belt_split2" or c.get("orient", "N") not in ("N", ""):
        return
    cname = c.get("name", "")
    net_a = conn_map.get((cname, "a"))
    if not net_a or conn_map.get((cname, "b")) is not None:
        return

    gx, gy = c.get("x", 0), c.get("y", 0)
    py_a = gy + int(PIN_LOW_Y_OFFSET * units)
    py_b = gy + int(PIN_HIGH_Y_OFFSET * units)
    front_x = gx - int(PICKUP_X_OFFSET * units)

    if not _is_pin_a_front_blocked(net_a, front_x, py_a, m2_by_x_bucket):
        return

    incoming_m1 = [
        w
        for w in net_a.get("wires", [])
        if w.get("layer") == "M1" and w["y1"] == py_a and w["y2"] == py_a
    ]
    if not any(max(w["x1"], w["x2"]) >= gx for w in incoming_m1):
        return

    start_x = min(min(w["x1"], w["x2"]) for w in incoming_m1)
    end_x = max(max(w["x1"], w["x2"]) for w in incoming_m1)
    m2_feeder = _find_m2_at_track(net_a, start_x, py_a)
    if not m2_feeder:
        return

    if _is_pin_b_relocation_blocked(net_a, py_a, py_b, start_x, end_x, m2_by_x_bucket, m1_by_y):
        return

    _apply_pin_a_to_b_relocation(
        cname, net_a, m2_feeder, incoming_m1, py_a, py_b, start_x, end_x, m1_by_y, conn_map
    )


def relocate_blocked_splitter_inputs(def_data: JsonDict) -> None:
    """Relocate splitter inputs from blocked pin a to pin b when pin a is crossed.

    Factorio splitters accept items equally on either input lane (a at y=0.5 or
    b at y=1.5). When a routing net crossing occurs directly in front of pin a,
    an underground jumper cannot resolve it because the splitter body blocks the
    exit hood. Shift the input connection from pin a to pin b to move the
    surface crossing away from the splitter body so that underground jumpers
    resolve cleanly.
    """
    units = int(def_data.get("units", DEFAULT_UNITS))
    conn_map = _index_inst_pin_nets(def_data)
    m2_by_x_bucket, m1_by_y = _index_wires_for_relocation(def_data)
    for c in def_data.get("components", []):
        _relocate_single_splitter_input(c, units, conn_map, m2_by_x_bucket, m1_by_y)
