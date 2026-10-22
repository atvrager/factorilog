"""DEF file parser and pin extractor.

Reads OpenROAD DEF files and extracts components, pins, and routed nets.
"""

from __future__ import annotations

import collections
import math
import re
from typing import Any, TypeAlias

DefRecord: TypeAlias = dict[str, Any]
CoordPoint: TypeAlias = tuple[int, int]

DEFAULT_DBU_UNITS = 1000
TILE_CENTER_OFFSET = 0.5
GROUP_1 = 1
GROUP_2 = 2
GROUP_3 = 3
GROUP_4 = 4
GROUP_5 = 5
COLLINEAR_DEGREE = 2

UNITS_PAT = re.compile(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;")
DIEAREA_PAT = re.compile(r"DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*;")
COMP_PAT = re.compile(
    r"^\s*-\s+(\S+)\s+(\S+)\s+\+\s+(?:PLACED|FIXED)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)",
    re.MULTILINE,
)
PIN_PAT = re.compile(
    r"-\s+(\S+)\s+\+\s+NET\s+(\S+)\s+\+\s+DIRECTION\s+(\S+).*?(?:PLACED|FIXED)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)",
    re.DOTALL,
)
NETS_SECTION_PAT = re.compile(r"NETS\s+\d+\s*;(.*?)END\s+NETS", re.DOTALL)
NET_CHUNK_SPLIT_PAT = re.compile(r"(?m)^\s*-\s+")
VIA_LINE_PAT = re.compile(
    r"(?:ROUTED|FIXED|COVER|NEW)\s+(\S+)(?:\s+TAPER)?\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+([A-Za-z0-9_]+)"
)
WIRE_LINE_PAT = re.compile(
    r"(?:ROUTED|FIXED|COVER|NEW)\s+(\S+)(?:\s+TAPER)?\s+\(\s*(-?\d+)\s+(-?\d+)(?:\s+-?\d+)?\s*\)\s+\(\s*(\*|-?\d+)\s+(\*|-?\d+)"
)
CONN_PAT = re.compile(r"\(\s*([A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s*\)")
NDR_PAT = re.compile(r"\+\s+NONDEFAULTRULE\s+(\S+)")
BLOCKAGES_SECTION_PAT = re.compile(r"BLOCKAGES\s+\d+\s*;(.*?)END\s+BLOCKAGES", re.DOTALL)
RECT_PAT = re.compile(
    r"RECT\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)",
    re.MULTILINE,
)
BUS_SUFFIX_PAT = re.compile(r"(__|--)bus\d+$")


def _non_zero_wires(wires: list[DefRecord]) -> list[DefRecord]:
    """Filter out zero-length wire segments."""
    return [w for w in wires if not (w["x1"] == w["x2"] and w["y1"] == w["y2"])]


def _collect_m1_m2_via_points(
    wires: list[DefRecord], vias: list[DefRecord] | None
) -> set[CoordPoint]:
    """Identify via coordinates that bridge both M1 and M2 wire endpoints."""
    m1_pts = {(w["x1"], w["y1"]) for w in wires if w.get("layer") == "M1"} | {
        (w["x2"], w["y2"]) for w in wires if w.get("layer") == "M1"
    }
    m2_pts = {(w["x1"], w["y1"]) for w in wires if w.get("layer") == "M2"} | {
        (w["x2"], w["y2"]) for w in wires if w.get("layer") == "M2"
    }
    return {
        (v["x"], v["y"])
        for v in (vias or [])
        if (v["x"], v["y"]) in m1_pts and (v["x"], v["y"]) in m2_pts
    }


def _try_merge_wire_pair(
    w1: DefRecord,
    w2: DefRecord,
    pt_counts: dict[CoordPoint, int],
    via_pts: set[CoordPoint],
) -> DefRecord | None:
    """Return a merged wire dictionary if w1 and w2 are collinear and share a degree-2 endpoint."""
    if w1.get("layer") != w2.get("layer"):
        return None
    if (w1["y1"] == w1["y2"]) != (w2["y1"] == w2["y2"]):
        return None
    p1_1: CoordPoint = (w1["x1"], w1["y1"])
    p1_2: CoordPoint = (w1["x2"], w1["y2"])
    p2_1: CoordPoint = (w2["x1"], w2["y1"])
    p2_2: CoordPoint = (w2["x2"], w2["y2"])

    if p1_2 == p2_1 and pt_counts[p1_2] == COLLINEAR_DEGREE and p1_2 not in via_pts:
        return {**w1, "x2": w2["x2"], "y2": w2["y2"]}
    if p1_1 == p2_2 and pt_counts[p1_1] == COLLINEAR_DEGREE and p1_1 not in via_pts:
        return {**w2, "x2": w1["x2"], "y2": w1["y2"]}
    if p1_2 == p2_2 and pt_counts[p1_2] == COLLINEAR_DEGREE and p1_2 not in via_pts:
        return {**w1, "x2": w2["x1"], "y2": w2["y1"]}
    if p1_1 == p2_1 and pt_counts[p1_1] == COLLINEAR_DEGREE and p1_1 not in via_pts:
        return {**w1, "x1": w2["x2"], "y1": w2["y2"]}
    return None


def _find_mergeable_partner(
    start_idx: int,
    w1: DefRecord,
    wires: list[DefRecord],
    skip: set[int],
    pt_counts: dict[CoordPoint, int],
    via_pts: set[CoordPoint],
) -> tuple[int, DefRecord] | None:
    """Find the first wire after `start_idx` that can merge collinearly with `w1`."""
    for j in range(start_idx + 1, len(wires)):
        if j in skip:
            continue
        merged_w = _try_merge_wire_pair(w1, wires[j], pt_counts, via_pts)
        if merged_w is not None:
            return j, merged_w
    return None


def _merge_wire_pass(
    wires: list[DefRecord],
    via_pts: set[CoordPoint],
) -> tuple[list[DefRecord], bool]:
    """Run one pass of collinear wire merging and report whether any pair merged."""
    pt_counts: dict[CoordPoint, int] = collections.defaultdict(int)
    for w in wires:
        pt_counts[(w["x1"], w["y1"])] += 1
        pt_counts[(w["x2"], w["y2"])] += 1

    merged_wires: list[DefRecord] = []
    skip: set[int] = set()
    changed = False
    for i, w1 in enumerate(wires):
        if i in skip:
            continue
        partner = _find_mergeable_partner(i, w1, wires, skip, pt_counts, via_pts)
        if partner is not None:
            j, merged_w = partner
            skip.add(i)
            skip.add(j)
            changed = True
            merged_wires.append(merged_w)
        else:
            merged_wires.append(w1)
    return merged_wires, changed


def merge_collinear_wires(
    wires: list[DefRecord], vias: list[DefRecord] | None = None
) -> list[DefRecord]:
    """Merge contiguous collinear wire segments on the same layer."""
    active_wires = _non_zero_wires(wires)
    via_pts = _collect_m1_m2_via_points(active_wires, vias)
    changed = True
    while changed:
        active_wires, changed = _merge_wire_pass(active_wires, via_pts)
    return _non_zero_wires(active_wires)


def _parse_net_routed_line(
    line: str,
    wires: list[DefRecord],
    vias: list[DefRecord],
) -> None:
    """Parse a single routed DEF net line and append either a via or wire segment."""
    m_via = VIA_LINE_PAT.search(line)
    if m_via and not m_via.group(GROUP_4).isdigit() and m_via.group(GROUP_4) != "*":
        vias.append(
            {
                "layer": m_via.group(GROUP_1),
                "x": int(m_via.group(GROUP_2)),
                "y": int(m_via.group(GROUP_3)),
                "via": m_via.group(GROUP_4),
            }
        )
        return

    m_wire = WIRE_LINE_PAT.search(line)
    if m_wire:
        x1 = int(m_wire.group(GROUP_2))
        y1 = int(m_wire.group(GROUP_3))
        x2_str = m_wire.group(GROUP_4)
        y2_str = m_wire.group(GROUP_5)
        wires.append(
            {
                "layer": m_wire.group(GROUP_1),
                "x1": x1,
                "y1": y1,
                "x2": x1 if x2_str == "*" else int(x2_str),
                "y2": y1 if y2_str == "*" else int(y2_str),
            }
        )


def _parse_net_chunk(chunk: str) -> DefRecord | None:
    """Parse one net block from the DEF NETS section."""
    cleaned = chunk.strip()
    if not cleaned:
        return None
    lines = cleaned.splitlines()
    net_name = lines[0].split()[0]
    wires: list[DefRecord] = []
    vias: list[DefRecord] = []
    for line in lines:
        _parse_net_routed_line(line.strip(), wires, vias)

    conns = [
        (c[0], c[1])
        for c in CONN_PAT.findall(cleaned)
        if not (c[1].isdigit() or c[0].isdigit() or c[1] == "*" or c[0] == "*")
    ]
    ndr_match = NDR_PAT.search(cleaned)
    return {
        "name": net_name,
        "wires": merge_collinear_wires(wires, vias),
        "vias": vias,
        "connections": conns,
        "nondefaultrule": ndr_match.group(GROUP_1) if ndr_match else None,
    }


def parse_def(text: str) -> DefRecord:
    """Parse DEF text into components, nets, pins, and design units."""
    m_units = UNITS_PAT.search(text)
    units = int(m_units.group(GROUP_1)) if m_units else DEFAULT_DBU_UNITS

    m_die = DIEAREA_PAT.search(text)
    die_area = (
        [
            int(m_die.group(GROUP_1)),
            int(m_die.group(GROUP_2)),
            int(m_die.group(GROUP_3)),
            int(m_die.group(GROUP_4)),
        ]
        if m_die
        else [0, 0, 0, 0]
    )

    components: list[DefRecord] = [
        {
            "name": m.group(GROUP_1),
            "macro": m.group(GROUP_2),
            "x": int(m.group(GROUP_3)),
            "y": int(m.group(GROUP_4)),
            "orient": m.group(GROUP_5),
        }
        for m in COMP_PAT.finditer(text)
    ]

    pins: list[DefRecord] = [
        {
            "name": m.group(GROUP_1),
            "net": m.group(GROUP_2),
            "direction": m.group(GROUP_3),
            "x": int(m.group(GROUP_4)),
            "y": int(m.group(GROUP_5)),
        }
        for m in PIN_PAT.finditer(text)
    ]

    nets: list[DefRecord] = []
    nets_match = NETS_SECTION_PAT.search(text)
    if nets_match:
        for chunk in NET_CHUNK_SPLIT_PAT.split(nets_match.group(GROUP_1)):
            parsed_net = _parse_net_chunk(chunk)
            if parsed_net is not None:
                nets.append(parsed_net)

    blockages: list[DefRecord] = []
    blockages_match = BLOCKAGES_SECTION_PAT.search(text)
    if blockages_match:
        blockages.extend(
            {
                "x1": int(m.group(GROUP_1)),
                "y1": int(m.group(GROUP_2)),
                "x2": int(m.group(GROUP_3)),
                "y2": int(m.group(GROUP_4)),
            }
            for m in RECT_PAT.finditer(blockages_match.group(GROUP_1))
        )

    return {
        "units": units,
        "die_area": die_area,
        "components": components,
        "pins": pins,
        "nets": nets,
        "blockages": blockages,
    }


def _build_known_material_lookup(known_materials: list[str]) -> dict[str, str]:
    """Build an O(1) lookup table from raw/hyphenated/underscored net names to base material."""
    lookup: dict[str, str] = {}
    for known in known_materials:
        known_base = BUS_SUFFIX_PAT.sub("", known).replace("_", "-")
        lookup.setdefault(known, known_base)
        lookup.setdefault(known.replace("-", "_"), known_base)
        lookup.setdefault(known_base, known_base)
    return lookup


def extract_pins(def_data: DefRecord, netlist_data: DefRecord) -> dict[str, list[DefRecord]]:
    """Extract input and output pin coordinates and types by material name."""
    scale = 1.0 / float(def_data.get("units", DEFAULT_DBU_UNITS))
    fluids = set(netlist_data.get("fluids") or [])
    inputs: list[DefRecord] = []
    outputs: list[DefRecord] = []

    known_materials = list(netlist_data.get("inputs", [])) + list(netlist_data.get("outputs", []))
    known_lookup = _build_known_material_lookup(known_materials)

    for pin in def_data.get("pins", []):
        raw_net = pin.get("net", pin["name"])
        direction = pin.get("direction", "INPUT").upper()
        base_net = BUS_SUFFIX_PAT.sub("", raw_net)
        base_mat = base_net.replace("_", "-")
        mat = known_lookup.get(base_mat) or known_lookup.get(base_net) or raw_net.replace("_", "-")

        entry = {
            "name": mat,
            "x": math.floor(pin["x"] * scale) + TILE_CENTER_OFFSET,
            "y": math.floor(pin["y"] * scale) + TILE_CENTER_OFFSET,
            "type": "pipe" if mat in fluids else "belt",
        }
        if direction == "INPUT":
            inputs.append(entry)
        elif direction == "OUTPUT":
            outputs.append(entry)

    inputs.sort(key=lambda p: (p["name"], p["x"], p["y"]))
    outputs.sort(key=lambda p: (p["name"], p["x"], p["y"]))

    return {
        "inputs": inputs,
        "outputs": outputs,
    }
