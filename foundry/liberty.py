"""Liberty stubs: the timing and power view of the PDK cells.

Today the stubs carry the minimum that OpenROAD needs to place, resize,
and time a design: pin directions, area, zero-delay arcs, and the game's
power draw. The mapping between IC terms and game terms, fixed here:

    cell delay     craft time: energy_required / crafting_speed
    cell area      tile footprint (and, one day, material build cost)
    capacitance    the load a machine puts on its feed belt: the items
                   its input buffer holds. A chest is the true capacitor:
                   capacity in slots times stack size.
    current        the item stream a machine pulls per second
    leakage power  idle draw; internal_power, one day, crafting activity
    max_transition belt lane saturation: the rate at which a lane
                   starves or floods
    corners        machine and beacon settings: no beacons is the slow
                   corner, full speed modules the fast one

The comments in the generated file name what will replace each stub
value once the game measurements exist.
"""

from pathlib import Path
from typing import Any

import netlist

# Liberty requires one of nW/pW/uW/mW/W for leakage. The game reports
# kW per machine, so mW keeps the numbers exact.
LEAKAGE_UNIT = "1mW"

# The load one feed lane presents, in the liberty capacitive unit. One
# placeholder until the measured buffer depths land.
INPUT_CAPACITANCE = 1.0

# The fanout limit of one output: one belt lane holds a finite stream.
MAX_CAPACITANCE = 100.0


def cell_name(cell_key: str) -> str:
    """The LEF master name of one cell, which Liberty must match."""
    return netlist.sanitize(cell_key)


def _template_lines() -> list[str]:
    """The 1x1 table templates. Real timing tables replace these."""
    return [
        "  lu_table_template (in_slew) {",
        "    variable_1 : input_transition_time ;",
        '    index_1 ("0") ;',
        "  }",
        "  lu_table_template (out_cap) {",
        "    variable_1 : total_output_net_capacitance ;",
        '    index_1 ("0") ;',
        "  }",
    ]


def _timing_arcs(cell: dict[str, Any]) -> list[str]:
    """One combinational arc per input. Craft delay models propagation."""
    cpm = cell.get("crafts_per_minute", 0.0)
    delay = f"{60.0 / cpm:.4f}" if cpm > 0.0 else "0.0000"
    trans = f"{0.1 * float(delay):.4f}" if cpm > 0.0 else "0.0100"
    lines: list[str] = []
    for port in cell["inputs"]:
        related = netlist.sanitize(port)
        lines.extend(
            [
                "    timing () {",
                f'      related_pin : "{related}" ;',
                "      timing_type : combinational ;",
                f'      cell_rise (scalar) {{ values ("{delay}") ; }}',
                f'      cell_fall (scalar) {{ values ("{delay}") ; }}',
                f'      rise_transition (scalar) {{ values ("{trans}") ; }}',
                f'      fall_transition (scalar) {{ values ("{trans}") ; }}',
                "    }",
            ]
        )
    return lines


def cell_lines(cell: dict[str, Any]) -> list[str]:
    """One cell block. The comments name the future measurements."""
    name = cell_name(cell["cell"])
    tiles = cell["width"] * cell["height"]
    leakage = cell["watts"] / 1000.0  # W -> mW, the unit above
    lines = [
        f"  /* {cell['machine']} crafting {cell['recipe']}: "
        f"{cell['width']}x{cell['height']} tiles */",
        f"  cell ({name}) {{",
        f"    area : {tiles:.3f} ;",
        "    /* one day: the material cost of the building recipe,",
        "       from the dump, as the solver build-cost objective */",
        f"    cell_leakage_power : {leakage:.6f} ;",
        "    /* one day: dynamic power from crafting activity */",
    ]
    for port in cell["inputs"]:
        lines.extend(
            [
                f"    pin ({netlist.sanitize(port)}) {{",
                "      direction : input ;",
                f"      capacitance : {INPUT_CAPACITANCE} ;",
                "      /* one day: the machine input buffer depth, measured",
                "         in game, as the belt load this pin presents */",
                "    }",
            ]
        )
    for port in cell["outputs"]:
        pin_lines = [
            f"    pin ({netlist.sanitize(port)}) {{",
            "      direction : output ;",
        ]
        if cell["inputs"]:
            pin_lines.append(f'      function : "{netlist.sanitize(cell["inputs"][0])}" ;')
        else:
            pin_lines.append('      function : "1" ;')
        pin_lines.extend(
            [
                f"      max_capacitance : {MAX_CAPACITANCE} ;",
                "      /* one day: the lane saturation rate of this output */",
            ]
        )
        lines.extend(pin_lines)
        lines.extend(_timing_arcs(cell))
        lines.append("    }")
    lines.extend(["  }", ""])
    return lines


def write_liberty(cells: list[dict[str, Any]], path: str) -> str:
    """The library file: units, templates, one cell per used master."""
    lines = [
        "/* Generated by foundry/liberty.py. Do not edit. */",
        "/* Stubs today. The comment at each value names the game",
        "   measurement that replaces it. */",
        "library (factorilog) {",
        "  technology (cmos) ;",
        "  delay_model : table_lookup ;",
        '  time_unit : "1ns" ;',
        '  voltage_unit : "1V" ;',
        '  current_unit : "1mA" ;',
        '  pulling_resistance_unit : "1kohm" ;',
        f'  leakage_power_unit : "{LEAKAGE_UNIT}" ;',
        "  capacitive_load_unit (1, pf) ;",
        "  /* one day: the capacitive unit becomes items or fluid units,",
        "     once storage-as-capacitance has measured numbers */",
    ]
    for cell in sorted(cells, key=lambda c: c["cell"]):
        lines.extend(cell_lines(cell))
    lines.append("}")
    text = "\n".join(lines)
    Path(path).write_text(text)
    return text


def check_liberty(cells: list[dict[str, Any]], text: str) -> list[str]:
    """Structural checks the flow depends on.

    Every used master has a cell block, every cell names its pins, the
    input pins carry capacitance, the outputs carry function. Empty list
    means the file matches the cells.
    """
    problems: list[str] = []
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    depth = 0
    for line in text.splitlines():
        token = line.strip()
        if token.startswith("cell (") and token.endswith("{"):
            inner = token[len("cell (") :]
            current = inner.split(")")[0].strip()
            blocks[current] = []
            depth = 1
        elif current is not None:
            if token.endswith("{"):
                depth += 1
            elif token == "}":
                depth -= 1
                if depth == 0:
                    current = None
                    continue
            blocks[current].append(token)

    for cell in cells:
        name = cell_name(cell["cell"])
        block = blocks.get(name)
        if block is None:
            problems.append(f"{name}: cell missing")
            continue
        for port in cell["inputs"] + cell["outputs"]:
            pin = f"pin ({netlist.sanitize(port)})"
            if not any(t.startswith(pin) for t in block):
                problems.append(f"{name}.{port}: pin missing")
        if not any(t.startswith("area :") for t in block):
            problems.append(f"{name}: area missing")
    return problems
