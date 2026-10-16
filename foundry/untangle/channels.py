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
                                break


def shift_m2_segment(
    def_data: dict[str, Any],
    tgt_net: dict[str, Any],
    tgt_wire: dict[str, Any],
    x_new: int,
    netlist_data: dict[str, Any] | None = None,
    fluids: set[str] | frozenset[str] | None = None,
) -> bool:
    """Shift an M2 vertical wire segment to a new X coordinate.

    Stretches or shortens connecting M1 segments and updates vias.
    Verifies that the new position and extensions do not collide with
    components, other nets, or incompatible fluids.
    """
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])
    if netlist_data is None:
        netlist_data = def_data.get("_netlist_data")
    if fluids is None and netlist_data is not None:
        fluids = set(netlist_data.get("fluids", []))

    x = tgt_wire.get("x1", 0)
    if tgt_wire.get("x2") != x or x_new == x:
        return False
    y1 = tgt_wire.get("y1", 0)
    y2 = tgt_wire.get("y2", 0)
    tgt_min_y, tgt_max_y = min(y1, y2), max(y1, y2)
    f_tgt = _get_net_fluid(tgt_net) if _is_fluid_net(tgt_net, fluids=fluids) else None

    ep1_y, ep2_y = y1, y2
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
    has_ep1 = any(w["y1"] == ep1_y and (w["x1"] == x or w["x2"] == x) for w in m1_wires)
    has_ep2 = any(w["y1"] == ep2_y and (w["x1"] == x or w["x2"] == x) for w in m1_wires)
    if not (has_ep1 and has_ep2):
        return False

    touching_tgt = [
        w for w in tgt_net.get("wires", []) if w is not tgt_wire and wires_touch(w, tgt_wire)
    ]
    if any(w not in m1_wires for w in touching_tgt):
        return False

    for c in def_data.get("components", []):
        cx, cy, cw, ch = get_component_box(c, units, netlist_data)
        if (cx <= x_new <= cx + cw) and not (cy + ch <= tgt_min_y or cy >= tgt_max_y):
            return False

    for on in nets:
        if on is tgt_net:
            continue
        of = _get_net_fluid(on) if _is_fluid_net(on, fluids=fluids) else None
        on_diff_fluid = f_tgt is not None and of is not None and f_tgt != of
        for ow in on.get("wires", []):
            if ow.get("layer") != "M2" or ow.get("x1") != ow.get("x2"):
                continue
            ow_x = ow["x1"]
            ow_min_y, ow_max_y = min(ow["y1"], ow["y2"]), max(ow["y1"], ow["y2"])
            if ow_x == x_new:
                if max(ow_min_y, tgt_min_y) < min(ow_max_y, tgt_max_y):
                    return False
                if on_diff_fluid:
                    gap = min(abs(ow_min_y - tgt_max_y), abs(tgt_min_y - ow_max_y))
                    if gap <= units:
                        return False
            elif on_diff_fluid and abs(ow_x - x_new) <= units:
                if max(ow_min_y, tgt_min_y) <= min(ow_max_y, tgt_max_y):
                    return False

    x_span_min = min(x, x_new)
    x_span_max = max(x, x_new)
    for mw in m1_wires:
        my = mw["y1"]
        for c in def_data.get("components", []):
            cx, cy, cw, ch = get_component_box(c, units, netlist_data)
            if (cy <= my <= cy + ch) and not (cx + cw <= x_span_min or cx >= x_span_max):
                return False
        for on in nets:
            if on is tgt_net:
                continue
            for ow in on.get("wires", []):
                if ow.get("layer") == "M1" and ow.get("y1") == my and ow.get("y2") == my:
                    ow_min_x, ow_max_x = min(ow["x1"], ow["x2"]), max(ow["x1"], ow["x2"])
                    if max(ow_min_x, x_span_min) < min(ow_max_x, x_span_max):
                        return False

    tgt_wire["x1"] = x_new
    tgt_wire["x2"] = x_new
    for mw in m1_wires:
        if mw["x1"] == x:
            mw["x1"] = x_new
        elif mw["x2"] == x:
            mw["x2"] = x_new

    for v in tgt_net.get("vias", []):
        vx, vy = v.get("x"), v.get("y")
        if vx == x and vy in (ep1_y, ep2_y):
            v["x"] = x_new

    return True


def separate_overlapping_channel_routes(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any] | None = None,
    fluids: set[str] | frozenset[str] | None = None,
) -> None:
    """Separate parallel or conflicting nets that detailed router placed on colliding tracks.

    Handles both horizontal M1 segments and vertical M2 segments.
    Separates overlapping tracks and prevents adjacent surface fluid mixing.
    """
    if fluids is None and netlist_data is not None:
        fluids = set(netlist_data.get("fluids", []))
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
                        f1 = _get_net_fluid(n1) if _is_fluid_net(n1, fluids=fluids) else None
                        f2 = _get_net_fluid(n2) if _is_fluid_net(n2, fluids=fluids) else None
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
                                of = (
                                    _get_net_fluid(on) if _is_fluid_net(on, fluids=fluids) else None
                                )
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
                            touching_w2 = [
                                w for w in n2.get("wires", []) if w is not w2 and wires_touch(w, w2)
                            ]
                            if any(w not in m2_wires for w in touching_w2):
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
        y1, y2 = tgt_wire["y1"], tgt_wire["y2"]
        tgt_min_y, tgt_max_y = min(y1, y2), max(y1, y2)
        ep1_y, ep2_y = y1, y2
        m1_wires = [
            w
            for w in tgt_net.get("wires", [])
            if w.get("layer") == "M1"
            and (
                (w["y1"] == ep1_y and (w["x1"] == x or w["x2"] == x))
                or (w["y1"] == ep2_y and (w["x1"] == x or w["x2"] == x))
            )
        ]
        for step in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5):
            x_new = x + step * units
            x_span_min = min(x, x_new)
            x_span_max = max(x, x_new)
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
            if shift_m2_segment(def_data, tgt_net, tgt_wire, x_new, netlist_data, fluids):
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
                        f1 = _get_net_fluid(n1) if _is_fluid_net(n1, fluids=fluids) else None
                        f2 = _get_net_fluid(n2) if _is_fluid_net(n2, fluids=fluids) else None
                        different_fluids = f1 is not None and f2 is not None and f1 != f2

                        if x1 == x2:
                            overlap_min = max(w1_min_y, w2_min_y)
                            overlap_max = min(w1_max_y, w2_max_y)
                            if overlap_min <= overlap_max:
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


def untangle_congested_turn_crossings(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any] | None = None,
) -> None:
    """Untangle crossings that bottleneck within 1 tile of turns.

    When an M2 segment crosses an M1 segment within 1 tile of a turn,
    Factorio underground jumper placement fails because curved turn belts
    cannot serve as underground belt entrance or exit hoods.
    Shifting the M2 segment creates a straight 2-tile runway for the jumper
    or eliminates the crossing completely.
    """
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])
    if netlist_data is None:
        netlist_data = def_data.get("_netlist_data")
    fluids = set(netlist_data.get("fluids", [])) if netlist_data else None

    for _pass in range(10):
        any_shifted = False
        m1_by_y: collections.defaultdict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = (
            collections.defaultdict(list)
        )
        for on in nets:
            for ow in on.get("wires", []):
                if ow.get("layer") == "M1" and ow.get("y1") == ow.get("y2"):
                    m1_by_y[ow["y1"]].append((on, ow))

        for n1 in nets:
            for w_m2 in list(n1.get("wires", [])):
                if w_m2.get("layer") != "M2" or w_m2.get("x1") != w_m2.get("x2"):
                    continue
                x_m2 = w_m2["x1"]
                m2_min_y, m2_max_y = min(w_m2["y1"], w_m2["y2"]), max(w_m2["y1"], w_m2["y2"])
                candidate_ys = [y for y in m1_by_y if m2_min_y <= y <= m2_max_y]

                shifted = False
                for y_m1 in candidate_ys:
                    for n2, w_m1 in m1_by_y[y_m1]:
                        if n1 is n2:
                            continue
                        m1_min_x, m1_max_x = (
                            min(w_m1["x1"], w_m1["x2"]),
                            max(w_m1["x1"], w_m1["x2"]),
                        )
                        dist_m1 = min(abs(x_m2 - m1_min_x), abs(x_m2 - m1_max_x))
                        dist_m2 = min(abs(y_m1 - m2_min_y), abs(y_m1 - m2_max_y))
                        if not (m1_min_x <= x_m2 <= m1_max_x):
                            continue
                        if dist_m1 > 2 * units or dist_m2 > 2 * units:
                            continue

                        # Candidate 1: Shift w_m2 on n1
                        for step in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5):
                            x_new = x_m2 + step * units
                            if (
                                m1_min_x <= x_new <= m1_max_x
                                and min(abs(x_new - m1_min_x), abs(x_new - m1_max_x)) <= 2 * units
                            ):
                                continue

                            has_new_congestion = False
                            for cy in candidate_ys:
                                for on, ow in m1_by_y[cy]:
                                    if on is n1:
                                        continue
                                    ox_min, ox_max = (
                                        min(ow["x1"], ow["x2"]),
                                        max(ow["x1"], ow["x2"]),
                                    )
                                    if (
                                        ox_min <= x_new <= ox_max
                                        and min(abs(x_new - ox_min), abs(x_new - ox_max))
                                        <= 2 * units
                                    ):
                                        has_new_congestion = True
                                        break
                                if has_new_congestion:
                                    break
                            if has_new_congestion:
                                continue

                            res = shift_m2_segment(def_data, n1, w_m2, x_new, netlist_data, fluids)
                            if res:
                                shifted = True
                                any_shifted = True
                                break

                        if shifted:
                            break

                        # Candidate 2: Shift M2 wire connected to w_m1 on n2
                        if dist_m1 <= 2 * units:
                            w_m2_conn = next(
                                (
                                    w
                                    for w in n2.get("wires", [])
                                    if w.get("layer") == "M2"
                                    and (w.get("x1") in (m1_min_x, m1_max_x))
                                    and w.get("x1") == w.get("x2")
                                    and (w.get("y1") == y_m1 or w.get("y2") == y_m1)
                                ),
                                None,
                            )
                            if w_m2_conn:
                                ep_x = w_m2_conn["x1"]
                                steps = (
                                    (1, 2, 3, 4, 5) if ep_x == m1_max_x else (-1, -2, -3, -4, -5)
                                )
                                for step in steps:
                                    x_new = ep_x + step * units
                                    if abs(x_new - x_m2) < 2 * units:
                                        continue
                                    conn_min_y = min(w_m2_conn["y1"], w_m2_conn["y2"])
                                    conn_max_y = max(w_m2_conn["y1"], w_m2_conn["y2"])
                                    conn_candidate_ys = [
                                        y for y in m1_by_y if conn_min_y <= y <= conn_max_y
                                    ]
                                    has_new_congestion = False
                                    for cy in conn_candidate_ys:
                                        for on, ow in m1_by_y[cy]:
                                            if on is n2:
                                                continue
                                            ox_min = min(ow["x1"], ow["x2"])
                                            ox_max = max(ow["x1"], ow["x2"])
                                            if (
                                                ox_min <= x_new <= ox_max
                                                and min(abs(x_new - ox_min), abs(x_new - ox_max))
                                                <= 2 * units
                                            ):
                                                has_new_congestion = True
                                                break
                                        if has_new_congestion:
                                            break
                                    if has_new_congestion:
                                        continue

                                    if shift_m2_segment(
                                        def_data, n2, w_m2_conn, x_new, netlist_data, fluids
                                    ):
                                        shifted = True
                                        any_shifted = True
                                        break
                        if shifted:
                            break
                    if shifted:
                        break
                if shifted:
                    break
        if not any_shifted:
            break


def _get_pin_offset_and_face(
    macro: str,
    pin: str,
    cell: dict[str, Any] | None = None,
) -> tuple[tuple[float, float], str]:
    """Return ((offset_x, offset_y), face) for a component pin in tiles."""
    if macro == "belt_merge2":
        merge_pins = {
            "a": ((0.25, 0.5), "W"),
            "b": ((0.25, 1.5), "W"),
            "y": ((0.75, 0.5), "E"),
        }
        return merge_pins.get(pin, ((0.5, 0.5), "E"))
    if macro == "belt_split2":
        split_pins = {
            "a": ((0.25, 0.5), "W"),
            "b": ((0.25, 1.5), "W"),
            "y1": ((0.75, 0.5), "E"),
            "y2": ((0.75, 1.5), "E"),
        }
        return split_pins.get(pin, ((0.5, 0.5), "E"))
    if macro == "belt_balancer2":
        balancer_pins = {
            "a1": ((0.25, 0.5), "W"),
            "a2": ((0.25, 1.5), "W"),
            "y1": ((0.75, 0.5), "E"),
            "y2": ((0.75, 1.5), "E"),
        }
        return balancer_pins.get(pin, ((0.5, 0.5), "E"))
    if macro == "pump":
        pump_pins = {
            "a": ((0.5, 0.25), "S"),
            "y": ((0.5, 1.75), "N"),
        }
        return pump_pins.get(pin, ((0.5, 1.0), "N"))

    if cell:
        try:
            from foundry.lef import get_macro_port_geometry
        except ImportError:
            from lef import get_macro_port_geometry

        outputs = [o.replace("-", "_") for o in cell.get("outputs", [])]
        inputs = [i.replace("-", "_") for i in cell.get("inputs", [])]
        if pin in outputs:
            orig_outputs = cell.get("outputs", [])
            idx = outputs.index(pin)
            orig_name = orig_outputs[idx] if idx < len(orig_outputs) else ""
            if orig_name:
                geom = get_macro_port_geometry(cell, orig_name, is_output=True)
                r = geom["rect"]
                return ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0), geom.get("face", "E")
        if pin in inputs:
            orig_inputs = cell.get("inputs", [])
            idx = inputs.index(pin)
            orig_name = orig_inputs[idx] if idx < len(orig_inputs) else ""
            if orig_name:
                geom = get_macro_port_geometry(cell, orig_name, is_output=False)
                r = geom["rect"]
                return ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0), geom.get("face", "W")
        w = float(cell.get("width", 3))
        h = float(cell.get("height", 3))
        return (w / 2.0, h / 2.0), "E"

    return (0.5, 0.5), "E"


def route_unrouted_signal_nets(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any] | None = None,
) -> int:
    """Synthesize orthogonal routes for unrouted point-to-point signal nets.

    When OpenROAD detailed routing leaves point-to-point signal nets unrouted
    (empty wires list), this pass finds an open channel track between the
    source pin and sink pin, and creates an orthogonal Z-shaped route on
    M1 and M2 layers.

    Returns the number of nets routed.
    """
    if netlist_data is None:
        netlist_data = def_data.get("_netlist_data")

    units = int(def_data.get("units", 1000))
    spatial = SpatialWireIndex(def_data, units)

    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    cell_map = netlist_data.get("cells", {}) if netlist_data else {}
    inst_map = (
        {inst["name"]: inst for inst in netlist_data.get("instances", [])} if netlist_data else {}
    )
    pin_dict = {p["name"]: p for p in def_data.get("pins", [])}

    comp_boxes = [
        (c.get("name", ""), *get_component_box(c, units, netlist_data))
        for c in def_data.get("components", [])
    ]

    def _is_driver(inst: str, pin: str) -> bool:
        if inst in comp_map:
            c = comp_map[inst]
            cell_id = inst_map.get(inst, {}).get("cell", c.get("macro", ""))
            macro = c.get("macro", "")
            if macro in ("belt_merge2", "pump") and pin == "y":
                return True
            if macro in ("belt_split2", "belt_balancer2") and pin in ("y1", "y2"):
                return True
            cell = cell_map.get(cell_id)
            if cell:
                outputs = [o.replace("-", "_") for o in cell.get("outputs", [])]
                if pin in outputs:
                    return True
        elif inst == "PIN" or inst in pin_dict:
            p = pin_dict.get(pin) if inst == "PIN" else pin_dict.get(inst)
            if p and p.get("direction") == "INPUT":
                return True
        return False

    def _get_pin_loc(inst: str, pin: str) -> tuple[tuple[int, int], str] | None:
        if inst in comp_map:
            c = comp_map[inst]
            cell_id = inst_map.get(inst, {}).get("cell", c.get("macro", ""))
            cell = cell_map.get(cell_id)
            (off_x, off_y), face = _get_pin_offset_and_face(c.get("macro", ""), pin, cell)
            px = c["x"] + round(off_x * units)
            py = c["y"] + round(off_y * units)
            return (px, py), face
        if inst == "PIN" or inst in pin_dict:
            p = pin_dict.get(pin) if inst == "PIN" else pin_dict.get(inst)
            if p:
                face = "E" if p.get("direction") == "INPUT" else "W"
                return (p["x"], p["y"]), face
        return None

    routed_count = 0

    for net in def_data.get("nets", []):
        if net.get("wires"):
            continue
        conns = net.get("connections", [])
        if len(conns) != 2:
            continue

        c1, c2 = conns[0], conns[1]
        if _is_driver(c1[0], c1[1]):
            driver, sink = c1, c2
        elif _is_driver(c2[0], c2[1]):
            driver, sink = c2, c1
        else:
            driver, sink = c1, c2

        src_info = _get_pin_loc(driver[0], driver[1])
        dst_info = _get_pin_loc(sink[0], sink[1])
        if not src_info or not dst_info:
            continue

        (x1, y1), face1 = src_info
        (x2, y2), face2 = dst_info
        inst_src, inst_dst = driver[0], sink[0]

        if y1 == y2:
            h_min, h_max = min(x1, x2), max(x1, x2)
            h_blocked = False
            for ow_x1, ow_x2, _oname, _ in spatial.m1_by_y.get(y1, []):
                if max(ow_x1, h_min) < min(ow_x2, h_max):
                    h_blocked = True
                    break
            if not h_blocked:
                for c_name, cx, cy, cw, ch in comp_boxes:
                    if (
                        c_name not in (inst_src, inst_dst)
                        and cx < h_max
                        and cx + cw > h_min
                        and cy < y1 < cy + ch
                    ):
                        h_blocked = True
                        break
            if not h_blocked:
                w = {"layer": "M1", "x1": h_min, "y1": y1, "x2": h_max, "y2": y1}
                net["wires"] = [w]
                spatial.m1_by_y[y1].append((h_min, h_max, net.get("name", ""), w))
                routed_count += 1
                continue

        # Z-route with intermediate vertical segment on M2
        min_x = min(x1, x2)
        max_x = max(x1, x2)
        grid_rem = units // 2

        # Tracks between endpoints
        inside_tracks = [
            x
            for x in range((min_x // units) * units + grid_rem, max_x + units, units)
            if min_x <= x <= max_x
        ]
        inside_tracks.sort(key=lambda x: abs(x - (x1 + x2) / 2))
        inside_tracks = inside_tracks[:30]

        # Tracks outside endpoints as fallbacks
        outside_tracks = []
        for i in range(1, 30):
            outside_tracks.append(((max_x // units) + i) * units + grid_rem)
            outside_tracks.append(((min_x // units) - i) * units + grid_rem)

        all_candidates = inside_tracks + outside_tracks

        for x_mid in all_candidates:
            if face1 == "E" and x_mid < x1:
                continue
            if face1 == "W" and x_mid > x1:
                continue
            if face2 == "W" and x_mid > x2:
                continue
            if face2 == "E" and x_mid < x2:
                continue

            y_min, y_max = min(y1, y2), max(y1, y2)

            # Check vertical M2 segment
            v_blocked = False
            for _c_name, cx, cy, cw, ch in comp_boxes:
                if cx < x_mid < cx + cw and not (cy + ch <= y_min or cy >= y_max):
                    v_blocked = True
                    break
            if v_blocked:
                continue

            for ow_y1, ow_y2, _oname, _ in spatial.m2_by_x.get(x_mid, []):
                if max(ow_y1, y_min) < min(ow_y2, y_max):
                    v_blocked = True
                    break
            if v_blocked:
                continue

            # Check source M1 segment
            h1_min, h1_max = min(x1, x_mid), max(x1, x_mid)
            h1_blocked = False
            for ow_x1, ow_x2, _oname, _ in spatial.m1_by_y.get(y1, []):
                if max(ow_x1, h1_min) < min(ow_x2, h1_max):
                    h1_blocked = True
                    break
            if h1_blocked:
                continue

            for c_name, cx, cy, cw, ch in comp_boxes:
                if (
                    c_name not in (inst_src, inst_dst)
                    and cx < h1_max
                    and cx + cw > h1_min
                    and cy < y1 < cy + ch
                ):
                    h1_blocked = True
                    break
            if h1_blocked:
                continue

            # Check sink M1 segment
            h2_min, h2_max = min(x2, x_mid), max(x2, x_mid)
            h2_blocked = False
            for ow_x1, ow_x2, _oname, _ in spatial.m1_by_y.get(y2, []):
                if max(ow_x1, h2_min) < min(ow_x2, h2_max):
                    h2_blocked = True
                    break
            if h2_blocked:
                continue

            for c_name, cx, cy, cw, ch in comp_boxes:
                if (
                    c_name not in (inst_src, inst_dst)
                    and cx < h2_max
                    and cx + cw > h2_min
                    and cy < y2 < cy + ch
                ):
                    h2_blocked = True
                    break
            if h2_blocked:
                continue

            # Candidate is valid and clear
            w_src = {"layer": "M1", "x1": h1_min, "y1": y1, "x2": h1_max, "y2": y1}
            w_mid = {"layer": "M2", "x1": x_mid, "y1": y_min, "x2": x_mid, "y2": y_max}
            w_dst = {"layer": "M1", "x1": h2_min, "y1": y2, "x2": h2_max, "y2": y2}
            wires = [
                w for w in (w_src, w_mid, w_dst) if not (w["x1"] == w["x2"] and w["y1"] == w["y2"])
            ]
            net["wires"] = wires
            net["vias"] = [
                {"layer": "M1", "x": x_mid, "y": y1, "via": "SURFACE_TURN_VIA"},
                {"layer": "M1", "x": x_mid, "y": y2, "via": "SURFACE_TURN_VIA"},
            ]

            spatial.m1_by_y[y1].append((h1_min, h1_max, net.get("name", ""), w_src))
            spatial.m2_by_x[x_mid].append((y_min, y_max, net.get("name", ""), w_mid))
            spatial.m1_by_y[y2].append((h2_min, h2_max, net.get("name", ""), w_dst))
            routed_count += 1
            break

    return routed_count
