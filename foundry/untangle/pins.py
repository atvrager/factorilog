"""Pin untangling and commutativity optimization for DEF layouts."""

from __future__ import annotations

from typing import Any

try:
    from foundry.def_parser import merge_collinear_wires
    from foundry.lef import get_macro_port_slots
    from foundry.untangle.spatial import get_component_box
except ImportError:
    from def_parser import merge_collinear_wires
    from lef import get_macro_port_slots

    from untangle.spatial import get_component_box


def untangle_splitter_outputs(def_data: dict[str, Any]) -> None:
    """Untangle symmetric splitter outputs whose initial routing segments cross.

    Splitter outputs y1 and y2 carry identical items at identical rates.
    When the router connects y1 and y2 such that one output turns across the other,
    swapping the initial segments and net connections eliminates the crossing.
    """
    scale = 1.0 / float(def_data.get("units", 1000))
    for c in def_data.get("components", []):
        if c.get("macro") not in ("belt_split2", "belt_balancer2"):
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
                and w["y1"] != w["y2"]
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
                and w["y1"] != w["y2"]
                and (
                    (w["x1"] == end2[0] and w["y1"] == end2[1])
                    or (w["x2"] == end2[0] and w["y2"] == end2[1])
                )
            ),
            None,
        )
        if not m2_1 and m2_2:
            other_y2 = m2_2["y2"] if m2_2["y1"] == end2[1] else m2_2["y1"]
            m1_w1_xs = [
                x
                for w in n_y1.get("wires", [])
                if w.get("layer") == "M1" and w["y1"] == py1[1] and w["y2"] == py1[1]
                for x in (w["x1"], w["x2"])
            ]
            min_w1_x = min(m1_w1_xs) if m1_w1_xs else min(w1["x1"], w1["x2"])
            max_w1_x = max(m1_w1_xs) if m1_w1_xs else max(w1["x1"], w1["x2"])
            if other_y2 < py1[1] and min_w1_x <= end2[0] <= max_w1_x:
                dst_inst, dst_pin = next(
                    ((inst, pin) for inst, pin in n_y1.get("connections", []) if inst != name),
                    (None, None),
                )
                dst_comp = next(
                    (comp for comp in def_data.get("components", []) if comp["name"] == dst_inst),
                    None,
                )
                can_swap = False
                if (
                    dst_comp
                    and dst_comp.get("macro") in ("belt_split2", "belt_merge2")
                    and dst_comp.get("y") == c.get("y")
                    and dst_pin == "a"
                ):
                    b_used = any(
                        any(inst == dst_inst and p == "b" for inst, p in on.get("connections", []))
                        for on in def_data.get("nets", [])
                    )
                    if not b_used:
                        can_swap = True

                if can_swap:
                    blocked = False
                    for other_net in def_data.get("nets", []):
                        if other_net is n_y1 or other_net is n_y2:
                            continue
                        for ow in other_net.get("wires", []):
                            if (
                                ow.get("layer") == "M1"
                                and ow["y1"] == ow["y2"] == py2[1]
                                and not (
                                    max(ow["x1"], ow["x2"]) <= end2[0]
                                    or min(ow["x1"], ow["x2"]) >= max_w1_x
                                )
                            ):
                                blocked = True
                                break
                        if blocked:
                            break
                    if not blocked:
                        w2["y1"] = py1[1]
                        w2["y2"] = py1[1]
                        if m2_2["y1"] == py2[1]:
                            m2_2["y1"] = py1[1]
                        else:
                            m2_2["y2"] = py1[1]
                        for v in n_y2.get("vias", []):
                            if v.get("x") == end2[0] and v.get("y") == py2[1]:
                                v["y"] = py1[1]
                        n_y2["connections"] = [
                            (name, "y1") if (inst == name and p == "y2") else (inst, p)
                            for inst, p in n_y2.get("connections", [])
                        ]
                        for w in n_y1.get("wires", []):
                            if w.get("layer") == "M1" and w["y1"] == py1[1] and w["y2"] == py1[1]:
                                w["y1"] = py2[1]
                                w["y2"] = py2[1]
                        for v in n_y1.get("vias", []):
                            if v.get("y") == py1[1]:
                                v["y"] = py2[1]
                        n_y1["connections"] = [
                            (name, "y2")
                            if (inst == name and p == "y1")
                            else (dst_inst, "b")
                            if (inst == dst_inst and p == "a")
                            else (inst, p)
                            for inst, p in n_y1.get("connections", [])
                        ]
                        n_y1["wires"] = merge_collinear_wires(
                            n_y1.get("wires", []), n_y1.get("vias", [])
                        )
                        n_y2["wires"] = merge_collinear_wires(
                            n_y2.get("wires", []), n_y2.get("vias", [])
                        )
                        continue

        if not m2_2 and m2_1:
            other_y1 = m2_1["y2"] if m2_1["y1"] == end1[1] else m2_1["y1"]
            m1_w2_xs = [
                x
                for w in n_y2.get("wires", [])
                if w.get("layer") == "M1" and w["y1"] == py2[1] and w["y2"] == py2[1]
                for x in (w["x1"], w["x2"])
            ]
            min_w2_x = min(m1_w2_xs) if m1_w2_xs else min(w2["x1"], w2["x2"])
            max_w2_x = max(m1_w2_xs) if m1_w2_xs else max(w2["x1"], w2["x2"])
            if other_y1 > py2[1] and min_w2_x <= end1[0] <= max_w2_x:
                dst_inst, dst_pin = next(
                    ((inst, pin) for inst, pin in n_y2.get("connections", []) if inst != name),
                    (None, None),
                )
                dst_comp = next(
                    (comp for comp in def_data.get("components", []) if comp["name"] == dst_inst),
                    None,
                )
                can_swap = False
                if (
                    dst_comp
                    and dst_comp.get("macro") in ("belt_split2", "belt_merge2")
                    and dst_comp.get("y") == c.get("y")
                    and dst_pin == "b"
                ):
                    a_used = any(
                        any(inst == dst_inst and p == "a" for inst, p in on.get("connections", []))
                        for on in def_data.get("nets", [])
                    )
                    if not a_used:
                        can_swap = True

                if can_swap:
                    blocked = False
                    for other_net in def_data.get("nets", []):
                        if other_net is n_y1 or other_net is n_y2:
                            continue
                        for ow in other_net.get("wires", []):
                            if (
                                ow.get("layer") == "M1"
                                and ow["y1"] == ow["y2"] == py1[1]
                                and not (
                                    max(ow["x1"], ow["x2"]) <= end1[0]
                                    or min(ow["x1"], ow["x2"]) >= max_w2_x
                                )
                            ):
                                blocked = True
                                break
                        if blocked:
                            break
                    if not blocked:
                        w1["y1"] = py2[1]
                        w1["y2"] = py2[1]
                        if m2_1["y1"] == py1[1]:
                            m2_1["y1"] = py2[1]
                        else:
                            m2_1["y2"] = py2[1]
                        for v in n_y1.get("vias", []):
                            if v.get("x") == end1[0] and v.get("y") == py1[1]:
                                v["y"] = py2[1]
                        n_y1["connections"] = [
                            (name, "y2") if (inst == name and p == "y1") else (inst, p)
                            for inst, p in n_y1.get("connections", [])
                        ]
                        for w in n_y2.get("wires", []):
                            if w.get("layer") == "M1" and w["y1"] == py2[1] and w["y2"] == py2[1]:
                                w["y1"] = py1[1]
                                w["y2"] = py1[1]
                        for v in n_y2.get("vias", []):
                            if v.get("y") == py2[1]:
                                v["y"] = py1[1]
                        n_y2["connections"] = [
                            (name, "y1")
                            if (inst == name and p == "y2")
                            else (dst_inst, "a")
                            if (inst == dst_inst and p == "b")
                            else (inst, p)
                            for inst, p in n_y2.get("connections", [])
                        ]
                        n_y1["wires"] = merge_collinear_wires(
                            n_y1.get("wires", []), n_y1.get("vias", [])
                        )
                        n_y2["wires"] = merge_collinear_wires(
                            n_y2.get("wires", []), n_y2.get("vias", [])
                        )
                        continue

        if not m2_1 or not m2_2:
            continue
        other_y1 = m2_1["y2"] if m2_1["y1"] == end1[1] else m2_1["y1"]
        other_y2 = m2_2["y2"] if m2_2["y1"] == end2[1] else m2_2["y1"]
        dy1 = "N" if other_y1 > end1[1] else "S"
        dy2 = "N" if other_y2 > end2[1] else "S"
        cross_pin = (
            (dy1 == "N" and dy2 == "S")
            or (dy1 == "N" and dy2 == "N" and end1[0] < end2[0])
            or (dy1 == "S" and dy2 == "S" and end2[0] < end1[0])
        )

        next_m1_1 = next(
            (
                w
                for w in n_y1.get("wires", [])
                if w.get("layer") == "M1"
                and w["y1"] == other_y1
                and (w["x1"] == end1[0] or w["x2"] == end1[0])
                and w is not w1
            ),
            None,
        )
        next_m1_2 = next(
            (
                w
                for w in n_y2.get("wires", [])
                if w.get("layer") == "M1"
                and w["y1"] == other_y2
                and (w["x1"] == end2[0] or w["x2"] == end2[0])
                and w is not w2
            ),
            None,
        )

        swap_trunks = False
        dir_x1 = ""
        dir_x2 = ""
        if next_m1_1 and next_m1_2:
            dir_x1 = (
                "E"
                if (next_m1_1["x2"] if next_m1_1["x1"] == end1[0] else next_m1_1["x1"]) > end1[0]
                else "W"
            )
            dir_x2 = (
                "E"
                if (next_m1_2["x2"] if next_m1_2["x1"] == end2[0] else next_m1_2["x1"]) > end2[0]
                else "W"
            )
            if (
                (dir_x1 == "W" and dir_x2 == "E" and end1[0] > end2[0])
                or (dir_x1 == "E" and dir_x2 == "W" and end1[0] < end2[0])
                or (
                    dir_x1 == "E"
                    and dir_x2 == "E"
                    and (
                        (dy1 == "S" and dy2 == "S" and other_y1 > other_y2 and end1[0] < end2[0])
                        or (dy1 == "S" and dy2 == "S" and other_y2 > other_y1 and end2[0] < end1[0])
                        or (dy1 == "N" and dy2 == "N" and other_y1 < other_y2 and end1[0] < end2[0])
                        or (dy1 == "N" and dy2 == "N" and other_y2 < other_y1 and end2[0] < end1[0])
                    )
                )
                or (
                    dir_x1 == "W"
                    and dir_x2 == "W"
                    and (
                        (dy1 == "S" and dy2 == "S" and other_y1 > other_y2 and end1[0] > end2[0])
                        or (dy1 == "S" and dy2 == "S" and other_y2 > other_y1 and end2[0] > end1[0])
                        or (dy1 == "N" and dy2 == "N" and other_y1 < other_y2 and end1[0] > end2[0])
                        or (dy1 == "N" and dy2 == "N" and other_y2 < other_y1 and end2[0] > end1[0])
                    )
                )
            ):
                swap_trunks = True

        if not cross_pin and not swap_trunks:
            continue

        if swap_trunks and next_m1_1 and next_m1_2:
            x_inner = min(end1[0], end2[0])
            x_outer = max(end1[0], end2[0])
            if dir_x1 == "W" and dir_x2 == "E":
                net_in, old_x_in, other_y_in, w_pin_in, m2_in, next_m1_in = (
                    n_y1,
                    end1[0],
                    other_y1,
                    w1,
                    m2_1,
                    next_m1_1,
                )
                net_out, old_x_out, other_y_out, w_pin_out, m2_out, next_m1_out = (
                    n_y2,
                    end2[0],
                    other_y2,
                    w2,
                    m2_2,
                    next_m1_2,
                )
            elif dir_x1 == "E" and dir_x2 == "W":
                net_in, old_x_in, other_y_in, w_pin_in, m2_in, next_m1_in = (
                    n_y2,
                    end2[0],
                    other_y2,
                    w2,
                    m2_2,
                    next_m1_2,
                )
                net_out, old_x_out, other_y_out, w_pin_out, m2_out, next_m1_out = (
                    n_y1,
                    end1[0],
                    other_y1,
                    w1,
                    m2_1,
                    next_m1_1,
                )
            elif dir_x1 == "E" and dir_x2 == "E":
                if (dy1 == "S" and other_y1 > other_y2) or (dy1 == "N" and other_y1 < other_y2):
                    net_out, old_x_out, other_y_out, w_pin_out, m2_out, next_m1_out = (
                        n_y1,
                        end1[0],
                        other_y1,
                        w1,
                        m2_1,
                        next_m1_1,
                    )
                    net_in, old_x_in, other_y_in, w_pin_in, m2_in, next_m1_in = (
                        n_y2,
                        end2[0],
                        other_y2,
                        w2,
                        m2_2,
                        next_m1_2,
                    )
                else:
                    net_in, old_x_in, other_y_in, w_pin_in, m2_in, next_m1_in = (
                        n_y1,
                        end1[0],
                        other_y1,
                        w1,
                        m2_1,
                        next_m1_1,
                    )
                    net_out, old_x_out, other_y_out, w_pin_out, m2_out, next_m1_out = (
                        n_y2,
                        end2[0],
                        other_y2,
                        w2,
                        m2_2,
                        next_m1_2,
                    )
            else:
                if (dy1 == "S" and other_y1 > other_y2) or (dy1 == "N" and other_y1 < other_y2):
                    net_in, old_x_in, other_y_in, w_pin_in, m2_in, next_m1_in = (
                        n_y1,
                        end1[0],
                        other_y1,
                        w1,
                        m2_1,
                        next_m1_1,
                    )
                    net_out, old_x_out, other_y_out, w_pin_out, m2_out, next_m1_out = (
                        n_y2,
                        end2[0],
                        other_y2,
                        w2,
                        m2_2,
                        next_m1_2,
                    )
                else:
                    net_out, old_x_out, other_y_out, w_pin_out, m2_out, next_m1_out = (
                        n_y1,
                        end1[0],
                        other_y1,
                        w1,
                        m2_1,
                        next_m1_1,
                    )
                    net_in, old_x_in, other_y_in, w_pin_in, m2_in, next_m1_in = (
                        n_y2,
                        end2[0],
                        other_y2,
                        w2,
                        m2_2,
                        next_m1_2,
                    )

            if dy1 == "N":
                py_in, pin_name_in = py2, "y2"
                py_out, pin_name_out = py1, "y1"
            else:
                py_in, pin_name_in = py1, "y1"
                py_out, pin_name_out = py2, "y2"

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
                                max(ow["x1"], ow["x2"]) < py1[0]
                                or min(ow["x1"], ow["x2"]) > x_outer
                            )
                        ):
                            blocked = True
                            break
                        if (
                            ow["y1"] == py2[1]
                            and ow["y2"] == py2[1]
                            and not (
                                max(ow["x1"], ow["x2"]) < py2[0]
                                or min(ow["x1"], ow["x2"]) > x_outer
                            )
                        ):
                            blocked = True
                            break
                    elif layer == "M2":
                        if ow["x1"] == x_inner and ow["x2"] == x_inner:
                            y_min = min(py_in[1], other_y_in)
                            y_max = max(py_in[1], other_y_in)
                            if not (
                                max(ow["y1"], ow["y2"]) < y_min or min(ow["y1"], ow["y2"]) > y_max
                            ):
                                blocked = True
                                break
                        if ow["x1"] == x_outer and ow["x2"] == x_outer:
                            y_min = min(py_out[1], other_y_out)
                            y_max = max(py_out[1], other_y_out)
                            if not (
                                max(ow["y1"], ow["y2"]) < y_min or min(ow["y1"], ow["y2"]) > y_max
                            ):
                                blocked = True
                                break
                if blocked:
                    break

            if not blocked:
                netlist = def_data.get("_netlist_data")
                units = int(def_data.get("units", 1000))
                y_min_in = min(py_in[1], other_y_in)
                y_max_in = max(py_in[1], other_y_in)
                y_min_out = min(py_out[1], other_y_out)
                y_max_out = max(py_out[1], other_y_out)
                for comp in def_data.get("components", []):
                    if comp.get("name") == name:
                        continue
                    cx, cy, cw, ch = get_component_box(comp, units, netlist)
                    if cx <= x_inner <= cx + cw and not (cy + ch <= y_min_in or cy >= y_max_in):
                        blocked = True
                        break
                    if cx <= x_outer <= cx + cw and not (cy + ch <= y_min_out or cy >= y_max_out):
                        blocked = True
                        break
                    x_min_pin_in = min(py_in[0], x_inner)
                    x_max_pin_in = max(py_in[0], x_inner)
                    if cy <= py_in[1] <= cy + ch and not (
                        cx + cw <= x_min_pin_in or cx >= x_max_pin_in
                    ):
                        blocked = True
                        break
                    x_min_pin_out = min(py_out[0], x_outer)
                    x_max_pin_out = max(py_out[0], x_outer)
                    if cy <= py_out[1] <= cy + ch and not (
                        cx + cw <= x_min_pin_out or cx >= x_max_pin_out
                    ):
                        blocked = True
                        break

            if not blocked:
                if w_pin_in["x1"] == py_in[0]:
                    w_pin_in["x2"] = x_inner
                else:
                    w_pin_in["x1"] = x_inner
                w_pin_in["y1"] = py_in[1]
                w_pin_in["y2"] = py_in[1]

                m2_in["x1"] = x_inner
                m2_in["x2"] = x_inner
                if m2_in["y1"] == other_y_in:
                    m2_in["y2"] = py_in[1]
                else:
                    m2_in["y1"] = py_in[1]

                if next_m1_in["x1"] == old_x_in:
                    next_m1_in["x1"] = x_inner
                else:
                    next_m1_in["x2"] = x_inner

                conns_in = [conn for conn in net_in.get("connections", []) if conn[0] != name]
                net_in["connections"] = [(name, pin_name_in), *conns_in]

                for v in net_in.get("vias", []):
                    if v.get("x") == old_x_in:
                        v["x"] = x_inner
                    if v.get("y") in (py1[1], py2[1]):
                        v["y"] = py_in[1]

                if w_pin_out["x1"] == py_out[0]:
                    w_pin_out["x2"] = x_outer
                else:
                    w_pin_out["x1"] = x_outer
                w_pin_out["y1"] = py_out[1]
                w_pin_out["y2"] = py_out[1]

                m2_out["x1"] = x_outer
                m2_out["x2"] = x_outer
                if m2_out["y1"] == other_y_out:
                    m2_out["y2"] = py_out[1]
                else:
                    m2_out["y1"] = py_out[1]

                if next_m1_out["x1"] == old_x_out:
                    next_m1_out["x1"] = x_outer
                else:
                    next_m1_out["x2"] = x_outer

                conns_out = [conn for conn in net_out.get("connections", []) if conn[0] != name]
                net_out["connections"] = [(name, pin_name_out), *conns_out]

                for v in net_out.get("vias", []):
                    if v.get("x") == old_x_out:
                        v["x"] = x_outer
                    if v.get("y") in (py1[1], py2[1]):
                        v["y"] = py_out[1]
                continue

        if not cross_pin:
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
                            max(ow["x1"], ow["x2"]) < x_min_1 or min(ow["x1"], ow["x2"]) > x_max_1
                        )
                    ):
                        blocked = True
                        break
                    if (
                        ow["y1"] == py2[1]
                        and ow["y2"] == py2[1]
                        and not (
                            max(ow["x1"], ow["x2"]) < x_min_2 or min(ow["x1"], ow["x2"]) > x_max_2
                        )
                    ):
                        blocked = True
                        break
                elif layer == "M2":
                    if (
                        ow["x1"] == end1[0]
                        and ow["x2"] == end1[0]
                        and not (
                            max(ow["y1"], ow["y2"]) < y_min_1 or min(ow["y1"], ow["y2"]) > y_max_1
                        )
                    ):
                        blocked = True
                        break
                    if (
                        ow["x1"] == end2[0]
                        and ow["x2"] == end2[0]
                        and not (
                            max(ow["y1"], ow["y2"]) < y_min_2 or min(ow["y1"], ow["y2"]) > y_max_2
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

    Splitters face East and only accept inputs on their West face. When an input net
    approaches from the East into an input pin, it creates an illegal backwards segment.
    Removing this segment allows the vertical belt to side-load directly into the
    splitter output.
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
    """Untangle symmetric merger and balancer inputs whose routing segments cross."""
    scale = 1.0 / float(def_data.get("units", 1000))
    units = int(def_data.get("units", 1000))
    for c in def_data.get("components", []):
        macro = c.get("macro", "")
        if macro not in ("belt_merge2", "belt_balancer2"):
            continue
        pin_a_name = "a1" if macro == "belt_balancer2" else "a"
        pin_b_name = "a2" if macro == "belt_balancer2" else "b"
        name = c.get("name", "")
        gx = c.get("x", 0) * scale
        gy = c.get("y", 0) * scale
        n_a = None
        n_b = None
        for n in def_data.get("nets", []):
            for inst, pin in n.get("connections", []):
                if inst == name and pin == pin_a_name:
                    n_a = n
                elif inst == name and pin == pin_b_name:
                    n_b = n
        if not n_a or not n_b or n_a is n_b:
            continue
        pa = (round((gx + 0.25) * units), round((gy + 0.5) * units))
        pb = (round((gx + 0.25) * units), round((gy + 1.5) * units))
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
                and w["y1"] != w["y2"]
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
                and w["y1"] != w["y2"]
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
        dy_a = "N" if other_ya > enda[1] else "S"
        other_yb = m2_b["y2"] if m2_b["y1"] == endb[1] else m2_b["y1"]
        dy_b = "N" if other_yb > endb[1] else "S"

        # Check for entrance crossing (trunks cross near pins)
        entrance_cross = dy_a == "N" and dy_b == "S"
        if (
            dy_a == "N" and dy_b == "N" and (min(endb[0], pb[0]) < enda[0] < max(endb[0], pb[0]))
        ) or (
            dy_a == "S" and dy_b == "S" and (min(enda[0], pa[0]) < endb[0] < max(enda[0], pa[0]))
        ):
            entrance_cross = True

        # Check for approach crossing (M1 approach wire crosses the other net's M2 trunk)
        m1_app_a = next(
            (
                w
                for w in n_a.get("wires", [])
                if w.get("layer") == "M1"
                and w["y1"] == other_ya
                and (w["x1"] == enda[0] or w["x2"] == enda[0])
                and w is not wa
            ),
            None,
        )
        m1_app_b = next(
            (
                w
                for w in n_b.get("wires", [])
                if w.get("layer") == "M1"
                and w["y1"] == other_yb
                and (w["x1"] == endb[0] or w["x2"] == endb[0])
                and w is not wb
            ),
            None,
        )
        approach_cross = False
        if (
            m1_app_a
            and (min(other_yb, pb[1]) <= other_ya <= max(other_yb, pb[1]))
            and (
                min(m1_app_a["x1"], m1_app_a["x2"]) < endb[0] < max(m1_app_a["x1"], m1_app_a["x2"])
            )
        ):
            approach_cross = True
        if (
            m1_app_b
            and (min(other_ya, pa[1]) <= other_yb <= max(other_ya, pa[1]))
            and (
                min(m1_app_b["x1"], m1_app_b["x2"]) < enda[0] < max(m1_app_b["x1"], m1_app_b["x2"])
            )
        ):
            approach_cross = True

        if not entrance_cross and not approach_cross:
            continue

        # If approach cross and trunks are adjacent (<= 2 units apart), swap tracks and pins
        if approach_cross and abs(enda[0] - endb[0]) <= 2 * units and m1_app_a and m1_app_b:
            x_min_a = min(enda[0], endb[0])
            x_max_a = max(enda[0], endb[0])
            blocked = False
            for other_net in def_data.get("nets", []):
                if other_net is n_a or other_net is n_b:
                    continue
                for ow in other_net.get("wires", []):
                    layer = ow.get("layer")
                    if layer == "M1":
                        if ow["y1"] == other_ya and not (
                            max(ow["x1"], ow["x2"]) <= x_min_a or min(ow["x1"], ow["x2"]) >= x_max_a
                        ):
                            blocked = True
                            break
                        if ow["y1"] == other_yb and not (
                            max(ow["x1"], ow["x2"]) <= x_min_a or min(ow["x1"], ow["x2"]) >= x_max_a
                        ):
                            blocked = True
                            break
                    elif layer == "M2":
                        if ow["x1"] == endb[0] and not (
                            max(ow["y1"], ow["y2"]) <= min(other_ya, pb[1])
                            or min(ow["y1"], ow["y2"]) >= max(other_ya, pb[1])
                        ):
                            blocked = True
                            break
                        if ow["x1"] == enda[0] and not (
                            max(ow["y1"], ow["y2"]) <= min(other_yb, pa[1])
                            or min(ow["y1"], ow["y2"]) >= max(other_yb, pa[1])
                        ):
                            blocked = True
                            break
                if blocked:
                    break
            if not blocked:
                if m1_app_a["x1"] == enda[0]:
                    m1_app_a["x1"] = endb[0]
                else:
                    m1_app_a["x2"] = endb[0]
                if m1_app_b["x1"] == endb[0]:
                    m1_app_b["x1"] = enda[0]
                else:
                    m1_app_b["x2"] = enda[0]

                m2_a["x1"] = endb[0]
                m2_a["x2"] = endb[0]
                if m2_a["y1"] == other_ya:
                    m2_a["y2"] = pb[1]
                else:
                    m2_a["y1"] = pb[1]

                m2_b["x1"] = enda[0]
                m2_b["x2"] = enda[0]
                if m2_b["y1"] == other_yb:
                    m2_b["y2"] = pa[1]
                else:
                    m2_b["y1"] = pa[1]

                wa["y1"] = pb[1]
                wa["y2"] = pb[1]
                wa["x1"] = min(endb[0], pb[0])
                wa["x2"] = max(endb[0], pb[0])

                wb["y1"] = pa[1]
                wb["y2"] = pa[1]
                wb["x1"] = min(enda[0], pa[0])
                wb["x2"] = max(enda[0], pa[0])

                n_a["connections"] = [
                    (inst, pin_b_name if inst == name and p == pin_a_name else p)
                    for inst, p in n_a.get("connections", [])
                ]
                n_b["connections"] = [
                    (inst, pin_a_name if inst == name and p == pin_b_name else p)
                    for inst, p in n_b.get("connections", [])
                ]
                continue

        # Otherwise, direct pin swap: keep M2 trunks on their tracks, adjust trunk Y-end and stubs
        y_min_a = min(other_ya, pb[1])
        y_max_a = max(other_ya, pb[1])
        y_min_b = min(other_yb, pa[1])
        y_max_b = max(other_yb, pa[1])
        x_min_a = min(enda[0], pb[0])
        x_max_a = max(enda[0], pb[0])
        x_min_b = min(endb[0], pa[0])
        x_max_b = max(endb[0], pa[0])

        blocked = False
        for other_net in def_data.get("nets", []):
            if other_net is n_a or other_net is n_b:
                continue
            for ow in other_net.get("wires", []):
                layer = ow.get("layer")
                if layer == "M1":
                    if ow["y1"] == pb[1] and not (
                        max(ow["x1"], ow["x2"]) <= x_min_a or min(ow["x1"], ow["x2"]) >= x_max_a
                    ):
                        blocked = True
                        break
                    if ow["y1"] == pa[1] and not (
                        max(ow["x1"], ow["x2"]) <= x_min_b or min(ow["x1"], ow["x2"]) >= x_max_b
                    ):
                        blocked = True
                        break
                elif layer == "M2":
                    if ow["x1"] == enda[0] and not (
                        max(ow["y1"], ow["y2"]) <= y_min_a or min(ow["y1"], ow["y2"]) >= y_max_a
                    ):
                        blocked = True
                        break
                    if ow["x1"] == endb[0] and not (
                        max(ow["y1"], ow["y2"]) <= y_min_b or min(ow["y1"], ow["y2"]) >= y_max_b
                    ):
                        blocked = True
                        break
            if blocked:
                break
        if blocked:
            continue

        if m2_a["y1"] == other_ya:
            m2_a["y2"] = pb[1]
        else:
            m2_a["y1"] = pb[1]

        if m2_b["y1"] == other_yb:
            m2_b["y2"] = pa[1]
        else:
            m2_b["y1"] = pa[1]

        wa["y1"] = pb[1]
        wa["y2"] = pb[1]
        wa["x1"] = min(enda[0], pb[0])
        wa["x2"] = max(enda[0], pb[0])

        wb["y1"] = pa[1]
        wb["y2"] = pa[1]
        wb["x1"] = min(endb[0], pa[0])
        wb["x2"] = max(endb[0], pa[0])

        n_a["connections"] = [
            (inst, pin_b_name if inst == name and p == pin_a_name else p)
            for inst, p in n_a.get("connections", [])
        ]
        n_b["connections"] = [
            (inst, pin_a_name if inst == name and p == pin_b_name else p)
            for inst, p in n_b.get("connections", [])
        ]


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
        elif other_ya > enda[1] and other_yb > endb[1] and other_ya > other_yb:
            x_shift = x_track - units
            w_arr_b = next(
                (
                    w
                    for w in n_b.get("wires", [])
                    if w.get("layer") == "M1"
                    and w["y1"] == other_yb
                    and (w["x1"] == x_track or w["x2"] == x_track)
                ),
                None,
            )
            if w_arr_b:
                if w_arr_b["x1"] == x_track:
                    w_arr_b["x1"] = x_shift
                else:
                    w_arr_b["x2"] = x_shift
                m2_b["x1"] = x_shift
                m2_b["x2"] = x_shift
                if m2_b["y1"] == other_yb:
                    m2_b["y2"] = pa[1]
                else:
                    m2_b["y1"] = pa[1]
                wb["x1"] = x_shift
                wb["x2"] = pa[0]
                wb["y1"] = pa[1]
                wb["y2"] = pa[1]
                m2_a["x1"] = x_track
                m2_a["x2"] = x_track
                if m2_a["y1"] == other_ya:
                    m2_a["y2"] = pb[1]
                else:
                    m2_a["y1"] = pb[1]
                wa["x1"] = x_track
                wa["x2"] = pb[0]
                wa["y1"] = pb[1]
                wa["y2"] = pb[1]
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
                for v in n_b.get("vias", []):
                    if v.get("x") == x_track and v.get("y") == other_yb:
                        v["x"] = x_shift
                    elif v.get("x") == x_track and v.get("y") == pb[1]:
                        v["x"] = x_shift
                        v["y"] = pa[1]
                for v in n_a.get("vias", []):
                    if v.get("x") == x_track and v.get("y") == pa[1]:
                        v["y"] = pb[1]


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
