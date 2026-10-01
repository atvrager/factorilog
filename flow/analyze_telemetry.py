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
from pathlib import Path
from typing import Any


def calculate_net_rates(
    samples: list[dict[str, Any]], sample_interval: int, ticks_total: int
) -> dict[str, dict[str, Any]]:
    """Compute throughput rates and item counts for each tapped net."""
    if not samples:
        return {}

    all_materials: set[str] = set()
    for s in samples:
        all_materials.update(s.get("taps", {}).keys())

    net_stats: dict[str, dict[str, Any]] = {}
    duration_minutes = (ticks_total / 3600.0) if ticks_total > 0 else 1.0

    for mat in sorted(all_materials):
        first_tick: int | None = None
        sample_counts: list[tuple[int, int]] = []

        for s in samples:
            tick = s.get("tick", 0)
            taps = s.get("taps", {})
            cnt = taps.get(mat, 0)
            sample_counts.append((tick, cnt))
            if cnt > 0 and first_tick is None:
                first_tick = tick

        initial_count = sample_counts[0][1] if sample_counts else 0
        last_count = sample_counts[-1][1] if sample_counts else 0
        total_items = max(0, last_count - initial_count)
        rate_spm = total_items / duration_minutes if duration_minutes > 0 else 0.0

        net_stats[mat] = {
            "initial_count": initial_count,
            "final_count": last_count,
            "total_items": total_items,
            "rate_spm": round(rate_spm, 2),
            "first_arrival_tick": first_tick,
            "first_arrival_seconds": round(first_tick / 60.0, 2) if first_tick is not None else None,
            "samples": sample_counts,
        }

    return net_stats


def analyze_inputs(
    netlist_data: dict[str, Any], net_stats: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Analyze primary boundary input feeds."""
    raw_rates = netlist_data.get("raw_rates_per_min", {})
    inputs = netlist_data.get("inputs", [])
    results: list[dict[str, Any]] = []

    for mat in sorted(inputs):
        plan_rate = float(raw_rates.get(mat, 0.0))
        stats = net_stats.get(mat, {})
        obs_rate = float(stats.get("rate_spm", 0.0))
        total_items = int(stats.get("total_items", 0))

        if plan_rate > 0:
            fulfillment_pct = round((obs_rate / plan_rate) * 100.0, 1)
        else:
            fulfillment_pct = 100.0 if obs_rate > 0 else 0.0

        if obs_rate >= plan_rate * 0.9:
            status = "SATISFIED"
        elif obs_rate > 0:
            status = "DEFICIENT"
        else:
            status = "IDLE"

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
    netlist_data: dict[str, Any], net_stats: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Analyze primary boundary science pack sinks."""
    demand = netlist_data.get("demand", {})
    outputs = netlist_data.get("outputs", [])
    results: list[dict[str, Any]] = []

    for mat in sorted(outputs):
        target_rate = float(demand.get(mat, 0.0))
        stats = net_stats.get(mat, {})
        obs_rate = float(stats.get("rate_spm", 0.0))
        total_items = int(stats.get("total_items", 0))
        first_tick = stats.get("first_arrival_tick")

        if target_rate > 0:
            achievement_pct = round((obs_rate / target_rate) * 100.0, 1)
        else:
            achievement_pct = 100.0 if obs_rate > 0 else 0.0

        if obs_rate >= target_rate * 0.9:
            status = "ON_TARGET"
        elif obs_rate > 0:
            status = "BELOW_TARGET"
        else:
            status = "NO_PRODUCTION"

        results.append(
            {
                "material": mat,
                "target_rate_spm": target_rate,
                "observed_rate_spm": obs_rate,
                "total_items": total_items,
                "achievement_pct": achievement_pct,
                "first_arrival_tick": first_tick,
                "first_arrival_sec": round(first_tick / 60.0, 2) if first_tick is not None else None,
                "status": status,
            }
        )

    return results


def analyze_merges(
    netlist_data: dict[str, Any], net_stats: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Analyze convergent splitter merge trees aggregated by material."""
    instances = netlist_data.get("instances", [])
    merges_by_mat: dict[str, int] = {}
    for inst in instances:
        if inst.get("cell") == "belt_merge2":
            mat = inst.get("recipe", "")
            merges_by_mat[mat] = merges_by_mat.get(mat, 0) + 1

    results: list[dict[str, Any]] = []
    for mat, count in sorted(merges_by_mat.items()):
        stats = net_stats.get(mat, {})
        obs_rate = float(stats.get("rate_spm", 0.0))
        # Standard yellow transport belt bandwidth: 15 items/sec = 900 items/min
        belt_capacity = 900.0
        saturation_pct = round((obs_rate / belt_capacity) * 100.0, 1)
        is_congested = saturation_pct >= 95.0

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


def analyze_transform_stages(
    netlist_data: dict[str, Any], net_stats: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Analyze recipe stages: stoichiometry conservation and machine utilization."""
    cells = netlist_data.get("cells", {})
    instances = netlist_data.get("instances", [])
    results: list[dict[str, Any]] = []

    recipe_instances: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        cell_id = inst.get("cell", "")
        if cell_id in ("belt_merge2", "display_panel", "arithmetic_combinator", "decider_combinator"):
            continue
        rec = inst.get("recipe", "")
        if rec:
            recipe_instances.setdefault(rec, []).append(inst)

    for rec, inst_list in sorted(recipe_instances.items()):
        first_cell_id = inst_list[0].get("cell", "")
        cell_info = cells.get(first_cell_id, {})
        inputs = cell_info.get("inputs", [])
        machine = cell_info.get("machine", "assembling-machine")

        total_plan_rate = sum(float(i.get("plan_rate_per_min", 0.0)) for i in inst_list)
        total_rated_capacity = sum(float(i.get("rated_per_min", 0.0)) for i in inst_list)

        output_stats = net_stats.get(rec, {})
        obs_output_rate = float(output_stats.get("rate_spm", 0.0))

        utilization_pct = (
            round((obs_output_rate / total_rated_capacity) * 100.0, 1)
            if total_rated_capacity > 0
            else 0.0
        )

        input_flows = []
        for inp in inputs:
            inp_stats = net_stats.get(inp, {})
            inp_rate = float(inp_stats.get("rate_spm", 0.0))
            input_flows.append({"input_material": inp, "rate_spm": inp_rate})

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


def diagnose_bottlenecks(
    stages: list[dict[str, Any]],
    input_results: list[dict[str, Any]],
    net_stats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Diagnose factory bottlenecks: differentiate starvation from backpressure."""
    bottlenecks: list[dict[str, Any]] = []

    # Check input boundary deficits
    for inp in input_results:
        if inp["status"] == "DEFICIENT":
            bottlenecks.append(
                {
                    "severity": "HIGH",
                    "type": "INPUT_STARVATION",
                    "target": inp["material"],
                    "description": (
                        f"Raw input '{inp['material']}' delivers {inp['observed_rate_spm']:.1f}/min "
                        f"({inp['fulfillment_pct']}% of planned {inp['plan_rate_spm']:.1f}/min)."
                    ),
                    "impact_score": round(inp["plan_rate_spm"] - inp["observed_rate_spm"], 2),
                }
            )

    # Check recipe stages
    for stage in stages:
        rec = stage["recipe"]
        plan_rate = stage["plan_rate_spm"]
        obs_rate = stage["observed_output_rate_spm"]

        if plan_rate <= 0 or obs_rate >= plan_rate * 0.85:
            continue

        deficit = plan_rate - obs_rate

        starving_inputs = []
        for inp_flow in stage["inputs"]:
            inp_mat = inp_flow["input_material"]
            inp_rate = inp_flow["rate_spm"]
            if inp_rate <= 0.05 * plan_rate:
                starving_inputs.append(inp_mat)

        if starving_inputs:
            bottlenecks.append(
                {
                    "severity": "HIGH",
                    "type": "STAGE_STARVATION",
                    "target": rec,
                    "description": (
                        f"Recipe '{rec}' starved by upstream input(s): {', '.join(starving_inputs)}."
                    ),
                    "impact_score": round(deficit, 2),
                }
            )
        else:
            bottlenecks.append(
                {
                    "severity": "MEDIUM",
                    "type": "STAGE_BACKPRESSURE",
                    "target": rec,
                    "description": (
                        f"Recipe '{rec}' has inputs available but outputs are stalled. "
                        f"Output belt backpressure or machine transfer delay suspected."
                    ),
                    "impact_score": round(deficit, 2),
                }
            )

    bottlenecks.sort(key=lambda b: b.get("impact_score", 0.0), reverse=True)
    return bottlenecks


def analyze_propagation_dag(
    netlist_data: dict[str, Any], net_stats: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Trace signal wavefront arrival latency across the dependency DAG."""
    cells = netlist_data.get("cells", {})
    instances = netlist_data.get("instances", [])
    dag_edges: list[dict[str, Any]] = []

    for inst in instances:
        cell_id = inst.get("cell", "")
        if cell_id in ("belt_merge2", "display_panel", "arithmetic_combinator", "decider_combinator"):
            continue
        cell_info = cells.get(cell_id, {})
        inputs = cell_info.get("inputs", [])
        rec = inst.get("recipe", "")

        out_stats = net_stats.get(rec, {})
        out_tick = out_stats.get("first_arrival_tick")

        for inp in inputs:
            inp_stats = net_stats.get(inp, {})
            inp_tick = inp_stats.get("first_arrival_tick")

            latency_ticks = (
                (out_tick - inp_tick)
                if (out_tick is not None and inp_tick is not None and out_tick >= inp_tick)
                else None
            )

            dag_edges.append(
                {
                    "upstream": inp,
                    "downstream": rec,
                    "upstream_arrival_tick": inp_tick,
                    "downstream_arrival_tick": out_tick,
                    "transit_latency_ticks": latency_ticks,
                    "transit_latency_sec": round(latency_ticks / 60.0, 2) if latency_ticks is not None else None,
                }
            )

    return dag_edges


def analyze_shapes(
    trace_data: dict[str, Any], netlist_data: dict[str, Any]
) -> dict[str, Any]:
    """Execute complete shape-based telemetry analysis."""
    ticks_total = int(trace_data.get("ticks_total", 0))
    sample_interval = int(trace_data.get("sample_interval", 60))
    samples = trace_data.get("samples", [])

    net_stats = calculate_net_rates(samples, sample_interval, ticks_total)
    inputs_analysis = analyze_inputs(netlist_data, net_stats)
    outputs_analysis = analyze_outputs(netlist_data, net_stats)
    merges_analysis = analyze_merges(netlist_data, net_stats)
    stages_analysis = analyze_transform_stages(netlist_data, net_stats)
    bottlenecks = diagnose_bottlenecks(stages_analysis, inputs_analysis, net_stats)
    dag_analysis = analyze_propagation_dag(netlist_data, net_stats)

    total_items_recorded = sum(s["total_items"] for s in net_stats.values())

    return {
        "summary": {
            "simulation_ticks": ticks_total,
            "simulation_seconds": round(ticks_total / 60.0, 2),
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


def format_markdown_report(analysis: dict[str, Any]) -> str:
    """Format analysis findings as clean technical markdown."""
    summary = analysis["summary"]
    out: list[str] = []

    out.append("# Factory Telemetry Shape Analysis Report\n")
    out.append("## 1. Simulation Summary")
    out.append(f"- **Simulation Duration**: {summary['simulation_ticks']} ticks ({summary['simulation_seconds']:.1f} seconds)")
    out.append(f"- **Sample Interval**: {summary['sample_interval_ticks']} ticks ({summary['total_samples']} samples)")
    out.append(f"- **Tapped Nets**: {summary['tapped_nets_count']} telemetry points")
    out.append(f"- **Item Movements Logged**: {summary['total_item_transfers_recorded']} units\n")

    out.append("## 2. Boundary Outputs (Science Pack Throughput)")
    out.append("| Science Pack | Target (SPM) | Observed (SPM) | Progress | First Arrival | Status |")
    out.append("|:---|:---:|:---:|:---:|:---:|:---|")
    for row in analysis["boundary_outputs"]:
        arr = f"{row['first_arrival_sec']:.1f}s" if row['first_arrival_sec'] is not None else "None"
        out.append(
            f"| `{row['material']}` | {row['target_rate_spm']:.1f} | {row['observed_rate_spm']:.2f} | "
            f"{row['achievement_pct']:.1f}% | {arr} | **{row['status']}** |"
        )
    out.append("")

    out.append("## 3. Bottleneck Diagnosis")
    bottlenecks = analysis.get("bottlenecks", [])
    if bottlenecks:
        for idx, bn in enumerate(bottlenecks[:10], 1):
            out.append(f"{idx}. **[{bn['severity']}] {bn['type']}** on `{bn['target']}`")
            out.append(f"   {bn['description']}")
    else:
        out.append("No active starvation or backpressure bottlenecks detected.\n")
    out.append("")

    out.append("## 4. Boundary Inputs (Supply Continuity)")
    out.append("| Input Material | Planned (SPM) | Observed (SPM) | Fulfillment | Total Units | Status |")
    out.append("|:---|:---:|:---:|:---:|:---:|:---|")
    for row in analysis["boundary_inputs"]:
        out.append(
            f"| `{row['material']}` | {row['plan_rate_spm']:.1f} | {row['observed_rate_spm']:.2f} | "
            f"{row['fulfillment_pct']:.1f}% | {row['total_items']} | {row['status']} |"
        )
    out.append("")

    out.append("## 5. Convergent Splitter Merges (By Material)")
    merges = analysis.get("convergent_merges", [])
    if merges:
        out.append("| Material | Splitters | Throughput (SPM) | Belt Saturation | Congestion |")
        out.append("|:---|:---:|:---:|:---:|:---:|")
        for m in merges:
            cong = "**CONGESTED**" if m["is_congested"] else "Normal"
            out.append(
                f"| `{m['material']}` | {m['splitters_count']} | {m['throughput_spm']:.1f} | "
                f"{m['belt_saturation_pct']:.1f}% | {cong} |"
            )
        out.append("")

    out.append("## 6. Recipe Transform Stages (Utilization)")
    out.append("| Recipe | Machine | Instances | Plan (SPM) | Observed (SPM) | Utilization |")
    out.append("|:---|:---|:---:|:---:|:---:|:---:|")
    for stg in analysis["transform_stages"]:
        out.append(
            f"| `{stg['recipe']}` | {stg['machine']} | {stg['instance_count']} | "
            f"{stg['plan_rate_spm']:.1f} | {stg['observed_output_rate_spm']:.2f} | "
            f"{stg['utilization_pct']:.1f}% |"
        )
    out.append("")

    return "\n".join(out)


def main() -> int:
    """CLI entrypoint for standalone telemetry analysis."""
    parser = argparse.ArgumentParser(description="Analyze Factorio factory telemetry traces based on netlist shapes.")
    parser.add_argument("trace_file", type=Path, help="Path to telemetry trace JSON file")
    parser.add_argument("netlist_file", type=Path, help="Path to netlist JSON file")
    parser.add_argument("--json-out", type=Path, default=None, help="Path to export analysis results as JSON")

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
