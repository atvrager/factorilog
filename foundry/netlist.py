"""Netlister: the solver plan as structural Verilog.

The LP yields rates. A netlist needs instances, pins, and nets:

-  One cell module per (building, recipe) pair that the plan uses.
-  One instance per machine, `ceil` of the fractional plan count. The
    plan count is fractional; silicon instances are not. Surplus capacity
    shows up as a starved instance, never as a wrong rate.
-  One wire per material. Multiple producers of one material need explicit
    merge cells: Verilog forbids two drivers on one wire, and in the game
    that merge is a splitter joining belt lanes.
-  Raw materials are top-level inputs. Demanded items are outputs.

Belt lane counts are not in the Verilog. They live in the JSON sidecar,
because lane packing is a place-and-route decision.
"""

from __future__ import annotations

import collections
import math
import re
from typing import Any

import pdk
import solve

# The transport cell that joins two streams of one material. A splitter
# run backwards. The PDK carries it next to the recipe cells.
MERGE_CELL = "belt_merge2"
SPLIT_CELL = "belt_split2"
BALANCER_CELL = "belt_balancer2"
PUMP_CELL = "pump"


def balancer_cell() -> dict[str, Any]:
    """The transport balancer cell as a PDK cell record.

    The splitter footprint is 1.8 x 0.8 tiles in the dump. The record
    snaps it to the tile grid, 1 x 2. Two inputs, two outputs, and no
    crafting rate.
    """
    return {
        "cell": BALANCER_CELL,
        "machine": "splitter",
        "recipe": "balancer2",
        "width": 1,
        "height": 2,
        "watts": 0.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a1", "a2"],
        "outputs": ["y1", "y2"],
    }


def split_cell() -> dict[str, Any]:
    """The split cell as a PDK cell record.

    The splitter footprint is 1.8 x 0.8 tiles in the dump; the record
    snaps it to the tile grid, 1 x 2. One input, two outputs, and no
    crafting rate.
    """
    return {
        "cell": SPLIT_CELL,
        "machine": "splitter",
        "recipe": "split2",
        "width": 1,
        "height": 2,
        "watts": 0.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a"],
        "outputs": ["y1", "y2"],
    }


def merge_cell() -> dict[str, Any]:
    """The merge cell as a PDK cell record.

    Uses a 3x3 footprint containing a 1x2 splitter and symmetric opposing
    side-load belts so both output lanes are balanced 50/50.
    """
    return {
        "cell": MERGE_CELL,
        "machine": "splitter",
        "recipe": "merge2",
        "width": 3,
        "height": 3,
        "watts": 0.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a", "b"],
        "outputs": ["y"],
    }


def pump_cell() -> dict[str, Any]:
    """The fluid buffer cell as a PDK cell record.

    The pump footprint is 1x2 tiles in the dump. One fluid input port,
    one fluid output port, and 29kW power draw.
    """
    return {
        "cell": PUMP_CELL,
        "machine": "pump",
        "recipe": "pump",
        "width": 1,
        "height": 2,
        "watts": 29000.0,
        "crafts_per_minute": 0.0,
        "inputs": ["a"],
        "outputs": ["y"],
    }


def sanitize(name: str) -> str:
    """Item or machine name -> Verilog identifier."""
    return name.replace("-", "_").replace(".", "__")


class Instance:
    """One placed machine, one cell."""

    def __init__(
        self,
        name: str,
        cell_key: str,
        recipe: str,
        plan_rate: float,
        rated: float,
        rank: int = 0,
        control_behavior: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.cell_key = cell_key
        self.recipe = recipe
        self.plan_rate = plan_rate
        self.rated = rated
        self.rank = rank
        self.control_behavior = control_behavior


class Net:
    """One material, its drivers, and its sinks."""

    def __init__(self, material: str, is_fluid: bool = False) -> None:
        self.material = material
        self.is_fluid = is_fluid
        self.drivers: list[str] = []  # instance or merge output names
        self.sinks: list[str] = []
        self.rate_per_min = 0.0
        self.bus_inputs: list[str] = []
        self.sub_buses: list[str] = []
        self.orig_drivers: list[str] = []
        self.driver_rates: dict[str, float] = {}
        self.driver_rated: dict[str, float] = {}
        self.sink_rates: dict[str, float] = {}
        self.sink_rated: dict[str, float] = {}


class Netlist:
    """Instances, nets, and the cell table of one plan."""

    def __init__(self, label: str, fluids: set[str] | None = None) -> None:
        self.label = label
        self.fluids: set[str] = set(fluids) if fluids else set()
        self.cells: dict[str, dict[str, Any]] = {}
        self.instances: list[Instance] = []
        self.nets: dict[str, Net] = {}
        self.inputs: list[str] = []
        self.outputs: list[str] = []
        self.balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] = {}
        self.merge_plans: dict[str, list[tuple[str, str, str, str]]] = {}
        self.material_depths: dict[str, int] = {}
        self.belt_capacity: float = 0.0

    def net(self, material: str) -> Net:
        if material not in self.nets:
            self.nets[material] = Net(material, is_fluid=(material in self.fluids))
        return self.nets[material]


def stage_rank_of(
    material_or_recipe: str,
    inputs: list[str] | None = None,
    material_depths: dict[str, int] | None = None,
) -> int:
    """Classify instances into structured linear row placement bands based on DAG depth."""
    if material_depths:
        d = material_depths.get(material_or_recipe)
        if d is None and inputs:
            d = max((material_depths.get(p, 0) for p in inputs), default=0) + 1
        if d is not None:
            return d
    return 0


def assign_gaa_pin_faces(
    inputs: list[str],
    feeder_coords: dict[str, tuple[float, float]] | None = None,
    mach_y: float = 0.0,
    depths: dict[str, int] | None = None,
) -> dict[str, str]:
    """Assign input ports to West, North, or South faces based on driver direction."""
    face_map: dict[str, str] = {}
    if feeder_coords:
        for p in inputs:
            drv_y = feeder_coords.get(p, (0.0, mach_y))[1]
            if drv_y > mach_y + 1.0:
                face_map[p] = "N"
            elif drv_y < mach_y - 1.0:
                face_map[p] = "S"
            else:
                face_map[p] = "W"
        return face_map

    faces_order = ["W", "N", "S"]
    sorted_inputs = sorted(inputs, key=lambda p: (depths.get(p, 0) if depths else 0, p))
    for idx, p in enumerate(sorted_inputs):
        face_map[p] = faces_order[idx % len(faces_order)]
    return face_map


def build(
    data: solve.Data,
    demand: dict[str, float],
    allowed: set[str],
    machine_override: str | dict[str, str] | None = None,
    enable_compound_cells: bool = False,  # noqa: ARG001
) -> Netlist:
    """Solve the plan, then lower it to instances and nets."""
    tech = pdk.Pdk(data)
    rates, raw, _surplus = solve.solve_rates(data, demand, allowed)
    lines, _power = solve.machine_counts(data, rates)
    plan_count = {line[0]: line[3] for line in lines}

    nl = Netlist("factory", fluids=set(data.prototypes.get("fluid", {}).keys()))
    belt_tiers = tech.belt_tiers()
    if belt_tiers:
        nl.belt_capacity = (
            float(belt_tiers[0]["items_per_lane"]) * pdk.LANES_PER_BELT * pdk.TICKS_PER_SECOND
        )
    for recipe, rate in sorted(rates.items()):
        if isinstance(machine_override, dict):
            machine = machine_override.get(recipe, "assembling-machine-3")
        elif machine_override:
            machine = machine_override
        else:
            recipe_proto = data.recipes[recipe]
            machine, _speed = data.machine_for(recipe_proto)

        recipe_proto = data.recipes[recipe]
        cell = tech.cell(machine, recipe)

        if cell is None:
            raise RuntimeError(f"no cell for {machine} running {recipe}")
        nl.cells[cell["cell"]] = cell
        is_single_pack = len(demand) <= 1 and "space-science-pack" not in demand
        is_full_chassis = len(demand) >= 6 or "space-science-pack" in demand
        scale_for_throughput = is_single_pack or is_full_chassis
        eff_cpm = (
            float(cell.get("effective_crafts_per_minute", cell["crafts_per_minute"]))
            if scale_for_throughput
            else float(cell["crafts_per_minute"])
        )
        count = (
            max(1, math.ceil(rate / eff_cpm - 1e-9))
            if (scale_for_throughput and eff_cpm > 0)
            else max(1, math.ceil(plan_count.get(recipe, 1.0)))
        )
        if (
            is_single_pack
            and eff_cpm > 0
            and cell.get("has_fluids")
            and len(cell.get("outputs", [])) > 1
            and rate / (count * eff_cpm) > 0.90
        ):
            count += 1
        if (
            is_single_pack
            and eff_cpm > 0
            and nl.belt_capacity > 0
            and len(cell.get("outputs", [])) == 1
            and recipe_proto
        ):
            out_name = cell["outputs"][0]
            res_entry = next(
                (e for e in solve.Data.results_of(recipe_proto) if solve.name_of(e) == out_name),
                None,
            )
            out_amt = solve.amount_of(res_entry) if res_entry else 1.0
            drv_cap = eff_cpm * out_amt
            if rate * out_amt > nl.belt_capacity and drv_cap > 0:
                max_drvs_bus = max(1, int((0.90 * nl.belt_capacity) // drv_cap))
                b_max_cap = min(nl.belt_capacity, max_drvs_bus * drv_cap)
                c_totals: list[float] = []
                for c_rec, c_rate in rates.items():
                    c_proto = data.recipes.get(c_rec)
                    if c_proto:
                        c_totals.extend(
                            c_rate * solve.amount_of(ing)
                            for ing in c_proto.get("ingredients", [])
                            if solve.name_of(ing) == out_name
                        )
                if len(c_totals) > 1:
                    whole_d = sum(d for d in c_totals if d <= b_max_cap + 1e-6)
                    split_ds = [d for d in c_totals if d > b_max_cap + 1e-6]
                    if 0.50 * b_max_cap <= whole_d <= b_max_cap + 1e-6 and len(split_ds) == 1:
                        k_split = math.ceil(split_ds[0] / b_max_cap - 1e-9)
                        min_part_drvs = math.ceil(whole_d / drv_cap - 1e-9) + math.ceil(
                            split_ds[0] / drv_cap - 1e-9
                        )
                        if k_split >= 2 and min_part_drvs > count:
                            count = min_part_drvs
        if is_single_pack and count > 2 and (count & (count - 1)) != 0 and eff_cpm > 0:
            d_tree = int(math.log2(count))
            next_pow2 = 1 << (d_tree + 1)
            if eff_cpm < rate * (0.5**d_tree) - 1e-6:
                item_amts = [
                    solve.amount_of(e)
                    for e in (recipe_proto.get("ingredients", []) if recipe_proto else [])
                    if solve.name_of(e) not in nl.fluids
                ]
                if item_amts and (
                    nl.belt_capacity <= 0 or rate * max(item_amts) <= nl.belt_capacity + 1e-6
                ):
                    excess_item_rate = min(item_amts) * (rate * (0.5**d_tree) - eff_cpm)
                    belt_ips = (
                        (nl.belt_capacity / pdk.TICKS_PER_SECOND) if nl.belt_capacity > 0 else 15.0
                    )
                    if 0.0 < excess_item_rate <= belt_ips / 3.0 + 1e-6:
                        count = next_pow2
        # Each instance of the fleet takes an equal share of the recipe.
        share = rate / count
        rated_crafts = eff_cpm
        for index in range(count):
            inst = Instance(
                f"u_{sanitize(recipe)}_{index + 1}",
                cell["cell"],
                recipe,
                share,
                rated_crafts,
            )
            nl.instances.append(inst)
            for inp in cell.get("inputs", []):
                in_amt = 1.0
                if recipe_proto:
                    in_entry = next(
                        (e for e in recipe_proto.get("ingredients", []) if solve.name_of(e) == inp),
                        None,
                    )
                    if in_entry:
                        in_amt = solve.amount_of(in_entry)
                in_net = nl.net(inp)
                in_net.sinks.append(inst.name)
                in_net.sink_rates[inst.name] = share * in_amt
                in_net.sink_rated[inst.name] = rated_crafts * in_amt
            for outp in cell.get("outputs", []):
                amt = 1.0
                if recipe_proto:
                    res_entry = next(
                        (
                            e
                            for e in solve.Data.results_of(recipe_proto)
                            if solve.name_of(e) == outp
                        ),
                        None,
                    )
                    if res_entry:
                        amt = solve.amount_of(res_entry)
                out_net = nl.net(outp)
                out_net.rate_per_min += share * amt
                out_net.drivers.append(inst.name)
                out_net.driver_rates[inst.name] = share * amt
                out_net.driver_rated[inst.name] = rated_crafts * amt

    for material, rate in raw.items():
        net = nl.net(material)
        net.rate_per_min = rate
        is_fluid = net.is_fluid or material in nl.fluids
        # Divide high-throughput input lines into power-of-two buses.
        if not is_fluid and nl.belt_capacity > 0 and rate > nl.belt_capacity:
            num_belts = 2 ** math.ceil(math.log2(math.ceil(rate / nl.belt_capacity)))
            net.bus_inputs = [f"{material}__bus{i}" for i in range(num_belts)]
            for bus_name in net.bus_inputs:
                nl.net(bus_name).rate_per_min = rate / num_belts

    nl.inputs = []
    for m in sorted(raw):
        net = nl.net(m)
        if net.bus_inputs:
            nl.inputs.extend(net.bus_inputs)
        else:
            nl.inputs.append(m)
    nl.outputs = sorted(
        m for m in demand if m in nl.nets and nl.nets[m].drivers and not nl.net(m).sinks
    )
    # Some demanded items feed other recipes. They must still leave the
    # block, so an output is any demand item with a driver.
    # Order input ports by reverse recipe DAG depth (higher depth intermediates before lower depth base inputs)
    depths: dict[str, int] = dict.fromkeys(raw, 0)
    changed = True
    while changed:
        changed = False
        for cell in nl.cells.values():
            in_depth = max((depths.get(p, 0) for p in cell.get("inputs", [])), default=0)
            for out_p in cell.get("outputs", []):
                new_d = in_depth + 1
                if new_d > depths.get(out_p, 0):
                    depths[out_p] = new_d
                    changed = True

    for cell in nl.cells.values():
        cell["inputs"] = sorted(cell["inputs"], key=lambda p: (depths.get(p, 0), p))
        inps = cell.get("inputs", [])
        if (
            inps
            and not cell.get("compound")
            and not cell.get("circuit_cell")
            and not cell.get("has_fluids")
            and not cell.get("landing_pad")
        ):
            faces = assign_gaa_pin_faces(inps, depths=depths)
            cell["port_faces"] = faces
            if any(f in ("N", "S") for f in faces.values()):
                m_w = int(cell.get("machine_width", cell.get("width", 3)))
                m_h = int(cell.get("machine_height", cell.get("height", 3)))
                cell["width"] = m_w + 2
                cell["height"] = m_h + 2
                cell["in_slots"] = {}
                w_inps = [p for p in inps if faces.get(p) == "W"]
                offset_in = 1 + (m_h - len(w_inps)) // 2 if len(w_inps) <= m_h else 1
                for idx, p in enumerate(w_inps):
                    cell["in_slots"][p] = offset_in + idx
                for face_dir in ("N", "S"):
                    f_inps = [p for p in inps if faces.get(p) == face_dir]
                    if len(f_inps) > 1:
                        offset_x = 1 + (m_w - len(f_inps)) // 2 if len(f_inps) <= m_w else 1
                        for idx, p in enumerate(f_inps):
                            if len(f_inps) == 2 and m_w >= 3:
                                cell["in_slots"][p] = 1 + 2 * idx
                            else:
                                cell["in_slots"][p] = offset_x + idx
                outs = cell.get("outputs", [])
                offset_out = 1 + (m_h - len(outs)) // 2 if len(outs) <= m_h else 1
                for idx, p in enumerate(outs):
                    cell.setdefault("out_slots", {})[p] = offset_out + idx
    nl.material_depths = depths
    instances_by_depth: dict[int, list[Instance]] = collections.defaultdict(list)
    for inst in nl.instances:
        c = nl.cells.get(inst.cell_key, {})
        d = min((depths.get(p, 0) for p in c.get("outputs", [])), default=0)
        if d == 0:
            d = max((depths.get(p, 0) for p in c.get("inputs", [])), default=0)
        instances_by_depth[d].append(inst)

    current_rank = 0
    max_machines_per_row = 5

    def _assign_recipe_chunk_ranks(inst_list: list[Instance], sort_recipes: bool = True) -> None:
        nonlocal current_rank
        by_recipe: dict[str, list[Instance]] = collections.defaultdict(list)
        for inst in inst_list:
            by_recipe[inst.recipe].append(inst)
        recipe_keys = sorted(by_recipe.keys()) if sort_recipes else list(by_recipe.keys())
        for r_name in recipe_keys:
            r_insts = by_recipe[r_name]
            for chunk_start in range(0, len(r_insts), max_machines_per_row):
                for inst in r_insts[chunk_start : chunk_start + max_machines_per_row]:
                    inst.rank = current_rank
                current_rank += 1

    for d in sorted(instances_by_depth.keys()):
        if d <= 2:
            _assign_recipe_chunk_ranks(instances_by_depth[d], sort_recipes=True)

    higher_term: list[Instance] = []
    for d in sorted(instances_by_depth.keys()):
        if d >= 3:
            non_term_at_d: list[Instance] = []
            for inst in instances_by_depth[d]:
                c = nl.cells.get(inst.cell_key, {})
                if any(outp in demand for outp in c.get("outputs", [])):
                    higher_term.append(inst)
                else:
                    non_term_at_d.append(inst)
            if non_term_at_d:
                _assign_recipe_chunk_ranks(non_term_at_d, sort_recipes=True)
    if higher_term:
        _assign_recipe_chunk_ranks(higher_term, sort_recipes=False)
    return nl


def merge_net(nl: Netlist, net: Net) -> list[tuple[str, str, str, str]]:
    """Replace the drivers of one net with a merge tree.

    Two or more producers on one material become `belt_merge2` instances.
    Each driver output gets a fresh wire. Each merge output gets a fresh
    wire, except the last, which drives the material wire itself. Returns
    entries of (merge name, a wire, b wire, y wire), empty for one driver.
    """
    if net.is_fluid or net.material in nl.fluids or len(net.drivers) <= 1:
        return []
    if not net.orig_drivers:
        net.orig_drivers = list(net.drivers)
    material = sanitize(net.material)
    entries: list[tuple[str, str, str, str]] = []
    count = 0
    inst_by_name = {inst.name: inst for inst in nl.instances}

    def _build_planar_merge_tree(items: list[str], default_rank: int, is_root: bool = False) -> str:
        nonlocal count
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            count += 1
            name = f"u_merge_{material}_{count}"
            a_wire, b_wire = items[0], items[1]
            y_wire = material if is_root else f"{material}__m{count}"
            entries.append((name, a_wire, b_wire, y_wire))
            nl.instances.append(
                Instance(name, MERGE_CELL, net.material, 0.0, 0.0, rank=default_rank)
            )
            return y_wire

        mid = (len(items) + 1) // 2
        left_out = _build_planar_merge_tree(items[:mid], default_rank, is_root=False)
        right_out = _build_planar_merge_tree(items[mid:], default_rank, is_root=False)

        count += 1
        name = f"u_merge_{material}_{count}"
        y_wire = material if is_root else f"{material}__m{count}"
        entries.append((name, left_out, right_out, y_wire))
        nl.instances.append(Instance(name, MERGE_CELL, net.material, 0.0, 0.0, rank=default_rank))
        return y_wire

    drivers_by_rank: dict[int, list[str]] = collections.defaultdict(list)
    for d in net.drivers:
        inst = inst_by_name.get(d)
        r = inst.rank if inst else 0
        drivers_by_rank[r].append(f"{material}__{d}")

    sorted_ranks = sorted(drivers_by_rank.keys())
    if len(sorted_ranks) == 1:
        _build_planar_merge_tree(drivers_by_rank[sorted_ranks[0]], sorted_ranks[0], is_root=True)
    else:
        rank_roots: list[str] = []
        for r in sorted_ranks:
            sub_root = _build_planar_merge_tree(drivers_by_rank[r], r, is_root=False)
            rank_roots.append(sub_root)
        top_rank = max(sorted_ranks)
        _build_planar_merge_tree(rank_roots, top_rank, is_root=True)

    final_merger = f"u_merge_{material}_{count}"
    net.drivers = [final_merger]
    return entries


def _balance_inputs(
    nl: Netlist,
    material: str,
    raw_material: str,
    bus_wires: list[str],
    bal_entries: list[tuple[str, str, str, str, str]],
    bal_count: list[int],
) -> list[str]:
    """Balance power-of-two input bus wires with belt_balancer2 cells."""
    n = len(bus_wires)
    if n <= 1:
        return bus_wires
    s1: list[str] = []
    for i in range(0, n, 2):
        if i + 1 < n:
            bal_count[0] += 1
            name = f"u_bal_{material}_{bal_count[0]}"
            y1 = f"{material}__bal_{bal_count[0]}_1"
            y2 = f"{material}__bal_{bal_count[0]}_2"
            bal_entries.append((name, bus_wires[i], bus_wires[i + 1], y1, y2))
            nl.instances.append(Instance(name, BALANCER_CELL, raw_material, 0.0, 0.0, rank=0))
            s1.extend([y1, y2])
        else:
            s1.append(bus_wires[i])
    return s1


def _partition_sinks_by_demand(
    sinks: list[str],
    sink_rates: dict[str, float],
    k: int,
    bus_max_cap: float | None = None,
    inst_by_name: dict[str, Instance] | None = None,
) -> list[list[str]]:
    """Partition an ordered list of sinks into k groups balanced by demand."""
    if k <= 1 or len(sinks) <= 1:
        return [list(sinks)]
    k = min(k, len(sinks))

    if inst_by_name is not None:
        by_recipe: dict[str, list[str]] = collections.defaultdict(list)
        first_idx: dict[str, int] = {}
        for idx, s in enumerate(sinks):
            rec = inst_by_name[s].recipe if s in inst_by_name else ""
            if rec not in first_idx:
                first_idx[rec] = idx
            by_recipe[rec].append(s)
        if len(by_recipe) > 1:
            groups: list[list[str]] = [[] for _ in range(k)]
            bus_loads = [0.0] * k
            recipe_totals = {
                r: sum(max(1e-6, sink_rates.get(s, 1.0)) for s in r_sinks)
                for r, r_sinks in by_recipe.items()
            }
            if len(by_recipe) >= k and (
                bus_max_cap is None or max(recipe_totals.values()) <= bus_max_cap + 1e-6
            ):
                block_recipes = sorted(
                    by_recipe.keys(),
                    key=lambda r: (-round(recipe_totals[r], 6), first_idx[r]),
                )
                for rec in block_recipes:
                    r_sinks = by_recipe[rec]
                    group_w = recipe_totals[rec]
                    best_b = min(range(k), key=lambda b: (round(bus_loads[b], 6), b))
                    groups[best_b].extend(r_sinks)
                    bus_loads[best_b] += group_w
                inst_map = inst_by_name
                groups = [g for g in groups if g]
                groups.sort(
                    key=lambda g: (
                        min(
                            (inst_map[s].rank for s in g if s in inst_map),
                            default=0,
                        ),
                        _natural_key(g[0]),
                    )
                )
                return groups

            if bus_max_cap is not None:
                whole_recipes = [r for r in by_recipe if recipe_totals[r] <= bus_max_cap + 1e-6]
                split_recipes = [r for r in by_recipe if recipe_totals[r] > bus_max_cap + 1e-6]
                if (
                    whole_recipes
                    and len(split_recipes) == 1
                    and sum(recipe_totals[r] for r in whole_recipes) >= 0.50 * bus_max_cap
                ):
                    whole_sorted = sorted(
                        whole_recipes,
                        key=lambda r: (first_idx[r], -round(recipe_totals[r], 6)),
                    )
                    for rec in whole_sorted:
                        r_sinks = by_recipe[rec]
                        group_w = recipe_totals[rec]
                        placed = False
                        for b in range(k - 1):
                            if groups[b] and bus_loads[b] + group_w <= bus_max_cap + 1e-6:
                                groups[b].extend(r_sinks)
                                bus_loads[b] += group_w
                                placed = True
                                break
                        if not placed:
                            for b in range(k - 1):
                                if not groups[b] and group_w <= bus_max_cap + 1e-6:
                                    groups[b].extend(r_sinks)
                                    bus_loads[b] += group_w
                                    placed = True
                                    break
                            if not placed:
                                best_b = min(range(k), key=lambda b: (round(bus_loads[b], 6), b))
                                groups[best_b].extend(r_sinks)
                                bus_loads[best_b] += group_w
                    empty_buses = [b for b in range(k) if not groups[b]]
                    s_rec = split_recipes[0]
                    s_sinks = by_recipe[s_rec]
                    s_total = recipe_totals[s_rec]
                    if (
                        empty_buses
                        and len(s_sinks) >= len(empty_buses)
                        and s_total / len(empty_buses) <= bus_max_cap + 1e-6
                    ):
                        s_idx = 0
                        n_s = len(s_sinks)
                        for b_pos, b in enumerate(empty_buses):
                            rem_b = len(empty_buses) - b_pos
                            rem_s = n_s - s_idx
                            chunk = (rem_s + rem_b - 1) // rem_b
                            groups[b].extend(s_sinks[s_idx : s_idx + chunk])
                            bus_loads[b] += sum(
                                max(1e-6, sink_rates.get(s, 1.0))
                                for s in s_sinks[s_idx : s_idx + chunk]
                            )
                            s_idx += chunk
                        return [g for g in groups if g]
                    groups = [[] for _ in range(k)]
                    bus_loads = [0.0] * k

            sorted_recipes = sorted(
                by_recipe.keys(),
                key=lambda r: (
                    -round(max(sink_rates.get(s, 1.0) for s in by_recipe[r]), 6),
                    -round(recipe_totals[r], 6),
                    first_idx[r],
                ),
            )
            for rec in sorted_recipes:
                r_sinks = by_recipe[rec]
                r_weights = [max(1e-6, sink_rates.get(s, 1.0)) for s in r_sinks]
                group_w = sum(r_weights)
                target_after = (sum(bus_loads) + group_w) / k
                if len(r_sinks) <= k:
                    bus_order = sorted(range(k), key=lambda b: (round(bus_loads[b], 6), b))
                    for s, w, b in zip(r_sinks, r_weights, bus_order, strict=False):
                        groups[b].append(s)
                        bus_loads[b] += w
                elif len(r_sinks) <= 2 * k and max(r_weights) > target_after / 3.0:
                    for s, w in zip(r_sinks, r_weights, strict=True):
                        best_b = min(range(k), key=lambda b: (round(bus_loads[b], 6), b))
                        groups[best_b].append(s)
                        bus_loads[best_b] += w
                else:
                    needs = [max(0.0, target_after - bus_loads[b]) for b in range(k)]
                    sum_needs = sum(needs)
                    quotas = (
                        [group_w * (nd / sum_needs) for nd in needs]
                        if sum_needs > 1e-6
                        else [group_w / k] * k
                    )
                    cur_bus = 0
                    cur_added = 0.0
                    n_r = len(r_sinks)
                    for idx, (s, w) in enumerate(zip(r_sinks, r_weights, strict=True)):
                        rem_sinks = n_r - idx
                        while cur_bus < k - 1 and (cur_added > 0 or groups[cur_bus]):
                            empty_after = sum(1 for b in range(cur_bus + 1, k) if not groups[b])
                            if (
                                rem_sinks <= empty_after
                                or (
                                    bus_max_cap is not None
                                    and bus_loads[cur_bus] + w > bus_max_cap + 1e-6
                                )
                                or (
                                    cur_added + w > quotas[cur_bus]
                                    and abs((cur_added + w) - quotas[cur_bus])
                                    > abs(cur_added - quotas[cur_bus])
                                )
                            ):
                                cur_bus += 1
                                cur_added = 0.0
                            else:
                                break
                        groups[cur_bus].append(s)
                        bus_loads[cur_bus] += w
                        cur_added += w
            return [g for g in groups if g]

    weights = [max(1e-6, sink_rates.get(s, 1.0)) for s in sinks]
    total_w = sum(weights)
    target_w = total_w / k

    groups = [[] for _ in range(k)]
    cur_bus = 0
    cur_w = 0.0
    n_sinks = len(sinks)
    for idx, (s, w) in enumerate(zip(sinks, weights, strict=True)):
        rem_sinks = n_sinks - idx
        rem_buses = k - 1 - cur_bus
        if (
            cur_bus < k - 1
            and groups[cur_bus]
            and (
                rem_sinks <= rem_buses
                or (bus_max_cap is not None and cur_w + w > bus_max_cap + 1e-6)
                or (cur_w + w > target_w and abs((cur_w + w) - target_w) > abs(cur_w - target_w))
            )
        ):
            cur_bus += 1
            cur_w = 0.0
        groups[cur_bus].append(s)
        cur_w += w
    return [g for g in groups if g]


def split_net(
    nl: Netlist,
    net: Net,
    bal_entries: list[tuple[str, str, str, str, str]] | None = None,
    synthesize_trees: bool = True,
) -> list[tuple[str, str, str, str]]:
    """Replace the sinks of one net with a split tree or multi-drop bus.

    When synthesize_trees is True, consumers become `belt_split2` instances.
    When synthesize_trees is False, consumers share a multi-drop belt bus.
    """
    if net.is_fluid or net.material in nl.fluids or len(net.sinks) <= 1:
        return []
    material = sanitize(net.material)
    entries: list[tuple[str, str, str, str]] = []
    count = [0]
    bal_count = [0]
    inst_by_name = {inst.name: inst for inst in nl.instances}
    sink_ranks = [inst_by_name[s].rank for s in net.sinks if s in inst_by_name]
    driver_ranks = [inst_by_name[d].rank for d in net.drivers if d in inst_by_name]
    is_primary = (net.material in nl.inputs) or (
        sink_ranks and driver_ranks and min(driver_ranks) < min(sink_ranks)
    )
    if is_primary and sink_ranks and min(sink_ranks) > 0:
        rank = min(sink_ranks)
    elif driver_ranks:
        rank = min(driver_ranks)
    elif sink_ranks:
        rank = min(sink_ranks)
    else:
        rank = 0

    wire_target_rank: dict[str, int] = {}
    wire_weight: dict[str, float] = {}
    for s in net.sinks:
        w_s = max(1e-6, net.sink_rates.get(s, 1.0))
        wire_weight[s] = w_s
        wire_weight[f"{material}__{s}"] = w_s
        if s in inst_by_name:
            wire_target_rank[s] = inst_by_name[s].rank
            wire_target_rank[f"{material}__{s}"] = inst_by_name[s].rank

    def _sink_rank(w: str, default: int) -> int:
        if w in wire_target_rank:
            return wire_target_rank[w]
        m = re.search(r"__rf_(\d+)_", w)
        if m:
            return int(m.group(1))
        return default

    def _sink_weight(w: str) -> float:
        return max(1e-6, wire_weight.get(w, 1.0))

    is_single_pack = len(nl.outputs) <= 1 and "space-science-pack" not in nl.outputs

    def _build_tree(
        in_wire: str,
        sinks: list[str],
        tree_rank: int | None = None,
        unbacked_flow: float | None = None,
    ) -> None:
        if not synthesize_trees or len(sinks) <= 1:
            return
        count[0] += 1
        name = f"u_split_{material}_{count[0]}"
        r = rank if tree_rank is None else tree_rank
        if len(sinks) == 2:
            r0 = _sink_rank(sinks[0], r)
            r1 = _sink_rank(sinks[1], r)
            if r <= min(r0, r1) and r0 < r1:
                y1_wire, y2_wire = sinks[1], sinks[0]
            else:
                y1_wire, y2_wire = sinks[0], sinks[1]
            entries.append((name, in_wire, y1_wire, y2_wire))
            nl.instances.append(Instance(name, SPLIT_CELL, net.material, 0.0, 0.0, rank=r))
            return

        weights = [_sink_weight(w) for w in sinks]
        total_w = sum(weights)
        if not is_single_pack:
            mid = len(sinks) // 2
        else:
            eff_flow = min(total_w, unbacked_flow) if unbacked_flow is not None else total_w
            if weights[0] > eff_flow / 4.0 + 1e-6 and (
                weights[0] > weights[1] + 1e-6
                or (
                    unbacked_flow is None
                    and len(sinks) >= 6
                    and weights[0] >= 4.0 * weights[-1] - 1e-6
                )
            ):
                best_mid = 1
            elif weights[-1] > eff_flow / 4.0 + 1e-6 and (
                weights[-1] > weights[-2] + 1e-6
                or (
                    unbacked_flow is None
                    and len(sinks) >= 6
                    and weights[-1] >= 4.0 * weights[0] - 1e-6
                )
            ):
                best_mid = len(sinks) - 1
            else:
                best_mid = len(sinks) // 2
                best_key: tuple[float, int, int] | None = None
                acc_w = 0.0
                for idx in range(1, len(sinks)):
                    acc_w += weights[idx - 1]
                    diff = round(abs(acc_w - (total_w - acc_w)), 6)
                    rank_boundary = (
                        0 if _sink_rank(sinks[idx - 1], r) != _sink_rank(sinks[idx], r) else 1
                    )
                    cand_key = (diff, rank_boundary, abs(idx - len(sinks) // 2))
                    if best_key is None or cand_key < best_key:
                        best_key = cand_key
                        best_mid = idx
            mid = best_mid
        left = sinks[:mid]
        right = sinks[mid:]
        r_left = min((_sink_rank(w, r) for w in left), default=r)
        r_right = min((_sink_rank(w, r) for w in right), default=r)
        is_trunk = r <= min(r_left, r_right) and r_left < r_right

        if not is_single_pack:
            left_rank = r_left if r_left == max((_sink_rank(w, r) for w in left), default=r) else r
            right_rank = (
                r_right if r_right == max((_sink_rank(w, r) for w in right), default=r) else r
            )
        else:
            left_rank = r_left
            right_rank = r_right

        w_left = left[0] if len(left) == 1 else f"{material}__sp_{count[0]}_1"
        w_right = right[0] if len(right) == 1 else f"{material}__sp_{count[0]}_2"
        wire_target_rank[w_left] = r_left
        wire_target_rank[w_right] = r_right
        wire_weight[w_left] = sum(_sink_weight(w) for w in left)
        wire_weight[w_right] = sum(_sink_weight(w) for w in right)
        child_flow = (unbacked_flow / 2.0) if unbacked_flow is not None else (total_w / 2.0)

        if is_trunk:
            y1_wire = w_right
            y2_wire = w_left
            entries.append((name, in_wire, y1_wire, y2_wire))
            nl.instances.append(Instance(name, SPLIT_CELL, net.material, 0.0, 0.0, rank=r))
            if len(right) > 1:
                _build_tree(y1_wire, right, tree_rank=right_rank, unbacked_flow=child_flow)
            if len(left) > 1:
                _build_tree(y2_wire, left, tree_rank=left_rank, unbacked_flow=child_flow)
        else:
            y1_wire = w_left
            y2_wire = w_right
            entries.append((name, in_wire, y1_wire, y2_wire))
            nl.instances.append(Instance(name, SPLIT_CELL, net.material, 0.0, 0.0, rank=r))
            if len(left) > 1:
                _build_tree(y1_wire, left, tree_rank=left_rank, unbacked_flow=child_flow)
            if len(right) > 1:
                _build_tree(y2_wire, right, tree_rank=right_rank, unbacked_flow=child_flow)

    def _build_rank_grouped_tree(
        in_wire: str, sink_names: list[str], bus_supply: float | None = None
    ) -> None:
        if not is_single_pack:
            ordered_sinks = sorted(
                sink_names,
                key=lambda s: (
                    inst_by_name[s].rank if s in inst_by_name else rank,
                    _natural_key(s),
                ),
            )
        else:
            ordered_sinks = sorted(
                sink_names,
                key=lambda s: (
                    inst_by_name[s].rank if s in inst_by_name else rank,
                    -round(net.sink_rates.get(s, 1.0), 6),
                    _natural_key(s),
                ),
            )
        sink_r_set = {inst_by_name[s].rank if s in inst_by_name else rank for s in ordered_sinks}
        sorted_sink_ranks = sorted(sink_r_set)
        sub_targets = [f"{material}__{s}" for s in ordered_sinks]
        if len(sorted_sink_ranks) <= 1:
            target_rank = sorted_sink_ranks[0] if sorted_sink_ranks else rank
            if (
                is_single_pack
                and driver_ranks
                and net.material not in nl.inputs
                and target_rank > max(driver_ranks) + 1
            ):
                chosen_rank = (max(driver_ranks) + target_rank) // 2
            else:
                chosen_rank = rank if (driver_ranks and not is_primary) else target_rank
        else:
            chosen_rank = rank if rank < min(sorted_sink_ranks) else min(sorted_sink_ranks)
        _build_tree(in_wire, sub_targets, tree_rank=chosen_rank, unbacked_flow=bus_supply)

    orig_drivers = net.orig_drivers if net.orig_drivers else list(net.drivers)
    belt_cap = nl.belt_capacity
    max_sink_rate = max((net.sink_rates.get(s, 0.0) for s in net.sinks), default=0.0)
    needs_multibus_for_heavy_sink = (
        len(orig_drivers) >= 4
        and len(net.sinks) >= 2
        and max_sink_rate > 0.50 * net.rate_per_min + 1e-6
    )
    sink_recipes = {inst_by_name[s].recipe for s in net.sinks if s in inst_by_name}
    has_multi_recipe_sinks = len(orig_drivers) >= 3 and len(sink_recipes) >= 2
    is_full_chassis = len(nl.outputs) >= 6 or "space-science-pack" in nl.outputs
    can_multibus_base = (
        (
            is_single_pack
            or (
                is_full_chassis
                and belt_cap > 0
                and net.rate_per_min > belt_cap
                and len(orig_drivers) >= 4
                and len(net.sinks) >= 4
            )
        )
        and synthesize_trees
        and not net.bus_inputs
        and net.material not in nl.inputs
        and net.material not in nl.outputs
        and belt_cap > 0
        and (net.rate_per_min > belt_cap or needs_multibus_for_heavy_sink or has_multi_recipe_sinks)
        and len(orig_drivers) >= 2
        and len(net.sinks) >= 2
        and net.material in getattr(nl, "merge_plans", {})
    )

    multibus_plan: tuple[list[list[str]], list[list[str]], list[float], list[float]] | None = None
    if can_multibus_base:
        eff_cap = 0.90 * belt_cap
        ordered_drivers = sorted(
            orig_drivers,
            key=lambda d: (inst_by_name[d].rank if d in inst_by_name else 0, _natural_key(d)),
        )
        ordered_sinks = sorted(
            net.sinks,
            key=lambda s: (inst_by_name[s].rank if s in inst_by_name else rank, _natural_key(s)),
        )
        max_drv_rated = max(
            (net.driver_rated.get(d, net.driver_rates.get(d, 0.0)) for d in ordered_drivers),
            default=0.0,
        )
        max_drvs_per_bus = (
            max(1, int(eff_cap // max_drv_rated)) if max_drv_rated > 0 else len(ordered_drivers)
        )
        min_buses_by_drv = math.ceil(len(ordered_drivers) / max_drvs_per_bus)
        min_buses_by_rate = math.ceil(net.rate_per_min / eff_cap)
        bus_max_cap = min(
            belt_cap, max_drvs_per_bus * max_drv_rated if max_drv_rated > 0 else belt_cap
        )
        force_multibus = net.rate_per_min > belt_cap or needs_multibus_for_heavy_sink
        max_sink_groups = len(ordered_sinks) if is_single_pack else len(ordered_sinks) // 2
        if force_multibus:
            base_k = min(
                max(2, min_buses_by_drv, min_buses_by_rate),
                len(ordered_drivers) // 2,
                max_sink_groups,
            )
            max_f_k = min(len(ordered_drivers) // 2, max_sink_groups)
            if len(sink_recipes) > 1 and base_k + 1 <= max_f_k:
                cand_ks = [base_k, base_k + 1]
            else:
                cand_ks = [base_k]
        else:
            max_k = min(len(sink_recipes), len(ordered_drivers) - 1, max_sink_groups)
            cand_ks = list(range(max_k, 1, -1))

        recipe_totals = {
            r: sum(
                max(1e-6, net.sink_rates.get(s, 1.0))
                for s in ordered_sinks
                if s in inst_by_name and inst_by_name[s].recipe == r
            )
            for r in sink_recipes
        }

        for cand_k in cand_ks:
            if cand_k < 2:
                continue
            s_groups = _partition_sinks_by_demand(
                ordered_sinks,
                net.sink_rates,
                cand_k,
                bus_max_cap=bus_max_cap,
                inst_by_name=inst_by_name,
            )
            if not is_single_pack:
                while any(len(g) < 2 for g in s_groups) and any(len(g) > 2 for g in s_groups):
                    small_idx = next(i for i, g in enumerate(s_groups) if len(g) < 2)
                    big_idx = max(range(len(s_groups)), key=lambda i: len(s_groups[i]))
                    s_groups[small_idx].append(s_groups[big_idx].pop())
                if any(len(g) < 2 for g in s_groups):
                    continue
            eff_k = len(s_groups)
            if eff_k < 2:
                continue
            if (
                force_multibus
                and len(cand_ks) > 1
                and cand_k == cand_ks[0]
                and any(v > bus_max_cap + 1e-6 for v in recipe_totals.values())
                and any(
                    len({inst_by_name[s].recipe for s in grp if s in inst_by_name}) > 1
                    and any(
                        recipe_totals.get(inst_by_name[s].recipe, 0.0) > bus_max_cap + 1e-6
                        for s in grp
                        if s in inst_by_name
                    )
                    for grp in s_groups
                )
            ):
                next_groups = _partition_sinks_by_demand(
                    ordered_sinks,
                    net.sink_rates,
                    cand_ks[1],
                    bus_max_cap=bus_max_cap,
                    inst_by_name=inst_by_name,
                )
                next_demands = [
                    sum(max(1e-6, net.sink_rates.get(s, 1.0)) for s in grp) for grp in next_groups
                ]
                next_needed = (
                    sum(math.ceil((d - 1e-6) / max_drv_rated) for d in next_demands)
                    if max_drv_rated > 0
                    else len(ordered_drivers) + 1
                )
                if next_needed <= len(ordered_drivers):
                    continue
            min_drvs = [2 if (force_multibus or len(grp) < 2) else 1 for grp in s_groups]
            if sum(min_drvs) > len(ordered_drivers):
                continue
            b_demands = [
                sum(max(1e-6, net.sink_rates.get(s, 1.0)) for s in grp) for grp in s_groups
            ]
            tot_demand = sum(b_demands)
            d_groups: list[list[str]] = []
            n_drvs = len(ordered_drivers)
            needed_drvs = (
                [
                    max(min_drvs[b], math.ceil((b_demands[b] - 1e-6) / max_drv_rated))
                    for b in range(eff_k)
                ]
                if max_drv_rated > 0
                else []
            )
            if needed_drvs and sum(needed_drvs) == n_drvs:
                prev_idx = 0
                for b in range(eff_k):
                    next_idx = prev_idx + needed_drvs[b]
                    d_groups.append(ordered_drivers[prev_idx:next_idx])
                    prev_idx = next_idx
            else:
                prev_idx = 0
                cum_demand = 0.0
                for b in range(eff_k):
                    cum_demand += b_demands[b]
                    if b == eff_k - 1:
                        next_idx = n_drvs
                    else:
                        raw_ideal = (cum_demand / tot_demand) * n_drvs
                        ideal_idx = (
                            math.ceil(raw_ideal - 1e-9)
                            if b_demands[b] < (tot_demand / eff_k) - 1e-6
                            else round(raw_ideal)
                        )
                        min_idx = prev_idx + min_drvs[b]
                        max_idx = n_drvs - sum(min_drvs[b + 1 :])
                        next_idx = max(min_idx, min(max_idx, ideal_idx))
                    d_groups.append(ordered_drivers[prev_idx:next_idx])
                    prev_idx = next_idx
            b_supplies = [
                sum(net.driver_rated.get(d, net.driver_rates.get(d, 0.0)) for d in d_grp)
                for d_grp in d_groups
            ]
            if force_multibus or all(
                b_supplies[b] >= 0.98 * b_demands[b] - 1e-6 for b in range(eff_k)
            ):
                multibus_plan = (s_groups, d_groups, b_demands, b_supplies)
                break

    if net.bus_inputs and len(net.bus_inputs) > 1 and bal_entries is not None:
        bus_wires = [sanitize(b) for b in net.bus_inputs]
        balanced_outputs = _balance_inputs(
            nl, material, net.material, bus_wires, bal_entries, bal_count
        )
        if not is_single_pack:
            k_bal = len(balanced_outputs)
            chunk_size = len(net.sinks) / k_bal
            sink_groups = [
                net.sinks[round(i * chunk_size) : round((i + 1) * chunk_size)] for i in range(k_bal)
            ]
        else:
            ordered_sinks = sorted(
                net.sinks,
                key=lambda s: (
                    inst_by_name[s].rank if s in inst_by_name else rank,
                    _natural_key(s),
                ),
            )
            sink_groups = _partition_sinks_by_demand(
                ordered_sinks, net.sink_rates, len(balanced_outputs), inst_by_name=inst_by_name
            )
        for out_wire, sub_sink_names in zip(balanced_outputs, sink_groups, strict=False):
            if synthesize_trees:
                if len(sub_sink_names) > 1:
                    _build_rank_grouped_tree(out_wire, sub_sink_names)
                elif len(sub_sink_names) == 1:
                    single_target = f"{material}__{sub_sink_names[0]}"
                    for idx, b_entry in enumerate(bal_entries):
                        b_name, b_a1, b_a2, b_y1, b_y2 = b_entry
                        if b_y1 == out_wire:
                            bal_entries[idx] = (b_name, b_a1, b_a2, single_target, b_y2)
                        elif b_y2 == out_wire:
                            bal_entries[idx] = (b_name, b_a1, b_a2, b_y1, single_target)
    elif multibus_plan is not None:
        sink_groups, drv_groups, _bus_demands, bus_supplies = multibus_plan

        # Rebuild merge trees for each sub-bus in-place
        new_merge_entries: list[tuple[str, str, str, str]] = []
        new_merge_insts: list[Instance] = []
        m_count = 0

        def _build_sub_merge_tree(
            items: list[str], default_rank: int, root_wire: str | None = None
        ) -> str:
            nonlocal m_count
            if len(items) == 1:
                return items[0]
            if len(items) == 2:
                m_count += 1
                m_name = f"u_merge_{material}_{m_count}"
                y_w = root_wire if root_wire is not None else f"{material}__m{m_count}"
                new_merge_entries.append((m_name, items[0], items[1], y_w))
                new_merge_insts.append(
                    Instance(m_name, MERGE_CELL, net.material, 0.0, 0.0, rank=default_rank)
                )
                return y_w
            mid_idx = (len(items) + 1) // 2
            l_out = _build_sub_merge_tree(items[:mid_idx], default_rank, root_wire=None)
            r_out = _build_sub_merge_tree(items[mid_idx:], default_rank, root_wire=None)
            m_count += 1
            m_name = f"u_merge_{material}_{m_count}"
            y_w = root_wire if root_wire is not None else f"{material}__m{m_count}"
            new_merge_entries.append((m_name, l_out, r_out, y_w))
            new_merge_insts.append(
                Instance(m_name, MERGE_CELL, net.material, 0.0, 0.0, rank=default_rank)
            )
            return y_w

        bus_wires: list[str] = []
        bus_root_mergers: list[str] = []
        for b, d_grp in enumerate(drv_groups):
            if len(d_grp) == 1:
                b_wire = f"{material}__{d_grp[0]}"
                bus_wires.append(b_wire)
                bus_root_mergers.append(d_grp[0])
                continue
            b_wire = f"{material}__bus{b}"
            bus_wires.append(b_wire)
            drvs_by_r: dict[int, list[str]] = collections.defaultdict(list)
            for d in d_grp:
                inst = inst_by_name.get(d)
                r_d = inst.rank if inst else 0
                drvs_by_r[r_d].append(f"{material}__{d}")
            s_ranks = sorted(drvs_by_r.keys())
            if len(s_ranks) == 1:
                _build_sub_merge_tree(drvs_by_r[s_ranks[0]], s_ranks[0], root_wire=b_wire)
            else:
                r_roots = [
                    _build_sub_merge_tree(drvs_by_r[r_d], r_d, root_wire=None) for r_d in s_ranks
                ]
                _build_sub_merge_tree(r_roots, max(s_ranks), root_wire=b_wire)
            bus_root_mergers.append(f"u_merge_{material}_{m_count}")

        nl.instances = [
            inst
            for inst in nl.instances
            if not (inst.cell_key == MERGE_CELL and inst.recipe == net.material)
        ]
        nl.instances.extend(new_merge_insts)
        nl.merge_plans[net.material][:] = new_merge_entries
        net.sub_buses = list(bus_wires)
        net.drivers = [bus_root_mergers[0]]

        for in_w, s_grp, b_sup in zip(bus_wires, sink_groups, bus_supplies, strict=True):
            if len(s_grp) > 1:
                _build_rank_grouped_tree(in_w, s_grp, bus_supply=b_sup if b_sup > 0 else None)
            elif len(s_grp) == 1:
                single_target = f"{material}__{s_grp[0]}"
                for idx, m_entry in enumerate(nl.merge_plans[net.material]):
                    m_name, m_a, m_b, m_y = m_entry
                    if m_y == in_w:
                        nl.merge_plans[net.material][idx] = (m_name, m_a, m_b, single_target)
    elif synthesize_trees:
        _build_rank_grouped_tree(material, net.sinks)

    if entries:
        net.sinks = [entries[0][0]]
    elif bal_entries:
        net.sinks = [bal_entries[0][0]]
    return entries


def emit_verilog(
    nl: Netlist,
    merge_plans: dict[str, list[tuple[str, str, str, str]]],
    split_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
    balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None = None,
) -> str:
    """The netlist as flat structural Verilog, against LEF masters.

    No cell modules live here. In a standard-cell flow the design
    netlist instantiates LEF masters directly; the cell modules are
    library code (emit_cells_v), not design code. OpenROAD takes the
    masters from tech.lef at link time.
    """
    if split_plans is None:
        split_plans = {}
    if balancer_plans is None:
        balancer_plans = getattr(nl, "balancer_plans", {})
    out: list[str] = []
    out.append(f"// {nl.label}: generated by foundry/netlist.py. Do not edit.")
    out.append("")
    input_wires = [sanitize(m) for m in nl.inputs]
    ports = [f"  input  wire {w}" for w in input_wires]
    ports.extend(f"  output wire {sanitize(m)}" for m in nl.outputs)
    out.append(f"module {nl.label} (")
    out.append(",\n".join(ports))
    out.append(");")
    # Wires: one per internal material plus one per pre-merge driver.
    internal = [
        m
        for m in sorted(nl.nets)
        if m not in nl.inputs
        and m not in nl.outputs
        and not nl.nets[m].bus_inputs
        and not getattr(nl.nets[m], "sub_buses", None)
    ]
    out.extend(f"  wire {sanitize(m)};  // {nl.nets[m].rate_per_min:.2f} per min" for m in internal)
    declared: set[str] = set(input_wires) | {sanitize(m) for m in internal}
    for material, plan in sorted(merge_plans.items()):
        bare = sanitize(material)
        for _name, a_wire, b_wire, y_wire in plan:
            for wire in (a_wire, b_wire, y_wire):
                if wire == bare or wire in declared:
                    continue
                declared.add(wire)
                out.append(f"  wire {wire};")
    for material, b_plan in sorted(balancer_plans.items()):
        bare = sanitize(material)
        for _name, a1_wire, a2_wire, y1_wire, y2_wire in b_plan:
            for wire in (a1_wire, a2_wire, y1_wire, y2_wire):
                if wire == bare or wire in declared or wire in input_wires:
                    continue
                declared.add(wire)
                out.append(f"  wire {wire};")
    for material, plan in sorted(split_plans.items()):
        bare = sanitize(material)
        for _name, a_wire, y1_wire, y2_wire in plan:
            for wire in (a_wire, y1_wire, y2_wire):
                if wire == bare or wire in declared:
                    continue
                declared.add(wire)
                out.append(f"  wire {wire};")
    for inst in nl.instances:
        if inst.cell_key == pdk.ARITHMETIC_COMBINATOR_CELL:
            c_out_wire = f"c_out_{sanitize(inst.recipe)}"
            if c_out_wire not in declared:
                declared.add(c_out_wire)
                out.append(f"  wire {c_out_wire};")
    out.append("")
    for inst in nl.instances:
        if inst.cell_key == MERGE_CELL:
            plan = merge_plans[inst.recipe]
            entry = next(e for e in plan if e[0] == inst.name)
            _name, a_wire, b_wire, y_wire = entry
            out.append(f"  {MERGE_CELL} {inst.name} (.a({a_wire}), .b({b_wire}), .y({y_wire}));")
            continue
        if inst.cell_key == BALANCER_CELL:
            b_plan = balancer_plans.get(inst.recipe) or balancer_plans.get(
                sanitize(inst.recipe), []
            )
            b_entry = next((e for e in b_plan if e[0] == inst.name), None)
            if b_entry:
                _name, a1_wire, a2_wire, y1_wire, y2_wire = b_entry
                out.append(
                    f"  {BALANCER_CELL} {inst.name} (.a1({a1_wire}), .a2({a2_wire}), .y1({y1_wire}), .y2({y2_wire}));"
                )
            continue
        if inst.cell_key == SPLIT_CELL:
            plan = split_plans[inst.recipe]
            entry = next(e for e in plan if e[0] == inst.name)
            _name, a_wire, y1_wire, y2_wire = entry
            out.append(
                f"  {SPLIT_CELL} {inst.name} (.a({a_wire}), .y1({y1_wire}), .y2({y2_wire}));"
            )
            continue
        if inst.cell_key == pdk.DISPLAY_PANEL_CELL:
            mat_wire = sanitize(inst.recipe)
            c_out_wire = f"c_out_{mat_wire}"
            c_disp_wire = f"c_disp_{mat_wire}"
            conn_wire = c_out_wire if c_out_wire in declared else c_disp_wire
            out.append(f"  // telemetry display monitor for {inst.recipe}")
            out.append(f"  {pdk.DISPLAY_PANEL_CELL} {inst.name} (.a({conn_wire}));")
            continue
        if inst.cell_key == pdk.ARITHMETIC_COMBINATOR_CELL and inst.name.startswith("u_acc_"):
            mat_wire = sanitize(inst.recipe)
            c_out_wire = f"c_out_{mat_wire}"
            c_in_wire = f"c_in_{mat_wire}"
            out.append(f"  // telemetry accumulator counter for {inst.recipe}")
            out.append(
                f"  {pdk.ARITHMETIC_COMBINATOR_CELL} {inst.name} (.a({c_in_wire}), .y({c_out_wire}));"
            )
            continue
        cell = nl.cells[inst.cell_key]
        if cell.get("circuit_cell"):
            module = sanitize(inst.cell_key)
            conns = []
            if "a" in cell.get("inputs", []):
                in_wire = next(
                    (sanitize(n) for n, net in nl.nets.items() if inst.name in net.sinks),
                    f"c_{inst.name}_in",
                )
                if in_wire not in declared:
                    declared.add(in_wire)
                    out.insert(len(ports) + 2, f"  wire {in_wire};")
                conns.append(f".a({in_wire})")
            if "y" in cell.get("outputs", []):
                out_wire = next(
                    (sanitize(n) for n, net in nl.nets.items() if inst.name in net.drivers),
                    f"c_{inst.name}_out",
                )
                if out_wire not in declared:
                    declared.add(out_wire)
                    out.insert(len(ports) + 2, f"  wire {out_wire};")
                conns.append(f".y({out_wire})")
            out.append(f"  {module} {inst.name} (")
            out.append(",\n".join(f"    {c}" for c in conns))
            out.append("  );")
            continue
        module = sanitize(inst.cell_key)
        conns = []
        for p in cell["inputs"]:
            if (split_plans and p in split_plans) or (balancer_plans and p in balancer_plans):
                conns.append(f".{sanitize(p)}({sanitize(p)}__{inst.name})")
            else:
                conns.append(f".{sanitize(p)}({sanitize(p)})")
        for port in cell["outputs"]:
            if port in merge_plans:
                conns.append(f".{sanitize(port)}({sanitize(port)}__{inst.name})")
            else:
                conns.append(f".{sanitize(port)}({sanitize(port)})")
        util = f"{inst.plan_rate / inst.rated * 100:.0f}%" if inst.rated else "-"
        out.append(f"  // plan {inst.plan_rate:.2f}/min of {inst.rated:.2f}/min rated, {util}")
        out.append(f"  {module} {inst.name} (")
        out.append(",\n".join(f"    {c}" for c in conns))
        out.append("  );")
    out.append("endmodule")
    return "\n".join(out) + "\n"


def insert_telemetry(
    nl: Netlist,
    tap_scope: str = "io,merges",
    enable_display: bool = True,
    enable_circuit: bool = False,
    merge_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
) -> list[dict[str, Any]]:
    """Insert telemetry tap points and readout cells into the netlist.

    tap_scope can be a comma-separated list of scopes:
      - 'io': input and output boundary nets
      - 'merges': multi-driver nets that use splitters
      - 'all': every net in the netlist
      - 'none': disable telemetry
      - or specific material names.
    """
    scopes = {s.strip().lower() for s in tap_scope.split(",") if s.strip()}
    if "none" in scopes or (not enable_display and not enable_circuit):
        return []

    tapped_materials: set[str] = set()
    if "all" in scopes:
        tapped_materials.update(nl.nets.keys())
    else:
        if "io" in scopes:
            tapped_materials.update(nl.inputs)
            tapped_materials.update(nl.outputs)
        if "merges" in scopes and merge_plans:
            tapped_materials.update(merge_plans.keys())
        for s in scopes:
            for m in nl.nets:
                if s == m.lower() or s == sanitize(m).lower():
                    tapped_materials.add(m)

    if not tapped_materials:
        return []

    if enable_display:
        disp_cell = pdk.display_panel_cell()
        nl.cells[disp_cell["cell"]] = disp_cell
    if enable_circuit:
        acc_cell = pdk.arithmetic_combinator_cell()
        nl.cells[acc_cell["cell"]] = acc_cell

    records: list[dict[str, Any]] = []
    max_rank = max((inst.rank for inst in nl.instances), default=0)
    telemetry_rank = max_rank + 1
    for mat in sorted(tapped_materials):
        if mat not in nl.nets:
            continue
        bare = sanitize(mat)
        disp_name = None
        circ_name = None

        rank = telemetry_rank
        if enable_circuit:
            circ_name = f"u_acc_{bare}"
            c_in_name = f"c_in_{bare}"
            c_out_name = f"c_out_{bare}"
            nl.instances.append(
                Instance(circ_name, pdk.ARITHMETIC_COMBINATOR_CELL, mat, 0.0, 0.0, rank=rank)
            )
            nl.net(c_in_name).sinks.append(circ_name)
            nl.net(c_out_name).drivers.append(circ_name)

        if enable_display:
            disp_name = f"u_disp_{bare}"
            nl.instances.append(
                Instance(disp_name, pdk.DISPLAY_PANEL_CELL, mat, 0.0, 0.0, rank=rank)
            )
            if enable_circuit:
                c_out_name = f"c_out_{bare}"
                nl.net(c_out_name).sinks.append(disp_name)
            else:
                c_disp_name = f"c_disp_{bare}"
                nl.net(c_disp_name).sinks.append(disp_name)

        records.append(
            {
                "material": mat,
                "display_instance": disp_name,
                "circuit_instance": circ_name,
            }
        )

    return records


def emit_cells_v(nl: Netlist) -> str:
    """The cell library as Verilog stubs.

    One empty module per used cell, ports matching the LEF view. This is
    the library code that pyslang and yosys read with the netlist; OpenROAD
    ignores it and links the LEF masters instead.
    """
    out: list[str] = ["// cell library stubs: generated. Do not edit.", ""]
    for key in sorted(nl.cells):
        cell = nl.cells[key]
        module = sanitize(key)
        # A recipe may craft an item that is also a building name, such as
        # electric-furnace. Say "crafting" so nobody reads a cell as a
        # machine type.
        out.append(
            f"// cell {cell['machine']} crafting {cell['recipe']}: "
            f"{cell['width']}x{cell['height']} tiles, "
            f"{cell['watts'] / 1000:.0f} kW, "
            f"{cell['crafts_per_minute']:.2f} crafts/min rated"
        )
        out.append(f"module {module} (")
        lines = [f"  input  wire {sanitize(p)}" for p in cell["inputs"]]
        lines.extend(f"  output wire {sanitize(p)}" for p in cell["outputs"])
        out.append(",\n".join(lines))
        out.append(");")
        out.append("endmodule")
        out.append("")
    out.append("// splitter run backwards: joins two lanes of one material")
    out.append(f"module {MERGE_CELL} (")
    out.append("  input  wire a,")
    out.append("  input  wire b,")
    out.append("  output wire y")
    out.append(");")
    out.append("endmodule")
    out.append("")
    out.append("// fluid buffer: pump enforces unidirectional flow")
    out.append(f"module {PUMP_CELL} (")
    out.append("  input  wire a,")
    out.append("  output wire y")
    out.append(");")
    out.append("endmodule")
    out.append("")
    out.append("// splitter run forwards: splits one lane of one material into two")
    out.append(f"module {SPLIT_CELL} (")
    out.append("  input  wire a,")
    out.append("  output wire y1,")
    out.append("  output wire y2")
    out.append(");")
    out.append("endmodule")
    out.append("")
    out.append("// splitter 2-to-2 balancer: balances two lanes of one material")
    out.append(f"module {BALANCER_CELL} (")
    out.append("  input  wire a1,")
    out.append("  input  wire a2,")
    out.append("  output wire y1,")
    out.append("  output wire y2")
    out.append(");")
    out.append("endmodule")
    return "\n".join(out) + "\n"


def merge_plans_of(nl: Netlist) -> dict[str, list[tuple[str, str, str, str]]]:
    """Merge insertion for every multi-driver net, before emission."""
    plans: dict[str, list[tuple[str, str, str, str]]] = {}
    for net in nl.nets.values():
        if net.is_fluid or net.material in nl.fluids:
            continue
        entries = merge_net(nl, net)
        if entries:
            plans[net.material] = entries
    nl.merge_plans = plans
    return plans


def split_plans_of(
    nl: Netlist, synthesize_trees: bool = True
) -> dict[str, list[tuple[str, str, str, str]]]:
    """Split insertion for every multi-sink net, before emission.

    When synthesize_trees is False, consumers use multi-drop belt buses.
    """
    plans: dict[str, list[tuple[str, str, str, str]]] = {}
    nl.balancer_plans = {}
    for net in list(nl.nets.values()):
        if net.is_fluid or net.material in nl.fluids:
            continue
        bal_entries: list[tuple[str, str, str, str, str]] = []
        entries = split_net(nl, net, bal_entries, synthesize_trees=synthesize_trees)
        if entries:
            plans[net.material] = entries
        if bal_entries:
            nl.balancer_plans[net.material] = bal_entries
    return plans


def balancer_plans_of(nl: Netlist) -> dict[str, list[tuple[str, str, str, str, str]]]:
    """Return balancer plans populated during split planning."""
    return getattr(nl, "balancer_plans", {})


def _instance_flow_priority(inst: Instance) -> int:
    """Return placement priority along West-to-East flow (0=split/in, 1=machine, 2=merge/out)."""
    if "split" in inst.cell_key or "balancer" in inst.cell_key or "split" in inst.name:
        return 0
    if "merge" in inst.cell_key or "merge" in inst.name:
        return 2
    if "display" in inst.cell_key or "combinator" in inst.cell_key or "disp" in inst.name:
        return 3
    return 1


def _natural_key(s: str) -> list[int | str]:
    """Sort strings with embedded numbers naturally (e.g. 7 before 10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def emit_ranks_tcl(
    nl: Netlist,
    merge_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
    split_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
    balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None = None,
) -> str:
    """Emit instance rank assignments as a TCL dictionary and lists."""
    lines = [
        "# ranks.tcl - Instance rank assignments for structured linear row placement",
        "set instance_ranks [dict create]",
    ]
    fluid_nets = [sanitize(m) for m in sorted(getattr(nl, "fluids", set())) if m in nl.nets]
    if fluid_nets:
        lines.append(f"set fluid_nets [list {' '.join(fluid_nets)}]")
    raw_by_rank: dict[int, list[str]] = {}
    inst_by_name = {inst.name: inst for inst in nl.instances}
    for inst in nl.instances:
        raw_by_rank.setdefault(inst.rank, []).append(inst.name)

    active_ranks = sorted(raw_by_rank.keys())
    rank_remap = {old_r: new_r for new_r, old_r in enumerate(active_ranks)}
    by_rank: dict[int, list[str]] = {}
    for inst in nl.instances:
        new_r = rank_remap[inst.rank]
        inst.rank = new_r
        lines.append(f"dict set instance_ranks {inst.name} {new_r}")
        by_rank.setdefault(new_r, []).append(inst.name)

    # Build intra-rank dependency graph from wire drivers and sinks
    wire_driver: dict[str, str] = {}
    wire_sinks: dict[str, list[str]] = collections.defaultdict(list)

    if merge_plans:
        for plan in merge_plans.values():
            for m_name, a_w, b_w, y_w in plan:
                wire_sinks[a_w].append(m_name)
                wire_sinks[b_w].append(m_name)
                wire_driver[y_w] = m_name

    if split_plans:
        for plan in split_plans.values():
            for s_name, in_w, y1_w, y2_w in plan:
                wire_sinks[in_w].append(s_name)
                wire_driver[y1_w] = s_name
                wire_driver[y2_w] = s_name

    if balancer_plans:
        for b_plan in balancer_plans.values():
            for bal_name, a1_w, a2_w, y1_w, y2_w in b_plan:
                wire_sinks[a1_w].append(bal_name)
                wire_sinks[a2_w].append(bal_name)
                wire_driver[y1_w] = bal_name
                wire_driver[y2_w] = bal_name

    for inst in nl.instances:
        cell = nl.cells.get(inst.cell_key)
        if not cell:
            continue
        for p in cell.get("inputs", []):
            if (split_plans and p in split_plans) or (balancer_plans and p in balancer_plans):
                wire_sinks[f"{sanitize(p)}__{inst.name}"].append(inst.name)
            else:
                wire_sinks[sanitize(p)].append(inst.name)
        for p in cell.get("outputs", []):
            if merge_plans and p in merge_plans:
                wire_driver[f"{sanitize(p)}__{inst.name}"] = inst.name
            else:
                wire_driver[sanitize(p)] = inst.name

    deps: dict[str, set[str]] = collections.defaultdict(set)
    for wire, driver in wire_driver.items():
        for sink in wire_sinks.get(wire, []):
            if driver != sink:
                deps[sink].add(driver)

    forward_deps: dict[str, set[str]] = collections.defaultdict(set)
    reverse_deps: dict[str, set[str]] = collections.defaultdict(set)
    for wire, driver in wire_driver.items():
        for sink in wire_sinks.get(wire, []):
            if driver != sink:
                forward_deps[driver].add(sink)
                reverse_deps[sink].add(driver)

    terminal_order: dict[str, int] = {}
    for idx, out_mat in enumerate(nl.outputs):
        for inst in nl.instances:
            if inst.recipe == out_mat:
                terminal_order[inst.name] = idx

    inst_terminal_prio: dict[str, int] = dict(terminal_order)
    queue = list(terminal_order.keys())

    while queue:
        curr = queue.pop(0)
        curr_prio = inst_terminal_prio[curr]
        for upstream in reverse_deps.get(curr, set()):
            if upstream not in inst_terminal_prio or curr_prio < inst_terminal_prio[upstream]:
                inst_terminal_prio[upstream] = curr_prio
                queue.append(upstream)

    num_ranks = (max(by_rank.keys()) + 1) if by_rank else 0
    lines.append(f"set num_ranks {num_ranks}")
    # Map input wires for all instances
    inst_input_wires: dict[str, list[str]] = collections.defaultdict(list)
    for inst in nl.instances:
        cell = nl.cells.get(inst.cell_key, {})
        for p in cell.get("inputs", []):
            wname = (
                f"{sanitize(p)}__{inst.name}"
                if (split_plans and p in split_plans) or (balancer_plans and p in balancer_plans)
                else sanitize(p)
            )
            inst_input_wires[inst.name].append(wname)

    if merge_plans:
        for plan in merge_plans.values():
            for m_name, a_w, b_w, _y_w in plan:
                inst_input_wires[m_name].extend([a_w, b_w])

    if split_plans:
        for plan in split_plans.values():
            for s_name, in_w, _y1_w, _y2_w in plan:
                inst_input_wires[s_name].append(in_w)

    if balancer_plans:
        for b_plan in balancer_plans.values():
            for bal_name, a1_w, a2_w, _y1_w, _y2_w in b_plan:
                inst_input_wires[bal_name].extend([a1_w, a2_w])

    rank_offsets: dict[int, int] = {}
    inst_abs_center: dict[str, float] = {}
    sorted_by_rank: dict[int, list[str]] = {}

    for r in range(num_ranks):
        rank_insts = set(by_rank.get(r, []))
        rank_deps = {
            name: {d for d in deps.get(name, set()) if d in rank_insts} for name in rank_insts
        }
        in_degree = {name: len(d_set) for name, d_set in rank_deps.items()}

        def _find_driver_x(start: str) -> float:
            visited: set[str] = set()
            q = [start]
            while q:
                c = q.pop(0)
                if c in inst_abs_center:
                    return inst_abs_center[c]
                for prv in reverse_deps.get(c, set()):
                    if prv not in visited:
                        visited.add(prv)
                        q.append(prv)
            return 0.0

        def _machine_driver_x(name: str) -> float:
            in_wires = inst_input_wires.get(name, [])
            driver_xs = [
                _find_driver_x(d) for w in in_wires for d in wire_driver.get(w, "").split()
            ]
            valid_xs = [x for x in driver_xs if x > 0]
            return sum(valid_xs) / len(valid_xs) if valid_xs else 0.0

        machines_in_rank = [
            name for name in rank_insts if _instance_flow_priority(inst_by_name[name]) == 1
        ]
        machines_in_rank.sort(
            key=lambda name: (round(_machine_driver_x(name), 1), _natural_key(name))
        )
        machine_idx = {name: i for i, name in enumerate(machines_in_rank)}

        def _downstream_machines(start: str, rank_set: set[str]) -> set[str]:
            visited: set[str] = set()
            q = [start]
            machs: set[str] = set()
            while q:
                c = q.pop(0)
                for nxt in forward_deps.get(c, set()):
                    if nxt in rank_set and nxt not in visited:
                        visited.add(nxt)
                        if _instance_flow_priority(inst_by_name[nxt]) == 1:
                            machs.add(nxt)
                        else:
                            q.append(nxt)
            return machs

        def _upstream_machines(start: str, rank_set: set[str]) -> set[str]:
            visited: set[str] = set()
            q = [start]
            machs: set[str] = set()
            while q:
                c = q.pop(0)
                for prv in reverse_deps.get(c, set()):
                    if prv in rank_set and prv not in visited:
                        visited.add(prv)
                        if _instance_flow_priority(inst_by_name[prv]) == 1:
                            machs.add(prv)
                        else:
                            q.append(prv)
            return machs

        has_furnaces = any(
            "furnace" in nl.cells.get(inst_by_name[iname].cell_key, {}).get("machine", "")
            for iname in rank_insts
        )

        def _inst_mat(name: str) -> str:
            wires = inst_input_wires.get(name, [])
            if wires:
                return wires[0].split("__")[0]
            inst = inst_by_name.get(name)
            return inst.recipe if inst else ""

        mat_driver_x: dict[str, float] = {}
        for name in rank_insts:
            m_name = _inst_mat(name)
            dx = _find_driver_x(name)
            if dx > 0 and (m_name not in mat_driver_x or dx < mat_driver_x[m_name]):
                mat_driver_x[m_name] = dx
            if _instance_flow_priority(inst_by_name[name]) == 1:
                in_wires = inst_input_wires.get(name, [])
                for w in in_wires:
                    in_mat = w.split("__")[0]
                    for d in wire_driver.get(w, "").split():
                        in_dx = _find_driver_x(d)
                        if in_dx > 0 and (
                            in_mat not in mat_driver_x or in_dx < mat_driver_x[in_mat]
                        ):
                            mat_driver_x[in_mat] = in_dx

        machine_group_x: dict[str, float] = {}
        for name in machines_in_rank:
            in_mats = [w.split("__")[0] for w in inst_input_wires.get(name, [])]
            in_drv_xs = [mat_driver_x[m] for m in in_mats if m in mat_driver_x]
            gx = max(in_drv_xs) if in_drv_xs else _machine_driver_x(name)
            machine_group_x[name] = gx
            inst = inst_by_name.get(name)
            if inst and inst.recipe:
                mat_driver_x[inst.recipe] = gx
                mat_driver_x[sanitize(inst.recipe)] = gx

        rank_sort_keys: dict[str, tuple[Any, ...]] = {}
        for name in rank_insts:
            prio = _instance_flow_priority(inst_by_name[name])
            mat = _inst_mat(name)
            if prio == 1:
                inst_group_x = machine_group_x.get(name, 0.0)
            elif prio == 2:
                up = _upstream_machines(name, rank_insts)
                up_gxs = [machine_group_x[m] for m in up if m in machine_group_x]
                inst_group_x = max(up_gxs) if up_gxs else mat_driver_x.get(mat, 0.0)
            elif prio == 0:
                down = _downstream_machines(name, rank_insts)
                down_gxs = [machine_group_x[m] for m in down if m in machine_group_x]
                if down_gxs:
                    inst_group_x = min(down_gxs)
                else:
                    up = _upstream_machines(name, rank_insts)
                    up_gxs = [machine_group_x[m] for m in up if m in machine_group_x]
                    inst_group_x = max(up_gxs) if up_gxs else mat_driver_x.get(mat, 0.0)
            else:
                inst_group_x = mat_driver_x.get(mat, 0.0)

            if prio == 0:
                span = 0
                min_down = -1
                down = _downstream_machines(name, rank_insts)
                down_indices = [machine_idx[m] for m in down if m in machine_idx]
                if down_indices:
                    min_down = min(down_indices)
                    span = max(down_indices) - min(down_indices)
                drv_x = _find_driver_x(name)
                rank_sort_keys[name] = (
                    round(inst_group_x, 1),
                    0,
                    mat,
                    min_down,
                    -span,
                    round(drv_x, 1),
                    _natural_key(name),
                )
            elif prio == 1:
                eff_idx = machine_idx[name]
                rank_sort_keys[name] = (
                    round(inst_group_x, 1),
                    1,
                    eff_idx,
                    0,
                    0.0,
                    _natural_key(name),
                )
            elif prio == 2:
                span = 0
                up = _upstream_machines(name, rank_insts)
                up_indices = [machine_idx[m] for m in up if m in machine_idx]
                if up_indices:
                    min_up = min(up_indices)
                    span = max(up_indices) - min(up_indices)
                else:
                    min_up = len(machines_in_rank)
                rank_sort_keys[name] = (
                    round(inst_group_x, 1),
                    2,
                    mat,
                    min_up,
                    span,
                    0.0,
                    _natural_key(name),
                )
            else:
                rank_sort_keys[name] = (
                    round(inst_group_x, 1),
                    3,
                    mat,
                    len(machines_in_rank) + 1,
                    0,
                    0.0,
                    inst_by_name[name].recipe or "",
                    _natural_key(name),
                )

        ready = [name for name, deg in in_degree.items() if deg == 0]
        sorted_insts: list[str] = []

        while ready:
            ready.sort(key=lambda name: rank_sort_keys[name])
            curr = ready.pop(0)
            sorted_insts.append(curr)
            for other, d_set in rank_deps.items():
                if curr in d_set:
                    d_set.remove(curr)
                    if len(d_set) == 0 and other not in sorted_insts and other not in ready:
                        ready.append(other)

        for name in rank_insts:
            if name not in sorted_insts:
                sorted_insts.append(name)

        sorted_by_rank[r] = sorted_insts
        insts = " ".join(sorted_insts)
        lines.append(f"set rank_instances({r}) [list {insts}]")

        rel_centers: dict[str, float] = {}
        curr_x = 0.0
        for iname in sorted_insts:
            inst = inst_by_name[iname]
            cell = nl.cells.get(inst.cell_key, {})
            w = float(cell.get("width", 3))
            pad = 8.0
            if "merge" in iname:
                pad = 16.0
            elif "split" in iname or "bal" in iname:
                pad = 10.0
            rel_centers[iname] = curr_x + w / 2.0
            curr_x += w + pad

        has_furnaces = any(
            any(
                inp in nl.inputs
                for inp in nl.cells.get(inst_by_name[iname].cell_key, {}).get("inputs", [])
            )
            for iname in sorted_insts
        )

        connected_pairs: list[tuple[float, float]] = []
        for iname in sorted_insts:
            for w in inst_input_wires[iname]:
                connected_pairs.extend(
                    (inst_abs_center[d], rel_centers[iname])
                    for d in wire_driver.get(w, "").split()
                    if d in inst_abs_center
                )

        if has_furnaces or not connected_pairs:
            offset = 0
        else:
            avg_feeder_x = sum(p[0] for p in connected_pairs) / len(connected_pairs)
            avg_rel_x = sum(p[1] for p in connected_pairs) / len(connected_pairs)
            offset = max(0, round(avg_feeder_x - avg_rel_x) + 4)

        rank_offsets[r] = offset
        for iname in sorted_insts:
            inst_abs_center[iname] = offset + rel_centers[iname]
        lines.append(f"set rank_offset_x({r}) {offset}")

    return "\n".join(lines) + "\n"
