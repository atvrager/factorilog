"""Route straightening and jog elimination for DEF layouts."""

from __future__ import annotations

import collections
from typing import Any

try:
    from foundry.untangle.spatial import (
        SpatialWireIndex,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )
except ImportError:
    from untangle.spatial import (
        SpatialWireIndex,
        _is_fluid_net,
        get_component_box,
        wires_touch,
    )


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
                continue


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


def eliminate_staircase_jogs(def_data: dict[str, Any]) -> None:
    """Eliminate unblocked intermediate staircase jogs on routing tracks.

    When an orthogonal route contains a staircase jog:
      M1(y1, xa->x1) -> M2(x1, y1->y2) -> M1(y2, x1->x2) -> M2(x2, y2->y3)
    where (x1, y1) and (x1, y2) are pure degree-2 junctions without pins,
    if the direct corridor along y1 from x1 to x2 is clear, and the vertical track
    at x2 from y1 to y2 is clear:
      extend M1(y1) from x1 to x2,
      extend M2(x2) to y1,
      remove the intermediate M2 jog and M1 segment.
    Similarly for vertical staircases (M2 -> M1 -> M2 -> M1).
    """
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])
    wire_index = SpatialWireIndex(def_data, units)
    comp_boxes = wire_index.comp_boxes

    def _is_h_clear(y: int, x1: int, x2: int, net_to_ignore: dict[str, Any]) -> bool:
        return wire_index.is_h_clear_detour(y, x1, x2, net_to_ignore)

    def _is_v_clear(x: int, y1: int, y2: int, net_to_ignore: dict[str, Any]) -> bool:
        return wire_index.is_v_clear_detour(x, y1, y2, net_to_ignore)

    try:
        from foundry.def_parser import merge_collinear_wires
    except ImportError:
        from def_parser import merge_collinear_wires

    for _pass in range(5):
        any_modified = False
        for net in nets:
            wires = net.get("wires", [])
            if len(wires) < 4:
                continue

            is_fluid = _is_fluid_net(net)
            deg: dict[tuple[int, int], int] = collections.defaultdict(int)
            for w in wires:
                deg[(w["x1"], w["y1"])] += 1
                deg[(w["x2"], w["y2"])] += 1

            modified = False

            # 1. Horizontal staircase jog collapsing (M1 -> M2 -> M1 -> M2)
            for i, w1 in enumerate(wires):
                if w1.get("layer") != "M1" or w1["y1"] != w1["y2"]:
                    continue
                y1 = w1["y1"]
                for j, wj in enumerate(wires):
                    if i == j or wj.get("layer") != "M2" or wj["x1"] != wj["x2"]:
                        continue
                    x1 = wj["x1"]
                    if not (
                        (w1["x1"] == x1 or w1["x2"] == x1) and (wj["y1"] == y1 or wj["y2"] == y1)
                    ):
                        continue
                    if deg[(x1, y1)] != 2:
                        continue
                    y2 = wj["y2"] if wj["y1"] == y1 else wj["y1"]
                    if deg[(x1, y2)] != 2:
                        continue

                    xa = w1["x1"] if w1["x2"] == x1 else w1["x2"]

                    for k, w2 in enumerate(wires):
                        if (
                            k in (i, j)
                            or w2.get("layer") != "M1"
                            or w2["y1"] != w2["y2"]
                            or w2["y1"] != y2
                        ):
                            continue
                        if not (w2["x1"] == x1 or w2["x2"] == x1):
                            continue
                        x2 = w2["x1"] if w2["x2"] == x1 else w2["x2"]

                        if not ((xa < x1 < x2) or (xa > x1 > x2)):
                            continue

                        if any(
                            cx <= x2 <= cx + cw and cy <= y2 <= cy + ch
                            for cx, cy, cw, ch in comp_boxes
                        ):
                            continue

                        for m, w3 in enumerate(wires):
                            if (
                                m in (i, j, k)
                                or w3.get("layer") != "M2"
                                or w3["x1"] != w3["x2"]
                                or w3["x1"] != x2
                            ):
                                continue
                            min_w3 = min(w3["y1"], w3["y2"])
                            max_w3 = max(w3["y1"], w3["y2"])
                            w3_covers_both = (
                                is_fluid and min_w3 <= min(y1, y2) and max_w3 >= max(y1, y2)
                            )

                            if not (w3["y1"] == y2 or w3["y2"] == y2 or w3_covers_both):
                                continue
                            if not w3_covers_both:
                                y3 = w3["y1"] if w3["y2"] == y2 else w3["y2"]
                                if (y1 < y2 and y3 <= y2) or (y1 > y2 and y3 >= y2):
                                    continue

                            if any(w not in (wj, w1, w2) and wires_touch(w, wj) for w in wires):
                                continue
                            if any(w not in (wj, w2, w3) and wires_touch(w, w2) for w in wires):
                                continue

                            if _is_h_clear(y1, x1, x2, net) and (
                                w3_covers_both or _is_v_clear(x2, y1, y2, net)
                            ):
                                if w1["x1"] == x1:
                                    w1["x1"] = x2
                                else:
                                    w1["x2"] = x2
                                if not w3_covers_both:
                                    if w3["y1"] == y2:
                                        w3["y1"] = y1
                                    else:
                                        w3["y2"] = y1
                                wires.remove(wj)
                                wires.remove(w2)
                                net["vias"] = [
                                    v
                                    for v in net.get("vias", [])
                                    if not (
                                        (v.get("x") == x1 and v.get("y") in (y1, y2))
                                        or (v.get("x") == x2 and v.get("y") == y2)
                                    )
                                ]
                                net["vias"].append(
                                    {"layer": "M1", "x": x2, "y": y1, "via": "SURFACE_TURN_VIA"}
                                )
                                net["wires"] = merge_collinear_wires(wires, net.get("vias", []))
                                modified = True
                                any_modified = True
                                break
                        if modified:
                            break

                        # Option B: extend w0 at xa to y2, extend w2 at y2 to xa, remove wj and w1
                        for m, w0 in enumerate(wires):
                            if (
                                m in (i, j, k)
                                or w0.get("layer") != "M2"
                                or w0["x1"] != w0["x2"]
                                or w0["x1"] != xa
                            ):
                                continue
                            min_w0 = min(w0["y1"], w0["y2"])
                            max_w0 = max(w0["y1"], w0["y2"])
                            w0_covers_both = (
                                is_fluid and min_w0 <= min(y1, y2) and max_w0 >= max(y1, y2)
                            )

                            if not (w0["y1"] == y1 or w0["y2"] == y1 or w0_covers_both):
                                continue
                            if not w0_covers_both:
                                y0 = w0["y1"] if w0["y2"] == y1 else w0["y2"]
                                if (y1 < y2 and y0 >= y1) or (y1 > y2 and y0 <= y1):
                                    continue
                            if any(
                                cx <= xa <= cx + cw and cy <= y1 <= cy + ch
                                for cx, cy, cw, ch in comp_boxes
                            ):
                                continue

                            if any(w not in (wj, w1, w2) and wires_touch(w, wj) for w in wires):
                                continue
                            if any(w not in (wj, w1, w0) and wires_touch(w, w1) for w in wires):
                                continue

                            if (w0_covers_both or _is_v_clear(xa, y1, y2, net)) and _is_h_clear(
                                y2, xa, x1, net
                            ):
                                if not w0_covers_both:
                                    if w0["y1"] == y1:
                                        w0["y1"] = y2
                                    else:
                                        w0["y2"] = y2
                                if w2["x1"] == x1:
                                    w2["x1"] = xa
                                else:
                                    w2["x2"] = xa
                                wires.remove(wj)
                                wires.remove(w1)
                                net["vias"] = [
                                    v
                                    for v in net.get("vias", [])
                                    if not (
                                        (v.get("x") == x1 and v.get("y") in (y1, y2))
                                        or (v.get("x") == xa and v.get("y") == y1)
                                    )
                                ]
                                net["vias"].append(
                                    {"layer": "M2", "x": xa, "y": y2, "via": "SURFACE_TURN_VIA"}
                                )
                                net["wires"] = merge_collinear_wires(wires, net.get("vias", []))
                                modified = True
                                any_modified = True
                                break
                        if modified:
                            break
                    if modified:
                        break
                if modified:
                    break

            if modified:
                continue

            # 2. Vertical staircase jog collapsing (M2 -> M1 -> M2 -> M1)
            for i, w1 in enumerate(wires):
                if w1.get("layer") != "M2" or w1["x1"] != w1["x2"]:
                    continue
                x1 = w1["x1"]
                for j, wj in enumerate(wires):
                    if i == j or wj.get("layer") != "M1" or wj["y1"] != wj["y2"]:
                        continue
                    y1 = wj["y1"]
                    if not (
                        (w1["y1"] == y1 or w1["y2"] == y1) and (wj["x1"] == x1 or wj["x2"] == x1)
                    ):
                        continue
                    if deg[(x1, y1)] != 2:
                        continue
                    x2 = wj["x2"] if wj["x1"] == x1 else wj["x1"]
                    if deg[(x2, y1)] != 2:
                        continue

                    ya = w1["y1"] if w1["y2"] == y1 else w1["y2"]

                    for k, w2 in enumerate(wires):
                        if (
                            k in (i, j)
                            or w2.get("layer") != "M2"
                            or w2["x1"] != w2["x2"]
                            or w2["x1"] != x2
                        ):
                            continue
                        if not (w2["y1"] == y1 or w2["y2"] == y1):
                            continue
                        y2 = w2["y1"] if w2["y2"] == y1 else w2["y2"]

                        if not ((ya < y1 < y2) or (ya > y1 > y2)):
                            continue

                        if any(
                            cx <= x2 <= cx + cw and cy <= y2 <= cy + ch
                            for cx, cy, cw, ch in comp_boxes
                        ):
                            continue

                        for m, w3 in enumerate(wires):
                            if (
                                m in (i, j, k)
                                or w3.get("layer") != "M1"
                                or w3["y1"] != w3["y2"]
                                or w3["y1"] != y2
                            ):
                                continue
                            min_w3 = min(w3["x1"], w3["x2"])
                            max_w3 = max(w3["x1"], w3["x2"])
                            w3_covers_both = (
                                is_fluid and min_w3 <= min(x1, x2) and max_w3 >= max(x1, x2)
                            )

                            if not (w3["x1"] == x2 or w3["x2"] == x2 or w3_covers_both):
                                continue
                            if not w3_covers_both:
                                x3 = w3["x1"] if w3["x2"] == x2 else w3["x2"]
                                if (x1 < x2 and x3 <= x2) or (x1 > x2 and x3 >= x2):
                                    continue

                            if any(w not in (wj, w1, w2) and wires_touch(w, wj) for w in wires):
                                continue
                            if any(w not in (wj, w2, w3) and wires_touch(w, w2) for w in wires):
                                continue

                            if _is_v_clear(x1, y1, y2, net) and (
                                w3_covers_both or _is_h_clear(y2, x1, x2, net)
                            ):
                                if w1["y1"] == y1:
                                    w1["y1"] = y2
                                else:
                                    w1["y2"] = y2
                                if not w3_covers_both:
                                    if w3["x1"] == x2:
                                        w3["x1"] = x1
                                    else:
                                        w3["x2"] = x1
                                wires.remove(wj)
                                wires.remove(w2)
                                net["vias"] = [
                                    v
                                    for v in net.get("vias", [])
                                    if not (
                                        (v.get("y") == y1 and v.get("x") in (x1, x2))
                                        or (v.get("y") == y2 and v.get("x") == x2)
                                    )
                                ]
                                net["vias"].append(
                                    {"layer": "M2", "x": x1, "y": y2, "via": "SURFACE_TURN_VIA"}
                                )
                                net["wires"] = merge_collinear_wires(wires, net.get("vias", []))
                                modified = True
                                any_modified = True
                                break
                        if modified:
                            break

                        # Option B: extend w0 at ya to x2, extend w2 at x2 to ya, remove wj and w1
                        for m, w0 in enumerate(wires):
                            if (
                                m in (i, j, k)
                                or w0.get("layer") != "M1"
                                or w0["y1"] != w0["y2"]
                                or w0["y1"] != ya
                            ):
                                continue
                            min_w0 = min(w0["x1"], w0["x2"])
                            max_w0 = max(w0["x1"], w0["x2"])
                            w0_covers_both = (
                                is_fluid and min_w0 <= min(x1, x2) and max_w0 >= max(x1, x2)
                            )

                            if not (w0["x1"] == x1 or w0["x2"] == x1 or w0_covers_both):
                                continue
                            if not w0_covers_both:
                                x0 = w0["x1"] if w0["x2"] == x1 else w0["x2"]
                                if (x1 < x2 and x0 >= x1) or (x1 > x2 and x0 <= x1):
                                    continue
                            if any(
                                cx <= x1 <= cx + cw and cy <= ya <= cy + ch
                                for cx, cy, cw, ch in comp_boxes
                            ):
                                continue

                            if any(w not in (wj, w1, w2) and wires_touch(w, wj) for w in wires):
                                continue
                            if any(w not in (wj, w1, w0) and wires_touch(w, w1) for w in wires):
                                continue

                            if (w0_covers_both or _is_h_clear(ya, x1, x2, net)) and _is_v_clear(
                                x2, ya, y1, net
                            ):
                                if not w0_covers_both:
                                    if w0["x1"] == x1:
                                        w0["x1"] = x2
                                    else:
                                        w0["x2"] = x2
                                if w2["y1"] == y1:
                                    w2["y1"] = ya
                                else:
                                    w2["y2"] = ya
                                wires.remove(wj)
                                wires.remove(w1)
                                net["vias"] = [
                                    v
                                    for v in net.get("vias", [])
                                    if not (
                                        (v.get("y") == y1 and v.get("x") in (x1, x2))
                                        or (v.get("y") == ya and v.get("x") == x1)
                                    )
                                ]
                                net["vias"].append(
                                    {"layer": "M1", "x": x2, "y": ya, "via": "SURFACE_TURN_VIA"}
                                )
                                net["wires"] = merge_collinear_wires(wires, net.get("vias", []))
                                modified = True
                                any_modified = True
                                break
                        if modified:
                            break
                    if modified:
                        break
                if modified:
                    break

        if not any_modified:
            break
        wire_index.rebuild()


def eliminate_u_detours(def_data: dict[str, Any]) -> None:
    """Eliminate unblocked U-detour loops across routing channels.

    When an orthogonal route contains a U-detour:
      M1(y1, xa->x1) -> M2(x1, y1->y2) -> M1(y2, x1->x2) -> M2(x2, y2->y3)
    where the two vertical segments go in opposite directions:
      (y1 < y2 and y3 < y2) or (y1 > y2 and y3 > y2)
    and (x1, y1) and (x1, y2) are degree-2 junctions without pins:
    if the direct corridor along y1 from x1 to x2 is clear:
      extend M1(y1) from x1 to x2,
      update M2(x2) to connect at (x2, y1),
      remove the intermediate M2 jog (x1) and M1 segment (y2).
    Similarly for horizontal U-detours (M2 -> M1 -> M2 -> M1).
    """
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])
    wire_index = SpatialWireIndex(def_data, units)
    comp_boxes = wire_index.comp_boxes

    def _is_h_clear(y: int, x1: int, x2: int, net_to_ignore: dict[str, Any]) -> bool:
        return wire_index.is_h_clear_detour(y, x1, x2, net_to_ignore)

    def _is_v_clear(x: int, y1: int, y2: int, net_to_ignore: dict[str, Any]) -> bool:
        return wire_index.is_v_clear_detour(x, y1, y2, net_to_ignore)

    try:
        from foundry.def_parser import merge_collinear_wires
    except ImportError:
        from def_parser import merge_collinear_wires

    for _pass in range(5):
        any_modified = False
        for net in nets:
            wires = net.get("wires", [])
            if len(wires) < 4:
                continue

            deg: dict[tuple[int, int], int] = collections.defaultdict(int)
            for w in wires:
                deg[(w["x1"], w["y1"])] += 1
                deg[(w["x2"], w["y2"])] += 1

            modified = False

            # 1. Vertical U-detour (M1 -> M2 -> M1 -> M2) where M2 jogs reverse direction
            for i, w1 in enumerate(wires):
                if w1.get("layer") != "M1" or w1["y1"] != w1["y2"]:
                    continue
                y1 = w1["y1"]
                for j, wj in enumerate(wires):
                    if i == j or wj.get("layer") != "M2" or wj["x1"] != wj["x2"]:
                        continue
                    x1 = wj["x1"]
                    if not (
                        (w1["x1"] == x1 or w1["x2"] == x1) and (wj["y1"] == y1 or wj["y2"] == y1)
                    ):
                        continue
                    if deg[(x1, y1)] != 2:
                        continue
                    y2 = wj["y2"] if wj["y1"] == y1 else wj["y1"]
                    if deg[(x1, y2)] != 2:
                        continue
                    if any(
                        cx <= x1 <= cx + cw and cy <= y2 <= cy + ch for cx, cy, cw, ch in comp_boxes
                    ):
                        continue

                    for k, w2 in enumerate(wires):
                        if (
                            k in (i, j)
                            or w2.get("layer") != "M1"
                            or w2["y1"] != w2["y2"]
                            or w2["y1"] != y2
                        ):
                            continue
                        if not (w2["x1"] == x1 or w2["x2"] == x1):
                            continue
                        x2 = w2["x1"] if w2["x2"] == x1 else w2["x2"]

                        for m, w3 in enumerate(wires):
                            if (
                                m in (i, j, k)
                                or w3.get("layer") != "M2"
                                or w3["x1"] != w3["x2"]
                                or w3["x1"] != x2
                            ):
                                continue
                            if not (w3["y1"] == y2 or w3["y2"] == y2):
                                continue
                            y3 = w3["y1"] if w3["y2"] == y2 else w3["y2"]

                            # Opposite direction check (U-detour):
                            if not ((y1 < y2 and y3 < y2) or (y1 > y2 and y3 > y2)):
                                continue

                            if any(w not in (wj, w1, w2) and wires_touch(w, wj) for w in wires):
                                continue
                            if any(w not in (wj, w2, w3) and wires_touch(w, w2) for w in wires):
                                continue

                            # Check if y1 is clear between x1 and x2, and x2 is clear between y1 and y2
                            if not _is_h_clear(y1, x1, x2, net) or not _is_v_clear(x2, y1, y2, net):
                                continue

                            # Extend w1 to x2 at y1
                            if w1["x1"] == x1:
                                w1["x1"] = x2
                            else:
                                w1["x2"] = x2

                            # Reconnect w3 to y1
                            if w3["y1"] == y2:
                                w3["y1"] = y1
                            else:
                                w3["y2"] = y1

                            wires.remove(wj)
                            wires.remove(w2)
                            net["vias"] = [
                                v
                                for v in net.get("vias", [])
                                if not (
                                    (v.get("x") == x1 and v.get("y") in (y1, y2))
                                    or (v.get("x") == x2 and v.get("y") == y2)
                                )
                            ]
                            if w3["y1"] == w3["y2"]:
                                wires.remove(w3)
                            else:
                                net["vias"].append(
                                    {"layer": "M1", "x": x2, "y": y1, "via": "SURFACE_TURN_VIA"}
                                )
                            net["wires"] = merge_collinear_wires(wires, net.get("vias", []))
                            modified = True
                            any_modified = True
                            break
                        if modified:
                            break
                    if modified:
                        break
                if modified:
                    break

            if modified:
                continue

            # 2. Horizontal U-detour (M2 -> M1 -> M2 -> M1) where M1 jogs reverse direction
            for i, w1 in enumerate(wires):
                if w1.get("layer") != "M2" or w1["x1"] != w1["x2"]:
                    continue
                x1 = w1["x1"]
                for j, wj in enumerate(wires):
                    if i == j or wj.get("layer") != "M1" or wj["y1"] != wj["y2"]:
                        continue
                    y1 = wj["y1"]
                    if not (
                        (w1["y1"] == y1 or w1["y2"] == y1) and (wj["x1"] == x1 or wj["x2"] == x1)
                    ):
                        continue
                    if deg[(x1, y1)] != 2:
                        continue
                    x2 = wj["x2"] if wj["x1"] == x1 else wj["x1"]
                    if deg[(x2, y1)] != 2:
                        continue
                    if any(
                        cx <= x2 <= cx + cw and cy <= y1 <= cy + ch for cx, cy, cw, ch in comp_boxes
                    ):
                        continue

                    for k, w2 in enumerate(wires):
                        if (
                            k in (i, j)
                            or w2.get("layer") != "M2"
                            or w2["x1"] != w2["x2"]
                            or w2["x1"] != x2
                        ):
                            continue
                        if not (w2["y1"] == y1 or w2["y2"] == y1):
                            continue
                        y2 = w2["y1"] if w2["y2"] == y1 else w2["y2"]

                        for m, w3 in enumerate(wires):
                            if (
                                m in (i, j, k)
                                or w3.get("layer") != "M1"
                                or w3["y1"] != w3["y2"]
                                or w3["y1"] != y2
                            ):
                                continue
                            if not (w3["x1"] == x2 or w3["x2"] == x2):
                                continue
                            x3 = w3["x1"] if w3["x2"] == x2 else w3["x2"]

                            # Opposite direction check (U-detour):
                            if not ((x1 < x2 and x3 < x2) or (x1 > x2 and x3 > x2)):
                                continue

                            if any(w not in (wj, w1, w2) and wires_touch(w, wj) for w in wires):
                                continue
                            if any(w not in (wj, w2, w3) and wires_touch(w, w2) for w in wires):
                                continue

                            # Check if x1 is clear between y1 and y2, and y2 is clear between x1 and x2
                            if not _is_v_clear(x1, y1, y2, net) or not _is_h_clear(y2, x1, x2, net):
                                continue

                            # Extend w1 to y2 at x1
                            if w1["y1"] == y1:
                                w1["y1"] = y2
                            else:
                                w1["y2"] = y2

                            # Reconnect w3 to x1
                            if w3["x1"] == x2:
                                w3["x1"] = x1
                            else:
                                w3["x2"] = x1

                            wires.remove(wj)
                            wires.remove(w2)
                            net["vias"] = [
                                v
                                for v in net.get("vias", [])
                                if not (
                                    (v.get("y") == y1 and v.get("x") in (x1, x2))
                                    or (v.get("y") == y2 and v.get("x") == x2)
                                )
                            ]
                            if w3["x1"] == w3["x2"]:
                                wires.remove(w3)
                            else:
                                net["vias"].append(
                                    {"layer": "M2", "x": x1, "y": y2, "via": "SURFACE_TURN_VIA"}
                                )
                            net["wires"] = merge_collinear_wires(wires, net.get("vias", []))
                            modified = True
                            any_modified = True
                            break
                        if modified:
                            break
                    if modified:
                        break
                if modified:
                    break

        if not any_modified:
            break
        wire_index.rebuild()


def shift_congested_pin_turns(def_data: dict[str, Any]) -> None:
    """Shift perpendicular turns that bottleneck immediately at component pin exits.

    When a net connects to a component pin and turns perpendicularly within 1 tile
    of the component boundary, and crosses another net within 1 tile of the turn,
    extending the straight pin runway into the corridor before turning creates
    adequate space for underground jumper synthesis.
    """
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])
    components = def_data.get("components", [])
    netlist = def_data.get("_netlist_data")
    wire_index = SpatialWireIndex(def_data, units)

    def _is_h_clear(y: int, x1: int, x2: int, net_to_ignore: dict[str, Any]) -> bool:
        return wire_index.is_h_clear_detour(y, x1, x2, net_to_ignore)

    def _is_v_clear(x: int, y1: int, y2: int, net_to_ignore: dict[str, Any]) -> bool:
        return wire_index.is_v_clear_detour(x, y1, y2, net_to_ignore)

    for c in components:
        orient = c.get("orient", "N")
        if orient not in ("N", ""):
            continue
        gx = c.get("x", 0)
        cname = c.get("name", "")
        _cx, _cy, cw, _ch = get_component_box(c, units, netlist)
        exit_x = gx + cw

        entry_x = gx

        for n in nets:
            if not any(inst == cname for inst, _p in n.get("connections", [])):
                continue
            for w_pin in list(n.get("wires", [])):
                if w_pin.get("layer") != "M1" or w_pin["y1"] != w_pin["y2"]:
                    continue
                py = w_pin["y1"]
                # Check for congested turn near component pin (East exit or West entrance)
                is_east = (gx <= min(w_pin["x1"], w_pin["x2"]) <= exit_x) and (
                    exit_x <= max(w_pin["x1"], w_pin["x2"]) <= exit_x + units
                )
                is_west = (gx <= max(w_pin["x1"], w_pin["x2"]) <= exit_x) and (
                    entry_x - units <= min(w_pin["x1"], w_pin["x2"]) <= entry_x
                )
                if not (is_east or is_west):
                    continue

                if is_east:
                    turn_x = max(w_pin["x1"], w_pin["x2"])
                    steps = (1, 2)
                    dx_sign = 1
                else:
                    turn_x = min(w_pin["x1"], w_pin["x2"])
                    steps = (2, 1)
                    dx_sign = -1

                w_turn = next(
                    (
                        w
                        for w in n.get("wires", [])
                        if w.get("layer") == "M2"
                        and w["x1"] == turn_x
                        and w["x2"] == turn_x
                        and (w["y1"] == py or w["y2"] == py)
                    ),
                    None,
                )
                if not w_turn:
                    continue

                min_y = min(w_turn["y1"], w_turn["y2"])
                max_y = max(w_turn["y1"], w_turn["y2"])
                y_other = w_turn["y1"] if w_turn["y2"] == py else w_turn["y2"]

                # Find connecting M1 wire at y_other
                w_other = next(
                    (
                        w
                        for w in n.get("wires", [])
                        if w.get("layer") == "M1"
                        and w["y1"] == y_other
                        and w["y2"] == y_other
                        and (w["x1"] == turn_x or w["x2"] == turn_x)
                    ),
                    None,
                )
                if not w_other:
                    continue

                # Check for a crossing within 1 tile of the turn:
                # 1. On w_turn within 1 tile of py
                # 2. On w_other within 1 tile of turn_x
                has_bottleneck = False
                for on in nets:
                    if on is n:
                        continue
                    for ow in on.get("wires", []):
                        if (
                            ow.get("layer") == "M1"
                            and ow["y1"] == ow["y2"]
                            and min(ow["x1"], ow["x2"]) <= turn_x <= max(ow["x1"], ow["x2"])
                            and abs(ow["y1"] - py) <= units
                            and ow["y1"] != py
                        ):
                            has_bottleneck = True
                            break
                        if (
                            ow.get("layer") == "M2"
                            and ow["x1"] == ow["x2"]
                            and min(ow["y1"], ow["y2"]) <= y_other <= max(ow["y1"], ow["y2"])
                            and abs(ow["x1"] - turn_x) <= units
                            and ow["x1"] != turn_x
                            and min(w_other["x1"], w_other["x2"])
                            <= ow["x1"]
                            <= max(w_other["x1"], w_other["x2"])
                        ):
                            has_bottleneck = True
                            break
                    if has_bottleneck:
                        break

                if not has_bottleneck:
                    continue

                # Try shifting by step tiles in dx_sign direction
                other_end_x = w_other["x2"] if w_other["x1"] == turn_x else w_other["x1"]
                for step in steps:
                    x_new = turn_x + dx_sign * step * units
                    if not _is_h_clear(py, turn_x, x_new, n):
                        continue
                    if not _is_v_clear(x_new, min_y, max_y, n):
                        continue
                    is_w_other_expanding = abs(x_new - other_end_x) > abs(turn_x - other_end_x)
                    if is_w_other_expanding and not _is_h_clear(y_other, turn_x, x_new, n):
                        continue

                    # Safety: w_turn cannot connect to any other wire in net besides w_pin and w_other
                    if any(
                        w not in (w_turn, w_pin, w_other) and wires_touch(w, w_turn)
                        for w in n.get("wires", [])
                    ):
                        continue
                    if not is_w_other_expanding:
                        shortened_span = {
                            "layer": "M1",
                            "y1": y_other,
                            "y2": y_other,
                            "x1": min(turn_x, x_new),
                            "x2": max(turn_x, x_new),
                        }
                        if any(
                            w not in (w_turn, w_other) and wires_touch(w, shortened_span)
                            for w in n.get("wires", [])
                        ):
                            continue

                    # Apply shift
                    if w_pin["x1"] == turn_x:
                        w_pin["x1"] = x_new
                    else:
                        w_pin["x2"] = x_new

                    w_turn["x1"] = x_new
                    w_turn["x2"] = x_new

                    if w_other["x1"] == turn_x:
                        w_other["x1"] = x_new
                    else:
                        w_other["x2"] = x_new

                    for v in n.get("vias", []):
                        if v.get("x") == turn_x and v.get("y") in (py, y_other):
                            v["x"] = x_new
                    break


def simplify_orthogonal_routes(def_data: dict[str, Any]) -> None:
    """Simplify stepped staircase jogs and U-detours across routing channels."""
    eliminate_staircase_jogs(def_data)
    eliminate_u_detours(def_data)
    shift_congested_pin_turns(def_data)


def shortcut_backward_splitter_detours(def_data: dict[str, Any]) -> None:
    """Shortcut backward loops that route around the rear of splitters."""
    units = int(def_data.get("units", 1000))
    nets = def_data.get("nets", [])
    components = {
        c["name"]: c for c in def_data.get("components", []) if c.get("macro") == "belt_split2"
    }

    for net in nets:
        src_comp_name = next(
            (
                inst
                for inst, p in net.get("connections", [])
                if p in ("y1", "y2") and inst in components
            ),
            None,
        )
        if not src_comp_name:
            continue
        c = components[src_comp_name]
        gx = c.get("x", 0)
        gy = c.get("y", 0)
        cw = units
        pin_exit_x = gx + cw

        wires = net.get("wires", [])
        w_rear = next(
            (w for w in wires if w.get("layer") == "M1" and min(w["x1"], w["x2"]) < gx),
            None,
        )
        if not w_rear:
            continue

        w_v_rear = next((w for w in wires if w.get("layer") == "M2" and w["x1"] < gx), None)
        if not w_v_rear:
            continue

        y_chan = w_v_rear["y1"] if w_v_rear["y2"] == w_rear["y1"] else w_v_rear["y2"]
        w_chan = next(
            (
                w
                for w in wires
                if w.get("layer") == "M1"
                and w["y1"] == y_chan
                and (w["x1"] == w_v_rear["x1"] or w["x2"] == w_v_rear["x1"])
            ),
            None,
        )
        if not w_chan:
            continue

        for step in (2, 3, 4):
            x_fwd = gx + cw + step * units - units // 2
            y_low = min(gy, y_chan)
            y_high = max(gy + 2 * units, y_chan)
            blocked = False
            for oc in def_data.get("components", []):
                ocx = oc.get("x", 0)
                ocy = oc.get("y", 0)
                if ocx <= x_fwd <= ocx + 5 * units and not (
                    ocy + 5 * units <= y_low or ocy >= y_high
                ):
                    blocked = True
                    break
            if blocked:
                continue

            wires.remove(w_rear)
            wires.remove(w_v_rear)

            w_pin = next(
                (
                    w
                    for w in wires
                    if w.get("layer") == "M1" and max(w["x1"], w["x2"]) <= pin_exit_x + 2 * units
                ),
                None,
            )
            if w_pin:
                if w_pin["x1"] < w_pin["x2"]:
                    w_pin["x2"] = x_fwd
                else:
                    w_pin["x1"] = x_fwd
                y_pin = w_pin["y1"]
            else:
                y_pin = gy + units // 2
                wires.append(
                    {"layer": "M1", "x1": pin_exit_x, "y1": y_pin, "x2": x_fwd, "y2": y_pin}
                )

            wires.append(
                {
                    "layer": "M2",
                    "x1": x_fwd,
                    "y1": min(y_pin, y_chan),
                    "x2": x_fwd,
                    "y2": max(y_pin, y_chan),
                }
            )

            if w_chan["x1"] == w_v_rear["x1"]:
                w_chan["x1"] = x_fwd
            else:
                w_chan["x2"] = x_fwd
            break
