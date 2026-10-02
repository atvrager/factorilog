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

import math
from typing import Any

import pdk
import solve

# The transport cell that joins two streams of one material. A splitter
# run backwards. The PDK carries it next to the recipe cells.
MERGE_CELL = "belt_merge2"
SPLIT_CELL = "belt_split2"
PUMP_CELL = "pump"


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

    The splitter footprint is 1.8 x 0.8 tiles in the dump; the record
    snaps it to the tile grid, 2 x 1. Two inputs, one output, and no
    crafting rate.
    """
    return {
        "cell": MERGE_CELL,
        "machine": "splitter",
        "recipe": "merge2",
        "width": 1,
        "height": 2,
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
        self, name: str, cell_key: str, recipe: str, plan_rate: float, rated: float
    ) -> None:
        self.name = name
        self.cell_key = cell_key
        self.recipe = recipe
        self.plan_rate = plan_rate
        self.rated = rated


class Net:
    """One material, its drivers, and its sinks."""

    def __init__(self, material: str) -> None:
        self.material = material
        self.drivers: list[str] = []  # instance or merge output names
        self.sinks: list[str] = []
        self.rate_per_min = 0.0


class Netlist:
    """Instances, nets, and the cell table of one plan."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.cells: dict[str, dict[str, Any]] = {}
        self.instances: list[Instance] = []
        self.nets: dict[str, Net] = {}
        self.inputs: list[str] = []
        self.outputs: list[str] = []

    def net(self, material: str) -> Net:
        if material not in self.nets:
            self.nets[material] = Net(material)
        return self.nets[material]


def build(data: solve.Data, demand: dict[str, float], allowed: set[str]) -> Netlist:
    """Solve the plan, then lower it to instances and nets."""
    tech = pdk.Pdk(data)
    rates, raw, _surplus = solve.solve_rates(data, demand, allowed)
    lines, _power = solve.machine_counts(data, rates)
    plan_count = {line[0]: line[3] for line in lines}

    nl = Netlist("factory")
    for recipe, rate in sorted(rates.items()):
        recipe_proto = data.recipes[recipe]
        machine, _speed = data.machine_for(recipe_proto)
        cell = tech.cell(machine, recipe)
        if cell is None:
            raise RuntimeError(f"no cell for {machine} running {recipe}")
        nl.cells[cell["cell"]] = cell
        count = max(1, math.ceil(plan_count.get(recipe, 1.0)))
        # Each instance of the fleet takes an equal share of the recipe.
        share = rate / count
        for index in range(count):
            inst = Instance(
                f"u_{sanitize(recipe)}_{index + 1}",
                cell["cell"],
                recipe,
                share,
                cell["crafts_per_minute"],
            )
            nl.instances.append(inst)
            for entry in solve.Data.ingredients_of(recipe_proto):
                nl.net(solve.name_of(entry)).sinks.append(inst.name)
            for entry in solve.Data.results_of(recipe_proto):
                material = solve.name_of(entry)
                nl.net(material).rate_per_min += rate * solve.amount_of(entry)
                nl.net(material).drivers.append(inst.name)

    for material, rate in raw.items():
        nl.net(material).rate_per_min = rate
    nl.inputs = sorted(nl.net(m).material for m in raw)
    nl.outputs = sorted(
        m for m in demand if m in nl.nets and nl.nets[m].drivers and not nl.net(m).sinks
    )
    # Some demanded items feed other recipes. They must still leave the
    # block, so an output is any demand item with a driver.
    nl.outputs = sorted(m for m in demand if m in nl.nets and nl.nets[m].drivers)
    return nl


def merge_net(nl: Netlist, net: Net) -> list[tuple[str, str, str, str]]:
    """Replace the drivers of one net with a merge tree.

    Two or more producers on one material become `belt_merge2` instances.
    Each driver output gets a fresh wire. Each merge output gets a fresh
    wire, except the last, which drives the material wire itself. Returns
    entries of (merge name, a wire, b wire, y wire), empty for one driver.
    """
    if len(net.drivers) <= 1:
        return []
    material = sanitize(net.material)
    entries: list[tuple[str, str, str, str]] = []
    current = [f"{material}__{d}" for d in net.drivers]
    count = 0
    while len(current) > 1:
        count += 1
        name = f"u_merge_{material}_{count}"
        a_wire, b_wire = current[0], current[1]
        # The last merge of the tree drives the material wire itself.
        y_wire = material if len(current) == 2 else f"{material}__m{count}"
        entries.append((name, a_wire, b_wire, y_wire))
        nl.instances.append(Instance(name, MERGE_CELL, net.material, 0.0, 0.0))
        current = [*current[2:], y_wire]
    net.drivers = [current[0]] if current else []
    return entries


def split_net(nl: Netlist, net: Net) -> list[tuple[str, str, str, str]]:
    """Replace the sinks of one net with a split tree.

    Two or more consumers on one material become `belt_split2` instances.
    Each split output gets a fresh wire driving downstream sinks or subsequent splitters.
    Returns entries of (split name, a wire, y1 wire, y2 wire), empty for <=1 sink.
    """
    if len(net.sinks) <= 1:
        return []
    material = sanitize(net.material)
    entries: list[tuple[str, str, str, str]] = []
    targets = [f"{material}__{s}" for s in net.sinks]
    count = [0]

    def _build_tree(in_wire: str, sinks: list[str]) -> None:
        if len(sinks) <= 1:
            return
        count[0] += 1
        name = f"u_split_{material}_{count[0]}"
        if len(sinks) == 2:
            y1_wire, y2_wire = sinks[0], sinks[1]
            entries.append((name, in_wire, y1_wire, y2_wire))
            nl.instances.append(Instance(name, SPLIT_CELL, net.material, 0.0, 0.0))
            return

        mid = len(sinks) // 2
        left = sinks[:mid]
        right = sinks[mid:]
        y1_wire = left[0] if len(left) == 1 else f"{material}__sp_{count[0]}_1"
        y2_wire = right[0] if len(right) == 1 else f"{material}__sp_{count[0]}_2"
        entries.append((name, in_wire, y1_wire, y2_wire))
        nl.instances.append(Instance(name, SPLIT_CELL, net.material, 0.0, 0.0))

        if len(left) > 1:
            _build_tree(y1_wire, left)
        if len(right) > 1:
            _build_tree(y2_wire, right)

    _build_tree(material, targets)
    net.sinks = [entries[0][0]] if entries else net.sinks
    return entries


def emit_verilog(
    nl: Netlist,
    merge_plans: dict[str, list[tuple[str, str, str, str]]],
    split_plans: dict[str, list[tuple[str, str, str, str]]] | None = None,
) -> str:
    """The netlist as flat structural Verilog, against LEF masters.

    No cell modules live here. In a standard-cell flow the design
    netlist instantiates LEF masters directly; the cell modules are
    library code (emit_cells_v), not design code. OpenROAD takes the
    masters from tech.lef at link time.
    """
    if split_plans is None:
        split_plans = {}
    out: list[str] = []
    out.append(f"// {nl.label}: generated by foundry/netlist.py. Do not edit.")
    out.append("")
    ports = [f"  input  wire {sanitize(m)}" for m in nl.inputs]
    ports.extend(f"  output wire {sanitize(m)}" for m in nl.outputs)
    out.append(f"module {nl.label} (")
    out.append(",\n".join(ports))
    out.append(");")
    # Wires: one per internal material plus one per pre-merge driver.
    internal = [m for m in sorted(nl.nets) if m not in nl.inputs and m not in nl.outputs]
    out.extend(f"  wire {sanitize(m)};  // {nl.nets[m].rate_per_min:.2f} per min" for m in internal)
    declared: set[str] = set()
    for material, plan in sorted(merge_plans.items()):
        bare = sanitize(material)
        for _name, a_wire, b_wire, y_wire in plan:
            for wire in (a_wire, b_wire, y_wire):
                if wire == bare or wire in declared:
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
        if inst.cell_key == pdk.ARITHMETIC_COMBINATOR_CELL:
            mat_wire = sanitize(inst.recipe)
            c_out_wire = f"c_out_{mat_wire}"
            c_in_wire = f"c_in_{mat_wire}"
            out.append(f"  // telemetry accumulator counter for {inst.recipe}")
            out.append(
                f"  {pdk.ARITHMETIC_COMBINATOR_CELL} {inst.name} (.a({c_in_wire}), .y({c_out_wire}));"
            )
            continue
        cell = nl.cells[inst.cell_key]
        module = sanitize(inst.cell_key)
        conns = []
        for p in cell["inputs"]:
            if p in split_plans:
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
    for mat in sorted(tapped_materials):
        if mat not in nl.nets:
            continue
        bare = sanitize(mat)
        disp_name = None
        circ_name = None

        if enable_circuit:
            circ_name = f"u_acc_{bare}"
            c_in_name = f"c_in_{bare}"
            c_out_name = f"c_out_{bare}"
            nl.instances.append(Instance(circ_name, pdk.ARITHMETIC_COMBINATOR_CELL, mat, 0.0, 0.0))
            nl.net(c_in_name).sinks.append(circ_name)
            nl.net(c_out_name).drivers.append(circ_name)

        if enable_display:
            disp_name = f"u_disp_{bare}"
            nl.instances.append(Instance(disp_name, pdk.DISPLAY_PANEL_CELL, mat, 0.0, 0.0))
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
    return "\n".join(out) + "\n"


def merge_plans_of(nl: Netlist) -> dict[str, list[tuple[str, str, str, str]]]:
    """Merge insertion for every multi-driver net, before emission."""
    plans: dict[str, list[tuple[str, str, str, str]]] = {}
    for net in nl.nets.values():
        entries = merge_net(nl, net)
        if entries:
            plans[net.material] = entries
    return plans


def split_plans_of(nl: Netlist) -> dict[str, list[tuple[str, str, str, str]]]:
    """Split insertion for every multi-sink net, before emission."""
    plans: dict[str, list[tuple[str, str, str, str]]] = {}
    for net in list(nl.nets.values()):
        entries = split_net(nl, net)
        if entries:
            plans[net.material] = entries
    return plans
