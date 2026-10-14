"""Spatial indexing and geometric clearance utilities for DEF routing."""

from __future__ import annotations

import bisect
import collections
from typing import Any


def _is_fluid_net(net: dict[str, Any], fluids: set[str] | frozenset[str] | None = None) -> bool:
    """Return True if the net carries a fluid material."""
    if net.get("nondefaultrule") == "fluid_ndr":
        return True
    if fluids:
        mat = net.get("name", "").split("__")[0].replace("_", "-")
        return mat in fluids or mat.replace("-", "_") in fluids
    return False


def _get_net_fluid(net: dict[str, Any]) -> str:
    """Extract normalized fluid name from net name."""
    name = net.get("name", "")
    return name.split("__")[0].replace("_", "-")


def get_component_box(
    c: dict[str, Any],
    units: int,
    netlist_data: dict[str, Any] | None = None,
) -> tuple[int, int, int, int]:
    """Calculate the bounding box (x, y, w, h) of a placed component."""
    cx = c.get("x", 0)
    cy = c.get("y", 0)
    orient = c.get("orient", "N")
    macro = c.get("macro", "")
    cname = c.get("name", "")
    if netlist_data:
        cell_map = netlist_data.get("cells", {})
        inst_map = {inst["name"]: inst for inst in netlist_data.get("instances", [])}
        cell = None
        if cname in inst_map:
            cell = cell_map.get(inst_map[cname].get("cell", ""))
        if not cell and macro:
            macro_map = {
                cd.get("cell", k).replace(".", "__").replace("-", "_"): cd
                for k, cd in cell_map.items()
                if isinstance(cd, dict)
            }
            cell = macro_map.get(macro)
        if cell:
            w = int(cell.get("width", 3)) * units
            h = int(cell.get("height", 3)) * units
            if orient in ("W", "E"):
                return cx, cy, h, w
            return cx, cy, w, h
    if macro in ("belt_split2", "belt_balancer2"):
        cw, ch = (2 * units, units) if orient in ("W", "E") else (units, 2 * units)
    elif macro == "belt_merge2":
        cw, ch = 3 * units, 3 * units
    elif "refinery" in macro or "chemical_plant" in macro or "assembling_machine" in macro:
        cw, ch = 5 * units, 5 * units
    elif "furnace" in macro:
        cw, ch = 5 * units, 3 * units
    else:
        cw, ch = 3 * units, 3 * units
    return cx, cy, cw, ch


def wires_touch(w1: dict[str, Any], w2: dict[str, Any]) -> bool:
    """Return True if two wire segments connect or intersect."""
    min_x1, max_x1 = min(w1["x1"], w1["x2"]), max(w1["x1"], w1["x2"])
    min_y1, max_y1 = min(w1["y1"], w1["y2"]), max(w1["y1"], w1["y2"])
    min_x2, max_x2 = min(w2["x1"], w2["x2"]), max(w2["x1"], w2["x2"])
    min_y2, max_y2 = min(w2["y1"], w2["y2"]), max(w2["y1"], w2["y2"])

    if max_x1 < min_x2 or max_x2 < min_x1 or max_y1 < min_y2 or max_y2 < min_y1:
        return False

    is_h1 = w1["y1"] == w1["y2"]
    is_v1 = w1["x1"] == w1["x2"]
    is_h2 = w2["y1"] == w2["y2"]
    is_v2 = w2["x1"] == w2["x2"]

    if is_h1 and is_h2:
        return w1["y1"] == w2["y1"]
    if is_v1 and is_v2:
        return w1["x1"] == w2["x1"]
    if is_h1 and is_v2:
        return (min_x1 <= w2["x1"] <= max_x1) and (min_y2 <= w1["y1"] <= max_y2)
    if is_v1 and is_h2:
        return (min_x2 <= w1["x1"] <= max_x2) and (min_y1 <= w2["y1"] <= max_y1)
    return False


class SpatialWireIndex:
    """Fast spatial index for wire clearance and intersection queries."""

    def __init__(self, def_data: dict[str, Any], units: int = 1000) -> None:
        self.def_data = def_data
        self.units = units
        self.comp_boxes: list[tuple[int, int, int, int]] = []
        self.m1_by_y: dict[int, list[tuple[int, int, str, dict[str, Any]]]] = (
            collections.defaultdict(list)
        )
        self.m2_by_x: dict[int, list[tuple[int, int, str, dict[str, Any]]]] = (
            collections.defaultdict(list)
        )
        self.sorted_m1_y: list[int] = []
        self.sorted_m2_x: list[int] = []
        self.rebuild()

    def rebuild(self) -> None:
        """Rebuild the spatial index from the current DEF data."""
        netlist_data = self.def_data.get("_netlist_data")
        self.comp_boxes = [
            get_component_box(c, self.units, netlist_data)
            for c in self.def_data.get("components", [])
        ]
        self.m1_by_y.clear()
        self.m2_by_x.clear()

        for net in self.def_data.get("nets", []):
            net_name = net.get("name", "")
            for w in net.get("wires", []):
                layer = w.get("layer")
                if layer == "M1":
                    y = w["y1"]
                    if y == w["y2"]:
                        min_x = min(w["x1"], w["x2"])
                        max_x = max(w["x1"], w["x2"])
                        self.m1_by_y[y].append((min_x, max_x, net_name, w))
                elif layer == "M2":
                    x = w["x1"]
                    if x == w["x2"]:
                        min_y = min(w["y1"], w["y2"])
                        max_y = max(w["y1"], w["y2"])
                        self.m2_by_x[x].append((min_y, max_y, net_name, w))

        self.sorted_m1_y = sorted(self.m1_by_y.keys())
        self.sorted_m2_x = sorted(self.m2_by_x.keys())

    def is_v_clear_staircase(
        self,
        x: int,
        y1: int,
        y2: int,
        net_to_ignore: dict[str, Any] | None = None,
        min_runway: int = 2000,
    ) -> bool:
        """Check if a vertical segment is clear of obstacles for staircase elimination."""
        min_y = min(y1, y2)
        max_y = max(y1, y2)
        if min_y == max_y:
            return True

        for cx, cy, cw, ch in self.comp_boxes:
            if cx <= x <= cx + cw and not (cy + ch <= min_y or cy >= max_y):
                return False

        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""

        # Check collinear M2 wires on track x
        for ow_min, ow_max, on_name, _ in self.m2_by_x.get(x, []):
            if on_name == ignore_name:
                continue
            if max(ow_min, min_y) < min(ow_max, max_y):
                return False

        # Check crossing M1 wires
        start_idx = bisect.bisect_left(self.sorted_m1_y, min_y)
        end_idx = bisect.bisect_right(self.sorted_m1_y, max_y)
        for y_idx in range(start_idx, end_idx):
            ow_y = self.sorted_m1_y[y_idx]
            for ow_min_x, ow_max_x, on_name, _ in self.m1_by_y[ow_y]:
                if on_name == ignore_name:
                    continue
                if ow_min_x <= x <= ow_max_x:
                    runway_y1 = abs(ow_y - min_y)
                    runway_y2 = abs(ow_y - max_y)
                    if runway_y1 < min_runway and runway_y2 < min_runway:
                        return False

        return True

    def is_h_clear_staircase(
        self,
        y: int,
        x1: int,
        x2: int,
        net_to_ignore: dict[str, Any] | None = None,
        min_runway: int = 2000,
    ) -> bool:
        """Check if a horizontal segment is clear of obstacles for staircase elimination."""
        min_x = min(x1, x2)
        max_x = max(x1, x2)
        if min_x == max_x:
            return True

        for cx, cy, cw, ch in self.comp_boxes:
            if cy <= y <= cy + ch and not (cx + cw <= min_x or cx >= max_x):
                return False

        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""

        # Check collinear M1 wires on track y
        for ow_min, ow_max, on_name, _ in self.m1_by_y.get(y, []):
            if on_name == ignore_name:
                continue
            if max(ow_min, min_x) < min(ow_max, max_x):
                return False

        # Check crossing M2 wires
        start_idx = bisect.bisect_left(self.sorted_m2_x, min_x)
        end_idx = bisect.bisect_right(self.sorted_m2_x, max_x)
        for x_idx in range(start_idx, end_idx):
            ow_x = self.sorted_m2_x[x_idx]
            for ow_min_y, ow_max_y, on_name, _ in self.m2_by_x[ow_x]:
                if on_name == ignore_name:
                    continue
                if ow_min_y <= y <= ow_max_y:
                    runway_x1 = abs(ow_x - min_x)
                    runway_x2 = abs(ow_x - max_x)
                    if runway_x1 < min_runway and runway_x2 < min_runway:
                        return False

        return True

    def is_v_clear_detour(
        self,
        x: int,
        y1: int,
        y2: int,
        net_to_ignore: dict[str, Any] | None = None,
    ) -> bool:
        """Check if a vertical segment is clear for U-detour elimination."""
        min_y = min(y1, y2)
        max_y = max(y1, y2)
        if min_y == max_y:
            return True

        for cx, cy, cw, ch in self.comp_boxes:
            if cx <= x <= cx + cw and not (cy + ch <= min_y or cy >= max_y):
                return False

        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""

        for ow_min, ow_max, on_name, _ in self.m2_by_x.get(x, []):
            if on_name == ignore_name:
                continue
            if max(ow_min, min_y) < min(ow_max, max_y):
                return False

        start_idx = bisect.bisect_left(self.sorted_m1_y, min_y)
        end_idx = bisect.bisect_right(self.sorted_m1_y, max_y)
        min_dist = 2 * self.units
        for y_idx in range(start_idx, end_idx):
            ow_y = self.sorted_m1_y[y_idx]
            for ow_min_x, ow_max_x, on_name, _ in self.m1_by_y[ow_y]:
                if on_name == ignore_name:
                    continue
                if ow_min_x <= x <= ow_max_x:
                    runway_y1 = abs(ow_y - min_y)
                    runway_y2 = abs(ow_y - max_y)
                    runway_x1 = abs(x - ow_min_x)
                    runway_x2 = abs(x - ow_max_x)
                    if (runway_y1 < min_dist or runway_y2 < min_dist) and (
                        runway_x1 < min_dist or runway_x2 < min_dist
                    ):
                        return False

        return True

    def is_h_clear_detour(
        self,
        y: int,
        x1: int,
        x2: int,
        net_to_ignore: dict[str, Any] | None = None,
    ) -> bool:
        """Check if a horizontal segment is clear for U-detour elimination."""
        min_x = min(x1, x2)
        max_x = max(x1, x2)
        if min_x == max_x:
            return True

        for cx, cy, cw, ch in self.comp_boxes:
            if cy <= y <= cy + ch and not (cx + cw <= min_x or cx >= max_x):
                return False

        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""

        for ow_min, ow_max, on_name, _ in self.m1_by_y.get(y, []):
            if on_name == ignore_name:
                continue
            if max(ow_min, min_x) < min(ow_max, max_x):
                return False

        start_idx = bisect.bisect_left(self.sorted_m2_x, min_x)
        end_idx = bisect.bisect_right(self.sorted_m2_x, max_x)
        min_dist = 2 * self.units
        for x_idx in range(start_idx, end_idx):
            ow_x = self.sorted_m2_x[x_idx]
            for ow_min_y, ow_max_y, on_name, _ in self.m2_by_x[ow_x]:
                if on_name == ignore_name:
                    continue
                if ow_min_y <= y <= ow_max_y:
                    runway_x1 = abs(ow_x - min_x)
                    runway_x2 = abs(ow_x - max_x)
                    runway_y1 = abs(y - ow_min_y)
                    runway_y2 = abs(y - ow_max_y)
                    if (runway_x1 < min_dist or runway_x2 < min_dist) and (
                        runway_y1 < min_dist or runway_y2 < min_dist
                    ):
                        return False

        return True
