"""DEF net untangling for symmetric splitter and merger pins.

Eliminates avoidable crossings before blueprint translation by swapping
symmetric pin connections when initial wire tracks cross.
"""

from __future__ import annotations

from typing import Any


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
        gx = round(c.get("x", 0) * scale * 1000)
        gy = round(c.get("y", 0) * scale * 1000)

        p0, p1 = inputs[0], inputs[1]
        py0 = gy + 500
        py1 = gy + 1500

        n0 = next(
            (
                n
                for n in def_data.get("nets", [])
                if any(conn[0] == cname and conn[1] == p0 for conn in n.get("connections", []))
            ),
            None,
        )
        n1 = next(
            (
                n
                for n in def_data.get("nets", [])
                if any(conn[0] == cname and conn[1] == p1 for conn in n.get("connections", []))
            ),
            None,
        )
        if not n0 or not n1:
            continue

        pickup_x = gx - 500
        w_m2_cross = next(
            (
                w
                for w in n1.get("wires", [])
                if w.get("layer") == "M2"
                and abs(w.get("x1", 0) - pickup_x) <= 500
                and min(w["y1"], w["y2"]) <= py0 <= max(w["y1"], w["y2"])
            ),
            None,
        )
        w_m1_1 = next(
            (
                w
                for w in n1.get("wires", [])
                if w.get("layer") == "M1"
                and abs(w.get("x2", 0) - gx) <= 500
                and (w["y1"] == py1 or w["y2"] == py1)
            ),
            None,
        )

        needs_swap = (w_m2_cross is not None) or (
            w_m1_1 is not None
            and any(
                w.get("layer") == "M2"
                and abs(w.get("x1", 0) - pickup_x) <= 500
                and max(w["y1"], w["y2"]) == py1
                and min(w["y1"], w["y2"]) <= py0
                for w in n1.get("wires", [])
            )
        )

        if not needs_swap:
            continue

        conns0 = [conn for conn in n0.get("connections", []) if conn[0] != cname]
        conns1 = [conn for conn in n1.get("connections", []) if conn[0] != cname]
        n0["connections"] = [(cname, p1), *conns0]
        n1["connections"] = [(cname, p0), *conns1]

        for w in n1.get("wires", []):
            if w.get("layer") in ("M1", "M2"):
                if w["y1"] == py1:
                    w["y1"] = py0
                if w["y2"] == py1:
                    w["y2"] = py0

        for w in n0.get("wires", []):
            if w.get("layer") in ("M1", "M2"):
                if w["y1"] == py0:
                    w["y1"] = py1
                if w["y2"] == py0:
                    w["y2"] = py1
