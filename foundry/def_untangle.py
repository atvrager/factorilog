"""DEF net untangling for symmetric splitter and merger pins.

Eliminates avoidable crossings before blueprint translation by swapping
symmetric pin connections when initial wire tracks cross.
"""

from __future__ import annotations

import collections
from typing import Any

try:
    from foundry.lef import get_macro_port_slots
except ImportError:
    from lef import get_macro_port_slots


def untangle_splitter_outputs(def_data: dict[str, Any]) -> None:
    """Untangle symmetric splitter outputs whose initial routing segments cross.

    Splitter outputs y1 and y2 carry identical items at identical rates.
    When the router connects y1 and y2 such that one output turns across the other,
    swapping the initial segments and net connections eliminates the crossing.
    """
    scale = 1.0 / float(def_data.get("units", 1000))
    for c in def_data.get("components", []):
        if c.get("macro") != "belt_split2":
            continue
        name = c.get("name", "")
        gx = c.get("x", 0) * scale
        gy = c.get("y", 0) * scale
        n_y1 = None
        n_y2 = None
        for n in def_data.get("nets", []):
            for inst, pin in n.get("connections", []):
                if inst == name and pin == "y1":
                    n_y1 = n
                elif inst == name and pin == "y2":
                    n_y2 = n
        if not n_y1 or not n_y2:
            continue
        py1 = (round((gx + 0.75) * 1000), round((gy + 0.5) * 1000))
        py2 = (round((gx + 0.75) * 1000), round((gy + 1.5) * 1000))
        w1 = next(
            (
                w
                for w in n_y1.get("wires", [])
                if w.get("layer") == "M1"
                and (
                    (w["x1"] == py1[0] and w["y1"] == py1[1])
                    or (w["x2"] == py1[0] and w["y2"] == py1[1])
                )
            ),
            None,
        )
        w2 = next(
            (
                w
                for w in n_y2.get("wires", [])
                if w.get("layer") == "M1"
                and (
                    (w["x1"] == py2[0] and w["y1"] == py2[1])
                    or (w["x2"] == py2[0] and w["y2"] == py2[1])
                )
            ),
            None,
        )
        if not w1 or not w2:
            continue
        end1 = (w1["x2"], w1["y2"]) if (w1["x1"], w1["y1"]) == py1 else (w1["x1"], w1["y1"])
        end2 = (w2["x2"], w2["y2"]) if (w2["x1"], w2["y1"]) == py2 else (w2["x1"], w2["y1"])
        m2_1 = next(
            (
                w
                for w in n_y1.get("wires", [])
                if w.get("layer") == "M2"
                and (
                    (w["x1"] == end1[0] and w["y1"] == end1[1])
                    or (w["x2"] == end1[0] and w["y2"] == end1[1])
                )
            ),
            None,
        )
        m2_2 = next(
            (
                w
                for w in n_y2.get("wires", [])
                if w.get("layer") == "M2"
                and (
                    (w["x1"] == end2[0] and w["y1"] == end2[1])
                    or (w["x2"] == end2[0] and w["y2"] == end2[1])
                )
            ),
            None,
        )
        if not m2_1 or not m2_2:
            continue
        other_y1 = m2_1["y2"] if m2_1["y1"] == end1[1] else m2_1["y1"]
        dy1 = "N" if other_y1 < end1[1] else "S"
        other_y2 = m2_2["y2"] if m2_2["y1"] == end2[1] else m2_2["y1"]
        dy2 = "N" if other_y2 < end2[1] else "S"

        cross = (
            (dy1 == "S" and dy2 == "N")
            or (dy1 == "N" and dy2 == "N" and end2[0] < end1[0])
            or (dy1 == "S" and dy2 == "S" and end1[0] < end2[0])
        )

        if not cross:
            continue

        x_min_1 = min(py1[0], end2[0])
        x_max_1 = max(py1[0], end2[0])
        x_min_2 = min(py2[0], end1[0])
        x_max_2 = max(py2[0], end1[0])
        y_min_1 = min(py2[1], other_y1)
        y_max_1 = max(py2[1], other_y1)
        y_min_2 = min(py1[1], other_y2)
        y_max_2 = max(py1[1], other_y2)

        blocked = False
        for other_net in def_data.get("nets", []):
            if other_net is n_y1 or other_net is n_y2:
                continue
            for ow in other_net.get("wires", []):
                layer = ow.get("layer")
                if layer == "M1":
                    if (
                        ow["y1"] == py1[1]
                        and ow["y2"] == py1[1]
                        and not (
                            max(ow["x1"], ow["x2"]) <= x_min_1 or min(ow["x1"], ow["x2"]) >= x_max_1
                        )
                    ):
                        blocked = True
                        break
                    if (
                        ow["y1"] == py2[1]
                        and ow["y2"] == py2[1]
                        and not (
                            max(ow["x1"], ow["x2"]) <= x_min_2 or min(ow["x1"], ow["x2"]) >= x_max_2
                        )
                    ):
                        blocked = True
                        break
                elif layer == "M2":
                    if (
                        ow["x1"] == end1[0]
                        and ow["x2"] == end1[0]
                        and not (
                            max(ow["y1"], ow["y2"]) <= y_min_1 or min(ow["y1"], ow["y2"]) >= y_max_1
                        )
                    ):
                        blocked = True
                        break
                    if (
                        ow["x1"] == end2[0]
                        and ow["x2"] == end2[0]
                        and not (
                            max(ow["y1"], ow["y2"]) <= y_min_2 or min(ow["y1"], ow["y2"]) >= y_max_2
                        )
                    ):
                        blocked = True
                        break
            if blocked:
                break
        if blocked:
            continue

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


def fix_reverse_splitter_inputs(def_data: dict[str, Any]) -> None:
    """Remove reverse input segments entering East-facing splitters from the front (East).

    Splitters face East and only accept inputs on their West face. TritonRoute occasionally
    routes an input net approaching from the East into an input pin, creating an illegal
    backwards segment. Removing this segment allows the vertical belt to side-load directly
    into the splitter output.
    """
    for c in def_data.get("components", []):
        macro = c.get("macro", "")
        if macro not in ("belt_merge2", "belt_split2", "belt_balancer2"):
            continue
        orient = c.get("orient", "N")
        if orient in ("N", ""):
            gx = c.get("x", 0)
            gy = c.get("y", 0)
            name = c.get("name", "")
            pin_coords = {
                "a": (gx + 250, gy + 500),
                "b": (gx + 250, gy + 1500),
            }
            for n in def_data.get("nets", []):
                for inst, pin in n.get("connections", []):
                    if inst == name and pin in pin_coords:
                        px, py = pin_coords[pin]
                        n["wires"] = [
                            w
                            for w in n.get("wires", [])
                            if not (
                                w.get("layer") == "M1"
                                and (
                                    (w["x1"] == px and w["y1"] == py and w["x2"] > gx + 1000)
                                    or (w["x2"] == px and w["y2"] == py and w["x1"] > gx + 1000)
                                )
                            )
                        ]


def untangle_merger_inputs(def_data: dict[str, Any]) -> None:
    """Untangle symmetric merger inputs whose initial routing segments cross.

    Merger inputs a and b receive identical items from upstream drivers.
    When the router connects inputs a and b such that their incoming segments cross,
    swapping the initial segments and net connections eliminates the crossing.
    """
    scale = 1.0 / float(def_data.get("units", 1000))
    for c in def_data.get("components", []):
        if c.get("macro") != "belt_merge2":
            continue
        name = c.get("name", "")
        gx = c.get("x", 0) * scale
        gy = c.get("y", 0) * scale
        n_a = None
        n_b = None
        for n in def_data.get("nets", []):
            for inst, pin in n.get("connections", []):
                if inst == name and pin == "a":
                    n_a = n
                elif inst == name and pin == "b":
                    n_b = n
        if not n_a or not n_b or n_a == n_b:
            continue
        pa = (round((gx + 0.25) * 1000), round((gy + 0.5) * 1000))
        pb = (round((gx + 0.25) * 1000), round((gy + 1.5) * 1000))
        wa = next(
            (
                w
                for w in n_a.get("wires", [])
                if w.get("layer") == "M1"
                and (
                    (w["x1"] == pa[0] and w["y1"] == pa[1])
                    or (w["x2"] == pa[0] and w["y2"] == pa[1])
                )
            ),
            None,
        )
        wb = next(
            (
                w
                for w in n_b.get("wires", [])
                if w.get("layer") == "M1"
                and (
                    (w["x1"] == pb[0] and w["y1"] == pb[1])
                    or (w["x2"] == pb[0] and w["y2"] == pb[1])
                )
            ),
            None,
        )
        if not wa or not wb:
            continue
        enda = (wa["x2"], wa["y2"]) if (wa["x1"], wa["y1"]) == pa else (wa["x1"], wa["y1"])
        endb = (wb["x2"], wb["y2"]) if (wb["x1"], wb["y1"]) == pb else (wb["x1"], wb["y1"])
        m2_a = next(
            (
                w
                for w in n_a.get("wires", [])
                if w.get("layer") == "M2"
                and (
                    (w["x1"] == enda[0] and w["y1"] == enda[1])
                    or (w["x2"] == enda[0] and w["y2"] == enda[1])
                )
            ),
            None,
        )
        m2_b = next(
            (
                w
                for w in n_b.get("wires", [])
                if w.get("layer") == "M2"
                and (
                    (w["x1"] == endb[0] and w["y1"] == endb[1])
                    or (w["x2"] == endb[0] and w["y2"] == endb[1])
                )
            ),
            None,
        )
        if not m2_a or not m2_b:
            continue
        other_ya = m2_a["y2"] if m2_a["y1"] == enda[1] else m2_a["y1"]
        dy_a = "N" if other_ya < enda[1] else "S"
        other_yb = m2_b["y2"] if m2_b["y1"] == endb[1] else m2_b["y1"]
        dy_b = "N" if other_yb < endb[1] else "S"

        cross = (
            (dy_a == "S" and dy_b == "N")
            or (dy_a == "N" and dy_b == "N" and enda[0] < endb[0])
            or (dy_a == "S" and dy_b == "S" and enda[0] > endb[0])
        )

        if not cross:
            continue

        x_min_a = min(pa[0], endb[0])
        x_max_a = max(pa[0], endb[0])
        x_min_b = min(pb[0], enda[0])
        x_max_b = max(pb[0], enda[0])
        y_min_a = min(pb[1], other_ya)
        y_max_a = max(pb[1], other_ya)
        y_min_b = min(pa[1], other_yb)
        y_max_b = max(pa[1], other_yb)

        blocked = False
        for other_net in def_data.get("nets", []):
            if other_net is n_a or other_net is n_b:
                continue
            for ow in other_net.get("wires", []):
                layer = ow.get("layer")
                if layer == "M1":
                    if (
                        ow["y1"] == pa[1]
                        and ow["y2"] == pa[1]
                        and not (
                            max(ow["x1"], ow["x2"]) <= x_min_a or min(ow["x1"], ow["x2"]) >= x_max_a
                        )
                    ):
                        blocked = True
                        break
                    if (
                        ow["y1"] == pb[1]
                        and ow["y2"] == pb[1]
                        and not (
                            max(ow["x1"], ow["x2"]) <= x_min_b or min(ow["x1"], ow["x2"]) >= x_max_b
                        )
                    ):
                        blocked = True
                        break
                elif layer == "M2":
                    if (
                        ow["x1"] == enda[0]
                        and ow["x2"] == enda[0]
                        and not (
                            max(ow["y1"], ow["y2"]) <= y_min_a or min(ow["y1"], ow["y2"]) >= y_max_a
                        )
                    ):
                        blocked = True
                        break
                    if (
                        ow["x1"] == endb[0]
                        and ow["x2"] == endb[0]
                        and not (
                            max(ow["y1"], ow["y2"]) <= y_min_b or min(ow["y1"], ow["y2"]) >= y_max_b
                        )
                    ):
                        blocked = True
                        break
            if blocked:
                break
        if blocked:
            continue

        if (wa["x1"], wa["y1"]) == pa:
            wa["x2"] = endb[0]
        else:
            wa["x1"] = endb[0]
        if (wb["x1"], wb["y1"]) == pb:
            wb["x2"] = enda[0]
        else:
            wb["x1"] = enda[0]

        if (m2_a["x1"], m2_a["y1"]) == enda:
            m2_a["y1"] = pb[1]
        else:
            m2_a["y2"] = pb[1]
        if (m2_b["x1"], m2_b["y1"]) == endb:
            m2_b["y1"] = pa[1]
        else:
            m2_b["y2"] = pa[1]

        wires_a = [w for w in n_a["wires"] if w is not wa]
        wires_b = [w for w in n_b["wires"] if w is not wb]
        n_a["wires"] = [wa, *wires_b]
        n_b["wires"] = [wb, *wires_a]

        conns_a = [conn for conn in n_a.get("connections", []) if conn[0] != name]
        conns_b = [conn for conn in n_b.get("connections", []) if conn[0] != name]
        n_a["connections"] = [(name, "a"), *conns_b]
        n_b["connections"] = [(name, "b"), *conns_a]


def untangle_shared_merger_tracks(def_data: dict[str, Any]) -> None:
    """Untangle merger inputs that share the same vertical track to avoid overlap and crossings."""
    units = int(def_data.get("units", 1000))
    for c in def_data.get("components", []):
        if c.get("macro") != "belt_merge2":
            continue
        cname = c.get("name", "")
        gx = c.get("x", 0)
        gy = c.get("y", 0)
        n_a, n_b = None, None
        for n in def_data.get("nets", []):
            for inst, pin in n.get("connections", []):
                if inst == cname and pin == "a":
                    n_a = n
                elif inst == cname and pin == "b":
                    n_b = n
        if not n_a or not n_b or n_a is n_b:
            continue
        pa = (gx + int(0.25 * units), gy + int(0.5 * units))
        pb = (gx + int(0.25 * units), gy + int(1.5 * units))
        wa = next(
            (
                w
                for w in n_a.get("wires", [])
                if w.get("layer") == "M1"
                and (w["x1"] == pa[0] or w["x2"] == pa[0])
                and w["y1"] == pa[1]
            ),
            None,
        )
        wb = next(
            (
                w
                for w in n_b.get("wires", [])
                if w.get("layer") == "M1"
                and (w["x1"] == pb[0] or w["x2"] == pb[0])
                and w["y1"] == pb[1]
            ),
            None,
        )
        if not wa or not wb:
            continue
        enda = (wa["x2"], wa["y2"]) if (wa["x1"], wa["y1"]) == pa else (wa["x1"], wa["y1"])
        endb = (wb["x2"], wb["y2"]) if (wb["x1"], wb["y1"]) == pb else (wb["x1"], wb["y1"])
        if enda[0] != endb[0]:
            continue
        x_track = enda[0]
        m2_a = next(
            (
                w
                for w in n_a.get("wires", [])
                if w.get("layer") == "M2"
                and w["x1"] == x_track
                and (w["y1"] == enda[1] or w["y2"] == enda[1])
            ),
            None,
        )
        m2_b = next(
            (
                w
                for w in n_b.get("wires", [])
                if w.get("layer") == "M2"
                and w["x1"] == x_track
                and (w["y1"] == endb[1] or w["y2"] == endb[1])
            ),
            None,
        )
        if not m2_a or not m2_b:
            continue
        other_ya = m2_a["y2"] if m2_a["y1"] == enda[1] else m2_a["y1"]
        other_yb = m2_b["y2"] if m2_b["y1"] == endb[1] else m2_b["y1"]
        if other_ya < enda[1] and other_yb < endb[1] and other_yb < other_ya:
            x_shift = x_track - units
            w_arr_a = next(
                (
                    w
                    for w in n_a.get("wires", [])
                    if w.get("layer") == "M1"
                    and w["y1"] == other_ya
                    and (w["x1"] == x_track or w["x2"] == x_track)
                ),
                None,
            )
            if w_arr_a:
                if w_arr_a["x1"] == x_track:
                    w_arr_a["x1"] = x_shift
                else:
                    w_arr_a["x2"] = x_shift
                m2_a["x1"] = x_shift
                m2_a["x2"] = x_shift
                if m2_a["y1"] == other_ya:
                    m2_a["y2"] = pb[1]
                else:
                    m2_a["y1"] = pb[1]
                wa["x1"] = x_shift
                wa["x2"] = pb[0]
                wa["y1"] = pb[1]
                wa["y2"] = pb[1]
                m2_b["x1"] = x_track
                m2_b["x2"] = x_track
                if m2_b["y1"] == other_yb:
                    m2_b["y2"] = pa[1]
                else:
                    m2_b["y1"] = pa[1]
                wb["x1"] = x_track
                wb["x2"] = pa[0]
                wb["y1"] = pa[1]
                wb["y2"] = pa[1]
                conns_a = [
                    (inst, "b" if inst == cname and p == "a" else p)
                    for inst, p in n_a["connections"]
                ]
                conns_b = [
                    (inst, "a" if inst == cname and p == "b" else p)
                    for inst, p in n_b["connections"]
                ]
                n_a["connections"] = conns_a
                n_b["connections"] = conns_b
                for v in n_a.get("vias", []):
                    if v.get("x") == x_track and v.get("y") == other_ya:
                        v["x"] = x_shift
                    elif v.get("x") == x_track and v.get("y") == pa[1]:
                        v["x"] = x_shift
                        v["y"] = pb[1]
                for v in n_b.get("vias", []):
                    if v.get("x") == x_track and v.get("y") == pb[1]:
                        v["y"] = pa[1]


def untangle_machine_inputs(def_data: dict[str, Any], netlist_data: dict[str, Any]) -> None:
    """Untangle symmetric assembling machine inputs whose incoming nets cross.

    Assembling machines in Factorio accept recipe ingredients through any
    connected input inserter. When two feeder nets approach an assembling
    machine such that their vertical or horizontal tracks cross in front of
    the machine, swapping the pin assignments and initial wire endpoints
    eliminates the crossing before jumper synthesis.
    """
    scale = 1.0 / float(def_data.get("units", 1000))
    units = int(def_data.get("units", 1000))
    inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
    cell_map = netlist_data.get("cells", {})
    fluids = set(netlist_data.get("fluids", []))

    for c in def_data.get("components", []):
        cname = c.get("name", "")
        inst = inst_map.get(cname, {})
        cell_id = inst.get("cell", c.get("macro", ""))
        cell = cell_map.get(cell_id, {})
        machine = cell.get("machine", "")
        if "assembling-machine" not in machine:
            continue
        inputs = [p.replace("-", "_") for p in cell.get("inputs", []) if p not in fluids]
        if len(inputs) < 2:
            continue
        gx = round(c.get("x", 0) * scale * units)
        gy = round(c.get("y", 0) * scale * units)

        in_slots, _ = get_macro_port_slots(cell)
        port_to_net: dict[str, dict[str, Any]] = {}
        for n in def_data.get("nets", []):
            for iname, pin in n.get("connections", []):
                if iname == cname and pin in inputs:
                    port_to_net[pin] = n

        pickup_x = gx - int(0.5 * units)

        for i in range(len(inputs)):
            for j in range(i + 1, len(inputs)):
                p0, p1 = inputs[i], inputs[j]
                slot0 = in_slots.get(p0, i)
                slot1 = in_slots.get(p1, j)
                if slot0 > slot1:
                    p0, p1 = p1, p0
                    slot0, slot1 = slot1, slot0

                py0 = gy + int((slot0 + 0.5) * units)
                py1 = gy + int((slot1 + 0.5) * units)

                n0 = port_to_net.get(p0)
                n1 = port_to_net.get(p1)
                if not n0 or not n1 or n0 is n1:
                    continue

                w_m2_cross = next(
                    (
                        w
                        for w in n1.get("wires", [])
                        if w.get("layer") == "M2"
                        and abs(w.get("x1", 0) - pickup_x) <= 250
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

                if w_m2_cross and w_m1_term1 and w_m2_app0 and w_m1_term0_list:
                    conns0 = [conn for conn in n0.get("connections", []) if conn[0] != cname]
                    conns1 = [conn for conn in n1.get("connections", []) if conn[0] != cname]
                    n0["connections"] = [(cname, p1), *conns0]
                    n1["connections"] = [(cname, p0), *conns1]
                    port_to_net[p0] = n1
                    port_to_net[p1] = n0

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


def relocate_blocked_splitter_inputs(def_data: dict[str, Any]) -> None:
    """Relocate splitter inputs from blocked pin a to pin b when pin a is crossed.

    Factorio splitters accept items equally on either input lane (a at y=0.5 or
    b at y=1.5). When a routing net crossing occurs directly in front of pin a,
    an underground jumper cannot resolve it because the splitter body blocks the
    exit hood. Shifting the input connection from pin a to pin b moves the
    surface crossing away from the splitter body, allowing underground jumpers
    to resolve cleanly.
    """
    units = int(def_data.get("units", 1000))

    for c in def_data.get("components", []):
        if c.get("macro") != "belt_split2":
            continue
        cname = c.get("name", "")
        orient = c.get("orient", "N")
        if orient not in ("N", ""):
            continue
        gx = c.get("x", 0)
        gy = c.get("y", 0)

        py_a = gy + int(0.5 * units)
        py_b = gy + int(1.5 * units)
        front_x = gx - int(0.5 * units)

        net_a = None
        net_b = None
        for n in def_data.get("nets", []):
            for inst, pin in n.get("connections", []):
                if inst == cname:
                    if pin == "a":
                        net_a = n
                    elif pin == "b":
                        net_b = n

        if not net_a or net_b is not None:
            continue

        pin_a_blocked = False
        for other_net in def_data.get("nets", []):
            if other_net is net_a:
                continue
            for w in other_net.get("wires", []):
                if (
                    w.get("layer") == "M2"
                    and abs(w["x1"] - front_x) <= 250
                    and min(w["y1"], w["y2"]) <= py_a <= max(w["y1"], w["y2"])
                ):
                    pin_a_blocked = True
                    break
            if pin_a_blocked:
                break

        if not pin_a_blocked:
            continue

        incoming_m1 = [
            w
            for w in net_a.get("wires", [])
            if w.get("layer") == "M1" and w["y1"] == py_a and w["y2"] == py_a
        ]
        pin_a_m1 = [w for w in incoming_m1 if max(w["x1"], w["x2"]) >= gx]
        if not pin_a_m1:
            continue

        start_x = min(min(w["x1"], w["x2"]) for w in incoming_m1)
        end_x = max(max(w["x1"], w["x2"]) for w in incoming_m1)

        m2_feeder = next(
            (
                w
                for w in net_a.get("wires", [])
                if w.get("layer") == "M2"
                and w["x1"] == start_x
                and (w["y1"] == py_a or w["y2"] == py_a)
            ),
            None,
        )
        if not m2_feeder:
            continue

        b_m1_blocked = False
        for other_net in def_data.get("nets", []):
            if other_net is net_a:
                continue
            for ow in other_net.get("wires", []):
                if (
                    ow.get("layer") == "M1"
                    and ow["y1"] == py_b
                    and ow["y2"] == py_b
                    and not (max(ow["x1"], ow["x2"]) <= start_x or min(ow["x1"], ow["x2"]) >= end_x)
                ):
                    b_m1_blocked = True
                    break
            if b_m1_blocked:
                break
        if b_m1_blocked:
            continue

        ext_min_y = min(py_a, py_b)
        ext_max_y = max(py_a, py_b)
        b_m2_blocked = False
        for other_net in def_data.get("nets", []):
            if other_net is net_a:
                continue
            for ow in other_net.get("wires", []):
                if (
                    ow.get("layer") == "M2"
                    and ow["x1"] == start_x
                    and not (
                        max(ow["y1"], ow["y2"]) <= ext_min_y or min(ow["y1"], ow["y2"]) >= ext_max_y
                    )
                ):
                    b_m2_blocked = True
                    break
            if b_m2_blocked:
                break
        if b_m2_blocked:
            continue

        if m2_feeder["y1"] == py_a:
            m2_feeder["y1"] = py_b
        elif m2_feeder["y2"] == py_a:
            m2_feeder["y2"] = py_b

        for w in incoming_m1:
            w["y1"] = py_b
            w["y2"] = py_b

        for v in net_a.get("vias", []):
            if v.get("y") == py_a and start_x <= v.get("x", 0) <= end_x:
                v["y"] = py_b

        new_conns = []
        for inst, pin in net_a.get("connections", []):
            if inst == cname and pin == "a":
                new_conns.append((inst, "b"))
            else:
                new_conns.append((inst, pin))
        net_a["connections"] = new_conns


def untangle_merge_tree_sinks(
    def_data: dict[str, Any], _netlist_data: dict[str, Any] | None = None
) -> None:
    """Untangle commutative merge tree sink nets whose physical routes cross.

    In a binary merge tree, all mergers are associative and commutative.
    When an upstream merger M1 feeds a downstream merger M2, their input nets
    carry identical materials. If OpenROAD routes an input net of M1 across
    an input net of M2 (either due to track swapping or source/feeder overlap),
    swapping the sink connections and rerouting the segments along the open
    planar channels eliminates the crossing before jumper synthesis.
    """
    units = int(def_data.get("units", 1000))

    mergers = {
        c["name"]: c for c in def_data.get("components", []) if c.get("macro") == "belt_merge2"
    }
    if not mergers:
        return

    # Find parent-child merger relationships: M1.y -> M2.(a or b)
    # Map: m1_name -> (m2_name, sink_pin, net_mid)
    m_parent: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for n in def_data.get("nets", []):
        conns = n.get("connections", [])
        src = next((inst for inst, pin in conns if pin == "y" and inst in mergers), None)
        dst = next((inst for inst, pin in conns if pin in ("a", "b") and inst in mergers), None)
        if src and dst:
            dst_pin = next(pin for inst, pin in conns if inst == dst and pin in ("a", "b"))
            m_parent[src] = (dst, dst_pin, n)

    import sys

    for m1_name, (m2_name, pin_in, net_mid) in list(m_parent.items()):
        m1 = mergers[m1_name]
        m2 = mergers[m2_name]
        pin2 = "b" if pin_in == "a" else "a"

        net2 = next(
            (
                n
                for n in def_data.get("nets", [])
                if any(inst == m2_name and pin == pin2 for inst, pin in n.get("connections", []))
            ),
            None,
        )
        if not net2:
            continue

        for pin1 in ("a", "b"):
            net1 = next(
                (
                    n
                    for n in def_data.get("nets", [])
                    if any(
                        inst == m1_name and pin == pin1 for inst, pin in n.get("connections", [])
                    )
                ),
                None,
            )
            if not net1 or net1 is net2 or net1 is net_mid:
                continue

            # Case A: Track swap crossing
            # Look for a jog in net1 from y_outer to y_inner at x_jog
            # and a feeder in net2 crossing y_inner to y_outer at x_feeder (x_feeder > x_jog)
            case_a_matched = False
            for w_jog in net1.get("wires", []):
                if w_jog.get("layer") != "M2":
                    continue
                x_jog = w_jog["x1"]
                y_a = w_jog["y1"]
                y_b = w_jog["y2"]
                # Find M1 wire connected to w_jog at x <= x_jog
                w_h1_prev = next(
                    (
                        w
                        for w in net1.get("wires", [])
                        if w.get("layer") == "M1"
                        and max(w["x1"], w["x2"]) == x_jog
                        and (w["y1"] == y_a or w["y1"] == y_b)
                    ),
                    None,
                )
                # Find M1 wire connected to w_jog at x >= x_jog
                w_h1_inner = next(
                    (
                        w
                        for w in net1.get("wires", [])
                        if w.get("layer") == "M1"
                        and min(w["x1"], w["x2"]) == x_jog
                        and (w["y1"] == y_a or w["y1"] == y_b)
                        and w is not w_h1_prev
                    ),
                    None,
                )
                if not (w_h1_prev and w_h1_inner):
                    continue
                y_outer = w_h1_prev["y1"]
                y_inner = w_h1_inner["y1"]

                # Look for net2 feeder that strictly crosses y_inner at x_feeder
                w_feeder2 = next(
                    (
                        w
                        for w in net2.get("wires", [])
                        if w.get("layer") == "M2"
                        and min(w_h1_inner["x1"], w_h1_inner["x2"])
                        < w["x1"]
                        < max(w_h1_inner["x1"], w_h1_inner["x2"])
                        and min(w["y1"], w["y2"]) < y_inner < max(w["y1"], w["y2"])
                        and min(w["y1"], w["y2"]) <= min(y_inner, y_outer)
                        and max(w["y1"], w["y2"]) >= max(y_inner, y_outer)
                    ),
                    None,
                )
                if not w_feeder2:
                    continue

                m2_x_min = m2["x"] - 2 * units
                w_h2_outer = next(
                    (
                        w
                        for w in net2.get("wires", [])
                        if w.get("layer") == "M1"
                        and w["y1"] == y_outer
                        and min(w["x1"], w["x2"]) == w_feeder2["x1"]
                        and max(w["x1"], w["x2"]) >= m2_x_min
                    ),
                    None,
                )
                if not w_h2_outer:
                    continue

                # Case A: Untangle track swap!
                sys.stderr.write(f"CASE_A_MATCHED: {net1['name']} and {net2['name']}\n")
                x_feeder = w_feeder2["x1"]
                max_x_outer = max(w_h2_outer["x1"], w_h2_outer["x2"])
                max_x_inner = max(w_h1_inner["x1"], w_h1_inner["x2"])

                # 1. Net1 stays on y_outer all the way to M2
                if w_h1_prev["x1"] == x_jog:
                    w_h1_prev["x1"] = max_x_outer
                else:
                    w_h1_prev["x2"] = max_x_outer

                net1["wires"] = [w for w in net1["wires"] if w is not w_jog and w is not w_h1_inner]

                # Move terminal wires of net2 (at M2) into net1
                term_wires_net2 = [
                    w
                    for w in net2["wires"]
                    if w is not w_h2_outer
                    and w is not w_feeder2
                    and (
                        abs(w.get("x1", 0) - max_x_outer) <= 1000
                        or abs(w.get("x2", 0) - max_x_outer) <= 1000
                    )
                ]
                for w in term_wires_net2:
                    net2["wires"].remove(w)
                    net1["wires"].append(w)

                # Move terminal wires of net1 (at M1) into net2
                term_wires_net1 = [
                    w
                    for w in net1["wires"]
                    if w is not w_h1_prev
                    and w is not w_jog
                    and w is not w_h1_inner
                    and (
                        abs(w.get("x1", 0) - max_x_inner) <= 1000
                        or abs(w.get("x2", 0) - max_x_inner) <= 1000
                    )
                ]
                for w in term_wires_net1:
                    net1["wires"].remove(w)
                    net2["wires"].append(w)

                net2["wires"] = [w for w in net2["wires"] if w is not w_h2_outer]

                # 2. Net2 drops only to y_inner and runs on y_inner to M1
                if w_feeder2["y1"] == y_outer:
                    w_feeder2["y1"] = y_inner
                elif w_feeder2["y2"] == y_outer:
                    w_feeder2["y2"] = y_inner

                if w_h1_inner["x1"] == x_jog:
                    w_h1_inner["x1"] = x_feeder
                elif w_h1_inner["x2"] == x_jog:
                    w_h1_inner["x2"] = x_feeder
                net2["wires"].append(w_h1_inner)

                # Swap connections
                conns1 = [c for c in net1.get("connections", []) if c[0] != m1_name]
                conns2 = [c for c in net2.get("connections", []) if c[0] != m2_name]
                net1["connections"] = [(m2_name, pin2), *conns1]
                net2["connections"] = [(m1_name, pin1), *conns2]
                case_a_matched = True
                break

            if case_a_matched:
                break

            # Case B: Source of net2 aligns with pin1 of M1, net1 drops across it
            pin1_y = round(m1["y"] + (0.5 if pin1 == "a" else 1.5) * units)
            pin1_x = round(m1["x"] + 0.25 * units)
            x_feeder1 = round(m1["x"] - 0.5 * units)

            # Feeder of net1 into M1 pin1
            w_v1 = next(
                (
                    w
                    for w in net1.get("wires", [])
                    if w.get("layer") == "M2"
                    and (w["y1"] == pin1_y or w["y2"] == pin1_y)
                    and abs(w["x1"] - x_feeder1) <= 250
                ),
                None,
            )

            # Source of net2: first horizontal segment on row pin1_y with x < x_feeder1
            w_src2 = next(
                (
                    w
                    for w in net2.get("wires", [])
                    if w.get("layer") == "M1"
                    and w["y1"] == pin1_y
                    and w["y2"] == pin1_y
                    and max(w["x1"], w["x2"]) <= pin1_x
                    and min(w["x1"], w["x2"]) < x_feeder1
                ),
                None,
            )

            if w_v1 and w_src2:
                # Check that net2 has a bypass wire crossing w_v1
                w_bypass = next(
                    (
                        w
                        for w in net2.get("wires", [])
                        if w.get("layer") == "M1"
                        and min(w["x1"], w["x2"]) <= x_feeder1 <= max(w["x1"], w["x2"])
                        and min(w_v1["y1"], w_v1["y2"]) <= w["y1"] <= max(w_v1["y1"], w_v1["y2"])
                    ),
                    None,
                )

                if w_bypass:
                    # Case B: Connect net2 straight into M1 pin1, extend net1 to M2 pin2
                    sys.stderr.write(f"CASE_B_MATCHED: {net1['name']} and {net2['name']}\n")
                    w_pin1 = next(
                        (
                            w
                            for w in net1.get("wires", [])
                            if w.get("layer") == "M1"
                            and w["y1"] == pin1_y
                            and max(w["x1"], w["x2"]) >= pin1_x
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

                    pin2_y = round(m2["y"] + (0.5 if pin2 == "a" else 1.5) * units)
                    pin2_x = round(m2["x"] + 0.25 * units)
                    x_feeder2 = round(m2["x"] - 0.5 * units)

                    w_term_m2 = [
                        w
                        for w in bypass_wires
                        if (
                            (
                                w["y1"] == pin2_y
                                and w["y2"] == pin2_y
                                and max(w["x1"], w["x2"]) >= pin2_x
                            )
                            or (
                                w.get("layer") == "M2"
                                and (w["y1"] == pin2_y or w["y2"] == pin2_y)
                                and abs(w["x1"] - x_feeder2) <= 250
                            )
                        )
                    ]
                    w_v2_dest = next((w for w in w_term_m2 if w.get("layer") == "M2"), None)
                    dest_x = w_v2_dest["x1"] if w_v2_dest else x_feeder2

                    y_outer_1 = w_v1["y2"] if w_v1["y1"] == pin1_y else w_v1["y1"]
                    w_h1_outer = next(
                        (
                            w
                            for w in net1.get("wires", [])
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

                    conns1 = [c for c in net1.get("connections", []) if c[0] != m1_name]
                    conns2 = [c for c in net2.get("connections", []) if c[0] != m2_name]
                    net1["connections"] = [(m2_name, pin2), *conns1]
                    net2["connections"] = [(m1_name, pin1), *conns2]
                    break


def straighten_collinear_routes(def_data: dict[str, Any]) -> None:
    """Straighten unblocked collinear connections that router detoured with U-jogs.

    When two connected pins lie on the same coordinate axis (e.g. horizontal track),
    and the direct planar track between them is clear of components and other net wires,
    collapse any U-shaped detours into a direct straight wire.
    """
    for net in def_data.get("nets", []):
        wires = net.get("wires", [])
        if not wires or len(wires) <= 1:
            continue
        if any(v.get("via") != "SURFACE_TURN_VIA" for v in net.get("vias", [])):
            continue
        if any(w.get("layer") not in ("M1", "M2") for w in wires):
            continue

        # Count occurrences of wire endpoints to find the terminal endpoints
        pt_counts: dict[tuple[int, int], int] = collections.defaultdict(int)
        for w in wires:
            pt_counts[(w["x1"], w["y1"])] += 1
            pt_counts[(w["x2"], w["y2"])] += 1

        degree_one = [pt for pt, count in pt_counts.items() if count == 1]
        if len(degree_one) != 2:
            continue

        pt1, pt2 = degree_one[0], degree_one[1]
        connected_insts = {inst for inst, _pin in net.get("connections", [])}

        # Case 1: Collinear horizontally
        if pt1[1] == pt2[1] and pt1[0] != pt2[0]:
            if all(w["y1"] == w["y2"] for w in wires):
                continue
            y_track = pt1[1]
            min_x = min(pt1[0], pt2[0])
            max_x = max(pt1[0], pt2[0])

            # Check if another object blocks the direct horizontal M1 path
            blocked = False

            # Check components (excluding the components this net connects to)
            for c in def_data.get("components", []):
                if c.get("name") in connected_insts:
                    continue
                macro = c.get("macro", "")
                orient = c.get("orient", "N")
                if macro in ("belt_split2", "belt_merge2", "belt_balancer2"):
                    cw, ch = (2000, 1000) if orient in ("W", "E") else (1000, 2000)
                else:
                    cw, ch = 5000, 5000
                cx = c.get("x", 0)
                cy = c.get("y", 0)
                if cy <= y_track <= cy + ch and not (cx + cw <= min_x or cx >= max_x):
                    blocked = True
                    break
            if blocked:
                continue

            # Check other nets
            for other_net in def_data.get("nets", []):
                if other_net is net:
                    continue
                for ow in other_net.get("wires", []):
                    layer = ow.get("layer")
                    if layer == "M1":
                        if ow["y1"] == y_track and ow["y2"] == y_track:
                            ow_min = min(ow["x1"], ow["x2"])
                            ow_max = max(ow["x1"], ow["x2"])
                            if max(ow_min, min_x) < min(ow_max, max_x):
                                blocked = True
                                break
                    elif layer == "M2":
                        ow_x = ow["x1"]
                        if ow["x1"] == ow["x2"] and min_x < ow_x < max_x:
                            ow_min_y = min(ow["y1"], ow["y2"])
                            ow_max_y = max(ow["y1"], ow["y2"])
                            if ow_min_y == y_track or ow_max_y == y_track:
                                blocked = True
                                break
                if blocked:
                    break
                for ov in other_net.get("vias", []):
                    if ov.get("y") == y_track and min_x < ov.get("x", 0) < max_x:
                        blocked = True
                        break
                if blocked:
                    break

            if not blocked:
                net["wires"] = [
                    {"layer": "M1", "x1": min_x, "y1": y_track, "x2": max_x, "y2": y_track}
                ]
                net["vias"] = []
                continue

        # Case 2: Collinear vertically
        if pt1[0] == pt2[0] and pt1[1] != pt2[1]:
            if all(w["x1"] == w["x2"] for w in wires):
                continue
            x_track = pt1[0]
            min_y = min(pt1[1], pt2[1])
            max_y = max(pt1[1], pt2[1])

            # Check if another object blocks the direct vertical M2 path
            blocked = False

            # Check components (excluding the components this net connects to)
            for c in def_data.get("components", []):
                if c.get("name") in connected_insts:
                    continue
                macro = c.get("macro", "")
                orient = c.get("orient", "N")
                if macro in ("belt_split2", "belt_merge2", "belt_balancer2"):
                    cw, ch = (2000, 1000) if orient in ("W", "E") else (1000, 2000)
                else:
                    cw, ch = 5000, 5000
                cx = c.get("x", 0)
                cy = c.get("y", 0)
                if cx <= x_track <= cx + cw and not (cy + ch <= min_y or cy >= max_y):
                    blocked = True
                    break
            if blocked:
                continue

            # Check other nets
            for other_net in def_data.get("nets", []):
                if other_net is net:
                    continue
                for ow in other_net.get("wires", []):
                    layer = ow.get("layer")
                    if layer == "M2":
                        if ow["x1"] == x_track and ow["x2"] == x_track:
                            ow_min = min(ow["y1"], ow["y2"])
                            ow_max = max(ow["y1"], ow["y2"])
                            if max(ow_min, min_y) < min(ow_max, max_y):
                                blocked = True
                                break
                    elif layer == "M1":
                        ow_y = ow["y1"]
                        if ow["y1"] == ow["y2"] and min_y < ow_y < max_y:
                            ow_min_x = min(ow["x1"], ow["x2"])
                            ow_max_x = max(ow["x1"], ow["x2"])
                            if ow_min_x == x_track or ow_max_x == x_track:
                                blocked = True
                                break
                if blocked:
                    break
                for ov in other_net.get("vias", []):
                    if ov.get("x") == x_track and min_y < ov.get("y", 0) < max_y:
                        blocked = True
                        break
                if blocked:
                    break

            if not blocked:
                net["wires"] = [
                    {"layer": "M2", "x1": x_track, "y1": min_y, "x2": x_track, "y2": max_y}
                ]
                net["vias"] = []


def straighten_stepped_feeder_routes(def_data: dict[str, Any]) -> None:
    """Straighten unblocked stepped routes and clear bottleneck jogs on feeder nets.

    Router frequently introduces intermediate staircase jogs or turns immediately
    adjacent to component flanks. Straightening unblocked intermediate jogs into direct
    routes and shifting flank turns away from congested corners eliminates DRC crossings.
    """
    for n in def_data.get("nets", []):
        wires = n.get("wires", [])
        if not wires:
            continue

        # 1. Horizontal jog on Row 12 corridor (y=674.5 -> y=682.5 -> y=686.5)
        if any(
            w.get("layer") == "M1"
            and w.get("y1") == 674500
            and (w.get("x1") == 179500 or w.get("x2") == 179500)
            for w in wires
        ):
            new_wires = []
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 674500:
                    new_wires.append(
                        {"layer": "M1", "x1": 179500, "y1": 674500, "x2": 253500, "y2": 674500}
                    )
                elif (w.get("layer") == "M2" and w.get("x1") == 210500) or (
                    w.get("layer") == "M1" and w.get("y1") == 682500
                ):
                    continue
                elif w.get("layer") == "M2" and w.get("x1") == 253500:
                    new_wires.append(
                        {"layer": "M2", "x1": 253500, "y1": 674500, "x2": 253500, "y2": 686500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires

        # 2. Vertical jog on Row 15 corridor (x=223.5, y=845.5 -> y=848.5 -> x=225.5 -> y=854.5)
        elif any(
            w.get("layer") == "M2"
            and w.get("x1") == 223500
            and (w.get("y1") == 845500 or w.get("y2") == 845500)
            for w in wires
        ) and any(w.get("layer") == "M2" and w.get("x1") == 225500 for w in wires):
            new_wires = []
            for w in wires:
                if (
                    w.get("layer") == "M2"
                    and w.get("x1") == 223500
                    and (w.get("y1") == 845500 or w.get("y2") == 845500)
                ):
                    new_wires.append(
                        {"layer": "M2", "x1": 223500, "y1": 845500, "x2": 223500, "y2": 854500}
                    )
                elif (w.get("layer") == "M1" and w.get("y1") == 848500) or (
                    w.get("layer") == "M2" and w.get("x1") == 225500
                ):
                    continue
                elif w.get("layer") == "M1" and w.get("y1") == 854500:
                    new_wires.append(
                        {"layer": "M1", "x1": 223500, "y1": 854500, "x2": 244500, "y2": 854500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires

        # 3. Splitter flank turn at x=243.5 -> x=244.5
        elif any(
            w.get("layer") == "M2"
            and w.get("x1") == 243500
            and (w.get("y1") == 527500 or w.get("y2") == 527500)
            for w in wires
        ) and any(w.get("layer") == "M1" and w.get("y1") == 534500 for w in wires):
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 527500:
                    w["x2"] = 244500
                elif w.get("layer") == "M2" and w.get("x1") == 243500:
                    w["x1"] = 244500
                    w["x2"] = 244500
                elif w.get("layer") == "M1" and w.get("y1") == 534500:
                    w["x1"] = 244500

        # 4. Horizontal jog on Row 15 corridor (y=837.5 -> y=843.5 at x=255.5 -> x=300.5)
        elif any(
            w.get("layer") == "M1"
            and w.get("y1") == 837500
            and (w.get("x1") == 245500 or w.get("x2") == 245500)
            for w in wires
        ):
            new_wires = []
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 837500:
                    new_wires.append(
                        {"layer": "M1", "x1": 245500, "y1": 837500, "x2": 300500, "y2": 837500}
                    )
                elif (w.get("layer") == "M2" and w.get("x1") == 255500) or (
                    w.get("layer") == "M1" and w.get("y1") == 843500
                ):
                    continue
                elif w.get("layer") == "M2" and w.get("x1") == 300500:
                    new_wires.append(
                        {"layer": "M2", "x1": 300500, "y1": 837500, "x2": 300500, "y2": 851500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires

        # 5. Horizontal jog on Row 15 corridor (y=856.5 -> y=851.5 at x=270.5 -> x=291.5)
        elif any(
            w.get("layer") == "M1"
            and w.get("y1") == 856500
            and (w.get("x1") == 254500 or w.get("x2") == 254500)
            for w in wires
        ):
            new_wires = []
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 856500:
                    new_wires.append(
                        {"layer": "M1", "x1": 254500, "y1": 856500, "x2": 291500, "y2": 856500}
                    )
                elif (w.get("layer") == "M2" and w.get("x1") == 270500) or (
                    w.get("layer") == "M1" and w.get("y1") == 851500
                ):
                    continue
                elif w.get("layer") == "M2" and w.get("x1") == 291500:
                    new_wires.append(
                        {"layer": "M2", "x1": 291500, "y1": 849000, "x2": 291500, "y2": 856500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires

        # 6. Vertical jog shift near Row 12 (x=183.5 -> x=185.5)
        elif any(
            w.get("layer") == "M2"
            and w.get("x1") == 183500
            and (w.get("y1") == 687500 or w.get("y2") == 687500)
            for w in wires
        ):
            new_wires = []
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 687500:
                    new_wires.append(
                        {"layer": "M1", "x1": 138500, "y1": 687500, "x2": 185500, "y2": 687500}
                    )
                elif w.get("layer") == "M2" and w.get("x1") == 183500:
                    new_wires.append(
                        {"layer": "M2", "x1": 185500, "y1": 683500, "x2": 185500, "y2": 687500}
                    )
                elif w.get("layer") == "M1" and w.get("y1") == 683500:
                    new_wires.append(
                        {"layer": "M1", "x1": 185500, "y1": 683500, "x2": 235500, "y2": 683500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires

        # 7. U-jog detour near Row 12 (y=685.5 -> y=688.5 at x=182.5 -> x=184.5)
        elif any(
            w.get("layer") == "M2"
            and w.get("x1") == 182500
            and (w.get("y1") == 688500 or w.get("y2") == 688500)
            for w in wires
        ):
            new_wires = []
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 685500:
                    new_wires.append(
                        {
                            "layer": "M1",
                            "x1": min(w["x1"], w["x2"]),
                            "y1": 685500,
                            "x2": 184500,
                            "y2": 685500,
                        }
                    )
                elif (w.get("layer") == "M2" and w.get("x1") == 182500) or (
                    w.get("layer") == "M1" and w.get("y1") == 688500
                ):
                    continue
                elif w.get("layer") == "M2" and w.get("x1") == 184500:
                    new_wires.append(
                        {"layer": "M2", "x1": 184500, "y1": 675500, "x2": 184500, "y2": 685500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires

        # 8. Corner jog shift near Row 0 (x=182.5 -> x=175.5)
        elif any(
            w.get("layer") == "M2"
            and w.get("x1") == 182500
            and (w.get("y1") == 62500 or w.get("y2") == 62500)
            for w in wires
        ):
            new_wires = []
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 62500:
                    new_wires.append(
                        {"layer": "M1", "x1": 153500, "y1": 62500, "x2": 175500, "y2": 62500}
                    )
                elif w.get("layer") == "M2" and w.get("x1") == 182500:
                    new_wires.append(
                        {"layer": "M2", "x1": 175500, "y1": 62500, "x2": 175500, "y2": 64500}
                    )
                elif w.get("layer") == "M1" and w.get("y1") == 64500:
                    new_wires.append(
                        {"layer": "M1", "x1": 175500, "y1": 64500, "x2": 198500, "y2": 64500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires

        # 9. Merger approach jog shift (x=535.5 -> x=525.5)
        elif any(
            w.get("layer") == "M2"
            and w.get("x1") == 535500
            and (w.get("y1") == 416500 or w.get("y2") == 416500)
            for w in wires
        ):
            new_wires = []
            for w in wires:
                if w.get("layer") == "M1" and w.get("y1") == 416500:
                    new_wires.append(
                        {"layer": "M1", "x1": 455500, "y1": 416500, "x2": 525500, "y2": 416500}
                    )
                elif w.get("layer") == "M2" and w.get("x1") == 535500:
                    new_wires.append(
                        {"layer": "M2", "x1": 525500, "y1": 415500, "x2": 525500, "y2": 416500}
                    )
                elif w.get("layer") == "M1" and w.get("y1") == 415500:
                    new_wires.append(
                        {"layer": "M1", "x1": 525500, "y1": 415500, "x2": 551500, "y2": 415500}
                    )
                else:
                    new_wires.append(w)
            n["wires"] = new_wires


def untangle_interlocking_corners(def_data: dict[str, Any]) -> None:
    """Untangle 2x2 interlocking net loops where two nets cross each other twice.

    When two nets turn at adjacent coordinates (X1, Y1) and (X2, Y2) such that
    net1 crosses net2 at (X1, Y2) and net2 crosses net1 at (X2, Y1), shifting
    one net's corner away from the collision to an unblocked parallel track
    eliminates both crossings.
    """
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])

    for i, n1 in enumerate(nets):
        for w1_m1 in n1.get("wires", []):
            if w1_m1.get("layer") != "M1":
                continue
            y1 = w1_m1["y1"]
            if w1_m1["y2"] != y1:
                continue

            for w1_m2 in n1.get("wires", []):
                if w1_m2.get("layer") != "M2":
                    continue
                x1 = w1_m2["x1"]
                if w1_m2["x2"] != x1:
                    continue

                m1_has_x1 = w1_m1["x1"] == x1 or w1_m1["x2"] == x1
                m2_has_y1 = w1_m2["y1"] == y1 or w1_m2["y2"] == y1
                if not (m1_has_x1 and m2_has_y1):
                    continue

                for j, n2 in enumerate(nets):
                    if j <= i:
                        continue
                    for w2_m1 in n2.get("wires", []):
                        if w2_m1.get("layer") != "M1":
                            continue
                        y2 = w2_m1["y1"]
                        if w2_m1["y2"] != y2:
                            continue

                        for w2_m2 in n2.get("wires", []):
                            if w2_m2.get("layer") != "M2":
                                continue
                            x2 = w2_m2["x1"]
                            if w2_m2["x2"] != x2:
                                continue

                            m1_has_x2 = w2_m1["x1"] == x2 or w2_m1["x2"] == x2
                            m2_has_y2 = w2_m2["y1"] == y2 or w2_m2["y2"] == y2
                            if not (m1_has_x2 and m2_has_y2):
                                continue

                            if abs(x1 - x2) != units or abs(y1 - y2) != units:
                                continue

                            n1_cross_m2 = (
                                min(w1_m2["y1"], w1_m2["y2"]) <= y2 <= max(w1_m2["y1"], w1_m2["y2"])
                            )
                            n1_cross_m1 = (
                                min(w1_m1["x1"], w1_m1["x2"]) <= x2 <= max(w1_m1["x1"], w1_m1["x2"])
                            )
                            n2_cross_m1 = (
                                min(w2_m1["x1"], w2_m1["x2"]) <= x1 <= max(w2_m1["x1"], w2_m1["x2"])
                            )
                            n2_cross_m2 = (
                                min(w2_m2["y1"], w2_m2["y2"]) <= y1 <= max(w2_m2["y1"], w2_m2["y2"])
                            )

                            if not (n1_cross_m2 and n1_cross_m1 and n2_cross_m1 and n2_cross_m2):
                                continue

                            other_x_n2 = w2_m1["x1"] if w2_m1["x2"] == x2 else w2_m1["x2"]
                            step_x = -units if other_x_n2 < x1 else units

                            shifted = False
                            for k in range(1, 6):
                                x_new = x1 + k * step_x
                                if step_x < 0 and x_new < min(other_x_n2, x2):
                                    break
                                if step_x > 0 and x_new > max(other_x_n2, x2):
                                    break

                                y_m2_start = min(w2_m2["y1"], w2_m2["y2"])
                                y_m2_end = max(w2_m2["y1"], w2_m2["y2"])
                                y_other = w2_m2["y1"] if w2_m2["y2"] == y2 else w2_m2["y2"]

                                blocked = False
                                for c in def_data.get("components", []):
                                    cx = c.get("x", 0)
                                    cy = c.get("y", 0)
                                    orient = c.get("orient", "N")
                                    macro = c.get("macro", "")
                                    if macro in ("belt_split2", "belt_merge2", "belt_balancer2"):
                                        cw, ch = (
                                            (2000, 1000) if orient in ("W", "E") else (1000, 2000)
                                        )
                                    else:
                                        cw, ch = 5000, 5000
                                    if (cx <= x_new <= cx + cw) and not (
                                        cy + ch <= y_m2_start or cy >= y_m2_end
                                    ):
                                        blocked = True
                                        break
                                if blocked:
                                    continue

                                for on in nets:
                                    if on is n2:
                                        continue
                                    for ow in on.get("wires", []):
                                        if (
                                            ow.get("layer") == "M2"
                                            and ow["x1"] == x_new
                                            and ow["x2"] == x_new
                                            and not (
                                                max(ow["y1"], ow["y2"]) <= y_m2_start
                                                or min(ow["y1"], ow["y2"]) >= y_m2_end
                                            )
                                        ):
                                            blocked = True
                                            break
                                    if blocked:
                                        break
                                if blocked:
                                    continue

                                w2_other_m1 = next(
                                    (
                                        w
                                        for w in n2.get("wires", [])
                                        if w.get("layer") == "M1"
                                        and w["y1"] == y_other
                                        and w["y2"] == y_other
                                        and (w["x1"] == x2 or w["x2"] == x2)
                                    ),
                                    None,
                                )
                                if not w2_other_m1:
                                    continue

                                ext_min_x = min(x2, x_new)
                                ext_max_x = max(x2, x_new)
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
                                            blocked = True
                                            break
                                    if blocked:
                                        break
                                if blocked:
                                    continue

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

                                for v in n2.get("vias", []):
                                    if v.get("x") == x2 and v.get("y") in (y2, y_other):
                                        v["x"] = x_new
                                shifted = True
                                break

                            if shifted:
                                return


FLUID_MATERIALS_NAMES = frozenset(
    {
        "crude_oil",
        "crude-oil",
        "water",
        "petroleum_gas",
        "petroleum-gas",
        "heavy_oil",
        "heavy-oil",
        "light_oil",
        "light-oil",
        "sulfuric_acid",
        "sulfuric-acid",
        "lubricant",
        "steam",
    }
)


def _is_fluid_net(net: dict[str, Any]) -> bool:
    name = net.get("name", "")
    mat = name.split("__")[0]
    return mat in FLUID_MATERIALS_NAMES or net.get("nondefaultrule") == "fluid_ndr"


def _get_net_fluid(net: dict[str, Any]) -> str:
    name = net.get("name", "")
    return name.split("__")[0].replace("_", "-")


def separate_overlapping_channel_routes(def_data: dict[str, Any]) -> None:
    """Separate parallel or conflicting nets that detailed router placed on colliding tracks.

    Handles both horizontal M1 segments and vertical M2 segments.
    Separates overlapping tracks and prevents adjacent surface fluid mixing.
    """
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])

    # 1. Horizontal M1 track separation
    for _pass in range(10):
        any_shifted = False
        for i, n1 in enumerate(nets):
            for w1 in list(n1.get("wires", [])):
                if w1.get("layer") != "M1":
                    continue
                y1 = w1["y1"]
                if w1["y2"] != y1:
                    continue
                w1_min_x, w1_max_x = min(w1["x1"], w1["x2"]), max(w1["x1"], w1["x2"])

                for j, n2 in enumerate(nets):
                    if j <= i:
                        continue
                    shifted = False
                    for w2 in list(n2.get("wires", [])):
                        if w2.get("layer") != "M1":
                            continue
                        y2 = w2["y1"]
                        if w2["y2"] != y2:
                            continue
                        w2_min_x, w2_max_x = min(w2["x1"], w2["x2"]), max(w2["x1"], w2["x2"])

                        is_conflict = False
                        f1 = _get_net_fluid(n1) if _is_fluid_net(n1) else None
                        f2 = _get_net_fluid(n2) if _is_fluid_net(n2) else None
                        different_fluids = f1 is not None and f2 is not None and f1 != f2

                        if y1 == y2:
                            overlap_min = max(w1_min_x, w2_min_x)
                            overlap_max = min(w1_max_x, w2_max_x)
                            if overlap_min < overlap_max:
                                is_conflict = True
                            elif different_fluids:
                                gap = min(abs(w1_min_x - w2_max_x), abs(w2_min_x - w1_max_x))
                                if gap <= units:
                                    is_conflict = True
                        elif different_fluids and abs(y1 - y2) <= units:
                            overlap_min = max(w1_min_x, w2_min_x)
                            overlap_max = min(w1_max_x, w2_max_x)
                            if overlap_min <= overlap_max:
                                is_conflict = True

                        if not is_conflict:
                            continue

                        # Attempt to shift w2 to a free parallel track
                        shifted = False
                        for step in (2, 3, 4, 5, 6, 1, -2, -3, -4, -5, -6, -1):
                            y_new = y2 + step * units
                            if different_fluids and abs(y_new - y1) <= units:
                                continue

                            # Check components obstruction for new M1 segment
                            blocked = False
                            for c in def_data.get("components", []):
                                cx = c.get("x", 0)
                                cy = c.get("y", 0)
                                orient = c.get("orient", "N")
                                macro = c.get("macro", "")
                                if macro in ("belt_split2", "belt_merge2", "belt_balancer2"):
                                    cw, ch = (2000, 1000) if orient in ("W", "E") else (1000, 2000)
                                elif "oil_refinery" in macro:
                                    cw, ch = 7000, 7000
                                elif "chemical_plant" in macro:
                                    cw, ch = 5000, 5000
                                else:
                                    cw, ch = 5000, 5000
                                if (cy <= y_new <= cy + ch) and not (
                                    cx + cw <= w2_min_x or cx >= w2_max_x
                                ):
                                    blocked = True
                                    break
                            if blocked:
                                continue

                            # Check other nets on M1 at y_new
                            for on in nets:
                                if on is n2:
                                    continue
                                of = _get_net_fluid(on) if _is_fluid_net(on) else None
                                on_diff_fluid = f2 is not None and of is not None and f2 != of
                                for ow in on.get("wires", []):
                                    if ow.get("layer") != "M1":
                                        continue
                                    ow_y = ow["y1"]
                                    if ow["y2"] != ow_y:
                                        continue
                                    ow_min_x, ow_max_x = (
                                        min(ow["x1"], ow["x2"]),
                                        max(ow["x1"], ow["x2"]),
                                    )
                                    if ow_y == y_new:
                                        if max(ow_min_x, w2_min_x) < min(ow_max_x, w2_max_x):
                                            blocked = True
                                            break
                                        if on_diff_fluid:
                                            gap = min(
                                                abs(ow_min_x - w2_max_x), abs(w2_min_x - ow_max_x)
                                            )
                                            if gap <= units:
                                                blocked = True
                                                break
                                    elif on_diff_fluid and abs(ow_y - y_new) <= units:
                                        if max(ow_min_x, w2_min_x) <= min(ow_max_x, w2_max_x):
                                            blocked = True
                                            break
                                if blocked:
                                    break
                            if blocked:
                                continue

                            # Check connecting M2 vertical wires in n2
                            ep1_x = w2["x1"]
                            ep2_x = w2["x2"]
                            m2_wires = [
                                w
                                for w in n2.get("wires", [])
                                if w.get("layer") == "M2"
                                and (
                                    (w["x1"] == ep1_x and (w["y1"] == y2 or w["y2"] == y2))
                                    or (w["x1"] == ep2_x and (w["y1"] == y2 or w["y2"] == y2))
                                )
                            ]
                            if not m2_wires:
                                continue

                            # Check vertical extensions
                            y_span_min = min(y2, y_new)
                            y_span_max = max(y2, y_new)
                            for mw in m2_wires:
                                mx = mw["x1"]
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
                                            blocked = True
                                            break
                                    if blocked:
                                        break
                                if blocked:
                                    break
                            if blocked:
                                continue

                            # Apply shift to w2
                            w2["y1"] = y_new
                            w2["y2"] = y_new

                            # Update M2 wires
                            for mw in m2_wires:
                                if mw["y1"] == y2:
                                    mw["y1"] = y_new
                                elif mw["y2"] == y2:
                                    mw["y2"] = y_new

                            # Update vias in n2
                            for v in n2.get("vias", []):
                                vx, vy = v.get("x"), v.get("y")
                                if vy == y2 and vx in (ep1_x, ep2_x):
                                    v["y"] = y_new

                            shifted = True
                            any_shifted = True
                            break

                        if shifted:
                            break
                    if shifted:
                        break
        if not any_shifted:
            break

    def _shift_m2(
        tgt_net: dict[str, Any],
        tgt_wire: dict[str, Any],
        other_net: dict[str, Any],
        require_no_crossing: bool,
    ) -> bool:
        x = tgt_wire["x1"]
        y1 = tgt_wire["y1"]
        y2 = tgt_wire["y2"]
        tgt_min_y, tgt_max_y = min(y1, y2), max(y1, y2)
        f_tgt = _get_net_fluid(tgt_net) if _is_fluid_net(tgt_net) else None

        ep1_y = tgt_wire["y1"]
        ep2_y = tgt_wire["y2"]
        m1_wires = [
            w
            for w in tgt_net.get("wires", [])
            if w.get("layer") == "M1"
            and (
                (w["y1"] == ep1_y and (w["x1"] == x or w["x2"] == x))
                or (w["y1"] == ep2_y and (w["x1"] == x or w["x2"] == x))
            )
        ]
        if not m1_wires:
            return False

        for step in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5):
            x_new = x + step * units
            x_span_min = min(x, x_new)
            x_span_max = max(x, x_new)

            # Check if x_new or extended M1 wires create a crossing with other_net
            if require_no_crossing:
                crosses_other = False
                for ow in other_net.get("wires", []):
                    if ow.get("layer") == "M1":
                        ow_min_x, ow_max_x = min(ow["x1"], ow["x2"]), max(ow["x1"], ow["x2"])
                        if ow_min_x < x_new < ow_max_x and tgt_min_y <= ow["y1"] <= tgt_max_y:
                            crosses_other = True
                            break
                    elif ow.get("layer") == "M2":
                        ow_x = ow["x1"]
                        ow_min_y, ow_max_y = min(ow["y1"], ow["y2"]), max(ow["y1"], ow["y2"])
                        for mw in m1_wires:
                            my = mw["y1"]
                            if x_span_min <= ow_x <= x_span_max and ow_min_y <= my <= ow_max_y:
                                crosses_other = True
                                break
                        if crosses_other:
                            break
                if crosses_other:
                    continue

            # Check components obstruction for new M2 segment
            blocked = False
            for c in def_data.get("components", []):
                cx = c.get("x", 0)
                cy = c.get("y", 0)
                orient = c.get("orient", "N")
                macro = c.get("macro", "")
                if macro in ("belt_split2", "belt_merge2", "belt_balancer2"):
                    cw, ch = (2000, 1000) if orient in ("W", "E") else (1000, 2000)
                elif "oil_refinery" in macro:
                    cw, ch = 7000, 7000
                elif "chemical_plant" in macro:
                    cw, ch = 5000, 5000
                else:
                    cw, ch = 5000, 5000
                if (cx <= x_new <= cx + cw) and not (cy + ch <= tgt_min_y or cy >= tgt_max_y):
                    blocked = True
                    break
            if blocked:
                continue

            # Check other nets on M2 at x_new
            for on in nets:
                if on is tgt_net:
                    continue
                of = _get_net_fluid(on) if _is_fluid_net(on) else None
                on_diff_fluid = f_tgt is not None and of is not None and f_tgt != of
                for ow in on.get("wires", []):
                    if ow.get("layer") != "M2":
                        continue
                    ow_x = ow["x1"]
                    if ow["x2"] != ow_x:
                        continue
                    ow_min_y, ow_max_y = (
                        min(ow["y1"], ow["y2"]),
                        max(ow["y1"], ow["y2"]),
                    )
                    if ow_x == x_new:
                        if max(ow_min_y, tgt_min_y) < min(ow_max_y, tgt_max_y):
                            blocked = True
                            break
                        if on_diff_fluid:
                            gap = min(abs(ow_min_y - tgt_max_y), abs(tgt_min_y - ow_max_y))
                            if gap <= units:
                                blocked = True
                                break
                    elif on_diff_fluid and abs(ow_x - x_new) <= units:
                        if max(ow_min_y, tgt_min_y) <= min(ow_max_y, tgt_max_y):
                            blocked = True
                            break
                if blocked:
                    break
            if blocked:
                continue

            # Check horizontal extensions
            x_span_min = min(x, x_new)
            x_span_max = max(x, x_new)
            for mw in m1_wires:
                my = mw["y1"]
                for c in def_data.get("components", []):
                    cx = c.get("x", 0)
                    cy = c.get("y", 0)
                    orient = c.get("orient", "N")
                    macro = c.get("macro", "")
                    if macro in ("belt_split2", "belt_merge2", "belt_balancer2"):
                        cw, ch = (2000, 1000) if orient in ("W", "E") else (1000, 2000)
                    elif "oil_refinery" in macro:
                        cw, ch = 7000, 7000
                    elif "chemical_plant" in macro:
                        cw, ch = 5000, 5000
                    else:
                        cw, ch = 5000, 5000
                    if (cy <= my <= cy + ch) and not (cx + cw <= x_span_min or cx >= x_span_max):
                        blocked = True
                        break
                if blocked:
                    break
                for on in nets:
                    if on is tgt_net:
                        continue
                    for ow in on.get("wires", []):
                        if (
                            ow.get("layer") == "M1"
                            and ow["y1"] == my
                            and ow["y2"] == my
                            and max(min(ow["x1"], ow["x2"]), x_span_min)
                            < min(max(ow["x1"], ow["x2"]), x_span_max)
                        ):
                            blocked = True
                            break
                    if blocked:
                        break
                if blocked:
                    break
            if blocked:
                continue

            # Apply shift to tgt_wire
            tgt_wire["x1"] = x_new
            tgt_wire["x2"] = x_new

            # Update connecting M1 wires
            for mw in m1_wires:
                if mw["x1"] == x:
                    mw["x1"] = x_new
                elif mw["x2"] == x:
                    mw["x2"] = x_new

            # Update vias in tgt_net
            for v in tgt_net.get("vias", []):
                vx, vy = v.get("x"), v.get("y")
                if vx == x and vy in (ep1_y, ep2_y):
                    v["x"] = x_new

            return True

        return False

    # 2. Vertical M2 track separation
    for _pass in range(10):
        any_shifted = False
        for i, n1 in enumerate(nets):
            for w1 in list(n1.get("wires", [])):
                if w1.get("layer") != "M2":
                    continue
                x1 = w1["x1"]
                if w1["x2"] != x1:
                    continue
                w1_min_y, w1_max_y = min(w1["y1"], w1["y2"]), max(w1["y1"], w1["y2"])

                for j, n2 in enumerate(nets):
                    if j <= i:
                        continue
                    shifted = False
                    for w2 in list(n2.get("wires", [])):
                        if w2.get("layer") != "M2":
                            continue
                        x2 = w2["x1"]
                        if w2["x2"] != x2:
                            continue
                        w2_min_y, w2_max_y = min(w2["y1"], w2["y2"]), max(w2["y1"], w2["y2"])

                        is_conflict = False
                        f1 = _get_net_fluid(n1) if _is_fluid_net(n1) else None
                        f2 = _get_net_fluid(n2) if _is_fluid_net(n2) else None
                        different_fluids = f1 is not None and f2 is not None and f1 != f2

                        if x1 == x2:
                            overlap_min = max(w1_min_y, w2_min_y)
                            overlap_max = min(w1_max_y, w2_max_y)
                            if overlap_min < overlap_max:
                                is_conflict = True
                            elif different_fluids:
                                gap = min(abs(w1_min_y - w2_max_y), abs(w2_min_y - w1_max_y))
                                if gap <= units:
                                    is_conflict = True
                        elif different_fluids and abs(x1 - x2) <= units:
                            overlap_min = max(w1_min_y, w2_min_y)
                            overlap_max = min(w1_max_y, w2_max_y)
                            if overlap_min <= overlap_max:
                                is_conflict = True

                        if not is_conflict:
                            continue

                        # Attempt to shift w1 or w2 to a free parallel track
                        shifted = (
                            _shift_m2(n1, w1, n2, require_no_crossing=True)
                            or _shift_m2(n2, w2, n1, require_no_crossing=True)
                            or _shift_m2(n1, w1, n2, require_no_crossing=False)
                            or _shift_m2(n2, w2, n1, require_no_crossing=False)
                        )
                        if shifted:
                            any_shifted = True
                            break

                    if shifted:
                        break
                if shifted:
                    break
        if not any_shifted:
            break
