"""DEF file parser and pin extractor.

Reads OpenROAD DEF files and extracts components, pins, and routed nets.
"""

from __future__ import annotations

import collections
import math
import re
from typing import Any


def merge_collinear_wires(
    wires: list[dict[str, Any]], vias: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Merge contiguous collinear wire segments on the same layer."""
    wires = [w for w in wires if not (w["x1"] == w["x2"] and w["y1"] == w["y2"])]
    m1_pts = {(w["x1"], w["y1"]) for w in wires if w.get("layer") == "M1"} | {
        (w["x2"], w["y2"]) for w in wires if w.get("layer") == "M1"
    }
    m2_pts = {(w["x1"], w["y1"]) for w in wires if w.get("layer") == "M2"} | {
        (w["x2"], w["y2"]) for w in wires if w.get("layer") == "M2"
    }
    via_pts = {
        (v["x"], v["y"])
        for v in (vias or [])
        if (v["x"], v["y"]) in m1_pts and (v["x"], v["y"]) in m2_pts
    }
    changed = True
    while changed:
        changed = False
        pt_counts: dict[tuple[int, int], int] = collections.defaultdict(int)
        for w in wires:
            pt_counts[(w["x1"], w["y1"])] += 1
            pt_counts[(w["x2"], w["y2"])] += 1

        merged_wires: list[dict[str, Any]] = []
        skip: set[int] = set()
        for i, w1 in enumerate(wires):
            if i in skip:
                continue
            merged = False
            for j, w2 in enumerate(wires):
                if j <= i or j in skip:
                    continue
                if w1.get("layer") != w2.get("layer"):
                    continue
                is_w1_h = w1["y1"] == w1["y2"]
                is_w2_h = w2["y1"] == w2["y2"]
                if is_w1_h != is_w2_h:
                    continue
                p1_1 = (w1["x1"], w1["y1"])
                p1_2 = (w1["x2"], w1["y2"])
                p2_1 = (w2["x1"], w2["y1"])
                p2_2 = (w2["x2"], w2["y2"])

                if p1_2 == p2_1 and pt_counts[p1_2] == 2 and p1_2 not in via_pts:
                    new_w = dict(w1)
                    new_w["x2"], new_w["y2"] = w2["x2"], w2["y2"]
                    merged = True
                elif p1_1 == p2_2 and pt_counts[p1_1] == 2 and p1_1 not in via_pts:
                    new_w = dict(w2)
                    new_w["x2"], new_w["y2"] = w1["x2"], w1["y2"]
                    merged = True
                elif p1_2 == p2_2 and pt_counts[p1_2] == 2 and p1_2 not in via_pts:
                    new_w = dict(w1)
                    new_w["x2"], new_w["y2"] = w2["x1"], w2["y1"]
                    merged = True
                elif p1_1 == p2_1 and pt_counts[p1_1] == 2 and p1_1 not in via_pts:
                    new_w = dict(w1)
                    new_w["x1"], new_w["y1"] = w2["x2"], w2["y2"]
                    merged = True

                if merged:
                    merged_wires.append(new_w)
                    skip.add(i)
                    skip.add(j)
                    changed = True
                    break
            if not merged:
                merged_wires.append(w1)
        wires = merged_wires
    return [w for w in wires if not (w["x1"] == w["x2"] and w["y1"] == w["y2"])]


def parse_def(text: str) -> dict[str, Any]:
    """Parse DEF text into components, nets, pins, and design units."""
    units = 1000
    m_units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text)
    if m_units:
        units = int(m_units.group(1))

    die_area = [0, 0, 0, 0]
    m_die = re.search(
        r"DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*;", text
    )
    if m_die:
        die_area = [
            int(m_die.group(1)),
            int(m_die.group(2)),
            int(m_die.group(3)),
            int(m_die.group(4)),
        ]

    comp_pat = re.compile(
        r"^\s*-\s+(\S+)\s+(\S+)\s+\+\s+(?:PLACED|FIXED)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\S+)",
        re.MULTILINE,
    )
    components: list[dict[str, Any]] = [
        {
            "name": m.group(1),
            "macro": m.group(2),
            "x": int(m.group(3)),
            "y": int(m.group(4)),
            "orient": m.group(5),
        }
        for m in comp_pat.finditer(text)
    ]

    pin_pat = re.compile(
        r"-\s+(\S+)\s+\+\s+NET\s+(\S+)\s+\+\s+DIRECTION\s+(\S+).*?(?:PLACED|FIXED)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)",
        re.DOTALL,
    )
    pins: list[dict[str, Any]] = [
        {
            "name": m.group(1),
            "net": m.group(2),
            "direction": m.group(3),
            "x": int(m.group(4)),
            "y": int(m.group(5)),
        }
        for m in pin_pat.finditer(text)
    ]

    # Parse NETS section
    nets: list[dict[str, Any]] = []
    nets_match = re.search(r"NETS\s+\d+\s*;(.*?)END\s+NETS", text, re.DOTALL)
    if nets_match:
        nets_text = nets_match.group(1)
        net_chunks = re.split(r"(?m)^\s*-\s+", nets_text)
        for chunk in net_chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            lines = chunk.splitlines()
            header = lines[0].split()
            net_name = header[0]
            wires: list[dict[str, Any]] = []
            vias: list[dict[str, Any]] = []

            for line in lines:
                line = line.strip()
                m_via = re.search(
                    r"(?:ROUTED|FIXED|COVER|NEW)\s+(\S+)(?:\s+TAPER)?\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+([A-Za-z0-9_]+)",
                    line,
                )
                if m_via and not m_via.group(4).isdigit() and m_via.group(4) != "*":
                    vias.append(
                        {
                            "layer": m_via.group(1),
                            "x": int(m_via.group(2)),
                            "y": int(m_via.group(3)),
                            "via": m_via.group(4),
                        }
                    )
                    continue

                m_wire = re.search(
                    r"(?:ROUTED|FIXED|COVER|NEW)\s+(\S+)(?:\s+TAPER)?\s+\(\s*(-?\d+)\s+(-?\d+)(?:\s+-?\d+)?\s*\)\s+\(\s*(\*|-?\d+)\s+(\*|-?\d+)",
                    line,
                )
                if m_wire:
                    layer = m_wire.group(1)
                    x1 = int(m_wire.group(2))
                    y1 = int(m_wire.group(3))
                    x2_str = m_wire.group(4)
                    y2_str = m_wire.group(5)
                    x2 = x1 if x2_str == "*" else int(x2_str)
                    y2 = y1 if y2_str == "*" else int(y2_str)
                    wires.append(
                        {
                            "layer": layer,
                            "x1": x1,
                            "y1": y1,
                            "x2": x2,
                            "y2": y2,
                        }
                    )

            conns = [
                (c[0], c[1])
                for c in re.findall(r"\(\s*([A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s*\)", chunk)
                if not (c[1].isdigit() or c[0].isdigit() or c[1] == "*" or c[0] == "*")
            ]

            ndr_match = re.search(r"\+\s+NONDEFAULTRULE\s+(\S+)", chunk)
            ndr = ndr_match.group(1) if ndr_match else None

            nets.append(
                {
                    "name": net_name,
                    "wires": merge_collinear_wires(wires, vias),
                    "vias": vias,
                    "connections": conns,
                    "nondefaultrule": ndr,
                }
            )

    # Parse BLOCKAGES section
    blockages: list[dict[str, Any]] = []
    blockages_match = re.search(r"BLOCKAGES\s+\d+\s*;(.*?)END\s+BLOCKAGES", text, re.DOTALL)
    if blockages_match:
        rect_pat = re.compile(
            r"RECT\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)",
            re.MULTILINE,
        )
        blockages.extend(
            {
                "x1": int(m.group(1)),
                "y1": int(m.group(2)),
                "x2": int(m.group(3)),
                "y2": int(m.group(4)),
            }
            for m in rect_pat.finditer(blockages_match.group(1))
        )

    return {
        "units": units,
        "die_area": die_area,
        "components": components,
        "pins": pins,
        "nets": nets,
        "blockages": blockages,
    }


def extract_pins(
    def_data: dict[str, Any], netlist_data: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Extract input and output pin coordinates and types by material name."""
    scale = 1.0 / float(def_data.get("units", 1000))
    fluids = set(netlist_data.get("fluids") or [])
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []

    known_materials = list(netlist_data.get("inputs", [])) + list(netlist_data.get("outputs", []))

    for pin in def_data.get("pins", []):
        raw_net = pin.get("net", pin["name"])
        direction = pin.get("direction", "INPUT").upper()

        mat = raw_net.replace("_", "-")
        base_net = re.sub(r"(__|--)bus\d+$", "", raw_net)
        base_mat = base_net.replace("_", "-")
        for known in known_materials:
            known_base = re.sub(r"(__|--)bus\d+$", "", known).replace("_", "-")
            if known == base_mat or known.replace("-", "_") == base_net or known_base == base_mat:
                mat = known_base
                break

        x = math.floor(pin["x"] * scale) + 0.5
        y = math.floor(pin["y"] * scale) + 0.5
        pin_type = "pipe" if mat in fluids else "belt"

        entry = {
            "name": mat,
            "x": x,
            "y": y,
            "type": pin_type,
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
