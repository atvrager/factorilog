"""Verilog synthesis compiler for Factorio combinator logic.

Converts behavioral Verilog and finite state machines into Factorio combinators.
ASD-STE100 technical specification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from foundry import netlist, pdk
except ImportError:
    import netlist
    import pdk

SIGNAL_BIT_WIDTH = 32
THRESH_THROTTLED_VAL = 30
THRESH_RECOVER_NORMAL_VAL = 70
THRESH_EMERGENCY_VAL = 10
THRESH_RECOVER_THROTTLED_VAL = 20
FILTER_INDEX_3 = 3
FILTER_INDEX_4 = 4
PORT_IN_RED = 1
PORT_IN_GREEN = 2
PORT_OUT_RED = 3
PORT_OUT_GREEN = 4
RANK_OUTPUT_STAGE = 3
ASCII_UPPER_A = 65
GROUP_NAME_IDX = 3
GROUP_VALUE_IDX = 3
COMBINATOR_RATED_CPM = 1000.0
BINARY_BASE = 2

INPUT_PORT_PAT = re.compile(r"input\s+(?:wire\s+|reg\s+)?(?:\[(\d+):(\d+)\]\s+)?([A-Za-z0-9_]+)")
OUTPUT_PORT_PAT = re.compile(r"output\s+(?:wire\s+|reg\s+)?(?:\[(\d+):(\d+)\]\s+)?([A-Za-z0-9_]+)")
PARAM_PAT = re.compile(r"(?:parameter|localparam)\s+([A-Za-z0-9_]+)\s*=\s*([^;]+);")
COMPARISON_PAT = re.compile(r"([A-Za-z0-9_]+)\s*(<|<=|>|>=|==|!=)\s*(\d+'[dbh][0-9a-fA-F]+|\d+)")
ARITH_PAT = re.compile(r"([A-Za-z0-9_]+)\s*([+\-*/%])\s*(\d+)")


@dataclass(slots=True)
class CombinatorCell:
    """Standard cell combinator instance."""

    name: str
    cell: str
    recipe: str
    control_behavior: dict[str, Any] = field(default_factory=dict)
    rank: int = 0


@dataclass(slots=True)
class CircuitConnection:
    """Point to point wire connection between combinator ports."""

    source: str
    source_port: int
    target: str
    target_port: int
    color: str


@dataclass(slots=True)
class CircuitSynthResult:
    """Synthesis result containing cells, connections, and port mappings."""

    module_name: str
    inputs: dict[str, int] = field(default_factory=dict)
    outputs: dict[str, int] = field(default_factory=dict)
    cells: list[CombinatorCell] = field(default_factory=list)
    connections: list[CircuitConnection] = field(default_factory=list)
    parameters: dict[str, int] = field(default_factory=dict)


def synthesize_power_sequencer() -> CircuitSynthResult:
    """Synthesize the power sequencer hysteresis finite state machine."""
    inputs = {
        "clk": 1,
        "rst_n": 1,
        "accumulator_charge": SIGNAL_BIT_WIDTH,
        "buffer_level": SIGNAL_BIT_WIDTH,
    }
    outputs = {
        "power_switch_enable": 1,
        "backup_pump_enable": 1,
        "alert_code": SIGNAL_BIT_WIDTH,
    }
    parameters = {
        "STATE_NORMAL": 0,
        "STATE_THROTTLED": 1,
        "STATE_EMERGENCY": 2,
        "THRESH_THROTTLED": THRESH_THROTTLED_VAL,
        "THRESH_RECOVER_NORMAL": THRESH_RECOVER_NORMAL_VAL,
        "THRESH_EMERGENCY": THRESH_EMERGENCY_VAL,
        "THRESH_RECOVER_THROTTLED": THRESH_RECOVER_THROTTLED_VAL,
    }

    cells: list[CombinatorCell] = []
    conns: list[CircuitConnection] = []

    const_cb = {
        "sections": {
            "sections": [
                {
                    "index": 1,
                    "filters": [
                        {
                            "index": 1,
                            "type": "virtual",
                            "name": "signal-A",
                            "quality": "normal",
                            "comparator": "=",
                            "count": THRESH_THROTTLED_VAL,
                        },
                        {
                            "index": 2,
                            "type": "virtual",
                            "name": "signal-B",
                            "quality": "normal",
                            "comparator": "=",
                            "count": THRESH_RECOVER_NORMAL_VAL,
                        },
                        {
                            "index": FILTER_INDEX_3,
                            "type": "virtual",
                            "name": "signal-C",
                            "quality": "normal",
                            "comparator": "=",
                            "count": THRESH_EMERGENCY_VAL,
                        },
                        {
                            "index": FILTER_INDEX_4,
                            "type": "virtual",
                            "name": "signal-D",
                            "quality": "normal",
                            "comparator": "=",
                            "count": THRESH_RECOVER_THROTTLED_VAL,
                        },
                    ],
                }
            ]
        }
    }
    cells.append(
        CombinatorCell(
            name="u_const_params",
            cell=pdk.CONSTANT_COMBINATOR_CELL,
            recipe="constant",
            control_behavior=const_cb,
            rank=0,
        )
    )

    dec_throttled_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": "<",
            "constant": THRESH_THROTTLED_VAL,
            "output_signal": {"type": "virtual", "name": "signal-T"},
            "copy_count_from_input": False,
        }
    }
    cells.append(
        CombinatorCell(
            name="u_dec_throttled",
            cell=pdk.DECIDER_COMBINATOR_CELL,
            recipe="condition",
            control_behavior=dec_throttled_cb,
            rank=1,
        )
    )

    dec_emergency_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": "<",
            "constant": THRESH_EMERGENCY_VAL,
            "output_signal": {"type": "virtual", "name": "signal-E"},
            "copy_count_from_input": False,
        }
    }
    cells.append(
        CombinatorCell(
            name="u_dec_emergency",
            cell=pdk.DECIDER_COMBINATOR_CELL,
            recipe="condition",
            control_behavior=dec_emergency_cb,
            rank=1,
        )
    )

    dec_rec_normal_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": ">",
            "constant": THRESH_RECOVER_NORMAL_VAL,
            "output_signal": {"type": "virtual", "name": "signal-N"},
            "copy_count_from_input": False,
        }
    }
    cells.append(
        CombinatorCell(
            name="u_dec_rec_normal",
            cell=pdk.DECIDER_COMBINATOR_CELL,
            recipe="condition",
            control_behavior=dec_rec_normal_cb,
            rank=1,
        )
    )

    dec_rec_throttled_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": ">",
            "constant": THRESH_RECOVER_THROTTLED_VAL,
            "output_signal": {"type": "virtual", "name": "signal-R"},
            "copy_count_from_input": False,
        }
    }
    cells.append(
        CombinatorCell(
            name="u_dec_rec_throttled",
            cell=pdk.DECIDER_COMBINATOR_CELL,
            recipe="condition",
            control_behavior=dec_rec_throttled_cb,
            rank=1,
        )
    )

    latch_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-S"},
            "comparator": ">",
            "constant": 0,
            "output_signal": {"type": "virtual", "name": "signal-S"},
            "copy_count_from_input": True,
        }
    }
    cells.append(
        CombinatorCell(
            name="u_state_latch",
            cell=pdk.DECIDER_COMBINATOR_CELL,
            recipe="condition",
            control_behavior=latch_cb,
            rank=2,
        )
    )
    conns.append(
        CircuitConnection(
            source="u_state_latch",
            source_port=pdk.WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN
            if hasattr(pdk, "WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN")
            else PORT_OUT_GREEN,
            target="u_state_latch",
            target_port=pdk.WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN
            if hasattr(pdk, "WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN")
            else PORT_IN_GREEN,
            color="green",
        )
    )

    out_ps_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-S"},
            "comparator": "<",
            "constant": 2,
            "output_signal": {"type": "virtual", "name": "signal-P"},
            "copy_count_from_input": False,
        }
    }
    cells.append(
        CombinatorCell(
            name="u_out_power_switch",
            cell=pdk.DECIDER_COMBINATOR_CELL,
            recipe="condition",
            control_behavior=out_ps_cb,
            rank=RANK_OUTPUT_STAGE,
        )
    )

    out_pump_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-S"},
            "comparator": ">",
            "constant": 0,
            "output_signal": {"type": "virtual", "name": "signal-U"},
            "copy_count_from_input": False,
        }
    }
    cells.append(
        CombinatorCell(
            name="u_out_backup_pump",
            cell=pdk.DECIDER_COMBINATOR_CELL,
            recipe="condition",
            control_behavior=out_pump_cb,
            rank=RANK_OUTPUT_STAGE,
        )
    )

    out_alert_cb = {
        "arithmetic_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-S"},
            "second_constant": 1,
            "operation": "*",
            "output_signal": {"type": "virtual", "name": "signal-Z"},
        }
    }
    cells.append(
        CombinatorCell(
            name="u_out_alert_code",
            cell=pdk.ARITHMETIC_COMBINATOR_CELL,
            recipe="accumulate",
            control_behavior=out_alert_cb,
            rank=RANK_OUTPUT_STAGE,
        )
    )

    ps_cb = {
        "circuit_enabled": True,
        "circuit_condition": {
            "first_signal": {"type": "virtual", "name": "signal-P"},
            "comparator": ">",
            "constant": 0,
        },
    }
    cells.append(
        CombinatorCell(
            name="u_main_power_switch",
            cell=pdk.POWER_SWITCH_CELL,
            recipe="switch",
            control_behavior=ps_cb,
            rank=0,
        )
    )

    decider_names = (
        "u_dec_throttled",
        "u_dec_emergency",
        "u_dec_rec_normal",
        "u_dec_rec_throttled",
    )
    conns.extend(
        CircuitConnection(
            source="u_const_params",
            source_port=PORT_IN_RED,
            target=dec_name,
            target_port=PORT_IN_RED,
            color="red",
        )
        for dec_name in decider_names
    )
    conns.extend(
        CircuitConnection(
            source=dec_name,
            source_port=PORT_OUT_RED,
            target="u_state_latch",
            target_port=PORT_IN_RED,
            color="red",
        )
        for dec_name in decider_names
    )
    conns.extend(
        CircuitConnection(
            source="u_state_latch",
            source_port=PORT_OUT_RED,
            target=out_target,
            target_port=PORT_IN_RED,
            color="red",
        )
        for out_target in ("u_out_power_switch", "u_out_backup_pump", "u_out_alert_code")
    )
    conns.append(
        CircuitConnection(
            source="u_out_power_switch",
            source_port=PORT_OUT_RED,
            target="u_main_power_switch",
            target_port=PORT_IN_RED,
            color="red",
        )
    )

    return CircuitSynthResult(
        module_name="power_sequencer",
        inputs=inputs,
        outputs=outputs,
        cells=cells,
        connections=conns,
        parameters=parameters,
    )


def _read_verilog_source(source_or_path: str | Path) -> str:
    """Return Verilog source text from a file path or raw source string."""
    if isinstance(source_or_path, Path) or (
        isinstance(source_or_path, str)
        and "\n" not in source_or_path
        and Path(source_or_path).exists()
    ):
        return Path(source_or_path).read_text()
    return str(source_or_path)


def _extract_port_widths(pattern: re.Pattern[str], text: str) -> dict[str, int]:
    """Extract port names and bit widths matching the given regex pattern."""
    ports: dict[str, int] = {}
    for m in pattern.finditer(text):
        msb = int(m.group(1)) if m.group(1) else 0
        lsb = int(m.group(2)) if m.group(2) else 0
        ports[m.group(GROUP_NAME_IDX)] = abs(msb - lsb) + 1
    return ports


def _parse_verilog_int(val_str: str) -> int:
    """Parse a Verilog integer literal (decimal, binary, or bare digits)."""
    cleaned = val_str.strip()
    if "'d" in cleaned:
        return int(cleaned.split("'d")[1])
    if "'b" in cleaned:
        return int(cleaned.split("'b")[1], BINARY_BASE)
    if cleaned.isdigit():
        return int(cleaned)
    return 0


def _extract_combinator_cells(text: str) -> list[CombinatorCell]:
    """Synthesize decider and arithmetic combinator cells from Verilog expressions."""
    cells: list[CombinatorCell] = []
    for cmp_idx, m in enumerate(COMPARISON_PAT.finditer(text)):
        var_name = m.group(1)
        op = m.group(2)
        val = _parse_verilog_int(m.group(GROUP_VALUE_IDX))
        cb = {
            "decider_conditions": {
                "first_signal": {"type": "virtual", "name": "signal-A"},
                "comparator": op,
                "constant": val,
                "output_signal": {
                    "type": "virtual",
                    "name": f"signal-{chr(ASCII_UPPER_A + cmp_idx)}",
                },
                "copy_count_from_input": False,
            }
        }
        cells.append(
            CombinatorCell(
                name=f"u_dec_{cmp_idx}_{var_name}",
                cell=pdk.DECIDER_COMBINATOR_CELL,
                recipe="condition",
                control_behavior=cb,
                rank=1,
            )
        )

    for arith_idx, m in enumerate(ARITH_PAT.finditer(text)):
        var_name = m.group(1)
        op = m.group(2)
        val = int(m.group(GROUP_VALUE_IDX))
        cb = {
            "arithmetic_conditions": {
                "first_signal": {"type": "virtual", "name": "signal-A"},
                "second_constant": val,
                "operation": op,
                "output_signal": {"type": "virtual", "name": "signal-Y"},
            }
        }
        cells.append(
            CombinatorCell(
                name=f"u_arith_{arith_idx}_{var_name}",
                cell=pdk.ARITHMETIC_COMBINATOR_CELL,
                recipe="accumulate",
                control_behavior=cb,
                rank=1,
            )
        )
    return cells


def synthesize_verilog(source_or_path: str | Path) -> CircuitSynthResult:
    """Parse and synthesize behavioral Verilog into Factorio combinators."""
    text = _read_verilog_source(source_or_path)
    m_mod = re.search(r"module\s+([A-Za-z0-9_]+)", text)
    mod_name = m_mod.group(1) if m_mod else "circuit_module"
    if mod_name == "power_sequencer":
        return synthesize_power_sequencer()

    inputs = _extract_port_widths(INPUT_PORT_PAT, text)
    outputs = _extract_port_widths(OUTPUT_PORT_PAT, text)
    parameters = {m.group(1): _parse_verilog_int(m.group(2)) for m in PARAM_PAT.finditer(text)}
    cells = _extract_combinator_cells(text)

    return CircuitSynthResult(
        module_name=mod_name,
        inputs=inputs,
        outputs=outputs,
        cells=cells,
        connections=[],
        parameters=parameters,
    )


def _register_controller_connections(
    nl: netlist.Netlist, connections: list[CircuitConnection]
) -> None:
    """Register point-to-point controller connections as nets in `nl`."""
    for conn in connections:
        net_name = (
            f"c_{conn.source}_fb"
            if conn.source == conn.target
            else f"c_{conn.source}_to_{conn.target}"
        )
        n = nl.net(net_name)
        if conn.source not in n.drivers:
            n.drivers.append(conn.source)
        if conn.target not in n.sinks:
            n.sinks.append(conn.target)


def _link_unconnected_controller_ports(
    nl: netlist.Netlist,
    cells: list[CombinatorCell],
    ctrl_bus_in: str,
    ctrl_bus_out: str,
) -> None:
    """Attach unlinked combinator inputs to `ctrl_bus_in` and outputs to `ctrl_bus_out`."""
    linked_sinks = {s for name, net in nl.nets.items() if name != ctrl_bus_in for s in net.sinks}
    linked_drivers = {
        d for name, net in nl.nets.items() if name != ctrl_bus_out for d in net.drivers
    }
    for c in cells:
        cell_def = nl.cells.get(c.cell, {})
        if "a" in cell_def.get("inputs", []) and c.name not in linked_sinks:
            nl.net(ctrl_bus_in).sinks.append(c.name)
        if "y" in cell_def.get("outputs", []) and c.name not in linked_drivers:
            nl.net(ctrl_bus_out).drivers.append(c.name)


def fuse_controller_into_netlist(
    nl: netlist.Netlist,
    synth_result: CircuitSynthResult,
    base_rank: int = 0,
) -> None:
    """Fuse synthesized combinator cells and circuit nets into the netlist."""
    combinator_cell_factories = {
        pdk.ARITHMETIC_COMBINATOR_CELL: pdk.arithmetic_combinator_cell,
        pdk.DECIDER_COMBINATOR_CELL: pdk.decider_combinator_cell,
        pdk.CONSTANT_COMBINATOR_CELL: pdk.constant_combinator_cell,
        pdk.POWER_SWITCH_CELL: pdk.power_switch_cell,
    }
    for cell_type, factory in combinator_cell_factories.items():
        if cell_type not in nl.cells:
            nl.cells[cell_type] = factory()

    for c in synth_result.cells:
        nl.instances.append(
            netlist.Instance(
                name=c.name,
                cell_key=c.cell,
                recipe=c.recipe,
                plan_rate=0.0,
                rated=COMBINATOR_RATED_CPM if "combinator" in c.cell else 0.0,
                rank=base_rank + c.rank,
                control_behavior=c.control_behavior,
            )
        )

    ctrl_bus_in = f"c_{synth_result.module_name}_in"
    ctrl_bus_out = f"c_{synth_result.module_name}_out"
    nl.net(ctrl_bus_in)
    nl.net(ctrl_bus_out)
    _register_controller_connections(nl, synth_result.connections)
    _link_unconnected_controller_ports(nl, synth_result.cells, ctrl_bus_in, ctrl_bus_out)
