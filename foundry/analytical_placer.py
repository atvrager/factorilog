"""Analytical placement with router-aware directed legalization.

Computes continuous 2D coordinates by solving a directed quadratic wirelength
minimization system with density spreading, then legalizes cells onto the
discrete Factorio tile grid with clearance halos for the 3D maze router.
"""

from __future__ import annotations

import collections
import copy
import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

try:
    from foundry.chassis import CityBlockChassis
    from foundry.entity_helpers import entity_size
except ImportError:
    from chassis import CityBlockChassis
    from entity_helpers import entity_size


def _sanitize(name: str) -> str:
    """Convert item or machine name into a valid Verilog identifier."""
    return name.replace("-", "_").replace(".", "__")


def _natural_key(text: str) -> list[int | str]:
    """Return a sort key that orders embedded integers numerically."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", text)]


@dataclass
class PlacerNode:
    """One placeable cell instance in the analytical placement graph."""

    name: str
    macro: str
    recipe: str
    width: int
    height: int
    rank: int = 0
    node_type: str = "machine"
    pad_w: int = 4
    pad_e: int = 4
    pad_n: int = 3
    pad_s: int = 3
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


@dataclass
class PlacerEdge:
    """One directed connection between two nodes or boundary anchors."""

    source: str
    sink: str
    wire: str
    weight: float = 1.0
    source_dy: float = 0.0
    sink_dy: float = 0.0
    is_fluid: bool = False


@dataclass
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


def _classify_node_type(name: str, macro: str, cell_meta: dict[str, Any] | None = None) -> str:
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
    if (
        "combinator" in m_low
        or "power_switch" in m_low
        or (cell_meta and cell_meta.get("circuit_cell"))
        or n_low.startswith(("u_acc_", "u_comb_", "u_dec_", "u_latch_", "u_switch_", "u_params_"))
    ):
        return "combinator"
    return "machine"


def _solve_1d_pav(
    ideal_y: list[float],
    heights: list[int],
    gaps: list[int],
    y_lo: int,
    y_hi: int,
    weights: list[float] | None = None,
    snap_even_base: int | None = None,
) -> list[int]:
    """Solve 1D non-overlapping placement with the Pool-Adjacent-Violators algorithm.

    Minimizes sum_i w_i * (y_i - ideal_y_i)^2 subject to:
      y_{i+1} >= y_i + heights[i] + gaps[i]
      y_lo <= y_0
      y_{n-1} + heights[n-1] <= y_hi
    """
    n = len(ideal_y)
    if n == 0:
        return []
    if weights is None:
        weights = [1.0] * n

    avail = max(1, y_hi - y_lo)
    total_h = sum(heights)
    eff_gaps = list(gaps[: max(0, n - 1)])
    total_g = sum(eff_gaps)

    if total_h + total_g > avail and total_g > 0:
        spare = max(0, avail - total_h)
        scale = float(spare) / float(total_g)
        eff_gaps = [max(1, math.floor(g * scale)) for g in eff_gaps]
        while total_h + sum(eff_gaps) > avail and any(g > 1 for g in eff_gaps):
            for idx in range(len(eff_gaps)):
                if eff_gaps[idx] > 1 and total_h + sum(eff_gaps) > avail:
                    eff_gaps[idx] -= 1
        if total_h + sum(eff_gaps) > avail:
            eff_gaps = [0] * len(eff_gaps)

    offsets = [0] * n
    acc = 0
    for i in range(n - 1):
        acc += heights[i] + eff_gaps[i]
        offsets[i + 1] = acc

    total_span = acc + heights[-1]
    z_max = float(max(y_lo, y_hi - total_span))
    z_min = float(y_lo)

    z_target = [float(ideal_y[i]) - float(offsets[i]) for i in range(n)]

    # Pool-Adjacent-Violators (PAV) blocks: [weight_sum, weighted_z_sum, start_idx, end_idx]
    blocks: list[list[float | int]] = []
    for i in range(n):
        w = max(1e-6, float(weights[i]))
        blocks.append([w, w * z_target[i], i, i])
        while len(blocks) >= 2:
            b_last = blocks[-1]
            b_prev = blocks[-2]
            mean_last = float(b_last[1]) / float(b_last[0])
            mean_prev = float(b_prev[1]) / float(b_prev[0])
            if mean_prev > mean_last:
                b_prev[0] = float(b_prev[0]) + float(b_last[0])
                b_prev[1] = float(b_prev[1]) + float(b_last[1])
                b_prev[3] = int(b_last[3])
                blocks.pop()
            else:
                break

    z_opt = [0.0] * n
    for w_sum, wz_sum, s_idx, e_idx in blocks:
        val = min(z_max, max(z_min, float(wz_sum) / float(w_sum)))
        for idx in range(int(s_idx), int(e_idx) + 1):
            z_opt[idx] = val

    y_int = [round(z_opt[i] + offsets[i]) for i in range(n)]

    if snap_even_base is not None and total_span + 2 * n <= avail:
        for i in range(n):
            if (y_int[i] - snap_even_base) % 2 != 0:
                if y_int[i] + 1 + heights[i] <= y_hi:
                    y_int[i] += 1
                elif y_int[i] - 1 >= y_lo:
                    y_int[i] -= 1

    # Forward sweep to enforce lower bound and minimum gaps
    y_int[0] = max(y_lo, y_int[0])
    for i in range(1, n):
        min_allowed = y_int[i - 1] + heights[i - 1] + eff_gaps[i - 1]
        if y_int[i] < min_allowed:
            y_int[i] = min_allowed

    # Backward sweep to enforce upper bound
    if y_int[-1] + heights[-1] > y_hi:
        y_int[-1] = max(y_lo, y_hi - heights[-1])
        for i in range(n - 2, -1, -1):
            max_allowed = y_int[i + 1] - heights[i] - eff_gaps[i]
            if y_int[i] > max_allowed:
                y_int[i] = max(y_lo, max_allowed)

    # Final forward clamp if the column saturates the vertical span
    y_int[0] = max(y_lo, y_int[0])
    for i in range(1, n):
        min_req = y_int[i - 1] + heights[i - 1] + (1 if eff_gaps[i - 1] > 0 else 0)
        if y_int[i] < min_req and min_req + heights[i] <= y_hi:
            y_int[i] = min_req

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
    if not blocked and grid_w == 3 and grid_h == 3:
        blocked.add(((grid_w - 1) // 2, (grid_h - 1) // 2))

    active = [b for b in cells if b not in blocked]
    return active if active else [(0, 0)]


class AnalyticalPlacer:
    """Analytical global placer and router-aware directed legalizer."""

    def __init__(
        self,
        cell_pad_x: int = 4,
        cell_pad_y: int = 3,
        split_pad_x: int = 6,
        split_pad_y: int = 2,
        core_margin: int = 8,
        max_core_utilization: float = 0.50,
    ) -> None:
        self.cell_pad_x = max(4, cell_pad_x)
        self.cell_pad_y = max(2, cell_pad_y)
        self.split_pad_x = max(5, split_pad_x)
        self.split_pad_y = max(2, split_pad_y)
        self.core_margin = max(4, core_margin)
        self.max_core_utilization = max(0.15, min(0.85, max_core_utilization))

    def _make_node(
        self,
        name: str,
        macro: str,
        recipe: str,
        rank: int,
        cell_meta: dict[str, Any] | None = None,
    ) -> PlacerNode:
        """Create a PlacerNode with footprint and clearance halos."""
        if cell_meta:
            w = int(cell_meta.get("width", 0))
            h = int(cell_meta.get("height", 0))
        else:
            w, h = 0, 0
        if w <= 0 or h <= 0:
            if macro == "belt_merge2":
                ew, eh = 3.0, 3.0
            elif macro in ("belt_split2", "belt_balancer2"):
                ew, eh = 1.0, 2.0
            else:
                ew, eh = entity_size(macro)
            w = int(ew) if w <= 0 else w
            h = int(eh) if h <= 0 else h

        ntype = _classify_node_type(name, macro, cell_meta)
        if ntype in ("split", "merge", "balancer"):
            pad_w = self.split_pad_x
            pad_e = self.split_pad_x
            pad_n = self.split_pad_y
            pad_s = self.split_pad_y
        elif ntype in ("display", "combinator"):
            pad_w = 3
            pad_e = 3
            pad_n = 1
            pad_s = 1
        else:
            pad_w = self.cell_pad_x
            pad_e = self.cell_pad_x
            pad_n = self.cell_pad_y
            pad_s = self.cell_pad_y

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

    def build_graph_from_netlist(
        self,
        nl: Any,
        merge_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        split_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None = None,
    ) -> tuple[dict[str, PlacerNode], list[PlacerEdge], list[str], list[str]]:
        """Extract nodes and directed edges from a synthesized Netlist object."""
        nodes: dict[str, PlacerNode] = {}
        for inst in nl.instances:
            cell = nl.cells.get(inst.cell_key)
            if cell is None:
                if inst.cell_key == "belt_merge2":
                    cell = {"width": 3, "height": 3, "inputs": ["a", "b"], "outputs": ["y"]}
                elif inst.cell_key == "belt_split2":
                    cell = {"width": 1, "height": 2, "inputs": ["a"], "outputs": ["y1", "y2"]}
                elif inst.cell_key == "belt_balancer2":
                    cell = {
                        "width": 1,
                        "height": 2,
                        "inputs": ["a1", "a2"],
                        "outputs": ["y1", "y2"],
                    }
                else:
                    cell = {"width": 3, "height": 3, "inputs": [], "outputs": []}
            nodes[inst.name] = self._make_node(
                name=inst.name,
                macro=inst.cell_key,
                recipe=inst.recipe or "",
                rank=inst.rank,
                cell_meta=cell,
            )

        wire_drivers: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
        wire_sinks: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
        fluid_set = {_sanitize(f) for f in getattr(nl, "fluids", set())} | set(
            getattr(nl, "fluids", set())
        )

        inputs_sanitized = [_sanitize(m) for m in getattr(nl, "inputs", [])]
        outputs_sanitized = [_sanitize(m) for m in getattr(nl, "outputs", [])]

        for inp in inputs_sanitized:
            wire_drivers[inp].append((f"PIN_IN:{inp}", 0.0))
        for outp in outputs_sanitized:
            wire_sinks[outp].append((f"PIN_OUT:{outp}", 0.0))

        if merge_plans:
            for plan in merge_plans.values():
                for m_name, a_w, b_w, y_w in plan:
                    if m_name in nodes:
                        nodes[m_name].inputs.extend([a_w, b_w])
                        nodes[m_name].outputs.append(y_w)
                    wire_sinks[a_w].append((m_name, 0.0))
                    wire_sinks[b_w].append((m_name, 1.0))
                    wire_drivers[y_w].append((m_name, 1.0))

        if split_plans:
            for plan in split_plans.values():
                for s_name, in_w, y1_w, y2_w in plan:
                    if s_name in nodes:
                        nodes[s_name].inputs.append(in_w)
                        nodes[s_name].outputs.extend([y1_w, y2_w])
                    wire_sinks[in_w].append((s_name, 0.5))
                    wire_drivers[y1_w].append((s_name, 0.0))
                    wire_drivers[y2_w].append((s_name, 1.0))

        if balancer_plans:
            for b_plan in balancer_plans.values():
                for bal_name, a1_w, a2_w, y1_w, y2_w in b_plan:
                    if bal_name in nodes:
                        nodes[bal_name].inputs.extend([a1_w, a2_w])
                        nodes[bal_name].outputs.extend([y1_w, y2_w])
                    wire_sinks[a1_w].append((bal_name, 0.0))
                    wire_sinks[a2_w].append((bal_name, 1.0))
                    wire_drivers[y1_w].append((bal_name, 0.0))
                    wire_drivers[y2_w].append((bal_name, 1.0))

        for inst in nl.instances:
            node = nodes[inst.name]
            if node.node_type in ("split", "merge", "balancer"):
                continue
            cell = nl.cells.get(inst.cell_key, {})
            if node.node_type == "display":
                mat_w = _sanitize(inst.recipe)
                c_out = f"c_out_{mat_w}"
                w_name = c_out if f"u_acc_{mat_w}" in nodes else mat_w
                node.inputs.append(w_name)
                wire_sinks[w_name].append((inst.name, 0.5))
                continue
            if node.node_type == "combinator" and inst.name.startswith("u_acc_"):
                mat_w = _sanitize(inst.recipe)
                c_out = f"c_out_{mat_w}"
                node.inputs.append(mat_w)
                node.outputs.append(c_out)
                wire_sinks[mat_w].append((inst.name, 0.5))
                wire_drivers[c_out].append((inst.name, 0.5))
                continue

            in_ports = list(cell.get("inputs", []))
            out_ports = list(cell.get("outputs", []))
            h = node.height
            off_in = max(0, (h - len(in_ports)) // 2)
            for idx, p in enumerate(in_ports):
                w_name = (
                    f"{_sanitize(p)}__{inst.name}"
                    if (split_plans and p in split_plans)
                    or (balancer_plans and p in balancer_plans)
                    else _sanitize(p)
                )
                node.inputs.append(w_name)
                wire_sinks[w_name].append((inst.name, float(off_in + idx)))

            off_out = max(0, (h - len(out_ports)) // 2)
            for idx, p in enumerate(out_ports):
                w_name = (
                    f"{_sanitize(p)}__{inst.name}"
                    if (merge_plans and p in merge_plans)
                    else _sanitize(p)
                )
                node.outputs.append(w_name)
                wire_drivers[w_name].append((inst.name, float(off_out + idx)))

        edges: list[PlacerEdge] = []
        for wire, drvs in wire_drivers.items():
            base_mat = wire.split("__")[0]
            is_fl = base_mat in fluid_set
            snks = wire_sinks.get(wire, [])
            scale_w = (1.5 if is_fl else 1.0) / max(1.0, math.sqrt(float(len(drvs))))
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

        return nodes, edges, inputs_sanitized, outputs_sanitized

    def build_graph_from_data(
        self,
        netlist_data: dict[str, Any],
        def_data: dict[str, Any] | None = None,
    ) -> tuple[dict[str, PlacerNode], list[PlacerEdge], list[str], list[str]]:
        """Extract nodes and directed edges from netlist_data and optional def_data."""
        cells_map = netlist_data.get("cells", {})
        inst_list = netlist_data.get("instances", [])
        inst_meta_map = {inst["name"]: inst for inst in inst_list}
        fluid_set = {_sanitize(f) for f in netlist_data.get("fluids", [])} | set(
            netlist_data.get("fluids", [])
        )

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
        else:
            for inst in inst_list:
                cname = inst["name"]
                macro = inst.get("cell", "")
                recipe = inst.get("recipe", "")
                rank = int(inst.get("rank", 0))
                cell_meta = cells_map.get(macro)
                nodes[cname] = self._make_node(cname, macro, recipe, rank, cell_meta)

        inputs_sanitized = [_sanitize(m) for m in netlist_data.get("inputs", [])]
        outputs_sanitized = [_sanitize(m) for m in netlist_data.get("outputs", [])]
        edges: list[PlacerEdge] = []

        if def_data and def_data.get("nets"):
            in_pin_names = {
                p.get("net", p.get("name", ""))
                for p in def_data.get("pins", [])
                if p.get("direction") == "INPUT"
            } | set(inputs_sanitized)
            for net in def_data["nets"]:
                nname = net.get("name", "")
                conns = net.get("connections", [])
                drivers: list[tuple[str, float]] = []
                sinks: list[tuple[str, float]] = []
                for inst_name, port in conns:
                    if inst_name == "PIN":
                        if nname in in_pin_names or port in in_pin_names:
                            drivers.append((f"PIN_IN:{nname}", 0.0))
                        else:
                            sinks.append((f"PIN_OUT:{nname}", 0.0))
                        continue
                    node = nodes.get(inst_name)
                    if node is None:
                        continue
                    if node.node_type in ("split", "balancer"):
                        if port in ("y1", "y"):
                            drivers.append((inst_name, 0.0))
                            node.outputs.append(nname)
                        elif port == "y2":
                            drivers.append((inst_name, 1.0))
                            node.outputs.append(nname)
                        else:
                            dy = 1.0 if port in ("b", "a2") else 0.0
                            sinks.append((inst_name, dy))
                            node.inputs.append(nname)
                    elif node.node_type == "merge":
                        if port == "y":
                            drivers.append((inst_name, 0.5))
                            node.outputs.append(nname)
                        else:
                            dy = 1.0 if port in ("b", "a2") else 0.0
                            sinks.append((inst_name, dy))
                            node.inputs.append(nname)
                    else:
                        cmeta = cells_map.get(node.macro, {})
                        outs = [_sanitize(o) for o in cmeta.get("outputs", [])] + list(
                            cmeta.get("outputs", [])
                        )
                        if port in outs or _sanitize(port) in outs or port in ("y", "out"):
                            drivers.append((inst_name, node.height / 2.0))
                            node.outputs.append(nname)
                        else:
                            idx = 0
                            clean_ins = [_sanitize(i) for i in cmeta.get("inputs", [])]
                            if _sanitize(port) in clean_ins:
                                idx = clean_ins.index(_sanitize(port))
                            off = max(0, (node.height - len(clean_ins)) // 2)
                            sinks.append((inst_name, float(off + idx)))
                            node.inputs.append(nname)

                base_mat = nname.split("__")[0]
                is_fl = base_mat in fluid_set
                for drv, d_dy in drivers:
                    for snk, s_dy in sinks:
                        if drv != snk:
                            edges.append(
                                PlacerEdge(
                                    source=drv,
                                    sink=snk,
                                    wire=nname,
                                    weight=1.5 if is_fl else 1.0,
                                    source_dy=d_dy,
                                    sink_dy=s_dy,
                                    is_fluid=is_fl,
                                )
                            )
        else:
            # Infer connections from cell inputs and outputs when DEF nets are absent
            mat_producers: dict[str, list[str]] = collections.defaultdict(list)
            mat_consumers: dict[str, list[tuple[str, float]]] = collections.defaultdict(list)
            for inp in inputs_sanitized:
                mat_producers[inp].append(f"PIN_IN:{inp}")
            for outp in outputs_sanitized:
                mat_consumers[outp].append((f"PIN_OUT:{outp}", 0.0))

            for cname, node in nodes.items():
                cmeta = cells_map.get(node.macro, {})
                in_list = [_sanitize(i) for i in cmeta.get("inputs", [])]
                out_list = [_sanitize(o) for o in cmeta.get("outputs", [])]
                for idx, inp in enumerate(in_list):
                    node.inputs.append(inp)
                    mat_consumers[inp].append((cname, float(idx)))
                for outp in out_list:
                    node.outputs.append(outp)
                    mat_producers[outp].append(cname)

            for mat, prods in mat_producers.items():
                is_fl = mat in fluid_set
                cons = mat_consumers.get(mat, [])
                w = 1.0 / max(1.0, float(len(prods) * len(cons)) ** 0.5)
                for p in prods:
                    p_dy = nodes[p].height / 2.0 if p in nodes else 0.0
                    for c, c_dy in cons:
                        if p != c:
                            edges.append(
                                PlacerEdge(
                                    source=p,
                                    sink=c,
                                    wire=mat,
                                    weight=w,
                                    source_dy=p_dy,
                                    sink_dy=c_dy,
                                    is_fluid=is_fl,
                                )
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

        adj: dict[str, list[str]] = collections.defaultdict(list)
        rev_adj: dict[str, list[str]] = collections.defaultdict(list)
        in_deg: dict[str, int] = dict.fromkeys(nodes, 0)

        def _has_path(start: str, target: str) -> bool:
            stack = [start]
            seen: set[str] = set()
            while stack:
                curr = stack.pop()
                if curr == target:
                    return True
                for nxt in adj.get(curr, []):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            return False

        # Add physical belt/fluid edges first, then circuit/display edges, skipping only cycle-creating back-edges
        sorted_edges = sorted(
            edges,
            key=lambda edge: (
                1
                if (
                    edge.source in nodes
                    and nodes[edge.source].node_type in ("combinator", "display")
                )
                else 0
            ),
        )
        for e in sorted_edges:
            if e.source in nodes and e.sink in nodes and e.source != e.sink:
                u, v = e.source, e.sink
                if v in adj[u] or _has_path(v, u):
                    continue
                adj[u].append(v)
                rev_adj[v].append(u)
                in_deg[v] += 1

        # Kahn topological sort with rank + type tie-breaking
        type_order = {
            "split": 0,
            "balancer": 0,
            "pump": 1,
            "machine": 2,
            "merge": 3,
            "combinator": 4,
            "display": 5,
        }
        ready = [name for name, d in in_deg.items() if d == 0]
        topo_order: list[str] = []
        visited: set[str] = set()

        while ready:
            ready.sort(
                key=lambda n: (
                    nodes[n].rank,
                    type_order.get(nodes[n].node_type, 2),
                    _natural_key(n),
                )
            )
            u = ready.pop(0)
            if u in visited:
                continue
            visited.add(u)
            topo_order.append(u)
            for v in adj[u]:
                in_deg[v] -= 1
                if in_deg[v] <= 0 and v not in visited:
                    ready.append(v)

        topo_order.extend(
            name
            for name in sorted(
                nodes,
                key=lambda n: (
                    nodes[n].rank,
                    type_order.get(nodes[n].node_type, 2),
                    _natural_key(n),
                ),
            )
            if name not in visited
        )

        # Forward pass: compute ASAP flow depth strictly respecting all DAG edges
        depth: dict[str, int] = {}
        for u in topo_order:
            node = nodes[u]
            preds = rev_adj.get(u, [])
            base_d = node.rank if not edges else 0
            d = max([base_d] + [depth[p] + 1 for p in preds if p in depth])
            depth[u] = d

        # Place telemetry displays and circuit accumulators in the final East column band
        max_non_disp = max(
            (depth[u] for u in nodes if nodes[u].node_type not in ("display", "combinator")),
            default=0,
        )
        for u in topo_order:
            node = nodes[u]
            if node.node_type == "combinator" and u.startswith("u_acc_"):
                preds = [p for p in rev_adj.get(u, []) if p in depth]
                depth[u] = max([max_non_disp + 1] + [depth[p] + 1 for p in preds])
            elif node.node_type == "display":
                preds = [p for p in rev_adj.get(u, []) if p in depth]
                depth[u] = max([max_non_disp + 2] + [depth[p] + 1 for p in preds])

        fluid_nodes = {u for e in edges if e.is_fluid for u in (e.source, e.sink) if u in nodes}
        # Backward tightening pass: pull splitters, balancers, and solid machines East toward sinks
        for u in reversed(topo_order):
            node = nodes[u]
            if node.node_type in ("split", "balancer", "machine") and (
                use_chassis or u not in fluid_nodes
            ):
                succs = [s for s in adj.get(u, []) if s in depth]
                preds = [p for p in rev_adj.get(u, []) if p in depth]
                min_allowed = max([0] + [depth[p] + 1 for p in preds])
                if succs:
                    ideal_pull = min(depth[s] - 1 for s in succs)
                    depth[u] = max(min_allowed, ideal_pull)

        # Compact occupied depth values into contiguous column indices 0 .. C-1
        unique_depths = sorted(set(depth.values()))
        depth_remap = {old_d: new_d for new_d, old_d in enumerate(unique_depths)}
        for u, node in nodes.items():
            c_idx = depth_remap[depth[u]]
            node.flow_depth = c_idx
            node.col_index = c_idx

    def compute_auto_chassis_grid(
        self,
        nl: Any,
        merge_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        split_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None = None,
        core_w: int = 94,
        core_h: int = 94,
    ) -> tuple[int, int]:
        """Compute minimal square city-block grid (W, H) from core area and terminal demand."""
        nodes, _edges, _ins, _outs = self.build_graph_from_netlist(
            nl, merge_plans, split_plans, balancer_plans
        )
        single_core_capacity = float(max(20, core_w - 10) * max(20, core_h - 14))
        total_demand = 0.0
        for nd in nodes.values():
            area = float((nd.width + nd.pad_w + nd.pad_e) * (nd.height + nd.pad_n + nd.pad_s))
            if nd.node_type in ("machine", "pump"):
                total_demand += area + 45.0 * float(len(nd.inputs)) + 40.0 * float(len(nd.outputs))
            else:
                total_demand += area * 0.35
        target_cap_per_block = single_core_capacity * 0.63
        req_blocks = max(1, math.ceil(total_demand / max(1.0, target_cap_per_block)))
        side = max(1, math.ceil(math.sqrt(req_blocks)))
        return side, side

    def _find_tarjan_sccs(
        self,
        machine_names: list[str],
        mach_adj: dict[str, dict[str, float]],
    ) -> list[list[str]]:
        """Find strongly connected components in the machine graph using Tarjan's algorithm."""
        index = 0
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        sccs: list[list[str]] = []

        def _strongconnect(u: str) -> None:
            nonlocal index
            indices[u] = index
            lowlink[u] = index
            index += 1
            stack.append(u)
            on_stack.add(u)

            for v, w in sorted(mach_adj.get(u, {}).items()):
                if w <= 0:
                    continue
                if v not in indices:
                    _strongconnect(v)
                    lowlink[u] = min(lowlink[u], lowlink[v])
                elif v in on_stack:
                    lowlink[u] = min(lowlink[u], indices[v])

            if lowlink[u] == indices[u]:
                scc: list[str] = []
                while True:
                    w_node = stack.pop()
                    on_stack.remove(w_node)
                    scc.append(w_node)
                    if w_node == u:
                        break
                sccs.append(scc)

        for u in machine_names:
            if u not in indices:
                _strongconnect(u)
        sccs.reverse()
        return sccs

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
        """Assign balanced machine buckets to 2D grid blocks via Harmonic-X + KL/FM swap search."""
        num_b = len(active_blocks)
        k_bins = len(dp_buckets)
        buckets: list[list[str]] = list(dp_buckets) + [[] for _ in range(max(0, num_b - k_bins))]

        b_of_mach: dict[str, int] = {}
        for i, b_list in enumerate(buckets):
            for u in b_list:
                b_of_mach[u] = i

        b_adj: list[list[float]] = [[0.0] * num_b for _ in range(num_b)]
        for i, b_list in enumerate(buckets):
            if not b_list:
                continue
            inv_len = 2.4 / float(len(b_list))
            for u in b_list:
                u_edges = mach_adj.get(u, {})
                u_tot = sum(u_edges.values())
                if u_tot <= 0.0:
                    continue
                scale_u = inv_len / u_tot
                for v, w in u_edges.items():
                    j = b_of_mach.get(v, -1)
                    if j >= 0 and j != i:
                        b_adj[i][j] += w * scale_u

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
            else float(max_rank) * 0.5
            for b_list in buckets
        ]
        b_recipes = [{nodes[u].recipe for u in b_list} for b_list in buckets]
        b_outs = [
            set().union(*(reachable_outs.get(u, set()) for u in b_list)) if b_list else set()
            for b_list in buckets
        ]

        b_contig: list[list[float]] = [[0.0] * num_b for _ in range(num_b)]
        for i in range(k_bins - 1):
            if b_recipes[i] & b_recipes[i + 1]:
                b_contig[i][i + 1] = b_contig[i + 1][i] = 1.4
            elif len(b_outs[i]) == 1 and b_outs[i] == b_outs[i + 1]:
                b_contig[i][i + 1] = b_contig[i + 1][i] = 1.0

        # Step 1: 1D Harmonic X-coordinate relaxation
        max_x = float(max(1, grid_w - 1))
        x_val = [
            0.0
            if (b_west[i] > 0 and b_east[i] == 0 and b_avg_rank[i] <= 3.0)
            else max_x
            if b_east[i] > 0
            else (b_avg_rank[i] / float(max(1, max_rank))) * max_x
            if buckets[i]
            else max_x * 0.55
            for i in range(num_b)
        ]
        for _it in range(40):
            for i in range(k_bins):
                num_x = (
                    b_west[i] * 2.2 * 0.0
                    + b_east[i] * 4.5 * max_x
                    + 0.8 * ((b_avg_rank[i] / float(max(1, max_rank))) * max_x)
                )
                den_x = b_west[i] * 2.2 + b_east[i] * 4.5 + 0.8
                for j in range(k_bins):
                    if b_adj[i][j] > 0:
                        num_x += b_adj[i][j] * max(0.0, x_val[j] - 0.6)
                        den_x += b_adj[i][j]
                    if b_adj[j][i] > 0:
                        num_x += b_adj[j][i] * min(max_x, x_val[j] + 0.6)
                        den_x += b_adj[j][i]
                if den_x > 0:
                    x_val[i] = max(0.0, min(max_x, num_x / den_x))

        # Step 2: Initial column-major bottom-to-top assignment (no snake inversion)
        sorted_b = sorted(
            range(num_b),
            key=lambda i: (
                1 if b_east[i] > 0 else 0,
                round(x_val[i], 3),
                -round(b_west[i], 2),
                round(b_avg_rank[i], 2),
                0 if buckets[i] else 1,
                i,
            ),
        )
        grid_blocks_col_order = sorted(active_blocks, key=lambda b: (b[0], b[1]))
        perm: list[tuple[int, int]] = [(0, 0)] * num_b
        for pos_idx, b_idx in enumerate(sorted_b):
            perm[b_idx] = grid_blocks_col_order[pos_idx]

        # Step 3: 2D KL/FM Pairwise Swap Local Search
        def _anchor_cost(i: int, blk: tuple[int, int]) -> float:
            bx, by = blk
            if not buckets[i]:
                edge_pen = 10.0 if bx in (0, grid_w - 1) else 0.0
                return edge_pen + 0.6 * float(by) + 0.4 * abs(float(bx) - max_x * 0.5)
            return (
                b_west[i] * 1.8 * float(bx)
                + b_east[i] * 4.5 * float((grid_w - 1) - bx)
                + 0.45 * abs(float(bx) - x_val[i])
            )

        def _pair_interaction(
            i: int, blk_i: tuple[int, int], j: int, blk_j: tuple[int, int]
        ) -> float:
            bx_i, by_i = blk_i
            bx_j, by_j = blk_j
            cost = 0.0
            w_ij = b_adj[i][j]
            if w_ij > 0:
                dx = abs(bx_j - bx_i)
                back_pen = 2.8 * float(bx_i - bx_j) if bx_i > bx_j else 0.0
                same_col_pen = (
                    0.4 if (bx_i == bx_j and b_avg_rank[i] + 0.5 < b_avg_rank[j]) else 0.0
                )
                dy = 1.45 * abs(by_j - by_i)
                cost += w_ij * (float(dx) + back_pen + same_col_pen + dy)
            w_ji = b_adj[j][i]
            if w_ji > 0:
                dx = abs(bx_i - bx_j)
                back_pen = 2.8 * float(bx_j - bx_i) if bx_j > bx_i else 0.0
                same_col_pen = (
                    0.4 if (bx_i == bx_j and b_avg_rank[j] + 0.5 < b_avg_rank[i]) else 0.0
                )
                dy = 1.45 * abs(by_i - by_j)
                cost += w_ji * (float(dx) + back_pen + same_col_pen + dy)
            w_c = b_contig[i][j]
            if w_c > 0:
                cost += w_c * float(abs(bx_i - bx_j) + abs(by_i - by_j))
            return cost

        def _node_contrib(i: int, blk_i: tuple[int, int], cur_perm: list[tuple[int, int]]) -> float:
            c = _anchor_cost(i, blk_i)
            for k in range(num_b):
                if k != i:
                    c += _pair_interaction(i, blk_i, k, cur_perm[k])
            return c

        for _pass in range(num_b * 3):
            best_delta = -1e-4
            best_pair: tuple[int, int] | None = None
            for i in range(num_b):
                blk_i = perm[i]
                base_i = _node_contrib(i, blk_i, perm)
                for j in range(i + 1, num_b):
                    if not buckets[i] and not buckets[j]:
                        continue
                    blk_j = perm[j]
                    old_c = (
                        base_i
                        + _node_contrib(j, blk_j, perm)
                        - _pair_interaction(i, blk_i, j, blk_j)
                    )
                    perm[i], perm[j] = blk_j, blk_i
                    new_c = (
                        _node_contrib(i, blk_j, perm)
                        + _node_contrib(j, blk_i, perm)
                        - _pair_interaction(i, blk_j, j, blk_i)
                    )
                    perm[i], perm[j] = blk_i, blk_j
                    delta = new_c - old_c
                    if delta < best_delta:
                        best_delta = delta
                        best_pair = (i, j)
            if best_pair is None:
                break
            si, sj = best_pair
            perm[si], perm[sj] = perm[sj], perm[si]

        return perm[:k_bins]

    def _partition_into_blocks(
        self,
        nodes: dict[str, PlacerNode],
        grid_w: int,
        grid_h: int,
        core_w: int = 94,
        core_h: int = 94,
        empty_blocks: set[tuple[int, int]] | None = None,
        distribute_across_blocks: bool = False,
        edges: list[PlacerEdge] | None = None,
    ) -> dict[tuple[int, int], list[str]]:
        """Partition nodes into chassis blocks using domain-coherent phases."""
        active_blocks = _get_chassis_block_sequence(grid_w, grid_h, empty_blocks)
        if len(active_blocks) <= 1 or not nodes:
            blk = active_blocks[0] if active_blocks else (0, 0)
            for node in nodes.values():
                node.assigned_block = blk
            return {blk: list(nodes.keys())}

        def _node_area(nd: PlacerNode) -> float:
            return float((nd.width + nd.pad_w + nd.pad_e) * (nd.height + nd.pad_n + nd.pad_s))

        total_padded_area = sum(_node_area(n) for n in nodes.values())
        single_core_capacity = float(core_w * core_h) * self.max_core_utilization

        if not distribute_across_blocks and total_padded_area <= single_core_capacity:
            blk0 = active_blocks[0]
            for node in nodes.values():
                node.assigned_block = blk0
            return {blk0: list(nodes.keys())}

        num_blocks = len(active_blocks)
        block_bins: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
        block_area: dict[tuple[int, int], float] = dict.fromkeys(active_blocks, 0.0)

        in_drivers: dict[str, list[str]] = collections.defaultdict(list)
        out_sinks: dict[str, list[str]] = collections.defaultdict(list)
        if edges:
            for e in edges:
                if e.source in nodes and e.sink in nodes and e.source != e.sink:
                    in_drivers[e.sink].append(e.source)
                    out_sinks[e.source].append(e.sink)

        machine_names = [name for name, nd in nodes.items() if nd.node_type in ("machine", "pump")]
        if not machine_names or not edges:
            # Fallback for graphs without machine/pump nodes or without edges
            ordered_names = sorted(
                nodes.keys(),
                key=lambda name: (
                    nodes[name].rank,
                    nodes[name].flow_depth,
                    nodes[name].recipe,
                    _natural_key(name),
                ),
            )
            multi_block_cap = single_core_capacity * 0.72
            target_per_block = max(
                1.0,
                total_padded_area / float(num_blocks)
                if (
                    distribute_across_blocks
                    or total_padded_area > multi_block_cap * float(num_blocks)
                )
                else multi_block_cap,
            )
            cur_blk_idx = 0
            cur_area = 0.0
            for u in ordered_names:
                u_area = _node_area(nodes[u])
                if (
                    cur_area > 0
                    and cur_area + u_area > target_per_block
                    and cur_blk_idx < num_blocks - 1
                ):
                    cur_blk_idx += 1
                    cur_area = 0.0
                blk = active_blocks[cur_blk_idx]
                nodes[u].assigned_block = blk
                block_bins[blk].append(u)
                cur_area += u_area
            return dict(block_bins)

        # Phase 1: Partition production machines and pumps by cycle-aware DAG domain
        fluid_machines: set[str] = set()
        fwd_adj: dict[str, list[str]] = collections.defaultdict(list)
        rev_adj: dict[str, list[str]] = collections.defaultdict(list)
        if edges:
            for e in edges:
                fwd_adj[e.source].append(e.sink)
                rev_adj[e.sink].append(e.source)
                if e.is_fluid:
                    if e.source in nodes and nodes[e.source].node_type in ("machine", "pump"):
                        fluid_machines.add(e.source)
                    if e.sink in nodes and nodes[e.sink].node_type in ("machine", "pump"):
                        fluid_machines.add(e.sink)

        machine_set = set(machine_names)
        mach_adj: dict[str, dict[str, float]] = collections.defaultdict(
            lambda: collections.defaultdict(float)
        )
        west_in_weight: dict[str, float] = collections.defaultdict(float)
        east_out_weight: dict[str, float] = collections.defaultdict(float)

        for u in machine_names:
            down_machs: list[str] = []
            seen_m: set[str] = {u}
            stack_m = list(fwd_adj.get(u, []))
            while stack_m:
                cur = stack_m.pop()
                if cur in seen_m:
                    continue
                seen_m.add(cur)
                if cur.startswith("PIN_OUT:"):
                    east_out_weight[u] += 2.5
                elif cur in machine_set:
                    down_machs.append(cur)
                elif cur in nodes and nodes[cur].node_type in ("split", "merge", "balancer"):
                    stack_m.extend(fwd_adj.get(cur, []))
            if down_machs:
                wt = 1.0 / max(1.0, math.sqrt(float(len(down_machs))))
                for v in down_machs:
                    mach_adj[u][v] += wt

            seen_up: set[str] = {u}
            stack_up = list(rev_adj.get(u, []))
            while stack_up:
                cur = stack_up.pop()
                if cur in seen_up:
                    continue
                seen_up.add(cur)
                if cur.startswith("PIN_IN:"):
                    west_in_weight[u] += 6.0 if u in fluid_machines else 2.0
                elif (
                    cur not in machine_set
                    and cur in nodes
                    and nodes[cur].node_type
                    in (
                        "split",
                        "merge",
                        "balancer",
                    )
                ):
                    stack_up.extend(rev_adj.get(cur, []))

        # Condense strongly connected components (Tarjan's SCC) for feedback loops
        sccs = self._find_tarjan_sccs(machine_names, mach_adj)
        has_cycles = any(len(scc) > 1 for scc in sccs)
        cyclic_nodes: set[str] = {u for scc in sccs if len(scc) > 1 for u in scc}

        # Find reachable PIN_OUT targets for each node to identify dedicated science domains
        reachable_outs: dict[str, set[str]] = {}
        for u in machine_names:
            outs: set[str] = set()
            seen_u: set[str] = {u}
            stack_u = list(fwd_adj.get(u, []))
            while stack_u:
                cur = stack_u.pop()
                if cur in seen_u:
                    continue
                seen_u.add(cur)
                if cur.startswith("PIN_OUT:"):
                    outs.add(cur)
                elif cur in nodes and nodes[cur].node_type not in ("combinator", "display"):
                    stack_u.extend(fwd_adj.get(cur, []))
            reachable_outs[u] = outs

        acyclic_machines = [u for u in machine_names if u not in cyclic_nodes]
        solid_machine_names = [u for u in acyclic_machines if u not in fluid_machines]
        fluid_machine_names = [u for u in acyclic_machines if u in fluid_machines]

        furnace_names = [
            u
            for u in solid_machine_names
            if ("furnace" in nodes[u].macro and not nodes[u].macro.startswith("assembling_machine"))
            or nodes[u].rank <= 1
        ]
        furnace_set = set(furnace_names)
        non_furnace_solid = [u for u in solid_machine_names if u not in furnace_set]

        shared_solid = [u for u in non_furnace_solid if len(reachable_outs.get(u, set())) != 1]
        dedicated_by_out: dict[str, list[str]] = collections.defaultdict(list)
        for u in non_furnace_solid:
            ro = reachable_outs.get(u, set())
            if len(ro) == 1:
                dedicated_by_out[next(iter(ro))].append(u)

        def _sort_mach_list(mlist: list[str]) -> list[str]:
            return sorted(
                mlist,
                key=lambda name: (
                    nodes[name].rank,
                    nodes[name].flow_depth,
                    _sanitize(nodes[name].recipe),
                    _natural_key(name),
                ),
            )

        furnace_names = _sort_mach_list(furnace_names)
        shared_solid = _sort_mach_list(shared_solid)
        fluid_machine_names = _sort_mach_list(fluid_machine_names)

        max_grp_len = 8

        def _chunk_by_recipe(mlist: list[str], chunk_sz: int = max_grp_len) -> list[list[str]]:
            if grid_w >= 5 or has_cycles:
                by_rec_only: dict[str, list[str]] = collections.defaultdict(list)
                for name in mlist:
                    nd = nodes[name]
                    by_rec_only[_sanitize(nd.recipe) or name].append(name)
                res_grps: list[list[str]] = []
                for rec_k in sorted(
                    by_rec_only.keys(),
                    key=lambda rk: (min(nodes[u].rank for u in by_rec_only[rk]), rk),
                ):
                    rg = by_rec_only[rec_k]
                    if len(rg) <= chunk_sz:
                        res_grps.append(rg)
                    else:
                        n_chunks = max(1, math.ceil(len(rg) / float(chunk_sz)))
                        step = math.ceil(len(rg) / float(n_chunks))
                        res_grps.extend(rg[i : i + step] for i in range(0, len(rg), step))
                return res_grps

            by_rec: dict[tuple[int, str], list[str]] = collections.defaultdict(list)
            for name in mlist:
                nd = nodes[name]
                by_rec[(nd.rank, _sanitize(nd.recipe) or name)].append(name)
            res_grps = []
            for k in sorted(by_rec.keys(), key=lambda key: (key[0], key[1])):
                rg = by_rec[k]
                if len(rg) <= chunk_sz:
                    res_grps.append(rg)
                else:
                    res_grps.extend(rg[i : i + chunk_sz] for i in range(0, len(rg), chunk_sz))
            return res_grps

        groups: list[list[str]] = []
        atomic_domain_first_nodes: set[str] = set()

        # 0. Cyclic feedback SCC groups (if any exist in recipe graph)
        for scc in sccs:
            if len(scc) > 1:
                sorted_scc = _sort_mach_list(scc)
                scc_area = sum(_node_area(nodes[u]) for u in sorted_scc)
                if scc_area <= single_core_capacity * 0.55:
                    groups.append(sorted_scc)
                    atomic_domain_first_nodes.add(sorted_scc[0])
                else:
                    groups.extend(
                        sorted_scc[i : i + max_grp_len]
                        for i in range(0, len(sorted_scc), max_grp_len)
                    )

        # 1. Smelting domain groups
        groups.extend(_chunk_by_recipe(furnace_names, max_grp_len))

        # 2. Fluid & chemical domain groups (fed by West PIN_IN crude-oil, water, coal)
        if fluid_machine_names:
            fluid_area = sum(_node_area(nodes[u]) for u in fluid_machine_names)
            if fluid_area <= single_core_capacity * 0.55:
                groups.append(fluid_machine_names)
                atomic_domain_first_nodes.add(fluid_machine_names[0])
            else:
                groups.extend(_chunk_by_recipe(fluid_machine_names, 10))

        # 3. Shared intermediate component domain groups
        if shared_solid:
            shared_area = sum(_node_area(nodes[u]) for u in shared_solid)
            if shared_area <= single_core_capacity * 0.45:
                groups.append(shared_solid)
                atomic_domain_first_nodes.add(shared_solid[0])
            else:
                groups.extend(_chunk_by_recipe(shared_solid, max_grp_len))

        # 4. Dedicated science pack assembly domains
        sorted_out_keys = sorted(
            dedicated_by_out.keys(),
            key=lambda out_k: (
                max(nodes[u].rank for u in dedicated_by_out[out_k]),
                len(dedicated_by_out[out_k]),
                out_k,
            ),
        )
        for out_k in sorted_out_keys:
            dom_machs = _sort_mach_list(dedicated_by_out[out_k])
            dom_area = sum(_node_area(nodes[u]) for u in dom_machs)
            if dom_area <= single_core_capacity * 0.48:
                groups.append(dom_machs)
                atomic_domain_first_nodes.add(dom_machs[0])
            else:
                groups.extend(_chunk_by_recipe(dom_machs, max_grp_len))

        def _cluster_weight(nd: PlacerNode) -> float:
            return _node_area(nd) + 45.0 * float(len(nd.inputs)) + 40.0 * float(len(nd.outputs))

        grp_weights = [
            min(single_core_capacity * 0.35, sum(_cluster_weight(nodes[u]) for u in grp))
            if (grp and grp[0] in atomic_domain_first_nodes and grid_w < 5)
            else sum(_cluster_weight(nodes[u]) for u in grp)
            for grp in groups
        ]
        pref = [0.0]
        for gw in grp_weights:
            pref.append(pref[-1] + gw)

        total_cluster_w = pref[-1]
        num_groups = len(groups)
        if distribute_across_blocks:
            k_bins = min(num_groups, num_blocks)
        else:
            k_bins = min(num_groups, num_blocks, max(1, math.ceil(total_cluster_w / 1400.0)))

        assigned: set[str] = set()
        blk_to_seq_idx = {b: idx for idx, b in enumerate(active_blocks)}

        if k_bins <= 1:
            blk = active_blocks[0]
            for grp in groups:
                for u in grp:
                    nodes[u].assigned_block = blk
                    block_bins[blk].append(u)
                    block_area[blk] += _node_area(nodes[u])
                    assigned.add(u)
        else:
            # Exact DP sequence partitioning into k_bins contiguous non-empty block segments
            inf_tuple = (float("inf"), float("inf"), -1)
            dp: list[list[tuple[float, float, int]]] = [
                [inf_tuple] * (num_groups + 1) for _ in range(k_bins + 1)
            ]
            for i in range(1, num_groups - k_bins + 2):
                w = pref[i]
                dp[1][i] = (w, w * w, 0)
            for b_idx in range(2, k_bins + 1):
                max_i = num_groups - (k_bins - b_idx)
                for i in range(b_idx, max_i + 1):
                    best_val = inf_tuple
                    for p in range(b_idx - 1, i):
                        prev_max, prev_sq, _ = dp[b_idx - 1][p]
                        seg_w = pref[i] - pref[p]
                        cand_max = prev_max if prev_max >= seg_w else seg_w
                        cand_sq = prev_sq + seg_w * seg_w
                        if (round(cand_max, 2), round(cand_sq, 2)) < (
                            round(best_val[0], 2),
                            round(best_val[1], 2),
                        ):
                            best_val = (cand_max, cand_sq, p)
                    dp[b_idx][i] = best_val

            seg_bounds: list[tuple[int, int]] = []
            curr_i = num_groups
            for b_idx in range(k_bins, 0, -1):
                prev_i = dp[b_idx][curr_i][2]
                seg_bounds.append((prev_i, curr_i))
                curr_i = prev_i
            seg_bounds.reverse()

            dp_buckets = [
                [u for grp in groups[g_start:g_end] for u in grp] for g_start, g_end in seg_bounds
            ]

            if grid_w >= 5 or has_cycles:
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
                        nodes[u].assigned_block = blk
                        block_bins[blk].append(u)
                        block_area[blk] += _node_area(nodes[u])
                        assigned.add(u)
                # Use South-to-North, West-to-East row-major ordering for tree leaf partitioning
                blk_to_seq_idx = {b: b[1] * grid_w + b[0] for b in active_blocks}
            else:
                for b_idx, bucket in enumerate(dp_buckets):
                    blk = active_blocks[b_idx]
                    for u in bucket:
                        nodes[u].assigned_block = blk
                        block_bins[blk].append(u)
                        block_area[blk] += _node_area(nodes[u])
                        assigned.add(u)

        def _assign_to_best_block(u: str, preferred_blk: tuple[int, int]) -> None:
            u_area = _node_area(nodes[u])
            nodes[u].assigned_block = preferred_blk
            block_bins[preferred_blk].append(u)
            block_area[preferred_blk] += u_area
            assigned.add(u)

        self._last_rewired_ports: dict[str, dict[str, str]] = {}
        if len(active_blocks) > 1 and edges:
            self._last_rewired_ports = self._rewire_trees_for_blocks(
                nodes=nodes,
                edges=edges,
                blk_to_seq_idx=blk_to_seq_idx,
                assigned=assigned,
                assign_fn=_assign_to_best_block,
            )
            if self._last_rewired_ports:
                in_drivers.clear()
                out_sinks.clear()
                for e in edges:
                    if e.source in nodes and e.sink in nodes and e.source != e.sink:
                        out_sinks[e.source].append(e.sink)
                        in_drivers[e.sink].append(e.source)
                self._compute_flow_depths_and_columns(nodes, edges, use_chassis=True)

        pin_in_nodes: set[str] = set()
        pin_out_nodes: set[str] = set()
        if edges:
            for e in edges:
                if e.source.startswith("PIN_IN:") and e.sink in nodes:
                    pin_in_nodes.add(e.sink)
                if e.sink.startswith("PIN_OUT:") and e.source in nodes:
                    pin_out_nodes.add(e.source)

        def _blk_pt(u: str) -> tuple[int, float]:
            ub = nodes[u].assigned_block
            return (
                ub[0],
                (float(ub[1]) - 0.5) if nodes[u].is_matrix_node else float(ub[1]),
            )

        def _find_upstream_pts(start_u: str, ref_by: float = 1.0) -> list[tuple[int, float]]:
            found: list[tuple[int, float]] = []
            if start_u in pin_in_nodes:
                found.append((0, float(ref_by)))
            seen: set[str] = {start_u}
            stack = list(in_drivers.get(start_u, []))
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                if cur in assigned and nodes[cur].assigned_block in block_area:
                    found.append(_blk_pt(cur))
                elif cur in nodes:
                    if cur in pin_in_nodes:
                        found.append((0, float(ref_by)))
                    stack.extend(in_drivers.get(cur, []))
            return found

        def _find_downstream_pts(start_u: str, ref_by: float = 1.0) -> list[tuple[int, float]]:
            found: list[tuple[int, float]] = []
            if start_u in pin_out_nodes:
                found.append((max(0, grid_w - 1), float(ref_by)))
            seen: set[str] = {start_u}
            stack = list(out_sinks.get(start_u, []))
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                if cur in assigned and nodes[cur].assigned_block in block_area:
                    found.append(_blk_pt(cur))
                elif cur in nodes:
                    if cur in pin_out_nodes:
                        found.append((max(0, grid_w - 1), float(ref_by)))
                    stack.extend(out_sinks.get(cur, []))
            return found

        bay_counts: dict[tuple[int, int], int] = collections.Counter()

        # Phase 2: Assign mergers bottom-up in topological order from machine drivers to root
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
            is_mat = nodes[m_name].is_matrix_node
            drv_blks = [
                nodes[d].assigned_block
                for d in in_drivers.get(m_name, [])
                if d in assigned and nodes[d].assigned_block in block_area
            ]
            drv_pts = [
                _blk_pt(d)
                for d in in_drivers.get(m_name, [])
                if d in assigned and nodes[d].assigned_block in block_area
            ]
            ref_by_m = (
                sum(pt[1] for pt in drv_pts) / len(drv_pts)
                if drv_pts
                else float(max(1, grid_h // 2))
            )
            down_pts = _find_downstream_pts(m_name, ref_by=ref_by_m)
            down_blks = [(pt[0], min(grid_h - 1, max(0, round(pt[1])))) for pt in down_pts]
            if drv_blks or is_mat:
                counts = collections.Counter(drv_blks)
                cand_blks = (
                    list(active_blocks)
                    if is_mat
                    else list(dict.fromkeys(drv_blks + down_blks)) or list(active_blocks)
                )
                ref_pts = drv_pts or down_pts or [(active_blocks[0][0], float(active_blocks[0][1]))]
                down_wt = (0.45 if is_mat else 1.0) / max(1, len(down_pts))
                best_blk = min(
                    cand_blks,
                    key=lambda b: (
                        (bay_counts[b] // 6)
                        if (is_mat and grid_w >= 5)
                        else (1 if (is_mat and grid_h >= 2 and b[1] == 0) else 0),
                        (1 if (is_mat and grid_h >= 2 and b[1] == 0) else 0)
                        if grid_w >= 5
                        else ((bay_counts[b] // 6) if is_mat else 0),
                        0 if (not is_mat and counts[b] > len(drv_blks) // 2) else 1,
                        round(
                            sum(
                                abs(b[0] - dpt[0])
                                + abs(((float(b[1]) - 0.5) if is_mat else float(b[1])) - dpt[1])
                                for dpt in ref_pts
                            )
                            + down_wt
                            * sum(
                                abs(b[0] - spt[0])
                                + abs(((float(b[1]) - 0.5) if is_mat else float(b[1])) - spt[1])
                                for spt in down_pts
                            )
                            + (0.35 * bay_counts[b] if is_mat else 0.0),
                            3,
                        ),
                        -counts[b],
                        bay_counts[b] if is_mat else block_area[b],
                        blk_to_seq_idx[b],
                    ),
                )
            else:
                best_blk = down_blks[0] if down_blks else active_blocks[0]
            _assign_to_best_block(m_name, best_blk)
            if is_mat:
                bay_counts[best_blk] += 1

        # Phase 3: Assign splitters and balancers bottom-up in reverse-topological order from sinks to root
        rem_splits = [
            name
            for name, nd in nodes.items()
            if name not in assigned and nd.node_type in ("split", "balancer")
        ]
        rem_splits.sort(key=lambda name: (-nodes[name].flow_depth, _natural_key(name)))
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
            is_mat = nodes[s_name].is_matrix_node
            is_bal = nodes[s_name].node_type == "balancer"
            snk_blks = [
                nodes[v].assigned_block
                for v in out_sinks.get(s_name, [])
                if v in assigned and nodes[v].assigned_block in block_area
            ]
            snk_pts = [
                _blk_pt(v)
                for v in out_sinks.get(s_name, [])
                if v in assigned and nodes[v].assigned_block in block_area
            ]
            ref_by_s = (
                sum(pt[1] for pt in snk_pts) / len(snk_pts)
                if snk_pts
                else float(max(1, grid_h // 2))
            )
            up_pts = _find_upstream_pts(s_name, ref_by=ref_by_s)
            up_blks = [(pt[0], min(grid_h - 1, max(0, round(pt[1])))) for pt in up_pts]
            if snk_blks or is_mat:
                counts = collections.Counter(snk_blks)
                cand_blks = (
                    list(active_blocks)
                    if is_mat
                    else list(dict.fromkeys(snk_blks + up_blks)) or list(active_blocks)
                )
                ref_pts = snk_pts or up_pts or [(active_blocks[0][0], float(active_blocks[0][1]))]
                in_port_cnt = 2.0 if is_bal else (0.45 if is_mat else 1.0)
                up_wt = in_port_cnt / max(1, len(up_pts))
                best_blk = min(
                    cand_blks,
                    key=lambda b: (
                        (bay_counts[b] // 6)
                        if (is_mat and grid_w >= 5)
                        else (1 if (is_mat and grid_h >= 2 and b[1] == 0) else 0),
                        1
                        if (
                            grid_w >= 5
                            and is_bal
                            and any(nodes[x].node_type == "balancer" for x in block_bins[b])
                        )
                        else 0,
                        b[0] if (grid_w >= 5 and is_bal and s_name in pin_in_nodes) else 0,
                        (1 if (is_mat and not is_bal and grid_h >= 2 and b[1] == 0) else 0)
                        if grid_w >= 5
                        else ((bay_counts[b] // 6) if is_mat else 0),
                        0 if (not is_mat and counts[b] > len(snk_blks) // 2) else 1,
                        round(
                            sum(
                                abs(b[0] - spt[0])
                                + abs(((float(b[1]) - 0.5) if is_mat else float(b[1])) - spt[1])
                                for spt in ref_pts
                            )
                            + up_wt
                            * sum(
                                abs(b[0] - upt[0])
                                + abs(((float(b[1]) - 0.5) if is_mat else float(b[1])) - upt[1])
                                for upt in up_pts
                            )
                            + (0.35 * bay_counts[b] if is_mat else 0.0),
                            3,
                        ),
                        -counts[b],
                        bay_counts[b] if is_mat else block_area[b],
                        blk_to_seq_idx[b],
                    ),
                )
            else:
                best_blk = up_blks[0] if up_blks else active_blocks[0]
            _assign_to_best_block(s_name, best_blk)
            if is_mat:
                bay_counts[best_blk] += 1

        # Phase 3b: Eliminate multi-block U-turns for matrix splitters by placing them in the intermediate bay between vertically separated sinks
        if grid_w >= 5 and len(active_blocks) > 1:
            mat_splits = [
                name
                for name, nd in nodes.items()
                if nd.is_matrix_node and nd.node_type == "split" and name in assigned
            ]
            mat_splits.sort(key=lambda name: (nodes[name].flow_depth, _natural_key(name)))
            for s_name in mat_splits:
                cur_blk = nodes[s_name].assigned_block
                drv_pts = [
                    _blk_pt(u)
                    for u in in_drivers.get(s_name, [])
                    if u in assigned and nodes[u].assigned_block in block_area
                ]
                snk_pts = [
                    _blk_pt(v)
                    for v in out_sinks.get(s_name, [])
                    if v in assigned and nodes[v].assigned_block in block_area
                ]
                if not drv_pts or len(snk_pts) < 2:
                    continue
                min_sy = min(pt[1] for pt in snk_pts)
                max_sy = max(pt[1] for pt in snk_pts)
                if max_sy - min_sy < 2.0:
                    continue
                all_nbr_pts = drv_pts + snk_pts

                def _mat_wl(
                    b: tuple[int, int], _pts: list[tuple[int, float]] = all_nbr_pts
                ) -> float:
                    by_f = float(b[1]) - 0.5
                    return sum(abs(b[0] - pt[0]) + abs(by_f - pt[1]) for pt in _pts)

                cur_wl = _mat_wl(cur_blk)
                between_cands = [
                    b
                    for b in active_blocks
                    if min_sy < (float(b[1]) - 0.5) < max_sy
                    and (bay_counts[b] < 6 if b != cur_blk else True)
                ]
                if not between_cands:
                    continue
                best_cand = min(
                    between_cands,
                    key=lambda b: (
                        round(_mat_wl(b), 3),
                        bay_counts[b] if b != cur_blk else max(0, bay_counts[b] - 1),
                        blk_to_seq_idx[b],
                    ),
                )
                if best_cand != cur_blk and _mat_wl(best_cand) <= cur_wl - 1.0:
                    u_area = _node_area(nodes[s_name])
                    block_bins[cur_blk].remove(s_name)
                    block_area[cur_blk] -= u_area
                    bay_counts[cur_blk] -= 1
                    nodes[s_name].assigned_block = best_cand
                    block_bins[best_cand].append(s_name)
                    block_area[best_cand] += u_area
                    bay_counts[best_cand] += 1

        # Phase 4: Assign remaining telemetry combinators and displays to connected driver or co-sink block
        rem_names = [name for name in nodes if name not in assigned]
        rem_names.sort(key=lambda name: (nodes[name].flow_depth, _natural_key(name)))
        for u_name in rem_names:
            nb_blks = [
                nodes[nb].assigned_block
                for nb in in_drivers.get(u_name, []) + out_sinks.get(u_name, [])
                if nb in assigned and nodes[nb].assigned_block in block_area
            ]
            if not nb_blks:
                nb_blks = [
                    nodes[co].assigned_block
                    for drv in in_drivers.get(u_name, [])
                    for co in out_sinks.get(drv, [])
                    if co in assigned and nodes[co].assigned_block in block_area
                ]
            best_blk = nb_blks[0] if nb_blks else min(active_blocks, key=lambda b: block_area[b])
            _assign_to_best_block(u_name, best_blk)

        return {blk: block_bins[blk] for blk in active_blocks if block_bins[blk]}

    def _rewire_trees_for_blocks(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        blk_to_seq_idx: dict[tuple[int, int], int],
        assigned: set[str],
        assign_fn: Any,
    ) -> dict[str, dict[str, str]]:
        """Re-parenthesize merger and splitter trees so each block has self-contained local trees."""
        rewire_map: dict[str, dict[str, str]] = {}
        in_edges: dict[str, list[PlacerEdge]] = collections.defaultdict(list)
        out_edges: dict[str, list[PlacerEdge]] = collections.defaultdict(list)
        for e in edges:
            if e.source != e.sink:
                out_edges[e.source].append(e)
                in_edges[e.sink].append(e)

        # 1. Re-parenthesize belt_merge2 connected components
        merge_names = [n for n, nd in nodes.items() if nd.node_type == "merge"]
        merge_set = set(merge_names)
        m_adj: dict[str, set[str]] = collections.defaultdict(set)
        for e in edges:
            if (
                e.source in merge_set
                and e.sink in merge_set
                and nodes[e.source].recipe == nodes[e.sink].recipe
            ):
                m_adj[e.source].add(e.sink)
                m_adj[e.sink].add(e.source)

        seen_m: set[str] = set()
        m_comps: list[list[str]] = []
        for m in sorted(merge_names, key=_natural_key):
            if m in seen_m:
                continue
            comp: list[str] = []
            q = [m]
            seen_m.add(m)
            while q:
                cur = q.pop(0)
                comp.append(cur)
                for nb in sorted(m_adj.get(cur, ()), key=_natural_key):
                    if nb not in seen_m:
                        seen_m.add(nb)
                        q.append(nb)
            m_comps.append(comp)

        for comp in m_comps:
            comp_set = set(comp)
            out_w: dict[str, str] = {}
            valid_m = True
            for m in comp:
                ow_set = {e.wire for e in out_edges.get(m, [])}
                iw_list = [e.wire for e in in_edges.get(m, [])]
                if len(ow_set) != 1 or len(iw_list) != 2 or len(set(iw_list)) != 2:
                    valid_m = False
                    break
                out_w[m] = next(iter(ow_set))
            if not valid_m:
                continue

            internal_m = [m for m in comp if any(e.sink in comp_set for e in out_edges.get(m, []))]
            root_m = [m for m in comp if m not in internal_m]
            leaf_edges = [e for m in comp for e in in_edges.get(m, []) if e.source not in comp_set]
            if (
                len(root_m) != 1
                or len(internal_m) != len(comp) - 1
                or len(leaf_edges) != len(comp) + 1
                or len({e.wire for e in leaf_edges}) != len(leaf_edges)
                or not all(e.source in assigned and e.source in nodes for e in leaf_edges)
            ):
                continue

            avail_pairs = [(m, out_w[m]) for m in sorted(internal_m, key=_natural_key)]
            root_pair = (root_m[0], out_w[root_m[0]])

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

            def _build_merge_subtree(
                wires: list[str],
                _avail: list[tuple[str, str]] = avail_pairs,
                _root: tuple[str, str] = root_pair,
                use_root: bool = False,
            ) -> tuple[str, list[str]]:
                if len(wires) == 1:
                    return wires[0], []
                if len(wires) == 2:
                    m_name, y_w = _root if use_root else _avail.pop(0)
                    rewire_map[m_name] = {"a": wires[0], "b": wires[1], "y": y_w}
                    return y_w, [m_name]
                mid = (len(wires) + 1) // 2
                w_left, used_l = _build_merge_subtree(wires[:mid], _avail, _root, use_root=False)
                w_right, used_r = _build_merge_subtree(wires[mid:], _avail, _root, use_root=False)
                m_name, y_w = _root if use_root else _avail.pop(0)
                rewire_map[m_name] = {"a": w_left, "b": w_right, "y": y_w}
                return y_w, used_l + used_r + [m_name]

            def _build_spatial_merge_subtree(
                blk_items: list[tuple[str, tuple[int, int]]],
                _avail: list[tuple[str, str]] = avail_pairs,
                _root: tuple[str, str] = root_pair,
                use_root: bool = False,
            ) -> tuple[str, list[str]]:
                if len(blk_items) == 1:
                    return blk_items[0][0], []
                if len(blk_items) == 2:
                    m_name, y_w = _root if use_root else _avail.pop(0)
                    rewire_map[m_name] = {
                        "a": blk_items[0][0],
                        "b": blk_items[1][0],
                        "y": y_w,
                    }
                    return y_w, [m_name]
                xs = [b[0] for _, b in blk_items]
                ys = [b[1] for _, b in blk_items]
                if (max(xs) - min(xs)) >= (max(ys) - min(ys)):
                    ordered = sorted(
                        blk_items, key=lambda it: (it[1][0], it[1][1], _natural_key(it[0]))
                    )
                else:
                    ordered = sorted(
                        blk_items, key=lambda it: (it[1][1], it[1][0], _natural_key(it[0]))
                    )
                mid = (len(ordered) + 1) // 2
                w_left, used_l = _build_spatial_merge_subtree(
                    ordered[:mid], _avail, _root, use_root=False
                )
                w_right, used_r = _build_spatial_merge_subtree(
                    ordered[mid:], _avail, _root, use_root=False
                )
                m_name, y_w = _root if use_root else _avail.pop(0)
                rewire_map[m_name] = {"a": w_left, "b": w_right, "y": y_w}
                return y_w, used_l + used_r + [m_name]

            if len(sorted_blks) == 1:
                only_b = sorted_blks[0]
                _rw, used_m = _build_merge_subtree([e.wire for e in by_blk[only_b]], use_root=True)
                for m_name in used_m:
                    nodes[m_name].is_matrix_node = False
                    assign_fn(m_name, only_b)
            else:
                blk_export_items: list[tuple[str, tuple[int, int]]] = []
                for b in sorted_blks:
                    b_wires = [e.wire for e in by_blk[b]]
                    exp_w, used_local = _build_merge_subtree(b_wires, use_root=False)
                    blk_export_items.append((exp_w, b))
                    for m_name in used_local:
                        nodes[m_name].is_matrix_node = False
                        assign_fn(m_name, b)
                _rw, used_mat = _build_spatial_merge_subtree(blk_export_items, use_root=True)
                for m_name in used_mat:
                    nodes[m_name].is_matrix_node = True

        # 2. Re-parenthesize belt_split2 forests by material
        split_names = [n for n, nd in nodes.items() if nd.node_type == "split"]
        split_set = set(split_names)
        s_adj: dict[str, set[str]] = collections.defaultdict(set)
        for e in edges:
            if (
                e.source in split_set
                and e.sink in split_set
                and nodes[e.source].recipe == nodes[e.sink].recipe
            ):
                s_adj[e.source].add(e.sink)
                s_adj[e.sink].add(e.source)

        seen_s: set[str] = set()
        s_comps: list[list[str]] = []
        for s in sorted(split_names, key=_natural_key):
            if s in seen_s:
                continue
            comp = []
            q = [s]
            seen_s.add(s)
            while q:
                cur = q.pop(0)
                comp.append(cur)
                for nb in sorted(s_adj.get(cur, ()), key=_natural_key):
                    if nb not in seen_s:
                        seen_s.add(nb)
                        q.append(nb)
            s_comps.append(comp)

        valid_s_by_recipe: dict[str, list[tuple[str, list[str], set[str], list[PlacerEdge]]]] = (
            collections.defaultdict(list)
        )
        for comp in s_comps:
            comp_set = set(comp)
            in_w: dict[str, str] = {}
            valid_s = True
            for s in comp:
                iw_set = {e.wire for e in in_edges.get(s, [])}
                ow_set = {e.wire for e in out_edges.get(s, [])}
                if len(iw_set) != 1 or len(ow_set) != 2:
                    valid_s = False
                    break
                in_w[s] = next(iter(iw_set))
            if not valid_s:
                continue

            int_wires = {e.wire for e in edges if e.source in comp_set and e.sink in comp_set}
            root_s = [s for s in comp if in_w[s] not in int_wires]
            leaf_edges = [e for s in comp for e in out_edges.get(s, []) if e.sink not in comp_set]
            if (
                len(root_s) != 1
                or len(int_wires) != len(comp) - 1
                or len(leaf_edges) != len(comp) + 1
                or len({e.wire for e in leaf_edges}) != len(leaf_edges)
                or not all(e.sink in assigned and e.sink in nodes for e in leaf_edges)
            ):
                continue

            r_wire = in_w[root_s[0]]
            r_key = nodes[root_s[0]].recipe or r_wire.split("__")[0]
            valid_s_by_recipe[r_key].append((r_wire, comp, int_wires, leaf_edges))

        for _r_key, comp_infos in sorted(valid_s_by_recipe.items()):
            comp_infos.sort(key=lambda item: _natural_key(item[0]))
            root_wires = [item[0] for item in comp_infos]
            orig_sizes = [len(item[3]) for item in comp_infos]
            avail_splits = sorted((s for item in comp_infos for s in item[1]), key=_natural_key)
            avail_int_w = sorted((w for item in comp_infos for w in item[2]), key=_natural_key)
            all_leaves = [e for item in comp_infos for e in item[3]]
            all_leaves.sort(
                key=lambda e: (
                    blk_to_seq_idx.get(nodes[e.sink].assigned_block, 0),
                    nodes[e.sink].rank,
                    nodes[e.sink].flow_depth,
                    _natural_key(e.sink),
                    _natural_key(e.wire),
                )
            )

            total_s = len(all_leaves)
            k_trees = len(root_wires)
            cuts = [0]
            cum = 0
            max_b_size = max(max(orig_sizes), 24)
            blk_bounds = [
                idx
                for idx in range(1, total_s)
                if nodes[all_leaves[idx].sink].assigned_block
                != nodes[all_leaves[idx - 1].sink].assigned_block
            ]
            for t_idx in range(k_trees - 1):
                cum += orig_sizes[t_idx]
                best_c = cum
                rem_trees = k_trees - 1 - t_idx
                for b_cut in sorted(blk_bounds, key=lambda x: abs(x - cum)):
                    if (
                        abs(b_cut - cum) <= 6
                        and 2 <= (b_cut - cuts[-1]) <= max_b_size
                        and 2 * rem_trees <= (total_s - b_cut) <= max_b_size * rem_trees
                    ):
                        best_c = b_cut
                        break
                cuts.append(best_c)
            cuts.append(total_s)

            def _build_split_subtree(
                in_wire: str,
                out_wires: list[str],
                _avail_s: list[str] = avail_splits,
                _avail_w: list[str] = avail_int_w,
            ) -> list[str]:
                if len(out_wires) < 2:
                    return []
                s_name = _avail_s.pop(0)
                if len(out_wires) == 2:
                    rewire_map[s_name] = {
                        "a": in_wire,
                        "y1": out_wires[0],
                        "y2": out_wires[1],
                    }
                    return [s_name]
                mid = len(out_wires) // 2
                left_o = out_wires[:mid]
                right_o = out_wires[mid:]
                w_l = left_o[0] if len(left_o) == 1 else _avail_w.pop(0)
                w_r = right_o[0] if len(right_o) == 1 else _avail_w.pop(0)
                rewire_map[s_name] = {"a": in_wire, "y1": w_l, "y2": w_r}
                used = [s_name]
                if len(left_o) > 1:
                    used.extend(_build_split_subtree(w_l, left_o, _avail_s, _avail_w))
                if len(right_o) > 1:
                    used.extend(_build_split_subtree(w_r, right_o, _avail_s, _avail_w))
                return used

            def _build_spatial_split_subtree(
                in_wire: str,
                blk_items: list[tuple[str, tuple[int, int]]],
                _avail_s: list[str] = avail_splits,
                _avail_w: list[str] = avail_int_w,
            ) -> list[str]:
                if len(blk_items) < 2:
                    return []
                s_name = _avail_s.pop(0)
                if len(blk_items) == 2:
                    rewire_map[s_name] = {
                        "a": in_wire,
                        "y1": blk_items[0][0],
                        "y2": blk_items[1][0],
                    }
                    return [s_name]
                xs = [b[0] for _, b in blk_items]
                ys = [b[1] for _, b in blk_items]
                if (max(xs) - min(xs)) >= (max(ys) - min(ys)):
                    ordered = sorted(
                        blk_items, key=lambda it: (it[1][0], it[1][1], _natural_key(it[0]))
                    )
                else:
                    ordered = sorted(
                        blk_items, key=lambda it: (it[1][1], it[1][0], _natural_key(it[0]))
                    )
                mid = len(ordered) // 2
                left_o = ordered[:mid]
                right_o = ordered[mid:]
                w_l = left_o[0][0] if len(left_o) == 1 else _avail_w.pop(0)
                w_r = right_o[0][0] if len(right_o) == 1 else _avail_w.pop(0)
                rewire_map[s_name] = {"a": in_wire, "y1": w_l, "y2": w_r}
                used = [s_name]
                if len(left_o) > 1:
                    used.extend(_build_spatial_split_subtree(w_l, left_o, _avail_s, _avail_w))
                if len(right_o) > 1:
                    used.extend(_build_spatial_split_subtree(w_r, right_o, _avail_s, _avail_w))
                return used

            for t_idx in range(k_trees):
                b_leaves = all_leaves[cuts[t_idx] : cuts[t_idx + 1]]
                r_wire = root_wires[t_idx]
                by_b: dict[tuple[int, int], list[PlacerEdge]] = collections.defaultdict(list)
                b_blks: list[tuple[int, int]] = []
                for e in b_leaves:
                    b = nodes[e.sink].assigned_block
                    if b not in by_b:
                        b_blks.append(b)
                    by_b[b].append(e)

                if len(b_blks) == 1:
                    only_b = b_blks[0]
                    used_s = _build_split_subtree(r_wire, [e.wire for e in b_leaves])
                    for s_name in used_s:
                        nodes[s_name].is_matrix_node = False
                        assign_fn(s_name, only_b)
                else:
                    blk_feed_items: list[tuple[str, tuple[int, int]]] = []
                    for b in b_blks:
                        bw = [e.wire for e in by_b[b]]
                        if len(bw) == 1:
                            blk_feed_items.append((bw[0], b))
                        else:
                            fw = avail_int_w.pop(0)
                            blk_feed_items.append((fw, b))
                            used_local = _build_split_subtree(fw, bw)
                            for s_name in used_local:
                                nodes[s_name].is_matrix_node = False
                                assign_fn(s_name, b)
                    used_mat = _build_spatial_split_subtree(r_wire, blk_feed_items)
                    for s_name in used_mat:
                        nodes[s_name].is_matrix_node = True

        # 3. Mark belt_balancer2 cells as matrix nodes in multi-block chassis
        for nd in nodes.values():
            if nd.node_type == "balancer":
                nd.is_matrix_node = True

        # 4. Update nodes and edges in-place from rewire_map
        if rewire_map:
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

            for iname, pmap in rewire_map.items():
                nd = nodes[iname]
                if nd.node_type == "merge":
                    nd.inputs = [pmap["a"], pmap["b"]]
                    nd.outputs = [pmap["y"]]
                    wire_snks[pmap["a"]].append((iname, 0.0))
                    wire_snks[pmap["b"]].append((iname, 1.0))
                    wire_drv[pmap["y"]] = (iname, 0.5)
                elif nd.node_type == "split":
                    nd.inputs = [pmap["a"]]
                    nd.outputs = [pmap["y1"], pmap["y2"]]
                    wire_snks[pmap["a"]].append((iname, 0.5))
                    wire_drv[pmap["y1"]] = (iname, 0.0)
                    wire_drv[pmap["y2"]] = (iname, 1.0)

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
        n = len(block_nodes)
        if n == 0:
            return

        name_to_idx = {node.name: idx for idx, node in enumerate(block_nodes)}
        max_col = max((node.col_index for node in block_nodes), default=0)
        usable_w = max(10.0, x_hi - x_lo)
        usable_h = max(10.0, y_hi - y_lo)

        # 1. Compute initial X target from normalized flow column index
        x_depth_target = np.zeros(n, dtype=float)
        for i, node in enumerate(block_nodes):
            frac_x = (node.col_index + 0.5) / float(max(1, max_col + 1))
            x_depth_target[i] = x_lo + frac_x * usable_w

        # Build SPD Laplacian system A_x * x = b_x with directed forward offsets
        reg_x = 0.35
        a_x = np.eye(n, dtype=float) * reg_x
        b_x = reg_x * x_depth_target.copy()

        for e in edges:
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
                a_x[v_idx, v_idx] += w * 0.5
                b_x[v_idx] += w * 0.5 * (x_lo + 4.0)
            elif u_idx is not None and v_idx is None:
                a_x[u_idx, u_idx] += w * 0.5
                b_x[u_idx] += w * 0.5 * (x_hi - 4.0)

        try:
            sol_x = np.linalg.solve(a_x, b_x)
        except np.linalg.LinAlgError:
            sol_x = x_depth_target

        # 2. Compute Y band targets so parallel recipes and instances spread across [y_lo, y_hi]
        # Order machines in each column by (recipe, natural_key) and spread them over [y_lo, y_hi]
        y_band_target = np.full(n, 0.5 * (y_lo + y_hi), dtype=float)
        by_col: dict[int, list[int]] = collections.defaultdict(list)
        for i, node in enumerate(block_nodes):
            by_col[node.col_index].append(i)

        # Assign recipe vertical band centers so connected recipes share similar Y regions
        recipes = sorted({node.recipe for node in block_nodes if node.recipe})
        recipe_band: dict[str, float] = {}
        for r_idx, r_name in enumerate(recipes):
            recipe_band[r_name] = (r_idx + 0.5) / float(max(1, len(recipes)))

        for col_id in sorted(by_col.keys()):
            col_indices = by_col[col_id]
            col_indices.sort(
                key=lambda idx: (
                    recipe_band.get(block_nodes[idx].recipe, 0.5),
                    _natural_key(block_nodes[idx].name),
                )
            )
            m_count = len(col_indices)
            for rank_in_col, idx in enumerate(col_indices):
                frac_y = (rank_in_col + 0.5) / float(max(1, m_count))
                y_band_target[idx] = y_lo + frac_y * usable_h

        # Propagate Y centroids to splitters (from sinks) and mergers (from drivers)
        adj_sinks: dict[int, list[int]] = collections.defaultdict(list)
        adj_drivers: dict[int, list[int]] = collections.defaultdict(list)
        for e in edges:
            u_idx = name_to_idx.get(e.source)
            v_idx = name_to_idx.get(e.sink)
            if u_idx is not None and v_idx is not None and u_idx != v_idx:
                adj_sinks[u_idx].append(v_idx)
                adj_drivers[v_idx].append(u_idx)

        for _ in range(8):
            # Backward pass for splitters/balancers
            for col_id in sorted(by_col.keys(), reverse=True):
                for idx in by_col[col_id]:
                    if block_nodes[idx].node_type in ("split", "balancer") and adj_sinks[idx]:
                        y_band_target[idx] = float(
                            np.mean([y_band_target[s] for s in adj_sinks[idx]])
                        )
            # Forward pass for mergers/circuit/display
            for col_id in sorted(by_col.keys()):
                for idx in by_col[col_id]:
                    if (
                        block_nodes[idx].node_type in ("merge", "combinator", "display")
                        and adj_drivers[idx]
                    ):
                        y_band_target[idx] = float(
                            np.mean([y_band_target[d] for d in adj_drivers[idx]])
                        )

        # Solve port-aware quadratic Y system A_y * y = b_y
        reg_y = 0.65
        a_y = np.eye(n, dtype=float) * reg_y
        b_y = reg_y * y_band_target.copy()

        for e in edges:
            u_idx = name_to_idx.get(e.source)
            v_idx = name_to_idx.get(e.sink)
            if u_idx is not None and v_idx is not None and u_idx != v_idx:
                w = float(e.weight)
                # Port offset difference: (y_v + sink_dy) - (y_u + source_dy) = 0
                port_diff = float(e.source_dy) - float(e.sink_dy)
                a_y[u_idx, u_idx] += w
                a_y[v_idx, v_idx] += w
                a_y[u_idx, v_idx] -= w
                a_y[v_idx, u_idx] -= w
                b_y[u_idx] -= w * port_diff
                b_y[v_idx] += w * port_diff

        try:
            sol_y = np.linalg.solve(a_y, b_y)
        except np.linalg.LinAlgError:
            sol_y = y_band_target

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
        """Legalize nodes inside [core_min_x, core_max_x] x [core_min_y, core_max_y].

        Returns the actual (used_width, used_height) of the core.
        """
        if not block_nodes:
            return core_max_x - core_min_x, core_max_y - core_min_y

        margin_x = 5
        margin_y = 4
        x_lo = core_min_x + margin_x
        y_lo = core_min_y + margin_y
        x_hi = core_max_x - margin_x
        y_hi = core_max_y - margin_y
        usable_h = max(16, y_hi - y_lo)

        # Group nodes by initial column index
        raw_cols: dict[int, list[PlacerNode]] = collections.defaultdict(list)
        for node in block_nodes:
            raw_cols[node.col_index].append(node)

        def _col_req_h(col: list[PlacerNode]) -> int:
            if not col:
                return 0
            req = 0
            for idx, nd in enumerate(col):
                if idx < len(col) - 1:
                    nxt = col[idx + 1]
                    v_gap = max(
                        10
                        if (nd.height >= 7 or nxt.height >= 7)
                        else (6 if nd.node_type == "machine" or nxt.node_type == "machine" else 4),
                        nd.pad_n + nxt.pad_s,
                    )
                    step = nd.height + v_gap
                    req += step + (step % 2)
                else:
                    req += nd.height + (nd.height % 2)
            return req

        # Fold any column whose vertical height exceeds usable_h into sub-columns when fixed_core is True
        expanded_cols: list[list[PlacerNode]] = []
        for col_id in sorted(raw_cols.keys()):
            col_nodes = raw_cols[col_id]
            col_nodes.sort(key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)))

            req_h = _col_req_h(col_nodes)
            num_subcols = max(1, math.ceil(float(req_h) / float(usable_h))) if fixed_core else 1
            if num_subcols == 1:
                expanded_cols.append(col_nodes)
            else:
                sub_lists: list[list[PlacerNode]] = [[] for _ in range(num_subcols)]
                for idx, nd in enumerate(col_nodes):
                    sub_lists[idx % num_subcols].append(nd)
                expanded_cols.extend(sl for sl in sub_lists if sl)

        if fixed_core:
            avail_w = max(10, x_hi - x_lo)
            node_type_by_name = {nd.name: nd.node_type for nd in block_nodes}

            def _total_min_w(cols: list[list[PlacerNode]]) -> int:
                if not cols:
                    return 0
                w_total = sum(max(nd.width for nd in col) for col in cols)
                g_total = 0
                for c_i in range(len(cols) - 1):
                    max_e = max(nd.pad_e for nd in cols[c_i])
                    max_w = max(nd.pad_w for nd in cols[c_i + 1])
                    g_total += max(5, max(max_e, max_w) + 1)
                return w_total + g_total

            def _total_hard_w(cols: list[list[PlacerNode]]) -> int:
                if not cols:
                    return 0
                return sum(max(nd.width for nd in col) for col in cols) + 4 * (len(cols) - 1)

            for allow_cross in (False, True):
                while (
                    len(expanded_cols) > 1
                    and (
                        _total_hard_w(expanded_cols) if allow_cross else _total_min_w(expanded_cols)
                    )
                    > avail_w
                ):
                    best_idx = -1
                    best_h: int | None = None
                    for idx in range(len(expanded_cols) - 1):
                        if not allow_cross:
                            names_a = {nd.name for nd in expanded_cols[idx]}
                            names_b = {nd.name for nd in expanded_cols[idx + 1]}
                            has_cross = any(
                                node_type_by_name.get(e.source) not in ("combinator", "display")
                                and node_type_by_name.get(e.sink) not in ("combinator", "display")
                                and (
                                    (e.source in names_a and e.sink in names_b)
                                    or (e.source in names_b and e.sink in names_a)
                                )
                                for e in edges
                            )
                            if has_cross:
                                continue
                        merged_cand = sorted(
                            expanded_cols[idx] + expanded_cols[idx + 1],
                            key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)),
                        )
                        comb_h = _col_req_h(merged_cand)
                        if comb_h > usable_h:
                            continue
                        if best_h is None or comb_h < best_h:
                            best_h = comb_h
                            best_idx = idx
                    if best_idx < 0:
                        break
                    merged_col = sorted(
                        expanded_cols[best_idx] + expanded_cols[best_idx + 1],
                        key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)),
                    )
                    expanded_cols[best_idx : best_idx + 2] = [merged_col]

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
        num_cols = len(cols)
        if num_cols == 0:
            return 0

        col_widths = [max(nd.width for nd in col) for col in cols]
        min_gaps: list[int] = []
        ideal_gaps: list[int] = []
        for c in range(num_cols - 1):
            max_e = max(nd.pad_e for nd in cols[c])
            max_w = max(nd.pad_w for nd in cols[c + 1])
            min_gaps.append(max(5, max(max_e, max_w) + 1))
            extra_ch = (
                0 if fixed_x else min(14, max(0, max(len(cols[c]), len(cols[c + 1])) - 8) // 3)
            )
            ideal_gaps.append(max_e + max_w + 2 + extra_ch)

        avail_w = max(10, x_hi - x_lo)
        sum_w = sum(col_widths)

        if not fixed_x:
            actual_gaps = list(ideal_gaps)
        else:
            if sum_w + sum(ideal_gaps) <= avail_w:
                # Distribute modest extra space while keeping connected columns compact
                spare = avail_w - (sum_w + sum(ideal_gaps))
                bonus = min(4, spare // max(1, num_cols))
                actual_gaps = [g + bonus for g in ideal_gaps]
            elif sum_w + sum(min_gaps) <= avail_w:
                spare = avail_w - (sum_w + sum(min_gaps))
                diff_total = max(1, sum(ideal_gaps[i] - min_gaps[i] for i in range(num_cols - 1)))
                actual_gaps = [
                    min_gaps[i] + math.floor(spare * (ideal_gaps[i] - min_gaps[i]) / diff_total)
                    for i in range(num_cols - 1)
                ]
            else:
                spare = max(0, avail_w - sum_w)
                base_g = max(2, spare // max(1, num_cols - 1))
                actual_gaps = [base_g] * max(0, num_cols - 1)

        # Assign integer X to each column and node
        col_x: list[int] = []
        curr_x = x_lo
        for c, col in enumerate(cols):
            if fixed_x and curr_x + col_widths[c] > x_hi:
                curr_x = max(x_lo, x_hi - col_widths[c])
            col_x.append(curr_x)
            for nd in col:
                nd.x = curr_x + (col_widths[c] - nd.width) // 2
                nd.col_index = c
            if c < num_cols - 1:
                curr_x += col_widths[c] + actual_gaps[c]
            else:
                curr_x += col_widths[c]

        used_width = max(1, curr_x - x_lo)

        # Build adjacency maps for iterative Y alignment
        node_map = {nd.name: nd for col in cols for nd in col}
        sinks_of: dict[str, list[tuple[str, float, float]]] = collections.defaultdict(list)
        drivers_of: dict[str, list[tuple[str, float, float]]] = collections.defaultdict(list)
        for e in edges:
            if e.source in node_map and e.sink in node_map and e.source != e.sink:
                sinks_of[e.source].append((e.sink, e.source_dy, e.sink_dy))
                drivers_of[e.sink].append((e.source, e.source_dy, e.sink_dy))

        # Initial Y legalization per column with column parity stagger
        band_h = max(8, y_hi - y_lo)
        for c, col in enumerate(cols):
            col.sort(key=lambda nd: (round(nd.cont_y, 2), _natural_key(nd.name)))
            stagger = (c % 3) * 2 if band_h >= 24 else 0
            c_y_lo = min(y_hi - sum(nd.height for nd in col), y_lo + stagger)
            heights = [nd.height for nd in col]
            gaps = [
                max(
                    (
                        10
                        if (col[i].height >= 7 or col[i + 1].height >= 7) and len(col) <= 4
                        else (
                            6
                            if col[i].node_type == "machine" or col[i + 1].node_type == "machine"
                            else 4
                        )
                    ),
                    col[i].pad_n + col[i + 1].pad_s,
                )
                for i in range(len(col) - 1)
            ]
            # Spread initial ideal_y across [c_y_lo, y_hi] while respecting cont_y order
            m = len(col)
            span_h = sum(heights) + sum(gaps)
            if m == 1:
                ideals = [float(np.clip(col[0].cont_y, c_y_lo, y_hi - col[0].height))]
            else:
                step = max(float(span_h) / float(m), min(16.0, float(band_h - 4) / float(m)))
                center_y = float(np.mean([nd.cont_y for nd in col]))
                start_y = max(
                    float(c_y_lo),
                    min(
                        float(y_hi) - step * (m - 1) - heights[-1], center_y - 0.5 * step * (m - 1)
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

        # Iterative refinement: align splitters to sink centroids, mergers to driver centroids,
        # and downstream machines to upstream driver centroids
        for _pass in range(6):
            # 1. Backward sweep: pull splitters and balancers to exact vertical centroid of sinks
            for c in range(num_cols - 1, -1, -1):
                col = cols[c]
                if not any(nd.node_type in ("split", "balancer") for nd in col):
                    continue
                ideals = []
                for nd in col:
                    if nd.node_type in ("split", "balancer") and sinks_of.get(nd.name):
                        target_ys = [
                            node_map[s_name].y + s_dy - src_dy
                            for s_name, src_dy, s_dy in sinks_of[nd.name]
                            if s_name in node_map
                        ]
                        ideals.append(float(np.mean(target_ys)) if target_ys else float(nd.y))
                    else:
                        ideals.append(float(nd.y))

                # Re-sort splitters in this column if ideal centroids crossed
                paired = sorted(
                    zip(ideals, col, strict=False),
                    key=lambda item: (round(item[0], 2), _natural_key(item[1].name)),
                )
                cols[c] = [item[1] for item in paired]
                col = cols[c]
                sorted_ideals = [item[0] for item in paired]
                heights = [nd.height for nd in col]
                gaps = [
                    max(
                        6
                        if col[i].node_type == "machine" or col[i + 1].node_type == "machine"
                        else 4,
                        col[i].pad_n + col[i + 1].pad_s,
                    )
                    for i in range(len(col) - 1)
                ]
                stagger = (c % 3) * 2 if band_h >= 24 else 0
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

            # 2. Forward sweep: pull mergers, pumps, and circuit/display cells to driver centroids
            for c in range(num_cols):
                col = cols[c]
                if not any(
                    nd.node_type in ("merge", "pump", "combinator", "display") for nd in col
                ):
                    continue
                ideals = []
                for nd in col:
                    if nd.node_type in ("merge", "pump", "combinator", "display"):
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
                            ideals.append(
                                0.75 * float(np.mean(drv_ys)) + 0.25 * float(np.mean(snk_ys))
                            )
                        elif drv_ys:
                            ideals.append(float(np.mean(drv_ys)))
                        elif snk_ys:
                            ideals.append(float(np.mean(snk_ys)))
                        else:
                            ideals.append(float(nd.y))
                    else:
                        ideals.append(float(nd.y))

                paired = sorted(
                    zip(ideals, col, strict=False),
                    key=lambda item: (round(item[0], 2), _natural_key(item[1].name)),
                )
                cols[c] = [item[1] for item in paired]
                col = cols[c]
                sorted_ideals = [item[0] for item in paired]
                heights = [nd.height for nd in col]
                gaps = [
                    max(
                        6
                        if col[i].node_type == "machine" or col[i + 1].node_type == "machine"
                        else 4,
                        col[i].pad_n + col[i + 1].pad_s,
                    )
                    for i in range(len(col) - 1)
                ]
                stagger = (c % 3) * 2 if band_h >= 24 else 0
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

        return used_width

    def place_netlist(
        self,
        nl: Any,
        merge_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        split_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None = None,
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = 94,
        chassis_core_h: int = 94,
        distribute_across_blocks: bool = False,
    ) -> AnalyticalPlacementResult:
        """Run analytical placement on a synthesized Netlist object."""
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
        netlist_data: dict[str, Any],
        def_data: dict[str, Any] | None = None,
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = 94,
        chassis_core_h: int = 94,
        distribute_across_blocks: bool = False,
    ) -> AnalyticalPlacementResult:
        """Run analytical placement on netlist_data and optional def_data dictionaries."""
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

    def _place_graph(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = 94,
        chassis_core_h: int = 94,
        distribute_across_blocks: bool = False,
    ) -> AnalyticalPlacementResult:
        """Execute the 4-stage analytical placement and directed legalization pipeline."""
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
                self._compute_flow_depths_and_columns(nodes, edges)

            for (bx, by), inst_names in block_bins.items():
                blk_nodes = [nodes[name] for name in inst_names]
                mat_nodes = [nd for nd in blk_nodes if nd.is_matrix_node]
                core_nodes = [nd for nd in blk_nodes if not nd.is_matrix_node]

                if mat_nodes:
                    mat_nodes.sort(key=lambda nd: (nd.flow_depth, _natural_key(nd.name)))
                    if len(mat_nodes) <= 6:
                        bay_slots = (
                            (18, 45, 72)
                            if len(mat_nodes) <= 3
                            else (16, 36, 56, 76)
                            if len(mat_nodes) == 4
                            else (14, 28, 42, 56, 70, 84)
                        )
                        for m_idx, nd in enumerate(mat_nodes):
                            cur_mx = bx * chassis_pitch + bay_slots[m_idx % len(bay_slots)]
                            bay_y = by * chassis_pitch + (6 if (m_idx % 2 == 0) else 11)
                            nd.x = cur_mx
                            nd.y = bay_y
                            nd.cont_x = float(cur_mx)
                            nd.cont_y = float(bay_y)
                    else:
                        bay_slots = (12, 24, 36, 48, 60, 72, 84)
                        for m_idx, nd in enumerate(mat_nodes):
                            row_grp = m_idx // 7
                            slot_idx = m_idx % 7
                            cur_mx = bx * chassis_pitch + bay_slots[slot_idx] + (row_grp % 2) * 5
                            bay_y = by * chassis_pitch + 5 + row_grp * 8 + (slot_idx % 2) * 3
                            nd.x = cur_mx
                            nd.y = bay_y
                            nd.cont_x = float(cur_mx)
                            nd.cont_y = float(bay_y)

                if core_nodes:
                    extra_bay_y = max(0, (len(mat_nodes) - 1) // 7) * 8 if len(mat_nodes) > 6 else 0
                    c_min_x = bx * chassis_pitch + margin_x
                    c_min_y = (
                        by * chassis_pitch + margin_y + (14 if gw * gh > 1 else 0) + extra_bay_y
                    )
                    c_max_x = bx * chassis_pitch + margin_x + chassis_core_w
                    c_max_y = by * chassis_pitch + margin_y + chassis_core_h
                    local_cols = sorted({nd.flow_depth for nd in core_nodes})
                    col_remap = {old_c: new_c for new_c, old_c in enumerate(local_cols)}
                    for nd in core_nodes:
                        nd.col_index = col_remap[nd.flow_depth]
                    self._solve_continuous_block(
                        core_nodes,
                        edges,
                        float(c_min_x + 4),
                        float(c_min_y + 4),
                        float(c_max_x - 4),
                        float(c_max_y - 4),
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
        else:
            self._last_rewired_ports = {}
            # Single-core adaptive die sizing
            all_nodes = list(nodes.values())
            for nd in all_nodes:
                nd.assigned_block = (0, 0)
                nd.is_matrix_node = False

            padded_area = sum(
                (nd.width + nd.pad_w + nd.pad_e) * (nd.height + nd.pad_n + nd.pad_s)
                for nd in all_nodes
            )
            by_col_h: dict[int, int] = collections.defaultdict(int)
            for nd in all_nodes:
                v_gap = (
                    max(6, nd.pad_n + nd.pad_s)
                    if nd.node_type == "machine"
                    else max(4, nd.pad_n + nd.pad_s)
                    if nd.node_type in ("split", "merge", "balancer")
                    else 2
                )
                by_col_h[nd.col_index] += nd.height + v_gap
            max_col_h = max(by_col_h.values(), default=48)
            target_h = max(
                48,
                max_col_h + 16,
                min(160, round(math.sqrt(max(400.0, padded_area * 1.3)))),
            )
            if target_h % 2 != 0:
                target_h += 1

            c_min_x = self.core_margin
            c_min_y = self.core_margin
            c_max_y = c_min_y + target_h
            est_w = max(60, round(padded_area * 1.4 / float(max(48, min(160, target_h)))))

            self._solve_continuous_block(
                all_nodes,
                edges,
                float(c_min_x + 4),
                float(c_min_y + 4),
                float(c_min_x + est_w),
                float(c_max_y - 4),
            )
            used_w, _used_h = self._legalize_block(
                all_nodes,
                edges,
                c_min_x,
                c_min_y,
                c_min_x + max(est_w, 400),
                c_max_y,
                fixed_core=False,
            )

            max_placed_x = max((nd.x + nd.width for nd in all_nodes), default=c_min_x + 30)
            max_placed_y = max((nd.y + nd.height for nd in all_nodes), default=c_min_y + 30)
            core_hi_x = max(c_min_x + used_w, max_placed_x + 8)
            core_hi_y = max(c_max_y, max_placed_y + 8)
            if (core_hi_x - c_min_x) % 2 != 0:
                core_hi_x += 1
            if (core_hi_y - c_min_y) % 2 != 0:
                core_hi_y += 1

            die_w = core_hi_x + self.core_margin
            die_h = core_hi_y + self.core_margin
            core_bounds = (c_min_x, c_min_y, core_hi_x, core_hi_y)
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
        dbu: int = 1000,
    ) -> dict[str, tuple[int, int]]:
        """Compute PAV-spread boundary pin DBU coordinates aligned with connected cells."""
        in_targets: dict[str, list[tuple[float, float, float, str]]] = collections.defaultdict(list)
        out_targets: dict[str, list[tuple[float, float, float, str]]] = collections.defaultdict(
            list
        )
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

        pin_coords: dict[str, tuple[int, int]] = {}
        for target_map, x_dbu in ((in_targets, 250), (out_targets, int(die_w * dbu - 250))):
            if not target_map:
                continue
            items = [
                (
                    sum(t[0] for t in ts) / float(len(ts)),
                    sum(t[1] for t in ts) / float(len(ts)),
                    sum(t[2] for t in ts) / float(len(ts)),
                    ts[0][3],
                    pname,
                )
                for pname, ts in target_map.items()
                if ts
            ]
            items.sort(
                key=lambda item: (
                    round(item[1] + 0.005 * item[2], 3),
                    item[3],
                    round(item[0], 2),
                    _natural_key(item[4]),
                )
            )
            n_pins = len(items)
            min_gap = max(4, min(10, (die_h - 24) // max(2, n_pins * 2)))
            gaps = [
                3 if items[i][3] == items[i + 1][3] else min_gap for i in range(max(0, n_pins - 1))
            ]
            y_vals = _solve_1d_pav(
                [item[0] for item in items],
                heights=[1] * n_pins,
                gaps=gaps,
                y_lo=6,
                y_hi=max(14, die_h - 6),
            )
            if use_chassis and grid_h > 1:
                for idx in range(n_pins):
                    yv = y_vals[idx]
                    k = round(float(yv) / float(chassis_pitch))
                    if 1 <= k < grid_h and abs(yv - k * chassis_pitch) <= 5:
                        if items[idx][1] >= float(k * chassis_pitch):
                            y_vals[idx] = k * chassis_pitch + 6
                            for j in range(idx + 1, n_pins):
                                req = y_vals[j - 1] + gaps[j - 1] + 1
                                if y_vals[j] < req:
                                    y_vals[j] = req
                        else:
                            y_vals[idx] = k * chassis_pitch - 6
                            for j in range(idx - 1, -1, -1):
                                req = y_vals[j + 1] - gaps[j] - 1
                                if y_vals[j] > req:
                                    y_vals[j] = req
                if target_map is in_targets:
                    for idx, item in enumerate(items):
                        pname = item[4]
                        phys_sinks = [
                            v
                            for v in in_sink_nodes.get(pname, [])
                            if v.node_type not in ("combinator", "display")
                        ]
                        if phys_sinks and all(
                            not v.is_matrix_node and v.assigned_block[0] == 0 for v in phys_sinks
                        ):
                            by_s = min(v.assigned_block[1] for v in phys_sinks)
                            if by_s * chassis_pitch <= y_vals[idx] < (by_s + 1) * chassis_pitch:
                                prefer_south = by_s + 1 < grid_h and (
                                    by_s == 0
                                    or y_vals[idx] >= by_s * chassis_pitch + chassis_pitch // 2
                                )
                                if prefer_south:
                                    seg_lo = (by_s + 1) * chassis_pitch + 6
                                    seg_hi = (by_s + 2) * chassis_pitch - 6
                                    cand_range = range(seg_lo, seg_hi + 1)
                                elif by_s > 0:
                                    seg_lo = (by_s - 1) * chassis_pitch + 6
                                    seg_hi = by_s * chassis_pitch - 6
                                    cand_range = range(seg_hi, seg_lo - 1, -1)
                                else:
                                    cand_range = range(0)
                                for cand_y in cand_range:
                                    if all(
                                        abs(cand_y - y_vals[j]) >= 4
                                        for j in range(n_pins)
                                        if j != idx
                                    ):
                                        y_vals[idx] = cand_y
                                        break
            for item, yv in zip(items, y_vals, strict=False):
                pin_coords[item[4]] = (x_dbu, int(yv) * dbu + 500)
        return pin_coords

    def _compute_graph_hpwl(
        self,
        nodes: dict[str, PlacerNode],
        edges: list[PlacerEdge],
        die_w: int,
    ) -> float:
        """Compute total Half-Perimeter Wirelength across all nets in the graph."""
        by_wire: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
        for e in edges:
            if e.source in nodes:
                u = nodes[e.source]
                by_wire[e.wire].append((float(u.x + u.width), float(u.y) + e.source_dy))
            elif e.source.startswith("PIN_IN:") and e.sink in nodes:
                v = nodes[e.sink]
                by_wire[e.wire].append((0.0, float(v.y) + e.sink_dy))

            if e.sink in nodes:
                v = nodes[e.sink]
                by_wire[e.wire].append((float(v.x - 1), float(v.y) + e.sink_dy))
            elif e.sink.startswith("PIN_OUT:") and e.source in nodes:
                u = nodes[e.source]
                by_wire[e.wire].append((float(die_w), float(u.y) + e.source_dy))

        total = 0.0
        for pts in by_wire.values():
            if len(pts) >= 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                total += (max(xs) - min(xs)) + (max(ys) - min(ys))
        return round(total, 2)

    def emit_analytical_tcl(
        self,
        nl: Any,
        merge_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        split_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
        balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None = None,
        extra_chassis_grids: list[tuple[int, int]] | None = None,
    ) -> str:
        """Emit TCL dictionaries with analytical coordinates for single-core and chassis modes."""
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

        grids = [(1, 1), (2, 2), (3, 3)]
        if len(getattr(nl, "instances", [])) > 150:
            grids.extend([(4, 4), (5, 5), (6, 6), (7, 7)])
        if extra_chassis_grids:
            for g in extra_chassis_grids:
                if g not in grids:
                    grids.append(g)

        for gw, gh in grids:
            ch_res = self.place_netlist(
                nl,
                merge_plans=merge_plans,
                split_plans=split_plans,
                balancer_plans=balancer_plans,
                use_chassis=True,
                grid_w=gw,
                grid_h=gh,
            )
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

        return "\n".join(lines) + "\n"

    def apply_to_def_data(
        self,
        def_data: dict[str, Any],
        netlist_data: dict[str, Any],
        use_chassis: bool = False,
        grid_w: int = 1,
        grid_h: int = 1,
        chassis_pitch: int = 100,
        chassis_core_w: int = 94,
        chassis_core_h: int = 94,
        distribute_across_blocks: bool = False,
        untangle_ports: bool = True,
    ) -> tuple[dict[str, Any], AnalyticalPlacementResult]:
        """Apply analytical placement to a parsed DEF dictionary and untangle splitter ports."""
        updated = copy.deepcopy(def_data)
        dbu = int(updated.get("units", 1000))

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
                {((grid_w - 1) // 2, (grid_h - 1) // 2)} if (grid_w >= 3 and grid_h >= 3) else None
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
        def_data: dict[str, Any],
        netlist_data: dict[str, Any],
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

        def _connected_y(pin_obj: dict[str, Any]) -> float:
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
                gaps=[3] * max(0, len(sorted_pins) - 1),
                y_lo=6,
                y_hi=max(12, die_h - 6),
            )
            for p_obj, yv in zip(sorted_pins, y_vals, strict=False):
                p_obj["x"] = int(x_edge * dbu)
                p_obj["y"] = int(yv * dbu)


def apply_port_rewiring_to_def(
    def_data: dict[str, Any],
    rewire_map: dict[str, dict[str, str]],
) -> None:
    """Apply splitter and merger port-to-net rewirings to a parsed DEF dictionary."""
    if not rewire_map:
        return
    net_map = {n["name"]: n for n in def_data.get("nets", [])}
    for net_obj in def_data.get("nets", []):
        conns = net_obj.get("connections", [])
        net_obj["connections"] = [
            (iname, port)
            for iname, port in conns
            if not (iname in rewire_map and port in rewire_map[iname])
        ]
    for iname, pmap in rewire_map.items():
        for port, target_net in pmap.items():
            t_net_obj = net_map.get(target_net)
            if t_net_obj is not None:
                pair = (iname, port)
                if pair not in t_net_obj["connections"]:
                    t_net_obj["connections"].append(pair)


def untangle_splitter_merger_ports(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any] | None = None,
) -> None:
    """Swap symmetric splitter outputs (y1/y2) and merger inputs (a/b) to eliminate crossings."""
    scale = 1.0 / float(def_data.get("units", 1000))
    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    pin_map = {p.get("net", p.get("name", "")): p for p in def_data.get("pins", [])}
    cells_map = (netlist_data or {}).get("cells", {})
    inst_map = {i["name"]: i for i in (netlist_data or {}).get("instances", [])}
    nets = def_data.get("nets", [])

    def _inst_height(iname: str) -> float:
        comp = comp_map.get(iname, {})
        macro = inst_map.get(iname, {}).get("cell", comp.get("macro", ""))
        if macro in cells_map:
            return float(cells_map[macro].get("height", 2))
        if any(k in macro for k in ("split", "merge", "balancer")):
            return 2.0
        return 3.0

    def _inst_width(iname: str) -> float:
        comp = comp_map.get(iname, {})
        macro = inst_map.get(iname, {}).get("cell", comp.get("macro", ""))
        if macro in cells_map:
            return float(cells_map[macro].get("width", 1))
        if any(k in macro for k in ("split", "merge", "balancer")):
            return 1.0
        return 3.0

    def _is_output_port(iname: str, port: str) -> bool:
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

    def _net_sink_center(net_obj: dict[str, Any], exclude_inst: str) -> tuple[float, float]:
        xs, ys = [], []
        nname = net_obj.get("name", "")
        for iname, port in net_obj.get("connections", []):
            if iname == exclude_inst:
                continue
            if iname == "PIN":
                p = pin_map.get(nname) or pin_map.get(port)
                if p and p.get("direction") == "OUTPUT":
                    xs.append(float(p["x"]) * scale)
                    ys.append(float(p["y"]) * scale)
            elif iname in comp_map and not _is_output_port(iname, port):
                c = comp_map[iname]
                xs.append(float(c["x"]) * scale + _inst_width(iname) / 2.0)
                ys.append(float(c["y"]) * scale + _inst_height(iname) / 2.0)
        return (
            float(np.mean(xs)) if xs else 0.0,
            float(np.mean(ys)) if ys else 0.0,
        )

    def _net_driver_center(net_obj: dict[str, Any], exclude_inst: str) -> tuple[float, float]:
        xs, ys = [], []
        nname = net_obj.get("name", "")
        for iname, port in net_obj.get("connections", []):
            if iname == exclude_inst:
                continue
            if iname == "PIN":
                p = pin_map.get(nname) or pin_map.get(port)
                if p and p.get("direction") == "INPUT":
                    xs.append(float(p["x"]) * scale)
                    ys.append(float(p["y"]) * scale)
            elif iname in comp_map and _is_output_port(iname, port):
                c = comp_map[iname]
                xs.append(float(c["x"]) * scale + _inst_width(iname) / 2.0)
                ys.append(float(c["y"]) * scale + _inst_height(iname) / 2.0)
        return (
            float(np.mean(xs)) if xs else 0.0,
            float(np.mean(ys)) if ys else 0.0,
        )

    for _pass in range(2):
        # Map (inst_name, port) -> (net_obj, conn_index)
        inst_port_ref: dict[tuple[str, str], tuple[dict[str, Any], int]] = {}
        for net_obj in nets:
            for c_idx, (iname, port) in enumerate(net_obj.get("connections", [])):
                inst_port_ref[(iname, port)] = (net_obj, c_idx)

        for cname, comp in comp_map.items():
            macro = inst_map.get(cname, {}).get("cell", comp.get("macro", ""))
            if "split2" in macro or "balancer2" in macro:
                ref1 = inst_port_ref.get((cname, "y1"))
                ref2 = inst_port_ref.get((cname, "y2"))
                if ref1 and ref2 and ref1[0] is not ref2[0]:
                    net1, idx1 = ref1
                    net2, idx2 = ref2
                    x1, y1 = _net_sink_center(net1, cname)
                    x2, y2 = _net_sink_center(net2, cname)
                    should_swap = (y1 > y2) if abs(y1 - y2) >= 0.5 else (x1 > x2)
                    if should_swap:
                        net1["connections"][idx1] = (cname, "y2")
                        net2["connections"][idx2] = (cname, "y1")
                        inst_port_ref[(cname, "y2")] = (net1, idx1)
                        inst_port_ref[(cname, "y1")] = (net2, idx2)

            if "merge2" in macro or "balancer2" in macro:
                p_a = "a" if (cname, "a") in inst_port_ref else "a1"
                p_b = "b" if (cname, "b") in inst_port_ref else "a2"
                ref_a = inst_port_ref.get((cname, p_a))
                ref_b = inst_port_ref.get((cname, p_b))
                if ref_a and ref_b and ref_a[0] is not ref_b[0]:
                    net_a, idx_a = ref_a
                    net_b, idx_b = ref_b
                    xa, ya = _net_driver_center(net_a, cname)
                    xb, yb = _net_driver_center(net_b, cname)
                    should_swap = (ya > yb) if abs(ya - yb) >= 0.5 else (xa > xb)
                    if should_swap:
                        net_a["connections"][idx_a] = (cname, p_b)
                        net_b["connections"][idx_b] = (cname, p_a)
                        inst_port_ref[(cname, p_b)] = (net_a, idx_a)
                        inst_port_ref[(cname, p_a)] = (net_b, idx_b)


def compute_def_hpwl(
    def_data: dict[str, Any],
    netlist_data: dict[str, Any] | None = None,
) -> float:
    """Compute total Half-Perimeter Wirelength (HPWL) in tiles for a placed DEF dictionary."""
    scale = 1.0 / float(def_data.get("units", 1000))
    comp_map = {c["name"]: c for c in def_data.get("components", [])}
    pin_map = {p.get("net", p.get("name", "")): p for p in def_data.get("pins", [])}
    cells_map = (netlist_data or {}).get("cells", {})
    inst_map = {i["name"]: i for i in (netlist_data or {}).get("instances", [])}

    total_hpwl = 0.0
    for net in def_data.get("nets", []):
        nname = net.get("name", "")
        pts: list[tuple[float, float]] = []
        for iname, port in net.get("connections", []):
            if iname == "PIN":
                p = pin_map.get(nname) or pin_map.get(port)
                if p:
                    pts.append((float(p["x"]) * scale, float(p["y"]) * scale))
            elif iname in comp_map:
                c = comp_map[iname]
                macro = inst_map.get(iname, {}).get("cell", c.get("macro", ""))
                cmeta = cells_map.get(macro, {})
                w = float(cmeta.get("width", 1 if "split" in macro or "merge" in macro else 3))
                h = float(cmeta.get("height", 2 if "split" in macro or "merge" in macro else 3))
                pts.append((float(c["x"]) * scale + w / 2.0, float(c["y"]) * scale + h / 2.0))
        if len(pts) >= 2:
            xs = [pt[0] for pt in pts]
            ys = [pt[1] for pt in pts]
            total_hpwl += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return round(total_hpwl, 2)
