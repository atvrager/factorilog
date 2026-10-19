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
from collections.abc import Mapping, Sequence
from typing import Any

import pdk
import solve

# The transport cell that joins two streams of one material. A splitter
# run backwards. The PDK carries it next to the recipe cells.
MERGE_CELL = "belt_merge2"

SPLIT_CELL = "belt_split2"
BALANCER_CELL = "belt_balancer2"
PUMP_CELL = "pump"

MERGE_CELL_SIZE = 3
WATTS_PER_KW = 1000.0
PUMP_POWER_KW = 29.0
PUMP_POWER_WATTS = PUMP_POWER_KW * WATTS_PER_KW
PLAN_COUNT_TUPLE_INDEX = 3
FULL_CHASSIS_PACK_COUNT = 6
MAX_MACHINES_PER_ROW = 5
TERMINAL_STAGE_MIN_DEPTH = 3
FLUID_RECIPE_HEADROOM_UTIL = 0.90
BUS_EFFECTIVE_CAPACITY_RATIO = 0.90
MIN_WHOLE_BUS_FRACTION = 0.50
BINARY_SPLIT_RATIO = 0.50
POW2_EXCESS_BELT_DIVISOR = 3.0
FOUR_SIDED_INPUT_THRESHOLD = 6
DEFAULT_MACHINE_DIM = 3

MIN_SPACED_PORT_WIDTH = 3
FLOAT_TOL = 1e-6
CEIL_EPS = 1e-9
ROUND_DIGITS = 6
RANK_COORD_DECIMALS = 1


FLOW_PRIORITY_SPLIT = 0
FLOW_PRIORITY_MACHINE = 1
FLOW_PRIORITY_MERGE = 2
FLOW_PRIORITY_TELEMETRY = 3

RANK_PAD_DEFAULT = 8.0
RANK_PAD_MERGE = 16.0
RANK_PAD_SPLIT = 10.0
RANK_FEEDER_OFFSET = 4

SPLIT_DOMINANT_SINK_RATIO = 4.0
SPLIT_DOMINANT_MIN_SINKS = 6
MULTIBUS_HEAVY_SINK_MIN_DRIVERS = 4
MULTIBUS_MULTI_RECIPE_MIN_DRIVERS = 3
MULTIBUS_CHASSIS_MIN_ENDPOINTS = 4
MULTIBUS_SUPPLY_RATIO = 0.98

ControlBehavior = Mapping[str, object]


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
        "width": MERGE_CELL_SIZE,
        "height": MERGE_CELL_SIZE,
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
        "watts": PUMP_POWER_WATTS,
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
        control_behavior: ControlBehavior | None = None,
    ) -> None:
        """Initialize a placed cell instance."""
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
        """Initialize a material net with empty driver and sink lists."""
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
        """Initialize an empty netlist container."""
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
        """Return the existing Net for material, or create and register a new one."""
        if material not in self.nets:
            self.nets[material] = Net(material, is_fluid=(material in self.fluids))
        return self.nets[material]


def stage_rank_of(
    material_or_recipe: str,
    inputs: list[str] | None = None,
    material_depths: dict[str, int] | None = None,
) -> int:
    """Classify instances into structured linear row placement bands based on DAG depth."""
    if not material_depths:
        return 0
    d = material_depths.get(material_or_recipe)
    if d is None and inputs:
        d = max((material_depths.get(p, 0) for p in inputs), default=0) + 1
    return d if d is not None else 0


def _coord_face_for_dy(dy: float) -> str:
    """Classify vertical feeder offset relative to machine center into N, S, or W face."""
    if dy > 1.0:
        return "N"
    if dy < -1.0:
        return "S"
    return "W"


def assign_gaa_pin_faces(
    inputs: list[str],
    feeder_coords: dict[str, tuple[float, float]] | None = None,
    mach_y: float = 0.0,
    depths: dict[str, int] | None = None,
    allow_east: bool = False,
) -> dict[str, str]:
    """Assign input ports to West, North, South, or East faces based on driver direction."""
    if feeder_coords:
        return {
            p: _coord_face_for_dy(feeder_coords.get(p, (0.0, mach_y))[1] - mach_y) for p in inputs
        }
    faces_order = ("W", "N", "S", "E") if allow_east else ("W", "N", "S")
    depth_map = depths or {}
    sorted_inputs = sorted(inputs, key=lambda p: (depth_map.get(p, 0), p))
    return {p: faces_order[idx % len(faces_order)] for idx, p in enumerate(sorted_inputs)}


def _recipe_entry_amount(entries: list[pdk.ProtoRecord], target_name: str) -> float:
    """Return the item/fluid amount for target_name in a recipe entry list, defaulting to 1.0."""
    for entry in entries:
        if solve.name_of(entry) == target_name:
            return solve.amount_of(entry)
    return 1.0


def _consumer_demands_for_item(
    data: solve.Data, rates: dict[str, float], item_name: str
) -> list[float]:
    """Collect per-recipe consumption rates for item_name across all active recipes."""
    demands: list[float] = []
    for c_rec, c_rate in rates.items():
        c_proto = data.recipes.get(c_rec)
        if not c_proto:
            continue
        demands.extend(
            c_rate * solve.amount_of(ing)
            for ing in c_proto.get("ingredients", [])
            if solve.name_of(ing) == item_name
        )
    return demands


def _partitioned_driver_count(
    data: solve.Data,
    rates: dict[str, float],
    recipe_proto: pdk.ProtoRecord,
    out_name: str,
    rate: float,
    eff_cpm: float,
    belt_capacity: float,
    base_count: int,
) -> int:
    """Compute extra driver count needed when a high-rate net partitions across sub-buses."""
    out_amt = _recipe_entry_amount(solve.Data.results_of(recipe_proto), out_name)
    drv_cap = eff_cpm * out_amt
    if rate * out_amt <= belt_capacity or drv_cap <= 0:
        return base_count
    max_drvs_bus = max(1, int((BUS_EFFECTIVE_CAPACITY_RATIO * belt_capacity) // drv_cap))
    b_max_cap = min(belt_capacity, max_drvs_bus * drv_cap)
    c_totals = _consumer_demands_for_item(data, rates, out_name)
    if len(c_totals) <= 1:
        return base_count
    whole_d = sum(d for d in c_totals if d <= b_max_cap + FLOAT_TOL)
    split_ds = [d for d in c_totals if d > b_max_cap + FLOAT_TOL]
    if not (
        MIN_WHOLE_BUS_FRACTION * b_max_cap <= whole_d <= b_max_cap + FLOAT_TOL
        and len(split_ds) == 1
    ):
        return base_count
    min_part_drvs = math.ceil(whole_d / drv_cap - CEIL_EPS) + math.ceil(
        split_ds[0] / drv_cap - CEIL_EPS
    )
    return max(base_count, min_part_drvs)


def _pow2_promoted_count(
    recipe: str,
    recipe_proto: pdk.ProtoRecord,
    rate: float,
    eff_cpm: float,
    belt_capacity: float,
    fluids: set[str],
    count: int,
) -> int:
    """Promote non-power-of-two machine count when binary splitter tree starves a branch."""
    if (
        recipe in ("boiler-steam", "steam-power")
        or count <= 2
        or (count & (count - 1)) == 0
        or eff_cpm <= 0
        or belt_capacity <= 0
    ):
        return count
    d_tree = int(math.log2(count))
    branch_rate = rate * (BINARY_SPLIT_RATIO**d_tree)
    if eff_cpm >= branch_rate - FLOAT_TOL:
        return count
    ing_raw: Any = recipe_proto.get("ingredients", [])
    item_amts = [solve.amount_of(e) for e in ing_raw if solve.name_of(e) not in fluids]
    if not item_amts or rate * max(item_amts) > belt_capacity + FLOAT_TOL:
        return count
    excess_item_rate = min(item_amts) * (branch_rate - eff_cpm)
    belt_ips = belt_capacity / pdk.TICKS_PER_SECOND
    if 0.0 < excess_item_rate <= belt_ips / POW2_EXCESS_BELT_DIVISOR + FLOAT_TOL:
        return 1 << (d_tree + 1)
    return count


def _select_recipe_machine(
    data: solve.Data,
    recipe: str,
    machine_override: str | Mapping[str, str] | None,
) -> str:
    """Resolve the building prototype name for recipe given optional overrides."""
    if isinstance(machine_override, str):
        return machine_override
    if isinstance(machine_override, Mapping):
        return machine_override.get(recipe, "assembling-machine-3")
    machine, _speed = data.machine_for(data.recipes[recipe])
    return machine


def _resolve_recipe_cell_and_count(
    data: solve.Data,
    tech: pdk.Pdk,
    nl: Netlist,
    rates: dict[str, float],
    plan_count: dict[str, float],
    machine_override: str | Mapping[str, str] | None,
    scale_for_throughput: bool,
    is_single_pack: bool,
    recipe: str,
    rate: float,
) -> tuple[dict[str, Any], float, int]:
    """Resolve PDK cell record, effective crafts/min, and integer instance count for recipe."""
    machine = _select_recipe_machine(data, recipe, machine_override)
    recipe_proto = data.recipes[recipe]
    cell = tech.cell(machine, recipe)
    if cell is None:
        raise RuntimeError(f"no cell for {machine} running {recipe}")
    eff_cpm = (
        float(cell.get("effective_crafts_per_minute", cell["crafts_per_minute"]))
        if scale_for_throughput
        else float(cell["crafts_per_minute"])
    )
    count = (
        max(1, math.ceil(rate / eff_cpm - CEIL_EPS))
        if (scale_for_throughput and eff_cpm > 0)
        else max(1, math.ceil(plan_count.get(recipe, 1.0)))
    )
    if not is_single_pack or eff_cpm <= 0:
        return cell, eff_cpm, count
    outs = cell.get("outputs", [])
    if (
        cell.get("has_fluids")
        and len(outs) > 1
        and rate / (count * eff_cpm) > FLUID_RECIPE_HEADROOM_UTIL
    ):
        count += 1
    elif nl.belt_capacity > 0 and len(outs) == 1 and recipe_proto:
        count = _partitioned_driver_count(
            data, rates, recipe_proto, outs[0], rate, eff_cpm, nl.belt_capacity, count
        )
    count = _pow2_promoted_count(
        recipe, recipe_proto, rate, eff_cpm, nl.belt_capacity, nl.fluids, count
    )
    return cell, eff_cpm, count


def _scale_steam_power_to_placed_load(
    data: solve.Data,
    tech: pdk.Pdk,
    nl: Netlist,
    rates: dict[str, float],
    raw: dict[str, float],
    plan_count: dict[str, float],
    machine_override: str | Mapping[str, str] | None,
    scale_for_throughput: bool,
    is_single_pack: bool,
    extra_power_watts: float,
) -> None:
    """Scale steam-power and boiler-steam rates to cover integer placed machines and inserters."""
    ins_watts = solve.inserter_peak_watts(data)
    placed_watts = float(extra_power_watts)
    for recipe, rate in sorted(rates.items()):
        if recipe in ("boiler-steam", "steam-power"):
            continue
        cell, _eff_cpm, count = _resolve_recipe_cell_and_count(
            data,
            tech,
            nl,
            rates,
            plan_count,
            machine_override,
            scale_for_throughput,
            is_single_pack,
            recipe,
            rate,
        )
        m_proto = data.machines.get(cell["machine"], {})
        m_watts = solve.machine_peak_watts(m_proto)
        solid_ports = sum(
            1 for p in (*cell.get("inputs", []), *cell.get("outputs", [])) if p not in nl.fluids
        )
        placed_watts += count * (m_watts + solid_ports * ins_watts)

    se_recipe = data.recipes["steam-power"]
    boiler_recipe = data.recipes["boiler-steam"]
    gen_watts = float(se_recipe.get("power_output_watts", 0.0))
    if gen_watts <= 0.0 or placed_watts <= 0.0:
        return
    se_steam = solve.amount_of(se_recipe["ingredients"][0])
    boiler_steam = solve.amount_of(solve.Data.results_of(boiler_recipe)[0])
    engines_per_boiler = boiler_steam / se_steam if se_steam > 0 else 2.0
    net_gen_watts = max(1.0, gen_watts - (ins_watts / max(1.0, engines_per_boiler)))
    req_engines = max(1, math.ceil(placed_watts / net_gen_watts - CEIL_EPS))
    se_cell = tech.cell("steam-engine", "steam-power")
    boiler_cell = tech.cell("boiler", "boiler-steam")
    if not (se_cell and boiler_cell):
        return
    req_se_rate = req_engines * float(se_cell["crafts_per_minute"])
    if req_se_rate <= rates["steam-power"]:
        return
    new_boiler_rate = (req_se_rate * se_steam) / boiler_steam
    delta_boiler_rate = new_boiler_rate - rates["boiler-steam"]
    rates["steam-power"] = req_se_rate
    rates["boiler-steam"] = new_boiler_rate
    plan_count["steam-power"] = float(req_engines)
    plan_count["boiler-steam"] = new_boiler_rate / float(boiler_cell["crafts_per_minute"])
    for ing in boiler_recipe.get("ingredients", []):
        ing_name = solve.name_of(ing)
        if ing_name in raw:
            raw[ing_name] += delta_boiler_rate * solve.amount_of(ing)


def _populate_recipe_instances(
    data: solve.Data,
    tech: pdk.Pdk,
    nl: Netlist,
    rates: dict[str, float],
    plan_count: dict[str, float],
    machine_override: str | Mapping[str, str] | None,
    scale_for_throughput: bool,
    is_single_pack: bool,
) -> None:
    """Instantiate machines for each recipe in rates and wire their input/output nets."""
    for recipe, rate in sorted(rates.items()):
        recipe_proto = data.recipes[recipe]
        cell, eff_cpm, count = _resolve_recipe_cell_and_count(
            data,
            tech,
            nl,
            rates,
            plan_count,
            machine_override,
            scale_for_throughput,
            is_single_pack,
            recipe,
            rate,
        )
        nl.cells[cell["cell"]] = cell
        share = rate / count
        ing_list = recipe_proto.get("ingredients", [])
        res_list = solve.Data.results_of(recipe_proto)
        in_amts = {p: _recipe_entry_amount(ing_list, p) for p in cell.get("inputs", [])}
        out_amts = {p: _recipe_entry_amount(res_list, p) for p in cell.get("outputs", [])}
        for index in range(count):
            inst = Instance(
                f"u_{sanitize(recipe)}_{index + 1}",
                cell["cell"],
                recipe,
                share,
                eff_cpm,
            )
            nl.instances.append(inst)
            for inp, in_amt in in_amts.items():
                in_net = nl.net(inp)
                in_net.sinks.append(inst.name)
                in_net.sink_rates[inst.name] = share * in_amt
                in_net.sink_rated[inst.name] = eff_cpm * in_amt
            for outp, amt in out_amts.items():
                out_net = nl.net(outp)
                out_net.rate_per_min += share * amt
                out_net.drivers.append(inst.name)
                out_net.driver_rates[inst.name] = share * amt
                out_net.driver_rated[inst.name] = eff_cpm * amt


def _partition_steam_subnets(nl: Netlist) -> None:
    """Split multi-boiler steam net into dedicated 1-boiler-to-2-engines fluid sub-nets."""
    if "steam" not in nl.nets or len(nl.nets["steam"].drivers) <= 1:
        return
    steam_net = nl.nets.pop("steam")
    drvs = list(steam_net.drivers)
    sinks = list(steam_net.sinks)
    for b_idx, drv in enumerate(drvs):
        sub_name = f"steam__{b_idx + 1}"
        nl.fluids.add(sub_name)
        sub_net = nl.net(sub_name)
        drv_r = steam_net.driver_rates.get(drv, 0.0)
        sub_net.drivers.append(drv)
        sub_net.driver_rates[drv] = drv_r
        sub_net.driver_rated[drv] = steam_net.driver_rated.get(drv, 0.0)
        sub_net.rate_per_min = drv_r
        for s_name in sinks[2 * b_idx : 2 * b_idx + 2]:
            sub_net.sinks.append(s_name)
            sub_net.sink_rates[s_name] = steam_net.sink_rates.get(s_name, 0.0)
            sub_net.sink_rated[s_name] = steam_net.sink_rated.get(s_name, 0.0)


def _relax_cell_depths(
    cell: pdk.ProtoRecord, depths: dict[str, int], raw: dict[str, float], max_depth: int
) -> bool:
    """Update output material depths for one cell and return True if any depth increased."""
    inps: Any = cell.get("inputs", [])
    outs: Any = cell.get("outputs", [])
    new_d = max((depths.get(p, 0) for p in inps), default=0) + 1
    if new_d > max_depth:
        return False
    updated = False
    for out_p in outs:
        if out_p not in raw and new_d > depths.get(out_p, 0):
            depths[out_p] = new_d
            updated = True
    return updated


def _compute_material_depths(nl: Netlist, raw: dict[str, float]) -> dict[str, int]:
    """Compute longest-path DAG depth for every material starting from raw inputs at depth 0."""
    depths: dict[str, int] = dict.fromkeys(raw, 0)
    max_depth = len(nl.cells) + 2
    while any(_relax_cell_depths(c, depths, raw, max_depth) for c in nl.cells.values()):
        pass
    return depths


def _centered_port_slots(
    ports: list[str], span: int, allow_wide_pair: bool = False
) -> dict[str, int]:
    """Compute 1-based centered slot indices for ports along a cell face of length span."""
    if allow_wide_pair and len(ports) == 2 and span >= MIN_SPACED_PORT_WIDTH:
        return {p: 1 + 2 * idx for idx, p in enumerate(ports)}
    offset = 1 + (span - len(ports)) // 2 if len(ports) <= span else 1
    return {p: offset + idx for idx, p in enumerate(ports)}


def _configure_cell_gaa_geometry(cell: dict[str, object], depths: dict[str, int]) -> None:
    """Sort cell inputs by DAG depth and assign multi-face GAA port slots when needed."""
    raw_inps: Any = cell.get("inputs", [])
    inps: list[str] = sorted(raw_inps, key=lambda p: (depths.get(p, 0), p))
    cell["inputs"] = inps
    if (
        not inps
        or cell.get("compound")
        or cell.get("circuit_cell")
        or cell.get("has_fluids")
        or cell.get("landing_pad")
    ):
        return
    raw_outs: Any = cell.get("outputs", [])
    outs: list[str] = list(raw_outs)
    faces = assign_gaa_pin_faces(
        inps,
        depths=depths,
        allow_east=(not outs and len(inps) > FOUR_SIDED_INPUT_THRESHOLD),
    )
    cell["port_faces"] = faces
    if not any(f in ("N", "S", "E") for f in faces.values()):
        return
    w_raw: Any = cell.get("machine_width", cell.get("width", DEFAULT_MACHINE_DIM))
    h_raw: Any = cell.get("machine_height", cell.get("height", DEFAULT_MACHINE_DIM))
    m_w, m_h = int(w_raw), int(h_raw)
    cell["width"] = m_w + 2
    cell["height"] = m_h + 2
    in_slots: dict[str, int] = {}
    in_slots.update(_centered_port_slots([p for p in inps if faces.get(p) == "W"], m_h))
    for face_dir in ("N", "S"):
        f_inps = [p for p in inps if faces.get(p) == face_dir]
        if len(f_inps) > 1:
            in_slots.update(_centered_port_slots(f_inps, m_w, allow_wide_pair=True))
    e_inps = [p for p in inps if faces.get(p) == "E"]
    if e_inps:
        in_slots.update(_centered_port_slots(e_inps, m_h))
    cell["in_slots"] = in_slots
    if outs:
        out_slots: Any = cell.setdefault("out_slots", {})
        out_slots.update(_centered_port_slots(outs, m_h))


def _assign_recipe_chunks(
    inst_list: list[Instance], start_rank: int, sort_recipes: bool = True
) -> int:
    """Assign consecutive row ranks to recipe-grouped chunks of instances and return next rank."""
    if not inst_list:
        return start_rank
    current_rank = start_rank
    by_recipe: dict[str, list[Instance]] = collections.defaultdict(list)
    for inst in inst_list:
        by_recipe[inst.recipe].append(inst)
    recipe_keys = sorted(by_recipe.keys()) if sort_recipes else list(by_recipe.keys())
    for r_name in recipe_keys:
        r_insts = by_recipe[r_name]
        for chunk_start in range(0, len(r_insts), MAX_MACHINES_PER_ROW):
            for inst in r_insts[chunk_start : chunk_start + MAX_MACHINES_PER_ROW]:
                inst.rank = current_rank
            current_rank += 1
    return current_rank


def _instance_dag_depth(inst: Instance, nl: Netlist, depths: dict[str, int]) -> int:
    """Compute effective DAG depth of a recipe instance from its output or input ports."""
    c = nl.cells.get(inst.cell_key, {})
    d = min((depths.get(p, 0) for p in c.get("outputs", [])), default=0)
    return d if d > 0 else max((depths.get(p, 0) for p in c.get("inputs", [])), default=0)


def _partition_terminal_stage_instances(
    insts: list[Instance],
    nl: Netlist,
    demand: dict[str, float],
    higher_term: list[Instance],
) -> list[Instance]:
    """Separate terminal demand-producing instances from non-terminal stage instances."""
    non_term: list[Instance] = []
    for inst in insts:
        outs = nl.cells.get(inst.cell_key, {}).get("outputs", [])
        if any(o in demand for o in outs):
            higher_term.append(inst)
        else:
            non_term.append(inst)
    return non_term


def _assign_instance_stage_ranks(
    nl: Netlist, demand: dict[str, float], depths: dict[str, int]
) -> None:
    """Partition instances by DAG depth and assign linear row ranks in chunks."""
    power_recipes = {"boiler-steam", "steam-power"}
    power_plant_insts = [inst for inst in nl.instances if inst.recipe in power_recipes]
    lab_insts = [
        inst
        for inst in nl.instances
        if inst.recipe not in power_recipes
        and nl.cells.get(inst.cell_key, {}).get("machine") == "lab"
    ]
    instances_by_depth: dict[int, list[Instance]] = collections.defaultdict(list)
    for inst in nl.instances:
        if (
            inst.recipe not in power_recipes
            and nl.cells.get(inst.cell_key, {}).get("machine") != "lab"
        ):
            instances_by_depth[_instance_dag_depth(inst, nl, depths)].append(inst)

    rank = 0
    higher_term: list[Instance] = []
    for d in sorted(instances_by_depth.keys()):
        if d < TERMINAL_STAGE_MIN_DEPTH:
            rank = _assign_recipe_chunks(instances_by_depth[d], rank, sort_recipes=True)
            continue
        non_term = _partition_terminal_stage_instances(
            instances_by_depth[d], nl, demand, higher_term
        )
        rank = _assign_recipe_chunks(non_term, rank, sort_recipes=True)

    rank = _assign_recipe_chunks(higher_term, rank, sort_recipes=False)
    rank = _assign_recipe_chunks(lab_insts, rank, sort_recipes=True)
    _assign_recipe_chunks(power_plant_insts, rank, sort_recipes=True)


def _configure_raw_input_buses(nl: Netlist, raw: dict[str, float]) -> None:
    """Set raw material input rates and divide high-rate solid inputs into power-of-two buses."""
    for material, rate in raw.items():
        net = nl.net(material)
        net.rate_per_min = rate
        if not (net.is_fluid or material in nl.fluids) and 0 < nl.belt_capacity < rate:
            num_belts = 2 ** math.ceil(math.log2(math.ceil(rate / nl.belt_capacity)))
            net.bus_inputs = [f"{material}__bus{i}" for i in range(num_belts)]
            for bus_name in net.bus_inputs:
                nl.net(bus_name).rate_per_min = rate / num_belts

    nl.inputs = []
    for m in sorted(raw):
        bus_in = nl.net(m).bus_inputs
        nl.inputs.extend(bus_in if bus_in else [m])


def build(
    data: solve.Data,
    demand: dict[str, float],
    allowed: set[str],
    machine_override: str | dict[str, str] | None = None,
    enable_compound_cells: bool = False,
    extra_power_watts: float = 0.0,
) -> Netlist:
    """Solve the plan, then lower it to instances and nets."""
    del enable_compound_cells
    tech = pdk.Pdk(data)
    rates, raw, _surplus = solve.solve_rates(
        data, demand, allowed, extra_power_watts=extra_power_watts
    )
    lines, _power = solve.machine_counts(data, rates)
    plan_count = {line[0]: line[PLAN_COUNT_TUPLE_INDEX] for line in lines}

    nl = Netlist("factory", fluids=set(data.prototypes.get("fluid", {}).keys()))
    belt_tiers = tech.belt_tiers()
    if belt_tiers:
        nl.belt_capacity = (
            float(belt_tiers[0]["items_per_lane"]) * pdk.LANES_PER_BELT * pdk.TICKS_PER_SECOND
        )

    is_single_pack = len(demand) <= 1 and "space-science-pack" not in demand
    is_full_chassis = len(demand) >= FULL_CHASSIS_PACK_COUNT or "space-science-pack" in demand
    scale_for_throughput = is_single_pack or is_full_chassis

    if "steam-power" in rates and "boiler-steam" in rates and not isinstance(machine_override, str):
        _scale_steam_power_to_placed_load(
            data,
            tech,
            nl,
            rates,
            raw,
            plan_count,
            machine_override,
            scale_for_throughput,
            is_single_pack,
            extra_power_watts,
        )

    _populate_recipe_instances(
        data,
        tech,
        nl,
        rates,
        plan_count,
        machine_override,
        scale_for_throughput,
        is_single_pack,
    )
    _partition_steam_subnets(nl)
    _configure_raw_input_buses(nl, raw)
    nl.outputs = sorted(
        m for m in demand if m in nl.nets and nl.nets[m].drivers and not nl.net(m).sinks
    )
    depths = _compute_material_depths(nl, raw)
    for cell in nl.cells.values():
        _configure_cell_gaa_geometry(cell, depths)
    nl.material_depths = depths
    _assign_instance_stage_ranks(nl, demand, depths)
    return nl


def _build_planar_merge_tree(
    items: list[str],
    default_rank: int,
    material: str,
    raw_material: str,
    count_box: list[int],
    entries: list[tuple[str, str, str, str]],
    instances: list[Instance],
    root_wire: str | None = None,
) -> str:
    """Recursively construct a binary merge tree for a list of driver wires."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        left_out, right_out = items[0], items[1]
    else:
        mid = (len(items) + 1) // 2
        left_out = _build_planar_merge_tree(
            items[:mid], default_rank, material, raw_material, count_box, entries, instances
        )
        right_out = _build_planar_merge_tree(
            items[mid:], default_rank, material, raw_material, count_box, entries, instances
        )
    count_box[0] += 1
    name = f"u_merge_{material}_{count_box[0]}"
    y_wire = root_wire if root_wire is not None else f"{material}__m{count_box[0]}"
    entries.append((name, left_out, right_out, y_wire))
    instances.append(Instance(name, MERGE_CELL, raw_material, 0.0, 0.0, rank=default_rank))
    return y_wire


def _merge_drivers_by_rank(
    drivers: list[str],
    inst_by_name: dict[str, Instance],
    material: str,
    raw_material: str,
    count_box: list[int],
    entries: list[tuple[str, str, str, str]],
    instances: list[Instance],
    root_wire: str,
) -> None:
    """Group drivers by instance rank and build a rank-aware merge tree."""
    drivers_by_rank: dict[int, list[str]] = collections.defaultdict(list)
    for d in drivers:
        inst = inst_by_name.get(d)
        r = inst.rank if inst else 0
        drivers_by_rank[r].append(f"{material}__{d}")

    sorted_ranks = sorted(drivers_by_rank.keys())
    if len(sorted_ranks) == 1:
        _build_planar_merge_tree(
            drivers_by_rank[sorted_ranks[0]],
            sorted_ranks[0],
            material,
            raw_material,
            count_box,
            entries,
            instances,
            root_wire=root_wire,
        )
        return

    rank_roots = [
        _build_planar_merge_tree(
            drivers_by_rank[r], r, material, raw_material, count_box, entries, instances
        )
        for r in sorted_ranks
    ]
    _build_planar_merge_tree(
        rank_roots,
        max(sorted_ranks),
        material,
        raw_material,
        count_box,
        entries,
        instances,
        root_wire=root_wire,
    )


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
    count_box = [0]
    inst_by_name = {inst.name: inst for inst in nl.instances}
    _merge_drivers_by_rank(
        net.drivers,
        inst_by_name,
        material,
        net.material,
        count_box,
        entries,
        nl.instances,
        root_wire=material,
    )
    net.drivers = [f"u_merge_{material}_{count_box[0]}"]
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


def _group_sinks_by_recipe(
    sinks: list[str], inst_by_name: dict[str, Instance]
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Group sink instance names by recipe and record first appearance index."""
    by_recipe: dict[str, list[str]] = collections.defaultdict(list)
    first_idx: dict[str, int] = {}
    for idx, s in enumerate(sinks):
        rec = inst_by_name[s].recipe if s in inst_by_name else ""
        if rec not in first_idx:
            first_idx[rec] = idx
        by_recipe[rec].append(s)
    return by_recipe, first_idx


def _partition_by_recipe_blocks(
    by_recipe: dict[str, list[str]],
    first_idx: dict[str, int],
    recipe_totals: dict[str, float],
    k: int,
    inst_by_name: dict[str, Instance],
) -> list[list[str]]:
    """Assign whole recipe sink groups to the least-loaded bus."""
    groups: list[list[str]] = [[] for _ in range(k)]
    bus_loads = [0.0] * k
    block_recipes = sorted(
        by_recipe.keys(),
        key=lambda r: (-round(recipe_totals[r], ROUND_DIGITS), first_idx[r]),
    )
    for rec in block_recipes:
        best_b = min(range(k), key=lambda b: (round(bus_loads[b], ROUND_DIGITS), b))
        groups[best_b].extend(by_recipe[rec])
        bus_loads[best_b] += recipe_totals[rec]
    non_empty = [g for g in groups if g]
    non_empty.sort(
        key=lambda g: (
            min((inst_by_name[s].rank for s in g if s in inst_by_name), default=0),
            _natural_key(g[0]),
        )
    )
    return non_empty


def _place_whole_recipe_on_bus(
    r_sinks: list[str],
    group_w: float,
    groups: list[list[str]],
    bus_loads: list[float],
    k: int,
    bus_max_cap: float,
) -> None:
    """Pack a whole recipe sink group onto a non-final bus if capacity allows."""
    for b in range(k - 1):
        if groups[b] and bus_loads[b] + group_w <= bus_max_cap + FLOAT_TOL:
            groups[b].extend(r_sinks)
            bus_loads[b] += group_w
            return
    for b in range(k - 1):
        if not groups[b] and group_w <= bus_max_cap + FLOAT_TOL:
            groups[b].extend(r_sinks)
            bus_loads[b] += group_w
            return
    best_b = min(range(k), key=lambda b: (round(bus_loads[b], ROUND_DIGITS), b))
    groups[best_b].extend(r_sinks)
    bus_loads[best_b] += group_w


def _try_partition_whole_and_split_recipes(
    by_recipe: dict[str, list[str]],
    first_idx: dict[str, int],
    recipe_totals: dict[str, float],
    sink_rates: dict[str, float],
    k: int,
    bus_max_cap: float,
) -> list[list[str]] | None:
    """Pack whole recipes on initial buses and spread a single split recipe across remaining buses."""
    whole_recipes = [r for r in by_recipe if recipe_totals[r] <= bus_max_cap + FLOAT_TOL]
    split_recipes = [r for r in by_recipe if recipe_totals[r] > bus_max_cap + FLOAT_TOL]
    if (
        not whole_recipes
        or len(split_recipes) != 1
        or sum(recipe_totals[r] for r in whole_recipes) < MIN_WHOLE_BUS_FRACTION * bus_max_cap
    ):
        return None
    groups: list[list[str]] = [[] for _ in range(k)]
    bus_loads = [0.0] * k
    whole_sorted = sorted(
        whole_recipes,
        key=lambda r: (first_idx[r], -round(recipe_totals[r], ROUND_DIGITS)),
    )
    for rec in whole_sorted:
        _place_whole_recipe_on_bus(
            by_recipe[rec], recipe_totals[rec], groups, bus_loads, k, bus_max_cap
        )
    empty_buses = [b for b in range(k) if not groups[b]]
    s_rec = split_recipes[0]
    s_sinks = by_recipe[s_rec]
    if (
        not empty_buses
        or len(s_sinks) < len(empty_buses)
        or recipe_totals[s_rec] / len(empty_buses) > bus_max_cap + FLOAT_TOL
    ):
        return None
    s_idx = 0
    for b_pos, b in enumerate(empty_buses):
        rem_b = len(empty_buses) - b_pos
        chunk = (len(s_sinks) - s_idx + rem_b - 1) // rem_b
        chunk_sinks = s_sinks[s_idx : s_idx + chunk]
        groups[b].extend(chunk_sinks)
        bus_loads[b] += sum(max(FLOAT_TOL, sink_rates.get(s, 1.0)) for s in chunk_sinks)
        s_idx += chunk
    return [g for g in groups if g]


def _should_advance_quota_bus(
    cur_bus: int,
    cur_added: float,
    w: float,
    rem_sinks: int,
    groups: list[list[str]],
    bus_loads: list[float],
    quotas: list[float],
    bus_max_cap: float | None,
) -> bool:
    """Return True when the quota-based partitioner should advance to the next bus."""
    k = len(groups)
    empty_after = sum(1 for b in range(cur_bus + 1, k) if not groups[b])
    if rem_sinks <= empty_after:
        return True
    if bus_max_cap is not None and bus_loads[cur_bus] + w > bus_max_cap + FLOAT_TOL:
        return True
    quota = quotas[cur_bus]
    return cur_added + w > quota and abs((cur_added + w) - quota) > abs(cur_added - quota)


def _distribute_recipe_by_quotas(
    r_sinks: list[str],
    r_weights: list[float],
    target_after: float,
    groups: list[list[str]],
    bus_loads: list[float],
    k: int,
    bus_max_cap: float | None,
) -> None:
    """Distribute a large recipe's sinks across buses proportional to remaining bus need."""
    group_w = sum(r_weights)
    needs = [max(0.0, target_after - bus_loads[b]) for b in range(k)]
    sum_needs = sum(needs)
    quotas = (
        [group_w * (nd / sum_needs) for nd in needs] if sum_needs > FLOAT_TOL else [group_w / k] * k
    )
    cur_bus = 0
    cur_added = 0.0
    n_r = len(r_sinks)
    for idx, (s, w) in enumerate(zip(r_sinks, r_weights, strict=True)):
        rem_sinks = n_r - idx
        while cur_bus < k - 1 and (cur_added > 0 or groups[cur_bus]):
            if not _should_advance_quota_bus(
                cur_bus, cur_added, w, rem_sinks, groups, bus_loads, quotas, bus_max_cap
            ):
                break
            cur_bus += 1
            cur_added = 0.0
        groups[cur_bus].append(s)
        bus_loads[cur_bus] += w
        cur_added += w


def _assign_sinks_to_least_loaded_buses(
    r_sinks: list[str],
    r_weights: list[float],
    groups: list[list[str]],
    bus_loads: list[float],
    k: int,
    distinct_buses: bool,
) -> None:
    """Assign a small recipe's sinks to the least-loaded buses."""
    if distinct_buses:
        bus_order = sorted(range(k), key=lambda b: (round(bus_loads[b], ROUND_DIGITS), b))
        for s, w, b in zip(r_sinks, r_weights, bus_order, strict=False):
            groups[b].append(s)
            bus_loads[b] += w
        return
    for s, w in zip(r_sinks, r_weights, strict=True):
        best_b = min(range(k), key=lambda b: (round(bus_loads[b], ROUND_DIGITS), b))
        groups[best_b].append(s)
        bus_loads[best_b] += w


def _partition_by_recipe_quotas(
    by_recipe: dict[str, list[str]],
    first_idx: dict[str, int],
    recipe_totals: dict[str, float],
    sink_rates: dict[str, float],
    k: int,
    bus_max_cap: float | None,
) -> list[list[str]]:
    """Partition multi-recipe sinks across k buses using load-balanced quotas."""
    groups: list[list[str]] = [[] for _ in range(k)]
    bus_loads = [0.0] * k
    sorted_recipes = sorted(
        by_recipe.keys(),
        key=lambda r: (
            -round(max(sink_rates.get(s, 1.0) for s in by_recipe[r]), ROUND_DIGITS),
            -round(recipe_totals[r], ROUND_DIGITS),
            first_idx[r],
        ),
    )
    for rec in sorted_recipes:
        r_sinks = by_recipe[rec]
        r_weights = [max(FLOAT_TOL, sink_rates.get(s, 1.0)) for s in r_sinks]
        target_after = (sum(bus_loads) + sum(r_weights)) / k
        if len(r_sinks) <= k:
            _assign_sinks_to_least_loaded_buses(
                r_sinks, r_weights, groups, bus_loads, k, distinct_buses=True
            )
        elif len(r_sinks) <= 2 * k and max(r_weights) > target_after / POW2_EXCESS_BELT_DIVISOR:
            _assign_sinks_to_least_loaded_buses(
                r_sinks, r_weights, groups, bus_loads, k, distinct_buses=False
            )
        else:
            _distribute_recipe_by_quotas(
                r_sinks, r_weights, target_after, groups, bus_loads, k, bus_max_cap
            )
    return [g for g in groups if g]


def _should_advance_sequential_bus(
    cur_bus: int,
    k: int,
    cur_group: list[str],
    rem_sinks: int,
    cur_w: float,
    w: float,
    target_w: float,
    bus_max_cap: float | None,
) -> bool:
    """Return True when the sequential weight partitioner should move to the next bus."""
    if cur_bus >= k - 1 or not cur_group:
        return False
    if rem_sinks <= (k - 1 - cur_bus):
        return True
    if bus_max_cap is not None and cur_w + w > bus_max_cap + FLOAT_TOL:
        return True
    return cur_w + w > target_w and abs((cur_w + w) - target_w) > abs(cur_w - target_w)


def _try_partition_multi_recipe_sinks(
    sinks: list[str],
    sink_rates: dict[str, float],
    k: int,
    bus_max_cap: float | None,
    inst_by_name: dict[str, Instance] | None,
) -> list[list[str]] | None:
    """Partition sinks by recipe structure when multiple recipes consume the net."""
    if inst_by_name is None:
        return None
    by_recipe, first_idx = _group_sinks_by_recipe(sinks, inst_by_name)
    if len(by_recipe) <= 1:
        return None
    recipe_totals = {
        r: sum(max(FLOAT_TOL, sink_rates.get(s, 1.0)) for s in r_sinks)
        for r, r_sinks in by_recipe.items()
    }
    if len(by_recipe) >= k and (
        bus_max_cap is None or max(recipe_totals.values()) <= bus_max_cap + FLOAT_TOL
    ):
        return _partition_by_recipe_blocks(by_recipe, first_idx, recipe_totals, k, inst_by_name)
    if bus_max_cap is not None:
        whole_split = _try_partition_whole_and_split_recipes(
            by_recipe, first_idx, recipe_totals, sink_rates, k, bus_max_cap
        )
        if whole_split is not None:
            return whole_split
    return _partition_by_recipe_quotas(
        by_recipe, first_idx, recipe_totals, sink_rates, k, bus_max_cap
    )


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

    multi_recipe_groups = _try_partition_multi_recipe_sinks(
        sinks, sink_rates, k, bus_max_cap, inst_by_name
    )
    if multi_recipe_groups is not None:
        return multi_recipe_groups

    weights = [max(FLOAT_TOL, sink_rates.get(s, 1.0)) for s in sinks]
    target_w = sum(weights) / k
    groups: list[list[str]] = [[] for _ in range(k)]
    cur_bus = 0
    cur_w = 0.0
    n_sinks = len(sinks)
    for idx, (s, w) in enumerate(zip(sinks, weights, strict=True)):
        if _should_advance_sequential_bus(
            cur_bus, k, groups[cur_bus], n_sinks - idx, cur_w, w, target_w, bus_max_cap
        ):
            cur_bus += 1
            cur_w = 0.0
        groups[cur_bus].append(s)
        cur_w += w
    return [g for g in groups if g]


class _SplitTreeContext:
    """State and rank/weight lookup tables for split tree synthesis on a net."""

    def __init__(
        self,
        nl: Netlist,
        net: Net,
        inst_by_name: dict[str, Instance],
        synthesize_trees: bool,
    ) -> None:
        self.nl = nl
        self.net = net
        self.material = sanitize(net.material)
        self.inst_by_name = inst_by_name
        self.synthesize_trees = synthesize_trees
        self.entries: list[tuple[str, str, str, str]] = []
        self.count = [0]
        self.sink_ranks = [inst_by_name[s].rank for s in net.sinks if s in inst_by_name]
        self.driver_ranks = [inst_by_name[d].rank for d in net.drivers if d in inst_by_name]
        self.is_primary = (net.material in nl.inputs) or bool(
            self.sink_ranks and self.driver_ranks and min(self.driver_ranks) < min(self.sink_ranks)
        )
        self.rank = self._compute_base_rank()
        has_labs = any(c.get("machine") == "lab" for c in nl.cells.values())
        self.is_single_pack = (
            len(nl.outputs) <= 1 and "space-science-pack" not in nl.outputs and not has_labs
        )
        self.has_labs = has_labs
        self.wire_target_rank: dict[str, int] = {}
        self.wire_weight: dict[str, float] = {}
        for s in net.sinks:
            w_s = max(FLOAT_TOL, net.sink_rates.get(s, 1.0))
            self.wire_weight[s] = w_s
            self.wire_weight[f"{self.material}__{s}"] = w_s
            if s in inst_by_name:
                self.wire_target_rank[s] = inst_by_name[s].rank
                self.wire_target_rank[f"{self.material}__{s}"] = inst_by_name[s].rank

    def _compute_base_rank(self) -> int:
        if self.is_primary and self.sink_ranks and min(self.sink_ranks) > 0:
            return min(self.sink_ranks)
        if self.driver_ranks:
            return min(self.driver_ranks)
        return min(self.sink_ranks) if self.sink_ranks else 0

    def sink_rank(self, w: str, default: int) -> int:
        """Resolve the target rank for a sink or intermediate wire."""
        if w in self.wire_target_rank:
            return self.wire_target_rank[w]
        m = re.search(r"__rf_(\d+)_", w)
        return int(m.group(1)) if m else default

    def sink_weight(self, w: str) -> float:
        """Resolve the demand weight for a sink or intermediate wire."""
        return max(FLOAT_TOL, self.wire_weight.get(w, 1.0))


def _is_dominant_edge_sink(
    edge_w: float,
    neighbor_w: float,
    opposite_w: float,
    eff_flow: float,
    num_sinks: int,
    unbacked_flow: float | None,
) -> bool:
    """Return True when the first or last sink dominates flow enough to split off alone."""
    if edge_w <= eff_flow / SPLIT_DOMINANT_SINK_RATIO + FLOAT_TOL:
        return False
    if edge_w > neighbor_w + FLOAT_TOL:
        return True
    return (
        unbacked_flow is None
        and num_sinks >= SPLIT_DOMINANT_MIN_SINKS
        and edge_w >= SPLIT_DOMINANT_SINK_RATIO * opposite_w - FLOAT_TOL
    )


def _choose_balanced_split_index(
    ctx: _SplitTreeContext,
    sinks: list[str],
    weights: list[float],
    total_w: float,
    r: int,
) -> int:
    """Find the split index that best balances flow weights and rank boundaries."""
    best_mid = len(sinks) // 2
    best_key: tuple[float, int, int] | None = None
    acc_w = 0.0
    for idx in range(1, len(sinks)):
        acc_w += weights[idx - 1]
        diff = round(abs(acc_w - (total_w - acc_w)), ROUND_DIGITS)
        rank_boundary = 0 if ctx.sink_rank(sinks[idx - 1], r) != ctx.sink_rank(sinks[idx], r) else 1
        cand_key = (diff, rank_boundary, abs(idx - len(sinks) // 2))
        if best_key is None or cand_key < best_key:
            best_key = cand_key
            best_mid = idx
    return best_mid


def _select_split_midpoint(
    ctx: _SplitTreeContext,
    sinks: list[str],
    weights: list[float],
    total_w: float,
    r: int,
    unbacked_flow: float | None,
) -> int:
    """Choose the split index dividing sinks into left and right subtrees."""
    if not ctx.is_single_pack:
        return len(sinks) // 2
    eff_flow = min(total_w, unbacked_flow) if unbacked_flow is not None else total_w
    if _is_dominant_edge_sink(
        weights[0], weights[1], weights[-1], eff_flow, len(sinks), unbacked_flow
    ):
        return 1
    if _is_dominant_edge_sink(
        weights[-1], weights[-2], weights[0], eff_flow, len(sinks), unbacked_flow
    ):
        return len(sinks) - 1
    return _choose_balanced_split_index(ctx, sinks, weights, total_w, r)


def _child_subtree_rank(
    ctx: _SplitTreeContext, branch_sinks: list[str], branch_min_rank: int, parent_rank: int
) -> int:
    """Compute the rank assigned to a child split subtree."""
    if ctx.is_single_pack:
        return branch_min_rank
    branch_max_rank = max(
        (ctx.sink_rank(w, parent_rank) for w in branch_sinks), default=parent_rank
    )
    return branch_min_rank if branch_min_rank == branch_max_rank else parent_rank


def _register_branch_wire(
    ctx: _SplitTreeContext, branch: list[str], branch_rank: int, suffix: int
) -> str:
    """Register target rank and weight for a split branch wire and return its name."""
    w_name = branch[0] if len(branch) == 1 else f"{ctx.material}__sp_{ctx.count[0]}_{suffix}"
    ctx.wire_target_rank[w_name] = branch_rank
    ctx.wire_weight[w_name] = sum(ctx.sink_weight(w) for w in branch)
    return w_name


def _build_split_tree(
    ctx: _SplitTreeContext,
    in_wire: str,
    sinks: list[str],
    tree_rank: int | None = None,
    unbacked_flow: float | None = None,
) -> None:
    """Recursively synthesize belt_split2 instances for a list of sink wires."""
    if not ctx.synthesize_trees or len(sinks) <= 1:
        return
    ctx.count[0] += 1
    name = f"u_split_{ctx.material}_{ctx.count[0]}"
    r = ctx.rank if tree_rank is None else tree_rank
    if len(sinks) == 2:
        r0, r1 = ctx.sink_rank(sinks[0], r), ctx.sink_rank(sinks[1], r)
        swap = r <= min(r0, r1) and r0 < r1
        y1_wire, y2_wire = (sinks[1], sinks[0]) if swap else (sinks[0], sinks[1])
        ctx.entries.append((name, in_wire, y1_wire, y2_wire))
        ctx.nl.instances.append(Instance(name, SPLIT_CELL, ctx.net.material, 0.0, 0.0, rank=r))
        return

    weights = [ctx.sink_weight(w) for w in sinks]
    total_w = sum(weights)
    mid = _select_split_midpoint(ctx, sinks, weights, total_w, r, unbacked_flow)
    left, right = sinks[:mid], sinks[mid:]
    r_left = min((ctx.sink_rank(w, r) for w in left), default=r)
    r_right = min((ctx.sink_rank(w, r) for w in right), default=r)
    left_rank = _child_subtree_rank(ctx, left, r_left, r)
    right_rank = _child_subtree_rank(ctx, right, r_right, r)

    w_left = _register_branch_wire(ctx, left, r_left, 1)
    w_right = _register_branch_wire(ctx, right, r_right, 2)
    child_flow = (unbacked_flow or total_w) / 2.0

    if r <= min(r_left, r_right) and r_left < r_right:
        w_left, w_right = w_right, w_left
        left, right = right, left
        left_rank, right_rank = right_rank, left_rank

    ctx.entries.append((name, in_wire, w_left, w_right))
    ctx.nl.instances.append(Instance(name, SPLIT_CELL, ctx.net.material, 0.0, 0.0, rank=r))
    _build_split_tree(ctx, w_left, left, tree_rank=left_rank, unbacked_flow=child_flow)
    _build_split_tree(ctx, w_right, right, tree_rank=right_rank, unbacked_flow=child_flow)


def _choose_rank_grouped_tree_rank(ctx: _SplitTreeContext, sorted_sink_ranks: list[int]) -> int:
    """Select the root split rank for a rank-grouped sink list."""
    if len(sorted_sink_ranks) > 1:
        return min(ctx.rank, min(sorted_sink_ranks))
    target_rank = sorted_sink_ranks[0] if sorted_sink_ranks else ctx.rank
    if (
        ctx.is_single_pack
        and ctx.driver_ranks
        and ctx.net.material not in ctx.nl.inputs
        and target_rank > max(ctx.driver_ranks) + 1
    ):
        return (max(ctx.driver_ranks) + target_rank) // 2
    return ctx.rank if (ctx.driver_ranks and not ctx.is_primary) else target_rank


def _build_rank_grouped_tree(
    ctx: _SplitTreeContext,
    in_wire: str,
    sink_names: list[str],
    bus_supply: float | None = None,
) -> None:
    """Order sinks by rank and demand and build a split tree rooted at `in_wire`."""
    if not ctx.is_single_pack:
        ordered_sinks = sorted(
            sink_names,
            key=lambda s: (
                ctx.inst_by_name[s].rank if s in ctx.inst_by_name else ctx.rank,
                _natural_key(s),
            ),
        )
    else:
        ordered_sinks = sorted(
            sink_names,
            key=lambda s: (
                ctx.inst_by_name[s].rank if s in ctx.inst_by_name else ctx.rank,
                -round(ctx.net.sink_rates.get(s, 1.0), ROUND_DIGITS),
                _natural_key(s),
            ),
        )
    sorted_sink_ranks = sorted(
        {ctx.inst_by_name[s].rank if s in ctx.inst_by_name else ctx.rank for s in ordered_sinks}
    )
    sub_targets = [f"{ctx.material}__{s}" for s in ordered_sinks]
    chosen_rank = _choose_rank_grouped_tree_rank(ctx, sorted_sink_ranks)
    _build_split_tree(ctx, in_wire, sub_targets, tree_rank=chosen_rank, unbacked_flow=bus_supply)


def _is_multibus_eligible(
    ctx: _SplitTreeContext,
    orig_drivers: list[str],
    needs_multibus_for_heavy_sink: bool,
    has_multi_recipe_sinks: bool,
) -> bool:
    """Return True if a net qualifies for multi-bus parallel merge/split partitioning."""
    nl, net = ctx.nl, ctx.net
    belt_cap = nl.belt_capacity
    if (
        not ctx.synthesize_trees
        or net.bus_inputs
        or net.material in nl.inputs
        or net.material in nl.outputs
        or belt_cap <= 0
        or len(orig_drivers) < 2
        or len(net.sinks) < 2
        or net.material not in getattr(nl, "merge_plans", {})
    ):
        return False
    if not (net.rate_per_min > belt_cap or needs_multibus_for_heavy_sink or has_multi_recipe_sinks):
        return False
    if ctx.is_single_pack:
        return True
    is_full_chassis = (
        len(nl.outputs) >= FULL_CHASSIS_PACK_COUNT
        or "space-science-pack" in nl.outputs
        or ctx.has_labs
    )
    return (
        is_full_chassis
        and net.rate_per_min > belt_cap
        and len(orig_drivers) >= MULTIBUS_CHASSIS_MIN_ENDPOINTS
        and len(net.sinks) >= MULTIBUS_CHASSIS_MIN_ENDPOINTS
    )


def _rebalance_small_sink_groups(s_groups: list[list[str]]) -> bool:
    """Ensure every sink group in a multi-pack build has at least two sinks."""
    while any(len(g) < 2 for g in s_groups) and any(len(g) > 2 for g in s_groups):
        small_idx = next(i for i, g in enumerate(s_groups) if len(g) < 2)
        big_idx = max(range(len(s_groups)), key=lambda i: len(s_groups[i]))
        s_groups[small_idx].append(s_groups[big_idx].pop())
    return all(len(g) >= 2 for g in s_groups)


def _has_mixed_overcap_recipe_group(
    s_groups: list[list[str]],
    recipe_totals: dict[str, float],
    bus_max_cap: float,
    inst_by_name: dict[str, Instance],
) -> bool:
    """Return True if any sink group mixes an over-capacity recipe with another recipe."""
    if not any(v > bus_max_cap + FLOAT_TOL for v in recipe_totals.values()):
        return False
    for grp in s_groups:
        grp_recipes = {inst_by_name[s].recipe for s in grp if s in inst_by_name}
        if len(grp_recipes) > 1 and any(
            recipe_totals.get(r, 0.0) > bus_max_cap + FLOAT_TOL for r in grp_recipes
        ):
            return True
    return False


def _should_skip_for_larger_k(
    cand_k: int,
    cand_ks: list[int],
    force_multibus: bool,
    s_groups: list[list[str]],
    ordered_sinks: list[str],
    ordered_drivers: list[str],
    net: Net,
    recipe_totals: dict[str, float],
    bus_max_cap: float,
    max_drv_rated: float,
    inst_by_name: dict[str, Instance],
) -> bool:
    """Return True if cand_ks[1] can cleanly separate mixed over-capacity recipes."""
    if (
        not force_multibus
        or len(cand_ks) <= 1
        or cand_k != cand_ks[0]
        or max_drv_rated <= 0
        or not _has_mixed_overcap_recipe_group(s_groups, recipe_totals, bus_max_cap, inst_by_name)
    ):
        return False
    next_groups = _partition_sinks_by_demand(
        ordered_sinks,
        net.sink_rates,
        cand_ks[1],
        bus_max_cap=bus_max_cap,
        inst_by_name=inst_by_name,
    )
    next_needed = sum(
        math.ceil(
            (sum(max(FLOAT_TOL, net.sink_rates.get(s, 1.0)) for s in grp) - FLOAT_TOL)
            / max_drv_rated
        )
        for grp in next_groups
    )
    return next_needed <= len(ordered_drivers)


def _partition_drivers_for_buses(
    ordered_drivers: list[str],
    b_demands: list[float],
    min_drvs: list[int],
    max_drv_rated: float,
) -> list[list[str]]:
    """Partition ordered drivers across sub-buses to match bus demands."""
    eff_k = len(b_demands)
    n_drvs = len(ordered_drivers)
    needed_drvs = (
        [
            max(min_drvs[b], math.ceil((b_demands[b] - FLOAT_TOL) / max_drv_rated))
            for b in range(eff_k)
        ]
        if max_drv_rated > 0
        else []
    )
    if needed_drvs and sum(needed_drvs) == n_drvs:
        d_groups: list[list[str]] = []
        prev_idx = 0
        for count_b in needed_drvs:
            d_groups.append(ordered_drivers[prev_idx : prev_idx + count_b])
            prev_idx += count_b
        return d_groups

    tot_demand = sum(b_demands)
    d_groups = []
    prev_idx = 0
    cum_demand = 0.0
    for b in range(eff_k):
        cum_demand += b_demands[b]
        if b == eff_k - 1:
            next_idx = n_drvs
        else:
            raw_ideal = (cum_demand / tot_demand) * n_drvs
            ideal_idx = (
                math.ceil(raw_ideal - CEIL_EPS)
                if b_demands[b] < (tot_demand / eff_k) - FLOAT_TOL
                else round(raw_ideal)
            )
            min_idx = prev_idx + min_drvs[b]
            max_idx = n_drvs - sum(min_drvs[b + 1 :])
            next_idx = max(min_idx, min(max_idx, ideal_idx))
        d_groups.append(ordered_drivers[prev_idx:next_idx])
        prev_idx = next_idx
    return d_groups


def _candidate_multibus_ks(
    force_multibus: bool,
    min_buses_by_drv: int,
    min_buses_by_rate: int,
    num_drivers: int,
    max_sink_groups: int,
    num_sink_recipes: int,
) -> list[int]:
    """Compute candidate sub-bus counts k to evaluate."""
    if force_multibus:
        max_f_k = min(num_drivers // 2, max_sink_groups)
        base_k = min(max(2, min_buses_by_drv, min_buses_by_rate), max_f_k)
        ks = [base_k, base_k + 1] if (num_sink_recipes > 1 and base_k + 1 <= max_f_k) else [base_k]
        return [k for k in ks if k >= 2]
    max_k = min(num_sink_recipes, num_drivers - 1, max_sink_groups)
    return list(range(max_k, 1, -1))


def _evaluate_multibus_candidate(
    ctx: _SplitTreeContext,
    cand_k: int,
    cand_ks: list[int],
    force_multibus: bool,
    ordered_sinks: list[str],
    ordered_drivers: list[str],
    recipe_totals: dict[str, float],
    bus_max_cap: float,
    max_drv_rated: float,
) -> tuple[list[list[str]], list[list[str]], list[float], list[float]] | None:
    """Evaluate a single candidate k for multibus partitioning."""
    net = ctx.net
    s_groups = _partition_sinks_by_demand(
        ordered_sinks,
        net.sink_rates,
        cand_k,
        bus_max_cap=bus_max_cap,
        inst_by_name=ctx.inst_by_name,
    )
    if not ctx.is_single_pack and not _rebalance_small_sink_groups(s_groups):
        return None
    eff_k = len(s_groups)
    if eff_k < 2 or _should_skip_for_larger_k(
        cand_k,
        cand_ks,
        force_multibus,
        s_groups,
        ordered_sinks,
        ordered_drivers,
        net,
        recipe_totals,
        bus_max_cap,
        max_drv_rated,
        ctx.inst_by_name,
    ):
        return None
    min_drvs = [2 if (force_multibus or len(grp) < 2) else 1 for grp in s_groups]
    if sum(min_drvs) > len(ordered_drivers):
        return None
    b_demands = [sum(max(FLOAT_TOL, net.sink_rates.get(s, 1.0)) for s in grp) for grp in s_groups]
    d_groups = _partition_drivers_for_buses(ordered_drivers, b_demands, min_drvs, max_drv_rated)
    b_supplies = [
        sum(net.driver_rated.get(d, net.driver_rates.get(d, 0.0)) for d in d_grp)
        for d_grp in d_groups
    ]
    if force_multibus or all(
        b_supplies[b] >= MULTIBUS_SUPPLY_RATIO * b_demands[b] - FLOAT_TOL for b in range(eff_k)
    ):
        return (s_groups, d_groups, b_demands, b_supplies)
    return None


def _compute_driver_bus_limits(
    net: Net, ordered_drivers: list[str], belt_cap: float
) -> tuple[float, int, float]:
    """Compute max driver rated capacity, max drivers per bus, and per-bus capacity limit."""
    eff_cap = BUS_EFFECTIVE_CAPACITY_RATIO * belt_cap
    max_drv_rated = max(
        (net.driver_rated.get(d, net.driver_rates.get(d, 0.0)) for d in ordered_drivers),
        default=0.0,
    )
    if max_drv_rated <= 0:
        return 0.0, len(ordered_drivers), belt_cap
    max_drvs_per_bus = max(1, int(eff_cap // max_drv_rated))
    bus_max_cap = min(belt_cap, max_drvs_per_bus * max_drv_rated)
    return max_drv_rated, max_drvs_per_bus, bus_max_cap


def _plan_multibus_partition(
    ctx: _SplitTreeContext, orig_drivers: list[str]
) -> tuple[list[list[str]], list[list[str]], list[float], list[float]] | None:
    """Plan a parallel sub-bus partition of drivers and sinks when beneficial."""
    net, inst_by_name = ctx.net, ctx.inst_by_name
    belt_cap = ctx.nl.belt_capacity
    max_sink_rate = max((net.sink_rates.get(s, 0.0) for s in net.sinks), default=0.0)
    needs_heavy = (
        len(orig_drivers) >= MULTIBUS_HEAVY_SINK_MIN_DRIVERS
        and max_sink_rate > BINARY_SPLIT_RATIO * net.rate_per_min + FLOAT_TOL
    )
    sink_recipes = {inst_by_name[s].recipe for s in net.sinks if s in inst_by_name}
    has_multi_recipe = (
        len(orig_drivers) >= MULTIBUS_MULTI_RECIPE_MIN_DRIVERS and len(sink_recipes) >= 2
    )
    if not _is_multibus_eligible(ctx, orig_drivers, needs_heavy, has_multi_recipe):
        return None

    ordered_drivers = sorted(
        orig_drivers,
        key=lambda d: (inst_by_name[d].rank if d in inst_by_name else 0, _natural_key(d)),
    )
    ordered_sinks = sorted(
        net.sinks,
        key=lambda s: (inst_by_name[s].rank if s in inst_by_name else ctx.rank, _natural_key(s)),
    )
    max_drv_rated, max_drvs_per_bus, bus_max_cap = _compute_driver_bus_limits(
        net, ordered_drivers, belt_cap
    )
    force_multibus = net.rate_per_min > belt_cap or needs_heavy
    max_sink_groups = len(ordered_sinks) if ctx.is_single_pack else len(ordered_sinks) // 2
    cand_ks = _candidate_multibus_ks(
        force_multibus,
        math.ceil(len(ordered_drivers) / max_drvs_per_bus),
        math.ceil(net.rate_per_min / (BUS_EFFECTIVE_CAPACITY_RATIO * belt_cap)),
        len(ordered_drivers),
        max_sink_groups,
        len(sink_recipes),
    )
    recipe_totals = {
        r: sum(
            max(FLOAT_TOL, net.sink_rates.get(s, 1.0))
            for s in ordered_sinks
            if s in inst_by_name and inst_by_name[s].recipe == r
        )
        for r in sink_recipes
    }
    for cand_k in cand_ks:
        plan = _evaluate_multibus_candidate(
            ctx,
            cand_k,
            cand_ks,
            force_multibus,
            ordered_sinks,
            ordered_drivers,
            recipe_totals,
            bus_max_cap,
            max_drv_rated,
        )
        if plan is not None:
            return plan
    return None


def _retarget_balancer_output(
    bal_entries: list[tuple[str, str, str, str, str]], out_wire: str, single_target: str
) -> None:
    """Rewrite a balancer output wire directly to a single sink target wire."""
    for idx, (b_name, b_a1, b_a2, b_y1, b_y2) in enumerate(bal_entries):
        if b_y1 == out_wire:
            bal_entries[idx] = (b_name, b_a1, b_a2, single_target, b_y2)
        elif b_y2 == out_wire:
            bal_entries[idx] = (b_name, b_a1, b_a2, b_y1, single_target)


def _split_balanced_bus_inputs(
    ctx: _SplitTreeContext,
    bal_entries: list[tuple[str, str, str, str, str]],
    bal_count: list[int],
) -> None:
    """Balance multi-belt raw input buses and build split trees per balanced output."""
    nl, net = ctx.nl, ctx.net
    bus_wires = [sanitize(b) for b in net.bus_inputs]
    balanced_outputs = _balance_inputs(
        nl, ctx.material, net.material, bus_wires, bal_entries, bal_count
    )
    if not ctx.is_single_pack:
        k_bal = len(balanced_outputs)
        chunk_size = len(net.sinks) / k_bal
        sink_groups = [
            net.sinks[round(i * chunk_size) : round((i + 1) * chunk_size)] for i in range(k_bal)
        ]
    else:
        ordered_sinks = sorted(
            net.sinks,
            key=lambda s: (
                ctx.inst_by_name[s].rank if s in ctx.inst_by_name else ctx.rank,
                _natural_key(s),
            ),
        )
        sink_groups = _partition_sinks_by_demand(
            ordered_sinks, net.sink_rates, len(balanced_outputs), inst_by_name=ctx.inst_by_name
        )
    if not ctx.synthesize_trees:
        return
    for out_wire, sub_sink_names in zip(balanced_outputs, sink_groups, strict=False):
        if len(sub_sink_names) > 1:
            _build_rank_grouped_tree(ctx, out_wire, sub_sink_names)
        elif len(sub_sink_names) == 1:
            _retarget_balancer_output(bal_entries, out_wire, f"{ctx.material}__{sub_sink_names[0]}")


def _retarget_merge_output(
    merge_entries: list[tuple[str, str, str, str]], in_w: str, single_target: str
) -> None:
    """Rewrite a sub-bus root merger output wire directly to a single sink target wire."""
    for idx, (m_name, m_a, m_b, m_y) in enumerate(merge_entries):
        if m_y == in_w:
            merge_entries[idx] = (m_name, m_a, m_b, single_target)


def _rebuild_subbus_merges(ctx: _SplitTreeContext, drv_groups: list[list[str]]) -> list[str]:
    """Rebuild merge trees per driver group and replace net merge instances in place."""
    nl, net = ctx.nl, ctx.net
    new_merge_entries: list[tuple[str, str, str, str]] = []
    new_merge_insts: list[Instance] = []
    m_count_box = [0]
    bus_wires: list[str] = []
    bus_root_mergers: list[str] = []

    for b, d_grp in enumerate(drv_groups):
        if len(d_grp) == 1:
            bus_wires.append(f"{ctx.material}__{d_grp[0]}")
            bus_root_mergers.append(d_grp[0])
            continue
        b_wire = f"{ctx.material}__bus{b}"
        bus_wires.append(b_wire)
        _merge_drivers_by_rank(
            d_grp,
            ctx.inst_by_name,
            ctx.material,
            net.material,
            m_count_box,
            new_merge_entries,
            new_merge_insts,
            root_wire=b_wire,
        )
        bus_root_mergers.append(f"u_merge_{ctx.material}_{m_count_box[0]}")

    nl.instances = [
        inst
        for inst in nl.instances
        if not (inst.cell_key == MERGE_CELL and inst.recipe == net.material)
    ]
    nl.instances.extend(new_merge_insts)
    nl.merge_plans[net.material][:] = new_merge_entries
    net.sub_buses = list(bus_wires)
    net.drivers = [bus_root_mergers[0]]
    return bus_wires


def _apply_multibus_plan(
    ctx: _SplitTreeContext,
    multibus_plan: tuple[list[list[str]], list[list[str]], list[float], list[float]],
) -> None:
    """Rebuild per-sub-bus merge trees and synthesize split trees for each sub-bus."""
    sink_groups, drv_groups, _bus_demands, bus_supplies = multibus_plan
    bus_wires = _rebuild_subbus_merges(ctx, drv_groups)
    for in_w, s_grp, b_sup in zip(bus_wires, sink_groups, bus_supplies, strict=True):
        if len(s_grp) > 1:
            _build_rank_grouped_tree(ctx, in_w, s_grp, bus_supply=b_sup if b_sup > 0 else None)
        elif len(s_grp) == 1:
            _retarget_merge_output(
                ctx.nl.merge_plans[ctx.net.material], in_w, f"{ctx.material}__{s_grp[0]}"
            )


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
    inst_by_name = {inst.name: inst for inst in nl.instances}
    ctx = _SplitTreeContext(nl, net, inst_by_name, synthesize_trees)
    orig_drivers = net.orig_drivers or list(net.drivers)
    multibus_plan = _plan_multibus_partition(ctx, orig_drivers)

    if len(net.bus_inputs) > 1 and bal_entries is not None:
        _split_balanced_bus_inputs(ctx, bal_entries, [0])
    elif multibus_plan is not None:
        _apply_multibus_plan(ctx, multibus_plan)
    elif synthesize_trees:
        _build_rank_grouped_tree(ctx, ctx.material, net.sinks)

    root_entries = ctx.entries or bal_entries
    if root_entries:
        net.sinks = [root_entries[0][0]]
    return ctx.entries


def _declare_plan_wires(
    plan_dict: Mapping[str, Sequence[Sequence[str]]],
    declared: set[str],
    skip_wires: set[str],
    out: list[str],
) -> None:
    """Declare intermediate plan wires not yet in `declared` or `skip_wires`."""
    for material, plan in sorted(plan_dict.items()):
        bare = sanitize(material)
        for entry in plan:
            for wire in entry[1:]:
                if wire == bare or wire in declared or wire in skip_wires:
                    continue
                declared.add(wire)
                out.append(f"  wire {wire};")


def _declare_verilog_wires(
    nl: Netlist,
    input_wires: list[str],
    merge_plans: dict[str, list[tuple[str, str, str, str]]],
    split_plans: dict[str, list[tuple[str, str, str, str]]],
    balancer_plans: dict[str, list[tuple[str, str, str, str, str]]],
    out: list[str],
) -> set[str]:
    """Emit wire declarations for internal nets, transport plans, and combinators."""
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
    _declare_plan_wires(merge_plans, declared, set(), out)
    _declare_plan_wires(balancer_plans, declared, set(input_wires), out)
    _declare_plan_wires(split_plans, declared, set(), out)
    for inst in nl.instances:
        if inst.cell_key == pdk.ARITHMETIC_COMBINATOR_CELL:
            c_out_wire = f"c_out_{sanitize(inst.recipe)}"
            if c_out_wire not in declared:
                declared.add(c_out_wire)
                out.append(f"  wire {c_out_wire};")
    return declared


def _try_emit_special_instance_verilog(
    inst: Instance,
    merge_plans: dict[str, list[tuple[str, str, str, str]]],
    split_plans: dict[str, list[tuple[str, str, str, str]]],
    balancer_plans: dict[str, list[tuple[str, str, str, str, str]]],
    declared: set[str],
    out: list[str],
) -> bool:
    """Emit Verilog for transport or telemetry cells; return True if handled."""
    if inst.cell_key == MERGE_CELL:
        _name, a_wire, b_wire, y_wire = next(
            e for e in merge_plans[inst.recipe] if e[0] == inst.name
        )
        out.append(f"  {MERGE_CELL} {inst.name} (.a({a_wire}), .b({b_wire}), .y({y_wire}));")
        return True
    if inst.cell_key == BALANCER_CELL:
        b_plan = balancer_plans.get(inst.recipe) or balancer_plans.get(sanitize(inst.recipe), [])
        b_entry = next((e for e in b_plan if e[0] == inst.name), None)
        if b_entry:
            _name, a1_w, a2_w, y1_w, y2_w = b_entry
            out.append(
                f"  {BALANCER_CELL} {inst.name} (.a1({a1_w}), .a2({a2_w}), .y1({y1_w}), .y2({y2_w}));"
            )
        return True
    if inst.cell_key == SPLIT_CELL:
        _name, a_wire, y1_wire, y2_wire = next(
            e for e in split_plans[inst.recipe] if e[0] == inst.name
        )
        out.append(f"  {SPLIT_CELL} {inst.name} (.a({a_wire}), .y1({y1_wire}), .y2({y2_wire}));")
        return True
    if inst.cell_key == pdk.DISPLAY_PANEL_CELL:
        mat_wire = sanitize(inst.recipe)
        c_out_wire = f"c_out_{mat_wire}"
        conn_wire = c_out_wire if c_out_wire in declared else f"c_disp_{mat_wire}"
        out.append(f"  // telemetry display monitor for {inst.recipe}")
        out.append(f"  {pdk.DISPLAY_PANEL_CELL} {inst.name} (.a({conn_wire}));")
        return True
    if inst.cell_key == pdk.ARITHMETIC_COMBINATOR_CELL and inst.name.startswith("u_acc_"):
        mat_wire = sanitize(inst.recipe)
        out.append(f"  // telemetry accumulator counter for {inst.recipe}")
        out.append(
            f"  {pdk.ARITHMETIC_COMBINATOR_CELL} {inst.name} (.a(c_in_{mat_wire}), .y(c_out_{mat_wire}));"
        )
        return True
    return False


def _emit_circuit_cell_verilog(
    nl: Netlist,
    inst: Instance,
    declared: set[str],
    wire_insert_idx: int,
    out: list[str],
) -> None:
    """Emit Verilog instantiation and any implicit wires for a custom circuit cell."""
    cell = nl.cells[inst.cell_key]
    conns: list[str] = []
    if "a" in cell.get("inputs", []):
        in_wire = next(
            (sanitize(n) for n, net in nl.nets.items() if inst.name in net.sinks),
            f"c_{inst.name}_in",
        )
        if in_wire not in declared:
            declared.add(in_wire)
            out.insert(wire_insert_idx, f"  wire {in_wire};")
        conns.append(f".a({in_wire})")
    if "y" in cell.get("outputs", []):
        out_wire = next(
            (sanitize(n) for n, net in nl.nets.items() if inst.name in net.drivers),
            f"c_{inst.name}_out",
        )
        if out_wire not in declared:
            declared.add(out_wire)
            out.insert(wire_insert_idx, f"  wire {out_wire};")
        conns.append(f".y({out_wire})")
    out.append(f"  {sanitize(inst.cell_key)} {inst.name} (")
    out.append(",\n".join(f"    {c}" for c in conns))
    out.append("  );")


def _resolve_port_wire(
    nl: Netlist,
    inst_name: str,
    port: str,
    is_input: bool,
    merge_plans: Mapping[str, object] | None,
    split_plans: Mapping[str, object] | None,
    balancer_plans: Mapping[str, object] | None,
) -> str:
    """Resolve the connected wire name for a cell input or output port."""
    if is_input and (
        (split_plans and port in split_plans) or (balancer_plans and port in balancer_plans)
    ):
        return f"{sanitize(port)}__{inst_name}"
    if not is_input and merge_plans and port in merge_plans:
        return f"{sanitize(port)}__{inst_name}"
    if port in nl.nets:
        return sanitize(port)
    return next(
        (
            sanitize(n)
            for n, net_obj in nl.nets.items()
            if n.startswith(f"{port}__")
            and inst_name in (net_obj.sinks if is_input else net_obj.drivers)
        ),
        sanitize(port),
    )


def _emit_recipe_cell_verilog(
    nl: Netlist,
    inst: Instance,
    merge_plans: dict[str, list[tuple[str, str, str, str]]],
    split_plans: dict[str, list[tuple[str, str, str, str]]],
    balancer_plans: dict[str, list[tuple[str, str, str, str, str]]],
    out: list[str],
) -> None:
    """Emit Verilog instantiation for a crafting recipe cell."""
    cell = nl.cells[inst.cell_key]
    conns = [
        f".{sanitize(p)}({_resolve_port_wire(nl, inst.name, p, True, merge_plans, split_plans, balancer_plans)})"
        for p in cell["inputs"]
    ]
    conns.extend(
        f".{sanitize(p)}({_resolve_port_wire(nl, inst.name, p, False, merge_plans, split_plans, balancer_plans)})"
        for p in cell["outputs"]
    )
    util = f"{inst.plan_rate / inst.rated * 100:.0f}%" if inst.rated else "-"
    out.append(f"  // plan {inst.plan_rate:.2f}/min of {inst.rated:.2f}/min rated, {util}")
    out.append(f"  {sanitize(inst.cell_key)} {inst.name} (")
    out.append(",\n".join(f"    {c}" for c in conns))
    out.append("  );")


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
    splits = split_plans if split_plans is not None else {}
    balancers = balancer_plans if balancer_plans is not None else getattr(nl, "balancer_plans", {})
    out: list[str] = [
        f"// {nl.label}: generated by foundry/netlist.py. Do not edit.",
        "",
    ]
    input_wires = [sanitize(m) for m in nl.inputs]
    ports = [f"  input  wire {w}" for w in input_wires]
    ports.extend(f"  output wire {sanitize(m)}" for m in nl.outputs)
    out.extend([f"module {nl.label} (", ",\n".join(ports), ");"])

    declared = _declare_verilog_wires(nl, input_wires, merge_plans, splits, balancers, out)
    out.append("")
    wire_insert_idx = len(ports) + 2
    for inst in nl.instances:
        if _try_emit_special_instance_verilog(inst, merge_plans, splits, balancers, declared, out):
            continue
        if nl.cells[inst.cell_key].get("circuit_cell"):
            _emit_circuit_cell_verilog(nl, inst, declared, wire_insert_idx, out)
        else:
            _emit_recipe_cell_verilog(nl, inst, merge_plans, splits, balancers, out)
    out.append("endmodule")
    return "\n".join(out) + "\n"


def _resolve_tapped_materials(
    nl: Netlist,
    scopes: set[str],
    merge_plans: dict[str, list[tuple[str, str, str, str]]] | None,
) -> set[str]:
    """Resolve solid material names matching the requested telemetry tap scopes."""
    if "all" in scopes:
        candidates = set(nl.nets.keys())
    else:
        candidates = set()
        if "io" in scopes:
            candidates.update(nl.inputs)
            candidates.update(nl.outputs)
        if "merges" in scopes and merge_plans:
            candidates.update(merge_plans.keys())
        candidates.update(
            m for s in scopes for m in nl.nets if s in (m.lower(), sanitize(m).lower())
        )
    return {
        m for m in candidates if m in nl.nets and not nl.nets[m].is_fluid and m not in nl.fluids
    }


def _insert_material_telemetry(
    nl: Netlist,
    mat: str,
    rank: int,
    enable_display: bool,
    enable_circuit: bool,
) -> dict[str, Any]:
    """Instantiate telemetry combinator/display cells for a single material net."""
    bare = sanitize(mat)
    circ_name: str | None = None
    disp_name: str | None = None
    if enable_circuit:
        circ_name = f"u_acc_{bare}"
        nl.instances.append(
            Instance(circ_name, pdk.ARITHMETIC_COMBINATOR_CELL, mat, 0.0, 0.0, rank=rank)
        )
        nl.net(f"c_in_{bare}").sinks.append(circ_name)
        nl.net(f"c_out_{bare}").drivers.append(circ_name)
    if enable_display:
        disp_name = f"u_disp_{bare}"
        nl.instances.append(Instance(disp_name, pdk.DISPLAY_PANEL_CELL, mat, 0.0, 0.0, rank=rank))
        target_net = f"c_out_{bare}" if enable_circuit else f"c_disp_{bare}"
        nl.net(target_net).sinks.append(disp_name)
    return {
        "material": mat,
        "display_instance": disp_name,
        "circuit_instance": circ_name,
    }


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
    tapped_materials = _resolve_tapped_materials(nl, scopes, merge_plans)
    if not tapped_materials:
        return []

    if enable_display:
        disp_cell = pdk.display_panel_cell()
        nl.cells[disp_cell["cell"]] = disp_cell
    if enable_circuit:
        acc_cell = pdk.arithmetic_combinator_cell()
        nl.cells[acc_cell["cell"]] = acc_cell

    telemetry_rank = max((inst.rank for inst in nl.instances), default=0) + 1
    return [
        _insert_material_telemetry(nl, mat, telemetry_rank, enable_display, enable_circuit)
        for mat in sorted(tapped_materials)
        if mat in nl.nets
    ]


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
            f"{cell['watts'] / WATTS_PER_KW:.0f} kW, "
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
        return FLOW_PRIORITY_SPLIT
    if "merge" in inst.cell_key or "merge" in inst.name:
        return FLOW_PRIORITY_MERGE
    if "display" in inst.cell_key or "combinator" in inst.cell_key or "disp" in inst.name:
        return FLOW_PRIORITY_TELEMETRY
    return FLOW_PRIORITY_MACHINE


def _natural_key(s: str) -> list[int | str]:
    """Sort strings with embedded numbers naturally (e.g. 7 before 10)."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


class _RankDependencyGraph:
    """Wire-level and instance-level dependency graph for TCL rank ordering."""

    def __init__(
        self,
        nl: Netlist,
        merge_plans: dict[str, list[tuple[str, str, str, str]]] | None,
        split_plans: dict[str, list[tuple[str, str, str, str]]] | None,
        balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None,
    ) -> None:
        self.nl = nl
        self.inst_by_name = {inst.name: inst for inst in nl.instances}
        self.wire_driver: dict[str, str] = {}
        self.wire_sinks: dict[str, list[str]] = collections.defaultdict(list)
        self.inst_input_wires: dict[str, list[str]] = collections.defaultdict(list)
        self._record_plan_wires(merge_plans, split_plans, balancer_plans)
        self._record_instance_port_wires(merge_plans, split_plans, balancer_plans)
        self.deps: dict[str, set[str]] = collections.defaultdict(set)
        self.forward_deps: dict[str, set[str]] = collections.defaultdict(set)
        self.reverse_deps: dict[str, set[str]] = collections.defaultdict(set)
        for wire, driver in self.wire_driver.items():
            for sink in self.wire_sinks.get(wire, []):
                if driver != sink:
                    self.deps[sink].add(driver)
                    self.forward_deps[driver].add(sink)
                    self.reverse_deps[sink].add(driver)

    def _record_plan_wires(
        self,
        merge_plans: dict[str, list[tuple[str, str, str, str]]] | None,
        split_plans: dict[str, list[tuple[str, str, str, str]]] | None,
        balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None,
    ) -> None:
        for plan in (merge_plans or {}).values():
            for m_name, a_w, b_w, y_w in plan:
                self.wire_sinks[a_w].append(m_name)
                self.wire_sinks[b_w].append(m_name)
                self.wire_driver[y_w] = m_name
                self.inst_input_wires[m_name].extend([a_w, b_w])
        for plan in (split_plans or {}).values():
            for s_name, in_w, y1_w, y2_w in plan:
                self.wire_sinks[in_w].append(s_name)
                self.wire_driver[y1_w] = s_name
                self.wire_driver[y2_w] = s_name
                self.inst_input_wires[s_name].append(in_w)
        for b_plan in (balancer_plans or {}).values():
            for bal_name, a1_w, a2_w, y1_w, y2_w in b_plan:
                self.wire_sinks[a1_w].append(bal_name)
                self.wire_sinks[a2_w].append(bal_name)
                self.wire_driver[y1_w] = bal_name
                self.wire_driver[y2_w] = bal_name
                self.inst_input_wires[bal_name].extend([a1_w, a2_w])

    def _record_instance_port_wires(
        self,
        merge_plans: dict[str, list[tuple[str, str, str, str]]] | None,
        split_plans: dict[str, list[tuple[str, str, str, str]]] | None,
        balancer_plans: dict[str, list[tuple[str, str, str, str, str]]] | None,
    ) -> None:
        for inst in self.nl.instances:
            cell = self.nl.cells.get(inst.cell_key)
            if not cell:
                continue
            for p in cell.get("inputs", []):
                wname = _resolve_port_wire(
                    self.nl, inst.name, p, True, merge_plans, split_plans, balancer_plans
                )
                self.wire_sinks[wname].append(inst.name)
                self.inst_input_wires[inst.name].append(wname)
            for p in cell.get("outputs", []):
                wname = _resolve_port_wire(
                    self.nl, inst.name, p, False, merge_plans, split_plans, balancer_plans
                )
                self.wire_driver[wname] = inst.name

    def find_driver_x(self, start: str, inst_abs_center: dict[str, float]) -> float:
        """Breadth-first search upstream to find the first placed driver X coordinate."""
        visited: set[str] = set()
        q = [start]
        while q:
            c = q.pop(0)
            if c in inst_abs_center:
                return inst_abs_center[c]
            for prv in self.reverse_deps.get(c, set()):
                if prv not in visited:
                    visited.add(prv)
                    q.append(prv)
        return 0.0

    def machine_driver_x(self, name: str, inst_abs_center: dict[str, float]) -> float:
        """Compute average upstream driver X coordinate for a machine instance."""
        driver_xs = [
            self.find_driver_x(d, inst_abs_center)
            for w in self.inst_input_wires.get(name, [])
            for d in self.wire_driver.get(w, "").split()
        ]
        valid_xs = [x for x in driver_xs if x > 0]
        return sum(valid_xs) / len(valid_xs) if valid_xs else 0.0

    def connected_machines_in_rank(self, start: str, rank_set: set[str], forward: bool) -> set[str]:
        """Find machine instances in `rank_set` reachable from `start` within the rank."""
        adj = self.forward_deps if forward else self.reverse_deps
        visited: set[str] = set()
        q = [start]
        machs: set[str] = set()
        while q:
            c = q.pop(0)
            for neighbor in adj.get(c, set()):
                if neighbor in rank_set and neighbor not in visited:
                    visited.add(neighbor)
                    if (
                        _instance_flow_priority(self.inst_by_name[neighbor])
                        == FLOW_PRIORITY_MACHINE
                    ):
                        machs.add(neighbor)
                    else:
                        q.append(neighbor)
        return machs

    def inst_mat(self, name: str) -> str:
        """Return the base material name associated with an instance."""
        wires = self.inst_input_wires.get(name, [])
        if wires:
            return wires[0].split("__")[0]
        inst = self.inst_by_name.get(name)
        return inst.recipe if inst else ""


def _compact_and_emit_instance_ranks(nl: Netlist, lines: list[str]) -> dict[int, list[str]]:
    """Remap occupied ranks to contiguous indices and append TCL dict assignments."""
    active_ranks = sorted({inst.rank for inst in nl.instances})
    rank_remap = {old_r: new_r for new_r, old_r in enumerate(active_ranks)}
    by_rank: dict[int, list[str]] = {}
    for inst in nl.instances:
        new_r = rank_remap[inst.rank]
        inst.rank = new_r
        lines.append(f"dict set instance_ranks {inst.name} {new_r}")
        by_rank.setdefault(new_r, []).append(inst.name)
    return by_rank


def _update_min_positive(mapping: dict[str, float], key: str, val: float) -> None:
    """Update `mapping[key]` with `val` if `val` is positive and smaller than existing."""
    if val > 0 and (key not in mapping or val < mapping[key]):
        mapping[key] = val


def _record_machine_input_driver_coords(
    graph: _RankDependencyGraph,
    name: str,
    inst_abs_center: dict[str, float],
    mat_driver_x: dict[str, float],
) -> None:
    """Record minimum positive upstream driver X coordinates for all inputs of a machine."""
    for w in graph.inst_input_wires.get(name, []):
        in_mat = w.split("__")[0]
        for d in graph.wire_driver.get(w, "").split():
            _update_min_positive(mat_driver_x, in_mat, graph.find_driver_x(d, inst_abs_center))


def _compute_rank_group_coords(
    graph: _RankDependencyGraph,
    rank_insts: set[str],
    machines_in_rank: list[str],
    inst_abs_center: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute material driver X coordinates and machine group X coordinates for one rank."""
    mat_driver_x: dict[str, float] = {}
    for name in rank_insts:
        _update_min_positive(
            mat_driver_x, graph.inst_mat(name), graph.find_driver_x(name, inst_abs_center)
        )
        if _instance_flow_priority(graph.inst_by_name[name]) == FLOW_PRIORITY_MACHINE:
            _record_machine_input_driver_coords(graph, name, inst_abs_center, mat_driver_x)

    machine_group_x: dict[str, float] = {}
    for name in machines_in_rank:
        in_mats = [w.split("__")[0] for w in graph.inst_input_wires.get(name, [])]
        in_drv_xs = [mat_driver_x[m] for m in in_mats if m in mat_driver_x]
        gx = max(in_drv_xs) if in_drv_xs else graph.machine_driver_x(name, inst_abs_center)
        machine_group_x[name] = gx
        inst = graph.inst_by_name.get(name)
        if inst and inst.recipe:
            mat_driver_x[inst.recipe] = gx
            mat_driver_x[sanitize(inst.recipe)] = gx
    return mat_driver_x, machine_group_x


def _instance_group_x(
    graph: _RankDependencyGraph,
    name: str,
    prio: int,
    mat: str,
    rank_insts: set[str],
    mat_driver_x: dict[str, float],
    machine_group_x: dict[str, float],
) -> float:
    """Resolve the horizontal group coordinate for an instance within its rank."""
    if prio == FLOW_PRIORITY_MACHINE:
        return machine_group_x.get(name, 0.0)
    if prio == FLOW_PRIORITY_MERGE:
        up = graph.connected_machines_in_rank(name, rank_insts, forward=False)
        up_gxs = [machine_group_x[m] for m in up if m in machine_group_x]
        return max(up_gxs) if up_gxs else mat_driver_x.get(mat, 0.0)
    if prio == FLOW_PRIORITY_SPLIT:
        down = graph.connected_machines_in_rank(name, rank_insts, forward=True)
        down_gxs = [machine_group_x[m] for m in down if m in machine_group_x]
        if down_gxs:
            return min(down_gxs)
        up = graph.connected_machines_in_rank(name, rank_insts, forward=False)
        up_gxs = [machine_group_x[m] for m in up if m in machine_group_x]
        return max(up_gxs) if up_gxs else mat_driver_x.get(mat, 0.0)
    return mat_driver_x.get(mat, 0.0)


def _instance_rank_sort_key(
    graph: _RankDependencyGraph,
    name: str,
    rank_insts: set[str],
    machines_in_rank: list[str],
    machine_idx: dict[str, int],
    mat_driver_x: dict[str, float],
    machine_group_x: dict[str, float],
    inst_abs_center: dict[str, float],
) -> tuple[object, ...]:
    """Build the lexicographic sort key for ordering an instance within its rank."""
    prio = _instance_flow_priority(graph.inst_by_name[name])
    mat = graph.inst_mat(name)
    gx = round(
        _instance_group_x(graph, name, prio, mat, rank_insts, mat_driver_x, machine_group_x),
        RANK_COORD_DECIMALS,
    )
    if prio == FLOW_PRIORITY_SPLIT:
        down = graph.connected_machines_in_rank(name, rank_insts, forward=True)
        down_indices = [machine_idx[m] for m in down if m in machine_idx]
        min_down = min(down_indices) if down_indices else -1
        span = (max(down_indices) - min_down) if down_indices else 0
        drv_x = round(graph.find_driver_x(name, inst_abs_center), RANK_COORD_DECIMALS)
        return (gx, FLOW_PRIORITY_SPLIT, mat, min_down, -span, drv_x, _natural_key(name))
    if prio == FLOW_PRIORITY_MACHINE:
        return (gx, FLOW_PRIORITY_MACHINE, machine_idx[name], 0, 0.0, _natural_key(name))
    if prio == FLOW_PRIORITY_MERGE:
        up = graph.connected_machines_in_rank(name, rank_insts, forward=False)
        up_indices = [machine_idx[m] for m in up if m in machine_idx]
        min_up = min(up_indices) if up_indices else len(machines_in_rank)
        span = (max(up_indices) - min_up) if up_indices else 0
        return (gx, FLOW_PRIORITY_MERGE, mat, min_up, span, 0.0, _natural_key(name))
    return (
        gx,
        FLOW_PRIORITY_TELEMETRY,
        mat,
        len(machines_in_rank) + 1,
        0,
        0.0,
        graph.inst_by_name[name].recipe or "",
        _natural_key(name),
    )


def _toposort_rank_instances(
    rank_insts: set[str],
    deps: dict[str, set[str]],
    rank_sort_keys: dict[str, tuple[object, ...]],
) -> list[str]:
    """Topologically sort instances within a rank using `rank_sort_keys` as tie-breaker."""
    rank_deps = {name: {d for d in deps.get(name, set()) if d in rank_insts} for name in rank_insts}
    ready = [name for name, d_set in rank_deps.items() if not d_set]
    sorted_insts: list[str] = []
    while ready:
        ready.sort(key=lambda name: rank_sort_keys[name])
        curr = ready.pop(0)
        sorted_insts.append(curr)
        for other, d_set in rank_deps.items():
            if curr in d_set:
                d_set.remove(curr)
                if not d_set and other not in sorted_insts and other not in ready:
                    ready.append(other)
    sorted_insts.extend(name for name in rank_insts if name not in sorted_insts)
    return sorted_insts


def _instance_x_padding(iname: str) -> float:
    """Return horizontal spacing pad after an instance in a rank."""
    if "merge" in iname:
        return RANK_PAD_MERGE
    if "split" in iname or "bal" in iname:
        return RANK_PAD_SPLIT
    return RANK_PAD_DEFAULT


def _compute_rank_x_offset(
    graph: _RankDependencyGraph,
    sorted_insts: list[str],
    inst_abs_center: dict[str, float],
) -> int:
    """Compute horizontal X offset for a rank and update `inst_abs_center` in place."""
    nl = graph.nl
    rel_centers: dict[str, float] = {}
    curr_x = 0.0
    for iname in sorted_insts:
        inst = graph.inst_by_name[iname]
        cell = nl.cells.get(inst.cell_key, {})
        w = float(cell.get("width", DEFAULT_MACHINE_DIM))
        rel_centers[iname] = curr_x + w / 2.0
        curr_x += w + _instance_x_padding(iname)

    has_raw_consumer = any(
        any(
            inp in nl.inputs
            for inp in nl.cells.get(graph.inst_by_name[iname].cell_key, {}).get("inputs", [])
        )
        for iname in sorted_insts
    )
    connected_pairs = [
        (inst_abs_center[d], rel_centers[iname])
        for iname in sorted_insts
        for w in graph.inst_input_wires[iname]
        for d in graph.wire_driver.get(w, "").split()
        if d in inst_abs_center
    ]
    if has_raw_consumer or not connected_pairs:
        offset = 0
    else:
        avg_feeder_x = sum(p[0] for p in connected_pairs) / len(connected_pairs)
        avg_rel_x = sum(p[1] for p in connected_pairs) / len(connected_pairs)
        offset = max(0, round(avg_feeder_x - avg_rel_x) + RANK_FEEDER_OFFSET)

    for iname in sorted_insts:
        inst_abs_center[iname] = offset + rel_centers[iname]
    return offset


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

    by_rank = _compact_and_emit_instance_ranks(nl, lines)
    graph = _RankDependencyGraph(nl, merge_plans, split_plans, balancer_plans)
    num_ranks = (max(by_rank.keys()) + 1) if by_rank else 0
    lines.append(f"set num_ranks {num_ranks}")

    inst_abs_center: dict[str, float] = {}
    for r in range(num_ranks):
        rank_insts = set(by_rank.get(r, []))
        machines_in_rank = sorted(
            (
                name
                for name in rank_insts
                if _instance_flow_priority(graph.inst_by_name[name]) == FLOW_PRIORITY_MACHINE
            ),
            key=lambda name: (
                round(graph.machine_driver_x(name, inst_abs_center), RANK_COORD_DECIMALS),
                _natural_key(name),
            ),
        )
        machine_idx = {name: i for i, name in enumerate(machines_in_rank)}
        mat_driver_x, machine_group_x = _compute_rank_group_coords(
            graph, rank_insts, machines_in_rank, inst_abs_center
        )
        rank_sort_keys = {
            name: _instance_rank_sort_key(
                graph,
                name,
                rank_insts,
                machines_in_rank,
                machine_idx,
                mat_driver_x,
                machine_group_x,
                inst_abs_center,
            )
            for name in rank_insts
        }
        sorted_insts = _toposort_rank_instances(rank_insts, graph.deps, rank_sort_keys)
        lines.append(f"set rank_instances({r}) [list {' '.join(sorted_insts)}]")
        offset = _compute_rank_x_offset(graph, sorted_insts, inst_abs_center)
        lines.append(f"set rank_offset_x({r}) {offset}")

    return "\n".join(lines) + "\n"
