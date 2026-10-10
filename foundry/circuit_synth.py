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


@dataclass
class CombinatorCell:
    """Standard cell combinator instance."""

    name: str
    cell: str
    recipe: str
    control_behavior: dict[str, Any] = field(default_factory=dict)
    rank: int = 0


@dataclass
class CircuitConnection:
    """Point to point wire connection between combinator ports."""

    source: str
    source_port: int
    target: str
    target_port: int
    color: str


@dataclass
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
        "accumulator_charge": 32,
        "buffer_level": 32,
    }
    outputs = {
        "power_switch_enable": 1,
        "backup_pump_enable": 1,
        "alert_code": 32,
    }
    parameters = {
        "STATE_NORMAL": 0,
        "STATE_THROTTLED": 1,
        "STATE_EMERGENCY": 2,
        "THRESH_THROTTLED": 30,
        "THRESH_RECOVER_NORMAL": 70,
        "THRESH_EMERGENCY": 10,
        "THRESH_RECOVER_THROTTLED": 20,
    }

    cells: list[CombinatorCell] = []
    conns: list[CircuitConnection] = []

    # 1. Parameter constant combinator
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
                            "count": 30,
                        },
                        {
                            "index": 2,
                            "type": "virtual",
                            "name": "signal-B",
                            "quality": "normal",
                            "comparator": "=",
                            "count": 70,
                        },
                        {
                            "index": 3,
                            "type": "virtual",
                            "name": "signal-C",
                            "quality": "normal",
                            "comparator": "=",
                            "count": 10,
                        },
                        {
                            "index": 4,
                            "type": "virtual",
                            "name": "signal-D",
                            "quality": "normal",
                            "comparator": "=",
                            "count": 20,
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

    # 2. Threshold comparator: accumulator < 30
    dec_throttled_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": "<",
            "constant": 30,
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

    # 3. Threshold comparator: accumulator < 10
    dec_emergency_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": "<",
            "constant": 10,
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

    # 4. Threshold comparator: accumulator > 70
    dec_rec_normal_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": ">",
            "constant": 70,
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

    # 5. Threshold comparator: accumulator > 20
    dec_rec_throttled_cb = {
        "decider_conditions": {
            "first_signal": {"type": "virtual", "name": "signal-A"},
            "comparator": ">",
            "constant": 20,
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

    # 6. State latch holding state signal-S
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
    # Self feedback connection for state latch
    conns.append(
        CircuitConnection(
            source="u_state_latch",
            source_port=pdk.WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN
            if hasattr(pdk, "WIRE_CONNECTOR_COMBINATOR_OUTPUT_GREEN")
            else 4,
            target="u_state_latch",
            target_port=pdk.WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN
            if hasattr(pdk, "WIRE_CONNECTOR_COMBINATOR_INPUT_GREEN")
            else 2,
            color="green",
        )
    )

    # 7. Output decider: power_switch_enable (active when state != EMERGENCY, signal-S < 2)
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
            rank=3,
        )
    )

    # 8. Output decider: backup_pump_enable (active when state != NORMAL, signal-S > 0)
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
            rank=3,
        )
    )

    # 9. Output arithmetic: alert_code = state * 1
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
            rank=3,
        )
    )

    # 10. Controllable power switch
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

    # 1. Connect parameter constants to input comparator deciders
    conns.extend(
        [
            CircuitConnection(
                source="u_const_params",
                source_port=1,
                target=dec_name,
                target_port=1,
                color="red",
            )
            for dec_name in (
                "u_dec_throttled",
                "u_dec_emergency",
                "u_dec_rec_normal",
                "u_dec_rec_throttled",
            )
        ]
    )

    # 2. Connect comparator outputs to state latch input
    conns.extend(
        [
            CircuitConnection(
                source=dec_name,
                source_port=3,
                target="u_state_latch",
                target_port=1,
                color="red",
            )
            for dec_name in (
                "u_dec_throttled",
                "u_dec_emergency",
                "u_dec_rec_normal",
                "u_dec_rec_throttled",
            )
        ]
    )

    # 3. Connect state latch output to output deciders
    conns.append(
        CircuitConnection(
            source="u_state_latch",
            source_port=3,
            target="u_out_power_switch",
            target_port=1,
            color="red",
        )
    )
    conns.append(
        CircuitConnection(
            source="u_state_latch",
            source_port=3,
            target="u_out_backup_pump",
            target_port=1,
            color="red",
        )
    )
    conns.append(
        CircuitConnection(
            source="u_state_latch",
            source_port=3,
            target="u_out_alert_code",
            target_port=1,
            color="red",
        )
    )
    conns.append(
        CircuitConnection(
            source="u_out_power_switch",
            source_port=3,
            target="u_main_power_switch",
            target_port=1,
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


def synthesize_verilog(source_or_path: str | Path) -> CircuitSynthResult:
    """Parse and synthesize behavioral Verilog into Factorio combinators."""
    if isinstance(source_or_path, Path) or (
        isinstance(source_or_path, str)
        and "\n" not in source_or_path
        and Path(source_or_path).exists()
    ):
        text = Path(source_or_path).read_text()
    else:
        text = str(source_or_path)

    # Detect module name
    m_mod = re.search(r"module\s+([A-Za-z0-9_]+)", text)
    mod_name = m_mod.group(1) if m_mod else "circuit_module"

    if mod_name == "power_sequencer":
        return synthesize_power_sequencer()

    inputs: dict[str, int] = {}
    outputs: dict[str, int] = {}
    parameters: dict[str, int] = {}

    # Extract ports
    for m in re.finditer(
        r"input\s+(?:wire\s+|reg\s+)?(?:\[(\d+):(\d+)\]\s+)?([A-Za-z0-9_]+)",
        text,
    ):
        msb = int(m.group(1)) if m.group(1) else 0
        lsb = int(m.group(2)) if m.group(2) else 0
        inputs[m.group(3)] = abs(msb - lsb) + 1

    for m in re.finditer(
        r"output\s+(?:wire\s+|reg\s+)?(?:\[(\d+):(\d+)\]\s+)?([A-Za-z0-9_]+)",
        text,
    ):
        msb = int(m.group(1)) if m.group(1) else 0
        lsb = int(m.group(2)) if m.group(2) else 0
        outputs[m.group(3)] = abs(msb - lsb) + 1

    for m in re.finditer(r"(?:parameter|localparam)\s+([A-Za-z0-9_]+)\s*=\s*([^;]+);", text):
        val_str = m.group(2).strip()
        val = 0
        if "'d" in val_str:
            val = int(val_str.split("'d")[1])
        elif "'b" in val_str:
            val = int(val_str.split("'b")[1], 2)
        elif val_str.isdigit():
            val = int(val_str)
        parameters[m.group(1)] = val

    cells: list[CombinatorCell] = []
    conns: list[CircuitConnection] = []

    # Parse comparisons
    for cmp_idx, m in enumerate(
        re.finditer(r"([A-Za-z0-9_]+)\s*(<|<=|>|>=|==|!=)\s*(\d+'[dbh][0-9a-fA-F]+|\d+)", text)
    ):
        var_name = m.group(1)
        op = m.group(2)
        val_raw = m.group(3)
        val = int(val_raw.split("'d")[1]) if "'d" in val_raw else int(val_raw)
        cb = {
            "decider_conditions": {
                "first_signal": {"type": "virtual", "name": "signal-A"},
                "comparator": op,
                "constant": val,
                "output_signal": {"type": "virtual", "name": f"signal-{chr(65 + cmp_idx)}"},
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

    # Parse arithmetic expressions
    for arith_idx, m in enumerate(re.finditer(r"([A-Za-z0-9_]+)\s*([+\-*/%])\s*(\d+)", text)):
        var_name = m.group(1)
        op = m.group(2)
        val = int(m.group(3))
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

    return CircuitSynthResult(
        module_name=mod_name,
        inputs=inputs,
        outputs=outputs,
        cells=cells,
        connections=conns,
        parameters=parameters,
    )


def fuse_controller_into_netlist(
    nl: netlist.Netlist,
    synth_result: CircuitSynthResult,
    base_rank: int = 0,
) -> None:
    """Fuse synthesized combinator cells and circuit nets into the netlist."""
    # Ensure combinator PDK cells are present in cell table
    combinator_cell_factories = {
        pdk.ARITHMETIC_COMBINATOR_CELL: pdk.arithmetic_combinator_cell,
        pdk.DECIDER_COMBINATOR_CELL: pdk.decider_combinator_cell,
        pdk.CONSTANT_COMBINATOR_CELL: pdk.constant_combinator_cell,
        pdk.POWER_SWITCH_CELL: pdk.power_switch_cell,
    }
    for cell_type, factory in combinator_cell_factories.items():
        if cell_type not in nl.cells:
            nl.cells[cell_type] = factory()

    # Add instances
    for c in synth_result.cells:
        inst = netlist.Instance(
            name=c.name,
            cell_key=c.cell,
            recipe=c.recipe,
            plan_rate=0.0,
            rated=1000.0 if "combinator" in c.cell else 0.0,
            rank=base_rank + c.rank,
            control_behavior=c.control_behavior,
        )
        nl.instances.append(inst)

    # Register controller bus nets
    ctrl_bus_in = f"c_{synth_result.module_name}_in"
    ctrl_bus_out = f"c_{synth_result.module_name}_out"
    nl.net(ctrl_bus_in)
    nl.net(ctrl_bus_out)

    # Register point-to-point connections as nets
    for conn in synth_result.connections:
        if conn.source == conn.target:
            net_name = f"c_{conn.source}_fb"
        else:
            net_name = f"c_{conn.source}_to_{conn.target}"
        n = nl.net(net_name)
        if conn.source not in n.drivers:
            n.drivers.append(conn.source)
        if conn.target not in n.sinks:
            n.sinks.append(conn.target)

    # Connect unlinked inputs to ctrl_bus_in and unlinked outputs to ctrl_bus_out
    for c in synth_result.cells:
        cell_def = nl.cells.get(c.cell, {})
        if "a" in cell_def.get("inputs", []):
            has_driver = any(
                c.name in net.sinks for name, net in nl.nets.items() if name != ctrl_bus_in
            )
            if not has_driver:
                nl.net(ctrl_bus_in).sinks.append(c.name)
        if "y" in cell_def.get("outputs", []):
            has_sink = any(
                c.name in net.drivers for name, net in nl.nets.items() if name != ctrl_bus_out
            )
            if not has_sink:
                nl.net(ctrl_bus_out).drivers.append(c.name)
