"""Unit tests for shape-based telemetry analysis."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "flow"))

import analyze_telemetry


class TestTelemetryAnalysis(unittest.TestCase):
    """Test telemetry rate calculation, shape analysis, and bottleneck diagnosis."""

    def setUp(self) -> None:
        self.netlist_data = {
            "inputs": ["iron-ore", "copper-ore"],
            "outputs": ["automation-science-pack"],
            "raw_rates_per_min": {
                "iron-ore": 120.0,
                "copper-ore": 60.0,
            },
            "demand": {
                "automation-science-pack": 60.0,
            },
            "cells": {
                "furnace_iron": {
                    "machine": "electric-furnace",
                    "inputs": ["iron-ore"],
                    "outputs": ["iron-plate"],
                },
                "gear_assembler": {
                    "machine": "assembling-machine-3",
                    "inputs": ["iron-plate"],
                    "outputs": ["iron-gear-wheel"],
                },
                "science_assembler": {
                    "machine": "assembling-machine-3",
                    "inputs": ["iron-gear-wheel", "copper-plate"],
                    "outputs": ["automation-science-pack"],
                },
            },
            "instances": [
                {
                    "name": "u_furnace_1",
                    "cell": "furnace_iron",
                    "recipe": "iron-plate",
                    "plan_rate_per_min": 120.0,
                    "rated_per_min": 150.0,
                },
                {
                    "name": "u_merge_iron_plate_1",
                    "cell": "belt_merge2",
                    "recipe": "iron-plate",
                },
                {
                    "name": "u_gear_1",
                    "cell": "gear_assembler",
                    "recipe": "iron-gear-wheel",
                    "plan_rate_per_min": 60.0,
                    "rated_per_min": 75.0,
                },
                {
                    "name": "u_science_1",
                    "cell": "science_assembler",
                    "recipe": "automation-science-pack",
                    "plan_rate_per_min": 60.0,
                    "rated_per_min": 75.0,
                },
            ],
        }

    def test_calculate_net_rates(self) -> None:
        """Test rate and arrival time calculations from telemetry samples."""
        samples = [
            {"tick": 0, "taps": {"iron-plate": 0, "copper-plate": 0}},
            {"tick": 60, "taps": {"iron-plate": 10, "copper-plate": 0}},
            {"tick": 120, "taps": {"iron-plate": 30, "copper-plate": 5}},
            {"tick": 3600, "taps": {"iron-plate": 120, "copper-plate": 60}},
        ]
        stats = analyze_telemetry.calculate_net_rates(samples, 60, 3600)

        self.assertIn("iron-plate", stats)
        self.assertEqual(stats["iron-plate"]["total_items"], 120)
        self.assertEqual(stats["iron-plate"]["rate_spm"], 120.0)
        self.assertEqual(stats["iron-plate"]["first_arrival_tick"], 60)

        self.assertIn("copper-plate", stats)
        self.assertEqual(stats["copper-plate"]["total_items"], 60)
        self.assertEqual(stats["copper-plate"]["rate_spm"], 60.0)
        self.assertEqual(stats["copper-plate"]["first_arrival_tick"], 120)

    def test_shape_analysis_complete(self) -> None:
        """Verify complete shape analysis pipeline."""
        samples = [
            {
                "tick": 0,
                "taps": {
                    "iron-ore": 0,
                    "iron-plate": 0,
                    "iron-gear-wheel": 0,
                    "automation-science-pack": 0,
                },
            },
            {
                "tick": 3600,
                "taps": {
                    "iron-ore": 120,
                    "iron-plate": 120,
                    "iron-gear-wheel": 60,
                    "automation-science-pack": 58,
                },
            },
        ]
        trace_data = {
            "ticks_total": 3600,
            "sample_interval": 60,
            "samples": samples,
        }

        results = analyze_telemetry.analyze_shapes(trace_data, self.netlist_data)

        # 1. Inputs check
        inputs = {i["material"]: i for i in results["boundary_inputs"]}
        self.assertEqual(inputs["iron-ore"]["status"], "SATISFIED")
        self.assertEqual(inputs["copper-ore"]["status"], "IDLE")

        # 2. Outputs check
        outputs = {o["material"]: o for o in results["boundary_outputs"]}
        self.assertEqual(outputs["automation-science-pack"]["status"], "ON_TARGET")
        self.assertAlmostEqual(outputs["automation-science-pack"]["observed_rate_spm"], 58.0)

        # 3. Merges check
        merges = results["convergent_merges"]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["material"], "iron-plate")
        self.assertEqual(merges[0]["splitters_count"], 1)

        # 4. Stages check
        stages = {s["recipe"]: s for s in results["transform_stages"]}
        self.assertIn("iron-plate", stages)
        self.assertIn("iron-gear-wheel", stages)
        self.assertEqual(stages["iron-plate"]["instance_count"], 1)

        # 5. Bottlenecks
        bottlenecks = results["bottlenecks"]
        # copper-ore is idle, but not an input deficit for iron stages
        report = analyze_telemetry.format_markdown_report(results)
        self.assertIn("Factory Telemetry Shape Analysis Report", report)
        self.assertIn("automation-science-pack", report)

    def test_bottleneck_starvation_vs_backpressure(self) -> None:
        """Test distinction between starvation and backpressure diagnoses."""
        # Scenario: gear assembler has input iron-plate = 0 -> starvation
        samples = [
            {"tick": 0, "taps": {"iron-plate": 0, "iron-gear-wheel": 0}},
            {"tick": 3600, "taps": {"iron-plate": 0, "iron-gear-wheel": 0}},
        ]
        trace_data = {"ticks_total": 3600, "sample_interval": 60, "samples": samples}
        results = analyze_telemetry.analyze_shapes(trace_data, self.netlist_data)

        bns = results["bottlenecks"]
        types = [b["type"] for b in bns]
        self.assertIn("STAGE_STARVATION", types)


if __name__ == "__main__":
    unittest.main()
