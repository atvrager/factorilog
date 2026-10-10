"""DEF net untangling for symmetric splitter and merger pins.

Eliminates avoidable crossings before blueprint translation by swapping
symmetric pin connections when initial wire tracks cross.
"""

from __future__ import annotations

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
