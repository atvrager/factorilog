"""Analytical placement with router-aware directed legalization.

Computes continuous 2D coordinates from a directed quadratic wirelength system.
Legalizes cells onto the Factorio tile grid with router clearance halos.
"""

from __future__ import annotations

import collections
import copy
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias

import numpy as np

try:
    from foundry.chassis import CityBlockChassis
    from foundry.entity_helpers import entity_size
except ImportError:
    from chassis import CityBlockChassis
    from entity_helpers import entity_size

NetlistLike: TypeAlias = Any
JsonMap: TypeAlias = Mapping[str, Any]
MutableJsonMap: TypeAlias = dict[str, Any]
MergePlanList: TypeAlias = list[tuple[str, str, str, str]]
SplitPlanList: TypeAlias = list[tuple[str, str, str, str]]
BalancerPlanList: TypeAlias = list[tuple[str, str, str, str, str]]
MergePlansMap: TypeAlias = dict[str, MergePlanList]
SplitPlansMap: TypeAlias = dict[str, SplitPlanList]
BalancerPlansMap: TypeAlias = dict[str, BalancerPlanList]
BlockCoord: TypeAlias = tuple[int, int]
BlockAssignFn: TypeAlias = Callable[[str, BlockCoord], None]

_NATURAL_KEY_RE = re.compile(r"(\d+)")

# Numeric constants for placement, partitioning, and legalization
_EPS = 1e-6
_KL_MIN_DELTA = -1e-4
_PIN_X_TIE_WT = 0.005
_MIN_CORE_UTIL = 0.15
_EMPTY_LAB_X_WT = 0.2
_FWD_SINK_Y_WT = 0.25
_NON_MACH_DEMAND_SCALE = 0.35
_ATOMIC_CAP_FRAC = 0.35
_BAY_COUNT_PEN = 0.35
_SLICE_DEV_FRAC = 0.35
_REG_X = 0.35
_EMPTY_CENTER_X_WT = 0.4
_SAME_COL_RANK_PEN = 0.4
_HARMONIC_DEV_WT = 0.45
_SHARED_ATOMIC_FRAC = 0.45
_MAT_PORT_WT = 0.45
_DED_ATOMIC_FRAC = 0.48
_HALF = 0.5
_DEFAULT_MAX_CORE_UTIL = 0.50
_EMPTY_DEFAULT_X_FRAC = 0.55
_SCC_ATOMIC_FRAC = 0.55
_HARMONIC_NEIGHBOR_SHIFT = 0.6
_EMPTY_Y_WT = 0.6
_TARGET_BLOCK_CAP_FRAC = 0.63
_REG_Y = 0.65
_MULTI_BLOCK_CAP_FRAC = 0.72
_LAB_Y_CENTER_WT = 0.75
_FWD_DRV_Y_WT = 0.75
_HARMONIC_RANK_WT = 0.8
_MAX_CORE_UTIL = 0.85
_BELT_CAP_SAFETY = 0.94
_SLICE_CAP_HEADROOM = 1.10
_AREA_H_SCALE = 1.3
_RECIPE_CONTIG_WT = 1.4
_AREA_W_SCALE = 1.4
_PAIR_DY_WT = 1.45
_FLUID_EDGE_WT = 1.5
_ANCHOR_WEST_WT = 1.8
_HARMONIC_WEST_WT = 2.2
_BUCKET_EDGE_NORM = 2.4
_EAST_PIN_WT = 2.5
_BACKTRACK_X_PEN = 2.8
_MERGE2_FLOAT_DIM = 3.0
_WEST_RANK_CUTOFF = 3.0
_LAB_EAST_WT = 3.5
_BOUNDARY_ANCHOR_INSET = 4.0
_HARMONIC_EAST_WT = 4.5
_POWER_WEST_WT = 4.5
_LAB_EAST_ANCHOR_WT = 5.5
_LABS_PER_HALF_BLK_F = 6.0
_FLUID_WEST_WT = 6.0
_BOILERS_PER_BLK_F = 10.0
_EMPTY_EDGE_PEN = 10.0
_MIN_CONT_USABLE_DIM = 10.0
_MAX_COL_IDEAL_STEP = 16.0
_LAB_SAME_COL_PEN = 18.0
_NON_LAB_EAST_COL_PEN = 22.0
_OUT_PORT_AREA_WT = 40.0
_IN_PORT_AREA_WT = 45.0
_MIN_PADDED_AREA_SQ = 400.0
_SMALL_RECIPE_MIX_PEN = 600.0
_DEFAULT_BELT_CAP = 900.0
_CLUSTER_BIN_DIVISOR = 1400.0
_LARGE_RECIPE_MIX_PEN = 3000.0
_DOMAIN_MIX_PEN = 6000.0

_PAD_Y_DEFAULT = 3
_MERGE2_DIM = 3
_CENTER_HOLE_GRID = 3
_MERGE_ORDER = 3
_SWITCHED_MIN_DEPTH = 3
_KL_PASS_MULT = 3
_PIN_SAME_SINK_GAP = 3
_STAGGER_MOD = 3
_DEFAULT_MACRO_DIM = 3
_EXTRA_CH_DIV = 3
_BAY_ROW_Y_MOD = 3
_ROUND_3 = 3

_PAD_X_DEFAULT = 4
_COMB_ORDER = 4
_MAX_BOILER_FIRST_COL = 4
_TREE_GAP = 4
_CORE_INSET = 4
_MIN_PIN_GAP = 4
_CUT_SNAP_TOL = 4
_BAY_SLOTS_4_LEN = 4
_ROUND_4 = 4

_MIN_SPLIT_PAD_X = 5
_DISP_ORDER = 5
_LARGE_GRID_W = 5
_MIN_OUTPUTS_FOR_POWER_BLK = 5
_LEGALIZE_MARGIN_X = 5
_MIN_COL_GAP = 5
_PIN_BOUNDARY_TOL = 5
_BAY_ROW_X_STEP = 5
_BAY_BASE_Y = 5

_SPLIT_PAD_X_DEFAULT = 6
_LABS_PER_HALF_BLK = 6
_BAY_CAP = 6
_CUT_ORIG_TOL = 6
_MACH_V_GAP = 6
_LEGALIZE_PASSES = 6
_PIN_Y_MARGIN = 6

_SEVEN_GRID_W = 7
_LARGE_CELL_H = 7
_BAY_DENSE_COLS = 7

_CORE_MARGIN_DEFAULT = 8
_SWITCHED_PAD = 8
_COL_TARGET_CAP = 8
_MAX_GRP_LEN = 8
_CONTINUOUS_Y_PASSES = 8
_MIN_BAND_H = 8
_NEXT_BOILER_MIN_GAP = 8
_BAY_ROW_Y_STEP = 8

_BOILER_MIN_GAP = 9
_BAY_ODD_Y = 11
_HUGE_GRID_W = 12
_MIN_PIN_HI_Y = 12
_CORE_H_DEDUCT = 14
_MAX_EXTRA_CH = 14
_BAY_CLEARANCE_Y = 14
_LARGE_CHUNK_SZ = 16
_MIN_USABLE_H = 16
_SINGLE_CORE_H_PAD = 16
_MIN_EFF_CORE_DIM = 20
_MAX_ORIG_SPLIT_SLICE = 24
_MIN_STAGGER_BAND_H = 24
_PIN_GAP_H_DEDUCT = 24
_FALLBACK_PLACED_EXTENT = 30
_HARMONIC_ITERS = 40
_MIN_SINGLE_CORE_H = 48
_MIN_SINGLE_CORE_W = 60
_DEFAULT_CHASSIS_CORE_DIM = 94
_HUGE_RECIPE_COUNT = 160
_MAX_SINGLE_CORE_H = 160
_PIN_EDGE_INSET_DBU = 250
_MAX_UNBOUNDED_CORE_W = 400
_PIN_HALF_TILE_DBU = 500
_DEFAULT_DBU = 1000

_BAY_SLOTS_3 = (18, 45, 72)
_BAY_SLOTS_4 = (16, 36, 56, 76)
_BAY_SLOTS_6 = (14, 28, 42, 56, 70, 84)
_BAY_SLOTS_7 = (12, 24, 36, 48, 60, 72, 84)

_COMBINATOR_PREFIXES = ("u_acc_", "u_comb_", "u_dec_", "u_latch_", "u_switch_", "u_params_")
_TOPO_TYPE_ORDER: dict[str, int] = {
    "split": 0,
    "balancer": 0,
    "pump": 1,
    "machine": 2,
    "merge": _MERGE_ORDER,
    "combinator": _COMB_ORDER,
    "display": _DISP_ORDER,
}


def _sanitize(name: str) -> str:
    """Convert item or machine name into a valid Verilog identifier."""
    return name.replace("-", "_").replace(".", "__")


def _natural_key(text: str) -> list[int | str]:
    """Return a sort key that orders embedded integers numerically."""
    parts = _NATURAL_KEY_RE.split(text)
    return [int(tok) if tok.isdigit() else tok.lower() for tok in parts]


@dataclass(slots=True)
class PlacerNode:
    """One placeable cell instance in the analytical placement graph."""

    name: str
    macro: str
    recipe: str
    width: int
    height: int
    rank: int = 0
    node_type: str = "machine"
    pad_w: int = _PAD_X_DEFAULT
    pad_e: int = _PAD_X_DEFAULT
    pad_n: int = _PAD_Y_DEFAULT
    pad_s: int = _PAD_Y_DEFAULT
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    flow_depth: int = 0
    col_index: int = 0
    assigned_block: tuple[int, int] = (0, 0)
    is_matrix_node: bool = False
    cont_x: float = 0.0
    cont_y: float = 0.0
    x: int = 0
    y: int = 0


@dataclass(slots=True)
class PlacerEdge:
    """One directed connection between two nodes or boundary anchors."""

    source: str
    sink: str
    wire: str
    weight: float = 1.0
    source_dy: float = 0.0
    sink_dy: float = 0.0
    is_fluid: bool = False


@dataclass(slots=True)
class AnalyticalPlacementResult:
    """Legalized coordinates, block assignments, and metrics from analytical placement."""

    coords: dict[str, tuple[int, int]]
    continuous_coords: dict[str, tuple[float, float]]
    block_assignments: dict[str, tuple[int, int]]
    die_width: int
    die_height: int
    core_bounds: tuple[int, int, int, int]
    grid_w: int
    grid_h: int
    hpwl: float
    rewired_ports: dict[str, dict[str, str]] = field(default_factory=dict)
    matrix_nodes: set[str] = field(default_factory=set)
    pin_coords: dict[str, tuple[int, int]] = field(default_factory=dict)


def _is_combinator_node(m_low: str, n_low: str, cell_meta: JsonMap | None) -> bool:
    """Return True if macro, name, or metadata marks a circuit combinator cell."""
    if "combinator" in m_low or "power_switch" in m_low:
        return True
    has_circuit_flag = bool(cell_meta and cell_meta.get("circuit_cell"))
    return has_circuit_flag or n_low.startswith(_COMBINATOR_PREFIXES)


def _classify_node_type(name: str, macro: str, cell_meta: JsonMap | None = None) -> str:
    """Return the functional class of an instance for halo and flow rules."""
    m_low = macro.lower()
    n_low = name.lower()
    if "split" in m_low or "split" in n_low:
        return "split"
    if "balancer" in m_low or "bal" in n_low:
        return "balancer"
    if "merge" in m_low or "merge" in n_low:
        return "merge"
    if "pump" in m_low:
        return "pump"
    if "display" in m_low or "disp" in n_low:
        return "display"
    if _is_combinator_node(m_low, n_low, cell_meta):
        return "combinator"
    return "machine"


def _decrement_gaps_to_fit(eff_gaps: list[int], excess: int) -> None:
    """Decrement gaps greater than one unit until excess span is removed."""
    while excess > 0 and any(g > 1 for g in eff_gaps):
        for idx, gap_val in enumerate(eff_gaps):
            if gap_val > 1 and excess > 0:
                eff_gaps[idx] -= 1
                excess -= 1


def _compress_1d_pav_gaps(gaps: list[int], heights: list[int], n: int, avail: int) -> list[int]:
    """Scale and clamp inter-cell vertical gaps to fit within available span."""
    total_h = sum(heights)
    eff_gaps = list(gaps[: max(0, n - 1)])
    total_g = sum(eff_gaps)
    if total_h + total_g <= avail or total_g <= 0:
        return eff_gaps
    spare = max(0, avail - total_h)
    scale = float(spare) / float(total_g)
    eff_gaps = [max(1, math.floor(g * scale)) for g in eff_gaps]
    _decrement_gaps_to_fit(eff_gaps, total_h + sum(eff_gaps) - avail)
    if total_h + sum(eff_gaps) > avail:
        return [0] * len(eff_gaps)
    return eff_gaps


def _pool_adjacent_violators(
    z_target: list[float],
    weights: list[float],
    z_min: float,
    z_max: float,
) -> list[float]:
    """Run Pool-Adjacent-Violators isotonic regression on shifted targets."""
    n = len(z_target)
    blocks: list[list[float | int]] = []
    for i in range(n):
        w = max(_EPS, float(weights[i]))
        blocks.append([w, w * z_target[i], i, i])
        while len(blocks) >= 2:
            b_last = blocks[-1]
            b_prev = blocks[-2]
            if float(b_prev[1]) / float(b_prev[0]) <= float(b_last[1]) / float(b_last[0]):
                break
            b_prev[0] = float(b_prev[0]) + float(b_last[0])
            b_prev[1] = float(b_prev[1]) + float(b_last[1])
            b_prev[-1] = int(b_last[-1])
            blocks.pop()
    z_opt = [0.0] * n
    for w_sum, wz_sum, s_idx, e_idx in blocks:
        val = min(z_max, max(z_min, float(wz_sum) / float(w_sum)))
        for idx in range(int(s_idx), int(e_idx) + 1):
            z_opt[idx] = val
    return z_opt


def _snap_1d_pav_even(
    y_int: list[int],
    heights: list[int],
    y_lo: int,
    y_hi: int,
    avail: int,
    total_extent: int,
    snap_even_base: int | None,
) -> None:
    """Snap coordinates to even offsets from snap_even_base when slack permits."""
    if snap_even_base is None or total_extent + 2 * len(y_int) > avail:
        return
    for i, y_val in enumerate(y_int):
        if (y_val - snap_even_base) % 2 != 0:
            if y_val + 1 + heights[i] <= y_hi:
                y_int[i] += 1
            elif y_val - 1 >= y_lo:
                y_int[i] -= 1


def _sweep_1d_pav_bounds(
    y_int: list[int],
    heights: list[int],
    eff_gaps: list[int],
    y_lo: int,
    y_hi: int,
) -> None:
    """Enforce lower and upper boundary constraints with forward and backward sweeps."""
    n = len(y_int)
    y_int[0] = max(y_lo, y_int[0])
    for i in range(1, n):
        y_int[i] = max(y_int[i], y_int[i - 1] + heights[i - 1] + eff_gaps[i - 1])

    if y_int[-1] + heights[-1] > y_hi:
        y_int[-1] = max(y_lo, y_hi - heights[-1])
        for i in range(n - 2, -1, -1):
            y_int[i] = max(y_lo, min(y_int[i], y_int[i + 1] - heights[i] - eff_gaps[i]))

    y_int[0] = max(y_lo, y_int[0])
    for i in range(1, n):
        min_req = y_int[i - 1] + heights[i - 1] + min(1, eff_gaps[i - 1])
        if y_int[i] < min_req and min_req + heights[i] <= y_hi:
            y_int[i] = min_req


def _solve_1d_pav(
    ideal_y: list[float],
    heights: list[int],
    gaps: list[int],
    y_lo: int,
    y_hi: int,
    weights: list[float] | None = None,
    snap_even_base: int | None = None,
) -> list[int]:
    """Solve 1D non-overlapping placement with the Pool-Adjacent-Violators algorithm."""
    n = len(ideal_y)
    if n == 0:
        return []
    eff_weights = [1.0] * n if weights is None else weights
    avail = max(1, y_hi - y_lo)
    eff_gaps = _compress_1d_pav_gaps(gaps, heights, n, avail)

    offsets = [0] * n
    acc = 0
    for i in range(n - 1):
        acc += heights[i] + eff_gaps[i]
        offsets[i + 1] = acc

    total_extent = acc + heights[-1]
    z_max = float(max(y_lo, y_hi - total_extent))
    z_min = float(y_lo)
    z_target = [float(ideal_y[i]) - float(offsets[i]) for i in range(n)]
    z_opt = _pool_adjacent_violators(z_target, eff_weights, z_min, z_max)

    y_int = [round(z_opt[i] + offsets[i]) for i in range(n)]
    _snap_1d_pav_even(y_int, heights, y_lo, y_hi, avail, total_extent, snap_even_base)
    _sweep_1d_pav_bounds(y_int, heights, eff_gaps, y_lo, y_hi)
    return y_int


def _get_chassis_block_sequence(
    grid_w: int,
    grid_h: int,
    empty_blocks: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Return the West-to-East column-snake sequence of active city blocks."""
    cells: list[tuple[int, int]] = []
    for bx in range(grid_w):
        y_range = range(grid_h) if (bx % 2 == 0) else range(grid_h - 1, -1, -1)
        cells.extend((bx, by) for by in y_range)

    blocked = set(empty_blocks) if empty_blocks else set()
    if not blocked and grid_w == _CENTER_HOLE_GRID and grid_h == _CENTER_HOLE_GRID:
        blocked.add(((grid_w - 1) // 2, (grid_h - 1) // 2))

    active = [b for b in cells if b not in blocked]
    return active if active else [(0, 0)]


def _default_netlist_cell_meta(cell_key: str) -> MutableJsonMap:
    """Return default cell dimensions and port names for synthesized macros."""
    if cell_key == "belt_merge2":
        return {"width": _MERGE2_DIM, "height": _MERGE2_DIM, "inputs": ["a", "b"], "outputs": ["y"]}
    if cell_key == "belt_split2":
        return {"width": 1, "height": 2, "inputs": ["a"], "outputs": ["y1", "y2"]}
    if cell_key == "belt_balancer2":
        return {"width": 1, "height": 2, "inputs": ["a1", "a2"], "outputs": ["y1", "y2"]}
    return {"width": _MERGE2_DIM, "height": _MERGE2_DIM, "inputs": [], "outputs": []}


def _register_merge_plans(
    nodes: dict[str, PlacerNode],
    wire_drivers: dict[str, list[tuple[str, float]]],
    wire_sinks: dict[str, list[tuple[str, float]]],
    merge_plans: MergePlansMap | None,
) -> None:
    """Register merger plan connections onto nodes, wire_drivers, and wire_sinks."""
    if not merge_plans:
        return
    for plan in merge_plans.values():
        for m_name, a_w, b_w, y_w in plan:
            if m_name in nodes:
                nodes[m_name].inputs.extend([a_w, b_w])
                nodes[m_name].outputs.append(y_w)
            wire_sinks[a_w].append((m_name, 0.0))
            wire_sinks[b_w].append((m_name, 1.0))
            wire_drivers[y_w].append((m_name, 1.0))


def _register_split_plans(
    nodes: dict[str, PlacerNode],
    wire_drivers: dict[str, list[tuple[str, float]]],
    wire_sinks: dict[str, list[tuple[str, float]]],
    split_plans: SplitPlansMap | None,
) -> None:
    """Register splitter plan connections onto nodes, wire_drivers, and wire_sinks."""
    if not split_plans:
        return
    for plan in split_plans.values():
        for s_name, in_w, y1_w, y2_w in plan:
            if s_name in nodes:
                nodes[s_name].inputs.append(in_w)
                nodes[s_name].outputs.extend([y1_w, y2_w])
            wire_sinks[in_w].append((s_name, _HALF))
            wire_drivers[y1_w].append((s_name, 0.0))
            wire_drivers[y2_w].append((s_name, 1.0))


def _register_balancer_plans(
    nodes: dict[str, PlacerNode],
    wire_drivers: dict[str, list[tuple[str, float]]],
    wire_sinks: dict[str, list[tuple[str, float]]],
    balancer_plans: BalancerPlansMap | None,
) -> None:
    """Register balancer plan connections onto nodes, wire_drivers, and wire_sinks."""
    if not balancer_plans:
        return
    for b_plan in balancer_plans.values():
        for bal_name, a1_w, a2_w, y1_w, y2_w in b_plan:
            if bal_name in nodes:
                nodes[bal_name].inputs.extend([a1_w, a2_w])
                nodes[bal_name].outputs.extend([y1_w, y2_w])
            wire_sinks[a1_w].append((bal_name, 0.0))
            wire_sinks[a2_w].append((bal_name, 1.0))
            wire_drivers[y1_w].append((bal_name, 0.0))
            wire_drivers[y2_w].append((bal_name, 1.0))


def _build_netlist_port_wire_index(
    nl_nets: JsonMap,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """Build O(1) lookup tables mapping (base_port, inst_name) to sanitized wire names."""
    sink_wire_map: dict[tuple[str, str], str] = {}
    drv_wire_map: dict[tuple[str, str], str] = {}
    if not nl_nets:
        return sink_wire_map, drv_wire_map
    for n_key, net_obj in nl_nets.items():
        if "__" not in n_key:
            continue
        base_p = n_key.split("__", 1)[0]
        s_key = _sanitize(n_key)
        for snk in getattr(net_obj, "sinks", ()):
            sink_wire_map.setdefault((base_p, snk), s_key)
        for drv in getattr(net_obj, "drivers", ()):
            drv_wire_map.setdefault((base_p, drv), s_key)
    return sink_wire_map, drv_wire_map


def _connect_netlist_in_ports(
    inst_name: str,
    node: PlacerNode,
    in_ports: list[str],
    nl_nets: JsonMap,
    sink_wire_map: dict[tuple[str, str], str],
    wire_sinks: dict[str, list[tuple[str, float]]],
    split_plans: SplitPlansMap | None,
    balancer_plans: BalancerPlansMap | None,
) -> None:
    """Connect input ports of a netlist machine instance to wire_sinks."""
    off_in = max(0, (node.height - len(in_ports)) // 2)
    for idx, p in enumerate(in_ports):
        s_port = _sanitize(p)
        if (split_plans and p in split_plans) or (balancer_plans and p in balancer_plans):
            w_name = f"{s_port}__{inst_name}"
        elif nl_nets and p not in nl_nets:
            w_name = sink_wire_map.get((p, inst_name), s_port)
        else:
            w_name = s_port
        node.inputs.append(w_name)
        wire_sinks[w_name].append((inst_name, float(off_in + idx)))


def _connect_netlist_out_ports(
    inst_name: str,
    node: PlacerNode,
    out_ports: list[str],
    nl_nets: JsonMap,
    drv_wire_map: dict[tuple[str, str], str],
    wire_drivers: dict[str, list[tuple[str, float]]],
    merge_plans: MergePlansMap | None,
) -> None:
    """Connect output ports of a netlist machine instance to wire_drivers."""
    off_out = max(0, (node.height - len(out_ports)) // 2)
    for idx, p in enumerate(out_ports):
        s_port = _sanitize(p)
        if merge_plans and p in merge_plans:
            w_name = f"{s_port}__{inst_name}"
        elif nl_nets and p not in nl_nets:
            w_name = drv_wire_map.get((p, inst_name), s_port)
        else:
            w_name = s_port
        node.outputs.append(w_name)
        wire_drivers[w_name].append((inst_name, float(off_out + idx)))


def _connect_netlist_instance_ports(
    inst: NetlistLike,
    node: PlacerNode,
    nodes: dict[str, PlacerNode],
    cell: JsonMap,
    nl_nets: JsonMap,
    sink_wire_map: dict[tuple[str, str], str],
    drv_wire_map: dict[tuple[str, str], str],
    wire_drivers: dict[str, list[tuple[str, float]]],
    wire_sinks: dict[str, list[tuple[str, float]]],
    merge_plans: MergePlansMap | None,
    split_plans: SplitPlansMap | None,
    balancer_plans: BalancerPlansMap | None,
) -> None:
    """Connect one netlist instance's inputs and outputs to wire endpoint tables."""
    if node.node_type in ("split", "merge", "balancer"):
        return
    if node.node_type == "display":
        mat_w = _sanitize(inst.recipe)
        c_out = f"c_out_{mat_w}"
        w_name = c_out if f"u_acc_{mat_w}" in nodes else mat_w
        node.inputs.append(w_name)
        wire_sinks[w_name].append((inst.name, _HALF))
        return
    if node.node_type == "combinator" and inst.name.startswith("u_acc_"):
        mat_w = _sanitize(inst.recipe)
        c_out = f"c_out_{mat_w}"
        node.inputs.append(mat_w)
        node.outputs.append(c_out)
        wire_sinks[mat_w].append((inst.name, _HALF))
        wire_drivers[c_out].append((inst.name, _HALF))
        return

    _connect_netlist_in_ports(
        inst.name,
        node,
        list(cell.get("inputs", [])),
        nl_nets,
        sink_wire_map,
        wire_sinks,
        split_plans,
        balancer_plans,
    )
    _connect_netlist_out_ports(
        inst.name,
        node,
        list(cell.get("outputs", [])),
        nl_nets,
        drv_wire_map,
        wire_drivers,
        merge_plans,
    )


def _build_edges_from_wire_endpoints(
    wire_drivers: dict[str, list[tuple[str, float]]],
    wire_sinks: dict[str, list[tuple[str, float]]],
    fluid_set: set[str],
) -> list[PlacerEdge]:
    """Create directed PlacerEdge objects from wire driver and sink tables."""
    edges: list[PlacerEdge] = []
    for wire, drvs in wire_drivers.items():
        base_mat = wire.split("__")[0]
        is_fl = base_mat in fluid_set
        snks = wire_sinks.get(wire, [])
        scale_w = (_FLUID_EDGE_WT if is_fl else 1.0) / max(1.0, math.sqrt(float(len(drvs))))
        for drv, drv_dy in drvs:
            for snk, snk_dy in snks:
                if drv != snk:
                    edges.append(
                        PlacerEdge(
                            source=drv,
                            sink=snk,
                            wire=wire,
                            weight=scale_w,
                            source_dy=drv_dy,
                            sink_dy=snk_dy,
                            is_fluid=is_fl,
                        )
                    )
    return edges


def _precompute_macro_port_tables(
    cells_map: JsonMap,
) -> tuple[dict[str, set[str]], dict[str, dict[str, int]], dict[str, int]]:
    """Build per-macro output port sets and input port index tables."""
    macro_out_sets: dict[str, set[str]] = {}
    macro_in_index: dict[str, dict[str, int]] = {}
    macro_in_count: dict[str, int] = {}
    for m_name, cmeta in cells_map.items():
        outs = cmeta.get("outputs", [])
        macro_out_sets[m_name] = {_sanitize(o) for o in outs} | set(outs) | {"y", "out"}
        ins = cmeta.get("inputs", [])
        in_idx: dict[str, int] = {}
        for idx, inp in enumerate(ins):
            in_idx.setdefault(_sanitize(inp), idx)
        macro_in_index[m_name] = in_idx
        macro_in_count[m_name] = len(ins)
    return macro_out_sets, macro_in_index, macro_in_count


def _classify_tree_def_connection(
    inst_name: str,
    port: str,
    nname: str,
    node: PlacerNode,
    drivers: list[tuple[str, float]],
    sinks: list[tuple[str, float]],
) -> None:
    """Append one splitter, balancer, or merger DEF connection to drivers or sinks."""
    is_split = node.node_type in ("split", "balancer")
    if is_split and port in ("y1", "y", "y2"):
        drivers.append((inst_name, 1.0 if port == "y2" else 0.0))
        node.outputs.append(nname)
        return
    if not is_split and port == "y":
        drivers.append((inst_name, _HALF))
        node.outputs.append(nname)
        return
    sinks.append((inst_name, 1.0 if port in ("b", "a2") else 0.0))
    node.inputs.append(nname)


def _classify_def_net_connection(
    inst_name: str,
    port: str,
    nname: str,
    node: PlacerNode,
    out_set: set[str],
    in_idx_map: dict[str, int],
    in_count: int,
    drivers: list[tuple[str, float]],
    sinks: list[tuple[str, float]],
) -> None:
    """Append one DEF net connection to drivers or sinks and update node ports."""
    if node.node_type in ("split", "balancer", "merge"):
        _classify_tree_def_connection(inst_name, port, nname, node, drivers, sinks)
        return
    s_port = _sanitize(port)
    if port in out_set or s_port in out_set:
        drivers.append((inst_name, node.height / 2.0))
        node.outputs.append(nname)
    else:
        idx = in_idx_map.get(s_port, 0)
        off = max(0, (node.height - in_count) // 2)
        sinks.append((inst_name, float(off + idx)))
        node.inputs.append(nname)


def _append_directed_edges_for_net(
    edges: list[PlacerEdge],
    nname: str,
    drivers: list[tuple[str, float]],
    sinks: list[tuple[str, float]],
    w_val: float,
    is_fl: bool,
) -> None:
    """Append directed PlacerEdge objects for all driver-to-sink pairs on one net."""
    for drv, d_dy in drivers:
        for snk, s_dy in sinks:
            if drv != snk:
                edges.append(
                    PlacerEdge(
                        source=drv,
                        sink=snk,
                        wire=nname,
                        weight=w_val,
                        source_dy=d_dy,
                        sink_dy=s_dy,
                        is_fluid=is_fl,
                    )
                )


def _collect_def_net_endpoints(
    net: JsonMap,
    nname: str,
    in_pin_names: set[str],
    nodes: dict[str, PlacerNode],
    macro_out_sets: dict[str, set[str]],
    macro_in_index: dict[str, dict[str, int]],
    macro_in_count: dict[str, int],
    default_outs: set[str],
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Collect driver and sink (endpoint, dy) lists for a single DEF net."""
    drivers: list[tuple[str, float]] = []
    sinks: list[tuple[str, float]] = []
    for inst_name, port in net.get("connections", []):
        if inst_name == "PIN":
            if nname in in_pin_names or port in in_pin_names:
                drivers.append((f"PIN_IN:{nname}", 0.0))
            else:
                sinks.append((f"PIN_OUT:{nname}", 0.0))
        elif inst_name in nodes:
            node = nodes[inst_name]
            _classify_def_net_connection(
                inst_name,
                port,
                nname,
                node,
                macro_out_sets.get(node.macro, default_outs),
                macro_in_index.get(node.macro, {}),
                macro_in_count.get(node.macro, 0),
                drivers,
                sinks,
            )
    return drivers, sinks


def _build_edges_from_def_nets(
    def_data: JsonMap,
    nodes: dict[str, PlacerNode],
    cells_map: JsonMap,
    inputs_sanitized: list[str],
    fluid_set: set[str],
) -> list[PlacerEdge]:
    """Extract directed PlacerEdge objects from explicit DEF nets."""
    in_pin_names = {
        p.get("net", p.get("name", ""))
        for p in def_data.get("pins", [])
        if p.get("direction") == "INPUT"
    } | set(inputs_sanitized)
    macro_out_sets, macro_in_index, macro_in_count = _precompute_macro_port_tables(cells_map)
    default_outs = {"y", "out"}
    edges: list[PlacerEdge] = []

    for net in def_data["nets"]:
        nname = net.get("name", "")
        drivers, sinks = _collect_def_net_endpoints(
            net,
            nname,
            in_pin_names,
            nodes,
            macro_out_sets,
            macro_in_index,
            macro_in_count,
            default_outs,
        )
        is_fl = nname.split("__")[0] in fluid_set
        w_val = _FLUID_EDGE_WT if is_fl else 1.0
        _append_directed_edges_for_net(edges, nname, drivers, sinks, w_val, is_fl)
    return edges


def _build_inferred_edges_from_data(
    nodes: dict[str, PlacerNode],
    cells_map: JsonMap,
    inputs_sanitized: list[str],
    outputs_sanitized: list[str],
    fluid_set: set[str],
) -> list[PlacerEdge]:
    """Infer directed PlacerEdge objects from cell inputs and outputs when DEF nets are absent."""
    mat_producers: dict[str, list[str]] = collections.defaultdict(list)
    mat_consumers: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
    for inp in inputs_sanitized:
        mat_producers[inp].append(f"PIN_IN:{inp}")
    for outp in outputs_sanitized:
        mat_consumers[outp].append((f"PIN_OUT:{outp}", 0.0))

    for cname, node in nodes.items():
        cmeta = cells_map.get(node.macro, {})
        for idx, inp in enumerate(_sanitize(i) for i in cmeta.get("inputs", [])):
            node.inputs.append(inp)
            mat_consumers[inp].append((cname, float(idx)))
        for outp in (_sanitize(o) for o in cmeta.get("outputs", [])):
            node.outputs.append(outp)
            mat_producers[outp].append(cname)

    edges: list[PlacerEdge] = []
    for mat, prods in mat_producers.items():
        is_fl = mat in fluid_set
        cons = mat_consumers.get(mat, [])
        w = 1.0 / max(1.0, float(len(prods) * len(cons)) ** _HALF)
        drvs = [(p, nodes[p].height / 2.0 if p in nodes else 0.0) for p in prods]
        _append_directed_edges_for_net(edges, mat, drvs, cons, w, is_fl)
    return edges


def _has_dag_path(adj: dict[str, list[str]], start: str, target: str) -> bool:
    """Return True if a directed path exists from start to target in adj."""
    stack = [start]
    seen: set[str] = set()
    while stack:
        curr = stack.pop()
        if curr == target:
            return True
        for nxt in adj.get(curr, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _topo_node_sort_key(
    nodes: dict[str, PlacerNode], name: str
) -> tuple[int, int, list[int | str]]:
    """Return deterministic tie-breaking sort key for topological ordering."""
    nd = nodes[name]
    return (nd.rank, _TOPO_TYPE_ORDER.get(nd.node_type, 2), _natural_key(name))


def _run_kahn_sort(
    nodes: dict[str, PlacerNode],
    adj: dict[str, list[str]],
    in_deg: dict[str, int],
) -> tuple[list[str], set[str]]:
    """Execute Kahn topological sort with rank and node-type tie breaking."""
    ready = collections.deque(name for name, d in in_deg.items() if d == 0)
    topo_order: list[str] = []
    visited: set[str] = set()

    while ready:
        ordered_ready = sorted(ready, key=lambda n: _topo_node_sort_key(nodes, n))
        ready = collections.deque(ordered_ready)
        u = ready.popleft()
        if u in visited:
            continue
        visited.add(u)
        topo_order.append(u)
        for v in adj[u]:
            in_deg[v] -= 1
            if in_deg[v] <= 0 and v not in visited:
                ready.append(v)
    return topo_order, visited


def _add_dag_edges(
    nodes: dict[str, PlacerNode],
    sorted_edges: list[PlacerEdge],
    adj: dict[str, list[str]],
    rev_adj: dict[str, list[str]],
    in_deg: dict[str, int],
    *,
    check_cycles: bool,
) -> None:
    """Populate adjacency lists from sorted_edges while optionally skipping cycle edges."""
    for e in sorted_edges:
        u, v = e.source, e.sink
        if u not in nodes or v not in nodes or u == v:
            continue
        if v in adj[u] or (check_cycles and _has_dag_path(adj, v, u)):
            continue
        adj[u].append(v)
        rev_adj[v].append(u)
        in_deg[v] += 1


def _build_acyclic_flow_graph(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Build cycle-broken DAG adjacency lists and topological node ordering."""
    adj: dict[str, list[str]] = collections.defaultdict(list)
    rev_adj: dict[str, list[str]] = collections.defaultdict(list)
    in_deg: dict[str, int] = dict.fromkeys(nodes, 0)

    sorted_edges = sorted(
        edges,
        key=lambda edge: (
            1
            if (edge.source in nodes and nodes[edge.source].node_type in ("combinator", "display"))
            else 0
        ),
    )
    _add_dag_edges(nodes, sorted_edges, adj, rev_adj, in_deg, check_cycles=False)
    topo_order, visited = _run_kahn_sort(nodes, adj, in_deg)

    if len(visited) < len(nodes):
        adj.clear()
        rev_adj.clear()
        in_deg = dict.fromkeys(nodes, 0)
        _add_dag_edges(nodes, sorted_edges, adj, rev_adj, in_deg, check_cycles=True)
        topo_order, visited = _run_kahn_sort(nodes, adj, in_deg)

    all_sorted = sorted(nodes, key=lambda n: _topo_node_sort_key(nodes, n))
    topo_order.extend(name for name in all_sorted if name not in visited)
    return adj, rev_adj, topo_order


def _compute_asap_depths(
    nodes: dict[str, PlacerNode],
    topo_order: list[str],
    rev_adj: dict[str, list[str]],
    has_edges: bool,
    has_power_switch: bool,
    min_switched_depth: int,
) -> dict[str, int]:
    """Compute forward ASAP flow depths respecting all DAG edges."""
    depth: dict[str, int] = {}
    for u in topo_order:
        node = nodes[u]
        preds = rev_adj.get(u, [])
        base_d = node.rank if not has_edges else 0
        if (
            min_switched_depth > 0
            and node.node_type == "machine"
            and node.recipe not in ("boiler-steam", "steam-power")
        ):
            base_d = max(base_d, min_switched_depth)
        if has_power_switch and node.recipe == "steam-power":
            node.pad_e = max(node.pad_e, _SWITCHED_PAD)
        depth[u] = max([base_d] + [depth[p] + 1 for p in preds if p in depth])
    return depth


def _build_parallel_machine_groups(
    nodes: dict[str, PlacerNode],
    topo_order: list[str],
    adj: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Group non-interacting parallel machines that share block, recipe, and rank."""
    groups_by_key: dict[tuple[tuple[int, int], str, int], list[str]] = collections.defaultdict(list)
    for u in topo_order:
        nd = nodes[u]
        if nd.node_type == "machine" and nd.recipe:
            groups_by_key[(nd.assigned_block, nd.recipe, nd.rank)].append(u)

    mach_group_of: dict[str, list[str]] = {}
    for grp in groups_by_key.values():
        grp_set = set(grp)
        if len(grp) > 1 and not any(v in grp_set for m in grp for v in adj.get(m, [])):
            mach_group_of.update(dict.fromkeys(grp, grp))
    return mach_group_of


def _sync_parallel_group_depths(
    topo_order: list[str],
    rev_adj: dict[str, list[str]],
    mach_group_of: dict[str, list[str]],
    depth: dict[str, int],
) -> None:
    """Align ASAP flow depths across parallel machine groups."""
    if not mach_group_of:
        return
    seen_grps: set[int] = set()
    for u in topo_order:
        preds = rev_adj.get(u, [])
        if preds:
            depth[u] = max(depth[u], max((depth[p] + 1 for p in preds if p in depth), default=0))
        grp = mach_group_of.get(u)
        if (
            grp is not None
            and id(grp) not in seen_grps
            and all(all(p in depth for p in rev_adj.get(m, [])) for m in grp)
        ):
            seen_grps.add(id(grp))
            max_g_d = max(
                max([depth[m]] + [depth[p] + 1 for p in rev_adj.get(m, []) if p in depth])
                for m in grp
            )
            for m in grp:
                depth[m] = max_g_d


def _place_telemetry_depths(
    nodes: dict[str, PlacerNode],
    topo_order: list[str],
    rev_adj: dict[str, list[str]],
    depth: dict[str, int],
    has_power_switch: bool,
) -> None:
    """Place telemetry displays and circuit accumulators in the final East column band."""
    max_non_disp = max(
        (depth[u] for u in nodes if nodes[u].node_type not in ("display", "combinator")),
        default=0,
    )
    for u in topo_order:
        node = nodes[u]
        if node.node_type == "combinator" and u.startswith("u_acc_"):
            if has_power_switch:
                node.pad_w = max(node.pad_w, _SWITCHED_PAD)
            preds = [p for p in rev_adj.get(u, []) if p in depth]
            depth[u] = max([max_non_disp + 1] + [depth[p] + 1 for p in preds])
        elif node.node_type == "display":
            preds = [p for p in rev_adj.get(u, []) if p in depth]
            depth[u] = max([max_non_disp + 2] + [depth[p] + 1 for p in preds])


def _node_switched_floor_depth(node: PlacerNode, min_switched_depth: int) -> int:
    """Return minimum floor depth for a node when a power switch is present."""
    if (
        min_switched_depth > 0
        and node.node_type == "machine"
        and node.recipe not in ("boiler-steam", "steam-power")
    ):
        return min_switched_depth
    return 0


def _tighten_single_node_backward(
    u: str,
    node: PlacerNode,
    adj: dict[str, list[str]],
    rev_adj: dict[str, list[str]],
    depth: dict[str, int],
    min_switched_depth: int,
) -> None:
    """Pull one node East toward its immediate successors."""
    succs = [s for s in adj.get(u, []) if s in depth]
    if not succs:
        return
    preds = [p for p in rev_adj.get(u, []) if p in depth]
    floor_d = _node_switched_floor_depth(node, min_switched_depth)
    min_allowed = max([floor_d] + [depth[p] + 1 for p in preds])
    depth[u] = max(min_allowed, min(depth[s] - 1 for s in succs))


def _tighten_group_backward(
    grp: list[str],
    node: PlacerNode,
    adj: dict[str, list[str]],
    rev_adj: dict[str, list[str]],
    depth: dict[str, int],
    min_switched_depth: int,
) -> None:
    """Pull one parallel machine group East toward its immediate successors."""
    floor_d = _node_switched_floor_depth(node, min_switched_depth)
    grp_min = max(
        max([floor_d] + [depth[p] + 1 for p in rev_adj.get(m, []) if p in depth]) for m in grp
    )
    grp_succs = [s for m in grp for s in adj.get(m, []) if s in depth]
    if grp_succs:
        new_d = max(grp_min, min(depth[s] - 1 for s in grp_succs))
        for m in grp:
            depth[m] = new_d


def _tighten_backward_depths(
    nodes: dict[str, PlacerNode],
    topo_order: list[str],
    adj: dict[str, list[str]],
    rev_adj: dict[str, list[str]],
    fluid_nodes: set[str],
    mach_group_of: dict[str, list[str]],
    depth: dict[str, int],
    use_chassis: bool,
    min_switched_depth: int,
) -> None:
    """Pull splitters, balancers, and solid machines East toward their sinks."""
    pulled_grps: set[int] = set()
    for u in reversed(topo_order):
        node = nodes[u]
        if node.node_type not in ("split", "balancer", "machine") or (
            not use_chassis and u in fluid_nodes
        ):
            continue
        grp = mach_group_of.get(u)
        if grp is not None:
            if id(grp) not in pulled_grps:
                pulled_grps.add(id(grp))
                _tighten_group_backward(grp, node, adj, rev_adj, depth, min_switched_depth)
            continue
        _tighten_single_node_backward(u, node, adj, rev_adj, depth, min_switched_depth)


def _apply_power_column_alignment(
    nodes: dict[str, PlacerNode],
    topo_order: list[str],
    adj: dict[str, list[str]],
    rev_adj: dict[str, list[str]],
    depth: dict[str, int],
    boilers: list[str],
    engines: list[str],
    targets: tuple[int, int, int, int, int, int],
) -> None:
    """Assign aligned column depths to boilers, steam engines, and fuel splitters."""
    b_target, b_target2, max_b_col, s_target, s_target2, cap_s1 = targets
    for idx_b, u in enumerate(boilers):
        preds = [p for p in rev_adj.get(u, []) if p in depth]
        min_b = max([0] + [depth[p] + 1 for p in preds])
        tgt_b = b_target if idx_b < max_b_col else b_target2
        depth[u] = max(min_b, tgt_b)
    max_b_d = max(depth[u] for u in boilers)
    for idx_e, u in enumerate(engines):
        tgt_s = s_target if idx_e < cap_s1 else s_target2
        depth[u] = max(max_b_d + 1, tgt_s)
    boiler_fuels = {inp for b_u in boilers for inp in nodes[b_u].inputs}
    for u in reversed(topo_order):
        if nodes[u].node_type in ("split", "balancer") and nodes[u].recipe in boiler_fuels:
            _tighten_single_node_backward(u, nodes[u], adj, rev_adj, depth, 0)


def _select_second_col_target(candidates: list[int], first_target: int) -> int:
    """Return second column target depth from candidates or increment first_target."""
    if len(candidates) >= 2:
        return candidates[1]
    return first_target + 1


def _align_power_columns(
    nodes: dict[str, PlacerNode],
    topo_order: list[str],
    adj: dict[str, list[str]],
    rev_adj: dict[str, list[str]],
    depth: dict[str, int],
) -> None:
    """Align terminal steam-power and boiler-steam cells with existing machine columns."""
    prod_mach_depths = sorted(
        {
            depth[u]
            for u, nd in nodes.items()
            if nd.node_type == "machine" and nd.recipe not in ("boiler-steam", "steam-power")
        }
    )
    boilers = [u for u, nd in nodes.items() if nd.recipe == "boiler-steam"]
    engines = [u for u, nd in nodes.items() if nd.recipe == "steam-power"]
    if not prod_mach_depths or not boilers or not engines:
        return
    count_at_depth = collections.Counter(depth.values())
    max_coal_split_d = max(
        (depth[u] for u in boilers for p in rev_adj.get(u, []) if p in depth),
        default=-1,
    )
    valid_b_depths = [d for d in prod_mach_depths if d > max_coal_split_d] or prod_mach_depths
    b_target = valid_b_depths[0]
    b_target2 = _select_second_col_target(valid_b_depths, b_target)
    max_b_col = min(_MAX_BOILER_FIRST_COL, max(0, _COL_TARGET_CAP - count_at_depth[b_target]))
    max_b_col2 = max(0, _COL_TARGET_CAP - count_at_depth[b_target2])
    last_b = b_target2 if len(boilers) > max_b_col else b_target
    s_candidates = [d for d in prod_mach_depths if d > last_b]
    s_target = s_candidates[0] if s_candidates else last_b + 1
    s_target2 = _select_second_col_target(s_candidates, s_target)
    cap_s1 = max(0, _COL_TARGET_CAP - count_at_depth[s_target])
    cap_s2 = max(0, _COL_TARGET_CAP - count_at_depth[s_target2])
    if len(boilers) <= max_b_col + max_b_col2 and len(engines) <= cap_s1 + cap_s2:
        _apply_power_column_alignment(
            nodes,
            topo_order,
            adj,
            rev_adj,
            depth,
            boilers,
            engines,
            (b_target, b_target2, max_b_col, s_target, s_target2, cap_s1),
        )


def _accumulate_node_core_demand(
    nodes: dict[str, PlacerNode],
    power_only_fuels: set[str],
) -> tuple[float, int, int]:
    """Return (total_demand, num_boilers, num_labs) across all placement nodes."""
    total_demand = 0.0
    num_boilers = 0
    num_labs = 0
    for nd in nodes.values():
        if nd.recipe == "boiler-steam":
            num_boilers += 1
            continue
        if nd.recipe == "steam-power" or nd.recipe in power_only_fuels:
            continue
        if nd.recipe == "infinite-research":
            num_labs += 1
            continue
        area = float((nd.width + nd.pad_w + nd.pad_e) * (nd.height + nd.pad_n + nd.pad_s))
        if nd.node_type in ("machine", "pump"):
            total_demand += (
                area
                + _IN_PORT_AREA_WT * float(len(nd.inputs))
                + _OUT_PORT_AREA_WT * float(len(nd.outputs))
            )
        else:
            total_demand += area * _NON_MACH_DEMAND_SCALE
    return total_demand, num_boilers, num_labs


@dataclass(slots=True)
class _TarjanState:
    """Mutable DFS state for Tarjan strongly connected component discovery."""

    index: int = 0
    indices: dict[str, int] = field(default_factory=dict)
    lowlink: dict[str, int] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    on_stack: set[str] = field(default_factory=set)
    sccs: list[list[str]] = field(default_factory=list)


def _pop_tarjan_scc(u: str, state: _TarjanState) -> None:
    """Pop one strongly connected component rooted at u from Tarjan stack."""
    scc: list[str] = []
    while True:
        w_node = state.stack.pop()
        state.on_stack.remove(w_node)
        scc.append(w_node)
        if w_node == u:
            break
    state.sccs.append(scc)


def _tarjan_strongconnect(
    u: str,
    mach_adj: dict[str, dict[str, float]],
    state: _TarjanState,
) -> None:
    """Run recursive Tarjan DFS step from node u."""
    state.indices[u] = state.index
    state.lowlink[u] = state.index
    state.index += 1
    state.stack.append(u)
    state.on_stack.add(u)

    for v, w in sorted(mach_adj.get(u, {}).items()):
        if w <= 0:
            continue
        if v not in state.indices:
            _tarjan_strongconnect(v, mach_adj, state)
            state.lowlink[u] = min(state.lowlink[u], state.lowlink[v])
        elif v in state.on_stack:
            state.lowlink[u] = min(state.lowlink[u], state.indices[v])

    if state.lowlink[u] == state.indices[u]:
        _pop_tarjan_scc(u, state)


@dataclass(slots=True)
class _BlockOptModel:
    """Precomputed bucket adjacency and boundary weights for 2D block assignment."""

    num_b: int
    k_bins: int
    grid_w: int
    max_x: float
    penult_x: float
    has_lab_buckets: bool
    buckets: list[list[str]]
    b_adj: list[list[float]]
    b_contig: list[list[float]]
    b_west: list[float]
    b_east: list[float]
    b_avg_rank: list[float]
    b_recipes: list[set[str]]
    x_val: list[float]


def _accumulate_bucket_machine_edges(
    i: int,
    u: str,
    inv_len: float,
    mach_adj: dict[str, dict[str, float]],
    b_of_mach: dict[str, int],
    b_adj_row: list[float],
) -> None:
    """Accumulate normalized outgoing weights from machine u into b_adj_row."""
    u_edges = mach_adj.get(u, {})
    u_tot = sum(u_edges.values())
    if u_tot <= 0.0:
        return
    scale_u = inv_len / u_tot
    for v, w in u_edges.items():
        j = b_of_mach.get(v, -1)
        if j >= 0 and j != i:
            b_adj_row[j] += w * scale_u


def _build_bucket_adjacency(
    buckets: list[list[str]],
    mach_adj: dict[str, dict[str, float]],
    num_b: int,
) -> list[list[float]]:
    """Build normalized directed adjacency matrix between machine buckets."""
    b_of_mach = {u: i for i, b_list in enumerate(buckets) for u in b_list}
    b_adj: list[list[float]] = [[0.0] * num_b for _ in range(num_b)]
    for i, b_list in enumerate(buckets):
        if not b_list:
            continue
        inv_len = _BUCKET_EDGE_NORM / float(len(b_list))
        for u in b_list:
            _accumulate_bucket_machine_edges(i, u, inv_len, mach_adj, b_of_mach, b_adj[i])
    return b_adj


def _build_bucket_contiguity(
    b_recipes: list[set[str]],
    b_outs: list[set[str]],
    num_b: int,
    k_bins: int,
) -> list[list[float]]:
    """Build symmetric contiguity affinity matrix between adjacent DP buckets."""
    b_contig: list[list[float]] = [[0.0] * num_b for _ in range(num_b)]
    for i in range(k_bins - 1):
        if b_recipes[i] & b_recipes[i + 1]:
            b_contig[i][i + 1] = b_contig[i + 1][i] = _RECIPE_CONTIG_WT
        elif len(b_outs[i]) == 1 and b_outs[i] == b_outs[i + 1]:
            b_contig[i][i + 1] = b_contig[i + 1][i] = 1.0
    return b_contig


def _initial_bucket_harmonic_x(
    i: int,
    buckets: list[list[str]],
    b_west: list[float],
    b_east: list[float],
    b_avg_rank: list[float],
    b_recipes: list[set[str]],
    max_rank: int,
    max_x: float,
    penult_x: float,
    has_lab_buckets: bool,
) -> float:
    """Compute initial continuous X coordinate for bucket i before harmonic relaxation."""
    if b_west[i] > 0 and b_east[i] == 0 and b_avg_rank[i] <= _WEST_RANK_CUTOFF:
        return 0.0
    if b_east[i] > 0:
        if not has_lab_buckets or "infinite-research" in b_recipes[i]:
            return max_x
        return penult_x
    if buckets[i]:
        extent_x = penult_x if has_lab_buckets else max_x
        return (b_avg_rank[i] / float(max(1, max_rank))) * extent_x
    return max_x if has_lab_buckets else max_x * _EMPTY_DEFAULT_X_FRAC


def _relax_one_bucket_x(
    i: int,
    x_val: list[float],
    b_adj: list[list[float]],
    b_west: list[float],
    b_east: list[float],
    b_avg_rank: list[float],
    b_recipes: list[set[str]],
    k_bins: int,
    denom_rank: float,
    rank_extent_x: float,
    max_x: float,
    penult_x: float,
    has_lab_buckets: bool,
) -> None:
    """Update x_val[i] with one Gauss-Seidel harmonic relaxation step."""
    east_target_x = (
        max_x if (not has_lab_buckets or "infinite-research" in b_recipes[i]) else penult_x
    )
    num_x = b_east[i] * _HARMONIC_EAST_WT * east_target_x + _HARMONIC_RANK_WT * (
        (b_avg_rank[i] / denom_rank) * rank_extent_x
    )
    den_x = b_west[i] * _HARMONIC_WEST_WT + b_east[i] * _HARMONIC_EAST_WT + _HARMONIC_RANK_WT
    for j in range(k_bins):
        if b_adj[i][j] > 0:
            num_x += b_adj[i][j] * max(0.0, x_val[j] - _HARMONIC_NEIGHBOR_SHIFT)
            den_x += b_adj[i][j]
        if b_adj[j][i] > 0:
            num_x += b_adj[j][i] * min(max_x, x_val[j] + _HARMONIC_NEIGHBOR_SHIFT)
            den_x += b_adj[j][i]
    if den_x > 0:
        x_val[i] = max(0.0, min(max_x, num_x / den_x))


def _relax_bucket_harmonic_x(
    x_val: list[float],
    b_adj: list[list[float]],
    b_west: list[float],
    b_east: list[float],
    b_avg_rank: list[float],
    b_recipes: list[set[str]],
    k_bins: int,
    max_rank: int,
    max_x: float,
    penult_x: float,
    has_lab_buckets: bool,
) -> None:
    """Relax continuous bucket X coordinates over _HARMONIC_ITERS Gauss-Seidel sweeps."""
    rank_extent_x = penult_x if has_lab_buckets else max_x
    denom_rank = float(max(1, max_rank))
    for _it in range(_HARMONIC_ITERS):
        for i in range(k_bins):
            _relax_one_bucket_x(
                i,
                x_val,
                b_adj,
                b_west,
                b_east,
                b_avg_rank,
                b_recipes,
                k_bins,
                denom_rank,
                rank_extent_x,
                max_x,
                penult_x,
                has_lab_buckets,
            )


def _bucket_col_sort_key(i: int, model: _BlockOptModel) -> tuple[object, ...]:
    """Return initial column-major ordering key for bucket i."""
    if model.has_lab_buckets:
        is_lab = "infinite-research" in model.b_recipes[i]
        tier = 2 if is_lab else (1 if not model.buckets[i] else 0)
        head: tuple[object, ...] = (tier, round(model.b_east[i], 2))
    else:
        head = (1 if model.b_east[i] > 0 else 0,)
    return (
        head,
        round(model.x_val[i], _ROUND_3),
        -round(model.b_west[i], 2),
        round(model.b_avg_rank[i], 2),
        0 if model.buckets[i] else 1,
        i,
    )


def _empty_bucket_anchor_cost(bx: int, by: int, model: _BlockOptModel) -> float:
    """Compute unary placement cost for an empty padding bucket at (bx, by)."""
    if model.has_lab_buckets:
        edge_pen = _EMPTY_EDGE_PEN if bx == 0 else 0.0
        return edge_pen + _EMPTY_LAB_X_WT * abs(float(bx) - model.max_x)
    edge_pen = _EMPTY_EDGE_PEN if bx in (0, model.grid_w - 1) else 0.0
    return (
        edge_pen
        + _EMPTY_Y_WT * float(by)
        + _EMPTY_CENTER_X_WT * abs(float(bx) - model.max_x * _HALF)
    )


def _bucket_anchor_cost(i: int, blk: tuple[int, int], model: _BlockOptModel) -> float:
    """Compute unary boundary and harmonic anchor cost for placing bucket i at blk."""
    bx, by = blk
    if not model.buckets[i]:
        return _empty_bucket_anchor_cost(bx, by, model)
    if model.has_lab_buckets:
        if "infinite-research" in model.b_recipes[i]:
            return model.b_east[i] * _LAB_EAST_ANCHOR_WT * float(
                (model.grid_w - 1) - bx
            ) + _LAB_Y_CENTER_WT * abs(float(by) - model.max_x * _HALF)
        lab_col_pen = _NON_LAB_EAST_COL_PEN if bx == model.grid_w - 1 else 0.0
        east_pen = (
            model.b_east[i] * _HARMONIC_EAST_WT * abs(float(max(0, model.grid_w - 2) - bx))
            if model.b_east[i] > 0
            else 0.0
        )
        return (
            model.b_west[i] * _ANCHOR_WEST_WT * float(bx)
            + east_pen
            + lab_col_pen
            + _HARMONIC_DEV_WT * abs(float(bx) - model.x_val[i])
        )
    return (
        model.b_west[i] * _ANCHOR_WEST_WT * float(bx)
        + model.b_east[i] * _HARMONIC_EAST_WT * float((model.grid_w - 1) - bx)
        + _HARMONIC_DEV_WT * abs(float(bx) - model.x_val[i])
    )


def _directed_pair_edge_cost(
    w_dir: float,
    bx_src: int,
    by_src: int,
    bx_dst: int,
    by_dst: int,
    rank_src: float,
    rank_dst: float,
) -> float:
    """Compute directed inter-block wirelength and backtracking cost."""
    if w_dir <= 0.0:
        return 0.0
    dx = abs(bx_dst - bx_src)
    back_pen = _BACKTRACK_X_PEN * float(bx_src - bx_dst) if bx_src > bx_dst else 0.0
    same_col_pen = _SAME_COL_RANK_PEN if (bx_src == bx_dst and rank_src + _HALF < rank_dst) else 0.0
    dy = _PAIR_DY_WT * abs(by_dst - by_src)
    return w_dir * (float(dx) + back_pen + same_col_pen + dy)


def _bucket_pair_interaction(
    i: int,
    blk_i: tuple[int, int],
    j: int,
    blk_j: tuple[int, int],
    model: _BlockOptModel,
) -> float:
    """Compute pairwise interaction cost between bucket i at blk_i and bucket j at blk_j."""
    bx_i, by_i = blk_i
    bx_j, by_j = blk_j
    cost = _directed_pair_edge_cost(
        model.b_adj[i][j], bx_i, by_i, bx_j, by_j, model.b_avg_rank[i], model.b_avg_rank[j]
    )
    cost += _directed_pair_edge_cost(
        model.b_adj[j][i], bx_j, by_j, bx_i, by_i, model.b_avg_rank[j], model.b_avg_rank[i]
    )
    w_c = model.b_contig[i][j]
    if w_c > 0:
        cost += w_c * float(abs(bx_i - bx_j) + abs(by_i - by_j))
    if (
        "infinite-research" in model.b_recipes[i]
        and "infinite-research" in model.b_recipes[j]
        and bx_i == bx_j
        and abs(by_i - by_j) < 2
    ):
        cost += _LAB_SAME_COL_PEN * float(2 - abs(by_i - by_j))
    return cost


def _bucket_node_contrib(
    i: int,
    blk_i: tuple[int, int],
    cur_perm: list[tuple[int, int]],
    model: _BlockOptModel,
) -> float:
    """Return total anchor plus pairwise interaction cost for bucket i at blk_i."""
    c = _bucket_anchor_cost(i, blk_i, model)
    for k in range(model.num_b):
        if k != i:
            c += _bucket_pair_interaction(i, blk_i, k, cur_perm[k], model)
    return c


def _find_best_kl_swap(
    perm: list[tuple[int, int]],
    model: _BlockOptModel,
) -> tuple[int, int] | None:
    """Return the pair (i, j) whose block swap yields the largest cost reduction."""
    best_delta = _KL_MIN_DELTA
    best_pair: tuple[int, int] | None = None
    for i in range(model.num_b):
        blk_i = perm[i]
        base_i = _bucket_node_contrib(i, blk_i, perm, model)
        for j in range(i + 1, model.num_b):
            if not model.buckets[i] and not model.buckets[j]:
                continue
            blk_j = perm[j]
            old_c = (
                base_i
                + _bucket_node_contrib(j, blk_j, perm, model)
                - _bucket_pair_interaction(i, blk_i, j, blk_j, model)
            )
            perm[i], perm[j] = blk_j, blk_i
            new_c = (
                _bucket_node_contrib(i, blk_j, perm, model)
                + _bucket_node_contrib(j, blk_i, perm, model)
                - _bucket_pair_interaction(i, blk_j, j, blk_i, model)
            )
            perm[i], perm[j] = blk_i, blk_j
            delta = new_c - old_c
            if delta < best_delta:
                best_delta = delta
                best_pair = (i, j)
    return best_pair


def _node_padded_area(nd: PlacerNode) -> float:
    """Return padded bounding-box area of one PlacerNode."""
    return float((nd.width + nd.pad_w + nd.pad_e) * (nd.height + nd.pad_n + nd.pad_s))


def _node_cluster_weight(nd: PlacerNode) -> float:
    """Return cluster partitioning weight combining padded area and port count."""
    return (
        _node_padded_area(nd)
        + _IN_PORT_AREA_WT * float(len(nd.inputs))
        + _OUT_PORT_AREA_WT * float(len(nd.outputs))
    )


def _sort_machines_by_flow(nodes: dict[str, PlacerNode], mlist: list[str]) -> list[str]:
    """Order machine names by rank, flow_depth, sanitized recipe, and natural key."""
    return sorted(
        mlist,
        key=lambda name: (
            nodes[name].rank,
            nodes[name].flow_depth,
            _sanitize(nodes[name].recipe),
            _natural_key(name),
        ),
    )


def _fallback_partition_blocks(
    nodes: dict[str, PlacerNode],
    active_blocks: list[tuple[int, int]],
    total_padded_area: float,
    single_core_capacity: float,
    distribute_across_blocks: bool,
) -> dict[tuple[int, int], list[str]]:
    """Partition nodes sequentially by rank and area when machines or edges are absent."""
    num_blocks = len(active_blocks)
    ordered_names = sorted(
        nodes.keys(),
        key=lambda name: (
            nodes[name].rank,
            nodes[name].flow_depth,
            nodes[name].recipe,
            _natural_key(name),
        ),
    )
    multi_block_cap = single_core_capacity * _MULTI_BLOCK_CAP_FRAC
    target_per_block = max(
        1.0,
        total_padded_area / float(num_blocks)
        if (distribute_across_blocks or total_padded_area > multi_block_cap * float(num_blocks))
        else multi_block_cap,
    )
    block_bins: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
    cur_blk_idx = 0
    cur_area = 0.0
    for u in ordered_names:
        u_area = _node_padded_area(nodes[u])
        if cur_area > 0 and cur_area + u_area > target_per_block and cur_blk_idx < num_blocks - 1:
            cur_blk_idx += 1
            cur_area = 0.0
        blk = active_blocks[cur_blk_idx]
        nodes[u].assigned_block = blk
        block_bins[blk].append(u)
        cur_area += u_area
    return dict(block_bins)


def _mark_fluid_edge_endpoints(
    e: PlacerEdge,
    nodes: dict[str, PlacerNode],
    fluid_machines: set[str],
) -> None:
    """Record fluid machine and pump endpoints of edge e."""
    if not e.is_fluid:
        return
    for endp in (e.source, e.sink):
        if endp in nodes and nodes[endp].node_type in ("machine", "pump"):
            fluid_machines.add(endp)


def _build_material_hub_graph(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
) -> tuple[dict[str, list[str]], dict[str, list[str]], set[str]]:
    """Build forward and reverse adjacency maps with virtual material hubs."""
    fluid_machines: set[str] = set()
    fwd_adj: dict[str, list[str]] = collections.defaultdict(list)
    rev_adj: dict[str, list[str]] = collections.defaultdict(list)
    for e in edges:
        fwd_adj[e.source].append(e.sink)
        rev_adj[e.sink].append(e.source)
        _mark_fluid_edge_endpoints(e, nodes, fluid_machines)
    for u_name, nd in nodes.items():
        if nd.node_type == "merge" and nd.recipe:
            hub = f"MAT_HUB:{nd.recipe}"
            fwd_adj[u_name].append(hub)
            rev_adj[hub].append(u_name)
        elif nd.node_type == "split" and nd.recipe:
            hub = f"MAT_HUB:{nd.recipe}"
            fwd_adj[hub].append(u_name)
            rev_adj[u_name].append(hub)
    return fwd_adj, rev_adj, fluid_machines


def _is_tree_or_hub_node(cur: str, nodes: dict[str, PlacerNode]) -> bool:
    """Return True if cur is a virtual material hub or a splitter/merger/balancer node."""
    if cur.startswith("MAT_HUB:"):
        return True
    return cur in nodes and nodes[cur].node_type in ("split", "merge", "balancer")


def _collect_downstream_machines_and_pins(
    u: str,
    nodes: dict[str, PlacerNode],
    fwd_adj: dict[str, list[str]],
    machine_set: set[str],
    lab_set: set[str],
) -> tuple[list[str], int, bool]:
    """Traverse downstream tree/hub nodes from u to find machines and PIN_OUT count."""
    down_machs: list[str] = []
    pin_out_cnt = 0
    seen_m: set[str] = {u}
    stack_m = list(fwd_adj.get(u, []))
    feeds_lab = False
    while stack_m:
        cur = stack_m.pop()
        if cur in seen_m:
            continue
        seen_m.add(cur)
        if cur.startswith("PIN_OUT:"):
            pin_out_cnt += 1
            continue
        if cur in machine_set:
            feeds_lab = feeds_lab or (cur in lab_set)
            down_machs.append(cur)
            continue
        if _is_tree_or_hub_node(cur, nodes):
            stack_m.extend(fwd_adj.get(cur, []))
    return down_machs, pin_out_cnt, feeds_lab


def _trace_machine_downstream(
    u: str,
    nodes: dict[str, PlacerNode],
    fwd_adj: dict[str, list[str]],
    machine_set: set[str],
    lab_set: set[str],
    mach_adj: dict[str, dict[str, float]],
    east_out_weight: dict[str, float],
) -> None:
    """Trace downstream machines and East boundary weights from machine u."""
    if u in lab_set:
        east_out_weight[u] += _LAB_EAST_WT
    down_machs, pin_out_cnt, feeds_lab = _collect_downstream_machines_and_pins(
        u, nodes, fwd_adj, machine_set, lab_set
    )
    if pin_out_cnt > 0:
        east_out_weight[u] += _EAST_PIN_WT * float(pin_out_cnt)
    if feeds_lab:
        east_out_weight[u] += _EAST_PIN_WT
    if down_machs:
        wt = 1.0 / max(1.0, math.sqrt(float(len(down_machs))))
        for v in down_machs:
            mach_adj[u][v] += wt


def _count_upstream_pin_in_hits(
    u: str,
    nodes: dict[str, PlacerNode],
    rev_adj: dict[str, list[str]],
    machine_set: set[str],
) -> int:
    """Count distinct West PIN_IN boundary sources reachable upstream from machine u."""
    seen_up: set[str] = {u}
    pin_in_hits = 0
    stack_up = list(rev_adj.get(u, []))
    while stack_up:
        cur = stack_up.pop()
        if cur in seen_up:
            continue
        seen_up.add(cur)
        if cur.startswith("PIN_IN:"):
            pin_in_hits += 1
            continue
        if cur not in machine_set and _is_tree_or_hub_node(cur, nodes):
            stack_up.extend(rev_adj.get(cur, []))
    return pin_in_hits


def _trace_machine_upstream(
    u: str,
    nodes: dict[str, PlacerNode],
    rev_adj: dict[str, list[str]],
    machine_set: set[str],
    fluid_machines: set[str],
    grid_w: int,
    west_in_weight: dict[str, float],
) -> None:
    """Trace upstream West boundary input weights reaching machine u."""
    pin_in_hits = _count_upstream_pin_in_hits(u, nodes, rev_adj, machine_set)
    if pin_in_hits <= 0:
        return
    if nodes[u].recipe in ("boiler-steam", "steam-power") and grid_w < _SEVEN_GRID_W:
        west_in_weight[u] = _POWER_WEST_WT
    else:
        in_wt = _FLUID_WEST_WT if u in fluid_machines else 2.0
        west_in_weight[u] += in_wt * float(pin_in_hits)


def _can_traverse_for_pin_out(
    cur: str,
    nodes: dict[str, PlacerNode],
    lab_set: set[str],
) -> bool:
    """Return True if cur should be expanded when searching for reachable PIN_OUTs."""
    if cur in lab_set:
        return False
    if cur.startswith("MAT_HUB:"):
        return True
    return cur in nodes and nodes[cur].node_type not in ("combinator", "display")


def _trace_one_machine_pin_outs(
    u: str,
    nodes: dict[str, PlacerNode],
    fwd_adj: dict[str, list[str]],
    lab_set: set[str],
    lab_feed_nodes: dict[str, str],
) -> set[str]:
    """Find all reachable PIN_OUT targets starting from machine u."""
    if u in lab_set:
        return {"PIN_OUT:infinite_research"}
    outs: set[str] = set()
    if u in lab_feed_nodes:
        outs.add(lab_feed_nodes[u])
    seen_u: set[str] = {u}
    stack_u = list(fwd_adj.get(u, []))
    while stack_u:
        cur = stack_u.pop()
        if cur in seen_u:
            continue
        seen_u.add(cur)
        if cur.startswith("PIN_OUT:"):
            outs.add(cur)
            continue
        if cur in lab_feed_nodes:
            outs.add(lab_feed_nodes[cur])
            continue
        if _can_traverse_for_pin_out(cur, nodes, lab_set):
            stack_u.extend(fwd_adj.get(cur, []))
    return outs


def _compute_reachable_pin_outs(
    machine_names: list[str],
    nodes: dict[str, PlacerNode],
    fwd_adj: dict[str, list[str]],
    lab_set: set[str],
    lab_feed_nodes: dict[str, str],
) -> dict[str, set[str]]:
    """Find reachable PIN_OUT targets for each machine to identify dedicated domains."""
    reachable: dict[str, set[str]] = {}
    for u in machine_names:
        reachable[u] = _trace_one_machine_pin_outs(u, nodes, fwd_adj, lab_set, lab_feed_nodes)
    return reachable


def _chunk_machines_by_recipe_only(
    nodes: dict[str, PlacerNode],
    mlist: list[str],
    chunk_sz: int,
    grid_w: int,
) -> list[list[str]]:
    """Group machines by recipe and split large recipe groups into balanced chunks."""
    by_rec_only: dict[str, list[str]] = collections.defaultdict(list)
    for name in mlist:
        nd = nodes[name]
        by_rec_only[_sanitize(nd.recipe) or name].append(name)
    res_grps: list[list[str]] = []
    ordered_keys = sorted(
        by_rec_only.keys(),
        key=lambda rk: (min(nodes[u].rank for u in by_rec_only[rk]), rk),
    )
    for rec_k in ordered_keys:
        rg = sorted(by_rec_only[rec_k], key=lambda u: (nodes[u].rank, _natural_key(u)))
        eff_chunk = (
            max(chunk_sz, _LARGE_CHUNK_SZ)
            if (grid_w >= _HUGE_GRID_W and len(rg) > _HUGE_RECIPE_COUNT)
            else chunk_sz
        )
        if len(rg) <= eff_chunk:
            res_grps.append(rg)
        else:
            n_chunks = max(1, math.ceil(len(rg) / float(eff_chunk)))
            step = math.ceil(len(rg) / float(n_chunks))
            res_grps.extend(rg[i : i + step] for i in range(0, len(rg), step))
    return res_grps


def _chunk_machines_by_rank_recipe(
    nodes: dict[str, PlacerNode],
    mlist: list[str],
    chunk_sz: int,
) -> list[list[str]]:
    """Group machines by (rank, recipe) and slice into chunks of size chunk_sz."""
    by_rec: dict[tuple[int, str], list[str]] = collections.defaultdict(list)
    for name in mlist:
        nd = nodes[name]
        by_rec[(nd.rank, _sanitize(nd.recipe) or name)].append(name)
    res_grps: list[list[str]] = []
    for k in sorted(by_rec.keys(), key=lambda key: (key[0], key[1])):
        rg = by_rec[k]
        if len(rg) <= chunk_sz:
            res_grps.append(rg)
        else:
            res_grps.extend(rg[i : i + chunk_sz] for i in range(0, len(rg), chunk_sz))
    return res_grps


def _chunk_machines(
    nodes: dict[str, PlacerNode],
    mlist: list[str],
    chunk_sz: int,
    grid_w: int,
    has_cycles: bool,
) -> list[list[str]]:
    """Chunk machine list by recipe or by (rank, recipe) according to grid topology."""
    if grid_w >= _LARGE_GRID_W or has_cycles:
        return _chunk_machines_by_recipe_only(nodes, mlist, chunk_sz, grid_w)
    return _chunk_machines_by_rank_recipe(nodes, mlist, chunk_sz)


def _pair_boilers_with_engines(
    boilers: list[str],
    engines: list[str],
    mach_adj: dict[str, dict[str, float]],
) -> list[list[str]]:
    """Group each boiler with its connected or corresponding steam engines."""
    used_eng: set[str] = set()
    triads: list[list[str]] = []
    eng_set = set(engines)
    for b_u in boilers:
        b_engs = sorted(
            [v for v in mach_adj.get(b_u, {}) if v in eng_set and v not in used_eng],
            key=_natural_key,
        )
        if not b_engs:
            b_idx_t = len(triads)
            b_engs = [e_u for e_u in engines[2 * b_idx_t : 2 * b_idx_t + 2] if e_u not in used_eng]
        used_eng.update(b_engs)
        triads.append([b_u, *b_engs])
    rem_engs = [e_u for e_u in engines if e_u not in used_eng]
    if rem_engs and triads:
        triads[-1].extend(rem_engs)
    return triads


def _build_power_triad_groups(
    nodes: dict[str, PlacerNode],
    power_machine_names: list[str],
    mach_adj: dict[str, dict[str, float]],
    grid_w: int,
    has_cycles: bool,
) -> list[list[str]]:
    """Build balanced boiler-engine triad groups for power generation blocks."""
    boilers = [u for u in power_machine_names if nodes[u].recipe == "boiler-steam"]
    engines = [u for u in power_machine_names if nodes[u].recipe == "steam-power"]
    if not boilers or not engines:
        return _chunk_machines(nodes, power_machine_names, 10, grid_w, has_cycles)
    triads = _pair_boilers_with_engines(boilers, engines, mach_adj)
    triad_chunk = _MAX_GRP_LEN if grid_w < _LARGE_GRID_W else 10
    n_p_chunks = max(1, math.ceil(len(triads) / float(triad_chunk)))
    p_step = math.ceil(len(triads) / float(n_p_chunks))
    triad_groups: list[list[str]] = []
    for i in range(0, len(triads), p_step):
        sub_t = triads[i : i + p_step]
        b_part = [t[0] for t in sub_t]
        e_part = [e_u for t in sub_t for e_u in t[1:]]
        triad_groups.append(b_part + e_part)
    return triad_groups


def _extend_domain_groups(
    groups: list[list[str]],
    grp_domains: list[str],
    new_grps: list[list[str]],
    dom_tag: str,
) -> None:
    """Append non-empty machine groups with their domain tag."""
    for g in new_grps:
        if g:
            groups.append(g)
            grp_domains.append(dom_tag)


def _classify_solid_domain_machines(
    nodes: dict[str, PlacerNode],
    non_power_acyclic: list[str],
    fluid_machines: set[str],
    reachable_outs: dict[str, set[str]],
    has_labs: bool,
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Partition solid acyclic machines into furnace, shared, and dedicated-output sets."""
    solid_names = [u for u in non_power_acyclic if u not in fluid_machines]
    furnace_prefixes = (
        ("assembling_machine", "assembling-machine") if has_labs else ("assembling_machine",)
    )
    furnace_names = _sort_machines_by_flow(
        nodes,
        [
            u
            for u in solid_names
            if ("furnace" in nodes[u].macro and not nodes[u].macro.startswith(furnace_prefixes))
            or nodes[u].rank <= 1
        ],
    )
    furnace_set = set(furnace_names)
    non_furnace_solid = [u for u in solid_names if u not in furnace_set]
    shared_solid = _sort_machines_by_flow(
        nodes, [u for u in non_furnace_solid if len(reachable_outs.get(u, set())) != 1]
    )
    dedicated_by_out: dict[str, list[str]] = collections.defaultdict(list)
    for u in non_furnace_solid:
        ro = reachable_outs.get(u, set())
        if len(ro) == 1:
            dedicated_by_out[next(iter(ro))].append(u)
    return furnace_names, shared_solid, dedicated_by_out


def _append_scc_domain_groups(
    nodes: dict[str, PlacerNode],
    sccs: list[list[str]],
    single_core_capacity: float,
    groups: list[list[str]],
    grp_domains: list[str],
    atomic_first: set[str],
) -> None:
    """Append strongly connected component machine groups."""
    for scc_idx, scc in enumerate(sccs):
        if len(scc) <= 1:
            continue
        sorted_scc = _sort_machines_by_flow(nodes, scc)
        scc_area = sum(_node_padded_area(nodes[u]) for u in sorted_scc)
        if scc_area <= single_core_capacity * _SCC_ATOMIC_FRAC:
            _extend_domain_groups(groups, grp_domains, [sorted_scc], f"scc:{scc_idx}")
            atomic_first.add(sorted_scc[0])
        else:
            scc_chunks = [
                sorted_scc[i : i + _MAX_GRP_LEN] for i in range(0, len(sorted_scc), _MAX_GRP_LEN)
            ]
            _extend_domain_groups(groups, grp_domains, scc_chunks, f"scc:{scc_idx}")


def _append_fluid_domain_groups(
    nodes: dict[str, PlacerNode],
    fluid_machine_names: list[str],
    west_in_weight: dict[str, float],
    single_core_capacity: float,
    has_labs: bool,
    grid_w: int,
    has_cycles: bool,
    groups: list[list[str]],
    grp_domains: list[str],
    atomic_first: set[str],
) -> None:
    """Append fluid/chemical domain machine groups."""
    if not fluid_machine_names:
        return
    fluid_area = sum(_node_padded_area(nodes[u]) for u in fluid_machine_names)
    if fluid_area <= single_core_capacity * _SCC_ATOMIC_FRAC:
        _extend_domain_groups(groups, grp_domains, [fluid_machine_names], "fluid")
        atomic_first.add(fluid_machine_names[0])
        return
    if not has_labs:
        _extend_domain_groups(
            groups,
            grp_domains,
            _chunk_machines(nodes, fluid_machine_names, 10, grid_w, has_cycles),
            "fluid",
        )
        return
    fluid_west = [u for u in fluid_machine_names if west_in_weight.get(u, 0.0) > 0.0]
    fluid_inner = [u for u in fluid_machine_names if west_in_weight.get(u, 0.0) <= 0.0]
    if fluid_west:
        _extend_domain_groups(
            groups,
            grp_domains,
            _chunk_machines(nodes, fluid_west, 10, grid_w, has_cycles),
            "fluid_west",
        )
    if fluid_inner:
        _extend_domain_groups(
            groups,
            grp_domains,
            _chunk_machines(nodes, fluid_inner, 10, grid_w, has_cycles),
            "fluid_inner",
        )


def _append_fluid_and_shared_groups(
    nodes: dict[str, PlacerNode],
    fluid_machine_names: list[str],
    shared_solid: list[str],
    west_in_weight: dict[str, float],
    single_core_capacity: float,
    has_labs: bool,
    grid_w: int,
    has_cycles: bool,
    groups: list[list[str]],
    grp_domains: list[str],
    atomic_first: set[str],
) -> None:
    """Append fluid/chemical and shared intermediate machine groups."""
    _append_fluid_domain_groups(
        nodes,
        fluid_machine_names,
        west_in_weight,
        single_core_capacity,
        has_labs,
        grid_w,
        has_cycles,
        groups,
        grp_domains,
        atomic_first,
    )
    if not shared_solid:
        return
    shared_area = sum(_node_padded_area(nodes[u]) for u in shared_solid)
    if shared_area <= single_core_capacity * _SHARED_ATOMIC_FRAC:
        _extend_domain_groups(groups, grp_domains, [shared_solid], "shared")
        atomic_first.add(shared_solid[0])
    else:
        _extend_domain_groups(
            groups,
            grp_domains,
            _chunk_machines(nodes, shared_solid, _MAX_GRP_LEN, grid_w, has_cycles),
            "shared",
        )


def _append_dedicated_output_groups(
    nodes: dict[str, PlacerNode],
    dedicated_by_out: dict[str, list[str]],
    single_core_capacity: float,
    has_labs: bool,
    grid_w: int,
    has_cycles: bool,
    groups: list[list[str]],
    grp_domains: list[str],
    atomic_first: set[str],
) -> None:
    """Append dedicated single-output machine groups."""
    sorted_out_keys = sorted(
        dedicated_by_out.keys(),
        key=lambda out_k: (
            max(nodes[u].rank for u in dedicated_by_out[out_k]),
            len(dedicated_by_out[out_k]),
            out_k,
        ),
    )
    for out_k in sorted_out_keys:
        dom_machs = _sort_machines_by_flow(nodes, dedicated_by_out[out_k])
        dom_area = sum(_node_padded_area(nodes[u]) for u in dom_machs)
        if dom_area <= single_core_capacity * _DED_ATOMIC_FRAC and (
            not has_labs or grid_w < _LARGE_GRID_W or len(dom_machs) <= 10
        ):
            _extend_domain_groups(groups, grp_domains, [dom_machs], f"ded:{out_k}")
            atomic_first.add(dom_machs[0])
        else:
            _extend_domain_groups(
                groups,
                grp_domains,
                _chunk_machines(nodes, dom_machs, _MAX_GRP_LEN, grid_w, has_cycles),
                f"ded:{out_k}",
            )


def _append_power_domain_groups(
    nodes: dict[str, PlacerNode],
    power_machine_names: list[str],
    mach_adj: dict[str, dict[str, float]],
    single_core_capacity: float,
    grid_w: int,
    has_cycles: bool,
    has_lab_groups: bool,
    groups: list[list[str]],
    grp_domains: list[str],
    atomic_first: set[str],
) -> list[list[str]]:
    """Append boiler/steam-engine power domain groups and return separate power_groups."""
    if not power_machine_names:
        return []
    first_power_grp_idx = len(groups)
    power_area = sum(_node_padded_area(nodes[u]) for u in power_machine_names)
    if power_area <= single_core_capacity * _SCC_ATOMIC_FRAC:
        _extend_domain_groups(groups, grp_domains, [power_machine_names], "power")
        atomic_first.add(power_machine_names[0])
        return []
    if _LARGE_GRID_W <= grid_w < _SEVEN_GRID_W:
        _extend_domain_groups(
            groups,
            grp_domains,
            _chunk_machines(nodes, power_machine_names, 10, grid_w, has_cycles),
            "power",
        )
        return []
    triad_groups = _build_power_triad_groups(
        nodes, power_machine_names, mach_adj, grid_w, has_cycles
    )
    if grid_w < _LARGE_GRID_W or (first_power_grp_idx == 0 and not has_lab_groups):
        _extend_domain_groups(groups, grp_domains, triad_groups, "power")
        return []
    return list(triad_groups)


def _append_dedicated_and_terminal_groups(
    nodes: dict[str, PlacerNode],
    dedicated_by_out: dict[str, list[str]],
    lab_acyclic: list[str],
    power_machine_names: list[str],
    mach_adj: dict[str, dict[str, float]],
    single_core_capacity: float,
    has_labs: bool,
    grid_w: int,
    has_cycles: bool,
    groups: list[list[str]],
    grp_domains: list[str],
    atomic_first: set[str],
) -> tuple[list[list[str]], list[list[str]]]:
    """Append dedicated science, lab, and power domain groups."""
    _append_dedicated_output_groups(
        nodes,
        dedicated_by_out,
        single_core_capacity,
        has_labs,
        grid_w,
        has_cycles,
        groups,
        grp_domains,
        atomic_first,
    )
    lab_groups: list[list[str]] = []
    if lab_acyclic:
        lab_sorted = _sort_machines_by_flow(nodes, lab_acyclic)
        if grid_w < _LARGE_GRID_W and len(lab_sorted) <= _MAX_GRP_LEN:
            _extend_domain_groups(groups, grp_domains, [lab_sorted], "lab")
            atomic_first.add(lab_sorted[0])
        else:
            lab_groups.extend(
                _chunk_machines(nodes, lab_sorted, _LABS_PER_HALF_BLK, grid_w, has_cycles)
            )
    power_groups = _append_power_domain_groups(
        nodes,
        power_machine_names,
        mach_adj,
        single_core_capacity,
        grid_w,
        has_cycles,
        bool(lab_groups),
        groups,
        grp_domains,
        atomic_first,
    )
    return lab_groups, power_groups


def _seg_mix_penalty(
    p_idx: int,
    i_idx: int,
    groups: list[list[str]],
    grp_domains: list[str],
    grp_recipes: list[str],
    has_labs: bool,
    grid_w: int,
) -> float:
    """Compute domain and recipe mixing penalty for DP segment [p_idx, i_idx)."""
    if not has_labs or grid_w < _LARGE_GRID_W or i_idx <= p_idx + 1:
        return 0.0
    if grp_domains[p_idx] != grp_domains[i_idx - 1]:
        return _DOMAIN_MIX_PEN
    n_recs = len(set(grp_recipes[p_idx:i_idx]))
    if n_recs <= 1:
        return 0.0
    if grp_domains[p_idx] == "furnace":
        return _LARGE_RECIPE_MIX_PEN * float(n_recs - 1)
    total_machs = sum(len(groups[g_idx]) for g_idx in range(p_idx, i_idx))
    rec_pen = _SMALL_RECIPE_MIX_PEN if total_machs <= _MAX_GRP_LEN else _LARGE_RECIPE_MIX_PEN
    return rec_pen * float(n_recs - 1)


def _find_best_dp_split(
    b_idx: int,
    i: int,
    dp_prev: list[tuple[float, float, int]],
    pref: list[float],
    groups: list[list[str]],
    grp_domains: list[str],
    grp_recipes: list[str],
    has_labs: bool,
    grid_w: int,
) -> tuple[float, float, int]:
    """Find optimal predecessor cut p for DP state (b_idx, i)."""
    best_val = (float("inf"), float("inf"), -1)
    for p in range(b_idx - 1, i):
        prev_max, prev_sq, _ = dp_prev[p]
        if prev_max == float("inf"):
            continue
        seg_w = (pref[i] - pref[p]) + _seg_mix_penalty(
            p, i, groups, grp_domains, grp_recipes, has_labs, grid_w
        )
        cand_max = max(prev_max, seg_w)
        cand_sq = prev_sq + seg_w * seg_w
        cand_key = (round(cand_max, 2), round(cand_sq, 2))
        best_key = (round(best_val[0], 2), round(best_val[1], 2))
        if (cand_key <= best_key) if has_labs else (cand_key < best_key):
            best_val = (cand_max, cand_sq, p)
    return best_val


def _solve_dp_group_segments(
    groups: list[list[str]],
    grp_domains: list[str],
    grp_recipes: list[str],
    pref: list[float],
    k_bins: int,
    has_labs: bool,
    grid_w: int,
) -> list[list[str]]:
    """Partition ordered machine groups into k_bins contiguous segments with DP."""
    num_groups = len(groups)
    inf_tuple = (float("inf"), float("inf"), -1)
    dp: list[list[tuple[float, float, int]]] = [
        [inf_tuple] * (num_groups + 1) for _ in range(k_bins + 1)
    ]
    for i in range(1, num_groups - k_bins + 2):
        w = pref[i] + _seg_mix_penalty(0, i, groups, grp_domains, grp_recipes, has_labs, grid_w)
        dp[1][i] = (w, w * w, 0)
    for b_idx in range(2, k_bins + 1):
        max_i = num_groups - (k_bins - b_idx)
        for i in range(b_idx, max_i + 1):
            dp[b_idx][i] = _find_best_dp_split(
                b_idx, i, dp[b_idx - 1], pref, groups, grp_domains, grp_recipes, has_labs, grid_w
            )

    seg_bounds: list[tuple[int, int]] = []
    curr_i = num_groups
    for b_idx in range(k_bins, 0, -1):
        prev_i = dp[b_idx][curr_i][2]
        seg_bounds.append((prev_i, curr_i))
        curr_i = prev_i
    seg_bounds.reverse()
    return [[u for grp in groups[g_start:g_end] for u in grp] for g_start, g_end in seg_bounds]


def _append_merged_bin_groups(
    dp_buckets: list[list[str]],
    extra_groups: list[list[str]],
    n_bins: int,
) -> None:
    """Append extra_groups into dp_buckets, merging overflow into n_bins buckets."""
    if not extra_groups or n_bins <= 0:
        return
    if len(extra_groups) <= n_bins:
        dp_buckets.extend(extra_groups)
        return
    merged: list[list[str]] = [[] for _ in range(n_bins)]
    for idx_g, grp in enumerate(extra_groups):
        merged[min(n_bins - 1, idx_g)].extend(grp)
    dp_buckets.extend(merged)


def _node_blk_pt(nd: PlacerNode) -> tuple[int, float]:
    """Return effective (bx, by) block point, shifting matrix nodes into South bay."""
    ub = nd.assigned_block
    return (ub[0], (float(ub[1]) - _HALF) if nd.is_matrix_node else float(ub[1]))


def _visit_boundary_or_assigned_node(
    cur: str,
    adj_map: dict[str, list[str]],
    pin_nodes: set[str],
    pin_x: int,
    ref_by: float,
    nodes: dict[str, PlacerNode],
    assigned: set[str],
    block_area: dict[tuple[int, int], float],
    found: list[tuple[int, float]],
    stack: list[str],
) -> None:
    """Process one visited node during boundary or assigned block search."""
    if cur in assigned and nodes[cur].assigned_block in block_area:
        found.append(_node_blk_pt(nodes[cur]))
        return
    if cur not in nodes:
        return
    if cur in pin_nodes:
        found.append((pin_x, float(ref_by)))
    stack.extend(adj_map.get(cur, []))


def _find_boundary_or_assigned_pts(
    start_u: str,
    adj_map: dict[str, list[str]],
    pin_nodes: set[str],
    pin_x: int,
    ref_by: float,
    nodes: dict[str, PlacerNode],
    assigned: set[str],
    block_area: dict[tuple[int, int], float],
) -> list[tuple[int, float]]:
    """Traverse adj_map from start_u to collect assigned block points and boundary pins."""
    found: list[tuple[int, float]] = []
    if start_u in pin_nodes:
        found.append((pin_x, float(ref_by)))
    seen: set[str] = {start_u}
    stack = list(adj_map.get(start_u, []))
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            _visit_boundary_or_assigned_node(
                cur,
                adj_map,
                pin_nodes,
                pin_x,
                ref_by,
                nodes,
                assigned,
                block_area,
                found,
                stack,
            )
    return found


def _merger_block_sort_key(
    b: tuple[int, int],
    is_mat: bool,
    grid_w: int,
    grid_h: int,
    drv_blks_len: int,
    counts: collections.Counter[tuple[int, int]],
    ref_pts: list[tuple[int, float]],
    down_pts: list[tuple[int, float]],
    down_wt: float,
    bay_counts: dict[tuple[int, int], int],
    block_area: dict[tuple[int, int], float],
    blk_to_seq_idx: dict[tuple[int, int], int],
) -> tuple[int, int, int, float, int, float | int, int]:
    """Compute candidate block evaluation tuple for merger block assignment."""
    south_row_pen = 1 if (is_mat and grid_h >= 2 and b[1] == 0) else 0
    bay_tier = (bay_counts[b] // _BAY_CAP) if is_mat else 0
    t1 = bay_tier if (is_mat and grid_w >= _LARGE_GRID_W) else south_row_pen
    t2 = south_row_pen if grid_w >= _LARGE_GRID_W else bay_tier
    maj = 0 if (not is_mat and counts[b] > drv_blks_len // 2) else 1
    by_eff = (float(b[1]) - _HALF) if is_mat else float(b[1])
    wl = (
        sum(abs(b[0] - dpt[0]) + abs(by_eff - dpt[1]) for dpt in ref_pts)
        + down_wt * sum(abs(b[0] - spt[0]) + abs(by_eff - spt[1]) for spt in down_pts)
        + (_BAY_COUNT_PEN * bay_counts[b] if is_mat else 0.0)
    )
    return (
        t1,
        t2,
        maj,
        round(wl, _ROUND_3),
        -counts[b],
        bay_counts[b] if is_mat else block_area[b],
        blk_to_seq_idx[b],
    )


def _splitter_wirelength_score(
    b: tuple[int, int],
    is_mat: bool,
    is_bal: bool,
    in_pin: bool,
    grid_w: int,
    ref_pts: list[tuple[int, float]],
    up_pts: list[tuple[int, float]],
    up_wt: float,
    bay_counts: dict[tuple[int, int], int],
) -> float:
    """Compute weighted Manhattan wirelength score for a candidate splitter block."""
    by_eff = (float(b[1]) - _HALF) if is_mat else float(b[1])
    wl = (
        sum(abs(b[0] - spt[0]) + abs(by_eff - spt[1]) for spt in ref_pts)
        + up_wt * sum(abs(b[0] - upt[0]) + abs(by_eff - upt[1]) for upt in up_pts)
        + (_BAY_COUNT_PEN * bay_counts[b] if is_mat else 0.0)
    )
    if grid_w >= _LARGE_GRID_W and grid_w != _SEVEN_GRID_W and is_bal and in_pin:
        wl += _HALF * float(b[0])
    return round(wl, _ROUND_3)


def _splitter_tier_penalties(
    b: tuple[int, int],
    is_mat: bool,
    is_bal: bool,
    in_pin: bool,
    grid_w: int,
    grid_h: int,
    total_bals: int,
    bay_counts: dict[tuple[int, int], int],
    block_bins: dict[tuple[int, int], list[str]],
    nodes: dict[str, PlacerNode],
) -> tuple[int, int, int, int]:
    """Compute primary tier penalties (t1, t2, t3, t4) for splitter block selection."""
    south_pen = 1 if (is_mat and grid_h >= 2 and b[1] == 0) else 0
    bay_tier = (bay_counts[b] // _BAY_CAP) if is_mat else 0
    if grid_w < _LARGE_GRID_W:
        return south_pen, 0, 0, bay_tier
    t1 = bay_tier if is_mat else south_pen
    t2 = b[0] if (is_bal and in_pin and total_bals <= grid_h + 2) else 0
    t3 = sum(1 for x in block_bins[b] if nodes[x].node_type == "balancer") if is_bal else 0
    t4 = 1 if (is_mat and not is_bal and south_pen) else 0
    return t1, t2, t3, t4


def _splitter_block_sort_key(
    b: tuple[int, int],
    is_mat: bool,
    is_bal: bool,
    in_pin: bool,
    grid_w: int,
    grid_h: int,
    total_bals: int,
    snk_blks_len: int,
    counts: collections.Counter[tuple[int, int]],
    ref_pts: list[tuple[int, float]],
    up_pts: list[tuple[int, float]],
    up_wt: float,
    bay_counts: dict[tuple[int, int], int],
    block_bins: dict[tuple[int, int], list[str]],
    nodes: dict[str, PlacerNode],
    block_area: dict[tuple[int, int], float],
    blk_to_seq_idx: dict[tuple[int, int], int],
) -> tuple[int, int, int, int, int, float, int, int, float | int, int]:
    """Compute candidate block evaluation tuple for splitter or balancer assignment."""
    t1, t2, t3, t4 = _splitter_tier_penalties(
        b, is_mat, is_bal, in_pin, grid_w, grid_h, total_bals, bay_counts, block_bins, nodes
    )
    maj = 0 if (not is_mat and counts[b] > snk_blks_len // 2) else 1
    wl = _splitter_wirelength_score(
        b, is_mat, is_bal, in_pin, grid_w, ref_pts, up_pts, up_wt, bay_counts
    )
    t_bx = (
        b[0] if (grid_w >= _LARGE_GRID_W and grid_w != _SEVEN_GRID_W and is_bal and in_pin) else 0
    )
    return (
        t1,
        t2,
        t3,
        t4,
        maj,
        wl,
        t_bx,
        -counts[b],
        bay_counts[b] if is_mat else block_area[b],
        blk_to_seq_idx[b],
    )


def _matrix_bay_wirelength(b: tuple[int, int], pts: list[tuple[int, float]]) -> float:
    """Compute Manhattan wirelength from matrix bay of block b to connected points."""
    by_f = float(b[1]) - _HALF
    return sum(abs(b[0] - pt[0]) + abs(by_f - pt[1]) for pt in pts)


def _try_relocate_matrix_splitter(
    s_name: str,
    nodes: dict[str, PlacerNode],
    active_blocks: list[tuple[int, int]],
    in_drivers: dict[str, list[str]],
    out_sinks: dict[str, list[str]],
    assigned: set[str],
    block_bins: dict[tuple[int, int], list[str]],
    block_area: dict[tuple[int, int], float],
    bay_counts: dict[tuple[int, int], int],
    blk_to_seq_idx: dict[tuple[int, int], int],
) -> None:
    """Relocate one matrix splitter to an intermediate bay when vertical U-turn is reduced."""
    cur_blk = nodes[s_name].assigned_block
    drv_pts = [
        _node_blk_pt(nodes[u])
        for u in in_drivers.get(s_name, [])
        if u in assigned and nodes[u].assigned_block in block_area
    ]
    snk_pts = [
        _node_blk_pt(nodes[v])
        for v in out_sinks.get(s_name, [])
        if v in assigned and nodes[v].assigned_block in block_area
    ]
    if not drv_pts or len(snk_pts) < 2:
        return
    min_sy = min(pt[1] for pt in snk_pts)
    max_sy = max(pt[1] for pt in snk_pts)
    if max_sy - min_sy < 2.0:
        return
    all_nbr_pts = drv_pts + snk_pts
    cur_wl = _matrix_bay_wirelength(cur_blk, all_nbr_pts)
    between_cands = [
        b
        for b in active_blocks
        if min_sy < (float(b[1]) - _HALF) < max_sy
        and (bay_counts[b] < _BAY_CAP if b != cur_blk else True)
    ]
    if not between_cands:
        return
    best_cand = min(
        between_cands,
        key=lambda b: (
            round(_matrix_bay_wirelength(b, all_nbr_pts), _ROUND_3),
            bay_counts[b] if b != cur_blk else max(0, bay_counts[b] - 1),
            blk_to_seq_idx[b],
        ),
    )
    if best_cand != cur_blk and _matrix_bay_wirelength(best_cand, all_nbr_pts) <= cur_wl - 1.0:
        u_area = _node_padded_area(nodes[s_name])
        block_bins[cur_blk].remove(s_name)
        block_area[cur_blk] -= u_area
        bay_counts[cur_blk] -= 1
        nodes[s_name].assigned_block = best_cand
        block_bins[best_cand].append(s_name)
        block_area[best_cand] += u_area
        bay_counts[best_cand] += 1


def _bfs_same_recipe_comp(
    start: str,
    adj: dict[str, set[str]],
    seen: set[str],
) -> list[str]:
    """Run BFS from start to collect one connected component of same-recipe nodes."""
    comp: list[str] = []
    q = collections.deque([start])
    seen.add(start)
    while q:
        cur = q.popleft()
        comp.append(cur)
        for nb in sorted(adj.get(cur, ()), key=_natural_key):
            if nb not in seen:
                seen.add(nb)
                q.append(nb)
    return comp


def _find_same_recipe_components(
    names: list[str],
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
) -> list[list[str]]:
    """Find connected components of same-recipe tree nodes in deterministic BFS order."""
    name_set = set(names)
    adj: dict[str, set[str]] = collections.defaultdict(set)
    for e in edges:
        if (
            e.source in name_set
            and e.sink in name_set
            and nodes[e.source].recipe == nodes[e.sink].recipe
        ):
            adj[e.source].add(e.sink)
            adj[e.sink].add(e.source)

    seen: set[str] = set()
    return [
        _bfs_same_recipe_comp(start, adj, seen)
        for start in sorted(names, key=_natural_key)
        if start not in seen
    ]


def _extract_merge_out_wires(
    comp: list[str],
    in_edges: dict[str, list[PlacerEdge]],
    out_edges: dict[str, list[PlacerEdge]],
) -> dict[str, str] | None:
    """Return output wire map if every merger in comp has two distinct inputs and one output."""
    out_w: dict[str, str] = {}
    for m in comp:
        ow_set = {e.wire for e in out_edges.get(m, [])}
        iw_list = [e.wire for e in in_edges.get(m, [])]
        if len(ow_set) != 1 or len(iw_list) != 2 or len(set(iw_list)) != 2:
            return None
        out_w[m] = next(iter(ow_set))
    return out_w


def _validate_merge_component(
    comp: list[str],
    nodes: dict[str, PlacerNode],
    in_edges: dict[str, list[PlacerEdge]],
    out_edges: dict[str, list[PlacerEdge]],
    assigned: set[str],
) -> tuple[str, str, tuple[str, str], list[tuple[str, str]], list[PlacerEdge]] | None:
    """Validate a binary merger tree component and extract its root, internal pool, and leaves."""
    out_w = _extract_merge_out_wires(comp, in_edges, out_edges)
    if out_w is None:
        return None
    comp_set = set(comp)
    internal_m_set = {m for m in comp if any(e.sink in comp_set for e in out_edges.get(m, []))}
    root_m = [m for m in comp if m not in internal_m_set]
    leaf_edges = [e for m in comp for e in in_edges.get(m, []) if e.source not in comp_set]
    if len(root_m) != 1 or len(internal_m_set) != len(comp) - 1 or len(leaf_edges) != len(comp) + 1:
        return None
    if len({e.wire for e in leaf_edges}) != len(leaf_edges) or not all(
        e.source in assigned and e.source in nodes for e in leaf_edges
    ):
        return None

    avail_pairs = [(m, out_w[m]) for m in sorted(internal_m_set, key=_natural_key)]
    root_pair = (root_m[0], out_w[root_m[0]])
    r_wire = out_w[root_m[0]]
    r_key = nodes[root_m[0]].recipe or r_wire.split("__")[0]
    return r_key, r_wire, root_pair, avail_pairs, leaf_edges


def _extract_split_in_wires(
    comp: list[str],
    in_edges: dict[str, list[PlacerEdge]],
    out_edges: dict[str, list[PlacerEdge]],
) -> dict[str, str] | None:
    """Return input wire map if every splitter in comp has one input and two outputs."""
    in_w: dict[str, str] = {}
    for s in comp:
        iw_set = {e.wire for e in in_edges.get(s, [])}
        ow_set = {e.wire for e in out_edges.get(s, [])}
        if len(iw_set) != 1 or len(ow_set) != 2:
            return None
        in_w[s] = next(iter(iw_set))
    return in_w


def _validate_split_component(
    comp: list[str],
    nodes: dict[str, PlacerNode],
    in_edges: dict[str, list[PlacerEdge]],
    out_edges: dict[str, list[PlacerEdge]],
    assigned: set[str],
) -> tuple[str, str, list[str], set[str], list[PlacerEdge]] | None:
    """Validate a binary splitter tree component and extract root wire, wires, and leaves."""
    in_w = _extract_split_in_wires(comp, in_edges, out_edges)
    if in_w is None:
        return None
    comp_set = set(comp)
    int_wires = {e.wire for s in comp for e in out_edges.get(s, []) if e.sink in comp_set}
    root_s = [s for s in comp if in_w[s] not in int_wires]
    leaf_edges = [e for s in comp for e in out_edges.get(s, []) if e.sink not in comp_set]
    if len(root_s) != 1 or len(int_wires) != len(comp) - 1 or len(leaf_edges) != len(comp) + 1:
        return None
    if len({e.wire for e in leaf_edges}) != len(leaf_edges) or not all(
        e.sink in assigned and e.sink in nodes for e in leaf_edges
    ):
        return None

    r_wire = in_w[root_s[0]]
    r_key = nodes[root_s[0]].recipe or r_wire.split("__")[0]
    return r_key, r_wire, comp, int_wires, leaf_edges


def _snap_rate_cut_to_block(
    best_c: int,
    min_c: int,
    max_c: int,
    prev_c: int,
    rem_trees: int,
    total_w: float,
    cap_w: float,
    ideal_slice_w: float,
    prefix_w: list[float],
    blk_bounds: list[int],
) -> int:
    """Snap a rate-balanced splitter cut index to the nearest valid block boundary."""
    ordered_bounds = sorted(blk_bounds, key=lambda x: abs(x - best_c))
    for b_cut in ordered_bounds:
        if min_c <= b_cut <= max_c:
            b_slice_w = prefix_w[b_cut] - prefix_w[prev_c]
            b_rem_avg = (total_w - prefix_w[b_cut]) / float(rem_trees)
            if (
                b_slice_w <= cap_w + _EPS
                and b_rem_avg <= cap_w + _EPS
                and abs(b_slice_w - ideal_slice_w) <= _SLICE_DEV_FRAC * ideal_slice_w
            ):
                return b_cut
    return best_c


def _compute_rate_splitter_cuts(
    all_leaves: list[PlacerEdge],
    blk_bounds: list[int],
    k_trees: int,
    sink_wire_rates: dict[str, float],
    belt_cap: float,
) -> tuple[list[int], list[float]]:
    """Compute rate-balanced leaf slice cuts and per-slice demands across k_trees."""
    total_s = len(all_leaves)
    leaf_w = [max(_EPS, sink_wire_rates.get(e.wire, 1.0)) for e in all_leaves]
    prefix_w = [0.0]
    for w in leaf_w:
        prefix_w.append(prefix_w[-1] + w)
    total_w = prefix_w[-1]
    cuts = [0]
    b_cap = max(1.0, belt_cap)
    for t_idx in range(k_trees - 1):
        prev_c = cuts[-1]
        rem_trees = k_trees - 1 - t_idx
        min_c = prev_c + 2
        max_c = total_s - 2 * rem_trees
        rem_w = max(_EPS, total_w - prefix_w[prev_c])
        ideal_slice_w = rem_w / float(rem_trees + 1)
        cap_w = max(b_cap * _BELT_CAP_SAFETY, ideal_slice_w * _SLICE_CAP_HEADROOM)
        best_c = min(
            range(min_c, max_c + 1),
            key=lambda c: (
                0
                if (
                    (prefix_w[c] - prefix_w[prev_c]) <= cap_w + _EPS
                    and (total_w - prefix_w[c]) / float(rem_trees) <= cap_w + _EPS
                )
                else 1,
                round(abs((prefix_w[c] - prefix_w[prev_c]) - ideal_slice_w), _ROUND_4),
                abs(c - (prev_c + max(2, round((total_s - prev_c) / float(rem_trees + 1))))),
            ),
        )
        snapped_c = _snap_rate_cut_to_block(
            best_c,
            min_c,
            max_c,
            prev_c,
            rem_trees,
            total_w,
            cap_w,
            ideal_slice_w,
            prefix_w,
            blk_bounds,
        )
        cuts.append(snapped_c)
    cuts.append(total_s)
    demands = [prefix_w[cuts[i + 1]] - prefix_w[cuts[i]] for i in range(k_trees)]
    return cuts, demands


def _snap_orig_cut_to_block(
    cum: int,
    prev_cut: int,
    rem_trees: int,
    total_s: int,
    max_b_size: int,
    blk_bounds: list[int],
) -> int:
    """Snap a cardinality-based splitter cut index to the nearest valid block boundary."""
    ordered_bounds = sorted(blk_bounds, key=lambda x: abs(x - cum))
    for b_cut in ordered_bounds:
        if (
            abs(b_cut - cum) <= _CUT_ORIG_TOL
            and 2 <= (b_cut - prev_cut) <= max_b_size
            and 2 * rem_trees <= (total_s - b_cut) <= max_b_size * rem_trees
        ):
            return b_cut
    return cum


def _compute_orig_splitter_cuts(
    orig_sizes: list[int],
    blk_bounds: list[int],
    total_s: int,
    k_trees: int,
) -> list[int]:
    """Compute cardinality-preserving leaf slice cuts snapped to block boundaries."""
    cuts = [0]
    cum = 0
    max_b_size = max(max(orig_sizes), _MAX_ORIG_SPLIT_SLICE)
    for t_idx in range(k_trees - 1):
        cum += orig_sizes[t_idx]
        rem_trees = k_trees - 1 - t_idx
        best_c = _snap_orig_cut_to_block(cum, cuts[-1], rem_trees, total_s, max_b_size, blk_bounds)
        cuts.append(best_c)
    cuts.append(total_s)
    return cuts


def _order_spatial_blk_items(
    blk_items: list[tuple[str, tuple[int, int]]],
) -> list[tuple[str, tuple[int, int]]]:
    """Order inter-block wire items along their dominant spatial axis."""
    xs = [b[0] for _, b in blk_items]
    ys = [b[1] for _, b in blk_items]
    if (max(xs) - min(xs)) >= (max(ys) - min(ys)):
        return sorted(blk_items, key=lambda it: (it[1][0], it[1][1], _natural_key(it[0])))
    return sorted(blk_items, key=lambda it: (it[1][1], it[1][0], _natural_key(it[0])))


def _build_merge_subtree(
    wires: list[str],
    avail_q: collections.deque[tuple[str, str]],
    root_pair: tuple[str, str],
    rewire_map: dict[str, dict[str, str]],
    *,
    use_root: bool = False,
) -> tuple[str, list[str]]:
    """Recursively build a balanced binary merger subtree over wires."""
    if len(wires) == 1:
        return wires[0], []
    if len(wires) == 2:
        m_name, y_w = root_pair if use_root else avail_q.popleft()
        rewire_map[m_name] = {"a": wires[0], "b": wires[1], "y": y_w}
        return y_w, [m_name]
    mid = (len(wires) + 1) // 2
    w_left, used_l = _build_merge_subtree(wires[:mid], avail_q, root_pair, rewire_map)
    w_right, used_r = _build_merge_subtree(wires[mid:], avail_q, root_pair, rewire_map)
    m_name, y_w = root_pair if use_root else avail_q.popleft()
    rewire_map[m_name] = {"a": w_left, "b": w_right, "y": y_w}
    return y_w, used_l + used_r + [m_name]


def _build_spatial_merge_subtree(
    blk_items: list[tuple[str, tuple[int, int]]],
    avail_q: collections.deque[tuple[str, str]],
    root_pair: tuple[str, str],
    rewire_map: dict[str, dict[str, str]],
    *,
    use_root: bool = False,
) -> tuple[str, list[str]]:
    """Recursively build a spatially partitioned inter-block merger subtree."""
    if len(blk_items) == 1:
        return blk_items[0][0], []
    if len(blk_items) == 2:
        m_name, y_w = root_pair if use_root else avail_q.popleft()
        rewire_map[m_name] = {"a": blk_items[0][0], "b": blk_items[1][0], "y": y_w}
        return y_w, [m_name]
    ordered = _order_spatial_blk_items(blk_items)
    mid = (len(ordered) + 1) // 2
    w_left, used_l = _build_spatial_merge_subtree(ordered[:mid], avail_q, root_pair, rewire_map)
    w_right, used_r = _build_spatial_merge_subtree(ordered[mid:], avail_q, root_pair, rewire_map)
    m_name, y_w = root_pair if use_root else avail_q.popleft()
    rewire_map[m_name] = {"a": w_left, "b": w_right, "y": y_w}
    return y_w, used_l + used_r + [m_name]


def _rewire_single_merge_tree(
    leaf_edges: list[PlacerEdge],
    avail_q: collections.deque[tuple[str, str]],
    root_pair: tuple[str, str],
    nodes: dict[str, PlacerNode],
    blk_to_seq_idx: dict[tuple[int, int], int],
    rewire_map: dict[str, dict[str, str]],
    assign_fn: BlockAssignFn,
) -> None:
    """Re-parenthesize one binary merger tree into block-local and matrix subtrees."""
    by_blk: dict[tuple[int, int], list[PlacerEdge]] = collections.defaultdict(list)
    for e in leaf_edges:
        by_blk[nodes[e.source].assigned_block].append(e)
    sorted_blks = sorted(by_blk.keys(), key=lambda b: blk_to_seq_idx.get(b, 0))
    for b in sorted_blks:
        by_blk[b].sort(
            key=lambda e: (
                nodes[e.source].rank,
                nodes[e.source].flow_depth,
                _natural_key(e.source),
                _natural_key(e.wire),
            )
        )

    if len(sorted_blks) == 1:
        only_b = sorted_blks[0]
        _rw, used_m = _build_merge_subtree(
            [e.wire for e in by_blk[only_b]], avail_q, root_pair, rewire_map, use_root=True
        )
        for m_name in used_m:
            nodes[m_name].is_matrix_node = False
            assign_fn(m_name, only_b)
        return

    blk_export_items: list[tuple[str, tuple[int, int]]] = []
    for b in sorted_blks:
        b_wires = [e.wire for e in by_blk[b]]
        exp_w, used_local = _build_merge_subtree(
            b_wires, avail_q, root_pair, rewire_map, use_root=False
        )
        blk_export_items.append((exp_w, b))
        for m_name in used_local:
            nodes[m_name].is_matrix_node = False
            assign_fn(m_name, b)
    _rw, used_mat = _build_spatial_merge_subtree(
        blk_export_items, avail_q, root_pair, rewire_map, use_root=True
    )
    for m_name in used_mat:
        nodes[m_name].is_matrix_node = True


def _snap_merger_cut_to_block(
    best_mc: int,
    min_mc: int,
    max_mc: int,
    prev_mc: int,
    d_cur: float,
    d_rem_after: float,
    total_m_w: float,
    m_prefix_w: list[float],
    m_blk_bounds: list[int],
) -> int:
    """Snap a multibus merger cut index to the nearest valid block boundary."""
    ordered_bounds = sorted(m_blk_bounds, key=lambda x: abs(x - best_mc))
    for b_mc in ordered_bounds:
        if (
            min_mc <= b_mc <= max_mc
            and abs(b_mc - best_mc) <= _CUT_SNAP_TOL
            and (m_prefix_w[b_mc] - m_prefix_w[prev_mc]) >= d_cur - _EPS
            and (total_m_w - m_prefix_w[b_mc]) >= d_rem_after - _EPS
        ):
            return b_mc
    return best_mc


def _compute_multibus_merger_cuts(
    all_m_leaves: list[PlacerEdge],
    m_blk_bounds: list[int],
    k_m: int,
    demands: list[float] | None,
    driver_wire_rates: dict[str, float],
) -> list[int]:
    """Compute rate-matched leaf slice cuts across k_m parallel merger trees."""
    total_m = len(all_m_leaves)
    m_w = [max(_EPS, driver_wire_rates.get(e.wire, 1.0)) for e in all_m_leaves]
    m_prefix_w = [0.0]
    for w in m_w:
        m_prefix_w.append(m_prefix_w[-1] + w)
    total_m_w = m_prefix_w[-1]
    eff_demands = demands if (demands and len(demands) == k_m) else [total_m_w / float(k_m)] * k_m
    m_cuts = [0]
    for t_idx in range(k_m - 1):
        prev_mc = m_cuts[-1]
        rem_m = k_m - 1 - t_idx
        min_mc = prev_mc + 2
        max_mc = total_m - 2 * rem_m
        d_cur = max(_EPS, eff_demands[t_idx])
        d_rem_after = sum(max(_EPS, d) for d in eff_demands[t_idx + 1 :])
        rem_sup = max(_EPS, total_m_w - m_prefix_w[prev_mc])
        ideal_sup = rem_sup * (d_cur / (d_cur + d_rem_after))
        best_mc = min(
            range(min_mc, max_mc + 1),
            key=lambda mc: (
                0
                if (
                    (m_prefix_w[mc] - m_prefix_w[prev_mc]) >= d_cur - _EPS
                    and (total_m_w - m_prefix_w[mc]) >= d_rem_after - _EPS
                )
                else 1
                if (m_prefix_w[mc] - m_prefix_w[prev_mc]) >= min(d_cur, ideal_sup) - _EPS
                else 2,
                round(abs((m_prefix_w[mc] - m_prefix_w[prev_mc]) - ideal_sup), _ROUND_4),
                mc,
            ),
        )
        snapped_mc = _snap_merger_cut_to_block(
            best_mc,
            min_mc,
            max_mc,
            prev_mc,
            d_cur,
            d_rem_after,
            total_m_w,
            m_prefix_w,
            m_blk_bounds,
        )
        m_cuts.append(snapped_mc)
    m_cuts.append(total_m)
    return m_cuts


def _build_split_subtree(
    in_wire: str,
    out_wires: list[str],
    avail_s: collections.deque[str],
    avail_w: collections.deque[str],
    rewire_map: dict[str, dict[str, str]],
) -> list[str]:
    """Recursively build a balanced binary splitter subtree from in_wire to out_wires."""
    if len(out_wires) < 2:
        return []
    s_name = avail_s.popleft()
    if len(out_wires) == 2:
        rewire_map[s_name] = {"a": in_wire, "y1": out_wires[0], "y2": out_wires[1]}
        return [s_name]
    mid = len(out_wires) // 2
    left_o = out_wires[:mid]
    right_o = out_wires[mid:]
    w_l = left_o[0] if len(left_o) == 1 else avail_w.popleft()
    w_r = right_o[0] if len(right_o) == 1 else avail_w.popleft()
    rewire_map[s_name] = {"a": in_wire, "y1": w_l, "y2": w_r}
    used = [s_name]
    if len(left_o) > 1:
        used.extend(_build_split_subtree(w_l, left_o, avail_s, avail_w, rewire_map))
    if len(right_o) > 1:
        used.extend(_build_split_subtree(w_r, right_o, avail_s, avail_w, rewire_map))
    return used


def _build_spatial_split_subtree(
    in_wire: str,
    blk_items: list[tuple[str, tuple[int, int]]],
    avail_s: collections.deque[str],
    avail_w: collections.deque[str],
    rewire_map: dict[str, dict[str, str]],
) -> list[str]:
    """Recursively build a spatially partitioned inter-block splitter subtree."""
    if len(blk_items) < 2:
        return []
    s_name = avail_s.popleft()
    if len(blk_items) == 2:
        rewire_map[s_name] = {"a": in_wire, "y1": blk_items[0][0], "y2": blk_items[1][0]}
        return [s_name]
    ordered = _order_spatial_blk_items(blk_items)
    mid = len(ordered) // 2
    left_o = ordered[:mid]
    right_o = ordered[mid:]
    w_l = left_o[0][0] if len(left_o) == 1 else avail_w.popleft()
    w_r = right_o[0][0] if len(right_o) == 1 else avail_w.popleft()
    rewire_map[s_name] = {"a": in_wire, "y1": w_l, "y2": w_r}
    used = [s_name]
    if len(left_o) > 1:
        used.extend(_build_spatial_split_subtree(w_l, left_o, avail_s, avail_w, rewire_map))
    if len(right_o) > 1:
        used.extend(_build_spatial_split_subtree(w_r, right_o, avail_s, avail_w, rewire_map))
    return used


def _rewire_one_split_slice(
    r_wire: str,
    b_leaves: list[PlacerEdge],
    avail_s: collections.deque[str],
    avail_w: collections.deque[str],
    nodes: dict[str, PlacerNode],
    rewire_map: dict[str, dict[str, str]],
    assign_fn: BlockAssignFn,
) -> None:
    """Re-parenthesize one splitter slice into block-local and matrix subtrees."""
    by_b: dict[tuple[int, int], list[PlacerEdge]] = collections.defaultdict(list)
    for e in b_leaves:
        by_b[nodes[e.sink].assigned_block].append(e)
    b_blks = list(by_b.keys())

    if len(b_blks) == 1:
        only_b = b_blks[0]
        used_s = _build_split_subtree(
            r_wire, [e.wire for e in b_leaves], avail_s, avail_w, rewire_map
        )
        for s_name in used_s:
            nodes[s_name].is_matrix_node = False
            assign_fn(s_name, only_b)
        return

    blk_feed_items: list[tuple[str, tuple[int, int]]] = []
    for b in b_blks:
        bw = [e.wire for e in by_b[b]]
        if len(bw) == 1:
            blk_feed_items.append((bw[0], b))
            continue
        fw = avail_w.popleft()
        blk_feed_items.append((fw, b))
        for s_name in _build_split_subtree(fw, bw, avail_s, avail_w, rewire_map):
            nodes[s_name].is_matrix_node = False
            assign_fn(s_name, b)
    for s_name in _build_spatial_split_subtree(
        r_wire, blk_feed_items, avail_s, avail_w, rewire_map
    ):
        nodes[s_name].is_matrix_node = True


def _collect_non_rewired_endpoints(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
    rewire_map: dict[str, dict[str, str]],
) -> tuple[
    dict[str, tuple[float, bool]],
    dict[str, tuple[str, float]],
    dict[str, list[tuple[str, float]]],
    list[str],
]:
    """Collect wire metadata, drivers, and sinks from edges outside rewired tree ports."""
    wire_meta: dict[str, tuple[float, bool]] = {}
    wire_drv: dict[str, tuple[str, float]] = {}
    wire_snks: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
    wire_order: list[str] = []

    for e in edges:
        if e.wire not in wire_meta:
            wire_meta[e.wire] = (e.weight, e.is_fluid)
            wire_order.append(e.wire)
        if not (e.source in rewire_map and nodes[e.source].node_type in ("merge", "split")):
            wire_drv[e.wire] = (e.source, e.source_dy)
        if not (e.sink in rewire_map and nodes[e.sink].node_type in ("merge", "split")):
            pair = (e.sink, e.sink_dy)
            if pair not in wire_snks[e.wire]:
                wire_snks[e.wire].append(pair)
    return wire_meta, wire_drv, wire_snks, wire_order


def _register_rewired_node_ports(
    nodes: dict[str, PlacerNode],
    rewire_map: dict[str, dict[str, str]],
    wire_drv: dict[str, tuple[str, float]],
    wire_snks: dict[str, list[tuple[str, float]]],
) -> None:
    """Apply rewired merger and splitter port assignments to nodes and wire endpoint maps."""
    for iname, pmap in rewire_map.items():
        nd = nodes[iname]
        if nd.node_type == "merge":
            nd.inputs = [pmap["a"], pmap["b"]]
            nd.outputs = [pmap["y"]]
            wire_snks[pmap["a"]].append((iname, 0.0))
            wire_snks[pmap["b"]].append((iname, 1.0))
            wire_drv[pmap["y"]] = (iname, _HALF)
        elif nd.node_type == "split":
            nd.inputs = [pmap["a"]]
            nd.outputs = [pmap["y1"], pmap["y2"]]
            wire_snks[pmap["a"]].append((iname, _HALF))
            wire_drv[pmap["y1"]] = (iname, 0.0)
            wire_drv[pmap["y2"]] = (iname, 1.0)


def _apply_rewire_map_to_graph(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
    rewire_map: dict[str, dict[str, str]],
) -> None:
    """Update nodes and edges in place from rewire_map port assignments."""
    if not rewire_map:
        return
    wire_meta, wire_drv, wire_snks, wire_order = _collect_non_rewired_endpoints(
        nodes, edges, rewire_map
    )
    _register_rewired_node_ports(nodes, rewire_map, wire_drv, wire_snks)

    new_edges: list[PlacerEdge] = []
    for w in wire_order:
        if w not in wire_drv:
            continue
        drv, d_dy = wire_drv[w]
        w_wt, w_fl = wire_meta.get(w, (1.0, False))
        for snk, s_dy in wire_snks.get(w, []):
            if drv != snk:
                new_edges.append(
                    PlacerEdge(
                        source=drv,
                        sink=snk,
                        wire=w,
                        weight=w_wt,
                        source_dy=d_dy,
                        sink_dy=s_dy,
                        is_fluid=w_fl,
                    )
                )
    edges[:] = new_edges


def _accumulate_x_laplacian_edge(
    e: PlacerEdge,
    block_nodes: list[PlacerNode],
    name_to_idx: dict[str, int],
    x_lo: float,
    x_hi: float,
    a_x: np.ndarray,
    b_x: np.ndarray,
) -> None:
    """Add one directed edge contribution to the X Laplacian system."""
    u_idx = name_to_idx.get(e.source)
    v_idx = name_to_idx.get(e.sink)
    w = float(e.weight)
    if u_idx is not None and v_idx is not None and u_idx != v_idx:
        u_node = block_nodes[u_idx]
        v_node = block_nodes[v_idx]
        delta_x = float(u_node.width + u_node.pad_e + v_node.pad_w)
        if u_node.col_index > v_node.col_index:
            delta_x = -delta_x
        a_x[u_idx, u_idx] += w
        a_x[v_idx, v_idx] += w
        a_x[u_idx, v_idx] -= w
        a_x[v_idx, u_idx] -= w
        b_x[u_idx] -= w * delta_x
        b_x[v_idx] += w * delta_x
    elif u_idx is None and v_idx is not None:
        a_x[v_idx, v_idx] += w * _HALF
        b_x[v_idx] += w * _HALF * (x_lo + _BOUNDARY_ANCHOR_INSET)
    elif u_idx is not None and v_idx is None:
        a_x[u_idx, u_idx] += w * _HALF
        b_x[u_idx] += w * _HALF * (x_hi - _BOUNDARY_ANCHOR_INSET)


def _solve_continuous_x(
    block_nodes: list[PlacerNode],
    edges: list[PlacerEdge],
    name_to_idx: dict[str, int],
    x_lo: float,
    x_hi: float,
    usable_w: float,
) -> np.ndarray:
    """Solve directed quadratic X coordinates inside one core."""
    n = len(block_nodes)
    max_col = max((node.col_index for node in block_nodes), default=0)
    x_depth_target = np.zeros(n, dtype=float)
    denom = float(max(1, max_col + 1))
    for i, node in enumerate(block_nodes):
        frac_x = (node.col_index + _HALF) / denom
        x_depth_target[i] = x_lo + frac_x * usable_w

    a_x = np.eye(n, dtype=float) * _REG_X
    b_x = _REG_X * x_depth_target.copy()
    for e in edges:
        _accumulate_x_laplacian_edge(e, block_nodes, name_to_idx, x_lo, x_hi, a_x, b_x)

    try:
        return np.linalg.solve(a_x, b_x)
    except np.linalg.LinAlgError:
        return x_depth_target


def _sweep_y_band_column(
    col_indices: list[int],
    block_nodes: list[PlacerNode],
    target_types: set[str],
    adj_map: dict[int, list[int]],
    y_band_target: np.ndarray,
) -> None:
    """Update y_band_target for nodes in col_indices matching target_types."""
    for idx in col_indices:
        if block_nodes[idx].node_type in target_types and adj_map[idx]:
            y_band_target[idx] = float(np.mean([y_band_target[nb] for nb in adj_map[idx]]))


def _propagate_y_band_centroids(
    block_nodes: list[PlacerNode],
    by_col: dict[int, list[int]],
    adj_sinks: dict[int, list[int]],
    adj_drivers: dict[int, list[int]],
    y_band_target: np.ndarray,
) -> None:
    """Propagate vertical band centroids to splitters and mergers."""
    rev_cols = sorted(by_col.keys(), reverse=True)
    fwd_cols = sorted(by_col.keys())
    split_types = {"split", "balancer"}
    merge_types = {"merge", "combinator", "display"}
    for _ in range(_CONTINUOUS_Y_PASSES):
        for col_id in rev_cols:
            _sweep_y_band_column(by_col[col_id], block_nodes, split_types, adj_sinks, y_band_target)
        for col_id in fwd_cols:
            _sweep_y_band_column(
                by_col[col_id],
                block_nodes,
                merge_types,
                adj_drivers,
                y_band_target,
            )


def _init_y_band_targets(
    block_nodes: list[PlacerNode],
    by_col: dict[int, list[int]],
    y_lo: float,
    y_hi: float,
    usable_h: float,
) -> np.ndarray:
    """Initialize column-ordered vertical band targets for block_nodes."""
    n = len(block_nodes)
    y_band_target = np.full(n, _HALF * (y_lo + y_hi), dtype=float)
    recipes = sorted({node.recipe for node in block_nodes if node.recipe})
    r_denom = float(max(1, len(recipes)))
    recipe_band = {r_name: (r_idx + _HALF) / r_denom for r_idx, r_name in enumerate(recipes)}

    for col_id in sorted(by_col.keys()):
        col_indices = by_col[col_id]
        col_indices.sort(
            key=lambda idx: (
                recipe_band.get(block_nodes[idx].recipe, _HALF),
                _natural_key(block_nodes[idx].name),
            )
        )
        m_count = float(max(1, len(col_indices)))
        for rank_in_col, idx in enumerate(col_indices):
            frac_y = (rank_in_col + _HALF) / m_count
            y_band_target[idx] = y_lo + frac_y * usable_h
    return y_band_target


def _solve_continuous_y(
    block_nodes: list[PlacerNode],
    edges: list[PlacerEdge],
    name_to_idx: dict[str, int],
    y_lo: float,
    y_hi: float,
    usable_h: float,
) -> np.ndarray:
    """Solve port-aware quadratic Y coordinates inside one core."""
    n = len(block_nodes)
    by_col: dict[int, list[int]] = collections.defaultdict(list)
    for i, node in enumerate(block_nodes):
        by_col[node.col_index].append(i)

    y_band_target = _init_y_band_targets(block_nodes, by_col, y_lo, y_hi, usable_h)

    adj_sinks: dict[int, list[int]] = collections.defaultdict(list)
    adj_drivers: dict[int, list[int]] = collections.defaultdict(list)
    a_y = np.eye(n, dtype=float) * _REG_Y
    for e in edges:
        u_idx = name_to_idx.get(e.source)
        v_idx = name_to_idx.get(e.sink)
        if u_idx is not None and v_idx is not None and u_idx != v_idx:
            adj_sinks[u_idx].append(v_idx)
            adj_drivers[v_idx].append(u_idx)

    _propagate_y_band_centroids(block_nodes, by_col, adj_sinks, adj_drivers, y_band_target)
    b_y = _REG_Y * y_band_target.copy()

    for e in edges:
        u_idx = name_to_idx.get(e.source)
        v_idx = name_to_idx.get(e.sink)
        if u_idx is not None and v_idx is not None and u_idx != v_idx:
            w = float(e.weight)
            port_diff = float(e.source_dy) - float(e.sink_dy)
            a_y[u_idx, u_idx] += w
            a_y[v_idx, v_idx] += w
            a_y[u_idx, v_idx] -= w
            a_y[v_idx, u_idx] -= w
            b_y[u_idx] -= w * port_diff
            b_y[v_idx] += w * port_diff

    try:
        return np.linalg.solve(a_y, b_y)
    except np.linalg.LinAlgError:
        return y_band_target


def _pair_vertical_gap(
    nd: PlacerNode, nxt: PlacerNode, check_col_len: bool = False, col_len: int = 0
) -> int:
    """Compute required vertical router gap between two adjacent nodes in a column."""
    is_tall = (nd.height >= _LARGE_CELL_H or nxt.height >= _LARGE_CELL_H) and (
        not check_col_len or col_len <= _PAD_X_DEFAULT
    )
    if is_tall:
        base_gap = 10
    elif nd.node_type == "machine" or nxt.node_type == "machine":
        base_gap = _MACH_V_GAP
    else:
        base_gap = _PAD_X_DEFAULT
    return max(base_gap, nd.pad_n + nxt.pad_s)


def _col_req_h(col: list[PlacerNode]) -> int:
    """Compute required vertical span for a column of nodes."""
    if not col:
        return 0
    req = 0
    for idx in range(len(col) - 1):
        step = col[idx].height + _pair_vertical_gap(col[idx], col[idx + 1])
        req += step + (step % 2)
    last_h = col[-1].height
    return req + last_h + (last_h % 2)


def _expand_paired_power_columns(
    raw_cols: dict[int, list[PlacerNode]],
    boiler_col_ids: list[int],
    eng_col_ids: list[int],
    edges: list[PlacerEdge],
    usable_h: int,
) -> list[list[PlacerNode]]:
    """Expand paired boiler and steam engine columns into interleaved sub-columns."""
    boilers = sorted(
        [nd for cid in boiler_col_ids for nd in raw_cols[cid]],
        key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)),
    )
    all_engs = {nd.name: nd for cid in eng_col_ids for nd in raw_cols[cid]}
    engs_by_boiler: dict[str, list[PlacerNode]] = collections.defaultdict(list)
    used_e: set[str] = set()
    b_name_set = {b.name for b in boilers}
    for e in edges:
        if e.source in b_name_set and e.sink in all_engs and e.sink not in used_e:
            engs_by_boiler[e.source].append(all_engs[e.sink])
            used_e.add(e.sink)
    for b_nd in boilers:
        engs_by_boiler[b_nd.name].sort(key=lambda nd: _natural_key(nd.name))
    req_h = _col_req_h(boilers)
    num_b_subcols = max(1, math.ceil(float(req_h) / float(usable_h)))
    b_per_col = max(1, math.ceil(len(boilers) / float(num_b_subcols)))
    out_cols: list[list[PlacerNode]] = []
    for i in range(0, len(boilers), b_per_col):
        b_sub = boilers[i : i + b_per_col]
        out_cols.append(b_sub)
        max_fan = max((len(engs_by_boiler[b.name]) for b in b_sub), default=0)
        for f_idx in range(max_fan):
            e_sub = [
                engs_by_boiler[b.name][f_idx] for b in b_sub if len(engs_by_boiler[b.name]) > f_idx
            ]
            if e_sub:
                out_cols.append(e_sub)
    rem_e = [
        nd
        for nd in sorted(all_engs.values(), key=lambda x: _natural_key(x.name))
        if nd.name not in used_e
    ]
    if rem_e:
        out_cols.append(rem_e)
    return out_cols


def _split_col_into_subcols(
    col_nodes: list[PlacerNode],
    usable_h: int,
    fixed_core: bool,
) -> list[list[PlacerNode]]:
    """Sort col_nodes by continuous Y and split into height-bounded sub-columns."""
    col_nodes.sort(key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)))
    req_h = _col_req_h(col_nodes)
    num_subcols = max(1, math.ceil(float(req_h) / float(usable_h))) if fixed_core else 1
    if num_subcols == 1:
        return [col_nodes]
    sub_lists: list[list[PlacerNode]] = [[] for _ in range(num_subcols)]
    for idx, nd in enumerate(col_nodes):
        sub_lists[idx % num_subcols].append(nd)
    return [sl for sl in sub_lists if sl]


def _find_recipe_column_ids(
    raw_cols: dict[int, list[PlacerNode]],
    recipe_name: str,
    fixed_core: bool,
) -> list[int]:
    """Return column IDs where all nodes share recipe_name when fixed_core is True."""
    if not fixed_core:
        return []
    return [
        cid for cid, cn in raw_cols.items() if cn and all(nd.recipe == recipe_name for nd in cn)
    ]


def _expand_block_columns(
    raw_cols: dict[int, list[PlacerNode]],
    edges: list[PlacerEdge],
    usable_h: int,
    fixed_core: bool,
) -> tuple[list[list[PlacerNode]], bool]:
    """Expand raw columns into height-bounded sub-columns."""
    eng_col_ids = _find_recipe_column_ids(raw_cols, "steam-power", fixed_core)
    boiler_col_ids = _find_recipe_column_ids(raw_cols, "boiler-steam", fixed_core)
    paired_power_cols = bool(eng_col_ids and boiler_col_ids)
    power_expanded = (
        _expand_paired_power_columns(raw_cols, boiler_col_ids, eng_col_ids, edges, usable_h)
        if paired_power_cols
        else []
    )
    first_boiler_cid = boiler_col_ids[0] if boiler_col_ids else -1
    handled_col_ids = set(boiler_col_ids) | set(eng_col_ids) if paired_power_cols else set()
    expanded_cols: list[list[PlacerNode]] = []

    for col_id in sorted(raw_cols.keys()):
        if col_id == first_boiler_cid:
            expanded_cols.extend(power_expanded)
        elif col_id not in handled_col_ids:
            expanded_cols.extend(_split_col_into_subcols(raw_cols[col_id], usable_h, fixed_core))

    return expanded_cols, paired_power_cols


def _total_min_col_w(cols: list[list[PlacerNode]]) -> int:
    """Compute total minimum width of columns with router halos."""
    if not cols:
        return 0
    w_total = sum(max(nd.width for nd in col) for col in cols)
    g_total = 0
    for c_i in range(len(cols) - 1):
        max_e = max(nd.pad_e for nd in cols[c_i])
        max_w = max(nd.pad_w for nd in cols[c_i + 1])
        g_total += max(_MIN_COL_GAP, max(max_e, max_w) + 1)
    return w_total + g_total


def _total_hard_col_w(cols: list[list[PlacerNode]], paired_power_cols: bool) -> int:
    """Compute hard minimum width of columns."""
    if not cols:
        return 0
    gap_w = _PAD_X_DEFAULT if paired_power_cols else 2
    return sum(max(nd.width for nd in col) for col in cols) + gap_w * (len(cols) - 1)


def _can_merge_col_pair(
    col_a: list[PlacerNode],
    col_b: list[PlacerNode],
    allow_cross: bool,
    fold_trees: bool,
    need_hard: bool,
    non_circuit_adj: dict[str, set[str]],
) -> bool:
    """Check if two adjacent columns are eligible for horizontal folding."""
    if not allow_cross:
        names_b = {nd.name for nd in col_b}
        return not any(non_circuit_adj.get(nd.name, set()) & names_b for nd in col_a)
    if fold_trees and not need_hard:
        types_a = {nd.node_type for nd in col_a}
        types_b = {nd.node_type for nd in col_b}
        return (types_a <= {"split", "balancer"} and types_b <= {"split", "balancer"}) or (
            types_a == {"merge"} and types_b == {"merge"}
        )
    return True


def _find_best_col_merge_idx(
    expanded_cols: list[list[PlacerNode]],
    allow_cross: bool,
    fold_trees: bool,
    need_hard: bool,
    usable_h: int,
    non_circuit_adj: dict[str, set[str]],
) -> int:
    """Find best adjacent column pair index to fold horizontally."""
    best_idx = -1
    best_key: tuple[int, int] | None = None
    for idx in range(len(expanded_cols) - 1):
        col_a = expanded_cols[idx]
        col_b = expanded_cols[idx + 1]
        if not _can_merge_col_pair(
            col_a, col_b, allow_cross, fold_trees, need_hard, non_circuit_adj
        ):
            continue
        merged_cand = sorted(
            col_a + col_b,
            key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)),
        )
        comb_h = _col_req_h(merged_cand)
        if comb_h > usable_h:
            continue
        types_a = {nd.node_type for nd in col_a}
        types_b = {nd.node_type for nd in col_b}
        cand_key = (0 if types_a == types_b else 1, comb_h)
        if best_key is None or cand_key < best_key:
            best_key = cand_key
            best_idx = idx
    return best_idx


def _required_fold_width(
    expanded_cols: list[list[PlacerNode]],
    allow_cross: bool,
    fold_trees: bool,
    paired_power_cols: bool,
) -> int:
    """Compute required horizontal width of expanded_cols under current folding mode."""
    if not allow_cross or fold_trees:
        return _total_min_col_w(expanded_cols)
    return _total_hard_col_w(expanded_cols, paired_power_cols)


def _fold_columns_to_fit_width(
    expanded_cols: list[list[PlacerNode]],
    block_nodes: list[PlacerNode],
    edges: list[PlacerEdge],
    avail_w: int,
    usable_h: int,
    paired_power_cols: bool,
    fold_trees: bool,
) -> None:
    """Fold adjacent columns horizontally until total width fits available core width."""
    node_type_by_name = {nd.name: nd.node_type for nd in block_nodes}
    non_circuit_adj: dict[str, set[str]] = collections.defaultdict(set)
    for e in edges:
        if node_type_by_name.get(e.source) not in (
            "combinator",
            "display",
        ) and node_type_by_name.get(e.sink) not in ("combinator", "display"):
            non_circuit_adj[e.source].add(e.sink)
            non_circuit_adj[e.sink].add(e.source)

    for allow_cross in (False, True):
        while (
            len(expanded_cols) > 1
            and _required_fold_width(expanded_cols, allow_cross, fold_trees, paired_power_cols)
            > avail_w
        ):
            need_hard = _total_hard_col_w(expanded_cols, paired_power_cols) > avail_w
            best_idx = _find_best_col_merge_idx(
                expanded_cols,
                allow_cross,
                fold_trees,
                need_hard,
                usable_h,
                non_circuit_adj,
            )
            if best_idx < 0:
                break
            merged_col = sorted(
                expanded_cols[best_idx] + expanded_cols[best_idx + 1],
                key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)),
            )
            expanded_cols[best_idx : best_idx + 2] = [merged_col]


def _compute_band_column_gaps(
    cols: list[list[PlacerNode]],
    col_widths: list[int],
    avail_w: int,
    fixed_x: bool,
) -> list[int]:
    """Compute horizontal gaps between adjacent columns in a band."""
    num_cols = len(cols)
    min_gaps: list[int] = []
    ideal_gaps: list[int] = []
    for c in range(num_cols - 1):
        max_e = max(nd.pad_e for nd in cols[c])
        max_w = max(nd.pad_w for nd in cols[c + 1])
        has_boiler = any(nd.recipe == "boiler-steam" for nd in cols[c])
        next_has_boiler = any(nd.recipe == "boiler-steam" for nd in cols[c + 1])
        min_g = max(_MIN_COL_GAP, max(max_e, max_w) + 1)
        if has_boiler:
            min_g = max(min_g, _BOILER_MIN_GAP)
        elif next_has_boiler:
            min_g = max(min_g, _NEXT_BOILER_MIN_GAP)
        min_gaps.append(min_g)
        extra_ch = (
            0
            if fixed_x
            else min(
                _MAX_EXTRA_CH,
                max(0, max(len(cols[c]), len(cols[c + 1])) - _MAX_GRP_LEN) // _EXTRA_CH_DIV,
            )
        )
        ideal_gaps.append(max(min_g + 2, max_e + max_w + 2 + extra_ch))

    if not fixed_x:
        return list(ideal_gaps)

    sum_w = sum(col_widths)
    if sum_w + sum(ideal_gaps) <= avail_w:
        spare = avail_w - (sum_w + sum(ideal_gaps))
        bonus = min(_PAD_X_DEFAULT, spare // max(1, num_cols))
        return [g + bonus for g in ideal_gaps]
    if sum_w + sum(min_gaps) <= avail_w:
        spare = avail_w - (sum_w + sum(min_gaps))
        diff_total = max(1, sum(ideal_gaps[i] - min_gaps[i] for i in range(num_cols - 1)))
        return [
            min_gaps[i] + math.floor(spare * (ideal_gaps[i] - min_gaps[i]) / diff_total)
            for i in range(num_cols - 1)
        ]
    spare = max(0, avail_w - sum_w)
    n_gaps = max(1, num_cols - 1)
    base_g = max(2, spare // n_gaps)
    rem_g = max(0, spare - base_g * n_gaps)
    return [base_g + (1 if i < rem_g else 0) for i in range(max(0, num_cols - 1))]


def _initial_legalize_col_y(
    col: list[PlacerNode],
    c_idx: int,
    y_lo: int,
    y_hi: int,
    band_h: int,
    snap_even_base: int | None,
) -> None:
    """Assign initial legalized Y coordinates to a single column."""
    col.sort(key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)))
    stagger = (c_idx % _STAGGER_MOD) * 2 if band_h >= _MIN_STAGGER_BAND_H else 0
    c_y_lo = min(y_hi - sum(nd.height for nd in col), y_lo + stagger)
    heights = [nd.height for nd in col]
    gaps = [
        max(
            (
                10
                if (col[i].height >= _LARGE_CELL_H or col[i + 1].height >= _LARGE_CELL_H)
                and len(col) <= _PAD_X_DEFAULT
                else (
                    _MACH_V_GAP
                    if col[i].node_type == "machine" or col[i + 1].node_type == "machine"
                    else _PAD_X_DEFAULT
                )
            ),
            col[i].pad_n + col[i + 1].pad_s,
        )
        for i in range(len(col) - 1)
    ]
    m = len(col)
    span_h = sum(heights) + sum(gaps)
    if m == 1:
        ideals = [float(np.clip(col[0].cont_y, c_y_lo, y_hi - col[0].height))]
    else:
        step = max(
            float(span_h) / float(m),
            min(_MAX_COL_IDEAL_STEP, float(band_h - _PAD_X_DEFAULT) / float(m)),
        )
        center_y = float(np.mean([nd.cont_y for nd in col]))
        start_y = max(
            float(c_y_lo),
            min(
                float(y_hi) - step * (m - 1) - heights[-1],
                center_y - _HALF * step * (m - 1),
            ),
        )
        ideals = [start_y + i * step for i in range(m)]

    y_vals = _solve_1d_pav(
        ideals,
        heights,
        gaps,
        y_lo=c_y_lo,
        y_hi=y_hi,
        snap_even_base=snap_even_base,
    )
    for nd, yv in zip(col, y_vals, strict=False):
        nd.y = yv


def _apply_refined_col_y(
    cols: list[list[PlacerNode]],
    c_idx: int,
    ideals: list[float],
    y_lo: int,
    y_hi: int,
    band_h: int,
    snap_even_base: int | None,
) -> None:
    """Sort a column by ideal Y targets and apply PAV legalization."""
    col = cols[c_idx]
    paired = sorted(
        zip(ideals, col, strict=False),
        key=lambda item: (round(item[0], 2), _natural_key(item[1].name)),
    )
    cols[c_idx] = [item[1] for item in paired]
    col = cols[c_idx]
    sorted_ideals = [item[0] for item in paired]
    heights = [nd.height for nd in col]
    gaps = [
        max(
            _MACH_V_GAP
            if col[i].node_type == "machine" or col[i + 1].node_type == "machine"
            else _PAD_X_DEFAULT,
            col[i].pad_n + col[i + 1].pad_s,
        )
        for i in range(len(col) - 1)
    ]
    stagger = (c_idx % _STAGGER_MOD) * 2 if band_h >= _MIN_STAGGER_BAND_H else 0
    c_y_lo = min(y_hi - sum(heights), y_lo + stagger)
    y_vals = _solve_1d_pav(
        sorted_ideals,
        heights,
        gaps,
        y_lo=c_y_lo,
        y_hi=y_hi,
        snap_even_base=snap_even_base,
    )
    for nd, yv in zip(col, y_vals, strict=False):
        nd.y = yv


def _compute_forward_sweep_ideal_y(
    nd: PlacerNode,
    node_map: dict[str, PlacerNode],
    drivers_of: dict[str, list[tuple[str, float, float]]],
    sinks_of: dict[str, list[tuple[str, float, float]]],
) -> float:
    """Compute ideal Y coordinate for a node during forward sweep alignment."""
    if (
        nd.node_type not in ("merge", "pump", "combinator", "display")
        and nd.recipe != "steam-power"
    ):
        return float(nd.y)
    drv_ys = [
        node_map[d_name].y + src_dy - s_dy
        for d_name, src_dy, s_dy in drivers_of.get(nd.name, [])
        if d_name in node_map
    ]
    snk_ys = [
        node_map[s_name].y + s_dy - src_dy
        for s_name, src_dy, s_dy in sinks_of.get(nd.name, [])
        if s_name in node_map
    ]
    if drv_ys and snk_ys:
        return _FWD_DRV_Y_WT * float(np.mean(drv_ys)) + _FWD_SINK_Y_WT * float(np.mean(snk_ys))
    if drv_ys:
        return float(np.mean(drv_ys))
    if snk_ys:
        return float(np.mean(snk_ys))
    return float(nd.y)


def _compute_backward_sweep_ideal_y(
    nd: PlacerNode,
    node_map: dict[str, PlacerNode],
    sinks_of: dict[str, list[tuple[str, float, float]]],
) -> float:
    """Compute ideal Y coordinate for a splitter or balancer during backward sweep."""
    if nd.node_type not in ("split", "balancer") or not sinks_of.get(nd.name):
        return float(nd.y)
    target_ys = [
        node_map[s_name].y + s_dy - src_dy
        for s_name, src_dy, s_dy in sinks_of[nd.name]
        if s_name in node_map
    ]
    return float(np.mean(target_ys)) if target_ys else float(nd.y)


def _refine_band_y_alignment(
    cols: list[list[PlacerNode]],
    node_map: dict[str, PlacerNode],
    sinks_of: dict[str, list[tuple[str, float, float]]],
    drivers_of: dict[str, list[tuple[str, float, float]]],
    y_lo: int,
    y_hi: int,
    band_h: int,
    snap_even_base: int | None,
) -> None:
    """Iteratively align splitters to sinks and mergers/engines to drivers."""
    num_cols = len(cols)
    for _pass in range(_LEGALIZE_PASSES):
        for c in range(num_cols - 1, -1, -1):
            col = cols[c]
            if any(nd.node_type in ("split", "balancer") for nd in col):
                ideals = [_compute_backward_sweep_ideal_y(nd, node_map, sinks_of) for nd in col]
                _apply_refined_col_y(cols, c, ideals, y_lo, y_hi, band_h, snap_even_base)

        for c in range(num_cols):
            col = cols[c]
            if any(
                nd.node_type in ("merge", "pump", "combinator", "display")
                or nd.recipe == "steam-power"
                for nd in col
            ):
                ideals = [
                    _compute_forward_sweep_ideal_y(nd, node_map, drivers_of, sinks_of) for nd in col
                ]
                _apply_refined_col_y(cols, c, ideals, y_lo, y_hi, band_h, snap_even_base)


def _select_sparse_bay_slots(mat_count: int) -> tuple[int, ...]:
    """Select horizontal slot offsets for sparse matrix bays."""
    if mat_count <= _MERGE_ORDER:
        return _BAY_SLOTS_3
    if mat_count == _BAY_SLOTS_4_LEN:
        return _BAY_SLOTS_4
    return _BAY_SLOTS_6


def _place_block_matrix_nodes(
    mat_nodes: list[PlacerNode],
    bx: int,
    by: int,
    chassis_pitch: int,
) -> None:
    """Place routing matrix nodes into the dedicated bay of a chassis block."""
    if not mat_nodes:
        return
    mat_nodes.sort(key=lambda nd: (nd.flow_depth, _natural_key(nd.name)))
    if len(mat_nodes) <= _BAY_CAP:
        bay_slots = _select_sparse_bay_slots(len(mat_nodes))
        for m_idx, nd in enumerate(mat_nodes):
            cur_mx = bx * chassis_pitch + bay_slots[m_idx % len(bay_slots)]
            bay_y = by * chassis_pitch + (_PIN_Y_MARGIN if (m_idx % 2 == 0) else _BAY_ODD_Y)
            nd.x = cur_mx
            nd.y = bay_y
            nd.cont_x = float(cur_mx)
            nd.cont_y = float(bay_y)
        return
    for m_idx, nd in enumerate(mat_nodes):
        row_grp = m_idx // _BAY_DENSE_COLS
        slot_idx = m_idx % _BAY_DENSE_COLS
        cur_mx = bx * chassis_pitch + _BAY_SLOTS_7[slot_idx] + (row_grp % 2) * _BAY_ROW_X_STEP
        bay_y = (
            by * chassis_pitch
            + _BAY_BASE_Y
            + row_grp * _BAY_ROW_Y_STEP
            + (slot_idx % 2) * _BAY_ROW_Y_MOD
        )
        nd.x = cur_mx
        nd.y = bay_y
        nd.cont_x = float(cur_mx)
        nd.cont_y = float(bay_y)


def _push_pins_north_from_idx(
    idx: int,
    y_vals: list[int],
    gaps: list[int],
    n_pins: int,
) -> None:
    """Push subsequent pin Y coordinates North to maintain minimum gap spacing."""
    for j in range(idx + 1, n_pins):
        req = y_vals[j - 1] + gaps[j - 1] + 1
        if y_vals[j] < req:
            y_vals[j] = req


def _push_pins_south_from_idx(
    idx: int,
    y_vals: list[int],
    gaps: list[int],
) -> None:
    """Push preceding pin Y coordinates South to maintain minimum gap spacing."""
    for j in range(idx - 1, -1, -1):
        req = y_vals[j + 1] - gaps[j] - 1
        if y_vals[j] > req:
            y_vals[j] = req


def _nudge_pins_from_chassis_boundaries(
    items: list[tuple[float, float, float, str, str]],
    y_vals: list[int],
    gaps: list[int],
    grid_h: int,
    chassis_pitch: int,
) -> None:
    """Nudge boundary pins away from horizontal chassis avenue centerlines."""
    n_pins = len(items)
    for idx in range(n_pins):
        yv = y_vals[idx]
        k = round(float(yv) / float(chassis_pitch))
        if 1 <= k < grid_h and abs(yv - k * chassis_pitch) <= _PIN_BOUNDARY_TOL:
            if items[idx][1] >= float(k * chassis_pitch):
                y_vals[idx] = k * chassis_pitch + _PIN_Y_MARGIN
                _push_pins_north_from_idx(idx, y_vals, gaps, n_pins)
            else:
                y_vals[idx] = k * chassis_pitch - _PIN_Y_MARGIN
                _push_pins_south_from_idx(idx, y_vals, gaps)


def _west_core_pin_cand_range(
    by_s: int,
    yv: int,
    grid_h: int,
    chassis_pitch: int,
) -> range:
    """Return candidate Y coordinate range in an adjacent block for a West-column input pin."""
    prefer_south = by_s + 1 < grid_h and (
        by_s == 0 or yv >= by_s * chassis_pitch + chassis_pitch // 2
    )
    if prefer_south:
        seg_lo = (by_s + 1) * chassis_pitch + _PIN_Y_MARGIN
        seg_hi = (by_s + 2) * chassis_pitch - _PIN_Y_MARGIN
        return range(seg_lo, seg_hi + 1)
    if by_s > 0:
        seg_lo = (by_s - 1) * chassis_pitch + _PIN_Y_MARGIN
        seg_hi = by_s * chassis_pitch - _PIN_Y_MARGIN
        return range(seg_hi, seg_lo - 1, -1)
    return range(0)


def _relocate_west_core_input_pins(
    items: list[tuple[float, float, float, str, str]],
    y_vals: list[int],
    in_sink_nodes: dict[str, list[PlacerNode]],
    grid_h: int,
    chassis_pitch: int,
) -> None:
    """Relocate input pins that feed West-column core cells to adjacent block segments."""
    n_pins = len(items)
    for idx, item in enumerate(items):
        pname = item[-1]
        phys_sinks = [
            v for v in in_sink_nodes.get(pname, []) if v.node_type not in ("combinator", "display")
        ]
        if not phys_sinks or not all(
            not v.is_matrix_node and v.assigned_block[0] == 0 for v in phys_sinks
        ):
            continue
        by_s = min(v.assigned_block[1] for v in phys_sinks)
        if not (by_s * chassis_pitch <= y_vals[idx] < (by_s + 1) * chassis_pitch):
            continue
        cand_range = _west_core_pin_cand_range(by_s, y_vals[idx], grid_h, chassis_pitch)
        for cand_y in cand_range:
            if all(abs(cand_y - y_vals[j]) >= _MIN_PIN_GAP for j in range(n_pins) if j != idx):
                y_vals[idx] = cand_y
                break


def _append_chassis_tcl_lines(
    lines: list[str],
    gw: int,
    gh: int,
    ch_res: AnalyticalPlacementResult,
) -> None:
    """Append TCL dictionary definitions for one chassis grid result."""
    dict_name = f"analytical_chassis_{gw}x{gh}"
    lines.append(f"set {dict_name} [dict create]")
    for name in sorted(ch_res.coords.keys(), key=_natural_key):
        ix, iy = ch_res.coords[name]
        lines.append(f"dict set {dict_name} {name} [list {ix} {iy}]")
    if ch_res.rewired_ports:
        rw_name = f"analytical_rewire_{gw}x{gh}"
        lines.append(f"set {rw_name} [dict create]")
        for iname in sorted(ch_res.rewired_ports.keys(), key=_natural_key):
            pmap = ch_res.rewired_ports[iname]
            flat_kv = " ".join(f"{k} {v}" for k, v in pmap.items())
            lines.append(f"dict set {rw_name} {iname} [list {flat_kv}]")
    if ch_res.pin_coords:
        pin_dict_name = f"analytical_pins_{gw}x{gh}"
        lines.append(f"set {pin_dict_name} [dict create]")
        for pname in sorted(ch_res.pin_coords.keys(), key=_natural_key):
            px, py = ch_res.pin_coords[pname]
            lines.append(f"dict set {pin_dict_name} {pname} [list {px} {py}]")


def _def_inst_dims(
    iname: str,
    comp_map: dict[str, MutableJsonMap],
    inst_map: dict[str, JsonMap],
    cells_map: JsonMap,
) -> tuple[float, float]:
    """Return (width, height) in tiles for a DEF component instance."""
    comp = comp_map.get(iname, {})
    macro = inst_map.get(iname, {}).get("cell", comp.get("macro", ""))
    if macro in cells_map:
        cmeta = cells_map[macro]
        return float(cmeta.get("width", 1)), float(cmeta.get("height", 2))
    if any(k in macro for k in ("split", "merge", "balancer")):
        return 1.0, 2.0
    return _MERGE2_FLOAT_DIM, _MERGE2_FLOAT_DIM


def _is_def_output_port(
    iname: str,
    port: str,
    comp_map: dict[str, MutableJsonMap],
    inst_map: dict[str, JsonMap],
    cells_map: JsonMap,
    pin_map: dict[str, JsonMap],
) -> bool:
    """Return True if (iname, port) acts as a signal driver on a DEF net."""
    if iname == "PIN":
        p = pin_map.get(port, {})
        return p.get("direction") == "INPUT"
    comp = comp_map.get(iname, {})
    macro = inst_map.get(iname, {}).get("cell", comp.get("macro", ""))
    if any(k in macro for k in ("split", "merge", "balancer")):
        return port in ("y", "y1", "y2")
    outs = cells_map.get(macro, {}).get("outputs", [])
    clean_outs = {_sanitize(o) for o in outs} | set(outs)
    return port in clean_outs or _sanitize(port) in clean_outs or port in ("y", "out")


def _append_def_peer_point(
    iname: str,
    port: str,
    nname: str,
    want_driver: bool,
    expected_pin_dir: str,
    scale: float,
    comp_map: dict[str, MutableJsonMap],
    inst_map: dict[str, JsonMap],
    cells_map: JsonMap,
    pin_map: dict[str, JsonMap],
    xs: list[float],
    ys: list[float],
) -> None:
    """Append one matching driver or sink peer coordinate to xs and ys."""
    if iname == "PIN":
        p = pin_map.get(nname) or pin_map.get(port)
        if p and p.get("direction") == expected_pin_dir:
            xs.append(float(p["x"]) * scale)
            ys.append(float(p["y"]) * scale)
        return
    if iname not in comp_map:
        return
    is_drv = _is_def_output_port(iname, port, comp_map, inst_map, cells_map, pin_map)
    if is_drv == want_driver:
        c = comp_map[iname]
        w, h = _def_inst_dims(iname, comp_map, inst_map, cells_map)
        xs.append(float(c["x"]) * scale + w / 2.0)
        ys.append(float(c["y"]) * scale + h / 2.0)


def _def_net_peer_center(
    net_obj: JsonMap,
    exclude_inst: str,
    want_driver: bool,
    scale: float,
    comp_map: dict[str, MutableJsonMap],
    inst_map: dict[str, JsonMap],
    cells_map: JsonMap,
    pin_map: dict[str, JsonMap],
) -> tuple[float, float]:
    """Compute mean (x, y) center of driver or sink peers on a DEF net."""
    xs: list[float] = []
    ys: list[float] = []
    nname = net_obj.get("name", "")
    expected_pin_dir = "INPUT" if want_driver else "OUTPUT"
    for iname, port in net_obj.get("connections", []):
        if iname != exclude_inst:
            _append_def_peer_point(
                iname,
                port,
                nname,
                want_driver,
                expected_pin_dir,
                scale,
                comp_map,
                inst_map,
                cells_map,
                pin_map,
                xs,
                ys,
            )
    return (
        float(np.mean(xs)) if xs else 0.0,
        float(np.mean(ys)) if ys else 0.0,
    )


def _try_swap_port_pair(
    cname: str,
    port_1: str,
    port_2: str,
    want_driver: bool,
    inst_port_ref: dict[tuple[str, str], tuple[MutableJsonMap, int]],
    scale: float,
    comp_map: dict[str, MutableJsonMap],
    inst_map: dict[str, JsonMap],
    cells_map: JsonMap,
    pin_map: dict[str, JsonMap],
) -> None:
    """Swap two symmetric ports on an instance if their connected peer centroids cross."""
    ref1 = inst_port_ref.get((cname, port_1))
    ref2 = inst_port_ref.get((cname, port_2))
    if not (ref1 and ref2 and ref1[0] is not ref2[0]):
        return
    net1, idx1 = ref1
    net2, idx2 = ref2
    x1, y1 = _def_net_peer_center(
        net1, cname, want_driver, scale, comp_map, inst_map, cells_map, pin_map
    )
    x2, y2 = _def_net_peer_center(
        net2, cname, want_driver, scale, comp_map, inst_map, cells_map, pin_map
    )
    should_swap = (y1 > y2) if abs(y1 - y2) >= _HALF else (x1 > x2)
    if should_swap:
        net1["connections"][idx1] = (cname, port_2)
        net2["connections"][idx2] = (cname, port_1)
        inst_port_ref[(cname, port_2)] = (net1, idx1)
        inst_port_ref[(cname, port_1)] = (net2, idx2)


def _resolve_macro_footprint(macro: str, cell_meta: JsonMap | None) -> tuple[int, int]:
    """Resolve integer (width, height) footprint for macro from cell_meta or entity_size."""
    w = int(cell_meta.get("width", 0)) if cell_meta else 0
    h = int(cell_meta.get("height", 0)) if cell_meta else 0
    if w > 0 and h > 0:
        return w, h
    if macro == "belt_merge2":
        ew, eh = _MERGE2_FLOAT_DIM, _MERGE2_FLOAT_DIM
    elif macro in ("belt_split2", "belt_balancer2"):
        ew, eh = 1.0, 2.0
    else:
        ew, eh = entity_size(macro)
    return (int(ew) if w <= 0 else w), (int(eh) if h <= 0 else h)


def _select_best_merger_block(
    m_name: str,
    nodes: dict[str, PlacerNode],
    active_blocks: list[tuple[int, int]],
    in_drivers: dict[str, list[str]],
    out_sinks: dict[str, list[str]],
    pin_out_nodes: set[str],
    assigned: set[str],
    block_area: dict[tuple[int, int], float],
    bay_counts: dict[tuple[int, int], int],
    blk_to_seq_idx: dict[tuple[int, int], int],
    grid_w: int,
    grid_h: int,
) -> tuple[int, int]:
    """Select the best chassis block for a ready merger node."""
    is_mat = nodes[m_name].is_matrix_node
    drv_blks = [
        nodes[d].assigned_block
        for d in in_drivers.get(m_name, [])
        if d in assigned and nodes[d].assigned_block in block_area
    ]
    drv_pts = [
        _node_blk_pt(nodes[d])
        for d in in_drivers.get(m_name, [])
        if d in assigned and nodes[d].assigned_block in block_area
    ]
    ref_by_m = (
        sum(pt[1] for pt in drv_pts) / len(drv_pts) if drv_pts else float(max(1, grid_h // 2))
    )
    down_pts = _find_boundary_or_assigned_pts(
        m_name,
        out_sinks,
        pin_out_nodes,
        max(0, grid_w - 1),
        ref_by_m,
        nodes,
        assigned,
        block_area,
    )
    down_blks = [(pt[0], min(grid_h - 1, max(0, round(pt[1])))) for pt in down_pts]
    if not drv_blks and not is_mat:
        return down_blks[0] if down_blks else active_blocks[0]
    counts = collections.Counter(drv_blks)
    cand_blks = (
        list(active_blocks)
        if is_mat
        else list(dict.fromkeys(drv_blks + down_blks)) or list(active_blocks)
    )
    ref_pts = drv_pts or down_pts or [(active_blocks[0][0], float(active_blocks[0][1]))]
    down_wt = (_MAT_PORT_WT if is_mat else 1.0) / max(1, len(down_pts))
    return min(
        cand_blks,
        key=lambda b: _merger_block_sort_key(
            b,
            is_mat,
            grid_w,
            grid_h,
            len(drv_blks),
            counts,
            ref_pts,
            down_pts,
            down_wt,
            bay_counts,
            block_area,
            blk_to_seq_idx,
        ),
    )


def _select_best_splitter_block(
    s_name: str,
    nodes: dict[str, PlacerNode],
    active_blocks: list[tuple[int, int]],
    in_drivers: dict[str, list[str]],
    out_sinks: dict[str, list[str]],
    pin_in_nodes: set[str],
    assigned: set[str],
    block_bins: dict[tuple[int, int], list[str]],
    block_area: dict[tuple[int, int], float],
    bay_counts: dict[tuple[int, int], int],
    blk_to_seq_idx: dict[tuple[int, int], int],
    grid_w: int,
    grid_h: int,
    total_bals: int,
) -> tuple[int, int]:
    """Select the best chassis block for a ready splitter or balancer node."""
    is_mat = nodes[s_name].is_matrix_node
    is_bal = nodes[s_name].node_type == "balancer"
    valid_snks = [
        nodes[v]
        for v in out_sinks.get(s_name, [])
        if v in assigned and nodes[v].assigned_block in block_area
    ]
    snk_blks = [nd.assigned_block for nd in valid_snks]
    snk_pts = [_node_blk_pt(nd) for nd in valid_snks]
    ref_by_s = (
        sum(pt[1] for pt in snk_pts) / len(snk_pts) if snk_pts else float(max(1, grid_h // 2))
    )
    up_pts = _find_boundary_or_assigned_pts(
        s_name, in_drivers, pin_in_nodes, 0, ref_by_s, nodes, assigned, block_area
    )
    up_blks = [(pt[0], min(grid_h - 1, max(0, round(pt[1])))) for pt in up_pts]
    if not snk_blks and not is_mat:
        return up_blks[0] if up_blks else active_blocks[0]
    counts = collections.Counter(snk_blks)
    cand_blks = (
        list(active_blocks)
        if is_mat
        else list(dict.fromkeys(snk_blks + up_blks)) or list(active_blocks)
    )
    ref_pts = snk_pts or up_pts or [(active_blocks[0][0], float(active_blocks[0][1]))]
    in_port_cnt = 2.0 if is_bal else (_MAT_PORT_WT if is_mat else 1.0)
    up_wt = in_port_cnt / max(1, len(up_pts))
    in_pin = s_name in pin_in_nodes
    return min(
        cand_blks,
        key=lambda b: _splitter_block_sort_key(
            b,
            is_mat,
            is_bal,
            in_pin,
            grid_w,
            grid_h,
            total_bals,
            len(snk_blks),
            counts,
            ref_pts,
            up_pts,
            up_wt,
            bay_counts,
            block_bins,
            nodes,
            block_area,
            blk_to_seq_idx,
        ),
    )


def _find_telemetry_neighbor_blocks(
    u_name: str,
    nodes: dict[str, PlacerNode],
    in_drivers: dict[str, list[str]],
    out_sinks: dict[str, list[str]],
    assigned: set[str],
    block_area: dict[tuple[int, int], float],
) -> list[tuple[int, int]]:
    """Find assigned block coordinates of direct or two-hop neighbors for telemetry node u_name."""
    nb_blks = [
        nodes[nb].assigned_block
        for nb in in_drivers.get(u_name, []) + out_sinks.get(u_name, [])
        if nb in assigned and nodes[nb].assigned_block in block_area
    ]
    if nb_blks:
        return nb_blks
    return [
        nodes[co].assigned_block
        for drv in in_drivers.get(u_name, [])
        for co in out_sinks.get(drv, [])
        if co in assigned and nodes[co].assigned_block in block_area
    ]


def _compute_dp_bin_counts(
    groups: list[list[str]],
    lab_groups: list[list[str]],
    power_groups: list[list[str]],
    nodes: dict[str, PlacerNode],
    atomic_first: set[str],
    single_core_capacity: float,
    num_blocks: int,
    grid_w: int,
    distribute_across_blocks: bool,
) -> tuple[list[float], int, int, int]:
    """Compute prefix cluster weights and bin counts (n_lab_bins, n_power_bins, k_bins)."""
    grp_weights = [
        min(
            single_core_capacity * _ATOMIC_CAP_FRAC,
            sum(_node_cluster_weight(nodes[u]) for u in grp),
        )
        if (grp and grp[0] in atomic_first and grid_w < _LARGE_GRID_W)
        else sum(_node_cluster_weight(nodes[u]) for u in grp)
        for grp in groups
    ]
    pref = [0.0]
    for gw in grp_weights:
        pref.append(pref[-1] + gw)

    total_cluster_w = pref[-1]
    total_lab_w = sum(_node_cluster_weight(nodes[u]) for lg in lab_groups for u in lg)
    total_power_w = sum(_node_cluster_weight(nodes[u]) for pg in power_groups for u in pg)
    num_groups = len(groups)
    n_lab_bins = min(len(lab_groups), max(0, num_blocks - 1)) if lab_groups else 0
    n_power_bins = (
        min(len(power_groups), max(0, num_blocks - 1 - n_lab_bins)) if power_groups else 0
    )
    avail_prod_blocks = max(1, num_blocks - n_lab_bins - n_power_bins)
    if distribute_across_blocks:
        k_bins = min(num_groups, avail_prod_blocks)
    else:
        est_bins = max(
            1,
            math.ceil((total_cluster_w + total_lab_w + total_power_w) / _CLUSTER_BIN_DIVISOR),
        )
        k_bins = min(num_groups, avail_prod_blocks, est_bins)
    return pref, n_lab_bins, n_power_bins, k_bins


def _collect_valid_tree_components(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
    in_edges: dict[str, list[PlacerEdge]],
    out_edges: dict[str, list[PlacerEdge]],
    assigned: set[str],
) -> tuple[
    list[tuple[str, str, tuple[str, str], list[tuple[str, str]], list[PlacerEdge]]],
    dict[str, list[tuple[str, tuple[str, str], list[tuple[str, str]], list[PlacerEdge]]]],
    dict[str, list[tuple[str, list[str], set[str], list[PlacerEdge]]]],
]:
    """Find and validate all binary merger and splitter tree components."""
    merge_names = [n for n, nd in nodes.items() if nd.node_type == "merge"]
    m_comps = _find_same_recipe_components(merge_names, nodes, edges)
    valid_m_list: list[
        tuple[str, str, tuple[str, str], list[tuple[str, str]], list[PlacerEdge]]
    ] = []
    valid_m_by_recipe: dict[
        str, list[tuple[str, tuple[str, str], list[tuple[str, str]], list[PlacerEdge]]]
    ] = collections.defaultdict(list)
    for comp in m_comps:
        m_info = _validate_merge_component(comp, nodes, in_edges, out_edges, assigned)
        if m_info is not None:
            r_key, r_wire, root_pair, avail_pairs, leaf_edges = m_info
            valid_m_list.append(m_info)
            valid_m_by_recipe[r_key].append((r_wire, root_pair, avail_pairs, leaf_edges))

    split_names = [n for n, nd in nodes.items() if nd.node_type == "split"]
    s_comps = _find_same_recipe_components(split_names, nodes, edges)
    valid_s_by_recipe: dict[str, list[tuple[str, list[str], set[str], list[PlacerEdge]]]] = (
        collections.defaultdict(list)
    )
    for comp in s_comps:
        s_info = _validate_split_component(comp, nodes, in_edges, out_edges, assigned)
        if s_info is not None:
            r_key, r_wire, s_comp, int_wires, leaf_edges = s_info
            valid_s_by_recipe[r_key].append((r_wire, s_comp, int_wires, leaf_edges))
    return valid_m_list, valid_m_by_recipe, valid_s_by_recipe


def _least_loaded_block(
    active_blocks: list[tuple[int, int]],
    block_area: dict[tuple[int, int], float],
) -> tuple[int, int]:
    """Return the active block with minimum accumulated padded area."""
    if not active_blocks:
        return (0, 0)
    return min(active_blocks, key=lambda b: block_area[b])


def _relocate_matrix_splitters_for_large_grid(
    nodes: dict[str, PlacerNode],
    active_blocks: list[tuple[int, int]],
    in_drivers: dict[str, list[str]],
    out_sinks: dict[str, list[str]],
    assigned: set[str],
    block_bins: dict[tuple[int, int], list[str]],
    block_area: dict[tuple[int, int], float],
    bay_counts: dict[tuple[int, int], int],
    blk_to_seq_idx: dict[tuple[int, int], int],
) -> None:
    """Relocate matrix splitters into intermediate bays on wide chassis grids."""
    mat_splits = sorted(
        (
            name
            for name, nd in nodes.items()
            if nd.is_matrix_node and nd.node_type == "split" and name in assigned
        ),
        key=lambda name: (nodes[name].flow_depth, _natural_key(name)),
    )
    for s_name in mat_splits:
        cand_blocks = list(active_blocks)
        _try_relocate_matrix_splitter(
            s_name,
            nodes,
            cand_blocks,
            in_drivers,
            out_sinks,
            assigned,
            block_bins,
            block_area,
            bay_counts,
            blk_to_seq_idx,
        )


def _assign_band_column_x_coords(
    cols: list[list[PlacerNode]],
    col_widths: list[int],
    actual_gaps: list[int],
    x_lo: int,
    x_hi: int,
    fixed_x: bool,
) -> int:
    """Assign integer X coordinates to nodes in each column and return used band width."""
    num_cols = len(cols)
    curr_x = x_lo
    for c, col in enumerate(cols):
        if fixed_x and curr_x + col_widths[c] > x_hi:
            curr_x = max(x_lo, x_hi - col_widths[c])
        for nd in col:
            nd.x = curr_x + (col_widths[c] - nd.width) // 2
            nd.col_index = c
        curr_x += col_widths[c] + (actual_gaps[c] if c < num_cols - 1 else 0)
    return max(1, curr_x - x_lo)


def _collect_boundary_pin_targets(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
) -> tuple[
    dict[str, list[tuple[float, float, float, str]]],
    dict[str, list[tuple[float, float, float, str]]],
    dict[str, list[PlacerNode]],
]:
    """Collect West input and East output boundary pin target lists from graph edges."""
    in_targets: dict[str, list[tuple[float, float, float, str]]] = collections.defaultdict(list)
    out_targets: dict[str, list[tuple[float, float, float, str]]] = collections.defaultdict(list)
    in_sink_nodes: dict[str, list[PlacerNode]] = collections.defaultdict(list)
    for e in edges:
        if e.source.startswith("PIN_IN:") and e.sink in nodes:
            pname = e.source[len("PIN_IN:") :]
            v = nodes[e.sink]
            in_sink_nodes[pname].append(v)
            in_targets[pname].append(
                (float(v.y) + float(e.sink_dy), float(v.y), float(v.x), v.name)
            )
        elif e.sink.startswith("PIN_OUT:") and e.source in nodes:
            pname = e.sink[len("PIN_OUT:") :]
            u = nodes[e.source]
            out_targets[pname].append(
                (float(u.y) + float(e.source_dy), float(u.y), float(u.x), u.name)
            )
    return in_targets, out_targets, in_sink_nodes


def _append_edge_wire_points(
    e: PlacerEdge,
    nodes: dict[str, PlacerNode],
    die_w: int,
    pts: list[tuple[float, float]],
) -> None:
    """Append driver and sink pin endpoints for one edge to pts."""
    if e.source in nodes:
        u = nodes[e.source]
        pts.append((float(u.x + u.width), float(u.y) + e.source_dy))
    elif e.source.startswith("PIN_IN:") and e.sink in nodes:
        v = nodes[e.sink]
        pts.append((0.0, float(v.y) + e.sink_dy))
    if e.sink in nodes:
        v = nodes[e.sink]
        pts.append((float(v.x - 1), float(v.y) + e.sink_dy))
    elif e.sink.startswith("PIN_OUT:") and e.source in nodes:
        u = nodes[e.source]
        pts.append((float(die_w), float(u.y) + e.source_dy))


def _collect_wire_endpoints(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
    die_w: int,
) -> dict[str, list[tuple[float, float]]]:
    """Collect driver and sink pin coordinates grouped by wire name."""
    by_wire: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for e in edges:
        _append_edge_wire_points(e, nodes, die_w, by_wire[e.wire])
    return by_wire


def _record_inst_data_wire_rates(
    inst: MutableJsonMap,
    cells_map: JsonMap,
    sink_rates: dict[str, float],
    driver_rates: dict[str, float],
) -> None:
    """Record sink and driver wire rates for one instance dictionary."""
    cname = inst["name"]
    cmeta = cells_map.get(inst.get("cell", ""), {})
    p_rate = float(inst.get("plan_rate_per_min", 0.0))
    r_rate = float(inst.get("rated_per_min", p_rate))
    eff_r = r_rate if r_rate > 0.0 else p_rate
    if p_rate > 0.0:
        for inp, amt in (cmeta.get("in_amounts") or {}).items():
            sink_rates[f"{_sanitize(inp)}__{cname}"] = p_rate * float(amt)
    if eff_r > 0.0:
        for outp, amt in (cmeta.get("out_amounts") or {}).items():
            driver_rates[f"{_sanitize(outp)}__{cname}"] = eff_r * float(amt)


def _build_internal_driver_sink_maps(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build initial in_drivers and out_sinks adjacency maps for internal graph edges."""
    in_drivers: dict[str, list[str]] = collections.defaultdict(list)
    out_sinks: dict[str, list[str]] = collections.defaultdict(list)
    for e in edges:
        if e.source in nodes and e.sink in nodes and e.source != e.sink:
            in_drivers[e.sink].append(e.source)
            out_sinks[e.source].append(e.sink)
    return in_drivers, out_sinks


def _extract_boundary_pin_connected_sets(
    nodes: dict[str, PlacerNode],
    edges: list[PlacerEdge],
) -> tuple[set[str], set[str]]:
    """Return (pin_in_nodes, pin_out_nodes) connected to West PIN_IN and East PIN_OUT."""
    pin_in_nodes = {e.sink for e in edges if e.source.startswith("PIN_IN:") and e.sink in nodes}
    pin_out_nodes = {e.source for e in edges if e.sink.startswith("PIN_OUT:") and e.source in nodes}
    return pin_in_nodes, pin_out_nodes


def _split_and_remap_block_nodes(
    nodes: dict[str, PlacerNode],
    inst_names: list[str],
) -> tuple[list[PlacerNode], list[PlacerNode]]:
    """Split block instances into (mat_nodes, core_nodes) and remap core col_index."""
    blk_nodes = [nodes[name] for name in inst_names]
    mat_nodes = [nd for nd in blk_nodes if nd.is_matrix_node]
    core_nodes = [nd for nd in blk_nodes if not nd.is_matrix_node]
    local_cols = sorted({nd.flow_depth for nd in core_nodes})
    col_remap = {old_c: new_c for new_c, old_c in enumerate(local_cols)}
    for nd in core_nodes:
        nd.col_index = col_remap[nd.flow_depth]
    return mat_nodes, core_nodes


def _build_sorted_boundary_pin_items(
    target_map: dict[str, list[tuple[float, float, float, str]]],
    die_h: int,
) -> tuple[list[tuple[float, float, float, str, str]], list[int]]:
    """Build sorted boundary pin item tuples and adjacent pin spacing gaps."""
    items = [
        (
            sum(t[0] for t in ts) / float(len(ts)),
            sum(t[1] for t in ts) / float(len(ts)),
            sum(t[2] for t in ts) / float(len(ts)),
            ts[0][-1],
            pname,
        )
        for pname, ts in target_map.items()
        if ts
    ]
    items.sort(
        key=lambda item: (
            round(item[1] + _PIN_X_TIE_WT * item[2], _ROUND_3),
            item[-2],
            round(item[0], 2),
            _natural_key(item[-1]),
        )
    )
    n_pins = len(items)
    min_gap = max(
        _MIN_PIN_GAP,
        min(10, (die_h - _PIN_GAP_H_DEDUCT) // max(2, n_pins * 2)),
    )
    gaps = [
        _PIN_SAME_SINK_GAP if items[i][-2] == items[i + 1][-2] else min_gap
        for i in range(max(0, n_pins - 1))
    ]
    return items, gaps


def _untangle_single_component_ports(
    cname: str,
    comp: MutableJsonMap,
    inst_port_ref: dict[tuple[str, str], tuple[MutableJsonMap, int]],
    scale: float,
    comp_map: dict[str, MutableJsonMap],
    inst_map: dict[str, JsonMap],
    cells_map: JsonMap,
    pin_map: dict[str, JsonMap],
) -> None:
    """Attempt output and input port swaps on one splitter, merger, or balancer component."""
    macro = inst_map.get(cname, {}).get("cell", comp.get("macro", ""))
    if "split2" in macro or "balancer2" in macro:
        _try_swap_port_pair(
            cname, "y1", "y2", False, inst_port_ref, scale, comp_map, inst_map, cells_map, pin_map
        )
    if "merge2" in macro or "balancer2" in macro:
        p_a = "a" if (cname, "a") in inst_port_ref else "a1"
        p_b = "b" if (cname, "b") in inst_port_ref else "a2"
        _try_swap_port_pair(
            cname, p_a, p_b, True, inst_port_ref, scale, comp_map, inst_map, cells_map, pin_map
        )


def _def_connection_point(
    iname: str,
    port: str,
    nname: str,
    scale: float,
    comp_map: dict[str, MutableJsonMap],
    inst_map: dict[str, JsonMap],
    cells_map: JsonMap,
    pin_map: dict[str, JsonMap],
) -> tuple[float, float] | None:
    """Return (x, y) in tiles for a single DEF net connection endpoint."""
    if iname == "PIN":
        p = pin_map.get(nname) or pin_map.get(port)
        return (float(p["x"]) * scale, float(p["y"]) * scale) if p else None
    if iname not in comp_map:
        return None
    c = comp_map[iname]
    macro = inst_map.get(iname, {}).get("cell", c.get("macro", ""))
    cmeta = cells_map.get(macro, {})
    is_tree = "split" in macro or "merge" in macro
    w = float(cmeta.get("width", 1 if is_tree else _DEFAULT_MACRO_DIM))
    h = float(cmeta.get("height", 2 if is_tree else _DEFAULT_MACRO_DIM))
    return (float(c["x"]) * scale + w / 2.0, float(c["y"]) * scale + h / 2.0)


class AnalyticalPlacer:
    """Analytical global placer and router-aware directed legalizer."""

    def __init__(
        self,
        cell_pad_x: int = _PAD_X_DEFAULT,
        cell_pad_y: int = _PAD_Y_DEFAULT,
        split_pad_x: int = _SPLIT_PAD_X_DEFAULT,
        split_pad_y: int = 2,
        core_margin: int = _CORE_MARGIN_DEFAULT,
        max_core_utilization: float = _DEFAULT_MAX_CORE_UTIL,
    ) -> None:
        self.cell_pad_x = max(_PAD_X_DEFAULT, cell_pad_x)
        self.cell_pad_y = max(2, cell_pad_y)
        self.split_pad_x = max(_MIN_SPLIT_PAD_X, split_pad_x)
        self.split_pad_y = max(2, split_pad_y)
        self.core_margin = max(_PAD_X_DEFAULT, core_margin)
        self.max_core_utilization = max(_MIN_CORE_UTIL, min(_MAX_CORE_UTIL, max_core_utilization))
        self._sink_wire_rates: dict[str, float] = {}
        self._driver_wire_rates: dict[str, float] = {}
        self._belt_capacity: float = _DEFAULT_BELT_CAP

    def _make_node(
        self,
        name: str,
        macro: str,
        recipe: str,
        rank: int,
        cell_meta: JsonMap | None = None,
    ) -> PlacerNode:
        """Create a PlacerNode with footprint and clearance halos."""
        w, h = _resolve_macro_footprint(macro, cell_meta)
        ntype = _classify_node_type(name, macro, cell_meta)
        if ntype in ("split", "merge", "balancer"):
            pad_w, pad_e = self.split_pad_x, self.split_pad_x
            pad_n, pad_s = self.split_pad_y, self.split_pad_y
        elif ntype in ("display", "combinator"):
            pad_w, pad_e = _PAD_Y_DEFAULT, _PAD_Y_DEFAULT
            pad_n, pad_s = 1, 1
        else:
            pad_w, pad_e = self.cell_pad_x, self.cell_pad_x
            pad_n, pad_s = self.cell_pad_y, self.cell_pad_y

        return PlacerNode(
            name=name,
            macro=macro,
            recipe=recipe,
            width=w,
            height=h,
            rank=rank,
            node_type=ntype,
            pad_w=pad_w,
            pad_e=pad_e,
            pad_n=pad_n,
            pad_s=pad_s,
        )

    def _extract_netlist_wire_rates(self, nl: NetlistLike) -> None:
        """Populate per-wire sink and driver rate tables from a Netlist object."""
        self._sink_wire_rates = {}
        self._driver_wire_rates = {}
        b_cap = float(getattr(nl, "belt_capacity", 0.0))
        if b_cap > 0.0:
            self._belt_capacity = b_cap
        for mat_name, net_obj in getattr(nl, "nets", {}).items():
            s_mat = _sanitize(mat_name)
            for snk_name, s_rate in getattr(net_obj, "sink_rates", {}).items():
                self._sink_wire_rates[f"{s_mat}__{snk_name}"] = float(s_rate)
            drv_rates = getattr(net_obj, "driver_rates", {})
            drv_rated = getattr(net_obj, "driver_rated", {})
            for drv_name in set(drv_rates) | set(drv_rated):
                d_rate = float(drv_rated.get(drv_name, drv_rates.get(drv_name, 1.0)))
                self._driver_wire_rates[f"{s_mat}__{drv_name}"] = d_rate

    def build_graph_from_netlist(
        self,
        nl: NetlistLike,
        merge_plans: MergePlansMap | None = None,
        split_plans: SplitPlansMap | None = None,
        balancer_plans: BalancerPlansMap | None = None,
    ) -> tuple[dict[str, PlacerNode], list[PlacerEdge], list[str], list[str]]:
        """Extract nodes and directed edges from a synthesized Netlist object."""
        nodes: dict[str, PlacerNode] = {}
        for inst in nl.instances:
            cell = nl.cells.get(inst.cell_key) or _default_netlist_cell_meta(inst.cell_key)
            nodes[inst.name] = self._make_node(
                name=inst.name,
                macro=inst.cell_key,
                recipe=inst.recipe or "",
                rank=inst.rank,
                cell_meta=cell,
            )

        wire_drivers: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
        wire_sinks: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
        raw_fluids = getattr(nl, "fluids", set())
        fluid_set = {_sanitize(f) for f in raw_fluids} | set(raw_fluids)

        inputs_sanitized = [_sanitize(m) for m in getattr(nl, "inputs", [])]
        outputs_sanitized = [_sanitize(m) for m in getattr(nl, "outputs", [])]
        for inp in inputs_sanitized:
            wire_drivers[inp].append((f"PIN_IN:{inp}", 0.0))
        for outp in outputs_sanitized:
            wire_sinks[outp].append((f"PIN_OUT:{outp}", 0.0))

        _register_merge_plans(nodes, wire_drivers, wire_sinks, merge_plans)
        _register_split_plans(nodes, wire_drivers, wire_sinks, split_plans)
        _register_balancer_plans(nodes, wire_drivers, wire_sinks, balancer_plans)

        nl_nets = getattr(nl, "nets", {})
        sink_wire_map, drv_wire_map = _build_netlist_port_wire_index(nl_nets)
        for inst in nl.instances:
            _connect_netlist_instance_ports(
                inst,
                nodes[inst.name],
                nodes,
                nl.cells.get(inst.cell_key, {}),
                nl_nets,
                sink_wire_map,
                drv_wire_map,
                wire_drivers,
                wire_sinks,
                merge_plans,
                split_plans,
                balancer_plans,
            )

        self._extract_netlist_wire_rates(nl)
        edges = _build_edges_from_wire_endpoints(wire_drivers, wire_sinks, fluid_set)
        return nodes, edges, inputs_sanitized, outputs_sanitized

    def _extract_data_wire_rates(
        self,
        inst_list: list[MutableJsonMap],
        cells_map: JsonMap,
    ) -> None:
        """Populate per-wire sink and driver rate tables from netlist_data dictionaries."""
        self._sink_wire_rates = {}
        self._driver_wire_rates = {}
        for inst in inst_list:
            _record_inst_data_wire_rates(
                inst, cells_map, self._sink_wire_rates, self._driver_wire_rates
            )

    def _build_nodes_from_data(
        self,
        inst_list: list[MutableJsonMap],
        cells_map: JsonMap,
        def_data: JsonMap | None,
    ) -> dict[str, PlacerNode]:
        """Instantiate PlacerNode dictionary from netlist_data and optional def_data."""
        inst_meta_map = {inst["name"]: inst for inst in inst_list}
        nodes: dict[str, PlacerNode] = {}
        if def_data and def_data.get("components"):
            for comp in def_data["components"]:
                cname = comp["name"]
                inst_meta = inst_meta_map.get(cname, {})
                macro = inst_meta.get("cell", comp.get("macro", ""))
                recipe = inst_meta.get("recipe", "")
                rank = int(inst_meta.get("rank", 0))
                cell_meta = cells_map.get(macro) or cells_map.get(comp.get("macro", ""))
                nodes[cname] = self._make_node(cname, macro, recipe, rank, cell_meta)
            return nodes

        for inst in inst_list:
            cname = inst["name"]
            macro = inst.get("cell", "")
            recipe = inst.get("recipe", "")
            rank = int(inst.get("rank", 0))
            nodes[cname] = self._make_node(cname, macro, recipe, rank, cells_map.get(macro))
        return nodes

    def build_graph_from_data(
        self,
        netlist_data: JsonMap,
        def_data: JsonMap | None = None,
    ) -> tuple[dict[str, PlacerNode], list[PlacerEdge], list[str], list[str]]:
        """Extract nodes and directed edges from netlist_data and optional def_data."""
        cells_map = netlist_data.get("cells", {})
        inst_list = netlist_data.get("instances", [])
        raw_fluids = netlist_data.get("fluids", [])
        fluid_set = {_sanitize(f) for f in raw_fluids} | set(raw_fluids)

        self._extract_data_wire_rates(inst_list, cells_map)
        nodes = self._build_nodes_from_data(inst_list, cells_map, def_data)
        inputs_sanitized = [_sanitize(m) for m in netlist_data.get("inputs", [])]
        outputs_sanitized = [_sanitize(m) for m in netlist_data.get("outputs", [])]

        if def_data and def_data.get("nets"):
            edges = _build_edges_from_def_nets(
                def_data, nodes, cells_map, inputs_sanitized, fluid_set
            )
        else:
            edges = _build_inferred_edges_from_data(
                nodes, cells_map, inputs_sanitized, outputs_sanitized, fluid_set
            )
        return nodes, edges, inputs_sanitized, outputs_sanitized

    def _compute_flow_depths_and_columns(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        use_chassis: bool = False,
    ) -> None:
        """Assign topological flow depths and tighten splitter/merger column indices."""
        if not nodes:
            return

        adj, rev_adj, topo_order = _build_acyclic_flow_graph(nodes, edges)
        has_power_switch = any("power_switch" in nd.macro for nd in nodes.values())
        has_power_plant = any(nd.recipe in ("boiler-steam", "steam-power") for nd in nodes.values())
        min_switched_depth = _SWITCHED_MIN_DEPTH if (has_power_switch and has_power_plant) else 0

        depth = _compute_asap_depths(
            nodes, topo_order, rev_adj, bool(edges), has_power_switch, min_switched_depth
        )

        active_gw = getattr(self, "_active_grid_w", 1)
        has_block_assignments = (
            use_chassis
            and active_gw != _SEVEN_GRID_W
            and any(nd.assigned_block != (0, 0) for nd in nodes.values())
        )
        mach_group_of: dict[str, list[str]] = {}
        if has_block_assignments:
            mach_group_of = _build_parallel_machine_groups(nodes, topo_order, adj)
            _sync_parallel_group_depths(topo_order, rev_adj, mach_group_of, depth)

        _place_telemetry_depths(nodes, topo_order, rev_adj, depth, has_power_switch)

        fluid_nodes = {u for e in edges if e.is_fluid for u in (e.source, e.sink) if u in nodes}
        _tighten_backward_depths(
            nodes,
            topo_order,
            adj,
            rev_adj,
            fluid_nodes,
            mach_group_of,
            depth,
            use_chassis,
            min_switched_depth,
        )

        if not has_power_switch:
            _align_power_columns(nodes, topo_order, adj, rev_adj, depth)

        unique_depths = sorted(set(depth.values()))
        depth_remap = {old_d: new_d for new_d, old_d in enumerate(unique_depths)}
        for u, node in nodes.items():
            c_idx = depth_remap[depth[u]]
            node.flow_depth = c_idx
            node.col_index = c_idx

    def compute_auto_chassis_grid(
        self,
        nl: NetlistLike,
        merge_plans: MergePlansMap | None = None,
        split_plans: SplitPlansMap | None = None,
        balancer_plans: BalancerPlansMap | None = None,
        core_w: int = _DEFAULT_CHASSIS_CORE_DIM,
        core_h: int = _DEFAULT_CHASSIS_CORE_DIM,
    ) -> tuple[int, int]:
        """Compute minimal square city-block grid (W, H) from core area and terminal demand."""
        nodes, _edges, _ins, _outs = self.build_graph_from_netlist(
            nl, merge_plans, split_plans, balancer_plans
        )
        single_core_capacity = float(
            max(_MIN_EFF_CORE_DIM, core_w - 10) * max(_MIN_EFF_CORE_DIM, core_h - _CORE_H_DEDUCT)
        )
        boiler_inputs = {
            inp for nd in nodes.values() if nd.recipe == "boiler-steam" for inp in nd.inputs
        }
        non_power_inputs = {
            inp
            for nd in nodes.values()
            if nd.node_type == "machine" and nd.recipe not in ("boiler-steam", "steam-power")
            for inp in nd.inputs
        }
        power_only_fuels = boiler_inputs - non_power_inputs
        total_demand, num_boilers, num_labs = _accumulate_node_core_demand(nodes, power_only_fuels)

        target_cap_per_block = single_core_capacity * _TARGET_BLOCK_CAP_FRAC
        req_blocks = max(1, math.ceil(total_demand / max(1.0, target_cap_per_block)))
        base_side = max(1, math.ceil(math.sqrt(req_blocks)))
        has_many_outputs = len(getattr(nl, "outputs", [])) >= _MIN_OUTPUTS_FOR_POWER_BLK
        power_blocks = (
            math.ceil(num_boilers / _BOILERS_PER_BLK_F) if (has_many_outputs or num_labs > 0) else 0
        )
        lab_blocks = math.ceil(num_labs / _LABS_PER_HALF_BLK_F) * 2 if num_labs > 0 else 0
        extra_blocks = power_blocks + lab_blocks
        side = base_side + round(extra_blocks / float(base_side)) if extra_blocks else base_side
        return side, side

    def _find_tarjan_sccs(
        self,
        machine_names: list[str],
        mach_adj: dict[str, dict[str, float]],
    ) -> list[list[str]]:
        """Find strongly connected components in the machine graph with Tarjan's algorithm."""
        state = _TarjanState()
        for u in machine_names:
            if u not in state.indices:
                _tarjan_strongconnect(u, mach_adj, state)
        state.sccs.reverse()
        return state.sccs

    def _optimize_2d_block_assignment(
        self,
        dp_buckets: list[list[str]],
        active_blocks: list[tuple[int, int]],
        nodes: dict[str, PlacerNode],
        mach_adj: dict[str, dict[str, float]],
        west_in_weight: dict[str, float],
        east_out_weight: dict[str, float],
        reachable_outs: dict[str, set[str]],
        grid_w: int,
    ) -> list[tuple[int, int]]:
        """Assign balanced machine buckets to 2D grid blocks with Harmonic-X and KL/FM search."""
        num_b = len(active_blocks)
        k_bins = len(dp_buckets)
        buckets: list[list[str]] = list(dp_buckets) + [[] for _ in range(max(0, num_b - k_bins))]

        b_adj = _build_bucket_adjacency(buckets, mach_adj, num_b)
        b_west = [
            max((west_in_weight.get(u, 0.0) for u in b_list), default=0.0) for b_list in buckets
        ]
        b_east = [
            max((east_out_weight.get(u, 0.0) for u in b_list), default=0.0) for b_list in buckets
        ]
        max_rank = max((nodes[u].flow_depth for b_list in buckets for u in b_list), default=1)
        b_avg_rank = [
            (sum(nodes[u].flow_depth for u in b_list) / float(len(b_list)))
            if b_list
            else float(max_rank) * _HALF
            for b_list in buckets
        ]
        b_recipes = [{nodes[u].recipe for u in b_list} for b_list in buckets]
        b_outs = [
            set().union(*(reachable_outs.get(u, set()) for u in b_list)) if b_list else set()
            for b_list in buckets
        ]
        b_contig = _build_bucket_contiguity(b_recipes, b_outs, num_b, k_bins)
        has_lab_buckets = any("infinite-research" in r for r in b_recipes)

        max_x = float(max(1, grid_w - 1))
        penult_x = max(0.0, max_x - 1.0)
        x_val = [
            _initial_bucket_harmonic_x(
                i,
                buckets,
                b_west,
                b_east,
                b_avg_rank,
                b_recipes,
                max_rank,
                max_x,
                penult_x,
                has_lab_buckets,
            )
            for i in range(num_b)
        ]
        _relax_bucket_harmonic_x(
            x_val,
            b_adj,
            b_west,
            b_east,
            b_avg_rank,
            b_recipes,
            k_bins,
            max_rank,
            max_x,
            penult_x,
            has_lab_buckets,
        )

        model = _BlockOptModel(
            num_b=num_b,
            k_bins=k_bins,
            grid_w=grid_w,
            max_x=max_x,
            penult_x=penult_x,
            has_lab_buckets=has_lab_buckets,
            buckets=buckets,
            b_adj=b_adj,
            b_contig=b_contig,
            b_west=b_west,
            b_east=b_east,
            b_avg_rank=b_avg_rank,
            b_recipes=b_recipes,
            x_val=x_val,
        )

        sorted_b = sorted(range(num_b), key=lambda i: _bucket_col_sort_key(i, model))
        grid_blocks_col_order = sorted(active_blocks, key=lambda b: (b[0], b[1]))
        perm: list[tuple[int, int]] = [(0, 0)] * num_b
        for pos_idx, b_idx in enumerate(sorted_b):
            perm[b_idx] = grid_blocks_col_order[pos_idx]

        for _pass in range(num_b * _KL_PASS_MULT):
            best_pair = _find_best_kl_swap(perm, model)
            if best_pair is None:
                break
            si, sj = best_pair
            perm[si], perm[sj] = perm[sj], perm[si]

        return perm[:k_bins]

    def _build_all_domain_groups(
        self,
        nodes: dict[str, PlacerNode],
        machine_names: list[str],
        sccs: list[list[str]],
        fluid_machines: set[str],
        lab_set: set[str],
        reachable_outs: dict[str, set[str]],
        west_in_weight: dict[str, float],
        mach_adj: dict[str, dict[str, float]],
        single_core_capacity: float,
        grid_w: int,
        has_cycles: bool,
    ) -> tuple[list[list[str]], list[str], set[str], list[list[str]], list[list[str]], list[str]]:
        """Classify machines by domain and build ordered groups for DP partitioning."""
        cyclic_nodes = {u for scc in sccs if len(scc) > 1 for u in scc}
        acyclic_machines = [u for u in machine_names if u not in cyclic_nodes]
        power_machine_names = _sort_machines_by_flow(
            nodes,
            [u for u in acyclic_machines if nodes[u].recipe in ("boiler-steam", "steam-power")],
        )
        terminal_set = set(power_machine_names) | lab_set
        lab_acyclic = [u for u in acyclic_machines if u in lab_set]
        non_power_acyclic = [u for u in acyclic_machines if u not in terminal_set]
        fluid_machine_names = _sort_machines_by_flow(
            nodes, [u for u in non_power_acyclic if u in fluid_machines]
        )
        furnace_names, shared_solid, dedicated_by_out = _classify_solid_domain_machines(
            nodes, non_power_acyclic, fluid_machines, reachable_outs, bool(lab_set)
        )

        groups: list[list[str]] = []
        grp_domains: list[str] = []
        atomic_first: set[str] = set()
        _append_scc_domain_groups(
            nodes, sccs, single_core_capacity, groups, grp_domains, atomic_first
        )
        _extend_domain_groups(
            groups,
            grp_domains,
            _chunk_machines(nodes, furnace_names, _MAX_GRP_LEN, grid_w, has_cycles),
            "furnace",
        )
        _append_fluid_and_shared_groups(
            nodes,
            fluid_machine_names,
            shared_solid,
            west_in_weight,
            single_core_capacity,
            bool(lab_set),
            grid_w,
            has_cycles,
            groups,
            grp_domains,
            atomic_first,
        )
        lab_groups, power_groups = _append_dedicated_and_terminal_groups(
            nodes,
            dedicated_by_out,
            lab_acyclic,
            power_machine_names,
            mach_adj,
            single_core_capacity,
            bool(lab_set),
            grid_w,
            has_cycles,
            groups,
            grp_domains,
            atomic_first,
        )
        return groups, grp_domains, atomic_first, lab_groups, power_groups, power_machine_names

    def _assign_dp_buckets_to_blocks(
        self,
        dp_buckets: list[list[str]],
        active_blocks: list[tuple[int, int]],
        nodes: dict[str, PlacerNode],
        mach_adj: dict[str, dict[str, float]],
        west_in_weight: dict[str, float],
        east_out_weight: dict[str, float],
        reachable_outs: dict[str, set[str]],
        grid_w: int,
        grid_h: int,
        k_bins: int,
        has_cycles: bool,
        has_power_machines: bool,
        assign_fn: BlockAssignFn,
        blk_to_seq_idx: dict[tuple[int, int], int],
    ) -> dict[tuple[int, int], int]:
        """Map DP machine buckets onto 2D grid blocks and assign their member nodes."""
        if grid_w >= _LARGE_GRID_W or has_cycles:
            bucket_to_blk = self._optimize_2d_block_assignment(
                dp_buckets=dp_buckets,
                active_blocks=active_blocks,
                nodes=nodes,
                mach_adj=mach_adj,
                west_in_weight=west_in_weight,
                east_out_weight=east_out_weight,
                reachable_outs=reachable_outs,
                grid_w=grid_w,
            )
            for b_idx, bucket in enumerate(dp_buckets):
                blk = bucket_to_blk[b_idx]
                for u in bucket:
                    assign_fn(u, blk)
            return {b: b[1] * grid_w + b[0] for b in active_blocks}

        if (
            grid_w == _CENTER_HOLE_GRID
            and grid_h == _CENTER_HOLE_GRID
            and k_bins == _CENTER_HOLE_GRID
            and has_power_machines
        ):
            blk_targets = [(0, 0), (1, 0), (0, 2)]
        else:
            blk_targets = active_blocks[: len(dp_buckets)]
        for b_idx, bucket in enumerate(dp_buckets):
            blk = blk_targets[b_idx]
            for u in bucket:
                assign_fn(u, blk)
        return blk_to_seq_idx

    def _assign_merger_blocks(
        self,
        nodes: dict[str, PlacerNode],
        active_blocks: list[tuple[int, int]],
        in_drivers: dict[str, list[str]],
        out_sinks: dict[str, list[str]],
        pin_out_nodes: set[str],
        assigned: set[str],
        block_area: dict[tuple[int, int], float],
        bay_counts: dict[tuple[int, int], int],
        blk_to_seq_idx: dict[tuple[int, int], int],
        grid_w: int,
        grid_h: int,
        assign_fn: BlockAssignFn,
    ) -> None:
        """Assign merger nodes bottom-up from connected machine drivers to root."""
        rem_merges = [
            name for name, nd in nodes.items() if name not in assigned and nd.node_type == "merge"
        ]
        rem_merges.sort(key=lambda name: (nodes[name].flow_depth, _natural_key(name)))
        while rem_merges:
            ready_idx = next(
                (
                    idx
                    for idx, m_name in enumerate(rem_merges)
                    if all(d in assigned for d in in_drivers.get(m_name, []))
                ),
                0,
            )
            m_name = rem_merges.pop(ready_idx)
            best_blk = _select_best_merger_block(
                m_name,
                nodes,
                active_blocks,
                in_drivers,
                out_sinks,
                pin_out_nodes,
                assigned,
                block_area,
                bay_counts,
                blk_to_seq_idx,
                grid_w,
                grid_h,
            )
            assign_fn(m_name, best_blk)
            if nodes[m_name].is_matrix_node:
                bay_counts[best_blk] += 1

    def _assign_splitter_balancer_blocks(
        self,
        nodes: dict[str, PlacerNode],
        active_blocks: list[tuple[int, int]],
        in_drivers: dict[str, list[str]],
        out_sinks: dict[str, list[str]],
        pin_in_nodes: set[str],
        assigned: set[str],
        block_bins: dict[tuple[int, int], list[str]],
        block_area: dict[tuple[int, int], float],
        bay_counts: dict[tuple[int, int], int],
        blk_to_seq_idx: dict[tuple[int, int], int],
        grid_w: int,
        grid_h: int,
        assign_fn: BlockAssignFn,
    ) -> None:
        """Assign splitter and balancer nodes bottom-up in reverse-topological order."""
        rem_splits = [
            name
            for name, nd in nodes.items()
            if name not in assigned and nd.node_type in ("split", "balancer")
        ]
        rem_splits.sort(key=lambda name: (-nodes[name].flow_depth, _natural_key(name)))
        total_bals = sum(1 for nd in nodes.values() if nd.node_type == "balancer")
        while rem_splits:
            ready_idx = next(
                (
                    idx
                    for idx, s_name in enumerate(rem_splits)
                    if all(v in assigned for v in out_sinks.get(s_name, []))
                ),
                0,
            )
            s_name = rem_splits.pop(ready_idx)
            best_blk = _select_best_splitter_block(
                s_name,
                nodes,
                active_blocks,
                in_drivers,
                out_sinks,
                pin_in_nodes,
                assigned,
                block_bins,
                block_area,
                bay_counts,
                blk_to_seq_idx,
                grid_w,
                grid_h,
                total_bals,
            )
            assign_fn(s_name, best_blk)
            if nodes[s_name].is_matrix_node:
                bay_counts[best_blk] += 1

    def _assign_remaining_telemetry_blocks(
        self,
        nodes: dict[str, PlacerNode],
        active_blocks: list[tuple[int, int]],
        in_drivers: dict[str, list[str]],
        out_sinks: dict[str, list[str]],
        assigned: set[str],
        block_area: dict[tuple[int, int], float],
        assign_fn: BlockAssignFn,
    ) -> None:
        """Assign remaining telemetry combinators and displays to connected driver blocks."""
        rem_names = [name for name in nodes if name not in assigned]
        rem_names.sort(key=lambda name: (nodes[name].flow_depth, _natural_key(name)))
        for u_name in rem_names:
            nb_blks = _find_telemetry_neighbor_blocks(
                u_name, nodes, in_drivers, out_sinks, assigned, block_area
            )
            best_blk = nb_blks[0] if nb_blks else _least_loaded_block(active_blocks, block_area)
            assign_fn(u_name, best_blk)

    def _partition_machines_phase1(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        machine_names: list[str],
        active_blocks: list[tuple[int, int]],
        single_core_capacity: float,
        grid_w: int,
        grid_h: int,
        distribute_across_blocks: bool,
        assign_fn: BlockAssignFn,
        blk_to_seq_idx: dict[tuple[int, int], int],
    ) -> dict[tuple[int, int], int]:
        """Run Phase 1 domain-coherent machine partitioning across active blocks."""
        fwd_adj, rev_adj, fluid_machines = _build_material_hub_graph(nodes, edges)
        machine_set = set(machine_names)
        lab_set = {u for u in machine_names if nodes[u].recipe == "infinite-research"}
        lab_feed_nodes = (
            {
                u_name: f"PIN_OUT:{_sanitize(nd.recipe)}"
                for u_name, nd in nodes.items()
                if u_name not in lab_set
                and nd.recipe
                and bool(set(fwd_adj.get(u_name, [])) & lab_set)
            }
            if lab_set
            else {}
        )

        mach_adj: dict[str, dict[str, float]] = collections.defaultdict(
            lambda: collections.defaultdict(float)
        )
        west_in_weight: dict[str, float] = collections.defaultdict(float)
        east_out_weight: dict[str, float] = collections.defaultdict(float)
        for u in machine_names:
            _trace_machine_downstream(
                u, nodes, fwd_adj, machine_set, lab_set, mach_adj, east_out_weight
            )
            _trace_machine_upstream(
                u, nodes, rev_adj, machine_set, fluid_machines, grid_w, west_in_weight
            )

        sccs = self._find_tarjan_sccs(machine_names, mach_adj)
        has_cycles = any(len(scc) > 1 for scc in sccs)
        reachable_outs = _compute_reachable_pin_outs(
            machine_names, nodes, fwd_adj, lab_set, lab_feed_nodes
        )
        (
            groups,
            grp_domains,
            atomic_first,
            lab_groups,
            power_groups,
            power_machine_names,
        ) = self._build_all_domain_groups(
            nodes,
            machine_names,
            sccs,
            fluid_machines,
            lab_set,
            reachable_outs,
            west_in_weight,
            mach_adj,
            single_core_capacity,
            grid_w,
            has_cycles,
        )
        pref, n_lab_bins, n_power_bins, k_bins = _compute_dp_bin_counts(
            groups,
            lab_groups,
            power_groups,
            nodes,
            atomic_first,
            single_core_capacity,
            len(active_blocks),
            grid_w,
            distribute_across_blocks,
        )
        if k_bins <= 1 and not lab_groups and not power_groups:
            blk0 = active_blocks[0]
            for grp in groups:
                for u in grp:
                    assign_fn(u, blk0)
            return blk_to_seq_idx

        grp_recipes = [nodes[grp[0]].recipe if grp else "" for grp in groups]
        dp_buckets = _solve_dp_group_segments(
            groups, grp_domains, grp_recipes, pref, k_bins, bool(lab_set), grid_w
        )
        _append_merged_bin_groups(dp_buckets, lab_groups, n_lab_bins)
        _append_merged_bin_groups(dp_buckets, power_groups, n_power_bins)
        return self._assign_dp_buckets_to_blocks(
            dp_buckets,
            active_blocks,
            nodes,
            mach_adj,
            west_in_weight,
            east_out_weight,
            reachable_outs,
            grid_w,
            grid_h,
            k_bins,
            has_cycles,
            bool(power_machine_names),
            assign_fn,
            blk_to_seq_idx,
        )

    def _rewire_and_refresh_block_graph(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        blk_to_seq_idx: dict[tuple[int, int], int],
        assigned: set[str],
        assign_fn: BlockAssignFn,
        in_drivers: dict[str, list[str]],
        out_sinks: dict[str, list[str]],
    ) -> None:
        """Rewire splitter/merger trees per block and refresh adjacency tables."""
        self._last_rewired_ports = self._rewire_trees_for_blocks(
            nodes=nodes,
            edges=edges,
            blk_to_seq_idx=blk_to_seq_idx,
            assigned=assigned,
            assign_fn=assign_fn,
        )
        if not self._last_rewired_ports:
            return
        in_drivers.clear()
        out_sinks.clear()
        for e in edges:
            if e.source in nodes and e.sink in nodes and e.source != e.sink:
                out_sinks[e.source].append(e.sink)
                in_drivers[e.sink].append(e.source)
        self._compute_flow_depths_and_columns(nodes, edges, use_chassis=True)

    def _partition_into_blocks(
        self,
        nodes: dict[str, PlacerNode],
        grid_w: int,
        grid_h: int,
        core_w: int = _DEFAULT_CHASSIS_CORE_DIM,
        core_h: int = _DEFAULT_CHASSIS_CORE_DIM,
        empty_blocks: set[tuple[int, int]] | None = None,
        distribute_across_blocks: bool = False,
        edges: list[PlacerEdge] | None = None,
    ) -> dict[tuple[int, int], list[str]]:
        """Partition nodes into chassis blocks with domain-coherent phases."""
        active_blocks = _get_chassis_block_sequence(grid_w, grid_h, empty_blocks)
        total_padded_area = sum(_node_padded_area(n) for n in nodes.values())
        single_core_capacity = float(core_w * core_h) * self.max_core_utilization
        if (
            len(active_blocks) <= 1
            or not nodes
            or (not distribute_across_blocks and total_padded_area <= single_core_capacity)
        ):
            blk = active_blocks[0] if active_blocks else (0, 0)
            for node in nodes.values():
                node.assigned_block = blk
            return {blk: list(nodes.keys())}

        machine_names = [name for name, nd in nodes.items() if nd.node_type in ("machine", "pump")]
        if not machine_names or not edges:
            return _fallback_partition_blocks(
                nodes,
                active_blocks,
                total_padded_area,
                single_core_capacity,
                distribute_across_blocks,
            )

        block_bins: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
        block_area: dict[tuple[int, int], float] = dict.fromkeys(active_blocks, 0.0)
        in_drivers, out_sinks = _build_internal_driver_sink_maps(nodes, edges)
        assigned: set[str] = set()

        def _assign_to_best_block(u: str, preferred_blk: tuple[int, int]) -> None:
            nodes[u].assigned_block = preferred_blk
            block_bins[preferred_blk].append(u)
            block_area[preferred_blk] += _node_padded_area(nodes[u])
            assigned.add(u)

        blk_to_seq_idx = {b: idx for idx, b in enumerate(active_blocks)}
        blk_to_seq_idx = self._partition_machines_phase1(
            nodes,
            edges,
            machine_names,
            active_blocks,
            single_core_capacity,
            grid_w,
            grid_h,
            distribute_across_blocks,
            _assign_to_best_block,
            blk_to_seq_idx,
        )

        self._last_rewired_ports: dict[str, dict[str, str]] = {}
        self._rewire_and_refresh_block_graph(
            nodes, edges, blk_to_seq_idx, assigned, _assign_to_best_block, in_drivers, out_sinks
        )

        pin_in_nodes, pin_out_nodes = _extract_boundary_pin_connected_sets(nodes, edges)
        bay_counts: dict[tuple[int, int], int] = collections.Counter()

        self._assign_merger_blocks(
            nodes,
            active_blocks,
            in_drivers,
            out_sinks,
            pin_out_nodes,
            assigned,
            block_area,
            bay_counts,
            blk_to_seq_idx,
            grid_w,
            grid_h,
            _assign_to_best_block,
        )
        self._assign_splitter_balancer_blocks(
            nodes,
            active_blocks,
            in_drivers,
            out_sinks,
            pin_in_nodes,
            assigned,
            block_bins,
            block_area,
            bay_counts,
            blk_to_seq_idx,
            grid_w,
            grid_h,
            _assign_to_best_block,
        )

        if grid_w >= _LARGE_GRID_W:
            _relocate_matrix_splitters_for_large_grid(
                nodes,
                active_blocks,
                in_drivers,
                out_sinks,
                assigned,
                block_bins,
                block_area,
                bay_counts,
                blk_to_seq_idx,
            )

        self._assign_remaining_telemetry_blocks(
            nodes, active_blocks, in_drivers, out_sinks, assigned, block_area, _assign_to_best_block
        )
        return {blk: block_bins[blk] for blk in active_blocks if block_bins[blk]}

    def _precompute_splitter_slices(
        self,
        nodes: dict[str, PlacerNode],
        blk_to_seq_idx: dict[tuple[int, int], int],
        valid_s_by_recipe: dict[str, list[tuple[str, list[str], set[str], list[PlacerEdge]]]],
        valid_m_by_recipe: dict[
            str, list[tuple[str, tuple[str, str], list[tuple[str, str]], list[PlacerEdge]]]
        ],
    ) -> tuple[
        dict[str, list[float]],
        dict[str, tuple[list[str], list[str], list[str], list[PlacerEdge], list[int]]],
    ]:
        """Precompute leaf slices and per-bus demand targets for splitter forests."""
        s_slice_demands: dict[str, list[float]] = {}
        s_precomputed: dict[
            str, tuple[list[str], list[str], list[str], list[PlacerEdge], list[int]]
        ] = {}
        b_cap = max(1.0, getattr(self, "_belt_capacity", _DEFAULT_BELT_CAP))

        for r_key, comp_infos in sorted(valid_s_by_recipe.items()):
            comp_infos.sort(key=lambda item: _natural_key(item[0]))
            root_wires = [item[0] for item in comp_infos]
            orig_sizes = [len(item[-1]) for item in comp_infos]
            avail_splits = sorted((s for item in comp_infos for s in item[1]), key=_natural_key)
            avail_int_w = sorted((w for item in comp_infos for w in item[2]), key=_natural_key)
            all_leaves = sorted(
                (e for item in comp_infos for e in item[-1]),
                key=lambda e: (
                    blk_to_seq_idx.get(nodes[e.sink].assigned_block, 0),
                    nodes[e.sink].rank,
                    nodes[e.sink].flow_depth,
                    _natural_key(e.sink),
                    _natural_key(e.wire),
                ),
            )
            total_s = len(all_leaves)
            k_trees = len(root_wires)
            blk_bounds = [
                idx
                for idx in range(1, total_s)
                if nodes[all_leaves[idx].sink].assigned_block
                != nodes[all_leaves[idx - 1].sink].assigned_block
            ]
            if len(valid_m_by_recipe.get(r_key, [])) > 1 and k_trees > 1:
                cuts, demands = _compute_rate_splitter_cuts(
                    all_leaves, blk_bounds, k_trees, self._sink_wire_rates, b_cap
                )
                s_slice_demands[r_key] = demands
            else:
                cuts = _compute_orig_splitter_cuts(orig_sizes, blk_bounds, total_s, k_trees)
            s_precomputed[r_key] = (root_wires, avail_splits, avail_int_w, all_leaves, cuts)
        return s_slice_demands, s_precomputed

    def _rewire_multibus_merges(
        self,
        nodes: dict[str, PlacerNode],
        blk_to_seq_idx: dict[tuple[int, int], int],
        valid_m_by_recipe: dict[
            str, list[tuple[str, tuple[str, str], list[tuple[str, str]], list[PlacerEdge]]]
        ],
        s_slice_demands: dict[str, list[float]],
        rewire_map: dict[str, dict[str, str]],
        assign_fn: BlockAssignFn,
    ) -> None:
        """Re-parenthesize multibus merger forests matched to downstream splitter slice demands."""
        for r_key, m_comp_infos in sorted(valid_m_by_recipe.items()):
            m_comp_infos.sort(key=lambda item: _natural_key(item[0]))
            k_m = len(m_comp_infos)
            if k_m == 1:
                _r_wire, root_pair, avail_pairs, leaf_edges = m_comp_infos[0]
                _rewire_single_merge_tree(
                    leaf_edges,
                    collections.deque(avail_pairs),
                    root_pair,
                    nodes,
                    blk_to_seq_idx,
                    rewire_map,
                    assign_fn,
                )
                continue
            avail_m_q = collections.deque(
                sorted(
                    (pair for item in m_comp_infos for pair in item[2]),
                    key=lambda p: _natural_key(p[0]),
                )
            )
            root_m_pairs = [item[1] for item in m_comp_infos]
            all_m_leaves = sorted(
                (e for item in m_comp_infos for e in item[-1]),
                key=lambda e: (
                    blk_to_seq_idx.get(nodes[e.source].assigned_block, 0),
                    nodes[e.source].rank,
                    nodes[e.source].flow_depth,
                    _natural_key(e.source),
                    _natural_key(e.wire),
                ),
            )
            total_m = len(all_m_leaves)
            m_blk_bounds = [
                idx
                for idx in range(1, total_m)
                if nodes[all_m_leaves[idx].source].assigned_block
                != nodes[all_m_leaves[idx - 1].source].assigned_block
            ]
            m_cuts = _compute_multibus_merger_cuts(
                all_m_leaves, m_blk_bounds, k_m, s_slice_demands.get(r_key), self._driver_wire_rates
            )
            for t_idx in range(k_m):
                sub_leaves = all_m_leaves[m_cuts[t_idx] : m_cuts[t_idx + 1]]
                _rewire_single_merge_tree(
                    sub_leaves,
                    avail_m_q,
                    root_m_pairs[t_idx],
                    nodes,
                    blk_to_seq_idx,
                    rewire_map,
                    assign_fn,
                )

    def _rewire_trees_for_blocks(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        blk_to_seq_idx: dict[tuple[int, int], int],
        assigned: set[str],
        assign_fn: BlockAssignFn,
    ) -> dict[str, dict[str, str]]:
        """Re-parenthesize merger and splitter trees so each block has self-contained local trees."""
        rewire_map: dict[str, dict[str, str]] = {}
        in_edges: dict[str, list[PlacerEdge]] = collections.defaultdict(list)
        out_edges: dict[str, list[PlacerEdge]] = collections.defaultdict(list)
        for e in edges:
            if e.source != e.sink:
                out_edges[e.source].append(e)
                in_edges[e.sink].append(e)

        valid_m_list, valid_m_by_recipe, valid_s_by_recipe = _collect_valid_tree_components(
            nodes, edges, in_edges, out_edges, assigned
        )
        s_slice_demands, s_precomputed = self._precompute_splitter_slices(
            nodes, blk_to_seq_idx, valid_s_by_recipe, valid_m_by_recipe
        )

        if any(len(v) > 1 for v in valid_m_by_recipe.values()):
            self._rewire_multibus_merges(
                nodes, blk_to_seq_idx, valid_m_by_recipe, s_slice_demands, rewire_map, assign_fn
            )
        else:
            for _r_key, _r_wire, root_pair, avail_pairs, leaf_edges in valid_m_list:
                _rewire_single_merge_tree(
                    leaf_edges,
                    collections.deque(avail_pairs),
                    root_pair,
                    nodes,
                    blk_to_seq_idx,
                    rewire_map,
                    assign_fn,
                )

        for _r_key, (root_wires, avail_splits, avail_int_w, all_leaves, cuts) in sorted(
            s_precomputed.items()
        ):
            avail_s_q = collections.deque(avail_splits)
            avail_w_q = collections.deque(avail_int_w)
            for t_idx, r_wire in enumerate(root_wires):
                b_leaves = all_leaves[cuts[t_idx] : cuts[t_idx + 1]]
                _rewire_one_split_slice(
                    r_wire, b_leaves, avail_s_q, avail_w_q, nodes, rewire_map, assign_fn
                )

        for nd in nodes.values():
            if nd.node_type == "balancer":
                nd.is_matrix_node = True

        _apply_rewire_map_to_graph(nodes, edges, rewire_map)
        return rewire_map

    def _solve_continuous_block(
        self,
        block_nodes: list[PlacerNode],
        edges: list[PlacerEdge],
        x_lo: float,
        y_lo: float,
        x_hi: float,
        y_hi: float,
    ) -> None:
        """Solve the directed quadratic system for continuous (cont_x, cont_y) inside one core."""
        if not block_nodes:
            return
        name_to_idx = {node.name: idx for idx, node in enumerate(block_nodes)}
        usable_w = max(_MIN_CONT_USABLE_DIM, x_hi - x_lo)
        usable_h = max(_MIN_CONT_USABLE_DIM, y_hi - y_lo)
        sol_x = _solve_continuous_x(block_nodes, edges, name_to_idx, x_lo, x_hi, usable_w)
        sol_y = _solve_continuous_y(block_nodes, edges, name_to_idx, y_lo, y_hi, usable_h)
        for i, node in enumerate(block_nodes):
            node.cont_x = float(np.clip(sol_x[i], x_lo, x_hi - node.width))
            node.cont_y = float(np.clip(sol_y[i], y_lo, y_hi - node.height))

    def _legalize_block(
        self,
        block_nodes: list[PlacerNode],
        edges: list[PlacerEdge],
        core_min_x: int,
        core_min_y: int,
        core_max_x: int,
        core_max_y: int,
        fixed_core: bool = True,
    ) -> tuple[int, int]:
        """Legalize nodes inside [core_min_x, core_max_x] x [core_min_y, core_max_y]."""
        if not block_nodes:
            return core_max_x - core_min_x, core_max_y - core_min_y

        margin_x = _LEGALIZE_MARGIN_X
        margin_y = _PAD_X_DEFAULT
        x_lo = core_min_x + margin_x
        y_lo = core_min_y + margin_y
        x_hi = core_max_x - margin_x
        y_hi = core_max_y - margin_y
        usable_h = max(_MIN_USABLE_H, y_hi - y_lo)

        raw_cols: dict[int, list[PlacerNode]] = collections.defaultdict(list)
        for node in block_nodes:
            raw_cols[node.col_index].append(node)

        expanded_cols, paired_power_cols = _expand_block_columns(
            raw_cols, edges, usable_h, fixed_core
        )

        if fixed_core:
            avail_w = max(10, x_hi - x_lo)
            fold_trees = getattr(self, "_active_grid_w", 1) == _SEVEN_GRID_W
            _fold_columns_to_fit_width(
                expanded_cols,
                block_nodes,
                edges,
                avail_w,
                usable_h,
                paired_power_cols,
                fold_trees,
            )

        used_w = self._legalize_column_band(
            expanded_cols,
            edges,
            x_lo,
            x_hi,
            y_lo,
            y_hi,
            fixed_x=fixed_core,
            snap_even_base=core_min_y,
        )
        return used_w + 2 * margin_x, core_max_y - core_min_y

    def _legalize_column_band(
        self,
        cols: list[list[PlacerNode]],
        edges: list[PlacerEdge],
        x_lo: int,
        x_hi: int,
        y_lo: int,
        y_hi: int,
        fixed_x: bool = True,
        snap_even_base: int | None = None,
    ) -> int:
        """Assign integer X and Y coordinates to a sequence of columns in one band."""
        if not cols:
            return 0

        col_widths = [max(nd.width for nd in col) for col in cols]
        avail_w = max(10, x_hi - x_lo)
        actual_gaps = _compute_band_column_gaps(cols, col_widths, avail_w, fixed_x)
        used_width = _assign_band_column_x_coords(
            cols, col_widths, actual_gaps, x_lo, x_hi, fixed_x
        )

        node_map = {nd.name: nd for col in cols for nd in col}
        sinks_of: dict[str, list[tuple[str, float, float]]] = collections.defaultdict(list)
        drivers_of: dict[str, list[tuple[str, float, float]]] = collections.defaultdict(list)
        for e in edges:
            if e.source in node_map and e.sink in node_map and e.source != e.sink:
                sinks_of[e.source].append((e.sink, e.source_dy, e.sink_dy))
                drivers_of[e.sink].append((e.source, e.source_dy, e.sink_dy))

        band_h = max(_MIN_BAND_H, y_hi - y_lo)
        for c, col in enumerate(cols):
            _initial_legalize_col_y(col, c, y_lo, y_hi, band_h, snap_even_base)

        _refine_band_y_alignment(
            cols, node_map, sinks_of, drivers_of, y_lo, y_hi, band_h, snap_even_base
        )
        return used_width

    def place_netlist(
        self,
        nl: NetlistLike,
        merge_plans: MergePlansMap | None = None,
        split_plans: SplitPlansMap | None = None,
        balancer_plans: BalancerPlansMap | None = None,
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = _DEFAULT_CHASSIS_CORE_DIM,
        chassis_core_h: int = _DEFAULT_CHASSIS_CORE_DIM,
        distribute_across_blocks: bool = False,
    ) -> AnalyticalPlacementResult:
        """Run analytical placement on a synthesized Netlist object."""
        self._active_grid_w = max(1, grid_w) if use_chassis else 1
        nodes, edges, _ins, _outs = self.build_graph_from_netlist(
            nl, merge_plans, split_plans, balancer_plans
        )
        return self._place_graph(
            nodes,
            edges,
            use_chassis=use_chassis,
            grid_w=grid_w,
            grid_h=grid_h,
            chassis_pitch=chassis_pitch,
            chassis_core_w=chassis_core_w,
            chassis_core_h=chassis_core_h,
            distribute_across_blocks=distribute_across_blocks,
        )

    def place_data(
        self,
        netlist_data: JsonMap,
        def_data: JsonMap | None = None,
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = _DEFAULT_CHASSIS_CORE_DIM,
        chassis_core_h: int = _DEFAULT_CHASSIS_CORE_DIM,
        distribute_across_blocks: bool = False,
    ) -> AnalyticalPlacementResult:
        """Run analytical placement on netlist_data and optional def_data dictionaries."""
        self._active_grid_w = max(1, grid_w) if use_chassis else 1
        nodes, edges, _ins, _outs = self.build_graph_from_data(netlist_data, def_data)
        return self._place_graph(
            nodes,
            edges,
            use_chassis=use_chassis,
            grid_w=grid_w,
            grid_h=grid_h,
            chassis_pitch=chassis_pitch,
            chassis_core_w=chassis_core_w,
            chassis_core_h=chassis_core_h,
            distribute_across_blocks=distribute_across_blocks,
        )

    def _place_single_chassis_block(
        self,
        bx: int,
        by: int,
        inst_names: list[str],
        blk_ctx: tuple[
            dict[str, PlacerNode],
            list[PlacerEdge],
            int,
            int,
            int,
            int,
            int,
            int,
            int,
        ],
    ) -> None:
        """Place routing matrix and core nodes inside one chassis block."""
        (
            nodes,
            edges,
            gw,
            gh,
            margin_x,
            margin_y,
            chassis_pitch,
            chassis_core_w,
            chassis_core_h,
        ) = blk_ctx
        mat_nodes, core_nodes = _split_and_remap_block_nodes(nodes, inst_names)
        _place_block_matrix_nodes(mat_nodes, bx, by, chassis_pitch)
        if not core_nodes:
            return
        extra_bay_y = (
            max(0, (len(mat_nodes) - 1) // _BAY_DENSE_COLS) * _BAY_ROW_Y_STEP
            if len(mat_nodes) > _BAY_CAP
            else 0
        )
        c_min_x = bx * chassis_pitch + margin_x
        c_min_y = (
            by * chassis_pitch + margin_y + (_BAY_CLEARANCE_Y if gw * gh > 1 else 0) + extra_bay_y
        )
        c_max_x = bx * chassis_pitch + margin_x + chassis_core_w
        c_max_y = by * chassis_pitch + margin_y + chassis_core_h
        self._solve_continuous_block(
            core_nodes,
            edges,
            float(c_min_x + _CORE_INSET),
            float(c_min_y + _CORE_INSET),
            float(c_max_x - _CORE_INSET),
            float(c_max_y - _CORE_INSET),
        )
        self._legalize_block(
            core_nodes,
            edges,
            c_min_x,
            c_min_y,
            c_max_x,
            c_max_y,
            fixed_core=True,
        )

    def _place_single_core_adaptive(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
    ) -> tuple[int, int, tuple[int, int, int, int]]:
        """Execute single-core adaptive die sizing and placement."""
        self._last_rewired_ports = {}
        all_nodes = list(nodes.values())
        for nd in all_nodes:
            nd.assigned_block = (0, 0)
            nd.is_matrix_node = False

        padded_area = sum(
            (nd.width + nd.pad_w + nd.pad_e) * (nd.height + nd.pad_n + nd.pad_s) for nd in all_nodes
        )
        by_col_h: dict[int, int] = collections.defaultdict(int)
        for nd in all_nodes:
            v_gap = (
                max(_MACH_V_GAP, nd.pad_n + nd.pad_s)
                if nd.node_type == "machine"
                else max(_PAD_X_DEFAULT, nd.pad_n + nd.pad_s)
                if nd.node_type in ("split", "merge", "balancer")
                else 2
            )
            by_col_h[nd.col_index] += nd.height + v_gap
        max_col_h = max(by_col_h.values(), default=_MIN_SINGLE_CORE_H)
        target_h = max(
            _MIN_SINGLE_CORE_H,
            max_col_h + _SINGLE_CORE_H_PAD,
            min(
                _MAX_SINGLE_CORE_H,
                round(math.sqrt(max(_MIN_PADDED_AREA_SQ, padded_area * _AREA_H_SCALE))),
            ),
        )
        if target_h % 2 != 0:
            target_h += 1

        c_min_x = self.core_margin
        c_min_y = self.core_margin
        c_max_y = c_min_y + target_h
        est_w = max(
            _MIN_SINGLE_CORE_W,
            round(
                padded_area
                * _AREA_W_SCALE
                / float(max(_MIN_SINGLE_CORE_H, min(_MAX_SINGLE_CORE_H, target_h)))
            ),
        )

        self._solve_continuous_block(
            all_nodes,
            edges,
            float(c_min_x + _CORE_INSET),
            float(c_min_y + _CORE_INSET),
            float(c_min_x + est_w),
            float(c_max_y - _CORE_INSET),
        )
        used_w, _used_h = self._legalize_block(
            all_nodes,
            edges,
            c_min_x,
            c_min_y,
            c_min_x + max(est_w, _MAX_UNBOUNDED_CORE_W),
            c_max_y,
            fixed_core=False,
        )

        max_placed_x = max(
            (nd.x + nd.width for nd in all_nodes), default=c_min_x + _FALLBACK_PLACED_EXTENT
        )
        max_placed_y = max(
            (nd.y + nd.height for nd in all_nodes), default=c_min_y + _FALLBACK_PLACED_EXTENT
        )
        core_hi_x = max(c_min_x + used_w, max_placed_x + _CORE_MARGIN_DEFAULT)
        core_hi_y = max(c_max_y, max_placed_y + _CORE_MARGIN_DEFAULT)
        if (core_hi_x - c_min_x) % 2 != 0:
            core_hi_x += 1
        if (core_hi_y - c_min_y) % 2 != 0:
            core_hi_y += 1

        die_w = core_hi_x + self.core_margin
        die_h = core_hi_y + self.core_margin
        core_bounds = (c_min_x, c_min_y, core_hi_x, core_hi_y)
        return die_w, die_h, core_bounds

    def _place_graph(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = _DEFAULT_CHASSIS_CORE_DIM,
        chassis_core_h: int = _DEFAULT_CHASSIS_CORE_DIM,
        distribute_across_blocks: bool = False,
    ) -> AnalyticalPlacementResult:
        """Execute the 4-stage analytical placement and directed legalization pipeline."""
        self._active_grid_w = max(1, grid_w) if use_chassis else 1
        self._compute_flow_depths_and_columns(nodes, edges, use_chassis=use_chassis)

        if use_chassis:
            gw = max(1, grid_w)
            gh = max(1, grid_h)
            margin_x = (chassis_pitch - chassis_core_w) // 2
            margin_y = (chassis_pitch - chassis_core_h) // 2
            die_w = gw * chassis_pitch
            die_h = gh * chassis_pitch
            core_bounds = (margin_x, margin_y, die_w - margin_x, die_h - margin_y)

            block_bins = self._partition_into_blocks(
                nodes,
                grid_w=gw,
                grid_h=gh,
                core_w=chassis_core_w,
                core_h=chassis_core_h,
                distribute_across_blocks=distribute_across_blocks,
                edges=edges,
            )

            if getattr(self, "_last_rewired_ports", None):
                self._compute_flow_depths_and_columns(
                    nodes, edges, use_chassis=(use_chassis and gw != _SEVEN_GRID_W)
                )

            for (bx, by), inst_names in block_bins.items():
                blk_ctx = (
                    nodes,
                    edges,
                    gw,
                    gh,
                    margin_x,
                    margin_y,
                    chassis_pitch,
                    chassis_core_w,
                    chassis_core_h,
                )
                self._place_single_chassis_block(bx, by, inst_names, blk_ctx)
        else:
            die_w, die_h, core_bounds = self._place_single_core_adaptive(nodes, edges)
            gw, gh = 1, 1

        coords = {name: (nd.x, nd.y) for name, nd in nodes.items()}
        cont_coords = {
            name: (round(nd.cont_x, 2), round(nd.cont_y, 2)) for name, nd in nodes.items()
        }
        blk_map = {name: nd.assigned_block for name, nd in nodes.items()}
        mat_set = {name for name, nd in nodes.items() if nd.is_matrix_node}
        rewired = dict(getattr(self, "_last_rewired_ports", {}))
        pin_coords = self._compute_boundary_pin_coords(
            nodes,
            edges,
            die_w=die_w,
            die_h=die_h,
            use_chassis=use_chassis,
            grid_h=gh,
            chassis_pitch=chassis_pitch,
        )
        hpwl = self._compute_graph_hpwl(nodes, edges, die_w)

        return AnalyticalPlacementResult(
            coords=coords,
            continuous_coords=cont_coords,
            block_assignments=blk_map,
            die_width=die_w,
            die_height=die_h,
            core_bounds=core_bounds,
            grid_w=gw,
            grid_h=gh,
            hpwl=hpwl,
            rewired_ports=rewired,
            matrix_nodes=mat_set,
            pin_coords=pin_coords,
        )

    def _compute_boundary_pin_coords(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        die_w: int,
        die_h: int,
        use_chassis: bool = False,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        dbu: int = _DEFAULT_DBU,
    ) -> dict[str, tuple[int, int]]:
        """Compute PAV-spread boundary pin DBU coordinates aligned with connected cells."""
        in_targets, out_targets, in_sink_nodes = _collect_boundary_pin_targets(nodes, edges)
        pin_coords: dict[str, tuple[int, int]] = {}
        east_x_dbu = int(die_w * dbu - _PIN_EDGE_INSET_DBU)
        for target_map, x_dbu in ((in_targets, _PIN_EDGE_INSET_DBU), (out_targets, east_x_dbu)):
            if not target_map:
                continue
            items, gaps = _build_sorted_boundary_pin_items(target_map, die_h)
            y_vals = _solve_1d_pav(
                [item[0] for item in items],
                heights=[1] * len(items),
                gaps=gaps,
                y_lo=_PIN_Y_MARGIN,
                y_hi=max(_BAY_CLEARANCE_Y, die_h - _PIN_Y_MARGIN),
            )
            if use_chassis and grid_h > 1:
                _nudge_pins_from_chassis_boundaries(items, y_vals, gaps, grid_h, chassis_pitch)
                if target_map is in_targets:
                    _relocate_west_core_input_pins(
                        items, y_vals, in_sink_nodes, grid_h, chassis_pitch
                    )
            for item, yv in zip(items, y_vals, strict=False):
                pin_coords[item[-1]] = (x_dbu, int(yv) * dbu + _PIN_HALF_TILE_DBU)
        return pin_coords

    def _compute_graph_hpwl(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        die_w: int,
    ) -> float:
        """Compute total Half-Perimeter Wirelength across all nets in the graph."""
        by_wire = _collect_wire_endpoints(nodes, edges, die_w)
        total = 0.0
        for pts in by_wire.values():
            if len(pts) >= 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                total += (max(xs) - min(xs)) + (max(ys) - min(ys))
        return round(total, 2)

    def emit_analytical_tcl(
        self,
        nl: NetlistLike,
        merge_plans: MergePlansMap | None = None,
        split_plans: SplitPlansMap | None = None,
        balancer_plans: BalancerPlansMap | None = None,
        extra_chassis_grids: list[tuple[int, int]] | None = None,
    ) -> str:
        """Emit TCL dictionaries with analytical coordinates for single-core and chassis modes."""
        if extra_chassis_grids:
            grids = list(dict.fromkeys(extra_chassis_grids))
        else:
            grids = [(1, 1), (2, 2), (_CENTER_HOLE_GRID, _CENTER_HOLE_GRID)]

        chassis_results: dict[tuple[int, int], AnalyticalPlacementResult] = {}
        has_labs = any(c.get("machine") == "lab" for c in getattr(nl, "cells", {}).values())
        if extra_chassis_grids and (len(getattr(nl, "outputs", [])) >= _MERGE_ORDER or has_labs):
            gw0, gh0 = grids[0]
            first_ch = self.place_netlist(
                nl,
                merge_plans=merge_plans,
                split_plans=split_plans,
                balancer_plans=balancer_plans,
                use_chassis=True,
                grid_w=gw0,
                grid_h=gh0,
            )
            chassis_results[(gw0, gh0)] = first_ch
            single_res = first_ch
        else:
            single_res = self.place_netlist(
                nl,
                merge_plans=merge_plans,
                split_plans=split_plans,
                balancer_plans=balancer_plans,
                use_chassis=False,
            )

        c_lo_x, c_lo_y, c_hi_x, c_hi_y = single_res.core_bounds
        lines = [
            "",
            "# Analytical Placement with Directed Legalization",
            f"set analytical_die_w {single_res.die_width}",
            f"set analytical_die_h {single_res.die_height}",
            f"set analytical_core_lo_x {c_lo_x}",
            f"set analytical_core_lo_y {c_lo_y}",
            f"set analytical_core_hi_x {c_hi_x}",
            f"set analytical_core_hi_y {c_hi_y}",
            "set analytical_coords [dict create]",
        ]
        for name in sorted(single_res.coords.keys(), key=_natural_key):
            ix, iy = single_res.coords[name]
            lines.append(f"dict set analytical_coords {name} [list {ix} {iy}]")
        if single_res.pin_coords:
            lines.append("set analytical_pins [dict create]")
            for pname in sorted(single_res.pin_coords.keys(), key=_natural_key):
                px, py = single_res.pin_coords[pname]
                lines.append(f"dict set analytical_pins {pname} [list {px} {py}]")

        for gw, gh in grids:
            ch_res = chassis_results.get((gw, gh))
            if ch_res is None:
                ch_res = self.place_netlist(
                    nl,
                    merge_plans=merge_plans,
                    split_plans=split_plans,
                    balancer_plans=balancer_plans,
                    use_chassis=True,
                    grid_w=gw,
                    grid_h=gh,
                )
            _append_chassis_tcl_lines(lines, gw, gh, ch_res)

        return "\n".join(lines) + "\n"

    def apply_to_def_data(
        self,
        def_data: JsonMap,
        netlist_data: JsonMap,
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = _DEFAULT_CHASSIS_CORE_DIM,
        chassis_core_h: int = _DEFAULT_CHASSIS_CORE_DIM,
        distribute_across_blocks: bool = False,
        untangle_ports: bool = True,
    ) -> tuple[MutableJsonMap, AnalyticalPlacementResult]:
        """Apply analytical placement to a parsed DEF dictionary and untangle splitter ports."""
        updated = dict(copy.deepcopy(def_data))
        dbu = int(updated.get("units", _DEFAULT_DBU))

        res = self.place_data(
            netlist_data,
            def_data=updated,
            use_chassis=use_chassis,
            grid_w=grid_w,
            grid_h=grid_h,
            chassis_pitch=chassis_pitch,
            chassis_core_w=chassis_core_w,
            chassis_core_h=chassis_core_h,
            distribute_across_blocks=distribute_across_blocks,
        )

        if res.rewired_ports:
            apply_port_rewiring_to_def(updated, res.rewired_ports)

        updated["die_area"] = [0, 0, res.die_width * dbu, res.die_height * dbu]
        if use_chassis:
            chassis = CityBlockChassis(
                pitch=chassis_pitch,
                core_width=chassis_core_w,
                core_height=chassis_core_h,
            )
            empty_blks = (
                {((grid_w - 1) // 2, (grid_h - 1) // 2)}
                if (grid_w >= _CENTER_HOLE_GRID and grid_h >= _CENTER_HOLE_GRID)
                else None
            )
            updated["blockages"] = [
                {
                    "x1": x * dbu,
                    "y1": y * dbu,
                    "x2": (x + w) * dbu,
                    "y2": (y + h) * dbu,
                }
                for x, y, w, h in chassis.get_placement_blockages(
                    grid_w, grid_h, empty_blocks=empty_blks
                )
            ]
        else:
            updated["blockages"] = []

        for comp in updated.get("components", []):
            cname = comp["name"]
            if cname in res.coords:
                ix, iy = res.coords[cname]
                comp["x"] = int(ix * dbu)
                comp["y"] = int(iy * dbu)
                comp["orient"] = "N"

        for net_obj in updated.get("nets", []):
            net_obj["wires"] = []
            net_obj["vias"] = []

        self._align_boundary_pins(updated, netlist_data, res.die_width, res.die_height, dbu)

        if untangle_ports:
            untangle_splitter_merger_ports(updated, netlist_data)

        return updated, res

    def _align_boundary_pins(
        self,
        def_data: MutableJsonMap,
        netlist_data: JsonMap,
        die_w: int,
        die_h: int,
        dbu: int,
    ) -> None:
        """Align boundary input and output pins on West and East edges with connected instances."""
        pins = def_data.get("pins", [])
        if not pins:
            return

        comp_map = {c["name"]: c for c in def_data.get("components", [])}
        cells_map = netlist_data.get("cells", {})
        inst_map = {i["name"]: i for i in netlist_data.get("instances", [])}
        net_map = {n["name"]: n for n in def_data.get("nets", [])}

        in_pins = [p for p in pins if p.get("direction") == "INPUT"]
        out_pins = [p for p in pins if p.get("direction") == "OUTPUT"]

        def _connected_y(pin_obj: JsonMap) -> float:
            nname = pin_obj.get("net", pin_obj.get("name", ""))
            net = net_map.get(nname)
            if not net:
                return float(die_h) / 2.0
            ys: list[float] = []
            for iname, _port in net.get("connections", []):
                if iname in comp_map:
                    c = comp_map[iname]
                    macro = inst_map.get(iname, {}).get("cell", c.get("macro", ""))
                    h = float(cells_map.get(macro, {}).get("height", 2))
                    ys.append(float(c["y"]) / float(dbu) + h / 2.0)
            return float(np.mean(ys)) if ys else float(die_h) / 2.0

        for pin_group, x_edge in ((in_pins, 0), (out_pins, die_w)):
            if not pin_group:
                continue
            ideals = [_connected_y(p) for p in pin_group]
            paired = sorted(
                zip(ideals, pin_group, strict=False),
                key=lambda item: (round(item[0], 2), item[1].get("name", "")),
            )
            sorted_ideals = [item[0] for item in paired]
            sorted_pins = [item[1] for item in paired]
            y_vals = _solve_1d_pav(
                sorted_ideals,
                heights=[1] * len(sorted_pins),
                gaps=[_PIN_SAME_SINK_GAP] * max(0, len(sorted_pins) - 1),
                y_lo=_PIN_Y_MARGIN,
                y_hi=max(_MIN_PIN_HI_Y, die_h - _PIN_Y_MARGIN),
            )
            for p_obj, yv in zip(sorted_pins, y_vals, strict=False):
                p_obj["x"] = int(x_edge * dbu)
                p_obj["y"] = int(yv * dbu)


def apply_port_rewiring_to_def(
    def_data: MutableJsonMap,
    rewire_map: dict[str, dict[str, str]],
) -> None:
    """Apply splitter and merger port-to-net rewirings to a parsed DEF dictionary."""
    if not rewire_map:
        return
    rewired_pairs = {(iname, port) for iname, pmap in rewire_map.items() for port in pmap}
    net_map = {n["name"]: n for n in def_data.get("nets", [])}
    for net_obj in def_data.get("nets", []):
        conns = net_obj.get("connections", [])
        net_obj["connections"] = [
            (iname, port) for iname, port in conns if (iname, port) not in rewired_pairs
        ]
    for iname, pmap in rewire_map.items():
        for port, target_net in pmap.items():
            t_net_obj = net_map.get(target_net)
            if t_net_obj is not None and (iname, port) not in t_net_obj["connections"]:
                t_net_obj["connections"].append((iname, port))


def untangle_splitter_merger_ports(
    def_data: MutableJsonMap,
    netlist_data: JsonMap | None = None,
) -> None:
    """Swap symmetric splitter outputs (y1/y2) and merger inputs (a/b) to eliminate crossings."""
    scale = 1.0 / float(def_data.get("units", _DEFAULT_DBU))
    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    pin_map = {p.get("net", p.get("name", "")): p for p in def_data.get("pins", [])}
    cells_map = (netlist_data or {}).get("cells", {})
    inst_map = {i["name"]: i for i in (netlist_data or {}).get("instances", [])}
    nets = def_data.get("nets", [])

    for _pass in range(2):
        inst_port_ref = {
            (iname, port): (net_obj, c_idx)
            for net_obj in nets
            for c_idx, (iname, port) in enumerate(net_obj.get("connections", []))
        }
        for cname, comp in comp_map.items():
            _untangle_single_component_ports(
                cname, comp, inst_port_ref, scale, comp_map, inst_map, cells_map, pin_map
            )


def compute_def_hpwl(
    def_data: JsonMap,
    netlist_data: JsonMap | None = None,
) -> float:
    """Compute total Half-Perimeter Wirelength (HPWL) in tiles for a placed DEF dictionary."""
    scale = 1.0 / float(def_data.get("units", _DEFAULT_DBU))
    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    pin_map = {p.get("net", p.get("name", "")): p for p in def_data.get("pins", [])}
    cells_map = (netlist_data or {}).get("cells", {})
    inst_map = {i["name"]: i for i in (netlist_data or {}).get("instances", [])}

    total_hpwl = 0.0
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        pts = [
            pt
            for iname, port in net.get("connections", [])
            if (
                pt := _def_connection_point(
                    iname, port, nname, scale, comp_map, inst_map, cells_map, pin_map
                )
            )
            is not None
        ]
        if len(pts) >= 2:
            xs = [pt[0] for pt in pts]
            ys = [pt[1] for pt in pts]
            total_hpwl += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return round(total_hpwl, 2)
