"""Unit tests for Level 1 macro floorplanning, recipe clustering, and channel routing."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from typing import Any

from foundry.entity_helpers import (
    DIRECTION_EAST,
    DIRECTION_NORTH,
    DIRECTION_SOUTH,
    DIRECTION_WEST,
    check_collisions,
    check_underground_pairing,
)
from foundry.macro_floorplan import MacroFloorplan, MacroFloorplanner, RecipeCluster


MOCK_NETLIST_DATA: dict[str, Any] = {
    "demand": {"automation-science-pack": 60.0},
    "fluids": ["crude-oil", "water"],
    "inputs": ["copper-ore", "iron-ore"],
    "outputs": ["automation-science-pack"],
    "cells": {
        "electric-furnace.iron-plate": {
            "cell": "electric-furnace.iron-plate",
            "recipe": "iron-plate",
            "width": 5,
            "height": 3,
            "inputs": ["iron-ore"],
            "outputs": ["iron-plate"],
        },
        "electric-furnace.copper-plate": {
            "cell": "electric-furnace.copper-plate",
            "recipe": "copper-plate",
            "width": 5,
            "height": 3,
            "inputs": ["copper-ore"],
            "outputs": ["copper-plate"],
        },
        "assembling-machine-3.iron-gear-wheel": {
            "cell": "assembling-machine-3.iron-gear-wheel",
            "recipe": "iron-gear-wheel",
            "width": 5,
            "height": 3,
            "inputs": ["iron-plate"],
            "outputs": ["iron-gear-wheel"],
        },
        "assembling-machine-3.automation-science-pack": {
            "cell": "assembling-machine-3.automation-science-pack",
            "recipe": "automation-science-pack",
            "width": 5,
            "height": 5,
            "inputs": ["copper-plate", "iron-gear-wheel"],
            "outputs": ["automation-science-pack"],
        },
    },
    "instances": [
        {
            "name": "u_iron_1",
            "cell": "electric-furnace.iron-plate",
            "recipe": "iron-plate",
            "rank": 0,
        },
        {
            "name": "u_iron_2",
            "cell": "electric-furnace.iron-plate",
            "recipe": "iron-plate",
            "rank": 0,
        },
        {
            "name": "u_copper_1",
            "cell": "electric-furnace.copper-plate",
            "recipe": "copper-plate",
            "rank": 0,
        },
        {
            "name": "u_gear_1",
            "cell": "assembling-machine-3.iron-gear-wheel",
            "recipe": "iron-gear-wheel",
            "rank": 1,
        },
        {
            "name": "u_sci_1",
            "cell": "assembling-machine-3.automation-science-pack",
            "recipe": "automation-science-pack",
            "rank": 2,
        },
        {
            "name": "u_sci_2",
            "cell": "assembling-machine-3.automation-science-pack",
            "recipe": "automation-science-pack",
            "rank": 2,
        },
    ],
}


class TestMacroFloorplan(unittest.TestCase):
    """Test suite for Level 1 macro floorplanning, clustering, and channel routing."""

    def setUp(self) -> None:
        self.floorplanner = MacroFloorplanner(max_block_area=8836.0)

    def test_cluster_netlist_grouping(self) -> None:
        """Verify grouping of instances by recipe and area calculation."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        self.assertEqual(len(clusters), 4)

        cluster_map = {c.recipe: c for c in clusters}
        self.assertIn("iron-plate", cluster_map)
        self.assertIn("copper-plate", cluster_map)
        self.assertIn("iron-gear-wheel", cluster_map)
        self.assertIn("automation-science-pack", cluster_map)

        # iron-plate has 2 instances of 5x3 = 30.0 tiles
        iron_cluster = cluster_map["iron-plate"]
        self.assertEqual(len(iron_cluster.instances), 2)
        self.assertEqual(iron_cluster.total_area, 30.0)
        self.assertEqual(iron_cluster.inputs, {"iron-ore"})
        self.assertEqual(iron_cluster.outputs, {"iron-plate"})

        # automation-science-pack has 2 instances of 5x5 = 50.0 tiles
        sci_cluster = cluster_map["automation-science-pack"]
        self.assertEqual(len(sci_cluster.instances), 2)
        self.assertEqual(sci_cluster.total_area, 50.0)
        self.assertEqual(sci_cluster.inputs, {"copper-plate", "iron-gear-wheel"})
        self.assertEqual(sci_cluster.outputs, {"automation-science-pack"})

    def test_multi_block_stage_allocation(self) -> None:
        """Verify dividing an oversized stage across multiple blocks."""
        # Set small max_block_area (20 tiles). A 30-tile cluster must split into 2 parts.
        small_planner = MacroFloorplanner(max_block_area=20.0)
        clusters = small_planner.cluster_netlist(MOCK_NETLIST_DATA)

        iron_parts = [c for c in clusters if c.recipe == "iron-plate"]
        self.assertEqual(len(iron_parts), 2)
        self.assertEqual(iron_parts[0].sub_cluster_index, 0)
        self.assertEqual(iron_parts[1].sub_cluster_index, 1)
        self.assertEqual(iron_parts[0].total_sub_clusters, 2)
        self.assertEqual(len(iron_parts[0].instances), 1)
        self.assertEqual(len(iron_parts[1].instances), 1)

    def test_plan_grid_dimensions_and_channels(self) -> None:
        """Verify grid dimensioning and instantiation of street channels and crossroads."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        # 4 clusters -> 2x2 grid
        floorplan = self.floorplanner.plan_grid(clusters)
        self.assertEqual(floorplan.grid_w, 2)
        self.assertEqual(floorplan.grid_h, 2)

        # Verify internal channels
        self.assertIn("h_channel_0_0", floorplan.channels)
        self.assertIn("h_channel_1_0", floorplan.channels)
        self.assertIn("v_channel_0_0", floorplan.channels)
        self.assertIn("v_channel_0_1", floorplan.channels)

        # Verify 4-way crossroad intersection
        self.assertIn((1, 1), floorplan.intersections)
        ix = floorplan.intersections[(1, 1)]
        self.assertEqual(ix.center_x, 100.0)
        self.assertEqual(ix.center_y, 100.0)

    def test_inter_block_route_assignment(self) -> None:
        """Verify inter-block nets allocate lanes in street channels."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        floorplan = self.floorplanner.plan_grid(clusters, grid_w=2, grid_h=2)
        self.floorplanner.assign_inter_block_routes(floorplan, MOCK_NETLIST_DATA)

        # Verify inter-block routes exist
        self.assertIn("iron-plate", floorplan.routes)
        self.assertIn("copper-plate", floorplan.routes)
        self.assertIn("iron-gear-wheel", floorplan.routes)

        iron_route = floorplan.routes["iron-plate"]
        self.assertGreater(len(iron_route.segments), 0)
        # Net must have allocated lanes in traversed channels
        for seg in iron_route.segments:
            ch = floorplan.channels[seg.channel_id]
            lane = ch.get_lane(seg.lane_index)
            self.assertTrue(lane.is_allocated)
            self.assertEqual(lane.net_name, "iron-plate")

    def test_emit_channel_entities_collision_free(self) -> None:
        """Verify emitted channel entities have zero collisions and valid undergrounds."""
        clusters = self.floorplanner.cluster_netlist(MOCK_NETLIST_DATA)
        floorplan = self.floorplanner.plan_grid(clusters, grid_w=2, grid_h=2)
        self.floorplanner.assign_inter_block_routes(floorplan, MOCK_NETLIST_DATA)

        entities = floorplan.emit_channel_entities()
        self.assertGreater(len(entities), 0)

        # Check for overlapping collisions
        collisions = check_collisions(entities)
        self.assertEqual(collisions, [])

        # Check underground belt pairing
        unpaired = check_underground_pairing(entities)
        self.assertEqual(unpaired, [])

    def test_full_pipeline_with_automation_science_fixture(self) -> None:
        """Verify macro floorplanning with actual science factory netlist fixture."""
        fixture_path = Path("flow/automation_science_netlist.json")
        if not fixture_path.exists():
            self.skipTest("automation_science_netlist.json fixture not found")

        netlist_data = json.loads(fixture_path.read_text(encoding="utf-8"))

        # Plan with small block area (80 tiles) to force multi-block allocation
        planner = MacroFloorplanner(max_block_area=80.0)
        floorplan = planner.plan(netlist_data)

        self.assertGreaterEqual(floorplan.grid_w, 2)
        self.assertGreaterEqual(floorplan.grid_h, 2)

        # Every instance must map to a block coordinate
        for inst in netlist_data["instances"]:
            iname = inst["name"]
            self.assertIn(iname, floorplan.instance_to_block)
            bx, by = floorplan.instance_to_block[iname]
            self.assertGreaterEqual(bx, 0)
            self.assertLess(bx, floorplan.grid_w)
            self.assertGreaterEqual(by, 0)
            self.assertLess(by, floorplan.grid_h)

        # Emit channel entities and verify zero collisions
        entities = floorplan.emit_channel_entities()
        self.assertGreater(len(entities), 0)
        collisions = check_collisions(entities)
        self.assertEqual(collisions, [])
