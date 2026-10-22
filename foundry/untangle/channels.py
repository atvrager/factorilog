"""Channel separation and corner untangling for DEF layouts."""

from __future__ import annotations

import collections
from typing import Any

try:
    from foundry.untangle.spatial import (
        SpatialWireIndex,
        _get_net_fluid,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )
except ImportError:
    from untangle.spatial import (
        SpatialWireIndex,
        _get_net_fluid,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )

JsonDict = dict[str, Any]

DEFAULT_UNITS = 1000
TERMINAL_WIRE_TOLERANCE = 1000
FEEDER_X_TOLERANCE = 250
MERGER_X_OFFSET_TILES = 2
PIN_A_Y_OFFSET = 0.5
PIN_B_Y_OFFSET = 1.5
PIN_X_IN_OFFSET = 0.25
PIN_X_OUT_OFFSET = 0.75
PUMP_PIN_A_Y_OFFSET = 0.25
PUMP_PIN_Y_Y_OFFSET = 1.75
PUMP_DEFAULT_Y_OFFSET = 1.0
DEFAULT_PIN_OFFSET = 0.5
FEEDER_X_OFFSET = 0.5
CORNER_SHIFT_MAX_STEP = 6
BELT_SHORT_EXTENT = 1000
BELT_LONG_EXTENT = 2000
DEFAULT_MACHINE_EXTENT = 5000
OIL_REFINERY_EXTENT = 7000
MAX_SEPARATION_PASSES = 10
M1_SHIFT_STEPS = (2, 3, 4, 5, 6, 1, -2, -3, -4, -5, -6, -1)
M2_SHIFT_STEPS = (1, -1, 2, -2, 3, -3, 4, -4, 5, -5)
M2_POS_SHIFT_STEPS = (1, 2, 3, 4, 5)
M2_NEG_SHIFT_STEPS = (-1, -2, -3, -4, -5)
TURN_CLEARANCE_TILES = 2
RECT_X2_INDEX = 2
RECT_Y2_INDEX = 3
RECT_MIDPOINT_DIVISOR = 2.0
DEFAULT_CELL_EXTENT = 3.0
SIGNAL_ENDPOINT_COUNT = 2
MAX_TRACK_CANDIDATES = 30
HORIZONTAL_ORIENTS = ("W", "E")
BELT_TWO_TILE_MACROS = ("belt_split2", "belt_merge2", "belt_balancer2")

MERGE_PIN_OFFSETS: dict[str, tuple[tuple[float, float], str]] = {
    "a": ((PIN_X_IN_OFFSET, PIN_A_Y_OFFSET), "W"),
    "b": ((PIN_X_IN_OFFSET, PIN_B_Y_OFFSET), "W"),
    "y": ((PIN_X_OUT_OFFSET, PIN_A_Y_OFFSET), "E"),
}
SPLIT_PIN_OFFSETS: dict[str, tuple[tuple[float, float], str]] = {
    "a": ((PIN_X_IN_OFFSET, PIN_A_Y_OFFSET), "W"),
    "b": ((PIN_X_IN_OFFSET, PIN_B_Y_OFFSET), "W"),
    "y1": ((PIN_X_OUT_OFFSET, PIN_A_Y_OFFSET), "E"),
    "y2": ((PIN_X_OUT_OFFSET, PIN_B_Y_OFFSET), "E"),
}
BALANCER_PIN_OFFSETS: dict[str, tuple[tuple[float, float], str]] = {
    "a1": ((PIN_X_IN_OFFSET, PIN_A_Y_OFFSET), "W"),
    "a2": ((PIN_X_IN_OFFSET, PIN_B_Y_OFFSET), "W"),
    "y1": ((PIN_X_OUT_OFFSET, PIN_A_Y_OFFSET), "E"),
    "y2": ((PIN_X_OUT_OFFSET, PIN_B_Y_OFFSET), "E"),
}
PUMP_PIN_OFFSETS: dict[str, tuple[tuple[float, float], str]] = {
    "a": ((DEFAULT_PIN_OFFSET, PUMP_PIN_A_Y_OFFSET), "S"),
    "y": ((DEFAULT_PIN_OFFSET, PUMP_PIN_Y_Y_OFFSET), "N"),
}
MACRO_PIN_TABLES: dict[
    str, tuple[dict[str, tuple[tuple[float, float], str]], tuple[tuple[float, float], str]]
] = {
    "belt_merge2": (MERGE_PIN_OFFSETS, ((DEFAULT_PIN_OFFSET, DEFAULT_PIN_OFFSET), "E")),
    "belt_split2": (SPLIT_PIN_OFFSETS, ((DEFAULT_PIN_OFFSET, DEFAULT_PIN_OFFSET), "E")),
    "belt_balancer2": (BALANCER_PIN_OFFSETS, ((DEFAULT_PIN_OFFSET, DEFAULT_PIN_OFFSET), "E")),
    "pump": (PUMP_PIN_OFFSETS, ((DEFAULT_PIN_OFFSET, PUMP_DEFAULT_Y_OFFSET), "N")),
}


def _merger_edge_for_net(
    net: JsonDict, mergers: dict[str, JsonDict]
) -> tuple[str, str, str] | None:
    """Return (src_merger, dst_merger, dst_pin) when a net connects two mergers."""
    conns = net.get("connections", [])
    src = next((inst for inst, pin in conns if pin == "y" and inst in mergers), None)
    if not src:
        return None
    for inst, pin in conns:
        if pin in ("a", "b") and inst in mergers:
            return src, inst, pin
    return None


def _build_merger_parent_map(
    def_data: JsonDict, mergers: dict[str, JsonDict]
) -> dict[str, tuple[str, str, JsonDict]]:
    """Map upstream merger name to (downstream_merger_name, input_pin, connecting_net)."""
    m_parent: dict[str, tuple[str, str, JsonDict]] = {}
    nets = def_data.get("nets", [])
    for n in nets:
        edge = _merger_edge_for_net(n, mergers)
        if edge is not None:
            src, dst, dst_pin = edge
            m_parent[src] = (dst, dst_pin, n)
    return m_parent


def _find_net_with_pin(def_data: JsonDict, inst_name: str, pin_name: str) -> JsonDict | None:
    """Find the first net connected to (inst_name, pin_name)."""
    nets = def_data.get("nets", [])
    for n in nets:
        conns = n.get("connections", [])
        if any(inst == inst_name and pin == pin_name for inst, pin in conns):
            return n
    return None


def _swap_merger_sink_connections(
    net1: JsonDict,
    net2: JsonDict,
    m1_name: str,
    pin1: str,
    m2_name: str,
    pin2: str,
) -> None:
    """Swap the sink connections of net1 and net2 between m1 and m2."""
    raw_conns1 = net1.get("connections", [])
    raw_conns2 = net2.get("connections", [])
    conns1 = [c for c in raw_conns1 if c[0] != m1_name]
    conns2 = [c for c in raw_conns2 if c[0] != m2_name]
    net1["connections"] = [(m2_name, pin2), *conns1]
    net2["connections"] = [(m1_name, pin1), *conns2]


def _replace_wire_endpoint(wire: JsonDict, k1: str, k2: str, old_val: int, new_val: int) -> None:
    """Replace the endpoint coordinate matching old_val with new_val."""
    if wire[k1] == old_val:
        wire[k1] = new_val
    elif wire[k2] == old_val:
        wire[k2] = new_val


def _find_jog_connected_m1_pair(
    wires: list[JsonDict], x_jog: int, y_a: int, y_b: int
) -> tuple[JsonDict, JsonDict] | None:
    """Find the incoming and outgoing M1 wires connected to an M2 jog at x_jog."""
    w_h1_prev = next(
        (
            w
            for w in wires
            if w.get("layer") == "M1"
            and max(w["x1"], w["x2"]) == x_jog
            and (w["y1"] == y_a or w["y1"] == y_b)
        ),
        None,
    )
    if not w_h1_prev:
        return None
    w_h1_inner = next(
        (
            w
            for w in wires
            if w.get("layer") == "M1"
            and min(w["x1"], w["x2"]) == x_jog
            and (w["y1"] == y_a or w["y1"] == y_b)
            and w is not w_h1_prev
        ),
        None,
    )
    if not w_h1_inner:
        return None
    return w_h1_prev, w_h1_inner


def _is_crossing_feeder_m2(w: JsonDict, w_h1_inner: JsonDict, y_inner: int, y_outer: int) -> bool:
    """Return True if wire w is an M2 feeder crossing y_inner and spanning y_outer..y_inner."""
    if w.get("layer") != "M2":
        return False
    if not (
        min(w_h1_inner["x1"], w_h1_inner["x2"]) < w["x1"] < max(w_h1_inner["x1"], w_h1_inner["x2"])
    ):
        return False
    w_min_y, w_max_y = min(w["y1"], w["y2"]), max(w["y1"], w["y2"])
    if not (w_min_y < y_inner < w_max_y):
        return False
    return w_min_y <= min(y_inner, y_outer) and w_max_y >= max(y_inner, y_outer)


def _match_case_a_wires(
    net1: JsonDict, net2: JsonDict, w_jog: JsonDict, m2_x_min: int
) -> tuple[JsonDict, JsonDict, JsonDict, JsonDict] | None:
    """Match the wire segments for a Case A track-swap crossing."""
    if w_jog.get("layer") != "M2":
        return None
    wires1 = net1.get("wires", [])
    m1_pair = _find_jog_connected_m1_pair(wires1, w_jog["x1"], w_jog["y1"], w_jog["y2"])
    if not m1_pair:
        return None
    w_h1_prev, w_h1_inner = m1_pair
    y_outer = w_h1_prev["y1"]
    y_inner = w_h1_inner["y1"]

    wires2 = net2.get("wires", [])
    w_feeder2 = next(
        (w for w in wires2 if _is_crossing_feeder_m2(w, w_h1_inner, y_inner, y_outer)),
        None,
    )
    if not w_feeder2:
        return None

    w_h2_outer = next(
        (
            w
            for w in wires2
            if w.get("layer") == "M1"
            and w["y1"] == y_outer
            and min(w["x1"], w["x2"]) == w_feeder2["x1"]
            and max(w["x1"], w["x2"]) >= m2_x_min
        ),
        None,
    )
    if not w_h2_outer:
        return None
    return w_h1_prev, w_h1_inner, w_feeder2, w_h2_outer


def _wire_near_x(w: JsonDict, target_x: int) -> bool:
    """Return True if either x endpoint of wire w is within TERMINAL_WIRE_TOLERANCE of target_x."""
    return (
        abs(w.get("x1", 0) - target_x) <= TERMINAL_WIRE_TOLERANCE
        or abs(w.get("x2", 0) - target_x) <= TERMINAL_WIRE_TOLERANCE
    )


def _apply_case_a_swap(
    net1: JsonDict,
    net2: JsonDict,
    w_jog: JsonDict,
    matched: tuple[JsonDict, JsonDict, JsonDict, JsonDict],
    m1_name: str,
    pin1: str,
    m2_name: str,
    pin2: str,
) -> None:
    """Reroute wires and swap sink connections for a Case A track-swap crossing."""
    w_h1_prev, w_h1_inner, w_feeder2, w_h2_outer = matched
    x_jog = w_jog["x1"]
    y_outer = w_h1_prev["y1"]
    y_inner = w_h1_inner["y1"]
    x_feeder = w_feeder2["x1"]
    max_x_outer = max(w_h2_outer["x1"], w_h2_outer["x2"])
    max_x_inner = max(w_h1_inner["x1"], w_h1_inner["x2"])

    if w_h1_prev["x1"] == x_jog:
        w_h1_prev["x1"] = max_x_outer
    else:
        w_h1_prev["x2"] = max_x_outer

    raw_wires1 = net1.get("wires", [])
    wires1 = [w for w in raw_wires1 if w is not w_jog and w is not w_h1_inner]
    wires2 = list(net2.get("wires", []))

    term_wires_net2 = [
        w
        for w in wires2
        if w is not w_h2_outer and w is not w_feeder2 and _wire_near_x(w, max_x_outer)
    ]
    term2_ids = {id(w) for w in term_wires_net2}
    wires2 = [w for w in wires2 if id(w) not in term2_ids]
    wires1.extend(term_wires_net2)

    term_wires_net1 = [
        w
        for w in wires1
        if w is not w_h1_prev
        and w is not w_jog
        and w is not w_h1_inner
        and _wire_near_x(w, max_x_inner)
    ]
    term1_ids = {id(w) for w in term_wires_net1}
    net1["wires"] = [w for w in wires1 if id(w) not in term1_ids]
    wires2.extend(term_wires_net1)

    net2["wires"] = [w for w in wires2 if w is not w_h2_outer]
    _replace_wire_endpoint(w_feeder2, "y1", "y2", y_outer, y_inner)
    _replace_wire_endpoint(w_h1_inner, "x1", "x2", x_jog, x_feeder)
    net2["wires"].append(w_h1_inner)

    _swap_merger_sink_connections(net1, net2, m1_name, pin1, m2_name, pin2)


def _find_case_a_match(
    net1: JsonDict, net2: JsonDict, m2_x_min: int
) -> tuple[JsonDict, tuple[JsonDict, JsonDict, JsonDict, JsonDict]] | None:
    """Find the first M2 jog in net1 that matches a Case A crossing with net2."""
    wires1 = net1.get("wires", [])
    for w_jog in wires1:
        matched = _match_case_a_wires(net1, net2, w_jog, m2_x_min)
        if matched is not None:
            return w_jog, matched
    return None


def _try_untangle_merge_case_a(
    net1: JsonDict,
    net2: JsonDict,
    m1_name: str,
    pin1: str,
    m2: JsonDict,
    m2_name: str,
    pin2: str,
    units: int,
) -> bool:
    """Attempt Case A track-swap untangling between net1 and net2."""
    m2_x_min = m2["x"] - MERGER_X_OFFSET_TILES * units
    found = _find_case_a_match(net1, net2, m2_x_min)
    if found is None:
        return False
    w_jog, matched = found
    _apply_case_a_swap(net1, net2, w_jog, matched, m1_name, pin1, m2_name, pin2)
    return True


def _merger_pin_coords(merger: JsonDict, pin: str, units: int) -> tuple[int, int, int]:
    """Calculate (pin_y, pin_x, x_feeder) for a merger input pin."""
    y_off = PIN_A_Y_OFFSET if pin == "a" else PIN_B_Y_OFFSET
    pin_y = round(merger["y"] + y_off * units)
    pin_x = round(merger["x"] + PIN_X_IN_OFFSET * units)
    x_feeder = round(merger["x"] - FEEDER_X_OFFSET * units)
    return pin_y, pin_x, x_feeder


def _find_case_b_triggers(
    net1: JsonDict, net2: JsonDict, pin1_y: int, pin1_x: int, x_feeder1: int
) -> tuple[JsonDict, JsonDict] | None:
    """Find (w_v1, w_src2) when net1 feeder drops across net2 source on row pin1_y."""
    wires1 = net1.get("wires", [])
    w_v1 = next(
        (
            w
            for w in wires1
            if w.get("layer") == "M2"
            and (w["y1"] == pin1_y or w["y2"] == pin1_y)
            and abs(w["x1"] - x_feeder1) <= FEEDER_X_TOLERANCE
        ),
        None,
    )
    if not w_v1:
        return None

    wires2 = net2.get("wires", [])
    w_src2 = next(
        (
            w
            for w in wires2
            if w.get("layer") == "M1"
            and w["y1"] == pin1_y
            and w["y2"] == pin1_y
            and max(w["x1"], w["x2"]) <= pin1_x
            and min(w["x1"], w["x2"]) < x_feeder1
        ),
        None,
    )
    if not w_src2:
        return None

    v1_min_y, v1_max_y = min(w_v1["y1"], w_v1["y2"]), max(w_v1["y1"], w_v1["y2"])
    has_bypass = any(
        w.get("layer") == "M1"
        and min(w["x1"], w["x2"]) <= x_feeder1 <= max(w["x1"], w["x2"])
        and v1_min_y <= w["y1"] <= v1_max_y
        for w in wires2
    )
    if not has_bypass:
        return None
    return w_v1, w_src2


def _is_m2_terminal_wire(w: JsonDict, pin2_y: int, pin2_x: int, x_feeder2: int) -> bool:
    """Return True if wire w is part of the terminal connection into M2 pin2."""
    if w["y1"] == pin2_y and w["y2"] == pin2_y and max(w["x1"], w["x2"]) >= pin2_x:
        return True
    return (
        w.get("layer") == "M2"
        and (w["y1"] == pin2_y or w["y2"] == pin2_y)
        and abs(w["x1"] - x_feeder2) <= FEEDER_X_TOLERANCE
    )


def _reroute_net1_to_m2_terminal(
    net1: JsonDict,
    w_v1: JsonDict,
    w_pin1: JsonDict | None,
    w_term_m2: list[JsonDict],
    pin1_y: int,
    pin2_y: int,
    x_feeder2: int,
) -> None:
    """Extend net1 outer horizontal track to M2 terminal wires and remove old M1 feeder."""
    w_v2_dest = next((w for w in w_term_m2 if w.get("layer") == "M2"), None)
    dest_x = w_v2_dest["x1"] if w_v2_dest else x_feeder2
    y_outer_1 = w_v1["y2"] if w_v1["y1"] == pin1_y else w_v1["y1"]

    wires1 = net1.get("wires", [])
    w_h1_outer = next(
        (
            w
            for w in wires1
            if w.get("layer") == "M1"
            and w["y1"] == y_outer_1
            and (w["x1"] == w_v1["x1"] or w["x2"] == w_v1["x1"])
        ),
        None,
    )
    if w_h1_outer:
        if w_h1_outer["x1"] == w_v1["x1"]:
            w_h1_outer["x1"] = dest_x
        else:
            w_h1_outer["x2"] = dest_x

    net1["wires"] = [w for w in net1["wires"] if w is not w_v1 and w is not w_pin1]
    if w_v2_dest:
        if w_v2_dest["y1"] == pin2_y:
            w_v2_dest["y2"] = y_outer_1
        else:
            w_v2_dest["y1"] = y_outer_1
        net1["wires"].extend(w_term_m2)


def _try_untangle_merge_case_b(
    net1: JsonDict,
    net2: JsonDict,
    m1: JsonDict,
    m1_name: str,
    pin1: str,
    m2: JsonDict,
    m2_name: str,
    pin2: str,
    units: int,
) -> bool:
    """Attempt Case B feeder-across-source untangling between net1 and net2."""
    pin1_y, pin1_x, x_feeder1 = _merger_pin_coords(m1, pin1, units)
    triggers = _find_case_b_triggers(net1, net2, pin1_y, pin1_x, x_feeder1)
    if triggers is None:
        return False
    w_v1, w_src2 = triggers

    wires1 = net1.get("wires", [])
    w_pin1 = next(
        (
            w
            for w in wires1
            if w.get("layer") == "M1" and w["y1"] == pin1_y and max(w["x1"], w["x2"]) >= pin1_x
        ),
        None,
    )
    pin_in_x = max(w_pin1["x1"], w_pin1["x2"]) if w_pin1 else pin1_x
    if w_src2["x1"] < w_src2["x2"]:
        w_src2["x2"] = pin_in_x
    else:
        w_src2["x1"] = pin_in_x

    bypass_wires = [w for w in net2.get("wires", []) if w is not w_src2]
    net2["wires"] = [w_src2]

    pin2_y, pin2_x, x_feeder2 = _merger_pin_coords(m2, pin2, units)
    w_term_m2 = [w for w in bypass_wires if _is_m2_terminal_wire(w, pin2_y, pin2_x, x_feeder2)]
    _reroute_net1_to_m2_terminal(net1, w_v1, w_pin1, w_term_m2, pin1_y, pin2_y, x_feeder2)
    _swap_merger_sink_connections(net1, net2, m1_name, pin1, m2_name, pin2)
    return True


def _untangle_merger_pair_sinks(
    def_data: JsonDict,
    mergers: dict[str, JsonDict],
    m1_name: str,
    m2_name: str,
    pin_in: str,
    net_mid: JsonDict,
    units: int,
) -> None:
    """Check and untangle crossing input nets for a parent-child merger pair."""
    m1 = mergers[m1_name]
    m2 = mergers[m2_name]
    pin2 = "b" if pin_in == "a" else "a"
    net2 = _find_net_with_pin(def_data, m2_name, pin2)
    if not net2:
        return

    for pin1 in ("a", "b"):
        net1 = _find_net_with_pin(def_data, m1_name, pin1)
        if not net1 or net1 is net2 or net1 is net_mid:
            continue
        if _try_untangle_merge_case_a(net1, net2, m1_name, pin1, m2, m2_name, pin2, units):
            break
        if _try_untangle_merge_case_b(net1, net2, m1, m1_name, pin1, m2, m2_name, pin2, units):
            break


def untangle_merge_tree_sinks(def_data: JsonDict, _netlist_data: JsonDict | None = None) -> None:
    """Untangle commutative merge tree sink nets whose physical routes cross.

    In a binary merge tree, all mergers are associative and commutative.
    When an upstream merger M1 feeds a downstream merger M2, their input nets
    carry identical materials. If OpenROAD routes an input net of M1 across
    an input net of M2 (either due to track swapping or source/feeder overlap),
    swapping the sink connections and rerouting the segments along the open
    planar channels eliminates the crossing before jumper synthesis.
    """
    units = int(def_data.get("units", DEFAULT_UNITS))
    mergers = {
        c["name"]: c for c in def_data.get("components", []) if c.get("macro") == "belt_merge2"
    }
    if not mergers:
        return

    m_parent = _build_merger_parent_map(def_data, mergers)
    for m1_name, (m2_name, pin_in, net_mid) in list(m_parent.items()):
        _untangle_merger_pair_sinks(def_data, mergers, m1_name, m2_name, pin_in, net_mid, units)


def _is_corner_pair(w_m1: JsonDict, w_m2: JsonDict) -> bool:
    """Return True if horizontal M1 wire w_m1 and vertical M2 wire w_m2 meet at a corner."""
    if w_m1.get("layer") != "M1" or w_m2.get("layer") != "M2":
        return False
    y1 = w_m1["y1"]
    x1 = w_m2["x1"]
    if w_m1["y2"] != y1 or w_m2["x2"] != x1:
        return False
    return (w_m1["x1"] == x1 or w_m1["x2"] == x1) and (w_m2["y1"] == y1 or w_m2["y2"] == y1)


def _are_interlocking_corners(
    w1_m1: JsonDict,
    w1_m2: JsonDict,
    w2_m1: JsonDict,
    w2_m2: JsonDict,
    units: int,
) -> bool:
    """Return True if two corner wire pairs form a 2x2 interlocking double-crossing."""
    if not _is_corner_pair(w2_m1, w2_m2):
        return False
    x1, y1 = w1_m2["x1"], w1_m1["y1"]
    x2, y2 = w2_m2["x1"], w2_m1["y1"]
    if abs(x1 - x2) != units or abs(y1 - y2) != units:
        return False
    n1_cross_m2 = min(w1_m2["y1"], w1_m2["y2"]) <= y2 <= max(w1_m2["y1"], w1_m2["y2"])
    n1_cross_m1 = min(w1_m1["x1"], w1_m1["x2"]) <= x2 <= max(w1_m1["x1"], w1_m1["x2"])
    n2_cross_m1 = min(w2_m1["x1"], w2_m1["x2"]) <= x1 <= max(w2_m1["x1"], w2_m1["x2"])
    n2_cross_m2 = min(w2_m2["y1"], w2_m2["y2"]) <= y1 <= max(w2_m2["y1"], w2_m2["y2"])
    return n1_cross_m2 and n1_cross_m1 and n2_cross_m1 and n2_cross_m2


def _corner_component_size(c: JsonDict) -> tuple[int, int]:
    """Return component (width, height) used for corner shift obstruction checks."""
    orient = c.get("orient", "N")
    macro = c.get("macro", "")
    if macro in BELT_TWO_TILE_MACROS:
        if orient in HORIZONTAL_ORIENTS:
            return BELT_LONG_EXTENT, BELT_SHORT_EXTENT
        return BELT_SHORT_EXTENT, BELT_LONG_EXTENT
    return DEFAULT_MACHINE_EXTENT, DEFAULT_MACHINE_EXTENT


def _corner_shift_hits_component(
    def_data: JsonDict, x_new: int, y_m2_start: int, y_m2_end: int
) -> bool:
    """Return True if vertical segment (x_new, y_m2_start..y_m2_end) hits a component."""
    comps = def_data.get("components", [])
    for c in comps:
        cx = c.get("x", 0)
        cy = c.get("y", 0)
        cw, ch = _corner_component_size(c)
        if (cx <= x_new <= cx + cw) and not (cy + ch <= y_m2_start or cy >= y_m2_end):
            return True
    return False


def _corner_shift_hits_m2(
    def_data: JsonDict, n2: JsonDict, x_new: int, y_m2_start: int, y_m2_end: int
) -> bool:
    """Return True if vertical segment (x_new, y_m2_start..y_m2_end) overlaps another net on M2."""
    nets = def_data.get("nets", [])
    for on in nets:
        if on is n2:
            continue
        for ow in on.get("wires", []):
            if (
                ow.get("layer") == "M2"
                and ow["x1"] == x_new
                and ow["x2"] == x_new
                and not (
                    max(ow["y1"], ow["y2"]) <= y_m2_start or min(ow["y1"], ow["y2"]) >= y_m2_end
                )
            ):
                return True
    return False


def _corner_shift_hits_m1(
    def_data: JsonDict, n2: JsonDict, y_other: int, ext_min_x: int, ext_max_x: int
) -> bool:
    """Return True if horizontal segment (ext_min_x..ext_max_x, y_other) overlaps another M1 wire."""
    nets = def_data.get("nets", [])
    for on in nets:
        if on is n2:
            continue
        for ow in on.get("wires", []):
            if (
                ow.get("layer") == "M1"
                and ow["y1"] == y_other
                and ow["y2"] == y_other
                and max(min(ow["x1"], ow["x2"]), ext_min_x)
                < min(max(ow["x1"], ow["x2"]), ext_max_x)
            ):
                return True
    return False


def _apply_corner_shift(
    n2: JsonDict,
    w2_m1: JsonDict,
    w2_m2: JsonDict,
    w2_other_m1: JsonDict,
    x2: int,
    x_new: int,
    y2: int,
    y_other: int,
) -> None:
    """Update wire and via coordinates of n2 to shift its corner from x2 to x_new."""
    w2_m2["x1"] = x_new
    w2_m2["x2"] = x_new
    if w2_m1["x1"] == x2:
        w2_m1["x1"] = x_new
    else:
        w2_m1["x2"] = x_new
    if w2_other_m1["x1"] == x2:
        w2_other_m1["x1"] = x_new
    else:
        w2_other_m1["x2"] = x_new
    vias = n2.get("vias", [])
    for v in vias:
        if v.get("x") == x2 and v.get("y") in (y2, y_other):
            v["x"] = x_new


def _find_valid_corner_shift_wire(
    def_data: JsonDict,
    n2: JsonDict,
    x2: int,
    x_new: int,
    y_m2_start: int,
    y_m2_end: int,
    y_other: int,
) -> JsonDict | None:
    """Return the connecting M1 wire at y_other when shifting n2's corner to x_new is clear."""
    if _corner_shift_hits_component(def_data, x_new, y_m2_start, y_m2_end):
        return None
    if _corner_shift_hits_m2(def_data, n2, x_new, y_m2_start, y_m2_end):
        return None
    wires2 = n2.get("wires", [])
    w2_other_m1 = next(
        (
            w
            for w in wires2
            if w.get("layer") == "M1"
            and w["y1"] == y_other
            and w["y2"] == y_other
            and (w["x1"] == x2 or w["x2"] == x2)
        ),
        None,
    )
    if not w2_other_m1:
        return None
    if _corner_shift_hits_m1(def_data, n2, y_other, min(x2, x_new), max(x2, x_new)):
        return None
    return w2_other_m1


def _try_shift_interlocking_corner(
    def_data: JsonDict,
    n2: JsonDict,
    x1: int,
    w2_m1: JsonDict,
    w2_m2: JsonDict,
    units: int,
) -> None:
    """Try shifting n2's vertical corner segment away from x1 to eliminate a 2x2 loop."""
    x2 = w2_m2["x1"]
    y2 = w2_m1["y1"]
    other_x_n2 = w2_m1["x1"] if w2_m1["x2"] == x2 else w2_m1["x2"]
    step_x = -units if other_x_n2 < x1 else units
    y_m2_start = min(w2_m2["y1"], w2_m2["y2"])
    y_m2_end = max(w2_m2["y1"], w2_m2["y2"])
    y_other = w2_m2["y1"] if w2_m2["y2"] == y2 else w2_m2["y2"]

    for k in range(1, CORNER_SHIFT_MAX_STEP):
        x_new = x1 + k * step_x
        if (step_x < 0 and x_new < min(other_x_n2, x2)) or (
            step_x > 0 and x_new > max(other_x_n2, x2)
        ):
            break
        w2_other_m1 = _find_valid_corner_shift_wire(
            def_data, n2, x2, x_new, y_m2_start, y_m2_end, y_other
        )
        if w2_other_m1 is not None:
            _apply_corner_shift(n2, w2_m1, w2_m2, w2_other_m1, x2, x_new, y2, y_other)
            break


def _untangle_corner_pair_against_net2(
    def_data: JsonDict,
    n2: JsonDict,
    w1_m1: JsonDict,
    w1_m2: JsonDict,
    units: int,
) -> None:
    """Check all corner wire pairs in n2 against (w1_m1, w1_m2) and untangle if interlocking."""
    wires2 = n2.get("wires", [])
    for w2_m1 in wires2:
        if w2_m1.get("layer") != "M1" or w2_m1["y1"] != w2_m1["y2"]:
            continue
        for w2_m2 in wires2:
            if _are_interlocking_corners(w1_m1, w1_m2, w2_m1, w2_m2, units):
                _try_shift_interlocking_corner(def_data, n2, w1_m2["x1"], w2_m1, w2_m2, units)


def _untangle_net1_corners(
    def_data: JsonDict, nets: list[JsonDict], i: int, n1: JsonDict, units: int
) -> None:
    """Inspect all corner pairs on n1 against subsequent nets in the layout."""
    wires1 = n1.get("wires", [])
    for w1_m1 in wires1:
        if w1_m1.get("layer") != "M1" or w1_m1["y1"] != w1_m1["y2"]:
            continue
        for w1_m2 in wires1:
            if not _is_corner_pair(w1_m1, w1_m2):
                continue
            for j in range(i + 1, len(nets)):
                _untangle_corner_pair_against_net2(def_data, nets[j], w1_m1, w1_m2, units)


def untangle_interlocking_corners(def_data: JsonDict) -> None:
    """Untangle 2x2 interlocking net loops where two nets cross each other twice.

    When two nets turn at adjacent coordinates (X1, Y1) and (X2, Y2) such that
    net1 crosses net2 at (X1, Y2) and net2 crosses net1 at (X2, Y1), shifting
    one net's corner away from the collision to an unblocked parallel track
    eliminates both crossings.
    """
    units = int(def_data.get("units", DEFAULT_UNITS))
    nets = def_data.get("nets", [])
    for i, n1 in enumerate(nets):
        _untangle_net1_corners(def_data, nets, i, n1, units)


def _is_m1_endpoint_wire(w: JsonDict, x: int, ep_y: int) -> bool:
    """Return True if wire w is an M1 segment meeting (x, ep_y)."""
    return w.get("layer") == "M1" and w["y1"] == ep_y and (w["x1"] == x or w["x2"] == x)


def _find_valid_m2_endpoint_wires(
    tgt_net: JsonDict, tgt_wire: JsonDict, x: int, ep1_y: int, ep2_y: int
) -> list[JsonDict] | None:
    """Find M1 endpoint wires for tgt_wire and verify no other wires touch tgt_wire."""
    wires = tgt_net.get("wires", [])
    m1_wires = [
        w for w in wires if _is_m1_endpoint_wire(w, x, ep1_y) or _is_m1_endpoint_wire(w, x, ep2_y)
    ]
    if not m1_wires:
        return None
    has_ep1 = any(_is_m1_endpoint_wire(w, x, ep1_y) for w in m1_wires)
    has_ep2 = any(_is_m1_endpoint_wire(w, x, ep2_y) for w in m1_wires)
    if not (has_ep1 and has_ep2):
        return None

    m1_wire_ids = {id(w) for w in m1_wires}
    touching_tgt = [w for w in wires if w is not tgt_wire and wires_touch(w, tgt_wire)]
    if any(id(w) not in m1_wire_ids for w in touching_tgt):
        return None
    return m1_wires


def _v_segment_hits_component(
    def_data: JsonDict,
    x_new: int,
    tgt_min_y: int,
    tgt_max_y: int,
    units: int,
    netlist_data: JsonDict | None,
) -> bool:
    """Return True if vertical segment (x_new, tgt_min_y..tgt_max_y) intersects a component."""
    comps = def_data.get("components", [])
    for c in comps:
        cx, cy, cw, ch = get_component_box(c, units, netlist_data)
        if (cx <= x_new <= cx + cw) and not (cy + ch <= tgt_min_y or cy >= tgt_max_y):
            return True
    return False


def _same_track_interval_conflict(
    min1: int, max1: int, min2: int, max2: int, on_diff_fluid: bool, units: int
) -> bool:
    """Return True if two intervals on the same track overlap or violate fluid spacing."""
    if max(min1, min2) < min(max1, max2):
        return True
    if not on_diff_fluid:
        return False
    return min(abs(min1 - max2), abs(min2 - max1)) <= units


def _parallel_track_interval_conflict(
    track1: int,
    min1: int,
    max1: int,
    track2: int,
    min2: int,
    max2: int,
    on_diff_fluid: bool,
    units: int,
) -> bool:
    """Return True if two parallel intervals conflict on identical or adjacent tracks."""
    if track1 == track2:
        return _same_track_interval_conflict(min1, max1, min2, max2, on_diff_fluid, units)
    if on_diff_fluid and abs(track1 - track2) <= units:
        return max(min1, min2) <= min(max1, max2)
    return False


def _m2_wire_conflicts_at_x(
    ow: JsonDict,
    x_new: int,
    tgt_min_y: int,
    tgt_max_y: int,
    on_diff_fluid: bool,
    units: int,
) -> bool:
    """Return True if vertical M2 wire ow conflicts with (x_new, tgt_min_y..tgt_max_y)."""
    if ow.get("layer") != "M2" or ow.get("x1") != ow.get("x2"):
        return False
    return _parallel_track_interval_conflict(
        ow["x1"],
        min(ow["y1"], ow["y2"]),
        max(ow["y1"], ow["y2"]),
        x_new,
        tgt_min_y,
        tgt_max_y,
        on_diff_fluid,
        units,
    )


def _v_segment_hits_other_nets(
    def_data: JsonDict,
    tgt_net: JsonDict,
    x_new: int,
    tgt_min_y: int,
    tgt_max_y: int,
    f_tgt: str | None,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Return True if vertical segment (x_new, tgt_min_y..tgt_max_y) conflicts with other nets."""
    nets = def_data.get("nets", [])
    for on in nets:
        if on is tgt_net:
            continue
        of = _get_net_fluid(on) if _is_fluid_net(on, fluids=fluids) else None
        on_diff_fluid = f_tgt is not None and of is not None and f_tgt != of
        for ow in on.get("wires", []):
            if _m2_wire_conflicts_at_x(ow, x_new, tgt_min_y, tgt_max_y, on_diff_fluid, units):
                return True
    return False


def _h_span_hits_component(
    def_data: JsonDict,
    my: int,
    x_span_min: int,
    x_span_max: int,
    units: int,
    netlist_data: JsonDict | None,
) -> bool:
    """Return True if horizontal span (x_span_min..x_span_max, my) intersects a component."""
    comps = def_data.get("components", [])
    for c in comps:
        cx, cy, cw, ch = get_component_box(c, units, netlist_data)
        if (cy <= my <= cy + ch) and not (cx + cw <= x_span_min or cx >= x_span_max):
            return True
    return False


def _is_overlapping_m1_at_y(ow: JsonDict, my: int, x_span_min: int, x_span_max: int) -> bool:
    """Return True if horizontal M1 wire ow on row my overlaps x_span_min..x_span_max."""
    if ow.get("layer") != "M1" or ow.get("y1") != my or ow.get("y2") != my:
        return False
    return max(min(ow["x1"], ow["x2"]), x_span_min) < min(max(ow["x1"], ow["x2"]), x_span_max)


def _h_span_hits_other_m1(
    def_data: JsonDict, tgt_net: JsonDict, my: int, x_span_min: int, x_span_max: int
) -> bool:
    """Return True if horizontal span (x_span_min..x_span_max, my) overlaps another M1 wire."""
    nets = def_data.get("nets", [])
    for on in nets:
        if on is tgt_net:
            continue
        if any(
            _is_overlapping_m1_at_y(ow, my, x_span_min, x_span_max) for ow in on.get("wires", [])
        ):
            return True
    return False


def _m1_extensions_blocked(
    def_data: JsonDict,
    tgt_net: JsonDict,
    m1_wires: list[JsonDict],
    x_span_min: int,
    x_span_max: int,
    units: int,
    netlist_data: JsonDict | None,
) -> bool:
    """Return True if any M1 horizontal extension across x_span_min..x_span_max is blocked."""
    for mw in m1_wires:
        my = mw["y1"]
        if _h_span_hits_component(def_data, my, x_span_min, x_span_max, units, netlist_data):
            return True
        if _h_span_hits_other_m1(def_data, tgt_net, my, x_span_min, x_span_max):
            return True
    return False


def _apply_m2_segment_shift(
    tgt_net: JsonDict,
    tgt_wire: JsonDict,
    m1_wires: list[JsonDict],
    x: int,
    x_new: int,
    ep1_y: int,
    ep2_y: int,
) -> None:
    """Update M2 segment, connected M1 segments, and vias to x_new."""
    tgt_wire["x1"] = x_new
    tgt_wire["x2"] = x_new
    for mw in m1_wires:
        _replace_wire_endpoint(mw, "x1", "x2", x, x_new)
    vias = tgt_net.get("vias", [])
    for v in vias:
        if v.get("x") == x and v.get("y") in (ep1_y, ep2_y):
            v["x"] = x_new


def shift_m2_segment(
    def_data: JsonDict,
    tgt_net: JsonDict,
    tgt_wire: JsonDict,
    x_new: int,
    netlist_data: JsonDict | None = None,
    fluids: set[str] | frozenset[str] | None = None,
) -> bool:
    """Shift an M2 vertical wire segment to a new X coordinate.

    Stretches or shortens connecting M1 segments and updates vias.
    Verifies that the new position and extensions do not collide with
    components, other nets, or incompatible fluids.
    """
    units = int(def_data.get("units", DEFAULT_UNITS))
    if netlist_data is None:
        netlist_data = def_data.get("_netlist_data")
    if fluids is None and netlist_data is not None:
        fluids = set(netlist_data.get("fluids", []))

    x = tgt_wire.get("x1", 0)
    if tgt_wire.get("x2") != x or x_new == x:
        return False
    y1, y2 = tgt_wire.get("y1", 0), tgt_wire.get("y2", 0)
    tgt_min_y, tgt_max_y = min(y1, y2), max(y1, y2)
    f_tgt = _get_net_fluid(tgt_net) if _is_fluid_net(tgt_net, fluids=fluids) else None

    m1_wires = _find_valid_m2_endpoint_wires(tgt_net, tgt_wire, x, y1, y2)
    if m1_wires is None:
        return False
    if _v_segment_hits_component(def_data, x_new, tgt_min_y, tgt_max_y, units, netlist_data):
        return False
    if _v_segment_hits_other_nets(
        def_data, tgt_net, x_new, tgt_min_y, tgt_max_y, f_tgt, fluids, units
    ):
        return False
    if _m1_extensions_blocked(
        def_data, tgt_net, m1_wires, min(x, x_new), max(x, x_new), units, netlist_data
    ):
        return False

    _apply_m2_segment_shift(tgt_net, tgt_wire, m1_wires, x, x_new, y1, y2)
    return True


def _are_different_fluids(
    n1: JsonDict, n2: JsonDict, fluids: set[str] | frozenset[str] | None
) -> bool:
    """Return True if both nets carry fluids and their fluid types differ."""
    f1 = _get_net_fluid(n1) if _is_fluid_net(n1, fluids=fluids) else None
    f2 = _get_net_fluid(n2) if _is_fluid_net(n2, fluids=fluids) else None
    return f1 is not None and f2 is not None and f1 != f2


def _m1_wires_conflict(
    n1: JsonDict,
    w1: JsonDict,
    n2: JsonDict,
    w2: JsonDict,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Return True if two horizontal M1 wires overlap or violate fluid spacing."""
    y1, y2 = w1["y1"], w2["y1"]
    w1_min_x, w1_max_x = min(w1["x1"], w1["x2"]), max(w1["x1"], w1["x2"])
    w2_min_x, w2_max_x = min(w2["x1"], w2["x2"]), max(w2["x1"], w2["x2"])
    different_fluids = _are_different_fluids(n1, n2, fluids)

    if y1 == y2:
        if max(w1_min_x, w2_min_x) < min(w1_max_x, w2_max_x):
            return True
        if different_fluids:
            gap = min(abs(w1_min_x - w2_max_x), abs(w2_min_x - w1_max_x))
            return gap <= units
        return False
    if different_fluids and abs(y1 - y2) <= units:
        return max(w1_min_x, w2_min_x) <= min(w1_max_x, w2_max_x)
    return False


def _channel_component_size(c: JsonDict) -> tuple[int, int]:
    """Return component (width, height) used for M1 channel separation checks."""
    orient = c.get("orient", "N")
    macro = c.get("macro", "")
    if macro in BELT_TWO_TILE_MACROS:
        if orient in HORIZONTAL_ORIENTS:
            return BELT_LONG_EXTENT, BELT_SHORT_EXTENT
        return BELT_SHORT_EXTENT, BELT_LONG_EXTENT
    if "oil_refinery" in macro:
        return OIL_REFINERY_EXTENT, OIL_REFINERY_EXTENT
    return DEFAULT_MACHINE_EXTENT, DEFAULT_MACHINE_EXTENT


def _m1_shift_hits_component(def_data: JsonDict, y_new: int, w2_min_x: int, w2_max_x: int) -> bool:
    """Return True if horizontal segment (w2_min_x..w2_max_x, y_new) hits a component."""
    comps = def_data.get("components", [])
    for c in comps:
        cx = c.get("x", 0)
        cy = c.get("y", 0)
        cw, ch = _channel_component_size(c)
        if (cy <= y_new <= cy + ch) and not (cx + cw <= w2_min_x or cx >= w2_max_x):
            return True
    return False


def _m1_wire_conflicts_at_y(
    ow: JsonDict,
    y_new: int,
    w2_min_x: int,
    w2_max_x: int,
    on_diff_fluid: bool,
    units: int,
) -> bool:
    """Return True if horizontal M1 wire ow conflicts with (w2_min_x..w2_max_x, y_new)."""
    if ow.get("layer") != "M1" or ow["y1"] != ow["y2"]:
        return False
    return _parallel_track_interval_conflict(
        ow["y1"],
        min(ow["x1"], ow["x2"]),
        max(ow["x1"], ow["x2"]),
        y_new,
        w2_min_x,
        w2_max_x,
        on_diff_fluid,
        units,
    )


def _m1_shift_hits_other_m1(
    def_data: JsonDict,
    n2: JsonDict,
    f2: str | None,
    y_new: int,
    w2_min_x: int,
    w2_max_x: int,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Return True if shifting n2's M1 wire to y_new conflicts with another net on M1."""
    nets = def_data.get("nets", [])
    for on in nets:
        if on is n2:
            continue
        of = _get_net_fluid(on) if _is_fluid_net(on, fluids=fluids) else None
        on_diff_fluid = f2 is not None and of is not None and f2 != of
        for ow in on.get("wires", []):
            if _m1_wire_conflicts_at_y(ow, y_new, w2_min_x, w2_max_x, on_diff_fluid, units):
                return True
    return False


def _find_valid_m1_endpoint_wires(n2: JsonDict, w2: JsonDict, y2: int) -> list[JsonDict] | None:
    """Find vertical M2 endpoint wires for w2 and verify no other wires in n2 touch w2."""
    ep1_x, ep2_x = w2["x1"], w2["x2"]
    wires2 = n2.get("wires", [])
    m2_wires = [
        w
        for w in wires2
        if w.get("layer") == "M2"
        and (
            (w["x1"] == ep1_x and (w["y1"] == y2 or w["y2"] == y2))
            or (w["x1"] == ep2_x and (w["y1"] == y2 or w["y2"] == y2))
        )
    ]
    if not m2_wires:
        return None
    m2_wire_ids = {id(w) for w in m2_wires}
    touching_w2 = [w for w in wires2 if w is not w2 and wires_touch(w, w2)]
    if any(id(w) not in m2_wire_ids for w in touching_w2):
        return None
    return m2_wires


def _v_span_hits_other_m2(
    def_data: JsonDict, n2: JsonDict, mx: int, y_span_min: int, y_span_max: int
) -> bool:
    """Return True if vertical span (mx, y_span_min..y_span_max) overlaps another net on M2."""
    nets = def_data.get("nets", [])
    for on in nets:
        if on is n2:
            continue
        for ow in on.get("wires", []):
            if (
                ow.get("layer") == "M2"
                and ow["x1"] == mx
                and ow["x2"] == mx
                and max(min(ow["y1"], ow["y2"]), y_span_min)
                < min(max(ow["y1"], ow["y2"]), y_span_max)
            ):
                return True
    return False


def _m2_extensions_hit_other_m2(
    def_data: JsonDict,
    n2: JsonDict,
    m2_wires: list[JsonDict],
    y_span_min: int,
    y_span_max: int,
) -> bool:
    """Return True if any M2 vertical extension across y_span_min..y_span_max hits another M2 wire."""
    wires = tuple(m2_wires)
    for mw in wires:
        if _v_span_hits_other_m2(def_data, n2, mw["x1"], y_span_min, y_span_max):
            return True
    return False


def _apply_m1_segment_shift(
    n2: JsonDict, w2: JsonDict, m2_wires: list[JsonDict], y2: int, y_new: int
) -> None:
    """Update M1 wire w2, connected M2 wires, and vias in n2 from y2 to y_new."""
    ep1_x, ep2_x = w2["x1"], w2["x2"]
    w2["y1"] = y_new
    w2["y2"] = y_new
    for mw in m2_wires:
        _replace_wire_endpoint(mw, "y1", "y2", y2, y_new)
    vias = n2.get("vias", [])
    for v in vias:
        if v.get("y") == y2 and v.get("x") in (ep1_x, ep2_x):
            v["y"] = y_new


def _can_shift_m1_to_y(
    def_data: JsonDict,
    n2: JsonDict,
    w2: JsonDict,
    f2: str | None,
    y1: int,
    y2: int,
    y_new: int,
    different_fluids: bool,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> list[JsonDict] | None:
    """Return connected M2 wires when shifting w2 to y_new is valid and unobstructed."""
    if different_fluids and abs(y_new - y1) <= units:
        return None
    w2_min_x, w2_max_x = min(w2["x1"], w2["x2"]), max(w2["x1"], w2["x2"])
    if _m1_shift_hits_component(def_data, y_new, w2_min_x, w2_max_x):
        return None
    if _m1_shift_hits_other_m1(def_data, n2, f2, y_new, w2_min_x, w2_max_x, fluids, units):
        return None
    m2_wires = _find_valid_m1_endpoint_wires(n2, w2, y2)
    if m2_wires is None:
        return None
    if _m2_extensions_hit_other_m2(def_data, n2, m2_wires, min(y2, y_new), max(y2, y_new)):
        return None
    return m2_wires


def _try_shift_conflicting_m1(
    def_data: JsonDict,
    n1: JsonDict,
    w1: JsonDict,
    n2: JsonDict,
    w2: JsonDict,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Try shifting horizontal M1 wire w2 to a free parallel track."""
    y1, y2 = w1["y1"], w2["y1"]
    different_fluids = _are_different_fluids(n1, n2, fluids)
    f2 = _get_net_fluid(n2) if _is_fluid_net(n2, fluids=fluids) else None

    for step in M1_SHIFT_STEPS:
        y_new = y2 + step * units
        m2_wires = _can_shift_m1_to_y(
            def_data, n2, w2, f2, y1, y2, y_new, different_fluids, fluids, units
        )
        if m2_wires is not None:
            _apply_m1_segment_shift(n2, w2, m2_wires, y2, y_new)
            return True
    return False


def _separate_m1_against_net2(
    def_data: JsonDict,
    n1: JsonDict,
    w1: JsonDict,
    n2: JsonDict,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Check w1 against all M1 wires in n2 and shift the first conflicting wire."""
    for w2 in list(n2.get("wires", [])):
        if w2.get("layer") != "M1" or w2["y1"] != w2["y2"]:
            continue
        if not _m1_wires_conflict(n1, w1, n2, w2, fluids, units):
            continue
        if _try_shift_conflicting_m1(def_data, n1, w1, n2, w2, fluids, units):
            return True
    return False


def _separate_m1_wire_across_nets(
    def_data: JsonDict,
    nets: list[JsonDict],
    i: int,
    n1: JsonDict,
    w1: JsonDict,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Check horizontal M1 wire w1 against all subsequent nets and stop on first shift."""
    for j in range(i + 1, len(nets)):
        if _separate_m1_against_net2(def_data, n1, w1, nets[j], fluids, units):
            return True
    return False


def _run_m1_separation_pass(
    def_data: JsonDict,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Run one pass of horizontal M1 track separation across all nets."""
    any_shifted = False
    nets = def_data.get("nets", [])
    for i, n1 in enumerate(nets):
        for w1 in list(n1.get("wires", [])):
            if w1.get("layer") != "M1" or w1["y1"] != w1["y2"]:
                continue
            if _separate_m1_wire_across_nets(def_data, nets, i, n1, w1, fluids, units):
                any_shifted = True
    return any_shifted


def _shift_crosses_other_wire(
    ow: JsonDict,
    m1_y_set: set[int],
    x_new: int,
    x_span_min: int,
    x_span_max: int,
    tgt_min_y: int,
    tgt_max_y: int,
) -> bool:
    """Return True if wire ow in other_net crosses the shifted M2 segment or its M1 extensions."""
    layer = ow.get("layer")
    if layer == "M1":
        ow_min_x, ow_max_x = min(ow["x1"], ow["x2"]), max(ow["x1"], ow["x2"])
        return ow_min_x < x_new < ow_max_x and tgt_min_y <= ow["y1"] <= tgt_max_y
    if layer == "M2" and x_span_min <= ow["x1"] <= x_span_max:
        ow_min_y, ow_max_y = min(ow["y1"], ow["y2"]), max(ow["y1"], ow["y2"])
        return (tgt_min_y in m1_y_set and ow_min_y <= tgt_min_y <= ow_max_y) or (
            tgt_max_y in m1_y_set and ow_min_y <= tgt_max_y <= ow_max_y
        )
    return False


def _shift_crosses_other_net(
    other_net: JsonDict,
    m1_y_set: set[int],
    x_new: int,
    x_span_min: int,
    x_span_max: int,
    tgt_min_y: int,
    tgt_max_y: int,
) -> bool:
    """Return True if shifting to x_new crosses any wire in other_net."""
    other_wires = other_net.get("wires", [])
    return any(
        _shift_crosses_other_wire(ow, m1_y_set, x_new, x_span_min, x_span_max, tgt_min_y, tgt_max_y)
        for ow in other_wires
    )


def _try_shift_conflicting_m2(
    def_data: JsonDict,
    tgt_net: JsonDict,
    tgt_wire: JsonDict,
    other_net: JsonDict,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
    require_no_crossing: bool,
) -> bool:
    """Try shifting vertical M2 segment tgt_wire to a nearby free track."""
    x = tgt_wire["x1"]
    y1, y2 = tgt_wire["y1"], tgt_wire["y2"]
    tgt_min_y, tgt_max_y = min(y1, y2), max(y1, y2)
    wires = tgt_net.get("wires", [])
    m1_y_set = {
        w["y1"] for w in wires if _is_m1_endpoint_wire(w, x, y1) or _is_m1_endpoint_wire(w, x, y2)
    }
    for step in M2_SHIFT_STEPS:
        x_new = x + step * units
        if require_no_crossing and _shift_crosses_other_net(
            other_net, m1_y_set, x_new, min(x, x_new), max(x, x_new), tgt_min_y, tgt_max_y
        ):
            continue
        if shift_m2_segment(def_data, tgt_net, tgt_wire, x_new, netlist_data, fluids):
            return True
    return False


def _m2_wires_conflict(
    n1: JsonDict,
    w1: JsonDict,
    n2: JsonDict,
    w2: JsonDict,
    fluids: set[str] | frozenset[str] | None,
    units: int,
) -> bool:
    """Return True if two vertical M2 wires overlap or violate fluid spacing."""
    x1, x2 = w1["x1"], w2["x1"]
    w1_min_y, w1_max_y = min(w1["y1"], w1["y2"]), max(w1["y1"], w1["y2"])
    w2_min_y, w2_max_y = min(w2["y1"], w2["y2"]), max(w2["y1"], w2["y2"])
    different_fluids = _are_different_fluids(n1, n2, fluids)

    if x1 == x2:
        if max(w1_min_y, w2_min_y) <= min(w1_max_y, w2_max_y):
            return True
        if different_fluids:
            gap = min(abs(w1_min_y - w2_max_y), abs(w2_min_y - w1_max_y))
            return gap <= units
        return False
    if different_fluids and abs(x1 - x2) <= units:
        return max(w1_min_y, w2_min_y) <= min(w1_max_y, w2_max_y)
    return False


def _resolve_m2_pair_conflict(
    def_data: JsonDict,
    n1: JsonDict,
    w1: JsonDict,
    n2: JsonDict,
    w2: JsonDict,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Try shifting either w1 or w2 to resolve a vertical M2 track conflict."""
    return (
        _try_shift_conflicting_m2(
            def_data, n1, w1, n2, units, netlist_data, fluids, require_no_crossing=True
        )
        or _try_shift_conflicting_m2(
            def_data, n2, w2, n1, units, netlist_data, fluids, require_no_crossing=True
        )
        or _try_shift_conflicting_m2(
            def_data, n1, w1, n2, units, netlist_data, fluids, require_no_crossing=False
        )
        or _try_shift_conflicting_m2(
            def_data, n2, w2, n1, units, netlist_data, fluids, require_no_crossing=False
        )
    )


def _separate_m2_against_net2(
    def_data: JsonDict,
    n1: JsonDict,
    w1: JsonDict,
    n2: JsonDict,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Check vertical M2 wire w1 against all M2 wires in n2 and resolve the first conflict."""
    for w2 in list(n2.get("wires", [])):
        if w2.get("layer") != "M2" or w2["x1"] != w2["x2"]:
            continue
        if not _m2_wires_conflict(n1, w1, n2, w2, fluids, units):
            continue
        if _resolve_m2_pair_conflict(def_data, n1, w1, n2, w2, units, netlist_data, fluids):
            return True
    return False


def _separate_m2_wire_across_nets(
    def_data: JsonDict,
    nets: list[JsonDict],
    i: int,
    n1: JsonDict,
    w1: JsonDict,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Check vertical M2 wire w1 against all subsequent nets and stop on first shift."""
    for j in range(i + 1, len(nets)):
        if _separate_m2_against_net2(def_data, n1, w1, nets[j], units, netlist_data, fluids):
            return True
    return False


def _run_m2_separation_pass(
    def_data: JsonDict,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Run one pass of vertical M2 track separation across all nets."""
    any_shifted = False
    nets = def_data.get("nets", [])
    for i, n1 in enumerate(nets):
        for w1 in list(n1.get("wires", [])):
            if w1.get("layer") != "M2" or w1["x1"] != w1["x2"]:
                continue
            if _separate_m2_wire_across_nets(
                def_data, nets, i, n1, w1, units, netlist_data, fluids
            ):
                any_shifted = True
                break
    return any_shifted


def separate_overlapping_channel_routes(
    def_data: JsonDict,
    netlist_data: JsonDict | None = None,
    fluids: set[str] | frozenset[str] | None = None,
) -> None:
    """Separate parallel or conflicting nets that detailed router placed on colliding tracks.

    Handles both horizontal M1 segments and vertical M2 segments.
    Separates overlapping tracks and prevents adjacent surface fluid mixing.
    """
    if fluids is None and netlist_data is not None:
        fluids = set(netlist_data.get("fluids", []))
    units = int(def_data.get("units", DEFAULT_UNITS))

    for _pass in range(MAX_SEPARATION_PASSES):
        if not _run_m1_separation_pass(def_data, fluids, units):
            break

    for _pass in range(MAX_SEPARATION_PASSES):
        if not _run_m2_separation_pass(def_data, units, netlist_data, fluids):
            break


def _build_m1_by_y_index(
    def_data: JsonDict,
) -> collections.defaultdict[int, list[tuple[JsonDict, JsonDict]]]:
    """Group horizontal M1 wires by their Y coordinate."""
    m1_by_y: collections.defaultdict[int, list[tuple[JsonDict, JsonDict]]] = (
        collections.defaultdict(list)
    )
    nets = def_data.get("nets", [])
    for on in nets:
        for ow in on.get("wires", []):
            if ow.get("layer") == "M1" and ow.get("y1") == ow.get("y2"):
                m1_by_y[ow["y1"]].append((on, ow))
    return m1_by_y


def _x_near_m1_turn(x_coord: int, ow_min_x: int, ow_max_x: int, max_dist: int) -> bool:
    """Return True if x_coord crosses [ow_min_x, ow_max_x] within max_dist of an endpoint."""
    if not (ow_min_x <= x_coord <= ow_max_x):
        return False
    return min(abs(x_coord - ow_min_x), abs(x_coord - ow_max_x)) <= max_dist


def _has_m1_turn_congestion(
    m1_by_y: collections.defaultdict[int, list[tuple[JsonDict, JsonDict]]],
    candidate_ys: list[int],
    ignore_net: JsonDict,
    x_new: int,
    max_dist: int,
) -> bool:
    """Return True if x_new crosses any M1 wire on candidate_ys too close to its turn."""
    ys = tuple(candidate_ys)
    for cy in ys:
        row = m1_by_y.get(cy, ())
        for on, ow in row:
            if on is ignore_net:
                continue
            ox_min, ox_max = min(ow["x1"], ow["x2"]), max(ow["x1"], ow["x2"])
            if _x_near_m1_turn(x_new, ox_min, ox_max, max_dist):
                return True
    return False


def _try_shift_primary_m2_turn(
    def_data: JsonDict,
    m1_by_y: collections.defaultdict[int, list[tuple[JsonDict, JsonDict]]],
    candidate_ys: list[int],
    n1: JsonDict,
    w_m2: JsonDict,
    m1_min_x: int,
    m1_max_x: int,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Candidate 1: Try shifting w_m2 on n1 away from the congested turn crossing."""
    x_m2 = w_m2["x1"]
    max_dist = TURN_CLEARANCE_TILES * units
    for step in M2_SHIFT_STEPS:
        x_new = x_m2 + step * units
        if _x_near_m1_turn(x_new, m1_min_x, m1_max_x, max_dist):
            continue
        if _has_m1_turn_congestion(m1_by_y, candidate_ys, n1, x_new, max_dist):
            continue
        if shift_m2_segment(def_data, n1, w_m2, x_new, netlist_data, fluids):
            return True
    return False


def _try_shift_connected_m2_turn(
    def_data: JsonDict,
    m1_by_y: collections.defaultdict[int, list[tuple[JsonDict, JsonDict]]],
    n2: JsonDict,
    x_m2: int,
    y_m1: int,
    m1_min_x: int,
    m1_max_x: int,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Candidate 2: Try shifting the M2 wire connected to w_m1 on n2."""
    wires2 = n2.get("wires", [])
    w_m2_conn = next(
        (
            w
            for w in wires2
            if w.get("layer") == "M2"
            and (w.get("x1") in (m1_min_x, m1_max_x))
            and w.get("x1") == w.get("x2")
            and (w.get("y1") == y_m1 or w.get("y2") == y_m1)
        ),
        None,
    )
    if not w_m2_conn:
        return False

    ep_x = w_m2_conn["x1"]
    steps = M2_POS_SHIFT_STEPS if ep_x == m1_max_x else M2_NEG_SHIFT_STEPS
    max_dist = TURN_CLEARANCE_TILES * units
    conn_min_y = min(w_m2_conn["y1"], w_m2_conn["y2"])
    conn_max_y = max(w_m2_conn["y1"], w_m2_conn["y2"])
    all_ys = tuple(m1_by_y.keys())
    conn_candidate_ys = [y for y in all_ys if conn_min_y <= y <= conn_max_y]

    for step in steps:
        x_new = ep_x + step * units
        if abs(x_new - x_m2) < max_dist:
            continue
        if _has_m1_turn_congestion(m1_by_y, conn_candidate_ys, n2, x_new, max_dist):
            continue
        if shift_m2_segment(def_data, n2, w_m2_conn, x_new, netlist_data, fluids):
            return True
    return False


def _untangle_m2_against_m1_row(
    def_data: JsonDict,
    m1_by_y: collections.defaultdict[int, list[tuple[JsonDict, JsonDict]]],
    candidate_ys: list[int],
    n1: JsonDict,
    w_m2: JsonDict,
    y_m1: int,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Check w_m2 against all M1 wires at y_m1 and shift to clear turn congestion."""
    x_m2 = w_m2["x1"]
    m2_min_y, m2_max_y = min(w_m2["y1"], w_m2["y2"]), max(w_m2["y1"], w_m2["y2"])
    max_dist = TURN_CLEARANCE_TILES * units
    dist_m2 = min(abs(y_m1 - m2_min_y), abs(y_m1 - m2_max_y))
    if dist_m2 > max_dist:
        return False

    row = m1_by_y.get(y_m1, ())
    for n2, w_m1 in row:
        if n1 is n2:
            continue
        m1_min_x, m1_max_x = min(w_m1["x1"], w_m1["x2"]), max(w_m1["x1"], w_m1["x2"])
        if not _x_near_m1_turn(x_m2, m1_min_x, m1_max_x, max_dist):
            continue
        if _try_shift_primary_m2_turn(
            def_data,
            m1_by_y,
            candidate_ys,
            n1,
            w_m2,
            m1_min_x,
            m1_max_x,
            units,
            netlist_data,
            fluids,
        ):
            return True
        if _try_shift_connected_m2_turn(
            def_data, m1_by_y, n2, x_m2, y_m1, m1_min_x, m1_max_x, units, netlist_data, fluids
        ):
            return True
    return False


def _untangle_m2_wire_turns(
    def_data: JsonDict,
    m1_by_y: collections.defaultdict[int, list[tuple[JsonDict, JsonDict]]],
    n1: JsonDict,
    w_m2: JsonDict,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Check vertical M2 wire w_m2 against all candidate M1 rows."""
    m2_min_y, m2_max_y = min(w_m2["y1"], w_m2["y2"]), max(w_m2["y1"], w_m2["y2"])
    all_ys = tuple(m1_by_y.keys())
    candidate_ys = [y for y in all_ys if m2_min_y <= y <= m2_max_y]
    for y_m1 in candidate_ys:
        if _untangle_m2_against_m1_row(
            def_data, m1_by_y, candidate_ys, n1, w_m2, y_m1, units, netlist_data, fluids
        ):
            return True
    return False


def _run_turn_crossing_pass(
    def_data: JsonDict,
    units: int,
    netlist_data: JsonDict | None,
    fluids: set[str] | frozenset[str] | None,
) -> bool:
    """Run one pass of congested turn-crossing untangling across all nets."""
    any_shifted = False
    m1_by_y = _build_m1_by_y_index(def_data)
    nets = def_data.get("nets", [])
    for n1 in nets:
        for w_m2 in list(n1.get("wires", [])):
            if w_m2.get("layer") != "M2" or w_m2.get("x1") != w_m2.get("x2"):
                continue
            if _untangle_m2_wire_turns(def_data, m1_by_y, n1, w_m2, units, netlist_data, fluids):
                any_shifted = True
                break
    return any_shifted


def untangle_congested_turn_crossings(
    def_data: JsonDict,
    netlist_data: JsonDict | None = None,
) -> None:
    """Untangle crossings that bottleneck within 1 tile of turns.

    When an M2 segment crosses an M1 segment within 1 tile of a turn,
    Factorio underground jumper placement fails because curved turn belts
    cannot serve as underground belt entrance or exit hoods.
    Shifting the M2 segment creates a straight 2-tile runway for the jumper
    or eliminates the crossing completely.
    """
    units = int(def_data.get("units", DEFAULT_UNITS))
    if netlist_data is None:
        netlist_data = def_data.get("_netlist_data")
    fluids = set(netlist_data.get("fluids", [])) if netlist_data else None

    for _pass in range(MAX_SEPARATION_PASSES):
        if not _run_turn_crossing_pass(def_data, units, netlist_data, fluids):
            break


def _cell_port_offset_and_face(
    cell: JsonDict, pin: str, key: str, is_output: bool, default_face: str
) -> tuple[tuple[float, float], str] | None:
    """Return port center offset and face for a cell port list."""
    try:
        from foundry.lef import get_macro_port_geometry
    except ImportError:
        from lef import get_macro_port_geometry

    raw_ports = cell.get(key, [])
    norm_ports = [p.replace("-", "_") for p in raw_ports]
    if pin not in norm_ports:
        return None
    idx = norm_ports.index(pin)
    orig_name = raw_ports[idx] if idx < len(raw_ports) else ""
    if not orig_name:
        return None
    geom = get_macro_port_geometry(cell, orig_name, is_output=is_output)
    r = geom["rect"]
    cx = (r[0] + r[RECT_X2_INDEX]) / RECT_MIDPOINT_DIVISOR
    cy = (r[1] + r[RECT_Y2_INDEX]) / RECT_MIDPOINT_DIVISOR
    return (cx, cy), geom.get("face", default_face)


def _get_pin_offset_and_face(
    macro: str,
    pin: str,
    cell: JsonDict | None = None,
) -> tuple[tuple[float, float], str]:
    """Return ((offset_x, offset_y), face) for a component pin in tiles."""
    if macro in MACRO_PIN_TABLES:
        pin_table, default_val = MACRO_PIN_TABLES[macro]
        return pin_table.get(pin, default_val)

    if cell:
        out_res = _cell_port_offset_and_face(cell, pin, "outputs", is_output=True, default_face="E")
        if out_res is not None:
            return out_res
        in_res = _cell_port_offset_and_face(cell, pin, "inputs", is_output=False, default_face="W")
        if in_res is not None:
            return in_res
        w = float(cell.get("width", DEFAULT_CELL_EXTENT))
        h = float(cell.get("height", DEFAULT_CELL_EXTENT))
        return (w / RECT_MIDPOINT_DIVISOR, h / RECT_MIDPOINT_DIVISOR), "E"

    return (DEFAULT_PIN_OFFSET, DEFAULT_PIN_OFFSET), "E"


def _build_component_tile_buckets(
    def_data: JsonDict, units: int, netlist_data: JsonDict | None
) -> tuple[
    dict[int, list[tuple[str, int, int, int, int]]],
    dict[int, list[tuple[str, int, int, int, int]]],
]:
    """Build X-tile and Y-tile spatial buckets of component bounding boxes."""
    comp_boxes = [
        (c.get("name", ""), *get_component_box(c, units, netlist_data))
        for c in def_data.get("components", [])
    ]
    comp_by_x_tile: dict[int, list[tuple[str, int, int, int, int]]] = {}
    comp_by_y_tile: dict[int, list[tuple[str, int, int, int, int]]] = {}
    for cbox in comp_boxes:
        _cn, cx, cy, cw, ch = cbox
        for tx in range(cx // units, (cx + cw) // units + 1):
            comp_by_x_tile.setdefault(tx, []).append(cbox)
        for ty in range(cy // units, (cy + ch) // units + 1):
            comp_by_y_tile.setdefault(ty, []).append(cbox)
    return comp_by_x_tile, comp_by_y_tile


def _is_component_driver_pin(
    inst: str,
    pin: str,
    comp_map: dict[str, JsonDict],
    inst_map: dict[str, JsonDict],
    cell_map: JsonDict,
) -> bool:
    """Return True if pin is an output driver pin on placed component inst."""
    c = comp_map[inst]
    macro = c.get("macro", "")
    if macro in ("belt_merge2", "pump") and pin == "y":
        return True
    if macro in ("belt_split2", "belt_balancer2") and pin in ("y1", "y2"):
        return True
    cell_id = inst_map.get(inst, {}).get("cell", macro)
    cell = cell_map.get(cell_id)
    if not cell:
        return False
    outputs = [o.replace("-", "_") for o in cell.get("outputs", [])]
    return pin in outputs


def _is_signal_driver(
    inst: str,
    pin: str,
    comp_map: dict[str, JsonDict],
    inst_map: dict[str, JsonDict],
    cell_map: JsonDict,
    pin_dict: dict[str, JsonDict],
) -> bool:
    """Return True if (inst, pin) is the source driver of a signal net."""
    if inst in comp_map:
        return _is_component_driver_pin(inst, pin, comp_map, inst_map, cell_map)
    if inst == "PIN" or inst in pin_dict:
        p = pin_dict.get(pin) if inst == "PIN" else pin_dict.get(inst)
        return bool(p and p.get("direction") == "INPUT")
    return False


def _get_signal_pin_loc(
    inst: str,
    pin: str,
    comp_map: dict[str, JsonDict],
    inst_map: dict[str, JsonDict],
    cell_map: JsonDict,
    pin_dict: dict[str, JsonDict],
    units: int,
) -> tuple[tuple[int, int], str] | None:
    """Return ((x, y), face) for a component pin or top-level IO pin."""
    if inst in comp_map:
        c = comp_map[inst]
        cell_id = inst_map.get(inst, {}).get("cell", c.get("macro", ""))
        cell = cell_map.get(cell_id)
        (off_x, off_y), face = _get_pin_offset_and_face(c.get("macro", ""), pin, cell)
        return (c["x"] + round(off_x * units), c["y"] + round(off_y * units)), face
    if inst == "PIN" or inst in pin_dict:
        p = pin_dict.get(pin) if inst == "PIN" else pin_dict.get(inst)
        if p:
            face = "E" if p.get("direction") == "INPUT" else "W"
            return (p["x"], p["y"]), face
    return None


def _is_signal_h_segment_blocked(
    y: int,
    h_min: int,
    h_max: int,
    inst_src: str,
    inst_dst: str,
    spatial: SpatialWireIndex,
    comp_by_y_tile: dict[int, list[tuple[str, int, int, int, int]]],
    units: int,
) -> bool:
    """Return True if horizontal M1 segment (h_min..h_max, y) hits a wire or component."""
    for ow_x1, ow_x2, _oname, _ in spatial.m1_by_y.get(y, []):
        if max(ow_x1, h_min) < min(ow_x2, h_max):
            return True
    for c_name, cx, cy, cw, ch in comp_by_y_tile.get(y // units, ()):
        if (
            c_name not in (inst_src, inst_dst)
            and cx < h_max
            and cx + cw > h_min
            and cy < y < cy + ch
        ):
            return True
    return False


def _is_signal_v_segment_blocked(
    x_mid: int,
    y_min: int,
    y_max: int,
    spatial: SpatialWireIndex,
    comp_by_x_tile: dict[int, list[tuple[str, int, int, int, int]]],
    units: int,
) -> bool:
    """Return True if vertical M2 segment (x_mid, y_min..y_max) hits a component or M2 wire."""
    for _c_name, cx, cy, cw, ch in comp_by_x_tile.get(x_mid // units, ()):
        if cx < x_mid < cx + cw and not (cy + ch <= y_min or cy >= y_max):
            return True
    for ow_y1, ow_y2, _oname, _ in spatial.m2_by_x.get(x_mid, []):
        if max(ow_y1, y_min) < min(ow_y2, y_max):
            return True
    return False


def _build_z_route_candidate_tracks(x1: int, x2: int, units: int) -> list[int]:
    """Build ordered list of candidate M2 X tracks for Z-shaped routing."""
    min_x, max_x = min(x1, x2), max(x1, x2)
    grid_rem = units // 2
    inside_tracks = [
        x
        for x in range((min_x // units) * units + grid_rem, max_x + units, units)
        if min_x <= x <= max_x
    ]
    inside_tracks.sort(key=lambda x: abs(x - (x1 + x2) / RECT_MIDPOINT_DIVISOR))
    inside_tracks = inside_tracks[:MAX_TRACK_CANDIDATES]

    outside_tracks: list[int] = []
    for i in range(1, MAX_TRACK_CANDIDATES):
        outside_tracks.append(((max_x // units) + i) * units + grid_rem)
        outside_tracks.append(((min_x // units) - i) * units + grid_rem)
    return inside_tracks + outside_tracks


def _faces_allow_mid_track(x1: int, face1: str, x2: int, face2: str, x_mid: int) -> bool:
    """Return True if x_mid respects the exit/entry faces of both pins."""
    if (face1 == "E" and x_mid < x1) or (face1 == "W" and x_mid > x1):
        return False
    return not ((face2 == "W" and x_mid > x2) or (face2 == "E" and x_mid < x2))


def _commit_z_route(
    net: JsonDict,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    x_mid: int,
    spatial: SpatialWireIndex,
) -> None:
    """Store Z-shaped route wires and vias on net and update the spatial index."""
    h1_min, h1_max = min(x1, x_mid), max(x1, x_mid)
    y_min, y_max = min(y1, y2), max(y1, y2)
    h2_min, h2_max = min(x2, x_mid), max(x2, x_mid)

    w_src = {"layer": "M1", "x1": h1_min, "y1": y1, "x2": h1_max, "y2": y1}
    w_mid = {"layer": "M2", "x1": x_mid, "y1": y_min, "x2": x_mid, "y2": y_max}
    w_dst = {"layer": "M1", "x1": h2_min, "y1": y2, "x2": h2_max, "y2": y2}
    net["wires"] = [
        w for w in (w_src, w_mid, w_dst) if not (w["x1"] == w["x2"] and w["y1"] == w["y2"])
    ]
    net["vias"] = [
        {"layer": "M1", "x": x_mid, "y": y1, "via": "SURFACE_TURN_VIA"},
        {"layer": "M1", "x": x_mid, "y": y2, "via": "SURFACE_TURN_VIA"},
    ]

    net_name = net.get("name", "")
    spatial.m1_by_y[y1].append((h1_min, h1_max, net_name, w_src))
    spatial.m2_by_x[x_mid].append((y_min, y_max, net_name, w_mid))
    spatial.m1_by_y[y2].append((h2_min, h2_max, net_name, w_dst))


def _try_route_single_signal_net(
    net: JsonDict,
    src_info: tuple[tuple[int, int], str],
    dst_info: tuple[tuple[int, int], str],
    inst_src: str,
    inst_dst: str,
    spatial: SpatialWireIndex,
    comp_by_x_tile: dict[int, list[tuple[str, int, int, int, int]]],
    comp_by_y_tile: dict[int, list[tuple[str, int, int, int, int]]],
    units: int,
) -> bool:
    """Attempt to route a single point-to-point signal net directly on M1 or via Z-route."""
    (x1, y1), face1 = src_info
    (x2, y2), face2 = dst_info

    if y1 == y2:
        h_min, h_max = min(x1, x2), max(x1, x2)
        if not _is_signal_h_segment_blocked(
            y1, h_min, h_max, inst_src, inst_dst, spatial, comp_by_y_tile, units
        ):
            w = {"layer": "M1", "x1": h_min, "y1": y1, "x2": h_max, "y2": y1}
            net["wires"] = [w]
            spatial.m1_by_y[y1].append((h_min, h_max, net.get("name", ""), w))
            return True

    y_min, y_max = min(y1, y2), max(y1, y2)
    for x_mid in _build_z_route_candidate_tracks(x1, x2, units):
        if not _faces_allow_mid_track(x1, face1, x2, face2, x_mid):
            continue
        if _is_signal_v_segment_blocked(x_mid, y_min, y_max, spatial, comp_by_x_tile, units):
            continue
        if _is_signal_h_segment_blocked(
            y1, min(x1, x_mid), max(x1, x_mid), inst_src, inst_dst, spatial, comp_by_y_tile, units
        ):
            continue
        if _is_signal_h_segment_blocked(
            y2, min(x2, x_mid), max(x2, x_mid), inst_src, inst_dst, spatial, comp_by_y_tile, units
        ):
            continue
        _commit_z_route(net, x1, y1, x2, y2, x_mid, spatial)
        return True
    return False


def _select_driver_and_sink(
    net: JsonDict,
    comp_map: dict[str, JsonDict],
    inst_map: dict[str, JsonDict],
    cell_map: JsonDict,
    pin_dict: dict[str, JsonDict],
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Return (driver_conn, sink_conn) for a two-terminal signal net."""
    c1, c2 = net["connections"][0], net["connections"][1]
    if _is_signal_driver(c2[0], c2[1], comp_map, inst_map, cell_map, pin_dict) and not (
        _is_signal_driver(c1[0], c1[1], comp_map, inst_map, cell_map, pin_dict)
    ):
        return c2, c1
    return c1, c2


def route_unrouted_signal_nets(
    def_data: JsonDict,
    netlist_data: JsonDict | None = None,
) -> int:
    """Synthesize orthogonal routes for unrouted point-to-point signal nets.

    When OpenROAD detailed routing leaves point-to-point signal nets unrouted
    (empty wires list), this pass finds an open channel track between the
    source pin and sink pin, and creates an orthogonal Z-shaped route on
    M1 and M2 layers.

    Returns the number of nets routed.
    """
    unrouted_nets = [
        net
        for net in def_data.get("nets", [])
        if not net.get("wires") and len(net.get("connections", [])) == SIGNAL_ENDPOINT_COUNT
    ]
    if not unrouted_nets:
        return 0

    if netlist_data is None:
        netlist_data = def_data.get("_netlist_data")

    units = int(def_data.get("units", DEFAULT_UNITS))
    spatial = SpatialWireIndex(def_data, units)
    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    cell_map = netlist_data.get("cells", {}) if netlist_data else {}
    inst_map = (
        {inst["name"]: inst for inst in netlist_data.get("instances", [])} if netlist_data else {}
    )
    pin_dict = {p["name"]: p for p in def_data.get("pins", [])}
    comp_by_x_tile, comp_by_y_tile = _build_component_tile_buckets(def_data, units, netlist_data)

    routed_count = 0
    for net in unrouted_nets:
        driver, sink = _select_driver_and_sink(net, comp_map, inst_map, cell_map, pin_dict)
        src_info = _get_signal_pin_loc(
            driver[0], driver[1], comp_map, inst_map, cell_map, pin_dict, units
        )
        dst_info = _get_signal_pin_loc(
            sink[0], sink[1], comp_map, inst_map, cell_map, pin_dict, units
        )
        if not src_info or not dst_info:
            continue
        if _try_route_single_signal_net(
            net,
            src_info,
            dst_info,
            driver[0],
            sink[0],
            spatial,
            comp_by_x_tile,
            comp_by_y_tile,
            units,
        ):
            routed_count += 1

    return routed_count
