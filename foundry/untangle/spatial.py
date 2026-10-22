"""Spatial indexing and geometric clearance utilities for DEF routing."""

from __future__ import annotations

import bisect
import collections
from typing import Any

JsonDict = dict[str, Any]

DEFAULT_UNITS = 1000
DEFAULT_MIN_RUNWAY = 2000
DEFAULT_MACRO_TILES = 3
SPLITTER_LONG_TILES = 2
LARGE_MACHINE_TILES = 5
FURNACE_WIDTH_TILES = 5
FURNACE_HEIGHT_TILES = 3
DETOUR_MIN_CLEAR_TILES = 2
HORIZONTAL_ORIENTS = ("W", "E")


def _is_fluid_net(net: JsonDict, fluids: set[str] | frozenset[str] | None = None) -> bool:
    """Return True if the net carries a fluid material."""
    if net.get("nondefaultrule") == "fluid_ndr":
        return True
    if fluids:
        mat = net.get("name", "").split("__")[0].replace("_", "-")
        return mat in fluids or mat.replace("-", "_") in fluids
    return False


def _get_net_fluid(net: JsonDict) -> str:
    """Extract normalized fluid name from net name."""
    name = net.get("name", "")
    return name.split("__")[0].replace("_", "-")


def _orient_dimensions(w: int, h: int, orient: str) -> tuple[int, int]:
    """Return (width, height) adjusted for component orientation."""
    if orient in HORIZONTAL_ORIENTS:
        return h, w
    return w, h


def _ensure_netlist_maps(netlist_data: JsonDict) -> None:
    """Populate cached instance and macro lookup maps on netlist_data."""
    if netlist_data.get("_inst_map") is None:
        inst_map: dict[str, JsonDict] = {}
        for inst in netlist_data.get("instances", []):
            inst_map[inst["name"]] = inst
        netlist_data["_inst_map"] = inst_map
    if netlist_data.get("_macro_map") is None:
        cell_map = netlist_data.get("cells", {})
        macro_map: dict[str, JsonDict] = {}
        for k, cd in cell_map.items():
            if isinstance(cd, dict):
                key = cd.get("cell", k).replace(".", "__").replace("-", "_")
                macro_map[key] = cd
        netlist_data["_macro_map"] = macro_map


def _lookup_prepared_netlist_cell(
    cname: str, macro: str, netlist_data: JsonDict
) -> JsonDict | None:
    """Find the cell definition using pre-populated netlist lookup maps."""
    cell_map = netlist_data.get("cells", {})
    inst_map = netlist_data.get("_inst_map") or {}
    cell = cell_map.get(inst_map[cname].get("cell", "")) if cname in inst_map else None
    if not cell and macro:
        macro_map = netlist_data.get("_macro_map") or {}
        cell = macro_map.get(macro)
    return cell


def _fallback_macro_dimensions(macro: str, orient: str, units: int) -> tuple[int, int]:
    """Calculate component (width, height) from macro name when netlist data is absent."""
    if macro in ("belt_split2", "belt_balancer2"):
        if orient in HORIZONTAL_ORIENTS:
            return SPLITTER_LONG_TILES * units, units
        return units, SPLITTER_LONG_TILES * units
    if macro == "belt_merge2":
        return DEFAULT_MACRO_TILES * units, DEFAULT_MACRO_TILES * units
    if "refinery" in macro or "chemical_plant" in macro or "assembling_machine" in macro:
        return LARGE_MACHINE_TILES * units, LARGE_MACHINE_TILES * units
    if "furnace" in macro:
        return FURNACE_WIDTH_TILES * units, FURNACE_HEIGHT_TILES * units
    return DEFAULT_MACRO_TILES * units, DEFAULT_MACRO_TILES * units


def _compute_component_box_prepared(
    c: JsonDict,
    units: int,
    netlist_data: JsonDict | None = None,
) -> tuple[int, int, int, int]:
    """Calculate bounding box assuming netlist lookup maps are already initialized."""
    cx = c.get("x", 0)
    cy = c.get("y", 0)
    orient = c.get("orient", "N")
    cached = c.get("_box_cache")
    if cached is not None:
        cache_x, cache_y, cache_orient, cache_units, cache_w, cache_h = cached
        if (cache_x, cache_y, cache_orient, cache_units) == (cx, cy, orient, units):
            return cx, cy, cache_w, cache_h

    macro = c.get("macro", "")
    cname = c.get("name", "")
    cell = _lookup_prepared_netlist_cell(cname, macro, netlist_data) if netlist_data else None
    if cell:
        raw_w = int(cell.get("width", DEFAULT_MACRO_TILES)) * units
        raw_h = int(cell.get("height", DEFAULT_MACRO_TILES)) * units
        cw, ch = _orient_dimensions(raw_w, raw_h, orient)
    else:
        cw, ch = _fallback_macro_dimensions(macro, orient, units)

    c["_box_cache"] = (cx, cy, orient, units, cw, ch)
    return cx, cy, cw, ch


def get_component_box(
    c: JsonDict,
    units: int,
    netlist_data: JsonDict | None = None,
) -> tuple[int, int, int, int]:
    """Calculate the bounding box (x, y, w, h) of a placed component."""
    if netlist_data is not None:
        _ensure_netlist_maps(netlist_data)
    return _compute_component_box_prepared(c, units, netlist_data)


def wires_touch(w1: JsonDict, w2: JsonDict) -> bool:
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


def _interval_hits_detour_endpoint(ow_min: int, ow_max: int, coord: int, min_dist: int) -> bool:
    """Return True if coord lies within [ow_min, ow_max] and within min_dist of an endpoint."""
    if not (ow_min <= coord <= ow_max):
        return False
    return abs(coord - ow_min) < min_dist or abs(coord - ow_max) < min_dist


class SpatialWireIndex:
    """Fast spatial index for wire clearance and intersection queries."""

    def __init__(self, def_data: JsonDict, units: int = DEFAULT_UNITS) -> None:
        self.def_data = def_data
        self.units = units
        self.comp_boxes: list[tuple[int, int, int, int]] = []
        self.m1_by_y: dict[int, list[tuple[int, int, str, JsonDict]]] = collections.defaultdict(
            list
        )
        self.m2_by_x: dict[int, list[tuple[int, int, str, JsonDict]]] = collections.defaultdict(
            list
        )
        self.sorted_m1_y: list[int] = []
        self.sorted_m2_x: list[int] = []
        self.rebuild()

    def _index_wire(self, net_name: str, w: JsonDict) -> None:
        """Add a single horizontal M1 or vertical M2 wire segment to the index."""
        layer = w.get("layer")
        if layer == "M1" and w["y1"] == w["y2"]:
            min_x, max_x = min(w["x1"], w["x2"]), max(w["x1"], w["x2"])
            self.m1_by_y[w["y1"]].append((min_x, max_x, net_name, w))
        elif layer == "M2" and w["x1"] == w["x2"]:
            min_y, max_y = min(w["y1"], w["y2"]), max(w["y1"], w["y2"])
            self.m2_by_x[w["x1"]].append((min_y, max_y, net_name, w))

    def rebuild(self) -> None:
        """Rebuild the spatial index from the current DEF data."""
        netlist_data = self.def_data.get("_netlist_data")
        if netlist_data is not None:
            _ensure_netlist_maps(netlist_data)
        self.comp_boxes = [
            _compute_component_box_prepared(c, self.units, netlist_data)
            for c in self.def_data.get("components", [])
        ]
        self.m1_by_y.clear()
        self.m2_by_x.clear()

        for net in self.def_data.get("nets", []):
            net_name = net.get("name", "")
            for w in net.get("wires", []):
                self._index_wire(net_name, w)

        self.sorted_m1_y = sorted(self.m1_by_y.keys())
        self.sorted_m2_x = sorted(self.m2_by_x.keys())

    def _v_hits_component(self, x: int, min_y: int, max_y: int) -> bool:
        """Return True if vertical segment (x, min_y..max_y) intersects any component box."""
        for cx, cy, cw, ch in self.comp_boxes:
            if cx <= x <= cx + cw and not (cy + ch <= min_y or cy >= max_y):
                return True
        return False

    def _h_hits_component(self, y: int, min_x: int, max_x: int) -> bool:
        """Return True if horizontal segment (min_x..max_x, y) intersects any component box."""
        for cx, cy, cw, ch in self.comp_boxes:
            if cy <= y <= cy + ch and not (cx + cw <= min_x or cx >= max_x):
                return True
        return False

    def _v_hits_collinear_m2(self, x: int, min_y: int, max_y: int, ignore_name: str) -> bool:
        """Return True if vertical segment overlaps an existing M2 wire on track x."""
        for ow_min, ow_max, on_name, _ in self.m2_by_x.get(x, []):
            if on_name != ignore_name and max(ow_min, min_y) < min(ow_max, max_y):
                return True
        return False

    def _h_hits_collinear_m1(self, y: int, min_x: int, max_x: int, ignore_name: str) -> bool:
        """Return True if horizontal segment overlaps an existing M1 wire on track y."""
        for ow_min, ow_max, on_name, _ in self.m1_by_y.get(y, []):
            if on_name != ignore_name and max(ow_min, min_x) < min(ow_max, max_x):
                return True
        return False

    def _v_hits_crossing_m1_staircase(
        self, x: int, min_y: int, max_y: int, ignore_name: str, min_runway: int
    ) -> bool:
        """Return True if any crossing M1 wire violates staircase runway clearance."""
        start_idx = bisect.bisect_left(self.sorted_m1_y, min_y)
        end_idx = bisect.bisect_right(self.sorted_m1_y, max_y)
        for y_idx in range(start_idx, end_idx):
            ow_y = self.sorted_m1_y[y_idx]
            if abs(ow_y - min_y) >= min_runway or abs(ow_y - max_y) >= min_runway:
                continue
            for ow_min_x, ow_max_x, on_name, _ in self.m1_by_y[ow_y]:
                if on_name != ignore_name and ow_min_x <= x <= ow_max_x:
                    return True
        return False

    def _h_hits_crossing_m2_staircase(
        self, y: int, min_x: int, max_x: int, ignore_name: str, min_runway: int
    ) -> bool:
        """Return True if any crossing M2 wire violates staircase runway clearance."""
        start_idx = bisect.bisect_left(self.sorted_m2_x, min_x)
        end_idx = bisect.bisect_right(self.sorted_m2_x, max_x)
        for x_idx in range(start_idx, end_idx):
            ow_x = self.sorted_m2_x[x_idx]
            if abs(ow_x - min_x) >= min_runway or abs(ow_x - max_x) >= min_runway:
                continue
            for ow_min_y, ow_max_y, on_name, _ in self.m2_by_x[ow_x]:
                if on_name != ignore_name and ow_min_y <= y <= ow_max_y:
                    return True
        return False

    def is_v_clear_staircase(
        self,
        x: int,
        y1: int,
        y2: int,
        net_to_ignore: JsonDict | None = None,
        min_runway: int = DEFAULT_MIN_RUNWAY,
    ) -> bool:
        """Check if a vertical segment is clear of obstacles for staircase elimination."""
        min_y, max_y = min(y1, y2), max(y1, y2)
        if min_y == max_y:
            return True
        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""
        if self._v_hits_component(x, min_y, max_y):
            return False
        if self._v_hits_collinear_m2(x, min_y, max_y, ignore_name):
            return False
        return not self._v_hits_crossing_m1_staircase(x, min_y, max_y, ignore_name, min_runway)

    def is_h_clear_staircase(
        self,
        y: int,
        x1: int,
        x2: int,
        net_to_ignore: JsonDict | None = None,
        min_runway: int = DEFAULT_MIN_RUNWAY,
    ) -> bool:
        """Check if a horizontal segment is clear of obstacles for staircase elimination."""
        min_x, max_x = min(x1, x2), max(x1, x2)
        if min_x == max_x:
            return True
        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""
        if self._h_hits_component(y, min_x, max_x):
            return False
        if self._h_hits_collinear_m1(y, min_x, max_x, ignore_name):
            return False
        return not self._h_hits_crossing_m2_staircase(y, min_x, max_x, ignore_name, min_runway)

    def _v_hits_crossing_m1_detour(self, x: int, min_y: int, max_y: int, ignore_name: str) -> bool:
        """Return True if any crossing M1 wire violates U-detour runway clearance."""
        start_idx = bisect.bisect_left(self.sorted_m1_y, min_y)
        end_idx = bisect.bisect_right(self.sorted_m1_y, max_y)
        min_dist = DETOUR_MIN_CLEAR_TILES * self.units
        for y_idx in range(start_idx, end_idx):
            ow_y = self.sorted_m1_y[y_idx]
            if abs(ow_y - min_y) >= min_dist and abs(ow_y - max_y) >= min_dist:
                continue
            for ow_min_x, ow_max_x, on_name, _ in self.m1_by_y[ow_y]:
                if on_name != ignore_name and _interval_hits_detour_endpoint(
                    ow_min_x, ow_max_x, x, min_dist
                ):
                    return True
        return False

    def _h_hits_crossing_m2_detour(self, y: int, min_x: int, max_x: int, ignore_name: str) -> bool:
        """Return True if any crossing M2 wire violates U-detour runway clearance."""
        start_idx = bisect.bisect_left(self.sorted_m2_x, min_x)
        end_idx = bisect.bisect_right(self.sorted_m2_x, max_x)
        min_dist = DETOUR_MIN_CLEAR_TILES * self.units
        for x_idx in range(start_idx, end_idx):
            ow_x = self.sorted_m2_x[x_idx]
            if abs(ow_x - min_x) >= min_dist and abs(ow_x - max_x) >= min_dist:
                continue
            for ow_min_y, ow_max_y, on_name, _ in self.m2_by_x[ow_x]:
                if on_name != ignore_name and _interval_hits_detour_endpoint(
                    ow_min_y, ow_max_y, y, min_dist
                ):
                    return True
        return False

    def is_v_clear_detour(
        self,
        x: int,
        y1: int,
        y2: int,
        net_to_ignore: JsonDict | None = None,
    ) -> bool:
        """Check if a vertical segment is clear for U-detour elimination."""
        min_y, max_y = min(y1, y2), max(y1, y2)
        if min_y == max_y:
            return True
        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""
        if self._v_hits_component(x, min_y, max_y):
            return False
        if self._v_hits_collinear_m2(x, min_y, max_y, ignore_name):
            return False
        return not self._v_hits_crossing_m1_detour(x, min_y, max_y, ignore_name)

    def is_h_clear_detour(
        self,
        y: int,
        x1: int,
        x2: int,
        net_to_ignore: JsonDict | None = None,
    ) -> bool:
        """Check if a horizontal segment is clear for U-detour elimination."""
        min_x, max_x = min(x1, x2), max(x1, x2)
        if min_x == max_x:
            return True
        ignore_name = net_to_ignore.get("name", "") if net_to_ignore else ""
        if self._h_hits_component(y, min_x, max_x):
            return False
        if self._h_hits_collinear_m1(y, min_x, max_x, ignore_name):
            return False
        return not self._h_hits_crossing_m2_detour(y, min_x, max_x, ignore_name)
