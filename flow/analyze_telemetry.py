"""Shape-based telemetry analysis for Factorio factory netlists.

Analyzes runtime telemetry traces against netlist graph topology:
1. Boundary Inputs: supply continuity and feed rates.
2. Boundary Outputs: science pack throughput and pipeline latency.
3. Convergent Merges: splitter trees aggregated by material and belt saturation.
4. Transform Stages: recipe stoichiometry, mass conservation, and cell utilization.
5. Bottleneck Diagnosis: automated identification of starvation versus backpressure.
6. Propagation DAG: temporal wavefront delay along dependency paths.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TICKS_PER_MINUTE = 3600.0
TICKS_PER_SECOND = 60.0
DEFAULT_SAMPLE_INTERVAL = 60
SATISFACTION_RATIO = 0.9
BOTTLENECK_RATIO = 0.85
STARVATION_RATIO = 0.05
YELLOW_BELT_CAPACITY_SPM = 900.0
CONGESTION_SATURATION_PCT = 95.0
PERCENTAGE_SCALE = 100.0
MAX_REPORTED_BOTTLENECKS = 10

NON_RECIPE_CELLS = (
    "belt_merge2",
    "belt_split2",
    "display_panel",
    "arithmetic_combinator",
    "decider_combinator",
)


def _as_float(val: object, default: float = 0.0) -> float:
    """Convert an arbitrary object to float, or return default."""
    if isinstance(val, (int, float, str)):
        try:
            return float(val)
        except ValueError:
            return default
    return default


def _as_int(val: object, default: int = 0) -> int:
    """Convert an arbitrary object to int, or return default."""
    if isinstance(val, (int, float, str)):
        try:
            return int(val)
        except ValueError:
            return default
    return default


def _build_material_stat(
    sample_counts: list[tuple[int, int]],
    first_tick: int | None,
    duration_minutes: float,
) -> dict[str, Any]:
    """Compute summary dictionary for one material from its sample series."""
    initial_count = sample_counts[0][1] if sample_counts else 0
    last_count = sample_counts[-1][1] if sample_counts else 0
    total_items = max(0, last_count - initial_count)
    rate_spm = total_items / duration_minutes if duration_minutes > 0 else 0.0
    arrival_sec = round(first_tick / TICKS_PER_SECOND, 2) if first_tick is not None else None
    return {
        "initial_count": initial_count,
        "final_count": last_count,
        "total_items": total_items,
        "rate_spm": round(rate_spm, 2),
        "first_arrival_tick": first_tick,
        "first_arrival_seconds": arrival_sec,
        "samples": sample_counts,
    }


def _collect_all_materials(samples: Sequence[Mapping[str, object]]) -> list[str]:
    """Return sorted list of all material keys across telemetry samples."""
    all_materials: set[str] = set()
    for s in samples:
        taps = s.get("taps", {})
        if isinstance(taps, Mapping):
            all_materials.update(str(k) for k in taps.keys())
    return sorted(all_materials)


def calculate_net_rates(
    samples: Sequence[Mapping[str, object]],
    sample_interval: int,
    ticks_total: int,
) -> dict[str, dict[str, Any]]:
    """Compute throughput rates and item counts for each tapped net."""
    if not samples:
        return {}

    materials = _collect_all_materials(samples)
    counts_by_mat: dict[str, list[tuple[int, int]]] = {m: [] for m in materials}
    first_by_mat: dict[str, int | None] = {m: None for m in materials}

    for s in samples:
        tick = _as_int(s.get("tick", 0))
        taps_obj = s.get("taps", {})
        taps = taps_obj if isinstance(taps_obj, Mapping) else {}
        for mat in materials:
            cnt = _as_int(taps.get(mat, 0))
            counts_by_mat[mat].append((tick, cnt))
            if cnt > 0 and first_by_mat[mat] is None:
                first_by_mat[mat] = tick

    duration_minutes = (ticks_total / TICKS_PER_MINUTE) if ticks_total > 0 else 1.0
    return {
        mat: _build_material_stat(counts_by_mat[mat], first_by_mat[mat], duration_minutes)
        for mat in materials
    }


def _rate_percentage(observed: float, planned: float) -> float:
    """Compute observed-to-planned percentage."""
    if planned > 0:
        return round((observed / planned) * PERCENTAGE_SCALE, 1)
    return PERCENTAGE_SCALE if observed > 0 else 0.0


def _classify_rate_status(
    obs_rate: float,
    target_rate: float,
    labels: tuple[str, str, str],
) -> str:
    """Classify observed rate against target rate using three status labels."""
    if obs_rate >= target_rate * SATISFACTION_RATIO:
        return labels[0]
    if obs_rate > 0:
        return labels[1]
    return labels[2]


def analyze_inputs(
    netlist_data: Mapping[str, object],
    net_stats: Mapping[str, Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Analyze primary boundary input feeds."""
    raw_rates_obj = netlist_data.get("raw_rates_per_min", {})
    raw_rates = raw_rates_obj if isinstance(raw_rates_obj, Mapping) else {}
    inputs_obj = netlist_data.get("inputs", [])
    inputs = [str(x) for x in inputs_obj] if isinstance(inputs_obj, Sequence) else []
    results: list[dict[str, Any]] = []

    for mat in sorted(inputs):
        plan_rate = _as_float(raw_rates.get(mat, 0.0))
        stats = net_stats.get(mat, {})
        obs_rate = _as_float(stats.get("rate_spm", 0.0))
        total_items = _as_int(stats.get("total_items", 0))
        fulfillment_pct = _rate_percentage(obs_rate, plan_rate)
        status = _classify_rate_status(obs_rate, plan_rate, ("SATISFIED", "DEFICIENT", "IDLE"))

        results.append(
            {
                "material": mat,
                "plan_rate_spm": plan_rate,
                "observed_rate_spm": obs_rate,
                "total_items": total_items,
                "fulfillment_pct": fulfillment_pct,
                "status": status,
            }
        )

    return results


def analyze_outputs(
    netlist_data: Mapping[str, object],
    net_stats: Mapping[str, Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Analyze primary boundary science pack sinks."""
    demand_obj = netlist_data.get("demand", {})
    demand = demand_obj if isinstance(demand_obj, Mapping) else {}
    outputs_obj = netlist_data.get("outputs", [])
    outputs = [str(x) for x in outputs_obj] if isinstance(outputs_obj, Sequence) else []
    results: list[dict[str, Any]] = []

    for mat in sorted(outputs):
        target_rate = _as_float(demand.get(mat, 0.0))
        stats = net_stats.get(mat, {})
        obs_rate = _as_float(stats.get("rate_spm", 0.0))
        total_items = _as_int(stats.get("total_items", 0))
        first_tick_obj = stats.get("first_arrival_tick")
        first_tick = _as_int(first_tick_obj) if first_tick_obj is not None else None
        achievement_pct = _rate_percentage(obs_rate, target_rate)
        status = _classify_rate_status(
            obs_rate, target_rate, ("ON_TARGET", "BELOW_TARGET", "NO_PRODUCTION")
        )
        arrival_sec = round(first_tick / TICKS_PER_SECOND, 2) if first_tick is not None else None

        results.append(
            {
                "material": mat,
                "target_rate_spm": target_rate,
                "observed_rate_spm": obs_rate,
                "total_items": total_items,
                "achievement_pct": achievement_pct,
                "first_arrival_tick": first_tick,
                "first_arrival_sec": arrival_sec,
                "status": status,
            }
        )

    return results


def analyze_merges(
    netlist_data: Mapping[str, object],
    net_stats: Mapping[str, Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Analyze convergent splitter merge trees aggregated by material."""
    instances_obj = netlist_data.get("instances", [])
    instances = instances_obj if isinstance(instances_obj, Sequence) else []
    merges_by_mat: dict[str, int] = {}
    for inst in instances:
        if isinstance(inst, Mapping) and inst.get("cell") in ("belt_merge2", "belt_split2"):
            mat = str(inst.get("recipe", ""))
            merges_by_mat[mat] = merges_by_mat.get(mat, 0) + 1

    results: list[dict[str, Any]] = []
    for mat, count in sorted(merges_by_mat.items()):
        stats = net_stats.get(mat, {})
        obs_rate = _as_float(stats.get("rate_spm", 0.0))
        # Standard yellow transport belt bandwidth: 15 items/sec = 900 items/min
        saturation_pct = round((obs_rate / YELLOW_BELT_CAPACITY_SPM) * PERCENTAGE_SCALE, 1)
        is_congested = saturation_pct >= CONGESTION_SATURATION_PCT

        results.append(
            {
                "material": mat,
                "splitters_count": count,
                "throughput_spm": obs_rate,
                "belt_saturation_pct": saturation_pct,
                "is_congested": is_congested,
            }
        )

    return results


def _group_recipe_instances(
    instances: Sequence[object],
) -> dict[str, list[Mapping[str, object]]]:
    """Group non-routing netlist instances by recipe name."""
    recipe_instances: dict[str, list[Mapping[str, object]]] = {}
    for inst in instances:
        if not isinstance(inst, Mapping):
            continue
        cell_id = str(inst.get("cell", ""))
        if cell_id in NON_RECIPE_CELLS:
            continue
        rec = str(inst.get("recipe", ""))
        if rec:
            recipe_instances.setdefault(rec, []).append(inst)
    return recipe_instances


def analyze_transform_stages(
    netlist_data: Mapping[str, object],
    net_stats: Mapping[str, Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Analyze recipe stages: stoichiometry conservation and machine utilization."""
    cells_obj = netlist_data.get("cells", {})
    cells = cells_obj if isinstance(cells_obj, Mapping) else {}
    instances_obj = netlist_data.get("instances", [])
    instances = instances_obj if isinstance(instances_obj, Sequence) else []
    results: list[dict[str, Any]] = []

    recipe_instances = _group_recipe_instances(instances)
    for rec, inst_list in sorted(recipe_instances.items()):
        first_cell_id = str(inst_list[0].get("cell", ""))
        cell_info_obj = cells.get(first_cell_id, {})
        cell_info = cell_info_obj if isinstance(cell_info_obj, Mapping) else {}
        inputs_obj = cell_info.get("inputs", [])
        inputs = [str(x) for x in inputs_obj] if isinstance(inputs_obj, Sequence) else []
        machine = str(cell_info.get("machine", "assembling-machine"))

        total_plan_rate = sum(_as_float(i.get("plan_rate_per_min", 0.0)) for i in inst_list)
        total_rated_capacity = sum(_as_float(i.get("rated_per_min", 0.0)) for i in inst_list)

        output_stats = net_stats.get(rec, {})
        obs_output_rate = _as_float(output_stats.get("rate_spm", 0.0))
        utilization_pct = (
            round((obs_output_rate / total_rated_capacity) * PERCENTAGE_SCALE, 1)
            if total_rated_capacity > 0
            else 0.0
        )

        input_flows = [
            {
                "input_material": inp,
                "rate_spm": _as_float(net_stats.get(inp, {}).get("rate_spm", 0.0)),
            }
            for inp in inputs
        ]

        results.append(
            {
                "recipe": rec,
                "machine": machine,
                "instance_count": len(inst_list),
                "plan_rate_spm": round(total_plan_rate, 2),
                "rated_capacity_spm": round(total_rated_capacity, 2),
                "observed_output_rate_spm": obs_output_rate,
                "utilization_pct": utilization_pct,
                "inputs": input_flows,
            }
        )

    return results


def _diagnose_single_stage(stage: Mapping[str, object]) -> dict[str, Any] | None:
    """Evaluate one transform stage for starvation or backpressure."""
    rec = str(stage.get("recipe", ""))
    plan_rate = _as_float(stage.get("plan_rate_spm", 0.0))
    obs_rate = _as_float(stage.get("observed_output_rate_spm", 0.0))

    if plan_rate <= 0 or obs_rate >= plan_rate * BOTTLENECK_RATIO:
        return None

    deficit = round(plan_rate - obs_rate, 2)
    inputs_obj = stage.get("inputs", [])
    inputs = inputs_obj if isinstance(inputs_obj, Sequence) else []
    starving_inputs = [
        str(inp_flow["input_material"])
        for inp_flow in inputs
        if isinstance(inp_flow, Mapping)
        and _as_float(inp_flow.get("rate_spm", 0.0)) <= STARVATION_RATIO * plan_rate
    ]

    if starving_inputs:
        return {
            "severity": "HIGH",
            "type": "STAGE_STARVATION",
            "target": rec,
            "description": (
                f"Recipe '{rec}' starved by upstream input(s): {', '.join(starving_inputs)}."
            ),
            "impact_score": deficit,
        }
    return {
        "severity": "MEDIUM",
        "type": "STAGE_BACKPRESSURE",
        "target": rec,
        "description": (
            f"Recipe '{rec}' has inputs available but outputs are stalled. "
            f"Output belt backpressure or machine transfer delay suspected."
        ),
        "impact_score": deficit,
    }


def diagnose_bottlenecks(
    stages: Sequence[Mapping[str, object]],
    input_results: Sequence[Mapping[str, object]],
    net_stats: Mapping[str, Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Diagnose factory bottlenecks: differentiate starvation from backpressure."""
    bottlenecks: list[dict[str, Any]] = []

    for inp in input_results:
        if inp.get("status") == "DEFICIENT":
            mat = str(inp.get("material", ""))
            obs = _as_float(inp.get("observed_rate_spm", 0.0))
            plan = _as_float(inp.get("plan_rate_spm", 0.0))
            pct = _as_float(inp.get("fulfillment_pct", 0.0))
            bottlenecks.append(
                {
                    "severity": "HIGH",
                    "type": "INPUT_STARVATION",
                    "target": mat,
                    "description": (
                        f"Raw input '{mat}' delivers {obs:.1f}/min "
                        f"({pct}% of planned {plan:.1f}/min)."
                    ),
                    "impact_score": round(plan - obs, 2),
                }
            )

    for stage in stages:
        diag = _diagnose_single_stage(stage)
        if diag is not None:
            bottlenecks.append(diag)

    bottlenecks.sort(key=lambda b: _as_float(b.get("impact_score", 0.0)), reverse=True)
    return bottlenecks


def _compute_transit_latency(inp_tick: object, out_tick: object) -> tuple[int | None, float | None]:
    """Compute tick and second transit latency between upstream and downstream ticks."""
    if isinstance(inp_tick, int) and isinstance(out_tick, int) and out_tick >= inp_tick:
        ticks = out_tick - inp_tick
        return ticks, round(ticks / TICKS_PER_SECOND, 2)
    return None, None


def _extract_cell_inputs(cells: Mapping[str, object], cell_id: str) -> list[str]:
    """Return the list of input material names for a cell definition."""
    cell_info_obj = cells.get(cell_id, {})
    cell_info = cell_info_obj if isinstance(cell_info_obj, Mapping) else {}
    inputs_obj = cell_info.get("inputs", [])
    return [str(x) for x in inputs_obj] if isinstance(inputs_obj, Sequence) else []


def analyze_propagation_dag(
    netlist_data: Mapping[str, object],
    net_stats: Mapping[str, Mapping[str, object]],
) -> list[dict[str, Any]]:
    """Trace signal wavefront arrival latency across the dependency DAG."""
    cells_obj = netlist_data.get("cells", {})
    cells = cells_obj if isinstance(cells_obj, Mapping) else {}
    instances_obj = netlist_data.get("instances", [])
    instances = instances_obj if isinstance(instances_obj, Sequence) else []
    dag_edges: list[dict[str, Any]] = []

    for inst in instances:
        if not isinstance(inst, Mapping):
            continue
        cell_id = str(inst.get("cell", ""))
        if cell_id in NON_RECIPE_CELLS:
            continue
        rec = str(inst.get("recipe", ""))
        out_tick = net_stats.get(rec, {}).get("first_arrival_tick")
        for inp in _extract_cell_inputs(cells, cell_id):
            inp_tick = net_stats.get(inp, {}).get("first_arrival_tick")
            latency_ticks, latency_sec = _compute_transit_latency(inp_tick, out_tick)
            dag_edges.append(
                {
                    "upstream": inp,
                    "downstream": rec,
                    "upstream_arrival_tick": inp_tick,
                    "downstream_arrival_tick": out_tick,
                    "transit_latency_ticks": latency_ticks,
                    "transit_latency_sec": latency_sec,
                }
            )

    return dag_edges


def analyze_shapes(
    trace_data: Mapping[str, object],
    netlist_data: Mapping[str, object],
) -> dict[str, Any]:
    """Execute complete shape-based telemetry analysis."""
    ticks_total = _as_int(trace_data.get("ticks_total", 0))
    sample_interval = _as_int(trace_data.get("sample_interval", DEFAULT_SAMPLE_INTERVAL))
    samples_obj = trace_data.get("samples", [])
    samples = (
        [s for s in samples_obj if isinstance(s, Mapping)]
        if isinstance(samples_obj, Sequence)
        else []
    )

    net_stats = calculate_net_rates(samples, sample_interval, ticks_total)
    inputs_analysis = analyze_inputs(netlist_data, net_stats)
    outputs_analysis = analyze_outputs(netlist_data, net_stats)
    merges_analysis = analyze_merges(netlist_data, net_stats)
    stages_analysis = analyze_transform_stages(netlist_data, net_stats)
    bottlenecks = diagnose_bottlenecks(stages_analysis, inputs_analysis, net_stats)
    dag_analysis = analyze_propagation_dag(netlist_data, net_stats)

    total_items_recorded = sum(_as_int(s["total_items"]) for s in net_stats.values())

    return {
        "summary": {
            "simulation_ticks": ticks_total,
            "simulation_seconds": round(ticks_total / TICKS_PER_SECOND, 2),
            "sample_interval_ticks": sample_interval,
            "total_samples": len(samples),
            "tapped_nets_count": len(net_stats),
            "total_item_transfers_recorded": total_items_recorded,
        },
        "boundary_inputs": inputs_analysis,
        "boundary_outputs": outputs_analysis,
        "convergent_merges": merges_analysis,
        "transform_stages": stages_analysis,
        "bottlenecks": bottlenecks,
        "propagation_dag": dag_analysis,
        "net_statistics": net_stats,
    }


def _format_bottlenecks_section(bottlenecks: Sequence[Mapping[str, object]]) -> list[str]:
    """Format the bottleneck diagnosis markdown section."""
    lines = ["## 3. Bottleneck Diagnosis"]
    if bottlenecks:
        for idx, bn in enumerate(bottlenecks[:MAX_REPORTED_BOTTLENECKS], 1):
            lines.append(f"{idx}. **[{bn['severity']}] {bn['type']}** on `{bn['target']}`")
            lines.append(f"   {bn['description']}")
    else:
        lines.append("No active starvation or backpressure bottlenecks detected.\n")
    lines.append("")
    return lines


def _format_merges_section(merges: Sequence[Mapping[str, object]]) -> list[str]:
    """Format the convergent splitter merges markdown section."""
    lines = ["## 5. Convergent Splitter Merges (By Material)"]
    if merges:
        lines.append("| Material | Splitters | Throughput (SPM) | Belt Saturation | Congestion |")
        lines.append("|:---|:---:|:---:|:---:|:---:|")
        for m in merges:
            cong = "**CONGESTED**" if m.get("is_congested") else "Normal"
            lines.append(
                f"| `{m['material']}` | {m['splitters_count']} | {_as_float(m['throughput_spm']):.1f} | "
                f"{_as_float(m['belt_saturation_pct']):.1f}% | {cong} |"
            )
        lines.append("")
    return lines


def format_markdown_report(analysis: Mapping[str, object]) -> str:
    """Format analysis findings as clean technical markdown."""
    summary_obj = analysis.get("summary", {})
    summary: dict[str, Any] = dict(summary_obj) if isinstance(summary_obj, Mapping) else {}
    outputs_obj = analysis.get("boundary_outputs", [])
    boundary_outputs: list[Any] = list(outputs_obj) if isinstance(outputs_obj, Sequence) else []
    inputs_obj = analysis.get("boundary_inputs", [])
    boundary_inputs: list[Any] = list(inputs_obj) if isinstance(inputs_obj, Sequence) else []
    stages_obj = analysis.get("transform_stages", [])
    transform_stages: list[Any] = list(stages_obj) if isinstance(stages_obj, Sequence) else []
    bns_obj = analysis.get("bottlenecks", [])
    bottlenecks = (
        [b for b in bns_obj if isinstance(b, Mapping)] if isinstance(bns_obj, Sequence) else []
    )
    merges_obj = analysis.get("convergent_merges", [])
    merges = (
        [m for m in merges_obj if isinstance(m, Mapping)]
        if isinstance(merges_obj, Sequence)
        else []
    )

    out: list[str] = [
        "# Factory Telemetry Shape Analysis Report\n",
        "## 1. Simulation Summary",
        f"- **Simulation Duration**: {summary['simulation_ticks']} ticks ({summary['simulation_seconds']:.1f} seconds)",
        f"- **Sample Interval**: {summary['sample_interval_ticks']} ticks ({summary['total_samples']} samples)",
        f"- **Tapped Nets**: {summary['tapped_nets_count']} telemetry points",
        f"- **Item Movements Logged**: {summary['total_item_transfers_recorded']} units\n",
        "## 2. Boundary Outputs (Science Pack Throughput)",
        "| Science Pack | Target (SPM) | Observed (SPM) | Progress | First Arrival | Status |",
        "|:---|:---:|:---:|:---:|:---:|:---|",
    ]
    for row in boundary_outputs:
        arr = f"{row['first_arrival_sec']:.1f}s" if row["first_arrival_sec"] is not None else "None"
        out.append(
            f"| `{row['material']}` | {row['target_rate_spm']:.1f} | {row['observed_rate_spm']:.2f} | "
            f"{row['achievement_pct']:.1f}% | {arr} | **{row['status']}** |"
        )
    out.append("")
    out.extend(_format_bottlenecks_section(bottlenecks))

    out.append("## 4. Boundary Inputs (Supply Continuity)")
    out.append(
        "| Input Material | Planned (SPM) | Observed (SPM) | Fulfillment | Total Units | Status |"
    )
    out.append("|:---|:---:|:---:|:---:|:---:|:---|")
    for row in boundary_inputs:
        out.append(
            f"| `{row['material']}` | {row['plan_rate_spm']:.1f} | {row['observed_rate_spm']:.2f} | "
            f"{row['fulfillment_pct']:.1f}% | {row['total_items']} | {row['status']} |"
        )
    out.append("")
    out.extend(_format_merges_section(merges))

    out.append("## 6. Recipe Transform Stages (Utilization)")
    out.append("| Recipe | Machine | Instances | Plan (SPM) | Observed (SPM) | Utilization |")
    out.append("|:---|:---|:---:|:---:|:---:|:---:|")
    for stg in transform_stages:
        out.append(
            f"| `{stg['recipe']}` | {stg['machine']} | {stg['instance_count']} | "
            f"{stg['plan_rate_spm']:.1f} | {stg['observed_output_rate_spm']:.2f} | "
            f"{stg['utilization_pct']:.1f}% |"
        )
    out.append("")

    return "\n".join(out)


def main() -> int:
    """CLI entrypoint for standalone telemetry analysis."""
    parser = argparse.ArgumentParser(
        description="Analyze Factorio factory telemetry traces based on netlist shapes."
    )
    parser.add_argument("trace_file", type=Path, help="Path to telemetry trace JSON file")
    parser.add_argument("netlist_file", type=Path, help="Path to netlist JSON file")
    parser.add_argument(
        "--json-out", type=Path, default=None, help="Path to export analysis results as JSON"
    )

    args = parser.parse_args()
    trace_data = json.loads(args.trace_file.read_text())
    netlist_data = json.loads(args.netlist_file.read_text())

    results = analyze_shapes(trace_data, netlist_data)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        # Avoid circular references or non-serializable objects
        args.json_out.write_text(json.dumps(results, indent=2))
        print(f"Exported JSON analysis to: {args.json_out}")

    report = format_markdown_report(results)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
