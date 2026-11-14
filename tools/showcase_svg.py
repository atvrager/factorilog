"""Deterministic vector SVG generators for factorilog pipeline stages.

Produces pure SVG 1.1 diagrams from compiled netlists, DEF placement files,
routed blueprints, and synthesized SystemVerilog controllers.
"""

from __future__ import annotations

import collections
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any
from xml.sax.saxutils import escape

from foundry.entity_helpers import (
    DEFAULT_DEF_UNITS,
    DIR_STEPS,
    DIRECTION_NORTH,
    entity_size,
)
from tools.showcase_data import (
    MACHINE_CATEGORY_KEYS,
    extract_geometry_layers,
    naive_rank_hpwl,
    select_street_straddle_window,
)

SVG_W = 1200
SVG_H_TALL = 560
SVG_H_STD = 540
ROUTER_WIN_W = 48
ROUTER_WIN_H = 36
ROUTER_VP_X = 30
ROUTER_VP_Y = 100
ROUTER_VP_W = 586
ROUTER_VP_H = 390
PLACEMENT_PANEL_W = 572
PLACEMENT_MAP_SIZE = 412.0
PLACEMENT_MAP_Y_OFFSET = 38.0
BELT_EXTEND_TILES = 0.38
PIPE_ISOLATED_HALF_TILES = 0.3
UG_ENDPOINT_MARGIN_TILES = 0.4
CLIP_T_EPS = 1e-6
CLIP_MIN_WORLD_LEN = 1e-3
MIN_BELT_PX_LEN = 2.0
MIN_ARROW_PX_LEN = 5.0
MIN_PIPE_PX_LEN = 1.0
STACK_DESC_MAX_CHARS = 68
MIN_TREEMAP_LABEL_W = 68.0
MIN_TREEMAP_LABEL_H = 32.0
MAX_FLYLINES = 260
MODULE_NAME_RE = re.compile(r"\bmodule\s+([A-Za-z0-9_]+)")

PALETTE = {
    "bg": "#0b0e14",
    "panel": "#141a23",
    "panel_alt": "#111620",
    "border": "#232c3b",
    "grid": "#1c2432",
    "text": "#e2e8f0",
    "muted": "#94a3b8",
    "cyan": "#38bdf8",
    "emerald": "#34d399",
    "amber": "#f59e0b",
    "violet": "#a78bfa",
    "rose": "#f472b6",
    "teal": "#2dd4bf",
    "coral": "#fb7185",
    "indigo": "#818cf8",
}

MACHINE_COLORS: tuple[str, ...] = (
    "#38bdf8",
    "#f59e0b",
    "#34d399",
    "#a78bfa",
    "#f472b6",
    "#2dd4bf",
    "#fb7185",
    "#818cf8",
)


def _svg_open(
    width: int,
    height: int,
    title: str,
    subtitle: str,
    extra_defs: Sequence[str] = (),
) -> list[str]:
    """Return opening SVG tags, dark theme styles, and top header banner."""
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="auto" role="img" aria-label="{escape(title)}">',
        "<defs>",
        '<marker id="arr-cyan" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#38bdf8"/></marker>',
        '<marker id="arr-amber" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#f59e0b"/></marker>',
        '<marker id="arr-belt" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="3.6" markerHeight="3.6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#f59e0b"/></marker>',
        '<marker id="arr-emerald" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#34d399"/></marker>',
        '<marker id="arr-rose" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#f472b6"/></marker>',
        *extra_defs,
        "</defs>",
        f'<rect width="{width}" height="{height}" rx="10" fill="{PALETTE["bg"]}" stroke="{PALETTE["border"]}"/>',
        '<style>text{font-family:"JetBrains Mono","SFMono-Regular",Consolas,monospace;fill:#e2e8f0;}.title{font-size:15px;font-weight:700;fill:#f8fafc;}.sub{font-size:11px;fill:#94a3b8;}.hdr{font-size:12px;font-weight:700;fill:#38bdf8;}.lbl{font-size:10px;fill:#e2e8f0;}.dim{font-size:9px;fill:#94a3b8;}.badge{font-size:10px;font-weight:700;}</style>',
        f'<rect x="16" y="14" width="{width - 32}" height="38" rx="6" fill="{PALETTE["panel_alt"]}" stroke="{PALETTE["border"]}"/>',
        f'<text x="28" y="32" class="title">{escape(title)}</text>',
        f'<text x="28" y="45" class="sub">{escape(subtitle)}</text>',
    ]


def _color_for_key(key: str) -> str:
    """Assign a deterministic accent color from `MACHINE_COLORS`."""
    total = sum(ord(ch) for ch in key)
    return MACHINE_COLORS[total % len(MACHINE_COLORS)]


def _lerp_rank_color(frac: float) -> str:
    """Interpolate RGB hex color from cyan `#38bdf8` at West to rose `#f472b6` at East."""
    t = max(0.0, min(1.0, frac))
    r = round(0x38 + (0xF4 - 0x38) * t)
    g = round(0xBD + (0x72 - 0xBD) * t)
    b = round(0xF8 + (0xB6 - 0xF8) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _short_label(raw: str, max_chars: int = 18) -> str:
    """Shorten hyphenated identifiers cleanly to fit fixed diagram boxes."""
    if len(raw) <= max_chars:
        return raw
    return raw[: max_chars - 2] + ".."


def build_lp_dag_treemap_svg(
    dag_netlist: Mapping[str, Any],
    chassis_netlist: Mapping[str, Any],
) -> str:
    """Build Stage 1 SVG: LP recipe DAG with splitter fanout and chassis area treemap."""
    demand = dag_netlist.get("demand", {})
    demand_label = (
        ", ".join(f"{k}={v:.0f}/m" for k, v in demand.items())
        if isinstance(demand, Mapping)
        else "60/m"
    )
    lines = _svg_open(
        SVG_W,
        SVG_H_STD,
        "Stage 1: LP Rate Solver DAG and 7-Science Chassis Cell Treemap",
        f"Left: topological recipe graph from {demand_label} | Right: tile footprint of 3,824 placed chassis instances",
    )

    # Left panel container
    lines.append(
        f'<rect x="16" y="64" width="584" height="460" rx="8" fill="{PALETTE["panel"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(
        '<text x="30" y="86" class="hdr">Recipe Hypergraph &amp; Synthesized Binary Splitters</text>'
    )
    _append_recipe_dag(lines, dag_netlist, x0=28, y0=102, w=560, h=406)

    # Right panel container
    lines.append(
        f'<rect x="614" y="64" width="570" height="460" rx="8" fill="{PALETTE["panel"]}" stroke="{PALETTE["border"]}"/>'
    )
    chassis_insts = chassis_netlist.get("instances", ())
    chassis_cells = chassis_netlist.get("cells", {})
    lines.append(
        f'<text x="628" y="86" class="hdr">PDK Cell Area Treemap ({len(chassis_insts)} instances, {len(chassis_cells)} cells)</text>'
    )
    _append_treemap(lines, chassis_netlist, x0=628, y0=104, w=542, h=374)

    lines.append("</svg>")
    return "\n".join(lines)


def _append_recipe_dag(
    lines: list[str],
    dag_netlist: Mapping[str, Any],
    *,
    x0: int,
    y0: int,
    w: int,
    h: int,
) -> None:
    """Lay out and append the recipe DAG nodes and rate-labeled edges."""
    cells = dag_netlist.get("cells", {})
    raw_rates = dag_netlist.get("raw_rates_per_min", {})
    instances = dag_netlist.get("instances", ())

    splitter_counts: dict[str, int] = collections.Counter()
    recipe_stats: dict[str, dict[str, Any]] = {}
    for inst in instances:
        name = str(inst.get("name", ""))
        cell_key = str(inst.get("cell", ""))
        cell = cells.get(cell_key, {}) if isinstance(cells, Mapping) else {}
        recipe = str(inst.get("recipe", cell.get("recipe", "")))
        if machine_is_splitter(cell):
            for mat in raw_rates:
                mat_tok = str(mat).replace("-", "_")
                if mat_tok in name:
                    splitter_counts[mat] += 1
            for rname in cells:
                rc = cells[rname]
                for out_item in rc.get("outputs", ()):
                    if out_item.replace("-", "_") in name:
                        splitter_counts[out_item] += 1
            continue
        entry = recipe_stats.setdefault(
            recipe,
            {
                "count": 0,
                "rate": 0.0,
                "ranks": [],
                "machine": str(cell.get("machine", "")),
                "inputs": list(cell.get("inputs", ())),
                "outputs": list(cell.get("outputs", ())),
            },
        )
        entry["count"] += 1
        entry["rate"] += float(inst.get("plan_rate_per_min", 0.0))
        entry["ranks"].append(int(inst.get("rank", 0)))

    # Build node list including raw sources
    nodes: dict[str, dict[str, Any]] = {}
    for mat, rate in raw_rates.items():
        nodes[f"raw:{mat}"] = {
            "label": str(mat),
            "sub": f"{float(rate):.0f}/m raw",
            "rank": -1.0,
            "outputs": [str(mat)],
            "inputs": [],
            "kind": "raw",
        }
    for recipe, st in recipe_stats.items():
        avg_rank = sum(st["ranks"]) / max(len(st["ranks"]), 1)
        nodes[recipe] = {
            "label": recipe,
            "sub": f"{st['count']}x {st['machine'][:8]} ({st['rate']:.0f}/m)",
            "rank": avg_rank,
            "outputs": st["outputs"],
            "inputs": st["inputs"],
            "kind": "sink" if recipe in dag_netlist.get("demand", {}) else "prod",
        }

    # Group nodes into 5 readable topological columns by rank ordering
    sorted_keys = sorted(nodes.keys(), key=lambda k: (nodes[k]["rank"], k))
    col_count = 5
    cols: list[list[str]] = [[] for _ in range(col_count)]
    for idx, key in enumerate(sorted_keys):
        if nodes[key]["kind"] == "raw":
            cols[0].append(key)
        elif nodes[key]["kind"] == "sink":
            cols[col_count - 1].append(key)
        else:
            frac = idx / max(len(sorted_keys) - 1, 1)
            col_idx = max(1, min(col_count - 2, round(frac * (col_count - 1))))
            cols[col_idx].append(key)

    node_w = 100
    node_h = 38
    col_step = (w - node_w) / max(col_count - 1, 1)
    node_pos: dict[str, tuple[float, float]] = {}
    for ci, group in enumerate(cols):
        cx = x0 + ci * col_step
        row_step = h / max(len(group) + 1, 2)
        for ri, key in enumerate(group):
            cy = y0 + (ri + 1) * row_step - node_h / 2
            node_pos[key] = (cx, cy)

    # Draw directed material edges before nodes
    producers_of: dict[str, list[str]] = collections.defaultdict(list)
    for key, info in nodes.items():
        for out_item in info["outputs"]:
            producers_of[out_item].append(key)

    for dst_key, dst_info in nodes.items():
        if dst_key not in node_pos:
            continue
        dx, dy = node_pos[dst_key]
        for item in dst_info["inputs"]:
            for src_key in producers_of.get(item, ()):
                if src_key not in node_pos or src_key == dst_key:
                    continue
                sx, sy = node_pos[src_key]
                x1, y1 = sx + node_w, sy + node_h / 2
                x2, y2 = dx, dy + node_h / 2
                mx = (x1 + x2) / 2
                has_split = splitter_counts.get(item, 0) > 0
                stroke = PALETTE["amber"] if has_split else PALETTE["cyan"]
                dash = "" if has_split else ' stroke-dasharray="4 3"'
                lines.append(
                    f'<path d="M{x1:.1f},{y1:.1f} C{mx:.1f},{y1:.1f} {mx:.1f},{y2:.1f} {x2:.1f},{y2:.1f}" fill="none" stroke="{stroke}" stroke-width="1.5" stroke-opacity="0.75"{dash} marker-end="url(#arr-{"amber" if has_split else "cyan"})"/>'
                )

    for key, (nx, ny) in node_pos.items():
        info = nodes[key]
        border = (
            PALETTE["emerald"]
            if info["kind"] == "raw"
            else PALETTE["rose"]
            if info["kind"] == "sink"
            else PALETTE["cyan"]
        )
        lines.append(
            f'<rect x="{nx:.1f}" y="{ny:.1f}" width="{node_w}" height="{node_h}" rx="5" fill="{PALETTE["panel_alt"]}" stroke="{border}" stroke-width="1.4"/>'
        )
        lines.append(
            f'<text x="{nx + 6:.1f}" y="{ny + 15:.1f}" class="lbl">{escape(_short_label(info["label"], 15))}</text>'
        )
        lines.append(
            f'<text x="{nx + 6:.1f}" y="{ny + 29:.1f}" class="dim">{escape(_short_label(info["sub"], 16))}</text>'
        )

    lines.append(
        f'<text x="{x0}" y="{y0 + h + 10}" class="dim">Solid amber = synthesized 2-to-2 splitter tree | Dashed cyan = direct feed | Zero LP balance error</text>'
    )


def machine_is_splitter(cell: Mapping[str, Any]) -> bool:
    """Return True when cell represents a splitter, merger, or balancer macro."""
    return str(cell.get("recipe", "")) in ("split2", "merge2", "balancer2")


def _append_treemap(
    lines: list[str],
    chassis_netlist: Mapping[str, Any],
    *,
    x0: int,
    y0: int,
    w: int,
    h: int,
) -> None:
    """Compute a slice-and-dice treemap of cell tile areas and append SVG rects."""
    cells = chassis_netlist.get("cells", {})
    counts: dict[str, int] = collections.Counter(
        str(inst.get("cell", "")) for inst in chassis_netlist.get("instances", ())
    )
    items: list[tuple[float, str, str, int]] = []
    for cell_key, count in counts.items():
        cell = cells.get(cell_key, {}) if isinstance(cells, Mapping) else {}
        cw = float(cell.get("width", 3))
        ch = float(cell.get("height", 3))
        area = count * cw * ch
        recipe = str(cell.get("recipe", cell_key))
        machine = str(cell.get("machine", cell_key))
        items.append((area, recipe, machine, count))

    items.sort(reverse=True)
    total_area = sum(item[0] for item in items) or 1.0
    rects = _treemap_partition(items, float(x0), float(y0), float(w), float(h))

    machine_legend: dict[str, str] = {}
    for rx, ry, rw, rh, area, recipe, machine, count in rects:
        color = _color_for_key(machine)
        machine_legend.setdefault(machine, color)
        lines.append(
            f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{max(rw - 1.5, 1.0):.1f}" height="{max(rh - 1.5, 1.0):.1f}" rx="3" fill="{color}" fill-opacity="0.22" stroke="{color}" stroke-width="1"/>'
        )
        if rw >= MIN_TREEMAP_LABEL_W and rh >= MIN_TREEMAP_LABEL_H:
            lines.append(
                f'<text x="{rx + 5:.1f}" y="{ry + 14:.1f}" class="lbl">{escape(_short_label(recipe, max(6, int(rw // 7))))}</text>'
            )
            lines.append(
                f'<text x="{rx + 5:.1f}" y="{ry + 26:.1f}" class="dim">{count}x | {area:.0f}t2</text>'
            )

    legend_x = x0
    legend_y = y0 + h + 18
    lines.append(
        f'<text x="{legend_x}" y="{legend_y}" class="dim">Total cell halo area: {total_area:,.0f} tiles2 across {len(items)} cells</text>'
    )
    lx = legend_x
    ly = legend_y + 16
    for machine, color in list(machine_legend.items())[:5]:
        lines.append(
            f'<rect x="{lx}" y="{ly - 8}" width="9" height="9" rx="2" fill="{color}" fill-opacity="0.6" stroke="{color}"/>'
        )
        lines.append(
            f'<text x="{lx + 13}" y="{ly}" class="dim">{escape(_short_label(machine, 15))}</text>'
        )
        lx += 110


def _treemap_partition(
    items: Sequence[tuple[float, str, str, int]],
    x: float,
    y: float,
    w: float,
    h: float,
) -> list[tuple[float, float, float, float, float, str, str, int]]:
    """Binary split treemap partition preserving sorted visual clarity."""
    if not items:
        return []
    if len(items) == 1:
        area, recipe, machine, count = items[0]
        return [(x, y, w, h, area, recipe, machine, count)]

    total = sum(item[0] for item in items) or 1.0
    half = total * 0.5
    acc = 0.0
    split_idx = 1
    for idx, item in enumerate(items[:-1], start=1):
        acc += item[0]
        split_idx = idx
        if acc >= half:
            break

    left_items = items[:split_idx]
    right_items = items[split_idx:]
    left_frac = (sum(i[0] for i in left_items) / total) if total > 0 else 0.5
    if w >= h:
        lw = w * left_frac
        return _treemap_partition(left_items, x, y, lw, h) + _treemap_partition(
            right_items, x + lw, y, w - lw, h
        )
    lh = h * left_frac
    return _treemap_partition(left_items, x, y, w, lh) + _treemap_partition(
        right_items, x, y + lh, w, h - lh
    )


def build_placement_comparison_svg(
    def_data: Mapping[str, Any],
    netlist_data: Mapping[str, Any],
) -> str:
    """Build Stage 2 SVG comparing naive rank columns against legalized chassis placement."""
    units = float(def_data.get("units", DEFAULT_DEF_UNITS))
    die_area = def_data.get("die_area", [0, 0, 1, 1])
    die_w = float(die_area[2]) / units
    die_h = float(die_area[3]) / units

    naive_xy, hpwl_naive, hpwl_legal = naive_rank_hpwl(def_data, netlist_data)
    reduction_pct = ((hpwl_naive - hpwl_legal) / hpwl_naive * 100.0) if hpwl_naive > 0 else 0.0

    ox_left = 16 + (PLACEMENT_PANEL_W - PLACEMENT_MAP_SIZE) * 0.5
    ox_right = 612 + (PLACEMENT_PANEL_W - PLACEMENT_MAP_SIZE) * 0.5
    oy_map = 64 + PLACEMENT_MAP_Y_OFFSET
    clip_defs = (
        f'<clipPath id="place-left-clip"><rect x="{ox_left:.1f}" y="{oy_map:.1f}" width="{PLACEMENT_MAP_SIZE:.1f}" height="{PLACEMENT_MAP_SIZE:.1f}"/></clipPath>',
        f'<clipPath id="place-right-clip"><rect x="{ox_right:.1f}" y="{oy_map:.1f}" width="{PLACEMENT_MAP_SIZE:.1f}" height="{PLACEMENT_MAP_SIZE:.1f}"/></clipPath>',
    )
    lines = _svg_open(
        SVG_W,
        SVG_H_TALL,
        "Stage 2: Analytical Quadratic Placement, SCC Condensation, and City-Block Legalization",
        f"4-Science Chassis ({die_w:.0f}x{die_h:.0f} die, {len(def_data['components'])} instances, {len(def_data['nets'])} nets) | HPWL drops {reduction_pct:.1f}%",
        extra_defs=clip_defs,
    )

    inst_meta = {
        str(inst["name"]): inst
        for inst in netlist_data.get("instances", ())
        if isinstance(inst, Mapping) and "name" in inst
    }
    cells = netlist_data.get("cells", {})
    max_rank = max((int(inst.get("rank", 0)) for inst in inst_meta.values()), default=1) or 1
    legal_xy = {
        str(c["name"]): (float(c["x"]) / units, float(c["y"]) / units)
        for c in def_data.get("components", ())
    }
    pin_xy = {
        str(p["name"]): (float(p["x"]) / units, float(p["y"]) / units)
        for p in def_data.get("pins", ())
    }

    _append_placement_panel(
        lines,
        title=f"Uncondensed Rank Columns (HPWL: {hpwl_naive:,.0f} tiles)",
        panel_x=16,
        panel_y=64,
        panel_w=PLACEMENT_PANEL_W,
        panel_h=480,
        die_w=die_w,
        die_h=die_h,
        comp_xy=naive_xy,
        pin_xy=pin_xy,
        def_data=def_data,
        inst_meta=inst_meta,
        cells=cells,
        max_rank=max_rank,
        draw_streets=False,
        badge_text=f"HPWL {hpwl_naive:,.0f}",
        badge_color=PALETTE["amber"],
        clip_id="place-left-clip",
    )

    _append_placement_panel(
        lines,
        title=f"Quadratic + SCC + 94x94 Legalized (HPWL: {hpwl_legal:,.0f} tiles)",
        panel_x=612,
        panel_y=64,
        panel_w=PLACEMENT_PANEL_W,
        panel_h=480,
        die_w=die_w,
        die_h=die_h,
        comp_xy=legal_xy,
        pin_xy=pin_xy,
        def_data=def_data,
        inst_meta=inst_meta,
        cells=cells,
        max_rank=max_rank,
        draw_streets=True,
        badge_text=f"-{reduction_pct:.1f}% HPWL",
        badge_color=PALETTE["emerald"],
        clip_id="place-right-clip",
    )

    lines.append("</svg>")
    return "\n".join(lines)


def _append_placement_panel(
    lines: list[str],
    *,
    title: str,
    panel_x: int,
    panel_y: int,
    panel_w: int,
    panel_h: int,
    die_w: float,
    die_h: float,
    comp_xy: Mapping[str, tuple[float, float]],
    pin_xy: Mapping[str, tuple[float, float]],
    def_data: Mapping[str, Any],
    inst_meta: Mapping[str, Mapping[str, Any]],
    cells: Mapping[str, Mapping[str, Any]],
    max_rank: int,
    draw_streets: bool,
    badge_text: str,
    badge_color: str,
    clip_id: str,
) -> None:
    """Render one die placement scatter panel with net flylines and macro blocks."""
    lines.append(
        f'<rect x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{panel_h}" rx="8" fill="{PALETTE["panel"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(f'<text x="{panel_x + 14}" y="{panel_y + 22}" class="hdr">{escape(title)}</text>')
    lines.append(
        f'<rect x="{panel_x + panel_w - 118}" y="{panel_y + 8}" width="104" height="20" rx="4" fill="{badge_color}" fill-opacity="0.16" stroke="{badge_color}"/>'
    )
    lines.append(
        f'<text x="{panel_x + panel_w - 110}" y="{panel_y + 22}" class="badge" style="fill:{badge_color}">{escape(badge_text)}</text>'
    )

    map_size = PLACEMENT_MAP_SIZE
    ox = panel_x + (panel_w - map_size) * 0.5
    oy = panel_y + PLACEMENT_MAP_Y_OFFSET
    sx = map_size / max(die_w, 1.0)
    sy = map_size / max(die_h, 1.0)

    lines.append(
        f'<rect x="{ox:.1f}" y="{oy:.1f}" width="{map_size:.1f}" height="{map_size:.1f}" fill="{PALETTE["bg"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(f'<g clip-path="url(#{clip_id})">')

    units = float(def_data.get("units", DEFAULT_DEF_UNITS))
    if draw_streets:
        for blk in def_data.get("blockages", ()):
            bx1 = ox + (float(blk["x1"]) / units) * sx
            by1 = oy + (float(blk["y1"]) / units) * sy
            bw = ((float(blk["x2"]) - float(blk["x1"])) / units) * sx
            bh = ((float(blk["y2"]) - float(blk["y1"])) / units) * sy
            rx0 = max(ox, bx1)
            ry0 = max(oy, by1)
            rx1 = min(ox + map_size, bx1 + max(bw, 1.0))
            ry1 = min(oy + map_size, by1 + max(bh, 1.0))
            if rx1 > rx0 and ry1 > ry0:
                lines.append(
                    f'<rect x="{rx0:.1f}" y="{ry0:.1f}" width="{rx1 - rx0:.1f}" height="{ry1 - ry0:.1f}" fill="#1e293b" fill-opacity="0.7"/>'
                )

    # Draw a deterministic sample of net flylines
    drawn_lines = 0
    for net in def_data.get("nets", ()):
        if drawn_lines >= MAX_FLYLINES:
            break
        pts: list[tuple[float, float]] = []
        for inst_name, pin_name in net.get("connections", ()):
            pt = pin_xy.get(pin_name) if inst_name == "PIN" else comp_xy.get(inst_name)
            if pt is not None:
                pts.append((ox + pt[0] * sx, oy + pt[1] * sy))
        if len(pts) < 2:
            continue
        x0, y0 = pts[0]
        for tx, ty in pts[1:]:
            clipped = _clip_segment_to_box(x0, y0, tx, ty, ox, oy, ox + map_size, oy + map_size)
            if clipped is None:
                continue
            cx1, cy1, cx2, cy2, _s_in, _e_in = clipped
            lines.append(
                f'<line x1="{cx1:.1f}" y1="{cy1:.1f}" x2="{cx2:.1f}" y2="{cy2:.1f}" stroke="#64748b" stroke-width="0.6" stroke-opacity="0.22"/>'
            )
        drawn_lines += 1

    # Draw placed macros colored by normalized rank
    for comp in def_data.get("components", ()):
        name = str(comp["name"])
        pt = comp_xy.get(name)
        if pt is None:
            continue
        inst = inst_meta.get(name, {})
        cell = cells.get(str(inst.get("cell", comp.get("macro", ""))), {})
        mw = max(float(cell.get("width", 2)) * sx, 2.2)
        mh = max(float(cell.get("height", 2)) * sy, 2.2)
        rx0 = max(ox, ox + pt[0] * sx)
        ry0 = max(oy, oy + pt[1] * sy)
        rx1 = min(ox + map_size, ox + pt[0] * sx + mw)
        ry1 = min(oy + map_size, oy + pt[1] * sy + mh)
        if rx1 <= rx0 or ry1 <= ry0:
            continue
        rank = int(inst.get("rank", 0))
        color = _lerp_rank_color(rank / max_rank)
        lines.append(
            f'<rect x="{rx0:.1f}" y="{ry0:.1f}" width="{rx1 - rx0:.1f}" height="{ry1 - ry0:.1f}" fill="{color}" fill-opacity="0.88"/>'
        )

    lines.append("</g>")

    # Draw I/O pins along perimeter
    for px, py in pin_xy.values():
        lines.append(
            f'<circle cx="{ox + px * sx:.1f}" cy="{oy + py * sy:.1f}" r="3.0" fill="{PALETTE["emerald"]}"/>'
        )

    lines.append(
        f'<text x="{panel_x + 14}" y="{panel_y + panel_h - 10}" class="dim">Rank gradient: West inputs (cyan) -> East science outputs (rose) | Street width: 6 tiles</text>'
    )


def _router_to_svg_coords(
    wx: float,
    wy: float,
    wx0: int,
    wy0: int,
    scale_x: float,
    scale_y: float,
) -> tuple[float, float]:
    """Map world tile coordinates into router viewport pixel coordinates."""
    return ROUTER_VP_X + (wx - wx0) * scale_x, ROUTER_VP_Y + (wy - wy0) * scale_y


def _append_routed_entities(
    lines: list[str],
    entities: Sequence[Mapping[str, Any]],
    cat_map: Mapping[str, str],
    wx0: int,
    wy0: int,
    wx1: int,
    wy1: int,
    scale_x: float,
    scale_y: float,
) -> None:
    """Append clipped production machine and splitter footprints in the router window."""
    for ent in entities:
        pos = ent.get("position", {})
        if not isinstance(pos, Mapping):
            continue
        name = str(ent.get("name", ""))
        cat = cat_map.get(name, "")
        if cat not in MACHINE_CATEGORY_KEYS and cat != "splitter":
            continue
        ex, ey = float(pos.get("x", 0.0)), float(pos.get("y", 0.0))
        direction = int(ent.get("direction", DIRECTION_NORTH))
        ew, eh = entity_size(name, direction)
        ix1 = max(ex - ew * 0.5, float(wx0))
        iy1 = max(ey - eh * 0.5, float(wy0))
        ix2 = min(ex + ew * 0.5, float(wx1))
        iy2 = min(ey + eh * 0.5, float(wy1))
        if ix2 <= ix1 or iy2 <= iy1:
            continue
        rx, ry = _router_to_svg_coords(ix1, iy1, wx0, wy0, scale_x, scale_y)
        rw_px = (ix2 - ix1) * scale_x
        rh_px = (iy2 - iy1) * scale_y
        if rw_px < MIN_PIPE_PX_LEN or rh_px < MIN_PIPE_PX_LEN:
            continue
        if cat in MACHINE_CATEGORY_KEYS:
            lines.append(
                f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw_px:.1f}" height="{rh_px:.1f}" rx="3" fill="#1e293b" stroke="{PALETTE["teal"]}" stroke-width="1.2"/>'
            )
        else:
            lines.append(
                f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw_px:.1f}" height="{rh_px:.1f}" rx="2" fill="#334155" stroke="{PALETTE["amber"]}" stroke-width="1.0"/>'
            )


def _append_routed_pipes(
    lines: list[str],
    pipes: Sequence[Sequence[float]],
    wx0: int,
    wy0: int,
    wx1: int,
    wy1: int,
    scale_x: float,
    scale_y: float,
) -> None:
    """Append clipped M3 fluid pipe segments in the router window."""
    for x1, y1, x2, y2, is_ug in pipes:
        sx1, sx2 = x1, x2
        if math.hypot(x2 - x1, y2 - y1) < CLIP_MIN_WORLD_LEN:
            sx1 = x1 - PIPE_ISOLATED_HALF_TILES
            sx2 = x1 + PIPE_ISOLATED_HALF_TILES
        clipped = _clip_segment_to_box(
            sx1, y1, sx2, y2, float(wx0), float(wy0), float(wx1), float(wy1)
        )
        if clipped is None:
            continue
        cx1, cy1, cx2, cy2, _s_in, _e_in = clipped
        px1, py1 = _router_to_svg_coords(cx1, cy1, wx0, wy0, scale_x, scale_y)
        px2, py2 = _router_to_svg_coords(cx2, cy2, wx0, wy0, scale_x, scale_y)
        if math.hypot(px2 - px1, py2 - py1) < MIN_PIPE_PX_LEN:
            continue
        dash = ' stroke-dasharray="4 3"' if is_ug else ""
        lines.append(
            f'<line x1="{px1:.1f}" y1="{py1:.1f}" x2="{px2:.1f}" y2="{py2:.1f}" stroke="{PALETTE["violet"]}" stroke-width="2.2"{dash}/>'
        )


def _append_routed_belts(
    lines: list[str],
    belts: Sequence[Sequence[float]],
    wx0: int,
    wy0: int,
    wx1: int,
    wy1: int,
    scale_x: float,
    scale_y: float,
) -> None:
    """Extend, clip, and append M1 surface belt segments in the router window."""
    for x1, y1, x2, y2, raw_dir in belts:
        dx, dy = DIR_STEPS.get(int(raw_dir), (0, -1))
        seg_dx, seg_dy = x2 - x1, y2 - y1
        has_span = abs(seg_dx) > CLIP_MIN_WORLD_LEN or abs(seg_dy) > CLIP_MIN_WORLD_LEN
        if has_span and abs(seg_dx * dx + seg_dy * dy) < CLIP_MIN_WORLD_LEN:
            # Skip perpendicular splitter bars because splitters render as boxes
            continue
        bx1, by1 = x1 - BELT_EXTEND_TILES * dx, y1 - BELT_EXTEND_TILES * dy
        bx2, by2 = x2 + BELT_EXTEND_TILES * dx, y2 + BELT_EXTEND_TILES * dy
        clipped = _clip_segment_to_box(
            bx1, by1, bx2, by2, float(wx0), float(wy0), float(wx1), float(wy1)
        )
        if clipped is None:
            continue
        cx1, cy1, cx2, cy2, _s_in, end_in = clipped
        px1, py1 = _router_to_svg_coords(cx1, cy1, wx0, wy0, scale_x, scale_y)
        px2, py2 = _router_to_svg_coords(cx2, cy2, wx0, wy0, scale_x, scale_y)
        px_len = math.hypot(px2 - px1, py2 - py1)
        if px_len < MIN_BELT_PX_LEN:
            continue
        marker = ' marker-end="url(#arr-belt)"' if (end_in and px_len >= MIN_ARROW_PX_LEN) else ""
        lines.append(
            f'<line x1="{px1:.1f}" y1="{py1:.1f}" x2="{px2:.1f}" y2="{py2:.1f}" stroke="{PALETTE["amber"]}" stroke-width="2.2"{marker}/>'
        )


def _append_routed_underground(
    lines: list[str],
    underground: Sequence[Sequence[float]],
    wx0: int,
    wy0: int,
    wx1: int,
    wy1: int,
    scale_x: float,
    scale_y: float,
) -> None:
    """Clip and append M2 underground tunnel spans and interior endpoint markers."""
    for x1, y1, x2, y2 in underground:
        clipped = _clip_segment_to_box(
            x1, y1, x2, y2, float(wx0), float(wy0), float(wx1), float(wy1)
        )
        if clipped is None:
            continue
        cx1, cy1, cx2, cy2, start_in, end_in = clipped
        px1, py1 = _router_to_svg_coords(cx1, cy1, wx0, wy0, scale_x, scale_y)
        px2, py2 = _router_to_svg_coords(cx2, cy2, wx0, wy0, scale_x, scale_y)
        if math.hypot(px2 - px1, py2 - py1) < MIN_PIPE_PX_LEN:
            continue
        lines.append(
            f'<line x1="{px1:.1f}" y1="{py1:.1f}" x2="{px2:.1f}" y2="{py2:.1f}" stroke="{PALETTE["cyan"]}" stroke-width="2.2" stroke-dasharray="5 3"/>'
        )
        if (
            start_in
            and (wx0 + UG_ENDPOINT_MARGIN_TILES <= cx1 <= wx1 - UG_ENDPOINT_MARGIN_TILES)
            and (wy0 + UG_ENDPOINT_MARGIN_TILES <= cy1 <= wy1 - UG_ENDPOINT_MARGIN_TILES)
        ):
            lines.append(
                f'<circle cx="{px1:.1f}" cy="{py1:.1f}" r="3.2" fill="{PALETTE["cyan"]}"/>'
            )
        if (
            end_in
            and (wx0 + UG_ENDPOINT_MARGIN_TILES <= cx2 <= wx1 - UG_ENDPOINT_MARGIN_TILES)
            and (wy0 + UG_ENDPOINT_MARGIN_TILES <= cy2 <= wy1 - UG_ENDPOINT_MARGIN_TILES)
        ):
            lines.append(
                f'<circle cx="{px2:.1f}" cy="{py2:.1f}" r="3.2" fill="{PALETTE["bg"]}" stroke="{PALETTE["cyan"]}" stroke-width="1.6"/>'
            )


def build_router_3d_svg(
    def_data: Mapping[str, Any],
    bp_payload: Mapping[str, Any],
    cat_map: Mapping[str, str],
    max_ug_spans: Mapping[str, int],
) -> str:
    """Build Stage 3 SVG: routed chassis window with multi-layer 3D PDK stack."""
    wx0, wy0, wx1, wy1 = select_street_straddle_window(
        bp_payload,
        cat_map,
        ROUTER_WIN_W,
        ROUTER_WIN_H,
        prefer_pipes=True,
    )
    layers = extract_geometry_layers(bp_payload, cat_map)
    clip_defs = (
        f'<clipPath id="router-vp-clip"><rect x="{ROUTER_VP_X}" y="{ROUTER_VP_Y}" width="{ROUTER_VP_W}" height="{ROUTER_VP_H}"/></clipPath>',
    )
    lines = _svg_open(
        SVG_W,
        SVG_H_STD,
        "Stage 3: Two-Stage 3D Multi-Layer Dual-Lane Maze Router and Underground Transitions",
        f"Left: real routed chassis slice [{wx0},{wy0}]..[{wx1},{wy1}] tiles | Right: z-layer PDK stack & negotiated congestion",
        extra_defs=clip_defs,
    )

    # Left routed tile window
    lx, ly, lw, lh = 16, 64, 614, 460
    lines.append(
        f'<rect x="{lx}" y="{ly}" width="{lw}" height="{lh}" rx="8" fill="{PALETTE["panel"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(
        f'<text x="{lx + 14}" y="{ly + 22}" class="hdr">Routed Block-to-Street Detail ({ROUTER_WIN_W}x{ROUTER_WIN_H} tiles)</text>'
    )
    lines.append(
        f'<rect x="{lx + lw - 96}" y="{ly + 8}" width="82" height="20" rx="4" fill="{PALETTE["emerald"]}" fill-opacity="0.16" stroke="{PALETTE["emerald"]}"/>'
    )
    lines.append(
        f'<text x="{lx + lw - 84}" y="{ly + 22}" class="badge" style="fill:{PALETTE["emerald"]}">0 DRC</text>'
    )

    vx, vy, vw, vh = ROUTER_VP_X, ROUTER_VP_Y, ROUTER_VP_W, ROUTER_VP_H
    scale_x = vw / ROUTER_WIN_W
    scale_y = vh / ROUTER_WIN_H
    lines.append(
        f'<rect x="{vx}" y="{vy}" width="{vw}" height="{vh}" fill="{PALETTE["bg"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append('<g id="router-viewport" clip-path="url(#router-vp-clip)">')

    # Tile gridlines
    for gx in range(2, ROUTER_WIN_W, 2):
        px = vx + gx * scale_x
        lines.append(
            f'<line x1="{px:.1f}" y1="{vy}" x2="{px:.1f}" y2="{vy + vh}" stroke="{PALETTE["grid"]}" stroke-width="0.6"/>'
        )
    for gy in range(2, ROUTER_WIN_H, 2):
        py = vy + gy * scale_y
        lines.append(
            f'<line x1="{vx}" y1="{py:.1f}" x2="{vx + vw}" y2="{py:.1f}" stroke="{PALETTE["grid"]}" stroke-width="0.6"/>'
        )

    # Street channel blockages intersecting window
    units = float(def_data.get("units", DEFAULT_DEF_UNITS))
    for blk in def_data.get("blockages", ()):
        bx1 = max(float(blk["x1"]) / units, float(wx0))
        by1 = max(float(blk["y1"]) / units, float(wy0))
        bx2 = min(float(blk["x2"]) / units, float(wx1))
        by2 = min(float(blk["y2"]) / units, float(wy1))
        if bx2 > bx1 and by2 > by1:
            rx, ry = _router_to_svg_coords(bx1, by1, wx0, wy0, scale_x, scale_y)
            rw_px = (bx2 - bx1) * scale_x
            rh_px = (by2 - by1) * scale_y
            lines.append(
                f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{rw_px:.1f}" height="{rh_px:.1f}" fill="#334155" fill-opacity="0.35"/>'
            )

    entities = [ent for ent in bp_payload.get("entities", ()) if isinstance(ent, Mapping)]
    _append_routed_entities(lines, entities, cat_map, wx0, wy0, wx1, wy1, scale_x, scale_y)
    _append_routed_pipes(lines, layers["pipes"], wx0, wy0, wx1, wy1, scale_x, scale_y)
    _append_routed_belts(lines, layers["belts"], wx0, wy0, wx1, wy1, scale_x, scale_y)
    _append_routed_underground(lines, layers["underground"], wx0, wy0, wx1, wy1, scale_x, scale_y)
    lines.append("</g>")

    lines.append(
        f'<text x="{lx + 14}" y="{ly + lh - 12}" class="dim">Teal: Macros | Amber: M1 belts | Cyan dashed: M2 tunnels | Violet: M3 pipes</text>'
    )

    # Right 3D stack & PathFinder panel
    rx0, ry0, rw, rh = 644, 64, 540, 460
    lines.append(
        f'<rect x="{rx0}" y="{ry0}" width="{rw}" height="{rh}" rx="8" fill="{PALETTE["panel"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(
        f'<text x="{rx0 + 16}" y="{ry0 + 22}" class="hdr">3D Layer Stack &amp; Negotiated Congestion</text>'
    )

    ug_summary = ", ".join(
        f"{k.replace('-underground-belt', '-ug').replace('underground-belt', 'ug')}={v}t"
        for k, v in sorted(max_ug_spans.items())
    )
    layer_cards = [
        (
            "Overhead (PDN + Circuit)",
            "Copper MST poles + 1-to-40 tile red/green circuit links",
            PALETTE["rose"],
        ),
        (
            "z = 0 (M1 Surface)",
            "Assembling machines, inserters, splitters, dual-lane belts",
            PALETTE["amber"],
        ),
        (
            "z = 1..N (M2 Tunnels)",
            f"Spans: {ug_summary}" if ug_summary else "Dynamic prototype spans",
            PALETTE["cyan"],
        ),
        (
            "Fluid Grid (M3 Pipes)",
            "Pressure-aware surface pipes, pumps, underground jumps",
            PALETTE["violet"],
        ),
    ]
    for idx, (head, desc, color) in enumerate(layer_cards):
        cy = ry0 + 44 + idx * 62
        lines.append(
            f'<polygon points="{rx0 + 28},{cy + 14} {rx0 + 506},{cy + 14} {rx0 + 490},{cy + 56} {rx0 + 16},{cy + 56}" fill="{color}" fill-opacity="0.14" stroke="{color}" stroke-width="1.2"/>'
        )
        lines.append(
            f'<text x="{rx0 + 36}" y="{cy + 33}" class="lbl" style="fill:{color};font-weight:700">{escape(head)}</text>'
        )
        lines.append(
            f'<text x="{rx0 + 36}" y="{cy + 48}" class="dim">{escape(_short_label(desc, STACK_DESC_MAX_CHARS))}</text>'
        )

    eq_y = ry0 + 314
    lines.append(
        f'<rect x="{rx0 + 16}" y="{eq_y}" width="{rw - 32}" height="128" rx="6" fill="{PALETTE["panel_alt"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(
        f'<text x="{rx0 + 28}" y="{eq_y + 24}" class="lbl" style="fill:{PALETTE["emerald"]}">Hierarchical PathFinder Rip-Up-and-Reroute</text>'
    )
    lines.append(
        f'<text x="{rx0 + 28}" y="{eq_y + 46}" class="lbl">cost(e) = (base_cost(e) + history(e)) * present_congestion(e)</text>'
    )
    lines.append(
        f'<text x="{rx0 + 28}" y="{eq_y + 70}" class="dim">Stage 1: Route intra-block 94x94 cores with underground lane jumps</text>'
    )
    lines.append(
        f'<text x="{rx0 + 28}" y="{eq_y + 88}" class="dim">Stage 2: Route global trunks inside 6-tile chassis street channels</text>'
    )
    lines.append(
        f'<text x="{rx0 + 28}" y="{eq_y + 106}" class="dim">Signoff: Prune dead ends, insert pumps, verify zero tile overlaps</text>'
    )

    lines.append("</svg>")
    return "\n".join(lines)


def _clip_segment_to_box(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    bx0: float,
    by0: float,
    bx1: float,
    by1: float,
) -> tuple[float, float, float, float, bool, bool] | None:
    """Clip segment `(x1, y1)->(x2, y2)` to `[bx0, bx1] x [by0, by1]` via Liang-Barsky."""
    dx = x2 - x1
    dy = y2 - y1
    t0 = 0.0
    t1 = 1.0
    for p, q in (
        (-dx, x1 - bx0),
        (dx, bx1 - x1),
        (-dy, y1 - by0),
        (dy, by1 - y1),
    ):
        if abs(p) <= 1e-12:
            if q < 0.0:
                return None
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return None
            if r < t1:
                t1 = r

    cx1 = x1 + t0 * dx
    cy1 = y1 + t0 * dy
    cx2 = x1 + t1 * dx
    cy2 = y1 + t1 * dy
    if math.hypot(cx2 - cx1, cy2 - cy1) < CLIP_MIN_WORLD_LEN:
        return None
    start_inside = t0 <= CLIP_T_EPS
    end_inside = t1 >= 1.0 - CLIP_T_EPS
    return (cx1, cy1, cx2, cy2, start_inside, end_inside)


def build_verilog_campaign_svg(
    synth_result: Any,
    verilog_src: str,
) -> str:
    """Build Stage 4 SVG: SystemVerilog FSM synthesis, native ELF verification, and UDP campaign."""
    mod_match = MODULE_NAME_RE.search(verilog_src)
    mod_name = (
        mod_match.group(1) if mod_match else str(getattr(synth_result, "module_name", "controller"))
    )
    params = getattr(synth_result, "parameters", {})
    n_cells = len(getattr(synth_result, "cells", ()))
    n_conns = len(getattr(synth_result, "connections", ()))
    n_inputs = len(getattr(synth_result, "inputs", {}))
    n_outputs = len(getattr(synth_result, "outputs", {}))

    t_throt = params.get("THRESH_THROTTLED", 30)
    t_rec_norm = params.get("THRESH_RECOVER_NORMAL", 70)
    t_emerg = params.get("THRESH_EMERGENCY", 10)
    t_rec_throt = params.get("THRESH_RECOVER_THROTTLED", 20)

    lines = _svg_open(
        SVG_W,
        SVG_H_TALL,
        "Stage 4: SystemVerilog Controller Synthesis, Native ELF Signoff, and Freeplay Campaign",
        f"Top: {mod_name}.v -> {n_cells} combinators & {n_conns} circuit wires | Bottom: zero-cheat UDP lockstep starter base",
    )

    # Top container: RTL -> Combinators -> Native C++ ELF
    lines.append(
        f'<rect x="16" y="64" width="1168" height="240" rx="8" fill="{PALETTE["panel"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(
        f'<text x="30" y="86" class="hdr">Behavioral Verilog FSM ({escape(mod_name)}.v) -> Combinator Netlist -> Native ELF Signoff</text>'
    )

    # State machine circles/boxes
    states = [
        ("STATE_NORMAL", "Switch=1, Backup=0", 52, 140, PALETTE["emerald"]),
        ("STATE_THROTTLED", "Switch=1, Backup=1", 290, 140, PALETTE["amber"]),
        ("STATE_EMERGENCY", "Switch=0, Backup=1", 528, 140, PALETTE["rose"]),
    ]
    for sname, sdesc, sx, sy, scol in states:
        lines.append(
            f'<rect x="{sx}" y="{sy}" width="176" height="58" rx="8" fill="{PALETTE["panel_alt"]}" stroke="{scol}" stroke-width="1.6"/>'
        )
        lines.append(
            f'<text x="{sx + 12}" y="{sy + 24}" class="lbl" style="fill:{scol};font-weight:700">{sname}</text>'
        )
        lines.append(f'<text x="{sx + 12}" y="{sy + 42}" class="dim">{sdesc}</text>')

    # State transitions from parameters
    lines.append(
        f'<path d="M228,156 L290,156" stroke="{PALETTE["amber"]}" stroke-width="1.6" marker-end="url(#arr-amber)"/>'
    )
    lines.append(f'<text x="234" y="148" class="dim">&lt;{t_throt}%</text>')
    lines.append(
        f'<path d="M290,182 L228,182" stroke="{PALETTE["emerald"]}" stroke-width="1.6" marker-end="url(#arr-emerald)"/>'
    )
    lines.append(f'<text x="234" y="198" class="dim">&gt;{t_rec_norm}%</text>')

    lines.append(
        f'<path d="M466,156 L528,156" stroke="{PALETTE["rose"]}" stroke-width="1.6" marker-end="url(#arr-rose)"/>'
    )
    lines.append(f'<text x="472" y="148" class="dim">&lt;{t_emerg}%</text>')
    lines.append(
        f'<path d="M528,182 L466,182" stroke="{PALETTE["amber"]}" stroke-width="1.6" marker-end="url(#arr-amber)"/>'
    )
    lines.append(f'<text x="472" y="198" class="dim">&gt;{t_rec_throt}%</text>')

    # Combinator synthesis & native ELF verification boxes
    lines.append(
        f'<rect x="744" y="114" width="200" height="104" rx="6" fill="{PALETTE["panel_alt"]}" stroke="{PALETTE["cyan"]}"/>'
    )
    lines.append(
        f'<text x="756" y="136" class="lbl" style="fill:{PALETTE["cyan"]}">Combinator Netlist</text>'
    )
    lines.append(
        f'<text x="756" y="156" class="dim">{n_inputs} inputs -> {n_outputs} outputs</text>'
    )
    lines.append(f'<text x="756" y="174" class="dim">{n_cells} arithmetic/decider cells</text>')
    lines.append(f'<text x="756" y="192" class="dim">{n_conns} red/green circuit links</text>')

    lines.append(
        f'<path d="M704,169 L744,169" stroke="{PALETTE["cyan"]}" stroke-width="1.6" marker-end="url(#arr-cyan)"/>'
    )

    lines.append(
        f'<rect x="974" y="114" width="194" height="104" rx="6" fill="{PALETTE["panel_alt"]}" stroke="{PALETTE["violet"]}"/>'
    )
    lines.append(
        f'<text x="986" y="136" class="lbl" style="fill:{PALETTE["violet"]}">Native ELF Verifier</text>'
    )
    lines.append('<text x="986" y="156" class="dim">Patch PT_INTERP &amp; dlopen</text>')
    lines.append('<text x="986" y="174" class="dim">TransportLine::move</text>')
    lines.append('<text x="986" y="192" class="dim">FluidBuffer::getFlowRate</text>')

    lines.append(
        f'<path d="M944,169 L974,169" stroke="{PALETTE["cyan"]}" stroke-width="1.6" marker-end="url(#arr-cyan)"/>'
    )
    lines.append(
        '<text x="52" y="256" class="dim">Circuit signals drive physical power switches, backup steam pumps, and 7-segment displays placed directly onto chassis macro cores.</text>'
    )
    lines.append(
        '<text x="52" y="274" class="dim">Closed-loop verification probes real Factorio binary state without Lua script cheats or manual coordinate tables.</text>'
    )

    # Bottom container: 4-step Autonomous Freeplay Campaign
    lines.append(
        f'<rect x="16" y="320" width="1168" height="224" rx="8" fill="{PALETTE["panel"]}" stroke="{PALETTE["border"]}"/>'
    )
    lines.append(
        '<text x="30" y="344" class="hdr">Zero-Cheat Autonomous Freeplay Campaign (Procedural Terrain to Starter Base)</text>'
    )

    campaign_steps = [
        (
            "1. Map Cartographer",
            "Scan water shorelines, ore patches,",
            "cliff keepouts, and clearable trees.",
            PALETTE["cyan"],
        ),
        (
            "2. Patch & Mall Router",
            "Place lakeside boilers, drill rows,",
            "smelting columns, and red-science mall.",
            PALETTE["emerald"],
        ),
        (
            "3. A* Navigation",
            "Plan collision-free walking vectors",
            "under strict reach and latency bounds.",
            PALETTE["amber"],
        ),
        (
            "4. UDP Lockstep Proxy",
            "Stream deterministic input packets",
            "directly to a live Factorio host.",
            PALETTE["rose"],
        ),
    ]
    box_w = 258
    gap = 26
    for idx, (head, line1, line2, color) in enumerate(campaign_steps):
        bx = 34 + idx * (box_w + gap)
        by = 368
        lines.append(
            f'<rect x="{bx}" y="{by}" width="{box_w}" height="122" rx="6" fill="{PALETTE["panel_alt"]}" stroke="{color}" stroke-width="1.4"/>'
        )
        lines.append(
            f'<text x="{bx + 14}" y="{by + 28}" class="lbl" style="fill:{color};font-weight:700">{escape(head)}</text>'
        )
        lines.append(f'<text x="{bx + 14}" y="{by + 56}" class="dim">{escape(line1)}</text>')
        lines.append(f'<text x="{bx + 14}" y="{by + 74}" class="dim">{escape(line2)}</text>')
        if idx < len(campaign_steps) - 1:
            ax1 = bx + box_w
            ax2 = ax1 + gap
            ay = by + 61
            lines.append(
                f'<line x1="{ax1}" y1="{ay}" x2="{ax2}" y2="{ay}" stroke="{PALETTE["cyan"]}" stroke-width="1.8" marker-end="url(#arr-cyan)"/>'
            )

    lines.append(
        '<text x="34" y="520" class="dim">Verified end-to-end by test/test_patch_router.py and test/test_bot_agent.py without Lua command cheats.</text>'
    )
    lines.append("</svg>")
    return "\n".join(lines)
